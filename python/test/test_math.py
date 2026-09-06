"""Tests for Infernux.mathf (Mathf utility class) and Infernux.jit kernels."""

from __future__ import annotations

import math

import pytest

from Infernux.mathf import Mathf
from Infernux.lib import Vector2, Vector3, quatf, vec4f


class TestVector3Validation:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (Vector2(1, 2), (1.0, 2.0)),
            (Vector3(1, 2, 3), (1.0, 2.0, 3.0)),
            (vec4f(1, 2, 3, 4), (1.0, 2.0, 3.0, 4.0)),
            (quatf(1, 2, 3, 4), (1.0, 2.0, 3.0, 4.0)),
        ],
    )
    def test_native_vectors_iterate_without_pointer_backed_state(self, value, expected):
        assert tuple(value) == expected

    def test_constructor_and_component_writes_reject_non_finite_values(self):
        with pytest.raises(ValueError, match="finite"):
            Vector3(float("nan"), 0, 0)
        value = Vector3(1, 2, 3)
        with pytest.raises(ValueError, match="finite"):
            value.x = float("inf")

    def test_smooth_damp_rejects_invalid_timing(self):
        with pytest.raises(ValueError, match="smooth_time"):
            Vector3.smooth_damp(Vector3(), Vector3(1, 0, 0), Vector3(), 0.0, float("inf"), 0.02)

    @pytest.mark.parametrize(
        "factory",
        [
            lambda: Vector2(float("nan"), 0),
            lambda: vec4f(0, 0, float("inf"), 0),
        ],
    )
    def test_other_vector_dimensions_share_finite_contract(self, factory):
        with pytest.raises(ValueError, match="finite"):
            factory()


# ═══════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════

class TestMathfConstants:
    def test_pi(self):
        assert Mathf.PI == pytest.approx(math.pi)

    def test_tau(self):
        assert Mathf.TAU == pytest.approx(math.tau)

    def test_infinity(self):
        assert Mathf.Infinity == float("inf")

    def test_negative_infinity(self):
        assert Mathf.NegativeInfinity == float("-inf")

    def test_epsilon_positive(self):
        assert Mathf.Epsilon > 0

    def test_deg2rad(self):
        assert Mathf.Deg2Rad == pytest.approx(math.pi / 180.0)

    def test_rad2deg(self):
        assert Mathf.Rad2Deg == pytest.approx(180.0 / math.pi)


# ═══════════════════════════════════════════════════════════════════════════
# Clamping / Interpolation
# ═══════════════════════════════════════════════════════════════════════════

class TestClamping:
    def test_clamp_within_range(self):
        assert Mathf.clamp(5, 0, 10) == 5

    def test_clamp_below_min(self):
        assert Mathf.clamp(-1, 0, 10) == 0

    def test_clamp_above_max(self):
        assert Mathf.clamp(15, 0, 10) == 10

    def test_clamp01_within(self):
        assert Mathf.clamp01(0.5) == 0.5

    def test_clamp01_below(self):
        assert Mathf.clamp01(-0.3) == 0.0

    def test_clamp01_above(self):
        assert Mathf.clamp01(1.7) == 1.0


class TestInterpolation:
    def test_lerp_at_zero(self):
        assert Mathf.lerp(0, 10, 0) == 0

    def test_lerp_at_one(self):
        assert Mathf.lerp(0, 10, 1) == 10

    def test_lerp_at_half(self):
        assert Mathf.lerp(0, 10, 0.5) == pytest.approx(5.0)

    def test_lerp_clamps_t(self):
        assert Mathf.lerp(0, 10, 2.0) == 10
        assert Mathf.lerp(0, 10, -1.0) == 0

    def test_lerp_unclamped(self):
        assert Mathf.lerp_unclamped(0, 10, 2.0) == pytest.approx(20.0)

    def test_inverse_lerp(self):
        assert Mathf.inverse_lerp(0, 10, 5) == pytest.approx(0.5)
        assert Mathf.inverse_lerp(0, 10, -1) == 0.0
        assert Mathf.inverse_lerp(0, 10, 15) == 1.0

    def test_inverse_lerp_degenerate(self):
        assert Mathf.inverse_lerp(5, 5, 5) == 0.0

    def test_move_towards_under_delta(self):
        assert Mathf.move_towards(0, 10, 100) == 10

    def test_move_towards_over_delta(self):
        assert Mathf.move_towards(0, 10, 3) == 3

    def test_smooth_step_endpoints(self):
        assert Mathf.smooth_step(0, 1, 0) == 0
        assert Mathf.smooth_step(0, 1, 1) == 1

    def test_smooth_step_monotonic(self):
        prev = 0.0
        for i in range(11):
            t = i / 10.0
            val = Mathf.smooth_step(0, 1, t)
            assert val >= prev
            prev = val


class TestSmoothDamp:
    def test_returns_tuple(self):
        result = Mathf.smooth_damp(0, 10, 0, 0.3, delta_time=0.016)
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_moves_towards_target(self):
        pos, vel = Mathf.smooth_damp(0, 10, 0, 0.3, delta_time=0.016)
        assert pos > 0
        assert vel > 0

    def test_converges_over_many_steps(self):
        pos, vel = 0.0, 0.0
        for _ in range(300):
            pos, vel = Mathf.smooth_damp(pos, 10.0, vel, 0.3, delta_time=0.016)
        assert pos == pytest.approx(10.0, abs=0.01)


# ═══════════════════════════════════════════════════════════════════════════
# Angle helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestAngleHelpers:
    def test_delta_angle_same(self):
        assert Mathf.delta_angle(90, 90) == pytest.approx(0)

    def test_delta_angle_wrap(self):
        assert Mathf.delta_angle(350, 10) == pytest.approx(20)

    def test_delta_angle_negative_wrap(self):
        assert Mathf.delta_angle(10, 350) == pytest.approx(-20)

    def test_lerp_angle_shortest_path(self):
        assert Mathf.lerp_angle(350, 10, 0.5) == pytest.approx(360)

    def test_move_towards_angle(self):
        result = Mathf.move_towards_angle(0, 90, 30)
        assert result == pytest.approx(30)


# ═══════════════════════════════════════════════════════════════════════════
# Repeating patterns
# ═══════════════════════════════════════════════════════════════════════════

class TestRepeating:
    def test_repeat(self):
        assert Mathf.repeat(3.5, 2.0) == pytest.approx(1.5)

    def test_repeat_zero_length(self):
        assert Mathf.repeat(5.0, 0.0) == 0.0

    def test_ping_pong(self):
        assert Mathf.ping_pong(1.5, 2.0) == pytest.approx(1.5)
        assert Mathf.ping_pong(3.0, 2.0) == pytest.approx(1.0)


# ═══════════════════════════════════════════════════════════════════════════
# Comparison
# ═══════════════════════════════════════════════════════════════════════════

class TestComparison:
    def test_approximately_equal(self):
        assert Mathf.approximately(1.0, 1.0 + 1e-8) is True

    def test_approximately_not_equal(self):
        assert Mathf.approximately(1.0, 2.0) is False

    def test_sign_positive(self):
        assert Mathf.sign(5.0) == 1.0

    def test_sign_negative(self):
        assert Mathf.sign(-3.0) == -1.0

    def test_sign_zero(self):
        assert Mathf.sign(0.0) == 1.0


# ═══════════════════════════════════════════════════════════════════════════
# Transcendental wrappers
# ═══════════════════════════════════════════════════════════════════════════

class TestTranscendental:
    def test_sin(self):
        assert Mathf.sin(0) == pytest.approx(0)
        assert Mathf.sin(math.pi / 2) == pytest.approx(1)

    def test_cos(self):
        assert Mathf.cos(0) == pytest.approx(1)

    def test_tan(self):
        assert Mathf.tan(0) == pytest.approx(0)

    def test_asin(self):
        assert Mathf.asin(1) == pytest.approx(math.pi / 2)

    def test_acos(self):
        assert Mathf.acos(1) == pytest.approx(0)

    def test_atan(self):
        assert Mathf.atan(1) == pytest.approx(math.pi / 4)

    def test_atan2(self):
        assert Mathf.atan2(1, 1) == pytest.approx(math.pi / 4)

    def test_sqrt(self):
        assert Mathf.sqrt(4) == pytest.approx(2)

    def test_sqrt_negative_clamped(self):
        assert Mathf.sqrt(-1) == 0.0

    def test_pow(self):
        assert Mathf.pow(2, 10) == pytest.approx(1024)

    def test_exp(self):
        assert Mathf.exp(0) == pytest.approx(1)

    def test_log(self):
        assert Mathf.log(1) == pytest.approx(0)

    def test_log_nonpositive(self):
        assert Mathf.log(0) == float("-inf")

    def test_log10(self):
        assert Mathf.log10(100) == pytest.approx(2)

    def test_log10_nonpositive(self):
        assert Mathf.log10(0) == float("-inf")


# ═══════════════════════════════════════════════════════════════════════════
# Rounding / Abs / Min / Max
# ═══════════════════════════════════════════════════════════════════════════

class TestRounding:
    def test_abs(self):
        assert Mathf.abs(-5.5) == 5.5

    def test_min(self):
        assert Mathf.min(3, 1, 2) == 1

    def test_max(self):
        assert Mathf.max(3, 1, 2) == 3

    def test_floor(self):
        assert Mathf.floor(1.7) == 1.0

    def test_ceil(self):
        assert Mathf.ceil(1.1) == 2.0

    def test_round(self):
        assert Mathf.round(1.5) == 2.0

    def test_floor_to_int(self):
        assert Mathf.floor_to_int(1.9) == 1
        assert isinstance(Mathf.floor_to_int(1.9), int)

    def test_ceil_to_int(self):
        assert Mathf.ceil_to_int(1.1) == 2

    def test_round_to_int(self):
        assert Mathf.round_to_int(2.7) == 3


# ═══════════════════════════════════════════════════════════════════════════
# Power-of-two helpers
# ═══════════════════════════════════════════════════════════════════════════

class TestPowerOfTwo:
    def test_is_power_of_two(self):
        assert Mathf.is_power_of_two(1) is True
        assert Mathf.is_power_of_two(2) is True
        assert Mathf.is_power_of_two(4) is True
        assert Mathf.is_power_of_two(3) is False
        assert Mathf.is_power_of_two(0) is False

    def test_next_power_of_two(self):
        assert Mathf.next_power_of_two(3) == 4
        assert Mathf.next_power_of_two(4) == 4
        assert Mathf.next_power_of_two(5) == 8
        assert Mathf.next_power_of_two(0) == 1

    def test_closest_power_of_two(self):
        assert Mathf.closest_power_of_two(3) == 4
        assert Mathf.closest_power_of_two(5) == 4
        assert Mathf.closest_power_of_two(7) == 8



