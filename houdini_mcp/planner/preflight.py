"""Static preflight before touching Houdini (light checks; no hou calls)."""

from __future__ import annotations

from typing import Any

from planner.preflight_suggestions import suggestions_for_preflight_errors


def _is_abs_houdini_path(s: str) -> bool:
    t = (s or "").strip()
    return len(t) >= 2 and t.startswith("/") and not t.startswith("//")


def preflight_plan(plan: dict[str, Any]) -> dict[str, Any]:
    warnings: list[str] = []
    errors: list[str] = []

    rid = plan.get("recipe_id")
    if rid is not None and not str(rid).strip():
        warnings.append("plan.recipe_id is empty")

    actions = plan.get("actions")
    if not isinstance(actions, list):
        errors.append("plan.actions must be a list")
        return {
            "ok": False,
            "warnings": warnings,
            "errors": errors,
            "suggestions": suggestions_for_preflight_errors(errors),
        }
    if not actions:
        warnings.append("plan has zero actions")

    for i, a in enumerate(actions):
        if not isinstance(a, dict) or not a.get("op"):
            errors.append(f"actions[{i}] must be a dict with 'op'")
            continue
        op = str(a.get("op"))

        if op == "node.create":
            pp = str(a.get("parent_path") or "")
            nt = str(a.get("node_type") or "")
            if not pp.strip():
                errors.append(f"actions[{i}] node.create requires non-empty parent_path")
            elif not _is_abs_houdini_path(pp):
                warnings.append(f"actions[{i}] parent_path does not look like an absolute node path: {pp!r}")
            if not nt.strip():
                errors.append(f"actions[{i}] node.create requires non-empty node_type")

        if op == "node.connect":
            for key in ("src", "dst"):
                v = str(a.get(key) or "")
                if not v.strip():
                    errors.append(f"actions[{i}] node.connect requires non-empty {key}")
                elif not _is_abs_houdini_path(v):
                    warnings.append(f"actions[{i}] node.connect {key} not absolute path: {v!r}")

        if op == "parm.set":
            np = str(a.get("node_path") or "")
            pn = str(a.get("parm_name") or "")
            if not np.strip():
                errors.append(f"actions[{i}] parm.set requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] parm.set node_path not absolute path: {np!r}")
            if not pn.strip():
                errors.append(f"actions[{i}] parm.set requires non-empty parm_name")

        if op in (
            "node.delete",
            "node.rename",
            "node.set_position",
            "node.set_comment",
            "node.set_color",
            "node.info",
            "node.duplicate",
            "node.bypass",
            "node.lock",
            "node.set_selectable",
            "node.set_flag",
            "node.list_inputs",
            "node.list_outputs",
            "node.references_list",
            "node.dependents_list",
        ):
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] {op} requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] {op} node_path not absolute path: {np!r}")
            if op == "node.rename" and not str(a.get("new_name") or "").strip():
                errors.append(f"actions[{i}] node.rename requires non-empty new_name")

        if op == "node.change_type":
            np = str(a.get("node_path") or "")
            nt = str(a.get("node_type") or a.get("type_name") or "")
            if not np.strip():
                errors.append(f"actions[{i}] node.change_type requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] node.change_type node_path not absolute path: {np!r}")
            if not nt.strip():
                errors.append(f"actions[{i}] node.change_type requires non-empty node_type (or type_name)")

        if op == "node.match_definition":
            np = str(a.get("node_path") or "")
            df = str(a.get("definition") or a.get("type_name") or "")
            if not np.strip():
                errors.append(f"actions[{i}] node.match_definition requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] node.match_definition node_path not absolute path: {np!r}")
            if not df.strip():
                errors.append(f"actions[{i}] node.match_definition requires non-empty definition (or type_name)")

        if op == "node.reparent":
            np = str(a.get("node_path") or "")
            pp = str(a.get("parent_path") or a.get("new_parent_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] node.reparent requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] node.reparent node_path not absolute path: {np!r}")
            if not pp.strip():
                errors.append(f"actions[{i}] node.reparent requires non-empty parent_path")
            elif not _is_abs_houdini_path(pp):
                warnings.append(f"actions[{i}] node.reparent parent_path not absolute path: {pp!r}")

        if op == "network.set_current_node":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] network.set_current_node requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] network.set_current_node node_path not absolute path: {np!r}")

        if op == "node.disconnect":
            dst = str(a.get("dst") or "")
            if not dst.strip():
                errors.append(f"actions[{i}] node.disconnect requires non-empty dst")
            elif not _is_abs_houdini_path(dst):
                warnings.append(f"actions[{i}] node.disconnect dst not absolute path: {dst!r}")

        if op in ("parm.get_raw", "parm.exists"):
            np = str(a.get("node_path") or "")
            pn = str(a.get("parm_name") or "")
            if not np.strip():
                errors.append(f"actions[{i}] {op} requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] {op} node_path not absolute path: {np!r}")
            if not pn.strip():
                errors.append(f"actions[{i}] {op} requires non-empty parm_name")

        if op in ("parm.set_expression", "parm.revert_defaults", "parm.list"):
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] {op} requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] {op} node_path not absolute path: {np!r}")
            if op == "parm.set_expression":
                if not str(a.get("parm_name") or "").strip():
                    errors.append(f"actions[{i}] parm.set_expression requires non-empty parm_name")

        if op in ("parm.press_button", "parm.clear_keyframes"):
            np = str(a.get("node_path") or "")
            pn = str(a.get("parm_name") or "")
            if not np.strip():
                errors.append(f"actions[{i}] {op} requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] {op} node_path not absolute path: {np!r}")
            if not pn.strip():
                errors.append(f"actions[{i}] {op} requires non-empty parm_name")

        if op == "parm.multiparm_resize":
            np = str(a.get("node_path") or "")
            fld = str(a.get("folder_parm") or a.get("parm_name") or "")
            if not np.strip():
                errors.append(f"actions[{i}] parm.multiparm_resize requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] parm.multiparm_resize node_path not absolute path: {np!r}")
            if not fld.strip():
                errors.append(f"actions[{i}] parm.multiparm_resize requires folder_parm (or parm_name)")
            if a.get("count") is None and a.get("num_instances") is None:
                errors.append(f"actions[{i}] parm.multiparm_resize requires count (or num_instances)")

        if op in ("hip.load", "hip.merge"):
            fp = str(a.get("file_path") or "")
            if not fp.strip():
                errors.append(f"actions[{i}] {op} requires non-empty file_path")

        if op == "graph.glob":
            gp = str(a.get("parent_path") or a.get("path") or "")
            if not gp.strip():
                errors.append(f"actions[{i}] graph.glob requires non-empty parent_path (or path)")
            elif not _is_abs_houdini_path(gp):
                warnings.append(f"actions[{i}] graph.glob parent not absolute path: {gp!r}")

        if op == "graph.layout_children":
            gp = str(a.get("parent_path") or a.get("path") or "")
            if not gp.strip():
                errors.append(f"actions[{i}] graph.layout_children requires non-empty parent_path (or path)")
            elif not _is_abs_houdini_path(gp):
                warnings.append(f"actions[{i}] graph.layout_children parent not absolute path: {gp!r}")

        if op in (
            "geo.info",
            "geo.bounding_box",
            "geo.point_count",
            "geo.primitive_count",
            "geo.vertex_count",
            "geo.interpolate_p",
            "geo.list_attribs",
            "geo.sample_points",
            "geo.sample_primitives",
            "geo.groups_list",
            "geo.group_count",
            "geo.primitive_type_breakdown",
            "geo.has_packed_primitives",
            "geo.detail_attrib_get",
            "geo.is_empty",
            "geo.topology_summary",
            "attrib.summary",
            "attrib.exists",
        ):
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] {op} requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] {op} node_path not absolute path: {np!r}")
            if op == "attrib.summary" and not str(a.get("name") or a.get("attrib_name") or "").strip():
                errors.append(f"actions[{i}] attrib.summary requires non-empty name (or attrib_name)")
            if op == "attrib.exists" and not str(a.get("name") or a.get("attrib_name") or "").strip():
                errors.append(f"actions[{i}] attrib.exists requires non-empty name (or attrib_name)")
            if op == "geo.detail_attrib_get" and not str(a.get("name") or a.get("attrib_name") or "").strip():
                errors.append(f"actions[{i}] geo.detail_attrib_get requires non-empty name (or attrib_name)")

        if op == "geo.save_to_file":
            np = str(a.get("node_path") or "")
            fp = str(a.get("file_path") or a.get("path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] geo.save_to_file requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] geo.save_to_file node_path not absolute path: {np!r}")
            if not fp.strip():
                errors.append(f"actions[{i}] geo.save_to_file requires non-empty file_path (or path)")

        if op == "rop.evaluate_path":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] rop.evaluate_path requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] rop.evaluate_path node_path not absolute path: {np!r}")

        if op in ("exec.render_rop", "exec.render_write", "exec.node_execute"):
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] {op} requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] {op} node_path not absolute path: {np!r}")

        if op == "exec.python":
            c = str(a.get("code") or a.get("source") or "")
            if not c.strip():
                errors.append(f"actions[{i}] exec.python requires non-empty code (or source)")

        if op == "mcp.ctrl_null_setup":
            pp = str(a.get("parent_path") or "")
            if not pp.strip():
                errors.append(f"actions[{i}] mcp.ctrl_null_setup requires parent_path")
            elif not _is_abs_houdini_path(pp):
                warnings.append(f"actions[{i}] mcp.ctrl_null_setup parent_path not absolute path: {pp!r}")
            inp = str(a.get("input_from") or "")
            if inp.strip() and not _is_abs_houdini_path(inp):
                warnings.append(f"actions[{i}] mcp.ctrl_null_setup input_from not absolute path: {inp!r}")
            for j, b in enumerate(a.get("bindings") or []):
                if not isinstance(b, dict):
                    continue
                rn = str(b.get("ref_node") or b.get("node_path") or "")
                if rn.strip() and not _is_abs_houdini_path(rn):
                    warnings.append(f"actions[{i}] mcp.ctrl_null_setup bindings[{j}] ref_node not absolute: {rn!r}")

        if op == "scene.summary":
            for key, lo, hi in (
                ("max_selected_detail_nodes", 0, 32),
                ("max_parms_per_node", 1, 128),
                ("geo_hint_max_geos", 0, 32),
            ):
                v = a.get(key)
                if v is None:
                    continue
                try:
                    iv = int(v)
                    if iv < lo or iv > hi:
                        warnings.append(
                            f"actions[{i}] scene.summary {key}={iv} outside recommended [{lo},{hi}] (receiver clamps)"
                        )
                except (TypeError, ValueError):
                    warnings.append(f"actions[{i}] scene.summary {key} is not an int")

        if op == "viewport.snapshot":
            outp = str(a.get("output_path") or a.get("path") or "")
            if outp.strip() and not outp.strip().startswith("$") and not _is_abs_houdini_path(outp):
                warnings.append(f"actions[{i}] viewport.snapshot output_path not absolute (vars like $HIP are ok): {outp!r}")

        if op == "material.assign_object":
            op_path = str(a.get("obj_path") or a.get("object_path") or a.get("node_path") or "")
            mp = str(a.get("material_path") or a.get("mat_path") or "")
            if not op_path.strip():
                errors.append(f"actions[{i}] material.assign_object requires obj_path (or node_path)")
            elif not _is_abs_houdini_path(op_path):
                warnings.append(f"actions[{i}] material.assign_object obj_path not absolute path: {op_path!r}")
            if not mp.strip():
                errors.append(f"actions[{i}] material.assign_object requires material_path")
            elif not _is_abs_houdini_path(mp):
                warnings.append(f"actions[{i}] material.assign_object material_path not absolute path: {mp!r}")

        if op == "material.clear_object":
            op_path = str(a.get("obj_path") or a.get("object_path") or a.get("node_path") or "")
            if not op_path.strip():
                errors.append(f"actions[{i}] material.clear_object requires obj_path (or node_path)")
            elif not _is_abs_houdini_path(op_path):
                warnings.append(f"actions[{i}] material.clear_object obj_path not absolute path: {op_path!r}")

        if op == "lop.stage_summary":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] lop.stage_summary requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] lop.stage_summary node_path not absolute path: {np!r}")

        if op == "solaris.usd_file_set":
            np = str(a.get("node_path") or "")
            fp = str(a.get("file_path") or a.get("path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] solaris.usd_file_set requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] solaris.usd_file_set node_path not absolute path: {np!r}")
            if not fp.strip():
                errors.append(f"actions[{i}] solaris.usd_file_set requires non-empty file_path")

        if op == "solaris.karma_render_set":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] solaris.karma_render_set requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] solaris.karma_render_set node_path not absolute path: {np!r}")
            has_k = any(
                a.get(k) is not None
                for k in (
                    "picture",
                    "picture_path",
                    "camera",
                    "camera_path",
                    "width",
                    "res_width",
                    "height",
                    "res_height",
                    "override_resolution",
                    "enable_resolution_override",
                )
            )
            if not has_k:
                errors.append(f"actions[{i}] solaris.karma_render_set needs at least one of picture, camera, width, height, override_resolution")

        if op == "mtlx.texture_file_set":
            np = str(a.get("node_path") or "")
            fp = str(a.get("file_path") or a.get("path") or a.get("texture_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] mtlx.texture_file_set requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] mtlx.texture_file_set node_path not absolute path: {np!r}")
            if not fp.strip():
                errors.append(f"actions[{i}] mtlx.texture_file_set requires non-empty file_path")

        if op == "mtlx.standard_surface_set":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] mtlx.standard_surface_set requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] mtlx.standard_surface_set node_path not absolute path: {np!r}")
            has_m = any(
                a.get(k) is not None
                for k in ("roughness", "metallic", "metalness", "coat", "coat_weight", "base_color", "specular", "specular_color")
            )
            if not has_m:
                errors.append(f"actions[{i}] mtlx.standard_surface_set requires at least one shading field")

        if op == "subnet.collapse":
            raw = a.get("node_paths") or a.get("paths") or []
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list) or not raw:
                errors.append(f"actions[{i}] subnet.collapse requires non-empty node_paths")
            else:
                for p in raw:
                    ps = str(p or "").strip()
                    if not ps:
                        errors.append(f"actions[{i}] subnet.collapse node_paths must not contain empty strings")
                    elif not _is_abs_houdini_path(ps):
                        warnings.append(f"actions[{i}] subnet.collapse path not absolute: {ps!r}")

        if op == "hda.definition_save":
            np = str(a.get("node_path") or "")
            fp = str(a.get("file_path") or a.get("path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] hda.definition_save requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] hda.definition_save node_path not absolute path: {np!r}")
            if not fp.strip():
                errors.append(f"actions[{i}] hda.definition_save requires non-empty file_path")

        if op == "hda.install_file":
            fp = str(a.get("file_path") or a.get("path") or "")
            if not fp.strip():
                errors.append(f"actions[{i}] hda.install_file requires non-empty file_path")

        if op == "parm.keyframe_set":
            np = str(a.get("node_path") or "")
            pn = str(a.get("parm_name") or "")
            if not np.strip():
                errors.append(f"actions[{i}] parm.keyframe_set requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] parm.keyframe_set node_path not absolute path: {np!r}")
            if not pn.strip():
                errors.append(f"actions[{i}] parm.keyframe_set requires non-empty parm_name")
            if a.get("frame") is None:
                errors.append(f"actions[{i}] parm.keyframe_set requires frame")
            if a.get("value") is None:
                errors.append(f"actions[{i}] parm.keyframe_set requires value")

        if op == "parm.keyframe_list":
            np = str(a.get("node_path") or "")
            pn = str(a.get("parm_name") or "")
            if not np.strip():
                errors.append(f"actions[{i}] parm.keyframe_list requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] parm.keyframe_list node_path not absolute path: {np!r}")
            if not pn.strip():
                errors.append(f"actions[{i}] parm.keyframe_list requires non-empty parm_name")

        if op == "sop.vex_snippet_set":
            np = str(a.get("node_path") or "")
            has_code = a.get("code") is not None or a.get("snippet") is not None or a.get("vex") is not None or a.get("source") is not None
            if not np.strip():
                errors.append(f"actions[{i}] sop.vex_snippet_set requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] sop.vex_snippet_set node_path not absolute path: {np!r}")
            if not has_code:
                errors.append(f"actions[{i}] sop.vex_snippet_set requires code (or snippet/vex/source)")

        if op == "sop.vex_snippet_get":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] sop.vex_snippet_get requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] sop.vex_snippet_get node_path not absolute path: {np!r}")

        if op == "sop.wrangle_run_over_set":
            np = str(a.get("node_path") or "")
            rw = str(a.get("run_over") or a.get("run_class") or a.get("class") or a.get("domain") or "")
            if not np.strip():
                errors.append(f"actions[{i}] sop.wrangle_run_over_set requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] sop.wrangle_run_over_set node_path not absolute path: {np!r}")
            if not rw.strip():
                errors.append(f"actions[{i}] sop.wrangle_run_over_set requires run_over")

        if op == "sop.wrangle_group_set":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] sop.wrangle_group_set requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] sop.wrangle_group_set node_path not absolute path: {np!r}")
            if a.get("group") is None and a.get("group_mask") is None and a.get("pattern") is None:
                errors.append(f"actions[{i}] sop.wrangle_group_set requires group (or group_mask); use \"\" to clear")
            gtk = a.get("group_type", a.get("bind_type"))
            if gtk is not None and not str(gtk).strip():
                warnings.append(f"actions[{i}] sop.wrangle_group_set group_type is empty; receiver will use defaults")

        if op == "sop.wrangle_create":
            pp = str(a.get("parent_path") or "")
            if not pp.strip():
                errors.append(f"actions[{i}] sop.wrangle_create requires non-empty parent_path")
            elif not _is_abs_houdini_path(pp):
                warnings.append(f"actions[{i}] sop.wrangle_create parent_path not absolute path: {pp!r}")

        if op == "sop.camphor_tree_build":
            gp = str(a.get("parent_geo_path") or a.get("geo_path") or "")
            if not gp.strip():
                errors.append(f"actions[{i}] sop.camphor_tree_build requires parent_geo_path (Geometry OBJ path)")
            elif not _is_abs_houdini_path(gp):
                warnings.append(f"actions[{i}] sop.camphor_tree_build parent_geo_path not absolute path: {gp!r}")

        if op == "node.spare_parm_add":
            np = str(a.get("node_path") or "")
            pn = str(a.get("parm_name") or a.get("name") or "")
            if not np.strip():
                errors.append(f"actions[{i}] node.spare_parm_add requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] node.spare_parm_add node_path not absolute path: {np!r}")
            if not pn.strip():
                errors.append(f"actions[{i}] node.spare_parm_add requires parm_name (or name)")

        if op == "node.spare_parm_remove":
            np = str(a.get("node_path") or "")
            pn = str(a.get("parm_name") or a.get("name") or "")
            if not np.strip():
                errors.append(f"actions[{i}] node.spare_parm_remove requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] node.spare_parm_remove node_path not absolute path: {np!r}")
            if not pn.strip():
                errors.append(f"actions[{i}] node.spare_parm_remove requires parm_name (or name)")

        if op == "node.diagnostics":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] node.diagnostics requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] node.diagnostics node_path not absolute path: {np!r}")

        if op == "geo.sample_points":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] geo.sample_points requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] geo.sample_points node_path not absolute path: {np!r}")

        if op == "sop.wrangle_recompile":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] sop.wrangle_recompile requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] sop.wrangle_recompile node_path not absolute path: {np!r}")

        if op == "geo.group_count":
            np = str(a.get("node_path") or "")
            gn = str(a.get("group_name") or a.get("name") or "")
            if not np.strip():
                errors.append(f"actions[{i}] geo.group_count requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] geo.group_count node_path not absolute path: {np!r}")
            if not gn.strip():
                errors.append(f"actions[{i}] geo.group_count requires group_name (or name)")

        if op == "network.clipboard_copy":
            raw = a.get("node_paths") or a.get("paths") or []
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list) or not raw:
                errors.append(f"actions[{i}] network.clipboard_copy requires non-empty node_paths")

        if op == "network.clipboard_paste":
            pp = str(a.get("parent_path") or a.get("path") or "")
            if not pp.strip():
                errors.append(f"actions[{i}] network.clipboard_paste requires parent_path")
            elif not _is_abs_houdini_path(pp):
                warnings.append(f"actions[{i}] network.clipboard_paste parent_path not absolute path: {pp!r}")

        if op in ("geo.prim_intrinsics_bulk", "geo.volume_primitives_scan"):
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] {op} requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] {op} node_path not absolute path: {np!r}")

        if op == "geo.prim_bbox":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] geo.prim_bbox requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] geo.prim_bbox node_path not absolute path: {np!r}")

        if op == "vellum.graph_summary":
            pp = str(a.get("parent_path") or a.get("path") or "")
            if not pp.strip():
                errors.append(f"actions[{i}] vellum.graph_summary requires parent_path")
            elif not _is_abs_houdini_path(pp):
                warnings.append(f"actions[{i}] vellum.graph_summary parent_path not absolute path: {pp!r}")

        if op in ("obj.display_sop_path", "obj.render_sop_path", "obj.world_bounds", "obj.geo_summary"):
            opn = str(a.get("obj_path") or a.get("node_path") or "")
            if not opn.strip():
                errors.append(f"actions[{i}] {op} requires obj_path (or node_path)")
            elif not _is_abs_houdini_path(opn):
                warnings.append(f"actions[{i}] {op} path not absolute path: {opn!r}")

        if op == "obj.file_node_set_path":
            np = str(a.get("node_path") or "")
            fp = str(a.get("file_path") or a.get("path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] obj.file_node_set_path requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] obj.file_node_set_path node_path not absolute path: {np!r}")
            if not fp.strip():
                errors.append(f"actions[{i}] obj.file_node_set_path requires non-empty file_path")

        if op == "obj.camera_clip":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] obj.camera_clip requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] obj.camera_clip node_path not absolute path: {np!r}")
            if a.get("near") is None and a.get("far") is None:
                errors.append(f"actions[{i}] obj.camera_clip requires at least one of near, far")

        if op == "parm.keyframe_delete_frame":
            np = str(a.get("node_path") or "")
            pn = str(a.get("parm_name") or "")
            if not np.strip():
                errors.append(f"actions[{i}] parm.keyframe_delete_frame requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] parm.keyframe_delete_frame node_path not absolute path: {np!r}")
            if not pn.strip():
                errors.append(f"actions[{i}] parm.keyframe_delete_frame requires non-empty parm_name")
            if a.get("frame") is None:
                errors.append(f"actions[{i}] parm.keyframe_delete_frame requires frame")

        if op == "selection.set":
            raw = a.get("node_paths") or a.get("paths") or []
            if isinstance(raw, str):
                raw = [raw]
            if not isinstance(raw, list) or not raw:
                errors.append(f"actions[{i}] selection.set requires non-empty node_paths list")
            else:
                for p in raw:
                    ps = str(p or "").strip()
                    if ps and not _is_abs_houdini_path(ps):
                        warnings.append(f"actions[{i}] selection.set path not absolute: {ps!r}")

        if op == "timeline.offset_frame":
            if a.get("delta") is None and a.get("offset") is None:
                errors.append(f"actions[{i}] timeline.offset_frame requires delta (or offset)")

        if op == "playback.set":
            mode = str(a.get("mode") or a.get("state") or "").strip().lower()
            if not mode:
                errors.append(f"actions[{i}] playback.set requires mode (or state)")

        if op == "path.expand_string":
            ps = str(a.get("string") or a.get("path") or a.get("value") or "")
            if not ps.strip():
                errors.append(f"actions[{i}] path.expand_string requires string (or path / value)")

        if op == "path.file_exists":
            fp = str(a.get("file_path") or a.get("path") or "")
            if not fp.strip():
                errors.append(f"actions[{i}] path.file_exists requires file_path (or path)")

        if op == "cache.clear_all" or op == "cache.pdg_clear":
            pass

        if op == "top.workitems_scan":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] top.workitems_scan requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] top.workitems_scan node_path not absolute path: {np!r}")
            mi = a.get("max_items")
            if mi is not None:
                try:
                    if int(mi) < 1:
                        warnings.append(f"actions[{i}] top.workitems_scan max_items < 1; runtime will clamp to 1")
                    elif int(mi) > 200:
                        warnings.append(f"actions[{i}] top.workitems_scan max_items > 200; runtime will clamp to 200")
                except Exception:
                    errors.append(f"actions[{i}] top.workitems_scan max_items must be int-like")

        if op == "hda.ensure_file":
            fp = str(a.get("file_path") or a.get("path") or "")
            if not fp.strip():
                errors.append(f"actions[{i}] hda.ensure_file requires file_path (or path)")

        if op == "io.file_parms_guess":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] io.file_parms_guess requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] io.file_parms_guess node_path not absolute path: {np!r}")

        if op == "chop.parm_channel_state":
            np = str(a.get("node_path") or "")
            pn = str(a.get("parm_name") or "")
            if not np.strip():
                errors.append(f"actions[{i}] chop.parm_channel_state requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] chop.parm_channel_state node_path not absolute path: {np!r}")
            if not pn.strip():
                errors.append(f"actions[{i}] chop.parm_channel_state requires non-empty parm_name")

        if op == "lop.usd_layer_stack":
            np = str(a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] lop.usd_layer_stack requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] lop.usd_layer_stack node_path not absolute path: {np!r}")

        if op == "viewport.flipbook":
            ou = str(a.get("output_path") or a.get("path") or a.get("file_path") or "")
            if not ou.strip():
                errors.append(f"actions[{i}] viewport.flipbook requires output_path (or path / file_path)")

        if op == "validate.parm_range":
            np = str(a.get("node_path") or "")
            pn = str(a.get("parm_name") or "")
            if not np.strip():
                errors.append(f"actions[{i}] validate.parm_range requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] validate.parm_range node_path not absolute path: {np!r}")
            if not pn.strip():
                errors.append(f"actions[{i}] validate.parm_range requires non-empty parm_name")
            if a.get("value") is None:
                errors.append(f"actions[{i}] validate.parm_range requires value")

        if op == "session.snapshot":
            pass

        if op == "shelf.run_tool":
            tp = str(a.get("tool_path") or a.get("tool_name") or a.get("name") or "")
            if not tp.strip():
                errors.append(f"actions[{i}] shelf.run_tool requires tool_path (or tool_name / name)")

        if op == "node.preset_apply":
            np = str(a.get("node_path") or "")
            pn = str(a.get("preset_name") or a.get("name") or a.get("preset") or "")
            if not np.strip():
                errors.append(f"actions[{i}] node.preset_apply requires non-empty node_path")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] node.preset_apply node_path not absolute path: {np!r}")
            if not pn.strip():
                errors.append(f"actions[{i}] node.preset_apply requires preset_name (or name / preset)")

        if op in ("obj.xform_get", "obj.world_transform_get", "obj.local_transform_get"):
            np = str(a.get("obj_path") or a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] {op} requires obj_path (or node_path)")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] {op} path not absolute path: {np!r}")

        if op == "obj.xform_set":
            np = str(a.get("obj_path") or a.get("node_path") or "")
            if not np.strip():
                errors.append(f"actions[{i}] obj.xform_set requires obj_path (or node_path)")
            elif not _is_abs_houdini_path(np):
                warnings.append(f"actions[{i}] obj.xform_set path not absolute path: {np!r}")
            has_any = any(
                a.get(k) is not None for k in ("translate", "rotate", "scale", "t", "r", "s")
            )
            if not has_any:
                errors.append(f"actions[{i}] obj.xform_set requires at least one of translate, rotate, scale (or t, r, s)")

    result: dict[str, Any] = {"ok": not errors, "warnings": warnings, "errors": errors}
    result["suggestions"] = suggestions_for_preflight_errors(errors) if errors else []
    return result
