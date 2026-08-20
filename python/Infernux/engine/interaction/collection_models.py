"""Renderer-independent collection and tree interaction state.

The models in this module own view interaction mechanics, not authored data or
the Editor's global selection. Panels project the current typed selection into
the collection, ask it to resolve range/keyboard gestures against stable item
IDs, and then submit the resulting intent through their normal command path.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Optional


def _stable_ids(values: Iterable[object]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        stable_id = str(value or "").strip()
        if not stable_id:
            raise ValueError("collection item IDs must be non-empty")
        if stable_id in seen:
            raise ValueError(f"duplicate collection item ID: {stable_id}")
        seen.add(stable_id)
        result.append(stable_id)
    return tuple(result)


@dataclass(frozen=True, slots=True)
class CollectionSelection:
    selected_ids: tuple[str, ...] = ()
    primary_id: str = ""
    anchor_id: str = ""
    cursor_id: str = ""


@dataclass(frozen=True, slots=True)
class CollectionRenameSession:
    item_id: str
    original_value: str
    buffer: str
    focus_pending: bool = True


@dataclass(frozen=True, slots=True)
class CollectionInsertion:
    index: int
    dragged_ids: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class CollectionViewport:
    first_index: int
    last_index: int
    item_ids: tuple[str, ...]


class CollectionInteractionModel:
    """Stable-ID list interaction shared by editor collection surfaces."""

    __slots__ = (
        "_ordered_ids",
        "_index",
        "_selection",
        "_rename",
        "_insertion",
        "_revision",
    )

    def __init__(self, item_ids: Iterable[object] = ()) -> None:
        self._ordered_ids: tuple[str, ...] = ()
        self._index: dict[str, int] = {}
        self._selection = CollectionSelection()
        self._rename: Optional[CollectionRenameSession] = None
        self._insertion: Optional[CollectionInsertion] = None
        self._revision = 0
        self.set_items(item_ids)

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def ordered_ids(self) -> tuple[str, ...]:
        return self._ordered_ids

    @property
    def selection(self) -> CollectionSelection:
        return self._selection

    @property
    def rename_session(self) -> Optional[CollectionRenameSession]:
        return self._rename

    @property
    def insertion(self) -> Optional[CollectionInsertion]:
        return self._insertion

    def contains(self, item_id: object) -> bool:
        return str(item_id or "").strip() in self._index

    def set_items(self, item_ids: Iterable[object]) -> bool:
        """Replace the projection while preserving valid interaction state."""

        ordered = _stable_ids(item_ids)
        if ordered == self._ordered_ids:
            return False
        self._ordered_ids = ordered
        self._index = {stable_id: index for index, stable_id in enumerate(ordered)}

        selected = tuple(
            stable_id
            for stable_id in self._selection.selected_ids
            if stable_id in self._index
        )
        primary = self._selection.primary_id
        if primary not in selected:
            primary = selected[-1] if selected else ""
        anchor = self._selection.anchor_id
        if anchor not in self._index:
            anchor = primary
        cursor = self._selection.cursor_id
        if cursor not in self._index:
            cursor = primary
        self._selection = CollectionSelection(selected, primary, anchor, cursor)

        if self._rename is not None and self._rename.item_id not in self._index:
            self._rename = None
        if self._insertion is not None:
            self._insertion = replace(
                self._insertion,
                index=max(0, min(self._insertion.index, len(ordered))),
                dragged_ids=tuple(
                    stable_id
                    for stable_id in self._insertion.dragged_ids
                    if stable_id in self._index
                ),
            )
        self._revision += 1
        return True

    def project_selection(
        self,
        selected_ids: Iterable[object],
        *,
        primary_id: object = "",
        preserve_anchor: bool = True,
    ) -> bool:
        """Project the global typed selection without becoming its authority."""

        requested = _stable_ids(selected_ids)
        unknown = tuple(stable_id for stable_id in requested if stable_id not in self._index)
        if unknown:
            raise KeyError(f"collection selection references unknown items: {unknown}")
        requested_set = set(requested)
        selected = tuple(
            stable_id for stable_id in self._ordered_ids if stable_id in requested_set
        )
        primary = str(primary_id or "").strip()
        if primary:
            if primary not in requested_set:
                raise ValueError("collection primary must be one of the selected items")
        elif selected:
            primary = selected[-1]

        anchor = self._selection.anchor_id if preserve_anchor else ""
        if anchor not in self._index:
            anchor = primary
        cursor = primary or (
            self._selection.cursor_id
            if self._selection.cursor_id in self._index
            else ""
        )
        projected = CollectionSelection(selected, primary, anchor, cursor)
        if projected == self._selection:
            return False
        self._selection = projected
        self._revision += 1
        return True

    def clear_selection(self) -> bool:
        if self._selection == CollectionSelection():
            return False
        self._selection = CollectionSelection()
        self._revision += 1
        return True

    def activate(
        self,
        item_id: object,
        *,
        toggle: bool = False,
        extend: bool = False,
    ) -> CollectionSelection:
        """Resolve a pointer/keyboard selection gesture against current order."""

        if toggle and extend:
            raise ValueError("collection selection cannot toggle and extend together")
        target = self._require_item(item_id)
        current = self._selection
        if extend:
            anchor = current.anchor_id if current.anchor_id in self._index else (
                current.primary_id if current.primary_id in self._index else target
            )
            left, right = sorted((self._index[anchor], self._index[target]))
            selected = self._ordered_ids[left : right + 1]
            updated = CollectionSelection(selected, target, anchor, target)
        elif toggle:
            selected_set = set(current.selected_ids)
            if target in selected_set:
                selected_set.remove(target)
            else:
                selected_set.add(target)
            selected = tuple(
                stable_id for stable_id in self._ordered_ids if stable_id in selected_set
            )
            primary = target if target in selected_set else (
                selected[-1] if selected else ""
            )
            anchor = target if target in selected_set else (
                current.anchor_id if current.anchor_id in self._index else primary
            )
            updated = CollectionSelection(selected, primary, anchor, target)
        else:
            updated = CollectionSelection((target,), target, target, target)
        if updated != current:
            self._selection = updated
            self._revision += 1
        return self._selection

    def move_cursor(
        self,
        offset: int,
        *,
        extend: bool = False,
        wrap: bool = False,
    ) -> CollectionSelection:
        if not self._ordered_ids or int(offset) == 0:
            return self._selection
        current_id = self._selection.cursor_id or self._selection.primary_id
        current_index = self._index.get(current_id, 0 if offset > 0 else len(self._ordered_ids) - 1)
        target_index = current_index + int(offset)
        if wrap:
            target_index %= len(self._ordered_ids)
        else:
            target_index = max(0, min(target_index, len(self._ordered_ids) - 1))
        return self.activate(self._ordered_ids[target_index], extend=extend)

    def move_to_edge(self, *, last: bool = False, extend: bool = False) -> CollectionSelection:
        if not self._ordered_ids:
            return self._selection
        return self.activate(self._ordered_ids[-1 if last else 0], extend=extend)

    def viewport(
        self,
        first_index: int,
        visible_count: int,
        *,
        overscan: int = 0,
    ) -> CollectionViewport:
        count = len(self._ordered_ids)
        if visible_count < 0 or overscan < 0:
            raise ValueError("collection viewport counts must be non-negative")
        first = max(0, min(int(first_index) - int(overscan), count))
        last = max(
            first,
            min(int(first_index) + int(visible_count) + int(overscan), count),
        )
        return CollectionViewport(first, last, self._ordered_ids[first:last])

    def begin_rename(self, item_id: object, value: object) -> CollectionRenameSession:
        target = self._require_item(item_id)
        text = str(value or "")
        session = CollectionRenameSession(target, text, text, True)
        if session != self._rename:
            self._rename = session
            self._revision += 1
        return session

    def update_rename(self, value: object) -> bool:
        if self._rename is None:
            return False
        text = str(value or "")
        if text == self._rename.buffer:
            return False
        self._rename = replace(self._rename, buffer=text)
        self._revision += 1
        return True

    def consume_rename_focus(self) -> bool:
        if self._rename is None or not self._rename.focus_pending:
            return False
        self._rename = replace(self._rename, focus_pending=False)
        return True

    def rename_candidate(self) -> Optional[tuple[str, str]]:
        if self._rename is None:
            return None
        return self._rename.item_id, self._rename.buffer.strip()

    def finish_rename(self) -> bool:
        if self._rename is None:
            return False
        self._rename = None
        self._revision += 1
        return True

    cancel_rename = finish_rename

    def set_insertion(
        self,
        index: int,
        dragged_ids: Iterable[object] = (),
    ) -> CollectionInsertion:
        insertion = CollectionInsertion(
            max(0, min(int(index), len(self._ordered_ids))),
            tuple(self._require_item(value) for value in dragged_ids),
        )
        if insertion != self._insertion:
            self._insertion = insertion
            self._revision += 1
        return insertion

    def clear_insertion(self) -> bool:
        if self._insertion is None:
            return False
        self._insertion = None
        self._revision += 1
        return True

    def _require_item(self, item_id: object) -> str:
        stable_id = str(item_id or "").strip()
        if stable_id not in self._index:
            raise KeyError(f"unknown collection item: {stable_id}")
        return stable_id


@dataclass(frozen=True, slots=True)
class TreeProjectionRow:
    item_id: str
    parent_id: str
    depth: int
    sibling_index: int
    expanded: bool
    has_children: bool


class TreeProjectionModel:
    """Keeps foldout state separate from a tree's mutable data objects."""

    __slots__ = ("_expanded_ids", "_revision")

    def __init__(self, expanded_ids: Iterable[object] = ()) -> None:
        self._expanded_ids = set(_stable_ids(expanded_ids))
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def expanded_ids(self) -> frozenset[str]:
        return frozenset(self._expanded_ids)

    def is_expanded(self, item_id: object) -> bool:
        return str(item_id or "").strip() in self._expanded_ids

    def set_expanded(self, item_id: object, expanded: bool) -> bool:
        stable_id = str(item_id or "").strip()
        if not stable_id:
            raise ValueError("tree item ID must be non-empty")
        changed = False
        if expanded:
            if stable_id not in self._expanded_ids:
                self._expanded_ids.add(stable_id)
                changed = True
        elif stable_id in self._expanded_ids:
            self._expanded_ids.remove(stable_id)
            changed = True
        if changed:
            self._revision += 1
        return changed

    def replace_expanded(self, item_ids: Iterable[object]) -> bool:
        expanded = set(_stable_ids(item_ids))
        if expanded == self._expanded_ids:
            return False
        self._expanded_ids = expanded
        self._revision += 1
        return True

    def toggle(self, item_id: object) -> bool:
        stable_id = str(item_id or "").strip()
        self.set_expanded(stable_id, stable_id not in self._expanded_ids)
        return stable_id in self._expanded_ids

    def reconcile(self, valid_ids: Iterable[object]) -> bool:
        valid = set(_stable_ids(valid_ids))
        retained = self._expanded_ids & valid
        if retained == self._expanded_ids:
            return False
        self._expanded_ids = retained
        self._revision += 1
        return True

    def reveal_ancestors(
        self,
        item_id: object,
        parent_by_id: Mapping[str, object],
    ) -> bool:
        current = str(item_id or "").strip()
        seen: set[str] = set()
        changed = False
        while current:
            if current in seen:
                raise ValueError(f"tree parent cycle at: {current}")
            seen.add(current)
            parent = str(parent_by_id.get(current, "") or "").strip()
            if not parent:
                break
            changed = self.set_expanded(parent, True) or changed
            current = parent
        return changed

    def project(
        self,
        root_ids: Iterable[object],
        children_by_id: Mapping[str, Iterable[object]],
    ) -> tuple[TreeProjectionRow, ...]:
        roots = _stable_ids(root_ids)
        children = {
            str(parent or "").strip(): _stable_ids(values)
            for parent, values in children_by_id.items()
        }
        rows: list[TreeProjectionRow] = []
        emitted: set[str] = set()

        def visit(stable_id: str, parent_id: str, depth: int, sibling_index: int, path: set[str]) -> None:
            if stable_id in path:
                raise ValueError(f"tree contains a cycle at: {stable_id}")
            if stable_id in emitted:
                raise ValueError(f"tree item has multiple parents: {stable_id}")
            emitted.add(stable_id)
            descendants = children.get(stable_id, ())
            expanded = stable_id in self._expanded_ids
            rows.append(
                TreeProjectionRow(
                    stable_id,
                    parent_id,
                    depth,
                    sibling_index,
                    expanded,
                    bool(descendants),
                )
            )
            if not expanded:
                return
            child_path = set(path)
            child_path.add(stable_id)
            for index, child_id in enumerate(descendants):
                visit(child_id, stable_id, depth + 1, index, child_path)

        for index, root_id in enumerate(roots):
            visit(root_id, "", 0, index, set())
        return tuple(rows)


__all__ = [
    "CollectionInsertion",
    "CollectionInteractionModel",
    "CollectionRenameSession",
    "CollectionSelection",
    "CollectionViewport",
    "TreeProjectionModel",
    "TreeProjectionRow",
]
