"""Stable-ID parameter diffs and immutable graph-authoring transactions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

from .parameters import GraphParameterCollection, GraphParameterDefinition


def _require_index(index: int | None, field_name: str) -> int:
    if type(index) is not int or index < 0:
        raise ValueError(
            f"graph parameter {field_name} must be a non-negative integer"
        )
    return index


@dataclass(frozen=True, slots=True)
class GraphParameterDiff:
    """One exact, reversible parameter mutation keyed by stable identity.

    Creation and deletion use ``None`` on the absent side. An update keeps the
    same stable ID on both sides. A move is represented by equal definitions at
    different indices, so all CRUD and ordering changes share one replay model.
    """

    before: GraphParameterDefinition | None
    after: GraphParameterDefinition | None
    before_index: int | None
    after_index: int | None

    def __post_init__(self) -> None:
        if self.before is None and self.after is None:
            raise ValueError("graph parameter diff cannot be empty")
        if self.before is not None:
            _require_index(self.before_index, "before_index")
        elif self.before_index is not None:
            raise ValueError("graph parameter create diff cannot have a before_index")
        if self.after is not None:
            _require_index(self.after_index, "after_index")
        elif self.after_index is not None:
            raise ValueError("graph parameter delete diff cannot have an after_index")
        if (
            self.before is not None
            and self.after is not None
            and self.before.stable_id != self.after.stable_id
        ):
            raise ValueError("graph parameter diff cannot change stable identity")
        if not self.changed:
            raise ValueError("graph parameter diff cannot be a no-op")

    @property
    def stable_id(self) -> str:
        parameter = self.after if self.after is not None else self.before
        assert parameter is not None
        return parameter.stable_id

    @property
    def changed(self) -> bool:
        return self.before != self.after or self.before_index != self.after_index

    def inverse(self) -> "GraphParameterDiff":
        return GraphParameterDiff(
            before=self.after,
            after=self.before,
            before_index=self.after_index,
            after_index=self.before_index,
        )

    def apply(
        self, collection: GraphParameterCollection
    ) -> GraphParameterCollection:
        """Apply only when the collection exactly matches the before-state."""
        if self.before is None:
            if collection.find(self.stable_id) is not None:
                raise ValueError(
                    f"graph parameter diff create target already exists: {self.stable_id!r}"
                )
            assert self.after is not None
            target_index = _require_index(self.after_index, "after_index")
            if target_index > len(collection.values):
                raise ValueError(
                    f"graph parameter diff after_index is out of range: {target_index}"
                )
            return collection.insert(self.after, target_index)

        current_index = collection.index_of(self.stable_id)
        expected_index = _require_index(self.before_index, "before_index")
        if (
            current_index != expected_index
            or collection.find(self.stable_id) != self.before
        ):
            raise ValueError(
                f"graph parameter diff precondition failed for {self.stable_id!r}"
            )
        if self.after is None:
            return collection.remove(self.stable_id)
        target_index = _require_index(self.after_index, "after_index")
        if target_index >= len(collection.values):
            raise ValueError(
                f"graph parameter diff after_index is out of range: {target_index}"
            )
        updated = collection.replace(self.after)
        return updated.move(self.stable_id, target_index)

    def revert(
        self, collection: GraphParameterCollection
    ) -> GraphParameterCollection:
        return self.inverse().apply(collection)


@dataclass(frozen=True, slots=True)
class GraphParameterTransaction:
    """Immutable atomic sequence of parameter CRUD and ordering changes.

    Builder methods return a new transaction. A failed step therefore leaves
    both the source collection and the previous transaction untouched. Diffs
    can be stored directly by an editor's undo command without graph snapshots.
    """

    base: GraphParameterCollection
    collection: GraphParameterCollection
    diffs: tuple[GraphParameterDiff, ...] = ()

    def __post_init__(self) -> None:
        replayed = self.base
        for diff in self.diffs:
            replayed = diff.apply(replayed)
        if replayed.values != self.collection.values:
            raise ValueError(
                "graph parameter transaction result does not match its diffs"
            )

    @classmethod
    def begin(
        cls, collection: GraphParameterCollection
    ) -> "GraphParameterTransaction":
        if not isinstance(collection, GraphParameterCollection):
            raise TypeError("graph parameter transaction requires a collection")
        return cls(collection, collection)

    @classmethod
    def _from_verified_step(
        cls,
        base: GraphParameterCollection,
        collection: GraphParameterCollection,
        diffs: tuple[GraphParameterDiff, ...],
    ) -> "GraphParameterTransaction":
        """Construct from a diff that was just applied by a builder method."""
        transaction = object.__new__(cls)
        object.__setattr__(transaction, "base", base)
        object.__setattr__(transaction, "collection", collection)
        object.__setattr__(transaction, "diffs", diffs)
        return transaction

    @property
    def changed(self) -> bool:
        return bool(self.diffs)

    def _append(
        self,
        diff: GraphParameterDiff,
        collection: GraphParameterCollection,
    ) -> "GraphParameterTransaction":
        if not diff.changed:
            return self
        return GraphParameterTransaction._from_verified_step(
            self.base,
            collection,
            (*self.diffs, diff),
        )

    def create(
        self,
        parameter: GraphParameterDefinition,
        index: int = -1,
    ) -> "GraphParameterTransaction":
        updated = self.collection.insert(parameter, index)
        target_index = updated.index_of(parameter.stable_id)
        diff = GraphParameterDiff(None, parameter, None, target_index)
        return self._append(diff, updated)

    def update(
        self, parameter: GraphParameterDefinition
    ) -> "GraphParameterTransaction":
        before = self.collection.require(parameter.stable_id)
        if before == parameter:
            return self
        index = self.collection.index_of(parameter.stable_id)
        updated = self.collection.replace(parameter)
        diff = GraphParameterDiff(before, parameter, index, index)
        return self._append(diff, updated)

    def delete(self, stable_id: str) -> "GraphParameterTransaction":
        before = self.collection.require(stable_id)
        before_index = self.collection.index_of(stable_id)
        updated = self.collection.remove(stable_id)
        diff = GraphParameterDiff(before, None, before_index, None)
        return self._append(diff, updated)

    def move(
        self, stable_id: str, target_index: int
    ) -> "GraphParameterTransaction":
        parameter = self.collection.require(stable_id)
        before_index = self.collection.index_of(stable_id)
        updated = self.collection.move(stable_id, target_index)
        after_index = updated.index_of(stable_id)
        if before_index == after_index:
            return self
        diff = GraphParameterDiff(
            parameter,
            parameter,
            before_index,
            after_index,
        )
        return self._append(diff, updated)

    def reorder(self, stable_ids: Iterable[str]) -> "GraphParameterTransaction":
        target = self.collection.reorder(stable_ids)
        transaction = self
        for target_index, parameter in enumerate(target.values):
            current_index = transaction.collection.index_of(parameter.stable_id)
            if current_index != target_index:
                transaction = transaction.move(parameter.stable_id, target_index)
        return transaction

    def apply(
        self, collection: GraphParameterCollection | None = None
    ) -> GraphParameterCollection:
        current = self.base if collection is None else collection
        for diff in self.diffs:
            current = diff.apply(current)
        return current

    def revert(
        self, collection: GraphParameterCollection | None = None
    ) -> GraphParameterCollection:
        current = self.collection if collection is None else collection
        for diff in reversed(self.diffs):
            current = diff.revert(current)
        return current


__all__ = ["GraphParameterDiff", "GraphParameterTransaction"]
