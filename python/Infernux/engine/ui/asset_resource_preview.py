"""Resource preview bridge for editor UI.

This module intentionally keeps only a thin Python wrapper and delegates
preview generation/caching to the native C++ preview task system.
C++ manages all caching and change-detection internally via generation
counters.  Python just passes content hints and gets back texture IDs.
"""

from __future__ import annotations

import os
import zlib
from typing import Any, Optional

from Infernux.debug import Debug
from Infernux.engine.texture_task_bridge import safe_mtime_ns


_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tga", ".gif", ".hdr", ".pic", ".psd", ".pnm", ".pgm", ".ppm"}
_MATERIAL_EXTS = {".mat"}
_MODEL_EXTS = {".fbx", ".obj", ".gltf", ".glb", ".dae", ".blend"}
_PREFAB_EXTS = {".prefab"}


def _resolve_native_engine(panel: Any) -> Any:
    try:
        from Infernux.engine.ui.editor_services import EditorServices

        svc = EditorServices.instance()
        native = svc.native_engine if svc else None
        if native:
            return native
    except Exception as exc:
        Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")

    if panel is not None and hasattr(panel, "get_native_engine"):
        try:
            return panel.get_native_engine()
        except Exception as exc:
            Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")
    return None


def _try_get_cpp_mesh_preview(native: Any, norm_path: str) -> int:
    """Query or schedule a mesh/model/prefab preview via the unified C++ API."""
    if native is None:
        return 0
    cache_key = f"mesh|{norm_path}"
    mtime_hint = safe_mtime_ns(norm_path)
    try:
        if hasattr(native, "pump_preview_tasks"):
            native.pump_preview_tasks()
        return int(native.query_or_schedule_mesh_preview(cache_key, norm_path, int(mtime_hint)))
    except Exception as exc:
        Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")
    return 0


def _try_get_cpp_texture_preview(native: Any, norm_path: str,
                                 texture_settings: Optional[Any]) -> tuple[int, int, int]:
    """Return (tex_id, width, height) via the unified C++ query.

    C++ manages caching / generation counter internally.
    Python just passes a content-stamp hint (mtime combo) for change detection.
    """
    if native is None:
        return (0, 0, 0)

    cache_key = f"texedit|{norm_path}" if texture_settings is not None else f"tex|{norm_path}"
    nearest = False
    srgb = False
    max_size = 2048
    texture_format = "auto"
    settings_stamp = 0
    if texture_settings is not None:
        filter_mode = getattr(texture_settings, "filter_mode", None)
        mode_name = getattr(filter_mode, "name", "")
        nearest = str(mode_name).upper() == "POINT"
        srgb = bool(getattr(texture_settings, "srgb", False))
        max_size = max(1, int(getattr(texture_settings, "max_size", 2048)))
        format_value = getattr(texture_settings, "format", None)
        to_string = getattr(format_value, "to_string", None)
        texture_format = str(to_string() if callable(to_string) else "auto")
        settings_values = (
            getattr(getattr(texture_settings, "texture_type", None), "value", 0),
            getattr(filter_mode, "value", 0),
            getattr(getattr(texture_settings, "wrap_mode", None), "value", 0),
            int(bool(getattr(texture_settings, "generate_mipmaps", True))),
            int(srgb),
            max_size,
            int(getattr(texture_settings, "aniso_level", 1)),
            getattr(format_value, "value", 0),
            getattr(getattr(texture_settings, "compression", None), "value", 0),
            getattr(getattr(texture_settings, "compression_quality", None), "value", 0),
        )
        settings_stamp = zlib.crc32("|".join(str(value) for value in settings_values).encode("ascii"))

    # Content stamp: image mtime XOR meta mtime.
    # C++ uses this to detect changes and bump its generation counter.
    image_mtime = safe_mtime_ns(norm_path)
    meta_mtime = safe_mtime_ns(f"{norm_path}.meta")
    content_stamp = (image_mtime ^ ((meta_mtime * 2654435761) & 0xFFFFFFFFFFFFFFFF)) & 0xFFFFFFFFFFFFFFFF
    content_stamp ^= (int(settings_stamp) << 32) | int(settings_stamp)

    try:
        tex_id, w, h = native.query_or_schedule_texture_preview(
            cache_key, norm_path, int(content_stamp), nearest=bool(nearest), srgb=bool(srgb),
            max_size=max_size, texture_format=texture_format, pump=True)
        return (int(tex_id), int(w), int(h))
    except Exception as exc:
        Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")
    return (0, 0, 0)


def _try_get_cpp_material_preview_texture(native: Any, norm_path: str,
                                          material_json: str = "",
                                          file_mtime_hint: int = 0) -> int:
    """Query or schedule a material preview via the unified C++ API.

    C++ manages all caching / change-detection internally.
    Python just passes the content (JSON or mtime hint) and gets back a texture id.
    """
    if native is None:
        return 0

    # Single shared "mat|" key for the Project-panel thumbnail (mtime-driven).
    # Inspector live edits use a separate "matedit|" key so JSON and disk saves
    # do not fight over the same generation counter.
    cache_key = f"matedit|{norm_path}" if material_json else f"mat|{norm_path}"
    try:
        if hasattr(native, "pump_preview_tasks"):
            native.pump_preview_tasks()
        if hasattr(native, 'query_or_schedule_material_preview'):
            tex_id = int(native.query_or_schedule_material_preview(
                cache_key, norm_path, material_json, int(file_mtime_hint)))
            if tex_id == 0 and hasattr(native, "pump_preview_tasks") and hasattr(native, "get_material_preview_texture_id"):
                native.pump_preview_tasks()
                tex_id = int(native.get_material_preview_texture_id(cache_key) or 0)
            return tex_id
        # Fallback for older native builds without unified API
        return int(native.get_material_preview_texture_id(cache_key) or 0)
    except Exception as exc:
        Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")
    return 0


def get_resource_preview_texture_id(panel: Any, file_path: str, preview_size: int = 128,
                                    texture_settings: Optional[Any] = None,
                                    cache_tag: str = "",
                                    material_async: bool = True,
                                    material_json: str = "") -> int:
    """Return ImGui texture ID for a resource path.

    This function only delegates to native C++ preview tasks.
    """
    _ = preview_size
    _ = material_async

    if not file_path:
        return 0

    native = _resolve_native_engine(panel)
    if not native:
        return 0

    norm_path = os.path.normpath(file_path)
    ext = os.path.splitext(norm_path)[1].lower()

    if "::submat:" in norm_path:
        # Passive read of the shared "mat|" key (mtime=0): the C++ Project panel
        # owns mtime-based change detection. Python and C++ compute mtimes on
        # different epochs, so if both passed a hint the generation would ping-pong
        # and re-render every frame. Live edits still use the JSON ("matedit|") key.
        return _try_get_cpp_material_preview_texture(
            native, norm_path, material_json=material_json, file_mtime_hint=0)

    if ext in _IMAGE_EXTS:
        tex_id, _, _ = _try_get_cpp_texture_preview(native, norm_path, texture_settings)
        return tex_id

    if ext in _MATERIAL_EXTS:
        # Passive read (mtime=0) of the shared "mat|" key — see note above.
        return _try_get_cpp_material_preview_texture(
            native, norm_path, material_json=material_json, file_mtime_hint=0)

    if ext in _MODEL_EXTS or ext in _PREFAB_EXTS:
        return _try_get_cpp_mesh_preview(native, norm_path)

    return 0


def render_resource_preview_rect(ctx: Any, panel: Any, file_path: str, width: float, height: float,
                                 preview_size: int = 128, texture_settings: Optional[Any] = None,
                                 preserve_aspect: bool = False, center: bool = False,
                                 cache_tag: str = "") -> bool:
    """Render a resource preview image directly to an ImGui rect."""
    if not file_path:
        return False

    native = _resolve_native_engine(panel)
    if not native:
        return False

    norm_path = os.path.normpath(file_path)
    ext = os.path.splitext(norm_path)[1].lower()
    tex_id = 0
    src_w = 0
    src_h = 0

    if "::submat:" in norm_path:
        # Passive read of the shared "mat|" key (mtime=0); C++ Project panel owns
        # mtime change detection. Live edits use the JSON ("matedit|") key.
        tex_id = _try_get_cpp_material_preview_texture(
            native, norm_path, material_json=cache_tag, file_mtime_hint=0)
        if tex_id != 0:
            src_w = 256
            src_h = 256
    elif ext in _MODEL_EXTS or ext in _PREFAB_EXTS:
        tex_id = _try_get_cpp_mesh_preview(native, norm_path)
        if tex_id == 0:
            return False
        src_w = 256
        src_h = 256
    elif ext in _IMAGE_EXTS:
        tex_id, src_w, src_h = _try_get_cpp_texture_preview(native, norm_path, texture_settings)
    elif ext in _MATERIAL_EXTS:
        # Passive read (mtime=0) of the shared "mat|" key; C++ Project panel owns
        # mtime change detection. Live edits use the JSON ("matedit|") key.
        tex_id = _try_get_cpp_material_preview_texture(
            native, norm_path, material_json=cache_tag, file_mtime_hint=0)
        if preserve_aspect:
            src_w = 256
            src_h = 256

    if tex_id == 0:
        return False

    draw_w = float(width)
    draw_h = float(height)

    if preserve_aspect and src_w > 0 and src_h > 0:
        scale = min(float(width) / float(src_w), float(height) / float(src_h))
        draw_w = max(1.0, float(src_w) * scale)
        draw_h = max(1.0, float(src_h) * scale)

    if center:
        offset_y = max((float(height) - draw_h) * 0.5, 0.0)
        if offset_y > 0.0:
            ctx.dummy(1.0, offset_y)

        offset_x = max((float(width) - draw_w) * 0.5, 0.0)
        if offset_x > 0.0:
            ctx.set_cursor_pos_x(ctx.get_cursor_pos_x() + offset_x)

    ctx.image(tex_id, draw_w, draw_h)

    if center:
        remaining_y = max(float(height) - draw_h - max((float(height) - draw_h) * 0.5, 0.0), 0.0)
        if remaining_y > 0.0:
            ctx.dummy(1.0, remaining_y)

    return True


def invalidate_resource_preview(file_path: str) -> None:
    """Invalidate native preview task/cache entries for one file path."""
    if not file_path:
        return

    norm = os.path.normpath(file_path)
    native = _resolve_native_engine(None)
    if native is None:
        return

    ext = os.path.splitext(norm)[1].lower()
    try:
        if ext in _IMAGE_EXTS:
            native.invalidate_texture_preview_task(f"tex|{norm}")
            native.invalidate_texture_preview_task(f"texedit|{norm}")
        if ext in _MATERIAL_EXTS:
            native.invalidate_material_preview_task(f"mat|{norm}")
    except Exception as exc:
        Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")


def invalidate_live_texture_preview(file_path: str) -> None:
    """Release the transient Inspector generation for one texture."""
    native = _resolve_native_engine(None)
    if native is None or not file_path:
        return
    try:
        native.invalidate_texture_preview_task(f"texedit|{os.path.normpath(file_path)}")
    except Exception as exc:
        Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")


def invalidate_all_resource_previews() -> None:
    """Best-effort flush point for native previews.

    Native layer currently exposes per-resource invalidation only, so we just
    pump pending tasks to converge queued work.
    """
    native = _resolve_native_engine(None)
    if native is not None:
        try:
            native.pump_preview_tasks()
        except Exception as exc:
            Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")
