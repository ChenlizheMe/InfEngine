"""
Authoritative registry and lookup for Python component types.

Component classes register at definition time. Project scripts additionally
publish source descriptors after background validation, so editor menus never
scan the filesystem or execute user modules on demand.

Usage:
    from Infernux.components import get_type, T

    # Get component type by name
    TestCache = get_type("testcache")
    comp = self.game_object.get_component(TestCache)

    # Or use the T shorthand
    comp = self.game_object.get_component(T.testcache)
"""

from dataclasses import dataclass
import ast
import importlib
import os
import sys
import threading
from typing import Dict, Type, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from .component import InxComponent


@dataclass(frozen=True)
class ComponentRegistration:
    """Immutable description consumed by the Add Component menu."""

    type_name: str
    category: str
    menu_path: str
    script_path: str
    component_type: Optional[Type['InxComponent']]
    project_script: bool
    allow_multiple: bool = True
    user_addable: bool = True
    removable: bool = True
    intrinsic: bool = False
    required_types: tuple[object, ...] = ()
    incompatible_types: tuple[object, ...] = ()
    exclusive_groups: tuple[str, ...] = ()
    satisfied_types: tuple[str, ...] = ()


_registration_lock = threading.RLock()
_type_registrations: dict[str, ComponentRegistration] = {}
_script_registrations: dict[str, ComponentRegistration] = {}
_registration_revision = 0
_engine_catalog_loaded = False

_ENGINE_COMPONENT_MODULES = (
    "Infernux.components.particle_system",
    "Infernux.components.skeletal_animator",
    "Infernux.components.spirit_animator",
    "Infernux.components.timeline_action",
    "Infernux.renderstack.render_stack",
    "Infernux.ui.ui_canvas",
    "Infernux.ui.ui_text",
    "Infernux.ui.ui_image",
    "Infernux.ui.ui_button",
)


def ensure_engine_component_catalog_loaded() -> None:
    """Load every engine-owned, user-addable Python component exactly once."""
    global _engine_catalog_loaded
    with _registration_lock:
        if _engine_catalog_loaded:
            return
        _engine_catalog_loaded = True
    try:
        for module_name in _ENGINE_COMPONENT_MODULES:
            importlib.import_module(module_name)
    except Exception:
        with _registration_lock:
            _engine_catalog_loaded = False
        raise


def _normalized_path(path: str) -> str:
    from Infernux.engine.path_utils import path_key

    return path_key(path) if path else ""


def _module_file(component_type: type) -> str:
    from Infernux.engine.path_utils import resolved_path

    module = sys.modules.get(component_type.__module__)
    return resolved_path(getattr(module, "__file__", "") or "") if module else ""


def _is_project_script(path: str) -> bool:
    if not path:
        return False
    from Infernux.engine.project_context import get_assets_root
    from Infernux.engine.path_utils import is_path_within

    assets_root = get_assets_root()
    return bool(assets_root and is_path_within(path, assets_root))


def _bump_revision() -> None:
    global _registration_revision
    _registration_revision += 1


def register_component_type(component_type: Type['InxComponent'], *, script_path: str = "") -> None:
    """Register the newest loaded definition of one Python component type."""
    if getattr(component_type, "_is_broken", False):
        return
    identity = getattr(component_type, "_get_type_guid", None)
    if not callable(identity):
        return
    from Infernux.engine.path_utils import resolved_path

    source_path = script_path or _module_file(component_type)
    path = resolved_path(source_path) if source_path else ""
    project_script = bool(script_path) or _is_project_script(path)
    menu_path = str(getattr(component_type, "_component_menu_path_", "") or "")
    category = str(getattr(component_type, "_component_category_", "") or "")
    if not category:
        category = "Scripts" if project_script else (menu_path.rsplit("/", 1)[0] if "/" in menu_path else "Engine")
    registration = ComponentRegistration(
        type_name=component_type.__name__,
        category=category,
        menu_path=menu_path,
        script_path=path if project_script else "",
        component_type=component_type,
        project_script=project_script,
        allow_multiple=not bool(getattr(component_type, "_disallow_multiple_", False)),
        user_addable=bool(getattr(component_type, "_component_user_addable_", True)),
        removable=bool(getattr(component_type, "_component_removable_", True)),
        intrinsic=bool(getattr(component_type, "_component_intrinsic_", False)),
        required_types=tuple(getattr(component_type, "_require_components_", ()) or ()),
        incompatible_types=tuple(getattr(component_type, "_incompatible_components_", ()) or ()),
        exclusive_groups=tuple(getattr(component_type, "_component_exclusive_groups_", ()) or ()),
        satisfied_types=tuple(getattr(component_type, "_component_satisfied_types_", ()) or ()),
    )
    type_key = str(identity())
    with _registration_lock:
        if project_script and path:
            normalized = _normalized_path(path)
            for key, previous in tuple(_type_registrations.items()):
                if previous.project_script and _normalized_path(previous.script_path) == normalized:
                    _type_registrations.pop(key, None)
        _type_registrations[type_key] = registration
        if project_script and path:
            _script_registrations[_normalized_path(path)] = registration
        _bump_revision()


def _direct_component_declarations(file_path: str) -> tuple[str, ...]:
    """Read direct InxComponent declarations without executing user code."""
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as stream:
            tree = ast.parse(stream.read(), filename=file_path)
    except (OSError, SyntaxError, UnicodeError):
        return ()

    names = []
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        for base in node.bases:
            base_name = base.id if isinstance(base, ast.Name) else (
                base.attr if isinstance(base, ast.Attribute) else ""
            )
            if base_name == "InxComponent":
                names.append(node.name)
                break
    return tuple(names)


def register_component_script(file_path: str) -> bool:
    """Index one validated project script in the component registry."""
    from Infernux.engine.path_utils import resolved_path

    path = resolved_path(file_path)
    declarations = _direct_component_declarations(path)
    key = _normalized_path(path)
    with _registration_lock:
        if len(declarations) != 1:
            if _script_registrations.pop(key, None) is not None:
                _bump_revision()
            return False
        previous = _script_registrations.get(key)
        registration = ComponentRegistration(
            type_name=declarations[0],
            category="Scripts",
            menu_path="",
            script_path=path,
            component_type=(
                previous.component_type
                if previous is not None and previous.type_name == declarations[0]
                else None
            ),
            project_script=True,
            allow_multiple=(previous.allow_multiple if previous is not None else True),
            user_addable=(previous.user_addable if previous is not None else True),
            removable=(previous.removable if previous is not None else True),
            intrinsic=(previous.intrinsic if previous is not None else False),
            required_types=(previous.required_types if previous is not None else ()),
            incompatible_types=(previous.incompatible_types if previous is not None else ()),
            exclusive_groups=(previous.exclusive_groups if previous is not None else ()),
            satisfied_types=(previous.satisfied_types if previous is not None else ()),
        )
        if previous != registration:
            _script_registrations[key] = registration
            _bump_revision()
    return True


def unregister_component_script(file_path: str) -> None:
    """Remove a deleted or invalid project script from the menu registry."""
    key = _normalized_path(file_path)
    with _registration_lock:
        if _script_registrations.pop(key, None) is not None:
            _bump_revision()


def get_component_registrations(*, project_root: str = "") -> tuple[ComponentRegistration, ...]:
    """Return one stable Add Component snapshot without filesystem work."""
    from Infernux.engine.path_utils import is_path_within

    ensure_engine_component_catalog_loaded()

    with _registration_lock:
        engine_entries = [
            entry
            for entry in _type_registrations.values()
            if not entry.project_script and entry.menu_path
            and entry.user_addable and not entry.intrinsic
        ]
        project_entries = [
            entry for entry in _script_registrations.values()
            if entry.user_addable and not entry.intrinsic
        ]
    if project_root:
        assets_root = os.path.join(project_root, "Assets")
        project_entries = [
            entry for entry in project_entries
            if entry.script_path and is_path_within(entry.script_path, assets_root)
        ]
    merged = {entry.type_name: entry for entry in engine_entries}
    merged.update({entry.type_name: entry for entry in project_entries})
    return tuple(sorted(merged.values(), key=lambda entry: (entry.category.casefold(), entry.type_name.casefold())))


def get_component_registration_revision() -> int:
    with _registration_lock:
        return _registration_revision


def get_component_constraints(component_type: type) -> ComponentRegistration:
    """Return the final decorator-aware registry record for a loaded type."""
    if not isinstance(component_type, type):
        raise TypeError("component constraints require a component type")
    identity = getattr(component_type, "_get_type_guid", None)
    if not callable(identity):
        raise TypeError("component type has no stable registry identity")
    type_key = str(identity())
    with _registration_lock:
        registration = _type_registrations.get(type_key)
    if registration is None or registration.component_type is not component_type:
        register_component_type(component_type)
        with _registration_lock:
            registration = _type_registrations.get(type_key)
    if registration is None:
        raise LookupError(f"component type '{component_type.__name__}' is not registered")
    return registration


def component_constraint_type_id(value: object) -> str:
    """Return the canonical constraint token for a component declaration.

    Class references are exact identities. String declarations intentionally
    remain semantic aliases (for example ``"Collider"``).
    """
    if isinstance(value, str):
        return value
    cpp_name = str(getattr(value, "_cpp_type_name", "") or "")
    if cpp_name:
        return f"native:{cpp_name}"
    identity = getattr(value, "_get_type_guid", None)
    if callable(identity):
        type_guid = str(identity() or "")
        if type_guid:
            return f"python:{type_guid}"
    return str(getattr(value, "__name__", "") or "")


def get_python_attachment_blockers(game_object, component_type: type) -> tuple[str, ...]:
    """Evaluate one loaded Python type against an object's live components."""
    registration = get_component_constraints(component_type)
    blockers = []
    if registration.intrinsic:
        blockers.append("intrinsic components cannot be attached")
    if not registration.user_addable:
        blockers.append("component is not user-addable")
    existing = tuple(game_object.get_py_components() or ())
    if not registration.allow_multiple and any(
        isinstance(component, component_type) for component in existing
    ):
        blockers.append("only one instance is allowed per GameObject")

    candidate_incompatible = {
        component_constraint_type_id(value)
        for value in registration.incompatible_types
        if component_constraint_type_id(value)
    }
    for component in existing:
        existing_type = type(component)
        existing_registration = get_component_constraints(existing_type)
        existing_name = existing_type.__name__
        existing_identity = component_constraint_type_id(existing_type)
        candidate_identity = component_constraint_type_id(component_type)
        existing_incompatible = {
            component_constraint_type_id(value)
            for value in existing_registration.incompatible_types
            if component_constraint_type_id(value)
        }
        existing_aliases = {
            existing_name,
            existing_identity,
            *(component_constraint_type_id(value) for value in existing_registration.satisfied_types),
        }
        candidate_aliases = {
            component_type.__name__,
            candidate_identity,
            *(component_constraint_type_id(value) for value in registration.satisfied_types),
        }
        if candidate_incompatible & existing_aliases or existing_incompatible & candidate_aliases:
            blockers.append(f"incompatible with existing component '{existing_name}'")
        if set(registration.exclusive_groups) & set(existing_registration.exclusive_groups):
            blockers.append(
                f"exclusive component group already owned by '{existing_name}'"
            )
    return tuple(sorted(set(blockers)))


def get_type(name: str) -> Optional[Type['InxComponent']]:
    """
    Get a component class by its name.

    Reads the latest loaded definition from the component registry.

    Args:
        name: The class name (e.g., "testcache", "PlayerController")

    Returns:
        The component class, or None if not found
    """
    with _registration_lock:
        matches = [
            entry.component_type
            for entry in _type_registrations.values()
            if entry.type_name == name and entry.component_type is not None
        ]
    return matches[-1] if matches else None


def get_type_by_identity(
    name: str,
    script_guid: str,
    type_guid: str,
) -> Optional[Type['InxComponent']]:
    """Resolve exactly one component class by its complete stable identity."""
    with _registration_lock:
        component_types = [
            entry.component_type
            for entry in _type_registrations.values()
            if entry.component_type is not None
        ]
    matches = []
    for component_type in component_types:
        known_script_guids = {
            component_type._get_intrinsic_script_guid(),
            getattr(component_type, "_asset_script_guid_", ""),
        }
        if (
            component_type.__name__ == name
            and type_guid == component_type._get_type_guid()
            and script_guid in known_script_guids
        ):
            matches.append(component_type)
    # Script hot reload leaves historical class objects alive while existing
    # references drain. Equal stable identities are successive definitions of
    # the same authored component, so the newest definition is authoritative.
    return matches[-1] if matches else None


def get_all_types() -> Dict[str, Type['InxComponent']]:
    """
    Get all known InxComponent subclass types.

    Returns:
        Dictionary of class_name -> class_type
    """
    with _registration_lock:
        registrations = tuple(_type_registrations.values())
    return {
        entry.type_name: entry.component_type
        for entry in registrations
        if entry.component_type is not None
    }


class _TypeAccessor:
    """
    Dynamic attribute accessor for component types.

    Allows accessing component types as attributes:
        T.testcache  -> get_type("testcache")
        T.Movement   -> get_type("Movement")
    """

    def __getattr__(self, name: str) -> Optional[Type['InxComponent']]:
        """Get component type by attribute access."""
        result = get_type(name)
        if result is None:
            return None
        return result

    def __repr__(self) -> str:
        types = list(get_all_types().keys())
        return f"<ComponentTypes: {types}>"


# Global instance for easy access: T.MyComponent
T = _TypeAccessor()
