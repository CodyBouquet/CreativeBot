"""
Full /catalogitems scanner — builds the "stocked universe" cache.

WHY THIS EXISTS
    The reorder report must evaluate EVERY stocked SKU (CAT_SAFTYSTK > 0), not
    just the items currently below safety stock. The BMS /catalogitems endpoint
    has no per-item filter and is ~1M rows sorted by CAT_SEQUENCE, and stocked
    items are scattered through the high-sequence rows — so the only way to find
    them all is to page through the whole catalog. That takes ~45-60 min and the
    API throttles under sustained pagination, so this runs as a SEPARATE weekly
    job (cron) decoupled from the email run. The report just reads the cache.

OUTPUT
    cfg.STOCKED_CATALOG_CACHE — a JSON file shaped like:
        {
          "scanned_at": "<iso8601>",   # when the last *complete* scan finished
          "next_page":  <int>,         # resume cursor (next page to fetch)
          "complete":   true|false,    # false => partial scan, do NOT trust as full
          "items": { "<CAT_SEQUENCE>": {safety, reorder, vendor, box, style,
                                        stynum, color, desc}, ... }
        }

USAGE
    python -m email_reports.inventory.catalog_scan          # resume-or-start
    python -m email_reports.inventory.catalog_scan --fresh  # ignore checkpoint
"""
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from tqdm import tqdm

# Reuse the report's auth + request plumbing so credentials/session logic lives
# in exactly one place. Works both as a module (-m) and run directly.
try:
    from . import inventory_email as ie
    from . import inventory_email_config as cfg
except ImportError:
    import inventory_email as ie
    import inventory_email_config as cfg


# --- Tuning knobs ----------------------------------------------------------
# Pulled from config where they belong; the rest are local scan internals.
PAGE_LIMIT = 1000          # rows per /catalogitems page (max the API honours)
MAX_RETRIES = 4            # per-page attempts before a page is deemed "failed"
BACKOFF_BASE = 2.0         # seconds; retry sleeps 2, 4, 8 ... (BACKOFF_BASE**n)


def _fetch_page(session, page):
    """
    Fetch one /catalogitems page with retry + exponential backoff.

    Returns the list of row dicts on success, or None if every attempt failed
    (so the caller can record the page for a later repair pass — a failed page
    must never be silently treated as "end of catalog", since that would drop
    every stocked SKU beyond it).
    """
    for attempt in range(MAX_RETRIES):
        try:
            r = ie._get(
                session,
                "catalogitems",
                {
                    "company": ie.COMPANY,
                    "startdate": "19000101",   # widest window => whole catalog
                    "enddate": "21000101",
                    "page": str(page),
                    "pagelimit": str(PAGE_LIMIT),
                },
                timeout=300,
            )
            if r.status_code == 200:
                return r.json()
            # Non-200 (throttle / transient 5xx): fall through to backoff.
        except Exception:
            # Network/timeout: fall through to backoff and try again.
            pass
        # Exponential backoff before the next attempt (skip after the last try).
        if attempt < MAX_RETRIES - 1:
            time.sleep(BACKOFF_BASE ** (attempt + 1))
    return None


def _extract_stocked(rows, items):
    """
    Pull the stocked SKUs (CAT_SAFTYSTK > 0) out of a page's rows into `items`.

    Mutates `items` in place. Each kept SKU records its current thresholds plus
    the descriptive fields the report needs to render a human-readable line.
    """
    for it in rows:
        # CAT_SAFTYSTK > 0 is the definition of "Creative Carpet stocks this".
        if ie._f(it.get("CAT_SAFTYSTK")) <= 0:
            continue
        seq = str(it.get("CAT_SEQUENCE", "")).strip()
        if not seq:
            continue
        items[seq] = {
            "safety":  ie._f(it.get("CAT_SAFTYSTK")),    # current safety threshold
            "reorder": ie._f(it.get("CAT_REORDER")),     # current reorder point
            "vendor":  str(it.get("CAT_VENDORID", "")).strip(),
            "box":     ie._f(it.get("CAT_UNIT_PER_BOX")) or 1.0,
            "style":   str(it.get("CAT_STYLE", "")).strip(),
            "stynum":  str(it.get("CAT_STYNUM", "")).strip(),   # vendor style number
            "color":   str(it.get("CAT_COLOR", "")).strip(),
            "desc":    str(it.get("CAT_ITEMDESC", "")).strip(), # full item description
        }


def _load_checkpoint(fresh):
    """
    Load a prior partial scan so we can resume from where it died.

    Returns (items_dict, start_page). With --fresh, or when no usable/complete
    checkpoint exists, starts clean from page 1. A checkpoint already marked
    complete is ignored here — a manual rerun means "rescan", so we start over.
    """
    if fresh or not os.path.exists(cfg.STOCKED_CATALOG_CACHE):
        return {}, 1
    try:
        with open(cfg.STOCKED_CATALOG_CACHE) as f:
            data = json.load(f)
    except Exception:
        return {}, 1
    # Resume only from an *incomplete* checkpoint; a complete file => full rescan.
    if data.get("complete"):
        return {}, 1
    return data.get("items", {}), int(data.get("next_page", 1))


def _write_cache(items, next_page, complete):
    """
    Persist the cache atomically (write to a temp file, then rename).

    Called after every batch as a checkpoint AND at the end. The temp+rename
    keeps the report from ever reading a half-written file. `scanned_at` is only
    stamped on a complete scan so freshness checks never trust a partial run.
    """
    payload = {
        "scanned_at": datetime.now(timezone.utc).isoformat() if complete else None,
        "next_page": next_page,
        "complete": complete,
        "items": items,
    }
    tmp = cfg.STOCKED_CATALOG_CACHE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(payload, f)
    os.replace(tmp, cfg.STOCKED_CATALOG_CACHE)


def scan_stocked_catalog(session, fresh=False):
    """
    Walk all of /catalogitems and cache every stocked SKU (CAT_SAFTYSTK > 0).

    Returns the {seq: {...}} items dict. Resilience features:
      - no early-stop: reads to the true end (a short/empty page),
      - low concurrency batches to stay under the throttle,
      - per-page retry/backoff,
      - failed pages collected and retried in a repair pass; anything still
        failing leaves complete=False and is logged (never silently dropped),
      - per-batch checkpointing so a crash resumes instead of restarting.
    """
    items, page = _load_checkpoint(fresh)
    if page > 1:
        print(f"resuming catalog scan from page {page} "
              f"({len(items)} stocked SKUs already cached)", file=sys.stderr)

    batch = cfg.CATALOG_SCAN_BATCH
    failed_pages = []   # pages that exhausted retries — repaired after the walk
    reached_end = False

    bar = tqdm(desc="catalog scan", unit="pg", file=sys.stderr, dynamic_ncols=True)
    try:
        # --- Main walk: fetch `batch` pages at a time until a short page (= end).
        while not reached_end:
            pages = range(page, page + batch)
            with ThreadPoolExecutor(max_workers=batch) as ex:
                results = list(ex.map(lambda p: (p, _fetch_page(session, p)), pages))

            # Process this batch's pages in page order.
            for p, rows in results:
                if rows is None:
                    # Total failure for this page: remember it, but keep going —
                    # we cannot conclude "end of catalog" from a failure.
                    failed_pages.append(p)
                    continue
                _extract_stocked(rows, items)
                # A page shorter than the limit means we've hit the catalog's end.
                if len(rows) < PAGE_LIMIT:
                    reached_end = True

            page += batch
            bar.update(batch)
            # Checkpoint after every batch so a crash resumes from `page`.
            _write_cache(items, page, complete=False)

        # --- Repair pass: one more try at any pages that failed during the walk.
        if failed_pages:
            print(f"retrying {len(failed_pages)} failed page(s)...", file=sys.stderr)
            still_failed = []
            for p in failed_pages:
                rows = _fetch_page(session, p)
                if rows is None:
                    still_failed.append(p)
                else:
                    _extract_stocked(rows, items)
            failed_pages = still_failed
    finally:
        bar.close()

    # --- Finalize: complete only if NOTHING was left unread. Loudly flag gaps so
    # a partial scan is never mistaken for a full one ("no silent caps").
    complete = not failed_pages
    if failed_pages:
        print(f"WARNING: scan INCOMPLETE — {len(failed_pages)} page(s) never "
              f"fetched: {failed_pages[:20]}{'...' if len(failed_pages) > 20 else ''}",
              file=sys.stderr)
    _write_cache(items, page, complete=complete)
    print(f"catalog scan {'complete' if complete else 'PARTIAL'}: "
          f"{len(items)} stocked SKUs cached", file=sys.stderr)
    return items


def main():
    """CLI entry point: authenticate, scan, exit non-zero on a partial scan."""
    fresh = "--fresh" in sys.argv
    session = ie.make_session(ie.authenticate())
    items = scan_stocked_catalog(session, fresh=fresh)
    # Re-read the cache flag so the exit code reflects what actually got written.
    with open(cfg.STOCKED_CATALOG_CACHE) as f:
        complete = json.load(f).get("complete", False)
    sys.exit(0 if complete else 1)


if __name__ == "__main__":
    main()
