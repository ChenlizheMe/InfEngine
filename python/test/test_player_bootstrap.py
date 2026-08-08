from __future__ import annotations

import sys
import types
import os


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


def test_player_bootstrap_forces_player_mode_before_engine_creation(monkeypatch):
    from Infernux.engine import engine as engine_module
    from Infernux.engine.player_bootstrap import PlayerBootstrap

    monkeypatch.delenv("_INFERNUX_PLAYER_MODE", raising=False)
    monkeypatch.setattr(engine_module, "_PLAYER_MODE", None)

    PlayerBootstrap._force_player_mode()

    assert os.environ["_INFERNUX_PLAYER_MODE"] == "1"
    assert engine_module._PLAYER_MODE == "1"


def test_player_starts_fresh_scene_without_second_document_transaction(monkeypatch):
    from Infernux.engine.player_bootstrap import PlayerBootstrap

    calls = []

    class PlayerRuntimeSession:
        def activate(self):
            calls.append("activate")
            return True

    class Engine:
        def get_player_runtime(self):
            return PlayerRuntimeSession()

    bootstrap = PlayerBootstrap.__new__(PlayerBootstrap)
    bootstrap.engine = Engine()
    bootstrap.runtime_session = None

    bootstrap._create_managers()
    assert bootstrap.runtime_session is not None

    assert bootstrap._activate_initial_scene_for_play() is True
    assert calls == ["activate"]


def test_player_runtime_session_does_not_construct_editor_managers():
    from Infernux.engine.player_bootstrap import PlayerBootstrap

    class RuntimeSession:
        pass

    class Engine:
        def get_player_runtime(self):
            return RuntimeSession()

    bootstrap = PlayerBootstrap.__new__(PlayerBootstrap)
    bootstrap.engine = Engine()
    bootstrap.runtime_session = None
    bootstrap._create_managers()

    assert bootstrap.runtime_session is not None
    assert getattr(bootstrap, "scene_file_manager", None) is None


def test_player_bootstrap_uses_boot_validated_archive_summary(monkeypatch):
    from Infernux.engine.player_bootstrap import PlayerBootstrap

    digest = "a" * 64
    monkeypatch.setenv("_INFERNUX_PLAYER_CONTENT_ARCHIVE_SHA256", digest)
    monkeypatch.setenv("_INFERNUX_PLAYER_CONTENT_ARCHIVE_BYTES", "4096")

    assert PlayerBootstrap._validated_archive_summary(
        "Game_Data/Content.inxpkg"
    ) == (digest, 4096)
