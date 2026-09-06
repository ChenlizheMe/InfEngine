"""Explicit migration of complete, reusable legacy Hub resources."""

from __future__ import annotations

import errno
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from hub_utils import get_hub_shared_data_dir, get_hub_user_data_dir, is_project_open


@dataclass(frozen=True)
class MigrationPlan:
    source: Path
    destination: Path
    items: tuple[Path, ...]
    conflicts: tuple[Path, ...]


def _inside(root: Path, relative: Path) -> Path:
    path = root / relative
    if path.resolve() != path or not path.is_relative_to(root) or path == root:
        raise ValueError(f"Migration path is redirected or outside its resource root: {path}")
    return path


def inspect_legacy_storage() -> MigrationPlan:
    """Enumerate finished resources only; never change the user's old data."""
    source = Path(get_hub_user_data_dir()).resolve()
    destination = Path(get_hub_shared_data_dir()).resolve()
    if source == destination:
        return MigrationPlan(source, destination, (), ())
    if source.is_relative_to(destination) or destination.is_relative_to(source):
        raise ValueError("Legacy and shared resource directories must not overlap")

    candidates: set[Path] = set()
    # The relative package location is the stable reference in project registries.
    if not os.environ.get("INFERNUX_PACKAGE_CACHE_ROOT", "").strip():
        candidates.update(source.glob("Library/Plugins/packages/**/*.inxpkg"))
    for directory in source.glob("Runtimes/python*"):
        if (directory / ".infernux-private-python-runtime.json").is_file():
            candidates.add(directory)
    for directory in source.glob("Engines/*"):
        if re.fullmatch(r"\d+\.\d+\.\d+(?:[-+._a-zA-Z0-9]*)?", directory.name):
            if directory.is_dir() and any(directory.glob("*.whl")):
                candidates.add(directory)
    for marker in source.glob("PlatformKits/android/*/*/infernux-android-support.json"):
        candidates.add(marker.parent)
    for pattern in (
        "Runtimes/cpython-*.tar.gz",
        "Downloads/RuntimeBootstrap/cpython-*.tar.gz",
    ):
        candidates.update(source.glob(pattern))

    items, conflicts = [], []
    for path in sorted(candidates):
        relative = path.relative_to(source)
        _inside(source, relative)
        target = _inside(destination, relative)
        (conflicts if target.exists() else items).append(relative)
    return MigrationPlan(source, destination, tuple(items), tuple(conflicts))


def _move_resource(source: Path, destination: Path) -> None:
    """Move a unit; cross-volume copy must finish before touching the original."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Migration will not overwrite an existing resource: {destination}")
    try:
        source.rename(destination)
        return
    except OSError as exc:
        if exc.errno != errno.EXDEV:
            raise
    # This is filesystem compatibility, not a retry after a broken resource.
    with tempfile.TemporaryDirectory(prefix=".hub-migrate-", dir=destination.parent) as staging:
        staged = Path(staging) / source.name
        if source.is_dir():
            # Do not traverse Windows junctions while copying/deleting a unit.
            for directory, children, _files in os.walk(source, followlinks=False):
                for child in children:
                    entry = Path(directory) / child
                    if entry.is_junction():
                        raise ValueError(f"Cannot migrate a resource containing a junction: {entry}")
            shutil.copytree(source, staged, symlinks=True)
        else:
            shutil.copy2(source, staged)
        if destination.exists() or destination.is_symlink():
            raise FileExistsError(f"Migration target appeared during copy: {destination}")
        staged.rename(destination)
    # At this point the destination is complete even if removal is interrupted.
    if source.is_dir():
        shutil.rmtree(source)
    else:
        source.unlink()


def migrate_legacy_storage(
    plan: MigrationPlan,
    project_roots: Iterable[str] = (),
    *,
    progress: Callable[[str], None] | None = None,
) -> tuple[Path, ...]:
    """Execute a user-approved snapshot, preserving collisions and private state."""
    for project in project_roots:
        if not Path(project).is_dir():
            raise FileNotFoundError(f"Cannot check whether a registered project is open: {project}")
        if is_project_open(project):
            raise RuntimeError(f"Close the Editor before migrating shared resources: {project}")
    if inspect_legacy_storage() != plan:
        raise RuntimeError("Shared resources changed after the migration preview; inspect them again")
    moved: list[Path] = []
    for relative in plan.items:
        source = _inside(plan.source, relative)
        destination = _inside(plan.destination, relative)
        if progress:
            progress(relative.as_posix())
        try:
            _move_resource(source, destination)
        except (OSError, ValueError) as exc:
            raise RuntimeError(
                f"Migration stopped at {relative.as_posix()}: {exc}\n"
                f"Completed resources before this item: {len(moved)}. "
                f"Destination for this item: {destination}. Published destinations are retained. "
                "If removal failed after copying, the destination is complete and the "
                "remaining original files are retained. Conflicts are not changed."
            ) from exc
        moved.append(relative)
        # Remove only newly empty ancestors, never the old user-data root.
        parent = source.parent
        while parent != plan.source:
            if any(parent.iterdir()):
                break
            parent.rmdir()
            parent = parent.parent
    return tuple(moved)
