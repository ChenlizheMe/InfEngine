"""Typed expression IR compiler shared by backend-specific graph frontends."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from .document import GraphDocument, GraphLinkRecord, GraphNodeRecord
from .registry import (
    COMMON_NODE_REGISTRY,
    NodeDefinitionRegistry,
    PortDef,
    PortDirection,
    PortKind,
)
from .types import AssetReference, PORTABLE_TYPE_SYSTEM, TypeRef, TypeSystem, ValueType
from .ramp import Curve, Gradient


@dataclass(frozen=True)
class ExpressionDiagnostic:
    code: str
    message: str
    node_uid: str = ""
    link_uid: str = ""


class ExpressionCompileError(ValueError):
    def __init__(self, diagnostics):
        self.diagnostics = tuple(diagnostics)
        super().__init__("; ".join(item.message for item in self.diagnostics))


@dataclass(frozen=True)
class ExpressionOperand:
    value_type: TypeRef
    value_id: str = ""
    literal: Any = None


@dataclass(frozen=True)
class ExpressionInstruction:
    result_id: str
    opcode: str
    result_type: TypeRef
    operands: tuple[ExpressionOperand, ...]
    source_node_uid: str
    source_port_id: str
    immediates: tuple[tuple[str, Any], ...] = ()

    def immediate_dict(self) -> dict[str, Any]:
        return dict(self.immediates)


@dataclass(frozen=True)
class ExpressionProgram:
    instructions: tuple[ExpressionInstruction, ...]
    outputs: tuple[tuple[str, TypeRef], ...]
    semantic_hash: str


class ExpressionCompiler:
    def __init__(
        self,
        registry: NodeDefinitionRegistry = COMMON_NODE_REGISTRY,
        type_system: TypeSystem = PORTABLE_TYPE_SYSTEM,
    ) -> None:
        self._registry = registry
        self._types = type_system

    def compile(self, document: GraphDocument, outputs) -> ExpressionProgram:
        by_uid = {node.uid: node for node in document.nodes}
        incoming = {
            (link.target_node, link.target_port): link
            for link in document.links
            if link.kind is PortKind.VALUE
        }
        diagnostics = self._validate(document, by_uid)
        if diagnostics:
            raise ExpressionCompileError(diagnostics)

        instructions: list[ExpressionInstruction] = []
        values: dict[tuple[str, str], tuple[str, TypeRef]] = {}
        active: set[tuple[str, str]] = set()

        def emit(node_uid: str, port_id: str) -> tuple[str, TypeRef]:
            key = (node_uid, port_id)
            cached = values.get(key)
            if cached is not None:
                return cached
            if key in active:
                raise ExpressionCompileError(
                    [ExpressionDiagnostic("value_cycle", "expression graph contains a cycle", node_uid)]
                )
            active.add(key)
            node = by_uid[node_uid]
            definition = self._registry.get(node.type_id)
            output = definition.port(port_id) if definition else None
            if output is None or output.direction is not PortDirection.OUTPUT:
                raise ExpressionCompileError(
                    [ExpressionDiagnostic("missing_output", f"unknown output {node_uid}.{port_id}", node_uid)]
                )

            operands = []
            input_types: dict[str, TypeRef] = {}
            for port in definition.ports:
                if port.direction is not PortDirection.INPUT or port.kind is not PortKind.VALUE:
                    continue
                link = incoming.get((node_uid, port.id))
                if link is not None:
                    value_id, value_type = emit(link.source_node, link.source_port)
                    if port.value_type is not None and not self._types.can_connect(
                        value_type, port.value_type
                    ):
                        raise ExpressionCompileError(
                            [
                                ExpressionDiagnostic(
                                    "type_mismatch",
                                    f"cannot connect {value_type} to {port.value_type}",
                                    node_uid,
                                    link.uid,
                                )
                            ]
                        )
                    operands.append(ExpressionOperand(value_type, value_id=value_id))
                else:
                    value_type = port.value_type or TypeRef(ValueType.F32)
                    operands.append(
                        ExpressionOperand(
                            value_type,
                            literal=node.properties.get(port.id, port.default),
                        )
                    )
                input_types[port.id] = value_type

            try:
                result_type = self._resolve_output_type(definition.type_id, output, input_types)
            except TypeError as exc:
                raise ExpressionCompileError(
                    [ExpressionDiagnostic("type_mismatch", str(exc), node_uid)]
                ) from exc
            result_id = f"{node_uid}.{port_id}"
            opcode = definition.target_opcodes.get("expression", "")
            if not opcode:
                raise ExpressionCompileError(
                    [ExpressionDiagnostic("missing_target", f"{definition.type_id} has no expression target", node_uid)]
                )
            immediates = []
            for prop in definition.properties:
                literal = node.properties.get(prop.id, prop.default)
                literal_error = self._literal_error(prop.value_type, literal)
                if literal_error:
                    raise ExpressionCompileError(
                        [
                            ExpressionDiagnostic(
                                "invalid_literal",
                                f"{definition.type_id}.{prop.id} {literal_error}",
                                node_uid,
                            )
                        ]
                    )
                immediates.append((prop.id, literal))
            if opcode == "constant":
                prop = definition.properties[0]
                literal = node.properties.get(prop.id, prop.default)
                operands = [ExpressionOperand(prop.value_type, literal=literal)]
                result_type = prop.value_type
                immediates = []
            instructions.append(
                ExpressionInstruction(
                    result_id,
                    opcode,
                    result_type,
                    tuple(operands),
                    node_uid,
                    port_id,
                    tuple(immediates),
                )
            )
            active.remove(key)
            values[key] = (result_id, result_type)
            return values[key]

        compiled_outputs = tuple(emit(str(node_uid), str(port_id)) for node_uid, port_id in outputs)
        return ExpressionProgram(
            tuple(instructions),
            compiled_outputs,
            document.semantic_hash(),
        )

    def _validate(self, document, by_uid) -> list[ExpressionDiagnostic]:
        diagnostics = []
        for node in document.nodes:
            definition = self._registry.get(node.type_id)
            if definition is None:
                diagnostics.append(
                    ExpressionDiagnostic("unknown_node", f"unknown node type {node.type_id!r}", node.uid)
                )
                continue
            editable_inputs = {
                port.id
                for port in definition.ports
                if port.direction is PortDirection.INPUT
                and port.kind is PortKind.VALUE
                and not port.required
            }
            unknown = set(node.properties) - (
                {item.id for item in definition.properties} | editable_inputs
            )
            if unknown:
                diagnostics.append(
                    ExpressionDiagnostic(
                        "unknown_property",
                        f"{node.type_id} has unknown properties: {sorted(unknown)}",
                        node.uid,
                    )
                )
        seen_targets = set()
        for link in document.links:
            if link.kind is not PortKind.VALUE:
                continue
            source = by_uid.get(link.source_node)
            target = by_uid.get(link.target_node)
            source_def = self._registry.get(source.type_id) if source else None
            target_def = self._registry.get(target.type_id) if target else None
            source_port = source_def.port(link.source_port) if source_def else None
            target_port = target_def.port(link.target_port) if target_def else None
            if source_port is None or source_port.direction is not PortDirection.OUTPUT:
                diagnostics.append(ExpressionDiagnostic("invalid_source", "invalid value link source", link_uid=link.uid))
            if target_port is None or target_port.direction is not PortDirection.INPUT:
                diagnostics.append(ExpressionDiagnostic("invalid_target", "invalid value link target", link_uid=link.uid))
            target_key = (link.target_node, link.target_port)
            if target_key in seen_targets:
                diagnostics.append(ExpressionDiagnostic("multiple_inputs", "value input has multiple links", link_uid=link.uid))
            seen_targets.add(target_key)
        return diagnostics

    def _resolve_output_type(
        self,
        type_id: str,
        output: PortDef,
        inputs: Mapping[str, TypeRef],
    ) -> TypeRef:
        if output.value_type is not None:
            return output.value_type
        if type_id in {
            "common.math.add",
            "common.math.subtract",
            "common.math.multiply",
            "common.math.divide",
            "common.math.lerp",
        }:
            return self._types.unify_numeric(inputs["a"], inputs["b"])
        raise TypeError(f"cannot resolve type variable {output.type_variable!r} for {type_id}")

    @staticmethod
    def _literal_error(value_type: TypeRef, value: Any) -> str:
        kind = value_type.value_type
        if kind is ValueType.CURVE:
            try:
                Curve.from_dict(value)
                return ""
            except (TypeError, ValueError) as exc:
                return str(exc)
        if kind is ValueType.GRADIENT:
            try:
                Gradient.from_dict(value)
                return ""
            except (TypeError, ValueError) as exc:
                return str(exc)
        if kind is ValueType.ASSET_REF:
            try:
                AssetReference.from_dict(value)
                return ""
            except (TypeError, ValueError):
                return "must contain string guid and path_hint fields"
        if kind is ValueType.STRING:
            return "must be a string" if type(value) is not str else ""
        if kind is ValueType.BOOL:
            return "must be a bool" if type(value) is not bool else ""
        if kind in {ValueType.I32, ValueType.U32}:
            if type(value) is not int:
                return "must be an integer"
            if kind is ValueType.U32 and value < 0:
                return "must be non-negative"
            return ""
        if kind is ValueType.F32:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return "must be a number"
            return "must be finite" if not math.isfinite(float(value)) else ""
        size = {
            ValueType.VEC2: 2,
            ValueType.VEC3: 3,
            ValueType.VEC4: 4,
            ValueType.COLOR: 4,
            ValueType.MAT3: 9,
            ValueType.MAT4: 16,
        }.get(kind)
        if size is None:
            return "has an unsupported literal type"
        if not isinstance(value, (list, tuple)) or len(value) != size:
            return f"must contain exactly {size} numbers"
        if any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            for item in value
        ):
            return "must contain finite numbers"
        return ""


__all__ = [
    "ExpressionCompileError",
    "ExpressionCompiler",
    "ExpressionDiagnostic",
    "ExpressionInstruction",
    "ExpressionOperand",
    "ExpressionProgram",
]
