"""
Shared infrastructure for report modules.
"""
from typing import Any, Callable


class ReportContext:
    """
    Per-send-window cache. Modules with expensive shared work (e.g., a single
    Rollmaster pull that's identical across all recipients) stash it here on
    the first build_section call so subsequent users in the loop reuse it.

    Use a stable, descriptive cache key — `'inventory.lowstock_rows'` not
    `'data'` — to avoid collisions across modules.
    """
    def __init__(self):
        """Start with an empty per-send-window cache."""
        self._cache: dict[str, Any] = {}

    def get_or_compute(self, key: str, fn: Callable[[], Any]) -> Any:
        """Return the cached value for key, computing it via fn() and caching it on first access."""
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]


def report_card(title: str, body_html: str, *, icon: str = "",
                badge: str = "", accent: str = "#0077aa") -> str:
    """
    Wrap a report's inner HTML in a titled card for the email digest.

    Each module renders its own card via this helper so the digest wrapper just
    stacks them. Outlook-safe: table-based with inline styles only; the colored
    title bar carries the icon + title and an optional right-aligned badge (e.g.
    "5 to order"). border-radius degrades to square corners where unsupported.
    """
    icon_html = (
        f'<span style="font-size:17px; vertical-align:middle;">{icon}</span>&nbsp;&nbsp;'
        if icon else ""
    )
    badge_html = (
        f'<td align="right" style="padding:13px 18px; color:#ffffff; '
        f'font-size:12px; white-space:nowrap;">{badge}</td>'
        if badge else ""
    )
    return (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="border-collapse:separate; margin:18px 0; border:1px solid #e4e4e4; '
        'border-radius:10px; overflow:hidden; background:#ffffff;">'
        f'<tr><td style="padding:0; background:{accent};">'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
        f'<td style="padding:13px 18px; color:#ffffff; font-size:15px; '
        f'font-weight:bold; letter-spacing:0.3px;">{icon_html}{title}</td>'
        f'{badge_html}'
        '</tr></table></td></tr>'
        f'<tr><td style="padding:14px 18px 8px;">{body_html}</td></tr>'
        '</table>'
    )
