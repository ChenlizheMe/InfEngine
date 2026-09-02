"""Animation FSM logic tests (pure Python — no GPU sampling needed).

Regression coverage for the animation-audit fixes:
- trigger consumption uses identifier boundaries (not substring matching)
- AnimTransition.duration drives the cross-fade for that transition
- non-looping clips report loop=False to the native pose submission
- empty-condition ("clip finished") transitions
"""
from __future__ import annotations

from types import SimpleNamespace

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
from Infernux.components.builtin.skinned_mesh_renderer import SkinnedMeshRenderer
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
    def __init__(self, take_name="Walk", duration_hint=2.0, source_model_guid=""):
        self.take_name = take_name
        self.duration_hint = duration_hint
        self.source_model_guid = source_model_guid
        self.source_model_path = ""


class _RendererBinding:
    def __init__(self, native):
        self.native = native

    def _get_bound_native_component(self):
        return self.native


class _RendererOwner:
    def __init__(self, renderer):
        self.renderer = renderer
        self.lookups = 0

    def get_component(self, _component_type):
        self.lookups += 1
        return self.renderer

    def get_children(self):
        return []


class _HierarchyOwner:
    def __init__(self, renderer=None, children=()):
        self.renderer = renderer
        self.children = list(children)

    def get_component(self, _component_type):
        return self.renderer

    def get_children(self):
        return list(self.children)


class _NativePoseRecorder:
    def __init__(self):
        self.calls = []
        self.pose_stacks = []

    def submit_animation_pose(self, *args):
        self.calls.append(args)

    def submit_pose_stack(self, layers):
        self.pose_stacks.append(layers)


def test_skeletal_animator_reacquires_renderer_after_scene_replacement(monkeypatch):
    animator = _make_animator()
    stale = _RendererBinding(None)
    live = _RendererBinding(object())
    owner = _RendererOwner(live)
    animator._skinned_renderer = stale
    monkeypatch.setattr(
        SkeletalAnimator,
        "game_object",
        property(lambda _self: owner),
    )

    assert animator._resolve_skinned_renderer() is live
    assert animator._skinned_renderer is live
    assert owner.lookups == 1


def test_skeletal_animator_has_no_same_object_renderer_requirement():
    assert SkinnedMeshRenderer not in tuple(
        getattr(SkeletalAnimator, "_require_components_", ()) or ()
    )


def test_skinned_mesh_renderer_exposes_authoritative_runtime_pose_time(monkeypatch):
    renderer = SkinnedMeshRenderer()
    native = SimpleNamespace(
        runtime_animation_time=1.25,
        runtime_animation_normalized_time=0.625,
    )
    monkeypatch.setattr(renderer, "_get_bound_native_component", lambda: native)

    assert renderer.runtime_animation_time == pytest.approx(1.25)
    assert renderer.runtime_animation_normalized_time == pytest.approx(0.625)

    monkeypatch.setattr(renderer, "_get_bound_native_component", lambda: None)
    with pytest.raises(ReferenceError, match="not bound"):
        _ = renderer.runtime_animation_time


def test_skeletal_animator_uses_local_renderer_for_single_node_model(monkeypatch):
    animator = _make_animator()
    local = _RendererBinding(object())
    owner = _HierarchyOwner(renderer=local)
    monkeypatch.setattr(
        SkeletalAnimator,
        "game_object",
        property(lambda _self: owner),
    )

    assert animator._resolve_skinned_renderers(force=True) == [local]
    assert animator._resolve_skinned_renderer() is local


def test_skeletal_animator_collects_all_descendant_renderers_from_model_root(monkeypatch):
    animator = _make_animator()
    body = _RendererBinding(object())
    visor = _RendererBinding(object())
    nested = _HierarchyOwner(children=[_HierarchyOwner(renderer=visor)])
    owner = _HierarchyOwner(children=[_HierarchyOwner(renderer=body), nested])
    monkeypatch.setattr(
        SkeletalAnimator,
        "game_object",
        property(lambda _self: owner),
    )

    assert animator._resolve_skinned_renderers(force=True) == [body, visor]
    assert animator._resolve_skinned_renderer() is body


def test_skeletal_animator_submits_same_pose_to_all_descendant_renderers(monkeypatch):
    animator = _make_animator()
    body_native = _NativePoseRecorder()
    visor_native = _NativePoseRecorder()
    owner = _HierarchyOwner(children=[
        _HierarchyOwner(renderer=_RendererBinding(body_native)),
        _HierarchyOwner(renderer=_RendererBinding(visor_native)),
    ])
    monkeypatch.setattr(
        SkeletalAnimator,
        "game_object",
        property(lambda _self: owner),
    )
    animator._fsm = AnimStateMachine(
        states=[AnimState(name="Walk", loop=True)],
        default_state="Walk",
    )
    animator._current_state_name = "Walk"
    animator._current_clip = _FakeClip("Walk", duration_hint=2.0)
    animator._elapsed = 0.5
    animator._playing = True
    animator._last_native_pose_key = None

    animator._sync_native_runtime_playback()

    assert len(body_native.calls) == 1
    assert visor_native.calls == body_native.calls


def test_skeletal_animator_submits_clip_source_separately_from_render_model(monkeypatch):
    animator = _make_animator()
    native = _NativePoseRecorder()
    owner = _HierarchyOwner(renderer=_RendererBinding(native))
    monkeypatch.setattr(SkeletalAnimator, "game_object", property(lambda _self: owner))
    animator._fsm = AnimStateMachine(
        states=[AnimState(name="Walk", loop=True)],
        default_state="Walk",
    )
    animator._current_state_name = "Walk"
    animator._current_clip = _FakeClip("Walk", source_model_guid="d" * 32)
    animator._elapsed = 0.5
    animator._playing = True
    animator._last_native_pose_key = None

    animator._sync_native_runtime_playback()

    assert len(native.calls) == 1
    assert native.calls[0][7] == "d" * 32


def test_blend_state_uses_pose_stack_as_its_only_native_path(monkeypatch):
    animator = _make_animator()
    native = _NativePoseRecorder()
    monkeypatch.setattr(
        animator,
        "_resolve_clip_b",
        lambda _state: _FakeClip("Run", source_model_guid="e" * 32),
    )
    state = AnimState(name="Locomotion", kind="blend", blend_value=0.25)
    animator._current_clip = _FakeClip("Walk", source_model_guid="d" * 32)
    animator._elapsed = 0.5

    assert animator._submit_blend_state([native], state) is True
    assert native.calls == []
    assert native.pose_stacks == [[
        {
            "take_name": "Walk",
            "source_model_guid": "d" * 32,
            "time": 0.5,
            "weight": 0.75,
            "loop": True,
        },
        {
            "take_name": "Run",
            "source_model_guid": "e" * 32,
            "time": 0.5,
            "weight": 0.25,
            "loop": True,
        },
    ]]


def test_blend_state_does_not_fall_back_when_pose_stack_is_missing(monkeypatch):
    animator = _make_animator()
    native = SimpleNamespace(submit_animation_pose=lambda *_args: None)
    monkeypatch.setattr(animator, "_resolve_clip_b", lambda _state: _FakeClip("Run"))
    animator._current_clip = _FakeClip("Walk")

    with pytest.raises(AttributeError, match="submit_pose_stack"):
        animator._submit_blend_state([native], AnimState(name="Locomotion", kind="blend"))


def test_native_pose_submission_failure_propagates(monkeypatch):
    animator = _make_animator()

    class Native:
        @staticmethod
        def submit_animation_pose(*_args):
            raise RuntimeError("native pose rejected")

    owner = _HierarchyOwner(renderer=_RendererBinding(Native()))
    monkeypatch.setattr(SkeletalAnimator, "game_object", property(lambda _self: owner))
    animator._fsm = AnimStateMachine(states=[AnimState(name="Walk")], default_state="Walk")
    animator._current_state_name = "Walk"
    animator._current_clip = _FakeClip("Walk")

    with pytest.raises(RuntimeError, match="native pose rejected"):
        animator._sync_native_runtime_playback()


def test_blend_parameter_type_error_propagates() -> None:
    animator = _make_animator()
    state = AnimState(name="Locomotion", kind="blend")
    animator._parameters["Locomotion/Lerp"] = "not-a-number"

    with pytest.raises(ValueError):
        animator._blend_state_lerp(state)


def test_native_duration_query_failure_propagates(monkeypatch) -> None:
    animator = _make_animator()

    class Native:
        @staticmethod
        def get_animation_duration_seconds(_take_name, _source_guid):
            raise RuntimeError("duration query rejected")

    monkeypatch.setattr(
        animator,
        "_resolve_skinned_renderer",
        lambda: _RendererBinding(Native()),
    )

    with pytest.raises(RuntimeError, match="duration query rejected"):
        animator._clip_duration(_FakeClip("Walk", duration_hint=0.0))


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

    def test_cross_fade_can_preserve_looping_phase(self):
        anim = _make_animator()
        walk = AnimState(name="Walk", loop=True)
        run = AnimState(name="Run", loop=True)
        anim._fsm = AnimStateMachine(states=[walk, run], default_state="Walk")
        anim._clip_cache = {
            "Walk": _FakeClip("Walk", duration_hint=2.0),
            "Run": _FakeClip("Run", duration_hint=4.0),
        }
        anim._current_state_name = "Walk"
        anim._current_clip = anim._clip_cache["Walk"]
        anim._elapsed = 1.5
        anim._playing = True
        anim._apply_active_take = lambda: None
        anim._sync_native_runtime_playback = lambda: None

        assert anim.cross_fade("Run", 0.2, preserve_phase=True) is True
        assert anim._elapsed == pytest.approx(3.0)
        assert anim._prev_event_norm == pytest.approx(0.75)
        assert anim._blend_from_elapsed == pytest.approx(1.5)

    def test_cross_fade_restarts_by_default(self):
        anim = _make_animator()
        walk = AnimState(name="Walk", loop=True)
        run = AnimState(name="Run", loop=True)
        anim._fsm = AnimStateMachine(states=[walk, run], default_state="Walk")
        anim._clip_cache = {
            "Walk": _FakeClip("Walk", duration_hint=2.0),
            "Run": _FakeClip("Run", duration_hint=4.0),
        }
        anim._current_state_name = "Walk"
        anim._current_clip = anim._clip_cache["Walk"]
        anim._elapsed = 1.5
        anim._playing = True
        anim._apply_active_take = lambda: None
        anim._sync_native_runtime_playback = lambda: None

        assert anim.cross_fade("Run", 0.2) is True
        assert anim._elapsed == pytest.approx(0.0)
        assert anim._prev_event_norm == pytest.approx(0.0)

    def test_fsm_transition_can_preserve_looping_phase(self):
        anim = _make_animator()
        moving = AnimParameter(
            name="moving",
            value_type=TypeRef(ValueType.BOOL),
            default=False,
        )
        walk = AnimState(name="Walk", loop=True, exit_time_normalized=0.0)
        run = AnimState(name="Run", loop=True)
        walk.transitions.append(
            AnimTransition(
                target_state="Run",
                conditions=[_condition(moving)],
                duration=0.2,
                synchronize_normalized_time=True,
            )
        )
        anim._fsm = AnimStateMachine(
            states=[walk, run],
            parameters=[moving],
            default_state="Walk",
        )
        anim._parameters = {"moving": True}
        anim._clip_cache = {
            "Walk": _FakeClip("Walk", duration_hint=2.0),
            "Run": _FakeClip("Run", duration_hint=4.0),
        }
        anim._current_state_name = "Walk"
        anim._current_clip = anim._clip_cache["Walk"]
        anim._elapsed = 1.5
        anim._playing = True
        anim._apply_active_take = lambda: None
        anim._sync_native_runtime_playback = lambda: None

        anim._try_auto_transition()

        assert anim._current_state_name == "Run"
        assert anim._elapsed == pytest.approx(3.0)
        assert anim._prev_event_norm == pytest.approx(0.75)
        assert anim._blend_duration == pytest.approx(0.2)


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

    def test_dispatch_uses_precomputed_arity_without_signature_reflection(self, monkeypatch):
        from Infernux.core import animation_event
        from Infernux.core.animation_event import AnimationEvent
        from Infernux.engine import runtime_dispatch
        from Infernux.engine.runtime_dispatch import publish_runtime_dispatch_epoch

        class AritySink:
            def __init__(self):
                self.calls = []

            def one_arg(self, value):
                self.calls.append(("one", value))

            def broken(self, _value, _number):
                self.calls.append("broken")
                raise TypeError("handler body")

        sink = AritySink()
        publication = publish_runtime_dispatch_epoch((AritySink,))
        publication.commit()
        try:
            descriptor = runtime_dispatch.current_runtime_epoch().descriptor_for(AritySink)
            assert descriptor is not None
            assert descriptor.methods["one_arg"].accepted_arg_counts == (1,)

            def reject_signature(_callback):
                raise AssertionError("event dispatch must not inspect signatures")

            monkeypatch.setattr(runtime_dispatch.inspect, "signature", reject_signature)
            go = _FakeGameObject([sink])
            animation_event.dispatch_animation_events(
                go,
                [
                    AnimationEvent(0.5, "one_arg", "L", 2.0),
                    AnimationEvent(0.5, "broken", "R", 3.0),
                ],
                0.4,
                0.6,
                looped=False,
            )
            assert sink.calls == [("one", "L"), "broken"]
        finally:
            publication.rollback()

    def test_generic_sink_and_named_alias_are_invoked_once(self):
        from Infernux.core.animation_event import AnimationEvent, dispatch_animation_events
        from Infernux.engine.runtime_dispatch import publish_runtime_dispatch_epoch

        class AliasSink:
            def __init__(self):
                self.calls = 0

            def on_animation_event(self, _name, _text, _number):
                self.calls += 1

            alias = on_animation_event

        sink = AliasSink()
        publication = publish_runtime_dispatch_epoch((AliasSink,))
        publication.commit()
        try:
            dispatch_animation_events(
                _FakeGameObject([sink]),
                [AnimationEvent(0.5, "alias", "", 0.0)],
                0.4,
                0.6,
                looped=False,
            )
            assert sink.calls == 1
        finally:
            publication.rollback()

    def test_event_batch_keeps_one_epoch_after_handler_publishes_reload(self):
        from Infernux.core.animation_event import AnimationEvent, dispatch_animation_events
        from Infernux.engine.runtime_dispatch import (
            current_runtime_epoch,
            publish_runtime_dispatch_epoch,
        )

        class EpochSink:
            def __init__(self):
                self.calls = []

            def first(self):
                self.calls.append("first-old")

            def second(self):
                self.calls.append("second-old")

        sink = EpochSink()
        original_first = EpochSink.first
        original_second = EpochSink.second
        baseline = current_runtime_epoch()
        old_publication = publish_runtime_dispatch_epoch((EpochSink,))
        old_publication.commit()
        replacement_publication = None

        def second_new(self):
            self.calls.append("second-new")

        def first_reload(self):
            nonlocal replacement_publication
            self.calls.append("first-old")
            EpochSink.second = second_new
            replacement_publication = publish_runtime_dispatch_epoch((EpochSink,))
            replacement_publication.commit()

        EpochSink.first = first_reload
        reload_publication = publish_runtime_dispatch_epoch((EpochSink,))
        reload_publication.commit()
        try:
            dispatch_animation_events(
                _FakeGameObject([sink]),
                [AnimationEvent(0.5, "first"), AnimationEvent(0.5, "second")],
                0.4,
                0.6,
                looped=False,
            )
            assert sink.calls == ["first-old", "second-old"]

            dispatch_animation_events(
                _FakeGameObject([sink]),
                [AnimationEvent(0.5, "second")],
                0.4,
                0.6,
                looped=False,
            )
            assert sink.calls[-1] == "second-new"
        finally:
            if replacement_publication is not None:
                replacement_publication.rollback()
            EpochSink.first = original_first
            EpochSink.second = original_second
            reload_publication.rollback()
            old_publication.rollback()
            assert current_runtime_epoch() is baseline


# ── Serialization round-trips for new fields ────────────────────────────────

class TestAnimationSerialization:
    def test_transition_duration_round_trip(self):
        condition = AnimCondition(parameter_id="speed-id", operator=">", threshold=1.0)
        tr = AnimTransition(
            target_state="Run",
            conditions=[condition],
            duration=0.25,
            synchronize_normalized_time=True,
        )
        tr2 = AnimTransition.from_dict(tr.to_dict())
        assert tr2.duration == 0.25
        assert tr2.target_state == "Run"
        assert tr2.synchronize_normalized_time is True

    def test_clip2d_events_round_trip(self):
        from Infernux.core.animation_clip import AnimationClip, AnimationFrame
        from Infernux.core.animation_event import AnimationEvent
        clip = AnimationClip(
            name="walk",
            frames=[
                AnimationFrame(sprite_frame_id=f"{index + 1:032x}")
                for index in (0, 1, 2)
            ],
            fps=12.0,
        )
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


def _write_texture_asset_metadata(
    texture_path,
    *,
    guid: str,
    texture_type,
    sprite_frame_ids=(),
):
    import json

    from Infernux.core.asset_types import SpriteFrame, TextureImportSettings

    texture_path.parent.mkdir(parents=True, exist_ok=True)
    texture_path.write_bytes(b"test texture")
    settings = TextureImportSettings(
        texture_type=texture_type,
        sprite_frames=[
            SpriteFrame(stable_id=stable_id, name=f"frame_{index}", w=16, h=16)
            for index, stable_id in enumerate(sprite_frame_ids)
        ],
    )

    def tagged(value):
        if type(value) is bool:
            tag = "bool"
        elif type(value) is int:
            tag = "int"
        elif type(value) is list:
            tag = "json_array"
        else:
            tag = "string"
        return {"type": tag, "value": value}

    metadata = {"guid": tagged(guid)}
    metadata.update({key: tagged(value) for key, value in settings.to_dict().items()})
    texture_path.with_name(texture_path.name + ".meta").write_text(
        json.dumps({"metadata": metadata}, indent=2),
        encoding="utf-8",
    )


class TestAnimationClipSpriteFrameReferences:
    FRAME_A = "1" * 32
    FRAME_B = "2" * 32
    TEXTURE_GUID = "a" * 32

    def test_valid_sprite_frame_references_resolve_project_relative_path(self, tmp_path):
        from Infernux.core.animation_clip import AnimationClip, AnimationFrame
        from Infernux.core.asset_types import TextureType

        project = tmp_path / "project"
        texture = project / "Assets" / "Sprites" / "sheet.png"
        _write_texture_asset_metadata(
            texture,
            guid=self.TEXTURE_GUID,
            texture_type=TextureType.SPRITE,
            sprite_frame_ids=(self.FRAME_A, self.FRAME_B),
        )
        clip = AnimationClip(
            authoring_texture_path="Assets/Sprites/sheet.png",
            frames=[AnimationFrame(sprite_frame_id=self.FRAME_B)],
        )

        resolved = clip.validate_sprite_frame_references(project_root=str(project))

        assert resolved == str(texture)

    def test_missing_sprite_frame_reference_is_rejected(self, tmp_path):
        from Infernux.core.animation_clip import AnimationClip, AnimationFrame
        from Infernux.core.asset_types import TextureType

        texture = tmp_path / "sheet.png"
        _write_texture_asset_metadata(
            texture,
            guid=self.TEXTURE_GUID,
            texture_type=TextureType.SPRITE,
            sprite_frame_ids=(self.FRAME_A,),
        )
        clip = AnimationClip(
            authoring_texture_path=str(texture),
            frames=[AnimationFrame(sprite_frame_id=self.FRAME_B)],
        )

        with pytest.raises(ValueError, match=self.FRAME_B):
            clip.validate_sprite_frame_references()

    def test_non_sprite_texture_is_rejected_before_frame_lookup(self, tmp_path):
        from Infernux.core.animation_clip import AnimationClip, AnimationFrame
        from Infernux.core.asset_types import TextureType

        texture = tmp_path / "albedo.png"
        _write_texture_asset_metadata(
            texture,
            guid=self.TEXTURE_GUID,
            texture_type=TextureType.DEFAULT,
        )
        clip = AnimationClip(
            authoring_texture_path=str(texture),
            frames=[AnimationFrame(sprite_frame_id=self.FRAME_A)],
        )

        with pytest.raises(ValueError, match="not imported as Sprite"):
            clip.validate_sprite_frame_references()

    def test_path_and_guid_survive_file_round_trip_and_guid_resolves_moved_texture(
        self, tmp_path
    ):
        from Infernux.core.animation_clip import AnimationClip, AnimationFrame
        from Infernux.core.asset_types import TextureType

        project = tmp_path / "project"
        texture = project / "Assets" / "Sprites" / "sheet.png"
        clip_path = project / "Assets" / "Animations" / "walk.animclip2d"
        clip_path.parent.mkdir(parents=True)
        _write_texture_asset_metadata(
            texture,
            guid=self.TEXTURE_GUID,
            texture_type=TextureType.SPRITE,
            sprite_frame_ids=(self.FRAME_A,),
        )
        clip = AnimationClip(
            name="walk",
            authoring_texture_guid=self.TEXTURE_GUID,
            authoring_texture_path="Assets/Sprites/sheet.png",
            frames=[AnimationFrame(sprite_frame_id=self.FRAME_A)],
        )
        assert clip.save(str(clip_path)) is True

        loaded = AnimationClip.load(str(clip_path))

        assert loaded is not None
        assert loaded.authoring_texture_guid == self.TEXTURE_GUID
        assert loaded.authoring_texture_path == "Assets/Sprites/sheet.png"
        loaded.authoring_texture_path = "Assets/Sprites/stale-path.png"
        assert loaded.validate_sprite_frame_references(
            project_root=str(project),
            guid_paths={self.TEXTURE_GUID: str(texture)},
        ) == str(texture)


def test_spirit_animator_guid_does_not_fall_back_to_stale_path(monkeypatch, tmp_path):
    from Infernux.components import spirit_animator as animator_module
    from Infernux.core.anim_state_machine import AnimState

    stale_path = tmp_path / "stale.animclip2d"
    stale_path.write_text("{}", encoding="utf-8")
    database = type(
        "Database",
        (),
        {"get_path_from_guid": staticmethod(lambda _guid: "")},
    )()
    monkeypatch.setattr(animator_module, "_get_asset_database", lambda: database)

    state = AnimState(
        name="Walk",
        clip_guid="b" * 32,
        clip_path=str(stale_path),
    )

    assert animator_module._resolve_clip_path(state) is None


def test_skeletal_animator_asset_database_failure_is_not_suppressed(monkeypatch):
    from Infernux.components import skeletal_animator as animator_module

    class Database:
        @staticmethod
        def get_path_from_guid(_guid):
            raise RuntimeError("catalog unavailable")

    monkeypatch.setattr(animator_module, "_get_asset_database", lambda: Database())

    with pytest.raises(RuntimeError, match="catalog unavailable"):
        animator_module._resolve_clip_path_from("c" * 32)


def test_skeletal_animator_does_not_derive_model_identity_from_path():
    from Infernux.components import skeletal_animator as animator_module
    from Infernux.core.animation_clip3d import AnimationClip3D

    clip = AnimationClip3D(source_model_path="Assets/Models/stale.fbx")

    assert animator_module._animation_source_guid(clip) == ""


class TestSpiritAnimatorAssetReload:
    @staticmethod
    def _clip(frame_count: int, fps: float):
        from Infernux.core.animation_clip import AnimationClip, AnimationFrame

        return AnimationClip(
            frames=[
                AnimationFrame(sprite_frame_id=f"{index + 1:032x}")
                for index in range(frame_count)
            ],
            fps=fps,
        )

    def test_clip_hot_reload_preserves_state_progress_and_playback(
        self, tmp_path, monkeypatch
    ):
        import json
        from Infernux.components import spirit_animator as animator_module

        path = tmp_path / "walk.animclip2d"
        clip_guid = "a" * 32
        old_clip = self._clip(4, 4.0)
        path.write_text(json.dumps(old_clip.to_dict()), encoding="utf-8")
        database = type(
            "Database",
            (),
            {"get_path_from_guid": staticmethod(lambda guid: str(path) if guid == clip_guid else "")},
        )()
        monkeypatch.setattr(animator_module, "_get_asset_database", lambda: database)

        state = AnimState(name="Walk", clip_guid=clip_guid, clip_path=str(path))
        animator = SpiritAnimator()
        animator._fsm = AnimStateMachine(
            states=[state],
            default_state="Walk",
        )
        animator._clip_cache = {"Walk": old_clip}
        animator._current_state_name = "Walk"
        animator._current_clip = old_clip
        animator._elapsed = old_clip.duration * 0.5
        animator._playing = True
        animator._parameters = {"speed": 3.0}

        class Renderer:
            frame_id = ""
            sync_count = 0

            def sync_visual(self):
                self.sync_count += 1

        renderer = Renderer()
        animator._sprite_renderer = renderer

        replacement = self._clip(8, 8.0)
        path.write_text(json.dumps(replacement.to_dict()), encoding="utf-8")
        from Infernux.engine.interaction import AssetMutation, AssetMutationKind

        animator._on_asset_changed(
            AssetMutation(AssetMutationKind.MODIFIED, f"{path}.meta")
        )

        assert animator._current_state_name == "Walk"
        assert animator._current_clip is animator._clip_cache["Walk"]
        assert animator._current_clip.frame_count == 8
        assert animator.normalized_time == pytest.approx(0.5)
        assert animator._playing is True
        assert animator._parameters == {"speed": 3.0}
        assert renderer.frame_id == replacement.frames[4].sprite_frame_id
        assert renderer.sync_count == 1

    def test_invalid_clip_hot_reload_keeps_previous_runtime_clip(
        self, tmp_path, monkeypatch
    ):
        import json
        from Infernux.components import spirit_animator as animator_module

        path = tmp_path / "walk.animclip2d"
        clip_guid = "b" * 32
        clip = self._clip(2, 2.0)
        path.write_text(json.dumps(clip.to_dict()), encoding="utf-8")
        database = type(
            "Database",
            (),
            {"get_path_from_guid": staticmethod(lambda guid: str(path) if guid == clip_guid else "")},
        )()
        monkeypatch.setattr(animator_module, "_get_asset_database", lambda: database)
        state = AnimState(name="Walk", clip_guid=clip_guid, clip_path=str(path))
        animator = SpiritAnimator()
        animator._fsm = AnimStateMachine(states=[state], default_state="Walk")
        animator._clip_cache = {"Walk": clip}
        animator._current_state_name = "Walk"
        animator._current_clip = clip
        animator._elapsed = 0.25
        animator._playing = True
        animator._sprite_renderer = None

        path.write_text("not json", encoding="utf-8")
        from Infernux.engine.interaction import AssetMutation, AssetMutationKind

        animator._on_asset_changed(
            AssetMutation(AssetMutationKind.MODIFIED, str(path))
        )

        assert animator._current_clip is clip
        assert animator._clip_cache["Walk"] is clip
        assert animator._elapsed == pytest.approx(0.25)
        assert animator._playing is True

    def test_controller_hot_reload_preserves_timeline_progress_without_clip_parse(
        self, tmp_path, monkeypatch
    ):
        from Infernux.core.animation_timeline import AnimationTimeline
        from Infernux.core.animation_clip import AnimationClip

        controller_path = tmp_path / "controller.animfsm"
        timeline_path = tmp_path / "motion.animtimeline"
        timeline_guid = "c" * 32
        from Infernux.components import spirit_animator as animator_module
        database = type(
            "Database",
            (),
            {"get_path_from_guid": staticmethod(lambda guid: str(timeline_path) if guid == timeline_guid else "")},
        )()
        monkeypatch.setattr(animator_module, "_get_asset_database", lambda: database)
        timeline = AnimationTimeline(name="motion", duration=4.0)
        assert timeline.save(str(timeline_path)) is True
        state = AnimState(
            name="Timeline",
            kind="timeline",
            timeline_guid=timeline_guid,
            timeline_path=str(timeline_path),
        )
        current_fsm = AnimStateMachine(
            name="controller",
            mode="2d",
            states=[state],
            default_state="Timeline",
        )
        current_fsm.file_path = str(controller_path)

        animator = SpiritAnimator()
        animator._fsm = current_fsm
        animator._current_state_name = "Timeline"
        animator._current_timeline = timeline
        animator._current_clip = None
        animator._timeline_cache = {"Timeline": timeline}
        animator._clip_cache = {}
        animator._elapsed = 2.0
        animator._playing = True
        animator._parameters = {}

        replacement_timeline = AnimationTimeline(name="motion", duration=8.0)
        assert replacement_timeline.save(str(timeline_path)) is True
        replacement_fsm = AnimStateMachine(
            name="controller",
            mode="2d",
            states=[
                AnimState(
                    name="Timeline",
                    kind="timeline",
                    timeline_guid=timeline_guid,
                    timeline_path=str(timeline_path),
                )
            ],
            default_state="Timeline",
        )
        assert replacement_fsm.save(str(controller_path)) is True

        def reject_clip_parse(_path):
            raise AssertionError("controller/timeline hot reload must not parse a 2D clip")

        monkeypatch.setattr(AnimationClip, "load", reject_clip_parse)
        applied = []
        monkeypatch.setattr(
            animator,
            "_apply_timeline",
            lambda value, elapsed: applied.append((value, elapsed)),
        )

        from Infernux.engine.interaction import AssetMutation, AssetMutationKind

        animator._on_asset_changed(
            AssetMutation(AssetMutationKind.MODIFIED, str(controller_path))
        )

        assert animator.current_state == "Timeline"
        assert animator._current_clip is None
        assert animator._current_timeline is not None
        assert animator._current_timeline.duration == pytest.approx(8.0)
        assert animator.normalized_time == pytest.approx(0.5)
        assert animator.is_playing is True
        assert applied == [(animator._current_timeline, pytest.approx(4.0))]


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
