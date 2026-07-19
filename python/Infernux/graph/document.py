"""Strict GraphDocument with canonical serialization and semantic identity."""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import math
from typing import Any, Mapping

from .registry import PortKind


GRAPH_DOCUMENT_SCHEMA = "infernux.graph_document"


class GraphDocumentError(ValueError):
    pass


def _finite_json(value: Any) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise GraphDocumentError("graph values must be finite JSON data") from exc


@dataclass(frozen=True)
class GraphNodeRecord:
    uid: str
    type_id: str
    position: tuple[float, float] = (0.0, 0.0)
    properties: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.uid) is not str or type(self.type_id) is not str or not self.uid or not self.type_id:
            raise GraphDocumentError("graph nodes require uid and type_id")
        if len(self.position) != 2 or not all(math.isfinite(float(v)) for v in self.position):
            raise GraphDocumentError("graph node position requires two finite numbers")
        object.__setattr__(self, "position", tuple(float(v) for v in self.position))
        object.__setattr__(self, "properties", _finite_json(dict(self.properties)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "uid": self.uid,
            "type_id": self.type_id,
            "position": list(self.position),
            "properties": _finite_json(dict(self.properties)),
        }


@dataclass(frozen=True)
class GraphLinkRecord:
    uid: str
    source_node: str
    source_port: str
    target_node: str
    target_port: str
    kind: PortKind = PortKind.VALUE

    def __post_init__(self) -> None:
        identifiers = (
            self.uid,
            self.source_node,
            self.source_port,
            self.target_node,
            self.target_port,
        )
        if any(type(value) is not str for value in identifiers) or not all(identifiers):
            raise GraphDocumentError("graph links require stable endpoint identifiers")
        object.__setattr__(self, "kind", PortKind(self.kind))

    def to_dict(self) -> dict[str, str]:
        return {
            "uid": self.uid,
            "source_node": self.source_node,
            "source_port": self.source_port,
            "target_node": self.target_node,
            "target_port": self.target_port,
            "kind": self.kind.value,
        }


@dataclass(frozen=True)
class GraphDocument:
    domain: str
    nodes: tuple[GraphNodeRecord, ...] = ()
    links: tuple[GraphLinkRecord, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if type(self.domain) is not str or not self.domain:
            raise GraphDocumentError("graph domain cannot be empty")
        if len({node.uid for node in self.nodes}) != len(self.nodes):
            raise GraphDocumentError("graph node uids must be unique")
        if len({link.uid for link in self.links}) != len(self.links):
            raise GraphDocumentError("graph link uids must be unique")
        node_ids = {node.uid for node in self.nodes}
        if any(link.source_node not in node_ids or link.target_node not in node_ids for link in self.links):
            raise GraphDocumentError("graph links must reference existing nodes")
        object.__setattr__(self, "metadata", _finite_json(dict(self.metadata)))

    def to_dict(self) -> dict[str, Any]:
        return {
            "$schema": GRAPH_DOCUMENT_SCHEMA,
            "domain": self.domain,
            "nodes": [node.to_dict() for node in self.nodes],
            "links": [link.to_dict() for link in self.links],
            "metadata": _finite_json(dict(self.metadata)),
        }

    def canonical_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    def semantic_hash(self) -> str:
        semantic = {
            "domain": self.domain,
            "nodes": [
                {
                    "uid": node.uid,
                    "type_id": node.type_id,
                    "properties": _finite_json(dict(node.properties)),
                }
                for node in sorted(self.nodes, key=lambda item: item.uid)
            ],
            "links": [
                link.to_dict()
                for link in sorted(self.links, key=lambda item: item.uid)
            ],
            "metadata": _finite_json(dict(self.metadata)),
        }
        payload = json.dumps(semantic, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @classmethod
    def from_dict(cls, value: Any) -> "GraphDocument":
        if type(value) is not dict:
            raise GraphDocumentError("graph document root must be an object")
        expected = {"$schema", "domain", "nodes", "links", "metadata"}
        if set(value) != expected:
            raise GraphDocumentError(
                f"graph document keys mismatch; missing={sorted(expected - set(value))}, "
                f"unknown={sorted(set(value) - expected)}"
            )
        if value["$schema"] != GRAPH_DOCUMENT_SCHEMA:
            raise GraphDocumentError("unsupported graph document schema")
        if type(value["nodes"]) is not list or type(value["links"]) is not list:
            raise GraphDocumentError("graph nodes and links must be arrays")
        nodes = tuple(cls._parse_node(item, index) for index, item in enumerate(value["nodes"]))
        links = tuple(cls._parse_link(item, index) for index, item in enumerate(value["links"]))
        if type(value["metadata"]) is not dict:
            raise GraphDocumentError("graph metadata must be an object")
        return cls(value["domain"], nodes, links, value["metadata"])

    @classmethod
    def from_json(cls, text: str | bytes) -> "GraphDocument":
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        return cls.from_dict(json.loads(text))

    @staticmethod
    def _parse_node(value: Any, index: int) -> GraphNodeRecord:
        expected = {"uid", "type_id", "position", "properties"}
        if type(value) is not dict or set(value) != expected:
            raise GraphDocumentError(f"nodes[{index}] has invalid fields")
        if type(value["position"]) is not list or type(value["properties"]) is not dict:
            raise GraphDocumentError(f"nodes[{index}] has invalid position or properties")
        return GraphNodeRecord(
            value["uid"],
            value["type_id"],
            tuple(value["position"]),
            value["properties"],
        )

    @staticmethod
    def _parse_link(value: Any, index: int) -> GraphLinkRecord:
        expected = {"uid", "source_node", "source_port", "target_node", "target_port", "kind"}
        if type(value) is not dict or set(value) != expected:
            raise GraphDocumentError(f"links[{index}] has invalid fields")
        return GraphLinkRecord(
            value["uid"],
            value["source_node"],
            value["source_port"],
            value["target_node"],
            value["target_port"],
            PortKind(value["kind"]),
        )


__all__ = [
    "GRAPH_DOCUMENT_SCHEMA",
    "GraphDocument",
    "GraphDocumentError",
    "GraphLinkRecord",
    "GraphNodeRecord",
]
