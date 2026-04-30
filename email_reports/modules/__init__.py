"""
Report modules. Each report_key in AVAILABLE_REPORTS has a corresponding
module here exposing one function:

    build_section(user, ctx) -> str | None

`user` is a sqlite3.Row from m365_directory.list_active_users() — has
`email`, `display_name`, and `rm_sales_id` columns.

`ctx` is a ReportContext (see base.py) — modules use it to cache work
that's identical across users (e.g., the inventory pull) so the per-user
loop doesn't re-fetch.

Returning None means "no section for this user" — the digest skips it
silently. Raise an exception only for unrecoverable problems; a single
report's failure shouldn't block a user from receiving the others.
"""
from typing import Callable

from .base import ReportContext
from .inventory import build_section as _inventory
from .sales import build_section as _sales
from .master_sales import build_section as _master_sales
from .late_net30 import build_section as _late_net30

MODULES: dict[str, Callable] = {
    "inventory_low_stock":  _inventory,
    "sales_report":         _sales,
    "master_sales_report":  _master_sales,
    "late_net30":           _late_net30,
}

__all__ = ["MODULES", "ReportContext"]
