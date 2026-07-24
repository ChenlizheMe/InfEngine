"""ParticleGraph editor authoring tools for MCP developer sessions."""

from __future__ import annotations

import os

from Infernux.engine.path_utils import relative_path, same_path
from Infernux.mcp.tools.common import (
    find_game_object,
    main_thread,
    register_tool_metadata,
    resolve_asset_path,
)


def register_particle_tools(mcp, project_path: str) -> None:
    _register_metadata()

    @mcp.tool(name="particle_graph_open_asset")
    def particle_graph_open_asset(asset_path: str) -> dict:
        """Open one ParticleGraph asset in the visible editor window."""

        def _open():
            target = resolve_asset_path(project_path, asset_path)
            if os.path.splitext(target)[1].lower() != ".particlegraph":
                raise ValueError(
                    "particle_graph_open_asset requires a .particlegraph asset"
                )
            if not os.path.isfile(target):
                raise FileNotFoundError(f"ParticleGraph asset not found: {asset_path}")
            panel = _open_particle_graph_panel(target)
            return _portable_snapshot(panel.authoring_snapshot(), project_path)

        return main_thread(
            "particle_graph_open_asset",
            _open,
            arguments={"asset_path": asset_path},
        )

    @mcp.tool(name="particle_graph_inspect_editor")
    def particle_graph_inspect_editor() -> dict:
        """Inspect the ParticleGraph document currently open in its editor."""

        def _inspect():
            panel = _require_particle_graph_panel()
            return _portable_snapshot(panel.authoring_snapshot(), project_path)

        return main_thread("particle_graph_inspect_editor", _inspect)

    @mcp.tool(name="particle_graph_set_node_asset")
    def particle_graph_set_node_asset(
        node_uid: str,
        property_name: str,
        asset_path: str,
    ) -> dict:
        """Set a Mesh or Material AssetRef on a node in the live ParticleGraph editor."""

        def _set():
            panel = _require_particle_graph_panel()
            target = resolve_asset_path(project_path, asset_path)
            reference = panel.set_node_asset_reference(
                str(node_uid), str(property_name), target
            )
            snapshot = _portable_snapshot(panel.authoring_snapshot(), project_path)
            return {
                "node_uid": str(node_uid),
                "property_name": str(property_name),
                "asset": reference,
                "editor": snapshot,
            }

        return main_thread(
            "particle_graph_set_node_asset",
            _set,
            arguments={
                "node_uid": node_uid,
                "property_name": property_name,
                "asset_path": asset_path,
            },
        )

    @mcp.tool(name="particle_graph_add_node")
    def particle_graph_add_node(
        stage: str,
        type_id: str,
        x: float = 0.0,
        y: float = 0.0,
    ) -> dict:
        """Add one typed node through the live ParticleGraph editor model."""

        def _add():
            panel = _require_particle_graph_panel()
            node = panel.add_authoring_node(stage, type_id, x, y)
            return {
                "node": node,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_add_node",
            _add,
            arguments={"stage": stage, "type_id": type_id, "x": x, "y": y},
        )

    @mcp.tool(name="particle_graph_set_node_property")
    def particle_graph_set_node_property(
        node_uid: str,
        property_name: str,
        value,
    ) -> dict:
        """Set one typed non-asset property on a live ParticleGraph node."""

        def _set():
            panel = _require_particle_graph_panel()
            result = panel.set_node_property(node_uid, property_name, value)
            return {
                **result,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_set_node_property",
            _set,
            arguments={
                "node_uid": node_uid,
                "property_name": property_name,
                "value": value,
            },
        )

    @mcp.tool(name="particle_graph_connect_stream")
    def particle_graph_connect_stream(
        source_node_uid: str,
        target_node_uid: str,
    ) -> dict:
        """Connect the stream output/input of two nodes in the same particle stage."""

        def _connect():
            panel = _require_particle_graph_panel()
            result = panel.connect_stream(source_node_uid, target_node_uid)
            return {
                **result,
                "source_node_uid": str(source_node_uid),
                "target_node_uid": str(target_node_uid),
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_connect_stream",
            _connect,
            arguments={
                "source_node_uid": source_node_uid,
                "target_node_uid": target_node_uid,
            },
        )

    @mcp.tool(name="particle_graph_connect_value")
    def particle_graph_connect_value(
        source_node_uid: str,
        source_port: str,
        target_node_uid: str,
        target_port: str,
    ) -> dict:
        """Connect or replace one typed value input in the live ParticleGraph."""

        def _connect():
            panel = _require_particle_graph_panel()
            result = panel.connect_value(
                source_node_uid,
                source_port,
                target_node_uid,
                target_port,
            )
            return {
                **result,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_connect_value",
            _connect,
            arguments={
                "source_node_uid": source_node_uid,
                "source_port": source_port,
                "target_node_uid": target_node_uid,
                "target_port": target_port,
            },
        )

    @mcp.tool(name="particle_graph_select_emitter")
    def particle_graph_select_emitter(emitter_id: str) -> dict:
        """Select one emitter in the visible ParticleGraph editor."""

        def _select():
            panel = _require_particle_graph_panel()
            result = panel.select_authoring_emitter(emitter_id)
            return {
                **result,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_select_emitter",
            _select,
            arguments={"emitter_id": emitter_id},
        )

    @mcp.tool(name="particle_graph_add_emitter")
    def particle_graph_add_emitter(name: str) -> dict:
        """Add one emitter through the live ParticleGraph document."""

        def _add():
            panel = _require_particle_graph_panel()
            emitter = panel.add_authoring_emitter(name)
            return {
                "emitter": emitter,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_add_emitter",
            _add,
            arguments={"name": name},
        )

    @mcp.tool(name="particle_graph_set_emitter_settings")
    def particle_graph_set_emitter_settings(
        emitter_id: str, settings: dict
    ) -> dict:
        """Replace one emitter's complete current settings schema."""

        def _set():
            panel = _require_particle_graph_panel()
            result = panel.set_authoring_emitter_settings(emitter_id, settings)
            return {
                **result,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_set_emitter_settings",
            _set,
            arguments={"emitter_id": emitter_id, "settings": settings},
        )

    @mcp.tool(name="particle_graph_add_event_type")
    def particle_graph_add_event_type(
        name: str,
        capacity_per_step: int,
        fields: list[dict],
    ) -> dict:
        """Add a typed event schema through the live ParticleGraph document."""

        def _add():
            panel = _require_particle_graph_panel()
            event_type = panel.add_event_type(name, capacity_per_step, fields)
            return {
                "event_type": event_type,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_add_event_type",
            _add,
            arguments={
                "name": name,
                "capacity_per_step": capacity_per_step,
                "fields": fields,
            },
        )

    @mcp.tool(name="particle_graph_add_event_route")
    def particle_graph_add_event_route(
        event_type_id: str,
        source_emitter_id: str,
        source_stage: str,
        target_emitter_id: str,
        spawn_count: int = 1,
    ) -> dict:
        """Add a directed typed route through the live ParticleGraph document."""

        def _add():
            panel = _require_particle_graph_panel()
            route = panel.add_event_route(
                event_type_id,
                source_emitter_id,
                source_stage,
                target_emitter_id,
                spawn_count,
            )
            return {
                "event_route": route,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_add_event_route",
            _add,
            arguments={
                "event_type_id": event_type_id,
                "source_emitter_id": source_emitter_id,
                "source_stage": source_stage,
                "target_emitter_id": target_emitter_id,
                "spawn_count": spawn_count,
            },
        )

    @mcp.tool(name="particle_graph_set_rendering_output")
    def particle_graph_set_rendering_output(node_uid: str) -> dict:
        """Connect the Rendering root stream to exactly one output node."""

        def _set():
            panel = _require_particle_graph_panel()
            result = panel.set_rendering_output(str(node_uid))
            return {
                **result,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_set_rendering_output",
            _set,
            arguments={"node_uid": node_uid},
        )

    @mcp.tool(name="particle_graph_reload_editor")
    def particle_graph_reload_editor() -> dict:
        """Reload the open ParticleGraph from disk after a successful save."""

        def _reload():
            panel = _require_particle_graph_panel()
            if not panel.reload_from_disk():
                raise RuntimeError("Particle Graph editor could not reload its asset")
            return _portable_snapshot(panel.authoring_snapshot(), project_path)

        return main_thread("particle_graph_reload_editor", _reload)

    @mcp.tool(name="particle_system_inspect_runtime")
    def particle_system_inspect_runtime(
        object_id: int, ordinal: int = 0
    ) -> dict:
        """Inspect one live ParticleSystem control plane without reading particles back."""

        def _inspect():
            obj = find_game_object(object_id)
            component = _find_particle_system(obj, int(ordinal))
            if component is None:
                raise FileNotFoundError(
                    f"ParticleSystem {ordinal} was not found on GameObject {object_id}."
                )
            return {
                "object_id": int(obj.id),
                "object_name": str(obj.name),
                "runtime": component.runtime_diagnostics(),
            }

        return main_thread(
            "particle_system_inspect_runtime",
            _inspect,
            arguments={"object_id": object_id, "ordinal": ordinal},
        )

    @mcp.tool(name="particle_system_request_gpu_diagnostics")
    def particle_system_request_gpu_diagnostics(
        object_id: int, ordinal: int = 0
    ) -> dict:
        """Request one asynchronous GPU particle counter snapshot."""

        def _request():
            obj = find_game_object(object_id)
            component = _find_particle_system(obj, int(ordinal))
            if component is None:
                raise FileNotFoundError(
                    f"ParticleSystem {ordinal} was not found on GameObject {object_id}."
                )
            return {
                "object_id": int(obj.id),
                "object_name": str(obj.name),
                "request_id": component.request_gpu_diagnostics(),
                "status": "pending",
            }

        return main_thread(
            "particle_system_request_gpu_diagnostics",
            _request,
            arguments={"object_id": object_id, "ordinal": ordinal},
        )

    @mcp.tool(name="particle_system_poll_gpu_diagnostics")
    def particle_system_poll_gpu_diagnostics(
        object_id: int, request_id: int, ordinal: int = 0
    ) -> dict:
        """Poll a previously requested GPU particle counter snapshot."""

        def _poll():
            obj = find_game_object(object_id)
            component = _find_particle_system(obj, int(ordinal))
            if component is None:
                raise FileNotFoundError(
                    f"ParticleSystem {ordinal} was not found on GameObject {object_id}."
                )
            return {
                "object_id": int(obj.id),
                "object_name": str(obj.name),
                "diagnostics": component.poll_gpu_diagnostics(int(request_id)),
            }

        return main_thread(
            "particle_system_poll_gpu_diagnostics",
            _poll,
            arguments={
                "object_id": object_id,
                "request_id": request_id,
                "ordinal": ordinal,
            },
        )


def _require_particle_graph_panel():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.engine.ui.window_manager import WindowManager

    manager = WindowManager.instance()
    panel = (
        manager.get_window_instance("particle_graph_editor")
        if manager is not None
        else None
    )
    if not isinstance(panel, ParticleGraphEditorPanel) or not bool(panel.is_open):
        raise RuntimeError(
            "Particle Graph Editor is not open. Open a .particlegraph asset first."
        )
    return panel


def _find_particle_system(obj, ordinal: int):
    from Infernux.components.particle_system import ParticleSystem

    matches = []
    try:
        matches = [
            component
            for component in (obj.get_py_components() or ())
            if isinstance(component, ParticleSystem)
        ]
    except (AttributeError, RuntimeError, TypeError):
        return None
    return matches[ordinal] if 0 <= ordinal < len(matches) else None


def _open_particle_graph_panel(file_path: str):
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.engine.ui.window_manager import WindowManager

    manager = WindowManager.instance()
    if manager is None:
        raise RuntimeError("WindowManager is not initialized")

    panel = manager.get_window_instance("particle_graph_editor")
    if isinstance(panel, ParticleGraphEditorPanel) and bool(panel.is_open):
        snapshot = panel.authoring_snapshot()
        current_path = str(snapshot.get("file_path") or "")
        if current_path and same_path(current_path, file_path):
            _focus_particle_graph_panel(manager)
            return panel
        if bool(snapshot.get("dirty")):
            raise RuntimeError(
                "Particle Graph Editor has unsaved changes; save or discard them before opening another asset"
            )

    panel = manager.open_window("particle_graph_editor")
    if not isinstance(panel, ParticleGraphEditorPanel):
        raise RuntimeError("Particle Graph Editor window could not be opened")
    if not panel._open_particlegraph(file_path):
        raise RuntimeError(f"ParticleGraph asset could not be opened: {file_path}")
    _focus_particle_graph_panel(manager)
    return panel


def _focus_particle_graph_panel(manager) -> None:
    from Infernux.engine.ui.closable_panel import ClosablePanel

    ClosablePanel.focus_panel_by_id("particle_graph_editor")
    try:
        manager._engine.select_docked_window("particle_graph_editor")
    except Exception:
        pass


def _portable_snapshot(snapshot: dict, project_path: str) -> dict:
    result = dict(snapshot)
    file_path = str(result.get("file_path") or "")
    if file_path:
        result["file_path"] = relative_path(file_path, project_path)
    return result


def _register_metadata() -> None:
    register_tool_metadata(
        "particle_graph_open_asset",
        summary="Open a ParticleGraph asset in the visible Particle Graph Editor.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "open", "vfx"],
        aliases=["open particle graph", "打开粒子图"],
        preconditions=["asset_path must identify an imported .particlegraph asset."],
        side_effects=["Opens and focuses the visible Particle Graph Editor window."],
        recovery=[
            "Save or discard the currently open dirty ParticleGraph before opening another asset."
        ],
        next_suggested_tools=["particle_graph_inspect_editor"],
    )
    register_tool_metadata(
        "particle_graph_inspect_editor",
        summary="Inspect the live ParticleGraph editor document and node properties.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "vfx"],
        aliases=["particle graph nodes", "粒子图节点", "粒子图检查"],
        preconditions=["A .particlegraph asset must be open in Particle Graph Editor."],
        recovery=["Open the ParticleGraph asset in the editor, then retry."],
        next_suggested_tools=["particle_graph_set_node_asset", "editor_save_focused"],
    )
    register_tool_metadata(
        "particle_graph_add_node",
        summary="Add a typed node through the live ParticleGraph authoring model.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "authoring"],
        aliases=["add particle node", "创建粒子节点"],
        preconditions=["A .particlegraph asset must be open."],
        side_effects=["Records Undo, marks the panel dirty, and republishes the live draft."],
        recovery=["Use particle_graph_inspect_editor to inspect valid canvas state."],
        next_suggested_tools=[
            "particle_graph_set_node_property",
            "particle_graph_connect_stream",
        ],
    )
    register_tool_metadata(
        "particle_graph_set_node_property",
        summary="Set a typed scalar/vector property through the live ParticleGraph editor.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "property"],
        aliases=["edit particle node", "设置粒子节点参数"],
        preconditions=[
            "The node must exist in the open ParticleGraph.",
            "Asset references must use particle_graph_set_node_asset.",
        ],
        side_effects=["Records Undo, marks the panel dirty, and republishes the live draft."],
        recovery=["Inspect the node properties before retrying."],
        next_suggested_tools=["particle_graph_connect_stream", "editor_save_document"],
    )
    register_tool_metadata(
        "particle_graph_connect_stream",
        summary="Connect two stream nodes in one ParticleGraph stage.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "stream"],
        aliases=["connect particle nodes", "连接粒子节点"],
        preconditions=[
            "Both node UIDs must exist in the same stage.",
            "The source must expose out and the target must expose in stream ports.",
        ],
        side_effects=["Records Undo, marks the panel dirty, and republishes the live draft."],
        recovery=["Inspect existing links and endpoint stages before retrying."],
        next_suggested_tools=["editor_save_document", "particle_graph_inspect_editor"],
    )
    register_tool_metadata(
        "particle_graph_connect_value",
        summary="Connect or replace one typed ParticleGraph value input.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "value"],
        aliases=["connect particle value", "连接粒子数值端口"],
        preconditions=["Both nodes and named ports must exist in the selected emitter."],
        side_effects=["Records Undo, marks the document dirty, and republishes the live draft."],
        recovery=["Inspect registered node types, nodes, ports, and existing links before retrying."],
        next_suggested_tools=["editor_save_document", "particle_graph_inspect_editor"],
    )
    register_tool_metadata(
        "particle_graph_select_emitter",
        summary="Select an emitter by stable ID in the visible ParticleGraph editor.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "emitter"],
        aliases=["select particle emitter", "选择粒子发射器"],
        preconditions=["The emitter stable ID must appear in particle_graph_inspect_editor."],
        side_effects=["Changes the visible authoring emitter without modifying the asset."],
        recovery=["Inspect the editor and retry with an existing emitter stable ID."],
        next_suggested_tools=["particle_graph_add_node", "particle_graph_inspect_editor"],
    )
    register_tool_metadata(
        "particle_graph_add_emitter",
        summary="Add an emitter through the live ParticleGraph editor document.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "emitter", "authoring"],
        aliases=["add particle emitter", "添加粒子发射器"],
        preconditions=["A .particlegraph asset must be open."],
        side_effects=["Records Undo, selects the new emitter, and republishes the live draft."],
        recovery=["Use a non-empty emitter display name that is unique in the graph."],
        next_suggested_tools=["particle_graph_set_emitter_settings"],
    )
    register_tool_metadata(
        "particle_graph_set_emitter_settings",
        summary="Replace one emitter's complete current settings through the editor.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "emitter", "settings"],
        aliases=["set particle emitter", "设置粒子发射器"],
        preconditions=["Use the complete settings object returned by particle_graph_inspect_editor."],
        side_effects=["Records Undo, marks the document dirty, and republishes the live draft."],
        recovery=["Inspect the emitter and retry with the exact current settings field set."],
        next_suggested_tools=["particle_graph_add_event_route", "editor_save_document"],
    )
    register_tool_metadata(
        "particle_graph_add_event_type",
        summary="Add a typed event schema through the live ParticleGraph editor.",
        category="assets/particle_graph",
        tags=["particle", "graph", "event", "schema"],
        aliases=["add particle event", "添加粒子事件类型"],
        preconditions=["Field types use the current TypeRef object shape."],
        side_effects=["Records Undo, rebuilds derived node definitions, and republishes the draft."],
        recovery=["Fix invalid field types/defaults and retry; no partial schema is retained."],
        next_suggested_tools=["particle_graph_add_event_route"],
    )
    register_tool_metadata(
        "particle_graph_add_event_route",
        summary="Route one typed event between two ParticleGraph emitters.",
        category="assets/particle_graph",
        tags=["particle", "graph", "event", "route"],
        aliases=["route particle event", "连接粒子事件"],
        preconditions=["Event and emitter stable IDs must exist; routes must remain acyclic."],
        side_effects=["Records Undo and exposes route-specific Event Output/Payload nodes."],
        recovery=["Inspect event_types, event_routes, and emitters before retrying."],
        next_suggested_tools=["particle_graph_add_node", "particle_graph_inspect_editor"],
    )
    register_tool_metadata(
        "particle_graph_set_node_asset",
        summary="Set a Mesh or Material reference through the live ParticleGraph editor model.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "mesh", "material"],
        aliases=["set mesh output", "particle material", "设置粒子网格"],
        preconditions=[
            "A .particlegraph asset must be open.",
            "asset_path must identify an imported asset inside Assets/.",
        ],
        side_effects=[
            "Updates the live editor document, records Undo, marks the panel dirty, and republishes its in-memory draft."
        ],
        recovery=[
            "Call particle_graph_inspect_editor to verify node_uid and AssetRef property names."
        ],
        next_suggested_tools=[
            "particle_graph_inspect_editor",
            "particle_graph_set_rendering_output",
            "editor_save_document",
        ],
    )
    register_tool_metadata(
        "particle_graph_set_rendering_output",
        summary="Route one emitter's Rendering stream to a selected output node.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "output"],
        aliases=["set particle output", "connect mesh output", "设置粒子输出"],
        preconditions=[
            "A .particlegraph asset must be open.",
            "node_uid must identify a Rendering output in the live editor document.",
        ],
        side_effects=[
            "Replaces the Rendering root output connection, records Undo, and marks the document dirty."
        ],
        recovery=[
            "Call particle_graph_inspect_editor and inspect links before retrying."
        ],
        next_suggested_tools=["editor_save_document", "particle_graph_reload_editor"],
    )
    register_tool_metadata(
        "particle_graph_reload_editor",
        summary="Reload the clean ParticleGraph editor document from its saved asset.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "reload", "persistence"],
        aliases=["reopen particle graph", "verify particle save", "重载粒子图"],
        preconditions=["The open ParticleGraph document must be clean and have a source path."],
        recovery=["Save the document with editor_save_document, then retry."],
        next_suggested_tools=["particle_graph_inspect_editor"],
    )
    register_tool_metadata(
        "particle_system_inspect_runtime",
        summary="Inspect ParticleSystem scheduling, hot-reload, and event-domain state on demand.",
        category="runtime/particles",
        tags=["particle", "runtime", "event", "hot reload", "diagnostics"],
        aliases=["particle runtime state", "粒子运行状态"],
        preconditions=["object_id must own a live ParticleSystem component."],
        recovery=["Find the object and verify its component list before retrying."],
        next_suggested_tools=["runtime_read_errors", "capture_request"],
    )
    register_tool_metadata(
        "particle_system_request_gpu_diagnostics",
        summary="Request one asynchronous GPU particle/event counter snapshot.",
        category="runtime/particles",
        tags=["particle", "gpu", "event", "diagnostics", "readback"],
        aliases=["read particle counts", "读取粒子计数"],
        preconditions=["object_id must own a live GPU ParticleSystem component."],
        side_effects=["Records one counter-buffer copy after the next submitted frame."],
        recovery=["Keep the editor running and poll the returned request_id."],
        next_suggested_tools=["particle_system_poll_gpu_diagnostics"],
    )
    register_tool_metadata(
        "particle_system_poll_gpu_diagnostics",
        summary="Poll a requested GPU particle/event counter snapshot without stalling.",
        category="runtime/particles",
        tags=["particle", "gpu", "event", "diagnostics", "poll"],
        aliases=["poll particle counts", "轮询粒子计数"],
        preconditions=["request_id must come from the same ParticleSystem component."],
        recovery=["If status is pending, advance frames and poll again."],
        next_suggested_tools=["runtime_read_errors", "capture_request"],
    )
