# property-scraper

## Saratoga County (saratogacounty.prosgar.com)

```
pip install -r requirements.txt
python saratoga_pros_scraper.py list    --out data   # ~4 min, all ~103k parcels -> data/parcels.csv
python saratoga_pros_scraper.py details --out data   # resumable, ~40 h for everything
python saratoga_pros_scraper.py export  --out data   # tidy CSVs from data/details.jsonl
```

`parcels.csv` (owner, SBL, address, lot size, type, style, year built, sq ft, beds/baths,
last sale) is useful on its own. `details` adds assessments, building details, owners and
mailing addresses, sales history, land, improvements, special districts and exemptions.
Villages are subsets of towns on this site, so parcels are de-duplicated by (SWIS, ParcelId).

`webscraper.ipynb` is the older notebook for a different site (saratoga.sdgnys.com).
