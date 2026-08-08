"""Physics settings — load / save / apply project-level physics configuration.

Previously embedded in ``engine.ui.tag_layer_settings``; extracted here so
the physics subsystem owns its own configuration without depending on the
editor UI layer.
"""

from __future__ import annotations

import json
import math
import os
from typing import Dict

from Infernux.math.coerce import coerce_vec3

_PHYSICS_SETTINGS_FILE = "PhysicsSettings.json"

DEFAULT_PHYSICS_SETTINGS: Dict[str, object] = {
    "gravity": [0.0, -9.81, 0.0],
    "fixed_delta_time": 0.02,
    "max_fixed_delta_time": 0.1,
    "collision_steps": 2,
    "velocity_steps": 10,
    "position_steps": 3,
    "penetration_slop": 0.002,
    "speculative_contact_distance": 0.01,
    "linear_cast_max_penetration": 0.1,
    "baumgarte": 0.15,
    "max_penetration_distance": 0.05,
    "linear_cast_threshold": 0.5,
    "min_velocity_for_restitution": 1.0,
    "time_before_sleep": 0.5,
    "point_velocity_sleep_threshold": 0.03,
    "temp_allocator_mb": 256,
    "max_jobs": 4096,
    "max_barriers": 16,
    "max_bodies": 65536,
    "max_body_pairs": 65536,
    "max_contact_constraints": 65536,
    "max_concurrency": 0,
}

_DOCUMENT_KEYS = set(DEFAULT_PHYSICS_SETTINGS)


class PhysicsSettingsError(ValueError):
    """Raised when an existing physics settings document is invalid."""


def _finite_number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PhysicsSettingsError(f"{field} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise PhysicsSettingsError(f"{field} must be finite")
    return result


def _integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise PhysicsSettingsError(f"{field} must be an integer")
    if value < minimum or value > maximum:
        raise PhysicsSettingsError(f"{field} must be in [{minimum}, {maximum}]")
    return value


def normalize(data: object) -> dict:
    if not isinstance(data, dict):
        raise PhysicsSettingsError("physics settings must be a JSON object")

    unknown = set(data) - _DOCUMENT_KEYS
    if unknown:
        raise PhysicsSettingsError(f"unknown physics settings fields: {', '.join(sorted(unknown))}")

    required = set(DEFAULT_PHYSICS_SETTINGS)
    missing = required - set(data)
    if missing:
        raise PhysicsSettingsError(f"missing physics settings fields: {', '.join(sorted(missing))}")

    gravity = data["gravity"]
    if not isinstance(gravity, list) or len(gravity) != 3:
        raise PhysicsSettingsError("gravity must contain exactly three numbers")
    validated_gravity = [_finite_number(value, f"gravity[{index}]") for index, value in enumerate(gravity)]

    fixed_dt = _finite_number(data["fixed_delta_time"], "fixed_delta_time")
    max_fixed_dt = _finite_number(data["max_fixed_delta_time"], "max_fixed_delta_time")
    if fixed_dt < 0.001:
        raise PhysicsSettingsError("fixed_delta_time must be at least 0.001")
    if max_fixed_dt < fixed_dt:
        raise PhysicsSettingsError("max_fixed_delta_time must be greater than or equal to fixed_delta_time")

    result = {
        "gravity": validated_gravity,
        "fixed_delta_time": fixed_dt,
        "max_fixed_delta_time": max_fixed_dt,
    }
    result.update(
        collision_steps=_integer(data["collision_steps"], "collision_steps", 1, 16),
        velocity_steps=_integer(data["velocity_steps"], "velocity_steps", 2, 64),
        position_steps=_integer(data["position_steps"], "position_steps", 1, 64),
        temp_allocator_mb=_integer(data["temp_allocator_mb"], "temp_allocator_mb", 16, 4096),
        max_jobs=_integer(data["max_jobs"], "max_jobs", 64, 1_000_000),
        max_barriers=_integer(data["max_barriers"], "max_barriers", 1, 4096),
        max_bodies=_integer(data["max_bodies"], "max_bodies", 1024, 10_000_000),
        max_body_pairs=_integer(data["max_body_pairs"], "max_body_pairs", 1024, 10_000_000),
        max_contact_constraints=_integer(
            data["max_contact_constraints"], "max_contact_constraints", 1024, 10_000_000
        ),
        max_concurrency=_integer(data["max_concurrency"], "max_concurrency", 0, 256),
    )
    for field in (
        "penetration_slop",
        "speculative_contact_distance",
        "linear_cast_max_penetration",
        "baumgarte",
        "max_penetration_distance",
        "linear_cast_threshold",
    ):
        value = _finite_number(data[field], field)
        if value < 0.0 or value > 1.0:
            raise PhysicsSettingsError(f"{field} must be in [0, 1]")
        result[field] = value

    restitution_threshold = _finite_number(
        data["min_velocity_for_restitution"], "min_velocity_for_restitution"
    )
    if restitution_threshold <= 0.0 or restitution_threshold > 1000.0:
        raise PhysicsSettingsError("min_velocity_for_restitution must be in (0, 1000]")
    result["min_velocity_for_restitution"] = restitution_threshold

    time_before_sleep = _finite_number(data["time_before_sleep"], "time_before_sleep")
    if time_before_sleep < 0.0 or time_before_sleep > 60.0:
        raise PhysicsSettingsError("time_before_sleep must be in [0, 60]")
    result["time_before_sleep"] = time_before_sleep

    sleep_threshold = _finite_number(
        data["point_velocity_sleep_threshold"], "point_velocity_sleep_threshold"
    )
    if sleep_threshold <= 0.0 or sleep_threshold > 100.0:
        raise PhysicsSettingsError("point_velocity_sleep_threshold must be in (0, 100]")
    result["point_velocity_sleep_threshold"] = sleep_threshold
    return result


def settings_path(project_path: str) -> str:
    """Return the absolute path to the physics settings JSON file."""
    return os.path.join(project_path, "ProjectSettings", _PHYSICS_SETTINGS_FILE)


def load(project_path: str) -> dict:
    """Load and strictly validate the current physics settings schema."""
    if not project_path:
        return dict(DEFAULT_PHYSICS_SETTINGS)

    path = settings_path(project_path)
    if not os.path.isfile(path):
        return dict(DEFAULT_PHYSICS_SETTINGS)

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError) as exc:
        raise PhysicsSettingsError(f"failed to read {path}: {exc}") from exc
    return normalize(data)


def save(project_path: str, settings: dict) -> None:
    """Validate and atomically persist physics settings."""
    if not project_path:
        raise PhysicsSettingsError("project_path is required to save physics settings")
    validated = normalize(settings)
    path = settings_path(project_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    content = json.dumps(validated, indent=2, ensure_ascii=False) + "\n"
    from Infernux.core.document_store import DocumentStore
    DocumentStore.instance().write_and_wait(path, content)


def apply(settings: dict) -> None:
    """Push *settings* into the live engine (gravity, fixed timestep, etc.)."""
    from Infernux.lib import EngineConfig, SceneManager, Vector3
    from Infernux.physics import Physics
    from Infernux.timing import Time

    validated = normalize(settings)
    gravity = validated["gravity"]
    fixed_dt = validated["fixed_delta_time"]
    max_fixed_dt = validated["max_fixed_delta_time"]

    config = EngineConfig.get()
    config.physics_collision_steps = validated["collision_steps"]
    config.physics_velocity_steps = validated["velocity_steps"]
    config.physics_position_steps = validated["position_steps"]
    config.physics_penetration_slop = validated["penetration_slop"]
    config.physics_speculative_contact_distance = validated["speculative_contact_distance"]
    config.physics_linear_cast_max_penetration = validated["linear_cast_max_penetration"]
    config.physics_baumgarte = validated["baumgarte"]
    config.physics_max_penetration_distance = validated["max_penetration_distance"]
    config.physics_linear_cast_threshold = validated["linear_cast_threshold"]
    config.physics_min_velocity_for_restitution = validated["min_velocity_for_restitution"]
    config.physics_time_before_sleep = validated["time_before_sleep"]
    config.physics_point_velocity_sleep_threshold = validated["point_velocity_sleep_threshold"]
    config.physics_temp_allocator_size = validated["temp_allocator_mb"] * 1024 * 1024
    config.physics_max_jobs = validated["max_jobs"]
    config.physics_max_barriers = validated["max_barriers"]
    config.physics_max_bodies = validated["max_bodies"]
    config.physics_max_body_pairs = validated["max_body_pairs"]
    config.physics_max_contact_constraints = validated["max_contact_constraints"]
    config.physics_max_concurrency = validated["max_concurrency"]

    Physics.gravity = coerce_vec3(gravity)
    sm = SceneManager.instance()
    sm.set_fixed_time_step(fixed_dt)
    sm.set_max_fixed_delta_time(max_fixed_dt)
    Time._fixed_delta_time = fixed_dt
    Time._maximum_delta_time = max_fixed_dt
