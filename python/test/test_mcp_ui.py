"""Semantic screen-UI MCP tool contracts."""

from __future__ import annotations

import inspect
from contextlib import nullcontext
from pathlib import Path

import pytest

from Infernux.mcp.tools import ui
from Infernux.mcp.tools.common import _requires_saved_scene_file


class _FakeMcp:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, *args, **kwargs):
        name = str(kwargs.get("name") or (args[0] if args else ""))

        def _register(fn):
            self.tools[name] = fn
            return fn

        return _register


def test_ui_bind_click_is_registered():
    mcp = _FakeMcp()

    ui.register_ui_tools(mcp)

    assert "ui_bind_click" in mcp.tools
    assert "script_path" not in inspect.signature(mcp.tools["ui_bind_click"]).parameters


def test_ui_mutations_require_a_saved_scene():
    assert _requires_saved_scene_file("ui_bind_click") is True
    assert _requires_saved_scene_file("ui_set_text") is True
    assert _requires_saved_scene_file("ui_inspect") is False


def test_ui_bind_click_creates_typed_persistent_entry(monkeypatch):
    class MenuController:
        def start_expedition(self):
            pass

    class UIButton:
        def __init__(self):
            self.on_click_entries = []

    class Object:
        def __init__(self, object_id, name, components):
            self.id = object_id
            self.name = name
            self._components = components

        def get_parent(self):
            return None

        def get_py_components(self):
            return list(self._components)

    button = UIButton()
    target_component = MenuController()
    objects = {
        10: Object(10, "Start Expedition", [button]),
        20: Object(20, "Menu Controller", [target_component]),
    }
    monkeypatch.setattr(ui, "main_thread", lambda _name, fn, **_kwargs: fn())
    monkeypatch.setattr(ui, "_find_game_object", lambda object_id: objects[object_id])
    monkeypatch.setattr(
        ui,
        "_commit_python_component_fields",
        lambda obj, values, _description: [setattr(obj, key, value) for key, value in values.items()],
    )
    mcp = _FakeMcp()
    ui.register_ui_tools(mcp)

    result = mcp.tools["ui_bind_click"](
        button_id=10,
        target_id=20,
        component_name="MenuController",
        method_name="start_expedition",
    )

    assert result["binding"] == {
        "target_id": 20,
        "component_name": "MenuController",
        "method_name": "start_expedition",
        "argument_count": 0,
    }
    from Infernux.ui.ui_event_entry import _get_serializable_raw_field

    assert _get_serializable_raw_field(
        button.on_click_entries[0], "target"
    ).persistent_id == 20


def test_ui_bind_click_rejects_component_not_attached_to_target(monkeypatch):
    class UIButton:
        on_click_entries = []

    class Object:
        def __init__(self, object_id, components):
            self.id = object_id
            self.name = str(object_id)
            self._components = components

        def get_py_components(self):
            return list(self._components)

    objects = {10: Object(10, [UIButton()]), 20: Object(20, [])}
    monkeypatch.setattr(ui, "main_thread", lambda _name, fn, **_kwargs: fn())
    monkeypatch.setattr(ui, "_find_game_object", lambda object_id: objects[object_id])
    mcp = _FakeMcp()
    ui.register_ui_tools(mcp)

    with pytest.raises(FileNotFoundError, match="was not found on GameObject 20"):
        mcp.tools["ui_bind_click"](
            button_id=10,
            target_id=20,
            component_name="MenuController",
            method_name="start_expedition",
        )


def test_persistent_click_dispatch_resolves_attached_component():
    from Infernux.components import GameObjectRef
    from Infernux.ui import UIButton
    from Infernux.ui.ui_event_entry import UIEventEntry

    calls = []

    class MenuController:
        def start_expedition(self):
            calls.append("started")

    class TargetObject:
        id = 20
        name = "Menu Controller"

        def __init__(self):
            self._components = [MenuController()]

        def get_py_components(self):
            return list(self._components)

    target = TargetObject()
    button = UIButton()
    button.on_click_entries = [
        UIEventEntry(
            target=GameObjectRef(target),
            component_name="MenuController",
            method_name="start_expedition",
            arguments=[],
        )
    ]

    button.on_pointer_click(None)

    assert calls == ["started"]


def test_ui_field_mutation_uses_serialized_property_core(monkeypatch):
    import Infernux.engine.interaction as interaction

    class Component:
        x = 1.0
        y = 2.0

    class SceneObjects:
        def __init__(self):
            self.actions = []

        def user_action(self, description):
            self.actions.append(description)
            return nullcontext()

    class Transaction:
        def __init__(self, component, field):
            self.component = component
            self.field = field

        def commit_or_raise(self, value):
            setattr(self.component, self.field, value)
            return interaction.PropertyTransactionStatus.APPLIED

    component = Component()
    scene_objects = SceneObjects()
    calls = []

    def _factory(components, field, **kwargs):
        calls.append((components, field, kwargs["description"]))
        return Transaction(components[0], field)

    monkeypatch.setattr(ui, "_scene_object_service", lambda: scene_objects)
    monkeypatch.setattr(ui, "_invalidate_ui_cache", lambda: None)
    monkeypatch.setattr(
        interaction,
        "make_python_component_property_transaction",
        _factory,
    )

    ui._commit_python_component_fields(
        component,
        {"x": 5.0, "y": 7.0},
        "Set UI rectangle",
    )

    assert (component.x, component.y) == (5.0, 7.0)
    assert scene_objects.actions == ["Set UI rectangle"]
    assert [(field, description) for _targets, field, description in calls] == [
        ("x", "Set UI rectangle"),
        ("y", "Set UI rectangle"),
    ]


def test_ui_creation_uses_hierarchy_service_as_one_user_action(scene, monkeypatch):
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    try:
        monkeypatch.setattr(ui, "main_thread", lambda _name, fn, **_kwargs: fn())
        mcp = _FakeMcp()
        ui.register_ui_tools(mcp)

        result = mcp.tools["ui_create_text"](
            name="Status",
            text="Ready",
            rect={"x": 12.0, "y": 24.0},
        )

        obj = scene.find_by_id(result["object_id"])
        assert obj is not None
        assert result["fields"]["text"] == "Ready"
        assert result["fields"]["x"] == 12.0
        assert result["parent_id"] > 0
        assert len(manager.action_journal.applied_entries()) == 1
        assert manager.undo_description == "Create UI Text"
    finally:
        core.shutdown()
        UndoManager._instance = previous_manager


def test_ui_click_entries_commit_through_python_component_document(scene, monkeypatch):
    from Infernux.components import GameObjectRef
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.undo import UndoManager
    from Infernux.ui.ui_event_entry import UIEventEntry

    previous_manager = UndoManager._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    try:
        creation = HierarchyCreationService.instance()
        button_entry = creation.create("ui.button", name="Start", select=False)
        target_entry = creation.create("empty", name="Controller", select=False)
        button_obj = scene.find_by_id(int(button_entry["id"]))
        target_obj = scene.find_by_id(int(target_entry["id"]))
        button = ui._find_named_component(button_obj, {"UIButton"})
        entry = UIEventEntry(
            target=GameObjectRef(target_obj),
            component_name="MenuController",
            method_name="start_game",
            arguments=[],
        )
        manager.clear()
        monkeypatch.setattr(ui, "_invalidate_ui_cache", lambda: None)

        ui._commit_python_component_fields(
            button,
            {"on_click_entries": [entry]},
            "Bind UIButton on_click",
        )

        assert len(button.on_click_entries) == 1
        assert button.on_click_entries[0].method_name == "start_game"
        assert len(manager.action_journal.applied_entries()) == 1
        manager.undo()
        assert button.on_click_entries == []
    finally:
        core.shutdown()
        UndoManager._instance = previous_manager


def test_ui_mcp_mutations_have_no_private_undo_or_dirty_bypass():
    source = Path(ui.__file__).read_text(encoding="utf-8")

    assert "SceneFileManager" not in source
    assert ".mark_dirty(" not in source
    assert "_record_property" not in source
    assert "UndoManager" not in source
    assert "HierarchyCreationService.instance().create" not in source
    assert "creation.create(" in source
    assert "make_python_component_property_transaction" in source
    assert "_scene_object_service().user_action" in source
