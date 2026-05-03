"""Rollback helper: receiver-side undo_group covers batch; expose explicit undo for recovery."""

from __future__ import annotations

from core.result import CoreResult
from core.undo import rollback as remote_rollback


def rollback_last_remote() -> CoreResult:
    """Ask Houdini to perform one undo (used when batch was not grouped — Sprint 1 escape hatch)."""
    return remote_rollback()
