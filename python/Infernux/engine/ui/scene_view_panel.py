"""
Unity-style Scene View panel with 3D viewport and camera controls.
"""

import math
import os
import time
from Infernux.lib import InxGUIContext, TextureLoader, InputManager
from Infernux.engine.i18n import t
from .editor_panel import EditorPanel
from .closable_panel import ClosablePanel
from .panel_registry import editor_panel
from .theme import Theme, ImGuiCol, ImGuiStyleVar
from .viewport_utils import ViewportInfo, capture_viewport_info
from . import imgui_keys as _keys
import Infernux.resources as _resources

# Gizmo handle IDs — must match C++ EditorTools constants
from Infernux.debug import Debug

_SCENE_VIEWPORT_SEMANTIC_ID = "scene_view.viewport"
from Infernux.lib._Infernux import (
    GIZMO_X_AXIS_ID,
    GIZMO_Y_AXIS_ID,
    GIZMO_Z_AXIS_ID,
    GIZMO_XY_PLANE_ID,
    GIZMO_XZ_PLANE_ID,
    GIZMO_YZ_PLANE_ID,
    GIZMO_CENTER_ID,
)

_GIZMO_IDS = {
    GIZMO_X_AXIS_ID: 1,
    GIZMO_Y_AXIS_ID: 2,
    GIZMO_Z_AXIS_ID: 3,
    GIZMO_XY_PLANE_ID: 4,
    GIZMO_XZ_PLANE_ID: 5,
    GIZMO_YZ_PLANE_ID: 6,
    GIZMO_CENTER_ID: 7,
}
_AXIS_DIRS = {1: (1.0, 0.0, 0.0), 2: (0.0, 1.0, 0.0), 3: (0.0, 0.0, 1.0)}
_PLANE_AXIS_PAIRS = {4: (1, 2), 5: (1, 3), 6: (2, 3)}
GIZMO_CENTER_HANDLE = 7

# Tool mode constants — must match C++ EditorTools::ToolMode
TOOL_NONE = 0
TOOL_TRANSLATE = 1
TOOL_ROTATE = 2
TOOL_SCALE = 3

# Unity-style snap increments used while Ctrl is held during gizmo drags.
TRANSLATE_SNAP_STEP = 1.0
ROTATE_SNAP_DEGREES = 15.0
SCALE_SNAP_FACTOR = 0.1


# ======================================================================
# Quaternion math helpers  (matches GLM convention: ZYX intrinsic order,
# euler = (pitch/X, yaw/Y, roll/Z) in degrees)
# ======================================================================

def _euler_deg_to_quat(ex, ey, ez):
    """Euler angles (degrees, YXZ intrinsic) → quaternion (w,x,y,z).

    Matches C++ ``EulerYXZToQuat``:  q = qY * qX * qZ.
    """
    rx = math.radians(ex) * 0.5
    ry = math.radians(ey) * 0.5
    rz = math.radians(ez) * 0.5
    cx, sx = math.cos(rx), math.sin(rx)
    cy, sy = math.cos(ry), math.sin(ry)
    cz, sz = math.cos(rz), math.sin(rz)
    return (
        cy * cx * cz + sy * sx * sz,   # w
        cy * sx * cz + sy * cx * sz,   # x
        sy * cx * cz - cy * sx * sz,   # y
        cy * cx * sz - sy * sx * cz,   # z
    )


def _quat_to_euler_deg(q):
    """Quaternion (w,x,y,z) → Euler angles (degrees, YXZ intrinsic).

    Matches C++ ``QuatToEulerYXZ``.
    """
    w, x, y, z = q
    # sinX = 2(w*x - y*z)
    sin_x = 2.0 * (w * x - y * z)
    if abs(sin_x) < 0.9999:
        ex = math.asin(max(-1.0, min(1.0, sin_x)))
        ey = math.atan2(2.0 * (x * z + w * y),
                        1.0 - 2.0 * (x * x + y * y))
        ez = math.atan2(2.0 * (x * y + w * z),
                        1.0 - 2.0 * (x * x + z * z))
    else:
        # Gimbal lock at pitch = ±90°
        ex = math.copysign(math.pi / 2.0, sin_x)
        ey = math.atan2(-(2.0 * (x * z - w * y)),
                        1.0 - 2.0 * (y * y + z * z))
        ez = 0.0
    return (math.degrees(ex), math.degrees(ey), math.degrees(ez))


def _quat_mul(a, b):
    """Hamilton product of two quaternions (w,x,y,z)."""
    aw, ax, ay, az = a
    bw, bx, by, bz = b
    return (
        aw * bw - ax * bx - ay * by - az * bz,
        aw * bx + ax * bw + ay * bz - az * by,
        aw * by - ax * bz + ay * bw + az * bx,
        aw * bz + ax * by - ay * bx + az * bw,
    )


def _axis_angle_to_quat(ax, ay, az, angle_deg):
    """Axis-angle → quaternion (w,x,y,z).  Axis must be unit-length."""
    half = math.radians(angle_deg) * 0.5
    s = math.sin(half)
    return (math.cos(half), ax * s, ay * s, az * s)


from ._scene_view_gizmo import SceneViewGizmoMixin
from ._scene_view_camera import SceneViewCameraMixin
from ._scene_view_overlays import SceneViewOverlaysMixin
from ._scene_view_picking import SceneViewPickingMixin
from ._scene_view_math import SceneViewMathMixin
from ._scene_view_line_tools import SceneViewLineToolsMixin

@editor_panel("Scene", type_id="scene_view", title_key="panel.scene")
class SceneViewPanel(SceneViewGizmoMixin, SceneViewCameraMixin, SceneViewOverlaysMixin, SceneViewPickingMixin, SceneViewMathMixin, SceneViewLineToolsMixin, EditorPanel):
    """
    Unity-style Scene View panel with 3D viewport and camera controls.
    
    Controls (Unity-style):
    - Right-click + drag: Rotate camera (look around)
    - Middle-click + drag: Pan camera
    - Scroll wheel: Zoom in/out (dolly)
    - Right-click + WASD: Fly mode movement
    - Right-click + QE: Up/Down in fly mode
    - Shift: Speed boost in fly mode
    """
    
    WINDOW_TYPE_ID = "scene_view"
    WINDOW_DISPLAY_NAME = "Scene"

    # Key codes imported from shared imgui_keys module
    KEY_W = _keys.KEY_W
    KEY_A = _keys.KEY_A
    KEY_S = _keys.KEY_S
    KEY_D = _keys.KEY_D
    KEY_Q = _keys.KEY_Q
    KEY_E = _keys.KEY_E
    KEY_R = _keys.KEY_R
    KEY_LEFT_SHIFT = _keys.KEY_LEFT_SHIFT
    KEY_RIGHT_SHIFT = _keys.KEY_RIGHT_SHIFT
    
    def __init__(self, title: str = "Scene", engine=None):
        super().__init__(title, window_id="scene_view")
        self._engine = engine
        self._play_mode_manager = None
        self._last_frame_time = 0.0
        self._on_object_picked = None
        
        # Scene render target size tracking
        self._last_scene_width = 0
        self._last_scene_height = 0
        
        # Mouse button state tracking for detecting press/release
        self._was_left_down = False
        self._was_right_down = False
        self._was_middle_down = False
        
        # Mouse position tracking for non-captured viewport interactions.
        self._last_mouse_x = 0.0
        self._last_mouse_y = 0.0

        # Unity-style camera capture: lock the cursor during scene-camera drag
        # and restore it to the press position on release.
        self._is_camera_dragging = False
        self._camera_capture_active = False
        self._camera_capture_restore_pos: tuple[float, float] | None = None

        # Editor gizmo drag state (shared across translate/rotate/scale)
        self._is_gizmo_dragging = False
        self._gizmo_drag_axis = 0          # 1=X, 2=Y, 3=Z, 4=XY, 5=XZ, 6=YZ
        self._gizmo_drag_axis_dir = (1.0, 0.0, 0.0)
        self._gizmo_drag_start_t = 0.0     # parameter along axis at grab (translate/scale)
        self._gizmo_drag_plane_axes = (0, 0)
        self._gizmo_drag_plane_u = (1.0, 0.0, 0.0)
        self._gizmo_drag_plane_v = (0.0, 1.0, 0.0)
        self._gizmo_drag_plane_start_uv = (0.0, 0.0)
        self._gizmo_drag_start_pos = (0.0, 0.0, 0.0)  # object pos at grab
        self._gizmo_drag_start_euler = (0.0, 0.0, 0.0)  # object euler at grab (rotate)
        self._gizmo_drag_start_rotation = None  # object world rotation quat at grab
        self._gizmo_drag_start_scale = (1.0, 1.0, 1.0)  # object local_scale at grab (scale)
        self._gizmo_drag_start_screen = (0.0, 0.0) # screen pos at grab (rotate)
        self._gizmo_drag_obj_id = 0        # object being dragged
        self._gizmo_drag_items = {}        # object_id -> start transform snapshot for multi-edit
        self._gizmo_drag_selection_snapshot = None
        self._gizmo_drag_edit_key = ""
        self._gizmo_drag_cancel_token = ""
        self._gizmo_drag_rigidbody = None  # Rigidbody temporarily driven by the gizmo
        self._gizmo_drag_rigidbodies = []   # [(Rigidbody, restore_dynamic), ...]
        self._gizmo_drag_restore_dynamic = False
        self._gizmo_snap_active = False    # Ctrl held during current drag frame
        self._gizmo_tool_mode = TOOL_TRANSLATE  # current tool mode (Python tracking)
        self._coord_space = 0  # 0=Global, 1=Local

        # Scene picking cycle state (Unity-style repeated click cycling)
        self._pick_cycle_candidates = []
        self._pick_cycle_index = -1
        self._pick_cycle_last_mouse = (-1.0, -1.0)
        self._pick_cycle_last_viewport = (0, 0)
        self._pending_scene_pick = None

        # Box-select state
        self._box_select_active = False
        self._box_select_start = (0.0, 0.0)  # screen-space start (local to viewport)
        self._box_select_end = (0.0, 0.0)
        self._box_select_vp = None  # ViewportInfo when box-select started

        # Focus tracking for view-owned interactions
        self._was_focused: bool = False
        self._on_focus_gained = None   # callback() when panel gains focus

        # Smooth camera fly-to animation state
        self._fly_to_active: bool = False
        self._fly_to_start_focus = (0.0, 0.0, 0.0)
        self._fly_to_start_dist = 0.0
        self._fly_to_start_yaw = 0.0
        self._fly_to_start_pitch = 0.0
        self._fly_to_target_focus = (0.0, 0.0, 0.0)
        self._fly_to_target_dist = 0.0
        self._fly_to_target_yaw = 0.0
        self._fly_to_target_pitch = 0.0
        self._fly_to_elapsed = 0.0
        self._fly_to_duration = 0.5  # seconds
        # Near/far toggle: alternate between two distances on repeated
        # double-clicks of the same object (like Unity).
        self._fly_to_last_obj_id: int = 0
        self._fly_to_close: bool = False

        # Tool icon texture ids (lazily loaded)
        self._tool_icon_ids: dict[int, int] = {}  # tool_mode -> tex_id
        self._tool_icons_loaded: bool = False

        # Gizmo hover pick cache — avoids expensive per-triangle raycast
        # every frame when the mouse hasn't moved.
        self._hover_pick_cache_pos: tuple[float, float] = (-1.0, -1.0)
        self._hover_pick_cache_result: int = 0

        # Edit-mode ParticleSystem preview follows the primary scene selection.
        self._particle_preview_component = None
        self._particle_preview_object = None
        self._particle_preview_speed = 1.0
        self._particle_preview_seek_time = 0.0
        self._particle_preview_seek_editing = False
        self._particle_preview_playing = False
        self._particle_preview_prepared = False
        self._particle_preview_restore_pending = False
        self._particle_preview_height = 150.0
        self._particle_preview_resize_drag = False
        self._particle_preview_resize_start_y = 0.0
        self._particle_preview_resize_start_height = 0.0
        self._particle_preview_last_tick = 0.0
        self._particle_preview_selection_service = None

        # LineRenderer scene authoring state.
        self._line_edit_mode = 0  # 0=None, 1=Edit Points, 2=Create Points
        self._line_active_component_id = 0
        self._line_show_wireframe = True
        self._line_simplify_preview = False
        self._line_simplify_tolerance = 1.0
        self._line_create_input = 0  # 0=mouse plane, 1=physics raycast
        self._line_create_layer_mask = 0x7FFFFFFF
        self._line_create_min_distance = 0.1
        self._line_create_offset = 0.0
        self._line_selected_points = set()
        self._line_point_dragging = False
        self._line_drag_plane_point = (0.0, 0.0, 0.0)
        self._line_drag_plane_normal = (0.0, 0.0, 1.0)
        self._line_drag_start_world = (0.0, 0.0, 0.0)
        self._line_drag_start_positions = []
        self._line_drag_before_document = None
        self._line_create_dragging = False
        self._line_create_before_document = None
    
    def set_engine(self, engine):
        """Set the engine reference for camera control."""
        if engine is None:
            self._end_camera_capture(restore_cursor=False)
            self._force_camera_input_release()
        self._engine = engine

    def set_play_mode_manager(self, manager):
        """Set the PlayModeManager so the panel can show play-mode border."""
        previous = self._play_mode_manager
        if previous is manager:
            return
        if previous is not None:
            previous.remove_state_change_listener(
                self._on_particle_preview_play_mode_changed
            )
        self._play_mode_manager = manager
        if manager is not None and self._enable_called:
            manager.add_state_change_listener(
                self._on_particle_preview_play_mode_changed
            )

    def set_on_object_picked(self, callback):
        """Set callback for scene object picking (receives object ID or 0)."""
        self._on_object_picked = callback

    # ------------------------------------------------------------------
    # EditorPanel hooks
    # ------------------------------------------------------------------

    def _initial_size(self):
        return (800, 600)

    def _window_flags(self) -> int:
        return Theme.WINDOW_FLAGS_VIEWPORT | Theme.WINDOW_FLAGS_NO_SCROLL

    def save_state(self) -> dict:
        # Scene view keeps many runtime-only camera/gizmo/render caches.
        # Persisting them across sessions can corrupt first-frame behavior.
        return {}

    def load_state(self, data: dict) -> None:
        # Intentionally ignore persisted data for Scene View.
        return

    def _pre_render(self, ctx) -> None:
        """Keep edit-mode particle simulation independent of Scene tab visibility."""
        now = time.monotonic()
        previous = self._particle_preview_last_tick
        self._particle_preview_last_tick = now
        delta_time = 0.0 if previous <= 0.0 else min(max(now - previous, 0.0), 0.1)
        self._tick_particle_preview(delta_time)

    def on_enable(self):
        # Reset transient per-session viewport/input caches.
        self._last_scene_width = 0
        self._last_scene_height = 0
        self._last_frame_time = 0.0
        self._particle_preview_last_tick = 0.0
        self._hover_pick_cache_pos = (-1.0, -1.0)
        self._hover_pick_cache_result = 0
        from Infernux.engine.interaction import SelectionService

        self._particle_preview_selection_service = SelectionService.instance()
        self._particle_preview_selection_service.add_listener(
            self._on_particle_preview_selection_changed
        )
        if self._play_mode_manager is not None:
            self._play_mode_manager.add_state_change_listener(
                self._on_particle_preview_play_mode_changed
            )
        self._restore_particle_preview_selection()

    def on_disable(self):
        """Panel closed — shrink render target to save GPU memory."""
        self._interrupt_gizmo_drag(commit=True)
        selection = getattr(self, "_particle_preview_selection_service", None)
        if selection is not None:
            selection.remove_listener(self._on_particle_preview_selection_changed)
        self._particle_preview_selection_service = None
        if self._play_mode_manager is not None:
            self._play_mode_manager.remove_state_change_listener(
                self._on_particle_preview_play_mode_changed
            )
        self._release_particle_preview_selection()
        self._end_camera_capture(restore_cursor=False)
        self._force_camera_input_release()
        if self._engine:
            self._engine.set_scene_view_visible(False)
            if self._last_scene_width != 1 or self._last_scene_height != 1:
                self._engine.resize_scene_render_target(1, 1)
                self._last_scene_width = 1
                self._last_scene_height = 1

    def _on_not_visible(self, ctx):
        """Window collapsed/tabbed out — mark invisible for C++ side."""
        self._interrupt_gizmo_drag(commit=True)
        self._particle_preview_resize_drag = False
        if self._engine:
            self._engine.set_scene_view_visible(False)

    def _update_visible_frame_state(self):
        import time
        current_time = time.time()
        self._delta_time = current_time - self._last_frame_time if self._last_frame_time > 0 else 0.016
        self._last_frame_time = current_time
        self._delta_time = min(self._delta_time, 0.1)

        if self._fly_to_active:
            self._tick_fly_to(self._delta_time)

        # Determine play-mode border colour
        self._play_border_clr = None
        pm = self._play_mode_manager
        if pm is None:
            from Infernux.engine.play_mode import PlayModeManager, PlayModeState
            pm = PlayModeManager.instance()
        if pm and not pm.is_edit_mode:
            from Infernux.engine.play_mode import PlayModeState
            self._play_border_clr = Theme.BORDER_PAUSE if pm.state == PlayModeState.PAUSED else Theme.BORDER_PLAY

    def _on_visible_pre(self, ctx):
        self._update_visible_frame_state()
        # Activate C++ scene rendering and request full-speed frames only
        # when the Scene View panel is actually visible.  Previously these
        # lived in _pre_render (which runs every frame for all panels) and
        # caused a pointless True/False toggle when the tab was hidden,
        # plus prevented idle-throttle even when only Game View was active.
        if self._engine:
            self._engine.set_scene_view_visible(True)
            native = self._engine.get_native_engine()
            if native:
                native.request_full_speed_frame()

        # Track focus for view-owned interactions
        focused = ClosablePanel.get_active_view_id() == self.window_id
        if not focused:
            self._interrupt_gizmo_drag(commit=True)
        if not focused and self._camera_capture_active:
            self._is_camera_dragging = False
            self._end_camera_capture(restore_cursor=False)
            self._force_camera_input_release()
        if focused and not self._was_focused:
            if self._on_focus_gained:
                self._on_focus_gained()
        self._was_focused = focused

    def on_render_content(self, ctx: InxGUIContext):
        delta_time = getattr(self, '_delta_time', 0.016)
        _play_border_clr = getattr(self, '_play_border_clr', None)

        # Get content region for scene viewport
        avail_width = ctx.get_content_region_avail_width()
        avail_height = ctx.get_content_region_avail_height()
        
        scene_width = max(int(avail_width), 64)
        scene_height = max(int(avail_height), 64)
        
        cursor_start_x = ctx.get_cursor_pos_x()
        cursor_start_y = ctx.get_cursor_pos_y()
        
        # Resize scene render target if size changed
        if self._engine and (scene_width != self._last_scene_width or scene_height != self._last_scene_height):
            self._engine.resize_scene_render_target(scene_width, scene_height)
            self._last_scene_width = scene_width
            self._last_scene_height = scene_height
        
        # Get and display scene texture
        scene_texture_id = 0
        if self._engine:
            scene_texture_id = self._engine.get_scene_texture_id()
        
        if scene_texture_id != 0:
            ctx.image(scene_texture_id, float(scene_width), float(scene_height), 0.0, 0.0, 1.0, 1.0)

            # Give the rendered image a real interaction item. This keeps
            # human clicks and MCP synthetic SDL input on the same surface.
            ctx.set_cursor_pos_x(cursor_start_x)
            ctx.set_cursor_pos_y(cursor_start_y)
            # The Scene View draws toolbar, Prefab Mode, and particle-preview
            # controls over this full-viewport item. Let those later items own
            # input inside their rectangles instead of being blocked by the
            # viewport hit target underneath.
            ctx.set_next_item_allow_overlap()
            ctx.invisible_button("##SceneViewportInput", float(scene_width), float(scene_height))
            ctx.record_semantic_item(
                "viewport", "Scene Viewport", True, _SCENE_VIEWPORT_SEMANTIC_ID
            )

            vp = capture_viewport_info(ctx)
            is_scene_hovered = vp.is_hovered
            overlay_hovered = self._render_overlays_and_shortcuts(
                ctx,
                vp,
                cursor_start_x,
                cursor_start_y,
                scene_width,
                scene_height,
                delta_time,
            )

            gizmo_consumed = self._process_gizmo_and_camera(
                ctx, vp, delta_time, is_scene_hovered, overlay_hovered)

            self._handle_picking_and_selection(
                ctx, vp, gizmo_consumed, overlay_hovered, is_scene_hovered, _play_border_clr)

        else:
            # Placeholder when texture not ready
            ctx.invisible_button("scene_placeholder", float(scene_width), float(scene_height))
            ctx.set_cursor_pos_x(cursor_start_x + 8)
            ctx.set_cursor_pos_y(cursor_start_y + 8)
            ctx.label(t("scene_view.loading"))

    # ------------------------------------------------------------------
    # Orientation Gizmo  (Unity-style axis widget)
    # ------------------------------------------------------------------

    # Orientation gizmo constants now come from Theme
    _GIZMO_AXIS_COLORS = {
        'X': (0.9, 0.2, 0.2),      # red
        'Y': (0.2, 0.85, 0.2),     # green
        'Z': (0.3, 0.4, 0.95),     # blue
    }
    _GIZMO_AXIS_VIEWS = {
        # axis_label: (yaw, pitch) — camera looks FROM that axis direction
        # pitch convention: forward.y = -sin(pitch), so +pitch = look down
        '+X': (-90.0, 0.0),
        '-X': (90.0, 0.0),
        '+Y': (0.0, 89.9),    # look straight down (avoid exact 90 for gimbal lock)
        '-Y': (0.0, -89.9),   # look straight up
        '+Z': (180.0, 0.0),
        '-Z': (0.0, 0.0),
    }

    # ------------------------------------------------------------------
    # Tool mode management
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Gizmo interaction helpers (all in Python)
    # ------------------------------------------------------------------

    @staticmethod
    def _snap_delta(delta: float, step: float) -> float:
        """Quantize a signed delta to fixed increments."""
        if step <= 1e-8:
            return delta
        return round(delta / step) * step

    @staticmethod
    def _plane_factor(current: float, start: float) -> float:
        if abs(start) < 1e-6:
            return 1.0 + (current - start)
        return current / start

    # ------------------------------------------------------------------
    # Mode-specific drag handlers
    # ------------------------------------------------------------------

    @staticmethod
    def _vec3_approx_equal(a, b, eps=1e-5):
        """Check if two Vector3-like objects are approximately equal."""
        return (abs(a[0] - b[0]) < eps and
                abs(a[1] - b[1]) < eps and
                abs(a[2] - b[2]) < eps)

    # ------------------------------------------------------------------
    # Smooth camera fly-to (animated frame-selected)
    # ------------------------------------------------------------------

    @staticmethod
    def _lerp_angle(a: float, b: float, t: float) -> float:
        """Linearly interpolate between angles (degrees) via shortest path."""
        diff = ((b - a + 180.0) % 360.0) - 180.0
        return a + diff * t

    @staticmethod
    def _compute_object_bounds(game_object):
        """Compute bounding-sphere center and radius for *game_object*.

        Merges world-space AABBs of all MeshRenderers on the object and its
        children.  Falls back to the transform position with a default radius
        if no renderers exist.
        """
        bmin = [float('inf')] * 3
        bmax = [float('-inf')] * 3
        found = False

        def _collect(obj):
            nonlocal found
            mr = obj.get_cpp_component("MeshRenderer")
            if mr is not None:
                try:
                    bounds = mr.get_world_bounds()
                    if bounds and len(bounds) == 6:
                        for i in range(3):
                            if bounds[i] < bmin[i]:
                                bmin[i] = bounds[i]
                            if bounds[i + 3] > bmax[i]:
                                bmax[i] = bounds[i + 3]
                        found = True
                except Exception as _exc:
                    Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                    pass
            for child in obj.get_children():
                _collect(child)

        _collect(game_object)

        if found:
            cx = (bmin[0] + bmax[0]) * 0.5
            cy = (bmin[1] + bmax[1]) * 0.5
            cz = (bmin[2] + bmax[2]) * 0.5
            dx = bmax[0] - bmin[0]
            dy = bmax[1] - bmin[1]
            dz = bmax[2] - bmin[2]
            radius = math.sqrt(dx * dx + dy * dy + dz * dz) * 0.5
            return (cx, cy, cz), max(radius, 0.1)

        # Fallback: use transform position with a default radius
        pos = game_object.transform.position
        return (pos.x, pos.y, pos.z), 1.0

    @staticmethod
    def _vector3_to_tuple(vec) -> tuple[float, float, float]:
        return (float(vec.x), float(vec.y), float(vec.z))

    @staticmethod
    def _normalize3(vec):
        x, y, z = vec
        length = math.sqrt(x * x + y * y + z * z)
        if length < 1e-6:
            return None
        return (x / length, y / length, z / length)

    @classmethod
    def _planar_visible_side(cls, obj, mr):
        """Return a preferred world-space viewing side for flat one-sided meshes."""
        try:
            positions = mr.get_positions()
            indices = mr.get_indices()
        except Exception as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            return None

        if not positions or len(indices) < 3:
            return None

        sum_x = 0.0
        sum_y = 0.0
        sum_z = 0.0
        total_area2 = 0.0
        tri_count = len(indices) // 3
        for tri in range(tri_count):
            i0 = indices[tri * 3]
            i1 = indices[tri * 3 + 1]
            i2 = indices[tri * 3 + 2]
            if i0 >= len(positions) or i1 >= len(positions) or i2 >= len(positions):
                continue
            p0 = positions[i0]
            p1 = positions[i1]
            p2 = positions[i2]
            ax = p1[0] - p0[0]
            ay = p1[1] - p0[1]
            az = p1[2] - p0[2]
            bx = p2[0] - p0[0]
            by = p2[1] - p0[1]
            bz = p2[2] - p0[2]
            nx = ay * bz - az * by
            ny = az * bx - ax * bz
            nz = ax * by - ay * bx
            area2 = math.sqrt(nx * nx + ny * ny + nz * nz)
            if area2 < 1e-8:
                continue
            sum_x += nx
            sum_y += ny
            sum_z += nz
            total_area2 += area2

        if total_area2 < 1e-6:
            return None

        normal = cls._normalize3((sum_x, sum_y, sum_z))
        if normal is None:
            return None

        coherence = math.sqrt(sum_x * sum_x + sum_y * sum_y + sum_z * sum_z) / total_area2
        if coherence < 0.98:
            return None

        try:
            material = mr.get_effective_material(0)
            render_state = material.get_render_state() if material is not None else None
        except Exception:
            render_state = None

        if render_state is None:
            return None

        cull_mode = int(getattr(render_state, 'cull_mode', 0))
        if cull_mode == 0:
            return None

        try:
            front_face = int(getattr(render_state, 'front_face', 1))
        except (TypeError, ValueError):
            front_face = 1
        front_sign = -1.0 if front_face == 1 else 1.0
        visible_sign = front_sign if cull_mode == 2 else -front_sign
        local_side = (
            normal[0] * visible_sign,
            normal[1] * visible_sign,
            normal[2] * visible_sign,
        )

        try:
            from Infernux.math import Vector3
            world_side_vec = obj.transform.transform_direction(Vector3(*local_side))
            world_side = cls._vector3_to_tuple(world_side_vec)
        except Exception as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            return None

        return cls._normalize3(world_side)

    @classmethod
    def _preferred_focus_angles(cls, game_object):
        """Return yaw/pitch override for flat one-sided meshes when possible."""
        world_sides = []

        def _collect(obj):
            mr = obj.get_cpp_component("MeshRenderer")
            if mr is not None:
                side = cls._planar_visible_side(obj, mr)
                if side is not None:
                    world_sides.append(side)
            for child in obj.get_children():
                _collect(child)

        _collect(game_object)
        if not world_sides:
            return None

        sx = sum(side[0] for side in world_sides)
        sy = sum(side[1] for side in world_sides)
        sz = sum(side[2] for side in world_sides)
        visible_side = cls._normalize3((sx, sy, sz))
        if visible_side is None:
            return None

        forward = (-visible_side[0], -visible_side[1], -visible_side[2])
        yaw = math.degrees(math.atan2(forward[0], forward[2]))
        # C++ convention: forward.y = -sin(pitch), so pitch = asin(-forward.y)
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, -forward[1]))))
        return yaw, pitch

