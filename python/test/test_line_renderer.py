from __future__ import annotations

import pytest

from Infernux import LineRenderer as LineRendererComponent
from Infernux.lib import LineAlignment, LineRenderer as NativeLineRenderer, LineTextureMode, Vector3


def _xyz(value):
    return pytest.approx((float(value[0]), float(value[1]), float(value[2])))


def test_line_renderer_is_available_through_the_component_system(scene):
    owner = scene.create_game_object("Line")
    line = owner.add_component("LineRenderer")

    assert isinstance(line, LineRendererComponent)
    assert line.position_count == 2
    assert line._cpp_component.vertex_count == 4
    assert line._cpp_component.index_count == 6


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
    line.texture_scale = 3.0

    document = line.serialize_document()
    assert document["type"] == "LineRenderer"
    assert document["useInlineMesh"] is False
    assert "inlineVertices" not in document
    assert "inlineIndices" not in document
    assert len(document["positions"]) == 3

    copy = NativeLineRenderer()
    assert copy.deserialize_document(document)
    assert copy.position_count == 3
    assert _xyz(copy.get_position(2)) == (4.0, 5.0, 6.0)
    assert copy.index_count == 12
    assert copy.use_world_space is True
    assert copy.alignment == LineAlignment.TransformZ
    assert copy.texture_mode == LineTextureMode.Tile


def test_line_renderer_simplify_and_validation(scene):
    line = scene.create_game_object("SimplifiedLine").add_component("LineRenderer")
    line.set_positions([(0, 0, 0), (1, 0.001, 0), (2, 0, 0), (3, 1, 0)])
    line.simplify(0.01)
    assert line.position_count == 3

    with pytest.raises(ValueError):
        line.start_width = -1.0
    with pytest.raises(ValueError):
        line.texture_scale = -1.0
    with pytest.raises(IndexError):
        line.get_position(99)
