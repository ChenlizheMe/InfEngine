"""CylinderCollider wrapper for the native Jolt cylinder shape."""

from __future__ import annotations

import math

from Infernux.components.builtin.collider import Collider
from Infernux.components.builtin_component import CppProperty
from Infernux.components.fields import FieldType


class CylinderCollider(Collider):
    """A finite cylinder collider with a selectable local axis."""

    _cpp_type_name = "CylinderCollider"

    radius = CppProperty(
        "radius",
        FieldType.FLOAT,
        default=0.5,
        tooltip="Radius of the cylinder collider",
        range=(0.001, 100000.0),
        slider=False,
    )
    height = CppProperty(
        "height",
        FieldType.FLOAT,
        default=1.0,
        tooltip="Total height of the cylinder",
        range=(0.001, 100000.0),
        slider=False,
    )
    direction = CppProperty(
        "direction",
        FieldType.INT,
        default=1,
        tooltip="Local cylinder axis: 0=X, 1=Y, 2=Z",
        range=(0, 2),
    )

    def on_draw_gizmos_selected(self):
        from Infernux.gizmos import Gizmos

        transform = self.transform
        cpp = self._get_bound_native_component()
        if transform is None or cpp is None:
            return

        radius = float(cpp.radius)
        half_height = float(cpp.height) * 0.5
        center = cpp.center
        direction = int(cpp.direction)
        if direction == 0:
            axis, tangent, bitangent = (1, 0, 0), (0, 1, 0), (0, 0, 1)
        elif direction == 2:
            axis, tangent, bitangent = (0, 0, 1), (1, 0, 0), (0, 1, 0)
        else:
            axis, tangent, bitangent = (0, 1, 0), (1, 0, 0), (0, 0, 1)

        old_matrix = Gizmos.matrix
        old_color = Gizmos.color
        Gizmos.matrix = transform.local_to_world_matrix()
        Gizmos.color = (0.53, 1.0, 0.29)
        top = tuple(center[i] + axis[i] * half_height for i in range(3))
        bottom = tuple(center[i] - axis[i] * half_height for i in range(3))
        Gizmos.draw_wire_arc(top, axis, radius, 0, 360, 24)
        Gizmos.draw_wire_arc(bottom, axis, radius, 0, 360, 24)
        for angle_degrees in (0, 90, 180, 270):
            angle = math.radians(angle_degrees)
            offset = tuple(
                radius * (math.cos(angle) * tangent[i] + math.sin(angle) * bitangent[i])
                for i in range(3)
            )
            Gizmos.draw_line(
                tuple(top[i] + offset[i] for i in range(3)),
                tuple(bottom[i] + offset[i] for i in range(3)),
            )
        Gizmos.color = old_color
        Gizmos.matrix = old_matrix
