from __future__ import annotations

from pathlib import Path

import pytest

from Infernux.engine.build import (
    BuildArtifact,
    BuildDiagnostic,
    BuildExporterRegistry,
    BuildPlan,
    BuildProfile,
    BuildRequest,
    BuildResult,
    BuildService,
    BuildStep,
    BuildTarget,
    BuildTargetId,
    BuildUnavailableError,
    CapabilityReport,
    DiagnosticSeverity,
    PlatformCapabilities,
    PlatformExporter,
)
from Infernux.engine.build_cancellation import BuildCancelled


def _target(identifier: str = "fixture-x64") -> BuildTarget:
    return BuildTarget(
        BuildTargetId(identifier),
        "Fixture x64",
        "fixture",
        "x86_64",
        PlatformCapabilities(graphics_api="vulkan"),
    )


class _FixtureExporter(PlatformExporter):
    def __init__(self, target: BuildTarget | None = None, *, available: bool = True):
        self.target = target or _target()
        self.available = available
        self.calls: list[str] = []

    @property
    def exporter_id(self) -> str:
        return "infernux/test-fixture"

    def targets(self):
        return (self.target,)

    def doctor(self, request):
        self.calls.append("doctor")
        if self.available:
            return CapabilityReport(True, details={"toolchain": "fixture"})
        return CapabilityReport(
            False,
            (
                BuildDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "fixture.missing",
                    "Fixture toolchain is missing",
                ),
            ),
        )

    def create_plan(self, request):
        self.calls.append("plan")
        return BuildPlan(
            request.target,
            (BuildStep("package", "Package fixture", "package"),),
        )

    def execute(self, request, plan):
        self.calls.append("execute")
        request.report("package", 1, 2, "Writing fixture")
        return BuildResult(
            request.target,
            True,
            (BuildArtifact(str(Path(request.output_dir) / "fixture.bin"), "player"),),
        )

    def audit(self, request, result):
        self.calls.append("audit")
        return result

    def smoke(self, request, result):
        self.calls.append("smoke")
        return result


def _request(tmp_path, progress=None):
    return BuildRequest(
        str(tmp_path / "project"),
        BuildTargetId("fixture-x64"),
        str(tmp_path / "output"),
        BuildProfile(),
        progress=progress,
    )


def test_target_ids_and_graphics_backends_are_strict():
    assert BuildTargetId("android-x64-emulator") == "android-x64-emulator"
    with pytest.raises(ValueError, match="lowercase"):
        BuildTargetId("Android x64")
    with pytest.raises(ValueError, match="Vulkan or WebGPU"):
        PlatformCapabilities(graphics_api="OpenGL")


def test_registry_registration_is_atomic_and_owner_can_unload(tmp_path):
    registry = BuildExporterRegistry()
    exporter = _FixtureExporter()
    registration = registry.register("package:infernux/test", exporter)

    assert [item.id for item in registry.targets()] == ["fixture-x64"]
    assert registry.resolve("fixture-x64")[0] is exporter

    with pytest.raises(RuntimeError, match="already registered"):
        registry.register("package:infernux/duplicate", _FixtureExporter())
    assert [item.id for item in registry.targets()] == ["fixture-x64"]

    assert registry.unregister(registration)
    assert registry.targets() == ()
    with pytest.raises(KeyError, match="Unknown build target"):
        registry.resolve("fixture-x64")


def test_registry_can_remove_all_contributions_from_one_preload_owner():
    registry = BuildExporterRegistry()
    first = _FixtureExporter(_target("fixture-first"))

    class SecondExporter(_FixtureExporter):
        @property
        def exporter_id(self):
            return "infernux/test-fixture-second"

    registry.register("preload-guid", first)
    registry.register("preload-guid", SecondExporter(_target("fixture-second")))

    assert registry.unregister_owner("preload-guid") == 2
    assert registry.targets() == ()


def test_build_service_runs_one_exporter_pipeline_and_reports_progress(tmp_path):
    registry = BuildExporterRegistry()
    exporter = _FixtureExporter()
    registry.register("package:infernux/test", exporter)
    progress = []
    request = _request(tmp_path, progress.append)

    result = BuildService(registry).execute(request, run_smoke=True)

    assert result.success
    assert result.elapsed_seconds > 0
    assert exporter.calls == ["doctor", "plan", "execute", "audit", "smoke"]
    assert [item.phase for item in progress] == [
        "doctor",
        "doctor",
        "plan",
        "plan",
        "execute",
        "package",
        "audit",
        "audit",
        "smoke",
        "smoke",
        "complete",
    ]


def test_build_service_refuses_unavailable_target_before_planning(tmp_path):
    registry = BuildExporterRegistry()
    exporter = _FixtureExporter(available=False)
    registry.register("package:infernux/test", exporter)

    with pytest.raises(BuildUnavailableError) as error:
        BuildService(registry).create_plan(_request(tmp_path))

    assert error.value.diagnostics[0].code == "fixture.missing"
    assert exporter.calls == ["doctor"]


def test_build_service_does_not_audit_or_smoke_a_failed_execution(tmp_path):
    class FailingExporter(_FixtureExporter):
        def execute(self, request, plan):
            self.calls.append("execute")
            return BuildResult(request.target, False)

    registry = BuildExporterRegistry()
    exporter = FailingExporter()
    registry.register("package:infernux/test", exporter)
    progress = []

    result = BuildService(registry).execute(
        _request(tmp_path, progress.append),
        run_smoke=True,
    )

    assert not result.success
    assert exporter.calls == ["doctor", "plan", "execute"]
    assert [item.phase for item in progress] == [
        "doctor",
        "doctor",
        "plan",
        "plan",
        "execute",
        "complete",
    ]


def test_build_request_cancellation_stops_progress_delivery(tmp_path):
    request = _request(tmp_path)
    request.cancellation.cancel()
    with pytest.raises(BuildCancelled, match="Build cancelled"):
        request.report("doctor", 0, 1, "Checking")
