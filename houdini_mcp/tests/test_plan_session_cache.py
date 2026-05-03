"""Tests for runtime.plan_session_cache (no Houdini)."""

from __future__ import annotations

from runtime.plan_session_cache import get_plan, remember_plan


def test_remember_and_get_roundtrip():
    plan = {"plan_id": "abc-123", "actions": [{"op": "noop"}], "recipe_id": "x"}
    assert remember_plan(plan) == "abc-123"
    got = get_plan("abc-123")
    assert got is not None
    assert got["plan_id"] == "abc-123"
    assert got["actions"][0]["op"] == "noop"
    # Mutating returned plan must not affect cache
    got["actions"][0]["op"] = "changed"
    got2 = get_plan("abc-123")
    assert got2["actions"][0]["op"] == "noop"


def test_unknown_plan_id():
    assert get_plan("nonexistent") is None


def test_remember_without_plan_id_returns_none():
    assert remember_plan({"actions": []}) is None
