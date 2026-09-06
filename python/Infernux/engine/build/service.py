"""Single build service used by Editor, CLI, Headless, and MCP frontends."""

from __future__ import annotations

import time
from dataclasses import replace

from .contracts import (
    BuildDiagnostic,
    BuildPlan,
    BuildRequest,
    BuildResult,
    DiagnosticSeverity,
)
from .registry import BuildExporterRegistry, exporter_registry


class BuildUnavailableError(RuntimeError):
    def __init__(self, diagnostics: tuple[BuildDiagnostic, ...]) -> None:
        self.diagnostics = diagnostics
        message = "; ".join(item.message for item in diagnostics) or (
            "The selected build target is unavailable"
        )
        super().__init__(message)


class BuildService:
    def __init__(self, registry: BuildExporterRegistry | None = None) -> None:
        self.registry = registry or exporter_registry

    def create_plan(self, request: BuildRequest) -> BuildPlan:
        exporter, _target = self.registry.resolve(request.target)
        request.report("doctor", 0, 1, "Checking target toolchain")
        report = exporter.doctor(request)
        request.report("doctor", 1, 1, "Target toolchain check complete")
        if not report.available:
            diagnostics = report.diagnostics or (
                BuildDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "build.target.unavailable",
                    f"Build target is unavailable: {request.target}",
                    source=exporter.exporter_id,
                    detail=report.details,
                ),
            )
            raise BuildUnavailableError(diagnostics)
        request.report("plan", 0, 1, "Creating build plan")
        plan = exporter.create_plan(request)
        if plan.target != request.target:
            raise RuntimeError(
                f"Exporter {exporter.exporter_id} returned a plan for {plan.target}, "
                f"expected {request.target}"
            )
        request.report("plan", 1, 1, "Build plan ready", steps=len(plan.steps))
        return plan

    def execute(
        self,
        request: BuildRequest,
        plan: BuildPlan | None = None,
        *,
        run_smoke: bool = False,
    ) -> BuildResult:
        exporter, _target = self.registry.resolve(request.target)
        accepted_plan = plan or self.create_plan(request)
        if accepted_plan.target != request.target:
            raise ValueError("Build plan target does not match the build request")
        started = time.perf_counter()
        request.report("execute", 0, 1, "Executing build plan")
        result = exporter.execute(request, accepted_plan)
        self._validate_result(request, exporter.exporter_id, result)
        if result.success:
            request.report("audit", 0, 1, "Auditing build artifacts")
            result = exporter.audit(request, result)
            self._validate_result(request, exporter.exporter_id, result)
            request.report("audit", 1, 1, "Build artifact audit complete")
        if run_smoke and result.success:
            request.report("smoke", 0, 1, "Running target smoke test")
            result = exporter.smoke(request, result)
            self._validate_result(request, exporter.exporter_id, result)
            request.report("smoke", 1, 1, "Target smoke test complete")
        elapsed = max(result.elapsed_seconds, time.perf_counter() - started)
        result = replace(result, elapsed_seconds=elapsed)
        request.report(
            "complete",
            1,
            1,
            "Build complete" if result.success else "Build failed",
            success=result.success,
            artifacts=len(result.artifacts),
        )
        return result

    @staticmethod
    def _validate_result(
        request: BuildRequest,
        exporter_id: str,
        result: BuildResult,
    ) -> None:
        if not isinstance(result, BuildResult):
            raise TypeError(f"Exporter {exporter_id} did not return BuildResult")
        if result.target != request.target:
            raise RuntimeError(
                f"Exporter {exporter_id} returned a result for {result.target}, "
                f"expected {request.target}"
            )


build_service = BuildService()


__all__ = ["BuildService", "BuildUnavailableError", "build_service"]
