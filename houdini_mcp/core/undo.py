"""Remote Core: undo group boundaries (used by batch receiver; thin RPC for advanced cases)."""

from __future__ import annotations

from core.bridge import send_expect_core_result
from core.result import CoreResult


def begin(label: str) -> CoreResult:
    return send_expect_core_result("core.dispatch", {"op": "undo.begin", "label": label})


def end() -> CoreResult:
    return send_expect_core_result("core.dispatch", {"op": "undo.end"})


def rollback() -> CoreResult:
    return send_expect_core_result("core.dispatch", {"op": "undo.rollback"})
