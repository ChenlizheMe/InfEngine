"""Current typed value-document schema used inside serialized fields."""
from __future__ import annotations


TYPE_KEY = "$type"

ENUM = "enum"
GAME_OBJECT_REF = "game_object_ref"
COMPONENT_REF = "component_ref"
ASSET_REF = "asset_ref"
SERIALIZABLE_OBJECT = "serializable_object"


def make_document(document_type: str, **payload) -> dict:
    return {TYPE_KEY: document_type, **payload}


def make_enum(enum_type: str, name: str) -> dict:
    return make_document(ENUM, enum_type=enum_type, name=name)


def make_game_object_ref(object_id: int) -> dict:
    return make_document(GAME_OBJECT_REF, object_id=object_id)


def make_component_ref(game_object_id: int, component_type: str) -> dict:
    return make_document(
        COMPONENT_REF,
        game_object_id=game_object_id,
        component_type=component_type,
    )


def make_asset_ref(asset_type: str, guid: str, path_hint: str = "") -> dict:
    return make_document(
        ASSET_REF,
        asset_type=asset_type,
        guid=guid,
        path_hint=path_hint,
    )


def make_serializable_object(type_id: str, fields: dict) -> dict:
    return make_document(SERIALIZABLE_OBJECT, type_id=type_id, fields=fields)
