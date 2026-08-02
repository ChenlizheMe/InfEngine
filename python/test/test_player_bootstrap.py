from __future__ import annotations

import sys
import types


def _stub_engine_status(monkeypatch):
    module = types.ModuleType("Infernux.engine.ui.engine_status")

    class EngineStatus:
        @classmethod
        def set(cls, *_args, **_kwargs):
            pass

        @classmethod
        def clear(cls, *_args, **_kwargs):
            pass

    module.EngineStatus = EngineStatus
    monkeypatch.setitem(sys.modules, "Infernux.engine.ui.engine_status", module)


def test_player_queues_initial_scene_activation_until_main_loop(monkeypatch):
    from Infernux.engine.deferred_task import DeferredTaskRunner
    from Infernux.engine.player_bootstrap import PlayerBootstrap

    _stub_engine_status(monkeypatch)
    DeferredTaskRunner._instance = None
    activated = []
    bootstrap = PlayerBootstrap.__new__(PlayerBootstrap)
    bootstrap._activate_initial_scene_for_play = lambda: activated.append(True)

    bootstrap._enter_play_mode()

    runner = DeferredTaskRunner.instance()
    assert runner.is_busy
    assert activated == []
    runner.tick()
    assert activated == [True]
    runner.tick()
    assert not runner.is_busy


def test_player_starts_fresh_scene_without_second_document_transaction(monkeypatch):
    import Infernux.lib as native_lib
    from Infernux.engine.player_bootstrap import PlayerBootstrap
    from Infernux.engine.play_mode import PlayModeState
    from Infernux.renderstack.render_stack import RenderStack

    calls = []

    class Scene:
        def serialize_document(self):
            calls.append("snapshot")
            return {"scene": "fresh"}

        def set_playing(self, playing):
            calls.append(("scene_playing", playing))

    scene = Scene()

    class NativeSceneManager:
        def get_active_scene(self):
            return scene

        def play(self):
            calls.append("native_play")

    native_scene_manager = NativeSceneManager()

    class SceneManagerBinding:
        @staticmethod
        def instance():
            return native_scene_manager

    class PlayModeManager:
        def __init__(self):
            self._state = PlayModeState.EDIT
            self._scene_backup = None
            self._last_frame_time = 0.0

        def _notify_state_change(self, old_state, new_state):
            calls.append(("notify", old_state, new_state))

    play_mode = PlayModeManager()

    class Engine:
        def get_play_mode_manager(self):
            return play_mode

    monkeypatch.setattr(native_lib, "SceneManager", SceneManagerBinding)
    monkeypatch.setattr("Infernux.timing.Time._reset", lambda: calls.append("time_reset"))
    monkeypatch.setattr(
        "Infernux.components.builtin.sprite_renderer.SpriteRenderer.init_all_in_scene",
        lambda current: calls.append(("sprite_init", current)),
    )
    monkeypatch.setattr(RenderStack, "_active_instance", object())

    bootstrap = PlayerBootstrap.__new__(PlayerBootstrap)
    bootstrap.engine = Engine()

    assert bootstrap._activate_initial_scene_for_play() is True
    assert play_mode._scene_backup == {"scene": "fresh"}
    assert play_mode._state is PlayModeState.PLAYING
    assert RenderStack._active_instance is None
    assert calls == [
        "snapshot",
        "time_reset",
        ("notify", PlayModeState.EDIT, PlayModeState.PLAYING),
        ("scene_playing", True),
        ("sprite_init", scene),
        "native_play",
    ]
