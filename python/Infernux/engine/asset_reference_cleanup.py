"""Clear live Python serialized references after an asset is deleted."""

from __future__ import annotations

import os
from typing import Any

from Infernux.engine.path_utils import resolved_path, same_path


def clear_deleted_asset_references(asset_guid: str, asset_path: str) -> dict[str, Any]:
    """Clear matching AssetRefBase values from active Python components."""
    guid = str(asset_guid or "").strip()
    path = resolved_path(str(asset_path or "")) if asset_path else ""
    if not guid and not path:
        return {"references_cleared": 0, "components_changed": 0, "fields": []}

    components = _active_python_components()
    changed_components = 0
    references_cleared = 0
    changed_fields: list[str] = []
    for component in components:
        seen: set[int] = set()
        changes, fields = _clear_owner_references(component, guid, path, seen, prefix="")
        if not changes:
            continue
        references_cleared += changes
        changed_components += 1
        component_id = int(getattr(component, "component_id", 0) or 0)
        type_name = type(component).__name__
        changed_fields.extend(f"{type_name}:{component_id}.{field}" for field in fields)
        validator = getattr(component, "on_validate", None)
        if callable(validator):
            try:
                validator()
            except Exception:
                # Reference cleanup and scene dirtiness must survive an optional
                # component validation hook that rejects a now-missing asset.
                pass

    if references_cleared:
        _mark_active_scene_dirty()
    return {
        "references_cleared": references_cleared,
        "components_changed": changed_components,
        "fields": changed_fields,
    }


def _active_python_components() -> list[Any]:
    from Infernux.components.component import InxComponent

    result = []
    seen: set[int] = set()
    for components in list(InxComponent._active_instances.values()):
        for component in list(components):
            identity = id(component)
            if identity in seen or component is None or getattr(component, "_is_destroyed", False):
                continue
            seen.add(identity)
            result.append(component)
    return result


def _clear_owner_references(
    owner: Any,
    asset_guid: str,
    asset_path: str,
    seen: set[int],
    *,
    prefix: str,
) -> tuple[int, list[str]]:
    if owner is None or id(owner) in seen:
        return 0, []
    seen.add(id(owner))

    from Infernux.components.serialized_field import FieldType, get_raw_field_value, get_serialized_fields

    changes = 0
    changed_fields: list[str] = []
    for field_name, metadata in get_serialized_fields(type(owner)).items():
        try:
            raw = get_raw_field_value(owner, field_name)
        except (AttributeError, RuntimeError, TypeError, ValueError):
            continue
        field_path = f"{prefix}.{field_name}" if prefix else field_name
        if _matches_deleted_asset(raw, asset_guid, asset_path):
            _set_without_field_transaction(owner, field_name, None)
            changes += 1
            changed_fields.append(field_path)
            continue
        if metadata.field_type is FieldType.SERIALIZABLE_OBJECT:
            nested_changes, nested_fields = _clear_owner_references(
                raw,
                asset_guid,
                asset_path,
                seen,
                prefix=field_path,
            )
            changes += nested_changes
            changed_fields.extend(nested_fields)
            continue
        if metadata.field_type is not FieldType.LIST or not isinstance(raw, list):
            continue

        replacement = list(raw)
        list_changed = False
        for index, item in enumerate(raw):
            item_path = f"{field_path}[{index}]"
            if _matches_deleted_asset(item, asset_guid, asset_path):
                replacement[index] = None
                list_changed = True
                changes += 1
                changed_fields.append(item_path)
                continue
            if metadata.element_type is FieldType.SERIALIZABLE_OBJECT or metadata.element_class is not None:
                nested_changes, nested_fields = _clear_owner_references(
                    item,
                    asset_guid,
                    asset_path,
                    seen,
                    prefix=item_path,
                )
                changes += nested_changes
                changed_fields.extend(nested_fields)
        if list_changed:
            _set_without_field_transaction(owner, field_name, replacement)
    return changes, changed_fields


def _matches_deleted_asset(value: Any, asset_guid: str, asset_path: str) -> bool:
    from Infernux.core.asset_ref import AssetRefBase

    if not isinstance(value, AssetRefBase):
        return False
    reference_guid = str(value.guid or "").strip()
    if asset_guid and reference_guid:
        return reference_guid == asset_guid
    hint = str(value.path_hint or "").strip()
    if not hint or not asset_path:
        return False
    return same_path(hint, asset_path)


def _set_without_field_transaction(owner: Any, field_name: str, value: Any) -> None:
    sentinel = object()
    previous = getattr(owner, "_inf_deserializing", sentinel)
    try:
        setattr(owner, "_inf_deserializing", True)
        setattr(owner, field_name, value)
    finally:
        if previous is sentinel:
            try:
                delattr(owner, "_inf_deserializing")
            except AttributeError:
                pass
        else:
            setattr(owner, "_inf_deserializing", previous)


def _mark_active_scene_dirty() -> None:
    from Infernux.engine.scene_manager import SceneFileManager

    manager = SceneFileManager.instance()
    if manager is not None:
        manager.mark_dirty()


__all__ = ["clear_deleted_asset_references"]
