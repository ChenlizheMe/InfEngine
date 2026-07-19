"""Unit tests for the AnimationTimeline data model (``.animtimeline``).

Covers keyframe (de)serialization, interpolation curves, transform sampling at
boundaries / interior / interpolated points, apply modes, and file round-trips.
Pure-logic tests — no renderer required.
"""
from __future__ import annotations

import json
import math

import pytest

from Infernux.core.animation_timeline import (
    AnimationTimeline,
    TimelineKeyframe,
    INTERP_CONSTANT,
    INTERP_LINEAR,
    INTERP_EASE_IN,
    INTERP_EASE_OUT,
    INTERP_EASE_IN_OUT,
    INTERP_MODES,
    APPLY_ADDITIVE,
    APPLY_ABSOLUTE,
    APPLY_MODES,
    _apply_interp,
)


# ── module constants ────────────────────────────────────────────────────────

def test_interp_modes_membership():
    assert set(INTERP_MODES) == {
        INTERP_CONSTANT, INTERP_LINEAR, INTERP_EASE_IN, INTERP_EASE_OUT, INTERP_EASE_IN_OUT
    }


def test_interp_modes_count():
    assert len(INTERP_MODES) == 5


def test_apply_modes_membership():
    assert set(APPLY_MODES) == {APPLY_ADDITIVE, APPLY_ABSOLUTE}


def test_apply_mode_values():
    assert APPLY_ADDITIVE == "additive"
    assert APPLY_ABSOLUTE == "absolute"


# ── _apply_interp curve ─────────────────────────────────────────────────────

@pytest.mark.parametrize("mode", INTERP_MODES)
def test_interp_clamps_below_zero(mode):
    assert _apply_interp(mode, -0.5) == 0.0


@pytest.mark.parametrize("mode", INTERP_MODES)
def test_interp_clamps_above_one(mode):
    assert _apply_interp(mode, 1.5) == 1.0


@pytest.mark.parametrize("mode", INTERP_MODES)
def test_interp_endpoints_exact(mode):
    assert _apply_interp(mode, 0.0) == 0.0
    assert _apply_interp(mode, 1.0) == 1.0


def test_interp_linear_midpoint():
    assert _apply_interp(INTERP_LINEAR, 0.5) == pytest.approx(0.5)


def test_interp_linear_is_identity():
    for u in (0.1, 0.25, 0.5, 0.75, 0.9):
        assert _apply_interp(INTERP_LINEAR, u) == pytest.approx(u)


def test_interp_constant_holds_previous():
    for u in (0.01, 0.3, 0.5, 0.99):
        assert _apply_interp(INTERP_CONSTANT, u) == 0.0


def test_interp_ease_in_quadratic():
    assert _apply_interp(INTERP_EASE_IN, 0.5) == pytest.approx(0.25)
    assert _apply_interp(INTERP_EASE_IN, 0.2) == pytest.approx(0.04)


def test_interp_ease_out_quadratic():
    assert _apply_interp(INTERP_EASE_OUT, 0.5) == pytest.approx(0.75)
    assert _apply_interp(INTERP_EASE_OUT, 0.2) == pytest.approx(0.36)


def test_interp_ease_in_out_smoothstep():
    assert _apply_interp(INTERP_EASE_IN_OUT, 0.5) == pytest.approx(0.5)
    assert _apply_interp(INTERP_EASE_IN_OUT, 0.25) == pytest.approx(0.15625)


def test_interp_ease_in_is_below_linear():
    assert _apply_interp(INTERP_EASE_IN, 0.5) < 0.5


def test_interp_ease_out_is_above_linear():
    assert _apply_interp(INTERP_EASE_OUT, 0.5) > 0.5


def test_interp_unknown_mode_falls_back_to_linear():
    assert _apply_interp("nonsense", 0.5) == pytest.approx(0.5)


def test_interp_monotonic_for_eases():
    for mode in (INTERP_LINEAR, INTERP_EASE_IN, INTERP_EASE_OUT, INTERP_EASE_IN_OUT):
        prev = -1.0
        for i in range(11):
            v = _apply_interp(mode, i / 10.0)
            assert v >= prev - 1e-9
            prev = v


# ── TimelineKeyframe ────────────────────────────────────────────────────────

def test_keyframe_defaults():
    k = TimelineKeyframe()
    assert k.time == 0.0
    assert k.position == [0.0, 0.0, 0.0]
    assert k.rotation == [0.0, 0.0, 0.0]
    assert k.scale == [1.0, 1.0, 1.0]
    assert k.interp == INTERP_LINEAR


def test_keyframe_default_lists_are_independent():
    a = TimelineKeyframe()
    b = TimelineKeyframe()
    a.position[0] = 9.0
    assert b.position[0] == 0.0


def test_keyframe_to_dict_keys():
    d = TimelineKeyframe(time=1.5).to_dict()
    assert set(d) == {"time", "position", "rotation", "scale", "interp"}


def test_keyframe_round_trip():
    k = TimelineKeyframe(time=2.0, position=[1, 2, 3], rotation=[10, 20, 30],
                         scale=[2, 2, 2], interp=INTERP_EASE_IN)
    k2 = TimelineKeyframe.from_dict(k.to_dict())
    assert k2.time == 2.0
    assert k2.position == [1.0, 2.0, 3.0]
    assert k2.rotation == [10.0, 20.0, 30.0]
    assert k2.scale == [2.0, 2.0, 2.0]
    assert k2.interp == INTERP_EASE_IN


def test_keyframe_from_dict_invalid_interp_is_rejected():
    document = TimelineKeyframe().to_dict()
    document["interp"] = "bogus"
    with pytest.raises(ValueError, match="interpolation"):
        TimelineKeyframe.from_dict(document)


def test_keyframe_from_dict_missing_fields_is_rejected():
    with pytest.raises(ValueError, match="fields mismatch"):
        TimelineKeyframe.from_dict({})


def test_keyframe_from_dict_short_vector_is_rejected():
    document = TimelineKeyframe().to_dict()
    document["position"] = [1, 2]
    with pytest.raises(TypeError, match="three finite numbers"):
        TimelineKeyframe.from_dict(document)


def test_keyframe_from_dict_non_numeric_vector_is_rejected():
    document = TimelineKeyframe().to_dict()
    document["scale"] = ["a", "b", "c"]
    with pytest.raises(TypeError, match="three finite numbers"):
        TimelineKeyframe.from_dict(document)


def test_keyframe_from_dict_coerces_ints_to_float():
    document = TimelineKeyframe().to_dict()
    document["position"] = [1, 2, 3]
    k = TimelineKeyframe.from_dict(document)
    assert all(isinstance(v, float) for v in k.position)


# ── AnimationTimeline basics ────────────────────────────────────────────────

def test_timeline_defaults():
    tl = AnimationTimeline()
    assert tl.name == ""
    assert tl.duration == 2.0
    assert tl.apply_mode == APPLY_ADDITIVE
    assert tl.keyframes == []
    assert tl.file_path == ""


def test_timeline_sorted_keys():
    tl = AnimationTimeline(keyframes=[
        TimelineKeyframe(time=2.0),
        TimelineKeyframe(time=0.5),
        TimelineKeyframe(time=1.0),
    ])
    times = [k.time for k in tl.sorted_keys()]
    assert times == [0.5, 1.0, 2.0]


def test_timeline_sorted_keys_does_not_mutate():
    tl = AnimationTimeline(keyframes=[TimelineKeyframe(time=2.0), TimelineKeyframe(time=0.0)])
    tl.sorted_keys()
    assert [k.time for k in tl.keyframes] == [2.0, 0.0]


# ── sample() ────────────────────────────────────────────────────────────────

def test_sample_empty_returns_none():
    assert AnimationTimeline().sample(0.5) is None


def test_sample_single_key_any_time():
    tl = AnimationTimeline(keyframes=[TimelineKeyframe(time=1.0, position=[5, 6, 7])])
    for t in (-1.0, 1.0, 99.0):
        pos, _rot, _scl = tl.sample(t)
        assert pos == [5.0, 6.0, 7.0]


def test_sample_before_first_key_clamps():
    tl = AnimationTimeline(keyframes=[
        TimelineKeyframe(time=1.0, position=[1, 0, 0]),
        TimelineKeyframe(time=2.0, position=[2, 0, 0]),
    ])
    pos, _r, _s = tl.sample(0.0)
    assert pos == [1.0, 0.0, 0.0]


def test_sample_after_last_key_clamps():
    tl = AnimationTimeline(keyframes=[
        TimelineKeyframe(time=1.0, position=[1, 0, 0]),
        TimelineKeyframe(time=2.0, position=[2, 0, 0]),
    ])
    pos, _r, _s = tl.sample(9.0)
    assert pos == [2.0, 0.0, 0.0]


def test_sample_linear_midpoint():
    tl = AnimationTimeline(keyframes=[
        TimelineKeyframe(time=0.0, position=[0, 0, 0]),
        TimelineKeyframe(time=2.0, position=[10, 0, 0], interp=INTERP_LINEAR),
    ])
    pos, _r, _s = tl.sample(1.0)
    assert pos[0] == pytest.approx(5.0)


def test_sample_uses_target_key_interp():
    # b.interp = constant → holds A's value until B reached.
    tl = AnimationTimeline(keyframes=[
        TimelineKeyframe(time=0.0, position=[0, 0, 0]),
        TimelineKeyframe(time=2.0, position=[10, 0, 0], interp=INTERP_CONSTANT),
    ])
    pos, _r, _s = tl.sample(1.5)
    assert pos[0] == pytest.approx(0.0)


def test_sample_ease_in_midpoint():
    tl = AnimationTimeline(keyframes=[
        TimelineKeyframe(time=0.0, position=[0, 0, 0]),
        TimelineKeyframe(time=1.0, position=[100, 0, 0], interp=INTERP_EASE_IN),
    ])
    pos, _r, _s = tl.sample(0.5)
    assert pos[0] == pytest.approx(25.0)


def test_sample_interior_key_returns_that_key():
    tl = AnimationTimeline(keyframes=[
        TimelineKeyframe(time=0.0, position=[0, 0, 0]),
        TimelineKeyframe(time=1.0, position=[5, 0, 0]),
        TimelineKeyframe(time=2.0, position=[9, 0, 0]),
    ])
    pos, _r, _s = tl.sample(1.0)
    assert pos[0] == pytest.approx(5.0)


def test_sample_interpolates_rotation_and_scale():
    tl = AnimationTimeline(keyframes=[
        TimelineKeyframe(time=0.0, rotation=[0, 0, 0], scale=[1, 1, 1]),
        TimelineKeyframe(time=1.0, rotation=[90, 0, 0], scale=[3, 3, 3], interp=INTERP_LINEAR),
    ])
    _p, rot, scl = tl.sample(0.5)
    assert rot[0] == pytest.approx(45.0)
    assert scl[0] == pytest.approx(2.0)


def test_sample_unsorted_keyframes_still_correct():
    tl = AnimationTimeline(keyframes=[
        TimelineKeyframe(time=2.0, position=[20, 0, 0]),
        TimelineKeyframe(time=0.0, position=[0, 0, 0]),
    ])
    pos, _r, _s = tl.sample(1.0)
    assert pos[0] == pytest.approx(10.0)


def test_sample_zero_span_coincident_keys():
    tl = AnimationTimeline(keyframes=[
        TimelineKeyframe(time=1.0, position=[1, 0, 0]),
        TimelineKeyframe(time=1.0, position=[2, 0, 0]),
    ])
    result = tl.sample(1.0)
    assert result is not None  # must not divide by zero


def test_sample_returns_copies_not_references():
    key = TimelineKeyframe(time=0.0, position=[1, 2, 3])
    tl = AnimationTimeline(keyframes=[key])
    pos, _r, _s = tl.sample(0.0)
    pos[0] = 999.0
    assert key.position[0] == 1.0


# ── serialization ───────────────────────────────────────────────────────────

def test_timeline_to_dict_keys():
    d = AnimationTimeline().to_dict()
    assert set(d) == {"name", "duration", "apply_mode", "keyframes"}


def test_timeline_to_dict_excludes_file_path():
    tl = AnimationTimeline()
    tl.file_path = "/tmp/x.animtimeline"
    assert "file_path" not in tl.to_dict()


def test_timeline_round_trip():
    tl = AnimationTimeline(name="Spin", duration=3.5, apply_mode=APPLY_ABSOLUTE,
                           keyframes=[TimelineKeyframe(time=0.0), TimelineKeyframe(time=3.5)])
    tl2 = AnimationTimeline.from_dict(tl.to_dict())
    assert tl2.name == "Spin"
    assert tl2.duration == 3.5
    assert tl2.apply_mode == APPLY_ABSOLUTE
    assert len(tl2.keyframes) == 2


def test_timeline_from_dict_invalid_apply_mode_is_rejected():
    document = AnimationTimeline().to_dict()
    document["apply_mode"] = "weird"
    with pytest.raises(ValueError, match="apply_mode"):
        AnimationTimeline.from_dict(document)


def test_timeline_from_dict_rejects_noncanonical_keyframes():
    document = AnimationTimeline().to_dict()
    document["keyframes"] = ["bad"]
    with pytest.raises(ValueError, match="keyframe fields mismatch"):
        AnimationTimeline.from_dict(document)


def test_timeline_from_dict_rejects_missing_schema():
    with pytest.raises(ValueError, match="fields mismatch"):
        AnimationTimeline.from_dict({})


def test_timeline_save_load_round_trip(tmp_path):
    path = str(tmp_path / "test.animtimeline")
    tl = AnimationTimeline(duration=4.0, keyframes=[TimelineKeyframe(time=1.0, position=[1, 2, 3])])
    assert tl.save(path) is True
    loaded = AnimationTimeline.load(path)
    assert loaded is not None
    assert loaded.duration == 4.0
    assert loaded.keyframes[0].position == [1.0, 2.0, 3.0]


def test_timeline_save_uses_file_path_when_no_arg(tmp_path):
    path = str(tmp_path / "fp.animtimeline")
    tl = AnimationTimeline()
    tl.file_path = path
    assert tl.save() is True


def test_timeline_save_without_target_fails():
    assert AnimationTimeline().save() is False


def test_timeline_load_missing_returns_none(tmp_path):
    assert AnimationTimeline.load(str(tmp_path / "nope.animtimeline")) is None


def test_timeline_load_empty_path_returns_none():
    assert AnimationTimeline.load("") is None


def test_timeline_load_invalid_json_returns_none(tmp_path):
    path = tmp_path / "bad.animtimeline"
    path.write_text("{ not valid json ", encoding="utf-8")
    assert AnimationTimeline.load(str(path)) is None


def test_timeline_load_non_object_json_returns_none(tmp_path):
    path = tmp_path / "arr.animtimeline"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert AnimationTimeline.load(str(path)) is None


def test_timeline_load_sets_name_from_filename(tmp_path):
    path = tmp_path / "MyTimeline.animtimeline"
    AnimationTimeline(duration=1.0).save(str(path))
    loaded = AnimationTimeline.load(str(path))
    assert loaded.name == "MyTimeline"


def test_timeline_saved_file_is_valid_json(tmp_path):
    path = tmp_path / "j.animtimeline"
    AnimationTimeline(name="x").save(str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["name"] == "x"
