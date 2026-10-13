# property-scraper

Pulls every parcel in Saratoga County, NY (~103k) from public government data
into two CSVs you can filter for homes worth pursuing.

```
pip install -r requirements.txt
python -m saratoga.build --out data
```

A full run makes ~210 requests, one per second, and takes a few minutes.

## Output

`data/saratoga_parcels.csv`, one row per parcel on the current (2026) county roll:

| Column | Source |
| --- | --- |
| `swis`, `print_key`, `sbl` | Parcel ids (SWIS + print key is the unique key) |
| `municipality`, `address`, `school_district` | County GIS |
| `owner`, `mail_address`, `mail_city`, `mail_state`, `mail_zip` | County GIS |
| `all_owners` | State roll: every owner's name, `;`-separated |
| `likely_owner_occupied` | Mailing address matches the property address |
| `property_class_code`, `property_class` | County GIS (210 = 1 family, 220 = 2 family, 230 = 3 family, 311 = residential vacant land…) |
| `acres`, `frontage_ft`, `depth_ft` | County GIS |
| `land_assessed_value`, `total_assessed_value` | County GIS |
| `full_market_value`, `county_taxable_value`, `school_taxable_value` | State roll (one year behind the county roll) |
| `exemption_codes` | State roll (e.g. 41800/41801 senior, 41834/41854 STAR, 4112x veterans) |
| `water`, `sewer`, `utilities` | County GIS |
| `deed_book`, `deed_page` | County GIS: the most recent recorded deed |
| `current_deed_first_seen`, `transferred_since_2023` | Derived: the roll year the current deed first appeared |
| `grid_east`, `grid_north` | NY State Plane East (feet) |

`data/saratoga_deed_history.csv`: one row per distinct deed seen on each
parcel in the 2023–2026 rolls, with the owner at the time. A new deed between
roll years means the property changed hands.

## Sources

- **Saratoga County GIS** parcel layers behind the county's public map viewer
  (`spatialags.vhb.com/.../NY_County_Saratoga/MapServer`).
- **NYS ORPTS assessment rolls** on data.ny.gov (dataset `7vem-aaz7`), published as open data.

### Not included

Building details (living sq ft, bedrooms, bathrooms, year built, style) and
sale prices exist only in the county's PROS site (`saratogacounty.prosgar.com`).
Its license limits use to reviewing and appealing assessments and forbids
downloading or copying the data, so this project does not scrape it. The
county's Real Property Tax Service office (518-885-2219) can provide the
assessment roll extract, including residential inventory and sales, on request
or through a FOIL request.

`webscraper.ipynb` is the earlier Selenium scraper for the county's old
ImageMate site (`saratoga.sdgnys.com`), which now sits behind a Cloudflare
challenge and no longer serves pages.
