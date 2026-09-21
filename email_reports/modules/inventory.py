"""
Inventory low-stock reorder report.

Section content is identical for every recipient (it's a global view, not
sliced per user). The expensive Rollmaster pull happens once per send window
via ReportContext, then every subscriber reuses the cached rows.
"""
from __future__ import annotations

import logging

from .base import ReportContext, report_card

log = logging.getLogger(__name__)


def build_section(user, ctx: ReportContext) -> str | None:
    """Return the reorder HTML table (same for every recipient), reusing the cached row pull; None when there's nothing to show.

    The pull evaluates every stocked SKU; the email lists only the ones that need
    attention — available balance below the safety stock entered in BMS.
    """
    rows = ctx.get_or_compute("inventory.lowstock_rows", _pull_rows)

    display = [r for r in rows if r.get("order_now")]
    if not display:
        return None

    title = "Inventory — Low Stock"
    badge = f"{len(display)} below safety stock"
    return report_card(title, _render_html(display), icon="📦", badge=badge)


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
    # Match the .txt file's sort order: deepest below safety first, then by sequence.
    rows.sort(key=lambda r: (
        not r.get("order_now"),
        r.get("available", 0) - r.get("safety_cur", 0),
        r.get("seq", ""),
    ))
    return rows


def _render_html(rows: list[dict]) -> str:
    """
    Table for the card body — one row per SKU that needs reordering.

    Five columns — Item (style/color with a vendor·sequence subline), On Hand,
    Committed, Available, and Safety. Committed is quantity sold on open orders
    that is neither assigned to a roll nor on a purchase order — demand nothing is
    covering yet. Any positive committed is worth a look; where it exceeds what is
    available it is shown in red, since the stock on hand can't satisfy it.

    A SKU is listed when its available balance falls below the safety stock entered
    in BMS; the deepest shortfalls come first. Available is shown in red on every
    row, since every listed row is a shortfall.
    """
    th  = ("text-align:left; padding:7px 9px; border-bottom:2px solid #e4e4e4; "
           "font-size:11px; letter-spacing:0.5px; color:#999; text-transform:uppercase;")
    thr = th + " text-align:right;"

    caption = (
        'Available balance below the safety stock entered in BMS — order now. '
        'Committed is open-order quantity sold that is not yet assigned to a roll '
        'and not on a PO; where it exceeds available (red) stock on hand cannot '
        'cover it. Deepest shortfalls are listed first.'
    )
    head = (
        f'<p style="font-size:12px; color:#888; margin:2px 0 10px;">{caption}</p>'
        '<table style="border-collapse:collapse; width:100%; font-size:13px;">'
        '<thead><tr>'
        f'<th style="{th}">Item</th>'
        f'<th style="{thr}">On Hand</th>'
        f'<th style="{thr}">Committed</th>'
        f'<th style="{thr}">Available</th>'
        f'<th style="{thr}">Safety</th>'
        '</tr></thead><tbody>'
    )

    rows_html = []
    for i, r in enumerate(rows):
        style  = (r.get("style") or "").strip() or "—"
        color  = (r.get("color") or "").strip()
        vendor = (r.get("vendor") or "").strip() or "—"
        seq    = r.get("seq", "")

        zebra     = "#ffffff" if i % 2 == 0 else "#fafafa"
        accent    = "#d6452c"
        avail_col = "#d6452c"
        # Uncovered demand the available stock can't satisfy. Worth calling out on
        # its own — it can be true of a SKU that isn't otherwise critical.
        committed = r.get("committed", 0)
        comm_col  = "#d6452c" if committed > r.get("available", 0) else "#777"

        name = style if not color else f'{style} <span style="color:#999;">· {color}</span>'
        sub  = f'{vendor} · {seq}'

        td  = f"padding:8px 9px; border-bottom:1px solid #f0f0f0; background:{zebra};"
        tdr = td + " text-align:right; font-variant-numeric:tabular-nums;"
        rows_html.append(
            '<tr>'
            f'<td style="{td} border-left:3px solid {accent};">{name}'
            f'<div style="color:#aaa; font-size:11px; font-family:monospace; '
            f'margin-top:2px;">{sub}</div></td>'
            f'<td style="{tdr}">{r.get("on_hand", 0):.0f}</td>'
            f'<td style="{tdr} color:{comm_col};">{committed:.0f}</td>'
            f'<td style="{tdr} color:{avail_col}; font-weight:bold;">{r.get("available", 0):.0f}</td>'
            f'<td style="{tdr}">{r.get("safety_cur", 0):.0f}</td>'
            '</tr>'
        )
    return head + "".join(rows_html) + "</tbody></table>"
