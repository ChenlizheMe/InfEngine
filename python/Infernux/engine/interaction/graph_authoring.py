"""Shared identities, selection projection, and typed diffs for graph editors."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Iterable, Optional, Protocol, Sequence

from .descriptors import SelectionDomain, SelectionSnapshot, SelectionTarget
from .selection import SelectionChange, SelectionService


class GraphElementKind(str, Enum):
    GRAPH = "graph"
    NODE = "node"
    LINK = "link"
    PARAMETER = "parameter"
    EVENT_TYPE = "event_type"
    EVENT_FLOW = "event_flow"
    EMITTER = "emitter"
    ATTRIBUTE = "attribute"
    DATA_INTERFACE = "data_interface"


@dataclass(frozen=True, slots=True)
class GraphElementRef:
    kind: GraphElementKind
    stable_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", GraphElementKind(self.kind))
        stable_id = str(self.stable_id or "").strip()
        if not stable_id:
            raise ValueError("graph element stable_id must not be empty")
        object.__setattr__(self, "stable_id", stable_id)

    def selection_target(self, document_id: str) -> SelectionTarget:
        return SelectionTarget.graph_element(
            document_id,
            self.stable_id,
            sub_kind=self.kind.value,
        )

    @classmethod
    def from_selection_target(
        cls,
        target: SelectionTarget,
        *,
        document_id: str,
    ) -> Optional["GraphElementRef"]:
        if (
            target.domain is not SelectionDomain.GRAPH_ELEMENT
            or target.document_id != str(document_id or "")
        ):
            return None
        try:
            return cls(GraphElementKind(target.sub_kind), target.target_id)
        except (TypeError, ValueError):
            return None


class GraphMutationKind(str, Enum):
    INSERT = "insert"
    REMOVE = "remove"
    UPDATE = "update"
    MOVE = "move"


@dataclass(frozen=True, slots=True)
class GraphMutation:
    kind: GraphMutationKind
    element: GraphElementRef
    before: object = None
    after: object = None
    before_index: int = -1
    after_index: int = -1

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", GraphMutationKind(self.kind))
        if not isinstance(self.element, GraphElementRef):
            raise TypeError("graph mutation element must be a GraphElementRef")
        object.__setattr__(self, "before", copy.deepcopy(self.before))
        object.__setattr__(self, "after", copy.deepcopy(self.after))
        object.__setattr__(self, "before_index", int(self.before_index))
        object.__setattr__(self, "after_index", int(self.after_index))

    def inverted(self) -> "GraphMutation":
        inverse_kind = {
            GraphMutationKind.INSERT: GraphMutationKind.REMOVE,
            GraphMutationKind.REMOVE: GraphMutationKind.INSERT,
            GraphMutationKind.UPDATE: GraphMutationKind.UPDATE,
            GraphMutationKind.MOVE: GraphMutationKind.MOVE,
        }[self.kind]
        return GraphMutation(
            inverse_kind,
            self.element,
            before=self.after,
            after=self.before,
            before_index=self.after_index,
            after_index=self.before_index,
        )


@dataclass(frozen=True, slots=True)
class GraphActionDiff:
    document_id: str
    mutations: tuple[GraphMutation, ...]
    before_revision: int = 0
    after_revision: int = 0

    def __post_init__(self) -> None:
        document_id = str(self.document_id or "").strip()
        if not document_id:
            raise ValueError("graph diff document_id must not be empty")
        mutations = tuple(self.mutations)
        if not mutations:
            raise ValueError("graph diff must contain at least one mutation")
        if any(not isinstance(item, GraphMutation) for item in mutations):
            raise TypeError("graph diff mutations must be GraphMutation values")
        before_revision = int(self.before_revision)
        after_revision = int(self.after_revision)
        if before_revision < 0 or after_revision < 0:
            raise ValueError("graph diff revisions must be non-negative")
        object.__setattr__(self, "document_id", document_id)
        object.__setattr__(self, "mutations", mutations)
        object.__setattr__(self, "before_revision", before_revision)
        object.__setattr__(self, "after_revision", after_revision)

    def inverted(self) -> "GraphActionDiff":
        return GraphActionDiff(
            self.document_id,
            tuple(item.inverted() for item in reversed(self.mutations)),
            before_revision=self.after_revision,
            after_revision=self.before_revision,
        )


class GraphDomainAdapter(Protocol):
    def contains(self, element: GraphElementRef) -> bool: ...

    def apply_diff(self, diff: GraphActionDiff) -> None: ...


class GraphSelectionController:
    """Project one graph document's selection through the global authority."""

    def __init__(
        self,
        *,
        owner_id: str,
        document_id: Callable[[], str],
        contains: Callable[[GraphElementRef], bool],
        view=None,
        element_from_view: Optional[
            Callable[[GraphElementKind, str], GraphElementRef]
        ] = None,
        element_to_view: Optional[Callable[[GraphElementRef], str]] = None,
        on_changed: Optional[Callable[[tuple[GraphElementRef, ...]], None]] = None,
    ) -> None:
        owner_id = str(owner_id or "").strip()
        if not owner_id:
            raise ValueError("graph selection owner_id must not be empty")
        if not callable(document_id) or not callable(contains):
            raise TypeError("graph selection requires document and containment callbacks")
        self.owner_id = owner_id
        self._document_id = document_id
        self._contains = contains
        self._view = view
        self._element_from_view = element_from_view or (
            lambda kind, stable_id: GraphElementRef(kind, stable_id)
        )
        self._element_to_view = element_to_view or (lambda element: element.stable_id)
        self._on_changed = on_changed
        self._service: Optional[SelectionService] = None
        self._elements: tuple[GraphElementRef, ...] = ()

    @property
    def elements(self) -> tuple[GraphElementRef, ...]:
        return self._elements

    @property
    def primary(self) -> Optional[GraphElementRef]:
        return self._elements[-1] if self._elements else None

    def primary_id(self, kind: GraphElementKind) -> str:
        primary = self.primary
        return primary.stable_id if primary is not None and primary.kind is kind else ""

    def selected_ids(self, kind: GraphElementKind) -> tuple[str, ...]:
        kind = GraphElementKind(kind)
        return tuple(item.stable_id for item in self._elements if item.kind is kind)

    def bind(self, service: Optional[SelectionService] = None) -> None:
        target = service or SelectionService.instance()
        if self._service is target:
            self.project(target.snapshot)
            return
        self.unbind()
        self._service = target
        target.add_listener(self._on_selection_changed)
        self.project(target.snapshot)

    def unbind(self) -> None:
        if self._service is not None:
            self._service.remove_listener(self._on_selection_changed)
        self._service = None

    def set_view(self, view) -> None:
        self._view = view
        self._project_view()

    def refresh(self) -> None:
        service = self._service or SelectionService.instance()
        self.project(service.snapshot)

    def select(
        self,
        elements: Iterable[GraphElementRef],
        *,
        reason: str = "graph_selection",
        record_history: bool = True,
    ) -> bool:
        items = tuple(dict.fromkeys(elements))
        if not items:
            return self.clear(reason=reason, record_history=record_history)
        if any(not isinstance(item, GraphElementRef) for item in items):
            raise TypeError("graph selection values must be GraphElementRef values")
        kinds = {item.kind for item in items}
        if len(kinds) > 1 or (len(items) > 1 and GraphElementKind.NODE not in kinds):
            raise ValueError("graph selection may only multi-select nodes of one document")
        if any(not self._contains(item) for item in items):
            raise ValueError("graph selection contains an element outside the document")
        document_id = str(self._document_id() or "").strip()
        if not document_id:
            raise RuntimeError("graph selection document is not bound")
        service = self._service or SelectionService.instance()
        targets = tuple(item.selection_target(document_id) for item in items)
        changed = service.replace(
            targets,
            owner_id=self.owner_id,
            primary=targets[-1],
            anchor=targets[0],
            reason=reason,
            record_history=record_history,
        )
        if self._service is not service:
            self.project(service.snapshot)
        return changed

    def select_one(
        self,
        kind: GraphElementKind,
        stable_id: str,
        *,
        reason: str = "graph_selection",
        record_history: bool = True,
    ) -> bool:
        return self.select(
            (GraphElementRef(kind, stable_id),),
            reason=reason,
            record_history=record_history,
        )

    def clear(
        self,
        *,
        reason: str = "graph_clear_selection",
        record_history: bool = True,
    ) -> bool:
        service = self._service or SelectionService.instance()
        snapshot = service.snapshot
        document_id = str(self._document_id() or "").strip()
        owns_selection = snapshot.owner_id == self.owner_id or any(
            target.domain is SelectionDomain.GRAPH_ELEMENT
            and target.document_id == document_id
            for target in snapshot.targets
        )
        if not owns_selection:
            self._set_elements(())
            return False
        changed = service.clear(reason=reason, record_history=record_history)
        if self._service is not service:
            self.project(service.snapshot)
        return changed

    def accept_view_selection(
        self,
        node_ids: Sequence[str],
        link_id: str,
        *,
        record_history: bool,
    ) -> bool:
        if link_id:
            elements = (
                self._element_from_view(GraphElementKind.LINK, link_id),
            )
        else:
            elements = tuple(
                self._element_from_view(GraphElementKind.NODE, node_id)
                for node_id in node_ids
                if node_id
            )
        return self.select(
            elements,
            reason="graph_canvas_selection",
            record_history=record_history,
        )

    def project(self, snapshot: SelectionSnapshot) -> None:
        document_id = str(self._document_id() or "").strip()
        elements: list[GraphElementRef] = []
        stale = False
        for target in snapshot.targets:
            element = GraphElementRef.from_selection_target(
                target,
                document_id=document_id,
            )
            if element is None:
                continue
            if not self._contains(element):
                stale = True
                continue
            elements.append(element)
        if stale and self._service is not None and self._service.snapshot == snapshot:
            self._service.clear(reason="graph_drop_stale_selection", record_history=False)
            return
        self._set_elements(tuple(elements))

    def _on_selection_changed(self, change: SelectionChange) -> None:
        self.project(change.after)

    def _set_elements(self, elements: tuple[GraphElementRef, ...]) -> None:
        self._elements = elements
        self._project_view()
        if self._on_changed is not None:
            self._on_changed(elements)

    def _project_view(self) -> None:
        view = self._view
        if view is None:
            return
        nodes = tuple(
            view_id
            for element in self._elements
            if element.kind is GraphElementKind.NODE
            and (view_id := self._element_to_view(element))
        )
        primary = self.primary
        link = (
            self._element_to_view(primary)
            if primary is not None and primary.kind is GraphElementKind.LINK
            else ""
        )
        setter = getattr(view, "set_selection", None)
        if callable(setter):
            setter(nodes, link, notify=False)
            return
        view.selected_nodes = list(nodes)
        view.selected_link = link
