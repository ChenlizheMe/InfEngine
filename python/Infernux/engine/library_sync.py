"""Synchronize engine resources from the Python package into the project Library."""

import logging
import json
import os
import shutil
import tempfile

from .path_utils import relative_path

_log = logging.getLogger("Infernux.library_sync")

_SKIP = {
    "__pycache__",
    "__init__.py",
    "__init__.pyi",
    "icons.zip",
    # These are wheel/build inputs, not project-visible engine resources.
    "player_runtime",
    "project_templates",
}
_SYNC_MANIFEST = ".InfernuxResources.json"
_SYNC_SCHEMA = 1


def _ignored_resource_entries(_directory: str, entries: list[str]) -> list[str]:
    """Keep package/build metadata out of the project Library cache."""
    return [entry for entry in entries if entry in _SKIP or entry.endswith(".meta")]


def _resource_snapshot(root: str) -> dict[str, dict[str, int]]:
    snapshot: dict[str, dict[str, int]] = {}
    for directory, folders, files in os.walk(root):
        folders[:] = sorted(folder for folder in folders if folder not in _SKIP)
        for filename in sorted(files):
            if filename in _SKIP or filename.endswith(".meta"):
                continue
            source = os.path.join(directory, filename)
            relative = relative_path(source, root)
            stat = os.stat(source)
            snapshot[relative] = {
                "size": int(stat.st_size),
                "modified_ns": int(stat.st_mtime_ns),
            }
    return snapshot


def _destination_files(root: str) -> set[str]:
    files: set[str] = set()
    for directory, _folders, filenames in os.walk(root):
        for filename in filenames:
            files.add(relative_path(os.path.join(directory, filename), root))
    return files


def _read_sync_manifest(path: str) -> dict[str, dict[str, int]]:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            document = json.load(stream)
        if document.get("schema") != _SYNC_SCHEMA:
            return {}
        entries = document.get("entries")
        return entries if isinstance(entries, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}


def _write_sync_manifest(path: str, entries: dict[str, dict[str, int]]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".InfernuxResources-", suffix=".tmp", dir=os.path.dirname(path)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                {"schema": _SYNC_SCHEMA, "entries": entries},
                stream,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        os.replace(temporary, path)
    except BaseException:
        try:
            os.remove(temporary)
        except OSError:
            pass
        raise


def sync_resources(project_path: str) -> str:
    """Copy package resources into ``<project>/Library/Resources``.

    Uses an incremental manifest so an unchanged editor launch does not delete
    and recopy every built-in shader, texture, and model before a window can be
    shown. Changed files are still mirrored exactly and stale files are removed.

    Returns the Library resources directory path.
    """
    from Infernux.resources import get_package_resources_path

    src = get_package_resources_path()
    dst = os.path.join(project_path, "Library", "Resources")
    manifest_path = os.path.join(project_path, "Library", _SYNC_MANIFEST)
    source_entries = _resource_snapshot(src)
    previous_entries = _read_sync_manifest(manifest_path)

    os.makedirs(dst, exist_ok=True)
    changed = 0
    for relative, identity in source_entries.items():
        source = os.path.join(src, *relative.split("/"))
        target = os.path.join(dst, *relative.split("/"))
        if previous_entries.get(relative) == identity and os.path.isfile(target):
            continue
        os.makedirs(os.path.dirname(target), exist_ok=True)
        shutil.copy2(source, target)
        changed += 1

    removed = 0
    # Library/Resources is an engine-owned mirror.  Enumerating destination
    # names keeps upgrades from retaining files produced before the manifest
    # existed, or other residue that was never recorded in it.
    for relative in _destination_files(dst) - source_entries.keys():
        target = os.path.join(dst, *relative.split("/"))
        try:
            os.remove(target)
            removed += 1
        except FileNotFoundError:
            pass

    for directory, folders, files in os.walk(dst, topdown=False):
        if directory == dst:
            continue
        if not folders and not files:
            try:
                os.rmdir(directory)
            except OSError:
                pass

    _write_sync_manifest(manifest_path, source_entries)

    _log.info("Synced engine resources -> %s (%d changed, %d removed)", dst, changed, removed)
    return dst
