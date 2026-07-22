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
from Infernux.graph.types import AssetReference, CoordinateSpace, TypeRef, ValueType


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


def _point_cache_sample(
    type_id: str,
    label: str,
    result_type: TypeRef,
    *,
    channel: str,
    semantic: str = "raw",
) -> NodeDef:
    return NodeDef(
        type_id,
        label,
        (
            PortDef(
                "index",
                PortDirection.INPUT,
                value_type=TypeRef(ValueType.U32),
                required=False,
                default=0,
            ),
            PortDef("value", PortDirection.OUTPUT, value_type=result_type),
        ),
        (
            PropertyDef("interface", TypeRef(ValueType.STRING), ""),
            PropertyDef("channel", TypeRef(ValueType.STRING), channel),
            PropertyDef("lookup", TypeRef(ValueType.STRING), "index"),
            PropertyDef("semantic", TypeRef(ValueType.STRING), semantic),
        ),
        {"expression": "sample_point_cache"},
    )


def _vector_field_sample() -> NodeDef:
    return NodeDef(
        "particle.vector_field.sample",
        "Sample Vector Field",
        (
            PortDef(
                "position",
                PortDirection.INPUT,
                value_type=TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
                required=True,
            ),
            PortDef(
                "value",
                PortDirection.OUTPUT,
                value_type=TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
            ),
        ),
        (PropertyDef("interface", TypeRef(ValueType.STRING), ""),),
        {"expression": "sample_vector_field"},
    )


def _attribute_read(type_id: str, label: str, result_type: TypeRef, attribute: str) -> NodeDef:
    return NodeDef(
        type_id,
        label,
        (PortDef("value", PortDirection.OUTPUT, value_type=result_type),),
        (PropertyDef("attribute", TypeRef(ValueType.STRING), attribute),),
        {"expression": "load_attribute"},
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
        "particle.attribute.set_color",
        "Set Color",
        "attribute.set_color",
        (PropertyDef("value", TypeRef(ValueType.COLOR), [1.0, 1.0, 1.0, 1.0]),),
    ),
    _operation(
        "particle.attribute.set_size",
        "Set Size",
        "attribute.set_size",
        (PropertyDef("value", TypeRef(ValueType.F32), 1.0),),
    ),
    _operation(
        "particle.attribute.set_rotation",
        "Set Rotation",
        "attribute.set_rotation",
        (PropertyDef("value", TypeRef(ValueType.F32), 0.0),),
    ),
    _operation(
        "particle.update.rotate",
        "Rotate",
        "integrate.angular_velocity",
        (PropertyDef("degrees_per_second", TypeRef(ValueType.F32), 90.0),),
    ),
    _operation(
        "particle.attribute.set_orientation",
        "Set Orientation",
        "attribute.set_orientation",
        (PropertyDef("degrees", TypeRef(ValueType.VEC3), [0.0, 0.0, 0.0]),),
    ),
    _operation(
        "particle.update.rotate_orientation",
        "Angular Velocity 3D",
        "integrate.angular_velocity_3d",
        (
            PropertyDef(
                "degrees_per_second",
                TypeRef(ValueType.VEC3),
                [0.0, 90.0, 0.0],
            ),
        ),
    ),
    _operation(
        "particle.update.acceleration",
        "Acceleration",
        "integrate.acceleration",
        (PropertyDef("value", TypeRef(ValueType.VEC3), [0.0, -9.81, 0.0]),),
    ),
    _operation(
        "particle.update.kill_if",
        "Kill If",
        "lifecycle.kill_if",
        (PropertyDef("condition", TypeRef(ValueType.BOOL), False),),
    ),
    NodeDef(
        "particle.output.sprite",
        "Sprite Output",
        (_stream("in", PortDirection.INPUT),),
        (
            PropertyDef("material", TypeRef(ValueType.ASSET_REF), AssetReference().to_dict()),
            PropertyDef("receive_scene_lighting", TypeRef(ValueType.BOOL), False),
            PropertyDef("receive_shadows", TypeRef(ValueType.BOOL), False),
            PropertyDef("soft_particles", TypeRef(ValueType.BOOL), False),
            PropertyDef("soft_distance", TypeRef(ValueType.F32), 1.0),
            PropertyDef("sort", TypeRef(ValueType.STRING), "back_to_front"),
        ),
        {"particle_hir": "render.sprite"},
    ),
    NodeDef(
        "particle.output.mesh",
        "Static Mesh Output",
        (_stream("in", PortDirection.INPUT),),
        (
            PropertyDef("mesh", TypeRef(ValueType.ASSET_REF), AssetReference().to_dict()),
            PropertyDef("material", TypeRef(ValueType.ASSET_REF), AssetReference().to_dict()),
            PropertyDef("receive_scene_lighting", TypeRef(ValueType.BOOL), False),
            PropertyDef("receive_shadows", TypeRef(ValueType.BOOL), False),
            PropertyDef("sort", TypeRef(ValueType.STRING), "none"),
        ),
        {"particle_hir": "render.mesh"},
    ),
    _point_cache_sample(
        "particle.point_cache.sample_f32",
        "Sample Point Cache Float",
        TypeRef(ValueType.F32),
        channel="value",
    ),
    _point_cache_sample(
        "particle.point_cache.sample_u32",
        "Sample Point Cache UInt",
        TypeRef(ValueType.U32),
        channel="$id",
    ),
    _point_cache_sample(
        "particle.point_cache.sample_vec2",
        "Sample Point Cache Vector 2",
        TypeRef(ValueType.VEC2),
        channel="value",
    ),
    _point_cache_sample(
        "particle.point_cache.sample_vec3_raw",
        "Sample Point Cache Vector 3",
        TypeRef(ValueType.VEC3),
        channel="value",
    ),
    _point_cache_sample(
        "particle.point_cache.sample_vec4",
        "Sample Point Cache Vector 4",
        TypeRef(ValueType.VEC4),
        channel="value",
    ),
    _point_cache_sample(
        "particle.point_cache.sample_color",
        "Sample Point Cache Color",
        TypeRef(ValueType.COLOR),
        channel="$color",
    ),
    _point_cache_sample(
        "particle.point_cache.sample_position",
        "Sample Point Cache Position",
        TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
        channel="$position",
        semantic="position",
    ),
    _point_cache_sample(
        "particle.point_cache.sample_direction",
        "Sample Point Cache Direction",
        TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
        channel="direction",
        semantic="direction",
    ),
    _point_cache_sample(
        "particle.point_cache.sample_normal",
        "Sample Point Cache Normal",
        TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
        channel="$normal",
        semantic="normal",
    ),
    _point_cache_sample(
        "particle.point_cache.sample_vector",
        "Sample Point Cache Vector",
        TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
        channel="velocity",
        semantic="vector",
    ),
    _vector_field_sample(),
    _attribute_read(
        "particle.attribute.read_f32",
        "Read Float Attribute",
        TypeRef(ValueType.F32),
        "builtin.age",
    ),
    _attribute_read(
        "particle.attribute.read_u32",
        "Read UInt Attribute",
        TypeRef(ValueType.U32),
        "builtin.id",
    ),
    _attribute_read(
        "particle.attribute.read_vec3",
        "Read Vector Attribute",
        TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
        "builtin.position",
    ),
    _attribute_read(
        "particle.attribute.read_color",
        "Read Color Attribute",
        TypeRef(ValueType.COLOR),
        "builtin.color",
    ),
)

for _definition in PARTICLE_NODE_DEFINITIONS:
    COMMON_NODE_REGISTRY.register(_definition)


__all__ = ["PARTICLE_NODE_DEFINITIONS"]
