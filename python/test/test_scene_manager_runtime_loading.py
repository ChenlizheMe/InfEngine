from __future__ import annotations


def test_player_detection_uses_native_scene_manager_without_editor_manager(monkeypatch):
    from Infernux.lib import SceneManager as native_scene_manager
    from Infernux.scene import SceneManager

    class Native:
        @staticmethod
        def instance():
            return Native

        @staticmethod
        def is_playing():
            return True

    monkeypatch.setattr("Infernux.engine.play_mode.PlayModeManager._instance", None)
    monkeypatch.setattr("Infernux.scene._NativeSceneManager", Native)

    assert SceneManager._is_in_play_mode() is True


def test_player_runtime_load_is_queued_until_pending_transaction_is_processed(monkeypatch):
    from Infernux.scene import SceneManager

    monkeypatch.setattr(SceneManager, "_is_in_play_mode", staticmethod(lambda: True))
    monkeypatch.setattr(
        SceneManager,
        "_load_build_list",
        staticmethod(lambda: ["/project/Scenes/Main.scene"]),
    )
    monkeypatch.setattr(
        "Infernux.scene.os.path.isfile",
        lambda path: path == "/project/Scenes/Main.scene",
    )
    monkeypatch.setattr(SceneManager, "_pending_scene_load", None)
    monkeypatch.setattr(SceneManager, "_active_scene_transaction", None)

    assert SceneManager.load_scene("Main") is True
    assert SceneManager._pending_scene_load == "/project/Scenes/Main.scene"


def test_player_runtime_tick_advances_pending_scene_load_before_time(monkeypatch):
    from Infernux.engine.player_runtime import PlayerRuntimeSession

    calls = []

    class RuntimeSceneManager:
        @staticmethod
        def is_scene_load_pending():
            calls.append("pending?")
            return True

        @staticmethod
        def process_pending_load():
            calls.append("process")

    monkeypatch.setattr("Infernux.scene.SceneManager", RuntimeSceneManager)
    monkeypatch.setattr("Infernux.timing.Time._tick", lambda value: calls.append("time"))

    session = PlayerRuntimeSession()
    session._state = "playing"
    session.tick(1.0 / 60.0)

    assert calls == ["pending?", "process", "time"]


def test_pending_scene_transaction_starts_the_new_scene_once(monkeypatch):
    from Infernux.scene import SceneManager

    calls = []

    class Transaction:
        succeeded = True
        error = ""

        def poll(self):
            calls.append("poll")
            return True

    class Native:
        @staticmethod
        def instance():
            return Native

        @staticmethod
        def get_active_scene():
            return object()

        @staticmethod
        def _start_active_scene_for_play():
            calls.append("start")

    monkeypatch.setattr(SceneManager, "_pending_scene_load", None)
    monkeypatch.setattr(SceneManager, "_active_scene_transaction", Transaction())
    monkeypatch.setattr(SceneManager, "_active_scene_load_path", "/project/Scenes/Main.scene")
    monkeypatch.setattr(SceneManager, "_active_scene_file_manager", None)
    monkeypatch.setattr("Infernux.scene._NativeSceneManager", Native)

    SceneManager.process_pending_load()
    SceneManager.process_pending_load()

    assert calls == ["poll", "start"]
