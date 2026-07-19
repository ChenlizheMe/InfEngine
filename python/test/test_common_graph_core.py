from __future__ import annotations
import json

import pytest

from Infernux.graph import (
    ExpressionCompileError,
    ExpressionCompiler,
    GraphDocument,
    GraphDocumentError,
    GraphLinkRecord,
    GraphNodeRecord,
    PortKind,
    ValueType,
)


def _expression_document(position=(0.0, 0.0)):
    return GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord("a", "common.constant.vec3", position, {"value": [1.0, 0.0, 0.0]}),
            GraphNodeRecord("b", "common.constant.vec3", (10.0, 0.0), {"value": [0.0, 2.0, 0.0]}),
            GraphNodeRecord("add", "common.math.add"),
            GraphNodeRecord("normal", "common.vector.normalize"),
            GraphNodeRecord("random", "common.random.f32"),
            GraphNodeRecord("seed", "common.constant.u32", properties={"value": 17}),
        ),
        links=(
            GraphLinkRecord("l1", "a", "value", "add", "a"),
            GraphLinkRecord("l2", "b", "value", "add", "b"),
            GraphLinkRecord("l3", "add", "result", "normal", "value"),
            GraphLinkRecord("l4", "seed", "value", "random", "seed"),
        ),
    )


def test_graph_document_v2_round_trip_is_canonical_and_strict():
    document = _expression_document()
    restored = GraphDocument.from_json(document.canonical_json())

    assert restored == document
    assert json.loads(restored.canonical_json())["$version"] == 2

    invalid = restored.to_dict()
    invalid["future"] = True
    with pytest.raises(GraphDocumentError, match="keys mismatch"):
        GraphDocument.from_dict(invalid)


def test_graph_semantic_hash_ignores_canvas_position_but_not_program_values():
    first = _expression_document((0.0, 0.0))
    moved = _expression_document((900.0, -300.0))
    changed_nodes = list(first.nodes)
    changed_nodes[0] = GraphNodeRecord(
        "a", "common.constant.vec3", (0.0, 0.0), {"value": [0.0, 1.0, 0.0]}
    )
    changed = GraphDocument(first.domain, tuple(changed_nodes), first.links)

    assert first.semantic_hash() == moved.semantic_hash()
    assert first.semantic_hash() != changed.semantic_hash()


def test_common_add_normalize_random_compile_to_typed_expression_ir():
    program = ExpressionCompiler().compile(
        _expression_document(),
        outputs=(("normal", "result"), ("random", "value")),
    )

    by_result = {instruction.result_id: instruction for instruction in program.instructions}
    assert by_result["add.result"].opcode == "add"
    assert by_result["add.result"].result_type.value_type is ValueType.VEC3
    assert by_result["normal.result"].opcode == "normalize"
    assert by_result["random.value"].opcode == "random_f32"
    assert by_result["random.value"].operands[2].value_id == "seed.value"
    assert dict(program.outputs)["normal.result"].value_type is ValueType.VEC3


def test_common_expression_compiler_rejects_shape_mismatch():
    document = GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord("scalar", "common.constant.f32", properties={"value": 1.0}),
            GraphNodeRecord("vector", "common.constant.vec3", properties={"value": [1.0, 2.0, 3.0]}),
            GraphNodeRecord("add", "common.math.add"),
        ),
        links=(
            GraphLinkRecord("l1", "scalar", "value", "add", "a"),
            GraphLinkRecord("l2", "vector", "value", "add", "b"),
        ),
    )

    with pytest.raises(ExpressionCompileError, match="matching shapes"):
        ExpressionCompiler().compile(document, outputs=(("add", "result"),))


def test_common_expression_compiler_rejects_bad_literal_and_input_type():
    bad_literal = GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord("vector", "common.constant.vec3", properties={"value": [1.0, 2.0]}),
        ),
    )
    with pytest.raises(ExpressionCompileError, match="exactly 3"):
        ExpressionCompiler().compile(bad_literal, outputs=(("vector", "value"),))

    bad_input = GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord("scalar", "common.constant.f32", properties={"value": 1.0}),
            GraphNodeRecord("normal", "common.vector.normalize"),
        ),
        links=(GraphLinkRecord("link", "scalar", "value", "normal", "value"),),
    )
    with pytest.raises(ExpressionCompileError, match="cannot connect"):
        ExpressionCompiler().compile(bad_input, outputs=(("normal", "result"),))
