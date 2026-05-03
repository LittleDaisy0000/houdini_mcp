from __future__ import annotations

from planner.adhoc_plan import build_adhoc_plan
from planner.preflight import preflight_plan


def test_build_adhoc_plan_basic() -> None:
    plan = build_adhoc_plan(
        [{"op": "graph.exists", "node_path": "/obj/geo1"}],
        recipe_tag="test_nl",
        intent="check geo exists",
    )
    assert plan["plan_id"]
    assert plan["recipe_id"] == "test_nl"
    assert plan["recipe_version"] == "adhoc"
    assert plan["actions"] == [{"op": "graph.exists", "node_path": "/obj/geo1"}]
    assert plan.get("adhoc_intent") == "check geo exists"


def test_adhoc_preflight() -> None:
    plan = build_adhoc_plan([{"op": "parm.set", "node_path": "/obj/box1", "parm_name": "size", "value": 2}])
    pf = preflight_plan(plan)
    assert pf["ok"]


def test_build_adhoc_plan_session_context() -> None:
    plan = build_adhoc_plan(
        [{"op": "session.snapshot"}],
        session_context={"hip_path": "/tmp/foo.hip", "frame": 12},
    )
    assert plan["session_context"]["frame"] == 12
    assert plan["session_context"]["hip_path"] == "/tmp/foo.hip"
