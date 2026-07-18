"""Public ParticleScript DSL markers and non-executing AST frontend."""

from __future__ import annotations

import ast
import hashlib
from pathlib import Path
from typing import Any

from Infernux.graph import GraphDocument, GraphLinkRecord, GraphNodeRecord, PortKind
from Infernux.graph.types import AssetReference

from .asset import (
    EmitterSettings,
    EmitterShape,
    ParticleBurst,
    ParticleEmitterAsset,
    ParticleGraphAsset,
    ScalarRange,
    default_stage_graph,
)
from .hir import ParticleGraphCompiler


class ParticleScript:
    """Marker base for one authored ParticleScript asset."""


class ParticleEmitter:
    """Marker base for one emitter declaration nested in ParticleScript."""


class InitContext:
    pass


class UpdateContext:
    pass


class RenderingContext:
    pass


class ParticleStream:
    def set_velocity(self, value) -> None: ...
    def set_lifetime(self, value) -> None: ...
    def acceleration(self, value) -> None: ...
    def sprite(
        self,
        *,
        material: AssetReference = AssetReference(),
        receive_scene_lighting: bool = False,
        receive_shadows: bool = False,
        sort: str = "back_to_front",
    ) -> None: ...


class ParticleScriptError(ValueError):
    pass


class ParticleScriptCompiler:
    """Parse the public DSL into ParticleGraph without executing source code."""

    _STAGE_METHODS = frozenset({"init", "update", "rendering"})
    _OPERATIONS = {
        "init": {
            "set_velocity": ("particle.init.set_velocity", "value"),
            "set_lifetime": ("particle.init.set_lifetime", "value"),
        },
        "update": {
            "acceleration": ("particle.update.acceleration", "value"),
        },
        "rendering": {
            "sprite": ("particle.output.sprite", ""),
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
        emitters = tuple(
            self._parse_emitter(node, source_name)
            for node in script.body
            if isinstance(node, ast.ClassDef) and self._inherits(node, "ParticleEmitter")
        )
        if not emitters:
            raise ParticleScriptError("ParticleScript requires at least one nested ParticleEmitter")
        stable_id = self._string_assignment(script, "stable_id", required=False)
        if not stable_id:
            stable_id = hashlib.sha256(source_name.encode("utf-8")).hexdigest()[:32]
        return ParticleGraphAsset(stable_id=stable_id, name=script.name, emitters=emitters)

    def compile(self, source: str | bytes, *, source_name: str = "<particle-script>"):
        return ParticleGraphCompiler().compile(self.parse(source, source_name=source_name))

    def load(self, path: str):
        return self.compile(Path(path).read_text(encoding="utf-8"), source_name=str(path))

    def _parse_emitter(self, node: ast.ClassDef, source_name: str) -> ParticleEmitterAsset:
        self._validate_emitter_class(node, source_name)
        stable_id = self._string_assignment(node, "stable_id", required=True)
        settings_node = self._assignment(node, "settings")
        if settings_node is None:
            raise self._error(source_name, node, f"emitter {node.name} requires settings")
        settings = self._constructor(settings_node, "EmitterSettings")
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
            stage: self._parse_stage(stage, methods[stage], source_name)
            for stage in ("init", "update", "rendering")
        }
        return ParticleEmitterAsset(
            stable_id=stable_id,
            name=node.name,
            settings=settings,
            init=stages["init"],
            update=stages["update"],
            rendering=stages["rendering"],
        )

    def _parse_stage(self, stage: str, method, source_name: str) -> GraphDocument:
        if isinstance(method, ast.AsyncFunctionDef):
            raise self._error(source_name, method, "ParticleScript stage methods cannot be async")
        if len(method.args.args) != 3:
            raise self._error(source_name, method, f"{stage} requires (self, ctx, particles)")
        stream_name = method.args.args[2].arg
        root = default_stage_graph(stage)
        nodes = [root.nodes[0]]
        links = []
        previous_uid = root.nodes[0].uid
        operation_index = 0
        for statement in method.body:
            if isinstance(statement, ast.Pass):
                continue
            call = statement.value if isinstance(statement, ast.Expr) else None
            if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
                raise self._error(source_name, statement, "stage bodies only allow particle operation calls")
            if not isinstance(call.func.value, ast.Name) or call.func.value.id != stream_name:
                raise self._error(source_name, call, "particle operations must target the stage particle stream")
            operation = self._OPERATIONS[stage].get(call.func.attr)
            if operation is None:
                raise self._error(source_name, call, f"unsupported {stage} operation {call.func.attr!r}")
            type_id, positional_property = operation
            properties = self._call_properties(
                call,
                positional_property=positional_property,
                source_name=source_name,
            )
            uid = f"{stage}.{operation_index}.{call.func.attr}"
            nodes.append(GraphNodeRecord(uid, type_id, properties=properties))
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

    def _call_properties(self, call, *, positional_property: str, source_name: str) -> dict[str, Any]:
        properties = {}
        if positional_property:
            if len(call.args) != 1:
                raise self._error(source_name, call, "particle operation requires exactly one value")
            properties[positional_property] = self._value(call.args[0])
        elif call.args:
            raise self._error(source_name, call, "particle output arguments must be named")
        for keyword in call.keywords:
            if keyword.arg is None or keyword.arg in properties:
                raise self._error(source_name, keyword, "invalid or duplicate particle operation argument")
            properties[keyword.arg] = self._value(keyword.value)
        return properties

    def _constructor(self, node: ast.AST, expected_name: str):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name) or node.func.id != expected_name:
            raise ParticleScriptError(f"expected {expected_name}(...) constructor")
        positional_names = {
            "EmitterSettings": (),
            "ScalarRange": ("minimum", "maximum"),
            "ParticleBurst": ("time", "count", "cycles", "interval"),
            "EmitterShape": ("kind", "space", "radius", "angle_degrees", "dimensions"),
            "AssetReference": ("guid", "path_hint"),
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
        }[expected_name]
        try:
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
            raise self._error(source_name, statement, "unsupported ParticleScript class statement")

    def _validate_emitter_class(self, node: ast.ClassDef, source_name: str) -> None:
        for statement in node.body:
            if self._is_docstring(statement):
                continue
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if self._is_named_assignment(statement, "stable_id") or self._is_named_assignment(
                statement, "settings"
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
    "InitContext",
    "ParticleEmitter",
    "ParticleScript",
    "ParticleScriptCompiler",
    "ParticleScriptError",
    "ParticleStream",
    "RenderingContext",
    "UpdateContext",
]
