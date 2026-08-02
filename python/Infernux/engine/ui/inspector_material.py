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
from Infernux.engine.i18n import t
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
    return tex_guid[:8] + "..."


def _render_texture2d_property(ctx, prop, prop_name, wid_prefix, plw):
    """Render a canonical GUID-backed Texture2D property. Returns True if changed."""
    changed = False
    display = _resolve_texture_display(prop)
    field_label(ctx, prop_name, plw)

    from .igui import IGUI

    def _on_tex_drop(payload):
        nonlocal changed
        dropped = str(payload).replace("\\", "/")
        guid = _resolve_path_to_guid(dropped)
        if not guid:
            logging.getLogger(__name__).warning(
                "Cannot resolve dropped texture path to GUID: %s", dropped)
            return
        prop["guid"] = guid
        changed = True

    def _on_tex_pick(picked_path):
        nonlocal changed
        picked = str(picked_path).replace("\\", "/")
        guid = _resolve_path_to_guid(picked)
        if not guid:
            logging.getLogger(__name__).warning(
                "Cannot resolve picked texture path to GUID: %s", picked)
            return
        prop["guid"] = guid
        changed = True

    def _on_tex_clear():
        nonlocal changed
        prop["guid"] = ""
        changed = True

    def _tex_asset_items(filt):
        from .inspector_components import _picker_assets
        return _picker_assets(filt, "*.png", assets_only=True) + _picker_assets(filt, "*.jpg", assets_only=True)

    IGUI.object_field(
        ctx,
        f"{wid_prefix}_{prop_name}_tex",
        display, "Texture",
        clickable=True,
        accept="TEXTURE_FILE",
        on_drop=_on_tex_drop,
        picker_asset_items=_tex_asset_items,
        on_pick=_on_tex_pick,
        on_clear=_on_tex_clear,
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
        changed = _render_texture2d_property(ctx, prop, prop_name, wid_prefix, plw)

    elif ptype == 7:  # Color
        if value is not None and len(value) >= 4:
            x, y, z, w = value[0], value[1], value[2], value[3]
        else:
            x, y, z, w = 1.0, 1.0, 1.0, 1.0
        field_label(ctx, prop_name, plw)
        allow_hdr = bool(prop.get("hdr", False))
        nr, ng, nb, na = _render_color_bar(
            ctx, wid, float(x), float(y), float(z), float(w), allow_hdr=allow_hdr)
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
_surface_batch_plans: dict = {}


# ═══════════════════════════════════════════════════════════════════════════
# Extracted section renderers (called from render_material_body)
# ═══════════════════════════════════════════════════════════════════════════

def _render_shader_section(ctx, mat_data, state, is_builtin, default_open):
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
    if render_compact_section_header(ctx, t("material.shader_section"), level="secondary",
                                     default_open=default_open):
        shaders = mat_data.setdefault("shaders", {})
        vert_ref = shaders.get("vertex", "")
        frag_ref = shaders.get("fragment", "")
        vert_shader_id = shader_utils.shader_ref_id(vert_ref)
        frag_shader_id = shader_utils.shader_ref_id(frag_ref)
        s_lw = max_label_w(ctx, [t("material.vertex"), t("material.fragment")])
        from .inspector_components import _picker_assets

        def _apply_shader(shader_key, new_value, other_key):
            nonlocal changed, requires_deserialize, requires_pipeline_refresh, change_key
            old_val = shaders.get(shader_key, "")
            ext = ".vert" if shader_key == "vertex" else ".frag"
            new_ref = shader_utils.make_shader_reference(new_value, ext)
            if not new_ref["guid"] and not new_ref["shader_id"]:
                return
            shaders[shader_key] = new_ref
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
        field_label(ctx, t("material.vertex"), s_lw)
        vert_items = shader_utils.get_shader_candidates(".vert", _shader_cache)
        vert_display = shader_utils.shader_display_from_value(vert_ref, vert_items)

        if _render_obj_field(ctx, "mat_vert", vert_display, "Vert", "SHADER_FILE",
                             lambda p: _apply_shader("vertex", p, "fragment"),
                             picker_asset_items=lambda filt: _picker_assets(filt, "*.vert"),
                             on_pick=lambda picked: _apply_shader("vertex", picked, "fragment"),
                             semantic_id="asset.material.shader.vertex"):
            ctx.open_popup("mat_vert_popup")
        if ctx.begin_popup("mat_vert_popup"):
            for display, value in vert_items:
                if ctx.selectable(display, value == vert_shader_id):
                    _apply_shader("vertex", value, "fragment")
            ctx.end_popup()

        # Fragment shader
        field_label(ctx, t("material.fragment"), s_lw)
        frag_items = shader_utils.get_shader_candidates(".frag", _shader_cache)
        frag_display = shader_utils.shader_display_from_value(frag_ref, frag_items)

        if _render_obj_field(ctx, "mat_frag", frag_display, "Frag", "SHADER_FILE",
                             lambda p: _apply_shader("fragment", p, "vertex"),
                             picker_asset_items=lambda filt: _picker_assets(filt, "*.frag"),
                             on_pick=lambda picked: _apply_shader("fragment", picked, "vertex"),
                             semantic_id="asset.material.shader.fragment"):
            ctx.open_popup("mat_frag_popup")
        if ctx.begin_popup("mat_frag_popup"):
            for display, value in frag_items:
                if ctx.selectable(display, value == frag_shader_id):
                    _apply_shader("fragment", value, "vertex")
            ctx.end_popup()
    _record_profile_timing("materialShader", section_t0)
    if is_builtin:
        ctx.end_disabled()
    return changed, requires_deserialize, requires_pipeline_refresh, change_key


def _prepare_surface_options_batch(ctx, rs, so_lw):
    """Prepare the reusable native surface plan and its current values."""
    entries = []

    def add(key, desc):
        entries.append((key, desc))

    surface_items = [t("material.opaque"), t("material.transparent")]
    add("surface", {"t": 7, "w": "##mat_surface_type", "n": t("material.surface_type"),
                    "ei": 1 if rs.get("blendEnable", False) else 0, "en": surface_items,
                    "sid": "asset.material.surface.type"})

    cull_items = [t("material.cull_none"), t("material.cull_front"), t("material.cull_back")]
    cull_idx = {0: 0, 1: 1, 2: 2}.get(int(rs.get("cullMode", 2)), 2)
    add("cull", {"t": 7, "w": "##mat_cull_mode", "n": t("material.cull_mode"),
                 "ei": cull_idx, "en": cull_items, "sid": "asset.material.surface.cull"})

    add("depth_write", {"t": 2, "w": "##mat_depth_write", "n": t("material.depth_write"),
                        "b": bool(rs.get("depthWriteEnable", True)), "fl": True,
                        "sid": "asset.material.surface.depth_write"})

    compare_items = [t("material.compare_never"), t("material.compare_less"),
                     t("material.compare_equal"), t("material.compare_less_equal"),
                     t("material.compare_greater"), t("material.compare_not_equal"),
                     t("material.compare_greater_equal"), t("material.compare_always")]
    depth_enabled = bool(rs.get("depthTestEnable", True))
    depth_op = int(rs.get("depthCompareOp", 1))
    depth_names = compare_items if depth_enabled else ["Off"] + compare_items[1:]
    add("depth_test", {"t": 7, "w": "##mat_depth_test", "n": t("material.depth_test"),
                       "ei": depth_op if depth_enabled else 7, "en": depth_names,
                       "sid": "asset.material.surface.depth_test"})

    if rs.get("blendEnable", False):
        src = int(rs.get("srcColorBlendFactor", 6))
        dst = int(rs.get("dstColorBlendFactor", 7))
        blend_idx = 1 if (src, dst) == (1, 1) else 2 if (src, dst) == (1, 7) else 0
        add("blend", {"t": 7, "w": "##mat_blend_mode", "n": t("material.blend_mode"),
                      "ei": blend_idx,
                      "en": [t("material.blend_alpha"), t("material.blend_additive"),
                             t("material.blend_premultiply")],
                      "sid": "asset.material.surface.blend"})

    alpha_clip = bool(rs.get("alphaClipEnabled", False))
    add("alpha_clip", {"t": 2, "w": "##mat_alpha_clip", "n": t("material.alpha_clip"),
                       "b": alpha_clip, "fl": True,
                       "sid": "asset.material.surface.alpha_clip"})
    if alpha_clip:
        add("alpha_threshold", {"t": 0, "w": "##mat_alpha_threshold", "n": t("material.threshold"),
                                "f": float(rs.get("alphaClipThreshold", 0.5)),
                                "mn": 0.0, "mx": 1.0, "sl": True,
                                "sid": "asset.material.surface.alpha_threshold"})

    is_transparent = bool(rs.get("blendEnable", False))
    rq_min, rq_max = (2501, 5000) if is_transparent else (0, 2500)
    rq = max(rq_min, min(int(rs.get("renderQueue", 2000)), rq_max))
    add("render_queue", {"t": 1, "w": "##mat_render_queue", "n": t("material.render_queue"),
                         "i": rq, "sp": 1.0, "mn": rq_min, "mx": rq_max,
                         "sid": "asset.material.surface.render_queue"})

    plan_key = tuple(
        (key, desc["t"], desc["w"], desc["n"], tuple(desc.get("en", ())),
         desc.get("mn"), desc.get("mx"), desc.get("sp"), desc.get("sl"), desc.get("fl"),
         desc.get("sid", ""))
        for key, desc in entries
    )
    plan = _surface_batch_plans.get(plan_key)
    if plan is None:
        plan = ctx.create_property_batch_plan([desc for _, desc in entries])
        _surface_batch_plans[plan_key] = plan

    values = []
    for _, desc in entries:
        if desc["t"] == 0:
            values.append(desc["f"])
        elif desc["t"] == 1:
            values.append(desc["i"])
        elif desc["t"] == 2:
            values.append(desc["b"])
        else:
            values.append(desc["ei"])
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


def _render_surface_options_batch(ctx, rs, mat_data, overrides, so_lw):
    """Render all steady-state surface controls through one native bridge call."""
    entries, plan, values, depth_enabled, depth_op = _prepare_surface_options_batch(
        ctx, rs, so_lw)
    changes = ctx.render_property_batch_plan_values(plan, values, so_lw)
    return _apply_surface_option_changes(
        changes, entries, rs, mat_data, overrides, depth_enabled, depth_op)


def _render_surface_options_section(ctx, mat_data, is_builtin, default_open):
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
    if render_compact_section_header(ctx, t("material.surface_options"), level="secondary",
                                     default_open=default_open):
        rs = mat_data.setdefault("renderState", {})
        overrides = int(mat_data.get("renderStateOverrides", 0))

        so_labels = [t("material.surface_type"), t("material.cull_mode"), t("material.depth_write"),
                     t("material.depth_test"), t("material.blend_mode"), t("material.alpha_clip"),
                     t("material.render_queue")]
        so_lw = max_label_w(ctx, so_labels)

        overrides, change_key = _render_surface_options_batch(ctx, rs, mat_data, overrides, so_lw)
        if change_key:
            changed = True
            requires_deserialize = True
            requires_pipeline_refresh = True

    _record_profile_timing("materialSurface", section_t0)
    if is_builtin:
        ctx.end_disabled()
    return changed, requires_deserialize, requires_pipeline_refresh, change_key


def _render_properties_section(ctx, mat_data, is_builtin, default_open):
    """Render material shader properties.

    Returns ``(changed, change_key, requires_deserialize)``.
    """
    changed = False
    change_key = ""
    requires_deserialize = False

    if is_builtin:
        ctx.begin_disabled(True)
    section_t0 = _profile_start()
    if render_compact_section_header(ctx, t("material.properties_section"), level="secondary",
                                     default_open=default_open):
        props = mat_data.get("properties", {})
        if not props:
            ctx.label(t("material.no_properties"))
        else:
            prop_names = shader_utils.get_material_property_display_order(mat_data)
            plw = max_label_w(ctx, prop_names)
            for prop_name in prop_names:
                prop = props[prop_name]
                ptype = int(prop.get("type", 0))
                value = prop.get("value")
                prop_changed = render_material_property(
                    ctx, prop_name, prop, ptype, value, plw,
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
    try:
        embedded_path = getattr(state, "file_path", "") or ""
        is_embedded = "::submat:" in embedded_path

        # Hot-path optimization: most property drags already push values via
        # native setters in render_material_property. Full JSON deserialize is
        # only needed when material structure changed (shader sync/texture slots).
        if requires_deserialize:
            if not native_mat.deserialize_document(mat_data):
                raise RuntimeError("material document was rejected by the current native schema")
        if requires_pipeline_refresh:
            _refresh_pipeline(panel)

        if is_embedded:
            state.extra["cached_json"] = json.dumps(mat_data)
            try:
                state.extra["_applied_version"] = native_mat.get_version()
            except (AttributeError, RuntimeError):
                pass
            return

        # Use cached file-path when available (avoids per-frame panel lookup).
        file_path = state.extra.get("_mat_file_path", "")
        if not file_path:
            file_path = _ensure_material_file_path(panel, native_mat)
            if file_path:
                state.extra["_mat_file_path"] = file_path

        if exec_layer:
            exec_layer.schedule_rw_save(native_mat)

        # ── Deferred undo ───────────────────────────────────────────
        # During continuous drag (60 fps), calling json.dumps every frame
        # costs ~1-5 ms and is the main source of drag stutter.  Instead,
        # we save the pre-drag JSON once and mark a pending undo commit.
        # The actual json.dumps + record happens in render_material_body
        # on the first frame where changed=False (drag ended).
        if requires_deserialize:
            # Structural changes (shader swap, texture slot) always need
            # an immediate snapshot because they can't be replayed
            # incrementally.
            new_json = json.dumps(mat_data)
            state.extra["cached_json"] = new_json
            # Pre-capture save snapshot.
            if file_path:
                from Infernux.core.assets import AssetManager
                AssetManager.set_material_save_snapshot(file_path, new_json)
            from Infernux.engine.undo import UndoManager, MaterialDocumentCommand
            mgr = UndoManager.instance()
            new_document = copy.deepcopy(mat_data)
            if mgr and not mgr.is_executing and mgr.enabled and new_document != old_document:
                mgr.record(MaterialDocumentCommand(
                    native_mat, old_document, new_document, "Edit Material",
                    refresh_callback=lambda _mat: _refresh_pipeline(panel),
                    edit_key=change_key,
                ))
        else:
            # Lightweight path: just remember that an undo commit is needed.
            if not state.extra.get("_undo_pending"):
                # First frame of this drag — save the starting snapshot.
                state.extra["_undo_old_document"] = copy.deepcopy(old_document)
                state.extra["_undo_edit_key"] = change_key
            state.extra["_undo_pending"] = True
    except (RuntimeError, ValueError) as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
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


def _flush_deferred_undo(panel, state, mat_data, native_mat):
    """Commit deferred undo snapshot when drag ends (changed=False frame)."""
    if not state.extra.get("_undo_pending"):
        return
    state.extra["_undo_pending"] = False

    old_document = state.extra.pop("_undo_old_document", None)
    edit_key = state.extra.pop("_undo_edit_key", "")
    if old_document is None:
        return

    try:
        from Infernux.engine.undo import UndoManager, MaterialDocumentCommand
        mgr = UndoManager.instance()
        new_json = json.dumps(mat_data)
        state.extra["cached_json"] = new_json

        # Pre-capture save snapshot so _save_material_resource doesn't need
        # to call native_mat.serialize() on the main thread.
        from Infernux.core.assets import AssetManager
        file_path = state.extra.get("_mat_file_path", "")
        if file_path:
            AssetManager.set_material_save_snapshot(file_path, new_json)

        if not (mgr and not mgr.is_executing and mgr.enabled):
            return

        new_document = copy.deepcopy(mat_data)
        if new_document != old_document:
            mgr.record(MaterialDocumentCommand(
                native_mat, old_document, new_document, "Edit Material",
                refresh_callback=lambda _mat: _refresh_pipeline(panel),
                edit_key=edit_key,
            ))
    except (RuntimeError, ValueError) as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")


# ═══════════════════════════════════════════════════════════════════════════
# Body renderer (called from asset_details_renderer)
# ═══════════════════════════════════════════════════════════════════════════


def _sync_shader_annotations(mat_data, state):
    """Synchronise shader property annotations into *mat_data*.

    Returns ``(changed, requires_deserialize)`` — True if new/removed
    properties were detected and pushed to the native material.
    """
    vert_shader_id = shader_utils.shader_ref_id(mat_data.get("shaders", {}).get("vertex", ""))
    frag_shader_id = shader_utils.shader_ref_id(mat_data.get("shaders", {}).get("fragment", ""))
    prop_gen = shader_utils.get_shader_property_generation()
    sync_key = f"{vert_shader_id}|{frag_shader_id}:{prop_gen}"
    last_sync_key = state.extra.get("shader_sync_key", "")
    last_validation_key = state.extra.get("shader_validation_key", "")
    mat_version = state.extra.get("mat_version", -1)
    last_validation_version = state.extra.get("shader_validation_mat_version", -2)
    needs_validation = (
        sync_key != last_validation_key
        or not mat_data.get("_shader_property_order")
        or (mat_version != -1 and mat_version != last_validation_version)
    )
    missing_shader_props = False
    if needs_validation and (vert_shader_id or frag_shader_id):
        current_props = mat_data.get("properties", {})
        expected_shader_props = shader_utils.get_all_shader_property_names(vert_shader_id, frag_shader_id)
        missing_shader_props = any(name not in current_props for name in expected_shader_props)
        state.extra["shader_validation_key"] = sync_key
        if mat_version != -1:
            state.extra["shader_validation_mat_version"] = mat_version

    changed = False
    requires_deserialize = False
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


def _render_material_top_native(ctx, panel, state, mat_data, section_readonly,
                                default_open_sections, is_embedded_slot):
    """Render the stable material top area in one native bridge call."""
    native_renderer = getattr(ctx, "render_material_top", None)
    if not callable(native_renderer):
        return None

    shaders = mat_data.setdefault("shaders", {})
    vert_ref = shaders.get("vertex", "")
    frag_ref = shaders.get("fragment", "")
    vert_items = shader_utils.get_shader_candidates(".vert", _shader_cache)
    frag_items = shader_utils.get_shader_candidates(".frag", _shader_cache)
    vert_display = shader_utils.shader_display_from_value(vert_ref, vert_items)
    frag_display = shader_utils.shader_display_from_value(frag_ref, frag_items)
    shader_lw = max_label_w(ctx, [t("material.vertex"), t("material.fragment")])

    rs = mat_data.setdefault("renderState", {})
    overrides = int(mat_data.get("renderStateOverrides", 0))
    surface_labels = [t("material.surface_type"), t("material.cull_mode"),
                      t("material.depth_write"), t("material.depth_test"),
                      t("material.blend_mode"), t("material.alpha_clip"),
                      t("material.render_queue")]
    surface_lw = max_label_w(ctx, surface_labels)
    entries, surface_plan, surface_values, depth_enabled, depth_op = (
        _prepare_surface_options_batch(ctx, rs, surface_lw)
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
        t("material.shader_section"), t("material.vertex"), vert_display,
        t("material.fragment"), frag_display, shader_lw,
        t("material.surface_options"), surface_plan, surface_values, surface_lw,
        int(EditorIcons.get_cached(Theme.ICON_IMG_PICKER) or 0),
        int(preview_tex_id or 0), "Material preview unavailable.",
        bool(default_open_sections), bool(section_readonly),
    )
    (vert_flags, vert_picker_open, vert_payload, vert_list_open,
     frag_flags, frag_picker_open, frag_payload, frag_list_open,
     surface_changes) = interaction

    changed = False
    requires_deserialize = False
    requires_pipeline_refresh = False
    change_key = ""

    def apply_shader(shader_key, new_value, other_key):
        nonlocal changed, requires_deserialize, requires_pipeline_refresh, change_key
        old_val = shaders.get(shader_key, "")
        ext = ".vert" if shader_key == "vertex" else ".frag"
        new_ref = shader_utils.make_shader_reference(new_value, ext)
        if not new_ref["guid"] and not new_ref["shader_id"]:
            return
        shaders[shader_key] = new_ref
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

    if vert_payload:
        apply_shader("vertex", str(vert_payload), "fragment")
    if frag_payload:
        apply_shader("fragment", str(frag_payload), "vertex")

    from . import igui as _igui_module
    from .igui import IGUI
    from .inspector_components import _picker_assets

    def render_shader_popups(field_id, flags, picker_open, list_open,
                             popup_id, items, selected_id, apply):
        if flags:
            _igui_module._popup_needs_focus.add(field_id)
            _igui_module._picker_filters.pop(f"_igui_filter_{field_id}", None)
        if picker_open or flags:
            ctx.push_id_str(field_id)
            try:
                IGUI._render_object_picker_popup(
                    ctx, field_id, None,
                    lambda filt: _picker_assets(filt, "*.vert" if field_id == "mat_vert" else "*.frag"),
                    apply, None,
                )
            finally:
                ctx.pop_id()
        if flags & 1:
            ctx.open_popup(popup_id)
        if list_open or (flags & 1):
            if ctx.begin_popup(popup_id):
                for display, value in items:
                    if ctx.selectable(display, value == selected_id):
                        apply(value)
                ctx.end_popup()

    render_shader_popups(
        "mat_vert", int(vert_flags), bool(vert_picker_open), bool(vert_list_open),
        "mat_vert_popup", vert_items, shader_utils.shader_ref_id(vert_ref),
        lambda value: apply_shader("vertex", value, "fragment"),
    )
    render_shader_popups(
        "mat_frag", int(frag_flags), bool(frag_picker_open), bool(frag_list_open),
        "mat_frag_popup", frag_items, shader_utils.shader_ref_id(frag_ref),
        lambda value: apply_shader("fragment", value, "vertex"),
    )

    overrides, surface_change_key = _apply_surface_option_changes(
        surface_changes, entries, rs, mat_data, overrides, depth_enabled, depth_op)
    if surface_change_key:
        changed = True
        requires_deserialize = True
        requires_pipeline_refresh = True
        change_key = surface_change_key

    return changed, requires_deserialize, requires_pipeline_refresh, change_key


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

    s_ch, s_ds, s_pr, s_ck = _render_shader_section(
        ctx, mat_data, state, section_readonly, default_open_sections)
    changed |= s_ch
    requires_deserialize |= s_ds
    requires_pipeline_refresh |= s_pr
    if s_ck:
        change_key = s_ck

    ctx.separator()
    so_ch, so_ds, so_pr, so_ck = _render_surface_options_section(
        ctx, mat_data, section_readonly, default_open_sections)
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
    return changed, requires_deserialize, requires_pipeline_refresh, change_key


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
    sync_ch, sync_ds = _sync_shader_annotations(mat_data, state)
    changed |= sync_ch
    requires_deserialize |= sync_ds

    # ── Top row: Shader + Surface + live preview ───────────────────────
    top_result = _render_material_top_native(
        ctx, panel, state, mat_data, section_readonly,
        default_open_sections, is_embedded_slot)
    if top_result is None:
        top_result = _render_material_top_legacy(
            ctx, panel, state, mat_data, section_readonly,
            default_open_sections, is_embedded_slot)
    top_changed, top_deserialize, top_pipeline_refresh, top_change_key = top_result
    changed |= top_changed
    requires_deserialize |= top_deserialize
    requires_pipeline_refresh |= top_pipeline_refresh
    if top_change_key:
        change_key = top_change_key

    # ── Properties ─────────────────────────────────────────────────────
    p_ch, p_ck, p_ds = _render_virtualized_material_block(
        ctx, state, "properties",
        lambda: _render_properties_section(
            ctx, mat_data, section_readonly, default_open_sections,
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
        old_document = _document_before_material_edit(state, mat_data)
        _apply_material_changes(panel, state, mat_data, _native_mat,
                                requires_deserialize, requires_pipeline_refresh,
                                old_document, change_key, exec_layer)
        # Mark the native version as "ours" so _refresh_material (called once
        # per frame by asset_details_renderer) can skip the expensive
        # serialize -> json.loads -> merge -> json.dumps round-trip.
        try:
            state.extra["_applied_version"] = _native_mat.get_version()
        except (AttributeError, RuntimeError):
            pass
    else:
        # Drag ended (or no edit this frame) — commit deferred undo snapshot.
        _flush_deferred_undo(panel, state, mat_data, _native_mat)

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
    if _inline_layer is not None:
        _inline_layer.flush_rw_autosave()


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
                      drag_type: str, on_drop,
                      picker_asset_items=None, on_pick=None, on_clear=None,
                      semantic_id: str = "") -> bool:
    """Simplified object-field renderer accepting drag-drop."""
    from . import inspector_components as comp_ui
    return comp_ui.render_object_field(ctx, fid, display, type_hint,
                                       clickable=True,
                                       accept_drag_type=drag_type,
                                       on_drop_callback=on_drop,
                                       picker_asset_items=picker_asset_items,
                                       on_pick=on_pick,
                                       on_clear=on_clear,
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


def _build_inline_state(panel, native_mat):
    extra = _get_inline_material_extra(panel, native_mat)
    mat_path = getattr(native_mat, "file_path", "") or ""
    state = extra.get("_inline_state")
    if state is None:
        state = SimpleNamespace(
            extra=extra,
            exec_layer=_InlineMaterialExecLayer(panel),
            file_path=mat_path,
        )
        extra["_inline_state"] = state
    else:
        state.file_path = mat_path
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
    cache_hit = (extra is not None
                 and mat_version != -1
                 and (extra.get("mat_version", -1) == mat_version
                      or extra.get("_applied_version", -2) == mat_version))
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


def _refresh_pipeline(panel):
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
    if engine and _native_mat and hasattr(engine, 'refresh_material_pipeline'):
        engine.refresh_material_pipeline(_native_mat)


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


