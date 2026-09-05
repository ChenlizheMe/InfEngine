"""Compare two structured Infernux headless trajectory results."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import sys
from typing import Any


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("baseline", help="Baseline headless smoke JSON")
    parser.add_argument("candidate", help="Candidate headless smoke JSON")
    parser.add_argument("--tolerance", type=float, default=1.0e-4)
    parser.add_argument("--max-differences", type=int, default=50)
    return parser


def _load(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if (
        not isinstance(value, dict)
        or value.get("schema") != "infernux.headless_project_smoke"
        or value.get("status") != "passed"
        or not value.get("scene")
        or not isinstance(value.get("trajectory"), list)
        or not value["trajectory"]
    ):
        raise ValueError(f"Not a successful recorded headless run: {path}")
    samples = value["trajectory"]
    frames = [sample.get("frame") for sample in samples]
    if (
        not all(isinstance(frame, int) and not isinstance(frame, bool) for frame in frames)
        or frames[0] != 0
        or frames[-1] != value.get("play_frames")
        or any(after <= before for before, after in zip(frames, frames[1:]))
        or len(frames) < 2
    ):
        raise ValueError(f"Headless trajectory does not cover the complete play interval: {path}")
    for sample in samples:
        objects = sample.get("objects", {})
        if not objects or any(
            state.get("status") in {"missing", "ambiguous"}
            or "position" not in state
            for state in objects.values()
        ):
            raise ValueError(f"Headless trajectory has no usable tracked object state: {path}")
    return value


def compare_trajectories(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
    *,
    tolerance: float,
    max_differences: int = 50,
) -> dict[str, Any]:
    if not math.isfinite(tolerance) or tolerance < 0.0:
        raise ValueError("tolerance must be finite and non-negative")
    if max_differences <= 0:
        raise ValueError("max_differences must be positive")

    differences: list[dict[str, Any]] = []
    maximum_error = 0.0
    difference_count = 0

    def report(path: str, expected: Any, actual: Any, error: float | None = None) -> None:
        nonlocal difference_count, maximum_error
        difference_count += 1
        if error is not None:
            maximum_error = max(maximum_error, error)
        if len(differences) < max_differences:
            entry: dict[str, Any] = {
                "path": path,
                "baseline": expected,
                "candidate": actual,
            }
            if error is not None:
                entry["absolute_error"] = error
            differences.append(entry)

    def compare(expected: Any, actual: Any, path: str) -> None:
        if isinstance(expected, bool) or isinstance(actual, bool):
            if expected != actual:
                report(path, expected, actual)
            return
        if isinstance(expected, (int, float)) and isinstance(actual, (int, float)):
            error = abs(float(expected) - float(actual))
            if not math.isfinite(error) or error > tolerance:
                report(path, expected, actual, error)
            else:
                nonlocal_maximum(error)
            return
        if isinstance(expected, dict) and isinstance(actual, dict):
            expected_keys = set(expected)
            actual_keys = set(actual)
            for key in sorted(expected_keys | actual_keys):
                child_path = f"{path}.{key}" if path else str(key)
                if key not in expected:
                    report(child_path, "<missing>", actual[key])
                elif key not in actual:
                    report(child_path, expected[key], "<missing>")
                else:
                    compare(expected[key], actual[key], child_path)
            return
        if isinstance(expected, list) and isinstance(actual, list):
            if len(expected) != len(actual):
                report(f"{path}.length", len(expected), len(actual))
            for index, (expected_item, actual_item) in enumerate(zip(expected, actual)):
                compare(expected_item, actual_item, f"{path}[{index}]")
            return
        if expected != actual:
            report(path, expected, actual)

    def nonlocal_maximum(error: float) -> None:
        nonlocal maximum_error
        maximum_error = max(maximum_error, error)

    compare(baseline.get("scene"), candidate.get("scene"), "scene")
    compare(baseline.get("play_frames"), candidate.get("play_frames"), "play_frames")
    compare(baseline.get("fixed_delta"), candidate.get("fixed_delta"), "fixed_delta")
    compare(baseline["trajectory"], candidate["trajectory"], "trajectory")
    return {
        "schema": "infernux.headless_trajectory_comparison",
        "status": "passed" if not differences else "failed",
        "tolerance": tolerance,
        "maximum_absolute_error": maximum_error,
        "difference_count": difference_count,
        "differences": differences,
    }


def main() -> int:
    args = _parser().parse_args()
    result = compare_trajectories(
        _load(args.baseline),
        _load(args.candidate),
        tolerance=args.tolerance,
        max_differences=args.max_differences,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
