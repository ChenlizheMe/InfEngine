"""R3-B4 descriptor retirement and owner transaction tests."""

from __future__ import annotations

import pytest

from Infernux.components import InxComponent
from Infernux.coroutine import Coroutine
from Infernux.engine.runtime_dispatch import (
    ReloadableCallbackRegistry,
    current_runtime_epoch,
    has_runtime_phase,
    publish_runtime_dispatch_epoch,
    runtime_descriptor_diagnostic,
)


class _RetirementProbe(InxComponent):
    def update(self, _delta_time):
        self.calls.append("old")

    def handle(self, value):
        self.calls.append(value)


def _publish_initial(component_type):
    publication = publish_runtime_dispatch_epoch((component_type,))
    publication.commit()
    return publication


def test_retirement_is_staged_until_commit_and_old_epoch_remains_usable():
    owner = _RetirementProbe()
    owner.calls = []
    initial = _publish_initial(_RetirementProbe)
    before = current_runtime_epoch()
    callback_registry = ReloadableCallbackRegistry()
    callback_registry.add_listener(owner.handle)
    coroutine = Coroutine((value for value in ()), owner, creation_epoch=before)

    retirement = publish_runtime_dispatch_epoch(
        (),
        retired_types=(_RetirementProbe,),
        defer_commit=True,
    )
    try:
        assert current_runtime_epoch() is before
        assert retirement.after.descriptor_for(_RetirementProbe) is None
        assert before.descriptor_for(_RetirementProbe) is not None
        assert coroutine.creation_epoch is before

        retirement.commit()
        assert current_runtime_epoch() is retirement.after
        assert current_runtime_epoch().descriptor_for(_RetirementProbe) is None
        assert runtime_descriptor_diagnostic(_RetirementProbe)["status"] == "retired"
        result = callback_registry.invoke(7)
        assert result[0].status == "method_missing"
        assert owner.calls == []
    finally:
        retirement.rollback()
        initial.rollback()


def test_failed_or_cancelled_retirement_restores_current_descriptor():
    initial = _publish_initial(_RetirementProbe)
    before = current_runtime_epoch()
    try:
        staged = publish_runtime_dispatch_epoch(
            (),
            retired_types=(_RetirementProbe,),
            defer_commit=True,
        )
        staged.rollback()
        assert current_runtime_epoch() is before
        assert current_runtime_epoch().descriptor_for(_RetirementProbe) is not None
        assert _RetirementProbe not in current_runtime_epoch().retired_types
    finally:
        initial.rollback()


def test_deferred_retirement_does_not_notify_scheduler_before_commit():
    from Infernux.components._component_lifecycle import RuntimeExecutionScheduler

    class _SchedulerSpy:
        def __init__(self):
            self.calls = []

        def mark_type_body_reload(self, component_type, *, phase_presence_changed):
            self.calls.append((component_type, phase_presence_changed))

    spy = _SchedulerSpy()
    RuntimeExecutionScheduler._live_schedulers.add(spy)
    initial = _publish_initial(_RetirementProbe)
    spy.calls.clear()
    try:
        staged = publish_runtime_dispatch_epoch(
            (),
            retired_types=(_RetirementProbe,),
            defer_commit=True,
        )
        assert spy.calls == []
        assert _RetirementProbe not in current_runtime_epoch().retired_types
        staged.rollback()
        assert spy.calls == []

        committed = publish_runtime_dispatch_epoch(
            (),
            retired_types=(_RetirementProbe,),
            defer_commit=True,
        )
        committed.commit()
        assert spy.calls == [(_RetirementProbe, True)]
        assert _RetirementProbe in current_runtime_epoch().retired_types
        committed.rollback()
        assert spy.calls == [(_RetirementProbe, True), (_RetirementProbe, True)]
    finally:
        initial.rollback()


def test_retirement_cannot_cross_an_active_frame_safe_point():
    from Infernux.components._component_lifecycle import RuntimeExecutionScheduler

    owner = _RetirementProbe()
    owner.calls = []
    initial = _publish_initial(_RetirementProbe)
    scheduler = RuntimeExecutionScheduler()
    scheduler.register_component(owner)
    frame = scheduler.begin_frame()
    before = current_runtime_epoch()
    try:
        with pytest.raises(RuntimeError, match="safe point"):
            publish_runtime_dispatch_epoch(
                (),
                retired_types=(_RetirementProbe,),
                defer_commit=True,
            )
        assert current_runtime_epoch() is before
        assert frame.epoch is before
    finally:
        frame.close()
        initial.rollback()


def test_retired_callback_reports_missing_without_executing_old_body():
    owner = _RetirementProbe()
    owner.calls = []
    initial = _publish_initial(_RetirementProbe)
    registry = ReloadableCallbackRegistry()
    registry.add_listener(owner.handle)
    retirement = publish_runtime_dispatch_epoch(
        (),
        retired_types=(_RetirementProbe,),
    )
    try:
        result = registry.invoke("unexpected")
        assert result[0].status == "method_missing"
        assert owner.calls == []
    finally:
        retirement.rollback()
        initial.rollback()


def test_script_move_publishes_new_type_and_retires_old_type_as_one_epoch():
    from Infernux.components.registry import (
        publish_component_script_types_batch,
        restore_component_registry_state,
        snapshot_component_registry_state,
        register_component_type,
    )

    class OldMoveType(InxComponent):
        pass

    class NewMoveType(InxComponent):
        pass

    old_path = "C:/runtime-retirement/OldMoveType.py"
    new_path = "C:/runtime-retirement/NewMoveType.py"
    registry_before = snapshot_component_registry_state()
    register_component_type(OldMoveType, script_path=old_path)
    initial = _publish_initial(OldMoveType)
    try:
        retired = publish_component_script_types_batch(
            ((new_path, (NewMoveType,)),),
            remove_paths=(old_path,),
        )
        assert retired == (OldMoveType,)
        staged = publish_runtime_dispatch_epoch(
            (NewMoveType,),
            retired_types=retired,
            defer_commit=True,
        )
        assert current_runtime_epoch().descriptor_for(OldMoveType) is not None
        staged.commit()
        assert current_runtime_epoch().descriptor_for(OldMoveType) is None
        assert current_runtime_epoch().descriptor_for(NewMoveType) is not None
        staged.rollback()
        assert current_runtime_epoch().descriptor_for(OldMoveType) is not None
    finally:
        initial.rollback()
        restore_component_registry_state(registry_before)


def test_script_delete_transaction_retires_only_at_commit():
    from Infernux.engine.play_mode import ScriptDeleteBatch

    class _Manager:
        @staticmethod
        def _get_active_scene_for_script_reload():
            return None

    initial = _publish_initial(_RetirementProbe)
    before = current_runtime_epoch()
    batch = ScriptDeleteBatch(
        _Manager(),
        (),
        retired_types=(_RetirementProbe,),
    )
    try:
        batch.commit()
        assert current_runtime_epoch().descriptor_for(_RetirementProbe) is None
        batch.rollback()
        assert current_runtime_epoch() is before
        assert current_runtime_epoch().descriptor_for(_RetirementProbe) is not None
    finally:
        initial.rollback()


def test_retired_type_phase_fallback_cannot_reenter_scheduler_plan():
    from Infernux.components._component_lifecycle import (
        ComponentLifecycleMixin,
        RuntimeExecutionScheduler,
    )

    class _ScheduledRetirementProbe(ComponentLifecycleMixin):
        def __init__(self):
            self._component_id = 901
            self._execution_order = 0
            self._native_generation = 1
            self._enabled = True
            self._is_destroyed = False

        def update(self, _delta_time):
            return None

    owner = _ScheduledRetirementProbe()
    initial = _publish_initial(_ScheduledRetirementProbe)
    scheduler = RuntimeExecutionScheduler()
    scheduler.register_component(owner)
    try:
        assert has_runtime_phase(_ScheduledRetirementProbe, "update") is True
        assert scheduler.phase_plan("update") == (owner,)

        retirement = publish_runtime_dispatch_epoch(
            (),
            retired_types=(_ScheduledRetirementProbe,),
            defer_commit=True,
        )
        retirement.commit()
        assert current_runtime_epoch().descriptor_for(_ScheduledRetirementProbe) is None
        assert has_runtime_phase(_ScheduledRetirementProbe, "update") is False
        assert scheduler.phase_plan("update") == ()

        retirement.rollback()
        assert current_runtime_epoch().descriptor_for(_ScheduledRetirementProbe) is not None
        assert has_runtime_phase(_ScheduledRetirementProbe, "update") is True
        assert scheduler.phase_plan("update") == (owner,)
    finally:
        initial.rollback()
