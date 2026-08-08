from __future__ import annotations

from typing import Any, Callable, Generator, Optional


class WaitForSeconds:
    """Suspend a coroutine for a given number of seconds."""

    duration: float
    def __init__(self, seconds: float) -> None: ...

class WaitForSecondsRealtime:
    """Suspend a coroutine for a given number of real-time seconds."""

    duration: float
    def __init__(self, seconds: float) -> None: ...

class WaitForEndOfFrame:
    """Suspend a coroutine until the end of the current frame."""
    ...

class WaitForFixedUpdate:
    """Suspend a coroutine until the next fixed update."""
    ...

class WaitUntil:
    """Suspend a coroutine until the predicate returns True."""

    predicate: Callable[[], bool]
    def __init__(self, predicate: Callable[[], bool]) -> None: ...

class WaitWhile:
    """Suspend a coroutine while the predicate returns True."""

    predicate: Callable[[], bool]
    def __init__(self, predicate: Callable[[], bool]) -> None: ...

class Coroutine:
    """A handle to a running coroutine."""

    def __init__(self, generator: Generator, owner: Any = ...) -> None: ...
    @property
    def is_finished(self) -> bool:
        """Returns True if the coroutine has completed."""
        ...

class CoroutineScheduler:
    """Manages coroutine lifecycle — start, stop, and tick."""

    def __init__(self, on_active_changed: Optional[Callable[[bool], None]] = ...) -> None: ...
    def start(self, generator: Generator, owner: Any = ...) -> Coroutine:
        """Start a new coroutine from a generator and return a handle."""
        ...
    def stop(self, coroutine: Coroutine) -> None:
        """Stop a running coroutine."""
        ...
    def stop_all(self) -> None:
        """Stop all running coroutines."""
        ...
    @property
    def count(self) -> int:
        """The number of currently running coroutines."""
        ...
    def tick_update(self, scaled_dt: float) -> None:
        """Advance coroutines waiting on WaitForSeconds."""
        ...
    def tick_fixed_update(self, fixed_dt: float) -> None:
        """Advance coroutines waiting on WaitForFixedUpdate."""
        ...
    def tick_late_update(self, scaled_dt: float) -> None:
        """Advance coroutines waiting on WaitForEndOfFrame."""
        ...
