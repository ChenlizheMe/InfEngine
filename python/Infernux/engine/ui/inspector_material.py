"""
Material Asset Inspector — body renderer for the unified asset inspector.

This module provides ``render_material_body(ctx, panel, state)`` which renders
the material-specific UI sections: shader selection, render settings,
dynamic properties, and auto-save scheduling.

State is managed by the unified ``asset_details_renderer`` module.
"""

from __future__ import annotations

import copy
import json
import os
import time as _time
from types import SimpleNamespace
from typing import Optional

from Infernux.lib import InxGUIContext
from Infernux.engine.i18n import get_locale, t
from . import inspector_support as _inspector_support
from .asset_execution_layer import AssetAccessMode, get_asset_execution_layer
from .asset_resource_preview import get_resource_preview_texture_id
from .inspector_utils import (
    max_label_w,
    field_label,
    render_compact_section_header,
    _render_color_bar,
    LABEL_PAD,
)
from .theme import Theme, ImGuiCol, ImGuiStyleVar
from . import inspector_shader_utils as shader_utils
from Infernux.debug import Debug
from Infernux.engine.path_utils import resolved_path
import logging
from ._inspector_references import (
    _project_texture_guid_and_path,
)


_PROFILE_ENABLED = _inspector_support.is_inspector_profile_enabled()


def _profile_start() -> float:
    return _time.perf_counter() if _PROFILE_ENABLED else 0.0


def _record_profile_timing(bucket: str, start_time: float) -> None:
    if _PROFILE_ENABLED:
        _inspector_support.record_inspector_profile_timing(
            bucket, (_time.perf_counter() - start_time) * 1000.0,
        )


def _render_virtualized_material_block(ctx, state, block_key, renderer, empty_result):
    heights = state.extra.setdefault("_material_virtual_block_heights", {})
    cached_height = heights.get(block_key, 0.0)
    visibility_query = getattr(ctx, "is_virtualized_region_visible", None)
    if cached_height > 0.0 and callable(visibility_query) and not visibility_query(cached_height):
        ctx.dummy(0.0, cached_height)
        return empty_result

    start_y = ctx.get_cursor_pos_y()
    try:
        return renderer()
    finally:
        measured_height = max(0.0, ctx.get_cursor_pos_y() - start_y)
        if measured_height > 0.0:
            heights[block_key] = measured_height
        else:
            heights.pop(block_key, None)


def _draw_centered_texture(ctx: InxGUIContext, tex_id: int, width: float, height: float,
                           src_w: int = 256, src_h: int = 256) -> bool:
    """Draw a cached preview texture without touching the preview query path."""
    if tex_id == 0 or width <= 0.0 or height <= 0.0 or src_w <= 0 or src_h <= 0:
        return False

    scale = min(float(width) / float(src_w), float(height) / float(src_h))
    draw_w = max(1.0, float(src_w) * scale)
    draw_h = max(1.0, float(src_h) * scale)

    offset_y = max((float(height) - draw_h) * 0.5, 0.0)
    if offset_y > 0.0:
        ctx.dummy(1.0, offset_y)

    offset_x = max((float(width) - draw_w) * 0.5, 0.0)
    if offset_x > 0.0:
        ctx.set_cursor_pos_x(ctx.get_cursor_pos_x() + offset_x)

    ctx.image(tex_id, draw_w, draw_h)

    remaining_y = max(float(height) - draw_h - offset_y, 0.0)
    if remaining_y > 0.0:
        ctx.dummy(1.0, remaining_y)

    return True


def _query_material_preview_tex(panel, native_mat, mat_data, state, cache_tag, preview_path) -> tuple[int, str]:
    """Resolve a material preview texture for asset and inline (prefab/scene) editors.

    Returns (texture_id, native_cache_key). The key is what the C++ preview
    task system indexes this preview under; callers use it to cheaply
    re-resolve the currently-published descriptor on later frames.
    """
    from .asset_resource_preview import (
        _resolve_native_engine,
        _try_get_cpp_material_preview_texture,
        get_resource_preview_texture_id,
    )

    embedded = isinstance(preview_path, str) and "::submat:" in preview_path
    json_blob = (cache_tag or "").strip()
    if not json_blob and not embedded:
        try:
            json_blob = state.extra.get("cached_json") or json.dumps(
                mat_data, sort_keys=True, ensure_ascii=False)
        except Exception:
            json_blob = ""

    if embedded and preview_path:
        tex = int(get_resource_preview_texture_id(
            panel, preview_path, material_json="") or 0)
        return tex, f"mat|{resolved_path(preview_path)}"

    native = _resolve_native_engine(panel)
    if native is None:
        return 0, ""

    # The Inspector document is authoritative while it is open. This path also
    # covers brand-new resources before their first asynchronous disk save.
    if json_blob:
        guid = (getattr(native_mat, "guid", "") or "").strip()
        path_hint = preview_path or f"inline:{guid or id(native_mat)}"
        tex = int(_try_get_cpp_material_preview_texture(
            native, path_hint, material_json=json_blob, file_mtime_hint=0) or 0)
        return tex, f"mat|{path_hint}"

    if not preview_path:
        return 0, ""

    tex = int(get_resource_preview_texture_id(
        panel, preview_path, material_json="") or 0)
    return tex, f"mat|{resolved_path(preview_path)}"


def _is_material_preview_ready(panel, preview_path, cache_tag) -> bool:
    """Return true only when the native texture is for the requested content."""
    from .asset_resource_preview import _resolve_native_engine

    native = _resolve_native_engine(panel)
    if native is None or not hasattr(native, "is_material_preview_ready"):
        # Older native builds have no generation status; preserve their
        # previous non-zero-texture cache behavior.
        return True

    embedded = isinstance(preview_path, str) and "::submat:" in preview_path
    norm_path = resolved_path(preview_path or "")
    cache_key = f"mat|{norm_path}"
    try:
        return bool(native.is_material_preview_ready(cache_key))
    except Exception:
        return False


def _get_cached_material_preview_tex(panel, native_mat, mat_data, state, cache_tag, preview_path) -> int:
    """Avoid repeating filesystem and native preview lookups for a stable material.

    Only the *readiness* decision is cached. The descriptor handle itself is
    re-resolved by native cache key on every frame: the C++ side replaces the
    underlying ImGui texture (new VkDescriptorSet) whenever the preview
    re-renders, and may evict textures under memory pressure. Reusing a raw
    handle across frames binds a freed descriptor — Vulkan validation errors,
    previews rendering as the font atlas, and intermittent crashes.
    """
    embedded = isinstance(preview_path, str) and "::submat:" in preview_path
    # Asset refreshes publish a new in-memory JSON document before the next
    # inspector frame.  Do not let the old preview token survive that refresh;
    # this keeps external/native version changes on the same generation path
    # as an inline edit without reading the file from disk.
    live_json = state.extra.get("cached_json", "")
    if not embedded and live_json and live_json != (cache_tag or ""):
        cache_tag = live_json
        state.extra["_material_cache_tag"] = cache_tag

    preview_key = (preview_path or "", cache_tag or "")
    cached_key = state.extra.get("_material_preview_query_key")
    cached_ready = bool(state.extra.get("_material_preview_tex_ready", False))
    native_key = state.extra.get("_material_preview_native_key") or ""
    if cached_key == preview_key and cached_ready and native_key:
        try:
            from .asset_resource_preview import _resolve_native_engine
            native = _resolve_native_engine(panel)
            if native is not None and hasattr(native, "get_material_preview_texture_id"):
                live_tex = int(native.get_material_preview_texture_id(native_key) or 0)
                if live_tex:
                    return live_tex
                # Preview texture was replaced or evicted; fall through and
                # re-query so a fresh render gets scheduled.
        except Exception as exc:
            Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")

    tex, native_key = _query_material_preview_tex(
        panel, native_mat, mat_data, state, cache_tag, preview_path,
    )
    ready = bool(tex) and _is_material_preview_ready(panel, preview_path, cache_tag)
    state.extra["_material_preview_query_key"] = preview_key
    state.extra["_material_preview_native_key"] = native_key
    state.extra["_material_preview_tex_ready"] = ready
    if not ready:
        try:
            from .asset_resource_preview import _resolve_native_engine
            native = _resolve_native_engine(panel)
            if native is not None and hasattr(native, "request_full_speed_frame"):
                native.request_full_speed_frame()
        except Exception:
            pass
    return int(tex or 0)


# ═══════════════════════════════════════════════════════════════════════════
#  Material property renderer (JSON type system: ptype 0-7)
# ═══════════════════════════════════════════════════════════════════════════

def _get_asset_database():
    """Get the asset database from EditorServices or AssetRegistry (fallback)."""
    try:
        from .editor_services import EditorServices
        adb = EditorServices.instance()._asset_database
        if adb:
            return adb
    except Exception as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        pass
    try:
        from Infernux.lib import AssetRegistry
        return AssetRegistry.instance().get_asset_database()
    except Exception as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return None


def _resolve_path_to_guid(path_str):
    """Resolve a filesystem path to an asset GUID."""
    adb = _get_asset_database()
    return adb.get_guid_from_path(path_str) or "" if adb else ""


def _resolve_texture_display(prop):
    """Return display text for a texture property's GUID."""
    import os
    tex_guid = prop.get("guid", "")
    if not isinstance(tex_guid, str) or not tex_guid:
        return t("igui.none")
    adb = _get_asset_database()
    if adb:
        tex_path = adb.get_path_from_guid(tex_guid)
        if tex_path:
            return os.path.basename(tex_path)
    return f"{t('material.missing_texture')} ({tex_guid[:8]}...)"


def _render_texture2d_property(ctx, prop, prop_name, wid_prefix, plw,
                              reference_cache=None):
    """Render a canonical GUID-backed Texture2D property. Returns True if changed."""
    changed = False
    tex_guid = str(prop.get("guid", "") or "")
    if isinstance(reference_cache, dict):
        if "database" not in reference_cache:
            reference_cache["database"] = _get_asset_database()
        adb = reference_cache["database"]
        database_generation = getattr(adb, "query_generation", -1) if adb else -1
        cache_key = (id(adb), int(database_generation or 0), tex_guid)
        if reference_cache.get("key") != cache_key:
            tex_path = adb.get_path_from_guid(tex_guid) if adb and tex_guid else ""
            reference_cache["key"] = cache_key
            reference_cache["path"] = tex_path or ""
            reference_cache["display"] = (
                os.path.basename(tex_path) if tex_path else (
                    f"{t('material.missing_texture')} ({tex_guid[:8]}...)"
                    if tex_guid else t("igui.none")
                )
            )
        display = reference_cache["display"]
        texture_path = reference_cache["path"]
    else:
        display = _resolve_texture_display(prop)
        texture_path = ""
    field_label(ctx, prop_name, plw)

    from .igui import IGUI

    def _assign_texture(payload):
        nonlocal changed
        guid, path = _project_texture_guid_and_path(payload)
        if not guid:
            logging.getLogger(__name__).warning(
                "Texture must belong to the current project's Assets folder: %s", payload)
            return
        prop["guid"] = guid
        changed = True

    def _on_tex_clear():
        nonlocal changed
        prop["guid"] = ""
        changed = True

    def _texture_path():
        if texture_path:
            return texture_path
        adb = _get_asset_database()
        return adb.get_path_from_guid(tex_guid) if adb and tex_guid else ""

    IGUI.asset_reference_field(
        ctx,
        f"{wid_prefix}_{prop_name}_tex",
        display, "Texture",
        asset_type="Texture",
        clickable=True,
        accept="TEXTURE_FILE",
        on_assign=_assign_texture,
        on_clear=_on_tex_clear,
        ping_path=_texture_path() or None,
        has_value=bool(prop.get("guid")),
        reference_value={
            "asset_type": "Texture",
            "guid": str(prop.get("guid", "") or ""),
            "path_hint": texture_path or _texture_path(),
        },
    )
    return changed


def render_material_property(
    ctx: InxGUIContext,
    prop_name: str,
    prop: dict,
    ptype: int,
    value,
    plw: float,
    wid_prefix: str = "mp",
    reference_cache=None,
) -> bool:
    """Render one material property row.  Returns ``True`` if changed."""
    changed = False
    wid = f"##{wid_prefix}_{prop_name}"

    if ptype == 0:  # Float
        field_label(ctx, prop_name, plw)
        authored_range = prop.get("range")
        if isinstance(authored_range, list) and len(authored_range) == 2:
            nv = float(ctx.float_slider(
                wid, float(value), float(authored_range[0]), float(authored_range[1])))
        else:
            nv = float(ctx.drag_float(wid, float(value), 0.1, 0.0, 100.0))
        if nv != float(value):
            prop["value"] = nv
            changed = True

    elif ptype == 1:  # Float2
        x, y = value[0], value[1]
        nx, ny = ctx.vector2(prop_name, float(x), float(y), 0.1, plw)
        if [nx, ny] != [x, y]:
            prop["value"] = [nx, ny]
            changed = True

    elif ptype == 2:  # Float3
        x, y, z = value[0], value[1], value[2]
        nx, ny, nz = ctx.vector3(
            prop_name, float(x), float(y), float(z), 0.1, plw,
        )
        if [nx, ny, nz] != [x, y, z]:
            prop["value"] = [nx, ny, nz]
            changed = True

    elif ptype == 3:  # Float4
        x, y, z, w = value[0], value[1], value[2], value[3]
        nx, ny, nz, nw = ctx.vector4(
            prop_name, float(x), float(y), float(z), float(w), 0.1, plw,
        )
        if [nx, ny, nz, nw] != [x, y, z, w]:
            prop["value"] = [nx, ny, nz, nw]
            changed = True

    elif ptype == 4:  # Int
        field_label(ctx, prop_name, plw)
        authored_range = prop.get("range")
        if isinstance(authored_range, list) and len(authored_range) == 2:
            nv = int(ctx.int_slider(
                wid, int(value), int(authored_range[0]), int(authored_range[1])))
        else:
            nv = int(ctx.drag_int(wid, int(value), 1.0, 0, 0))
        if nv != int(value):
            prop["value"] = nv
            changed = True

    elif ptype == 5:  # Mat4
        arr = list(value)
        mat_changed = False
        for row in range(4):
            base = row * 4
            nx, ny, nz, nw = ctx.vector4(
                f"{prop_name}[{row}]",
                float(arr[base]), float(arr[base + 1]),
                float(arr[base + 2]), float(arr[base + 3]),
            )
            nr = [nx, ny, nz, nw]
            if nr != arr[base:base + 4]:
                arr[base:base + 4] = nr
                mat_changed = True
        if mat_changed:
            prop["value"] = arr
            changed = True

    elif ptype == 6:  # Texture2D
        changed = _render_texture2d_property(
            ctx, prop, prop_name, wid_prefix, plw,
            reference_cache=reference_cache,
        )

    elif ptype == 7:  # Color
        if value is not None and len(value) >= 4:
            x, y, z, w = value[0], value[1], value[2], value[3]
        else:
            x, y, z, w = 1.0, 1.0, 1.0, 1.0
        field_label(ctx, prop_name, plw)
        allow_hdr = bool(prop.get("hdr", False))
        nr, ng, nb, na = _render_color_bar(
            ctx, wid, float(x), float(y), float(z), float(w),
            allow_hdr=allow_hdr, default_hdr_enabled=allow_hdr)
        if (nr, ng, nb, na) != (x, y, z, w):
            prop["value"] = [nr, ng, nb, na]
            changed = True

    else:
        ctx.label(f"{prop_name}: (type {ptype})")

    return changed


# ═══════════════════════════════════════════════════════════════════════════
# Module-level shortcuts (set per render call from unified state)
# ═══════════════════════════════════════════════════════════════════════════

_native_mat = None
_cached_data: Optional[dict] = None
_shader_cache: dict = {".vert": None, ".frag": None}
_SURFACE_BATCH_SCHEMA = 2


def _shader_value_token(value):
    """Return a stable, value-only key for a serialized shader reference."""
    if isinstance(value, dict):
        return (
            str(value.get("guid", "") or ""),
            str(value.get("shader_id", "") or ""),
            str(value.get("path_hint", "") or ""),
            str(value.get("builtin", "") or ""),
        )
    return (type(value).__name__, str(value or ""))


def _bump_material_schema_revision(state) -> int:
    revision = int(state.extra.get("_material_schema_revision", 0)) + 1
    state.extra["_material_schema_revision"] = revision
    state.extra.pop("_material_property_layout_cache", None)
    state.extra.pop("_material_shader_ui_cache", None)
    return revision


def _get_material_shader_ui_cache(state, vert_ref, frag_ref):
    """Cache dropdown catalogs and stable display/path data per shader revision."""
    generation = int(state.extra.get("_shader_catalog_generation", -1))
    key = (
        generation,
        _shader_value_token(vert_ref),
        _shader_value_token(frag_ref),
    )
    cache = state.extra.get("_material_shader_ui_cache")
    if isinstance(cache, dict) and cache.get("key") == key:
        return cache

    vert_items = shader_utils.get_shader_candidates(".vert", _shader_cache)
    frag_items = shader_utils.get_shader_candidates(".frag", _shader_cache)
    cache = {
        "key": key,
        "vertex_items": vert_items,
        "fragment_items": frag_items,
        "vertex_id": shader_utils.shader_ref_id(vert_ref),
        "fragment_id": shader_utils.shader_ref_id(frag_ref),
        "vertex_display": shader_utils.shader_display_from_value(vert_ref, vert_items),
        "fragment_display": shader_utils.shader_display_from_value(frag_ref, frag_items),
        "vertex_path": _shader_reference_path(vert_ref, ".vert"),
        "fragment_path": _shader_reference_path(frag_ref, ".frag"),
    }
    state.extra["_material_shader_ui_cache"] = cache
    return cache


def _get_material_property_layout_cache(ctx, state, mat_data):
    """Cache property order and label width; values never invalidate this cache."""
    locale = get_locale()
    key = (
        id(ctx),
        locale,
        int(state.extra.get("_material_schema_revision", 0)),
    )
    cache = state.extra.get("_material_property_layout_cache")
    if isinstance(cache, dict) and cache.get("key") == key:
        return cache

    prop_names = tuple(shader_utils.get_material_property_display_order(mat_data))
    cache = {
        "key": key,
        "property_names": prop_names,
        "label_width": max_label_w(ctx, prop_names),
    }
    state.extra["_material_property_layout_cache"] = cache
    return cache


def _shader_reference_path(value, ext: str) -> str:
    reference = shader_utils.make_shader_reference(value, ext)
    path = str(reference.get("path_hint", "") or "")
    if not path:
        path = str(shader_utils.get_shader_file_path(
            shader_utils.shader_ref_id(reference), ext,
        ) or "")
    return path


def _surface_batch_cache_is_current(entries) -> bool:
    """Check the Python/native descriptor contract before reusing a plan."""
    if not isinstance(entries, list):
        return False
    for entry in entries:
        if not isinstance(entry, (tuple, list)) or len(entry) != 2:
            return False
        descriptor = entry[1]
        if not isinstance(descriptor, dict):
            return False
        try:
            property_type = int(descriptor.get("t", -1))
        except (TypeError, ValueError):
            return False
        required_keys = {
            0: ("f",),
            1: ("i",),
            2: ("b",),
            3: ("s",),
            4: ("f", "f2"),
            5: ("f", "f2", "f3"),
            6: ("f", "f2", "f3", "f4"),
            7: ("ei", "en"),
            8: ("f", "f2", "f3", "f4"),
        }.get(property_type)
        if required_keys is None or any(key not in descriptor for key in required_keys):
            return False
    return True


def _ping_shader_reference(value, ext: str) -> None:
    path = _shader_reference_path(value, ext)
    if not path:
        return
    from ._inspector_references import ping_asset_in_project

    ping_asset_in_project(path)


# ═══════════════════════════════════════════════════════════════════════════
# Extracted section renderers (called from render_material_body)
# ═══════════════════════════════════════════════════════════════════════════

def _render_shader_section(ctx, mat_data, state, is_builtin, default_open,
                           layout_cache=None):
    """Render the vertex/fragment shader selection section.

    Returns ``(changed, requires_deserialize, requires_pipeline_refresh, change_key)``.
    """
    changed = False
    requires_deserialize = False
    requires_pipeline_refresh = False
    change_key = ""

    if is_builtin:
        ctx.begin_disabled(True)
    section_t0 = _profile_start()
    section_label = (
        layout_cache["shader_section_label"]
        if isinstance(layout_cache, dict)
        else t("material.shader_section")
    )
    if render_compact_section_header(ctx, section_label, level="secondary",
                                     default_open=default_open):
        shaders = mat_data.setdefault("shaders", {})
        vert_ref = shaders.get("vertex", "")
        frag_ref = shaders.get("fragment", "")
        if isinstance(layout_cache, dict):
            s_lw = layout_cache["shader_label_width"]
        else:
            s_lw = max_label_w(ctx, [t("material.vertex"), t("material.fragment")])
        def _apply_shader(shader_key, new_value, other_key):
            nonlocal changed, requires_deserialize, requires_pipeline_refresh, change_key
            old_val = shaders.get(shader_key, "")
            ext = ".vert" if shader_key == "vertex" else ".frag"
            new_ref = shader_utils.make_shader_reference(new_value, ext)
            if not new_ref["guid"] and not new_ref["shader_id"]:
                return
            shaders[shader_key] = new_ref
            if new_ref != old_val:
                _bump_material_schema_revision(state)
            changed = True
            change_key = f"shader.{shader_key}"
            requires_deserialize = True
            requires_pipeline_refresh = True
            if new_ref != old_val:
                other_id = shader_utils.shader_ref_id(shaders.get(other_key, ""))
                new_id = shader_utils.shader_ref_id(new_ref)
                v, f = (new_id, other_id) if shader_key == "vertex" else (other_id, new_id)
                shader_utils.sync_all_shader_properties(mat_data, v, f, remove_unknown=True)
                state.extra["shader_sync_key"] = f"{v}|{f}:{shader_utils.get_shader_property_generation()}"

        # Vertex shader
        shader_ui = _get_material_shader_ui_cache(state, vert_ref, frag_ref)
        shader_labels = (
            layout_cache["shader_labels"]
            if isinstance(layout_cache, dict)
            else (t("material.vertex"), t("material.fragment"))
        )
        field_label(ctx, shader_labels[0], s_lw)
        vert_display = shader_ui["vertex_display"]

        _render_obj_field(
            ctx, "mat_vert", vert_display, "Vert", "SHADER_FILE",
            lambda p: _apply_shader("vertex", p, "fragment"),
            ping_path=shader_ui["vertex_path"] or None,
            has_value=bool(shader_ui["vertex_id"]),
            semantic_id="asset.material.shader.vertex",
        )

        # Fragment shader
        field_label(ctx, shader_labels[1], s_lw)
        frag_display = shader_ui["fragment_display"]

        _render_obj_field(
            ctx, "mat_frag", frag_display, "Frag", "SHADER_FILE",
            lambda p: _apply_shader("fragment", p, "vertex"),
            ping_path=shader_ui["fragment_path"] or None,
            has_value=bool(shader_ui["fragment_id"]),
            semantic_id="asset.material.shader.fragment",
        )
    _record_profile_timing("materialShader", section_t0)
    if is_builtin:
        ctx.end_disabled()
    return changed, requires_deserialize, requires_pipeline_refresh, change_key


def _prepare_surface_options_batch(ctx, rs, so_lw, cache=None):
    """Prepare the reusable native surface plan and its current values.

    The native batch still receives fresh values every frame so controls remain
    fully interactive. Descriptor dictionaries and the native plan only depend
    on the shape of the surface section, the label width, and the context that
    owns the plan, so those objects are reused by inline material inspectors.
    """
    blend_enabled = bool(rs.get("blendEnable", False))
    alpha_clip = bool(rs.get("alphaClipEnabled", False))
    depth_enabled = bool(rs.get("depthTestEnable", True))
    # ``ei`` is part of the native enum descriptor contract even when the
    # current value is refreshed through ``render_property_batch_plan_values``.
    # Keep it on every cached enum descriptor so first-use plan construction
    # and cache reuse have the same complete schema.
    surface_index = 1 if blend_enabled else 0
    cull_index = {0: 0, 1: 1, 2: 2}.get(int(rs.get("cullMode", 2)), 2)
    depth_compare = int(rs.get("depthCompareOp", 1))
    depth_index = depth_compare if depth_enabled else 7
    src = int(rs.get("srcColorBlendFactor", 6))
    dst = int(rs.get("dstColorBlendFactor", 7))
    blend_index = 1 if (src, dst) == (1, 1) else 2 if (src, dst) == (1, 7) else 0
    rq_min, rq_max = (2501, 5000) if blend_enabled else (0, 2500)
    render_queue = max(rq_min, min(int(rs.get("renderQueue", 2000)), rq_max))
    locale = get_locale()
    shape_key = (
        _SURFACE_BATCH_SCHEMA, id(ctx), locale, float(so_lw),
        blend_enabled, alpha_clip, depth_enabled,
    )
    cached_entries = cache.get("entries") if isinstance(cache, dict) else None
    cached_schema_valid = _surface_batch_cache_is_current(cached_entries)
    if (isinstance(cache, dict) and cache.get("shape_key") == shape_key
            and cache.get("context") is ctx and cached_schema_valid
            and cache.get("plan") is not None):
        entries = cache["entries"]
        plan = cache["plan"]
    else:
        entries = []

        def add(key, desc):
            entries.append((key, desc))

        surface_items = [t("material.opaque"), t("material.transparent")]
        add("surface", {"t": 7, "w": "##mat_surface_type", "n": t("material.surface_type"),
                        "ei": surface_index, "en": surface_items,
                        "sid": "asset.material.surface.type"})

        cull_items = [t("material.cull_none"), t("material.cull_front"), t("material.cull_back")]
        add("cull", {"t": 7, "w": "##mat_cull_mode", "n": t("material.cull_mode"),
                     "ei": cull_index, "en": cull_items,
                     "sid": "asset.material.surface.cull"})

        add("depth_write", {"t": 2, "w": "##mat_depth_write", "n": t("material.depth_write"),
                             "b": bool(rs.get("depthWriteEnable", True)), "fl": True,
                             "sid": "asset.material.surface.depth_write"})

        compare_items = [t("material.compare_never"), t("material.compare_less"),
                         t("material.compare_equal"), t("material.compare_less_equal"),
                         t("material.compare_greater"), t("material.compare_not_equal"),
                         t("material.compare_greater_equal"), t("material.compare_always")]
        depth_names = compare_items if depth_enabled else ["Off"] + compare_items[1:]
        add("depth_test", {"t": 7, "w": "##mat_depth_test", "n": t("material.depth_test"),
                            "ei": depth_index, "en": depth_names,
                            "sid": "asset.material.surface.depth_test"})

        if blend_enabled:
            add("blend", {"t": 7, "w": "##mat_blend_mode", "n": t("material.blend_mode"),
                           "ei": blend_index,
                           "en": [t("material.blend_alpha"), t("material.blend_additive"),
                                  t("material.blend_premultiply")],
                           "sid": "asset.material.surface.blend"})

        add("alpha_clip", {"t": 2, "w": "##mat_alpha_clip", "n": t("material.alpha_clip"),
                           "b": alpha_clip, "fl": True,
                           "sid": "asset.material.surface.alpha_clip"})
        if alpha_clip:
            add("alpha_threshold", {"t": 0, "w": "##mat_alpha_threshold", "n": t("material.threshold"),
                                     "f": float(rs.get("alphaClipThreshold", 0.5)),
                                     "mn": 0.0, "mx": 1.0, "sl": True,
                                     "sid": "asset.material.surface.alpha_threshold"})

        add("render_queue", {"t": 1, "w": "##mat_render_queue", "n": t("material.render_queue"),
                             "i": render_queue,
                             "sp": 1.0, "mn": rq_min, "mx": rq_max,
                             "sid": "asset.material.surface.render_queue"})

        # The per-document cache owns the native plan.  Keeping a module-level
        # map keyed by ``id(ctx)`` lets a recycled Python object identity reuse
        # a plan compiled from a different descriptor schema.
        plan = ctx.create_property_batch_plan([desc for _, desc in entries])
        if isinstance(cache, dict):
            cache.clear()
            cache.update({
                "shape_key": shape_key,
                "context": ctx,
                "entries": entries,
                "plan": plan,
            })

    depth_op = depth_compare
    cull_idx = cull_index
    blend_idx = blend_index
    rq = render_queue
    value_by_key = {
        "surface": 1 if blend_enabled else 0,
        "cull": cull_idx,
        "depth_write": bool(rs.get("depthWriteEnable", True)),
        "depth_test": depth_op if depth_enabled else 7,
        "blend": blend_idx,
        "alpha_clip": alpha_clip,
        "alpha_threshold": float(rs.get("alphaClipThreshold", 0.5)),
        "render_queue": rq,
    }
    values = []
    for key, desc in entries:
        value = value_by_key[key]
        if desc["t"] == 0:
            values.append(value)
        elif desc["t"] == 1:
            values.append(value)
        elif desc["t"] == 2:
            values.append(value)
        else:
            values.append(value)
    return entries, plan, values, depth_enabled, depth_op


def _apply_surface_option_changes(changes, entries, rs, mat_data, overrides,
                                  depth_enabled, depth_op):
    change_key = ""
    for raw_index, value in changes.items():
        key = entries[int(raw_index)][0]
        if key == "surface":
            if int(value) == 1:
                rs.update({"blendEnable": True, "srcColorBlendFactor": 6,
                           "dstColorBlendFactor": 7, "colorBlendOp": 0,
                           "srcAlphaBlendFactor": 0, "dstAlphaBlendFactor": 1,
                           "alphaBlendOp": 0, "depthWriteEnable": False,
                           "renderQueue": 3000})
            else:
                rs.update({"blendEnable": False, "depthWriteEnable": True, "renderQueue": 2000})
            overrides |= 0x80 | 0x10 | 0x20 | 0x02 | 0x40
        elif key == "cull":
            rs["cullMode"] = int(value)
            overrides |= 0x01
        elif key == "depth_write":
            rs["depthWriteEnable"] = bool(value)
            overrides |= 0x02
        elif key == "depth_test":
            new_op = int(value)
            if not depth_enabled and new_op > 0:
                rs["depthTestEnable"] = True
                rs["depthCompareOp"] = new_op
                overrides |= 0x04 | 0x08
            elif depth_enabled and new_op != depth_op:
                rs["depthCompareOp"] = new_op
                overrides |= 0x08
        elif key == "blend":
            rs["srcColorBlendFactor"], rs["dstColorBlendFactor"] = {
                0: (6, 7), 1: (1, 1), 2: (1, 7),
            }[int(value)]
            rs["colorBlendOp"] = 0
            overrides |= 0x20
        elif key == "alpha_clip":
            rs["alphaClipEnabled"] = bool(value)
            if value and "alphaClipThreshold" not in rs:
                rs["alphaClipThreshold"] = 0.5
            overrides |= 0x100
        elif key == "alpha_threshold":
            rs["alphaClipThreshold"] = float(value)
            overrides |= 0x100
        elif key == "render_queue":
            rs["renderQueue"] = int(value)
            overrides |= 0x40
        change_key = f"render_state.{key}"

    if change_key:
        mat_data["renderStateOverrides"] = overrides
    return overrides, change_key


def _render_surface_options_batch(ctx, rs, mat_data, overrides, so_lw, cache=None):
    """Render all steady-state surface controls through one native bridge call."""
    entries, plan, values, depth_enabled, depth_op = _prepare_surface_options_batch(
        ctx, rs, so_lw, cache=cache)
    changes = ctx.render_property_batch_plan_values(plan, values, so_lw)
    return _apply_surface_option_changes(
        changes, entries, rs, mat_data, overrides, depth_enabled, depth_op)


def _render_surface_options_section(ctx, mat_data, is_builtin, default_open,
                                    cache=None, layout_cache=None):
    """Render surface options (cull, depth, blend, alpha clip, render queue).

    Returns ``(changed, requires_deserialize, requires_pipeline_refresh, change_key)``.
    """
    changed = False
    requires_deserialize = False
    requires_pipeline_refresh = False
    change_key = ""

    if is_builtin:
        ctx.begin_disabled(True)
    section_t0 = _profile_start()
    section_label = (
        layout_cache["surface_section_label"]
        if isinstance(layout_cache, dict)
        else t("material.surface_options")
    )
    if render_compact_section_header(ctx, section_label, level="secondary",
                                     default_open=default_open):
        rs = mat_data.setdefault("renderState", {})
        overrides = int(mat_data.get("renderStateOverrides", 0))

        so_lw = (
            layout_cache["surface_label_width"]
            if isinstance(layout_cache, dict)
            else max_label_w(ctx, [
                t("material.surface_type"), t("material.cull_mode"),
                t("material.depth_write"), t("material.depth_test"),
                t("material.blend_mode"), t("material.alpha_clip"),
                t("material.render_queue"),
            ])
        )

        overrides, change_key = _render_surface_options_batch(
            ctx, rs, mat_data, overrides, so_lw, cache=cache)
        if change_key:
            changed = True
            requires_deserialize = True
            requires_pipeline_refresh = True

    _record_profile_timing("materialSurface", section_t0)
    if is_builtin:
        ctx.end_disabled()
    return changed, requires_deserialize, requires_pipeline_refresh, change_key


def _render_properties_section(ctx, mat_data, state, is_builtin, default_open):
    """Render material shader properties.

    Returns ``(changed, change_key, requires_deserialize)``.
    """
    changed = False
    change_key = ""
    requires_deserialize = False

    if is_builtin:
        ctx.begin_disabled(True)
    section_t0 = _profile_start()
    layout_cache = _get_material_layout_cache(ctx, state)
    if render_compact_section_header(
            ctx, layout_cache["properties_section_label"], level="secondary",
                                     default_open=default_open):
        props = mat_data.get("properties", {})
        if not props:
            ctx.label(layout_cache["no_properties_label"])
        else:
            layout_cache = _get_material_property_layout_cache(ctx, state, mat_data)
            prop_names = layout_cache["property_names"]
            plw = layout_cache["label_width"]
            for prop_name in prop_names:
                prop = props[prop_name]
                ptype = int(prop.get("type", 0))
                value = prop.get("value")
                prop_changed = render_material_property(
                    ctx, prop_name, prop, ptype, value, plw,
                    reference_cache=state.extra.setdefault(
                        "_material_texture_reference_cache", {}
                    ),
                )
                if prop_changed:
                    if ptype == 6:
                        _apply_native_prop(prop_name, prop.get("guid", ""), ptype)
                    else:
                        _apply_native_prop(prop_name, prop["value"], ptype)
                    changed = True
                    change_key = f"property.{prop_name}"
                    if ptype == 6:
                        requires_deserialize = True
    _record_profile_timing("materialProperties", section_t0)
    if is_builtin:
        ctx.end_disabled()
    return changed, change_key, requires_deserialize


def _apply_material_changes(panel, state, mat_data, native_mat,
                            requires_deserialize, requires_pipeline_refresh,
                            old_document, change_key, exec_layer):
    """Serialize and save material changes, record undo."""
    del exec_layer
    from Infernux.engine.interaction import AuthoringMutationService

    try:
        embedded_path = getattr(state, "file_path", "") or ""
        is_embedded = "::submat:" in embedded_path

        if is_embedded:
            state.extra["cached_json"] = json.dumps(mat_data)
            try:
                state.extra["_applied_version"] = native_mat.get_version()
            except (AttributeError, RuntimeError):
                pass
            return

        controller = getattr(state, "resource_controller", None)
        document_id = str(getattr(state, "document_id", "") or "")
        if controller is None and not document_id and not embedded_path:
            # Runtime material clones intentionally have no durable .mat
            # document. They remain editable in Play Mode, but their changes
            # are memory/GPU-local and must never be queued as asset writes.
            if requires_deserialize:
                if not native_mat.deserialize_document(copy.deepcopy(mat_data)):
                    raise RuntimeError(
                        "transient material live-preview document was rejected"
                    )
            if requires_pipeline_refresh:
                _refresh_pipeline(panel, native_mat)
            state.extra["cached_json"] = json.dumps(mat_data)
            state.extra["cached_data"] = mat_data
            state.extra["_inline_autosave_pending"] = False
            try:
                state.extra["_applied_version"] = native_mat.get_version()
            except (AttributeError, RuntimeError):
                pass
            return

        if not AuthoringMutationService.require().can_record():
            _restore_rejected_material_edit(
                panel, state, native_mat, mat_data, old_document
            )
            raise RuntimeError(
                "Material edit requires an available global Action Journal"
            )
        if controller is None or not document_id:
            raise RuntimeError("Material edit requires a formal Material document")

        # ── Deferred undo ───────────────────────────────────────────
        # During continuous drag (60 fps), calling json.dumps every frame
        # costs ~1-5 ms and is the main source of drag stutter.  Instead,
        # we save the pre-drag JSON once and mark a pending undo commit.
        # The actual json.dumps + record happens in render_material_body
        # on the first frame where changed=False (drag ended).
        # Native memory follows each rendered input frame so previews remain
        # live. The Action Journal receives one document transaction when the
        # gesture ends; intermediate text values are not separate user actions.
        if requires_deserialize:
            if not native_mat.deserialize_document(copy.deepcopy(mat_data)):
                raise RuntimeError("material live-preview document was rejected")
            if requires_pipeline_refresh:
                _refresh_pipeline(panel, native_mat)
        _update_material_edit_session(
            panel,
            state,
            native_mat,
            old_document,
            mat_data,
            change_key,
        )
    except Exception:
        _restore_rejected_material_edit(
            panel, state, native_mat, mat_data, old_document
        )
        raise


def _restore_rejected_material_edit(
    panel,
    state,
    native_mat,
    mat_data,
    old_document,
) -> None:
    """Restore both cached and native material state after a rejected edit."""
    restored = copy.deepcopy(old_document)
    mat_data.clear()
    mat_data.update(restored)
    state.extra["cached_data"] = mat_data
    state.extra["cached_json"] = json.dumps(restored)
    state.extra.pop("_material_preview_pending", None)
    if not native_mat.deserialize_document(restored):
        raise RuntimeError("material rollback document was rejected")
    _refresh_pipeline(panel)
    try:
        state.extra["_applied_version"] = native_mat.get_version()
    except (AttributeError, RuntimeError):
        pass


def _document_before_material_edit(state, mat_data) -> dict:
    """Recover the pre-edit document only on frames that actually changed it."""
    cached_json = state.extra.get("cached_json", "")
    if cached_json:
        try:
            cached_document = json.loads(cached_json)
            if isinstance(cached_document, dict):
                return cached_document
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return copy.deepcopy(mat_data)


def _material_edit_session_key(state, native_mat) -> str:
    identity = (
        getattr(state, "file_path", "")
        or getattr(native_mat, "file_path", "")
        or getattr(native_mat, "guid", "")
        or str(id(native_mat))
    )
    return f"inspector:material:{identity}"


def _commit_material_edit_session(panel, state, native_mat, session) -> None:
    old_document = copy.deepcopy(session.initial_value)
    new_document = copy.deepcopy(session.current_value)
    if new_document != old_document:
        controller = getattr(state, "resource_controller", None)
        if controller is None or not getattr(state, "document_id", ""):
            _restore_rejected_material_edit(
                panel, state, native_mat, new_document, old_document
            )
            raise RuntimeError("Material edit requires a formal Material document")
        if not controller.commit_applied_document(
            old_document,
            new_document,
            view_id="inspector",
            edit_key=str(session.metadata.get("edit_key", "")),
            description="Edit Material",
        ):
            _restore_rejected_material_edit(
                panel, state, native_mat, new_document, old_document
            )
            raise RuntimeError("Material document transaction was rejected")

    state.extra["cached_json"] = json.dumps(new_document)
    state.extra["cached_data"] = new_document


def _cancel_material_edit_session(panel, state, native_mat, session) -> None:
    old_document = copy.deepcopy(session.initial_value)
    if not native_mat.deserialize_document(old_document):
        raise RuntimeError("material rollback document was rejected")
    cached_data = state.extra.get("cached_data")
    if isinstance(cached_data, dict):
        cached_data.clear()
        cached_data.update(copy.deepcopy(old_document))
    else:
        state.extra["cached_data"] = copy.deepcopy(old_document)
    state.extra["cached_json"] = json.dumps(old_document)
    state.extra.pop("_material_preview_pending", None)
    _refresh_pipeline(panel, native_mat)


def _update_material_edit_session(
    panel,
    state,
    native_mat,
    old_document,
    mat_data,
    change_key,
) -> None:
    from Infernux.engine.interaction import ContinuousEditService

    edits = ContinuousEditService.instance()
    session_key = _material_edit_session_key(state, native_mat)
    session = edits.get(session_key)
    if session is not None and session.metadata.get("edit_key", "") != change_key:
        edits.commit(session_key)
        session = None
    if session is None:
        session = edits.begin(
            session_key,
            owner_id="inspector",
            document_id=str(getattr(state, "document_id", "") or ""),
            description="Edit Material",
            initial_value=old_document,
            metadata={"edit_key": str(change_key or "")},
            on_commit=lambda item: _commit_material_edit_session(
                panel, state, native_mat, item
            ),
            on_cancel=lambda item: _cancel_material_edit_session(
                panel, state, native_mat, item
            ),
        )
    edits.update(session.key, mat_data)


def _flush_deferred_undo(
    panel,
    state,
    mat_data,
    native_mat,
    *,
    input_active: bool = False,
    force: bool = False,
):
    """Commit the Core-owned material edit session when a gesture ends."""
    del panel, mat_data
    if input_active and not force:
        return
    from Infernux.engine.interaction import ContinuousEditService

    edits = ContinuousEditService.instance()
    session_key = _material_edit_session_key(state, native_mat)
    if force:
        edits.commit(session_key)
    else:
        edits.commit_if_idle(session_key, idle_seconds=0.75)


# ═══════════════════════════════════════════════════════════════════════════
# Body renderer (called from asset_details_renderer)
# ═══════════════════════════════════════════════════════════════════════════


def _sync_shader_annotations(mat_data, state):
    """Synchronise shader property annotations into *mat_data*.

    Returns ``(changed, requires_deserialize)`` — True if new/removed
    properties were detected and pushed to the native material.
    """
    shaders = mat_data.get("shaders", {})
    vert_ref = shaders.get("vertex", "")
    frag_ref = shaders.get("fragment", "")
    ref_token = (_shader_value_token(vert_ref), _shader_value_token(frag_ref))
    cached_ids = state.extra.get("_material_shader_ids")
    if isinstance(cached_ids, dict) and cached_ids.get("refs") == ref_token:
        vert_shader_id = cached_ids["vertex"]
        frag_shader_id = cached_ids["fragment"]
    else:
        vert_shader_id = shader_utils.shader_ref_id(vert_ref)
        frag_shader_id = shader_utils.shader_ref_id(frag_ref)
        state.extra["_material_shader_ids"] = {
            "refs": ref_token,
            "vertex": vert_shader_id,
            "fragment": frag_shader_id,
        }
    prop_gen = shader_utils.get_shader_property_generation()
    if state.extra.get("_shader_catalog_generation", -1) != prop_gen:
        state.extra["_shader_catalog_generation"] = prop_gen
        if isinstance(state.extra.get("shader_cache"), dict):
            state.extra["shader_cache"][".vert"] = None
            state.extra["shader_cache"][".frag"] = None
        state.extra.pop("_material_shader_ui_cache", None)
    sync_key = f"{vert_shader_id}|{frag_shader_id}:{prop_gen}"
    last_sync_key = state.extra.get("shader_sync_key", "")
    last_validation_key = state.extra.get("shader_validation_key", "")
    current_property_names = tuple(sorted(
        str(name) for name in mat_data.get("properties", {}).keys()))
    last_validation_property_names = tuple(
        state.extra.get("shader_validation_property_names", ()))
    needs_validation = (
        sync_key != last_validation_key
        or not mat_data.get("_shader_property_order")
        or current_property_names != last_validation_property_names
    )
    missing_shader_props = False
    if needs_validation and (vert_shader_id or frag_shader_id):
        current_props = mat_data.get("properties", {})
        expected_shader_props = shader_utils.get_all_shader_property_names(vert_shader_id, frag_shader_id)
        missing_shader_props = any(name not in current_props for name in expected_shader_props)
        state.extra["shader_validation_key"] = sync_key
        state.extra["shader_validation_property_names"] = current_property_names

    changed = False
    requires_deserialize = False
    if sync_key != last_sync_key:
        _bump_material_schema_revision(state)
    if (vert_shader_id or frag_shader_id) and (sync_key != last_sync_key or missing_shader_props):
        old_key = last_sync_key.rsplit(":", 1)[0] if last_sync_key else ""
        remove = (f"{vert_shader_id}|{frag_shader_id}" == old_key) and bool(old_key)
        state.extra["shader_sync_key"] = sync_key
        if vert_shader_id or frag_shader_id:
            old_prop_names = set(mat_data.get("properties", {}).keys())
            shader_utils.sync_all_shader_properties(mat_data, vert_shader_id, frag_shader_id,
                                                    remove_unknown=remove)
            new_prop_names = set(mat_data.get("properties", {}).keys())
            if new_prop_names != old_prop_names:
                changed = True
                requires_deserialize = True
    return changed, requires_deserialize


def _prepare_shader_annotations(mat_data, state, *, read_only: bool):
    """Synchronize shader metadata without inventing a user edit.

    Built-in materials are shared, immutable renderer resources.  Their
    Inspector copy may still need newly imported shader annotations, but that
    cache refresh must never enter the document/undo path.
    """
    changed, requires_deserialize = _sync_shader_annotations(mat_data, state)
    if not read_only or not changed:
        return changed, requires_deserialize

    state.extra["cached_data"] = mat_data
    state.extra["cached_json"] = json.dumps(mat_data)
    state.extra.pop("_material_preview_pending", None)
    return False, False


def _get_material_layout_cache(ctx, state):
    """Cache translated labels/widths while keeping locale changes observable."""
    cache = state.extra.setdefault("_material_layout_cache", {})
    key = (id(ctx), get_locale())
    if cache.get("key") != key:
        shader_labels = (t("material.vertex"), t("material.fragment"))
        surface_labels = (
            t("material.surface_type"), t("material.cull_mode"),
            t("material.depth_write"), t("material.depth_test"),
            t("material.blend_mode"), t("material.alpha_clip"),
            t("material.render_queue"),
        )
        cache.clear()
        cache.update({
            "key": key,
            "shader_labels": shader_labels,
            "surface_labels": surface_labels,
            "shader_section_label": t("material.shader_section"),
            "surface_section_label": t("material.surface_options"),
            "properties_section_label": t("material.properties_section"),
            "no_properties_label": t("material.no_properties"),
            "shader_label_width": max_label_w(ctx, shader_labels),
            "surface_label_width": max_label_w(ctx, surface_labels),
        })
    return cache


def _render_material_top_native(ctx, panel, state, mat_data, section_readonly,
                                default_open_sections, is_embedded_slot):
    """Render the stable material top area in one native bridge call."""
    native_renderer = getattr(ctx, "render_material_top", None)
    if not callable(native_renderer):
        return None

    layout_cache = _get_material_layout_cache(ctx, state)
    shaders = mat_data.setdefault("shaders", {})
    vert_ref = shaders.get("vertex", "")
    frag_ref = shaders.get("fragment", "")
    shader_ui = _get_material_shader_ui_cache(state, vert_ref, frag_ref)
    vert_items = shader_ui["vertex_items"]
    frag_items = shader_ui["fragment_items"]
    vert_display = shader_ui["vertex_display"]
    frag_display = shader_ui["fragment_display"]
    shader_lw = layout_cache["shader_label_width"]

    rs = mat_data.setdefault("renderState", {})
    overrides = int(mat_data.get("renderStateOverrides", 0))
    surface_lw = layout_cache["surface_label_width"]
    surface_cache = state.extra.setdefault("_material_surface_batch_cache", {})
    entries, surface_plan, surface_values, depth_enabled, depth_op = (
        _prepare_surface_options_batch(ctx, rs, surface_lw, cache=surface_cache)
    )

    cache_tag = state.extra.get("_material_cache_tag", "")
    if not cache_tag and not is_embedded_slot:
        try:
            cache_tag = state.extra.get("cached_json") or json.dumps(
                mat_data, sort_keys=True, ensure_ascii=False)
        except Exception:
            cache_tag = ""
        state.extra["_material_cache_tag"] = cache_tag

    now = _time.time()
    if (state.extra.get("_material_preview_pending", False)
            and now >= float(state.extra.get("_material_preview_ready_at", 0.0) or 0.0)):
        try:
            cache_tag = json.dumps(mat_data, sort_keys=True, ensure_ascii=False)
            state.extra["_material_cache_tag"] = cache_tag
        except Exception:
            cache_tag = state.extra.get("_material_cache_tag", cache_tag)
        state.extra["_material_preview_pending"] = False

    preview_path = getattr(state, "file_path", "")
    if not preview_path and _native_mat is not None:
        preview_path = _ensure_material_file_path(panel, _native_mat)
    previous_preview_path = state.extra.get("_material_preview_path", "")
    if preview_path != previous_preview_path:
        if previous_preview_path:
            from .asset_resource_preview import invalidate_live_material_preview
            invalidate_live_material_preview(previous_preview_path)
        state.extra["_material_preview_path"] = preview_path
    preview_tex_id = 0
    if preview_path or _native_mat is not None:
        preview_tex_id = _get_cached_material_preview_tex(
            panel, _native_mat, mat_data, state, cache_tag, preview_path)

    from .editor_icons import EditorIcons
    interaction = native_renderer(
        layout_cache["shader_section_label"], layout_cache["shader_labels"][0], vert_display,
        layout_cache["shader_labels"][1], frag_display, shader_lw,
        layout_cache["surface_section_label"], surface_plan, surface_values, surface_lw,
        int(EditorIcons.get_cached(Theme.ICON_IMG_PICKER) or 0),
        int(preview_tex_id or 0), "Material preview unavailable.",
        bool(default_open_sections), bool(section_readonly),
    )
    (vert_flags, vert_picker_open, vert_payload, vert_list_open,
     frag_flags, frag_picker_open, frag_payload, frag_list_open,
     surface_changes, active_surface_index,
     deactivated_surface_index) = interaction

    changed = False
    requires_deserialize = False
    requires_pipeline_refresh = False
    change_key = ""

    def apply_shader(shader_key, new_value, other_key):
        nonlocal changed, requires_deserialize, requires_pipeline_refresh, change_key
        old_val = shaders.get(shader_key, "")
        ext = ".vert" if shader_key == "vertex" else ".frag"
        if isinstance(new_value, dict):
            new_value = (
                new_value.get("path_hint")
                or new_value.get("guid")
                or new_value.get("builtin")
                or ""
            )
        new_ref = shader_utils.make_shader_reference(new_value, ext)
        if not new_ref["guid"] and not new_ref["shader_id"]:
            return
        shaders[shader_key] = new_ref
        if new_ref != old_val:
            _bump_material_schema_revision(state)
        changed = True
        change_key = f"shader.{shader_key}"
        requires_deserialize = True
        requires_pipeline_refresh = True
        if new_ref != old_val:
            other_id = shader_utils.shader_ref_id(shaders.get(other_key, ""))
            new_id = shader_utils.shader_ref_id(new_ref)
            vert_id, frag_id = ((new_id, other_id) if shader_key == "vertex"
                                else (other_id, new_id))
            shader_utils.sync_all_shader_properties(
                mat_data, vert_id, frag_id, remove_unknown=True)
            state.extra["shader_sync_key"] = (
                f"{vert_id}|{frag_id}:{shader_utils.get_shader_property_generation()}"
            )

    from .igui import IGUI
    from Infernux.engine.interaction import AssetReferenceFieldModel

    def process_shader_field(field_id, flags, picker_open, list_open,
                             popup_id, items, selected_id, selected_ref,
                             reference_path, payload, apply):
        del list_open, popup_id, items
        ext = ".vert" if field_id == "mat_vert" else ".frag"
        asset_type = "Shader.Vertex" if ext == ".vert" else "Shader.Fragment"
        reference_value = {
            "asset_type": asset_type,
            "guid": str(selected_ref.get("guid") or "")
            if isinstance(selected_ref, dict) else "",
            "path_hint": reference_path,
            "builtin": (
                str(selected_ref.get("shader_id") or "")
                if isinstance(selected_ref, dict) and not reference_path
                else ""
            ),
        }
        model = AssetReferenceFieldModel(
            field_id=field_id,
            display_text=(vert_display if field_id == "mat_vert" else frag_display),
            type_hint=asset_type,
            accept="SHADER_FILE",
            on_assign=apply,
            ping_path=reference_path or None,
            has_value=bool(str(selected_id or "").strip()),
            reference_value=reference_value,
            semantic_id=(
                "asset.material.shader.vertex"
                if field_id == "mat_vert"
                else "asset.material.shader.fragment"
            ),
        )
        if payload:
            model.dispatch_drop(payload)
        IGUI.process_object_field_interaction(
            ctx,
            model,
            int(flags),
            picker_open=bool(picker_open),
            poll_picker=False,
        )

    process_shader_field(
        "mat_vert", int(vert_flags), bool(vert_picker_open), bool(vert_list_open),
        "mat_vert_popup", vert_items, shader_ui["vertex_id"], vert_ref,
        shader_ui["vertex_path"],
        vert_payload,
        lambda value: apply_shader("vertex", value, "fragment"),
    )
    process_shader_field(
        "mat_frag", int(frag_flags), bool(frag_picker_open), bool(frag_list_open),
        "mat_frag_popup", frag_items, shader_ui["fragment_id"], frag_ref,
        shader_ui["fragment_path"],
        frag_payload,
        lambda value: apply_shader("fragment", value, "vertex"),
    )

    overrides, surface_change_key = _apply_surface_option_changes(
        surface_changes, entries, rs, mat_data, overrides, depth_enabled, depth_op)
    if surface_change_key:
        changed = True
        requires_deserialize = True
        requires_pipeline_refresh = True
        change_key = surface_change_key

    return (
        changed,
        requires_deserialize,
        requires_pipeline_refresh,
        change_key,
        int(active_surface_index) >= 0,
        int(deactivated_surface_index) >= 0,
    )


def _render_material_top_legacy(ctx, panel, state, mat_data, section_readonly,
                                default_open_sections, is_embedded_slot):
    """Compatibility path for bindings without the native material-top batch."""
    changed = False
    requires_deserialize = False
    requires_pipeline_refresh = False
    change_key = ""

    did_split = ctx.begin_table("##material_top_split", 2, 0, 0.0)
    if did_split:
        ctx.table_next_column()

    layout_cache = _get_material_layout_cache(ctx, state)
    s_ch, s_ds, s_pr, s_ck = _render_shader_section(
        ctx, mat_data, state, section_readonly, default_open_sections,
        layout_cache=layout_cache)
    changed |= s_ch
    requires_deserialize |= s_ds
    requires_pipeline_refresh |= s_pr
    if s_ck:
        change_key = s_ck

    ctx.separator()
    surface_cache = state.extra.setdefault("_material_surface_batch_cache", {})
    so_ch, so_ds, so_pr, so_ck = _render_surface_options_section(
        ctx, mat_data, section_readonly, default_open_sections,
        cache=surface_cache, layout_cache=layout_cache)
    changed |= so_ch
    requires_deserialize |= so_ds
    requires_pipeline_refresh |= so_pr
    if so_ck:
        change_key = so_ck

    now = _time.time()
    cache_tag = state.extra.get("_material_cache_tag", "")
    if not cache_tag and not is_embedded_slot:
        try:
            cache_tag = state.extra.get("cached_json") or json.dumps(
                mat_data, sort_keys=True, ensure_ascii=False)
        except Exception:
            cache_tag = ""
        state.extra["_material_cache_tag"] = cache_tag
    if (state.extra.get("_material_preview_pending", False)
            and now >= float(state.extra.get("_material_preview_ready_at", 0.0) or 0.0)):
        try:
            cache_tag = json.dumps(mat_data, sort_keys=True, ensure_ascii=False)
            state.extra["_material_cache_tag"] = cache_tag
        except Exception:
            cache_tag = state.extra.get("_material_cache_tag", cache_tag)
        state.extra["_material_preview_pending"] = False

    if did_split:
        ctx.table_next_column()
        avail_w = max(140.0, ctx.get_content_region_avail_width())
        preview_size = min(max(avail_w * 0.90, 140.0), 240.0)
        preview_path = getattr(state, "file_path", "")
        if not preview_path and _native_mat is not None:
            preview_path = _ensure_material_file_path(panel, _native_mat)
        previous_preview_path = state.extra.get("_material_preview_path", "")
        if preview_path != previous_preview_path:
            if previous_preview_path:
                from .asset_resource_preview import invalidate_live_material_preview
                invalidate_live_material_preview(previous_preview_path)
            state.extra["_material_preview_path"] = preview_path
        preview_tex_id = 0
        if preview_path or _native_mat is not None:
            preview_tex_id = _get_cached_material_preview_tex(
                panel, _native_mat, mat_data, state, cache_tag, preview_path)
        if not _draw_centered_texture(ctx, preview_tex_id, avail_w, preview_size, 256, 256):
            ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
            ctx.label("Material preview unavailable.")
            ctx.pop_style_color(1)
        ctx.end_table()
    ctx.separator()
    return (
        changed,
        requires_deserialize,
        requires_pipeline_refresh,
        change_key,
        bool(ctx.is_any_item_active()),
        bool(ctx.is_item_deactivated_after_edit()),
    )


def render_material_body(ctx: InxGUIContext, panel, state):
    """Render a material while guaranteeing balanced ImGui state on errors."""
    native_mat = state.extra["native_mat"]
    is_embedded_slot = "::submat:" in (
        (getattr(native_mat, "file_path", "") or getattr(state, "file_path", "") or "")
    )
    pushed_style_vars = 0
    disabled = False
    try:
        ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding, *Theme.INSPECTOR_FRAME_PAD)
        pushed_style_vars += 1
        ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing, *Theme.INSPECTOR_ITEM_SPC)
        pushed_style_vars += 1
        if is_embedded_slot:
            ctx.begin_disabled(True)
            disabled = True
        _render_material_body_impl(ctx, panel, state)
    finally:
        try:
            if disabled:
                ctx.end_disabled()
        finally:
            if pushed_style_vars:
                ctx.pop_style_var(pushed_style_vars)


def _render_material_body_impl(ctx: InxGUIContext, panel, state):
    """Render the material-specific inspector body.

    *state* is the ``_State`` object from ``asset_details_renderer``.  Relevant
    fields: ``state.settings`` (Material wrapper), ``state.extra``
    (native_mat, cached_data, shader_cache), ``state.exec_layer``.
    """
    global _native_mat, _cached_data, _shader_cache

    _native_mat = state.extra["native_mat"]
    _cached_data = state.extra["cached_data"]
    _shader_cache = state.extra["shader_cache"]
    exec_layer = state.exec_layer
    mat_data = _cached_data
    is_builtin = bool(getattr(_native_mat, "is_builtin", False) or mat_data.get("builtin", False))
    if is_builtin:
        mat_data["builtin"] = True

    is_embedded_slot = "::submat:" in (
        (getattr(_native_mat, "file_path", "") or getattr(state, "file_path", "") or "")
    )
    # Built-in materials use per-section disabled(); embedded uses one outer disabled()
    # so every control (including pickers) looks non-interactive like a default slot material.
    section_readonly = is_builtin

    default_open_sections = bool(state.extra.get("default_open_sections", True))

    if is_builtin:
        ctx.label(t("material.builtin_locked"))
    elif is_embedded_slot:
        ctx.label(t("material.embedded_model_slot"))

    changed = False
    requires_deserialize = False
    requires_pipeline_refresh = False
    change_key = ""

    # Sync shader annotations (both vertex + fragment properties)
    sync_ch, sync_ds = _prepare_shader_annotations(
        mat_data,
        state,
        read_only=is_builtin,
    )
    changed |= sync_ch
    requires_deserialize |= sync_ds

    # ── Top row: Shader + Surface + live preview ───────────────────────
    # Shader ObjectFields own live ImGui popups. Keep their chrome and popup
    # contents on the shared field call path instead of folding them into the
    # native material-top batch, whose return boundary breaks popup identity.
    # Surface controls remain native-batched inside the shared implementation.
    top_result = _render_material_top_legacy(
        ctx, panel, state, mat_data, section_readonly,
        default_open_sections, is_embedded_slot)
    (
        top_changed,
        top_deserialize,
        top_pipeline_refresh,
        top_change_key,
        top_edit_active,
        top_edit_finished,
    ) = top_result
    changed |= top_changed
    requires_deserialize |= top_deserialize
    requires_pipeline_refresh |= top_pipeline_refresh
    if top_change_key:
        change_key = top_change_key

    # ── Properties ─────────────────────────────────────────────────────
    p_ch, p_ck, p_ds = _render_virtualized_material_block(
        ctx, state, "properties",
        lambda: _render_properties_section(
            ctx, mat_data, state, section_readonly, default_open_sections,
        ),
        (False, "", False),
    )
    changed |= p_ch
    requires_deserialize |= p_ds
    if p_ck:
        change_key = p_ck

    ctx.separator()

    # ── Auto-save on change ─────────────────────────────────────────────
    if changed:
        # Publish the latest document on the following frame.
        state.extra["_material_preview_pending"] = True
        state.extra["_material_preview_ready_at"] = _time.time()
        state.extra["_inline_autosave_pending"] = True
        old_document = _document_before_material_edit(state, mat_data)
        _apply_material_changes(panel, state, mat_data, _native_mat,
                                requires_deserialize, requires_pipeline_refresh,
                                old_document, change_key, exec_layer)
        if top_edit_finished:
            _flush_deferred_undo(
                panel, state, mat_data, _native_mat, force=True
            )
        # Mark the native version as "ours" so _refresh_material (called once
        # per frame by asset_details_renderer) can skip the expensive
        # serialize -> json.loads -> merge -> json.dumps round-trip.
        try:
            state.extra["_applied_version"] = _native_mat.get_version()
        except (AttributeError, RuntimeError):
            pass
    else:
        # Drag ended (or no edit this frame) — commit deferred undo snapshot.
        _flush_deferred_undo(
            panel,
            state,
            mat_data,
            _native_mat,
            input_active=bool(
                top_edit_active or ctx.is_any_item_active() or ctx.want_text_input()
            ),
            force=top_edit_finished,
        )

def render_inline_material_body(ctx: InxGUIContext, panel, native_mat, cache_key: str | None = None) -> None:
    """Render a MeshRenderer-linked material using the shared material inspector."""
    if native_mat is None:
        return
    inline_t0 = _profile_start()
    state = _build_inline_state(panel, native_mat)
    ctx.push_id_str(cache_key or f"inline_material_{id(native_mat)}")
    try:
        render_material_body(ctx, panel, state)
    finally:
        ctx.pop_id()
        _record_profile_timing("materialInline", inline_t0)

    # Inline materials don't go through render_asset_inspector's footer,
    # so the debounced save would never be flushed.  Drive it here.
    _inline_layer = getattr(panel, "_inline_material_exec_layer", None)
    if _inline_layer is not None and state.extra.get("_inline_autosave_pending", False):
        if _inline_layer.flush_rw_autosave():
            state.extra["_inline_autosave_pending"] = False


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _on_shader_drop(path: str, required_ext: str, shaders_dict: dict):
    if path.lower().endswith(required_ext):
        key = "vertex" if required_ext == ".vert" else "fragment"
        old = shaders_dict.get(key, "")
        reference = shader_utils.make_shader_reference(path, required_ext)
        shaders_dict[key] = reference
        if reference != old and _cached_data:
            vert_id = shader_utils.shader_ref_id(shaders_dict.get("vertex", ""))
            frag_id = shader_utils.shader_ref_id(shaders_dict.get("fragment", ""))
            shader_utils.sync_all_shader_properties(_cached_data, vert_id, frag_id, remove_unknown=True)


def _render_obj_field(ctx: InxGUIContext, fid: str, display: str, type_hint: str,
                      drag_type: str, on_assign,
                      on_clear=None,
                      on_ping=None, ping_path=None, has_value=None,
                      semantic_id: str = "", asset_type: str = "",
                      reference_value=None) -> bool:
    """Simplified object-field renderer accepting drag-drop."""
    from . import inspector_components as comp_ui
    resolved_asset_type = asset_type or type_hint
    return comp_ui.render_asset_reference_field(ctx, fid, display, type_hint,
                                       asset_type=resolved_asset_type,
                                       clickable=True,
                                       accept_drag_type=drag_type,
                                       on_assign=on_assign,
                                       on_clear=on_clear,
                                       on_ping=on_ping,
                                       ping_path=ping_path,
                                       has_value=has_value,
                                       reference_value=(
                                           reference_value
                                           if reference_value is not None
                                           else ({
                                               "asset_type": resolved_asset_type,
                                               "path_hint": str(ping_path or ""),
                                           } if has_value else None)
                                       ),
                                       semantic_id=semantic_id)


def _apply_native_prop(prop_name: str, value, ptype: int):
    """Forward a property change to the C++ material."""
    if not _native_mat:
        return
    if ptype == 0:
        _native_mat.set_float(prop_name, float(value))
    elif ptype == 1:
        _native_mat.set_vector2(prop_name, (float(value[0]), float(value[1])))
    elif ptype == 2:
        _native_mat.set_vector3(prop_name, (float(value[0]), float(value[1]), float(value[2])))
    elif ptype == 3:
        _native_mat.set_vector4(prop_name, (float(value[0]), float(value[1]), float(value[2]), float(value[3])))
    elif ptype == 4:
        _native_mat.set_int(prop_name, int(value))
    elif ptype == 5:
        _native_mat.set_matrix(prop_name, [float(v) for v in value])
    elif ptype == 6:
        _native_mat.set_texture_guid(prop_name, str(value))
    elif ptype == 7:
        _native_mat.set_color(prop_name, (float(value[0]), float(value[1]), float(value[2]), float(value[3])))


class _InlineMaterialExecLayer:
    """Lightweight adapter so inline material editing uses the same autosave path."""

    # The adapter exists for view-local rendering state and may disappear or
    # temporarily fail to resolve the selected slot.  Durable asset writes
    # must therefore use the controller's stable category/path directly.
    view_scoped_persistence = True

    def __init__(self, panel):
        self._panel = panel

    def schedule_rw_save(self, resource_obj):
        file_path = _ensure_material_file_path(self._panel, resource_obj)
        if not file_path:
            return
        current_layer = getattr(self._panel, "_inline_material_exec_layer", None)
        layer = get_asset_execution_layer(
            current_layer,
            "material",
            file_path,
            AssetAccessMode.READ_WRITE_RESOURCE,
            autosave_debounce_sec=0.25,
        )
        self._panel._inline_material_exec_layer = layer
        layer.schedule_rw_save(resource_obj)

    def flush_rw_autosave(self, *, force: bool = False):
        layer = getattr(self._panel, "_inline_material_exec_layer", None)
        if layer is None or layer is self:
            return False
        return layer.flush_rw_autosave(force=force)

    def cancel_rw_autosave(self) -> bool:
        layer = getattr(self._panel, "_inline_material_exec_layer", None)
        if layer is None or layer is self:
            return False
        return bool(layer.cancel_rw_autosave())

    def refresh_binding(self, category: str, file_path: str) -> None:
        del category, file_path


def _build_inline_state(panel, native_mat):
    extra = _get_inline_material_extra(panel, native_mat)
    mat_path = getattr(native_mat, "file_path", "") or ""
    state = extra.get("_inline_state")
    if state is None:
        state = SimpleNamespace(
            extra=extra,
            exec_layer=_InlineMaterialExecLayer(panel),
            file_path=mat_path,
            category="material",
            settings=native_mat,
            resource_controller=None,
            document_id="",
        )
        extra["_inline_state"] = state
    else:
        state.file_path = mat_path
    binding_key = (
        mat_path,
        str(getattr(native_mat, "guid", "") or ""),
        id(state.exec_layer),
    )
    if (mat_path and "::submat:" not in mat_path and not bool(
        getattr(native_mat, "is_builtin", False)
    ) and (
        state.resource_controller is None
        or extra.get("_inline_document_binding_key") != binding_key
    )):
        from Infernux.engine.interaction import (
            DocumentKind,
            ensure_editable_resource_document,
        )

        controller = ensure_editable_resource_document(
            category="material",
            document_kind=DocumentKind.MATERIAL,
            file_path=mat_path,
            resource=native_mat,
            guid=str(getattr(native_mat, "guid", "") or ""),
            title=os.path.basename(mat_path),
            view_id="inspector",
            exec_layer=state.exec_layer,
            on_restored=notify_material_document_restored,
            autosave_debounce_sec=0.25,
        )
        state.resource_controller = controller
        state.settings = getattr(controller, "resource", native_mat)
        state.document_id = controller.document_id
        extra["_inline_document_binding_key"] = binding_key
    return state


def _get_inline_material_extra(panel, native_mat) -> dict:
    cache = getattr(panel, "_inline_material_cache", None)
    if cache is None:
        cache = {}
        panel._inline_material_cache = cache

    mat_id = id(native_mat)
    try:
        mat_version = native_mat.get_version()
    except (AttributeError, RuntimeError):
        mat_version = -1

    extra = cache.get(mat_id)
    cache_hit = (extra is not None and (
        mat_version == -1
        or extra.get("mat_version", -1) == mat_version
        or extra.get("_applied_version", -2) == mat_version
    ))
    if cache_hit:
        extra["mat_version"] = mat_version
        return extra

    try:
        fresh = native_mat.serialize_document()
    except (RuntimeError, ValueError, TypeError):
        fresh = {}

    shader_cache = extra.get("shader_cache") if isinstance(extra, dict) else None
    if not isinstance(shader_cache, dict):
        shader_cache = {".vert": None, ".frag": None}

    extra = {
        "native_mat": native_mat,
        "cached_data": fresh,
        "cached_json": json.dumps(fresh),
        "shader_cache": shader_cache,
        "shader_sync_key": extra.get("shader_sync_key", "") if isinstance(extra, dict) else "",
        "mat_version": mat_version,
        "mat_ref": native_mat,
        "default_open_sections": True,
    }
    cache[mat_id] = extra
    return extra


def _refresh_pipeline(panel, native_mat=None):
    """Ask the engine to rebuild the material pipeline."""
    engine = None
    if panel and hasattr(panel, '_get_native_engine'):
        engine = panel._get_native_engine()
    if engine is None:
        try:
            from Infernux.engine.ui.editor_services import EditorServices
            svc = EditorServices.instance()
            if svc:
                engine = svc.native_engine
        except Exception as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            pass
    material = native_mat or _native_mat
    if engine and material and hasattr(engine, 'refresh_material_pipeline'):
        engine.refresh_material_pipeline(material)


def notify_material_document_restored(resource) -> None:
    """Publish a restored Material document to rendering and preview views."""
    native_mat = getattr(resource, "native", None) or resource
    _refresh_pipeline(None, native_mat)
    file_path = str(getattr(native_mat, "file_path", "") or "")
    if file_path:
        from .asset_resource_preview import invalidate_live_material_preview

        invalidate_live_material_preview(file_path)


def _ensure_material_file_path(panel, native_mat) -> str:
    """Ensure a native material has a stable file path for autosave/undo replay."""
    if not native_mat:
        return ""
    file_path = getattr(native_mat, "file_path", "") or ""
    if file_path:
        return file_path
    if panel and hasattr(panel, "_ensure_material_file_path"):
        return panel._ensure_material_file_path(native_mat)
    return ""


