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
    _register_authoring_metadata()

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

    @mcp.tool(name="particle_graph_list_node_types")
    def particle_graph_list_node_types(
        query: str = "",
        offset: int = 0,
        limit: int = 100,
    ) -> dict:
        """Search the node types available to the selected ParticleGraph emitter."""

        def _list():
            panel = _require_particle_graph_panel()
            return panel.authoring_type_catalog(
                query=str(query),
                offset=int(offset),
                limit=int(limit),
            )

        return main_thread(
            "particle_graph_list_node_types",
            _list,
            arguments={"query": query, "offset": offset, "limit": limit},
        )

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
        """Set one typed Inspector field on a live ParticleGraph node."""

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

    @mcp.tool(name="particle_graph_patch_emitter_settings")
    def particle_graph_patch_emitter_settings(
        emitter_id: str, values: dict
    ) -> dict:
        """Patch selected fields on one emitter through the strict editor schema."""

        def _patch():
            panel = _require_particle_graph_panel()
            result = panel.patch_authoring_emitter_settings(emitter_id, values)
            return {
                **result,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_patch_emitter_settings",
            _patch,
            arguments={"emitter_id": emitter_id, "values": values},
        )

    @mcp.tool(name="particle_graph_set_emitter_lifecycle")
    def particle_graph_set_emitter_lifecycle(
        emitter_id: str,
        enabled: bool,
        play_on_start: bool,
    ) -> dict:
        """Set Enabled and Play On Start independently from emission settings."""

        def _set():
            panel = _require_particle_graph_panel()
            result = panel.set_authoring_emitter_lifecycle(
                emitter_id,
                enabled=enabled,
                play_on_start=play_on_start,
            )
            return {
                **result,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_set_emitter_lifecycle",
            _set,
            arguments={
                "emitter_id": emitter_id,
                "enabled": enabled,
                "play_on_start": play_on_start,
            },
        )

    @mcp.tool(name="particle_graph_add_data_interface")
    def particle_graph_add_data_interface(
        emitter_id: str, kind: str, name: str = ""
    ) -> dict:
        """Add a typed Data Interface to one ParticleGraph emitter."""

        def _add():
            panel = _require_particle_graph_panel()
            interface = panel.add_authoring_data_interface(emitter_id, kind, name)
            return {
                "interface": interface,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_add_data_interface",
            _add,
            arguments={"emitter_id": emitter_id, "kind": kind, "name": name},
        )

    @mcp.tool(name="particle_graph_set_data_interface_asset")
    def particle_graph_set_data_interface_asset(
        emitter_id: str, interface_id: str, asset_path: str
    ) -> dict:
        """Set an imported source asset on a typed ParticleGraph Data Interface."""

        def _set():
            panel = _require_particle_graph_panel()
            target = resolve_asset_path(project_path, asset_path)
            interface = panel.set_authoring_data_interface_asset(
                emitter_id, interface_id, target
            )
            return {
                "interface": interface,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_set_data_interface_asset",
            _set,
            arguments={
                "emitter_id": emitter_id,
                "interface_id": interface_id,
                "asset_path": asset_path,
            },
        )

    @mcp.tool(name="particle_graph_patch_data_interface")
    def particle_graph_patch_data_interface(
        emitter_id: str, interface_id: str, values: dict
    ) -> dict:
        """Patch editable fields on one ParticleGraph Data Interface."""

        def _patch():
            panel = _require_particle_graph_panel()
            interface = panel.patch_authoring_data_interface(
                emitter_id, interface_id, values
            )
            return {
                "interface": interface,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_patch_data_interface",
            _patch,
            arguments={
                "emitter_id": emitter_id,
                "interface_id": interface_id,
                "values": values,
            },
        )

    @mcp.tool(name="particle_graph_remove_data_interface")
    def particle_graph_remove_data_interface(
        emitter_id: str, interface_id: str
    ) -> dict:
        """Remove an unreferenced Data Interface from one ParticleGraph emitter."""

        def _remove():
            panel = _require_particle_graph_panel()
            interface = panel.remove_authoring_data_interface(
                emitter_id, interface_id
            )
            return {
                "interface": interface,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_remove_data_interface",
            _remove,
            arguments={"emitter_id": emitter_id, "interface_id": interface_id},
        )

    @mcp.tool(name="particle_graph_remove_emitter")
    def particle_graph_remove_emitter(emitter_id: str) -> dict:
        """Remove an emitter and event routes that reference it."""

        def _remove():
            panel = _require_particle_graph_panel()
            result = panel.remove_authoring_emitter(emitter_id)
            return {
                **result,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_remove_emitter",
            _remove,
            arguments={"emitter_id": emitter_id},
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
                "event_type_id": event_type["stable_id"],
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
                "event_route_id": route["stable_id"],
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

    @mcp.tool(name="particle_graph_add_event_output")
    def particle_graph_add_event_output(
        route_id: str, x: float = 0.0, y: float = 0.0
    ) -> dict:
        """Add an Event Output in the route's source emitter and stage."""

        def _add():
            panel = _require_particle_graph_panel()
            node = panel.add_event_output_node(route_id, x, y)
            return {
                "node": node,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_add_event_output",
            _add,
            arguments={"route_id": route_id, "x": x, "y": y},
        )

    @mcp.tool(name="particle_graph_add_event_payload")
    def particle_graph_add_event_payload(
        route_id: str, x: float = 0.0, y: float = 0.0
    ) -> dict:
        """Add an Event Payload in the route's target Init graph."""

        def _add():
            panel = _require_particle_graph_panel()
            node = panel.add_event_payload_node(route_id, x, y)
            return {
                "node": node,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_add_event_payload",
            _add,
            arguments={"route_id": route_id, "x": x, "y": y},
        )

    @mcp.tool(name="particle_graph_update_event_type")
    def particle_graph_update_event_type(
        event_type_id: str,
        name: str,
        capacity_per_step: int,
        fields: list[dict],
    ) -> dict:
        """Update a typed event schema without changing its stable identity."""

        def _update():
            panel = _require_particle_graph_panel()
            event_type = panel.update_event_type(
                event_type_id, name, capacity_per_step, fields
            )
            return {
                "event_type": event_type,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_update_event_type",
            _update,
            arguments={
                "event_type_id": event_type_id,
                "name": name,
                "capacity_per_step": capacity_per_step,
                "fields": fields,
            },
        )

    @mcp.tool(name="particle_graph_update_event_route")
    def particle_graph_update_event_route(
        route_id: str,
        event_type_id: str,
        source_emitter_id: str,
        source_stage: str,
        target_emitter_id: str,
        spawn_count: int = 1,
    ) -> dict:
        """Update an event route without changing its stable identity."""

        def _update():
            panel = _require_particle_graph_panel()
            route = panel.update_event_route(
                route_id,
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
            "particle_graph_update_event_route",
            _update,
            arguments={
                "route_id": route_id,
                "event_type_id": event_type_id,
                "source_emitter_id": source_emitter_id,
                "source_stage": source_stage,
                "target_emitter_id": target_emitter_id,
                "spawn_count": spawn_count,
            },
        )

    @mcp.tool(name="particle_graph_remove_event_route")
    def particle_graph_remove_event_route(route_id: str) -> dict:
        """Remove one event route and its route-private graph nodes."""

        def _remove():
            panel = _require_particle_graph_panel()
            route = panel.remove_event_route(route_id)
            return {
                "event_route": route,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_remove_event_route",
            _remove,
            arguments={"route_id": route_id},
        )

    @mcp.tool(name="particle_graph_remove_event_type")
    def particle_graph_remove_event_type(event_type_id: str) -> dict:
        """Remove one event schema and cascade all dependent routes and nodes."""

        def _remove():
            panel = _require_particle_graph_panel()
            event_type = panel.remove_event_type(event_type_id)
            return {
                "event_type": event_type,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_remove_event_type",
            _remove,
            arguments={"event_type_id": event_type_id},
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

    @mcp.tool(name="particle_graph_discard_editor")
    def particle_graph_discard_editor() -> dict:
        """Explicitly discard the visible ParticleGraph document's unsaved state."""

        def _discard():
            panel = _require_particle_graph_panel()
            result = panel.discard_unsaved_changes()
            return {
                **result,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread("particle_graph_discard_editor", _discard)


def register_particle_runtime_tools(mcp) -> None:
    """Register live ParticleSystem controls independently from asset authoring."""
    _register_runtime_metadata()

    @mcp.tool(name="particle_system_inspect_runtime")
    def particle_system_inspect_runtime(
        object_id: int, ordinal: int = 0
    ) -> dict:
        """Inspect one live ParticleSystem control plane without particle readback."""

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

    def _control_emitter(
        operation: str,
        method_name: str,
        object_id: int,
        emitter_index: int,
        ordinal: int,
    ) -> dict:
        def _control():
            obj = find_game_object(object_id)
            component = _find_particle_system(obj, int(ordinal))
            if component is None:
                raise FileNotFoundError(
                    f"ParticleSystem {ordinal} was not found on GameObject {object_id}."
                )
            accepted = bool(getattr(component, method_name)(int(emitter_index)))
            return {
                "object_id": int(obj.id),
                "object_name": str(obj.name),
                "emitter_index": int(emitter_index),
                "operation": operation,
                "accepted": accepted,
                "runtime": component.runtime_diagnostics(),
            }

        return main_thread(
            f"particle_system_{operation}_emitter",
            _control,
            arguments={
                "object_id": object_id,
                "emitter_index": emitter_index,
                "ordinal": ordinal,
            },
        )

    @mcp.tool(name="particle_system_start_emitter")
    def particle_system_start_emitter(
        object_id: int, emitter_index: int, ordinal: int = 0
    ) -> dict:
        """Start one emitter; invalid indices are harmless no-ops."""
        return _control_emitter(
            "start", "start_emitter", object_id, emitter_index, ordinal
        )

    @mcp.tool(name="particle_system_pause_emitter")
    def particle_system_pause_emitter(
        object_id: int, emitter_index: int, ordinal: int = 0
    ) -> dict:
        """Pause one emitter; invalid indices are harmless no-ops."""
        return _control_emitter(
            "pause", "pause_emitter", object_id, emitter_index, ordinal
        )

    @mcp.tool(name="particle_system_terminate_emitter")
    def particle_system_terminate_emitter(
        object_id: int, emitter_index: int, ordinal: int = 0
    ) -> dict:
        """Terminate and reset one emitter; invalid indices are harmless no-ops."""
        return _control_emitter(
            "terminate", "terminate_emitter", object_id, emitter_index, ordinal
        )

    @mcp.tool(name="particle_system_restart_emitter")
    def particle_system_restart_emitter(
        object_id: int, emitter_index: int, ordinal: int = 0
    ) -> dict:
        """Restart one emitter; invalid indices are harmless no-ops."""
        return _control_emitter(
            "restart", "restart", object_id, emitter_index, ordinal
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


def _portable_snapshot(
    snapshot: dict,
    project_path: str,
    *,
    include_registered_types: bool = False,
) -> dict:
    result = dict(snapshot)
    if not include_registered_types:
        result.pop("registered_types", None)
    file_path = str(result.get("file_path") or "")
    if file_path:
        result["file_path"] = relative_path(file_path, project_path)
    return result


def _register_authoring_metadata() -> None:
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
        "particle_graph_list_node_types",
        summary="Search a compact, paged catalog of node types for the selected emitter.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "nodes", "search"],
        aliases=["list particle nodes", "search particle nodes", "查找粒子节点"],
        preconditions=[
            "A ParticleGraph must be open and the intended emitter must be selected."
        ],
        side_effects=[],
        recovery=[
            "Use particle_graph_select_emitter before searching emitter-specific event nodes."
        ],
        next_suggested_tools=["particle_graph_add_node"],
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
        "particle_graph_patch_emitter_settings",
        summary="Patch selected emitter settings through the strict live editor document.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "emitter", "settings", "patch"],
        aliases=["patch particle emitter", "修改粒子发射器参数"],
        preconditions=["A .particlegraph asset must be open."],
        side_effects=["Records one Undo transaction, marks the document dirty, and republishes the draft."],
        recovery=["Use only field names returned in the emitter settings snapshot."],
        next_suggested_tools=["editor_save_document", "particle_graph_inspect_editor"],
    )
    register_tool_metadata(
        "particle_graph_set_emitter_lifecycle",
        summary="Set Enabled and Play On Start for one ParticleGraph emitter.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "emitter", "lifecycle"],
        aliases=["set particle emitter lifecycle", "设置粒子发射器生命周期"],
        preconditions=["The emitter stable ID must exist in the open .particlegraph."],
        side_effects=["Records one Undo transaction, marks the document dirty, and republishes the draft."],
        recovery=["Use booleans for enabled and play_on_start."],
        next_suggested_tools=["editor_save_document", "particle_graph_inspect_editor"],
    )
    register_tool_metadata(
        "particle_graph_add_data_interface",
        summary="Add a typed Data Interface to a ParticleGraph emitter.",
        category="assets/particle_graph",
        tags=["particle", "graph", "data interface", "sdf", "vector field"],
        aliases=["add particle data interface", "添加粒子数据接口"],
        preconditions=[
            "kind must be sdf_volume, vector_field, or point_cache.",
            "The emitter stable ID must exist in the open ParticleGraph.",
        ],
        side_effects=["Records Undo, marks the document dirty, and republishes the draft."],
        recovery=["Inspect emitter data_interfaces and retry with a current stable ID."],
        next_suggested_tools=["particle_graph_set_data_interface_asset"],
    )
    register_tool_metadata(
        "particle_graph_set_data_interface_asset",
        summary="Bind an imported source asset to a typed ParticleGraph Data Interface.",
        category="assets/particle_graph",
        tags=["particle", "graph", "data interface", "asset"],
        aliases=["bind particle data asset", "绑定粒子数据资产"],
        preconditions=[
            "SDF volumes require .inxsdf, vector fields require .inxvfield, and point caches require .pointcache.",
            "The source asset must already be imported and have a GUID.",
        ],
        side_effects=["Records Undo, marks the document dirty, and republishes the draft."],
        recovery=["Import the matching source asset, then retry with its Assets-relative path."],
        next_suggested_tools=["particle_graph_patch_data_interface"],
    )
    register_tool_metadata(
        "particle_graph_patch_data_interface",
        summary="Patch editable transform and sampling fields on one Data Interface.",
        category="assets/particle_graph",
        tags=["particle", "graph", "data interface", "settings"],
        aliases=["edit particle data interface", "修改粒子数据接口"],
        preconditions=["Use only fields returned by particle_graph_inspect_editor."],
        side_effects=["Records Undo, marks the document dirty, and republishes the draft."],
        recovery=["Use particle_graph_set_data_interface_asset for resource references."],
        next_suggested_tools=["particle_graph_set_node_property", "editor_save_document"],
    )
    register_tool_metadata(
        "particle_graph_remove_data_interface",
        summary="Remove an unreferenced Data Interface from one ParticleGraph emitter.",
        category="assets/particle_graph",
        tags=["particle", "graph", "data interface", "remove"],
        aliases=["remove particle data interface", "移除粒子数据接口"],
        preconditions=["No graph node may still reference the interface stable ID."],
        side_effects=["Records Undo, marks the document dirty, and republishes the draft."],
        recovery=["Clear or redirect referencing node properties before removal."],
        next_suggested_tools=["particle_graph_inspect_editor", "editor_save_document"],
    )
    register_tool_metadata(
        "particle_graph_remove_emitter",
        summary="Remove one emitter and cascade every event route that references it.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "emitter", "remove"],
        aliases=["remove particle emitter", "移除粒子发射器"],
        preconditions=["The graph must keep at least one emitter."],
        side_effects=["Records Undo and removes dependent route-private nodes."],
        recovery=["Inspect emitters and retry with a current stable ID."],
        next_suggested_tools=["particle_graph_inspect_editor", "editor_save_document"],
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
        "particle_graph_add_event_output",
        summary="Add a route-specific Event Output to the correct source emitter and stage.",
        category="assets/particle_graph",
        tags=["particle", "graph", "event", "output", "authoring"],
        aliases=["add event output", "添加粒子事件输出"],
        preconditions=["The route stable ID must appear in particle_graph_inspect_editor."],
        side_effects=["Selects the source emitter and records one node-add Undo transaction."],
        recovery=["Create or inspect the event route, then retry with its stable ID."],
        next_suggested_tools=["particle_graph_connect_stream", "editor_save_document"],
    )
    register_tool_metadata(
        "particle_graph_add_event_payload",
        summary="Add a route-specific Event Payload to the correct target Init graph.",
        category="assets/particle_graph",
        tags=["particle", "graph", "event", "payload", "authoring"],
        aliases=["add event payload", "添加粒子事件载荷"],
        preconditions=["The route stable ID must appear in particle_graph_inspect_editor."],
        side_effects=["Selects the target emitter and records one node-add Undo transaction."],
        recovery=["Create or inspect the event route, then retry with its stable ID."],
        next_suggested_tools=["particle_graph_connect_value", "editor_save_document"],
    )
    register_tool_metadata(
        "particle_graph_update_event_type",
        summary="Edit an event schema in place while preserving stable event/field identities.",
        category="assets/particle_graph",
        tags=["particle", "graph", "event", "schema", "edit", "hot reload"],
        aliases=["edit particle event", "修改粒子事件类型"],
        preconditions=[
            "Use the complete current field list returned by particle_graph_inspect_editor.",
            "Every field object must include stable_id, name, type, and default.",
        ],
        side_effects=[
            "Records Undo and preserves unaffected route nodes and links.",
            "Removed fields or type changes disconnect only links using those payload ports.",
        ],
        recovery=["Inspect the current event schema and retry with its stable IDs."],
        next_suggested_tools=["editor_save_document", "particle_graph_inspect_editor"],
    )
    register_tool_metadata(
        "particle_graph_update_event_route",
        summary="Edit an event route in place while preserving its stable route identity.",
        category="assets/particle_graph",
        tags=["particle", "graph", "event", "route", "edit", "hot reload"],
        aliases=["edit particle event route", "修改粒子事件路由"],
        preconditions=["Route, event, and emitter stable IDs must be current."],
        side_effects=[
            "A spawn-count-only edit preserves route-private nodes and links.",
            "Changing type/source/stage/target removes route-private nodes whose context is no longer valid.",
        ],
        recovery=["Inspect the route and re-add its context-specific nodes after endpoint changes."],
        next_suggested_tools=["particle_graph_add_node", "editor_save_document"],
    )
    register_tool_metadata(
        "particle_graph_remove_event_route",
        summary="Remove a typed event route and its route-private nodes through the editor.",
        category="assets/particle_graph",
        tags=["particle", "graph", "event", "route", "remove"],
        aliases=["remove particle event route", "移除粒子事件路由"],
        preconditions=["The route stable ID must appear in particle_graph_inspect_editor."],
        side_effects=["Records one Undo transaction and removes derived Output/Payload nodes."],
        recovery=["Inspect event_routes and retry with a current stable ID."],
        next_suggested_tools=["particle_graph_inspect_editor", "editor_save_document"],
    )
    register_tool_metadata(
        "particle_graph_remove_event_type",
        summary="Remove a typed event schema and cascade its routes through the editor.",
        category="assets/particle_graph",
        tags=["particle", "graph", "event", "schema", "remove"],
        aliases=["remove particle event type", "移除粒子事件类型"],
        preconditions=["The event type stable ID must appear in particle_graph_inspect_editor."],
        side_effects=["Records one Undo transaction and cascades dependent routes and nodes."],
        recovery=["Inspect event_types and retry with a current stable ID."],
        next_suggested_tools=["particle_graph_inspect_editor", "editor_save_document"],
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
        "particle_graph_discard_editor",
        summary="Explicitly discard the visible ParticleGraph document before opening another asset.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "discard", "unsaved"],
        aliases=["discard particle graph", "放弃粒子图修改"],
        preconditions=["A ParticleGraph editor document must be visible."],
        side_effects=["Restores the saved asset, or clears an unsaved in-memory graph."],
        recovery=["Inspect the editor after discard before opening another asset."],
        next_suggested_tools=["particle_graph_open_asset", "particle_graph_inspect_editor"],
    )


def _register_runtime_metadata() -> None:
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
    for tool_name, verb in (
        ("particle_system_start_emitter", "start"),
        ("particle_system_pause_emitter", "pause"),
        ("particle_system_terminate_emitter", "terminate and reset"),
        ("particle_system_restart_emitter", "restart"),
    ):
        register_tool_metadata(
            tool_name,
            summary=f"{verb.capitalize()} one ParticleSystem emitter by index.",
            category="runtime/particles",
            tags=["particle", "runtime", "emitter", "control"],
            aliases=[f"{verb} particle emitter"],
            preconditions=["object_id must own a live ParticleSystem component."],
            side_effects=[f"Attempts to {verb} only the requested emitter."],
            recovery=["An invalid emitter index is a harmless no-op with accepted=false."],
            next_suggested_tools=["particle_system_inspect_runtime"],
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
