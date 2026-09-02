from __future__ import annotations

import json
import sys
from pathlib import Path

from Infernux.engine.build import (
    BuildConfiguration,
    BuildExporterRegistry,
    BuildProfile,
    BuildRequest,
    current_host_player_target,
    exporter_registry,
)
from Infernux.plugins import InxPackage, PluginManager


PLUGIN_ROOT = Path(__file__).resolve().parents[2] / "external" / "plugins"
if sys.platform == "win32":
    EDITOR_ROOT = PLUGIN_ROOT / "infernux_windows" / "Editor"
    sys.path.insert(0, str(EDITOR_ROOT))
    from infernux_windows import WindowsPlatformExporter as HostPlatformExporter
    from infernux_windows.exporter import windows_target as host_target
else:
    EDITOR_ROOT = PLUGIN_ROOT / "infernux_linux" / "Editor"
    sys.path.insert(0, str(EDITOR_ROOT))
    from infernux_linux import LinuxPlatformExporter as HostPlatformExporter
    from infernux_linux.exporter import linux_target as host_target


def _request(tmp_path: Path, **options) -> BuildRequest:
    target = host_target()
    assert target is not None
    project = tmp_path / "Project"
    (project / "Assets").mkdir(parents=True)
    (project / "ProjectSettings").mkdir()
    (project / "ProjectSettings" / "BuildSettings.json").write_text(
        json.dumps({"scenes": []}), encoding="utf-8"
    )
    entries = options.pop("asset_catalog_entries", ())
    return BuildRequest(
        str(project),
        target.id,
        str(tmp_path / "Player"),
        BuildProfile(
            configuration=BuildConfiguration.RELEASE,
            options=options,
        ),
        asset_catalog_entries=entries,
    )


def test_host_plugin_contributes_only_the_current_vulkan_target():
    target = host_target()
    assert target is not None

    targets = HostPlatformExporter().targets()

    assert targets == (target,)
    assert target.id in {"windows-x64", "linux-x64"}
    assert target.capabilities.graphics_api == "vulkan"
    assert current_host_player_target(targets) == target


def test_host_plugin_owns_its_registered_target():
    registry = BuildExporterRegistry()
    exporter = HostPlatformExporter()
    registration = registry.register("package:host-platform", exporter)

    target = host_target()
    assert target is not None
    assert registry.resolve(target.id)[0] is exporter

    registry.unregister(registration)
    assert registry.targets() == ()


def test_host_exporter_doctor_rejects_invalid_project(tmp_path):
    target = host_target()
    assert target is not None
    request = BuildRequest(
        str(tmp_path / "MissingProject"),
        target.id,
        str(tmp_path / "Player"),
    )

    report = HostPlatformExporter().doctor(request)

    assert not report.available
    assert [item.code for item in report.diagnostics] == [
        "host-player.project.invalid"
    ]


def test_host_exporter_routes_settings_catalog_progress_and_cancellation(
    monkeypatch, tmp_path
):
    captured = {}

    class _Builder:
        def __init__(self, project, output, **kwargs):
            captured.update(project=project, output=output, kwargs=kwargs)

        def freeze_asset_index_entries(self, entries):
            captured["entries"] = entries

        def _validate_output_directory(self):
            captured["validated"] = True

        def build(self, *, on_progress, cancel_event):
            captured["cancel_event"] = cancel_event
            on_progress("Cooking", 0.25)
            return captured["output"]

    monkeypatch.setattr("Infernux.engine.game_builder.GameBuilder", _Builder)
    progress = []
    request = _request(
        tmp_path,
        build_settings={
            "game_name": "Balance040",
            "display_mode": "windowed",
            "window_width": 960,
            "window_height": 540,
            "window_resizable": False,
            "lto": False,
            "enable_jit": True,
            "splash_items": [],
        },
        asset_catalog_entries=[{"guid": "a" * 32}],
    )
    object.__setattr__(request, "progress", progress.append)
    exporter = HostPlatformExporter()

    result = exporter.execute(request, exporter.create_plan(request))

    assert result.success
    assert result.artifacts[0].kind == "player-directory"
    assert captured["kwargs"]["game_name"] == "Balance040"
    assert captured["kwargs"]["display_mode"] == "windowed"
    assert captured["kwargs"]["debug_mode"] is False
    assert captured["entries"] == [{"guid": "a" * 32}]
    assert captured["validated"] is True
    assert not captured["cancel_event"].is_set()
    assert [(item.phase, item.completed, item.total) for item in progress] == [
        ("desktop", 250, 1000)
    ]


def test_host_exporter_publishes_catalog_for_a_clean_standalone_project(
    monkeypatch, tmp_path
):
    captured = {}
    lifecycle = []

    class _Builder:
        def __init__(self, *_args, **_kwargs):
            lifecycle.append("builder")

        def freeze_asset_index_entries(self, entries):
            captured["entries"] = entries

        def _validate_output_directory(self):
            pass

        def build(self, **_kwargs):
            return str(tmp_path / "Player")

    monkeypatch.setattr("Infernux.engine.game_builder.GameBuilder", _Builder)

    def publish(root):
        lifecycle.append("publish")
        return {
            "path": str(Path(root) / "Library" / "AssetIndex.json"),
            "entries": [{"guid": "b" * 32}],
        }

    monkeypatch.setattr(
        "Infernux.engine.player_build_preflight.publish_player_asset_catalog_for_host",
        publish,
    )

    request = _request(tmp_path)
    exporter = HostPlatformExporter()
    result = exporter.execute(request, exporter.create_plan(request))

    assert result.success
    assert captured["entries"] == [{"guid": "b" * 32}]
    assert lifecycle == ["publish", "builder"]


def test_host_build_target_follows_installed_plugin_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setenv(
        "INFERNUX_PACKAGE_CACHE_ROOT",
        str(tmp_path / "hub-package-cache"),
    )
    project = tmp_path / "Project"
    (project / "Assets").mkdir(parents=True)
    (project / "ProjectSettings").mkdir()
    source_name = "infernux_windows" if sys.platform == "win32" else "infernux_linux"
    package = tmp_path / "host-platform.inxpkg"
    InxPackage.export_source(str(PLUGIN_ROOT / source_name), str(package))
    reference = f"infernux/platform-{'windows' if sys.platform == 'win32' else 'linux'}"
    expected_target = "windows-x64" if sys.platform == "win32" else "linux-x64"
    manager = PluginManager(str(project), runtime=False)
    exporter_registry.clear()

    try:
        assert exporter_registry.targets() == ()

        state = manager.install_package(str(package), install_dependencies=False)
        assert state.loaded is True
        assert [str(item.id) for item in exporter_registry.targets()] == [
            expected_target
        ]

        manager.set_enabled(reference, False)
        assert exporter_registry.targets() == ()

        state = manager.set_enabled(reference, True)
        assert state.loaded is True
        assert [str(item.id) for item in exporter_registry.targets()] == [
            expected_target
        ]

        manager.uninstall(reference)
        assert exporter_registry.targets() == ()
    finally:
        manager.shutdown()
        exporter_registry.clear()
