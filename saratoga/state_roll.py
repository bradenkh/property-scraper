"""NYS Office of Real Property Tax Services assessment rolls on data.ny.gov.

Dataset 7vem-aaz7, "Property Assessment Data from Local Assessment Rolls",
is published as open data and adds what the county GIS layer lacks: full
market value, every owner's name, taxable values and exemption codes.
"""

import pandas as pd

DATASET_URL = "https://data.ny.gov/resource/7vem-aaz7.json"
PAGE_SIZE = 50000


def latest_roll_year(session):
    rows = session.get_json(DATASET_URL, {
        "$select": "max(roll_year) AS year",
        "$where": "county_name='Saratoga'",
    })
    return int(rows[0]["year"])


def fetch_roll(session, roll_year, progress=None):
    rows = []
    offset = 0
    while True:
        page = session.get_json(DATASET_URL, {
            "$where": f"county_name='Saratoga' AND roll_year='{roll_year}'",
            "$order": ":id",
            "$limit": PAGE_SIZE,
            "$offset": offset,
        })
        rows.extend(page)
        offset += len(page)
        if progress:
            progress(f"state roll {roll_year}: {offset} rows")
        if len(page) < PAGE_SIZE:
            break
    return pd.DataFrame(rows)
