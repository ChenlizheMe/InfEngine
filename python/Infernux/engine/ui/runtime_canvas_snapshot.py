"""Revision-driven runtime Canvas discovery for editor UI consumers."""

from __future__ import annotations

from Infernux.ui.ui_canvas_utils import (
    canvas_membership_revision,
    collect_runtime_canvases_with_go as _collect_runtime_canvases_with_go,
    collect_sorted_runtime_canvases as _collect_sorted_runtime_canvases,
    scene_canvas_cache_key as _scene_canvas_cache_key,
)


def _runtime_scene_key(active_scene, persistent_scene) -> tuple:
    scenes = []
    for scene in (active_scene, persistent_scene):
        if scene is not None and all(scene is not existing for existing in scenes):
            scenes.append(scene)
    return tuple(_scene_canvas_cache_key(scene) for scene in scenes)


def runtime_canvas_snapshot_token(active_scene, persistent_scene=None) -> tuple:
    """Return the scene epoch plus the dedicated Canvas membership revision."""
    return (
        _runtime_scene_key(active_scene, persistent_scene),
        canvas_membership_revision(),
    )


def collect_sorted_runtime_canvas_snapshot(active_scene, persistent_scene=None):
    """Return the current sorted Canvas snapshot without unrelated scene scans."""
    return _collect_sorted_runtime_canvases(
        active_scene,
        persistent_scene,
        allow_stale_empty=False,
    )


def collect_runtime_canvas_snapshot_with_go(active_scene, persistent_scene=None):
    """Return the shared runtime Canvas snapshot with owning GameObjects."""
    collect_sorted_runtime_canvas_snapshot(active_scene, persistent_scene)
    return _collect_runtime_canvases_with_go(
        active_scene,
        persistent_scene,
        allow_stale_empty=False,
    )
