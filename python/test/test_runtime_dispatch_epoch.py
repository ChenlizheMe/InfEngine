"""Focused R3-A tests for immutable runtime dispatch publications."""

from __future__ import annotations

import threading

import pytest

from Infernux.components._component_lifecycle import (
    ComponentLifecycleMixin,
    RuntimeExecutionScheduler,
)
from Infernux.components.script_loader import (
    ComponentBodyReloadRequest,
    ComponentBodyReloadTransaction,
    _plan_component_class_body_patch,
)
from Infernux.components.registry import snapshot_component_registry_state
from Infernux.engine.runtime_dispatch import (
    RuntimeDispatchPublication,
    build_type_dispatch_descriptor,
    current_runtime_epoch,
    publish_runtime_dispatch_epoch,
    resolve_runtime_method,
)


class _EpochProbe(ComponentLifecycleMixin):
    def __init__(self) -> None:
        self._component_id = 1
        self._execution_order = 0
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

    def fixed_update(self, _delta_time: float) -> None:
        self.calls.append("fixed-old")

    def update(self, _delta_time: float) -> None:
        self.calls.append(self.helper())

    def late_update(self, _delta_time: float) -> None:
        self.calls.append("late-old")

    def helper(self) -> str:
        return "helper-old"


def _publish(component_type, instance):
    del instance
    publication = publish_runtime_dispatch_epoch(
        (component_type,),
    )
    publication.commit()
    return publication


def test_one_frame_uses_one_epoch_for_fixed_update_update_and_late_update():
    scheduler = RuntimeExecutionScheduler()
    probe = _EpochProbe()
    old_methods = (
        _EpochProbe.fixed_update,
        _EpochProbe.update,
        _EpochProbe.late_update,
        _EpochProbe.helper,
    )
    scheduler.register_component(probe)
    frame = scheduler.begin_frame()
    before = frame.epoch

    def new_fixed(self, _delta_time):
        self.calls.append("fixed-new")

    def new_update(self, _delta_time):
        self.calls.append(self.helper())

    def new_late(self, _delta_time):
        self.calls.append("late-new")

    before_publish = current_runtime_epoch()
    try:
        _publish(_EpochProbe, probe)
    except RuntimeError as exc:
        assert "safe point" in str(exc)
    else:
        raise AssertionError("active frame accepted a runtime publication")
    assert current_runtime_epoch() is before_publish

    # The safe-point guard keeps class body and helper lookup unchanged while
    # this frame is alive; direct self.helper() therefore stays old naturally.
    frame.execute_phase("fixed_update", 0.02)
    frame.execute_phase("fixed_update", 0.02)
    frame.execute_phase("update", 0.016)
    frame.execute_phase("late_update", 0.016)
    assert frame.epoch is before
    assert probe.calls == ["fixed-old", "fixed-old", "helper-old", "late-old"]
    frame.close()

    try:
        _EpochProbe.fixed_update = new_fixed
        _EpochProbe.update = new_update
        _EpochProbe.late_update = new_late
        _EpochProbe.helper = lambda self: "helper-new"
        _publish(_EpochProbe, probe)
        next_frame = scheduler.begin_frame()
        try:
            assert next_frame.epoch.epoch_id > before.epoch_id
            next_frame.execute_phase("fixed_update", 0.02)
            next_frame.execute_phase("update", 0.016)
            next_frame.execute_phase("late_update", 0.016)
        finally:
            next_frame.close()
        assert probe.calls[-3:] == ["fixed-new", "helper-new", "late-new"]
    finally:
        (_EpochProbe.fixed_update, _EpochProbe.update,
         _EpochProbe.late_update, _EpochProbe.helper) = old_methods
        _publish(_EpochProbe, probe)


def test_phase_override_add_and_remove_are_published_to_the_next_frame():
    class AddRemoveProbe(ComponentLifecycleMixin):
        def __init__(self):
            self._component_id = 2
            self._execution_order = 0
            self._native_generation = 1
            self._enabled = True
            self._is_destroyed = False
            self.calls = []

        def update(self, _delta_time):
            self.calls.append("update")

    scheduler = RuntimeExecutionScheduler()
    probe = AddRemoveProbe()
    scheduler.register_component(probe)
    assert scheduler.phase_plan("late_update") == ()
    assert scheduler.profiler_snapshot()["plan_builds"] == 1

    AddRemoveProbe.late_update = lambda self, _delta_time: self.calls.append("late")
    _publish(AddRemoveProbe, probe)
    assert scheduler.phase_plan("late_update") == (probe,)
    assert scheduler.profiler_snapshot()["plan_builds"] == 2

    del AddRemoveProbe.late_update
    _publish(AddRemoveProbe, probe)
    assert scheduler.phase_plan("late_update") == ()
    assert scheduler.profiler_snapshot()["plan_builds"] == 3


def test_phase_plan_is_not_rebuilt_for_update_or_helper_replacement():
    class PlanProbe(ComponentLifecycleMixin):
        def __init__(self):
            self._component_id = 3
            self._execution_order = 0
            self._native_generation = 1
            self._enabled = True
            self._is_destroyed = False

        def update(self, _delta_time):
            return self.helper()

        def helper(self):
            return "old"

    scheduler = RuntimeExecutionScheduler()
    probe = PlanProbe()
    scheduler.register_component(probe)
    plan = scheduler.phase_plan("update")
    assert plan == (probe,)
    assert scheduler.profiler_snapshot()["plan_builds"] == 1

    PlanProbe.update = lambda self, _delta_time: self.helper()
    PlanProbe.helper = lambda self: "new"
    _publish(PlanProbe, probe)

    assert scheduler.phase_plan("update") is plan
    assert scheduler.profiler_snapshot()["plan_builds"] == 1


def test_direct_lifecycle_entry_requires_a_published_epoch_descriptor():
    class DirectEntryProbe(ComponentLifecycleMixin):
        def __init__(self):
            self._enabled = True
            self._coroutine_scheduler = None
            self.calls = []

        def update(self, _delta_time):
            self.calls.append("update")

        def fixed_update(self, _delta_time):
            self.calls.append("fixed")

        def late_update(self, _delta_time):
            self.calls.append("late")

    assert "_runtime_phase_dispatch" not in DirectEntryProbe.__dict__
    assert "_runtime_phase_invokers" not in DirectEntryProbe.__dict__
    probe = DirectEntryProbe()
    publication = publish_runtime_dispatch_epoch((DirectEntryProbe,))
    publication.commit()
    try:
        probe._call_update(0.1)
        probe._call_fixed_update(0.1)
        probe._call_late_update(0.1)
        descriptor = current_runtime_epoch().require_descriptor(DirectEntryProbe)
        assert len(descriptor.phase_invokers) == 3
        assert probe.calls == ["update", "fixed", "late"]
        assert "_runtime_phase_dispatch" not in DirectEntryProbe.__dict__
        assert "_runtime_phase_invokers" not in DirectEntryProbe.__dict__
    finally:
        publication.rollback()


def test_scheduler_shares_one_cached_invoker_tuple_across_instances_and_frames():
    class SharedInvokerProbe(ComponentLifecycleMixin):
        def __init__(self):
            self._enabled = True
            self._coroutine_scheduler = None
            self.calls = 0

        def update(self, _delta_time):
            self.calls += 1

    first = SharedInvokerProbe()
    second = SharedInvokerProbe()
    scheduler = RuntimeExecutionScheduler()
    scheduler.register_component(first)
    scheduler.register_component(second)

    first_frame = scheduler.begin_frame()
    try:
        descriptor = current_runtime_epoch().descriptor_for(SharedInvokerProbe)
        assert descriptor is not None
        assert descriptor.phase_dispatch is descriptor.phase_dispatch
        assert descriptor.phase_invokers is descriptor.phase_invokers
        first_invokers = first_frame.component_snapshots[id(first)][1]
        second_invokers = first_frame.component_snapshots[id(second)][1]
        assert first_invokers is descriptor.phase_invokers
        assert second_invokers is first_invokers
    finally:
        first_frame.close()

    second_frame = scheduler.begin_frame()
    try:
        assert second_frame.component_snapshots[id(first)][1] is first_invokers
        assert second_frame.component_snapshots[id(second)][1] is first_invokers
    finally:
        second_frame.close()

    independent = build_type_dispatch_descriptor(SharedInvokerProbe)
    assert independent.phase_invokers is independent.phase_invokers


def test_helper_lookup_can_use_old_or_current_epoch():
    probe = _EpochProbe()
    old_helper = _EpochProbe.helper
    before = current_runtime_epoch()
    old = resolve_runtime_method(probe, "helper", epoch=before)
    assert old is not None and old() == "helper-old"

    _EpochProbe.helper = lambda self: "helper-new"
    publication = _publish(_EpochProbe, probe)
    try:
        assert resolve_runtime_method(probe, "helper", epoch=before)() == "helper-old"
        assert resolve_runtime_method(probe, "helper", epoch=publication.after)() == "helper-new"
    finally:
        publication.rollback()
        _EpochProbe.helper = old_helper
        _publish(_EpochProbe, probe)


def test_failed_transaction_rolls_back_epoch_and_class_body(monkeypatch):
    class TransactionProbe(ComponentLifecycleMixin):
        def update(self, _delta_time):
            return self.helper()

        def helper(self):
            return "old"

    class CandidateProbe(ComponentLifecycleMixin):
        def update(self, _delta_time):
            return self.helper()

        def helper(self):
            return "new"

    CandidateProbe.__name__ = TransactionProbe.__name__
    CandidateProbe.__qualname__ = TransactionProbe.__qualname__
    before = current_runtime_epoch()
    request = ComponentBodyReloadRequest(
        file_path="transaction_probe.py",
        target_types=(TransactionProbe,),
        instances_by_type={TransactionProbe: ()},
    )
    transaction = ComponentBodyReloadTransaction(
        (request,),
        ((TransactionProbe, _plan_component_class_body_patch(TransactionProbe, CandidateProbe)),),
        registry_entries=(),
        registry_snapshot=snapshot_component_registry_state(),
        module_snapshot={},
        diagnostic_snapshot=({}, 0),
        had_live_targets=False,
        member_status=(),
    )

    def fail_commit(self):
        raise RuntimeError("simulated publication failure")

    monkeypatch.setattr(RuntimeDispatchPublication, "commit", fail_commit)
    try:
        transaction.commit()
    except RuntimeError as exc:
        assert "simulated publication failure" in str(exc)
    else:
        raise AssertionError("failed publication unexpectedly committed")

    assert transaction.rolled_back is True
    assert current_runtime_epoch() is before
    assert TransactionProbe.helper(TransactionProbe()) == "old"


def test_active_frame_rejects_transaction_before_class_mutation():
    class ActiveTarget(ComponentLifecycleMixin):
        def update(self, _delta_time):
            return self.helper()

        def helper(self):
            return "old"

    class ActiveCandidate(ComponentLifecycleMixin):
        def update(self, _delta_time):
            return self.helper()

        def helper(self):
            return "new"

    ActiveCandidate.__name__ = ActiveTarget.__name__
    ActiveCandidate.__qualname__ = ActiveTarget.__qualname__
    probe = ActiveTarget()
    scheduler = RuntimeExecutionScheduler()
    scheduler.register_component(probe)
    frame = scheduler.begin_frame()
    before = current_runtime_epoch()
    request = ComponentBodyReloadRequest(
        file_path="active_target.py",
        target_types=(ActiveTarget,),
        instances_by_type={ActiveTarget: (probe,)},
    )
    transaction = ComponentBodyReloadTransaction(
        (request,),
        ((ActiveTarget, _plan_component_class_body_patch(ActiveTarget, ActiveCandidate)),),
        registry_entries=(),
        registry_snapshot=snapshot_component_registry_state(),
        module_snapshot={},
        diagnostic_snapshot=({}, 0),
        had_live_targets=True,
        member_status=(),
    )
    try:
        with pytest.raises(RuntimeError, match="safe point"):
            transaction.commit()
    finally:
        frame.close()
    assert transaction.rolled_back is True
    assert current_runtime_epoch() is before
    assert ActiveTarget.helper(probe) == "old"


def test_publication_validation_failure_does_not_advance_epoch(monkeypatch):
    class ValidationProbe(ComponentLifecycleMixin):
        def helper(self):
            return "stable"

    before = current_runtime_epoch()
    import Infernux.engine.runtime_dispatch as runtime_dispatch

    def fail_validation(*_args, **_kwargs):
        raise RuntimeError("validation failure")

    monkeypatch.setattr(
        runtime_dispatch,
        "validate_runtime_callback_bindings",
        fail_validation,
    )
    try:
        publish_runtime_dispatch_epoch((ValidationProbe,))
    except RuntimeError as exc:
        assert "validation failure" in str(exc)
    else:
        raise AssertionError("validation failure unexpectedly published")
    assert current_runtime_epoch() is before


def test_deferred_commit_publishes_only_at_commit():
    class DeferredProbe(ComponentLifecycleMixin):
        def helper(self):
            return "stable"

    before = current_runtime_epoch()
    publication = publish_runtime_dispatch_epoch(
        (DeferredProbe,),
        defer_commit=True,
    )

    assert current_runtime_epoch() is before
    publication.commit()
    assert current_runtime_epoch() is publication.after
    assert publication.committed is True
    publication.rollback()
    assert current_runtime_epoch() is before


def test_runtime_epoch_publication_rejects_non_owner_thread():
    class WorkerPublicationProbe(ComponentLifecycleMixin):
        def helper(self):
            return "stable"

    # Bind the publication owner deterministically before starting the worker.
    from Infernux.engine.runtime_dispatch import assert_runtime_dispatch_safe_point

    assert_runtime_dispatch_safe_point()
    failures = []

    def publish_from_worker():
        try:
            publish_runtime_dispatch_epoch((WorkerPublicationProbe,))
        except Exception as exc:
            failures.append(exc)

    worker = threading.Thread(target=publish_from_worker)
    worker.start()
    worker.join(timeout=2.0)

    assert worker.is_alive() is False
    assert len(failures) == 1
    assert "owner thread" in str(failures[0])


def test_stale_publication_token_cannot_rewind_a_newer_epoch():
    class StaleProbe(ComponentLifecycleMixin):
        def helper(self):
            return "stable"

    first = _publish(StaleProbe, StaleProbe())
    second = _publish(StaleProbe, StaleProbe())
    first.rollback()
    assert current_runtime_epoch() is second.after
    second.rollback()
    assert current_runtime_epoch() is first.after
