"""
NodeGraphView — Reusable ImGui canvas for rendering and interacting
with a :class:`~Infernux.core.node_graph.NodeGraph`.

Handles:
- Background grid (scales with zoom)
- Node rendering (header + body + pins) with shadows
- Curved connection lines (cubic bezier) with arrow heads
- Canvas panning (middle-mouse drag)
- Scroll-wheel zoom (centred on cursor)
- Node dragging
- Connection creation by dragging from pin to pin
- Node / link selection with hover highlight
- Right-click context menu
- Minimap in bottom-right corner
- Drop targets on the canvas (accept any ImGui payload type; host ``on_canvas_drop`` filters)
- Keyboard delete for selected nodes / links
- Callbacks for graph mutations
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple, TYPE_CHECKING, Union

from Infernux.core.node_graph import (
    GraphLink,
    GraphNode,
    NodeGraph,
    NodeTypeDef,
    PinDef,
    PinKind,
)
from Infernux.engine.i18n import t
from Infernux.engine.ui.inspector_utils import preserve_ui_float_precision
from Infernux.engine.ui.theme import ImGuiStyleVar, ImGuiWindowFlags, Theme

if TYPE_CHECKING:
    from Infernux.lib import InxGUIContext


# ═══════════════════════════════════════════════════════════════════════════
# Visual constants (at zoom = 1.0)
# ═══════════════════════════════════════════════════════════════════════════

_GRID_SIZE = 20.0
_GRID_COLOR = (0.13, 0.13, 0.14, 1.0)
_GRID_COLOR2 = (0.18, 0.18, 0.19, 1.0)

_NODE_ROUNDING = 5.0
_NODE_BORDER_THICKNESS = 1.0
_NODE_HEADER_H = 30.0
_NODE_PIN_ROW_H = 22.0
_NODE_PAD_X = 10.0
_NODE_BODY_MIN_H = 10.0
_DETACHED_FIELD_ROW_H = 24.0
_PIN_RADIUS = 5.0
_PIN_HIT_RADIUS = 11.0
_HEADER_COLOR_SWATCH_SIZE = 14.0
_HEADER_COLOR_SWATCH_PAD = 7.0
_HEADER_COLOR_SWATCH_OUTLINE = (0.88, 0.89, 0.91, 0.92)

_LINK_THICKNESS = 2.0
_LINK_SEGMENTS = 28

# NASA-style: near-black panels, neutral gray; selection uses editor theme red
_BG_COLOR = (0.07, 0.07, 0.08, 1.0)
_NODE_BODY_COLOR = (0.13, 0.13, 0.14, 1.0)
_GRAPH_NODE_BODY_COLOR = (0.075, 0.075, 0.078, 0.98)
_GRAPH_NODE_HEADER_COLOR = (0.105, 0.105, 0.11, 1.0)
_GRAPH_NODE_CONTEXT_COLOR = (0.135, 0.135, 0.14, 1.0)
_NODE_SHADOW_COLOR = (0.0, 0.0, 0.0, 0.5)
_NODE_SELECTED_BORDER = Theme.APPLY_BUTTON
_NODE_BORDER_COLOR = (0.28, 0.28, 0.30, 1.0)
_PIN_HOVER_COLOR = (0.88, 0.88, 0.90, 0.75)

_LINK_DEFAULT_COLOR = (0.42, 0.42, 0.44, 0.88)
_LINK_SELECTED_COLOR = Theme.APPLY_BUTTON
_LINK_HOVER_COLOR = (0.55, 0.55, 0.58, 1.0)
_PENDING_LINK_COLOR = (0.65, 0.65, 0.68, 0.5)

_TEXT_COLOR = (0.90, 0.91, 0.92, 1.0)
_TEXT_DIM_COLOR = (0.52, 0.53, 0.55, 1.0)
_TEXT_BODY_COLOR = (0.62, 0.63, 0.65, 1.0)

_ZOOM_MIN = 0.3
_ZOOM_MAX = 2.5
_ZOOM_SPEED = 0.08

# Inline ImGui widgets inside nodes are laid out in child-window coordinates
# while node chrome is drawn straight to the draw list in screen space. The
# canvas child must therefore never scroll: any scroll offset would slide the
# widgets off their nodes. The wheel belongs to zoom.
_CANVAS_WINDOW_FLAGS = ImGuiWindowFlags.NoScrollbar | ImGuiWindowFlags.NoScrollWithMouse

# Base font size ImGui widgets are authored against, used to derive the
# per-zoom font scale for inline node fields.
_INLINE_BASE_FONT = 18.0
_INLINE_MIN_FONT = 9.0

# Absolute floor for draw-list node text so glyphs never collapse to nothing.
# Kept small on purpose: node text must stay proportional to the node box.
_TEXT_MIN_FONT = 5.0

_MINIMAP_SIZE = 120.0
_MINIMAP_PAD = 8.0
_MINIMAP_BG = (0.06, 0.06, 0.07, 0.75)
_MINIMAP_NODE = (0.38, 0.38, 0.40, 0.65)
_MINIMAP_VIEW = (
    Theme.APPLY_BUTTON[0],
    Theme.APPLY_BUTTON[1],
    Theme.APPLY_BUTTON[2],
    0.45,
)

# ImGuiKey constants (see imgui_keys.py)
_KEY_DELETE = 522
_KEY_C = 548
_KEY_V = 567
_IMGUI_MOD_CTRL = 1 << 12


# ═══════════════════════════════════════════════════════════════════════════
# Bezier helper
# ═══════════════════════════════════════════════════════════════════════════

def _bezier_points(
    x1: float, y1: float, x2: float, y2: float, segments: int = _LINK_SEGMENTS
) -> List[Tuple[float, float]]:
    dx = abs(x2 - x1) * 0.5
    dx = max(dx, 30.0)
    cx1, cy1 = x1 + dx, y1
    cx2, cy2 = x2 - dx, y2
    pts: List[Tuple[float, float]] = []
    for i in range(segments + 1):
        t = i / segments
        it = 1.0 - t
        px = it**3 * x1 + 3 * it**2 * t * cx1 + 3 * it * t**2 * cx2 + t**3 * x2
        py = it**3 * y1 + 3 * it**2 * t * cy1 + 3 * it * t**2 * cy2 + t**3 * y2
        pts.append((px, py))
    return pts


# ═══════════════════════════════════════════════════════════════════════════
# Cached layout per node
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class _PinLayout:
    pin_def: PinDef
    cx: float = 0.0
    cy: float = 0.0


@dataclass
class _NodeLayout:
    node: GraphNode
    typedef: NodeTypeDef
    sx: float = 0.0
    sy: float = 0.0
    w: float = 140.0
    h: float = 60.0
    input_pins: List[_PinLayout] = field(default_factory=list)
    output_pins: List[_PinLayout] = field(default_factory=list)


@dataclass(frozen=True)
class NodeCreationEntry:
    """One domain-provided item rendered by the shared creation palette."""

    key: str
    label: str
    category: str = ""
    type_id: str = ""


# ═══════════════════════════════════════════════════════════════════════════
# View
# ═══════════════════════════════════════════════════════════════════════════

class NodeGraphView:
    """Stateful widget that renders a :class:`NodeGraph` onto an ImGui canvas."""

    def __init__(self) -> None:
        self.graph: Optional[NodeGraph] = None
        # Hosts set this to a stable editor-specific namespace (for example
        # ``animfsm.graph``). It keeps generic canvas semantics unambiguous
        # when several graph editors are open in one Editor session.
        self.semantic_namespace: str = "node_graph"

        # Camera
        self.pan_x: float = 50.0
        self.pan_y: float = 50.0
        self.zoom: float = 1.0

        # Selection
        self.selected_nodes: List[str] = []
        self.selected_link: str = ""

        # In-progress connection drag
        self._dragging_pin: bool = False
        self._drag_src_node: str = ""
        self._drag_src_pin: str = ""
        self._drag_src_kind: PinKind = PinKind.OUTPUT
        self._drag_end_x: float = 0.0
        self._drag_end_y: float = 0.0

        # Node drag
        self._dragging_node: bool = False
        self._drag_node_id: str = ""

        # Canvas panning
        self._panning: bool = False

        # Canvas origin (screen coords)
        self._origin_x: float = 0.0
        self._origin_y: float = 0.0
        self._canvas_w: float = 0.0
        self._canvas_h: float = 0.0

        # Cached layouts
        self._layouts: Dict[str, _NodeLayout] = {}
        # Test doubles predating request-driven capture do not expose the
        # native property, so direct helper tests keep semantic behavior.
        self._semantic_capture_active: bool = True

        # Hovered link uid (for highlight)
        self._hovered_link: str = ""

        # Hovered pin: (node_uid, pin_id, pin_kind) or empty
        self._hovered_pin: Tuple[str, str, PinKind] = ("", "", PinKind.OUTPUT)

        # ── Callbacks ─────────────────────────────────────────────────
        self.on_link_created: Optional[Callable[[str, str, str, str], None]] = None
        self.on_link_deleted: Optional[Callable[[str], None]] = None
        self.on_link_replaced: Optional[
            Callable[[str, str, str, str, str], None]
        ] = None
        self.on_nodes_deleted: Optional[Callable[[List[str]], None]] = None
        self.on_node_add_request: Optional[Callable[[str, float, float], None]] = None
        self.on_node_creation_entries: Optional[
            Callable[[dict], List[NodeCreationEntry]]
        ] = None
        self.on_node_creation_selected: Optional[
            Callable[[NodeCreationEntry, dict], object]
        ] = None
        self.on_node_creation_requested: Optional[Callable[[dict], None]] = None
        # Pin drag released over empty canvas: (src_node, src_pin, src_kind, graph_x, graph_y).
        # Hosts can use this to pop a "create node and auto-connect" search menu.
        self.on_link_dropped_empty: Optional[Callable[[str, str, "PinKind", float, float], None]] = None
        self.on_node_selected: Optional[Callable[[str], None]] = None
        self.on_node_data_changed: Optional[Callable[[str, str, object, object], None]] = None
        # Called immediately before user-driven selection changes (click), so editors can snapshot undo.
        self.on_before_selection_change: Optional[Callable[[], None]] = None
        # Drop handler: (payload_type, payload path str or uint64 id, graph_x, graph_y)
        self.on_canvas_drop: Optional[Callable[[str, Union[str, int], float, float], None]] = None
        # If set, called on left-click on a node (after pins); return True to skip drag.
        self.on_node_primary_click: Optional[Callable[[str, float, float], bool]] = None
        self.on_node_drag_start: Optional[Callable[[str], None]] = None
        self.on_node_drag_end: Optional[Callable[[str], None]] = None
        self.on_node_header_color_changed: Optional[
            Callable[[str, Tuple[float, float, float, float], Tuple[float, float, float, float], bool], None]
        ] = None
        self.on_copy: Optional[Callable[[], None]] = None
        self.on_paste: Optional[Callable[[], None]] = None

        self._header_color_popup_node_uid: str = ""
        self._open_header_color_popup_next_frame: bool = False
        self._header_color_popup_initial_color: Optional[Tuple[float, float, float, float]] = None
        self._header_color_popup_changed: bool = False

        # Shared Blender-style node creation palette.  It serves both empty
        # canvas context actions and a connection dropped onto empty space.
        self._node_create_request: Optional[dict] = None
        self._node_create_search: str = ""
        self._node_create_focus: bool = False
        self._open_node_create_popup_next_frame: bool = False
        self._inline_control_hovered: bool = False
        self._inline_control_active: bool = False
        self._canvas_window_hovered: bool = False

        self._body_renderers: Dict[str, Callable] = {}

    def _notify_before_selection_change(self) -> None:
        if self.on_before_selection_change:
            self.on_before_selection_change()

    # ── Public API ────────────────────────────────────────────────────

    def reset_interaction_state(self) -> None:
        """Drop selection and transient gestures when the host replaces the graph."""
        self.selected_nodes.clear()
        self.selected_link = ""
        self._dragging_pin = False
        self._drag_src_node = ""
        self._drag_src_pin = ""
        self._dragging_node = False
        self._drag_node_id = ""
        self._panning = False
        self._layouts.clear()
        self._hovered_link = ""
        self._hovered_pin = ("", "", PinKind.OUTPUT)
        self._header_color_popup_node_uid = ""
        self._open_header_color_popup_next_frame = False
        self._header_color_popup_initial_color = None
        self._header_color_popup_changed = False
        self._node_create_request = None
        self._node_create_search = ""
        self._node_create_focus = False
        self._open_node_create_popup_next_frame = False
        self._inline_control_hovered = False
        self._inline_control_active = False
        self._canvas_window_hovered = False

    def register_body_renderer(self, type_id: str, renderer: Callable) -> None:
        self._body_renderers[type_id] = renderer

    def _semantic_id(self, suffix: str) -> str:
        root = str(self.semantic_namespace or "node_graph").strip(".") or "node_graph"
        return f"{root}.{suffix}" if suffix else root

    def _record_semantic_item(
        self, ctx, kind: str, label: str, enabled: bool, suffix: str, **values
    ) -> None:
        if not self._semantic_capture_active:
            return
        recorder = getattr(ctx, "record_semantic_item", None)
        if callable(recorder):
            recorder(kind, label, enabled, self._semantic_id(suffix), **values)

    def _record_semantic_rect(
        self,
        ctx,
        kind: str,
        label: str,
        enabled: bool,
        suffix: str,
        x: float,
        y: float,
        width: float,
        height: float,
    ) -> None:
        if not self._semantic_capture_active:
            return
        recorder = getattr(ctx, "record_semantic_rect", None)
        if callable(recorder):
            recorder(kind, label, x, y, width, height, enabled, self._semantic_id(suffix))

    def get_layout(self, uid: str) -> Optional[_NodeLayout]:
        """Return cached screen-space layout for *uid*, or None."""
        return self._layouts.get(uid)

    def screen_to_graph(self, sx: float, sy: float) -> Tuple[float, float]:
        gx = (sx - self._origin_x - self.pan_x) / self.zoom
        gy = (sy - self._origin_y - self.pan_y) / self.zoom
        return gx, gy

    def graph_to_screen(self, gx: float, gy: float) -> Tuple[float, float]:
        sx = self._origin_x + gx * self.zoom + self.pan_x
        sy = self._origin_y + gy * self.zoom + self.pan_y
        return sx, sy

    def center_on_nodes(self) -> None:
        if not self.graph or not self.graph.nodes:
            return
        bounds = []
        for node in self.graph.nodes:
            typedef = self.graph.get_type(node.type_id)
            if typedef is None:
                continue
            max_pins = max(len(typedef.input_pins()), len(typedef.output_pins()), 1)
            extra_pad = getattr(typedef, "body_bottom_pad", 0.0) or 0.0
            width = float(typedef.min_width)
            height = _NODE_HEADER_H + max_pins * _NODE_PIN_ROW_H + _NODE_BODY_MIN_H + extra_pad
            bounds.append((node.pos_x, node.pos_y, node.pos_x + width, node.pos_y + height))
        if not bounds:
            return

        min_x = min(value[0] for value in bounds)
        min_y = min(value[1] for value in bounds)
        max_x = max(value[2] for value in bounds)
        max_y = max(value[3] for value in bounds)
        bounds_w = max(max_x - min_x, 1.0)
        bounds_h = max(max_y - min_y, 1.0)
        padding = 32.0
        usable_w = max(self._canvas_w - padding * 2.0, 1.0)
        usable_h = max(self._canvas_h - padding * 2.0, 1.0)
        self.zoom = max(_ZOOM_MIN, min(1.0, usable_w / bounds_w, usable_h / bounds_h))
        self.pan_x = (self._canvas_w - bounds_w * self.zoom) * 0.5 - min_x * self.zoom
        self.pan_y = (self._canvas_h - bounds_h * self.zoom) * 0.5 - min_y * self.zoom

    # ── Main render ───────────────────────────────────────────────────

    def render(self, ctx: InxGUIContext) -> None:
        if self.graph is None:
            return

        canvas_w = ctx.get_content_region_avail_width()
        canvas_h = ctx.get_content_region_avail_height()
        if canvas_w < 1 or canvas_h < 1:
            return

        self._canvas_w = canvas_w
        self._canvas_h = canvas_h

        if not ctx.begin_child(
            "##node_graph_canvas", canvas_w, canvas_h, False, _CANVAS_WINDOW_FLAGS
        ):
            ctx.end_child()
            return

        # Inline node widgets are placed by absolute cursor position, which
        # inflates the child's content extent. Pin the scroll offset so their
        # child-space coordinates stay aligned with the draw-list node chrome.
        ctx.set_scroll_x(0.0)
        ctx.set_scroll_y(0.0)

        # Window-level hover, unlike the background item's hover, still reports
        # true while the cursor sits over an inline node widget — the wheel has
        # to keep zooming there. It goes false while a widget is being edited.
        self._canvas_window_hovered = bool(ctx.is_window_hovered())

        self._origin_x = ctx.get_window_pos_x()
        self._origin_y = ctx.get_window_pos_y()
        self._semantic_capture_active = bool(getattr(ctx, "semantic_capture_enabled", True))

        # Invisible button for mouse events
        ctx.set_cursor_pos_x(0)
        ctx.set_cursor_pos_y(0)
        # The canvas background owns panning/selection, but inline node fields
        # are submitted afterwards and must remain the topmost interactive
        # items for both human and synthetic pointer input.
        ctx.set_next_item_allow_overlap()
        ctx.invisible_button("##canvas_bg", canvas_w, canvas_h)
        self._record_semantic_item(ctx, "node_graph_canvas", "Node Graph", True, "canvas")
        canvas_hovered = ctx.is_item_hovered()

        # Clipping
        clip_x0 = self._origin_x
        clip_y0 = self._origin_y
        clip_x1 = self._origin_x + canvas_w
        clip_y1 = self._origin_y + canvas_h
        ctx.push_draw_list_clip_rect(clip_x0, clip_y0, clip_x1, clip_y1)

        # Background
        ctx.draw_filled_rect(clip_x0, clip_y0, clip_x1, clip_y1, *_BG_COLOR)

        # Grid
        self._draw_grid(ctx, clip_x0, clip_y0, clip_x1, clip_y1)

        # Compute node layouts
        self._compute_layouts()

        # Detect hovered pin (for highlight ring)
        mx_h = ctx.get_mouse_pos_x()
        my_h = ctx.get_mouse_pos_y()
        if self._dragging_pin:
            # During drag, highlight the nearest valid target pin
            self._hovered_pin = self._find_drag_target_pin(mx_h, my_h)
        else:
            h_node, h_pin, h_kind = self._hit_test_pin(mx_h, my_h)
            self._hovered_pin = (h_node, h_pin or "", h_kind)

        # Links (behind nodes)
        self._draw_links(ctx)

        # Nodes
        self._draw_nodes(ctx)

        # Real ImGui literal controls are overlaid after the draw-list nodes.
        # They remain part of the shared canvas, so every graph editor gets
        # the same inline-value behavior.
        self._inline_control_hovered = False
        self._inline_control_active = False
        self._draw_inline_fields(ctx)

        # Pending connection line
        if self._dragging_pin:
            self._draw_pending_link(ctx)

        # Minimap
        self._draw_minimap(ctx, clip_x0, clip_y0, clip_x1, clip_y1)

        # Zoom indicator
        if abs(self.zoom - 1.0) > 0.01:
            ctx.draw_text(
                clip_x0 + 8, clip_y1 - 22,
                f"{self.zoom * 100:.0f}%", 0.55, 0.55, 0.58, 0.8, 0.0,
            )

        ctx.pop_draw_list_clip_rect()

        # Handle interaction
        self._handle_interaction(ctx, canvas_hovered, canvas_w, canvas_h)

        # Header color popup
        self._draw_header_color_popup(ctx)

        # Drop targets on the canvas — accept any payload type; host panel decides what to do.
        if ctx.begin_drag_drop_target():
            tup = ctx.accept_any_drag_drop_payload()
            if tup is not None and self.on_canvas_drop:
                dtype, payload = tup
                mx = ctx.get_mouse_pos_x()
                my = ctx.get_mouse_pos_y()
                gx, gy = self.screen_to_graph(mx, my)
                self.on_canvas_drop(dtype, payload, gx, gy)
            ctx.end_drag_drop_target()

        # Context menu
        if ctx.begin_popup_context_window("##node_graph_ctx", 1):
            self._draw_context_menu(ctx)
            ctx.end_popup()

        self._draw_node_create_popup(ctx)

        ctx.end_child()

    # ── Grid ──────────────────────────────────────────────────────────

    def _draw_grid(self, ctx, x0, y0, x1, y1):
        step = _GRID_SIZE * self.zoom
        if step < 4.0:
            step = _GRID_SIZE * 5 * self.zoom
        ox = self.pan_x % step
        oy = self.pan_y % step

        alpha = min(1.0, self.zoom)
        col = (_GRID_COLOR[0], _GRID_COLOR[1], _GRID_COLOR[2], _GRID_COLOR[3] * alpha)
        x = x0 + ox
        while x < x1:
            ctx.draw_line(x, y0, x, y1, *col, 0.5)
            x += step
        y = y0 + oy
        while y < y1:
            ctx.draw_line(x0, y, x1, y, *col, 0.5)
            y += step

        big_step = step * 5
        big_ox = self.pan_x % big_step
        big_oy = self.pan_y % big_step
        x = x0 + big_ox
        while x < x1:
            ctx.draw_line(x, y0, x, y1, *_GRID_COLOR2, 1.0)
            x += big_step
        y = y0 + big_oy
        while y < y1:
            ctx.draw_line(x0, y, x1, y, *_GRID_COLOR2, 1.0)
            y += big_step

    # ── Layout ────────────────────────────────────────────────────────

    @staticmethod
    def _inline_field_is_hidden(node: GraphNode, field_def) -> bool:
        """Mirror the skip rules of :meth:`_draw_inline_fields`."""
        if str(field_def.data_type) == "asset_ref":
            return True
        return bool(field_def.visible_when_field) and (
            node.data.get(field_def.visible_when_field) != field_def.visible_when_value
        )

    def _detached_field_rows(self, node: GraphNode, typedef: NodeTypeDef) -> int:
        """Number of inline value rows drawn below the pin rows of *node*."""
        pin_ids = {pin.id for pin in typedef.input_pins()}
        return sum(
            1
            for field_def in typedef.inline_fields
            if field_def.id not in pin_ids
            and not self._inline_field_is_hidden(node, field_def)
        )

    def _compute_layouts(self) -> None:
        self._layouts.clear()
        graph = self.graph
        if graph is None:
            return

        z = self.zoom
        for node in graph.nodes:
            typedef = graph.get_type(node.type_id)
            if typedef is None:
                continue

            in_pins = typedef.input_pins()
            out_pins = typedef.output_pins()
            max_pins = max(len(in_pins), len(out_pins), 1)

            w = typedef.min_width * z
            if getattr(typedef, "inline_fields", ()):
                # Reserve exactly the rows the inline pass will draw. The
                # typedef's static pad also counts fields that visibility rules
                # hide, which left nodes with dead space below their content.
                extra_pad = (
                    self._detached_field_rows(node, typedef) * _DETACHED_FIELD_ROW_H
                )
            else:
                extra_pad = getattr(typedef, "body_bottom_pad", 0.0) or 0.0
            h = (_NODE_HEADER_H + max_pins * _NODE_PIN_ROW_H + _NODE_BODY_MIN_H + extra_pad) * z

            sx = self._origin_x + node.pos_x * z + self.pan_x
            sy = self._origin_y + node.pos_y * z + self.pan_y

            layout = _NodeLayout(node=node, typedef=typedef, sx=sx, sy=sy, w=w, h=h)

            hdr_h = _NODE_HEADER_H * z
            row_h = _NODE_PIN_ROW_H * z

            for i, pdef in enumerate(in_pins):
                cy = sy + hdr_h + i * row_h + row_h * 0.5
                layout.input_pins.append(_PinLayout(pin_def=pdef, cx=sx, cy=cy))

            for i, pdef in enumerate(out_pins):
                cy = sy + hdr_h + i * row_h + row_h * 0.5
                layout.output_pins.append(_PinLayout(pin_def=pdef, cx=sx + w, cy=cy))

            self._layouts[node.uid] = layout

    # ── Node drawing ──────────────────────────────────────────────────

    def _draw_nodes(self, ctx) -> None:
        for uid, layout in self._layouts.items():
            self._draw_one_node(ctx, layout)
            if not self._semantic_capture_active:
                continue
            label = str(layout.node.data.get("label", layout.typedef.label))
            center_x = layout.sx + layout.w * 0.5
            center_y = layout.sy + layout.h * 0.5
            center_enabled = self._node_drag_point_hits(uid, center_x, center_y)
            self._record_semantic_rect(
                ctx,
                "node_graph_node",
                label,
                center_enabled,
                f"node.{uid}",
                layout.sx,
                layout.sy,
                layout.w,
                layout.h,
            )
            drag_point = self._find_node_drag_semantic_point(uid, layout)
            drag_x, drag_y = drag_point or (center_x, center_y)
            handle_size = 8.0 * max(1.0, self.zoom)
            self._record_semantic_rect(
                ctx,
                "node_graph_node_drag_handle",
                f"{label} Drag",
                drag_point is not None,
                f"node.{uid}.drag",
                drag_x - handle_size * 0.5,
                drag_y - handle_size * 0.5,
                handle_size,
                handle_size,
            )

    def _node_drag_point_hits(self, uid: str, x: float, y: float) -> bool:
        pin_node, pin_id, _pin_kind = self._hit_test_pin(x, y)
        if pin_id is not None or pin_node:
            return False
        if layout := self._layouts.get(uid):
            if getattr(layout.typedef, "show_header_color_swatch", True) and self._hit_test_header_color_swatch(x, y):
                return False
        return self._hit_test_node(x, y) == uid

    def _find_node_drag_semantic_point(self, uid: str, layout: _NodeLayout) -> Optional[Tuple[float, float]]:
        sx, sy, w, h = layout.sx, layout.sy, layout.w, layout.h
        candidates = [
            (sx + w * 0.5, sy + h * 0.5),
            (sx + 6.0 * self.zoom, sy + h * 0.65),
            (sx + w - 6.0 * self.zoom, sy + h * 0.65),
            (sx + w * 0.25, sy + h * 0.75),
            (sx + w * 0.75, sy + h * 0.75),
            (sx + w * 0.5, sy + h * 0.85),
        ]
        for x, y in candidates:
            if self._node_drag_point_hits(uid, x, y):
                return x, y
        return None

    def _draw_one_node(self, ctx, layout: _NodeLayout) -> None:
        sx, sy, w, h = layout.sx, layout.sy, layout.w, layout.h
        z = self.zoom
        is_selected = layout.node.uid in self.selected_nodes
        rounding = _NODE_ROUNDING * z
        hdr_h = _NODE_HEADER_H * z
        pad_x = _NODE_PAD_X * z
        modern_graph = layout.typedef.visual_style in {"graph", "context"}

        # Shadow
        sh = 3.0 * z
        ctx.draw_filled_rect(
            sx + sh, sy + sh, sx + w + sh, sy + h + sh,
            *_NODE_SHADOW_COLOR, rounding,
        )

        # Body
        body_color = _GRAPH_NODE_BODY_COLOR if modern_graph else _NODE_BODY_COLOR
        ctx.draw_filled_rect(sx, sy, sx + w, sy + h, *body_color, rounding)

        # Header
        accent = self._resolve_node_header_color(layout)
        hdr = (
            _GRAPH_NODE_CONTEXT_COLOR
            if layout.typedef.visual_style == "context"
            else (_GRAPH_NODE_HEADER_COLOR if modern_graph else accent)
        )
        ctx.draw_filled_rect(sx, sy, sx + w, sy + hdr_h, *hdr, rounding)
        flat_h = min(rounding, hdr_h * 0.5)
        ctx.draw_filled_rect(sx, sy + hdr_h - flat_h, sx + w, sy + hdr_h, *hdr, 0)
        if modern_graph:
            accent_h = max(2.0, 3.0 * z)
            ctx.draw_filled_rect(
                sx, sy, sx + w, sy + accent_h, *accent, min(rounding, 3.0 * z)
            )

        # Header label
        label = layout.node.data.get("label", layout.typedef.label)
        font_sz = self._zoom_font(15.0)
        sw_x1, sw_y1, sw_x2, sw_y2 = self._node_header_swatch_rect(layout)
        label_right = (
            sw_x1 - 6.0 * z
            if layout.typedef.show_header_color_swatch
            else (
                sx + w * 0.60
                if layout.typedef.category_label
                else sx + w - pad_x
            )
        )
        ctx.draw_text_aligned(
            sx + pad_x, sy, max(sx + pad_x + 1.0, label_right), sy + hdr_h,
            label, *_TEXT_COLOR, 0.0, 0.5, font_sz, True,
        )

        if layout.typedef.show_header_color_swatch:
            ctx.draw_filled_rect(sw_x1, sw_y1, sw_x2, sw_y2, *accent, 2.0 * z)
            ctx.draw_rect(
                sw_x1,
                sw_y1,
                sw_x2,
                sw_y2,
                *_HEADER_COLOR_SWATCH_OUTLINE,
                max(1.0, 1.15 * z),
                2.0 * z,
            )
        elif layout.typedef.category_label:
            ctx.draw_text_aligned(
                sx + w * 0.56,
                sy,
                sx + w - pad_x,
                sy + hdr_h,
                layout.typedef.category_label,
                *_TEXT_DIM_COLOR,
                1.0,
                0.5,
                self._zoom_font(9.0),
                True,
            )

        # Subtitle (e.g. clip path)
        subtitle = layout.node.data.get("subtitle", "")
        if subtitle:
            body_top = sy + hdr_h + 2 * z
            ctx.draw_text_aligned(
                sx + pad_x, body_top, sx + w - pad_x, body_top + 16 * z,
                subtitle, *_TEXT_BODY_COLOR, 0.0, 0.0, self._zoom_font(10.0), True,
            )

        # Border — instant hover/selection feedback (no per-frame easing; cheap).
        if is_selected:
            ctx.draw_rect(sx, sy, sx + w, sy + h, *_NODE_SELECTED_BORDER, 2.5 * z, rounding)
        else:
            mx = ctx.get_mouse_pos_x()
            my = ctx.get_mouse_pos_y()
            hovered = (sx <= mx <= sx + w) and (sy <= my <= sy + h)
            if hovered:
                base = _NODE_BORDER_COLOR
                acc = _NODE_SELECTED_BORDER
                bcol = (
                    base[0] + (acc[0] - base[0]) * 0.5,
                    base[1] + (acc[1] - base[1]) * 0.5,
                    base[2] + (acc[2] - base[2]) * 0.5,
                    base[3] + (acc[3] - base[3]) * 0.5,
                )
                ctx.draw_rect(sx, sy, sx + w, sy + h, *bcol, _NODE_BORDER_THICKNESS * z, rounding)
            else:
                ctx.draw_rect(sx, sy, sx + w, sy + h, *_NODE_BORDER_COLOR, _NODE_BORDER_THICKNESS * z, rounding)

        # Pins
        pin_r = _PIN_RADIUS * z
        node_uid = layout.node.uid
        for pl in layout.input_pins:
            self._draw_pin(ctx, pl, PinKind.INPUT, pin_r, node_uid)
        for pl in layout.output_pins:
            self._draw_pin(ctx, pl, PinKind.OUTPUT, pin_r, node_uid)
        self._record_pin_semantics(ctx, layout, str(label))

        # Pin labels
        dim_font = self._zoom_font(12.5)
        row_h = _NODE_PIN_ROW_H * z
        for pl in layout.input_pins:
            ctx.draw_text_aligned(
                pl.cx + pin_r + 4 * z, pl.cy - row_h * 0.5,
                pl.cx + w * 0.45, pl.cy + row_h * 0.5,
                pl.pin_def.label, *_TEXT_DIM_COLOR, 0.0, 0.5, dim_font, True,
            )
        for pl in layout.output_pins:
            ctx.draw_text_aligned(
                sx + w * 0.55, pl.cy - row_h * 0.5,
                pl.cx - pin_r - 4 * z, pl.cy + row_h * 0.5,
                pl.pin_def.label, *_TEXT_DIM_COLOR, 1.0, 0.5, dim_font, True,
            )

        # Custom body renderer
        renderer = self._body_renderers.get(layout.typedef.type_id)
        if renderer:
            body_y = (sy + hdr_h
                      + max(len(layout.input_pins), len(layout.output_pins)) * row_h)
            renderer(ctx, layout.node, sx + pad_x, body_y, w - pad_x * 2)

    def _zoom_font(self, base: float) -> float:
        """Font size for node text authored at *base* px for zoom 1.0.

        Strictly proportional to zoom: a floor would keep glyphs at full size
        while the node shrinks, which is what made labels spill out of nodes.
        """
        return max(_TEXT_MIN_FONT, base * self.zoom)

    def _input_has_link(self, node_uid: str, pin_id: str) -> bool:
        graph = self.graph
        return bool(
            graph
            and any(
                link.target_node == node_uid and link.target_pin == pin_id
                for link in graph.links
            )
        )

    def _commit_inline_value(self, node: GraphNode, field_id: str, value) -> None:
        previous = copy.deepcopy(node.data.get(field_id))
        value = preserve_ui_float_precision(value, previous)
        if previous == value:
            return
        if self.on_node_data_changed is not None:
            self.on_node_data_changed(node.uid, field_id, previous, copy.deepcopy(value))
        else:
            node.data[field_id] = copy.deepcopy(value)

    def _draw_inline_fields(self, ctx) -> None:
        if self.graph is None or self.zoom < 0.68:
            return
        saved_x = ctx.get_cursor_pos_x()
        saved_y = ctx.get_cursor_pos_y()
        # Widgets are authored at zoom 1.0; scale the font (and the frame
        # metrics derived from it) so they shrink and grow with the node chrome
        # instead of spilling out of it.
        z = self.zoom
        ctx.set_window_font_scale(max(_INLINE_MIN_FONT / _INLINE_BASE_FONT, z))
        ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding, 4.0 * z, 2.0 * z)
        ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing, 4.0 * z, 3.0 * z)
        ctx.push_style_var_vec2(ImGuiStyleVar.ItemInnerSpacing, 3.0 * z, 3.0 * z)
        ctx.push_style_var_float(ImGuiStyleVar.FrameRounding, 3.0 * z)
        try:
            for layout in self._layouts.values():
                fields = getattr(layout.typedef, "inline_fields", ())
                if not fields:
                    continue
                # Skip nodes outside the visible canvas: their inline widgets
                # would otherwise be submitted at far-off cursor positions.
                if (
                    layout.sx + layout.w < self._origin_x
                    or layout.sx > self._origin_x + self._canvas_w
                    or layout.sy + layout.h < self._origin_y
                    or layout.sy > self._origin_y + self._canvas_h
                ):
                    continue
                input_rows = {
                    pin.pin_def.id: pin.cy for pin in layout.input_pins
                }
                detached_index = 0
                for field_def in fields:
                    if self._inline_field_is_hidden(layout.node, field_def):
                        continue
                    row_y = input_rows.get(field_def.id)
                    detached = row_y is None
                    if row_y is not None and self._input_has_link(
                        layout.node.uid, field_def.id
                    ):
                        continue
                    if row_y is None:
                        row_y = (
                            layout.sy
                            + _NODE_HEADER_H * self.zoom
                            + max(
                                len(layout.input_pins),
                                len(layout.output_pins),
                                1,
                            )
                            * _NODE_PIN_ROW_H
                            * self.zoom
                            + (detached_index + 0.5) * _DETACHED_FIELD_ROW_H * self.zoom
                        )
                        detached_index += 1
                    self._draw_inline_field(ctx, layout, field_def, row_y, detached=detached)
        finally:
            ctx.pop_style_var(4)
            ctx.set_window_font_scale(1.0)
            ctx.set_cursor_pos_x(saved_x)
            ctx.set_cursor_pos_y(saved_y)
            # SetCursorPos is used here only to restore the canvas layout after
            # overlaying real ImGui controls on draw-list nodes. ImGui requires
            # a submitted item after a cursor restore that can touch the child
            # boundary; otherwise EndChild reports a recoverable layout error.
            ctx.dummy(0.0, 0.0)

    def _note_inline_control_state(self, ctx) -> None:
        """Record hover/active state of the widget just submitted.

        Hover suppresses canvas clicks and node drags; only an *active* widget
        (a drag in progress, a focused text field) suppresses wheel zoom, so
        the wheel keeps zooming while merely pointing at a node value.
        """
        if ctx.is_item_active():
            self._inline_control_active = True
            self._inline_control_hovered = True
        elif ctx.is_item_hovered():
            self._inline_control_hovered = True

    def _draw_inline_field(
        self, ctx, layout: _NodeLayout, field_def, row_y: float, *, detached: bool = False
    ) -> None:
        value = copy.deepcopy(layout.node.data.get(field_def.id, field_def.default))
        local_x = layout.sx - self._origin_x
        local_y = row_y - self._origin_y - 9.0 * self.zoom
        field_x = local_x + layout.w * 0.42
        # Widths scale with the node so the control never crosses its right
        # edge; the minimum also scales, otherwise zoomed-out nodes overflow.
        field_w = max(24.0 * self.zoom, layout.w * 0.52 - _NODE_PAD_X * self.zoom)
        if detached:
            ctx.draw_text_aligned(
                layout.sx + _NODE_PAD_X * self.zoom,
                row_y - 10.0 * self.zoom,
                layout.sx + layout.w * 0.40,
                row_y + 10.0 * self.zoom,
                str(field_def.label),
                *_TEXT_DIM_COLOR,
                0.0,
                0.5,
                self._zoom_font(11.0),
                True,
            )
        ctx.set_cursor_pos_x(field_x)
        ctx.set_cursor_pos_y(local_y)
        ctx.push_id_str(f"inline_{layout.node.uid}_{field_def.id}")
        try:
            data_type = str(field_def.data_type)
            new_value = value
            if data_type == "bool":
                new_value = bool(ctx.checkbox("##value", bool(value)))
            elif data_type in {"i32", "u32"}:
                ctx.set_next_item_width(field_w)
                new_value = int(ctx.input_int("##value", int(value or 0)))
                if data_type == "u32":
                    new_value = max(0, new_value)
            elif data_type == "f32":
                ctx.set_next_item_width(field_w)
                new_value = float(
                    ctx.drag_float("##value", float(value or 0.0), 0.05, -1.0e7, 1.0e7)
                )
            elif data_type in {"vec2", "vec3", "vec4", "color"}:
                size = {"vec2": 2, "vec3": 3, "vec4": 4, "color": 4}[data_type]
                components = list(value or [0.0] * size)[:size]
                components += [0.0] * (size - len(components))
                gap = 3.0 * self.zoom
                component_w = max(14.0 * self.zoom, (field_w - (size - 1) * gap) / size)
                edited = []
                for index, component in enumerate(components):
                    slot_x = field_x + index * (component_w + gap)
                    axis_w = min(10.0 * self.zoom, component_w * 0.24)
                    ctx.draw_text_aligned(
                        self._origin_x + slot_x,
                        row_y - 9.0 * self.zoom,
                        self._origin_x + slot_x + axis_w,
                        row_y + 9.0 * self.zoom,
                        "XYZW"[index],
                        *_TEXT_DIM_COLOR,
                        0.0,
                        0.5,
                        self._zoom_font(9.0),
                        True,
                    )
                    ctx.set_cursor_pos_x(slot_x + axis_w)
                    ctx.set_cursor_pos_y(local_y)
                    ctx.set_next_item_width(max(10.0 * self.zoom, component_w - axis_w))
                    edited.append(
                        float(
                            ctx.drag_float(
                                f"##{index}",
                                float(component),
                                0.05,
                                -1.0e7,
                                1.0e7,
                            )
                        )
                    )
                    self._note_inline_control_state(ctx)
                new_value = edited
            elif field_def.enum_values:
                options = list(field_def.enum_values)
                index = options.index(value) if value in options else 0
                ctx.set_next_item_width(field_w)
                index = ctx.combo("##value", index, options, -1)
                new_value = options[max(0, min(index, len(options) - 1))]
            elif data_type == "string":
                ctx.set_next_item_width(field_w)
                new_value = ctx.text_input("##value", str(value or ""), 256)
            else:
                return
            self._note_inline_control_state(ctx)
            semantic_values = {}
            semantic_kind = "control"
            if data_type == "bool":
                semantic_kind = "checkbox"
                semantic_values["bool_value"] = bool(new_value)
            elif data_type in {"i32", "u32", "f32"}:
                semantic_kind =     "drag_float" if data_type == "f32" else "int_input"
                semantic_values["numeric_value"] = float(new_value)
            elif field_def.enum_values:
                semantic_kind = "combo"
                semantic_values["string_value"] = str(new_value)
            elif data_type == "string":
                semantic_kind = "text_input"
                semantic_values["string_value"] = str(new_value)
            self._record_semantic_item(
                ctx,
                semantic_kind,
                str(field_def.label),
                True,
                f"inline.{layout.node.uid}.{field_def.id}",
                **semantic_values,
            )
            self._commit_inline_value(layout.node, field_def.id, new_value)
        finally:
            ctx.pop_id()

    def _record_pin_semantics(self, ctx, layout: _NodeLayout, node_label: str) -> None:
        if not self._semantic_capture_active:
            return
        pin_hit_r = _PIN_HIT_RADIUS * self.zoom
        node_uid = layout.node.uid
        for pl in layout.input_pins:
            hit_node, hit_pin, hit_kind = self._hit_test_pin(pl.cx, pl.cy)
            enabled = hit_node == node_uid and hit_pin == pl.pin_def.id and hit_kind == PinKind.INPUT
            self._record_semantic_rect(
                ctx,
                "node_graph_port",
                f"{node_label} {pl.pin_def.label} Input",
                enabled,
                f"port.{node_uid}.input.{pl.pin_def.id}",
                pl.cx - pin_hit_r,
                pl.cy - pin_hit_r,
                pin_hit_r * 2.0,
                pin_hit_r * 2.0,
            )
        for pl in layout.output_pins:
            hit_node, hit_pin, hit_kind = self._hit_test_pin(pl.cx, pl.cy)
            enabled = hit_node == node_uid and hit_pin == pl.pin_def.id and hit_kind == PinKind.OUTPUT
            self._record_semantic_rect(
                ctx,
                "node_graph_port",
                f"{node_label} {pl.pin_def.label} Output",
                enabled,
                f"port.{node_uid}.output.{pl.pin_def.id}",
                pl.cx - pin_hit_r,
                pl.cy - pin_hit_r,
                pin_hit_r * 2.0,
                pin_hit_r * 2.0,
            )

    def _draw_pin(self, ctx, pl: _PinLayout, kind: PinKind, radius: float,
                   node_uid: str = "") -> None:
        color = pl.pin_def.color
        z = self.zoom
        connected = self._is_pin_connected(node_uid, pl.pin_def.id, kind == PinKind.OUTPUT)
        # Crisp dot: dark outline (matches the dark theme) + filled/hollow core.
        ctx.draw_filled_circle(pl.cx, pl.cy, radius + 1.2 * z, 0.08, 0.08, 0.09, 1.0)
        if connected:
            ctx.draw_filled_circle(pl.cx, pl.cy, radius, *color)
        else:
            ctx.draw_filled_circle(pl.cx, pl.cy, radius, 0.17, 0.17, 0.18, 1.0)
            ctx.draw_circle(pl.cx, pl.cy, radius, *color, 1.6 * z)
        # Hover / drag-target highlight ring
        hp_node, hp_pin, hp_kind = self._hovered_pin
        if (hp_pin and hp_pin == pl.pin_def.id and hp_kind == kind
                and hp_node == node_uid):
            ctx.draw_circle(pl.cx, pl.cy, radius + 3.0 * self.zoom,
                            *_PIN_HOVER_COLOR, 1.8 * self.zoom)

    def _is_pin_connected(self, node_uid: str, pin_id: str, is_output: bool) -> bool:
        if self.graph is None:
            return False
        for lk in self.graph.links:
            if is_output and lk.source_node == node_uid and lk.source_pin == pin_id:
                return True
            if not is_output and lk.target_node == node_uid and lk.target_pin == pin_id:
                return True
        return False

    # ── Link drawing ──────────────────────────────────────────────────

    def _draw_links(self, ctx) -> None:
        if self.graph is None:
            return

        # Pre-compute hovered link
        mx = ctx.get_mouse_pos_x()
        my = ctx.get_mouse_pos_y()
        self._hovered_link = self._hit_test_link(mx, my)

        for lk in self.graph.links:
            src_l = self._layouts.get(lk.source_node)
            dst_l = self._layouts.get(lk.target_node)
            if src_l is None or dst_l is None:
                continue

            sx2, sy2 = self._find_pin_pos(src_l, lk.source_pin, PinKind.OUTPUT)
            ex2, ey2 = self._find_pin_pos(dst_l, lk.target_pin, PinKind.INPUT)
            if sx2 is None or ex2 is None:
                continue

            is_sel = lk.uid == self.selected_link
            is_hov = lk.uid == self._hovered_link

            if is_sel:
                color, thick = _LINK_SELECTED_COLOR, 3.0 * self.zoom
            elif is_hov:
                color, thick = _LINK_HOVER_COLOR, 2.6 * self.zoom
            else:
                color, thick = _LINK_DEFAULT_COLOR, _LINK_THICKNESS * self.zoom

            self._draw_link_with_arrow(ctx, sx2, sy2, ex2, ey2, color, thick)
            if not self._semantic_capture_active:
                continue
            points = _bezier_points(sx2, sy2, ex2, ey2)
            hit_point = self._find_link_semantic_point(lk.uid, points)
            mid_x, mid_y = hit_point or points[len(points) // 2]
            source = self.graph.find_node(lk.source_node)
            target = self.graph.find_node(lk.target_node)
            source_label = str(source.data.get("label", lk.source_node)) if source else lk.source_node
            target_label = str(target.data.get("label", lk.target_node)) if target else lk.target_node
            link_hit_r = 7.0 * max(1.0, self.zoom)
            self._record_semantic_rect(
                ctx,
                "node_graph_link",
                f"{source_label} to {target_label}",
                hit_point is not None,
                f"link.{lk.uid}",
                mid_x - link_hit_r,
                mid_y - link_hit_r,
                link_hit_r * 2.0,
                link_hit_r * 2.0,
            )

    def _find_link_semantic_point(self, link_uid: str, points: List[Tuple[float, float]]) -> Optional[Tuple[float, float]]:
        if not points:
            return None
        midpoint = len(points) // 2
        indices = sorted(range(len(points)), key=lambda index: abs(index - midpoint))
        for index in indices:
            x, y = points[index]
            pin_node, pin_id, _pin_kind = self._hit_test_pin(x, y)
            if pin_id is not None or pin_node or self._hit_test_node(x, y):
                continue
            if self._hit_test_link(x, y) == link_uid:
                return x, y
        return None

    def _draw_pending_link(self, ctx) -> None:
        src_l = self._layouts.get(self._drag_src_node)
        if src_l is None:
            return
        sx2, sy2 = self._find_pin_pos(src_l, self._drag_src_pin, self._drag_src_kind)
        if sx2 is None:
            return
        self._draw_bezier(
            ctx, sx2, sy2, self._drag_end_x, self._drag_end_y,
            _PENDING_LINK_COLOR, 2.0 * self.zoom,
        )

    def _draw_bezier(self, ctx, x1, y1, x2, y2, color, thickness):
        pts = _bezier_points(x1, y1, x2, y2)
        for i in range(len(pts) - 1):
            ctx.draw_line(
                pts[i][0], pts[i][1], pts[i + 1][0], pts[i + 1][1],
                *color, thickness,
            )

    def _draw_link_with_arrow(self, ctx, x1, y1, x2, y2, color, thickness):
        """Bezier link trimmed before the target pin + a solid triangle arrowhead."""
        z = self.zoom
        pts = _bezier_points(x1, y1, x2, y2)
        if len(pts) < 2:
            return
        ex, ey = pts[-1]
        px, py = pts[-2]
        ang = math.atan2(ey - py, ex - px)
        dx, dy = math.cos(ang), math.sin(ang)
        gap = (_PIN_RADIUS + 3.0) * z          # tip sits just outside the input pin
        a_len = 13.0 * z
        a_half = 7.0 * z
        tipx, tipy = ex - dx * gap, ey - dy * gap
        basex, basey = tipx - dx * a_len, tipy - dy * a_len
        # Draw through the arrow base exactly. Distance-to-end trimming can
        # stop at the previous coarse Bezier sample and leave a visible gap.
        cutoff = gap + a_len
        for i in range(len(pts) - 1):
            ax, ay = pts[i]
            bx, by = pts[i + 1]
            if math.hypot(ex - bx, ey - by) <= cutoff:
                ctx.draw_line(ax, ay, basex, basey, *color, thickness)
                break
            ctx.draw_line(ax, ay, bx, by, *color, thickness)
        self._draw_filled_arrow(ctx, tipx, tipy, basex, basey, a_half, color)

    @staticmethod
    def _draw_filled_arrow(ctx, tipx, tipy, basex, basey, half_w, color):
        """Fill a triangle (tip→base) with perpendicular scanlines (no native tri prim)."""
        dx = tipx - basex
        dy = tipy - basey
        length = math.hypot(dx, dy) or 1.0
        ux, uy = dx / length, dy / length
        perx, pery = -uy, ux
        steps = max(6, int(length))
        for i in range(steps + 1):
            f = i / steps
            cx = basex + ux * length * f
            cy = basey + uy * length * f
            hw = half_w * (1.0 - f)
            ctx.draw_line(cx + perx * hw, cy + pery * hw,
                          cx - perx * hw, cy - pery * hw, *color, 2.0)
    def _resolve_node_header_color(self, layout: _NodeLayout) -> Tuple[float, float, float, float]:
        raw = layout.node.data.get("header_color", layout.typedef.header_color)
        return self._coerce_rgba(raw, layout.typedef.header_color)

    def _coerce_rgba(self, value, fallback) -> Tuple[float, float, float, float]:
        try:
            if isinstance(value, (list, tuple)) and len(value) >= 3:
                r = float(value[0])
                g = float(value[1])
                b = float(value[2])
                a = float(value[3]) if len(value) >= 4 else 1.0
                return (r, g, b, a)
        except Exception:
            pass
        if isinstance(fallback, (list, tuple)) and len(fallback) >= 3:
            a = float(fallback[3]) if len(fallback) >= 4 else 1.0
            return (float(fallback[0]), float(fallback[1]), float(fallback[2]), a)
        return (0.3, 0.3, 0.3, 1.0)

    def _node_header_swatch_rect(self, layout: _NodeLayout) -> Tuple[float, float, float, float]:
        z = self.zoom
        sx, sy, w = layout.sx, layout.sy, layout.w
        hdr_h = _NODE_HEADER_H * z
        sw = max(10.0 * z, _HEADER_COLOR_SWATCH_SIZE * z)
        pad = _HEADER_COLOR_SWATCH_PAD * z
        x2 = sx + w - pad
        x1 = x2 - sw
        right_half_min = sx + w * 0.5 + 2.0 * z
        if x1 < right_half_min:
            x1 = right_half_min
            x2 = x1 + sw
        y1 = sy + (hdr_h - sw) * 0.5
        y2 = y1 + sw
        return (x1, y1, x2, y2)

    def _hit_test_header_color_swatch(self, mx: float, my: float) -> str:
        for uid in reversed(list(self._layouts)):
            layout = self._layouts[uid]
            if not getattr(layout.typedef, "show_header_color_swatch", True):
                continue
            x1, y1, x2, y2 = self._node_header_swatch_rect(layout)
            if x1 <= mx <= x2 and y1 <= my <= y2:
                return uid
        return ""

    def _draw_header_color_popup(self, ctx) -> None:
        uid = self._header_color_popup_node_uid
        if not uid or self.graph is None:
            return

        popup_id = f"##node_header_color_popup_{uid}"
        if self._open_header_color_popup_next_frame:
            ctx.open_popup(popup_id)
            self._open_header_color_popup_next_frame = False

        node = self.graph.find_node(uid)
        layout = self._layouts.get(uid)
        if node is None or layout is None:
            self._header_color_popup_initial_color = None
            self._header_color_popup_changed = False
            self._header_color_popup_node_uid = ""
            return

        base = self._resolve_node_header_color(layout)
        if ctx.begin_popup(popup_id):
            changed, nr, ng, nb, na = ctx.color_picker(
                f"##node_header_color_picker_{uid}",
                float(base[0]),
                float(base[1]),
                float(base[2]),
                float(base[3]),
                1 << 18,
            )
            if changed:
                new_color = (float(nr), float(ng), float(nb), float(na))
                old_color = base
                if new_color != old_color:
                    self._header_color_popup_changed = True
                    if self.on_node_header_color_changed is not None:
                        self.on_node_header_color_changed(uid, old_color, new_color, False)
                    else:
                        node.data["header_color"] = new_color
            ctx.end_popup()
        else:
            if self._header_color_popup_changed:
                final_color = self._coerce_rgba(node.data.get("header_color", base), base)
                initial_color = self._header_color_popup_initial_color or final_color
                if initial_color != final_color and self.on_node_header_color_changed is not None:
                    self.on_node_header_color_changed(uid, initial_color, final_color, True)
            self._header_color_popup_initial_color = None
            self._header_color_popup_changed = False
            self._header_color_popup_node_uid = ""

    def _find_pin_pos(self, layout, pin_id, kind):
        pins = layout.output_pins if kind == PinKind.OUTPUT else layout.input_pins
        for pl in pins:
            if pl.pin_def.id == pin_id:
                return pl.cx, pl.cy
        return None, None

    # ── Minimap ───────────────────────────────────────────────────────

    def _draw_minimap(self, ctx, cx0, cy0, cx1, cy1):
        if not self.graph or not self.graph.nodes:
            return

        # Compute graph-space bounding box of all nodes
        nodes = self.graph.nodes
        n_x0 = min(n.pos_x for n in nodes)
        n_x1 = max(n.pos_x + 160 for n in nodes)
        n_y0 = min(n.pos_y for n in nodes)
        n_y1 = max(n.pos_y + 80 for n in nodes)

        # Compute visible graph-space viewport
        v_gx0 = -self.pan_x / self.zoom
        v_gy0 = -self.pan_y / self.zoom
        v_gx1 = v_gx0 + self._canvas_w / self.zoom
        v_gy1 = v_gy0 + self._canvas_h / self.zoom

        # Union of nodes and viewport — this is the total extent we must show
        total_x0 = min(n_x0, v_gx0) - 20
        total_y0 = min(n_y0, v_gy0) - 20
        total_x1 = max(n_x1, v_gx1) + 20
        total_y1 = max(n_y1, v_gy1) + 20
        total_w = max(total_x1 - total_x0, 1.0)
        total_h = max(total_y1 - total_y0, 1.0)

        # Auto-size minimap to aspect ratio (capped)
        aspect = total_w / total_h
        mm_w = _MINIMAP_SIZE
        mm_h = mm_w / max(aspect, 0.3)
        mm_h = min(mm_h, _MINIMAP_SIZE)
        mm_w = min(mm_w, mm_h * aspect) if aspect < 0.3 else mm_w

        mm_x = cx1 - mm_w - _MINIMAP_PAD
        mm_y = cy1 - mm_h - _MINIMAP_PAD

        # Background
        ctx.draw_filled_rect(mm_x, mm_y, mm_x + mm_w, mm_y + mm_h,
                             *_MINIMAP_BG, 4.0)

        # Clip minimap contents to its bounds
        ctx.push_draw_list_clip_rect(mm_x, mm_y, mm_x + mm_w, mm_y + mm_h)

        # Compute mapping: graph coords → minimap screen coords
        pad = 6.0
        inner_w = mm_w - pad * 2
        inner_h = mm_h - pad * 2
        sx = inner_w / total_w
        sy = inner_h / total_h
        s = min(sx, sy)
        off_x = mm_x + pad + (inner_w - total_w * s) * 0.5
        off_y = mm_y + pad + (inner_h - total_h * s) * 0.5

        # Draw node rectangles
        for n in nodes:
            nx = off_x + (n.pos_x - total_x0) * s
            ny = off_y + (n.pos_y - total_y0) * s
            nw = max(3, 120 * s)
            nh = max(2, 50 * s)
            ctx.draw_filled_rect(nx, ny, nx + nw, ny + nh, *_MINIMAP_NODE, 1.0)

        # Draw viewport rectangle
        vx0 = off_x + (v_gx0 - total_x0) * s
        vy0 = off_y + (v_gy0 - total_y0) * s
        vx1 = off_x + (v_gx1 - total_x0) * s
        vy1 = off_y + (v_gy1 - total_y0) * s
        ctx.draw_rect(vx0, vy0, vx1, vy1, *_MINIMAP_VIEW, 1.0, 2.0)

        ctx.pop_draw_list_clip_rect()

    # ── Interaction ───────────────────────────────────────────────────

    def _handle_interaction(self, ctx, canvas_hovered, canvas_w, canvas_h):
        mx = ctx.get_mouse_pos_x()
        my = ctx.get_mouse_pos_y()

        # Pin dragging
        if self._dragging_pin:
            self._drag_end_x = mx
            self._drag_end_y = my
            if not ctx.is_mouse_button_down(0):
                self._try_complete_link(mx, my)
                self._dragging_pin = False
            return

        # Node dragging (divide delta by zoom)
        if self._dragging_node:
            if ctx.is_mouse_button_down(0):
                dx = ctx.get_mouse_drag_delta_x(0)
                dy = ctx.get_mouse_drag_delta_y(0)
                node = self.graph.find_node(self._drag_node_id) if self.graph else None
                if node:
                    node.pos_x += dx / self.zoom
                    node.pos_y += dy / self.zoom
                ctx.reset_mouse_drag_delta(0)
            else:
                ended_id = self._drag_node_id
                self._dragging_node = False
                if self.on_node_drag_end:
                    self.on_node_drag_end(ended_id)
            return

        # Panning
        if self._panning:
            if ctx.is_mouse_button_down(2):
                dx = ctx.get_mouse_drag_delta_x(2)
                dy = ctx.get_mouse_drag_delta_y(2)
                self.pan_x += dx
                self.pan_y += dy
                ctx.reset_mouse_drag_delta(2)
            else:
                self._panning = False
            return

        # Zoom (scroll wheel, centred on cursor). Handled before the inline
        # widget guard so pointing at a node value still zooms, matching every
        # other node editor; only an in-progress edit keeps the wheel.
        if self._canvas_window_hovered and not self._inline_control_active:
            wheel = ctx.get_mouse_wheel_delta()
            if abs(wheel) > 0.01:
                old_zoom = self.zoom
                self.zoom = max(_ZOOM_MIN, min(_ZOOM_MAX, self.zoom + wheel * _ZOOM_SPEED))
                ratio = self.zoom / old_zoom
                self.pan_x = mx - self._origin_x - (mx - self._origin_x - self.pan_x) * ratio
                self.pan_y = my - self._origin_y - (my - self._origin_y - self.pan_y) * ratio

        if self._inline_control_hovered:
            return

        # Delete key — remove selected nodes or link even when the cursor
        # is no longer hovering the canvas after selection.
        if ctx.is_key_pressed(_KEY_DELETE):
            if self.selected_nodes:
                if self.on_nodes_deleted:
                    self.on_nodes_deleted(list(self.selected_nodes))
                elif self.graph:
                    for uid in list(self.selected_nodes):
                        self.graph.remove_node(uid)
                self.selected_nodes.clear()
                return
            if self.selected_link:
                link_uid = self.selected_link
                if self.on_link_deleted:
                    self.on_link_deleted(link_uid)
                elif self.graph:
                    self.graph.remove_link(link_uid)
                self.selected_link = ""
                return

        if canvas_hovered and ctx.is_key_down(_IMGUI_MOD_CTRL):
            if ctx.is_key_pressed(_KEY_C) and self.on_copy:
                self.on_copy()
            if ctx.is_key_pressed(_KEY_V) and self.on_paste:
                self.on_paste()

        if not canvas_hovered:
            return

        # Middle-mouse → panning
        if ctx.is_mouse_button_clicked(2):
            self._panning = True
            return

        # Right-click — select link under cursor for context menu
        if ctx.is_mouse_button_clicked(1):
            hit_lk = self._hit_test_link(mx, my)
            if hit_lk:
                self._notify_before_selection_change()
                self.selected_link = hit_lk
                self.selected_nodes.clear()
                if self.on_node_selected:
                    self.on_node_selected("")

        # Left click
        if ctx.is_mouse_button_clicked(0):
            # Header color swatch
            hit_color_uid = self._hit_test_header_color_swatch(mx, my)
            if hit_color_uid:
                self._notify_before_selection_change()
                self.selected_nodes = [hit_color_uid]
                self.selected_link = ""
                if self.on_node_selected:
                    self.on_node_selected(hit_color_uid)
                self._header_color_popup_node_uid = hit_color_uid
                layout = self._layouts.get(hit_color_uid)
                if layout is not None:
                    self._header_color_popup_initial_color = self._resolve_node_header_color(layout)
                else:
                    self._header_color_popup_initial_color = None
                self._header_color_popup_changed = False
                self._open_header_color_popup_next_frame = True
                return

            # Pins first
            hit_node, hit_pin, hit_kind = self._hit_test_pin(mx, my)
            if hit_pin is not None:
                # Drag-to-disconnect: if dragging from an input pin that
                # already has a link, detach and re-drag from the source end.
                if hit_kind == PinKind.INPUT and self.graph:
                    existing = self._find_link_to_input(hit_node, hit_pin)
                    if existing:
                        src_n, src_p = existing.source_node, existing.source_pin
                        if self.on_link_deleted:
                            self.on_link_deleted(existing.uid)
                        else:
                            self.graph.remove_link(existing.uid)
                        # Start a new drag from the output end
                        self._dragging_pin = True
                        self._drag_src_node = src_n
                        self._drag_src_pin = src_p
                        self._drag_src_kind = PinKind.OUTPUT
                        self._drag_end_x = mx
                        self._drag_end_y = my
                        return
                self._dragging_pin = True
                self._drag_src_node = hit_node
                self._drag_src_pin = hit_pin
                self._drag_src_kind = hit_kind
                self._drag_end_x = mx
                self._drag_end_y = my
                return

            # Nodes
            hit_uid = self._hit_test_node(mx, my)
            if hit_uid:
                self._notify_before_selection_change()
                self.selected_nodes = [hit_uid]
                self.selected_link = ""
                if self.on_node_selected:
                    self.on_node_selected(hit_uid)
                consumed = False
                if self.on_node_primary_click:
                    consumed = bool(self.on_node_primary_click(hit_uid, mx, my))
                if not consumed:
                    self._dragging_node = True
                    self._drag_node_id = hit_uid
                    if self.on_node_drag_start:
                        self.on_node_drag_start(hit_uid)
                return

            # Links
            hit_lk = self._hit_test_link(mx, my)
            if hit_lk:
                self._notify_before_selection_change()
                self.selected_link = hit_lk
                self.selected_nodes.clear()
                if self.on_node_selected:
                    self.on_node_selected("")
                return

            # Empty space — deselect
            self._notify_before_selection_change()
            self.selected_nodes.clear()
            self.selected_link = ""
            if self.on_node_selected:
                self.on_node_selected("")

    # ── Hit testing ───────────────────────────────────────────────────

    def _hit_test_pin(self, mx, my):
        hit_r = _PIN_HIT_RADIUS * self.zoom
        for uid, layout in self._layouts.items():
            for pl in layout.output_pins:
                if _dist(mx, my, pl.cx, pl.cy) <= hit_r:
                    return uid, pl.pin_def.id, PinKind.OUTPUT
            for pl in layout.input_pins:
                if _dist(mx, my, pl.cx, pl.cy) <= hit_r:
                    return uid, pl.pin_def.id, PinKind.INPUT
        return "", None, PinKind.OUTPUT

    def _hit_test_node(self, mx, my):
        for uid in reversed(list(self._layouts)):
            layout = self._layouts[uid]
            if (layout.sx <= mx <= layout.sx + layout.w
                    and layout.sy <= my <= layout.sy + layout.h):
                return uid
        return ""

    def _find_drag_target_pin(self, mx, my):
        """Find the nearest valid target pin during a link drag."""
        if self.graph is None:
            return "", "", PinKind.OUTPUT
        hit_r = _PIN_HIT_RADIUS * self.zoom
        want_kind = (PinKind.INPUT if self._drag_src_kind == PinKind.OUTPUT
                     else PinKind.OUTPUT)
        for uid, layout in self._layouts.items():
            if uid == self._drag_src_node:
                continue
            pins = (layout.input_pins if want_kind == PinKind.INPUT
                    else layout.output_pins)
            for pl in pins:
                if _dist(mx, my, pl.cx, pl.cy) <= hit_r:
                    if self._drag_src_kind == PinKind.OUTPUT:
                        endpoints = (
                            self._drag_src_node, self._drag_src_pin, uid, pl.pin_def.id
                        )
                    else:
                        endpoints = (
                            uid, pl.pin_def.id, self._drag_src_node, self._drag_src_pin
                        )
                    existing = self._replaceable_input_link(endpoints)
                    ignore_uid = existing.uid if existing is not None else ""
                    if self.graph.validate_link(
                        *endpoints, ignore_link_uid=ignore_uid
                    ):
                        return uid, pl.pin_def.id, want_kind
        return "", "", PinKind.OUTPUT

    def _hit_test_link(self, mx, my, threshold=6.0):
        if self.graph is None:
            return ""
        t_scaled = threshold * max(1.0, self.zoom)
        for lk in self.graph.links:
            src_l = self._layouts.get(lk.source_node)
            dst_l = self._layouts.get(lk.target_node)
            if not src_l or not dst_l:
                continue
            sx2, sy2 = self._find_pin_pos(src_l, lk.source_pin, PinKind.OUTPUT)
            ex2, ey2 = self._find_pin_pos(dst_l, lk.target_pin, PinKind.INPUT)
            if sx2 is None or ex2 is None:
                continue
            pts = _bezier_points(sx2, sy2, ex2, ey2, segments=12)
            for i in range(len(pts) - 1):
                if _point_segment_dist(mx, my, *pts[i], *pts[i + 1]) < t_scaled:
                    return lk.uid
        return ""

    def _find_link_to_input(self, node_uid: str, pin_id: str):
        """Find an existing link targeting the given input pin, or None."""
        if self.graph is None:
            return None
        for lk in self.graph.links:
            if lk.target_node == node_uid and lk.target_pin == pin_id:
                return lk
        return None

    def _replaceable_input_link(self, endpoints):
        if self.graph is None:
            return None
        existing = self._find_link_to_input(endpoints[2], endpoints[3])
        if existing is None:
            return None
        if self.on_link_replaced is not None:
            return existing
        if self.on_link_created is None and self.on_link_deleted is None:
            return existing
        return None

    def _try_complete_link(self, mx, my):
        target_node, target_pin, target_kind = self._hit_test_pin(mx, my)
        if target_pin is None:
            if self._drag_src_node and self._drag_src_pin:
                gx, gy = self.screen_to_graph(mx, my)
                if self.on_link_dropped_empty is not None:
                    self.on_link_dropped_empty(
                        self._drag_src_node,
                        self._drag_src_pin,
                        self._drag_src_kind,
                        gx,
                        gy,
                    )
                else:
                    self._request_node_creation(
                        gx,
                        gy,
                        self._drag_src_node,
                        self._drag_src_pin,
                        self._drag_src_kind,
                    )
            return
        if target_kind == self._drag_src_kind:
            return
        if target_node == self._drag_src_node:
            return
        if self._drag_src_kind == PinKind.OUTPUT:
            src_n, src_p = self._drag_src_node, self._drag_src_pin
            dst_n, dst_p = target_node, target_pin
        else:
            src_n, src_p = target_node, target_pin
            dst_n, dst_p = self._drag_src_node, self._drag_src_pin
        endpoints = (src_n, src_p, dst_n, dst_p)
        existing = self._replaceable_input_link(endpoints)
        ignore_uid = existing.uid if existing is not None else ""
        if self.graph is not None and not self.graph.validate_link(
            *endpoints, ignore_link_uid=ignore_uid
        ):
            return
        if existing is not None:
            if self.on_link_replaced:
                self.on_link_replaced(existing.uid, *endpoints)
            elif self.graph:
                self.graph.replace_link(existing.uid, *endpoints)
            return
        if self.on_link_created:
            self.on_link_created(*endpoints)
        elif self.graph:
            self.graph.add_link(*endpoints)

    # ── Context menu ──────────────────────────────────────────────────

    def _request_node_creation(
        self,
        gx: float,
        gy: float,
        source_node: str = "",
        source_pin: str = "",
        source_kind: PinKind = PinKind.OUTPUT,
    ) -> None:
        self._node_create_request = {
            "gx": float(gx),
            "gy": float(gy),
            "source_node": str(source_node),
            "source_pin": str(source_pin),
            "source_kind": PinKind(source_kind),
        }
        if self.on_node_creation_requested is not None:
            self.on_node_creation_requested(dict(self._node_create_request))
        self._node_create_search = ""
        self._node_create_focus = True
        self._open_node_create_popup_next_frame = True

    def _compatible_pin_for_type(self, typedef: NodeTypeDef, request: dict):
        if self.graph is None or not request.get("source_node"):
            return None
        source_kind = request["source_kind"]
        candidates = (
            typedef.input_pins()
            if source_kind == PinKind.OUTPUT
            else typedef.output_pins()
        )
        for pin in candidates:
            # The graph cannot validate a node that does not exist yet.  Pin
            # category/type checks provide the palette filter; full domain
            # validation runs immediately after creation.
            source_node = self.graph.find_node(request["source_node"])
            source_def = self.graph.get_type(source_node.type_id) if source_node else None
            source_pin = (
                next(
                    (item for item in source_def.pins if item.id == request["source_pin"]),
                    None,
                )
                if source_def
                else None
            )
            if source_pin is None:
                return None
            if source_pin.pin_category != pin.pin_category:
                continue
            if (
                source_pin.data_type not in {"", "any"}
                and pin.data_type not in {"", "any"}
                and source_pin.data_type != pin.data_type
                and not (
                    source_pin.data_type in {"i32", "u32"}
                    and pin.data_type == "f32"
                    and source_kind == PinKind.OUTPUT
                )
            ):
                continue
            return pin
        return None

    def _default_creation_entries(self, request: dict) -> List[NodeCreationEntry]:
        if self.graph is None:
            return []
        entries: List[NodeCreationEntry] = []
        for typedef in self.graph.registered_types():
            if not typedef.deletable:
                continue
            if request.get("source_node") and self._compatible_pin_for_type(
                typedef, request
            ) is None:
                continue
            entries.append(
                NodeCreationEntry(
                    key=typedef.type_id,
                    label=typedef.label,
                    category=(
                        typedef.category_label
                        or typedef.type_id.split(".", 1)[0].upper()
                    ),
                    type_id=typedef.type_id,
                )
            )
        return entries

    def _creation_entries(self, request: dict) -> List[NodeCreationEntry]:
        if self.on_node_creation_entries is None:
            return self._default_creation_entries(request)
        return list(self.on_node_creation_entries(dict(request)) or ())

    def _create_from_palette(self, entry: NodeCreationEntry, request: dict) -> None:
        if self.graph is None:
            return
        if self.on_node_creation_selected is not None:
            self.on_node_creation_selected(entry, dict(request))
            return
        typedef = self.graph.get_type(entry.type_id)
        if typedef is None:
            return
        before = {node.uid for node in self.graph.nodes}
        result = None
        if self.on_node_add_request:
            result = self.on_node_add_request(
                typedef.type_id, request["gx"], request["gy"]
            )
        else:
            result = self.graph.add_node(
                typedef.type_id, request["gx"], request["gy"]
            )
        node_uid = getattr(result, "uid", result if isinstance(result, str) else "")
        if not node_uid:
            created = [node for node in self.graph.nodes if node.uid not in before]
            if created:
                node_uid = created[-1].uid
        if not node_uid or not request.get("source_node"):
            return
        pin = self._compatible_pin_for_type(typedef, request)
        if pin is None:
            return
        if request["source_kind"] == PinKind.OUTPUT:
            endpoints = (
                request["source_node"],
                request["source_pin"],
                node_uid,
                pin.id,
            )
        else:
            endpoints = (
                node_uid,
                pin.id,
                request["source_node"],
                request["source_pin"],
            )
        if not self.graph.validate_link(*endpoints):
            return
        if self.on_link_created:
            self.on_link_created(*endpoints)
        else:
            self.graph.add_link(*endpoints)

    def _draw_node_create_popup(self, ctx) -> None:
        popup_id = "##shared_node_create_palette"
        if self._open_node_create_popup_next_frame:
            ctx.open_popup(popup_id)
            self._open_node_create_popup_next_frame = False
        request = self._node_create_request
        if request is None or self.graph is None:
            return
        selected: Optional[NodeCreationEntry] = None
        if ctx.begin_popup(popup_id):
            if self._node_create_focus:
                ctx.set_keyboard_focus_here()
                self._node_create_focus = False
            self._node_create_search = ctx.input_text_with_hint(
                "##node_create_search",
                t("node_graph.search_nodes"),
                self._node_create_search,
                160,
            )
            query = self._node_create_search.strip().casefold()
            grouped: Dict[str, List[NodeCreationEntry]] = {}
            for entry in self._creation_entries(request):
                haystack = (
                    f"{entry.label} {entry.key} {entry.type_id} {entry.category}"
                    .casefold()
                )
                if query and query not in haystack:
                    continue
                grouped.setdefault(entry.category or "NODE", []).append(entry)
            for category in sorted(grouped):
                ctx.label(category)
                for entry in sorted(
                    grouped[category], key=lambda item: item.label.casefold()
                ):
                    entry_selected = ctx.selectable(entry.label, False)
                    self._record_semantic_item(
                        ctx,
                        "selectable",
                        entry.label,
                        True,
                        f"create.{entry.type_id}",
                        string_value=entry.type_id,
                    )
                    if entry_selected:
                        selected = entry
                        ctx.close_current_popup()
            ctx.end_popup()
        else:
            self._node_create_request = None
        if selected is not None:
            request = dict(request)
            self._node_create_request = None
            self._create_from_palette(selected, request)

    def _draw_context_menu(self, ctx) -> None:
        mx = ctx.get_mouse_pos_x()
        my = ctx.get_mouse_pos_y()
        gx, gy = self.screen_to_graph(mx, my)

        if self.graph is None:
            return

        add_label = t("node_graph.add_node")
        add_requested = ctx.menu_item(add_label, "", False, True)
        self._record_semantic_item(
            ctx, "menu_item", add_label, True, "context.add_node", bool_value=True
        )
        if add_requested:
            self._request_node_creation(gx, gy)

        # Delete selected nodes
        if self.selected_nodes:
            label = f"Delete Node ({len(self.selected_nodes)})"
            deleted = ctx.menu_item(label, "", False, True)
            self._record_semantic_item(ctx, "menu_item", label, True, "context.delete_nodes")
            if deleted:
                if self.on_nodes_deleted:
                    self.on_nodes_deleted(list(self.selected_nodes))
                else:
                    for uid in self.selected_nodes:
                        self.graph.remove_node(uid)
                self.selected_nodes.clear()

        # Delete selected link
        if self.selected_link:
            deleted = ctx.menu_item("Delete Link", "", False, True)
            self._record_semantic_item(ctx, "menu_item", "Delete Link", True, "context.delete_link")
            if deleted:
                if self.on_link_deleted:
                    self.on_link_deleted(self.selected_link)
                else:
                    self.graph.remove_link(self.selected_link)
                self.selected_link = ""

        ctx.separator()
        centered = ctx.menu_item("Center View", "", False, True)
        self._record_semantic_item(ctx, "menu_item", "Center View", True, "context.center_view")
        if centered:
            self.center_on_nodes()
        reset_zoom = ctx.menu_item("Reset Zoom", "", False, True)
        self._record_semantic_item(ctx, "menu_item", "Reset Zoom", True, "context.reset_zoom")
        if reset_zoom:
            self.zoom = 1.0


# ═══════════════════════════════════════════════════════════════════════════
# Geometry helpers
# ═══════════════════════════════════════════════════════════════════════════

def _dist(x1, y1, x2, y2):
    return math.hypot(x2 - x1, y2 - y1)


def _point_segment_dist(px, py, ax, ay, bx, by):
    dx = bx - ax
    dy = by - ay
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-8:
        return _dist(px, py, ax, ay)
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
    return _dist(px, py, ax + t * dx, ay + t * dy)
