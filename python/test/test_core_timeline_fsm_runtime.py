"""Unit tests for TimelineFSMRuntime — the renderer-agnostic Timeline FSM player.

Exercises state entry, playback advance, looping vs play-once, exit-time gating,
parameter/trigger-driven transitions and timeline-end auto-advance. Runs with a
``transform=None`` target so it tests pure state-machine logic (no renderer).
"""
from __future__ import annotations

import uuid

import pytest

import Infernux.core.timeline_fsm_runtime as timeline_runtime_module
from Infernux.core.anim_state_machine import (
    AnimCondition,
    AnimParameter,
    AnimState,
    AnimStateMachine,
    AnimTransition,
)
from Infernux.core.animation_timeline import AnimationTimeline, TimelineKeyframe
from Infernux.core.timeline_fsm_runtime import TimelineFSMRuntime
from Infernux.graph import TypeRef, ValueType


_TIMELINE_PATHS = {}


@pytest.fixture(autouse=True)
def _asset_database(monkeypatch):
    _TIMELINE_PATHS.clear()

    class Database:
        @staticmethod
        def get_path_from_guid(guid):
            return _TIMELINE_PATHS.get(guid, "")

    monkeypatch.setattr(timeline_runtime_module, "_get_asset_database", lambda: Database())


# ── helpers ─────────────────────────────────────────────────────────────────

def _timeline_file(tmp_path, name, duration=1.0):
    tl = AnimationTimeline(duration=duration, keyframes=[
        TimelineKeyframe(time=0.0, position=[0, 0, 0]),
        TimelineKeyframe(time=duration, position=[1, 0, 0]),
    ])
    path = str(tmp_path / f"{name}.animtimeline")
    tl.save(path)
    guid = uuid.uuid5(uuid.NAMESPACE_URL, path).hex
    _TIMELINE_PATHS[guid] = path
    return guid, path


def _state(tmp_path, name, *, loop=True, exit_time=1.0, dur=1.0, trans=None):
    s = AnimState(name=name, kind="timeline", loop=loop, exit_time_normalized=exit_time)
    s.timeline_guid, s.timeline_path = _timeline_file(tmp_path, name, dur)
    for target, cond in (trans or []):
        conditions = []
        if cond:
            parts = cond.split()
            parameter_name = parts[0]
            operator = parts[1] if len(parts) == 3 else "=="
            threshold = float(parts[2]) if len(parts) == 3 else 1.0
            conditions.append(
                AnimCondition(
                    parameter_id=f"parameter:{parameter_name}",
                    operator=operator,
                    threshold=threshold,
                )
            )
        s.transitions.append(AnimTransition(target_state=target, conditions=conditions))
    return s


def _fsm(states, default=None):
    fsm = AnimStateMachine(mode="timeline")
    fsm.states = states
    fsm.default_state = default or (states[0].name if states else "")
    parameter_ids = {
        condition.parameter_id
        for state in states
        for transition in state.transitions
        for condition in transition.conditions
    }
    fsm.parameters = [
        AnimParameter(stable_id=parameter_id, name=parameter_id.removeprefix("parameter:"))
        for parameter_id in sorted(parameter_ids)
    ]
    return fsm


# ── setup / parameters ──────────────────────────────────────────────────────

def test_runtime_no_fsm_play_false():
    rt = TimelineFSMRuntime()
    assert rt.play() is False


def test_runtime_set_fsm_none_resets():
    rt = TimelineFSMRuntime()
    rt.set_fsm(None)
    assert rt.fsm is None
    assert rt.is_playing is False
    assert rt.current_state == ""


def test_runtime_initializes_parameter_defaults(tmp_path):
    fsm = _fsm([_state(tmp_path, "A")])
    from Infernux.core.anim_state_machine import AnimParameter
    fsm.parameters = [
        AnimParameter(name="spd", value_type=TypeRef(ValueType.F32), default=2.0),
        AnimParameter(name="flag", value_type=TypeRef(ValueType.BOOL), default=True),
        AnimParameter(name="cnt", value_type=TypeRef(ValueType.I32), default=3),
    ]
    rt = TimelineFSMRuntime()
    rt.set_fsm(fsm)
    assert rt.get_float("spd") == 2.0
    assert rt.get_bool("flag") is True
    assert rt.get_int("cnt") == 3


def test_runtime_parameter_setters(tmp_path):
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A")]))
    rt.set_float("x", 1.5)
    rt.set_int("y", 4)
    rt.set_bool("z", True)
    assert rt.get_float("x") == 1.5
    assert rt.get_int("y") == 4
    assert rt.get_bool("z") is True


def test_runtime_get_parameter_default():
    rt = TimelineFSMRuntime()
    assert rt.get_parameter("missing", "fallback") == "fallback"


def test_runtime_set_trigger():
    rt = TimelineFSMRuntime()
    rt.set_trigger("fire")
    assert rt.get_bool("fire") is True


# ── play / state entry ──────────────────────────────────────────────────────

def test_runtime_play_default_state(tmp_path):
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A"), _state(tmp_path, "B")]))
    assert rt.play() is True
    assert rt.current_state == "A"
    assert rt.is_playing is True


def test_runtime_play_named_state(tmp_path):
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A"), _state(tmp_path, "B")]))
    assert rt.play("B") is True
    assert rt.current_state == "B"


def test_runtime_play_unknown_state_false(tmp_path):
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A")]))
    assert rt.play("ghost") is False


def test_runtime_rejects_timeline_state_without_guid(tmp_path):
    state = _state(tmp_path, "A")
    state.timeline_guid = ""
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([state]))

    with pytest.raises(ValueError, match="has no asset GUID"):
        rt.play()


def test_runtime_rejects_unregistered_timeline_guid(tmp_path):
    state = _state(tmp_path, "A")
    state.timeline_guid = "missing-guid"
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([state]))

    with pytest.raises(FileNotFoundError, match="missing-guid"):
        rt.play()


def test_runtime_play_no_default_false(tmp_path):
    fsm = _fsm([_state(tmp_path, "A")])
    fsm.default_state = ""
    rt = TimelineFSMRuntime()
    rt.set_fsm(fsm)
    assert rt.play() is False


def test_runtime_stop(tmp_path):
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A")]))
    rt.play()
    rt.stop()
    assert rt.is_playing is False


# ── playback advance ────────────────────────────────────────────────────────

def test_runtime_normalized_time_starts_zero(tmp_path):
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A", dur=2.0)]))
    rt.play()
    assert rt.normalized_time == pytest.approx(0.0)


def test_runtime_advance_increases_normalized_time(tmp_path):
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A", dur=2.0)]))
    rt.play()
    rt.update(1.0, transform=None)
    assert rt.normalized_time == pytest.approx(0.5)


def test_runtime_loop_wraps(tmp_path):
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A", loop=True, dur=1.0)]))
    rt.play()
    rt.update(1.5, transform=None)
    assert rt.is_playing is True
    assert rt.normalized_time == pytest.approx(0.5, abs=1e-6)


def test_runtime_non_loop_stops_at_end(tmp_path):
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A", loop=False, dur=1.0)]))
    rt.play()
    rt.update(1.5, transform=None)
    assert rt.is_playing is False
    assert rt.normalized_time == pytest.approx(1.0)


def test_runtime_non_loop_clamps_normalized(tmp_path):
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A", loop=False, dur=1.0)]))
    rt.play()
    rt.update(5.0, transform=None)
    assert rt.normalized_time <= 1.0


def test_runtime_playback_speed(tmp_path):
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A", dur=2.0)]))
    rt.play()
    rt.playback_speed = 2.0
    rt.update(0.5, transform=None)
    assert rt.normalized_time == pytest.approx(0.5)  # 0.5s * 2x / 2.0s dur


def test_runtime_update_no_transform_safe(tmp_path):
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A")]))
    rt.play()
    rt.update(0.1, transform=None)  # must not raise


def test_first_native_handle_does_not_compare_against_none(tmp_path):
    class StrictHandle:
        def __eq__(self, other):
            if other is None:
                raise TypeError("native handle cannot compare with None")
            return self is other

    class Transform:
        def __init__(self):
            self.handle = StrictHandle()
            self.calls = []
            self.position = self.local_position = type("V", (), {"x": 0.0, "y": 0.0, "z": 0.0})()
            self.euler_angles = self.local_euler_angles = self.position
            self.local_scale = type("V", (), {"x": 1.0, "y": 1.0, "z": 1.0})()

        def set_local_trs(self, *values):
            self.calls.append(values)

    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A")]))
    transform = Transform()

    assert rt.play(transform=transform)
    rt.update(0.1, transform=transform)

    assert transform.calls


def test_transform_contract_failure_propagates(tmp_path):
    class InvalidTransform:
        handle = object()

    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A")]))

    with pytest.raises(AttributeError):
        rt.play(transform=InvalidTransform())


def test_runtime_update_without_play_is_noop(tmp_path):
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A")]))
    rt.update(1.0, transform=None)
    assert rt.current_state == ""


# ── transitions ─────────────────────────────────────────────────────────────

def test_runtime_condition_transition_fires(tmp_path):
    a = _state(tmp_path, "A", loop=True, exit_time=0.0, trans=[("B", "go")])
    b = _state(tmp_path, "B")
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([a, b]))
    rt.play()
    rt.set_bool("go", True)
    rt.update(0.1, transform=None)
    assert rt.current_state == "B"


def test_runtime_condition_blocked_until_exit_time(tmp_path):
    a = _state(tmp_path, "A", loop=True, exit_time=1.0, dur=1.0, trans=[("B", "go")])
    b = _state(tmp_path, "B")
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([a, b]))
    rt.play()
    rt.set_bool("go", True)
    rt.update(0.2, transform=None)  # progress 0.2 < exit 1.0
    assert rt.current_state == "A"


def test_runtime_trigger_consumed_after_transition(tmp_path):
    a = _state(tmp_path, "A", loop=True, exit_time=0.0, trans=[("B", "fire")])
    b = _state(tmp_path, "B")
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([a, b]))
    rt.play()
    rt.set_trigger("fire")
    rt.update(0.1, transform=None)
    assert rt.current_state == "B"
    assert rt.get_bool("fire") is False


def test_runtime_timeline_end_auto_advance(tmp_path):
    # Empty condition + non-looping → advance when the timeline finishes.
    a = _state(tmp_path, "A", loop=False, exit_time=1.0, dur=1.0, trans=[("B", "")])
    b = _state(tmp_path, "B")
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([a, b]))
    rt.play()
    rt.update(1.1, transform=None)
    assert rt.current_state == "B"


def test_runtime_no_transition_when_condition_false(tmp_path):
    a = _state(tmp_path, "A", loop=True, exit_time=0.0, trans=[("B", "go")])
    b = _state(tmp_path, "B")
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([a, b]))
    rt.play()
    rt.update(0.1, transform=None)  # go stays default 0/False
    assert rt.current_state == "A"


def test_runtime_float_condition_transition(tmp_path):
    a = _state(tmp_path, "A", loop=True, exit_time=0.0, trans=[("B", "speed > 0.5")])
    b = _state(tmp_path, "B")
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([a, b]))
    rt.play()
    rt.set_float("speed", 1.0)
    rt.update(0.1, transform=None)
    assert rt.current_state == "B"


def test_runtime_re_enter_same_state_is_noop(tmp_path):
    rt = TimelineFSMRuntime()
    rt.set_fsm(_fsm([_state(tmp_path, "A")]))
    rt.play("A")
    rt.update(0.3, transform=None)
    elapsed_before = rt.normalized_time
    rt.play("A")  # restart_same_clip defaults False → no-op
    assert rt.normalized_time == pytest.approx(elapsed_before, abs=1e-6)
