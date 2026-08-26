from __future__ import annotations

import builtins
from types import SimpleNamespace

from Infernux.application import Application
from Infernux.engine import component_restore


def test_player_component_restore_does_not_import_editor_gizmos(monkeypatch):
    monkeypatch.setattr(Application, "is_editor", staticmethod(lambda: False))
    original_import = builtins.__import__

    def reject_gizmos(name, *args, **kwargs):
        if name == "Infernux.gizmos" or name.startswith("Infernux.gizmos."):
            raise AssertionError("Player component restore imported editor Gizmos")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_gizmos)

    component_restore._notify_editor_scene_changed()


def test_scene_publish_accepts_native_fresh_python_component_id():
    class NativeComponent:
        execution_order = 0

        def _set_component_id(self, value):
            self.component_id = int(value)

    class Instance:
        def __init__(self):
            self._cpp_component = None
            self.after_deserialize = False

        def _refresh_native_handle(self):
            return None

        def _call_on_after_deserialize(self):
            self.after_deserialize = True

    instance = Instance()
    native = NativeComponent()

    class Target:
        def _attach_prepared_py_component(self, value, _index):
            value._cpp_component = native
            return value

        def _activate_prepared_py_component(self, value):
            self.activated = value

        def _remove_prepared_py_component(self, _value):
            return True

    target = Target()
    pending = SimpleNamespace(
        game_object_id=7,
        type_name="Probe",
        script_guid="script-guid",
        type_guid="type-guid",
        enabled=True,
        execution_order=3,
        component_index=0,
        fields_document={"__type_name__": "Probe", "__component_id__": 42, "speed": 2.0},
    )

    class Scene:
        def get_pending_py_components(self):
            return [pending]

        def take_pending_py_components(self):
            return [pending]

        def find_by_id(self, value):
            return target if int(value) == 7 else None

    prepared = component_restore.PreparedPythonComponent(
        game_object_id=7,
        source_object_id=7,
        document_path="objects[0].components[0]",
        type_name="Probe",
        script_guid="script-guid",
        type_guid="type-guid",
        enabled=True,
        execution_order=3,
        component_id=6,
        fields_document={"__type_name__": "Probe", "__component_id__": 6, "speed": 2.0},
        instance=instance,
    )
    graph = component_restore.PreparedPythonComponentGraph([prepared])

    component_restore._publish_prepared_scene_python_components(
        Scene(), graph, clear_registries=False
    )

    assert prepared.component_id == 42
    assert prepared.fields_document["__component_id__"] == 42
    assert native.component_id == 42
    assert instance._component_id == 42
    assert instance.after_deserialize is True
    assert target.activated is native
