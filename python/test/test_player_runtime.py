from __future__ import annotations


def _fake_scene_manager(monkeypatch, scene):
    import Infernux.lib as native_lib

    class SceneManager:
        @staticmethod
        def instance():
            return SceneManager

        @staticmethod
        def get_active_scene():
            return scene

        @staticmethod
        def play():
            return None

        @staticmethod
        def stop():
            return None

    monkeypatch.setattr(native_lib, "SceneManager", SceneManager)
    return SceneManager


def test_player_runtime_activation_does_not_snapshot_scene(monkeypatch):
    from Infernux.engine.player_runtime import PlayerRuntimeSession

    calls = []

    class Scene:
        main_camera = object()

        def get_all_objects(self):
            return []

        def set_playing(self, value):
            calls.append(("set_playing", value))

    scene = Scene()

    scene_manager = _fake_scene_manager(monkeypatch, scene)
    monkeypatch.setattr(scene_manager, "play", lambda: calls.append("play"))
    monkeypatch.setattr(
        "Infernux.engine.player_runtime.PlayerRuntimeSession._refresh_loaded_scene",
        staticmethod(lambda current: calls.append(("refresh", current))),
    )
    monkeypatch.setattr("Infernux.timing.Time._reset", lambda: calls.append("reset"))

    session = PlayerRuntimeSession()
    assert session.activate() is True
    assert session.is_playing
    assert calls == [
        "reset",
        ("set_playing", True),
        ("refresh", scene),
        "play",
    ]
    assert not hasattr(session, "_scene_backup")


def test_player_runtime_load_scene_success_does_not_initialize_runtime_state(
    monkeypatch, tmp_path
):
    from Infernux.engine import player_runtime
    from Infernux.engine.player_runtime import PlayerRuntimeSession

    scene_path = tmp_path / "Main.scene"
    scene_path.write_text("scene", encoding="utf-8")

    class Scene:
        main_camera = None

        def get_all_objects(self):
            return []

    scene = Scene()
    _fake_scene_manager(monkeypatch, scene)
    refreshes = []
    monkeypatch.setattr(
        player_runtime.PlayerRuntimeSession,
        "_refresh_loaded_scene",
        staticmethod(lambda current: refreshes.append(current)),
    )

    class Transaction:
        error = ""

        def __init__(self, current, **kwargs):
            assert current is scene
            assert kwargs["path"] == str(scene_path)

        def run_to_completion(self, *, raise_on_failure):
            assert raise_on_failure is False
            return True

    monkeypatch.setattr(player_runtime, "SceneDocumentTransaction", Transaction)

    session = PlayerRuntimeSession()
    assert session.load_scene(str(scene_path)) is True
    assert session.active_scene_path == str(scene_path)
    assert refreshes == []


def test_player_runtime_load_scene_failure_keeps_previous_scene_state(
    monkeypatch, tmp_path
):
    from Infernux.engine import player_runtime
    from Infernux.engine.player_runtime import PlayerRuntimeSession

    scene_path = tmp_path / "Broken.scene"
    scene_path.write_text("scene", encoding="utf-8")

    class Scene:
        main_camera = None

        def get_all_objects(self):
            return []

    scene = Scene()
    _fake_scene_manager(monkeypatch, scene)

    class Transaction:
        error = "invalid scene"

        def __init__(self, *_args, **_kwargs):
            pass

        def run_to_completion(self, *, raise_on_failure):
            return False

    monkeypatch.setattr(player_runtime, "SceneDocumentTransaction", Transaction)

    session = PlayerRuntimeSession()
    assert session.load_scene(str(scene_path)) is False
    assert session.active_scene_path is None
    assert session.state == "stopped"


def test_player_runtime_tick_and_shutdown_own_runtime_lifecycle(monkeypatch):
    import Infernux.components.component as component_module
    from Infernux.engine.player_runtime import PlayerRuntimeSession

    calls = []

    class Component:
        def _call_on_destroy(self):
            calls.append("destroy")

    monkeypatch.setattr(component_module.InxComponent, "_active_instances", {"x": [Component()]})
    monkeypatch.setattr(
        component_module.InxComponent,
        "_clear_all_instances",
        lambda: calls.append("clear"),
    )
    monkeypatch.setattr("Infernux.timing.Time._tick", lambda value: calls.append(("tick", value)))

    class NativeEngine:
        @staticmethod
        def get_game_only_frame_ms():
            return 4.0

    session = PlayerRuntimeSession(native_engine=NativeEngine())
    session._state = "playing"
    assert session.tick(0.25) == 0.25
    assert calls == [("tick", 0.25)]

    _fake_scene_manager(monkeypatch, object())
    session.shutdown()
    assert calls[-2:] == ["destroy", "clear"]
    assert session.state == "stopped"


def test_player_runtime_steady_tick_does_not_prepare_python_phase_plan(monkeypatch):
    from Infernux.engine.player_runtime import PlayerRuntimeSession

    class Scheduler:
        prepare_calls = 0

        def prepare_frame(self):
            self.prepare_calls += 1
            raise AssertionError("steady Player frames must not prepare a Python phase plan")

        def clear(self):
            return None

    scheduler = Scheduler()
    monkeypatch.setattr("Infernux.timing.Time._tick", lambda _value: None)

    session = PlayerRuntimeSession(scheduler=scheduler)
    session._state = "playing"

    assert session.tick(1.0 / 60.0) == 1.0 / 60.0
    assert scheduler.prepare_calls == 0
