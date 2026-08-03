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


def test_hierarchy_creation_wiring_exposes_canvas_only_ui_and_particle_effect(monkeypatch):
    from Infernux.engine.bootstrap_hierarchy import _creation

    class _Service:
        def configure(self, **_kwargs):
            pass

        def create(self, *_args, **_kwargs):
            raise AssertionError("registration must not create scene objects")

    class _Hierarchy:
        def __init__(self):
            self.entries = []

        def clear_create_entries(self):
            self.entries.clear()

        def add_create_entry(self, category, locale_key, callback):
            self.entries.append((category, locale_key, callback))

    hierarchy = _Hierarchy()
    monkeypatch.setattr(_creation.HierarchyCreationService, "instance", staticmethod(lambda: _Service()))

    _creation.wire_creation_callbacks(
        SimpleNamespace(hp=hierarchy, sel=object(), undo=object())
    )

    ui_entries = [(category, locale_key) for category, locale_key, _callback in hierarchy.entries if category == "UI"]
    effect_entries = [
        (category, locale_key)
        for category, locale_key, _callback in hierarchy.entries
        if category == "Effect"
    ]
    assert ui_entries == [("UI", "hierarchy.ui_canvas")]
    assert effect_entries == [("Effect", "hierarchy.particle_system")]


def test_hierarchy_creation_catalog_includes_image():
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService

    service = HierarchyCreationService()
    kinds = {entry["kind"] for entry in service.list_create_kinds()}

    assert "ui.image" in kinds
    assert service._description_for("ui.image") == "Create Image"


def test_creation_selects_through_typed_service_and_records_one_context():
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.engine.interaction import SelectionService, SelectionTarget

    revealed = []
    recorded = []
    selection = SelectionService()
    selection.select(
        SelectionTarget.scene_object(7),
        owner_id="hierarchy",
        record_history=False,
    )

    class _UndoTracker:
        @staticmethod
        def record_create(object_id, description, **kwargs):
            recorded.append((object_id, description, kwargs))

    service = HierarchyCreationService()
    service.configure(
        selection_manager=None,
        undo_tracker=_UndoTracker(),
        hierarchy_panel=SimpleNamespace(
            set_pending_expand_id=lambda object_id: revealed.append(object_id)
        ),
    )

    service._finalize(_Object(42, "Created"), 0, "Create", select=True, record_undo=True)

    assert selection.snapshot.primary == SelectionTarget.scene_object(42)
    assert revealed == [42]
    assert len(recorded) == 1
    assert recorded[0][2]["before_selection"].primary == SelectionTarget.scene_object(7)
    assert recorded[0][2]["after_selection"].primary == SelectionTarget.scene_object(42)


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
        selection_manager=SimpleNamespace(get_primary=lambda: 0),
        undo_tracker=None,
        hierarchy_panel=None,
    )

    assert service._find_canvas_parent_id(_UiScene(), 0) == 39
    assert service._find_canvas_parent_id(_UiScene(), 12) == 39


def test_hierarchy_creation_configures_ui_after_parenting_and_records_once(scene):
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.ui import UICanvas, UIImage

    canvas = scene.create_game_object("Canvas")
    canvas.add_py_component(UICanvas())
    recorded = []

    class _UndoTracker:
        @staticmethod
        def record_create(object_id, description, **kwargs):
            recorded.append((object_id, description, kwargs))

    configured = []

    def configure_created(obj):
        assert obj.get_parent() is canvas
        image = obj.get_py_component(UIImage)
        image.width = 384.0
        configured.append(image)

    service = HierarchyCreationService()
    service.configure(undo_tracker=_UndoTracker())
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
    assert len(recorded) == 1
    assert recorded[0][0] == image_object.id
    assert recorded[0][2]["after_selection"].owner_id == "ui_editor"


def test_hierarchy_creation_configuration_failure_rolls_back_without_history(scene):
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.engine.interaction import SelectionService
    from Infernux.ui import UICanvas

    canvas = scene.create_game_object("Canvas")
    canvas.add_py_component(UICanvas())
    before_ids = {obj.id for obj in scene.get_all_objects()}
    before_selection = SelectionService.instance().snapshot
    recorded = []

    class _UndoTracker:
        @staticmethod
        def record_create(*args, **kwargs):
            recorded.append((args, kwargs))

    def reject_created(obj):
        assert obj.get_parent() is canvas
        raise RuntimeError("reject configured UI")

    service = HierarchyCreationService()
    service.configure(undo_tracker=_UndoTracker())
    with pytest.raises(RuntimeError, match="reject configured UI"):
        service.create(
            "ui.text",
            parent_id=canvas.id,
            configure_created=reject_created,
        )

    assert {obj.id for obj in scene.get_all_objects()} == before_ids
    assert SelectionService.instance().snapshot == before_selection
    assert recorded == []


def test_ui_editor_creation_uses_shared_atomic_hierarchy_service(scene, monkeypatch):
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.engine.interaction import EditorContextSnapshot, SelectionService
    from Infernux.engine.ui._ui_editor_creation import UIEditorCreationMixin
    from Infernux.engine.undo import HierarchyUndoTracker, UndoManager
    from Infernux.ui import UICanvas, UIText

    class _Hierarchy:
        def __init__(self):
            self.expanded = []

        def set_pending_expand_id(self, object_id):
            self.expanded.append(int(object_id))

    class _Editor(UIEditorCreationMixin):
        def __init__(self, hierarchy):
            self._hierarchy_panel = hierarchy
            self._focused_canvas_id = 0
            self._selected_element_comp = None
            self._dragging = False
            self._resizing = False
            self._resize_handle_idx = -1
            self._rotating = False
            self._active_alignment_guides = []

        def _clear_interaction_state(self):
            self._selected_element_comp = None
            self._dragging = False
            self._resizing = False
            self._resize_handle_idx = -1
            self._rotating = False
            self._active_alignment_guides = []

    selection = SelectionService.instance()
    original_selection = selection.snapshot
    previous_manager = UndoManager.instance()
    manager = UndoManager()
    manager.set_context_hooks(
        lambda: EditorContextSnapshot(selection=selection.snapshot),
        lambda context, _phase: selection.apply_snapshot(
            context.selection,
            record_history=False,
        ),
    )
    hierarchy = _Hierarchy()
    service = HierarchyCreationService()
    service.configure(
        undo_tracker=HierarchyUndoTracker(),
        hierarchy_panel=hierarchy,
    )
    monkeypatch.setattr(HierarchyCreationService, "_instance", service)
    editor = _Editor(hierarchy)

    try:
        editor._create_canvas()
        canvas = scene.find_by_id(editor._focused_canvas_id)
        assert canvas is not None
        assert canvas.get_py_component(UICanvas) is not None
        assert len(manager.action_journal.entries) == 1
        assert selection.snapshot.owner_id == "ui_editor"

        editor._create_text_element(canvas)
        text = editor._selected_element_comp
        text_object = text.game_object
        text_id = text_object.id
        assert isinstance(text, UIText)
        assert text_object.get_parent() is canvas
        assert len(manager.action_journal.entries) == 2
        assert selection.snapshot.owner_id == "ui_editor"
        assert hierarchy.expanded == [canvas.id, text_id]

        manager.undo()
        assert scene.find_by_id(text_id) is None
        assert scene.find_by_id(canvas.id) is canvas
        assert selection.snapshot.primary.scene_object_id() == canvas.id
    finally:
        manager.clear()
        selection.apply_snapshot(original_selection, record_history=False)
        UndoManager._instance = previous_manager
