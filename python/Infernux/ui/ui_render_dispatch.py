"""Unified UI element rendering dispatch.

Provides a registry-based pattern so that adding a new UI component type
(e.g. UIButton) only requires registering one renderer per back-end,
rather than scattering ``isinstance`` chains across multiple panels.

Two back-ends:
- **editor**: ImGui draw-list commands via ``InxGUIContext`` (UI Editor panel).
- **runtime**: GPU ScreenUI renderer commands (Game View panel).
"""

from __future__ import annotations

from typing import Callable, Dict, Optional

from Infernux.ui.enums import TextAlignH, TextAlignV
from Infernux.engine.ui.theme import Theme
from Infernux.ui.ui_render_revision import get_runtime_ui_revision


# ── Shared attribute helpers ─────────────────────────────────────────

def _pad_rgba(color, default=None) -> list:
    """Ensure *color* is a 4-element RGBA list, padding missing channels with 1.0."""
    if not color:
        if default is not None:
            return list(default)
        return [1.0, 1.0, 1.0, 1.0]
    color = list(color)
    while len(color) < 4:
        color.append(1.0)
    return color


def extract_common(elem) -> dict:
    """Extract shared visual attributes from any InxUIScreenComponent."""
    color = _pad_rgba(getattr(elem, "color", None))
    opacity = max(0.0, min(1.0, float(getattr(elem, "opacity", 1.0))))
    return {
        "color": color,
        "opacity": opacity,
        "rotation": float(getattr(elem, "rotation", 0.0)),
        "mirror_h": bool(getattr(elem, "mirror_x", False)),
        "mirror_v": bool(getattr(elem, "mirror_y", False)),
        "corner_radius": float(getattr(elem, "corner_radius", 0.0)),
    }


def _extract_text_attrs(elem, scale: float = 1.0) -> dict:
    """Extract text-rendering attributes from a text-bearing element.

    *scale* is applied to letter_spacing (zoom for editor, text_scale for
    runtime).  font_size is returned raw (caller applies own scaling).
    """
    ah = getattr(elem, "text_align_h", TextAlignH.Left)
    av = getattr(elem, "text_align_v", TextAlignV.Top)
    ax, ay = text_align_to_float(ah, av)
    return {
        "font_path": str(getattr(elem, "font_path", "") or ""),
        "font_size": float(getattr(elem, "font_size", Theme.UI_DEFAULT_FONT_SIZE)),
        "line_height": float(getattr(elem, "line_height", Theme.UI_DEFAULT_LINE_HEIGHT)),
        "letter_spacing": float(getattr(elem, "letter_spacing", Theme.UI_DEFAULT_LETTER_SPACING)) * scale,
        "align_x": ax,
        "align_y": ay,
    }


def text_align_to_float(align_h, align_v) -> tuple[float, float]:
    """Convert TextAlignH/V enums to (0.0/0.5/1.0) floats."""
    ax = 0.0 if align_h == TextAlignH.Left else (0.5 if align_h == TextAlignH.Center else 1.0)
    ay = 0.0 if align_v == TextAlignV.Top else (0.5 if align_v == TextAlignV.Center else 1.0)
    return ax, ay


# ── Registry ─────────────────────────────────────────────────────────

# Keyed by (component class name, backend_name)
_RENDERERS: Dict[tuple[str, str], Callable] = {}
_RESOLVED_RENDERERS: Dict[tuple[type, str], Optional[Callable]] = {}
_REVISION_MASK = (1 << 64) - 1


def runtime_ui_revision(scene, canvases, width: int, height: int,
                        texture_generation: int = 0) -> int:
    """Build an O(1) content revision for native command-list reuse.

    UI components increment a shared generation on visual mutation. Scene
    structure covers hierarchy and active-state changes, so an unchanged frame
    no longer crosses Python/pybind once per visible UI element.
    """
    del canvases
    signature = hash((
        id(scene),
        int(getattr(scene, "structure_version", 0)),
        int(width),
        int(height),
        int(texture_generation),
        get_runtime_ui_revision(),
    )) & _REVISION_MASK
    return signature or 1


def register_ui_renderer(component_cls_name: str, backend: str, fn: Callable):
    """Register a renderer function for a UI component type.

    Args:
        component_cls_name: e.g. ``"UIText"``, ``"UIImage"``.
        backend: ``"editor"`` or ``"runtime"``.
        fn: Callable with backend-specific signature (see below).
    """
    _RENDERERS[(component_cls_name, backend)] = fn
    _RESOLVED_RENDERERS.clear()


def get_ui_renderer(component_cls_name: str, backend: str) -> Optional[Callable]:
    """Look up a registered renderer."""
    return _RENDERERS.get((component_cls_name, backend))


def _resolve_renderer(elem_type: type, backend: str) -> Optional[Callable]:
    cache_key = (elem_type, backend)
    cached = _RESOLVED_RENDERERS.get(cache_key, None)
    if cache_key in _RESOLVED_RENDERERS:
        return cached

    fn = _RENDERERS.get((elem_type.__name__, backend))
    if fn is None:
        for base in elem_type.__mro__[1:]:
            fn = _RENDERERS.get((base.__name__, backend))
            if fn is not None:
                break

    _RESOLVED_RENDERERS[cache_key] = fn
    return fn


def dispatch(elem, backend: str, **kwargs):
    """Dispatch rendering of *elem* to the registered handler.

    Returns True if a handler was found and called, False otherwise.
    """
    fn = _resolve_renderer(type(elem), backend)
    if fn is None:
        return False
    fn(elem, **kwargs)
    return True


# ══════════════════════════════════════════════════════════════════════
#  Built-in renderers — EDITOR back-end (ImGui draw-list)
# ══════════════════════════════════════════════════════════════════════

def _editor_render_text(elem, ctx, base_sx, base_sy, base_sw, base_sh, zoom, get_tex_id, **_kw):
    """Render a UIText element in the UI Editor panel."""
    attrs = extract_common(elem)
    color = attrs["color"]
    ta = _extract_text_attrs(elem, scale=zoom)
    text_size = max(1.0, ta["font_size"] * zoom)
    editor_wrap_width = base_sw
    if getattr(elem, "is_auto_width", lambda: False)():
        editor_wrap_width = 0.0
    elif hasattr(elem, "get_editor_wrap_width"):
        editor_wrap_width = float(elem.get_editor_wrap_width()) * zoom
    ctx.draw_text_ex_aligned(
        base_sx, base_sy, base_sx + base_sw, base_sy + base_sh,
        elem.text,
        color[0], color[1], color[2], color[3] * attrs["opacity"],
        ta["align_x"], ta["align_y"], text_size,
        editor_wrap_width,
        attrs["rotation"], attrs["mirror_h"], attrs["mirror_v"], False,
        ta["font_path"], ta["line_height"], ta["letter_spacing"],
    )


def _editor_render_image(elem, ctx, base_sx, base_sy, base_sw, base_sh, zoom, get_tex_id, **_kw):
    """Render a UIImage element in the UI Editor panel."""
    attrs = extract_common(elem)
    color = attrs["color"]
    cr, cg, cb = color[0], color[1], color[2]
    ca = color[3] * attrs["opacity"]
    tex_path = str(getattr(elem, "texture_path", "") or "")
    tex_id = get_tex_id(tex_path) if tex_path else 0
    rounding = attrs["corner_radius"] * zoom
    if tex_id:
        ctx.draw_image_rect(
            tex_id, base_sx, base_sy,
            base_sx + base_sw, base_sy + base_sh,
            0.0, 0.0, 1.0, 1.0,
            cr, cg, cb, ca,
            attrs["rotation"], attrs["mirror_h"], attrs["mirror_v"],
            rounding,
        )
    else:
        _draw_editor_placeholder(ctx, base_sx, base_sy, base_sw, base_sh,
                                 cr, cg, cb, ca, rounding, attrs["corner_radius"] * zoom,
                                 attrs["rotation"], attrs["mirror_h"], attrs["mirror_v"])

def _draw_editor_placeholder(ctx, x, y, w, h, cr, cg, cb, ca, rounding, rect_rounding, rotation=0.0, mirror_h=False, mirror_v=False):
    """Draw a placeholder rect with tint + cross pattern (editor backend)."""
    tint = Theme.UI_EDITOR_PLACEHOLDER_TINT
    alpha = Theme.UI_EDITOR_PLACEHOLDER_ALPHA
    ctx.draw_filled_rect_rotated(x, y, x + w, y + h,
                         cr * tint, cg * tint, cb * tint, ca * alpha,
                         rotation, mirror_h, mirror_v, rounding)
    ctx.draw_rect(x, y, x + w, y + h, 0.5, 0.5, 0.5, 0.5, 1.0, rect_rounding)
    ctx.draw_line(x, y, x + w, y + h, 0.5, 0.5, 0.5, 0.3, 1.0)
    ctx.draw_line(x + w, y, x, y + h, 0.5, 0.5, 0.5, 0.3, 1.0)


# ══════════════════════════════════════════════════════════════════════
#  Built-in renderers — RUNTIME back-end (GPU ScreenUI renderer)
# ══════════════════════════════════════════════════════════════════════

def _runtime_render_text(elem, renderer, ui_list, sx, sy, sw, sh,
                         ref_w, ref_h, scale_x, scale_y, text_scale, get_tex_id, **_kw):
    """Render a UIText element via the GPU ScreenUI renderer."""
    revision = int(getattr(elem, "_ui_render_revision", 0))
    packet_key = (id(renderer), ui_list, revision, sx, sy, sw, sh, text_scale)
    packet = getattr(elem, "_runtime_text_packet", None)
    if packet is not None and packet[0] == packet_key:
        renderer.add_text(*packet[1])
        return

    attrs = extract_common(elem)
    color = attrs["color"]
    ta = _extract_text_attrs(elem, scale=text_scale)
    font_size_raw = ta["font_size"]

    wrap_width = float(elem.get_wrap_width()) if hasattr(elem, "get_wrap_width") else 0.0
    scaled_wrap_width = 0.0 if wrap_width <= 0.0 else wrap_width * text_scale

    auto_width = getattr(elem, "is_auto_width", lambda: False)()
    auto_height = getattr(elem, "is_auto_height", lambda: False)()
    if auto_width or auto_height:
        measure_key = (
            elem.text,
            ta["font_path"],
            font_size_raw,
            ta["line_height"],
            ta["letter_spacing"],
            scaled_wrap_width,
            text_scale,
        )
        if getattr(elem, "_runtime_measure_key", None) != measure_key:
            elem._runtime_measure_size = renderer.measure_text(
                elem.text,
                font_size_raw * text_scale,
                scaled_wrap_width,
                ta["font_path"], ta["line_height"], ta["letter_spacing"],
            )
            elem._runtime_measure_key = measure_key
        measured_w, measured_h = elem._runtime_measure_size

        if auto_width:
            target_width = max(1.0, float(measured_w) / max(text_scale, 1e-6))
            if abs(float(elem.width) - target_width) > 0.01:
                elem.set_size_preserve_corner(
                    target_width,
                    float(elem.height), ref_w, ref_h, "top_left",
                )
            sw = elem.width * scale_x
        elif auto_height:
            target_height = max(1.0, float(measured_h) / max(text_scale, 1e-6))
            if abs(float(elem.height) - target_height) > 0.01:
                elem.set_size_preserve_corner(
                    float(elem.width),
                    target_height,
                    ref_w, ref_h, "top_left",
                )
            sh = elem.height * scale_y

    font_size = max(1.0, font_size_raw * text_scale)
    ca = color[3] * attrs["opacity"]
    arguments = (
        ui_list,
        sx, sy, sx + sw, sy + sh,
        elem.text,
        color[0], color[1], color[2], ca,
        ta["align_x"], ta["align_y"], font_size,
        0.0 if getattr(elem, "is_auto_width", lambda: False)() else scaled_wrap_width,
        attrs["rotation"], attrs["mirror_h"], attrs["mirror_v"],
        ta["font_path"], ta["line_height"], ta["letter_spacing"],
    )
    elem._runtime_text_packet = (packet_key, arguments)
    renderer.add_text(*arguments)


def _runtime_render_image(elem, renderer, ui_list, sx, sy, sw, sh,
                          scale_x, scale_y, get_tex_id, **_kw):
    """Render a UIImage element via the GPU ScreenUI renderer."""
    tex_path = getattr(elem, "texture_path", "") or ""
    tex_id = get_tex_id(tex_path) if tex_path else 0
    revision = int(getattr(elem, "_ui_render_revision", 0))
    packet_key = (id(renderer), ui_list, revision, tex_id, sx, sy, sw, sh, scale_x, scale_y)
    packet = getattr(elem, "_runtime_image_packet", None)
    if packet is not None and packet[0] == packet_key:
        kind, arguments = packet[1]
        if kind == "image":
            renderer.add_image(*arguments)
        else:
            renderer.add_filled_rect(*arguments)
        return

    attrs = extract_common(elem)
    color = attrs["color"]
    cr, cg, cb = color[0], color[1], color[2]
    ca = color[3] * attrs["opacity"]
    rounding = attrs["corner_radius"] * min(scale_x, scale_y)
    if tex_id:
        kind = "image"
        arguments = (
            ui_list, tex_id,
            sx, sy, sx + sw, sy + sh,
            0.0, 0.0, 1.0, 1.0,
            cr, cg, cb, ca,
            attrs["rotation"], attrs["mirror_h"], attrs["mirror_v"],
            rounding,
        )
    else:
        kind = "rect"
        arguments = (
            ui_list,
            sx, sy, sx + sw, sy + sh,
            cr, cg, cb, ca,
            rounding,
        )
    elem._runtime_image_packet = (packet_key, (kind, arguments))
    if kind == "image":
        renderer.add_image(*arguments)
    else:
        renderer.add_filled_rect(*arguments)


# ── Register built-in renderers ──────────────────────────────────────

register_ui_renderer("UIText", "editor", _editor_render_text)
register_ui_renderer("UIImage", "editor", _editor_render_image)
register_ui_renderer("UIText", "runtime", _runtime_render_text)
register_ui_renderer("UIImage", "runtime", _runtime_render_image)


# ══════════════════════════════════════════════════════════════════════
#  UIButton renderers — shared helpers + per-backend glue
# ══════════════════════════════════════════════════════════════════════

def _get_button_bg(elem):
    """Return the button's background colour as a 4-element list."""
    return _pad_rgba(getattr(elem, "background_color", None),
                     default=Theme.UI_DEFAULT_BUTTON_BG)


def _get_label_attrs(elem, scale: float):
    """Return (label, color, text_attrs) for a button's label text."""
    label = getattr(elem, "label", "") or ""
    lc = _pad_rgba(getattr(elem, "label_color", None),
                   default=Theme.UI_DEFAULT_LABEL_COLOR)
    # Buttons default to Center/Center alignment
    ah = getattr(elem, "text_align_h", TextAlignH.Center)
    av = getattr(elem, "text_align_v", TextAlignV.Center)
    ax, ay = text_align_to_float(ah, av)
    ta = {
        "font_path": str(getattr(elem, "font_path", "") or ""),
        "font_size": float(getattr(elem, "font_size", Theme.UI_DEFAULT_FONT_SIZE)),
        "line_height": float(getattr(elem, "line_height", Theme.UI_DEFAULT_LINE_HEIGHT)),
        "letter_spacing": float(getattr(elem, "letter_spacing", Theme.UI_DEFAULT_LETTER_SPACING)) * scale,
        "align_x": ax,
        "align_y": ay,
    }
    return label, lc, ta


def _editor_render_button(elem, ctx, base_sx, base_sy, base_sw, base_sh, zoom, get_tex_id, **_kw):
    """Render a UIButton element in the UI Editor panel."""
    attrs = extract_common(elem)
    bg = _get_button_bg(elem)
    rounding = attrs["corner_radius"] * zoom

    # Background: texture image or solid fill
    tex_path = getattr(elem, "texture_path", "") or ""
    tex_id = get_tex_id(tex_path) if tex_path else 0
    if tex_id:
        ctx.draw_image_rect(
            tex_id,
            base_sx, base_sy, base_sx + base_sw, base_sy + base_sh,
            0.0, 0.0, 1.0, 1.0,
            bg[0], bg[1], bg[2], bg[3] * attrs["opacity"],
            attrs["rotation"], attrs["mirror_h"], attrs["mirror_v"],
            rounding,
        )
    else:
        ctx.draw_filled_rect_rotated(
            base_sx, base_sy, base_sx + base_sw, base_sy + base_sh,
            bg[0], bg[1], bg[2], bg[3] * attrs["opacity"],
            attrs["rotation"], attrs["mirror_h"], attrs["mirror_v"],
            rounding,
        )

    # Label
    label, lc, ta = _get_label_attrs(elem, scale=zoom)
    if label:
        font_size = max(1.0, ta["font_size"] * zoom)
        ctx.draw_text_ex_aligned(
            base_sx, base_sy, base_sx + base_sw, base_sy + base_sh,
            label,
            lc[0], lc[1], lc[2], lc[3] * attrs["opacity"],
            ta["align_x"], ta["align_y"], font_size,
            base_sw,
            attrs["rotation"], attrs["mirror_h"], attrs["mirror_v"], False,
            ta["font_path"], ta["line_height"], ta["letter_spacing"],
        )


def _runtime_render_button(elem, renderer, ui_list, sx, sy, sw, sh,
                           scale_x, scale_y, text_scale, get_tex_id, **_kw):
    """Render a UIButton element via the GPU ScreenUI renderer."""
    tex_path = getattr(elem, "texture_path", "") or ""
    tex_id = get_tex_id(tex_path) if tex_path else 0
    revision = int(getattr(elem, "_ui_render_revision", 0))
    state = getattr(elem, "_current_state", None)
    packet_key = (id(renderer), ui_list, revision, state, tex_id, sx, sy, sw, sh, scale_x, scale_y, text_scale)
    packet = getattr(elem, "_runtime_button_packet", None)
    if packet is not None and packet[0] == packet_key:
        for kind, arguments in packet[1]:
            if kind == "image":
                renderer.add_image(*arguments)
            elif kind == "rect":
                renderer.add_filled_rect(*arguments)
            else:
                renderer.add_text(*arguments)
        return

    attrs = extract_common(elem)
    tint = _pad_rgba(elem.get_current_tint() if hasattr(elem, "get_current_tint") else None)
    bg = _get_button_bg(elem)
    r = bg[0] * tint[0]
    g = bg[1] * tint[1]
    b = bg[2] * tint[2]
    a = bg[3] * tint[3] * attrs["opacity"]
    rounding = attrs["corner_radius"] * min(scale_x, scale_y)

    # Background: texture image or solid fill
    commands = []
    if tex_id:
        arguments = (
            ui_list, tex_id,
            sx, sy, sx + sw, sy + sh,
            0.0, 0.0, 1.0, 1.0,
            r, g, b, a,
            attrs["rotation"], attrs["mirror_h"], attrs["mirror_v"],
            rounding,
        )
        commands.append(("image", arguments))
    else:
        commands.append(("rect", (ui_list, sx, sy, sx + sw, sy + sh, r, g, b, a, rounding)))

    # Label
    label, lc, ta = _get_label_attrs(elem, scale=text_scale)
    if label:
        font_size = max(1.0, ta["font_size"] * text_scale)
        commands.append(("text", (
            ui_list,
            sx, sy, sx + sw, sy + sh,
            label,
            lc[0], lc[1], lc[2], lc[3] * attrs["opacity"],
            ta["align_x"], ta["align_y"], font_size,
            sw,
            attrs["rotation"], attrs["mirror_h"], attrs["mirror_v"],
            ta["font_path"], ta["line_height"], ta["letter_spacing"],
        )))

    elem._runtime_button_packet = (packet_key, commands)
    for kind, arguments in commands:
        if kind == "image":
            renderer.add_image(*arguments)
        elif kind == "rect":
            renderer.add_filled_rect(*arguments)
        else:
            renderer.add_text(*arguments)


register_ui_renderer("UIButton", "editor", _editor_render_button)
register_ui_renderer("UIButton", "runtime", _runtime_render_button)
