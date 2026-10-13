import pandas as pd

from saratoga.build import build, likely_owner_occupied, summarize_state_roll
from saratoga.county_gis import deed_history


def _layer(deeds):
    return pd.DataFrame(
        [{"SWIS": "412001", "PRINT_KEY": key, "DEED_BOOK": book, "DEED_PAGE": page, "OWNER": owner}
         for key, book, page, owner in deeds]
    )


def test_deed_history_keeps_first_year_each_deed_appears():
    layers = {
        2023: _layer([("1", "2010", "5", "SMITH"), ("2", "2001", "9", "JONES")]),
        2024: _layer([("1", "2010", "5", "SMITH"), ("2", "2024", "77", "DOE")]),
        2025: _layer([("1", "2010", "5", "SMITH"), ("2", "2024", "77", "DOE")]),
    }
    history = deed_history(layers)
    assert history[history.print_key == "1"].first_seen_roll_year.tolist() == [2023]
    assert history[history.print_key == "2"].first_seen_roll_year.tolist() == [2023, 2024]


def test_build_flags_transfers_and_joins_state_roll():
    gis = pd.DataFrame([
        {"SWIS": "412001", "PRINT_KEY": "1", "MUNI": "Ballston Spa", "PROP_ADDR": "163 WEST HIGH ST",
         "MAIL_1ADDR": "163  West High Street", "DEED_BOOK": "2010", "DEED_PAGE": "5"},
        {"SWIS": "412001", "PRINT_KEY": "2", "MUNI": "Ballston Spa", "PROP_ADDR": "10 MAIN ST",
         "MAIL_1ADDR": "PO Box 4", "DEED_BOOK": "2024", "DEED_PAGE": "77"},
    ])
    history = deed_history({
        2023: _layer([("1", "2010", "5", "SMITH"), ("2", "2001", "9", "JONES")]),
        2024: _layer([("1", "2010", "5", "SMITH"), ("2", "2024", "77", "DOE")]),
    })
    state = summarize_state_roll(pd.DataFrame([{
        "swis_code": "412001", "print_key_code": "1", "roll_year": "2025",
        "primary_owner_first_name": "Dorothy", "primary_owner_last_name": "Smith",
        "additional_owner_1_first": "Al", "additional_owner_1_last_name": "Smith",
        "full_market_value": "250000", "exemption_code_1": "41854",
    }]))

    parcels = build(gis, history, state).set_index("print_key")

    assert parcels.loc["1", "transferred_since_2023"] == False  # noqa: E712
    assert parcels.loc["2", "transferred_since_2023"] == True  # noqa: E712
    assert parcels.loc["1", "all_owners"] == "Dorothy Smith; Al Smith"
    assert parcels.loc["1", "full_market_value"] == 250000
    assert parcels.loc["1", "exemption_codes"] == "41854"
    assert parcels.loc["1", "likely_owner_occupied"] == True  # noqa: E712
    assert parcels.loc["2", "likely_owner_occupied"] is None
    assert pd.isna(parcels.loc["2", "full_market_value"])


def test_likely_owner_occupied():
    assert likely_owner_occupied("12 Oak Ln", "12 OAK LANE")
    assert not likely_owner_occupied("12 Oak Ln", "40 Elm St")
    assert likely_owner_occupied("Oak Ln", "12 Oak Ln") is None
