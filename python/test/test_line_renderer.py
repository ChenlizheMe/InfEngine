from __future__ import annotations

import pytest

from Infernux import LineRenderer as LineRendererComponent
from Infernux.graph.ramp import Curve, CurveKey, Gradient, GradientKey
from Infernux.lib import (
    LineAlignment,
    LineRenderer as NativeLineRenderer,
    LineTextureMode,
    Vector3,
)
from Infernux.engine.ui._scene_view_line_tools import SceneViewLineToolsMixin
from Infernux.engine.scene_document_transaction import SceneDocumentTransaction


def _xyz(value):
    return pytest.approx((float(value[0]), float(value[1]), float(value[2])))


def test_line_renderer_is_available_through_the_component_system(scene):
    owner = scene.create_game_object("Line")
    line = owner.add_component("LineRenderer")

    assert isinstance(line, LineRendererComponent)
    assert line.position_count == 2
    assert line._cpp_component.vertex_count == 4
    assert line._cpp_component.index_count == 6


def test_line_renderer_inspector_uses_specialized_curve_gradient_and_position_editors(
    scene, monkeypatch
):
    from Infernux.engine.ui import inspector_components, inspector_utils
    from Infernux.engine.ui.particle_graph_editor_panel import (
        ParticleGraphEditorPanel,
    )

    line = scene.create_game_object("InspectedLine").add_component("LineRenderer")
    visited = []

    class Context:
        def collapsing_header(self, label):
            visited.append(label)
            return True

        def vector3(self, _label, x, y, z, _speed, _label_width):
            return (x, y, z)

    monkeypatch.setattr(
        inspector_components,
        "render_builtin_via_setters",
        lambda *_args, **_kwargs: visited.append("Built-in Properties"),
    )
    monkeypatch.setattr(inspector_utils, "max_label_w", lambda *_args: 120.0)
    monkeypatch.setattr(
        ParticleGraphEditorPanel,
        "_render_curve_property",
        lambda _ctx, _uid, _key, value, **_kwargs: (
            visited.append("Curve Editor") or value
        ),
    )
    monkeypatch.setattr(
        ParticleGraphEditorPanel,
        "_render_gradient_property",
        lambda _ctx, _uid, _key, value, **_kwargs: (
            visited.append("Gradient Editor") or value
        ),
    )

    line.render_inspector(Context())

    assert visited == [
        "Built-in Properties",
        "Width Curve",
        "Curve Editor",
        "Color Gradient",
        "Gradient Editor",
        "Positions",
    ]


def test_line_renderer_point_edits_rebuild_only_derived_geometry(scene):
    line = scene.create_game_object("Polyline").add_component("LineRenderer")
    line.set_positions([(0, 0, 0), (1, 1, 0), (2, 0, 0)])

    assert line.position_count == 3
    assert _xyz(line.get_position(1)) == (1.0, 1.0, 0.0)
    assert line._cpp_component.vertex_count == 6
    assert line._cpp_component.index_count == 12

    line.loop = True
    assert line._cpp_component.vertex_count == 8
    assert line._cpp_component.index_count == 18


def test_line_renderer_ignores_consecutive_duplicate_samples_in_generated_ribbon(scene):
    line = scene.create_game_object("RuntimeTrail").add_component("LineRenderer")
    line.set_positions(
        [
            (0, 0, 0),
            (0, 0, 0),
            (1, 0, 0),
            (1, 0, 0),
            (2, 0, 0),
        ]
    )

    assert line.position_count == 5
    assert line._cpp_component.vertex_count == 6
    assert line._cpp_component.index_count == 12


def test_line_renderer_serializes_authored_state_without_generated_mesh(scene):
    line = scene.create_game_object("SerializableLine").add_component("LineRenderer")
    line.set_positions([(0, 0, 0), (1, 2, 3), (4, 5, 6)])
    line.start_width = 0.25
    line.end_width = 0.75
    line.width_multiplier = 2.0
    line.start_color = [1.0, 0.25, 0.0, 0.8]
    line.end_color = [0.0, 0.5, 1.0, 0.2]
    line.use_world_space = True
    line.alignment = LineAlignment.TransformZ
    line.texture_mode = LineTextureMode.Tile
    line.texture_scale = [3.0, 2.0]
    line.num_corner_vertices = 3
    line.num_cap_vertices = 2
    line.shadow_bias = 0.25
    line.generate_lighting_data = True
    line.width_curve = Curve(
        (
            CurveKey(0.0, 0.25),
            CurveKey(0.5, 1.5, 0.0, 0.0),
            CurveKey(1.0, 0.75),
        )
    )
    line.color_gradient = Gradient(
        (
            GradientKey(0.0, (1.0, 0.25, 0.0, 0.8)),
            GradientKey(0.5, (0.2, 1.0, 0.4, 0.5)),
            GradientKey(1.0, (0.0, 0.5, 1.0, 0.2)),
        ),
        "fixed",
    )

    document = line.serialize_document()
    assert document["type"] == "LineRenderer"
    assert document["useInlineMesh"] is False
    assert "inlineVertices" not in document
    assert "inlineIndices" not in document
    assert len(document["positions"]) == 3
    assert len(document["widthCurve"]["keys"]) == 3
    assert len(document["colorGradient"]["keys"]) == 3
    assert document["textureScale"] == [3.0, 2.0]
    assert document["numCornerVertices"] == 3
    assert document["numCapVertices"] == 2
    assert document["shadowBias"] == pytest.approx(0.25)
    assert document["generateLightingData"] is True

    copy = NativeLineRenderer()
    assert copy.deserialize_document(document)
    assert copy.position_count == 3
    assert _xyz(copy.get_position(2)) == (4.0, 5.0, 6.0)
    assert copy.index_count > 12
    assert copy.use_world_space is True
    assert copy.alignment == LineAlignment.TransformZ
    assert copy.texture_mode == LineTextureMode.Tile


def test_line_renderer_scene_snapshot_round_trips_through_validation(scene):
    line = scene.create_game_object("SnapshotLine").add_component("LineRenderer")
    line.set_positions([(0, 0, 0), (1, 2, 0), (3, 1, 0)])

    transaction = SceneDocumentTransaction(scene, document=scene.serialize_document())

    assert transaction.run_to_completion(raise_on_failure=False) is True
    restored = scene.find("SnapshotLine").get_component("LineRenderer")
    assert restored.position_count == 3


def test_line_renderer_bounds_follow_cached_maximum_width(scene):
    line = scene.create_game_object("BoundedLine").add_component("LineRenderer")
    line.set_positions([(0, 0, 0), (2, 0, 0)])
    line.start_width = 4.0
    line.end_width = 4.0

    bounds = line._cpp_component.get_world_bounds()

    assert bounds[1] == pytest.approx(-2.0)
    assert bounds[4] == pytest.approx(2.0)


def test_line_renderer_supports_unity_texture_modes_curves_and_rounding(scene):
    line = scene.create_game_object("StyledLine").add_component("LineRenderer")
    line.set_positions([(0, 0, 0), (1, 1, 0), (2, 0, 0)])

    line.num_corner_vertices = 3
    assert line._cpp_component.vertex_count == 12
    assert line._cpp_component.index_count == 30

    line.num_corner_vertices = 0
    line.num_cap_vertices = 2
    assert line._cpp_component.vertex_count == 14
    assert line._cpp_component.index_count == 36

    line.width_curve = Curve(
        (CurveKey(0.0, 0.1), CurveKey(0.5, 1.0), CurveKey(1.0, 0.2)),
        "repeat",
        "ping_pong",
    )
    restored_curve = line.width_curve
    assert restored_curve.pre_wrap == "repeat"
    assert restored_curve.post_wrap == "ping_pong"
    assert [key.time for key in restored_curve.keys] == pytest.approx([0.0, 0.5, 1.0])
    assert [key.value for key in restored_curve.keys] == pytest.approx([0.1, 1.0, 0.2])

    line.color_gradient = Gradient(
        (
            GradientKey(0.0, (1.0, 0.0, 0.0, 1.0)),
            GradientKey(1.0, (0.0, 0.0, 1.0, 0.25)),
        ),
        "fixed",
    )
    assert line.color_gradient.mode == "fixed"
    assert line.start_color == pytest.approx([1.0, 0.0, 0.0, 1.0])
    assert line.end_color == pytest.approx([0.0, 0.0, 1.0, 0.25])

    for mode in (
        LineTextureMode.Stretch,
        LineTextureMode.Tile,
        LineTextureMode.DistributePerSegment,
        LineTextureMode.RepeatPerSegment,
        LineTextureMode.Static,
    ):
        line.texture_mode = mode
        assert line.texture_mode == mode


def test_line_renderer_supports_unity_perceptual_gradient_mode(scene):
    line = scene.create_game_object("PerceptualLine").add_component("LineRenderer")
    line.color_gradient = Gradient(
        (
            GradientKey(0.0, (1.0, 0.0, 0.0, 1.0)),
            GradientKey(1.0, (0.0, 1.0, 0.0, 0.25)),
        ),
        "perceptual_blend",
    )

    assert line.color_gradient.mode == "perceptual_blend"
    assert line.serialize_document()["colorGradient"]["mode"] == 2


def test_line_renderer_scene_tool_geometry_helpers():
    positions = [(0, 0, 0), (1, 0.001, 0), (2, 0, 0), (3, 1, 0)]
    assert SceneViewLineToolsMixin._line_simplify_positions(positions, 0.01) == [
        (0.0, 0.0, 0.0),
        (2.0, 0.0, 0.0),
        (3.0, 1.0, 0.0),
    ]
    assert SceneViewLineToolsMixin._line_subdivide_positions(
        [(0, 0, 0), (2, 0, 0), (4, 0, 0)], {0, 1}
    ) == [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (2.0, 0.0, 0.0), (4.0, 0.0, 0.0)]
    assert SceneViewLineToolsMixin._line_ray_plane_intersection(
        (0, 0, -1), (0, 0, 1), (0, 0, 0), (0, 0, 1)
    ) == (0.0, 0.0, 0.0)


def test_line_renderer_bakes_an_expanded_static_mesh(scene):
    line = scene.create_game_object("SourceLine").add_component("LineRenderer")
    line.set_positions([(0, 0, 0), (2, 0, 0)])
    line.start_width = 0.5
    line.end_width = 0.5
    target = scene.create_game_object("BakedLine").add_component("MeshRenderer")

    line.bake_mesh(target)

    assert target._cpp_component.vertex_count == 4
    assert target._cpp_component.index_count == 6
    assert target._cpp_component.mesh_name == ""
    assert target._cpp_component.get_world_bounds()[1] < 0.0
    assert target._cpp_component.get_world_bounds()[4] > 0.0


def test_line_renderer_simplify_and_validation(scene):
    line = scene.create_game_object("SimplifiedLine").add_component("LineRenderer")
    line.set_positions([(0, 0, 0), (1, 0.001, 0), (2, 0, 0), (3, 1, 0)])
    line.simplify(0.01)
    assert line.position_count == 3

    with pytest.raises(ValueError):
        line.start_width = -1.0
    with pytest.raises(ValueError):
        line.texture_scale = [float("nan"), 1.0]
    with pytest.raises(ValueError):
        line.num_corner_vertices = 1025
    with pytest.raises(IndexError):
        line.get_position(99)
