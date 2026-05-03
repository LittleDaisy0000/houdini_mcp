"""Static catalog of batch ops implemented in ``houdini_receiver_template.py``.

Agents should call the ``houdini_ops_catalog`` MCP tool for an up-to-date list.
"""

from __future__ import annotations

from typing import Any

# fmt: off
_OPS: list[dict[str, Any]] = [
    {
        "op": "node.create",
        "summary": "Create a node under an existing parent. Grid SOP: receiver auto-fixes 1×1 polygon rows/cols to 2×2 (degenerate for Copy to Points).",
        "fields": {
            "parent_path": "Absolute Houdini path to parent (e.g. /obj/geo1).",
            "node_type": "Houdini node type name (e.g. box, merge).",
            "node_name": "Optional exact name; fails if that name already exists under parent.",
            "auto_layout": "Optional bool, default true; call parent.layoutChildren() after create.",
        },
    },
    {
        "op": "node.connect",
        "summary": "Wire one node's output into another's input.",
        "fields": {
            "src": "Absolute path to source node.",
            "dst": "Absolute path to destination node.",
            "src_output": "Output index (default 0).",
            "dst_input": "Input index (default 0).",
        },
    },
    {
        "op": "node.disconnect",
        "summary": "Clear a node's input wire (set input to None).",
        "fields": {"dst": "Absolute path to destination node.", "dst_input": "Input index (default 0)."},
    },
    {
        "op": "node.delete",
        "summary": "Destroy a node (hou.Node.destroy).",
        "fields": {"node_path": "Absolute node path."},
    },
    {
        "op": "node.rename",
        "summary": "Rename a node; optional unique_name (default true).",
        "fields": {"node_path": "Absolute node path.", "new_name": "New base name.", "unique_name": "Bool, default true."},
    },
    {
        "op": "node.duplicate",
        "summary": "Duplicate a node in the same parent (hou.copyNodes or copyTo).",
        "fields": {
            "node_path": "Source absolute path.",
            "new_name": "Optional name for the copy.",
            "auto_layout": "Optional bool, default true; call parent.layoutChildren() after duplicate.",
        },
    },
    {
        "op": "node.set_position",
        "summary": "Network editor position (x, y).",
        "fields": {"node_path": "Absolute node path.", "x": "Float.", "y": "Float."},
    },
    {
        "op": "node.set_comment",
        "summary": "Set the node comment / badge text.",
        "fields": {"node_path": "Absolute node path.", "comment": "String."},
    },
    {
        "op": "node.set_color",
        "summary": "Set node color (RGB 0–1).",
        "fields": {"node_path": "Absolute node path.", "r": "Float.", "g": "Float.", "b": "Float."},
    },
    {
        "op": "node.info",
        "summary": "Read-only metadata: type, category, flags, bypass/lock, cook errors (subset).",
        "fields": {"node_path": "Absolute node path."},
    },
    {
        "op": "node.references_list",
        "summary": "Read-only: cooking/reference dependencies via hou.Node.references() (not the same as input wires; use node.list_inputs for links).",
        "fields": {"node_path": "Absolute node path."},
    },
    {
        "op": "node.dependents_list",
        "summary": "Read-only: nodes that require this one to cook (hou.Node.dependents(), when available).",
        "fields": {"node_path": "Absolute node path."},
    },
    {
        "op": "node.bypass",
        "summary": "Enable or disable bypass on a node.",
        "fields": {"node_path": "Absolute node path.", "enabled": "Bool (default true); alias key: bypass."},
    },
    {
        "op": "node.lock",
        "summary": "Lock or unlock a node in the network editor.",
        "fields": {"node_path": "Absolute node path.", "locked": "Bool, default true."},
    },
    {
        "op": "node.set_selectable",
        "summary": "Whether the node can be selected in the network editor.",
        "fields": {"node_path": "Absolute node path.", "selectable": "Bool, default true."},
    },
    {
        "op": "node.set_flag",
        "summary": "Set display / render / template flags on a node.",
        "fields": {
            "node_path": "Absolute node path.",
            "display": "Optional bool.",
            "render": "Optional bool.",
            "template": "Optional bool.",
        },
    },
    {
        "op": "node.list_inputs",
        "summary": "List input slots: index, connected flag, and optional source node path.",
        "fields": {"node_path": "Absolute node path."},
    },
    {
        "op": "node.list_outputs",
        "summary": "List outgoing wires: destination input index and destination node path.",
        "fields": {"node_path": "Absolute node path."},
    },
    {
        "op": "node.change_type",
        "summary": "Replace a node with another type (hou.Node.changeNodeType).",
        "fields": {
            "node_path": "Absolute node path.",
            "node_type": "Target type name (alias: type_name).",
            "force": "Optional bool for force_change_op_type (default false).",
        },
    },
    {
        "op": "node.match_definition",
        "summary": "Sync a node instance to a saved digital asset definition (hou.Node.matchDefinition).",
        "fields": {"node_path": "Absolute node path.", "definition": "Definition name (alias: type_name)."},
    },
    {
        "op": "node.reparent",
        "summary": "Move a node under another parent (hou.moveNodesTo or setParent fallback).",
        "fields": {
            "node_path": "Absolute path of node to move.",
            "parent_path": "Absolute path of new parent network (alias: new_parent_path).",
        },
    },
    {
        "op": "material.assign_object",
        "summary": "Assign a material network node to an object-level container (setMaterial when available; fallback to shop_materialpath parm).",
        "fields": {
            "obj_path": "Object node path (aliases: object_path, node_path).",
            "material_path": "Material / shader node path (aliases: mat_path).",
        },
    },
    {
        "op": "material.clear_object",
        "summary": "Clear OBJ-level material assignment (setMaterial(None) or fallback clear shop_materialpath).",
        "fields": {"obj_path": "Object node path (aliases: object_path, node_path)."},
    },
    {
        "op": "subnet.collapse",
        "summary": "Collapse listed nodes into a new subnet (hou.collapseIntoSubnet); optional subnet_name renames the subnet.",
        "fields": {
            "node_paths": "Non-empty list of absolute paths (alias: paths).",
            "subnet_name": "Optional base name for the new subnet node.",
        },
    },
    {
        "op": "hda.definition_save",
        "summary": "Save a digital asset definition from an instance to .hdalc/.otl on disk (definition.save).",
        "fields": {"node_path": "Instance path.", "file_path": "Destination path (alias: path)."},
    },
    {
        "op": "hda.install_file",
        "summary": "Install HDAs from disk into this session (hou.hda.installFile).",
        "fields": {"file_path": "Absolute path to .hdalc/.otl (alias: path)."},
    },
    {
        "op": "node.setup_vellum_ctrl",
        "summary": "Build CTRL spare parms + expressions for vellum cloth/wind (recipe helper).",
        "fields": {
            "ctrl_node_path": "Null or control node path.",
            "pin_node_path": "Pin / constraint target path.",
            "solver_node_path": "Vellum solver path.",
            "constraints_node_path": "Vellum constraints path.",
        },
    },
    {
        "op": "node.setup_vellum_collisions",
        "summary": "Optional collision wiring for vellum (ground + object_merge slots); recipe helper.",
        "fields": {
            "geo_path": "Geometry container path.",
            "solver_path": "Vellum solver path.",
            "ctrl_node_path": "Optional control null for collider parms.",
            "use_ground_plane": "0/1.",
            "static_collider_path": "Single collider SOP path.",
            "static_collider_paths": "Joined with ||| for multiple.",
            "collider_import_slots": "Number of import slots (clamped).",
            "ground_offset_y": "etc. — see receiver for full set.",
        },
    },
    {
        "op": "parm.get",
        "summary": "Read a parameter's evaluated value.",
        "fields": {"node_path": "Absolute node path.", "parm_name": "Parameter name."},
    },
    {
        "op": "parm.get_raw",
        "summary": "Read raw / unexpanded parameter string (falls back to rawValue).",
        "fields": {"node_path": "Absolute node path.", "parm_name": "Parameter name."},
    },
    {
        "op": "parm.exists",
        "summary": "Return whether a parameter exists on the node.",
        "fields": {"node_path": "Absolute node path.", "parm_name": "Parameter name."},
    },
    {
        "op": "parm.set",
        "summary": "Set one parameter (with aliases for resample/clip/vellum).",
        "fields": {"node_path": "Absolute node path.", "parm_name": "Parameter name.", "value": "Scalar or tuple as needed."},
    },
    {
        "op": "parm.set_expression",
        "summary": "Set a parameter expression; language hscript (default) or python.",
        "fields": {
            "node_path": "Absolute node path.",
            "parm_name": "Parameter name.",
            "expression": "Expression string (alias key: expr).",
            "language": "hscript or python.",
        },
    },
    {
        "op": "parm.set_batch",
        "summary": "Set many parms on one node; missing keys are skipped with warnings.",
        "fields": {"node_path": "Absolute node path.", "params": "Dict of parm_name → value."},
    },
    {
        "op": "parm.revert_defaults",
        "summary": "Revert one parm to defaults, or all parms on the node if parm_name omitted.",
        "fields": {"node_path": "Absolute node path.", "parm_name": "Optional; omit to revert all."},
    },
    {
        "op": "parm.list",
        "summary": "List parameter names: merges hou.Parm instances + node type parmTemplateGroup (covers Merge multiparms when .parms() is empty).",
        "fields": {"node_path": "Absolute node path.", "prefix": "Optional string.", "max_count": "Int, default 500, max 5000."},
    },
    {
        "op": "parm.press_button",
        "summary": "Press a button parm (hou.Parm.pressButton).",
        "fields": {"node_path": "Absolute node path.", "parm_name": "Button parameter name."},
    },
    {
        "op": "parm.multiparm_resize",
        "summary": "Set multiparm / folder instance count (folder parm .set(n)).",
        "fields": {
            "node_path": "Absolute node path.",
            "folder_parm": "Multiparm folder parm name (alias: parm_name).",
            "count": "Target instance count (alias: num_instances).",
        },
    },
    {
        "op": "parm.clear_keyframes",
        "summary": "Delete all keyframes on a parm or parmTuple if supported.",
        "fields": {"node_path": "Absolute node path.", "parm_name": "Parameter name."},
    },
    {
        "op": "parm.keyframe_set",
        "summary": "Set a keyframe at a frame on a scalar parm (or one component of a tuple via component index).",
        "fields": {
            "node_path": "Absolute node path.",
            "parm_name": "Parameter name.",
            "frame": "Frame number.",
            "value": "Numeric value.",
            "component": "Optional int index when parm_name refers to a vector/tuple.",
        },
    },
    {
        "op": "parm.keyframe_list",
        "summary": "List keyframes on a parm (frame + value), truncated by max_keys.",
        "fields": {
            "node_path": "Absolute node path.",
            "parm_name": "Parameter name.",
            "max_keys": "Max rows (default 256, cap 4096).",
            "component": "Optional int index for tuple parms.",
        },
    },
    {
        "op": "parm.keyframe_delete_frame",
        "summary": "Delete the keyframe at a given frame on a scalar parm (or tuple component).",
        "fields": {
            "node_path": "Absolute node path.",
            "parm_name": "Parameter name.",
            "frame": "Frame number.",
            "component": "Optional int index for tuple parms.",
        },
    },
    {
        "op": "graph.exists",
        "summary": "Return whether a node path exists.",
        "fields": {"node_path": "Absolute node path."},
    },
    {
        "op": "graph.list_children",
        "summary": "List child node names under a network.",
        "fields": {"path": "Absolute path to parent node."},
    },
    {
        "op": "graph.layout_children",
        "summary": "Auto-layout child nodes in a network.",
        "fields": {"parent_path": "Absolute parent network path (alias: path)."},
    },
    {
        "op": "graph.glob",
        "summary": "Glob-match descendant nodes from a parent (hou.Node.glob or recursiveGlob).",
        "fields": {
            "parent_path": "Absolute path to parent network (alias: path).",
            "pattern": "Glob pattern, default *.",
            "recursive": "Bool; use recursiveGlob when true and available.",
        },
    },
    {
        "op": "network.set_current_node",
        "summary": "Focus the network editor on this node (setCurrent).",
        "fields": {"node_path": "Absolute node path."},
    },
    {
        "op": "selection.clear",
        "summary": "Clear the current node selection.",
        "fields": {},
    },
    {
        "op": "selection.set",
        "summary": "Replace selection with the given node paths (missing nodes warned).",
        "fields": {"node_paths": "List of absolute paths (alias: paths)."},
    },
    {
        "op": "viewport.frame_selected",
        "summary": "Frame contents in the first SceneViewer viewport (hou.ui desktop; may fail in headless hython).",
        "fields": {},
    },
    {
        "op": "timeline.set_range",
        "summary": "Set playback range on the playbar.",
        "fields": {"start": "Frame (float).", "end": "Frame (float)."},
    },
    {
        "op": "timeline.set_frame",
        "summary": "Set the global frame (hou.setFrame).",
        "fields": {"frame": "Frame number (float)."},
    },
    {
        "op": "timeline.set_fps",
        "summary": "Set scene frames-per-second (hou.setFps).",
        "fields": {"fps": "Float, default 24."},
    },
    {
        "op": "timeline.get_state",
        "summary": "Read-only: current frame, fps, playback range from the playbar.",
        "fields": {},
    },
    {
        "op": "timeline.offset_frame",
        "summary": "Move the global frame by delta (hou.setFrame(frame + delta)); alias key: offset.",
        "fields": {"delta": "Frame delta (float/int); alias: offset."},
    },
    {
        "op": "playback.set",
        "summary": "Playbar playback mode: play | pause | stop. Dispatches via hou.ui.executeDeferred so the UI timeline updates (MCP runs on a background thread).",
        "fields": {
            "mode": "play | pause | stop (alias: state).",
            "dispatch": "Response field: ui_thread (normal) or direct (fallback if deferred failed).",
        },
    },
    {
        "op": "exec.cook",
        "summary": "Force-cook a node.",
        "fields": {"node_path": "Absolute node path."},
    },
    {
        "op": "geo.info",
        "summary": "Read-only SOP geometry stats (points/prims/bbox); optional force_cook (default true).",
        "fields": {"node_path": "Absolute path to a node with geometry().", "force_cook": "Bool, default true."},
    },
    {
        "op": "geo.is_empty",
        "summary": "Lightweight guard: no geometry or zero points (point-count heuristic; use geo.topology_summary for prims+points).",
        "fields": {"node_path": "Absolute path.", "force_cook": "Bool, default true."},
    },
    {
        "op": "geo.topology_summary",
        "summary": "Point/prim/vertex counts, empty flag, and degenerate hint (0 points but prims>0).",
        "fields": {"node_path": "Absolute path.", "force_cook": "Bool, default true."},
    },
    {
        "op": "geo.bounding_box",
        "summary": "Axis-aligned bbox (min/max/center/size) from cooked geometry.",
        "fields": {"node_path": "Absolute path to a node with geometry().", "force_cook": "Bool, default true."},
    },
    {
        "op": "geo.point_count",
        "summary": "Number of points in cooked geometry.",
        "fields": {"node_path": "Absolute path to a node with geometry().", "force_cook": "Bool, default true."},
    },
    {
        "op": "geo.primitive_count",
        "summary": "Number of primitives in cooked geometry.",
        "fields": {"node_path": "Absolute path to a node with geometry().", "force_cook": "Bool, default true."},
    },
    {
        "op": "geo.vertex_count",
        "summary": "Number of vertices in cooked geometry.",
        "fields": {"node_path": "Absolute path to a node with geometry().", "force_cook": "Bool, default true."},
    },
    {
        "op": "geo.save_to_file",
        "summary": "Export cooked geometry to disk (Geometry.saveToFile, fallback writeToFile).",
        "fields": {
            "node_path": "Absolute path to a node with geometry().",
            "file_path": "Output path (alias: path).",
            "force_cook": "Bool, default true.",
            "mkdirs": "Create parent dirs (default true).",
        },
    },
    {
        "op": "geo.interpolate_p",
        "summary": "Sample P on a primitive interior via (u, v) using Prim.positionAtInterior.",
        "fields": {
            "node_path": "Absolute path to a node with geometry().",
            "prim_index": "Primitive index (alias: primitive).",
            "u": "Float barycentric-ish coordinate (see Houdini docs).",
            "v": "Float.",
            "force_cook": "Bool, default true.",
        },
    },
    {
        "op": "attrib.summary",
        "summary": "Peek attribute values on point/prim/vertex scope (first samples only).",
        "fields": {
            "node_path": "Absolute path to a node with geometry().",
            "name": "Attribute name (alias: attrib_name).",
            "scope": "point | prim | vertex (aliases: attrib_type, pt/prim/vtx).",
            "max_elements": "How many elements to sample from the start (1–64, default 8).",
            "force_cook": "Bool, default true.",
        },
    },
    {
        "op": "attrib.exists",
        "summary": "Check whether a geometry attribute exists (point/prim/vertex/detail).",
        "fields": {
            "node_path": "Absolute path to a node with geometry().",
            "name": "Attribute name (alias: attrib_name).",
            "scope": "point | prim | vertex | detail (aliases: global, geo).",
            "force_cook": "Bool, default true.",
        },
    },
    {
        "op": "sop.vex_snippet_set",
        "summary": "Set VEX / snippet string on a wrangle-family SOP (attribwrangle, volumewrangle, …).",
        "fields": {
            "node_path": "SOP path (e.g. /obj/geo1/attribwrangle1).",
            "code": "Full VEX (aliases: snippet, vex, source).",
            "parm_name": "Optional: force snippet parm token if auto-detect fails.",
        },
    },
    {
        "op": "sop.vex_snippet_get",
        "summary": "Read back raw / unexpanded VEX from a wrangle (for verification or diffs).",
        "fields": {"node_path": "SOP path.", "parm_name": "Optional if non-default snippet parm."},
    },
    {
        "op": "sop.wrangle_run_over_set",
        "summary": "Set run-over / class menu (points, vertices, primitives, detail, …) by keyword matching menu labels.",
        "fields": {
            "node_path": "Wrangle SOP path.",
            "run_over": "Short keyword, e.g. point, prim, vertex, detail, volume (aliases: run_class, class, domain).",
        },
    },
    {
        "op": "sop.wrangle_group_set",
        "summary": "Set wrangle Group mask; also sets Group Type menu when needed (defaults to Points for @ptnum patterns). Empty string clears group.",
        "fields": {
            "node_path": "Wrangle SOP path.",
            "group": "Group pattern (aliases: group_mask, pattern).",
            "group_type": "Optional keyword for Group Type menu (aliases: bind_type), e.g. points / primitives / guess.",
        },
    },
    {
        "op": "sop.wrangle_create",
        "summary": "Create a wrangle under a SOP network (default /obj/.../geo1), then optionally set code, run_over, group.",
        "fields": {
            "parent_path": "Parent network, usually /obj/<geo>.",
            "node_type": "Default attribwrangle; falls back across common types.",
            "node_name": "Optional.",
            "code": "Optional initial VEX (aliases: snippet, vex).",
            "run_over": "Optional.",
            "group": "Optional group mask.",
            "auto_layout": "Bool, default true.",
        },
    },
    {
        "op": "geo.list_attribs",
        "summary": "List geometry attribute names by scope (or all scopes); useful before/after wrangles.",
        "fields": {
            "node_path": "Display SOP or node with geometry().",
            "scope": "all | point | primitive | vertex | detail (default all).",
            "force_cook": "Bool, default true.",
        },
    },
    {
        "op": "mcp.ctrl_null_setup",
        "summary": "Create a Null (default name mcp_ctrl) under a GEO SOP network and add spare parms that ch() reference parms on other SOPs — use after an effect to expose tuning knobs.",
        "fields": {
            "parent_path": "SOP network, e.g. /obj/geo1.",
            "null_name": "Null node name; default mcp_ctrl.",
            "bindings": "List of {spare_name, ref_node, ref_parm, label?}; scalar or tuple parms on ref_node.",
            "input_from": "Optional SOP path to wire into the new null's input 0.",
            "set_display_flag": "Bool; if true set display flag on the new null (often leave false).",
            "auto_layout": "Bool, default true.",
            "color": "Optional [r,g,b] 0..1 for null color.",
        },
    },
    {
        "op": "node.spare_parm_add",
        "summary": "Add a spare parameter to a node (for wrangle ch() / driver controls).",
        "fields": {
            "node_path": "Target node (often a wrangle).",
            "parm_name": "Internal name (alias: name).",
            "parm_type": "float | float3 | int | toggle | string | rgb (aliases: type).",
            "label": "UI label; defaults to parm_name.",
            "default": "Default value (alias: default_value).",
        },
    },
    {
        "op": "node.spare_parm_remove",
        "summary": "Remove a spare parameter from a node by name.",
        "fields": {"node_path": "Target node.", "parm_name": "Spare token to remove (alias: name)."},
    },
    {
        "op": "node.diagnostics",
        "summary": "After optional cook: collect node errors/warnings; optional include_children for one-level SOP children.",
        "fields": {
            "node_path": "Node to query.",
            "force_cook": "Bool, default true.",
            "include_children": "Bool, default false; append child cook errors (one level).",
        },
    },
    {
        "op": "geo.sample_points",
        "summary": "Sample first N points with a list of point attribute values (JSON-friendly; default attrib P).",
        "fields": {
            "node_path": "SOP with geometry().",
            "attributes": "List of attrib names, or a single space/comma string (default [P]).",
            "max_points": "1–4096, default 32.",
            "force_cook": "Bool, default true.",
        },
    },
    {
        "op": "sop.wrangle_recompile",
        "summary": "Press compile/reload Button parms when present; else cook(force=True) as fallback (Houdini 20+ often has no compile button).",
        "fields": {"node_path": "Wrangle or similar SOP path."},
    },
    {
        "op": "sop.camphor_tree_build",
        "summary": "Low-poly stylized broad-crown tree (樟树-like): subnet under a GEO with spare parms gen_depth, branch_angle, trunk_scale, poly_keep; L-system + polyreduce + OUT.",
        "fields": {
            "parent_geo_path": "Geometry OBJ path (e.g. /obj/geo1); alias geo_path.",
            "subnet_name": "Subnet node name, default camphor_tree_ctrl.",
            "replace_existing": "Bool, default true; destroy existing subnet of same name.",
            "auto_layout": "Bool, default true; layoutChildren on GEO and subnet.",
        },
    },
    {
        "op": "geo.groups_list",
        "summary": "List point / primitive / (optional) edge group names on cooked geometry.",
        "fields": {
            "node_path": "SOP with geometry().",
            "include_edge_groups": "Bool, default false.",
            "force_cook": "Bool, default true.",
        },
    },
    {
        "op": "geo.group_count",
        "summary": "Count elements in a named point / primitive / edge group (glob pattern @group).",
        "fields": {
            "node_path": "SOP with geometry().",
            "group_name": "Group name (alias: name).",
            "scope": "point | primitive | edge (default point).",
            "force_cook": "Bool, default true.",
        },
    },
    {
        "op": "geo.sample_primitives",
        "summary": "Sample first N primitives with prim attribute values + optional prim_type string.",
        "fields": {
            "node_path": "SOP with geometry().",
            "attributes": "Prim attrib names (list or string); empty = primnum + prim_type only.",
            "max_primitives": "1–4096 (aliases: max_points).",
            "include_prim_type": "Bool, default true.",
            "force_cook": "Bool, default true.",
        },
    },
    {
        "op": "geo.primitive_type_breakdown",
        "summary": "Histogram of hou.Prim.type() names (polygon vs packed vs …).",
        "fields": {"node_path": "SOP with geometry().", "force_cook": "Bool, default true."},
    },
    {
        "op": "geo.has_packed_primitives",
        "summary": "True if any primitive looks packed (primType or name/heuristic).",
        "fields": {"node_path": "SOP with geometry().", "force_cook": "Bool, default true."},
    },
    {
        "op": "geo.detail_attrib_get",
        "summary": "Read one global/detail attribute value by name (serialize vectors to lists).",
        "fields": {"node_path": "SOP with geometry().", "name": "Detail attrib (alias: attrib_name).", "force_cook": "Bool, default true."},
    },
    {
        "op": "network.clipboard_copy",
        "summary": "Copy nodes to the Houdini clipboard (hou.copyNodesToClipboard).",
        "fields": {"node_paths": "Non-empty list of absolute paths (alias: paths)."},
    },
    {
        "op": "network.clipboard_paste",
        "summary": "Paste clipboard nodes under a parent network (hou.pasteNodesFromClipboard).",
        "fields": {"parent_path": "Network to paste into (alias: path)."},
    },
    {
        "op": "obj.display_sop_path",
        "summary": "OBJ geo container: resolve the display SOP (displayNode() or scan isDisplayFlagSet).",
        "fields": {"obj_path": "e.g. /obj/geo1 (alias: node_path)."},
    },
    {
        "op": "obj.render_sop_path",
        "summary": "OBJ geo container: resolve the render SOP (renderNode() or scan isRenderFlagSet).",
        "fields": {"obj_path": "e.g. /obj/geo1 (alias: node_path)."},
    },
    {
        "op": "obj.world_bounds",
        "summary": "OBJ geo container world-space AABB: geometryBoundingBox() when available; else display SOP bbox promoted by OBJ worldTransform().",
        "fields": {"obj_path": "e.g. /obj/geo1 (alias: node_path).", "force_cook": "Bool, default true (display path)."},
    },
    {
        "op": "obj.geo_summary",
        "summary": "OBJ geo container: display + render paths, bbox, point/prim/vertex counts from display cooked geo.",
        "fields": {"obj_path": "e.g. /obj/geo1 (alias: node_path).", "force_cook": "Bool, default true."},
    },
    {
        "op": "obj.file_node_set_path",
        "summary": "Set a file/Alembic/USD-style disk path parm on a node (file, filepath1, …; optional parm_name).",
        "fields": {"node_path": "Any node with a file parm.", "file_path": "Path (alias: path).", "parm_name": "Optional force token."},
    },
    {
        "op": "obj.camera_clip",
        "summary": "Set camera near/far clipping (heuristic parm names: near/znear, far/zfar).",
        "fields": {"node_path": "Camera OBJ node.", "near": "Optional float.", "far": "Optional float."},
    },
    {
        "op": "geo.prim_intrinsics_bulk",
        "summary": "Batch-read hou.Prim intrinsicNames/intrinsicValue (Volume/VDB/Packed/unpack-friendly); optional prim indices or filters.",
        "fields": {
            "node_path": "SOP with geometry().",
            "prim_indices": "Optional explicit prim numbers (alias: indices); otherwise scan from start with filters.",
            "max_primitives": "How many prims to return when scanning (default 24, cap 512).",
            "max_intrinsics_per_prim": "Cap intrinsic keys per prim (default 256; 0 = large internal cap).",
            "prim_type_contains": "Substring filter on str(prim.type()) (alias: type_filter).",
            "volume_family_only": "Shortcut filter for names containing vdb/volume/fog/openvdb/houdini.",
            "keys_only": "Return intrinsic_names lists without values (smaller payload).",
            "force_cook": "Bool, default true.",
        },
    },
    {
        "op": "geo.volume_primitives_scan",
        "summary": "Lightweight listing of volume/VDB-like primitives with first intrinsic name tokens.",
        "fields": {"node_path": "SOP path.", "max_list": "Cap listed prims (default 128).", "force_cook": "Bool, default true."},
    },
    {
        "op": "geo.prim_bbox",
        "summary": "Axis-aligned bounding box of one primitive (prim.boundingBox()).",
        "fields": {"node_path": "SOP path.", "prim_index": "Prim index (alias: primitive).", "force_cook": "Bool, default true."},
    },
    {
        "op": "vellum.graph_summary",
        "summary": "Recursive scan under a network for nodes whose type name contains vellum/cloth/… tokens.",
        "fields": {
            "parent_path": "OBJ/DOP subnet to scan (alias: path).",
            "type_contains": "Optional extra tokens (list or comma string); defaults include vellum, cloth, hair, …",
            "max_nodes": "Stop after this many matches (default 200).",
        },
    },
    {
        "op": "rop.evaluate_path",
        "summary": "Evaluate a render/output file path parm at a frame (default channel/picture).",
        "fields": {
            "node_path": "Absolute path to a ROP / driver node.",
            "channel": "Parm name (alias: parm_name); default picture.",
            "frame": "Optional float; default hou.frame().",
        },
    },
    {
        "op": "lop.stage_summary",
        "summary": "Solaris / LOP: call node.stage() and summarize (prim count; optional include_layer_paths for root sublayers).",
        "fields": {
            "node_path": "LOP / stage node path.",
            "max_prims": "Safety cap for traversal (default 500000).",
            "include_layer_paths": "Bool; if true, add root_layer_identifier and sub_layer_paths to payload.",
        },
    },
    {
        "op": "solaris.usd_file_set",
        "summary": "Set a USD / layer / import file path on a LOP node (tries filepath1, file, usdfilepath, …; optional parm_name to force the token).",
        "fields": {
            "node_path": "LOP node (e.g. usdimport, sublayer, reference).",
            "file_path": "Path on disk (alias: path).",
            "parm_name": "Optional: exact parm token if heuristics miss.",
        },
    },
    {
        "op": "solaris.karma_render_set",
        "summary": "Karma / USD Render style ROP or driver: set picture and/or camera and/or resolution (heuristic parm names across builds).",
        "fields": {
            "node_path": "ROP under /out or Solaris render node.",
            "picture": "Output image path (alias: picture_path).",
            "camera": "Camera OBJ path string (alias: camera_path).",
            "width": "Pixel width (alias: res_width).",
            "height": "Pixel height (alias: res_height).",
            "override_resolution": "Optional toggle when width/height used (alias: enable_resolution_override).",
        },
    },
    {
        "op": "mtlx.texture_file_set",
        "summary": "MaterialX image/file texture: set texture map path (file, filename, filepath, …).",
        "fields": {"node_path": "MTLX texture / image node.", "file_path": "Texture path (aliases: path, texture_path).", "parm_name": "Optional force token."},
    },
    {
        "op": "mtlx.standard_surface_set",
        "summary": "MaterialX standard_surface-style shading parms (best-effort aliases): roughness, metallic, coat, base_color [r,g,b], specular.",
        "fields": {
            "node_path": "MTLX standard_surface (or compatible) node.",
            "roughness": "Float.",
            "metallic": "Float (alias: metalness).",
            "coat": "Float (aliases: coat_weight).",
            "base_color": "[r,g,b] or scalar fallback.",
            "specular": "Scalar or [r,g,b] for specular_color tuple.",
        },
    },
    {
        "op": "exec.render_rop",
        "summary": "Call .render() on a ROP node — blocks until render finishes.",
        "fields": {"node_path": "Absolute path to ROP / driver node."},
    },
    {
        "op": "exec.render_write",
        "summary": "Blocking disk write for driver-like nodes: .execute() when present else .render().",
        "fields": {"node_path": "Absolute path to ROP / driver / COP-like node with execute/render."},
    },
    {
        "op": "exec.node_execute",
        "summary": "Blocking hou.Node.execute() — use for Alembic ROP, File Cache TOP, COP exports, etc.",
        "fields": {"node_path": "Target node with .execute()."},
    },
    {
        "op": "exec.python",
        "summary": "Execute arbitrary Python in the live Houdini session (hou in scope). Prefer undo_group; use for iterative NL→script workflows like reference Houdini MCPs.",
        "fields": {
            "code": "Python source (alias: source).",
            "use_undo_group": "Bool, default true.",
            "undo_label": "Undo block name, default mcp_exec_python.",
        },
    },
    {
        "op": "scene.summary",
        "summary": "Read-only: hip/frame/fps/selection plus /obj children; optional sop_children name samples per GEO. With rich_context, adds playback_globals ($RFSTART/$RFEND vs frame), selected_node_details (parm samples + cook errors), geo_display_hints (display SOP prim/point/packed hints).",
        "fields": {
            "max_obj_nodes": "Cap listed OBJ nodes, default 200.",
            "include_sop_children": "Bool, default true.",
            "rich_context": "Bool, default true — extra grounding for NL workflows.",
            "max_selected_detail_nodes": "Cap selected nodes expanded (0–32), default 8.",
            "max_parms_per_node": "Parm samples per selected node (1–128), default 24.",
            "geo_hint_max_geos": "First N GEO rows to cook display SOP for topology hints, default 6.",
            "diagnostics_force_cook": "Bool; if true, cook selected nodes before diagnostics (default true).",
        },
    },
    {
        "op": "session.snapshot",
        "summary": "Read-only merge of hip path, timeline state, and selected node paths; optional include_desktop for network editor pwd (needs hou.ui).",
        "fields": {"include_desktop": "Bool; if true, add desktop context (network pwd / current node)."},
    },
    {
        "op": "viewport.snapshot",
        "summary": "Save SceneViewer to image(s) via flipbook (needs GUI). Single frame, frame_start..frame_end range, or sparse `frames` list; max ~96 frames.",
        "fields": {
            "output_path": "File template (alias: path). For multi-frame, use .$F4 or receiver injects it.",
            "frame_start": "Optional float; single capture or range start.",
            "frame_end": "If set, contiguous range with frame_step (mutually exclusive with `frames`).",
            "frame_step": "Default 1.0 for range export.",
            "frames": "JSON array of floats for sparse captures (mutually exclusive with frame_end).",
            "restore_playbar_frame": "Bool, default true.",
            "include_image_base64": "If true, data.viewport_images[] with mime + data_base64 for multimodal review.",
            "max_image_bytes_per_file": "Cap per embedded file.",
            "max_images_embedded": "Max images when many files written.",
            "viewport_autoframe": "Bool or 'auto'|'all'|'selected'|'off'; default true — re-center view before capture (bbox from frame_node_path / selection, else frameSelected, else frameAll).",
            "frame_node_path": "Optional OBJ or SOP path to frame via display-geometry bbox; omit to use selection-only / frameAll fallback.",
        },
    },
    {
        "op": "shelf.run_tool",
        "summary": "Run a shelf tool by internal path (tries hscript toolrun, hou.ui, hou.shelves — version-dependent).",
        "fields": {
            "tool_path": "Shelf tool path (alias: tool_name, name), e.g. as shown in shelf editor.",
        },
    },
    {
        "op": "node.preset_apply",
        "summary": "Apply a named node preset if supported (hou.nodePresets / node methods — version-dependent).",
        "fields": {
            "node_path": "Absolute node path.",
            "preset_name": "Preset label (alias: name, preset).",
        },
    },
    {
        "op": "obj.xform_get",
        "summary": "Read translate/rotate/scale vectors from common parm tuples (t/r/s or translate/rotate/scale).",
        "fields": {"obj_path": "Object-level node (alias: node_path)."},
    },
    {
        "op": "obj.xform_set",
        "summary": "Set translate and/or rotate and/or scale using the same tuple naming rules as obj.xform_get.",
        "fields": {
            "obj_path": "Object-level node (alias: node_path).",
            "translate": "Optional [x,y,z] or uniform float (alias: t).",
            "rotate": "Optional [rx,ry,rz] degrees (alias: r).",
            "scale": "Optional [sx,sy,sz] or uniform float (alias: s).",
        },
    },
    {
        "op": "obj.world_transform_get",
        "summary": "World-space matrix (16 floats) plus translate/rotate/scale when Matrix4 extraction succeeds.",
        "fields": {"obj_path": "Absolute path (alias: node_path)."},
    },
    {
        "op": "obj.local_transform_get",
        "summary": "Local transform matrix (hou.Node.localTransform) plus translate/rotate/scale when available.",
        "fields": {"obj_path": "Absolute path (alias: node_path)."},
    },
    {
        "op": "path.expand_string",
        "summary": "Expand $HIP and variables via hou.expandString.",
        "fields": {"string": "Expression string (alias: path, value)."},
    },
    {
        "op": "path.file_exists",
        "summary": "Expand path then check os.path.isfile / isdir.",
        "fields": {"file_path": "Path string (alias: path)."},
    },
    {
        "op": "cache.clear_all",
        "summary": "Clear simulation/dynamics caches via hou.clearAllCaches() when available.",
        "fields": {},
    },
    {
        "op": "cache.pdg_clear",
        "summary": "Best-effort PDG / work item cache clear (hou.pdg / pdg module, optional node_path pdgNode(), then scene cache fallback).",
        "fields": {"node_path": "Optional TOP node path to attempt pdgNode() cache clear first."},
    },
    {
        "op": "top.workitems_scan",
        "summary": "Scan TOP work items via hou.TopNode.getPDGNode() + workItems; optional cookWorkItems to build PDG graph. Falls back to child_nodes + suggested_child_nodes on containers or empty graphs.",
        "fields": {
            "node_path": "TOP / scheduler (or /tasks container) node path.",
            "max_items": "Optional sample cap for returned work_items (default 20, max 200).",
            "cook_first": "Bool, default true: call cookWorkItems(block=True) if no work items yet.",
            "tops_only": "Bool, passed to cookWorkItems when supported (scheduler init / tops-only cooks).",
            "generate_only": "Bool, passed to cookWorkItems when supported (generate without full cook).",
        },
    },
    {
        "op": "hda.ensure_file",
        "summary": "Expand path and hou.hda.installFile (idempotent re-install of .hda/.otl to session).",
        "fields": {"file_path": "On-disk HDA (alias: path)."},
    },
    {
        "op": "io.file_parms_guess",
        "summary": "Heuristic list of string parm names that look like file/path/texture inputs (for Alembic/FBX/USD wrangling).",
        "fields": {"node_path": "Any node with parm templates."},
    },
    {
        "op": "chop.parm_channel_state",
        "summary": "Read-only: expression, raw value preview, keyframe count, and ch()-like hint (not a full CHOP graph dump).",
        "fields": {"node_path": "Absolute path.", "parm_name": "Parm or tuple name.", "component": "Optional index for vector parms."},
    },
    {
        "op": "lop.usd_layer_stack",
        "summary": "USD: root layer id + subLayerPaths from stage (requires .stage() on LOP).",
        "fields": {"node_path": "LOP with stage()."},
    },
    {
        "op": "viewport.flipbook",
        "summary": "First SceneViewer flipbook to output_path (GUI/headless may fail; blocking).",
        "fields": {"output_path": "Image / sequence path (alias: path, file_path)."},
    },
    {
        "op": "exec.cache",
        "summary": "Deprecated: calls hou.clearAllCaches() like cache.clear_all; use cache.clear_all or cache.pdg_clear.",
        "fields": {},
    },
    {
        "op": "hip.save",
        "summary": "Save .hip; omit file_path to save current file (may prompt if never saved).",
        "fields": {"file_path": "Optional absolute path to save as."},
    },
    {
        "op": "hip.load",
        "summary": "Load a .hip file (replaces session). Use with care.",
        "fields": {"file_path": "Absolute path.", "ignore_load_warnings": "Bool, default false."},
    },
    {
        "op": "hip.new",
        "summary": "Clear the session (new file). suppress_save_prompt default true for automation.",
        "fields": {"suppress_save_prompt": "Bool, default true."},
    },
    {
        "op": "hip.merge",
        "summary": "Merge another .hip into the current session (hou.hipFile.merge).",
        "fields": {"file_path": "Absolute path.", "ignore_load_warnings": "Bool, default false."},
    },
    {
        "op": "hip.session_info",
        "summary": "Read-only: current .hip path and unsaved-changes flag.",
        "fields": {},
    },
    {
        "op": "undo.begin",
        "summary": "No-op when using batch undo_group (kept for compatibility).",
        "fields": {},
    },
    {
        "op": "undo.end",
        "summary": "No-op when using batch undo_group.",
        "fields": {},
    },
    {
        "op": "undo.rollback",
        "summary": "Perform one Houdini undo (use carefully outside batches).",
        "fields": {},
    },
    {
        "op": "validate.node",
        "summary": "Fail if the node has cook errors.",
        "fields": {"node_path": "Absolute node path."},
    },
    {
        "op": "validate.parm_range",
        "summary": "Check value against float/int min/max, menu index, or toggle; fails if out of range (unbounded types get a warning).",
        "fields": {
            "node_path": "Absolute node path.",
            "parm_name": "Parameter name.",
            "value": "Candidate value.",
            "component": "Optional vector component index for tuple parms.",
        },
    },
]
# fmt: on


def get_op_catalog() -> dict[str, Any]:
    return {
        "ok": True,
        "ops": list(_OPS),
        "note": (
            "Compose these into plan_build_adhoc actions_json as a JSON array of objects, "
            "each with required key 'op' plus op-specific fields. Paths must be absolute Houdini node paths."
        ),
    }
