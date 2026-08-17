"""Focused contracts for Split-R5 typed runtime invalidation."""

from __future__ import annotations

import pytest

from Infernux.components._component_lifecycle import (
    ComponentLifecycleMixin,
    RuntimeExecutionScheduler,
    notify_runtime_component_changed,
    notify_runtime_component_value_changed,
)
from Infernux.components._component_registration import (
    candidate_component_registration_scope,
)
from Infernux.components.component import InxComponent
from Infernux.engine.player_runtime import PlayerRuntimeSession
from Infernux.engine.runtime_change_journal import (
    RuntimeChangeDomain,
    RuntimeChangeJournal,
    RuntimeFieldKey,
    RuntimeFrameBarrier,
)


class _Probe(ComponentLifecycleMixin):
    def __init__(self, component_id: int = 1) -> None:
        self._component_id = component_id
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
        self.calls.append("fixed")

    def update(self, _delta_time: float) -> None:
        self.calls.append("update")

    def late_update(self, _delta_time: float) -> None:
        self.calls.append("late")


def test_transaction_coalesces_large_component_field_batch_once():
    journal = RuntimeChangeJournal()
    cursor = journal.create_cursor("values", start_at_current=False)

    with journal.transaction():
        for component_id in range(1000):
            journal.publish_component_field("Mover", component_id, "speed")
            journal.publish_component_field("Mover", component_id, "speed")

    changes = journal.consume(cursor)

    assert changes.revision == 1
    assert set(changes.changes) == {RuntimeChangeDomain.COMPONENT_FIELD}
    assert len(changes.for_domain(RuntimeChangeDomain.COMPONENT_FIELD).fields) == 1000
    profile = journal.profiler_snapshot()
    assert profile["flush_count"] == 1
    assert profile["coalesced_count"] >= 1999


def test_failed_transaction_publishes_no_partial_changes():
    journal = RuntimeChangeJournal()
    cursor = journal.create_cursor("rollback", start_at_current=False)

    with pytest.raises(RuntimeError, match="abort"):
        with journal.transaction():
            journal.publish(RuntimeChangeDomain.COMPONENT_STRUCTURE, stable_id=1)
            raise RuntimeError("abort")

    changes = journal.consume(cursor)
    assert changes.changed is False
    assert changes.revision == 0


def test_stable_cursor_reuses_immutable_empty_change_set_without_flushing():
    journal = RuntimeChangeJournal()
    cursor = journal.create_cursor("stable")

    first = journal.consume(cursor)
    second = journal.consume(cursor)

    assert first is second
    assert first.changed is False
    assert first.from_revision == first.revision == 0
    assert journal.profiler_snapshot()["flush_count"] == 0

    journal.publish(RuntimeChangeDomain.MATERIAL, stable_id="material")
    dirty = journal.consume(cursor)
    after_dirty = journal.consume(cursor)
    stable_again = journal.consume(cursor)

    assert dirty.changed is True
    assert after_dirty is stable_again
    assert after_dirty.changed is False
    assert after_dirty.from_revision == after_dirty.revision == dirty.revision
    assert journal.profiler_snapshot()["flush_count"] == 1


def test_consumers_advance_independently_over_the_same_revision_stream():
    journal = RuntimeChangeJournal()
    scheduler_cursor = journal.create_cursor("scheduler", start_at_current=False)
    snapshot_cursor = journal.create_cursor("snapshot", start_at_current=False)

    journal.publish(RuntimeChangeDomain.COMPONENT_ENABLED, stable_id=7)
    scheduler_changes = journal.consume(scheduler_cursor)
    snapshot_changes = journal.consume(snapshot_cursor)

    assert scheduler_changes.revision == snapshot_changes.revision == 1
    assert scheduler_changes.for_domain(
        RuntimeChangeDomain.COMPONENT_ENABLED
    ).stable_ids == frozenset({7})
    assert snapshot_changes.for_domain(
        RuntimeChangeDomain.COMPONENT_ENABLED
    ).stable_ids == frozenset({7})

    journal.publish(RuntimeChangeDomain.PREVIEW_SOURCE, stable_id="material")
    assert journal.consume(scheduler_cursor).revision == 2
    assert snapshot_cursor.revision == 1


def test_stale_cursor_requests_typed_full_resync_after_history_retirement():
    journal = RuntimeChangeJournal(history_limit=2)
    cursor = journal.create_cursor(
        "slow-consumer",
        domains=(RuntimeChangeDomain.COMPONENT_FIELD,),
        start_at_current=False,
    )
    for component_id in range(3):
        journal.publish_component_field("Mover", component_id, "speed")
        journal.flush()

    changes = journal.consume(cursor, flush=False)

    assert changes.full_resync is True
    assert changes.for_domain(RuntimeChangeDomain.COMPONENT_FIELD).broad is True
    assert changes.revision == 3


def test_value_revision_does_not_rebuild_runtime_phase_plan():
    journal = RuntimeChangeJournal()
    scheduler = RuntimeExecutionScheduler(name="value-test", change_journal=journal)
    probe = _Probe()
    scheduler.register_component(probe)
    plan = scheduler.prepare_frame()
    assert scheduler.profiler_snapshot()["plan_builds"] == 1

    notify_runtime_component_value_changed(probe, "speed")
    changes = scheduler.consume_runtime_changes(RuntimeFrameBarrier.UPDATE_SCRIPT)

    field_changes = changes.for_domain(RuntimeChangeDomain.COMPONENT_FIELD)
    assert field_changes.fields == frozenset(
        {RuntimeFieldKey(f"{_Probe.__module__}.{_Probe.__qualname__}", 1, "speed")}
    )
    assert scheduler.prepare_frame() is plan
    assert scheduler.profiler_snapshot()["plan_builds"] == 1


def test_serialized_field_setter_publishes_after_authoritative_storage_write():
    journal = RuntimeChangeJournal()
    scheduler = RuntimeExecutionScheduler(name="descriptor-value", change_journal=journal)
    cursor = journal.create_cursor(
        "descriptor-value",
        domains=(RuntimeChangeDomain.COMPONENT_FIELD,),
        start_at_current=False,
    )
    with candidate_component_registration_scope():
        component_type = type(
            "DescriptorValueProbe",
            (InxComponent,),
            {
                "__module__": "runtime_change_descriptor_probe",
                "__annotations__": {"speed": float},
                "speed": 1.0,
            },
        )
    component = component_type()
    component._registered_go_id = 41

    component.speed = 8.0
    changes = journal.consume(cursor)

    fields = changes.for_domain(RuntimeChangeDomain.COMPONENT_FIELD).fields
    assert fields == frozenset(
        {
            RuntimeFieldKey(
                component_type._type_guid_,
                component.component_id,
                "speed",
            )
        }
    )
    scheduler.clear()


def test_enabled_change_is_consumed_at_barrier_and_rebuilds_next_frame_only():
    journal = RuntimeChangeJournal()
    scheduler = RuntimeExecutionScheduler(name="barrier-test", change_journal=journal)
    probe = _Probe()
    scheduler.register_component(probe)
    frame = scheduler.begin_frame()

    frame.execute_phase("fixed_update", 0.02)
    probe._enabled = False
    notify_runtime_component_changed(probe)
    frame.execute_phase("update", 0.016)

    assert probe.calls == ["fixed"]
    assert frame.barrier_changes[RuntimeFrameBarrier.UPDATE_SCRIPT].affects(
        RuntimeChangeDomain.COMPONENT_ENABLED
    )
    assert probe in frame.phase_plan["late_update"]
    frame.close()

    assert scheduler.phase_plan("update") == ()
    assert scheduler.profiler_snapshot()["plan_builds"] == 2


def test_phase_transaction_coalesces_field_writes_until_the_next_barrier():
    journal = RuntimeChangeJournal()
    scheduler = RuntimeExecutionScheduler(name="phase-batch", change_journal=journal)

    class WritingProbe(_Probe):
        def update(self, _delta_time: float) -> None:
            for field_index in range(1000):
                notify_runtime_component_value_changed(self, f"field_{field_index}")

    probe = WritingProbe()
    scheduler.register_component(probe)
    frame = scheduler.begin_frame()
    assert journal.revision == 1

    frame.execute_phase("update", 0.016)
    assert journal.revision == 1

    frame.execute_phase("late_update", 0.016)
    changes = frame.barrier_changes[RuntimeFrameBarrier.LATE_SCRIPT]
    assert len(changes.for_domain(RuntimeChangeDomain.COMPONENT_FIELD).fields) == 1000
    assert journal.revision == 2
    assert journal.profiler_snapshot()["flush_count"] == 2
    frame.close()


def test_player_session_reuses_scheduler_journal_instead_of_creating_a_service():
    journal = RuntimeChangeJournal()
    scheduler = RuntimeExecutionScheduler(name="player", change_journal=journal)
    session = PlayerRuntimeSession(scheduler=scheduler)

    assert session.execution_scheduler is scheduler
    assert session.execution_scheduler.change_journal is journal


def test_native_frame_records_the_production_barrier_order_without_rebuilding_for_values():
    journal = RuntimeChangeJournal()
    scheduler = RuntimeExecutionScheduler(name="native-flow", change_journal=journal)
    probe = _Probe()
    scheduler.register_component(probe)

    scheduler.begin_native_frame()
    scheduler.execute_native_phase("fixed_update", 0.02)
    notify_runtime_component_value_changed(probe, "speed")
    scheduler.consume_native_barrier(RuntimeFrameBarrier.TRANSFORM_TO_PHYSICS)
    scheduler.consume_native_barrier(RuntimeFrameBarrier.PHYSICS_SIMULATION)
    scheduler.consume_native_barrier(RuntimeFrameBarrier.PHYSICS_TO_TRANSFORM)
    scheduler.consume_native_barrier(RuntimeFrameBarrier.TRANSFORM_RESOLVE)
    scheduler.execute_native_phase("update", 0.016)
    scheduler.execute_native_phase("late_update", 0.016)
    scheduler.consume_native_barrier(RuntimeFrameBarrier.FINAL_TRANSFORM_RESOLVE)
    scheduler.consume_native_barrier(RuntimeFrameBarrier.ANIMATION_TIMELINE)
    scheduler.consume_native_barrier(RuntimeFrameBarrier.RENDER_EXTRACTION)
    scheduler.consume_native_barrier(RuntimeFrameBarrier.RENDER_GRAPH)
    scheduler.consume_native_barrier(RuntimeFrameBarrier.SNAPSHOT_PUBLICATION)
    scheduler.consume_native_barrier(RuntimeFrameBarrier.PENDING_DESTROY)
    scheduler.end_native_frame()

    sequence = tuple(
        barrier for barrier, _changes in scheduler.last_completed_barrier_sequence()
    )
    assert sequence == (
        RuntimeFrameBarrier.SAFE_POINT,
        RuntimeFrameBarrier.FIXED_SCRIPT,
        RuntimeFrameBarrier.TRANSFORM_TO_PHYSICS,
        RuntimeFrameBarrier.PHYSICS_SIMULATION,
        RuntimeFrameBarrier.PHYSICS_TO_TRANSFORM,
        RuntimeFrameBarrier.TRANSFORM_RESOLVE,
        RuntimeFrameBarrier.UPDATE_SCRIPT,
        RuntimeFrameBarrier.LATE_SCRIPT,
        RuntimeFrameBarrier.FINAL_TRANSFORM_RESOLVE,
        RuntimeFrameBarrier.ANIMATION_TIMELINE,
        RuntimeFrameBarrier.RENDER_EXTRACTION,
        RuntimeFrameBarrier.RENDER_GRAPH,
        RuntimeFrameBarrier.SNAPSHOT_PUBLICATION,
        RuntimeFrameBarrier.PENDING_DESTROY,
        RuntimeFrameBarrier.RETIREMENT,
    )
    assert scheduler.profiler_snapshot()["plan_builds"] == 1
    assert scheduler.last_completed_frame_changes()[
        RuntimeFrameBarrier.TRANSFORM_TO_PHYSICS
    ].affects(RuntimeChangeDomain.COMPONENT_FIELD)


def test_native_barrier_without_an_open_frame_is_ignored():
    scheduler = RuntimeExecutionScheduler(
        name="closed-native-barrier",
        change_journal=RuntimeChangeJournal(),
    )

    assert (
        scheduler.consume_native_barrier(RuntimeFrameBarrier.RENDER_EXTRACTION)
        is None
    )
    assert scheduler.profiler_snapshot()["native_barriers_without_frame"] == 1
