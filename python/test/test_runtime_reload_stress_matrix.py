"""Deterministic cross-file R2/R3 Play/Pause/Step transaction matrix.

The matrix uses the real owner-side transaction APIs and lightweight scene
adapters. It deliberately has no watcher, sleep, watchdog, build, or wheel
inspection so failures are reproducible in source-state pytest.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

from Infernux.components.component_identity import bind_asset_script_guid
from Infernux.components.missing_script import MissingScript
from Infernux.components.registry import (
    get_type_by_identity,
    publish_component_script_types,
    restore_component_registry_state,
    snapshot_component_registry_state,
)
from Infernux.components.script_loader import (
    _clear_script_error,
    load_all_components_from_file,
    retire_script_module,
)
from Infernux.engine.play_mode import PlayModeManager, PlayModeState, ScriptReloadBatchInput
from Infernux.engine.project_context import (
    get_project_root,
    get_script_module_name,
    set_project_root,
)
from Infernux.engine.runtime_dispatch import current_runtime_epoch
from Infernux.engine.runtime_script_revision import ScriptRevisionJournal
from Infernux.ui.ui_event_system import UIEventProcessor


class _Object:
    def __init__(self, object_id, components):
        self.id = object_id
        self._components = list(components)

    def get_py_components(self):
        return tuple(self._components)

    def replace_py_component(self, old_component, new_component):
        self._components[self._components.index(old_component)] = new_component
        return new_component


class _Scene:
    def __init__(self, objects):
        self._objects = tuple(objects)

    def get_all_objects(self):
        return self._objects

    def find_by_id(self, object_id):
        return next((obj for obj in self._objects if obj.id == object_id), None)


class _SceneManager:
    def __init__(self, scene):
        self.scene = scene
        self.calls = []

    def get_active_scene(self):
        return self.scene

    def play(self):
        self.calls.append("play")

    def pause(self):
        self.calls.append("pause")

    def step(self, _delta_time):
        self.calls.append("step")


class _AssetDatabase:
    def __init__(self, entries):
        self._entries = {
            str(Path(path).resolve()).casefold(): guid for path, guid in entries.items()
        }

    def get_guid_from_path(self, path):
        return self._entries.get(str(Path(path).resolve()).casefold(), "")


class _Canvas:
    game_object = None
    enabled = True

    def __init__(self, target):
        self.target = target

    def raycast(self, _x, _y):
        return self.target


@pytest.fixture
def stress_project(tmp_path):
    previous_root = get_project_root()
    registry_snapshot = snapshot_component_registry_state()
    project = tmp_path / "RuntimeReloadStress"
    assets = project / "Assets"
    assets.mkdir(parents=True)
    set_project_root(str(project))
    yield assets
    for path in assets.glob("*.py"):
        _clear_script_error(str(path))
    restore_component_registry_state(registry_snapshot)
    project_key = str(project.resolve()).casefold()
    for name, module in tuple(sys.modules.items()):
        module_path = str(getattr(module, "__file__", "") or "")
        if module_path and str(Path(module_path).resolve()).casefold().startswith(project_key):
            sys.modules.pop(name, None)
    set_project_root(previous_root)


def _write(path, source):
    encoded = (textwrap.dedent(source).strip() + "\n").encode("utf-8")
    path.write_bytes(encoded)
    return encoded


def _load_component(path, source, guid):
    classes = tuple(load_all_components_from_file(
        str(path), register=False, source_only=True, source=source
    ))
    assert len(classes) == 1
    component_type = classes[0]
    bind_asset_script_guid(component_type, guid)
    publish_component_script_types(str(path), (component_type,))
    return component_type


def _manager(monkeypatch, entries, objects):
    manager = PlayModeManager()
    manager._state = PlayModeState.PLAYING
    scene = _Scene(objects)
    scene_manager = _SceneManager(scene)
    monkeypatch.setattr(manager, "_get_scene_manager", lambda: scene_manager)
    manager.set_asset_database(_AssetDatabase(entries))
    return manager, scene_manager


def _commit(manager, revisions):
    batch = manager.prepare_script_reload_batch(tuple(
        ScriptReloadBatchInput(str(path), guid, source, retire_script_paths=retired)
        for path, guid, source, retired in revisions
    ))
    outcome = manager.commit_script_reload_batch(batch)
    assert outcome.success is True, outcome.error
    assert batch.committed
    return batch


def test_cross_file_play_pause_step_reload_keeps_identity_and_switches_epoch(
    stress_project, monkeypatch
):
    assets = stress_project
    helper_path, a_path, b_path = (
        assets / "shared_helper.py",
        assets / "stress_a.py",
        assets / "stress_b.py",
    )
    a_guid, b_guid = "stress-a-guid", "stress-b-guid"
    helper_old = _write(helper_path, """
        def tag(name): return name + '-helper-old'
    """)
    a_old = _write(a_path, """
        import shared_helper
        from Infernux.components import InxComponent
        class StressA(InxComponent):
            _uses_component_data_store = False
            def awake(self):
                self.awake_count = getattr(self, 'awake_count', 0) + 1
                self.events, self.coroutine_values = [], []
            def start(self):
                self.start_count = getattr(self, 'start_count', 0) + 1
                self.start_coroutine(self._routine())
            def update(self, _dt):
                self.events.append(('update', self.helper(), self.peer.helper(), shared_helper.tag('A')))
            def helper(self): return 'A-old'
            @property
            def label(self): return 'A-property-old'
            def on_pointer_click(self, _event): self.events.append(('ui', self.helper()))
            def on_collision_stay(self, _event): self.events.append(('collision', self.helper()))
            def _routine(self):
                self.coroutine_values.append(('old-generator', self.helper()))
                yield None
                self.coroutine_values.append(('old-resume', self.helper()))
    """)
    b_old = _write(b_path, """
        import shared_helper
        from Infernux.components import InxComponent
        class StressB(InxComponent):
            _uses_component_data_store = False
            def awake(self):
                self.awake_count = getattr(self, 'awake_count', 0) + 1
                self.events = []
            def start(self): self.start_count = getattr(self, 'start_count', 0) + 1
            def update(self, _dt):
                self.events.append(('update', self.helper(), self.peer.helper(), shared_helper.tag('B')))
            def helper(self): return 'B-old'
            @property
            def label(self): return 'B-property-old'
            def on_pointer_click(self, _event): self.events.append(('ui', self.helper()))
            def on_collision_stay(self, _event): self.events.append(('collision', self.helper()))
    """)
    a_type = _load_component(a_path, a_old, a_guid)
    b_type = _load_component(b_path, b_old, b_guid)
    instances = [a_type(), a_type(), b_type(), b_type()]
    for component, guid in zip(instances, (a_guid, a_guid, b_guid, b_guid)):
        component._script_guid = guid
    a_first, a_second, b_first, b_second = instances
    a_first.peer, b_first.peer = b_first, a_first
    a_second.peer, b_second.peer = b_second, a_second
    for component in instances:
        component._call_awake()
        component._call_start()
    original_ids = tuple(component.component_id for component in instances)
    original_types = tuple(type(component) for component in instances)
    old_epoch_id = current_runtime_epoch().epoch_id
    old_coroutine = a_first._coroutine_scheduler._coroutines[0]
    assert old_coroutine.creation_epoch_id == old_epoch_id
    objects = tuple(_Object(index + 1, (component,)) for index, component in enumerate(instances))
    manager, scene_manager = _manager(
        monkeypatch, {str(a_path): a_guid, str(b_path): b_guid}, objects
    )

    a_new = _write(a_path, """
        import shared_helper
        from Infernux.components import InxComponent
        class StressA(InxComponent):
            _uses_component_data_store = False
            def awake(self): self.awake_count = getattr(self, 'awake_count', 0) + 1
            def start(self): self.start_count = getattr(self, 'start_count', 0) + 1
            def update(self, _dt): self.events.append(('update-new', self.helper(), self.peer.helper(), shared_helper.tag('A')))
            def helper(self): return 'A-new'
            @property
            def label(self): return 'A-property-new'
            def on_pointer_click(self, _event): self.events.append(('ui-new', self.helper()))
            def on_collision_stay(self, _event): self.events.append(('collision-new', self.helper()))
            def _routine(self):
                self.coroutine_values.append(('new-generator', self.helper()))
                yield None
                self.coroutine_values.append(('new-resume', self.helper()))
    """)
    b_new = _write(b_path, """
        import shared_helper
        from Infernux.components import InxComponent
        class StressB(InxComponent):
            _uses_component_data_store = False
            def awake(self): self.awake_count = getattr(self, 'awake_count', 0) + 1
            def start(self): self.start_count = getattr(self, 'start_count', 0) + 1
            def update(self, _dt): self.events.append(('update-new', self.helper(), self.peer.helper(), shared_helper.tag('B')))
            def helper(self): return 'B-new'
            @property
            def label(self): return 'B-property-new'
            def on_pointer_click(self, _event): self.events.append(('ui-new', self.helper()))
            def on_collision_stay(self, _event): self.events.append(('collision-new', self.helper()))
    """)
    helper_new = _write(helper_path, """
        def tag(name): return name + '-helper-new'
    """)

    _commit(manager, ((helper_path, "", helper_new, ()), (a_path, a_guid, a_new, ()), (b_path, b_guid, b_new, ())))
    assert current_runtime_epoch().epoch_id > old_epoch_id
    assert tuple(component.component_id for component in instances) == original_ids
    assert tuple(type(component) for component in instances) == original_types
    assert all(component.awake_count == 1 and component.start_count == 1 for component in instances)
    assert (a_first.helper(), b_first.helper(), a_first.label, b_first.label) == (
        "A-new", "B-new", "A-property-new", "B-property-new"
    )
    a_first._call_update(0.016)
    assert a_first.events[-1] == ("update-new", "A-new", "B-new", "A-helper-new")
    a_first._call_on_collision_stay("collision")
    assert a_first.events[-1] == ("collision-new", "A-new")
    a_first._try_get_game_object = lambda: type("UIOwner", (), {"name": "StressA"})()
    processor = UIEventProcessor()
    canvas = _Canvas(a_first)
    processor.process([canvas], [(0.0, 0.0)], True, False, True, (0.0, 0.0), 0.016)
    processor.process([canvas], [(0.0, 0.0)], False, True, False, (0.0, 0.0), 0.016)
    assert a_first.events[-1] == ("ui-new", "A-new")
    a_first._tick_coroutines_update(0.016)
    assert ("old-resume", "A-new") in a_first.coroutine_values
    new_coroutine = a_first.start_coroutine(a_first._routine())
    assert new_coroutine.creation_epoch_id == current_runtime_epoch().epoch_id
    assert ("new-generator", "A-new") in a_first.coroutine_values
    assert old_coroutine.is_stale_epoch

    assert manager.pause() is True
    manager.step_frame()
    assert manager.resume() is True
    assert scene_manager.calls == ["pause", "step", "play"]

    assert manager.pause() is True
    a_latest = _write(a_path, a_new.decode().replace("A-new", "A-latest"))
    b_latest = _write(b_path, b_new.decode().replace("B-new", "B-latest"))
    helper_latest = _write(helper_path, helper_new.decode().replace("helper-new", "helper-latest"))
    _commit(manager, ((helper_path, "", helper_latest, ()), (a_path, a_guid, a_latest, ()), (b_path, b_guid, b_latest, ())))
    assert all(component.awake_count == 1 and component.start_count == 1 for component in instances)
    assert (a_first.helper(), b_first.helper()) == ("A-latest", "B-latest")
    assert manager.resume() is True

    assert get_type_by_identity("StressA", a_guid, a_type._get_type_guid()) is a_type
    assert get_type_by_identity("StressB", b_guid, b_type._get_type_guid()) is b_type
    assert sys.modules[get_script_module_name(str(a_path))].StressA is a_type
    assert sys.modules[get_script_module_name(str(b_path))].StressB is b_type


def test_supersede_save_candidates_and_failed_batch_rollback_are_deterministic(
    stress_project, monkeypatch
):
    path = stress_project / "supersede.py"
    journal = ScriptRevisionJournal()
    requests = [journal.request(str(path), f"value = {index}") for index in range(8)]
    assert all(requests)
    for request in requests[:-1]:
        assert journal.complete(request, succeeded=True) is False
    assert journal.complete(requests[-1], succeeded=True) is True
    claimed = journal.claim_ready_batch((str(path),))
    assert [item.request.generation for item in claimed] == [8]
    assert journal.commit_published_batch((claimed[0].request,)) is True
    assert journal.last_known_good(str(path)).source == b"value = 7"

    guid = "rollback-stress-guid"
    old_source = _write(path, """
        from Infernux.components import InxComponent
        class RollbackStress(InxComponent):
            _uses_component_data_store = False
            def helper(self): return 'old'
    """)
    component_type = _load_component(path, old_source, guid)
    component = component_type()
    component._script_guid = guid
    obj = _Object(77, (component,))
    manager, _scene_manager = _manager(monkeypatch, {str(path): guid}, (obj,))
    old_module = sys.modules[get_script_module_name(str(path))]
    old_identity = component.component_id
    candidate = _write(path, """
        from Infernux.components import InxComponent
        class RollbackStress(InxComponent):
            _uses_component_data_store = False
            def helper(self): return 'candidate'
    """)
    batch = manager.prepare_script_reload_batch((ScriptReloadBatchInput(str(path), guid, candidate),))
    import Infernux.components.script_loader as script_loader
    apply_real = script_loader._apply_component_body_patch_plans

    def apply_then_fail(plans):
        apply_real(plans)
        raise RuntimeError("matrix publish failure")

    monkeypatch.setattr(script_loader, "_apply_component_body_patch_plans", apply_then_fail)
    outcome = manager.commit_script_reload_batch(batch)
    assert outcome.success is False
    assert "matrix publish failure" in outcome.error
    assert component.helper() == "old"
    assert component.component_id == old_identity
    assert sys.modules[get_script_module_name(str(path))] is old_module
    assert get_type_by_identity("RollbackStress", guid, component_type._get_type_guid()) is component_type


def test_rename_then_delete_uses_transaction_owner_and_keeps_stable_identity(
    stress_project, monkeypatch
):
    old_path = stress_project / "rename_before_delete.py"
    new_path = stress_project / "renamed_before_delete.py"
    guid = "rename-delete-guid"
    old_source = _write(old_path, """
        from Infernux.components import InxComponent
        class RenameDeleteProbe(InxComponent):
            _uses_component_data_store = False
            def helper(self): return 'old-path'
    """)
    component_type = _load_component(old_path, old_source, guid)
    component = component_type()
    component._script_guid = guid
    obj = _Object(91, (component,))
    manager, _scene_manager = _manager(
        monkeypatch, {str(old_path): guid, str(new_path): guid}, (obj,)
    )
    old_module_name = get_script_module_name(str(old_path))
    new_source = _write(new_path, """
        from Infernux.components import InxComponent
        class RenameDeleteProbe(InxComponent):
            _uses_component_data_store = False
            def helper(self): return 'new-path'
    """)
    batch = manager.prepare_script_reload_batch((ScriptReloadBatchInput(
        str(new_path), guid, new_source, retire_script_paths=(str(old_path),)
    ),))
    assert manager.commit_script_reload_batch(batch).success is True
    assert component.helper() == "new-path"
    assert type(component) is component_type
    assert retire_script_module(str(old_path)) is not None
    assert old_module_name not in sys.modules
    delete_batch = manager.prepare_script_delete_batch(guid, str(new_path))
    assert manager.commit_script_delete_batch(delete_batch) == 1
    assert isinstance(obj.get_py_components()[0], MissingScript)
    assert obj.get_py_components()[0]._script_guid == guid
