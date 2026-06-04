"""
Late Net30 report — Net30 invoices that have aged past their due date.
"""
from .base import ReportContext


def build_section(user, ctx: ReportContext) -> str | None:
    """Return the late-Net30 aging-invoices HTML section. Stub: returns None until the Rollmaster aging-invoices pull is wired up."""
    # Stub until the Rollmaster aging-invoices pull is wired in a later phase.
    return None
