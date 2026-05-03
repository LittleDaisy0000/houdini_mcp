"""Bridge: parse structured ``result`` when wire-level ``ok`` is false (e.g. batch.execute step failure)."""

from __future__ import annotations

from unittest.mock import patch

from core.bridge import send_expect_core_result
from core.result import CoreResult


def test_send_expect_core_result_batch_failure_keeps_step_data() -> None:
    fake = {
        "ok": False,
        "error": {"code": "EXECUTION_ERROR", "message": "sop.wrangle_recompile: no button"},
        "result": {
            "ok": False,
            "data": {
                "steps": [
                    {
                        "index": 0,
                        "op": "sop.wrangle_recompile",
                        "ok": False,
                        "data": {"all_button_parm_tokens": ["foo"]},
                        "errors": ["sop.wrangle_recompile: no button"],
                        "warnings": [],
                    }
                ]
            },
            "errors": ["sop.wrangle_recompile: no button"],
            "warnings": [],
        },
    }
    with patch("core.bridge.send_raw", return_value=fake):
        r = send_expect_core_result("batch.execute", {"actions": []})
    assert r.ok is False
    assert r.data is not None
    assert r.data.get("steps")
    assert r.errors


def test_send_expect_core_result_top_ok_true_unchanged() -> None:
    fake = {
        "ok": True,
        "result": {"ok": True, "data": {"dry_run": False, "steps": []}, "errors": [], "warnings": []},
    }
    with patch("core.bridge.send_raw", return_value=fake):
        r = send_expect_core_result("batch.execute", {"actions": []})
    assert r.ok is True
    assert r.data is not None
