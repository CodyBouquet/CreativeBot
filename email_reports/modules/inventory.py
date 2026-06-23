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

    The pull evaluates every stocked SKU. Normally the email is action-focused and
    shows only SKUs at or below their reorder point (ORDER NOW). When the temporary
    INVENTORY_EMAIL_SHOW_ALL flag is set it instead lists EVERY stocked SKU — a
    review view for setting safety/reorder fields catalog-wide (current → suggested).
    """
    rows = ctx.get_or_compute("inventory.lowstock_rows", _pull_rows)

    show_all = False
    try:
        from ..inventory import inventory_email_config as cfg
        show_all = bool(getattr(cfg, "INVENTORY_EMAIL_SHOW_ALL", False))
    except Exception:
        log.exception("could not read INVENTORY_EMAIL_SHOW_ALL")

    display = rows if show_all else [r for r in rows if r.get("order_now")]
    if not display:
        return None

    if show_all:
        below = sum(1 for r in display if r.get("order_now"))
        title = "Inventory — Stock Review (all stocked SKUs)"
        badge = f"{below} below / {len(display)} stocked"
    else:
        title = "Inventory — Low Stock"
        badge = f"{len(display)} to order"
    return report_card(title, _render_html(display, show_all=show_all), icon="📦", badge=badge)


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


def _render_html(rows: list[dict], show_all: bool = False) -> str:
    """
    Table for the card body. Normally one row per SKU to order; when show_all is
    set it's every stocked SKU — a review view for setting safety/reorder fields.

    Six columns — Item (style/color with a vendor·sequence subline), On Hand,
    Inv Pos, Safety (current→suggested), Reorder (current→suggested), and Order Qty.
    The threshold columns show the days-of-supply suggestion against the current
    value (current reorder points are mostly unset in BMS, so the 0→value gap is
    visible). Order Qty is shown only for general SKUs that are actually below their
    reorder point; manual-safety SKUs and not-yet-due rows show "—". Rows alternate
    a faint zebra fill; urgent rows (general: inv_pos below suggested safety; manual:
    on-hand below the entered safety) get a red left-accent and red, bold order qty.
    """
    th  = ("text-align:left; padding:7px 9px; border-bottom:2px solid #e4e4e4; "
           "font-size:11px; letter-spacing:0.5px; color:#999; text-transform:uppercase;")
    thr = th + " text-align:right;"

    caption = (
        'Every stocked SKU — review and set safety / reorder. Below-reorder items '
        'first. Thresholds shown current → <strong>suggested</strong>.'
        if show_all else
        'At or below reorder point — order now. Thresholds shown current → '
        '<strong>suggested</strong>.'
    )
    head = (
        f'<p style="font-size:12px; color:#888; margin:2px 0 10px;">{caption}</p>'
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

        # Safety & reorder show current → suggested for every SKU (the suggestion is
        # guidance for manual-safety items too). Order Qty is meaningful only for a
        # general SKU that's actually below its reorder point — otherwise "—".
        safety_cell  = _cur_rec(r.get("safety_cur", 0), r.get("rec_safety", 0))
        reorder_cell = _cur_rec(r.get("reorder_cur", 0), r.get("rec_rop", 0))
        qty_cell     = f'{r.get("rec_qty", 0):.0f}' if (r.get("order_now") and not manual) else "—"

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
