# Ozon Analytics

A set of standalone Python scripts for daily and weekly Ozon marketplace
analytics. The project pulls data from the Ozon Seller API and Performance API,
updates Google Sheets, sends messages to bots, and writes part of the analytics
into PostgreSQL for use in Grafana.

> This is a cleaned, public extract of a production system. No secrets, no
> product catalog and no generated data are committed. To run it, copy
> `.env.example` to `.env` and `constants.example.py` to `constants.py`, and
> provide your own `google-creds.json` (gitignored).

## What it solves

Regular operational Ozon reporting, split across focused scripts run by cron:
summaries, SKU analytics, operational metrics, price segments, weekly analytics,
supply needs and database loading. There is no single central entry point by
design; each script owns one slice.

## Features

- daily Ozon metrics into Google Sheets;
- weekly and comparative reports;
- price-segment calculation;
- data export into PostgreSQL (Grafana backend);
- messages to bots;
- separate operational-analytics and supply-needs jobs.

## Stack

Python, requests, pandas, gspread, oauth2client, openpyxl, psycopg2-binary,
python-dotenv.

## Project structure

```text
ozon-analytics/
├── constants.example.py   # copy to constants.py and fill with your mappings
├── ozon_to_sheets.py
├── weekly_sum.py
├── week_live.py
├── weekfindyn.py
├── grafana_report.py
├── daily.py
├── dailyhour.py
├── seg.py / segW.py
├── ozon_google_op.py
├── ozon_bot.py
├── ozon_obor.py
├── obor_excel.py
├── finance_accrual.py     # finance/accrual compatibility layer (see below)
└── .env.example
```

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env                 # fill OZON_*, PG_*, sheet names
cp constants.example.py constants.py # fill SKU / warehouse mappings
# place your Google service-account key as google-creds.json (gitignored)
```

## Entry points

There is no single `main.py`. Run a specific script:

```bash
python ozon_to_sheets.py
python grafana_report.py
python weekly_sum.py
python daily.py
python dailyhour.py
python week_live.py
python seg.py
```

## finance/transaction to finance/accrual migration (Ozon, July 2026)

The methods `/v3/finance/transaction/list` and `/v3/finance/transaction/totals`
are being deprecated and switched off by Ozon on 3-6 July 2026. The replacement
is the new `/v1/finance/accrual/{types,by-day,postings}` family.

To avoid editing the same logic in 7 scripts, a single layer **`finance_accrual.py`**
mirrors the old interface:

| Function | Replaces | Returns |
|---|---|---|
| `totals(date)` | `transaction/totals` for a day | dict with the same keys (`accruals_for_sale`, `sale_commission`, `processing_and_delivery`, ...) and signs |
| `totals_period(d1, d2)` | `transaction/totals` for a period | the same, summed by day |
| `sales_by_sku(date)` | per-SKU sales from `transaction/list` | `{sku: amount}` |
| `sales_total(date)` | total sales for a day | amount |
| `accrual_types()` | (none) | `{type_id: name}` (reference, cached) |
| `postings(numbers)` | `accrual/postings` | raw accruals per posting |

Usage:

```python
from finance_accrual import totals, sales_by_sku

t = totals("2026-06-17")          # drop-in for the old commission logic
sku_amount = sales_by_sku("2026-06-17")
```

The `type_id` to category mapping (`CATEGORY_MAP` in the module) was reconciled
to the cent against the old `totals`. An unknown `type_id` falls into
`others_amount` and prints `[accrual] UNMAPPED type_id=...`; the total stays
correct, but the type should be assigned to the right category by hand.

**`probe_accrual.py`** is a read-only exploration script: it calls the three new
methods for a date and compares them with the old ones while they are still
alive. Run it on several dates and close all `UNMAPPED` before the final switch:

```bash
python probe_accrual.py 2026-06-15
python finance_accrual.py 2026-06-15   # quick self-check of totals()
```

Notes on the new methods: amounts arrive as strings (`"-42.7"`), `by-day`
paginates via a `last_id` cursor, and `accrual/postings` takes posting numbers
(not dates) and can be empty for fresh postings, so the workhorse for regular
reports is `by-day`.

## Notes

- Flat script architecture: shared business logic is spread across files.
- No secrets are committed: credentials live in `.env` and `google-creds.json`
  (both gitignored); the product catalog lives in `constants.py` (gitignored,
  see `constants.example.py`).
- Some jobs depend on the Google Sheets structure and the mappings in
  `constants.py`.
- Some dates and modes are computed automatically as "yesterday", convenient for
  cron but worth checking for manual backfill.

See `CONFIG.md` and `RUNBOOK.md` for environment variables and cron details.

## License

MIT, see [LICENSE](LICENSE).
