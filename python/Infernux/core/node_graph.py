"""
NodeGraph — Generic visual node-graph data model.

Reusable foundation for any node-based editor (state machines,
shader graphs, dialogue trees, etc.).  The *view* layer lives in
:mod:`Infernux.engine.ui.node_graph_view`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, IntEnum
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, runtime_checkable
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
    visual_style: str = "default"
    category_label: str = ""
    show_header_color_swatch: bool = True

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
    """Generic node-graph container with CRUD and serialisation."""

    _DEFAULT_TYPE_COMPATIBILITY = {
        ("int", "float"),
        ("vec3", "color"),
        ("color", "vec3"),
    }

    def __init__(
        self,
        graph_kind: str = "",
        catalog: Optional[NodeCatalog] = None,
    ) -> None:
        self.nodes: List[GraphNode] = []
        self.links: List[GraphLink] = []
        self._type_registry: Dict[str, NodeTypeDef] = {}
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

    def registered_types(self) -> List[NodeTypeDef]:
        return list(self._type_registry.values())

    def register_type_compatibility(self, source_type: str, target_type: str) -> None:
        self._type_compatibility.add((source_type.lower(), target_type.lower()))

    # ── Node CRUD ─────────────────────────────────────────────────────

    def add_node(
        self,
        type_id: str,
        x: float = 0.0,
        y: float = 0.0,
        uid: Optional[str] = None,
        **data: Any,
    ) -> GraphNode:
        node = GraphNode(
            uid=uid or uuid.uuid4().hex[:8],
            type_id=type_id,
            pos_x=x,
            pos_y=y,
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
        if not self.validate_link(src_node, src_pin, dst_node, dst_pin):
            return None
        link = GraphLink(
            uid=uid or uuid.uuid4().hex[:8],
            source_node=src_node,
            source_pin=src_pin,
            target_node=dst_node,
            target_pin=dst_pin,
            data=data,
        )
        self.links.append(link)
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
        typedef = self.get_type(node.type_id)
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

    # ── Serialisation ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "nodes": [n.to_dict() for n in self.nodes],
            "links": [lk.to_dict() for lk in self.links],
        }

    def load_dict(self, d: dict) -> None:
        self.nodes = [GraphNode.from_dict(nd) for nd in d.get("nodes", [])]
        self.links = [GraphLink.from_dict(lk) for lk in d.get("links", [])]

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    def load_json(self, text: str) -> None:
        self.load_dict(json.loads(text))

    def clear(self) -> None:
        self.nodes.clear()
        self.links.clear()
