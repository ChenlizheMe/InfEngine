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
    EXEC = "exec"
    EVENT = "event"


class PortDimensionPolicy(str, Enum):
    """How one value input participates in numeric shape resolution."""

    EXACT = "exact"
    FIXED = "fixed"
    PROMOTE = "promote"


@dataclass(frozen=True)
class PortDef:
    id: str
    direction: PortDirection
    kind: PortKind = PortKind.VALUE
    value_type: TypeRef | None = None
    type_variable: str = ""
    required: bool = True
    default: Any = None
    display_name: str = ""
    dimension_policy: PortDimensionPolicy = PortDimensionPolicy.EXACT
    type_property: str = ""
    max_connections: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "direction", PortDirection(self.direction))
        object.__setattr__(self, "kind", PortKind(self.kind))
        object.__setattr__(
            self,
            "dimension_policy",
            PortDimensionPolicy(self.dimension_policy),
        )
        if not self.id:
            raise ValueError("graph port id cannot be empty")
        if type(self.display_name) is not str:
            raise ValueError("graph port display_name must be a string")
        if self.kind is PortKind.VALUE and self.value_type is None and not self.type_variable:
            raise ValueError("value ports require a concrete type or type variable")
        if self.kind is not PortKind.VALUE and (self.value_type is not None or self.type_variable):
            raise ValueError("exec and event ports cannot carry a value type")
        if self.kind is not PortKind.VALUE and self.dimension_policy is not PortDimensionPolicy.EXACT:
            raise ValueError("only value ports may define a dimension policy")
        if self.dimension_policy is PortDimensionPolicy.FIXED and self.value_type is None:
            raise ValueError("fixed-dimension ports require a concrete value type")
        if self.dimension_policy is PortDimensionPolicy.PROMOTE and not self.type_variable:
            raise ValueError("promoted ports require a shared type variable")
        if self.type_property and (
            self.kind is not PortKind.VALUE
            or self.value_type is not None
        ):
            raise ValueError(
                "property-typed ports must be value ports without a concrete type"
            )
        if (
            self.max_connections is not None
            and (
                type(self.max_connections) is not int
                or self.max_connections < -1
            )
        ):
            raise ValueError(
                "graph port max_connections must be -1, non-negative, or None"
            )


@dataclass(frozen=True)
class PropertyDef:
    id: str
    value_type: TypeRef
    default: Any
    choices: tuple[tuple[str, Any], ...] = ()

    def __post_init__(self) -> None:
        choice_values = [value for _label, value in self.choices]
        if len(choice_values) != len(set(choice_values)):
            raise ValueError(f"graph property {self.id!r} contains duplicate choices")
        if self.choices and self.default not in choice_values:
            raise ValueError(
                f"graph property {self.id!r} default must be one of its choices"
            )


@dataclass(frozen=True)
class NodePresentation:
    """Optional authoring chrome owned by the strict node definition.

    ``None`` and empty values intentionally delegate to the graph frontend's
    domain defaults.  A domain that needs distinct chrome declares it beside
    its ports and properties instead of maintaining a parallel ``NodeTypeDef``.
    """

    header_color: tuple[float, float, float, float] | None = None
    min_width: float | None = None
    deletable: bool | None = None
    body_bottom_pad: float | None = None
    visual_style: str = ""
    category_label: str = ""
    show_header_color_swatch: bool | None = None

    def __post_init__(self) -> None:
        if self.header_color is not None:
            if len(self.header_color) != 4:
                raise ValueError("node presentation header_color must contain RGBA")
            object.__setattr__(
                self,
                "header_color",
                tuple(float(value) for value in self.header_color),
            )
        for field_name in ("min_width", "body_bottom_pad"):
            value = getattr(self, field_name)
            if value is not None and float(value) < 0.0:
                raise ValueError(f"node presentation {field_name} cannot be negative")
        if self.deletable is not None and type(self.deletable) is not bool:
            raise TypeError("node presentation deletable must be bool or None")
        if (
            self.show_header_color_swatch is not None
            and type(self.show_header_color_swatch) is not bool
        ):
            raise TypeError(
                "node presentation show_header_color_swatch must be bool or None"
            )


@dataclass(frozen=True)
class NodeDef:
    type_id: str
    display_name: str
    ports: tuple[PortDef, ...]
    properties: tuple[PropertyDef, ...] = ()
    target_opcodes: Mapping[str, str] = field(default_factory=dict)
    presentation: NodePresentation = field(default_factory=NodePresentation)

    def __post_init__(self) -> None:
        if not self.type_id or "." not in self.type_id:
            raise ValueError("node type id must be a namespaced identifier")
        port_ids = [port.id for port in self.ports]
        if len(port_ids) != len(set(port_ids)):
            raise ValueError(f"node {self.type_id!r} contains duplicate port ids")
        property_ids = [item.id for item in self.properties]
        if len(property_ids) != len(set(property_ids)):
            raise ValueError(f"node {self.type_id!r} contains duplicate property ids")
        for port in self.ports:
            if port.type_property and port.type_property not in property_ids:
                raise ValueError(
                    f"node {self.type_id!r} port {port.id!r} references unknown "
                    f"type property {port.type_property!r}"
                )

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
    "NodePresentation",
    "PortDef",
    "PortDimensionPolicy",
    "PortDirection",
    "PortKind",
    "PropertyDef",
]
