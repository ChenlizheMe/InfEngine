from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

from Infernux.engine.hierarchy_creation_service import _unique_scene_object_name


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


def test_hierarchy_creation_wiring_exposes_all_user_facing_ui_elements(monkeypatch):
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
    assert ui_entries == [
        ("UI", "hierarchy.ui_canvas"),
        ("UI", "hierarchy.ui_image"),
        ("UI", "hierarchy.ui_text"),
        ("UI", "hierarchy.ui_button"),
    ]


def test_hierarchy_creation_catalog_includes_image():
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService

    service = HierarchyCreationService()
    kinds = {entry["kind"] for entry in service.list_create_kinds()}

    assert "ui.image" in kinds
    assert service._description_for("ui.image") == "Create Image"


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
