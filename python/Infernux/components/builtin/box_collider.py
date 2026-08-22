"""
BoxCollider — Python BuiltinComponent wrapper for C++ BoxCollider.

Exposes size, center, is_trigger via CppProperty descriptors and
draws a green wireframe cube Gizmo in the editor Scene View.

Example::

    from Infernux.components.builtin import BoxCollider

    class MyScript(InxComponent):
        def start(self):
            col = self.game_object.get_component(BoxCollider)
            col.size = Vector3(2, 1, 1)
"""

from __future__ import annotations

from Infernux.components.builtin_component import CppProperty
from Infernux.components.fields import FieldType
from Infernux.components.builtin.collider import Collider


class BoxCollider(Collider):
    """Python wrapper for the C++ BoxCollider component."""

    _cpp_type_name = "BoxCollider"

    # ---- Box-specific properties ----
    size = CppProperty(
        "size",
        FieldType.VEC3,
        default=None,
        tooltip="Size of the box collider (full extents)",
    )

    # ------------------------------------------------------------------
    # Gizmos — Unity-style green wireframe box
    # ------------------------------------------------------------------

    def on_draw_gizmos_selected(self):
        """Draw green wireframe cube when selected (Unity-style)."""
        from Infernux.gizmos import Gizmos

        transform = self.transform
        if transform is None:
            return

        # Read size/center from C++ component
        cpp = self._get_bound_native_component()
        if cpp is None:
            return

        s = cpp.size
        size = (s.x, s.y, s.z)

        c = cpp.center
        center = (c.x, c.y, c.z)

        # Use the transform's world matrix so the box follows pos/rot/scale
        old_matrix = Gizmos.matrix
        old_color = Gizmos.color
        Gizmos.matrix = transform.local_to_world_matrix()
        Gizmos.color = (0.53, 1.0, 0.29)
        Gizmos.draw_wire_cube(center, size)
        Gizmos.color = old_color
        Gizmos.matrix = old_matrix
