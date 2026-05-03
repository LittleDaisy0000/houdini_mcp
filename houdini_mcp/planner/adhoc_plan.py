"""Build a Plan from caller-supplied action steps (no recipe YAML).

Used for open-ended / NL-driven workflows: the agent composes ``actions`` lists
that match the Houdini receiver's ``op`` vocabulary.
"""

from __future__ import annotations

import copy
import re
import uuid
from typing import Any

_MAX_ACTIONS = 512


def _sanitize_recipe_tag(raw: str | None) -> str:
    if not raw or not str(raw).strip():
        return "adhoc"
    s = str(raw).strip()[:96]
    s = re.sub(r"[^a-zA-Z0-9._:-]+", "_", s)
    return s or "adhoc"


def build_adhoc_plan(
    actions: list[dict[str, Any]],
    *,
    recipe_tag: str | None = None,
    intent: str | None = None,
    session_context: dict[str, Any] | None = None,
    estimated_risk: str = "low",
    required_confirm: bool = False,
    rollback_strategy: str = "undo_group",
) -> dict[str, Any]:
    if not isinstance(actions, list):
        raise TypeError("actions must be a list of op dicts")
    if len(actions) > _MAX_ACTIONS:
        raise ValueError(f"Too many actions (max {_MAX_ACTIONS})")

    normalized: list[dict[str, Any]] = []
    for i, step in enumerate(actions):
        if not isinstance(step, dict):
            raise TypeError(f"actions[{i}] must be a dict")
        op = step.get("op")
        if not op or not str(op).strip():
            raise ValueError(f"actions[{i}] missing non-empty 'op'")
        body = {k: v for k, v in step.items() if k != "op"}
        normalized.append({"op": str(op), **copy.deepcopy(body)})

    tag = _sanitize_recipe_tag(recipe_tag)
    plan_id = str(uuid.uuid4())
    plan: dict[str, Any] = {
        "plan_id": plan_id,
        "recipe_id": tag,
        "recipe_version": "adhoc",
        "recipe_file": None,
        "actions": normalized,
        "estimated_risk": estimated_risk,
        "required_confirm": required_confirm,
        "rollback_strategy": rollback_strategy,
        "outputs": {},
    }
    if intent and str(intent).strip():
        plan["adhoc_intent"] = str(intent).strip()[:2000]
    if session_context and isinstance(session_context, dict) and session_context:
        plan["session_context"] = copy.deepcopy(session_context)
    return plan
