"""ComponentCoroutineMixin — extracted from InxComponent."""
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


class ComponentCoroutineMixin:
    """ComponentCoroutineMixin method group for InxComponent."""

    def _sync_coroutine_scheduler_state(self, active=None) -> None:
        """Publish active coroutine work to the shared lifecycle scheduler."""
        scheduler = self._coroutine_scheduler
        if active is None:
            active = scheduler is not None and scheduler.count > 0
        runtime_scheduler = scheduler if bool(active) else None
        if self.__dict__.get("_runtime_coroutine_scheduler") is runtime_scheduler:
            return
        self.__dict__["_runtime_coroutine_scheduler"] = runtime_scheduler
        from ._component_lifecycle import RuntimeExecutionScheduler

        RuntimeExecutionScheduler._notify_component_runtime_work(self)

    def start_coroutine(self, generator) -> 'Coroutine':
        """Start a coroutine on this component.

        Args:
            generator: A generator object (call your generator function first).

        Returns:
            A :class:`~Infernux.coroutine.Coroutine` handle that can be passed
            to :meth:`stop_coroutine` or ``yield``-ed from another coroutine to
            wait for completion.

        Example::

            from Infernux.coroutine import WaitForSeconds

            class Enemy(InxComponent):
                def start(self):
                    self.start_coroutine(self.patrol())

                def patrol(self):
                    while True:
                        debug.log("Moving left")
                        yield WaitForSeconds(2)
                        debug.log("Moving right")
                        yield WaitForSeconds(2)
        """
        from Infernux.coroutine import CoroutineScheduler
        from Infernux.engine.runtime_dispatch import current_runtime_epoch

        creation_epoch = current_runtime_epoch()
        if self._coroutine_scheduler is None:
            self._coroutine_scheduler = CoroutineScheduler(
                on_active_changed=self._sync_coroutine_scheduler_state,
                creation_epoch=creation_epoch,
            )
        coroutine = self._coroutine_scheduler.start(
            generator,
            owner=self,
            epoch=creation_epoch,
        )
        return coroutine

    def stop_coroutine(self, coroutine) -> None:
        """Stop a specific coroutine previously started with :meth:`start_coroutine`.

        Args:
            coroutine: The :class:`~Infernux.coroutine.Coroutine` handle.
        """
        if self._coroutine_scheduler is not None:
            self._coroutine_scheduler.stop(coroutine)
            self._sync_coroutine_scheduler_state()

    def stop_all_coroutines(self) -> None:
        """Stop **all** coroutines running on this component."""
        if self._coroutine_scheduler is not None:
            self._coroutine_scheduler.stop_all()
            self._sync_coroutine_scheduler_state()

    def _tick_coroutines_update(self, delta_time: float):
        """Advance coroutine work scheduled for the Update phase."""
        scheduler = self.__dict__.get("_coroutine_scheduler")
        if scheduler is not None:
            scheduler.tick_update(delta_time)

    def _tick_coroutines_fixed_update(self, fixed_delta_time: float):
        """Advance coroutine work scheduled for the fixed-update phase."""
        scheduler = self.__dict__.get("_coroutine_scheduler")
        if scheduler is not None:
            scheduler.tick_fixed_update(fixed_delta_time)

    def _tick_coroutines_late_update(self, delta_time: float):
        """Advance coroutine work scheduled for the late-update phase."""
        scheduler = self.__dict__.get("_coroutine_scheduler")
        if scheduler is not None:
            scheduler.tick_late_update(delta_time)

    def _stop_coroutines_for_game_object_deactivate(self):
        """Unity stops all coroutines when the owning GameObject is deactivated."""
        if self._coroutine_scheduler is not None:
            self._coroutine_scheduler.stop_all()
            self._sync_coroutine_scheduler_state()
            self._coroutine_scheduler = None

    @classmethod
    def _clear_all_instances(cls) -> None:
        """Clear the active-instances registry (call on scene unload/reload)."""
        cls._active_instances.clear()

