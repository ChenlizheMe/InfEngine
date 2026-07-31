"""UndoManager — the central undo/redo stack singleton."""

from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Callable, List, Optional

from Infernux.engine.undo._base import UndoCommand
from Infernux.engine.undo._helpers import _bump_inspector_values


class UndoManager:
    """Central undo/redo manager with save-point tracking.

    Exception-safe: if ``undo()`` or ``redo()`` raises, the stack entry
    is preserved (not lost).
    """

    MAX_STACK_DEPTH: int = 200
    _instance: Optional[UndoManager] = None

    def __init__(self) -> None:
        self._undo_stack: List[UndoCommand] = []
        self._redo_stack: List[UndoCommand] = []
        self._save_point: Optional[int] = 0
        self._base_scene_dirty: bool = False
        self._is_executing: bool = False
        self._enabled: bool = True
        self._suppress_property_recording: bool = False
        self._on_state_changed: Optional[Callable[[], None]] = None
        UndoManager._instance = self

    @classmethod
    def instance(cls) -> Optional[UndoManager]:
        return cls._instance

    # -- Context managers --

    @contextmanager
    def suppress(self):
        prev = self._is_executing
        self._is_executing = True
        try:
            yield
        finally:
            self._is_executing = prev

    @contextmanager
    def suppress_property_recording(self):
        prev = self._suppress_property_recording
        self._suppress_property_recording = True
        try:
            yield
        finally:
            self._suppress_property_recording = prev

    # -- Properties --

    @property
    def is_executing(self) -> bool:
        return self._is_executing

    @property
    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    @property
    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0

    @property
    def undo_description(self) -> str:
        return self._undo_stack[-1].description if self._undo_stack else ""

    @property
    def redo_description(self) -> str:
        return self._redo_stack[-1].description if self._redo_stack else ""

    @property
    def _dirty_depth(self) -> int:
        return sum(1 for cmd in self._undo_stack if cmd.marks_dirty)

    @property
    def is_at_save_point(self) -> bool:
        if self._save_point is None:
            return False
        return self._dirty_depth == self._save_point

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    # -- Core operations --

    def execute(self, cmd: UndoCommand) -> bool:
        from Infernux.debug import Debug

        if not self._enabled:
            try:
                cmd.execute()
                _bump_inspector_values()
            except Exception as exc:
                Debug.log_exception(exc)
                return False
            return True

        self._is_executing = True
        try:
            cmd.execute()
        except Exception as exc:
            self._is_executing = False
            Debug.log_exception(exc)
            self._debug_dump_stack("execute-failed")
            return False
        self._is_executing = False
        _bump_inspector_values()

        if self._suppress_property_recording and cmd._is_property_edit:
            return True

        self._push(cmd)
        return True

    def record(self, cmd: UndoCommand) -> None:
        if not self._enabled:
            return
        if self._suppress_property_recording and cmd._is_property_edit:
            return
        self._push(cmd)
        _bump_inspector_values()

    def undo(self) -> None:
        from Infernux.debug import Debug

        if not self._undo_stack:
            return
        cmd = self._undo_stack.pop()
        self._is_executing = True
        try:
            cmd.undo()
        except Exception as exc:
            self._is_executing = False
            self._undo_stack.append(cmd)
            Debug.log_exception(exc)
            self._debug_dump_stack("undo-failed")
            return
        self._is_executing = False
        _bump_inspector_values()

        if cmd.supports_redo:
            self._redo_stack.append(cmd)

        self._sync_dirty()
        self._fire_state_changed()
        self._debug_dump_stack("undo")

    def redo(self) -> None:
        from Infernux.debug import Debug

        if not self._redo_stack:
            return
        cmd = self._redo_stack.pop()
        self._is_executing = True
        try:
            cmd.redo()
        except Exception as exc:
            self._is_executing = False
            self._redo_stack.append(cmd)
            Debug.log_exception(exc)
            self._debug_dump_stack("redo-failed")
            return
        self._is_executing = False
        _bump_inspector_values()

        self._undo_stack.append(cmd)

        self._sync_dirty()
        self._fire_state_changed()
        self._debug_dump_stack("redo")

    def clear(self, scene_is_dirty: bool = False) -> None:
        self._undo_stack.clear()
        self._redo_stack.clear()
        self._save_point = 0
        self._base_scene_dirty = bool(scene_is_dirty)
        self._fire_state_changed()

    def set_scene_dirty_baseline(self, scene_is_dirty: bool) -> None:
        """Update scene dirty baseline without clearing stacks (e.g. after play mode exit)."""
        self._base_scene_dirty = bool(scene_is_dirty)
        self._sync_dirty()
        self._fire_state_changed()

    def mark_save_point(self) -> None:
        self._save_point = self._dirty_depth
        self._base_scene_dirty = False

    def sync_dirty_state(self) -> None:
        self._sync_dirty()

    def set_on_state_changed(self, cb: Optional[Callable[[], None]]) -> None:
        self._on_state_changed = cb

    # -- Private --

    def _push(self, cmd: UndoCommand) -> None:
        if self._undo_stack and self._undo_stack[-1].can_merge(cmd):
            self._undo_stack[-1].merge(cmd)
        else:
            self._undo_stack.append(cmd)
            if len(self._undo_stack) > self.MAX_STACK_DEPTH:
                overflow = len(self._undo_stack) - self.MAX_STACK_DEPTH
                dirty_dropped = sum(
                    1 for c in self._undo_stack[:overflow] if c.marks_dirty)
                del self._undo_stack[:overflow]
                if self._save_point is not None:
                    self._save_point -= dirty_dropped
                    if self._save_point < 0:
                        self._save_point = None

        self._redo_stack.clear()
        self._sync_dirty()
        self._fire_state_changed()
        self._debug_dump_stack("push")

    def _debug_dump_stack(self, action: str) -> None:
        if os.environ.get("INFERNUX_UNDO_TRACE") != "1":
            return
        from Infernux.debug import Debug
        pos = len(self._undo_stack)
        total = pos + len(self._redo_stack)
        parts = []
        for i, cmd in enumerate(self._undo_stack, 1):
            parts.append(f"{i}: {cmd.description}")
        for j, cmd in enumerate(self._redo_stack[::-1], pos + 1):
            parts.append(f"{j}: (redo) {cmd.description}")
        Debug.log(
            f"[UndoTrace] {action} pos={pos} total={total} "
            + " | ".join(parts)
        )

    def _sync_dirty(self) -> None:
        from Infernux.engine.play_mode import PlayModeManager, PlayModeState
        pm = PlayModeManager.instance()
        if pm and pm.state != PlayModeState.EDIT:
            return
        from Infernux.engine.scene_manager import SceneFileManager
        sfm = SceneFileManager.instance()
        if sfm is None:
            return
        if self._base_scene_dirty or not self.is_at_save_point:
            sfm.mark_dirty()
        else:
            sfm.clear_dirty()

    def _fire_state_changed(self) -> None:
        if self._on_state_changed:
            self._on_state_changed()
