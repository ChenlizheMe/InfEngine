"""Regression tests for the cached Python lifecycle dispatch path."""

from __future__ import annotations

from Infernux.components import InxComponent
from Infernux.components._component_coroutine import ComponentCoroutineMixin
from Infernux.components._component_lifecycle import ComponentLifecycleMixin
from Infernux.components._component_lifecycle import _runtime_phase_dispatch_for_type
from Infernux.components._component_native import ComponentNativeMixin
from Infernux.components._component_registration import (
    candidate_component_registration_scope,
)
from Infernux.engine.runtime_dispatch import publish_runtime_dispatch_epoch


class _Probe(InxComponent):
    def awake(self) -> None:
        self.calls: list[str] = []

    def on_validate(self) -> None:
        self.calls.append("validate-old")

    def helper(self) -> None:
        self.calls.append("helper-old")


def test_reload_candidate_defers_dispatch_until_first_use():
    with candidate_component_registration_scope():
        class CandidateProbe(InxComponent):
            def update(self, delta_time: float) -> None:
                del delta_time

    assert "_runtime_phase_dispatch" not in CandidateProbe.__dict__
    assert "_runtime_phase_invokers" not in CandidateProbe.__dict__

    dispatch = _runtime_phase_dispatch_for_type(CandidateProbe)

    assert dispatch[0] == (CandidateProbe.update, True)
    assert CandidateProbe.__dict__["_runtime_phase_dispatch"] is dispatch
    assert CandidateProbe.__dict__["_runtime_phase_invokers"] is not None


def test_lifecycle_dispatch_does_not_retain_a_bound_method():
    probe = _Probe()
    probe.calls = []
    publication = publish_runtime_dispatch_epoch((_Probe,))
    publication.commit()
    try:
        assert probe._safe_lifecycle_call("on_validate") is True
        assert "_lifecycle_dispatch_cache" not in probe.__dict__

        assert probe._safe_lifecycle_call("on_validate") is True
        assert probe.calls == ["validate-old", "validate-old"]
    finally:
        publication.rollback()


def test_lifecycle_dispatch_uses_the_new_body_after_epoch_publication():
    probe = _Probe()
    probe.calls = []
    initial = publish_runtime_dispatch_epoch((_Probe,))
    initial.commit()

    old_method = _Probe.on_validate
    try:
        assert probe._safe_lifecycle_call("on_validate") is True

        def replacement(self) -> None:
            self.calls.append("validate-new")

        _Probe.on_validate = replacement
        publication = publish_runtime_dispatch_epoch((_Probe,))
        publication.commit()
        try:
            assert probe._safe_lifecycle_call("on_validate") is True
            assert probe.calls == ["validate-old", "validate-new"]
        finally:
            publication.rollback()
            _Probe.on_validate = old_method
    finally:
        initial.rollback()


def test_lifecycle_dispatch_observes_method_addition_and_deletion():
    probe = _Probe()
    probe.calls = []
    old_method = _Probe.on_validate
    initial = publish_runtime_dispatch_epoch((_Probe,))
    initial.commit()
    try:
        def validate(self) -> None:
            self.calls.append("validate")

        _Probe.on_validate = validate
        added = publish_runtime_dispatch_epoch((_Probe,))
        added.commit()
        try:
            assert probe._safe_lifecycle_call("on_validate") is True
            assert probe.calls == ["validate"]
        finally:
            added.rollback()

        del _Probe.on_validate
        removed = publish_runtime_dispatch_epoch((_Probe,))
        removed.commit()
        try:
            assert probe._safe_lifecycle_call("on_validate") is True
            assert probe.calls == ["validate"]
        finally:
            removed.rollback()
            _Probe.on_validate = old_method
    finally:
        initial.rollback()


def test_lifecycle_exception_isolated_without_retry():
    probe = _Probe()
    probe.calls = []
    old_method = _Probe.on_validate
    initial = publish_runtime_dispatch_epoch((_Probe,))
    initial.commit()
    try:
        def fail_once(self) -> None:
            self.calls.append("failed")
            raise RuntimeError("expected lifecycle failure")

        _Probe.on_validate = fail_once
        publication = publish_runtime_dispatch_epoch((_Probe,))
        publication.commit()
        try:
            assert probe._safe_lifecycle_call("on_validate") is False
            assert probe.calls == ["failed"]
        finally:
            publication.rollback()
            _Probe.on_validate = old_method
    finally:
        initial.rollback()


def test_published_type_does_not_fall_back_to_a_deleted_method():
    probe = _Probe()
    probe.calls = []
    old_helper = _Probe.helper
    initial = publish_runtime_dispatch_epoch((_Probe,))
    initial.commit()
    try:
        assert probe._safe_lifecycle_call("helper") is True
        del _Probe.helper
        publication = publish_runtime_dispatch_epoch((_Probe,))
        publication.commit()
        try:
            assert probe._safe_lifecycle_call("helper") is False
            assert probe.calls == ["helper-old"]
        finally:
            publication.rollback()
            _Probe.helper = old_helper
    finally:
        initial.rollback()


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
