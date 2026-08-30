from __future__ import annotations

import json
from pathlib import Path

import pytest

from Infernux.engine.build import (
    BuildArtifact,
    BuildPlan,
    BuildResult,
    BuildStep,
    BuildTarget,
    CapabilityReport,
    PlatformCapabilities,
    PlatformExporter,
    exporter_registry,
)
from Infernux.host import EditorAutomationHost, OperationError


class _FixtureExporter(PlatformExporter):
    @property
    def exporter_id(self) -> str:
        return "infernux/test-host-build"

    def targets(self):
        return (
            BuildTarget(
                "fixture-x64",
                "Fixture x64",
                "fixture",
                "x86_64",
                PlatformCapabilities(graphics_api="vulkan"),
            ),
        )

    def doctor(self, request):
        return CapabilityReport(True)

    def create_plan(self, request):
        return BuildPlan(
            request.target,
            (BuildStep("package", "Package fixture", "package"),),
        )

    def execute(self, request, plan):
        request.report("package", 1, 1, "Fixture published")
        artifact = Path(request.output_dir) / "fixture.bin"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_bytes(b"fixture")
        return BuildResult(
            request.target,
            True,
            (BuildArtifact(str(artifact), "fixture", size=7),),
            manifest={"configuration": request.profile.configuration.value},
        )


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "Project"
    (project / "Assets").mkdir(parents=True)
    settings = project / "ProjectSettings"
    settings.mkdir()
    (settings / "BuildSettings.json").write_text(
        json.dumps(
            {
                "game_name": "FixtureGame",
                "scenes": ["Assets/Main.scene"],
                "output_dir": str(tmp_path / "Build"),
            }
        ),
        encoding="utf-8",
    )
    return project


def test_host_build_routes_registered_target_and_returns_structured_result(
    monkeypatch, tmp_path
):
    registration = exporter_registry.register("test:host-build", _FixtureExporter())
    monkeypatch.setattr(
        "Infernux.engine.player_build_preflight.publish_player_asset_catalog_for_host",
        lambda _root: {"entries": [{"guid": "a" * 32}]},
    )
    project = _project(tmp_path)
    try:
        result = EditorAutomationHost().build_player(
            str(project),
            target="fixture-x64",
            persist_settings=True,
        )
    finally:
        exporter_registry.unregister(registration)

    assert result["target"] == "fixture-x64"
    assert result["artifacts"][0]["kind"] == "fixture"
    assert result["executable_path"] == ""
    assert result["progress"][-1]["phase"] == "complete"
    persisted = json.loads(
        (project / "ProjectSettings" / "BuildSettings.json").read_text(
            encoding="utf-8"
        )
    )
    assert persisted["build_target"] == "fixture-x64"


def test_host_build_reports_available_targets_for_missing_plugin(tmp_path):
    project = _project(tmp_path)

    with pytest.raises(OperationError) as error:
        EditorAutomationHost().build_player(
            str(project),
            target="android-arm64",
            persist_settings=False,
        )

    assert error.value.code == "player.target_unavailable"
    assert error.value.details["requested_target"] == "android-arm64"
    assert error.value.details["available_targets"]


def test_host_build_target_catalog_is_json_serializable():
    payload = EditorAutomationHost().player_build_targets()

    encoded = json.dumps(payload)

    assert "current_desktop_target" in encoded
    assert payload["targets"]
