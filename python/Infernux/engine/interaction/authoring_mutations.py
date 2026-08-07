"""Explicit document-authoring mutations shared by editor panels."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any, Optional

from .documents import DocumentRegistry


class AuthoringMutationService:
    """Record one already-visible authoring intent in the global journal.

    Panels submit a concrete mutation when the user commits an interaction.
    Rendering frames, background refreshes, and inferred before/after polling
    never enter this service.
    """

    _instance: Optional["AuthoringMutationService"] = None

    def __init__(self, documents: DocumentRegistry) -> None:
        self._documents = documents
        AuthoringMutationService._instance = self

    @classmethod
    def instance(cls) -> Optional["AuthoringMutationService"]:
        return cls._instance

    @classmethod
    def require(cls) -> "AuthoringMutationService":
        documents = DocumentRegistry.instance()
        if cls._instance is None or cls._instance._documents is not documents:
            cls._instance = cls(documents)
        return cls._instance

    def apply(
        self,
        document_id: str,
        description: str,
        mutation: Callable[[], Any],
        *,
        view_id: str,
        merge_key: str = "",
        before_selection: Any = None,
        after_selection: Any = None,
    ) -> bool:
        identifier = str(document_id or "").strip()
        if not identifier:
            raise ValueError("authoring mutation requires a document id")
        if not callable(mutation):
            raise TypeError("authoring mutation must be callable")
        owner_view_id = str(view_id or "").strip()
        if not owner_view_id:
            raise ValueError("authoring mutation requires an authoring view id")

        document = self._documents.require(identifier)
        controller = document.controller
        capture = getattr(controller, "capture_authoring_snapshot", None)
        restore = getattr(controller, "restore_authoring_snapshot", None)
        if not callable(capture) or not callable(restore):
            raise RuntimeError(
                f"document {identifier!r} has no authoring mutation adapter"
            )

        from Infernux.engine.undo import AuthoringDocumentSnapshotCommand, UndoManager

        manager = UndoManager.instance()
        if manager is None or not manager.enabled or manager.is_executing:
            return False

        before = copy.deepcopy(capture())
        before_revision = document.revision
        try:
            mutation()
            after = copy.deepcopy(capture())
        except Exception:
            restore(copy.deepcopy(before))
            self._documents.restore_content_revision(identifier, before_revision)
            raise

        if after == before:
            return False

        after_revision = self._documents.mark_changed(
            identifier,
            view_id=owner_view_id,
        )
        command = AuthoringDocumentSnapshotCommand(
            str(description or "Edit document"),
            identifier,
            before,
            after,
            before_revision,
            after_revision,
            merge_key=str(merge_key or ""),
        )
        if before_selection is not None:
            command.before_selection_snapshot = before_selection
        if after_selection is not None:
            command.after_selection_snapshot = after_selection
        if manager.record(command):
            if after_selection is not None:
                from .selection import SelectionService

                SelectionService.instance().apply_snapshot(
                    after_selection,
                    reason="authoring_mutation_selection",
                    record_history=False,
                )
            return True

        restore(copy.deepcopy(before))
        self._documents.restore_content_revision(identifier, before_revision)
        if before_selection is not None:
            from .selection import SelectionService

            SelectionService.instance().apply_snapshot(
                before_selection,
                reason="authoring_mutation_rollback",
                record_history=False,
            )
        return False

    @staticmethod
    def _undo_manager(*, require_edit_mode: bool):
        from Infernux.engine.undo import UndoManager

        manager = UndoManager.instance()
        if manager is None or not manager.enabled or manager.is_executing:
            return None
        if require_edit_mode:
            from Infernux.engine.play_mode import PlayModeManager, PlayModeState

            play_mode = PlayModeManager.instance()
            if play_mode is not None and play_mode.state is not PlayModeState.EDIT:
                return None
        return manager

    def can_record(self, *, require_edit_mode: bool = True) -> bool:
        """Return whether an authoring command may enter the global journal."""
        return self._undo_manager(require_edit_mode=require_edit_mode) is not None

    def execute_command(
        self,
        document_id: str,
        command_factory: Callable[[int, int], Any],
        *,
        view_id: str,
        before_selection: Any = None,
        after_selection: Any = None,
        require_edit_mode: bool = True,
    ) -> bool:
        """Create and execute one document-bound command through the global journal.

        The service owns revision reservation and rollback. Domain panels only
        describe how to construct their command for the supplied revision pair.
        """
        identifier = str(document_id or "").strip()
        owner_view_id = str(view_id or "").strip()
        if not identifier:
            raise ValueError("authoring command requires a document id")
        if not owner_view_id:
            raise ValueError("authoring command requires an authoring view id")
        if not callable(command_factory):
            raise TypeError("authoring command factory must be callable")

        manager = self._undo_manager(require_edit_mode=require_edit_mode)
        if manager is None:
            return False
        document = self._documents.require(identifier)
        before_revision = int(document.revision)
        after_revision = self._documents.reserve_changed_revision(
            identifier,
            view_id=owner_view_id,
        )
        try:
            command = command_factory(before_revision, after_revision)
        except Exception:
            self._documents.restore_content_revision(identifier, before_revision)
            raise
        if command is None:
            self._documents.restore_content_revision(identifier, before_revision)
            raise TypeError("authoring command factory returned no command")
        if before_selection is not None:
            command.before_selection_snapshot = before_selection
        if after_selection is not None:
            command.after_selection_snapshot = after_selection
        from Infernux.engine.undo._document_commands import DocumentRevisionCommand

        locator = self._documents.locate(identifier)
        if locator is None:
            command.dispose()
            self._documents.restore_content_revision(identifier, before_revision)
            raise RuntimeError("authoring command document has no stable locator")
        journal_command = DocumentRevisionCommand(
            command,
            locator,
            before_revision,
            after_revision,
        )
        applied = manager.execute(journal_command)
        if not applied:
            self._documents.restore_content_revision(identifier, before_revision)
        elif after_selection is not None:
            from .selection import SelectionService

            SelectionService.instance().apply_snapshot(
                after_selection,
                reason="authoring_command_selection",
                record_history=False,
            )
        return bool(applied)

    def record_applied_command(
        self,
        document_id: str,
        command: Any,
        *,
        view_id: str,
        before_revision: int,
        after_revision: int,
        rollback: Callable[[], Any],
        require_edit_mode: bool = True,
    ) -> bool:
        """Record a continuous edit whose model value is already visible.

        Continuous widgets mutate live while dragged. When the gesture ends,
        this method either records the prepared command or atomically restores
        both the model and document revision.
        """
        identifier = str(document_id or "").strip()
        owner_view_id = str(view_id or "").strip()
        if not identifier:
            raise ValueError("applied authoring command requires a document id")
        if not owner_view_id:
            raise ValueError("applied authoring command requires an authoring view id")
        if command is None:
            raise TypeError("applied authoring command must not be None")
        if not callable(rollback):
            raise TypeError("applied authoring command requires a rollback callback")

        document = self._documents.require(identifier)
        if int(document.revision) != int(after_revision):
            raise RuntimeError(
                "applied authoring command revision no longer matches its gesture"
            )
        manager = self._undo_manager(require_edit_mode=require_edit_mode)
        locator = self._documents.locate(identifier)
        if locator is None:
            command.dispose()
            raise RuntimeError("applied authoring command document has no stable locator")
        from Infernux.engine.undo._document_commands import DocumentRevisionCommand

        journal_command = DocumentRevisionCommand(
            command,
            locator,
            int(before_revision),
            int(after_revision),
        )
        if manager is not None and manager.record(journal_command):
            return True
        try:
            rollback()
        finally:
            self._documents.restore_content_revision(identifier, int(before_revision))
        return False

    def shutdown(self) -> None:
        if AuthoringMutationService._instance is self:
            AuthoringMutationService._instance = None


__all__ = ["AuthoringMutationService"]
