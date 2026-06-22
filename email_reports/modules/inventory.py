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
    """Return the reorder HTML table (same for every recipient), reusing the cached row pull; None when nothing needs ordering.

    The pull evaluates every stocked SKU, but the email is action-focused: it
    shows only SKUs at or below their reorder point (ORDER NOW). The full
    stocked-universe audit lives in the .txt report, not the email.
    """
    rows = ctx.get_or_compute("inventory.lowstock_rows", _pull_rows)
    order_rows = [r for r in rows if r.get("order_now")]
    if not order_rows:
        return None
    return report_card(
        "Inventory — Low Stock",
        _render_html(order_rows),
        icon="📦",
        badge=f"{len(order_rows)} to order",
    )


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
    # Match the .txt file's sort order: ORDER NOW first, then deepest below the
    # reorder point, then by sequence.
    rows.sort(key=lambda r: (
        not r.get("order_now"),
        r.get("inv_pos", 0) - r.get("rec_rop", 0),
        r.get("seq", ""),
    ))
    return rows


def _render_html(rows: list[dict]) -> str:
    """
    Action-focused table for the card body: one row per SKU to order.

    Six columns — Item (style/color with a vendor·sequence subline), On Hand,
    Inv Pos, Safety (current→recommended), Reorder (current→recommended), and
    Order Qty. The threshold columns convey how urgent the reorder is and, since
    current reorder points are mostly unset in BMS, make the 0→value gap visible.
    Manual-safety SKUs (hand-managed cushion/trim) carry no computed reorder or
    order qty, so those two cells show "—"; their alert is purely "below your
    entered safety stock." Rows alternate a faint zebra fill; urgent rows (general:
    inv_pos below recommended safety; manual: on-hand below the entered safety)
    get a red left-accent and red, bold order qty so they stand out.
    """
    th  = ("text-align:left; padding:7px 9px; border-bottom:2px solid #e4e4e4; "
           "font-size:11px; letter-spacing:0.5px; color:#999; text-transform:uppercase;")
    thr = th + " text-align:right;"

    head = (
        '<p style="font-size:12px; color:#888; margin:2px 0 10px;">'
        'At or below reorder point — order now. Thresholds shown current → '
        '<strong>recommended</strong>.</p>'
        '<table style="border-collapse:collapse; width:100%; font-size:13px;">'
        '<thead><tr>'
        f'<th style="{th}">Item</th>'
        f'<th style="{thr}">On Hand</th>'
        f'<th style="{thr}">Inv Pos</th>'
        f'<th style="{thr}">Safety</th>'
        f'<th style="{thr}">Reorder</th>'
        f'<th style="{thr}">Order Qty</th>'
        '</tr></thead><tbody>'
    )

    # cur → rec cell: current value muted, recommended bold (so the target reads
    # first). When the two match (e.g. manual-safety items, where the threshold is
    # just the entered number) collapse to a single value rather than "900 → 900".
    def _cur_rec(cur, rec):
        """Render a 'current → recommended' threshold cell body."""
        if abs(cur - rec) < 0.5:
            return f'<strong>{rec:.0f}</strong>'
        return (f'<span style="color:#aaa;">{cur:.0f}</span> '
                f'<span style="color:#ccc;">→</span> '
                f'<strong>{rec:.0f}</strong>')

    rows_html = []
    for i, r in enumerate(rows):
        style  = (r.get("style") or "").strip() or "—"
        color  = (r.get("color") or "").strip()
        vendor = (r.get("vendor") or "").strip() or "—"
        seq    = r.get("seq", "")
        manual = r.get("manual_safety")

        urgent    = r.get("urgent", False)
        zebra     = "#ffffff" if i % 2 == 0 else "#fafafa"
        accent    = "#d6452c" if urgent else "transparent"
        qty_color = "#d6452c" if urgent else "#222"

        name = style if not color else f'{style} <span style="color:#999;">· {color}</span>'
        sub  = f'{vendor} · {seq}'

        # Manual-safety SKUs have no computed reorder or order qty — show "—".
        safety_cell  = _cur_rec(r.get("safety_cur", 0), r.get("rec_safety", 0))
        reorder_cell = "—" if manual else _cur_rec(r.get("reorder_cur", 0), r.get("rec_rop", 0))
        qty_cell     = "—" if manual else f'{r.get("rec_qty", 0):.0f}'

        td  = f"padding:8px 9px; border-bottom:1px solid #f0f0f0; background:{zebra};"
        tdr = td + " text-align:right; font-variant-numeric:tabular-nums;"
        rows_html.append(
            '<tr>'
            f'<td style="{td} border-left:3px solid {accent};">{name}'
            f'<div style="color:#aaa; font-size:11px; font-family:monospace; '
            f'margin-top:2px;">{sub}</div></td>'
            f'<td style="{tdr}">{r.get("on_hand", 0):.0f}</td>'
            f'<td style="{tdr}">{r.get("inv_pos", 0):.0f}</td>'
            f'<td style="{tdr}">{safety_cell}</td>'
            f'<td style="{tdr}">{reorder_cell}</td>'
            f'<td style="{tdr} color:{qty_color}; font-weight:bold;">{qty_cell}</td>'
            '</tr>'
        )
    return head + "".join(rows_html) + "</tbody></table>"
