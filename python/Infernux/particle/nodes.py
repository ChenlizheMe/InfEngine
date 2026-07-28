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

ATTRIBUTE_COMPOSITION_CHOICES = (
    ("Set", "set"),
    ("Add", "add"),
    ("Multiply", "multiply"),
)

ATTRIBUTE_NODE_NAMES = MappingProxyType(
    {
        "particle.attribute.position": "Position",
        "particle.attribute.velocity": "Velocity",
        "particle.attribute.lifetime": "Lifetime",
        "particle.attribute.flipbook_frame": "Flipbook Frame",
        "particle.attribute.color": "Color",
        "particle.attribute.size": "Size",
        "particle.attribute.scale": "Scale 3D",
        "particle.attribute.strip_id": "Strip ID",
        "particle.attribute.ribbon_order": "Ribbon Order",
        "particle.attribute.ribbon_break": "Ribbon Break",
        "particle.attribute.rotation": "Rotation",
        "particle.attribute.orientation": "Orientation",
        "particle.attribute.cache": "Attribute Cache",
    }
)

ATTRIBUTE_OPERATION_SPECS = MappingProxyType(
    {
        "attribute.modify_position": ("builtin.position", "value", False),
        "attribute.modify_velocity": ("builtin.velocity", "value", False),
        "attribute.modify_lifetime": ("builtin.lifetime", "value", False),
        "attribute.modify_flipbook_frame": ("builtin.flipbook_frame", "value", False),
        "attribute.modify_color": ("builtin.color", "value", False),
        "attribute.modify_size": ("builtin.size", "value", False),
        "attribute.modify_scale": ("builtin.scale", "value", False),
        "attribute.modify_strip_id": ("builtin.ribbon_strip_id", "value", False),
        "attribute.modify_ribbon_order": ("builtin.ribbon_order", "value", False),
        "attribute.modify_ribbon_break": ("builtin.ribbon_break", "value", False),
        "attribute.modify_rotation": ("builtin.rotation", "value", False),
        "attribute.modify_orientation": ("builtin.orientation", "degrees", True),
    }
)


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
    parameter_type_by_id: Mapping[str, TypeRef]
    parameter_by_id: Mapping[str, object]
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
                    _exec("in", PortDirection.INPUT),
                    _exec("out", PortDirection.OUTPUT),
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
        "parameters": [
            {
                "stable_id": parameter.stable_id,
                "type": parameter.value_type.to_dict(),
            }
            for parameter in asset.parameters
        ],
        "event_types": [event_type.to_dict() for event_type in asset.event_types],
        "event_routes": [route.to_dict() for route in asset.event_routes],
        "emitter_attributes": {
            emitter.stable_id: [
                attribute.to_dict()
                for attribute in emitter.attributes
            ]
            for emitter in asset.emitters
        },
    }
    encoded = json.dumps(
        abi_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return ParticleGraphNodeDefinitionSet(
        registry,
        MappingProxyType(
            {
                parameter.stable_id: parameter.value_type
                for parameter in asset.parameters
            }
        ),
        MappingProxyType(
            {parameter.stable_id: parameter for parameter in asset.parameters}
        ),
        MappingProxyType(route_by_type_id),
        MappingProxyType(source_by_type_id),
        MappingProxyType(input_route_by_type_id),
        MappingProxyType(target_by_type_id),
        MappingProxyType(field_by_port),
        hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    )


def _exec(port_id: str, direction: PortDirection) -> PortDef:
    return PortDef(port_id, direction, kind=PortKind.EXEC)


def _operation(type_id: str, label: str, opcode: str, properties=()) -> NodeDef:
    properties = tuple(properties)
    return NodeDef(
        type_id,
        label,
        (
            _exec("in", PortDirection.INPUT),
            _exec("out", PortDirection.OUTPUT),
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


def _attribute_operation(
    type_id: str,
    attribute_name: str,
    opcode: str,
    value_type: TypeRef,
    default,
    *,
    property_id: str = "value",
    composable: bool = True,
) -> NodeDef:
    properties = (
        PropertyDef(
            "composition",
            TypeRef(ValueType.STRING),
            "set",
            ATTRIBUTE_COMPOSITION_CHOICES,
        ),
    ) if composable else ()
    return NodeDef(
        type_id,
        f"Set {attribute_name}",
        (
            _exec("in", PortDirection.INPUT),
            _exec("out", PortDirection.OUTPUT),
            PortDef(
                property_id,
                PortDirection.INPUT,
                value_type=value_type,
                required=False,
                default=default,
            ),
        ),
        properties,
        {"particle_hir": opcode},
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


def _get_attribute() -> NodeDef:
    return NodeDef(
        "particle.attribute.get",
        "Get Attribute",
        (
            _exec("in", PortDirection.INPUT),
            PortDef(
                "value",
                PortDirection.OUTPUT,
                type_variable="AttributeType",
                type_property="attribute",
            ),
            _exec("out", PortDirection.OUTPUT),
        ),
        (PropertyDef("attribute", TypeRef(ValueType.STRING), "builtin.position"),),
        {
            "expression": "load_attribute",
            "particle_hir": "attribute.capture",
        },
    )


def _attribute_cache_operation() -> NodeDef:
    return NodeDef(
        "particle.attribute.cache",
        "Attribute Cache",
        (
            _exec("in", PortDirection.INPUT),
            _exec("out", PortDirection.OUTPUT),
            PortDef(
                "value",
                PortDirection.INPUT,
                type_variable="AttributeType",
                type_property="value_type",
                required=False,
                default=0.0,
            ),
        ),
        (
            PropertyDef("name", TypeRef(ValueType.STRING), "Attribute Cache"),
            PropertyDef("value_type", TypeRef(ValueType.STRING), "f32"),
            PropertyDef("value_space", TypeRef(ValueType.STRING), "none"),
            PropertyDef(
                "composition",
                TypeRef(ValueType.STRING),
                "set",
                ATTRIBUTE_COMPOSITION_CHOICES,
            ),
        ),
        {"particle_hir": "attribute.modify_cache"},
    )


def _get_parameter() -> NodeDef:
    return NodeDef(
        "particle.parameter.get",
        "Get Parameter",
        (
            PortDef(
                "value",
                PortDirection.OUTPUT,
                type_variable="ParameterType",
                type_property="parameter",
            ),
        ),
        (PropertyDef("parameter", TypeRef(ValueType.STRING), ""),),
        {"expression": "load_parameter"},
    )


PARTICLE_NODE_DEFINITIONS = (
    NodeDef(
        "particle.root.init",
        "Initialize",
        (_exec("out", PortDirection.OUTPUT),),
        target_opcodes={"particle_hir": "stage.init"},
    ),
    NodeDef(
        "particle.root.update",
        "Update",
        (_exec("out", PortDirection.OUTPUT),),
        target_opcodes={"particle_hir": "stage.update"},
    ),
    NodeDef(
        "particle.root.rendering",
        "Output",
        (_exec("out", PortDirection.OUTPUT),),
        target_opcodes={"particle_hir": "stage.rendering"},
    ),
    NodeDef(
        "particle.root.collision_enter",
        "Collision Enter",
        (_exec("out", PortDirection.OUTPUT),),
        target_opcodes={"particle_hir": "stage.collision_enter"},
    ),
    NodeDef(
        "particle.root.collision_stay",
        "Collision Stay",
        (_exec("out", PortDirection.OUTPUT),),
        target_opcodes={"particle_hir": "stage.collision_stay"},
    ),
    NodeDef(
        "particle.root.collision_exit",
        "Collision Exit",
        (_exec("out", PortDirection.OUTPUT),),
        target_opcodes={"particle_hir": "stage.collision_exit"},
    ),
    NodeDef(
        "particle.control.if",
        "If",
        (
            _exec("in", PortDirection.INPUT),
            PortDef(
                "condition",
                PortDirection.INPUT,
                value_type=TypeRef(ValueType.BOOL),
                required=False,
                default=False,
                display_name="Condition",
            ),
            PortDef(
                "true",
                PortDirection.OUTPUT,
                kind=PortKind.EXEC,
                display_name="True",
            ),
            PortDef(
                "false",
                PortDirection.OUTPUT,
                kind=PortKind.EXEC,
                display_name="False",
            ),
        ),
        target_opcodes={"particle_hir": "control.if"},
    ),
    NodeDef(
        "particle.control.wait_frames",
        "Wait For Frames",
        (
            _exec("in", PortDirection.INPUT),
            PortDef(
                "frames",
                PortDirection.INPUT,
                value_type=TypeRef(ValueType.I32),
                required=False,
                default=1,
                display_name="Frames",
            ),
            _exec("out", PortDirection.OUTPUT),
        ),
        target_opcodes={"particle_hir": "control.wait_frames"},
    ),
    NodeDef(
        "particle.control.wait_seconds",
        "Wait For Seconds",
        (
            _exec("in", PortDirection.INPUT),
            PortDef(
                "seconds",
                PortDirection.INPUT,
                value_type=TypeRef(ValueType.F32),
                required=False,
                default=0.1,
                display_name="Seconds",
            ),
            _exec("out", PortDirection.OUTPUT),
        ),
        target_opcodes={"particle_hir": "control.wait_seconds"},
    ),
    NodeDef(
        "particle.control.until_frames",
        "Until Frames",
        (
            _exec("in", PortDirection.INPUT),
            PortDef(
                "frames",
                PortDirection.INPUT,
                value_type=TypeRef(ValueType.I32),
                required=False,
                default=1,
                display_name="Frames",
            ),
            _exec("out", PortDirection.OUTPUT),
        ),
        target_opcodes={"particle_hir": "control.until_frames"},
    ),
    NodeDef(
        "particle.control.until_seconds",
        "Until Seconds",
        (
            _exec("in", PortDirection.INPUT),
            PortDef(
                "seconds",
                PortDirection.INPUT,
                value_type=TypeRef(ValueType.F32),
                required=False,
                default=0.1,
                display_name="Seconds",
            ),
            _exec("out", PortDirection.OUTPUT),
        ),
        target_opcodes={"particle_hir": "control.until_seconds"},
    ),
    NodeDef(
        "particle.control.join_all",
        "Join All",
        tuple(
            PortDef(
                f"in{index}",
                PortDirection.INPUT,
                kind=PortKind.EXEC,
                display_name=f"In {index + 1}",
            )
            for index in range(4)
        )
        + (_exec("out", PortDirection.OUTPUT),),
        target_opcodes={"particle_hir": "control.join_all"},
    ),
    _attribute_cache_operation(),
    _attribute_operation(
        "particle.attribute.position",
        "Position",
        "attribute.modify_position",
        TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
        [0.0, 0.0, 0.0],
    ),
    _attribute_operation(
        "particle.attribute.velocity",
        "Velocity",
        "attribute.modify_velocity",
        TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
        [0.0, 1.0, 0.0],
    ),
    _attribute_operation(
        "particle.attribute.lifetime",
        "Lifetime",
        "attribute.modify_lifetime",
        TypeRef(ValueType.F32),
        5.0,
    ),
    _attribute_operation(
        "particle.attribute.flipbook_frame",
        "Flipbook Frame",
        "attribute.modify_flipbook_frame",
        TypeRef(ValueType.F32),
        0.0,
    ),
    _attribute_operation(
        "particle.attribute.color",
        "Color",
        "attribute.modify_color",
        TypeRef(ValueType.COLOR),
        [1.0, 1.0, 1.0, 1.0],
    ),
    _attribute_operation(
        "particle.attribute.size",
        "Size",
        "attribute.modify_size",
        TypeRef(ValueType.F32),
        1.0,
    ),
    _attribute_operation(
        "particle.attribute.scale",
        "Scale 3D",
        "attribute.modify_scale",
        TypeRef(ValueType.VEC3),
        [1.0, 1.0, 1.0],
    ),
    _attribute_operation(
        "particle.attribute.strip_id",
        "Strip ID",
        "attribute.modify_strip_id",
        TypeRef(ValueType.U32),
        0,
    ),
    _attribute_operation(
        "particle.attribute.ribbon_order",
        "Ribbon Order",
        "attribute.modify_ribbon_order",
        TypeRef(ValueType.U32),
        0,
    ),
    _attribute_operation(
        "particle.attribute.ribbon_break",
        "Ribbon Break",
        "attribute.modify_ribbon_break",
        TypeRef(ValueType.BOOL),
        True,
        composable=False,
    ),
    _attribute_operation(
        "particle.attribute.rotation",
        "Rotation",
        "attribute.modify_rotation",
        TypeRef(ValueType.F32),
        0.0,
    ),
    _attribute_operation(
        "particle.attribute.orientation",
        "Orientation",
        "attribute.modify_orientation",
        TypeRef(ValueType.VEC3),
        [0.0, 0.0, 0.0],
        property_id="degrees",
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
        (_exec("in", PortDirection.INPUT),),
        (
            PropertyDef("material", TypeRef(ValueType.ASSET_REF), AssetReference().to_dict()),
            PropertyDef("receive_scene_lighting", TypeRef(ValueType.BOOL), False),
            PropertyDef("receive_shadows", TypeRef(ValueType.BOOL), False),
            PropertyDef("soft_particles", TypeRef(ValueType.BOOL), False),
            PropertyDef("soft_distance", TypeRef(ValueType.F32), 1.0),
            PropertyDef("sort", TypeRef(ValueType.STRING), "back_to_front"),
            PropertyDef("alignment", TypeRef(ValueType.STRING), "camera_plane"),
            PropertyDef("alignment_axis", TypeRef(ValueType.VEC3), [0.0, 1.0, 0.0]),
            PropertyDef("flipbook_columns", TypeRef(ValueType.U32), 1),
            PropertyDef("flipbook_rows", TypeRef(ValueType.U32), 1),
        ),
        {"particle_hir": "render.sprite"},
    ),
    NodeDef(
        "particle.output.mesh",
        "Static Mesh Output",
        (_exec("in", PortDirection.INPUT),),
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
        (_exec("in", PortDirection.INPUT),),
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
    _vector_field_sample(),
    NodeDef(
        "particle.attribute.normalized_age",
        "Normalized Age",
        (PortDef("value", PortDirection.OUTPUT, value_type=TypeRef(ValueType.F32)),),
        target_opcodes={"expression": "normalized_age"},
    ),
    NodeDef(
        "particle.context.delta_time",
        "Delta Time",
        (PortDef("value", PortDirection.OUTPUT, value_type=TypeRef(ValueType.F32)),),
        target_opcodes={"expression": "delta_time"},
    ),
    _get_attribute(),
    _get_parameter(),
)

for _definition in PARTICLE_NODE_DEFINITIONS:
    COMMON_NODE_REGISTRY.register(_definition)


__all__ = [
    "ATTRIBUTE_COMPOSITION_CHOICES",
    "ATTRIBUTE_NODE_NAMES",
    "ATTRIBUTE_OPERATION_SPECS",
    "PARTICLE_NODE_DEFINITIONS",
    "ParticleGraphNodeDefinitionSet",
    "particle_event_output_type_id",
    "particle_event_payload_port_id",
    "particle_event_payload_type_id",
    "particle_graph_node_definitions",
]
