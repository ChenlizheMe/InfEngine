"""
Prefab system for Infernux.

Handles saving GameObjects as .prefab files and instantiating them back into scenes.
Prefab files contain a typed GameObject document wrapped in a strict envelope.
"""

import json
import os
import copy

from Infernux.debug import Debug
from Infernux.engine.path_utils import path_key, resolved_path

PREFAB_EXTENSION = ".prefab"
_PREFAB_TEMPLATE_SCENE_NAME = "__InfernuxPrefabTemplateCache__"
_PREFAB_TEMPLATE_CACHE = {}


class PrefabDocumentError(ValueError):
    """Raised when a prefab is not the single active document schema."""


def _validate_game_object_document(
    document: dict,
    location: str = "root_object",
    local_ids: set[int] = None,
) -> None:
    if not isinstance(document, dict):
        raise PrefabDocumentError(f"{location} must be an object")
    if local_ids is None:
        local_ids = set()
    required = {
        "local_id", "name", "active", "is_static", "tag", "layer",
        "transform", "components", "children",
    }
    if set(document) != required:
        missing = sorted(required - set(document))
        unknown = sorted(set(document) - required)
        raise PrefabDocumentError(
            f"{location} fields do not match the current schema; missing={missing}, unknown={unknown}"
        )
    local_id = document["local_id"]
    if type(local_id) is not int or local_id <= 0 or local_id in local_ids:
        raise PrefabDocumentError(f"{location}.local_id must be a unique positive integer")
    local_ids.add(local_id)
    if not isinstance(document["name"], str) or type(document["active"]) is not bool:
        raise PrefabDocumentError(f"{location} has invalid name/active fields")
    if type(document["is_static"]) is not bool or not isinstance(document["tag"], str):
        raise PrefabDocumentError(f"{location} has invalid is_static/tag fields")
    if type(document["layer"]) is not int or not 0 <= document["layer"] < 32:
        raise PrefabDocumentError(f"{location}.layer must be an integer in [0, 31]")
    if not isinstance(document["transform"], dict):
        raise PrefabDocumentError(f"{location}.transform must be an object")
    for field in ("components", "children"):
        if not isinstance(document[field], list):
            raise PrefabDocumentError(f"{location}.{field} must be an array")
    for index, child in enumerate(document["children"]):
        _validate_game_object_document(child, f"{location}.children[{index}]", local_ids)


def _validate_prefab_document(document: dict, file_path: str = "<memory>") -> None:
    if not isinstance(document, dict):
        raise PrefabDocumentError(f"Prefab '{file_path}' must contain an object")
    allowed = {"root_object", "source_canvas_name"}
    required = {"root_object"}
    if not required.issubset(document) or not set(document).issubset(allowed):
        raise PrefabDocumentError(f"Prefab '{file_path}' has missing or unknown envelope fields")
    if "source_canvas_name" in document and not isinstance(document["source_canvas_name"], str):
        raise PrefabDocumentError(f"Prefab '{file_path}' source_canvas_name must be a string")
    _validate_game_object_document(document["root_object"])


def _read_prefab_document(file_path: str) -> dict:
    with open(file_path, "r", encoding="utf-8") as file:
        document = json.load(file)
    _validate_prefab_document(document, file_path)
    return document


def _invalidate_prefab_template_cache(file_path: str = None, guid: str = ""):
    keys_to_remove = set()
    if guid:
        keys_to_remove.add(guid)
    if file_path:
        keys_to_remove.add(path_key(file_path))
    for key in keys_to_remove:
        _PREFAB_TEMPLATE_CACHE.pop(key, None)


def _get_prefab_template_scene():
    from Infernux.lib import SceneManager

    manager = SceneManager.instance()
    scene = manager.get_scene(_PREFAB_TEMPLATE_SCENE_NAME)
    if scene is None:
        scene = manager.create_scene(_PREFAB_TEMPLATE_SCENE_NAME)
        scene.set_playing(False)
    return scene


def _get_file_stamp(file_path: str):
    try:
        stat = os.stat(file_path)
        return (stat.st_mtime_ns, stat.st_size)
    except OSError:
        # Missing file is not an error in the cache-stamp probe path; the
        # caller (cached prefab template lookup) treats None as "no cache
        # entry" and re-reads the prefab from disk if it exists.
        return None


def _load_prefab_template_payload(file_path: str, resolved_guid: str):
    try:
        prefab_data = _read_prefab_document(file_path)
    except (OSError, json.JSONDecodeError, PrefabDocumentError) as exc:
        Debug.log_error(f"Failed to read prefab file: {exc}")
        return None

    root_obj_data = copy.deepcopy(prefab_data["root_object"])
    _strip_prefab_runtime_fields(root_obj_data)
    if resolved_guid:
        _stamp_prefab_guid(root_obj_data, resolved_guid)

    return root_obj_data


def _get_cached_prefab_template(file_path: str, resolved_guid: str, asset_database=None):
    stamp = _get_file_stamp(file_path)
    if stamp is None:
        Debug.log_warning(f"Prefab file not found: {file_path}")
        return None

    cache_key = resolved_guid or path_key(file_path)
    cached = _PREFAB_TEMPLATE_CACHE.get(cache_key)
    if cached and cached.get("stamp") == stamp:
        template = cached.get("template")
        if template is not None:
            return template

    template_payload = _load_prefab_template_payload(file_path, resolved_guid)
    if template_payload is None:
        return None

    template_scene = _get_prefab_template_scene()

    old_template = cached.get("template") if cached else None
    from Infernux.engine.component_restore import instantiate_game_object_document_transactionally
    try:
        template = instantiate_game_object_document_transactionally(
            template_scene,
            template_payload,
            None,
            asset_database,
        )
    except RuntimeError as exc:
        Debug.log_error(f"Failed to preflight cached prefab template: {exc}")
        return None
    if template is None:
        Debug.log_error("Failed to build cached prefab template from JSON.")
        return None

    if old_template is not None:
        template_scene.destroy_game_object(old_template)
        template_scene.process_pending_destroys()

    _PREFAB_TEMPLATE_CACHE[cache_key] = {
        "stamp": stamp,
        "template": template,
    }
    return template


def _strip_prefab_runtime_fields(obj_data: dict):
    if not isinstance(obj_data, dict):
        raise PrefabDocumentError("root_object must be an object")

    nodes = []

    def collect(node: dict, location: str) -> None:
        if not isinstance(node, dict):
            raise PrefabDocumentError(f"{location} must be an object")
        nodes.append((node, location))
        children = node.get("children")
        if not isinstance(children, list):
            raise PrefabDocumentError(f"{location}.children must be an array")
        for index, child in enumerate(children):
            collect(child, f"{location}.children[{index}]")

    collect(obj_data, "root_object")
    has_runtime_ids = ["id" in node for node, _location in nodes]
    has_local_ids = ["local_id" in node for node, _location in nodes]
    if all(has_runtime_ids) and not any(has_local_ids):
        runtime_to_local = {}
        for local_id, (node, location) in enumerate(nodes, start=1):
            runtime_id = node["id"]
            if type(runtime_id) is not int or runtime_id <= 0 or runtime_id in runtime_to_local:
                raise PrefabDocumentError(f"{location}.id must be a unique positive integer")
            runtime_to_local[runtime_id] = local_id
            node["local_id"] = local_id
    elif all(has_local_ids) and not any(has_runtime_ids):
        runtime_to_local = None
    else:
        raise PrefabDocumentError("ObjectGraph must contain either runtime id or local_id on every node")

    def rewrite_references(value, path: str):
        if isinstance(value, list):
            return [rewrite_references(item, f"{path}[{index}]") for index, item in enumerate(value)]
        if not isinstance(value, dict):
            return value
        from Infernux.components.value_document import TYPE_KEY, GAME_OBJECT_REF, COMPONENT_REF
        document_type = value.get(TYPE_KEY)
        if document_type == GAME_OBJECT_REF:
            target_id = value["object_id"]
            if target_id == 0 or runtime_to_local is None:
                return dict(value)
            if target_id not in runtime_to_local:
                raise PrefabDocumentError(f"{path}: prefab cannot reference GameObject {target_id} outside its subtree")
            remapped = dict(value)
            remapped["object_id"] = runtime_to_local[target_id]
            return remapped
        if document_type == COMPONENT_REF:
            target_id = value["game_object_id"]
            if target_id == 0 or runtime_to_local is None:
                return copy.deepcopy(value)
            if target_id not in runtime_to_local:
                raise PrefabDocumentError(f"{path}: prefab cannot reference component outside its subtree")
            remapped = dict(value)
            remapped["game_object_id"] = runtime_to_local[target_id]
            return remapped
        return {key: rewrite_references(item, f"{path}.{key}") for key, item in value.items()}

    next_local_component_id = 1
    for node, location in nodes:
        node.pop("id", None)
        transform = node.get("transform")
        if isinstance(transform, dict):
            transform.pop("component_id", None)
        for index, component in enumerate(node.get("components", [])):
            if not isinstance(component, dict):
                continue
            component["component_id"] = next_local_component_id
            next_local_component_id += 1
            component.pop("instance_guid", None)
            if not isinstance(component.get("data"), dict):
                continue
            component["data"] = rewrite_references(
                component["data"],
                f"{location}.components[{index}].data",
            )


def read_prefab_source_canvas(file_path: str = None, guid: str = None,
                              asset_database=None) -> str:
    """Return the ``source_canvas_name`` stored in a prefab, or ``""``."""
    if not file_path and guid and asset_database:
        file_path = asset_database.get_path_from_guid(guid)
    if not file_path or not os.path.isfile(file_path):
        return ""
    try:
        data = _read_prefab_document(file_path)
        return data.get("source_canvas_name", "")
    except (OSError, json.JSONDecodeError, PrefabDocumentError):
        return ""


def serialize_prefab_document(
    game_object,
    *,
    source_canvas_name: str = "",
    root_document_template: dict = None,
) -> dict:
    """Capture the exact strict prefab document owned by a GameObject tree."""
    if game_object is None:
        raise ValueError("cannot serialize prefab without a GameObject")

    from Infernux.engine.component_restore import (
        serialize_game_object_document_authoritatively,
    )

    go_data = serialize_game_object_document_authoritatively(game_object)
    if not isinstance(go_data, dict):
        raise TypeError("GameObject.serialize_document() did not return a dict")
    if isinstance(root_document_template, dict):
        for key in ("name", "active", "is_static", "tag", "layer"):
            if key in root_document_template:
                go_data[key] = copy.deepcopy(root_document_template[key])
        root_transform = go_data.get("transform")
        template_transform = root_document_template.get("transform")
        if isinstance(root_transform, dict) and isinstance(template_transform, dict):
            for key in ("position", "rotation"):
                if key in template_transform:
                    root_transform[key] = copy.deepcopy(template_transform[key])

    # Strip linkage and convert runtime IDs/references to prefab-local IDs.
    _strip_prefab_fields(go_data)
    _strip_prefab_runtime_fields(go_data)

    prefab_data = {
        "root_object": go_data,
    }
    if source_canvas_name:
        prefab_data["source_canvas_name"] = source_canvas_name
    _validate_prefab_document(prefab_data)
    return prefab_data


def save_prefab_document(prefab_data: dict, file_path: str, asset_database=None) -> bool:
    """Durably publish an already captured strict prefab document."""
    if not file_path.lower().endswith(PREFAB_EXTENSION):
        file_path += PREFAB_EXTENSION

    try:
        _validate_prefab_document(prefab_data, file_path)
    except PrefabDocumentError as exc:
        Debug.log_error(f"Refusing to save invalid prefab: {exc}")
        return False

    try:
        os.makedirs(os.path.dirname(resolved_path(file_path)), exist_ok=True)
        from Infernux.core.document_store import DocumentStore
        content = json.dumps(prefab_data, indent=2, ensure_ascii=False)
        DocumentStore.instance().write_and_wait(file_path, content)
    except (OSError, RuntimeError) as exc:
        Debug.log_error(f"Failed to write prefab file: {exc}")
        return False

    if asset_database:
        try:
            from Infernux.core.assets import AssetManager
            mutation = AssetManager.import_asset(file_path, database=asset_database)
            if not mutation:
                raise RuntimeError(mutation.error)
            guid = mutation.guid
            _invalidate_prefab_template_cache(file_path, guid)
        except Exception as exc:
            _invalidate_prefab_template_cache(file_path, "")
            Debug.log_error(f"Failed to register prefab in AssetDatabase: {exc}")
            return False
    else:
        _invalidate_prefab_template_cache(file_path, "")

    return True


def save_prefab(game_object, file_path: str, asset_database=None,
               source_canvas_name: str = "", root_document_template: dict = None) -> bool:
    """Serialize and durably publish a GameObject hierarchy as a prefab."""
    try:
        prefab_data = serialize_prefab_document(
            game_object,
            source_canvas_name=source_canvas_name,
            root_document_template=root_document_template,
        )
    except Exception as exc:
        Debug.log_error(f"Failed to serialize GameObject for prefab: {exc}")
        return False
    return save_prefab_document(prefab_data, file_path, asset_database)


def instantiate_prefab(file_path: str = None, guid: str = None,
                       scene=None, parent=None, asset_database=None,
                       *, instantiate_in_world_space: bool = False,
                       configure_created=None):
    """Instantiate a prefab into the active scene.

    Supply either *file_path* or *guid* (GUID is resolved via asset_database).
    Returns the root GameObject, or None on failure.
    """
    # Resolve path from GUID if needed
    resolved_guid = guid or ""
    if not file_path and guid and asset_database:
        file_path = asset_database.get_path_from_guid(guid)

    if not file_path or not os.path.isfile(file_path):
        Debug.log_warning(f"Prefab file not found: {file_path}")
        return None

    # If we have a path but no GUID, try to resolve GUID from the asset database
    if not resolved_guid and asset_database:
        try:
            resolved_guid = asset_database.get_guid_from_path(file_path) or ""
        except Exception:
            resolved_guid = ""

    if scene is None:
        from Infernux.lib import SceneManager
        scene = SceneManager.instance().get_active_scene()
    if scene is None:
        Debug.log_warning("No active scene — cannot instantiate prefab.")
        return None

    template = _get_cached_prefab_template(file_path, resolved_guid, asset_database)
    if template is None:
        return None

    # Repeated prefab instantiation now uses native C++ clone from a cached template.
    from Infernux.engine.component_restore import clone_game_object_transactionally
    try:
        new_obj = clone_game_object_transactionally(
            scene,
            template,
            parent,
            asset_database,
            instantiate_in_world_space=instantiate_in_world_space,
            configure_created=configure_created,
        )
    except RuntimeError as exc:
        Debug.log_error(f"Failed to preflight prefab clone: {exc}")
        return None
    if new_obj is None:
        Debug.log_error("Failed to instantiate prefab from cached template.")
        return None

    return new_obj


def _stamp_prefab_guid(obj_data: dict, guid: str, is_root: bool = True):
    """Recursively stamp prefab_guid (and prefab_root on root) into JSON data."""
    obj_data["prefab_guid"] = guid
    if is_root:
        obj_data["prefab_root"] = True
    for child in obj_data.get("children", []):
        _stamp_prefab_guid(child, guid, is_root=False)


def _link_created_prefab_source(game_object, file_path: str, asset_database) -> bool:
    """Turn the saved source hierarchy into an instance of its new Prefab."""
    if game_object is None or asset_database is None:
        return False
    try:
        guid = str(asset_database.get_guid_from_path(file_path) or "")
    except Exception as exc:
        Debug.log_warning(f"Failed to resolve created prefab GUID: {exc}")
        return False
    if not guid:
        Debug.log_warning(f"Created prefab has no AssetDatabase GUID: {file_path}")
        return False

    def _link(obj, is_root: bool) -> None:
        obj.prefab_guid = guid
        obj.prefab_root = is_root
        children = list(obj.get_children()) if hasattr(obj, "get_children") else []
        for child in children:
            _link(child, False)

    try:
        _link(game_object, True)
    except Exception as exc:
        Debug.log_warning(f"Failed to link created prefab source: {exc}")
        return False

    return True


def _strip_prefab_fields(obj_data: dict):
    """Recursively remove prefab_guid/prefab_root so the template is clean."""
    obj_data.pop("prefab_guid", None)
    obj_data.pop("prefab_root", None)
    for child in obj_data.get("children", []):
        _strip_prefab_fields(child)
