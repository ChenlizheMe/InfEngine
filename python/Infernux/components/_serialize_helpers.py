"""
Shared serialization helpers for InxComponent and SerializableObject.

Eliminates the duplicate dict-key ref dispatch and asset-ref creation
boilerplate that was copy-pasted between component.py and
serializable_object.py.
"""

from __future__ import annotations

from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .fields import FieldMetadata


# ──────────────────────────────────────────────────────────────────────
# Asset reference typed documents
# ──────────────────────────────────────────────────────────────────────

def _serialize_asset_ref(value: Any) -> Optional[dict]:
    """Serialize an asset-ref-like object to its canonical dict form.

    Returns None if *value* is not a recognised asset-ref type.
    """
    from Infernux.core.asset_ref import TextureRef, ShaderRef, AssetRefBase, get_asset_type_for_ref
    from .value_document import make_asset_ref

    if isinstance(value, TextureRef):
        return make_asset_ref("Texture", value.guid, value.path_hint)
    if isinstance(value, ShaderRef):
        return make_asset_ref("Shader", value.guid, value.path_hint)
    if isinstance(value, AssetRefBase):
        asset_type = get_asset_type_for_ref(value)
        if asset_type is not None:
            return make_asset_ref(asset_type, value.guid, value.path_hint)

    return None


# ──────────────────────────────────────────────────────────────────────
# Vector serialization
# ──────────────────────────────────────────────────────────────────────

def serialize_vec(value: Any) -> Optional[list]:
    """Serialize a vec-like object (has x, y, [z, [w]]) to a float list.

    Returns None if *value* is not vec-like.
    """
    if hasattr(value, "x") and hasattr(value, "y"):
        if hasattr(value, "z"):
            if hasattr(value, "w"):
                return [float(value.x), float(value.y), float(value.z), float(value.w)]
            return [float(value.x), float(value.y), float(value.z)]
        return [float(value.x), float(value.y)]
    return None


# ──────────────────────────────────────────────────────────────────────
# Typed value-document deserialization dispatch
# ──────────────────────────────────────────────────────────────────────

def deserialize_dict_ref(value: dict) -> Any:
    """Attempt to deserialize a dict into the appropriate ref wrapper.

    Returns the deserialized ref object, or *value* unchanged if no
    recognised dict-key marker was found.
    """
    from .value_document import (
        TYPE_KEY,
        GAME_OBJECT_REF,
        COMPONENT_REF,
        ASSET_REF,
        SERIALIZABLE_OBJECT,
    )

    document_type = value.get(TYPE_KEY)
    if document_type == GAME_OBJECT_REF:
        from .ref_wrappers import GameObjectRef
        return GameObjectRef(persistent_id=value["object_id"])
    if document_type == COMPONENT_REF:
        from .ref_wrappers import ComponentRef
        return ComponentRef._from_dict(value)
    if document_type == ASSET_REF:
        asset_type = value["asset_type"]
        guid = value["guid"]
        path_hint = value["path_hint"]
        if asset_type == "Prefab":
            from .ref_wrappers import PrefabRef
            return PrefabRef(guid=guid, path_hint=path_hint)
        if asset_type == "Material":
            from .ref_wrappers import MaterialRef
            return MaterialRef(guid=guid, path_hint=path_hint)
        if asset_type == "Texture":
            from Infernux.core.asset_ref import TextureRef
            return TextureRef(guid=guid, path_hint=path_hint)
        if asset_type == "Shader":
            from Infernux.core.asset_ref import ShaderRef
            return ShaderRef(guid=guid, path_hint=path_hint)
        from Infernux.core.asset_ref import create_asset_ref

        return create_asset_ref(asset_type, guid=guid, path_hint=path_hint)
    if document_type == SERIALIZABLE_OBJECT:
        from .serializable_object import SerializableObject
        return SerializableObject._deserialize(value)

    return value


# ──────────────────────────────────────────────────────────────────────
# Null-value factory for ref field types
# ──────────────────────────────────────────────────────────────────────

def make_null_ref(field_type, field_meta=None) -> Any:
    """Return an empty/null ref for the given FieldType.

    Used when a serialized value is None but the field type implies a
    non-None wrapper (e.g. GameObjectRef(persistent_id=0)).
    """
    from .fields import FieldType

    if field_type == FieldType.GAME_OBJECT:
        from .ref_wrappers import GameObjectRef
        return GameObjectRef(persistent_id=0)
    if field_type == FieldType.MATERIAL:
        from .ref_wrappers import MaterialRef
        return MaterialRef(guid="")
    if field_type == FieldType.TEXTURE:
        from Infernux.core.asset_ref import TextureRef
        return TextureRef()
    if field_type == FieldType.SHADER:
        from Infernux.core.asset_ref import ShaderRef
        return ShaderRef()
    if field_type == FieldType.ASSET:
        asset_type = str(getattr(field_meta, "asset_type", "") or "").strip()
        if not asset_type:
            raise ValueError("ASSET fields require an explicit asset_type")
        from Infernux.core.asset_reference_types import asset_type_registry

        asset_type = asset_type_registry.require(asset_type).type_id
        from Infernux.core.asset_ref import create_asset_ref

        return create_asset_ref(asset_type)
    if field_type == FieldType.COMPONENT:
        from .ref_wrappers import ComponentRef
        comp_type = getattr(field_meta, "component_type", "") or ""
        return ComponentRef(component_type=comp_type)
    if field_type == FieldType.SERIALIZABLE_OBJECT:
        so_cls = getattr(field_meta, "serializable_class", None)
        if so_cls is not None:
            return so_cls()
        return None
    return None
