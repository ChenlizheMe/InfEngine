from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "acceptance" / "compare_render_frames.py"


def _write_frame(path: Path, color: tuple[int, int, int]) -> None:
    Image.new("RGB", (32, 18), color).save(path)


def test_render_frame_comparison_accepts_matching_backend_output(tmp_path: Path):
    reference = tmp_path / "vulkan.png"
    candidate = tmp_path / "webgpu.png"
    report = tmp_path / "report.json"
    _write_frame(reference, (48, 96, 144))
    _write_frame(candidate, (49, 95, 145))

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(reference),
            str(candidate),
            "--report",
            str(report),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "passed"
    assert payload["failures"] == []


def test_render_frame_comparison_rejects_materially_different_output(tmp_path: Path):
    reference = tmp_path / "vulkan.png"
    candidate = tmp_path / "webgpu.png"
    report = tmp_path / "report.json"
    _write_frame(reference, (16, 16, 16))
    _write_frame(candidate, (240, 80, 32))

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            str(reference),
            str(candidate),
            "--report",
            str(report),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 1
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["status"] == "failed"
    assert payload["failures"]
