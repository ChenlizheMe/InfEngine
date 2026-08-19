from __future__ import annotations

import sys
import types
import os

import pytest


def _runtime_contract(tmp_path):
    from Infernux.engine.player_service_graph import (
        PlayerRuntimeAssetCatalog,
        RuntimeFeatureSet,
        RuntimeFlavor,
        RuntimeProductManifest,
        player_manifest_service_section,
        runtime_policy_for,
    )

    flavor = RuntimeFlavor.PLAYER_RELEASE
    features = RuntimeFeatureSet()
    document = {
        "$schema": "infernux.player_runtime_manifest",
        "manifest_version": 1,
        "product": {"flavor": flavor.value},
        "features": features.to_manifest(),
        "runtime_policy": runtime_policy_for(flavor).to_manifest(),
        "services": player_manifest_service_section(flavor, features),
    }
    return (
        RuntimeProductManifest.from_document(document),
        PlayerRuntimeAssetCatalog.from_documents(
            str(tmp_path),
            {"artifacts": []},
            {"entries": []},
        ),
    )


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


def test_player_activates_initial_scene_without_editor_deferred_tasks(monkeypatch):
    from Infernux.engine.player_bootstrap import PlayerBootstrap

    _stub_engine_status(monkeypatch)
    activated = []
    bootstrap = PlayerBootstrap.__new__(PlayerBootstrap)
    bootstrap._activate_initial_scene_for_play = lambda: activated.append(True) or True

    bootstrap._enter_play_mode()
    assert activated == [True]


def test_player_bootstrap_forces_player_mode_before_engine_creation(monkeypatch):
    from Infernux.engine import engine as engine_module
    from Infernux.engine.player_bootstrap import PlayerBootstrap

    # Register an environment restoration even when the variable was absent.
    # ``_force_player_mode`` writes through ``os.environ`` directly.
    monkeypatch.setenv("_INFERNUX_PLAYER_MODE", "__pytest_restore__")
    monkeypatch.delenv("_INFERNUX_PLAYER_MODE")
    monkeypatch.setattr(engine_module, "_PLAYER_MODE", None)

    PlayerBootstrap._force_player_mode()

    assert os.environ["_INFERNUX_PLAYER_MODE"] == "1"
    assert engine_module._PLAYER_MODE == "1"


def test_player_starts_fresh_scene_without_second_document_transaction(
    monkeypatch, tmp_path
):
    from Infernux.engine.player_bootstrap import PlayerBootstrap

    calls = []

    class PlayerRuntimeSession:
        def configure_runtime_contract(self, manifest, catalog):
            calls.append(("configure", manifest, catalog))

        def activate(self):
            calls.append("activate")
            return True

    class Engine:
        def get_player_runtime(self):
            return PlayerRuntimeSession()

    bootstrap = PlayerBootstrap.__new__(PlayerBootstrap)
    bootstrap.engine = Engine()
    bootstrap.runtime_session = None
    bootstrap._runtime_manifest, bootstrap._runtime_catalog = _runtime_contract(tmp_path)

    bootstrap._create_managers()
    assert bootstrap.runtime_session is not None

    assert bootstrap._activate_initial_scene_for_play() is True
    assert calls[0][0] == "configure"
    assert calls[-1] == "activate"


def test_player_runtime_session_does_not_construct_editor_managers(tmp_path):
    from Infernux.engine.player_bootstrap import PlayerBootstrap

    class RuntimeSession:
        def configure_runtime_contract(self, _manifest, _catalog):
            return None

    class Engine:
        def get_player_runtime(self):
            return RuntimeSession()

    bootstrap = PlayerBootstrap.__new__(PlayerBootstrap)
    bootstrap.engine = Engine()
    bootstrap.runtime_session = None
    bootstrap._runtime_manifest, bootstrap._runtime_catalog = _runtime_contract(tmp_path)
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


def test_player_run_loads_scene_without_starting_play():
    from Infernux.engine.player_bootstrap import PlayerBootstrap

    calls = []

    class Engine:
        def prepare_startup_refresh(self):
            calls.append("prepare")

    bootstrap = PlayerBootstrap.__new__(PlayerBootstrap)
    bootstrap.engine = Engine()
    bootstrap._force_player_mode = lambda: calls.append("force")
    bootstrap._load_runtime_contract = lambda: calls.append("contract")
    bootstrap._init_engine = lambda: calls.append("engine")
    bootstrap._create_managers = lambda: calls.append("managers")
    bootstrap._setup_game_camera = lambda: calls.append("camera")
    bootstrap._register_player_gui = lambda: calls.append("gui")
    bootstrap._load_initial_scene = lambda: calls.append("scene")
    bootstrap._enter_play_mode = lambda: calls.append("play")

    bootstrap.run()

    assert "play" not in calls
    assert calls == [
        "force",
        "contract",
        "engine",
        "managers",
        "camera",
        "gui",
        "scene",
        "prepare",
    ]


def test_player_bootstrap_does_not_discover_project_requirements(monkeypatch):
    from Infernux.engine.player_bootstrap import PlayerBootstrap

    calls = []
    bootstrap = PlayerBootstrap.__new__(PlayerBootstrap)
    bootstrap._validate_runtime_manifest = lambda: calls.append("manifest")
    bootstrap._apply_runtime_policy = lambda: calls.append("policy")

    bootstrap._load_runtime_contract()

    assert calls == ["manifest", "policy"]


def test_player_release_policy_rejects_debug_control_environment(monkeypatch, tmp_path):
    from Infernux.engine.player_bootstrap import PlayerBootstrap

    bootstrap = PlayerBootstrap.__new__(PlayerBootstrap)
    bootstrap.project_path = str(tmp_path)
    bootstrap.splash_items = []
    bootstrap._runtime_manifest, bootstrap._runtime_catalog = _runtime_contract(tmp_path)
    monkeypatch.setenv("_INFERNUX_PLAYER_DEBUG_BUILD", "0")
    monkeypatch.setenv("_INFERNUX_PLAYER_CONTROL_FILE", "commands.json")

    with pytest.raises(RuntimeError, match="cannot enable the debug control"):
        bootstrap._apply_runtime_policy()


def test_player_supervisor_scene_override_resolves_cooked_catalog_artifact(
    monkeypatch, tmp_path
):
    import json

    from Infernux.engine.player_bootstrap import PlayerBootstrap

    settings = tmp_path / "ProjectSettings"
    settings.mkdir()
    (settings / "BuildSettings.json").write_text(
        json.dumps(
            {
                "scenes": [
                    "Assets/Scenes/Start.scene",
                    "Assets/Scenes/VoxelContinent.scene",
                ]
            }
        ),
        encoding="utf-8",
    )
    cooked = tmp_path / "Library" / "Artifacts" / "voxel.inxscene"
    cooked.parent.mkdir(parents=True)
    cooked.write_text("{}", encoding="utf-8")

    loaded = []

    class Manifest:
        @staticmethod
        def require_service(service):
            assert service == "player_scene_service"

    class RuntimeSession:
        @staticmethod
        def load_scene(path):
            loaded.append(path)
            return True

    bootstrap = PlayerBootstrap.__new__(PlayerBootstrap)
    bootstrap.project_path = str(tmp_path)
    bootstrap._runtime_manifest = Manifest()
    bootstrap.runtime_session = RuntimeSession()
    bootstrap._resolve_runtime_scene = lambda reference: (
        str(cooked)
        if str(reference).replace("\\", "/")
        == "Assets/Scenes/VoxelContinent.scene"
        else None
    )
    monkeypatch.setenv(
        "_INFERNUX_PLAYER_START_SCENE",
        "Assets/Scenes/VoxelContinent.scene",
    )

    bootstrap._load_initial_scene()

    assert loaded == [str(cooked)]


def test_run_player_reveals_window_without_startup_sleep():
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath(
        "Infernux", "engine", "__init__.py"
    ).read_text(encoding="utf-8")
    start = source.index("def run_player")
    body = source[start : source.index("\n__all__ =", start)]
    assert "time.sleep" not in body
    assert "_INFERNUX_PLAYER_FULLSCREEN" in body
    assert "_INFERNUX_PLAYER_WINDOW_TITLE" in body
    assert body.index("_INFERNUX_PLAYER_FULLSCREEN") < body.index("bootstrap.run()")
    assert "_signal_engine_loaded" in body


def test_player_init_engine_publishes_window_chrome_before_native_renderer():
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath(
        "Infernux", "engine", "player_bootstrap.py"
    ).read_text(encoding="utf-8")
    start = source.index("def _init_engine")
    body = source[start : source.index("\n    def ", start + 1)]
    assert body.index("_INFERNUX_PLAYER_FULLSCREEN") < body.index("init_renderer")
    assert body.index("_INFERNUX_PLAYER_WINDOW_TITLE") < body.index("init_renderer")


def test_scene_transaction_invokes_on_tick_while_waiting():
    from pathlib import Path

    source = Path(__file__).resolve().parents[1].joinpath(
        "Infernux", "engine", "runtime_scene_transaction.py"
    ).read_text(encoding="utf-8")
    start = source.index("def run_to_completion")
    body = source[start : start + 500]
    assert "on_tick" in body
    assert "on_tick is not None" in body
