"""
Script loader for dynamically importing InxComponent subclasses from .py files.

This module provides utilities to load Python scripts and extract component classes
for use in the Infernux editor. Used for drag-and-drop script attachment.
"""

import os
import sys
import importlib
import importlib.util
import inspect
from typing import Type, List, Optional

from Infernux.engine.path_utils import path_key, resolved_path
from Infernux.engine.project_context import (
    get_script_module_name,
    resolve_script_path,
    temporary_script_import_paths,
)

from .component import InxComponent


class ScriptLoadError(Exception):
    """Raised when a script cannot be loaded or doesn't contain valid components."""
    pass


# ---------------------------------------------------------------------------
# Script error tracking — allows the editor to know which scripts are broken
# without crashing.  Components backed by broken scripts can still be
# *attached* to GameObjects (they keep their serialized data), but Play
# mode is blocked until every script compiles cleanly.
# ---------------------------------------------------------------------------

# Maps normalised absolute path → error message string
_script_errors: dict[str, str] = {}
_script_error_revision = 0


def _set_script_error_key(key: str, message: str | None) -> None:
    """Update one normalized error entry and advance the change revision."""
    global _script_error_revision
    previous = _script_errors.get(key)
    if message is None:
        if key not in _script_errors:
            return
        _script_errors.pop(key, None)
    else:
        if previous == message:
            return
        _script_errors[key] = message
    _script_error_revision += 1


def _normalize_script_path(file_path: str) -> str:
    """Return a stable absolute key for script-error bookkeeping."""
    return path_key(file_path)


def _unique_module_name_for_path(file_path: str) -> str:
    """Build a fallback module name for scripts without a valid import path."""
    import hashlib

    normalized_path = path_key(file_path)
    module_name = os.path.splitext(os.path.basename(file_path))[0]
    path_hash = hashlib.md5(normalized_path.encode()).hexdigest()[:8]
    return f"infernux_script_{module_name}_{path_hash}"


def _clear_loaded_script_modules(module_names: List[str]) -> None:
    """Drop cached script modules and clear serialized-field metadata."""
    if not module_names:
        return

    from .serialized_field import clear_serialized_fields_cache

    seen_module_ids: set[int] = set()
    for module_name in module_names:
        old_module = sys.modules.get(module_name)
        if old_module is None or id(old_module) in seen_module_ids:
            continue
        seen_module_ids.add(id(old_module))

        old_module_name = getattr(old_module, "__name__", "")
        for _, obj in inspect.getmembers(old_module, inspect.isclass):
            if getattr(obj, '__module__', None) != old_module_name:
                continue
            if '_serialized_fields_' in obj.__dict__:
                clear_serialized_fields_cache(obj)
                obj._serialized_fields_ = {}

    for module_name in module_names:
        sys.modules.pop(module_name, None)


def _record_script_error(file_path: str, exc: Exception) -> None:
    """Record that *file_path* failed to load with *exc*."""
    import traceback
    norm = _normalize_script_path(file_path)
    tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    _set_script_error_key(norm, tb_str)
    # Also log to Console so the user sees it
    try:
        from Infernux.debug import Debug
        Debug.log_error(tb_str, source_file=file_path, source_line=0)
    except ImportError:
        import sys
        print(tb_str, file=sys.stderr)


def _load_script_module(file_path: str, module_name: str):
    """Execute the exact script artifact resolved from its asset GUID.

    ``import_module`` performs a second path search. That is unnecessary for
    editor sources and unreliable for external sourceless modules in a Nuitka
    standalone Player. Register the canonical name before execution so cyclic
    imports resolve to the same module object.
    """
    spec = importlib.util.spec_from_file_location(module_name, file_path)
    if spec is None or spec.loader is None:
        raise ScriptLoadError(f"Failed to create module spec for {file_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        if sys.modules.get(module_name) is module:
            sys.modules.pop(module_name, None)
        raise
    return module


def set_script_error(file_path: str, message: str) -> None:
    """Record an error message for a script (no exception object needed)."""
    _set_script_error_key(_normalize_script_path(file_path), message)


def _clear_script_error(file_path: str) -> None:
    """Clear any previously recorded error for *file_path*."""
    _set_script_error_key(_normalize_script_path(file_path), None)


def clear_deleted_script_errors(path: str) -> list[str]:
    """Forget tracked script errors for a deleted script path.

    Accepts either a single script file or a directory path. Directory cleanup is
    useful for editor-side recursive deletes where every nested broken script
    should stop blocking Play Mode immediately instead of waiting for a restart.
    """
    if not path:
        return []

    normalized = _normalize_script_path(path)
    removed: list[str] = []

    if os.path.isdir(path):
        prefix = normalized.rstrip("\\/") + os.sep
        for key in list(_script_errors.keys()):
            if key == normalized or key.startswith(prefix):
                _set_script_error_key(key, None)
                removed.append(key)
        return removed

    if normalized in _script_errors:
        _set_script_error_key(normalized, None)
        removed.append(normalized)
    return removed


def get_script_errors() -> dict[str, str]:
    """Return a snapshot of all currently broken scripts {path: traceback}."""
    return dict(_script_errors)


def has_script_errors() -> bool:
    """Return True if any loaded script has unresolved errors."""
    return bool(_script_errors)


def get_script_error_revision() -> int:
    """Return a monotonic revision changed only when diagnostics change."""
    return _script_error_revision


def get_script_error_by_path(file_path: str) -> Optional[str]:
    """Return the error string for *file_path*, or ``None`` if it loaded OK."""
    return _script_errors.get(_normalize_script_path(file_path))


def load_component_from_file(file_path: str) -> Type[InxComponent]:
    """
    Load the first InxComponent subclass from a Python file.
    
    Args:
        file_path: Absolute path to the .py file
        
    Returns:
        The first InxComponent subclass found in the file
        
    Raises:
        ScriptLoadError: If file doesn't exist, can't be imported, or contains no components
    """
    components = load_all_components_from_file(file_path)
    if not components:
        raise ScriptLoadError(f"No InxComponent subclasses found in {file_path}")
    if len(components) > 1:
        names = ", ".join(cls.__name__ for cls in components)
        raise ScriptLoadError(
            f"Script '{file_path}' defines multiple InxComponent classes ({names}). "
            "Dragging or attaching by script file requires exactly one component class."
        )
    return components[0]


def load_all_components_from_file(file_path: str) -> List[Type[InxComponent]]:
    """
    Load all InxComponent subclasses from a Python file.
    
    Args:
        file_path: Absolute path to the .py file
        
    Returns:
        List of InxComponent subclasses found in the file (may be empty)
        
    Raises:
        ScriptLoadError: If file doesn't exist or can't be imported
    """
    # Resolve path (project-relative allowed)
    file_path = resolve_script_path(file_path)

    # Validate file exists
    if not os.path.exists(file_path):
        raise ScriptLoadError(f"Script file not found: {file_path}")
    
    if not file_path.endswith(('.py', '.pyc')):
        raise ScriptLoadError(f"Not a Python file: {file_path}")
    
    module_name = get_script_module_name(file_path) or _unique_module_name_for_path(file_path)
    _clear_loaded_script_modules([module_name])

    importlib.invalidate_caches()

    # Execute the module — catch errors so a broken script never crashes the editor
    try:
        with temporary_script_import_paths(file_path):
            module = _load_script_module(file_path, module_name)
    except Exception as exc:
        # Track this script as having a load error
        _record_script_error(file_path, exc)
        # Return empty list — the component can still be referenced by GUID/type
        # but will not be instantiable until the script is fixed.
        return []

    # If we get here the script loaded successfully — clear any prior error
    _clear_script_error(file_path)

    # Direct imports and engine-created component instances share class identity.
    sys.modules[module_name] = module

    # Find all InxComponent subclasses in the module
    components = []
    for name, obj in inspect.getmembers(module, inspect.isclass):
        # Check if it's a subclass of InxComponent (but not InxComponent itself)
        if issubclass(obj, InxComponent) and obj is not InxComponent:
            # Ensure it's defined in this module (not imported)
            if obj.__module__ == module_name:
                components.append(obj)

    from .registry import register_component_type
    for component_type in components:
        register_component_type(component_type, script_path=file_path)

    return components


def load_component_class_from_file(file_path: str, type_name: str = "") -> Optional[Type[InxComponent]]:
    """Load a specific component class from a Python file.

    When ``type_name`` is provided, prefer an exact class-name match. If the
    authored name is missing but the file still defines exactly one
    ``InxComponent`` subclass, return that class so a pure class rename
    (same script GUID / one-component-per-file) keeps scene references alive.
    """
    components = load_all_components_from_file(file_path)
    if not components:
        return None

    if type_name:
        for component_class in components:
            if component_class.__name__ == type_name:
                return component_class
        if len(components) == 1:
            return components[0]
        return None

    if len(components) != 1:
        return None

    return components[0]



def create_component_instance(component_class: Type[InxComponent]) -> InxComponent:
    """
    Create an instance of a component class.
    
    Args:
        component_class: The InxComponent subclass to instantiate
        
    Returns:
        New instance of the component
        
    Raises:
        ScriptLoadError: If instantiation fails
    """
    return component_class()


def load_and_create_component(
    file_path: str,
    asset_database=None,
    type_name: str = "",
    *,
    script_guid: str = "",
) -> Optional[InxComponent]:
    """
    Convenience function: Load first component from file and create instance.
    
    Args:
        file_path: Absolute path to the .py file
        
    Returns:
        New instance of the first component found, or None if the script
        has errors (the error is already logged to Console).
        
    Note:
        ``script_guid`` should be supplied when the caller already resolved
        ``file_path`` from a stable component identity. Packaged ``.pyc``
        artifacts do not necessarily support a reverse path-to-GUID lookup.

    Raises:
        ScriptLoadError: If AssetDatabase is missing or GUID cannot be resolved.
    """
    if asset_database is None and not script_guid:
        raise ScriptLoadError("AssetDatabase is required for script components (GUID-only mode)")

    if type_name:
        component_class = load_component_class_from_file(file_path, type_name=type_name)
        if component_class is None:
            # Script had errors, contains no InxComponent subclasses, or no longer
            # defines the requested component type.
            return None
    else:
        component_class = load_component_from_file(file_path)

    instance = create_component_instance(component_class)
    # Resolve and store script GUID
    guid = script_guid or asset_database.get_guid_from_path(file_path)
    if not guid and asset_database is not None:
        from Infernux.core.assets import AssetManager
        mutation = AssetManager.import_asset(
            file_path,
            database=asset_database,
            suppress_watcher_echo=False,
        )
        guid = mutation.guid
    if not guid:
        raise ScriptLoadError(f"Failed to resolve GUID for script: {file_path}")
    from Infernux.components.component_identity import bind_asset_script_guid
    bind_asset_script_guid(component_class, guid)
    instance._script_guid = guid
    instance._script_path = resolved_path(file_path)
    return instance


def get_component_info(component_class: Type[InxComponent]) -> dict:
    """
    Extract metadata from a component class.
    
    Args:
        component_class: The InxComponent subclass
        
    Returns:
        Dictionary with component metadata (name, docstring, fields)
    """
    from .serialized_field import get_serialized_fields
    
    return {
        'name': component_class.__name__,
        'module': component_class.__module__,
        'docstring': inspect.getdoc(component_class) or "",
        'fields': list(get_serialized_fields(component_class).keys()),
    }


# Example usage (for testing):
if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        script_path = sys.argv[1]
        print(f"Loading components from: {script_path}")

        components = load_all_components_from_file(script_path)
        print(f"Found {len(components)} component(s):")

        for comp_class in components:
            info = get_component_info(comp_class)
            print(f"\n  - {info['name']}")
            print(f"    Doc: {info['docstring'][:50]}...")
            print(f"    Fields: {info['fields']}")

            # Try to instantiate
            instance = create_component_instance(comp_class)
            print(f"    [OK] Instantiation successful")

    else:
        print("Usage: python script_loader.py <path_to_script.py>")
