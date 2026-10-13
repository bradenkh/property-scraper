"""Pull every Saratoga County parcel and write one combined CSV.

    python -m saratoga.build --out data

Writes:
    data/saratoga_parcels.csv      one row per parcel (current county roll)
    data/saratoga_deed_history.csv one row per deed seen per parcel since 2023
"""

import argparse
import datetime
import os
import re
import sys

import pandas as pd

from saratoga import county_gis, state_roll
from saratoga.http import PoliteSession

GIS_FIELDS = ",".join([
    "SWIS", "PRINT_KEY", "SBL", "MUNI", "PROP_ADDR", "ROLL_YEAR", "OWNER",
    "MAIL_1ADDR", "MAIL_2ADDR", "MAIL_CITY", "MAIL_STATE", "MAIL_ZIP",
    "ACRES", "FRONTAGE", "DEPTH", "PRP_CLS_CODE", "PROP_CLASS", "LAND_AV", "TOTAL_AV",
    "SCHOOL", "WATER", "SEWER", "UTILITIES", "DEED_BOOK", "DEED_PAGE", "EASTING", "NORTHING",
])
HISTORY_FIELDS = "SWIS,PRINT_KEY,OWNER,DEED_BOOK,DEED_PAGE"


def _name(row, prefix, last_key):
    parts = [row.get(f"{prefix}first"), row.get(f"{prefix}mi"), row.get(last_key), row.get(f"{prefix}suffix")]
    return " ".join(str(p) for p in parts if isinstance(p, str) and p.strip())


def summarize_state_roll(roll):
    """Collapse the state roll's wide owner/exemption columns into a few useful ones."""
    def owners(row):
        names = [_name(row, "primary_owner_", "primary_owner_last_name")]
        for i in (1, 2, 3):
            names.append(_name(row, f"additional_owner_{i}_", f"additional_owner_{i}_last_name"))
        return "; ".join(n for n in names if n)

    # Primary owner columns are named primary_owner_first_name, not primary_owner_first.
    roll = roll.rename(columns={"primary_owner_first_name": "primary_owner_first"})
    exemption_cols = [c for c in roll.columns if re.fullmatch(r"exemption_code_\d+", c)]
    out = pd.DataFrame({
        "swis": roll["swis_code"],
        "print_key": roll["print_key_code"],
        "state_roll_year": roll["roll_year"],
        "all_owners": roll.apply(owners, axis=1),
        "full_market_value": pd.to_numeric(roll.get("full_market_value"), errors="coerce"),
        "county_taxable_value": pd.to_numeric(roll.get("county_taxable_value"), errors="coerce"),
        "school_taxable_value": pd.to_numeric(roll.get("school_taxable"), errors="coerce"),
        "roll_section": roll.get("roll_section"),
        "exemption_codes": roll[exemption_cols].apply(
            lambda r: ";".join(sorted({str(v) for v in r if isinstance(v, str) and v.strip()})), axis=1
        ) if exemption_cols else "",
    })
    return out.drop_duplicates(["swis", "print_key"])


def _addr_key(address):
    """House number + first street word, e.g. '163 West High St' -> '163 WEST'."""
    if not isinstance(address, str):
        return None
    tokens = re.sub(r"[^A-Z0-9 ]", " ", address.upper()).split()
    if len(tokens) < 2 or not tokens[0][0].isdigit():
        return None
    return f"{tokens[0]} {tokens[1]}"


def deed_year(deed_book):
    """Saratoga has numbered deed books by recording year since 2005
    (book 2018 = recorded in 2018). Older books are numbered below 2000, so
    for those we only know the deed predates 2005."""
    try:
        book = int(deed_book)
    except (TypeError, ValueError):
        return None
    return book if 2005 <= book <= datetime.date.today().year else None


def likely_owner_occupied(property_addr, mailing_addr):
    p, m = _addr_key(property_addr), _addr_key(mailing_addr)
    if p is None or m is None:
        return None
    return p == m


def build(gis_current, history, state_summary):
    parcels = gis_current.rename(columns={
        "SWIS": "swis", "PRINT_KEY": "print_key", "SBL": "sbl", "MUNI": "municipality",
        "PROP_ADDR": "address", "ROLL_YEAR": "roll_year", "OWNER": "owner",
        "MAIL_1ADDR": "mail_address", "MAIL_2ADDR": "mail_address_2", "MAIL_CITY": "mail_city",
        "MAIL_STATE": "mail_state", "MAIL_ZIP": "mail_zip", "ACRES": "acres",
        "FRONTAGE": "frontage_ft", "DEPTH": "depth_ft", "PRP_CLS_CODE": "property_class_code",
        "PROP_CLASS": "property_class", "LAND_AV": "land_assessed_value",
        "TOTAL_AV": "total_assessed_value", "SCHOOL": "school_district", "WATER": "water",
        "SEWER": "sewer", "UTILITIES": "utilities", "DEED_BOOK": "deed_book",
        "DEED_PAGE": "deed_page", "EASTING": "grid_east", "NORTHING": "grid_north",
    })
    parcels["likely_owner_occupied"] = [
        likely_owner_occupied(a, m) for a, m in zip(parcels["address"], parcels["mail_address"])
    ]

    parcels["last_deed_year"] = parcels["deed_book"].map(deed_year)

    # The roll year the current deed first appeared in. If that is later than
    # the earliest year we looked at, the parcel was re-deeded in between; if
    # the owner name also changed, it most likely sold.
    earliest = history["first_seen_roll_year"].min()
    current_deed = history.merge(
        parcels[["swis", "print_key", "deed_book", "deed_page"]],
        on=["swis", "print_key", "deed_book", "deed_page"],
    )[["swis", "print_key", "first_seen_roll_year"]]
    current_deed = current_deed.rename(columns={"first_seen_roll_year": "current_deed_first_seen"})
    first_owner = (
        history.sort_values("first_seen_roll_year")
        .drop_duplicates(["swis", "print_key"])[["swis", "print_key", "owner"]]
        .rename(columns={"owner": "_first_owner"})
    )
    parcels = parcels.merge(current_deed, on=["swis", "print_key"], how="left")
    parcels = parcels.merge(first_owner, on=["swis", "print_key"], how="left")
    parcels[f"redeeded_since_{earliest}"] = parcels["current_deed_first_seen"] > earliest
    parcels[f"owner_changed_since_{earliest}"] = (
        parcels[f"redeeded_since_{earliest}"]
        & parcels["_first_owner"].notna()
        & (parcels["_first_owner"] != parcels["owner"])
    )
    parcels = parcels.drop(columns="_first_owner")

    parcels = parcels.merge(state_summary, on=["swis", "print_key"], how="left")
    return parcels.sort_values(["municipality", "address"]).reset_index(drop=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", default="data", help="output directory (default: data)")
    parser.add_argument("--delay", type=float, default=1.0, help="seconds between requests (default: 1)")
    args = parser.parse_args(argv)

    session = PoliteSession(delay_seconds=args.delay)
    log = lambda msg: print(msg, file=sys.stderr, flush=True)

    current_year = max(county_gis.PARCEL_LAYERS)
    layers = {current_year: county_gis.fetch_layer(session, current_year, GIS_FIELDS, log)}
    for year in sorted(county_gis.PARCEL_LAYERS, reverse=True)[1:]:
        layers[year] = county_gis.fetch_layer(session, year, HISTORY_FIELDS, log)
    history = county_gis.deed_history(layers)

    roll_year = state_roll.latest_roll_year(session)
    state_summary = summarize_state_roll(state_roll.fetch_roll(session, roll_year, log))

    parcels = build(layers[current_year], history, state_summary)

    os.makedirs(args.out, exist_ok=True)
    parcels_path = os.path.join(args.out, "saratoga_parcels.csv")
    history_path = os.path.join(args.out, "saratoga_deed_history.csv")
    parcels.to_csv(parcels_path, index=False)
    history.to_csv(history_path, index=False)
    matched = parcels["state_roll_year"].notna().mean()
    log(f"wrote {len(parcels)} parcels to {parcels_path} ({matched:.1%} matched to the {roll_year} state roll)")
    log(f"wrote {len(history)} deed records to {history_path}")


if __name__ == "__main__":
    main()
