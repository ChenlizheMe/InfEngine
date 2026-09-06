"""Shared projection of phase-local exporter progress onto one global range."""

from __future__ import annotations

from .contracts import BuildProgress


_PHASE_RANGES = {
    "doctor": (0.00, 0.03),
    "plan": (0.03, 0.05),
    "execute": (0.05, 0.06),
    "prepare": (0.06, 0.14),
    "python-runtime": (0.14, 0.24),
    "analyze": (0.24, 0.32),
    "cook": (0.32, 0.55),
    "shaders": (0.55, 0.64),
    "native": (0.64, 0.78),
    "compile": (0.78, 0.92),
    "desktop": (0.06, 0.96),
    "package": (0.92, 0.96),
    "audit": (0.96, 0.99),
    "smoke": (0.99, 0.995),
    "complete": (0.995, 1.00),
}


def build_progress_fraction(progress: BuildProgress) -> float:
    start, end = _PHASE_RANGES.get(str(progress.phase), (0.05, 0.95))
    if progress.total > 0:
        local = max(0.0, min(1.0, progress.completed / progress.total))
    else:
        local = 0.0
    return start + (end - start) * local


__all__ = ["build_progress_fraction"]
