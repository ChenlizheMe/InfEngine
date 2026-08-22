"""
Gizmos — Unity-style immediate-mode gizmo drawing API.

Usage in InxComponent subclasses::

    from Infernux.gizmos import Gizmos

    class MyComponent(InxComponent):
        always_show: bool = True   # show gizmo even when not selected

        def on_draw_gizmos(self):
            # Called for ALL components every frame (if always_show=True)
            Gizmos.color = (0, 1, 0)
            Gizmos.draw_wire_cube(self.transform.position, (1, 1, 1))

        def on_draw_gizmos_selected(self):
            # Called only when this object (or an ancestor) is selected
            Gizmos.color = (1, 1, 0)
            Gizmos.draw_wire_sphere(self.transform.position, 2.0)

The Gizmos class accumulates line segments during callback invocations,
then the GizmosCollector packs everything and uploads to C++ in one batch.
"""

from __future__ import annotations

import math
from typing import Tuple, Optional, List

from Infernux.components._gizmo_ids import (
    ICON_KIND_CAMERA,
    ICON_KIND_DEFAULT,
    ICON_KIND_LIGHT,
    ICON_KIND_PARTICLE,
)

# Type alias for 3-component tuples
Vec3 = Tuple[float, float, float]

# Try to import C++ gizmo geometry helpers (available after engine build)
try:
    from Infernux.lib import generate_wire_sphere as _cpp_wire_sphere
    from Infernux.lib import generate_wire_arc as _cpp_wire_arc
    _HAS_CPP_GIZMOS = True
except ImportError:
    _HAS_CPP_GIZMOS = False


class Gizmos:
    """Unity-style immediate-mode gizmo drawing.

    All methods are class-level (static-ish).  State resets each frame
    via ``_begin_frame()``, called by the collector.

    Drawing primitives accumulate line-segment vertices into a shared
    per-frame buffer.  The collector packs and uploads them to the C++
    ``GizmosDrawCallBuffer`` before ``SubmitCulling()``.
    """

    # ---- Per-frame state (reset each frame) ----
    color: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    matrix: Optional[List[float]] = None  # 16-float column-major; None = identity

    # ---- Per-frame accumulation buffers ----
    # Each entry: (vertex_list, index_list, world_matrix_16_floats)
    _draw_batches: List[Tuple[List[List[float]], List[int], List[float]]] = []

    # Icon entries: (position_vec3, object_id_int, color_vec3, icon_kind_int)
    _icon_entries: List[Tuple[Vec3, int, Tuple[float, float, float], int]] = []

    # ---- Internal ----
    _identity_matrix: List[float] = [
        1, 0, 0, 0,
        0, 1, 0, 0,
        0, 0, 1, 0,
        0, 0, 0, 1,
    ]

    @classmethod
    def _begin_frame(cls):
        """Reset per-frame state.  Called by GizmosCollector at frame start."""
        cls.color = (1.0, 1.0, 1.0)
        cls.matrix = None
        cls._draw_batches.clear()
        cls._icon_entries.clear()

    @classmethod
    def _current_matrix(cls) -> List[float]:
        return cls.matrix if cls.matrix is not None else cls._identity_matrix

    # ====================================================================
    # Primitive: line
    # ====================================================================

    @classmethod
    def draw_line(cls, start: Vec3, end: Vec3):
        """Draw a single line segment from *start* to *end*."""
        c = cls.color
        verts = [
            [start[0], start[1], start[2], c[0], c[1], c[2]],
            [end[0], end[1], end[2], c[0], c[1], c[2]],
        ]
        indices = [0, 1]
        cls._draw_batches.append((verts, indices, list(cls._current_matrix())))

    # ====================================================================
    # Primitive: ray
    # ====================================================================

    @classmethod
    def draw_ray(cls, origin: Vec3, direction: Vec3):
        """Draw a ray from *origin* in *direction* (magnitude = length)."""
        end = (
            origin[0] + direction[0],
            origin[1] + direction[1],
            origin[2] + direction[2],
        )
        cls.draw_line(origin, end)

    # ====================================================================
    # Primitive: icon (billboard diamond at a world position)
    # ====================================================================

    @classmethod
    def draw_icon(cls, position: Vec3, object_id: int,
                  color: Optional[Tuple[float, float, float]] = None,
                  icon_kind: int = ICON_KIND_DEFAULT):
        """Register a clickable icon at *position* for the given GameObject.

        Icons are rendered as camera-facing diamond quads in the scene view.
        Clicking an icon selects the owning GameObject (Unity-style).

        Args:
            position: World-space position for the icon center.
            object_id: The owning GameObject's ID (used for picking).
            color: Icon tint color ``(r, g, b)``.  Defaults to ``Gizmos.color``.
            icon_kind: Built-in icon kind used by the native billboard material.
        """
        c = color if color is not None else cls.color
        cls._icon_entries.append((position, object_id, c, int(icon_kind)))

    # ====================================================================
    # Primitive: wire cube
    # ====================================================================

    @classmethod
    def draw_wire_cube(cls, center: Vec3, size: Vec3):
        """Draw a wireframe axis-aligned box centered at *center* with *size*."""
        hx, hy, hz = size[0] * 0.5, size[1] * 0.5, size[2] * 0.5
        cx, cy, cz = center

        # 8 corners
        corners = [
            (cx - hx, cy - hy, cz - hz),  # 0
            (cx + hx, cy - hy, cz - hz),  # 1
            (cx + hx, cy + hy, cz - hz),  # 2
            (cx - hx, cy + hy, cz - hz),  # 3
            (cx - hx, cy - hy, cz + hz),  # 4
            (cx + hx, cy - hy, cz + hz),  # 5
            (cx + hx, cy + hy, cz + hz),  # 6
            (cx - hx, cy + hy, cz + hz),  # 7
        ]

        # 12 edges as line pairs
        edges = [
            0, 1, 1, 2, 2, 3, 3, 0,  # front face
            4, 5, 5, 6, 6, 7, 7, 4,  # back face
            0, 4, 1, 5, 2, 6, 3, 7,  # connecting edges
        ]

        c = cls.color
        verts = [[p[0], p[1], p[2], c[0], c[1], c[2]] for p in corners]
        cls._draw_batches.append((verts, edges, list(cls._current_matrix())))

    # ====================================================================
    # Primitive: wire sphere
    # ====================================================================

    @classmethod
    def draw_wire_sphere(cls, center: Vec3, radius: float, segments: int = 24):
        """Draw a wireframe sphere as three axis-aligned circles."""
        c = cls.color
        cx, cy, cz = center
        mat = list(cls._current_matrix())

        if _HAS_CPP_GIZMOS:
            vert_flat, vert_count, idx_flat = _cpp_wire_sphere(
                cx, cy, cz, radius, segments, c[0], c[1], c[2])
            # Convert numpy arrays to nested lists for batch storage
            verts = []
            for i in range(vert_count):
                off = i * 6
                verts.append([vert_flat[off], vert_flat[off+1], vert_flat[off+2],
                              vert_flat[off+3], vert_flat[off+4], vert_flat[off+5]])
            indices = idx_flat.tolist()
            cls._draw_batches.append((verts, indices, mat))
            return

        verts = []
        indices = []

        # Pre-compute trig table
        import math as _math
        _two_pi = 2.0 * _math.pi
        cos_tab = [_math.cos(_two_pi * i / segments) for i in range(segments)]
        sin_tab = [_math.sin(_two_pi * i / segments) for i in range(segments)]

        for axis in range(3):
            base = len(verts)
            for i in range(segments):
                ca = cos_tab[i] * radius
                sa = sin_tab[i] * radius
                if axis == 0:  # YZ circle
                    p = (cx, cy + ca, cz + sa)
                elif axis == 1:  # XZ circle
                    p = (cx + ca, cy, cz + sa)
                else:  # XY circle
                    p = (cx + ca, cy + sa, cz)
                verts.append([p[0], p[1], p[2], c[0], c[1], c[2]])

            for i in range(segments):
                indices.append(base + i)
                indices.append(base + (i + 1) % segments)

        cls._draw_batches.append((verts, indices, mat))

    # ====================================================================
    # Primitive: wire frustum
    # ====================================================================

    @classmethod
    def draw_frustum(cls, position: Vec3, fov_deg: float, aspect: float,
                     near: float, far: float,
                     forward: Vec3 = (0, 0, -1),
                     up: Vec3 = (0, 1, 0),
                     right: Vec3 = (1, 0, 0)):
        """Draw a camera frustum wireframe.

        Args:
            position: Camera position.
            fov_deg: Vertical field of view in degrees.
            aspect: Width / height aspect ratio.
            near: Near clip distance.
            far: Far clip distance.
            forward, up, right: Camera basis vectors (world-space).
        """
        half_fov = math.radians(fov_deg * 0.5)
        tan_fov = math.tan(half_fov)

        near_h = near * tan_fov
        near_w = near_h * aspect
        far_h = far * tan_fov
        far_w = far_h * aspect

        px, py, pz = position
        fx, fy, fz = forward
        ux, uy, uz = up
        rx, ry, rz = right

        def _add(a, b):
            return (a[0]+b[0], a[1]+b[1], a[2]+b[2])

        def _scale(v, s):
            return (v[0]*s, v[1]*s, v[2]*s)

        nc = _add(position, _scale(forward, near))
        fc = _add(position, _scale(forward, far))

        # Near plane corners
        ntl = _add(_add(nc, _scale(up, near_h)), _scale(right, -near_w))
        ntr = _add(_add(nc, _scale(up, near_h)), _scale(right, near_w))
        nbl = _add(_add(nc, _scale(up, -near_h)), _scale(right, -near_w))
        nbr = _add(_add(nc, _scale(up, -near_h)), _scale(right, near_w))

        # Far plane corners
        ftl = _add(_add(fc, _scale(up, far_h)), _scale(right, -far_w))
        ftr = _add(_add(fc, _scale(up, far_h)), _scale(right, far_w))
        fbl = _add(_add(fc, _scale(up, -far_h)), _scale(right, -far_w))
        fbr = _add(_add(fc, _scale(up, -far_h)), _scale(right, far_w))

        corners = [ntl, ntr, nbr, nbl, ftl, ftr, fbr, fbl]
        # same edge topology as a cube
        edges = [
            0, 1, 1, 2, 2, 3, 3, 0,
            4, 5, 5, 6, 6, 7, 7, 4,
            0, 4, 1, 5, 2, 6, 3, 7,
        ]

        c = cls.color
        verts = [[p[0], p[1], p[2], c[0], c[1], c[2]] for p in corners]
        cls._draw_batches.append((verts, edges, list(cls._current_matrix())))

    # ====================================================================
    # Primitive: wire arc / circle
    # ====================================================================

    @classmethod
    def draw_wire_arc(cls, center: Vec3, normal: Vec3, radius: float,
                      start_angle_deg: float = 0.0, arc_deg: float = 360.0,
                      segments: int = 32):
        """Draw a wireframe arc (or full circle) in a plane defined by *normal*."""
        c = cls.color
        mat = list(cls._current_matrix())

        if _HAS_CPP_GIZMOS:
            vert_flat, vert_count, idx_flat = _cpp_wire_arc(
                center[0], center[1], center[2],
                normal[0], normal[1], normal[2],
                radius, start_angle_deg, arc_deg, segments,
                c[0], c[1], c[2])
            if vert_count == 0:
                return
            verts = []
            for i in range(vert_count):
                off = i * 6
                verts.append([vert_flat[off], vert_flat[off+1], vert_flat[off+2],
                              vert_flat[off+3], vert_flat[off+4], vert_flat[off+5]])
            indices = idx_flat.tolist()
            cls._draw_batches.append((verts, indices, mat))
            return

        # Build local basis from normal
        nx, ny, nz = normal
        length = math.sqrt(nx*nx + ny*ny + nz*nz)
        if length < 1e-8:
            return
        nx, ny, nz = nx/length, ny/length, nz/length

        # Choose a non-parallel axis for cross product
        if abs(ny) < 0.99:
            ax, ay, az = 0, 1, 0
        else:
            ax, ay, az = 1, 0, 0

        # u = normalize(cross(normal, arbitrary))
        ux = ny * az - nz * ay
        uy = nz * ax - nx * az
        uz = nx * ay - ny * ax
        ul = math.sqrt(ux*ux + uy*uy + uz*uz)
        ux, uy, uz = ux/ul, uy/ul, uz/ul

        # v = cross(normal, u)
        vx = ny * uz - nz * uy
        vy = nz * ux - nx * uz
        vz = nx * uy - ny * ux

        cx, cy, cz = center
        c = cls.color
        verts = []
        indices = []

        start_rad = math.radians(start_angle_deg)
        arc_rad = math.radians(arc_deg)

        for i in range(segments + 1):
            angle = start_rad + arc_rad * i / segments
            ca, sa = math.cos(angle), math.sin(angle)
            px = cx + radius * (ca * ux + sa * vx)
            py = cy + radius * (ca * uy + sa * vy)
            pz = cz + radius * (ca * uz + sa * vz)
            verts.append([px, py, pz, c[0], c[1], c[2]])
            if i > 0:
                indices.append(i - 1)
                indices.append(i)

        cls._draw_batches.append((verts, indices, list(cls._current_matrix())))

    # ====================================================================
    # Utility: get packed data for upload
    # ====================================================================

    @classmethod
    def _get_packed_data(cls):
        """Pack all draw batches into flat ``array.array`` buffers for C++ upload.

        Returns:
            ``(vert_buf, vert_count, idx_buf, desc_buf, desc_count)``
            using stdlib ``array.array`` (no numpy), or ``None`` if empty.
        """
        if not cls._draw_batches:
            return None

        import array as _array

        # Flatten all data into plain lists first, then convert once
        all_verts = []
        all_indices = []
        all_descs = []
        vert_offset = 0
        idx_offset = 0

        for verts, indices, matrix in cls._draw_batches:
            n_verts = len(verts)
            n_indices = len(indices)

            # Flatten vertex data: each v is [x,y,z,r,g,b]
            for v in verts:
                all_verts.extend(v)

            # Offset indices
            if vert_offset == 0:
                all_indices.extend(indices)
            else:
                all_indices.extend(idx + vert_offset for idx in indices)

            # Descriptor: [indexStart, indexCount, worldMatrix(16)]
            all_descs.append(float(idx_offset))
            all_descs.append(float(n_indices))
            all_descs.extend(matrix)

            vert_offset += n_verts
            idx_offset += n_indices

        # Single array construction from complete lists
        vert_buf = _array.array('f', all_verts)
        idx_buf = _array.array('I', all_indices)
        desc_buf = _array.array('f', all_descs)

        return vert_buf, vert_offset, idx_buf, desc_buf, len(cls._draw_batches)

    # ====================================================================
    # Utility: get packed icon data for upload
    # ====================================================================

    @classmethod
    def _get_packed_icon_data(cls):
        """Pack all icon entries into flat ``array.array`` buffers for C++ upload.

        Returns:
                        ``(pos_color_buf, id_buf, kind_buf, icon_count)``
            using stdlib ``array.array`` (no numpy), or ``None`` if empty.

            - ``pos_color_buf``: float32 array ``[x, y, z, r, g, b]`` per icon
            - ``id_buf``: uint32 array ``[lo, hi]`` per icon (64-bit object ID
              split into two 32-bit halves)
                        - ``kind_buf``: uint32 array ``[icon_kind]`` per icon
        """
        if not cls._icon_entries:
            return None

        import array as _array

        pos_color_buf = _array.array('f')   # float32: x,y,z,r,g,b per icon
        id_buf = _array.array('I')          # uint32: lo,hi per icon
        kind_buf = _array.array('I')        # uint32: icon kind per icon

        for position, object_id, color, icon_kind in cls._icon_entries:
            pos_color_buf.extend([
                position[0], position[1], position[2],
                color[0], color[1], color[2],
            ])
            lo = object_id & 0xFFFFFFFF
            hi = (object_id >> 32) & 0xFFFFFFFF
            id_buf.append(lo)
            id_buf.append(hi)
            kind_buf.append(int(icon_kind))

        return pos_color_buf, id_buf, kind_buf, len(cls._icon_entries)
