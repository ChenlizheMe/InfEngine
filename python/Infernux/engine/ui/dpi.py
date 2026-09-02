"""Strict DPI helpers shared by Editor presentation code."""

from __future__ import annotations

from math import isfinite


def editor_dpi_scale(ctx) -> float:
    """Return the native per-monitor content scale or fail immediately."""
    scale = float(ctx.get_dpi_scale())
    if not isfinite(scale) or scale <= 0.0:
        raise RuntimeError(f"Editor reported an invalid display scale: {scale!r}")
    return scale


def scaled_editor_metric(ctx, value: float) -> float:
    """Convert an authored 100%-DPI Editor metric to the active monitor."""
    return float(value) * editor_dpi_scale(ctx)
