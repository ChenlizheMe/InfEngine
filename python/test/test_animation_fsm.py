"""Animation FSM logic tests (pure Python — no GPU sampling needed).

Regression coverage for the animation-audit fixes:
- trigger consumption uses identifier boundaries (not substring matching)
- AnimTransition.duration drives the cross-fade for that transition
- non-looping clips report loop=False to the native pose submission
- empty-condition ("clip finished") transitions
"""
from __future__ import annotations

import pytest

from Infernux.core.anim_state_machine import (
    AnimCondition,
    AnimParameter,
    AnimState,
    AnimStateMachine,
    AnimTransition,
)
from Infernux.graph import TypeRef, ValueType
from Infernux.components.skeletal_animator import SkeletalAnimator
from Infernux.components.spirit_animator import SpiritAnimator


def _make_animator() -> SkeletalAnimator:
    """Bare SkeletalAnimator with a hand-built FSM (no renderer / no scene)."""
    anim = SkeletalAnimator()
    anim._parameters = {}
    anim._duration_cache = {}
    anim._clip_cache = {}
    return anim


def _condition(parameter: AnimParameter, operator: str = "==", threshold: float = 1.0):
    return AnimCondition(
        parameter_id=parameter.stable_id,
        operator=operator,
        threshold=threshold,
    )


class _FakeClip:
    def __init__(self, take_name="Walk", duration_hint=2.0):
        self.take_name = take_name
        self.duration_hint = duration_hint
        self.source_model_guid = ""


def test_skeletal_mismatch_check_is_inconclusive_when_renderer_has_no_guid():
    from Infernux.components.skeletal_animator import _skinned_mismatch_message

    clip = _FakeClip()
    clip.source_model_path = "Assets/Models/Driver.fbx"

    assert _skinned_mismatch_message(None, clip, "") == ""


class TestTriggerConsumption:
    def test_exact_identifier_consumed(self):
        anim = _make_animator()
        attack = AnimParameter(
            name="attack", value_type=TypeRef(ValueType.BOOL), default=False
        )
        anim._fsm = AnimStateMachine(parameters=[attack])
        anim._parameters = {"attack": True}
        anim._consume_triggers(
            AnimTransition(conditions=[_condition(attack)])
        )
        assert anim._parameters["attack"] is False

    def test_substring_not_consumed(self):
        anim = _make_animator()
        attack = AnimParameter(
            name="attack", value_type=TypeRef(ValueType.BOOL), default=False
        )
        attacking = AnimParameter(
            name="is_attacking", value_type=TypeRef(ValueType.BOOL), default=False
        )
        anim._fsm = AnimStateMachine(parameters=[attack, attacking])
        anim._parameters = {"attack": True}
        anim._consume_triggers(
            AnimTransition(conditions=[_condition(attacking)])
        )
        assert anim._parameters["attack"] is True, \
            "'attack' must NOT be consumed by identifier 'is_attacking'"

    def test_multiple_triggers(self):
        anim = _make_animator()
        jump = AnimParameter(
            name="jump", value_type=TypeRef(ValueType.BOOL), default=False
        )
        fire = AnimParameter(
            name="fire", value_type=TypeRef(ValueType.BOOL), default=False
        )
        anim._fsm = AnimStateMachine(parameters=[jump, fire])
        anim._parameters = {"jump": True, "fire": True, "idle": True}
        anim._consume_triggers(
            AnimTransition(conditions=[_condition(jump), _condition(fire)])
        )
        assert anim._parameters["jump"] is False
        assert anim._parameters["fire"] is False
        assert anim._parameters["idle"] is True

    def test_spirit_animator_same_semantics(self):
        sp = SpiritAnimator()
        attack = AnimParameter(
            name="attack", value_type=TypeRef(ValueType.BOOL), default=False
        )
        attacking = AnimParameter(
            name="is_attacking", value_type=TypeRef(ValueType.BOOL), default=False
        )
        sp._fsm = AnimStateMachine(parameters=[attack, attacking])
        sp._parameters = {"attack": True}
        sp._consume_triggers(
            AnimTransition(conditions=[_condition(attacking)])
        )
        assert sp._parameters["attack"] is True


class TestTransitionDuration:
    def test_transition_duration_overrides_component_fade(self):
        anim = _make_animator()
        anim.cross_fade_duration = 0.15
        prev = _FakeClip("A")
        nxt = _FakeClip("B")
        anim._start_blend_if_needed(prev, 0.5, 1.0, nxt, fade_duration=0.6)
        assert anim._blend_duration == pytest.approx(0.6)
        assert anim._blend_from_take_name == "A"

    def test_component_fade_is_fallback(self):
        anim = _make_animator()
        anim.cross_fade_duration = 0.25
        anim._start_blend_if_needed(_FakeClip("A"), 0.0, 1.0, _FakeClip("B"),
                                    fade_duration=None)
        assert anim._blend_duration == pytest.approx(0.25)

    def test_zero_duration_means_hard_cut(self):
        anim = _make_animator()
        anim.cross_fade_duration = 0.25
        anim._start_blend_if_needed(_FakeClip("A"), 0.0, 1.0, _FakeClip("B"),
                                    fade_duration=0.0)
        assert anim._blend_duration == 0.0
        assert anim._blend_from_clip is None

    def test_same_take_fade_allowed(self):
        # Restarting the same take with a fade is valid (sampled at two times).
        anim = _make_animator()
        anim.cross_fade_duration = 0.2
        anim._start_blend_if_needed(_FakeClip("Run"), 1.2, 1.0, _FakeClip("Run"),
                                    fade_duration=None)
        assert anim._blend_duration == pytest.approx(0.2)
        assert anim._blend_from_take_name == "Run"


class TestEmptyConditionTransition:
    def _animator_with_state(self, loop: bool, duration: float):
        anim = _make_animator()
        fsm = AnimStateMachine(name="t", mode="3d")
        state = AnimState(name="S", loop=loop)
        fsm.states.append(state)
        anim._fsm = fsm
        anim._current_state_name = "S"
        anim._current_clip = _FakeClip("S_take", duration_hint=duration)
        return anim, state

    def test_clip_finished_fires_for_non_loop(self):
        anim, _ = self._animator_with_state(loop=False, duration=1.0)
        anim._elapsed = 1.0
        tr = AnimTransition(target_state="Next")
        assert anim._evaluate_condition(tr) is True

    def test_clip_not_finished_does_not_fire(self):
        anim, _ = self._animator_with_state(loop=False, duration=1.0)
        anim._elapsed = 0.4
        tr = AnimTransition(target_state="Next")
        assert anim._evaluate_condition(tr) is False

    def test_looping_state_never_fires_empty_condition(self):
        anim, _ = self._animator_with_state(loop=True, duration=1.0)
        anim._elapsed = 5.0
        tr = AnimTransition(target_state="Next")
        assert anim._evaluate_condition(tr) is False

    def test_parameter_condition(self):
        anim, _ = self._animator_with_state(loop=True, duration=1.0)
        speed = AnimParameter(name="speed")
        anim._fsm.parameters.append(speed)
        anim._parameters = {"speed": 3.0}
        tr = AnimTransition(
            target_state="Run", conditions=[_condition(speed, ">", 2.0)]
        )
        assert anim._evaluate_condition(tr) is True
        tr2 = AnimTransition(
            target_state="Run", conditions=[_condition(speed, ">", 5.0)]
        )
        assert anim._evaluate_condition(tr2) is False


class TestNormalizedTime:
    def test_normalized_clamps_to_one(self):
        anim = _make_animator()
        anim._current_clip = _FakeClip(duration_hint=2.0)
        anim._elapsed = 5.0
        assert anim.normalized_time == 1.0

    def test_unknown_duration_uses_default_period(self):
        anim = _make_animator()
        anim._current_clip = _FakeClip(duration_hint=0.0)
        anim._elapsed = 0.25
        assert 0.0 <= anim.normalized_time < 1.0


# ── Safe condition evaluator (replaces eval()) ──────────────────────────────

class TestSafeConditionEvaluator:
    def test_simple_compare(self):
        from Infernux.core.anim_state_machine import evaluate_anim_condition
        assert evaluate_anim_condition("speed > 2.0", {"speed": 3.0}) is True
        assert evaluate_anim_condition("speed > 2.0", {"speed": 1.0}) is False

    def test_and_chain(self):
        from Infernux.core.anim_state_machine import evaluate_anim_condition
        ctx = {"speed": 3.0, "grounded": True}
        assert evaluate_anim_condition("(speed > 0.5) and (grounded == 1.0)", ctx) is True
        ctx["grounded"] = False
        assert evaluate_anim_condition("(speed > 0.5) and (grounded == 1.0)", ctx) is False

    def test_or_and_not(self):
        from Infernux.core.anim_state_machine import evaluate_anim_condition
        assert evaluate_anim_condition("a or b", {"a": False, "b": True}) is True
        assert evaluate_anim_condition("not grounded", {"grounded": False}) is True
        assert evaluate_anim_condition("not grounded", {"grounded": True}) is False

    def test_bool_param_truthiness(self):
        from Infernux.core.anim_state_machine import evaluate_anim_condition
        assert evaluate_anim_condition("is_running", {"is_running": True}) is True
        assert evaluate_anim_condition("is_running", {"is_running": False}) is False

    def test_string_state_compare(self):
        from Infernux.core.anim_state_machine import evaluate_anim_condition
        assert evaluate_anim_condition('state == "idle"', {"state": "idle"}) is True
        assert evaluate_anim_condition('state == "idle"', {"state": "run"}) is False

    def test_unknown_identifier_defaults_zero(self):
        from Infernux.core.anim_state_machine import evaluate_anim_condition
        assert evaluate_anim_condition("missing > 0", {}) is False
        assert evaluate_anim_condition("missing == 0", {}) is True

    def test_malformed_raises(self):
        from Infernux.core.anim_state_machine import evaluate_anim_condition, AnimConditionError
        # Calls / attribute access / subscripts are rejected (no eval()).
        with pytest.raises((AnimConditionError, SyntaxError)):
            evaluate_anim_condition("__import__('os').system('x')", {})
        with pytest.raises((AnimConditionError, SyntaxError)):
            evaluate_anim_condition("obj.attr", {})


# ── Animation events ────────────────────────────────────────────────────────

class _EventSink:
    def __init__(self):
        self.calls = []
        self.footsteps = 0

    def on_animation_event(self, name, string_arg, number_arg):
        self.calls.append((name, string_arg, number_arg))

    def footstep(self, string_arg, number_arg):
        self.footsteps += 1


class _FakeGameObject:
    def __init__(self, comps):
        self._comps = comps

    def get_py_components(self):
        return list(self._comps)


class TestAnimationEventWindowing:
    def _ev(self, t, name="e"):
        from Infernux.core.animation_event import AnimationEvent
        return AnimationEvent(time_normalized=t, function=name)

    def test_forward_window(self):
        from Infernux.core.animation_event import collect_crossed_events
        evs = [self._ev(0.3), self._ev(0.6)]
        fired = collect_crossed_events(evs, 0.2, 0.5, looped=False)
        assert [e.time_normalized for e in fired] == [0.3]

    def test_no_double_fire(self):
        from Infernux.core.animation_event import collect_crossed_events
        evs = [self._ev(0.3)]
        assert collect_crossed_events(evs, 0.3, 0.5, looped=False) == []  # exclusive lower bound

    def test_loop_wrap_window(self):
        from Infernux.core.animation_event import collect_crossed_events
        evs = [self._ev(0.9), self._ev(0.05)]
        fired = collect_crossed_events(evs, 0.8, 0.1, looped=True)
        names = sorted(e.time_normalized for e in fired)
        assert names == [0.05, 0.9]

    def test_dispatch_calls_generic_and_named(self):
        from Infernux.core.animation_event import AnimationEvent, dispatch_animation_events
        sink = _EventSink()
        go = _FakeGameObject([sink])
        evs = [AnimationEvent(time_normalized=0.5, function="footstep", string_arg="L", number_arg=2.0)]
        dispatch_animation_events(go, evs, 0.4, 0.6, looped=False)
        assert sink.footsteps == 1
        assert sink.calls == [("footstep", "L", 2.0)]


# ── Serialization round-trips for new fields ────────────────────────────────

class TestAnimationSerialization:
    def test_transition_duration_round_trip(self):
        condition = AnimCondition(parameter_id="speed-id", operator=">", threshold=1.0)
        tr = AnimTransition(target_state="Run", conditions=[condition], duration=0.25)
        tr2 = AnimTransition.from_dict(tr.to_dict())
        assert tr2.duration == 0.25
        assert tr2.target_state == "Run"

    def test_clip2d_events_round_trip(self):
        from Infernux.core.animation_clip import AnimationClip
        from Infernux.core.animation_event import AnimationEvent
        clip = AnimationClip(name="walk", frame_indices=[0, 1, 2], fps=12.0)
        clip.events = [AnimationEvent(0.5, "footstep", "L", 1.0)]
        clip2 = AnimationClip.from_dict(clip.to_dict())
        assert len(clip2.events) == 1
        assert clip2.events[0].function == "footstep"
        assert clip2.events[0].time_normalized == 0.5

    def test_clip3d_events_round_trip(self):
        from Infernux.core.animation_clip3d import AnimationClip3D
        from Infernux.core.animation_event import AnimationEvent
        clip = AnimationClip3D(name="run", take_name="Run")
        clip.events = [AnimationEvent(0.25, "hit", "", 3.0)]
        clip2 = AnimationClip3D.from_dict(clip.to_dict())
        assert len(clip2.events) == 1
        assert clip2.events[0].number_arg == 3.0


class TestBlendStateModel:
    def test_blend_state_round_trip(self):
        st = AnimState(name="LocoBlend", kind="blend",
                       clip_guid="A-guid", clip_b_guid="B-guid", blend_value=0.7)
        st2 = AnimState.from_dict(st.to_dict())
        assert st2.kind == "blend"
        assert st2.clip_guid == "A-guid"
        assert st2.clip_b_guid == "B-guid"
        assert st2.blend_value == 0.7

    def test_blend_value_out_of_range_is_rejected(self):
        document = AnimState(name="x", kind="blend").to_dict()
        document["blend_value"] = 5.0
        with pytest.raises(ValueError):
            AnimState.from_dict(document)

    def test_default_state_is_clip(self):
        st = AnimState(name="idle")
        assert st.kind == "clip"
        assert AnimState.from_dict(st.to_dict()).kind == "clip"
