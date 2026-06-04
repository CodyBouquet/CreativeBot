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
