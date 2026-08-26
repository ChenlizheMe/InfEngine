from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from Infernux.engine import player_package_native


def _native_available() -> bool:
    try:
        player_package_native._backend()
        return not player_package_native.using_test_backend()
    except Exception:
        return False


def _prepare_project(project: Path) -> None:
    (project / "Assets").mkdir(parents=True)
    (project / "Assets" / "absence-probe.txt").write_text(
        "MCP is optional", encoding="utf-8"
    )
    (project / "ProjectSettings").mkdir()
    (project / "ProjectSettings/BuildSettings.json").write_text(
        json.dumps({"scenes": []}), encoding="utf-8"
    )


def _run_editor_without_mcp(project: Path, port: int) -> None:
    from Infernux.engine.bootstrap import EditorBootstrap
    from Infernux.engine.interaction import (
        DocumentActionStatus,
        DocumentRegistry,
        EditorInteractionCore,
    )
    from Infernux.engine.undo import UndoManager
    from Infernux.lib import SceneManager
    from Infernux.plugins import PluginManager

    _prepare_project(project)
    os.environ["LOCALAPPDATA"] = str(project / ".local-state")
    os.environ["INFERNUX_MCP_PORT"] = str(port)
    bootstrap = EditorBootstrap(str(project))
    executable = None
    try:
        bootstrap.run()
        manager = PluginManager.instance()
        assert manager is not None
        assert manager.registry.installed() == ()
        assert EditorInteractionCore.instance() is bootstrap.interaction_core
        assert not any(
            name == "infernux_mcp" or name.startswith("infernux_mcp.")
            for name in sys.modules
        )
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))

        database = bootstrap.engine.get_asset_database()
        asset_probe = project / "Assets" / "absence-probe.txt"
        assert database.get_guid_from_path(str(asset_probe))

        SceneManager.instance().get_active_scene().create_game_object(
            "No MCP Runtime Object"
        )
        scene_path = project / "Assets" / "NoMCP.scene"
        save = DocumentRegistry.instance().request_save_to_resource(
            bootstrap.scene_file_manager.document_id,
            str(scene_path),
        )
        assert save.status is DocumentActionStatus.APPLIED
        (project / "ProjectSettings" / "BuildSettings.json").write_text(
            json.dumps({"scenes": ["Assets/NoMCP.scene"]}),
            encoding="utf-8",
        )

        play = bootstrap.engine.get_play_mode_manager()
        assert play.enter_play_mode() is True
        from Infernux.engine.deferred_task import DeferredTaskRunner

        DeferredTaskRunner.instance().tick()
        assert play.is_playing is True
        assert play.exit_play_mode() is True
        DeferredTaskRunner.instance().tick()
        assert play.is_edit_mode is True

        from Infernux.engine.game_builder import GameBuilder
        from Infernux.engine.player_build_preflight import publish_player_asset_catalog

        output = project.parent / "NoMCPPlayer"
        builder = GameBuilder(
            str(project),
            str(output),
            game_name="NoMCPPlayer",
            display_mode="windowed",
            window_width=640,
            window_height=360,
            debug_mode=True,
            lto=False,
        )
        builder.freeze_asset_index_entries(
            list(publish_player_asset_catalog(str(project), database)["entries"])
        )
        built = Path(builder.build())
        executable = built / "NoMCPPlayer.exe"
        assert executable.is_file()
        assert not (project / "Packages" / "infernux" / "mcp").exists()
        assert not any(
            name == "infernux_mcp" or name.startswith("infernux_mcp.")
            for name in sys.modules
        )
    finally:
        if bootstrap.engine is not None:
            bootstrap.engine.exit()
        core = getattr(bootstrap, "interaction_core", None)
        if core is not None and EditorInteractionCore.instance() is core:
            core.shutdown()
        undo = getattr(bootstrap, "undo_manager", None)
        if undo is not None and UndoManager.instance() is undo:
            undo.shutdown()
        EditorBootstrap._instance = None

    assert executable is not None
    plugin_source = (
        Path(__file__).parents[2]
        / "external"
        / "plugins"
        / "infernux_mcp"
        / "Editor"
    )
    sys.path.insert(0, str(plugin_source))
    try:
        from infernux_mcp.supervisor import SupervisorSession

        supervisor = SupervisorSession(
            str(project),
            session_id="no-mcp-player-validation",
            build_profile="debug_feedback",
        )
        launched = supervisor.launch_player(
            str(executable), timeout_seconds=120.0
        )
        assert launched["player_ready"] is True, {
            "status": launched,
            "logs": supervisor.player_read_logs(limit=200),
        }
        stopped = supervisor.stop_player(timeout_seconds=30.0)
        assert stopped["stopped"] is True
        assert supervisor.player_read_logs(limit=200)["crash_lines"] == []
    finally:
        try:
            sys.path.remove(str(plugin_source))
        except ValueError:
            pass


@pytest.mark.skipif(not _native_available(), reason="native graphical engine unavailable")
def test_full_editor_bootstrap_remains_functional_without_mcp():
    repository = Path(__file__).parents[2]
    short_root = Path(tempfile.mkdtemp(prefix="inx-no-mcp-"))
    project = short_root / "Project"
    try:
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        environment = os.environ.copy()
        # Deliberately exclude the MCP source checkout. Production may only import
        # it after the package has been installed under the project Packages root.
        environment["PYTHONPATH"] = str(repository / "python")
        result = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--editor-without-mcp",
                str(project),
                str(port),
            ],
            cwd=repository,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=300,
            check=False,
        )
        assert result.returncode == 0, result.stdout + "\n" + result.stderr
    finally:
        shutil.rmtree(short_root, ignore_errors=True)


if __name__ == "__main__" and len(sys.argv) == 4 and sys.argv[1] == "--editor-without-mcp":
    _run_editor_without_mcp(Path(sys.argv[2]), int(sys.argv[3]))
