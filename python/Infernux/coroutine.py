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
``yield WaitForEndOfFrame(n)``       Resume after *n* frame-end phases (default 1).
``yield WaitForFrames(n)``           Resume after *n* ``update`` frames.
``yield WaitForFixedUpdate()``       Resume at the next ``fixed_update()`` step.
``yield WaitUntil(pred)``            Resume when ``pred()`` returns ``True``.
``yield WaitWhile(pred)``            Resume when ``pred()`` returns ``False``.
``yield another_coroutine``          Wait until *another_coroutine* finishes.
==========================  ====================================================
"""

from __future__ import annotations

import time as _time
import weakref
from typing import Any, Callable, Generator, Optional


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
    """Suspend until one or more frame-end phases have completed."""
    __slots__ = ("frames", "_remaining")

    def __init__(self, frames: int = 1):
        if isinstance(frames, bool) or not isinstance(frames, int):
            raise TypeError("frames must be an integer")
        if frames < 1:
            raise ValueError("frames must be at least 1")
        self.frames = frames
        self._remaining = frames

    def _tick(self) -> bool:
        self._remaining -= 1
        return self._remaining <= 0

    def __repr__(self) -> str:
        return f"WaitForEndOfFrame({self.frames})"


class WaitForFrames:
    """Suspend for an exact number of ``update`` frames."""
    __slots__ = ("frames", "_remaining")

    def __init__(self, frames: int = 1):
        if isinstance(frames, bool) or not isinstance(frames, int):
            raise TypeError("frames must be an integer")
        if frames < 1:
            raise ValueError("frames must be at least 1")
        self.frames = frames
        self._remaining = frames

    def _tick(self) -> bool:
        self._remaining -= 1
        return self._remaining <= 0

    def __repr__(self) -> str:
        return f"WaitForFrames({self.frames})"


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

def _capture_runtime_epoch() -> Any:
    """Read the owner-published epoch at coroutine creation time."""
    try:
        from Infernux.engine.runtime_dispatch import current_runtime_epoch

        return current_runtime_epoch()
    except (ImportError, AttributeError):
        return None

class Coroutine:
    """Opaque handle to a running coroutine.  Returned by ``start_coroutine()``.

    Attributes:
        is_finished (bool): ``True`` once the generator has completed or been stopped.
    """
    _next_id: int = 0
    __slots__ = (
        "_id", "_generator", "_owner_ref", "_current_yield", "_is_finished",
        "_phase", "_creation_epoch", "_creation_epoch_id", "_is_stale_epoch",
    )

    def __init__(
        self,
        generator: Generator,
        owner: Any = None,
        *,
        creation_epoch: Any = None,
    ):
        Coroutine._next_id += 1
        self._id: int = Coroutine._next_id
        self._generator: Optional[Generator] = generator
        self._owner_ref: Any = owner          # component reference for error reporting
        self._current_yield: Any = None
        self._is_finished: bool = False
        self._phase: str = "update"           # which tick phase should process this
        self._creation_epoch: Any = (
            _capture_runtime_epoch() if creation_epoch is None else creation_epoch
        )
        self._creation_epoch_id: int = int(
            getattr(self._creation_epoch, "epoch_id", 0)
        )
        self._is_stale_epoch: bool = False

    @property
    def is_finished(self) -> bool:
        """``True`` when the coroutine has ended (completed or stopped)."""
        return self._is_finished

    @property
    def creation_epoch(self) -> Any:
        """Immutable dispatch epoch captured when this coroutine was started."""
        return self._creation_epoch

    @property
    def creation_epoch_id(self) -> int:
        return self._creation_epoch_id

    @property
    def is_stale_epoch(self) -> bool:
        """Whether this coroutine was created under a different runtime epoch."""
        return self._is_stale_epoch

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
    _live_schedulers: "weakref.WeakSet[CoroutineScheduler]" = weakref.WeakSet()
    __slots__ = (
        "_coroutines", "_on_active_changed", "_creation_epoch",
        "_creation_epoch_id", "_observed_epoch_id", "_stale_epoch_count", "__weakref__",
    )

    def __init__(self, on_active_changed=None, *, creation_epoch: Any = None) -> None:
        self._coroutines: list[Coroutine] = []
        self._on_active_changed = on_active_changed
        self._creation_epoch = (
            _capture_runtime_epoch() if creation_epoch is None else creation_epoch
        )
        self._creation_epoch_id = int(getattr(self._creation_epoch, "epoch_id", 0))
        self._observed_epoch_id = self._creation_epoch_id
        self._stale_epoch_count = 0
        self._live_schedulers.add(self)

    @classmethod
    def _notify_runtime_epoch_published(cls, epoch: Any) -> None:
        """Refresh epoch diagnostics at an owner safe point, not per frame."""
        for scheduler in tuple(cls._live_schedulers):
            scheduler._observe_epoch(epoch)

    def _observe_epoch(self, epoch: Any) -> None:
        epoch_id = int(getattr(epoch, "epoch_id", self._observed_epoch_id))
        if epoch_id == self._observed_epoch_id:
            return
        self._observed_epoch_id = epoch_id
        self._stale_epoch_count = 0
        for coroutine in self._coroutines:
            coroutine._is_stale_epoch = coroutine.creation_epoch_id != epoch_id
            self._stale_epoch_count += int(coroutine._is_stale_epoch)

    def _notify_active_changed(self, was_active: bool) -> None:
        is_active = bool(self._coroutines)
        if was_active == is_active or self._on_active_changed is None:
            return
        self._on_active_changed(is_active)

    # -- Public API (used by InxComponent) ----------------------------------

    def start(
        self,
        generator: Generator,
        owner: Any = None,
        *,
        epoch: Any = None,
    ) -> Coroutine:
        """Start a new coroutine and return a handle."""
        was_active = bool(self._coroutines)
        selected_epoch = _capture_runtime_epoch() if epoch is None else epoch
        self._observe_epoch(selected_epoch)
        co = Coroutine(generator, owner, creation_epoch=selected_epoch)
        co._is_stale_epoch = co.creation_epoch_id != self._observed_epoch_id
        self._advance(co)                       # run until first yield
        if not co._is_finished:
            self._coroutines.append(co)
            self._stale_epoch_count += int(co._is_stale_epoch)
        self._notify_active_changed(was_active)
        return co

    def stop(self, coroutine: Coroutine) -> None:
        """Immediately stop *coroutine*."""
        was_active = bool(self._coroutines)
        if coroutine._is_finished:
            return

        if coroutine in self._coroutines:
            self._coroutines.remove(coroutine)
            self._stale_epoch_count -= int(coroutine._is_stale_epoch)

        coroutine._is_finished = True
        generator = coroutine._generator
        coroutine._generator = None
        self._notify_active_changed(was_active)
        if generator is not None:
            generator.close()

    def stop_all(self) -> None:
        """Stop every running coroutine."""
        was_active = bool(self._coroutines)
        coroutines = self._coroutines
        self._coroutines = []
        generators: list[Generator] = []
        for co in coroutines:
            co._is_finished = True
            if co._generator is not None:
                generators.append(co._generator)
                co._generator = None
        self._stale_epoch_count = 0
        self._notify_active_changed(was_active)

        errors: list[Exception] = []
        for generator in generators:
            try:
                generator.close()
            except Exception as exc:
                errors.append(exc)
        if errors:
            raise ExceptionGroup("failed to close coroutines", errors)

    @property
    def count(self) -> int:
        """Number of running coroutines."""
        return len(self._coroutines)

    @property
    def creation_epoch(self) -> Any:
        return self._creation_epoch

    @property
    def creation_epoch_id(self) -> int:
        return self._creation_epoch_id

    @property
    def stale_epoch_coroutine_count(self) -> int:
        return self._stale_epoch_count

    def diagnostics(self) -> dict[str, int]:
        """Return cheap counters for runtime/editor diagnostics."""
        return {
            "active_count": len(self._coroutines),
            "stale_epoch_count": self._stale_epoch_count,
            "creation_epoch_id": self._creation_epoch_id,
            "observed_epoch_id": self._observed_epoch_id,
        }

    # -- Tick entry points (called from component lifecycle) ----------------

    def tick_update(self, scaled_dt: float, *, epoch: Any = None) -> None:
        """Process coroutines waiting in the **update** phase."""
        self._tick("update", scaled_dt, epoch=epoch)

    def tick_fixed_update(self, fixed_dt: float, *, epoch: Any = None) -> None:
        """Process coroutines waiting in the **fixed_update** phase."""
        self._tick("fixed_update", fixed_dt, epoch=epoch)

    def tick_late_update(self, scaled_dt: float, *, epoch: Any = None) -> None:
        """Process coroutines waiting in the **late_update** phase."""
        self._tick("late_update", scaled_dt, epoch=epoch)

    # -- Internal -----------------------------------------------------------

    def _tick(self, phase: str, dt: float, *, epoch: Any = None) -> None:
        if not self._coroutines:
            return
        if epoch is not None:
            self._observe_epoch(epoch)

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
                should_advance = current._tick()
            elif isinstance(current, WaitForFrames):
                should_advance = current._tick()
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
                self.stop(co)
                raise TypeError(
                    "unsupported coroutine yield value "
                    f"{type(current).__name__}; yield None, a yield instruction, "
                    "or a Coroutine handle"
                )

            if should_advance:
                self._advance(co)
                if co._is_finished:
                    to_remove.append(co)

        for co in to_remove:
            if co in self._coroutines:
                self._coroutines.remove(co)
                self._stale_epoch_count -= int(co._is_stale_epoch)
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


def notify_runtime_epoch_published(epoch: Any) -> None:
    """Notify live coroutine schedulers of a newly published runtime epoch."""
    CoroutineScheduler._notify_runtime_epoch_published(epoch)
