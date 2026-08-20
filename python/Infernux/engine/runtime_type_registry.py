"""Build-authored Python component type and lifecycle contract for Player."""

from __future__ import annotations

import json
import os
from typing import Any, Optional


RUNTIME_TYPE_REGISTRY_SCHEMA = "infernux.runtime_type_registry"
RUNTIME_TYPE_REGISTRY_VERSION = 1
_runtime_types: dict[str, dict[str, Any]] = {}
_runtime_registry_installed = False
_RUNTIME_LIFECYCLE_METHODS = frozenset(
    {
        "awake",
        "start",
        "fixed_update",
        "update",
        "late_update",
        "on_enable",
        "on_disable",
        "on_destroy",
        "on_collision_enter",
        "on_collision_stay",
        "on_collision_exit",
        "on_trigger_enter",
        "on_trigger_stay",
        "on_trigger_exit",
    }
)


def install_runtime_type_registry(path: str) -> int:
    with open(path, "r", encoding="utf-8") as stream:
        document = json.load(stream)
    if document.get("$schema") != RUNTIME_TYPE_REGISTRY_SCHEMA:
        raise RuntimeError("Unsupported Player runtime type registry schema")
    if document.get("registry_version") != RUNTIME_TYPE_REGISTRY_VERSION:
        raise RuntimeError("Unsupported Player runtime type registry version")
    entries = document.get("types")
    if not isinstance(entries, list):
        raise RuntimeError("Player runtime type registry has no type list")

    prepared: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("Player runtime type registry contains a malformed entry")
        required = ("script_guid", "type_guid", "module", "qualname", "runtime_path")
        if any(not isinstance(entry.get(key), str) or not entry[key] for key in required):
            raise RuntimeError("Player runtime type registry entry has incomplete identity")
        phases = entry.get("lifecycle", [])
        if not isinstance(phases, list) or any(
            not isinstance(phase, str) or not phase for phase in phases
        ):
            raise RuntimeError("Player runtime type registry entry has invalid lifecycle data")
        if len(phases) != len(set(phases)) or any(
            phase not in _RUNTIME_LIFECYCLE_METHODS for phase in phases
        ):
            raise RuntimeError("Player runtime type registry entry has unknown lifecycle data")
        type_guid = entry["type_guid"]
        if type_guid in prepared:
            raise RuntimeError(f"Duplicate Player runtime component type: {type_guid}")
        prepared[type_guid] = dict(entry, lifecycle=tuple(sorted(set(phases))))

    global _runtime_types, _runtime_registry_installed
    _runtime_types = prepared
    _runtime_registry_installed = True
    return len(prepared)


def runtime_type_contract(type_guid: str) -> Optional[dict[str, Any]]:
    record = _runtime_types.get(str(type_guid))
    return dict(record) if record is not None else None


def validate_runtime_component_identity(
    *,
    script_guid: str,
    type_guid: str,
    module_name: str,
    qualified_name: str,
) -> Optional[dict[str, Any]]:
    if not _runtime_registry_installed:
        if os.environ.get("_INFERNUX_PLAYER_MODE") == "1":
            raise RuntimeError("RuntimeTypeRegistry is not installed for Player")
        return None
    record = _runtime_types.get(type_guid)
    if record is None:
        raise RuntimeError(f"Player component type is absent from RuntimeTypeRegistry: {type_guid}")
    expected = {
        "script_guid": script_guid,
        "module": module_name,
        "qualname": qualified_name,
    }
    mismatches = [
        key for key, value in expected.items() if record.get(key) != value
    ]
    if mismatches:
        raise RuntimeError(
            "Player component identity disagrees with RuntimeTypeRegistry: "
            + ", ".join(mismatches)
        )
    return dict(record)


def bind_runtime_lifecycle_contract(component_type: type, record: Optional[dict[str, Any]]) -> None:
    if record is None:
        if os.environ.get("_INFERNUX_PLAYER_MODE") == "1":
            raise RuntimeError("Player component has no RuntimeTypeRegistry contract")
        return
    lifecycle = frozenset(str(item) for item in record.get("lifecycle", ()))
    actual = frozenset(
        name
        for name in _RUNTIME_LIFECYCLE_METHODS
        if callable(getattr(component_type, name, None))
        and name in component_type.__dict__
    )
    if actual != lifecycle:
        raise RuntimeError(
            f"Player component lifecycle differs from RuntimeTypeRegistry: "
            f"{component_type.__module__}.{component_type.__qualname__}"
        )
    component_type._runtime_declared_phases_ = frozenset(
        lifecycle.intersection({"fixed_update", "update", "late_update"})
    )


def clear_runtime_type_registry() -> None:
    global _runtime_types, _runtime_registry_installed
    _runtime_types = {}
    _runtime_registry_installed = False


__all__ = [
    "RUNTIME_TYPE_REGISTRY_SCHEMA",
    "RUNTIME_TYPE_REGISTRY_VERSION",
    "bind_runtime_lifecycle_contract",
    "clear_runtime_type_registry",
    "install_runtime_type_registry",
    "runtime_type_contract",
    "validate_runtime_component_identity",
]
