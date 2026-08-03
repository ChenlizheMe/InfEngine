"""Portable parameter schema shared by every authored graph domain."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping
import uuid

from .types import CoordinateSpace, TypeRef, ValueType


@dataclass(frozen=True, slots=True)
class GraphParameterDefinition:
    """Stable, typed Blackboard value independent of a graph runtime.

    Domains may subclass this definition to restrict supported value types or
    runtime mutability. Identity, metadata, serialization, and authoring update
    semantics remain common so every Graph editor can share one parameter
    collection and Detail drawer.
    """

    stable_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "Parameter"
    value_type: TypeRef = field(default_factory=lambda: TypeRef(ValueType.F32))
    default: Any = 0.0
    exposed: bool = True
    writable: bool = False
    category: str = ""
    tooltip: str = ""
    attributes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        stable_id = str(self.stable_id).strip()
        name = str(self.name).strip()
        if not stable_id:
            raise ValueError("graph parameter stable_id cannot be empty")
        if not name:
            raise ValueError("graph parameter name cannot be empty")
        if not isinstance(self.value_type, TypeRef):
            raise TypeError("graph parameter value_type must be a TypeRef")
        if type(self.exposed) is not bool or type(self.writable) is not bool:
            raise TypeError("graph parameter capability flags must be bool values")
        if type(self.category) is not str or type(self.tooltip) is not str:
            raise TypeError("graph parameter metadata must be strings")
        attributes = tuple(str(value).strip() for value in self.attributes)
        if any(not value for value in attributes):
            raise ValueError("graph parameter attributes cannot be empty")
        if len(attributes) != len(set(attributes)):
            raise ValueError("graph parameter attributes must be unique")
        object.__setattr__(self, "stable_id", stable_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "default", copy.deepcopy(self.default))
        object.__setattr__(self, "attributes", attributes)

    def to_dict(self) -> dict[str, Any]:
        return {
            "stable_id": self.stable_id,
            "name": self.name,
            "type": self.value_type.to_dict(),
            "default": copy.deepcopy(self.default),
            "exposed": self.exposed,
            "writable": self.writable,
            "category": self.category,
            "tooltip": self.tooltip,
            "attributes": list(self.attributes),
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        location: str = "graph parameter",
    ) -> "GraphParameterDefinition":
        if type(value) is not dict:
            raise TypeError(f"{location} must be an object")
        expected = {
            "stable_id",
            "name",
            "type",
            "default",
            "exposed",
            "writable",
            "category",
            "tooltip",
            "attributes",
        }
        fields = set(value)
        if fields != expected:
            raise ValueError(
                f"{location} fields mismatch; "
                f"missing={sorted(expected - fields)}, "
                f"unknown={sorted(fields - expected)}"
            )
        attributes = value["attributes"]
        if type(attributes) is not list or any(
            type(item) is not str for item in attributes
        ):
            raise TypeError(f"{location}.attributes must be an array of strings")
        return cls(
            stable_id=value["stable_id"],
            name=value["name"],
            value_type=TypeRef.from_dict(value["type"]),
            default=copy.deepcopy(value["default"]),
            exposed=value["exposed"],
            writable=value["writable"],
            category=value["category"],
            tooltip=value["tooltip"],
            attributes=tuple(attributes),
        )

    def with_updates(self, values: Mapping[str, Any]) -> "GraphParameterDefinition":
        """Return one validated replacement without changing stable identity."""
        if type(values) is not dict or not values:
            raise ValueError("graph parameter update must be a non-empty object")
        allowed = {
            "name",
            "type",
            "value_type",
            "default",
            "exposed",
            "writable",
            "category",
            "tooltip",
            "attributes",
        }
        unknown = set(values) - allowed
        if unknown:
            raise ValueError(f"unknown graph parameter fields: {sorted(unknown)}")
        if "type" in values and "value_type" in values:
            raise ValueError("graph parameter type was supplied twice")
        encoded_type = values.get("value_type", values.get("type", self.value_type))
        value_type = (
            TypeRef.from_dict(encoded_type)
            if type(encoded_type) is dict
            else encoded_type
            if isinstance(encoded_type, TypeRef)
            else TypeRef(ValueType(str(encoded_type)), CoordinateSpace.NONE)
        )
        attributes = values.get("attributes", self.attributes)
        if type(attributes) is list:
            attributes = tuple(attributes)
        return replace(
            self,
            name=values.get("name", self.name),
            value_type=value_type,
            default=copy.deepcopy(values.get("default", self.default)),
            exposed=values.get("exposed", self.exposed),
            writable=values.get("writable", self.writable),
            category=values.get("category", self.category),
            tooltip=values.get("tooltip", self.tooltip),
            attributes=attributes,
        )


class GraphParameterCollection:
    """Validated immutable operations for one Blackboard parameter list."""

    def __init__(self, parameters: Iterable[GraphParameterDefinition] = ()) -> None:
        self._parameters = tuple(parameters)
        if not all(
            isinstance(parameter, GraphParameterDefinition)
            for parameter in self._parameters
        ):
            raise TypeError("graph parameter collection contains an invalid value")
        ids = [parameter.stable_id for parameter in self._parameters]
        names = [parameter.name for parameter in self._parameters]
        if len(ids) != len(set(ids)):
            raise ValueError("graph parameter stable IDs must be unique")
        if len(names) != len(set(names)):
            raise ValueError("graph parameter names must be unique")

    @property
    def values(self) -> tuple[GraphParameterDefinition, ...]:
        return self._parameters

    def find(self, stable_id: str) -> GraphParameterDefinition | None:
        return next(
            (
                parameter
                for parameter in self._parameters
                if parameter.stable_id == str(stable_id)
            ),
            None,
        )

    def insert(
        self, parameter: GraphParameterDefinition, index: int = -1
    ) -> "GraphParameterCollection":
        if self.find(parameter.stable_id) is not None:
            raise ValueError(
                f"graph parameter stable ID already exists: {parameter.stable_id!r}"
            )
        if any(value.name == parameter.name for value in self._parameters):
            raise ValueError(
                f"graph parameter name already exists: {parameter.name!r}"
            )
        values = list(self._parameters)
        target = len(values) if index < 0 else max(0, min(int(index), len(values)))
        values.insert(target, parameter)
        return GraphParameterCollection(values)

    def replace(
        self, parameter: GraphParameterDefinition
    ) -> "GraphParameterCollection":
        index = next(
            (
                index
                for index, value in enumerate(self._parameters)
                if value.stable_id == parameter.stable_id
            ),
            -1,
        )
        if index < 0:
            raise KeyError(f"graph parameter not found: {parameter.stable_id!r}")
        if any(
            value.stable_id != parameter.stable_id
            and value.name == parameter.name
            for value in self._parameters
        ):
            raise ValueError(
                f"graph parameter name already exists: {parameter.name!r}"
            )
        values = list(self._parameters)
        values[index] = parameter
        return GraphParameterCollection(values)

    def remove(self, stable_id: str) -> "GraphParameterCollection":
        if self.find(stable_id) is None:
            raise KeyError(f"graph parameter not found: {stable_id!r}")
        return GraphParameterCollection(
            value
            for value in self._parameters
            if value.stable_id != str(stable_id)
        )


__all__ = ["GraphParameterCollection", "GraphParameterDefinition"]
