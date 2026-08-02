"""Authoritative Python filesystem path operations.

Display paths, persistent portable paths, and dictionary identity keys are
different concepts. Callers must choose the operation matching their intent
instead of assembling identity checks from ``abspath``/``normcase``.
"""

from __future__ import annotations

import hashlib
import os
import sys
from typing import TypeAlias

PathLike: TypeAlias = str | os.PathLike[str]


def _expand_windows_long_path(path: str) -> str:
    if sys.platform != "win32" or not path:
        return path

    import ctypes

    candidate = path
    suffix: list[str] = []
    while candidate and not os.path.exists(candidate):
        parent, name = os.path.split(candidate)
        if parent == candidate:
            break
        suffix.append(name)
        candidate = parent

    buffer = ctypes.create_unicode_buffer(32768)
    length = ctypes.windll.kernel32.GetLongPathNameW(candidate, buffer, len(buffer))
    if not length or length >= len(buffer):
        return path

    resolved = buffer.value
    for name in reversed(suffix):
        resolved = os.path.join(resolved, name)
    return resolved


def lexical_path(path: PathLike) -> str:
    """Return an absolute normalized path without consulting the filesystem."""
    if not path:
        return ""
    return os.path.abspath(os.path.normpath(os.fspath(path)))


def resolved_path(path: PathLike) -> str:
    """Return an absolute display/storage path with existing aliases resolved."""
    if not path:
        return ""
    lexical = lexical_path(path)
    resolved = os.path.realpath(lexical)
    return os.path.normpath(_expand_windows_long_path(resolved))


def path_key(path: PathLike) -> str:
    """Return the cross-platform identity key for a filesystem path."""
    return os.path.normcase(resolved_path(path))


def path_fingerprint(path: PathLike) -> str:
    """Return a stable, non-reversible identity for a filesystem path."""
    identity = path_key(path)
    if not identity:
        return ""
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def lexical_path_key(path: PathLike) -> str:
    """Return a disk-independent key for a path that has no known identity."""
    if not path:
        return ""
    return os.path.normcase(lexical_path(path))


def same_path(left: PathLike, right: PathLike) -> bool:
    """Return whether two path spellings refer to the same filesystem identity."""
    if not left or not right:
        return False
    try:
        if os.path.exists(left) and os.path.exists(right):
            return os.path.samefile(left, right)
    except OSError:
        pass
    return path_key(left) == path_key(right)


def is_path_within(path: PathLike, root: PathLike, *, allow_root: bool = True) -> bool:
    """Return whether *path* resolves inside *root* using path components."""
    candidate = path_key(path)
    parent = path_key(root)
    if not candidate or not parent:
        return False
    try:
        inside = os.path.commonpath((candidate, parent)) == parent
    except ValueError:
        return False
    return inside and (allow_root or candidate != parent)


def relative_path(
    path: PathLike,
    root: PathLike,
    *,
    resolve: bool = True,
    allow_root: bool = False,
) -> str:
    """Return a portable relative path, rejecting paths outside *root*."""
    normalize = resolved_path if resolve else lexical_path
    key = path_key if resolve else lexical_path_key
    candidate = normalize(path)
    parent = normalize(root)
    candidate_key = key(candidate)
    parent_key = key(parent)
    try:
        inside = os.path.commonpath((candidate_key, parent_key)) == parent_key
    except ValueError:
        inside = False
    if not inside or (not allow_root and candidate_key == parent_key):
        raise ValueError(f"Path is outside root: {candidate!r} is not under {parent!r}")
    relative = os.path.relpath(candidate, parent)
    if relative == "." and not allow_root:
        raise ValueError("Path must name an entry below the root")
    return portable_path(relative)


def portable_path(path: PathLike) -> str:
    """Normalize separators for a project-relative path stored in an asset."""
    if not path:
        return ""
    return os.path.normpath(os.fspath(path)).replace("\\", "/")


def safe_path(path: PathLike) -> str:
    """Normalize a Python path before crossing the UTF-8 C++ boundary."""
    return resolved_path(path)
