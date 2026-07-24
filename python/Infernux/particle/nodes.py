"""Particle stage roots and initial domain operation definitions."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from types import MappingProxyType
from typing import Mapping

from Infernux.graph.registry import (
    COMMON_NODE_REGISTRY,
    NodeDef,
    NodeDefinitionRegistry,
    PortDef,
    PortDirection,
    PortKind,
    PropertyDef,
)
from Infernux.graph.types import AssetReference, CoordinateSpace, TypeRef, ValueType


_EVENT_OUTPUT_PREFIX = "particle.event.output"
_EVENT_PAYLOAD_PREFIX = "particle.event.payload"


def particle_event_payload_port_id(field_stable_id: str) -> str:
    digest = hashlib.sha256(str(field_stable_id).encode("utf-8")).hexdigest()
    return f"payload.{digest}"


def particle_event_output_type_id(route_stable_id: str, source_stage: str) -> str:
    digest = hashlib.sha256(str(route_stable_id).encode("utf-8")).hexdigest()
    return f"{_EVENT_OUTPUT_PREFIX}.{source_stage}.{digest}"


def particle_event_payload_type_id(route_stable_id: str) -> str:
    digest = hashlib.sha256(str(route_stable_id).encode("utf-8")).hexdigest()
    return f"{_EVENT_PAYLOAD_PREFIX}.{digest}"


@dataclass(frozen=True)
class ParticleGraphNodeDefinitionSet:
    registry: NodeDefinitionRegistry
    event_route_by_type_id: Mapping[str, str]
    event_source_by_type_id: Mapping[str, tuple[str, str]]
    event_input_route_by_type_id: Mapping[str, str]
    event_target_by_type_id: Mapping[str, str]
    event_field_by_port: Mapping[tuple[str, str], str]
    abi_fingerprint: str


def particle_graph_node_definitions(asset) -> ParticleGraphNodeDefinitionSet:
    """Build immutable asset-local metadata for event-schema-derived nodes."""

    registry = NodeDefinitionRegistry()
    for definition in COMMON_NODE_REGISTRY.definitions():
        registry.register(definition)

    event_types = {event_type.stable_id: event_type for event_type in asset.event_types}
    emitter_names = {emitter.stable_id: emitter.name for emitter in asset.emitters}
    route_by_type_id = {}
    source_by_type_id = {}
    input_route_by_type_id = {}
    target_by_type_id = {}
    field_by_port = {}
    for route in asset.event_routes:
        event_type = event_types[route.event_type_id]
        type_id = particle_event_output_type_id(route.stable_id, route.source_stage)
        if type_id in route_by_type_id:
            raise ValueError("particle event output node identity collision")
        route_by_type_id[type_id] = route.stable_id
        source_by_type_id[type_id] = (
            route.source_emitter_id,
            route.source_stage,
        )
        registry.register(
            NodeDef(
                type_id,
                f"Event Output: {event_type.name} -> "
                f"{emitter_names.get(route.target_emitter_id, route.target_emitter_id)}",
                (
                    _stream("in", PortDirection.INPUT),
                    _stream("out", PortDirection.OUTPUT),
                    PortDef(
                        "condition",
                        PortDirection.INPUT,
                        value_type=TypeRef(ValueType.BOOL),
                        required=False,
                        default=True,
                    ),
                    *(
                        PortDef(
                            particle_event_payload_port_id(field.stable_id),
                            PortDirection.INPUT,
                            value_type=field.value_type,
                            required=False,
                            default=field.default,
                            display_name=field.name,
                        )
                        for field in event_type.fields
                    ),
                ),
                (),
                {"particle_hir": "event.emit"},
            )
        )
        if event_type.fields:
            payload_type_id = particle_event_payload_type_id(route.stable_id)
            if payload_type_id in input_route_by_type_id:
                raise ValueError("particle event payload node identity collision")
            input_route_by_type_id[payload_type_id] = route.stable_id
            target_by_type_id[payload_type_id] = route.target_emitter_id
            payload_ports = tuple(
                PortDef(
                    particle_event_payload_port_id(field.stable_id),
                    PortDirection.OUTPUT,
                    value_type=field.value_type,
                    display_name=field.name,
                )
                for field in event_type.fields
            )
            for field, port in zip(event_type.fields, payload_ports):
                field_by_port[(payload_type_id, port.id)] = field.stable_id
            registry.register(
                NodeDef(
                    payload_type_id,
                    f"Event Payload: {event_type.name} <- "
                    f"{emitter_names.get(route.source_emitter_id, route.source_emitter_id)}",
                    payload_ports,
                    (),
                    {"expression": "event_payload"},
                )
            )
    abi_payload = {
        "event_types": [event_type.to_dict() for event_type in asset.event_types],
        "event_routes": [route.to_dict() for route in asset.event_routes],
    }
    encoded = json.dumps(
        abi_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return ParticleGraphNodeDefinitionSet(
        registry,
        MappingProxyType(route_by_type_id),
        MappingProxyType(source_by_type_id),
        MappingProxyType(input_route_by_type_id),
        MappingProxyType(target_by_type_id),
        MappingProxyType(field_by_port),
        hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


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
        "particle.attribute.set_scale",
        "Set Scale 3D",
        "attribute.set_scale",
        (PropertyDef("value", TypeRef(ValueType.VEC3), [1.0, 1.0, 1.0]),),
    ),
    _operation(
        "particle.attribute.set_strip_id",
        "Set Strip ID",
        "attribute.set_strip_id",
        (PropertyDef("value", TypeRef(ValueType.U32), 0),),
    ),
    _operation(
        "particle.attribute.set_ribbon_order",
        "Set Ribbon Order",
        "attribute.set_ribbon_order",
        (PropertyDef("value", TypeRef(ValueType.U32), 0),),
    ),
    _operation(
        "particle.attribute.set_ribbon_break",
        "Break Ribbon",
        "attribute.set_ribbon_break",
        (PropertyDef("value", TypeRef(ValueType.BOOL), True),),
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
        "particle.update.collide_plane",
        "Plane Collision",
        "collision.plane",
        (
            PropertyDef(
                "point",
                TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
                [0.0, 0.0, 0.0],
            ),
            PropertyDef(
                "normal",
                TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
                [0.0, 1.0, 0.0],
            ),
            PropertyDef("radius", TypeRef(ValueType.F32), 0.0),
            PropertyDef("restitution", TypeRef(ValueType.F32), 0.5),
            PropertyDef("friction", TypeRef(ValueType.F32), 0.1),
        ),
    ),
    _operation(
        "particle.update.collide_sphere",
        "Sphere Collision",
        "collision.sphere",
        (
            PropertyDef(
                "center",
                TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
                [0.0, 0.0, 0.0],
            ),
            PropertyDef("sphere_radius", TypeRef(ValueType.F32), 1.0),
            PropertyDef("particle_radius", TypeRef(ValueType.F32), 0.0),
            PropertyDef("restitution", TypeRef(ValueType.F32), 0.5),
            PropertyDef("friction", TypeRef(ValueType.F32), 0.1),
        ),
    ),
    _operation(
        "particle.update.collide_sdf",
        "SDF Collision",
        "collision.sdf",
        (
            PropertyDef("interface", TypeRef(ValueType.STRING), ""),
            PropertyDef("particle_radius", TypeRef(ValueType.F32), 0.0),
            PropertyDef("restitution", TypeRef(ValueType.F32), 0.5),
            PropertyDef("friction", TypeRef(ValueType.F32), 0.1),
            PropertyDef("inverted", TypeRef(ValueType.BOOL), False),
        ),
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
            PropertyDef("cast_shadows", TypeRef(ValueType.BOOL), False),
            PropertyDef("sort", TypeRef(ValueType.STRING), "none"),
        ),
        {"particle_hir": "render.mesh"},
    ),
    NodeDef(
        "particle.output.ribbon",
        "Ribbon Output",
        (_stream("in", PortDirection.INPUT),),
        (
            PropertyDef("material", TypeRef(ValueType.ASSET_REF), AssetReference().to_dict()),
            PropertyDef("receive_scene_lighting", TypeRef(ValueType.BOOL), False),
            PropertyDef("receive_shadows", TypeRef(ValueType.BOOL), False),
            PropertyDef("soft_particles", TypeRef(ValueType.BOOL), False),
            PropertyDef("soft_distance", TypeRef(ValueType.F32), 1.0),
            PropertyDef("sort", TypeRef(ValueType.STRING), "none"),
            PropertyDef("uv_mode", TypeRef(ValueType.STRING), "stretch"),
            PropertyDef("uv_scale", TypeRef(ValueType.F32), 1.0),
        ),
        {"particle_hir": "render.ribbon"},
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
        "particle.attribute.read_bool",
        "Read Bool Attribute",
        TypeRef(ValueType.BOOL),
        "builtin.ribbon_break",
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


__all__ = [
    "PARTICLE_NODE_DEFINITIONS",
    "ParticleGraphNodeDefinitionSet",
    "particle_event_output_type_id",
    "particle_event_payload_port_id",
    "particle_event_payload_type_id",
    "particle_graph_node_definitions",
]
