from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from Infernux.engine.hierarchy_creation_service import (
    _component_names,
    _unique_scene_object_name,
)


@dataclass
class _Object:
    id: int
    name: str


class _Scene:
    def __init__(self, *objects: _Object) -> None:
        self._objects = list(objects)

    def get_all_objects(self):
        return list(self._objects)


def test_unique_scene_object_name_uses_first_available_unity_style_suffix():
    scene = _Scene(_Object(1, "Cube"), _Object(2, "Cube (1)"), _Object(3, "Cube (3)"))

    assert _unique_scene_object_name(scene, "Cube") == "Cube (2)"
    assert _unique_scene_object_name(scene, "Cube", exclude_id=1) == "Cube"


def test_component_names_deduplicates_python_components_in_combined_component_view():
    component = SimpleNamespace(component_id=17, type_name="ParticleSystem")
    obj = SimpleNamespace(
        get_components=lambda: [component],
        get_py_components=lambda: [component],
    )

    assert _component_names(obj) == ["ParticleSystem"]


def test_hierarchy_creation_wiring_only_configures_shared_creation_service(monkeypatch):
    from Infernux.engine.bootstrap_hierarchy import _creation

    class _Service:
        configured = None

        def configure(self, **kwargs):
            self.configured = kwargs

        def create(self, *_args, **_kwargs):
            raise AssertionError("registration must not create scene objects")

    navigation = object()
    service = _Service()
    monkeypatch.setattr(
        _creation.HierarchyCreationService,
        "instance",
        staticmethod(lambda: service),
    )

    _creation.wire_creation_callbacks(
        SimpleNamespace(
            selection=object(),
            bs=SimpleNamespace(
                interaction_core=SimpleNamespace(navigation=navigation)
            ),
        )
    )

    assert service.configured["navigation_service"] is navigation
    assert service.configured["selection_service"] is not None


def test_hierarchy_creation_catalog_includes_image():
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService

    service = HierarchyCreationService()
    kinds = {entry["kind"] for entry in service.list_create_kinds()}

    assert "ui.image" in kinds
    assert service._description_for("ui.image") == "Create Image"


def test_creation_selects_through_typed_service_and_records_one_context():
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.engine.interaction import SelectionService, SelectionTarget
    from Infernux.engine.undo import UndoManager

    revealed = []
    selection = SelectionService()
    selection.select(
        SelectionTarget.scene_object(7),
        owner_id="hierarchy",
        record_history=False,
    )

    previous_manager = UndoManager.instance()
    manager = UndoManager()
    service = HierarchyCreationService()
    service.configure(
        selection_service=None,
        navigation_service=SimpleNamespace(
            reveal=lambda target, **_kwargs: revealed.append(
                target.scene_object_id()
            ) or True
        ),
    )
    try:
        service._finalize(_Object(42, "Created"), 0, "Create", select=True, record_undo=True)

        assert selection.snapshot.primary == SelectionTarget.scene_object(42)
        assert revealed == [42]
        entries = manager.action_journal.applied_entries()
        assert len(entries) == 1
        command = entries[0].action
        assert command.before_selection_snapshot.primary == SelectionTarget.scene_object(7)
        assert command.after_selection_snapshot.primary == SelectionTarget.scene_object(42)
    finally:
        UndoManager._instance = previous_manager


def test_ui_element_creation_uses_the_only_existing_canvas_when_context_parent_is_lost():
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.ui import UICanvas

    class _UiObject:
        def __init__(self, object_id, components=(), parent=None):
            self.id = object_id
            self._components = list(components)
            self._parent = parent

        def get_py_components(self):
            return list(self._components)

        def get_parent(self):
            return self._parent

    canvas = _UiObject(39, [UICanvas()])
    unrelated = _UiObject(12)

    class _UiScene:
        def find_by_id(self, object_id):
            return {39: canvas, 12: unrelated}.get(object_id)

        def get_all_objects(self):
            return [unrelated, canvas]

    service = HierarchyCreationService()
    service.configure(
        selection_service=SimpleNamespace(primary_scene_object_id=lambda: 0),
        navigation_service=None,
    )

    assert service._find_canvas_parent_id(_UiScene(), 0) == 39
    assert service._find_canvas_parent_id(_UiScene(), 12) == 39


def test_hierarchy_creation_configures_ui_after_parenting_and_records_once(scene):
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.engine.undo import UndoManager
    from Infernux.ui import UICanvas, UIImage

    canvas = scene.create_game_object("Canvas")
    canvas.add_py_component(UICanvas())
    configured = []

    def configure_created(obj):
        assert obj.get_parent() is canvas
        image = obj.get_py_component(UIImage)
        image.width = 384.0
        configured.append(image)

    service = HierarchyCreationService()
    previous_manager = UndoManager.instance()
    manager = UndoManager()
    try:
        created = service.create(
            "ui.image",
            parent_id=canvas.id,
            selection_owner_id="ui_editor",
            configure_created=configure_created,
        )

        image_object = scene.find_by_id(created["id"])
        assert image_object.get_parent() is canvas
        assert image_object.get_py_component(UIImage).width == pytest.approx(384.0)
        assert configured == [image_object.get_py_component(UIImage)]
        entries = manager.action_journal.applied_entries()
        assert len(entries) == 1
        command = entries[0].action
        assert command._object_id == image_object.id
        assert command.after_selection_snapshot.owner_id == "ui_editor"
    finally:
        UndoManager._instance = previous_manager


def test_hierarchy_creation_absorbs_initializer_property_edits_into_create(scene):
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.engine.interaction import EditorContextSnapshot, SelectionService
    from Infernux.engine.undo import (
        SetPropertyCommand,
        UndoManager,
    )
    from Infernux.ui import UICanvas, UIText

    canvas = scene.create_game_object("Canvas")
    canvas.add_py_component(UICanvas())
    selection = SelectionService.instance()
    original_selection = selection.snapshot
    previous_manager = UndoManager.instance()
    manager = UndoManager()
    manager.set_context_hooks(
        lambda: EditorContextSnapshot(selection=selection.snapshot),
        lambda context, _phase: (
            selection.apply_snapshot(context.selection, record_history=False),
            True,
        )[1],
    )
    service = HierarchyCreationService()

    def configure_created(obj):
        text = obj.get_py_component(UIText)
        old_x = float(text.x)
        text.x = 48.0
        manager.record(SetPropertyCommand(text, "x", old_x, 48.0, "Set x"))

    try:
        created = service.create(
            "ui.text",
            parent_id=canvas.id,
            configure_created=configure_created,
        )
        text_id = int(created["id"])

        assert len(manager.action_journal.entries) == 1
        assert manager.undo_description == "Create Text"

        manager.undo()
        assert scene.find_by_id(text_id) is None
        assert manager.can_undo is False
        assert manager.can_redo is True

        manager.redo()
        restored = scene.find_by_id(text_id)
        assert restored is not None
        assert restored.get_py_component(UIText).x == pytest.approx(48.0)
    finally:
        manager.clear()
        selection.apply_snapshot(original_selection, record_history=False)
        UndoManager._instance = previous_manager


def test_hierarchy_creation_configuration_failure_rolls_back_without_history(scene):
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.engine.interaction import SelectionService
    from Infernux.ui import UICanvas

    canvas = scene.create_game_object("Canvas")
    canvas.add_py_component(UICanvas())
    before_ids = {obj.id for obj in scene.get_all_objects()}
    before_selection = SelectionService.instance().snapshot
    def reject_created(obj):
        assert obj.get_parent() is canvas
        raise RuntimeError("reject configured UI")

    service = HierarchyCreationService()
    with pytest.raises(RuntimeError, match="reject configured UI"):
        service.create(
            "ui.text",
            parent_id=canvas.id,
            configure_created=reject_created,
        )

    assert {obj.id for obj in scene.get_all_objects()} == before_ids
    assert SelectionService.instance().snapshot == before_selection


def test_hierarchy_creation_rejected_by_history_rolls_back_object_and_selection(scene):
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.engine.interaction import SelectionService, SelectionTarget
    from Infernux.engine.undo import UndoManager

    selection = SelectionService.instance()
    anchor = scene.create_game_object("Anchor")
    selection.select(
        SelectionTarget.scene_object(anchor.id),
        owner_id="hierarchy",
        record_history=False,
    )
    before_selection = selection.snapshot
    before_ids = {obj.id for obj in scene.get_all_objects()}

    service = HierarchyCreationService()
    previous_manager = UndoManager.instance()
    manager = UndoManager()
    original_record = manager.record
    manager.record = lambda *_args, **_kwargs: False
    try:
        with pytest.raises(RuntimeError, match="rejected hierarchy creation"):
            service.create("empty")

        assert {obj.id for obj in scene.get_all_objects()} == before_ids
        assert selection.snapshot == before_selection
    finally:
        manager.record = original_record
        UndoManager._instance = previous_manager


def test_ui_editor_creation_uses_shared_atomic_hierarchy_service(scene, monkeypatch):
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.engine.interaction import EditorContextSnapshot, SelectionService
    from Infernux.engine.undo import UndoManager
    from Infernux.ui import UICanvas, UIText

    class _Navigation:
        def __init__(self):
            self.revealed = []

        def reveal(self, target, **_kwargs):
            self.revealed.append(target.scene_object_id())
            return True

    selection = SelectionService.instance()
    original_selection = selection.snapshot
    previous_manager = UndoManager.instance()
    manager = UndoManager()
    manager.set_context_hooks(
        lambda: EditorContextSnapshot(selection=selection.snapshot),
        lambda context, _phase: (
            selection.apply_snapshot(
                context.selection,
                record_history=False,
            ),
            True,
        )[1],
    )
    navigation = _Navigation()
    service = HierarchyCreationService()
    service.configure(
        navigation_service=navigation,
    )
    monkeypatch.setattr(HierarchyCreationService, "_instance", service)
    try:
        created_canvas = service.create(
            "ui.canvas",
            selection_owner_id="ui_editor",
            selection_reason="ui_editor_create_canvas",
        )
        canvas = scene.find_by_id(created_canvas["id"])
        assert canvas is not None
        assert canvas.get_py_component(UICanvas) is not None
        assert len(manager.action_journal.entries) == 1
        assert selection.snapshot.owner_id == "ui_editor"

        service.create(
            "ui.text",
            parent_id=canvas.id,
            selection_owner_id="ui_editor",
            selection_reason="ui_editor_create_text",
        )
        text_object = scene.find_by_id(selection.primary_scene_object_id())
        text = text_object.get_py_component(UIText)
        text_id = text_object.id
        assert isinstance(text, UIText)
        assert (text.x, text.y) == (-80.0, -20.0)
        assert text_object.get_parent() is canvas
        assert len(manager.action_journal.entries) == 2
        assert selection.snapshot.owner_id == "ui_editor"
        assert navigation.revealed == [canvas.id, text_id]

        manager.undo()
        assert scene.find_by_id(text_id) is None
        assert scene.find_by_id(canvas.id) is canvas
        assert selection.snapshot.primary.scene_object_id() == canvas.id
    finally:
        manager.clear()
        selection.apply_snapshot(original_selection, record_history=False)
        UndoManager._instance = previous_manager


def test_ui_editor_creation_buttons_submit_the_global_scene_command(monkeypatch):
    from Infernux.engine.interaction import CommandResult, CommandStatus, EditorCommandRegistry
    from Infernux.engine.ui._ui_editor_creation import UIEditorCreationMixin

    calls = []
    registry = SimpleNamespace(
        execute=lambda command_id, **kwargs: (
            calls.append((command_id, kwargs)),
            CommandResult(command_id, CommandStatus.EXECUTED),
        )[1]
    )
    monkeypatch.setattr(EditorCommandRegistry, "_instance", registry)
    editor = UIEditorCreationMixin()
    canvas = SimpleNamespace(id=73)

    assert editor._create_canvas()
    assert editor._create_text_element(canvas)
    assert editor._create_image_element(canvas)
    assert editor._create_button_element(canvas)
    assert [payload[1]["payload"] for payload in calls] == [
        {"kind": "ui.canvas", "parent_id": 0},
        {"kind": "ui.text", "parent_id": 73},
        {"kind": "ui.image", "parent_id": 73},
        {"kind": "ui.button", "parent_id": 73},
    ]
