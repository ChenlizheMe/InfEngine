"""
Camera — Python InxComponent wrapper for the C++ Camera component.

Exposes projection settings, clear flags, culling mask, and coordinate
conversion as CppProperty descriptors and delegate methods.

Provides built-in Gizmos drawing (frustum wireframe) via
``on_draw_gizmos_selected()``, rendered automatically by the GizmosCollector
when the camera is selected.

Example::

    from Infernux.components.builtin import Camera
    from Infernux.lib import CameraProjection

    class CinematicCamera(InxComponent):
        def start(self):
            cam = self.game_object.get_component(Camera)
            cam.field_of_view = 45.0
            cam.near_clip = 0.1
"""

from __future__ import annotations

from typing import Any, Optional, Tuple

from Infernux.components.builtin_component import BuiltinComponent, CppProperty
from Infernux.components.fields import FieldType
from Infernux.components._gizmo_ids import ICON_KIND_CAMERA


def _vec4_to_list(v):
    """Convert C++ vec4 to [r, g, b, a] list for COLOR field."""
    return [float(v[0]), float(v[1]), float(v[2]), float(v[3])]


def _list_to_vec4(v):
    """Convert RGBA list/tuple back to vec4f for C++ setter."""
    if isinstance(v, (list, tuple)):
        from Infernux.lib import vec4f
        return vec4f(float(v[0]), float(v[1]), float(v[2]), float(v[3]))
    return v


# Maximum far-plane distance for gizmo visualization (Unity caps ~1000)
_FAR_CLIP_VISUAL_CAP = 1000.0

# Gizmo color — Unity uses white for camera gizmos
_CAMERA_GIZMO_COLOR = (1.0, 1.0, 1.0)


class Camera(BuiltinComponent):
    """Python wrapper for the C++ Camera component.

    Properties delegate to the C++ ``Camera`` via CppProperty.
    Draws a Unity-style frustum wireframe gizmo when selected.
    """

    _cpp_type_name = "Camera"
    _component_category_ = "Rendering"
    _display_name_key = "component.camera"

    # Gizmo visibility: only show frustum wireframe when camera is selected
    _always_show = False

    # Scene icon: white diamond shown at camera position (Unity-style)
    _gizmo_icon_color = (1.0, 1.0, 1.0)
    _gizmo_icon_kind = ICON_KIND_CAMERA

    # ---- Projection ----
    projection_mode = CppProperty(
        "projection_mode",
        FieldType.ENUM,
        default=None,
        enum_type="CameraProjection",
        enum_labels=["camera.projection.perspective", "camera.projection.orthographic"],
        display_name_key="camera.projection_mode",
        tooltip="camera.tooltip.projection_mode",
    )
    field_of_view = CppProperty(
        "field_of_view",
        FieldType.FLOAT,
        default=60.0,
        range=(1.0, 179.0),
        visible_when=lambda comp: int(comp.projection_mode) == 0,
        display_name_key="camera.field_of_view",
        tooltip="camera.tooltip.field_of_view",
    )
    orthographic_size = CppProperty(
        "orthographic_size",
        FieldType.FLOAT,
        default=5.0,
        visible_when=lambda comp: int(comp.projection_mode) == 1,
        display_name_key="camera.orthographic_size",
        tooltip="camera.tooltip.orthographic_size",
    )
    # ---- Clipping ----
    near_clip = CppProperty(
        "near_clip",
        FieldType.FLOAT,
        default=0.01,
        display_name_key="camera.near_clip",
        header="camera.section.clipping",
        tooltip="camera.tooltip.near_clip",
    )
    far_clip = CppProperty(
        "far_clip",
        FieldType.FLOAT,
        default=1000.0,
        display_name_key="camera.far_clip",
        tooltip="camera.tooltip.far_clip",
    )

    # ---- Multi-camera ----
    depth = CppProperty(
        "depth",
        FieldType.FLOAT,
        default=0.0,
        display_name_key="camera.depth",
        tooltip="camera.tooltip.depth",
    )

    # ---- Clear flags & background ----
    clear_flags = CppProperty(
        "clear_flags",
        FieldType.ENUM,
        default=None,
        enum_type="CameraClearFlags",
        enum_labels=[
            "camera.clear.skybox",
            "camera.clear.solid_color",
            "camera.clear.depth_only",
            "camera.clear.dont_clear",
        ],
        display_name_key="camera.clear_flags",
        header="camera.section.clear",
        tooltip="camera.tooltip.clear_flags",
    )
    background_color = CppProperty(
        "background_color",
        FieldType.COLOR,
        default=None,
        visible_when=lambda comp: int(comp.clear_flags) == 1,
        display_name_key="camera.background_color",
        tooltip="camera.tooltip.background_color",
        get_converter=_vec4_to_list,
        set_converter=_list_to_vec4,
    )

    # ---- Output safety & quantization ----
    stop_nans = CppProperty(
        "stop_nans",
        FieldType.BOOL,
        default=False,
        header="camera.section.output",
        display_name_key="camera.stop_nans",
        tooltip="camera.tooltip.stop_nans",
    )
    dithering = CppProperty(
        "dithering",
        FieldType.BOOL,
        default=False,
        display_name_key="camera.dithering",
        tooltip="camera.tooltip.dithering",
    )

    # ------------------------------------------------------------------
    # Read-only properties (delegates)
    # ------------------------------------------------------------------

    @property
    def aspect_ratio(self) -> float:
        """Aspect ratio (width / height) — read-only, computed from viewport."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.aspect_ratio
        return 1.778

    @property
    def culling_mask(self) -> int:
        """Layer culling bitmask — read-only."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.culling_mask
        return 0xFFFFFFFF

    @property
    def pixel_width(self) -> int:
        """Render target width in pixels (read-only)."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.pixel_width
        return 0

    @property
    def pixel_height(self) -> int:
        """Render target height in pixels (read-only)."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.pixel_height
        return 0

    # ------------------------------------------------------------------
    # Coordinate conversion (delegate methods)
    # ------------------------------------------------------------------

    def screen_to_world_point(
        self, x: float, y: float, depth: float = 0.0
    ) -> Optional[Tuple[float, float, float]]:
        """Convert screen coordinates (x, y) + depth [0..1] to world position."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.screen_to_world_point(x, y, depth)
        return None

    def world_to_screen_point(
        self, x: float, y: float, z: float
    ) -> Optional[Tuple[float, float]]:
        """Convert world position to screen coordinates (x, y)."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.world_to_screen_point(x, y, z)
        return None

    def screen_point_to_ray(
        self, x: float, y: float
    ) -> Optional[Tuple[Tuple[float, float, float], Tuple[float, float, float]]]:
        """Build a ray from viewport-relative screen coordinates.

        Returns ``((ox, oy, oz), (dx, dy, dz))`` — origin at the near
        plane and a normalised direction vector.
        """
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.screen_point_to_ray(x, y)
        return None

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def serialize(self) -> str:
        """Serialize Camera to JSON string (delegates to C++)."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.serialize()
        return "{}"

    def deserialize(self, json_str: str) -> bool:
        """Deserialize Camera from JSON string (delegates to C++)."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.deserialize(json_str)
        return False

    # ------------------------------------------------------------------
    # Gizmos — Unity-style camera frustum + body icon
    # ------------------------------------------------------------------

    def on_draw_gizmos_selected(self):
        """Draw camera frustum wireframe and body icon when selected.

        Called automatically by the GizmosCollector when the owning
        GameObject (or an ancestor) is selected in the editor.
        Replicates Unity's camera gizmo appearance: wireframe frustum
        (perspective or orthographic), a small film-gate rectangle, a
        camera body, and a film reel triangle on top.
        """
        from Infernux.gizmos import Gizmos

        if self._get_bound_native_component() is None:
            return
        transform = self.transform

        # Transform data is required to draw a valid camera frustum.  A stale
        # native binding is an engine lifecycle error and must reach the gizmo
        # invocation boundary instead of becoming a per-frame log message.
        pos = transform.position
        position = (pos.x, pos.y, pos.z)
        fwd = transform.forward
        forward = (fwd.x, fwd.y, fwd.z)
        u = transform.up
        up = (u.x, u.y, u.z)
        r = transform.right
        right = (r.x, r.y, r.z)

        fov = self.field_of_view
        aspect = self.aspect_ratio
        near = self.near_clip
        far = min(self.far_clip, _FAR_CLIP_VISUAL_CAP)

        from Infernux.lib import CameraProjection
        is_ortho = (self.projection_mode == CameraProjection.Orthographic)
        ortho_size = self.orthographic_size if is_ortho else 0.0

        Gizmos.color = _CAMERA_GIZMO_COLOR

        # ---- Frustum wireframe ----
        if is_ortho:
            self._draw_ortho_frustum(position, forward, up, right,
                                     ortho_size, aspect, near, far)
        else:
            Gizmos.draw_frustum(position, fov, aspect, near, far,
                                forward, up, right)

    # ---- Helper: orthographic frustum ----

    @staticmethod
    def _draw_ortho_frustum(position, forward, up, right,
                            ortho_size, aspect, near, far):
        from Infernux.gizmos import Gizmos

        hh = ortho_size
        hw = ortho_size * aspect

        def _a(a, b):
            return (a[0]+b[0], a[1]+b[1], a[2]+b[2])
        def _s(v, s):
            return (v[0]*s, v[1]*s, v[2]*s)

        nc = _a(position, _s(forward, near))
        fc = _a(position, _s(forward, far))

        ntl = _a(_a(nc, _s(up, hh)), _s(right, -hw))
        ntr = _a(_a(nc, _s(up, hh)), _s(right,  hw))
        nbr = _a(_a(nc, _s(up,-hh)), _s(right,  hw))
        nbl = _a(_a(nc, _s(up,-hh)), _s(right, -hw))
        ftl = _a(_a(fc, _s(up, hh)), _s(right, -hw))
        ftr = _a(_a(fc, _s(up, hh)), _s(right,  hw))
        fbr = _a(_a(fc, _s(up,-hh)), _s(right,  hw))
        fbl = _a(_a(fc, _s(up,-hh)), _s(right, -hw))

        for a, b in [(ntl,ntr),(ntr,nbr),(nbr,nbl),(nbl,ntl),
                     (ftl,ftr),(ftr,fbr),(fbr,fbl),(fbl,ftl),
                     (ntl,ftl),(ntr,ftr),(nbr,fbr),(nbl,fbl)]:
            Gizmos.draw_line(a, b)


