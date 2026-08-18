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
- Callbacks for graph mutations
"""

from __future__ import annotations

import copy
import math
import os
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
from Infernux.engine.interaction.context_menus import (
    ContextMenuBuilder,
    ContextMenuCommand,
    ResolvedContextMenuCommand,
)
from Infernux.engine.interaction.search import SearchQueryModel
from Infernux.engine.i18n import t
from Infernux.engine.ui.imgui_keys import KEY_LEFT_CTRL, KEY_RIGHT_CTRL, KEY_SPACE
from Infernux.engine.ui.inspector_utils import (
    preserve_ui_float_precision,
    render_color_value_bar,
)
from Infernux.engine.ui.theme import ImGuiCol, ImGuiStyleVar, ImGuiWindowFlags, Theme

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
# Typography is locked to 18px for every on-node label (title, pins, A/B/X…).
_NODE_FONT = 18.0
_NODE_HEADER_H = 30.0
_CONTEXT_HEADER_H = 30.0
_NODE_PIN_ROW_H = 24.0
_NODE_PAD_X = 12.0
_NODE_BODY_MIN_H = 12.0
_CONTEXT_BODY_MIN_H = 32.0
_DETACHED_FIELD_ROW_H = 24.0
_PIN_RADIUS = 5.0
_PIN_HIT_RADIUS = 12.0
# Compact inline value box hugging an unconnected input pin (Unity style):
# keeps numeric inputs from inflating the node body width.
_PIN_VALUE_HUG_GAP = 7.0
_PIN_VALUE_HUG_W = 96.0
_PIN_VEC_HUG_W = 150.0
# Asset-valued input pin box (texture / mesh / material slots).
_PIN_ASSET_HUG_W = 180.0
# Gap between vector component slots (X / Y / Z spacing).
_PIN_VEC_COMPONENT_GAP = 6.0
# Unified background for the whole pin value box (vector components share one
# capsule instead of three separate frames).
_PIN_VALUE_BG = (0.11, 0.11, 0.12, 0.92)
_PIN_VALUE_BG_ROUNDING = 3.0
# Vertical inset of the value box against the pin row height; smaller = taller.
_PIN_VALUE_BOX_INSET = 3.0
# Font scale for the compact pin value widgets (relative to the node font).
_PIN_VALUE_FONT_SCALE = 0.82
_HEADER_COLOR_SWATCH_SIZE = 16.0
_HEADER_COLOR_SWATCH_PAD = 8.0
_HEADER_COLOR_SWATCH_OUTLINE = (0.88, 0.89, 0.91, 0.92)

_LINK_THICKNESS = 2.0
_LINK_SEGMENTS = 28

# NASA-style: near-black panels, neutral gray; selection uses editor theme red
_BG_COLOR = (0.07, 0.07, 0.08, 1.0)
_NODE_BODY_COLOR = (0.13, 0.13, 0.14, 1.0)
_GRAPH_NODE_BODY_COLOR = (0.075, 0.075, 0.078, 0.98)
_GRAPH_NODE_HEADER_COLOR = (0.105, 0.105, 0.11, 1.0)
# Unity VFX-style context: saturated header, recessed body slot.
_GRAPH_NODE_CONTEXT_BODY = (0.055, 0.055, 0.058, 0.98)
_GRAPH_NODE_CONTEXT_SLOT = (0.085, 0.085, 0.090, 0.95)
_NODE_SHADOW_COLOR = (0.0, 0.0, 0.0, 0.5)
_NODE_SELECTED_BORDER = Theme.APPLY_BUTTON
_NODE_BORDER_COLOR = (0.28, 0.28, 0.30, 1.0)
_PIN_HOVER_COLOR = (0.88, 0.88, 0.90, 0.75)

_LINK_DEFAULT_COLOR = (0.42, 0.42, 0.44, 0.88)
_LINK_SELECTED_COLOR = Theme.APPLY_BUTTON
_LINK_HOVER_COLOR = (0.55, 0.55, 0.58, 1.0)
_PENDING_LINK_COLOR = (0.65, 0.65, 0.68, 0.5)

_NODE_GRAPH_EDIT_CONTEXT_MENU = (
    ContextMenuCommand("edit.copy", semantic_id="context.edit.copy"),
    ContextMenuCommand("edit.cut", semantic_id="context.edit.cut"),
    ContextMenuCommand("edit.paste", semantic_id="context.edit.paste"),
    ContextMenuCommand("edit.duplicate", semantic_id="context.edit.duplicate"),
    ContextMenuCommand("edit.delete", semantic_id="context.edit.delete"),
    ContextMenuCommand(
        "graph.center_view",
        separator_before=True,
        semantic_id="context.center_view",
    ),
    ContextMenuCommand(
        "graph.reset_zoom",
        semantic_id="context.reset_zoom",
    ),
)
_NODE_GRAPH_CONTEXT_MENU_BUILDER = ContextMenuBuilder()

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

# Inline ImGui widgets share the same 18px face as draw-list node text.
_INLINE_BASE_FONT = _NODE_FONT
_INLINE_FONT_SCALE = 1.0

# Absolute floor for draw-list / widget text so glyphs never collapse to nothing.
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
    return [_bezier_point(x1, y1, cx1, cy1, cx2, cy2, x2, y2, i / segments)
            for i in range(segments + 1)]


def _bezier_point(
    x1: float, y1: float, cx1: float, cy1: float,
    cx2: float, cy2: float, x2: float, y2: float, t: float,
) -> Tuple[float, float]:
    it = 1.0 - t
    return (
        it**3 * x1 + 3 * it**2 * t * cx1 + 3 * it * t**2 * cx2 + t**3 * x2,
        it**3 * y1 + 3 * it**2 * t * cy1 + 3 * it * t**2 * cy2 + t**3 * y2,
    )


def _bezier_tangent(
    x1: float, y1: float, cx1: float, cy1: float,
    cx2: float, cy2: float, x2: float, y2: float, t: float,
) -> Tuple[float, float]:
    it = 1.0 - t
    return (
        3 * it**2 * (cx1 - x1) + 6 * it * t * (cx2 - cx1) + 3 * t**2 * (x2 - cx2),
        3 * it**2 * (cy1 - y1) + 6 * it * t * (cy2 - cy1) + 3 * t**2 * (y2 - cy2),
    )


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
    # Shared canvas pad so hanging pin value boxes stay in the clip region.
    # Must not depend on which pins are currently wired.
    left_reserve: float = 0.0


@dataclass(frozen=True)
class NodeCreationEntry:
    """One domain-provided item rendered by the shared creation palette."""

    key: str
    label: str
    category: str = ""
    type_id: str = ""
    enabled: bool = True
    disabled_reason: str = ""


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
        self._reconnect_link_uid: str = ""
        # Reconnect press: dragging either a connected Input pin or the
        # Output-side half of its wire lifts that same wire back into the hand.
        # A click without enough pointer movement remains a no-op.
        self._link_drag_uid: str = ""
        self._link_drag_press_x: float = 0.0
        self._link_drag_press_y: float = 0.0

        # Node drag
        self._dragging_node: bool = False
        self._drag_node_id: str = ""

        # Canvas panning
        self._panning: bool = False
        self._pan_gesture_before: Optional[Tuple[float, float, float]] = None
        self._zoom_gesture_before: Optional[Tuple[float, float, float]] = None

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
        self.on_node_creation_command: Optional[
            Callable[[dict], bool]
        ] = None
        # Pin drag released over empty canvas: (src_node, src_pin, src_kind, graph_x, graph_y).
        # Hosts can use this to pop a "create node and auto-connect" search menu.
        self.on_link_dropped_empty: Optional[Callable[[str, str, "PinKind", float, float], None]] = None
        self.on_node_selected: Optional[Callable[[str], None]] = None
        # Asset-valued input edited: (node_uid, field_id, asset_type, reference).
        # ``reference`` stays structured so picker, drag/drop, paste and clear
        # all use the same contract as Inspector ObjectFields.
        self.on_node_asset_reference: Optional[
            Callable[[str, str, str, object], None]
        ] = None
        self.on_node_asset_reference_items: Optional[
            Callable[[str, str, str, str], object]
        ] = None
        self.on_selection_changed: Optional[
            Callable[[Tuple[str, ...], str, bool], bool]
        ] = None
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
        self.on_view_gesture_committed: Optional[
            Callable[
                [
                    str,
                    Tuple[float, float, float],
                    Tuple[float, float, float],
                ],
                None,
            ]
        ] = None
        self._header_color_popup_node_uid: str = ""
        self._open_header_color_popup_next_frame: bool = False
        self._header_color_popup_initial_color: Optional[Tuple[float, float, float, float]] = None
        self._header_color_popup_changed: bool = False

        # Shared Blender-style node creation palette.  It serves both empty
        # canvas context actions and a connection dropped onto empty space.
        self._node_create_request: Optional[dict] = None
        self._node_create_search = SearchQueryModel()
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

    def reset_interaction_state(self, *, clear_selection: bool = True) -> None:
        """Cancel transient gestures, optionally retaining projected selection."""
        self._commit_view_gesture("pan", self._pan_gesture_before)
        self._commit_view_gesture("zoom", self._zoom_gesture_before)
        if clear_selection:
            self.project_selection((), "")
        self._dragging_pin = False
        self._drag_src_node = ""
        self._drag_src_pin = ""
        self._reconnect_link_uid = ""
        self._link_drag_uid = ""
        self._dragging_node = False
        self._drag_node_id = ""
        self._panning = False
        self._pan_gesture_before = None
        self._zoom_gesture_before = None
        self._layouts.clear()
        self._hovered_link = ""
        self._hovered_pin = ("", "", PinKind.OUTPUT)
        self._header_color_popup_node_uid = ""
        self._open_header_color_popup_next_frame = False
        self._header_color_popup_initial_color = None
        self._header_color_popup_changed = False
        self._node_create_request = None
        self._node_create_search.clear()
        self._node_create_focus = False
        self._open_node_create_popup_next_frame = False
        self._inline_control_hovered = False
        self._inline_control_active = False
        self._canvas_window_hovered = False

    def _view_state(self) -> Tuple[float, float, float]:
        return (float(self.pan_x), float(self.pan_y), float(self.zoom))

    def _commit_view_gesture(
        self,
        kind: str,
        before: Optional[Tuple[float, float, float]],
    ) -> None:
        if before is None:
            return
        after = self._view_state()
        if before != after and self.on_view_gesture_committed is not None:
            self.on_view_gesture_committed(kind, before, after)

    def cancel_node_drag(self) -> bool:
        """Stop an active node drag without publishing a completed gesture."""
        if not self._dragging_node:
            return False
        self._dragging_node = False
        self._drag_node_id = ""
        return True

    def bind_graph(
        self,
        graph: Optional[NodeGraph],
        *,
        preserve_selection: bool = True,
    ) -> None:
        """Replace the model while preserving stable selections that still exist."""
        selected_nodes = tuple(self.selected_nodes) if preserve_selection else ()
        selected_link = self.selected_link if preserve_selection else ""
        self.graph = graph
        self.reset_interaction_state(clear_selection=not preserve_selection)
        if graph is None or not preserve_selection:
            return
        selected_nodes = tuple(
            stable_id
            for stable_id in selected_nodes
            if graph.find_node(stable_id) is not None
        )
        if selected_link and graph.find_link(selected_link) is None:
            selected_link = ""
        if selected_link:
            selected_nodes = ()
        self.project_selection(selected_nodes, selected_link)

    def project_selection(
        self,
        node_ids=(),
        link_id: str = "",
    ) -> bool:
        """Apply an authoritative selection projection from the host service."""
        nodes = tuple(dict.fromkeys(str(uid) for uid in node_ids if str(uid)))
        link = str(link_id or "")
        if nodes and link:
            raise ValueError("node graph selection cannot contain nodes and a link")
        if tuple(self.selected_nodes) == nodes and self.selected_link == link:
            return False
        self.selected_nodes = list(nodes)
        self.selected_link = link
        return True

    def request_selection(
        self,
        node_ids=(),
        link_id: str = "",
        *,
        record_history: bool = True,
    ) -> bool:
        """Publish a user selection intent without mutating the local projection."""
        nodes = tuple(dict.fromkeys(str(uid) for uid in node_ids if str(uid)))
        link = str(link_id or "")
        if nodes and link:
            raise ValueError("node graph selection cannot contain nodes and a link")
        if tuple(self.selected_nodes) == nodes and self.selected_link == link:
            return False
        callback = self.on_selection_changed
        if callback is None:
            return False
        return bool(callback(nodes, link, bool(record_history)))

    @staticmethod
    def _selection_after_node_click(
        selected_nodes: List[str],
        node_uid: str,
        *,
        additive: bool,
    ) -> tuple[str, ...]:
        """Resolve the shared graph selection semantics for one node click."""
        current = list(dict.fromkeys(str(uid) for uid in selected_nodes if uid))
        node_uid = str(node_uid or "")
        if not node_uid:
            return tuple(current)
        if not additive:
            return (node_uid,)
        if node_uid in current:
            current.remove(node_uid)
        else:
            current.append(node_uid)
        return tuple(current)

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

    def _submit_canvas_background_region(
        self, ctx: InxGUIContext, canvas_w: float, canvas_h: float
    ) -> bool:
        """Publish the fixed child canvas without adding an overlapping item."""
        # The host child already owns this fixed extent and graph interactions
        # use explicit hit tests. Any background ImGui item overlaps the real
        # inline widgets and can prevent InputScalar from acquiring ActiveID.
        self._record_semantic_rect(
            ctx,
            "node_graph_canvas",
            "Node Graph",
            True,
            "canvas",
            self._origin_x,
            self._origin_y,
            canvas_w,
            canvas_h,
        )
        return self._canvas_window_hovered

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

    @staticmethod
    def _is_context_style(typedef: NodeTypeDef) -> bool:
        return getattr(typedef, "visual_style", "graph") == "context"

    @classmethod
    def _header_height(cls, typedef: NodeTypeDef) -> float:
        return _CONTEXT_HEADER_H if cls._is_context_style(typedef) else _NODE_HEADER_H

    @classmethod
    def _body_min_height(cls, typedef: NodeTypeDef) -> float:
        return _CONTEXT_BODY_MIN_H if cls._is_context_style(typedef) else _NODE_BODY_MIN_H

    @staticmethod
    def _node_type(graph, node):
        resolver = getattr(graph, "get_node_type", None)
        return resolver(node) if resolver is not None else graph.get_type(node.type_id)

    def center_on_nodes(self) -> None:
        if not self.graph or not self.graph.nodes:
            return
        bounds = []
        for node in self.graph.nodes:
            typedef = self._node_type(self.graph, node)
            if typedef is None:
                continue
            max_pins = max(len(typedef.input_pins()), len(typedef.output_pins()), 1)
            extra_pad = getattr(typedef, "body_bottom_pad", 0.0) or 0.0
            width = self._natural_node_width(node, typedef)
            height = (
                self._header_height(typedef)
                + max_pins * _NODE_PIN_ROW_H
                + self._body_min_height(typedef)
                + extra_pad
            )
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

    def render(
        self,
        ctx: InxGUIContext,
        *,
        defer_canvas_drop_target: bool = False,
    ) -> None:
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

        clip_rect_pushed = False
        try:
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

        # Canvas interaction is handled by the explicit hit tests below. A
        # full-window InvisibleButton would acquire ImGui's ActiveID before
        # inline fields are submitted and make those real widgets impossible
        # to focus. Keep only a layout reservation and a semantic rectangle.
            canvas_hovered = self._submit_canvas_background_region(ctx, canvas_w, canvas_h)

        # Clipping
            clip_x0 = self._origin_x
            clip_y0 = self._origin_y
            clip_x1 = self._origin_x + canvas_w
            clip_y1 = self._origin_y + canvas_h
            ctx.push_draw_list_clip_rect(clip_x0, clip_y0, clip_x1, clip_y1)
            clip_rect_pushed = True

        # Background
            ctx.draw_filled_rect(clip_x0, clip_y0, clip_x1, clip_y1, *_BG_COLOR)

        # Grid
            self._draw_grid(ctx, clip_x0, clip_y0, clip_x1, clip_y1)

        # Compute node layouts
            self._layout_measure_context = ctx
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
            clip_rect_pushed = False

        # Handle interaction
            self._handle_interaction(ctx, canvas_hovered, canvas_w, canvas_h)

        # Header color popup
            self._draw_header_color_popup(ctx)

            if not defer_canvas_drop_target:
                self.render_canvas_drop_target(ctx)

        # Context menu
            if ctx.begin_popup_context_window("##node_graph_ctx", 1):
                try:
                    self._draw_context_menu(ctx)
                finally:
                    ctx.end_popup()

            self._draw_node_create_popup(ctx)
        finally:
            if clip_rect_pushed:
                ctx.pop_draw_list_clip_rect()
            ctx.end_child()

    def render_canvas_drop_target(self, ctx: InxGUIContext) -> None:
        """Submit the canvas target after any floating drag sources."""
        if self.graph is None or self._canvas_w < 1.0 or self._canvas_h < 1.0:
            return
        clip_x0 = self._origin_x
        clip_y0 = self._origin_y
        clip_x1 = clip_x0 + self._canvas_w
        clip_y1 = clip_y0 + self._canvas_h
        if ctx.begin_drag_drop_target_rect(
            clip_x0,
            clip_y0,
            clip_x1,
            clip_y1,
            f"##{self.semantic_namespace}_canvas_drop_target",
        ):
            tup = ctx.accept_any_drag_drop_payload()
            if tup is not None and self.on_canvas_drop:
                dtype, payload = tup
                mx = ctx.get_mouse_pos_x()
                my = ctx.get_mouse_pos_y()
                gx, gy = self.screen_to_graph(mx, my)
                self.on_canvas_drop(dtype, payload, gx, gy)
            ctx.end_drag_drop_target()

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
            # Asset-valued inputs render in the compact pin value box; they are
            # not drawn as wide detached body rows.
            return False
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

    def _natural_node_width(self, node: GraphNode, typedef: NodeTypeDef) -> float:
        """Measure enough room for titles and pin labels without clipping them."""
        ctx = getattr(self, "_layout_measure_context", None)
        measure = getattr(ctx, "calc_text_width", None)

        def text_width(value: object) -> float:
            text = str(value or "")
            if callable(measure):
                try:
                    return float(measure(text))
                except (RuntimeError, TypeError, ValueError):
                    pass
            return float(len(text)) * (_NODE_FONT * 0.52)

        node_data = getattr(node, "data", {})
        title = str(node_data.get("label", getattr(typedef, "label", "")))
        header_reserve = (
            _HEADER_COLOR_SWATCH_SIZE + _HEADER_COLOR_SWATCH_PAD * 2.0
            if getattr(typedef, "show_header_color_swatch", True)
            else _NODE_PAD_X * 2.0
        )
        width = max(float(typedef.min_width), text_width(title) + header_reserve)

        inputs = typedef.input_pins()
        outputs = typedef.output_pins()
        input_width = max(
            (text_width(getattr(pin, "label", "")) for pin in inputs), default=0.0
        )
        output_width = max(
            (text_width(getattr(pin, "label", "")) for pin in outputs), default=0.0
        )
        pin_reserve = _NODE_PAD_X + _PIN_RADIUS * 2.0 + 8.0

        # Pin labels are clipped to their respective 45% side of the node.
        if input_width:
            width = max(width, (input_width + pin_reserve) / 0.45)
        if output_width:
            width = max(width, (output_width + pin_reserve) / 0.45)
        if input_width and output_width:
            width = max(width, input_width + output_width + pin_reserve * 2.0)
        return width

    def _pin_hug_field_width(self, data_type) -> float:
        data_type_str = str(data_type)
        if data_type_str in {"asset_ref", "texture2d", "mesh"}:
            return _PIN_ASSET_HUG_W
        if data_type_str in {"vec2", "vec3", "vec4", "color"}:
            return _PIN_VEC_HUG_W
        return _PIN_VALUE_HUG_W

    def _stable_pin_hug_pad(self) -> float:
        """Left padding for hanging pin value boxes.

        The pad is constant for a given graph topology of field types. It
        must not shrink when a pin is wired, or the node body would slide.
        """
        graph = self.graph
        if graph is None:
            return 0.0
        max_field_w = 0.0
        for node in graph.nodes:
            typedef = self._node_type(graph, node)
            if typedef is None:
                continue
            fields = getattr(typedef, "inline_fields", ())
            if not fields:
                continue
            pin_ids = {pin.id for pin in typedef.input_pins()}
            for field in fields:
                if field.id in pin_ids:
                    max_field_w = max(
                        max_field_w, self._pin_hug_field_width(field.data_type)
                    )
        if max_field_w <= 0.0:
            return 0.0
        return (
            max_field_w + _PIN_RADIUS + _PIN_VALUE_HUG_GAP + _NODE_PAD_X
        ) * self.zoom

    def _compute_layouts(self) -> None:
        self._layouts.clear()
        graph = self.graph
        if graph is None:
            return

        z = self.zoom
        hug_pad = self._stable_pin_hug_pad()
        for node in graph.nodes:
            typedef = self._node_type(graph, node)
            if typedef is None:
                continue

            in_pins = typedef.input_pins()
            out_pins = typedef.output_pins()
            max_pins = max(len(in_pins), len(out_pins), 1)

            w = self._natural_node_width(node, typedef) * z
            if getattr(typedef, "inline_fields", ()):
                # Reserve exactly the rows the inline pass will draw. The
                # typedef's static pad also counts fields that visibility rules
                # hide, which left nodes with dead space below their content.
                extra_pad = (
                    self._detached_field_rows(node, typedef) * _DETACHED_FIELD_ROW_H
                )
            else:
                extra_pad = getattr(typedef, "body_bottom_pad", 0.0) or 0.0
            h = (
                self._header_height(typedef)
                + max_pins * _NODE_PIN_ROW_H
                + self._body_min_height(typedef)
                + extra_pad
            ) * z

            sx = self._origin_x + node.pos_x * z + self.pan_x + hug_pad
            sy = self._origin_y + node.pos_y * z + self.pan_y

            layout = _NodeLayout(node=node, typedef=typedef, sx=sx, sy=sy, w=w, h=h,
                                 left_reserve=hug_pad)

            hdr_h = self._header_height(typedef) * z
            row_h = _NODE_PIN_ROW_H * z
            pin_area_h = max_pins * row_h

            for i, pdef in enumerate(in_pins):
                slot = pin_area_h / max(len(in_pins), 1)
                cy = sy + hdr_h + slot * (i + 0.5)
                layout.input_pins.append(_PinLayout(pin_def=pdef, cx=sx, cy=cy))

            for i, pdef in enumerate(out_pins):
                slot = pin_area_h / max(len(out_pins), 1)
                cy = sy + hdr_h + slot * (i + 0.5)
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
            interaction_point = self._find_node_drag_semantic_point(uid, layout)
            interaction_x, interaction_y = interaction_point or (center_x, center_y)
            interaction_size = 12.0 * max(1.0, self.zoom)
            self._record_semantic_rect(
                ctx,
                "node_graph_node",
                label,
                interaction_point is not None,
                f"node.{uid}",
                interaction_x - interaction_size * 0.5,
                interaction_y - interaction_size * 0.5,
                interaction_size,
                interaction_size,
            )
            drag_x, drag_y = interaction_x, interaction_y
            handle_size = 8.0 * max(1.0, self.zoom)
            self._record_semantic_rect(
                ctx,
                "node_graph_node_drag_handle",
                f"{label} Drag",
                interaction_point is not None,
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
        header_h = self._header_height(layout.typedef) * self.zoom
        candidates = [
            # Header text is draw-list content rather than an ImGui widget, so
            # it is the canonical point for both selection and drag semantics.
            # Body centres commonly contain inline numeric controls.
            (sx + w * 0.5, sy + header_h * 0.5),
            (sx + w * 0.25, sy + header_h * 0.5),
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
        is_context = self._is_context_style(layout.typedef)
        hdr_h = self._header_height(layout.typedef) * z
        pad_x = _NODE_PAD_X * z
        modern_graph = layout.typedef.visual_style in {"graph", "context"}

        # Shadow
        sh = 3.0 * z
        ctx.draw_filled_rect(
            sx + sh, sy + sh, sx + w + sh, sy + h + sh,
            *_NODE_SHADOW_COLOR, rounding,
        )

        # Body
        if is_context:
            body_color = _GRAPH_NODE_CONTEXT_BODY
        elif modern_graph:
            body_color = _GRAPH_NODE_BODY_COLOR
        else:
            body_color = _NODE_BODY_COLOR
        ctx.draw_filled_rect(sx, sy, sx + w, sy + h, *body_color, rounding)

        # Header — Unity VFX contexts use a solid coloured title bar.
        accent = self._resolve_node_header_color(layout)
        if is_context:
            hdr = accent
        elif modern_graph:
            hdr = _GRAPH_NODE_HEADER_COLOR
        else:
            hdr = accent
        ctx.draw_filled_rect(sx, sy, sx + w, sy + hdr_h, *hdr, rounding)
        flat_h = min(rounding, hdr_h * 0.5)
        ctx.draw_filled_rect(sx, sy + hdr_h - flat_h, sx + w, sy + hdr_h, *hdr, 0)
        if modern_graph and not is_context:
            accent_h = max(2.0, 3.0 * z)
            ctx.draw_filled_rect(
                sx, sy, sx + w, sy + accent_h, *accent, min(rounding, 3.0 * z)
            )

        # Recessed block-slot area for context nodes (Unity VFX Context look).
        if is_context:
            slot_pad = 6.0 * z
            slot_top = sy + hdr_h + 4.0 * z
            slot_bottom = sy + h - slot_pad
            if slot_bottom > slot_top + 4.0 * z:
                ctx.draw_filled_rect(
                    sx + slot_pad,
                    slot_top,
                    sx + w - slot_pad,
                    slot_bottom,
                    *_GRAPH_NODE_CONTEXT_SLOT,
                    max(2.0, 3.0 * z),
                )

        # Header label — node name only (no category chips / Particle subtitle).
        label = layout.node.data.get("label", layout.typedef.label)
        font_sz = self._zoom_font(_NODE_FONT)
        sw_x1, sw_y1, sw_x2, sw_y2 = self._node_header_swatch_rect(layout)
        label_right = (
            sw_x1 - 6.0 * z
            if layout.typedef.show_header_color_swatch
            else sx + w - pad_x
        )
        # Title: optically center in header; modern graph nodes exclude the
        # top accent strip. Inset the text box ~1px (at zoom 1) top/bottom.
        if modern_graph and not is_context:
            title_top = sy + max(2.0, 3.0 * z)
        else:
            title_top = sy
        title_inset = 1.0 * z
        ctx.draw_text_aligned(
            sx + pad_x,
            title_top + title_inset,
            max(sx + pad_x + 1.0, label_right),
            sy + hdr_h - title_inset,
            label,
            *_TEXT_COLOR,
            0.0,
            0.5,
            font_sz,
            True,
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

        # Subtitle (e.g. clip path) — same 18px face as the rest of the node.
        subtitle = "" if is_context else layout.node.data.get("subtitle", "")
        if subtitle:
            body_top = sy + hdr_h + 2 * z
            sub_inset = 1.0 * z
            ctx.draw_text_aligned(
                sx + pad_x,
                body_top + sub_inset,
                sx + w - pad_x,
                body_top + _NODE_PIN_ROW_H * z - sub_inset,
                subtitle,
                *_TEXT_BODY_COLOR,
                0.0,
                0.0,
                font_sz,
                True,
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

        # Pin labels (A / B / …) — same 18px face as the title.
        dim_font = self._zoom_font(_NODE_FONT)
        row_h = _NODE_PIN_ROW_H * z
        label_half = max(row_h * 0.5 - 1.0 * z, dim_font * 0.5)
        for pl in layout.input_pins:
            ctx.draw_text_aligned(
                pl.cx + pin_r + 4 * z, pl.cy - label_half,
                pl.cx + w * 0.45, pl.cy + label_half,
                pl.pin_def.label, *_TEXT_DIM_COLOR, 0.0, 0.5, dim_font, True,
            )
        for pl in layout.output_pins:
            ctx.draw_text_aligned(
                sx + w * 0.55, pl.cy - label_half,
                pl.cx - pin_r - 4 * z, pl.cy + label_half,
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

    def _draw_inline_fields(self, ctx) -> None:
        if self.graph is None:
            return
        saved_x = ctx.get_cursor_pos_x()
        saved_y = ctx.get_cursor_pos_y()
        # Widgets are authored at zoom 1.0; scale the font (and the frame
        # metrics derived from it) with the node chrome at every zoom level —
        # never hide them past a threshold, or zooming out looks like the
        # node contents vanished.
        z = self.zoom
        font_scale = max(_TEXT_MIN_FONT / _INLINE_BASE_FONT, z * _INLINE_FONT_SCALE)
        ctx.set_window_font_scale(font_scale)
        ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding, 4.0 * z, 2.0 * z)
        ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing, 4.0 * z, 3.0 * z)
        ctx.push_style_var_vec2(ImGuiStyleVar.ItemInnerSpacing, 3.0 * z, 2.0 * z)
        ctx.push_style_var_float(ImGuiStyleVar.FrameRounding, 2.0 * z)
        try:
            # Inline controls may publish a property edit immediately. The
            # document callback can rebuild this layout cache while the frame
            # is still drawing, so iterate over a stable frame snapshot.
            for layout in tuple(self._layouts.values()):
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
                            + self._header_height(layout.typedef) * self.zoom
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
        value_shape_matches = True
        z = self.zoom
        font_scale = max(_TEXT_MIN_FONT / _INLINE_BASE_FONT, z * _INLINE_FONT_SCALE)
        local_x = layout.sx - self._origin_x
        # Half of the taller 18px frame so the widget sits on the pin row.
        # Pin value boxes are shorter, so center against their own extent.
        if not detached:
            box_h = (_NODE_PIN_ROW_H - _PIN_VALUE_BOX_INSET) * self.zoom
            local_y = row_y - self._origin_y - box_h * 0.5 + 1.0 * self.zoom
        else:
            local_y = row_y - self._origin_y - 11.0 * self.zoom
        # When the field is backed by an unconnected input pin, the compact
        # value box hangs OUTSIDE the node's left edge, right-aligned against
        # the pin circle (Unity Shader Graph style). This keeps numeric inputs
        # from inflating the node body width. Detached fields keep the wide
        # full-node-width box in the body.
        if detached:
            field_x = local_x + layout.w * 0.42
            # Widths scale with the node so the control never crosses its right
            # edge; the minimum also scales, otherwise zoomed-out nodes overflow.
            field_w = max(24.0 * self.zoom, layout.w * 0.52 - _NODE_PAD_X * self.zoom)
        else:
            field_w = self._pin_hug_field_width(field_def.data_type) * self.zoom
            # Right-align the box so its right edge meets the pin circle with a
            # small gap, hanging into the reserved left-of-node space.
            field_x = local_x - _PIN_RADIUS * self.zoom - _PIN_VALUE_HUG_GAP * self.zoom - field_w
        label_half = _NODE_PIN_ROW_H * 0.5 * self.zoom
        if detached:
            ctx.draw_text_aligned(
                layout.sx + _NODE_PAD_X * self.zoom,
                row_y - label_half,
                layout.sx + layout.w * 0.40,
                row_y + label_half,
                str(field_def.label),
                *_TEXT_DIM_COLOR,
                0.0,
                0.5,
                self._zoom_font(_NODE_FONT),
                True,
            )
        # Non-detached (pin-backed) fields keep their label inside the node at
        # the default pin-label position, drawn by _draw_nodes. Only the value
        # box hangs outside the left edge.

        # Pin value box: a unified self-drawn background (vector components
        # read as one capsule instead of separate frames) and a shorter frame.
        pin_value_box = not detached
        if pin_value_box:
            box_top_y = row_y - (_NODE_PIN_ROW_H - _PIN_VALUE_BOX_INSET) * 0.5 * self.zoom
            box_bot_y = row_y + (_NODE_PIN_ROW_H - _PIN_VALUE_BOX_INSET) * 0.5 * self.zoom
            box_x0 = self._origin_x + field_x
            box_x1 = self._origin_x + field_x + field_w
            # Hover feedback: the transparent widget frames give no visual cue,
            # so brighten the unified box background while the pointer is over
            # it (Unity value-slot behaviour).
            mpx = ctx.get_mouse_pos_x()
            mpy = ctx.get_mouse_pos_y()
            box_hovered = (
                box_x0 <= mpx <= box_x1 and box_top_y <= mpy <= box_bot_y
            )
            bg = (
                (min(1.0, _PIN_VALUE_BG[0] + 0.06), min(1.0, _PIN_VALUE_BG[1] + 0.06),
                 min(1.0, _PIN_VALUE_BG[2] + 0.06), 1.0)
                if box_hovered
                else _PIN_VALUE_BG
            )
            ctx.draw_filled_rect(
                box_x0,
                box_top_y,
                box_x1,
                box_bot_y,
                *bg,
                _PIN_VALUE_BG_ROUNDING * self.zoom,
            )

        ctx.set_cursor_pos_x(field_x)
        ctx.set_cursor_pos_y(local_y)
        ctx.push_id_str(f"inline_{layout.node.uid}_{field_def.id}")
        try:
            data_type = str(field_def.data_type)
            new_value = value
            semantic_id = self._semantic_id(
                f"inline.{layout.node.uid}.{field_def.id}"
            )
            semantic_recorded_by_widget = False
            if data_type == "bool":
                new_value = bool(ctx.checkbox("##value", bool(value)))
            elif data_type == "i32":
                ctx.set_next_item_width(field_w)
                value_shape_matches = not isinstance(value, (list, tuple))
                scalar_value = self._inline_scalar_value(value, field_def.default)
                if pin_value_box:
                    ctx.set_window_font_scale(font_scale * _PIN_VALUE_FONT_SCALE)
                    ctx.push_style_color(ImGuiCol.FrameBg, 0.0, 0.0, 0.0, 0.0)
                    ctx.push_style_color(ImGuiCol.FrameBgHovered, 0.0, 0.0, 0.0, 0.0)
                    ctx.push_style_color(ImGuiCol.FrameBgActive, 0.0, 0.0, 0.0, 0.0)
                semantic_input = getattr(ctx, "input_int_semantic", None)
                if callable(semantic_input):
                    new_value = int(
                        semantic_input("##value", int(scalar_value), semantic_id)
                    )
                    semantic_recorded_by_widget = True
                else:
                    new_value = int(ctx.input_int("##value", int(scalar_value)))
                if pin_value_box:
                    ctx.pop_style_color(3)
                    ctx.set_window_font_scale(font_scale)
            elif data_type == "u32":
                ctx.set_next_item_width(field_w)
                value_shape_matches = not isinstance(value, (list, tuple))
                current = max(
                    0,
                    min(0xFFFFFFFF, int(self._inline_scalar_value(value, field_def.default))),
                )
                if pin_value_box:
                    ctx.set_window_font_scale(font_scale * _PIN_VALUE_FONT_SCALE)
                    ctx.push_style_color(ImGuiCol.FrameBg, 0.0, 0.0, 0.0, 0.0)
                    ctx.push_style_color(ImGuiCol.FrameBgHovered, 0.0, 0.0, 0.0, 0.0)
                    ctx.push_style_color(ImGuiCol.FrameBgActive, 0.0, 0.0, 0.0, 0.0)
                semantic_input = getattr(ctx, "input_uint_semantic", None)
                if callable(semantic_input):
                    new_value = int(semantic_input("##value", current, semantic_id))
                    semantic_recorded_by_widget = True
                else:
                    input_uint = getattr(ctx, "input_uint", None)
                    if callable(input_uint):
                        new_value = int(input_uint("##value", current))
                    else:
                        # Keep source-Python editor runs compatible with an older
                        # native module while it is waiting to be rebuilt.  The
                        # signed ImGui binding cannot accept values above INT_MAX.
                        raw_value = ctx.text_input("##value", str(current), 11)
                        try:
                            new_value = int(str(raw_value).strip(), 10)
                        except ValueError:
                            new_value = current
                new_value = max(0, min(0xFFFFFFFF, new_value))
                if pin_value_box:
                    ctx.pop_style_color(3)
                    ctx.set_window_font_scale(font_scale)
            elif data_type == "f32":
                ctx.set_next_item_width(field_w)
                # Dynamic graph ports can change shape while a live document is
                # being rebuilt.  Keep a stale frame snapshot from escaping the
                # canvas render and unbalancing the surrounding ImGui child.
                value_shape_matches = not isinstance(value, (list, tuple))
                scalar_value = self._inline_scalar_value(value, field_def.default)
                if pin_value_box:
                    ctx.set_window_font_scale(font_scale * _PIN_VALUE_FONT_SCALE)
                    ctx.push_style_color(ImGuiCol.FrameBg, 0.0, 0.0, 0.0, 0.0)
                    ctx.push_style_color(ImGuiCol.FrameBgHovered, 0.0, 0.0, 0.0, 0.0)
                    ctx.push_style_color(ImGuiCol.FrameBgActive, 0.0, 0.0, 0.0, 0.0)
                semantic_drag = getattr(ctx, "drag_float_semantic", None)
                if callable(semantic_drag):
                    new_value = float(
                        semantic_drag(
                            "##value",
                            float(scalar_value or 0.0),
                            0.05,
                            -1.0e7,
                            1.0e7,
                            semantic_id,
                        )
                    )
                    semantic_recorded_by_widget = True
                else:
                    new_value = float(
                        ctx.drag_float(
                            "##value", float(scalar_value or 0.0), 0.05, -1.0e7, 1.0e7
                        )
                    )
                if pin_value_box:
                    ctx.pop_style_color(3)
                    ctx.set_window_font_scale(font_scale)
            elif data_type == "color":
                value_shape_matches = self._inline_vector_shape_matches(value, 4)
                components = self._inline_vector_value(value, field_def.default, 4)
                allow_hdr = bool(getattr(field_def, "hdr", True))
                new_value = render_color_value_bar(
                    ctx,
                    f"##value_{layout.node.uid}_{field_def.id}",
                    components,
                    allow_hdr=allow_hdr,
                    default_hdr_enabled=allow_hdr,
                    width=field_w,
                    height=(_NODE_PIN_ROW_H - _PIN_VALUE_BOX_INSET) * self.zoom,
                )
            elif data_type in {"vec2", "vec3", "vec4"}:
                size = {"vec2": 2, "vec3": 3, "vec4": 4}[data_type]
                value_shape_matches = self._inline_vector_shape_matches(value, size)
                components = self._inline_vector_value(value, field_def.default, size)
                gap = _PIN_VEC_COMPONENT_GAP * self.zoom
                component_w = max(14.0 * self.zoom, (field_w - (size - 1) * gap) / size)
                if pin_value_box:
                    ctx.set_window_font_scale(font_scale * _PIN_VALUE_FONT_SCALE)
                    ctx.push_style_color(ImGuiCol.FrameBg, 0.0, 0.0, 0.0, 0.0)
                    ctx.push_style_color(ImGuiCol.FrameBgHovered, 0.0, 0.0, 0.0, 0.0)
                    ctx.push_style_color(ImGuiCol.FrameBgActive, 0.0, 0.0, 0.0, 0.0)
                edited = []
                for index, component in enumerate(components):
                    slot_x = field_x + index * (component_w + gap)
                    axis_w = min(12.0 * self.zoom, component_w * 0.30)
                    axis_half = 9.0 * self.zoom
                    ctx.draw_text_aligned(
                        self._origin_x + slot_x,
                        row_y - axis_half,
                        self._origin_x + slot_x + axis_w,
                        row_y + axis_half,
                        "XYZW"[index],
                        *_TEXT_DIM_COLOR,
                        0.0,
                        0.5,
                        self._zoom_font(_NODE_FONT) * _PIN_VALUE_FONT_SCALE,
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
                if pin_value_box:
                    ctx.pop_style_color(3)
                    ctx.set_window_font_scale(font_scale)
                new_value = edited
            elif field_def.enum_values:
                values = list(field_def.enum_values)
                labels = list(
                    getattr(field_def, "enum_labels", ()) or field_def.enum_values
                )
                index = values.index(value) if value in values else 0
                ctx.set_next_item_width(field_w)
                if pin_value_box:
                    ctx.set_window_font_scale(font_scale * _PIN_VALUE_FONT_SCALE)
                    ctx.push_style_color(ImGuiCol.FrameBg, 0.0, 0.0, 0.0, 0.0)
                    ctx.push_style_color(ImGuiCol.FrameBgHovered, 0.0, 0.0, 0.0, 0.0)
                    ctx.push_style_color(ImGuiCol.FrameBgActive, 0.0, 0.0, 0.0, 0.0)
                index = ctx.combo("##value", index, labels, -1)
                if pin_value_box:
                    ctx.pop_style_color(3)
                    ctx.set_window_font_scale(font_scale)
                new_value = values[max(0, min(index, len(values) - 1))]
            elif data_type == "string":
                ctx.set_next_item_width(field_w)
                if pin_value_box:
                    ctx.set_window_font_scale(font_scale * _PIN_VALUE_FONT_SCALE)
                    ctx.push_style_color(ImGuiCol.FrameBg, 0.0, 0.0, 0.0, 0.0)
                    ctx.push_style_color(ImGuiCol.FrameBgHovered, 0.0, 0.0, 0.0, 0.0)
                    ctx.push_style_color(ImGuiCol.FrameBgActive, 0.0, 0.0, 0.0, 0.0)
                new_value = ctx.text_input("##value", str(value or ""), 256)
                if pin_value_box:
                    ctx.pop_style_color(3)
                    ctx.set_window_font_scale(font_scale)
            elif data_type in {"asset_ref", "texture2d", "mesh"}:
                # Asset-valued input pin: render the shared Inspector asset
                # reference field (picker + drag-drop) in the pin value box.
                asset_type = (
                    str(getattr(field_def, "asset_type", "") or "")
                    or {"texture2d": "Texture", "mesh": "Mesh"}.get(
                        data_type, "Asset"
                    )
                )
                ref = self._asset_reference_value(value)
                ref_changed = self._render_inline_asset_reference(
                    ctx, layout, field_def, asset_type, ref, field_w
                )
                if ref_changed is not None:
                    new_value = ref_changed
                value_shape_matches = True
            else:
                return
            self._note_inline_control_state(ctx)
            semantic_values = {}
            semantic_kind = "control"
            if data_type == "bool":
                semantic_kind = "checkbox"
                semantic_values["bool_value"] = bool(new_value)
            elif data_type in {"i32", "u32", "f32"}:
                semantic_kind = "drag_float" if data_type == "f32" else "int_input"
                semantic_values["numeric_value"] = float(new_value)
            elif field_def.enum_values:
                semantic_kind = "combo"
                semantic_values["string_value"] = str(new_value)
            elif data_type == "string":
                semantic_kind = "text_input"
                semantic_values["string_value"] = str(new_value)
            if not semantic_recorded_by_widget:
                self._record_semantic_item(
                    ctx,
                    semantic_kind,
                    str(field_def.label),
                    True,
                    f"inline.{layout.node.uid}.{field_def.id}",
                    **semantic_values,
                )
            # A live graph rebuild can leave one frame where the new field type
            # and the old property value disagree. Draw a projected value for
            # that frame, but never overwrite the document with the projection.
            if value_shape_matches:
                self._commit_inline_value(layout.node, field_def.id, new_value)
        finally:
            ctx.pop_id()

    def _asset_reference_value(self, value):
        """Normalize an asset-valued node input to a displayable reference dict."""
        if value is None:
            return None
        if isinstance(value, dict):
            return value
        # AssetRef-like objects expose to_dict()/guid/path_hint.
        to_dict = getattr(value, "to_dict", None)
        if callable(to_dict):
            try:
                return to_dict()
            except Exception:
                pass
        guid = getattr(value, "guid", None)
        path_hint = getattr(value, "path_hint", "")
        if guid or path_hint:
            return {"guid": guid, "path_hint": path_hint}
        return None

    def _render_inline_asset_reference(
        self, ctx, layout: _NodeLayout, field_def, asset_type: str, ref, field_w: float
    ):
        """Render the shared asset-reference field in a pin value box."""
        from Infernux.engine.ui.igui import IGUI

        display = ""
        if isinstance(ref, dict):
            path_hint = str(ref.get("path_hint") or ref.get("path") or "")
            builtin = str(ref.get("builtin") or ref.get("built_in") or "")
            if builtin:
                display = f"Built-in {builtin}"
            elif path_hint:
                display = os.path.basename(path_hint.replace("\\", "/"))
            else:
                display = str(ref.get("name") or ref.get("guid") or "")
        elif ref is not None:
            display = str(ref)
        if display.startswith("builtin-mesh:"):
            display = f"Built-in {display.removeprefix('builtin-mesh:')}"

        has_value = bool(
            isinstance(ref, dict)
            and any(
                str(ref.get(name) or "").strip()
                for name in ("guid", "path_hint", "path", "builtin", "built_in")
            )
        )

        callback = self.on_node_asset_reference

        def _assign(payload):
            if callback is not None:
                callback(layout.node.uid, field_def.id, asset_type, payload)

        def _clear():
            if callback is not None:
                callback(layout.node.uid, field_def.id, asset_type, None)

        additional_items = None
        item_callback = self.on_node_asset_reference_items
        if item_callback is not None:
            additional_items = lambda query: item_callback(
                layout.node.uid,
                field_def.id,
                asset_type,
                query,
            )

        ping_path = ""
        if isinstance(ref, dict):
            ping_path = str(ref.get("path_hint") or "").strip()

        changed = IGUI.asset_reference_field(
            ctx,
            f"inline_asset_{layout.node.uid}_{field_def.id}",
            display or t("igui.none"),
            asset_type,
            clickable=True,
            on_assign=_assign,
            additional_asset_items=additional_items,
            on_clear=_clear,
            ping_path=ping_path or None,
            has_value=has_value,
            asset_type=asset_type,
            semantic_id=self._semantic_id(
                f"inline.{layout.node.uid}.{field_def.id}"
            ),
            reference_value=ref,
            fixed_width=max(60.0 * self.zoom, field_w),
        )
        if changed:
            # Assignment is routed through the panel callback; keep the current
            # node data until the document applies the mutation.
            return None
        return None

    @staticmethod
    def _inline_scalar_value(value, default=0.0) -> float:
        candidate = value
        while isinstance(candidate, (list, tuple)):
            if not candidate:
                candidate = default
                break
            candidate = candidate[0]
        try:
            return float(candidate)
        except (TypeError, ValueError):
            try:
                return float(default)
            except (TypeError, ValueError):
                return 0.0

    @classmethod
    def _inline_vector_value(cls, value, default, size: int) -> list[float]:
        source = value if isinstance(value, (list, tuple)) else default
        if not isinstance(source, (list, tuple)):
            source = [source]
        components = [cls._inline_scalar_value(item) for item in list(source)[:size]]
        components.extend([0.0] * (size - len(components)))
        return components

    @staticmethod
    def _inline_vector_shape_matches(value, size: int) -> bool:
        return (
            isinstance(value, (list, tuple))
            and len(value) == size
            and all(not isinstance(item, (list, tuple)) for item in value)
        )

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
            # Reconnect is transactional: keep the original graph edge alive
            # until release, but visually lift it from the Input immediately.
            if self._dragging_pin and lk.uid == self._reconnect_link_uid:
                continue
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
        curve_dx = abs(x2 - x1) * 0.5
        curve_dx = max(curve_dx, 30.0)
        cx1, cy1 = x1 + curve_dx, y1
        cx2, cy2 = x2 - curve_dx, y2
        gap = (_PIN_RADIUS + 3.0) * z
        a_len = 9.0 * z
        a_half = 5.0 * z

        # Use the analytic endpoint tangent and trim the sampled curve by
        # arc length. Connecting the last coarse sample directly to an arrow
        # base was the source of the visible folded/kinked tail.
        tx, ty = _bezier_tangent(x1, y1, cx1, cy1, cx2, cy2, x2, y2, 1.0)
        tangent_len = math.hypot(tx, ty) or 1.0
        dx, dy = tx / tangent_len, ty / tangent_len
        tipx, tipy = x2 - dx * gap, y2 - dy * gap
        cutoff = gap + a_len

        trim_t = 0.0
        previous = (x2, y2)
        travelled = 0.0
        samples = max(32, _LINK_SEGMENTS * 2)
        for index in range(1, samples + 1):
            t = 1.0 - index / samples
            current = _bezier_point(x1, y1, cx1, cy1, cx2, cy2, x2, y2, t)
            segment = math.hypot(previous[0] - current[0], previous[1] - current[1])
            if travelled + segment >= cutoff:
                ratio = (cutoff - travelled) / max(segment, 1.0e-6)
                trim_t = t + (previous_t - t) * ratio if index > 1 else t
                break
            travelled += segment
            previous = current
            previous_t = t
        else:
            trim_t = 0.0

        basex, basey = _bezier_point(x1, y1, cx1, cy1, cx2, cy2, x2, y2, trim_t)
        curve_points = [
            _bezier_point(x1, y1, cx1, cy1, cx2, cy2, x2, y2, trim_t * i / samples)
            for i in range(samples + 1)
        ]
        for i in range(len(curve_points) - 1):
            ctx.draw_line(*curve_points[i], *curve_points[i + 1], *color, thickness)
        # Keep the arrow base exactly on the curve while retaining the
        # endpoint-facing arrow direction computed from the analytic tangent.
        if math.hypot(curve_points[-1][0] - basex, curve_points[-1][1] - basey) > 0.25:
            ctx.draw_line(*curve_points[-1], basex, basey, *color, thickness)
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
        hdr_h = self._header_height(layout.typedef) * z
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

        # Space: open the node-create palette at the canvas centre (Unity
        # Shader Graph style). Only while hovering the canvas and not editing
        # an inline value so the palette search box keeps Space.
        if (
            canvas_hovered
            and not self._inline_control_active
            and ctx.is_key_pressed(KEY_SPACE)
        ):
            if self.graph is not None and self._node_create_request is None:
                cx = self._origin_x + canvas_w * 0.5
                cy = self._origin_y + canvas_h * 0.5
                gx, gy = self.screen_to_graph(cx, cy)
                self._request_node_creation(gx, gy)
            return

        # Pin dragging
        if self._dragging_pin:
            self._drag_end_x = mx
            self._drag_end_y = my
            if not ctx.is_mouse_button_down(0):
                self._try_complete_link(mx, my)
                self._dragging_pin = False
                self._reconnect_link_uid = ""
            return

        # A press on a connected Input or a link's Output-side half becomes a
        # reconnect gesture only after the pointer crosses the drag threshold.
        if self._link_drag_uid:
            if not ctx.is_mouse_button_down(0):
                self._link_drag_uid = ""
                return
            dx = abs(mx - self._link_drag_press_x)
            dy = abs(my - self._link_drag_press_y)
            if dx + dy > 4.0 * max(1.0, self.zoom):
                link = self.graph.find_link(self._link_drag_uid) if self.graph else None
                self._link_drag_uid = ""
                if link is None:
                    return
                self._begin_link_reconnect(link, mx, my)
                self.request_selection((), "", record_history=False)
                return
            return

        # Node dragging (divide delta by zoom)
        if self._dragging_node:
            if self.on_node_drag_start is None or self.on_node_drag_end is None:
                self.cancel_node_drag()
                return
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
                before = self._pan_gesture_before
                self._pan_gesture_before = None
                self._commit_view_gesture("pan", before)
            return

        # Zoom (scroll wheel, centred on cursor). Handled before the inline
        # widget guard so pointing at a node value still zooms, matching every
        # other node editor; only an in-progress edit keeps the wheel.
        if self._canvas_window_hovered and not self._inline_control_active:
            wheel = ctx.get_mouse_wheel_delta()
            if abs(wheel) > 0.01:
                if self._zoom_gesture_before is None:
                    self._zoom_gesture_before = self._view_state()
                old_zoom = self.zoom
                self.zoom = max(_ZOOM_MIN, min(_ZOOM_MAX, self.zoom + wheel * _ZOOM_SPEED))
                ratio = self.zoom / old_zoom
                self.pan_x = mx - self._origin_x - (mx - self._origin_x - self.pan_x) * ratio
                self.pan_y = my - self._origin_y - (my - self._origin_y - self.pan_y) * ratio
            elif self._zoom_gesture_before is not None:
                before = self._zoom_gesture_before
                self._zoom_gesture_before = None
                self._commit_view_gesture("zoom", before)
        elif self._zoom_gesture_before is not None:
            before = self._zoom_gesture_before
            self._zoom_gesture_before = None
            self._commit_view_gesture("zoom", before)

        if self._inline_control_hovered:
            return

        if not canvas_hovered:
            return

        # Middle-mouse → panning
        if ctx.is_mouse_button_clicked(2):
            if self._zoom_gesture_before is not None:
                before = self._zoom_gesture_before
                self._zoom_gesture_before = None
                self._commit_view_gesture("zoom", before)
            self._panning = True
            self._pan_gesture_before = self._view_state()
            return

        # Right-click — select link under cursor for context menu
        if ctx.is_mouse_button_clicked(1):
            hit_lk = self._hit_test_link(mx, my)
            if hit_lk:
                self._notify_before_selection_change()
                self.request_selection((), hit_lk)
                if self.on_node_selected:
                    self.on_node_selected("")

        # Left click
        if ctx.is_mouse_button_clicked(0):
            # Header color swatch
            hit_color_uid = self._hit_test_header_color_swatch(mx, my)
            if hit_color_uid:
                self._notify_before_selection_change()
                self.request_selection((hit_color_uid,), "")
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
                        self._arm_link_reconnect(existing, mx, my)
                        return
                self._dragging_pin = True
                self._reconnect_link_uid = ""
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
                additive = (
                    ctx.is_key_down(KEY_LEFT_CTRL)
                    or ctx.is_key_down(KEY_RIGHT_CTRL)
                )
                selected = self._selection_after_node_click(
                    self.selected_nodes,
                    hit_uid,
                    additive=additive,
                )
                self.request_selection(selected, "")
                if self.on_node_selected:
                    self.on_node_selected(selected[-1] if selected else "")
                if hit_uid not in selected:
                    return
                consumed = False
                if self.on_node_primary_click:
                    consumed = bool(self.on_node_primary_click(hit_uid, mx, my))
                if (
                    not consumed
                    and self.on_node_drag_start is not None
                    and self.on_node_drag_end is not None
                ):
                    self._dragging_node = True
                    self._drag_node_id = hit_uid
                    self.on_node_drag_start(hit_uid)
                return

            # Links — clicking selects; dragging the Output-side half lifts
            # the existing Input connection back into a pending wire.
            hit_lk, hit_progress = self._hit_test_link_with_progress(mx, my)
            if hit_lk:
                self._notify_before_selection_change()
                self.request_selection((), hit_lk)
                if self.on_node_selected:
                    self.on_node_selected("")
                if self.graph:
                    link = self.graph.find_link(hit_lk)
                    if link is not None:
                        # Only the curve's actual Output-side half arms the
                        # rewire gesture. The Input-side half remains a normal
                        # link selection target.
                        if hit_progress <= 0.5:
                            self._arm_link_reconnect(link, mx, my)
                return

            # Empty space — deselect
            self._notify_before_selection_change()
            self.request_selection((), "")
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
        return self._hit_test_link_with_progress(mx, my, threshold)[0]

    def _hit_test_link_with_progress(self, mx, my, threshold=6.0):
        """Return the nearest hit link and its visual arc-length progress."""
        if self.graph is None:
            return "", 0.0
        t_scaled = threshold * max(1.0, self.zoom)
        best_uid = ""
        best_progress = 0.0
        best_distance = t_scaled
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
            segment_lengths = [
                _dist(*pts[i], *pts[i + 1]) for i in range(len(pts) - 1)
            ]
            total_length = sum(segment_lengths)
            traversed = 0.0
            for i in range(len(pts) - 1):
                distance, segment_t = _point_segment_projection(
                    mx, my, *pts[i], *pts[i + 1]
                )
                if distance < best_distance:
                    segment_length = segment_lengths[i]
                    best_uid = lk.uid
                    best_distance = distance
                    best_progress = (
                        (traversed + segment_length * segment_t) / total_length
                        if total_length > 1e-6
                        else 0.0
                    )
                traversed += segment_lengths[i]
        return best_uid, best_progress

    def _begin_link_reconnect(self, link: GraphLink, mx: float, my: float) -> None:
        """Lift an existing Input connection back into a pending wire."""
        self._dragging_pin = True
        self._reconnect_link_uid = link.uid
        self._drag_src_node = link.source_node
        self._drag_src_pin = link.source_pin
        self._drag_src_kind = PinKind.OUTPUT
        self._drag_end_x = float(mx)
        self._drag_end_y = float(my)

    def _arm_link_reconnect(self, link: GraphLink, mx: float, my: float) -> None:
        """Remember a reconnect press until it becomes an actual drag."""
        self._link_drag_uid = link.uid
        self._link_drag_press_x = float(mx)
        self._link_drag_press_y = float(my)

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
        return existing if self.on_link_replaced is not None else None

    def _try_complete_link(self, mx, my):
        target_node, target_pin, target_kind = self._hit_test_pin(mx, my)
        if target_pin is None:
            if self._reconnect_link_uid:
                if self.on_link_deleted is not None:
                    self.on_link_deleted(self._reconnect_link_uid)
                return
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
        existing = (
            self.graph.find_link(self._reconnect_link_uid)
            if self.graph is not None and self._reconnect_link_uid
            else self._replaceable_input_link(endpoints)
        )
        if existing is not None and endpoints == (
            existing.source_node,
            existing.source_pin,
            existing.target_node,
            existing.target_pin,
        ):
            return
        ignore_uid = existing.uid if existing is not None else ""
        if self.graph is not None and not self.graph.validate_link(
            *endpoints, ignore_link_uid=ignore_uid
        ):
            return
        if existing is not None:
            if self.on_link_replaced is not None:
                self.on_link_replaced(existing.uid, *endpoints)
            return
        if self.on_link_created is not None:
            self.on_link_created(*endpoints)

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
        self._node_create_search.clear()
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
            source_def = (
                self._node_type(self.graph, source_node) if source_node else None
            )
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
        if self.graph is None or not entry.enabled:
            return
        if self.on_node_creation_selected is not None:
            self.on_node_creation_selected(entry, dict(request))
            return
        if self.on_node_add_request is None:
            return
        if request.get("source_node") and self.on_link_created is None:
            return
        typedef = self.graph.get_type(entry.type_id)
        if typedef is None:
            return
        before = {node.uid for node in self.graph.nodes}
        result = self.on_node_add_request(
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
        self.on_link_created(*endpoints)

    @staticmethod
    def _creation_command_payload(
        entry: NodeCreationEntry,
        request: dict,
    ) -> dict:
        source_kind = PinKind(
            request.get("source_kind", PinKind.OUTPUT)
        )
        return {
            "entry_key": entry.key,
            "type_id": entry.type_id,
            "gx": float(request["gx"]),
            "gy": float(request["gy"]),
            "source_node": str(request.get("source_node", "") or ""),
            "source_pin": str(request.get("source_pin", "") or ""),
            "source_kind": source_kind.value,
        }

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
            search_text = ctx.input_text_with_hint(
                "##node_create_search",
                t("node_graph.search_nodes"),
                self._node_create_search.query,
                160,
            )
            self._node_create_search.set_query(search_text)
            grouped: Dict[str, List[NodeCreationEntry]] = {}
            for entry in self._creation_entries(request):
                haystack = (
                    f"{entry.label} {entry.key} {entry.type_id} {entry.category}"
                    .casefold()
                )
                if not self._node_create_search.matches_normalized(haystack):
                    continue
                grouped.setdefault(entry.category or "NODE", []).append(entry)
            # Fixed-height scrollable result list so a large node catalog never
            # blows up the palette beyond the viewport.
            list_h = 260.0 * max(1.0, self.zoom)
            if ctx.begin_child("##node_create_list", 0, list_h, False):
                try:
                    for category in sorted(grouped):
                        ctx.label(category)
                        for entry in sorted(
                            grouped[category], key=lambda item: item.label.casefold()
                        ):
                            if not entry.enabled:
                                ctx.begin_disabled(True)
                            entry_selected = ctx.selectable(entry.label, False)
                            self._record_semantic_item(
                                ctx,
                                "selectable",
                                entry.label,
                                entry.enabled,
                                f"create.{entry.type_id}",
                                string_value=entry.type_id,
                            )
                            if not entry.enabled:
                                ctx.end_disabled()
                            if entry_selected and entry.enabled:
                                selected = entry
                                ctx.close_current_popup()
                finally:
                    ctx.end_child()
            ctx.end_popup()
        else:
            self._node_create_request = None
        if selected is not None:
            request = dict(request)
            self._node_create_request = None
            if self.on_node_creation_command is not None:
                self.on_node_creation_command(
                    self._creation_command_payload(selected, request)
                )

    def _draw_context_menu(self, ctx) -> None:
        mx = ctx.get_mouse_pos_x()
        my = ctx.get_mouse_pos_y()
        gx, gy = self.screen_to_graph(mx, my)

        if self.graph is None:
            return

        def _record_command(
            semantic_ctx: object,
            command: ResolvedContextMenuCommand,
        ) -> None:
            self._record_semantic_item(
                semantic_ctx,
                "menu_item",
                command.label,
                command.enabled,
                command.spec.semantic_id or f"context.{command.spec.command_id}",
                bool_value=command.checked,
            )

        _NODE_GRAPH_CONTEXT_MENU_BUILDER.render(
            ctx,
            (
                ContextMenuCommand(
                    "graph.add_node",
                    label=t("node_graph.add_node"),
                    payload={"gx": gx, "gy": gy},
                    semantic_id="context.add_node",
                ),
            ),
            semantic_recorder=_record_command,
        )

        _NODE_GRAPH_CONTEXT_MENU_BUILDER.render(
            ctx,
            _NODE_GRAPH_EDIT_CONTEXT_MENU,
            semantic_recorder=_record_command,
        )

# ═══════════════════════════════════════════════════════════════════════════
# Geometry helpers
# ═══════════════════════════════════════════════════════════════════════════

def _dist(x1, y1, x2, y2):
    return math.hypot(x2 - x1, y2 - y1)


def _point_segment_dist(px, py, ax, ay, bx, by):
    return _point_segment_projection(px, py, ax, ay, bx, by)[0]


def _point_segment_projection(px, py, ax, ay, bx, by):
    """Return distance to a segment and the clamped projection along it."""
    dx = bx - ax
    dy = by - ay
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-8:
        return _dist(px, py, ax, ay), 0.0
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / len_sq))
    return _dist(px, py, ax + t * dx, ay + t * dy), t
