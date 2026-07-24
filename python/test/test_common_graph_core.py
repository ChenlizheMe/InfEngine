from __future__ import annotations
import json

import pytest

from Infernux.graph import (
    CoordinateSpace,
    ExpressionCompileError,
    ExpressionCompiler,
    GraphDocument,
    GraphDocumentError,
    GraphLinkRecord,
    GraphNodeRecord,
    PortKind,
    TypeRef,
    TypeSystem,
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


def test_graph_document_round_trip_is_canonical_and_strict():
    document = _expression_document()
    restored = GraphDocument.from_json(document.canonical_json())

    assert restored == document
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


def test_numeric_space_inherits_from_spatial_operand_but_rejects_mixed_spaces():
    types = TypeSystem()
    simulation = TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION)
    world = TypeRef(ValueType.VEC3, CoordinateSpace.WORLD)
    constant = TypeRef(ValueType.VEC3)

    assert types.unify_numeric(simulation, constant) == simulation
    assert types.unify_numeric(constant, simulation) == simulation
    with pytest.raises(TypeError, match="cannot mix simulation and world"):
        types.unify_numeric(simulation, world)


def test_common_lifecycle_math_preserves_color_type_and_scalar_factor():
    document = GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord(
                "start",
                "common.constant.color",
                properties={"value": [1.0, 0.25, 0.0, 1.0]},
            ),
            GraphNodeRecord(
                "end",
                "common.constant.color",
                properties={"value": [0.0, 0.0, 0.0, 0.0]},
            ),
            GraphNodeRecord("age", "common.constant.f32", properties={"value": 1.0}),
            GraphNodeRecord("lifetime", "common.constant.f32", properties={"value": 2.0}),
            GraphNodeRecord("normalized_age", "common.math.divide"),
            GraphNodeRecord("color_over_life", "common.math.lerp"),
        ),
        links=(
            GraphLinkRecord("age-link", "age", "value", "normalized_age", "a"),
            GraphLinkRecord("lifetime-link", "lifetime", "value", "normalized_age", "b"),
            GraphLinkRecord("start-link", "start", "value", "color_over_life", "a"),
            GraphLinkRecord("end-link", "end", "value", "color_over_life", "b"),
            GraphLinkRecord("factor-link", "normalized_age", "result", "color_over_life", "t"),
        ),
    )

    program = ExpressionCompiler().compile(
        document,
        outputs=(("color_over_life", "result"),),
    )

    by_result = {instruction.result_id: instruction for instruction in program.instructions}
    assert by_result["normalized_age.result"].opcode == "divide"
    assert by_result["color_over_life.result"].opcode == "lerp"
    assert by_result["color_over_life.result"].result_type.value_type is ValueType.COLOR


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


def test_curve_and_gradient_nodes_compile_strict_authored_literals():
    curve = {
        "keys": [
            {"time": 0.0, "value": 0.0, "in_tangent": 0.0, "out_tangent": 1.0},
            {"time": 1.0, "value": 1.0, "in_tangent": 1.0, "out_tangent": 0.0},
        ],
        "pre_wrap": "clamp",
        "post_wrap": "repeat",
    }
    gradient = {
        "keys": [
            {"time": 0.0, "color": [1.0, 0.0, 0.0, 1.0]},
            {"time": 1.0, "color": [0.0, 0.0, 1.0, 0.0]},
        ],
        "mode": "linear",
    }
    document = GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord("curve", "common.curve.sample", properties={"curve": curve, "t": 0.25}),
            GraphNodeRecord("gradient", "common.gradient.sample", properties={"gradient": gradient, "t": 0.75}),
        ),
    )
    program = ExpressionCompiler().compile(document, outputs=(("curve", "value"), ("gradient", "color")))

    assert [instruction.opcode for instruction in program.instructions] == ["sample_curve", "sample_gradient"]
    assert program.instructions[0].immediate_dict()["curve"] == curve
    assert program.instructions[1].immediate_dict()["gradient"] == gradient

    invalid = dict(curve)
    invalid["keys"] = [dict(curve["keys"][0]), dict(curve["keys"][0])]
    bad_document = GraphDocument("particle.expression", nodes=(GraphNodeRecord("curve", "common.curve.sample", properties={"curve": invalid}),))
    with pytest.raises(ExpressionCompileError, match="strictly increasing"):
        ExpressionCompiler().compile(bad_document, outputs=(("curve", "value"),))


def test_shared_noise_nodes_preserve_vector_space_and_require_coordinates():
    document = GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord(
                "position",
                "common.constant.vec3",
                properties={"value": [1.25, -2.0, 0.5]},
            ),
            GraphNodeRecord(
                "value-noise",
                "common.noise.value3d",
                properties={"frequency": 2.0, "seed": 9},
            ),
            GraphNodeRecord("vector-noise", "common.noise.vector3d"),
        ),
        links=(
            GraphLinkRecord("value-position", "position", "value", "value-noise", "position"),
            GraphLinkRecord("vector-position", "position", "value", "vector-noise", "position"),
        ),
    )

    program = ExpressionCompiler().compile(
        document,
        outputs=(("value-noise", "value"), ("vector-noise", "value")),
    )

    assert [item.opcode for item in program.instructions][-2:] == [
        "value_noise_3d",
        "vector_noise_3d",
    ]
    assert program.outputs[0][1].value_type is ValueType.F32
    assert program.outputs[1][1].value_type is ValueType.VEC3

    missing = GraphDocument(
        "particle.expression",
        nodes=(GraphNodeRecord("noise", "common.noise.value3d"),),
    )
    with pytest.raises(ExpressionCompileError, match="required input"):
        ExpressionCompiler().compile(missing, outputs=(("noise", "value"),))
