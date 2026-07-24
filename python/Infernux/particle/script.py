"""Public ParticleScript DSL markers and non-executing AST frontend."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from Infernux.graph import GraphDocument, GraphLinkRecord, GraphNodeRecord, PortKind
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
    ScalarRange,
    default_stage_graph,
)
from .data_interface import PointCache, SdfVolume, VectorField
from .hir import ParticleGraphCompiler
from .nodes import (
    particle_event_output_type_id,
    particle_event_payload_port_id,
    particle_event_payload_type_id,
)


class ParticleScript:
    """Marker base for one authored ParticleScript asset."""


class ParticleEmitter:
    """Marker base for one emitter declaration nested in ParticleScript."""


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


class _ParticleStageContext:
    def sample_vector_field(self, interface: str, position): ...
    def sample_curve(self, curve: Curve, t: float) -> float: ...
    def sample_gradient(self, gradient: Gradient, t: float): ...
    def value_noise_3d(self, position, frequency: float = 1.0, seed: int = 0) -> float: ...
    def vector_noise_3d(self, position, frequency: float = 1.0, seed: int = 0): ...
    def event_payload(self, *, route: str, field: str): ...


class InitContext(_ParticleStageContext):
    pass


class UpdateContext(_ParticleStageContext):
    pass


class RenderingContext(_ParticleStageContext):
    pass


class ParticleStream:
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

    def set_velocity(self, value) -> None: ...
    def set_lifetime(self, value) -> None: ...
    def set_rotation(self, value) -> None: ...
    def set_orientation(self, degrees) -> None: ...
    def set_color(self, value) -> None: ...
    def set_size(self, value) -> None: ...
    def set_scale(self, value) -> None: ...
    def set_strip_id(self, value: int) -> None: ...
    def set_ribbon_order(self, value: int) -> None: ...
    def break_ribbon(self, value: bool) -> None: ...
    def acceleration(self, value) -> None: ...
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
    def rotate(self, degrees_per_second) -> None: ...
    def rotate_orientation(self, degrees_per_second) -> None: ...
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
    _OPERATIONS = {
        "init": {
            "set_velocity": ("particle.attribute.set_velocity", "value"),
            "set_lifetime": ("particle.attribute.set_lifetime", "value"),
            "set_rotation": ("particle.attribute.set_rotation", "value"),
            "set_orientation": ("particle.attribute.set_orientation", "degrees"),
            "set_color": ("particle.attribute.set_color", "value"),
            "set_size": ("particle.attribute.set_size", "value"),
            "set_scale": ("particle.attribute.set_scale", "value"),
            "set_strip_id": ("particle.attribute.set_strip_id", "value"),
            "set_ribbon_order": ("particle.attribute.set_ribbon_order", "value"),
            "break_ribbon": ("particle.attribute.set_ribbon_break", "value"),
        },
        "update": {
            "acceleration": ("particle.update.acceleration", "value"),
            "collide_plane": ("particle.update.collide_plane", ""),
            "collide_sphere": ("particle.update.collide_sphere", ""),
            "collide_sdf": ("particle.update.collide_sdf", ""),
            "set_rotation": ("particle.attribute.set_rotation", "value"),
            "set_orientation": ("particle.attribute.set_orientation", "degrees"),
            "set_color": ("particle.attribute.set_color", "value"),
            "set_size": ("particle.attribute.set_size", "value"),
            "set_scale": ("particle.attribute.set_scale", "value"),
            "set_strip_id": ("particle.attribute.set_strip_id", "value"),
            "set_ribbon_order": ("particle.attribute.set_ribbon_order", "value"),
            "break_ribbon": ("particle.attribute.set_ribbon_break", "value"),
            "rotate": ("particle.update.rotate", "degrees_per_second"),
            "rotate_orientation": (
                "particle.update.rotate_orientation",
                "degrees_per_second",
            ),
            "kill_if": ("particle.update.kill_if", "condition"),
        },
        "rendering": {
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
        event_types = self._parse_event_types(script, source_name)
        event_routes = self._parse_event_routes(script, source_name)
        event_type_map = {value.stable_id: value for value in event_types}
        event_route_map = {value.stable_id: value for value in event_routes}
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
                _EventParseContext(emitter_id, event_type_map, event_route_map),
            )
            for node, emitter_id in zip(emitter_nodes, emitter_ids)
        )
        stable_id = self._string_assignment(script, "stable_id", required=False)
        if not stable_id:
            stable_id = hashlib.sha256(source_name.encode("utf-8")).hexdigest()[:32]
        try:
            return ParticleGraphAsset(
                stable_id=stable_id,
                name=script.name,
                emitters=emitters,
                event_types=event_types,
                event_routes=event_routes,
            )
        except (TypeError, ValueError) as exc:
            raise ParticleScriptError(f"invalid ParticleScript event graph: {exc}") from exc

    def compile(self, source: str | bytes, *, source_name: str = "<particle-script>"):
        return ParticleGraphCompiler().compile(self.parse(source, source_name=source_name))

    def load(self, path: str):
        return self.compile(Path(path).read_text(encoding="utf-8"), source_name=str(path))

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
        settings_node = self._assignment(node, "settings")
        if settings_node is None:
            raise self._error(source_name, node, f"emitter {node.name} requires settings")
        settings = self._constructor(settings_node, "EmitterSettings")
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
            isinstance(value, (VectorField, SdfVolume, PointCache)) for value in data_interfaces
        ):
            raise self._error(
                source_name,
                data_interfaces_node or node,
                "emitter data_interfaces must contain VectorField, SdfVolume, or PointCache values",
            )
        methods = {
            item.name: item
            for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        missing = self._STAGE_METHODS - set(methods)
        unknown = set(methods) - self._STAGE_METHODS
        if missing or unknown:
            raise self._error(
                source_name,
                node,
                f"emitter methods mismatch; missing={sorted(missing)}, unknown={sorted(unknown)}",
            )
        stages = {
            stage: self._parse_stage(
                stage,
                methods[stage],
                source_name,
                event_context,
            )
            for stage in ("init", "update", "rendering")
        }
        return ParticleEmitterAsset(
            stable_id=stable_id,
            name=node.name,
            settings=settings,
            data_interfaces=data_interfaces,
            init=stages["init"],
            update=stages["update"],
            rendering=stages["rendering"],
        )

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
        stream_name = method.args.args[2].arg
        context_name = method.args.args[1].arg
        root = default_stage_graph(stage)
        nodes = [root.nodes[0]]
        links = []
        previous_uid = root.nodes[0].uid
        operation_index = 0
        expression_index = 0
        for statement in method.body:
            if isinstance(statement, ast.Pass):
                continue
            call = statement.value if isinstance(statement, ast.Expr) else None
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                raise self._error(source_name, statement, "stage bodies only allow particle operation calls")
            if not isinstance(call.func.value, ast.Name) or call.func.value.id != stream_name:
                raise self._error(source_name, call, "particle operations must target the stage particle stream")
            if call.func.attr == "emit_event":
                event_node, event_links, expression_index = self._parse_event_output_call(
                    call,
                    stage=stage,
                    operation_index=operation_index,
                    context_name=context_name,
                    stream_name=stream_name,
                    source_name=source_name,
                    expression_index=expression_index,
                    nodes=nodes,
                    links=links,
                    event_context=event_context,
                )
                nodes.append(event_node)
                links.extend(event_links)
                uid = event_node.uid
                links.append(
                    GraphLinkRecord(
                        f"{stage}.link.{operation_index}",
                        previous_uid,
                        "out",
                        uid,
                        "in",
                        PortKind.STREAM,
                    )
                )
                previous_uid = uid
                operation_index += 1
                continue
            operation = self._OPERATIONS[stage].get(call.func.attr)
            if operation is None:
                raise self._error(source_name, call, f"unsupported {stage} operation {call.func.attr!r}")
            type_id, positional_property = operation
            properties = {}
            value_source = None
            if positional_property:
                if len(call.args) != 1:
                    raise self._error(source_name, call, "particle operation requires exactly one value")
                argument = call.args[0]
                if self._is_particle_expression(argument, context_name, stream_name):
                    value_source, expression_index = self._parse_expression(
                        argument,
                        stage=stage,
                        context_name=context_name,
                        stream_name=stream_name,
                        source_name=source_name,
                        expression_index=expression_index,
                        nodes=nodes,
                        links=links,
                        event_context=event_context,
                    )
                else:
                    properties[positional_property] = self._value(argument)
            elif call.args:
                raise self._error(source_name, call, "particle output arguments must be named")
            for keyword in call.keywords:
                if keyword.arg is None or keyword.arg in properties:
                    raise self._error(source_name, keyword, "invalid or duplicate particle operation argument")
                properties[keyword.arg] = self._value(keyword.value)
            uid = f"{stage}.{operation_index}.{call.func.attr}"
            nodes.append(GraphNodeRecord(uid, type_id, properties=properties))
            if value_source is not None:
                source_uid, source_port = value_source
                links.append(
                    GraphLinkRecord(
                        f"{stage}.value.{operation_index}",
                        source_uid,
                        source_port,
                        uid,
                        positional_property,
                        PortKind.VALUE,
                    )
                )
            links.append(
                GraphLinkRecord(
                    f"{stage}.link.{operation_index}",
                    previous_uid,
                    "out",
                    uid,
                    "in",
                    PortKind.STREAM,
                )
            )
            previous_uid = uid
            operation_index += 1
        if stage == "rendering" and not any(node.type_id.startswith("particle.output.") for node in nodes):
            raise self._error(source_name, method, "rendering requires at least one output operation")
        return GraphDocument(root.domain, tuple(nodes), tuple(links))

    def _parse_event_output_call(
        self,
        call: ast.Call,
        *,
        stage: str,
        operation_index: int,
        context_name: str,
        stream_name: str,
        source_name: str,
        expression_index: int,
        nodes: list[GraphNodeRecord],
        links: list[GraphLinkRecord],
        event_context: _EventParseContext,
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
            if self._is_particle_expression(condition, context_name, stream_name):
                source, expression_index = self._parse_expression(
                    condition,
                    stage=stage,
                    context_name=context_name,
                    stream_name=stream_name,
                    source_name=source_name,
                    expression_index=expression_index,
                    nodes=nodes,
                    links=links,
                    event_context=event_context,
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
                if self._is_particle_expression(value_node, context_name, stream_name):
                    source, expression_index = self._parse_expression(
                        value_node,
                        stage=stage,
                        context_name=context_name,
                        stream_name=stream_name,
                        source_name=source_name,
                        expression_index=expression_index,
                        nodes=nodes,
                        links=links,
                        event_context=event_context,
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
    def _is_particle_expression(node: ast.AST, context_name: str, stream_name: str) -> bool:
        if isinstance(node, ast.BinOp) and isinstance(
            node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div)
        ):
            return True
        if isinstance(node, ast.Compare):
            return True
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            return node.value.id == stream_name
        return (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == context_name
        )

    def _parse_expression(
        self,
        node: ast.AST,
        *,
        stage: str,
        context_name: str,
        stream_name: str,
        source_name: str,
        expression_index: int,
        nodes: list[GraphNodeRecord],
        links: list[GraphLinkRecord],
        event_context: _EventParseContext,
    ) -> tuple[tuple[str, str], int]:
        if isinstance(node, ast.Constant) and type(node.value) in {int, float}:
            uid = f"{stage}.expr.{expression_index}.constant"
            nodes.append(
                GraphNodeRecord(
                    uid,
                    "common.constant.f32",
                    properties={"value": float(node.value)},
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
                stream_name=stream_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
            )
            right, expression_index = self._parse_expression(
                node.right,
                stage=stage,
                context_name=context_name,
                stream_name=stream_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
            )
            operation = {
                ast.Add: ("add", "common.math.add"),
                ast.Sub: ("subtract", "common.math.subtract"),
                ast.Mult: ("multiply", "common.math.multiply"),
                ast.Div: ("divide", "common.math.divide"),
            }[type(node.op)]
            uid = f"{stage}.expr.{expression_index}.{operation[0]}"
            nodes.append(GraphNodeRecord(uid, operation[1]))
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

        if isinstance(node, ast.Compare):
            if len(node.ops) != 1 or len(node.comparators) != 1:
                raise self._error(source_name, node, "chained particle comparisons are unsupported")
            comparison = {
                ast.Lt: ("less_than", "common.compare.less_than"),
                ast.LtE: ("less_equal", "common.compare.less_equal"),
                ast.Gt: ("greater_than", "common.compare.greater_than"),
                ast.GtE: ("greater_equal", "common.compare.greater_equal"),
            }.get(type(node.ops[0]))
            if comparison is None:
                raise self._error(source_name, node, "particle comparisons support <, <=, > and >=")
            left, expression_index = self._parse_expression(
                node.left,
                stage=stage,
                context_name=context_name,
                stream_name=stream_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
            )
            right, expression_index = self._parse_expression(
                node.comparators[0],
                stage=stage,
                context_name=context_name,
                stream_name=stream_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
            )
            uid = f"{stage}.expr.{expression_index}.{comparison[0]}"
            nodes.append(GraphNodeRecord(uid, comparison[1]))
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
            and node.value.id == stream_name
        ):
            attributes = {
                "position": ("particle.attribute.read_vec3", "builtin.position"),
                "age": ("particle.attribute.read_f32", "builtin.age"),
                "lifetime": ("particle.attribute.read_f32", "builtin.lifetime"),
                "size": ("particle.attribute.read_f32", "builtin.size"),
                "scale": ("particle.attribute.read_vec3", "builtin.scale"),
                "ribbon_strip_id": ("particle.attribute.read_u32", "builtin.ribbon_strip_id"),
                "ribbon_order": ("particle.attribute.read_u32", "builtin.ribbon_order"),
                "ribbon_break": ("particle.attribute.read_bool", "builtin.ribbon_break"),
                "rotation": ("particle.attribute.read_f32", "builtin.rotation"),
                "color": ("particle.attribute.read_color", "builtin.color"),
            }
            if node.attr not in attributes:
                raise self._error(source_name, node, f"unsupported particle attribute {node.attr!r}")
            type_id, attribute = attributes[node.attr]
            uid = f"{stage}.expr.{expression_index}.{node.attr}"
            nodes.append(
                GraphNodeRecord(
                    uid,
                    type_id,
                    properties={"attribute": attribute},
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
            nodes.append(
                GraphNodeRecord(uid, particle_event_payload_type_id(route_id))
            )
            return (
                uid,
                particle_event_payload_port_id(field_id),
            ), expression_index + 1
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
                stream_name=stream_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
            )
            uid = f"{stage}.expr.{expression_index}.{node.func.attr}"
            node_type = (
                "common.curve.sample"
                if node.func.attr == "sample_curve"
                else "common.gradient.sample"
            )
            output_port = "value" if node.func.attr == "sample_curve" else "color"
            property_name = "curve" if node.func.attr == "sample_curve" else "gradient"
            nodes.append(
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
                stream_name=stream_name,
                source_name=source_name,
                expression_index=expression_index,
                nodes=nodes,
                links=links,
                event_context=event_context,
            )
            uid = f"{stage}.expr.{expression_index}.{node.func.attr}"
            type_id = (
                "common.noise.value3d"
                if node.func.attr == "value_noise_3d"
                else "common.noise.vector3d"
            )
            nodes.append(GraphNodeRecord(uid, type_id, properties=properties))
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
            stream_name=stream_name,
            source_name=source_name,
            expression_index=expression_index,
            nodes=nodes,
            links=links,
            event_context=event_context,
        )
        uid = f"{stage}.expr.{expression_index}.sample_vector_field"
        nodes.append(
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

    def _constructor(self, node: ast.AST, expected_name: str):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != expected_name:
            raise ParticleScriptError(f"expected {expected_name}(...) constructor")
        positional_names = {
            "EmitterSettings": (),
            "ScalarRange": ("minimum", "maximum"),
            "ParticleBurst": ("time", "count", "cycles", "interval"),
            "EmitterShape": ("kind", "space", "radius", "angle_degrees", "dimensions"),
            "AssetReference": ("guid", "path_hint"),
            "VectorField": (),
            "SdfVolume": (),
            "PointCache": (),
            "CurveKey": ("time", "value", "in_tangent", "out_tangent"),
            "Curve": ("keys", "pre_wrap", "post_wrap"),
            "GradientKey": ("time", "color"),
            "Gradient": ("keys", "mode"),
            "EventField": ("stable_id", "name", "value_type", "default", "space"),
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
            "PointCache": PointCache,
            "CurveKey": CurveKey,
            "Curve": Curve,
            "GradientKey": GradientKey,
            "Gradient": Gradient,
            "EventField": EventField,
            "EventType": EventType,
            "EventRoute": EventRoute,
        }[expected_name]
        try:
            if expected_name == "VectorField" and type(values.get("texture")) is dict:
                values["texture"] = AssetReference.from_dict(values["texture"])
            if expected_name == "SdfVolume" and type(values.get("texture")) is dict:
                values["texture"] = AssetReference.from_dict(values["texture"])
            if expected_name == "PointCache" and type(values.get("cache")) is dict:
                values["cache"] = AssetReference.from_dict(values["cache"])
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
            "PointCache",
            "CurveKey",
            "Curve",
            "GradientKey",
            "Gradient",
            "EventField",
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
            if self._is_named_assignment(statement, "stable_id") or self._is_named_assignment(
                statement, "settings"
            ) or self._is_named_assignment(statement, "data_interfaces"):
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
    "ParticleScript",
    "ParticleScriptCompiler",
    "ParticleScriptError",
    "ParticleStream",
    "PointCache",
    "RenderingContext",
    "UpdateContext",
    "SdfVolume",
    "VectorField",
]
