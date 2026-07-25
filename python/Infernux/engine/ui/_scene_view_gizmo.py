"""SceneViewGizmoMixin — extracted from SceneViewPanel."""
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

# Tool mode constants — imported from scene_view_panel
from .scene_view_panel import (
    TOOL_NONE, TOOL_TRANSLATE, TOOL_ROTATE, TOOL_SCALE,
    TRANSLATE_SNAP_STEP, ROTATE_SNAP_DEGREES, SCALE_SNAP_FACTOR,
    _GIZMO_IDS, _AXIS_DIRS, _PLANE_AXIS_PAIRS, GIZMO_CENTER_HANDLE,
)

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


def _rotate_vector_by_quat(q, v):
    vx, vy, vz = float(v[0]), float(v[1]), float(v[2])
    qx, qy, qz, qw = float(q.x), float(q.y), float(q.z), float(q.w)

    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)

    return (
        vx + qw * tx + (qy * tz - qz * ty),
        vy + qw * ty + (qz * tx - qx * tz),
        vz + qw * tz + (qx * ty - qy * tx),
    )


class SceneViewGizmoMixin:
    """SceneViewGizmoMixin method group for SceneViewPanel."""

    def _finish_gizmo_drag(self, mode: int, *, record_undo: bool):
        if record_undo:
            self._record_gizmo_undo(mode)

        rb_entries = list(getattr(self, "_gizmo_drag_rigidbodies", []) or [])
        if not rb_entries and self._gizmo_drag_rigidbody is not None:
            rb_entries = [(self._gizmo_drag_rigidbody, self._gizmo_drag_restore_dynamic)]

        if rb_entries:
            from Infernux.lib._Infernux import Physics

            Physics.sync_transforms()

        self._is_gizmo_dragging = False
        self._gizmo_snap_active = False
        self._gizmo_drag_rigidbody = None
        self._gizmo_drag_rigidbodies = []
        self._gizmo_drag_restore_dynamic = False
        self._gizmo_drag_items = {}

        for rb, restore_dynamic in rb_entries:
            if not restore_dynamic or rb is None:
                continue
            from Infernux.lib._Infernux import Vector3

            rb.is_kinematic = False
            rb.velocity = Vector3(0.0, 0.0, 0.0)
            rb.angular_velocity = Vector3(0.0, 0.0, 0.0)
            rb.wake_up()

        if self._engine:
            self._engine.set_editor_tool_highlight(0)

    def _begin_gizmo_rigidbody_drive(self, obj):
        self._gizmo_drag_rigidbody = None
        self._gizmo_drag_restore_dynamic = False

        scene = obj.scene if obj is not None else None
        if scene is None or not scene.is_playing():
            return

        rb = obj.get_component("Rigidbody")
        if rb is None:
            return

        self._gizmo_drag_rigidbody = rb
        if not bool(getattr(rb, "is_kinematic", False)):
            rb.is_kinematic = True
            rb.wake_up()
            self._gizmo_drag_restore_dynamic = True

    def _begin_gizmo_rigidbody_drive_many(self, objects):
        self._gizmo_drag_rigidbody = None
        self._gizmo_drag_rigidbodies = []
        self._gizmo_drag_restore_dynamic = False

        for obj in objects:
            scene = obj.scene if obj is not None else None
            if scene is None or not scene.is_playing():
                continue
            rb = obj.get_component("Rigidbody")
            if rb is None:
                continue
            restore_dynamic = False
            if not bool(getattr(rb, "is_kinematic", False)):
                rb.is_kinematic = True
                rb.wake_up()
                restore_dynamic = True
            self._gizmo_drag_rigidbodies.append((rb, restore_dynamic))

        if self._gizmo_drag_rigidbodies:
            self._gizmo_drag_rigidbody, self._gizmo_drag_restore_dynamic = self._gizmo_drag_rigidbodies[0]

    def _sync_gizmo_rigidbody_transforms(self):
        if getattr(self, "_gizmo_drag_rigidbodies", None):
            from Infernux.lib._Infernux import Physics

            Physics.sync_transforms()

    def _apply_gizmo_position(self, obj, new_pos):
        from Infernux.lib._Infernux import Physics, Vector3

        rb = self._gizmo_drag_rigidbody
        target = Vector3(new_pos[0], new_pos[1], new_pos[2])
        obj.transform.position = target
        if rb is not None:
            Physics.sync_transforms()

    def _apply_gizmo_rotation(self, obj, rotation):
        from Infernux.lib._Infernux import Physics

        rb = self._gizmo_drag_rigidbody
        obj.transform.rotation = rotation
        if rb is not None:
            Physics.sync_transforms()

    def _get_gizmo_drag_objects(self, scene, primary_id: int):
        if scene is None:
            return []
        ids = []
        try:
            from Infernux.engine.ui.selection_manager import SelectionManager
            ids = list(SelectionManager.instance().get_ids() or [])
        except Exception:
            ids = []
        if primary_id and primary_id not in ids:
            ids.insert(0, primary_id)
        if not ids and primary_id:
            ids = [primary_id]

        objects = []
        seen = set()
        for oid in ids:
            if not oid or oid in seen:
                continue
            seen.add(oid)
            obj = scene.find_by_id(oid)
            if obj is not None:
                objects.append(obj)
        return objects

    def _snapshot_gizmo_object(self, obj):
        p = obj.transform.position
        e = obj.transform.euler_angles
        s = obj.transform.local_scale
        return {
            "pos": (p[0], p[1], p[2]),
            "euler": (e[0], e[1], e[2]),
            "rotation": obj.transform.rotation,
            "scale": (s[0], s[1], s[2]),
        }

    def _for_each_gizmo_drag_object(self, scene):
        for oid, snapshot in (getattr(self, "_gizmo_drag_items", {}) or {}).items():
            obj = scene.find_by_id(oid) if scene else None
            if obj is not None:
                yield oid, obj, snapshot

    def _process_gizmo_and_camera(self, ctx, vp, delta_time, is_scene_hovered, overlay_hovered):
        """Handle gizmo interaction and camera drag. Returns whether gizmo consumed the input."""
        left_down = ctx.is_mouse_button_down(0)
        left_clicked = left_down and not self._was_left_down
        self._was_left_down = left_down
        gizmo_consumed = False

        if self._engine:
            local_mx, local_my = vp.mouse_local(ctx)
            gizmo_consumed = self._update_gizmo_interaction(
                ctx, local_mx, local_my, vp.width, vp.height,
                left_down, left_clicked, is_scene_hovered)

        # Camera drag
        mgr = InputManager.instance()
        right_down = mgr.get_mouse_button(1)
        middle_down = mgr.get_mouse_button(2)
        if is_scene_hovered and not overlay_hovered and (right_down or middle_down) and not self._is_camera_dragging:
            self._is_camera_dragging = True
            self._fly_to_active = False
            self._begin_camera_capture(ctx)

        if is_scene_hovered or self._is_camera_dragging:
            self._process_camera_input(ctx, delta_time)

        if self._is_camera_dragging and not right_down and not middle_down:
            self._is_camera_dragging = False
            self._end_camera_capture(ctx)

        return gizmo_consumed

    def _start_gizmo_drag(self, engine, handle, ctx, local_mx, local_my,
                          scene_w, scene_h, mode):
        """Initialize gizmo drag state.  Returns ``False`` if blocked (prefab child)."""
        from Infernux.lib._Infernux import SceneManager as _SM
        scene = _SM.instance().get_active_scene()
        sel_id = engine.get_selected_object_id()
        selected_objects = self._get_gizmo_drag_objects(scene, sel_id)
        if selected_objects:
            for _obj in selected_objects:
                _is_prefab_child = (
                    bool(getattr(_obj, 'prefab_guid', None))
                    and not bool(getattr(_obj, 'prefab_root', False))
                )
                if _is_prefab_child:
                    return False

        self._is_gizmo_dragging = True
        self._gizmo_drag_axis = handle
        self._gizmo_snap_active = self._is_ctrl_down(ctx)
        self._gizmo_drag_start_screen = (local_mx, local_my)
        obj_pos = (0.0, 0.0, 0.0)
        obj_euler = (0.0, 0.0, 0.0)
        obj_rot = None
        obj_scale = (1.0, 1.0, 1.0)
        self._gizmo_drag_items = {}
        if selected_objects:
            primary = selected_objects[0]
            for obj in selected_objects:
                self._gizmo_drag_items[int(obj.id)] = self._snapshot_gizmo_object(obj)
            if primary:
                sel_id = int(primary.id)
                obj = primary
                p = obj.transform.position
                obj_pos = (p[0], p[1], p[2])
                e = obj.transform.euler_angles
                obj_euler = (e[0], e[1], e[2])
                obj_rot = obj.transform.rotation
                s = obj.transform.local_scale
                obj_scale = (s[0], s[1], s[2])
                basis_axes = self._gizmo_basis_axes(obj)
                self._begin_gizmo_rigidbody_drive_many(selected_objects)
        else:
            basis_axes = self._gizmo_basis_axes(None)

        if handle in _PLANE_AXIS_PAIRS:
            plane_axes = _PLANE_AXIS_PAIRS[handle]
            self._gizmo_drag_plane_axes = plane_axes
            self._gizmo_drag_plane_u = basis_axes[plane_axes[0]]
            self._gizmo_drag_plane_v = basis_axes[plane_axes[1]]
            start_uv = self._plane_hit_coords(
                engine, local_mx, local_my, scene_w, scene_h,
                obj_pos, self._gizmo_drag_plane_u, self._gizmo_drag_plane_v,
            )
            self._gizmo_drag_plane_start_uv = start_uv if start_uv is not None else (0.0, 0.0)
            self._gizmo_drag_axis_dir = self._gizmo_drag_plane_u
        else:
            self._gizmo_drag_plane_axes = (0, 0)
            self._gizmo_drag_plane_start_uv = (0.0, 0.0)
            self._gizmo_drag_axis_dir = basis_axes.get(handle, _AXIS_DIRS[1])
        self._gizmo_drag_obj_id = sel_id
        self._gizmo_drag_start_pos = obj_pos
        self._gizmo_drag_start_euler = obj_euler
        self._gizmo_drag_start_rotation = obj_rot
        self._gizmo_drag_start_scale = obj_scale

        if handle == GIZMO_CENTER_HANDLE:
            # Uniform scale: radial distance on the camera-facing plane.
            self._gizmo_drag_start_t = self._view_plane_radial_dist(
                engine, local_mx, local_my, scene_w, scene_h, obj_pos)
        elif mode in (TOOL_TRANSLATE, TOOL_SCALE) and handle not in _PLANE_AXIS_PAIRS:
            ray = engine.screen_to_world_ray(local_mx, local_my, scene_w, scene_h)
            self._gizmo_drag_start_t = self._closest_param_on_axis(
                ray[:3], ray[3:], self._gizmo_drag_start_pos, self._gizmo_drag_axis_dir)

        return True

    def _update_gizmo_interaction(self, ctx, local_mx, local_my, scene_w, scene_h,
                                   left_down, left_clicked, is_hovered):
        """Python-side hover highlight + axis-constrained drag for all tool modes.

        Returns True if the gizmo consumed the input this frame.
        """
        engine = self._engine
        if not engine:
            return False

        mode = self._gizmo_tool_mode
        if mode == TOOL_NONE:
            return False

        # -----------------------------------------------------------
        # DRAG CONTINUATION (dispatches to mode-specific handler)
        # -----------------------------------------------------------
        if self._is_gizmo_dragging:
            if not left_down:
                self._finish_gizmo_drag(mode, record_undo=True)
                return False

            self._gizmo_snap_active = self._is_ctrl_down(ctx)

            if mode == TOOL_TRANSLATE:
                self._drag_translate(engine, local_mx, local_my, scene_w, scene_h)
            elif mode == TOOL_ROTATE:
                self._drag_rotate(engine, local_mx, local_my, scene_w, scene_h)
            elif mode == TOOL_SCALE:
                self._drag_scale(engine, local_mx, local_my, scene_w, scene_h)

            return True  # consumed

        # -----------------------------------------------------------
        # HOVER DETECTION (using existing picking infrastructure)
        # -----------------------------------------------------------
        if not is_hovered:
            engine.set_editor_tool_highlight(0)
            self._hover_pick_cache_pos = (-1.0, -1.0)
            return False

        pos_key = (local_mx, local_my)
        if pos_key == self._hover_pick_cache_pos:
            picked = self._hover_pick_cache_result
        else:
            picked = engine.pick_gizmo_axis(local_mx, local_my, scene_w, scene_h)
            self._hover_pick_cache_pos = pos_key
            self._hover_pick_cache_result = picked

        handle = _GIZMO_IDS.get(picked, 0)
        engine.set_editor_tool_highlight(handle)

        if handle == 0:
            return False  # not hovering any gizmo handle

        # -----------------------------------------------------------
        # DRAG START
        # -----------------------------------------------------------
        if left_clicked:
            self._start_gizmo_drag(engine, handle, ctx, local_mx, local_my,
                                   scene_w, scene_h, mode)
            return True  # consumed (even if blocked by prefab guard)

        return True  # hovering a gizmo handle — consume to suppress picking

    def _gizmo_basis_axes(self, obj=None):
        if self._coord_space == 1 and obj is not None:
            r = obj.transform.right
            u = obj.transform.up
            f = obj.transform.forward
            return {
                1: (r[0], r[1], r[2]),
                2: (u[0], u[1], u[2]),
                3: (f[0], f[1], f[2]),
            }
        return dict(_AXIS_DIRS)

    def _plane_hit_coords(self, engine, local_mx, local_my, scene_w, scene_h, plane_origin, axis_u, axis_v):
        ray = engine.screen_to_world_ray(local_mx, local_my, scene_w, scene_h)
        ray_o = ray[:3]
        ray_d = ray[3:]
        normal = self._cross3(axis_u, axis_v)
        denom = self._dot3(ray_d, normal)
        if abs(denom) < 1e-8:
            return None

        t = self._dot3(self._sub3(plane_origin, ray_o), normal) / denom
        if t < 0.0:
            return None

        hit = self._add3(ray_o, self._scale3(ray_d, t))
        rel = self._sub3(hit, plane_origin)
        return (self._dot3(rel, axis_u), self._dot3(rel, axis_v))

    def _view_plane_radial_dist(self, engine, local_mx, local_my, scene_w, scene_h, plane_origin) -> float:
        """Distance from *plane_origin* to the mouse ray's hit on the camera-facing plane."""
        ray = engine.screen_to_world_ray(local_mx, local_my, scene_w, scene_h)
        ray_o = ray[:3]
        ray_d = ray[3:]
        to_cam = self._sub3(ray_o, plane_origin)
        length = math.sqrt(self._dot3(to_cam, to_cam))
        if length < 1e-8:
            return 0.0
        normal = self._scale3(to_cam, 1.0 / length)
        denom = self._dot3(ray_d, normal)
        if abs(denom) < 1e-8:
            return 0.0
        t = self._dot3(self._sub3(plane_origin, ray_o), normal) / denom
        if t < 0.0:
            return 0.0
        hit = self._add3(ray_o, self._scale3(ray_d, t))
        rel = self._sub3(hit, plane_origin)
        return math.sqrt(max(0.0, self._dot3(rel, rel)))

    def _gizmo_world_scale(self, engine, obj_pos) -> float:
        """Match C++ EditorTools visual scale: camDist * 0.15 * handleSize(1.75)."""
        cam = engine.editor_camera.position
        dx = float(cam.x) - float(obj_pos[0])
        dy = float(cam.y) - float(obj_pos[1])
        dz = float(cam.z) - float(obj_pos[2])
        dist = math.sqrt(dx * dx + dy * dy + dz * dz)
        return max(dist * 0.15 * 1.75, 0.01)

    def _is_ctrl_down(self, ctx: InxGUIContext) -> bool:
        return ctx.is_key_down(_keys.KEY_LEFT_CTRL) or ctx.is_key_down(_keys.KEY_RIGHT_CTRL)

    def _record_gizmo_undo(self, mode: int):
        """Record an undo command for the gizmo drag that just finished."""
        from Infernux.lib._Infernux import SceneManager as _SM, Vector3
        from Infernux.engine.undo import UndoManager, SetPropertyCommand, CompoundCommand

        scene = _SM.instance().get_active_scene()
        if not scene or not self._gizmo_drag_obj_id:
            return

        snapshots = getattr(self, "_gizmo_drag_items", {}) or {}
        if not snapshots:
            obj = scene.find_by_id(self._gizmo_drag_obj_id)
            if not obj:
                return
            snapshots = {self._gizmo_drag_obj_id: {
                "pos": self._gizmo_drag_start_pos,
                "euler": self._gizmo_drag_start_euler,
                "scale": self._gizmo_drag_start_scale,
            }}

        commands = []
        desc = "Transform"
        prop_name = "position"
        old_key = "pos"

        if mode == TOOL_TRANSLATE:
            desc = "Translate"
            prop_name = "position"
            old_key = "pos"
        elif mode == TOOL_ROTATE:
            desc = "Rotate"
            prop_name = "euler_angles"
            old_key = "euler"
        elif mode == TOOL_SCALE:
            desc = "Scale"
            prop_name = "local_scale"
            old_key = "scale"
        else:
            return

        for oid, snapshot in snapshots.items():
            obj = scene.find_by_id(oid)
            if not obj:
                continue
            transform = obj.transform
            if mode == TOOL_ROTATE:
                for rotate_prop, rotate_key in (("euler_angles", "euler"), ("position", "pos")):
                    old_val = Vector3(*snapshot[rotate_key])
                    new_val_raw = getattr(transform, rotate_prop)
                    new_val = Vector3(new_val_raw[0], new_val_raw[1], new_val_raw[2])
                    if not self._vec3_approx_equal(old_val, new_val):
                        commands.append(SetPropertyCommand(transform, rotate_prop, old_val, new_val, desc))
                continue

            old_val = Vector3(*snapshot[old_key])
            new_val_raw = getattr(transform, prop_name)
            new_val = Vector3(new_val_raw[0], new_val_raw[1], new_val_raw[2])
            if self._vec3_approx_equal(old_val, new_val):
                continue
            commands.append(SetPropertyCommand(transform, prop_name, old_val, new_val, desc))

        if not commands:
            return
        cmd = commands[0] if len(commands) == 1 else CompoundCommand(commands, desc)
        UndoManager.instance().record(cmd)

    def _set_tool_mode(self, mode: int):
        """Switch the active editor tool (syncs to C++ and resets drag)."""
        if mode == self._gizmo_tool_mode:
            return
        if self._is_gizmo_dragging:
            self._finish_gizmo_drag(self._gizmo_tool_mode, record_undo=False)
        self._gizmo_tool_mode = mode
        if self._engine:
            self._engine.set_editor_tool_mode(mode)
            self._engine.set_editor_tool_highlight(0)

    def _drag_translate(self, engine, local_mx, local_my, scene_w, scene_h):
        """Axis-constrained translation: project mouse ray onto drag axis."""
        if self._gizmo_drag_axis in _PLANE_AXIS_PAIRS:
            uv = self._plane_hit_coords(
                engine,
                local_mx,
                local_my,
                scene_w,
                scene_h,
                self._gizmo_drag_start_pos,
                self._gizmo_drag_plane_u,
                self._gizmo_drag_plane_v,
            )
            if uv is None:
                return

            du = uv[0] - self._gizmo_drag_plane_start_uv[0]
            dv = uv[1] - self._gizmo_drag_plane_start_uv[1]
            if self._gizmo_snap_active:
                du = self._snap_delta(du, TRANSLATE_SNAP_STEP)
                dv = self._snap_delta(dv, TRANSLATE_SNAP_STEP)

            delta_u = self._scale3(self._gizmo_drag_plane_u, du)
            delta_v = self._scale3(self._gizmo_drag_plane_v, dv)
            new_pos = self._add3(self._gizmo_drag_start_pos, self._add3(delta_u, delta_v))
            delta_pos = self._sub3(new_pos, self._gizmo_drag_start_pos)

            from Infernux.lib._Infernux import SceneManager as _SM
            scene = _SM.instance().get_active_scene()
            if scene:
                from Infernux.lib._Infernux import Vector3
                for _oid, obj, snapshot in self._for_each_gizmo_drag_object(scene):
                    sp = snapshot["pos"]
                    target = self._add3(sp, delta_pos)
                    obj.transform.position = Vector3(target[0], target[1], target[2])
                self._sync_gizmo_rigidbody_transforms()
            return

        ray = engine.screen_to_world_ray(local_mx, local_my, scene_w, scene_h)
        ad = self._gizmo_drag_axis_dir
        sp = self._gizmo_drag_start_pos

        cur_t = self._closest_param_on_axis(ray[:3], ray[3:], sp, ad)
        delta = cur_t - self._gizmo_drag_start_t
        if self._gizmo_snap_active:
            delta = self._snap_delta(delta, TRANSLATE_SNAP_STEP)

        new_pos = (sp[0] + ad[0] * delta,
                   sp[1] + ad[1] * delta,
                   sp[2] + ad[2] * delta)
        delta_pos = self._sub3(new_pos, sp)
        from Infernux.lib._Infernux import SceneManager as _SM
        scene = _SM.instance().get_active_scene()
        if scene:
            from Infernux.lib._Infernux import Vector3
            for _oid, obj, snapshot in self._for_each_gizmo_drag_object(scene):
                start = snapshot["pos"]
                target = self._add3(start, delta_pos)
                obj.transform.position = Vector3(target[0], target[1], target[2])
            self._sync_gizmo_rigidbody_transforms()

    def _drag_rotate(self, engine, local_mx, local_my, scene_w, scene_h):
        """Rotation around the drag axis (world or local depending on coord space)."""
        # Screen-space delta from drag start → rotation angle.
        # 200 pixels of horizontal movement ≈ 180°, like Unity.
        dx = local_mx - self._gizmo_drag_start_screen[0]

        ad = self._gizmo_drag_axis_dir  # world-space axis (global or local)

        # Camera-relative sign correction so the visible ring always follows
        # the mouse drag direction.
        #
        # Derivation: the front-most point on the ring (nearest the camera)
        # moves by  δθ · cross(A, P_front).  The horizontal screen component
        # of that movement must have the same sign as the mouse dx.
        # Working through the projection math:
        #   sign = sign( dot(A, camera_up) )
        # where camera_up = cross(camera_right, view_fwd) and
        #       camera_right = normalize(cross(view_fwd, world_up)).
        cam_pos = engine.editor_camera.position
        op = self._gizmo_drag_start_pos
        vf = (op[0] - cam_pos.x, op[1] - cam_pos.y, op[2] - cam_pos.z)
        vf_len = math.sqrt(vf[0]**2 + vf[1]**2 + vf[2]**2)
        if vf_len > 1e-9:
            vf = (vf[0]/vf_len, vf[1]/vf_len, vf[2]/vf_len)
            # camera_right = normalize(cross(view_fwd, world_up=(0,1,0)))
            #              = normalize((-vf_z, 0, vf_x))
            cr_x, cr_z = -vf[2], vf[0]
            cr_len = math.sqrt(cr_x**2 + cr_z**2)
            if cr_len > 1e-9:
                cr = (cr_x/cr_len, 0.0, cr_z/cr_len)
            else:
                cr = (1.0, 0.0, 0.0)  # camera looking straight up/down
            # camera_up = cross(camera_right, view_fwd)
            cu = (cr[1]*vf[2] - cr[2]*vf[1],
                  cr[2]*vf[0] - cr[0]*vf[2],
                  cr[0]*vf[1] - cr[1]*vf[0])
            sign_val = ad[0]*cu[0] + ad[1]*cu[1] + ad[2]*cu[2]
            sign = 1.0 if sign_val >= 0 else -1.0
        else:
            sign = 1.0

        angle_deg = -dx * (180.0 / 200.0) * sign
        if self._gizmo_snap_active:
            angle_deg = self._snap_delta(angle_deg, ROTATE_SNAP_DEGREES)

        q_start = self._gizmo_drag_start_rotation
        if q_start is None:
            return

        from Infernux.lib._Infernux import SceneManager as _SM, Vector3, quatf

        q_delta = quatf.angle_axis(angle_deg, Vector3(ad[0], ad[1], ad[2]))

        # Always pre-multiply: the axis in q_delta is already expressed in
        # world space for both Global mode (world unit axis) and Local mode
        # (object's local axis mapped to world space).
        scene = _SM.instance().get_active_scene()
        if scene:
            pivot = self._gizmo_drag_start_pos
            for _oid, obj, snapshot in self._for_each_gizmo_drag_object(scene):
                start_rot = snapshot.get("rotation")
                if start_rot is None:
                    continue
                q_new = q_delta * start_rot
                obj.transform.rotation = q_new
                start_pos = snapshot["pos"]
                rel = Vector3(start_pos[0] - pivot[0], start_pos[1] - pivot[1], start_pos[2] - pivot[2])
                rotated_rel = _rotate_vector_by_quat(q_delta, rel)
                obj.transform.position = Vector3(
                    pivot[0] + rotated_rel[0],
                    pivot[1] + rotated_rel[1],
                    pivot[2] + rotated_rel[2],
                )
            self._sync_gizmo_rigidbody_transforms()

    def _drag_scale_plane(self, engine, local_mx, local_my, scene_w, scene_h):
        """Scale along two axes defined by the selected plane handle."""
        uv = self._plane_hit_coords(
            engine, local_mx, local_my, scene_w, scene_h,
            self._gizmo_drag_start_pos,
            self._gizmo_drag_plane_u, self._gizmo_drag_plane_v,
        )
        if uv is None:
            return

        factor_u = self._plane_factor(uv[0], self._gizmo_drag_plane_start_uv[0])
        factor_v = self._plane_factor(uv[1], self._gizmo_drag_plane_start_uv[1])
        if self._gizmo_snap_active:
            factor_u = 1.0 + self._snap_delta(factor_u - 1.0, SCALE_SNAP_FACTOR)
            factor_v = 1.0 + self._snap_delta(factor_v - 1.0, SCALE_SNAP_FACTOR)
        factor_u = max(factor_u, 0.01)
        factor_v = max(factor_v, 0.01)

        from Infernux.lib._Infernux import SceneManager as _SM, Vector3
        scene = _SM.instance().get_active_scene()
        if not scene:
            return
        axis_a, axis_b = self._gizmo_drag_plane_axes
        for _oid, obj, snapshot in self._for_each_gizmo_drag_object(scene):
            ss = snapshot["scale"]
            new_scale = list(ss)
            if self._coord_space == 1:
                new_scale[axis_a - 1] = max(ss[axis_a - 1] * factor_u, 0.001)
                new_scale[axis_b - 1] = max(ss[axis_b - 1] * factor_v, 0.001)
            else:
                r = obj.transform.right
                u = obj.transform.up
                f = obj.transform.forward
                local_axes = [
                    (r[0], r[1], r[2]),
                    (u[0], u[1], u[2]),
                    (f[0], f[1], f[2]),
                ]
                for i in range(3):
                    dot_u = self._dot3(self._gizmo_drag_plane_u, local_axes[i])
                    dot_v = self._dot3(self._gizmo_drag_plane_v, local_axes[i])
                    local_factor_u = 1.0 + (factor_u - 1.0) * dot_u * dot_u
                    local_factor_v = 1.0 + (factor_v - 1.0) * dot_v * dot_v
                    new_scale[i] = max(ss[i] * local_factor_u * local_factor_v, 0.001)

            obj.transform.local_scale = Vector3(new_scale[0], new_scale[1], new_scale[2])
        self._sync_gizmo_rigidbody_transforms()

    def _drag_scale_uniform(self, engine, local_mx, local_my, scene_w, scene_h):
        """Uniform XYZ scale driven by the grey center cube.

        Clicking near the origin makes a pure radial ratio (cur/start) extremely
        sensitive. Use a reference length matching the scale-axis shaft tip so
        center-cube dragging feels like grabbing an axis tip.
        """
        cur = self._view_plane_radial_dist(
            engine, local_mx, local_my, scene_w, scene_h, self._gizmo_drag_start_pos)
        start = self._gizmo_drag_start_t
        # Scale shaft length in BuildScaleHandleMeshes is 0.75 (local gizmo units).
        ref = self._gizmo_world_scale(engine, self._gizmo_drag_start_pos) * 0.75
        factor = 1.0 + (cur - start) / max(ref, 1e-6)
        if self._gizmo_snap_active:
            factor = 1.0 + self._snap_delta(factor - 1.0, SCALE_SNAP_FACTOR)
        factor = max(factor, 0.01)

        from Infernux.lib._Infernux import SceneManager as _SM, Vector3
        scene = _SM.instance().get_active_scene()
        if not scene:
            return
        for _oid, obj, snapshot in self._for_each_gizmo_drag_object(scene):
            ss = snapshot["scale"]
            obj.transform.local_scale = Vector3(
                max(ss[0] * factor, 0.001),
                max(ss[1] * factor, 0.001),
                max(ss[2] * factor, 0.001),
            )
        self._sync_gizmo_rigidbody_transforms()

    def _drag_scale(self, engine, local_mx, local_my, scene_w, scene_h):
        """Scale along the drag axis. In Local mode, scale applies directly to
        the corresponding local_scale component. In Global mode, the world-axis
        scale factor is decomposed onto local axes."""
        if self._gizmo_drag_axis == GIZMO_CENTER_HANDLE:
            self._drag_scale_uniform(engine, local_mx, local_my, scene_w, scene_h)
            return
        if self._gizmo_drag_axis in _PLANE_AXIS_PAIRS:
            self._drag_scale_plane(engine, local_mx, local_my, scene_w, scene_h)
            return

        ray = engine.screen_to_world_ray(local_mx, local_my, scene_w, scene_h)
        ad = self._gizmo_drag_axis_dir
        sp = self._gizmo_drag_start_pos

        cur_t = self._closest_param_on_axis(ray[:3], ray[3:], sp, ad)
        start_t = self._gizmo_drag_start_t

        # Scale factor: ratio of current projection to initial projection
        if abs(start_t) < 1e-6:
            factor = 1.0 + (cur_t - start_t)
        else:
            factor = cur_t / start_t
        if self._gizmo_snap_active:
            factor = 1.0 + self._snap_delta(factor - 1.0, SCALE_SNAP_FACTOR)
        factor = max(factor, 0.01)

        from Infernux.lib._Infernux import SceneManager as _SM, Vector3
        scene = _SM.instance().get_active_scene()
        if not scene:
            return
        for _oid, obj, snapshot in self._for_each_gizmo_drag_object(scene):
            ss = snapshot["scale"]
            new_scale = list(ss)

            if self._coord_space == 1:
                # Local mode: scale directly on the axis component (1=X, 2=Y, 3=Z)
                axis_idx = self._gizmo_drag_axis - 1  # 0, 1, or 2
                new_scale[axis_idx] = max(ss[axis_idx] * factor, 0.001)
            else:
                # Global mode: decompose world-axis scale onto local axes
                r = obj.transform.right
                u = obj.transform.up
                f = obj.transform.forward
                local_axes = [
                    (r[0], r[1], r[2]),
                    (u[0], u[1], u[2]),
                    (f[0], f[1], f[2]),
                ]
                for i in range(3):
                    dot_val = (ad[0] * local_axes[i][0] +
                               ad[1] * local_axes[i][1] +
                               ad[2] * local_axes[i][2])
                    local_factor = 1.0 + (factor - 1.0) * dot_val * dot_val
                    new_scale[i] = max(ss[i] * local_factor, 0.001)

            obj.transform.local_scale = Vector3(new_scale[0], new_scale[1], new_scale[2])
        self._sync_gizmo_rigidbody_transforms()

