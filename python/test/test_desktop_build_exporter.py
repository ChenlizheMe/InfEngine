from __future__ import annotations

from pathlib import Path
from Infernux.engine.build import (
    BuildConfiguration,
    BuildExporterRegistry,
    BuildProfile,
    BuildRequest,
    DesktopPlatformExporter,
    current_desktop_target,
    ensure_desktop_exporter_registered,
)


def _request(tmp_path: Path, **options) -> BuildRequest:
    target = current_desktop_target()
    assert target is not None
    project = tmp_path / "Project"
    (project / "Assets").mkdir(parents=True)
    (project / "ProjectSettings").mkdir()
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


def test_desktop_exporter_contributes_only_the_current_vulkan_target():
    target = current_desktop_target()
    assert target is not None

    targets = DesktopPlatformExporter().targets()

    assert targets == (target,)
    assert target.id in {"windows-x64", "linux-x64"}
    assert target.capabilities.graphics_api == "vulkan"


def test_desktop_exporter_registration_is_idempotent():
    registry = BuildExporterRegistry()

    registration = ensure_desktop_exporter_registered(registry)
    repeated = ensure_desktop_exporter_registered(registry)

    assert registration is not None
    assert repeated is None
    assert registry.resolve(current_desktop_target().id)[0].exporter_id == (
        "infernux/platform-desktop"
    )


def test_desktop_exporter_doctor_rejects_invalid_project(tmp_path):
    target = current_desktop_target()
    request = BuildRequest(
        str(tmp_path / "MissingProject"),
        target.id,
        str(tmp_path / "Player"),
    )

    report = DesktopPlatformExporter().doctor(request)

    assert not report.available
    assert [item.code for item in report.diagnostics] == ["desktop.project.invalid"]


def test_desktop_exporter_routes_settings_catalog_progress_and_cancellation(
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

    result = DesktopPlatformExporter().execute(
        request,
        DesktopPlatformExporter().create_plan(request),
    )

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


def test_desktop_exporter_publishes_catalog_for_a_clean_standalone_project(
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
    result = DesktopPlatformExporter().execute(
        request,
        DesktopPlatformExporter().create_plan(request),
    )

    assert result.success
    assert captured["entries"] == [{"guid": "b" * 32}]
    assert lifecycle == ["publish", "builder"]
