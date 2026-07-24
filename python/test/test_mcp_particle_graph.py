from __future__ import annotations

import pytest

from Infernux.mcp.tools import assets as assets_module
from Infernux.mcp.tools import particle as module


class _FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        name = str(kwargs.get("name") or (args[0] if args else ""))

        def _register(fn):
            self.tools[name] = fn
            return fn

        return _register


class _Panel:
    def __init__(self, graph_path):
        self.graph_path = str(graph_path)
        self.calls = []

    def authoring_snapshot(self):
        return {
            "file_path": self.graph_path,
            "dirty": True,
            "emitter_index": 0,
            "selected_node_uid": "rendering::mesh-output",
            "nodes": [],
        }

    def set_node_asset_reference(self, node_uid, property_name, file_path):
        self.calls.append((node_uid, property_name, file_path))
        return {"guid": "mesh-guid", "path_hint": "Assets/Models/Shard.obj"}

    def add_authoring_node(self, stage, type_id, x, y):
        self.calls.append(("add", stage, type_id, x, y))
        return {
            "uid": f"{stage}::new-node",
            "type_id": type_id,
            "stage": stage,
            "properties": {},
        }

    def set_node_property(self, node_uid, property_name, value):
        self.calls.append(("property", node_uid, property_name, value))
        return {
            "node_uid": node_uid,
            "property_name": property_name,
            "value": value,
            "changed": True,
        }

    def connect_stream(self, source_node_uid, target_node_uid):
        self.calls.append(("connect", source_node_uid, target_node_uid))
        return {"link_uid": "update::new-link", "changed": True}

    def connect_value(self, source_node_uid, source_port, target_node_uid, target_port):
        self.calls.append(
            ("value", source_node_uid, source_port, target_node_uid, target_port)
        )
        return {"link_uid": "init::value-link", "changed": True}

    def select_authoring_emitter(self, emitter_id):
        self.calls.append(("select-emitter", emitter_id))
        return {"stable_id": emitter_id, "index": 1}

    def add_authoring_emitter(self, name):
        self.calls.append(("add-emitter", name))
        return {"stable_id": "target", "name": name, "settings": {"spawn_rate": 10.0}}

    def set_authoring_emitter_settings(self, emitter_id, settings):
        self.calls.append(("emitter-settings", emitter_id, settings))
        return {"stable_id": emitter_id, "settings": settings, "changed": True}

    def patch_authoring_emitter_settings(self, emitter_id, values):
        self.calls.append(("patch-emitter-settings", emitter_id, values))
        return {"stable_id": emitter_id, "settings": values, "changed": True}

    def remove_authoring_emitter(self, emitter_id):
        self.calls.append(("remove-emitter", emitter_id))
        return {
            "emitter": {"stable_id": emitter_id},
            "removed_route_ids": ["route-id"],
            "changed": True,
        }

    def add_event_type(self, name, capacity_per_step, fields):
        self.calls.append(("event-type", name, capacity_per_step, fields))
        return {"stable_id": "event-id", "name": name}

    def add_event_route(
        self,
        event_type_id,
        source_emitter_id,
        source_stage,
        target_emitter_id,
        spawn_count,
    ):
        self.calls.append(
            (
                "event-route",
                event_type_id,
                source_emitter_id,
                source_stage,
                target_emitter_id,
                spawn_count,
            )
        )
        return {"stable_id": "route-id", "event_type_id": event_type_id}

    def add_event_output_node(self, route_id, x, y):
        self.calls.append(("event-output", route_id, x, y))
        return {
            "uid": "update::event-output",
            "type_id": "event-output-type",
            "stage": "update",
        }

    def add_event_payload_node(self, route_id, x, y):
        self.calls.append(("event-payload", route_id, x, y))
        return {
            "uid": "init::event-payload",
            "type_id": "event-payload-type",
            "stage": "init",
        }

    def update_event_type(self, event_type_id, name, capacity_per_step, fields):
        self.calls.append(
            ("update-event-type", event_type_id, name, capacity_per_step, fields)
        )
        return {
            "stable_id": event_type_id,
            "name": name,
            "capacity_per_step": capacity_per_step,
            "fields": fields,
            "changed": True,
        }

    def update_event_route(
        self,
        route_id,
        event_type_id,
        source_emitter_id,
        source_stage,
        target_emitter_id,
        spawn_count,
    ):
        self.calls.append(
            (
                "update-event-route",
                route_id,
                event_type_id,
                source_emitter_id,
                source_stage,
                target_emitter_id,
                spawn_count,
            )
        )
        return {"stable_id": route_id, "spawn_count": spawn_count, "changed": True}

    def remove_event_route(self, route_id):
        self.calls.append(("remove-event-route", route_id))
        return {"stable_id": route_id, "event_type_id": "event-id"}

    def remove_event_type(self, event_type_id):
        self.calls.append(("remove-event-type", event_type_id))
        return {"stable_id": event_type_id, "name": "Impact"}

    def set_rendering_output(self, node_uid):
        self.calls.append(("output", node_uid))
        return {"node_uid": node_uid, "link_uid": "rendering::mesh-link", "changed": True}

    def reload_from_disk(self):
        self.calls.append(("reload",))
        return True

    def discard_unsaved_changes(self):
        self.calls.append(("discard",))
        return {
            "discarded": True,
            "previous_file_path": "",
            "file_path": "",
            "dirty": False,
        }


def test_particle_graph_mcp_tools_edit_the_live_panel_document(tmp_path, monkeypatch):
    assets = tmp_path / "Assets"
    models = assets / "Models"
    models.mkdir(parents=True)
    mesh_path = models / "Shard.obj"
    mesh_path.write_text("o Shard\nv 0 0 0\n", encoding="utf-8")
    graph_path = assets / "Sparks.particlegraph"
    graph_path.write_text("{}", encoding="utf-8")
    panel = _Panel(graph_path)
    monkeypatch.setattr(module, "_require_particle_graph_panel", lambda: panel)
    monkeypatch.setattr(module, "_open_particle_graph_panel", lambda _path: panel)
    monkeypatch.setattr(
        module,
        "main_thread",
        lambda _operation, callback, **_kwargs: callback(),
    )
    mcp = _FakeMcp()
    module.register_particle_tools(mcp, str(tmp_path))

    opened = mcp.tools["particle_graph_open_asset"]("Assets/Sparks.particlegraph")
    inspected = mcp.tools["particle_graph_inspect_editor"]()
    changed = mcp.tools["particle_graph_set_node_asset"](
        "rendering::mesh-output", "mesh", "Assets/Models/Shard.obj"
    )
    added = mcp.tools["particle_graph_add_node"](
        "update", "particle.update.rotate_orientation", 120.0, 40.0
    )
    property_changed = mcp.tools["particle_graph_set_node_property"](
        "update::new-node", "degrees_per_second", [1.0, 2.0, 3.0]
    )
    connected = mcp.tools["particle_graph_connect_stream"](
        "update::root.update", "update::new-node"
    )
    value_connected = mcp.tools["particle_graph_connect_value"](
        "init::payload", "value", "init::size", "value"
    )
    selected = mcp.tools["particle_graph_select_emitter"]("target")
    emitter = mcp.tools["particle_graph_add_emitter"]("Target")
    emitter_settings = mcp.tools["particle_graph_set_emitter_settings"](
        "target", {"spawn_rate": 0.0}
    )
    patched_settings = mcp.tools["particle_graph_patch_emitter_settings"](
        "target", {"capacity": 8}
    )
    event_type = mcp.tools["particle_graph_add_event_type"](
        "Impact",
        64,
        [
            {
                "name": "Weight",
                "type": {"value_type": "f32", "space": "none"},
                "default": 1.0,
            }
        ],
    )
    event_route = mcp.tools["particle_graph_add_event_route"](
        "event-id", "source", "update", "target", 2
    )
    event_output = mcp.tools["particle_graph_add_event_output"](
        "route-id", 280.0, 220.0
    )
    event_payload = mcp.tools["particle_graph_add_event_payload"](
        "route-id", 160.0, 40.0
    )
    updated_type = mcp.tools["particle_graph_update_event_type"](
        "event-id",
        "Impact Renamed",
        8,
        [
            {
                "stable_id": "field-id",
                "name": "Weight",
                "type": {"value_type": "f32", "space": "none"},
                "default": 2.0,
            }
        ],
    )
    updated_route = mcp.tools["particle_graph_update_event_route"](
        "route-id", "event-id", "source", "update", "target", 7
    )
    removed_route = mcp.tools["particle_graph_remove_event_route"]("route-id")
    removed_type = mcp.tools["particle_graph_remove_event_type"]("event-id")
    removed_emitter = mcp.tools["particle_graph_remove_emitter"]("target")
    routed = mcp.tools["particle_graph_set_rendering_output"](
        "rendering::mesh-output"
    )
    reloaded = mcp.tools["particle_graph_reload_editor"]()
    discarded = mcp.tools["particle_graph_discard_editor"]()

    assert opened["file_path"] == "Assets/Sparks.particlegraph"
    assert inspected["file_path"] == "Assets/Sparks.particlegraph"
    assert changed["asset"]["guid"] == "mesh-guid"
    assert changed["editor"]["dirty"] is True
    assert added["node"]["uid"] == "update::new-node"
    assert property_changed["value"] == [1.0, 2.0, 3.0]
    assert connected["link_uid"] == "update::new-link"
    assert value_connected["link_uid"] == "init::value-link"
    assert selected["stable_id"] == "target"
    assert emitter["emitter"]["stable_id"] == "target"
    assert emitter_settings["changed"] is True
    assert patched_settings["settings"] == {"capacity": 8}
    assert event_type["event_type_id"] == "event-id"
    assert event_type["event_type"]["stable_id"] == "event-id"
    assert event_route["event_route_id"] == "route-id"
    assert event_route["event_route"]["stable_id"] == "route-id"
    assert event_output["node"]["uid"] == "update::event-output"
    assert event_payload["node"]["uid"] == "init::event-payload"
    assert updated_type["event_type"]["stable_id"] == "event-id"
    assert updated_route["event_route"]["spawn_count"] == 7
    assert removed_route["event_route"]["stable_id"] == "route-id"
    assert removed_type["event_type"]["stable_id"] == "event-id"
    assert removed_emitter["emitter"]["stable_id"] == "target"
    assert routed["changed"] is True
    assert reloaded["file_path"] == "Assets/Sparks.particlegraph"
    assert discarded["discarded"] is True
    assert panel.calls == [
        ("rendering::mesh-output", "mesh", str(mesh_path.resolve())),
        ("add", "update", "particle.update.rotate_orientation", 120.0, 40.0),
        (
            "property",
            "update::new-node",
            "degrees_per_second",
            [1.0, 2.0, 3.0],
        ),
        ("connect", "update::root.update", "update::new-node"),
        ("value", "init::payload", "value", "init::size", "value"),
        ("select-emitter", "target"),
        ("add-emitter", "Target"),
        ("emitter-settings", "target", {"spawn_rate": 0.0}),
        ("patch-emitter-settings", "target", {"capacity": 8}),
        (
            "event-type",
            "Impact",
            64,
            [
                {
                    "name": "Weight",
                    "type": {"value_type": "f32", "space": "none"},
                    "default": 1.0,
                }
            ],
        ),
        ("event-route", "event-id", "source", "update", "target", 2),
        ("event-output", "route-id", 280.0, 220.0),
        ("event-payload", "route-id", 160.0, 40.0),
        (
            "update-event-type",
            "event-id",
            "Impact Renamed",
            8,
            [
                {
                    "stable_id": "field-id",
                    "name": "Weight",
                    "type": {"value_type": "f32", "space": "none"},
                    "default": 2.0,
                }
            ],
        ),
        (
            "update-event-route",
            "route-id",
            "event-id",
            "source",
            "update",
            "target",
            7,
        ),
        ("remove-event-route", "route-id"),
        ("remove-event-type", "event-id"),
        ("remove-emitter", "target"),
        ("output", "rendering::mesh-output"),
        ("reload",),
        ("discard",),
    ]


def test_particle_graph_panel_visibility_uses_the_panel_property(monkeypatch):
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.engine.ui.window_manager import WindowManager

    panel = ParticleGraphEditorPanel.__new__(ParticleGraphEditorPanel)
    panel._is_open = True

    class _Manager:
        @staticmethod
        def get_window_instance(window_id):
            assert window_id == "particle_graph_editor"
            return panel

    monkeypatch.setattr(WindowManager, "instance", classmethod(lambda _cls: _Manager()))
    assert module._require_particle_graph_panel() is panel

    panel._is_open = False
    with pytest.raises(RuntimeError, match="not open"):
        module._require_particle_graph_panel()


def test_particle_graph_open_asset_rejects_non_graph_files(tmp_path, monkeypatch):
    assets = tmp_path / "Assets"
    assets.mkdir()
    material = assets / "Surface.mat"
    material.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "main_thread",
        lambda _operation, callback, **_kwargs: callback(),
    )
    mcp = _FakeMcp()
    module.register_particle_tools(mcp, str(tmp_path))

    with pytest.raises(ValueError, match=r"requires a \.particlegraph"):
        mcp.tools["particle_graph_open_asset"]("Assets/Surface.mat")


def test_asset_create_particle_graph_uses_the_editor_asset_pipeline(
    tmp_path, monkeypatch
):
    assets = tmp_path / "Assets" / "Acceptance" / "VFX"
    assets.mkdir(parents=True)
    calls = []

    monkeypatch.setattr(
        assets_module,
        "main_thread",
        lambda _operation, callback, **_kwargs: callback(),
    )
    monkeypatch.setattr(assets_module, "get_asset_database", lambda: None)
    monkeypatch.setattr(
        "Infernux.engine.ui.project_file_ops.create_particlegraph",
        lambda directory, name, database: (
            calls.append((directory, name, database)) or (True, "")
        ),
    )
    mcp = _FakeMcp()
    assets_module.register_asset_tools(mcp, str(tmp_path))

    created = mcp.tools["asset_create_particle_graph"](
        "EventAcceptance.particlegraph", "Assets/Acceptance/VFX"
    )

    assert created["kind"] == "particlegraph"
    assert created["path"] == "Assets/Acceptance/VFX/EventAcceptance.particlegraph"
    assert created["created"] is True
    assert calls == [
        (str(assets.resolve()), "EventAcceptance.particlegraph", None)
    ]


def test_particle_system_runtime_tool_reads_only_the_control_plane(monkeypatch):
    class _Component:
        def runtime_diagnostics(self):
            return {
                "event_abi_hash": 41,
                "event_domain_serial": 7,
                "emitters": [{"index": 1, "target": "gpu", "simulation_step": 9}],
            }

    class _Object:
        id = 123
        name = "Event VFX"

    component = _Component()
    monkeypatch.setattr(module, "find_game_object", lambda object_id: _Object())
    monkeypatch.setattr(
        module,
        "_find_particle_system",
        lambda _obj, ordinal: component if ordinal == 0 else None,
    )
    monkeypatch.setattr(
        module,
        "main_thread",
        lambda _operation, callback, **_kwargs: callback(),
    )
    mcp = _FakeMcp()
    module.register_particle_tools(mcp, "C:/Project")

    state = mcp.tools["particle_system_inspect_runtime"](123)

    assert state == {
        "object_id": 123,
        "object_name": "Event VFX",
        "runtime": {
            "event_abi_hash": 41,
            "event_domain_serial": 7,
            "emitters": [{"index": 1, "target": "gpu", "simulation_step": 9}],
        },
    }


def test_particle_system_gpu_diagnostic_tools_request_then_poll(monkeypatch):
    class _Component:
        def request_gpu_diagnostics(self):
            return 77

        def poll_gpu_diagnostics(self, request_id):
            assert request_id == 77
            return {
                "request_id": 77,
                "status": "completed",
                "emitters": [{"stable_id": "target", "alive_count": 12}],
                "events": [{"route_stable_id": "impact", "spawned_count": 12}],
            }

    class _Object:
        id = 456
        name = "GPU Event VFX"

    component = _Component()
    monkeypatch.setattr(module, "find_game_object", lambda object_id: _Object())
    monkeypatch.setattr(
        module,
        "_find_particle_system",
        lambda _obj, ordinal: component if ordinal == 0 else None,
    )
    monkeypatch.setattr(
        module,
        "main_thread",
        lambda _operation, callback, **_kwargs: callback(),
    )
    mcp = _FakeMcp()
    module.register_particle_tools(mcp, "C:/Project")

    requested = mcp.tools["particle_system_request_gpu_diagnostics"](456)
    polled = mcp.tools["particle_system_poll_gpu_diagnostics"](456, 77)

    assert requested == {
        "object_id": 456,
        "object_name": "GPU Event VFX",
        "request_id": 77,
        "status": "pending",
    }
    assert polled["diagnostics"]["emitters"][0]["alive_count"] == 12
    assert polled["diagnostics"]["events"][0]["spawned_count"] == 12


def test_particle_system_emitter_control_tools_are_indexed_no_ops(monkeypatch):
    class _Component:
        def __init__(self):
            self.calls = []

        def start_emitter(self, index):
            self.calls.append(("start", index))
            return index == 0

        def pause_emitter(self, index):
            self.calls.append(("pause", index))
            return index == 0

        def terminate_emitter(self, index):
            self.calls.append(("terminate", index))
            return index == 0

        def restart(self, index):
            self.calls.append(("restart", index))
            return index == 0

        def runtime_diagnostics(self):
            return {"emitters": [{"index": 0, "playing": True}]}

    class _Object:
        id = 789
        name = "Controlled VFX"

    component = _Component()
    monkeypatch.setattr(module, "find_game_object", lambda object_id: _Object())
    monkeypatch.setattr(module, "_find_particle_system", lambda _obj, _ordinal: component)
    monkeypatch.setattr(
        module,
        "main_thread",
        lambda _operation, callback, **_kwargs: callback(),
    )
    mcp = _FakeMcp()
    module.register_particle_tools(mcp, "C:/Project")

    started = mcp.tools["particle_system_start_emitter"](789, 0)
    paused = mcp.tools["particle_system_pause_emitter"](789, 0)
    terminated = mcp.tools["particle_system_terminate_emitter"](789, 0)
    restarted = mcp.tools["particle_system_restart_emitter"](789, 0)
    invalid = mcp.tools["particle_system_start_emitter"](789, 99)

    assert started["accepted"] is True
    assert paused["operation"] == "pause"
    assert terminated["operation"] == "terminate"
    assert restarted["operation"] == "restart"
    assert invalid["accepted"] is False
    assert component.calls == [
        ("start", 0),
        ("pause", 0),
        ("terminate", 0),
        ("restart", 0),
        ("start", 99),
    ]
