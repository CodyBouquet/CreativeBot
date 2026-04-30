"""
Per-salesperson sales report.

Section is sliced by the recipient's RM Sales ID — users without one are
skipped (build_section returns None) so they don't get an empty section.
"""
from .base import ReportContext


def build_section(user, ctx: ReportContext) -> str | None:
    if not user["rm_sales_id"]:
        return None  # nothing personal to show; skip silently
    # Stub until the Rollmaster sales pull is wired in a later phase.
    return None
