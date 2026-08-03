"""Unit tests for the AnimStateMachine data model (``.animfsm`` / ``.timelinefsm``).

Covers the safe transition-condition evaluator, parameters, transitions, states
(clip / blend / timeline), the FSM container, helpers, and serialization.
Pure-logic tests — no renderer required.
"""
from __future__ import annotations

import pytest

from Infernux.core.anim_state_machine import (
    AnimStateMachine,
    AnimState,
    AnimTransition,
    AnimParameter,
    AnimConditionError,
    evaluate_anim_condition,
)
from Infernux.graph import TypeRef, ValueType


# ── evaluate_anim_condition ─────────────────────────────────────────────────

def test_condition_empty_is_false():
    assert evaluate_anim_condition("", {}) is False
    assert evaluate_anim_condition("   ", {}) is False


def test_condition_unknown_name_is_falsey():
    assert evaluate_anim_condition("missing", {}) is False


def test_condition_bare_truthy_flag():
    assert evaluate_anim_condition("grounded", {"grounded": 1.0}) is True
    assert evaluate_anim_condition("grounded", {"grounded": 0.0}) is False


def test_condition_bool_flag():
    assert evaluate_anim_condition("jump", {"jump": True}) is True
    assert evaluate_anim_condition("jump", {"jump": False}) is False


@pytest.mark.parametrize("expr,ctx,expected", [
    ("speed > 0.5", {"speed": 1.0}, True),
    ("speed > 0.5", {"speed": 0.1}, False),
    ("speed >= 1.0", {"speed": 1.0}, True),
    ("speed < 0.5", {"speed": 0.1}, True),
    ("speed <= 0.5", {"speed": 0.5}, True),
    ("speed == 1.0", {"speed": 1.0}, True),
    ("speed != 1.0", {"speed": 2.0}, True),
    ("speed != 1.0", {"speed": 1.0}, False),
])
def test_condition_comparisons(expr, ctx, expected):
    assert evaluate_anim_condition(expr, ctx) is expected


def test_condition_and_chain():
    ctx = {"speed": 1.0, "grounded": 1.0}
    assert evaluate_anim_condition("(speed > 0.5) and (grounded == 1.0)", ctx) is True
    ctx["grounded"] = 0.0
    assert evaluate_anim_condition("(speed > 0.5) and (grounded == 1.0)", ctx) is False


def test_condition_or_chain():
    assert evaluate_anim_condition("a or b", {"a": 0, "b": 1}) is True
    assert evaluate_anim_condition("a or b", {"a": 0, "b": 0}) is False


def test_condition_not():
    assert evaluate_anim_condition("not grounded", {"grounded": 0}) is True
    assert evaluate_anim_condition("not grounded", {"grounded": 1}) is False


def test_condition_chained_comparison():
    assert evaluate_anim_condition("0 < x < 10", {"x": 5}) is True
    assert evaluate_anim_condition("0 < x < 10", {"x": 50}) is False


def test_condition_unary_minus():
    assert evaluate_anim_condition("x < -1", {"x": -5}) is True


def test_condition_string_equality():
    assert evaluate_anim_condition('state == "idle"', {"state": "idle"}) is True
    assert evaluate_anim_condition('state == "idle"', {"state": "run"}) is False


def test_condition_numeric_literal_true():
    assert evaluate_anim_condition("1 == 1", {}) is True


def test_condition_syntax_error_raises():
    with pytest.raises((AnimConditionError, SyntaxError)):
        evaluate_anim_condition("1 +", {})


def test_condition_function_call_rejected():
    with pytest.raises(AnimConditionError):
        evaluate_anim_condition("foo()", {})


def test_condition_attribute_rejected():
    with pytest.raises(AnimConditionError):
        evaluate_anim_condition("a.b", {"a": 1})


def test_condition_subscript_rejected():
    with pytest.raises(AnimConditionError):
        evaluate_anim_condition("a[0]", {"a": [1]})


def test_condition_mixed_type_compare_does_not_crash():
    # str vs number should be handled gracefully (no exception)
    result = evaluate_anim_condition('x > 1', {"x": "hello"})
    assert isinstance(result, bool)


# ── AnimParameter ───────────────────────────────────────────────────────────

def test_parameter_defaults():
    p = AnimParameter()
    assert p.name == "NewVar"
    assert p.value_type == TypeRef(ValueType.F32)
    assert p.default == 0.0
    assert p.stable_id


def test_parameter_to_dict_bool():
    d = AnimParameter(
        name="jump", value_type=TypeRef(ValueType.BOOL), default=True
    ).to_dict()
    assert d["name"] == "jump"
    assert d["type"] == {"value_type": "bool", "space": "none"}
    assert d["default"] is True
    assert d["stable_id"]


def test_parameter_to_dict_float():
    d = AnimParameter(
        name="speed", value_type=TypeRef(ValueType.F32), default=2.5
    ).to_dict()
    assert d["default"] == 2.5


def test_parameter_to_dict_int():
    d = AnimParameter(
        name="hp", value_type=TypeRef(ValueType.I32), default=7
    ).to_dict()
    assert d["default"] == 7


@pytest.mark.parametrize(
    "document",
    [
        {"name": "fire", "kind": "trigger", "default_bool": False},
        {"name": "s", "kind": "float", "default": 3.5},
        {"name": "b", "param_type": "bool", "default_bool": True},
    ],
)
def test_parameter_rejects_noncanonical_documents(document):
    with pytest.raises(ValueError):
        AnimParameter.from_dict(document)


def test_parameter_round_trip():
    p = AnimParameter(
        name="x", value_type=TypeRef(ValueType.I32), default=4
    )
    p2 = AnimParameter.from_dict(p.to_dict())
    assert p2.name == "x"
    assert p2.value_type == TypeRef(ValueType.I32)
    assert p2.default == 4
    assert p2.stable_id == p.stable_id


def test_parameter_invalid_float_default_is_rejected():
    with pytest.raises(TypeError):
        AnimParameter.from_dict(
            {
                "stable_id": "parameter-s",
                "name": "s",
                "type": {"value_type": "f32", "space": "none"},
                "default": "NaNN",
                "exposed": True,
                "writable": True,
                "category": "",
                "tooltip": "",
                "attributes": [],
            }
        )


# ── AnimTransition ──────────────────────────────────────────────────────────

def test_transition_defaults():
    t = AnimTransition()
    assert t.target_state == ""
    assert t.condition == ""
    assert t.duration == 0.0


def test_transition_round_trip():
    t = AnimTransition(target_state="Run", condition="speed > 1", duration=0.25)
    t2 = AnimTransition.from_dict(t.to_dict())
    assert t2.target_state == "Run"
    assert t2.condition == "speed > 1"
    assert t2.duration == 0.25


def test_transition_incomplete_document_is_rejected():
    with pytest.raises(ValueError):
        AnimTransition.from_dict({})


# ── AnimState ───────────────────────────────────────────────────────────────

def test_state_defaults():
    s = AnimState()
    assert s.name == "New State"
    assert s.kind == "clip"
    assert s.speed == 1.0
    assert s.loop is True
    assert s.exit_time_normalized == 1.0
    assert s.blend_value == 0.5
    assert s.transitions == []


@pytest.mark.parametrize("kind", ["clip", "blend", "timeline"])
def test_state_kinds_round_trip(kind):
    s = AnimState(name="S", kind=kind)
    assert AnimState.from_dict(s.to_dict()).kind == kind


def test_state_blend_value_above_range_is_rejected():
    document = AnimState().to_dict()
    document["blend_value"] = 5.0
    with pytest.raises(ValueError):
        AnimState.from_dict(document)


def test_state_blend_value_below_range_is_rejected():
    document = AnimState().to_dict()
    document["blend_value"] = -2.0
    with pytest.raises(ValueError):
        AnimState.from_dict(document)


@pytest.mark.parametrize("value", [-1.0, 2.0])
def test_state_exit_time_out_of_range_is_rejected(value):
    document = AnimState().to_dict()
    document["exit_time_normalized"] = value
    with pytest.raises(ValueError):
        AnimState.from_dict(document)


def test_state_timeline_reference_round_trip():
    s = AnimState(name="T", kind="timeline", timeline_guid="abc", timeline_path="x.animtimeline")
    s2 = AnimState.from_dict(s.to_dict())
    assert s2.timeline_guid == "abc"
    assert s2.timeline_path == "x.animtimeline"


def test_state_three_channel_header_color_is_rejected():
    document = AnimState().to_dict()
    document["header_color"] = [0.1, 0.2, 0.3]
    with pytest.raises(TypeError):
        AnimState.from_dict(document)


def test_state_header_color_keeps_alpha():
    document = AnimState().to_dict()
    document["header_color"] = [0.1, 0.2, 0.3, 0.5]
    s = AnimState.from_dict(document)
    assert s.header_color == [0.1, 0.2, 0.3, 0.5]


@pytest.mark.parametrize("value", [[0.1], "x"])
def test_state_invalid_header_color_is_rejected(value):
    document = AnimState().to_dict()
    document["header_color"] = value
    with pytest.raises(TypeError):
        AnimState.from_dict(document)


def test_state_transitions_round_trip():
    s = AnimState(name="A", transitions=[
        AnimTransition(target_state="B", condition="x"),
        AnimTransition(target_state="C"),
    ])
    s2 = AnimState.from_dict(s.to_dict())
    assert len(s2.transitions) == 2
    assert s2.transitions[0].target_state == "B"


def test_state_blend_fields_round_trip():
    s = AnimState(name="Bl", kind="blend", clip_guid="a", clip_b_guid="b", blend_value=0.3)
    s2 = AnimState.from_dict(s.to_dict())
    assert s2.clip_guid == "a" and s2.clip_b_guid == "b"
    assert s2.blend_value == pytest.approx(0.3)


# ── AnimStateMachine ────────────────────────────────────────────────────────

def test_fsm_defaults():
    fsm = AnimStateMachine()
    assert fsm.name == "New State Machine"
    assert fsm.mode == "2d"
    assert fsm.default_state == ""
    assert fsm.state_count == 0


def test_fsm_add_state_sets_default():
    fsm = AnimStateMachine()
    fsm.add_state("Idle")
    assert fsm.default_state == "Idle"
    assert fsm.state_count == 1


def test_fsm_add_state_autoname():
    fsm = AnimStateMachine()
    s = fsm.add_state()
    assert s.name == "State 0"


def test_fsm_add_second_state_keeps_default():
    fsm = AnimStateMachine()
    fsm.add_state("Idle")
    fsm.add_state("Run")
    assert fsm.default_state == "Idle"
    assert fsm.state_count == 2


def test_fsm_get_state():
    fsm = AnimStateMachine()
    fsm.add_state("Idle")
    assert fsm.get_state("Idle").name == "Idle"
    assert fsm.get_state("Nope") is None


def test_fsm_remove_state():
    fsm = AnimStateMachine()
    fsm.add_state("Idle")
    fsm.add_state("Run")
    assert fsm.remove_state("Run") is True
    assert fsm.get_state("Run") is None
    assert fsm.state_count == 1


def test_fsm_remove_missing_state():
    fsm = AnimStateMachine()
    assert fsm.remove_state("ghost") is False


def test_fsm_remove_cleans_incoming_transitions():
    fsm = AnimStateMachine()
    a = fsm.add_state("A")
    fsm.add_state("B")
    a.transitions.append(AnimTransition(target_state="B"))
    fsm.remove_state("B")
    assert all(t.target_state != "B" for t in a.transitions)


def test_fsm_remove_default_reassigns():
    fsm = AnimStateMachine()
    fsm.add_state("A")
    fsm.add_state("B")
    fsm.remove_state("A")
    assert fsm.default_state == "B"


def test_fsm_remove_last_clears_default():
    fsm = AnimStateMachine()
    fsm.add_state("A")
    fsm.remove_state("A")
    assert fsm.default_state == ""


def test_fsm_round_trip():
    fsm = AnimStateMachine(name="Player", mode="3d")
    fsm.add_state("Idle")
    fsm.add_state("Run")
    fsm.parameters.append(
        AnimParameter(
            name="speed", value_type=TypeRef(ValueType.F32), default=1.0
        )
    )
    fsm2 = AnimStateMachine.from_dict(fsm.to_dict())
    assert fsm2.name == "Player"
    assert fsm2.mode == "3d"
    assert fsm2.state_count == 2
    assert fsm2.parameters[0].name == "speed"


def test_fsm_mode_timeline_preserved():
    fsm = AnimStateMachine(mode="timeline")
    assert AnimStateMachine.from_dict(fsm.to_dict()).mode == "timeline"


def test_fsm_copy_is_independent():
    fsm = AnimStateMachine()
    fsm.add_state("Idle")
    clone = fsm.copy()
    clone.states[0].name = "Changed"
    assert fsm.states[0].name == "Idle"


def test_fsm_equality_by_content():
    a = AnimStateMachine(name="X")
    a.add_state("Idle")
    b = a.copy()
    assert a == b


def test_fsm_identity_is_part_of_serialized_content():
    a = AnimStateMachine(name="X")
    a.add_state("Idle")
    b = AnimStateMachine(name="X")
    b.add_state("Idle")
    assert a != b


def test_fsm_equality_ignores_file_path():
    a = AnimStateMachine(name="X")
    b = AnimStateMachine(name="X")
    a.file_path = "/tmp/a.animfsm"
    b.file_path = "/tmp/b.animfsm"
    assert a == b


def test_fsm_inequality():
    a = AnimStateMachine(name="X")
    b = AnimStateMachine(name="Y")
    assert a != b


def test_fsm_from_dict_rejects_bad_parameters():
    document = AnimStateMachine().to_dict()
    document["parameters"] = ["bad"]
    with pytest.raises(TypeError, match="animation parameter"):
        AnimStateMachine.from_dict(document)


def test_fsm_save_load_round_trip(tmp_path):
    path = str(tmp_path / "player.animfsm")
    fsm = AnimStateMachine(name="P", mode="3d")
    fsm.add_state("Idle")
    assert fsm.save(path) is True
    loaded = AnimStateMachine.load(path)
    assert loaded is not None
    assert loaded.mode == "3d"
    assert loaded.get_state("Idle") is not None


def test_fsm_load_missing_returns_none(tmp_path):
    assert AnimStateMachine.load(str(tmp_path / "nope.animfsm")) is None


def test_fsm_load_sets_name_from_filename(tmp_path):
    path = tmp_path / "Boss.animfsm"
    AnimStateMachine().save(str(path))
    assert AnimStateMachine.load(str(path)).name == "Boss"


def test_fsm_save_without_target_fails():
    assert AnimStateMachine().save() is False
