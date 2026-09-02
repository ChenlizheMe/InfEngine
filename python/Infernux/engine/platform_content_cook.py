"""Shared GUID-based content cook for platform-owned Player hosts."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Mapping

from Infernux.engine.build import BuildConfiguration, BuildRequest
from Infernux.engine.build_settings import load_build_settings_for_build
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


def read_cooked_player_icon(
    data_directory: str | Path,
    *,
    default_icon: str | Path,
) -> bytes:
    """Read the icon already sealed by the shared Player content cook.

    Platform exporters consume the cooked ``BuildManifest`` instead of
    re-reading editor settings.  An empty project icon selects the explicit
    engine default supplied by the host distribution.  A configured icon is
    read from the validated ``Content.inxpkg`` produced by the cook; it is not
    looked up again through its authoring path.
    """

    data_root = Path(resolved_path(data_directory))
    manifest_path = data_root / "BuildManifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(
            f"Player branding manifest is unreadable: {manifest_path}"
        ) from error
    if not isinstance(manifest, dict):
        raise ValueError("Player branding manifest must be a JSON object")

    relative = str(manifest.get("icon_path", "") or "").strip()
    if relative:
        normalized = Path(relative.replace("\\", "/"))
        if normalized.is_absolute() or ".." in normalized.parts:
            raise ValueError(f"Player icon path escapes the cooked data root: {relative}")
        from Infernux.engine.player_package_native import read_entry

        archive = data_root / "Content.inxpkg"
        if not archive.is_file():
            raise ValueError(f"Cooked Player content package is missing: {archive}")
        try:
            payload = read_entry(archive, normalized.as_posix())
        except (OSError, RuntimeError, ValueError) as error:
            raise ValueError(f"Cooked Player icon is missing: {relative}") from error
        if not payload:
            raise ValueError(f"Cooked Player icon is empty: {relative}")
        return payload

    icon = Path(resolved_path(default_icon))
    if not icon.is_file():
        raise ValueError(f"Default Player icon is missing: {icon}")
    payload = icon.read_bytes()
    if not payload:
        raise ValueError(f"Default Player icon is empty: {icon}")
    return payload


def build_settings_for_request(request: BuildRequest) -> dict[str, object]:
    from Infernux.engine.interaction.project_settings import (
        normalize_build_settings,
    )

    if "build_settings" in request.profile.options:
        configured = request.profile.options["build_settings"]
        if not isinstance(configured, Mapping):
            raise TypeError(
                "BuildProfile.options['build_settings'] must be a mapping"
            )
        return normalize_build_settings(dict(configured))
    return load_build_settings_for_build(request.project_root)


def cook_platform_content(
    request: BuildRequest,
    output_root: str | Path,
    *,
    platform_host: Mapping[str, object],
) -> PlatformContentCookResult:
    """Cook one immutable Player content closure for a native platform host."""

    settings = build_settings_for_request(request)
    game_name = str(settings["game_name"]).strip() or Path(
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
        icon_guid=str(settings["icon_guid"]),
        display_mode=str(settings["display_mode"]),
        window_width=int(settings["window_width"]),
        window_height=int(settings["window_height"]),
        window_resizable=bool(settings["window_resizable"]),
        splash_items=list(settings["splash_items"]),
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
    "read_cooked_player_icon",
]
