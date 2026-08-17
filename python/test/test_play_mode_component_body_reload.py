from __future__ import annotations

import os
import sys
import textwrap
import importlib
from types import SimpleNamespace

import pytest

from Infernux.components.component_identity import bind_asset_script_guid
from Infernux.components.registry import (
    get_type,
    get_type_by_identity,
    publish_component_script_types,
    restore_component_script_registry,
    snapshot_component_script_registry,
)
from Infernux.components.script_loader import (
    _clear_script_error,
    get_script_error_by_path,
    get_script_error_revision,
    load_all_components_from_file,
    set_script_error,
)
from Infernux.components.fields import get_serialized_fields
import Infernux.components.script_loader as script_loader
from Infernux.engine.play_mode import PlayModeManager, PlayModeState
from Infernux.engine.play_mode import ScriptReloadBatchInput
from Infernux.engine.project_context import (
    get_project_root,
    get_script_module_name,
    set_project_root,
)


class _NativeDispatchProbe:
    def __init__(self) -> None:
        self.handle = object()
        self.refresh_calls = 0

    def refresh_python_lifecycle_dispatch(self) -> None:
        self.refresh_calls += 1


class _ScriptObject:
    def __init__(self, components) -> None:
        self.id = 1
        self._components = list(components)

    def get_py_components(self):
        return tuple(self._components)

    def remove_py_component(self, component):
        self._components.remove(component)

    def add_py_component(self, component):
        self._components.append(component)


class _AtomicScriptObject(_ScriptObject):
    def __init__(self, object_id, components, *, fail=False):
        super().__init__(components)
        self.id = object_id
        self.fail = fail

    def replace_py_component(self, old_component, new_component):
        if self.fail:
            raise RuntimeError("simulated replacement failure")
        try:
            index = self._components.index(old_component)
        except ValueError:
            return None
        self._components[index] = new_component
        return new_component


class _MultiScriptScene:
    def __init__(self, objects):
        self._objects = tuple(objects)

    def get_all_objects(self):
        return self._objects

    def find_by_id(self, object_id):
        return next((obj for obj in self._objects if obj.id == object_id), None)


class _ScriptScene:
    def __init__(self, components) -> None:
        self._objects = (_ScriptObject(components),)

    def get_all_objects(self):
        return self._objects

    def find_by_id(self, object_id):
        return next((obj for obj in self._objects if obj.id == object_id), None)


class _ScriptSceneManager:
    def __init__(self, scene) -> None:
        self._scene = scene

    def get_active_scene(self):
        return self._scene


class _ScriptAssetDatabase:
    def __init__(self, path: str, guid: str) -> None:
        self._path = path
        self._guid = guid

    def get_guid_from_path(self, path: str) -> str:
        assert path == self._path
        return self._guid


class _ScriptBatchAssetDatabase:
    def __init__(self, entries) -> None:
        self._entries = {
            os.path.normcase(os.path.abspath(path)): guid
            for path, guid in entries.items()
        }

    def get_guid_from_path(self, path: str) -> str:
        return self._entries.get(os.path.normcase(os.path.abspath(path)), "")


@pytest.fixture
def component_script(tmp_path):
    previous_root = get_project_root()
    project = tmp_path / "Project"
    assets = project / "Assets"
    assets.mkdir(parents=True)
    set_project_root(str(project))
    cleanups = []

    def create(
        name: str,
        source: str,
        guid: str = "",
        *,
        expect_components: bool = True,
    ):
        path = assets / name
        snapshot = snapshot_component_script_registry(str(path))
        module_name = get_script_module_name(str(path))
        assert module_name
        previous_module = sys.modules.get(module_name)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        classes = tuple(load_all_components_from_file(str(path), register=False))
        if expect_components:
            assert classes
            assert all(component_type.__module__ == module_name for component_type in classes)
            for component_type in classes:
                bind_asset_script_guid(component_type, guid)
            publish_component_script_types(str(path), classes)
        cleanups.append((str(path), snapshot, module_name, previous_module))
        return path, classes

    yield create

    for path, snapshot, module_name, previous_module in reversed(cleanups):
        _clear_script_error(path)
        restore_component_script_registry(path, snapshot)
        if previous_module is None:
            sys.modules.pop(module_name, None)
        else:
            sys.modules[module_name] = previous_module
    set_project_root(previous_root)


def _play_manager(monkeypatch, path, guid, components):
    manager = PlayModeManager()
    manager._state = PlayModeState.PLAYING
    manager.set_asset_database(_ScriptAssetDatabase(str(path.resolve()), guid))
    scene = _ScriptScene(components)
    monkeypatch.setattr(
        manager,
        "_get_scene_manager",
        lambda: _ScriptSceneManager(scene),
    )
    return manager


def _play_batch_manager(monkeypatch, entries, components):
    manager = PlayModeManager()
    manager._state = PlayModeState.PLAYING
    manager.set_asset_database(_ScriptBatchAssetDatabase(entries))
    scene = _ScriptScene(components)
    monkeypatch.setattr(
        manager,
        "_get_scene_manager",
        lambda: _ScriptSceneManager(scene),
    )
    return manager


def _edit_multi_object_manager(monkeypatch, path, guid, objects):
    manager = PlayModeManager()
    manager._state = PlayModeState.EDIT
    manager.set_asset_database(_ScriptAssetDatabase(str(path.resolve()), guid))
    scene = _MultiScriptScene(objects)
    monkeypatch.setattr(
        manager,
        "_get_scene_manager",
        lambda: _ScriptSceneManager(scene),
    )
    return manager, scene


def _overwrite_preserving_pyc_fingerprint(path, source: str) -> None:
    previous = path.stat()
    encoded = textwrap.dedent(source).encode("utf-8")
    assert len(encoded) <= previous.st_size
    encoded += b" " * (previous.st_size - len(encoded))
    path.write_bytes(encoded)
    os.utime(
        path,
        ns=(previous.st_atime_ns, previous.st_mtime_ns),
    )


def test_play_body_reload_preserves_identity_state_and_uses_new_body(
    component_script,
    monkeypatch,
):
    guid = "play-body-identity-guid"
    path, (component_type,) = component_script(
        "BodyReloadProbe.py",
        """
        from Infernux.components import InxComponent

        class BodyReloadProbe(InxComponent):
            _uses_component_data_store = False
            value: int = 1

            def awake(self):
                self.awake_count = getattr(self, "awake_count", 0) + 1

            def start(self):
                self.start_count = getattr(self, "start_count", 0) + 1

            def update(self, delta_time):
                self.phase = ("old", delta_time)

            def helper(self):
                return "old-helper"

            @property
            def label(self):
                return "old-property"

            @staticmethod
            def static_label():
                return "old-static"

            @classmethod
            def class_label(cls):
                return "old-class:" + cls.__name__
        """,
        guid,
    )
    component = component_type()
    component._script_guid = guid
    component.value = 41
    component.runtime_note = {"keep": True}
    component._call_awake()
    component._call_start()
    native = _NativeDispatchProbe()
    component._cpp_component = native
    component._native_handle = native.handle
    original_component_id = component.component_id
    original_handle = component._native_handle
    holder = SimpleNamespace(component=component)
    manager = _play_manager(monkeypatch, path, guid, (component,))

    _overwrite_preserving_pyc_fingerprint(path, """
        from Infernux.components import InxComponent

        class BodyReloadProbe(InxComponent):
            _uses_component_data_store = False
            value: int = 1

            def awake(self):
                self.awake_count = getattr(self, "awake_count", 0) + 1

            def start(self):
                self.start_count = getattr(self, "start_count", 0) + 1

            def update(self, delta_time):
                self.phase = ("new", delta_time)

            def helper(self):
                return "new-helper"

            @property
            def label(self):
                return "new-property"

            @staticmethod
            def static_label():
                return "new-static"

            @classmethod
            def class_label(cls):
                return "new-class:" + cls.__name__
    """)

    assert manager.reload_components_from_script(str(path)) == 1
    assert holder.component is component
    assert type(component) is component_type
    assert component.component_id == original_component_id
    assert component._cpp_component is native
    assert component._native_handle is original_handle
    assert component.value == 41
    assert component.runtime_note == {"keep": True}
    assert component.awake_count == 1
    assert component.start_count == 1
    assert component._awake_called is True
    assert component._has_started is True
    assert native.refresh_calls == 1

    component._call_update(0.25)
    assert component.phase == ("new", 0.25)
    assert component.helper() == "new-helper"
    assert component.label == "new-property"
    assert component.static_label() == "new-static"
    assert component.class_label() == "new-class:BodyReloadProbe"


def test_schema_reload_preserves_live_cds_values_and_uses_new_field_default(
    component_script,
    monkeypatch,
):
    """Schema publication must not turn authored values into range minima."""
    guid = "schema-value-preservation-guid"
    path, (component_type,) = component_script(
        "SchemaValuePreservationProbe.py",
        """
        from Infernux.components import InxComponent, serialized_field

        class SchemaValuePreservationProbe(InxComponent):
            engine_force: float = serialized_field(
                default=6200.0, range=(0.0, 10000.0)
            )
            cruise_speed: float = serialized_field(
                default=12.5, range=(2.0, 40.0)
            )
            buoyancy_per_point: float = serialized_field(
                default=3100.0, range=(500.0, 8000.0)
            )
            playback: float = serialized_field(
                default=0.78, range=(0.0, 2.0)
            )
            controls_enabled: bool = serialized_field(default=True)

            def marker(self):
                return "old"
        """,
        guid,
    )
    component = component_type()
    component._script_guid = guid
    component.engine_force = 7111.0
    component.cruise_speed = 18.25
    component.buoyancy_per_point = 4321.0
    component.playback = 0.78
    component.controls_enabled = False
    manager = _play_manager(monkeypatch, path, guid, (component,))

    candidate = textwrap.dedent(
        """
        from Infernux.components import InxComponent, serialized_field

        class SchemaValuePreservationProbe(InxComponent):
            engine_force: float = serialized_field(
                default=6200.0, range=(0.0, 10000.0)
            )
            cruise_speed: float = serialized_field(
                default=12.5, range=(2.0, 40.0)
            )
            buoyancy_per_point: float = serialized_field(
                default=3100.0, range=(500.0, 8000.0)
            )
            playback: float = serialized_field(
                default=0.78, range=(0.0, 2.0)
            )
            controls_enabled: bool = serialized_field(default=True)
            new_tuning: float = serialized_field(
                default=7.5, range=(3.0, 20.0)
            )

            def marker(self):
                return "new"
        """
    ).encode("utf-8")
    path.write_bytes(candidate)

    batch = manager.prepare_script_reload_batch((
        ScriptReloadBatchInput(str(path), guid, candidate),
    ))
    outcome = manager.commit_script_reload_batch(batch)
    manager.finalize_script_reload_batch(batch)

    assert outcome.success is True
    assert component.marker() == "new"
    assert component.engine_force == 7111.0
    assert component.cruise_speed == 18.25
    assert component.buoyancy_per_point == 4321.0
    assert component.playback == 0.78
    assert component.controls_enabled is False
    assert component.new_tuning == 7.5


def test_schema_reload_preserves_live_python_descriptor_values_without_cds(
    component_script,
    monkeypatch,
):
    guid = "schema-python-value-preservation-guid"
    path, (component_type,) = component_script(
        "SchemaPythonValuePreservationProbe.py",
        """
        from Infernux.components import InxComponent, serialized_field

        class SchemaPythonValuePreservationProbe(InxComponent):
            _uses_component_data_store = False
            intensity: float = serialized_field(
                default=6.5, range=(1.0, 20.0)
            )

            def marker(self):
                return "old"
        """,
        guid,
    )
    component = component_type()
    component._script_guid = guid
    component.intensity = 13.25
    manager = _play_manager(monkeypatch, path, guid, (component,))

    candidate = textwrap.dedent(
        """
        from Infernux.components import InxComponent, serialized_field

        class SchemaPythonValuePreservationProbe(InxComponent):
            _uses_component_data_store = False
            intensity: float = serialized_field(
                default=6.5, range=(1.0, 20.0)
            )
            new_intensity: float = serialized_field(
                default=8.25, range=(4.0, 20.0)
            )

            def marker(self):
                return "new"
        """
    ).encode("utf-8")
    path.write_bytes(candidate)

    batch = manager.prepare_script_reload_batch((
        ScriptReloadBatchInput(str(path), guid, candidate),
    ))
    outcome = manager.commit_script_reload_batch(batch)
    manager.finalize_script_reload_batch(batch)

    assert outcome.success is True
    assert component.marker() == "new"
    assert component.intensity == 13.25
    assert component.new_intensity == 8.25


def test_schema_reload_refreshes_serialized_metadata_after_add_and_remove(
    component_script,
    monkeypatch,
):
    guid = "schema-metadata-refresh-guid"
    path, (component_type,) = component_script(
        "SchemaMetadataRefreshProbe.py",
        """
        from Infernux.components import InxComponent

        class SchemaMetadataRefreshProbe(InxComponent):
            retained: int = 1
            removed: float = 2.0
        """,
        guid,
    )
    component = component_type()
    component._script_guid = guid
    manager = _play_manager(monkeypatch, path, guid, (component,))

    assert tuple(get_serialized_fields(component_type)) == ("retained", "removed")

    added_source = textwrap.dedent(
        """
        from Infernux.components import InxComponent

        class SchemaMetadataRefreshProbe(InxComponent):
            retained: int = 1
            added: bool = True
        """
    ).encode("utf-8")
    path.write_bytes(added_source)
    batch = manager.prepare_script_reload_batch((
        ScriptReloadBatchInput(str(path), guid, added_source),
    ))
    assert manager.commit_script_reload_batch(batch).success is True
    manager.finalize_script_reload_batch(batch)

    assert tuple(get_serialized_fields(component_type)) == ("retained", "added")
    assert "removed" not in component_type.__dict__

    removed_source = textwrap.dedent(
        """
        from Infernux.components import InxComponent

        class SchemaMetadataRefreshProbe(InxComponent):
            retained: int = 1
        """
    ).encode("utf-8")
    path.write_bytes(removed_source)
    batch = manager.prepare_script_reload_batch((
        ScriptReloadBatchInput(str(path), guid, removed_source),
    ))
    assert manager.commit_script_reload_batch(batch).success is True
    manager.finalize_script_reload_batch(batch)

    assert tuple(get_serialized_fields(component_type)) == ("retained",)
    assert "added" not in component_type.__dict__


def test_play_body_reload_supports_multiple_types(component_script, monkeypatch):
    guid = "play-body-multiple-guid"
    path, classes = component_script(
        "MultipleBodyReload.py",
        """
        from Infernux.components import InxComponent

        class MultiReloadAlpha(InxComponent):
            _uses_component_data_store = False
            value: int = 1
            def helper(self): return "alpha-old"

        class MultiReloadBeta(InxComponent):
            _uses_component_data_store = False
            value: int = 2
            def helper(self): return "beta-old"
        """,
        guid,
    )
    by_name = {component_type.__name__: component_type for component_type in classes}
    alpha = by_name["MultiReloadAlpha"]()
    beta = by_name["MultiReloadBeta"]()
    for component in (alpha, beta):
        component._script_guid = guid
    manager = _play_manager(monkeypatch, path, guid, (alpha, beta))

    _overwrite_preserving_pyc_fingerprint(path, """
        from Infernux.components import InxComponent

        class MultiReloadAlpha(InxComponent):
            _uses_component_data_store = False
            value: int = 1
            def helper(self): return "alpha-new"

        class MultiReloadBeta(InxComponent):
            _uses_component_data_store = False
            value: int = 2
            def helper(self): return "beta-new"
    """)

    assert manager.reload_components_from_script(str(path)) == 2
    assert alpha.helper() == "alpha-new"
    assert beta.helper() == "beta-new"


def test_batch_body_reload_commits_two_scripts_without_recreating_lifecycle_state(
    component_script,
    monkeypatch,
):
    first_guid = "batch-first-guid"
    second_guid = "batch-second-guid"
    first_path, (first_type,) = component_script(
        "BatchFirst.py",
        """
        from Infernux.components import InxComponent

        class BatchFirst(InxComponent):
            _uses_component_data_store = False
            def awake(self):
                self.awake_count = getattr(self, "awake_count", 0) + 1
            def start(self):
                self.start_count = getattr(self, "start_count", 0) + 1
            def helper(self): return "first-old"
        """,
        first_guid,
    )
    second_path, (second_type,) = component_script(
        "BatchSecond.py",
        """
        from Infernux.components import InxComponent

        class BatchSecond(InxComponent):
            _uses_component_data_store = False
            def awake(self):
                self.awake_count = getattr(self, "awake_count", 0) + 1
            def start(self):
                self.start_count = getattr(self, "start_count", 0) + 1
            def helper(self): return "second-old"
        """,
        second_guid,
    )
    first = first_type()
    second = second_type()
    for component, guid in ((first, first_guid), (second, second_guid)):
        component._script_guid = guid
        component._call_awake()
        component._call_start()
    first_native = _NativeDispatchProbe()
    second_native = _NativeDispatchProbe()
    first._cpp_component = first_native
    second._cpp_component = second_native
    first_id, second_id = first.component_id, second.component_id
    first_module_name = get_script_module_name(str(first_path))
    second_module_name = get_script_module_name(str(second_path))
    first_module = sys.modules[first_module_name]
    second_module = sys.modules[second_module_name]
    manager = _play_batch_manager(
        monkeypatch,
        {str(first_path): first_guid, str(second_path): second_guid},
        (first, second),
    )

    first_source = textwrap.dedent("""
        from Infernux.components import InxComponent
        class BatchFirst(InxComponent):
            _uses_component_data_store = False
            def awake(self):
                self.awake_count = getattr(self, "awake_count", 0) + 1
            def start(self):
                self.start_count = getattr(self, "start_count", 0) + 1
            def helper(self): return "first-new"
    """).encode("utf-8")
    second_source = textwrap.dedent("""
        from Infernux.components import InxComponent
        class BatchSecond(InxComponent):
            _uses_component_data_store = False
            def awake(self):
                self.awake_count = getattr(self, "awake_count", 0) + 1
            def start(self):
                self.start_count = getattr(self, "start_count", 0) + 1
            def helper(self): return "second-new"
    """).encode("utf-8")
    first_path.write_bytes(first_source)
    second_path.write_bytes(second_source)

    batch = manager.prepare_script_reload_batch((
        ScriptReloadBatchInput(str(first_path), first_guid, first_source),
        ScriptReloadBatchInput(str(second_path), second_guid, second_source),
    ))
    assert [member.had_live_targets for member in batch.members] == [True, True]
    assert first.helper() == "first-old"
    assert second.helper() == "second-old"

    outcome = manager.commit_script_reload_batch(batch)
    assert outcome.success is True
    assert outcome.had_live_targets is True
    assert outcome.reloaded_count == 2
    assert first.helper() == "first-new"
    assert second.helper() == "second-new"
    assert type(first) is first_type
    assert type(second) is second_type
    assert (first.component_id, second.component_id) == (first_id, second_id)
    assert (first.awake_count, first.start_count) == (1, 1)
    assert (second.awake_count, second.start_count) == (1, 1)
    assert (first_native.refresh_calls, second_native.refresh_calls) == (1, 1)
    assert sys.modules[first_module_name] is not first_module
    assert sys.modules[second_module_name] is not second_module


def test_batch_staging_failure_restores_everything_before_live_mutation(
    component_script,
    monkeypatch,
):
    first_guid = "batch-stage-first-guid"
    second_guid = "batch-stage-second-guid"
    first_path, (first_type,) = component_script(
        "BatchStageFirst.py",
        """
        from Infernux.components import InxComponent
        class BatchStageFirst(InxComponent):
            _uses_component_data_store = False
            def helper(self): return "first-old"
        """,
        first_guid,
    )
    second_path, (second_type,) = component_script(
        "BatchStageSecond.py",
        """
        from Infernux.components import InxComponent
        class BatchStageSecond(InxComponent):
            _uses_component_data_store = False
            def helper(self): return "second-old"
        """,
        second_guid,
    )
    first = first_type()
    second = second_type()
    first._script_guid = first_guid
    second._script_guid = second_guid
    first_module_name = get_script_module_name(str(first_path))
    second_module_name = get_script_module_name(str(second_path))
    first_module = sys.modules[first_module_name]
    second_module = sys.modules[second_module_name]
    manager = _play_batch_manager(
        monkeypatch,
        {str(first_path): first_guid, str(second_path): second_guid},
        (first, second),
    )
    first_source = (
        b"from Infernux.components import InxComponent\n"
        b"class BatchStageFirst(InxComponent):\n"
        b"    _uses_component_data_store = False\n"
        b"    def helper(self): return 'first-new'\n"
    )
    broken_source = b"class BrokenCandidate(\n"
    with pytest.raises(Exception):
        manager.prepare_script_reload_batch((
            ScriptReloadBatchInput(str(first_path), first_guid, first_source),
            ScriptReloadBatchInput(str(second_path), second_guid, broken_source),
        ))

    assert first.helper() == "first-old"
    assert second.helper() == "second-old"
    assert type(first) is first_type
    assert type(second) is second_type
    assert sys.modules[first_module_name] is first_module
    assert sys.modules[second_module_name] is second_module


def test_batch_commit_failure_rolls_back_all_class_bodies_dispatch_and_modules(
    component_script,
    monkeypatch,
):
    first_guid = "batch-commit-first-guid"
    second_guid = "batch-commit-second-guid"
    first_path, (first_type,) = component_script(
        "BatchCommitFirst.py",
        """
        from Infernux.components import InxComponent
        class BatchCommitFirst(InxComponent):
            _uses_component_data_store = False
            def helper(self): return "first-old"
        """,
        first_guid,
    )
    second_path, (second_type,) = component_script(
        "BatchCommitSecond.py",
        """
        from Infernux.components import InxComponent
        class BatchCommitSecond(InxComponent):
            _uses_component_data_store = False
            def helper(self): return "second-old"
        """,
        second_guid,
    )
    first = first_type()
    second = second_type()
    first._script_guid = first_guid
    second._script_guid = second_guid
    first_native = _NativeDispatchProbe()
    second_native = _NativeDispatchProbe()
    first._cpp_component = first_native
    second._cpp_component = second_native
    old_first_dispatch = first_type.__dict__.get("_runtime_phase_invokers")
    old_second_dispatch = second_type.__dict__.get("_runtime_phase_invokers")
    old_first_module = sys.modules[get_script_module_name(str(first_path))]
    old_second_module = sys.modules[get_script_module_name(str(second_path))]
    manager = _play_batch_manager(
        monkeypatch,
        {str(first_path): first_guid, str(second_path): second_guid},
        (first, second),
    )
    first_source = textwrap.dedent("""
        from Infernux.components import InxComponent
        class BatchCommitFirst(InxComponent):
            _uses_component_data_store = False
            def helper(self): return "first-new"
    """).encode("utf-8")
    second_source = textwrap.dedent("""
        from Infernux.components import InxComponent
        class BatchCommitSecond(InxComponent):
            _uses_component_data_store = False
            def helper(self): return "second-new"
    """).encode("utf-8")
    batch = manager.prepare_script_reload_batch((
        ScriptReloadBatchInput(str(first_path), first_guid, first_source),
        ScriptReloadBatchInput(str(second_path), second_guid, second_source),
    ))
    real_apply = script_loader._apply_component_body_patch_plans

    def apply_then_fail(plans, instances_by_type=None):
        real_apply(plans, instances_by_type=instances_by_type)
        raise RuntimeError("simulated owner publish failure")

    monkeypatch.setattr(script_loader, "_apply_component_body_patch_plans", apply_then_fail)
    outcome = manager.commit_script_reload_batch(batch)
    assert outcome.success is False
    assert batch.rolled_back is True
    assert first.helper() == "first-old"
    assert second.helper() == "second-old"
    assert first_type.__dict__.get("_runtime_phase_invokers") is old_first_dispatch
    assert second_type.__dict__.get("_runtime_phase_invokers") is old_second_dispatch
    assert sys.modules[get_script_module_name(str(first_path))] is old_first_module
    assert sys.modules[get_script_module_name(str(second_path))] is old_second_module
    assert (first_native.refresh_calls, second_native.refresh_calls) == (1, 1)


def test_explicit_batch_rollback_restores_successfully_published_bodies(
    component_script,
    monkeypatch,
):
    first_guid = "batch-explicit-first-guid"
    second_guid = "batch-explicit-second-guid"
    first_path, (first_type,) = component_script(
        "BatchExplicitFirst.py",
        """
        from Infernux.components import InxComponent
        class BatchExplicitFirst(InxComponent):
            _uses_component_data_store = False
            def helper(self): return "first-old"
        """,
        first_guid,
    )
    second_path, (second_type,) = component_script(
        "BatchExplicitSecond.py",
        """
        from Infernux.components import InxComponent
        class BatchExplicitSecond(InxComponent):
            _uses_component_data_store = False
            def helper(self): return "second-old"
        """,
        second_guid,
    )
    first = first_type()
    second = second_type()
    first._script_guid = first_guid
    second._script_guid = second_guid
    manager = _play_batch_manager(
        monkeypatch,
        {str(first_path): first_guid, str(second_path): second_guid},
        (first, second),
    )
    first_source = b"from Infernux.components import InxComponent\nclass BatchExplicitFirst(InxComponent):\n    _uses_component_data_store = False\n    def helper(self): return 'first-new'\n"
    second_source = b"from Infernux.components import InxComponent\nclass BatchExplicitSecond(InxComponent):\n    _uses_component_data_store = False\n    def helper(self): return 'second-new'\n"
    batch = manager.prepare_script_reload_batch((
        ScriptReloadBatchInput(str(first_path), first_guid, first_source),
        ScriptReloadBatchInput(str(second_path), second_guid, second_source),
    ))
    assert manager.commit_script_reload_batch(batch).success is True
    assert first.helper() == "first-new"
    assert second.helper() == "second-new"

    manager.rollback_script_reload_batch(batch)
    assert first.helper() == "first-old"
    assert second.helper() == "second-old"
    assert type(first) is first_type
    assert type(second) is second_type


def test_batch_rollback_does_not_remove_unrelated_module_imported_after_staging(
    component_script,
    monkeypatch,
    tmp_path,
):
    guid = "batch-module-scope-guid"
    path, (component_type,) = component_script(
        "BatchModuleScope.py",
        """
        from Infernux.components import InxComponent
        class BatchModuleScope(InxComponent):
            _uses_component_data_store = False
            def helper(self): return "old"
        """,
        guid,
    )
    component = component_type()
    component._script_guid = guid
    manager = _play_manager(monkeypatch, path, guid, (component,))
    candidate = b"from Infernux.components import InxComponent\nclass BatchModuleScope(InxComponent):\n    _uses_component_data_store = False\n    def helper(self): return 'new'\n"
    batch = manager.prepare_script_reload_batch((
        ScriptReloadBatchInput(str(path), guid, candidate),
    ))

    module_name = "unrelated_module_after_staging"
    module_path = tmp_path / f"{module_name}.py"
    module_path.write_text("value = 17\n", encoding="utf-8")
    sys.path.insert(0, str(tmp_path))
    sys.modules.pop(module_name, None)
    try:
        unrelated = importlib.import_module(module_name)
        manager.rollback_script_reload_batch(batch)
        assert sys.modules[module_name] is unrelated
        assert unrelated.value == 17
    finally:
        sys.modules.pop(module_name, None)
        sys.path.remove(str(tmp_path))


def test_batch_reloads_helper_before_live_component_atomically(
    component_script,
    monkeypatch,
):
    live_guid = "batch-live-target-guid"
    helper_path, _ = component_script(
        "helper.py",
        """
        VALUE = "old"
        """,
        expect_components=False,
    )
    live_path, (live_type,) = component_script(
        "BatchLiveTarget.py",
        """
        import helper
        from Infernux.components import InxComponent
        class BatchLiveTarget(InxComponent):
            _uses_component_data_store = False
            def helper_value(self): return helper.VALUE
        """,
        live_guid,
    )
    live = live_type()
    live._script_guid = live_guid
    manager = _play_batch_manager(
        monkeypatch,
        {str(helper_path): "", str(live_path): live_guid},
        (live,),
    )
    assert live.helper_value() == "old"
    helper_source = b"VALUE = 'new'\n"
    live_source = (
        b"import helper\n"
        b"from Infernux.components import InxComponent\n"
        b"class BatchLiveTarget(InxComponent):\n"
        b"    _uses_component_data_store = False\n"
        b"    def helper_value(self): return helper.VALUE\n"
    )

    batch = manager.prepare_script_reload_batch((
        ScriptReloadBatchInput(str(helper_path), "", helper_source),
        ScriptReloadBatchInput(str(live_path), live_guid, live_source),
    ))
    assert [(member.had_live_targets, member.target_count) for member in batch.members] == [
        (False, 0),
        (True, 1),
    ]
    assert live.helper_value() == "old"
    outcome = manager.commit_script_reload_batch(batch)
    assert outcome.success is True
    assert live.helper_value() == "new"
    assert sys.modules[get_script_module_name(str(helper_path))].VALUE == "new"


def test_helper_candidate_top_level_failure_rolls_back_the_whole_batch(
    component_script,
    monkeypatch,
):
    live_guid = "batch-helper-failure-live-guid"
    helper_path, _ = component_script(
        "failure_helper.py",
        """
        VALUE = "old"
        """,
        expect_components=False,
    )
    live_path, (live_type,) = component_script(
        "BatchHelperFailureLive.py",
        """
        import failure_helper
        from Infernux.components import InxComponent
        class BatchHelperFailureLive(InxComponent):
            _uses_component_data_store = False
            def helper_value(self): return failure_helper.VALUE
        """,
        live_guid,
    )
    live = live_type()
    live._script_guid = live_guid
    helper_module_name = get_script_module_name(str(helper_path))
    live_module_name = get_script_module_name(str(live_path))
    old_helper_module = sys.modules[helper_module_name]
    old_live_module = sys.modules[live_module_name]
    manager = _play_batch_manager(
        monkeypatch,
        {str(helper_path): "", str(live_path): live_guid},
        (live,),
    )
    with pytest.raises(Exception):
        manager.prepare_script_reload_batch((
            ScriptReloadBatchInput(
                str(helper_path),
                "",
                b"VALUE = 'new'\nraise RuntimeError('helper import failed')\n",
            ),
            ScriptReloadBatchInput(
                str(live_path),
                live_guid,
                b"import failure_helper\n"
                b"from Infernux.components import InxComponent\n"
                b"class BatchHelperFailureLive(InxComponent):\n"
                b"    _uses_component_data_store = False\n"
                b"    def helper_value(self): return failure_helper.VALUE\n",
            ),
        ))

    assert live.helper_value() == "old"
    assert sys.modules[helper_module_name] is old_helper_module
    assert sys.modules[live_module_name] is old_live_module


def test_scc_batch_reuses_recursively_loaded_candidate_modules(
    component_script,
    monkeypatch,
):
    first_guid = "scc-first-guid"
    second_guid = "scc-second-guid"
    first_path, (first_type,) = component_script(
        "scc_a.py",
        """
        from Infernux.components import InxComponent
        class SccA(InxComponent):
            _uses_component_data_store = False
            def behavior(self): return "A-old"
        """,
        first_guid,
    )
    second_path, (second_type,) = component_script(
        "scc_b.py",
        """
        from Infernux.components import InxComponent
        class SccB(InxComponent):
            _uses_component_data_store = False
            def behavior(self): return "B-old"
        """,
        second_guid,
    )
    first = first_type()
    second = second_type()
    first._script_guid = first_guid
    second._script_guid = second_guid
    manager = _play_batch_manager(
        monkeypatch,
        {str(first_path): first_guid, str(second_path): second_guid},
        (first, second),
    )
    first_source = textwrap.dedent("""
        VALUE = "A-new"
        import scc_b
        from Infernux.components import InxComponent
        class SccA(InxComponent):
            _uses_component_data_store = False
            def behavior(self): return VALUE + "/" + scc_b.VALUE
            def peer_module(self): return scc_b
    """).encode("utf-8")
    second_source = textwrap.dedent("""
        VALUE = "B-new"
        import scc_a
        from Infernux.components import InxComponent
        class SccB(InxComponent):
            _uses_component_data_store = False
            def behavior(self): return VALUE + "/" + scc_a.VALUE
            def peer_module(self): return scc_a
    """).encode("utf-8")
    first_path.write_bytes(first_source)
    second_path.write_bytes(second_source)

    batch = manager.prepare_script_reload_batch((
        ScriptReloadBatchInput(str(first_path), first_guid, first_source),
        ScriptReloadBatchInput(str(second_path), second_guid, second_source),
    ))
    assert first.behavior() == "A-old"
    assert second.behavior() == "B-old"
    assert manager.commit_script_reload_batch(batch).success is True

    first_module = sys.modules[get_script_module_name(str(first_path))]
    second_module = sys.modules[get_script_module_name(str(second_path))]
    assert first.behavior() == "A-new/B-new"
    assert second.behavior() == "B-new/A-new"
    assert first.peer_module() is second_module
    assert second.peer_module() is first_module
    assert first_module.SccA is first_type
    assert second_module.SccB is second_type


def test_scc_second_member_validation_failure_restores_both_old_modules(
    component_script,
    monkeypatch,
):
    first_guid = "scc-failure-first-guid"
    second_guid = "scc-failure-second-guid"
    first_path, (first_type,) = component_script(
        "scc_failure_a.py",
        """
        from Infernux.components import InxComponent
        class SccFailureA(InxComponent):
            _uses_component_data_store = False
            def behavior(self): return "A-old"
        """,
        first_guid,
    )
    second_path, (second_type,) = component_script(
        "scc_failure_b.py",
        """
        from Infernux.components import InxComponent
        class SccFailureB(InxComponent):
            _uses_component_data_store = False
            value: float = 1.0
            def behavior(self): return "B-old"
        """,
        second_guid,
    )
    first = first_type()
    second = second_type()
    first._script_guid = first_guid
    second._script_guid = second_guid
    first_module_name = get_script_module_name(str(first_path))
    second_module_name = get_script_module_name(str(second_path))
    old_first_module = sys.modules[first_module_name]
    old_second_module = sys.modules[second_module_name]
    manager = _play_batch_manager(
        monkeypatch,
        {str(first_path): first_guid, str(second_path): second_guid},
        (first, second),
    )
    first_source = textwrap.dedent("""
        VALUE = "A-new"
        import scc_failure_b
        from Infernux.components import InxComponent
        class SccFailureA(InxComponent):
            _uses_component_data_store = False
            def behavior(self): return VALUE + "/" + scc_failure_b.VALUE
    """).encode("utf-8")
    second_source = textwrap.dedent("""
        VALUE = "B-new"
        import scc_failure_a
        from Infernux.components import InxComponent
        class SccFailureB(InxComponent):
            _uses_component_data_store = False
            value: int = 1
            def behavior(self): return VALUE + "/" + scc_failure_a.VALUE
    """).encode("utf-8")
    first_path.write_bytes(first_source)
    second_path.write_bytes(second_source)

    with pytest.raises(Exception):
        manager.prepare_script_reload_batch((
            ScriptReloadBatchInput(str(first_path), first_guid, first_source),
            ScriptReloadBatchInput(str(second_path), second_guid, second_source),
        ))

    assert first.behavior() == "A-old"
    assert second.behavior() == "B-old"
    assert sys.modules[first_module_name] is old_first_module
    assert sys.modules[second_module_name] is old_second_module


def test_staging_and_commit_leave_diagnostics_for_outer_collector_cleanup(
    component_script,
    monkeypatch,
):
    guid = "diagnostic-success-guid"
    path, (component_type,) = component_script(
        "DiagnosticSuccess.py",
        """
        from Infernux.components import InxComponent
        class DiagnosticSuccess(InxComponent):
            _uses_component_data_store = False
            def helper(self): return "old"
        """,
        guid,
    )
    component = component_type()
    component._script_guid = guid
    manager = _play_manager(monkeypatch, path, guid, (component,))
    old_diagnostic = "previous durable diagnostic"
    set_script_error(str(path), old_diagnostic)
    old_revision = get_script_error_revision()
    candidate = (
        b"from Infernux.components import InxComponent\n"
        b"class DiagnosticSuccess(InxComponent):\n"
        b"    _uses_component_data_store = False\n"
        b"    def helper(self): return 'new'\n"
    )

    batch = manager.prepare_script_reload_batch((
        ScriptReloadBatchInput(str(path), guid, candidate),
    ))
    assert get_script_error_by_path(str(path)) == old_diagnostic
    assert get_script_error_revision() == old_revision
    assert manager.commit_script_reload_batch(batch).success is True
    assert component.helper() == "new"
    assert get_script_error_by_path(str(path)) == old_diagnostic
    assert get_script_error_revision() == old_revision

    # Model the outer collector clearing diagnostics after its own commit. If
    # a later graph publication fails, the live transaction must restore them.
    _clear_script_error(str(path))
    assert get_script_error_by_path(str(path)) is None
    manager.rollback_script_reload_batch(batch)
    assert component.helper() == "old"
    assert get_script_error_by_path(str(path)) == old_diagnostic
    assert get_script_error_revision() == old_revision


def test_schema_failure_restores_diagnostic_contents_and_revision(
    component_script,
    monkeypatch,
):
    guid = "diagnostic-failure-guid"
    path, (component_type,) = component_script(
        "DiagnosticFailure.py",
        """
        from Infernux.components import InxComponent
        class DiagnosticFailure(InxComponent):
            _uses_component_data_store = False
            value: float = 1.0
            def helper(self): return "old"
        """,
        guid,
    )
    component = component_type()
    component._script_guid = guid
    manager = _play_manager(monkeypatch, path, guid, (component,))
    old_diagnostic = "diagnostic before rejected candidate"
    set_script_error(str(path), old_diagnostic)
    old_revision = get_script_error_revision()
    incompatible = (
        b"from Infernux.components import InxComponent\n"
        b"class DiagnosticFailure(InxComponent):\n"
        b"    _uses_component_data_store = False\n"
        b"    value: int = 1\n"
        b"    def helper(self): return 'new'\n"
    )

    with pytest.raises(Exception):
        manager.prepare_script_reload_batch((
            ScriptReloadBatchInput(str(path), guid, incompatible),
        ))

    assert component.helper() == "old"
    assert get_script_error_by_path(str(path)) == old_diagnostic
    assert get_script_error_revision() == old_revision


@pytest.mark.parametrize("state", (PlayModeState.EDIT, PlayModeState.PLAYING))
def test_reload_result_uses_frontend_code_in_edit_and_play_without_recompile(
    component_script,
    monkeypatch,
    state,
):
    guid = f"frontend-code-{state.name.lower()}-guid"
    path, (component_type,) = component_script(
        f"FrontendCode{state.name.title()}Probe.py",
        """
        from Infernux.components import InxComponent
        class FrontendCodeProbe(InxComponent):
            _uses_component_data_store = False
            def helper(self): return "old"
        """,
        guid,
    )
    component = component_type()
    component._script_guid = guid
    manager = _play_manager(monkeypatch, path, guid, (component,))
    manager._state = state

    candidate = textwrap.dedent(
        """
        from Infernux.components import InxComponent
        class FrontendCodeProbe(InxComponent):
            _uses_component_data_store = False
            def helper(self): return "new"
        """
    ).encode("utf-8")
    path.write_bytes(candidate)
    frontend_code = compile(candidate, str(path), "exec", dont_inherit=True)

    def unexpected_compile(*_args, **_kwargs):
        raise AssertionError("owner publish must execute the worker CodeType")

    monkeypatch.setattr(script_loader, "compile", unexpected_compile, raising=False)
    outcome = manager.reload_components_from_script_result(
        str(path),
        source=candidate,
        code=frontend_code,
    )

    assert outcome.success is True
    if state is PlayModeState.PLAYING:
        assert component.helper() == "new"
    else:
        live = manager._get_scene_manager().get_active_scene().get_all_objects()[0]
        assert live.get_py_components()[0].helper() == "new"


def test_multi_type_validation_rejects_all_before_first_publish(
    component_script,
    monkeypatch,
):
    guid = "play-body-atomic-guid"
    path, classes = component_script(
        "AtomicBodyReload.py",
        """
        from Infernux.components import InxComponent

        class AtomicReloadAlpha(InxComponent):
            _uses_component_data_store = False
            value: int = 1
            def helper(self): return "alpha-old"

        class AtomicReloadBeta(InxComponent):
            _uses_component_data_store = False
            value: int = 2
            def helper(self): return "beta-old"
        """,
        guid,
    )
    by_name = {component_type.__name__: component_type for component_type in classes}
    alpha = by_name["AtomicReloadAlpha"]()
    beta = by_name["AtomicReloadBeta"]()
    alpha_native = _NativeDispatchProbe()
    beta_native = _NativeDispatchProbe()
    alpha._cpp_component = alpha_native
    beta._cpp_component = beta_native
    for component in (alpha, beta):
        component._script_guid = guid
    manager = _play_manager(monkeypatch, path, guid, (alpha, beta))

    path.write_text(textwrap.dedent("""
        from Infernux.components import InxComponent

        class AtomicReloadAlpha(InxComponent):
            _uses_component_data_store = False
            value: int = 1
            def helper(self): return "alpha-new"

        class AtomicReloadBetaRenamed(InxComponent):
            _uses_component_data_store = False
            value: int = 2
            def helper(self): return "beta-new"
    """), encoding="utf-8")

    assert manager.reload_components_from_script(str(path)) == 0
    assert alpha.helper() == "alpha-old"
    assert beta.helper() == "beta-old"
    assert alpha_native.refresh_calls == 0
    assert beta_native.refresh_calls == 0


def test_play_batch_without_live_instances_publishes_candidate_registry_and_rolls_back(
    component_script,
    monkeypatch,
):
    guid = "play-no-live-registry-guid"
    path, (old_type,) = component_script(
        "NoLiveRegistryProbe.py",
        """
        from Infernux.components import InxComponent
        class NoLiveRegistryProbe(InxComponent):
            _uses_component_data_store = False
            def marker(self): return "old"
        """,
        guid,
    )
    assert get_type("NoLiveRegistryProbe") is old_type
    manager = _play_batch_manager(monkeypatch, {str(path): guid}, ())
    candidate_source = (
        b"from Infernux.components import InxComponent\n"
        b"class NoLiveRegistryProbe(InxComponent):\n"
        b"    _uses_component_data_store = False\n"
        b"    def marker(self): return 'candidate'\n"
    )

    batch = manager.prepare_script_reload_batch((
        ScriptReloadBatchInput(str(path), guid, candidate_source),
    ))
    outcome = manager.commit_script_reload_batch(batch)
    assert outcome.success is True
    candidate_type = get_type("NoLiveRegistryProbe")
    assert candidate_type is not old_type
    assert candidate_type().marker() == "candidate"
    assert get_type_by_identity(
        "NoLiveRegistryProbe", guid, candidate_type._get_type_guid()
    ) is candidate_type

    manager.rollback_script_reload_batch(batch)
    assert get_type("NoLiveRegistryProbe") is old_type
    assert old_type().marker() == "old"


def test_edit_reload_keeps_stable_instances_and_retries_after_body_publish_failure(
    component_script,
    monkeypatch,
):
    guid = "edit-atomic-replacement-guid"
    path, (old_type,) = component_script(
        "EditAtomicReplacementProbe.py",
        """
        from Infernux.components import InxComponent
        class EditAtomicReplacementProbe(InxComponent):
            _uses_component_data_store = False
            value: int = 7
            def marker(self): return "old"
        """,
        guid,
    )
    first = old_type()
    second = old_type()
    first._script_guid = guid
    second._script_guid = guid
    first_object = _AtomicScriptObject(101, (first,))
    second_object = _AtomicScriptObject(102, (second,))
    manager, scene = _edit_multi_object_manager(
        monkeypatch,
        path,
        guid,
        (first_object, second_object),
    )
    candidate_source = (
        b"from Infernux.components import InxComponent\n"
        b"class EditAtomicReplacementProbe(InxComponent):\n"
        b"    _uses_component_data_store = False\n"
        b"    value: int = 7\n"
        b"    def marker(self): return 'candidate'\n"
    )
    path.write_bytes(candidate_source)

    batch = manager.prepare_edit_script_reload_batch((
        ScriptReloadBatchInput(str(path), guid, candidate_source),
    ))
    assert first_object.get_py_components()[0] is first
    assert second_object.get_py_components()[0] is second
    assert first.marker() == "old"
    assert second.marker() == "old"

    real_apply = script_loader._apply_component_body_patch_plans

    def apply_then_fail(plans, instances_by_type=None):
        real_apply(plans, instances_by_type=instances_by_type)
        raise RuntimeError("simulated Edit body publication failure")

    monkeypatch.setattr(script_loader, "_apply_component_body_patch_plans", apply_then_fail)

    with pytest.raises(RuntimeError, match="simulated Edit body publication failure"):
        manager.commit_edit_script_reload_batch(batch)
    assert batch.rolled_back is True
    assert first_object.get_py_components()[0] is first
    assert second_object.get_py_components()[0] is second
    assert get_type("EditAtomicReplacementProbe") is old_type
    assert first.marker() == "old"
    assert second.marker() == "old"

    # The failed LKG transaction must be retryable after the external failure
    # is removed; no half-published registry or module is allowed to remain.
    monkeypatch.setattr(script_loader, "_apply_component_body_patch_plans", real_apply)
    retry = manager.prepare_edit_script_reload_batch((
        ScriptReloadBatchInput(str(path), guid, candidate_source),
    ))
    assert manager.commit_edit_script_reload_batch(retry) == 2
    assert retry.transaction.finalized is True
    assert first_object.get_py_components()[0] is first
    assert second_object.get_py_components()[0] is second
    assert type(first) is old_type
    assert type(second) is old_type
    assert all(
        component.marker() == "candidate"
        for obj in scene.get_all_objects()
        for component in obj.get_py_components()
    )


def test_edit_reload_publishes_component_free_helper_and_dependent_atomically(
    component_script,
    monkeypatch,
):
    """A real Assets helper must not be rejected as an empty component script."""
    helper_path, helper_classes = component_script(
        "runtime_r13_helper.py",
        "VALUE = 1\n",
        "runtime-r13-helper-guid",
        expect_components=False,
    )
    probe_path, (probe_type,) = component_script(
        "runtime_r13_probe.py",
        """
        from Infernux.components import InxComponent
        from runtime_r13_helper import VALUE

        class RuntimeR13Probe(InxComponent):
            _uses_component_data_store = False
            value: int = VALUE

            def marker(self):
                import runtime_r13_helper
                return runtime_r13_helper.VALUE
        """,
        "runtime-r13-probe-guid",
    )
    component = probe_type()
    component._script_guid = "runtime-r13-probe-guid"
    scene_object = _AtomicScriptObject(901, (component,))
    manager = PlayModeManager()
    manager._state = PlayModeState.EDIT
    manager.set_asset_database(
        _ScriptBatchAssetDatabase(
            {
                str(helper_path.resolve()): "runtime-r13-helper-guid",
                str(probe_path.resolve()): "runtime-r13-probe-guid",
            }
        )
    )
    scene = _MultiScriptScene((scene_object,))
    monkeypatch.setattr(
        manager,
        "_get_scene_manager",
        lambda: _ScriptSceneManager(scene),
    )

    helper_source = b"VALUE = 2\n"
    probe_source = (
        b"from Infernux.components import InxComponent\n"
        b"from runtime_r13_helper import VALUE\n"
        b"class RuntimeR13Probe(InxComponent):\n"
        b"    _uses_component_data_store = False\n"
        b"    value: int = VALUE\n"
        b"    def marker(self):\n"
        b"        import runtime_r13_helper\n"
        b"        return runtime_r13_helper.VALUE\n"
    )
    batch = manager.prepare_edit_script_reload_batch(
        (
            ScriptReloadBatchInput(
                str(helper_path), "runtime-r13-helper-guid", helper_source
            ),
            ScriptReloadBatchInput(
                str(probe_path), "runtime-r13-probe-guid", probe_source
            ),
        )
    )
    assert sys.modules["runtime_r13_helper"].VALUE == 1
    assert scene_object.get_py_components()[0] is component
    assert component.marker() == 1
    assert manager.commit_edit_script_reload_batch(batch) == 1
    assert batch.transaction.finalized is True
    assert scene_object.get_py_components()[0] is component
    assert type(component) is probe_type
    assert component.marker() == 2

    # A failed helper candidate is rejected before the dependent component is
    # replaced, and the already-published component remains the LKG instance.
    broken_helper = b"def broken(:\n"
    with pytest.raises(Exception):
        manager.prepare_edit_script_reload_batch(
            (
                ScriptReloadBatchInput(
                    str(helper_path), "runtime-r13-helper-guid", broken_helper
                ),
                ScriptReloadBatchInput(
                    str(probe_path), "runtime-r13-probe-guid", probe_source
                ),
            )
        )
    assert scene_object.get_py_components()[0].marker() == 2


@pytest.mark.parametrize(
    "broken_source",
    (
        "class BrokenReload(\n",
        """
        from Infernux.components import InxComponent
        class ReloadFailureProbe(InxComponent):
            _uses_component_data_store = False
            value: int = 1
            def helper(self): return "candidate"
        raise RuntimeError("candidate import failed")
        """,
    ),
)
def test_syntax_and_import_failure_keep_lkg_and_registry(
    component_script,
    monkeypatch,
    broken_source,
):
    guid = "play-body-lkg-guid"
    path, (component_type,) = component_script(
        "ReloadFailureProbe.py",
        """
        from Infernux.components import InxComponent
        class ReloadFailureProbe(InxComponent):
            _uses_component_data_store = False
            value: int = 1
            def helper(self): return "last-known-good"
        """,
        guid,
    )
    component = component_type()
    component._script_guid = guid
    manager = _play_manager(monkeypatch, path, guid, (component,))
    path.write_text(textwrap.dedent(broken_source), encoding="utf-8")

    assert manager.reload_components_from_script(str(path)) == 0
    assert component.helper() == "last-known-good"
    assert get_type_by_identity(
        component_type.__name__,
        guid,
        component_type._get_type_guid(),
    ) is component_type


@pytest.mark.parametrize(
    "candidate_source",
    (
        """
        from Infernux.components import InxComponent
        class ReplacementBase(InxComponent):
            _uses_component_data_store = False
        class RejectionProbe(ReplacementBase):
            _uses_component_data_store = False
            value: int = 1
            def helper(self): return "new"
        """,
        """
        from Infernux.components import InxComponent
        class RenamedRejectionProbe(InxComponent):
            _uses_component_data_store = False
            value: int = 1
            def helper(self): return "new"
        """,
    ),
)
def test_schema_base_and_type_rename_are_rejected(
    component_script,
    monkeypatch,
    candidate_source,
):
    guid = "play-body-rejection-guid"
    path, (component_type,) = component_script(
        "RejectionProbe.py",
        """
        from Infernux.components import InxComponent
        class RejectionProbe(InxComponent):
            _uses_component_data_store = False
            value: int = 1
            def helper(self): return "old"
        """,
        guid,
    )
    component = component_type()
    component._script_guid = guid
    manager = _play_manager(monkeypatch, path, guid, (component,))
    path.write_text(textwrap.dedent(candidate_source), encoding="utf-8")

    outcome = manager.reload_components_from_script_result(str(path))
    assert outcome.success is False
    assert outcome.had_live_targets is True
    assert outcome.reloaded_count == 0
    assert manager.reload_components_from_script(str(path)) == 0
    assert type(component) is component_type
    assert component.helper() == "old"


def test_script_delete_batch_is_transactional_in_edit_and_play(
    component_script,
    monkeypatch,
):
    from Infernux.engine.play_mode import PlayModeManager, PlayModeState

    guid = "delete-batch-guid"
    path, (component_type,) = component_script(
        "DeleteBatchProbe.py",
        """
        from Infernux.components import InxComponent
        class DeleteBatchProbe(InxComponent):
            _uses_component_data_store = False
            value: int = 7
        """,
        guid,
    )

    first = component_type()
    first._script_guid = guid
    second = component_type()
    second._script_guid = guid

    class _Object:
        def __init__(self, object_id, component, fail=False):
            self.id = object_id
            self._components = [component]
            self.fail = fail

        def get_py_components(self):
            return tuple(self._components)

        def replace_py_component(self, old, new):
            if self.fail:
                raise RuntimeError("delete replacement failed")
            self._components[self._components.index(old)] = new
            return new

    first_obj = _Object(501, first)
    second_obj = _Object(502, second)

    class _Scene:
        def __init__(self, objects):
            self.objects = tuple(objects)

        def get_all_objects(self):
            return self.objects

        def find_by_id(self, object_id):
            return next((obj for obj in self.objects if obj.id == object_id), None)

    class _SceneManager:
        def __init__(self, scene):
            self.scene = scene

        def get_active_scene(self):
            return self.scene

    manager = PlayModeManager()
    manager._state = PlayModeState.EDIT
    scene = _Scene((first_obj,))
    monkeypatch.setattr(manager, "_get_scene_manager", lambda: _SceneManager(scene))
    batch = manager.prepare_script_delete_batch(guid, str(path))
    assert manager.commit_script_delete_batch(batch) == 1
    from Infernux.components.missing_script import MissingScript
    assert isinstance(first_obj.get_py_components()[0], MissingScript)

    # The same owner API is used in Play/Pause, and a failure after one
    # replacement restores the first object's old component as well.
    manager._state = PlayModeState.PLAYING
    first_obj = _Object(501, first)
    failing_obj = _Object(502, second, fail=True)
    scene = _Scene((first_obj, failing_obj))
    monkeypatch.setattr(manager, "_get_scene_manager", lambda: _SceneManager(scene))
    batch = manager.prepare_script_delete_batch(guid, str(path))
    with pytest.raises(RuntimeError, match="delete replacement failed"):
        manager.commit_script_delete_batch(batch)
    assert first_obj.get_py_components() == (first,)
    assert failing_obj.get_py_components() == (second,)


def test_batch_rebinds_cross_module_candidate_types_to_stable_live_identity(
    component_script,
    monkeypatch,
):
    peer_guid = "stable-cross-module-peer-guid"
    peer_path, (peer_type,) = component_script(
        "stable_cross_module_peer.py",
        """
        from Infernux.components import InxComponent

        class StableCrossModulePeer(InxComponent):
            _uses_component_data_store = False
            def marker(self): return "peer-old"
        """,
        peer_guid,
    )
    owner_guid = "stable-cross-module-owner-guid"
    owner_path, (owner_type,) = component_script(
        "stable_cross_module_owner.py",
        """
        from Infernux.components import InxComponent
        from stable_cross_module_peer import StableCrossModulePeer

        def _captured_peer_method():
            captured = StableCrossModulePeer
            def captured_type(self): return captured
            return captured_type

        class StableCrossModuleOwner(InxComponent):
            _uses_component_data_store = False
            def imported_type(self): return StableCrossModulePeer
            def default_type(self, expected=StableCrossModulePeer): return expected
            @staticmethod
            def static_type(expected=StableCrossModulePeer): return expected
            captured_type = _captured_peer_method()
            def marker(self): return "owner-old"
        """,
        owner_guid,
    )
    peer = peer_type()
    owner = owner_type()
    peer._script_guid = peer_guid
    owner._script_guid = owner_guid
    manager = _play_batch_manager(
        monkeypatch,
        {str(owner_path): owner_guid, str(peer_path): peer_guid},
        (owner, peer),
    )
    owner_source = textwrap.dedent("""
        from Infernux.components import InxComponent
        from stable_cross_module_peer import StableCrossModulePeer

        def _captured_peer_method():
            captured = StableCrossModulePeer
            def captured_type(self): return captured
            return captured_type

        class StableCrossModuleOwner(InxComponent):
            _uses_component_data_store = False
            def imported_type(self): return StableCrossModulePeer
            def default_type(self, expected=StableCrossModulePeer): return expected
            @staticmethod
            def static_type(expected=StableCrossModulePeer): return expected
            captured_type = _captured_peer_method()
            def marker(self): return "owner-new"
    """).encode("utf-8")
    peer_source = textwrap.dedent("""
        from Infernux.components import InxComponent

        class StableCrossModulePeer(InxComponent):
            _uses_component_data_store = False
            def marker(self): return "peer-new"
    """).encode("utf-8")
    owner_path.write_bytes(owner_source)
    peer_path.write_bytes(peer_source)

    batch = manager.prepare_script_reload_batch((
        ScriptReloadBatchInput(str(owner_path), owner_guid, owner_source),
        ScriptReloadBatchInput(str(peer_path), peer_guid, peer_source),
    ))
    assert manager.commit_script_reload_batch(batch).success is True

    owner_module = sys.modules[get_script_module_name(str(owner_path))]
    assert owner.marker() == "owner-new"
    assert peer.marker() == "peer-new"
    assert owner_module.StableCrossModulePeer is peer_type
    assert owner.imported_type() is peer_type
    assert owner.default_type() is peer_type
    assert owner.static_type() is peer_type
    assert owner.captured_type() is peer_type
    assert isinstance(peer, owner.imported_type())
    assert type(owner) is owner_type
    assert type(peer) is peer_type
