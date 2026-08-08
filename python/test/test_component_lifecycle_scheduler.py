"""Regression tests for the shared runtime phase-plan service."""

from __future__ import annotations

from Infernux.components._component_lifecycle import (
    ComponentLifecycleMixin,
    RuntimeExecutionScheduler,
    refresh_runtime_dispatch_cache,
)


class _ScheduledProbe(ComponentLifecycleMixin):
    def __init__(self, component_id: int, order: int = 0) -> None:
        self._component_id = component_id
        self._execution_order = order
        self._native_generation = 1
        self._enabled = True
        self._is_destroyed = False
        self.calls: list[str] = []

    @property
    def component_id(self) -> int:
        return self._component_id

    @property
    def execution_order(self) -> int:
        return self._execution_order

    def update(self, delta_time: float) -> None:
        self.calls.append(f"update:{delta_time}")

    def fixed_update(self, delta_time: float) -> None:
        self.calls.append(f"fixed:{delta_time}")

    def late_update(self, delta_time: float) -> None:
        self.calls.append(f"late:{delta_time}")


class _FailingProbe(_ScheduledProbe):
    def update(self, _delta_time: float) -> None:
        self.calls.append("failed")
        raise RuntimeError("expected")


def test_runtime_scheduler_builds_once_and_reuses_stable_plan():
    scheduler = RuntimeExecutionScheduler()
    first = _ScheduledProbe(2, order=2)
    second = _ScheduledProbe(1, order=1)
    scheduler.register_component(first)
    scheduler.register_component(second)

    first_plan = scheduler.prepare_frame()
    second_plan = scheduler.prepare_frame()

    assert first_plan is second_plan
    assert second_plan["update"] == (second, first)
    counters = scheduler.profiler_snapshot()
    assert counters["plan_builds"] == 1
    assert counters["plan_hits"] == 1
    assert counters["plan_prepare_calls"] == 2


def test_runtime_scheduler_only_rebuilds_for_structure_not_body_reload():
    scheduler = RuntimeExecutionScheduler()
    probe = _ScheduledProbe(1)
    scheduler.register_component(probe)
    plan = scheduler.prepare_frame()
    assert scheduler.profiler_snapshot()["plan_builds"] == 1

    scheduler.mark_type_body_reload(type(probe))
    assert scheduler.prepare_frame() is plan
    counters = scheduler.profiler_snapshot()
    assert counters["plan_builds"] == 1
    assert counters["body_reload_updates"] == 1

    scheduler.mark_structure_changed("component enabled")
    assert scheduler.prepare_frame() is not plan
    assert scheduler.profiler_snapshot()["plan_builds"] == 2


def test_runtime_scheduler_preserves_order_and_exception_isolation():
    scheduler = RuntimeExecutionScheduler()
    failing = _FailingProbe(1)
    following = _ScheduledProbe(2)
    scheduler.register_component(failing)
    scheduler.register_component(following)

    scheduler.execute_phase("update", 0.25)

    assert failing.calls == ["failed"]
    assert following.calls == ["update:0.25"]
    assert scheduler.profiler_snapshot()["phase_dispatches"] == 2


def test_disabled_component_is_removed_from_next_structural_plan():
    scheduler = RuntimeExecutionScheduler()
    enabled = _ScheduledProbe(1)
    disabled = _ScheduledProbe(2)
    disabled._enabled = False
    scheduler.register_component(enabled)
    scheduler.register_component(disabled)

    assert scheduler.phase_plan("update") == (enabled,)

    disabled._enabled = True
    scheduler.mark_structure_changed("component enabled")
    assert scheduler.phase_plan("update") == (enabled, disabled)


def test_runtime_frame_keeps_one_dispatch_revision_across_all_phases():
    class BodyProbe(ComponentLifecycleMixin):
        def __init__(self) -> None:
            self._component_id = 1
            self._execution_order = 0
            self._native_generation = 1
            self._enabled = True
            self._is_destroyed = False
            self.calls = []

        @property
        def component_id(self):
            return self._component_id

        @property
        def execution_order(self):
            return self._execution_order

        def fixed_update(self, _delta_time):
            self.calls.append("fixed-old")

        def update(self, _delta_time):
            self.calls.append("update-old")

        def late_update(self, _delta_time):
            self.calls.append("late-old")

    scheduler = RuntimeExecutionScheduler()
    probe = BodyProbe()
    scheduler.register_component(probe)
    frame = scheduler.begin_frame()

    def new_fixed_update(self, _delta_time):
        self.calls.append("fixed-new")

    def new_update(self, _delta_time):
        self.calls.append("update-new")

    def new_late_update(self, _delta_time):
        self.calls.append("late-new")

    BodyProbe.fixed_update = new_fixed_update
    BodyProbe.update = new_update
    BodyProbe.late_update = new_late_update
    refresh_runtime_dispatch_cache(BodyProbe, (probe,))

    frame.execute_phase("fixed_update", 0.02)
    frame.execute_phase("update", 0.016)
    frame.execute_phase("late_update", 0.016)
    frame.close()

    assert probe.calls == ["fixed-old", "update-old", "late-old"]
    next_frame = scheduler.begin_frame()
    try:
        next_frame.execute_phase("fixed_update", 0.02)
        next_frame.execute_phase("update", 0.016)
        next_frame.execute_phase("late_update", 0.016)
    finally:
        next_frame.close()
    assert probe.calls == [
        "fixed-old",
        "update-old",
        "late-old",
        "fixed-new",
        "update-new",
        "late-new",
    ]


def test_closed_runtime_frame_rejects_late_dispatch():
    scheduler = RuntimeExecutionScheduler()
    probe = _ScheduledProbe(1)
    scheduler.register_component(probe)
    frame = scheduler.begin_frame()
    frame.close()

    try:
        frame.execute_phase("update", 0.016)
    except RuntimeError as exc:
        assert "already closed" in str(exc)
    else:
        raise AssertionError("closed frame accepted lifecycle dispatch")
