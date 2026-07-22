"""
Light — Python InxComponent wrapper for the C++ Light component.

Exposes all Light properties as CppProperty descriptors so they appear
in the InxComponent serialized-field system and Inspector UI.

The underlying rendering is handled entirely by C++.

Example::

    from Infernux.components.builtin import Light
    from Infernux.lib import LightType, LightShadows

    class DayNightCycle(InxComponent):
        def start(self):
            self.sun = self.game_object.get_component(Light)

        def update(self, dt):
            self.sun.intensity = ...
"""

from __future__ import annotations

import math

from Infernux.components.builtin_component import BuiltinComponent, CppProperty
from Infernux.components.serialized_field import FieldType
from Infernux.gizmos.gizmos import ICON_KIND_LIGHT


_DIRECTIONAL_LIGHT = 0
_POINT_LIGHT = 1
_SPOT_LIGHT = 2
_AREA_LIGHT = 3


def _v_add(a, b):
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _v_sub(a, b):
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _v_mul(v, scalar):
    return (v[0] * scalar, v[1] * scalar, v[2] * scalar)


def _v_length(v):
    return math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2])


def _v_normalize(v):
    length = _v_length(v)
    if length <= 1e-6:
        return (0.0, 0.0, 1.0)
    inv = 1.0 / length
    return (v[0] * inv, v[1] * inv, v[2] * inv)


def _v_cross(a, b):
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _clamp(value, min_value, max_value):
    return max(min_value, min(max_value, value))


def _light_gizmo_color(light):
    rgba = light.color
    r = float(rgba[0]) if len(rgba) > 0 else 1.0
    g = float(rgba[1]) if len(rgba) > 1 else 1.0
    b = float(rgba[2]) if len(rgba) > 2 else 1.0
    boost = 0.35
    return (
        _clamp(r * 0.75 + boost, 0.0, 1.0),
        _clamp(g * 0.75 + boost, 0.0, 1.0),
        _clamp(b * 0.75 + boost, 0.0, 1.0),
    )


def _rgb_to_rgba(v):
    """Convert C++ vec3 color to [r, g, b, a] list for COLOR field."""
    return [float(v[0]), float(v[1]), float(v[2]), float(getattr(v, 'w', 1.0)) if hasattr(v, 'w') else 1.0]


def _rgba_to_vec3(v):
    """Convert RGBA list/tuple back to Vector3 for C++ Light.color setter."""
    if isinstance(v, (list, tuple)):
        from Infernux.lib import Vector3
        return Vector3(float(v[0]), float(v[1]), float(v[2]))
    return v


class Light(BuiltinComponent):
    """Python wrapper for the C++ Light component.

    Properties delegate to the C++ ``Light`` object via CppProperty.
    All changes are immediately reflected in the renderer.
    """

    _cpp_type_name = "Light"
    _component_category_ = "Rendering"
    _always_show = False

    # Scene icon: yellow diamond shown at light position (Unity-style)
    _gizmo_icon_color = (1.0, 0.92, 0.016)
    _gizmo_icon_kind = ICON_KIND_LIGHT

    # ---- Light type ----
    light_type = CppProperty(
        "light_type",
        FieldType.ENUM,
        default=None,
        enum_type="LightType",
        enum_labels=["Directional", "Point", "Spot", "Area"],
        tooltip="Type of light (Directional, Point, Spot, Area)",
    )

    # ---- Color & intensity ----
    color = CppProperty(
        "color",
        FieldType.COLOR,
        default=None,
        header="Appearance",
        tooltip="Light color (linear RGB)",
        get_converter=_rgb_to_rgba,
        set_converter=_rgba_to_vec3,
    )
    intensity = CppProperty(
        "intensity",
        FieldType.FLOAT,
        default=1.0,
        range=(0.0, 10.0),
        tooltip="Light intensity multiplier",
    )

    # ---- Range (Point / Spot) ----
    range = CppProperty(
        "range",
        FieldType.FLOAT,
        default=10.0,
        range=(0.1, 100.0),
        visible_when=lambda comp: int(comp.light_type) in (1, 2),
        tooltip="Light range (Point / Spot lights)",
    )

    # ---- Spot angles ----
    spot_angle = CppProperty(
        "spot_angle",
        FieldType.FLOAT,
        default=30.0,
        range=(1.0, 179.0),
        visible_when=lambda comp: int(comp.light_type) == 2,
        tooltip="Inner spot angle in degrees",
    )
    outer_spot_angle = CppProperty(
        "outer_spot_angle",
        FieldType.FLOAT,
        default=45.0,
        range=(1.0, 179.0),
        visible_when=lambda comp: int(comp.light_type) == 2,
        tooltip="Outer spot angle in degrees",
    )

    # ---- Shadows ----
    shadows = CppProperty(
        "shadows",
        FieldType.ENUM,
        default=None,
        enum_type="LightShadows",
        enum_labels=["No Shadows", "Hard", "Soft"],
        header="Shadows",
        tooltip="Shadow type (None, Hard, Soft)",
    )
    shadow_strength = CppProperty(
        "shadow_strength",
        FieldType.FLOAT,
        default=1.0,
        range=(0.0, 1.0),
        visible_when=lambda comp: int(comp.shadows) > 0,
        tooltip="Shadow strength (0-1)",
    )
    shadow_bias = CppProperty(
        "shadow_bias",
        FieldType.FLOAT,
        default=0.0,
        range=(0.0, 1),
        visible_when=lambda comp: int(comp.shadows) > 0,
        tooltip="Shadow depth bias",
    )
    shadow_normal_bias = CppProperty(
        "shadow_normal_bias",
        FieldType.FLOAT,
        default=0.01,
        range=(0.0, 0.5),
        visible_when=lambda comp: int(comp.shadows) > 0,
        tooltip="Normal-offset bias in world units (pushes the sampling "
                "position along the surface normal to fight shadow acne)",
    )

    affect_geometry = CppProperty(
        "affect_geometry",
        FieldType.BOOL,
        default=True,
        header="Influence",
        tooltip="Allow this light to illuminate geometry renderers",
    )
    affect_particles = CppProperty(
        "affect_particles",
        FieldType.BOOL,
        default=True,
        tooltip="Allow this light to illuminate particle renderers",
    )

    @property
    def culling_mask(self) -> int:
        """Layer bitmask selecting which GameObjects this light affects."""
        return int(self._require_cpp_component().culling_mask)

    @culling_mask.setter
    def culling_mask(self, value: int) -> None:
        mask = int(value)
        if not 0 <= mask <= 0xFFFFFFFF:
            raise ValueError("Light.culling_mask must be an unsigned 32-bit integer")
        self._require_cpp_component().culling_mask = mask

    # ------------------------------------------------------------------
    # Methods (delegate to C++ Light)
    # ------------------------------------------------------------------

    def get_light_view_matrix(self):
        """Get the light's view matrix for shadow mapping."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.get_light_view_matrix()
        return None

    def get_light_projection_matrix(
        self,
        shadow_extent: float = 20.0,
        near_plane: float = 0.1,
        far_plane: float = 100.0,
    ):
        """Get the light's projection matrix for shadow mapping."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.get_light_projection_matrix(shadow_extent, near_plane, far_plane)
        return None

    def serialize(self) -> str:
        """Serialize Light to JSON string (delegates to C++)."""
        cpp = self._cpp_component
        if cpp is not None:
            return cpp.serialize()
        return "{}"

    # ------------------------------------------------------------------
    # Gizmos — light-type specific scene visualisation
    # ------------------------------------------------------------------

    def on_draw_gizmos_selected(self):
        """Draw a type-specific light gizmo when selected in the editor."""
        from Infernux.gizmos import Gizmos

        cpp = self._get_bound_native_component()
        if cpp is None:
            return

        transform = self.transform
        if transform is None:
            return

        pos = transform.position
        position = (pos.x, pos.y, pos.z)
        forward = _v_normalize((transform.forward.x, transform.forward.y, transform.forward.z))
        up = _v_normalize((transform.up.x, transform.up.y, transform.up.z))
        right = _v_normalize((transform.right.x, transform.right.y, transform.right.z))

        old_color = Gizmos.color
        Gizmos.color = _light_gizmo_color(self)

        light_type = int(self.light_type)
        if light_type == _DIRECTIONAL_LIGHT:
            self._draw_directional_gizmo(position, forward, up, right)
        elif light_type == _POINT_LIGHT:
            self._draw_point_gizmo(position)
        elif light_type == _SPOT_LIGHT:
            self._draw_spot_gizmo(position, forward, up, right)
        elif light_type == _AREA_LIGHT:
            self._draw_area_gizmo(position, forward, up, right)
        else:
            self._draw_point_gizmo(position)

        Gizmos.color = old_color

    def _draw_directional_gizmo(self, position, forward, up, right):
        from Infernux.gizmos import Gizmos

        shaft_length = 1.8
        arrow_size = 0.35
        offsets = [
            (0.0, 0.0),
            (-0.45, 0.35),
            (0.45, 0.35),
            (-0.45, -0.35),
            (0.45, -0.35),
        ]

        for right_offset, up_offset in offsets:
            origin = _v_add(position, _v_add(_v_mul(right, right_offset), _v_mul(up, up_offset)))
            end = _v_add(origin, _v_mul(forward, shaft_length))
            Gizmos.draw_line(origin, end)

            head_base = _v_add(end, _v_mul(forward, -arrow_size))
            Gizmos.draw_line(end, _v_add(head_base, _v_mul(right, arrow_size * 0.55)))
            Gizmos.draw_line(end, _v_add(head_base, _v_mul(right, -arrow_size * 0.55)))
            Gizmos.draw_line(end, _v_add(head_base, _v_mul(up, arrow_size * 0.55)))
            Gizmos.draw_line(end, _v_add(head_base, _v_mul(up, -arrow_size * 0.55)))

    def _draw_point_gizmo(self, position):
        from Infernux.gizmos import Gizmos

        radius = max(0.05, float(self.range))
        Gizmos.draw_wire_sphere(position, radius)

        inner = min(radius * 0.2, 0.6)
        Gizmos.draw_wire_sphere(position, inner, segments=16)

    def _draw_spot_gizmo(self, position, forward, up, right):
        from Infernux.gizmos import Gizmos

        light_range = max(0.05, float(self.range))
        outer_angle = math.radians(float(self.outer_spot_angle) * 0.5)
        inner_angle = math.radians(min(float(self.spot_angle), float(self.outer_spot_angle)) * 0.5)
        outer_radius = math.tan(outer_angle) * light_range
        inner_radius = math.tan(inner_angle) * light_range
        cone_center = _v_add(position, _v_mul(forward, light_range))

        ring_points = []
        spokes = 12
        for index in range(spokes):
            angle = (math.tau * index) / spokes
            radial = _v_add(_v_mul(right, math.cos(angle)), _v_mul(up, math.sin(angle)))
            ring_point = _v_add(cone_center, _v_mul(radial, outer_radius))
            ring_points.append(ring_point)

        for index in range(spokes):
            Gizmos.draw_line(ring_points[index], ring_points[(index + 1) % spokes])

        for index in (0, 3, 6, 9):
            Gizmos.draw_line(position, ring_points[index])

        if inner_radius > 0.001:
            Gizmos.draw_wire_arc(cone_center, forward, inner_radius, 0.0, 360.0, 24)

        Gizmos.draw_ray(position, _v_mul(forward, light_range))

    def _draw_area_gizmo(self, position, forward, up, right):
        from Infernux.gizmos import Gizmos

        half_width = 0.8
        half_height = 0.5

        corners = [
            _v_add(position, _v_add(_v_mul(right, -half_width), _v_mul(up, -half_height))),
            _v_add(position, _v_add(_v_mul(right, half_width), _v_mul(up, -half_height))),
            _v_add(position, _v_add(_v_mul(right, half_width), _v_mul(up, half_height))),
            _v_add(position, _v_add(_v_mul(right, -half_width), _v_mul(up, half_height))),
        ]

        for index in range(4):
            Gizmos.draw_line(corners[index], corners[(index + 1) % 4])

        inset = 0.28
        inner_corners = [
            _v_add(position, _v_add(_v_mul(right, -half_width + inset), _v_mul(up, -half_height + inset))),
            _v_add(position, _v_add(_v_mul(right, half_width - inset), _v_mul(up, -half_height + inset))),
            _v_add(position, _v_add(_v_mul(right, half_width - inset), _v_mul(up, half_height - inset))),
            _v_add(position, _v_add(_v_mul(right, -half_width + inset), _v_mul(up, half_height - inset))),
        ]
        for index in range(4):
            Gizmos.draw_line(inner_corners[index], inner_corners[(index + 1) % 4])

        for corner in corners:
            Gizmos.draw_line(corner, _v_add(corner, _v_mul(forward, 0.45)))

        center_tip = _v_add(position, _v_mul(forward, 0.75))
        Gizmos.draw_line(position, center_tip)
        Gizmos.draw_line(center_tip, _v_add(_v_add(center_tip, _v_mul(forward, -0.18)), _v_mul(right, 0.14)))
        Gizmos.draw_line(center_tip, _v_add(_v_add(center_tip, _v_mul(forward, -0.18)), _v_mul(right, -0.14)))
