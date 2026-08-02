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
    ``prefab_root`` is authoritative, while the parent walk also tolerates
    older scenes that did not persist that flag correctly.
    """
    if instance_obj is None:
        return None
    guid = getattr(instance_obj, "prefab_guid", "") or ""
    if not guid:
        return None

    current = instance_obj
    candidate = instance_obj
    while current is not None:
        if (getattr(current, "prefab_guid", "") or "") != guid:
            break
        candidate = current
        if bool(getattr(current, "prefab_root", False)):
            return current
        try:
            current = current.get_parent()
        except Exception:
            break
    return candidate


def compute_overrides(instance_obj, prefab_path: str,
                      asset_database=None) -> List[Override]:
    """Compare *instance_obj* (live GameObject) against the .prefab file.

    Returns a list of Override objects describing every property difference.
    """
    instance_obj = resolve_prefab_instance_root(instance_obj) or instance_obj
    prefab_data = _load_prefab_root(prefab_path)
    if prefab_data is None:
        return []

    instance_data = _serialize_obj(instance_obj)
    if instance_data is None:
        return []

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

    Debug.log_internal(f"Applied overrides to prefab: {os.path.basename(prefab_path)}")
    return True


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

    Debug.log_internal("Reverted prefab instance to source.")
    return True


def revert_overrides_with_undo(instance_obj, prefab_path: str,
                               asset_database=None) -> bool:
    """Revert a linked instance as one structural Undo command."""
    instance_obj = resolve_prefab_instance_root(instance_obj) or instance_obj
    reverted_document = _build_reverted_prefab_document(instance_obj, prefab_path)
    if reverted_document is None:
        Debug.log_error("Failed to load prefab for revert.")
        return False
    try:
        from Infernux.engine.component_restore import (
            serialize_game_object_document_authoritatively,
        )
        before_document = serialize_game_object_document_authoritatively(instance_obj)
        from Infernux.engine.undo import PrefabRevertCommand, UndoManager
        manager = UndoManager.instance()
        if manager is None:
            return revert_overrides(instance_obj, prefab_path, asset_database)
        if not manager.execute(PrefabRevertCommand(
            instance_obj.id,
            before_document,
            reverted_document,
            asset_database,
        )):
            return False
    except Exception as exc:
        Debug.log_error(f"Failed to record prefab revert: {exc}")
        return False
    Debug.log_internal("Reverted prefab instance to source.")
    return True


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
    if not prefab_path or not os.path.isfile(prefab_path):
        return None
    try:
        from Infernux.engine.prefab_manager import _read_prefab_document
        return _read_prefab_document(prefab_path)["root_object"]
    except (OSError, ValueError) as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return None


def _serialize_obj(obj) -> Optional[dict]:
    """Serialize a live GameObject to a dict."""
    try:
        from Infernux.engine.component_restore import (
            serialize_game_object_document_authoritatively,
        )

        return serialize_game_object_document_authoritatively(obj)
    except Exception as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return None


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
