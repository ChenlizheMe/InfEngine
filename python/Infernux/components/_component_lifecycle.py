"""ComponentLifecycleMixin — extracted from InxComponent."""
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

from Infernux.lib import GameObject


class ComponentLifecycleMixin:
    """ComponentLifecycleMixin method group for InxComponent."""

    def _safe_lifecycle_call(self, method_name: str, *args) -> bool:
        """Call *method_name* on self, catching and logging any exception."""
        try:
            getattr(self, method_name)(*args)
            return True
        except Exception as exc:
            # Route to DebugConsole so the Console Panel shows the error.
            try:
                from Infernux.debug import debug
                debug.log_exception(exc, context=self)
            except (ImportError, RuntimeError):
                # Absolute fallback if debug itself cannot be imported.
                import traceback
                traceback.print_exc()
            return False

    def _disable_after_awake_exception(self):
        """Unity disables a script component if its Awake throws."""
        self._enabled = False

        cpp_component = getattr(self, '_cpp_component', None)
        if cpp_component is None:
            return

        try:
            cpp_component.enabled = False
        except RuntimeError:
            self._invalidate_native_binding()

    def _call_awake(self):
        """Internal: Trigger awake lifecycle."""
        if self._awake_called:
            return
        self._awake_called = True
        if not self._safe_lifecycle_call("awake"):
            self._disable_after_awake_exception()

    def _call_start(self):
        """Internal: Trigger start lifecycle if not already called."""
        if self._has_started:
            return
        self._has_started = True
        self._safe_lifecycle_call("start")

    def _call_update(self, delta_time: float):
        """Internal: Trigger update lifecycle."""
        if not self.enabled:
            return
        self._safe_lifecycle_call("update", delta_time)
        # Tick coroutines after user update (matching Unity order)
        self._tick_coroutines_update(delta_time)

    def _call_fixed_update(self, fixed_delta_time: float):
        """Internal: Trigger fixed_update lifecycle."""
        if not self.enabled:
            return
        self._safe_lifecycle_call("fixed_update", fixed_delta_time)
        # Tick coroutines waiting for fixed_update
        self._tick_coroutines_fixed_update(fixed_delta_time)

    def _call_late_update(self, delta_time: float):
        """Internal: Trigger late_update lifecycle."""
        if not self.enabled:
            return
        self._safe_lifecycle_call("late_update", delta_time)
        # Tick coroutines waiting for end-of-frame
        self._tick_coroutines_late_update(delta_time)

    def _call_on_destroy(self):
        """Internal: Trigger on_destroy lifecycle."""
        if self._is_destroyed:
            return  # Already destroyed, don't call again
        self._is_destroyed = True
        self._enabled = False
        # Stop all coroutines before on_destroy callback
        if self._coroutine_scheduler is not None:
            self._coroutine_scheduler.stop_all()
            self._coroutine_scheduler = None
        # Remove from active-instances registry (safety net; _set_game_object(None)
        # should have done this already, but guard against missed calls)
        self._remove_from_active_registry()
        if self._awake_called:
            self._safe_lifecycle_call("on_destroy")
        # Clear references to help garbage collection
        self._cpp_component = None
        self._game_object = None
        self._game_object_ref = None
        self._release_component_data_slot()

    def _release_component_data_slot(self):
        """Relinquish the numeric-field slot exactly once."""
        cds_slot = getattr(self, '_cds_slot', None)
        cds_class_id = getattr(self, '_cds_class_id', None)
        self._cds_slot = None
        self._cds_class_id = None
        if cds_slot is None or cds_class_id is None:
            return
        from ._cds_bridge import release_slot as _cds_free
        _cds_free(self.__class__, cds_slot, cds_class_id)

    def _call_on_enable(self):
        """Internal: Trigger on_enable lifecycle."""
        self._enabled = True
        self._safe_lifecycle_call("on_enable")

    def _call_on_disable(self):
        """Internal: Trigger on_disable lifecycle."""
        self._enabled = False
        self._safe_lifecycle_call("on_disable")

    def _call_on_validate(self):
        """Internal: Trigger on_validate lifecycle (editor only)."""
        self._safe_lifecycle_call("on_validate")

    def _call_reset(self):
        """Internal: Trigger reset lifecycle (editor only)."""
        self._safe_lifecycle_call("reset")

    def _call_on_after_deserialize(self):
        """Trigger the transactional post-deserialize hook and propagate failure."""
        self.on_after_deserialize()

    def _call_on_before_serialize(self):
        """Internal: Trigger on_before_serialize lifecycle."""
        self._safe_lifecycle_call("on_before_serialize")

