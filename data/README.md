# Data directory

- `universe.csv` — point-in-time listing metadata used by
  `scripts/audit_data.py`. The dates are **approximate inception dates** and the
  file contains **no delisted funds**, so it is a schema/audit aid, not a
  survivorship-bias-free universe. Replace it with vendor PIT metadata before
  drawing investable conclusions.
- `samples/` — generated price and spread datasets. The price CSV is produced by
  `scripts/fetch_sample_data.py`; the spread snapshot by
  `scripts/fetch_etf_spreads.py`. CSVs in `samples/` are git-ignored.

See `docs/data-audit.md` for the metadata formats and the current audit.
