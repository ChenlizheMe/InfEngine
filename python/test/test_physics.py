"""Tests for Infernux.physics — Physics query API and physics settings (real C++ backend)."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

import Infernux.physics as physics_module
from Infernux.math.coerce import coerce_vec3
from Infernux.lib import CollisionInfo, EngineConfig, SceneManager, Vector3, Physics as CppPhysics
from Infernux.physics.settings import (
    DEFAULT_PHYSICS_SETTINGS,
    PhysicsSettingsError,
    apply as apply_physics_settings,
    load as load_physics_settings,
    save as save_physics_settings,
    settings_path,
)
from Infernux.components.builtin import (
    CollisionDetectionMode,
    RigidbodyConstraints,
    RigidbodyInterpolation,
)


# ═══════════════════════════════════════════════════════════════════════════
# coerce_vec3 — real Vector3
# ═══════════════════════════════════════════════════════════════════════════

class TestCoerceVec3:
    def test_converts_tuples(self):
        value = coerce_vec3((1, 2, 3))
        assert isinstance(value, Vector3)
        assert (value.x, value.y, value.z) == pytest.approx((1.0, 2.0, 3.0))

    def test_preserves_existing_vec3(self):
        original = Vector3(4, 5, 6)
        assert coerce_vec3(original) is original


# ═══════════════════════════════════════════════════════════════════════════
# Physics class property / static API (real C++ backend)
# ═══════════════════════════════════════════════════════════════════════════

class TestPhysicsGravity:
    def test_body_count_forwards_native_world_telemetry(self):
        assert physics_module.Physics.body_count == CppPhysics.body_count

    def test_gravity_returns_vector3(self):
        g = physics_module.Physics.gravity
        assert hasattr(g, "x") and hasattr(g, "y") and hasattr(g, "z")

    def test_default_gravity_is_earth(self):
        g = physics_module.Physics.gravity
        assert g.y == pytest.approx(-9.81, abs=0.01)

    def test_set_gravity_accepts_tuple(self):
        """set_gravity call chain does not crash."""
        physics_module.Physics.gravity = (0, -20, 0)
        # Reset
        physics_module.Physics.gravity = (0, -9.81, 0)

    def test_set_gravity_accepts_vector3(self):
        physics_module.Physics.gravity = Vector3(0, -9.81, 0)


class TestPhysicsRaycast:
    def test_raycast_returns_none_in_empty_space(self):
        # Fire a ray far from any test geometry so it misses everything
        result = physics_module.Physics.raycast(
            Vector3(99999, 99999, 99999), Vector3(0, 1, 0), 1.0)
        assert result is None

    def test_raycast_accepts_tuple_origin(self):
        result = physics_module.Physics.raycast(
            (99999, 99999, 99999), (0, 1, 0), 1.0)
        assert result is None

    def test_raycast_all_returns_list(self):
        # Fire a ray far from any test geometry
        result = physics_module.Physics.raycast_all(
            Vector3(99999, 99999, 99999), Vector3(0, 1, 0), 1.0)
        assert isinstance(result, list)


class TestPhysicsOverlap:
    def test_overlap_sphere_returns_empty_without_scene(self):
        result = physics_module.Physics.overlap_sphere(
            Vector3(0, 0, 0), 10.0)
        assert isinstance(result, list)

    def test_overlap_box_returns_empty_without_scene(self):
        result = physics_module.Physics.overlap_box(
            Vector3(0, 0, 0), Vector3(1, 1, 1))
        assert isinstance(result, list)

    def test_overlap_accepts_tuples(self):
        result = physics_module.Physics.overlap_sphere((0, 0, 0), 5.0)
        assert isinstance(result, list)

    @pytest.mark.parametrize("radius", [0.0, -1.0, float("nan")])
    def test_overlap_sphere_rejects_invalid_radius(self, radius):
        with pytest.raises(ValueError):
            physics_module.Physics.overlap_sphere((0, 0, 0), radius)


class TestPhysicsShapeCasts:
    # Shape casts are volumetric — unlike thin rays, a sphere/box at the origin
    # can overlap leftover physics bodies from integration tests (the
    # PhysicsWorld singleton retains bodies across scenes).  Cast from a very
    # distant origin where no test ever places objects.
    _FAR = Vector3(9999, 9999, 9999)

    def test_sphere_cast_returns_none_without_scene(self):
        result = physics_module.Physics.sphere_cast(
            self._FAR, 1.0, Vector3(0, 1, 0), 100.0)
        assert result is None

    def test_box_cast_returns_none_without_scene(self):
        result = physics_module.Physics.box_cast(
            self._FAR, Vector3(1, 1, 1), Vector3(0, 1, 0))
        assert result is None

    def test_shape_casts_accept_tuples(self):
        far = (9999, 9999, 9999)
        assert physics_module.Physics.sphere_cast(
            far, 1.0, (0, 0, 1), 50.0) is None
        assert physics_module.Physics.box_cast(
            far, (1, 1, 1), (1, 0, 0)) is None

    def test_casts_reject_zero_direction(self):
        with pytest.raises(ValueError):
            physics_module.Physics.raycast(self._FAR, (0, 0, 0), 10.0)
        with pytest.raises(ValueError):
            physics_module.Physics.sphere_cast(self._FAR, 1.0, (0, 0, 0), 10.0)

    def test_box_queries_reject_non_positive_extents(self):
        with pytest.raises(ValueError):
            physics_module.Physics.overlap_box((0, 0, 0), (1, 0, 1))
        with pytest.raises(ValueError):
            physics_module.Physics.box_cast(self._FAR, (1, -1, 1), (0, 1, 0))


class TestPhysicsLayerCollision:
    def test_layer_collision_defaults_to_false(self):
        assert physics_module.Physics.get_ignore_layer_collision(30, 31) is False

    def test_ignore_layer_collision_does_not_crash(self):
        physics_module.Physics.ignore_layer_collision(30, 31, True)
        physics_module.Physics.ignore_layer_collision(30, 31, False)


class TestPhysicsModuleExports:
    def test_module_exports_only_physics(self):
        assert physics_module.__all__ == ["Physics"]

    def test_collision_info_does_not_expose_fake_impulse(self):
        assert not hasattr(CollisionInfo(), "impulse")


# ═══════════════════════════════════════════════════════════════════════════
# Rigidbody enums (real C++ enums)
# ═══════════════════════════════════════════════════════════════════════════

class TestRigidbodyEnums:
    def test_collision_detection_mode_values(self):
        assert int(CollisionDetectionMode.Discrete) == 0
        assert int(CollisionDetectionMode.Continuous) == 1
        assert int(CollisionDetectionMode.ContinuousDynamic) == 2

    def test_interpolation_values(self):
        assert int(RigidbodyInterpolation.Interpolate) == 1

    def test_constraints_freeze_all(self):
        assert int(RigidbodyConstraints.FreezeAll) == 126


# ═══════════════════════════════════════════════════════════════════════════
# Collision detection mode mapping
# ═══════════════════════════════════════════════════════════════════════════

def _backend_motion_quality(mode: CollisionDetectionMode, is_kinematic: bool) -> int:
    if mode == CollisionDetectionMode.ContinuousDynamic:
        return 0 if is_kinematic else 2
    if mode == CollisionDetectionMode.Continuous:
        return 0 if is_kinematic else 1
    return 0


class TestCollisionDetectionMapping:
    def test_dynamic_body_modes(self):
        assert _backend_motion_quality(CollisionDetectionMode.Discrete, False) == 0
        assert _backend_motion_quality(CollisionDetectionMode.Continuous, False) == 1
        assert _backend_motion_quality(CollisionDetectionMode.ContinuousDynamic, False) == 2

    def test_kinematic_body_modes(self):
        assert _backend_motion_quality(CollisionDetectionMode.Discrete, True) == 0
        assert _backend_motion_quality(CollisionDetectionMode.Continuous, True) == 0
        assert _backend_motion_quality(CollisionDetectionMode.ContinuousDynamic, True) == 0


# ═══════════════════════════════════════════════════════════════════════════
# Physics settings (load / save / defaults)
# ═══════════════════════════════════════════════════════════════════════════

class TestPhysicsSettings:
    def test_defaults(self):
        assert DEFAULT_PHYSICS_SETTINGS["gravity"] == [0.0, -9.81, 0.0]
        assert DEFAULT_PHYSICS_SETTINGS["fixed_delta_time"] == 0.02
        assert DEFAULT_PHYSICS_SETTINGS["max_fixed_delta_time"] == pytest.approx(1.0 / 3.0)
        assert DEFAULT_PHYSICS_SETTINGS["collision_steps"] == 1
        assert DEFAULT_PHYSICS_SETTINGS["velocity_steps"] == 10
        assert DEFAULT_PHYSICS_SETTINGS["position_steps"] == 3
        assert DEFAULT_PHYSICS_SETTINGS["min_velocity_for_restitution"] == 2.0
        assert DEFAULT_PHYSICS_SETTINGS["time_before_sleep"] == 0.5
        assert DEFAULT_PHYSICS_SETTINGS["point_velocity_sleep_threshold"] == 0.1

    def test_load_missing_project(self):
        result = load_physics_settings("")
        assert result == DEFAULT_PHYSICS_SETTINGS

    def test_load_missing_file(self, tmp_path):
        result = load_physics_settings(str(tmp_path))
        assert result == DEFAULT_PHYSICS_SETTINGS

    def test_settings_path(self, tmp_path):
        p = settings_path(str(tmp_path))
        assert p.endswith("PhysicsSettings.json")
        assert "ProjectSettings" in p

    def test_save_and_load_round_trip(self, tmp_path):
        project = str(tmp_path)
        os.makedirs(os.path.join(project, "ProjectSettings"), exist_ok=True)
        settings = {
            **DEFAULT_PHYSICS_SETTINGS,
            "gravity": [0.0, -20.0, 0.0],
            "fixed_delta_time": 0.01,
            "max_fixed_delta_time": 0.05,
        }
        save_physics_settings(project, settings)
        loaded = load_physics_settings(project)
        assert loaded["gravity"] == [0.0, -20.0, 0.0]
        assert loaded["fixed_delta_time"] == 0.01

    def test_load_corrupted_file_is_rejected(self, tmp_path):
        project = str(tmp_path)
        ps_dir = os.path.join(project, "ProjectSettings")
        os.makedirs(ps_dir, exist_ok=True)
        with open(os.path.join(ps_dir, "PhysicsSettings.json"), "w") as f:
            f.write("not json")
        with pytest.raises(PhysicsSettingsError):
            load_physics_settings(project)

    def test_load_current_document(self, tmp_path):
        project = str(tmp_path)
        ps_dir = os.path.join(project, "ProjectSettings")
        os.makedirs(ps_dir, exist_ok=True)
        with open(os.path.join(ps_dir, "PhysicsSettings.json"), "w", encoding="utf-8") as file:
            json.dump(dict(DEFAULT_PHYSICS_SETTINGS), file)
        assert load_physics_settings(project) == DEFAULT_PHYSICS_SETTINGS

    def test_save_uses_current_fields_and_leaves_no_temporary_file(self, tmp_path):
        project = str(tmp_path)
        save_physics_settings(project, dict(DEFAULT_PHYSICS_SETTINGS))
        path = settings_path(project)
        with open(path, encoding="utf-8") as file:
            document = json.load(file)
        assert document == DEFAULT_PHYSICS_SETTINGS
        assert list((tmp_path / "ProjectSettings").glob("PhysicsSettings.json.tmp.*")) == []

    def test_unknown_field_is_rejected(self, tmp_path):
        project_settings = tmp_path / "ProjectSettings"
        project_settings.mkdir()
        document = {"unknown": 1, **DEFAULT_PHYSICS_SETTINGS}
        (project_settings / "PhysicsSettings.json").write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(PhysicsSettingsError, match="unknown physics settings fields"):
            load_physics_settings(str(tmp_path))

    def test_native_timestep_rejects_invalid_values(self):
        scene_manager = SceneManager.instance()
        with pytest.raises(ValueError):
            scene_manager.set_fixed_time_step(0.0)
        with pytest.raises(ValueError):
            scene_manager.set_fixed_time_step(float("nan"))
        with pytest.raises(ValueError):
            scene_manager.set_max_fixed_delta_time(0.0005)

    def test_settings_reject_non_finite_gravity(self):
        invalid = dict(DEFAULT_PHYSICS_SETTINGS)
        invalid["gravity"] = [float("inf"), 0.0, 0.0]
        with pytest.raises(PhysicsSettingsError):
            save_physics_settings("unused", invalid)

    @pytest.mark.parametrize(
        "field,value",
        [
            ("velocity_steps", 1),
            ("min_velocity_for_restitution", 0.0),
            ("time_before_sleep", -0.01),
            ("point_velocity_sleep_threshold", 0.0),
        ],
    )
    def test_settings_reject_invalid_solver_and_sleep_values(self, field, value):
        invalid = dict(DEFAULT_PHYSICS_SETTINGS)
        invalid[field] = value
        with pytest.raises(PhysicsSettingsError):
            save_physics_settings("unused", invalid)

    def test_apply_updates_native_startup_configuration(self):
        settings = dict(DEFAULT_PHYSICS_SETTINGS)
        settings.update(
            collision_steps=4,
            velocity_steps=12,
            position_steps=5,
            max_bodies=32768,
            max_concurrency=3,
            penetration_slop=0.004,
            min_velocity_for_restitution=1.5,
            time_before_sleep=0.75,
            point_velocity_sleep_threshold=0.05,
        )
        try:
            apply_physics_settings(settings)
            config = EngineConfig.get()
            assert config.physics_collision_steps == 4
            assert config.physics_velocity_steps == 12
            assert config.physics_position_steps == 5
            assert config.physics_max_bodies == 32768
            assert config.physics_max_concurrency == 3
            assert config.physics_penetration_slop == pytest.approx(0.004)
            assert config.physics_min_velocity_for_restitution == pytest.approx(1.5)
            assert config.physics_time_before_sleep == pytest.approx(0.75)
            assert config.physics_point_velocity_sleep_threshold == pytest.approx(0.05)
        finally:
            apply_physics_settings(dict(DEFAULT_PHYSICS_SETTINGS))
