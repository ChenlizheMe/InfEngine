"""Tests for Infernux.gizmos — Gizmos drawing API and GizmosCollector."""

import math
from types import SimpleNamespace

from Infernux.gizmos.gizmos import Gizmos
from Infernux.gizmos.collector import GizmosCollector
from Infernux.components.particle_system import ParticleBoundsMode, ParticleSystem
from Infernux.lib import Vector3
from Infernux.particle import EmitterShape, EmitterShapeKind


# ══════════════════════════════════════════════════════════════════════
# Per-frame state reset
# ══════════════════════════════════════════════════════════════════════

class TestFrameReset:
    def test_begin_frame_resets_color(self):
        Gizmos.color = (1, 0, 0)
        Gizmos._begin_frame()
        assert Gizmos.color == (1.0, 1.0, 1.0)

    def test_begin_frame_resets_matrix(self):
        Gizmos.matrix = [0] * 16
        Gizmos._begin_frame()
        assert Gizmos.matrix is None

    def test_begin_frame_clears_batches(self):
        Gizmos._draw_batches.append(("dummy",))
        Gizmos._begin_frame()
        assert len(Gizmos._draw_batches) == 0

    def test_begin_frame_clears_icons(self):
        Gizmos._icon_entries.append(("dummy",))
        Gizmos._begin_frame()
        assert len(Gizmos._icon_entries) == 0


# ══════════════════════════════════════════════════════════════════════
# draw_line
# ══════════════════════════════════════════════════════════════════════

class TestDrawLine:
    def test_single_line(self):
        Gizmos._begin_frame()
        Gizmos.color = (1, 0, 0)
        Gizmos.draw_line((0, 0, 0), (1, 1, 1))
        assert len(Gizmos._draw_batches) == 1
        verts, indices, matrix = Gizmos._draw_batches[0]
        assert len(verts) == 2
        assert indices == [0, 1]
        # Verify color embedded in vertices
        assert verts[0][3:6] == [1, 0, 0]

    def test_uses_identity_matrix_by_default(self):
        Gizmos._begin_frame()
        Gizmos.draw_line((0, 0, 0), (1, 0, 0))
        _, _, matrix = Gizmos._draw_batches[0]
        assert matrix == Gizmos._identity_matrix


# ══════════════════════════════════════════════════════════════════════
# draw_ray
# ══════════════════════════════════════════════════════════════════════

class TestDrawRay:
    def test_ray_endpoint(self):
        Gizmos._begin_frame()
        Gizmos.draw_ray((1, 2, 3), (10, 0, 0))
        verts, _, _ = Gizmos._draw_batches[0]
        # End = origin + direction
        assert verts[1][0] == pytest.approx(11.0)
        assert verts[1][1] == pytest.approx(2.0)
        assert verts[1][2] == pytest.approx(3.0)


# ══════════════════════════════════════════════════════════════════════
# draw_icon
# ══════════════════════════════════════════════════════════════════════

class TestDrawIcon:
    def test_icon_entry(self):
        Gizmos._begin_frame()
        Gizmos.color = (0, 1, 0)
        Gizmos.draw_icon((5, 5, 5), 42)
        assert len(Gizmos._icon_entries) == 1
        pos, obj_id, color, icon_kind = Gizmos._icon_entries[0]
        assert pos == (5, 5, 5)
        assert obj_id == 42
        assert color == (0, 1, 0)
        assert icon_kind == 0  # ICON_KIND_DEFAULT

    def test_icon_custom_color(self):
        Gizmos._begin_frame()
        Gizmos.draw_icon((0, 0, 0), 1, color=(1, 0, 0))
        _, _, color, _ = Gizmos._icon_entries[0]
        assert color == (1, 0, 0)


# ══════════════════════════════════════════════════════════════════════
# draw_wire_cube
# ══════════════════════════════════════════════════════════════════════

class TestDrawWireCube:
    def test_produces_8_verts_24_indices(self):
        Gizmos._begin_frame()
        Gizmos.draw_wire_cube((0, 0, 0), (2, 2, 2))
        verts, indices, _ = Gizmos._draw_batches[0]
        assert len(verts) == 8
        assert len(indices) == 24  # 12 edges × 2 indices


# ══════════════════════════════════════════════════════════════════════
# draw_wire_sphere (Python fallback)
# ══════════════════════════════════════════════════════════════════════

class TestDrawWireSphere:
    def test_generates_geometry(self):
        Gizmos._begin_frame()
        Gizmos.draw_wire_sphere((0, 0, 0), 1.0, segments=12)
        assert len(Gizmos._draw_batches) >= 1
        verts, indices, _ = Gizmos._draw_batches[0]
        assert len(verts) > 0
        assert len(indices) > 0

    def test_respects_current_color(self):
        Gizmos._begin_frame()
        Gizmos.color = (0.5, 0.5, 0.5)
        Gizmos.draw_wire_sphere((0, 0, 0), 1.0, segments=8)
        verts, _, _ = Gizmos._draw_batches[0]
        # Color channels in vertex data
        assert verts[0][3] == pytest.approx(0.5)


# ══════════════════════════════════════════════════════════════════════
# Custom matrix
# ══════════════════════════════════════════════════════════════════════

class TestCustomMatrix:
    def test_custom_matrix_used(self):
        Gizmos._begin_frame()
        custom = [2, 0, 0, 0, 0, 2, 0, 0, 0, 0, 2, 0, 0, 0, 0, 1]
        Gizmos.matrix = custom
        Gizmos.draw_line((0, 0, 0), (1, 0, 0))
        _, _, matrix = Gizmos._draw_batches[0]
        assert matrix == custom


class TestParticleEmitterShapes:
    def test_authored_shapes_generate_distinct_gizmo_geometry(self):
        shapes = (
            EmitterShape(EmitterShapeKind.POINT),
            EmitterShape(EmitterShapeKind.SPHERE, radius=2.0),
            EmitterShape(EmitterShapeKind.BOX, dimensions=(2.0, 3.0, 4.0)),
            EmitterShape(EmitterShapeKind.CONE, radius=1.5, angle_degrees=35.0),
            EmitterShape(EmitterShapeKind.MESH),
            EmitterShape(EmitterShapeKind.SDF, sdf_interface="shape-field"),
        )

        batch_counts = []
        component = ParticleSystem()
        for shape in shapes:
            Gizmos._begin_frame()
            component._draw_emitter_shape_gizmo(Gizmos, shape)
            batch_counts.append(len(Gizmos._draw_batches))

        assert all(count > 0 for count in batch_counts)
        assert batch_counts[3] > batch_counts[1]

    def test_manual_bounds_use_component_local_space_and_restore_gizmo_state(self):
        transform_matrix = [
            2.0, 0.0, 0.0, 0.0,
            0.0, 3.0, 0.0, 0.0,
            0.0, 0.0, 4.0, 0.0,
            5.0, 6.0, 7.0, 1.0,
        ]
        component = ParticleSystem()
        component._particle_metadata = SimpleNamespace(emitters=())
        component._try_get_transform = lambda: SimpleNamespace(
            local_to_world_matrix=lambda: transform_matrix
        )
        component.bounds_mode = ParticleBoundsMode.MANUAL
        component.manual_bounds_center = Vector3(1.0, 2.0, 3.0)
        component.manual_bounds_size = Vector3(-4.0, 6.0, 8.0)

        Gizmos._begin_frame()
        original_matrix = [1.0] * 16
        original_color = (0.1, 0.2, 0.3)
        Gizmos.matrix = original_matrix
        Gizmos.color = original_color

        component.on_draw_gizmos_selected()

        assert len(Gizmos._draw_batches) == 1
        vertices, indices, matrix = Gizmos._draw_batches[0]
        assert len(vertices) == 8
        assert len(indices) == 24
        assert matrix == transform_matrix
        positions = [vertex[:3] for vertex in vertices]
        assert [min(axis) for axis in zip(*positions)] == pytest.approx(
            [-1.0, -1.0, -1.0]
        )
        assert [max(axis) for axis in zip(*positions)] == pytest.approx(
            [3.0, 5.0, 7.0]
        )
        assert Gizmos.matrix == original_matrix
        assert Gizmos.color == original_color


class TestGizmosCollectorSelectionCache:
    def test_selected_subtree_is_reused_until_selection_or_scene_changes(self, monkeypatch):
        collector = GizmosCollector()
        scene = SimpleNamespace()
        calls = []

        def build(_scene, selected_id):
            calls.append(selected_id)
            return {selected_id, selected_id + 1}

        monkeypatch.setattr(collector, "_build_ancestor_set", build)

        assert collector._get_selected_ancestor_ids(scene, 7) == frozenset({7, 8})
        assert collector._get_selected_ancestor_ids(scene, 7) == frozenset({7, 8})
        assert calls == [7]

        assert collector._get_selected_ancestor_ids(scene, 9) == frozenset({9, 10})
        assert calls == [7, 9]

        collector.invalidate_cache()
        assert collector._get_selected_ancestor_ids(scene, 9) == frozenset({9, 10})
        assert calls == [7, 9, 9]

    def test_no_selection_does_not_cross_the_scene_binding(self, monkeypatch):
        collector = GizmosCollector()

        def unexpected_walk(*_args):
            raise AssertionError("no selection walk")

        monkeypatch.setattr(collector, "_build_ancestor_set", unexpected_walk)

        assert collector._get_selected_ancestor_ids(SimpleNamespace(), 0) == frozenset()


import pytest  # noqa: E402 — needed for approx
