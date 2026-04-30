"""
Master sales report — per-branch totals + overall total + per-rep breakdown.

Same content for every recipient (it's a management view, not sliced).
"""
from .base import ReportContext


def build_section(user, ctx: ReportContext) -> str | None:
    # Stub until the Rollmaster sales pull is wired in a later phase.
    return None
