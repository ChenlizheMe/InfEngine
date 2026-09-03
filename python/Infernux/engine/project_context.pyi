"""Project context — global project root and script-path resolution.

Provides the single source of truth for the currently-open project path
and helpers for resolving relative script paths (including ``.py → .pyc``
fallback in packaged builds).

Example::

    from Infernux.engine.project_context import set_project_root, resolve_script_path

    set_project_root("/path/to/project")
    abs_path = resolve_script_path("Assets/Scripts/player.py")
"""

from __future__ import annotations

from typing import Optional


def set_project_root(path: Optional[str]) -> None:
    """Set the current project root for path normalisation.

    Args:
        path: Absolute path to the project directory, or ``None`` to clear.
    """
    ...

def get_project_root() -> Optional[str]:
    """Return the current project root, or ``None`` if not set."""
    ...

def get_project_script_roots(project_root: Optional[str] = ...) -> tuple[str, ...]:
    """Return the project's Assets and Packages source roots."""
    ...

def package_script_role(path: str, project_root: Optional[str] = ...) -> str:
    """Return the canonical role of an installed package script."""
    ...

def is_project_component_script(path: str, project_root: Optional[str] = ...) -> bool:
    """Return whether a script belongs to Assets or package Runtime code."""
    ...

def get_script_module_name(
    path: Optional[str], project_root: Optional[str] = ...
) -> Optional[str]:
    """Return the deterministic module identity for project-owned code."""
    ...

def get_script_import_paths(path: Optional[str] = ...) -> list[str]:
    """Return explicit import roots for one project-owned script."""
    ...

def resolve_script_path(path: Optional[str]) -> Optional[str]:
    """Resolve a possibly-relative script path to an absolute path.

    In packaged builds, ``.py`` sources are compiled to ``.pyc``.
    If the ``.py`` path does not exist but a corresponding ``.pyc`` does,
    the ``.pyc`` path is returned.

    Args:
        path: Relative or absolute path to a script file.
    """
    ...

def resolve_guid_to_path(guid: str) -> Optional[str]:
    """Resolve a script GUID using the build-time manifest.

    In packaged builds, ``_script_guid_map.json`` maps GUIDs to relative
    ``.pyc`` paths.

    Args:
        guid: The script asset GUID string.
    """
    ...
