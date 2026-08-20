"""
Null-safe reference wrappers for GameObject and Component.

These wrappers track the validity of references so that accessing a
destroyed/missing object returns ``None`` instead of crashing with a
C++ exception.

``GameObjectRef`` stores a persistent scene-ID and lazily resolves the
live object via ``Scene.find_by_id``.  If the object has been destroyed
the wrapper evaluates to falsy and all attribute access returns ``None``.

``MaterialRef`` is defined in ``Infernux.core.asset_ref`` and re-exported
here for module-level import convenience.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

# Re-export MaterialRef from core so existing callers still work.
from Infernux.core.asset_ref import MaterialRef  # noqa: F401
from Infernux.debug import Debug

_log = logging.getLogger("Infernux.ref")


def _iter_reference_scenes():
    """Return every live scene that can own a serialized runtime reference."""
    try:
        from Infernux.lib import SceneManager as _SM

        manager = _SM.instance()
    except (ImportError, RuntimeError):
        return ()

    scenes = []
    for getter_name in ("get_active_scene", "get_runtime_persistent_scene"):
        getter = getattr(manager, getter_name, None)
        if not callable(getter):
            continue
        try:
            scene = getter()
        except RuntimeError:
            continue
        if scene is not None and all(scene is not existing for existing in scenes):
            scenes.append(scene)
    return tuple(scenes)


def _get_prefab_asset_database():
    try:
        from Infernux.core.asset_ref import _get_asset_database
        db = _get_asset_database()
        if db is not None:
            return db
    except Exception as exc:
        Debug.log_suppressed("ref_wrappers._get_asset_database.editor_path", exc)

    try:
        from Infernux.lib import AssetRegistry
        registry = AssetRegistry.instance()
        if registry is not None:
            return registry.get_asset_database()
    except Exception as exc:
        Debug.log_suppressed("ref_wrappers._get_asset_database.registry_path", exc)

    return None


def _is_python_component_entry(component) -> bool:
    try:
        from .component import InxComponent
        return isinstance(component, InxComponent) or hasattr(component, "get_py_component")
    except Exception:
        return hasattr(component, "get_py_component")


# ============================================================================
# GameObjectRef — Null-safe, persistent-ID based reference
# ============================================================================

class GameObjectRef:
    """Null-safe wrapper around a scene GameObject.

    Stores the persistent ``id`` (uint64, written into the .scene file) and
    lazily resolves the live C++ object each time it is accessed.  If the
    object has been destroyed or the scene was reloaded, the wrapper simply
    returns ``None`` instead of raising a pybind11 segfault.

    Supports truthiness check::

        if self.target:   # False when target is None or destroyed
            self.target.name
    """

    __slots__ = ("_persistent_id", "_cached_obj", "_cached_handle")

    def __init__(self, game_object=None, *, persistent_id: int = 0):
        if game_object is not None:
            self._persistent_id: int = int(game_object.id)
            self._cached_obj = game_object
            self._cached_handle = getattr(game_object, "handle", None)
        else:
            self._persistent_id = int(persistent_id)
            self._cached_obj = None
            self._cached_handle = None

    # -- resolution --------------------------------------------------------

    def _resolve(self):
        """Try to resolve the live object from active or persistent runtime scenes."""
        if self._persistent_id == 0:
            self._cached_obj = None
            self._cached_handle = None
            return None
        try:
            for scene in _iter_reference_scenes():
                obj = scene.find_by_id(self._persistent_id)
                if obj is not None:
                    self._cached_obj = obj
                    self._cached_handle = getattr(obj, "handle", None)
                    return obj
        except (ImportError, RuntimeError):
            # pybind11 raises RuntimeError when the C++ object behind a
            # GameObjectRef has been destroyed — that's the normal
            # "reference is dead" path, no need to log.
            pass
        except Exception as exc:
            _log.warning("GameObjectRef._resolve failed: %s", exc)
        self._cached_obj = None
        self._cached_handle = None
        return None

    # -- public API --------------------------------------------------------

    @property
    def persistent_id(self) -> int:
        """The persistent ID stored in the scene file."""
        return self._persistent_id

    def resolve(self):
        """Return the live GameObject, or ``None`` if destroyed/missing."""
        obj = self._cached_obj
        handle = self._cached_handle
        if obj is not None and handle is not None:
            try:
                for scene in _iter_reference_scenes():
                    resolved = scene.resolve_game_object(handle)
                    if resolved is not None:
                        self._cached_obj = resolved
                        return resolved
            except (ImportError, RuntimeError):
                pass
            self._cached_obj = None
            self._cached_handle = None
        elif obj is not None:
            # Non-native stand-ins used by tooling/tests do not expose handles.
            try:
                if int(obj.id) == self._persistent_id:
                    return obj
            except (RuntimeError, AttributeError, TypeError, ValueError):
                self._cached_obj = None
        return self._resolve()

    def __copy__(self):
        return type(self)(persistent_id=self._persistent_id)

    def __deepcopy__(self, memo):
        copied = type(self)(persistent_id=self._persistent_id)
        memo[id(self)] = copied
        return copied

    # -- convenience attribute forwarding ----------------------------------

    def __getattr__(self, name: str) -> Any:
        """Forward attribute access to the underlying GameObject."""
        # Avoid infinite recursion for our own slots
        if name.startswith("_"):
            raise AttributeError(name)
        obj = self.resolve()
        if obj is None:
            return None
        return getattr(obj, name)

    def __bool__(self) -> bool:
        return self.resolve() is not None

    def __eq__(self, other):
        if other is None:
            return self._persistent_id == 0
        if isinstance(other, GameObjectRef):
            return self._persistent_id == other._persistent_id
        # Compare to raw GameObject
        if hasattr(other, "id"):
            return self._persistent_id == other.id
        return NotImplemented

    def __hash__(self):
        return hash(self._persistent_id)

    def instantiate(self, *args, **kwargs):
        """Clone this referenced GameObject using Unity-style instantiate overloads."""
        try:
            from Infernux.lib import GameObject
            return GameObject.instantiate(self, *args, **kwargs)
        except Exception as exc:
            _log.warning("GameObjectRef.instantiate failed: %s", exc)
            return None

    def __repr__(self):
        obj = self.resolve()
        if obj is not None:
            return f"GameObjectRef('{obj.name}', id={self._persistent_id})"
        return f"GameObjectRef(None, id={self._persistent_id})"


# ============================================================================
# PrefabRef — Asset reference to a .prefab file (no scene instantiation)
# ============================================================================

class PrefabRef:
    """Reference to a prefab asset stored on disk.

    Unlike ``GameObjectRef`` which points to a live scene object,
    ``PrefabRef`` stores the asset GUID and file-path hint of a
    ``.prefab`` file.  Use :meth:`instantiate` to create a new
    scene object from the prefab.

    Compatible with ``FieldType.GAME_OBJECT`` — a single field can
    hold either a ``GameObjectRef`` or a ``PrefabRef``.
    """

    __slots__ = ("_guid", "_path_hint", "_name_cache", "_name_cache_path", "_name_cache_stamp")

    def __init__(self, guid: str = "", path_hint: str = ""):
        self._guid: str = guid
        self._path_hint: str = path_hint
        self._name_cache: str = ""
        self._name_cache_path: str = ""
        self._name_cache_stamp: tuple[int, int] | None = None

    # -- internal helpers --------------------------------------------------

    def _resolve_current_path(self) -> str:
        if self._guid:
            db = _get_prefab_asset_database()
            if db is not None:
                try:
                    resolved = db.get_path_from_guid(self._guid) or ""
                except Exception:
                    resolved = ""
                if resolved:
                    self._path_hint = resolved
        return self._path_hint

    @staticmethod
    def _get_file_stamp(file_path: str) -> tuple[int, int] | None:
        try:
            stat = os.stat(file_path)
            return (stat.st_mtime_ns, stat.st_size)
        except OSError:
            # Missing file is the expected signal for "no cache stamp" —
            # callers treat None as a re-read trigger.
            return None

    def _read_prefab_root_name(self, file_path: str) -> str:
        stamp = self._get_file_stamp(file_path)
        if (
            stamp is not None
            and self._name_cache
            and self._name_cache_path == file_path
            and self._name_cache_stamp == stamp
        ):
            return self._name_cache

        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            root_object = payload.get("root_object") if isinstance(payload, dict) else None
            root_name = root_object.get("name", "") if isinstance(root_object, dict) else ""
        except Exception:
            root_name = ""

        if root_name:
            self._name_cache = root_name
            self._name_cache_path = file_path
            self._name_cache_stamp = stamp

        return root_name

    # -- public properties -------------------------------------------------

    @property
    def guid(self) -> str:
        return self._guid

    @property
    def path_hint(self) -> str:
        return self._resolve_current_path()

    @property
    def persistent_id(self) -> int:
        """Always 0 — a prefab has no scene-persistent ID."""
        return 0

    @property
    def game_object(self):
        """Unity-style self alias for prefab asset references."""
        if not self:
            return None
        return self

    @property
    def name(self) -> str:
        """Human-readable display name derived from the prefab root object."""
        file_path = self._resolve_current_path()
        if file_path:
            root_name = self._read_prefab_root_name(file_path)
            if root_name:
                return root_name
            return os.path.splitext(os.path.basename(file_path))[0]
        return self._guid[:8] if self._guid else "None"

    # -- resolution --------------------------------------------------------

    def resolve(self):
        """A prefab is not a live scene object — always returns ``None``.

        Use :meth:`instantiate` to create a scene instance.
        """
        return None

    def instantiate(self, *args, **kwargs):
        """Create a new GameObject from this prefab using Unity-style instantiate overloads."""
        try:
            from Infernux.lib import GameObject
            return GameObject.instantiate(self, *args, **kwargs)
        except Exception as exc:
            _log.warning("PrefabRef.instantiate failed: %s", exc)
            return None

    # -- serialization -----------------------------------------------------

    def _serialize(self) -> dict:
        from .value_document import make_asset_ref
        return make_asset_ref("Prefab", self._guid, self._path_hint)

    @classmethod
    def _from_dict(cls, guid: str, path_hint: str = "") -> "PrefabRef":
        return cls(guid=guid, path_hint=path_hint)

    # -- dunder helpers ----------------------------------------------------

    def __bool__(self) -> bool:
        return bool(self._guid or self._path_hint)

    def __copy__(self):
        return type(self)(guid=self._guid, path_hint=self._path_hint)

    def __deepcopy__(self, memo):
        copied = type(self)(guid=self._guid, path_hint=self._path_hint)
        memo[id(self)] = copied
        return copied

    def __eq__(self, other):
        if other is None:
            return not self.__bool__()
        if isinstance(other, PrefabRef):
            return self._guid == other._guid and self._path_hint == other._path_hint
        return NotImplemented

    def __hash__(self):
        return hash((self._guid, self._path_hint))

    def __repr__(self):
        return f"PrefabRef(guid='{self._guid}', path='{self.path_hint}')"


# ============================================================================
# ComponentRef — Null-safe, persistent-ID based component reference
# ============================================================================


def _iter_live_components_on_game_object(game_object) -> list[Any]:
    """Return live Python-side component objects attached to *game_object*."""
    if game_object is None:
        return []

    try:
        go_id = int(game_object.id)
    except Exception:
        return []

    try:
        from .component import InxComponent
        candidates = list(InxComponent._active_instances.get(go_id, []))
    except Exception:
        return []

    result: list[Any] = []
    for comp in candidates:
        if comp is None or getattr(comp, "_is_destroyed", False):
            continue
        try:
            comp_go = getattr(comp, "game_object", None)
            if comp_go is None or int(comp_go.id) != go_id:
                continue
        except Exception as exc:
            Debug.log_suppressed("ref_wrappers.collect_components.iter", exc)
            continue
        if comp not in result:
            result.append(comp)
    return result


def _resolve_component_on_game_object(game_object, component_type: str = ""):
    """Resolve a component on *game_object* by type name using internal rules."""
    if game_object is None:
        return None

    live_components = _iter_live_components_on_game_object(game_object)

    if component_type:
        for comp in live_components:
            if comp.__class__.__name__ == component_type or getattr(comp, "type_name", "") == component_type:
                return comp

        try:
            from .builtin_component import BuiltinComponent
            builtin_cls = BuiltinComponent._builtin_registry.get(component_type)
            if builtin_cls is not None:
                cpp_type = getattr(builtin_cls, "_cpp_type_name", component_type)
                cpp_comp = game_object.get_cpp_component(cpp_type)
                if cpp_comp is not None:
                    return builtin_cls._get_or_create_wrapper(cpp_comp, game_object)
        except Exception as exc:
            Debug.log_suppressed("ref_wrappers.resolve_component.builtin_wrapper", exc)

        try:
            from .registry import get_type
            component_cls = get_type(component_type)
            if component_cls is not None:
                py_comp = game_object.get_py_component(component_cls)
                if py_comp is not None:
                    return py_comp
        except Exception as exc:
            Debug.log_suppressed("ref_wrappers.resolve_component.py_component_lookup", exc)

        try:
            return game_object.get_cpp_component(component_type)
        except Exception as exc:
            Debug.log_suppressed("ref_wrappers.resolve_component.get_cpp_component", exc)
            return None

    if live_components:
        return live_components[0]

    try:
        for comp in game_object.get_components() or []:
            type_name = getattr(comp, "type_name", "")
            if not type_name or type_name == "Transform" or _is_python_component_entry(comp):
                continue
            resolved = _resolve_component_on_game_object(game_object, type_name)
            if resolved is not None:
                return resolved
    except Exception as exc:
        Debug.log_suppressed("ref_wrappers.resolve_runtime_field_value", exc)

    return None


def _infer_component_type_on_game_object(game_object) -> str:
    """Infer a default component type name for a generic ComponentRef."""
    resolved = _resolve_component_on_game_object(game_object, "")
    if resolved is None:
        return ""
    return getattr(resolved, "type_name", type(resolved).__name__) or ""


def _get_component_handle(component):
    """Return the native ObjectHandle behind a component wrapper, if any."""
    if component is None:
        return None
    try:
        handle = getattr(component, "handle", None)
        if handle is not None:
            return handle
    except (RuntimeError, AttributeError):
        return None

    get_native = getattr(component, "_get_bound_native_component", None)
    if callable(get_native):
        try:
            native = get_native()
            return getattr(native, "handle", None) if native is not None else None
        except (RuntimeError, AttributeError):
            return None
    return None

class ComponentRef:
    """Null-safe reference to a component on a specific GameObject.

    Stores the target GameObject's persistent ID and the component type
    name.  Lazily resolves the live component instance at access time.

    Usage::

        class Follower(InxComponent):
            target = component_field(component_type="PlayerController")

            def update(self, dt):
                ctrl = self.target.resolve()
                if ctrl:
                    ctrl.do_something()

    Serialization uses the current typed value-document schema.
    """

    __slots__ = ("_go_id", "_component_type", "_cached", "_cached_handle")

    def __init__(self, *, go_id: int = 0, component_type: str = ""):
        self._go_id: int = int(go_id)
        self._component_type: str = component_type
        self._cached = None
        self._cached_handle = None

    # -- resolution --------------------------------------------------------

    def _resolve(self):
        """Try to resolve the component from active or persistent runtime scenes."""
        if self._go_id == 0:
            self._cached = None
            self._cached_handle = None
            return None

        try:
            for scene in _iter_reference_scenes():
                go = scene.find_by_id(self._go_id)
                if go is None:
                    continue

                found = _resolve_component_on_game_object(go, self._component_type)
                if found is not None:
                    self._cached = found
                    self._cached_handle = _get_component_handle(found)
                    return found

            self._cached = None
            self._cached_handle = None
            return None
        except (ImportError, RuntimeError) as exc:
            _log.warning("ComponentRef._resolve failed: %s", exc)
            self._cached = None
            self._cached_handle = None
            return None

    def resolve(self):
        """Return the live component instance, or ``None`` if unavailable."""
        # Quick validity check on cached value
        if self._cached is not None:
            try:
                if self._cached_handle is not None:
                    if not any(
                        scene.resolve_component(self._cached_handle) is not None
                        for scene in _iter_reference_scenes()
                    ):
                        self._cached = None
                        self._cached_handle = None
                    else:
                        return self._cached
                elif hasattr(self._cached, '_is_destroyed') and self._cached._is_destroyed:
                    self._cached = None
                else:
                    return self._cached
            except (ImportError, RuntimeError, AttributeError):
                self._cached = None
                self._cached_handle = None
        return self._resolve()

    def __copy__(self):
        return type(self)(go_id=self._go_id, component_type=self._component_type)

    def __deepcopy__(self, memo):
        copied = type(self)(go_id=self._go_id, component_type=self._component_type)
        memo[id(self)] = copied
        return copied

    # -- public properties -------------------------------------------------

    @property
    def go_id(self) -> int:
        return self._go_id

    @property
    def component_type(self) -> str:
        return self._component_type

    @property
    def display_name(self) -> str:
        """Human-readable label for Inspector display."""
        comp = self.resolve()
        if comp is None:
            return "None"
        # Try to get the GO name
        go_name = ""
        try_go = getattr(comp, "_try_get_game_object", None)
        go = try_go() if callable(try_go) else None
        if go is not None:
            go_name = getattr(go, "name", "")
        elif hasattr(comp, 'name'):
            go_name = comp.name or ""
        type_name = self._component_type or type(comp).__name__
        if go_name:
            return f"{type_name} ({go_name})"
        return type_name

    # -- serialization -----------------------------------------------------

    def _serialize(self) -> dict:
        from .value_document import make_component_ref
        return make_component_ref(self._go_id, self._component_type)

    @classmethod
    def _from_dict(cls, data: dict) -> "ComponentRef":
        return cls(
            go_id=data["game_object_id"],
            component_type=data["component_type"],
        )

    # -- dunder helpers ----------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        """Forward attribute access to the underlying component.

        This makes ``.resolve()`` unnecessary for most use cases::

            # Before: ctrl = self.target.resolve(); ctrl.do_something()
            # After:  self.target.do_something()
        """
        if name.startswith("_"):
            raise AttributeError(name)
        comp = self.resolve()
        if comp is None:
            return None
        return getattr(comp, name)

    def __bool__(self) -> bool:
        return self.resolve() is not None

    def __eq__(self, other):
        if other is None:
            return self._go_id == 0
        if isinstance(other, ComponentRef):
            return (self._go_id == other._go_id
                    and self._component_type == other._component_type)
        return NotImplemented

    def __hash__(self):
        return hash((self._go_id, self._component_type))

    def __repr__(self):
        comp = self.resolve()
        if comp is not None:
            return f"ComponentRef({self._component_type}, go_id={self._go_id})"
        return f"ComponentRef(None, type={self._component_type}, go_id={self._go_id})"
