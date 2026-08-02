"""Constant-time invalidation for cached runtime UI command lists."""

from __future__ import annotations

from enum import Enum


_runtime_ui_revision = 1


def is_unchanged_ui_scalar(instance, name: str, value) -> bool:
    """Return whether an authored scalar assignment leaves UI state unchanged."""
    if name.startswith("_") or not isinstance(
        value, (str, bytes, int, float, bool, type(None), Enum)
    ):
        return False
    try:
        previous = object.__getattribute__(instance, name)
    except (AttributeError, KeyError):
        return False
    return type(previous) is type(value) and previous == value


def mark_runtime_ui_dirty() -> int:
    """Invalidate runtime UI command lists after a visual state mutation."""
    global _runtime_ui_revision
    _runtime_ui_revision += 1
    return _runtime_ui_revision


def get_runtime_ui_revision() -> int:
    """Return the current process-wide runtime UI revision."""
    return _runtime_ui_revision
