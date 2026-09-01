"""ComponentNativeMixin — extracted from InxComponent."""
from __future__ import annotations

"""
InxComponent - Base class for all Python-defined components.

Provides Unity-style lifecycle methods and property injection.
Users inherit from this class to create custom game logic.

Example:
    from Infernux.components import InxComponent, serialized_field
    
    class PlayerController(InxComponent):
        speed: float = serialized_field(default=5.0)
        
        def start(self):
            print("Player started!")
        
        def update(self, delta_time: float):
            pos = self.transform.position
            self.transform.position = Vector3(pos.x + self.speed * delta_time, pos.y, pos.z)
"""

from typing import Optional, Dict, Any, Type, TYPE_CHECKING, List
import copy
import threading
import weakref

from Infernux.debug import Debug
from Infernux.lib import GameObject


class ComponentNativeMixin:
    """ComponentNativeMixin method group for InxComponent."""

    @staticmethod
    def _is_native_game_object_alive(game_object: Optional['GameObject']) -> bool:
        """Return True when a native GameObject wrapper still points to live data."""
        if game_object is None:
            return False
        try:
            return isinstance(game_object, GameObject) and int(game_object.id) > 0
        except Exception as exc:
            Debug.log_suppressed("_component_native.is_valid_game_object", exc)
            return False

    @staticmethod
    def _is_native_component_alive(component: Any) -> bool:
        """Return True when a native Component wrapper still points to live data."""
        if component is None:
            return False
        try:
            return int(component.component_id) > 0
        except Exception as exc:
            Debug.log_suppressed("_component_native.is_valid_native_component", exc)
            return False

    def _try_get_game_object(self) -> Optional['GameObject']:
        """Return the owning GameObject, or None when the component is unbound."""
        if self._is_destroyed:
            return None
        cpp_component = self._get_bound_native_component()
        if cpp_component is not None:
            try:
                go = cpp_component.game_object
            except RuntimeError:
                self._invalidate_native_binding()
                return None
            if go is None:
                self._invalidate_native_binding()
                return None
            if not self._is_native_game_object_alive(go):
                self._invalidate_native_binding()
                return None
            self._game_object = go
            self._game_object_ref = weakref.ref(go)
            return go
        # Use weak reference if available for safety
        if self._game_object_ref is not None:
            go = self._game_object_ref()
            if go is None:
                # GameObject was destroyed, mark component as invalid
                self._game_object = None
                return None
            if not self._is_native_game_object_alive(go):
                self._invalidate_native_binding()
                return None
            return go
        if self._game_object is not None and not self._is_native_game_object_alive(self._game_object):
            self._invalidate_native_binding()
            return None
        return self._game_object

    def _try_get_transform(self) -> Optional['Transform']:
        """Return the attached Transform, or None when the component is invalid."""
        go = self._try_get_game_object()
        if go is None:
            return None
        try:
            return go.get_transform()
        except RuntimeError:
            self._invalidate_native_binding()
            return None

    def _set_game_object(self, game_object: Optional['GameObject']):
        """Internal: Set the owning GameObject. Called by the engine."""
        # ---- Update active-instances registry ----
        self._remove_from_active_registry()

        self._game_object = game_object
        # Also store weak reference for safe access
        if game_object is not None:
            self._game_object_ref = weakref.ref(game_object)
            if self._registers_active_instance:
                go_id = game_object.id
                _registry = type(self)._active_instances
                lst = _registry.get(go_id)
                if lst is None:
                    _registry[go_id] = [self]
                elif self not in lst:
                    lst.append(self)
                self._registered_go_id = go_id
                from ._component_lifecycle import notify_runtime_component_added
                notify_runtime_component_added(self)
        else:
            self._game_object_ref = None

    def _remove_from_active_registry(self):
        """Remove this component from the active-instance registry."""
        old_id = getattr(self, '_registered_go_id', None)
        if old_id is None:
            return

        _registry = type(self)._active_instances
        lst = _registry.get(old_id)
        if lst is not None:
            if self in lst:
                lst.remove(self)
            if not lst:
                _registry.pop(old_id, None)
        self._registered_go_id = None
        from ._component_lifecycle import notify_runtime_component_removed
        notify_runtime_component_removed(self)

    def _bind_native_component(self, cpp_component, game_object=None):
        """Bind this Python instance to its native lifecycle authority."""
        self._cpp_component = cpp_component
        self._native_handle = getattr(cpp_component, "handle", None) if cpp_component is not None else None
        self._native_scene = getattr(game_object, "scene", None) if game_object is not None else None
        self._capture_bound_structure_version()
        self._native_game_object_handle = getattr(game_object, "handle", None) if game_object is not None else None
        self._native_generation += 1
        if cpp_component is not None:
            self._is_destroyed = False
        if game_object is not None:
            self._set_game_object(game_object)

        if cpp_component is not None:
            try:
                self._component_id = int(cpp_component.component_id)
            except Exception as exc:
                from Infernux.debug import Debug
                Debug.log_warning(f"[InxComponent] Failed to read component_id during bind: {exc}")
            try:
                self._execution_order = int(cpp_component.execution_order)
            except Exception as exc:
                from Infernux.debug import Debug
                Debug.log_warning(f"[InxComponent] Failed to read execution_order during bind: {exc}")
            try:
                self._enabled = bool(cpp_component.enabled)
            except Exception as exc:
                from Infernux.debug import Debug
                Debug.log_warning(f"[InxComponent] Failed to read enabled during bind: {exc}")
            self._sync_coroutine_scheduler_state()

    def _sync_native_state(
        self,
        enabled: bool,
        awake_called: bool,
        has_started: bool,
        is_destroyed: bool,
        execution_order: int,
    ):
        """Mirror native lifecycle state into Python for diagnostics and tooling."""
        self._enabled = bool(enabled)
        self._awake_called = bool(awake_called)
        self._has_started = bool(has_started)
        self._is_destroyed = bool(is_destroyed)
        self._execution_order = int(execution_order)

    def _refresh_native_handle(self):
        """Refresh identity after an explicit native component-ID re-key."""
        cpp_component = getattr(self, '_cpp_component', None)
        if cpp_component is None:
            self._native_handle = None
            return
        self._native_handle = cpp_component.handle

    def _invalidate_native_binding(self):
        """Invalidate native references after scene rebuild/destruction."""
        self._release_component_data_slot()
        self._cpp_component = None
        self._native_handle = None
        self._native_scene = None
        self._bound_structure_version = None
        self._native_game_object_handle = None
        self._enabled = False
        self._awake_called = False
        self._has_started = False
        self._is_destroyed = True
        self._remove_from_active_registry()
        self._game_object = None
        self._game_object_ref = None

    def _detach_native_binding_for_replacement(self):
        """Detach for script reload without invoking the user's on_destroy hook."""
        scheduler = getattr(self, '_coroutine_scheduler', None)
        if scheduler is not None:
            scheduler.stop_all()
            self._sync_coroutine_scheduler_state()
            self._coroutine_scheduler = None
        self._invalidate_native_binding()

    def _tracks_scene_structure_binding(self) -> bool:
        """Inspector builtin wrappers may need a Play-rebuild rebind.

        ``Scene.structure_version`` also advances for ordinary mutations
        (activate, instantiate, reparent).  That is an Inspector hint, not
        proof that a cached Rigidbody/Light wrapper is dead.
        """
        return bool(getattr(self, "_is_builtin_component_wrapper", False))

    def _capture_bound_structure_version(self) -> None:
        if not self._tracks_scene_structure_binding():
            self._bound_structure_version = None
            return
        try:
            scene = getattr(self, "_native_scene", None)
            self._bound_structure_version = (
                int(getattr(scene, "structure_version")) if scene is not None else None
            )
        except (TypeError, ValueError, AttributeError, RuntimeError):
            self._bound_structure_version = None

    def _native_binding_scene_revision(self):
        scene = getattr(self, "_native_scene", None)
        if scene is None:
            return None
        try:
            return int(getattr(scene, "structure_version"))
        except (TypeError, ValueError, AttributeError, RuntimeError):
            return None

    def _is_native_binding_stale(self) -> bool:
        """Return True when this wrapper outlived a scene-graph replacement.

        Play Mode rebuilds preserve authored component IDs.  The wrapper cache
        therefore cannot use identity alone: a pointer that still looks live
        may belong to the destroyed pre-rebuild Light/Camera/….
        """
        if bool(getattr(self, "_is_destroyed", False)):
            return True
        if getattr(self, "_cpp_component", None) is None:
            return True
        if not self._tracks_scene_structure_binding():
            return False
        bound = getattr(self, "_bound_structure_version", None)
        if bound is None:
            return False
        current = self._native_binding_scene_revision()
        if current is None:
            return False
        return int(bound) != current

    def _get_bound_native_component(self):
        """Return the native component if still alive, otherwise invalidate it."""
        cpp_component = getattr(self, '_cpp_component', None)
        if cpp_component is None:
            return None
        handle = getattr(self, '_native_handle', None)
        scene = getattr(self, '_native_scene', None)
        if handle is not None and scene is not None:
            try:
                resolved = scene.resolve_component(handle)
            except (RuntimeError, AttributeError):
                resolved = None
            if resolved is None:
                self._invalidate_native_binding()
                return None
            # Native scene resolution normally returns the private proxy. Some
            # public/facade resolution paths may already unwrap that proxy to
            # this Python component; that still proves the handle is live, but
            # must not make _cpp_component self-referential.
            if resolved is self:
                if self._tracks_scene_structure_binding():
                    self._capture_bound_structure_version()
                return cpp_component
            # A different public Python object at the same handle means this
            # wrapper belongs to the scene generation that was replaced.
            if getattr(resolved, "_get_bound_native_component", None) is not None:
                self._invalidate_native_binding()
                return None
            self._cpp_component = resolved
            if self._tracks_scene_structure_binding():
                self._capture_bound_structure_version()
            return resolved
        try:
            comp_id = int(cpp_component.component_id)
        except RuntimeError:
            self._invalidate_native_binding()
            return None
        except Exception:
            self._invalidate_native_binding()
            return None
        if comp_id <= 0:
            self._invalidate_native_binding()
            return None
        return cpp_component

