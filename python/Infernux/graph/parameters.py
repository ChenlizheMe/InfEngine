"""Portable parameter schema shared by every authored graph domain."""

from __future__ import annotations

import copy
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Mapping
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

    def index_of(self, stable_id: str) -> int:
        return next(
            (
                index
                for index, parameter in enumerate(self._parameters)
                if parameter.stable_id == str(stable_id)
            ),
            -1,
        )

    def require(self, stable_id: str) -> GraphParameterDefinition:
        parameter = self.find(stable_id)
        if parameter is None:
            raise KeyError(f"graph parameter not found: {stable_id!r}")
        return parameter

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

    def move(
        self, stable_id: str, target_index: int
    ) -> "GraphParameterCollection":
        """Move one parameter to its final index without changing identity."""
        source_index = self.index_of(stable_id)
        if source_index < 0:
            raise KeyError(f"graph parameter not found: {stable_id!r}")
        if type(target_index) is not int:
            raise TypeError("graph parameter target_index must be an integer")
        if target_index < 0 or target_index >= len(self._parameters):
            raise IndexError(
                f"graph parameter target_index is out of range: {target_index}"
            )
        if source_index == target_index:
            return self
        values = list(self._parameters)
        parameter = values.pop(source_index)
        values.insert(target_index, parameter)
        return GraphParameterCollection(values)

    def reorder(self, stable_ids: Iterable[str]) -> "GraphParameterCollection":
        """Return the exact stable-ID permutation supplied by the caller."""
        order = tuple(str(stable_id) for stable_id in stable_ids)
        current = tuple(parameter.stable_id for parameter in self._parameters)
        if len(order) != len(set(order)):
            raise ValueError("graph parameter reorder contains duplicate stable IDs")
        missing = sorted(set(current) - set(order))
        unknown = sorted(set(order) - set(current))
        if missing or unknown:
            raise ValueError(
                "graph parameter reorder must be an exact stable-ID permutation; "
                f"missing={missing}, unknown={unknown}"
            )
        if order == current:
            return self
        by_id = {parameter.stable_id: parameter for parameter in self._parameters}
        return GraphParameterCollection(by_id[stable_id] for stable_id in order)


_DEFAULT_UNSET = object()


@dataclass(frozen=True, slots=True)
class GraphParameterEdit:
    """One validated parameter replacement and its immutable collection result."""

    before: GraphParameterDefinition | None
    after: GraphParameterDefinition | None
    index: int
    collection: GraphParameterCollection

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass(frozen=True, slots=True)
class GraphParameterAuthoringPolicy:
    """Domain adapter for the parameter behavior shared by every graph editor.

    The graph layer owns identity, uniqueness, type/space parsing, default reset,
    and immutable edits. Domains only declare their parameter subtype and legal
    value contract; dependency rebuilds remain in the domain editor.
    """

    parameter_type: type[GraphParameterDefinition]
    value_types: tuple[ValueType, ...]
    default_for_type: Callable[[ValueType], Any]
    writable_types: frozenset[ValueType] | None = None
    allowed_spaces: Mapping[ValueType, tuple[CoordinateSpace, ...]] = field(
        default_factory=dict
    )
    normalize_name: Callable[[str], str] = str.strip

    def __post_init__(self) -> None:
        if not issubclass(self.parameter_type, GraphParameterDefinition):
            raise TypeError("graph parameter policy type must extend GraphParameterDefinition")
        kinds = tuple(ValueType(kind) for kind in self.value_types)
        if not kinds or len(kinds) != len(set(kinds)):
            raise ValueError("graph parameter policy value types must be non-empty and unique")
        spaces = {
            ValueType(kind): tuple(CoordinateSpace(space) for space in values)
            for kind, values in self.allowed_spaces.items()
        }
        unknown_space_types = set(spaces) - set(kinds)
        if unknown_space_types:
            raise ValueError(
                "graph parameter policy declares spaces for unsupported types: "
                f"{sorted(kind.value for kind in unknown_space_types)}"
            )
        if any(not values or len(values) != len(set(values)) for values in spaces.values()):
            raise ValueError("graph parameter policy spaces must be non-empty and unique")
        writable = self.writable_types
        if writable is not None:
            writable = frozenset(ValueType(kind) for kind in writable)
            unknown_writable = writable - set(kinds)
            if unknown_writable:
                raise ValueError(
                    "graph parameter policy marks unsupported types writable: "
                    f"{sorted(kind.value for kind in unknown_writable)}"
                )
        object.__setattr__(self, "value_types", kinds)
        object.__setattr__(self, "allowed_spaces", spaces)
        object.__setattr__(self, "writable_types", writable)

    def _type_ref(self, encoded: Any) -> TypeRef:
        value_type = (
            TypeRef.from_dict(encoded)
            if type(encoded) is dict
            else encoded
            if isinstance(encoded, TypeRef)
            else TypeRef(encoded)
            if isinstance(encoded, ValueType)
            else TypeRef(ValueType(str(encoded)))
        )
        if value_type.value_type not in self.value_types:
            raise ValueError(
                f"unsupported graph parameter type: {value_type.value_type.value!r}"
            )
        allowed = self.allowed_spaces.get(
            value_type.value_type,
            (CoordinateSpace.NONE,),
        )
        if value_type.space not in allowed:
            raise ValueError(
                f"graph parameter type {value_type.value_type.value!r} does not "
                f"support coordinate space {value_type.space.value!r}"
            )
        return value_type

    def _name(self, raw: Any) -> str:
        name = str(self.normalize_name(str(raw))).strip()
        if not name:
            raise ValueError("graph parameter name cannot be empty")
        return name

    def _writable(self, kind: ValueType, requested: Any) -> bool:
        writable = bool(requested)
        if self.writable_types is not None and kind not in self.writable_types:
            return False
        return writable

    def create(
        self,
        collection: GraphParameterCollection,
        *,
        name: str,
        value_type: TypeRef | ValueType | str | dict[str, Any],
        default: Any = _DEFAULT_UNSET,
        stable_id: str = "",
        exposed: bool = True,
        writable: bool = False,
        category: str = "",
        tooltip: str = "",
        attributes: Iterable[str] = (),
        index: int = -1,
    ) -> GraphParameterEdit:
        type_ref = self._type_ref(value_type)
        parameter = self.parameter_type(
            stable_id=str(stable_id).strip() or uuid.uuid4().hex,
            name=self._name(name),
            value_type=type_ref,
            default=copy.deepcopy(
                self.default_for_type(type_ref.value_type)
                if default is _DEFAULT_UNSET
                else default
            ),
            exposed=bool(exposed),
            writable=self._writable(type_ref.value_type, writable),
            category=str(category),
            tooltip=str(tooltip),
            attributes=tuple(attributes),
        )
        updated = collection.insert(parameter, index)
        return GraphParameterEdit(
            before=None,
            after=parameter,
            index=updated.index_of(parameter.stable_id),
            collection=updated,
        )

    def update(
        self,
        collection: GraphParameterCollection,
        stable_id: str,
        values: Mapping[str, Any],
    ) -> GraphParameterEdit:
        if type(values) is not dict or not values:
            raise ValueError("graph parameter update must be a non-empty object")
        current = collection.require(stable_id)
        if not isinstance(current, self.parameter_type):
            raise TypeError(
                f"graph parameter {stable_id!r} does not match the domain policy"
            )
        normalized = dict(values)
        if "name" in normalized:
            normalized["name"] = self._name(normalized["name"])
        encoded_type = normalized.get(
            "value_type",
            normalized.get("type", current.value_type),
        )
        type_ref = self._type_ref(encoded_type)
        if "type" in normalized:
            normalized["type"] = type_ref
        if "value_type" in normalized:
            normalized["value_type"] = type_ref
        if type_ref != current.value_type and "default" not in normalized:
            normalized["default"] = copy.deepcopy(
                self.default_for_type(type_ref.value_type)
            )
        normalized["writable"] = self._writable(
            type_ref.value_type,
            normalized.get("writable", current.writable),
        )
        replacement = current.with_updates(normalized)
        if not isinstance(replacement, self.parameter_type):
            raise TypeError("graph parameter update changed its domain type")
        updated = collection.replace(replacement)
        return GraphParameterEdit(
            before=current,
            after=replacement,
            index=collection.index_of(stable_id),
            collection=updated,
        )


__all__ = [
    "GraphParameterAuthoringPolicy",
    "GraphParameterCollection",
    "GraphParameterDefinition",
    "GraphParameterEdit",
]
