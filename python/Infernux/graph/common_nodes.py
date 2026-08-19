"""Common expression nodes usable by ParticleGraph and future graph targets."""

from __future__ import annotations

from .registry import (
    COMMON_NODE_REGISTRY,
    NodeDef,
    PortDef,
    PortDimensionPolicy,
    PortDirection,
    PropertyDef,
)
from .types import CoordinateSpace, TypeRef, ValueType
from .ramp import Curve, Gradient


def _input(
    port_id: str,
    value_type=None,
    *,
    variable="",
    default=None,
    required=False,
    dimension_policy=PortDimensionPolicy.EXACT,
    display_name="",
) -> PortDef:
    return PortDef(
        port_id,
        PortDirection.INPUT,
        value_type=value_type,
        type_variable=variable,
        required=required,
        default=default,
        dimension_policy=dimension_policy,
        display_name=display_name,
    )


def _output(port_id: str, value_type=None, *, variable="", type_property="") -> PortDef:
    return PortDef(
        port_id,
        PortDirection.OUTPUT,
        value_type=value_type,
        type_variable=variable,
        type_property=type_property,
    )


COMMON_NODE_DEFINITIONS = (
    NodeDef(
        "common.constant.bool",
        "Boolean",
        (_output("value", TypeRef(ValueType.BOOL)),),
        (PropertyDef("value", TypeRef(ValueType.BOOL), False),),
        {"expression": "constant"},
    ),
    NodeDef(
        "common.constant.i32",
        "Integer",
        (_output("value", TypeRef(ValueType.I32)),),
        (PropertyDef("value", TypeRef(ValueType.I32), 0),),
        {"expression": "constant"},
    ),
    NodeDef(
        "common.constant.f32",
        "Float",
        (_output("value", TypeRef(ValueType.F32)),),
        (PropertyDef("value", TypeRef(ValueType.F32), 0.0),),
        {"expression": "constant"},
    ),
    NodeDef(
        "common.constant.u32",
        "Unsigned Integer",
        (_output("value", TypeRef(ValueType.U32)),),
        (PropertyDef("value", TypeRef(ValueType.U32), 0),),
        {"expression": "constant"},
    ),
    NodeDef(
        "common.constant.vec2",
        "Vector 2 Constant",
        (_output("value", TypeRef(ValueType.VEC2)),),
        (PropertyDef("value", TypeRef(ValueType.VEC2), [0.0, 0.0]),),
        {"expression": "constant"},
    ),
    NodeDef(
        "common.constant.vec3",
        "Vector 3 Constant",
        (_output("value", TypeRef(ValueType.VEC3)),),
        (PropertyDef("value", TypeRef(ValueType.VEC3), [0.0, 0.0, 0.0]),),
        {"expression": "constant"},
    ),
    NodeDef(
        "common.constant.color",
        "Color",
        (_output("value", TypeRef(ValueType.COLOR)),),
        (PropertyDef("value", TypeRef(ValueType.COLOR), [1.0, 1.0, 1.0, 1.0]),),
        {"expression": "constant"},
    ),
    NodeDef(
        "common.math.add",
        "Add",
        (
            _input(
                "a", variable="T", default=0.0,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _input(
                "b", variable="T", default=0.0,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "add"},
    ),
    NodeDef(
        "common.math.subtract",
        "Subtract",
        (
            _input(
                "a", variable="T", default=0.0,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _input(
                "b", variable="T", default=0.0,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "subtract"},
    ),
    NodeDef(
        "common.math.multiply",
        "Multiply",
        (
            _input("a", variable="T", default=0.0, dimension_policy=PortDimensionPolicy.PROMOTE),
            _input("b", variable="T", default=0.0, dimension_policy=PortDimensionPolicy.PROMOTE),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "multiply"},
    ),
    NodeDef(
        "common.math.divide",
        "Divide",
        (
            _input("a", variable="T", default=0.0, dimension_policy=PortDimensionPolicy.PROMOTE),
            _input("b", variable="T", default=1.0, dimension_policy=PortDimensionPolicy.PROMOTE),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "divide"},
    ),
    NodeDef(
        "common.math.lerp",
        "Lerp",
        (
            _input("a", variable="T", default=0.0, dimension_policy=PortDimensionPolicy.PROMOTE),
            _input("b", variable="T", default=1.0, dimension_policy=PortDimensionPolicy.PROMOTE),
            _input(
                "t",
                TypeRef(ValueType.F32),
                default=0.5,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "lerp"},
    ),
    NodeDef(
        "common.math.minimum",
        "Minimum",
        (
            _input("a", variable="T", default=0.0, dimension_policy=PortDimensionPolicy.PROMOTE),
            _input("b", variable="T", default=0.0, dimension_policy=PortDimensionPolicy.PROMOTE),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "minimum"},
    ),
    NodeDef(
        "common.math.maximum",
        "Maximum",
        (
            _input("a", variable="T", default=0.0, dimension_policy=PortDimensionPolicy.PROMOTE),
            _input("b", variable="T", default=0.0, dimension_policy=PortDimensionPolicy.PROMOTE),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "maximum"},
    ),
    NodeDef(
        "common.math.power",
        "Power",
        (
            _input(
                "a", variable="T", default=1.0,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _input(
                "b", variable="T", default=1.0,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "power"},
    ),
    NodeDef(
        "common.math.clamp",
        "Clamp",
        (
            _input(
                "value", variable="T", default=0.0,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _input(
                "minimum", variable="T", default=0.0,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _input(
                "maximum", variable="T", default=1.0,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "clamp"},
    ),
    NodeDef(
        "common.math.saturate",
        "Saturate",
        (
            _input(
                "value",
                variable="T",
                default=0.0,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "saturate"},
    ),
    *tuple(
        NodeDef(
            f"common.math.{type_id}",
            display_name,
            (
                _input(
                    "value",
                    variable="T",
                    default=0.0,
                    dimension_policy=PortDimensionPolicy.PROMOTE,
                ),
                _output("result", variable="T"),
            ),
            target_opcodes={"expression": opcode},
        )
        for type_id, display_name, opcode in (
            ("absolute", "Absolute", "absolute"),
            ("floor", "Floor", "floor"),
            ("ceil", "Ceil", "ceil"),
            ("fraction", "Fraction", "fraction"),
            ("square_root", "Square Root", "square_root"),
            ("sine", "Sine", "sine"),
            ("cosine", "Cosine", "cosine"),
        )
    ),
    NodeDef(
        "common.constant.vec4",
        "Vector 4 Constant",
        (_output("value", TypeRef(ValueType.VEC4)),),
        (PropertyDef("value", TypeRef(ValueType.VEC4), [0.0, 0.0, 0.0, 0.0]),),
        {"expression": "constant"},
    ),
    NodeDef(
        "common.compare.less_than",
        "Less Than",
        (
            _input(
                "a",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _input(
                "b",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _output("result", TypeRef(ValueType.BOOL)),
        ),
        target_opcodes={"expression": "less_than"},
    ),
    NodeDef(
        "common.compare.less_equal",
        "Less Than Or Equal",
        (
            _input(
                "a",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _input(
                "b",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _output("result", TypeRef(ValueType.BOOL)),
        ),
        target_opcodes={"expression": "less_equal"},
    ),
    NodeDef(
        "common.compare.greater_than",
        "Greater Than",
        (
            _input(
                "a",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _input(
                "b",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _output("result", TypeRef(ValueType.BOOL)),
        ),
        target_opcodes={"expression": "greater_than"},
    ),
    NodeDef(
        "common.compare.greater_equal",
        "Greater Than Or Equal",
        (
            _input(
                "a",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _input(
                "b",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _output("result", TypeRef(ValueType.BOOL)),
        ),
        target_opcodes={"expression": "greater_equal"},
    ),
    NodeDef(
        "common.compare.equal",
        "Equal",
        (
            _input(
                "a",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _input(
                "b",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _output("result", TypeRef(ValueType.BOOL)),
        ),
        target_opcodes={"expression": "equal"},
    ),
    NodeDef(
        "common.compare.not_equal",
        "Not Equal",
        (
            _input(
                "a",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _input(
                "b",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _output("result", TypeRef(ValueType.BOOL)),
        ),
        target_opcodes={"expression": "not_equal"},
    ),
    NodeDef(
        "common.logic.and",
        "And",
        (
            _input("a", TypeRef(ValueType.BOOL), default=False),
            _input("b", TypeRef(ValueType.BOOL), default=False),
            _output("result", TypeRef(ValueType.BOOL)),
        ),
        target_opcodes={"expression": "logical_and"},
    ),
    NodeDef(
        "common.logic.or",
        "Or",
        (
            _input("a", TypeRef(ValueType.BOOL), default=False),
            _input("b", TypeRef(ValueType.BOOL), default=False),
            _output("result", TypeRef(ValueType.BOOL)),
        ),
        target_opcodes={"expression": "logical_or"},
    ),
    NodeDef(
        "common.logic.not",
        "Not",
        (
            _input("value", TypeRef(ValueType.BOOL), default=False),
            _output("result", TypeRef(ValueType.BOOL)),
        ),
        target_opcodes={"expression": "logical_not"},
    ),
    NodeDef(
        "common.vector.normalize",
        "Normalize",
        (
            _input(
                "value",
                TypeRef(ValueType.VEC3),
                default=[0.0, 0.0, 0.0],
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _output("result", TypeRef(ValueType.VEC3)),
        ),
        target_opcodes={"expression": "normalize"},
    ),
    NodeDef(
        "common.vector.length",
        "Length",
        (
            _input(
                "value",
                variable="T",
                required=True,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _output("result", TypeRef(ValueType.F32)),
        ),
        target_opcodes={"expression": "length"},
    ),
    NodeDef(
        "common.vector.dot",
        "Dot Product",
        (
            _input(
                "a", variable="T", required=True,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _input(
                "b", variable="T", required=True,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _output("result", TypeRef(ValueType.F32)),
        ),
        target_opcodes={"expression": "dot"},
    ),
    NodeDef(
        "common.vector.cross",
        "Cross Product",
        (
            _input(
                "a", variable="T", required=True,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _input(
                "b", variable="T", required=True,
                dimension_policy=PortDimensionPolicy.PROMOTE,
            ),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "cross"},
    ),
    NodeDef(
        "common.space.transform_position",
        "Transform Position",
        (
            _input(
                "input",
                TypeRef(ValueType.VEC3),
                default=[0.0, 0.0, 0.0],
                required=False,
                dimension_policy=PortDimensionPolicy.FIXED,
                display_name="Position",
            ),
            _output(
                "value", variable="SpatialOutput", type_property="target_space"
            ),
        ),
        (
            PropertyDef(
                "target_space",
                TypeRef(ValueType.STRING),
                CoordinateSpace.WORLD.value,
                tuple(
                    (label, space.value)
                    for label, space in (
                        ("Emitter Local", CoordinateSpace.EMITTER_LOCAL),
                        ("Simulation", CoordinateSpace.SIMULATION),
                        ("World", CoordinateSpace.WORLD),
                    )
                ),
            ),
        ),
        target_opcodes={"expression": "convert_space_position"},
    ),
    NodeDef(
        "common.space.transform_direction",
        "Transform Direction",
        (
            _input(
                "input",
                TypeRef(ValueType.VEC3),
                default=[0.0, 1.0, 0.0],
                required=False,
                dimension_policy=PortDimensionPolicy.FIXED,
                display_name="Direction",
            ),
            _output(
                "value", variable="SpatialOutput", type_property="target_space"
            ),
        ),
        (
            PropertyDef(
                "target_space",
                TypeRef(ValueType.STRING),
                CoordinateSpace.WORLD.value,
                tuple(
                    (label, space.value)
                    for label, space in (
                        ("Emitter Local", CoordinateSpace.EMITTER_LOCAL),
                        ("Simulation", CoordinateSpace.SIMULATION),
                        ("World", CoordinateSpace.WORLD),
                    )
                ),
            ),
        ),
        target_opcodes={"expression": "convert_space_direction"},
    ),
    NodeDef(
        "common.vector.compose2",
        "Vector 2",
        (
            _input(
                "x",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
                display_name="X",
            ),
            _input(
                "y",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
                display_name="Y",
            ),
            _output("value", TypeRef(ValueType.VEC2)),
        ),
        target_opcodes={"expression": "compose_vec2"},
    ),
    NodeDef(
        "common.vector.compose3",
        "Vector 3",
        (
            _input(
                "x",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
                display_name="X",
            ),
            _input(
                "y",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
                display_name="Y",
            ),
            _input(
                "z",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
                display_name="Z",
            ),
            _output("value", TypeRef(ValueType.VEC3)),
        ),
        target_opcodes={"expression": "compose_vec3"},
    ),
    NodeDef(
        "common.vector.compose4",
        "Vector 4",
        (
            _input(
                "x",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
                display_name="X",
            ),
            _input(
                "y",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
                display_name="Y",
            ),
            _input(
                "z",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
                display_name="Z",
            ),
            _input(
                "w",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
                display_name="W",
            ),
            _output("value", TypeRef(ValueType.VEC4)),
        ),
        target_opcodes={"expression": "compose_vec4"},
    ),
    NodeDef(
        "common.vector.split2",
        "Split Vector 2",
        (
            _input(
                "value",
                TypeRef(ValueType.VEC2),
                required=True,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _output("x", TypeRef(ValueType.F32)),
            _output("y", TypeRef(ValueType.F32)),
        ),
        target_opcodes={"expression": "split_component"},
    ),
    NodeDef(
        "common.vector.split3",
        "Split Vector 3",
        (
            _input(
                "value",
                TypeRef(ValueType.VEC3),
                required=True,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _output("x", TypeRef(ValueType.F32)),
            _output("y", TypeRef(ValueType.F32)),
            _output("z", TypeRef(ValueType.F32)),
        ),
        target_opcodes={"expression": "split_component"},
    ),
    NodeDef(
        "common.vector.split4",
        "Split Vector 4",
        (
            _input(
                "value",
                TypeRef(ValueType.VEC4),
                required=True,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _output("x", TypeRef(ValueType.F32)),
            _output("y", TypeRef(ValueType.F32)),
            _output("z", TypeRef(ValueType.F32)),
            _output("w", TypeRef(ValueType.F32)),
        ),
        target_opcodes={"expression": "split_component"},
    ),
    NodeDef(
        "common.random.f32",
        "Random",
        (
            _input(
                "minimum",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _input(
                "maximum",
                TypeRef(ValueType.F32),
                default=1.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _input("seed", TypeRef(ValueType.U32), default=0),
            _output("value", TypeRef(ValueType.F32)),
        ),
        target_opcodes={"expression": "random_f32"},
    ),
    NodeDef(
        "common.noise.value3d",
        "Value Noise 3D",
        (
            _input(
                "position",
                TypeRef(ValueType.VEC3),
                variable="P",
                required=True,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _input(
                "frequency",
                TypeRef(ValueType.F32),
                default=1.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _input("seed", TypeRef(ValueType.U32), default=0),
            _output("value", TypeRef(ValueType.F32)),
        ),
        target_opcodes={"expression": "value_noise_3d"},
    ),
    NodeDef(
        "common.noise.vector3d",
        "Vector Noise 3D",
        (
            _input(
                "position",
                TypeRef(ValueType.VEC3),
                variable="P",
                required=True,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _input(
                "frequency",
                TypeRef(ValueType.F32),
                default=1.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _input("seed", TypeRef(ValueType.U32), default=0),
            _output("value", variable="P"),
        ),
        target_opcodes={"expression": "vector_noise_3d"},
    ),
    NodeDef(
        "common.curve.sample",
        "Sample Curve",
        (
            _input(
                "curve",
                TypeRef(ValueType.CURVE),
                default=Curve().to_dict(),
            ),
            _input(
                "t",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _output("value", TypeRef(ValueType.F32)),
        ),
        target_opcodes={"expression": "sample_curve"},
    ),
    NodeDef(
        "common.gradient.sample",
        "Sample Gradient",
        (
            _input(
                "gradient",
                TypeRef(ValueType.GRADIENT),
                default=Gradient().to_dict(),
            ),
            _input(
                "t",
                TypeRef(ValueType.F32),
                default=0.0,
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _output("color", TypeRef(ValueType.COLOR)),
        ),
        target_opcodes={"expression": "sample_gradient"},
    ),
    NodeDef(
        "common.texture.sample2d",
        "Sample Texture 2D",
        (
            _input("texture", TypeRef(ValueType.TEXTURE2D), required=True),
            _input(
                "uv",
                TypeRef(ValueType.VEC2),
                default=[0.0, 0.0],
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
            _output("color", TypeRef(ValueType.COLOR)),
        ),
        target_opcodes={"expression": "sample_texture2d"},
    ),
)

for _definition in COMMON_NODE_DEFINITIONS:
    COMMON_NODE_REGISTRY.register(_definition)


__all__ = ["COMMON_NODE_DEFINITIONS"]
