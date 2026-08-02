"""Tests for Infernux.engine.play_mode — PlayModeState, PlayModeEvent, PlayModeManager."""

import threading

from Infernux.components import InxComponent
from Infernux.components.component_identity import bind_asset_script_guid
from Infernux.components.missing_script import MissingScript
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
        manager._dirty = False
        restored_cameras = []
        remembered_paths = []
        scene_changed = []
        manager._restore_camera_state = restored_cameras.append
        manager._remember_last_scene = remembered_paths.append
        manager._on_scene_changed = lambda: scene_changed.append(True)

        mgr = PlayModeManager()
        mgr._scene_path_backup = authored
        mgr._scene_dirty_backup = True
        mgr._restore_scene_file_path()

        assert manager.current_scene_path == authored
        assert manager.is_dirty is True
        assert restored_cameras == [authored]
        assert remembered_paths == [authored]
        assert scene_changed == [True]

    def test_listener_list_empty(self):
        mgr = PlayModeManager()
        assert mgr._state_change_listeners == []

    def test_set_asset_database(self):
        mgr = PlayModeManager()
        mgr.set_asset_database("fake_db")
        assert mgr._asset_database == "fake_db"

    def test_deleted_script_becomes_missing_and_recovers_with_identity(self, tmp_path, monkeypatch):
        script_guid = "1" * 32

        class ScriptProbe(InxComponent):
            speed: float = 1.0

        bind_asset_script_guid(ScriptProbe, script_guid)
        component = ScriptProbe()
        component._script_guid = script_guid
        component.speed = 7.5
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
        assert game_object.replace_calls == 1
        assert game_object.remove_calls == 0
        assert game_object.add_calls == 0

        script.write_text(
            "from Infernux.components import InxComponent\n"
            "class ScriptProbe(InxComponent):\n"
            "    speed: float = 1.0\n",
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
        assert game_object.replace_calls == 2
        assert game_object.remove_calls == 0
        assert game_object.add_calls == 0

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
            lambda snapshot, asset_database=None: component_restore.PreparedPythonComponentGraph([]),
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
