"""SceneViewPickingMixin — extracted from SceneViewPanel."""
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

# Gizmo handle IDs — must match C++ EditorTools constants.
# Defined locally to avoid a circular import with scene_view_panel.
from Infernux.debug import Debug
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


def _owns_particle_system(object_id: int) -> bool:
    """True when the GameObject *object_id* carries a ParticleSystem."""
    try:
        from Infernux.lib import SceneManager
        from Infernux.components.particle_system import ParticleSystem

        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(int(object_id)) if scene else None
        if obj is None:
            return False
        return any(isinstance(comp, ParticleSystem) for comp in obj.get_py_components())
    except Exception as exc:
        Debug.log_internal(f"Particle pick check failed: {exc}")
        return False


def _has_mesh_pick_geometry(object_id: int) -> bool:
    """True when the object has mesh bounds that participate in CPU ray picks."""
    try:
        from Infernux.lib import SceneManager
        from Infernux.components.builtin import MeshRenderer

        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(int(object_id)) if scene else None
        if obj is None:
            return False
        # SkinnedMeshRenderer subclasses MeshRenderer.
        return obj.get_component(MeshRenderer) is not None
    except Exception as exc:
        Debug.log_internal(f"Mesh pick geometry check failed: {exc}")
        return False


def _is_icon_only_pick_target(object_id: int) -> bool:
    """True for light/camera/etc. icons with no mesh geometry of their own."""
    if object_id <= 0:
        return False
    if _owns_particle_system(object_id):
        return False
    return not _has_mesh_pick_geometry(object_id)


def _object_ray_depth(object_id: int, ray_origin, ray_direction) -> float:
    """Approximate pick depth as projection of the object origin onto the ray."""
    try:
        from Infernux.lib import SceneManager

        scene = SceneManager.instance().get_active_scene()
        obj = scene.find_by_id(int(object_id)) if scene else None
        transform = obj.get_transform() if obj is not None else None
        if transform is None:
            return float("inf")
        pos = transform.position
        ox, oy, oz = ray_origin
        dx, dy, dz = ray_direction
        return (float(pos.x) - ox) * dx + (float(pos.y) - oy) * dy + (float(pos.z) - oz) * dz
    except Exception as exc:
        Debug.log_internal(f"Pick depth estimate failed: {exc}")
        return float("inf")


class SceneViewPickingMixin:
    """SceneViewPickingMixin method group for SceneViewPanel."""

    def _handle_picking_and_selection(self, ctx, vp, gizmo_consumed, overlay_hovered,
                                      is_scene_hovered, play_border_clr):
        """Handle object picking, box-select, and play-mode border drawing."""
        self._poll_scene_object_pick()

        if (is_scene_hovered and not gizmo_consumed
                and not overlay_hovered
                and ctx.is_mouse_button_clicked(0)
                and not self._box_select_active):
            ctrl = ctx.is_key_down(_keys.KEY_LEFT_CTRL) or ctx.is_key_down(_keys.KEY_RIGHT_CTRL)
            picked_id = self._pick_scene_object(ctx, vp)
            if self._on_object_picked:
                self._on_object_picked(picked_id, ctrl)
            self._request_particle_pick_refinement(ctx, vp, picked_id, ctrl)

        # Box-select
        if self._box_select_active:
            lx, ly = vp.mouse_local(ctx)
            self._box_select_end = (lx, ly)

            if not ctx.is_mouse_button_down(0):
                self._finalize_box_select(ctx, vp)
                self._box_select_active = False
            else:
                sx, sy = self._box_select_start
                ex, ey = self._box_select_end
                min_x = vp.image_min_x + min(sx, ex)
                min_y = vp.image_min_y + min(sy, ey)
                max_x = vp.image_min_x + max(sx, ex)
                max_y = vp.image_min_y + max(sy, ey)
                ctx.draw_filled_rect(min_x, min_y, max_x, max_y,
                                     0.3, 0.5, 0.9, 0.15)
                ctx.draw_rect(min_x, min_y, max_x, max_y,
                              0.3, 0.5, 0.9, 0.8, thickness=1.0)

        # Play-mode border
        if play_border_clr is not None:
            ctx.draw_rect(
                vp.image_min_x, vp.image_min_y,
                vp.image_max_x, vp.image_max_y,
                *play_border_clr,
                thickness=Theme.BORDER_THICKNESS,
            )

    def _request_particle_pick_refinement(self, ctx, vp, cpu_picked_id: int, ctrl: bool) -> None:
        """Queue a GPU object-ID readback so live particles become clickable.

        Ray picking only knows physics colliders, mesh bounds and gizmo icons.
        Particle output (billboards, mesh and ribbon particles) lives entirely in
        GPU buffers, so a click on a particle resolves to whatever happens to sit
        behind it. The GPU picking pass draws that output with its owning
        GameObject id, and the result — available a frame later — merges the
        ParticleSystem into the depth-cycle candidate list. Selection is only
        corrected when the ray hit was empty or a mesh/collider behind the spray;
        icon-only hits (lights, cameras, …) keep priority so they are not stolen.
        """
        self._pending_scene_pick = None
        # Additive picking builds a multi-selection; a deferred correction would
        # have to guess what to replace, so leave Ctrl-clicks to the ray path.
        if not self._engine or ctrl:
            return

        local_x, local_y = vp.mouse_local(ctx)
        if local_x < 0 or local_y < 0 or local_x > vp.width or local_y > vp.height:
            return

        request_id = self._engine.request_scene_object_pick(
            local_x, local_y, vp.width, vp.height)
        if request_id <= 0:
            return
        self._pending_scene_pick = {
            "request_id": request_id,
            "x": local_x,
            "y": local_y,
            "width": vp.width,
            "height": vp.height,
            "cpu_id": int(cpu_picked_id),
            "cpu_candidates": list(self._pick_cycle_candidates),
        }

    def _insert_ids_by_depth(self, base_ids, extra_ids, local_x, local_y, width, height):
        """Merge *extra_ids* into *base_ids* ordered by approximate ray depth."""
        if not extra_ids:
            return list(base_ids)

        ray = None
        if self._engine is not None:
            try:
                ray = self._engine.screen_to_world_ray(local_x, local_y, width, height)
            except Exception as exc:
                Debug.log_internal(f"Pick ray rebuild failed: {exc}")
                ray = None

        merged = list(base_ids)
        for object_id in extra_ids:
            oid = int(object_id)
            if oid <= 0 or oid in merged:
                continue
            if ray is None:
                merged.insert(0, oid)
                continue
            origin = (float(ray[0]), float(ray[1]), float(ray[2]))
            direction = (float(ray[3]), float(ray[4]), float(ray[5]))
            depth = _object_ray_depth(oid, origin, direction)
            insert_at = len(merged)
            for index, existing in enumerate(merged):
                if depth < _object_ray_depth(existing, origin, direction):
                    insert_at = index
                    break
            merged.insert(insert_at, oid)
        return merged

    def _merge_sticky_particle_candidates(self, ids, local_x, local_y, width, height):
        """Keep previously discovered particle hits across same-spot click cycles."""
        if not ids and not self._pick_cycle_candidates:
            return list(ids)

        viewport = (int(width), int(height))
        same_viewport = self._pick_cycle_last_viewport == viewport
        last_x, last_y = self._pick_cycle_last_mouse
        same_spot = abs(local_x - last_x) <= 3.0 and abs(local_y - last_y) <= 3.0
        if not (same_viewport and same_spot):
            return list(ids)

        sticky = [
            object_id
            for object_id in self._pick_cycle_candidates
            if object_id not in ids and _owns_particle_system(object_id)
        ]
        if not sticky:
            return list(ids)
        return self._insert_ids_by_depth(ids, sticky, local_x, local_y, width, height)

    def _poll_scene_object_pick(self):
        pending = getattr(self, "_pending_scene_pick", None)
        if not pending or not self._engine:
            return
        result = self._engine.query_scene_object_pick(pending["request_id"])
        status = result.get("status", "unknown")
        if status == "pending":
            return
        self._pending_scene_pick = None
        if status != "completed":
            # The ray pick already selected something; a failed readback simply
            # means no refinement is available.
            Debug.log_internal(f"Scene GPU picking failed: {result.get('error', status)}")
            return

        gpu_id = int(result.get("object_id", 0) or 0)
        if gpu_id <= 0 or gpu_id in _GIZMO_IDS:
            return
        if not _owns_particle_system(gpu_id):
            # Anything else the ray pass missed (a sprite behind a gizmo icon,
            # say) is not worth overriding an already-made selection for.
            return

        cpu_candidates = list(pending.get("cpu_candidates") or self._pick_cycle_candidates)
        if gpu_id in cpu_candidates:
            # Ray picking already knew about this ParticleSystem (usually via its
            # scene icon). Leave the depth-cycle list alone.
            return

        merged = self._insert_ids_by_depth(
            cpu_candidates,
            [gpu_id],
            pending["x"],
            pending["y"],
            pending["width"],
            pending["height"],
        )
        self._pick_cycle_candidates = merged
        self._pick_cycle_last_mouse = (pending["x"], pending["y"])
        self._pick_cycle_last_viewport = (int(pending["width"]), int(pending["height"]))

        from .selection_manager import SelectionManager
        cpu_id = int(pending["cpu_id"])
        if SelectionManager.instance().get_primary() != cpu_id:
            # Selection moved on since the click; don't fight the user.
            if gpu_id in merged:
                self._pick_cycle_index = merged.index(cpu_id) if cpu_id in merged else 0
            return

        # Icon billboards (lights, cameras, …) stay on top of particle sprays at
        # the same pixel. Mesh/collider hits behind the spray are corrected.
        if cpu_id > 0 and _is_icon_only_pick_target(cpu_id):
            self._pick_cycle_index = merged.index(cpu_id) if cpu_id in merged else 0
            return

        self._pick_cycle_index = merged.index(gpu_id) if gpu_id in merged else 0
        if cpu_id == gpu_id:
            return
        if self._on_object_picked:
            self._on_object_picked(gpu_id, False)

    def _cycle_pick_candidates(self, ids, local_x, local_y, width, height) -> int:
        if not ids:
            self._pick_cycle_candidates = []
            self._pick_cycle_index = -1
            return 0

        viewport = (int(width), int(height))
        same_viewport = self._pick_cycle_last_viewport == viewport
        last_x, last_y = self._pick_cycle_last_mouse
        same_spot = abs(local_x - last_x) <= 3.0 and abs(local_y - last_y) <= 3.0
        same_candidates = ids == self._pick_cycle_candidates
        if same_viewport and same_spot and same_candidates and self._pick_cycle_index >= 0:
            index = (self._pick_cycle_index + 1) % len(ids)
        else:
            index = 0

        self._pick_cycle_candidates = ids
        self._pick_cycle_index = index
        self._pick_cycle_last_mouse = (local_x, local_y)
        self._pick_cycle_last_viewport = viewport
        return ids[index]

    def _finalize_box_select(self, ctx: InxGUIContext, vp: ViewportInfo):
        """Complete a box-select drag: find objects inside the rectangle."""
        sx, sy = self._box_select_start
        ex, ey = self._box_select_end
        min_x, max_x = min(sx, ex), max(sx, ex)
        min_y, max_y = min(sy, ey), max(sy, ey)

        # Too small? Treat as a deselect click
        if abs(max_x - min_x) < 5 and abs(max_y - min_y) < 5:
            if self._on_object_picked:
                self._on_object_picked(0, False)
            return

        # Gather all scene objects and project them to screen space
        from Infernux.lib import SceneManager
        scene = SceneManager.instance().get_active_scene()
        if not scene or not self._engine:
            return

        native = self._engine.get_native_engine()
        if not native:
            return

        all_objects = scene.get_all_objects()
        selected_ids = []
        for obj in all_objects:
            t = obj.get_transform()
            if t is None:
                continue
            # Skip screen-space UI elements (canvas children with _hide_transform_)
            try:
                _skip = False
                for _pc in obj.get_py_components():
                    if getattr(type(_pc), '_hide_transform_', False):
                        _skip = True
                        break
                if _skip:
                    continue
            except RuntimeError as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                pass
            pos = t.position
            sp = native.editor_camera.world_to_screen_point(pos.x, pos.y, pos.z)
            if min_x <= sp.x <= max_x and min_y <= sp.y <= max_y:
                selected_ids.append(obj.id)

        ctrl = ctx.is_key_down(_keys.KEY_LEFT_CTRL) or ctx.is_key_down(_keys.KEY_RIGHT_CTRL)

        from .selection_manager import SelectionManager
        sel = SelectionManager.instance()
        if selected_ids:
            sel.box_select(
                selected_ids,
                additive=ctrl,
                owner_id="scene_view",
                record_history=True,
            )
        elif not ctrl:
            sel.clear(record_history=True)

        # Resolve primary object for inspector
        primary_id = sel.get_primary()
        primary_obj = scene.find_by_id(primary_id) if primary_id else None
        if self._on_box_select:
            self._on_box_select(primary_obj)

    def _pick_scene_object(self, ctx: InxGUIContext, vp: ViewportInfo) -> int:
        """Pick scene object under mouse cursor with repeated-click cycling."""
        if not self._engine:
            return 0

        local_x, local_y = vp.mouse_local(ctx)

        # Clamp within viewport
        if local_x < 0 or local_y < 0 or local_x > vp.width or local_y > vp.height:
            return 0

        candidates = self._engine.pick_scene_object_ids(local_x, local_y, vp.width, vp.height)

        # Filter invalid IDs and gizmo axis pseudo-IDs.
        ids = []
        for candidate in candidates:
            object_id = int(candidate)
            if object_id > 0 and object_id not in _GIZMO_IDS:
                ids.append(object_id)

        ids = self._merge_sticky_particle_candidates(
            ids, local_x, local_y, vp.width, vp.height)
        return self._cycle_pick_candidates(ids, local_x, local_y, vp.width, vp.height)
