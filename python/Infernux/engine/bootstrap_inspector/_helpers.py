"""Standalone utility functions for inspector wiring.

These helpers do not depend on any wiring-time closure state.
"""
from __future__ import annotations

from Infernux.debug import Debug


def _safe_sequence(values):
    if values is None:
        return []
    if isinstance(values, list):
        return values
    try:
        return list(values)
    except Exception as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return []


def _get_components_safe(obj):
    if obj is None:
        return []
    try:
        return _safe_sequence(obj.get_components())
    except Exception as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return []


def _get_py_components_safe(obj):
    if obj is None:
        return []
    try:
        return _safe_sequence(obj.get_py_components())
    except Exception as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return []


def _get_component_script_error(comp, asset_database):
    """Resolve project-script diagnostics without probing engine component GUIDs."""
    if getattr(comp, "_is_broken", False):
        return getattr(comp, "_broken_error", "") or "Script failed to load"

    script_guid = getattr(comp, "_script_guid", "") or ""
    asset_guid = getattr(type(comp), "_asset_script_guid_", "") or ""
    if not script_guid or script_guid != asset_guid:
        return None

    script_path = getattr(comp, "_script_path", "") or ""
    if not script_path and asset_database is not None:
        script_path = asset_database.get_path_from_guid(script_guid) or ""
    if not script_path:
        return None

    from Infernux.components.script_loader import get_script_error_by_path
    return get_script_error_by_path(script_path)


def _can_remove_component(obj, comp, type_name, is_native):
    """Check whether *comp* may be removed from *obj*."""
    if is_native:
        blockers = []
        if hasattr(obj, 'get_remove_component_blockers'):
            try:
                blockers = list(obj.get_remove_component_blockers(comp) or [])
            except RuntimeError:
                blockers = []
        can_remove = not blockers
        if can_remove and hasattr(obj, 'can_remove_component'):
            can_remove = bool(obj.can_remove_component(comp))
        if not can_remove:
            suffix = (
                f" required by: {', '.join(blockers)}"
                if blockers else
                "another component depends on it"
            )
            Debug.log_warning(f"Cannot remove '{type_name}' — {suffix}")
            return False
    return True


def _get_add_component_entries():
    """Convert the component registry snapshot to native menu entries."""
    from Infernux.lib import InspectorAddComponentEntry, get_registered_component_types
    from Infernux.engine.project_context import get_project_root
    project_root = get_project_root() or ""

    entries = []
    from Infernux.components.builtin_component import BuiltinComponent
    for type_name in sorted(get_registered_component_types()):
        if type_name == "Transform":
            continue
        e = InspectorAddComponentEntry()
        e.display_name = type_name
        # Use BuiltinComponent wrapper category if available
        wrapper_cls = BuiltinComponent._builtin_registry.get(type_name)
        e.category = getattr(wrapper_cls, '_component_category_', "Built-in") if wrapper_cls else "Built-in"
        e.is_native = True
        entries.append(e)

    from Infernux.components.registry import get_component_registrations
    seen = {e.display_name for e in entries}
    for registration in get_component_registrations(project_root=project_root):
        if registration.type_name in seen:
            continue
        e = InspectorAddComponentEntry()
        e.display_name = registration.type_name
        e.category = registration.category
        e.is_native = False
        e.script_path = registration.script_path
        entries.append(e)
        seen.add(registration.type_name)

    entries.sort(key=lambda entry: (entry.category.casefold(), entry.display_name.casefold()))
    return entries


def _load_script_component(script_path, asset_database):
    """Load a script component, returning the instance or ``None``."""
    from Infernux.components import load_and_create_component
    try:
        instance = load_and_create_component(
            script_path, asset_database=asset_database)
    except Exception as exc:
        Debug.log_error(f"Failed to load script '{script_path}': {exc}")
        return None
    if instance is None:
        Debug.log_error(f"No InxComponent found in '{script_path}'")
    return instance
