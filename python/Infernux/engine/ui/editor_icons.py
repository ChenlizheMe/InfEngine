"""Centralized editor icon texture loader.

Lazily uploads PNG icons from ``resources/icons/`` to GPU and resolves
their ImGui texture IDs.  All panels share a single cache.

Texture IDs are raw Vulkan descriptor handles owned by the native preview
system, which may replace or evict textures at any time.  IDs are therefore
re-resolved from the native side on every lookup instead of being reused
across frames — binding a cached freed descriptor causes Vulkan validation
errors and intermittent crashes.

Usage::

    from .editor_icons import EditorIcons
    tid = EditorIcons.get(native_engine, "plus")   # -> int texture id
"""

import os
import Infernux.resources as _resources
from Infernux.engine.texture_task_bridge import texture_stamp, query_or_schedule_texture

_cache: dict[str, int] = {}
_native = None


def _live_icon_id(name: str) -> int:
    """Resolve the currently-published descriptor for an icon (0 if absent)."""
    if _native is None:
        return 0
    getter = getattr(_native, "get_texture_preview_texture_id", None)
    if getter is None:
        return 0
    try:
        return int(getter(f"edicon|{name}") or 0)
    except Exception:
        return 0


def _ensure_loaded(native_engine) -> None:
    """Upload all known editor icons (once)."""
    global _native
    if native_engine is None:
        return
    _native = native_engine

    _ICONS = [
        "plus", "minus", "remove", "picker",
        "warning", "error",
        "ui_canvas", "ui_text", "ui_image", "ui_button",
        "tool_none", "tool_move", "tool_rotate", "tool_scale",
    ]
    for name in _ICONS:
        path = os.path.join(_resources.file_type_icons_dir, f"{name}.png")
        if not os.path.isfile(path):
            continue
        stamp = texture_stamp(path, "editor_icon")
        if stamp == 0:
            continue
        tid, _, _ = query_or_schedule_texture(
            native_engine,
            f"edicon|{name}",
            path,
            int(stamp),
            nearest=False,
            srgb=False,
        )
        # Overwrite even with 0 so a stale handle never survives eviction
        # or replacement of the underlying texture.
        _cache[name] = tid


class EditorIcons:
    """Thin façade around the module-level icon cache."""

    @staticmethod
    def get(native_engine, name: str) -> int:
        """Return ImGui texture id for *name*, or 0 if unavailable."""
        _ensure_loaded(native_engine)
        return _cache.get(name, 0)

    @staticmethod
    def get_cached(name: str) -> int:
        """Return the live icon id, or 0.  No engine required.

        Re-resolves the descriptor from the native side each call; falls back
        to the last queried value only when the native getter is unavailable
        (older builds).
        """
        if _native is not None and hasattr(_native, "get_texture_preview_texture_id"):
            return _live_icon_id(name)
        return _cache.get(name, 0)

    @staticmethod
    def reset():
        """Clear the cache (e.g. after engine re-init)."""
        global _native
        _cache.clear()
        _native = None
