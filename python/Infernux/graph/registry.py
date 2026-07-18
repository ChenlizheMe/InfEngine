"""Node definitions shared by graph authoring frontends and compiler targets."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping

from .types import TypeRef


class PortDirection(str, Enum):
    INPUT = "input"
    OUTPUT = "output"


class PortKind(str, Enum):
    VALUE = "value"
    STREAM = "stream"
    EVENT = "event"


@dataclass(frozen=True)
class PortDef:
    id: str
    direction: PortDirection
    kind: PortKind = PortKind.VALUE
    value_type: TypeRef | None = None
    type_variable: str = ""
    required: bool = True
    default: Any = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "direction", PortDirection(self.direction))
        object.__setattr__(self, "kind", PortKind(self.kind))
        if not self.id:
            raise ValueError("graph port id cannot be empty")
        if self.kind is PortKind.VALUE and self.value_type is None and not self.type_variable:
            raise ValueError("value ports require a concrete type or type variable")
        if self.kind is not PortKind.VALUE and (self.value_type is not None or self.type_variable):
            raise ValueError("stream and event ports cannot carry a value type")


@dataclass(frozen=True)
class PropertyDef:
    id: str
    value_type: TypeRef
    default: Any


@dataclass(frozen=True)
class NodeDef:
    type_id: str
    display_name: str
    ports: tuple[PortDef, ...]
    properties: tuple[PropertyDef, ...] = ()
    target_opcodes: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.type_id or "." not in self.type_id:
            raise ValueError("node type id must be a namespaced identifier")
        port_ids = [port.id for port in self.ports]
        if len(port_ids) != len(set(port_ids)):
            raise ValueError(f"node {self.type_id!r} contains duplicate port ids")
        property_ids = [item.id for item in self.properties]
        if len(property_ids) != len(set(property_ids)):
            raise ValueError(f"node {self.type_id!r} contains duplicate property ids")

    def port(self, port_id: str) -> PortDef | None:
        return next((port for port in self.ports if port.id == port_id), None)

    def property(self, property_id: str) -> PropertyDef | None:
        return next((item for item in self.properties if item.id == property_id), None)


class NodeDefinitionRegistry:
    def __init__(self) -> None:
        self._definitions: dict[str, NodeDef] = {}

    def register(self, definition: NodeDef) -> NodeDef:
        existing = self._definitions.get(definition.type_id)
        if existing is not None and existing != definition:
            raise ValueError(f"node definition {definition.type_id!r} is already registered")
        self._definitions[definition.type_id] = definition
        return definition

    def get(self, type_id: str) -> NodeDef | None:
        return self._definitions.get(str(type_id))

    def definitions(self) -> tuple[NodeDef, ...]:
        return tuple(self._definitions[key] for key in sorted(self._definitions))


COMMON_NODE_REGISTRY = NodeDefinitionRegistry()


__all__ = [
    "COMMON_NODE_REGISTRY",
    "NodeDef",
    "NodeDefinitionRegistry",
    "PortDef",
    "PortDirection",
    "PortKind",
    "PropertyDef",
]
