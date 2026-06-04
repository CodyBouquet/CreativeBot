"""
Master sales report — per-branch totals + overall total + per-rep breakdown.

Same content for every recipient (it's a management view, not sliced).
"""
from .base import ReportContext


def build_section(user, ctx: ReportContext) -> str | None:
    """Return the master sales HTML section (same for every recipient). Stub: returns None until the Rollmaster sales pull is wired up."""
    # Stub until the Rollmaster sales pull is wired in a later phase.
    return None
