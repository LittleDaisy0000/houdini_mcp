"""In-process plan cache for MCP: avoid embedding large ``plan`` JSON in tool arguments.

``plan_build_adhoc`` / ``plan_build_adhoc_execute`` store by ``plan_id``; ``runtime_execute`` / ``runtime_dry_run`` can resolve by id.
Cache is cleared when the MCP server process exits (e.g. Cursor reconnect).
"""

from __future__ import annotations

import copy
import threading
from typing import Any

_LOCK = threading.Lock()
_CACHE: dict[str, dict[str, Any]] = {}
_ORDER: list[str] = []
_MAX_PLANS = 128


def remember_plan(plan: dict[str, Any]) -> str | None:
    """Store ``plan`` under ``plan["plan_id"]``. Returns that id, or None if missing."""

    pid = plan.get("plan_id")
    if not pid or not isinstance(pid, str):
        return None
    with _LOCK:
        if pid in _CACHE:
            _ORDER.remove(pid)
        elif len(_ORDER) >= _MAX_PLANS:
            oldest = _ORDER.pop(0)
            _CACHE.pop(oldest, None)
        _CACHE[pid] = copy.deepcopy(plan)
        _ORDER.append(pid)
    return pid


def get_plan(plan_id: str) -> dict[str, Any] | None:
    """Return a deepcopy of the cached plan, or None if unknown."""

    with _LOCK:
        p = _CACHE.get(plan_id)
        if p is None:
            return None
        return copy.deepcopy(p)
