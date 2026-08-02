"""Particle stage roots and initial domain operation definitions."""

from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
from types import MappingProxyType
from typing import Any, Mapping

from Infernux.graph.registry import (
    COMMON_NODE_REGISTRY,
    NodeDef,
    NodeDefinitionRegistry,
    PortDef,
    PortDimensionPolicy,
    PortDirection,
    PortKind,
    PropertyDef,
)
from Infernux.graph.types import AssetReference, CoordinateSpace, TypeRef, ValueType


PARTICLE_EVENT_ACTIVE_TYPE_ID = "particle.event.active"
PARTICLE_EVENT_TRIGGER_TYPE_ID = "particle.event.trigger"
_EVENT_ACTIVE_COMPILE_PREFIX = "internal.particle.event.active"
_EVENT_TRIGGER_COMPILE_PREFIX = "internal.particle.event.trigger"

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


def _event_active_compile_type_id(event_stable_id: str) -> str:
    digest = hashlib.sha256(str(event_stable_id).encode("utf-8")).hexdigest()
    return f"{_EVENT_ACTIVE_COMPILE_PREFIX}.{digest}"


def _event_trigger_compile_type_id(event_stable_id: str) -> str:
    digest = hashlib.sha256(str(event_stable_id).encode("utf-8")).hexdigest()
    return f"{_EVENT_TRIGGER_COMPILE_PREFIX}.{digest}"


@dataclass(frozen=True)
class ParticleGraphNodeDefinitionSet:
    registry: NodeDefinitionRegistry
    parameter_type_by_id: Mapping[str, TypeRef]
    parameter_by_id: Mapping[str, object]
    event_type_by_id: Mapping[str, object]
    event_active_compile_type_by_id: Mapping[str, str]
    event_trigger_compile_type_by_id: Mapping[str, str]
    event_id_by_compile_type: Mapping[str, str]
    event_field_by_port: Mapping[tuple[str, str], str]
    emitter_index_by_id: Mapping[str, int]
    emitter_capacity_by_id: Mapping[str, int]
    output_definition_by_node: Mapping[tuple[str, str], NodeDef]
    output_internal_properties_by_node: Mapping[tuple[str, str], frozenset[str]]
    fragment_shader_choices: tuple[tuple[str, str], ...]
    abi_fingerprint: str


_SHADER_PROPERTY_TYPES = MappingProxyType(
    {
        "Float": TypeRef(ValueType.F32),
        "Float2": TypeRef(ValueType.VEC2),
        "Float3": TypeRef(ValueType.VEC3),
        "Float4": TypeRef(ValueType.VEC4),
        "Color": TypeRef(ValueType.COLOR),
        "Int": TypeRef(ValueType.I32),
        "Mat4": TypeRef(ValueType.MAT4),
        "Texture2D": TypeRef(ValueType.TEXTURE2D),
    }
)


def particle_shader_property_port_id(name: str) -> str:
    return f"shader.{str(name).strip()}"


def _particle_fragment_shader_catalog():
    """Return the imported fragment shader schema used by Particle Outputs."""
    try:
        from Infernux.engine.ui.inspector_shader_utils import (
            _get_shader_properties_cached,
            get_shader_candidates,
        )

        choices = tuple(
            (str(label), str(shader_id))
            for label, shader_id in get_shader_candidates(".frag")
            if str(shader_id)
        )
        properties = {
            shader_id: tuple(_get_shader_properties_cached(shader_id, ".frag"))
            for _label, shader_id in choices
        }
        return choices, properties
    except (ImportError, OSError, RuntimeError, TypeError, ValueError):
        return (("Particle Unlit", "Particle Unlit"),), {"Particle Unlit": ()}


def _shader_property_default(item: Mapping[str, Any], value_type: TypeRef):
    default = item.get("default")
    if value_type.value_type is ValueType.TEXTURE2D:
        token = str(default or "white").strip() or "white"
        return AssetReference(guid=token).to_dict()
    return default


def _particle_output_definition(
    base: NodeDef,
    shader_id: str,
    shader_properties: tuple[Mapping[str, Any], ...],
    compile_type_id: str,
    shader_choices: tuple[tuple[str, str], ...] = (),
) -> NodeDef:
    if shader_id and shader_id not in {value for _label, value in shader_choices}:
        shader_choices = ((f"Missing: {shader_id}", shader_id), *shader_choices)
    ports = list(base.ports)
    for item in shader_properties:
        if bool(item.get("internal", False)):
            continue
        name = str(item.get("name", "")).strip()
        value_type = _SHADER_PROPERTY_TYPES.get(str(item.get("type", "")))
        if not name or value_type is None:
            continue
        ports.append(
            PortDef(
                particle_shader_property_port_id(name),
                PortDirection.INPUT,
                value_type=value_type,
                required=False,
                default=_shader_property_default(item, value_type),
                display_name=name,
                dimension_policy=(
                    PortDimensionPolicy.FIXED
                    if value_type.value_type
                    in {
                        ValueType.F32,
                        ValueType.VEC2,
                        ValueType.VEC3,
                        ValueType.VEC4,
                        ValueType.COLOR,
                    }
                    else PortDimensionPolicy.EXACT
                ),
            )
        )
    return replace(
        base,
        type_id=compile_type_id,
        display_name=f"{base.display_name}: {shader_id or 'None'}",
        ports=tuple(ports),
        properties=tuple(
            PropertyDef("shader", TypeRef(ValueType.STRING), shader_id, shader_choices)
            if item.id == "shader"
            else item
            for item in base.properties
        ),
    )


def particle_graph_node_definitions(asset) -> ParticleGraphNodeDefinitionSet:
    """Build immutable asset-local metadata for event-schema-derived nodes."""

    registry = NodeDefinitionRegistry()
    for definition in COMMON_NODE_REGISTRY.definitions():
        registry.register(definition)

    fragment_shader_choices, fragment_shader_properties = (
        _particle_fragment_shader_catalog()
    )

    emitter_choices = tuple(
        (emitter.name, emitter.stable_id) for emitter in asset.emitters
    )
    registry.register(
        NodeDef(
            "particle.emitter.burst",
            "Burst",
            (
                _exec("in", PortDirection.INPUT),
                PortDef(
                    "count",
                    PortDirection.INPUT,
                    value_type=TypeRef(ValueType.U32),
                    required=False,
                    default=1,
                    display_name="Count",
                ),
                _exec("out", PortDirection.OUTPUT),
            ),
            (
                PropertyDef(
                    "emitter",
                    TypeRef(ValueType.STRING),
                    asset.emitters[0].stable_id,
                    emitter_choices,
                ),
            ),
            {"particle_hir": "emitter.burst"},
        )
    )
    registry.register(
        NodeDef(
            "particle.emitter.playing",
            "Set Emitter Playing",
            (
                _exec("in", PortDirection.INPUT),
                PortDef(
                    "playing",
                    PortDirection.INPUT,
                    value_type=TypeRef(ValueType.BOOL),
                    required=False,
                    default=True,
                    display_name="Playing",
                ),
                _exec("out", PortDirection.OUTPUT),
            ),
            (
                PropertyDef(
                    "emitter",
                    TypeRef(ValueType.STRING),
                    asset.emitters[0].stable_id,
                    emitter_choices,
                ),
            ),
            {"particle_hir": "emitter.set_playing"},
        )
    )

    event_choices = (("None", ""),) + tuple(
        (event_type.name, event_type.stable_id) for event_type in asset.event_types
    )
    event_property = PropertyDef(
        "event",
        TypeRef(ValueType.STRING),
        "",
        event_choices,
    )
    registry.register(
        NodeDef(
            PARTICLE_EVENT_ACTIVE_TYPE_ID,
            "Active Event: None",
            (_exec("out", PortDirection.OUTPUT),),
            (event_property,),
            {"expression": "event_payload", "particle_event_root": True},
        )
    )
    registry.register(
        NodeDef(
            PARTICLE_EVENT_TRIGGER_TYPE_ID,
            "Trigger Event: None",
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
            ),
            (event_property,),
            {"particle_hir": "event.trigger"},
        )
    )

    active_compile_type_by_id = {}
    trigger_compile_type_by_id = {}
    event_id_by_compile_type = {}
    field_by_port = {}
    for event_type in asset.event_types:
        active_type_id = _event_active_compile_type_id(event_type.stable_id)
        trigger_type_id = _event_trigger_compile_type_id(event_type.stable_id)
        if active_type_id in event_id_by_compile_type or trigger_type_id in event_id_by_compile_type:
            raise ValueError("particle event node identity collision")
        active_compile_type_by_id[event_type.stable_id] = active_type_id
        trigger_compile_type_by_id[event_type.stable_id] = trigger_type_id
        event_id_by_compile_type[active_type_id] = event_type.stable_id
        event_id_by_compile_type[trigger_type_id] = event_type.stable_id
        payload_outputs = tuple(
            PortDef(
                particle_event_payload_port_id(field.stable_id),
                PortDirection.OUTPUT,
                value_type=field.value_type,
                display_name=field.name,
            )
            for field in event_type.fields
        )
        for field, port in zip(event_type.fields, payload_outputs):
            field_by_port[(event_type.stable_id, port.id)] = field.stable_id
        registry.register(
            NodeDef(
                active_type_id,
                f"Active Event: {event_type.name}",
                (_exec("out", PortDirection.OUTPUT), *payload_outputs),
                (event_property,),
                {"expression": "event_payload", "particle_event_root": True},
            )
        )
        registry.register(
            NodeDef(
                trigger_type_id,
                f"Trigger Event: {event_type.name}",
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
                (event_property,),
                {"particle_hir": "event.trigger"},
            )
        )
    output_definition_by_node: dict[tuple[str, str], NodeDef] = {}
    output_internal_properties_by_node: dict[
        tuple[str, str], frozenset[str]
    ] = {}
    for emitter in asset.emitters:
        for node in emitter.rendering.nodes:
            if node.type_id not in {
                "particle.output.sprite",
                "particle.output.mesh",
                "particle.output.ribbon",
            }:
                continue
            base = registry.get(node.type_id)
            if base is None:
                continue
            shader_id = str(
                node.properties.get("shader", "Particle Unlit")
            ).strip()
            digest = hashlib.sha256(
                f"{emitter.stable_id}:{node.uid}:{shader_id}".encode("utf-8")
            ).hexdigest()
            shader_properties = fragment_shader_properties.get(shader_id, ())
            definition = _particle_output_definition(
                base,
                shader_id,
                shader_properties,
                f"internal.particle.output.{digest}",
                fragment_shader_choices,
            )
            registry.register(definition)
            output_key = (emitter.stable_id, node.uid)
            output_definition_by_node[output_key] = definition
            output_internal_properties_by_node[output_key] = frozenset(
                particle_shader_property_port_id(str(item.get("name", "")))
                for item in shader_properties
                if bool(item.get("internal", False))
                and str(item.get("name", "")).strip()
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
        "emitter_events": {
            emitter.stable_id: [
                {"flow_id": flow.stable_id, "event_id": flow.event_id}
                for flow in emitter.event_flows
            ]
            for emitter in asset.emitters
        },
        "emitter_attributes": {
            emitter.stable_id: [
                attribute.to_dict()
                for attribute in emitter.attributes
            ]
            for emitter in asset.emitters
        },
        "emitter_order": [emitter.stable_id for emitter in asset.emitters],
        "outputs": {
            f"{emitter_id}:{node_id}": {
                "type_id": definition.type_id,
                "shader": next(
                    (
                        item.default
                        for item in definition.properties
                        if item.id == "shader"
                    ),
                    "",
                ),
                "ports": [
                    {
                        "id": port.id,
                        "type": port.value_type.to_dict(),
                    }
                    for port in definition.ports
                    if port.id.startswith("shader.")
                    and port.value_type is not None
                ],
            }
            for (emitter_id, node_id), definition in output_definition_by_node.items()
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
        MappingProxyType({value.stable_id: value for value in asset.event_types}),
        MappingProxyType(active_compile_type_by_id),
        MappingProxyType(trigger_compile_type_by_id),
        MappingProxyType(event_id_by_compile_type),
        MappingProxyType(field_by_port),
        MappingProxyType(
            {emitter.stable_id: index for index, emitter in enumerate(asset.emitters)}
        ),
        MappingProxyType(
            {emitter.stable_id: emitter.settings.capacity for emitter in asset.emitters}
        ),
        MappingProxyType(output_definition_by_node),
        MappingProxyType(output_internal_properties_by_node),
        fragment_shader_choices,
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
                dimension_policy=PortDimensionPolicy.FIXED,
            ),
        ),
        properties,
        {"particle_hir": opcode},
    )


def _target_position_operation() -> NodeDef:
    properties = (
        PropertyDef(
            "target",
            TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
            [0.0, 0.0, 0.0],
        ),
        PropertyDef("speed", TypeRef(ValueType.F32), 5.0),
        PropertyDef("responsiveness", TypeRef(ValueType.F32), 8.0),
        PropertyDef("arrival_radius", TypeRef(ValueType.F32), 0.5),
    )
    return NodeDef(
        "particle.motion.target_position",
        "Target Position",
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
                    display_name=item.id.replace("_", " ").title(),
                    dimension_policy=PortDimensionPolicy.FIXED,
                )
                for item in properties
            ),
        ),
        properties,
        {"particle_hir": "motion.target_position"},
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


def _sdf_sample_distance() -> NodeDef:
    return NodeDef(
        "particle.sdf.sample_distance",
        "Sample SDF Distance",
        (
            PortDef(
                "position",
                PortDirection.INPUT,
                value_type=TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
                required=True,
            ),
            PortDef(
                "distance",
                PortDirection.OUTPUT,
                value_type=TypeRef(ValueType.F32),
            ),
        ),
        (PropertyDef("interface", TypeRef(ValueType.STRING), ""),),
        {"expression": "sample_sdf_distance"},
    )


def _sdf_sample_gradient() -> NodeDef:
    return NodeDef(
        "particle.sdf.sample_gradient",
        "Sample SDF Gradient",
        (
            PortDef(
                "position",
                PortDirection.INPUT,
                value_type=TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
                required=True,
            ),
            PortDef(
                "gradient",
                PortDirection.OUTPUT,
                value_type=TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
            ),
        ),
        (PropertyDef("interface", TypeRef(ValueType.STRING), ""),),
        {"expression": "sample_sdf_gradient"},
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


def _mesh_sample() -> NodeDef:
    return NodeDef(
        "particle.mesh.sample",
        "Sample Mesh",
        (
            PortDef(
                "mesh",
                PortDirection.INPUT,
                value_type=TypeRef(ValueType.MESH),
                required=False,
                default=AssetReference().to_dict(),
                display_name="Mesh",
            ),
            PortDef(
                "sample",
                PortDirection.INPUT,
                value_type=TypeRef(ValueType.VEC3),
                required=False,
                default=[0.5, 0.5, 0.5],
                display_name="Sample",
            ),
            PortDef(
                "position",
                PortDirection.OUTPUT,
                value_type=TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
                display_name="Position",
            ),
            PortDef(
                "normal",
                PortDirection.OUTPUT,
                value_type=TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION),
                display_name="Normal",
            ),
            PortDef(
                "tangent",
                PortDirection.OUTPUT,
                value_type=TypeRef(ValueType.VEC4, CoordinateSpace.SIMULATION),
                display_name="Tangent",
            ),
            PortDef(
                "uv",
                PortDirection.OUTPUT,
                value_type=TypeRef(ValueType.VEC2),
                display_name="UV",
            ),
            PortDef(
                "barycentric",
                PortDirection.OUTPUT,
                value_type=TypeRef(ValueType.VEC3),
                display_name="Barycentric",
            ),
        ),
        (
            PropertyDef(
                "mode",
                TypeRef(ValueType.STRING),
                "surface",
                (("Vertex", "vertex"), ("Edge", "edge"), ("Surface", "surface")),
            ),
            PropertyDef("seed", TypeRef(ValueType.U32), 0),
        ),
        {"expression": "sample_mesh"},
    )


def particle_event_node_definition(
    definitions: ParticleGraphNodeDefinitionSet,
    type_id: str,
    event_id: str,
) -> NodeDef | None:
    """Resolve the effective ABI-shaped definition of one generic event node."""
    if type_id == PARTICLE_EVENT_ACTIVE_TYPE_ID and event_id:
        type_id = definitions.event_active_compile_type_by_id.get(event_id, type_id)
    elif type_id == PARTICLE_EVENT_TRIGGER_TYPE_ID and event_id:
        type_id = definitions.event_trigger_compile_type_by_id.get(event_id, type_id)
    return definitions.registry.get(type_id)


def specialize_particle_event_document(document, definitions):
    """Create the compiler-only event ABI view without changing the asset graph."""
    nodes = []
    changed = False
    for node in document.nodes:
        type_id = node.type_id
        event_id = str(node.properties.get("event", ""))
        if type_id == PARTICLE_EVENT_ACTIVE_TYPE_ID and event_id:
            type_id = definitions.event_active_compile_type_by_id.get(event_id, type_id)
        elif type_id == PARTICLE_EVENT_TRIGGER_TYPE_ID and event_id:
            type_id = definitions.event_trigger_compile_type_by_id.get(event_id, type_id)
        replacement = replace(node, type_id=type_id)
        nodes.append(replacement)
        changed = changed or replacement != node
    return replace(document, nodes=tuple(nodes)) if changed else document


def particle_output_node_definition(
    definitions: ParticleGraphNodeDefinitionSet,
    emitter_id: str,
    node,
) -> NodeDef | None:
    if node is None:
        return None
    node_uid = str(node.uid).split("::", 1)[-1]
    return definitions.output_definition_by_node.get(
        (str(emitter_id), node_uid)
    ) or definitions.registry.get(node.type_id)


def specialize_particle_output_document(document, definitions, emitter_id: str):
    """Apply output-specific ShaderInfo ports in the compiler-only document."""
    nodes = []
    changed = False
    for node in document.nodes:
        definition = definitions.output_definition_by_node.get(
            (str(emitter_id), str(node.uid))
        )
        if definition is None:
            replacement = node
        else:
            internal_properties = definitions.output_internal_properties_by_node.get(
                (str(emitter_id), str(node.uid)), frozenset()
            )
            properties = {
                key: value
                for key, value in node.properties.items()
                if key not in internal_properties
            }
            replacement = replace(
                node, type_id=definition.type_id, properties=properties
            )
        nodes.append(replacement)
        changed = changed or replacement != node
    return replace(document, nodes=tuple(nodes)) if changed else document


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
        "particle.parameter",
        "Parameter",
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


def _set_parameter() -> NodeDef:
    return NodeDef(
        "particle.parameter.set",
        "Set Parameter",
        (
            _exec("in", PortDirection.INPUT),
            PortDef(
                "value",
                PortDirection.INPUT,
                type_variable="ParameterType",
                type_property="parameter",
                required=False,
                default=0.0,
                display_name="Value",
            ),
            _exec("out", PortDirection.OUTPUT),
        ),
        (PropertyDef("parameter", TypeRef(ValueType.STRING), ""),),
        {"particle_hir": "parameter.store"},
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
    _target_position_operation(),
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
        "particle.collision.plane",
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
        "particle.collision.sphere",
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
        "particle.collision.sdf",
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
        "particle.lifecycle.kill_if",
        "Kill If",
        "lifecycle.kill_if",
        (PropertyDef("condition", TypeRef(ValueType.BOOL), False),),
    ),
    NodeDef(
        "particle.output.sprite",
        "Sprite Output",
        (_exec("in", PortDirection.INPUT),),
        (
            PropertyDef("shader", TypeRef(ValueType.STRING), "Particle Unlit"),
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
        (
            _exec("in", PortDirection.INPUT),
            PortDef(
                "mesh",
                PortDirection.INPUT,
                value_type=TypeRef(ValueType.MESH),
                required=False,
                default=AssetReference().to_dict(),
                display_name="Mesh",
            ),
        ),
        (
            PropertyDef("shader", TypeRef(ValueType.STRING), "Particle Unlit"),
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
            PropertyDef("shader", TypeRef(ValueType.STRING), "Particle Unlit"),
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
    _sdf_sample_distance(),
    _sdf_sample_gradient(),
    _mesh_sample(),
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
    _set_parameter(),
)

for _definition in PARTICLE_NODE_DEFINITIONS:
    COMMON_NODE_REGISTRY.register(_definition)


__all__ = [
    "ATTRIBUTE_COMPOSITION_CHOICES",
    "ATTRIBUTE_NODE_NAMES",
    "ATTRIBUTE_OPERATION_SPECS",
    "PARTICLE_NODE_DEFINITIONS",
    "PARTICLE_EVENT_ACTIVE_TYPE_ID",
    "PARTICLE_EVENT_TRIGGER_TYPE_ID",
    "ParticleGraphNodeDefinitionSet",
    "particle_event_payload_port_id",
    "particle_event_node_definition",
    "particle_graph_node_definitions",
    "particle_output_node_definition",
    "particle_shader_property_port_id",
    "specialize_particle_event_document",
    "specialize_particle_output_document",
]
