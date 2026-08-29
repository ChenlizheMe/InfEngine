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


@dataclass(frozen=True, slots=True)
class PlatformContentCookResult:
    game_name: str
    data_directory: Path
    settings: Mapping[str, object]


def cook_platform_content(
    request: BuildRequest,
    output_root: str | Path,
    *,
    platform_host: Mapping[str, object],
) -> PlatformContentCookResult:
    """Cook one immutable Player content closure for a native platform host."""

    settings = load_build_settings(request.project_root)
    game_name = str(settings.get("game_name", "")).strip() or Path(
        request.project_root
    ).resolve().name
    root = Path(output_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
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
    request.report("cook", 0, 1000, "Publishing current project asset catalog")
    catalog = publish_player_asset_catalog_for_host(request.project_root)
    builder.freeze_asset_index_entries(list(catalog["entries"]))

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


__all__ = ["PlatformContentCookResult", "cook_platform_content"]
