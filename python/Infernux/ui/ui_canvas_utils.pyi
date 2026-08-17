"""Type stubs for Infernux.ui.ui_canvas_utils — shared canvas-discovery utilities."""

from __future__ import annotations

from typing import Any, List, Tuple

from Infernux.ui.ui_canvas import UICanvas


def scene_canvas_cache_key(scene: Any) -> tuple[int, int, int] | None:
    """Return the cache identity for one native Scene lifetime."""
    ...


def invalidate_canvas_cache() -> None:
    """Force cache invalidation (e.g. on scene load)."""
    ...


def collect_canvases_with_go(scene: Any) -> List[Tuple[Any, UICanvas]]:
    """Return ``[(GameObject, UICanvas), ...]`` for every canvas in *scene*.

    Walks the full scene hierarchy.

    Args:
        scene: The active Scene object.
    """
    ...


def collect_canvases(scene: Any, *, allow_stale_empty: bool = False) -> List[UICanvas]:
    """Return ``[UICanvas, ...]`` for every canvas in *scene*."""
    ...


def collect_sorted_canvases(scene: Any, *, allow_stale_empty: bool = False) -> List[UICanvas]:
    """Return ``[UICanvas, ...]`` sorted by ``sort_order`` (cached)."""
    ...


def collect_sorted_runtime_canvases(active_scene: Any, persistent_scene: Any = None, *, allow_stale_empty: bool = False) -> List[UICanvas]:
    """Return sorted canvases from active and DontDestroyOnLoad scenes."""
    ...
