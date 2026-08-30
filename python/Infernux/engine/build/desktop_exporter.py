"""Core desktop Player exporter backed by the existing GameBuilder pipeline."""

from __future__ import annotations

import platform
import sys
import time
from pathlib import Path
from typing import Mapping

from .contracts import (
    BuildArtifact,
    BuildConfiguration,
    BuildDiagnostic,
    BuildPlan,
    BuildRequest,
    BuildResult,
    BuildStep,
    BuildTarget,
    CapabilityReport,
    DiagnosticSeverity,
    PlatformCapabilities,
    PlatformExporter,
)
from .registry import BuildExporterRegistry, ExporterRegistration, exporter_registry


_DESKTOP_CAPABILITIES = PlatformCapabilities(
    graphics_api="vulkan",
    threads=True,
    dynamic_loading=True,
    filesystem=True,
    network=True,
    audio=True,
    pointer_input=True,
    text_input=True,
    gamepad_input=True,
    python_native_modules=True,
    numba=True,
    persistent_storage=True,
)


def current_desktop_target() -> BuildTarget | None:
    """Describe the desktop Player target supported by the current host."""

    machine = platform.machine().strip().casefold()
    if machine not in {"amd64", "x86_64"}:
        return None
    if sys.platform == "win32":
        return BuildTarget(
            "windows-x64",
            "Windows x64",
            "windows",
            "x86_64",
            _DESKTOP_CAPABILITIES,
        )
    if sys.platform.startswith("linux"):
        return BuildTarget(
            "linux-x64",
            "Linux x64",
            "linux",
            "x86_64",
            _DESKTOP_CAPABILITIES,
        )
    return None


class _CancellationEvent:
    """GameBuilder-compatible view over the public build cancellation token."""

    def __init__(self, request: BuildRequest) -> None:
        self._request = request

    def is_set(self) -> bool:
        return self._request.cancellation.cancelled


class DesktopPlatformExporter(PlatformExporter):
    @property
    def exporter_id(self) -> str:
        return "infernux/platform-desktop"

    def targets(self):
        target = current_desktop_target()
        return (target,) if target is not None else ()

    def doctor(self, request: BuildRequest) -> CapabilityReport:
        target = current_desktop_target()
        if target is None or request.target != target.id:
            return CapabilityReport(
                False,
                (
                    BuildDiagnostic(
                        DiagnosticSeverity.ERROR,
                        "desktop.host.unsupported",
                        "Desktop Player builds require a Windows x64 or Linux x64 host.",
                        source=self.exporter_id,
                    ),
                ),
            )
        project = Path(request.project_root)
        diagnostics = []
        if not (project / "Assets").is_dir() or not (
            project / "ProjectSettings"
        ).is_dir():
            diagnostics.append(
                BuildDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "desktop.project.invalid",
                    "Project root must contain Assets and ProjectSettings directories.",
                    source=self.exporter_id,
                )
            )
        return CapabilityReport(
            not diagnostics,
            tuple(diagnostics),
            {
                "host_platform": sys.platform,
                "host_architecture": platform.machine(),
            },
        )

    def create_plan(self, request: BuildRequest) -> BuildPlan:
        return BuildPlan(
            request.target,
            (
                BuildStep("runtime", "Prepare desktop runtime", "prepare"),
                BuildStep("cook", "Cook project content", "cook"),
                BuildStep("package", "Assemble desktop Player", "package"),
                BuildStep("audit", "Audit desktop Player", "audit"),
            ),
            {"graphics_api": "vulkan"},
        )

    def execute(self, request: BuildRequest, plan: BuildPlan) -> BuildResult:
        from Infernux.engine.game_builder import GameBuilder

        started = time.perf_counter()
        settings = _build_settings(request.profile.options)
        game_name = str(settings.get("game_name", "") or "").strip()
        if not game_name:
            game_name = Path(request.project_root).name
        # A standalone build may need a temporary headless Engine to publish
        # the AssetDatabase snapshot. Complete that lifecycle before creating
        # GameBuilder and its runtime-facing caches.
        if request.asset_catalog_entries:
            catalog_entries = [dict(item) for item in request.asset_catalog_entries]
        else:
            from Infernux.engine.player_build_preflight import (
                publish_player_asset_catalog_for_host,
            )

            catalog = publish_player_asset_catalog_for_host(request.project_root)
            catalog_entries = [dict(item) for item in catalog["entries"]]
        builder = GameBuilder(
            request.project_root,
            request.output_dir,
            game_name=game_name,
            icon_path=str(settings.get("icon_path", "") or "").strip() or None,
            display_mode=str(
                settings.get("display_mode", "fullscreen_borderless")
                or "fullscreen_borderless"
            ),
            window_width=int(settings.get("window_width", 1280)),
            window_height=int(settings.get("window_height", 720)),
            window_resizable=bool(settings.get("window_resizable", True)),
            splash_items=list(settings.get("splash_items", []) or []),
            debug_mode=(
                request.profile.configuration is BuildConfiguration.DEVELOPMENT
            ),
            lto=bool(settings.get("lto", True)),
            enable_jit=bool(settings.get("enable_jit", False)),
        )
        builder.freeze_asset_index_entries(catalog_entries)
        builder._validate_output_directory()

        def _progress(message: str, fraction: float) -> None:
            bounded = max(0.0, min(1.0, float(fraction)))
            request.report(
                "desktop",
                int(round(bounded * 1000.0)),
                1000,
                message,
            )

        output = builder.build(
            on_progress=_progress,
            cancel_event=_CancellationEvent(request),
        )
        return BuildResult(
            request.target,
            True,
            artifacts=(BuildArtifact(str(output), "player-directory"),),
            manifest={
                "output_dir": str(output),
                "graphics_api": "vulkan",
                "configuration": request.profile.configuration.value,
            },
            elapsed_seconds=time.perf_counter() - started,
        )


def _build_settings(options: Mapping[str, object]) -> Mapping[str, object]:
    value = options.get("build_settings", {})
    return value if isinstance(value, Mapping) else {}


def ensure_desktop_exporter_registered(
    registry: BuildExporterRegistry | None = None,
) -> ExporterRegistration | None:
    """Register the host-owned desktop target without masking collisions."""

    accepted_registry = registry or exporter_registry
    target = current_desktop_target()
    if target is None:
        return None
    try:
        exporter, _descriptor = accepted_registry.resolve(target.id)
    except KeyError:
        return accepted_registry.register(
            "core:infernux/platform-desktop",
            DesktopPlatformExporter(),
        )
    if exporter.exporter_id != "infernux/platform-desktop":
        raise RuntimeError(
            f"The core desktop build target is already owned by {exporter.exporter_id}"
        )
    return None


__all__ = [
    "DesktopPlatformExporter",
    "current_desktop_target",
    "ensure_desktop_exporter_registered",
]
