"""UndoManager compatibility surface backed by the global action journal."""

from __future__ import annotations

from contextlib import contextmanager
import os
from typing import Callable, Optional

from Infernux.engine.interaction import (
    ActionOrigin,
    EditorActionJournal,
    EditorContextSnapshot,
)
from Infernux.engine.undo._base import UndoCommand
from Infernux.engine.undo._helpers import _bump_inspector_values


class UndoManager:
    """Execute and replay editor actions through one chronological cursor."""

    MAX_STACK_DEPTH: int = 200
    _instance: Optional["UndoManager"] = None

    def __init__(self, journal: Optional[EditorActionJournal] = None) -> None:
        self._journal = journal or EditorActionJournal(self.MAX_STACK_DEPTH)
        self._save_signature: Optional[tuple[tuple[str, int], ...]] = ()
        self._base_scene_dirty: bool = False
        self._is_executing: bool = False
        self._enabled: bool = True
        self._suppress_property_recording: bool = False
        self._on_state_changed: Optional[Callable[[], None]] = None
        self._context_provider: Optional[Callable[[], EditorContextSnapshot]] = None
        self._context_restorer: Optional[
            Callable[[EditorContextSnapshot, str], None]
        ] = None
        UndoManager._instance = self

    @classmethod
    def instance(cls) -> Optional["UndoManager"]:
        return cls._instance

    @property
    def action_journal(self) -> EditorActionJournal:
        return self._journal

    # Read-only compatibility views. New code must query action_journal.
    @property
    def _undo_stack(self) -> list[UndoCommand]:
        return [entry.action for entry in self._journal.applied_entries()]

    @property
    def _redo_stack(self) -> list[UndoCommand]:
        return [entry.action for entry in self._journal.redo_entries()]

    @contextmanager
    def suppress(self):
        previous = self._is_executing
        self._is_executing = True
        try:
            yield
        finally:
            self._is_executing = previous

    @contextmanager
    def suppress_property_recording(self):
        previous = self._suppress_property_recording
        self._suppress_property_recording = True
        try:
            yield
        finally:
            self._suppress_property_recording = previous

    @property
    def is_executing(self) -> bool:
        return self._is_executing

    @property
    def can_undo(self) -> bool:
        return self._journal.can_undo

    @property
    def can_redo(self) -> bool:
        return self._journal.can_redo

    @property
    def undo_description(self) -> str:
        entry = self._journal.peek_undo()
        return entry.action.description if entry is not None else ""

    @property
    def redo_description(self) -> str:
        entry = self._journal.peek_redo()
        return entry.action.description if entry is not None else ""

    @property
    def _dirty_depth(self) -> int:
        return len(self._journal.dirty_signature())

    @property
    def is_at_save_point(self) -> bool:
        return (
            self._save_signature is not None
            and self._journal.dirty_signature() == self._save_signature
        )

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    def set_context_hooks(
        self,
        provider: Optional[Callable[[], EditorContextSnapshot]],
        restorer: Optional[Callable[[EditorContextSnapshot, str], None]],
    ) -> None:
        self._context_provider = provider
        self._context_restorer = restorer

    @contextmanager
    def transaction(self, description: str):
        from Infernux.engine.interaction import EditorTransaction

        transaction = EditorTransaction(description)
        try:
            with self.suppress():
                yield transaction
        except Exception:
            transaction.rollback()
            raise
        else:
            command = transaction.commit()
            if command is not None:
                self.record(command, transaction_id=transaction.transaction_id)

    def execute(
        self,
        cmd: UndoCommand,
        *,
        origin: ActionOrigin = ActionOrigin.USER,
        transaction_id: str = "",
    ) -> bool:
        from Infernux.debug import Debug

        before_context = self._capture_context()
        if not self._enabled:
            try:
                cmd.execute()
                _bump_inspector_values()
            except Exception as exc:
                Debug.log_exception(exc)
                cmd.dispose()
                return False
            cmd.dispose()
            return True

        self._is_executing = True
        try:
            cmd.execute()
        except Exception as exc:
            Debug.log_exception(exc)
            self._debug_dump_stack("execute-failed")
            cmd.dispose()
            return False
        finally:
            self._is_executing = False
        _bump_inspector_values()

        if self._suppress_property_recording and cmd._is_property_edit:
            cmd.dispose()
            return True

        after_context = self._capture_context()
        before_selection = cmd.before_selection_snapshot
        after_selection = cmd.after_selection_snapshot
        if before_context is not None and before_selection is not None:
            before_context = before_context.with_selection(before_selection)
        if after_context is not None and after_selection is not None:
            after_context = after_context.with_selection(after_selection)
        self._push(
            cmd,
            before_context=before_context,
            after_context=after_context,
            origin=origin,
            transaction_id=transaction_id,
        )
        return True

    def record(
        self,
        cmd: UndoCommand,
        *,
        origin: ActionOrigin = ActionOrigin.USER,
        transaction_id: str = "",
        before_context: Optional[EditorContextSnapshot] = None,
        after_context: Optional[EditorContextSnapshot] = None,
    ) -> None:
        if not self._enabled:
            cmd.dispose()
            return
        if self._suppress_property_recording and cmd._is_property_edit:
            cmd.dispose()
            return

        current_context = self._capture_context()
        before_context = before_context or current_context
        after_context = after_context or current_context
        before_selection = cmd.before_selection_snapshot
        after_selection = cmd.after_selection_snapshot
        if before_context is not None and before_selection is not None:
            before_context = before_context.with_selection(before_selection)
        if after_context is not None and after_selection is not None:
            after_context = after_context.with_selection(after_selection)
        self._push(
            cmd,
            before_context=before_context,
            after_context=after_context,
            origin=origin,
            transaction_id=transaction_id,
        )
        _bump_inspector_values()

    def undo(self) -> None:
        from Infernux.debug import Debug

        entry = self._journal.peek_undo()
        if entry is None:
            return
        self._restore_context(entry.after_context, "prepare_undo")
        self._is_executing = True
        try:
            entry.action.undo()
        except Exception as exc:
            Debug.log_exception(exc)
            self._debug_dump_stack("undo-failed")
            return
        finally:
            self._is_executing = False

        self._journal.commit_undo(entry)
        self._restore_context(entry.before_context, "undo_complete")
        _bump_inspector_values()
        self._sync_dirty()
        self._fire_state_changed()
        self._debug_dump_stack("undo")

    def redo(self) -> None:
        from Infernux.debug import Debug

        entry = self._journal.peek_redo()
        if entry is None:
            return
        self._restore_context(entry.before_context, "prepare_redo")
        self._is_executing = True
        try:
            entry.action.redo()
        except Exception as exc:
            Debug.log_exception(exc)
            self._debug_dump_stack("redo-failed")
            return
        finally:
            self._is_executing = False

        self._journal.commit_redo(entry)
        self._restore_context(entry.after_context, "redo_complete")
        _bump_inspector_values()
        self._sync_dirty()
        self._fire_state_changed()
        self._debug_dump_stack("redo")

    def clear(self, scene_is_dirty: bool = False) -> None:
        self._journal.clear()
        self._save_signature = ()
        self._base_scene_dirty = bool(scene_is_dirty)
        self._fire_state_changed()

    def set_scene_dirty_baseline(self, scene_is_dirty: bool) -> None:
        self._base_scene_dirty = bool(scene_is_dirty)
        self._sync_dirty()
        self._fire_state_changed()

    def mark_save_point(self) -> None:
        self._save_signature = self._journal.dirty_signature()
        self._base_scene_dirty = False

    def sync_dirty_state(self) -> None:
        self._sync_dirty()

    def set_on_state_changed(self, callback: Optional[Callable[[], None]]) -> None:
        self._on_state_changed = callback

    def _capture_context(self) -> Optional[EditorContextSnapshot]:
        if self._context_provider is None:
            return None
        try:
            return self._context_provider()
        except Exception as exc:
            from Infernux.debug import Debug

            Debug.log_suppressed("UndoManager.capture_context", exc)
            return None

    def _restore_context(
        self,
        context: Optional[EditorContextSnapshot],
        phase: str,
    ) -> None:
        if context is None or self._context_restorer is None:
            return
        try:
            self._context_restorer(context, phase)
        except Exception as exc:
            from Infernux.debug import Debug

            Debug.log_suppressed("UndoManager.restore_context", exc)

    def _push(
        self,
        cmd: UndoCommand,
        *,
        before_context: Optional[EditorContextSnapshot] = None,
        after_context: Optional[EditorContextSnapshot] = None,
        origin: ActionOrigin = ActionOrigin.USER,
        transaction_id: str = "",
    ) -> None:
        self._journal.max_entries = max(1, int(self.MAX_STACK_DEPTH))
        result = self._journal.record(
            cmd,
            before_context=before_context,
            after_context=after_context,
            origin=origin,
            transaction_id=transaction_id,
        )
        if not result.recorded:
            return

        # A merge mutates an already-saved operation without increasing stack
        # depth. The signature revision prevents a false clean state.
        if result.dropped and self._save_signature is not None:
            dropped_ids = {entry.operation_id for entry in result.dropped}
            if any(operation_id in dropped_ids for operation_id, _ in self._save_signature):
                self._save_signature = None

        self._sync_dirty()
        self._fire_state_changed()
        self._debug_dump_stack("push")

    def _debug_dump_stack(self, action: str) -> None:
        if os.environ.get("INFERNUX_UNDO_TRACE") != "1":
            return
        from Infernux.debug import Debug

        parts = []
        for index, entry in enumerate(self._journal.entries, 1):
            marker = "" if index <= self._journal.cursor else "(redo) "
            parts.append(f"{index}: {marker}{entry.action.description}")
        Debug.log(
            f"[UndoTrace] {action} pos={self._journal.cursor} "
            f"total={len(self._journal.entries)} " + " | ".join(parts)
        )

    def _sync_dirty(self) -> None:
        from Infernux.engine.play_mode import PlayModeManager, PlayModeState

        play_mode = PlayModeManager.instance()
        if play_mode and play_mode.state != PlayModeState.EDIT:
            return
        from Infernux.engine.scene_manager import SceneFileManager

        scene_files = SceneFileManager.instance()
        if scene_files is None:
            return
        if self._base_scene_dirty or not self.is_at_save_point:
            scene_files.mark_dirty()
        else:
            scene_files.clear_dirty()

    def _fire_state_changed(self) -> None:
        if self._on_state_changed:
            self._on_state_changed()
