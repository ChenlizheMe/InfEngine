from __future__ import annotations

import json
import sys
from pathlib import Path


PACKAGING_DIR = Path(__file__).resolve().parents[1]
MODEL_DIR = PACKAGING_DIR / "model"
for directory in (PACKAGING_DIR, MODEL_DIR):
    if str(directory) not in sys.path:
        sys.path.insert(0, str(directory))

from project_model import _create_default_project_content


def _fnv1a64(payload: bytes) -> str:
    value = 14695981039346656037
    for byte in payload:
        value ^= byte
        value = (value * 1099511628211) & 0xFFFFFFFFFFFFFFFF
    return f"{value:016x}"


def test_default_render_assets_seed_current_content_hashes(tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    final = tmp_path / "Project"
    (staging / "ProjectSettings").mkdir(parents=True)

    _create_default_project_content(str(staging), str(final), "Project")

    rendering = staging / "Assets" / "Rendering"
    for asset in (
        rendering / "Bloom.effect",
        rendering / "ACES Tone Mapping.effect",
        rendering / "Default Post Processing.effectgroup",
    ):
        metadata = json.loads(asset.with_name(asset.name + ".meta").read_text(encoding="utf-8"))
        assert metadata["metadata"]["content_hash"] == {
            "type": "string",
            "value": _fnv1a64(asset.read_bytes()),
        }
