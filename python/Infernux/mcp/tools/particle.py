"""ParticleGraph editor authoring tools for MCP developer sessions."""

from __future__ import annotations

import os

from Infernux.engine.path_utils import relative_path, same_path
from Infernux.mcp.tools.common import (
    find_game_object,
    get_asset_database,
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
        """Set a serialized Mesh or shader Texture2D input on a graph node."""

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

    @mcp.tool(name="particle_graph_add_parameter")
    def particle_graph_add_parameter(
        name: str,
        value_type: str = "f32",
        default=None,
        exposed: bool = True,
    ) -> dict:
        """Add one typed Blackboard parameter through the visible editor."""

        def _add():
            panel = _require_particle_graph_panel()
            parameter = panel.add_authoring_parameter(
                name,
                value_type,
                default,
                exposed=bool(exposed),
            )
            return {
                "parameter": parameter,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_add_parameter",
            _add,
            arguments={
                "name": name,
                "value_type": value_type,
                "default": default,
                "exposed": exposed,
            },
        )

    @mcp.tool(name="particle_graph_update_parameter")
    def particle_graph_update_parameter(parameter_id: str, values: dict) -> dict:
        """Patch a Blackboard parameter while preserving its stable identity."""

        def _update():
            panel = _require_particle_graph_panel()
            parameter = panel.update_authoring_parameter(parameter_id, values)
            return {
                "parameter": parameter,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_update_parameter",
            _update,
            arguments={"parameter_id": parameter_id, "values": values},
        )

    @mcp.tool(name="particle_graph_remove_parameter")
    def particle_graph_remove_parameter(parameter_id: str) -> dict:
        """Remove a Blackboard parameter and its dependent Parameter nodes."""

        def _remove():
            panel = _require_particle_graph_panel()
            parameter = panel.remove_authoring_parameter(parameter_id)
            return {
                "parameter": parameter,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_remove_parameter",
            _remove,
            arguments={"parameter_id": parameter_id},
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

    @mcp.tool(name="particle_graph_connect_exec")
    def particle_graph_connect_exec(
        source_node_uid: str,
        target_node_uid: str,
        source_port: str = "out",
        target_port: str = "in",
    ) -> dict:
        """Connect two named Exec ports in the same particle lifecycle flow."""

        def _connect():
            panel = _require_particle_graph_panel()
            result = panel.connect_exec(
                source_node_uid,
                target_node_uid,
                source_port,
                target_port,
            )
            return {
                **result,
                "source_node_uid": str(source_node_uid),
                "target_node_uid": str(target_node_uid),
                "source_port": str(source_port),
                "target_port": str(target_port),
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_connect_exec",
            _connect,
            arguments={
                "source_node_uid": source_node_uid,
                "target_node_uid": target_node_uid,
                "source_port": source_port,
                "target_port": target_port,
            },
        )

    @mcp.tool(name="particle_graph_disconnect_link")
    def particle_graph_disconnect_link(link_uid: str) -> dict:
        """Disconnect one Exec or value link in the live ParticleGraph document."""

        def _disconnect():
            panel = _require_particle_graph_panel()
            result = panel.disconnect_link(link_uid)
            return {
                **result,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_disconnect_link",
            _disconnect,
            arguments={"link_uid": link_uid},
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
        """Remove an emitter and its graph-owned event flows."""

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
        queue_capacity: int,
        fields: list[dict],
    ) -> dict:
        """Add a typed event schema through the live ParticleGraph document."""

        def _add():
            panel = _require_particle_graph_panel()
            event_type = panel.add_event_type(name, queue_capacity, fields)
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
                "queue_capacity": queue_capacity,
                "fields": fields,
            },
        )

    @mcp.tool(name="particle_graph_implement_event")
    def particle_graph_implement_event(event_type_id: str) -> dict:
        """Create a new empty Active Event flow on the selected emitter."""

        def _implement():
            panel = _require_particle_graph_panel()
            result = panel.add_authoring_event_flow(event_type_id)
            return {
                **result,
                "editor": _portable_snapshot(
                    panel.authoring_snapshot(), project_path
                ),
            }

        return main_thread(
            "particle_graph_implement_event",
            _implement,
            arguments={"event_type_id": event_type_id},
        )

    @mcp.tool(name="particle_graph_update_event_type")
    def particle_graph_update_event_type(
        event_type_id: str,
        name: str,
        queue_capacity: int,
        fields: list[dict],
    ) -> dict:
        """Update a typed event schema without changing its stable identity."""

        def _update():
            panel = _require_particle_graph_panel()
            event_type = panel.update_event_type(
                event_type_id, name, queue_capacity, fields
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
                "queue_capacity": queue_capacity,
                "fields": fields,
            },
        )

    @mcp.tool(name="particle_graph_remove_event_type")
    def particle_graph_remove_event_type(event_type_id: str) -> dict:
        """Remove one event schema and clear references without deleting nodes."""

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
        """Connect the Rendering root Exec output to exactly one output node."""

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

    @mcp.tool(name="particle_system_get_parameter")
    def particle_system_get_parameter(
        object_id: int, name: str, ordinal: int = 0
    ) -> dict:
        """Read one exposed ParticleGraph parameter from a live ParticleSystem."""

        def _get():
            obj = find_game_object(object_id)
            component = _require_particle_system(obj, int(ordinal))
            return {
                "object_id": int(obj.id),
                "object_name": str(obj.name),
                "name": str(name),
                "value": component.get_parameter(str(name)),
            }

        return main_thread(
            "particle_system_get_parameter",
            _get,
            arguments={"object_id": object_id, "name": name, "ordinal": ordinal},
        )

    @mcp.tool(name="particle_system_set_parameter")
    def particle_system_set_parameter(
        object_id: int, name: str, value, ordinal: int = 0
    ) -> dict:
        """Set one exposed typed ParticleGraph parameter."""

        def _set():
            obj = find_game_object(object_id)
            component = _require_particle_system(obj, int(ordinal))
            component.set_parameter(str(name), value)
            return {
                "object_id": int(obj.id),
                "object_name": str(obj.name),
                "name": str(name),
                "value": component.get_parameter(str(name)),
                "runtime": component.runtime_diagnostics(),
            }

        return main_thread(
            "particle_system_set_parameter",
            _set,
            arguments={
                "object_id": object_id,
                "name": name,
                "value": value,
                "ordinal": ordinal,
            },
        )

    @mcp.tool(name="particle_system_set_emitter_options")
    def particle_system_set_emitter_options(
        object_id: int,
        emitter: str,
        enabled: bool,
        play_on_start: bool,
        ordinal: int = 0,
    ) -> dict:
        """Set one emitter's scene-instance playback policy."""

        def _set():
            obj = find_game_object(object_id)
            component = _require_particle_system(obj, int(ordinal))
            changed = component.set_emitter_options(
                str(emitter),
                enabled=enabled,
                play_on_start=play_on_start,
            )
            return {
                "object_id": int(obj.id),
                "object_name": str(obj.name),
                "emitter": str(emitter),
                "changed": bool(changed),
                "emitters": component.emitter_instance_schema(),
            }

        return main_thread(
            "particle_system_set_emitter_options",
            _set,
            arguments={
                "object_id": object_id,
                "emitter": emitter,
                "enabled": enabled,
                "play_on_start": play_on_start,
                "ordinal": ordinal,
            },
        )

    @mcp.tool(name="particle_system_list_events")
    def particle_system_list_events(
        object_id: int, ordinal: int = 0
    ) -> dict:
        """List typed gameplay events accepted by a live ParticleSystem."""

        def _list():
            obj = find_game_object(object_id)
            component = _require_particle_system(obj, int(ordinal))
            return {
                "object_id": int(obj.id),
                "object_name": str(obj.name),
                "events": component.runtime_event_schema(),
            }

        return main_thread(
            "particle_system_list_events",
            _list,
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

    @mcp.tool(name="particle_system_seek")
    def particle_system_seek(
        object_id: int,
        time_seconds: float,
        emitter_index: int = -1,
        ordinal: int = 0,
    ) -> dict:
        """Seek all emitters, or one indexed emitter, using deterministic GPU replay."""

        def _seek():
            obj = find_game_object(object_id)
            component = _find_particle_system(obj, int(ordinal))
            if component is None:
                raise FileNotFoundError(
                    f"ParticleSystem {ordinal} was not found on GameObject {object_id}."
                )
            target = None if int(emitter_index) == -1 else int(emitter_index)
            accepted = bool(component.seek(float(time_seconds), target))
            return {
                "object_id": int(obj.id),
                "object_name": str(obj.name),
                "emitter_index": target,
                "time_seconds": float(time_seconds),
                "accepted": accepted,
                "runtime": component.runtime_diagnostics(),
            }

        return main_thread(
            "particle_system_seek",
            _seek,
            arguments={
                "object_id": object_id,
                "time_seconds": time_seconds,
                "emitter_index": emitter_index,
                "ordinal": ordinal,
            },
        )

    @mcp.tool(name="particle_system_request_gpu_diagnostics")
    def particle_system_request_gpu_diagnostics(
        object_id: int,
        ordinal: int = 0,
        sample_frames: int = 60,
        state_sample_count: int = 0,
    ) -> dict:
        """Request an isolated GPU snapshot and optional bounded live-state samples."""

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
                "request_id": component.request_gpu_diagnostics(
                    int(sample_frames), int(state_sample_count)
                ),
                "status": "pending",
            }

        return main_thread(
            "particle_system_request_gpu_diagnostics",
            _request,
            arguments={
                "object_id": object_id,
                "ordinal": ordinal,
                "sample_frames": sample_frames,
                "state_sample_count": state_sample_count,
            },
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

    @mcp.tool(name="particle_system_request_gpu_view_diagnostics")
    def particle_system_request_gpu_view_diagnostics(
        object_id: int,
        view: str,
        ordinal: int = 0,
        camera_component_id: int = 0,
    ) -> dict:
        """Request one asynchronous Scene/Game GPU cull-and-draw snapshot."""

        def _request():
            obj = find_game_object(object_id)
            component = _find_particle_system(obj, int(ordinal))
            if component is None:
                raise FileNotFoundError(
                    f"ParticleSystem {ordinal} was not found on GameObject {object_id}."
                )
            normalized_view = str(view).strip().lower()
            return {
                "object_id": int(obj.id),
                "object_name": str(obj.name),
                "view": normalized_view,
                "request_id": component.request_gpu_view_diagnostics(
                    normalized_view, int(camera_component_id)
                ),
                "camera_component_id": int(camera_component_id),
                "status": "pending",
            }

        return main_thread(
            "particle_system_request_gpu_view_diagnostics",
            _request,
            arguments={
                "object_id": object_id,
                "view": view,
                "ordinal": ordinal,
                "camera_component_id": camera_component_id,
            },
        )

    @mcp.tool(name="particle_system_poll_gpu_view_diagnostics")
    def particle_system_poll_gpu_view_diagnostics(
        object_id: int,
        view: str,
        request_id: int,
        ordinal: int = 0,
        camera_component_id: int = 0,
    ) -> dict:
        """Poll a requested per-view GPU particle cull-and-draw snapshot."""

        def _poll():
            obj = find_game_object(object_id)
            component = _find_particle_system(obj, int(ordinal))
            if component is None:
                raise FileNotFoundError(
                    f"ParticleSystem {ordinal} was not found on GameObject {object_id}."
                )
            normalized_view = str(view).strip().lower()
            return {
                "object_id": int(obj.id),
                "object_name": str(obj.name),
                "diagnostics": component.poll_gpu_view_diagnostics(
                    normalized_view, int(request_id), int(camera_component_id)
                ),
            }

        return main_thread(
            "particle_system_poll_gpu_view_diagnostics",
            _poll,
            arguments={
                "object_id": object_id,
                "view": view,
                "request_id": request_id,
                "ordinal": ordinal,
                "camera_component_id": camera_component_id,
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


def _require_particle_system(obj, ordinal: int):
    component = _find_particle_system(obj, ordinal)
    if component is None:
        raise FileNotFoundError(
            f"ParticleSystem {ordinal} was not found on GameObject {obj.id}."
        )
    return component


def _notify_particle_component_modified() -> None:
    from Infernux.engine.ui._inspector_undo import _notify_scene_modified

    _notify_scene_modified()


def _open_particle_graph_panel(file_path: str):
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.engine.ui.window_manager import WindowManager

    manager = WindowManager.instance()
    if manager is None:
        raise RuntimeError("WindowManager is not initialized")

    panel = manager.get_window_instance("particle_graph_editor")
    if isinstance(panel, ParticleGraphEditorPanel) and bool(panel.is_open):
        state = panel.authoring_document_state()
        current_path = str(state.get("file_path") or "")
        if current_path and same_path(current_path, file_path):
            _focus_particle_graph_panel(manager)
            return panel
        if bool(state.get("dirty")):
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
            "particle_graph_connect_exec",
        ],
    )
    register_tool_metadata(
        "particle_graph_add_parameter",
        summary="Add a typed field to the visible ParticleGraph Blackboard.",
        category="assets/particle_graph",
        tags=["particle", "graph", "parameter", "blackboard", "authoring"],
        aliases=["add particle parameter", "添加粒子参数"],
        preconditions=[
            "A .particlegraph asset must be open.",
            "value_type must be bool, i32, u32, f32, vec2, vec3, vec4, color, curve, gradient, texture2d, or mesh.",
        ],
        side_effects=[
            "Records Undo, selects the new Blackboard field, and republishes the live draft."
        ],
        recovery=["Inspect the Blackboard and retry with a unique non-empty name."],
        next_suggested_tools=[
            "particle_graph_add_node",
            "particle_graph_update_parameter",
        ],
    )
    register_tool_metadata(
        "particle_graph_update_parameter",
        summary="Update a ParticleGraph Blackboard field while preserving its stable ID.",
        category="assets/particle_graph",
        tags=["particle", "graph", "parameter", "blackboard", "edit"],
        aliases=["edit particle parameter", "修改粒子参数"],
        preconditions=[
            "parameter_id must identify a current Blackboard field.",
            "values may contain name, type, default, exposed, category, or tooltip.",
        ],
        side_effects=[
            "Records Undo and disconnects incompatible value links after a type change."
        ],
        recovery=["Inspect the editor and retry with the field's stable ID."],
        next_suggested_tools=["particle_graph_inspect_editor", "editor_save_document"],
    )
    register_tool_metadata(
        "particle_graph_remove_parameter",
        summary="Remove a ParticleGraph Blackboard field and dependent Parameter nodes.",
        category="assets/particle_graph",
        tags=["particle", "graph", "parameter", "blackboard", "remove"],
        aliases=["remove particle parameter", "移除粒子参数"],
        preconditions=["parameter_id must identify a current Blackboard field."],
        side_effects=[
            "Records Undo and removes every Parameter node referencing the field."
        ],
        recovery=["Inspect the editor and retry with a current parameter stable ID."],
        next_suggested_tools=["particle_graph_inspect_editor", "editor_save_document"],
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
        next_suggested_tools=["particle_graph_connect_exec", "editor_save_document"],
    )
    register_tool_metadata(
        "particle_graph_connect_exec",
        summary="Connect two named Exec ports in one ParticleGraph lifecycle flow.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "exec"],
        aliases=["connect particle nodes", "连接粒子节点"],
        preconditions=[
            "Both node UIDs must exist in the same stage.",
            "The named source and target ports must both be Exec ports.",
        ],
        side_effects=["Records Undo, marks the panel dirty, and republishes the live draft."],
        recovery=["Inspect existing links and endpoint stages before retrying."],
        next_suggested_tools=["editor_save_document", "particle_graph_inspect_editor"],
    )
    register_tool_metadata(
        "particle_graph_disconnect_link",
        summary="Disconnect one Exec or value link through the live ParticleGraph authoring model.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "link", "disconnect"],
        aliases=["disconnect particle nodes", "断开粒子节点"],
        preconditions=[
            "The link UID must identify an existing link in the open ParticleGraph."
        ],
        side_effects=[
            "Records Undo, marks the panel dirty, and republishes the live draft."
        ],
        recovery=[
            "Inspect the current links and retry with an existing link UID."
        ],
        next_suggested_tools=[
            "particle_graph_connect_exec",
            "editor_save_document",
        ],
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
        next_suggested_tools=["particle_graph_implement_event", "editor_save_document"],
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
        "particle_graph_add_data_interface",
        summary="Add a typed Data Interface to a ParticleGraph emitter.",
        category="assets/particle_graph",
        tags=["particle", "graph", "data interface", "sdf", "vector field"],
        aliases=["add particle data interface", "添加粒子数据接口"],
        preconditions=[
            "kind must be sdf_volume or vector_field.",
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
            "SDF volumes require .inxsdf and vector fields require .inxvfield.",
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
        summary="Remove one emitter and its private lifecycle/event flows.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "emitter", "remove"],
        aliases=["remove particle emitter", "移除粒子发射器"],
        preconditions=["The graph must keep at least one emitter."],
        side_effects=["Records Undo and removes the emitter-owned event implementations."],
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
        next_suggested_tools=["particle_graph_implement_event"],
    )
    register_tool_metadata(
        "particle_graph_implement_event",
        summary="Create a new empty Active Event flow on the selected emitter.",
        category="assets/particle_graph",
        tags=["particle", "graph", "event", "flow"],
        aliases=["implement particle event", "实现粒子事件流"],
        preconditions=["The event stable ID must exist and an emitter must be selected."],
        side_effects=["Records Undo and always creates one new no-input Active Event root."],
        recovery=["Inspect event_types and the selected emitter before retrying."],
        next_suggested_tools=["particle_graph_add_node", "particle_graph_inspect_editor"],
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
            "Records Undo and preserves unaffected Event roots and Trigger Event nodes.",
            "Removed fields or type changes disconnect only links using those payload ports.",
        ],
        recovery=["Inspect the current event schema and retry with its stable IDs."],
        next_suggested_tools=["editor_save_document", "particle_graph_inspect_editor"],
    )
    register_tool_metadata(
        "particle_graph_remove_event_type",
        summary="Remove a typed event schema and clear dependent event selections.",
        category="assets/particle_graph",
        tags=["particle", "graph", "event", "schema", "remove"],
        aliases=["remove particle event type", "移除粒子事件类型"],
        preconditions=["The event type stable ID must appear in particle_graph_inspect_editor."],
        side_effects=["Records one Undo transaction; dependent Active/Trigger nodes remain and select None."],
        recovery=["Inspect event_types and retry with a current stable ID."],
        next_suggested_tools=["particle_graph_inspect_editor", "editor_save_document"],
    )
    register_tool_metadata(
        "particle_graph_set_node_asset",
        summary="Set a Mesh or shader Texture2D input through the live ParticleGraph editor model.",
        category="assets/particle_graph",
        tags=["particle", "graph", "editor", "mesh", "texture", "shader"],
        aliases=["set particle mesh input", "set particle texture", "设置粒子网格输入", "设置粒子贴图"],
        preconditions=[
            "A .particlegraph asset must be open.",
            "asset_path must identify an imported asset inside Assets/.",
        ],
        side_effects=[
            "Updates the live editor document, records Undo, marks the panel dirty, and republishes its in-memory draft."
        ],
        recovery=[
            "Call particle_graph_inspect_editor to verify node_uid and the Mesh or shader Texture2D input ID."
        ],
        next_suggested_tools=[
            "particle_graph_inspect_editor",
            "particle_graph_set_rendering_output",
            "editor_save_document",
        ],
    )
    register_tool_metadata(
        "particle_graph_set_rendering_output",
        summary="Route one emitter's Rendering Exec output to a selected output node.",
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
    register_tool_metadata(
        "particle_system_get_parameter",
        summary="Read one exposed ParticleGraph parameter from a live ParticleSystem.",
        category="runtime/particles",
        tags=["particle", "runtime", "parameter", "read"],
        aliases=["get particle parameter", "读取粒子参数"],
        preconditions=[
            "object_id must own a live ParticleSystem component.",
            "name must be an exposed parameter name or stable ID.",
        ],
        recovery=["Inspect the graph Blackboard and component assignment before retrying."],
        next_suggested_tools=["particle_system_set_parameter"],
    )
    register_tool_metadata(
        "particle_system_set_parameter",
        summary="Set one exposed ParticleGraph value, Texture2D, or Mesh parameter.",
        category="runtime/particles",
        tags=["particle", "runtime", "parameter", "write", "gpu"],
        aliases=["set particle parameter", "设置粒子参数"],
        preconditions=[
            "object_id must own a live ParticleSystem component.",
            "The value shape and type must match the exposed parameter exactly.",
            "Texture2D and Mesh values use an AssetReference object with guid and path_hint.",
        ],
        side_effects=[
            "Serializes the override; numeric values upload parameter words, while Texture2D and Mesh values rebuild only the affected GPU resource binding and preserve compatible resident state."
        ],
        recovery=["Read the parameter first and retry with a matching typed value."],
        next_suggested_tools=["particle_system_get_parameter", "capture_request"],
    )
    register_tool_metadata(
        "particle_system_set_emitter_options",
        summary="Set Enabled and Play On Start on one ParticleSystem instance emitter.",
        category="runtime/particles",
        tags=["particle", "runtime", "emitter", "lifecycle", "scene"],
        aliases=["set particle emitter options", "设置粒子发射器实例"],
        preconditions=[
            "object_id must own a ParticleSystem component.",
            "emitter must be an authored emitter name or stable ID.",
        ],
        side_effects=[
            "Records the scene-instance override without modifying the ParticleGraph asset."
        ],
        recovery=["Inspect the ParticleSystem runtime and retry with a listed emitter."],
        next_suggested_tools=["particle_system_inspect_runtime", "capture_request"],
    )
    register_tool_metadata(
        "particle_system_list_events",
        summary="List typed per-particle events defined by one live GPU ParticleSystem.",
        category="runtime/particles",
        tags=["particle", "runtime", "event", "schema", "gpu"],
        aliases=["list particle events", "列出粒子事件"],
        preconditions=["object_id must own a live ParticleSystem with defined events."],
        recovery=["Open the Particle Graph Event page and verify its saved definitions."],
        next_suggested_tools=["particle_graph_inspect_editor"],
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
        "particle_system_seek",
        summary="Deterministically seek GPU particles from time zero with fixed-step replay.",
        category="runtime/particles",
        tags=["particle", "runtime", "seek", "deterministic", "gpu"],
        aliases=["seek particle system", "定位粒子时间"],
        preconditions=[
            "object_id must own a live ParticleSystem component.",
            "time_seconds must be finite, non-negative, and within the bounded preroll budget.",
            "emitter_index=-1 targets all enabled emitters.",
        ],
        side_effects=[
            "Resets the selected GPU emitter state and replays fixed simulation steps while preserving play/pause state."
        ],
        recovery=["Inspect runtime diagnostics and retry with a shorter target time."],
        next_suggested_tools=[
            "particle_system_inspect_runtime",
            "particle_system_request_gpu_diagnostics",
            "capture_request",
        ],
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
    register_tool_metadata(
        "particle_system_request_gpu_view_diagnostics",
        summary="Request one asynchronous Scene or Game GPU particle cull-and-draw snapshot.",
        category="runtime/particles",
        tags=["particle", "gpu", "view", "culling", "diagnostics", "readback"],
        aliases=["read particle view counts", "读取粒子视图计数"],
        preconditions=[
            "object_id must own a live GPU ParticleSystem component.",
            "view must be scene or game and that view must continue rendering.",
        ],
        side_effects=[
            "Records one explicit stats/indirect-buffer copy after the selected view's next submitted frame."
        ],
        recovery=["Keep the selected view rendering and poll the returned request_id."],
        next_suggested_tools=["particle_system_poll_gpu_view_diagnostics"],
    )
    register_tool_metadata(
        "particle_system_poll_gpu_view_diagnostics",
        summary="Poll a requested per-view GPU particle cull-and-draw snapshot without stalling.",
        category="runtime/particles",
        tags=["particle", "gpu", "view", "culling", "diagnostics", "poll"],
        aliases=["poll particle view counts", "轮询粒子视图计数"],
        preconditions=[
            "request_id and view must come from the same ParticleSystem request."
        ],
        recovery=["If status is pending, advance the selected view and poll again."],
        next_suggested_tools=["runtime_read_errors", "capture_request"],
    )
