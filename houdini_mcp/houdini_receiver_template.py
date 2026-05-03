"""Houdini in-process TCP receiver (load inside Houdini / hython).

Run from a Houdini Python shell (see README pattern): listens for JSON lines and executes
``health.ping``, ``core.dispatch``, and ``batch.execute`` using ``hou``.

This file is the **Core implementation surface** for atomic ops and ``core.dispatch``; the MCP server builds plans and calls the bridge.
"""

from __future__ import annotations

import json
import socket
import threading
import traceback
import uuid
from typing import Any, Callable

RECEIVER_VERSION = "HOUDINI_MCP_SPRINT2_P74"

HOST = "127.0.0.1"
PORT = 63556


def _result(ok: bool, data: Any = None, warnings: list | None = None, errors: list | None = None) -> dict[str, Any]:
    return {
        "ok": ok,
        "data": data,
        "warnings": list(warnings or []),
        "errors": list(errors or []),
    }


def _wire_response(request_id: str, inner: dict[str, Any], ok: bool | None = None) -> dict[str, Any]:
    if ok is None:
        ok = bool(inner.get("ok"))
    err = None
    if not ok:
        err = {"code": "EXECUTION_ERROR", "message": "; ".join(inner.get("errors") or ["Unknown error"])}
    return {"request_id": request_id, "ok": ok, "result": inner, "error": err}


def _resolve_parm_for_set(n: Any, parm_name: str, *, dry_run: bool) -> tuple[Any | None, str, list[str]]:
    """Return (parm_or_None, resolved_token, alias_warnings). Handles Resample legacy ``segsize`` → H20 ``length``."""
    warnings_local: list[str] = []
    name = str(parm_name)
    try:
        is_resample = n.type().name() == "resample"
    except Exception:
        is_resample = False

    if is_resample and name == "segsize":
        if not dry_run:
            for toggle in ("dolength", "dosegsize"):
                t = n.parm(toggle)
                if t is not None:
                    try:
                        t.set(1)
                    except Exception:
                        pass
                    break
        for cand in ("segsize", "length", "doclength"):
            p = n.parm(cand)
            if p is not None:
                if cand != "segsize":
                    warnings_local.append(
                        f"Parm alias (resample): `{name}` → `{cand}` for Houdini 20+ compatibility"
                    )
                return p, cand, warnings_local
        return None, name, warnings_local

    try:
        is_clip = n.type().name() == "clip"
    except Exception:
        is_clip = False

    if is_clip:
        clip_aliases: dict[str, tuple[str, ...]] = {
            "dist": ("dist", "distance"),
            "distance": ("distance", "dist"),
            "dirx": ("dirx", "directionx"),
            "diry": ("diry", "directiony"),
            "dirz": ("dirz", "directionz"),
            # Keep 侧：UI 叫 Keep，HDK/老档里多为 clipop；Clip 2.0 可能仍用 clipop
            "keep": ("clipop", "keep", "clipop1"),
            "clipop": ("clipop", "keep", "clipop1"),
        }
        if name in clip_aliases:
            for cand in clip_aliases[name]:
                p = n.parm(cand)
                if p is not None:
                    if cand != name:
                        warnings_local.append(f"Parm alias (clip): `{name}` → `{cand}`")
                    return p, cand, warnings_local

    try:
        is_vellumsolver = n.type().name() == "vellumsolver"
    except Exception:
        is_vellumsolver = False

    if is_vellumsolver:
        wind_aliases: dict[str, tuple[str, ...]] = {
            "builtin_wind_x": ("builtinwindx", "windx", "builtin_wind_x"),
            "builtin_wind_y": ("builtinwindy", "windy", "builtin_wind_y"),
            "builtin_wind_z": ("builtinwindz", "windz", "builtin_wind_z"),
            "builtin_wind_speed": ("builtinwindspeed", "windspeed", "builtin_wind_speed"),
            "builtin_wind_drag": ("builtinwinddrag", "winddrag", "builtin_wind_drag"),
            "builtin_wind_gust": ("builtinwindgust", "windgust", "builtin_wind_gust"),
            "builtin_wind_turbulence": ("builtinwindturbulence", "windturbulence", "builtin_wind_turbulence"),
        }
        if name in wind_aliases:
            for cand in wind_aliases[name]:
                p = n.parm(cand)
                if p is not None:
                    if cand != name:
                        warnings_local.append(f"Parm alias (vellumsolver): `{name}` → `{cand}`")
                    return p, cand, warnings_local

    try:
        is_vellumconstraints = n.type().name() == "vellumconstraints"
    except Exception:
        is_vellumconstraints = False

    if is_vellumconstraints:
        cloth_aliases: dict[str, tuple[str, ...]] = {
            "cloth_stretch_stiffness": ("stretchstiffness", "stiffness", "cloth_stretch_stiffness"),
            "cloth_bend_stiffness": ("bendstiffness", "bendstiff", "cloth_bend_stiffness"),
            "cloth_damping_ratio": (
                "stretchdampingratio",
                "benddampingratio",
                "dampingratio",
                "damping",
                "cloth_damping_ratio",
            ),
        }
        if name in cloth_aliases:
            for cand in cloth_aliases[name]:
                p = n.parm(cand)
                if p is not None:
                    if cand != name:
                        warnings_local.append(f"Parm alias (vellumconstraints): `{name}` → `{cand}`")
                    return p, cand, warnings_local

    p = n.parm(name)
    return p, name, warnings_local


def _grid_primitive_type_menu_label(n: Any) -> str | None:
    """Return lowercased Grid SOP ``type`` menu label, or None."""
    try:
        p = n.parm("type")
        if p is None:
            return None
        items = tuple(p.menuItems())
        idx = int(p.eval())
        if 0 <= idx < len(items):
            return str(items[idx]).strip().lower()
    except Exception:
        pass
    return None


def _fix_grid_if_1x1_polygon_degenerate(n: Any, warnings: list[str]) -> None:
    """Raise Grid ``rows``/``cols`` from 1×1 to 2×2 when it would be a degenerate polygon mesh.

    A 1×1 polygon Grid has no real quad footprint; templates wired into Copy to Points often vanish in the
    viewport. Skip when the primitive type is **Points** (legitimate single-point grids).
    """
    try:
        if n.type().name() != "grid":
            return
        label = _grid_primitive_type_menu_label(n)
        # Menu label is typically ``Points`` (single-point grids); do not match ``Polygon`` / ``Polygon Soup``.
        if label == "points":
            return
        pr, pc = n.parm("rows"), n.parm("cols")
        if pr is None or pc is None:
            return
        r, c = int(pr.eval()), int(pc.eval())
        if r <= 1 and c <= 1:
            pr.set(2)
            pc.set(2)
            warnings.append(
                f"grid {n.path()}: rows×cols was {r}×{c}; raised to 2×2 — "
                "1×1 polygon grids are degenerate (e.g. invisible Copy to Points leaf cards)."
            )
    except Exception:
        pass


def _expr_language(hou_mod: Any, lang: str) -> Any:
    s = (lang or "hscript").strip().lower()
    if s in ("python", "py"):
        return hou_mod.exprLanguage.Python
    return hou_mod.exprLanguage.Hscript


def _to_bool(v: Any, default: bool = False) -> bool:
    if v is None:
        return default
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return bool(int(v))
    s = str(v).strip().lower()
    if s in ("1", "true", "yes", "on"):
        return True
    if s in ("0", "false", "no", "off", ""):
        return False
    return bool(v)


def _prep_exclusive_sop_flags(n: Any, *, clear_display: bool, clear_render: bool) -> dict[str, Any]:
    """Before clearing display/render on ``n``, activate those flags on another SOP sibling.

    Houdini OBJ/SOP networks keep at least one node with display (and commonly render) enabled;
    clearing without transferring can leave flags unchanged on ``n``.
    """
    import hou  # type: ignore

    out: dict[str, Any] = {}
    parent = n.parent()
    if parent is None:
        return out
    others = [c for c in parent.children() if c is not n]
    if not others:
        return out
    pick = others[0]
    if clear_display and hasattr(pick, "setDisplayFlag"):
        try:
            pick.setDisplayFlag(True)
            out["display_shifted_to"] = pick.path()
        except hou.OperationFailed:
            pass
    if clear_render and hasattr(pick, "setRenderFlag"):
        try:
            pick.setRenderFlag(True)
            out["render_shifted_to"] = pick.path()
        except hou.OperationFailed:
            pass
    return out


def _parent_network_path(node_path: str) -> str | None:
    t = (node_path or "").strip().rstrip("/")
    if not t or "/" not in t:
        return None
    return t.rsplit("/", 1)[0]


def _flatten_parm_templates(tmpl: Any) -> list[Any]:
    """Yield leaf parm templates from a template tree (folders recurse)."""

    import hou  # type: ignore

    out: list[Any] = []
    try:
        if isinstance(tmpl, hou.FolderParmTemplate):
            for c in tmpl.parmTemplates():
                out.extend(_flatten_parm_templates(c))
        elif getattr(hou, "MultiparmParmTemplate", None) is not None and isinstance(
            tmpl, hou.MultiparmParmTemplate
        ):
            for c in tmpl.parmTemplates():
                out.extend(_flatten_parm_templates(c))
        else:
            out.append(tmpl)
    except Exception:
        pass
    return out


def _parm_template_names(n: Any) -> list[str]:
    """Parameter names from parm templates (instance layout first, then node-type defaults)."""

    names: list[str] = []
    seen_groups: set[int] = set()
    ptg_candidates: list[Any] = []
    try:
        if hasattr(n, "parmTemplateGroup"):
            ptg_candidates.append(n.parmTemplateGroup())
    except Exception:
        pass
    try:
        ptg_candidates.append(n.type().parmTemplateGroup())
    except Exception:
        pass

    for ptg in ptg_candidates:
        if ptg is None:
            continue
        gid = id(ptg)
        if gid in seen_groups:
            continue
        seen_groups.add(gid)
        try:
            for t in ptg.parmTemplates():
                for leaf in _flatten_parm_templates(t):
                    nm = getattr(leaf, "name", None)
                    if nm:
                        names.append(str(nm))
        except Exception:
            continue
    return names


def _parm_names_from_inputs(n: Any) -> list[str]:
    """Best-effort names for wired inputs (Merge etc.) when template enumeration is empty."""

    names: list[str] = []
    try:
        n_inputs = int(n.inputs.__len__())  # type: ignore[arg-type]
    except Exception:
        try:
            n_inputs = len(n.inputs())
        except Exception:
            n_inputs = 0
    for i in range(max(0, n_inputs)):
        names.append(f"input{i}")

    return names


def _collect_layout_parent_paths(action: dict[str, Any]) -> set[str]:
    """Parent networks that may need layout after ``action``."""

    out: set[str] = set()
    op = str(action.get("op") or "")

    if op == "node.create":
        pp = str(action.get("parent_path") or "").strip()
        if pp:
            out.add(pp)
    elif op == "mcp.ctrl_null_setup":
        pp = str(action.get("parent_path") or "").strip()
        if pp:
            out.add(pp)
    elif op in ("node.connect", "node.disconnect"):
        for k in ("src", "dst"):
            p = _parent_network_path(str(action.get(k) or ""))
            if p:
                out.add(p)
    elif op in (
        "node.delete",
        "node.rename",
        "node.duplicate",
        "node.set_position",
        "node.set_comment",
        "node.set_color",
        "node.bypass",
        "node.lock",
        "node.set_selectable",
        "node.set_flag",
        "node.setup_vellum_ctrl",
        "node.setup_vellum_collisions",
    ):
        p = _parent_network_path(
            str(action.get("node_path") or action.get("ctrl_node_path") or action.get("geo_path") or "")
        )
        if p:
            out.add(p)
    elif op in ("parm.set", "parm.set_batch", "parm.set_expression", "parm.revert_defaults"):
        p = _parent_network_path(str(action.get("node_path") or ""))
        if p:
            out.add(p)
    elif op == "graph.layout_children":
        pp = str(action.get("parent_path") or action.get("path") or "").strip()
        if pp:
            out.add(pp)

    return out


def _layout_networks_after_batch(actions: list[dict[str, Any]]) -> tuple[list[str], list[str], list[str]]:
    """Return (layout_ok_paths, layout_error_messages, skipped_parent_paths).

    If the batch includes ``node.set_position``, auto-layout would overwrite manual positions,
    so we skip layout for those parent networks only.
    """

    import hou  # type: ignore

    parents: set[str] = set()
    skip_parents: set[str] = set()
    hip_mutating = False
    for a in actions:
        if isinstance(a, dict):
            parents |= _collect_layout_parent_paths(a)
            op = str(a.get("op") or "")
            if op == "node.set_position":
                sp = _parent_network_path(str(a.get("node_path") or ""))
                if sp:
                    skip_parents.add(sp)
            if op in ("hip.load", "hip.merge", "hip.new"):
                hip_mutating = True

    if hip_mutating:
        for root in ("/obj", "/out", "/mat", "/stage", "/lop"):
            try:
                rn = hou.node(root)
                if rn is not None and hasattr(rn, "layoutChildren"):
                    parents.add(root)
            except Exception:
                continue

    ok_paths: list[str] = []
    errs: list[str] = []
    skipped: list[str] = []
    for pp in sorted(parents):
        if not pp or pp == "/":
            continue
        if pp in skip_parents:
            skipped.append(pp)
            continue
        n = hou.node(pp)
        if n is None:
            errs.append(f"layout: parent not found {pp!r}")
            continue
        try:
            n.layoutChildren()
            ok_paths.append(pp)
        except Exception as e:
            errs.append(f"layout {pp!r}: {e}")
    return ok_paths, errs, skipped


def _coerce_parm_value(parm: Any, value: Any) -> Any:
    try:
        import hou  # type: ignore

        if hasattr(parm, "tuple"):
            return value
        vt = parm.parmTemplate().type()
        if vt == hou.parmTemplateType.Menu:
            if isinstance(value, bool):
                return int(value)
            if isinstance(value, (int, float)):
                return int(value)
            s = str(value).strip()
            if s.isdigit() or (s.startswith("-") and s[1:].isdigit()):
                return int(s)
            return s
        if vt == hou.parmTemplateType.Int:
            return int(value)
        if vt == hou.parmTemplateType.Float:
            return float(value)
        if vt == hou.parmTemplateType.Toggle:
            return bool(int(value)) if str(value).isdigit() else bool(value)
        return str(value)
    except Exception:
        return value


def _first_matching_parm(node: Any, names: tuple[str, ...]) -> tuple[Any | None, str | None]:
    """Return (parm, token) for the first existing parm name on ``node``."""
    for nm in names:
        try:
            p = node.parm(nm)
        except Exception:
            p = None
        if p is not None:
            return p, nm
    return None, None


def _camphor_tree_subnet_add_controls(sn: Any) -> dict[str, Any]:
    """Spare parms on subnet: shape + polygon budget (樟树 stylized L-system)."""
    import hou  # type: ignore

    added: dict[str, Any] = {}
    ptg = sn.parmTemplateGroup()

    def _add(tmpl: Any) -> None:
        nonlocal ptg
        nm = tmpl.name()
        if ptg.find(nm) is None:
            ptg.append(tmpl)
            added[nm] = tmpl.label()

    _add(hou.IntParmTemplate("gen_depth", "Generations (depth)", 1, default_value=(3,)))
    _add(hou.FloatParmTemplate("branch_angle", "Branch angle", 1, default_value=(28.0,)))
    _add(hou.FloatParmTemplate("trunk_scale", "Trunk / step scale", 1, default_value=(1.0,)))
    _add(hou.FloatParmTemplate("poly_keep", "Keep polygons (0–1)", 1, default_value=(0.42,)))
    sn.setParmTemplateGroup(ptg)
    return added


def _camphor_tree_configure_lsystem_rules(lsys: Any) -> list[str]:
    """Broad-crown premise + rule (approx. rounded canopy); tolerate parm name differences across builds."""
    import hou  # type: ignore

    warns: list[str] = []
    premise = "FFFA"
    rule_txt = 'A= " [&FFFA] //// [&FFFA] //// [&FFFA] //// [&FFFA] "'
    p_pre, tok = _first_matching_parm(lsys, ("premise", "prem_string", "prem"))
    if p_pre is not None:
        try:
            p_pre.set(premise)
        except Exception as e:
            warns.append(f"premise set failed ({tok}): {e}")
    else:
        warns.append("lsystem: no premise parm found; using node defaults")

    hit = False
    for nm in ("rule1", "rule2", "rule3"):
        try:
            rp = lsys.parm(nm)
        except Exception:
            rp = None
        if rp is not None:
            try:
                rp.set(rule_txt)
                hit = True
                break
            except Exception as e:
                warns.append(f"{nm} set failed: {e}")
    if not hit:
        try:
            for pr in lsys.parms():
                try:
                    tmpl = pr.parmTemplate()
                    if tmpl.type() != hou.parmTemplateType.String:
                        continue
                    low = pr.name().lower()
                    if low.startswith("rule"):
                        pr.set(rule_txt)
                        hit = True
                        break
                except Exception:
                    continue
        except Exception:
            pass
    if not hit:
        warns.append("lsystem: could not set branching rule; tree may use factory defaults")

    return warns


def _camphor_tree_parm_expr(node: Any, parm_names: tuple[str, ...], expr: str) -> str | None:
    import hou  # type: ignore

    p, tok = _first_matching_parm(node, parm_names)
    if p is None:
        return None
    try:
        p.setExpression(expr, hou.exprLanguage.Hscript)
        return tok
    except Exception:
        try:
            p.setExpression(expr)
            return tok
        except Exception:
            return None


def _camphor_tree_connect_subnet_chain(sn: Any, lsys: Any, redu: Any | None, outn: Any) -> None:
    if redu is not None:
        redu.setInput(0, lsys, 0)
        outn.setInput(0, redu, 0)
    else:
        outn.setInput(0, lsys, 0)


def _procedural_tree_foliage_subnet_add_controls(sn: Any) -> dict[str, Any]:
    """Spare parms on subnet: L-system, branch mesh, scatter density, leaf instance attrs."""
    import hou  # type: ignore

    added: dict[str, Any] = {}
    ptg = sn.parmTemplateGroup()

    def _add(tmpl: Any) -> None:
        nonlocal ptg
        nm = tmpl.name()
        if ptg.find(nm) is None:
            ptg.append(tmpl)
            added[nm] = tmpl.label()

    _add(hou.IntParmTemplate("shape_seed", "Shape / scatter seed", 1, default_value=(13,)))
    _add(hou.IntParmTemplate("gen_depth", "Generations (depth)", 1, default_value=(4,)))
    _add(hou.FloatParmTemplate("branch_angle", "Branch angle (deg)", 1, default_value=(26.0,)))
    _add(hou.FloatParmTemplate("crown_spread", "Crown spread (angle ×)", 1, default_value=(1.05,)))
    _add(hou.FloatParmTemplate("trunk_scale", "Step / trunk length scale", 1, default_value=(1.0,)))
    _add(hou.FloatParmTemplate("lsys_random", "L-system random scale mix", 1, default_value=(0.18,)))
    _add(hou.FloatParmTemplate("resample_len", "Branch resample segment length", 1, default_value=(0.08,)))
    _add(hou.FloatParmTemplate("wire_radius", "Branch thickness", 1, default_value=(0.035,)))
    _add(hou.IntParmTemplate("wire_rings", "PolyWire cross-section divisions", 1, default_value=(6,)))
    _add(hou.FloatParmTemplate("scatter_density", "Leaf surface density", 1, default_value=(2.8,)))
    _add(hou.FloatParmTemplate("leaf_scale", "Leaf size", 1, default_value=(0.11,)))
    _add(hou.FloatParmTemplate("leaf_scale_var", "Leaf size variation", 1, default_value=(0.35,)))
    _add(hou.FloatParmTemplate("leaf_spin", "Leaf spin variation (deg)", 1, default_value=(72.0,)))
    rule_default = 'A= " [&FFFA] //// [&FFFA] //// [&FFFA] //// [&FFFA] "'
    _add(hou.StringParmTemplate("lsys_premise", "L-system premise", 1, default_value=("FFFA",)))
    _add(hou.StringParmTemplate("lsys_rule", "L-system rule", 1, default_value=(rule_default,)))
    sn.setParmTemplateGroup(ptg)
    return added


def _procedural_tree_foliage_orient_vex() -> str:
    return """vector n = normalize(v@N);
matrix3 aln = dihedral({0,0,1}, n);
float sp = radians(rand(@ptnum * 12 + chi("../shape_seed")) * ch("../leaf_spin"));
vector4 qspin = quaternion(sp, n);
p@orient = qmultiply(qspin, quaternion(aln));
f@pscale = ch("../leaf_scale") * (1.0 + fit01(rand(@ptnum + chi("../shape_seed")), -1, 1) * ch("../leaf_scale_var"));
"""


def _procedural_tree_foliage_bind_lsystem(lsys: Any) -> tuple[list[str], dict[str, Any]]:
    """Expression-bind L-system parms to subnet controls; return warnings + binding tokens."""
    import hou  # type: ignore

    warns: list[str] = []
    out: dict[str, Any] = {}
    expr_lang = hou.exprLanguage.Hscript

    def _sexpr(parm: Any, expr: str) -> bool:
        try:
            parm.setExpression(expr, expr_lang)
            return True
        except Exception:
            try:
                parm.setExpression(expr)
                return True
            except Exception:
                return False

    p_pre, tok = _first_matching_parm(lsys, ("premise", "prem_string", "prem"))
    if p_pre is not None:
        if not _sexpr(p_pre, 'chs("../lsys_premise")'):
            warns.append("lsystem: premise expression not applied")
        out["premise_parm"] = tok
    else:
        warns.append("lsystem: no premise parm")

    hit = False
    for nm in ("rule1", "rule2", "rule3"):
        try:
            rp = lsys.parm(nm)
        except Exception:
            rp = None
        if rp is not None and _sexpr(rp, 'chs("../lsys_rule")'):
            hit = True
            out["rule_parm"] = nm
            break
    if not hit:
        try:
            for pr in lsys.parms():
                try:
                    tmpl = pr.parmTemplate()
                    if tmpl.type() != hou.parmTemplateType.String:
                        continue
                    low = pr.name().lower()
                    if low.startswith("rule") and _sexpr(pr, 'chs("../lsys_rule")'):
                        hit = True
                        out["rule_parm"] = pr.name()
                        break
                except Exception:
                    continue
        except Exception:
            pass
    if not hit:
        warns.append("lsystem: could not bind rule string parm")

    gen_w = _camphor_tree_parm_expr(lsys, ("generations", "gense", "gen"), 'ch("../gen_depth")')
    ang_w = _camphor_tree_parm_expr(
        lsys,
        ("angle", "angles"),
        'ch("../branch_angle") * ch("../crown_spread")',
    )
    step_w = _camphor_tree_parm_expr(
        lsys,
        ("stepsize", "step", "length", "step_size"),
        '0.13 * ch("../trunk_scale")',
    )
    rand_w = _camphor_tree_parm_expr(
        lsys,
        ("randscale", "randomscale", "doscale", "scalevariance"),
        'ch("../lsys_random")',
    )
    out["generations_parm"] = gen_w
    out["angle_parm"] = ang_w
    out["step_parm"] = step_w
    out["randscale_parm"] = rand_w
    if gen_w is None:
        warns.append("lsystem: generations not wired")
    if ang_w is None:
        warns.append("lsystem: angle not wired")
    if step_w is None:
        warns.append("lsystem: step size not wired")
    if rand_w is None:
        warns.append("lsystem: random scale not wired (optional)")
    return warns, out


def _procedural_tree_foliage_try_create_node(sn: Any, type_names: tuple[str, ...], node_name: str) -> Any:
    import hou  # type: ignore

    last_err: str | None = None
    for nt in type_names:
        try:
            return sn.createNode(nt, node_name)
        except hou.OperationFailed as e:
            last_err = str(e)
            continue
    raise hou.OperationFailed(last_err or f"could not create any of {type_names!r}")


def _procedural_tree_foliage_enable_copytopoints_orient(ctp: Any) -> str | None:
    """Turn on 'Transform Using Point Orientations' (or equivalent) when we can detect it."""
    import hou  # type: ignore

    try:
        for pr in ctp.parms():
            try:
                tmpl = pr.parmTemplate()
                if tmpl.type() != hou.parmTemplateType.Toggle:
                    continue
                lab = (tmpl.label() or "").lower()
                if "transform using point orientation" in lab:
                    pr.set(1)
                    return pr.name()
            except Exception:
                continue
    except Exception:
        pass
    p, tok = _first_matching_parm(
        ctp,
        ("usepointorient", "useptorient", "doxformpoints", "usefullxform", "fullxform"),
    )
    if p is not None:
        try:
            p.set(1)
            return tok
        except Exception:
            return None
    return None


def _procedural_tree_foliage_build_subnet(
    geo: Any,
    subnet_name: str,
    replace_existing: bool,
    auto_layout: bool,
) -> dict[str, Any]:
    """Build L-system + polywire + scatter + copy-to-points tree inside a GEO object."""
    import hou  # type: ignore

    if replace_existing:
        ex = geo.node(subnet_name)
        if ex is not None:
            ex.destroy()
    elif geo.node(subnet_name) is not None:
        return {"ok": False, "errors": [f"{geo.path()}/{subnet_name} already exists"], "warnings": [], "data": {}}

    sn = geo.createNode("subnet", subnet_name)
    try:
        sn.setComment(
            "树形总控: 分枝深度/角度/冠幅、步长与随机、枝干粗细与分段、叶面密度、"
            "单叶尺寸与旋转；L-system premise/rule 可改写整体轮廓。"
        )
    except Exception:
        pass

    ctr = _procedural_tree_foliage_subnet_add_controls(sn)
    lsys = sn.createNode("lsystem", "lsystem1")
    resample = sn.createNode("resample", "branch_resample")
    resample.setInput(0, lsys, 0)
    polywire = _procedural_tree_foliage_try_create_node(sn, ("polywire", "polywire::2.0"), "branch_mesh")
    polywire.setInput(0, resample, 0)
    scatter = _procedural_tree_foliage_try_create_node(sn, ("scatter::2.0", "scatter"), "foliage_scatter")
    scatter.setInput(0, polywire, 0)
    orientwrangle = _procedural_tree_foliage_try_create_node(
        sn,
        ("attribwrangle::2.0", "attribwrangle"),
        "leaf_instance_attrs",
    )
    orientwrangle.setInput(0, scatter, 0)
    leafgrid = sn.createNode("grid", "leaf_grid")
    ctp = _procedural_tree_foliage_try_create_node(sn, ("copytopoints::2.0", "copytopoints"), "copy_to_points1")
    merger = sn.createNode("merge", "merge_branches_leaves")
    outn = sn.createNode("null", "OUT")

    ctp.setInput(0, leafgrid, 0)
    ctp.setInput(1, orientwrangle, 0)
    merger.setInput(0, polywire, 0)
    merger.setInput(1, ctp, 0)
    outn.setInput(0, merger, 0)

    try:
        leafgrid.parm("rows").set(1)
        leafgrid.parm("cols").set(1)
    except Exception:
        pass
    szp, _ = _first_matching_parm(leafgrid, ("size", "sizex"))
    if szp is not None:
        try:
            szp.set(1.0)
        except Exception:
            pass

    lw_lsys, bind = _procedural_tree_foliage_bind_lsystem(lsys)

    dolen, _ = _first_matching_parm(resample, ("dolength", "usemaxlength"))
    if dolen is not None:
        try:
            dolen.set(1)
        except Exception:
            pass
    seglen, _sltok = _first_matching_parm(resample, ("length", "seglength", "maxseglength"))
    if seglen is not None:
        try:
            seglen.setExpression('ch("../resample_len")', hou.exprLanguage.Hscript)
        except Exception:
            try:
                seglen.setExpression('ch("../resample_len")')
            except Exception:
                pass

    rad, rdtok = _first_matching_parm(polywire, ("radius", "rad", "width"))
    if rad is not None:
        try:
            rad.setExpression('ch("../wire_radius")', hou.exprLanguage.Hscript)
        except Exception:
            try:
                rad.setExpression('ch("../wire_radius")')
            except Exception:
                pass
    rings, rgtok = _first_matching_parm(polywire, ("cols", "rings", "divrows", "crosssections"))
    if rings is not None:
        try:
            rings.setExpression('ch("../wire_rings")', hou.exprLanguage.Hscript)
        except Exception:
            try:
                rings.setExpression('ch("../wire_rings")')
            except Exception:
                pass

    dens, _ = _first_matching_parm(scatter, ("density", "scatterdensity", "pointdensity"))
    if dens is not None:
        try:
            dens.setExpression('ch("../scatter_density")', hou.exprLanguage.Hscript)
        except Exception:
            try:
                dens.setExpression('ch("../scatter_density")')
            except Exception:
                pass
    seedp, _ = _first_matching_parm(scatter, ("seed", "randseed"))
    if seedp is not None:
        try:
            seedp.setExpression('ch("../shape_seed")', hou.exprLanguage.Hscript)
        except Exception:
            try:
                seedp.setExpression('ch("../shape_seed")')
            except Exception:
                pass

    orient_toggle = _procedural_tree_foliage_enable_copytopoints_orient(ctp)

    p_snip, tok = _wrangle_snippet_parm(orientwrangle)
    if p_snip is None:
        sn.destroy()
        return {
            "ok": False,
            "errors": ["leaf_instance_attrs: no VEX snippet parm"],
            "warnings": lw_lsys,
            "data": {},
        }
    vex_txt = _procedural_tree_foliage_orient_vex()
    p_snip.set(vex_txt)
    _sync_wrangle_snippet_aliases(orientwrangle, vex_txt, tok or "snippet")
    try:
        orientwrangle.parm("class").set(0)
    except Exception:
        pass

    for nm, val in (
        ("shape_seed", 13),
        ("gen_depth", 4),
        ("branch_angle", 26.0),
        ("crown_spread", 1.05),
        ("trunk_scale", 1.0),
        ("lsys_random", 0.18),
        ("resample_len", 0.08),
        ("wire_radius", 0.035),
        ("wire_rings", 6),
        ("scatter_density", 2.8),
        ("leaf_scale", 0.11),
        ("leaf_scale_var", 0.35),
        ("leaf_spin", 72.0),
    ):
        try:
            sn.parm(nm).set(val)
        except Exception:
            pass

    try:
        outn.setDisplayFlag(True)
        outn.setRenderFlag(True)
    except Exception:
        try:
            outn.setDisplayFlag(True)
        except Exception:
            pass

    if auto_layout:
        try:
            geo.layoutChildren()
            sn.layoutChildren()
        except Exception:
            pass

    warns: list[str] = [*lw_lsys]
    if orient_toggle:
        warns.append(f"copytopoints: enabled orient toggle {orient_toggle!r}")
    cook_warns: list[str] = []
    try:
        outn.cook(force=True)
    except Exception as e:
        cook_warns.append(f"OUT cook: {e}")

    prim_count = None
    pt_count = None
    try:
        g = outn.geometry()
        if g is not None:
            prim_count = len(g.prims())
            pt_count = len(g.points())
    except Exception:
        pass

    return {
        "ok": True,
        "errors": [],
        "warnings": warns + cook_warns,
        "data": {
            "parent_geo_path": geo.path(),
            "subnet_path": sn.path(),
            "lsystem_path": lsys.path(),
            "output_null_path": outn.path(),
            "spare_parms_added": ctr,
            "lsystem_bindings": bind,
            "copytopoints_orient_parm": orient_toggle,
            "display_prim_count": prim_count,
            "display_point_count": pt_count,
        },
    }


def _wrangle_snippet_parm(node: Any) -> tuple[Any | None, str | None]:
    """Pick the VEX/snippet parm that the wrangle node actually evaluates.

    Some builds expose both ``snippet`` and ``snippet1``. If we always prefer ``snippet1`` even when it is
    empty, MCP may write ``snippet`` while Houdini evaluates the empty ``snippet1`` (symptom: no attribute
    changes despite UI showing code).
    """

    def _nonempty_len(parm: Any) -> int:
        for attr in ("unexpandedString", "rawValue", "evalAsString"):
            fn = getattr(parm, attr, None)
            if not callable(fn):
                continue
            try:
                s = str(fn()).strip()
                return len(s)
            except Exception:
                continue
        try:
            return len(str(parm.eval()).strip())
        except Exception:
            return 0

    cands: list[tuple[str, Any]] = []
    for nm in ("snippet1", "snippet", "vex_snippet", "snippet_code"):
        try:
            p = node.parm(nm)
        except Exception:
            p = None
        if p is not None:
            cands.append((nm, p))

    best_nm: str | None = None
    best_p: Any | None = None
    best_len = -1
    for nm, p in cands:
        ln = _nonempty_len(p)
        if ln > best_len:
            best_len = ln
            best_nm = nm
            best_p = p

    if best_p is not None and best_len > 0:
        return best_p, best_nm

    # Fall back to presence-only ordering when all are empty.
    try:
        tlow = node.type().name().lower()
    except Exception:
        tlow = ""
    order = ("snippet1", "snippet", "vex_snippet", "snippet_code")
    if "::2." not in tlow and "2.0" not in tlow:
        order = ("snippet", "snippet1", "vex_snippet", "snippet_code")
    for nm in order:
        try:
            p = node.parm(nm)
        except Exception:
            p = None
        if p is not None:
            return p, nm
    return None, None


def _sync_wrangle_snippet_aliases(node: Any, text: str, primary_token: str) -> None:
    """Best-effort: keep sibling snippet parms in sync when a node exposes duplicates."""
    aliases = ("snippet1", "snippet", "vex_snippet", "snippet_code")
    for nm in aliases:
        if nm == primary_token:
            continue
        try:
            op = node.parm(nm)
        except Exception:
            op = None
        if op is None:
            continue
        try:
            op.set(text)
        except Exception:
            continue


def _walk_parm_templates(pt: Any) -> list[Any]:
    out: list[Any] = []
    try:
        subs = pt.parmTemplates()
    except Exception:
        subs = ()
    for ch in subs or ():
        out.append(ch)
        out.extend(_walk_parm_templates(ch))
    return out


def _is_wrangle_like_sop(node: Any) -> bool:
    try:
        name = (node.type().name() or "").lower()
    except Exception:
        return False
    return "wrangle" in name


def _all_button_parm_tokens(node: Any) -> list[str]:
    """Button parm tokens on ``node`` (template walk order); used for diagnostics and last-resort press."""
    import hou  # type: ignore

    try:
        ptg = node.parmTemplateGroup()
    except Exception:
        return []
    out: list[str] = []
    seen: set[str] = set()
    for tmpl in _walk_parm_templates(ptg):
        try:
            if tmpl.type() != hou.parmTemplateType.Button:
                continue
            tok = tmpl.name()
            if not tok or tok in seen:
                continue
            seen.add(tok)
            out.append(tok)
        except Exception:
            continue
    return out


def _wrangle_compile_button_candidates(node: Any) -> list[tuple[str, str]]:
    """Return [(parm_token, label_lower), ...] for button parms that look like compile/reload."""
    import hou  # type: ignore

    try:
        ptg = node.parmTemplateGroup()
    except Exception:
        return []
    toks: list[tuple[str, str]] = []
    seen: set[str] = set()
    for tmpl in _walk_parm_templates(ptg):
        try:
            if tmpl.type() != hou.parmTemplateType.Button:
                continue
            tok = tmpl.name()
            if not tok or tok in seen:
                continue
            label = ""
            try:
                label = str(tmpl.label() or "")
            except Exception:
                label = ""
            low = (tok + " " + label).lower()
            if not any(
                k in low
                for k in (
                    "reload",
                    "recompile",
                    "compile",
                    "update",
                    "refresh",
                    "flush",
                    "vex",
                    "snippet",
                    "shader",
                    "code",
                )
            ):
                continue
            seen.add(tok)
            toks.append((tok, low))
        except Exception:
            continue
    return toks


def _wrangle_press_best_compile_button(node: Any) -> tuple[str | None, list[str]]:
    """Try fixed tokens first, then scan parm templates for button parms.

    Returns ``(pressed_token_or_none, ordered_tokens_tried)``.
    """
    fixed = (
        "reload",
        "recompile",
        "compile",
        "reloadsnippet",
        "reloadsnippet1",
        "flush",
        "update",
        "forcereload",
        "reloadscript",
        "reloadvex",
        "compilevex",
        "compilebutton",
    )
    tried: list[str] = []
    for bn in fixed:
        tried.append(bn)
        p = node.parm(bn)
        if p is not None and hasattr(p, "pressButton"):
            try:
                p.pressButton()
                return bn, tried
            except Exception:
                continue
    ranked: list[tuple[int, str]] = []
    for tok, low in _wrangle_compile_button_candidates(node):
        score = 0
        if "recompile" in low:
            score += 6
        if "compile" in low:
            score += 5
        if "reload" in low:
            score += 4
        if "refresh" in low:
            score += 3
        if "update" in low:
            score += 2
        if "flush" in low:
            score += 1
        if "vex" in low:
            score += 3
        if "snippet" in low:
            score += 2
        ranked.append((score, tok))
    ranked.sort(key=lambda x: (-x[0], x[1]))
    for _sc, tok in ranked:
        if tok not in tried:
            tried.append(tok)
        p = node.parm(tok)
        if p is not None and hasattr(p, "pressButton"):
            try:
                p.pressButton()
                return tok, tried
            except Exception:
                continue
    if _is_wrangle_like_sop(node):
        for tok in _all_button_parm_tokens(node):
            if tok in tried:
                continue
            tried.append(tok)
            p = node.parm(tok)
            if p is not None and hasattr(p, "pressButton"):
                try:
                    p.pressButton()
                    return tok, tried
                except Exception:
                    continue
    return None, tried


def _wrangle_force_compile(node: Any) -> str | None:
    """Press a compile/reload button on wrangle-like SOPs when present."""
    tok, _ = _wrangle_press_best_compile_button(node)
    return tok


def _menu_parm_pick_keyword(p: Any, keyword: str) -> bool:
    """Set an ordered-menu parm by matching ``keyword`` against menu labels (substring / prefix)."""
    kw = keyword.strip().lower()
    if not kw:
        return False
    try:
        labs = p.menuLabels()
    except Exception:
        return False
    for i, lab in enumerate(labs):
        ll = lab.lower().replace(" ", "")
        if kw in ll or ll.startswith(kw):
            try:
                p.set(i)
                return True
            except Exception:
                continue
    return False


def _wrangle_set_run_over_menu(node: Any, keyword: str) -> tuple[bool, str | None]:
    for pn in ("class", "type", "runover", "runclass", "attribclass"):
        p = node.parm(pn)
        if p is None:
            continue
        try:
            if not hasattr(p, "menuLabels"):
                continue
        except Exception:
            continue
        if _menu_parm_pick_keyword(p, keyword):
            return True, pn
    return False, None


def _wrangle_set_group_type_menu(node: Any, keyword: str) -> tuple[bool, str | None]:
    """Set Attribute Wrangle \"Group Type\" / binding class menu when present."""
    for pn in ("grouptype", "group_type", "bindgrouptype", "bindclass"):
        p = node.parm(pn)
        if p is None:
            continue
        try:
            if not hasattr(p, "menuLabels"):
                continue
        except Exception:
            continue
        if _menu_parm_pick_keyword(p, keyword):
            return True, pn
    return False, None


def _serialize_geo_component(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, (str, bytes)):
        return v.decode("utf-8", errors="replace") if isinstance(v, bytes) else v
    if isinstance(v, (int, float, bool)):
        return v
    try:
        return [float(x) for x in v]
    except Exception:
        try:
            return str(v)
        except Exception:
            return None


def _prim_collect_intrinsics(pr: Any, max_keys: int, *, keys_only: bool = False) -> tuple[Any, bool]:
    """Return (intrinsic dict or list of names, truncated).

    ``max_keys`` caps intrinsic *names* per primitive (use 0 for cap 4096).
    """
    names_fn = getattr(pr, "intrinsicNames", None)
    if not callable(names_fn):
        return ({}, False) if not keys_only else ([], False)
    try:
        names = list(names_fn())
    except Exception:
        return ({}, False) if not keys_only else ([], False)
    lim = max_keys if max_keys > 0 else 4096
    truncated = len(names) > lim
    names_use = names[:lim]
    if keys_only:
        return names_use, truncated
    data: dict[str, Any] = {}
    for nm in names_use:
        try:
            data[nm] = _serialize_geo_component(pr.intrinsicValue(nm))
        except Exception:
            data[nm] = None
    return data, truncated


def _bbox_to_dict(bb: Any) -> dict[str, Any]:
    try:
        mn = bb.minvec()
        mx = bb.maxvec()
        return {
            "min": [float(mn.x()), float(mn.y()), float(mn.z())],
            "max": [float(mx.x()), float(mx.y()), float(mx.z())],
        }
    except Exception:
        return {}


def _bbox_dict_to_world_aabb(container: Any, local_bd: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Promote a local AABB (dict min/max) into a world-space axis-aligned bbox.

    Returns ``(bbox_dict_or_None, diagnostic_or_None)``. Several HOM variants exist across builds;
    we try BoundingBox helpers first, then fall back to transforming eight corners with ``p * M``.
    """
    errs: list[str] = []
    try:
        import hou  # type: ignore

        mn = local_bd.get("min")
        mx = local_bd.get("max")
        if not isinstance(mn, list) or not isinstance(mx, list) or len(mn) < 3 or len(mx) < 3:
            return None, "local bbox min/max missing"

        wm_fn = getattr(container, "worldTransform", None)
        if not callable(wm_fn):
            return None, "container has no worldTransform()"
        wm = wm_fn()

        # Preferred: hou.BoundingBox supports multiply/transform helpers on many builds.
        try:
            lb = hou.BoundingBox()
            lb.setMin(hou.Vector3(float(mn[0]), float(mn[1]), float(mn[2])))
            lb.setMax(hou.Vector3(float(mx[0]), float(mx[1]), float(mx[2])))
        except Exception as e:
            errs.append(f"BoundingBox.setup: {e}")
            lb = None

        if lb is not None:
            for label, call in (
                ("BoundingBox.transformed(matrix)", getattr(lb, "transformed", None)),
                ("BoundingBox * matrix", lambda: lb * wm),  # type: ignore[misc]
                ("matrix * BoundingBox", lambda: wm * lb),  # type: ignore[misc]
            ):
                if label.startswith("BoundingBox.transformed") and not callable(call):
                    continue
                try:
                    wb = call(wm) if label.startswith("BoundingBox.transformed") else call()
                    out = _bbox_to_dict(wb)
                    if out:
                        return out, None
                except Exception as e:
                    errs.append(f"{label}: {e}")

        # Fallback: transform eight corners (HOM uses row-vector convention: p * M).
        xs: list[float] = []
        ys: list[float] = []
        zs: list[float] = []
        for i in (0, 1):
            for j in (0, 1):
                for k in (0, 1):
                    x = float(mn[0] if i == 0 else mx[0])
                    y = float(mn[1] if j == 0 else mx[1])
                    z = float(mn[2] if k == 0 else mx[2])
                    try:
                        p = hou.Vector3(x, y, z) * wm
                        xs.append(float(p.x()))
                        ys.append(float(p.y()))
                        zs.append(float(p.z()))
                    except Exception as e:
                        errs.append(f"corner_transform: {e}")
        if xs and ys and zs:
            return (
                {"min": [min(xs), min(ys), min(zs)], "max": [max(xs), max(ys), max(zs)]},
                None,
            )

        tail = "; ".join(errs[-4:]) if errs else "unknown"
        return None, tail
    except Exception as e:
        return None, str(e)


def _iter_descendant_nodes(root: Any) -> list[Any]:
    out: list[Any] = []
    try:
        stack = list(root.children())
    except Exception:
        return out
    while stack:
        n = stack.pop()
        out.append(n)
        try:
            stack.extend(n.children())
        except Exception:
            pass
    return out


def _resolve_obj_display_sop(container: Any) -> tuple[Any | None, str]:
    dn_fn = getattr(container, "displayNode", None)
    if callable(dn_fn):
        try:
            dn = dn_fn()
            if dn is not None:
                return dn, "displayNode()"
        except Exception:
            pass
    for nd in _iter_descendant_nodes(container):
        try:
            if hasattr(nd, "isDisplayFlagSet") and nd.isDisplayFlagSet():
                return nd, "scan_display_flag"
        except Exception:
            continue
    return None, ""


def _resolve_obj_render_sop(container: Any) -> tuple[Any | None, str]:
    rn_fn = getattr(container, "renderNode", None)
    if callable(rn_fn):
        try:
            rn = rn_fn()
            if rn is not None:
                return rn, "renderNode()"
        except Exception:
            pass
    for nd in _iter_descendant_nodes(container):
        try:
            if hasattr(nd, "isRenderFlagSet") and nd.isRenderFlagSet():
                return nd, "scan_render_flag"
        except Exception:
            continue
    return None, ""


def _obj_bbox_payload(container: Any, *, force_cook_display: bool) -> tuple[dict[str, Any] | None, str, list[str]]:
    """Return (bbox_dict, source_tag, warnings)."""
    lw: list[str] = []
    gbb = getattr(container, "geometryBoundingBox", None)
    if callable(gbb):
        try:
            bb = gbb()
            bd = _bbox_to_dict(bb)
            if bd:
                return bd, "geometryBoundingBox", lw
        except Exception as e:
            lw.append(f"geometryBoundingBox failed: {e}")
    dn, _how = _resolve_obj_display_sop(container)
    if dn is None:
        return None, "none", lw + ["no display SOP resolved"]
    try:
        if force_cook_display:
            dn.cook(force=True)
        g = dn.geometry()
        if g is None:
            return None, "none", lw + ["display SOP has no geometry"]
        bb = g.boundingBox()
        bd = _bbox_to_dict(bb)
        wbd, diag = _bbox_dict_to_world_aabb(container, bd)
        if wbd:
            return wbd, "display_sop_boundingBox_xform_obj_world", lw
        msg = "bbox: could not promote display SOP local bbox to world space"
        if diag:
            msg = f"{msg}: {diag}"
        lw.append(msg)
        return bd, "display_sop_boundingBox_local", lw
    except Exception as e:
        return None, "none", lw + [str(e)]


def _coerce_xyz_vector(val: Any) -> list[float] | None:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        f = float(val)
        return [f, f, f]
    if isinstance(val, (list, tuple)):
        if len(val) >= 3:
            return [float(val[0]), float(val[1]), float(val[2])]
        if len(val) == 1:
            f = float(val[0])
            return [f, f, f]
    return None


def _read_vec3_parm_tuple(node: Any, candidates: tuple[str, ...]) -> tuple[list[float] | None, str | None]:
    for c in candidates:
        try:
            pt = node.parmTuple(c)
        except Exception:
            pt = None
        if pt is None:
            continue
        try:
            if len(pt) >= 3:
                return [float(pt[i].eval()) for i in range(3)], c
        except Exception:
            continue
    return None, None


def _set_vec3_parm_tuple(node: Any, candidates: tuple[str, ...], vals: list[float]) -> str | None:
    for c in candidates:
        try:
            pt = node.parmTuple(c)
        except Exception:
            pt = None
        if pt is None:
            continue
        try:
            if len(pt) >= 3:
                for i in range(3):
                    pt[i].set(vals[i])
                return c
        except Exception:
            continue
    return None


def _matrix4_to_list16(m: Any) -> list[float]:
    try:
        t = m.asTupleOfTuples()
        if t and len(t) == 4:
            out = [float(t[i][j]) for i in range(4) for j in range(4)]
            if len(out) == 16:
                return out
    except Exception:
        pass
    try:
        return [float(m[i, j]) for i in range(4) for j in range(4)]
    except Exception:
        return []


def _world_matrix_payload(hou: Any, n: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    try:
        wm = n.worldTransform()
    except Exception as e:
        return {"error": str(e)}
    data["world_matrix"] = _matrix4_to_list16(wm)
    try:
        tr = wm.extractTranslates()
        data["translate"] = [float(tr.x()), float(tr.y()), float(tr.z())]
    except Exception:
        pass
    try:
        rr = wm.extractRotates()
        data["rotate"] = [float(rr.x()), float(rr.y()), float(rr.z())]
    except Exception:
        pass
    try:
        ss = wm.extractScales()
        data["scale"] = [float(ss.x()), float(ss.y()), float(ss.z())]
    except Exception:
        pass
    return data


def _local_matrix_payload(hou: Any, n: Any) -> dict[str, Any]:
    data: dict[str, Any] = {}
    try:
        lm = n.localTransform()
    except Exception as e:
        return {"error": str(e)}
    data["local_matrix"] = _matrix4_to_list16(lm)
    try:
        tr = lm.extractTranslates()
        data["translate"] = [float(tr.x()), float(tr.y()), float(tr.z())]
    except Exception:
        pass
    try:
        rr = lm.extractRotates()
        data["rotate"] = [float(rr.x()), float(rr.y()), float(rr.z())]
    except Exception:
        pass
    try:
        ss = lm.extractScales()
        data["scale"] = [float(ss.x()), float(ss.y()), float(ss.z())]
    except Exception:
        pass
    return data


def _desktop_context_payload(hou: Any) -> dict[str, Any]:
    """Best-effort UI context (fails quietly in headless hython)."""
    out: dict[str, Any] = {}
    ui = getattr(hou, "ui", None)
    if ui is None:
        out["hou_ui_available"] = False
        return out
    out["hou_ui_available"] = True
    try:
        desk = ui.curDesktop()
        try:
            out["desktop_name"] = desk.name()
        except Exception:
            out["desktop_name"] = None
        net_tab = None
        for pt in desk.paneTabs():
            try:
                if pt.type() == hou.paneTabType.NetworkEditor:
                    net_tab = pt
                    break
            except Exception:
                continue
        if net_tab is not None:
            try:
                pwd = net_tab.pwd()
                out["network_pwd_path"] = pwd.path() if pwd is not None else None
            except Exception as e:
                out["network_pwd_error"] = str(e)
            try:
                cur = net_tab.currentNode()
                out["network_current_node_path"] = cur.path() if cur is not None else None
            except Exception:
                pass
    except Exception as e:
        out["desktop_error"] = str(e)
    return out


def _session_snapshot_payload(hou: Any, *, include_desktop: bool = False) -> dict[str, Any]:
    hp = ""
    try:
        hp = str(hou.hipFile.path())
    except Exception:
        pass
    unsaved = None
    try:
        unsaved = bool(hou.hipFile.hasUnsavedChanges())
    except Exception:
        pass
    start = end = None
    try:
        rng = hou.playbar.playbackRange()
        start = float(rng[0])
        end = float(rng[1])
    except Exception:
        pass
    sel: list[str] = []
    try:
        for sn in hou.selectedNodes():
            try:
                sel.append(sn.path())
            except Exception:
                continue
    except Exception:
        pass
    payload: dict[str, Any] = {
        "hip_path": hp,
        "has_unsaved_changes": unsaved,
        "frame": float(hou.frame()),
        "fps": float(hou.fps()),
        "playback_start": start,
        "playback_end": end,
        "selected_node_paths": sel,
    }
    if include_desktop:
        payload["desktop"] = _desktop_context_payload(hou)
    return payload


def _hscript_expr_float(hou: Any, expr: str) -> float | None:
    try:
        v = hou.hscriptExpression(expr)
        if v is None:
            return None
        return float(v)
    except Exception:
        return None


def _playback_globals_payload(hou: Any) -> dict[str, Any]:
    """Global playbar expressions ($RFSTART / $RFEND) vs current frame — sim range sanity."""
    out: dict[str, Any] = {}
    for key, ex in (
        ("RFSTART", "$RFSTART"),
        ("RFEND", "$RFEND"),
        ("FSTART", "$FSTART"),
        ("FEND", "$FEND"),
        ("FPS", "$FPS"),
    ):
        out[key] = _hscript_expr_float(hou, ex)
    try:
        cf = float(hou.frame())
    except Exception:
        cf = None
    out["current_frame"] = cf
    rs, re = out.get("RFSTART"), out.get("RFEND")
    covers = None
    if cf is not None and rs is not None and re is not None:
        lo, hi = (rs, re) if rs <= re else (re, rs)
        covers = bool(lo - 1e-6 <= cf <= hi + 1e-6)
    out["current_frame_inside_global_range"] = covers
    try:
        rng = hou.playbar.playbackRange()
        out["playback_range_tuple"] = [float(rng[0]), float(rng[1])]
    except Exception:
        out["playback_range_tuple"] = None
    return out


def _parm_samples_for_node(n: Any, max_parms: int) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    if max_parms <= 0:
        return out
    try:
        for p in n.parms():
            if len(out) >= max_parms:
                break
            try:
                if p.isHidden():
                    continue
            except Exception:
                pass
            try:
                tpl = p.parmTemplate()
                if tpl is not None and tpl.type() == hou.parmTemplateType.Folder:
                    continue
            except Exception:
                pass
            try:
                raw = p.evalAsString()
            except Exception:
                try:
                    raw = str(p.eval())
                except Exception:
                    raw = ""
            if len(raw) > 400:
                raw = raw[:400] + "…"
            try:
                lab = str(p.description() or p.name())
            except Exception:
                lab = p.name()
            out.append({"name": p.name(), "label": lab, "value": raw})
    except Exception:
        pass
    return out


def _node_diagnostics_compact(hou: Any, n: Any, *, force_cook: bool) -> dict[str, Any]:
    errs: list[str] = []
    wrns: list[str] = []
    if force_cook:
        try:
            n.cook(force=True)
        except Exception:
            pass
    try:
        er = n.errors()
        if er:
            errs.extend(str(x) for x in er)
    except Exception:
        pass
    try:
        wfn = getattr(n, "warnings", None)
        if callable(wfn):
            wv = wfn()
            if wv:
                wrns.extend(str(x) for x in wv)
    except Exception:
        pass
    info_err = None
    try:
        em = getattr(n, "errorsAsString", None)
        if callable(em):
            info_err = em()
    except Exception:
        pass
    data: dict[str, Any] = {
        "errors": errs[:64],
        "warnings": wrns[:48],
        "has_errors": bool(errs),
    }
    if info_err:
        data["errors_as_string"] = str(info_err)[:4000]
    return data


def _geo_topology_hint_for_display_sop(dn: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"display_sop_path": dn.path()}
    try:
        dn.cook(force=True)
    except Exception as e:
        out["cook_error"] = str(e)[:800]
        return out
    try:
        g = dn.geometry()
    except Exception as e:
        out["geometry_error"] = str(e)[:800]
        return out
    if g is None:
        out["geometry"] = None
        return out
    try:
        out["prim_count"] = int(len(g.prims()))
    except Exception:
        out["prim_count"] = None
    try:
        out["point_count"] = int(len(g.points()))
    except Exception:
        out["point_count"] = None
    packed = False
    try:
        for prim in g.prims()[:4096]:
            try:
                tname = prim.type().name()
            except Exception:
                continue
            if "Pack" in tname or tname in ("PackedGeometry", "PackedFragment"):
                packed = True
                break
    except Exception:
        pass
    out["has_packed_primitives_sampled"] = packed
    return out


def _template_numeric_bounds(hou: Any, tmpl: Any, component: int) -> tuple[float | None, float | None]:
    """Return (min, max) for float/int parm templates when exposed."""
    lo = hi = None
    try:
        if hasattr(tmpl, "min"):
            try:
                lo = float(tmpl.min(component))
            except Exception:
                try:
                    lo = float(tmpl.min())
                except Exception:
                    pass
        if hasattr(tmpl, "max"):
            try:
                hi = float(tmpl.max(component))
            except Exception:
                try:
                    hi = float(tmpl.max())
                except Exception:
                    pass
    except Exception:
        pass
    return lo, hi


def _validate_parm_range_core(hou: Any, n: Any, args: dict[str, Any]) -> dict[str, Any]:
    pname = str(args.get("parm_name") or "")
    val_raw = args.get("value")
    comp_raw = args.get("component")
    if not pname.strip() or val_raw is None:
        return {"ok": False, "errors": ["validate.parm_range requires parm_name and value"]}
    p = n.parm(pname)
    comp_idx = 0
    if p is None and hasattr(n, "parmTuple"):
        try:
            ptup = n.parmTuple(pname)
            if ptup is not None:
                comp_idx = int(comp_raw) if comp_raw is not None else 0
                comp_idx = max(0, min(comp_idx, len(ptup) - 1))
                p = ptup[comp_idx]
        except Exception:
            p = None
    if p is None:
        return {"ok": False, "errors": [f"Parm not found: {n.path()}.{pname}"]}
    tmpl = p.parmTemplate()
    vt = tmpl.type()
    detail: dict[str, Any] = {
        "node_path": n.path(),
        "parm_name": pname,
        "component": comp_idx,
        "template_type": type(tmpl).__name__,
    }
    checks_applied = False
    in_range = True
    lo: float | None = None
    hi: float | None = None
    coerced: Any = val_raw
    try:
        if vt == hou.parmTemplateType.Float:
            coerced = float(val_raw)
            lo, hi = _template_numeric_bounds(hou, tmpl, comp_idx)
            if lo is not None and coerced < lo:
                in_range = False
            if hi is not None and coerced > hi:
                in_range = False
            checks_applied = lo is not None or hi is not None
        elif vt == hou.parmTemplateType.Int:
            coerced = int(float(val_raw))
            lo, hi = _template_numeric_bounds(hou, tmpl, comp_idx)
            if lo is not None and coerced < int(lo):
                in_range = False
            if hi is not None and coerced > int(hi):
                in_range = False
            checks_applied = lo is not None or hi is not None
        elif vt == hou.parmTemplateType.Toggle:
            coerced = bool(int(val_raw)) if str(val_raw).isdigit() else bool(val_raw)
            checks_applied = True
        elif vt == hou.parmTemplateType.Menu:
            try:
                labels = p.menuLabels()
                nmenu = len(labels)
                if str(val_raw).isdigit():
                    coerced = int(val_raw)
                else:
                    lab = str(val_raw).strip()
                    coerced = -1
                    try:
                        coerced = labels.index(lab)
                    except ValueError:
                        for i, lb in enumerate(labels):
                            if lb.lower() == lab.lower():
                                coerced = i
                                break
                    if coerced < 0:
                        return {"ok": False, "errors": [f"validate.parm_range: unknown menu label {lab!r}"]}
                if coerced < 0 or coerced >= nmenu:
                    in_range = False
                checks_applied = True
            except Exception:
                coerced = val_raw
                checks_applied = False
        else:
            coerced = val_raw
            checks_applied = False
    except Exception as e:
        return {"ok": False, "errors": [f"validate.parm_range: could not coerce value: {e}"]}
    detail["value"] = coerced
    detail["min"] = lo
    detail["max"] = hi
    detail["in_range"] = in_range
    detail["checks_applied"] = checks_applied
    if not checks_applied:
        detail["note"] = "No strict min/max on this template type; value coerced only where applicable."
    return {"ok": True, "detail": detail, "in_range": in_range}


def _clear_session_caches_best_effort(hou: Any) -> tuple[bool, str, list[str]]:
    """Clear simulation/SOP/geo caches. Houdini 20+ builds often omit hou.clearAllCaches — use hscript fallbacks."""
    tried: list[str] = []
    fn = getattr(hou, "clearAllCaches", None)
    if callable(fn):
        try:
            fn()
            return True, "hou.clearAllCaches", tried
        except Exception as e:
            tried.append(f"hou.clearAllCaches: {e}")
    for cmd in ("sopcache -c", "geocache -c", "objcache -c", "opeventscriptcache -c"):
        try:
            hou.hscript(cmd)
            return True, f"hscript:{cmd}", tried
        except Exception as e:
            tried.append(f"hscript {cmd}: {e}")
    return False, "", tried


def _try_run_shelf_tool(hou: Any, tool_path: str) -> tuple[bool, str | None, list[str]]:
    errs: list[str] = []
    tp = tool_path.strip()
    if not tp:
        return False, None, ["empty tool path"]

    try:
        r = hou.hscript(f"toolrun {tp}")
        if isinstance(r, tuple) and len(r) >= 2 and str(r[1]).strip():
            errs.append(f"hscript stderr: {r[1]}")
        return True, "hscript:toolrun", errs
    except Exception as e:
        errs.append(f"hscript toolrun: {e}")

    ui = getattr(hou, "ui", None)
    if ui is not None:
        for meth in ("runShelfTool", "triggerShelfTool"):
            fn = getattr(ui, meth, None)
            if callable(fn):
                try:
                    fn(tp)
                    return True, f"hou.ui.{meth}", errs
                except Exception as e2:
                    errs.append(f"ui.{meth}: {e2}")

    shelves = getattr(hou, "shelves", None)
    if shelves is not None:
        st = getattr(shelves, "tool", None)
        if callable(st):
            try:
                tl = st(tp)
                if tl is not None:
                    run = getattr(tl, "run", None) or getattr(tl, "execute", None)
                    if callable(run):
                        run()
                        return True, "hou.shelves.tool(...).run", errs
            except Exception as e3:
                errs.append(f"hou.shelves.tool: {e3}")

    return False, None, errs


def _try_apply_node_preset(hou: Any, node: Any, preset_name: str) -> tuple[bool, str | None, list[str]]:
    errs: list[str] = []
    name = str(preset_name).strip()
    if not name:
        return False, None, ["empty preset name"]

    np_mod = getattr(hou, "nodePresets", None)
    if np_mod is not None:
        for fn_name in ("apply", "applyPreset", "load"):
            fn = getattr(np_mod, fn_name, None)
            if callable(fn):
                try:
                    fn(node, name)
                    return True, f"hou.nodePresets.{fn_name}", errs
                except TypeError:
                    try:
                        fn(name, node)
                        return True, f"hou.nodePresets.{fn_name}", errs
                    except Exception as e:
                        errs.append(f"nodePresets.{fn_name}: {e}")
                except Exception as e:
                    errs.append(f"nodePresets.{fn_name}: {e}")

    try:
        import nodePresets as _np  # type: ignore

        for fn_name in ("apply", "applyPreset", "load"):
            fn = getattr(_np, fn_name, None)
            if callable(fn):
                try:
                    fn(node, name)
                    return True, f"nodePresets.{fn_name}", errs
                except Exception as e:
                    errs.append(f"nodePresets module {fn_name}: {e}")
    except Exception:
        pass

    for meth in ("applyPreset", "loadPreset", "setPreset"):
        m = getattr(node, meth, None)
        if callable(m):
            try:
                m(name)
                return True, f"node.{meth}", errs
            except Exception as e:
                errs.append(f"node.{meth}: {e}")

    return False, None, errs


def _run_on_ui_thread(
    hou: Any, fn: Callable[[], None], *, timeout: float = 3.0
) -> tuple[bool, str | None, BaseException | None]:
    """Run ``fn`` on Houdini's UI thread; block until it runs.

    The TCP receiver handles requests on a background thread; playbar/viewport calls must
    usually run on the UI thread or they appear to succeed (API returns) without updating
    the interactive timeline.

    Returns ``(deferred_ok, deferred_error, fn_exception)``. If ``fn`` raises, ``deferred_ok``
    is True and the exception is the third element.
    """
    ui = getattr(hou, "ui", None)
    if ui is None:
        return False, "hou.ui not available", None
    ed = getattr(ui, "executeDeferred", None)
    add_cb = getattr(ui, "addEventLoopCallback", None)
    rm_cb = getattr(ui, "removeEventLoopCallback", None)
    if not callable(ed) and not (callable(add_cb) and callable(rm_cb)):
        return False, "no usable UI dispatcher (executeDeferred / event-loop callback)", None
    done = threading.Event()
    fn_exc: list[BaseException] = []
    dispatch_method = "executeDeferred" if callable(ed) else "addEventLoopCallback"

    def _wrapper() -> None:
        try:
            fn()
        except BaseException as e:
            fn_exc.append(e)
        finally:
            done.set()

    try:
        if callable(ed):
            ed(_wrapper)
        else:
            # One-shot event-loop callback fallback for Houdini builds without executeDeferred.
            cb_holder: dict[str, Callable[[], None]] = {}

            def _event_cb() -> None:
                try:
                    _wrapper()
                finally:
                    cb = cb_holder.get("cb")
                    if cb is not None:
                        try:
                            rm_cb(cb)  # type: ignore[misc]
                        except Exception:
                            pass

            cb_holder["cb"] = _event_cb
            add_cb(_event_cb)  # type: ignore[misc]
    except Exception as e:
        return False, f"{dispatch_method}: {e}", None
    if not done.wait(timeout=timeout):
        return (
            False,
            f"{dispatch_method} timed out after {timeout}s (is the Houdini UI/event loop running?)",
            None,
        )
    return True, None, fn_exc[0] if fn_exc else None


def _playback_apply_core(hou: Any, mode: str) -> dict[str, Any]:
    """Apply play / pause / stop on ``hou.playbar``; must run on UI thread for reliable playback."""
    pb = hou.playbar
    method: str | None = None
    pbm = getattr(hou, "playbarPlaybackMode", None)
    if pbm is not None and hasattr(pb, "setPlaybackMode"):
        if mode in ("play", "playing"):
            pb.setPlaybackMode(pbm.Play)
            method = "setPlaybackMode(Play)"
        elif mode in ("pause", "paused", "hold"):
            if hasattr(pbm, "Pause"):
                pb.setPlaybackMode(pbm.Pause)
                method = "setPlaybackMode(Pause)"
            else:
                pb.setPlaybackMode(pbm.Stop)
                method = "setPlaybackMode(Stop) [pause fallback]"
        elif mode in ("stop", "stopped", "rewind"):
            pb.setPlaybackMode(pbm.Stop)
            method = "setPlaybackMode(Stop)"
        else:
            raise ValueError(f"playback.set: unknown mode {mode!r}")
    else:
        if mode in ("play", "playing"):
            if not hasattr(pb, "play"):
                raise RuntimeError("playback.set: playbar.play not available in this Houdini build")
            pb.play()
            method = "play()"
        elif mode in ("pause", "stop", "stopped", "paused", "hold", "rewind"):
            if not hasattr(pb, "stop"):
                raise RuntimeError("playback.set: playbar.stop not available in this Houdini build")
            pb.stop()
            method = "stop()"
        else:
            raise ValueError(f"playback.set: unknown mode {mode!r}")
    playing: bool | None = None
    try:
        playing = bool(pb.isPlaying())
    except Exception:
        pass
    return {"method": method, "is_playing": playing}


def _top_try_get_pdg_node(n: Any) -> tuple[Any | None, str | None]:
    """Return (pdg_node_or_None, method_label). H20+ TOP uses getPDGNode(); older builds may use pdgNode()."""
    for label, fn in (
        ("getPDGNode()", getattr(n, "getPDGNode", None)),
        ("pdgNode()", getattr(n, "pdgNode", None)),
    ):
        if not callable(fn):
            continue
        try:
            pn = fn()
            return pn, label
        except Exception:
            continue
    return None, None


def _top_child_pdg_probe_row(ch: Any) -> dict[str, Any]:
    """Lightweight probe: TOP PDG accessors on a child node (H20+ getPDGNode vs legacy pdgNode)."""
    row: dict[str, Any] = {"node_path": ch.path(), "name": ch.name()}
    try:
        row["type_name"] = ch.type().nameWithCategory()
    except Exception:
        row["type_name"] = None
    gpdg = getattr(ch, "getPDGNode", None)
    row["get_pdg_node_callable"] = callable(gpdg)
    pdgn = getattr(ch, "pdgNode", None)
    row["pdg_node_callable"] = callable(pdgn)
    row["pdg_node_nonnull"] = None
    row["pdg_node_error"] = None
    row["pdg_access_method"] = None
    pn, how = _top_try_get_pdg_node(ch)
    if how:
        row["pdg_access_method"] = how
    if pn is not None:
        row["pdg_node_nonnull"] = True
    elif how:
        row["pdg_node_nonnull"] = False
    return row


def _top_workitems_child_hints(parent: Any, *, limit: int = 40) -> tuple[list[dict[str, Any]], list[str]]:
    """Return (child_probe_rows, suggested_paths_for_rescan)."""
    rows: list[dict[str, Any]] = []
    try:
        kids = list(parent.children())
    except Exception:
        return rows, []
    for ch in kids[: max(0, limit)]:
        rows.append(_top_child_pdg_probe_row(ch))
    suggested: list[str] = []
    for r in rows:
        if r.get("pdg_node_nonnull") is True:
            suggested.append(str(r["node_path"]))
    if not suggested:
        for r in rows:
            if r.get("get_pdg_node_callable"):
                tn = str(r.get("type_name") or "")
                if "localscheduler" in tn.lower():
                    continue
                suggested.append(str(r["node_path"]))
    if not suggested:
        for r in rows:
            if r.get("pdg_node_callable") or r.get("get_pdg_node_callable"):
                suggested.append(str(r["node_path"]))
    if not suggested:
        suggested = [str(r["node_path"]) for r in rows[:15]]
    return rows, suggested[:20]


def _mcp_viewport_autoframe_mode(args: dict[str, Any]) -> str:
    """Return off | auto | all | selected for viewport_autoframe."""
    va = args.get("viewport_autoframe", True)
    if va is False:
        return "off"
    if isinstance(va, str):
        s = va.strip().lower()
        if s in ("0", "false", "off", "no", "none"):
            return "off"
        if s in ("all", "selected", "auto"):
            return s
    return "auto"


def _mcp_bbox_from_node(hou: Any, n: Any) -> Any:
    """Cook-time bbox for a SOP or OBJ GEO display SOP; None if unavailable."""
    if n is None:
        return None
    try:
        if isinstance(n, hou.SopNode):
            g = n.geometry()
            if g is not None:
                return g.boundingBox()
        dn_get = getattr(n, "displayNode", None)
        if callable(dn_get):
            dn = n.displayNode()
            if dn is not None and isinstance(dn, hou.SopNode):
                g = dn.geometry()
                if g is not None:
                    return g.boundingBox()
    except Exception:
        return None
    return None


def _mcp_autoframe_sceneviewer(hou: Any, flip_tab: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Center SceneViewer on geometry before flipbook: bbox (explicit path / selection), else frameSelected, else frameAll."""
    meta: dict[str, Any] = {"mode": _mcp_viewport_autoframe_mode(args), "applied": False, "method": None}
    if meta["mode"] == "off":
        meta["method"] = "skipped"
        return meta
    try:
        vp = flip_tab.curViewport()
    except Exception as e:
        meta["error"] = str(e)
        return meta

    def _try_frame_bbox() -> str | None:
        fp = str(args.get("frame_node_path") or "").strip()
        if fp:
            n = hou.node(fp)
            bb = _mcp_bbox_from_node(hou, n)
            if bb is not None:
                try:
                    vp.frameBoundingBox(bb)
                    return "frame_node_path"
                except Exception as e:
                    meta["bbox_error"] = str(e)
        try:
            for n in hou.selectedNodes():
                bb = _mcp_bbox_from_node(hou, n)
                if bb is None:
                    continue
                try:
                    vp.frameBoundingBox(bb)
                    return "selected_nodes"
                except Exception as e:
                    meta["bbox_error"] = str(e)
                    continue
        except Exception:
            pass
        return None

    mode = meta["mode"]
    if mode == "all":
        try:
            vp.frameAll()
            meta.update({"applied": True, "method": "frameAll"})
        except Exception as e:
            meta["error"] = str(e)
        return meta
    if mode == "selected":
        fn = getattr(vp, "frameSelected", None) or getattr(vp, "frameSelection", None)
        if callable(fn):
            try:
                fn()
                meta.update({"applied": True, "method": "frameSelected"})
            except Exception as e:
                meta["error"] = str(e)
        return meta

    # auto
    m = _try_frame_bbox()
    if m:
        meta.update({"applied": True, "method": m})
        return meta
    fn = getattr(vp, "frameSelected", None) or getattr(vp, "frameSelection", None)
    if callable(fn):
        try:
            fn()
            meta.update({"applied": True, "method": "frameSelected"})
            return meta
        except Exception as e:
            meta["viewport_frame_error"] = str(e)
    try:
        vp.frameAll()
        meta.update({"applied": True, "method": "frameAll"})
    except Exception as e:
        meta["error"] = str(e)
    return meta


def _dispatch_core(op: str, args: dict[str, Any], *, dry_run: bool) -> dict[str, Any]:
    import hou  # type: ignore

    warnings: list[str] = []

    def require(cond: bool, msg: str) -> dict[str, Any] | None:
        if not cond:
            return _result(False, errors=[msg])
        return None

    if op == "node.create":
        parent_path = str(args.get("parent_path") or "")
        node_type = str(args.get("node_type") or "")
        node_name = args.get("node_name")
        auto_layout = bool(args.get("auto_layout", True))
        bad = require(bool(parent_path and node_type), "node.create requires parent_path and node_type")
        if bad:
            return bad
        if dry_run:
            return _result(
                True,
                data={"preview": f"create {node_type!r} under {parent_path!r} name={node_name!r} auto_layout={auto_layout}"},
            )
        parent = hou.node(parent_path)
        if parent is None:
            return _result(False, errors=[f"Parent not found: {parent_path}"])
        if node_name:
            try:
                clash = parent.node(str(node_name))
            except Exception:
                clash = None
            if clash is not None:
                return _result(
                    False,
                    errors=[
                        f"node.create: {parent_path}/{node_name} already exists. "
                        f"Delete or rename that node in Houdini, or use a new geo_name in the recipe inputs."
                    ],
                )
        try:
            n = parent.createNode(node_type, node_name) if node_name else parent.createNode(node_type)
            try:
                if str(node_type).strip().lower() == "grid":
                    _fix_grid_if_1x1_polygon_degenerate(n, warnings)
            except Exception:
                pass
            if auto_layout:
                try:
                    parent.layoutChildren()
                except Exception as le:
                    warnings.append(f"node.create auto_layout failed: {le}")
            if warnings:
                return _result(True, warnings=warnings, data={"node_path": n.path()})
            return _result(True, data={"node_path": n.path()})
        except hou.OperationFailed as e:
            return _result(False, errors=[str(e)])

    if op == "node.connect":
        src = str(args.get("src") or "")
        dst = str(args.get("dst") or "")
        so = int(args.get("src_output", 0))
        di = int(args.get("dst_input", 0))
        if dry_run:
            return _result(True, data={"preview": f"connect {src}:{so} -> {dst}:{di}"})
        sn = hou.node(src)
        dn = hou.node(dst)
        if sn is None or dn is None:
            return _result(False, errors=[f"connect missing node src={src!r} dst={dst!r}"])
        try:
            dn.setInput(di, sn, so)
            return _result(True, data={"dst": dst})
        except hou.OperationFailed as e:
            return _result(False, errors=[str(e)])

    if op == "node.set_flag":
        path = str(args.get("node_path") or "")
        if dry_run:
            return _result(True, data={"preview": f"set flags on {path}: {args!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        disp = args.get("display")
        rend = args.get("render")
        tmpl = args.get("template")
        try:
            clear_display = disp is not None and not _to_bool(disp)
            clear_render = rend is not None and not _to_bool(rend)
            xfer: dict[str, Any] = {}
            if clear_display or clear_render:
                xfer = _prep_exclusive_sop_flags(
                    n,
                    clear_display=clear_display,
                    clear_render=clear_render,
                )
            if disp is not None:
                n.setDisplayFlag(_to_bool(disp))
            if rend is not None:
                n.setRenderFlag(_to_bool(rend))
            if tmpl is not None:
                n.setTemplateFlag(_to_bool(tmpl))
            data = {
                "node_path": path,
                "display_flag": bool(n.isDisplayFlagSet()),
                "render_flag": bool(n.isRenderFlagSet()),
                "template_flag": bool(n.isTemplateFlagSet()),
            }
            if xfer:
                data["exclusive_flag_shift"] = xfer
            return _result(True, data=data)
        except hou.OperationFailed as e:
            return _result(False, errors=[str(e)])

    if op == "node.setup_vellum_ctrl":
        ctrl_path = str(args.get("ctrl_node_path") or "")
        pin_path = str(args.get("pin_node_path") or "")
        solver_path = str(args.get("solver_node_path") or "")
        constraints_path = str(args.get("constraints_node_path") or "")
        if dry_run:
            return _result(True, data={"preview": f"setup vellum ctrl {ctrl_path}"})

        ctrl = hou.node(ctrl_path)
        pin = hou.node(pin_path)
        solver = hou.node(solver_path)
        constraints = hou.node(constraints_path)
        if ctrl is None or pin is None or solver is None or constraints is None:
            return _result(
                False,
                errors=[
                    f"node.setup_vellum_ctrl missing node(s): ctrl={ctrl_path} pin={pin_path} solver={solver_path} constraints={constraints_path}"
                ],
            )

        try:
            # Build a clean, grouped spare-parameter UI on CTRL_vellum_cloth.
            # Keep core null parms untouched; only recreate our managed parms.
            managed = {
                "solver_substeps",
                "pin_edge_mode",
                "pin_band_width",
                "wind_dir_x",
                "wind_dir_y",
                "wind_dir_z",
                "wind_source_count",
                "wind2_dir_x",
                "wind2_dir_y",
                "wind2_dir_z",
                "wind3_dir_x",
                "wind3_dir_y",
                "wind3_dir_z",
                "wind4_dir_x",
                "wind4_dir_y",
                "wind4_dir_z",
                "wind_speed",
                "wind_drag",
                "wind_gust",
                "wind_turbulence",
                "cloth_stretch_stiffness",
                "cloth_bend_stiffness",
                "cloth_damping_ratio",
            }

            ptg = ctrl.parmTemplateGroup()

            # Remove old managed parms first to avoid duplicate/flat clutter after updates.
            for nm in managed:
                try:
                    if ptg.find(nm) is not None:
                        ptg.remove(nm)
                except Exception:
                    pass

            solver_substeps_t = hou.IntParmTemplate("solver_substeps", "Solver Substeps", 1, default_value=(2,))
            pin_mode_t = hou.StringParmTemplate("pin_edge_mode", "Pin Edge Mode", 1, default_value=("left",))
            pin_bw_t = hou.FloatParmTemplate("pin_band_width", "Pin Band Width", 1, default_value=(0.0,))

            wind_dir_group = hou.FolderParmTemplate(
                "wind_direction_group",
                "Wind Direction",
                [
                    hou.FloatParmTemplate("wind_dir_x", "Wind Dir X", 1, default_value=(1.0,)),
                    hou.FloatParmTemplate("wind_dir_y", "Wind Dir Y", 1, default_value=(0.0,)),
                    hou.FloatParmTemplate("wind_dir_z", "Wind Dir Z", 1, default_value=(0.0,)),
                    hou.IntParmTemplate("wind_source_count", "Wind Source Count", 1, default_value=(1,)),
                    hou.FloatParmTemplate("wind2_dir_x", "Wind2 Dir X", 1, default_value=(0.0,)),
                    hou.FloatParmTemplate("wind2_dir_y", "Wind2 Dir Y", 1, default_value=(0.0,)),
                    hou.FloatParmTemplate("wind2_dir_z", "Wind2 Dir Z", 1, default_value=(0.0,)),
                    hou.FloatParmTemplate("wind3_dir_x", "Wind3 Dir X", 1, default_value=(0.0,)),
                    hou.FloatParmTemplate("wind3_dir_y", "Wind3 Dir Y", 1, default_value=(0.0,)),
                    hou.FloatParmTemplate("wind3_dir_z", "Wind3 Dir Z", 1, default_value=(0.0,)),
                    hou.FloatParmTemplate("wind4_dir_x", "Wind4 Dir X", 1, default_value=(0.0,)),
                    hou.FloatParmTemplate("wind4_dir_y", "Wind4 Dir Y", 1, default_value=(0.0,)),
                    hou.FloatParmTemplate("wind4_dir_z", "Wind4 Dir Z", 1, default_value=(0.0,)),
                ],
                folder_type=hou.folderType.Collapsible,
            )
            wind_power_group = hou.FolderParmTemplate(
                "wind_power_group",
                "Wind Power",
                [
                    hou.FloatParmTemplate("wind_speed", "Wind Speed", 1, default_value=(4.0,)),
                    hou.FloatParmTemplate("wind_drag", "Wind Drag", 1, default_value=(1.0,)),
                    hou.FloatParmTemplate("wind_gust", "Wind Gust", 1, default_value=(0.3,)),
                    hou.FloatParmTemplate("wind_turbulence", "Wind Turbulence", 1, default_value=(0.2,)),
                ],
                folder_type=hou.folderType.Collapsible,
            )
            wind_setting_folder = hou.FolderParmTemplate(
                "wind_setting",
                "Wind Setting",
                [wind_dir_group, wind_power_group],
                folder_type=hou.folderType.Simple,
            )

            cloth_setting_folder = hou.FolderParmTemplate(
                "cloth_setting",
                "Cloth Setting",
                [
                    hou.FloatParmTemplate("cloth_stretch_stiffness", "Cloth Stretch Stiffness", 1, default_value=(1.0,)),
                    hou.FloatParmTemplate("cloth_bend_stiffness", "Cloth Bend Stiffness", 1, default_value=(0.05,)),
                    hou.FloatParmTemplate("cloth_damping_ratio", "Cloth Damping Ratio", 1, default_value=(0.05,)),
                ],
                folder_type=hou.folderType.Simple,
            )

            ptg.append(solver_substeps_t)
            ptg.append(hou.FolderParmTemplate("pin_setting", "Pin Setting", [pin_mode_t, pin_bw_t], folder_type=hou.folderType.Simple))
            ptg.append(wind_setting_folder)
            ptg.append(cloth_setting_folder)
            ctrl.setParmTemplateGroup(ptg)

            def set_hscript_expr(path: str, parm_name: str, expr: str) -> None:
                n = hou.node(path)
                if n is None:
                    return
                p, _, _ = _resolve_parm_for_set(n, parm_name, dry_run=False)
                if p is None:
                    return
                p.setExpression(expr, language=hou.exprLanguage.Hscript)

            set_hscript_expr(solver_path, "substeps", f'ch("{ctrl_path}/solver_substeps")')
            # Use turbulence/gust through expressions on stable built-in wind parms,
            # so the effect works even if solver has no dedicated gust/turbulence parms.
            sum_x = (
                f'ch("{ctrl_path}/wind_dir_x")'
                f' + if(ch("{ctrl_path}/wind_source_count")>=2, ch("{ctrl_path}/wind2_dir_x"), 0)'
                f' + if(ch("{ctrl_path}/wind_source_count")>=3, ch("{ctrl_path}/wind3_dir_x"), 0)'
                f' + if(ch("{ctrl_path}/wind_source_count")>=4, ch("{ctrl_path}/wind4_dir_x"), 0)'
            )
            sum_y = (
                f'ch("{ctrl_path}/wind_dir_y")'
                f' + if(ch("{ctrl_path}/wind_source_count")>=2, ch("{ctrl_path}/wind2_dir_y"), 0)'
                f' + if(ch("{ctrl_path}/wind_source_count")>=3, ch("{ctrl_path}/wind3_dir_y"), 0)'
                f' + if(ch("{ctrl_path}/wind_source_count")>=4, ch("{ctrl_path}/wind4_dir_y"), 0)'
            )
            sum_z = (
                f'ch("{ctrl_path}/wind_dir_z")'
                f' + if(ch("{ctrl_path}/wind_source_count")>=2, ch("{ctrl_path}/wind2_dir_z"), 0)'
                f' + if(ch("{ctrl_path}/wind_source_count")>=3, ch("{ctrl_path}/wind3_dir_z"), 0)'
                f' + if(ch("{ctrl_path}/wind_source_count")>=4, ch("{ctrl_path}/wind4_dir_z"), 0)'
            )
            set_hscript_expr(
                solver_path,
                "builtin_wind_x",
                f'({sum_x}) + ch("{ctrl_path}/wind_turbulence") * 0.12 * sin($T*4.21)',
            )
            set_hscript_expr(
                solver_path,
                "builtin_wind_y",
                f'({sum_y}) + ch("{ctrl_path}/wind_turbulence") * 0.08 * cos($T*3.73)',
            )
            set_hscript_expr(
                solver_path,
                "builtin_wind_z",
                f'({sum_z}) + ch("{ctrl_path}/wind_turbulence") * 0.12 * cos($T*5.17)',
            )
            set_hscript_expr(
                solver_path,
                "builtin_wind_speed",
                f'ch("{ctrl_path}/wind_speed") * (1 + ch("{ctrl_path}/wind_gust") * 0.35 * abs(sin($T*2.67)))',
            )
            set_hscript_expr(solver_path, "builtin_wind_drag", f'ch("{ctrl_path}/wind_drag")')

            set_hscript_expr(constraints_path, "cloth_stretch_stiffness", f'ch("{ctrl_path}/cloth_stretch_stiffness")')
            set_hscript_expr(constraints_path, "cloth_bend_stiffness", f'ch("{ctrl_path}/cloth_bend_stiffness")')
            # Damping may exist as stretch/bend split or unified parm; bind all available.
            set_hscript_expr(constraints_path, "stretchdampingratio", f'ch("{ctrl_path}/cloth_damping_ratio")')
            set_hscript_expr(constraints_path, "benddampingratio", f'ch("{ctrl_path}/cloth_damping_ratio")')
            set_hscript_expr(constraints_path, "cloth_damping_ratio", f'ch("{ctrl_path}/cloth_damping_ratio")')

            return _result(True, data={"ctrl_node_path": ctrl_path})
        except Exception as e:
            return _result(False, errors=[f"node.setup_vellum_ctrl failed: {e}"])

    if op == "node.setup_vellum_collisions":
        geo_path = str(args.get("geo_path") or "")
        solver_path = str(args.get("solver_path") or "")
        if dry_run:
            return _result(True, data={"preview": f"setup vellum collisions {geo_path} -> {solver_path}"})

        geo = hou.node(geo_path)
        solver = hou.node(solver_path)
        if geo is None or solver is None:
            return _result(False, errors=[f"node.setup_vellum_collisions missing geo={geo_path!r} or solver={solver_path!r}"])

        def _as_int(v: Any) -> int:
            try:
                return int(float(v))
            except (TypeError, ValueError):
                return 0

        def _as_float(v: Any, default: float = 0.0) -> float:
            try:
                return float(v)
            except (TypeError, ValueError):
                return default

        use_ground = _as_int(args.get("use_ground_plane")) != 0
        joined = str(args.get("static_collider_paths") or "").strip()
        if joined:
            collider_paths = [p.strip() for p in joined.split("|||") if p.strip()]
        else:
            one = str(args.get("static_collider_path") or "").strip()
            collider_paths = [one] if one else []

        try:
            nslots = int(float(args.get("collider_import_slots") or 2))
        except (TypeError, ValueError):
            nslots = 2
        nslots = max(1, min(nslots, 16))

        ground_y = _as_float(args.get("ground_offset_y"), -0.65)
        ground_sx = _as_float(args.get("ground_sizex"), 12.0)
        ground_sy = _as_float(args.get("ground_sizey"), 0.25)
        ground_sz = _as_float(args.get("ground_sizez"), 12.0)
        ground_tx = _as_float(args.get("ground_tx"), 0.0)
        ground_tz = _as_float(args.get("ground_tz"), 0.0)
        ground_rx = _as_float(args.get("ground_rx"), 0.0)
        ground_ry = _as_float(args.get("ground_ry"), 0.0)
        ground_rz = _as_float(args.get("ground_rz"), 0.0)

        ctrl_path = str(args.get("ctrl_node_path") or "").strip()
        ctrl = hou.node(ctrl_path) if ctrl_path else None
        CTRL_REL = "../CTRL_vellum_cloth"

        def _try_parm(n: Any, candidates: tuple[str, ...], val: float) -> bool:
            for name in candidates:
                p = n.parm(name)
                if p is not None:
                    p.set(val)
                    return True
            return False

        def _set_h_expr(n: Any, name: str, hexpr: str) -> None:
            p = n.parm(name)
            if p is None:
                return
            try:
                p.setExpression(hexpr, language=hou.exprLanguage.Hscript)
            except hou.OperationFailed:
                pass

        def _set_objpath_expr(n: Any, hexpr: str) -> bool:
            for cand in ("objpath1", "objpath", "path"):
                p = n.parm(cand)
                if p is not None:
                    try:
                        p.setExpression(hexpr, language=hou.exprLanguage.Hscript)
                        return True
                    except hou.OperationFailed:
                        return False
            return False

        def _set_xform_trs(xf: Any, slot: int) -> None:
            pre = f'{CTRL_REL}/imp_{slot}'
            _set_h_expr(xf, "tx", f'ch("{pre}_tx")')
            _set_h_expr(xf, "ty", f'ch("{pre}_ty")')
            _set_h_expr(xf, "tz", f'ch("{pre}_tz")')
            _set_h_expr(xf, "rx", f'ch("{pre}_rx")')
            _set_h_expr(xf, "ry", f'ch("{pre}_ry")')
            _set_h_expr(xf, "rz", f'ch("{pre}_rz")')
            for nm in ("scale", "uniformscale", "s"):
                if xf.parm(nm) is not None:
                    _set_h_expr(xf, nm, f'ch("{pre}_scale")')
                    return
            for nm in ("sx", "sy", "sz"):
                if xf.parm(nm) is not None:
                    _set_h_expr(xf, nm, f'ch("{pre}_scale")')

        try:
            sources: list[Any] = []
            coll_out: Any = None

            def _push_collider_ctrl() -> None:
                if ctrl is None:
                    return
                for name, val in (
                    ("coll_ground_sizex", ground_sx),
                    ("coll_ground_sizey", ground_sy),
                    ("coll_ground_sizez", ground_sz),
                    ("coll_ground_tx", ground_tx),
                    ("coll_ground_ty", ground_y),
                    ("coll_ground_tz", ground_tz),
                    ("coll_ground_rx", ground_rx),
                    ("coll_ground_ry", ground_ry),
                    ("coll_ground_rz", ground_rz),
                ):
                    p = ctrl.parm(name)
                    if p is not None:
                        try:
                            p.set(float(val))
                        except Exception:
                            pass
                for si in range(1, nslots + 1):
                    pn = f"imp_{si}_path"
                    p = ctrl.parm(pn)
                    if p is not None:
                        try:
                            p.set(collider_paths[si - 1] if si - 1 < len(collider_paths) else "")
                        except Exception:
                            pass

            if ctrl is not None:
                _push_collider_ctrl()

            if use_ground:
                ground = geo.node("mcp_ground_plane1") or geo.createNode("box", "mcp_ground_plane1")
                if ctrl is not None:
                    _set_h_expr(ground, "sizex", f'ch("{CTRL_REL}/coll_ground_sizex")')
                    _set_h_expr(ground, "sizey", f'ch("{CTRL_REL}/coll_ground_sizey")')
                    _set_h_expr(ground, "sizez", f'ch("{CTRL_REL}/coll_ground_sizez")')
                    _set_h_expr(ground, "tx", f'ch("{CTRL_REL}/coll_ground_tx")')
                    _set_h_expr(ground, "ty", f'ch("{CTRL_REL}/coll_ground_ty")')
                    _set_h_expr(ground, "tz", f'ch("{CTRL_REL}/coll_ground_tz")')
                    _set_h_expr(ground, "rx", f'ch("{CTRL_REL}/coll_ground_rx")')
                    _set_h_expr(ground, "ry", f'ch("{CTRL_REL}/coll_ground_ry")')
                    _set_h_expr(ground, "rz", f'ch("{CTRL_REL}/coll_ground_rz")')
                else:
                    _try_parm(ground, ("sizex",), ground_sx)
                    _try_parm(ground, ("sizey",), ground_sy)
                    _try_parm(ground, ("sizez",), ground_sz)
                    if ground.parm("tx") is not None:
                        ground.parm("tx").set(ground_tx)
                    if ground.parm("ty") is not None:
                        ground.parm("ty").set(ground_y)
                    if ground.parm("tz") is not None:
                        ground.parm("tz").set(ground_tz)
                    _try_parm(ground, ("rx",), ground_rx)
                    _try_parm(ground, ("ry",), ground_ry)
                    _try_parm(ground, ("rz",), ground_rz)
                sources.append(ground)

            if ctrl is not None:
                for si in range(1, nslots + 1):
                    om_name = f"mcp_collider_import{si}"
                    om = geo.node(om_name) or geo.createNode("object_merge", om_name)
                    if not _set_objpath_expr(om, f'chs("{CTRL_REL}/imp_{si}_path")'):
                        return _result(False, errors=["object_merge has no objpath parm for static collider"])
                    xf_name = f"mcp_collider_xform{si}"
                    xf = geo.node(xf_name) or geo.createNode("xform", xf_name)
                    xf.setInput(0, om, 0)
                    _set_xform_trs(xf, si)
                    sources.append(xf)
            else:
                for idx, collider_path in enumerate(collider_paths):
                    om_name = f"mcp_collider_import{idx + 1}"
                    om = geo.node(om_name) or geo.createNode("object_merge", om_name)
                    setp = False
                    for cand in ("objpath1", "objpath", "path"):
                        p = om.parm(cand)
                        if p is not None:
                            p.set(collider_path)
                            setp = True
                            break
                    if not setp:
                        return _result(False, errors=["object_merge has no objpath parm for static collider"])
                    sources.append(om)

            if not sources:
                try:
                    solver.setInput(2, None)
                except Exception:
                    pass
                return _result(True, data={"collision_inputs": 0})

            if len(sources) == 1:
                coll_out = sources[0]
                solver.setInput(2, coll_out, 0)
            else:
                mg = geo.node("mcp_collisions_merge1") or geo.createNode("merge", "mcp_collisions_merge1")
                for i, src in enumerate(sources):
                    mg.setInput(i, src, 0)
                coll_out = mg
                solver.setInput(2, mg, 0)

            if coll_out is not None:
                # Visibility toggle must be live without callback APIs (some Houdini builds miss setScriptCallback).
                view_sw = geo.node("mcp_collisions_view_switch1") or geo.createNode("switch", "mcp_collisions_view_switch1")
                view_off = geo.node("mcp_collisions_view_off1") or geo.createNode("null", "mcp_collisions_view_off1")
                view_sw.setInput(0, coll_out, 0)
                view_sw.setInput(1, view_off, 0)
                if ctrl is not None and ctrl.parm("coll_show_collisions") is not None:
                    _set_h_expr(view_sw, "input", f'if(ch("{CTRL_REL}/coll_show_collisions"),0,1)')
                else:
                    _try_parm(view_sw, ("input",), 0.0)

                view_n = geo.node("VIEW_mcp_collisions") or geo.createNode("null", "VIEW_mcp_collisions")
                view_n.setInput(0, view_sw, 0)
                try:
                    view_n.setDisplayFlag(False)
                    view_n.setTemplateFlag(True)
                except hou.OperationFailed:
                    pass

            return _result(
                True,
                data={
                    "collision_inputs": len(sources),
                    "static_collider_paths": collider_paths,
                    "collider_import_slots": nslots,
                    "ground_box": use_ground,
                    "collision_view_path": f"{geo_path}/VIEW_mcp_collisions",
                    "ctrl_collision_parms": bool(ctrl is not None),
                },
            )
        except Exception as e:
            return _result(False, errors=[f"node.setup_vellum_collisions failed: {e}"])

    if op == "parm.get":
        path = str(args.get("node_path") or "")
        name = str(args.get("parm_name") or "")
        if dry_run:
            return _result(True, data={"preview": f"get {path}/{name}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        p = n.parm(name)
        if p is None:
            return _result(False, errors=[f"Parm not found: {path}/{name}"])
        return _result(True, data={"value": p.eval()})

    if op == "parm.get_raw":
        path = str(args.get("node_path") or "")
        name = str(args.get("parm_name") or "")
        if not path.strip() or not name.strip():
            return _result(False, errors=["parm.get_raw requires node_path and parm_name"])
        if dry_run:
            return _result(True, data={"preview": f"get_raw {path}/{name}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        p, resolved, alias_warn = _resolve_parm_for_set(n, name, dry_run=False)
        warnings.extend(alias_warn)
        if p is None:
            return _result(False, errors=[f"Parm not found: {path}/{name}"])
        try:
            raw: str | None = None
            if hasattr(p, "unexpandedString"):
                try:
                    raw = p.unexpandedString()
                except hou.OperationFailed:
                    raw = None
            if raw is None:
                raw = str(p.rawValue())
            return _result(True, data={"node_path": path, "parm": resolved, "raw": raw})
        except Exception as e:
            return _result(False, errors=[f"parm.get_raw failed: {e}"])

    if op == "parm.exists":
        path = str(args.get("node_path") or "")
        name = str(args.get("parm_name") or "")
        if not path.strip() or not name.strip():
            return _result(False, errors=["parm.exists requires node_path and parm_name"])
        if dry_run:
            return _result(True, data={"preview": f"exists {path}/{name}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        p = n.parm(name)
        return _result(True, data={"node_path": path, "parm_name": name, "exists": p is not None})

    if op == "parm.set":
        path = str(args.get("node_path") or "")
        name = str(args.get("parm_name") or "")
        value = args.get("value")
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        p, resolved, alias_warn = _resolve_parm_for_set(n, name, dry_run=dry_run)
        warnings.extend(alias_warn)
        if dry_run:
            lbl = resolved if p is not None else name
            if p is None and hasattr(n, "parmTuple"):
                try:
                    pt_preview = n.parmTuple(name)
                    if pt_preview is not None:
                        lbl = f"{name} (parmTuple len={len(pt_preview)})"
                except Exception:
                    pass
            return _result(True, data={"preview": f"set {path}/{lbl} = {value!r}"})
        if p is None:
            if hasattr(n, "parmTuple"):
                try:
                    pt = n.parmTuple(name)
                except Exception:
                    pt = None
                if pt is not None:
                    try:
                        if isinstance(value, (list, tuple)) and len(value) == len(pt):
                            coerced_t = [float(x) for x in value]
                            pt.set(coerced_t)
                            try:
                                _fix_grid_if_1x1_polygon_degenerate(n, warnings)
                            except Exception:
                                pass
                            return _result(True, data={"node_path": path, "parm": name, "target": "parmTuple"})
                        return _result(
                            False,
                            errors=[
                                f"parm.set tuple {path}/{name}: expected sequence of length {len(pt)}, got {value!r}"
                            ],
                        )
                    except Exception as e:
                        return _result(False, errors=[f"parm.set parmTuple failed {path}/{name}: {e}"])
            warnings.append(f"Parm missing (skipped): {path}/{name}")
            return _result(True, warnings=warnings, data={"skipped": True})
        try:
            coerced = _coerce_parm_value(p, value)
            p.set(coerced)
            try:
                _fix_grid_if_1x1_polygon_degenerate(n, warnings)
            except Exception:
                pass
            return _result(True, data={"node_path": path, "parm": resolved})
        except Exception as e:
            return _result(False, errors=[f"parm.set failed {path}/{resolved}: {e}"])

    if op == "parm.set_batch":
        path = str(args.get("node_path") or "")
        params = args.get("params") or {}
        if not isinstance(params, dict):
            return _result(False, errors=["parm.set_batch requires dict params"])
        if dry_run:
            return _result(True, data={"preview": f"set_batch {path} keys={list(params.keys())}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        for k, v in params.items():
            p = n.parm(str(k))
            if p is None:
                warnings.append(f"Parm missing (skipped): {path}/{k}")
                continue
            try:
                p.set(_coerce_parm_value(p, v))
            except Exception as e:
                warnings.append(f"parm set failed {path}/{k}: {e}")
        try:
            _fix_grid_if_1x1_polygon_degenerate(n, warnings)
        except Exception:
            pass
        return _result(True, warnings=warnings, data={"node_path": path})

    if op == "parm.set_expression":
        path = str(args.get("node_path") or "")
        name = str(args.get("parm_name") or "")
        expr = str(args.get("expression") or args.get("expr") or "")
        lang = str(args.get("language") or args.get("lang") or "hscript")
        if not path.strip() or not name.strip():
            return _result(False, errors=["parm.set_expression requires node_path and parm_name"])
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        p, resolved, alias_warn = _resolve_parm_for_set(n, name, dry_run=dry_run)
        warnings.extend(alias_warn)
        if dry_run:
            lbl = resolved if p is not None else name
            return _result(True, data={"preview": f"expr {path}/{lbl} lang={lang!r}"})
        if p is None:
            return _result(False, errors=[f"Parm not found: {path}/{name}"])
        try:
            p.setExpression(expr, language=_expr_language(hou, lang))
            return _result(True, data={"node_path": path, "parm": resolved})
        except Exception as e:
            return _result(False, errors=[f"parm.set_expression failed: {e}"])

    if op == "parm.revert_defaults":
        path = str(args.get("node_path") or "")
        pname = args.get("parm_name")
        if not path.strip():
            return _result(False, errors=["parm.revert_defaults requires node_path"])
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        if dry_run:
            return _result(True, data={"preview": f"revert_defaults {path} parm={pname!r}"})
        try:
            def _clear_anim_and_revert(parm: Any) -> None:
                # Some Houdini builds keep expression channels after revertToDefaults();
                # clear channels first to force a true default reset.
                try:
                    parm.deleteAllKeyframes()
                except Exception:
                    pass
                parm.revertToDefaults()

            if pname is not None and str(pname).strip():
                p = n.parm(str(pname).strip())
                if p is None:
                    return _result(False, errors=[f"Parm not found: {path}/{pname}"])
                _clear_anim_and_revert(p)
                return _result(True, data={"node_path": path, "parm": str(pname)})
            nreverted = 0
            for p in n.parms():
                try:
                    _clear_anim_and_revert(p)
                    nreverted += 1
                except hou.OperationFailed:
                    continue
            return _result(True, data={"node_path": path, "reverted_parm_count": nreverted})
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "parm.list":
        path = str(args.get("node_path") or "")
        prefix = str(args.get("prefix") or "")
        try:
            max_n = int(args.get("max_count", 500))
        except (TypeError, ValueError):
            max_n = 500
        max_n = max(1, min(max_n, 5000))
        if not path.strip():
            return _result(False, errors=["parm.list requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"parm.list {path!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            warnings = list(warnings)
            seen: set[str] = set()
            ordered: list[str] = []

            def push(nm: str) -> None:
                nonlocal ordered
                if prefix and not nm.startswith(prefix):
                    return
                if nm in seen:
                    return
                seen.add(nm)
                ordered.append(nm)

            # Parm template group first: stable order and works when parm instances are sparse.
            for nm in _parm_template_names(n):
                push(nm)
                if len(ordered) >= max_n:
                    break

            if len(ordered) < max_n:
                parm_collections: list[Any] = []
                try:
                    parm_collections.append(tuple(n.parms()))
                except Exception:
                    pass
                try:
                    ap = getattr(n, "allParms", None)
                    if callable(ap):
                        parm_collections.append(tuple(ap()))
                except Exception:
                    pass
                for coll in parm_collections:
                    for p in coll:
                        try:
                            push(p.name())
                        except Exception:
                            continue
                        if len(ordered) >= max_n:
                            break
                    if len(ordered) >= max_n:
                        break

            # Tuple-level fallback (built-in SOPs often expose parms cleanly via parmTuples).
            if len(ordered) < max_n:
                try:
                    for pt in n.parmTuples():
                        try:
                            push(pt.name())
                        except Exception:
                            continue
                        if len(ordered) >= max_n:
                            break
                except Exception:
                    pass

            # Last resort: synthesize input0.. when templates/instances are invisible but wires exist.
            if len(ordered) < max_n and len(ordered) == 0:
                for nm in _parm_names_from_inputs(n):
                    push(nm)
                    if len(ordered) >= max_n:
                        break
                if ordered:
                    warnings.append("parm.list: used inputN fallback (templates/instances empty)")

            if len(ordered) >= max_n:
                warnings.append(f"parm.list truncated at max_count={max_n}")

            dbg = {
                "parms_len": None,
                "allparms_len": None,
                "parmtuples_len": None,
                "inputs_len": None,
                "template_names_len": len(_parm_template_names(n)),
            }
            try:
                dbg["parms_len"] = len(tuple(n.parms()))
            except Exception:
                dbg["parms_len"] = None
            try:
                ap = getattr(n, "allParms", None)
                dbg["allparms_len"] = len(tuple(ap())) if callable(ap) else None
            except Exception:
                dbg["allparms_len"] = None
            try:
                dbg["parmtuples_len"] = len(tuple(n.parmTuples()))
            except Exception:
                dbg["parmtuples_len"] = None
            try:
                dbg["inputs_len"] = len(n.inputs())
            except Exception:
                dbg["inputs_len"] = None

            return _result(
                True,
                warnings=warnings,
                data={"node_path": path, "parm_names": ordered, "count": len(ordered), "debug": dbg},
            )
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "graph.exists":
        path = str(args.get("node_path") or "")
        if dry_run:
            return _result(True, data={"preview": f"exists {path!r}"})
        return _result(True, data={"exists": hou.node(path) is not None})

    if op == "graph.list_children":
        path = str(args.get("path") or "")
        if dry_run:
            return _result(True, data={"preview": f"list_children {path!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        return _result(True, data={"children": [c.name() for c in n.children()]})

    if op == "graph.layout_children":
        parent = str(args.get("parent_path") or args.get("path") or "")
        if not parent.strip():
            return _result(False, errors=["graph.layout_children requires parent_path (or path)"])
        if dry_run:
            return _result(True, data={"preview": f"layout_children {parent!r}"})
        n = hou.node(parent)
        if n is None:
            return _result(False, errors=[f"Node not found: {parent}"])
        try:
            n.layoutChildren()
            return _result(True, data={"parent_path": parent, "laid_out": True})
        except Exception as e:
            return _result(False, errors=[f"graph.layout_children failed: {e}"])

    if op == "graph.glob":
        parent = str(args.get("parent_path") or args.get("path") or "")
        pattern = str(args.get("pattern") or "*")
        recursive = bool(args.get("recursive", False))
        if not parent.strip():
            return _result(False, errors=["graph.glob requires parent_path (or path)"])
        if dry_run:
            return _result(True, data={"preview": f"glob {parent!r} pat={pattern!r} recursive={recursive}"})
        n = hou.node(parent)
        if n is None:
            return _result(False, errors=[f"Node not found: {parent}"])
        try:
            if recursive and hasattr(n, "recursiveGlob"):
                found = n.recursiveGlob(pattern)
            else:
                found = n.glob(pattern)
            paths = [x.path() for x in found]
            return _result(True, data={"parent_path": parent, "pattern": pattern, "recursive": recursive, "matches": paths})
        except Exception as e:
            return _result(False, errors=[f"graph.glob failed: {e}"])

    if op == "node.delete":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["node.delete requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"delete {path!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            n.destroy()
            return _result(True, data={"destroyed": path})
        except hou.OperationFailed as e:
            return _result(False, errors=[str(e)])

    if op == "node.rename":
        path = str(args.get("node_path") or "")
        new_name = str(args.get("new_name") or "")
        unique_name = bool(args.get("unique_name", True))
        if not path.strip() or not new_name.strip():
            return _result(False, errors=["node.rename requires node_path and new_name"])
        if dry_run:
            return _result(True, data={"preview": f"rename {path!r} -> {new_name!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            n.setName(new_name.strip(), unique_name=unique_name)
            return _result(True, data={"node_path": n.path(), "name": n.name()})
        except hou.OperationFailed as e:
            return _result(False, errors=[str(e)])

    if op == "node.disconnect":
        dst = str(args.get("dst") or "")
        di = int(args.get("dst_input", 0))
        if not dst.strip():
            return _result(False, errors=["node.disconnect requires dst"])
        if dry_run:
            return _result(True, data={"preview": f"disconnect input {di} on {dst!r}"})
        dn = hou.node(dst)
        if dn is None:
            return _result(False, errors=[f"Node not found: {dst}"])
        try:
            dn.setInput(di, None)
            return _result(True, data={"dst": dst, "dst_input": di})
        except hou.OperationFailed as e:
            return _result(False, errors=[str(e)])

    if op == "node.duplicate":
        path = str(args.get("node_path") or "")
        new_name = args.get("new_name")
        auto_layout = bool(args.get("auto_layout", True))
        if not path.strip():
            return _result(False, errors=["node.duplicate requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"duplicate {path!r} auto_layout={auto_layout}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            parent = n.parent()
            if parent is None:
                return _result(False, errors=["node.duplicate: node has no parent"])
            if hasattr(hou, "copyNodes"):
                out = hou.copyNodes((n,), parent)
                if not out:
                    return _result(False, errors=["copyNodes returned no nodes"])
                dup = out[0]
            else:
                dup = n.copyTo(parent)
            if new_name is not None and str(new_name).strip():
                dup.setName(str(new_name).strip(), unique_name=True)
            if auto_layout:
                try:
                    parent.layoutChildren()
                except Exception as le:
                    warnings.append(f"node.duplicate auto_layout failed: {le}")
            if warnings:
                return _result(True, warnings=warnings, data={"node_path": dup.path(), "name": dup.name()})
            return _result(True, data={"node_path": dup.path(), "name": dup.name()})
        except Exception as e:
            return _result(False, errors=[f"node.duplicate failed: {e}"])

    if op == "node.set_position":
        path = str(args.get("node_path") or "")
        x = float(args.get("x", 0.0))
        y = float(args.get("y", 0.0))
        if not path.strip():
            return _result(False, errors=["node.set_position requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"set_position {path} ({x},{y})"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            n.setPosition((x, y))
            return _result(True, data={"node_path": path, "x": x, "y": y})
        except hou.OperationFailed as e:
            return _result(False, errors=[str(e)])

    if op == "node.set_comment":
        path = str(args.get("node_path") or "")
        comment = str(args.get("comment") or "")
        if not path.strip():
            return _result(False, errors=["node.set_comment requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"set_comment {path!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            n.setComment(comment)
            return _result(True, data={"node_path": path})
        except hou.OperationFailed as e:
            return _result(False, errors=[str(e)])

    if op == "node.set_color":
        path = str(args.get("node_path") or "")
        r = float(args.get("r", 0.5))
        g = float(args.get("g", 0.5))
        b = float(args.get("b", 0.5))
        if not path.strip():
            return _result(False, errors=["node.set_color requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"set_color {path} rgb=({r},{g},{b})"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            n.setColor(hou.Color((r, g, b)))
            return _result(True, data={"node_path": path})
        except hou.OperationFailed as e:
            return _result(False, errors=[str(e)])

    if op == "node.info":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["node.info requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"node.info {path!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            nt = n.type()
            errs = [str(e) for e in (n.errors() or [])][:32]
            data = {
                "node_path": n.path(),
                "name": n.name(),
                "type_name": nt.name(),
                "category": nt.category().name(),
                "child_count": len(n.children()),
                "display_flag": bool(n.isDisplayFlagSet()),
                "render_flag": bool(n.isRenderFlagSet()),
                "template_flag": bool(n.isTemplateFlagSet()),
                "is_bypassed": bool(n.isBypassed()),
                "is_locked": bool(n.isLocked()) if hasattr(n, "isLocked") else None,
                "errors": errs,
            }
            return _result(True, data=data)
        except Exception as e:
            return _result(False, errors=[f"node.info failed: {e}"])

    if op == "node.references_list":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["node.references_list requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"node.references_list {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            refs: list[str] = []
            ref_fn = getattr(n, "references", None)
            if callable(ref_fn):
                try:
                    seq = ref_fn() or ()
                    for x in seq:
                        try:
                            if x is not None:
                                refs.append(x.path())
                        except Exception:
                            continue
                except Exception as e:
                    return _result(False, errors=[f"node.references_list references(): {e}"])
            return _result(True, data={"node_path": path.strip(), "references": refs, "count": len(refs)})
        except Exception as e:
            return _result(False, errors=[f"node.references_list failed: {e}"])

    if op == "node.dependents_list":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["node.dependents_list requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"node.dependents_list {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            deps: list[str] = []
            dep_fn = getattr(n, "dependents", None)
            if callable(dep_fn):
                try:
                    seq = dep_fn() or ()
                    for x in seq:
                        try:
                            if x is not None:
                                deps.append(x.path())
                        except Exception:
                            continue
                except Exception as e:
                    return _result(False, errors=[f"node.dependents_list dependents(): {e}"])
            return _result(True, data={"node_path": path.strip(), "dependents": deps, "count": len(deps)})
        except Exception as e:
            return _result(False, errors=[f"node.dependents_list failed: {e}"])

    if op == "node.bypass":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["node.bypass requires node_path"])
        on = args.get("enabled")
        if on is None:
            on = args.get("bypass")
        bypass_on = True if on is None else bool(int(on)) if str(on).isdigit() else bool(on)
        if dry_run:
            return _result(True, data={"preview": f"bypass {path} -> {bypass_on}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            n.bypass(bypass_on)
            return _result(True, data={"node_path": path, "bypass": bypass_on})
        except hou.OperationFailed as e:
            return _result(False, errors=[str(e)])

    if op == "node.lock":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["node.lock requires node_path"])
        lk = args.get("locked")
        locked = True if lk is None else bool(int(lk)) if str(lk).isdigit() else bool(lk)
        if dry_run:
            return _result(True, data={"preview": f"lock {path} -> {locked}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            # SOP nodes often expose hard lock APIs instead of setLocked.
            if hasattr(n, "setHardLocked"):
                n.setHardLocked(locked)
            elif hasattr(n, "setLocked"):
                n.setLocked(locked)
            else:
                return _result(False, errors=[f"node.lock unsupported for this node type: {path}"])
            return _result(True, data={"node_path": path, "locked": locked})
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "node.set_selectable":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["node.set_selectable requires node_path"])
        sv = args.get("selectable")
        selectable = True if sv is None else bool(int(sv)) if str(sv).isdigit() else bool(sv)
        if dry_run:
            return _result(True, data={"preview": f"selectable {path} -> {selectable}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            method = None
            if hasattr(n, "setSelectable"):
                n.setSelectable(selectable)
                method = "setSelectable"
            elif hasattr(n, "setSelectableInViewport"):
                n.setSelectableInViewport(selectable)
                method = "setSelectableInViewport"
            elif hasattr(n, "setDisplayFlag"):
                # Fallback for node types without explicit selectability API.
                # Keep this non-destructive: only disable display when setting non-selectable.
                if not selectable:
                    n.setDisplayFlag(False)
                method = "display_flag_fallback"
            else:
                return _result(False, errors=[f"node.set_selectable unsupported for this node type: {path}"])
            return _result(True, data={"node_path": path, "selectable": selectable, "method": method})
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "selection.clear":
        if dry_run:
            return _result(True, data={"preview": "selection.clear"})
        try:
            hou.clearAllSelected()
            return _result(True, data={"cleared": True})
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "selection.set":
        raw = args.get("node_paths") or args.get("paths") or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return _result(False, errors=["selection.set requires node_paths list"])
        if dry_run:
            return _result(True, data={"preview": f"selection.set count={len(raw)}"})
        try:
            hou.clearAllSelected()
            selected: list[str] = []
            for p in raw:
                ps = str(p).strip()
                if not ps:
                    continue
                n = hou.node(ps)
                if n is None:
                    warnings.append(f"selection.set: missing node {ps!r}")
                    continue
                n.setSelected(True, clear_all_selected=False)
                selected.append(ps)
            return _result(True, warnings=warnings, data={"selected": selected})
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "timeline.set_range":
        start = float(args.get("start", 1))
        end = float(args.get("end", 240))
        if dry_run:
            return _result(True, data={"preview": f"timeline {start}-{end}"})
        try:
            hou.playbar.setPlaybackRange(start, end)
            return _result(True, data={"start": start, "end": end})
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "timeline.set_frame":
        frame = float(args.get("frame", 1))
        if dry_run:
            return _result(True, data={"preview": f"set_frame {frame}"})
        try:
            hou.setFrame(frame)
            return _result(True, data={"frame": frame})
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "timeline.set_fps":
        fps = float(args.get("fps", 24.0))
        if dry_run:
            return _result(True, data={"preview": f"set_fps {fps}"})
        try:
            hou.setFps(fps)
            return _result(True, data={"fps": fps})
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "timeline.offset_frame":
        dv = args.get("delta") if args.get("delta") is not None else args.get("offset")
        if dv is None:
            return _result(False, errors=["timeline.offset_frame requires delta (or offset)"])
        if dry_run:
            return _result(True, data={"preview": f"timeline.offset_frame {dv!r}"})
        try:
            nf = float(hou.frame()) + float(dv)
            hou.setFrame(nf)
            return _result(True, data={"frame": nf, "delta": float(dv)})
        except Exception as e:
            return _result(False, errors=[f"timeline.offset_frame failed: {e}"])

    if op == "playback.set":
        mode = str(args.get("mode") or args.get("state") or "").strip().lower()
        if not mode:
            return _result(False, errors=["playback.set requires mode (or state): play | pause | stop"])
        if dry_run:
            return _result(True, data={"preview": f"playback.set {mode!r}"})
        try:
            out: dict[str, Any] = {}

            def _work() -> None:
                out.update(_playback_apply_core(hou, mode))

            d_ok, d_err, fn_ex = _run_on_ui_thread(hou, _work, timeout=3.0)
            if fn_ex is not None:
                return _result(False, errors=[f"playback.set failed: {fn_ex}"])
            if d_ok:
                return _result(
                    True,
                    data={
                        "mode": mode,
                        "method": out.get("method"),
                        "is_playing": out.get("is_playing"),
                        "dispatch": "ui_thread",
                    },
                )
            warn = (
                f"playback.set: UI thread dispatch failed ({d_err}); "
                "used direct playbar call (often ineffective from the MCP TCP thread)."
            )
            try:
                out = _playback_apply_core(hou, mode)
            except Exception as e:
                return _result(False, errors=[f"playback.set failed: {e}"], warnings=[warn])
            return _result(
                True,
                data={
                    "mode": mode,
                    "method": out.get("method"),
                    "is_playing": out.get("is_playing"),
                    "dispatch": "direct",
                },
                warnings=[warn],
            )
        except ValueError as e:
            return _result(False, errors=[str(e)])
        except Exception as e:
            return _result(False, errors=[f"playback.set failed: {e}"])

    if op == "exec.cook":
        path = str(args.get("node_path") or "")
        if dry_run:
            return _result(True, data={"preview": f"cook {path}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            n.cook(force=True)
            return _result(True, data={"node_path": path})
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "geo.info":
        path = str(args.get("node_path") or "")
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.info requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.info {path!r} force_cook={force_cook}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path} (need a cooked SOP / compatible node)"])
            def _safe_geo_count(geo: Any, names: tuple[str, ...], fallback_seq: str | None = None) -> int | None:
                for nm in names:
                    fn = getattr(geo, nm, None)
                    if callable(fn):
                        try:
                            return int(fn())
                        except Exception:
                            continue
                if fallback_seq:
                    seq_fn = getattr(geo, fallback_seq, None)
                    if callable(seq_fn):
                        try:
                            return int(len(seq_fn()))
                        except Exception:
                            return None
                return None

            data: dict[str, Any] = {"node_path": path}
            npts = _safe_geo_count(g, ("numPoints", "pointCount"), "points")
            nprims = _safe_geo_count(g, ("numPrims", "numPrimitives", "primCount"), "prims")
            nverts = _safe_geo_count(g, ("numVertices", "vertexCount"), "vertices")
            if npts is not None:
                data["num_points"] = npts
            if nprims is not None:
                data["num_primitives"] = nprims
            if nverts is not None:
                data["num_vertices"] = nverts
            try:
                bb = g.boundingBox()
                data["bounds_min"] = [float(bb.minvec().x()), float(bb.minvec().y()), float(bb.minvec().z())]
                data["bounds_max"] = [float(bb.maxvec().x()), float(bb.maxvec().y()), float(bb.maxvec().z())]
            except Exception:
                pass
            return _result(True, data=data)
        except Exception as e:
            return _result(False, errors=[f"geo.info failed: {e}"])

    if op == "geo.is_empty":
        path = str(args.get("node_path") or "")
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.is_empty requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.is_empty {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(True, data={"node_path": path.strip(), "is_empty": True, "reason": "no_geometry"})
            np = None
            for nm in ("numPoints", "pointCount"):
                fn = getattr(g, nm, None)
                if callable(fn):
                    try:
                        np = int(fn())
                        break
                    except Exception:
                        continue
            if np is None:
                try:
                    pts = g.points()
                    np = len(pts)
                except Exception:
                    np = None
            empty = np == 0 if np is not None else True
            return _result(
                True,
                data={
                    "node_path": path.strip(),
                    "is_empty": empty,
                    "num_points": np,
                    "note": "Based on point count only; packed geo / volumes may need geo.topology_summary.",
                },
            )
        except Exception as e:
            return _result(False, errors=[f"geo.is_empty failed: {e}"])

    if op == "geo.topology_summary":
        path = str(args.get("node_path") or "")
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.topology_summary requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.topology_summary {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(
                    True,
                    data={
                        "node_path": path.strip(),
                        "has_geometry": False,
                        "is_empty": True,
                    },
                )
            npts = npr = nv = None
            try:
                npts = int(g.numPoints())
            except Exception:
                try:
                    npts = len(g.points())
                except Exception:
                    npts = None
            try:
                npr = int(g.numPrims())
            except Exception:
                try:
                    npr = len(g.prims())
                except Exception:
                    npr = None
            try:
                nv = int(g.numVertices())
            except Exception:
                nv = None
            degenerate = False
            try:
                if npts == 0 and (npr or 0) > 0:
                    degenerate = True
            except Exception:
                pass
            return _result(
                True,
                data={
                    "node_path": path.strip(),
                    "has_geometry": True,
                    "num_points": npts,
                    "num_primitives": npr,
                    "num_vertices": nv,
                    "is_empty": (npts == 0 if npts is not None else True) and (npr == 0 if npr is not None else True),
                    "possible_degenerate_points_zero_with_prims": degenerate,
                },
            )
        except Exception as e:
            return _result(False, errors=[f"geo.topology_summary failed: {e}"])

    if op == "exec.render_rop":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["exec.render_rop requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"render_rop {path!r} (blocking in Houdini)"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        if not hasattr(n, "render"):
            return _result(False, errors=[f"Node has no .render(): {path!r} (not a ROP/driver?)"])
        try:
            n.render()
            return _result(True, data={"node_path": path, "rendered": True})
        except Exception as e:
            return _result(False, errors=[f"exec.render_rop failed: {e}"])

    if op == "exec.cache":
        if dry_run:
            return _result(True, data={"preview": "exec.cache -> cache.clear_all chain"})
        okc, method, att = _clear_session_caches_best_effort(hou)
        if okc:
            w = ["exec.cache is deprecated; prefer op cache.clear_all."] + (att or [])
            return _result(True, warnings=w, data={"cleared": True, "method": method})
        return _result(
            False,
            errors=["exec.cache: no cache clear succeeded"],
            data={"attempts": att},
        )

    if op == "hip.save":
        file_path = args.get("file_path")
        if dry_run:
            return _result(True, data={"preview": f"hip.save {file_path!r}"})
        try:
            fp = str(file_path).strip() if file_path is not None else ""
            if fp:
                try:
                    hou.hipFile.save(fp, save_to_recent_files=True)
                except TypeError:
                    hou.hipFile.save(fp)
            else:
                try:
                    hou.hipFile.save(save_to_recent_files=True)
                except TypeError:
                    hou.hipFile.save()
            return _result(True, data={"saved": fp or hou.hipFile.path()})
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "hip.load":
        file_path = str(args.get("file_path") or "")
        ignore_warnings = bool(args.get("ignore_load_warnings", False))
        if not file_path.strip():
            return _result(False, errors=["hip.load requires file_path"])
        if dry_run:
            return _result(True, data={"preview": f"hip.load {file_path!r}"})
        try:
            hou.hipFile.load(file_path.strip(), ignore_load_warnings=ignore_warnings)
            return _result(True, data={"loaded": file_path.strip()})
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "hip.new":
        suppress = bool(args.get("suppress_save_prompt", True))
        if dry_run:
            return _result(True, data={"preview": f"hip.new suppress_save_prompt={suppress}"})
        try:
            hou.hipFile.clear(suppress_save_prompt=suppress)
            return _result(True, data={"cleared": True})
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "hip.merge":
        file_path = str(args.get("file_path") or "")
        ignore_warnings = bool(args.get("ignore_load_warnings", False))
        if not file_path.strip():
            return _result(False, errors=["hip.merge requires file_path"])
        if dry_run:
            return _result(True, data={"preview": f"hip.merge {file_path!r}"})
        try:
            try:
                hou.hipFile.merge(file_path.strip(), ignore_load_warnings=ignore_warnings)
            except TypeError:
                hou.hipFile.merge(file_path.strip())
            return _result(True, data={"merged": file_path.strip()})
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "path.expand_string":
        raw = str(args.get("string") or args.get("path") or args.get("value") or "")
        if not raw.strip():
            return _result(False, errors=["path.expand_string requires string (or path / value)"])
        if dry_run:
            return _result(True, data={"preview": f"path.expand_string {raw!r}"})
        try:
            expanded = str(hou.expandString(raw))
            return _result(True, data={"original": raw, "expanded": expanded})
        except Exception as e:
            return _result(False, errors=[f"path.expand_string failed: {e}"])

    if op == "path.file_exists":
        raw = str(args.get("file_path") or args.get("path") or "")
        if not raw.strip():
            return _result(False, errors=["path.file_exists requires file_path (or path)"])
        if dry_run:
            return _result(True, data={"preview": f"path.file_exists {raw!r}"})
        try:
            import os

            expanded = str(hou.expandString(raw.strip()))
            is_file = os.path.isfile(expanded)
            is_dir = os.path.isdir(expanded)
            return _result(
                True,
                data={
                    "original": raw.strip(),
                    "expanded": expanded,
                    "exists": is_file or is_dir,
                    "is_file": is_file,
                    "is_dir": is_dir,
                },
            )
        except Exception as e:
            return _result(False, errors=[f"path.file_exists failed: {e}"])

    if op == "cache.clear_all":
        if dry_run:
            return _result(True, data={"preview": "cache.clear_all"})
        try:
            okc, method, att = _clear_session_caches_best_effort(hou)
            if okc:
                return _result(
                    True,
                    data={"cleared": True, "method": method},
                    warnings=att or None,
                )
            return _result(
                False,
                errors=["cache.clear_all: no cache clear API succeeded"],
                data={"attempts": att},
            )
        except Exception as e:
            return _result(False, errors=[f"cache.clear_all failed: {e}"])

    if op == "cache.pdg_clear":
        if dry_run:
            return _result(True, data={"preview": "cache.pdg_clear"})
        tried: list[str] = []
        ok_any = False
        target_top = str(args.get("node_path") or "").strip()
        pdg_mod = getattr(hou, "pdg", None)
        if pdg_mod is not None:
            for meth in ("clearAllWorkItemCaches", "clearAllCaches", "clearCache"):
                fn = getattr(pdg_mod, meth, None)
                if callable(fn):
                    try:
                        fn()
                        tried.append(f"hou.pdg.{meth}")
                        ok_any = True
                        break
                    except Exception as e:
                        tried.append(f"hou.pdg.{meth}: {e}")
        if not ok_any and target_top:
            tn = hou.node(target_top)
            if tn is None:
                tried.append(f"target node not found: {target_top}")
            else:
                pdgn_fn = getattr(tn, "pdgNode", None)
                if callable(pdgn_fn):
                    try:
                        pn = pdgn_fn()
                    except Exception as e:
                        pn = None
                        tried.append(f"{target_top}.pdgNode: {e}")
                    if pn is not None:
                        for meth in ("clearCache", "clearAllCaches", "dirtyAllTasks", "dirtyAllWorkItems"):
                            fn = getattr(pn, meth, None)
                            if callable(fn):
                                try:
                                    fn()
                                    tried.append(f"{target_top}.pdgNode().{meth}")
                                    ok_any = True
                                    break
                                except Exception as e:
                                    tried.append(f"{target_top}.pdgNode().{meth}: {e}")
                else:
                    tried.append(f"{target_top}: no pdgNode()")
        if not ok_any:
            try:
                import pdg as _pdg  # type: ignore

                ctx = getattr(_pdg, "Context", None) or getattr(_pdg, "graphContext", None)
                if ctx is not None and hasattr(ctx, "active"):
                    c = ctx.active()
                    if c is not None and hasattr(c, "clearAllCaches"):
                        c.clearAllCaches()
                        tried.append("pdg.Context.active().clearAllCaches")
                        ok_any = True
            except Exception as e:
                tried.append(f"import pdg: {e}")
        warn_out: list[str] = []
        if not ok_any:
            okc, method, att = _clear_session_caches_best_effort(hou)
            if okc:
                ok_any = True
                tried.append(f"fallback:{method}")
                warn_out.append(
                    "cache.pdg_clear used fallback scene cache clear; PDG-specific API not available in this Houdini build."
                )
            if att:
                tried.extend(att)
        if ok_any:
            return _result(True, warnings=warn_out or None, data={"cleared": True, "methods": tried})
        return _result(False, errors=["cache.pdg_clear: no compatible PDG cache clear API in this build"], data={"tried": tried})

    if op == "top.workitems_scan":
        path = str(args.get("node_path") or "")
        max_items = int(args.get("max_items", 20) or 20)
        cook_first = bool(args.get("cook_first", True))
        tops_only = bool(args.get("tops_only", False))
        generate_only = bool(args.get("generate_only", False))
        if max_items < 1:
            max_items = 1
        if max_items > 200:
            max_items = 200
        if not path.strip():
            return _result(False, errors=["top.workitems_scan requires node_path"])
        if dry_run:
            return _result(
                True,
                data={
                    "preview": (
                        f"top.workitems_scan {path!r} max_items={max_items} "
                        f"cook_first={cook_first} tops_only={tops_only} generate_only={generate_only}"
                    )
                },
            )
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            data: dict[str, Any] = {
                "node_path": n.path(),
                "type_name": n.type().nameWithCategory(),
                "max_items": max_items,
                "cook_first": cook_first,
                "tops_only": tops_only,
                "generate_only": generate_only,
            }
            warn: list[str] = []

            def _fill_work_items_from_pdg(pn: Any, access_how: str) -> None:
                data["pdg_access_method"] = access_how
                data["pdg_node_type"] = type(pn).__name__ if pn is not None else None
                if pn is None:
                    return
                wi_list: list[Any] = []
                if hasattr(pn, "workItems") and not callable(getattr(pn, "workItems")):
                    try:
                        raw = pn.workItems
                        wi_list = list(raw) if raw is not None else []
                    except Exception:
                        wi_list = []
                else:
                    wi_fn = getattr(pn, "workItems", None)
                    if callable(wi_fn):
                        try:
                            wis = wi_fn()
                            wi_list = list(wis) if wis is not None else []
                        except Exception as e:
                            warn.append(f"workItems(): {e}")
                if not wi_list:
                    return
                data["work_item_count"] = len(wi_list)
                samples: list[dict[str, Any]] = []
                for wi in wi_list[:max_items]:
                    row: dict[str, Any] = {}
                    try:
                        row["id"] = int(getattr(wi, "id", -1))
                    except Exception:
                        pass
                    try:
                        row["name"] = str(getattr(wi, "name", ""))
                    except Exception:
                        pass
                    st = getattr(wi, "state", None)
                    if st is not None:
                        try:
                            row["state"] = str(st)
                        except Exception:
                            pass
                    if row:
                        samples.append(row)
                data["work_items"] = samples
                data["truncated"] = len(wi_list) > max_items

            pn0, how0 = _top_try_get_pdg_node(n)
            pn1: Any | None = None
            how1: str | None = None
            _fill_work_items_from_pdg(pn0, how0 or "none")
            if ("work_items" not in data) and cook_first:
                cwi = getattr(n, "cookWorkItems", None)
                if callable(cwi):
                    try:
                        cwi(block=True, tops_only=tops_only, generate_only=generate_only, nodes=())
                        data["cook_work_items_called"] = True
                    except TypeError:
                        try:
                            cwi(block=True)
                            data["cook_work_items_called"] = True
                        except Exception as e:
                            warn.append(f"cookWorkItems: {e}")
                    except Exception as e:
                        warn.append(f"cookWorkItems: {e}")
                else:
                    warn.append("top.workitems_scan: node has no cookWorkItems()")
                pn1, how1 = _top_try_get_pdg_node(n)
                _fill_work_items_from_pdg(pn1, how1 or how0 or "none")
            if "work_items" not in data and not how0:
                warn.append(
                    "top.workitems_scan: no getPDGNode()/pdgNode() on this node (not a TOP node?)"
                )
            elif "work_items" not in data and pn0 is None and (pn1 is None):
                warn.append(
                    "top.workitems_scan: PDG node is None until the TOP graph is cooked "
                    "(try cook_first=true or cook the TOP network in the UI)."
                )
            no_work_items = "work_items" not in data
            zero_items = data.get("work_item_count") == 0
            if no_work_items or zero_items:
                c_rows, c_sug = _top_workitems_child_hints(n, limit=40)
                if c_rows:
                    data["child_nodes"] = c_rows
                    data["child_count"] = len(c_rows)
                if c_sug:
                    data["suggested_child_nodes"] = c_sug
                    warn.append("top.workitems_scan: re-run on a suggested_child_nodes path to read work items.")
            return _result(True, warnings=warn or None, data=data)
        except Exception as e:
            return _result(False, errors=[f"top.workitems_scan failed: {e}"])

    if op == "hda.ensure_file":
        file_path = str(args.get("file_path") or args.get("path") or "")
        if not file_path.strip():
            return _result(False, errors=["hda.ensure_file requires file_path"])
        if dry_run:
            return _result(True, data={"preview": f"hda.ensure_file {file_path!r}"})
        try:
            fp = str(hou.expandString(file_path.strip()))
            inst = getattr(hou.hda, "installFile", None)
            if not callable(inst):
                return _result(False, errors=["hda.ensure_file: hou.hda.installFile not available"])
            inst(fp)
            return _result(True, data={"file_path": fp, "method": "hou.hda.installFile"})
        except Exception as e:
            return _result(False, errors=[f"hda.ensure_file failed: {e}"])

    if op == "io.file_parms_guess":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["io.file_parms_guess requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"io.file_parms_guess {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            hits: list[str] = []
            KEY = ("file", "path", "filepath", "filename", "abc", "usd", "fbx", "bgeo", "hip", "texture", "image")

            ptg = n.parmTemplateGroup()

            def walk(pt: Any) -> None:
                try:
                    subs = getattr(pt, "parmTemplates", None)
                    if callable(subs):
                        for ch in subs():
                            walk(ch)
                    nm = pt.name()
                    nlow = nm.lower()
                    if isinstance(pt, hou.StringParmTemplate) and any(k in nlow for k in KEY):
                        hits.append(nm)
                except Exception:
                    pass

            for pt in ptg.parmTemplates():
                walk(pt)
            hits = sorted(set(hits))
            return _result(True, data={"node_path": n.path(), "candidate_parm_names": hits, "count": len(hits)})
        except Exception as e:
            return _result(False, errors=[f"io.file_parms_guess failed: {e}"])

    if op == "chop.parm_channel_state":
        path = str(args.get("node_path") or "")
        pname = str(args.get("parm_name") or "")
        comp_raw = args.get("component")
        if not path.strip() or not pname.strip():
            return _result(False, errors=["chop.parm_channel_state requires node_path and parm_name"])
        if dry_run:
            return _result(True, data={"preview": f"chop.parm_channel_state {path!r}.{pname}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            p = n.parm(pname.strip())
            comp_idx = None
            if p is None and hasattr(n, "parmTuple"):
                try:
                    ptup = n.parmTuple(pname.strip())
                    if ptup is not None:
                        comp_idx = int(comp_raw) if comp_raw is not None else 0
                        comp_idx = max(0, min(comp_idx, len(ptup) - 1))
                        p = ptup[comp_idx]
                except Exception:
                    p = None
            if p is None:
                return _result(False, errors=[f"Parm not found: {path}.{pname}"])
            expr = ""
            try:
                expr = str(p.expression() or "")
            except Exception:
                pass
            raw = None
            try:
                raw = p.rawValue()
            except Exception:
                pass
            kc = None
            kfn = getattr(p, "keyframes", None)
            if callable(kfn):
                try:
                    ks = kfn()
                    kc = len(ks) if ks is not None else 0
                except Exception:
                    kc = None
            ref_like = bool(expr.strip()) or (kc or 0) > 0 or ("ch(" in expr)
            return _result(
                True,
                data={
                    "node_path": n.path(),
                    "parm_name": pname.strip(),
                    "component": comp_idx,
                    "has_expression": bool(expr.strip()),
                    "expression_preview": expr[:2000] if expr else "",
                    "raw_value_preview": str(raw)[:500] if raw is not None else None,
                    "keyframe_count": kc,
                    "likely_ch_reference": ref_like,
                },
            )
        except Exception as e:
            return _result(False, errors=[f"chop.parm_channel_state failed: {e}"])

    if op == "lop.usd_layer_stack":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["lop.usd_layer_stack requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"lop.usd_layer_stack {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            st_fn = getattr(n, "stage", None)
            if not callable(st_fn):
                return _result(False, errors=["lop.usd_layer_stack: node has no .stage()"])
            stage = st_fn()
            if stage is None:
                return _result(False, errors=["lop.usd_layer_stack: stage() returned None"])
            out: dict[str, Any] = {"node_path": path.strip()}
            rl = stage.GetRootLayer()
            if rl is not None:
                out["root_layer_identifier"] = getattr(rl, "identifier", None) or str(rl)
                slp = getattr(rl, "subLayerPaths", None)
                out["sub_layer_paths"] = list(slp)[:256] if slp is not None else []
            return _result(True, data=out)
        except Exception as e:
            return _result(False, errors=[f"lop.usd_layer_stack failed: {e}"])

    if op == "viewport.flipbook":
        output_path = str(args.get("output_path") or args.get("path") or args.get("file_path") or "")
        if not output_path.strip():
            return _result(False, errors=["viewport.flipbook requires output_path (or path / file_path); needs GUI"])
        if dry_run:
            return _result(True, data={"preview": f"viewport.flipbook -> {output_path!r}"})
        try:
            import glob as _glob
            import os as _os
            import re as _re

            ui = getattr(hou, "ui", None)
            if ui is None:
                return _result(False, errors=["viewport.flipbook: hou.ui not available (headless?)"])
            outp_raw = output_path.strip()
            desktop = ui.curDesktop()
            flip_tab = None
            for pt in desktop.paneTabs():
                try:
                    if pt.type() == hou.paneTabType.SceneViewer:
                        flip_tab = pt
                        break
                except Exception:
                    continue
            if flip_tab is None:
                return _result(False, errors=["viewport.flipbook: no SceneViewer pane tab"])
            af_flip = _mcp_autoframe_sceneviewer(hou, flip_tab, args)
            fb = getattr(flip_tab, "flipbook", None)
            if not callable(fb):
                return _result(False, errors=["viewport.flipbook: pane has no flipbook() in this build"])
            # Ensure output folder exists when output is a file sequence path.
            try:
                out_dir_raw = _os.path.dirname(outp_raw)
                out_dir = str(hou.expandString(out_dir_raw)) if out_dir_raw else ""
                if out_dir:
                    _os.makedirs(out_dir, exist_ok=True)
            except Exception:
                pass

            vpt = None
            try:
                vpt = flip_tab.curViewport()
            except Exception:
                vpt = None

            settings = None
            try:
                sb = flip_tab.flipbookSettings()
                settings = sb.stash() if hasattr(sb, "stash") else sb
            except Exception:
                settings = None

            if settings is not None:
                # Try known setting APIs across Houdini builds.
                for set_name in ("output", "setOutputPath", "setOutput", "setFilename", "setOutputFile", "filename"):
                    m = getattr(settings, set_name, None)
                    if callable(m):
                        try:
                            m(outp_raw)
                            break
                        except Exception:
                            continue
                for mp_name, mp_val in (
                    ("outputToMPlay", False),
                    ("setOutputToMPlay", False),
                    ("setUseMPlay", False),
                ):
                    m = getattr(settings, mp_name, None)
                    if callable(m):
                        try:
                            m(mp_val)
                            break
                        except Exception:
                            continue

                sf = args.get("start_frame")
                ef = args.get("end_frame")
                if sf is not None and ef is not None:
                    for fr_name in ("frameRange", "setFrameRange"):
                        m = getattr(settings, fr_name, None)
                        if callable(m):
                            try:
                                m((float(sf), float(ef)))
                                break
                            except Exception:
                                continue

            # Match Houdini signature variants:
            #   flipbook(viewport, settings)   (most common)
            #   flipbook(settings, viewport)   (legacy variant)
            #   flipbook(settings)
            #   flipbook()
            called = False
            errs: list[str] = []
            if settings is not None and vpt is not None:
                for call in (
                    lambda: fb(vpt, settings),
                    lambda: fb(settings, vpt),
                ):
                    try:
                        call()
                        called = True
                        break
                    except Exception as e:
                        errs.append(str(e))
            if not called and settings is not None:
                try:
                    fb(settings)
                    called = True
                except Exception as e:
                    errs.append(str(e))
            if not called:
                try:
                    fb()
                    called = True
                except Exception as e:
                    errs.append(str(e))
            if not called:
                joined = "; ".join(errs[-3:]) if errs else "unknown signature mismatch"
                return _result(False, errors=[f"viewport.flipbook failed: {joined}"])

            # Best-effort output verification: fail if target pattern produced zero files.
            out_dir_raw = _os.path.dirname(outp_raw)
            out_name_raw = _os.path.basename(outp_raw)
            out_dir = str(hou.expandString(out_dir_raw)) if out_dir_raw else ""
            if not out_dir:
                out_dir = "."
            frame_token = _re.search(r"\$F\d*", out_name_raw)
            if frame_token:
                pre = out_name_raw[: frame_token.start()]
                post = out_name_raw[frame_token.end() :]
                glob_pat = _os.path.join(out_dir, f"{pre}*{post}")
            else:
                glob_pat = _os.path.join(out_dir, out_name_raw)
            # Expand non-frame vars for filesystem check.
            glob_pat = str(hou.expandString(glob_pat))
            files = sorted(_glob.glob(glob_pat))
            if not files:
                return _result(
                    False,
                    errors=[
                        "viewport.flipbook produced no files on disk; "
                        f"checked pattern: {glob_pat}"
                    ],
                )
            return _result(
                True,
                data={
                    "output_path": str(hou.expandString(outp_raw)),
                    "output_template": outp_raw,
                    "note": "Blocking flipbook; may fail on farm/headless.",
                    "start_frame": args.get("start_frame"),
                    "end_frame": args.get("end_frame"),
                    "output_glob": glob_pat,
                    "file_count": len(files),
                    "viewport_autoframe": af_flip,
                },
            )
        except Exception as e:
            return _result(False, errors=[f"viewport.flipbook failed: {e}"])

    if op == "node.list_inputs":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["node.list_inputs requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"list_inputs {path!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            n_in = int(n.type().maxNumInputs())
        except Exception:
            try:
                n_in = len(n.inputConnections())
            except Exception:
                n_in = 0
        inputs: list[dict[str, Any]] = []
        for i in range(max(0, n_in)):
            sn = None
            try:
                sn = n.inputNode(i)
            except Exception:
                sn = None
            inputs.append(
                {
                    "index": i,
                    "connected": sn is not None,
                    "source_path": sn.path() if sn is not None else None,
                }
            )
        return _result(True, data={"node_path": path, "inputs": inputs})

    if op == "node.list_outputs":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["node.list_outputs requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"list_outputs {path!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            outs = n.outputConnections()
        except Exception:
            outs = []
        data_out: list[dict[str, Any]] = []
        for oc in outs:
            try:
                idx = int(oc.inputIndex())
            except Exception:
                idx = -1
            try:
                dn = oc.outputNode()
                dp = dn.path() if dn is not None else None
            except Exception:
                dp = None
            data_out.append({"dst_input_index": idx, "dst_node_path": dp})
        return _result(True, data={"node_path": path, "outputs": data_out})

    if op == "node.change_type":
        path = str(args.get("node_path") or "")
        new_type = str(args.get("node_type") or args.get("type_name") or "")
        force = bool(args.get("force", False))
        if not path.strip() or not new_type.strip():
            return _result(False, errors=["node.change_type requires node_path and node_type"])
        if dry_run:
            return _result(True, data={"preview": f"change_type {path!r} -> {new_type!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            nn = n.changeNodeType(new_type.strip(), force_change_op_type=force)
            return _result(True, data={"node_path": nn.path(), "type_name": nn.type().name()})
        except Exception as e:
            return _result(False, errors=[f"node.change_type failed: {e}"])

    if op == "node.match_definition":
        path = str(args.get("node_path") or "")
        defn = str(args.get("definition") or args.get("type_name") or "")
        if not path.strip() or not defn.strip():
            return _result(False, errors=["node.match_definition requires node_path and definition"])
        if dry_run:
            return _result(True, data={"preview": f"match_definition {path!r} -> {defn!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        if not hasattr(n, "matchDefinition"):
            return _result(False, errors=["node.match_definition: Node.matchDefinition not available in this Houdini build"])
        try:
            n.matchDefinition(defn.strip())
            return _result(True, data={"node_path": n.path(), "definition": defn.strip()})
        except Exception as e:
            return _result(False, errors=[f"node.match_definition failed: {e}"])

    if op == "rop.evaluate_path":
        path = str(args.get("node_path") or "")
        channel = str(args.get("channel") or args.get("parm_name") or "picture")
        frame = args.get("frame")
        if not path.strip():
            return _result(False, errors=["rop.evaluate_path requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"rop.evaluate_path {path!r} ch={channel!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            fnum = float(frame) if frame is not None else float(hou.frame())
        except Exception:
            fnum = float(hou.frame())

        def _resolve_output_parm() -> hou.Parm | None:
            want = channel.strip()
            if want:
                p_try = n.parm(want)
                if p_try is not None:
                    return p_try
            if hasattr(n, "renderParmPath"):
                try:
                    rp = n.renderParmPath()
                    pp = hou.parm(rp) if rp else None
                    if pp is not None:
                        return pp
                except Exception:
                    pass
            for cand in ("picture", "vm_picture", "copoutputpath", "soho_diskfile"):
                pc = n.parm(cand)
                if pc is not None:
                    return pc
            return None

        try:
            p = _resolve_output_parm()
            if p is None:
                return _result(
                    False,
                    errors=[
                        "rop.evaluate_path: could not resolve an output path parm "
                        f"(tried channel={channel!r} and defaults picture/vm_picture/copoutputpath/soho_diskfile)"
                    ],
                )
            out = str(p.evalAtTime(fnum))
            used = p.name()
            return _result(
                True,
                data={
                    "node_path": path,
                    "channel": used,
                    "frame": fnum,
                    "path": out,
                },
            )
        except Exception as e:
            return _result(False, errors=[f"rop.evaluate_path failed: {e}"])

    if op == "geo.bounding_box":
        path = str(args.get("node_path") or "")
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.bounding_box requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.bounding_box {path!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            bb = g.boundingBox()
            return _result(
                True,
                data={
                    "node_path": path,
                    "min": [float(bb.minvec().x()), float(bb.minvec().y()), float(bb.minvec().z())],
                    "max": [float(bb.maxvec().x()), float(bb.maxvec().y()), float(bb.maxvec().z())],
                    "center": [
                        float((bb.minvec().x() + bb.maxvec().x()) * 0.5),
                        float((bb.minvec().y() + bb.maxvec().y()) * 0.5),
                        float((bb.minvec().z() + bb.maxvec().z()) * 0.5),
                    ],
                    "size": [
                        float(bb.maxvec().x() - bb.minvec().x()),
                        float(bb.maxvec().y() - bb.minvec().y()),
                        float(bb.maxvec().z() - bb.minvec().z()),
                    ],
                },
            )
        except Exception as e:
            return _result(False, errors=[f"geo.bounding_box failed: {e}"])

    if op == "geo.point_count":
        path = str(args.get("node_path") or "")
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.point_count requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.point_count {path!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            cnt = None
            for nm in ("numPoints", "pointCount"):
                fn = getattr(g, nm, None)
                if callable(fn):
                    try:
                        cnt = int(fn())
                        break
                    except Exception:
                        continue
            if cnt is None:
                try:
                    cnt = len(g.points())
                except Exception:
                    cnt = None
            if cnt is None:
                return _result(False, errors=["geo.point_count: could not determine point count"])
            return _result(True, data={"node_path": path, "num_points": cnt})
        except Exception as e:
            return _result(False, errors=[f"geo.point_count failed: {e}"])

    if op == "geo.interpolate_p":
        path = str(args.get("node_path") or "")
        u = float(args.get("u", 0.0))
        v = float(args.get("v", 0.0))
        prim_index = int(args.get("prim_index", args.get("primitive", 0)))
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.interpolate_p requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.interpolate_p {path!r} prim={prim_index}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            prims = g.prims()
            if prim_index < 0 or prim_index >= len(prims):
                return _result(False, errors=[f"geo.interpolate_p: prim_index out of range: {prim_index}"])
            prim = prims[prim_index]
            pai = getattr(prim, "positionAtInterior", None)
            if not callable(pai):
                return _result(False, errors=["geo.interpolate_p: Prim.positionAtInterior not available in this Houdini build"])
            try:
                pos = pai(float(u), float(v), 0.0)
            except Exception as e:
                return _result(False, errors=[f"geo.interpolate_p: positionAtInterior failed: {e}"])
            return _result(
                True,
                data={"node_path": path, "prim_index": prim_index, "u": u, "v": v, "P": [float(pos.x()), float(pos.y()), float(pos.z())]},
            )
        except Exception as e:
            return _result(False, errors=[f"geo.interpolate_p failed: {e}"])

    if op == "attrib.summary":
        path = str(args.get("node_path") or "")
        scope = str(args.get("scope") or args.get("attrib_type") or "point").lower()
        name = str(args.get("name") or args.get("attrib_name") or "")
        max_elements = int(args.get("max_elements", 8))
        max_elements = max(1, min(max_elements, 64))
        force_cook = bool(args.get("force_cook", True))
        if not path.strip() or not name.strip():
            return _result(False, errors=["attrib.summary requires node_path and name"])
        if dry_run:
            return _result(True, data={"preview": f"attrib.summary {path!r} {scope}:{name}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            if scope in ("pt", "point"):
                it = g.points()
            elif scope in ("prim", "primitive"):
                it = g.prims()
            elif scope in ("vtx", "vertex", "vertices"):
                gv = getattr(g, "vertices", None)
                if callable(gv):
                    try:
                        it = gv()
                    except Exception:
                        it = []
                else:
                    vtxs: list[Any] = []
                    try:
                        for pr in g.prims():
                            try:
                                vtxs.extend(pr.vertices())
                            except Exception:
                                continue
                    except Exception:
                        vtxs = []
                    it = vtxs
            else:
                return _result(False, errors=[f"attrib.summary: unknown scope {scope!r} (use point|prim|vertex)"])

            a0 = None
            for x in it:
                try:
                    a = x.attribValue(name)
                except Exception:
                    a = None
                if a is not None:
                    a0 = a
                    break
            if a0 is None:
                return _result(False, errors=[f"attrib.summary: attribute not found or empty: {name!r}"])
            size = 1
            try:
                if hasattr(a0, "__len__") and not isinstance(a0, (str, bytes)):
                    size = int(len(a0))
            except Exception:
                size = 1

            samples: list[Any] = []
            total_el: int | None = None
            if scope in ("pt", "point") and hasattr(g, "numPoints"):
                try:
                    total_el = int(g.numPoints())
                except Exception:
                    total_el = None
            elif scope in ("prim", "primitive") and hasattr(g, "numPrims"):
                try:
                    total_el = int(g.numPrims())
                except Exception:
                    total_el = None
            n_samp = 0
            for idx, x in enumerate(it):
                if idx >= max_elements:
                    break
                try:
                    samples.append(x.attribValue(name))
                except Exception:
                    samples.append(None)
                n_samp += 1
            trunc = (total_el is not None and total_el > max_elements) or (n_samp >= max_elements)

            return _result(
                True,
                data={
                    "node_path": path,
                    "scope": scope,
                    "name": name,
                    "size": size,
                    "max_elements_cap": max_elements,
                    "element_count": total_el,
                    "first_non_null_sample": a0,
                    "samples": samples,
                    "truncated": trunc,
                    "truncate_reason": "max_elements" if trunc else None,
                },
            )
        except Exception as e:
            return _result(False, errors=[f"attrib.summary failed: {e}"])

    if op == "exec.render_write":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["exec.render_write requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"render_write {path!r} (blocking)"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        if not hasattr(n, "render"):
            return _result(False, errors=[f"exec.render_write: node has no .render(): {path!r}"])
        try:
            if hasattr(n, "execute"):
                n.execute()
            else:
                n.render()
            return _result(True, data={"node_path": path, "written": True})
        except Exception as e:
            return _result(False, errors=[f"exec.render_write failed: {e}"])

    if op == "network.set_current_node":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["network.set_current_node requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"set_current_node {path!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            n.setCurrent(True, clear_all_selected=True)
            return _result(True, data={"node_path": path})
        except Exception as e:
            return _result(False, errors=[f"network.set_current_node failed: {e}"])

    if op == "node.reparent":
        path = str(args.get("node_path") or "")
        parent_path = str(args.get("parent_path") or args.get("new_parent_path") or "")
        if not path.strip() or not parent_path.strip():
            return _result(False, errors=["node.reparent requires node_path and parent_path"])
        if dry_run:
            return _result(True, data={"preview": f"reparent {path!r} -> {parent_path!r}"})
        ch = hou.node(path)
        pa = hou.node(parent_path.strip())
        if ch is None:
            return _result(False, errors=[f"Node not found: {path}"])
        if pa is None:
            return _result(False, errors=[f"Parent not found: {parent_path}"])
        try:
            if hasattr(hou, "moveNodesTo"):
                hou.moveNodesTo([ch], pa)
            else:
                ch.setParent(pa)
            return _result(True, data={"node_path": ch.path(), "parent_path": pa.path()})
        except Exception as e:
            return _result(False, errors=[f"node.reparent failed: {e}"])

    if op == "geo.save_to_file":
        path = str(args.get("node_path") or "")
        file_path = str(args.get("file_path") or args.get("path") or "")
        force_cook = bool(args.get("force_cook", True))
        mkdirs = bool(args.get("mkdirs", True))
        if not path.strip() or not file_path.strip():
            return _result(False, errors=["geo.save_to_file requires node_path and file_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.save_to_file {path!r} -> {file_path!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            fp = file_path.strip()
            if mkdirs:
                import os

                d = os.path.dirname(fp)
                if d:
                    try:
                        os.makedirs(d, exist_ok=True)
                    except Exception:
                        pass
            saver = getattr(g, "saveToFile", None)
            if callable(saver):
                saver(fp)
            else:
                wbf = getattr(g, "writeToFile", None)
                if callable(wbf):
                    wbf(fp)
                else:
                    return _result(False, errors=["geo.save_to_file: geometry has no saveToFile/writeToFile in this build"])
            return _result(True, data={"node_path": path, "file_path": fp})
        except Exception as e:
            return _result(False, errors=[f"geo.save_to_file failed: {e}"])

    if op == "geo.primitive_count":
        path = str(args.get("node_path") or "")
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.primitive_count requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.primitive_count {path!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            cnt = None
            for nm in ("numPrims", "numPrimitives", "primCount"):
                fn = getattr(g, nm, None)
                if callable(fn):
                    try:
                        cnt = int(fn())
                        break
                    except Exception:
                        continue
            if cnt is None:
                try:
                    cnt = len(g.prims())
                except Exception:
                    cnt = None
            if cnt is None:
                return _result(False, errors=["geo.primitive_count: could not determine primitive count"])
            return _result(True, data={"node_path": path, "num_primitives": cnt})
        except Exception as e:
            return _result(False, errors=[f"geo.primitive_count failed: {e}"])

    if op == "geo.vertex_count":
        path = str(args.get("node_path") or "")
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.vertex_count requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.vertex_count {path!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            cnt = None
            for nm in ("numVertices", "vertexCount"):
                fn = getattr(g, nm, None)
                if callable(fn):
                    try:
                        cnt = int(fn())
                        break
                    except Exception:
                        continue
            if cnt is None:
                try:
                    gv = getattr(g, "vertices", None)
                    if callable(gv):
                        cnt = len(gv())
                    else:
                        cnt = 0
                        for pr in g.prims():
                            try:
                                cnt += len(pr.vertices())
                            except Exception:
                                continue
                except Exception:
                    cnt = None
            if cnt is None:
                return _result(False, errors=["geo.vertex_count: could not determine vertex count"])
            return _result(True, data={"node_path": path, "num_vertices": cnt})
        except Exception as e:
            return _result(False, errors=[f"geo.vertex_count failed: {e}"])

    if op == "attrib.exists":
        path = str(args.get("node_path") or "")
        name = str(args.get("name") or args.get("attrib_name") or "")
        scope = str(args.get("scope") or "point").lower()
        force_cook = bool(args.get("force_cook", True))
        if not path.strip() or not name.strip():
            return _result(False, errors=["attrib.exists requires node_path and name"])
        if dry_run:
            return _result(True, data={"preview": f"attrib.exists {path!r} {scope}:{name}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            exists = False
            if scope in ("pt", "point"):
                exists = g.findPointAttrib(name) is not None
            elif scope in ("prim", "primitive"):
                exists = g.findPrimAttrib(name) is not None
            elif scope in ("vtx", "vertex", "vertices"):
                exists = g.findVertexAttrib(name) is not None
            elif scope in ("detail", "global", "geo"):
                exists = g.findGlobalAttrib(name) is not None
            else:
                return _result(False, errors=[f"attrib.exists: unknown scope {scope!r}"])
            return _result(True, data={"node_path": path, "scope": scope, "name": name, "exists": exists})
        except Exception as e:
            return _result(False, errors=[f"attrib.exists failed: {e}"])

    if op == "parm.press_button":
        path = str(args.get("node_path") or "")
        parm_name = str(args.get("parm_name") or "")
        if not path.strip() or not parm_name.strip():
            return _result(False, errors=["parm.press_button requires node_path and parm_name"])
        if dry_run:
            return _result(True, data={"preview": f"press_button {path!r}.{parm_name}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            p = n.parm(parm_name.strip())
            if p is None:
                return _result(False, errors=[f"Parm not found: {path}.{parm_name}"])
            if hasattr(p, "pressButton"):
                p.pressButton()
            else:
                return _result(False, errors=["parm.press_button: parm has no pressButton()"])
            return _result(True, data={"node_path": path, "parm_name": parm_name.strip()})
        except Exception as e:
            return _result(False, errors=[f"parm.press_button failed: {e}"])

    if op == "parm.multiparm_resize":
        path = str(args.get("node_path") or "")
        folder = str(args.get("folder_parm") or args.get("parm_name") or "")
        raw_c = args.get("count", args.get("num_instances"))
        if not path.strip() or not folder.strip() or raw_c is None:
            return _result(False, errors=["parm.multiparm_resize requires node_path, folder_parm (or parm_name), and count"])
        if dry_run:
            return _result(True, data={"preview": f"multiparm_resize {path!r}.{folder}={raw_c!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            cnt = int(raw_c)
            p = n.parm(folder.strip())
            if p is None:
                return _result(False, errors=[f"Folder parm not found: {path}.{folder}"])
            p.set(cnt)
            return _result(True, data={"node_path": path, "folder_parm": folder.strip(), "count": cnt})
        except Exception as e:
            return _result(False, errors=[f"parm.multiparm_resize failed: {e}"])

    if op == "parm.clear_keyframes":
        path = str(args.get("node_path") or "")
        parm_name = str(args.get("parm_name") or "")
        if not path.strip() or not parm_name.strip():
            return _result(False, errors=["parm.clear_keyframes requires node_path and parm_name"])
        if dry_run:
            return _result(True, data={"preview": f"clear_keyframes {path!r}.{parm_name}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            name = parm_name.strip()
            p = n.parm(name)
            if p is not None and hasattr(p, "deleteAllKeyframes"):
                p.deleteAllKeyframes()
                return _result(True, data={"node_path": path, "parm_name": name, "target": "parm"})
            pt = None
            if hasattr(n, "parmTuple"):
                try:
                    pt = n.parmTuple(name)
                except Exception:
                    pt = None
            if pt is not None and hasattr(pt, "deleteAllKeyframes"):
                pt.deleteAllKeyframes()
                return _result(True, data={"node_path": path, "parm_name": name, "target": "parmTuple"})
            return _result(False, errors=[f"parm.clear_keyframes: no deleteAllKeyframes for {path}.{name}"])
        except Exception as e:
            return _result(False, errors=[f"parm.clear_keyframes failed: {e}"])

    if op == "hip.session_info":
        if dry_run:
            return _result(True, data={"preview": "hip.session_info"})
        try:
            hp = ""
            try:
                hp = str(hou.hipFile.path())
            except Exception:
                hp = ""
            unsaved = None
            try:
                unsaved = bool(hou.hipFile.hasUnsavedChanges())
            except Exception:
                unsaved = None
            return _result(
                True,
                data={
                    "hip_path": hp,
                    "has_unsaved_changes": unsaved,
                },
            )
        except Exception as e:
            return _result(False, errors=[f"hip.session_info failed: {e}"])

    if op == "timeline.get_state":
        if dry_run:
            return _result(True, data={"preview": "timeline.get_state"})
        try:
            start = end = None
            try:
                rng = hou.playbar.playbackRange()
                start = float(rng[0])
                end = float(rng[1])
            except Exception:
                pass
            return _result(
                True,
                data={
                    "frame": float(hou.frame()),
                    "fps": float(hou.fps()),
                    "playback_start": start,
                    "playback_end": end,
                },
            )
        except Exception as e:
            return _result(False, errors=[f"timeline.get_state failed: {e}"])

    if op == "viewport.frame_selected":
        if dry_run:
            return _result(True, data={"preview": "viewport.frame_selected"})
        try:
            ui = getattr(hou, "ui", None)
            if ui is None:
                return _result(False, errors=["viewport.frame_selected: hou.ui not available"])
            desktop = ui.curDesktop()
            framed = False
            for pt in desktop.paneTabs():
                try:
                    if pt.type() != hou.paneTabType.SceneViewer:
                        continue
                    vp = pt.curViewport()
                    fn = getattr(vp, "frameSelected", None) or getattr(vp, "frameSelection", None)
                    if callable(fn):
                        fn()
                        framed = True
                        break
                except Exception:
                    continue
            if not framed:
                return _result(False, errors=["viewport.frame_selected: no SceneViewer viewport found or frame failed"])
            return _result(True, data={"framed": True})
        except Exception as e:
            return _result(False, errors=[f"viewport.frame_selected failed: {e}"])

    if op == "material.assign_object":
        obj_path = str(args.get("obj_path") or args.get("object_path") or args.get("node_path") or "")
        mat_path = str(args.get("material_path") or args.get("mat_path") or "")
        if not obj_path.strip() or not mat_path.strip():
            return _result(False, errors=["material.assign_object requires obj_path (or node_path) and material_path"])
        if dry_run:
            return _result(True, data={"preview": f"assign_object material {mat_path!r} -> {obj_path!r}"})
        objn = hou.node(obj_path.strip())
        matn = hou.node(mat_path.strip())
        if objn is None:
            return _result(False, errors=[f"Object node not found: {obj_path}"])
        if matn is None:
            return _result(False, errors=[f"Material node not found: {mat_path}"])
        try:
            method = None
            if hasattr(objn, "setMaterial"):
                try:
                    objn.setMaterial(matn)
                    method = "setMaterial"
                except Exception:
                    method = None
            if method is None:
                # Fallback for builds/node types without setMaterial support.
                p = objn.parm("shop_materialpath")
                if p is not None:
                    p.set(matn.path())
                    method = "parm:shop_materialpath"
            if method is None:
                return _result(False, errors=[f"material.assign_object: no supported material API on {obj_path!r}"])
            return _result(True, data={"obj_path": objn.path(), "material_path": matn.path(), "method": method})
        except Exception as e:
            return _result(False, errors=[f"material.assign_object failed: {e}"])

    if op == "material.clear_object":
        obj_path = str(args.get("obj_path") or args.get("object_path") or args.get("node_path") or "")
        if not obj_path.strip():
            return _result(False, errors=["material.clear_object requires obj_path (or node_path)"])
        if dry_run:
            return _result(True, data={"preview": f"clear_object material {obj_path!r}"})
        objn = hou.node(obj_path.strip())
        if objn is None:
            return _result(False, errors=[f"Object node not found: {obj_path}"])
        try:
            method = None
            if hasattr(objn, "setMaterial"):
                try:
                    objn.setMaterial(None)
                    method = "setMaterial"
                except Exception:
                    method = None
            if method is None:
                p = objn.parm("shop_materialpath")
                if p is not None:
                    p.set("")
                    method = "parm:shop_materialpath"
            if method is None:
                return _result(False, errors=[f"material.clear_object: no supported material API on {obj_path!r}"])
            return _result(True, data={"obj_path": objn.path(), "cleared": True, "method": method})
        except Exception as e:
            return _result(False, errors=[f"material.clear_object failed: {e}"])

    if op == "lop.stage_summary":
        path = str(args.get("node_path") or "")
        max_prims = int(args.get("max_prims", 500000))
        max_prims = max(1, min(max_prims, 5_000_000))
        if not path.strip():
            return _result(False, errors=["lop.stage_summary requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"lop.stage_summary {path!r}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            st_fn = getattr(n, "stage", None)
            if not callable(st_fn):
                return _result(False, errors=["lop.stage_summary: node has no .stage() (not a LOP / Solaris context?)"])
            stage = st_fn()
            if stage is None:
                return _result(False, errors=["lop.stage_summary: stage() returned None"])
            out: dict[str, Any] = {"node_path": path, "stage_type": type(stage).__name__}
            counted: int | None = None
            truncated = False
            warn: list[str] = []
            try:
                tr = getattr(stage, "Traverse", None)
                if callable(tr):
                    it = tr()
                    cnt = 0
                    for _ in it:
                        cnt += 1
                        if cnt >= max_prims:
                            truncated = True
                            break
                    counted = cnt
            except Exception as e0:
                warn.append(f"Traverse: {e0}")
                counted = None
            if counted is None:
                try:
                    from pxr import Usd as _Usd

                    root = stage.GetPseudoRoot()
                    cnt = 0
                    for _ in _Usd.PrimRange(root):
                        cnt += 1
                        if cnt >= max_prims:
                            truncated = True
                            break
                    counted = cnt
                except Exception as e2:
                    out["prim_count_error"] = str(e2)
                    warn.append(f"pxr.PrimRange: {e2}")
            if counted is not None:
                out["prim_count"] = counted
            out["truncated"] = truncated
            out["max_prims"] = max_prims
            if bool(args.get("include_layer_paths", False)):
                try:
                    rl = stage.GetRootLayer()
                    if rl is not None:
                        out["root_layer_identifier"] = getattr(rl, "identifier", None) or str(rl)
                        slp = getattr(rl, "subLayerPaths", None)
                        out["sub_layer_paths"] = list(slp)[:128] if slp is not None else []
                except Exception as el:
                    out["layer_paths_error"] = str(el)
                    warn.append(f"include_layer_paths: {el}")
            return _result(True, data=out, warnings=warn or None)
        except Exception as e:
            return _result(False, errors=[f"lop.stage_summary failed: {e}"])

    if op == "subnet.collapse":
        raw = args.get("node_paths") or args.get("paths") or []
        if isinstance(raw, str):
            raw = [raw]
        subnet_name = args.get("subnet_name") or args.get("name")
        if not isinstance(raw, list) or not raw:
            return _result(False, errors=["subnet.collapse requires non-empty node_paths list"])
        if dry_run:
            return _result(True, data={"preview": f"subnet.collapse n={len(raw)}"})
        fn = getattr(hou, "collapseIntoSubnet", None)
        if not callable(fn):
            return _result(False, errors=["subnet.collapse: hou.collapseIntoSubnet not available in this build"])
        nodes: list[Any] = []
        for p in raw:
            ps = str(p).strip()
            if not ps:
                continue
            nn = hou.node(ps)
            if nn is None:
                return _result(False, errors=[f"subnet.collapse: node not found: {ps}"])
            nodes.append(nn)
        if not nodes:
            return _result(False, errors=["subnet.collapse: no valid nodes"])
        try:
            sn = fn(nodes)
            if sn is None:
                return _result(False, errors=["subnet.collapse: collapseIntoSubnet returned None"])
            if subnet_name is not None and str(subnet_name).strip():
                try:
                    sn.setName(str(subnet_name).strip(), unique_name=True)
                except Exception:
                    pass
            return _result(True, data={"subnet_path": sn.path(), "child_count": len(nodes)})
        except Exception as e:
            return _result(False, errors=[f"subnet.collapse failed: {e}"])

    if op == "hda.definition_save":
        path = str(args.get("node_path") or "")
        file_path = str(args.get("file_path") or args.get("path") or "")
        if not path.strip() or not file_path.strip():
            return _result(False, errors=["hda.definition_save requires node_path and file_path"])
        if dry_run:
            return _result(True, data={"preview": f"hda.definition_save {path!r} -> {file_path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            defn = n.type().definition()
            if defn is None:
                return _result(False, errors=["hda.definition_save: no type definition (not an editable digital asset?)"])
            fp = file_path.strip()
            import os

            d = os.path.dirname(fp)
            if d:
                try:
                    os.makedirs(d, exist_ok=True)
                except Exception:
                    pass
            defn.save(fp)
            return _result(True, data={"node_path": n.path(), "file_path": fp})
        except Exception as e:
            return _result(False, errors=[f"hda.definition_save failed: {e}"])

    if op == "hda.install_file":
        file_path = str(args.get("file_path") or args.get("path") or "")
        if not file_path.strip():
            return _result(False, errors=["hda.install_file requires file_path"])
        if dry_run:
            return _result(True, data={"preview": f"hda.install_file {file_path!r}"})
        try:
            inst = getattr(hou.hda, "installFile", None)
            if not callable(inst):
                return _result(False, errors=["hda.install_file: hou.hda.installFile not available"])
            inst(file_path.strip())
            return _result(True, data={"file_path": file_path.strip()})
        except Exception as e:
            return _result(False, errors=[f"hda.install_file failed: {e}"])

    if op == "parm.keyframe_set":
        path = str(args.get("node_path") or "")
        parm_name = str(args.get("parm_name") or "")
        frame = args.get("frame")
        value = args.get("value")
        comp_raw = args.get("component")
        if not path.strip() or not parm_name.strip() or frame is None or value is None:
            return _result(False, errors=["parm.keyframe_set requires node_path, parm_name, frame, and value"])
        if dry_run:
            return _result(True, data={"preview": f"keyframe_set {path!r}.{parm_name} @{frame}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            name = parm_name.strip()
            p = n.parm(name)
            if p is None and hasattr(n, "parmTuple"):
                try:
                    pt = n.parmTuple(name)
                except Exception:
                    pt = None
                if pt is not None:
                    idx = int(comp_raw) if comp_raw is not None else 0
                    idx = max(0, min(idx, len(pt) - 1))
                    p = pt[idx]
            if p is None:
                return _result(False, errors=[f"parm.keyframe_set: parm not found: {path}.{parm_name}"])
            kf = hou.Keyframe()
            set_frame = getattr(kf, "setFrame", None)
            if callable(set_frame):
                set_frame(float(frame))
            else:
                setattr(kf, "frame", float(frame))
            val_f = float(value)
            set_val = getattr(kf, "setValue", None)
            if callable(set_val):
                set_val(val_f)
            else:
                setattr(kf, "value", val_f)
            p.setKeyframe(kf)
            return _result(True, data={"node_path": path, "parm_name": name, "frame": float(frame), "value": val_f})
        except Exception as e:
            return _result(False, errors=[f"parm.keyframe_set failed: {e}"])

    if op == "parm.keyframe_list":
        path = str(args.get("node_path") or "")
        parm_name = str(args.get("parm_name") or "")
        max_keys = int(args.get("max_keys", 256))
        max_keys = max(1, min(max_keys, 4096))
        comp_raw = args.get("component")
        if not path.strip() or not parm_name.strip():
            return _result(False, errors=["parm.keyframe_list requires node_path and parm_name"])
        if dry_run:
            return _result(True, data={"preview": f"keyframe_list {path!r}.{parm_name}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            name = parm_name.strip()
            p = n.parm(name)
            if p is None and hasattr(n, "parmTuple"):
                try:
                    pt = n.parmTuple(name)
                except Exception:
                    pt = None
                if pt is not None:
                    idx = int(comp_raw) if comp_raw is not None else 0
                    idx = max(0, min(idx, len(pt) - 1))
                    p = pt[idx]
            if p is None:
                return _result(False, errors=[f"parm.keyframe_list: parm not found: {path}.{parm_name}"])
            kfn = getattr(p, "keyframes", None)
            if not callable(kfn):
                return _result(False, errors=["parm.keyframe_list: parm has no keyframes() in this build"])
            keys = kfn()
            rows: list[dict[str, Any]] = []
            for i, k in enumerate(keys):
                if i >= max_keys:
                    break
                fr = getattr(k, "frame", None)
                if callable(fr):
                    try:
                        fr = fr()
                    except Exception:
                        fr = None
                if fr is None:
                    fr = getattr(k, "time", None)
                    if callable(fr):
                        try:
                            fr = fr()
                        except Exception:
                            fr = None
                val = getattr(k, "value", None)
                if callable(val):
                    try:
                        val = val()
                    except Exception:
                        val = None
                rows.append({"frame": float(fr) if fr is not None else None, "value": val})
            return _result(
                True,
                data={"node_path": path, "parm_name": name, "keyframes": rows, "truncated": len(keys) > max_keys},
            )
        except Exception as e:
            return _result(False, errors=[f"parm.keyframe_list failed: {e}"])

    if op == "parm.keyframe_delete_frame":
        path = str(args.get("node_path") or "")
        parm_name = str(args.get("parm_name") or "")
        frame = args.get("frame")
        comp_raw = args.get("component")
        if not path.strip() or not parm_name.strip() or frame is None:
            return _result(False, errors=["parm.keyframe_delete_frame requires node_path, parm_name, and frame"])
        if dry_run:
            return _result(True, data={"preview": f"keyframe_delete_frame {path!r}.{parm_name} @{frame}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            name = parm_name.strip()
            p = n.parm(name)
            if p is None and hasattr(n, "parmTuple"):
                try:
                    pt = n.parmTuple(name)
                except Exception:
                    pt = None
                if pt is not None:
                    idx = int(comp_raw) if comp_raw is not None else 0
                    idx = max(0, min(idx, len(pt) - 1))
                    p = pt[idx]
            if p is None:
                return _result(False, errors=[f"parm.keyframe_delete_frame: parm not found: {path}.{parm_name}"])
            fnum = float(frame)
            del_fn = getattr(p, "deleteKeyframeAtFrame", None)
            if not callable(del_fn):
                del_fn = getattr(p, "deleteKeyframeAtTime", None)
            if callable(del_fn):
                try:
                    del_fn(fnum)
                except TypeError:
                    del_fn(hou.frameToTime(fnum))
            else:
                return _result(False, errors=["parm.keyframe_delete_frame: no deleteKeyframeAtFrame on this build"])
            return _result(True, data={"node_path": path, "parm_name": name, "frame": fnum})
        except Exception as e:
            return _result(False, errors=[f"parm.keyframe_delete_frame failed: {e}"])

    if op == "exec.node_execute":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["exec.node_execute requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"exec.node_execute {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            ex = getattr(n, "execute", None)
            if not callable(ex):
                return _result(False, errors=[f"exec.node_execute: node has no .execute(): {path!r}"])
            ex()
            return _result(True, data={"node_path": path, "executed": True})
        except Exception as e:
            return _result(False, errors=[f"exec.node_execute failed: {e}"])

    if op == "solaris.usd_file_set":
        path = str(args.get("node_path") or "")
        file_path = str(args.get("file_path") or args.get("path") or "")
        parm_hint = str(args.get("parm_name") or args.get("parm_hint") or "").strip()
        if not path.strip() or not file_path.strip():
            return _result(False, errors=["solaris.usd_file_set requires node_path and file_path"])
        if dry_run:
            return _result(True, data={"preview": f"solaris.usd_file_set {path!r} -> {file_path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        candidates = (
            "filepath1",
            "filepath2",
            "filepath",
            "file",
            "filename",
            "usdfilepath",
            "sourcefile",
            "usdafile",
            "usdfile",
            "input_file",
            "layerfilepath",
            "layerfile",
            "path",
        )
        if parm_hint:
            candidates = (parm_hint,) + tuple(x for x in candidates if x != parm_hint)
        p, used = _first_matching_parm(n, candidates)
        if p is None:
            return _result(
                False,
                errors=[
                    f"solaris.usd_file_set: no USD/layer file parm found on {path!r}; "
                    "pass parm_name if you know the token"
                ],
            )
        try:
            p.set(str(file_path.strip()))
            return _result(True, data={"node_path": path, "file_path": file_path.strip(), "parm_name": used})
        except Exception as e:
            return _result(False, errors=[f"solaris.usd_file_set failed: {e}"])

    if op == "solaris.karma_render_set":
        path = str(args.get("node_path") or "")
        picture = args.get("picture", args.get("picture_path"))
        camera = args.get("camera", args.get("camera_path"))
        width = args.get("width", args.get("res_width"))
        height = args.get("height", args.get("res_height"))
        enable_override = args.get("override_resolution", args.get("enable_resolution_override"))
        if not path.strip():
            return _result(False, errors=["solaris.karma_render_set requires node_path"])
        has_any = any(x is not None for x in (picture, camera, width, height, enable_override))
        if not has_any:
            return _result(
                False,
                errors=["solaris.karma_render_set requires at least one of picture, camera, width, height, override_resolution"],
            )
        if dry_run:
            return _result(True, data={"preview": f"solaris.karma_render_set {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        resolved: dict[str, Any] = {"node_path": path}
        local_warn: list[str] = []
        try:
            if picture is not None:
                pcands = (
                    "picture",
                    "vm_picture",
                    "soho_diskfile",
                    "copoutputpath",
                    "outputpicture",
                    "lopoutput",
                    "diskfile",
                    "output",
                )
                p, used = _first_matching_parm(n, pcands)
                if p is None:
                    return _result(False, errors=[f"solaris.karma_render_set: no picture/output parm on {path!r}"])
                p.set(str(picture).strip())
                resolved["picture_parm"] = used
            if camera is not None:
                ccands = ("camera", "render_camera", "cam", "primary_camera", "camerapath", "rendercamera", "renderCam")
                p, used = _first_matching_parm(n, ccands)
                if p is None:
                    return _result(False, errors=[f"solaris.karma_render_set: no camera parm on {path!r}"])
                p.set(str(camera).strip())
                resolved["camera_parm"] = used
            if enable_override is not None:
                ov = None
                if str(enable_override).isdigit():
                    ov = bool(int(enable_override))
                else:
                    ov = bool(enable_override)
                po, un = _first_matching_parm(
                    n,
                    ("override_resolution", "override_camera_resolution", "override_res", "resolutionoverride"),
                )
                if po is not None:
                    po.set(int(ov))
                    resolved["override_resolution_parm"] = un
                else:
                    local_warn.append("solaris.karma_render_set: override_resolution requested but no toggle parm found")
            if width is not None:
                pw, wn = _first_matching_parm(
                    n,
                    ("res_overridewidth", "overridewidth", "width", "size1", "res1", "Resolution1"),
                )
                if pw is not None:
                    pw.set(float(width))
                    resolved["width_parm"] = wn
                else:
                    local_warn.append("solaris.karma_render_set: width requested but no width parm matched")
            if height is not None:
                ph, hn = _first_matching_parm(
                    n,
                    ("res_overrideheight", "overrideheight", "height", "size2", "res2", "Resolution2"),
                )
                if ph is not None:
                    ph.set(float(height))
                    resolved["height_parm"] = hn
                else:
                    local_warn.append("solaris.karma_render_set: height requested but no height parm matched")
            return _result(True, warnings=local_warn or None, data=resolved)
        except Exception as e:
            return _result(False, errors=[f"solaris.karma_render_set failed: {e}"])

    if op == "mtlx.texture_file_set":
        path = str(args.get("node_path") or "")
        file_path = str(args.get("file_path") or args.get("path") or args.get("texture_path") or "")
        parm_hint = str(args.get("parm_name") or "").strip()
        if not path.strip() or not file_path.strip():
            return _result(False, errors=["mtlx.texture_file_set requires node_path and file_path"])
        if dry_run:
            return _result(True, data={"preview": f"mtlx.texture_file_set {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        candidates = (
            "file",
            "filename",
            "filepath",
            "image",
            "map",
            "texture_map",
            "filename0",
            "filepath1",
            "base_color_texture",
        )
        if parm_hint:
            candidates = (parm_hint,) + tuple(x for x in candidates if x != parm_hint)
        p, used = _first_matching_parm(n, candidates)
        if p is None:
            return _result(False, errors=[f"mtlx.texture_file_set: no file/image parm on {path!r}"])
        try:
            p.set(str(file_path.strip()))
            return _result(True, data={"node_path": path, "file_path": file_path.strip(), "parm_name": used})
        except Exception as e:
            return _result(False, errors=[f"mtlx.texture_file_set failed: {e}"])

    if op == "mtlx.standard_surface_set":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["mtlx.standard_surface_set requires node_path"])
        roughness = args.get("roughness")
        metallic = args.get("metallic", args.get("metalness"))
        coat = args.get("coat", args.get("coat_weight"))
        base_color = args.get("base_color")
        specular = args.get("specular", args.get("specular_color"))
        has_any = any(x is not None for x in (roughness, metallic, coat, base_color, specular))
        if not has_any:
            return _result(False, errors=["mtlx.standard_surface_set requires at least one material field"])
        if dry_run:
            return _result(True, data={"preview": f"mtlx.standard_surface_set {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        out: dict[str, Any] = {"node_path": path}
        lw: list[str] = []

        def _set_float(cands: tuple[str, ...], val: Any, key: str) -> None:
            p, used = _first_matching_parm(n, cands)
            if p is None:
                lw.append(f"mtlx.standard_surface_set: no parm for {key}")
                return
            try:
                p.set(float(val))
                out[f"{key}_parm"] = used
            except Exception as e:
                lw.append(f"mtlx.standard_surface_set: {key} set failed: {e}")

        try:
            if roughness is not None:
                _set_float(
                    ("specular_roughness", "roughness", "specularroughness", "coat_roughness"),
                    roughness,
                    "roughness",
                )
            if metallic is not None:
                _set_float(("metalness", "metallic", "metal"), metallic, "metallic")
            if coat is not None:
                _set_float(("coat", "coat_weight", "coatWeight"), coat, "coat")
            if base_color is not None:
                if isinstance(base_color, (list, tuple)) and len(base_color) >= 3:
                    r, g, b = float(base_color[0]), float(base_color[1]), float(base_color[2])
                    pt = None
                    if hasattr(n, "parmTuple"):
                        for tnm in ("base_color", "color", "basecolor"):
                            try:
                                pt = n.parmTuple(tnm)
                            except Exception:
                                pt = None
                            if pt is not None:
                                break
                    if pt is not None and len(pt) >= 3:
                        pt.set((r, g, b))
                        try:
                            out["base_color_parm"] = pt.name()
                        except Exception:
                            out["base_color_parm"] = "base_color"
                    else:
                        pr, _ = _first_matching_parm(n, ("base_colorr", "colorr", "basecolorr"))
                        pg, _ = _first_matching_parm(n, ("base_colorg", "colorg", "basecolorg"))
                        pb, _ = _first_matching_parm(n, ("base_colorb", "colorb", "basecolorb"))
                        if pr and pg and pb:
                            pr.set(r)
                            pg.set(g)
                            pb.set(b)
                            out["base_color_mode"] = "rgb_split"
                        else:
                            lw.append("mtlx.standard_surface_set: could not set base_color (no tuple or rgb parms)")
                else:
                    _set_float(("base", "base_weight", "baseWeight"), base_color, "base_color_scalar")
            if specular is not None:
                if isinstance(specular, (list, tuple)) and len(specular) >= 3:
                    r, g, b = float(specular[0]), float(specular[1]), float(specular[2])
                    pt = None
                    if hasattr(n, "parmTuple"):
                        for tnm in ("specular_color", "specular", "specColor"):
                            try:
                                pt = n.parmTuple(tnm)
                            except Exception:
                                pt = None
                            if pt is not None:
                                break
                    if pt is not None and len(pt) >= 3:
                        pt.set((r, g, b))
                        try:
                            out["specular_parm"] = pt.name()
                        except Exception:
                            out["specular_parm"] = "specular_color"
                    else:
                        lw.append("mtlx.standard_surface_set: specular tuple not found")
                else:
                    _set_float(("specular", "specular_intensity"), specular, "specular_scalar")
            return _result(True, warnings=lw or None, data=out)
        except Exception as e:
            return _result(False, errors=[f"mtlx.standard_surface_set failed: {e}"])

    if op == "sop.vex_snippet_set":
        path = str(args.get("node_path") or "")
        code = args.get("code", args.get("snippet", args.get("vex", args.get("source"))))
        parm_hint = str(args.get("parm_name") or "").strip()
        if not path.strip() or code is None:
            return _result(False, errors=["sop.vex_snippet_set requires node_path and code (or snippet/vex)"])
        text = str(code)
        if dry_run:
            return _result(True, data={"preview": f"sop.vex_snippet_set {path!r} ({len(text)} chars)"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            lwvs: list[str] = []
            if parm_hint:
                p = n.parm(parm_hint)
                used = parm_hint
                if p is None:
                    return _result(False, errors=[f"sop.vex_snippet_set: parm {parm_hint!r} not found on {path}"])
            else:
                p, used = _wrangle_snippet_parm(n)
                if p is None:
                    return _result(
                        False,
                        errors=[f"sop.vex_snippet_set: no snippet parm on {path!r}; pass parm_name"],
                    )
            p.set(text)
            _sync_wrangle_snippet_aliases(n, text, used)
            bn = _wrangle_force_compile(n)
            if bn:
                lwvs.append(f"sop.vex_snippet_set: pressed compile button {bn!r}")
            try:
                n.cook(force=True)
            except Exception:
                pass
            return _result(
                True,
                warnings=lwvs or None,
                data={
                    "node_path": path,
                    "parm_name": used,
                    "length": len(text),
                    "compile_button": bn,
                },
            )
        except Exception as e:
            return _result(False, errors=[f"sop.vex_snippet_set failed: {e}"])

    if op == "sop.vex_snippet_get":
        path = str(args.get("node_path") or "")
        parm_hint = str(args.get("parm_name") or "").strip()
        if not path.strip():
            return _result(False, errors=["sop.vex_snippet_get requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"sop.vex_snippet_get {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if parm_hint:
                p = n.parm(parm_hint)
                used = parm_hint
                if p is None:
                    return _result(False, errors=[f"sop.vex_snippet_get: parm {parm_hint!r} not found"])
            else:
                p, used = _wrangle_snippet_parm(n)
                if p is None:
                    return _result(False, errors=[f"sop.vex_snippet_get: no snippet parm on {path!r}"])
            raw = None
            for attr in ("rawValue", "unexpandedString", "evalAsString"):
                fn = getattr(p, attr, None)
                if callable(fn):
                    try:
                        raw = fn()
                        break
                    except Exception:
                        continue
            if raw is None:
                raw = str(p.eval())
            return _result(True, data={"node_path": path, "parm_name": used, "code": str(raw)})
        except Exception as e:
            return _result(False, errors=[f"sop.vex_snippet_get failed: {e}"])

    if op == "sop.wrangle_run_over_set":
        path = str(args.get("node_path") or "")
        run_over = str(args.get("run_over") or args.get("run_class") or args.get("class") or args.get("domain") or "")
        if not path.strip() or not run_over.strip():
            return _result(False, errors=["sop.wrangle_run_over_set requires node_path and run_over"])
        if dry_run:
            return _result(True, data={"preview": f"sop.wrangle_run_over_set {path!r} -> {run_over!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            ok, pn = _wrangle_set_run_over_menu(n, run_over)
            if not ok:
                return _result(
                    False,
                    errors=[f"sop.wrangle_run_over_set: could not match run_over {run_over!r} to a menu on {path}"],
                )
            return _result(True, data={"node_path": path, "run_over": run_over.strip(), "parm_name": pn})
        except Exception as e:
            return _result(False, errors=[f"sop.wrangle_run_over_set failed: {e}"])

    if op == "sop.wrangle_group_set":
        path = str(args.get("node_path") or "")
        group = args.get("group", args.get("group_mask", args.get("pattern")))
        group_type_kw = str(args.get("group_type") or args.get("bind_type") or "").strip()
        if not path.strip():
            return _result(False, errors=["sop.wrangle_group_set requires node_path"])
        if group is None:
            return _result(False, errors=["sop.wrangle_group_set requires group (or group_mask); use empty string to clear"])
        if dry_run:
            return _result(True, data={"preview": f"sop.wrangle_group_set {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        text = str(group)
        try:
            lw: list[str] = []
            gt_used = None
            if text.strip():
                gtl = group_type_kw.lower()
                if not gtl:
                    if "@ptnum" in text or "ptnum" in text.lower():
                        gtl = "points"
                    else:
                        gtl = "guess"
                ok_gt, gtp = _wrangle_set_group_type_menu(n, gtl)
                if ok_gt:
                    gt_used = gtp
                elif gtl not in ("guess", ""):
                    lw.append(f"sop.wrangle_group_set: could not set group_type menu from {gtl!r}")
            p, used = _first_matching_parm(n, ("group", "groupmask", "bindgroup", "grp"))
            if p is None:
                return _result(False, errors=[f"sop.wrangle_group_set: no group parm on {path!r}"])
            p.set(text)
            try:
                n.cook(force=True)
            except Exception:
                pass
            data_out: dict[str, Any] = {"node_path": path, "group_parm": used, "group": text}
            if gt_used:
                data_out["group_type_parm"] = gt_used
                data_out["group_type"] = group_type_kw or ("points" if "@ptnum" in text else "guess")
            return _result(True, warnings=lw or None, data=data_out)
        except Exception as e:
            return _result(False, errors=[f"sop.wrangle_group_set failed: {e}"])

    if op == "sop.camphor_tree_build":
        """Build a low-poly stylized tree (樟树-shaped broad crown) inside a GEO object via L-system + polyreduce.

        Creates a subnet with spare parms ``gen_depth``, ``branch_angle``, ``trunk_scale``, ``poly_keep``.
        """
        import hou  # type: ignore

        parent_geo_path = str(args.get("parent_geo_path") or args.get("geo_path") or "").strip()
        subnet_name = str(args.get("subnet_name") or "camphor_tree_ctrl").strip() or "camphor_tree_ctrl"
        replace_existing = bool(args.get("replace_existing", True))
        auto_layout = bool(args.get("auto_layout", True))
        if not parent_geo_path:
            return _result(False, errors=["sop.camphor_tree_build requires parent_geo_path (Geometry OBJ, e.g. /obj/geo1)"])
        if dry_run:
            return _result(
                True,
                data={
                    "preview": (
                        f"sop.camphor_tree_build subnet={subnet_name!r} under {parent_geo_path!r} "
                        f"replace={replace_existing}"
                    )
                },
            )
        geo = hou.node(parent_geo_path)
        if geo is None:
            return _result(False, errors=[f"sop.camphor_tree_build: node not found: {parent_geo_path}"])
        try:
            if geo.childTypeCategory() != hou.sopNodeTypeCategory():
                return _result(
                    False,
                    errors=[
                        "sop.camphor_tree_build: parent_geo_path must be a Geometry object "
                        "(SOP network parent such as /obj/geo1)"
                    ],
                )
        except Exception:
            pass

        try:
            existing = geo.node(subnet_name)
            if existing is not None:
                if replace_existing:
                    existing.destroy()
                else:
                    return _result(
                        False,
                        errors=[
                            f"sop.camphor_tree_build: {parent_geo_path}/{subnet_name} already exists "
                            f"(pass replace_existing:true or choose another subnet_name)"
                        ],
                    )

            sn = geo.createNode("subnet", subnet_name)
            lsys = sn.createNode("lsystem", "lsystem1")
            redu: Any | None = None
            try:
                redu = sn.createNode("polyreduce")
            except hou.OperationFailed:
                try:
                    redu = sn.createNode("polyreduce::2.0")
                except hou.OperationFailed:
                    redu = None
            outn = sn.createNode("null", "OUT")

            lw_rule = _camphor_tree_configure_lsystem_rules(lsys)
            gen_w = _camphor_tree_parm_expr(lsys, ("generations", "gense", "gen"), 'ch("../gen_depth")')
            ang_w = _camphor_tree_parm_expr(lsys, ("angle", "angles"), 'ch("../branch_angle")')
            step_w = _camphor_tree_parm_expr(
                lsys,
                ("stepsize", "step", "length", "step_size"),
                '0.13 * ch("../trunk_scale")',
            )

            _camphor_tree_connect_subnet_chain(sn, lsys, redu, outn)
            ctr = _camphor_tree_subnet_add_controls(sn)
            try:
                sn.parm("gen_depth").set(3)
                sn.parm("branch_angle").set(28)
                sn.parm("trunk_scale").set(1.0)
                sn.parm("poly_keep").set(0.42)
            except Exception:
                pass

            redu_warn: list[str] = []
            if redu is not None:
                rk, rktok = _first_matching_parm(redu, ("keepratio", "keep", "percentkeep", "keeppercent"))
                if rk is not None:
                    try:
                        rk.setExpression('ch("../poly_keep")', hou.exprLanguage.Hscript)
                    except Exception:
                        try:
                            rk.setExpression('ch("../poly_keep")')
                        except Exception:
                            redu_warn.append("polyreduce: could not bind poly_keep")
                else:
                    rp, rptok = _first_matching_parm(redu, ("percentage", "reduce", "reduction"))
                    if rp is not None:
                        try:
                            rp.setExpression('100 * (1 - ch("../poly_keep"))', hou.exprLanguage.Hscript)
                        except Exception:
                            redu_warn.append("polyreduce: could not bind reduction expression")
                    else:
                        redu_warn.append("polyreduce: no matching keep/reduce parm; adjust manually")

            try:
                outn.setDisplayFlag(True)
                outn.setRenderFlag(True)
            except Exception:
                try:
                    outn.setDisplayFlag(True)
                except Exception:
                    pass

            if auto_layout:
                try:
                    geo.layoutChildren()
                    sn.layoutChildren()
                except Exception:
                    pass

            warn_merge = [*lw_rule, *redu_warn]
            if gen_w is None:
                warn_merge.append("lsystem: generations not wired; set gen_depth manually if needed")
            if ang_w is None:
                warn_merge.append("lsystem: angle not wired; set branch_angle manually if needed")
            if step_w is None:
                warn_merge.append("lsystem: step size not wired; adjust trunk_scale manually if needed")

            try:
                outn.cook(force=True)
            except Exception:
                pass

            data_out: dict[str, Any] = {
                "parent_geo_path": parent_geo_path,
                "subnet_path": sn.path(),
                "lsystem_path": lsys.path(),
                "output_null_path": outn.path(),
                "spare_parms_added": ctr,
                "bindings": {"generations_parm": gen_w, "angle_parm": ang_w, "step_parm": step_w},
            }
            if redu is not None:
                data_out["polyreduce_path"] = redu.path()
            return _result(True, warnings=warn_merge or None, data=data_out)
        except Exception as e:
            return _result(False, errors=[f"sop.camphor_tree_build failed: {e}"])

    if op == "sop.wrangle_create":
        parent_path = str(args.get("parent_path") or "")
        node_name = args.get("node_name")
        node_type = str(args.get("node_type") or "attribwrangle")
        code = args.get("code", args.get("snippet", args.get("vex")))
        run_over = str(args.get("run_over") or args.get("run_class") or "").strip()
        group = args.get("group", args.get("group_mask"))
        auto_layout = bool(args.get("auto_layout", True))
        if not parent_path.strip():
            return _result(False, errors=["sop.wrangle_create requires parent_path (e.g. /obj/geo1)"])
        if dry_run:
            return _result(True, data={"preview": f"sop.wrangle_create under {parent_path!r}"})
        parent = hou.node(parent_path.strip())
        if parent is None:
            return _result(False, errors=[f"Parent not found: {parent_path}"])
        type_candidates = [node_type] if node_type else []
        for extra in ("attribwrangle", "attribwrangle::2.0", "volumewrangle", "volumewrangle::2.0"):
            if extra not in type_candidates:
                type_candidates.append(extra)
        n = None
        used_type = None
        last_err = None
        for nt in type_candidates:
            try:
                n = parent.createNode(nt, node_name) if node_name else parent.createNode(nt)
                used_type = nt
                break
            except Exception as e:
                last_err = str(e)
                continue
        if n is None:
            return _result(False, errors=[f"sop.wrangle_create: could not create wrangle ({last_err})"])
        lw: list[str] = []
        try:
            if code is not None:
                inner = _dispatch_core(
                    "sop.vex_snippet_set",
                    {"node_path": n.path(), "code": code},
                    dry_run=False,
                )
                if not inner.get("ok"):
                    return _result(
                        False,
                        errors=inner.get("errors") or ["sop.vex_snippet_set failed"],
                        data={"node_path": n.path()},
                    )
                lw.extend(inner.get("warnings") or [])
            if run_over:
                inner = _dispatch_core(
                    "sop.wrangle_run_over_set",
                    {"node_path": n.path(), "run_over": run_over},
                    dry_run=False,
                )
                if not inner.get("ok"):
                    lw.extend(inner.get("errors") or [f"run_over {run_over!r} not applied"])
                else:
                    lw.extend(inner.get("warnings") or [])
            if group is not None:
                inner = _dispatch_core(
                    "sop.wrangle_group_set",
                    {"node_path": n.path(), "group": group},
                    dry_run=False,
                )
                if not inner.get("ok"):
                    lw.extend(inner.get("errors") or ["group not applied"])
                else:
                    lw.extend(inner.get("warnings") or [])
            if auto_layout:
                try:
                    parent.layoutChildren()
                except Exception as le:
                    lw.append(f"sop.wrangle_create layoutChildren: {le}")
            data = {"node_path": n.path(), "node_type": used_type}
            return _result(True, warnings=lw or None, data=data)
        except Exception as e:
            return _result(False, errors=[f"sop.wrangle_create failed: {e}"])

    if op == "geo.list_attribs":
        path = str(args.get("node_path") or "")
        scope = str(args.get("scope") or "all").lower()
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.list_attribs requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.list_attribs {path!r} scope={scope}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])

            def _names(seq_fn: Any) -> list[str]:
                try:
                    attribs = seq_fn()
                except Exception:
                    return []
                outn: list[str] = []
                for a in attribs:
                    try:
                        outn.append(a.name())
                    except Exception:
                        continue
                return sorted(set(outn))

            data: dict[str, Any] = {"node_path": path}
            if scope in ("all", "*", "any"):
                data["point"] = _names(g.pointAttribs)
                data["primitive"] = _names(g.primAttribs)
                gv = getattr(g, "vertexAttribs", None)
                data["vertex"] = _names(gv) if callable(gv) else []
                gg = getattr(g, "globalAttribs", None)
                if not callable(gg):
                    gg = getattr(g, "detailAttribs", None)
                data["detail"] = _names(gg) if callable(gg) else []
            elif scope in ("point", "pt", "points"):
                data["names"] = _names(g.pointAttribs)
            elif scope in ("prim", "primitive", "primitives"):
                data["names"] = _names(g.primAttribs)
            elif scope in ("vertex", "vertices", "vtx"):
                gv = getattr(g, "vertexAttribs", None)
                if not callable(gv):
                    return _result(False, errors=["geo.list_attribs: vertexAttribs not available on this geometry"])
                data["names"] = _names(gv)
            elif scope in ("detail", "global", "geo"):
                gg = getattr(g, "globalAttribs", None)
                if not callable(gg):
                    gg = getattr(g, "detailAttribs", None)
                if not callable(gg):
                    return _result(False, errors=["geo.list_attribs: no global/detail attrib API"])
                data["names"] = _names(gg)
            else:
                return _result(False, errors=[f"geo.list_attribs: unknown scope {scope!r}"])
            return _result(True, data=data)
        except Exception as e:
            return _result(False, errors=[f"geo.list_attribs failed: {e}"])

    if op == "mcp.ctrl_null_setup":
        """Create a Null under a GEO SOP network and add spare parms that channel-reference key parms elsewhere."""
        warnings: list[str] = []
        parent_path = str(args.get("parent_path") or "").strip()
        null_name = str(args.get("null_name") or args.get("node_name") or "mcp_ctrl").strip()
        input_from = str(args.get("input_from") or "").strip()
        set_disp = bool(args.get("set_display_flag", args.get("display", False)))
        auto_layout = bool(args.get("auto_layout", True))
        color = args.get("color")
        bindings = args.get("bindings") or []
        if isinstance(bindings, str) and bindings.strip():
            try:
                import json as _jbind

                bindings = _jbind.loads(bindings)
            except Exception:
                return _result(False, errors=["mcp.ctrl_null_setup: bindings must be a list or JSON array string"])
        if not isinstance(bindings, list):
            bindings = []
        if len(bindings) > 48:
            return _result(False, errors=["mcp.ctrl_null_setup: too many bindings (max 48)"])
        if not parent_path:
            return _result(False, errors=["mcp.ctrl_null_setup requires parent_path (GEO network, e.g. /obj/geo1)"])
        if dry_run:
            return _result(
                True,
                data={
                    "preview": (
                        f"mcp.ctrl_null_setup parent={parent_path!r} null={null_name!r} "
                        f"bindings={len(bindings)} input_from={input_from!r}"
                    )
                },
            )
        parent = hou.node(parent_path)
        if parent is None:
            return _result(False, errors=[f"mcp.ctrl_null_setup: parent not found: {parent_path!r}"])
        try:
            if parent.childTypeCategory() != hou.sopNodeTypeCategory():
                return _result(
                    False,
                    errors=["mcp.ctrl_null_setup: parent_path must be a SOP network (typically /obj/<GeoName>)"],
                )
        except Exception:
            return _result(False, errors=["mcp.ctrl_null_setup: could not verify parent is a SOP network"])
        if parent.node(null_name) is not None:
            return _result(
                False,
                errors=[f"mcp.ctrl_null_setup: node {parent_path}/{null_name} already exists; pick another null_name"],
            )
        try:
            null = parent.createNode("null", null_name)
        except hou.OperationFailed as e:
            return _result(False, errors=[f"mcp.ctrl_null_setup: createNode failed: {e}"])
        if input_from:
            src = hou.node(input_from)
            if src is None:
                return _result(False, errors=[f"mcp.ctrl_null_setup: input_from not found: {input_from!r}"])
            try:
                null.setInput(0, src, 0)
            except hou.OperationFailed as e:
                return _result(False, errors=[f"mcp.ctrl_null_setup: setInput failed: {e}"])
        if isinstance(color, (list, tuple)) and len(color) >= 3:
            try:
                null.setColor(hou.Color(float(color[0]), float(color[1]), float(color[2])))
            except Exception:
                pass
        created: list[dict[str, Any]] = []
        try:
            for bi, raw in enumerate(bindings):
                if not isinstance(raw, dict):
                    continue
                spare_name = str(raw.get("spare_name") or raw.get("name") or "").strip()
                ref_node = str(raw.get("ref_node") or raw.get("node_path") or "").strip()
                ref_parm = str(raw.get("ref_parm") or raw.get("parm_name") or "").strip()
                label = str(raw.get("label") or spare_name).strip()
                if not spare_name or not ref_node or not ref_parm:
                    return _result(
                        False,
                        errors=[f"mcp.ctrl_null_setup: bindings[{bi}] needs spare_name, ref_node, ref_parm"],
                    )
                ref_n = hou.node(ref_node)
                if ref_n is None:
                    return _result(False, errors=[f"mcp.ctrl_null_setup: ref_node not found: {ref_node!r}"])
                try:
                    rel = null.relativePath(ref_n)
                except Exception:
                    return _result(
                        False,
                        errors=[f"mcp.ctrl_null_setup: cannot relativePath from {null.path()!r} to {ref_node!r}"],
                    )
                rp = ref_n.parm(ref_parm)
                pt = ref_n.parmTuple(ref_parm) if rp is None else None
                ptg = null.parmTemplateGroup()
                if ptg.find(spare_name) is not None:
                    return _result(False, errors=[f"mcp.ctrl_null_setup: spare {spare_name!r} already exists on null"])
                try:
                    if rp is not None:
                        tmpl = hou.FloatParmTemplate(
                            spare_name,
                            label or spare_name,
                            1,
                            default_value=(float(rp.eval()),),
                        )
                        ptg.append(tmpl)
                        null.setParmTemplateGroup(ptg)
                        np = null.parm(spare_name)
                        if np is not None:
                            np.setExpression(f'ch("{rel}/{ref_parm}")', language=hou.exprLanguage.Hscript)
                        created.append({"spare_name": spare_name, "ref": f"{ref_node}/{ref_parm}", "kind": "float"})
                    elif pt is not None and len(pt) > 0:
                        dv = tuple(float(x) for x in pt.eval())
                        tmpl = hou.FloatParmTemplate(spare_name, label or spare_name, len(pt), default_value=dv)
                        ptg.append(tmpl)
                        null.setParmTemplateGroup(ptg)
                        new_pt = null.parmTuple(spare_name)
                        if new_pt is not None:
                            for j in range(len(pt)):
                                comp = pt[j]
                                cname = comp.name()
                                nj = new_pt[j]
                                if nj is not None:
                                    nj.setExpression(f'ch("{rel}/{cname}")', language=hou.exprLanguage.Hscript)
                        created.append({"spare_name": spare_name, "ref": f"{ref_node}/{ref_parm}", "kind": f"float{len(pt)}"})
                    else:
                        return _result(
                            False,
                            errors=[f"mcp.ctrl_null_setup: parm {ref_parm!r} not on {ref_node!r}"],
                        )
                except Exception as e:
                    return _result(False, errors=[f"mcp.ctrl_null_setup: binding {spare_name!r} failed: {e}"])
            if set_disp:
                try:
                    null.setDisplayFlag(True)
                except Exception:
                    pass
            if auto_layout:
                try:
                    parent.layoutChildren()
                except Exception as le:
                    warnings.append(f"mcp.ctrl_null_setup auto_layout: {le}")
            out: dict[str, Any] = {"node_path": null.path(), "bindings_applied": created}
            if warnings:
                return _result(True, warnings=warnings, data=out)
            return _result(True, data=out)
        except Exception as e:
            return _result(False, errors=[f"mcp.ctrl_null_setup failed: {e}"])

    if op == "node.spare_parm_add":
        path = str(args.get("node_path") or "")
        parm_name = str(args.get("parm_name") or args.get("name") or "").strip()
        parm_type = str(args.get("parm_type") or args.get("type") or "float").strip().lower()
        label = str(args.get("label") or parm_name).strip()
        default = args.get("default", args.get("default_value"))
        if not path.strip() or not parm_name:
            return _result(False, errors=["node.spare_parm_add requires node_path and parm_name"])
        if dry_run:
            return _result(True, data={"preview": f"spare_parm_add {path!r}.{parm_name} ({parm_type})"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            ptg = n.parmTemplateGroup()
            if ptg.find(parm_name) is not None:
                return _result(False, errors=[f"node.spare_parm_add: parm {parm_name!r} already exists on {path}"])
            tmpl: Any = None
            if parm_type in ("float", "f"):
                dv = (float(default),) if default is not None else (0.0,)
                tmpl = hou.FloatParmTemplate(parm_name, label, 1, default_value=dv)
            elif parm_type in ("float3", "vec3", "vector3"):
                if isinstance(default, (list, tuple)) and len(default) >= 3:
                    dv = (float(default[0]), float(default[1]), float(default[2]))
                else:
                    dv = (0.0, 0.0, 0.0)
                tmpl = hou.FloatParmTemplate(parm_name, label, 3, default_value=dv)
            elif parm_type in ("int", "i"):
                dv = (int(default),) if default is not None else (0,)
                tmpl = hou.IntParmTemplate(parm_name, label, 1, default_value=dv)
            elif parm_type in ("toggle", "bool", "t"):
                dv = bool(int(default)) if default is not None and str(default).isdigit() else (bool(default) if default is not None else False)
                tmpl = hou.ToggleParmTemplate(parm_name, label, default_value=dv)
            elif parm_type in ("string", "str", "s"):
                dv = (str(default),) if default is not None else ("",)
                tmpl = hou.StringParmTemplate(parm_name, label, 1, default_value=dv)
            elif parm_type in ("rgb", "color"):
                if isinstance(default, (list, tuple)) and len(default) >= 3:
                    dv = (float(default[0]), float(default[1]), float(default[2]))
                else:
                    dv = (1.0, 1.0, 1.0)
                tmpl = hou.FloatParmTemplate(parm_name, label, 3, default_value=dv)
            else:
                return _result(False, errors=[f"node.spare_parm_add: unknown parm_type {parm_type!r}"])
            ptg.append(tmpl)
            n.setParmTemplateGroup(ptg)
            return _result(True, data={"node_path": path, "parm_name": parm_name, "parm_type": parm_type})
        except Exception as e:
            return _result(False, errors=[f"node.spare_parm_add failed: {e}"])

    if op == "node.spare_parm_remove":
        path = str(args.get("node_path") or "")
        parm_name = str(args.get("parm_name") or args.get("name") or "").strip()
        if not path.strip() or not parm_name:
            return _result(False, errors=["node.spare_parm_remove requires node_path and parm_name"])
        if dry_run:
            return _result(True, data={"preview": f"spare_parm_remove {path!r}.{parm_name}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            rm = getattr(n, "removeSpareParmTemplate", None)
            if callable(rm):
                try:
                    rm(parm_name)
                    return _result(True, data={"node_path": path, "removed": parm_name, "method": "removeSpareParmTemplate"})
                except Exception:
                    pass
            ptg = n.parmTemplateGroup()
            tmpl = ptg.find(parm_name)
            if tmpl is None:
                return _result(False, errors=[f"node.spare_parm_remove: no parm {parm_name!r} on {path}"])
            ptg.remove(tmpl)
            n.setParmTemplateGroup(ptg)
            return _result(True, data={"node_path": path, "removed": parm_name, "method": "parmTemplateGroup"})
        except Exception as e:
            return _result(False, errors=[f"node.spare_parm_remove failed: {e}"])

    if op == "node.diagnostics":
        path = str(args.get("node_path") or "")
        force_cook = bool(args.get("force_cook", True))
        include_children = bool(args.get("include_children", False))
        if not path.strip():
            return _result(False, errors=["node.diagnostics requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"node.diagnostics {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                try:
                    n.cook(force=True)
                except Exception as ce:
                    pass
            errs: list[str] = []
            wrns: list[str] = []
            try:
                er = n.errors()
                if er:
                    errs.extend(str(x) for x in er)
            except Exception:
                pass
            try:
                wfn = getattr(n, "warnings", None)
                if callable(wfn):
                    wv = wfn()
                    if wv:
                        wrns.extend(str(x) for x in wv)
            except Exception:
                pass
            if include_children:
                try:
                    for c in n.children():
                        try:
                            for x in c.errors() or []:
                                errs.append(f"{c.path()}: {x}")
                        except Exception:
                            continue
                except Exception:
                    pass
            info_err = None
            try:
                em = getattr(n, "errorsAsString", None)
                if callable(em):
                    info_err = em()
            except Exception:
                pass
            data: dict[str, Any] = {
                "node_path": path,
                "errors": errs[:128],
                "warnings": wrns[:128],
                "has_errors": bool(errs),
            }
            if info_err:
                data["errors_as_string"] = str(info_err)[:8000]
            return _result(True, data=data)
        except Exception as e:
            return _result(False, errors=[f"node.diagnostics failed: {e}"])

    if op == "geo.sample_points":
        path = str(args.get("node_path") or "")
        raw_attrs = args.get("attributes", args.get("attribs", args.get("names")))
        max_points = int(args.get("max_points", 32))
        max_points = max(1, min(max_points, 4096))
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.sample_points requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.sample_points {path!r} n<={max_points}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            attrs: list[str]
            if raw_attrs is None:
                attrs = ["P"]
            elif isinstance(raw_attrs, str):
                attrs = [x.strip() for x in raw_attrs.replace(",", " ").split() if x.strip()]
            elif isinstance(raw_attrs, (list, tuple)):
                attrs = [str(x).strip() for x in raw_attrs if str(x).strip()]
            else:
                return _result(False, errors=["geo.sample_points: attributes must be a list or string"])
            if not attrs:
                attrs = ["P"]

            pts = g.points()
            n_take = min(max_points, len(pts))
            rows: list[dict[str, Any]] = []
            for i in range(n_take):
                pt = pts[i]
                row: dict[str, Any] = {"ptnum": i}
                for an in attrs:
                    try:
                        row[an] = _serialize_geo_component(pt.attribValue(an))
                    except Exception:
                        row[an] = None
                rows.append(row)
            tp = len(pts)
            return _result(
                True,
                data={
                    "node_path": path,
                    "num_points_sampled": n_take,
                    "total_points": tp,
                    "max_points_cap": max_points,
                    "attributes": attrs,
                    "samples": rows,
                    "truncated": n_take < tp,
                    "truncate_reason": "max_points" if n_take < tp else None,
                },
            )
        except Exception as e:
            return _result(False, errors=[f"geo.sample_points failed: {e}"])

    if op == "sop.wrangle_recompile":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["sop.wrangle_recompile requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"sop.wrangle_recompile {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            btn, tried = _wrangle_press_best_compile_button(n)
            avail = [t for t, _ in _wrangle_compile_button_candidates(n)]
            try:
                ntype = n.type().name()
            except Exception:
                ntype = None
            all_btns = _all_button_parm_tokens(n)
            if btn:
                return _result(True, data={"node_path": path, "node_type": ntype, "button_parm": btn})
            try:
                n.cook(force=True)
                return _result(
                    True,
                    data={
                        "node_path": path,
                        "node_type": ntype,
                        "button_parm": None,
                        "compile_fallback": "cook(force=True)",
                        "attempted_button_parms": tried,
                        "available_compile_like_button_parms": avail,
                        "all_button_parm_tokens": all_btns,
                    },
                )
            except Exception as cook_e:
                return _result(
                    False,
                    data={
                        "node_path": path,
                        "node_type": ntype,
                        "attempted_button_parms": tried,
                        "available_compile_like_button_parms": avail,
                        "all_button_parm_tokens": all_btns,
                    },
                    errors=[
                        "sop.wrangle_recompile: no compile/reload button responded and cook(force=True) failed: "
                        + str(cook_e)
                    ],
                )
        except Exception as e:
            return _result(False, errors=[f"sop.wrangle_recompile failed: {e}"])

    if op == "geo.groups_list":
        path = str(args.get("node_path") or "")
        force_cook = bool(args.get("force_cook", True))
        include_edges = bool(args.get("include_edge_groups", False))
        if not path.strip():
            return _result(False, errors=["geo.groups_list requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.groups_list {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            ptg = [gr.name() for gr in g.pointGroups()]
            prg = [gr.name() for gr in g.primGroups()]
            edg: list[str] = []
            if include_edges:
                eg = getattr(g, "edgeGroups", None)
                if callable(eg):
                    try:
                        edg = [gr.name() for gr in eg()]
                    except Exception:
                        edg = []
            return _result(
                True,
                data={"node_path": path, "point_groups": sorted(ptg), "primitive_groups": sorted(prg), "edge_groups": sorted(edg)},
            )
        except Exception as e:
            return _result(False, errors=[f"geo.groups_list failed: {e}"])

    if op == "geo.group_count":
        path = str(args.get("node_path") or "")
        group_name = str(args.get("group_name") or args.get("name") or "").strip()
        scope = str(args.get("scope") or "point").lower()
        force_cook = bool(args.get("force_cook", True))
        if not path.strip() or not group_name:
            return _result(False, errors=["geo.group_count requires node_path and group_name"])
        if dry_run:
            return _result(True, data={"preview": f"geo.group_count {path!r} @{group_name!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            cnt = None
            if scope in ("point", "pt", "points"):
                pat = f"@{group_name}"
                try:
                    cnt = len(g.globPoints(pat))
                except Exception:
                    cnt = len(g.globPoints(group_name))
            elif scope in ("prim", "primitive", "primitives"):
                pat = f"@{group_name}"
                try:
                    cnt = len(g.globPrims(pat))
                except Exception:
                    cnt = len(g.globPrims(group_name))
            elif scope in ("edge", "edges"):
                eg = getattr(g, "globEdges", None)
                if not callable(eg):
                    return _result(False, errors=["geo.group_count: edge scope not supported on this geometry"])
                try:
                    cnt = len(eg(f"@{group_name}"))
                except Exception:
                    cnt = len(eg(group_name))
            else:
                return _result(False, errors=[f"geo.group_count: unknown scope {scope!r}"])
            return _result(True, data={"node_path": path, "group_name": group_name, "scope": scope, "count": int(cnt)})
        except Exception as e:
            return _result(False, errors=[f"geo.group_count failed: {e}"])

    if op == "geo.sample_primitives":
        path = str(args.get("node_path") or "")
        raw_attrs = args.get("attributes", args.get("attribs", args.get("names")))
        max_prims = int(args.get("max_primitives", args.get("max_points", 32)))
        max_prims = max(1, min(max_prims, 4096))
        force_cook = bool(args.get("force_cook", True))
        include_intrinsic_type = bool(args.get("include_prim_type", True))
        if not path.strip():
            return _result(False, errors=["geo.sample_primitives requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.sample_primitives {path!r} n<={max_prims}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            attrs: list[str]
            if raw_attrs is None:
                attrs = []
            elif isinstance(raw_attrs, str):
                attrs = [x.strip() for x in raw_attrs.replace(",", " ").split() if x.strip()]
            elif isinstance(raw_attrs, (list, tuple)):
                attrs = [str(x).strip() for x in raw_attrs if str(x).strip()]
            else:
                return _result(False, errors=["geo.sample_primitives: attributes must be a list or string"])
            prims = g.prims()
            n_take = min(max_prims, len(prims))
            rows: list[dict[str, Any]] = []
            for i in range(n_take):
                pr = prims[i]
                row: dict[str, Any] = {"primnum": i}
                if include_intrinsic_type:
                    try:
                        row["prim_type"] = str(pr.type())
                    except Exception:
                        row["prim_type"] = None
                for an in attrs:
                    try:
                        row[an] = _serialize_geo_component(pr.attribValue(an))
                    except Exception:
                        row[an] = None
                rows.append(row)
            tpr = len(prims)
            return _result(
                True,
                data={
                    "node_path": path,
                    "num_primitives_sampled": n_take,
                    "total_primitives": tpr,
                    "max_primitives_cap": max_prims,
                    "attributes": attrs,
                    "samples": rows,
                    "truncated": n_take < tpr,
                    "truncate_reason": "max_primitives" if n_take < tpr else None,
                },
            )
        except Exception as e:
            return _result(False, errors=[f"geo.sample_primitives failed: {e}"])

    if op == "geo.primitive_type_breakdown":
        path = str(args.get("node_path") or "")
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.primitive_type_breakdown requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.primitive_type_breakdown {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            counts: dict[str, int] = {}
            for pr in g.prims():
                try:
                    key = str(pr.type())
                except Exception:
                    key = "unknown"
                counts[key] = counts.get(key, 0) + 1
            return _result(True, data={"node_path": path, "counts": counts, "total_primitives": sum(counts.values())})
        except Exception as e:
            return _result(False, errors=[f"geo.primitive_type_breakdown failed: {e}"])

    if op == "geo.has_packed_primitives":
        path = str(args.get("node_path") or "")
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.has_packed_primitives requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.has_packed_primitives {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            packed = False
            pt_packed = getattr(hou.primType, "PackedGeometry", None)
            pt_frag = getattr(hou.primType, "PackedFragment", None)
            for pr in g.prims():
                try:
                    t = pr.type()
                    if pt_packed is not None and t == pt_packed:
                        packed = True
                        break
                    if pt_frag is not None and t == pt_frag:
                        packed = True
                        break
                    ts = str(t).lower()
                    if "packed" in ts:
                        packed = True
                        break
                    if hasattr(pr, "isPackedGeometry") and pr.isPackedGeometry():
                        packed = True
                        break
                except Exception:
                    continue
            return _result(True, data={"node_path": path, "has_packed_primitives": packed})
        except Exception as e:
            return _result(False, errors=[f"geo.has_packed_primitives failed: {e}"])

    if op == "geo.detail_attrib_get":
        path = str(args.get("node_path") or "")
        name = str(args.get("name") or args.get("attrib_name") or "").strip()
        force_cook = bool(args.get("force_cook", True))
        if not path.strip() or not name:
            return _result(False, errors=["geo.detail_attrib_get requires node_path and name"])
        if dry_run:
            return _result(True, data={"preview": f"geo.detail_attrib_get {path!r} {name!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            ga = g.findGlobalAttrib(name)
            if ga is None:
                return _result(False, errors=[f"geo.detail_attrib_get: no global/detail attrib {name!r}"])
            try:
                val = g.attribValue(name)
            except Exception:
                val = None
            return _result(
                True,
                data={"node_path": path, "name": name, "value": _serialize_geo_component(val)},
            )
        except Exception as e:
            return _result(False, errors=[f"geo.detail_attrib_get failed: {e}"])

    if op == "network.clipboard_copy":
        raw = args.get("node_paths") or args.get("paths") or []
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            return _result(False, errors=["network.clipboard_copy requires non-empty node_paths"])
        if dry_run:
            return _result(True, data={"preview": f"network.clipboard_copy n={len(raw)}"})
        nodes: list[Any] = []
        for p in raw:
            ps = str(p).strip()
            if not ps:
                continue
            nn = hou.node(ps)
            if nn is None:
                return _result(False, errors=[f"network.clipboard_copy: node not found: {ps}"])
            nodes.append(nn)
        if not nodes:
            return _result(False, errors=["network.clipboard_copy: no valid nodes"])
        try:
            fn = getattr(hou, "copyNodesToClipboard", None)
            if not callable(fn):
                return _result(False, errors=["network.clipboard_copy: hou.copyNodesToClipboard not available"])
            fn(tuple(nodes))
            return _result(True, data={"copied": [x.path() for x in nodes], "count": len(nodes)})
        except Exception as e:
            return _result(False, errors=[f"network.clipboard_copy failed: {e}"])

    if op == "network.clipboard_paste":
        parent_path = str(args.get("parent_path") or args.get("path") or "")
        if not parent_path.strip():
            return _result(False, errors=["network.clipboard_paste requires parent_path"])
        if dry_run:
            return _result(True, data={"preview": f"network.clipboard_paste -> {parent_path!r}"})
        parent = hou.node(parent_path.strip())
        if parent is None:
            return _result(False, errors=[f"Parent not found: {parent_path}"])
        try:
            fn = getattr(hou, "pasteNodesFromClipboard", None)
            if not callable(fn):
                return _result(False, errors=["network.clipboard_paste: hou.pasteNodesFromClipboard not available"])
            created = fn(parent)
            paths: list[str] = []
            if created:
                try:
                    for cn in created:
                        paths.append(cn.path())
                except Exception:
                    pass
            return _result(True, data={"parent_path": parent_path, "created_paths": paths})
        except Exception as e:
            return _result(False, errors=[f"network.clipboard_paste failed: {e}"])

    if op == "geo.prim_intrinsics_bulk":
        path = str(args.get("node_path") or "")
        raw_idx = args.get("prim_indices", args.get("indices"))
        max_prims = int(args.get("max_primitives", 24))
        max_prims = max(1, min(max_prims, 512))
        max_intrinsics = int(args.get("max_intrinsics_per_prim", 256))
        max_intrinsics = max(0, min(max_intrinsics, 4096))
        prim_type_filter = str(args.get("prim_type_contains") or args.get("type_filter") or "").strip().lower()
        keys_only = bool(args.get("keys_only", False))
        volume_family_only = bool(args.get("volume_family_only", False))
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.prim_intrinsics_bulk requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.prim_intrinsics_bulk {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            prims = g.prims()
            sel_idx: list[int] = []
            if raw_idx is not None:
                if isinstance(raw_idx, (list, tuple)):
                    for x in raw_idx:
                        try:
                            ii = int(x)
                            if 0 <= ii < len(prims):
                                sel_idx.append(ii)
                        except Exception:
                            continue
                else:
                    try:
                        ii = int(raw_idx)
                        if 0 <= ii < len(prims):
                            sel_idx = [ii]
                    except Exception:
                        pass
                if not sel_idx:
                    return _result(False, errors=["geo.prim_intrinsics_bulk: no valid prim_indices"])
            else:
                vf = ("vdb", "volume", "fog", "openvdb", "houdini")
                for i, pr in enumerate(prims):
                    tlow = str(pr.type()).lower()
                    if volume_family_only and not any(k in tlow for k in vf):
                        continue
                    if prim_type_filter and prim_type_filter not in tlow:
                        continue
                    sel_idx.append(i)
                    if len(sel_idx) >= max_prims:
                        break
                if not sel_idx:
                    return _result(
                        True,
                        warnings=["geo.prim_intrinsics_bulk: no primitives matched filter"],
                        data={"node_path": path, "primitives": [], "total_primitives": len(prims)},
                    )
            rows: list[dict[str, Any]] = []
            for i in sel_idx:
                pr = prims[i]
                try:
                    tname = str(pr.type())
                except Exception:
                    tname = "unknown"
                payload, intr_trunc = _prim_collect_intrinsics(pr, max_intrinsics, keys_only=keys_only)
                row: dict[str, Any] = {
                    "primnum": i,
                    "prim_type": tname,
                    "intrinsics_truncated": intr_trunc,
                }
                if keys_only:
                    row["intrinsic_names"] = payload
                else:
                    row["intrinsics"] = payload
                rows.append(row)
            capped = False
            if raw_idx is None and len(sel_idx) >= max_prims and len(prims) > max_prims:
                capped = True
            return _result(
                True,
                data={
                    "node_path": path,
                    "total_primitives": len(prims),
                    "returned": len(rows),
                    "max_primitives_cap": max_prims,
                    "keys_only": keys_only,
                    "volume_family_only": volume_family_only,
                    "prim_type_contains": prim_type_filter or None,
                    "primitives": rows,
                    "truncated": capped,
                    "truncate_reason": "max_primitives" if capped else None,
                },
            )
        except Exception as e:
            return _result(False, errors=[f"geo.prim_intrinsics_bulk failed: {e}"])

    if op == "geo.volume_primitives_scan":
        path = str(args.get("node_path") or "")
        max_list = int(args.get("max_list", 128))
        max_list = max(1, min(max_list, 2048))
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.volume_primitives_scan requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.volume_primitives_scan {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            hits: list[dict[str, Any]] = []
            vf = ("vdb", "volume", "fog", "openvdb")
            total_pr = len(g.prims())
            stopped_early = False
            for i, pr in enumerate(g.prims()):
                if len(hits) >= max_list:
                    stopped_early = True
                    break
                try:
                    tlow = str(pr.type()).lower()
                except Exception:
                    tlow = ""
                if not any(k in tlow for k in vf):
                    continue
                keys, _trunc = _prim_collect_intrinsics(pr, 48, keys_only=True)
                hits.append({"primnum": i, "prim_type": str(pr.type()), "intrinsic_name_sample": keys[:48]})
            return _result(
                True,
                data={
                    "node_path": path,
                    "volume_like_primitives": hits,
                    "count": len(hits),
                    "total_primitives_scanned_disk": total_pr,
                    "truncated": stopped_early,
                    "truncate_reason": "max_list" if stopped_early else None,
                    "note": "Lightweight scan; use geo.prim_intrinsics_bulk for full intrinsic maps.",
                },
            )
        except Exception as e:
            return _result(False, errors=[f"geo.volume_primitives_scan failed: {e}"])

    if op == "geo.prim_bbox":
        path = str(args.get("node_path") or "")
        prim_index = int(args.get("prim_index", args.get("primitive", 0)))
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["geo.prim_bbox requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"geo.prim_bbox {path!r} prim={prim_index}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            if force_cook:
                n.cook(force=True)
            g = n.geometry()
            if g is None:
                return _result(False, errors=[f"No geometry at {path}"])
            prims = g.prims()
            if prim_index < 0 or prim_index >= len(prims):
                return _result(False, errors=[f"geo.prim_bbox: prim_index out of range: {prim_index}"])
            pr = prims[prim_index]
            bb_fn = getattr(pr, "boundingBox", None)
            if not callable(bb_fn):
                return _result(False, errors=["geo.prim_bbox: primitive has no boundingBox() in this build"])
            bb = bb_fn()
            return _result(
                True,
                data={
                    "node_path": path,
                    "prim_index": prim_index,
                    "prim_type": str(pr.type()),
                    "bounding_box": _bbox_to_dict(bb),
                },
            )
        except Exception as e:
            return _result(False, errors=[f"geo.prim_bbox failed: {e}"])

    if op == "vellum.graph_summary":
        # Find vellum-related nodes under a network (recursive name/type scan).
        parent_path = str(args.get("parent_path") or args.get("path") or "")
        max_nodes = int(args.get("max_nodes", 200))
        max_nodes = max(1, min(max_nodes, 2000))
        tokens = args.get("type_contains")
        if tokens is None:
            needle = ("vellum", "cloth", "hair", "grain", "fluid", "constraint", "pack")
        elif isinstance(tokens, str):
            needle = tuple(x.strip().lower() for x in tokens.replace(",", " ").split() if x.strip())
        elif isinstance(tokens, (list, tuple)):
            needle = tuple(str(x).strip().lower() for x in tokens if str(x).strip())
        else:
            needle = ("vellum",)
        if not parent_path.strip():
            return _result(False, errors=["vellum.graph_summary requires parent_path"])
        if dry_run:
            return _result(True, data={"preview": f"vellum.graph_summary {parent_path!r}"})
        root = hou.node(parent_path.strip())
        if root is None:
            return _result(False, errors=[f"Node not found: {parent_path}"])
        try:
            matches: list[dict[str, Any]] = []

            def visit(nd: Any, depth: int) -> None:
                if len(matches) >= max_nodes:
                    return
                try:
                    ch = nd.children()
                except Exception:
                    ch = []
                for c in ch:
                    if len(matches) >= max_nodes:
                        return
                    try:
                        tn = c.type().name().lower()
                    except Exception:
                        tn = ""
                    if any(t in tn for t in needle):
                        matches.append({"node_path": c.path(), "type_name": c.type().name()})
                    visit(c, depth + 1)

            visit(root, 0)
            return _result(
                True,
                data={"parent_path": parent_path, "matches": matches, "count": len(matches)},
            )
        except Exception as e:
            return _result(False, errors=[f"vellum.graph_summary failed: {e}"])

    if op == "obj.display_sop_path":
        path = str(args.get("obj_path") or args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["obj.display_sop_path requires obj_path (or node_path)"])
        if dry_run:
            return _result(True, data={"preview": f"obj.display_sop_path {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            dn, how = _resolve_obj_display_sop(n)
            dp = dn.path() if dn is not None else None
            return _result(True, data={"obj_path": path, "display_sop_path": dp, "method": how})
        except Exception as e:
            return _result(False, errors=[f"obj.display_sop_path failed: {e}"])

    if op == "obj.render_sop_path":
        path = str(args.get("obj_path") or args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["obj.render_sop_path requires obj_path (or node_path)"])
        if dry_run:
            return _result(True, data={"preview": f"obj.render_sop_path {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            rn, how = _resolve_obj_render_sop(n)
            rp = rn.path() if rn is not None else None
            return _result(True, data={"obj_path": path, "render_sop_path": rp, "method": how})
        except Exception as e:
            return _result(False, errors=[f"obj.render_sop_path failed: {e}"])

    if op == "obj.world_bounds":
        path = str(args.get("obj_path") or args.get("node_path") or "")
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["obj.world_bounds requires obj_path (or node_path)"])
        if dry_run:
            return _result(True, data={"preview": f"obj.world_bounds {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            bd, src, lw = _obj_bbox_payload(n, force_cook_display=force_cook)
            if bd is None:
                return _result(False, warnings=lw or None, errors=["obj.world_bounds: could not compute bounds"])
            return _result(True, warnings=lw or None, data={"obj_path": path, "bounding_box": bd, "source": src})
        except Exception as e:
            return _result(False, errors=[f"obj.world_bounds failed: {e}"])

    if op == "obj.geo_summary":
        path = str(args.get("obj_path") or args.get("node_path") or "")
        force_cook = bool(args.get("force_cook", True))
        if not path.strip():
            return _result(False, errors=["obj.geo_summary requires obj_path (or node_path)"])
        if dry_run:
            return _result(True, data={"preview": f"obj.geo_summary {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            dn, dhow = _resolve_obj_display_sop(n)
            rn, rhow = _resolve_obj_render_sop(n)
            bd, bsrc, bw = _obj_bbox_payload(n, force_cook_display=force_cook)
            warn_merge: list[str] = list(bw or [])
            data: dict[str, Any] = {
                "obj_path": path,
                "display_sop_path": dn.path() if dn is not None else None,
                "display_method": dhow,
                "render_sop_path": rn.path() if rn is not None else None,
                "render_method": rhow,
                "bounding_box": bd,
                "bounds_source": bsrc,
            }
            if dn is not None:
                try:
                    if force_cook:
                        dn.cook(force=True)
                    g = dn.geometry()
                    if g is not None:

                        def _safe_geo_count(geo: Any, names: tuple[str, ...], fallback_seq: str | None = None) -> int | None:
                            for nm in names:
                                fn = getattr(geo, nm, None)
                                if callable(fn):
                                    try:
                                        return int(fn())
                                    except Exception:
                                        continue
                            if fallback_seq:
                                seq_fn = getattr(geo, fallback_seq, None)
                                if callable(seq_fn):
                                    try:
                                        return int(len(seq_fn()))
                                    except Exception:
                                        return None
                            return None

                        np_ = _safe_geo_count(g, ("numPoints", "pointCount"), "points")
                        npr = _safe_geo_count(g, ("numPrims", "numPrimitives", "primCount"), "prims")
                        nv = _safe_geo_count(g, ("numVertices", "vertexCount"), "vertices")
                        if np_ is not None:
                            data["num_points"] = np_
                        if npr is not None:
                            data["num_primitives"] = npr
                        if nv is not None:
                            data["num_vertices"] = nv
                except Exception as e:
                    warn_merge.append(f"geo counts: {e}")
            else:
                warn_merge.append("obj.geo_summary: no display SOP; counts omitted")
            return _result(True, warnings=warn_merge or None, data=data)
        except Exception as e:
            return _result(False, errors=[f"obj.geo_summary failed: {e}"])

    if op == "obj.file_node_set_path":
        path = str(args.get("node_path") or "")
        file_path = str(args.get("file_path") or args.get("path") or "")
        parm_hint = str(args.get("parm_name") or "").strip()
        if not path.strip() or not file_path.strip():
            return _result(False, errors=["obj.file_node_set_path requires node_path and file_path"])
        if dry_run:
            return _result(True, data={"preview": f"obj.file_node_set_path {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        candidates = (
            "file",
            "filename",
            "filepath1",
            "filepath2",
            "filepath",
            "fileName",
            "filename1",
            "cache_file",
            "usdfile",
            "alembic_path",
            "abcfile",
        )
        if parm_hint:
            candidates = (parm_hint,) + tuple(x for x in candidates if x != parm_hint)
        p, used = _first_matching_parm(n, candidates)
        if p is None:
            return _result(
                False,
                errors=[f"obj.file_node_set_path: no file-path parm on {path!r}; pass parm_name"],
            )
        try:
            p.set(str(file_path.strip()))
            return _result(True, data={"node_path": path, "file_path": file_path.strip(), "parm_name": used})
        except Exception as e:
            return _result(False, errors=[f"obj.file_node_set_path failed: {e}"])

    if op == "obj.camera_clip":
        path = str(args.get("node_path") or "")
        near = args.get("near")
        far = args.get("far")
        if not path.strip():
            return _result(False, errors=["obj.camera_clip requires node_path"])
        if near is None and far is None:
            return _result(False, errors=["obj.camera_clip requires at least one of near, far"])
        if dry_run:
            return _result(True, data={"preview": f"obj.camera_clip {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            resolved: dict[str, Any] = {"node_path": path}
            if near is not None:
                p, used = _first_matching_parm(n, ("near", "znear", "cam_near", "near_clip", "nearclip"))
                if p is None:
                    return _result(False, errors=["obj.camera_clip: no near/znear parm found on this camera"])
                p.set(float(near))
                resolved["near_parm"] = used
                resolved["near"] = float(near)
            if far is not None:
                p2, used2 = _first_matching_parm(n, ("far", "zfar", "cam_far", "far_clip", "farclip"))
                if p2 is None:
                    return _result(False, errors=["obj.camera_clip: no far/zfar parm found on this camera"])
                p2.set(float(far))
                resolved["far_parm"] = used2
                resolved["far"] = float(far)
            return _result(True, data=resolved)
        except Exception as e:
            return _result(False, errors=[f"obj.camera_clip failed: {e}"])

    if op == "exec.python":
        """Run arbitrary Python in the live Houdini session (``hou`` in global namespace).

        Matches “reference” Houdini MCP workflows that iterate with multi-line scripts instead of only atomic ops.
        """
        code = str(args.get("code") or args.get("source") or "").strip()
        if not code:
            return _result(False, errors=["exec.python requires non-empty code (or source)"])
        if len(code) > 400_000:
            return _result(False, errors=["exec.python: code exceeds 400000 characters"])
        use_undo = bool(args.get("use_undo_group", True))
        undo_label = str(args.get("undo_label") or "mcp_exec_python")
        if dry_run:
            return _result(
                True,
                data={"preview": f"exec.python ({len(code)} chars) use_undo_group={use_undo}"},
            )
        try:
            g: dict[str, Any] = {"hou": hou, "__name__": "__mcp_exec_python__"}
            if use_undo:
                with hou.undos.group(undo_label):
                    exec(compile(code, "<mcp exec.python>", "exec"), g, g)
            else:
                exec(compile(code, "<mcp exec.python>", "exec"), g, g)
            return _result(True, data={"executed": True, "char_count": len(code)})
        except Exception as e:
            tb = traceback.format_exc()
            return _result(False, data={"traceback": tb}, errors=[f"exec.python failed: {e}"])

    if op == "scene.summary":
        """Structured read-only overview: hip/frame plus ``/obj`` children (optional SOP name samples).

        Optional ``rich_context`` (default true) adds: global playbar expressions ($RFSTART/$RFEND), selected-node
        parm samples + light diagnostics, and per-GEO display SOP topology hints (prim/point counts, packed sample).
        """
        max_nodes = int(args.get("max_obj_nodes", 200))
        include_sops = bool(args.get("include_sop_children", True))
        rich = bool(args.get("rich_context", True))
        max_sel = max(0, min(int(args.get("max_selected_detail_nodes", 8)), 32))
        max_pp = max(1, min(int(args.get("max_parms_per_node", 24)), 128))
        geo_max = max(0, min(int(args.get("geo_hint_max_geos", 6)), 32))
        diag_cook = bool(args.get("diagnostics_force_cook", True))
        if dry_run:
            return _result(True, data={"preview": f"scene.summary rich_context={rich}"})
        try:
            root = hou.node("/obj")
            if root is None:
                return _result(False, errors=["scene.summary: /obj not found"])
            rows: list[dict[str, Any]] = []
            kids = root.children()
            for i, ch in enumerate(kids):
                if i >= max_nodes:
                    break
                row: dict[str, Any] = {"path": ch.path(), "name": ch.name(), "type": ch.type().name()}
                try:
                    if include_sops and ch.childTypeCategory() == hou.sopNodeTypeCategory():
                        sc = ch.children()
                        row["sop_child_count"] = len(sc)
                        row["sop_children"] = [p.name() for p in sc[:48]]
                except Exception:
                    pass
                rows.append(row)
            pay = _session_snapshot_payload(hou, include_desktop=False)
            pay["obj_nodes"] = rows
            pay["obj_node_total"] = len(kids)
            pay["obj_nodes_truncated"] = len(kids) > max_nodes
            if rich:
                pay["playback_globals"] = _playback_globals_payload(hou)
                sel_paths: list[str] = []
                try:
                    sel_paths = [str(p) for p in (pay.get("selected_node_paths") or []) if str(p).strip()][:max_sel]
                except Exception:
                    sel_paths = []
                details: list[dict[str, Any]] = []
                for sp in sel_paths:
                    sn = hou.node(sp)
                    if sn is None:
                        details.append({"path": sp, "missing": True})
                        continue
                    entry: dict[str, Any] = {
                        "path": sp,
                        "type": sn.type().name(),
                        "parm_samples": _parm_samples_for_node(sn, max_pp),
                        "diagnostics": _node_diagnostics_compact(hou, sn, force_cook=diag_cook),
                    }
                    try:
                        ins = sn.inputs()
                        entry["non_null_input_count"] = len([x for x in ins if x is not None])
                    except Exception:
                        pass
                    details.append(entry)
                pay["selected_node_details"] = details
                geo_hints: list[dict[str, Any]] = []
                if geo_max > 0:
                    collected = 0
                    for row in rows:
                        if collected >= geo_max:
                            break
                        pth = str(row.get("path") or "").strip()
                        if not pth:
                            continue
                        gn = hou.node(pth)
                        if gn is None:
                            continue
                        try:
                            if gn.childTypeCategory() != hou.sopNodeTypeCategory():
                                continue
                        except Exception:
                            continue
                        dn, how = _resolve_obj_display_sop(gn)
                        hint = {
                            "obj_path": pth,
                            "display_sop_path": dn.path() if dn is not None else None,
                            "display_resolve_method": how or None,
                        }
                        if dn is not None:
                            hint.update(_geo_topology_hint_for_display_sop(dn))
                        geo_hints.append(hint)
                        collected += 1
                pay["geo_display_hints"] = geo_hints
            return _result(True, data=pay)
        except Exception as e:
            return _result(False, errors=[f"scene.summary failed: {e}"])

    if op == "session.snapshot":
        inc_d = bool(args.get("include_desktop", False))
        if dry_run:
            return _result(True, data={"preview": f"session.snapshot include_desktop={inc_d}"})
        try:
            return _result(True, data=_session_snapshot_payload(hou, include_desktop=inc_d))
        except Exception as e:
            return _result(False, errors=[f"session.snapshot failed: {e}"])

    if op == "viewport.snapshot":
        """Write SceneViewer view(s) to disk via flipbook; requires GUI session.

        Single frame (default): current playbar, or ``frame_start`` only.
        Range: ``frame_end`` set → ``frame_start``..``frame_end`` inclusive with ``frame_step`` (one flipbook; basename gets ``.$F4`` if no ``$F`` token).
        Sparse: ``frames`` JSON array (or list) → one image per frame (playbar restored if ``restore_playbar_frame``).
        """
        import base64 as _b64
        import glob as _glob
        import json as _json
        import os as _os
        import re as _re

        try:
            _mvf = int(_os.getenv("HOUDINI_MCP_MAX_VIEWPORT_FRAMES", "96"))
        except ValueError:
            _mvf = 96
        MAX_VIEWPORT_FRAMES = max(1, min(512, _mvf))

        def _basename_has_frame_token(name: str) -> bool:
            return bool(_re.search(r"\$F\d*", name))

        def _inject_f4_basename(name_raw: str) -> str:
            if _basename_has_frame_token(name_raw):
                return name_raw
            stem, ext = _os.path.splitext(name_raw)
            return f"{stem}.$F4{ext}" if ext else f"{name_raw}.$F4"

        outp_raw = str(args.get("output_path") or args.get("path") or "$HIP/mcp_viewport_snapshot.png").strip()
        if not outp_raw:
            return _result(False, errors=["viewport.snapshot requires output_path (or path)"])

        saved_playbar = float(hou.frame())
        restore_pb = bool(args.get("restore_playbar_frame", args.get("restore_timeline", True)))

        frame_start_arg = args.get("frame_start")
        frame_end_arg = args.get("frame_end")
        try:
            frame_step = float(args.get("frame_step") or 1.0)
        except Exception:
            frame_step = 1.0
        if frame_step <= 0:
            frame_step = 1.0

        frames_arg = args.get("frames")
        if isinstance(frames_arg, str) and frames_arg.strip():
            try:
                frames_arg = _json.loads(frames_arg)
            except Exception:
                frames_arg = None
        sparse_list: list[float] | None = None
        if isinstance(frames_arg, list):
            sparse_list = []
            for x in frames_arg:
                try:
                    sparse_list.append(float(x))
                except Exception:
                    continue
            if not sparse_list:
                sparse_list = None

        range_mode = frame_end_arg is not None
        sparse_mode = sparse_list is not None

        if sparse_mode and range_mode:
            return _result(False, errors=["viewport.snapshot: use either `frames` or frame_start/frame_end, not both"])

        def _embed_viewport_b64(data_out: dict[str, Any], paths: list[str]) -> None:
            """Optional PNG/JPEG payloads for multimodal agents (paths on Houdini host)."""
            if not bool(args.get("include_image_base64")):
                return
            try:
                max_b = int(args.get("max_image_bytes_per_file") or 1_200_000)
            except Exception:
                max_b = 1_200_000
            max_b = max(4096, min(max_b, 6_000_000))
            try:
                max_n = int(args.get("max_images_embedded") or 3)
            except Exception:
                max_n = 3
            max_n = max(1, min(max_n, 8))
            if not paths:
                return
            chosen = list(paths)
            if len(chosen) > max_n:
                if max_n == 1:
                    chosen = [chosen[-1]]
                else:
                    idxs = [int(round(i * (len(chosen) - 1) / (max_n - 1))) for i in range(max_n)]
                    chosen = [chosen[i] for i in sorted(set(idxs))]
            out: list[dict[str, Any]] = []
            for p in chosen:
                ext = _os.path.splitext(p)[1].lower()
                mime = (
                    "image/png"
                    if ext == ".png"
                    else "image/jpeg"
                    if ext in (".jpg", ".jpeg")
                    else "application/octet-stream"
                )
                try:
                    with open(p, "rb") as fh:
                        blob = fh.read(max_b + 1)
                    if len(blob) > max_b:
                        out.append(
                            {
                                "path": p,
                                "mime": mime,
                                "data_base64": None,
                                "error": f"file larger than max_image_bytes_per_file ({max_b})",
                            }
                        )
                        continue
                    out.append(
                        {
                            "path": p,
                            "mime": mime,
                            "data_base64": _b64.standard_b64encode(blob).decode("ascii"),
                            "error": None,
                        }
                    )
                except Exception as e:
                    out.append({"path": p, "mime": mime, "data_base64": None, "error": str(e)})
            data_out["viewport_images"] = out

        if dry_run:
            ib = bool(args.get("include_image_base64"))
            vf = _mcp_viewport_autoframe_mode(args)
            return _result(
                True,
                data={
                    "preview": (
                        f"viewport.snapshot -> {outp_raw!r} sparse={sparse_mode} range={range_mode} "
                        f"embed_b64={ib} autoframe={vf}"
                    )
                },
            )

        def _flipbook_once(
            flip_tab: Any,
            outp_path: str,
            lo: float,
            hi: float,
            step: float,
        ) -> tuple[bool, list[str], list[str]]:
            errs: list[str] = []
            try:
                vpt = flip_tab.curViewport()
            except Exception:
                vpt = None
            settings = None
            try:
                sb = flip_tab.flipbookSettings()
                settings = sb.stash() if hasattr(sb, "stash") else sb
            except Exception:
                settings = None
            if settings is None or vpt is None:
                return False, [], ["no viewport or flipbook settings"]
            d0 = _os.path.dirname(outp_path)
            out_name_raw = _os.path.basename(outp_path) or "mcp_viewport_snapshot.png"
            multi = hi > lo + 1e-9
            if multi and not _basename_has_frame_token(out_name_raw):
                out_name_raw = _inject_f4_basename(out_name_raw)
            if d0:
                outp_use = f"{d0}/{out_name_raw}".replace("\\", "/")
            else:
                outp_use = out_name_raw

            for set_name in ("output", "setOutputPath", "setOutput", "setFilename", "setOutputFile", "filename"):
                m = getattr(settings, set_name, None)
                if callable(m):
                    try:
                        m(outp_use)
                        break
                    except Exception:
                        continue
            for mp_name, mp_val in (
                ("outputToMPlay", False),
                ("setOutputToMPlay", False),
                ("setUseMPlay", False),
            ):
                m = getattr(settings, mp_name, None)
                if callable(m):
                    try:
                        m(mp_val)
                        break
                    except Exception:
                        continue
            for fr_name in ("frameRange", "setFrameRange"):
                m = getattr(settings, fr_name, None)
                if callable(m):
                    try:
                        m((float(lo), float(hi)))
                        break
                    except Exception:
                        continue
            if multi and abs(float(step) - 1.0) > 1e-9:
                for incn in ("frameIncrement", "setFrameIncrement"):
                    incm = getattr(settings, incn, None)
                    if callable(incm):
                        try:
                            incm(float(step))
                            break
                        except Exception:
                            continue
            fb = getattr(flip_tab, "flipbook", None)
            if not callable(fb):
                return False, [], ["pane has no flipbook()"]
            called = False
            for call in (lambda: fb(vpt, settings), lambda: fb(settings, vpt)):
                try:
                    call()
                    called = True
                    break
                except Exception as e:
                    errs.append(str(e))
            if not called:
                try:
                    fb(settings)
                    called = True
                except Exception as e:
                    errs.append(str(e))
            if not called:
                joined = "; ".join(errs[-3:]) if errs else "flipbook failed"
                return False, [], [joined]

            out_dir_raw2 = _os.path.dirname(outp_use)
            out_name_use = _os.path.basename(outp_use)
            out_dir2b = str(hou.expandString(out_dir_raw2)) if out_dir_raw2 else ""
            if not out_dir2b:
                out_dir2b = "."
            frame_token = _re.search(r"\$F\d*", out_name_use)
            if frame_token:
                pre = out_name_use[: frame_token.start()]
                post = out_name_use[frame_token.end() :]
                glob_pat = _os.path.join(out_dir2b, f"{pre}*{post}")
            else:
                glob_pat = _os.path.join(out_dir2b, out_name_use)
            glob_pat = str(hou.expandString(glob_pat))
            files = sorted(_glob.glob(glob_pat))
            if not files:
                return False, [], [f"no file on disk; checked {glob_pat!r}"]
            return True, files, []

        try:
            ui = getattr(hou, "ui", None)
            if ui is None:
                return _result(False, errors=["viewport.snapshot: hou.ui not available (headless?)"])
            try:
                out_dir_raw = _os.path.dirname(outp_raw)
                if out_dir_raw:
                    _os.makedirs(str(hou.expandString(out_dir_raw)), exist_ok=True)
            except Exception:
                pass
            desktop = ui.curDesktop()
            flip_tab = None
            for pt in desktop.paneTabs():
                try:
                    if pt.type() == hou.paneTabType.SceneViewer:
                        flip_tab = pt
                        break
                except Exception:
                    continue
            if flip_tab is None:
                return _result(False, errors=["viewport.snapshot: no SceneViewer pane tab"])

            all_files: list[str] = []
            af_snap: dict[str, Any] = {}

            if sparse_mode and sparse_list is not None:
                if len(sparse_list) > MAX_VIEWPORT_FRAMES:
                    return _result(
                        False,
                        errors=[f"viewport.snapshot: too many frames ({len(sparse_list)} > {MAX_VIEWPORT_FRAMES})"],
                    )
                d_raw, n_raw = _os.path.dirname(outp_raw), _os.path.basename(outp_raw)
                if len(sparse_list) > 1 and not _basename_has_frame_token(n_raw):
                    n_raw = _inject_f4_basename(n_raw)
                outp_tmpl = f"{d_raw}/{n_raw}".replace("\\", "/") if d_raw else n_raw
                for fv in sparse_list:
                    hou.setFrame(float(fv))
                    af_snap = _mcp_autoframe_sceneviewer(hou, flip_tab, args)
                    ok, files, ferr = _flipbook_once(flip_tab, outp_tmpl, float(fv), float(fv), 1.0)
                    if not ok:
                        return _result(False, errors=[f"viewport.snapshot: {'; '.join(ferr)}"])
                    all_files.extend(files)
                if restore_pb:
                    try:
                        hou.setFrame(saved_playbar)
                    except Exception:
                        pass
                data_sparse = {
                    "output_path": all_files[-1] if all_files else "",
                    "output_paths": all_files,
                    "frames": sparse_list,
                    "file_count": len(all_files),
                    "output_template": outp_raw,
                    "mode": "sparse",
                    "viewport_autoframe": af_snap,
                }
                _embed_viewport_b64(data_sparse, all_files)
                return _result(True, data=data_sparse)

            if range_mode:
                start = float(frame_start_arg) if frame_start_arg is not None else saved_playbar
                end = float(frame_end_arg)
                if end < start:
                    start, end = end, start
                seq: list[float] = []
                t = float(start)
                guard = 0
                while t <= end + 1e-6 and guard < MAX_VIEWPORT_FRAMES + 8:
                    seq.append(float(t))
                    t += frame_step
                    guard += 1
                if len(seq) > MAX_VIEWPORT_FRAMES:
                    return _result(
                        False,
                        errors=[
                            f"viewport.snapshot: range yields {len(seq)} frames (max {MAX_VIEWPORT_FRAMES}); increase frame_step or narrow range",
                        ],
                    )
                lo, hi = seq[0], seq[-1]
                try:
                    hou.setFrame(float(lo))
                except Exception:
                    pass
                af_snap = _mcp_autoframe_sceneviewer(hou, flip_tab, args)
                ok, files, ferr = _flipbook_once(flip_tab, outp_raw, lo, hi, frame_step if len(seq) > 1 else 1.0)
                if not ok:
                    return _result(False, errors=[f"viewport.snapshot: {'; '.join(ferr)}"])
                if restore_pb:
                    try:
                        hou.setFrame(saved_playbar)
                    except Exception:
                        pass
                data_range = {
                    "output_path": files[-1] if files else "",
                    "output_paths": files,
                    "frame_start": lo,
                    "frame_end": hi,
                    "frame_step": frame_step,
                    "file_count": len(files),
                    "output_template": outp_raw,
                    "mode": "range",
                    "viewport_autoframe": af_snap,
                }
                _embed_viewport_b64(data_range, files)
                return _result(True, data=data_range)

            # single frame
            target = float(frame_start_arg) if frame_start_arg is not None else saved_playbar
            try:
                hou.setFrame(target)
            except Exception:
                pass
            af_snap = _mcp_autoframe_sceneviewer(hou, flip_tab, args)
            cur_f = float(hou.frame())
            ok, files, ferr = _flipbook_once(flip_tab, outp_raw, cur_f, cur_f, 1.0)
            if not ok:
                return _result(False, errors=[f"viewport.snapshot: {'; '.join(ferr)}"])
            if restore_pb and frame_start_arg is not None:
                try:
                    hou.setFrame(saved_playbar)
                except Exception:
                    pass
            data_single = {
                "output_path": files[-1],
                "frame": cur_f,
                "file_count": len(files),
                "output_template": outp_raw,
                "mode": "single",
                "viewport_autoframe": af_snap,
            }
            _embed_viewport_b64(data_single, files)
            return _result(True, data=data_single)
        except Exception as e:
            return _result(False, errors=[f"viewport.snapshot failed: {e}"])

    if op == "shelf.run_tool":
        tp = str(args.get("tool_path") or args.get("tool_name") or args.get("name") or "")
        if not tp.strip():
            return _result(False, errors=["shelf.run_tool requires tool_path (or tool_name / name)"])
        if dry_run:
            return _result(True, data={"preview": f"shelf.run_tool {tp!r}"})
        ok, method, serrs = _try_run_shelf_tool(hou, tp)
        if ok:
            wmsg = (serrs or [])[:8]
            return _result(True, warnings=wmsg or None, data={"tool_path": tp.strip(), "method": method})
        msg = "; ".join(serrs) if serrs else "unknown"
        return _result(False, errors=[f"shelf.run_tool failed: {msg}"])

    if op == "node.preset_apply":
        path = str(args.get("node_path") or "")
        pname = str(args.get("preset_name") or args.get("name") or args.get("preset") or "")
        if not path.strip() or not pname.strip():
            return _result(
                False,
                errors=["node.preset_apply requires node_path and preset_name (or name / preset)"],
            )
        if dry_run:
            return _result(True, data={"preview": f"node.preset_apply {path!r} {pname!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        ok, method, perrs = _try_apply_node_preset(hou, n, pname)
        if ok:
            return _result(True, data={"node_path": path, "preset_name": pname, "method": method})
        msg = "; ".join(perrs) if perrs else "unknown"
        return _result(False, errors=[f"node.preset_apply failed: {msg}"])

    if op == "obj.xform_get":
        path = str(args.get("obj_path") or args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["obj.xform_get requires obj_path (or node_path)"])
        if dry_run:
            return _result(True, data={"preview": f"obj.xform_get {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            tr, tn = _read_vec3_parm_tuple(n, ("t", "translate"))
            rr, rn = _read_vec3_parm_tuple(n, ("r", "rot", "rotate"))
            sr, sn = _read_vec3_parm_tuple(n, ("s", "scale"))
            return _result(
                True,
                data={
                    "node_path": path,
                    "translate": tr,
                    "translate_parm": tn,
                    "rotate": rr,
                    "rotate_parm": rn,
                    "scale": sr,
                    "scale_parm": sn,
                },
            )
        except Exception as e:
            return _result(False, errors=[f"obj.xform_get failed: {e}"])

    if op == "obj.xform_set":
        path = str(args.get("obj_path") or args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["obj.xform_set requires obj_path (or node_path)"])
        traw = args.get("translate") if args.get("translate") is not None else args.get("t")
        rraw = args.get("rotate") if args.get("rotate") is not None else args.get("r")
        sraw = args.get("scale") if args.get("scale") is not None else args.get("s")
        tv = _coerce_xyz_vector(traw)
        rv = _coerce_xyz_vector(rraw)
        sv = _coerce_xyz_vector(sraw)
        if tv is None and rv is None and sv is None:
            return _result(
                False,
                errors=["obj.xform_set requires at least one of translate, rotate, scale (or t, r, s)"],
            )
        if dry_run:
            return _result(
                True,
                data={"preview": f"obj.xform_set {path!r}", "translate": tv, "rotate": rv, "scale": sv},
            )
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            resolved: dict[str, Any] = {"node_path": path}
            if tv is not None:
                used = _set_vec3_parm_tuple(n, ("t", "translate"), tv)
                if used is None:
                    return _result(False, errors=["obj.xform_set: no translate parm tuple (t/translate)"])
                resolved["translate_parm"] = used
            if rv is not None:
                used = _set_vec3_parm_tuple(n, ("r", "rot", "rotate"), rv)
                if used is None:
                    return _result(False, errors=["obj.xform_set: no rotate parm tuple (r/rotate/rot)"])
                resolved["rotate_parm"] = used
            if sv is not None:
                used = _set_vec3_parm_tuple(n, ("s", "scale"), sv)
                if used is None:
                    return _result(False, errors=["obj.xform_set: no scale parm tuple (s/scale)"])
                resolved["scale_parm"] = used
            return _result(True, data=resolved)
        except Exception as e:
            return _result(False, errors=[f"obj.xform_set failed: {e}"])

    if op == "obj.world_transform_get":
        path = str(args.get("obj_path") or args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["obj.world_transform_get requires obj_path (or node_path)"])
        if dry_run:
            return _result(True, data={"preview": f"obj.world_transform_get {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            payload = _world_matrix_payload(hou, n)
            if payload.get("error"):
                return _result(False, errors=[str(payload["error"])])
            payload["node_path"] = path
            return _result(True, data=payload)
        except Exception as e:
            return _result(False, errors=[f"obj.world_transform_get failed: {e}"])

    if op == "obj.local_transform_get":
        path = str(args.get("obj_path") or args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["obj.local_transform_get requires obj_path (or node_path)"])
        if dry_run:
            return _result(True, data={"preview": f"obj.local_transform_get {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        try:
            payload = _local_matrix_payload(hou, n)
            if payload.get("error"):
                return _result(False, errors=[str(payload["error"])])
            payload["node_path"] = path
            return _result(True, data=payload)
        except Exception as e:
            return _result(False, errors=[f"obj.local_transform_get failed: {e}"])

    if op == "undo.begin":
        if dry_run:
            return _result(True, data={"preview": "undo.begin (no-op when batch groups)"})
        return _result(True, data={"noop": True})

    if op == "undo.end":
        if dry_run:
            return _result(True, data={"preview": "undo.end (no-op when batch groups)"})
        return _result(True, data={"noop": True})

    if op == "undo.rollback":
        if dry_run:
            return _result(True, data={"preview": "undo.rollback"})
        try:
            hou.undos.performUndo()
            return _result(True, data={"undid": True})
        except Exception as e:
            return _result(False, errors=[str(e)])

    if op == "validate.node":
        path = str(args.get("node_path") or "")
        if dry_run:
            return _result(True, data={"preview": f"validate.node {path}"})
        n = hou.node(path)
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        errs = [e for e in (n.errors() or [])]
        if errs:
            return _result(False, errors=errs, data={"node_path": path})
        return _result(True, data={"node_path": path})

    if op == "validate.parm_range":
        path = str(args.get("node_path") or "")
        if not path.strip():
            return _result(False, errors=["validate.parm_range requires node_path"])
        if dry_run:
            return _result(True, data={"preview": f"validate.parm_range {path!r}"})
        n = hou.node(path.strip())
        if n is None:
            return _result(False, errors=[f"Node not found: {path}"])
        r = _validate_parm_range_core(hou, n, args)
        if not r.get("ok"):
            return _result(False, errors=r.get("errors") or ["validate.parm_range failed"])
        det = r.get("detail") or {}
        in_range = bool(r.get("in_range"))
        w2: list[str] = list(warnings)
        if not det.get("checks_applied"):
            w2.append("validate.parm_range: min/max not defined for this parm template; in_range may be informational only.")
        if not in_range:
            return _result(False, errors=[f"validate.parm_range: value out of template range {det.get('min')}..{det.get('max')}"], data=det)
        return _result(True, warnings=w2 or None, data=det)

    return _result(False, errors=[f"Unknown op: {op!r}"])


def _handle_tool(request_id: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    import hou  # type: ignore

    if tool == "health.ping":
        return _wire_response(
            request_id,
            _result(
                True,
                data={
                    "receiver": RECEIVER_VERSION,
                    "houdini": hou.applicationVersionString(),
                    "pid": hou.applicationName(),
                },
            ),
        )

    if tool == "core.dispatch":
        op = str(args.get("op") or "")
        inner = _dispatch_core(op, args, dry_run=False)
        return _wire_response(request_id, inner, ok=inner.get("ok"))

    if tool == "batch.execute":
        actions = args.get("actions") or []
        dry_run = bool(args.get("dry_run"))
        undo_label = str(args.get("undo_label") or "mcp_batch")
        if not isinstance(actions, list):
            return _wire_response(
                request_id,
                _result(False, errors=["batch.execute requires actions:list"]),
                ok=False,
            )

        step_results: list[dict[str, Any]] = []
        if dry_run:
            for i, a in enumerate(actions):
                if not isinstance(a, dict):
                    step_results.append({"index": i, "ok": False, "errors": ["invalid action"]})
                    continue
                op = str(a.get("op") or "")
                inner = _dispatch_core(op, a, dry_run=True)
                step_results.append({"index": i, "op": op, **inner})
            inner = _result(True, data={"dry_run": True, "steps": step_results})
            return _wire_response(request_id, inner, ok=True)

        try:
            with hou.undos.group(undo_label):
                for i, a in enumerate(actions):
                    if not isinstance(a, dict):
                        step_results.append({"index": i, "ok": False, "errors": ["invalid action"]})
                        raise RuntimeError("invalid action")
                    op = str(a.get("op") or "")
                    inner = _dispatch_core(op, a, dry_run=False)
                    step_results.append({"index": i, "op": op, **inner})
                    if not inner.get("ok"):
                        raise RuntimeError("; ".join(inner.get("errors") or ["step failed"]))
            layout_ok, layout_errs, layout_skipped = _layout_networks_after_batch(
                [a for a in actions if isinstance(a, dict)]
            )
            inner_data: dict[str, Any] = {
                "dry_run": False,
                "steps": step_results,
                "batch_layout_parents": layout_ok,
                "batch_layout_skipped_parents": layout_skipped,
            }
            if layout_errs:
                inner_data["batch_layout_errors"] = layout_errs
            warn_merge: list[str] = []
            if layout_errs:
                warn_merge.extend(layout_errs)
            if layout_skipped:
                warn_merge.append(
                    "batch auto-layout skipped for parents with node.set_position (would overwrite positions): "
                    + ", ".join(layout_skipped)
                )
            return _wire_response(request_id, _result(True, data=inner_data, warnings=warn_merge or None), ok=True)
        except Exception as e:
            tb = traceback.format_exc()
            return _wire_response(
                request_id,
                _result(
                    False,
                    data={"steps": step_results, "traceback": tb},
                    errors=[str(e)],
                ),
                ok=False,
            )

    return _wire_response(
        request_id,
        _result(False, errors=[f"Unknown tool: {tool!r}"]),
        ok=False,
    )


def _client_loop(conn: socket.socket) -> None:
    buf = b""
    try:
        while True:
            chunk = conn.recv(65536)
            if not chunk:
                break
            buf += chunk
            if b"\n" not in buf:
                continue
            line, buf = buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                req = json.loads(line.decode("utf-8"))
            except json.JSONDecodeError as e:
                out = _wire_response(
                    str(uuid.uuid4()),
                    _result(False, errors=[f"Invalid JSON: {e}"]),
                    ok=False,
                )
                conn.sendall((json.dumps(out, ensure_ascii=False) + "\n").encode("utf-8"))
                continue

            request_id = str(req.get("request_id") or uuid.uuid4())
            tool = str(req.get("tool") or "")
            args = req.get("args") or {}
            if not isinstance(args, dict):
                args = {}

            try:
                resp = _handle_tool(request_id, tool, args)
            except Exception:
                resp = _wire_response(
                    request_id,
                    _result(False, errors=["receiver crash", traceback.format_exc()]),
                    ok=False,
                )
            conn.sendall((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _serve_forever() -> None:
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((HOST, PORT))
    srv.listen(8)
    while True:
        conn, _addr = srv.accept()
        t = threading.Thread(target=_client_loop, args=(conn,), daemon=True)
        t.start()


def start_receiver() -> None:
    """Start TCP server thread (call from Houdini)."""
    t = threading.Thread(target=_serve_forever, daemon=True)
    t.start()


if __name__ == "__main__":
    raise RuntimeError("Import this module inside Houdini and call start_receiver().")
