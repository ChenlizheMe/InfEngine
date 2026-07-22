from __future__ import annotations

import pytest

from Infernux.core.node_graph import (
    GraphCompiler,
    GraphCycleError,
    GraphDiagnostic,
    NodeCatalog,
    NodeGraph,
    NodeTypeDef,
    PinCategory,
    PinDef,
    PinKind,
)


def _data_source(type_id: str, data_type: str = "float") -> NodeTypeDef:
    return NodeTypeDef(
        type_id=type_id,
        label=type_id,
        pins=[PinDef("value", "Value", PinKind.OUTPUT, data_type=data_type)],
    )


def _data_target(type_id: str, data_type: str = "float", limit: int = 1) -> NodeTypeDef:
    return NodeTypeDef(
        type_id=type_id,
        label=type_id,
        pins=[
            PinDef(
                "value",
                "Value",
                PinKind.INPUT,
                data_type=data_type,
                max_connections=limit,
            )
        ],
    )


def _exec_type() -> NodeTypeDef:
    return NodeTypeDef(
        type_id="module",
        label="Module",
        pins=[
            PinDef("in", "In", PinKind.INPUT, pin_category=PinCategory.EXEC),
            PinDef("out", "Out", PinKind.OUTPUT, pin_category=PinCategory.EXEC),
        ],
    )


def test_catalog_partitions_types_by_graph_kind():
    catalog = NodeCatalog()
    anim_type = _exec_type()
    vfx_type = _data_source("constant")
    shader_type = _data_source("shader_constant", "vec3")

    catalog.register("anim_fsm", [anim_type])
    catalog.register("vfx", [vfx_type])
    catalog.register("shader", [shader_type])

    assert set(catalog.graph_kinds()) == {"anim_fsm", "vfx", "shader"}
    assert catalog.create_graph("anim_fsm").get_type("module") is anim_type
    assert catalog.create_graph("anim_fsm").get_type("constant") is None
    assert catalog.create_graph("vfx").get_type("constant") is vfx_type
    assert catalog.create_graph("shader").get_type("shader_constant") is shader_type
    assert catalog.create_graph("shader").get_type("constant") is None


def test_link_validation_enforces_direction_category_and_type():
    graph = NodeGraph()
    for typedef in (
        _data_source("float_source"),
        _data_source("vec_source", "vec3"),
        _data_target("float_target"),
        _exec_type(),
    ):
        graph.register_type(typedef)

    float_source = graph.add_node("float_source", uid="float_source")
    vec_source = graph.add_node("vec_source", uid="vec_source")
    target = graph.add_node("float_target", uid="target")
    module = graph.add_node("module", uid="module")

    assert graph.validate_link(float_source.uid, "value", target.uid, "value")
    assert graph.validate_link(vec_source.uid, "value", target.uid, "value").code == "type_mismatch"
    assert graph.validate_link(target.uid, "value", float_source.uid, "value").code == "invalid_direction"
    assert graph.validate_link(float_source.uid, "value", module.uid, "in").code == "category_mismatch"
    assert graph.add_link(vec_source.uid, "value", target.uid, "value") is None


def test_link_validation_enforces_max_connections_on_both_ends():
    graph = NodeGraph()
    graph.register_type(NodeTypeDef(
        type_id="limited_source",
        label="Limited Source",
        pins=[PinDef("out", "Out", PinKind.OUTPUT, max_connections=1)],
    ))
    graph.register_type(_data_target("target"))
    source = graph.add_node("limited_source", uid="source")
    first = graph.add_node("target", uid="first")
    second = graph.add_node("target", uid="second")
    other_source = graph.add_node("limited_source", uid="other_source")

    assert graph.add_link(source.uid, "out", first.uid, "value") is not None
    assert graph.validate_link(source.uid, "out", second.uid, "value").code == "source_full"
    assert graph.validate_link(other_source.uid, "out", first.uid, "value").code == "target_full"

    original = graph.links[0]
    replaced = graph.replace_link(
        original.uid, other_source.uid, "out", first.uid, "value"
    )
    assert replaced is original
    assert replaced.source_node == other_source.uid
    assert len(graph.links) == 1


def test_exec_topology_reachability_stage_grouping_and_cycle_detection():
    graph = NodeGraph()
    graph.register_type(_exec_type())
    start = graph.add_node("module", uid="start", stage="spawn")
    update = graph.add_node("module", uid="update", stage="update")
    output = graph.add_node("module", uid="output", stage="output")
    unused = graph.add_node("module", uid="unused", stage="update")

    assert graph.add_link(start.uid, "out", update.uid, "in") is not None
    assert graph.add_link(update.uid, "out", output.uid, "in") is not None
    assert [node.uid for node in graph.reachable_nodes([start.uid])] == ["start", "update", "output"]
    assert [node.uid for node in graph.topological_nodes()] == ["start", "unused", "update", "output"]
    assert [node.uid for node in graph.nodes_by_stage()["update"]] == ["update", "unused"]

    assert graph.add_link(output.uid, "out", start.uid, "in") is not None
    with pytest.raises(GraphCycleError, match="exec graph contains a cycle"):
        graph.topological_nodes()


def test_loaded_invalid_links_produce_structural_diagnostics():
    graph = NodeGraph()
    graph.register_type(_data_source("source"))
    graph.register_type(_data_target("target"))
    graph.load_dict({
        "nodes": [
            {"uid": "source", "type_id": "source"},
            {"uid": "target", "type_id": "target"},
        ],
        "links": [{
            "uid": "bad",
            "source_node": "source",
            "source_pin": "missing",
            "target_node": "target",
            "target_pin": "value",
        }],
    })

    assert [(item.code, item.link_uid) for item in graph.validate()] == [("missing_pin", "bad")]


def test_graph_compiler_protocol_is_domain_implementable():
    class Compiler:
        def validate(self, graph: NodeGraph):
            return [GraphDiagnostic("empty", "Graph is empty")] if not graph.nodes else []

        def compile(self, graph: NodeGraph):
            return tuple(node.uid for node in graph.nodes)

    compiler = Compiler()
    assert isinstance(compiler, GraphCompiler)
    assert compiler.validate(NodeGraph())[0].code == "empty"
