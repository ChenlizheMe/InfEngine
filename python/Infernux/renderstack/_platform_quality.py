"""Target-owned quality limits for built-in render pipelines.

Scene assets keep their authored desktop values. Platform hosts may publish a
render profile that bounds expensive built-in resources at runtime without
rewriting the scene or changing third-party pipelines.
"""

from __future__ import annotations

import os


def _bounded_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def mobile_profile_active() -> bool:
    return os.environ.get("INFERNUX_RENDER_PROFILE", "").strip().casefold() == "mobile"


def effective_shadow_resolution(requested: int) -> int:
    requested = max(256, min(8192, int(requested)))
    if not mobile_profile_active():
        return requested
    limit = _bounded_int("INFERNUX_MOBILE_MAX_SHADOW_RESOLUTION", 1024, 256, 8192)
    return min(requested, limit)


def effective_msaa_samples(requested: int) -> int:
    requested = int(requested)
    if requested not in (1, 2, 4, 8):
        raise ValueError(f"Unsupported MSAA sample count: {requested}")
    if not mobile_profile_active():
        return requested
    limit = _bounded_int("INFERNUX_MOBILE_MAX_MSAA_SAMPLES", 1, 1, 8)
    supported_limits = tuple(samples for samples in (1, 2, 4, 8) if samples <= limit)
    return min(requested, supported_limits[-1])
