"""
Prefab override diff system.

Compares a live prefab instance hierarchy against its source .prefab asset
to compute property-level overrides. Supports apply (write overrides back
to the .prefab file) and revert (reset instance to match the prefab).

Identification strategy:
  Nodes are matched by *name-path* (e.g. "Root/Child/GrandChild") since
  instance GameObjects get fresh IDs on instantiation. Name-path is stable
  as long as the user does not rename nodes — an acceptable trade-off for
  this iteration of the override system.
"""

import copy
from dataclasses import dataclass
import json
import os
from typing import Dict, List, Optional

from Infernux.debug import Debug


# ─── Public data types ────────────────────────────────────────────────────

class Override:
    """A single property-level override on one node."""
    __slots__ = ("node_path", "key", "prefab_value", "instance_value")

    def __init__(self, node_path: str, key: str, prefab_value, instance_value):
        self.node_path = node_path
        self.key = key
        self.prefab_value = prefab_value
        self.instance_value = instance_value

    def __repr__(self):
        return f"Override({self.node_path!r}, {self.key!r})"


@dataclass(frozen=True, slots=True)
class _PrefabApplyState:
    prefab_document: dict
    instance_documents: tuple[tuple[int, dict], ...]


# ─── Core diff ────────────────────────────────────────────────────────────

_SKIP_KEYS = frozenset({
    "id", "local_id", "children", "components",
    "transform", "prefab_guid", "prefab_root",
})

_TRANSFORM_KEYS = ("position", "rotation", "scale")
_ROOT_INSTANCE_KEYS = frozenset({"name", "active", "is_static", "tag", "layer"})


def resolve_prefab_instance_root(instance_obj):
    """Return the linked root for the prefab instance containing *instance_obj*.

    Every node in an instantiated prefab carries the same ``prefab_guid``.
    ``prefab_root`` is authoritative.
    """
    if instance_obj is None:
        return None
    guid = getattr(instance_obj, "prefab_guid", "") or ""
    if not guid:
        return None

    current = instance_obj
    while current is not None:
        if (getattr(current, "prefab_guid", "") or "") != guid:
            break
        if bool(getattr(current, "prefab_root", False)):
            return current
        current = current.get_parent()
    raise LookupError(f"prefab instance has no marked root for GUID: {guid}")


def compute_overrides(instance_obj, prefab_path: str,
                      asset_database=None) -> List[Override]:
    """Compare *instance_obj* (live GameObject) against the .prefab file.

    Returns a list of Override objects describing every property difference.
    """
    instance_obj = resolve_prefab_instance_root(instance_obj) or instance_obj
    prefab_data = _load_prefab_root(prefab_path)
    instance_data = _serialize_obj(instance_obj)

    overrides: List[Override] = []
    _diff_node(instance_data, prefab_data, "", overrides, is_root=True)
    return overrides


def apply_overrides_to_prefab(instance_obj, prefab_path: str,
                               asset_database=None) -> bool:
    """Write the current instance state back to the .prefab file.

    Resets the instance to non-overridden state (the prefab file now
    matches the instance).
    """
    instance_obj = resolve_prefab_instance_root(instance_obj) or instance_obj
    try:
        from Infernux.engine.prefab_manager import _read_prefab_document, save_prefab
        prefab_file = _read_prefab_document(prefab_path)
    except (OSError, ValueError) as exc:
        Debug.log_error(f"Failed to read prefab for apply: {exc}")
        return False

    prefab_guid = getattr(instance_obj, "prefab_guid", "") or ""
    instance_snapshots = _snapshot_linked_instances(instance_obj, prefab_guid)

    if not save_prefab(
        instance_obj,
        prefab_path,
        asset_database=asset_database,
        source_canvas_name=prefab_file.get("source_canvas_name", ""),
        root_document_template=prefab_file["root_object"],
    ):
        return False

    try:
        updated_prefab_root = _read_prefab_document(prefab_path)["root_object"]
    except (OSError, ValueError) as exc:
        Debug.log_error(f"Failed to read applied prefab: {exc}")
        return False

    if not _propagate_applied_prefab(
        prefab_file["root_object"],
        updated_prefab_root,
        instance_snapshots,
        prefab_guid,
        asset_database,
    ):
        return False

    return True


def build_prefab_apply_command(instance_obj, prefab_path: str,
                               asset_database=None):
    """Build the single reversible command for one Prefab Apply operation."""
    instance_root = resolve_prefab_instance_root(instance_obj) or instance_obj
    if instance_root is None or not prefab_path:
        raise ValueError("Prefab Apply requires a linked instance and asset path")
    from Infernux.engine.undo import PrefabApplyOverridesCommand

    prefab_guid = getattr(instance_root, "prefab_guid", "") or ""

    def capture_state():
        return _capture_prefab_apply_state(instance_root, prefab_path, prefab_guid)

    return PrefabApplyOverridesCommand(
        capture_state,
        lambda: apply_overrides_to_prefab(
            instance_root,
            prefab_path,
            asset_database,
        ),
        lambda state: _restore_prefab_apply_state(
            state,
            instance_root,
            prefab_path,
            asset_database,
        ),
    )


def _capture_prefab_apply_state(instance_root, prefab_path: str,
                                prefab_guid: str) -> _PrefabApplyState:
    from Infernux.engine.prefab_manager import _read_prefab_document

    prefab_document = copy.deepcopy(_read_prefab_document(prefab_path))
    scene = getattr(instance_root, "scene", None)
    documents: list[tuple[int, dict]] = []
    if scene is not None and prefab_guid:
        for obj in scene.get_all_objects():
            if (getattr(obj, "prefab_guid", "") or "") != prefab_guid:
                continue
            if not bool(getattr(obj, "prefab_root", False)):
                continue
            document = _serialize_obj(obj)
            if document is None:
                raise RuntimeError(
                    f"Failed to capture prefab instance '{getattr(obj, 'name', '')}'"
                )
            documents.append((int(obj.id), copy.deepcopy(document)))
    return _PrefabApplyState(prefab_document, tuple(documents))


def _write_prefab_apply_document(prefab_path: str, document: dict,
                                 asset_database=None) -> None:
    from Infernux.engine.prefab_manager import (
        _invalidate_prefab_template_cache,
        _validate_prefab_document,
    )

    payload = copy.deepcopy(document)
    _validate_prefab_document(payload, prefab_path)
    from Infernux.core.document_store import write_document_text

    write_document_text(
        prefab_path,
        json.dumps(payload, indent=2, ensure_ascii=False),
    )
    guid = ""
    if asset_database is not None:
        from Infernux.core.assets import AssetManager

        mutation = AssetManager.import_asset(prefab_path, database=asset_database)
        if not mutation:
            raise RuntimeError(
                getattr(mutation, "error", "Prefab asset reimport failed")
            )
        guid = str(getattr(mutation, "guid", "") or "")
    _invalidate_prefab_template_cache(prefab_path, guid)


def _restore_prefab_instance_documents(instance_root, documents,
                                       asset_database=None) -> None:
    if not documents:
        return
    scene = getattr(instance_root, "scene", None)
    if scene is None:
        raise RuntimeError("Prefab instance scene is unavailable")
    from Infernux.engine.component_restore import (
        commit_prepared_game_object_document,
        preflight_game_object_python_components,
    )

    prepared = []
    try:
        for object_id, document in documents:
            obj = scene.find_by_id(int(object_id))
            if obj is None:
                raise RuntimeError(f"Prefab instance root {object_id} is unavailable")
            plan = preflight_game_object_python_components(
                copy.deepcopy(document),
                asset_database,
                preserve_document_ids=True,
                reference_scene=scene,
            )
            prepared.append((obj, document, plan))
        for obj, document, plan in prepared:
            if not commit_prepared_game_object_document(
                obj,
                copy.deepcopy(document),
                plan,
                preserve_document_ids=True,
            ):
                raise RuntimeError("Prefab ObjectGraph restore failed")
    except Exception:
        for _obj, _document, plan in prepared:
            try:
                plan.discard()
            except Exception:
                pass
        raise


def _restore_prefab_apply_state(state: _PrefabApplyState, instance_root,
                                prefab_path: str, asset_database=None) -> None:
    if not isinstance(state, _PrefabApplyState):
        raise TypeError("Prefab Apply restore state is invalid")
    current = _capture_prefab_apply_state(
        instance_root,
        prefab_path,
        getattr(instance_root, "prefab_guid", "") or "",
    )
    try:
        _write_prefab_apply_document(
            prefab_path,
            state.prefab_document,
            asset_database,
        )
        _restore_prefab_instance_documents(
            instance_root,
            state.instance_documents,
            asset_database,
        )
    except Exception:
        try:
            _write_prefab_apply_document(
                prefab_path,
                current.prefab_document,
                asset_database,
            )
            _restore_prefab_instance_documents(
                instance_root,
                current.instance_documents,
                asset_database,
            )
        except Exception as rollback_exc:
            Debug.log_error(f"Prefab Apply rollback failed: {rollback_exc}")
        raise


def revert_overrides(instance_obj, prefab_path: str,
                     asset_database=None) -> bool:
    """Reset the instance hierarchy to match the source .prefab file.

    Preserves the instance's transform (position in scene) and its
    prefab linkage fields.
    """
    instance_obj = resolve_prefab_instance_root(instance_obj) or instance_obj
    prefab_data = _build_reverted_prefab_document(instance_obj, prefab_path)
    if prefab_data is None:
        Debug.log_error("Failed to load prefab for revert.")
        return False

    try:
        from Infernux.engine.component_restore import deserialize_game_object_document_transactionally
        if not deserialize_game_object_document_transactionally(
            instance_obj,
            prefab_data,
            asset_database,
            preserve_document_ids=False,
        ):
            Debug.log_error("Failed to apply prefab document during revert.")
            return False
    except Exception as exc:
        Debug.log_error(f"Failed to deserialize during revert: {exc}")
        return False

    return True


def build_prefab_revert_command(instance_obj, prefab_path: str,
                                asset_database=None):
    """Build the single reversible command for one Prefab Revert operation."""
    instance_obj = resolve_prefab_instance_root(instance_obj) or instance_obj
    if instance_obj is None or not prefab_path:
        raise ValueError("Prefab Revert requires a linked instance and asset path")
    reverted_document = _build_reverted_prefab_document(instance_obj, prefab_path)
    if reverted_document is None:
        raise RuntimeError("Failed to load prefab for revert")
    from Infernux.engine.component_restore import (
        serialize_game_object_document_authoritatively,
    )
    from Infernux.engine.undo import PrefabRevertCommand

    before_document = serialize_game_object_document_authoritatively(instance_obj)
    return PrefabRevertCommand(
        instance_obj.id,
        before_document,
        reverted_document,
        asset_database,
    )


def _build_reverted_prefab_document(instance_obj, prefab_path: str):
    instance_obj = resolve_prefab_instance_root(instance_obj) or instance_obj
    prefab_data = _load_prefab_root(prefab_path)
    if prefab_data is None:
        return None

    # Root position and rotation place the instance in its scene. They are
    # not prefab overrides, so preserve them while reverting prefab-owned
    # scale and every child transform.
    try:
        current_document = _serialize_obj(instance_obj)
        current_transform = current_document.get("transform")
    except Exception:
        current_document = None
        current_transform = None

    # Keep prefab linkage
    prefab_guid = getattr(instance_obj, 'prefab_guid', '')

    # Stamp prefab linkage into the template
    from Infernux.engine.prefab_manager import _stamp_prefab_guid
    if prefab_guid:
        _stamp_prefab_guid(prefab_data, prefab_guid, is_root=True)

    # These fields describe this scene instance, not the prefab asset. Revert
    # must not rename the placed object or change its scene organization.
    if current_document:
        for key in _ROOT_INSTANCE_KEYS:
            if key in current_document:
                prefab_data[key] = copy.deepcopy(current_document[key])

    # Restore transform
    if current_transform:
        prefab_transform = prefab_data.get("transform")
        if isinstance(prefab_transform, dict):
            for key in ("position", "rotation"):
                if key in current_transform:
                    prefab_transform[key] = copy.deepcopy(current_transform[key])

    return prefab_data


def _snapshot_linked_instances(instance_root, prefab_guid: str):
    """Capture every live root linked to the prefab before Apply writes it."""
    scene = getattr(instance_root, "scene", None)
    if scene is None or not prefab_guid:
        return []

    from Infernux.engine.prefab_manager import (
        _strip_prefab_fields,
        _strip_prefab_runtime_fields,
    )

    snapshots = []
    for obj in scene.get_all_objects():
        if (getattr(obj, "prefab_guid", "") or "") != prefab_guid:
            continue
        if not bool(getattr(obj, "prefab_root", False)):
            continue
        runtime_document = _serialize_obj(obj)
        if runtime_document is None:
            raise RuntimeError(
                f"Failed to snapshot linked prefab instance '{getattr(obj, 'name', '')}'"
            )
        local_document = copy.deepcopy(runtime_document)
        _strip_prefab_fields(local_document)
        _strip_prefab_runtime_fields(local_document)
        snapshots.append((obj, runtime_document, local_document))
    return snapshots


_MERGE_IDENTITY_KEYS = frozenset({
    "id", "local_id", "component_id", "instance_guid",
    "prefab_guid", "prefab_root",
})
_MISSING = object()


def _prefab_content_equal(left, right) -> bool:
    """Compare prefab content while ignoring runtime/local identity metadata."""
    if isinstance(left, dict) and isinstance(right, dict):
        left_keys = set(left) - _MERGE_IDENTITY_KEYS
        right_keys = set(right) - _MERGE_IDENTITY_KEYS
        return left_keys == right_keys and all(
            _prefab_content_equal(left[key], right[key]) for key in left_keys
        )
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(
            _prefab_content_equal(a, b) for a, b in zip(left, right)
        )
    return left == right


def _three_way_merge_prefab(base, local, remote):
    """Merge one instance's old overrides onto an updated prefab document."""
    if _prefab_content_equal(local, base):
        return copy.deepcopy(remote)
    if _prefab_content_equal(remote, base) or _prefab_content_equal(local, remote):
        return copy.deepcopy(local)

    if isinstance(base, dict) and isinstance(local, dict) and isinstance(remote, dict):
        merged = {}
        for key in set(base) | set(local) | set(remote):
            if key in _MERGE_IDENTITY_KEYS:
                value = remote.get(key, local.get(key, base.get(key, _MISSING)))
            else:
                base_value = base.get(key, _MISSING)
                local_value = local.get(key, _MISSING)
                remote_value = remote.get(key, _MISSING)
                if local_value is _MISSING:
                    value = _MISSING if remote_value == base_value else remote_value
                elif remote_value is _MISSING:
                    value = local_value if local_value != base_value else _MISSING
                elif base_value is _MISSING:
                    value = local_value if local_value != remote_value else remote_value
                else:
                    value = _three_way_merge_prefab(base_value, local_value, remote_value)
            if value is not _MISSING:
                merged[key] = copy.deepcopy(value)
        return merged

    if isinstance(base, list) and isinstance(local, list) and isinstance(remote, list):
        if len(base) == len(local) == len(remote):
            return [
                _three_way_merge_prefab(base_item, local_item, remote_item)
                for base_item, local_item, remote_item in zip(base, local, remote)
            ]
        # Concurrent structural edits cannot be matched safely without stable
        # element identities. Preserve the explicit instance override.
        return copy.deepcopy(local)

    # Both asset and instance changed the same scalar: the explicit instance
    # override wins, matching Unity-style per-instance override semantics.
    return copy.deepcopy(local)


def _propagate_applied_prefab(base_root: dict, updated_root: dict, snapshots,
                              prefab_guid: str, asset_database=None) -> bool:
    if not snapshots:
        return True

    from Infernux.engine.component_restore import (
        commit_prepared_game_object_document,
        preflight_game_object_python_components,
    )
    from Infernux.engine.prefab_manager import _stamp_prefab_guid

    prepared_updates = []
    try:
        for obj, runtime_document, local_document in snapshots:
            merged = _three_way_merge_prefab(base_root, local_document, updated_root)
            _stamp_prefab_guid(merged, prefab_guid, is_root=True)

            for key in _ROOT_INSTANCE_KEYS:
                if key in runtime_document:
                    merged[key] = copy.deepcopy(runtime_document[key])
            runtime_transform = runtime_document.get("transform")
            merged_transform = merged.get("transform")
            if isinstance(runtime_transform, dict) and isinstance(merged_transform, dict):
                for key in ("position", "rotation"):
                    if key in runtime_transform:
                        merged_transform[key] = copy.deepcopy(runtime_transform[key])

            prepared = preflight_game_object_python_components(
                merged,
                asset_database,
                preserve_document_ids=False,
                reference_scene=obj.scene,
            )
            prepared_updates.append((obj, merged, prepared))
    except Exception as exc:
        for _obj, _merged, prepared in prepared_updates:
            prepared.discard()
        Debug.log_error(f"Failed to preflight prefab instance propagation: {exc}")
        return False

    for index, (obj, merged, prepared) in enumerate(prepared_updates):
        try:
            if not commit_prepared_game_object_document(
                obj,
                merged,
                prepared,
                preserve_document_ids=False,
            ):
                raise RuntimeError("native ObjectGraph commit failed")
        except Exception as exc:
            prepared.discard()
            for _obj, _merged, remaining in prepared_updates[index + 1:]:
                remaining.discard()
            Debug.log_error(f"Failed to propagate applied prefab to scene instances: {exc}")
            return False
    return True


# ─── Internal helpers ─────────────────────────────────────────────────────

def _load_prefab_root(prefab_path: str) -> Optional[dict]:
    """Load and return the root_object dict from a .prefab file."""
    if not prefab_path:
        raise ValueError("prefab override comparison requires an asset path")
    from Infernux.engine.prefab_manager import _read_prefab_document

    return _read_prefab_document(prefab_path)["root_object"]


def _serialize_obj(obj) -> Optional[dict]:
    """Serialize a live GameObject to a dict."""
    from Infernux.engine.component_restore import (
        serialize_game_object_document_authoritatively,
    )

    return serialize_game_object_document_authoritatively(obj)


def _diff_node(instance: dict, prefab: dict, path: str,
               out: List[Override], *, is_root: bool = False):
    """Recursively diff one node."""
    node_name = instance.get("name", "")
    current_path = f"{path}/{node_name}" if path else node_name

    # Compare top-level scalar properties
    for key in set(instance.keys()) | set(prefab.keys()):
        if key in _SKIP_KEYS:
            continue
        if is_root and key in _ROOT_INSTANCE_KEYS:
            continue
        iv = instance.get(key)
        pv = prefab.get(key)
        if iv != pv:
            out.append(Override(current_path, key, pv, iv))

    # Compare transform sub-keys
    i_transform = instance.get("transform", {})
    p_transform = prefab.get("transform", {})
    for tk in _TRANSFORM_KEYS:
        if is_root and tk in ("position", "rotation"):
            continue
        iv = i_transform.get(tk)
        pv = p_transform.get(tk)
        if iv != pv:
            out.append(Override(current_path, f"transform.{tk}", pv, iv))

    # Compare components by type name matching
    _diff_components(
        instance.get("components", []),
        prefab.get("components", []),
        current_path, "components", out,
    )

    # Recurse children (match by index → name)
    i_children = instance.get("children", [])
    p_children = prefab.get("children", [])
    p_by_name = {c.get("name"): c for c in p_children}

    for i_child in i_children:
        child_name = i_child.get("name", "")
        p_child = p_by_name.get(child_name)
        if p_child is None:
            out.append(Override(current_path, f"added_child:{child_name}", None, child_name))
        else:
            _diff_node(i_child, p_child, current_path, out)

    for p_child in p_children:
        child_name = p_child.get("name", "")
        i_names = {c.get("name") for c in i_children}
        if child_name not in i_names:
            out.append(Override(current_path, f"removed_child:{child_name}", child_name, None))


def _diff_components(instance_comps: list, prefab_comps: list,
                     node_path: str, section: str,
                     out: List[Override]):
    """Diff component lists by type_name matching."""
    p_by_type: Dict[str, dict] = {}
    for c in prefab_comps:
        tn = c.get("type_id", "")
        if tn:
            p_by_type[tn] = c

    for ic in instance_comps:
        tn = ic.get("type_id", "")
        if not tn:
            continue
        pc = p_by_type.get(tn)
        if pc is None:
            out.append(Override(node_path, f"added_{section}:{tn}", None, tn))
            continue
        # Compare fields within this component
        skip = {"type_id", "component_id"}
        for key in set(ic.keys()) | set(pc.keys()):
            if key in skip:
                continue
            if ic.get(key) != pc.get(key):
                out.append(Override(node_path, f"{section}:{tn}.{key}",
                                   pc.get(key), ic.get(key)))

    i_types = {c.get("type_id", "") for c in instance_comps}
    for pc in prefab_comps:
        tn = pc.get("type_id", "")
        if tn and tn not in i_types:
            out.append(Override(node_path, f"removed_{section}:{tn}", tn, None))
