from __future__ import annotations

import os
import sys
import textwrap
from types import SimpleNamespace

import pytest

from Infernux.components.component_identity import bind_asset_script_guid
from Infernux.components.registry import (
    get_type_by_identity,
    publish_component_script_types,
    restore_component_script_registry,
    snapshot_component_script_registry,
)
from Infernux.components.script_loader import (
    _clear_script_error,
    load_all_components_from_file,
)
from Infernux.engine.play_mode import PlayModeManager, PlayModeState
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
        self._components = list(components)

    def get_py_components(self):
        return tuple(self._components)


class _ScriptScene:
    def __init__(self, components) -> None:
        self._objects = (_ScriptObject(components),)

    def get_all_objects(self):
        return self._objects


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


@pytest.fixture
def component_script(tmp_path):
    previous_root = get_project_root()
    project = tmp_path / "Project"
    assets = project / "Assets"
    assets.mkdir(parents=True)
    set_project_root(str(project))
    cleanups = []

    def create(name: str, source: str, guid: str):
        path = assets / name
        snapshot = snapshot_component_script_registry(str(path))
        module_name = get_script_module_name(str(path))
        assert module_name
        previous_module = sys.modules.get(module_name)
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        classes = tuple(load_all_components_from_file(str(path), register=False))
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

        class AtomicReloadBeta(InxComponent):
            _uses_component_data_store = False
            value: int = 2
            added: int = 3
            def helper(self): return "beta-new"
    """), encoding="utf-8")

    assert manager.reload_components_from_script(str(path)) == 0
    assert alpha.helper() == "alpha-old"
    assert beta.helper() == "beta-old"
    assert alpha_native.refresh_calls == 0
    assert beta_native.refresh_calls == 0


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
        class RejectionProbe(InxComponent):
            _uses_component_data_store = False
            value: int = 1
            added: int = 2
            def helper(self): return "new"
        """,
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

    assert manager.reload_components_from_script(str(path)) == 0
    assert type(component) is component_type
    assert component.helper() == "old"
