"""Remove stale Infernux installations and pip rename leftovers."""

from __future__ import annotations

import argparse
import ctypes
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


def _path_is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _loaded_windows_package_modules(roots: tuple[Path, ...]) -> list[tuple[int, str, Path]]:
    """Return processes that currently map a module from an installed package.

    pip renames a package directory before deleting it on Windows.  A loaded
    native module then prevents deletion and leaves the environment without an
    ``Infernux`` package, but with a partial ``~nfernux`` tree.  Detect that
    state before pip is allowed to mutate the installation.
    """
    if os.name != "nt":
        return []

    package_roots = tuple(
        candidate.resolve()
        for root in roots
        for candidate in (root / "Infernux",)
        if candidate.is_dir()
    )
    if not package_roots:
        return []

    from ctypes import wintypes

    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    enum_processes = psapi.EnumProcesses
    enum_processes.argtypes = (
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    enum_processes.restype = wintypes.BOOL
    enum_modules = psapi.EnumProcessModulesEx
    enum_modules.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.HMODULE),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
    )
    enum_modules.restype = wintypes.BOOL
    module_filename = psapi.GetModuleFileNameExW
    module_filename.argtypes = (
        wintypes.HANDLE,
        wintypes.HMODULE,
        wintypes.LPWSTR,
        wintypes.DWORD,
    )
    module_filename.restype = wintypes.DWORD
    open_process = kernel32.OpenProcess
    open_process.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    open_process.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL

    process_ids = (wintypes.DWORD * 8192)()
    bytes_returned = wintypes.DWORD()
    if not enum_processes(process_ids, ctypes.sizeof(process_ids), ctypes.byref(bytes_returned)):
        raise ctypes.WinError(ctypes.get_last_error())

    PROCESS_QUERY_INFORMATION = 0x0400
    PROCESS_VM_READ = 0x0010
    LIST_MODULES_ALL = 0x03
    process_count = bytes_returned.value // ctypes.sizeof(wintypes.DWORD)
    loaded: list[tuple[int, str, Path]] = []
    for pid in process_ids[:process_count]:
        if not pid:
            continue
        handle = open_process(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not handle:
            continue
        try:
            capacity = 256
            modules = (wintypes.HMODULE * capacity)()
            needed = wintypes.DWORD()
            if not enum_modules(
                handle,
                modules,
                ctypes.sizeof(modules),
                ctypes.byref(needed),
                LIST_MODULES_ALL,
            ):
                continue
            module_count = min(needed.value // ctypes.sizeof(wintypes.HMODULE), capacity)
            process_name = f"pid {pid}"
            for index, module in enumerate(modules[:module_count]):
                buffer = ctypes.create_unicode_buffer(32768)
                if not module_filename(handle, module, buffer, len(buffer)):
                    continue
                module_path = Path(buffer.value)
                if index == 0:
                    process_name = module_path.name or process_name
                if any(_path_is_within(module_path, root) for root in package_roots):
                    loaded.append((int(pid), process_name, module_path))
                    break
        finally:
            close_handle(handle)
    return loaded


def guard_not_loaded(roots: tuple[Path, ...]) -> None:
    loaded = _loaded_windows_package_modules(roots)
    if not loaded:
        return
    details = "; ".join(
        f"{name} (pid {pid}, module {module})" for pid, name, module in loaded
    )
    raise RuntimeError(
        "the installed Infernux package is in use; close all editors/players "
        f"before installing a wheel: {details}"
    )


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
    parser.add_argument("mode", choices=("guard", "residues", "purge", "verify"))
    parser.add_argument("--site-packages", action="append", default=[])
    args = parser.parse_args()

    roots = _site_package_roots(args.site_packages)
    if args.mode == "guard":
        guard_not_loaded(roots)
        return
    if args.mode == "verify":
        verify(roots)
        return

    removed = clean(roots, purge=args.mode == "purge")
    for path in removed:
        print(f"Removed stale Python package path: {path}")


if __name__ == "__main__":
    main()
