"""Manual smoke: call P48+ new/changed ops via TCP (run while Houdini receiver is listening).

Usage (from repo root):
  uv run python tests/manual_p48_receiver_smoke.py

Skips write-heavy ops (hda.ensure_file, viewport.flipbook file output) unless --writes is passed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from core.bridge import BridgeError, send_expect_core_result, send_raw  # noqa: E402


def _dispatch(op: str, **kwargs: object) -> dict:
    payload = {"op": op, **kwargs}
    return send_expect_core_result("core.dispatch", payload).to_dict()


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--writes", action="store_true", help="Run hda.ensure_file and viewport.flipbook (may write files)")
    args = p.parse_args()

    try:
        hp = send_raw("health.ping", {})
    except BridgeError as e:
        print("FAIL: cannot connect to receiver:", e)
        print("  Start the receiver in Houdini (127.0.0.1:63556) and retry.")
        return 1

    if not hp.get("ok"):
        print("FAIL: health.ping", hp)
        return 1
    inner = hp.get("result") or {}
    data = inner.get("data") if isinstance(inner, dict) else None
    rec = (data or {}).get("receiver") if isinstance(data, dict) else None
    print("OK  health.ping receiver=", rec, "houdini=", (data or {}).get("houdini") if isinstance(data, dict) else None)
    if rec and "P49" not in str(rec) and "P48" not in str(rec):
        print("WARN: expected P48+ in receiver string, got:", rec)

    def exists(path: str) -> bool:
        r = _dispatch("graph.exists", node_path=path)
        return bool(r.get("ok") and (r.get("data") or {}).get("exists"))

    geo_parent = "/obj/geo1"
    sop_candidates = [
        f"{geo_parent}/OUT",
        f"{geo_parent}/out",
        f"{geo_parent}/transform1",
        f"{geo_parent}/box1",
        f"{geo_parent}/file1",
    ]
    sop_path = next((p for p in sop_candidates if exists(p)), None)
    if sop_path is None and exists(geo_parent):
        lc = _dispatch("graph.list_children", path=geo_parent)
        kids = (lc.get("data") or {}).get("children") or []
        for name in kids:
            cand = f"{geo_parent}/{name}"
            if exists(cand):
                sop_path = cand
                break
    obj_path = geo_parent if exists(geo_parent) else "/obj"

    def first_existing(cands: list[str]) -> str | None:
        for c in cands:
            if exists(c):
                return c
        return None

    top_path = first_existing(["/tasks/topnet1", "/tasks/topnet"])
    if top_path is None and exists("/tasks"):
        lc = _dispatch("graph.list_children", path="/tasks")
        for nm in (lc.get("data") or {}).get("children") or []:
            cand = f"/tasks/{nm}"
            if exists(cand):
                top_path = cand
                break

    lop_path = first_existing(["/stage/usd_rop1", "/stage/karma1", "/stage/renderproduct1"])
    if lop_path is None and exists("/stage"):
        lc = _dispatch("graph.list_children", path="/stage")
        for nm in (lc.get("data") or {}).get("children") or []:
            cand = f"/stage/{nm}"
            if exists(cand):
                lop_path = cand
                break

    cases: list[tuple[str, dict]] = [
        ("session.snapshot", {"include_desktop": True}),
        ("path.expand_string", {"string": "$HIP"}),
        ("cache.pdg_clear", {"node_path": top_path} if top_path else {}),
        ("cache.clear_all", {}),
        ("exec.cache", {}),
        ("geo.topology_summary", {"node_path": sop_path or geo_parent}),
        ("io.file_parms_guess", {"node_path": obj_path}),
        (
            "chop.parm_channel_state",
            {"node_path": sop_path or f"{geo_parent}/transform1", "parm_name": "tx"},
        ),
        (
            "validate.parm_range",
            {"node_path": sop_path or f"{geo_parent}/transform1", "parm_name": "tx", "value": 0},
        ),
        ("top.workitems_scan", {"node_path": top_path} if top_path else {"node_path": "/tasks/topnet1"}),
        ("lop.usd_layer_stack", {"node_path": lop_path} if lop_path else {"node_path": "/stage/usd_rop1"}),
        (
            "lop.stage_summary",
            {"node_path": lop_path, "max_prims": 100, "include_layer_paths": True}
            if lop_path
            else {"node_path": "/stage/usd_rop1", "max_prims": 100, "include_layer_paths": True},
        ),
    ]

    optional_writes: list[tuple[str, dict]] = []
    if args.writes:
        optional_writes = [
            ("hda.ensure_file", {"file_path": "$HIP/no_such_asset_should_fail.hda"}),
            ("viewport.flipbook", {"output_path": "$HIP/__mcp_fb_test.$F4.jpg"}),
        ]

    failed = 0
    skipped = 0
    for op, kwargs in cases + optional_writes:
        try:
            out = _dispatch(op, **kwargs)
            ok = out.get("ok", False)
            err = out.get("errors") or []
            data = out.get("data")
            msg = "; ".join(err) if err else ""
            optional = op in ("top.workitems_scan", "lop.usd_layer_stack", "lop.stage_summary") and (
                "Node not found" in msg or "not found" in msg.lower()
            )
            if ok:
                status = "OK "
            elif optional:
                status = "SKIP"
                skipped += 1
            else:
                status = "FAIL"
                failed += 1
            preview = json.dumps(data or {}, ensure_ascii=False)
            if len(preview) > 400:
                preview = preview[:400] + "..."
            print(f"{status} {op} errors={err[:1]} data_preview={preview}")
        except BridgeError as e:
            failed += 1
            print(f"FAIL {op} BridgeError: {e}")
        except Exception as e:
            failed += 1
            print(f"FAIL {op} {e!r}")

    # Aliases: if default paths missing, print hint
    print()
    print("Note: top/lop tests expect named nodes; adjust paths in this script to match your HIP.")
    print(f"Done. failures={failed} skipped_optional={skipped}")
    return 0 if failed == 0 else 2


if __name__ == "__main__":
    raise SystemExit(main())
