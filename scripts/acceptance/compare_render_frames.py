"""Compare two backend frame captures using color and luminance statistics."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reference", help="Reference PNG, normally Vulkan")
    parser.add_argument("candidate", help="Candidate PNG, normally WebGPU")
    parser.add_argument("--report", help="Optional JSON report path")
    parser.add_argument("--tiles", type=int, default=4)
    parser.add_argument("--max-mean-luminance-delta", type=float, default=0.02)
    parser.add_argument("--max-channel-mean-delta", type=float, default=0.02)
    parser.add_argument("--max-histogram-distance", type=float, default=0.10)
    parser.add_argument("--max-tile-luminance-delta", type=float, default=0.05)
    parser.add_argument("--max-luminance-percentile-delta", type=float, default=0.05)
    return parser


def _load(path: Path, size: tuple[int, int] | None = None) -> np.ndarray:
    with Image.open(path) as image:
        rgb = image.convert("RGB")
        if size is not None and rgb.size != size:
            rgb = rgb.resize(size, Image.Resampling.BILINEAR)
        return np.asarray(rgb, dtype=np.float32) / 255.0


def _luminance(rgb: np.ndarray) -> np.ndarray:
    return rgb[..., 0] * 0.2126 + rgb[..., 1] * 0.7152 + rgb[..., 2] * 0.0722


def _histogram_distance(reference: np.ndarray, candidate: np.ndarray) -> float:
    reference_hist, _ = np.histogram(reference, bins=64, range=(0.0, 1.0), density=False)
    candidate_hist, _ = np.histogram(candidate, bins=64, range=(0.0, 1.0), density=False)
    reference_probability = reference_hist / max(int(reference_hist.sum()), 1)
    candidate_probability = candidate_hist / max(int(candidate_hist.sum()), 1)
    midpoint = (reference_probability + candidate_probability) * 0.5

    def divergence(probability: np.ndarray) -> float:
        mask = probability > 0.0
        return float(np.sum(probability[mask] * np.log2(probability[mask] / midpoint[mask])))

    return math.sqrt(max(0.0, 0.5 * divergence(reference_probability) + 0.5 * divergence(candidate_probability)))


def _tile_luminance(luminance: np.ndarray, tiles: int) -> list[float]:
    rows = np.array_split(luminance, tiles, axis=0)
    return [float(tile.mean()) for row in rows for tile in np.array_split(row, tiles, axis=1)]


def _metrics(reference: np.ndarray, candidate: np.ndarray, tiles: int) -> dict[str, Any]:
    reference_luminance = _luminance(reference)
    candidate_luminance = _luminance(candidate)
    reference_tiles = _tile_luminance(reference_luminance, tiles)
    candidate_tiles = _tile_luminance(candidate_luminance, tiles)
    reference_percentiles = np.percentile(reference_luminance, [1, 10, 50, 90, 99])
    candidate_percentiles = np.percentile(candidate_luminance, [1, 10, 50, 90, 99])
    return {
        "reference_mean_rgb": [float(value) for value in reference.mean(axis=(0, 1))],
        "candidate_mean_rgb": [float(value) for value in candidate.mean(axis=(0, 1))],
        "channel_mean_delta": float(
            np.max(np.abs(reference.mean(axis=(0, 1)) - candidate.mean(axis=(0, 1))))
        ),
        "reference_mean_luminance": float(reference_luminance.mean()),
        "candidate_mean_luminance": float(candidate_luminance.mean()),
        "mean_luminance_delta": float(abs(reference_luminance.mean() - candidate_luminance.mean())),
        "reference_luminance_percentiles": [float(value) for value in reference_percentiles],
        "candidate_luminance_percentiles": [float(value) for value in candidate_percentiles],
        "max_luminance_percentile_delta": float(
            np.max(np.abs(reference_percentiles - candidate_percentiles))
        ),
        "histogram_distance": _histogram_distance(reference_luminance, candidate_luminance),
        "max_tile_luminance_delta": max(
            abs(reference_value - candidate_value)
            for reference_value, candidate_value in zip(reference_tiles, candidate_tiles, strict=True)
        ),
        "reference_dark_ratio": float(np.mean(reference_luminance < 0.02)),
        "candidate_dark_ratio": float(np.mean(candidate_luminance < 0.02)),
        "reference_highlight_ratio": float(np.mean(reference_luminance > 0.90)),
        "candidate_highlight_ratio": float(np.mean(candidate_luminance > 0.90)),
    }


def main() -> int:
    args = _parser().parse_args()
    if args.tiles <= 0:
        raise ValueError("--tiles must be positive")
    reference_path = Path(args.reference).expanduser().resolve()
    candidate_path = Path(args.candidate).expanduser().resolve()
    if not reference_path.is_file() or not candidate_path.is_file():
        raise FileNotFoundError("both frame captures must exist")
    with Image.open(reference_path) as image:
        reference_size = image.size
    reference = _load(reference_path)
    candidate = _load(candidate_path, reference_size)
    metrics = _metrics(reference, candidate, args.tiles)
    thresholds = {
        "mean_luminance_delta": args.max_mean_luminance_delta,
        "channel_mean_delta": args.max_channel_mean_delta,
        "histogram_distance": args.max_histogram_distance,
        "max_tile_luminance_delta": args.max_tile_luminance_delta,
        "max_luminance_percentile_delta": args.max_luminance_percentile_delta,
    }
    failures = [
        name
        for name, maximum in thresholds.items()
        if float(metrics[name]) > float(maximum)
    ]
    payload = {
        "schema": "infernux.render_comparison",
        "status": "passed" if not failures else "failed",
        "reference": str(reference_path),
        "candidate": str(candidate_path),
        "comparison_size": list(reference_size),
        "metrics": metrics,
        "thresholds": thresholds,
        "failures": failures,
    }
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        report = Path(args.report).expanduser().resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(encoded, encoding="utf-8", newline="\n")
    print(encoded, end="")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
