"""Houdini MCP (stdio) — aligned with mobu_mcp_server: FastMCP + TCP bridge to Houdini.

红线2：场景写入只走 ``runtime_execute(plan)``（由 Runtime 调 batch.execute）。
红线3：执行由接收端 ``hou.undos.group`` 包裹并记录 run log（见 runtime/logger）。
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path
from typing import Annotated, Any, Optional

_root = Path(__file__).resolve().parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from mcp.server.fastmcp import FastMCP, Image
from pydantic import Field

from core.bridge import (
    BRIDGE_VERSION,
    BridgeError,
    HOUDINI_CONNECT_TIMEOUT_SEC,
    HOUDINI_HOST,
    HOUDINI_PORT,
    HOUDINI_TIMEOUT_SEC,
    send_expect_core_result,
    send_raw,
)
from core.result import CoreResult
from planner.adhoc_plan import build_adhoc_plan
from planner.op_catalog import get_op_catalog
from runtime.dryrun import dry_run
from runtime.executor import execute
from runtime.logger import get_logs
from runtime.plan_session_cache import get_plan, remember_plan
from runtime.rollback import rollback_last_remote
from runtime.session_lock import clear_session_lock, get_session_lock, update_session_lock

_MCP_INSTRUCTIONS = """\
Default Houdini effect workflow (match common Houdini MCP UIs: think → small Python → observe → fix).

=== ANY NL REQUEST — INTENT CONTRACT (all effects; not only path or fracture) ===
Before the first scene-changing tool call in a task: write a short numbered list of user deliverables ("done means …") in Houdini terms — geometry/behavior/materials/parms to expose/time range, etc. Map each item to evidence you will check in scene.data and viewport (and note what you are explicitly NOT building if the user did not ask for it).
Do NOT default to the last recipe you know (e.g. RBD fracture) unless the user asked for that class of effect. If one critical ambiguity remains, ask ONE targeted question instead of guessing silently.
After each major write: houdini_scene_and_viewport_review must verify EVERY contract item; any item unmet → fix before claiming 一致/完成. If the user asked to expose parameters, include houdini_create_ctrl_null (or mcp.ctrl_null_setup) when drivers exist — missing ctrl is not "done".

=== MOTION / SIMULATION — DEFAULT PIPELINE (mandatory; do NOT wait for the user to say "this is animation") ===
Apply whenever the goal involves or may involve time-varying behavior: animation, simulation, RBD, FLIP/particles over time, caches, keyframed deformation, **path following / slide along a curve**, effects described with motion over frames, or any NL where motion is plausible. Skip this block ONLY if the user explicitly requests a single static frame / still render only (no time axis).
A) Use houdini_scene_summary (same turn is ok) and read playback_start, playback_end, playback_globals vs the effect span you intend to verify.
B) If the playbar does not bracket that span (e.g. still one frame, inverted, too short, or missing frames where contact/explosion/peak happens), immediately houdini_execute_python to set hou.playbarFrameRange to a sensible inclusive range, then continue.
C) houdini_scene_and_viewport_review: keep auto_keyframe_viewport=true by default (≈ start/mid/end from the playbar). If critical beats are unlikely to land on those three (late hit, short spike, mid-range only), proactively pass frames_json (e.g. "[1,20,40,60]") or frame_end — do NOT wait for the user to ask for denser sampling.
D) After every such review in your reply: list which frame numbers were captured (from tool args or viewport.data); for each, state 一致/不一致 vs the user description; then overall 一致/部分一致/不一致. Chat has no live viewport stream — multi-frame stills are the only motion evidence.

=== PATH + CURVE MOTION + "expose parms" (mandatory interpretation) ===
If the user asks for a **curve path** (incl. S-curve), **object moves along the path**, and/or **expose important parameters**:
- You MUST build **visible curve geometry** (e.g. curve/resample; polywire or merge branch so the path is seen in viewport). Saying "path" without a curve-like SOP chain is wrong.
- You MUST make **position change across frames** on the cube (e.g. wrangle P from curve via primuv/xyzdist with animated u, or a robust OBJ constraint). A static box alone is failure.
- After motion works, you MUST call **houdini_create_ctrl_null** (or mcp.ctrl_null_setup) to expose drivers (path scale, u/speed, resample segments, box size, etc.) via ch() spares.
- Before claiming done: multi-frame review must show **different cube positions** on at least two frames OR you state mismatch. Do not default to RBD/fracture recipes for this class of request.

1) houdini_health once when the session is new (check mcp_bridge_hints for timeouts / remote host).
2) houdini_scene_summary before inventing node paths (keep rich_context on: playback_globals, selected parm/diagnostics, geo_display_hints). Each summary/review payload includes mcp_session_lock + mcp_bridge_hints when data is a dict.
3) Human-in-the-loop: do not assume one-shot success. Prefer a runnable v0 first, then v1 polish; ask the user for short confirmations (correct GEO path, happy with one frame, direction ok). When the user fixes a path (e.g. "only /obj/geo1"), call houdini_session_lock_set(locked_parent_path=...) so later tools drift less.
4) houdini_execute_python in small steps (inline code only). After EVERY write that changes geometry, materials, or simulation/time, the NEXT tool call MUST be houdini_scene_and_viewport_review (do not claim success from exec alone). For motion-class tasks, that review MUST follow the MOTION DEFAULT PIPELINE above (multi-frame + playbar fix if needed).
5) houdini_scene_and_viewport_review: compares scene.data (node tree) + viewport images to the user request. Large flipbooks / base64: raise HOUDINI_SOCKET_TIMEOUT_SEC (or HOUDINI_TIMEOUT_SEC); Houdini-side frame cap HOUDINI_MCP_MAX_VIEWPORT_FRAMES. If Cursor and Houdini differ hosts, treat paths as on the Houdini machine; use include_image_base64=false when embeds are too heavy.
6) If review says mismatch vs the user goal, revise Python and repeat execute → review until aligned or you state clearly why not (e.g. no hou.ui).
7) When an effect is stable inside a GEO, call houdini_create_ctrl_null (or plan op mcp.ctrl_null_setup) to add an ``mcp_ctrl`` Null with spare parms ch()‑linked to key drivers for later tuning. For path/curve+motion requests, ctrl null is part of the deliverable once v0 motion is visible — not optional "polish".
"""


def _core_payload(r: CoreResult) -> dict[str, Any]:
    return {
        "ok": r.ok,
        "bridge_version": BRIDGE_VERSION,
        "data": r.data,
        "warnings": r.warnings,
        "errors": r.errors,
    }


def _mcp_images_from_viewport_data(data: Any) -> list[Image]:
    """Turn receiver ``viewport_images`` entries into FastMCP Image blocks (inline thumbnails in Cursor)."""
    if not isinstance(data, dict):
        return []
    vis = data.get("viewport_images")
    if not isinstance(vis, list):
        return []
    out: list[Image] = []
    for item in vis:
        if not isinstance(item, dict):
            continue
        if item.get("error") or not item.get("data_base64"):
            continue
        try:
            raw = base64.standard_b64decode(str(item["data_base64"]))
            if raw:
                out.append(Image(data=raw))
        except Exception:
            continue
    return out


def _with_optional_inline_images(body: dict[str, Any], embed: bool, data: Any) -> dict[str, Any] | tuple[Any, ...]:
    if not embed or not body.get("ok"):
        return body
    imgs = _mcp_images_from_viewport_data(data)
    if not imgs:
        return body
    return (body, *imgs)


def _scene_summary_dispatch_kwargs(
    max_obj_nodes: int,
    include_sop_children: bool,
    *,
    rich_context: bool = True,
    max_selected_detail_nodes: int = 8,
    max_parms_per_node: int = 24,
    geo_hint_max_geos: int = 6,
    diagnostics_force_cook: bool = True,
) -> dict[str, Any]:
    return {
        "op": "scene.summary",
        "max_obj_nodes": max_obj_nodes,
        "include_sop_children": include_sop_children,
        "rich_context": rich_context,
        "max_selected_detail_nodes": max_selected_detail_nodes,
        "max_parms_per_node": max_parms_per_node,
        "geo_hint_max_geos": geo_hint_max_geos,
        "diagnostics_force_cook": diagnostics_force_cook,
    }


def _auto_sparse_frames_from_scene_summary(scene_data: Any) -> list[float] | None:
    """Pick ~3 keyframes from scene.summary playback range (matches reference MCP multi-frame review)."""
    if not isinstance(scene_data, dict):
        return None
    try:
        a = scene_data.get("playback_start")
        b = scene_data.get("playback_end")
        cur = scene_data.get("frame")
        if a is None or b is None:
            if cur is not None:
                return [float(cur)]
            return None
        lo, hi = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if hi < lo:
        lo, hi = hi, lo
    if hi - lo < 0.05:
        mid = float(scene_data.get("frame", lo))
        return [mid]
    mid = (lo + hi) / 2.0
    return [float(lo), float(mid), float(hi)]


def _mcp_bridge_hints() -> dict[str, Any]:
    return {
        "houdini_host": HOUDINI_HOST,
        "houdini_port": HOUDINI_PORT,
        "socket_timeout_sec": HOUDINI_TIMEOUT_SEC,
        "connect_timeout_sec": HOUDINI_CONNECT_TIMEOUT_SEC,
        "env": (
            "HOUDINI_SOCKET_TIMEOUT_SEC (or legacy HOUDINI_TIMEOUT_SEC) for long responses; "
            "HOUDINI_CONNECT_TIMEOUT_SEC; HOUDINI_HOST / HOUDINI_PORT when Houdini is remote."
        ),
        "receiver": "HOUDINI_MCP_MAX_VIEWPORT_FRAMES caps flipbook frame count (default 96, max 512).",
        "paths_and_pixels": (
            "$HIP and snapshot files live on the Houdini host. When the IDE is elsewhere, lean on "
            "scene paths + mcp_session_lock; if images time out or are huge, use include_image_base64=false."
        ),
    }


def _inject_mcp_context_into_data(data: Any) -> Any:
    if isinstance(data, dict):
        merged = dict(data)
    elif data is None:
        merged = {}
    else:
        return data
    merged["mcp_session_lock"] = get_session_lock()
    merged["mcp_bridge_hints"] = _mcp_bridge_hints()
    return merged


def _enrich_tool_data_dict(body: dict[str, Any]) -> dict[str, Any]:
    d = body.get("data")
    if isinstance(d, dict):
        return {**body, "data": _inject_mcp_context_into_data(d)}
    return body


def _enriched_scene_block(r: CoreResult) -> dict[str, Any]:
    return {
        "ok": r.ok,
        "data": _inject_mcp_context_into_data(r.data),
        "warnings": r.warnings,
        "errors": r.errors,
    }


def _resolve_viewport_frame_node_path(frame_node_path: Optional[str]) -> Optional[str]:
    fp = (frame_node_path or "").strip()
    if fp:
        return fp
    lp = (get_session_lock().get("locked_parent_path") or "").strip()
    return lp or None


def _apply_viewport_autoframe_to_payload(
    payload: dict[str, Any],
    *,
    viewport_autoframe: Any,
    frame_node_path: Optional[str],
) -> None:
    payload["viewport_autoframe"] = viewport_autoframe
    fn = _resolve_viewport_frame_node_path(frame_node_path)
    if fn:
        payload["frame_node_path"] = fn


mcp = FastMCP("Houdini-MCP", json_response=True, instructions=_MCP_INSTRUCTIONS)


@mcp.tool(
    title="Health check (Houdini)",
    description="Health check: verify Houdini TCP receiver connectivity and versions",
)
def houdini_health() -> dict[str, Any]:
    try:
        resp = send_raw("health.ping", {})
        if not resp.get("ok", False):
            err = resp.get("error") or {}
            return {
                "ok": False,
                "bridge_version": BRIDGE_VERSION,
                "error": f"{err.get('code')}: {err.get('message')}",
            }
        return {
            "ok": True,
            "bridge_version": BRIDGE_VERSION,
            "houdini": resp.get("result"),
            "mcp_bridge_hints": _mcp_bridge_hints(),
        }
    except BridgeError as e:
        return {"ok": False, "bridge_version": BRIDGE_VERSION, "error": str(e)}


@mcp.tool(
    title="Session lock (get)",
    description=(
        "Read the in-process session lock: preferred SOP network path (e.g. /obj/geo1), optional note, effect_tier (v0/v1). "
        "Same values are echoed under mcp_session_lock inside houdini_scene_summary / review scene.data when present."
    ),
)
def houdini_session_lock_get() -> dict[str, Any]:
    return {"ok": True, "bridge_version": BRIDGE_VERSION, "data": get_session_lock()}


@mcp.tool(
    title="Session lock (set / merge)",
    description=(
        "Set or merge human-confirmed scope so later edits default to the same GEO/SOP chain. "
        "Omit a field (null) to leave it unchanged; pass an empty string to clear that field. "
        "Use clear_all=true to reset everything. effect_tier examples: v0 (runnable first), v1 (detail pass)."
    ),
)
def houdini_session_lock_set(
    locked_parent_path: Annotated[
        Optional[str],
        Field(default=None, description="Preferred network path, e.g. /obj/geo1; empty string clears; omit = unchanged"),
    ] = None,
    note: Annotated[
        Optional[str],
        Field(default=None, description="Short reminder for the session; empty clears; omit = unchanged", max_length=2000),
    ] = None,
    effect_tier: Annotated[
        Optional[str],
        Field(default=None, description="e.g. v0 or v1; empty clears; omit = unchanged", max_length=32),
    ] = None,
    clear_all: Annotated[bool, Field(default=False, description="If true, ignore other fields and clear the lock")] = False,
) -> dict[str, Any]:
    if clear_all:
        clear_session_lock()
        return {"ok": True, "bridge_version": BRIDGE_VERSION, "data": get_session_lock()}
    return {
        "ok": True,
        "bridge_version": BRIDGE_VERSION,
        "data": update_session_lock(
            locked_parent_path=locked_parent_path,
            note=note,
            effect_tier=effect_tier,
        ),
    }


@mcp.tool(
    title="Session snapshot",
    description=(
        "Read-only: snapshot of the live Houdini session (hip path, unsaved flag, frame/range, selected node paths). "
        "Use before plan_build_adhoc to fill session_context_json or to choose absolute node paths."
    ),
)
def houdini_session_snapshot() -> dict[str, Any]:
    try:
        r = send_expect_core_result("core.dispatch", {"op": "session.snapshot"})
        body = {
            "ok": r.ok,
            "bridge_version": BRIDGE_VERSION,
            "data": r.data,
            "warnings": r.warnings,
            "errors": r.errors,
        }
        return _enrich_tool_data_dict(body)
    except BridgeError as e:
        return {"ok": False, "bridge_version": BRIDGE_VERSION, "error": str(e)}


@mcp.tool(
    title="Create MCP control Null",
    description=(
        "After finishing an effect inside a GEO SOP network, create a Null (default ``mcp_ctrl``) and expose "
        "important parameters as spare parms with ``ch()`` references to other SOPs (tuning hub). "
        "``parent_path`` is the GEO network (e.g. /obj/geo1). ``bindings_json`` is a JSON array of "
        '{"spare_name": "scatter_pts", "ref_node": "/obj/geo1/scatter1", "ref_parm": "npoints", "label": "Scatter count"}. '
        "Optional ``input_from`` wires a SOP into the null's input 0. Prefer calling once the node chain exists; "
        "then run houdini_scene_and_viewport_review."
    ),
)
def houdini_create_ctrl_null(
    parent_path: Annotated[str, Field(description="SOP network path, e.g. /obj/geo1")],
    null_name: Annotated[str, Field(default="mcp_ctrl", description="Name of the new Null", max_length=128)] = "mcp_ctrl",
    bindings_json: Annotated[
        str,
        Field(
            default="[]",
            description='JSON array of {spare_name, ref_node, ref_parm, label?}',
        ),
    ] = "[]",
    input_from: Annotated[
        Optional[str],
        Field(default=None, description="Optional SOP path to connect to the new null's input 0"),
    ] = None,
    set_display_flag: Annotated[bool, Field(default=False)] = False,
    auto_layout: Annotated[bool, Field(default=True)] = True,
    color_rgb: Annotated[
        Optional[str],
        Field(default=None, description='Optional JSON array "[r,g,b]" with values 0..1 for null wire color'),
    ] = None,
) -> dict[str, Any]:
    try:
        try:
            bindings = json.loads(bindings_json or "[]")
        except json.JSONDecodeError as e:
            return {"ok": False, "bridge_version": BRIDGE_VERSION, "error": f"bindings_json must be valid JSON: {e}"}
        if not isinstance(bindings, list):
            return {"ok": False, "bridge_version": BRIDGE_VERSION, "error": "bindings_json must decode to a JSON array"}
        payload: dict[str, Any] = {
            "op": "mcp.ctrl_null_setup",
            "parent_path": parent_path,
            "null_name": null_name,
            "bindings": bindings,
            "set_display_flag": set_display_flag,
            "auto_layout": auto_layout,
        }
        if input_from:
            payload["input_from"] = input_from
        if color_rgb is not None and str(color_rgb).strip():
            try:
                c = json.loads(color_rgb)
            except json.JSONDecodeError as e:
                return {"ok": False, "bridge_version": BRIDGE_VERSION, "error": f"color_rgb must be valid JSON: {e}"}
            if isinstance(c, list):
                payload["color"] = c
        r = send_expect_core_result("core.dispatch", payload)
        return _core_payload(r)
    except BridgeError as e:
        return {"ok": False, "bridge_version": BRIDGE_VERSION, "error": str(e)}


@mcp.tool(
    title="Execute Python in Houdini",
    description=(
        "Execute multi-line Python inside the live Houdini session (``hou`` is in scope). "
        "Default: wrap in hou.undos.group for one-undo rollback. "
        "POSTCONDITION (do not skip): if this call changes geometry, materials, simulation, or the timeline, "
        "you MUST immediately follow with houdini_scene_and_viewport_review in the same turn (or the very next tool batch). "
        "For 动画/破碎/RBD/flip/sim/cache-over-time requests, review MUST capture multiple timeline frames (Houdini does not stream video to the chat — "
        "use houdini_scene_and_viewport_review with auto_keyframe_viewport or explicit frames_json / frame_end). "
        "If scene.summary shows playback is still a single frame, set playbar range with Python before review. "
        "Never tell the user the effect is done based only on exec.ok — "
        "you need a review verdict (match / partial / mismatch) plus node tree + viewport evidence. "
        "Pass source ONLY via ``code`` — do NOT create workspace .py/.json for Houdini to load. "
        "Curve/path + slide + expose parms: follow MCP instructions PATH block + workspace rule houdini-mcp-path-motion-ctrl — not RBD-only templates. "
        "Risk: arbitrary scene modification — prefer plan_build_adhoc for tiny single-op edits."
    ),
)
def houdini_execute_python(
    code: Annotated[
        str,
        Field(
            description=(
                "Full Python source to exec() in Houdini. Must be inline text — do not point at repo files on disk."
            )
        ),
    ],
    undo_label: Annotated[str, Field(description="Undo block label")] = "mcp_exec_python",
    use_undo_group: Annotated[bool, Field(description="If true, wrap in hou.undos.group(undo_label)")] = True,
) -> dict[str, Any]:
    try:
        r = send_expect_core_result(
            "core.dispatch",
            {
                "op": "exec.python",
                "code": code,
                "undo_label": undo_label,
                "use_undo_group": use_undo_group,
            },
        )
        return {
            "ok": r.ok,
            "bridge_version": BRIDGE_VERSION,
            "data": r.data,
            "warnings": r.warnings,
            "errors": r.errors,
        }
    except BridgeError as e:
        return {"ok": False, "bridge_version": BRIDGE_VERSION, "error": str(e)}


@mcp.tool(
    title="Get Scene Summary",
    description=(
        "Read-only scene.summary: hip/frame/fps/selection plus /obj children with optional SOP name samples. "
        "With rich_context (default true), also returns playback_globals ($RFSTART/$RFEND vs current frame), "
        "selected_node_details (parm samples + compact cook errors/warnings per selected node), and geo_display_hints "
        "(display SOP prim/point counts + packed primitive sampling for the first GEOs). "
        "When data is a dict, also includes mcp_session_lock and mcp_bridge_hints (timeouts, remote host note). "
        "Use after natural-language requests to ground paths and catch broken cooks before more writes."
    ),
)
def houdini_scene_summary(
    max_obj_nodes: Annotated[int, Field(description="Max OBJ nodes to list", ge=1, le=2000)] = 200,
    include_sop_children: Annotated[bool, Field(description="List first SOP child names for GEO objects")] = True,
    rich_context: Annotated[
        bool,
        Field(
            description="If true, add playback_globals, selected_node_details, geo_display_hints (requires P73 receiver)",
        ),
    ] = True,
    max_selected_detail_nodes: Annotated[int, Field(description="Max selected nodes to expand", ge=0, le=32)] = 8,
    max_parms_per_node: Annotated[int, Field(description="Max parm samples per selected node", ge=1, le=128)] = 24,
    geo_hint_max_geos: Annotated[int, Field(description="Max GEO objects (from list head) for display topology hints", ge=0, le=32)] = 6,
    diagnostics_force_cook: Annotated[
        bool,
        Field(description="If true, force-cook each selected node before reading diagnostics (slower, more accurate)"),
    ] = True,
) -> dict[str, Any]:
    try:
        r = send_expect_core_result(
            "core.dispatch",
            _scene_summary_dispatch_kwargs(
                max_obj_nodes,
                include_sop_children,
                rich_context=rich_context,
                max_selected_detail_nodes=max_selected_detail_nodes,
                max_parms_per_node=max_parms_per_node,
                geo_hint_max_geos=geo_hint_max_geos,
                diagnostics_force_cook=diagnostics_force_cook,
            ),
        )
        body = {
            "ok": r.ok,
            "bridge_version": BRIDGE_VERSION,
            "data": r.data,
            "warnings": r.warnings,
            "errors": r.errors,
        }
        return _enrich_tool_data_dict(body)
    except BridgeError as e:
        return {"ok": False, "bridge_version": BRIDGE_VERSION, "error": str(e)}


@mcp.tool(
    title="Viewport Snapshot",
    structured_output=False,
    description=(
        "Save SceneViewer viewport to image file(s) via flipbook (needs Houdini GUI). "
        "Default: current playbar frame. Set frame_start alone to snap one frame. "
        "Set frame_end (with optional frame_start) for a contiguous range (auto-adds .$F4 in basename if missing). "
        "Pass frames_json='[1,24,48]' for sparse frames (e.g. animation check). "
        "With include_image_base64=true (default), the receiver fills data.viewport_images and this tool also returns "
        "MCP image blocks so clients can show inline thumbnails like other Houdini MCPs. "
        "For post-exec checks with automatic ~3 keyframes from the playbar, prefer houdini_scene_and_viewport_review. "
        "Before capture, the receiver can re-center the SceneViewer (viewport_autoframe default true: bbox from "
        "frame_node_path or network selection, else frameSelected, else frameAll). "
        "Paths remain on the Houdini host; large flipbook/base64 may need a higher HOUDINI_SOCKET_TIMEOUT_SEC "
        "(or HOUDINI_TIMEOUT_SEC) on the MCP bridge."
    ),
)
def houdini_viewport_snapshot(
    output_path: Annotated[
        str,
        Field(description="Output path or $HIP/... template; use .$F4 in name for explicit padding when batching"),
    ] = "$HIP/mcp_viewport_snapshot.png",
    frame_start: Annotated[
        Optional[float],
        Field(default=None, description="Timeline frame: single capture, or range start when frame_end is set"),
    ] = None,
    frame_end: Annotated[
        Optional[float],
        Field(default=None, description="If set, export frame_start..frame_end (inclusive) with frame_step"),
    ] = None,
    frame_step: Annotated[
        float,
        Field(default=1.0, ge=0.25, le=120.0, description="Step between frames for range export"),
    ] = 1.0,
    frames_json: Annotated[
        Optional[str],
        Field(
            default=None,
            description='JSON array of frame numbers for sparse export, e.g. "[1,12,24]". Mutually exclusive with frame_end.',
        ),
    ] = None,
    restore_playbar_frame: Annotated[
        bool,
        Field(description="After capture, restore playbar to the frame active before this tool"),
    ] = True,
    include_image_base64: Annotated[
        bool,
        Field(
            default=True,
            description="If true, receiver fills data.viewport_images (base64); MCP also attaches Image blocks for inline thumbnails",
        ),
    ] = True,
    max_image_bytes_per_file: Annotated[
        int,
        Field(default=1_200_000, ge=4096, le=4_000_000, description="Skip embedding if file exceeds this size"),
    ] = 1_200_000,
    max_images_embedded: Annotated[
        int,
        Field(default=3, ge=1, le=8, description="When many frames exported, embed up to this many (spread across range)"),
    ] = 3,
    viewport_autoframe: Annotated[
        Any,
        Field(
            default=True,
            description=(
                "Receiver: re-center view before flipbook. True=auto (bbox from frame_node_path or selected nodes, "
                "else frameSelected, else frameAll); False=off; or strings 'auto'|'all'|'selected'."
            ),
        ),
    ] = True,
    frame_node_path: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Optional OBJ/SOP path to frame (display SOP bbox). If omitted, uses mcp_session_lock.locked_parent_path when set.",
        ),
    ] = None,
):
    try:
        payload: dict[str, Any] = {
            "op": "viewport.snapshot",
            "output_path": output_path,
            "restore_playbar_frame": restore_playbar_frame,
            "include_image_base64": include_image_base64,
            "max_image_bytes_per_file": max_image_bytes_per_file,
            "max_images_embedded": max_images_embedded,
        }
        _apply_viewport_autoframe_to_payload(
            payload,
            viewport_autoframe=viewport_autoframe,
            frame_node_path=frame_node_path,
        )
        if frame_start is not None:
            payload["frame_start"] = frame_start
        if frame_end is not None:
            payload["frame_end"] = frame_end
        if frame_step != 1.0:
            payload["frame_step"] = frame_step
        if frames_json is not None and str(frames_json).strip():
            try:
                parsed = json.loads(frames_json)
            except json.JSONDecodeError as e:
                return {"ok": False, "bridge_version": BRIDGE_VERSION, "error": f"frames_json must be valid JSON: {e}"}
            if not isinstance(parsed, list):
                return {
                    "ok": False,
                    "bridge_version": BRIDGE_VERSION,
                    "error": "frames_json must decode to a JSON array of numbers",
                }
            payload["frames"] = parsed
        r = send_expect_core_result("core.dispatch", payload)
        body = _enrich_tool_data_dict(_core_payload(r))
        return _with_optional_inline_images(body, include_image_base64, r.data)
    except BridgeError as e:
        return {"ok": False, "bridge_version": BRIDGE_VERSION, "error": str(e)}


@mcp.tool(
    title="Scene + Viewport Review",
    structured_output=False,
    description=(
        "Closed-loop verification: fetch read-only scene node tree (scene.summary) plus viewport snapshot "
        "with embedded images by default. Use after houdini_execute_python or runtime_execute to check whether "
        "the scene structure and pixels match the user's request — compare `scene.data` (OBJ/SOP names) with "
        "`viewport.data.viewport_images` (when present). If inconsistent, revise code, execute again, then call this tool again. "
        "Also returns MCP Image blocks when pixels are embedded (inline thumbnails in the client). "
        "Scene half uses the same rich_context as houdini_scene_summary (playback_globals, selected parm/diagnostics, "
        "geo_display_hints) when enabled. "
        "MOTION CHECK: there is no live viewport stream in chat — motion is verified only by comparing several still captures. "
        "When auto_keyframe_viewport is true and you omit frames_json and frame_end, frames are taken from "
        "scene.summary playback_start..playback_end (~3 samples: start/mid/end). If those fields equal one frame or miss the sim, "
        "fix hou.playbarFrameRange (or pass explicit frames_json) before claiming the animation matches the user request. "
        "Set include_image_base64=false if payloads/timeouts are an issue (remote IDE vs Houdini host). "
        "Raise HOUDINI_SOCKET_TIMEOUT_SEC / HOUDINI_TIMEOUT_SEC for heavy cooks; receiver frame cap HOUDINI_MCP_MAX_VIEWPORT_FRAMES. "
        "Viewport capture uses the same viewport_autoframe / frame_node_path behavior as houdini_viewport_snapshot (default: auto-center on subject)."
    ),
)
def houdini_scene_and_viewport_review(
    max_obj_nodes: Annotated[int, Field(description="Max OBJ nodes in scene.summary", ge=1, le=2000)] = 200,
    include_sop_children: Annotated[bool, Field(description="Include first SOP child names per GEO in summary")] = True,
    rich_context: Annotated[bool, Field(description="Forward to scene.summary rich_context")] = True,
    max_selected_detail_nodes: Annotated[int, Field(ge=0, le=32)] = 8,
    max_parms_per_node: Annotated[int, Field(ge=1, le=128)] = 24,
    geo_hint_max_geos: Annotated[int, Field(ge=0, le=32)] = 6,
    diagnostics_force_cook: Annotated[bool, Field()] = True,
    output_path: Annotated[
        str,
        Field(description="Viewport image path template on Houdini host"),
    ] = "$HIP/mcp_review.png",
    frame_start: Annotated[
        Optional[float],
        Field(default=None, description="Optional single frame or range start for viewport snapshot"),
    ] = None,
    frame_end: Annotated[
        Optional[float],
        Field(default=None, description="If set, multi-frame viewport export (see houdini_viewport_snapshot)"),
    ] = None,
    frame_step: Annotated[float, Field(default=1.0, ge=0.25, le=120.0)] = 1.0,
    frames_json: Annotated[
        Optional[str],
        Field(default=None, description='Sparse frames JSON array; mutually exclusive with frame_end'),
    ] = None,
    restore_playbar_frame: Annotated[bool, Field(default=True)] = True,
    include_image_base64: Annotated[
        bool,
        Field(description="Embed captured image(s) as base64 for multimodal comparison to the request"),
    ] = True,
    max_image_bytes_per_file: Annotated[int, Field(default=1_200_000, ge=4096, le=4_000_000)] = 1_200_000,
    max_images_embedded: Annotated[int, Field(default=3, ge=1, le=8)] = 3,
    auto_keyframe_viewport: Annotated[
        bool,
        Field(
            description=(
                "Default true. If true and neither frames_json nor frame_end is set, derive ~3 sparse frames from "
                "scene.summary playback_start/playback_end. Motion/sim tasks: keep true unless you pass explicit "
                "frames_json/frame_end (see MCP instructions MOTION DEFAULT PIPELINE)."
            ),
        ),
    ] = True,
    viewport_autoframe: Annotated[
        Any,
        Field(
            default=True,
            description="Forwarded to viewport.snapshot: auto re-center SceneViewer before flipbook (see houdini_viewport_snapshot).",
        ),
    ] = True,
    frame_node_path: Annotated[
        Optional[str],
        Field(
            default=None,
            description="Forwarded to viewport.snapshot; defaults to session lock locked_parent_path when omitted.",
        ),
    ] = None,
):
    try:
        r_scene = send_expect_core_result(
            "core.dispatch",
            _scene_summary_dispatch_kwargs(
                max_obj_nodes,
                include_sop_children,
                rich_context=rich_context,
                max_selected_detail_nodes=max_selected_detail_nodes,
                max_parms_per_node=max_parms_per_node,
                geo_hint_max_geos=geo_hint_max_geos,
                diagnostics_force_cook=diagnostics_force_cook,
            ),
        )
        vp_payload: dict[str, Any] = {
            "op": "viewport.snapshot",
            "output_path": output_path,
            "restore_playbar_frame": restore_playbar_frame,
            "include_image_base64": include_image_base64,
            "max_image_bytes_per_file": max_image_bytes_per_file,
            "max_images_embedded": max_images_embedded,
        }
        _apply_viewport_autoframe_to_payload(
            vp_payload,
            viewport_autoframe=viewport_autoframe,
            frame_node_path=frame_node_path,
        )
        if frame_start is not None:
            vp_payload["frame_start"] = frame_start
        if frame_end is not None:
            vp_payload["frame_end"] = frame_end
        if frame_step != 1.0:
            vp_payload["frame_step"] = frame_step
        used_auto_frames: list[float] | None = None
        if frames_json is not None and str(frames_json).strip():
            try:
                parsed = json.loads(frames_json)
            except json.JSONDecodeError as e:
                return {
                    "ok": False,
                    "bridge_version": BRIDGE_VERSION,
                    "error": f"frames_json must be valid JSON: {e}",
                    "scene": _enriched_scene_block(r_scene),
                }
            if not isinstance(parsed, list):
                return {
                    "ok": False,
                    "bridge_version": BRIDGE_VERSION,
                    "error": "frames_json must decode to a JSON array of numbers",
                    "scene": _enriched_scene_block(r_scene),
                }
            vp_payload["frames"] = parsed
        elif frame_end is None and frame_start is None and auto_keyframe_viewport:
            auto_f = _auto_sparse_frames_from_scene_summary(r_scene.data if r_scene.ok else None)
            if auto_f:
                vp_payload["frames"] = auto_f
                used_auto_frames = list(auto_f)
        r_vp = send_expect_core_result("core.dispatch", vp_payload)
        ok = bool(r_scene.ok and r_vp.ok)
        body: dict[str, Any] = {
            "ok": ok,
            "bridge_version": BRIDGE_VERSION,
            "scene": _enriched_scene_block(r_scene),
            "viewport": {
                "ok": r_vp.ok,
                "data": r_vp.data,
                "warnings": r_vp.warnings,
                "errors": r_vp.errors,
            },
            "review_hints": (
                "Compare user's request vs scene.data (names/paths) and viewport.data.viewport_images (pixels). "
                "If mismatch, fix houdini_execute_python (or plan) and re-run, then call this tool again."
            ),
            "viewport_auto_frames": used_auto_frames,
        }
        return _with_optional_inline_images(body, include_image_base64, r_vp.data)
    except BridgeError as e:
        return {"ok": False, "bridge_version": BRIDGE_VERSION, "error": str(e)}


@mcp.tool(
    title="Houdini ops catalog",
    description=(
        "Reference: list atomic Houdini ops the receiver understands. "
        "Use with plan_build_adhoc so natural-language requests become explicit action steps."
    ),
)
def houdini_ops_catalog() -> dict[str, Any]:
    return get_op_catalog()


@mcp.tool(
    title="Plan build (adhoc)",
    description=(
        "Planner (open): build a Plan from a JSON array of atomic ops. "
        "Each element must be an object with 'op' (see houdini_ops_catalog) and op-specific fields. "
        "Then runtime_execute_plan_id(plan_id) or runtime_dry_run_plan_id. "
        "Optional intent/recipe_tag are for logging only. Optional session_context_json is stored on plan.session_context."
    ),
)
def plan_build_adhoc(
    actions_json: Annotated[
        str,
        Field(
            description='JSON array, e.g. [{"op":"parm.set","node_path":"/obj/geo1/box1","parm_name":"size","value":2}]'
        ),
    ],
    recipe_tag: Annotated[str | None, Field(description="Short label stored as plan.recipe_id (default adhoc)")] = None,
    intent: Annotated[str | None, Field(description="Optional human/NL summary for traceability")] = None,
    session_context_json: Annotated[
        str,
        Field(description='Optional JSON object string (hip path, frame, selection, etc.) stored as plan.session_context'),
    ] = "{}",
    estimated_risk: str = "low",
    required_confirm: bool = False,
    rollback_strategy: str = "undo_group",
) -> dict[str, Any]:
    try:
        raw = json.loads(actions_json or "[]")
        if not isinstance(raw, list):
            return {"ok": False, "error": "actions_json must decode to a JSON array"}
        ctx_obj = json.loads(session_context_json or "{}")
        if not isinstance(ctx_obj, dict):
            return {"ok": False, "error": "session_context_json must decode to a JSON object"}
        plan = build_adhoc_plan(
            raw,
            recipe_tag=recipe_tag,
            intent=intent,
            session_context=ctx_obj if ctx_obj else None,
            estimated_risk=estimated_risk,
            required_confirm=required_confirm,
            rollback_strategy=rollback_strategy,
        )
        remember_plan(plan)
        return {"ok": True, "plan": plan}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@mcp.tool(
    title="Plan build and execute (adhoc)",
    description=(
        "Open plan: build actions_json into a Plan and execute immediately in Houdini. "
        "Response omits the full plan body; use returned plan_id with logs if needed."
    ),
)
def plan_build_adhoc_execute(
    actions_json: Annotated[str, Field(description="JSON array of {op, ...} steps (see houdini_ops_catalog)")],
    recipe_tag: Annotated[str | None, Field(description="Stored as plan.recipe_id")] = None,
    intent: Annotated[str | None, Field(description="Optional NL summary")] = None,
    session_context_json: Annotated[str, Field(description="Optional JSON object stored as plan.session_context")] = "{}",
    estimated_risk: str = "low",
    required_confirm: bool = False,
    rollback_strategy: str = "undo_group",
) -> dict[str, Any]:
    try:
        raw = json.loads(actions_json or "[]")
        if not isinstance(raw, list):
            return {"ok": False, "error": "actions_json must decode to a JSON array", "plan_id": None, "run_id": None}
        ctx_obj = json.loads(session_context_json or "{}")
        if not isinstance(ctx_obj, dict):
            return {"ok": False, "error": "session_context_json must decode to a JSON object", "plan_id": None, "run_id": None}
        plan = build_adhoc_plan(
            raw,
            recipe_tag=recipe_tag,
            intent=intent,
            session_context=ctx_obj if ctx_obj else None,
            estimated_risk=estimated_risk,
            required_confirm=required_confirm,
            rollback_strategy=rollback_strategy,
        )
        remember_plan(plan)
        out = execute(plan)
        return {
            "ok": bool(out.get("ok")),
            "plan_id": plan.get("plan_id"),
            "recipe_id": plan.get("recipe_id"),
            **out,
        }
    except Exception as e:
        return {"ok": False, "error": str(e), "plan_id": None, "run_id": None}


def _resolve_plan_for_runtime(
    plan: dict[str, Any] | None,
    plan_id: str | None,
) -> tuple[dict[str, Any] | None, str | None]:
    """Return (plan_dict, error_message). If ``plan_id`` is set, cache wins over ``plan``."""

    pid = (plan_id or "").strip()
    if pid:
        cached = get_plan(pid)
        if cached is None:
            return (
                None,
                "Unknown or expired plan_id (MCP restarted or cache evicted). Call plan_build_adhoc again.",
            )
        return cached, None
    if isinstance(plan, dict):
        return plan, None
    return None, "Provide either plan (full dict) or plan_id from plan_build_adhoc (same MCP session)."


@mcp.tool(
    title="Runtime dry-run",
    description=(
        "Runtime: dry-run a Plan (preview steps via receiver; no scene commits). "
        "Prefer plan_id from plan_build_adhoc to avoid huge JSON in tool arguments."
    ),
)
def runtime_dry_run(
    plan: Annotated[Optional[dict[str, Any]], Field(default=None, description="Full plan; omit if plan_id set")] = None,
    plan_id: Annotated[Optional[str], Field(default=None, description="plan_id returned inside plan_build_adhoc plan")] = None,
) -> dict[str, Any]:
    resolved, err = _resolve_plan_for_runtime(plan, plan_id)
    if err or resolved is None:
        return {"ok": False, "error": err or "No plan"}
    d = dry_run(resolved)
    return {"ok": bool(d.get("ok")), **d}


@mcp.tool(
    title="Runtime execute",
    description=(
        "Runtime: execute a Plan via batch.execute on the Houdini receiver (undo_group). "
        "Prefer plan_id from plan_build_adhoc (same session) so you do not embed the full plan JSON."
    ),
)
def runtime_execute(
    plan: Annotated[Optional[dict[str, Any]], Field(default=None, description="Full plan; omit if plan_id set")] = None,
    plan_id: Annotated[Optional[str], Field(default=None, description="plan_id from plan_build_adhoc → plan.plan_id")] = None,
) -> dict[str, Any]:
    resolved, err = _resolve_plan_for_runtime(plan, plan_id)
    if err or resolved is None:
        return {"ok": False, "error": err or "No plan", "run_id": None, "preflight": None, "result": None}
    out = execute(resolved)
    return {"ok": bool(out.get("ok")), **out}


@mcp.tool(
    title="Runtime execute by plan_id",
    description=(
        "Execute a Plan using only plan_id from plan_build_adhoc (same MCP session). "
        "Use this when runtime_execute rejects omitting plan — avoids huge JSON and temp files."
    ),
)
def runtime_execute_plan_id(
    plan_id: Annotated[str, Field(description="Exact plan_id from plan_build_adhoc → plan.plan_id")],
) -> dict[str, Any]:
    resolved, err = _resolve_plan_for_runtime(None, plan_id)
    if err or resolved is None:
        return {"ok": False, "error": err or "No plan", "run_id": None, "preflight": None, "result": None}
    out = execute(resolved)
    return {"ok": bool(out.get("ok")), **out}


@mcp.tool(
    title="Runtime dry-run by plan_id",
    description="Dry-run a Plan using only plan_id from plan_build_adhoc (same MCP session).",
)
def runtime_dry_run_plan_id(
    plan_id: Annotated[str, Field(description="Exact plan_id from plan_build_adhoc → plan.plan_id")],
) -> dict[str, Any]:
    resolved, err = _resolve_plan_for_runtime(None, plan_id)
    if err or resolved is None:
        return {"ok": False, "error": err or "No plan"}
    d = dry_run(resolved)
    return {"ok": bool(d.get("ok")), **d}


@mcp.tool(
    title="Runtime get logs",
    description="Runtime: fetch structured logs for a run_id returned by runtime_execute",
)
def runtime_get_logs(run_id: str) -> dict[str, Any]:
    logs = get_logs(run_id)
    if logs is None:
        return {"ok": False, "error": f"Unknown run_id: {run_id!r}"}
    return {"ok": True, "logs": logs}


@mcp.tool(
    title="Houdini undo once",
    description=(
        "Escape hatch: ask Houdini to perform a single undo (prefer relying on batch undo_group). "
        "Use when a failed batch left the scene in an inconsistent state."
    ),
)
def houdini_undo_once() -> dict[str, Any]:
    r = rollback_last_remote()
    return {"ok": r.ok, "result": r.to_dict()}


if __name__ == "__main__":
    mcp.run(transport="stdio")
