"""
Late Net30 report — Net30 invoices that have aged past their due date.
"""
from .base import ReportContext


def build_section(user, ctx: ReportContext) -> str | None:
    # Stub until the Rollmaster aging-invoices pull is wired in a later phase.
    return None
