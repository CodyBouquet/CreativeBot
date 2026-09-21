# Inventory — Low Stock report

Evaluates **every stocked SKU** in Rollmaster (BMS) against the reorder point and
safety stock **entered in BMS**, and emails the ones that need reordering as a
card in the daily report digest. Nothing is computed or recommended — the
thresholds are whatever someone typed into the catalog.

Two pieces:

| File | Role |
|---|---|
| `inventory_email.py` | Pulls live stock for each stocked SKU, applies the notify policy, and returns one row per SKU. Also writes the full `.txt` audit when run by hand. |
| `catalog_scan.py` | Weekly job that walks the whole `/catalogitems` table (~1M rows) to find the stocked SKUs (`CAT_SAFTYSTK > 0`) and caches them. The report only reads that cache. |

`../modules/inventory.py` renders the rows as the email card; `../scheduler.py`
sends it.

## What "stocked" means

A SKU is stocked if its catalog record has `CAT_SAFTYSTK > 0`. That's the whole
definition. The report never looks at the `/lowstock` list except as a fallback
when the catalog cache is missing or stale (see Caching).

## Notify policy

Per SKU, using **available** = on hand − reserved (BMS's own per-roll
`AVAILABLE_FLOAT`, summed across the SKU's rolls):

| Flag | Condition | In the email |
|---|---|---|
| **critical** (`urgent`) | available < safety stock (`CAT_SAFTYSTK`) | red left accent, red Available, listed first |
| **reorder** (`order_now`) | available < reorder point (`CAT_REORDER`), **or** critical | listed |

The "or critical" keeps a below-safety SKU visible when its reorder point is
still unset (0) in BMS. A properly entered reorder point is always ≥ safety
stock, so once it's populated this reduces to plain "available < reorder".

Only SKUs with `order_now` appear in the email. The `.txt` audit lists all of
them.

## Columns

| Email | `.txt` | Source | Meaning |
|---|---|---|---|
| Item | `STYLE` / `COLOR` / `VENDOR` / `SEQUENCE` | catalog cache, live roll style/color | style · color, with vendor · sequence underneath |
| On Hand | `ON_HAND` | `/productstock` `ONHAND_FLOAT` summed over rolls | physical stock |
| Committed | `COMMITTED` | `/orderline` | **sold on open orders but not yet assigned to a roll and not on a PO** — demand nothing is covering yet. Per open-order line: `DMI_WQUANTITY − DMI_QTYASSIGNED`; lines with a `DMI_PONO` are skipped entirely. Shown red when it exceeds Available. |
| — | `RESERVED` | `/productstock` `RESERVED_FLOAT` | assigned to specific rolls |
| Available | `AVAIL` | `/productstock` `AVAILABLE_FLOAT` | on hand − reserved; what you can pull today |
| Reorder | `REORDER` | catalog `CAT_REORDER` | entered reorder point (often 0/unset) |
| Safety | `SAFETY` | catalog `CAT_SAFTYSTK` | entered safety stock |
| — | `NOTIFY` | | `CRIT` below safety, `YES` below reorder, blank otherwise |

Committed is display-only: the notify flags turn on Available. A positive
Committed is its own call to action — someone has to find stock for it or cut
a PO.

Sort order (both outputs): critical first, then deepest below the reorder
point, then by sequence.

## Data sources

| Step | Endpoint | Notes |
|---|---|---|
| Authentication | `POST /{alias}/token` | `x-api-key` header, form body, `granttype=application` |
| Stocked universe | `GET /{alias}/catalogitems` (weekly, via `catalog_scan.py`) | paged 1000 rows at a time, 6 pages concurrently; ~45–60 min; resumable |
| Fallback universe | `GET /{alias}/lowstock` | only when the cache is missing/partial/stale |
| Live stock | `GET /{alias}/productstock?catseq=…` | one call per stocked SKU, 16 in parallel |
| Open orders | `GET /{alias}/orders?startdate&enddate` | returns open orders only; `cfg.ORDER_HISTORY_FLOOR` → today |
| Open-order lines | `GET /{alias}/orderline?branch&startdate&enddate` | one call per branch that has open orders; filtered locally to stocked SKUs on open orders |

Quirks worth knowing:

- `/orderline` and `/lowstock` zero-pad `CAT_SEQUENCE` to 13 digits
  (`0000000684588`); `/productstock` and the catalog don't. The report
  normalises before comparing.
- Order lines with `DMI_STATUS = J` are the ones assigned to a roll; lines
  with a blank status are unassigned. Service/labor lines (status `S`/`L`/`I`)
  carry a `DMI_QTYASSIGNED` that is not a quantity — they're never stocked
  SKUs, so the report never sees them.
- `/orders` and `/orderline` need `startdate`/`enddate` even for open orders.

## Caching

**Stocked catalog** — `cfg.STOCKED_CATALOG_CACHE`
(`.stocked_catalog_cache.json`, gitignored), written by `catalog_scan.py`:

```json
{ "scanned_at": "<iso8601 or null>", "next_page": 123, "complete": true,
  "items": { "<CAT_SEQUENCE>": { "safety", "reorder", "vendor", "prodcode",
                                 "box", "style", "stynum", "color", "desc",
                                 "roll_sy" } } }
```

The report trusts it only when `complete` is true **and** `scanned_at` is
within `cfg.STOCKED_CATALOG_MAX_AGE_DAYS` (7). Otherwise it logs a warning and
falls back to `/lowstock`, which is narrower (only SKUs already below safety)
and has no reorder point or vendor. A partial scan (`complete: false`) is a
checkpoint: rerunning the scan resumes from `next_page`.

Nothing else is cached; every report run pulls live stock and open orders.

## Configuration — `inventory_email_config.py`

| Key | Purpose |
|---|---|
| `BMS_ALIAS`, `BMS_COMPANY` | tenant (`creativecarpets`) and company (`99`) |
| `ORDER_HISTORY_FLOOR` | earliest order date pulled when summing committed qty (`20240101`); an open order older than this would be missed |
| `STOCKED_CATALOG_CACHE`, `STOCKED_CATALOG_MAX_AGE_DAYS` | cache path and freshness limit |
| `CATALOG_SCAN_BATCH` | concurrent pages during the catalog scan (6; 20 timed out) |
| `OUTPUT_PATH` | where a manual run writes the `.txt` audit |

Credentials come from `.env`: `BMS_API_KEY`, `BMS_USERNAME`, `BMS_PASSWORD`.

## Running

Manual audit (writes `safety_stock_items.txt`):

```bash
cd CreativeBot
venv/bin/python -m email_reports.inventory.inventory_email
```

Rebuild the stocked-SKU cache (weekly cron on the Pi; ~45–60 min):

```bash
venv/bin/python -m email_reports.inventory.catalog_scan          # resume or start
venv/bin/python -m email_reports.inventory.catalog_scan --fresh  # ignore checkpoint
```

Exit status is 0 only for a complete scan.

Email delivery is not run from here. `email_reports/scheduler.py` runs from
cron every 15 minutes on the Pi, self-gates to one dispatch per day at the time
set on `/reports/settings`, builds each subscriber's digest (this card plus any
others they're subscribed to) and sends it through Microsoft Graph. The
`/reports` dashboard's **Run Now** sends just this card to its subscribers
immediately. Subscribers come from the M365 group synced by `m365_directory.py`.

## Known gaps

- **Available ignores committed.** A SKU can look fine on Available while
  thousands are sold and unassigned. Committed is shown for exactly that
  reason, but it doesn't flag the row.
- **Nothing is recommended.** Wrong or unset thresholds in BMS produce wrong
  or missing alerts; the report only reflects what's entered.
- **Open-order window.** Committed only sees orders dated on or after
  `ORDER_HISTORY_FLOOR`.
- **Catalog cache staleness.** A SKU stocked since the last weekly scan isn't
  evaluated until the next one.
- **No on-order column.** Open PO quantity per SKU isn't shown; a line on a PO
  is simply excluded from Committed.
