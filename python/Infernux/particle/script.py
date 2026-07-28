"""Public ParticleScript DSL markers and non-executing AST frontend."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from Infernux.graph import (
    GraphDocument,
    GraphLinkRecord,
    GraphNodeRecord,
    GraphSourceLocation,
    PortKind,
)
from Infernux.graph.expression_ir import ExpressionCompileError, ExpressionCompiler
from Infernux.graph.types import AssetReference, CoordinateSpace, TypeRef, ValueType
from Infernux.graph.ramp import Curve, CurveKey, Gradient, GradientKey

from .asset import (
    EmitterSettings,
    EmitterShape,
    ParticleBurst,
    ParticleEmitterAsset,
    ParticleEventField,
    ParticleEventRoute,
    ParticleEventType,
    ParticleGraphAsset,
    ParticleParameter,
    ScalarRange,
    default_stage_graph,
    particle_attribute_cache_id,
    particle_attribute_catalog,
    particle_attribute_zero,
    standard_particle_attributes,
)
from .data_interface import SdfVolume, VectorField
from .hir import ParticleGraphCompiler
from .nodes import (
    particle_event_output_type_id,
    particle_event_payload_port_id,
    particle_event_payload_type_id,
    particle_graph_node_definitions,
)


class ParticleScript:
    """Marker base for one authored ParticleScript asset."""


class ParticleEmitter:
    """Marker base for one emitter declaration nested in ParticleScript."""


@dataclass(frozen=True)
class Parameter:
    """One graph-level parameter declaration for the AST-only frontend."""

    stable_id: str
    name: str
    value_type: str
    default: Any
    exposed: bool = True
    category: str = ""
    tooltip: str = ""


@dataclass(frozen=True)
class EventField:
    """Literal schema declaration consumed by the non-executing AST frontend."""

    stable_id: str
    name: str
    value_type: str
    default: Any
    space: str = "none"


@dataclass(frozen=True)
class EventType:
    """One graph-owned typed event record schema."""

    stable_id: str
    name: str
    capacity_per_step: int
    fields: tuple[EventField, ...] = ()


@dataclass(frozen=True)
class EventRoute:
    """One next-step event route between stable emitter identities."""

    stable_id: str
    event_type_id: str
    source_emitter_id: str
    source_stage: str
    target_emitter_id: str
    spawn_count: int = 1


@dataclass(frozen=True)
class _EventParseContext:
    emitter_id: str
    event_types: dict[str, ParticleEventType]
    event_routes: dict[str, ParticleEventRoute]
    parameters_by_id: dict[str, ParticleParameter]
    parameter_id_by_name: dict[str, str]
    attributes_by_id: dict[str, str]
    attribute_id_by_name: dict[str, str]


class _ParticleStageContext:
    def parameter(self, name_or_stable_id: str): ...
    def sample_texture2d(self, texture, uv): ...
    def sample_vector_field(self, interface: str, position): ...
    def sample_curve(self, curve: Curve, t: float) -> float: ...
    def sample_gradient(self, gradient: Gradient, t: float): ...
    def value_noise_3d(self, position, frequency: float = 1.0, seed: int = 0) -> float: ...
    def vector_noise_3d(self, position, frequency: float = 1.0, seed: int = 0): ...
    def event_payload(self, *, route: str, field: str): ...


class _ResumableParticleStageContext(_ParticleStageContext):
    def wait_frames(self, frames: int) -> None: ...
    def wait_seconds(self, seconds: float) -> None: ...
    def until_frames(self, frames: int) -> None: ...
    def until_seconds(self, seconds: float) -> None: ...


class InitContext(_ResumableParticleStageContext):
    pass


class UpdateContext(_ResumableParticleStageContext):
    delta_time: float

    pass


class RenderingContext(_ResumableParticleStageContext):
    pass


class ParticleStream:
    id: int
    position: tuple[float, float, float]
    age: float
    lifetime: float
    size: float
    scale: tuple[float, float, float]
    rotation: float
    orientation: tuple[float, float, float]
    color: tuple[float, float, float, float]
    ribbon_strip_id: int
    ribbon_order: int
    ribbon_break: bool
    collision_hit: bool
    collision_normal: tuple[float, float, float]

    def get_attribute(self, name_or_stable_id: str): ...
    def set_attribute(self, name_or_stable_id: str, value) -> None: ...
    def add_attribute(self, name_or_stable_id: str, value) -> None: ...
    def multiply_attribute(self, name_or_stable_id: str, value) -> None: ...

    def set_position(self, value) -> None: ...
    def add_position(self, value) -> None: ...
    def multiply_position(self, value) -> None: ...
    def set_velocity(self, value) -> None: ...
    def add_velocity(self, value) -> None: ...
    def multiply_velocity(self, value) -> None: ...
    def set_lifetime(self, value) -> None: ...
    def add_lifetime(self, value) -> None: ...
    def multiply_lifetime(self, value) -> None: ...
    def set_flipbook_frame(self, value) -> None: ...
    def add_flipbook_frame(self, value) -> None: ...
    def multiply_flipbook_frame(self, value) -> None: ...
    def set_rotation(self, value) -> None: ...
    def add_rotation(self, value) -> None: ...
    def multiply_rotation(self, value) -> None: ...
    def set_orientation(self, degrees) -> None: ...
    def add_orientation(self, degrees) -> None: ...
    def multiply_orientation(self, degrees) -> None: ...
    def set_color(self, value) -> None: ...
    def add_color(self, value) -> None: ...
    def multiply_color(self, value) -> None: ...
    def set_size(self, value) -> None: ...
    def add_size(self, value) -> None: ...
    def multiply_size(self, value) -> None: ...
    def set_scale(self, value) -> None: ...
    def add_scale(self, value) -> None: ...
    def multiply_scale(self, value) -> None: ...
    def set_strip_id(self, value: int) -> None: ...
    def add_strip_id(self, value: int) -> None: ...
    def multiply_strip_id(self, value: int) -> None: ...
    def set_ribbon_order(self, value: int) -> None: ...
    def add_ribbon_order(self, value: int) -> None: ...
    def multiply_ribbon_order(self, value: int) -> None: ...
    def break_ribbon(self, value: bool) -> None: ...
    def collide_plane(
        self,
        *,
        point=(0.0, 0.0, 0.0),
        normal=(0.0, 1.0, 0.0),
        radius: float = 0.0,
        restitution: float = 0.5,
        friction: float = 0.1,
    ) -> None: ...
    def collide_sphere(
        self,
        *,
        center=(0.0, 0.0, 0.0),
        sphere_radius: float = 1.0,
        particle_radius: float = 0.0,
        restitution: float = 0.5,
        friction: float = 0.1,
    ) -> None: ...
    def collide_sdf(
        self,
        *,
        interface: str,
        particle_radius: float = 0.0,
        restitution: float = 0.5,
        friction: float = 0.1,
        inverted: bool = False,
    ) -> None: ...
    def collide_scene(
        self,
        *,
        particle_radius: float = 0.0,
        layer_mask: int = 0xFFFFFFFF,
        include_triggers: bool = False,
        restitution_scale: float = 1.0,
        friction_scale: float = 1.0,
    ) -> None: ...
    def kill_if(self, condition: bool) -> None: ...
    def emit_event(
        self,
        *,
        route: str,
        condition: bool = True,
        payload: dict[str, Any] | None = None,
    ) -> None: ...
    def sprite(
        self,
        *,
        material: AssetReference = AssetReference(),
        receive_scene_lighting: bool = False,
        receive_shadows: bool = False,
        soft_particles: bool = False,
        soft_distance: float = 1.0,
        sort: str = "back_to_front",
        alignment: str = "camera_plane",
        alignment_axis: tuple[float, float, float] = (0.0, 1.0, 0.0),
        flipbook_columns: int = 1,
        flipbook_rows: int = 1,
    ) -> None: ...
    def mesh(
        self,
        *,
        mesh: AssetReference,
        material: AssetReference = AssetReference(),
        receive_scene_lighting: bool = False,
        receive_shadows: bool = False,
        cast_shadows: bool = False,
        sort: str = "none",
    ) -> None: ...
    def ribbon(
        self,
        *,
        material: AssetReference = AssetReference(),
        receive_scene_lighting: bool = False,
        receive_shadows: bool = False,
        soft_particles: bool = False,
        soft_distance: float = 1.0,
        sort: str = "none",
        uv_mode: str = "stretch",
        uv_scale: float = 1.0,
    ) -> None: ...


class ParticleScriptError(ValueError):
    pass


class ParticleScriptCompiler:
    """Parse the public DSL into ParticleGraph without executing source code."""

    _STAGE_METHODS = frozenset({"init", "update", "rendering"})
    _COLLISION_LIFECYCLE_METHODS = frozenset(
        {"collision_enter", "collision_stay", "collision_exit"}
    )
    _ATTRIBUTE_TARGETS = {
        "position": ("particle.attribute.position", "value"),
        "velocity": ("particle.attribute.velocity", "value"),
        "lifetime": ("particle.attribute.lifetime", "value"),
        "flipbook_frame": ("particle.attribute.flipbook_frame", "value"),
        "rotation": ("particle.attribute.rotation", "value"),
        "orientation": ("particle.attribute.orientation", "degrees"),
        "color": ("particle.attribute.color", "value"),
        "size": ("particle.attribute.size", "value"),
        "scale": ("particle.attribute.scale", "value"),
        "strip_id": ("particle.attribute.strip_id", "value"),
        "ribbon_order": ("particle.attribute.ribbon_order", "value"),
    }
    _ATTRIBUTE_OPERATIONS = {
        f"{composition}_{attribute}": (type_id, property_id, composition)
        for attribute, (type_id, property_id) in _ATTRIBUTE_TARGETS.items()
        for composition in ("set", "add", "multiply")
    }
    _ATTRIBUTE_OPERATIONS["break_ribbon"] = (
        "particle.attribute.ribbon_break",
        "value",
        "",
    )
    _CONTROL_OPERATIONS = {
        "wait_frames": ("particle.control.wait_frames", "frames"),
        "wait_seconds": ("particle.control.wait_seconds", "seconds"),
        "until_frames": ("particle.control.until_frames", "frames"),
        "until_seconds": ("particle.control.until_seconds", "seconds"),
    }
    _OPERATIONS = {
        "init": dict(_ATTRIBUTE_OPERATIONS),
        "update": {
            **_ATTRIBUTE_OPERATIONS,
            "collide_plane": ("particle.update.collide_plane", ""),
            "collide_sphere": ("particle.update.collide_sphere", ""),
            "collide_sdf": ("particle.update.collide_sdf", ""),
            "collide_scene": ("particle.update.collide_scene", ""),
            "kill_if": ("particle.update.kill_if", "condition"),
        },
        "rendering": {
            **_ATTRIBUTE_OPERATIONS,
            "sprite": ("particle.output.sprite", ""),
            "mesh": ("particle.output.mesh", ""),
            "ribbon": ("particle.output.ribbon", ""),
        },
    }

    def parse(self, source: str | bytes, *, source_name: str = "<particle-script>") -> ParticleGraphAsset:
        if isinstance(source, bytes):
            source = source.decode("utf-8")
        try:
            module = ast.parse(source, filename=source_name)
        except SyntaxError as exc:
            raise ParticleScriptError(f"{source_name}:{exc.lineno}: {exc.msg}") from exc
        self._validate_module(module, source_name)
        script_classes = [
            node
            for node in module.body
            if isinstance(node, ast.ClassDef) and self._inherits(node, "ParticleScript")
        ]
        if len(script_classes) != 1:
            raise ParticleScriptError("ParticleScript source requires exactly one ParticleScript class")
        script = script_classes[0]
        self._validate_script_class(script, source_name)
        emitter_nodes = tuple(
            node
            for node in script.body
            if isinstance(node, ast.ClassDef) and self._inherits(node, "ParticleEmitter")
        )
        if not emitter_nodes:
            raise ParticleScriptError("ParticleScript requires at least one nested ParticleEmitter")
        emitter_ids = tuple(
            self._string_assignment(node, "stable_id", required=True)
            for node in emitter_nodes
        )
        if len(set(emitter_ids)) != len(emitter_ids):
            raise ParticleScriptError("ParticleScript emitter stable IDs must be unique")
        emitter_names = [node.name for node in emitter_nodes]
        if len(set(emitter_names)) != len(emitter_names):
            raise ParticleScriptError("ParticleScript emitter class names must be unique")
        parameters = self._parse_parameters(script, source_name)
        event_types = self._parse_event_types(script, source_name)
        event_routes = self._parse_event_routes(script, source_name)
        event_type_map = {value.stable_id: value for value in event_types}
        event_route_map = {value.stable_id: value for value in event_routes}
        parameter_map = {value.stable_id: value for value in parameters}
        parameter_id_by_name = {value.name: value.stable_id for value in parameters}
        emitter_id_set = set(emitter_ids)
        for route in event_routes:
            if route.event_type_id not in event_type_map:
                raise ParticleScriptError(
                    f"event route {route.stable_id!r} references unknown event type "
                    f"{route.event_type_id!r}"
                )
            if (
                route.source_emitter_id not in emitter_id_set
                or route.target_emitter_id not in emitter_id_set
            ):
                raise ParticleScriptError(
                    f"event route {route.stable_id!r} references an unknown emitter"
                )
        emitters = tuple(
            self._parse_emitter(
                node,
                source_name,
                _EventParseContext(
                    emitter_id,
                    event_type_map,
                    event_route_map,
                    parameter_map,
                    parameter_id_by_name,
                    {},
                    {},
                ),
            )
            for node, emitter_id in zip(emitter_nodes, emitter_ids)
        )
        stable_id = self._string_assignment(script, "stable_id", required=False)
        if not stable_id:
            stable_id = hashlib.sha256(source_name.encode("utf-8")).hexdigest()[:32]
        try:
            asset = ParticleGraphAsset(
                stable_id=stable_id,
                name=script.name,
                emitters=emitters,
                parameters=parameters,
                event_types=event_types,
                event_routes=event_routes,
            )
            return self._infer_attribute_cache_types(asset, source_name)
        except (TypeError, ValueError) as exc:
            raise ParticleScriptError(f"invalid ParticleScript event graph: {exc}") from exc

    def _infer_attribute_cache_types(
        self, asset: ParticleGraphAsset, source_name: str
    ) -> ParticleGraphAsset:
        definitions = particle_graph_node_definitions(asset)
        emitters = []
        for original in asset.emitters:
            emitter = original
            unresolved: list[tuple[str, str, str]] = []
            for _iteration in range(32):
                catalog = {
                    item.stable_id: item.value_type
                    for item in particle_attribute_catalog(emitter)
                }

                def resolve_type(property_name: str, selected):
                    if property_name == "attribute":
                        return catalog.get(str(selected))
                    if property_name == "parameter":
                        return definitions.parameter_type_by_id.get(str(selected))
                    if property_name == "value_type":
                        try:
                            return TypeRef(ValueType(str(selected)))
                        except ValueError:
                            return None
                    return None

                updates = {}
                unresolved = []
                changed = False
                for stage in (
                    "init",
                    "update",
                    "collision_enter",
                    "collision_stay",
                    "collision_exit",
                    "rendering",
                ):
                    document = getattr(emitter, stage)
                    if document is None:
                        continue
                    incoming = {
                        (link.target_node, link.target_port): link
                        for link in document.links
                        if link.kind is PortKind.VALUE
                    }
                    nodes = []
                    stage_changed = False
                    for node in document.nodes:
                        if node.type_id != "particle.attribute.cache":
                            nodes.append(node)
                            continue
                        link = incoming.get((node.uid, "value"))
                        if link is None:
                            nodes.append(node)
                            continue
                        try:
                            program = ExpressionCompiler(
                                definitions.registry,
                                property_type_resolver=resolve_type,
                            ).compile(
                                document,
                                ((link.source_node, link.source_port),),
                            )
                            value_type = program.outputs[0][1]
                        except ExpressionCompileError as exc:
                            unresolved.append((stage, node.uid, str(exc)))
                            nodes.append(node)
                            continue
                        properties = dict(node.properties)
                        if (
                            properties.get("value_type") != value_type.value_type.value
                            or properties.get("value_space") != value_type.space.value
                        ):
                            properties["value_type"] = value_type.value_type.value
                            properties["value_space"] = value_type.space.value
                            properties["value"] = particle_attribute_zero(value_type)
                            node = replace(node, properties=properties)
                            stage_changed = True
                            changed = True
                        nodes.append(node)
                    if stage_changed:
                        updates[stage] = replace(document, nodes=tuple(nodes))
                if updates:
                    emitter = replace(emitter, **updates)
                if not changed:
                    break
            if unresolved:
                stage, node_uid, detail = unresolved[0]
                raise ParticleScriptError(
                    f"{source_name}: cannot infer Attribute Cache {stage}.{node_uid}: {detail}"
                )
            emitters.append(emitter)
        return replace(asset, emitters=tuple(emitters))

    def compile(self, source: str | bytes, *, source_name: str = "<particle-script>"):
        return ParticleGraphCompiler().compile(self.parse(source, source_name=source_name))

    def load(self, path: str):
        return self.compile(Path(path).read_text(encoding="utf-8"), source_name=str(path))

    def _parse_parameters(
        self, script: ast.ClassDef, source_name: str
    ) -> tuple[ParticleParameter, ...]:
        assignment = self._assignment(script, "parameters")
        if assignment is None:
            return ()
        values = self._value(assignment)
        if not isinstance(values, (list, tuple)) or not all(
            isinstance(value, Parameter) for value in values
        ):
            raise self._error(
                source_name,
                assignment,
                "parameters must be a list or tuple of Parameter values",
            )
        try:
            result = tuple(
                ParticleParameter(
                    value.stable_id,
                    value.name,
                    TypeRef(ValueType(value.value_type)),
                    list(value.default)
                    if isinstance(value.default, tuple)
                    else value.default,
                    value.exposed,
                    value.category,
                    value.tooltip,
                )
                for value in values
            )
        except (TypeError, ValueError) as exc:
            raise self._error(
                source_name, assignment, f"invalid ParticleScript parameter: {exc}"
            ) from exc
        if len({value.stable_id for value in result}) != len(result):
            raise self._error(
                source_name, assignment, "parameter stable IDs must be unique"
            )
        if len({value.name for value in result}) != len(result):
            raise self._error(source_name, assignment, "parameter names must be unique")
        return result

    def _parse_event_types(
        self, script: ast.ClassDef, source_name: str
    ) -> tuple[ParticleEventType, ...]:
        assignment = self._assignment(script, "event_types")
        if assignment is None:
            return ()
        values = self._value(assignment)
        if not isinstance(values, (list, tuple)) or not all(
            isinstance(value, EventType) for value in values
        ):
            raise self._error(
                source_name,
                assignment,
                "event_types must be a list or tuple of EventType values",
            )
        result = []
        try:
            for event_type in values:
                if not isinstance(event_type.fields, (list, tuple)) or not all(
                    isinstance(field, EventField) for field in event_type.fields
                ):
                    raise TypeError("EventType.fields must contain EventField values")
                fields = tuple(
                    ParticleEventField(
                        field.stable_id,
                        field.name,
                        TypeRef(
                            ValueType(field.value_type),
                            CoordinateSpace(field.space),
                        ),
                        field.default,
                    )
                    for field in event_type.fields
                )
                result.append(
                    ParticleEventType(
                        event_type.stable_id,
                        event_type.name,
                        event_type.capacity_per_step,
                        fields,
                    )
                )
        except (TypeError, ValueError) as exc:
            raise self._error(
                source_name, assignment, f"invalid ParticleScript event type: {exc}"
            ) from exc
        if len({value.stable_id for value in result}) != len(result):
            raise self._error(
                source_name, assignment, "event type stable IDs must be unique"
            )
        return tuple(result)

    def _parse_event_routes(
        self, script: ast.ClassDef, source_name: str
    ) -> tuple[ParticleEventRoute, ...]:
        assignment = self._assignment(script, "event_routes")
        if assignment is None:
            return ()
        values = self._value(assignment)
        if not isinstance(values, (list, tuple)) or not all(
            isinstance(value, EventRoute) for value in values
        ):
            raise self._error(
                source_name,
                assignment,
                "event_routes must be a list or tuple of EventRoute values",
            )
        try:
            result = tuple(
                ParticleEventRoute(
                    route.stable_id,
                    route.event_type_id,
                    route.source_emitter_id,
                    route.source_stage,
                    route.target_emitter_id,
                    route.spawn_count,
                )
                for route in values
            )
        except (TypeError, ValueError) as exc:
            raise self._error(
                source_name, assignment, f"invalid ParticleScript event route: {exc}"
            ) from exc
        if len({value.stable_id for value in result}) != len(result):
            raise self._error(
                source_name, assignment, "event route stable IDs must be unique"
            )
        return result

    def _parse_emitter(
        self,
        node: ast.ClassDef,
        source_name: str,
        event_context: _EventParseContext,
    ) -> ParticleEmitterAsset:
        self._validate_emitter_class(node, source_name)
        stable_id = self._string_assignment(node, "stable_id", required=True)
        enabled_node = self._assignment(node, "enabled")
        play_on_start_node = self._assignment(node, "play_on_start")
        enabled = True if enabled_node is None else self._value(enabled_node)
        play_on_start = (
            True if play_on_start_node is None else self._value(play_on_start_node)
        )
        if type(enabled) is not bool or type(play_on_start) is not bool:
            raise self._error(
                source_name,
                enabled_node or play_on_start_node or node,
                "emitter enabled and play_on_start must be boolean literals",
            )
        settings_node = self._assignment(node, "settings")
        if settings_node is None:
            raise self._error(source_name, node, f"emitter {node.name} requires settings")
        settings = self._constructor(settings_node, "EmitterSettings")
        methods = {
            item.name: item
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        cache_by_name = self._collect_attribute_cache_owners(
            methods, source_name
        )
        attribute_context = _EventParseContext(
            event_context.emitter_id,
            event_context.event_types,
            event_context.event_routes,
            event_context.parameters_by_id,
            event_context.parameter_id_by_name,
            {stable_id: name for name, stable_id in cache_by_name.items()},
            dict(cache_by_name),
        )
        data_interfaces_node = self._assignment(node, "data_interfaces")
        data_interfaces = ()
        if data_interfaces_node is not None:
            decoded_interfaces = self._value(data_interfaces_node)
            if not isinstance(decoded_interfaces, (list, tuple)):
                raise self._error(
                    source_name,
                    data_interfaces_node,
                    "emitter data_interfaces must be a list or tuple",
                )
            data_interfaces = tuple(decoded_interfaces)
        if not all(
            isinstance(value, (VectorField, SdfVolume)) for value in data_interfaces
        ):
            raise self._error(
                source_name,
                data_interfaces_node or node,
                "emitter data_interfaces must contain VectorField or SdfVolume values",
            )
        missing = self._STAGE_METHODS - set(methods)
        unknown = set(methods) - (
            self._STAGE_METHODS | self._COLLISION_LIFECYCLE_METHODS
        )
        if missing or unknown:
            raise self._error(
                source_name,
                node,
                f"emitter methods mismatch; missing={sorted(missing)}, unknown={sorted(unknown)}",
            )
        collision_methods = self._COLLISION_LIFECYCLE_METHODS & set(methods)
        if collision_methods and not settings.collision_enabled:
            raise self._error(
                source_name,
                node,
                "collision lifecycle methods require EmitterSettings(collision_enabled=True)",
            )
        stages = {
            stage: self._parse_stage(
                stage,
                methods[stage],
                source_name,
                attribute_context,
            )
            for stage in ("init", "update", "rendering")
        }
        collision_stages = {
            stage: (
                self._parse_stage(
                    stage,
                    methods[stage],
                    source_name,
                    attribute_context,
                )
                if stage in collision_methods
                else None
            )
            for stage in (
                "collision_enter",
                "collision_stay",
                "collision_exit",
            )
        }
        return ParticleEmitterAsset(
            stable_id=stable_id,
            name=node.name,
            enabled=enabled,
            play_on_start=play_on_start,
            settings=settings,
            attributes=standard_particle_attributes(),
            data_interfaces=data_interfaces,
            init=stages["init"],
            update=stages["update"],
            collision_enter=collision_stages["collision_enter"],
            collision_stay=collision_stages["collision_stay"],
            collision_exit=collision_stages["collision_exit"],
            rendering=stages["rendering"],
        )

    def _collect_attribute_cache_owners(
        self, methods: dict[str, ast.AST], source_name: str
    ) -> dict[str, str]:
        owners: dict[str, str] = {}
        for stage in (
            "init",
            "update",
            "collision_enter",
            "collision_stay",
            "collision_exit",
            "rendering",
        ):
            method = methods.get(stage)
            if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if len(method.args.args) < 3:
                continue
            particle_name = method.args.args[2].arg
            for call in ast.walk(method):
                if not (
                    isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Attribute)
                    and isinstance(call.func.value, ast.Name)
                    and call.func.value.id == particle_name
                    and call.func.attr
                    in {"set_attribute", "add_attribute", "multiply_attribute"}
                ):
                    continue
                if len(call.args) != 2 or call.keywords:
                    raise self._error(
                        source_name,
                        call,
                        f"{call.func.attr} requires an attribute name and one value",
                    )
                name = self._literal(call.args[0])
                if type(name) is not str or not name.strip():
                    raise self._error(
                        source_name,
                        call.args[0],
                        "attribute cache name must be a non-empty string",
                    )
                name = name.strip()
                if name in owners:
                    raise self._error(
                        source_name,
                        call.args[0],
                        f"attribute cache {name!r} already has an owning write node",
                    )
                uid = f"{stage}.cache.{call.lineno}.{call.col_offset}"
                owners[name] = particle_attribute_cache_id(stage, uid)
        return owners

    def _parse_stage(
        self,
        stage: str,
        method,
        source_name: str,
        event_context: _EventParseContext,
    ) -> GraphDocument:
        if isinstance(method, ast.AsyncFunctionDef):
            raise self._error(source_name, method, "ParticleScript stage methods cannot be async")
        if len(method.args.args) != 3:
            raise self._error(source_name, method, f"{stage} requires (self, ctx, particles)")
        particle_name = method.args.args[2].arg
        context_name = method.args.args[1].arg
        operation_stage = (
            "update" if stage in self._COLLISION_LIFECYCLE_METHODS else stage
        )
        root = default_stage_graph(stage)
        nodes = [root.nodes[0]]
        links = []
        source_locations = {
            root.nodes[0].uid: self._source_location(source_name, method)
        }
        operation_index = 0
        expression_index = 0

        def append_operation(call: ast.Call) -> tuple[str, int]:
            nonlocal operation_index, expression_index
            index = operation_index
            target_name = (
                call.func.value.id
                if isinstance(call.func.value, ast.Name)
                else ""
            )
            control = self._CONTROL_OPERATIONS.get(call.func.attr)
            if target_name == context_name and control is not None:
                if len(call.args) != 1 or call.keywords:
                    raise self._error(
                        source_name,
                        call,
                        f"{call.func.attr} requires exactly one value",
                    )
                type_id, positional_property = control
                argument = call.args[0]
                properties = {}
                value_source = None
                if self._is_particle_expression(
                    argument, context_name, particle_name
                ):
                    value_source, expression_index = self._parse_expression(
                        argument,
                        stage=stage,
                        context_name=context_name,
                        particle_name=particle_name,
                        source_name=source_name,
                        expression_index=expression_index,
                        nodes=nodes,
                        links=links,
                        event_context=event_context,
                        source_locations=source_locations,
                    )
                else:
                    properties[positional_property] = self._value(argument)
                uid = f"{stage}.{index}.{call.func.attr}"
                self._append_source_node(
                    nodes,
                    source_locations,
                    GraphNodeRecord(uid, type_id, properties=properties),
                    source_name,
                    call,
                )
                if value_source is not None:
                    links.append(
                        GraphLinkRecord(
                            f"{stage}.value.{index}",
                            value_source[0],
                            value_source[1],
                            uid,
                            positional_property,
                            PortKind.VALUE,
                        )
                    )
                operation_index += 1
                return uid, index
            if target_name != particle_name:
                raise self._error(source_name, call, "particle operations must target the stage particle context")
            if call.func.attr in {
                "set_attribute",
                "add_attribute",
                "multiply_attribute",
            }:
                if len(call.args) != 2 or call.keywords:
                    raise self._error(
                        source_name,
                        call,
                        f"{call.func.attr} requires an attribute name and one value",
                    )
                cache_name = self._literal(call.args[0])
                attribute_id = self._resolve_attribute_cache_reference(
                    call.args[0], source_name, event_context
                )
                value = call.args[1]
                value_source = None
                uid = f"{stage}.cache.{call.lineno}.{call.col_offset}"
                if particle_attribute_cache_id(stage, uid) != attribute_id:
                    raise self._error(
                        source_name,
                        call,
                        "attribute cache writes must target their unique owning node",
                    )
                properties = {
                    "name": str(cache_name).strip(),
                    "value_type": ValueType.F32.value,
                    "value_space": CoordinateSpace.NONE.value,
                    "composition": call.func.attr.removesuffix("_attribute"),
                }
                if self._is_particle_expression(value, context_name, particle_name):
                    value_source, expression_index = self._parse_expression(
                        value,
                        stage=stage,
                        context_name=context_name,
                        particle_name=particle_name,
                        source_name=source_name,
                        expression_index=expression_index,
                        nodes=nodes,
                        links=links,
                        event_context=event_context,
                        source_locations=source_locations,
                    )
                else:
                    literal = self._value(value)
                    literal_type = self._particle_literal_type(literal)
                    properties["value"] = (
                        list(literal) if isinstance(literal, tuple) else literal
                    )
                    properties["value_type"] = literal_type.value_type.value
                    properties["value_space"] = literal_type.space.value
                self._append_source_node(
                    nodes,
                    source_locations,
                    GraphNodeRecord(
                        uid,
                        "particle.attribute.cache",
                        properties=properties,
                    ),
                    source_name,
                    call,
                )
                if value_source is not None:
                    links.append(
                        GraphLinkRecord(
                            f"{stage}.value.{index}",
                            value_source[0],
                            value_source[1],
                            uid,
                            "value",
                            PortKind.VALUE,
                        )
                    )
                operation_index += 1
                return uid, index
            if call.func.attr == "emit_event":
                event_node, event_links, expression_index = self._parse_event_output_call(
                    call,
                    stage=stage,
                    operation_index=index,
                    context_name=context_name,
                    particle_name=particle_name,
                    source_name=source_name,
                    expression_index=expression_index,
                    nodes=nodes,
                    links=links,
                    event_context=event_context,
                    source_locations=source_locations,
                )
                self._append_source_node(
                    nodes,
                    source_locations,
                    event_node,
                    source_name,
                    call,
                )
                links.extend(event_links)
                operation_index += 1
                return event_node.uid, index
            operation = self._OPERATIONS[operation_stage].get(call.func.attr)
            if operation is None:
                raise self._error(source_name, call, f"unsupported {stage} operation {call.func.attr!r}")
            if len(operation) == 3:
                type_id, positional_property, composition = operation
            else:
                type_id, positional_property = operation
                composition = ""
            properties = {}
            if composition:
                properties["composition"] = composition
            value_source = None
            if positional_property:
                if len(call.args) != 1:
                    raise self._error(source_name, call, "particle operation requires exactly one value")
                argument = call.args[0]
                if self._is_particle_expression(argument, context_name, particle_name):
                    value_source, expression_index = self._parse_expression(
                        argument,
                        stage=stage,
                        context_name=context_name,
                        particle_name=particle_name,
                        source_name=source_name,
                        expression_index=expression_index,
                        nodes=nodes,
                        links=links,
                        event_context=event_context,
                        source_locations=source_locations,
                    )
                else:
                    properties[positional_property] = self._value(argument)
            elif call.args:
                raise self._error(source_name, call, "particle output arguments must be named")
            for keyword in call.keywords:
                if keyword.arg is None or keyword.arg in properties:
                    raise self._error(source_name, keyword, "invalid or duplicate particle operation argument")
                properties[keyword.arg] = self._value(keyword.value)
            uid = f"{stage}.{index}.{call.func.attr}"
            self._append_source_node(
                nodes,
                source_locations,
                GraphNodeRecord(uid, type_id, properties=properties),
                source_name,
                call,
            )
            if value_source is not None:
                source_uid, source_port = value_source
                links.append(
                    GraphLinkRecord(
                        f"{stage}.value.{index}",
                        source_uid,
                        source_port,
                        uid,
                        positional_property,
                        PortKind.VALUE,
                    )
                )
            operation_index += 1

            return uid, index

        def compile_statements(
            statements: list[ast.stmt],
            previous_uid: str,
            previous_port: str,
        ) -> None:
            nonlocal operation_index, expression_index
            for offset, statement in enumerate(statements):
                if isinstance(statement, ast.Pass):
                    continue
                if isinstance(statement, ast.If):
                    condition, expression_index = self._parse_expression(
                        statement.test,
                        stage=stage,
                        context_name=context_name,
                        particle_name=particle_name,
                        source_name=source_name,
                        expression_index=expression_index,
                        nodes=nodes,
                        links=links,
                        event_context=event_context,
                        source_locations=source_locations,
                    )
                    index = operation_index
                    uid = f"{stage}.{index}.if"
                    self._append_source_node(
                        nodes,
                        source_locations,
                        GraphNodeRecord(uid, "particle.control.if"),
                        source_name,
                        statement,
                    )
                    links.extend(
                        (
                            GraphLinkRecord(
                                f"{stage}.link.{index}",
                                previous_uid,
                                previous_port,
                                uid,
                                "in",
                                PortKind.EXEC,
                            ),
                            GraphLinkRecord(
                                f"{stage}.value.{index}.condition",
                                condition[0],
                                condition[1],
                                uid,
                                "condition",
                                PortKind.VALUE,
                            ),
                        )
                    )
                    operation_index += 1

                    # Expand the continuation into both mutually exclusive paths.
                    # This keeps Wait/Until resumes inside their original execution
                    # lane instead of inventing a merge with ambiguous ownership.
                    remainder = list(statements[offset + 1 :])
                    compile_statements(
                        [*statement.body, *remainder], uid, "true"
                    )
                    compile_statements(
                        [*statement.orelse, *remainder], uid, "false"
                    )
                    return

                call = statement.value if isinstance(statement, ast.Expr) else None
                if not isinstance(call, ast.Call) or not isinstance(
                    call.func, ast.Attribute
                ):
                    raise self._error(
                        source_name,
                        statement,
                        "stage bodies only allow particle operation calls and if/else",
                    )
                uid, index = append_operation(call)
                links.append(
                    GraphLinkRecord(
                        f"{stage}.link.{index}",
                        previous_uid,
                        previous_port,
                        uid,
                        "in",
                        PortKind.EXEC,
                    )
                )
                previous_uid = uid
                previous_port = "out"

        compile_statements(list(method.body), root.nodes[0].uid, "out")
        if stage == "rendering" and not any(node.type_id.startswith("particle.output.") for node in nodes):
            raise self._error(source_name, method, "rendering requires at least one output operation")
        return GraphDocument(
            root.domain,
            tuple(nodes),
            tuple(links),
            source_locations=source_locations,
        )

    def _parse_event_output_call(
        self,
        call: ast.Call,
        *,
        stage: str,
        operation_index: int,
        context_name: str,
        particle_name: str,
        source_name: str,
        expression_index: int,
        nodes: list[GraphNodeRecord],
        links: list[GraphLinkRecord],
        event_context: _EventParseContext,
        source_locations: dict[str, GraphSourceLocation],
    ) -> tuple[GraphNodeRecord, list[GraphLinkRecord], int]:
        if call.args:
            raise self._error(
                source_name, call, "emit_event arguments must be explicitly named"
            )
        keywords = {}
        for keyword in call.keywords:
            if keyword.arg not in {"route", "condition", "payload"}:
                raise self._error(
                    source_name, keyword, "unsupported emit_event argument"
                )
            if keyword.arg in keywords:
                raise self._error(
                    source_name, keyword, "duplicate emit_event argument"
                )
            keywords[keyword.arg] = keyword.value
        if "route" not in keywords:
            raise self._error(source_name, call, "emit_event requires route")
        route_id = self._literal(keywords["route"])
        if type(route_id) is not str or not route_id:
            raise self._error(
                source_name, keywords["route"], "emit_event route must be a stable ID string"
            )
        route = event_context.event_routes.get(route_id)
        if route is None:
            raise self._error(
                source_name, keywords["route"], f"unknown event route {route_id!r}"
            )
        if (
            route.source_emitter_id != event_context.emitter_id
            or route.source_stage != stage
        ):
            raise self._error(
                source_name,
                call,
                f"event route {route_id!r} does not originate from "
                f"{event_context.emitter_id!r}.{stage}",
            )
        event_type = event_context.event_types[route.event_type_id]
        fields = {field.stable_id: field for field in event_type.fields}
        uid = f"{stage}.{operation_index}.emit_event"
        properties = {}
        value_links = []

        condition = keywords.get("condition")
        if condition is not None:
            if self._is_particle_expression(condition, context_name, particle_name):
                source, expression_index = self._parse_expression(
                    condition,
                    stage=stage,
                    context_name=context_name,
                    particle_name=particle_name,
                    source_name=source_name,
                    expression_index=expression_index,
                    nodes=nodes,
                    links=links,
                    event_context=event_context,
                    source_locations=source_locations,
                )
                value_links.append(
                    GraphLinkRecord(
                        f"{stage}.event.value.{operation_index}.condition",
                        source[0],
                        source[1],
                        uid,
                        "condition",
                        PortKind.VALUE,
                    )
                )
            else:
                properties["condition"] = self._value(condition)

        payload = keywords.get("payload")
        if payload is not None:
            if not isinstance(payload, ast.Dict):
                raise self._error(
                    source_name, payload, "emit_event payload must be a literal-key dictionary"
                )
            seen_fields = set()
            for key_node, value_node in zip(payload.keys, payload.values):
                if key_node is None:
                    raise self._error(
                        source_name, payload, "emit_event payload does not allow dictionary expansion"
                    )
                field_id = self._literal(key_node)
                if type(field_id) is not str:
                    raise self._error(
                        source_name, key_node, "emit_event payload keys must be field stable IDs"
                    )
                if field_id in seen_fields:
                    raise self._error(
                        source_name, key_node, f"duplicate event payload field {field_id!r}"
                    )
                seen_fields.add(field_id)
                field = fields.get(field_id)
                if field is None:
                    raise self._error(
                        source_name, key_node, f"unknown event payload field {field_id!r}"
                    )
                port_id = particle_event_payload_port_id(field.stable_id)
                if self._is_particle_expression(value_node, context_name, particle_name):
                    source, expression_index = self._parse_expression(
                        value_node,
                        stage=stage,
                        context_name=context_name,
                        particle_name=particle_name,
                        source_name=source_name,
                        expression_index=expression_index,
                        nodes=nodes,
                        links=links,
                        event_context=event_context,
                        source_locations=source_locations,
                    )
                    value_links.append(
                        GraphLinkRecord(
                            f"{stage}.event.value.{operation_index}.{port_id}",
                            source[0],
                            source[1],
                            uid,
                            port_id,
                            PortKind.VALUE,
                        )
                    )
                else:
                    properties[port_id] = self._value(value_node)
        return (
            GraphNodeRecord(
                uid,
                particle_event_output_type_id(route.stable_id, stage),
                properties=properties,
            ),
            value_links,
            expression_index,
        )

    @staticmethod
    def _is_particle_expression(node: ast.AST, context_name: str, particle_name: str) -> bool:
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
        ):
            return True
        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            return True
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            return True
        if isinstance(node, ast.Compare):
            return True
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return node.value.id in {context_name, particle_name}
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id in {context_name, particle_name}
        )

    @staticmethod
    def _source_location(source_name: str, node: ast.AST) -> GraphSourceLocation:
        line = int(getattr(node, "lineno", 0) or 0)
        end_line = int(getattr(node, "end_lineno", 0) or 0)
        return GraphSourceLocation(
            source_name=source_name,
            line=line,
            column=(int(getattr(node, "col_offset", 0)) + 1) if line else 0,
            end_line=end_line,
            end_column=(int(getattr(node, "end_col_offset", 0)) + 1)
            if end_line
            else 0,
        )

    @classmethod
    def _append_source_node(
        cls,
        nodes: list[GraphNodeRecord],
        source_locations: dict[str, GraphSourceLocation],
        record: GraphNodeRecord,
        source_name: str,
        source_node: ast.AST,
    ) -> None:
        nodes.append(record)
        source_locations[record.uid] = cls._source_location(source_name, source_node)

    def _parse_expression(
        self,
        node: ast.AST,
        *,
        stage: str,
        context_name: str,
        particle_name: str,
        source_name: str,
        expression_index: int,
        nodes: list[GraphNodeRecord],
        links: list[GraphLinkRecord],
        event_context: _EventParseContext,
        source_locations: dict[str, GraphSourceLocation],
    ) -> tuple[tuple[str, str], int]:
        def append_source(record: GraphNodeRecord, origin: ast.AST = node) -> None:
            self._append_source_node(
                nodes,
                source_locations,
                record,
                source_name,
                origin,
            )

        if isinstance(node, ast.Constant) and type(node.value) is bool:
            uid = f"{stage}.expr.{expression_index}.bool"
            append_source(
                GraphNodeRecord(
                    uid,
                    "common.constant.bool",
                    properties={"value": node.value},
                )
            )
            return (uid, "value"), expression_index + 1

        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            uid = f"{stage}.expr.{expression_index}.constant"
            append_source(
                GraphNodeRecord(
                    uid,
                    "common.constant.f32",
                    properties={"value": float(node.value)},
                )
            )
            return (uid, "value"), expression_index + 1

        if isinstance(node, (ast.List, ast.Tuple)):
            literal = self._value(node)
            if (
                not isinstance(literal, (list, tuple))
                or len(literal) not in {2, 3, 4}
                or not all(type(value) in {int, float} for value in literal)
            ):
                raise self._error(
                    source_name,
                    node,
                    "particle vector expressions require two, three, or four numeric values",
                )
            width = len(literal)
            uid = f"{stage}.expr.{expression_index}.vec{width}"
            append_source(
                GraphNodeRecord(
                    uid,
                    f"common.constant.vec{width}",
                    properties={"value": [float(value) for value in literal]},
                )
            )
            return (uid, "value"), expression_index + 1

        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
        ):
            left, expression_index = self._parse_expression(
                node.left,
                stage=stage,
                context_name=context_name,
                particle_name=particle_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
                source_locations=source_locations,
            )
            right, expression_index = self._parse_expression(
                node.right,
                stage=stage,
                context_name=context_name,
                particle_name=particle_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
                source_locations=source_locations,
            )
            operation = {
                ast.Add: ("add", "common.math.add"),
                ast.Sub: ("subtract", "common.math.subtract"),
                ast.Mult: ("multiply", "common.math.multiply"),
                ast.Div: ("divide", "common.math.divide"),
            }[type(node.op)]
            uid = f"{stage}.expr.{expression_index}.{operation[0]}"
            append_source(GraphNodeRecord(uid, operation[1]))
            links.extend(
                (
                    GraphLinkRecord(
                        f"{stage}.expr.link.{expression_index}.a",
                        left[0], left[1], uid, "a", PortKind.VALUE,
                    ),
                    GraphLinkRecord(
                        f"{stage}.expr.link.{expression_index}.b",
                        right[0], right[1], uid, "b", PortKind.VALUE,
                    ),
                )
            )
            return (uid, "result"), expression_index + 1

        if isinstance(node, ast.BoolOp) and isinstance(node.op, (ast.And, ast.Or)):
            if len(node.values) < 2:
                raise self._error(
                    source_name, node, "particle boolean operations require two inputs"
                )
            result, expression_index = self._parse_expression(
                node.values[0],
                stage=stage,
                context_name=context_name,
                particle_name=particle_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
                source_locations=source_locations,
            )
            operation = (
                ("and", "common.logic.and")
                if isinstance(node.op, ast.And)
                else ("or", "common.logic.or")
            )
            for value_node in node.values[1:]:
                right, expression_index = self._parse_expression(
                    value_node,
                    stage=stage,
                    context_name=context_name,
                    particle_name=particle_name,
                    source_name=source_name,
                    expression_index=expression_index,
                    nodes=nodes,
                    links=links,
                    event_context=event_context,
                    source_locations=source_locations,
                )
                uid = f"{stage}.expr.{expression_index}.{operation[0]}"
                append_source(GraphNodeRecord(uid, operation[1]), value_node)
                links.extend(
                    (
                        GraphLinkRecord(
                            f"{stage}.expr.link.{expression_index}.a",
                            result[0], result[1], uid, "a", PortKind.VALUE,
                        ),
                        GraphLinkRecord(
                            f"{stage}.expr.link.{expression_index}.b",
                            right[0], right[1], uid, "b", PortKind.VALUE,
                        ),
                    )
                )
                result = (uid, "result")
                expression_index += 1
            return result, expression_index

        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            operand, expression_index = self._parse_expression(
                node.operand,
                stage=stage,
                context_name=context_name,
                particle_name=particle_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
                source_locations=source_locations,
            )
            uid = f"{stage}.expr.{expression_index}.not"
            append_source(GraphNodeRecord(uid, "common.logic.not"))
            links.append(
                GraphLinkRecord(
                    f"{stage}.expr.link.{expression_index}.value",
                    operand[0], operand[1], uid, "value", PortKind.VALUE,
                )
            )
            return (uid, "result"), expression_index + 1

        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or len(node.comparators) != 1:
                raise self._error(source_name, node, "chained particle comparisons are unsupported")
            comparison = {
                ast.Lt: ("less_than", "common.compare.less_than"),
                ast.LtE: ("less_equal", "common.compare.less_equal"),
                ast.Gt: ("greater_than", "common.compare.greater_than"),
                ast.GtE: ("greater_equal", "common.compare.greater_equal"),
                ast.Eq: ("equal", "common.compare.equal"),
                ast.NotEq: ("not_equal", "common.compare.not_equal"),
            }.get(type(node.ops[0]))
            if comparison is None:
                raise self._error(
                    source_name,
                    node,
                    "particle comparisons support <, <=, >, >=, == and !=",
                )
            left, expression_index = self._parse_expression(
                node.left,
                stage=stage,
                context_name=context_name,
                particle_name=particle_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
                source_locations=source_locations,
            )
            right, expression_index = self._parse_expression(
                node.comparators[0],
                stage=stage,
                context_name=context_name,
                particle_name=particle_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
                source_locations=source_locations,
            )
            uid = f"{stage}.expr.{expression_index}.{comparison[0]}"
            append_source(GraphNodeRecord(uid, comparison[1]))
            links.extend(
                (
                    GraphLinkRecord(
                        f"{stage}.expr.link.{expression_index}.a",
                        left[0], left[1], uid, "a", PortKind.VALUE,
                    ),
                    GraphLinkRecord(
                        f"{stage}.expr.link.{expression_index}.b",
                        right[0], right[1], uid, "b", PortKind.VALUE,
                    ),
                )
            )
            return (uid, "result"), expression_index + 1

        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == context_name
        ):
            if node.attr == "delta_time":
                if stage not in {
                    "update",
                    "collision_enter",
                    "collision_stay",
                    "collision_exit",
                }:
                    raise self._error(
                        source_name,
                        node,
                        "Delta Time is only valid in Update and Collision lifecycle stages",
                    )
                uid = f"{stage}.expr.{expression_index}.delta_time"
                append_source(
                    GraphNodeRecord(uid, "particle.context.delta_time")
                )
                return (uid, "value"), expression_index + 1
            raise self._error(
                source_name,
                node,
                f"unsupported particle context value {node.attr!r}",
            )

        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == particle_name
        ):
            if node.attr == "normalized_age":
                uid = f"{stage}.expr.{expression_index}.normalized_age"
                append_source(
                    GraphNodeRecord(uid, "particle.attribute.normalized_age")
                )
                return (uid, "value"), expression_index + 1
            attributes = {
                "id": "builtin.id",
                "position": "builtin.position",
                "age": "builtin.age",
                "lifetime": "builtin.lifetime",
                "size": "builtin.size",
                "scale": "builtin.scale",
                "ribbon_strip_id": "builtin.ribbon_strip_id",
                "ribbon_order": "builtin.ribbon_order",
                "ribbon_break": "builtin.ribbon_break",
                "collision_hit": "builtin.collision_hit",
                "collision_normal": "builtin.collision_normal",
                "rotation": "builtin.rotation",
                "color": "builtin.color",
            }
            if node.attr not in attributes:
                raise self._error(source_name, node, f"unsupported particle attribute {node.attr!r}")
            attribute = attributes[node.attr]
            uid = f"{stage}.expr.{expression_index}.{node.attr}"
            append_source(
                GraphNodeRecord(
                    uid,
                    "particle.attribute.get",
                    properties={"attribute": attribute},
                )
            )
            return (uid, "value"), expression_index + 1

        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == particle_name
            and node.func.attr == "get_attribute"
        ):
            if len(node.args) != 1 or node.keywords:
                raise self._error(
                    source_name,
                    node,
                    "get_attribute requires exactly one attribute name or stable ID",
                )
            attribute_id = self._resolve_attribute_cache_reference(
                node.args[0], source_name, event_context
            )
            uid = f"{stage}.expr.{expression_index}.attribute_cache"
            append_source(
                GraphNodeRecord(
                    uid,
                    "particle.attribute.get",
                    properties={"attribute": attribute_id},
                )
            )
            return (uid, "value"), expression_index + 1

        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == context_name
        ):
            raise self._error(source_name, node, "unsupported particle expression")
        if node.func.attr == "parameter":
            if len(node.args) != 1 or node.keywords:
                raise self._error(
                    source_name,
                    node,
                    "parameter requires exactly one name or stable ID string",
                )
            lookup = self._literal(node.args[0])
            if type(lookup) is not str or not lookup:
                raise self._error(
                    source_name,
                    node.args[0],
                    "parameter name or stable ID must be a non-empty string",
                )
            stable_id = lookup
            if stable_id not in event_context.parameters_by_id:
                stable_id = event_context.parameter_id_by_name.get(lookup, "")
            if not stable_id:
                raise self._error(
                    source_name, node.args[0], f"unknown particle parameter {lookup!r}"
                )
            uid = f"{stage}.expr.{expression_index}.parameter"
            append_source(
                GraphNodeRecord(
                    uid,
                    "particle.parameter.get",
                    properties={"parameter": stable_id},
                )
            )
            return (uid, "value"), expression_index + 1
        if node.func.attr == "event_payload":
            if stage != "init":
                raise self._error(
                    source_name, node, "event_payload is only available in Init"
                )
            if node.args:
                raise self._error(
                    source_name, node, "event_payload arguments must be explicitly named"
                )
            keywords = {}
            for keyword in node.keywords:
                if keyword.arg not in {"route", "field"} or keyword.arg in keywords:
                    raise self._error(
                        source_name, keyword, "invalid or duplicate event_payload argument"
                    )
                keywords[keyword.arg] = keyword.value
            if set(keywords) != {"route", "field"}:
                raise self._error(
                    source_name, node, "event_payload requires route and field"
                )
            route_id = self._literal(keywords["route"])
            field_id = self._literal(keywords["field"])
            if type(route_id) is not str or type(field_id) is not str:
                raise self._error(
                    source_name, node, "event_payload route and field must be stable ID strings"
                )
            route = event_context.event_routes.get(route_id)
            if route is None:
                raise self._error(
                    source_name, keywords["route"], f"unknown event route {route_id!r}"
                )
            if route.target_emitter_id != event_context.emitter_id:
                raise self._error(
                    source_name,
                    node,
                    f"event route {route_id!r} does not target emitter "
                    f"{event_context.emitter_id!r}",
                )
            event_type = event_context.event_types[route.event_type_id]
            if field_id not in {field.stable_id for field in event_type.fields}:
                raise self._error(
                    source_name,
                    keywords["field"],
                    f"unknown event payload field {field_id!r}",
                )
            uid = f"{stage}.expr.{expression_index}.event_payload"
            append_source(
                GraphNodeRecord(uid, particle_event_payload_type_id(route_id))
            )
            return (
                uid,
                particle_event_payload_port_id(field_id),
            ), expression_index + 1
        if node.func.attr == "sample_texture2d":
            if len(node.args) != 2 or node.keywords:
                raise self._error(
                    source_name,
                    node,
                    "sample_texture2d requires a Texture2D value and UV value",
                )
            texture_source, expression_index = self._parse_expression(
                node.args[0],
                stage=stage,
                context_name=context_name,
                particle_name=particle_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
                source_locations=source_locations,
            )
            uv_source, expression_index = self._parse_expression(
                node.args[1],
                stage=stage,
                context_name=context_name,
                particle_name=particle_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
                source_locations=source_locations,
            )
            uid = f"{stage}.expr.{expression_index}.sample_texture2d"
            append_source(GraphNodeRecord(uid, "common.texture.sample2d"))
            links.extend(
                (
                    GraphLinkRecord(
                        f"{stage}.expr.link.{expression_index}.texture",
                        texture_source[0],
                        texture_source[1],
                        uid,
                        "texture",
                        PortKind.VALUE,
                    ),
                    GraphLinkRecord(
                        f"{stage}.expr.link.{expression_index}.uv",
                        uv_source[0],
                        uv_source[1],
                        uid,
                        "uv",
                        PortKind.VALUE,
                    ),
                )
            )
            return (uid, "color"), expression_index + 1
        if node.func.attr in {"sample_curve", "sample_gradient"}:
            if len(node.args) != 2 or node.keywords:
                raise self._error(
                    source_name,
                    node,
                    f"{node.func.attr} requires authored keys and one sample input",
                )
            literal = self._value(node.args[0])
            expected_type = Curve if node.func.attr == "sample_curve" else Gradient
            if not isinstance(literal, expected_type):
                raise self._error(
                    source_name,
                    node.args[0],
                    f"{node.func.attr} requires a {expected_type.__name__} value",
                )
            input_source, expression_index = self._parse_expression(
                node.args[1],
                stage=stage,
                context_name=context_name,
                particle_name=particle_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
                source_locations=source_locations,
            )
            uid = f"{stage}.expr.{expression_index}.{node.func.attr}"
            node_type = (
                "common.curve.sample"
                if node.func.attr == "sample_curve"
                else "common.gradient.sample"
            )
            output_port = "value" if node.func.attr == "sample_curve" else "color"
            property_name = "curve" if node.func.attr == "sample_curve" else "gradient"
            append_source(
                GraphNodeRecord(
                    uid,
                    node_type,
                    properties={property_name: literal.to_dict()},
                )
            )
            links.append(
                GraphLinkRecord(
                    f"{stage}.expr.link.{expression_index}",
                    input_source[0], input_source[1], uid, "t", PortKind.VALUE,
                )
            )
            return (uid, output_port), expression_index + 1

        if node.func.attr in {"value_noise_3d", "vector_noise_3d"}:
            if not 1 <= len(node.args) <= 3:
                raise self._error(
                    source_name,
                    node,
                    f"{node.func.attr} requires position and optional frequency/seed",
                )
            properties = {}
            positional_names = ("frequency", "seed")
            for name, value_node in zip(positional_names, node.args[1:]):
                properties[name] = self._value(value_node)
            for keyword in node.keywords:
                if keyword.arg not in positional_names or keyword.arg in properties:
                    raise self._error(source_name, keyword, "invalid or duplicate noise argument")
                properties[keyword.arg] = self._value(keyword.value)
            position_source, expression_index = self._parse_expression(
                node.args[0],
                stage=stage,
                context_name=context_name,
                particle_name=particle_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
                source_locations=source_locations,
            )
            uid = f"{stage}.expr.{expression_index}.{node.func.attr}"
            type_id = (
                "common.noise.value3d"
                if node.func.attr == "value_noise_3d"
                else "common.noise.vector3d"
            )
            append_source(GraphNodeRecord(uid, type_id, properties=properties))
            links.append(
                GraphLinkRecord(
                    f"{stage}.expr.link.{expression_index}",
                    position_source[0],
                    position_source[1],
                    uid,
                    "position",
                    PortKind.VALUE,
                )
            )
            return (uid, "value"), expression_index + 1

        if node.func.attr != "sample_vector_field":
            raise self._error(source_name, node, f"unsupported particle context expression {node.func.attr!r}")
        if len(node.args) != 2 or node.keywords:
            raise self._error(
                source_name,
                node,
                "sample_vector_field requires an interface name and particle position",
            )
        interface = self._literal(node.args[0])
        if type(interface) is not str or not interface.strip():
            raise self._error(source_name, node.args[0], "vector field interface must be a non-empty string")
        position_source, expression_index = self._parse_expression(
            node.args[1],
            stage=stage,
            context_name=context_name,
            particle_name=particle_name,
            source_name=source_name,
            expression_index=expression_index,
            nodes=nodes,
            links=links,
            event_context=event_context,
            source_locations=source_locations,
        )
        uid = f"{stage}.expr.{expression_index}.sample_vector_field"
        append_source(
            GraphNodeRecord(
                uid,
                "particle.vector_field.sample",
                properties={"interface": interface.strip()},
            )
        )
        links.append(
            GraphLinkRecord(
                f"{stage}.expr.link.{expression_index}",
                position_source[0],
                position_source[1],
                uid,
                "position",
                PortKind.VALUE,
            )
        )
        return (uid, "value"), expression_index + 1

    @staticmethod
    def _particle_literal_type(value) -> TypeRef:
        if type(value) is bool:
            return TypeRef(ValueType.BOOL)
        if type(value) is int:
            return TypeRef(ValueType.I32)
        if isinstance(value, float):
            return TypeRef(ValueType.F32)
        if isinstance(value, (list, tuple)):
            kind = {
                2: ValueType.VEC2,
                3: ValueType.VEC3,
                4: ValueType.VEC4,
                9: ValueType.MAT3,
                16: ValueType.MAT4,
            }.get(len(value))
            if kind is not None and all(
                not isinstance(item, bool) and isinstance(item, (int, float))
                for item in value
            ):
                return TypeRef(kind)
        raise ParticleScriptError(
            "Attribute Cache values must be GPU-storable bool, number, vector, "
            "matrix, or Texture2D expressions"
        )

    def _resolve_attribute_cache_reference(
        self,
        node: ast.AST,
        source_name: str,
        context: _EventParseContext,
    ) -> str:
        lookup = self._literal(node)
        if type(lookup) is not str or not lookup:
            raise self._error(
                source_name,
                node,
                "attribute cache name or stable ID must be a non-empty string",
            )
        stable_id = lookup
        if stable_id not in context.attributes_by_id:
            stable_id = context.attribute_id_by_name.get(lookup, "")
        if not stable_id:
            raise self._error(
                source_name,
                node,
                f"unknown particle attribute cache {lookup!r}",
            )
        return stable_id

    def _constructor(self, node: ast.AST, expected_name: str):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != expected_name:
            raise ParticleScriptError(f"expected {expected_name}(...) constructor")
        positional_names = {
            "EmitterSettings": (),
            "ScalarRange": ("minimum", "maximum"),
            "ParticleBurst": (
                "time",
                "count",
                "cycles",
                "interval",
                "probability",
            ),
            "EmitterShape": (
                "kind",
                "space",
                "radius",
                "angle_degrees",
                "dimensions",
                "mesh",
                "mesh_mode",
            ),
            "AssetReference": ("guid", "path_hint"),
            "VectorField": (),
            "SdfVolume": (),
            "CurveKey": ("time", "value", "in_tangent", "out_tangent"),
            "Curve": ("keys", "pre_wrap", "post_wrap"),
            "GradientKey": ("time", "color"),
            "Gradient": ("keys", "mode"),
            "EventField": ("stable_id", "name", "value_type", "default", "space"),
            "Parameter": (
                "stable_id",
                "name",
                "value_type",
                "default",
                "exposed",
                "category",
                "tooltip",
            ),
            "EventType": ("stable_id", "name", "capacity_per_step", "fields"),
            "EventRoute": (
                "stable_id",
                "event_type_id",
                "source_emitter_id",
                "source_stage",
                "target_emitter_id",
                "spawn_count",
            ),
        }[expected_name]
        if len(node.args) > len(positional_names):
            raise ParticleScriptError(f"{expected_name} has too many positional arguments")
        values = {
            name: self._value(argument)
            for name, argument in zip(positional_names, node.args)
        }
        for keyword in node.keywords:
            if keyword.arg is None or keyword.arg in values:
                raise ParticleScriptError(f"invalid {expected_name} argument")
            values[keyword.arg] = self._value(keyword.value)
        constructor = {
            "EmitterSettings": EmitterSettings,
            "ScalarRange": ScalarRange,
            "ParticleBurst": ParticleBurst,
            "EmitterShape": EmitterShape,
            "AssetReference": AssetReference,
            "VectorField": VectorField,
            "SdfVolume": SdfVolume,
            "CurveKey": CurveKey,
            "Curve": Curve,
            "GradientKey": GradientKey,
            "Gradient": Gradient,
            "EventField": EventField,
            "Parameter": Parameter,
            "EventType": EventType,
            "EventRoute": EventRoute,
        }[expected_name]
        try:
            if expected_name == "VectorField" and type(values.get("texture")) is dict:
                values["texture"] = AssetReference.from_dict(values["texture"])
            if expected_name == "SdfVolume" and type(values.get("texture")) is dict:
                values["texture"] = AssetReference.from_dict(values["texture"])
            if expected_name == "EmitterShape" and type(values.get("mesh")) is dict:
                values["mesh"] = AssetReference.from_dict(values["mesh"])
            result = constructor(**values)
            return result.to_dict() if isinstance(result, AssetReference) else result
        except (TypeError, ValueError) as exc:
            raise ParticleScriptError(f"invalid {expected_name}: {exc}") from exc

    def _value(self, node: ast.AST):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in {
            "ScalarRange",
            "ParticleBurst",
            "EmitterShape",
            "AssetReference",
            "VectorField",
            "SdfVolume",
            "CurveKey",
            "Curve",
            "GradientKey",
            "Gradient",
            "EventField",
            "Parameter",
            "EventType",
            "EventRoute",
        }:
            return self._constructor(node, node.func.id)
        if isinstance(node, (ast.List, ast.Tuple)):
            values = [self._value(item) for item in node.elts]
            return tuple(values) if isinstance(node, ast.Tuple) else values
        return self._literal(node)

    @staticmethod
    def _literal(node: ast.AST):
        try:
            return ast.literal_eval(node)
        except (ValueError, TypeError) as exc:
            raise ParticleScriptError("ParticleScript values must be literal data") from exc

    @staticmethod
    def _inherits(node: ast.ClassDef, base_name: str) -> bool:
        return any(isinstance(base, ast.Name) and base.id == base_name for base in node.bases)

    def _string_assignment(self, node: ast.ClassDef, name: str, *, required: bool) -> str:
        value = self._assignment(node, name)
        if value is None:
            if required:
                raise ParticleScriptError(f"{node.name} requires {name!r}")
            return ""
        result = self._literal(value)
        if type(result) is not str or not result:
            raise ParticleScriptError(f"{node.name}.{name} must be a non-empty string")
        return result

    @staticmethod
    def _assignment(node: ast.ClassDef, name: str):
        matches = []
        for statement in node.body:
            if isinstance(statement, ast.Assign) and any(
                isinstance(target, ast.Name) and target.id == name for target in statement.targets
            ):
                matches.append(statement.value)
            elif (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id == name
            ):
                matches.append(statement.value)
        if len(matches) > 1:
            raise ParticleScriptError(f"duplicate assignment for {name!r}")
        return matches[0] if matches else None

    def _validate_module(self, module: ast.Module, source_name: str) -> None:
        for statement in module.body:
            if self._is_docstring(statement):
                continue
            if isinstance(statement, ast.ImportFrom) and statement.module in {
                "Infernux.particle",
                "Infernux.particle.script",
            }:
                continue
            if isinstance(statement, ast.ClassDef) and self._inherits(statement, "ParticleScript"):
                continue
            raise self._error(source_name, statement, "unsupported top-level ParticleScript statement")

    def _validate_script_class(self, node: ast.ClassDef, source_name: str) -> None:
        for statement in node.body:
            if self._is_docstring(statement):
                continue
            if isinstance(statement, ast.ClassDef) and self._inherits(statement, "ParticleEmitter"):
                continue
            if self._is_named_assignment(statement, "stable_id"):
                continue
            if self._is_named_assignment(statement, "parameters"):
                continue
            if self._is_named_assignment(statement, "event_types"):
                continue
            if self._is_named_assignment(statement, "event_routes"):
                continue
            raise self._error(source_name, statement, "unsupported ParticleScript class statement")

    def _validate_emitter_class(self, node: ast.ClassDef, source_name: str) -> None:
        for statement in node.body:
            if self._is_docstring(statement):
                continue
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(
                self._is_named_assignment(statement, name)
                for name in (
                    "stable_id",
                    "enabled",
                    "play_on_start",
                    "settings",
                    "data_interfaces",
                )
            ):
                continue
            raise self._error(source_name, statement, "unsupported ParticleEmitter class statement")

    @staticmethod
    def _is_docstring(statement: ast.stmt) -> bool:
        return (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        )

    @staticmethod
    def _is_named_assignment(statement: ast.stmt, name: str) -> bool:
        if isinstance(statement, ast.Assign):
            return any(isinstance(target, ast.Name) and target.id == name for target in statement.targets)
        return (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == name
        )

    @staticmethod
    def _error(source_name: str, node: ast.AST, message: str) -> ParticleScriptError:
        return ParticleScriptError(f"{source_name}:{getattr(node, 'lineno', 0)}: {message}")


__all__ = [
    "AssetReference",
    "Curve",
    "CurveKey",
    "EventField",
    "EventRoute",
    "EventType",
    "InitContext",
    "Gradient",
    "GradientKey",
    "ParticleEmitter",
    "Parameter",
    "ParticleScript",
    "ParticleScriptCompiler",
    "ParticleScriptError",
    "ParticleStream",
    "RenderingContext",
    "UpdateContext",
    "SdfVolume",
    "VectorField",
]
