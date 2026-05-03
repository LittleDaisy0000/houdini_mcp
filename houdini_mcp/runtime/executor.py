"""Execute Plan via a single batch on the receiver (undo_group rollback strategy)."""

from __future__ import annotations

from typing import Any

from core.bridge import BridgeError, send_expect_core_result
from planner.preflight import preflight_plan
from runtime.logger import append_step, finish_run, start_run


def execute(plan: dict[str, Any]) -> dict[str, Any]:
    pf = preflight_plan(plan)
    if not pf["ok"]:
        return {"ok": False, "preflight": pf, "run_id": None, "result": None, "errors": pf["errors"]}

    plan_id = str(plan.get("plan_id") or "")
    run_id = start_run(plan_id)
    append_step(run_id, {"phase": "preflight", "detail": pf})

    undo_label = f"mcp_plan:{plan_id}" if plan_id else "mcp_plan"
    actions = plan.get("actions") or []

    try:
        r = send_expect_core_result(
            "batch.execute",
            {
                "actions": actions,
                "dry_run": False,
                "undo_label": undo_label,
                "plan_id": plan_id,
            },
        )
        append_step(run_id, {"phase": "batch.execute", "detail": r.to_dict()})
        finish_run(run_id, r.ok)
        return {
            "ok": r.ok,
            "run_id": run_id,
            "preflight": pf,
            "result": r.data,
            "warnings": r.warnings,
            "errors": r.errors,
        }
    except BridgeError as e:
        append_step(run_id, {"phase": "error", "detail": str(e)})
        finish_run(run_id, False)
        return {
            "ok": False,
            "run_id": run_id,
            "preflight": pf,
            "result": None,
            "warnings": list(pf.get("warnings") or []),
            "errors": [str(e)],
        }
