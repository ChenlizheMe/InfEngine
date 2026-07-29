"""Remove stale Infernux installations and pip rename leftovers."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import site
import sysconfig
import time


_RESIDUE_PATTERN = re.compile(
    r"^~+(?:nfernux(?:[-_.].*)?|infernux(?:[-_.].*)?|lib\d+)$",
    re.IGNORECASE,
)
_DIST_INFO_PATTERN = re.compile(r"^infernux(?:[-_.].*)?\.dist-info$", re.IGNORECASE)
_EDITABLE_PATTERN = re.compile(
    r"^__editable__(?:\.|_+)infernux(?:[-_.].*)?(?:\.pth|_finder\.py)$",
    re.IGNORECASE,
)
_EGG_LINK_PATTERN = re.compile(r"^infernux(?:[-_.].*)?\.egg-link$", re.IGNORECASE)


def _site_package_roots(explicit: list[str]) -> tuple[Path, ...]:
    candidates = [Path(value) for value in explicit]
    if not candidates:
        paths = sysconfig.get_paths()
        candidates.extend(Path(paths[key]) for key in ("purelib", "platlib") if paths.get(key))
        candidates.extend(Path(value) for value in site.getsitepackages())

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        key = os.path.normcase(str(resolved))
        if key not in seen:
            seen.add(key)
            unique.append(resolved)
    return tuple(unique)


def _matches(entry: Path, purge: bool) -> bool:
    name = entry.name
    if (
        _RESIDUE_PATTERN.fullmatch(name)
        or _EDITABLE_PATTERN.fullmatch(name)
        or _EGG_LINK_PATTERN.fullmatch(name)
    ):
        return True
    if not purge:
        return False
    return name.casefold() == "infernux" or _DIST_INFO_PATTERN.fullmatch(name) is not None


def _remove(path: Path) -> None:
    error: OSError | None = None
    for attempt in range(12):
        try:
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            return
        except OSError as exc:
            error = exc
            time.sleep(0.2 * (attempt + 1))
    raise RuntimeError(
        f"cannot remove stale Python package path {path}; close running Infernux editors/players and retry"
    ) from error


def clean(roots: tuple[Path, ...], purge: bool) -> list[Path]:
    removed: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for entry in tuple(root.iterdir()):
            if _matches(entry, purge):
                _remove(entry)
                removed.append(entry)
    return removed


def verify(roots: tuple[Path, ...]) -> None:
    residues = [entry for root in roots if root.is_dir() for entry in root.iterdir() if _matches(entry, False)]
    if residues:
        raise RuntimeError("stale pip directories remain: " + ", ".join(str(path) for path in residues))

    packages = [root / "Infernux" for root in roots if (root / "Infernux").is_dir()]
    if len(packages) != 1:
        raise RuntimeError(f"expected exactly one installed Infernux package, found {len(packages)}")
    metadata = tuple(packages[0].rglob("*.meta"))
    if metadata:
        raise RuntimeError(f"installed Infernux package contains derived metadata: {metadata[0]}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("residues", "purge", "verify"))
    parser.add_argument("--site-packages", action="append", default=[])
    args = parser.parse_args()

    roots = _site_package_roots(args.site_packages)
    if args.mode == "verify":
        verify(roots)
        return

    removed = clean(roots, purge=args.mode == "purge")
    for path in removed:
        print(f"Removed stale Python package path: {path}")


if __name__ == "__main__":
    main()
