"""Custom Inspector renderers for screen-space UI components."""

from __future__ import annotations

import math
import os
import copy

from Infernux.ui import UICanvas, UIText, UIImage, UIButton
from Infernux.ui.enums import TextResizeMode
from Infernux.ui.enums import RenderMode, TextAlignH, TextAlignV
from Infernux.engine.project_context import get_project_root
from Infernux.engine.path_utils import relative_path, resolved_path

from ._inspector_undo import (
    _record_property,
    _record_python_component_document_edit,
)
from .inspector_components import register_py_component_renderer
from Infernux.engine.i18n import t
from Infernux.engine.texture_task_bridge import texture_stamp, query_or_schedule_texture
from .inspector_utils import (
    field_label, max_label_w, render_compact_section_header,
    render_compact_section_title, _render_color_bar, render_inspector_checkbox,
    semantic_capture_enabled, inspector_component_semantic_id,
    record_inspector_component_item,
)
from .theme import Theme


def _igui():
    """Lazy import to avoid circular dependency with inspector_components."""
    from .igui import IGUI
    return IGUI


def _field_semantic_id(ctx, comp, field_name: str) -> str:
    if not semantic_capture_enabled(ctx):
        return ""
    return inspector_component_semantic_id(comp, field_name)


def _record_field(ctx, comp, field_name: str, kind: str, label: str, *, enabled: bool = True) -> str:
    return record_inspector_component_item(
        ctx, comp, field_name, kind, label, enabled=enabled,
    )


def _apply_if_changed(comp, field_name: str, current, new_value):
    if new_value == current:
        return
    _record_property(comp, field_name, current, new_value, f"Set {field_name}")


def _render_color_field(ctx, comp, field_name: str, label: str, lw: float,
                        imgui_id: str, *, default=None, allow_hdr: bool = True):
    """Render a color bar editor and apply changes via undo system.

    Consolidates the repeated get→pad→label→bar→compare→apply pattern.
    """
    if default is None:
        default = [1.0, 1.0, 1.0, 1.0]
    cur = list(getattr(comp, field_name, None) or default)
    while len(cur) < 4:
        cur.append(1.0)
    field_label(ctx, label, lw)
    nr, ng, nb, na = _render_color_bar(ctx, imgui_id, cur[0], cur[1], cur[2], cur[3],
                                       allow_hdr=allow_hdr)
    _record_field(ctx, comp, field_name, "color_field", label)
    new_color = [nr, ng, nb, na]
    if tuple(new_color) != tuple(cur[:4]):
        _apply_if_changed(comp, field_name, cur[:4], new_color)


def _render_texture_picker(ctx, comp, field_name: str, label: str, lw: float,
                           imgui_id: str):
    """Render a texture object-field picker and apply changes."""
    IGUI = _igui()
    tex_path = str(getattr(comp, field_name, "") or "")
    display = os.path.basename(tex_path) if tex_path else t("igui.none")

    def _assign(payload):
        new_path = _project_asset_reference_path("Texture", payload)
        if new_path != tex_path:
            _apply_if_changed(comp, field_name, tex_path, new_path)

    def _on_clear():
        if tex_path:
            _apply_if_changed(comp, field_name, tex_path, "")

    field_label(ctx, label, lw)
    IGUI.asset_reference_field(
        ctx, imgui_id, display, "Texture",
        asset_type="Texture", accept="TEXTURE_FILE",
        on_assign=_assign, on_clear=_on_clear,
        ping_path=tex_path or None,
        has_value=bool(tex_path),
        reference_value={"asset_type": "Texture", "path_hint": tex_path},
    )
    _record_field(ctx, comp, field_name, "object_field", label)


def _get_serializable_raw_field(obj, field_name: str, default=None):
    data = object.__getattribute__(obj, "__dict__")
    if field_name in data:
        return data[field_name]
    cls = object.__getattribute__(obj, "__class__")
    meta = getattr(cls, "_serialized_fields_", {}).get(field_name)
    if meta is not None:
        return meta.default
    return default


def _project_asset_reference_path(asset_type: str, payload) -> str:
    from Infernux.core.asset_reference_types import resolve_asset_reference_path

    path = resolve_asset_reference_path(asset_type, payload)
    root = get_project_root()
    if not root:
        return str(path).replace("\\", "/")
    absolute = path if os.path.isabs(path) else os.path.join(root, path)
    return relative_path(absolute, root)


def _find_canvas(comp):
    go = getattr(comp, "game_object", None)
    while go is not None:
        for py_comp in go.get_py_components():
            if isinstance(py_comp, UICanvas):
                return py_comp
        go = go.get_parent()
    return None


def _get_parent_rect(comp):
    """Return (canvas, parent_visual_rect) for alignment purposes."""
    from Infernux.ui import InxUIScreenComponent

    canvas = _find_canvas(comp)
    if canvas is None:
        return None, None

    cw = float(canvas.reference_width)
    ch = float(canvas.reference_height)
    parent_go = getattr(comp.game_object, "get_parent", lambda: None)()
    while parent_go is not None:
        for py_comp in parent_go.get_py_components():
            if isinstance(py_comp, InxUIScreenComponent):
                rect = py_comp.get_visual_rect(cw, ch)
                return canvas, rect
        for py_comp in parent_go.get_py_components():
            if isinstance(py_comp, UICanvas):
                return canvas, (0.0, 0.0, cw, ch)
        parent_go = parent_go.get_parent()

    return canvas, (0.0, 0.0, cw, ch)


def _canvas_dims(comp):
    """Return (canvas, cw, ch) or (None, 0, 0)."""
    canvas = _find_canvas(comp)
    if canvas is None:
        return None, 0.0, 0.0
    return canvas, float(canvas.reference_width), float(canvas.reference_height)


def _apply_visual_position(comp, vis_x, vis_y, canvas):
    """Move element so its visual AABB top-left is at (vis_x, vis_y), with undo."""
    cw = float(canvas.reference_width)
    ch = float(canvas.reference_height)
    _record_python_component_document_edit(
        comp,
        lambda: comp.set_visual_position(vis_x, vis_y, cw, ch),
        "Set Visual Position",
        edit_key="visual_position",
        validate=True,
    )


def _apply_size_with_undo(comp, width, height, canvas, resize_fn):
    """Apply one layout resize as an atomic component document edit."""
    cw = float(canvas.reference_width)
    ch = float(canvas.reference_height)
    _record_python_component_document_edit(
        comp,
        lambda: resize_fn(
            comp,
            float(width),
            float(height),
            cw,
            ch,
        ),
        "Set Layout Size",
        edit_key="layout_size",
        validate=True,
    )


def _apply_size_preserve_top_left(comp, width, height, canvas):
    """Update width/height while keeping the rotated top-left corner fixed."""
    _apply_size_with_undo(comp, width, height, canvas,
                          lambda c, w, h, cw, ch: c.set_size_preserve_corner(w, h, cw, ch, "top_left"))


def _set_native_size(comp):
    """Resize UIImage/UIButton to the native pixel dimensions of its texture."""
    tex_path = getattr(comp, "texture_path", "") or ""
    if not tex_path:
        return
    project_root = get_project_root()
    if not project_root:
        return
    abs_path = resolved_path(os.path.join(project_root, tex_path))
    if not os.path.isfile(abs_path):
        return

    try:
        from .editor_services import EditorServices
        svc = EditorServices.instance()
        native = svc.native_engine if svc else None
    except Exception:
        native = None
    if not native:
        return

    stamp = texture_stamp(abs_path, "ui_native_size")
    if stamp == 0:
        return

    _, tex_w, tex_h = query_or_schedule_texture(
        native,
        f"ui_native_size|{resolved_path(tex_path)}",
        abs_path,
        int(stamp),
        nearest=False,
        srgb=False,
        pump=True,
    )
    if tex_w <= 0 or tex_h <= 0:
        return

    canvas, _, _ = _canvas_dims(comp)
    w, h = float(tex_w), float(tex_h)
    if canvas is not None:
        _apply_size_preserve_top_left(comp, w, h, canvas)
    else:
        _apply_if_changed(comp, "width", comp.width, w)
        _apply_if_changed(comp, "height", comp.height, h)


def _align_component(comp, axis: str, mode: str):
    """Align the visual bounding box to the parent rect edge/center."""
    canvas, parent_rect = _get_parent_rect(comp)
    if canvas is None or parent_rect is None:
        return

    cw = float(canvas.reference_width)
    ch = float(canvas.reference_height)
    vis = comp.get_visual_rect(cw, ch)
    if vis is None:
        return

    vis_x, vis_y, vis_w, vis_h = vis
    parent_x, parent_y, parent_w, parent_h = parent_rect

    if axis == "x":
        if mode == "left":
            vis_x = parent_x
        elif mode == "center":
            vis_x = parent_x + (parent_w - vis_w) * 0.5
        elif mode == "right":
            vis_x = parent_x + parent_w - vis_w
    elif axis == "y":
        if mode == "top":
            vis_y = parent_y
        elif mode == "middle":
            vis_y = parent_y + (parent_h - vis_h) * 0.5
        elif mode == "bottom":
            vis_y = parent_y + parent_h - vis_h

    _apply_visual_position(comp, vis_x, vis_y, canvas)


def _render_common_position(ctx, comp):
    if not render_compact_section_header(ctx, t("ui_comp.location"), level="primary"):
        return

    section_lw = max_label_w(ctx, [t("ui_comp.alignment"), t("ui_comp.position"), t("ui_comp.rotation")])

    # ── Alignment ──
    render_compact_section_title(ctx, t("ui_comp.alignment"), level="secondary")
    field_label(ctx, t("ui_comp.alignment"), section_lw)
    clicked = Theme.render_inline_button_row(
        ctx,
        "ui_align_row",
        [
            ("left", t("ui_comp.align_left")),
            ("center_x", t("ui_comp.align_cx")),
            ("right", t("ui_comp.align_right")),
            ("top", t("ui_comp.align_top")),
            ("middle", t("ui_comp.align_mid")),
            ("bottom", t("ui_comp.align_bot")),
        ],
        semantic_base=_field_semantic_id(ctx, comp, "alignment"),
    )
    if clicked == "left":
        _align_component(comp, "x", "left")
    elif clicked == "center_x":
        _align_component(comp, "x", "center")
    elif clicked == "right":
        _align_component(comp, "x", "right")
    elif clicked == "top":
        _align_component(comp, "y", "top")
    elif clicked == "middle":
        _align_component(comp, "y", "middle")
    elif clicked == "bottom":
        _align_component(comp, "y", "bottom")

    # ── Position (shows visual AABB position) ──
    render_compact_section_title(ctx, t("ui_comp.position"), level="secondary")
    canvas, cw, ch = _canvas_dims(comp)
    if canvas is None:
        ctx.label(t("ui_comp.no_canvas_context"))
    else:
        vis = comp.get_visual_rect(cw, ch)
        if vis is None:
            return
        vis_x, vis_y, vis_w, vis_h = vis
        new_x, new_y = ctx.vector2(
            "Position",
            float(vis_x),
            float(vis_y),
            1.0,
            section_lw,
            semantic_id=_field_semantic_id(ctx, comp, "position"),
        )
        _record_field(ctx, comp, "position", "vector", t("ui_comp.position"))
        if float(new_x) != float(vis_x) or float(new_y) != float(vis_y):
            _apply_visual_position(comp, float(new_x), float(new_y), canvas)

    # ── Rotation ──
    render_compact_section_title(ctx, t("ui_comp.rotation"), level="secondary")
    field_label(ctx, t("ui_comp.rotation"), section_lw)
    new_rot = ctx.drag_float("##ui_rotation", float(comp.rotation), 1.0, -3600.0, 3600.0)
    _record_field(ctx, comp, "rotation", "drag_float", t("ui_comp.rotation"))
    if float(new_rot) != float(comp.rotation):
        _apply_if_changed(comp, "rotation", comp.rotation, float(new_rot))

    clicked = Theme.render_inline_button_row(
        ctx,
        "ui_rotation_row",
        [
            ("rotate_90", t("ui_comp.rotate_90")),
            ("mirror_x", t("ui_comp.mirror_h")),
            ("mirror_y", t("ui_comp.mirror_v")),
        ],
        active_items=[
            item_id for item_id, enabled in (
                ("mirror_x", bool(getattr(comp, "mirror_x", False))),
                ("mirror_y", bool(getattr(comp, "mirror_y", False))),
            ) if enabled
        ],
        semantic_base=_field_semantic_id(ctx, comp, "rotation_actions"),
    )
    if clicked == "rotate_90":
        new_rot = float(comp.rotation) + 90.0
        _apply_if_changed(comp, "rotation", comp.rotation, new_rot)
    elif clicked == "mirror_x":
        _apply_if_changed(comp, "mirror_x", comp.mirror_x, not bool(comp.mirror_x))
    elif clicked == "mirror_y":
        _apply_if_changed(comp, "mirror_y", comp.mirror_y, not bool(comp.mirror_y))


def _sync_text_layout_from_ctx(ctx, text_comp: UIText):
    text = getattr(text_comp, "text", "")
    font_size = max(1.0, float(getattr(text_comp, "font_size", Theme.UI_DEFAULT_FONT_SIZE)))
    wrap_width = float(text_comp.get_editor_wrap_width()) if hasattr(text_comp, "get_editor_wrap_width") else float(text_comp.get_wrap_width())
    font_path = str(getattr(text_comp, "font_path", "") or "")
    line_height = float(getattr(text_comp, "line_height", 1.2))
    letter_spacing = float(getattr(text_comp, "letter_spacing", 0.0))
    pad_x, pad_y = getattr(text_comp, "get_auto_size_padding", lambda: (0.0, 0.0))()
    canvas = _find_canvas(text_comp)
    if wrap_width > 0.0:
        measured_w, measured_h = ctx.calc_text_size_wrapped(
            text, font_size, wrap_width, font_path, line_height, letter_spacing
        )
    else:
        measured_w, measured_h = ctx.calc_text_size(text, font_size, font_path, line_height, letter_spacing)

    if canvas is None:
        if text_comp.is_auto_width():
            _apply_if_changed(text_comp, "width", text_comp.width, max(1.0, float(measured_w) + float(pad_x)))
        elif text_comp.is_auto_height():
            _apply_if_changed(text_comp, "height", text_comp.height, max(1.0, float(measured_h) + float(pad_y)))
        return

    if text_comp.is_auto_width():
        _apply_size_preserve_top_left(text_comp, max(1.0, float(measured_w) + float(pad_x)), float(text_comp.height), canvas)
    elif text_comp.is_auto_height():
        _apply_size_preserve_top_left(text_comp, float(text_comp.width), max(1.0, float(measured_h) + float(pad_y)), canvas)


def _set_text_resize_mode(ctx, text_comp: UIText, mode: TextResizeMode):
    if text_comp.resize_mode == mode:
        return
    _apply_if_changed(text_comp, "resize_mode", text_comp.resize_mode, mode)
    if bool(text_comp.lock_aspect_ratio):
        _apply_if_changed(text_comp, "lock_aspect_ratio", text_comp.lock_aspect_ratio, False)
    _sync_text_layout_from_ctx(ctx, text_comp)


def _apply_layout_size_changes(ctx, comp, size_x, size_y, section_lw):
    """Apply width/height edits from the layout Size field, honouring lock-aspect and canvas."""
    width_changed = float(size_x) != float(comp.width)
    height_changed = float(size_y) != float(comp.height)
    width_editable = not (isinstance(comp, UIText) and not comp.is_width_editable())
    height_editable = not (isinstance(comp, UIText) and not comp.is_height_editable())
    canvas, _, _ = _canvas_dims(comp)

    if width_changed and width_editable:
        old_w = max(float(comp.width), 1e-6)
        target_w = max(1.0, float(size_x))
        if bool(getattr(comp, "lock_aspect_ratio", False)):
            aspect = old_w / max(float(comp.height), 1e-6)
            target_h = max(1.0, float(size_x) / max(aspect, 1e-6))
            if canvas is None:
                _apply_if_changed(comp, "width", comp.width, target_w)
                _apply_if_changed(comp, "height", comp.height, target_h)
            else:
                _apply_size_preserve_top_left(comp, target_w, target_h, canvas)
        elif canvas is None:
            _apply_if_changed(comp, "width", comp.width, target_w)
        elif isinstance(comp, UIText):
            _apply_size_preserve_top_left(comp, target_w, float(comp.height), canvas)
            _sync_text_layout_from_ctx(ctx, comp)
        else:
            _apply_size_preserve_top_left(comp, target_w, float(comp.height), canvas)

    if height_changed and height_editable:
        old_h = max(float(comp.height), 1e-6)
        target_h = max(1.0, float(size_y))
        if bool(getattr(comp, "lock_aspect_ratio", False)):
            aspect = float(comp.width) / max(old_h, 1e-6)
            target_w = max(1.0, float(size_y) * aspect)
            if canvas is None:
                _apply_if_changed(comp, "width", comp.width, target_w)
                _apply_if_changed(comp, "height", comp.height, target_h)
            else:
                _apply_size_preserve_top_left(comp, target_w, target_h, canvas)
        elif canvas is None:
            _apply_if_changed(comp, "height", comp.height, target_h)
        elif isinstance(comp, UIText):
            _apply_size_preserve_top_left(comp, float(comp.width), target_h, canvas)
            _sync_text_layout_from_ctx(ctx, comp)
        else:
            _apply_size_preserve_top_left(comp, float(comp.width), target_h, canvas)


def _render_common_layout(ctx, comp):
    if not render_compact_section_header(ctx, t("ui_comp.layout"), level="primary"):
        return

    labels = [t("ui_comp.dimensions"), t("ui_comp.size"), t("ui_comp.modify")]
    if isinstance(comp, UIText):
        labels.append(t("ui_comp.resizing"))
    section_lw = max_label_w(ctx, labels)

    if isinstance(comp, UIText):
        _sync_text_layout_from_ctx(ctx, comp)

    render_compact_section_title(ctx, t("ui_comp.dimensions"), level="secondary")
    field_label(ctx, t("ui_comp.modify"), section_lw)

    # Build button list: Lock is always present;
    # Set Native Size appears for UIImage / UIButton with texture_path.
    modify_buttons = [("lock", t("ui_comp.lock"))]
    has_texture = bool(getattr(comp, "texture_path", "") or "")
    if has_texture:
        modify_buttons.append(("native_size", t("ui_comp.set_native_size")))

    clicked = Theme.render_inline_button_row(
        ctx,
        "ui_layout_lock_row",
        modify_buttons,
        active_items=["lock"] if bool(getattr(comp, "lock_aspect_ratio", False)) else [],
        semantic_base=_field_semantic_id(ctx, comp, "layout_actions"),
    )
    if clicked == "lock":
        new_lock = not bool(getattr(comp, "lock_aspect_ratio", False))
        if new_lock and isinstance(comp, UIText) and comp.resize_mode != TextResizeMode.FixedSize:
            _apply_if_changed(comp, "resize_mode", comp.resize_mode, TextResizeMode.FixedSize)
        _apply_if_changed(comp, "lock_aspect_ratio", comp.lock_aspect_ratio, new_lock)
    elif clicked == "native_size" and has_texture:
        _set_native_size(comp)

    field_label(ctx, t("ui_comp.size"), section_lw)
    size_x, size_y = ctx.vector2(
        "Size",
        float(comp.width),
        float(comp.height),
        1.0,
        section_lw,
        semantic_id=_field_semantic_id(ctx, comp, "size"),
    )
    _record_field(ctx, comp, "size", "vector", t("ui_comp.size"))
    _apply_layout_size_changes(ctx, comp, size_x, size_y, section_lw)

    if isinstance(comp, UIText):
        render_compact_section_title(ctx, t("ui_comp.resizing"), level="secondary")
        field_label(ctx, t("ui_comp.resizing"), section_lw)
        active = {
            TextResizeMode.AutoWidth: "auto_width",
            TextResizeMode.AutoHeight: "auto_height",
            TextResizeMode.FixedSize: "fixed_size",
        }.get(comp.resize_mode, "fixed_size")
        clicked = Theme.render_inline_button_row(
            ctx,
            "ui_text_resizing_row",
            [
                ("auto_width", t("ui_comp.auto_width")),
                ("auto_height", t("ui_comp.auto_height")),
                ("fixed_size", t("ui_comp.fixed_size")),
            ],
            active_items=[active],
            semantic_base=_field_semantic_id(ctx, comp, "resize_mode"),
        )
        if clicked == "auto_width":
            _set_text_resize_mode(ctx, comp, TextResizeMode.AutoWidth)
        elif clicked == "auto_height":
            _set_text_resize_mode(ctx, comp, TextResizeMode.AutoHeight)
        elif clicked == "fixed_size":
            _set_text_resize_mode(ctx, comp, TextResizeMode.FixedSize)


def _render_common_appearance(ctx, comp):
    if not render_compact_section_header(ctx, t("ui_comp.appearance"), level="primary"):
        return

    section_lw = max_label_w(ctx, [t("ui_comp.opacity"), t("ui_comp.corner_radius")])

    field_label(ctx, t("ui_comp.opacity"), section_lw)
    opacity_pct = max(0.0, min(100.0, float(getattr(comp, "opacity", 1.0)) * 100.0))
    new_opacity_pct = ctx.drag_float("##ui_opacity_pct", opacity_pct, 1.0, 0.0, 100.0)
    _record_field(ctx, comp, "opacity", "drag_float", t("ui_comp.opacity"))
    new_opacity = max(0.0, min(1.0, float(new_opacity_pct) / 100.0))
    if not math.isclose(new_opacity, float(getattr(comp, "opacity", 1.0)), rel_tol=1e-5, abs_tol=1e-6):
        _apply_if_changed(comp, "opacity", comp.opacity, new_opacity)

    field_label(ctx, t("ui_comp.corner_radius"), section_lw)
    is_text = isinstance(comp, UIText)
    if is_text and abs(float(getattr(comp, "corner_radius", 0.0))) > 1e-6:
        _apply_if_changed(comp, "corner_radius", comp.corner_radius, 0.0)

    if is_text:
        ctx.begin_disabled(True)
        ctx.drag_float("##ui_corner_radius_disabled", 0.0, 1.0, 0.0, 1000.0)
        _record_field(ctx, comp, "corner_radius", "drag_float", t("ui_comp.corner_radius"), enabled=False)
        ctx.end_disabled()
    else:
        new_radius = ctx.drag_float(
            "##ui_corner_radius",
            max(0.0, float(getattr(comp, "corner_radius", 0.0))),
            1.0,
            0.0,
            1000.0,
        )
        _record_field(ctx, comp, "corner_radius", "drag_float", t("ui_comp.corner_radius"))
        target_radius = max(0.0, float(new_radius))
        if not math.isclose(target_radius, float(getattr(comp, "corner_radius", 0.0)), rel_tol=1e-5, abs_tol=1e-6):
            _apply_if_changed(comp, "corner_radius", comp.corner_radius, target_radius)


def _render_font_picker(ctx, comp, field_name: str, lw: float, imgui_id: str):
    """Render the shared Font resource field and apply one Undo command."""
    IGUI = _igui()
    font_path = str(getattr(comp, field_name, "") or "")
    changed = False

    def _assign(payload):
        nonlocal changed
        new_path = _project_asset_reference_path("Font", payload)
        if new_path != font_path:
            _apply_if_changed(comp, field_name, font_path, new_path)
            changed = True

    def _clear():
        nonlocal changed
        if font_path:
            _apply_if_changed(comp, field_name, font_path, "")
            changed = True

    IGUI.asset_reference_field(
        ctx,
        imgui_id,
        os.path.basename(font_path) if font_path else t("ui_comp.default_font"),
        "Font",
        asset_type="Font",
        on_assign=_assign,
        on_clear=_clear,
        ping_path=font_path or None,
        has_value=bool(font_path),
        reference_value={"asset_type": "Font", "path_hint": font_path},
    )
    _record_field(ctx, comp, field_name, "object_field", t("ui_comp.font"))
    return changed


def _render_text_alignment_row(ctx, comp, lw: float, imgui_id: str,
                                default_h=None, default_v=None):
    """Render the horizontal+vertical text alignment button row and apply changes."""
    if default_h is None:
        default_h = TextAlignH.Left
    if default_v is None:
        default_v = TextAlignV.Top
    field_label(ctx, t("ui_comp.text_alignment"), lw)
    horiz = getattr(comp, "text_align_h", default_h)
    vert = getattr(comp, "text_align_v", default_v)
    h_map = {
        int(TextAlignH.Left): "text_left",
        int(TextAlignH.Center): "text_center_h",
        int(TextAlignH.Right): "text_right",
    }
    v_map = {
        int(TextAlignV.Top): "text_top",
        int(TextAlignV.Center): "text_center_v",
        int(TextAlignV.Bottom): "text_bottom",
    }
    active_items = [
        h_map.get(int(horiz), "text_left"),
        v_map.get(int(vert), "text_top"),
    ]
    clicked = Theme.render_inline_button_row(
        ctx,
        imgui_id,
        [
            ("text_left", t("ui_comp.text_left")),
            ("text_center_h", t("ui_comp.text_center")),
            ("text_right", t("ui_comp.text_right")),
            ("text_top", t("ui_comp.text_top")),
            ("text_center_v", t("ui_comp.text_middle")),
            ("text_bottom", t("ui_comp.text_bottom")),
        ],
        active_items=active_items,
        semantic_base=_field_semantic_id(ctx, comp, "text_alignment"),
    )
    if clicked == "text_left":
        _apply_if_changed(comp, "text_align_h", comp.text_align_h, TextAlignH.Left)
    elif clicked == "text_center_h":
        _apply_if_changed(comp, "text_align_h", comp.text_align_h, TextAlignH.Center)
    elif clicked == "text_right":
        _apply_if_changed(comp, "text_align_h", comp.text_align_h, TextAlignH.Right)
    elif clicked == "text_top":
        _apply_if_changed(comp, "text_align_v", comp.text_align_v, TextAlignV.Top)
    elif clicked == "text_center_v":
        _apply_if_changed(comp, "text_align_v", comp.text_align_v, TextAlignV.Center)
    elif clicked == "text_bottom":
        _apply_if_changed(comp, "text_align_v", comp.text_align_v, TextAlignV.Bottom)


def _render_text_typography(ctx, text_comp: UIText):
    if not render_compact_section_header(ctx, t("ui_comp.typography"), level="primary"):
        return

    section_lw = max_label_w(ctx, [t("ui_comp.content"), t("ui_comp.font"), t("ui_comp.font_size"), t("ui_comp.line_height"), t("ui_comp.letter_spacing"), t("ui_comp.text_alignment")])

    field_label(ctx, t("ui_comp.content"), section_lw)
    current_text = str(getattr(text_comp, "text", "") or "")
    new_text = ctx.input_text_multiline("##ui_text_content", current_text, 16384, -1.0, 96.0, 0)
    _record_field(ctx, text_comp, "text", "text_area", t("ui_comp.content"))
    if new_text != current_text:
        _apply_if_changed(text_comp, "text", current_text, new_text)
        _sync_text_layout_from_ctx(ctx, text_comp)

    field_label(ctx, t("ui_comp.font"), section_lw)
    if _render_font_picker(ctx, text_comp, "font_path", section_lw, "ui_text_font_path"):
        _sync_text_layout_from_ctx(ctx, text_comp)

    field_label(ctx, t("ui_comp.font_size"), section_lw)
    new_font_size = ctx.drag_float("##ui_text_font_size", text_comp.font_size, 0.5, 4.0, 1000000.0)
    _record_field(ctx, text_comp, "font_size", "drag_float", t("ui_comp.font_size"))
    target_font_size = max(4.0, min(1000000.0, float(new_font_size)))
    if not math.isclose(target_font_size, float(text_comp.font_size), rel_tol=1e-5, abs_tol=1e-6):
        _apply_if_changed(text_comp, "font_size", text_comp.font_size, target_font_size)
        _sync_text_layout_from_ctx(ctx, text_comp)

    field_label(ctx, t("ui_comp.line_height"), section_lw)
    new_line_height = ctx.drag_float("##ui_text_line_height", text_comp.line_height, 0.01, 0.5, 5.0)
    _record_field(ctx, text_comp, "line_height", "drag_float", t("ui_comp.line_height"))
    target_line_height = max(0.5, min(5.0, float(new_line_height)))
    if not math.isclose(target_line_height, float(text_comp.line_height), rel_tol=1e-5, abs_tol=1e-6):
        _apply_if_changed(text_comp, "line_height", text_comp.line_height, target_line_height)
        _sync_text_layout_from_ctx(ctx, text_comp)

    field_label(ctx, t("ui_comp.letter_spacing"), section_lw)
    new_letter_spacing = ctx.drag_float("##ui_text_letter_spacing", text_comp.letter_spacing, 0.1, -20.0, 100.0)
    _record_field(ctx, text_comp, "letter_spacing", "drag_float", t("ui_comp.letter_spacing"))
    target_letter_spacing = max(-20.0, min(100.0, float(new_letter_spacing)))
    if not math.isclose(target_letter_spacing, float(text_comp.letter_spacing), rel_tol=1e-5, abs_tol=1e-6):
        _apply_if_changed(text_comp, "letter_spacing", text_comp.letter_spacing, target_letter_spacing)
        _sync_text_layout_from_ctx(ctx, text_comp)

    if render_compact_section_header(ctx, t("ui_comp.text_alignment"), level="secondary"):
        _render_text_alignment_row(ctx, text_comp, section_lw, "ui_text_alignment_row")


def _render_text_fill(ctx, text_comp: UIText):
    if not render_compact_section_header(ctx, t("ui_comp.fill"), level="primary"):
        return
    section_lw = max_label_w(ctx, [t("ui_comp.color")])
    _render_color_field(ctx, text_comp, "color", t("ui_comp.color"), section_lw,
                        "##ui_text_fill_color")


def _render_canvas_inspector(ctx, canvas: UICanvas):
    from Infernux.ui.enums import UIScaleMode, ScreenMatchMode

    lw = max_label_w(ctx, [t("ui_comp.render_mode"), t("ui_comp.sort_order"), t("ui_comp.target_camera"), t("ui_comp.reference_size"),
                           t("ui_comp.ui_scale_mode"), t("ui_comp.screen_match_mode"), t("ui_comp.match"),
                           t("ui_comp.pixel_perfect"), t("ui_comp.ref_pixels_per_unit")])
    if render_compact_section_header(ctx, t("ui_comp.canvas"), level="secondary"):
        members = list(RenderMode)
        labels = [member.name for member in members]
        try:
            current_idx = members.index(canvas.render_mode)
        except ValueError:
            current_idx = 0
        field_label(ctx, t("ui_comp.render_mode"), lw)
        new_idx = ctx.combo("##canvas_render_mode", current_idx, labels, -1)
        _record_field(ctx, canvas, "render_mode", "combo", t("ui_comp.render_mode"))
        new_render_mode = members[new_idx]
        _apply_if_changed(canvas, "render_mode", canvas.render_mode, new_render_mode)

        field_label(ctx, t("ui_comp.sort_order"), lw)
        new_sort_order = int(ctx.drag_int("##canvas_sort_order", int(canvas.sort_order), 1.0, -1000, 1000))
        _record_field(ctx, canvas, "sort_order", "drag_int", t("ui_comp.sort_order"))
        _apply_if_changed(canvas, "sort_order", canvas.sort_order, new_sort_order)

        if canvas.render_mode == RenderMode.CameraOverlay:
            field_label(ctx, t("ui_comp.target_camera"), lw)
            new_target_camera = int(ctx.drag_int("##canvas_target_camera", int(canvas.target_camera_id), 1.0, -1, 1000000))
            _record_field(ctx, canvas, "target_camera_id", "drag_int", t("ui_comp.target_camera"))
            _apply_if_changed(canvas, "target_camera_id", canvas.target_camera_id, new_target_camera)

        new_ref_w, new_ref_h = ctx.vector2(
            "Reference Size",
            float(canvas.reference_width),
            float(canvas.reference_height),
            1.0,
            lw,
            semantic_id=_field_semantic_id(ctx, canvas, "reference_size"),
        )
        _record_field(ctx, canvas, "reference_size", "vector", t("ui_comp.reference_size"))
        _apply_if_changed(canvas, "reference_width", canvas.reference_width, max(1, int(round(new_ref_w))))
        _apply_if_changed(canvas, "reference_height", canvas.reference_height, max(1, int(round(new_ref_h))))

    # ── Canvas Scaler (Unity-aligned) ──
    if render_compact_section_header(ctx, t("ui_comp.canvas_scaler"), level="secondary"):
        scale_members = list(UIScaleMode)
        scale_labels = [m.name for m in scale_members]
        try:
            scale_idx = scale_members.index(canvas.ui_scale_mode)
        except ValueError:
            scale_idx = 1  # ScaleWithScreenSize default
        field_label(ctx, t("ui_comp.ui_scale_mode"), lw)
        new_scale_idx = ctx.combo("##canvas_scale_mode", scale_idx, scale_labels, -1)
        _record_field(ctx, canvas, "ui_scale_mode", "combo", t("ui_comp.ui_scale_mode"))
        new_scale_mode = scale_members[new_scale_idx]
        _apply_if_changed(canvas, "ui_scale_mode", canvas.ui_scale_mode, new_scale_mode)

        if canvas.ui_scale_mode == UIScaleMode.ScaleWithScreenSize:
            match_members = list(ScreenMatchMode)
            match_labels = [m.name for m in match_members]
            try:
                match_idx = match_members.index(canvas.screen_match_mode)
            except ValueError:
                match_idx = 0
            field_label(ctx, t("ui_comp.screen_match_mode"), lw)
            new_match_idx = ctx.combo("##canvas_match_mode", match_idx, match_labels, -1)
            _record_field(ctx, canvas, "screen_match_mode", "combo", t("ui_comp.screen_match_mode"))
            new_match_mode = match_members[new_match_idx]
            _apply_if_changed(canvas, "screen_match_mode", canvas.screen_match_mode, new_match_mode)

            if canvas.screen_match_mode == ScreenMatchMode.MatchWidthOrHeight:
                field_label(ctx, t("ui_comp.match"), lw)
                new_match = ctx.float_slider("##canvas_match_val", float(canvas.match_width_or_height), 0.0, 1.0)
                _record_field(ctx, canvas, "match_width_or_height", "float_slider", t("ui_comp.match"))
                if abs(float(new_match) - float(canvas.match_width_or_height)) > 1e-5:
                    _apply_if_changed(canvas, "match_width_or_height", canvas.match_width_or_height, float(new_match))

        new_pp = render_inspector_checkbox(ctx, t("ui_comp.pixel_perfect"), bool(canvas.pixel_perfect))
        _record_field(ctx, canvas, "pixel_perfect", "checkbox", t("ui_comp.pixel_perfect"))
        _apply_if_changed(canvas, "pixel_perfect", canvas.pixel_perfect, bool(new_pp))

        field_label(ctx, t("ui_comp.ref_pixels_per_unit"), lw)
        new_rppu = ctx.drag_float("##canvas_rppu", float(canvas.reference_pixels_per_unit), 1.0, 1.0, 10000.0)
        _record_field(ctx, canvas, "reference_pixels_per_unit", "drag_float", t("ui_comp.ref_pixels_per_unit"))
        if abs(float(new_rppu) - float(canvas.reference_pixels_per_unit)) > 0.01:
            _apply_if_changed(canvas, "reference_pixels_per_unit", canvas.reference_pixels_per_unit, max(1.0, float(new_rppu)))


def _render_text_inspector(ctx, text_comp: UIText):
    _render_common_position(ctx, text_comp)
    _render_common_layout(ctx, text_comp)
    _render_common_appearance(ctx, text_comp)
    _render_text_typography(ctx, text_comp)
    _render_text_fill(ctx, text_comp)


# ========================================================================
#  UIImage inspector
# ========================================================================

def _render_image_fill(ctx, img_comp: UIImage):
    if not render_compact_section_header(ctx, t("ui_comp.fill"), level="primary"):
        return

    section_lw = max_label_w(ctx, [t("ui_comp.texture"), t("ui_comp.color")])

    _render_texture_picker(ctx, img_comp, "texture_path", t("ui_comp.texture"),
                           section_lw, "ui_image_texture")

    # ── Tint color ──
    _render_color_field(ctx, img_comp, "color", t("ui_comp.color"), section_lw,
                        "##ui_image_fill_color")


def _render_image_inspector(ctx, img_comp: UIImage):
    _render_common_position(ctx, img_comp)
    _render_common_layout(ctx, img_comp)
    _render_common_appearance(ctx, img_comp)
    _render_image_fill(ctx, img_comp)


register_py_component_renderer("UICanvas", _render_canvas_inspector)
register_py_component_renderer("UIText", _render_text_inspector)
register_py_component_renderer("UIImage", _render_image_inspector)


# ── UIButton inspector ──

def _render_button_inspector(ctx, btn_comp: UIButton):
    _render_common_position(ctx, btn_comp)
    _render_common_layout(ctx, btn_comp)
    _render_common_appearance(ctx, btn_comp)

    # ── Interaction ──
    if render_compact_section_header(ctx, t("ui_comp.interaction"), level="primary"):
        new_inter = render_inspector_checkbox(ctx, t("ui_comp.interactable"), btn_comp.interactable)
        _record_field(ctx, btn_comp, "interactable", "checkbox", t("ui_comp.interactable"))
        _apply_if_changed(btn_comp, "interactable", btn_comp.interactable, new_inter)

        new_rc = render_inspector_checkbox(ctx, t("ui_comp.raycast_target"), btn_comp.raycast_target)
        _record_field(ctx, btn_comp, "raycast_target", "checkbox", t("ui_comp.raycast_target"))
        _apply_if_changed(btn_comp, "raycast_target", btn_comp.raycast_target, new_rc)

    # ── Content ──
    if render_compact_section_header(ctx, t("ui_comp.content"), level="primary"):
        lw = max_label_w(ctx, [t("ui_comp.label"), t("ui_comp.font"), t("ui_comp.font_size"), t("ui_comp.label_color"),
                                t("ui_comp.line_height"), t("ui_comp.letter_spacing")])
        field_label(ctx, t("ui_comp.label"), lw)
        new_label = ctx.text_input("##btn_label", str(btn_comp.label or ""), 256)
        _record_field(ctx, btn_comp, "label", "text_input", t("ui_comp.label"))
        _apply_if_changed(btn_comp, "label", btn_comp.label, new_label)

        field_label(ctx, t("ui_comp.font"), lw)
        _render_font_picker(ctx, btn_comp, "font_path", lw, "btn_font_path")

        field_label(ctx, t("ui_comp.font_size"), lw)
        new_fs = ctx.drag_float("##btn_font_size", btn_comp.font_size, 0.5, 4.0, 256.0)
        _record_field(ctx, btn_comp, "font_size", "drag_float", t("ui_comp.font_size"))
        _apply_if_changed(btn_comp, "font_size", btn_comp.font_size, new_fs)

        _render_color_field(ctx, btn_comp, "label_color", t("ui_comp.label_color"), lw,
                            "##btn_label_color")

        field_label(ctx, t("ui_comp.line_height"), lw)
        new_lh = ctx.drag_float("##btn_line_height", btn_comp.line_height, 0.01, 0.5, 5.0)
        _record_field(ctx, btn_comp, "line_height", "drag_float", t("ui_comp.line_height"))
        target_lh = max(0.5, min(5.0, float(new_lh)))
        if not math.isclose(target_lh, float(btn_comp.line_height), rel_tol=1e-5, abs_tol=1e-6):
            _apply_if_changed(btn_comp, "line_height", btn_comp.line_height, target_lh)

        field_label(ctx, t("ui_comp.letter_spacing"), lw)
        new_ls = ctx.drag_float("##btn_letter_spacing", btn_comp.letter_spacing, 0.1, -20.0, 100.0)
        _record_field(ctx, btn_comp, "letter_spacing", "drag_float", t("ui_comp.letter_spacing"))
        target_ls = max(-20.0, min(100.0, float(new_ls)))
        if not math.isclose(target_ls, float(btn_comp.letter_spacing), rel_tol=1e-5, abs_tol=1e-6):
            _apply_if_changed(btn_comp, "letter_spacing", btn_comp.letter_spacing, target_ls)

        # Text alignment buttons
        if render_compact_section_header(ctx, t("ui_comp.text_alignment"), level="secondary"):
            _render_text_alignment_row(ctx, btn_comp, lw, "btn_text_alignment_row",
                                        default_h=TextAlignH.Center, default_v=TextAlignV.Center)

    # ── Fill ──
    if render_compact_section_header(ctx, t("ui_comp.fill"), level="primary"):
        lw = max_label_w(ctx, [t("ui_comp.texture"), t("ui_comp.background")])

        _render_texture_picker(ctx, btn_comp, "texture_path", t("ui_comp.texture"),
                               lw, "btn_texture")

        _render_color_field(ctx, btn_comp, "background_color", t("ui_comp.background"), lw,
                            "##btn_bg_color", default=list(Theme.UI_DEFAULT_BUTTON_BG))

    # ── Color Tint ──
    if render_compact_section_header(ctx, t("ui_comp.color_tint"), level="secondary"):
        lw = max_label_w(ctx, [t("ui_comp.tint_normal"), t("ui_comp.tint_highlighted"), t("ui_comp.tint_pressed"), t("ui_comp.tint_disabled")])
        for label_str, field_name in [
            (t("ui_comp.tint_normal"), "normal_color"),
            (t("ui_comp.tint_highlighted"), "highlighted_color"),
            (t("ui_comp.tint_pressed"), "pressed_color"),
            (t("ui_comp.tint_disabled"), "disabled_color"),
        ]:
            _render_color_field(ctx, btn_comp, field_name, label_str, lw,
                                f"##btn_{field_name}")

    # ── Events — On Click () ──
    _render_on_click_events(ctx, btn_comp)


def _render_onclick_arg_go(ctx, btn_comp, entries, i, arg_index, arg, lw, label,
                           clone_entries_fn, resolve_go_fn):
    """Render a game_object On-Click argument field."""
    from Infernux.components.ref_wrappers import GameObjectRef
    from .inspector_components import render_object_field, _picker_scene_gameobjects

    target_ref = _get_serializable_raw_field(arg, "game_object")
    resolved_arg_go = target_ref.resolve() if hasattr(target_ref, "resolve") else None
    display = resolved_arg_go.name if resolved_arg_go else t("igui.none")

    def _make_cbs(_entry_idx=i, _arg_idx=arg_index):
        def _set(ref):
            ne = clone_entries_fn(entries)
            ne[_entry_idx].arguments[_arg_idx].game_object = ref
            _apply_if_changed(btn_comp, "on_click_entries", entries, ne)

        def _on_drop(payload):
            go = resolve_go_fn(payload)
            if go is not None:
                _set(GameObjectRef(go))

        return (_on_drop,
                lambda go: _set(GameObjectRef(go)),
                lambda: _set(GameObjectRef(persistent_id=0)))

    go_drop, go_pick, go_clear = _make_cbs()
    object_id = int(getattr(target_ref, "persistent_id", 0) or 0)
    field_label(ctx, label, lw)
    render_object_field(
        ctx, f"onclick_arg_go_{i}_{arg_index}", display, "GameObject",
        clickable=False,
        accept_drag_type="HIERARCHY_GAMEOBJECT",
        on_drop_callback=go_drop,
        picker_scene_items=lambda filt: _picker_scene_gameobjects(filt),
        on_pick=go_pick,
        on_clear=go_clear,
        on_ping=(
            lambda _id=object_id: _ping_scene_object(_id)
        ) if object_id > 0 else None,
    )


def _render_onclick_arg_comp(ctx, btn_comp, entries, i, arg_index, spec, arg, lw, label,
                             clone_entries_fn, resolve_go_fn):
    """Render a component On-Click argument field."""
    from Infernux.components.ref_wrappers import ComponentRef
    from .inspector_components import render_object_field, _picker_scene_gameobjects, _create_component_ref_from_go

    comp_ref = _get_serializable_raw_field(arg, "component")
    display = comp_ref.display_name if isinstance(comp_ref, ComponentRef) else t("igui.none")
    type_hint = spec.component_type or "Component"

    def _make_cbs(_entry_idx=i, _arg_idx=arg_index, _comp_type=spec.component_type):
        def _set(ref):
            ne = clone_entries_fn(entries)
            ne[_entry_idx].arguments[_arg_idx].component = ref
            _apply_if_changed(btn_comp, "on_click_entries", entries, ne)

        def _on_drop(payload):
            go = resolve_go_fn(payload)
            if go is None:
                return
            ref = _create_component_ref_from_go(go, _comp_type)
            if ref is not None:
                _set(ref)

        def _on_pick(go):
            ref = _create_component_ref_from_go(go, _comp_type)
            if ref is not None:
                _set(ref)

        return (_on_drop, _on_pick,
                lambda: _set(ComponentRef(component_type=_comp_type or "")))

    comp_drop, comp_pick, comp_clear = _make_cbs()
    object_id = int(getattr(comp_ref, "go_id", 0) or 0)
    field_label(ctx, label, lw)
    render_object_field(
        ctx, f"onclick_arg_comp_{i}_{arg_index}", display, type_hint,
        clickable=False,
        accept_drag_type="HIERARCHY_GAMEOBJECT",
        on_drop_callback=comp_drop,
        picker_scene_items=lambda filt, _ct=spec.component_type: _picker_scene_gameobjects(filt, required_component=_ct),
        on_pick=comp_pick,
        on_clear=comp_clear,
        on_ping=(
            lambda _id=object_id: _ping_scene_object(_id)
        ) if object_id > 0 else None,
    )


def _render_onclick_argument_field(
    ctx, btn_comp, entries, entry_idx, arg_index, spec, arg, lw,
    clone_entries_fn, resolve_go_fn,
):
    """Render one On Click() event argument field.

    Returns ``(updated_entries, updated_current_args | None, changed)``.
    """
    label = f"{spec.display_name}"
    kind = spec.kind
    i = entry_idx

    if kind == "bool":
        new_value = render_inspector_checkbox(ctx, label, bool(arg.bool_value))
        if bool(new_value) != bool(arg.bool_value):
            new_entries = clone_entries_fn(entries)
            new_entries[i].arguments[arg_index].bool_value = bool(new_value)
            _apply_if_changed(btn_comp, "on_click_entries", entries, new_entries)
            return new_entries, list(new_entries[i].arguments or []), True
    elif kind == "int":
        field_label(ctx, label, lw)
        new_value = int(ctx.drag_int(f"##onclick_arg_int_{i}_{arg_index}", int(arg.int_value), 1.0, -2147483647, 2147483647))
        if new_value != int(arg.int_value):
            new_entries = clone_entries_fn(entries)
            new_entries[i].arguments[arg_index].int_value = int(new_value)
            _apply_if_changed(btn_comp, "on_click_entries", entries, new_entries)
            return new_entries, list(new_entries[i].arguments or []), True
    elif kind == "float":
        field_label(ctx, label, lw)
        new_value = float(ctx.drag_float(f"##onclick_arg_float_{i}_{arg_index}", float(arg.float_value), 0.1, -1000000.0, 1000000.0))
        if not math.isclose(new_value, float(arg.float_value), rel_tol=1e-5, abs_tol=1e-6):
            new_entries = clone_entries_fn(entries)
            new_entries[i].arguments[arg_index].float_value = float(new_value)
            _apply_if_changed(btn_comp, "on_click_entries", entries, new_entries)
            return new_entries, list(new_entries[i].arguments or []), True
    elif kind == "game_object":
        _render_onclick_arg_go(ctx, btn_comp, entries, i, arg_index, arg, lw, label,
                               clone_entries_fn, resolve_go_fn)
    elif kind == "component":
        _render_onclick_arg_comp(ctx, btn_comp, entries, i, arg_index, spec, arg, lw, label,
                                 clone_entries_fn, resolve_go_fn)
    else:
        field_label(ctx, label, lw)
        new_value = ctx.text_input(f"##onclick_arg_str_{i}_{arg_index}", str(arg.string_value or ""), 1024)
        if new_value != str(arg.string_value or ""):
            new_entries = clone_entries_fn(entries)
            new_entries[i].arguments[arg_index].string_value = new_value
            _apply_if_changed(btn_comp, "on_click_entries", entries, new_entries)
            return new_entries, list(new_entries[i].arguments or []), True

    return entries, None, False


def _clone_onclick_argument(arg):
    """Clone a UIEventArgument without deepcopy of C++ objects."""
    from Infernux.ui.ui_event_entry import UIEventArgument
    return UIEventArgument(
        kind=getattr(arg, "kind", "string") or "string",
        name=getattr(arg, "name", "") or "",
        component_type=getattr(arg, "component_type", "") or "",
        int_value=int(getattr(arg, "int_value", 0) or 0),
        float_value=float(getattr(arg, "float_value", 0.0) or 0.0),
        bool_value=bool(getattr(arg, "bool_value", False)),
        string_value=getattr(arg, "string_value", "") or "",
        game_object=copy.deepcopy(_get_serializable_raw_field(arg, "game_object"), {}),
        component=copy.deepcopy(_get_serializable_raw_field(arg, "component"), {}),
    )


def _clone_onclick_entry(e):
    """Clone a UIEventEntry without deepcopy (avoids pickling C++ objects)."""
    from Infernux.ui.ui_event_entry import UIEventEntry
    from Infernux.components.ref_wrappers import GameObjectRef
    target_ref = _get_serializable_raw_field(e, "target")
    pid = getattr(target_ref, "persistent_id", 0) or 0
    return UIEventEntry(
        target=GameObjectRef(persistent_id=pid),
        component_name=getattr(e, "component_name", "") or "",
        method_name=getattr(e, "method_name", "") or "",
        arguments=[_clone_onclick_argument(arg) for arg in (getattr(e, "arguments", None) or [])],
    )


def _clone_onclick_entries(lst):
    return [_clone_onclick_entry(e) for e in lst]


def _persistent_event_combo_options(current: str, available, none_label: str):
    """Keep serialized bindings intact while scripts or references reload."""
    values = [""] + list(available)
    labels = [none_label] + list(available)
    if current and current not in values:
        values.append(current)
        labels.append(current)
    return labels, values


def _resolve_onclick_go(payload):
    """Resolve a hierarchy drag payload to a GameObject."""
    from Infernux.lib import SceneManager
    scene = SceneManager.instance().get_active_scene()
    if not scene:
        return None
    obj_id = int(payload) if isinstance(payload, (int, float)) else None
    if obj_id is None:
        return None
    return scene.find_by_id(obj_id)


def _ping_scene_object(object_id: int) -> None:
    from ._inspector_references import ping_scene_object_in_hierarchy

    ping_scene_object_in_hierarchy(int(object_id))


def _render_onclick_target_field(ctx, btn_comp, entries, i, entry, lw):
    """Render the target GameObject field for one On Click entry.

    Returns ``(on_drop, on_pick, on_clear, resolved_go)``.
    """
    from Infernux.components.ref_wrappers import GameObjectRef
    from .inspector_components import render_object_field, _picker_scene_gameobjects

    target_ref = _get_serializable_raw_field(entry, "target")
    resolved_go = target_ref.resolve() if hasattr(target_ref, "resolve") else None
    display = resolved_go.name if resolved_go else t("igui.none")

    def _make_target_cbs(_idx=i):
        def _set(ref):
            old_entries = list(btn_comp.on_click_entries or [])
            new_entries = _clone_onclick_entries(old_entries)
            if _idx >= len(new_entries):
                return
            new_entries[_idx].target = ref
            new_entries[_idx].component_name = ""
            new_entries[_idx].method_name = ""
            _record_property(btn_comp, "on_click_entries",
                             old_entries, new_entries, "Set on_click_entries")

        def _on_drop(payload):
            go = _resolve_onclick_go(payload)
            if go is not None:
                _set(GameObjectRef(go))

        return (_on_drop,
                lambda go: _set(GameObjectRef(go)),
                lambda: _set(GameObjectRef(persistent_id=0)))

    on_drop, on_pick, on_clear = _make_target_cbs()
    object_id = int(getattr(target_ref, "persistent_id", 0) or 0)

    field_label(ctx, t("ui_comp.target"), lw)
    render_object_field(
        ctx, f"onclick_target_{i}", display, "GameObject",
        clickable=False,
        accept_drag_type="HIERARCHY_GAMEOBJECT",
        on_drop_callback=on_drop,
        picker_scene_items=lambda filt: _picker_scene_gameobjects(filt),
        on_pick=on_pick,
        on_clear=on_clear,
        on_ping=(
            lambda _id=object_id: _ping_scene_object(_id)
        ) if object_id > 0 else None,
    )
    return resolved_go


def _render_onclick_arguments(ctx, btn_comp, entries, i, entry, lw,
                              selected_component, cur_method):
    """Render the arguments section for one On Click entry.

    Returns ``(entries, changed)``.
    """
    from Infernux.ui.ui_event_entry import get_method_parameter_specs, normalize_event_arguments
    changed = False

    if selected_component is None or not (getattr(entry, "method_name", "") or ""):
        return entries, changed

    specs = get_method_parameter_specs(selected_component, getattr(entry, "method_name", "") or "")
    current_args = list(getattr(entry, "arguments", None) or [])
    normalized_args = normalize_event_arguments(current_args, specs)
    if normalized_args != current_args:
        new_entries = _clone_onclick_entries(entries)
        new_entries[i].arguments = normalized_args
        _apply_if_changed(btn_comp, "on_click_entries", entries, new_entries)
        entries = new_entries
        entry = new_entries[i]
        current_args = list(entry.arguments or [])
        changed = True
    else:
        current_args = normalized_args

    if specs:
        if render_compact_section_header(ctx, t("ui_comp.arguments"), level="secondary"):
            field_label(ctx, t("ui_comp.arguments"), lw)
            ctx.label(t("ui_comp.params_count").format(n=len(specs)))
            for arg_index, spec in enumerate(specs):
                arg = current_args[arg_index]
                ctx.push_id(arg_index)
                entries, _upd, _ch = _render_onclick_argument_field(
                    ctx, btn_comp, entries, i, arg_index, spec, arg, lw,
                    _clone_onclick_entries, _resolve_onclick_go,
                )
                if _ch:
                    current_args = _upd
                    changed = True
                ctx.pop_id()
    elif cur_method:
        field_label(ctx, t("ui_comp.arguments"), lw)
        ctx.label(t("ui_comp.no_parameters"))

    return entries, changed


def _render_onclick_entry(ctx, btn_comp, entries, i, entry, lw):
    """Render one On Click entry (target, component, method, arguments).

    Returns ``(updated_entries, changed)``.
    """
    from Infernux.ui.ui_event_entry import get_callable_methods, get_method_parameter_specs, normalize_event_arguments

    changed = False

    # ── Target GameObject ──
    resolved_go = _render_onclick_target_field(ctx, btn_comp, entries, i, entry, lw)

    # ── Component combo ──
    comp_names = []
    if resolved_go:
        for py_comp in resolved_go.get_py_components():
            cname = type(py_comp).__name__
            if cname not in comp_names:
                comp_names.append(cname)

    cur_comp_name = getattr(entry, "component_name", "") or ""
    comp_labels, comp_values = _persistent_event_combo_options(
        cur_comp_name, comp_names, t("ui_comp.none")
    )
    try:
        comp_idx = comp_values.index(cur_comp_name)
    except ValueError:
        comp_idx = 0

    field_label(ctx, t("ui_comp.component"), lw)
    new_comp_idx = ctx.combo(f"##onclick_comp_{i}", comp_idx, comp_labels, -1)
    new_comp_name = comp_values[new_comp_idx] if 0 <= new_comp_idx < len(comp_values) else cur_comp_name
    if new_comp_name != cur_comp_name:
        new_entries = _clone_onclick_entries(entries)
        new_entries[i].component_name = new_comp_name
        new_entries[i].method_name = ""
        new_entries[i].arguments = []
        _apply_if_changed(btn_comp, "on_click_entries", entries, new_entries)
        entries = new_entries
        changed = True

    # ── Method combo ──
    method_names = []
    selected_component = None
    if resolved_go and cur_comp_name:
        for py_comp in resolved_go.get_py_components():
            if type(py_comp).__name__ == cur_comp_name:
                selected_component = py_comp
                method_names = get_callable_methods(py_comp)
                break

    cur_method = getattr(entry, "method_name", "") or ""
    method_labels, method_values = _persistent_event_combo_options(
        cur_method, method_names, t("ui_comp.none")
    )
    try:
        method_idx = method_values.index(cur_method)
    except ValueError:
        method_idx = 0

    field_label(ctx, t("ui_comp.method"), lw)
    new_method_idx = ctx.combo(f"##onclick_method_{i}", method_idx, method_labels, -1)
    new_method = method_values[new_method_idx] if 0 <= new_method_idx < len(method_values) else cur_method
    if new_method != cur_method:
        new_entries = _clone_onclick_entries(entries)
        new_entries[i].method_name = new_method
        if selected_component is not None and new_method:
            specs = get_method_parameter_specs(selected_component, new_method)
            new_entries[i].arguments = normalize_event_arguments([], specs)
        else:
            new_entries[i].arguments = []
        _apply_if_changed(btn_comp, "on_click_entries", entries, new_entries)
        entries = new_entries
        changed = True

    # ── Arguments ──
    entries, args_changed = _render_onclick_arguments(
        ctx, btn_comp, entries, i, entry, lw, selected_component, cur_method)
    changed = changed or args_changed

    return entries, changed


def _render_on_click_events(ctx, btn_comp):
    """Render the Unity-style On Click () persistent event list."""
    IGUI = _igui()
    from Infernux.ui.ui_event_entry import UIEventEntry

    entries = list(btn_comp.on_click_entries or [])

    def _on_add():
        old_entries = list(btn_comp.on_click_entries or [])
        new_entries = _clone_onclick_entries(old_entries)
        new_entries.append(UIEventEntry())
        _record_property(btn_comp, "on_click_entries",
                         old_entries, new_entries, "Set on_click_entries")

    header_open = IGUI.list_header(
        ctx, t("ui_comp.on_click"), len(entries),
        on_add=_on_add,
        level="primary",
    )
    if not header_open:
        return

    _BTN_W = 24.0
    remove_index = None
    lw = max_label_w(ctx, [t("ui_comp.target"), t("ui_comp.component"), t("ui_comp.method"), t("ui_comp.arguments")])

    for i, entry in enumerate(entries):
        ctx.push_id(i)

        entry_open = render_compact_section_header(
            ctx, t("ui_comp.click_entry").format(n=i + 1), level="tertiary", allow_overlap=True,
        )

        ctx.same_line(0, 0)
        avail_w = ctx.get_content_region_avail_width()
        if avail_w >= _BTN_W:
            ctx.set_cursor_pos_x(ctx.get_cursor_pos_x() + avail_w - _BTN_W)
        if _igui().list_item_remove_button(ctx, f"click_{i}"):
            remove_index = i

        if entry_open:
            entries, _changed = _render_onclick_entry(ctx, btn_comp, entries, i, entry, lw)

        ctx.pop_id()

    if remove_index is not None:
        old_entries = list(btn_comp.on_click_entries or [])
        new_entries = [_clone_onclick_entry(e) for j, e in enumerate(old_entries) if j != remove_index]
        _record_property(btn_comp, "on_click_entries",
                         old_entries, new_entries, "Set on_click_entries")


register_py_component_renderer("UIButton", _render_button_inspector)
