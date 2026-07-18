"""Particle stage roots and initial domain operation definitions."""

from __future__ import annotations

from Infernux.graph.registry import (
    COMMON_NODE_REGISTRY,
    NodeDef,
    PortDef,
    PortDirection,
    PortKind,
    PropertyDef,
)
from Infernux.graph.types import AssetReference, TypeRef, ValueType


def _stream(port_id: str, direction: PortDirection) -> PortDef:
    return PortDef(port_id, direction, kind=PortKind.STREAM)


def _operation(type_id: str, label: str, opcode: str, properties=()) -> NodeDef:
    properties = tuple(properties)
    return NodeDef(
        type_id,
        label,
        (
            _stream("in", PortDirection.INPUT),
            _stream("out", PortDirection.OUTPUT),
            *(
                PortDef(
                    item.id,
                    PortDirection.INPUT,
                    value_type=item.value_type,
                    required=False,
                    default=item.default,
                )
                for item in properties
            ),
        ),
        properties,
        {"particle_hir": opcode},
    )


PARTICLE_NODE_DEFINITIONS = (
    NodeDef(
        "particle.root.init",
        "Init",
        (_stream("out", PortDirection.OUTPUT),),
        target_opcodes={"particle_hir": "stage.init"},
    ),
    NodeDef(
        "particle.root.update",
        "Update",
        (_stream("out", PortDirection.OUTPUT),),
        target_opcodes={"particle_hir": "stage.update"},
    ),
    NodeDef(
        "particle.root.rendering",
        "Rendering",
        (_stream("out", PortDirection.OUTPUT),),
        target_opcodes={"particle_hir": "stage.rendering"},
    ),
    _operation(
        "particle.init.set_velocity",
        "Set Velocity",
        "attribute.set_velocity",
        (PropertyDef("value", TypeRef(ValueType.VEC3), [0.0, 1.0, 0.0]),),
    ),
    _operation(
        "particle.init.set_lifetime",
        "Set Lifetime",
        "attribute.set_lifetime",
        (PropertyDef("value", TypeRef(ValueType.F32), 5.0),),
    ),
    _operation(
        "particle.update.acceleration",
        "Acceleration",
        "integrate.acceleration",
        (PropertyDef("value", TypeRef(ValueType.VEC3), [0.0, -9.81, 0.0]),),
    ),
    NodeDef(
        "particle.output.sprite",
        "Sprite Output",
        (_stream("in", PortDirection.INPUT),),
        (
            PropertyDef("material", TypeRef(ValueType.ASSET_REF), AssetReference().to_dict()),
            PropertyDef("receive_scene_lighting", TypeRef(ValueType.BOOL), False),
            PropertyDef("receive_shadows", TypeRef(ValueType.BOOL), False),
            PropertyDef("sort", TypeRef(ValueType.STRING), "back_to_front"),
        ),
        {"particle_hir": "render.sprite"},
    ),
)

for _definition in PARTICLE_NODE_DEFINITIONS:
    COMMON_NODE_REGISTRY.register(_definition)


__all__ = ["PARTICLE_NODE_DEFINITIONS"]
