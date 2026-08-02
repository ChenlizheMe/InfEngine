"""Queue-route image ownership policies used by RenderEffect features."""

from __future__ import annotations

from enum import Enum
from typing import Iterable


class RoutePolicy(str, Enum):
    """How a route contributes its geometry and effect result to its parent."""

    INLINE = "inline"
    MASK_AND_MODIFY = "mask_and_modify"
    ISOLATE_AND_COMPOSITE = "isolate_and_composite"
    ADDITIVE_EXTRACT = "additive_extract"
    CUSTOM_FEATURE = "custom_feature"


def merge_route_policies(policies: Iterable[RoutePolicy]) -> RoutePolicy:
    """Return one route strategy or reject a semantically unsafe mixture."""
    normalized = {
        value if isinstance(value, RoutePolicy) else RoutePolicy(value)
        for value in policies
    }
    normalized.discard(RoutePolicy.INLINE)
    if not normalized:
        return RoutePolicy.INLINE
    if RoutePolicy.CUSTOM_FEATURE in normalized:
        if len(normalized) != 1:
            raise ValueError("custom route policy cannot be mixed with built-in policies")
        return RoutePolicy.CUSTOM_FEATURE
    if RoutePolicy.ADDITIVE_EXTRACT in normalized:
        if len(normalized) != 1:
            names = ", ".join(sorted(value.value for value in normalized))
            raise ValueError(
                "additive-extract route effects cannot be mixed with color-replacement "
                f"effects on one route: {names}"
            )
        return RoutePolicy.ADDITIVE_EXTRACT
    if RoutePolicy.ISOLATE_AND_COMPOSITE in normalized:
        return RoutePolicy.ISOLATE_AND_COMPOSITE
    return RoutePolicy.MASK_AND_MODIFY


__all__ = ["RoutePolicy", "merge_route_policies"]
