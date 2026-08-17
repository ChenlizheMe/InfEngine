"""Deferred multi-frame task runner.

Allows long operations to be split into discrete steps that execute one per
frame, giving the status bar a chance to redraw between steps.

Usage::

    from Infernux.engine.deferred_task import DeferredTaskRunner

    runner = DeferredTaskRunner.instance()
    runner.submit("Play Mode", [
        ("保存场景...",  0.2, save_fn),
        ("重建场景...",  0.6, rebuild_fn),
    ], on_done=lambda ok: EngineStatus.flash("Done", 1.0))

The runner is ticked once per frame by the engine's pre-GUI callback.
"""

from __future__ import annotations

from typing import Callable, Optional, List, Tuple


class DeferredTaskRunner:
    """Execute a sequence of steps across multiple frames."""

    _instance: Optional[DeferredTaskRunner] = None

    @classmethod
    def instance(cls) -> DeferredTaskRunner:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._steps: List[Tuple[str, float, Optional[Callable[[], object]]]] = []
        self._task_name: str = ""
        self._on_done: Optional[Callable[[bool], None]] = None
        self._index: int = 0
        self._failed: bool = False

    # ── public API ────────────────────────────────────────────────────

    @property
    def is_busy(self) -> bool:
        """True while a task is in progress."""
        return len(self._steps) > 0

    @property
    def active_task_name(self) -> str:
        """Stable name of the active task, or an empty string while idle."""
        return self._task_name if self.is_busy else ""

    @property
    def active_step_label(self) -> str:
        """Human-readable current step without exposing task internals."""
        if not self.is_busy or self._index >= len(self._steps):
            return ""
        return self._steps[self._index][0]

    def submit(
        self,
        task_name: str,
        steps: list[tuple[str, float, Optional[Callable[[], object]]]],
        on_done: Optional[Callable[[bool], None]] = None,
    ) -> bool:
        """Enqueue a multi-step task.  Returns False if already busy."""
        if self.is_busy:
            return False
        self._task_name = task_name
        self._steps = list(steps)
        self._on_done = on_done
        self._index = 0
        self._failed = False
        # Set the first step's status immediately so it's visible this frame
        if self._steps:
            label, progress, _ = self._steps[0]
            from Infernux.engine.ui.engine_status import EngineStatus
            EngineStatus.set(label, progress, source="deferred", priority=10)
        return True

    def tick(self) -> None:
        """Execute the next pending step (called once per frame)."""
        if not self._steps:
            return

        if self._index >= len(self._steps):
            # All steps done — finalise
            self._finish()
            return

        label, progress, fn = self._steps[self._index]

        # Update status bar *before* executing (visible this frame)
        from Infernux.engine.ui.engine_status import EngineStatus
        EngineStatus.set(label, progress, source="deferred", priority=10)

        # Execute the step
        if fn is not None and not self._failed:
            try:
                result = fn()
                if result is False:
                    self._failed = True
            except Exception as exc:
                from Infernux.debug import Debug
                Debug.log_error(f"[DeferredTask] Step '{label}' failed: {exc}")
                self._failed = True

        self._index += 1
        # The final operation is already complete. Finalizing here avoids an
        # empty extra editor frame before input and automation can observe the
        # transition as idle again.
        if self._index >= len(self._steps):
            self._finish()

    def cancel(self) -> None:
        """Cancel outstanding steps."""
        self._steps.clear()
        self._index = 0
        self._on_done = None
        self._failed = False
        self._task_name = ""
        from Infernux.engine.ui.engine_status import EngineStatus
        EngineStatus.clear(source="deferred")

    # ── internals ─────────────────────────────────────────────────────

    def _finish(self) -> None:
        on_done = self._on_done
        ok = not self._failed
        self._steps.clear()
        self._index = 0
        self._on_done = None
        self._failed = False
        self._task_name = ""
        from Infernux.engine.ui.engine_status import EngineStatus
        EngineStatus.clear(source="deferred")
        if on_done:
            try:
                on_done(ok)
            except Exception as exc:
                from Infernux.debug import Debug
                Debug.log_error(f"[DeferredTask] on_done callback failed: {exc}")
