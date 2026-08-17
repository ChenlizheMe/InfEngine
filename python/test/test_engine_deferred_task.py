"""Tests for Infernux.engine.deferred_task — DeferredTaskRunner."""

from __future__ import annotations

import sys
import types

import pytest


@pytest.fixture(autouse=True)
def _stub_engine_status(monkeypatch):
    """Stub EngineStatus so DeferredTaskRunner.submit/tick work."""
    fake_status = types.ModuleType("Infernux.engine.ui.engine_status")

    class EngineStatus:
        _label = ""
        _progress = 0.0

        @classmethod
        def set(cls, label, progress, **_kwargs):
            cls._label = label
            cls._progress = progress

        @classmethod
        def clear(cls, **_kwargs):
            cls._label = ""
            cls._progress = 0.0

    fake_status.EngineStatus = EngineStatus
    monkeypatch.setitem(sys.modules, "Infernux.engine.ui.engine_status", fake_status)
    yield EngineStatus


@pytest.fixture
def runner():
    from Infernux.engine.deferred_task import DeferredTaskRunner
    # Reset singleton
    DeferredTaskRunner._instance = None
    return DeferredTaskRunner.instance()


class TestDeferredTaskRunner:
    def test_singleton(self, runner):
        from Infernux.engine.deferred_task import DeferredTaskRunner
        assert DeferredTaskRunner.instance() is runner

    def test_not_busy_initially(self, runner):
        assert not runner.is_busy

    def test_submit_starts_task(self, runner):
        runner.submit("Test", [("Step 1", 0.5, lambda: None)])
        assert runner.is_busy
        assert runner.active_task_name == "Test"
        assert runner.active_step_label == "Step 1"

    def test_submit_rejects_when_busy(self, runner):
        runner.submit("A", [("s", 0.5, lambda: None)])
        assert not runner.submit("B", [("s", 0.5, lambda: None)])

    def test_tick_executes_steps(self, runner):
        executed = []
        runner.submit("Test", [
            ("S1", 0.3, lambda: executed.append(1)),
            ("S2", 0.6, lambda: executed.append(2)),
            ("S3", 1.0, lambda: executed.append(3)),
        ])

        runner.tick()  # execute step 0
        assert executed == [1]

        runner.tick()  # execute step 1
        assert executed == [1, 2]

        runner.tick()  # execute step 2
        assert executed == [1, 2, 3]
        assert not runner.is_busy
        assert runner.active_task_name == ""
        assert runner.active_step_label == ""

    def test_on_done_callback(self, runner):
        results = []
        runner.submit("Test", [
            ("S1", 1.0, lambda: None),
        ], on_done=lambda ok: results.append(ok))

        runner.tick()  # execute and finalize
        assert results == [True]

    def test_failure_propagates_to_on_done(self, runner):
        results = []

        def failing():
            raise RuntimeError("boom")

        runner.submit("Test", [
            ("S1", 1.0, failing),
        ], on_done=lambda ok: results.append(ok))

        runner.tick()  # execute (fails) and finalize
        assert results == [False]

    def test_cancel(self, runner):
        runner.submit("Test", [
            ("S1", 0.5, lambda: None),
            ("S2", 1.0, lambda: None),
        ])
        assert runner.is_busy
        runner.cancel()
        assert not runner.is_busy
        assert runner.active_task_name == ""
        assert runner.active_step_label == ""

    def test_none_fn_does_not_crash(self, runner):
        runner.submit("Test", [
            ("S1", 1.0, None),
        ])
        runner.tick()  # should not raise
        assert not runner.is_busy

    def test_false_return_marks_failed(self, runner):
        results = []
        runner.submit("Test", [
            ("S1", 0.5, lambda: False),
            ("S2", 1.0, lambda: None),
        ], on_done=lambda ok: results.append(ok))

        runner.tick()  # S1 returns False
        runner.tick()  # S2 skipped (failed) and finalizes
        assert results == [False]
