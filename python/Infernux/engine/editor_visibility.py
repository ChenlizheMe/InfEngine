"""Shared editor visibility gates derived from GameObject hierarchy state."""

from __future__ import annotations

from typing import Any


_MISSING = object()


def game_object_is_active_in_hierarchy(game_object: Any) -> bool:
    """Return the authoritative effective-active state for an editor object.

    Native ``GameObject`` instances expose ``active_in_hierarchy``.  The
    method fallback keeps lightweight test/tool adapters compatible, while a
    missing member is treated as active so non-GameObject doubles retain their
    previous behaviour.  Invalid native handles are never editor-visible.
    """
    if game_object is None:
        return False
    try:
        active = getattr(game_object, "active_in_hierarchy", _MISSING)
        if active is _MISSING:
            active = getattr(game_object, "is_active_in_hierarchy", _MISSING)
        if active is _MISSING:
            return True
        return bool(active() if callable(active) else active)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False


def component_owner_is_active_in_hierarchy(component: Any) -> bool:
    """Return whether a component has a live, effectively active owner."""
    if component is None:
        return False
    try:
        get_owner = getattr(component, "_try_get_game_object", None)
        if callable(get_owner):
            owner = get_owner()
        else:
            owner = getattr(component, "_game_object", _MISSING)
            if owner is _MISSING:
                owner = getattr(component, "game_object", _MISSING)
            if owner is _MISSING:
                return True
        return game_object_is_active_in_hierarchy(owner)
    except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
        return False
