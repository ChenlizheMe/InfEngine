"""Regression tests for the shared runtime phase-plan service."""

from __future__ import annotations

from Infernux.components._component_lifecycle import (
    ComponentLifecycleMixin,
    RuntimeExecutionScheduler,
)
from Infernux.engine.runtime_dispatch import publish_runtime_dispatch_epoch
from Infernux.components.component import InxComponent
from Infernux.engine.runtime_dispatch import build_type_dispatch_descriptor


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


class _Owner:
    def __init__(self, active: bool = True) -> None:
        self.active_in_hierarchy = active


def test_framework_lifecycle_noops_do_not_schedule_declarative_components():
    class DeclarativeComponent(InxComponent):
        pass

    class UpdatingComponent(InxComponent):
        def update(self, _delta_time: float) -> None:
            pass

    assert build_type_dispatch_descriptor(DeclarativeComponent).phase_presence == (
        False,
        False,
        False,
    )
    assert build_type_dispatch_descriptor(UpdatingComponent).phase_presence == (
        True,
        False,
        False,
    )


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


def test_phase_plan_snapshot_is_safe_while_native_frame_is_active():
    scheduler = RuntimeExecutionScheduler()
    component = _ScheduledProbe(1)
    scheduler.register_component(component)
    scheduler.begin_native_frame()
    try:
        snapshot = scheduler.phase_plan_snapshot()
    finally:
        scheduler.end_native_frame()

    assert snapshot["update"] == (component,)


def test_native_bridge_publishes_phase_plan_summary_on_structural_rebuild():
    class _NativeManager:
        def __init__(self) -> None:
            self.available = False
            self.plans = []

        def set_runtime_lifecycle_work_available(self, available):
            self.available = bool(available)

        def set_runtime_lifecycle_plan(self, revision, fixed_count, update_count, late_count):
            self.plans.append(
                (int(revision), int(fixed_count), int(update_count), int(late_count))
            )

    manager = _NativeManager()

    scheduler = RuntimeExecutionScheduler(name="native-plan", native_bridge=True)
    scheduler.bind_native_bridge(manager)
    scheduler.register_component(_ScheduledProbe(1))

    scheduler.prepare_frame()

    assert manager.available is True
    assert manager.plans[-1][1:] == (1, 1, 1)
    scheduler.clear()
    assert manager.available is False
    assert manager.plans[-1][1:] == (0, 0, 0)


def test_scene_replacement_retirement_cannot_remove_same_id_new_component():
    scheduler = RuntimeExecutionScheduler(name="scene-replacement")
    old_scene_component = _ScheduledProbe(17)
    new_scene_component = _ScheduledProbe(17)

    # Runtime scene transactions publish the replacement graph before the
    # retained old graph is finalized. Both mirrors may have the same authored
    # component ID and their first native binding generation.
    scheduler.register_component(old_scene_component)
    scheduler.register_component(new_scene_component)
    scheduler.unregister_component(old_scene_component)

    assert scheduler.phase_plan("update") == (new_scene_component,)
    scheduler.execute_phase("update", 0.25)
    assert old_scene_component.calls == []
    assert new_scene_component.calls == ["update:0.25"]


def test_scene_membership_refresh_recovers_missed_incremental_notification(monkeypatch):
    scheduler = RuntimeExecutionScheduler(name="scene-publication")
    component = _ScheduledProbe(31)

    from Infernux.components.component import InxComponent

    monkeypatch.setattr(
        InxComponent,
        "_active_instances",
        {31: [component]},
    )
    scheduler.refresh_scene_membership()

    assert scheduler.phase_plan("update") == (component,)


def test_scene_registry_rebuild_keeps_persistent_components_in_runtime_plan(monkeypatch):
    from Infernux.components.component import InxComponent
    from Infernux.engine.runtime_scene_transaction import SceneDocumentTransaction
    import Infernux.lib as native_lib

    class _SceneObject:
        def __init__(self, object_id, component):
            self.id = object_id
            self._component = component

        def get_py_components(self):
            return [self._component]

    class _Scene:
        def __init__(self, *objects):
            self._objects = objects

        def get_all_objects(self):
            return self._objects

    class _RegistryProbe(_ScheduledProbe):
        def _set_game_object(self, game_object):
            self._game_object = game_object
            InxComponent._active_instances.setdefault(game_object.id, []).append(self)

        def _refresh_native_handle(self):
            return None

    active_component = _RegistryProbe(41)
    persistent_component = _RegistryProbe(42)
    active_scene = _Scene(_SceneObject(401, active_component))
    persistent_scene = _Scene(_SceneObject(402, persistent_component))

    class _SceneManager:
        @staticmethod
        def instance():
            return type(
                "_Manager",
                (),
                {"get_runtime_persistent_scene": lambda self: persistent_scene},
            )()

    monkeypatch.setattr(native_lib, "SceneManager", _SceneManager)
    monkeypatch.setattr(InxComponent, "_active_instances", {999: [_ScheduledProbe(99)]})

    transaction = SceneDocumentTransaction(active_scene, document={})
    transaction._rebuild_python_registries()

    scheduler = RuntimeExecutionScheduler(name="persistent-scene-publication")
    scheduler.refresh_scene_membership()

    assert scheduler.phase_plan("update") == (active_component, persistent_component)


def test_scene_registry_reconcile_only_restores_persistent_components(monkeypatch):
    from Infernux.components.component import InxComponent
    from Infernux.engine.runtime_scene_transaction import SceneDocumentTransaction
    import Infernux.lib as native_lib

    class _SceneObject:
        def __init__(self, object_id, component):
            self.id = object_id
            self._component = component

        def get_py_components(self):
            return [self._component]

    class _Scene:
        def __init__(self, *objects):
            self._objects = objects

        def get_all_objects(self):
            return self._objects

    class _RegistryProbe(_ScheduledProbe):
        def __init__(self, component_id):
            super().__init__(component_id)
            self.bind_count = 0

        def _set_game_object(self, game_object):
            self.bind_count += 1
            InxComponent._active_instances.setdefault(game_object.id, []).append(self)

        def _refresh_native_handle(self):
            return None

    active_component = _RegistryProbe(51)
    persistent_component = _RegistryProbe(52)
    active_scene = _Scene(_SceneObject(501, active_component))
    persistent_scene = _Scene(_SceneObject(502, persistent_component))

    class _SceneManager:
        @staticmethod
        def instance():
            return type(
                "_Manager",
                (),
                {"get_runtime_persistent_scene": lambda self: persistent_scene},
            )()

    monkeypatch.setattr(native_lib, "SceneManager", _SceneManager)
    monkeypatch.setattr(InxComponent, "_active_instances", {501: [active_component]})

    transaction = SceneDocumentTransaction(active_scene, document={})
    transaction._reconcile_persistent_python_registries()

    assert active_component.bind_count == 0
    assert persistent_component.bind_count == 1
    assert InxComponent._active_instances == {
        501: [active_component],
        502: [persistent_component],
    }


def test_runtime_scheduler_reuses_immutable_execution_snapshot_between_frames():
    scheduler = RuntimeExecutionScheduler(name="snapshot-cache")
    probe = _ScheduledProbe(1)
    scheduler.register_component(probe)

    first = scheduler.begin_frame()
    first_plan = first.phase_plan
    first_components = first.component_snapshots
    first.close()

    second = scheduler.begin_frame()
    try:
        assert second.phase_plan is first_plan
        assert second.component_snapshots is first_components
    finally:
        second.close()

    counters = scheduler.profiler_snapshot()
    assert counters["execution_snapshot_builds"] == 1
    assert counters["execution_snapshot_hits"] == 1


def test_runtime_scheduler_does_not_rescan_dispatch_types_in_steady_state(monkeypatch):
    import Infernux.engine.runtime_dispatch as runtime_dispatch

    calls = 0
    original = runtime_dispatch.ensure_runtime_dispatch_types

    def counted(component_types):
        nonlocal calls
        calls += 1
        return original(component_types)

    monkeypatch.setattr(runtime_dispatch, "ensure_runtime_dispatch_types", counted)
    scheduler = RuntimeExecutionScheduler(name="steady-state")
    scheduler.register_component(_ScheduledProbe(1))

    scheduler.prepare_frame()
    scheduler.prepare_frame()
    scheduler.prepare_frame()

    assert calls == 1


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


def test_starting_coroutine_invalidates_cached_phase_membership(monkeypatch):
    scheduler = RuntimeExecutionScheduler(name="coroutine-phase-transition")
    probe = _ScheduledProbe(1)
    scheduler.register_component(probe)
    monkeypatch.setattr(
        RuntimeExecutionScheduler,
        "_has_phase",
        staticmethod(lambda _component, phase: phase == "update"),
    )

    assert scheduler.phase_plan("late_update") == ()

    class _ActiveCoroutineScheduler:
        count = 1

    probe._runtime_coroutine_scheduler = _ActiveCoroutineScheduler()
    RuntimeExecutionScheduler._notify_component_runtime_work(probe)

    assert scheduler.phase_plan("late_update") == (probe,)
    assert scheduler.profiler_snapshot()["coroutine_plan_invalidations"] == 1


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


def test_inactive_owner_is_excluded_but_reactivation_rebuilds_the_plan():
    scheduler = RuntimeExecutionScheduler()
    active_owner = _Owner(active=True)
    inactive_owner = _Owner(active=False)
    probe = _ScheduledProbe(1)
    probe._game_object = active_owner
    scheduler.register_component(probe)

    assert scheduler.phase_plan("update") == (probe,)

    # A present registration can be a native rebind.  It must refresh the
    # owner mirror instead of retaining the active state from the old owner.
    probe._game_object = inactive_owner
    scheduler.register_component(probe)
    assert scheduler.phase_plan("update") == ()

    inactive_owner.active_in_hierarchy = True
    probe._call_on_enable()
    assert scheduler.phase_plan("update") == (probe,)


def test_missing_game_object_on_lightweight_double_is_active():
    scheduler = RuntimeExecutionScheduler()
    probe = _ScheduledProbe(1)
    scheduler.register_component(probe)

    assert scheduler.phase_plan("update") == (probe,)


def test_unbound_component_is_excluded_from_the_plan():
    scheduler = RuntimeExecutionScheduler()
    probe = _ScheduledProbe(1)
    probe._game_object = None
    scheduler.register_component(probe)

    assert scheduler.phase_plan("update") == ()


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

    frame.execute_phase("fixed_update", 0.02)
    frame.execute_phase("update", 0.016)
    frame.execute_phase("late_update", 0.016)
    frame.close()

    def new_fixed_update(self, _delta_time):
        self.calls.append("fixed-new")

    def new_update(self, _delta_time):
        self.calls.append("update-new")

    def new_late_update(self, _delta_time):
        self.calls.append("late-new")

    BodyProbe.fixed_update = new_fixed_update
    BodyProbe.update = new_update
    BodyProbe.late_update = new_late_update
    publication = publish_runtime_dispatch_epoch((BodyProbe,))
    publication.commit()

    assert probe.calls == ["fixed-old", "update-old", "late-old"]
    try:
        next_frame = scheduler.begin_frame()
        try:
            next_frame.execute_phase("fixed_update", 0.02)
            next_frame.execute_phase("update", 0.016)
            next_frame.execute_phase("late_update", 0.016)
        finally:
            next_frame.close()
    finally:
        publication.rollback()
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


def test_native_bridge_reuses_one_snapshot_across_multiple_fixed_steps():
    scheduler = RuntimeExecutionScheduler(name="editor")
    probe = _ScheduledProbe(1)
    scheduler.register_component(probe)

    scheduler.begin_native_frame()
    try:
        scheduler.execute_native_phase("fixed_update", 0.02)
        scheduler.execute_native_phase("fixed_update", 0.02)
        scheduler.execute_native_phase("update", 0.016)
        scheduler.execute_native_phase("late_update", 0.016)
    finally:
        scheduler.end_native_frame()

    assert probe.calls == [
        "fixed:0.02",
        "fixed:0.02",
        "update:0.016",
        "late:0.016",
    ]
    counters = scheduler.profiler_snapshot()
    assert counters["native_frame_begins"] == 1
    assert counters["native_frame_ends"] == 1
    assert counters["plan_builds"] == 1
    assert counters["frame_begins"] == 1


def test_native_bridge_editor_update_filters_non_edit_mode_components():
    scheduler = RuntimeExecutionScheduler(name="editor")
    regular = _ScheduledProbe(1)
    preview = _ScheduledProbe(2)
    preview._execute_in_edit_mode = True
    scheduler.register_component(regular)
    scheduler.register_component(preview)

    scheduler.begin_native_frame()
    try:
        scheduler.execute_native_editor_update(0.016)
    finally:
        scheduler.end_native_frame()

    assert regular.calls == []
    assert preview.calls == ["update:0.016"]


def test_native_bridge_preserves_enabled_transition_between_phases():
    scheduler = RuntimeExecutionScheduler(name="player")

    class _DisablingProbe(_ScheduledProbe):
        def fixed_update(self, delta_time: float) -> None:
            self.calls.append(f"fixed:{delta_time}")
            self._enabled = False

    probe = _DisablingProbe(1)
    scheduler.register_component(probe)

    scheduler.begin_native_frame()
    try:
        scheduler.execute_native_phase("fixed_update", 0.02)
        scheduler.execute_native_phase("update", 0.016)
        scheduler.execute_native_phase("late_update", 0.016)
    finally:
        scheduler.end_native_frame()

    assert probe.calls == ["fixed:0.02"]


def test_owner_deactivation_during_fixed_update_skips_following_phases():
    scheduler = RuntimeExecutionScheduler(name="player")
    owner = _Owner(active=True)

    class _OwnerDeactivatingProbe(_ScheduledProbe):
        def fixed_update(self, delta_time: float) -> None:
            self.calls.append(f"fixed:{delta_time}")
            owner.active_in_hierarchy = False
            self._call_on_disable()

    probe = _OwnerDeactivatingProbe(1)
    probe._game_object = owner
    scheduler.register_component(probe)

    scheduler.begin_native_frame()
    try:
        scheduler.execute_native_phase("fixed_update", 0.02)
        scheduler.execute_native_phase("update", 0.016)
        scheduler.execute_native_phase("late_update", 0.016)
    finally:
        scheduler.end_native_frame()

    assert probe.calls == ["fixed:0.02"]
    assert probe._enabled is True
    assert scheduler.profiler_snapshot()["phase_skips"] == 2


def test_disabled_component_keeps_coroutines_running_in_all_phases():
    class _CoroutineRecorder:
        count = 1

        def __init__(self) -> None:
            self.ticks = []

        def tick_fixed_update(self, delta_time: float, *, epoch=None) -> None:
            self.ticks.append(("fixed", delta_time, epoch))

        def tick_update(self, delta_time: float, *, epoch=None) -> None:
            self.ticks.append(("update", delta_time, epoch))

        def tick_late_update(self, delta_time: float, *, epoch=None) -> None:
            self.ticks.append(("late", delta_time, epoch))

    scheduler = RuntimeExecutionScheduler(name="player")
    probe = _ScheduledProbe(1)
    probe._enabled = False
    coroutine_scheduler = _CoroutineRecorder()
    probe._runtime_coroutine_scheduler = coroutine_scheduler
    scheduler.register_component(probe)

    scheduler.begin_native_frame()
    try:
        scheduler.execute_native_phase("fixed_update", 0.02)
        scheduler.execute_native_phase("update", 0.016)
        scheduler.execute_native_phase("late_update", 0.016)
    finally:
        scheduler.end_native_frame()

    assert probe.calls == []
    assert [tick[:2] for tick in coroutine_scheduler.ticks] == [
        ("fixed", 0.02),
        ("update", 0.016),
        ("late", 0.016),
    ]
    assert len({id(tick[2]) for tick in coroutine_scheduler.ticks}) == 1
