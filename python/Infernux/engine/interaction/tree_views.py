"""Undoable tree presentation state owned by the interaction core."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable
from typing import Any, Optional

from .view_commands import ViewCommandService


def _unique_snapshot(values: Iterable[Hashable]) -> tuple[Hashable, ...]:
    result: list[Hashable] = []
    seen: set[Hashable] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return tuple(result)


class TreeViewStateService:
    """Submit foldout changes as non-dirty global view commands.

    Native and Python tree renderers keep their high-frequency projection
    locally. This service owns the user operation boundary: a foldout gesture
    captures the full stable-ID snapshot and routes it through the same global
    ActionJournal as other visible editor state.
    """

    _instance: Optional["TreeViewStateService"] = None

    def __init__(self, view_commands: ViewCommandService) -> None:
        if not isinstance(view_commands, ViewCommandService):
            raise TypeError("tree view state requires ViewCommandService")
        self._view_commands = view_commands
        TreeViewStateService._instance = self

    @classmethod
    def instance(cls) -> Optional["TreeViewStateService"]:
        return cls._instance

    @classmethod
    def require(cls) -> "TreeViewStateService":
        if cls._instance is None:
            cls._instance = cls(ViewCommandService.require())
        return cls._instance

    def set_expanded(
        self,
        current_ids: Iterable[Hashable],
        item_id: Hashable,
        expanded: bool,
        apply: Callable[[list[Hashable]], Any],
        *,
        description: str,
    ) -> bool:
        if not callable(apply):
            raise TypeError("tree view state command requires an apply callback")
        if item_id is None or (isinstance(item_id, str) and not item_id.strip()):
            raise ValueError("tree item ID must be non-empty")

        before = _unique_snapshot(current_ids)
        after_values = [value for value in before if value != item_id]
        if expanded:
            after_values.append(item_id)
        after = _unique_snapshot(after_values)
        return self._view_commands.set_value(
            before,
            after,
            lambda snapshot: apply(list(snapshot)),
            description=description,
        )

    def reveal_path(
        self,
        current_ids: Iterable[Hashable],
        path_ids: Iterable[Hashable],
        apply: Callable[[list[Hashable]], Any],
        *,
        description: str,
        record_history: bool = True,
    ) -> bool:
        """Expand every stable ID required to reveal one tree item."""
        if not callable(apply):
            raise TypeError("tree view state command requires an apply callback")
        before = _unique_snapshot(current_ids)
        path = _unique_snapshot(path_ids)
        if any(
            item is None or (isinstance(item, str) and not item.strip())
            for item in path
        ):
            raise ValueError("tree path IDs must be non-empty")
        after = _unique_snapshot((*before, *path))
        if before == after:
            return True
        if not record_history:
            apply(list(after))
            return True
        return self._view_commands.set_value(
            before,
            after,
            lambda snapshot: apply(list(snapshot)),
            description=description,
        )

    def shutdown(self) -> None:
        if TreeViewStateService._instance is self:
            TreeViewStateService._instance = None


__all__ = ["TreeViewStateService"]
