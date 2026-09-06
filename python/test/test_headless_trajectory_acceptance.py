from types import SimpleNamespace
import json

import pytest

from scripts.acceptance.compare_headless_trajectories import _load, compare_trajectories
from scripts.acceptance.headless_project_smoke import _capture_trajectory_sample


def test_capture_trajectory_records_transform_and_rigidbody_state():
    body = SimpleNamespace(
        type_name="Rigidbody",
        position=SimpleNamespace(x=1.0, y=2.0, z=3.0),
        velocity=SimpleNamespace(x=4.0, y=5.0, z=6.0),
        angular_velocity=SimpleNamespace(x=7.0, y=8.0, z=9.0),
    )
    obj = SimpleNamespace(
        name="PlayerBall",
        transform=SimpleNamespace(position=SimpleNamespace(x=10.0, y=11.0, z=12.0)),
        get_components=lambda: [body],
    )

    result = _capture_trajectory_sample([obj], ["PlayerBall"], 30, 1.0 / 60.0)

    assert result["frame"] == 30
    assert result["time_seconds"] == 0.5
    assert result["objects"]["PlayerBall"] == {
        "position": [1.0, 2.0, 3.0],
        "velocity": [4.0, 5.0, 6.0],
        "angular_velocity": [7.0, 8.0, 9.0],
    }


def test_compare_trajectories_accepts_values_within_tolerance():
    baseline = {
        "fixed_delta": 1.0 / 60.0,
        "trajectory": [{"frame": 1, "objects": {"Ball": {"position": [1.0, 2.0, 3.0]}}}],
    }
    candidate = {
        "fixed_delta": 1.0 / 60.0,
        "trajectory": [{"frame": 1, "objects": {"Ball": {"position": [1.00001, 2.0, 3.0]}}}],
    }

    result = compare_trajectories(baseline, candidate, tolerance=1.0e-4)

    assert result["status"] == "passed"
    assert result["maximum_absolute_error"] > 0.0


def test_compare_trajectories_reports_precise_field_path():
    baseline = {
        "fixed_delta": 0.02,
        "trajectory": [{"frame": 2, "objects": {"Ball": {"velocity": [1.0, 0.0, 0.0]}}}],
    }
    candidate = {
        "fixed_delta": 0.02,
        "trajectory": [{"frame": 2, "objects": {"Ball": {"velocity": [1.2, 0.0, 0.0]}}}],
    }

    result = compare_trajectories(baseline, candidate, tolerance=0.01)

    assert result["status"] == "failed"
    assert result["difference_count"] == 1
    assert result["differences"][0]["path"] == "trajectory[0].objects.Ball.velocity[0]"


@pytest.mark.parametrize("invalid", [None, "failed", "empty", "incomplete", "missing_object"])
def test_cli_does_not_accept_failed_or_unrecorded_headless_runs(tmp_path, invalid):
    report = {
        "schema": "infernux.headless_project_smoke", "status": "passed",
        "scene": "Assets/Main.scene", "play_frames": 30, "fixed_delta": 1 / 60,
        "trajectory": [
            {"frame": frame, "objects": {"Ball": {"position": [0, 1, 0]}}}
            for frame in (0, 30)
        ],
    }
    if invalid == "failed":
        report["status"] = "failed"
    elif invalid == "empty":
        report["trajectory"] = []
    elif invalid == "incomplete":
        report["trajectory"].pop()
    elif invalid == "missing_object":
        report["trajectory"][1]["objects"]["Ball"] = {"status": "missing"}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    if invalid is None:
        assert _load(str(path)) == report
    else:
        with pytest.raises(ValueError):
            _load(str(path))


def test_comparison_rejects_different_scenes_with_matching_motion():
    baseline = {"scene": "Assets/A.scene", "play_frames": 30, "fixed_delta": 1 / 60,
                "trajectory": [{"frame": 30, "objects": {"Ball": {"position": [0, 1, 0]}}}]}
    candidate = {**baseline, "scene": "Assets/B.scene"}
    result = compare_trajectories(baseline, candidate, tolerance=1e-4)
    assert result["status"] == "failed"
    assert result["differences"][0]["path"] == "scene"
