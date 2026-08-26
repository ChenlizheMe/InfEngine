"""Shared publication barrier for standalone Player builds.

Every build entry point must hand :class:`GameBuilder` an immutable snapshot
of an AssetIndex produced *after* editor document writes have reached disk.
The docked Build Settings UI has an incremental variant of this barrier; this
module provides the synchronous form used by automation and headless hosts.
"""

from __future__ import annotations

import os
from typing import Any

from Infernux.engine.path_utils import resolved_path


def publish_player_asset_catalog(project_root: str, asset_database: Any) -> dict[str, Any]:
    """Flush authoring writes, rebuild derived products, and snapshot AssetIndex."""

    requested_root = str(project_root or "").strip()
    if not requested_root:
        raise RuntimeError("No project root found")
    root = resolved_path(requested_root)
    if asset_database is None:
        raise RuntimeError("The editor asset database is unavailable")

    from Infernux.core.assets import AssetManager
    from Infernux.renderstack.discovery import discover_effect_features

    AssetManager.flush_all_asset_writes()
    discover_effect_features()
    asset_database.refresh()

    from Infernux.particle.artifact import ParticleArtifactRegistry

    try:
        ParticleArtifactRegistry.ensure_project_compiled(root, raise_on_error=True)
    except Exception as exc:
        raise RuntimeError(f"Particle artifact compile failed: {exc}") from exc

    asset_database.flush_derived_index()
    index_path = str(getattr(asset_database, "asset_index_path", "") or "")
    if not index_path or not os.path.isfile(index_path):
        raise RuntimeError(
            "The editor could not publish the current Library/AssetIndex.json"
        )

    from Infernux.engine.runtime_artifact_catalog import load_asset_index

    return {"path": index_path, "entries": load_asset_index(root)}


__all__ = ["publish_player_asset_catalog"]
