"""PlayModeSerializationMixin — extracted from PlayModeManager."""
from __future__ import annotations

"""
PlayMode - Runtime/Editor mode manager for Infernux.

Manages the play mode state machine:
- Edit Mode: Normal editor state, scene changes are persistent
- Play Mode: Runtime simulation, scene changes are temporary
- Pause Mode: Runtime paused, can step frame by frame

Handles:
- Scene state save/restore for play mode isolation (Unity-style)
- Delta time management
- Python component recreation after scene restore
"""

import time
import os
from enum import Enum, auto
from typing import Optional, List, Dict, Any, Callable, TYPE_CHECKING
from dataclasses import dataclass
from Infernux.debug import Debug, LogType
from Infernux.engine.project_context import resolve_script_path


class PlayModeSerializationMixin:
    """PlayModeSerializationMixin method group for PlayModeManager."""

    def _serialize_py_component(self, component: 'InxComponent') -> Dict[str, Any]:
        """Serialize Python component fields and metadata.

        Uses the component's ``_serialize_value`` so that ref wrappers
        (GameObjectRef, MaterialRef) are converted to JSON-safe dicts.
        """
        from Infernux.components.missing_script import MissingScript
        from Infernux.components.fields import get_raw_field_value, get_serialized_fields

        if isinstance(component, MissingScript):
            preserved = component._serialize_fields_document()
            data = {
                name: value for name, value in preserved.items()
                if name not in {"__type_name__", "__component_id__"}
            }
        else:
            fields = get_serialized_fields(component.__class__)
            data = {}
            for name, meta in fields.items():
                raw = get_raw_field_value(component, name)
                data[name] = component._serialize_value(raw)

        script_guid = getattr(component, "_script_guid", None)
        type_guid = component.__class__._get_type_guid()

        return {
            "type_name": getattr(component, "type_name", component.__class__.__name__),
            "script_guid": script_guid,
            "type_guid": type_guid,
            "component_id": int(getattr(component, "component_id", 0) or 0),
            "module_name": component.__class__.__module__,
            "qualified_name": component.__class__.__qualname__,
            "enabled": getattr(component, "enabled", True),
            "fields": data,
        }

    def _apply_py_component_state(self, component: 'InxComponent', state: Dict[str, Any]):
        """Apply serialized field values to a Python component instance.

        Uses ``_deserialize_value`` so that JSON dicts produced by
        ``_serialize_py_component`` are correctly reconstructed into
        GameObjectRef / MaterialRef / enum values.
        """
        if not state or component is None:
            return
        state_script_guid = state.get("script_guid") or ""
        live_script_guid = getattr(component, "_script_guid", "") or ""
        if state_script_guid and live_script_guid and state_script_guid != live_script_guid:
            raise ValueError("Play Mode component script GUID changed during state restore")
        # type_guid may change after a class rename; script GUID is authoritative.
        component.enabled = bool(state.get("enabled", True))
        component_id = state.get("component_id")
        if type(component_id) is int and component_id > 0:
            component._component_id = component_id
            from Infernux.components.component import InxComponent
            with InxComponent._id_lock:
                if InxComponent._next_component_id <= component_id:
                    InxComponent._next_component_id = component_id + 1

        fields = state.get("fields", {})
        
        # Get the new class's serialized fields - only restore fields that still exist
        from Infernux.components.fields import get_serialized_fields
        new_serialized_fields = get_serialized_fields(component.__class__)

        previous_deserializing = getattr(component, '_inf_deserializing', False)
        component._inf_deserializing = True
        try:
            for name, value in fields.items():
                # Only restore if the field still exists in the new class definition
                if name not in new_serialized_fields:
                    continue
                meta = new_serialized_fields[name]
                value = component._deserialize_value(value, meta)
                setattr(component, name, value)
        finally:
            component._inf_deserializing = previous_deserializing

        if state.get("script_guid"):
            component._script_guid = state.get("script_guid")

