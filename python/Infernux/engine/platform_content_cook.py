"""Shared GUID-based content cook for platform-owned Player hosts."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from Infernux.engine.build import BuildConfiguration, BuildRequest
from Infernux.engine.build_settings import load_build_settings
from Infernux.engine.game_builder import GameBuilder
from Infernux.engine.player_build_preflight import (
    publish_player_asset_catalog_for_host,
)
from Infernux.engine.path_utils import resolved_path


@dataclass(frozen=True, slots=True)
class PlatformContentCookResult:
    game_name: str
    data_directory: Path
    settings: Mapping[str, object]


def build_settings_for_request(request: BuildRequest) -> dict[str, object]:
    configured = request.profile.options.get("build_settings")
    if isinstance(configured, Mapping):
        return dict(configured)
    return dict(load_build_settings(request.project_root))


def cook_platform_content(
    request: BuildRequest,
    output_root: str | Path,
    *,
    platform_host: Mapping[str, object],
) -> PlatformContentCookResult:
    """Cook one immutable Player content closure for a native platform host."""

    settings = build_settings_for_request(request)
    game_name = str(settings.get("game_name", "")).strip() or Path(
        resolved_path(request.project_root)
    ).name
    root = Path(resolved_path(output_root))
    root.mkdir(parents=True, exist_ok=True)
    request.report("cook", 0, 1000, "Publishing current project asset catalog")
    if request.asset_catalog_entries:
        catalog_entries = [dict(item) for item in request.asset_catalog_entries]
    else:
        # Publish and fully release the temporary headless host before the
        # content builder creates runtime-facing caches.
        catalog = publish_player_asset_catalog_for_host(request.project_root)
        catalog_entries = list(catalog["entries"])
    builder = GameBuilder(
        request.project_root,
        str(root),
        game_name=game_name,
        icon_path=str(settings.get("icon_path", "") or "") or None,
        display_mode=str(settings.get("display_mode", "windowed")),
        window_width=int(settings.get("window_width", 1280)),
        window_height=int(settings.get("window_height", 720)),
        window_resizable=bool(settings.get("window_resizable", True)),
        splash_items=list(settings.get("splash_items", []) or []),
        debug_mode=request.profile.configuration
        is BuildConfiguration.DEVELOPMENT,
        lto=False,
        enable_jit=False,
    )
    builder.freeze_asset_index_entries(catalog_entries)

    def report(message: str, fraction: float) -> None:
        request.report(
            "cook",
            max(0, min(1000, int(float(fraction) * 1000))),
            1000,
            message,
        )

    cooked = Path(
        builder.cook_platform_content(
            str(root),
            platform_host=dict(platform_host),
            on_progress=report,
        )
    )
    if not cooked.is_dir():
        raise RuntimeError(
            f"Platform Player cook did not produce its data directory: {cooked}"
        )
    return PlatformContentCookResult(game_name, cooked, dict(settings))


__all__ = [
    "PlatformContentCookResult",
    "build_settings_for_request",
    "cook_platform_content",
]
