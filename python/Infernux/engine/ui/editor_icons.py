"""Centralized editor icon texture loader.

Uploads PNGs from ``resources/icons/`` as *pinned* ImGui textures so they
are never LRU-evicted by the texture-preview cache.  Binding a cached
descriptor from the unpinned preview path previously caused Vulkan
DEVICE_LOST cascades (fence/semaphore/command-buffer validation spam).

Usage::

    from .editor_icons import EditorIcons
    tid = EditorIcons.get(native_engine, "plus")
    tid = EditorIcons.get_cached("plus")  # resolves native via EditorServices
"""

from __future__ import annotations

import os

import Infernux.resources as _resources

_cache: dict[str, int] = {}
_submitted: set[str] = set()
_native = None

_ICONS = (
    "plus",
    "minus",
    "remove",
    "picker",
    "warning",
    "error",
    "ui_canvas",
    "ui_text",
    "ui_image",
    "ui_button",
    "tool_none",
    "tool_move",
    "tool_rotate",
    "tool_scale",
)


def _tex_name(name: str) -> str:
    return f"__edicon__{name}"


def _resolve_native(native_engine=None):
    global _native
    if native_engine is not None:
        _native = native_engine
        return _native
    if _native is not None:
        return _native
    try:
        from Infernux.engine.ui.editor_services import EditorServices

        svc = EditorServices.instance()
        eng = svc.native_engine if svc is not None else None
        if eng is not None:
            _native = eng
        return _native
    except Exception:
        return None


def _live_id(native, name: str) -> int:
    getter = getattr(native, "get_imgui_texture_id", None)
    if getter is None:
        return 0
    try:
        return int(getter(_tex_name(name)) or 0)
    except Exception:
        return 0


def _submit_icon(native, name: str) -> int:
    """Submit a pinned ImGui texture; return the live id (0 while uploading)."""
    tex_name = _tex_name(name)
    has_tex = getattr(native, "has_imgui_texture", None)
    if callable(has_tex):
        try:
            if has_tex(tex_name):
                tid = _live_id(native, name)
                _cache[name] = tid
                _submitted.add(name)
                return tid
        except Exception:
            pass

    path = os.path.join(_resources.file_type_icons_dir, f"{name}.png")
    if not os.path.isfile(path):
        return 0

    try:
        from Infernux.lib import TextureLoader
    except Exception:
        return 0

    submit = getattr(native, "submit_imgui_texture", None)
    if submit is None:
        return 0

    try:
        tex = TextureLoader.load_from_file(path, name)
        if tex is None or not tex.is_valid():
            return 0
        pixels = tex.get_pixels()
        submit(tex_name, pixels, int(tex.width), int(tex.height), False, True)
        tid = _live_id(native, name)
        _cache[name] = tid
        _submitted.add(name)
        return tid
    except Exception:
        return 0


def _ensure_loaded(native_engine) -> None:
    native = _resolve_native(native_engine)
    if native is None:
        return
    for name in _ICONS:
        if name in _submitted:
            tid = _live_id(native, name)
            if tid != 0:
                _cache[name] = tid
            continue
        _submit_icon(native, name)


class EditorIcons:
    """Thin façade around the module-level pinned icon cache."""

    @staticmethod
    def get(native_engine, name: str) -> int:
        """Return ImGui texture id for *name*, or 0 if unavailable / still uploading."""
        _ensure_loaded(native_engine)
        native = _resolve_native(native_engine)
        if native is None:
            return _cache.get(name, 0)
        tid = _live_id(native, name)
        if tid != 0:
            _cache[name] = tid
            return tid
        if name not in _submitted:
            return _submit_icon(native, name)
        return _cache.get(name, 0)

    @staticmethod
    def get_cached(name: str) -> int:
        """Return live icon id via EditorServices native engine when needed."""
        native = _resolve_native()
        if native is not None:
            return EditorIcons.get(native, name)
        return _cache.get(name, 0)

    @staticmethod
    def preload(native_engine) -> None:
        """Eagerly submit all known editor icons (call once after renderer init)."""
        _ensure_loaded(native_engine)

    @staticmethod
    def reset():
        """Clear the cache (e.g. after engine re-init)."""
        global _native
        _cache.clear()
        _submitted.clear()
        _native = None
