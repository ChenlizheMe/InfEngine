"""Regression tests for the cached Python lifecycle dispatch path."""

from __future__ import annotations

from Infernux.components._component_lifecycle import ComponentLifecycleMixin
from Infernux.components._component_coroutine import ComponentCoroutineMixin
from Infernux.components._component_native import ComponentNativeMixin


class _Probe(ComponentLifecycleMixin):
    def __init__(self) -> None:
        self.calls: list[str] = []

    def update(self, value: float) -> None:
        self.calls.append(f"update:{value}")


def test_lifecycle_dispatch_reuses_bound_method_for_one_component():
    probe = _Probe()

    assert probe._safe_lifecycle_call("update", 1.0) is True
    cached = probe.__dict__["_lifecycle_dispatch_cache"]["update"]
    assert cached[0] is _Probe
    bound_method = cached[1]

    assert probe._safe_lifecycle_call("update", 2.0) is True
    assert probe.__dict__["_lifecycle_dispatch_cache"]["update"][1] is bound_method
    assert probe.calls == ["update:1.0", "update:2.0"]


def test_lifecycle_dispatch_refreshes_after_class_replacement():
    probe = _Probe()
    assert probe._safe_lifecycle_call("update", 1.0) is True

    class Replacement(_Probe):
        def update(self, value: float) -> None:
            self.calls.append(f"replacement:{value}")

    probe.__class__ = Replacement
    assert probe._safe_lifecycle_call("update", 2.0) is True
    assert probe.calls == ["update:1.0", "replacement:2.0"]
    assert probe.__dict__["_lifecycle_dispatch_cache"]["update"][0] is Replacement


class _NativeCoroutineFlag:
    def __init__(self) -> None:
        self.transitions: list[bool] = []

    def set_coroutine_scheduler_active(self, active: bool) -> None:
        self.transitions.append(active)


class _NoUpdateOverride(ComponentLifecycleMixin, ComponentCoroutineMixin, ComponentNativeMixin):
    def __init__(self) -> None:
        self._cpp_component = None
        self._coroutine_scheduler = None
        self._enabled = True
        self._awake_called = True
        self._has_started = False
        self._is_destroyed = False
        self._native_generation = 0

    def start(self) -> None:
        self.start_coroutine(self._wait_once())

    def _wait_once(self):
        yield None


def test_coroutine_transition_reaches_native_without_update_override():
    component = _NoUpdateOverride()
    component._call_start()

    native = _NativeCoroutineFlag()
    native.component_id = 1
    native.execution_order = 0
    native.enabled = True
    component._bind_native_component(native)
    assert native.transitions == [True]
    assert component._coroutine_scheduler is not None

    component._tick_coroutines_update(1.0 / 60.0)
    assert native.transitions == [True, False]

    component.start_coroutine(component._wait_once())
    assert native.transitions == [True, False, True]
    component.stop_all_coroutines()
    assert native.transitions == [True, False, True, False]


def test_empty_retained_scheduler_rebind_publishes_false():
    component = _NoUpdateOverride()
    component._call_start()
    component.stop_all_coroutines()

    assert component._coroutine_scheduler is not None
    assert component._coroutine_scheduler.count == 0

    rebound = _NativeCoroutineFlag()
    rebound.component_id = 2
    rebound.execution_order = 0
    rebound.enabled = True
    component._bind_native_component(rebound)
    assert rebound.transitions == [False]


class _PhaseProbe(ComponentLifecycleMixin):
    def __init__(self) -> None:
        self.calls: list[str] = []
        self._enabled = True
        self._coroutine_scheduler = None

    def update(self, delta_time: float) -> None:
        self.calls.append(f"u:{delta_time}")

    def fixed_update(self, delta_time: float) -> None:
        self.calls.append(f"f:{delta_time}")

    def late_update(self, delta_time: float) -> None:
        self.calls.append(f"l:{delta_time}")


def test_runtime_phase_dispatch_uses_one_class_owned_unbound_table():
    probe = _PhaseProbe()

    probe._call_update(1.0)
    probe._call_fixed_update(2.0)
    probe._call_late_update(3.0)

    dispatch = _PhaseProbe.__dict__["_runtime_phase_dispatch"]
    assert len(dispatch) == 3
    assert dispatch[0] == (_PhaseProbe.update, True)
    assert dispatch[1] == (_PhaseProbe.fixed_update, True)
    assert dispatch[2] == (_PhaseProbe.late_update, True)
    assert probe.calls == ["u:1.0", "f:2.0", "l:3.0"]


def test_runtime_phase_dispatch_preserves_static_and_classmethod_descriptors():
    class DescriptorProbe(ComponentLifecycleMixin):
        calls: list[str] = []
        _enabled = True
        _coroutine_scheduler = None

        @staticmethod
        def update(delta_time: float) -> None:
            DescriptorProbe.calls.append(f"u:{delta_time}")

        @classmethod
        def fixed_update(cls, delta_time: float) -> None:
            cls.calls.append(f"f:{delta_time}")

        def late_update(self, delta_time: float) -> None:
            self.calls.append(f"l:{delta_time}")

    probe = DescriptorProbe()
    probe._call_update(1.0)
    probe._call_fixed_update(2.0)
    probe._call_late_update(3.0)

    assert DescriptorProbe.calls == ["u:1.0", "f:2.0", "l:3.0"]


def test_runtime_phase_dispatch_does_not_query_native_enabled_property():
    class DisabledProbe(ComponentLifecycleMixin):
        def __init__(self) -> None:
            self._enabled = False
            self._coroutine_scheduler = None
            self.calls: list[float] = []

        @property
        def enabled(self):
            raise AssertionError("hot phase path must use the mirrored enabled bit")

        def update(self, delta_time: float) -> None:
            self.calls.append(delta_time)

    probe = DisabledProbe()
    probe._call_update(1.0)
    assert probe.calls == []


def test_runtime_dispatch_treats_omitted_optional_phases_as_noops():
    class MinimalProbe(ComponentLifecycleMixin):
        def __init__(self) -> None:
            self._enabled = True
            self._runtime_coroutine_scheduler = None
            self.calls = []

        def update(self, delta_time: float) -> None:
            self.calls.append(delta_time)

    probe = MinimalProbe()
    probe._call_update(1.0)
    probe._call_fixed_update(2.0)
    probe._call_late_update(3.0)

    assert probe.calls == [1.0]
    assert len(MinimalProbe.__dict__["_runtime_phase_invokers"]) == 3


def test_runtime_dispatch_skips_retained_but_inactive_coroutine_scheduler():
    class SchedulerProbe(ComponentLifecycleMixin):
        def __init__(self) -> None:
            self._enabled = True
            self._runtime_coroutine_scheduler = None
            self.ticks = 0

        def update(self, _delta_time: float) -> None:
            pass

    class Scheduler:
        def tick_update(self, _delta_time: float) -> None:
            probe.ticks += 1

    probe = SchedulerProbe()
    retained = Scheduler()
    probe._coroutine_scheduler = retained
    probe._call_update(1.0)
    assert probe.ticks == 0

    probe._runtime_coroutine_scheduler = retained
    probe._call_update(1.0)
    assert probe.ticks == 1
