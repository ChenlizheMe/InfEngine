"""Placeholder component used when a Python script asset cannot be resolved."""

from __future__ import annotations

from typing import Any, Optional

from .component import InxComponent


class MissingScript(InxComponent):
    """Keeps a GameObject loadable when its script asset is missing or unloadable.

    The original ComponentRecord identity and field payload are preserved so a
    later rename/restore can rebind without losing authored data.
    """

    _uses_component_data_store = False
    _registers_active_instance = False
    _component_category_ = "Scripts"
    _is_broken = True

    def _serialize_fields_document(self) -> dict[str, Any]:
        preserved = getattr(self, "_preserved_fields", None)
        if isinstance(preserved, dict):
            document = dict(preserved)
            document["__type_name__"] = self._component_name
            document["__component_id__"] = self._component_id
            return document
        return {
            "__type_name__": self._component_name,
            "__component_id__": self._component_id,
        }

    def _deserialize_fields_document(
        self,
        data: dict[str, Any],
        *,
        _skip_on_after_deserialize: bool = False,
        repair: bool = False,
    ) -> None:
        if not isinstance(data, dict):
            raise TypeError("MissingScript fields document must be an object")
        self._preserved_fields = dict(data)
        saved_id = data.get("__component_id__")
        if type(saved_id) is int and saved_id > 0:
            self._component_id = saved_id
        type_name = data.get("__type_name__")
        if isinstance(type_name, str) and type_name:
            self._component_name = type_name


def create_missing_script_component(
    *,
    type_name: str,
    script_guid: str,
    type_guid: str,
    module_name: str,
    qualified_name: str,
    fields: dict[str, Any],
    error: str,
) -> MissingScript:
    """Build a broken placeholder that preserves the authored component identity."""
    if not type_name or not script_guid or not type_guid:
        raise ValueError("MissingScript requires type_name, script_guid, and type_guid")

    # Unique subclass so PyComponentProxy reads the original type name / GUIDs.
    cls = type(
        type_name,
        (MissingScript,),
        {
            "__module__": module_name or "infernux.missing_script",
            "__qualname__": qualified_name or type_name,
            "_uses_component_data_store": False,
            "_registers_active_instance": False,
        },
    )
    # __init_subclass__ derives provisional module-based IDs; overwrite with the
    # authored ComponentRecord identity so save/load round-trips stay stable.
    cls._type_guid_ = type_guid
    cls._asset_script_guid_ = script_guid
    cls._intrinsic_script_guid_ = script_guid
    from Infernux.engine.runtime_dispatch import publish_runtime_dispatch_epoch

    publication = publish_runtime_dispatch_epoch((cls,))
    publication.commit()

    instance = cls()
    instance._is_broken = True
    instance._broken_error = error
    instance._script_guid = script_guid
    instance._component_name = type_name
    instance._preserved_fields = dict(fields)
    return instance
