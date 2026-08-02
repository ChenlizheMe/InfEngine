"""Undo commands for editor-owned Project asset mutations."""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Optional

from Infernux.engine.path_utils import resolved_path, same_path
from Infernux.engine.undo._base import UndoCommand


class ProjectAssetRenameCommand(UndoCommand):
    """Replay a GUID-stable asset rename without creating another action."""

    marks_dirty = False

    def __init__(
        self,
        old_path: str,
        new_path: str,
        *,
        asset_database: Any = None,
        on_changed: Optional[Callable[[], None]] = None,
        move_fn: Optional[Callable[[str, str, Any], Optional[str]]] = None,
        description: str = "Rename Asset",
    ) -> None:
        super().__init__(description)
        self._old_path = resolved_path(old_path)
        self._new_path = resolved_path(new_path)
        if same_path(self._old_path, self._new_path):
            raise ValueError("asset rename command requires two different paths")
        if not same_path(
            os.path.dirname(self._old_path),
            os.path.dirname(self._new_path),
        ):
            raise ValueError("asset rename command cannot move between directories")
        self._asset_database = asset_database
        self._on_changed = on_changed
        self._move_fn = move_fn or self._rename

    @staticmethod
    def _rename(source: str, destination: str, asset_database: Any) -> Optional[str]:
        from Infernux.engine.ui import project_file_ops

        return project_file_ops.do_rename(
            source,
            os.path.basename(destination),
            asset_database,
        )

    def _apply(self, source: str, destination: str) -> None:
        if not os.path.exists(source):
            raise RuntimeError(f"asset rename source no longer exists: {source}")
        if os.path.exists(destination) and not same_path(source, destination):
            raise RuntimeError(
                f"asset rename destination is occupied by an external change: {destination}"
            )
        result = self._move_fn(source, destination, self._asset_database)
        if not result or not same_path(result, destination):
            raise RuntimeError(f"asset rename failed: {source} -> {destination}")
        if self._on_changed is not None:
            self._on_changed()

    def execute(self) -> None:
        self._apply(self._old_path, self._new_path)

    def undo(self) -> None:
        self._apply(self._new_path, self._old_path)

    def redo(self) -> None:
        self.execute()
