from __future__ import annotations

from pathlib import Path

import pytest

from Infernux import LineRenderer as LineRendererComponent
from Infernux.graph.ramp import AnimationCurve, Gradient, GradientKey, Keyframe
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


def test_line_renderer_default_material_is_double_sided_and_transparent(scene):
    line = scene.create_game_object("DefaultLine").add_component("LineRenderer")

    material = line._cpp_component.get_effective_material()
    state = material.get_render_state()

    assert material.name == "DefaultLineMaterial"
    assert state.cull_mode == 0
    assert state.blend_enable is True
    assert state.depth_test_enable is True
    assert state.depth_write_enable is False
    assert state.render_queue == 3000


def test_line_renderer_default_material_survives_shader_annotation_defaults(scene):
    """The line ribbon uses the general-purpose "Unlit" surface shader, whose
    annotation defaults (Cull Back, Queue 2000) are stamped onto materials via
    ApplyShaderRenderMeta right before pipeline creation. Back-face culling
    deletes every folded (reversed-winding) trail section and flickers the
    live tip, so the authored fields must be marked as explicit overrides.
    """
    from Infernux.lib import RenderStateOverride

    line = scene.create_game_object("OverrideLine").add_component("LineRenderer")
    material = line._cpp_component.get_effective_material()

    assert material.name == "DefaultLineMaterial"
    for flag in (
        RenderStateOverride.CULL_MODE,
        RenderStateOverride.DEPTH_WRITE,
        RenderStateOverride.DEPTH_TEST,
        RenderStateOverride.BLEND_ENABLE,
        RenderStateOverride.BLEND_MODE,
        RenderStateOverride.RENDER_QUEUE,
    ):
        assert material.has_override(flag), f"missing override for {flag}"


def test_line_renderer_inspector_is_entirely_generic_serialized_fields():
    from Infernux.components.builtin.line_renderer import LineRenderer
    from Infernux.components.fields import FieldType
    from Infernux.engine.ui.inspector_components import _collect_cpp_properties

    assert "render_inspector" not in LineRenderer.__dict__

    fields = dict(_collect_cpp_properties(LineRenderer))
    assert fields["positions"].metadata.field_type == FieldType.LIST
    assert fields["positions"].metadata.element_type == FieldType.VEC3
    assert fields["width_curve"].metadata.field_type == FieldType.ANIMATION_CURVE
    assert fields["width_curve"].metadata.curve_non_negative is True
    assert fields["color_gradient"].metadata.field_type == FieldType.GRADIENT
    assert fields["color_gradient"].metadata.hdr is True


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


def test_line_renderer_view_alignment_preserves_winding_when_direction_reverses():
    shader_root = (
        Path(__file__).parents[1]
        / "Infernux"
        / "resources"
        / "shaders"
        / "_templates"
    )
    for shader_name in ("vertex_main.glsl", "shadow_vertex_main.glsl"):
        source = (shader_root / shader_name).read_text(encoding="utf-8")
        assert "fallbackSide" in source
        assert "geometricLength" in source
        assert "smoothstep(0.025, 0.20, geometricLength)" in source
        # The well-conditioned regime must use the continuous geometric side
        # without any hemisphere snapping: aligning cross(facing, tangent) to
        # a camera axis put screen-horizontal segments exactly on the flip
        # boundary and made ribbons flicker a full width every frame. Only the
        # fallback may adopt the geometric side's hemisphere.
        assert "side = geometricSide / geometricLength;" in source
        assert "dot(fallbackSide, geometricSide) < 0.0" in source
        assert "geometricSide = -geometricSide" not in source
        assert "directionSign = dot(tangentWorld, facing)" not in source
        assert "[3].xyz - centerWorld.xyz" not in source


def test_line_renderer_defaults_to_world_space_like_unity(scene):
    # Trails feed world positions; a local-space default re-transforms them by
    # the owner's world matrix, so a moving owner renders the ribbon displaced
    # and its culling bounds drift away from the visible geometry.
    line = scene.create_game_object("WorldSpaceDefault").add_component("LineRenderer")

    assert line.use_world_space is True
    assert NativeLineRenderer().use_world_space is True


def test_line_renderer_near_reversal_tangent_is_deterministic(scene):
    # incoming + outgoing is a near-zero vector at a perfect turnaround; the
    # rebuilt tangent must fall back to the outgoing direction instead of
    # normalizing float noise into a random ribbon orientation.
    line = scene.create_game_object("NearReversal").add_component("LineRenderer")
    line.start_width = 0.4
    line.end_width = 0.4
    line.set_positions([(0, 0, 0), (1, 1e-9, 0), (1e-8, 0, 0)])
    target = scene.create_game_object("NearReversalBake").add_component("MeshRenderer")

    line.bake_mesh(target)

    positions = target._cpp_component.get_positions()
    assert len(positions) == 6
    for position in positions:
        for channel in position:
            assert abs(float(channel)) < 10.0
    first_side_y = float(positions[1][1]) - float(positions[0][1])
    assert abs(first_side_y) > 0.1
    for sample in range(1, 3):
        side_y = float(positions[sample * 2 + 1][1]) - float(positions[sample * 2][1])
        assert side_y * first_side_y > 0.0


def test_line_renderer_retraced_path_keeps_ribbon_sides_continuous(scene):
    line = scene.create_game_object("RetracedLine").add_component("LineRenderer")
    line.set_positions([(0, 0, 0), (1, 0, 0), (0, 0, 0)])
    line.start_width = 0.4
    line.end_width = 0.4
    target = scene.create_game_object("RetracedBake").add_component("MeshRenderer")

    line.bake_mesh(target)

    positions = target._cpp_component.get_positions()
    assert len(positions) == 6
    first_side_y = float(positions[1][1]) - float(positions[0][1])
    assert abs(first_side_y) > 0.1
    for sample in range(1, 3):
        side_y = float(positions[sample * 2 + 1][1]) - float(
            positions[sample * 2][1]
        )
        assert side_y * first_side_y > 0.0


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
    line.width_curve = AnimationCurve(
        (
            Keyframe(0.0, 0.25),
            Keyframe(0.5, 1.5, 0.0, 0.0),
            Keyframe(1.0, 0.75),
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

    line.width_curve = AnimationCurve(
        (Keyframe(0.0, 0.1), Keyframe(0.5, 1.0), Keyframe(1.0, 0.2)),
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
