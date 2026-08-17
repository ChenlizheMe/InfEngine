"""ComponentPhysicsMixin — extracted from InxComponent."""
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
from Infernux.engine.runtime_dispatch import current_runtime_epoch


class ComponentPhysicsMixin:
    """ComponentPhysicsMixin method group for InxComponent."""

    def _dispatch_runtime_event(self, method_name: str, *args) -> None:
        """Dispatch one native/editor event against one published epoch."""
        if not self.enabled:
            return
        self._safe_lifecycle_call(
            method_name,
            *args,
            epoch=current_runtime_epoch(),
        )

    def _call_on_collision_enter(self, collision):
        """Internal: Trigger on_collision_enter lifecycle."""
        self._dispatch_runtime_event("on_collision_enter", collision)

    def _call_on_collision_stay(self, collision):
        """Internal: Trigger on_collision_stay lifecycle."""
        self._dispatch_runtime_event("on_collision_stay", collision)

    def _call_on_collision_exit(self, collision):
        """Internal: Trigger on_collision_exit lifecycle."""
        self._dispatch_runtime_event("on_collision_exit", collision)

    def _call_on_trigger_enter(self, other):
        """Internal: Trigger on_trigger_enter lifecycle."""
        self._dispatch_runtime_event("on_trigger_enter", other)

    def _call_on_trigger_stay(self, other):
        """Internal: Trigger on_trigger_stay lifecycle."""
        self._dispatch_runtime_event("on_trigger_stay", other)

    def _call_on_trigger_exit(self, other):
        """Internal: Trigger on_trigger_exit lifecycle."""
        self._dispatch_runtime_event("on_trigger_exit", other)

    def _call_on_draw_gizmos(self):
        """Internal: Trigger on_draw_gizmos lifecycle (editor only)."""
        self._dispatch_runtime_event("on_draw_gizmos")

    def _call_on_draw_gizmos_selected(self):
        """Internal: Trigger on_draw_gizmos_selected lifecycle (editor only)."""
        self._dispatch_runtime_event("on_draw_gizmos_selected")

