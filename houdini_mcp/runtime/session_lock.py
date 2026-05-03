"""In-process session preferences for a single MCP server (stdio) instance.

Used for human-in-the-loop ``lock`` so the agent can restate the preferred GEO path
in every summary/review without persisting to disk.
"""

from __future__ import annotations

import threading
from typing import Any

_lock = threading.Lock()
_state: dict[str, Any] = {
    "locked_parent_path": None,
    "note": None,
    "effect_tier": None,
}


def set_session_lock(
    *,
    locked_parent_path: str | None = None,
    note: str | None = None,
    effect_tier: str | None = None,
) -> dict[str, Any]:
    """Replace lock state. Pass empty/None strings to clear fields."""
    global _state
    with _lock:
        lp = (locked_parent_path or "").strip() or None
        nt = (note or "").strip() or None
        if nt is not None and len(nt) > 2000:
            nt = nt[:2000]
        et = (effect_tier or "").strip() or None
        if et is not None and len(et) > 32:
            et = et[:32]
        _state = {
            "locked_parent_path": lp,
            "note": nt,
            "effect_tier": et,
        }
        return dict(_state)


def get_session_lock() -> dict[str, Any]:
    with _lock:
        return dict(_state)


def clear_session_lock() -> dict[str, Any]:
    return set_session_lock(locked_parent_path="", note="", effect_tier="")


def update_session_lock(
    *,
    locked_parent_path: str | None = None,
    note: str | None = None,
    effect_tier: str | None = None,
) -> dict[str, Any]:
    """Merge into lock state. ``None`` = leave field unchanged; ``\"\"`` clears that field."""
    global _state
    with _lock:
        next_state = dict(_state)
        if locked_parent_path is not None:
            lp = locked_parent_path.strip() or None
            next_state["locked_parent_path"] = lp
        if note is not None:
            nt = note.strip() or None
            if nt is not None and len(nt) > 2000:
                nt = nt[:2000]
            next_state["note"] = nt
        if effect_tier is not None:
            et = effect_tier.strip() or None
            if et is not None and len(et) > 32:
                et = et[:32]
            next_state["effect_tier"] = et
        _state = next_state
        return dict(_state)
