"""Integration tests — Physics simulation with real Jolt backend (real engine)."""
from __future__ import annotations

import pytest

from Infernux.components import InxComponent
from Infernux.engine.scene_document_transaction import SceneDocumentTransaction
from Infernux.timing import Time
from Infernux.lib import BoxCollider as NativeBoxCollider
from Infernux.lib import MeshCollider as NativeMeshCollider
from Infernux.lib import (
    ForceMode,
    CollisionDetectionMode,
    EngineConfig,
    InxPhysicMaterial,
    Physics,
    PrimitiveType,
    RigidbodyConstraints,
    SceneManager,
    Vector3,
    quatf,
)


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════

def _step_frames(n: int = 60, dt: float = 1.0 / 60.0):
    """Advance the physics simulation by *n* frames."""
    sm = SceneManager.instance()
    for _ in range(n):
        sm.step(dt)


def _assign_physic_material(collider, *, friction=0.4, bounciness=0.0,
                            friction_combine=0, bounce_combine=0):
    material = InxPhysicMaterial()
    material.friction = friction
    material.bounciness = bounciness
    material.friction_combine = friction_combine
    material.bounce_combine = bounce_combine
    collider.physic_material = material
    return material


def _make_ground(scene):
    """Create a large static ground plane (BoxCollider, no Rigidbody)."""
    ground = scene.create_game_object("Ground")
    ground.transform.position = Vector3(0, 0, 0)
    ground.transform.local_scale = Vector3(100, 1, 100)
    ground.add_component("BoxCollider")
    return ground


def _make_ball(scene, *, pos=None, mass=1.0, radius=0.5):
    """Create a dynamic sphere with Rigidbody + SphereCollider."""
    ball = scene.create_game_object("Ball")
    ball.transform.position = pos or Vector3(0, 10, 0)
    rb = ball.add_component("Rigidbody")
    rb.mass = mass
    col = ball.add_component("SphereCollider")
    col.radius = radius
    return ball, rb


# ═══════════════════════════════════════════════════════════════════════════
# Gravity & free fall
# ═══════════════════════════════════════════════════════════════════════════

class TestGravity:
    def test_player_style_scene_rebuild_preserves_static_collisions(self, scene):
        """A full native graph rebuild must still publish physics bodies on Play."""
        Physics.set_gravity(Vector3(0, -9.81, 0))
        _make_ground(scene)
        _make_ball(scene, pos=Vector3(0, 4, 0))

        snapshot = scene.serialize_document()
        transaction = SceneDocumentTransaction(
            scene,
            document=snapshot,
            after_publish=lambda: scene.set_playing(True),
        )
        assert transaction.run_to_completion() is True

        restored_ball = scene.find("Ball")
        manager = SceneManager.instance()
        manager.play()
        manager.pause()
        _step_frames(180)

        assert restored_ball.transform.position.y > 0.9

    def test_set_gravity_persists(self, scene):
        Physics.set_gravity(Vector3(0, -9.81, 0))
        g = Physics.get_gravity()
        assert g.y == pytest.approx(-9.81, abs=0.01)

    def test_custom_gravity(self, scene):
        Physics.set_gravity(Vector3(0, -20, 0))
        g = Physics.get_gravity()
        assert g.y == pytest.approx(-20, abs=0.01)
        Physics.set_gravity(Vector3(0, -9.81, 0))  # restore

    def test_ball_falls_under_gravity(self, scene):
        """A dynamic sphere above the ground should lose altitude."""
        Physics.set_gravity(Vector3(0, -9.81, 0))
        _make_ground(scene)
        ball, rb = _make_ball(scene, pos=Vector3(0, 10, 0))

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        y0 = ball.transform.position.y
        _step_frames(60)
        y1 = ball.transform.position.y

        assert y1 < y0, f"Ball should fall: {y0} → {y1}"

    def test_no_gravity_ball_stays(self, scene):
        """With gravity off a dynamic body should not fall."""
        _make_ground(scene)
        ball, rb = _make_ball(scene, pos=Vector3(0, 10, 0))
        rb.use_gravity = False

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        y0 = ball.transform.position.y
        _step_frames(30)
        y1 = ball.transform.position.y

        assert y1 == pytest.approx(y0, abs=0.1)


class TestRigidbodyPose:
    def test_velocity_set_before_deferred_body_creation_is_applied(self, scene):
        Physics.set_gravity(Vector3(0, 0, 0))
        _, rb = _make_ball(scene, pos=Vector3(0, 5, 0))
        rb.velocity = Vector3(2, 3, 4)
        rb.angular_velocity = Vector3(0.25, 0.5, 0.75)

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(1)

        assert tuple(rb.velocity) == pytest.approx((2, 3, 4))
        assert tuple(rb.angular_velocity) == pytest.approx((0.25, 0.5, 0.75), abs=0.001)

    def test_pose_assignment_preserves_velocity(self, scene):
        Physics.set_gravity(Vector3(0, 0, 0))
        ball, rb = _make_ball(scene, pos=Vector3(0, 5, 0))
        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(1)

        rb.velocity = Vector3(3, 4, 5)
        rb.angular_velocity = Vector3(0.5, 1.0, 1.5)
        rb.position = Vector3(7, 8, 9)
        rb.rotation = quatf(0.0, 0.0, 0.70710678, 0.70710678)

        assert tuple(rb.velocity) == pytest.approx((3, 4, 5))
        assert tuple(rb.angular_velocity) == pytest.approx((0.5, 1.0, 1.5))
        assert tuple(rb.position) == pytest.approx((7, 8, 9))
        assert tuple(ball.transform.position) == pytest.approx((7, 8, 9))

    def test_pose_assignment_rejects_invalid_values(self, scene):
        _, rb = _make_ball(scene)
        with pytest.raises(ValueError, match="finite"):
            rb.position = Vector3(float("nan"), 0, 0)
        with pytest.raises(ValueError, match="non-zero quaternion"):
            rb.rotation = quatf(0, 0, 0, 0)
        with pytest.raises(ValueError, match="finite"):
            rb.velocity = Vector3(1, 0, 0) / 0.0

    def test_kinematic_move_does_not_teleport_transform_before_step(self, scene):
        Physics.set_gravity(Vector3(0, 0, 0))
        body, rigidbody = _make_ball(scene, pos=Vector3(0, 5, 0))
        rigidbody.is_kinematic = True

        class KinematicMoveProbe(InxComponent):
            def awake(self):
                self.position_during_move = None
                self.did_move = False

            def fixed_update(self, _delta_time):
                if self.did_move:
                    return
                rigidbody.move_position(Vector3(4, 5, 0))
                self.position_during_move = tuple(body.transform.position)
                self.did_move = True

        probe = body.add_component(KinematicMoveProbe)
        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        sm.step()

        assert probe.position_during_move == pytest.approx((0, 5, 0))
        assert tuple(rigidbody.position) == pytest.approx((4, 5, 0), abs=0.001)
        assert tuple(body.transform.position) == pytest.approx((4, 5, 0), abs=0.001)

    def test_kinematic_move_rejects_non_kinematic_body(self, scene):
        _, rigidbody = _make_ball(scene)

        with pytest.raises(RuntimeError, match="requires a kinematic Rigidbody"):
            rigidbody.move_position(Vector3(1, 2, 3))


class TestFixedTiming:
    def test_fixed_time_is_current_inside_fixed_update(self, scene):
        probe_object = scene.create_game_object("FixedTimeProbe")

        class FixedTimeProbe(InxComponent):
            def awake(self):
                self.samples = []

            def fixed_update(self, _delta_time):
                self.samples.append((Time.fixed_time, Time.fixed_unscaled_time))

        probe = probe_object.add_component(FixedTimeProbe)
        manager = SceneManager.instance()
        manager.time_scale = 1.0
        manager.play()
        manager.pause()
        manager.step()
        manager.step()

        fixed_delta = manager.get_fixed_time_step()
        assert probe.samples == pytest.approx(
            [(fixed_delta, fixed_delta), (fixed_delta * 2.0, fixed_delta * 2.0)]
        )


# ═══════════════════════════════════════════════════════════════════════════
# Collision — ball lands on ground
# ═══════════════════════════════════════════════════════════════════════════

class TestCollision:
    def test_ball_lands_on_ground(self, scene):
        """Ball should collide with ground and stop roughly at ground level."""
        Physics.set_gravity(Vector3(0, -9.81, 0))
        _make_ground(scene)
        ball, rb = _make_ball(scene, pos=Vector3(0, 5, 0), radius=0.5)

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(180)  # 3 seconds — plenty of time to settle
        y = ball.transform.position.y
        # Ground top is at Y=0.5 (center 0, scale 1 → half-height 0.5)
        # Ball radius 0.5 → ball center rests at about Y=1.0
        assert y < 5.0, "Ball should have fallen"
        assert y > -1.0, "Ball should not fall through ground"

    def test_bounce_combine_mode_changes_contact_response(self, scene):
        ground = _make_ground(scene)
        ground_collider = ground.get_component("BoxCollider")
        _assign_physic_material(ground_collider, bounciness=0.0, bounce_combine=0)

        maximum_ball, _ = _make_ball(scene, pos=Vector3(-2, 3, 0))
        maximum_collider = maximum_ball.get_component("SphereCollider")
        _assign_physic_material(maximum_collider, bounciness=1.0, bounce_combine=3)

        minimum_ball, _ = _make_ball(scene, pos=Vector3(2, 3, 0))
        minimum_collider = minimum_ball.get_component("SphereCollider")
        _assign_physic_material(minimum_collider, bounciness=1.0, bounce_combine=1)

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(40)

        maximum_peak = maximum_ball.transform.position.y
        minimum_peak = minimum_ball.transform.position.y
        for _ in range(50):
            sm.step(1.0 / 60.0)
            maximum_peak = max(maximum_peak, maximum_ball.transform.position.y)
            minimum_peak = max(minimum_peak, minimum_ball.transform.position.y)

        assert maximum_peak > minimum_peak + 0.75

    def test_compound_material_uses_contacted_subshape(self, scene):
        ground = scene.create_game_object("CompoundMaterialGround")
        primary = ground.add_component("BoxCollider")
        primary.size = Vector3(1, 1, 1)
        primary.center = Vector3(20, 0, 0)
        _assign_physic_material(primary, bounciness=1.0, bounce_combine=3)

        contacted = ground.add_component("BoxCollider")
        contacted.size = Vector3(10, 1, 10)
        _assign_physic_material(contacted, bounciness=0.0, bounce_combine=1)

        ball, _ = _make_ball(scene, pos=Vector3(0, 3, 0))
        ball_collider = ball.get_component("SphereCollider")
        _assign_physic_material(ball_collider, bounciness=1.0, bounce_combine=0)

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(40)

        peak_after_contact = ball.transform.position.y
        for _ in range(50):
            sm.step(1.0 / 60.0)
            peak_after_contact = max(peak_after_contact, ball.transform.position.y)

        assert peak_after_contact < 1.5

    def test_box_stack_settles_without_lateral_drift(self, scene):
        Physics.set_gravity(Vector3(0, -9.81, 0))
        ground = scene.create_game_object("StackGround")
        ground.transform.position = Vector3(0, -0.5, 0)
        ground_collider = ground.add_component("BoxCollider")
        ground_collider.size = Vector3(20, 1, 20)

        boxes = []
        for index in range(8):
            box = scene.create_game_object(f"StackBox{index}")
            box.transform.position = Vector3(0, 0.5 + index * 1.01, 0)
            rigidbody = box.add_component("Rigidbody")
            collider = box.add_component("BoxCollider")
            collider.size = Vector3(1, 1, 1)
            boxes.append((box, rigidbody))

        manager = SceneManager.instance()
        manager.play()
        manager.pause()
        _step_frames(600)

        for index, (box, rigidbody) in enumerate(boxes):
            assert abs(box.transform.position.x) < 0.1
            assert abs(box.transform.position.z) < 0.1
            assert box.transform.position.y == pytest.approx(0.5 + index, abs=0.12)
            assert rigidbody.is_sleeping()

    def test_contact_friction_slows_non_rotating_box(self, scene):
        Physics.set_gravity(Vector3(0, -9.81, 0))

        rigidbodies = []
        for name, z, friction in (("Frictionless", -3.0, 0.0), ("HighFriction", 3.0, 1.0)):
            floor = scene.create_game_object(f"{name}Floor")
            floor.transform.position = Vector3(0, -0.5, z)
            floor_collider = floor.add_component("BoxCollider")
            floor_collider.size = Vector3(30, 1, 4)
            _assign_physic_material(floor_collider, friction=friction, friction_combine=3)

            box = scene.create_game_object(f"{name}Box")
            box.transform.position = Vector3(-8, 0.5, z)
            rigidbody = box.add_component("Rigidbody")
            rigidbody.constraints = int(RigidbodyConstraints.FreezeRotation)
            rigidbody.drag = 0.0
            collider = box.add_component("BoxCollider")
            collider.size = Vector3(1, 1, 1)
            _assign_physic_material(collider, friction=friction, friction_combine=3)
            rigidbody.velocity = Vector3(5, 0, 0)
            rigidbodies.append(rigidbody)

        manager = SceneManager.instance()
        manager.play()
        manager.pause()
        _step_frames(90)

        frictionless, high_friction = rigidbodies
        assert frictionless.velocity.x > 4.5
        assert abs(high_friction.velocity.x) < 0.25


# ═══════════════════════════════════════════════════════════════════════════
# Rigidbody properties in play mode
# ═══════════════════════════════════════════════════════════════════════════

class TestRigidbodyInScene:
    @pytest.mark.parametrize(
        "attribute,value",
        [
            ("mass", 0.0),
            ("drag", float("nan")),
            ("angular_drag", -0.1),
            ("constraints", 1),
            ("interpolation", 7),
            ("max_linear_velocity", -1.0),
        ],
    )
    def test_rigidbody_setters_reject_invalid_values(self, scene, attribute, value):
        _, rigidbody = _make_ball(scene)
        with pytest.raises((ValueError, TypeError)):
            setattr(rigidbody, attribute, value)

    def test_mass_affects_physics(self, scene):
        _make_ground(scene)
        heavy, rb_heavy = _make_ball(scene, pos=Vector3(-3, 10, 0), mass=100.0)
        light, rb_light = _make_ball(scene, pos=Vector3(3, 10, 0), mass=0.1)
        rb_heavy.use_gravity = True
        rb_light.use_gravity = True

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(30)

        # Both should have fallen (gravity is mass-independent in Newtonian physics,
        # but drag or solver differences may cause slight variation)
        assert heavy.transform.position.y < 10.0
        assert light.transform.position.y < 10.0

    def test_kinematic_body_does_not_fall(self, scene):
        _make_ground(scene)
        ball, rb = _make_ball(scene, pos=Vector3(0, 10, 0))
        rb.is_kinematic = True

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        y0 = ball.transform.position.y
        _step_frames(60)
        y1 = ball.transform.position.y
        assert y1 == pytest.approx(y0, abs=0.1)

    def test_velocity_readable_during_fall(self, scene):
        Physics.set_gravity(Vector3(0, -9.81, 0))
        _make_ground(scene)
        ball, rb = _make_ball(scene, pos=Vector3(0, 20, 0))

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(30)

        vel = rb.velocity
        # Ball is falling, so velocity Y should be negative
        assert vel.y < 0, f"Velocity should be downward: {vel.y}"

    def test_add_force_impulse(self, scene):
        """An upward impulse should propel the ball up."""
        Physics.set_gravity(Vector3(0, 0, 0))  # disable gravity for a clean test
        _make_ground(scene)
        ball, rb = _make_ball(scene, pos=Vector3(0, 5, 0))

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(1)
        rb.add_force(Vector3(0, 100, 0), ForceMode.Impulse)
        _step_frames(30)

        assert ball.transform.position.y > 5.0, "Impulse should move ball up"
        Physics.set_gravity(Vector3(0, -9.81, 0))


class TestRigidbodyNumerics:
    def test_start_force_survives_deferred_body_creation(self, scene):
        Physics.set_gravity(Vector3(0, 0, 0))
        body, rigidbody = _make_ball(scene, pos=Vector3(0, 5, 0), mass=4.0)
        rigidbody.use_gravity = False
        rigidbody.drag = 0.0
        rigidbody.angular_drag = 0.0
        rigidbody.max_angular_velocity = 100.0

        class StartForceProbe(InxComponent):
            def start(self):
                rigidbody.add_force(Vector3(3, 0, 0), ForceMode.VelocityChange)
                rigidbody.add_torque(Vector3(0, 0, 1), ForceMode.VelocityChange)

        body.add_component(StartForceProbe)
        manager = SceneManager.instance()
        manager.play()
        manager.pause()
        manager.step()

        assert tuple(rigidbody.velocity) == pytest.approx((3, 0, 0), abs=1e-4)
        assert tuple(rigidbody.angular_velocity) == pytest.approx((0, 0, 1), abs=1e-4)

    @pytest.mark.parametrize(
        "mode,expected_light,expected_heavy",
        [
            (ForceMode.Force, 0.2, 0.02),
            (ForceMode.Acceleration, 0.2, 0.2),
            (ForceMode.Impulse, 10.0, 1.0),
            (ForceMode.VelocityChange, 10.0, 10.0),
        ],
    )
    def test_linear_force_modes_follow_mass_contract(self, scene, mode, expected_light, expected_heavy):
        Physics.set_gravity(Vector3(0, 0, 0))
        _, light = _make_ball(scene, pos=Vector3(-2, 5, 0), mass=1.0)
        _, heavy = _make_ball(scene, pos=Vector3(2, 5, 0), mass=10.0)
        for rigidbody in (light, heavy):
            rigidbody.use_gravity = False
            rigidbody.drag = 0.0
            rigidbody.max_linear_velocity = 100.0

        manager = SceneManager.instance()
        manager.play()
        manager.pause()
        manager.step()

        force = Vector3(10, 0, 0)
        light.add_force(force, mode)
        heavy.add_force(force, mode)
        manager.step()

        assert light.velocity.x == pytest.approx(expected_light, abs=1e-4)
        assert heavy.velocity.x == pytest.approx(expected_heavy, abs=1e-4)

    def test_continuous_force_integrates_over_half_second(self, scene):
        Physics.set_gravity(Vector3(0, 0, 0))
        body = scene.create_game_object("ContinuousForceBox")
        body.transform.position = Vector3(0, 2, 0)
        body.transform.local_scale = Vector3(1.8, 0.7, 3.5)
        body.add_component("BoxCollider")
        rigidbody = body.add_component("Rigidbody")
        rigidbody.mass = 1.0
        rigidbody.use_gravity = False
        rigidbody.drag = 0.0
        rigidbody.max_linear_velocity = 500.0

        manager = SceneManager.instance()
        manager.play()
        manager.pause()
        manager.step()
        start_z = body.transform.position.z
        for _ in range(25):
            rigidbody.add_force(Vector3(0, 0, 28), ForceMode.Force)
            manager.step()

        assert rigidbody.velocity.z == pytest.approx(14.0, abs=0.05)
        assert body.transform.position.z - start_z == pytest.approx(3.64, abs=0.15)

    @pytest.mark.parametrize(
        "mode,input_value,expected_velocity",
        [
            (ForceMode.Acceleration, 2.0, 0.04),
            (ForceMode.VelocityChange, 1.0, 1.0),
        ],
    )
    def test_angular_mass_independent_modes_survive_partial_rotation_constraints(
        self, scene, mode, input_value, expected_velocity
    ):
        Physics.set_gravity(Vector3(0, 0, 0))
        _, light = _make_ball(scene, pos=Vector3(-2, 5, 0), mass=1.0)
        _, heavy = _make_ball(scene, pos=Vector3(2, 5, 0), mass=10.0)
        for rigidbody in (light, heavy):
            rigidbody.use_gravity = False
            rigidbody.angular_drag = 0.0
            rigidbody.max_angular_velocity = 100.0
            rigidbody.constraints = int(RigidbodyConstraints.FreezeRotationX)

        manager = SceneManager.instance()
        manager.play()
        manager.pause()
        manager.step()

        torque = Vector3(0, 0, input_value)
        light.add_torque(torque, mode)
        heavy.add_torque(torque, mode)
        manager.step()

        assert tuple(light.angular_velocity) == pytest.approx((0, 0, expected_velocity), abs=1e-4)
        assert tuple(heavy.angular_velocity) == pytest.approx((0, 0, expected_velocity), abs=1e-4)

    def test_angular_acceleration_inverts_coupled_allowed_inertia_subspace(self, scene):
        Physics.set_gravity(Vector3(0, 0, 0))
        body = scene.create_game_object("CoupledInertiaBox")
        body.transform.position = Vector3(0, 5, 0)
        body.transform.rotation = quatf(0.38268343, 0, 0, 0.92387953)
        rigidbody = body.add_component("Rigidbody")
        rigidbody.mass = 3.0
        rigidbody.use_gravity = False
        rigidbody.angular_drag = 0.0
        rigidbody.max_angular_velocity = 100.0
        rigidbody.constraints = int(RigidbodyConstraints.FreezeRotationX)
        collider = body.add_component("BoxCollider")
        collider.size = Vector3(1, 2, 4)

        manager = SceneManager.instance()
        manager.play()
        manager.pause()
        manager.step()
        rigidbody.add_torque(Vector3(0, 1, 2), ForceMode.Acceleration)
        manager.step()

        fixed_delta = manager.get_fixed_time_step()
        assert tuple(rigidbody.angular_velocity) == pytest.approx(
            (0, fixed_delta, fixed_delta * 2.0), abs=1e-4
        )

    def test_drag_uses_configured_collision_substeps(self, scene):
        Physics.set_gravity(Vector3(0, 0, 0))
        _, rigidbody = _make_ball(scene, pos=Vector3(0, 5, 0))
        rigidbody.use_gravity = False
        rigidbody.drag = 5.0
        rigidbody.angular_drag = 4.0
        rigidbody.max_linear_velocity = 100.0
        rigidbody.max_angular_velocity = 100.0

        manager = SceneManager.instance()
        manager.play()
        manager.pause()
        manager.step()
        rigidbody.velocity = Vector3(10, 0, 0)
        rigidbody.angular_velocity = Vector3(0, 10, 0)
        manager.step()

        fixed_delta = manager.get_fixed_time_step()
        substeps = EngineConfig.get().physics_collision_steps
        expected_linear = 10.0 * (1.0 - 5.0 * fixed_delta / substeps) ** substeps
        expected_angular = 10.0 * (1.0 - 4.0 * fixed_delta / substeps) ** substeps
        assert rigidbody.velocity.x == pytest.approx(expected_linear, abs=1e-4)
        assert rigidbody.angular_velocity.y == pytest.approx(expected_angular, abs=1e-4)

    def test_velocity_limits_apply_to_direct_assignment_and_impulses(self, scene):
        Physics.set_gravity(Vector3(0, 0, 0))
        _, rigidbody = _make_ball(scene, pos=Vector3(0, 5, 0))
        rigidbody.use_gravity = False
        rigidbody.drag = 0.0
        rigidbody.angular_drag = 0.0
        rigidbody.max_linear_velocity = 3.0
        rigidbody.max_angular_velocity = 2.0

        manager = SceneManager.instance()
        manager.play()
        manager.pause()
        manager.step()
        rigidbody.velocity = Vector3(30, 0, 0)
        rigidbody.angular_velocity = Vector3(0, 20, 0)

        assert tuple(rigidbody.velocity) == pytest.approx((3, 0, 0), abs=1e-4)
        assert tuple(rigidbody.angular_velocity) == pytest.approx((0, 2, 0), abs=1e-4)

        rigidbody.add_force(Vector3(30, 0, 0), ForceMode.Impulse)
        rigidbody.add_torque(Vector3(0, 20, 0), ForceMode.Impulse)
        manager.step()
        assert tuple(rigidbody.velocity) == pytest.approx((3, 0, 0), abs=1e-4)
        assert tuple(rigidbody.angular_velocity) == pytest.approx((0, 2, 0), abs=1e-4)


class TestContinuousCollisionDetection:
    @pytest.mark.parametrize(
        "mode,should_cross_wall",
        [
            (CollisionDetectionMode.Discrete, True),
            (CollisionDetectionMode.Continuous, False),
        ],
    )
    def test_fast_sphere_against_thin_static_wall(self, scene, mode, should_cross_wall):
        Physics.set_gravity(Vector3(0, 0, 0))

        wall = scene.create_game_object("ThinWall")
        wall.transform.position = Vector3(0, 0, 0)
        wall.transform.local_scale = Vector3(0.1, 10, 10)
        wall.add_component("BoxCollider")

        projectile, rigidbody = _make_ball(scene, pos=Vector3(-4, 0, 0), radius=0.25)
        rigidbody.use_gravity = False
        rigidbody.drag = 0.0
        rigidbody.max_linear_velocity = 1000.0
        rigidbody.collision_detection_mode = mode
        rigidbody.velocity = Vector3(600, 0, 0)

        manager = SceneManager.instance()
        manager.play()
        manager.pause()
        manager.step()

        if should_cross_wall:
            assert projectile.transform.position.x > 1.0
        else:
            assert projectile.transform.position.x < 0.0
            assert rigidbody.velocity.x < 1.0

    @pytest.mark.parametrize(
        "mode,should_cross",
        [
            (CollisionDetectionMode.Discrete, True),
            (CollisionDetectionMode.Continuous, False),
        ],
    )
    def test_fast_dynamic_spheres_respect_collision_mode(self, scene, mode, should_cross):
        Physics.set_gravity(Vector3(0, 0, 0))

        pair = []
        for side, velocity in ((-1, 600.0), (1, -600.0)):
            body = scene.create_game_object(f"DynamicPair{side}")
            body.transform.position = Vector3(side * 4.0, 0, 0)
            rigidbody = body.add_component("Rigidbody")
            rigidbody.use_gravity = False
            rigidbody.drag = 0.0
            rigidbody.max_linear_velocity = 1000.0
            rigidbody.collision_detection_mode = mode
            collider = body.add_component("SphereCollider")
            collider.radius = 0.25
            rigidbody.velocity = Vector3(velocity, 0, 0)
            pair.append((body, rigidbody))

        manager = SceneManager.instance()
        manager.play()
        manager.pause()
        manager.step()
        profile = manager.get_last_frame_profile()

        if should_cross:
            assert profile["dynamic_ccd_splits"] == 0
            assert pair[0][0].transform.position.x > 1.0
            assert pair[1][0].transform.position.x < -1.0
        else:
            assert profile["dynamic_ccd_splits"] >= 1
            assert pair[0][0].transform.position.x < 0.0
            assert pair[1][0].transform.position.x > 0.0
            assert pair[0][1].velocity.x < 1.0
            assert pair[1][1].velocity.x > -1.0


# ═══════════════════════════════════════════════════════════════════════════
# Raycasting with actual scene objects
# ═══════════════════════════════════════════════════════════════════════════

class TestRaycast:
    def test_raycast_hits_ground(self, scene):
        _make_ground(scene)

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(1)

        hit = Physics.raycast(Vector3(0, 50, 0), Vector3(0, -1, 0), 100.0)
        assert hit is not None
        assert 0 < hit.distance < 50.0
        assert hit.normal.y == pytest.approx(1.0, abs=0.1)

    def test_raycast_miss(self, scene):
        _make_ground(scene)
        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(1)

        # Ray pointing away from all objects
        hit = Physics.raycast(Vector3(0, 50, 0), Vector3(0, 1, 0), 100.0)
        assert hit is None

    def test_raycast_all_returns_multiple(self, scene):
        _make_ground(scene)
        # Create floating box collider above ground
        box = scene.create_game_object("FloatingBox")
        box.transform.position = Vector3(0, 5, 0)
        box.add_component("BoxCollider")

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(1)

        hits = Physics.raycast_all(Vector3(0, 50, 0), Vector3(0, -1, 0), 100.0)
        assert len(hits) >= 2  # at least ground + floating box


# ═══════════════════════════════════════════════════════════════════════════
# Overlap queries
# ═══════════════════════════════════════════════════════════════════════════

class TestOverlapQueries:
    def test_overlap_sphere_finds_colliders(self, scene):
        for i in range(3):
            go = scene.create_game_object(f"Obj{i}")
            go.transform.position = Vector3(float(i), 0, 0)
            go.add_component("SphereCollider")

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(1)

        result = Physics.overlap_sphere(Vector3(0, 0, 0), 50.0)
        assert len(result) >= 3

    def test_overlap_box_finds_colliders(self, scene):
        go = scene.create_game_object("TestBox")
        go.add_component("BoxCollider")

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(1)

        result = Physics.overlap_box(Vector3(0, 0, 0), Vector3(10, 10, 10))
        assert len(result) >= 1

    def test_overlap_box_honors_orientation(self, scene):
        target = scene.create_game_object("OrientedQueryTarget")
        target.transform.position = Vector3(0, 3, 0)
        target.add_component("SphereCollider")
        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(1)

        extents = Vector3(4, 0.25, 0.25)
        collider_id = target.get_component("SphereCollider").component_id
        assert collider_id not in [c.component_id for c in Physics.overlap_box(Vector3(0, 0, 0), extents)]
        quarter_turn_z = quatf(0.0, 0.0, 0.70710678, 0.70710678)
        assert collider_id in [
            c.component_id for c in Physics.overlap_box(Vector3(0, 0, 0), extents, quarter_turn_z)
        ]

    def test_overlap_capsule_finds_colliders(self, scene):
        target = scene.create_game_object("CapsuleQueryTarget")
        target.transform.position = Vector3(0, 2, 0)
        collider = target.add_component("SphereCollider")
        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(1)

        assert collider.component_id in [
            c.component_id
            for c in Physics.overlap_capsule(Vector3(0, -2, 0), Vector3(0, 2, 0), 0.75)
        ]


# ═══════════════════════════════════════════════════════════════════════════
# Shape casts
# ═══════════════════════════════════════════════════════════════════════════

class TestShapeCasts:
    def test_sphere_cast_hits_ground(self, scene):
        _make_ground(scene)
        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(1)

        hit = Physics.sphere_cast(Vector3(0, 50, 0), 1.0, Vector3(0, -1, 0), 100.0)
        assert hit is not None
        assert hit.distance < 60

    def test_box_cast_hits_ground(self, scene):
        _make_ground(scene)
        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(1)

        hit = Physics.box_cast(
            Vector3(0, 50, 0), Vector3(1, 1, 1), Vector3(0, -1, 0), max_distance=100.0
        )
        assert hit is not None

    def test_raycast_flushes_moved_static_collider_without_fixed_step(self, scene):
        target = scene.create_game_object("MovedStaticQueryTarget")
        target.transform.position = Vector3(0, 0, 0)
        target.add_component("BoxCollider")

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        sm.step()

        target.transform.position = Vector3(12, 0, 0)
        hit = Physics.raycast(Vector3(12, 5, 0), Vector3(0, -1, 0), 10.0)

        assert hit is not None
        assert hit.game_object.name == "MovedStaticQueryTarget"

    def test_capsule_cast_hits_ground(self, scene):
        _make_ground(scene)
        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(1)

        hit = Physics.capsule_cast(
            Vector3(0, 49, 0), Vector3(0, 51, 0), 0.5, Vector3(0, -1, 0), 100.0
        )
        assert hit is not None


class TestContactCallbacks:
    def test_trigger_enter_stay_exit_lifecycle(self, scene):
        class TriggerProbe(InxComponent):
            events = None

            def awake(self):
                self.events = []

            def on_trigger_enter(self, other):
                self.events.append("enter")

            def on_trigger_stay(self, other):
                self.events.append("stay")

            def on_trigger_exit(self, other):
                self.events.append("exit")

        trigger_object = scene.create_game_object("LifecycleTrigger")
        trigger = trigger_object.add_component("BoxCollider")
        trigger.size = Vector3(4, 4, 4)
        trigger.is_trigger = True

        mover = scene.create_game_object("LifecycleMover")
        mover.transform.position = Vector3(8, 0, 0)
        mover.add_component("SphereCollider")
        rigidbody = mover.add_component("Rigidbody")
        rigidbody.is_kinematic = True
        probe = mover.add_component(TriggerProbe)

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        sm.step()

        mover.transform.position = Vector3(0, 0, 0)
        Physics.sync_transforms()
        sm.step()
        assert "enter" in probe.events

        sm.step()
        assert "stay" in probe.events

        mover.transform.position = Vector3(8, 0, 0)
        Physics.sync_transforms()
        sm.step()
        assert "exit" in probe.events

# ═══════════════════════════════════════════════════════════════════════════
# Layer collision filtering
# ═══════════════════════════════════════════════════════════════════════════

class TestLayerCollision:
    def test_ignore_layer_collision_round_trip(self, scene):
        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(1)

        Physics.ignore_layer_collision(10, 11, True)
        assert Physics.get_ignore_layer_collision(10, 11) is True
        Physics.ignore_layer_collision(10, 11, False)
        assert Physics.get_ignore_layer_collision(10, 11) is False


class TestIncrementalTransformSync:
    def test_mesh_cooking_cache_reuses_identical_geometry(self, scene):
        NativeMeshCollider.clear_cooking_cache()
        first = scene.create_primitive(PrimitiveType.Cube, "CachedMeshA")
        second = scene.create_primitive(PrimitiveType.Cube, "CachedMeshB")
        first_mesh = first.add_component("MeshCollider")
        second_mesh = second.add_component("MeshCollider")

        Physics.sync_transforms()

        stats = NativeMeshCollider.get_cooking_cache_stats()
        assert stats["misses"] == 1
        assert stats["hits"] >= 1
        assert stats["async_submissions"] == 1
        assert stats["pending"] == 0
        assert first_mesh.is_cooking is False
        assert second_mesh.is_cooking is False
        assert first_mesh.shape_error == ""
        assert second_mesh.shape_error == ""

    def test_mesh_geometry_change_invalidates_pending_cook(self, scene):
        NativeMeshCollider.clear_cooking_cache()
        game_object = scene.create_primitive(PrimitiveType.Cube, "ChangingMesh")
        renderer = game_object.get_component("MeshRenderer")
        mesh = game_object.add_component("MeshCollider")
        Physics.sync_transforms()

        renderer.set_primitive_mesh(PrimitiveType.Sphere)
        assert mesh.is_cooking is True
        assert NativeMeshCollider.get_cooking_cache_stats()["pending"] == 1

        # Returning to the cached cube invalidates this collider's sphere
        # revision without cancelling the shared immutable worker payload.
        renderer.set_primitive_mesh(PrimitiveType.Cube)
        assert mesh.is_cooking is False
        Physics.sync_transforms()

        stats = NativeMeshCollider.get_cooking_cache_stats()
        assert stats["pending"] == 0
        assert stats["async_submissions"] == 2
        assert mesh.is_cooking is False
        assert mesh.shape_error == ""


    def test_pose_readback_only_visits_active_bodies(self, scene):
        Physics.set_gravity(Vector3(0, 0, 0))
        rigidbodies = [
            _make_ball(scene, pos=Vector3(index * 3.0, 5, 0), radius=0.5)[1]
            for index in range(24)
        ]
        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        sm.step()

        for rigidbody in rigidbodies:
            rigidbody.sleep()
        sm.step()
        assert sm.get_last_rigidbody_sync_candidate_count() == 0
        assert sm.get_last_interpolation_candidate_count() == 0

        rigidbodies[7].wake_up()
        sm.step()
        assert sm.get_last_rigidbody_sync_candidate_count() == 1
        assert sm.get_last_interpolation_candidate_count() == 1

    def test_only_changed_collider_is_considered(self, scene):
        objects = []
        for index in range(64):
            game_object = scene.create_game_object(f"StaticCollider{index}")
            game_object.transform.position = Vector3(float(index * 2), 0, 0)
            game_object.add_component("BoxCollider")
            objects.append(game_object)

        sm = SceneManager.instance()
        sm.play()
        sm.pause()

        sm.step()
        assert sm.get_last_collider_sync_candidate_count() == 0

        objects[17].transform.position = Vector3(34, 3, 0)
        sm.step()
        assert sm.get_last_collider_sync_candidate_count() == 1

    def test_dynamic_scale_change_rebuilds_shape_through_dirty_actor(self, scene):
        Physics.set_gravity(Vector3(0, 0, 0))
        game_object, rigidbody = _make_ball(scene, pos=Vector3(0, 5, 0), radius=0.5)
        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        sm.step()

        collider_id = game_object.get_component("SphereCollider").component_id
        assert collider_id in {
            collider.component_id
            for collider in Physics.overlap_sphere(Vector3(0, 5, 0), 0.1)
        }
        initial = {
            collider.component_id
            for collider in Physics.overlap_sphere(Vector3(1.5, 5, 0), 0.1)
        }
        assert collider_id not in initial
        game_object.transform.local_scale = Vector3(4, 4, 4)
        sm.step()
        scaled = {
            collider.component_id
            for collider in Physics.overlap_sphere(Vector3(1.5, 5, 0), 0.1)
        }
        assert collider_id in scaled


class TestPrimitiveColliderAlignment:
    def test_sphere_and_capsule_native_shapes_match_builtin_mesh_dimensions(self, scene):
        sphere = scene.create_primitive(PrimitiveType.Sphere, "AlignedSphere")
        assert sphere.get_component("SphereCollider") is not None

        capsule = scene.create_primitive(PrimitiveType.Capsule, "AlignedCapsule")
        capsule.transform.position = Vector3(3, 0, 0)
        assert capsule.get_component("CapsuleCollider") is not None

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        Physics.sync_transforms()
        sm.step()

        sphere_hit = Physics.raycast(Vector3(-2, 0, 0), Vector3(1, 0, 0), 4.0)
        assert sphere_hit is not None
        assert sphere_hit.game_object.name == "AlignedSphere"
        assert sphere_hit.distance == pytest.approx(1.5, abs=0.02)

        capsule_top_hit = Physics.raycast(Vector3(3, 3, 0), Vector3(0, -1, 0), 4.0)
        assert capsule_top_hit is not None
        assert capsule_top_hit.game_object.name == "AlignedCapsule"
        assert capsule_top_hit.distance == pytest.approx(2.0, abs=0.02)

        capsule_side_hit = Physics.raycast(Vector3(1, 0, 0), Vector3(1, 0, 0), 4.0)
        assert capsule_side_hit is not None
        assert capsule_side_hit.game_object.name == "AlignedCapsule"
        assert capsule_side_hit.distance == pytest.approx(1.5, abs=0.02)


class TestCompoundCollider:
    def test_detached_collider_does_not_allocate_actor(self, scene):
        initial_count = Physics.get_actor_count()
        detached = NativeBoxCollider()
        assert detached.serialize()
        assert Physics.get_actor_count() == initial_count

    def test_compound_uses_one_generational_actor_slot(self, scene):
        initial_count = Physics.get_actor_count()
        compound = scene.create_game_object("ActorSlot")
        primary = compound.add_component("BoxCollider")
        secondary = compound.add_component("SphereCollider")

        assert Physics.get_actor_count() == initial_count + 1
        assert compound.remove_component(secondary) is True
        assert Physics.get_actor_count() == initial_count + 1
        assert compound.remove_component(primary) is True
        assert Physics.get_actor_count() == initial_count

    def test_non_primary_collider_geometry_rebuilds_shared_shape(self, scene):
        game_object = scene.create_game_object("Compound")
        game_object.add_component("BoxCollider")
        sphere = game_object.add_component("SphereCollider")
        sphere.radius = 0.5
        sphere.center = Vector3(5, 0, 0)

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        sm.step()

        initial = Physics.raycast(Vector3(5, 5, 0), Vector3(0, -1, 0), 10.0)
        assert initial is not None
        assert initial.collider.component_id == sphere.component_id

        sphere.center = Vector3(10, 0, 0)
        moved = Physics.raycast(Vector3(10, 5, 0), Vector3(0, -1, 0), 10.0)
        assert moved is not None
        assert moved.collider.component_id == sphere.component_id

    def test_mixed_trigger_query_filters_per_subshape(self, scene):
        game_object = scene.create_game_object("MixedCompound")
        solid = game_object.add_component("BoxCollider")
        trigger = game_object.add_component("SphereCollider")
        trigger.center = Vector3(8, 0, 0)
        trigger.radius = 1.0
        trigger.is_trigger = True

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        sm.step()

        solid_hit = Physics.raycast(
            Vector3(0, 5, 0), Vector3(0, -1, 0), 10.0,
            query_triggers=False,
        )
        assert solid_hit is not None
        assert solid_hit.collider.component_id == solid.component_id

        trigger_hit = Physics.raycast(
            Vector3(8, 5, 0), Vector3(0, -1, 0), 10.0,
            query_triggers=False,
        )
        assert trigger_hit is None

    def test_mixed_trigger_preserves_solid_response(self, scene):
        compound = scene.create_game_object("MixedResponse")
        solid = compound.add_component("BoxCollider")
        solid.size = Vector3(4, 1, 4)
        trigger = compound.add_component("SphereCollider")
        trigger.center = Vector3(8, 2, 0)
        trigger.radius = 2.0
        trigger.is_trigger = True

        landing_ball, _ = _make_ball(scene, pos=Vector3(0, 5, 0))
        passing_ball, _ = _make_ball(scene, pos=Vector3(8, 6, 0))

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        _step_frames(120)

        assert landing_ball.transform.position.y > 0.0
        assert passing_ball.transform.position.y < 0.0

    def test_reenable_non_primary_collider_takes_actor_ownership(self, scene):
        compound = scene.create_game_object("ReenabledCompound")
        primary = compound.add_component("BoxCollider")
        secondary = compound.add_component("SphereCollider")

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        sm.step()

        primary.enabled = False
        secondary.enabled = False
        compound.transform.position = Vector3(12, 0, 0)
        secondary.enabled = True
        sm.step()

        hit = Physics.raycast(Vector3(12, 5, 0), Vector3(0, -1, 0), 10.0)
        assert hit is not None
        assert hit.collider.component_id == secondary.component_id

    def test_removing_secondary_collider_keeps_actor_body_alive(self, scene):
        compound = scene.create_game_object("RemovedCompoundChild")
        primary = compound.add_component("BoxCollider")
        secondary = compound.add_component("SphereCollider")
        secondary.center = Vector3(6, 0, 0)

        sm = SceneManager.instance()
        sm.play()
        sm.pause()
        sm.step()

        assert compound.remove_component(secondary) is True
        sm.step()

        primary_hit = Physics.raycast(Vector3(0, 5, 0), Vector3(0, -1, 0), 10.0)
        removed_hit = Physics.raycast(Vector3(6, 5, 0), Vector3(0, -1, 0), 10.0)
        assert primary_hit is not None
        assert primary_hit.collider.component_id == primary.component_id
        assert removed_hit is None
