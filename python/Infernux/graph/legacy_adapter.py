"""Adapters that let the existing NodeGraph canvas edit GraphDocument v2."""

from __future__ import annotations

from Infernux.core.node_graph import NodeGraph, PinCategory

from .document import GraphDocument, GraphLinkRecord, GraphNodeRecord
from .registry import PortKind


def graph_document_from_legacy(graph: NodeGraph, *, domain: str) -> GraphDocument:
    nodes = tuple(
        GraphNodeRecord(
            node.uid,
            node.type_id,
            (node.pos_x, node.pos_y),
            node.data,
        )
        for node in graph.nodes
    )
    links = []
    for link in graph.links:
        source = graph.find_node(link.source_node)
        source_pin = graph._find_pin_def(source, link.source_pin) if source is not None else None
        category = source_pin.pin_category if source_pin is not None else PinCategory.DATA
        links.append(
            GraphLinkRecord(
                link.uid,
                link.source_node,
                link.source_pin,
                link.target_node,
                link.target_pin,
                PortKind.VALUE if category is PinCategory.DATA else PortKind.STREAM,
            )
        )
    return GraphDocument(domain, nodes, tuple(links))


def apply_graph_document_to_legacy(document: GraphDocument, graph: NodeGraph) -> None:
    graph.load_dict(
        {
            "nodes": [
                {
                    "uid": node.uid,
                    "type_id": node.type_id,
                    "pos_x": node.position[0],
                    "pos_y": node.position[1],
                    "data": dict(node.properties),
                }
                for node in document.nodes
            ],
            "links": [
                {
                    "uid": link.uid,
                    "source_node": link.source_node,
                    "source_pin": link.source_port,
                    "target_node": link.target_node,
                    "target_pin": link.target_port,
                    "data": {},
                }
                for link in document.links
            ],
        }
    )


__all__ = ["apply_graph_document_to_legacy", "graph_document_from_legacy"]
