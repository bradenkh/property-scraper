#!/usr/bin/env python3
"""Scrape every parcel in Saratoga County from https://saratogacounty.prosgar.com.

Three resumable stages (run them in order, or use `all`):

  list     enumerate parcels per municipality via the search grid's AJAX endpoint
           -> <out>/parcels.csv
  details  fetch each parcel's detail page and parse it
           -> <out>/details.jsonl   (append-only; re-running skips finished parcels)
           NOTE: the server handles only ~0.7 pages/s no matter how many threads
           are used, so all ~103k parcels take roughly 40 hours.
           Requests are also throttled globally (--delay, default 1.5 s) and
           back off on 429/5xx. It is safe to
           Ctrl-C and re-run; finished parcels are skipped.
  export   flatten details.jsonl into tidy CSVs
           -> properties, buildings, owners, sales, deed_refs, lands,
              improvements, special_districts, exemptions

Usage:
  python saratoga_pros_scraper.py all --out data
  python saratoga_pros_scraper.py list --out data
  python saratoga_pros_scraper.py details --out data [--delay 1.5] [--limit 50]
  python saratoga_pros_scraper.py export --out data

Requires: requests, beautifulsoup4 (lxml optional).
"""
import argparse
import csv
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://saratogacounty.prosgar.com"
SEARCH_URL = BASE + "/PROSSearch/SearchFilters"
AJAX_URL = BASE + "/PROSSearch/GetAjax"
PARCEL_URL = BASE + "/PROSParcel/Parcel/{pid}?swis={swis}"
PAGE_SIZE = 500
TOKEN_RE = re.compile(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"')
USER_AGENT = "Mozilla/5.0 (compatible; property-scraper/1.0)"

PARCEL_COLUMNS = [
    "ParcelId", "SWIS", "Municipality", "SBL", "Address", "Owner", "LotSize",
    "PropertyType", "BuildingStyle", "YearBuilt", "SFLA", "NumBedrooms",
    "NumFullBaths", "NumHalfBaths", "Fireplaces", "RoomCounts", "Zoning",
    "MostRecentSaleDate", "MostRecentSalePrice",
]


def new_session():
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    # The site's EULA gate is only this client-side cookie (see Welcome.js).
    s.cookies.set("EULAAccepted", "true", domain="saratogacounty.prosgar.com")
    return s


class Throttle:
    """Global limiter: at most one request start per `delay` seconds, across
    all threads. Also supports a shared pause when the server pushes back."""

    def __init__(self, delay):
        self.delay = delay
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.delay
        if start > now:
            time.sleep(start - now)

    def pause(self, seconds):
        with self._lock:
            self._next = max(self._next, time.monotonic() + seconds)


THROTTLE = Throttle(1.5)


def request(session, method, url, retries=5, **kw):
    """Throttled HTTP with backoff on network errors, 429 and 5xx."""
    for attempt in range(retries):
        THROTTLE.wait()
        try:
            r = session.request(method, url, timeout=60, **kw)
            if r.status_code == 429 or r.status_code >= 500:
                try:
                    wait = float(r.headers.get("Retry-After", ""))
                except ValueError:
                    wait = 0
                # slow everyone down, not just this thread
                THROTTLE.pause(max(wait, 5 * 2 ** attempt))
                raise requests.HTTPError(f"HTTP {r.status_code}")
            r.raise_for_status()
            return r
        except requests.RequestException as e:
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


# --------------------------------------------------------------------------
# Stage 1: parcel list
# --------------------------------------------------------------------------

def get_municipalities(session):
    r = request(session, "GET", SEARCH_URL)
    soup = BeautifulSoup(r.text, "html.parser")
    sel = soup.find("select", id="Municipality")
    return [(o["value"], o.get_text(strip=True))
            for o in sel.find_all("option") if o.get("value")]


def list_municipality(muni_code):
    """Return every grid row for one municipality."""
    s = new_session()
    # The grid page takes its filters from the server-side session, so POST the
    # search form first, then read the grid page's own CSRF token.
    r = request(s, "GET", SEARCH_URL)
    tok = TOKEN_RE.search(r.text).group(1)
    request(s, "POST", SEARCH_URL, data={
        "__RequestVerificationToken": tok, "Address": "", "SBL": "",
        "Owner": "", "Street": "", "Municipality": muni_code})
    r = request(s, "GET", f"{BASE}/PROSSearch/SearchIndex?municipality={muni_code}")
    tok = TOKEN_RE.search(r.text).group(1)

    rows, start, total = [], 0, None
    while total is None or start < total:
        data = {"__RequestVerificationToken": tok, "draw": 1, "start": start,
                "length": PAGE_SIZE, "municipality": muni_code}
        for i in range(13):
            data[f"columns[{i}][data]"] = i
        j = request(s, "POST", AJAX_URL, data=data,
                    headers={"X-Requested-With": "XMLHttpRequest"}).json()
        total = j["recordsFiltered"]
        if not j["data"]:
            break
        rows.extend(j["data"])
        start += PAGE_SIZE
    if len(rows) != total:
        print(f"  WARNING {muni_code}: expected {total} rows, got {len(rows)}",
              file=sys.stderr)
    return rows


def stage_list(out):
    session = new_session()
    munis = get_municipalities(session)
    seen, parcels = set(), []
    for code, name in munis:
        rows = list_municipality(code)
        new = 0
        for row in rows:
            key = (row["SWIS"], row["ParcelId"])
            if key in seen:
                continue
            seen.add(key)
            parcels.append({c: row.get(c) for c in PARCEL_COLUMNS})
            new += 1
        print(f"{code} {name}: {len(rows)} rows ({new} new)")
    path = out / "parcels.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=PARCEL_COLUMNS)
        w.writeheader()
        w.writerows(parcels)
    print(f"Wrote {len(parcels)} parcels to {path}")


# --------------------------------------------------------------------------
# Stage 2: parcel detail pages
# --------------------------------------------------------------------------

def clean(el):
    return re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()


def slug(text):
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def parse_parcel(html):
    """Parse a detail page into a dict of scalar fields, grouped fields
    (e.g. each 'Building N'), and repeating tables."""
    soup = BeautifulSoup(html, "html.parser")
    main = soup.find("main") or soup
    out = {"fields": {}, "groups": {}, "tables": {}}

    title = main.find("h2")
    if title:
        out["title"] = clean(title)
    for h4 in main.find_all("h4"):
        m = re.match(r"(\d{4}) (.+ Assessment Information)", clean(h4))
        if m:
            out["fields"]["assessment_year"] = m.group(1)

    for table in main.find_all("table"):
        if table.find("th"):
            # repeating table: headers + data rows
            tid = table.get("id") or slug(clean(table.find("tr")))
            headers = [clean(th) for th in table.find_all("th")]
            rows = []
            for tr in table.find("tbody").find_all("tr") if table.find("tbody") else []:
                cells = [clean(td) for td in tr.find_all("td")]
                if len(cells) == len(headers) and any(cells):
                    rows.append({h: c for h, c in zip(headers, cells) if h})
            out["tables"][tid] = rows
            continue
        # key/value table; colspan header rows start a new group (e.g. Building 1)
        group = None
        for tr in table.find_all("tr"):
            tds = tr.find_all("td", recursive=False)
            if len(tds) == 1 and tds[0].get("colspan"):
                group = clean(tds[0])
                out["groups"].setdefault(group, {})
                continue
            if len(tds) != 2:
                continue
            label, value = clean(tds[0]), clean(tds[1])
            key = tr.get("id") or slug(label)
            if not label:
                continue
            target = out["groups"][group] if group else out["fields"]
            target[key] = value
            target.setdefault("_labels", {})[key] = label

    # Hidden 'No Exemption Data Found' text is the only trace of empty exemptions
    out["no_exemptions"] = "No Exemption Data Found" in main.get_text()
    return out


def fetch_detail(session_local, parcel):
    url = PARCEL_URL.format(pid=parcel["ParcelId"], swis=parcel["SWIS"])
    r = request(session_local, "GET", url)
    data = parse_parcel(r.text)
    data["ParcelId"], data["SWIS"] = int(parcel["ParcelId"]), parcel["SWIS"]
    data["SBL"] = parcel["SBL"]
    return data


# Property types (as shown in the search grid) that count as a place to live.
# Manufactured housing, seasonal camps and farms are deliberately left out.
SINGLE_FAMILY = {"1 Family Res", "Rural res", "Rurl res&rec", "Rural res&ag"}
MULTI_FAMILY = {"2 Family Res", "3 Family Res", "1 Fam Res w/Apt", "Res Multiple",
                "Multiple res", "Converted Res", "Apartment"}


def num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def beds_baths(p):
    """The grid's 'Bedrooms/Baths' column is 'beds / baths' (blank if unknown)."""
    parts = (p.get("RoomCounts") or "").split("/")
    if len(parts) != 2:
        return 0.0, 0.0
    return num(parts[0]), num(parts[1])


def select_parcels(parcels, a):
    """Apply the --segment / narrowing filters using only list-stage data."""
    if a.segment == "all":
        types = None
    else:
        types = {"single": SINGLE_FAMILY, "multi": MULTI_FAMILY,
                 "both": SINGLE_FAMILY | MULTI_FAMILY}[a.segment]
    munis = [m.lower() for m in a.municipality]
    out = []
    for p in parcels:
        if types is not None and p["PropertyType"] not in types:
            continue
        if a.segment != "all" and num(p["SFLA"]) <= 0:      # built only
            continue
        if munis and not any(m in p["Municipality"].lower() for m in munis):
            continue
        beds, baths = beds_baths(p)
        if a.min_beds and beds < a.min_beds:
            continue
        if a.min_baths and baths < a.min_baths:
            continue
        if a.min_sqft and num(p["SFLA"]) < a.min_sqft:
            continue
        if a.max_sqft and num(p["SFLA"]) > a.max_sqft:
            continue
        if a.min_year and num(p["YearBuilt"]) < a.min_year:
            continue
        if a.max_year and num(p["YearBuilt"]) > a.max_year:
            continue
        out.append(p)
    return out


def stage_details(out, workers, limit, a):
    with open(out / "parcels.csv", newline="", encoding="utf-8") as f:
        parcels = select_parcels(list(csv.DictReader(f)), a)
    print(f"{len(parcels)} parcels match the selection")
    path = out / "details.jsonl"
    done = set()
    if path.exists():
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    d = json.loads(line)
                    done.add((d["SWIS"], str(d["ParcelId"])))
                except (ValueError, KeyError):
                    pass  # truncated last line from an interrupted run
    todo = [p for p in parcels if (p["SWIS"], p["ParcelId"]) not in done]
    if limit:
        todo = todo[:limit]
    print(f"{len(done)} done, {len(todo)} to fetch")

    local = threading.local()

    def work(p):
        if not hasattr(local, "s"):
            local.s = new_session()
        return fetch_detail(local.s, p)

    lock, failures, n = threading.Lock(), [], 0
    start = time.time()
    with open(path, "a", encoding="utf-8") as fout, \
            ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(work, p): p for p in todo}
        for fut in as_completed(futs):
            p = futs[fut]
            try:
                d = fut.result()
            except Exception as e:
                failures.append((p["SWIS"], p["ParcelId"], str(e)))
                continue
            with lock:
                fout.write(json.dumps(d) + "\n")
                n += 1
                if n % 200 == 0:
                    fout.flush()
                    rate = n / (time.time() - start)
                    print(f"  {n}/{len(todo)}  {rate:.1f}/s  "
                          f"eta {(len(todo) - n) / rate / 60:.0f} min")
    if failures:
        print(f"{len(failures)} failures (re-run `details` to retry), e.g. {failures[:3]}")


# --------------------------------------------------------------------------
# Stage 3: export
# --------------------------------------------------------------------------

TABLE_FILES = {
    "parcel-owners": "owners", "parcel-sales": "sales",
    "parcel-deedrefs": "deed_refs", "parcel-lands": "lands",
    "parcel-improvs": "improvements", "parcel-specdists": "special_districts",
}


def write_csv(path, rows):
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    print(f"{path}: {len(rows)} rows")


def stage_export(out):
    buckets = {n: [] for n in list(TABLE_FILES.values()) + ["exemptions", "other_tables"]}
    properties, buildings = [], []
    with open(out / "details.jsonl", encoding="utf-8") as f:
        for line in f:
            try:
                d = json.loads(line)
            except ValueError:
                continue
            ident = {"ParcelId": d["ParcelId"], "SWIS": d["SWIS"], "SBL": d["SBL"]}
            fields = {k: v for k, v in d["fields"].items() if k != "_labels"}
            properties.append({**ident, "title": d.get("title", ""), **fields})
            for gname, g in d["groups"].items():
                buildings.append({**ident, "group": gname,
                                  **{k: v for k, v in g.items() if k != "_labels"}})
            for tid, rows in d["tables"].items():
                name = TABLE_FILES.get(tid)
                if name is None:
                    name = "exemptions" if "exempt" in tid else "other_tables"
                for row in rows:
                    extra = {"table": tid} if name == "other_tables" else {}
                    buckets[name].append({**ident, **extra, **row})
    write_csv(out / "properties.csv", properties)
    write_csv(out / "buildings.csv", buildings)
    for name, rows in buckets.items():
        if rows:
            write_csv(out / f"{name}.csv", rows)


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", choices=["list", "details", "export", "all"])
    ap.add_argument("--out", default="data", type=Path)
    ap.add_argument("--workers", type=int, default=1,
                    help="parallel requests; the server gains nothing past 1-2")
    ap.add_argument("--delay", type=float, default=1.5,
                    help="minimum seconds between request starts, across all "
                         "threads (default 1.5, i.e. <= 0.67 req/s)")
    ap.add_argument("--limit", type=int, default=0,
                    help="only fetch N details (for testing)")
    g = ap.add_argument_group("selection (details stage; uses list data only)")
    g.add_argument("--segment", choices=["both", "single", "multi", "all"],
                   default="both",
                   help="built residential parcels: single-family, multi-family, "
                        "both (default), or all parcels with no filtering")
    g.add_argument("--municipality", action="append", default=[],
                   help="substring of municipality name, repeatable "
                        "(e.g. --municipality Malta --municipality Wilton)")
    g.add_argument("--min-beds", type=int, default=0)
    g.add_argument("--min-baths", type=float, default=0)
    g.add_argument("--min-sqft", type=int, default=0)
    g.add_argument("--max-sqft", type=int, default=0)
    g.add_argument("--min-year", type=int, default=0, help="year built")
    g.add_argument("--max-year", type=int, default=0)
    g.add_argument("--count", action="store_true",
                   help="just print how many parcels the selection matches")
    a = ap.parse_args()
    THROTTLE.delay = a.delay
    a.out.mkdir(parents=True, exist_ok=True)
    if a.count:
        with open(a.out / "parcels.csv", newline="", encoding="utf-8") as f:
            n = len(select_parcels(list(csv.DictReader(f)), a))
        print(f"{n} parcels, ~{n * a.delay / 3600:.1f} h of detail fetching")
        return
    if a.stage in ("list", "all"):
        stage_list(a.out)
    if a.stage in ("details", "all"):
        stage_details(a.out, a.workers, a.limit, a)
    if a.stage in ("export", "all"):
        stage_export(a.out)


if __name__ == "__main__":
    main()
