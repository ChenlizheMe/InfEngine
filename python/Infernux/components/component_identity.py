"""Deterministic identities for Python component scripts and concrete types."""
from __future__ import annotations

import uuid


_SCRIPT_NAMESPACE = uuid.UUID("594f85cc-9c3a-4ea9-93ed-65a26f77e3a4")
_TYPE_NAMESPACE = uuid.UUID("41934666-ab60-4a29-b7ae-c8e15faf83c2")


def intrinsic_script_guid(module_name: str) -> str:
    if not isinstance(module_name, str) or not module_name:
        raise ValueError("Python component module name must be non-empty")
    return uuid.uuid5(_SCRIPT_NAMESPACE, module_name).hex


def component_type_guid(script_or_module_key: str, qualified_name: str) -> str:
    """Stable type identity keyed by script GUID (preferred) or module name.

    Asset-backed scripts should pass the AssetDatabase script GUID so renames
    that preserve the GUID do not invalidate serialized ComponentRecords.
    """
    if not isinstance(script_or_module_key, str) or not script_or_module_key:
        raise ValueError("Python component identity key must be non-empty")
    if not isinstance(qualified_name, str) or not qualified_name:
        raise ValueError("Python component qualified name must be non-empty")
    return uuid.uuid5(_TYPE_NAMESPACE, f"{script_or_module_key}:{qualified_name}").hex


def bind_asset_script_guid(component_type: type, script_guid: str) -> str:
    """Rebind a loaded script class to its AssetDatabase GUID.

    Returns the asset-stable type GUID used for serialization after the bind.
    """
    if not isinstance(script_guid, str) or not script_guid:
        raise ValueError("Asset script GUID must be non-empty")
    component_type._asset_script_guid_ = script_guid
    type_guid = component_type_guid(script_guid, component_type.__qualname__)
    component_type._type_guid_ = type_guid
    from .registry import register_component_type
    register_component_type(component_type)
    # __init_subclass__ sees only the provisional module identity. Register
    # the asset-stable key as well before any instance allocates its CDS slot.
    from ._cds_bridge import register_class
    register_class(component_type)
    return type_guid
