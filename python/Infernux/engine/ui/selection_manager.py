"""
Centralised selection state for the editor.

Hierarchy, Scene View, and other panels all read/write through this
single authority so multi-selection stays consistent everywhere.

Usage
-----
    from Infernux.engine.ui.selection_manager import SelectionManager

    sel = SelectionManager.instance()
    sel.select(obj_id)           # single select
    sel.toggle(obj_id)           # ctrl+click
    sel.range_select(obj_id)     # shift+click (needs ordered list)
    sel.box_select([id1, id2])   # replace with box-select result
    sel.clear()
"""
from __future__ import annotations

from typing import Callable, Optional, Sequence

from Infernux.engine.interaction import (
    SelectionChange,
    SelectionDomain,
    SelectionService,
    SelectionTarget,
)


class SelectionManager:
    """Compatibility adapter for legacy GameObject-only callers.

    The authoritative state lives in :class:`SelectionService`. New editor
    surfaces must use that typed service directly.
    """

    _instance: Optional["SelectionManager"] = None

    @classmethod
    def instance(cls) -> "SelectionManager":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        self._callbacks: list[Callable[[], None]] = []
        self._selection = SelectionService.instance()
        self._selection.add_listener(self._on_selection_changed)
        SelectionManager._instance = self

    def _on_selection_changed(self, _change: SelectionChange) -> None:
        for callback in tuple(self._callbacks):
            try:
                callback()
            except Exception as exc:
                from Infernux.debug import Debug

                Debug.log_suppressed("SelectionManager.listener", exc)

    # ── Registration ──────────────────────────────────────────────────

    def add_listener(self, cb: Callable[[], None]) -> None:
        if cb not in self._callbacks:
            self._callbacks.append(cb)

    def remove_listener(self, cb: Callable[[], None]) -> None:
        try:
            self._callbacks.remove(cb)
        except ValueError as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            pass

    # ── Ordered-ID hint (for shift-range) ─────────────────────────────

    def set_ordered_ids(self, ids: Sequence[int]) -> None:
        """Provide the visible ordering so shift-select can compute ranges."""
        self._selection.set_ordered_targets(
            "hierarchy",
            [SelectionTarget.scene_object(obj_id) for obj_id in ids if int(obj_id) > 0],
        )

    # ── Mutation ──────────────────────────────────────────────────────

    def select(
        self,
        obj_id: int,
        *,
        owner_id: str = "hierarchy",
        record_history: bool = True,
    ) -> None:
        """Replace selection with a single object."""
        obj_id = int(obj_id)
        if obj_id <= 0:
            self.clear(record_history=record_history)
            return
        self._selection.select(
            SelectionTarget.scene_object(obj_id),
            owner_id=owner_id,
            record_history=record_history,
        )

    def toggle(
        self,
        obj_id: int,
        *,
        owner_id: str = "hierarchy",
        record_history: bool = True,
    ) -> None:
        """Ctrl+click: add or remove *obj_id* from the selection."""
        obj_id = int(obj_id)
        if obj_id <= 0:
            return
        self._selection.toggle(
            SelectionTarget.scene_object(obj_id),
            owner_id=owner_id,
            record_history=record_history,
        )

    def range_select(
        self,
        obj_id: int,
        *,
        owner_id: str = "hierarchy",
        record_history: bool = True,
    ) -> None:
        """Shift+click: select contiguous range from primary → *obj_id*
        using the ordered-ID list provided by the panel."""
        obj_id = int(obj_id)
        if obj_id <= 0:
            self.clear(record_history=record_history)
            return
        self._selection.range_select(
            SelectionTarget.scene_object(obj_id),
            owner_id=owner_id,
            record_history=record_history,
        )

    def box_select(
        self,
        ids: Sequence[int],
        *,
        additive: bool = False,
        owner_id: str = "hierarchy",
        record_history: bool = True,
    ) -> None:
        """Replace (or union) selection with the result of a box/lasso drag."""
        targets = [
            SelectionTarget.scene_object(obj_id)
            for obj_id in dict.fromkeys(int(value) for value in ids)
            if obj_id > 0
        ]
        if additive:
            existing = [
                target
                for target in self._selection.snapshot.targets
                if target.domain is SelectionDomain.SCENE_OBJECT
            ]
            combined = list(dict.fromkeys(existing + targets))
            self._selection.replace(
                combined,
                owner_id=owner_id if combined else "",
                primary=combined[-1] if combined else None,
                anchor=self._selection.snapshot.anchor,
                record_history=record_history,
            )
            return
        self._selection.replace(
            targets,
            owner_id=owner_id if targets else "",
            record_history=record_history,
        )

    def clear(self, *, record_history: bool = True) -> None:
        self._selection.clear(record_history=record_history)

    def set_ids(self, ids: Sequence[int]) -> None:
        """Replace the entire selection with *ids* (last element = primary).

        Used by undo/redo to restore a previous selection state.
        """
        targets = [
            SelectionTarget.scene_object(obj_id)
            for obj_id in dict.fromkeys(int(value) for value in ids)
            if obj_id > 0
        ]
        self._selection.replace(
            targets,
            owner_id="hierarchy" if targets else "",
            record_history=False,
        )

    # ── Queries ───────────────────────────────────────────────────────

    def get_ids(self) -> list[int]:
        """Ordered list of selected IDs (last = most recently added)."""
        return [
            target.scene_object_id()
            for target in self._selection.snapshot.targets
            if target.domain is SelectionDomain.SCENE_OBJECT
        ]

    def get_primary(self) -> int:
        primary = self._selection.snapshot.primary
        return primary.scene_object_id() if primary is not None else 0

    def is_selected(self, obj_id: int) -> bool:
        return SelectionTarget.scene_object(obj_id) in self._selection.snapshot.targets

    def count(self) -> int:
        return len(self.get_ids())

    def is_empty(self) -> bool:
        return self.count() == 0

    def is_single(self) -> bool:
        return self.count() == 1

    def is_multi(self) -> bool:
        return self.count() > 1
