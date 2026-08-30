"""Platform-neutral support used by host desktop Player plugins."""

from __future__ import annotations

import platform
import sys
import time
from pathlib import Path
from typing import Iterable, Mapping

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
)


HOST_PLAYER_CAPABILITIES = PlatformCapabilities(
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


def host_machine_is_x64() -> bool:
    return platform.machine().strip().casefold() in {"amd64", "x86_64"}


def current_host_platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "linux"
    return ""


def current_host_player_target(
    targets: Iterable[BuildTarget],
) -> BuildTarget | None:
    """Select the installed target matching the current host and architecture."""

    if not host_machine_is_x64():
        return None
    host_platform = current_host_platform()
    if not host_platform:
        return None
    return next(
        (
            target
            for target in targets
            if target.platform.casefold() == host_platform
            and target.architecture.casefold() in {"amd64", "x86_64"}
        ),
        None,
    )


def inspect_host_player_request(
    request: BuildRequest,
    target: BuildTarget | None,
    *,
    exporter_id: str,
) -> CapabilityReport:
    if target is None or request.target != target.id:
        return CapabilityReport(
            False,
            (
                BuildDiagnostic(
                    DiagnosticSeverity.ERROR,
                    "host-player.target.unsupported",
                    "This Player target is not supported by the current host.",
                    source=exporter_id,
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
                "host-player.project.invalid",
                "Project root must contain Assets and ProjectSettings directories.",
                source=exporter_id,
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


def create_host_player_plan(request: BuildRequest) -> BuildPlan:
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


class _CancellationEvent:
    def __init__(self, request: BuildRequest) -> None:
        self._request = request

    def is_set(self) -> bool:
        return self._request.cancellation.cancelled


def execute_host_player_build(
    request: BuildRequest,
    plan: BuildPlan,
) -> BuildResult:
    del plan
    from Infernux.engine.game_builder import GameBuilder

    started = time.perf_counter()
    settings = _build_settings(request.profile.options)
    game_name = str(settings.get("game_name", "") or "").strip()
    if not game_name:
        game_name = Path(request.project_root).name
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
        request.report("desktop", int(round(bounded * 1000.0)), 1000, message)

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


__all__ = [
    "HOST_PLAYER_CAPABILITIES",
    "create_host_player_plan",
    "current_host_platform",
    "current_host_player_target",
    "execute_host_player_build",
    "host_machine_is_x64",
    "inspect_host_player_request",
]
