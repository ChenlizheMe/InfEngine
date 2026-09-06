"""Read the project GUID catalog through the native AssetDatabase when available."""

from __future__ import annotations

import json
import os
from typing import Any

from Infernux.engine.path_utils import is_path_within, path_key, resolved_path


_SKIPPED_DIRECTORIES = frozenset({".git", "__pycache__", ".venv", "venv"})


def project_guid_paths(
    project_root: str,
    *,
    engine: Any = None,
) -> tuple[dict[str, str], bool]:
    """Return GUID-to-path mappings and whether the native index supplied them.

    The native AssetDatabase is the *only* GUID authority for its project:
    when one is bound to ``project_root`` its catalog is returned as-is and
    any error propagates. There is deliberately no fallback from a live
    database to a filesystem scan — a stale or pending catalog is handled by
    the post-refresh preload catch-up, not by re-deriving identity from
    ``.meta`` files. The scan below only serves environments that have no
    native database at all (pure-Python tooling and tests).
    """

    project = resolved_path(project_root)
    database = _asset_database(engine)
    if database is not None:
        database_root = resolved_path(str(database.project_root or ""))
        if database_root and path_key(database_root) == path_key(project):
            result: dict[str, str] = {}
            for raw_guid in database.get_all_guids():
                guid = str(raw_guid).strip().casefold()
                raw_path = str(database.get_path_from_guid(raw_guid) or "").strip()
                if not guid or not raw_path:
                    continue
                path = resolved_path(
                    raw_path if os.path.isabs(raw_path) else os.path.join(project, raw_path)
                )
                if not _active_project_path(path, project) or not os.path.isfile(path):
                    continue
                other = result.get(guid)
                if other is not None and path_key(other) != path_key(path):
                    raise ValueError(f"Project contains duplicate GUID {guid}: {other}, {path}")
                result[guid] = path
            return result, True
    return _scan_guid_paths(project), False


def _asset_database(engine: Any):
    if engine is not None:
        return engine.get_asset_database()

    from Infernux.core.assets import AssetManager

    return AssetManager._asset_database


def _scan_guid_paths(project_root: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for root_name in ("Assets", "Packages"):
        root = os.path.join(project_root, root_name)
        if not os.path.isdir(root):
            continue
        for walk_root, dirs, names in os.walk(root):
            dirs[:] = [
                name
                for name in dirs
                if name not in _SKIPPED_DIRECTORIES and not name.startswith(".")
            ]
            for name in names:
                if not name.endswith(".meta"):
                    continue
                asset = os.path.join(walk_root, name[:-5])
                if not os.path.isfile(asset):
                    continue
                try:
                    with open(os.path.join(walk_root, name), "r", encoding="utf-8") as stream:
                        document = json.load(stream)
                    guid = str(document["metadata"]["guid"]["value"]).strip().casefold()
                except (OSError, json.JSONDecodeError, KeyError, TypeError):
                    continue
                if not guid:
                    continue
                other = result.get(guid)
                if other is not None and path_key(other) != path_key(asset):
                    raise ValueError(f"Project contains duplicate GUID {guid}: {other}, {asset}")
                result[guid] = asset
    return result


def _active_project_path(path: str, project_root: str) -> bool:
    return any(
        is_path_within(
            path,
            os.path.join(project_root, root_name),
            allow_root=False,
        )
        for root_name in ("Assets", "Packages")
    )


__all__ = ["project_guid_paths"]
