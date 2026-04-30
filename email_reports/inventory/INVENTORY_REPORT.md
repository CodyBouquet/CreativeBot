# Inventory Report (`inventory_email.py`)

A daily-runnable script that pulls demand + supply data from the Rollmaster
(Broadlume BMS) API and writes a low-stock reorder report covering every
stocking item currently below its safety-stock threshold. Output is plain text
at the path defined by `cfg.OUTPUT_PATH` (default `safety_stock_items.txt`).

## Run

```bash
./venv/bin/python inventory_email.py
```

First run takes ~6–8 minutes (most of it the `/invoicelines` walk and the
catalog scan for box qty). Subsequent runs reuse the box-qty cache and the
catalog scan is skipped.

Credentials live in `.env`:
```
BMS_API_KEY=...
BMS_USERNAME=...
BMS_PASSWORD=...
```

Knobs live in `inventory_email_config.py`. Edit those, not the script.

## Output columns

| Column     | Meaning                                                                                |
|------------|----------------------------------------------------------------------------------------|
| `SEQUENCE` | `CAT_SEQUENCE` — the Rollmaster item id                                                |
| `VENDOR`   | `VENDOR` from `/purchaseorderlines` (blank if no PO history)                           |
| `LT`       | Lead time in days, from `cfg.VENDOR_LEAD_TIMES[vendor]` or `DEFAULT_LEAD_TIME_DAYS`    |
| `ON_HAND`  | Sum of current rolls (`/productstock` `ONHAND_FLOAT`)                                  |
| `AVAIL`    | Sum of `AVAILABLE_FLOAT` — what you can pull today (already nets out reserved rolls)   |
| `UNASN`    | Open sales-order qty not yet allocated to a roll (`DMI_WQUANTITY − DMI_QTYASSIGNED`)   |
| `ON_PO`    | Open PO qty not yet received (`QTYORD − QTYREC`, `STATUS='O'`)                         |
| `SOLD_1YR` | `IVL_SQUAN` summed across invoices dated within `DEMAND_WINDOW_DAYS`                   |
| `PEAK_WK`  | Max single-week `DMI_SQUANTITY` bucketed by `DMH_DATE` ISO week                        |
| `SIGMA_WK` | Std-dev of weekly invoiced qty across the demand window (zeros included)               |
| `BOX`      | `CAT_UNIT_PER_BOX` (1 if not sold by box)                                              |
| `SAF_CUR`  | Currently configured safety stock (`CAT_SAFETY_STOCK` from `/lowstock`)                |
| `SAF_REC`  | **Recommended** safety = `Z × σ × √(LT/7)`, ceiled to box                              |
| `ROP_REC`  | **Recommended** reorder point = `daily_demand × LT + SAF_REC`, ceiled to box           |
| `QTY_REC`  | **Recommended** order qty = `daily_demand × REORDER_PERIOD_DAYS`, ceiled to box        |
| `STYLE`    | Human-readable style                                                                   |
| `COLOR`    | Human-readable color                                                                   |

How to read a row: `SAF_CUR` vs `SAF_REC` divergence flags miscalibrated safety
stocks. `AVAIL + ON_PO < ROP_REC` means you should be reordering — `QTY_REC` is
the suggested PO size.

## Data sources

| Step                             | Endpoint                                  | Notes                                                          |
|----------------------------------|-------------------------------------------|----------------------------------------------------------------|
| Authentication                   | `POST /{alias}/token`                     | `x-api-key` header, `application/x-www-form-urlencoded` body   |
| Items below safety stock         | `GET /{alias}/lowstock`                   | Single call, returns 53ish items                               |
| Open sales orders                | `GET /{alias}/orders`                     | `startdate`/`enddate` required even when filtering open        |
| Open PO supply                   | `GET /{alias}/purchaseorderlines`         | Used for `ON_PO` and `VENDOR` mapping                          |
| Per-roll inventory               | `GET /{alias}/productstock?catseq=…`      | One call per target seq, parallelized (16 workers)             |
| Bulk order lines                 | `GET /{alias}/orderline?branch=&dates=…`  | One call per branch, used for `UNASN` and `PEAK_WK`            |
| Past-year invoice headers        | `GET /{alias}/invoice?branch=&dates=…`    | Paginated, gives `IVC_INVNO → IVC_DATE` map                    |
| Past-year invoice lines          | `GET /{alias}/invoicelines`               | Paginated whole table, filtered locally by invno + catseq      |
| Box quantity (cached weekly)     | `GET /{alias}/catalogitems`               | Paginated; cache lives at `.box_qty_cache.json`                |

## Formulas

```
daily_demand   = SOLD_1YR / 365
weekly_sigma   = stdev([weekly_invoiced_qty for each ISO week in window])
SAF_REC        = ceil(SERVICE_LEVEL_Z × weekly_sigma × √(LT_days / 7), BOX)
ROP_REC        = ceil(daily_demand × LT_days + SAF_REC,                BOX)
QTY_REC        = ceil(daily_demand × REORDER_PERIOD_DAYS,              BOX)
```

`ceil(value, BOX) = math.ceil(value / BOX) × BOX`. `BOX = 1` for items not sold
by box, which collapses to a plain integer ceiling.

`SERVICE_LEVEL_Z` corresponds to fill rate:

| Z    | Service level | Stockout frequency        |
|------|---------------|---------------------------|
| 1.28 | 90 %          | ~5 weeks/year per item    |
| 1.65 | 95 %          | ~2.5 weeks/year per item  |
| 1.88 | 97 %          | ~1.5 weeks/year per item  |
| 2.33 | 99 %          | ~3.5 days/year per item   |

## Configuration

Everything in `inventory_email_config.py`:

- `BMS_ALIAS`, `BMS_COMPANY` — connection scope (tenant + company id).
- `SERVICE_LEVEL_Z` — global, applies to every item.
- `VENDOR_LEAD_TIMES` — dict keyed by `VENDOR` field (e.g. `"ALLSURF": 7`).
- `DEFAULT_LEAD_TIME_DAYS` — fallback when an item's vendor isn't in the dict
  or its catalog record has no vendor.
- `DEMAND_WINDOW_DAYS` — how far back demand history is measured.
- `ORDER_HISTORY_FLOOR` — earliest date we pull order/orderline history from.
- `REORDER_PERIOD_DAYS` — drives `QTY_REC`. A higher value = bigger, less
  frequent POs; lower = smaller, more frequent.
- `BOX_QTY_CACHE`, `BOX_QTY_CACHE_MAX_AGE_DAYS` — disk cache for box quantities;
  delete the file to force a fresh catalog scan.

## Caching

- **Box quantities** — refreshed weekly. The catalog scan that populates this
  takes ~2 min, so the cache makes daily runs ~2 min faster.
- **Invoice lines** — *not yet cached*. The walk runs in full every time
  (~6 min). Adding an `IVC_INVNO`-keyed cache would cut subsequent runs to
  about 60 s; see "Gaps" below.

## Known gaps & future work

Listed roughly by impact:

- **No `ORDER_NOW` trigger column.** The report shows recommendations but
  doesn't flag which items are currently below their reorder point.
- **Demand trend ignored.** Flat 12-month average understates a growing
  product and overstates a declining one. Add a recent-90-days × 4 vs
  trailing-12 ratio column.
- **Lead-time variability (σ_LT) not modeled.** Real safety formula is
  `Z × √(LT × σ_demand² + demand² × σ_LT²)`. Needs PO date-vs-receipt
  history; current vendor lead times are mostly user-supplied estimates.
- **Pent-up `UNASN` demand uncounted.** Currently informational only;
  doesn't feed the math. Items chronically out can have understated demand.
- **Stockout-corrected demand.** Days you were out of stock undercount in
  `SOLD_1YR`. Would need per-day stock history to correct.
- **Vendor mapping is fragile.** Items without recent POs have blank
  `VENDOR`. Fix: pull `CAT_VENDORID` from `/catalogitems` during the box-qty
  scan and merge it in.
- **No vendor MOQ.** `QTY_REC` may be below a vendor's minimum order
  quantity. Add `VENDOR_MIN_ORDER_QTY` to config alongside lead time.
- **No unit cost.** `CAT_NETCOST` would enable working-capital sorting and
  dollar-weighted recommendations.
- **Service level is global.** Z is the same for every item; high-value or
  customer-promise items might warrant 99 %, slow C-class items 90 %.
- **No write-back.** The script doesn't push `SAF_REC` to `CAT_SAFTYSTK` in
  Rollmaster — recommendations are advisory only.
- **Invoice-lines walk is the main cost.** Caching by `IVC_INVNO` and only
  fetching new invoices per run would drop daily-run time from ~6 min to
  under 60 s.

## Scheduling

Not yet wired. When ready:

```cron
0 6 * * * /home/vm/Dev/CreativeCarpet/CreativeBot/venv/bin/python /home/vm/Dev/CreativeCarpet/CreativeBot/inventory_email.py
```

Current script writes to a file. Email delivery is Phase 2 of
`EMAIL_REPORTS_PLAN.txt` and is blocked on Azure app registration.
