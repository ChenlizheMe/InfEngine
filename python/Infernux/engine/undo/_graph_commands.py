"""Precise, document-bound undo commands for graph authoring."""

from __future__ import annotations

from Infernux.engine.interaction import (
    DocumentRegistry,
    GraphActionDiff,
    GraphMutation,
    GraphMutationKind,
)
from Infernux.engine.undo._base import UndoCommand


class GraphDiffCommand(UndoCommand):
    """Apply a typed graph diff through the document's domain adapter."""

    MERGE_WINDOW = 0.3
    marks_dirty = False

    def __init__(
        self,
        description: str,
        diff: GraphActionDiff,
        *,
        merge_key: str = "",
    ) -> None:
        super().__init__(description)
        if not isinstance(diff, GraphActionDiff):
            raise TypeError("graph diff command requires a GraphActionDiff")
        self._diff = diff
        self._merge_key = str(merge_key or "")

    @property
    def diff(self) -> GraphActionDiff:
        return self._diff

    def _apply(self, diff: GraphActionDiff) -> None:
        registry = DocumentRegistry.instance()
        document = registry.require(diff.document_id)
        adapter = document.controller
        apply_diff = getattr(adapter, "apply_diff", None)
        if not callable(apply_diff):
            raise RuntimeError(
                f"graph document {diff.document_id!r} has no live domain adapter"
            )
        apply_diff(diff)
        registry.restore_content_revision(diff.document_id, diff.after_revision)
        refresh = getattr(adapter, "on_graph_diff_applied", None)
        if callable(refresh):
            refresh(diff)

    def execute(self) -> None:
        self._apply(self._diff)

    def undo(self) -> None:
        self._apply(self._diff.inverted())

    def redo(self) -> None:
        self.execute()

    def can_merge(self, other: UndoCommand) -> bool:
        left = self._diff.mutations
        right = other._diff.mutations if isinstance(other, GraphDiffCommand) else ()
        return (
            bool(self._merge_key)
            and isinstance(other, GraphDiffCommand)
            and self._diff.document_id == other._diff.document_id
            and self._merge_key == other._merge_key
            and len(left) == len(right) == 1
            and left[0].element == right[0].element
            and left[0].kind == right[0].kind
            and left[0].kind in (GraphMutationKind.UPDATE, GraphMutationKind.MOVE)
            and (other.timestamp - self.timestamp) <= self.MERGE_WINDOW
        )

    def merge(self, other: "GraphDiffCommand") -> None:
        left = self._diff.mutations[0]
        right = other._diff.mutations[0]
        merged = GraphMutation(
            left.kind,
            left.element,
            before=left.before,
            after=right.after,
            before_index=left.before_index,
            after_index=right.after_index,
        )
        self._diff = GraphActionDiff(
            self._diff.document_id,
            (merged,),
            before_revision=self._diff.before_revision,
            after_revision=other._diff.after_revision,
        )
        self.timestamp = other.timestamp
