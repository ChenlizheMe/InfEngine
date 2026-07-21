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
        from Infernux.components.serialized_field import get_raw_field_value, get_serialized_fields

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
        from Infernux.components.serialized_field import get_serialized_fields
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

    def _materialize_prefab_references_for_play(self):
        """Instantiate prefab-backed GameObject refs before runtime lifecycle begins."""
        from Infernux.components.component import InxComponent

        self.clear_runtime_hidden_object_ids()
        total_materialized = 0
        max_passes = 32
        for _ in range(max_passes):
            pass_materialized = 0
            active_components = []
            for components in list(InxComponent._active_instances.values()):
                for component in list(components):
                    if component is None or getattr(component, "_is_destroyed", False):
                        continue
                    active_components.append(component)

            if not active_components:
                break

            for component in active_components:
                pass_materialized += self._materialize_prefab_refs_on_owner(component)

            total_materialized += pass_materialized
            if pass_materialized == 0:
                break
        else:
            Debug.log_warning(
                "Stopped prefab GameObject materialization after 32 passes. "
                "Check for recursive prefab references."
            )

        if total_materialized > 0:
            Debug.log_internal(
                f"Materialized {total_materialized} prefab GameObject reference(s) for Play Mode"
            )

    def _materialize_prefab_refs_on_owner(self, owner) -> int:
        from Infernux.components.component import InxComponent
        from Infernux.components.serialized_field import get_serialized_fields, get_raw_field_value

        fields = get_serialized_fields(owner.__class__)
        if not fields:
            return 0

        materialized = 0
        is_component = isinstance(owner, InxComponent)
        previous_deserializing = getattr(owner, "_inf_deserializing", False) if is_component else False
        if is_component:
            owner._inf_deserializing = True
        try:
            for name, meta in fields.items():
                raw_value = get_raw_field_value(owner, name)
                new_value, changed = self._materialize_prefab_refs_in_value(raw_value, meta)
                if changed:
                    setattr(owner, name, new_value)
                    materialized += changed
        finally:
            if is_component:
                owner._inf_deserializing = previous_deserializing

        return materialized

    def _materialize_prefab_refs_in_value(self, value, field_meta_or_type):
        from Infernux.components.ref_wrappers import PrefabRef
        from Infernux.components.serializable_object import SerializableObject
        from Infernux.components.serialized_field import FieldType

        if hasattr(field_meta_or_type, "field_type"):
            field_type = field_meta_or_type.field_type
            element_type = getattr(field_meta_or_type, "element_type", None)
        else:
            field_type = field_meta_or_type
            element_type = None

        if field_type == FieldType.GAME_OBJECT:
            if isinstance(value, PrefabRef):
                instance = value.instantiate()
                if instance is not None:
                    self.register_runtime_hidden_object(instance)
                    return instance, 1
            return value, 0

        if field_type == FieldType.SERIALIZABLE_OBJECT:
            if isinstance(value, SerializableObject):
                return value, self._materialize_prefab_refs_on_owner(value)
            return value, 0

        if field_type == FieldType.LIST and isinstance(value, list):
            if element_type == FieldType.GAME_OBJECT:
                materialized = 0
                new_items = []
                for item in value:
                    if isinstance(item, PrefabRef):
                        instance = item.instantiate()
                        if instance is not None:
                            self.register_runtime_hidden_object(instance)
                            new_items.append(instance)
                            materialized += 1
                            continue
                    new_items.append(item)
                return (new_items if materialized else value), materialized

            if element_type == FieldType.SERIALIZABLE_OBJECT:
                materialized = 0
                for item in value:
                    if isinstance(item, SerializableObject):
                        materialized += self._materialize_prefab_refs_on_owner(item)
                return value, materialized

        return value, 0

