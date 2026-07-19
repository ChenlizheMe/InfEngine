"""
Prepared Python component graph transactions for Scene and GameObject documents.
"""

import json
import os
import copy
from dataclasses import dataclass
from typing import Optional, Any
from Infernux.engine.project_context import resolve_script_path, resolve_guid_to_path


class PythonComponentRestoreError(RuntimeError):
    """Raised when a Python component graph cannot be restored exactly."""


def _validate_reference_documents(
    value,
    path: str,
    scene,
    pending_types: set[tuple[int, str]],
    *,
    document_object_ids: Optional[set[int]] = None,
    document_native_types: Optional[set[tuple[int, str]]] = None,
) -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_reference_documents(
                item,
                f"{path}[{index}]",
                scene,
                pending_types,
                document_object_ids=document_object_ids,
                document_native_types=document_native_types,
            )
        return
    if not isinstance(value, dict):
        return

    from Infernux.components.value_document import (
        TYPE_KEY,
        GAME_OBJECT_REF,
        COMPONENT_REF,
        ASSET_REF,
        SERIALIZABLE_OBJECT,
    )

    document_type = value.get(TYPE_KEY)
    if document_type == GAME_OBJECT_REF:
        target_id = value.get("object_id")
        if type(target_id) is not int or target_id < 0:
            raise PythonComponentRestoreError(f"{path}: GameObjectRef id must be a non-negative integer")
        target_exists = (
            target_id in document_object_ids
            if document_object_ids is not None
            else scene.find_by_id(target_id) is not None
        )
        if target_id and not target_exists:
            raise PythonComponentRestoreError(f"{path}: GameObjectRef target {target_id} does not exist")
        return

    if document_type == COMPONENT_REF:
        target_id = value.get("game_object_id")
        type_name = value.get("component_type")
        if type(target_id) is not int or target_id < 0 or not isinstance(type_name, str):
            raise PythonComponentRestoreError(f"{path}: invalid ComponentRef target")
        if target_id == 0:
            return
        target = scene.find_by_id(target_id) if scene is not None else None
        target_exists = (
            target_id in document_object_ids
            if document_object_ids is not None
            else target is not None
        )
        if not target_exists:
            raise PythonComponentRestoreError(f"{path}: ComponentRef GameObject {target_id} does not exist")
        if not type_name:
            raise PythonComponentRestoreError(f"{path}: non-null ComponentRef requires type_name")
        native_exists = (
            (target_id, type_name) in document_native_types
            if document_native_types is not None
            else target.get_cpp_component(type_name) is not None
        )
        if not native_exists and (target_id, type_name) not in pending_types:
            raise PythonComponentRestoreError(
                f"{path}: ComponentRef target {target_id}:{type_name} does not exist"
            )
        return

    if document_type == ASSET_REF:
        return
    if document_type == SERIALIZABLE_OBJECT:
        nested_fields = value.get("fields")
        if not isinstance(nested_fields, dict):
            raise PythonComponentRestoreError(f"{path}: SerializableObject fields must be an object")
        for key, item in nested_fields.items():
            _validate_reference_documents(
                item,
                f"{path}.fields.{key}",
                scene,
                pending_types,
                document_object_ids=document_object_ids,
                document_native_types=document_native_types,
            )
        return
    if document_type is not None:
        return

    for key, item in value.items():
        _validate_reference_documents(
            item,
            f"{path}.{key}",
            scene,
            pending_types,
            document_object_ids=document_object_ids,
            document_native_types=document_native_types,
        )


@dataclass
class PreparedPythonComponent:
    game_object_id: Optional[int]
    source_object_id: int
    document_path: str
    type_name: str
    script_guid: str
    type_guid: str
    enabled: bool
    execution_order: int
    component_id: Optional[int]
    fields_document: dict
    instance: Any

    @property
    def component_index(self) -> int:
        marker = ".components["
        _prefix, separator, suffix = self.document_path.rpartition(marker)
        if not separator or not suffix.endswith("]"):
            raise PythonComponentRestoreError(
                f"invalid Python component document path '{self.document_path}'"
            )
        return int(suffix[:-1])


_COMPONENT_RECORD_FIELDS = {
    "component_id", "type_id", "enabled", "execution_order", "data",
}
_NATIVE_TYPE_PREFIX = "native:infernux."
_PYTHON_TYPE_PREFIX = "python:"


def _decode_python_component_record(record: dict, path: str) -> tuple[str, str, str, str, str]:
    if not isinstance(record, dict) or set(record) != _COMPONENT_RECORD_FIELDS:
        raise PythonComponentRestoreError(f"{path} must be an exact ComponentRecord")
    type_id = record.get("type_id")
    if not isinstance(type_id, str) or not type_id.startswith(_PYTHON_TYPE_PREFIX):
        raise PythonComponentRestoreError(f"{path}.type_id is not a Python component identity")
    parts = type_id[len(_PYTHON_TYPE_PREFIX):].split(":")
    if len(parts) != 4 or any(not part for part in parts):
        raise PythonComponentRestoreError(
            f"{path}.type_id must encode script GUID, type GUID, module, and qualname"
        )
    script_guid, type_guid, module_name, qualified_name = parts
    data = record.get("data")
    if isinstance(data, dict) and {
        "__type_name__", "__component_id__",
    }.intersection(data):
        raise PythonComponentRestoreError(f"{path}.data contains reserved component metadata")
    return script_guid, type_guid, module_name, qualified_name, qualified_name.rsplit(".", 1)[-1]


@dataclass
class PreparedPythonComponentGraph:
    components: list[PreparedPythonComponent]
    _closed: bool = False

    def require_open(self) -> None:
        if self._closed:
            raise PythonComponentRestoreError("prepared Python component graph was already consumed")

    def consume(self) -> None:
        self.require_open()
        self.components.clear()
        self._closed = True

    def discard(self) -> None:
        if self._closed:
            return
        for item in self.components:
            instance = item.instance
            if not getattr(instance, "_is_destroyed", False):
                instance._call_on_destroy()
        self.components.clear()
        self._closed = True


def _collect_scene_document_python_descriptors(document: dict):
    if not isinstance(document, dict) or not isinstance(document.get("objects"), list):
        raise PythonComponentRestoreError("Scene document requires an objects array")

    object_ids: set[int] = set()
    native_types: set[tuple[int, str]] = set()
    descriptors: list[tuple[Optional[int], str, dict]] = []

    def visit(obj, path: str):
        if not isinstance(obj, dict):
            raise PythonComponentRestoreError(f"{path} must be an object")
        object_id = obj.get("id")
        if type(object_id) is not int or object_id <= 0 or object_id in object_ids:
            raise PythonComponentRestoreError(f"{path}.id must be a unique positive integer")
        object_ids.add(object_id)

        transform = obj.get("transform")
        if isinstance(transform, dict) and isinstance(transform.get("type"), str):
            native_types.add((object_id, transform["type"]))
        components = obj.get("components")
        if not isinstance(components, list):
            raise PythonComponentRestoreError(f"{path}.components must be an array")
        for index, component in enumerate(components):
            component_path = f"{path}.components[{index}]"
            if not isinstance(component, dict) or not isinstance(component.get("type_id"), str):
                raise PythonComponentRestoreError(f"{component_path} has no typed component identity")
            type_id = component["type_id"]
            if type_id.startswith(_NATIVE_TYPE_PREFIX):
                native_type = type_id[len(_NATIVE_TYPE_PREFIX):]
                if not native_type:
                    raise PythonComponentRestoreError(f"{component_path}.type_id has no native type")
                native_types.add((object_id, native_type))
            elif type_id.startswith(_PYTHON_TYPE_PREFIX):
                descriptors.append((object_id, component_path, component))
            else:
                raise PythonComponentRestoreError(f"{component_path}.type_id has an unknown namespace")

        children = obj.get("children")
        if not isinstance(children, list):
            raise PythonComponentRestoreError(f"{path}.children must be an array")
        for index, child in enumerate(children):
            visit(child, f"{path}.children[{index}]")

    for index, root in enumerate(document["objects"]):
        visit(root, f"objects[{index}]")
    return object_ids, native_types, descriptors


def _prepare_python_component_records(
    object_ids: set[int],
    native_types: set[tuple[int, str]],
    raw_descriptors: list[tuple[Optional[int], str, dict]],
    asset_database=None,
) -> PreparedPythonComponentGraph:
    pending_types: set[tuple[int, str]] = set()
    component_type_counts: dict[tuple[int, str], int] = {}
    python_component_ids: set[int] = set()
    parsed: list[
        tuple[Optional[int], str, str, str, str, str, str, bool, int, int, dict]
    ] = []

    for object_id, document_path, descriptor in raw_descriptors:
        script_guid, type_guid, module_name, qualified_name, type_name = _decode_python_component_record(
            descriptor, document_path
        )
        enabled = descriptor.get("enabled")
        execution_order = descriptor.get("execution_order")
        data = descriptor.get("data")
        if (
            type(enabled) is not bool
            or type(execution_order) is not int
            or not isinstance(data, dict)
        ):
            raise PythonComponentRestoreError(f"Python component '{type_name}' has invalid typed fields")
        component_id = descriptor.get("component_id")
        if type(component_id) is not int or component_id <= 0:
            raise PythonComponentRestoreError(f"Python component '{type_name}' has invalid component_id")
        if component_id in python_component_ids:
            raise PythonComponentRestoreError(
                f"Python component '{type_name}' uses a duplicate component_id"
            )
        python_component_ids.add(component_id)
        fields = {
            "__type_name__": type_name,
            "__component_id__": component_id,
            **data,
        }
        if object_id is not None:
            pending_types.add((object_id, type_name))
            key = (object_id, type_name)
            component_type_counts[key] = component_type_counts.get(key, 0) + 1
        parsed.append(
            (
                object_id, document_path, type_name, script_guid, type_guid,
                module_name, qualified_name, enabled, execution_order, component_id, fields,
            )
        )

    graph = PreparedPythonComponentGraph([])
    try:
        for (
            object_id, document_path, type_name, script_guid, type_guid,
            module_name, qualified_name, enabled, execution_order, component_id, fields,
        ) in parsed:
            _validate_reference_documents(
                fields,
                f"{document_path}.py_fields",
                None,
                pending_types,
                document_object_ids=object_ids,
                document_native_types=native_types,
            )
            instance = None
            script_path = None
            construct_error = ""
            try:
                instance, script_path = create_component_instance(
                    script_guid,
                    type_guid,
                    type_name,
                    asset_database,
                )
            except Exception as exc:
                construct_error = str(exc)
                instance = None
            if instance is None:
                from Infernux.components.missing_script import create_missing_script_component

                location = script_path or script_guid or "<unresolved>"
                detail = construct_error or f"cannot resolve Python component from {location}"
                instance = create_missing_script_component(
                    type_name=type_name,
                    script_guid=script_guid,
                    type_guid=type_guid,
                    module_name=module_name,
                    qualified_name=qualified_name,
                    fields=fields,
                    error=f"Missing script '{type_name}': {detail}",
                )
                from Infernux.debug import Debug
                Debug.log_error(instance._broken_error)
            instance_type = type(instance)
            is_broken = bool(getattr(instance, "_is_broken", False))
            # Script file renames change the import module path. Class renames
            # change __qualname__/__name__ while the AssetDatabase script GUID
            # stays stable. Accept the live class when it came from that GUID;
            # rewrite authored __type_name__ so field restore can proceed.
            if (
                not is_broken
                and instance_type.__qualname__ != qualified_name
                and instance_type.__name__ != type_name
            ):
                # Only tolerate identity drift for asset-backed script loads.
                # Intrinsic/registry components must still match exactly.
                script_path = resolve_script_from_guid(script_guid, asset_database)
                if not (script_path and os.path.exists(script_path)):
                    instance._call_on_destroy()
                    raise PythonComponentRestoreError(
                        f"Python component '{type_name}' resolved to {instance_type.__module__}."
                        f"{instance_type.__qualname__}, expected {module_name}.{qualified_name}"
                    )
            if not is_broken and fields.get("__type_name__") != instance_type.__name__:
                live_fields = dict(fields)
                live_fields["__type_name__"] = instance_type.__name__
            else:
                live_fields = fields
            component_type = type(instance)
            if (
                not is_broken
                and object_id is not None
                and getattr(component_type, "_disallow_multiple_", False)
                and component_type_counts[(object_id, type_name)] != 1
            ):
                instance._call_on_destroy()
                raise PythonComponentRestoreError(
                    f"Python component '{type_name}' disallows multiple instances on one GameObject"
                )
            if not is_broken:
                for required_type in getattr(component_type, "_require_components_", ()):
                    if isinstance(required_type, str):
                        required_name = required_type
                        required_is_native = (object_id, required_name) in native_types
                    elif hasattr(required_type, "_cpp_type_name"):
                        required_name = str(required_type._cpp_type_name)
                        required_is_native = True
                    elif isinstance(required_type, type):
                        required_name = required_type.__name__
                        required_is_native = False
                    else:
                        instance._call_on_destroy()
                        raise PythonComponentRestoreError(
                            f"Python component '{type_name}' has an invalid required component declaration"
                        )
                    if not required_name:
                        instance._call_on_destroy()
                        raise PythonComponentRestoreError(
                            f"Python component '{type_name}' has an empty required component type"
                        )
                    available_types = native_types if required_is_native else pending_types
                    if object_id is None or (object_id, required_name) not in available_types:
                        instance._call_on_destroy()
                        raise PythonComponentRestoreError(
                            f"Python component '{type_name}' requires missing component '{required_name}'"
                        )
            try:
                instance._deserialize_fields_document(
                    live_fields,
                    _skip_on_after_deserialize=True,
                )
            except Exception as exc:
                instance._call_on_destroy()
                raise PythonComponentRestoreError(
                    f"invalid fields for Python component '{type_name}' at {document_path}: {exc}"
                ) from exc
            try:
                instance.enabled = enabled
            except Exception:
                instance._call_on_destroy()
                raise
            graph.components.append(
                PreparedPythonComponent(
                    object_id,
                    object_id,
                    document_path,
                    type_name,
                    script_guid,
                    type_guid,
                    enabled,
                    execution_order,
                    component_id,
                    fields,
                    instance,
                )
            )
        return graph
    except Exception:
        graph.discard()
        raise


def preflight_scene_python_components(document, asset_database=None) -> PreparedPythonComponentGraph:
    """Resolve and decode the complete Python graph before native scene commit."""
    records = getattr(document, "_python_component_records", None)
    if callable(records):
        object_ids, native_types, raw_descriptors = records()
    else:
        object_ids, native_types, raw_descriptors = _collect_scene_document_python_descriptors(document)
    return _prepare_python_component_records(
        set(object_ids),
        set(native_types),
        list(raw_descriptors),
        asset_database,
    )


def preflight_game_object_python_components(
    document: dict,
    asset_database=None,
    *,
    preserve_document_ids: bool,
) -> PreparedPythonComponentGraph:
    """Preflight one ObjectGraph before deserialize, instantiate, or clone."""
    object_ids: set[int] = set()
    native_types: set[tuple[int, str]] = set()
    descriptors: list[tuple[Optional[int], str, dict]] = []

    def visit(obj, path: str):
        if not isinstance(obj, dict):
            raise PythonComponentRestoreError(f"{path} must be an object")
        raw_id = obj.get("id") if preserve_document_ids else obj.get("local_id", obj.get("id"))
        if type(raw_id) is not int or raw_id <= 0 or raw_id in object_ids:
            id_field = "id" if preserve_document_ids else "local_id/id"
            raise PythonComponentRestoreError(f"{path}.{id_field} must be a unique positive integer")
        object_ids.add(raw_id)
        if preserve_document_ids:
            object_id: Optional[int] = raw_id
        else:
            object_id = raw_id

        transform = obj.get("transform")
        if not isinstance(transform, dict) or not isinstance(transform.get("type"), str):
            raise PythonComponentRestoreError(f"{path}.transform has no typed component record")
        if object_id is not None:
            native_types.add((object_id, transform["type"]))

        components = obj.get("components")
        if not isinstance(components, list):
            raise PythonComponentRestoreError(f"{path}.components must be an array")
        for index, component in enumerate(components):
            component_path = f"{path}.components[{index}]"
            if not isinstance(component, dict) or not isinstance(component.get("type_id"), str):
                raise PythonComponentRestoreError(f"{component_path} has no typed component identity")
            type_id = component["type_id"]
            if type_id.startswith(_NATIVE_TYPE_PREFIX):
                native_type = type_id[len(_NATIVE_TYPE_PREFIX):]
                if not native_type:
                    raise PythonComponentRestoreError(f"{component_path}.type_id has no native type")
                if object_id is not None:
                    native_types.add((object_id, native_type))
            elif type_id.startswith(_PYTHON_TYPE_PREFIX):
                descriptors.append((object_id, component_path, component))
            else:
                raise PythonComponentRestoreError(f"{component_path}.type_id has an unknown namespace")

        children = obj.get("children")
        if not isinstance(children, list):
            raise PythonComponentRestoreError(f"{path}.children must be an array")
        for index, child in enumerate(children):
            visit(child, f"{path}.children[{index}]")

    visit(document, "root_object")
    prepared = _prepare_python_component_records(
        object_ids,
        native_types,
        descriptors,
        asset_database,
    )
    if not preserve_document_ids:
        for component in prepared.components:
            component.game_object_id = None
            component.component_id = None
            component.fields_document.pop("__component_id__", None)
    return prepared


def _native_object_graph_document(document: dict) -> dict:
    """Remove prefab-only local IDs before crossing the strict native boundary."""
    native_document = copy.deepcopy(document)

    def visit(obj: dict) -> None:
        obj.pop("local_id", None)
        for child in obj["children"]:
            visit(child)

    visit(native_document)
    return native_document


def _build_instantiated_object_id_map(source_document: dict, created) -> dict[int, int]:
    created_document = created.serialize_document()
    mapping: dict[int, int] = {}

    def visit(source: dict, target: dict, path: str) -> None:
        source_id = source.get("local_id", source.get("id"))
        target_id = target.get("id")
        if type(source_id) is not int or source_id <= 0 or type(target_id) is not int or target_id <= 0:
            raise PythonComponentRestoreError(f"{path}: cannot build ObjectGraph ID mapping")
        if source_id in mapping:
            raise PythonComponentRestoreError(f"{path}: duplicate ObjectGraph source ID {source_id}")
        mapping[source_id] = target_id
        source_children = source.get("children")
        target_children = target.get("children")
        if not isinstance(source_children, list) or not isinstance(target_children, list):
            raise PythonComponentRestoreError(f"{path}: ObjectGraph children must be arrays")
        if len(source_children) != len(target_children):
            raise PythonComponentRestoreError(f"{path}: native ObjectGraph shape changed during instantiate")
        for index, (source_child, target_child) in enumerate(zip(source_children, target_children)):
            visit(source_child, target_child, f"{path}.children[{index}]")

    visit(source_document, created_document, "root_object")
    return mapping


def _remap_local_reference_document(value, object_id_map: dict[int, int], path: str):
    if isinstance(value, list):
        return [
            _remap_local_reference_document(item, object_id_map, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if not isinstance(value, dict):
        return value
    from Infernux.components.value_document import TYPE_KEY, GAME_OBJECT_REF, COMPONENT_REF
    document_type = value.get(TYPE_KEY)
    if document_type == GAME_OBJECT_REF:
        source_id = value["object_id"]
        if source_id == 0:
            return dict(value)
        remapped = dict(value)
        remapped["object_id"] = object_id_map[source_id]
        return remapped
    if document_type == COMPONENT_REF:
        source_id = value["game_object_id"]
        if source_id == 0:
            return copy.deepcopy(value)
        remapped = dict(value)
        remapped["game_object_id"] = object_id_map[source_id]
        return remapped
    return {
        key: _remap_local_reference_document(item, object_id_map, f"{path}.{key}")
        for key, item in value.items()
    }


def _publish_prepared_scene_python_components(
    scene,
    prepared_graph: PreparedPythonComponentGraph,
    *,
    clear_registries: bool = True,
    object_id_map: Optional[dict[int, int]] = None,
) -> None:
    """Match native pending descriptors and publish a preflighted graph."""
    prepared_graph.require_open()
    pending = scene.get_pending_py_components()
    prepared = prepared_graph.components
    if len(pending) != len(prepared):
        raise PythonComponentRestoreError("native pending Python component count changed after preflight")

    targets = []
    for pc, item in zip(pending, prepared):
        fields = pc.fields_document
        if not isinstance(fields, dict):
            raise PythonComponentRestoreError("native pending fields document must be an object")
        comparable_fields = dict(fields)
        if item.component_id is None:
            comparable_fields.pop("__component_id__", None)
        if (
            (item.game_object_id is not None and pc.game_object_id != item.game_object_id)
            or pc.type_name != item.type_name
            or (getattr(pc, "script_guid", "") or "") != item.script_guid
            or (getattr(pc, "type_guid", "") or "") != item.type_guid
            or bool(pc.enabled) != item.enabled
            or int(pc.execution_order) != item.execution_order
            or comparable_fields != item.fields_document
        ):
            raise PythonComponentRestoreError("native pending Python descriptor changed after preflight")
        target = scene.find_by_id(pc.game_object_id)
        if target is None:
            raise PythonComponentRestoreError(
                f"preflighted Python component target {pc.game_object_id} is missing after commit"
            )
        targets.append((target, item.instance))

    consumed = scene.take_pending_py_components()
    if len(consumed) != len(prepared):
        raise PythonComponentRestoreError("pending Python component queue changed during publish")

    if object_id_map is not None:
        try:
            for item in prepared:
                remapped_fields = _remap_local_reference_document(
                    item.fields_document,
                    object_id_map,
                    f"{item.document_path}.py_fields",
                )
                item.instance._deserialize_fields_document(
                    remapped_fields,
                    _skip_on_after_deserialize=True,
                )
        except (KeyError, TypeError, ValueError) as exc:
            raise PythonComponentRestoreError(
                f"failed to remap ObjectGraph-local Python references: {exc}"
            ) from exc

    if clear_registries:
        from Infernux.components.component import InxComponent
        InxComponent._clear_all_instances()
        from Infernux.components.builtin_component import BuiltinComponent
        BuiltinComponent._clear_cache()
        from Infernux.gizmos.collector import notify_scene_changed
        notify_scene_changed()

    attached = []
    try:
        for prepared_index, ((target, instance), item) in enumerate(zip(targets, prepared)):
            published_instance = target._attach_prepared_py_component(
                instance,
                pending[prepared_index].component_index,
            )
            if published_instance is not instance:
                raise PythonComponentRestoreError(
                    f"Python component '{item.type_name}' was rejected by its target GameObject"
                )
            native_component = getattr(instance, "_cpp_component", None)
            if native_component is None:
                raise PythonComponentRestoreError(
                    f"Python component '{item.type_name}' was not bound to a native proxy"
                )
            attached.append((target, instance, native_component))
            if item.component_id is not None:
                native_component._set_component_id(item.component_id)
                instance._component_id = item.component_id
                instance._refresh_native_handle()
            native_component.execution_order = item.execution_order
        for _target, instance, _native_component in attached:
            instance._call_on_after_deserialize()
        for target, _instance, native_component in attached:
            target._activate_prepared_py_component(native_component)
    except Exception as exc:
        for target, _instance, native_component in reversed(attached):
            target._remove_prepared_py_component(native_component)
        prepared_graph.discard()
        raise PythonComponentRestoreError(f"failed to publish Python component graph: {exc}") from exc
    prepared_graph.consume()


def publish_prepared_scene_python_components(
    scene,
    prepared_graph: PreparedPythonComponentGraph,
    *,
    clear_registries: bool = True,
    object_id_map: Optional[dict[int, int]] = None,
) -> None:
    """Consume a prepared graph, releasing every unattached instance on failure."""
    try:
        _publish_prepared_scene_python_components(
            scene,
            prepared_graph,
            clear_registries=clear_registries,
            object_id_map=object_id_map,
        )
    except Exception:
        prepared_graph.discard()
        raise


def replace_scene_python_components_for_play(
    scene,
    document,
    asset_database=None,
) -> bool:
    """Create fresh Python instances without rebuilding the native scene graph.

    Entering Play Mode only needs a fresh scripting domain. Native objects and
    components still match the snapshot captured immediately beforehand, so
    recreating thousands of renderers and colliders is unnecessary. Stop Mode
    continues to restore the complete document transactionally.
    """
    prepared_graph = preflight_scene_python_components(document, asset_database)
    prepared = list(prepared_graph.components)
    existing_by_object: dict[int, list[Any]] = {}
    targets: dict[int, Any] = {}

    try:
        for item in prepared:
            if item.game_object_id is None or item.component_id is None:
                raise PythonComponentRestoreError(
                    f"{item.document_path} has no persistent object/component identity"
                )
            target = targets.get(item.game_object_id)
            if target is None:
                target = scene.find_by_id(item.game_object_id)
                if target is None:
                    raise PythonComponentRestoreError(
                        f"Python component target {item.game_object_id} is missing"
                    )
                targets[item.game_object_id] = target
                existing_by_object[item.game_object_id] = list(target.get_py_components() or [])

        expected_ids_by_object: dict[int, set[int]] = {}
        for item in prepared:
            expected_ids_by_object.setdefault(item.game_object_id, set()).add(item.component_id)
        for object_id, existing in existing_by_object.items():
            existing_ids = {
                int(getattr(component, "_component_id", 0) or 0)
                for component in existing
            }
            if existing_ids != expected_ids_by_object[object_id]:
                raise PythonComponentRestoreError(
                    f"Python component identity changed on GameObject {object_id}"
                )

        for existing in existing_by_object.values():
            for component in existing:
                native_component = getattr(component, "_cpp_component", None)
                owner = getattr(component, "game_object", None)
                if native_component is None or owner is None:
                    raise PythonComponentRestoreError("live Python component lost its native binding")
                if not owner._remove_prepared_py_component(native_component):
                    raise PythonComponentRestoreError("failed to detach an edit-mode Python component")

        from Infernux.components.component import InxComponent
        from Infernux.components.builtin_component import BuiltinComponent
        from Infernux.gizmos.collector import notify_scene_changed

        InxComponent._clear_all_instances()
        BuiltinComponent._clear_cache()
        notify_scene_changed()

        attached = []
        for item in sorted(prepared, key=lambda value: (value.game_object_id, value.component_index)):
            target = targets[item.game_object_id]
            instance = target._attach_prepared_py_component(item.instance, item.component_index)
            if instance is not item.instance:
                raise PythonComponentRestoreError(
                    f"Python component '{item.type_name}' was rejected by its target GameObject"
                )
            native_component = getattr(instance, "_cpp_component", None)
            if native_component is None:
                raise PythonComponentRestoreError(
                    f"Python component '{item.type_name}' was not bound to a native proxy"
                )
            native_component._set_component_id(item.component_id)
            instance._component_id = item.component_id
            instance._refresh_native_handle()
            native_component.execution_order = item.execution_order
            attached.append((target, instance, native_component))

        for _target, instance, _native_component in attached:
            instance._call_on_after_deserialize()
        for target, _instance, native_component in attached:
            target._activate_prepared_py_component(native_component)
        prepared_graph.consume()
        return True
    except Exception:
        prepared_graph.discard()
        raise


def deserialize_scene_document_transactionally(
    scene,
    document: dict,
    asset_database=None,
    *,
    clear_registries: bool = True,
    after_publish=None,
) -> bool:
    """Run a complete in-memory Scene document transaction."""
    from Infernux.engine.scene_document_transaction import SceneDocumentTransaction

    transaction = SceneDocumentTransaction(
        scene,
        document=document,
        asset_database=asset_database,
        clear_registries=clear_registries,
        after_publish=after_publish,
    )
    transaction.run_to_completion(raise_on_failure=False)
    if transaction.failure_exception is not None:
        raise transaction.failure_exception
    return transaction.succeeded


def _require_clean_pending_queue(scene) -> None:
    if scene.has_pending_py_components():
        raise PythonComponentRestoreError(
            "ObjectGraph transaction requires an empty Scene pending Python queue"
        )


def deserialize_game_object_document_transactionally(
    game_object,
    document: dict,
    asset_database=None,
    *,
    preserve_document_ids: bool = True,
) -> bool:
    """Preflight Python data before replacing one live GameObject subtree."""
    scene = game_object.scene
    if scene is None:
        raise PythonComponentRestoreError("cannot deserialize a detached GameObject")
    _require_clean_pending_queue(scene)
    prepared = preflight_game_object_python_components(
        document,
        asset_database,
        preserve_document_ids=preserve_document_ids,
    )
    return commit_prepared_game_object_document(
        game_object,
        document,
        prepared,
        preserve_document_ids=preserve_document_ids,
    )


def serialize_game_object_document_authoritatively(game_object) -> dict:
    """Snapshot an ObjectGraph with current live wrapper field values.

    Native GameObject aggregation can lag behind Inspector-bound wrapper
    state until the next synchronization point. Structural Undo snapshots
    must capture the values the Inspector currently exposes.
    """
    document = game_object.serialize_document()
    native_metadata = {
        "type",
        "component_id",
        "enabled",
        "execution_order",
    }
    python_metadata = {
        "__type_name__",
        "__component_id__",
    }

    def visit(obj, node: dict) -> None:
        components = {}
        for component in obj.get_components():
            component_id = getattr(component, "component_id", 0) or 0
            if component_id:
                components[int(component_id)] = component

        for record in node.get("components", []):
            component_id = record.get("component_id")
            component = components.get(component_id)
            if component is None:
                continue
            try:
                type_id = record.get("type_id", "")
                if type_id.startswith("native:"):
                    serialized = component.serialize_document()
                    record["data"] = {
                        key: value for key, value in serialized.items()
                        if key not in native_metadata
                    }
                elif type_id.startswith("python:"):
                    serialized = component._serialize_fields_document()
                    record["data"] = {
                        key: value for key, value in serialized.items()
                        if key not in python_metadata
                    }
                record["enabled"] = bool(getattr(component, "enabled", True))
                record["execution_order"] = int(getattr(component, "execution_order", 0))
            except Exception as exc:
                from Infernux.debug import Debug
                Debug.log_suppressed(
                    f"component_restore.authoritative_snapshot[{component_id}]",
                    exc,
                )

        children = obj.get_children()
        child_documents = node.get("children", [])
        if len(children) != len(child_documents):
            raise PythonComponentRestoreError(
                "ObjectGraph shape changed during authoritative snapshot"
            )
        for child, child_document in zip(children, child_documents):
            visit(child, child_document)

    visit(game_object, document)
    return document


def commit_prepared_game_object_document(
    game_object,
    document: dict,
    prepared: PreparedPythonComponentGraph,
    *,
    preserve_document_ids: bool = True,
) -> bool:
    """Commit one already preflighted in-place ObjectGraph replacement."""
    scene = game_object.scene
    if scene is None:
        raise PythonComponentRestoreError("cannot deserialize a detached GameObject")
    _require_clean_pending_queue(scene)
    replaced_native_component_ids: set[int] = set()

    def collect_native_component_ids(object_document: dict) -> None:
        transform = object_document.get("transform")
        if isinstance(transform, dict) and type(transform.get("component_id")) is int:
            replaced_native_component_ids.add(transform["component_id"])
        for component in object_document.get("components", []):
            if isinstance(component, dict) and type(component.get("component_id")) is int:
                replaced_native_component_ids.add(component["component_id"])
        for child in object_document.get("children", []):
            if isinstance(child, dict):
                collect_native_component_ids(child)

    collect_native_component_ids(game_object.serialize_document())
    if not game_object._commit_document(
        _native_object_graph_document(document),
        preserve_document_ids,
    ):
        prepared.discard()
        return False
    from Infernux.components.builtin_component import BuiltinComponent
    BuiltinComponent._invalidate_component_ids(replaced_native_component_ids)
    object_id_map = None
    if any(item.game_object_id is None for item in prepared.components):
        object_id_map = _build_instantiated_object_id_map(document, game_object)
    publish_prepared_scene_python_components(
        scene,
        prepared,
        clear_registries=False,
        object_id_map=object_id_map,
    )
    return True


def instantiate_game_object_document_transactionally(
    scene,
    document: dict,
    parent=None,
    asset_database=None,
):
    """Preflight and instantiate one ID-less ObjectGraph document."""
    _require_clean_pending_queue(scene)
    prepared = preflight_game_object_python_components(
        document,
        asset_database,
        preserve_document_ids=False,
    )
    return instantiate_prepared_game_object_document(scene, document, prepared, parent)


def instantiate_prepared_game_object_document(
    scene,
    document: dict,
    prepared: PreparedPythonComponentGraph,
    parent=None,
):
    """Instantiate one already preflighted ID-less ObjectGraph."""
    _require_clean_pending_queue(scene)
    created = scene._instantiate_document(_native_object_graph_document(document), parent)
    if created is None:
        prepared.discard()
        return None
    try:
        object_id_map = _build_instantiated_object_id_map(document, created)
        publish_prepared_scene_python_components(
            scene,
            prepared,
            clear_registries=False,
            object_id_map=object_id_map,
        )
    except Exception:
        prepared.discard()
        scene.destroy_game_object(created)
        scene.process_pending_destroys()
        raise
    return created


def clone_game_object_transactionally(
    scene,
    source,
    parent=None,
    asset_database=None,
):
    """Preflight a source snapshot before native subtree clone/publish."""
    _require_clean_pending_queue(scene)
    source_document = source.serialize_document()
    prepared = preflight_game_object_python_components(
        source_document,
        asset_database,
        preserve_document_ids=False,
    )
    created = scene._clone_game_object(source, parent)
    if created is None:
        prepared.discard()
        return None
    try:
        object_id_map = _build_instantiated_object_id_map(source_document, created)
        publish_prepared_scene_python_components(
            scene,
            prepared,
            clear_registries=False,
            object_id_map=object_id_map,
        )
    except Exception:
        prepared.discard()
        scene.destroy_game_object(created)
        scene.process_pending_destroys()
        raise
    return created


def resolve_script_from_guid(
    script_guid: str,
    asset_database=None,
) -> Optional[str]:
    """Resolve a script GUID to an absolute filesystem path.

    Handles:
    - Normal editor look-up via AssetDatabase
    - Packaged-build ``.py → .pyc`` fallback
    - Build-time GUID manifest fallback
    """
    script_path = None

    if script_guid and asset_database:
        raw = asset_database.get_path_from_guid(script_guid)
        if raw:
            script_path = resolve_script_path(raw)

    # Packaged-build fallback: use build-time GUID manifest
    if not script_path and script_guid:
        script_path = resolve_guid_to_path(script_guid)

    return script_path


def create_component_instance(
    script_guid: str,
    type_guid: str,
    type_name: str,
    asset_database=None,
):
    """Create a Python component instance from an exact stable identity.

    Returns ``(instance, script_path)`` — *instance* may be ``None`` if
    the script cannot be loaded.
    """
    if not script_guid or not type_guid or not type_name:
        raise ValueError("Python component identity requires script_guid, type_guid, and type_name")

    script_path = resolve_script_from_guid(script_guid, asset_database)

    instance = None
    loaded_from_asset = False
    if script_path and os.path.exists(script_path):
        loaded_from_asset = True
        if asset_database is not None:
            from Infernux.components.script_loader import load_and_create_component
            instance = load_and_create_component(
                script_path,
                asset_database=asset_database,
                type_name=type_name,
                script_guid=script_guid,
            )
        else:
            from Infernux.components.script_loader import (
                create_component_instance as construct_component,
                load_component_class_from_file,
            )
            from Infernux.components.component_identity import bind_asset_script_guid
            component_type = load_component_class_from_file(script_path, type_name=type_name)
            if component_type is not None:
                bind_asset_script_guid(component_type, script_guid)
                instance = construct_component(component_type)
        if instance is not None:
            instance._script_guid = script_guid
    else:
        from Infernux.components.registry import get_type_by_identity
        comp_class = get_type_by_identity(type_name, script_guid, type_guid)
        if comp_class:
            instance = comp_class()
            instance._script_guid = script_guid

    # Asset-backed loads rebind type_guid to the script GUID. Documents written
    # before a rename may still carry the old module-based type_guid — accept
    # those as long as the class was resolved from the preserved script GUID.
    if (
        instance is not None
        and not loaded_from_asset
        and instance.__class__._get_type_guid() != type_guid
    ):
        instance = None

    return instance, script_path
