from __future__ import annotations

import pytest

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

    def set_rendering_output(self, node_uid):
        self.calls.append(("output", node_uid))
        return {"node_uid": node_uid, "link_uid": "rendering::mesh-link", "changed": True}

    def reload_from_disk(self):
        self.calls.append(("reload",))
        return True


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
    routed = mcp.tools["particle_graph_set_rendering_output"](
        "rendering::mesh-output"
    )
    reloaded = mcp.tools["particle_graph_reload_editor"]()

    assert opened["file_path"] == "Assets/Sparks.particlegraph"
    assert inspected["file_path"] == "Assets/Sparks.particlegraph"
    assert changed["asset"]["guid"] == "mesh-guid"
    assert changed["editor"]["dirty"] is True
    assert added["node"]["uid"] == "update::new-node"
    assert property_changed["value"] == [1.0, 2.0, 3.0]
    assert connected["link_uid"] == "update::new-link"
    assert value_connected["link_uid"] == "init::value-link"
    assert selected["stable_id"] == "target"
    assert event_type["event_type"]["stable_id"] == "event-id"
    assert event_route["event_route"]["stable_id"] == "route-id"
    assert routed["changed"] is True
    assert reloaded["file_path"] == "Assets/Sparks.particlegraph"
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
        ("output", "rendering::mesh-output"),
        ("reload",),
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
