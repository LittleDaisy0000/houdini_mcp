"""Dry-run: preview batch without committing (delegates to receiver when available)."""

from __future__ import annotations

from typing import Any

from core.bridge import BridgeError, send_expect_core_result
from planner.preflight import preflight_plan


def dry_run(plan: dict[str, Any]) -> dict[str, Any]:
    pf = preflight_plan(plan)
    if not pf["ok"]:
        return {"ok": False, "preflight": pf, "preview": None, "errors": pf["errors"]}
    actions = plan.get("actions") or []
    undo_label = f"mcp_dry_run:{plan.get('plan_id', '')}"
    try:
        r = send_expect_core_result(
            "batch.execute",
            {
                "actions": actions,
                "dry_run": True,
                "undo_label": undo_label,
                "plan_id": plan.get("plan_id"),
            },
        )
        return {
            "ok": r.ok,
            "preflight": pf,
            "preview": r.data,
            "warnings": r.warnings,
            "errors": r.errors,
        }
    except BridgeError as e:
        return {
            "ok": False,
            "preflight": pf,
            "preview": None,
            "warnings": list(pf.get("warnings") or []),
            "errors": [str(e)],
        }
