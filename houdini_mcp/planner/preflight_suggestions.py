"""Heuristic hints when static preflight fails (no Houdini calls)."""

from __future__ import annotations


def suggestions_for_preflight_errors(errors: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for e in errors:
        el = str(e).lower()
        if "requires non-empty node_path" in el or "requires non-empty parent_path" in el or "requires non-empty dst" in el:
            msg = (
                "Resolve absolute node paths first: call houdini_session_snapshot (or core.dispatch session.snapshot), "
                "or use graph.glob under a known parent."
            )
            if msg not in seen:
                seen.add(msg)
                out.append(msg)
        if "requires non-empty file_path" in el:
            msg = "Disk ops need a non-empty absolute file_path (or path alias)."
            if msg not in seen:
                seen.add(msg)
                out.append(msg)
        if "requires non-empty" in el and "parm_name" in el:
            msg = "Confirm parm_name matches a real parameter on the target node (node.info / parm.list)."
            if msg not in seen:
                seen.add(msg)
                out.append(msg)
        if "node.create" in el and "node_type" in el:
            msg = "Use a valid Houdini node type string (see houdini_ops_catalog or node.info on a similar node)."
            if msg not in seen:
                seen.add(msg)
                out.append(msg)
    return out
