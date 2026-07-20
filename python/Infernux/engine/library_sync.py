"""Synchronize engine resources from the Python package into the project Library."""

import logging
import os
import shutil

_log = logging.getLogger("Infernux.library_sync")

_SKIP = {"__pycache__", "__init__.py", "__init__.pyi", "icons.zip"}


def _ignored_resource_entries(_directory: str, entries: list[str]) -> list[str]:
    """Keep package/build metadata out of the project Library cache."""
    return [entry for entry in entries if entry in _SKIP or entry.endswith(".meta")]


def sync_resources(project_path: str) -> str:
    """Copy package resources into ``<project>/Library/Resources``.

    Performs a clean copy every launch so the Library always mirrors the
    current Python environment's built-in resources.

    Returns the Library resources directory path.
    """
    from Infernux.resources import get_package_resources_path

    src = get_package_resources_path()
    dst = os.path.join(project_path, "Library", "Resources")

    if os.path.isdir(dst):
        shutil.rmtree(dst)

    shutil.copytree(
        src, dst,
        ignore=_ignored_resource_entries,
    )

    _log.info("Synced engine resources → %s", dst)
    return dst
