"""Saratoga County's public GIS parcel layers.

The county's map viewer (https://spatial.vhb.com/SaratogaMapViewer/) is backed
by an ArcGIS MapServer with one parcel layer per roll year. Each layer carries
the owner, mailing address, acreage, frontage, assessed value, property class,
water/sewer and the deed book/page of the most recent recorded deed.
"""

import pandas as pd

SERVICE_URL = "https://spatialags.vhb.com/arcgis/rest/services/29820_Saratoga/NY_County_Saratoga/MapServer"

# Roll year -> layer id. Only 2023 onward carry DEED_BOOK / DEED_PAGE.
PARCEL_LAYERS = {2026: 57, 2025: 74, 2024: 73, 2023: 72}
PAGE_SIZE = 2000  # the service's maxRecordCount


def fetch_layer(session, roll_year, fields="*", progress=None):
    """Return every parcel in one roll year's layer as a DataFrame (no geometry)."""
    url = f"{SERVICE_URL}/{PARCEL_LAYERS[roll_year]}/query"
    total = session.get_json(url, {"where": "1=1", "returnCountOnly": "true", "f": "json"})["count"]
    rows = []
    offset = 0
    while True:
        payload = session.get_json(url, {
            "where": "1=1",
            "outFields": fields,
            "returnGeometry": "false",
            "orderByFields": "OBJECTID",
            "resultOffset": offset,
            "resultRecordCount": PAGE_SIZE,
            "f": "json",
        })
        features = payload.get("features", [])
        rows.extend(f["attributes"] for f in features)
        offset += len(features)
        if progress:
            progress(f"county GIS {roll_year}: {offset}/{total}")
        if not features or not payload.get("exceededTransferLimit"):
            break
    return pd.DataFrame(rows)


def deed_history(layers):
    """Turn per-year deed references into one row per parcel per deed seen.

    `layers` maps roll year -> DataFrame with SWIS, PRINT_KEY, DEED_BOOK,
    DEED_PAGE, OWNER. A new deed reference between two roll years means the
    parcel was transferred in between (a sale, gift or refinance-free
    re-deeding; the price is not public in this source).
    """
    frames = []
    for year, df in layers.items():
        frame = df[["SWIS", "PRINT_KEY", "DEED_BOOK", "DEED_PAGE", "OWNER"]].copy()
        frame["roll_year"] = year
        frames.append(frame)
    history = pd.concat(frames, ignore_index=True)
    history = history.dropna(subset=["DEED_BOOK", "DEED_PAGE"])
    history = history.sort_values(["SWIS", "PRINT_KEY", "roll_year"])
    # Keep the first roll year each distinct deed appears in.
    history = history.drop_duplicates(["SWIS", "PRINT_KEY", "DEED_BOOK", "DEED_PAGE"], keep="first")
    return history.rename(columns={
        "SWIS": "swis",
        "PRINT_KEY": "print_key",
        "DEED_BOOK": "deed_book",
        "DEED_PAGE": "deed_page",
        "OWNER": "owner",
        "roll_year": "first_seen_roll_year",
    }).reset_index(drop=True)
