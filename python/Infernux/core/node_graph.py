"""
NodeGraph — Generic visual node-graph data model.

Reusable foundation for any node-based editor (state machines,
shader graphs, dialogue trees, etc.).  The *view* layer lives in
:mod:`Infernux.engine.ui.node_graph_view`.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Protocol, Sequence, runtime_checkable
import json
import uuid


# ═══════════════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════════════

class PinKind(IntEnum):
    INPUT = 0
    OUTPUT = 1


class PinCategory(str, Enum):
    DATA = "data"
    EXEC = "exec"


class GraphDiagnosticSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class GraphDiagnostic:
    code: str
    message: str
    severity: GraphDiagnosticSeverity = GraphDiagnosticSeverity.ERROR
    node_uid: str = ""
    link_uid: str = ""


@dataclass(frozen=True)
class LinkValidationResult:
    is_valid: bool
    code: str = ""
    message: str = ""

    def __bool__(self) -> bool:
        return self.is_valid


class GraphCycleError(ValueError):
    """Raised when a requested topological order contains a cycle."""


@runtime_checkable
class GraphCompiler(Protocol):
    """Domain compiler contract implemented by VFX and future graph domains."""

    def validate(self, graph: "NodeGraph") -> Sequence[GraphDiagnostic]: ...

    def compile(self, graph: "NodeGraph") -> Any: ...


# ═══════════════════════════════════════════════════════════════════════════
# Definitions (registered once per node type)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class PinDef:
    """Definition of a single pin on a node type."""

    id: str
    label: str
    kind: PinKind
    color: tuple = (0.80, 0.80, 0.80, 1.0)
    max_connections: int = -1  # -1 = unlimited
    data_type: str = "any"
    pin_category: PinCategory = PinCategory.DATA

    def __post_init__(self) -> None:
        self.kind = PinKind(self.kind)
        self.pin_category = PinCategory(self.pin_category)
        self.data_type = str(self.data_type).strip().lower() or "any"
        if self.max_connections < -1:
            raise ValueError("max_connections must be -1 or non-negative")


@dataclass(frozen=True)
class NodeInlineFieldDef:
    """Editable literal displayed inside a node when no wire supplies it."""

    id: str
    label: str
    data_type: str
    default: Any = None
    asset_type: str = ""
    enum_values: tuple[str, ...] = ()
    enum_labels: tuple[str, ...] = ()
    visible_when_field: str = ""
    visible_when_value: Any = None

    def __post_init__(self) -> None:
        if self.enum_labels and len(self.enum_labels) != len(self.enum_values):
            raise ValueError("inline enum labels must match enum values")


@dataclass
class NodeTypeDef:
    """Registered blueprint for a category of nodes."""

    type_id: str
    label: str
    header_color: tuple = (0.30, 0.30, 0.30, 1.0)
    pins: List[PinDef] = field(default_factory=list)
    min_width: float = 140.0
    deletable: bool = True
    body_bottom_pad: float = 0.0  # extra height below pins for custom body UI (px at zoom=1)
    visual_style: str = "graph"
    category_label: str = ""
    show_header_color_swatch: bool = True
    inline_fields: List[NodeInlineFieldDef] = field(default_factory=list)

    def __post_init__(self) -> None:
        pin_ids = [pin.id for pin in self.pins]
        if len(pin_ids) != len(set(pin_ids)):
            raise ValueError(f"node type {self.type_id!r} contains duplicate pin ids")

    def input_pins(self) -> List[PinDef]:
        return [p for p in self.pins if p.kind == PinKind.INPUT]

    def output_pins(self) -> List[PinDef]:
        return [p for p in self.pins if p.kind == PinKind.OUTPUT]


# ═══════════════════════════════════════════════════════════════════════════
# Instances (per-graph objects)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class GraphNode:
    """A concrete node placed on the canvas."""

    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    type_id: str = ""
    pos_x: float = 0.0
    pos_y: float = 0.0
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "type_id": self.type_id,
            "pos_x": self.pos_x,
            "pos_y": self.pos_y,
            "data": dict(self.data),
        }

    @staticmethod
    def from_dict(d: dict) -> GraphNode:
        return GraphNode(
            uid=d["uid"],
            type_id=d["type_id"],
            pos_x=d.get("pos_x", 0.0),
            pos_y=d.get("pos_y", 0.0),
            data=d.get("data", {}),
        )


@dataclass
class GraphLink:
    """A directed connection between two pins on two nodes."""

    uid: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    source_node: str = ""   # node uid
    source_pin: str = ""    # pin id
    target_node: str = ""   # node uid
    target_pin: str = ""    # pin id
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "uid": self.uid,
            "source_node": self.source_node,
            "source_pin": self.source_pin,
            "target_node": self.target_node,
            "target_pin": self.target_pin,
            "data": dict(self.data),
        }

    @staticmethod
    def from_dict(d: dict) -> GraphLink:
        return GraphLink(
            uid=d["uid"],
            source_node=d["source_node"],
            source_pin=d["source_pin"],
            target_node=d["target_node"],
            target_pin=d["target_pin"],
            data=d.get("data", {}),
        )


class NodeGraphElementKind(str, Enum):
    """Structural element kinds owned by every node graph."""

    NODE = "node"
    LINK = "link"


class NodeGraphMutationKind(str, Enum):
    """Reversible authoring operations supported by :class:`NodeGraph`."""

    INSERT = "insert"
    REMOVE = "remove"
    UPDATE = "update"
    MOVE = "move"


@dataclass(frozen=True, slots=True)
class NodeGraphElementState:
    payload: dict
    index: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", copy.deepcopy(dict(self.payload)))
        object.__setattr__(self, "index", int(self.index))


@dataclass(frozen=True, slots=True)
class NodeGraphAuthoringState:
    """Identity-indexed structural state used to build precise graph diffs."""

    nodes: dict[str, NodeGraphElementState]
    links: dict[str, NodeGraphElementState]

    def __post_init__(self) -> None:
        object.__setattr__(self, "nodes", dict(self.nodes))
        object.__setattr__(self, "links", dict(self.links))


@dataclass(frozen=True, slots=True)
class NodeGraphClipboardState:
    """Portable structural subset captured from one :class:`NodeGraph`.

    The payload contains only selected nodes and links whose two endpoints are
    both selected.  Domain-owned data stays inside the normal authoring
    payload, so FSM, Particle, and future Shader Graph adapters can remap their
    own stable references without implementing another graph clipboard.
    """

    graph_kind: str
    state: NodeGraphAuthoringState

    def __post_init__(self) -> None:
        graph_kind = str(self.graph_kind or "").strip()
        if not graph_kind:
            raise ValueError("node graph clipboard graph_kind must not be empty")
        if not isinstance(self.state, NodeGraphAuthoringState):
            raise TypeError("node graph clipboard state must be authoring state")
        if not self.state.nodes:
            raise ValueError("node graph clipboard must contain at least one node")
        object.__setattr__(self, "graph_kind", graph_kind)


@dataclass(frozen=True, slots=True)
class NodeGraphPasteResult:
    """Result of one atomic subgraph paste."""

    node_ids: tuple[str, ...]
    link_ids: tuple[str, ...]
    node_id_map: dict[str, str]
    link_id_map: dict[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "node_ids", tuple(self.node_ids))
        object.__setattr__(self, "link_ids", tuple(self.link_ids))
        object.__setattr__(self, "node_id_map", dict(self.node_id_map))
        object.__setattr__(self, "link_id_map", dict(self.link_id_map))


@dataclass(frozen=True, slots=True)
class NodeGraphMutation:
    """A reversible node/link edit independent of any editor domain."""

    kind: NodeGraphMutationKind
    element_kind: NodeGraphElementKind
    stable_id: str
    before: object = None
    after: object = None
    before_index: int = -1
    after_index: int = -1

    def __post_init__(self) -> None:
        object.__setattr__(self, "kind", NodeGraphMutationKind(self.kind))
        object.__setattr__(
            self, "element_kind", NodeGraphElementKind(self.element_kind)
        )
        stable_id = str(self.stable_id or "").strip()
        if not stable_id:
            raise ValueError("node graph mutation stable_id must not be empty")
        object.__setattr__(self, "stable_id", stable_id)
        object.__setattr__(self, "before", copy.deepcopy(self.before))
        object.__setattr__(self, "after", copy.deepcopy(self.after))
        object.__setattr__(self, "before_index", int(self.before_index))
        object.__setattr__(self, "after_index", int(self.after_index))

    def inverted(self) -> "NodeGraphMutation":
        inverse_kind = {
            NodeGraphMutationKind.INSERT: NodeGraphMutationKind.REMOVE,
            NodeGraphMutationKind.REMOVE: NodeGraphMutationKind.INSERT,
            NodeGraphMutationKind.UPDATE: NodeGraphMutationKind.UPDATE,
            NodeGraphMutationKind.MOVE: NodeGraphMutationKind.MOVE,
        }[self.kind]
        return NodeGraphMutation(
            inverse_kind,
            self.element_kind,
            self.stable_id,
            before=self.after,
            after=self.before,
            before_index=self.after_index,
            after_index=self.before_index,
        )


@dataclass(frozen=True, slots=True)
class NodeGraphRebuildResult:
    """Atomic result of rebuilding one node against a dynamic definition."""

    node_uid: str
    before: NodeGraphAuthoringState
    after: NodeGraphAuthoringState
    mutations: tuple[NodeGraphMutation, ...]
    removed_link_ids: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.mutations)


@dataclass(frozen=True, slots=True)
class NodeGraphBatchRebuildResult:
    """Atomic result of rebuilding a dependency set of dynamic nodes."""

    node_uids: tuple[str, ...]
    before: NodeGraphAuthoringState
    after: NodeGraphAuthoringState
    mutations: tuple[NodeGraphMutation, ...]
    removed_link_ids: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        return bool(self.mutations)


# ═══════════════════════════════════════════════════════════════════════════
# Type catalog
# ═══════════════════════════════════════════════════════════════════════════

class NodeCatalog:
    """Node type definitions partitioned by authoring domain."""

    def __init__(self) -> None:
        self._types_by_kind: Dict[str, Dict[str, NodeTypeDef]] = {}

    def register(self, graph_kind: str, types: Iterable[NodeTypeDef]) -> None:
        graph_kind = graph_kind.strip()
        if not graph_kind:
            raise ValueError("graph_kind must not be empty")
        domain_types = self._types_by_kind.setdefault(graph_kind, {})
        for typedef in types:
            existing = domain_types.get(typedef.type_id)
            if existing is not None and existing != typedef:
                raise ValueError(
                    f"node type {typedef.type_id!r} is already registered for {graph_kind!r}"
                )
            domain_types[typedef.type_id] = typedef

    def graph_kinds(self) -> List[str]:
        return list(self._types_by_kind)

    def registered_types(self, graph_kind: str) -> List[NodeTypeDef]:
        return list(self._types_by_kind.get(graph_kind, {}).values())

    def get_type(self, graph_kind: str, type_id: str) -> Optional[NodeTypeDef]:
        return self._types_by_kind.get(graph_kind, {}).get(type_id)

    def create_graph(self, graph_kind: str) -> "NodeGraph":
        return NodeGraph(graph_kind=graph_kind, catalog=self)


node_catalog = NodeCatalog()


# ═══════════════════════════════════════════════════════════════════════════
# Graph
# ═══════════════════════════════════════════════════════════════════════════

class NodeGraph:
    """Generic node-graph container with CRUD, serialisation, and precise edits."""

    _DEFAULT_TYPE_COMPATIBILITY = {
        ("int", "float"),
        ("vec3", "color"),
        ("color", "vec3"),
        ("vec4", "color"),
        ("color", "vec4"),
    }

    def __init__(
        self,
        graph_kind: str = "",
        catalog: Optional[NodeCatalog] = None,
    ) -> None:
        self.nodes: List[GraphNode] = []
        self.links: List[GraphLink] = []
        self._type_registry: Dict[str, NodeTypeDef] = {}
        self._node_definition_resolver: Optional[
            Callable[[GraphNode], Optional[NodeTypeDef]]
        ] = None
        self._node_definition_invalidator: Optional[Callable[[GraphNode], None]] = None
        self.graph_kind = graph_kind
        self._type_compatibility = set(self._DEFAULT_TYPE_COMPATIBILITY)
        if graph_kind:
            source_catalog = catalog or node_catalog
            for typedef in source_catalog.registered_types(graph_kind):
                self.register_type(typedef)

    # ── Type registry ─────────────────────────────────────────────────

    def register_type(self, typedef: NodeTypeDef) -> None:
        self._type_registry[typedef.type_id] = typedef

    def get_type(self, type_id: str) -> Optional[NodeTypeDef]:
        return self._type_registry.get(type_id)

    def set_node_definition_resolver(
        self,
        resolver: Optional[Callable[[GraphNode], Optional[NodeTypeDef]]],
        *,
        invalidator: Optional[Callable[[GraphNode], None]] = None,
    ) -> None:
        """Configure instance-dependent node definitions for this graph.

        Dynamic definitions are a graph capability, not a domain-owned rebuild
        protocol. A graph domain may supply the effective definition and an
        optional cache invalidator; :meth:`rebuild_node` still owns migration,
        link validation, rollback, and the reversible structural diff.
        """
        self._node_definition_resolver = resolver
        self._node_definition_invalidator = invalidator

    def get_node_type(self, node: GraphNode) -> Optional[NodeTypeDef]:
        """Resolve a node instance's effective type definition."""
        if self._node_definition_resolver is not None:
            return self._node_definition_resolver(node)
        return self.get_type(node.type_id)

    def invalidate_node_definition(self, node: GraphNode) -> None:
        """Invalidate resolver state before a node definition is re-resolved."""
        if self._node_definition_invalidator is not None:
            self._node_definition_invalidator(node)

    def registered_types(self) -> List[NodeTypeDef]:
        return list(self._type_registry.values())

    def register_type_compatibility(self, source_type: str, target_type: str) -> None:
        self._type_compatibility.add((source_type.lower(), target_type.lower()))

    # ── Node CRUD ─────────────────────────────────────────────────────

    def add_node(
        self,
        type_id: str,
        canvas_x: float = 0.0,
        canvas_y: float = 0.0,
        uid: Optional[str] = None,
        **data: Any,
    ) -> GraphNode:
        node_uid = str(uid) if uid else uuid.uuid4().hex[:8]
        while uid is None and self.find_node(node_uid) is not None:
            node_uid = uuid.uuid4().hex[:8]
        if self.find_node(node_uid) is not None:
            raise ValueError(f"node uid already exists: {node_uid!r}")
        node = GraphNode(
            uid=node_uid,
            type_id=type_id,
            pos_x=canvas_x,
            pos_y=canvas_y,
            data=data,
        )
        self.nodes.append(node)
        return node

    def remove_node(self, uid: str) -> bool:
        before = len(self.nodes)
        self.nodes = [n for n in self.nodes if n.uid != uid]
        self.links = [
            lk for lk in self.links
            if lk.source_node != uid and lk.target_node != uid
        ]
        return len(self.nodes) < before

    def find_node(self, uid: str) -> Optional[GraphNode]:
        for n in self.nodes:
            if n.uid == uid:
                return n
        return None

    # ── Link CRUD ─────────────────────────────────────────────────────

    def add_link(
        self,
        src_node: str,
        src_pin: str,
        dst_node: str,
        dst_pin: str,
        uid: Optional[str] = None,
        **data: Any,
    ) -> Optional[GraphLink]:
        link_uid = str(uid) if uid else uuid.uuid4().hex[:8]
        while uid is None and self.find_link(link_uid) is not None:
            link_uid = uuid.uuid4().hex[:8]
        if self.find_link(link_uid) is not None:
            raise ValueError(f"link uid already exists: {link_uid!r}")
        if not self.validate_link(src_node, src_pin, dst_node, dst_pin):
            return None
        link = GraphLink(
            uid=link_uid,
            source_node=src_node,
            source_pin=src_pin,
            target_node=dst_node,
            target_pin=dst_pin,
            data=data,
        )
        self.links.append(link)
        return link

    def replace_link(
        self,
        link_uid: str,
        src_node: str,
        src_pin: str,
        dst_node: str,
        dst_pin: str,
    ) -> Optional[GraphLink]:
        link = self.find_link(link_uid)
        if link is None or not self.validate_link(
            src_node,
            src_pin,
            dst_node,
            dst_pin,
            ignore_link_uid=link_uid,
        ):
            return None
        link.source_node = src_node
        link.source_pin = src_pin
        link.target_node = dst_node
        link.target_pin = dst_pin
        return link

    def validate_link(
        self,
        src_node: str,
        src_pin: str,
        dst_node: str,
        dst_pin: str,
        *,
        ignore_link_uid: str = "",
    ) -> LinkValidationResult:
        if src_node == dst_node:
            return LinkValidationResult(False, "self_loop", "A node cannot link to itself")

        source = self.find_node(src_node)
        target = self.find_node(dst_node)
        if source is None or target is None:
            return LinkValidationResult(False, "missing_node", "Link endpoint node does not exist")

        source_pin = self._find_pin_def(source, src_pin)
        target_pin = self._find_pin_def(target, dst_pin)
        if source_pin is None or target_pin is None:
            return LinkValidationResult(False, "missing_pin", "Link endpoint pin does not exist")
        if source_pin.kind != PinKind.OUTPUT or target_pin.kind != PinKind.INPUT:
            return LinkValidationResult(False, "invalid_direction", "Links must connect output to input")
        if source_pin.pin_category != target_pin.pin_category:
            return LinkValidationResult(
                False, "category_mismatch", "Exec and data pins cannot be connected"
            )
        if (
            source_pin.pin_category == PinCategory.DATA
            and not self._are_data_types_compatible(source_pin.data_type, target_pin.data_type)
        ):
            return LinkValidationResult(
                False,
                "type_mismatch",
                f"Cannot connect {source_pin.data_type} to {target_pin.data_type}",
            )

        for link in self.links:
            if link.uid == ignore_link_uid:
                continue
            if (
                link.source_node == src_node
                and link.source_pin == src_pin
                and link.target_node == dst_node
                and link.target_pin == dst_pin
            ):
                return LinkValidationResult(False, "duplicate", "Link already exists")

        if self._pin_connection_count(src_node, src_pin, PinKind.OUTPUT, ignore_link_uid) >= source_pin.max_connections >= 0:
            return LinkValidationResult(False, "source_full", "Source pin connection limit reached")
        if self._pin_connection_count(dst_node, dst_pin, PinKind.INPUT, ignore_link_uid) >= target_pin.max_connections >= 0:
            return LinkValidationResult(False, "target_full", "Target pin connection limit reached")
        return LinkValidationResult(True)

    def _find_pin_def(self, node: GraphNode, pin_id: str) -> Optional[PinDef]:
        typedef = self.get_node_type(node)
        if typedef is None:
            return None
        return next((pin for pin in typedef.pins if pin.id == pin_id), None)

    def _pin_connection_count(
        self,
        node_uid: str,
        pin_id: str,
        kind: PinKind,
        ignore_link_uid: str = "",
    ) -> int:
        if kind == PinKind.OUTPUT:
            return sum(
                link.uid != ignore_link_uid
                and link.source_node == node_uid
                and link.source_pin == pin_id
                for link in self.links
            )
        return sum(
            link.uid != ignore_link_uid
            and link.target_node == node_uid
            and link.target_pin == pin_id
            for link in self.links
        )

    def _are_data_types_compatible(self, source_type: str, target_type: str) -> bool:
        return (
            source_type == target_type
            or source_type == "any"
            or target_type == "any"
            or (source_type, target_type) in self._type_compatibility
        )

    def remove_link(self, uid: str) -> bool:
        before = len(self.links)
        self.links = [lk for lk in self.links if lk.uid != uid]
        return len(self.links) < before

    def remove_invalid_links_for_node(self, node_uid: str) -> tuple[str, ...]:
        """Remove links rejected by the node's current effective definition."""
        if self.find_node(node_uid) is None:
            return ()
        original = list(self.links)
        adjacent = [
            link
            for link in original
            if node_uid in {link.source_node, link.target_node}
        ]
        self.links = [link for link in original if link not in adjacent]
        removed: list[str] = []
        kept_ids = {link.uid for link in self.links}
        try:
            # Validate against the links already retained. This makes a port
            # capacity reduction deterministic instead of having every old
            # connection reject every other old connection.
            for link in adjacent:
                if self.validate_link(
                    link.source_node,
                    link.source_pin,
                    link.target_node,
                    link.target_pin,
                ):
                    self.links.append(link)
                    kept_ids.add(link.uid)
                else:
                    removed.append(link.uid)
            self.links = [link for link in original if link.uid in kept_ids]
        except Exception:
            self.links = original
            raise
        return tuple(removed)

    def rebuild_node(
        self,
        node_uid: str,
        data_updates: Optional[dict[str, Any]] = None,
        *,
        preserve_fields: Iterable[str] = (),
    ) -> NodeGraphRebuildResult:
        """Re-resolve one node schema and migrate its editable properties.

        A dynamic definition resolver may be registered once on the graph. The
        core keeps stable identity, initializes newly introduced inline fields,
        removes retired fields, validates adjacent links, rolls back failures,
        and returns one reversible structural diff.
        """
        node_uid = str(node_uid)
        batch = self.rebuild_nodes(
            {node_uid: dict(data_updates or {})},
            preserve_fields={node_uid: tuple(preserve_fields)},
        )
        return NodeGraphRebuildResult(
            node_uid,
            batch.before,
            batch.after,
            batch.mutations,
            batch.removed_link_ids,
        )

    def rebuild_nodes(
        self,
        data_updates: Mapping[str, Mapping[str, Any]],
        *,
        affected_node_uids: Iterable[str] = (),
        preserve_fields: Optional[Mapping[str, Iterable[str]]] = None,
    ) -> NodeGraphBatchRebuildResult:
        """Atomically rebuild dynamic definitions for a dependency set.

        Domains identify which stable nodes depend on a changed schema and
        provide only their data updates. The graph owns field migration,
        definition invalidation, deterministic link pruning, rollback, and the
        combined reversible diff.
        """
        updates = {
            str(node_uid): copy.deepcopy(dict(values))
            for node_uid, values in dict(data_updates).items()
        }
        requested = set(updates)
        requested.update(str(node_uid) for node_uid in affected_node_uids)
        node_uids = tuple(node.uid for node in self.nodes if node.uid in requested)
        missing = requested - set(node_uids)
        if missing:
            raise KeyError(f"nodes not found: {sorted(missing)!r}")
        if not node_uids:
            raise ValueError("dynamic node rebuild requires at least one node")
        preserved = {
            str(node_uid): set(fields)
            for node_uid, fields in dict(preserve_fields or {}).items()
        }
        before = self.capture_authoring_state()
        old_definitions = {
            node_uid: self.get_node_type(self.find_node(node_uid))
            for node_uid in node_uids
        }
        try:
            for node_uid, values in updates.items():
                self.find_node(node_uid).data.update(values)
            for node_uid in node_uids:
                self.invalidate_node_definition(self.find_node(node_uid))

            for node_uid in node_uids:
                node = self.find_node(node_uid)
                old_definition = old_definitions[node_uid]
                new_definition = self.get_node_type(node)
                if new_definition is None:
                    raise RuntimeError(
                        f"node {node_uid!r} has no effective definition after rebuild"
                    )
                old_inline = (
                    {field.id: field for field in old_definition.inline_fields}
                    if old_definition is not None
                    else {}
                )
                new_inline = {
                    field.id: field for field in new_definition.inline_fields
                }
                keep = preserved.get(node_uid, set()) | set(updates.get(node_uid, {}))
                for field_id in old_inline.keys() - new_inline.keys() - keep:
                    node.data.pop(field_id, None)
                for field_id, field_definition in new_inline.items():
                    previous_field = old_inline.get(field_id)
                    type_changed = (
                        previous_field is not None
                        and previous_field.data_type != field_definition.data_type
                    )
                    if field_id not in node.data or (
                        type_changed and field_id not in keep
                    ):
                        node.data[field_id] = copy.deepcopy(field_definition.default)

            removed_links: list[str] = []
            for node_uid in node_uids:
                for link_uid in self.remove_invalid_links_for_node(node_uid):
                    if link_uid not in removed_links:
                        removed_links.append(link_uid)
            after = self.capture_authoring_state()
        except Exception:
            self.restore_authoring_state(before)
            raise
        return NodeGraphBatchRebuildResult(
            node_uids,
            before,
            after,
            self.diff_authoring_states(before, after),
            tuple(removed_links),
        )

    def find_link(self, uid: str) -> Optional[GraphLink]:
        for lk in self.links:
            if lk.uid == uid:
                return lk
        return None

    def get_links_for_node(self, node_uid: str) -> List[GraphLink]:
        return [
            lk for lk in self.links
            if lk.source_node == node_uid or lk.target_node == node_uid
        ]

    # ── Validation and topology ──────────────────────────────────────

    def validate(self) -> List[GraphDiagnostic]:
        diagnostics: List[GraphDiagnostic] = []
        seen_nodes = set()
        for node in self.nodes:
            if node.uid in seen_nodes:
                diagnostics.append(GraphDiagnostic(
                    "duplicate_node_uid", f"Duplicate node uid {node.uid!r}", node_uid=node.uid
                ))
            seen_nodes.add(node.uid)
            if self.get_type(node.type_id) is None:
                diagnostics.append(GraphDiagnostic(
                    "unknown_node_type",
                    f"Unknown node type {node.type_id!r}",
                    node_uid=node.uid,
                ))

        seen_links = set()
        for link in self.links:
            if link.uid in seen_links:
                diagnostics.append(GraphDiagnostic(
                    "duplicate_link_uid", f"Duplicate link uid {link.uid!r}", link_uid=link.uid
                ))
            seen_links.add(link.uid)
            result = self.validate_link(
                link.source_node,
                link.source_pin,
                link.target_node,
                link.target_pin,
                ignore_link_uid=link.uid,
            )
            if not result:
                diagnostics.append(GraphDiagnostic(
                    result.code, result.message, link_uid=link.uid
                ))
        return diagnostics

    def links_for_category(self, pin_category: PinCategory | str) -> List[GraphLink]:
        category = PinCategory(pin_category)
        result: List[GraphLink] = []
        for link in self.links:
            source = self.find_node(link.source_node)
            if source is None:
                continue
            pin = self._find_pin_def(source, link.source_pin)
            if pin is not None and pin.pin_category == category:
                result.append(link)
        return result

    def reachable_nodes(
        self,
        start_node_uids: Iterable[str],
        pin_category: PinCategory | str = PinCategory.EXEC,
    ) -> List[GraphNode]:
        outgoing: Dict[str, List[str]] = {}
        for link in self.links_for_category(pin_category):
            outgoing.setdefault(link.source_node, []).append(link.target_node)

        visited = set()
        pending = list(start_node_uids)
        while pending:
            uid = pending.pop(0)
            if uid in visited or self.find_node(uid) is None:
                continue
            visited.add(uid)
            pending.extend(outgoing.get(uid, ()))
        return [node for node in self.nodes if node.uid in visited]

    def topological_nodes(
        self,
        pin_category: PinCategory | str = PinCategory.EXEC,
    ) -> List[GraphNode]:
        links = self.links_for_category(pin_category)
        indegree = {node.uid: 0 for node in self.nodes}
        outgoing: Dict[str, List[str]] = {}
        for link in links:
            if link.source_node not in indegree or link.target_node not in indegree:
                continue
            outgoing.setdefault(link.source_node, []).append(link.target_node)
            indegree[link.target_node] += 1

        pending = [node.uid for node in self.nodes if indegree[node.uid] == 0]
        ordered_uids: List[str] = []
        while pending:
            uid = pending.pop(0)
            ordered_uids.append(uid)
            for target_uid in outgoing.get(uid, ()):
                indegree[target_uid] -= 1
                if indegree[target_uid] == 0:
                    pending.append(target_uid)
        if len(ordered_uids) != len(self.nodes):
            raise GraphCycleError(f"{PinCategory(pin_category).value} graph contains a cycle")
        by_uid = {node.uid: node for node in self.nodes}
        return [by_uid[uid] for uid in ordered_uids]

    def nodes_by_stage(self, stage_key: str = "stage") -> Dict[str, List[GraphNode]]:
        grouped: Dict[str, List[GraphNode]] = {}
        for node in self.nodes:
            stage = str(node.data.get(stage_key, ""))
            grouped.setdefault(stage, []).append(node)
        return grouped

    # ── Reversible authoring ─────────────────────────────────────────

    def authoring_node_payload(self, node: GraphNode) -> dict:
        """Return the domain-neutral payload required to restore *node*."""
        return {
            "type_id": str(node.type_id),
            "position": [float(node.pos_x), float(node.pos_y)],
            "properties": copy.deepcopy(node.data),
        }

    def authoring_link_payload(self, link: GraphLink) -> dict:
        """Return the domain-neutral payload required to restore *link*."""
        return {
            "source_node": str(link.source_node),
            "source_port": str(link.source_pin),
            "target_node": str(link.target_node),
            "target_port": str(link.target_pin),
            "properties": copy.deepcopy(link.data),
        }

    def capture_authoring_state(
        self,
        *,
        identity: Optional[Callable[[NodeGraphElementKind, str], str]] = None,
    ) -> NodeGraphAuthoringState:
        """Capture stable node/link state for a future precise diff."""
        encode = identity or (lambda _kind, stable_id: stable_id)
        nodes: dict[str, NodeGraphElementState] = {}
        for index, node in enumerate(self.nodes):
            stable_id = str(encode(NodeGraphElementKind.NODE, node.uid))
            if stable_id in nodes:
                raise ValueError(f"duplicate node graph identity: {stable_id!r}")
            nodes[stable_id] = NodeGraphElementState(
                self.authoring_node_payload(node), index
            )
        links: dict[str, NodeGraphElementState] = {}
        for index, link in enumerate(self.links):
            stable_id = str(encode(NodeGraphElementKind.LINK, link.uid))
            if stable_id in links:
                raise ValueError(f"duplicate node graph identity: {stable_id!r}")
            links[stable_id] = NodeGraphElementState(
                self.authoring_link_payload(link), index
            )
        return NodeGraphAuthoringState(nodes, links)

    def capture_authoring_subgraph(
        self,
        node_ids: Iterable[str],
    ) -> NodeGraphClipboardState:
        """Capture selected nodes plus every internal link in stable order."""
        selected = {str(value) for value in node_ids if str(value)}
        if not selected:
            raise ValueError("node graph clipboard selection must not be empty")
        state = self.capture_authoring_state()
        nodes = {
            stable_id: item
            for stable_id, item in state.nodes.items()
            if stable_id in selected
        }
        missing = selected - set(nodes)
        if missing:
            raise KeyError(f"node graph clipboard nodes do not exist: {sorted(missing)}")
        links = {
            stable_id: item
            for stable_id, item in state.links.items()
            if str(item.payload.get("source_node", "")) in selected
            and str(item.payload.get("target_node", "")) in selected
        }
        return NodeGraphClipboardState(
            self.graph_kind or "node_graph",
            NodeGraphAuthoringState(nodes, links),
        )

    @staticmethod
    def _fresh_clipboard_id(occupied: set[str]) -> str:
        while True:
            stable_id = uuid.uuid4().hex[:8]
            if stable_id not in occupied:
                return stable_id

    def paste_authoring_subgraph(
        self,
        clipboard: NodeGraphClipboardState,
        *,
        offset: tuple[float, float] = (48.0, 48.0),
        node_identity: Optional[Callable[[str, dict], str]] = None,
        link_identity: Optional[Callable[[str, dict], str]] = None,
        node_payload: Optional[
            Callable[[str, str, dict, Mapping[str, str]], dict]
        ] = None,
        link_payload: Optional[
            Callable[[str, str, dict, Mapping[str, str]], dict]
        ] = None,
    ) -> NodeGraphPasteResult:
        """Paste a captured subgraph as one atomic model operation.

        The graph core owns ordering, identity collision checks, internal-link
        remapping, validation, and rollback.  Domain adapters may only provide
        stable-ID factories and pure payload transforms for embedded domain
        references.
        """
        if not isinstance(clipboard, NodeGraphClipboardState):
            raise TypeError("clipboard must be a NodeGraphClipboardState")
        if clipboard.graph_kind != (self.graph_kind or "node_graph"):
            raise ValueError(
                f"cannot paste {clipboard.graph_kind!r} nodes into "
                f"{(self.graph_kind or 'node_graph')!r}"
            )
        if len(offset) != 2:
            raise ValueError("node graph paste offset must contain two values")

        occupied_nodes = {node.uid for node in self.nodes}
        occupied_links = {link.uid for link in self.links}
        node_map: dict[str, str] = {}
        link_map: dict[str, str] = {}
        ordered_nodes = sorted(
            clipboard.state.nodes.items(),
            key=lambda pair: (pair[1].index, pair[0]),
        )
        ordered_links = sorted(
            clipboard.state.links.items(),
            key=lambda pair: (pair[1].index, pair[0]),
        )

        for old_id, item in ordered_nodes:
            new_id = str(
                node_identity(old_id, copy.deepcopy(item.payload))
                if node_identity is not None
                else self._fresh_clipboard_id(occupied_nodes)
            ).strip()
            if not new_id or new_id in occupied_nodes or new_id in node_map.values():
                raise ValueError(f"node graph paste produced duplicate node ID: {new_id!r}")
            occupied_nodes.add(new_id)
            node_map[old_id] = new_id
        for old_id, item in ordered_links:
            new_id = str(
                link_identity(old_id, copy.deepcopy(item.payload))
                if link_identity is not None
                else self._fresh_clipboard_id(occupied_links)
            ).strip()
            if not new_id or new_id in occupied_links or new_id in link_map.values():
                raise ValueError(f"node graph paste produced duplicate link ID: {new_id!r}")
            occupied_links.add(new_id)
            link_map[old_id] = new_id

        mutations: list[NodeGraphMutation] = []
        for order, (old_id, item) in enumerate(ordered_nodes):
            new_id = node_map[old_id]
            payload = copy.deepcopy(item.payload)
            position = payload.get("position")
            if not isinstance(position, (list, tuple)) or len(position) != 2:
                raise RuntimeError("node graph clipboard node position is invalid")
            payload["position"] = [
                float(position[0]) + float(offset[0]),
                float(position[1]) + float(offset[1]),
            ]
            if node_payload is not None:
                payload = node_payload(old_id, new_id, payload, node_map)
            if not isinstance(payload, dict):
                raise TypeError("node graph paste node transform must return a dict")
            mutations.append(
                NodeGraphMutation(
                    NodeGraphMutationKind.INSERT,
                    NodeGraphElementKind.NODE,
                    new_id,
                    after=payload,
                    after_index=len(self.nodes) + order,
                )
            )

        for order, (old_id, item) in enumerate(ordered_links):
            new_id = link_map[old_id]
            payload = copy.deepcopy(item.payload)
            source = str(payload.get("source_node", ""))
            target = str(payload.get("target_node", ""))
            if source not in node_map or target not in node_map:
                raise RuntimeError("node graph clipboard link escapes the captured subgraph")
            payload["source_node"] = node_map[source]
            payload["target_node"] = node_map[target]
            if link_payload is not None:
                payload = link_payload(old_id, new_id, payload, node_map)
            if not isinstance(payload, dict):
                raise TypeError("node graph paste link transform must return a dict")
            mutations.append(
                NodeGraphMutation(
                    NodeGraphMutationKind.INSERT,
                    NodeGraphElementKind.LINK,
                    new_id,
                    after=payload,
                    after_index=len(self.links) + order,
                )
            )

        self.apply_authoring_mutations(mutations)
        return NodeGraphPasteResult(
            tuple(node_map[old_id] for old_id, _item in ordered_nodes),
            tuple(link_map[old_id] for old_id, _item in ordered_links),
            node_map,
            link_map,
        )

    def restore_authoring_state(self, state: NodeGraphAuthoringState) -> None:
        """Restore an authoring checkpoint without replaying a partial diff."""
        if not isinstance(state, NodeGraphAuthoringState):
            raise TypeError("authoring checkpoint must be a NodeGraphAuthoringState")

        restored_nodes: list[GraphNode] = []
        for stable_id, item in sorted(
            state.nodes.items(), key=lambda pair: (pair[1].index, pair[0])
        ):
            payload = item.payload
            position = payload.get("position")
            properties = payload.get("properties")
            if (
                not isinstance(position, (list, tuple))
                or len(position) != 2
                or not isinstance(properties, dict)
            ):
                raise RuntimeError("node authoring checkpoint is invalid")
            restored_nodes.append(
                GraphNode(
                    uid=stable_id,
                    type_id=str(payload.get("type_id", "")),
                    pos_x=float(position[0]),
                    pos_y=float(position[1]),
                    data=copy.deepcopy(properties),
                )
            )

        restored_links: list[GraphLink] = []
        for stable_id, item in sorted(
            state.links.items(), key=lambda pair: (pair[1].index, pair[0])
        ):
            payload = item.payload
            properties = payload.get("properties", {})
            if not isinstance(properties, dict):
                raise RuntimeError("link authoring checkpoint is invalid")
            restored_links.append(
                GraphLink(
                    uid=stable_id,
                    source_node=str(payload.get("source_node", "")),
                    source_pin=str(payload.get("source_port", "")),
                    target_node=str(payload.get("target_node", "")),
                    target_pin=str(payload.get("target_port", "")),
                    data=copy.deepcopy(properties),
                )
            )

        self.nodes = restored_nodes
        self.links = restored_links
        for node in self.nodes:
            self.on_authoring_node_restored(node)
        for link in self.links:
            self.on_authoring_link_restored(link)
        for node in self.nodes:
            self.invalidate_node_definition(node)

    @staticmethod
    def merge_authoring_states(
        states: Iterable[NodeGraphAuthoringState],
    ) -> NodeGraphAuthoringState:
        nodes: dict[str, NodeGraphElementState] = {}
        links: dict[str, NodeGraphElementState] = {}
        for state in states:
            overlap = nodes.keys() & state.nodes.keys()
            if overlap:
                raise ValueError(f"duplicate node graph identities: {sorted(overlap)!r}")
            overlap = links.keys() & state.links.keys()
            if overlap:
                raise ValueError(f"duplicate node graph identities: {sorted(overlap)!r}")
            nodes.update(state.nodes)
            links.update(state.links)
        return NodeGraphAuthoringState(nodes, links)

    @staticmethod
    def diff_authoring_states(
        before: NodeGraphAuthoringState,
        after: NodeGraphAuthoringState,
    ) -> tuple[NodeGraphMutation, ...]:
        """Build a deterministic, reversible structural diff."""
        mutations: list[NodeGraphMutation] = []

        def removed(kind, left, right):
            return sorted(
                left.keys() - right.keys(),
                key=lambda stable_id: (-left[stable_id].index, stable_id),
            )

        def inserted(kind, left, right):
            return sorted(
                right.keys() - left.keys(),
                key=lambda stable_id: (right[stable_id].index, stable_id),
            )

        for element_kind, left, right in (
            (NodeGraphElementKind.LINK, before.links, after.links),
            (NodeGraphElementKind.NODE, before.nodes, after.nodes),
        ):
            for stable_id in removed(element_kind, left, right):
                item = left[stable_id]
                mutations.append(
                    NodeGraphMutation(
                        NodeGraphMutationKind.REMOVE,
                        element_kind,
                        stable_id,
                        before=item.payload,
                        before_index=item.index,
                    )
                )

        for element_kind, left, right in (
            (NodeGraphElementKind.NODE, before.nodes, after.nodes),
            (NodeGraphElementKind.LINK, before.links, after.links),
        ):
            for stable_id in inserted(element_kind, left, right):
                item = right[stable_id]
                mutations.append(
                    NodeGraphMutation(
                        NodeGraphMutationKind.INSERT,
                        element_kind,
                        stable_id,
                        after=item.payload,
                        after_index=item.index,
                    )
                )

        for element_kind, left, right in (
            (NodeGraphElementKind.NODE, before.nodes, after.nodes),
            (NodeGraphElementKind.LINK, before.links, after.links),
        ):
            common_ids = left.keys() & right.keys()
            before_order = [
                stable_id
                for stable_id, _item in sorted(
                    left.items(), key=lambda pair: (pair[1].index, pair[0])
                )
                if stable_id in common_ids
            ]
            after_order = [
                stable_id
                for stable_id, _item in sorted(
                    right.items(), key=lambda pair: (pair[1].index, pair[0])
                )
                if stable_id in common_ids
            ]
            before_rank = {
                stable_id: index for index, stable_id in enumerate(before_order)
            }
            after_rank = {
                stable_id: index for index, stable_id in enumerate(after_order)
            }
            for stable_id in sorted(left.keys() & right.keys()):
                old = left[stable_id]
                new = right[stable_id]
                order_changed = before_rank[stable_id] != after_rank[stable_id]
                if old.payload == new.payload and not order_changed:
                    continue
                if (
                    element_kind is NodeGraphElementKind.NODE
                    and not order_changed
                    and old.payload.get("type_id") == new.payload.get("type_id")
                    and old.payload.get("properties") == new.payload.get("properties")
                    and {
                        key: value
                        for key, value in old.payload.items()
                        if key != "position"
                    }
                    == {
                        key: value
                        for key, value in new.payload.items()
                        if key != "position"
                    }
                ):
                    mutations.append(
                        NodeGraphMutation(
                            NodeGraphMutationKind.MOVE,
                            element_kind,
                            stable_id,
                            before={"position": old.payload["position"]},
                            after={"position": new.payload["position"]},
                        )
                    )
                    continue
                mutations.append(
                    NodeGraphMutation(
                        NodeGraphMutationKind.UPDATE,
                        element_kind,
                        stable_id,
                        before=old.payload,
                        after=new.payload,
                        before_index=old.index,
                        after_index=new.index,
                    )
                )
        priority = {
            (NodeGraphElementKind.LINK, NodeGraphMutationKind.REMOVE): 0,
            (NodeGraphElementKind.NODE, NodeGraphMutationKind.INSERT): 1,
            (NodeGraphElementKind.NODE, NodeGraphMutationKind.UPDATE): 2,
            (NodeGraphElementKind.NODE, NodeGraphMutationKind.MOVE): 2,
            (NodeGraphElementKind.LINK, NodeGraphMutationKind.UPDATE): 3,
            (NodeGraphElementKind.NODE, NodeGraphMutationKind.REMOVE): 4,
            (NodeGraphElementKind.LINK, NodeGraphMutationKind.INSERT): 5,
        }
        return tuple(
            mutation
            for _index, mutation in sorted(
                enumerate(mutations),
                key=lambda item: (
                    priority[(item[1].element_kind, item[1].kind)],
                    item[0],
                ),
            )
        )

    @staticmethod
    def invert_authoring_mutations(
        mutations: Iterable[NodeGraphMutation],
    ) -> tuple[NodeGraphMutation, ...]:
        return tuple(item.inverted() for item in reversed(tuple(mutations)))

    def prepare_authoring_node_restore(self, payload: dict) -> None:
        """Domain hook called before a node is inserted from a diff."""

    def on_authoring_node_restored(self, node: GraphNode) -> None:
        """Domain hook called after a node payload has been restored."""

    def on_authoring_link_restored(self, link: GraphLink) -> None:
        """Domain hook called after a link payload has been restored."""

    @staticmethod
    def _move_element(items: list, value, index: int) -> None:
        items.remove(value)
        items.insert(max(0, min(int(index), len(items))), value)

    def apply_authoring_mutation(self, mutation: NodeGraphMutation) -> None:
        """Replay one structural mutation through the graph's domain CRUD hooks."""
        if not isinstance(mutation, NodeGraphMutation):
            raise TypeError("authoring mutation must be a NodeGraphMutation")
        payload = mutation.after
        stable_id = mutation.stable_id
        if mutation.element_kind is NodeGraphElementKind.NODE:
            node = self.find_node(stable_id)
            if mutation.kind is NodeGraphMutationKind.INSERT:
                if node is not None or not isinstance(payload, dict):
                    raise RuntimeError(f"cannot insert node {stable_id!r}")
                position = payload.get("position")
                properties = payload.get("properties")
                if (
                    not isinstance(position, (list, tuple))
                    or len(position) != 2
                    or not isinstance(properties, dict)
                ):
                    raise RuntimeError("node authoring payload is invalid")
                self.prepare_authoring_node_restore(payload)
                node = self.add_node(
                    str(payload.get("type_id", "")),
                    float(position[0]),
                    float(position[1]),
                    uid=stable_id,
                    **copy.deepcopy(properties),
                )
                if node.uid != stable_id:
                    raise RuntimeError("node identity changed while replaying a graph edit")
                self._move_element(self.nodes, node, mutation.after_index)
                self.invalidate_node_definition(node)
                self.on_authoring_node_restored(node)
                return
            if mutation.kind is NodeGraphMutationKind.REMOVE:
                if node is not None:
                    self.invalidate_node_definition(node)
                if node is not None and not self.remove_node(stable_id):
                    raise RuntimeError(f"cannot remove node {stable_id!r}")
                return
            if node is None or not isinstance(payload, dict):
                raise RuntimeError(f"cannot update node {stable_id!r}")
            if mutation.kind is NodeGraphMutationKind.MOVE:
                position = payload.get("position")
                if not isinstance(position, (list, tuple)) or len(position) != 2:
                    raise RuntimeError("node move requires a two-value position")
                node.pos_x = float(position[0])
                node.pos_y = float(position[1])
                self.on_authoring_node_restored(node)
                return
            if mutation.kind is NodeGraphMutationKind.UPDATE:
                if str(payload.get("type_id", "")) != node.type_id:
                    raise RuntimeError("node update cannot change the registered type")
                position = payload.get("position")
                properties = payload.get("properties")
                if (
                    not isinstance(position, (list, tuple))
                    or len(position) != 2
                    or not isinstance(properties, dict)
                ):
                    raise RuntimeError("node authoring payload is invalid")
                node.pos_x = float(position[0])
                node.pos_y = float(position[1])
                node.data = copy.deepcopy(properties)
                self._move_element(self.nodes, node, mutation.after_index)
                self.invalidate_node_definition(node)
                self.on_authoring_node_restored(node)
                return
            raise RuntimeError(f"unsupported node mutation: {mutation.kind.value}")

        link = self.find_link(stable_id)
        if mutation.kind is NodeGraphMutationKind.INSERT:
            if link is not None or not isinstance(payload, dict):
                raise RuntimeError(f"cannot insert link {stable_id!r}")
            validation = self.validate_link(
                str(payload.get("source_node", "")),
                str(payload.get("source_port", "")),
                str(payload.get("target_node", "")),
                str(payload.get("target_port", "")),
            )
            if not validation:
                detail = f"{validation.code}: {validation.message}".strip(": ")
                raise RuntimeError(
                    "link insertion was rejected while replaying a graph edit"
                    + (f" ({detail})" if detail else "")
                )
            link = self.add_link(
                str(payload.get("source_node", "")),
                str(payload.get("source_port", "")),
                str(payload.get("target_node", "")),
                str(payload.get("target_port", "")),
                uid=stable_id,
                **copy.deepcopy(payload.get("properties", {})),
            )
            if link is None or link.uid != stable_id:
                raise RuntimeError("link insertion was rejected while replaying a graph edit")
            self._move_element(self.links, link, mutation.after_index)
            self.on_authoring_link_restored(link)
            return
        if mutation.kind is NodeGraphMutationKind.REMOVE:
            if link is not None and not self.remove_link(stable_id):
                raise RuntimeError(f"cannot remove link {stable_id!r}")
            return
        if link is None or not isinstance(payload, dict):
            raise RuntimeError(f"cannot update link {stable_id!r}")
        if mutation.kind is NodeGraphMutationKind.UPDATE:
            link = self.replace_link(
                stable_id,
                str(payload.get("source_node", "")),
                str(payload.get("source_port", "")),
                str(payload.get("target_node", "")),
                str(payload.get("target_port", "")),
            )
            if link is None:
                raise RuntimeError("link replacement was rejected while replaying a graph edit")
            link.data = copy.deepcopy(payload.get("properties", {}))
            self._move_element(self.links, link, mutation.after_index)
            self.on_authoring_link_restored(link)
            return
        raise RuntimeError(f"unsupported link mutation: {mutation.kind.value}")

    def apply_authoring_mutations(
        self, mutations: Iterable[NodeGraphMutation]
    ) -> None:
        checkpoint = self.capture_authoring_state()
        try:
            for mutation in tuple(mutations):
                self.apply_authoring_mutation(mutation)
        except Exception as exc:
            try:
                self.restore_authoring_state(checkpoint)
            except Exception as rollback_error:
                exc.add_note(f"NodeGraph rollback also failed: {rollback_error}")
            raise

    # ── Serialisation ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "links": [lk.to_dict() for lk in self.links],
        }

    def load_dict(self, d: dict) -> None:
        nodes = [GraphNode.from_dict(nd) for nd in d.get("nodes", [])]
        links = [GraphLink.from_dict(lk) for lk in d.get("links", [])]
        node_ids = [node.uid for node in nodes]
        link_ids = [link.uid for link in links]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("node graph data contains duplicate node ids")
        if len(link_ids) != len(set(link_ids)):
            raise ValueError("node graph data contains duplicate link ids")
        self.nodes = nodes
        self.links = links

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    def load_json(self, text: str) -> None:
        self.load_dict(json.loads(text))

    def clear(self) -> None:
        self.nodes.clear()
        self.links.clear()
