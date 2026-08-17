"""Shared canvas-discovery utilities for the UI system.

Avoids duplicating the recursive canvas-collection logic across
UIEditorPanel and GameViewPanel.
"""

from __future__ import annotations

from operator import attrgetter
from typing import List, Tuple, TYPE_CHECKING

if TYPE_CHECKING:
    pass  # avoid circular imports at runtime

_sort_key = attrgetter('sort_order')

# ── Cached canvas collection ────────────────────────────────────────
# Avoids a full DFS every frame; rebuilt only when Canvas membership changes.
_canvas_cache: list = []
_canvas_sorted_cache: list = []
_canvas_with_go_cache: list = []
_canvas_cache_scene = None
_canvas_cache_scene_key = None
_canvas_cache_membership_revision: int = -1
_runtime_canvas_cache: list = []
_runtime_canvas_with_go_cache: list = []
_runtime_canvas_cache_key = None
_runtime_canvas_sort_signature: tuple = ()
_canvas_membership_revision: int = 0


def scene_canvas_cache_key(scene) -> tuple[int, int] | None:
    """Return the stable scene epoch used by Canvas discovery.

    General hierarchy revisions deliberately do not participate here. A scene
    containing thousands of unrelated 3D objects can change topology without
    changing its Canvas membership. Canvas attach/detach publishes the
    dedicated revision below, while a temporal discontinuity covers reloads
    that retain the same native scene wrapper.
    """
    if scene is None:
        return None
    world_id = int(getattr(scene, "world_id", 0) or 0)
    return (
        world_id if world_id > 0 else id(scene),
        int(getattr(scene, "temporal_discontinuity_revision", 0)),
    )


def canvas_membership_revision() -> int:
    """Return the monotonic revision for Canvas discovery snapshots."""
    return _canvas_membership_revision


def _rebuild_cache(scene) -> None:
    global _canvas_cache, _canvas_sorted_cache, _canvas_with_go_cache
    global _canvas_cache_scene, _canvas_cache_scene_key
    global _canvas_cache_membership_revision
    from Infernux.ui import UICanvas

    result: list = []

    def _walk(go):
        for comp in go.get_py_components():
            if isinstance(comp, UICanvas):
                result.append((go, comp))
        for child in go.get_children():
            _walk(child)

    if scene is not None:
        for root in scene.get_root_objects():
            _walk(root)

    _canvas_with_go_cache = result
    _canvas_cache = [comp for _, comp in result]
    _canvas_sorted_cache = sorted(_canvas_cache, key=_sort_key)
    _canvas_cache_scene = scene
    _canvas_cache_scene_key = scene_canvas_cache_key(scene)
    _canvas_cache_membership_revision = _canvas_membership_revision


def _ensure_cache(scene, *, allow_stale_empty: bool = False) -> None:
    global _canvas_cache_scene, _canvas_cache_scene_key
    global _canvas_cache_membership_revision
    if scene is None:
        return

    scene_key = scene_canvas_cache_key(scene)
    if (
        scene is _canvas_cache_scene
        and scene_key == _canvas_cache_scene_key
        and _canvas_cache_membership_revision == _canvas_membership_revision
    ):
        return
    _rebuild_cache(scene)


def invalidate_canvas_cache() -> None:
    """Publish a Canvas membership change and invalidate derived snapshots."""
    global _canvas_cache_scene, _canvas_cache_scene_key
    global _canvas_cache_membership_revision, _runtime_canvas_cache
    global _runtime_canvas_with_go_cache
    global _runtime_canvas_cache_key, _runtime_canvas_sort_signature
    global _canvas_membership_revision
    _canvas_membership_revision += 1
    _canvas_cache_scene = None
    _canvas_cache_scene_key = None
    _canvas_cache_membership_revision = -1
    _runtime_canvas_cache = []
    _runtime_canvas_with_go_cache = []
    _runtime_canvas_cache_key = None
    _runtime_canvas_sort_signature = ()


def collect_canvases_with_go(scene) -> List[Tuple]:
    """Return ``[(GameObject, UICanvas), ...]`` for every canvas in *scene*.

    Walks the full scene hierarchy.  Used by UIEditorPanel which needs
    both the owning GameObject and the canvas component.
    """
    if scene is None:
        return []
    _ensure_cache(scene)
    return _canvas_with_go_cache


def collect_canvases(scene, *, allow_stale_empty: bool = False) -> list:
    """Return ``[UICanvas, ...]`` for every canvas in *scene*.

    Lighter variant used by GameViewPanel which only needs the component.
    """
    if scene is None:
        return []
    _ensure_cache(scene, allow_stale_empty=allow_stale_empty)
    return _canvas_cache


def collect_sorted_canvases(scene, *, allow_stale_empty: bool = False) -> list:
    """Return ``[UICanvas, ...]`` sorted by ``sort_order`` (cached)."""
    if scene is None:
        return []
    _ensure_cache(scene, allow_stale_empty=allow_stale_empty)
    return _canvas_sorted_cache


def collect_sorted_runtime_canvases(
    active_scene,
    persistent_scene=None,
    *,
    allow_stale_empty: bool = False,
) -> list:
    """Return sorted canvases from both runtime-owned scenes.

    ``DontDestroyOnLoad`` transfers roots into a separate native Scene. Runtime
    UI remains one visual/input domain, so the active and persistent scenes
    must be collected as one cached snapshot.
    """
    global _runtime_canvas_cache, _runtime_canvas_cache_key
    global _runtime_canvas_with_go_cache
    global _runtime_canvas_sort_signature

    scenes = []
    for scene in (active_scene, persistent_scene):
        if scene is not None and all(scene is not existing for existing in scenes):
            scenes.append(scene)
    scenes = tuple(scenes)
    key = (
        tuple(scene_canvas_cache_key(scene) for scene in scenes),
        _canvas_membership_revision,
    )
    if key == _runtime_canvas_cache_key:
        sort_signature = tuple(_sort_key(canvas) for canvas in _runtime_canvas_cache)
        if sort_signature != _runtime_canvas_sort_signature:
            _runtime_canvas_cache.sort(key=_sort_key)
            _runtime_canvas_sort_signature = tuple(
                _sort_key(canvas) for canvas in _runtime_canvas_cache
            )
        return _runtime_canvas_cache

    from Infernux.ui import UICanvas

    result = []

    def _walk(game_object):
        for component in game_object.get_py_components():
            if isinstance(component, UICanvas):
                result.append((game_object, component))
        for child in game_object.get_children():
            _walk(child)

    for scene in scenes:
        for root in scene.get_root_objects():
            _walk(root)

    _runtime_canvas_with_go_cache = result
    _runtime_canvas_cache = sorted(
        (component for _, component in result),
        key=_sort_key,
    )
    _runtime_canvas_cache_key = key
    _runtime_canvas_sort_signature = tuple(
        _sort_key(canvas) for canvas in _runtime_canvas_cache
    )
    return _runtime_canvas_cache


def collect_runtime_canvases_with_go(
    active_scene,
    persistent_scene=None,
    *,
    allow_stale_empty: bool = False,
) -> List[Tuple]:
    """Return canvases from the complete runtime world with their owners.

    The editor and player share one UI domain even though
    ``DontDestroyOnLoad`` roots live in a separate native Scene.
    """
    collect_sorted_runtime_canvases(
        active_scene,
        persistent_scene,
        allow_stale_empty=allow_stale_empty,
    )
    return _runtime_canvas_with_go_cache
