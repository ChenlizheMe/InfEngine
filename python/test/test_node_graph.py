from __future__ import annotations

import pytest

from Infernux.core.node_graph import (
    GraphCompiler,
    GraphCycleError,
    GraphDiagnostic,
    NodeCatalog,
    NodeGraph,
    NodeGraphElementKind,
    NodeGraphMutation,
    NodeGraphMutationKind,
    NodeInlineFieldDef,
    NodeTypeDef,
    PinCategory,
    PinDef,
    PinKind,
)


def _dynamic_node_graph():
    graph = NodeGraph()
    invalidations = []
    scalar = NodeTypeDef(
        "dynamic",
        "Dynamic Scalar",
        pins=[PinDef("value", "Value", PinKind.OUTPUT, data_type="float")],
        inline_fields=[NodeInlineFieldDef("scalar", "Scalar", "float", 1.0)],
    )
    vector = NodeTypeDef(
        "dynamic",
        "Dynamic Vector",
        pins=[PinDef("value", "Value", PinKind.OUTPUT, data_type="vec3")],
        inline_fields=[
            NodeInlineFieldDef("vector", "Vector", "vec3", [0.0, 0.0, 0.0])
        ],
    )
    graph.register_type(scalar)
    graph.register_type(_data_target("target", "float"))

    def resolve(node):
        if node.type_id == "dynamic":
            return vector if node.data.get("mode") == "vector" else scalar
        return graph.get_type(node.type_id)

    def invalidate(node):
        assert node.type_id == "dynamic"
        invalidations.append(node.uid)

    graph.set_node_definition_resolver(resolve, invalidator=invalidate)
    return graph, invalidations


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


def test_dynamic_node_rebuild_migrates_fields_and_disconnects_invalid_links():
    graph, invalidations = _dynamic_node_graph()
    dynamic = graph.add_node("dynamic", uid="dynamic", mode="scalar", scalar=4.0)
    target = graph.add_node("target", uid="target")
    link = graph.add_link(dynamic.uid, "value", target.uid, "value", uid="link")
    assert link is not None

    result = graph.rebuild_node(dynamic.uid, {"mode": "vector"})

    assert result.changed
    assert result.removed_link_ids == (link.uid,)
    assert invalidations == [dynamic.uid]
    assert dynamic.data == {
        "mode": "vector",
        "vector": [0.0, 0.0, 0.0],
    }
    assert graph.find_link(link.uid) is None
    assert [mutation.kind for mutation in result.mutations] == [
        NodeGraphMutationKind.REMOVE,
        NodeGraphMutationKind.UPDATE,
    ]

    graph.apply_authoring_mutations(
        NodeGraph.invert_authoring_mutations(result.mutations)
    )

    restored = graph.find_node(dynamic.uid)
    assert restored.data == {"mode": "scalar", "scalar": 4.0}
    assert graph.find_link(link.uid) is not None


def test_node_graph_rejects_duplicate_stable_ids_at_the_write_boundary():
    graph = NodeGraph()
    graph.register_type(_data_source("source"))
    graph.register_type(_data_target("target", limit=-1))
    graph.add_node("source", uid="source")
    graph.add_node("target", uid="target")
    graph.add_link("source", "value", "target", "value", uid="wire")

    with pytest.raises(ValueError, match="node uid already exists"):
        graph.add_node("source", uid="source")
    with pytest.raises(ValueError, match="link uid already exists"):
        graph.add_link("source", "value", "target", "value", uid="wire")
    with pytest.raises(ValueError, match="duplicate node ids"):
        graph.load_dict(
            {
                "nodes": [
                    {"uid": "duplicate", "type_id": "source"},
                    {"uid": "duplicate", "type_id": "target"},
                ]
            }
        )


def test_node_graph_authoring_replay_rolls_back_a_partial_failure_atomically():
    graph = NodeGraph()
    graph.register_type(_data_source("source"))
    graph.add_node("source", uid="existing", value=1.0)
    before = graph.capture_authoring_state()
    mutations = (
        NodeGraphMutation(
            NodeGraphMutationKind.INSERT,
            NodeGraphElementKind.NODE,
            "inserted",
            after={
                "type_id": "source",
                "position": [0.0, 0.0],
                "properties": {},
            },
            after_index=1,
        ),
        NodeGraphMutation(
            NodeGraphMutationKind.UPDATE,
            NodeGraphElementKind.NODE,
            "missing",
            after={
                "type_id": "source",
                "position": [0.0, 0.0],
                "properties": {},
            },
        ),
    )

    with pytest.raises(RuntimeError, match="cannot update node"):
        graph.apply_authoring_mutations(mutations)

    assert graph.capture_authoring_state() == before


def test_dynamic_rebuild_keeps_the_first_valid_link_when_capacity_shrinks():
    graph = NodeGraph()
    capacity = {"limit": -1}
    source = NodeTypeDef(
        "dynamic-source",
        "Dynamic Source",
        pins=[PinDef("value", "Value", PinKind.OUTPUT, max_connections=-1)],
    )
    target = _data_target("target", limit=-1)
    graph.register_type(source)
    graph.register_type(target)

    def resolve(node):
        if node.type_id != "dynamic-source":
            return graph.get_type(node.type_id)
        resolved = NodeTypeDef(
            source.type_id,
            source.label,
            pins=[
                PinDef(
                    "value",
                    "Value",
                    PinKind.OUTPUT,
                    max_connections=capacity["limit"],
                )
            ],
        )
        return resolved

    def invalidate(_node):
        capacity["limit"] = 1

    graph.set_node_definition_resolver(resolve, invalidator=invalidate)
    dynamic = graph.add_node("dynamic-source", uid="dynamic")
    first = graph.add_node("target", uid="first")
    second = graph.add_node("target", uid="second")
    graph.add_link(dynamic.uid, "value", first.uid, "value", uid="first-link")
    graph.add_link(dynamic.uid, "value", second.uid, "value", uid="second-link")

    result = graph.rebuild_node(dynamic.uid)

    assert result.removed_link_ids == ("second-link",)
    assert [link.uid for link in graph.links] == ["first-link"]


def test_node_canvas_coordinates_do_not_reserve_x_or_y_data_properties():
    graph = NodeGraph()
    node = graph.add_node(
        "vector",
        40.0,
        80.0,
        uid="vector",
        x=1.0,
        y=2.0,
        z=3.0,
    )

    assert (node.pos_x, node.pos_y) == (40.0, 80.0)
    assert node.data == {"x": 1.0, "y": 2.0, "z": 3.0}


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


def test_node_graph_structural_diff_round_trips_nodes_and_links():
    graph = NodeGraph()
    graph.register_type(_data_source("source"))
    graph.register_type(_data_target("target"))
    before = graph.capture_authoring_state()

    graph.add_node("source", 10.0, 20.0, uid="source", value=2.0)
    graph.add_node("target", 30.0, 40.0, uid="target")
    graph.add_link("source", "value", "target", "value", uid="wire")
    after = graph.capture_authoring_state()
    mutations = NodeGraph.diff_authoring_states(before, after)

    assert [
        (item.kind, item.element_kind, item.stable_id) for item in mutations
    ] == [
        (NodeGraphMutationKind.INSERT, NodeGraphElementKind.NODE, "source"),
        (NodeGraphMutationKind.INSERT, NodeGraphElementKind.NODE, "target"),
        (NodeGraphMutationKind.INSERT, NodeGraphElementKind.LINK, "wire"),
    ]

    graph.apply_authoring_mutations(NodeGraph.invert_authoring_mutations(mutations))
    assert graph.capture_authoring_state() == before
    graph.apply_authoring_mutations(mutations)
    assert graph.capture_authoring_state() == after


def test_node_graph_structural_diff_distinguishes_move_from_property_update():
    graph = NodeGraph()
    node = graph.add_node("value", 0.0, 0.0, uid="value", amount=1.0)
    before_move = graph.capture_authoring_state()
    node.pos_x = 15.0
    move = NodeGraph.diff_authoring_states(
        before_move, graph.capture_authoring_state()
    )
    assert len(move) == 1
    assert move[0].kind is NodeGraphMutationKind.MOVE
    assert move[0].before == {"position": [0.0, 0.0]}
    assert move[0].after == {"position": [15.0, 0.0]}

    before_update = graph.capture_authoring_state()
    node.data["amount"] = 3.0
    update = NodeGraph.diff_authoring_states(
        before_update, graph.capture_authoring_state()
    )
    assert len(update) == 1
    assert update[0].kind is NodeGraphMutationKind.UPDATE

    graph.apply_authoring_mutations(NodeGraph.invert_authoring_mutations(update))
    assert node.data["amount"] == 1.0
    graph.apply_authoring_mutations(update)
    assert node.data["amount"] == 3.0


def test_node_graph_structural_diff_supports_document_identity_projection():
    graph = NodeGraph()
    graph.add_node("value", uid="canvas-node")

    state = graph.capture_authoring_state(
        identity=lambda kind, stable_id: f"document/{kind.value}/{stable_id}"
    )

    assert tuple(state.nodes) == ("document/node/canvas-node",)


def test_node_graph_diff_relinks_before_removing_an_old_endpoint():
    graph = NodeGraph()
    graph.register_type(_exec_type())
    graph.add_node("module", uid="entry")
    graph.add_node("module", uid="old")
    graph.add_node("module", uid="replacement")
    graph.add_link("entry", "out", "old", "in", uid="entry-link")
    before = graph.capture_authoring_state()

    graph.replace_link("entry-link", "entry", "out", "replacement", "in")
    graph.remove_node("old")
    after = graph.capture_authoring_state()
    mutations = NodeGraph.diff_authoring_states(before, after)

    relink_index = next(
        index
        for index, item in enumerate(mutations)
        if item.element_kind is NodeGraphElementKind.LINK
        and item.kind is NodeGraphMutationKind.UPDATE
    )
    removal_index = next(
        index
        for index, item in enumerate(mutations)
        if item.element_kind is NodeGraphElementKind.NODE
        and item.kind is NodeGraphMutationKind.REMOVE
    )
    assert relink_index < removal_index
    graph.load_dict({
        "nodes": [
            {"uid": "entry", "type_id": "module"},
            {"uid": "old", "type_id": "module"},
            {"uid": "replacement", "type_id": "module"},
        ],
        "links": [{
            "uid": "entry-link",
            "source_node": "entry",
            "source_pin": "out",
            "target_node": "old",
            "target_pin": "in",
        }],
    })
    graph.apply_authoring_mutations(mutations)
    assert graph.capture_authoring_state() == after
    graph.apply_authoring_mutations(NodeGraph.invert_authoring_mutations(mutations))
    assert graph.capture_authoring_state() == before


def test_node_graph_clipboard_pastes_internal_subgraph_atomically():
    graph = NodeGraph("clipboard")
    graph.register_type(_exec_type())
    graph.add_node("module", 10.0, 20.0, uid="first", marker="a")
    graph.add_node("module", 30.0, 40.0, uid="second", marker="b")
    graph.add_node("module", 50.0, 60.0, uid="outside")
    graph.add_link("first", "out", "second", "in", uid="internal")
    graph.add_link("second", "out", "outside", "in", uid="external")

    clipboard = graph.capture_authoring_subgraph(("first", "second"))
    assert tuple(clipboard.state.nodes) == ("first", "second")
    assert tuple(clipboard.state.links) == ("internal",)

    node_ids = {"first": "first-copy", "second": "second-copy"}
    result = graph.paste_authoring_subgraph(
        clipboard,
        node_identity=lambda old_id, _payload: node_ids[old_id],
        link_identity=lambda _old_id, _payload: "internal-copy",
    )

    assert result.node_id_map == node_ids
    assert result.link_id_map == {"internal": "internal-copy"}
    assert (graph.find_node("first-copy").pos_x, graph.find_node("first-copy").pos_y) == (
        58.0,
        68.0,
    )
    pasted_link = graph.find_link("internal-copy")
    assert pasted_link is not None
    assert (pasted_link.source_node, pasted_link.target_node) == (
        "first-copy",
        "second-copy",
    )
    assert graph.find_link("external-copy") is None


def test_node_graph_clipboard_rolls_back_when_domain_validation_rejects_link():
    graph = NodeGraph("clipboard")
    graph.register_type(_exec_type())
    graph.add_node("module", uid="first")
    graph.add_node("module", uid="second")
    graph.add_link("first", "out", "second", "in", uid="wire")
    clipboard = graph.capture_authoring_subgraph(("first", "second"))
    before = graph.capture_authoring_state()

    with pytest.raises(RuntimeError, match="link insertion was rejected"):
        graph.paste_authoring_subgraph(
            clipboard,
            node_identity=lambda old_id, _payload: f"{old_id}-copy",
            link_identity=lambda _old_id, _payload: "wire-copy",
            link_payload=lambda _old, _new, payload, _mapping: {
                **payload,
                "target_port": "missing",
            },
        )

    assert graph.capture_authoring_state() == before
