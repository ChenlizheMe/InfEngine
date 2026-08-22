"""Stable editor-window identities and explicit panel view-state schemas."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


_MISSING = object()


def _validated_view_state_value(value: Any, *, path: str) -> Any:
    """Return a detached JSON-safe view-state value or reject it.

    Panel state is deliberately narrower than general serialization. Runtime
    objects, document payloads, caches, and non-finite values must never leak
    into the editor session file.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"panel view state '{path}' must be finite")
        return value
    if isinstance(value, (list, tuple)):
        return [
            _validated_view_state_value(item, path=f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"panel view state '{path}' requires string keys")
            result[key] = _validated_view_state_value(item, path=f"{path}.{key}")
        return result
    raise TypeError(
        f"panel view state '{path}' cannot store runtime value {type(value).__name__}"
    )


@dataclass(frozen=True, slots=True)
class PanelViewStateField:
    """Declare one persisted presentation-only attribute on a panel."""

    key: str
    attribute: str
    value_type: type | tuple[type, ...]
    default: Any = _MISSING

    def __post_init__(self) -> None:
        key = str(self.key or "").strip()
        attribute = str(self.attribute or "").strip()
        if not key:
            raise ValueError("panel view-state field requires a key")
        if not attribute:
            raise ValueError("panel view-state field requires an attribute")
        if not isinstance(self.value_type, (type, tuple)):
            raise TypeError("panel view-state field requires an explicit value type")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "attribute", attribute)

    def capture(self, panel: object) -> Any:
        owner, leaf = self._resolve_owner(panel)
        if not hasattr(owner, leaf):
            if self.default is _MISSING:
                raise AttributeError(
                    f"panel does not define declared view-state attribute '{self.attribute}'"
                )
            value = self.default
        else:
            value = getattr(owner, leaf)
        if not isinstance(value, self.value_type):
            raise TypeError(
                f"panel view-state field '{self.key}' requires {self.value_type}, "
                f"got {type(value).__name__}"
            )
        return _validated_view_state_value(value, path=self.key)

    def restore(self, panel: object, value: Any) -> None:
        if not isinstance(value, self.value_type):
            raise TypeError(
                f"persisted panel view-state field '{self.key}' requires "
                f"{self.value_type}, got {type(value).__name__}"
            )
        owner, leaf = self._resolve_owner(panel)
        setattr(owner, leaf, _validated_view_state_value(value, path=self.key))

    def _resolve_owner(self, panel: object) -> tuple[object, str]:
        parts = self.attribute.split(".")
        owner = panel
        for part in parts[:-1]:
            if not hasattr(owner, part):
                raise AttributeError(
                    f"panel does not define declared view-state path '{self.attribute}'"
                )
            owner = getattr(owner, part)
        return owner, parts[-1]


@dataclass(frozen=True, slots=True)
class PanelViewStateSchema:
    """Versioned, allow-listed presentation state for an editor panel."""

    schema_id: str
    fields: tuple[PanelViewStateField, ...] = ()
    version: int = 1

    def __post_init__(self) -> None:
        schema_id = str(self.schema_id or "").strip()
        fields = tuple(self.fields)
        if not schema_id:
            raise ValueError("panel view-state schema requires an id")
        if int(self.version) <= 0:
            raise ValueError("panel view-state schema version must be positive")
        if len({field.key for field in fields}) != len(fields):
            raise ValueError("panel view-state schema keys must be unique")
        if len({field.attribute for field in fields}) != len(fields):
            raise ValueError("panel view-state schema attributes must be unique")
        object.__setattr__(self, "schema_id", schema_id)
        object.__setattr__(self, "fields", fields)
        object.__setattr__(self, "version", int(self.version))

    def capture(self, panel: object) -> dict[str, Any]:
        return {
            "schema": self.schema_id,
            "version": self.version,
            "values": {field.key: field.capture(panel) for field in self.fields},
        }

    def restore(self, panel: object, data: dict[str, Any]) -> None:
        if not isinstance(data, dict) or set(data) != {"schema", "version", "values"}:
            raise ValueError("panel view state does not match the declared schema envelope")
        if data["schema"] != self.schema_id or data["version"] != self.version:
            raise ValueError(
                f"panel view state requires {self.schema_id} v{self.version}"
            )
        values = data["values"]
        if not isinstance(values, dict):
            raise TypeError("panel view-state values must be an object")
        expected = {field.key for field in self.fields}
        if set(values) != expected:
            raise ValueError("panel view-state fields do not match the declared schema")
        for field in self.fields:
            field.restore(panel, values[field.key])


@dataclass(frozen=True, slots=True)
class WindowLocator:
    """Identify a logical window without retaining its live panel instance."""

    window_id: str
    type_id: str

    def __post_init__(self) -> None:
        window_id = str(self.window_id or "").strip()
        type_id = str(self.type_id or "").strip()
        if not window_id:
            raise ValueError("window locator requires a window_id")
        if not type_id:
            raise ValueError("window locator requires a type_id")
        object.__setattr__(self, "window_id", window_id)
        object.__setattr__(self, "type_id", type_id)
