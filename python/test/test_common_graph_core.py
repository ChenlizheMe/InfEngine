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
    GraphSourceLocation,
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


def test_graph_source_locations_are_ephemeral_and_non_semantic():
    document = _expression_document()
    located = GraphDocument(
        document.domain,
        document.nodes,
        document.links,
        document.metadata,
        {
            "add": GraphSourceLocation(
                "Smoke.particle.py",
                line=12,
                column=9,
                end_line=12,
                end_column=28,
            )
        },
    )

    assert located == document
    assert located.semantic_hash() == document.semantic_hash()
    assert located.canonical_json() == document.canonical_json()
    assert "source_locations" not in located.to_dict()
    assert located.source_location("add").describe("add", "a") == (
        "Smoke.particle.py:12:9 [add.a]"
    )
    assert GraphDocument.from_json(located.canonical_json()).source_locations == {}


def test_graph_document_uses_exec_as_the_only_control_link_kind():
    document = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root", "particle.root.update"),
            GraphNodeRecord("tail", "particle.attribute.size"),
        ),
        links=(
            GraphLinkRecord(
                "root-tail", "root", "out", "tail", "in", PortKind.EXEC
            ),
        ),
    )
    payload = document.to_dict()

    assert payload["links"][0]["kind"] == "exec"
    payload["links"][0]["kind"] = "stream"
    with pytest.raises(GraphDocumentError, match="invalid port kind 'stream'"):
        GraphDocument.from_dict(payload)


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


def test_common_foundational_math_nodes_compile_with_strict_vector_types():
    document = GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord(
                "a", "common.constant.vec3", properties={"value": [1.0, 2.0, 3.0]}
            ),
            GraphNodeRecord(
                "b", "common.constant.vec3", properties={"value": [3.0, 2.0, 1.0]}
            ),
            GraphNodeRecord("dot", "common.vector.dot"),
            GraphNodeRecord("cross", "common.vector.cross"),
            GraphNodeRecord("length", "common.vector.length"),
            GraphNodeRecord("sine", "common.math.sine"),
            GraphNodeRecord(
                "clamp",
                "common.math.clamp",
                properties={"minimum": -0.5, "maximum": 0.5},
            ),
        ),
        links=(
            GraphLinkRecord("a-dot", "a", "value", "dot", "a"),
            GraphLinkRecord("b-dot", "b", "value", "dot", "b"),
            GraphLinkRecord("a-cross", "a", "value", "cross", "a"),
            GraphLinkRecord("b-cross", "b", "value", "cross", "b"),
            GraphLinkRecord("cross-length", "cross", "result", "length", "value"),
            GraphLinkRecord("a-sine", "a", "value", "sine", "value"),
            GraphLinkRecord("sine-clamp", "sine", "result", "clamp", "value"),
        ),
    )

    program = ExpressionCompiler().compile(
        document,
        outputs=(("dot", "result"), ("length", "result"), ("clamp", "result")),
    )
    by_result = {
        instruction.result_id: instruction for instruction in program.instructions
    }

    assert by_result["dot.result"].result_type == TypeRef(ValueType.F32)
    assert by_result["cross.result"].result_type == TypeRef(ValueType.VEC3)
    assert by_result["length.result"].result_type == TypeRef(ValueType.F32)
    assert by_result["sine.result"].result_type == TypeRef(ValueType.VEC3)
    assert by_result["clamp.result"].result_type == TypeRef(ValueType.VEC3)
    assert [operand.value_type for operand in by_result["clamp.result"].operands] == [
        TypeRef(ValueType.VEC3),
        TypeRef(ValueType.VEC3),
        TypeRef(ValueType.VEC3),
    ]


def test_common_vector_math_rejects_scalar_dot_and_non_vec3_cross():
    scalar_dot = GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord("a", "common.constant.f32", properties={"value": 1.0}),
            GraphNodeRecord("b", "common.constant.f32", properties={"value": 2.0}),
            GraphNodeRecord("dot", "common.vector.dot"),
        ),
        links=(
            GraphLinkRecord("a-dot", "a", "value", "dot", "a"),
            GraphLinkRecord("b-dot", "b", "value", "dot", "b"),
        ),
    )
    with pytest.raises(ExpressionCompileError, match="requires matching vector inputs"):
        ExpressionCompiler().compile(scalar_dot, outputs=(("dot", "result"),))

    vec2_cross = GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord(
                "a", "common.constant.vec2", properties={"value": [1.0, 0.0]}
            ),
            GraphNodeRecord(
                "b", "common.constant.vec2", properties={"value": [0.0, 1.0]}
            ),
            GraphNodeRecord("cross", "common.vector.cross"),
        ),
        links=(
            GraphLinkRecord("a-cross", "a", "value", "cross", "a"),
            GraphLinkRecord("b-cross", "b", "value", "cross", "b"),
        ),
    )
    with pytest.raises(ExpressionCompileError, match="requires matching vec3 inputs"):
        ExpressionCompiler().compile(vec2_cross, outputs=(("cross", "result"),))


def test_common_expression_compiler_promotes_numeric_inputs_to_largest_shape():
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

    program = ExpressionCompiler().compile(document, outputs=(("add", "result"),))

    resize = next(
        instruction
        for instruction in program.instructions
        if instruction.opcode == "numeric_resize"
    )
    add = next(
        instruction for instruction in program.instructions if instruction.opcode == "add"
    )
    assert resize.result_type == TypeRef(ValueType.VEC3)
    assert resize.operands[0].value_type == TypeRef(ValueType.F32)
    assert add.result_type == TypeRef(ValueType.VEC3)
    assert all(operand.value_type == TypeRef(ValueType.VEC3) for operand in add.operands)


def test_boolean_constants_comparisons_and_logic_compile_to_typed_expression_ir():
    document = GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord("one", "common.constant.f32", properties={"value": 1.0}),
            GraphNodeRecord("two", "common.constant.f32", properties={"value": 2.0}),
            GraphNodeRecord("truth", "common.constant.bool", properties={"value": True}),
            GraphNodeRecord("equal", "common.compare.equal"),
            GraphNodeRecord("not-equal", "common.compare.not_equal"),
            GraphNodeRecord("and", "common.logic.and"),
            GraphNodeRecord("or", "common.logic.or"),
            GraphNodeRecord("not", "common.logic.not"),
        ),
        links=(
            GraphLinkRecord("one-equal-a", "one", "value", "equal", "a"),
            GraphLinkRecord("two-equal-b", "two", "value", "equal", "b"),
            GraphLinkRecord("one-ne-a", "one", "value", "not-equal", "a"),
            GraphLinkRecord("two-ne-b", "two", "value", "not-equal", "b"),
            GraphLinkRecord("equal-and-a", "equal", "result", "and", "a"),
            GraphLinkRecord("truth-and-b", "truth", "value", "and", "b"),
            GraphLinkRecord("and-or-a", "and", "result", "or", "a"),
            GraphLinkRecord("ne-or-b", "not-equal", "result", "or", "b"),
            GraphLinkRecord("or-not", "or", "result", "not", "value"),
        ),
    )

    program = ExpressionCompiler().compile(document, outputs=(("not", "result"),))

    assert program.outputs[-1][1] == TypeRef(ValueType.BOOL)
    assert {instruction.opcode for instruction in program.instructions} >= {
        "equal",
        "not_equal",
        "logical_and",
        "logical_or",
        "logical_not",
    }


def test_vector_split_exposes_independent_scalar_components():
    document = GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord(
                "vector",
                "common.constant.vec3",
                properties={"value": [1.0, 2.0, 3.0]},
            ),
            GraphNodeRecord("split", "common.vector.split3"),
        ),
        links=(
            GraphLinkRecord("vector-split", "vector", "value", "split", "value"),
        ),
    )

    program = ExpressionCompiler().compile(
        document,
        outputs=(("split", "x"), ("split", "y"), ("split", "z")),
    )

    splits = [
        instruction
        for instruction in program.instructions
        if instruction.opcode == "split_component"
    ]
    assert [item.immediate_dict()["component"] for item in splits] == [0, 1, 2]
    assert all(item.result_type == TypeRef(ValueType.F32) for item in splits)


def test_numeric_space_inherits_from_spatial_operand_but_rejects_mixed_spaces():
    types = TypeSystem()
    simulation = TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION)
    world = TypeRef(ValueType.VEC3, CoordinateSpace.WORLD)
    constant = TypeRef(ValueType.VEC3)

    assert types.unify_numeric(simulation, constant) == simulation
    assert types.unify_numeric(constant, simulation) == simulation
    with pytest.raises(TypeError, match="cannot mix simulation and world"):
        types.unify_numeric(simulation, world)


def test_spatial_vec3_slots_accept_convertible_spaces_and_untyped_vectors():
    types = TypeSystem()
    simulation = TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION)
    world = TypeRef(ValueType.VEC3, CoordinateSpace.WORLD)
    view = TypeRef(ValueType.VEC3, CoordinateSpace.VIEW)
    constant = TypeRef(ValueType.VEC3)

    assert types.can_connect(world, simulation)
    assert types.can_connect(simulation, world)
    assert types.can_connect(constant, simulation)
    assert types.can_resize_numeric(world, simulation)
    assert not types.can_connect(world, view)
    assert types.adaptation_ops(world, simulation, semantic="position") == (
        (
            "convert_space",
            simulation,
            {"from": "world", "to": "simulation", "semantic": "position"},
        ),
    )
    assert types.adaptation_ops(constant, simulation) == (
        (
            "convert_space",
            simulation,
            {"from": "none", "to": "simulation", "semantic": "direction"},
        ),
    )


def test_vec4_and_color_are_assignment_compatible_four_channel_values():
    types = TypeSystem()
    vec4 = TypeRef(ValueType.VEC4)
    color = TypeRef(ValueType.COLOR)

    assert types.can_connect(vec4, color)
    assert types.can_connect(color, vec4)


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

    broadcast = GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord("scalar", "common.constant.f32", properties={"value": 1.0}),
            GraphNodeRecord("normal", "common.vector.normalize"),
        ),
        links=(GraphLinkRecord("link", "scalar", "value", "normal", "value"),),
    )
    program = ExpressionCompiler().compile(broadcast, outputs=(("normal", "result"),))
    resize = next(
        instruction
        for instruction in program.instructions
        if instruction.opcode == "numeric_resize"
    )
    assert resize.result_type == TypeRef(ValueType.VEC3)
    assert resize.operands[0].value_type == TypeRef(ValueType.F32)


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
    assert program.instructions[0].operands[0].literal == curve
    assert program.instructions[1].operands[0].literal == gradient

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


def test_fixed_noise_coordinate_truncates_but_frequency_remains_scalar():
    document = GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord(
                "position",
                "common.vector.compose4",
                properties={"x": 1.0, "y": 2.0, "z": 3.0, "w": 4.0},
            ),
            GraphNodeRecord(
                "frequency", "common.constant.f32", properties={"value": 2.5}
            ),
            GraphNodeRecord("noise", "common.noise.value3d"),
        ),
        links=(
            GraphLinkRecord("position-link", "position", "value", "noise", "position"),
            GraphLinkRecord("frequency-link", "frequency", "value", "noise", "frequency"),
        ),
    )

    program = ExpressionCompiler().compile(document, outputs=(("noise", "value"),))
    resize = next(
        instruction
        for instruction in program.instructions
        if instruction.opcode == "numeric_resize"
    )
    noise = next(
        instruction
        for instruction in program.instructions
        if instruction.opcode == "value_noise_3d"
    )

    assert resize.operands[0].value_type == TypeRef(ValueType.VEC4)
    assert resize.result_type == TypeRef(ValueType.VEC3)
    assert noise.operands[0].value_type == TypeRef(ValueType.VEC3)
    assert noise.operands[1].value_type == TypeRef(ValueType.F32)
    assert noise.operands[1].value_id == "frequency.value"


def test_vector_compose_nodes_expose_independent_component_inputs():
    document = GraphDocument(
        "particle.expression",
        nodes=(
            GraphNodeRecord("x", "common.constant.f32", properties={"value": 1.0}),
            GraphNodeRecord("vector", "common.vector.compose3", properties={"y": 2.0, "z": 3.0}),
        ),
        links=(GraphLinkRecord("x-link", "x", "value", "vector", "x"),),
    )

    program = ExpressionCompiler().compile(document, outputs=(("vector", "value"),))
    compose = next(
        instruction
        for instruction in program.instructions
        if instruction.opcode == "compose_vec3"
    )

    assert compose.result_type == TypeRef(ValueType.VEC3)
    assert [operand.value_type for operand in compose.operands] == [
        TypeRef(ValueType.F32),
        TypeRef(ValueType.F32),
        TypeRef(ValueType.F32),
    ]
    assert compose.operands[0].value_id == "x.value"
    assert [operand.literal for operand in compose.operands[1:]] == [2.0, 3.0]
