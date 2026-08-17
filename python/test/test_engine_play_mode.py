"""Tests for Infernux.engine.play_mode — PlayModeState, PlayModeEvent, PlayModeManager."""

import sys
import threading
import time

import pytest

from Infernux.components import InxComponent
from Infernux.components.component_identity import bind_asset_script_guid
from Infernux.components.missing_script import MissingScript
from Infernux.core.asset_ref import MaterialRef
from Infernux.engine.play_mode import PlayModeState, PlayModeEvent, PlayModeManager


class _FakeRuntimeGameObject:
    def __init__(self, object_id: int, name: str):
        self.id = object_id
        self.name = name
        self.transform = object()


class _FakeScriptGameObject:
    def __init__(self, object_id: int, component):
        self.id = object_id
        self.components = [component]
        self.remove_calls = 0
        self.add_calls = 0
        self.replace_calls = 0

    def get_py_components(self):
        return list(self.components)

    def remove_py_component(self, component):
        self.remove_calls += 1
        self.components.remove(component)

    def add_py_component(self, component):
        self.add_calls += 1
        self.components.append(component)

    def replace_py_component(self, old_component, new_component):
        self.replace_calls += 1
        index = self.components.index(old_component)
        new_component._component_id = old_component.component_id
        self.components[index] = new_component
        return new_component


class _FakeScriptScene:
    def __init__(self, game_object):
        self.game_object = game_object

    def get_all_objects(self):
        return [self.game_object]

    def find_by_id(self, object_id):
        return self.game_object if object_id == self.game_object.id else None


class _FakeMultiScriptScene:
    def __init__(self, game_objects):
        self.game_objects = list(game_objects)

    def get_all_objects(self):
        return list(self.game_objects)

    def find_by_id(self, object_id):
        return next((obj for obj in self.game_objects if obj.id == object_id), None)


class _FailingRecoveryGameObject(_FakeScriptGameObject):
    def __init__(self, object_id: int, component):
        super().__init__(object_id, component)
        self.fail_recovery = False

    def replace_py_component(self, old_component, new_component):
        if (
            self.fail_recovery
            and isinstance(old_component, MissingScript)
            and not isinstance(new_component, MissingScript)
        ):
            raise RuntimeError("simulated MissingScript replacement failure")
        return super().replace_py_component(old_component, new_component)


class _FakeScriptSceneManager:
    def __init__(self, scene):
        self.scene = scene

    def get_active_scene(self):
        return self.scene


# ══════════════════════════════════════════════════════════════════════
# PlayModeState enum
# ══════════════════════════════════════════════════════════════════════

class TestPlayModeState:
    def test_members_exist(self):
        assert PlayModeState.EDIT is not None
        assert PlayModeState.PLAYING is not None
        assert PlayModeState.PAUSED is not None

    def test_distinct_values(self):
        values = {PlayModeState.EDIT, PlayModeState.PLAYING, PlayModeState.PAUSED}
        assert len(values) == 3


# ══════════════════════════════════════════════════════════════════════
# PlayModeEvent
# ══════════════════════════════════════════════════════════════════════

class TestPlayModeEvent:
    def test_fields(self):
        evt = PlayModeEvent(
            old_state=PlayModeState.EDIT,
            new_state=PlayModeState.PLAYING,
            timestamp=1.0,
        )
        assert evt.old_state is PlayModeState.EDIT
        assert evt.new_state is PlayModeState.PLAYING
        assert evt.timestamp == 1.0


# ══════════════════════════════════════════════════════════════════════
# PlayModeManager
# ══════════════════════════════════════════════════════════════════════

class TestPlayModeManager:
    def test_initial_state_is_edit(self):
        mgr = PlayModeManager()
        assert mgr._state is PlayModeState.EDIT

    def test_singleton_instance(self):
        mgr = PlayModeManager()
        assert PlayModeManager.instance() is mgr

    def test_timing_defaults(self):
        mgr = PlayModeManager()
        assert mgr._delta_time == 0.0
        assert mgr._time_scale == 1.0
        assert mgr._total_play_time == 0.0
        assert mgr.step_sequence == 0

    def test_paused_step_sequence_records_completed_commands(self):
        class _SceneManager:
            def __init__(self):
                self.steps = []

            def step(self, delta_time):
                self.steps.append(delta_time)

        mgr = PlayModeManager()
        scene_manager = _SceneManager()
        mgr._state = PlayModeState.PAUSED
        mgr._get_scene_manager = lambda: scene_manager

        mgr.step_frame()
        mgr.step_frame()

        assert mgr.step_sequence == 2
        assert scene_manager.steps == [pytest.approx(1.0 / 60.0)] * 2

    def test_tick_does_not_poll_scene_load_service_without_pending_work(self, monkeypatch):
        mgr = PlayModeManager()
        mgr._state = PlayModeState.PLAYING
        mgr._native_engine = None

        from Infernux.scene import SceneManager

        monkeypatch.setattr(SceneManager, "_pending_scene_load", None)
        monkeypatch.setattr(SceneManager, "_active_scene_transaction", None)
        monkeypatch.setattr(
            SceneManager,
            "process_pending_load",
            staticmethod(lambda: (_ for _ in ()).throw(
                AssertionError("empty play tick must not poll scene loading")
            )),
        )

        mgr.tick(1.0 / 60.0)

        assert mgr._delta_time > 0.0

    def test_engine_steady_play_tick_does_not_prepare_python_phase_plan(self):
        from Infernux.engine.engine import Engine

        class Scheduler:
            prepare_calls = 0

            def prepare_frame(self):
                self.prepare_calls += 1
                raise AssertionError(
                    "steady Editor frames must not prepare a Python phase plan"
                )

        class PlayMode:
            is_playing = True

            def tick(self, _delta_time):
                return None

        scheduler = Scheduler()
        engine = Engine.__new__(Engine)
        engine._last_frame_time = time.time()
        engine._editor_frame_sync_callback = None
        engine._resources_manager = None
        engine._play_mode_manager = PlayMode()
        engine._player_runtime = None
        engine._runtime_scheduler = scheduler
        engine._scene_view_visible = False
        engine._tick_runtime_acceptance = lambda _delta_time: None
        engine._clear_uploaded_gizmos = lambda: None

        assert engine.tick_play_mode(1.0 / 60.0) == 1.0 / 60.0
        assert scheduler.prepare_calls == 0

    def test_debug_frame_gate_pauses_after_exact_completed_frame_budget(self):
        class _SceneManager:
            def __init__(self):
                self.pause_calls = 0

            def pause(self):
                self.pause_calls += 1

        mgr = PlayModeManager()
        scene_manager = _SceneManager()
        mgr._state = PlayModeState.PLAYING
        mgr._get_scene_manager = lambda: scene_manager
        completed = threading.Event()
        mgr._arm_debug_frame_pause_gate(2, completed, pause_on_complete=True)

        assert mgr._advance_debug_frame_pause_gate() is False
        assert mgr._advance_debug_frame_pause_gate() is False
        assert completed.is_set() is False
        assert mgr._advance_debug_frame_pause_gate() is True

        assert completed.is_set() is True
        assert mgr.state is PlayModeState.PAUSED
        assert scene_manager.pause_calls == 1

    def test_debug_frame_gate_notifies_a_hold_boundary_before_completion(self):
        mgr = PlayModeManager()
        mgr._state = PlayModeState.PLAYING
        completed = threading.Event()
        hold_complete = threading.Event()
        hold_callbacks = []
        mgr._arm_debug_frame_pause_gate(
            5,
            completed,
            pause_on_complete=False,
            hold_frame_count=2,
            hold_complete_event=hold_complete,
            hold_complete_callback=lambda: hold_callbacks.append(True),
        )

        assert mgr._advance_debug_frame_pause_gate() is False
        assert hold_complete.is_set() is False
        assert mgr._advance_debug_frame_pause_gate() is False
        assert hold_complete.is_set() is True
        assert hold_callbacks == [True]
        assert completed.is_set() is False

    def test_scene_backup_none_initially(self):
        mgr = PlayModeManager()
        assert mgr._scene_backup is None
        assert mgr._scene_path_backup is None

    def test_restore_scene_path_reasserts_authored_scene_persistence(self, monkeypatch, tmp_path):
        from Infernux.engine.scene_manager import SceneFileManager

        authored = str(tmp_path / "racetrack.scene")
        runtime = str(tmp_path / "results.scene")
        manager = SceneFileManager()
        manager._current_scene_path = runtime
        restored_cameras = []
        remembered_paths = []
        scene_changed = []
        manager._restore_camera_state = restored_cameras.append
        manager._remember_last_scene = remembered_paths.append
        manager._on_scene_changed = lambda: scene_changed.append(True)

        mgr = PlayModeManager()
        mgr._scene_path_backup = authored
        mgr._scene_document_id_backup = manager.document_id
        from Infernux.engine.interaction import DocumentRegistry

        document = DocumentRegistry.instance().require(manager.document_id)
        DocumentRegistry.instance().mark_changed(document.document_id)
        mgr._scene_revision_backup = document.revision
        mgr._scene_saved_revision_backup = document.saved_revision
        mgr._scene_document_state_backup = document.state
        DocumentRegistry.instance().restore_saved_revision(document.document_id)
        mgr._restore_scene_file_path()

        assert manager.current_scene_path == authored
        assert manager.is_dirty is True
        assert restored_cameras == [authored]
        assert remembered_paths == [authored]
        assert scene_changed == [True]

    def test_restore_unsaved_scene_preserves_document_identity_and_revision(self):
        from Infernux.engine.interaction import DocumentRegistry
        from Infernux.engine.scene_manager import SceneFileManager

        manager = SceneFileManager()
        original_document_id = manager.document_id
        document = DocumentRegistry.instance().require(original_document_id)
        DocumentRegistry.instance().mark_changed(document.document_id)
        original_revision = document.revision
        original_saved_revision = document.saved_revision
        manager._current_scene_path = "runtime.scene"
        DocumentRegistry.instance().restore_saved_revision(document.document_id)
        scene_changed = []
        remembered_paths = []
        manager._on_scene_changed = lambda: scene_changed.append(True)
        manager._remember_last_scene = remembered_paths.append

        mgr = PlayModeManager()
        mgr._scene_path_backup = None
        mgr._scene_document_id_backup = original_document_id
        mgr._scene_revision_backup = original_revision
        mgr._scene_saved_revision_backup = original_saved_revision
        mgr._scene_document_state_backup = document.state
        mgr._restore_scene_file_path()

        assert manager.current_scene_path is None
        assert manager.document_id == original_document_id
        assert manager.is_dirty is True
        assert scene_changed == [True]
        assert remembered_paths == []

    def test_exit_play_mode_restores_document_state_without_legacy_dirty_backup(
        self, monkeypatch
    ):
        from Infernux.engine.deferred_task import DeferredTaskRunner

        class _SceneManager:
            def __init__(self):
                self.stop_calls = 0

            def stop(self):
                self.stop_calls += 1

        class _Runner:
            is_busy = False

            def __init__(self):
                self.steps = []

            def submit(self, _name, steps, on_done=None):
                self.steps = list(steps)
                self.on_done = on_done
                return True

        scene_manager = _SceneManager()
        runner = _Runner()
        monkeypatch.setattr(DeferredTaskRunner, "_instance", runner)

        manager = PlayModeManager()
        manager._state = PlayModeState.PLAYING
        manager._scene_backup = object()
        manager._get_scene_manager = lambda: scene_manager
        rebuild_calls = []
        manager._rebuild_active_scene = lambda snapshot, **kwargs: (
            rebuild_calls.append((snapshot, kwargs)) or True
        )
        notifications = []
        manager._notify_state_change = lambda old, new: notifications.append((old, new))

        assert manager.exit_play_mode() is True
        assert manager.state is PlayModeState.EDIT
        assert scene_manager.stop_calls == 1

        _, _, restore = runner.steps[0]
        restore()

        assert rebuild_calls == [
            (manager._scene_backup, {"for_play": False, "restore_scene_path": True})
        ]
        assert notifications == [(PlayModeState.PLAYING, PlayModeState.EDIT)]

    def test_listener_list_empty(self):
        mgr = PlayModeManager()
        assert mgr._state_change_listeners == []

    def test_last_transition_timings_returns_an_isolated_snapshot(self):
        mgr = PlayModeManager()
        mgr._last_transition_timings_ms = {
            "transition": "exit",
            "total": 12.0,
            "phases": {"native_commit": 8.0},
        }

        timings = mgr.last_transition_timings_ms
        timings["total"] = 0.0
        timings["phases"]["native_commit"] = 0.0

        assert mgr.last_transition_timings_ms == {
            "transition": "exit",
            "total": 12.0,
            "phases": {"native_commit": 8.0},
        }

    def test_set_asset_database(self):
        mgr = PlayModeManager()
        mgr.set_asset_database("fake_db")
        assert mgr._asset_database == "fake_db"

    def test_deleted_script_becomes_missing_and_recovers_with_identity(self, tmp_path, monkeypatch):
        script_guid = "1" * 32

        class ScriptProbe(InxComponent):
            _uses_component_data_store = False
            speed: float = 1.0
            material: MaterialRef = MaterialRef()

        bind_asset_script_guid(ScriptProbe, script_guid)
        component = ScriptProbe()
        component._script_guid = script_guid
        component.speed = 7.5
        component.material = MaterialRef(guid="material-guid", path_hint="Assets/Test.mat")
        component.enabled = False
        component.execution_order = 23
        component._call_awake()
        component._call_start()
        original_id = component.component_id

        game_object = _FakeScriptGameObject(91, component)
        scene = _FakeScriptScene(game_object)
        manager = PlayModeManager()
        monkeypatch.setattr(
            manager,
            "_get_scene_manager",
            lambda: _FakeScriptSceneManager(scene),
        )

        script = tmp_path / "script_probe.py"
        assert manager.mark_components_missing_for_script(script_guid, str(script)) == 1
        missing = game_object.components[0]
        assert isinstance(missing, MissingScript)
        assert missing.component_id == original_id
        assert missing._script_guid == script_guid
        assert missing._serialize_fields_document()["speed"] == 7.5
        assert missing._serialize_fields_document()["material"]["guid"] == "material-guid"
        assert missing._enabled is False
        assert missing._awake_called is True
        assert missing._has_started is True
        assert missing._execution_order == 23
        assert game_object.replace_calls == 1
        assert game_object.remove_calls == 0
        assert game_object.add_calls == 0

        script.write_text(
            "from Infernux.components import InxComponent\n"
            "from Infernux.core.asset_ref import MaterialRef\n"
            "CALLS = []\n"
            "class ScriptProbe(InxComponent):\n"
            "    _uses_component_data_store = False\n"
            "    speed: float = 1.0\n"
            "    material: MaterialRef = MaterialRef()\n"
            "    def awake(self): CALLS.append('awake')\n"
            "    def start(self): CALLS.append('start')\n",
            encoding="utf-8",
        )

        class _AssetDatabase:
            def get_guid_from_path(self, path):
                assert path == str(script.resolve())
                return script_guid

        manager.set_asset_database(_AssetDatabase())
        manager.reload_components_from_script(str(script))

        restored = game_object.components[0]
        assert not isinstance(restored, MissingScript)
        assert restored.component_id == original_id
        assert restored._script_guid == script_guid
        assert restored._script_path == str(script.resolve())
        assert restored.speed == 7.5
        from Infernux.components.fields import get_raw_field_value

        restored_material = get_raw_field_value(restored, "material")
        assert restored_material.guid == "material-guid"
        assert restored_material.path_hint == "Assets/Test.mat"
        assert restored._enabled is False
        assert restored._awake_called is True
        assert restored._has_started is True
        assert restored._execution_order == 23
        restored._call_awake()
        restored._call_start()
        assert sys.modules[restored.__class__.__module__].CALLS == []
        assert game_object.replace_calls == 2
        assert game_object.remove_calls == 0
        assert game_object.add_calls == 0

    def test_missing_script_recovery_failure_rolls_back_scene_registry_and_module(
        self,
        tmp_path,
        monkeypatch,
    ):
        from Infernux.components.registry import snapshot_component_registry_state
        from Infernux.engine.project_context import get_script_module_name

        script_guid = "2" * 32

        class AtomicRecoveryProbe(InxComponent):
            _uses_component_data_store = False
            speed: float = 1.0

        bind_asset_script_guid(AtomicRecoveryProbe, script_guid)
        first = AtomicRecoveryProbe()
        second = AtomicRecoveryProbe()
        for index, component in enumerate((first, second), start=1):
            component._script_guid = script_guid
            component.speed = index * 4.5
            component.enabled = index == 1
            component._call_awake()
            component._call_start()

        first_object = _FailingRecoveryGameObject(101, first)
        second_object = _FailingRecoveryGameObject(102, second)
        scene = _FakeMultiScriptScene((first_object, second_object))
        manager = PlayModeManager()
        monkeypatch.setattr(
            manager,
            "_get_scene_manager",
            lambda: _FakeScriptSceneManager(scene),
        )

        script = tmp_path / "atomic_recovery_probe.py"
        assert manager.mark_components_missing_for_script(script_guid, str(script)) == 2
        first_missing = first_object.components[0]
        second_missing = second_object.components[0]
        before_registry = snapshot_component_registry_state()
        module_name = get_script_module_name(str(script.resolve()))
        before_module = sys.modules.get(module_name)

        script.write_text(
            "from Infernux.components import InxComponent\n"
            "class AtomicRecoveryProbe(InxComponent):\n"
            "    _uses_component_data_store = False\n"
            "    speed: float = 1.0\n",
            encoding="utf-8",
        )

        class _AssetDatabase:
            def get_guid_from_path(self, path):
                assert path == str(script.resolve())
                return script_guid

        manager.set_asset_database(_AssetDatabase())
        second_object.fail_recovery = True
        outcome = manager.reload_components_from_script_result(str(script))

        assert outcome.success is False
        assert "simulated MissingScript replacement failure" in outcome.error
        assert first_object.components[0] is first_missing
        assert second_object.components[0] is second_missing
        assert first_missing._serialize_fields_document()["speed"] == 4.5
        assert second_missing._serialize_fields_document()["speed"] == 9.0
        assert first_missing._awake_called is True
        assert first_missing._has_started is True
        assert second_missing._awake_called is True
        assert second_missing._has_started is True
        assert snapshot_component_registry_state() == before_registry
        if before_module is None:
            assert module_name not in sys.modules
        else:
            assert sys.modules[module_name] is before_module

    def test_register_runtime_hidden_object_tracks_ids(self):
        mgr = PlayModeManager()
        obj = _FakeRuntimeGameObject(404, "HiddenClone")

        mgr.register_runtime_hidden_object(obj)

        assert mgr.is_runtime_hidden_object_id(404)

    def test_runtime_hidden_listener_only_fires_for_state_changes(self):
        mgr = PlayModeManager()
        calls = []
        listener = lambda: calls.append(mgr.get_runtime_hidden_object_ids())
        mgr.add_runtime_hidden_listener(listener)

        obj = _FakeRuntimeGameObject(404, "HiddenClone")
        mgr.register_runtime_hidden_object(obj)
        mgr.register_runtime_hidden_object(obj)
        mgr.clear_runtime_hidden_object_ids()
        mgr.clear_runtime_hidden_object_ids()

        assert calls == [{404}, set()]

        mgr.remove_runtime_hidden_listener(listener)
        mgr.register_runtime_hidden_object(obj)
        assert calls == [{404}, set()]

    def test_rebuild_scene_failure_preserves_runtime_hidden_ids(self):
        mgr = PlayModeManager()
        mgr._runtime_hidden_object_ids = {1, 2, 3}

        assert not mgr._rebuild_active_scene(None, for_play=False)
        assert mgr._runtime_hidden_object_ids == {1, 2, 3}

    def test_rejected_document_preserves_runtime_hidden_ids(self, monkeypatch):
        class _RejectingScene:
            def _commit_document(self, snapshot):
                return False

        class _FakeSceneManager:
            def get_active_scene(self):
                return _RejectingScene()

        mgr = PlayModeManager()
        mgr._runtime_hidden_object_ids = {77}
        monkeypatch.setattr(mgr, "_get_scene_manager", lambda: _FakeSceneManager())

        assert not mgr._rebuild_active_scene({"invalid": True}, for_play=False)
        assert mgr._runtime_hidden_object_ids == {77}

    def test_rebuild_scene_does_not_materialize_prefab_refs_for_play(self, monkeypatch):
        class _FakeCommitToken:
            def __init__(self, scene, previous_document):
                self._scene = scene
                self._previous_document = previous_document
                self.is_active = True

            def rollback(self):
                if not self.is_active:
                    return False
                self._scene.document = self._previous_document
                self.is_active = False
                return True

            def finalize(self):
                self.is_active = False

        class _FakeScene:
            def __init__(self):
                self.playing = None
                self.document = {
                    "name": "FakeLiveScene",
                    "isPlaying": False,
                    "objects": [],
                }

            def serialize_document(self):
                return dict(self.document)

            def _commit_document_retaining_world(self, snapshot):
                token = _FakeCommitToken(self, dict(self.document))
                self.document = dict(snapshot)
                return token

            def set_playing(self, playing):
                self.playing = playing

        class _FakeSceneManager:
            def __init__(self, scene):
                self._scene = scene

            def get_active_scene(self):
                return self._scene

        mgr = PlayModeManager()
        scene = _FakeScene()
        scene_manager = _FakeSceneManager(scene)
        materialized = False

        monkeypatch.setattr(mgr, "_get_scene_manager", lambda: scene_manager)
        from Infernux.engine import component_restore
        monkeypatch.setattr(
            component_restore,
            "preflight_scene_python_components",
            lambda snapshot, asset_database=None, **_kwargs: component_restore.PreparedPythonComponentGraph([]),
        )
        monkeypatch.setattr(
            component_restore,
            "publish_prepared_scene_python_components",
            lambda scene, prepared, clear_registries=True: prepared.consume(),
        )

        def _unexpected_materialize():
            nonlocal materialized
            materialized = True

        monkeypatch.setattr(mgr, "_materialize_prefab_references_for_play", _unexpected_materialize)

        snapshot = {
            "name": "PlayModeRebuild",
            "isPlaying": False,
            "objects": [],
        }
        assert mgr._rebuild_active_scene(snapshot, for_play=True)
        assert scene.playing is True
        assert not materialized
