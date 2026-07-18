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
        "common.constant.vec3",
        "Vector 3",
        (_output("value", TypeRef(ValueType.VEC3)),),
        (PropertyDef("value", TypeRef(ValueType.VEC3), [0.0, 0.0, 0.0]),),
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
)

for _definition in COMMON_NODE_DEFINITIONS:
    COMMON_NODE_REGISTRY.register(_definition)


__all__ = ["COMMON_NODE_DEFINITIONS"]
