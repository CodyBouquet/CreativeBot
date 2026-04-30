"""
Inventory low-stock reorder report.

Section content is identical for every recipient (it's a global view, not
sliced per user). The expensive Rollmaster pull happens once per send window
via ReportContext, then every subscriber reuses the cached rows.
"""
from __future__ import annotations

import logging

from .base import ReportContext

log = logging.getLogger(__name__)


def build_section(user, ctx: ReportContext) -> str | None:
    rows = ctx.get_or_compute("inventory.lowstock_rows", _pull_rows)
    if not rows:
        return None
    return _render_html(rows)


def _pull_rows() -> list[dict]:
    """
    Lazy-imported so this module loads cheaply during tests / dashboard
    rendering. The actual Rollmaster pull only happens when build_section
    is called (i.e., during a scheduler dispatch or manual run).
    """
    try:
        from ..inventory import inventory_email
    except Exception:
        log.exception("could not import inventory_email")
        return []
    try:
        rows = inventory_email.build_report()
    except Exception:
        log.exception("inventory_email.build_report() failed")
        return []
    # Match the .txt file's sort order: largest current safety stock first.
    rows.sort(key=lambda r: (-r.get("safety_cur", 0), r.get("seq", "")))
    return rows


def _render_html(rows: list[dict]) -> str:
    """Compact 7-column table — readable on phones and laptops."""
    th_style  = "text-align:left; padding:6px 10px; border-bottom:1px solid #c8c8c8; font-size:11px; letter-spacing:1px; color:#555;"
    thr_style = "text-align:right; padding:6px 10px; border-bottom:1px solid #c8c8c8; font-size:11px; letter-spacing:1px; color:#555;"
    td_style  = "padding:5px 10px; border-bottom:1px solid #eee;"
    tdr_style = "padding:5px 10px; border-bottom:1px solid #eee; text-align:right; font-variant-numeric: tabular-nums;"
    seq_style = "padding:5px 10px; border-bottom:1px solid #eee; font-family: monospace; font-size:11px; color:#555;"

    head = (
        f'<p style="font-size:13px; color:#555; margin: 4px 0 12px;">'
        f'{len(rows)} item(s) below safety stock.</p>'
        f'<table style="border-collapse:collapse; width:100%; font-size:13px;">'
        f'<thead><tr>'
        f'<th style="{th_style}">Style / Color</th>'
        f'<th style="{th_style}">Vendor</th>'
        f'<th style="{thr_style}">LT</th>'
        f'<th style="{thr_style}">On Hand</th>'
        f'<th style="{thr_style}">Avail</th>'
        f'<th style="{thr_style}">Safety (Cur → Rec)</th>'
        f'<th style="{thr_style}">Reorder Qty</th>'
        f'</tr></thead><tbody>'
    )

    rows_html = []
    for r in rows:
        style = (r.get("style") or "").strip() or "—"
        color = (r.get("color") or "").strip()
        style_color = f"{style}<br><span style='color:#888;font-size:11px;'>{color}</span>" if color else style
        seq = r.get("seq", "")
        rows_html.append(
            f'<tr>'
            f'<td style="{td_style}">{style_color}'
            f'<div style="{seq_style}">{seq}</div></td>'
            f'<td style="{td_style}">{r.get("vendor", "") or "—"}</td>'
            f'<td style="{tdr_style}">{int(r.get("lead_time", 0))}</td>'
            f'<td style="{tdr_style}">{r.get("on_hand", 0):.0f}</td>'
            f'<td style="{tdr_style}">{r.get("avail", 0):.0f}</td>'
            f'<td style="{tdr_style}">{r.get("safety_cur", 0):.0f} → '
            f'<strong>{r.get("rec_safety", 0):.0f}</strong></td>'
            f'<td style="{tdr_style}"><strong>{r.get("rec_qty", 0):.0f}</strong></td>'
            f'</tr>'
        )
    return head + "".join(rows_html) + "</tbody></table>"
