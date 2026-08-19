"""SceneViewCameraMixin — extracted from SceneViewPanel."""
from __future__ import annotations

"""
Unity-style Scene View panel with 3D viewport and camera controls.
"""

import math
import os
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
from Infernux.lib._Infernux import (
    GIZMO_X_AXIS_ID,
    GIZMO_Y_AXIS_ID,
    GIZMO_Z_AXIS_ID,
    GIZMO_XY_PLANE_ID,
    GIZMO_XZ_PLANE_ID,
    GIZMO_YZ_PLANE_ID,
)


class SceneViewCameraMixin:
    """SceneViewCameraMixin method group for SceneViewPanel."""

    def _begin_camera_capture(self, ctx: InxGUIContext):
        if self._camera_capture_active:
            return
        self._camera_capture_restore_pos = (
            ctx.get_global_mouse_pos_x(),
            ctx.get_global_mouse_pos_y(),
        )
        InputManager.instance().set_editor_mouse_capture(True)
        from Infernux.engine.interaction import FocusService

        FocusService.instance().set_capture_owner("scene_view.camera")
        self._camera_capture_active = True

    def _end_camera_capture(self, ctx: InxGUIContext | None = None, *, restore_cursor: bool = True):
        mgr = InputManager.instance()
        if self._camera_capture_active or mgr.is_editor_mouse_capture_active:
            mgr.set_editor_mouse_capture(False)
        from Infernux.engine.interaction import FocusService

        focus = FocusService.instance()
        if focus.snapshot.capture_owner_id == "scene_view.camera":
            focus.set_capture_owner("")

        restore_pos = self._camera_capture_restore_pos
        self._camera_capture_active = False
        self._camera_capture_restore_pos = None

        if restore_cursor and ctx is not None and restore_pos is not None:
            ctx.warp_mouse_global(restore_pos[0], restore_pos[1])

    def _force_camera_input_release(self):
        if self._engine:
            self._engine.process_scene_view_input(
                0.0,
                False,
                False,
                0.0,
                0.0,
                0.0,
                False,
                False,
                False,
                False,
                False,
                False,
                False,
            )
        self._was_right_down = False
        self._was_middle_down = False

    def _process_camera_input(self, ctx: InxGUIContext, delta_time: float):
        """Process Unity-style scene camera input.

        Right/middle drag uses SDL relative mouse mode so the cursor stays
        locked during navigation and returns to its press position on release.
        """
        if not self._engine:
            return
        mgr = InputManager.instance()
        
        # Mouse button states
        right_down = mgr.get_mouse_button(1)
        middle_down = mgr.get_mouse_button(2)
        
        # Detect button just pressed
        right_just_pressed = right_down and not self._was_right_down
        middle_just_pressed = middle_down and not self._was_middle_down
        
        mouse_delta_x = 0.0
        mouse_delta_y = 0.0
        
        if (right_down or middle_down) and not right_just_pressed and not middle_just_pressed:
            if self._camera_capture_active:
                mouse_delta_x, mouse_delta_y = mgr.consume_editor_mouse_delta()
            else:
                raw_dx = ctx.get_mouse_pos_x() - self._last_mouse_x
                raw_dy = ctx.get_mouse_pos_y() - self._last_mouse_y
                if abs(raw_dx) > 0.1:
                    mouse_delta_x = raw_dx
                if abs(raw_dy) > 0.1:
                    mouse_delta_y = raw_dy

        # Keep local tracking in sync for picking and non-captured deltas.
        self._last_mouse_x = ctx.get_mouse_pos_x()
        self._last_mouse_y = ctx.get_mouse_pos_y()
        self._was_right_down = right_down
        self._was_middle_down = middle_down
        
        # Scroll wheel: zoom
        scroll_delta = ctx.get_mouse_wheel_delta()
        
        # Keyboard for fly mode (only when right mouse held)
        key_w = right_down and ctx.is_key_down(self.KEY_W)
        key_s = right_down and ctx.is_key_down(self.KEY_S)
        key_a = right_down and ctx.is_key_down(self.KEY_A)
        key_d = right_down and ctx.is_key_down(self.KEY_D)
        key_q = right_down and ctx.is_key_down(self.KEY_Q)
        key_e = right_down and ctx.is_key_down(self.KEY_E)
        key_shift = ctx.is_key_down(self.KEY_LEFT_SHIFT) or ctx.is_key_down(self.KEY_RIGHT_SHIFT)
        
        # Send to engine
        self._engine.process_scene_view_input(
            delta_time,
            right_down,
            middle_down,
            mouse_delta_x,
            mouse_delta_y,
            scroll_delta,
            key_w, key_a, key_s, key_d,
            key_q, key_e, key_shift
        )

    def _start_fly_to_orientation(self, target_yaw: float, target_pitch: float):
        """Start a smooth camera animation to a specific orientation."""
        cam = self._engine.editor_camera
        if not cam:
            return
        cur_pos = cam.position
        cur_dist = cam.focus_distance
        cur_yaw, cur_pitch = cam.rotation

        # Compute consistent focus from actual camera position to avoid
        # stale m_focusPoint causing an initial teleport/flash.
        yr = math.radians(cur_yaw)
        pr = math.radians(cur_pitch)
        cp = math.cos(pr)
        fwd = (math.sin(yr) * cp, -math.sin(pr), math.cos(yr) * cp)
        focus = (cur_pos.x + fwd[0] * cur_dist,
                 cur_pos.y + fwd[1] * cur_dist,
                 cur_pos.z + fwd[2] * cur_dist)

        self._fly_to_start_focus = focus
        self._fly_to_start_dist = cur_dist
        self._fly_to_start_yaw = cur_yaw
        self._fly_to_start_pitch = cur_pitch

        self._fly_to_target_focus = focus  # keep same focus point
        self._fly_to_target_dist = cur_dist    # keep same distance
        self._fly_to_target_yaw = target_yaw
        self._fly_to_target_pitch = target_pitch

        self._fly_to_elapsed = 0.0
        self._fly_to_duration = Theme.SCENE_ORIENT_FLY_DURATION
        self._fly_to_active = True

    def _tick_fly_to(self, dt: float):
        """Advance the fly-to animation by *dt* seconds."""
        self._fly_to_elapsed += dt
        t = min(self._fly_to_elapsed / self._fly_to_duration, 1.0)

        # Cubic ease-out for smooth deceleration
        t = 1.0 - (1.0 - t) ** 3

        # Interpolate focus point
        fx = self._fly_to_start_focus[0] + (self._fly_to_target_focus[0] - self._fly_to_start_focus[0]) * t
        fy = self._fly_to_start_focus[1] + (self._fly_to_target_focus[1] - self._fly_to_start_focus[1]) * t
        fz = self._fly_to_start_focus[2] + (self._fly_to_target_focus[2] - self._fly_to_start_focus[2]) * t

        # Interpolate distance
        dist = self._fly_to_start_dist + (self._fly_to_target_dist - self._fly_to_start_dist) * t

        # Interpolate yaw/pitch (shortest-path for yaw)
        yaw = self._lerp_angle(self._fly_to_start_yaw, self._fly_to_target_yaw, t)
        pitch = self._fly_to_start_pitch + (self._fly_to_target_pitch - self._fly_to_start_pitch) * t

        # Compute camera position from focus - forward * distance.
        yaw_rad = math.radians(yaw)
        pitch_rad = math.radians(pitch)
        cos_pitch = math.cos(pitch_rad)
        forward_x = math.sin(yaw_rad) * cos_pitch
        forward_y = -math.sin(pitch_rad)
        forward_z = math.cos(yaw_rad) * cos_pitch
        px = fx - forward_x * dist
        py = fy - forward_y * dist
        pz = fz - forward_z * dist

        self._engine.editor_camera.restore_state(
            px, py, pz, fx, fy, fz, dist, yaw, pitch
        )

        if t >= 1.0:
            self._fly_to_active = False

    def _capture_scene_camera_state(self):
        cam = self._engine.editor_camera if self._engine else None
        if cam is None:
            return None
        position = cam.position
        yaw, pitch = cam.rotation
        distance = float(cam.focus_distance)
        yaw_rad = math.radians(yaw)
        pitch_rad = math.radians(pitch)
        cos_pitch = math.cos(pitch_rad)
        forward = (
            math.sin(yaw_rad) * cos_pitch,
            -math.sin(pitch_rad),
            math.cos(yaw_rad) * cos_pitch,
        )
        return (
            float(position.x) + forward[0] * distance,
            float(position.y) + forward[1] * distance,
            float(position.z) + forward[2] * distance,
            distance,
            float(yaw),
            float(pitch),
            int(self._fly_to_last_obj_id),
            bool(self._fly_to_close),
        )

    def _animate_to_scene_camera_state(self, state) -> None:
        if state is None or not self._engine or not self._engine.editor_camera:
            return
        current = self._capture_scene_camera_state()
        if current is None:
            return
        self._fly_to_start_focus = (current[0], current[1], current[2])
        self._fly_to_start_dist = current[3]
        self._fly_to_start_yaw = current[4]
        self._fly_to_start_pitch = current[5]
        self._fly_to_target_focus = (state[0], state[1], state[2])
        self._fly_to_target_dist = state[3]
        self._fly_to_target_yaw = state[4]
        self._fly_to_target_pitch = state[5]
        self._fly_to_last_obj_id = int(state[6])
        self._fly_to_close = bool(state[7])
        self._fly_to_elapsed = 0.0
        self._fly_to_duration = 0.5
        self._fly_to_active = True

    def can_frame_object_by_id(self, object_id: int) -> bool:
        if (
            not self._engine
            or not self._engine.editor_camera
            or int(object_id or 0) <= 0
        ):
            return False
        from Infernux.lib import SceneManager

        scene = SceneManager.instance().get_active_scene()
        return bool(scene and scene.find_by_id(int(object_id)))

    def frame_object_by_id(self, object_id: int) -> bool:
        if not self.can_frame_object_by_id(object_id):
            return False
        from Infernux.lib import SceneManager

        scene = SceneManager.instance().get_active_scene()
        return self.fly_to_object(scene.find_by_id(int(object_id)))

    def fly_to_object(self, game_object):
        """Start a smooth camera animation to focus on *game_object*.

        Computes the bounding sphere of all MeshRenderers on the object
        (and children) and derives the camera distance using Unity's
        formula: distance = radius / sin(fov/2).

        World-space rotation is never changed — only the focus point and
        distance are animated so the object becomes visible from the
        current viewing direction.

        Alternates between a *far* (framing) and *close* (detail) distance
        on repeated double-clicks of the same object, like Unity.
        """
        if not self._engine or game_object is None:
            return False

        old_state = self._capture_scene_camera_state()
        if old_state is None:
            return False

        obj_id = game_object.id

        # Toggle near/far on repeated double-click of the same object
        target_close = (
            not self._fly_to_close
            if obj_id == self._fly_to_last_obj_id
            else False
        )

        center, radius = self._compute_object_bounds(game_object)

        # Target distance (Unity formula + small padding)
        cam = self._engine.editor_camera
        fov_deg = cam.fov
        half_fov_rad = math.radians(fov_deg * 0.5)
        sin_half = math.sin(half_fov_rad)
        if sin_half < 1e-6:
            sin_half = 1e-6
        far_dist = max(radius / sin_half * 1.2, 0.5)
        close_dist = far_dist * 0.4
        target_dist = close_dist if target_close else far_dist

        cur_yaw, cur_pitch = cam.rotation

        target_state = (
            float(center[0]),
            float(center[1]),
            float(center[2]),
            float(target_dist),
            float(cur_yaw),
            float(cur_pitch),
            int(obj_id),
            bool(target_close),
        )
        from Infernux.engine.interaction import ViewCommandService

        return ViewCommandService.require().set_value(
            old_state,
            target_state,
            self._animate_to_scene_camera_state,
            description="Frame Selected",
        )

    def focus_on(self, x: float, y: float, z: float, distance: float = 10.0):
        """Focus camera on a point."""
        cam = self._engine.editor_camera if self._engine else None
        if cam:
            cam.focus_on(x, y, z, distance)

    def reset_camera(self):
        """Reset camera to default position."""
        cam = self._engine.editor_camera if self._engine else None
        if cam:
            cam.reset()

    def _align_object_to_camera(self, object_id: int = 0) -> bool:
        """Align the selected object's world transform to the editor camera."""
        if not self._engine:
            return False

        obj_id = int(object_id or 0)
        if obj_id <= 0:
            from Infernux.engine.interaction import SelectionService

            obj_id = int(SelectionService.instance().primary_scene_object_id() or 0)
        if obj_id <= 0:
            obj_id = int(self._engine.get_selected_object_id() or 0)
        if obj_id <= 0:
            return False

        from Infernux.lib import SceneManager
        scene = SceneManager.instance().get_active_scene()
        if not scene:
            return False
        obj = scene.find_by_id(obj_id)
        if obj is None:
            return False

        cam = self._engine.editor_camera
        cam_pos = cam.position
        cam_yaw, cam_pitch = cam.rotation

        from Infernux.math import Vector3

        transform = obj.transform
        old_pos = (transform.position.x, transform.position.y, transform.position.z)
        old_euler = (transform.euler_angles.x, transform.euler_angles.y, transform.euler_angles.z)

        new_pos = Vector3(cam_pos.x, cam_pos.y, cam_pos.z)
        new_euler = Vector3(cam_pitch, cam_yaw, 0.0)

        from Infernux.engine.interaction import ComponentCommandService

        return ComponentCommandService.require().execute_property_changes(
            [
                (
                    transform,
                    "position",
                    Vector3(*old_pos),
                    new_pos,
                    "Align Position",
                ),
                (
                    transform,
                    "euler_angles",
                    Vector3(*old_euler),
                    new_euler,
                    "Align Rotation",
                ),
            ],
            description="Align With View",
        )

