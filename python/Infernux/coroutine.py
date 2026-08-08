"""
Coroutine system — Unity-style cooperative multitasking for ``InxComponent``.

Usage (inside a component)::

    from Infernux.coroutine import WaitForSeconds, WaitUntil

    class Enemy(InxComponent):
        def start(self):
            self.start_coroutine(self.patrol())

        def patrol(self):
            while True:
                debug.log("Moving left")
                yield WaitForSeconds(2)
                debug.log("Moving right")
                yield WaitForSeconds(2)

Yield instructions
------------------

==========================  ====================================================
``yield None``              Wait one **update** frame (same as bare ``yield``).
``yield WaitForSeconds(n)`` Wait *n* seconds of **scaled** game time.
``yield WaitForSecondsRealtime(n)``  Wait *n* seconds of wall-clock time.
``yield WaitForEndOfFrame()``        Resume after all ``late_update()`` this frame.
``yield WaitForFixedUpdate()``       Resume at the next ``fixed_update()`` step.
``yield WaitUntil(pred)``            Resume when ``pred()`` returns ``True``.
``yield WaitWhile(pred)``            Resume when ``pred()`` returns ``False``.
``yield another_coroutine``          Wait until *another_coroutine* finishes.
==========================  ====================================================
"""

from __future__ import annotations

import time as _time
from typing import Any, Callable, Generator, Optional
from Infernux.debug import Debug


# ======================================================================
# Yield instructions
# ======================================================================

class WaitForSeconds:
    """Suspend the coroutine for *seconds* of **scaled** game time."""
    __slots__ = ("duration", "_elapsed")

    def __init__(self, seconds: float):
        self.duration: float = float(seconds)
        self._elapsed: float = 0.0

    def _tick(self, scaled_dt: float) -> bool:
        """Accumulate *scaled_dt*; return ``True`` when done."""
        self._elapsed += scaled_dt
        return self._elapsed >= self.duration

    def __repr__(self) -> str:
        return f"WaitForSeconds({self.duration})"


class WaitForSecondsRealtime:
    """Suspend the coroutine for *seconds* of **wall-clock** time."""
    __slots__ = ("duration", "_target_time")

    def __init__(self, seconds: float):
        self.duration: float = float(seconds)
        self._target_time: float = _time.time() + self.duration

    def _is_ready(self) -> bool:
        return _time.time() >= self._target_time

    def __repr__(self) -> str:
        return f"WaitForSecondsRealtime({self.duration})"


class WaitForEndOfFrame:
    """Suspend until after all ``late_update()`` calls this frame."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "WaitForEndOfFrame()"


class WaitForFixedUpdate:
    """Suspend until the next ``fixed_update()`` physics step."""
    __slots__ = ()

    def __repr__(self) -> str:
        return "WaitForFixedUpdate()"


class WaitUntil:
    """Suspend until *predicate()* returns ``True``."""
    __slots__ = ("predicate",)

    def __init__(self, predicate: Callable[[], bool]):
        self.predicate = predicate

    def _is_ready(self) -> bool:
        return bool(self.predicate())

    def __repr__(self) -> str:
        return f"WaitUntil({self.predicate})"


class WaitWhile:
    """Suspend **while** *predicate()* returns ``True``; resume when ``False``."""
    __slots__ = ("predicate",)

    def __init__(self, predicate: Callable[[], bool]):
        self.predicate = predicate

    def _is_ready(self) -> bool:
        return not self.predicate()

    def __repr__(self) -> str:
        return f"WaitWhile({self.predicate})"


# ======================================================================
# Coroutine handle
# ======================================================================

class Coroutine:
    """Opaque handle to a running coroutine.  Returned by ``start_coroutine()``.

    Attributes:
        is_finished (bool): ``True`` once the generator has completed or been stopped.
    """
    _next_id: int = 0
    __slots__ = ("_id", "_generator", "_owner_ref",
                 "_current_yield", "_is_finished", "_phase")

    def __init__(self, generator: Generator, owner: Any = None):
        Coroutine._next_id += 1
        self._id: int = Coroutine._next_id
        self._generator: Optional[Generator] = generator
        self._owner_ref: Any = owner          # component reference for error reporting
        self._current_yield: Any = None
        self._is_finished: bool = False
        self._phase: str = "update"           # which tick phase should process this

    @property
    def is_finished(self) -> bool:
        """``True`` when the coroutine has ended (completed or stopped)."""
        return self._is_finished

    def __repr__(self) -> str:
        status = "finished" if self._is_finished else "running"
        return f"<Coroutine #{self._id} ({status})>"


# ======================================================================
# Per-component scheduler
# ======================================================================

class CoroutineScheduler:
    """Internal scheduler — drives coroutines for a single ``InxComponent``.

    The scheduler is lazily created on the first ``start_coroutine()`` call to
    avoid overhead on components that never use coroutines.
    """
    __slots__ = ("_coroutines", "_on_active_changed")

    def __init__(self, on_active_changed=None) -> None:
        self._coroutines: list[Coroutine] = []
        self._on_active_changed = on_active_changed

    def _notify_active_changed(self, was_active: bool) -> None:
        is_active = bool(self._coroutines)
        if was_active == is_active or self._on_active_changed is None:
            return
        self._on_active_changed(is_active)

    # -- Public API (used by InxComponent) ----------------------------------

    def start(self, generator: Generator, owner: Any = None) -> Coroutine:
        """Start a new coroutine and return a handle."""
        was_active = bool(self._coroutines)
        co = Coroutine(generator, owner)
        self._advance(co)                       # run until first yield
        if not co._is_finished:
            self._coroutines.append(co)
        self._notify_active_changed(was_active)
        return co

    def stop(self, coroutine: Coroutine) -> None:
        """Immediately stop *coroutine*."""
        was_active = bool(self._coroutines)
        if coroutine._is_finished:
            return
        coroutine._is_finished = True
        if coroutine._generator is not None:
            try:
                coroutine._generator.close()
            except RuntimeError as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                pass
            coroutine._generator = None
        try:
            self._coroutines.remove(coroutine)
        except ValueError as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            pass
        self._notify_active_changed(was_active)

    def stop_all(self) -> None:
        """Stop every running coroutine."""
        was_active = bool(self._coroutines)
        for co in self._coroutines:
            co._is_finished = True
            if co._generator is not None:
                try:
                    co._generator.close()
                except RuntimeError as _exc:
                    Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                    pass
                co._generator = None
        self._coroutines.clear()
        self._notify_active_changed(was_active)

    @property
    def count(self) -> int:
        """Number of running coroutines."""
        return len(self._coroutines)

    # -- Tick entry points (called from component lifecycle) ----------------

    def tick_update(self, scaled_dt: float) -> None:
        """Process coroutines waiting in the **update** phase."""
        self._tick("update", scaled_dt)

    def tick_fixed_update(self, fixed_dt: float) -> None:
        """Process coroutines waiting in the **fixed_update** phase."""
        self._tick("fixed_update", fixed_dt)

    def tick_late_update(self, scaled_dt: float) -> None:
        """Process coroutines waiting in the **late_update** phase."""
        self._tick("late_update", scaled_dt)

    # -- Internal -----------------------------------------------------------

    def _tick(self, phase: str, dt: float) -> None:
        if not self._coroutines:
            return

        was_active = True
        to_remove: list[Coroutine] = []

        # Iterate a snapshot so that coroutines started from within user code
        # during _advance don't affect the current tick.
        for co in list(self._coroutines):
            if co._is_finished:
                to_remove.append(co)
                continue
            if co._phase != phase:
                continue

            should_advance = False
            current = co._current_yield

            if current is None:
                # ``yield None`` / bare ``yield`` → wait one frame
                should_advance = True
            elif isinstance(current, WaitForSeconds):
                should_advance = current._tick(dt)
            elif isinstance(current, WaitForSecondsRealtime):
                should_advance = current._is_ready()
            elif isinstance(current, WaitForEndOfFrame):
                # Already in the correct phase (late_update)
                should_advance = True
            elif isinstance(current, WaitForFixedUpdate):
                # Already in the correct phase (fixed_update)
                should_advance = True
            elif isinstance(current, WaitUntil):
                should_advance = current._is_ready()
            elif isinstance(current, WaitWhile):
                should_advance = current._is_ready()
            elif isinstance(current, Coroutine):
                # Nested/chained coroutine — wait for it to finish
                should_advance = current._is_finished
            else:
                # Unknown yield value → treat as ``yield None`` (wait one frame)
                should_advance = True

            if should_advance:
                self._advance(co)
                if co._is_finished:
                    to_remove.append(co)

        for co in to_remove:
            try:
                self._coroutines.remove(co)
            except ValueError as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                pass
        self._notify_active_changed(was_active)

    def _advance(self, co: Coroutine) -> None:
        """Call ``next()`` on the generator and update the coroutine state."""
        if co._generator is None:
            co._is_finished = True
            return
        try:
            value = next(co._generator)
        except StopIteration:
            co._is_finished = True
            co._generator = None
            return
        except Exception as exc:
            co._is_finished = True
            co._generator = None
            # Route exception to the Console so it shows in the editor
            try:
                from Infernux.debug import debug
                debug.log_exception(exc, context=co._owner_ref)
            except ImportError:
                import traceback
                traceback.print_exc()
            return

        co._current_yield = value

        # Determine which tick phase should next process this coroutine
        if isinstance(value, WaitForEndOfFrame):
            co._phase = "late_update"
        elif isinstance(value, WaitForFixedUpdate):
            co._phase = "fixed_update"
        else:
            co._phase = "update"
