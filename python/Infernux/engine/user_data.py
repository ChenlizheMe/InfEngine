"""Authoritative per-user storage root for Editor and shared engine state."""

from __future__ import annotations

import os
from pathlib import Path

from .path_utils import resolved_path


DATA_ROOT_ENV = "INFERNUX_DATA_ROOT"


def get_infernux_data_root() -> str:
    """Return the Hub-owned data root used by source and installed Editors."""

    configured = os.environ.get(DATA_ROOT_ENV, "").strip()
    if configured:
        return resolved_path(os.path.expandvars(os.path.expanduser(configured)))
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if not local_app_data:
            raise RuntimeError("Infernux requires LOCALAPPDATA on Windows")
        return resolved_path(os.path.join(local_app_data, "InfernuxHub"))
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    base = (
        Path(xdg_data_home).expanduser()
        if xdg_data_home
        else Path.home() / ".local" / "share"
    )
    return resolved_path(base / "InfernuxHub")


def get_project_editor_layout_root(project_root: str | os.PathLike[str]) -> str:
    """Return the machine-local Editor layout directory owned by a project."""

    return resolved_path(os.path.join(project_root, "Cache", "Editor", "Layout"))


__all__ = [
    "DATA_ROOT_ENV",
    "get_infernux_data_root",
    "get_project_editor_layout_root",
]
