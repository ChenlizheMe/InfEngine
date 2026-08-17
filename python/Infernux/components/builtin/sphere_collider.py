"""
SphereCollider — Python BuiltinComponent wrapper for C++ SphereCollider.

Draws a green wireframe sphere Gizmo in the Scene View.

Example::

    from Infernux.components.builtin import SphereCollider

    class MyScript(InxComponent):
        def start(self):
            col = self.game_object.get_component(SphereCollider)
            col.radius = 2.0
"""

from __future__ import annotations

from Infernux.components.builtin_component import CppProperty
from Infernux.components.fields import FieldType
from Infernux.components.builtin.collider import Collider
from Infernux.math.coerce import quat_rotate


class SphereCollider(Collider):
    """Python wrapper for the C++ SphereCollider component."""

    _cpp_type_name = "SphereCollider"

    # ---- Sphere-specific properties ----
    radius = CppProperty(
        "radius",
        FieldType.FLOAT,
        default=0.5,
        tooltip="Radius of the sphere collider",
    )

    # ------------------------------------------------------------------
    # Gizmos — green wireframe sphere
    # ------------------------------------------------------------------

    def on_draw_gizmos_selected(self):
        """Draw green wireframe sphere when selected (Unity-style)."""
        from Infernux.gizmos import Gizmos

        transform = self.transform
        if transform is None:
            return

        pos = transform.position
        rot = transform.rotation  # world rotation quaternion
        scale = transform.local_scale

        cpp = self._get_bound_native_component()
        if cpp is None:
            return

        r = cpp.radius

        c = cpp.center
        local_center = (c.x, c.y, c.z)

        # Rotate center offset into world space
        cx, cy, cz = quat_rotate(rot, local_center)
        world_center = (pos.x + cx, pos.y + cy, pos.z + cz)

        # Sphere uses max scale axis for uniform radius (matches C++ physics shape)
        max_scale = max(abs(scale.x), abs(scale.y), abs(scale.z))
        final_radius = r * max_scale + 0.02

        old_color = Gizmos.color
        Gizmos.color = (0.53, 1.0, 0.29)
        Gizmos.draw_wire_sphere(world_center, final_radius)
        Gizmos.color = old_color
