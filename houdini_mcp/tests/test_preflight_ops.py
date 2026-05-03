from __future__ import annotations

from planner.adhoc_plan import build_adhoc_plan
from planner.preflight import preflight_plan


def test_preflight_new_node_ops() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "node.delete", "node_path": "/obj/geo1/box1"},
            {"op": "node.rename", "node_path": "/obj/geo1/box1", "new_name": "box2"},
            {"op": "node.disconnect", "dst": "/obj/geo1/merge1"},
            {"op": "node.info", "node_path": "/obj/geo1"},
        ]
    )
    pf = preflight_plan(plan)
    assert pf["ok"]


def test_preflight_parm_expression() -> None:
    plan = build_adhoc_plan(
        [{"op": "parm.set_expression", "node_path": "/obj/g1", "parm_name": "tx", "expression": "0"}]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_hip_load_requires_path() -> None:
    plan = build_adhoc_plan([{"op": "hip.load", "file_path": ""}])
    pf = preflight_plan(plan)
    assert not pf["ok"]
    assert any("hip.load" in e for e in pf["errors"])


def test_preflight_selection_set() -> None:
    plan = build_adhoc_plan([{"op": "selection.set", "node_paths": ["/obj/geo1"]}])
    assert preflight_plan(plan)["ok"]


def test_preflight_graph_glob_and_geo_info() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "graph.glob", "parent_path": "/obj/geo1", "pattern": "box*"},
            {"op": "graph.layout_children", "parent_path": "/obj/geo1"},
            {"op": "geo.info", "node_path": "/obj/geo1/box1"},
            {"op": "parm.exists", "node_path": "/obj/g1", "parm_name": "tx"},
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_extended_surface_ops() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "node.list_inputs", "node_path": "/obj/geo1/merge1"},
            {"op": "node.list_outputs", "node_path": "/obj/geo1/box1"},
            {"op": "geo.bounding_box", "node_path": "/obj/geo1/box1"},
            {"op": "geo.point_count", "node_path": "/obj/geo1/box1"},
            {"op": "geo.interpolate_p", "node_path": "/obj/geo1/box1", "prim_index": 0},
            {"op": "attrib.summary", "node_path": "/obj/geo1/box1", "name": "P", "scope": "point"},
            {"op": "rop.evaluate_path", "node_path": "/out/mantra1"},
            {"op": "exec.render_write", "node_path": "/out/karma1"},
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_node_change_type_requires_new_type() -> None:
    plan = build_adhoc_plan([{"op": "node.change_type", "node_path": "/obj/geo1/box1", "node_type": ""}])
    pf = preflight_plan(plan)
    assert not pf["ok"]
    assert any("node.change_type" in e for e in pf["errors"])


def test_preflight_p38_batch_ops() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "network.set_current_node", "node_path": "/obj/geo1/box1"},
            {"op": "node.reparent", "node_path": "/obj/geo1/box1", "parent_path": "/obj/geo1"},
            {"op": "parm.press_button", "node_path": "/obj/geo1/null1", "parm_name": "somebtn"},
            {"op": "parm.multiparm_resize", "node_path": "/obj/geo1/merge1", "folder_parm": "input", "count": 3},
            {"op": "parm.clear_keyframes", "node_path": "/obj/geo1/xform1", "parm_name": "tx"},
            {"op": "geo.save_to_file", "node_path": "/obj/geo1/box1", "file_path": "C:/tmp/out.bgeo.sc"},
            {"op": "geo.primitive_count", "node_path": "/obj/geo1/box1"},
            {"op": "geo.vertex_count", "node_path": "/obj/geo1/box1"},
            {"op": "attrib.exists", "node_path": "/obj/geo1/box1", "name": "P"},
            {"op": "hip.session_info"},
            {"op": "timeline.get_state"},
            {"op": "viewport.frame_selected"},
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_geo_save_requires_file_path() -> None:
    plan = build_adhoc_plan([{"op": "geo.save_to_file", "node_path": "/obj/geo1/box1", "file_path": ""}])
    pf = preflight_plan(plan)
    assert not pf["ok"]
    assert any("geo.save_to_file" in e for e in pf["errors"])


def test_preflight_p45_obj_priority() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "obj.display_sop_path", "obj_path": "/obj/geo1"},
            {"op": "obj.render_sop_path", "obj_path": "/obj/geo1"},
            {"op": "obj.world_bounds", "obj_path": "/obj/geo1"},
            {"op": "obj.geo_summary", "obj_path": "/obj/geo1"},
            {"op": "obj.file_node_set_path", "node_path": "/obj/geo1/file1", "file_path": "C:/mesh.abc"},
            {"op": "obj.camera_clip", "node_path": "/obj/cam1", "near": 0.1, "far": 1000},
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_p44_volume_vdb_vellum() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "geo.prim_intrinsics_bulk", "node_path": "/obj/geo1/vdb1", "volume_family_only": True, "keys_only": True},
            {"op": "geo.volume_primitives_scan", "node_path": "/obj/geo1/vdb1"},
            {"op": "geo.prim_bbox", "node_path": "/obj/geo1/vdb1", "prim_index": 0},
            {"op": "vellum.graph_summary", "parent_path": "/obj/sim1"},
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_p43_geo_network() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "geo.groups_list", "node_path": "/obj/geo1/box1"},
            {"op": "geo.group_count", "node_path": "/obj/geo1/box1", "group_name": "orig"},
            {"op": "geo.sample_primitives", "node_path": "/obj/geo1/box1", "attributes": ["Cd"], "max_primitives": 8},
            {"op": "geo.primitive_type_breakdown", "node_path": "/obj/geo1/box1"},
            {"op": "geo.has_packed_primitives", "node_path": "/obj/geo1/box1"},
            {"op": "geo.detail_attrib_get", "node_path": "/obj/geo1/box1", "name": "mydetail"},
            {"op": "network.clipboard_copy", "node_paths": ["/obj/geo1/box1"]},
            {"op": "network.clipboard_paste", "parent_path": "/obj/geo1"},
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_p42_ta_helpers() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "node.spare_parm_add", "node_path": "/obj/geo1/wrangle1", "parm_name": "amp", "parm_type": "float", "default": 1.5},
            {"op": "node.spare_parm_remove", "node_path": "/obj/geo1/wrangle1", "parm_name": "amp"},
            {"op": "node.diagnostics", "node_path": "/obj/geo1/wrangle1", "force_cook": False},
            {"op": "geo.sample_points", "node_path": "/obj/geo1/box1", "attributes": ["P", "Cd"], "max_points": 4},
            {"op": "sop.wrangle_recompile", "node_path": "/obj/geo1/wrangle1"},
            {"op": "sop.camphor_tree_build", "parent_geo_path": "/obj/geo1"},
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_p41_obj_wrangle() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "sop.wrangle_create", "parent_path": "/obj/geo1", "node_name": "attribwrangle1"},
            {
                "op": "sop.vex_snippet_set",
                "node_path": "/obj/geo1/attribwrangle1",
                "snippet": "@Cd = {1,0,0};",
            },
            {"op": "sop.wrangle_run_over_set", "node_path": "/obj/geo1/attribwrangle1", "run_over": "point"},
            {"op": "sop.wrangle_group_set", "node_path": "/obj/geo1/attribwrangle1", "group": "piece0"},
            {"op": "sop.vex_snippet_get", "node_path": "/obj/geo1/attribwrangle1"},
            {"op": "geo.list_attribs", "node_path": "/obj/geo1/box1", "scope": "point"},
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_p40_solaris_mtlx() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "solaris.usd_file_set", "node_path": "/stage/import1", "file_path": "C:/assets/set.usd"},
            {"op": "solaris.karma_render_set", "node_path": "/out/karma1", "picture": "$HIP/out/$OS.jpg"},
            {"op": "mtlx.texture_file_set", "node_path": "/stage/mtlx_network1/tex1", "file_path": "C:/tex/albedo.png"},
            {"op": "mtlx.standard_surface_set", "node_path": "/stage/mtlx_network1/surface1", "roughness": 0.4},
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_p39_material_rop_keyframe() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "material.assign_object", "obj_path": "/obj/geo1", "material_path": "/mat/principledshader1"},
            {"op": "material.clear_object", "obj_path": "/obj/geo1"},
            {"op": "lop.stage_summary", "node_path": "/stage/usd_rop1"},
            {"op": "subnet.collapse", "node_paths": ["/obj/geo1/box1", "/obj/geo1/xform1"]},
            {"op": "hda.definition_save", "node_path": "/obj/geo1/myhda1", "file_path": "C:/tmp/a.hdal"},
            {"op": "hda.install_file", "file_path": "C:/tmp/a.hdal"},
            {"op": "parm.keyframe_set", "node_path": "/obj/geo1/xform1", "parm_name": "tx", "frame": 1, "value": 0},
            {"op": "parm.keyframe_list", "node_path": "/obj/geo1/xform1", "parm_name": "tx"},
            {"op": "parm.keyframe_delete_frame", "node_path": "/obj/geo1/xform1", "parm_name": "tx", "frame": 1},
            {"op": "exec.node_execute", "node_path": "/out/alembic1"},
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_hip_merge_requires_path() -> None:
    plan = build_adhoc_plan([{"op": "hip.merge", "file_path": ""}])
    pf = preflight_plan(plan)
    assert not pf["ok"]
    assert any("hip.merge" in e for e in pf["errors"])


def test_preflight_p46_session_shelf_xform() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "session.snapshot"},
            {"op": "shelf.run_tool", "tool_path": "obj/geo"},
            {"op": "node.preset_apply", "node_path": "/obj/geo1/box1", "preset_name": "default"},
            {"op": "obj.xform_get", "node_path": "/obj/geo1"},
            {"op": "obj.xform_set", "node_path": "/obj/geo1", "translate": [1, 2, 3]},
            {"op": "obj.world_transform_get", "obj_path": "/obj/geo1"},
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_p48_new_ops() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "geo.topology_summary", "node_path": "/obj/geo1/box1"},
            {"op": "cache.pdg_clear"},
            {"op": "top.workitems_scan", "node_path": "/tasks/topnet1"},
            {"op": "hda.ensure_file", "file_path": "C:/tmp/x.hda"},
            {"op": "io.file_parms_guess", "node_path": "/obj/geo1/alembic1"},
            {"op": "chop.parm_channel_state", "node_path": "/obj/geo1/t1", "parm_name": "tx"},
            {"op": "lop.usd_layer_stack", "node_path": "/stage/rop1"},
            {"op": "viewport.flipbook", "output_path": "$HIP/out/fb.$F4.jpg"},
            {"op": "validate.parm_range", "node_path": "/obj/geo1/box1", "parm_name": "size", "value": 1.0},
            {"op": "session.snapshot", "include_desktop": True},
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_p47_timeline_path_geo_deps() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "timeline.offset_frame", "delta": 1},
            {"op": "playback.set", "mode": "stop"},
            {"op": "path.expand_string", "string": "$HIP/foo"},
            {"op": "path.file_exists", "file_path": "$HIP/houdini.env"},
            {"op": "cache.clear_all"},
            {"op": "geo.is_empty", "node_path": "/obj/geo1/box1"},
            {"op": "node.references_list", "node_path": "/obj/geo1/box1"},
            {"op": "node.dependents_list", "node_path": "/obj/geo1/box1"},
            {"op": "obj.local_transform_get", "node_path": "/obj/geo1"},
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_suggestions_when_missing_node_path() -> None:
    plan = build_adhoc_plan([{"op": "parm.set", "node_path": "", "parm_name": "tx", "value": 0}])
    pf = preflight_plan(plan)
    assert not pf["ok"]
    assert pf.get("suggestions")
    assert any("snapshot" in s.lower() or "glob" in s.lower() for s in pf["suggestions"])


def test_preflight_exec_python_requires_code() -> None:
    plan = build_adhoc_plan([{"op": "exec.python", "code": ""}])
    pf = preflight_plan(plan)
    assert not pf["ok"]
    assert any("exec.python" in e for e in pf["errors"])


def test_preflight_exec_python_scene_summary_viewport() -> None:
    plan = build_adhoc_plan(
        [
            {"op": "exec.python", "code": "print('mcp')"},
            {"op": "scene.summary"},
            {"op": "viewport.snapshot", "output_path": "$HIP/mcp_vp.png"},
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_mcp_ctrl_null_setup() -> None:
    plan = build_adhoc_plan(
        [
            {
                "op": "mcp.ctrl_null_setup",
                "parent_path": "/obj/geo1",
                "null_name": "mcp_ctrl",
                "bindings": [
                    {"spare_name": "npts", "ref_node": "/obj/geo1/scatter1", "ref_parm": "npoints"},
                ],
                "input_from": "/obj/geo1/box1",
            }
        ]
    )
    assert preflight_plan(plan)["ok"]


def test_preflight_mcp_ctrl_null_setup_requires_parent() -> None:
    plan = build_adhoc_plan([{"op": "mcp.ctrl_null_setup", "parent_path": "", "bindings": []}])
    pf = preflight_plan(plan)
    assert not pf["ok"]
    assert any("parent_path" in e for e in pf["errors"])


def test_preflight_scene_summary_rich_limits_warn() -> None:
    plan = build_adhoc_plan(
        [
            {
                "op": "scene.summary",
                "max_selected_detail_nodes": 99,
                "max_parms_per_node": 500,
            }
        ]
    )
    pf = preflight_plan(plan)
    assert pf["ok"]
    assert pf["warnings"]
