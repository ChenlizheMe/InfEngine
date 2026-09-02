"""Undo command execution backed by the global action journal."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import uuid
from typing import Callable, Optional, Union

from Infernux.engine.interaction import (
    ActionOrigin,
    ContextRestoreStatus,
    EditorActionJournal,
    EditorContextSnapshot,
    JournalEntry,
)
from Infernux.engine.undo._base import UndoCommand
from Infernux.engine.undo._helpers import (
    _bump_inspector_values,
    _inspector_snapshot_revision,
)


@dataclass(slots=True)
class _PendingReplay:
    direction: str
    entry: JournalEntry
    stage: str = "prepare"


class UndoManager:
    """Execute and replay editor actions through one chronological cursor."""

    MAX_STACK_DEPTH: int = 200
    _instance: Optional["UndoManager"] = None

    def __init__(self, journal: Optional[EditorActionJournal] = None) -> None:
        self._journal = journal or EditorActionJournal(self.MAX_STACK_DEPTH)
        self._is_executing: bool = False
        self._enabled: bool = True
        self._suppress_property_recording: bool = False
        self._on_state_changed: Optional[Callable[[], None]] = None
        self._context_provider: Optional[Callable[[], EditorContextSnapshot]] = None
        self._context_restorer: Optional[
            Callable[
                [EditorContextSnapshot, str],
                Union[ContextRestoreStatus, bool, None],
            ]
        ] = None
        self._pending_replay: Optional[_PendingReplay] = None
        self._user_action_depth = 0
        self._user_action_id = ""
        self._user_action_description = ""
        self._user_action_origin = ActionOrigin.USER
        self._user_action_command_id = ""
        self._user_action_before_context: Optional[EditorContextSnapshot] = None
        UndoManager._instance = self

    @classmethod
    def instance(cls) -> Optional["UndoManager"]:
        return cls._instance

    @property
    def action_journal(self) -> EditorActionJournal:
        return self._journal

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
        return self._is_executing or self._pending_replay is not None

    @property
    def is_replay_pending(self) -> bool:
        return self._pending_replay is not None

    @property
    def is_user_action_active(self) -> bool:
        return self._user_action_depth > 0

    @property
    def can_undo(self) -> bool:
        return self._pending_replay is None and self._journal.can_undo

    @property
    def can_redo(self) -> bool:
        return self._pending_replay is None and self._journal.can_redo

    @property
    def undo_description(self) -> str:
        entry = self._journal.peek_undo()
        return entry.action.description if entry is not None else ""

    @property
    def redo_description(self) -> str:
        entry = self._journal.peek_redo()
        return entry.action.description if entry is not None else ""

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = bool(value)

    def set_context_hooks(
        self,
        provider: Optional[Callable[[], EditorContextSnapshot]],
        restorer: Optional[
            Callable[
                [EditorContextSnapshot, str],
                Union[ContextRestoreStatus, bool, None],
            ]
        ],
    ) -> None:
        self._context_provider = provider
        self._context_restorer = restorer

    @contextmanager
    def user_action(
        self,
        description: str,
        *,
        origin: Optional[ActionOrigin] = None,
        command_id: str = "",
    ):
        """Aggregate one user intent into one chronological journal entry."""
        outermost = self._user_action_depth == 0
        if outermost:
            self._user_action_id = uuid.uuid4().hex
            self._user_action_description = str(description or "Editor Action")
            from Infernux.engine.interaction import current_action_origin

            self._user_action_origin = ActionOrigin(
                current_action_origin() if origin is None else origin
            )
            self._user_action_command_id = str(command_id or "").strip()
            self._user_action_before_context = self._capture_context()
        self._user_action_depth += 1
        try:
            yield self._user_action_id
        finally:
            self._user_action_depth -= 1
            if outermost:
                self._finish_user_action()

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
        origin: Optional[ActionOrigin] = None,
        transaction_id: str = "",
    ) -> bool:
        from Infernux.debug import Debug
        from Infernux.engine.interaction import current_action_origin

        origin = ActionOrigin(current_action_origin() if origin is None else origin)

        if self._pending_replay is not None:
            Debug.log_warning(
                f"Editor action '{cmd.description}' ignored while Undo/Redo is restoring context"
            )
            cmd.dispose()
            return False

        before_context = self._capture_context()
        revision_target = self._scene_revision_target(cmd, origin)
        if not self._enabled:
            if origin != ActionOrigin.SYSTEM:
                cmd.dispose()
                return False
            try:
                inspector_revision = _inspector_snapshot_revision()
                cmd.execute()
                _bump_inspector_values(inspector_revision)
            except Exception as exc:
                Debug.log_exception(exc)
                cmd.dispose()
                return False
            cmd.dispose()
            return True

        self._is_executing = True
        inspector_revision = _inspector_snapshot_revision()
        try:
            cmd.execute()
        except Exception as exc:
            Debug.log_exception(exc)
            cmd.dispose()
            return False
        finally:
            self._is_executing = False
        _bump_inspector_values(inspector_revision)

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
        try:
            journal_command = self._bind_scene_revision(cmd, revision_target)
        except Exception as exc:
            Debug.log_exception(exc)
            try:
                cmd.undo()
            except Exception as rollback_exc:
                Debug.log_exception(rollback_exc)
            cmd.dispose()
            return False
        self._push(
            journal_command,
            before_context=before_context,
            after_context=after_context,
            origin=origin,
            transaction_id=transaction_id,
            operation_id=journal_command.operation_id,
        )
        return True

    def record(
        self,
        cmd: UndoCommand,
        *,
        origin: Optional[ActionOrigin] = None,
        transaction_id: str = "",
        command_id: str = "",
        before_context: Optional[EditorContextSnapshot] = None,
        after_context: Optional[EditorContextSnapshot] = None,
    ) -> bool:
        from Infernux.engine.interaction import current_action_origin

        origin = ActionOrigin(current_action_origin() if origin is None else origin)
        if self._pending_replay is not None:
            cmd.dispose()
            return False
        if not self._enabled:
            cmd.dispose()
            return False
        if self._suppress_property_recording and cmd._is_property_edit:
            cmd.dispose()
            return False

        current_context = self._capture_context()
        revision_target = self._scene_revision_target(cmd, origin)
        before_context = before_context or current_context
        after_context = after_context or current_context
        before_selection = cmd.before_selection_snapshot
        after_selection = cmd.after_selection_snapshot
        if before_context is not None and before_selection is not None:
            before_context = before_context.with_selection(before_selection)
        if after_context is not None and after_selection is not None:
            after_context = after_context.with_selection(after_selection)
        try:
            journal_command = self._bind_scene_revision(cmd, revision_target)
        except Exception as exc:
            from Infernux.debug import Debug

            Debug.log_exception(exc)
            try:
                cmd.undo()
            except Exception as rollback_exc:
                Debug.log_exception(rollback_exc)
            cmd.dispose()
            return False
        recorded = self._push(
            journal_command,
            before_context=before_context,
            after_context=after_context,
            origin=origin,
            transaction_id=transaction_id,
            operation_id=journal_command.operation_id,
            command_id=command_id,
        )
        return recorded

    def undo(self) -> None:
        if self._pending_replay is not None:
            return
        entry = self._journal.peek_undo()
        if entry is None:
            return
        self._pending_replay = _PendingReplay("undo", entry)
        self.process_pending_replay()

    def redo(self) -> None:
        if self._pending_replay is not None:
            return
        entry = self._journal.peek_redo()
        if entry is None:
            return
        self._pending_replay = _PendingReplay("redo", entry)
        self.process_pending_replay()

    def process_pending_replay(self) -> ContextRestoreStatus:
        """Advance one Undo/Redo replay after queued editor lifecycle work."""
        from Infernux.debug import Debug

        pending = self._pending_replay
        if pending is None:
            return ContextRestoreStatus.READY

        expected = (
            self._journal.peek_undo()
            if pending.direction == "undo"
            else self._journal.peek_redo()
        )
        if expected is not pending.entry:
            Debug.log_error("Undo/Redo journal changed while context restore was pending")
            self._pending_replay = None
            return ContextRestoreStatus.FAILED

        while self._pending_replay is pending:
            is_undo = pending.direction == "undo"
            if pending.stage == "prepare":
                context = (
                    pending.entry.after_context if is_undo
                    else pending.entry.before_context
                )
                status = self._restore_context(
                    context,
                    "prepare_undo" if is_undo else "prepare_redo",
                )
                if status is ContextRestoreStatus.PENDING:
                    return status
                if status is ContextRestoreStatus.DISCARD:
                    self._journal.discard_replay_entry(pending.direction, pending.entry)
                    self._pending_replay = None
                    self._fire_state_changed()
                    next_entry = (
                        self._journal.peek_undo()
                        if pending.direction == "undo"
                        else self._journal.peek_redo()
                    )
                    if next_entry is None:
                        return ContextRestoreStatus.READY
                    self._pending_replay = _PendingReplay(pending.direction, next_entry)
                    return self.process_pending_replay()
                if status is ContextRestoreStatus.FAILED:
                    self._pending_replay = None
                    return status
                if not self._run_replay_action(pending, compensate=False):
                    self._pending_replay = None
                    return ContextRestoreStatus.FAILED
                pending.stage = "finalize"
                continue

            if pending.stage == "finalize":
                context = (
                    pending.entry.before_context if is_undo
                    else pending.entry.after_context
                )
                status = self._restore_context(
                    context,
                    "undo_complete" if is_undo else "redo_complete",
                )
                if status is ContextRestoreStatus.PENDING:
                    return status
                if status is ContextRestoreStatus.FAILED:
                    if not self._run_replay_action(pending, compensate=True):
                        self._pending_replay = None
                        Debug.log_error(
                            f"{pending.direction.title()} context restore failed and its data compensation also failed"
                        )
                        return status
                    pending.stage = "rollback"
                    continue

                if is_undo:
                    self._journal.commit_undo(pending.entry)
                else:
                    self._journal.commit_redo(pending.entry)
                self._pending_replay = None
                self._fire_state_changed()
                return ContextRestoreStatus.READY

            original_context = (
                pending.entry.after_context if is_undo
                else pending.entry.before_context
            )
            status = self._restore_context(
                original_context,
                "undo_rollback" if is_undo else "redo_rollback",
            )
            if status is ContextRestoreStatus.PENDING:
                return status
            self._pending_replay = None
            self._fire_state_changed()
            if status is ContextRestoreStatus.FAILED:
                Debug.log_error(
                    f"{pending.direction.title()} rollback restored data but could not restore the original editor context"
                )
            return ContextRestoreStatus.FAILED

        return ContextRestoreStatus.FAILED

    def _run_replay_action(self, pending: _PendingReplay, *, compensate: bool) -> bool:
        from Infernux.debug import Debug

        is_undo = pending.direction == "undo"
        callback = (
            pending.entry.action.redo if is_undo else pending.entry.action.undo
        ) if compensate else (
            pending.entry.action.undo if is_undo else pending.entry.action.redo
        )
        self._is_executing = True
        inspector_revision = _inspector_snapshot_revision()
        try:
            callback()
            _bump_inspector_values(inspector_revision)
            return True
        except Exception as exc:
            Debug.log_exception(exc)
            return False
        finally:
            self._is_executing = False

    def clear(self) -> None:
        if self._pending_replay is not None:
            raise RuntimeError("cannot clear Undo/Redo history during context restore")
        if self.is_user_action_active:
            raise RuntimeError("cannot clear Undo/Redo history during an active user action")
        self._journal.clear()
        self._fire_state_changed()

    def shutdown(self) -> None:
        """Release journal callbacks and the process-wide manager identity."""
        self._enabled = False
        self._pending_replay = None
        self._user_action_depth = 0
        self._user_action_id = ""
        self._user_action_description = ""
        self._user_action_command_id = ""
        self._user_action_before_context = None
        self._context_provider = None
        self._context_restorer = None
        self._on_state_changed = None
        self._journal.clear()
        if UndoManager._instance is self:
            UndoManager._instance = None

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
    ) -> ContextRestoreStatus:
        if context is None or self._context_restorer is None:
            return ContextRestoreStatus.READY
        try:
            result = self._context_restorer(context, phase)
            if isinstance(result, ContextRestoreStatus):
                return result
            return (
                ContextRestoreStatus.FAILED
                if result is False
                else ContextRestoreStatus.READY
            )
        except Exception as exc:
            from Infernux.debug import Debug

            Debug.log_error(
                f"Undo/Redo context restore failed during {phase}: {exc}"
            )
            return ContextRestoreStatus.FAILED

    def _push(
        self,
        cmd: UndoCommand,
        *,
        before_context: Optional[EditorContextSnapshot] = None,
        after_context: Optional[EditorContextSnapshot] = None,
        origin: ActionOrigin = ActionOrigin.USER,
        transaction_id: str = "",
        operation_id: str = "",
        command_id: str = "",
    ) -> bool:
        isolate_from_user_action = bool(getattr(cmd, "separates_history", False))
        if self.is_user_action_active and not isolate_from_user_action:
            operation_id = self._user_action_id
            transaction_id = self._user_action_id
            origin = self._user_action_origin
            command_id = self._user_action_command_id
            cmd.bind_operation_id(operation_id)
        self._journal.max_entries = max(1, int(self.MAX_STACK_DEPTH))
        result = self._journal.record(
            cmd,
            before_context=before_context,
            after_context=after_context,
            origin=origin,
            transaction_id=transaction_id,
            operation_id=operation_id,
            command_id=command_id,
        )
        if not result.recorded:
            return False

        if self.is_user_action_active and isolate_from_user_action:
            # A focus transition observed before the command body starts is a
            # separate action and becomes the context baseline for that body.
            # Without this split, finishing the user_action would silently
            # fold the panel switch into a graph/property mutation.
            if not self._journal.operation_entries(self._user_action_id):
                self._user_action_before_context = after_context or self._capture_context()

        self._fire_state_changed()
        return True

    def _finish_user_action(self) -> None:
        from Infernux.engine.undo._base import CompoundCommand
        from Infernux.engine.undo._structural_commands import GlobalContextCommand

        operation_id = self._user_action_id
        description = self._user_action_description
        origin = self._user_action_origin
        command_id = self._user_action_command_id
        before_context = self._user_action_before_context
        after_context = self._capture_context()
        self._user_action_id = ""
        self._user_action_description = ""
        self._user_action_origin = ActionOrigin.USER
        self._user_action_command_id = ""
        self._user_action_before_context = None

        entries = self._journal.operation_entries(operation_id)
        if len(entries) == 1:
            if not entries[0].action.preserves_explicit_context:
                entries[0].before_context = before_context
                entries[0].after_context = after_context
        elif len(entries) > 1:
            command = CompoundCommand(
                [entry.action for entry in entries],
                description=description,
            )
            command.bind_operation_id(operation_id)
            self._journal.replace_operation(
                operation_id,
                command,
                before_context=before_context,
                after_context=after_context,
                origin=origin,
                transaction_id=operation_id,
                command_id=command_id,
            )
        elif (
            origin is not ActionOrigin.EXTERNAL
            and before_context is not None
            and after_context is not None
            and before_context != after_context
        ):
            command = GlobalContextCommand(
                before_context,
                after_context,
                description=description,
            )
            command.bind_operation_id(operation_id)
            self.record(
                command,
                origin=origin,
                transaction_id=operation_id,
                command_id=command_id,
                before_context=before_context,
                after_context=after_context,
            )
            return

        if entries:
            self._fire_state_changed()

    @staticmethod
    def _active_scene_revision_target():
        try:
            from Infernux.engine.interaction import DocumentRegistry
            from Infernux.engine.scene_manager import SceneFileManager

            scene_files = SceneFileManager.instance()
            if scene_files is None:
                return None
            registry = DocumentRegistry.instance()
            document = registry.get(scene_files.document_id)
            locator = registry.locate(scene_files.document_id)
            if document is None or locator is None:
                return None
            return document, locator
        except (AttributeError, ImportError, RuntimeError):
            return None

    def _scene_revision_target(self, cmd: UndoCommand, origin: ActionOrigin):
        if not bool(cmd.marks_dirty) or origin is ActionOrigin.EXTERNAL:
            return None
        try:
            from Infernux.engine.play_mode import PlayModeManager, PlayModeState

            play_mode = PlayModeManager.instance()
            if play_mode is not None and play_mode.state is not PlayModeState.EDIT:
                return None
        except (AttributeError, ImportError, RuntimeError):
            pass
        target = self._active_scene_revision_target()
        if target is None:
            return None
        document, locator = target
        try:
            from Infernux.engine.interaction import FocusService

            source_view_id = FocusService.instance().snapshot.active_view_id
        except (AttributeError, ImportError, RuntimeError):
            source_view_id = ""
        return locator, int(document.revision), str(source_view_id or "")

    @staticmethod
    def _bind_scene_revision(cmd: UndoCommand, target):
        if target is None:
            return cmd
        from Infernux.engine.interaction import DocumentRegistry
        from Infernux.engine.undo._document_commands import DocumentRevisionCommand

        locator, before_revision, source_view_id = target
        registry = DocumentRegistry.instance()
        document = registry.resolve_locator(locator)
        if document is None or document.stable_id != locator.stable_id:
            raise RuntimeError("scene command changed its owning document during execution")
        if document.revision != before_revision:
            raise RuntimeError(
                "scene command changed DocumentRegistry revision outside UndoManager"
            )
        after_revision = registry.mark_changed(
            document.document_id,
            view_id=source_view_id,
        )
        return DocumentRevisionCommand(
            cmd,
            locator,
            before_revision,
            after_revision,
        )

    def _fire_state_changed(self) -> None:
        if self._on_state_changed:
            self._on_state_changed()
