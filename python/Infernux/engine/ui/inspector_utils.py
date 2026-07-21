"""
Unified layout utilities and field renderers for Inspector panel sub-modules.

All inspector code (components, render-stack, materials) **must** use the
functions in this module for a consistent look and feel.  DO NOT duplicate
field-rendering logic in individual inspector modules.

Hierarchy of abstractions
~~~~~~~~~~~~~~~~~~~~~~~~~
1.  **Constants** — drag-speeds, padding, minimum widths.
2.  **Layout helpers** — ``field_label``, ``max_label_w``, ``pretty_field_name``.
3.  **Enum helpers** — robust Python-Enum / pybind11-enum compat.
4.  **Unified field renderers** — ``render_serialized_field`` (FieldType-based),
    ``render_material_property`` (Material JSON type-system).
5.  **Change-detection** — ``has_field_changed``, ``float_close``.
6.  **Misc UI widgets** — ``render_info_text``, ``render_apply_revert``.
"""

from __future__ import annotations

import math
import os
from functools import lru_cache
from typing import Any

from Infernux.lib import InxGUIContext
from .theme import Theme, ImGuiCol, ImGuiStyleVar, ImGuiTreeNodeFlags
from Infernux.debug import Debug


# ═══════════════════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════════════════

LABEL_PAD: float = Theme.INSPECTOR_LABEL_PAD
"""Extra pixels added after the widest label text."""

MIN_LABEL_WIDTH: float = Theme.INSPECTOR_MIN_LABEL_WIDTH
"""Absolute lower bound for the label column so the inspector never looks cramped."""

DRAG_SPEED_DEFAULT: float = 0.1
"""Default drag-float speed — matches Unity feel."""

DRAG_SPEED_FINE: float = 0.01
"""Fine drag speed for scale / small-precision fields."""

DRAG_SPEED_INT: float = 1.0
"""Drag speed for integer fields."""


# ═══════════════════════════════════════════════════════════════════════════
#  Float comparison
# ═══════════════════════════════════════════════════════════════════════════

def float_close(a: float, b: float, rel_tol: float = 1e-5,
                abs_tol: float = 1e-7) -> bool:
    """Return True if *a* and *b* are close enough to be treated as equal.

    Avoids phantom change-detection caused by float32-float64 round-trips
    through JSON & pybind11.
    """
    return math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)


# ═══════════════════════════════════════════════════════════════════════════
#  Naming / formatting
# ═══════════════════════════════════════════════════════════════════════════

@lru_cache(maxsize=512)
def format_display_name(name: str, *, title_case: bool = False) -> str:
    """Format identifiers for UI display.

    Rules:
    - one or more leading underscores become a single ``[Inner]`` prefix
    - remaining underscores become spaces
    - optional title-casing only affects lowercase words so CamelCase survives
    """
    if not name:
        return ""

    stripped = name.lstrip("_")
    has_inner_prefix = stripped != name
    body = " ".join(stripped.replace("_", " ").split())

    if title_case:
        words = []
        for word in body.split(" "):
            if word.islower():
                words.append(word.title())
            else:
                words.append(word)
        body = " ".join(words)

    if has_inner_prefix:
        return f"[Inner] {body}" if body else "[Inner]"
    return body


def pretty_field_name(name: str) -> str:
    """Convert identifiers to a readable field label."""
    return format_display_name(name, title_case=True)


def semantic_capture_enabled(ctx: InxGUIContext) -> bool:
    """Return whether this frame is collecting an MCP semantic snapshot.

    Test doubles created before request-only capture do not expose the native
    property, so they retain the historical capture-on behavior.
    """
    return bool(getattr(ctx, "semantic_capture_enabled", True))


def inspector_component_semantic_id(component, field_name: str) -> str:
    """Return the stable semantic identity for an Inspector field.

    ImGui widget IDs are intentionally local and often hidden (``##...``).
    MCP needs an identity that survives relayouts and remains unambiguous when
    different components expose fields with the same display name.
    """
    try:
        game_object = getattr(component, "game_object", None)
        object_id = int(getattr(game_object, "id", 0) or 0)
        component_id = int(getattr(component, "component_id", 0) or 0)
    except (AttributeError, RuntimeError, TypeError, ValueError):
        return ""
    if object_id <= 0 or component_id <= 0:
        return ""
    suffix = str(field_name or "").strip(".")
    base = f"inspector.object.{object_id}.component.{component_id}"
    return f"{base}.{suffix}" if suffix else base


def record_inspector_component_item(
    ctx: InxGUIContext,
    component,
    field_name: str,
    kind: str,
    label: str,
    *,
    enabled: bool = True,
) -> str:
    """Record the previously rendered widget under the Inspector contract."""
    if not semantic_capture_enabled(ctx):
        return ""
    semantic_id = inspector_component_semantic_id(component, field_name)
    recorder = getattr(ctx, "record_semantic_item", None)
    if semantic_id and callable(recorder):
        recorder(kind, label, bool(enabled), semantic_id)
    return semantic_id


# ═══════════════════════════════════════════════════════════════════════════
#  Label / layout primitives
# ═══════════════════════════════════════════════════════════════════════════

_LABEL_W_CACHE: dict = {}  # (tuple_of_labels,) -> float


def max_label_w(ctx: InxGUIContext, labels, *, min_width: float = 0.0) -> float:
    """Return the pixel width for the widest label + padding.

    Results are cached per unique label set (font is constant at runtime).
    """
    key = tuple(labels)
    cached = _LABEL_W_CACHE.get(key)
    if cached is not None:
        return cached
    _min = min_width if min_width > 0 else MIN_LABEL_WIDTH
    w = 0.0
    for lb in labels:
        tw = ctx.calc_text_width(lb)
        if tw > w:
            w = tw
    result = max(w + LABEL_PAD, _min)
    _LABEL_W_CACHE[key] = result
    return result


def field_label(ctx: InxGUIContext, label: str, width: float = 0.0):
    """Label on the left; the next widget fills the remaining row width.

    If *width* is 0 the column is auto-sized to this label (but never
    narrower than ``MIN_LABEL_WIDTH``).
    """
    if width <= 0.0:
        width = max(ctx.calc_text_width(label) + LABEL_PAD, MIN_LABEL_WIDTH)
    ctx.align_text_to_frame_padding()
    ctx.label(label)
    ctx.same_line(width)
    ctx.set_next_item_width(-1)


# ═══════════════════════════════════════════════════════════════════════════
#  Enum helpers (Python Enum + pybind11 enum compat)
# ═══════════════════════════════════════════════════════════════════════════

def get_enum_members(enum_cls):
    """Return member list for a Python Enum **or** pybind11 enum-like type."""
    if enum_cls is None:
        return []
    members_dict = getattr(enum_cls, "__members__", None)
    if isinstance(members_dict, dict):
        return list(members_dict.values())
    try:
        return list(enum_cls)
    except TypeError:
        pass
    return []


def get_enum_member_name(member) -> str:
    """Human-readable name of an enum member."""
    name = getattr(member, "name", None)
    return str(name) if name else str(member)


def get_enum_member_value(member):
    """Raw value of an enum member."""
    return member.value if hasattr(member, "value") else member


def find_enum_index(members, current_value) -> int:
    """Find the best matching member index for the given *current_value*."""
    if not members:
        return 0
    # 1. Direct equality
    for idx, member in enumerate(members):
        if member == current_value:
            return idx
    # 2. Compare raw values
    current_raw = get_enum_member_value(current_value)
    for idx, member in enumerate(members):
        if get_enum_member_value(member) == current_raw:
            return idx
    # 3. Compare as ints
    try:
        current_int = int(current_raw)
    except (ValueError, TypeError):
        current_int = None
    if current_int is not None:
        for idx, member in enumerate(members):
            try:
                if int(get_enum_member_value(member)) == current_int:
                    return idx
            except (ValueError, TypeError) as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                continue
    return 0


# ═══════════════════════════════════════════════════════════════════════════
#  Unified serialized-field renderer
# ═══════════════════════════════════════════════════════════════════════════

def _label_or_fullwidth(ctx, display_name, lw, has_visible_label):
    """Render field label or set full-width for the next item."""
    if has_visible_label:
        field_label(ctx, display_name, lw)
    else:
        ctx.set_next_item_width(-1)


def _render_numeric_sf(ctx, wid, display_name, metadata, current_value, lw, has_visible_label, is_float):
    """Render a FLOAT or INT inspector field."""
    _label_or_fullwidth(ctx, display_name, lw, has_visible_label)
    if is_float:
        speed = getattr(metadata, "drag_speed", None) or DRAG_SPEED_DEFAULT
        slider = getattr(metadata, "slider", False)
        if metadata.range:
            if slider:
                return ctx.float_slider(wid, float(current_value), metadata.range[0], metadata.range[1])
            return ctx.drag_float(wid, float(current_value), speed, metadata.range[0], metadata.range[1])
        return ctx.drag_float(wid, float(current_value), speed, -1e6, 1e6)
    else:
        speed = getattr(metadata, "drag_speed", None) or DRAG_SPEED_INT
        slider = getattr(metadata, "slider", False)
        if metadata.range:
            if slider:
                return int(ctx.int_slider(wid, int(current_value), metadata.range[0], metadata.range[1]))
            return int(ctx.drag_int(wid, int(current_value), speed, metadata.range[0], metadata.range[1]))
        return int(ctx.drag_int(wid, int(current_value), speed, -1000000, 1000000))


def _render_vec_sf(ctx, wid, current_value, lw, has_visible_label, vector_label, ft):
    """Render a VEC2, VEC3, or VEC4 inspector field."""
    from Infernux.components.serialized_field import FieldType
    vec_lw = lw if has_visible_label else 1.0
    if ft == FieldType.VEC2:
        x, y = (float(current_value.x), float(current_value.y)) if current_value is not None else (0.0, 0.0)
        nx, ny = ctx.vector2(vector_label, x, y, DRAG_SPEED_DEFAULT, vec_lw)
        if any(not float_close(a, b) for a, b in [(nx, x), (ny, y)]):
            from Infernux.lib import Vector2
            return Vector2(nx, ny)
    elif ft == FieldType.VEC3:
        if current_value is not None:
            x, y, z = float(current_value.x), float(current_value.y), float(current_value.z)
        else:
            x, y, z = 0.0, 0.0, 0.0
        nx, ny, nz = ctx.vector3(vector_label, x, y, z, DRAG_SPEED_DEFAULT, vec_lw)
        if any(not float_close(a, b) for a, b in [(nx, x), (ny, y), (nz, z)]):
            from Infernux.lib import Vector3
            return Vector3(nx, ny, nz)
    elif ft == FieldType.VEC4:
        if current_value is not None:
            x, y, z, w = float(current_value.x), float(current_value.y), float(current_value.z), float(current_value.w)
        else:
            x, y, z, w = 0.0, 0.0, 0.0, 0.0
        nx, ny, nz, nw = ctx.vector4(vector_label, x, y, z, w, DRAG_SPEED_DEFAULT, vec_lw)
        if any(not float_close(a, b) for a, b in [(nx, x), (ny, y), (nz, z), (nw, w)]):
            from Infernux.lib import vec4f
            return vec4f(nx, ny, nz, nw)
    return current_value


def _render_enum_sf(ctx, wid, display_name, metadata, current_value, lw, has_visible_label):
    """Render an ENUM inspector field."""
    enum_cls = metadata.enum_type
    if isinstance(enum_cls, str):
        import Infernux.lib as _lib
        enum_cls = getattr(_lib, enum_cls, None)
    if enum_cls is not None:
        members = get_enum_members(enum_cls)
        if not members:
            ctx.label(f"{display_name}: {current_value}")
            return current_value
        if (hasattr(metadata, "enum_labels")
                and metadata.enum_labels
                and len(metadata.enum_labels) == len(members)):
            member_names = metadata.enum_labels
        else:
            member_names = [get_enum_member_name(m) for m in members]
        current_idx = find_enum_index(members, current_value)
        _label_or_fullwidth(ctx, display_name, lw, has_visible_label)
        new_idx = ctx.combo(wid, current_idx, member_names, -1)
        if new_idx != current_idx:
            return members[new_idx]
    else:
        ctx.label(f"{display_name}: {current_value}")
    return current_value


def _render_color_sf(ctx, wid, display_name, metadata, current_value, lw, has_visible_label):
    """Render a COLOR inspector field."""
    if current_value is not None:
        r, g, b, a = current_value[0], current_value[1], current_value[2], current_value[3]
    else:
        r, g, b, a = 1.0, 1.0, 1.0, 1.0
    _label_or_fullwidth(ctx, display_name, lw, has_visible_label)
    allow_hdr = getattr(metadata, 'hdr', False)
    nr, ng, nb, na = _render_color_bar(ctx, wid, r, g, b, a, allow_hdr=allow_hdr)
    if (nr, ng, nb, na) != (r, g, b, a):
        return [nr, ng, nb, na]
    return current_value


def render_serialized_field(
    ctx: InxGUIContext,
    wid: str,
    display_name: str,
    metadata,
    current_value: Any,
    lw: float,
) -> Any:
    """Render an inspector field based on its ``FieldType`` metadata.

    Handles **scalar / value types** only:
    ``FLOAT``, ``INT``, ``BOOL``, ``STRING``, ``VEC2``, ``VEC3``, ``VEC4``,
    ``ENUM``, ``COLOR``.
    """
    from Infernux.components.serialized_field import FieldType

    ft = metadata.field_type
    has_visible_label = bool(display_name and str(display_name).strip())
    vector_label = display_name if has_visible_label else " "

    if ft == FieldType.FLOAT:
        return _render_numeric_sf(ctx, wid, display_name, metadata, current_value, lw, has_visible_label, True)
    if ft == FieldType.INT:
        return _render_numeric_sf(ctx, wid, display_name, metadata, current_value, lw, has_visible_label, False)
    if ft == FieldType.BOOL:
        return render_inspector_checkbox(ctx, display_name if has_visible_label else wid, bool(current_value))
    if ft == FieldType.STRING:
        _label_or_fullwidth(ctx, display_name, lw, has_visible_label)
        multiline = getattr(metadata, "multiline", False)
        if multiline:
            return ctx.input_text_multiline(wid, str(current_value) if current_value else "", buffer_size=4096, width=-1, height=80)
        return ctx.text_input(wid, str(current_value) if current_value else "", 256)
    if ft in (FieldType.VEC2, FieldType.VEC3, FieldType.VEC4):
        return _render_vec_sf(ctx, wid, current_value, lw, has_visible_label, vector_label, ft)
    if ft == FieldType.ENUM:
        return _render_enum_sf(ctx, wid, display_name, metadata, current_value, lw, has_visible_label)
    if ft == FieldType.COLOR:
        return _render_color_sf(ctx, wid, display_name, metadata, current_value, lw, has_visible_label)

    ctx.label(f"{display_name}: {current_value}")
    return current_value


# ═══════════════════════════════════════════════════════════════════════════
#  Change detection
# ═══════════════════════════════════════════════════════════════════════════

def has_field_changed(field_type, old_value, new_value) -> bool:
    """Check whether a serialized field value has actually changed.

    Uses tolerance comparison for floats and identity check for vec
    types (``render_serialized_field`` already creates a new vec object
    only when the value differs).
    """
    from Infernux.components.serialized_field import FieldType

    if field_type == FieldType.FLOAT:
        if isinstance(new_value, (int, float)) and isinstance(old_value, (int, float)):
            return not float_close(float(new_value), float(old_value))
    elif field_type in (FieldType.VEC2, FieldType.VEC3, FieldType.VEC4):
        return new_value is not old_value
    elif field_type == FieldType.COLOR:
        from Infernux.components.serialized_field import rgba_equal
        return not rgba_equal(old_value, new_value)
    return new_value != old_value


# ═══════════════════════════════════════════════════════════════════════════
#  Info-text / tooltip helper
# ═══════════════════════════════════════════════════════════════════════════

def render_info_text(ctx: InxGUIContext, text: str):
    """Render a dimmed, non-editable info line below a field."""
    ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
    ctx.label(f"  {text}")
    ctx.pop_style_color(1)


# ═══════════════════════════════════════════════════════════════════════════
#  Unity-style colour bar (shared by FieldType.COLOR and material ptype 7)
# ═══════════════════════════════════════════════════════════════════════════

_COLOR_BAR_H: float = 20.0

# Per-widget HDR state: wid -> {"enabled": bool, "intensity": float}
_hdr_state: dict = {}
# Live base (pre-HDR) colour while the popup is open: wid -> [r, g, b, a]
_color_popup_live: dict = {}
# First-frame guard to prevent click-through on popup open
_color_popup_guard: set = set()


def _render_color_popup(ctx, wid, popup_id, r, g, b, a, allow_hdr, first_frame_set):
    """Render the colour-edit popup contents. Return *(nr, ng, nb, na)*."""
    nr, ng, nb, na = r, g, b, a
    if ctx.begin_popup(popup_id):
        first_frame = wid in first_frame_set
        if first_frame:
            first_frame_set.discard(wid)

        state = _hdr_state.setdefault(wid, {"enabled": False, "intensity": 1.0})
        live = _color_popup_live.setdefault(wid, [r, g, b, a])
        lr, lg, lb, la = live[0], live[1], live[2], live[3]

        # AlphaBar = 1 << 18
        changed_pick, pick_r, pick_g, pick_b, pick_a = ctx.color_picker(
            f"{wid}_cpicker", float(lr), float(lg), float(lb), float(la), 1 << 18,
        )
        interaction = False
        if changed_pick and not first_frame:
            lr, lg, lb, la = pick_r, pick_g, pick_b, pick_a
            interaction = True

        ctx.separator()
        new_r = float(ctx.drag_float(f"R##{wid}_r", float(lr), 0.01, 0.0, 0.0))
        new_g = float(ctx.drag_float(f"G##{wid}_g", float(lg), 0.01, 0.0, 0.0))
        new_b = float(ctx.drag_float(f"B##{wid}_b", float(lb), 0.01, 0.0, 0.0))
        new_a = float(ctx.drag_float(f"A##{wid}_a", float(la), 0.01, 0.0, 1.0))

        if new_r != lr:
            lr = new_r; interaction = True
        if new_g != lg:
            lg = new_g; interaction = True
        if new_b != lb:
            lb = new_b; interaction = True
        if new_a != la:
            la = new_a; interaction = True

        if allow_hdr:
            old_en = state["enabled"]
            state["enabled"] = ctx.checkbox("HDR##_hdr_toggle", state["enabled"])
            if state["enabled"]:
                old_int = state["intensity"]
                state["intensity"] = ctx.drag_float(
                    "Intensity##_hdr_val", state["intensity"], 0.01, 0.0, 0.0,
                )
                if state["intensity"] != old_int:
                    interaction = True
            else:
                state["intensity"] = 1.0
            if state["enabled"] != old_en:
                interaction = True

        _color_popup_live[wid] = [lr, lg, lb, la]

        if interaction:
            nr, ng, nb, na = lr, lg, lb, la
            if allow_hdr and state["enabled"]:
                m = state["intensity"]
                nr, ng, nb = lr * m, lg * m, lb * m

        ctx.end_popup()
    else:
        _color_popup_live.pop(wid, None)
        first_frame_set.discard(wid)

    return nr, ng, nb, na


def _render_color_bar(
    ctx: InxGUIContext,
    wid: str,
    r: float, g: float, b: float, a: float,
    *,
    allow_hdr: bool = False,
) -> tuple:
    """Render a Unity-style colour bar and return ``(nr, ng, nb, na)``.

    The bar is a horizontal rectangle:
    * Top 3/4 — solid RGB colour (full opacity).
    * Bottom 1/4 — grey-scale representing alpha.

    Clicking the bar opens a colour-edit popup directly.

    When *allow_hdr* is True the popup also shows an HDR checkbox and
    intensity slider.  The returned RGBA values are multiplied by the
    HDR intensity when HDR is enabled.
    """
    avail_w = ctx.get_content_region_avail_width()
    clicked = ctx.invisible_button(f"{wid}_bar", avail_w, _COLOR_BAR_H)

    min_x = ctx.get_item_rect_min_x()
    min_y = ctx.get_item_rect_min_y()
    max_x = ctx.get_item_rect_max_x()
    max_y = ctx.get_item_rect_max_y()

    split_y = min_y + (max_y - min_y) * 0.75

    # Top 3/4: RGB at full opacity
    ctx.draw_filled_rect(min_x, min_y, max_x, split_y, r, g, b, 1.0)
    # Bottom 1/4: alpha as grey
    ctx.draw_filled_rect(min_x, split_y, max_x, max_y, a, a, a, 1.0)
    # Thin border
    ctx.draw_rect(min_x, min_y, max_x, max_y, *Theme.COLOR_SWATCH_BORDER, 1.0)

    popup_id = f"{wid}_cpop"
    if clicked:
        # Compute base (pre-HDR) colour for live state
        hdr = _hdr_state.get(wid)
        if hdr and hdr["enabled"] and hdr["intensity"] not in (0.0, 0):
            inv = 1.0 / hdr["intensity"]
            _color_popup_live[wid] = [r * inv, g * inv, b * inv, a]
        else:
            _color_popup_live[wid] = [r, g, b, a]
        _color_popup_guard.add(wid)
        ctx.open_popup(popup_id)

    nr, ng, nb, na = _render_color_popup(ctx, wid, popup_id, r, g, b, a,
                                          allow_hdr, _color_popup_guard)

    return nr, ng, nb, na


# ═══════════════════════════════════════════════════════════════════════════
#  Material property renderer — moved to inspector_material.py
#  Re-exported here for import convenience.
# ═══════════════════════════════════════════════════════════════════════════

def render_material_property(*args, **kwargs):
    from .inspector_material import render_material_property as _rmp
    return _rmp(*args, **kwargs)


# ═══════════════════════════════════════════════════════════════════════════
#  Component header (icon + collapsing header + enabled checkbox)
# ═══════════════════════════════════════════════════════════════════════════

def render_component_header(
    ctx: InxGUIContext,
    type_name: str,
    *,
    header_id: str = "",
    icon_id: int = 0,
    show_enabled: bool = True,
    is_enabled: bool = True,
    suffix: str = "",
    default_open: bool = True,
    force_open: bool = False,
) -> tuple:
    """Render a standard component / section header.

    Layout:  [▶ full-width bar:  icon → checkbox → display_name]

    The collapsing header spans the entire row, and the icon, checkbox,
    and label are overlaid via ``SameLine``.

    Returns ``(header_open: bool, new_enabled: bool)``.
    """
    from .theme import Theme

    display_name = format_display_name(f"{type_name}{suffix}")
    new_enabled = is_enabled

    # ── styling ──
    ctx.push_style_color(ImGuiCol.Header, *Theme.INSPECTOR_HEADER_PRIMARY)
    ctx.push_style_color(ImGuiCol.HeaderHovered, *Theme.INSPECTOR_HEADER_PRIMARY_HOVERED)
    ctx.push_style_color(ImGuiCol.HeaderActive, *Theme.INSPECTOR_HEADER_PRIMARY_ACTIVE)
    ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding, *Theme.INSPECTOR_HEADER_PRIMARY_FRAME_PAD)
    ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing, *Theme.INSPECTOR_HEADER_ITEM_SPC)
    ctx.push_style_var_float(ImGuiStyleVar.FrameBorderSize, Theme.INSPECTOR_HEADER_BORDER_SIZE)
    ctx.set_window_font_scale(Theme.INSPECTOR_HEADER_PRIMARY_FONT_SCALE)

    # ── full-width collapsing header (arrow + hidden label) ──
    if force_open:
        ctx.set_next_item_open(True, 0)          # 0 = ImGuiCond_Always
    elif default_open:
        ctx.set_next_item_open(True, Theme.COND_FIRST_USE_EVER)
    ctx.set_next_item_allow_overlap()
    header_key = header_id or type_name
    _clip_wx = ctx.get_window_pos_x()
    _clip_cx = ctx.get_cursor_pos_x()
    _clip_avail = ctx.get_content_region_avail_width()
    _clip_max_x = _clip_wx + _clip_cx + _clip_avail - Theme.INSPECTOR_HEADER_RIGHT_MARGIN
    ctx.push_draw_list_clip_rect(0.0, 0.0, _clip_max_x, 1e7, True)
    header_open = ctx.collapsing_header(f"##comp_{header_key}")
    ctx.pop_draw_list_clip_rect()
    header_height = max(0.0, ctx.get_item_rect_max_y() - ctx.get_item_rect_min_y())

    # ── overlay icon / checkbox / label on the same row ──
    indent = Theme.INSPECTOR_HEADER_CONTENT_INDENT
    ctx.same_line(indent, 0)

    if icon_id:
        icon_size = float(Theme.COMPONENT_ICON_SIZE)
        ctx.dummy(icon_size, max(header_height, icon_size))
        slot_min_x = ctx.get_item_rect_min_x()
        slot_min_y = ctx.get_item_rect_min_y()
        slot_max_x = ctx.get_item_rect_max_x()
        slot_max_y = ctx.get_item_rect_max_y()
        draw_size = min(icon_size, slot_max_x - slot_min_x, slot_max_y - slot_min_y)
        draw_x = slot_min_x + max(0.0, (slot_max_x - slot_min_x - draw_size) * 0.5)
        draw_y = slot_min_y + max(0.0, (slot_max_y - slot_min_y - draw_size) * 0.5)
        ctx.draw_image_rect(icon_id, draw_x, draw_y, draw_x + draw_size, draw_y + draw_size)
        ctx.same_line(0, Theme.INSPECTOR_HEADER_ITEM_SPC[0])

    if show_enabled:
        new_enabled = render_inspector_checkbox(ctx, "##hdr_en", is_enabled)
        ctx.same_line(0, Theme.INSPECTOR_HEADER_ITEM_SPC[0])

    ctx.align_text_to_frame_padding()
    ctx.label(display_name)

    # ── cleanup ──
    ctx.set_window_font_scale(1.0)
    ctx.pop_style_color(3)
    ctx.pop_style_var(3)

    return header_open, new_enabled


def render_inspector_checkbox(ctx: InxGUIContext, label: str, value: bool) -> bool:
    """Render a compact checkbox with the shared inspector sizing."""
    ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding, *Theme.INSPECTOR_CHECKBOX_FRAME_PAD)
    ctx.set_window_font_scale(Theme.INSPECTOR_CHECKBOX_FONT_SCALE)
    new_value = ctx.checkbox(label, value)
    ctx.set_window_font_scale(1.0)
    ctx.pop_style_var(1)
    return new_value


def render_compact_section_header(
    ctx: InxGUIContext,
    label: str,
    *,
    icon_id: int = 0,
    default_open: bool = True,
    text_color=None,
    level: str = "secondary",
    allow_overlap: bool = False,
) -> bool:
    """Render a compact framed tree header shared by inspector-style panels."""
    if default_open:
        ctx.set_next_item_open(True, Theme.COND_FIRST_USE_EVER)
    if allow_overlap:
        ctx.set_next_item_allow_overlap()

    if level == "primary":
        frame_pad = Theme.INSPECTOR_HEADER_PRIMARY_FRAME_PAD
        font_scale = Theme.INSPECTOR_HEADER_PRIMARY_FONT_SCALE
        base_color = Theme.INSPECTOR_HEADER_PRIMARY
        hover_color = Theme.INSPECTOR_HEADER_PRIMARY_HOVERED
        active_color = Theme.INSPECTOR_HEADER_PRIMARY_ACTIVE
    elif level == "list":
        frame_pad = Theme.INSPECTOR_HEADER_LIST_FRAME_PAD
        font_scale = Theme.INSPECTOR_HEADER_LIST_FONT_SCALE
        base_color = Theme.INSPECTOR_HEADER_LIST
        hover_color = Theme.INSPECTOR_HEADER_LIST_HOVERED
        active_color = Theme.INSPECTOR_HEADER_LIST_ACTIVE
    elif level == "tertiary":
        frame_pad = Theme.INSPECTOR_HEADER_TERTIARY_FRAME_PAD
        font_scale = Theme.INSPECTOR_HEADER_TERTIARY_FONT_SCALE
        base_color = Theme.INSPECTOR_HEADER_TERTIARY
        hover_color = Theme.INSPECTOR_HEADER_TERTIARY_HOVERED
        active_color = Theme.INSPECTOR_HEADER_TERTIARY_ACTIVE
    else:
        frame_pad = Theme.INSPECTOR_HEADER_SECONDARY_FRAME_PAD
        font_scale = Theme.INSPECTOR_HEADER_SECONDARY_FONT_SCALE
        base_color = Theme.INSPECTOR_HEADER_SECONDARY
        hover_color = Theme.INSPECTOR_HEADER_SECONDARY_HOVERED
        active_color = Theme.INSPECTOR_HEADER_SECONDARY_ACTIVE

    native_header = getattr(ctx, "render_compact_section_header", None)
    if callable(native_header):
        effective_text_color = text_color if text_color is not None else Theme.TEXT
        return bool(native_header(
            label,
            int(icon_id or 0),
            bool(default_open),
            Theme.COND_FIRST_USE_EVER,
            bool(allow_overlap),
            frame_pad[0],
            frame_pad[1],
            Theme.INSPECTOR_HEADER_ITEM_SPC[0],
            Theme.INSPECTOR_HEADER_ITEM_SPC[1],
            Theme.INSPECTOR_HEADER_BORDER_SIZE,
            level in ("secondary", "list", "tertiary"),
            font_scale,
            Theme.INSPECTOR_HEADER_RIGHT_MARGIN,
            Theme.COMPONENT_ICON_SIZE,
            base_color,
            hover_color,
            active_color,
            text_color is not None,
            effective_text_color,
        ))

    ctx.push_style_color(ImGuiCol.Header, *base_color)
    ctx.push_style_color(ImGuiCol.HeaderHovered, *hover_color)
    ctx.push_style_color(ImGuiCol.HeaderActive, *active_color)
    ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding, *frame_pad)
    ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing, *Theme.INSPECTOR_HEADER_ITEM_SPC)
    ctx.push_style_var_float(ImGuiStyleVar.FrameBorderSize, Theme.INSPECTOR_HEADER_BORDER_SIZE)
    if level in ("secondary", "list", "tertiary"):
        ctx.push_style_var_float(ImGuiStyleVar.IndentSpacing, 0.0)
    ctx.set_window_font_scale(font_scale)
    if text_color is not None:
        ctx.push_style_color(ImGuiCol.Text, *text_color)

    if icon_id:
        ctx.image(icon_id, Theme.COMPONENT_ICON_SIZE, Theme.COMPONENT_ICON_SIZE)
        ctx.same_line()

    _cs_wx = ctx.get_window_pos_x()
    _cs_cx = ctx.get_cursor_pos_x()
    _cs_avail = ctx.get_content_region_avail_width()
    _cs_max_x = _cs_wx + _cs_cx + _cs_avail - Theme.INSPECTOR_HEADER_RIGHT_MARGIN
    ctx.push_draw_list_clip_rect(0.0, 0.0, _cs_max_x, 1e7, True)
    header_open = ctx.collapsing_header(label)
    ctx.pop_draw_list_clip_rect()

    if text_color is not None:
        ctx.pop_style_color(1)
    ctx.set_window_font_scale(1.0)
    ctx.pop_style_color(3)
    ctx.pop_style_var(4 if level in ("secondary", "list", "tertiary") else 3)
    return header_open


def render_compact_section_title(
    ctx: InxGUIContext,
    label: str,
    *,
    level: str = "secondary",
    text_color=None,
) -> None:
    """Render a non-collapsible inspector subsection title."""
    if level == "primary":
        font_scale = Theme.INSPECTOR_HEADER_PRIMARY_FONT_SCALE
        default_color = Theme.TEXT
    else:
        font_scale = Theme.INSPECTOR_HEADER_SECONDARY_FONT_SCALE
        default_color = Theme.TEXT_DIM

    color = text_color if text_color is not None else default_color
    ctx.dummy(0.0, Theme.INSPECTOR_SECTION_GAP * 0.5)
    ctx.set_window_font_scale(font_scale)
    ctx.push_style_color(ImGuiCol.Text, *color)
    ctx.label(label)
    ctx.pop_style_color(1)
    ctx.set_window_font_scale(1.0)
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  Apply / Revert buttons for asset inspectors
# ═══════════════════════════════════════════════════════════════════════════

_COL_BUTTON = 21  # ImGuiCol.Button


def render_apply_revert(
    ctx: InxGUIContext,
    is_dirty: bool,
    on_apply,
    on_revert,
    semantic_prefix: str = "",
) -> None:
    """Render a unified Apply / Revert button bar for import-settings editors."""
    ctx.separator()
    if is_dirty:
        ctx.push_style_color(_COL_BUTTON, *Theme.APPLY_BUTTON)
        ctx.button("Apply", on_apply)
        if semantic_prefix and semantic_capture_enabled(ctx):
            ctx.record_semantic_item("button", "Apply", True, f"{semantic_prefix}.apply")
        ctx.pop_style_color(1)
        ctx.same_line()
        ctx.button("Revert", on_revert)
        if semantic_prefix and semantic_capture_enabled(ctx):
            ctx.record_semantic_item("button", "Revert", True, f"{semantic_prefix}.revert")
    else:
        ctx.begin_disabled(True)
        ctx.button("Apply", None)
        if semantic_prefix and semantic_capture_enabled(ctx):
            ctx.record_semantic_item("button", "Apply", False, f"{semantic_prefix}.apply")
        ctx.same_line()
        ctx.button("Revert", None)
        if semantic_prefix and semantic_capture_enabled(ctx):
            ctx.record_semantic_item("button", "Revert", False, f"{semantic_prefix}.revert")
        ctx.end_disabled()
    return None


# ═══════════════════════════════════════════════════════════════════════════
#  Batch property descriptor builder (for C++ RenderPropertyBatch)
# ═══════════════════════════════════════════════════════════════════════════

# Must match PropertyDesc::Type enum in InxGUIContext.h
PROP_FLOAT  = 0
PROP_INT    = 1
PROP_BOOL   = 2
PROP_STRING = 3
PROP_VEC2   = 4
PROP_VEC3   = 5
PROP_VEC4   = 6
PROP_ENUM   = 7
PROP_COLOR  = 8

_FIELD_TYPE_TO_PROP = None  # lazy init


def _ensure_prop_map():
    global _FIELD_TYPE_TO_PROP
    if _FIELD_TYPE_TO_PROP is not None:
        return
    from Infernux.components.serialized_field import FieldType
    _FIELD_TYPE_TO_PROP = {
        FieldType.FLOAT:  PROP_FLOAT,
        FieldType.INT:    PROP_INT,
        FieldType.BOOL:   PROP_BOOL,
        FieldType.STRING: PROP_STRING,
        FieldType.VEC2:   PROP_VEC2,
        FieldType.VEC3:   PROP_VEC3,
        FieldType.VEC4:   PROP_VEC4,
        FieldType.ENUM:   PROP_ENUM,
        # COLOR intentionally omitted — rendered via Python _render_color_bar
        # (Unity long-bar style, same as material ptype-7).  C++ PROP_COLOR uses
        # ImGui::ColorEdit4 (numeric fields + square swatch) and must not be
        # used for component serialized fields.
    }


def is_batch_renderable(field_type) -> bool:
    """Return True if this FieldType can be handled by C++ batch renderer."""
    from Infernux.components.serialized_field import FieldType
    _ensure_prop_map()
    if field_type == FieldType.COLOR:
        return False
    return field_type in _FIELD_TYPE_TO_PROP


_VEC_KEYS = ("f", "f2", "f3", "f4")
_VEC_ATTRS = ("x", "y", "z", "w")


def _pack_vec_components(desc, current_value, n, default=0.0):
    """Pack *n* vector components into *desc* using standard keys."""
    for i in range(n):
        if current_value is not None:
            desc[_VEC_KEYS[i]] = float(getattr(current_value, _VEC_ATTRS[i]))
        else:
            desc[_VEC_KEYS[i]] = default


def build_scalar_desc(
    wid: str,
    display_name: str,
    metadata,
    current_value,
    *,
    header_text: str = "",
    space_before: float = 0,
    mixed: bool = False,
    semantic_id: str = "",
) -> dict | None:
    """Build a single property descriptor dict for the C++ batch renderer.

    Returns None if the field type is not batch-renderable (references, lists, etc.).
    """
    _ensure_prop_map()
    prop_type = _FIELD_TYPE_TO_PROP.get(metadata.field_type)
    if prop_type is None:
        return None

    desc = {"t": prop_type, "w": wid, "n": display_name}
    if semantic_id:
        desc["sid"] = semantic_id
    if mixed:
        desc["mix"] = True

    # --- Value ---
    if prop_type == PROP_FLOAT:
        desc["f"] = float(current_value) if current_value is not None else 0.0
    elif prop_type == PROP_INT:
        desc["i"] = int(current_value) if current_value is not None else 0
    elif prop_type == PROP_BOOL:
        desc["b"] = bool(current_value) if current_value is not None else False
    elif prop_type == PROP_STRING:
        desc["s"] = str(current_value) if current_value else ""
    elif prop_type == PROP_VEC2:
        _pack_vec_components(desc, current_value, 2)
    elif prop_type == PROP_VEC3:
        _pack_vec_components(desc, current_value, 3)
    elif prop_type == PROP_VEC4:
        _pack_vec_components(desc, current_value, 4)
    elif prop_type == PROP_ENUM:
        enum_cls = metadata.enum_type
        if isinstance(enum_cls, str):
            import Infernux.lib as _lib
            enum_cls = getattr(_lib, enum_cls, None)
        if enum_cls is not None:
            members = get_enum_members(enum_cls)
            if hasattr(metadata, "enum_labels") and metadata.enum_labels and len(metadata.enum_labels) == len(members):
                desc["en"] = metadata.enum_labels
            else:
                desc["en"] = [get_enum_member_name(m) for m in members]
            desc["ei"] = find_enum_index(members, current_value)
        else:
            return None  # can't render without enum info
    # PROP_COLOR: only if COLOR is re-added to _FIELD_TYPE_TO_PROP (not used today).

    # --- Metadata ---
    if metadata.range:
        desc["mn"] = float(metadata.range[0])
        desc["mx"] = float(metadata.range[1])
    speed = getattr(metadata, "drag_speed", None)
    if speed is not None:
        desc["sp"] = float(speed)
    elif prop_type == PROP_INT:
        desc["sp"] = DRAG_SPEED_INT
    elif prop_type == PROP_FLOAT:
        desc["sp"] = DRAG_SPEED_DEFAULT
    if getattr(metadata, "slider", False):
        desc["sl"] = True
    if getattr(metadata, "multiline", False):
        desc["ml"] = True

    # --- Layout ---
    if header_text:
        desc["hdr"] = header_text
    if space_before > 0:
        desc["spc"] = space_before

    # --- Tooltip ---
    if metadata.tooltip:
        desc["tt"] = metadata.tooltip

    return desc
