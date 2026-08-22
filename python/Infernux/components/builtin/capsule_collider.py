"""
CapsuleCollider — Python BuiltinComponent wrapper for C++ CapsuleCollider.

Draws a green wireframe capsule Gizmo in the Scene View.

Example::

    from Infernux.components.builtin import CapsuleCollider

    class MyScript(InxComponent):
        def start(self):
            col = self.game_object.get_component(CapsuleCollider)
            col.radius = 0.5
            col.height = 2.0
"""

from __future__ import annotations

import math

from Infernux.components.builtin_component import CppProperty
from Infernux.components.fields import FieldType
from Infernux.components.builtin.collider import Collider


class CapsuleCollider(Collider):
    """Python wrapper for the C++ CapsuleCollider component."""

    _cpp_type_name = "CapsuleCollider"

    # ---- Capsule-specific properties ----
    radius = CppProperty(
        "radius",
        FieldType.FLOAT,
        default=0.5,
        tooltip="Radius of the capsule collider",
    )
    height = CppProperty(
        "height",
        FieldType.FLOAT,
        default=2.0,
        tooltip="Total height of the capsule (including caps)",
    )
    direction = CppProperty(
        "direction",
        FieldType.INT,
        default=1,
        tooltip="Direction axis: 0=X, 1=Y, 2=Z",
        range=(0, 2),
    )

    # ------------------------------------------------------------------
    # Gizmos — green wireframe capsule
    # ------------------------------------------------------------------

    def on_draw_gizmos_selected(self):
        """Draw green wireframe capsule when selected (Unity-style)."""
        from Infernux.gizmos import Gizmos

        transform = self.transform
        if transform is None:
            return

        cpp = self._get_bound_native_component()
        if cpp is None:
            return

        r = cpp.radius

        h = cpp.height

        d = cpp.direction

        c = cpp.center
        cx, cy, cz = c.x, c.y, c.z

        # Use the transform's world matrix so the capsule follows pos/rot/scale
        old_matrix = Gizmos.matrix
        old_color = Gizmos.color
        Gizmos.matrix = transform.local_to_world_matrix()
        Gizmos.color = (0.53, 1.0, 0.29)

        # Compute capsule geometry in local space (scale is part of the matrix)
        half_cyl = max((h - 2.0 * r) * 0.5, 0.0)

        segments = 24

        # Axis vectors based on capsule direction
        if d == 0:       # X-axis
            axis = (1, 0, 0)
            perp1 = (0, 1, 0)
            perp2 = (0, 0, 1)
        elif d == 2:     # Z-axis
            axis = (0, 0, 1)
            perp1 = (1, 0, 0)
            perp2 = (0, 1, 0)
        else:            # Y-axis (default)
            axis = (0, 1, 0)
            perp1 = (1, 0, 0)
            perp2 = (0, 0, 1)

        # Top and bottom center points
        top = (cx + half_cyl * axis[0], cy + half_cyl * axis[1], cz + half_cyl * axis[2])
        bot = (cx - half_cyl * axis[0], cy - half_cyl * axis[1], cz - half_cyl * axis[2])

        # Full rings at top and bottom of cylinder section
        Gizmos.draw_wire_arc(top, axis, r, 0, 360, segments)
        Gizmos.draw_wire_arc(bot, axis, r, 0, 360, segments)

        # Hemisphere arcs — explicit parametric to avoid draw_wire_arc basis issues.
        # For each perpendicular plane, draw a semicircle:
        #   top: center + r * (cos(θ)*perp + sin(θ)*axis)  θ∈[0,π] → goes toward +axis pole
        #   bot: center + r * (cos(θ)*perp - sin(θ)*axis)  θ∈[0,π] → goes toward -axis pole
        half_seg = max(segments // 2, 8)
        for perp in (perp1, perp2):
            # Top hemisphere arc
            pts = []
            for i in range(half_seg + 1):
                angle = math.pi * i / half_seg
                ca, sa = math.cos(angle), math.sin(angle)
                pts.append((
                    top[0] + r * (ca * perp[0] + sa * axis[0]),
                    top[1] + r * (ca * perp[1] + sa * axis[1]),
                    top[2] + r * (ca * perp[2] + sa * axis[2]),
                ))
            for i in range(len(pts) - 1):
                Gizmos.draw_line(pts[i], pts[i + 1])

            # Bottom hemisphere arc
            pts = []
            for i in range(half_seg + 1):
                angle = math.pi * i / half_seg
                ca, sa = math.cos(angle), math.sin(angle)
                pts.append((
                    bot[0] + r * (ca * perp[0] - sa * axis[0]),
                    bot[1] + r * (ca * perp[1] - sa * axis[1]),
                    bot[2] + r * (ca * perp[2] - sa * axis[2]),
                ))
            for i in range(len(pts) - 1):
                Gizmos.draw_line(pts[i], pts[i + 1])

        # Four connecting lines between the two rings
        for angle_deg in (0, 90, 180, 270):
            angle = math.radians(angle_deg)
            ca, sa = math.cos(angle), math.sin(angle)
            off = (
                r * (ca * perp1[0] + sa * perp2[0]),
                r * (ca * perp1[1] + sa * perp2[1]),
                r * (ca * perp1[2] + sa * perp2[2]),
            )
            p1 = (top[0] + off[0], top[1] + off[1], top[2] + off[2])
            p2 = (bot[0] + off[0], bot[1] + off[1], bot[2] + off[2])
            Gizmos.draw_line(p1, p2)

        Gizmos.color = old_color
        Gizmos.matrix = old_matrix
