"""Hub-owned inspection and cleanup for downloaded plugin packages."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable
from hub_utils import get_hub_shared_data_dir


PACKAGE_CACHE_ROOT_ENV = "INFERNUX_PACKAGE_CACHE_ROOT"
PACKAGE_EXTENSION = ".inxpkg"


def plugin_library_root() -> Path:
    """Return the one package library shared with every launched Editor."""

    configured = os.environ.get(PACKAGE_CACHE_ROOT_ENV, "").strip()
    if configured:
        return Path(os.path.expandvars(os.path.expanduser(configured))).resolve()
    return (Path(get_hub_shared_data_dir()) / "Library" / "Plugins").resolve()


def _cache_location(value: object) -> str:
    text = str(value or "").replace("\\", "/").strip("/")
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or not path.parts
        or path.parts[0] != "packages"
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.suffix.casefold() != PACKAGE_EXTENSION
    ):
        raise ValueError(f"Hub plugin cache location is invalid: {value!r}")
    return path.as_posix()


def _project_references(project_root: str | os.PathLike[str]) -> set[str]:
    project = Path(project_root).expanduser().resolve()
    if not project.is_dir():
        raise FileNotFoundError(
            "Cannot clean the plugin library while a registered project is "
            f"unavailable: {project}"
        )
    registry_path = project / "ProjectSettings" / "InxPlugins.json"
    if not registry_path.is_file():
        return set()
    try:
        document = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            "Cannot clean the plugin library because a project registry is "
            f"unreadable: {registry_path}"
        ) from exc
    if (
        not isinstance(document, dict)
        or document.get("$schema") != "infernux.plugin_registry"
        or not isinstance(document.get("packages"), list)
        or not isinstance(document.get("installed"), list)
    ):
        raise RuntimeError(
            "Cannot clean the plugin library because a project registry is "
            f"invalid: {registry_path}"
        )

    references: set[str] = set()
    for raw in (*document["packages"], *document["installed"]):
        if not isinstance(raw, dict):
            raise RuntimeError(
                "Cannot clean the plugin library because a package record is "
                f"invalid: {registry_path}"
            )
        source = raw.get("source")
        if not isinstance(source, dict):
            continue
        if str(source.get("cache_scope", "")).casefold() != "hub":
            continue
        try:
            references.add(_cache_location(source.get("cache_location", "")))
        except ValueError as exc:
            raise RuntimeError(
                "Cannot clean the plugin library because a project reference "
                f"is invalid: {registry_path}"
            ) from exc
    return references


@dataclass(frozen=True, slots=True)
class PluginLibraryStats:
    root: Path
    package_count: int
    total_bytes: int
    removable: tuple[Path, ...]
    removable_bytes: int


def inspect_plugin_library(
    project_roots: Iterable[str | os.PathLike[str]],
) -> PluginLibraryStats:
    """Classify package files after validating every registered project."""

    referenced: set[str] = set()
    for project_root in tuple(project_roots):
        referenced.update(_project_references(project_root))

    root = plugin_library_root()
    package_root = root / "packages"
    packages = tuple(
        sorted(
            (
                path
                for path in package_root.rglob(f"*{PACKAGE_EXTENSION}")
                if path.is_file()
            ),
            key=lambda path: path.as_posix().casefold(),
        )
    ) if package_root.is_dir() else ()
    removable = tuple(
        path
        for path in packages
        if PurePosixPath(path.relative_to(root).as_posix()).as_posix()
        not in referenced
    )
    sizes = {path: path.stat().st_size for path in packages}
    return PluginLibraryStats(
        root=root,
        package_count=len(packages),
        total_bytes=sum(sizes.values()),
        removable=removable,
        removable_bytes=sum(sizes[path] for path in removable),
    )


def prune_unreferenced_packages(
    project_roots: Iterable[str | os.PathLike[str]],
) -> PluginLibraryStats:
    """Delete only package versions unused by every registered Hub project."""

    before = inspect_plugin_library(project_roots)
    package_root = before.root / "packages"
    for path in before.removable:
        path.unlink()
    if package_root.is_dir():
        for directory, _children, _files in os.walk(package_root, topdown=False):
            candidate = Path(directory)
            if candidate != package_root:
                try:
                    candidate.rmdir()
                except OSError:
                    pass
    return inspect_plugin_library(project_roots)


__all__ = [
    "PACKAGE_CACHE_ROOT_ENV",
    "PluginLibraryStats",
    "inspect_plugin_library",
    "plugin_library_root",
    "prune_unreferenced_packages",
]
