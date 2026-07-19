"""Base command classes for the undo system."""

from __future__ import annotations

import copy as _copy
import time as _time
from abc import ABC, abstractmethod
from typing import Any, Callable, List, Optional


class UndoCommand(ABC):
    """Base class for all undoable editor commands."""

    supports_redo: bool = True
    marks_dirty: bool = True
    _is_property_edit: bool = False

    def __init__(self, description: str = ""):
        self.description: str = description
        self.timestamp: float = _time.time()

    @abstractmethod
    def execute(self) -> None: ...

    @abstractmethod
    def undo(self) -> None: ...

    def redo(self) -> None:
        self.execute()

    def can_merge(self, other: UndoCommand) -> bool:
        return False

    def merge(self, other: UndoCommand) -> None:
        pass


class CompoundCommand(UndoCommand):
    """Groups multiple sub-commands into one undo step."""

    supports_redo = True

    def __init__(self, commands: List[UndoCommand], description: str = ""):
        super().__init__(description or "Compound")
        self._commands = list(commands)
        self.marks_dirty = any(c.marks_dirty for c in self._commands)

    def execute(self) -> None:
        for cmd in self._commands:
            cmd.execute()

    def undo(self) -> None:
        for cmd in reversed(self._commands):
            cmd.undo()

    def redo(self) -> None:
        for cmd in self._commands:
            cmd.redo()


class LambdaCommand(UndoCommand):
    """One-shot undoable action built from callables.

    Ideal for recording ad-hoc operations without defining a full command
    class.  Critical for future ShaderGraph undo support::

        from Infernux.engine.undo import UndoManager, LambdaCommand

        mgr = UndoManager.instance()
        old_state = capture_current_state()
        apply_new_state(new_state)
        mgr.record(LambdaCommand(
            "Connect Nodes",
            undo_fn=lambda: apply_new_state(old_state),
            redo_fn=lambda: apply_new_state(new_state),
        ))
    """

    def __init__(self, description: str,
                 undo_fn: Callable[[], None],
                 redo_fn: Callable[[], None],
                 marks_dirty: bool = True):
        super().__init__(description)
        self._undo_fn = undo_fn
        self._redo_fn = redo_fn
        self.marks_dirty = marks_dirty

    def execute(self) -> None:
        self._redo_fn()

    def undo(self) -> None:
        self._undo_fn()

    def redo(self) -> None:
        self._redo_fn()


_SNAPSHOT_UNHANDLED = object()


def _snapshot_math_value(val: Any) -> Any:
    cls = type(val)
    module_name = getattr(cls, "__module__", "")
    type_name = getattr(cls, "__name__", "")

    if not module_name.startswith(("Infernux.lib", "Infernux.math")):
        return _SNAPSHOT_UNHANDLED

    if type_name == "Vector2":
        return cls(val.x, val.y)
    if type_name == "Vector3":
        return cls(val.x, val.y, val.z)
    if type_name == "vec4f":
        return cls(val.x, val.y, val.z, val.w)
    if type_name == "quatf":
        return cls(val.w, val.x, val.y, val.z)

    return _SNAPSHOT_UNHANDLED


def _snapshot_custom_copy(val: Any) -> Any:
    cls = type(val)

    if getattr(cls, "__deepcopy__", None) is None and getattr(cls, "__copy__", None) is None:
        return _SNAPSHOT_UNHANDLED

    try:
        return _copy.deepcopy(val)
    except Exception:
        return _SNAPSHOT_UNHANDLED


def _snapshot_value(val: Any) -> Any:
    """Return a simple deep-ish copy suitable for undo storage."""
    if val is None or isinstance(val, (int, float, str, bool)):
        return val

    # RGBA colour fields: material ptype-7 ``[r,g,b,a]`` lists.
    try:
        from Infernux.components.serialized_field import is_rgba_storage, snapshot_rgba
        if is_rgba_storage(val):
            return snapshot_rgba(val)
    except Exception:
        pass

    if isinstance(val, list):
        return [_snapshot_value(v) for v in val]
    if isinstance(val, tuple):
        return tuple(_snapshot_value(v) for v in val)
    if isinstance(val, dict):
        return {k: _snapshot_value(v) for k, v in val.items()}

    math_snapshot = _snapshot_math_value(val)
    if math_snapshot is not _SNAPSHOT_UNHANDLED:
        return math_snapshot

    copied_value = _snapshot_custom_copy(val)
    if copied_value is not _SNAPSHOT_UNHANDLED:
        return copied_value

    return val
