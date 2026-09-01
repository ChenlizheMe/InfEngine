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
    """Suspend until one or more frame-end phases have completed."""

    frames: int
    def __init__(self, frames: int = ...) -> None: ...

class WaitForFrames:
    """Suspend a coroutine for an exact number of update frames."""

    frames: int
    def __init__(self, frames: int = ...) -> None: ...

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

    def __init__(
        self,
        generator: Generator,
        owner: Any = ...,
        *,
        creation_epoch: Any = ...,
    ) -> None: ...
    @property
    def is_finished(self) -> bool:
        """Returns True if the coroutine has completed."""
        ...
    @property
    def creation_epoch(self) -> Any: ...
    @property
    def creation_epoch_id(self) -> int: ...
    @property
    def is_stale_epoch(self) -> bool: ...

class CoroutineScheduler:
    """Manages coroutine lifecycle — start, stop, and tick."""

    def __init__(
        self,
        on_active_changed: Optional[Callable[[bool], None]] = ...,
        *,
        creation_epoch: Any = ...,
    ) -> None: ...
    def start(
        self,
        generator: Generator,
        owner: Any = ...,
        *,
        epoch: Any = ...,
    ) -> Coroutine:
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
    @property
    def creation_epoch(self) -> Any: ...
    @property
    def creation_epoch_id(self) -> int: ...
    @property
    def stale_epoch_coroutine_count(self) -> int: ...
    def diagnostics(self) -> dict[str, int]: ...
    def tick_update(self, scaled_dt: float, *, epoch: Any = ...) -> None:
        """Advance coroutines waiting on WaitForSeconds."""
        ...
    def tick_fixed_update(self, fixed_dt: float, *, epoch: Any = ...) -> None:
        """Advance coroutines waiting on WaitForFixedUpdate."""
        ...
    def tick_late_update(self, scaled_dt: float, *, epoch: Any = ...) -> None:
        """Advance coroutines waiting on WaitForEndOfFrame."""
        ...

def notify_runtime_epoch_published(epoch: Any) -> None: ...
