"""Undoable presentation-state commands owned by the interaction core."""

from __future__ import annotations

import copy
from collections.abc import Callable
from typing import Any, Optional


class ViewCommandService:
    """Record user-visible view state without making a document dirty."""

    _instance: Optional["ViewCommandService"] = None

    def __init__(self) -> None:
        ViewCommandService._instance = self

    @classmethod
    def instance(cls) -> Optional["ViewCommandService"]:
        return cls._instance

    @classmethod
    def require(cls) -> "ViewCommandService":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def set_value(
        self,
        old_value: Any,
        new_value: Any,
        apply: Callable[[Any], Any],
        *,
        description: str,
        owner_view_id: str = "",
    ) -> bool:
        if not callable(apply):
            raise TypeError("view state command requires an apply callback")
        if old_value == new_value:
            return False
        from Infernux.engine.undo import LambdaCommand, UndoManager

        manager = UndoManager.instance()
        if manager is None or not manager.enabled or manager.is_executing:
            # View state must not become inert merely because history is
            # temporarily unavailable. This is especially important for
            # pointer-driven Project navigation, which previously accepted
            # the click but silently kept the old directory.
            return apply(copy.deepcopy(new_value)) is not False
        before = copy.deepcopy(old_value)
        after = copy.deepcopy(new_value)
        command = LambdaCommand(
            str(description or "Edit View State"),
            undo_fn=lambda: apply(copy.deepcopy(before)),
            redo_fn=lambda: apply(copy.deepcopy(after)),
            marks_dirty=False,
        )
        target_view_id = str(owner_view_id or "").strip()
        if not target_view_id:
            return bool(manager.execute(command))

        context = self._context_for_view(target_view_id)
        if context is None:
            command.dispose()
            raise RuntimeError(
                f"view command owner is not bound to Interaction Core: {target_view_id}"
            )
        command.preserves_explicit_context = True
        try:
            apply(copy.deepcopy(after))
        except Exception:
            command.dispose()
            raise
        if manager.record(
            command,
            before_context=context,
            after_context=context,
        ):
            return True
        apply(copy.deepcopy(before))
        return False

    @staticmethod
    def _context_for_view(view_id: str):
        from Infernux.engine.interaction.contexts import FocusSnapshot
        from Infernux.engine.interaction.session import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is None:
            return None
        type_id = core.panels.type_id_for_view(view_id) or view_id
        document = core.documents.document_for_view(view_id)
        return core.capture_context(
            focus=FocusSnapshot(
                active_panel_id=type_id,
                active_view_id=view_id,
                active_document_id=(
                    document.document_id if document is not None else ""
                ),
            )
        )

    def shutdown(self) -> None:
        if ViewCommandService._instance is self:
            ViewCommandService._instance = None


__all__ = ["ViewCommandService"]
