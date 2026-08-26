"""Owner-thread dispatch shared by automation transports and Host services."""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable


class CommandFuture:
    def __init__(self, name: str, *, timeout_ms: int = 30000):
        self.name = str(name)
        self._event = threading.Event()
        self._result: Any = None
        self._error: BaseException | None = None
        self._lock = threading.Lock()
        self._cancelled = False
        self._deadline = time.monotonic() + max(int(timeout_ms), 1) / 1000.0

    def set_result(self, value: Any) -> None:
        with self._lock:
            if self._cancelled or self._event.is_set():
                return
            self._result = value
            self._event.set()

    def set_error(self, error: BaseException) -> None:
        with self._lock:
            if self._cancelled or self._event.is_set():
                return
            self._error = error
            self._event.set()

    def cancel(self, reason: str = "Host command timed out before execution.") -> bool:
        with self._lock:
            if self._event.is_set():
                return False
            self._cancelled = True
            self._error = TimeoutError(f"{reason} ({self.name})")
            self._event.set()
            return True

    def can_execute(self) -> bool:
        with self._lock:
            if self._cancelled or self._event.is_set():
                return False
            if time.monotonic() >= self._deadline:
                self._cancelled = True
                self._error = TimeoutError(
                    f"Host command expired before execution: {self.name}"
                )
                self._event.set()
                return False
            return True

    def result(self, timeout: float | None = None) -> Any:
        if not self._event.wait(timeout):
            self.cancel()
            raise TimeoutError(f"Host command timed out: {self.name}")
        if self._error is not None:
            raise self._error
        return self._result


class MainThreadCommandQueue:
    """Process singleton drained by graphical and headless engine ticks."""

    _instance: "MainThreadCommandQueue | None" = None

    def __init__(self) -> None:
        self._queue: queue.Queue[tuple[str, Callable[[], Any], CommandFuture]] = (
            queue.Queue()
        )
        self._main_thread_id: int | None = None
        self._owner_lock = threading.Lock()

    @classmethod
    def instance(cls) -> "MainThreadCommandQueue":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def submit(
        self, name: str, fn: Callable[[], Any], *, timeout_ms: int = 30000
    ) -> CommandFuture:
        future = CommandFuture(name, timeout_ms=timeout_ms)
        with self._owner_lock:
            owner_thread_id = self._main_thread_id
        if owner_thread_id == threading.get_ident():
            try:
                future.set_result(fn())
            except BaseException as exc:
                future.set_error(exc)
            return future
        self._queue.put((name, fn, future))
        return future

    def run_sync(
        self, name: str, fn: Callable[[], Any], *, timeout_ms: int = 30000
    ) -> Any:
        return self.submit(name, fn, timeout_ms=timeout_ms).result(
            timeout=max(timeout_ms, 1) / 1000.0
        )

    def drain(self, max_commands: int = 16) -> int:
        with self._owner_lock:
            self._main_thread_id = threading.get_ident()
        processed = 0
        for _ in range(max(int(max_commands), 0)):
            try:
                _name, fn, future = self._queue.get_nowait()
            except queue.Empty:
                break
            if future.can_execute():
                try:
                    future.set_result(fn())
                except BaseException as exc:
                    future.set_error(exc)
            processed += 1
        return processed

    def cancel_pending(self, reason: str = "Host service stopped.") -> int:
        cancelled = 0
        while True:
            try:
                _name, _fn, future = self._queue.get_nowait()
            except queue.Empty:
                break
            cancelled += int(future.cancel(reason))
        return cancelled

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        deadline = time.monotonic() + max(float(timeout), 0.0)
        while time.monotonic() < deadline:
            with self._owner_lock:
                ready = self._main_thread_id is not None
            if ready:
                return True
            time.sleep(0.01)
        with self._owner_lock:
            return self._main_thread_id is not None

    def release_owner(self, reason: str = "Host owner thread stopped.") -> int:
        cancelled = self.cancel_pending(reason)
        with self._owner_lock:
            self._main_thread_id = None
        return cancelled


__all__ = ["CommandFuture", "MainThreadCommandQueue"]
