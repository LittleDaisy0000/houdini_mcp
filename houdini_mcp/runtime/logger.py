"""Minimal structured run log (in-memory). Sprint 1."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RunRecord:
    run_id: str
    started_at: float
    plan_id: str
    steps: list[dict[str, Any]] = field(default_factory=list)
    finished_at: float | None = None
    ok: bool | None = None


_LOGS: dict[str, RunRecord] = {}


def start_run(plan_id: str) -> str:
    run_id = str(uuid.uuid4())
    _LOGS[run_id] = RunRecord(run_id=run_id, started_at=time.time(), plan_id=plan_id)
    return run_id


def append_step(run_id: str, step: dict[str, Any]) -> None:
    rec = _LOGS.get(run_id)
    if rec:
        rec.steps.append(step)


def finish_run(run_id: str, ok: bool) -> None:
    rec = _LOGS.get(run_id)
    if rec:
        rec.finished_at = time.time()
        rec.ok = ok


def get_logs(run_id: str) -> dict[str, Any] | None:
    rec = _LOGS.get(run_id)
    if not rec:
        return None
    return {
        "run_id": rec.run_id,
        "plan_id": rec.plan_id,
        "started_at": rec.started_at,
        "finished_at": rec.finished_at,
        "ok": rec.ok,
        "steps": list(rec.steps),
    }
