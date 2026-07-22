"""Common expression nodes usable by ParticleGraph and future graph targets."""

from __future__ import annotations

from .registry import (
    COMMON_NODE_REGISTRY,
    NodeDef,
    PortDef,
    PortDirection,
    PropertyDef,
)
from .types import TypeRef, ValueType
from .ramp import Curve, Gradient


def _input(port_id: str, value_type=None, *, variable="", default=None) -> PortDef:
    return PortDef(
        port_id,
        PortDirection.INPUT,
        value_type=value_type,
        type_variable=variable,
        required=False,
        default=default,
    )


def _output(port_id: str, value_type=None, *, variable="") -> PortDef:
    return PortDef(
        port_id,
        PortDirection.OUTPUT,
        value_type=value_type,
        type_variable=variable,
    )


COMMON_NODE_DEFINITIONS = (
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
        "common.constant.vec3",
        "Vector 3",
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
            _input("a", variable="T", default=0.0),
            _input("b", variable="T", default=0.0),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "add"},
    ),
    NodeDef(
        "common.math.subtract",
        "Subtract",
        (
            _input("a", variable="T", default=0.0),
            _input("b", variable="T", default=0.0),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "subtract"},
    ),
    NodeDef(
        "common.math.multiply",
        "Multiply",
        (
            _input("a", variable="T", default=0.0),
            _input("b", variable="T", default=0.0),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "multiply"},
    ),
    NodeDef(
        "common.math.divide",
        "Divide",
        (
            _input("a", variable="T", default=0.0),
            _input("b", variable="T", default=1.0),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "divide"},
    ),
    NodeDef(
        "common.math.lerp",
        "Lerp",
        (
            _input("a", variable="T", default=0.0),
            _input("b", variable="T", default=1.0),
            _input("t", TypeRef(ValueType.F32), default=0.5),
            _output("result", variable="T"),
        ),
        target_opcodes={"expression": "lerp"},
    ),
    NodeDef(
        "common.vector.normalize",
        "Normalize",
        (
            _input("value", TypeRef(ValueType.VEC3), default=[0.0, 0.0, 0.0]),
            _output("result", TypeRef(ValueType.VEC3)),
        ),
        target_opcodes={"expression": "normalize"},
    ),
    NodeDef(
        "common.random.f32",
        "Random",
        (
            _input("minimum", TypeRef(ValueType.F32), default=0.0),
            _input("maximum", TypeRef(ValueType.F32), default=1.0),
            _input("seed", TypeRef(ValueType.U32), default=0),
            _output("value", TypeRef(ValueType.F32)),
        ),
        target_opcodes={"expression": "random_f32"},
    ),
    NodeDef(
        "common.curve.sample",
        "Sample Curve",
        (
            _input("t", TypeRef(ValueType.F32), default=0.0),
            _output("value", TypeRef(ValueType.F32)),
        ),
        (PropertyDef("curve", TypeRef(ValueType.CURVE), Curve().to_dict()),),
        {"expression": "sample_curve"},
    ),
    NodeDef(
        "common.gradient.sample",
        "Sample Gradient",
        (
            _input("t", TypeRef(ValueType.F32), default=0.0),
            _output("color", TypeRef(ValueType.COLOR)),
        ),
        (PropertyDef("gradient", TypeRef(ValueType.GRADIENT), Gradient().to_dict()),),
        {"expression": "sample_gradient"},
    ),
)

for _definition in COMMON_NODE_DEFINITIONS:
    COMMON_NODE_REGISTRY.register(_definition)


__all__ = ["COMMON_NODE_DEFINITIONS"]
