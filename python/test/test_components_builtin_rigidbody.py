"""Tests for Infernux.components.builtin.rigidbody — Rigidbody wrapper (real C++ backend)."""

from __future__ import annotations

import pytest

from Infernux.lib import Vector3, quatf, ForceMode as CppForceMode
from Infernux.components.builtin.rigidbody import (
    CollisionDetectionMode,
    Rigidbody,
    RigidbodyConstraints,
    RigidbodyInterpolation,
)


# ══════════════════════════════════════════════════════════════════════
# Enum values
# ══════════════════════════════════════════════════════════════════════

class TestRigidbodyEnums:
    def test_collision_detection_mode_members(self):
        assert CollisionDetectionMode.Discrete.value == 0
        assert CollisionDetectionMode.Continuous.value == 1
        assert CollisionDetectionMode.ContinuousDynamic.value == 2

    def test_interpolation_members(self):
        assert RigidbodyInterpolation.None_.value == 0
        assert RigidbodyInterpolation.Interpolate.value == 1

    def test_constraints_flags(self):
        flags = RigidbodyConstraints
        assert flags.FreezePositionX != 0
        assert flags.FreezeRotationY != 0


# ══════════════════════════════════════════════════════════════════════
# CollisionDetectionMode → backend mapping
# ══════════════════════════════════════════════════════════════════════

class TestCollisionDetectionModeMapping:
    @pytest.fixture
    def _rb(self, cpp_rigidbody):
        rb = Rigidbody()
        rb._cpp_component = cpp_rigidbody
        return rb

    def test_discrete(self, _rb, cpp_rigidbody):
        _rb.collision_detection_mode = CollisionDetectionMode.Discrete
        assert cpp_rigidbody.collision_detection_mode == int(CollisionDetectionMode.Discrete)

    def test_continuous(self, _rb, cpp_rigidbody):
        _rb.collision_detection_mode = CollisionDetectionMode.Continuous
        assert cpp_rigidbody.collision_detection_mode == int(CollisionDetectionMode.Continuous)

    def test_continuous_dynamic(self, _rb, cpp_rigidbody):
        _rb.collision_detection_mode = CollisionDetectionMode.ContinuousDynamic
        assert cpp_rigidbody.collision_detection_mode == int(CollisionDetectionMode.ContinuousDynamic)

    def test_read_back_is_enum(self, _rb, cpp_rigidbody):
        cpp_rigidbody.collision_detection_mode = 1
        assert _rb.collision_detection_mode is CollisionDetectionMode.Continuous

    def test_unsupported_mode_is_rejected(self, cpp_rigidbody):
        with pytest.raises(ValueError):
            cpp_rigidbody.collision_detection_mode = 3


# ══════════════════════════════════════════════════════════════════════
# Property round-trip (real C++ Rigidbody)
# ══════════════════════════════════════════════════════════════════════

class TestPropertyRoundTrip:
    def test_mass_drag_gravity(self, cpp_rigidbody):
        rb = Rigidbody()
        rb._cpp_component = cpp_rigidbody

        rb.mass = 3.5
        rb.drag = 0.25
        rb.use_gravity = False

        assert cpp_rigidbody.mass == pytest.approx(3.5)
        assert cpp_rigidbody.drag == pytest.approx(0.25)
        assert cpp_rigidbody.use_gravity is False

    def test_enum_properties_round_trip(self, cpp_rigidbody):
        rb = Rigidbody()
        rb._cpp_component = cpp_rigidbody

        rb.collision_detection_mode = CollisionDetectionMode.ContinuousDynamic
        rb.interpolation = RigidbodyInterpolation.None_

        assert cpp_rigidbody.collision_detection_mode == int(CollisionDetectionMode.ContinuousDynamic)
        assert cpp_rigidbody.interpolation == int(RigidbodyInterpolation.None_)

        cpp_rigidbody.collision_detection_mode = 2
        cpp_rigidbody.interpolation = 1
        assert rb.collision_detection_mode is CollisionDetectionMode.ContinuousDynamic
        assert rb.interpolation is RigidbodyInterpolation.Interpolate

    def test_zero_drag_is_preserved(self, cpp_rigidbody):
        cpp_rigidbody.drag = 0.0
        cpp_rigidbody.angular_drag = 0.0
        assert cpp_rigidbody.drag == pytest.approx(0.0)
        assert cpp_rigidbody.angular_drag == pytest.approx(0.0)


# ══════════════════════════════════════════════════════════════════════
# Constraints helpers
# ══════════════════════════════════════════════════════════════════════

class TestConstraintsHelpers:
    def test_add_remove_has(self, cpp_rigidbody):
        rb = Rigidbody()
        rb._cpp_component = cpp_rigidbody

        rb.constraints_flags = (
            RigidbodyConstraints.FreezePositionX | RigidbodyConstraints.FreezeRotationY
        )
        assert rb.has_constraint(RigidbodyConstraints.FreezePositionX)
        assert rb.has_constraint(RigidbodyConstraints.FreezeRotationY)
        assert not rb.has_constraint(RigidbodyConstraints.FreezePositionZ)

        rb.add_constraint(RigidbodyConstraints.FreezeRotationZ)
        assert rb.has_constraint(RigidbodyConstraints.FreezeRotationZ)

        rb.remove_constraint(RigidbodyConstraints.FreezePositionX)
        assert not rb.has_constraint(RigidbodyConstraints.FreezePositionX)

    def test_freeze_rotation(self, cpp_rigidbody):
        rb = Rigidbody()
        rb._cpp_component = cpp_rigidbody
        assert rb.freeze_rotation is False
        rb.freeze_rotation = True
        assert cpp_rigidbody.freeze_rotation is True


# ══════════════════════════════════════════════════════════════════════
# Velocity & force methods (real C++ Rigidbody)
# ══════════════════════════════════════════════════════════════════════

class TestVelocityAndForce:
    def test_velocity_is_vector3(self, cpp_rigidbody):
        rb = Rigidbody()
        rb._cpp_component = cpp_rigidbody
        v = rb.velocity
        assert hasattr(v, 'x') and hasattr(v, 'y') and hasattr(v, 'z')

    def test_set_velocity_accepts_tuple(self, cpp_rigidbody):
        rb = Rigidbody()
        rb._cpp_component = cpp_rigidbody
        rb.velocity = (1, 2, 3)  # should not crash
        rb.angular_velocity = (4, 5, 6)  # should not crash

    def test_force_methods_do_not_crash(self, cpp_rigidbody):
        rb = Rigidbody()
        rb._cpp_component = cpp_rigidbody
        rb.add_force((1, 2, 3))
        rb.add_torque((4, 5, 6), CppForceMode.Impulse)
        rb.add_force_at_position((7, 8, 9), (1, 1, 1), CppForceMode.Acceleration)

    def test_kinematic_moves_reject_dynamic_rigidbody(self, cpp_rigidbody):
        rb = Rigidbody()
        rb._cpp_component = cpp_rigidbody
        with pytest.raises(RuntimeError, match="requires a kinematic Rigidbody"):
            rb.move_position((3, 2, 1))
        with pytest.raises(RuntimeError, match="requires a kinematic Rigidbody"):
            rb.move_rotation(quatf(0.0, 0.0, 0.0, 1.0))


# ══════════════════════════════════════════════════════════════════════
# Sleep API (real C++ Rigidbody)
# ══════════════════════════════════════════════════════════════════════

class TestSleepAPI:
    def test_sleep_api(self, cpp_rigidbody):
        rb = Rigidbody()
        rb._cpp_component = cpp_rigidbody
        sleeping = rb.is_sleeping()
        assert isinstance(sleeping, bool)
        rb.wake_up()
        rb.sleep()


# ══════════════════════════════════════════════════════════════════════
# Detached wrapper failures
# ══════════════════════════════════════════════════════════════════════

class TestDetachedWrapperFailures:
    def test_native_state_access_requires_binding(self):
        rb = Rigidbody()
        operations = (
            lambda: rb.mass,
            lambda: setattr(rb, "mass", 2.0),
            lambda: rb.freeze_rotation,
            lambda: rb.is_sleeping(),
            lambda: rb.rotation,
            lambda: rb.add_force((1, 2, 3)),
            lambda: rb.add_torque((1, 2, 3)),
            lambda: rb.add_force_at_position((1, 2, 3), (0, 0, 0)),
            lambda: rb.move_position((1, 2, 3)),
            lambda: rb.move_rotation((0.0, 0.0, 0.0, 1.0)),
            lambda: rb.wake_up(),
            lambda: rb.sleep(),
        )
        for operation in operations:
            with pytest.raises(ReferenceError):
                operation()


# ══════════════════════════════════════════════════════════════════════
# Read-only world info (real C++ Rigidbody)
# ══════════════════════════════════════════════════════════════════════

class TestWorldInfo:
    def test_position_is_vector3(self, cpp_rigidbody):
        rb = Rigidbody()
        rb._cpp_component = cpp_rigidbody
        pos = rb.position
        assert hasattr(pos, 'x') and hasattr(pos, 'y')

    def test_rotation_is_tuple_like(self, cpp_rigidbody):
        rb = Rigidbody()
        rb._cpp_component = cpp_rigidbody
        rot = rb.rotation
        assert len(rot) == 4
