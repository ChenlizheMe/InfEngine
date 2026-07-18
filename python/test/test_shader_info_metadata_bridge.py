from __future__ import annotations

import json

from Infernux.engine.ui import inspector_shader_utils
from Infernux.engine.ui import inspector_material
from Infernux.mcp.tools import api as mcp_api
from Infernux.mcp.tools import material as mcp_material


def _write_meta(path, metadata):
    document = {
        "meta_version": 2,
        "metadata": {
            key: {
                "type": "bool" if isinstance(value, bool) else "string",
                "value": value,
            }
            for key, value in metadata.items()
        },
    }
    path.with_name(path.name + ".meta").write_text(
        json.dumps(document), encoding="utf-8"
    )


def test_inspector_reads_native_shader_schema_without_parsing_source(tmp_path):
    shader = tmp_path / "structured.frag"
    shader.write_text(
        'ShaderInfo { Name "Source/ShouldNotBeParsed" }\nvoid main() {}\n',
        encoding="utf-8",
    )
    properties = [
        {
            "name": "baseColor",
            "type": "Color",
            "default": [1.0, 0.5, 0.25, 1.0],
            "hdr": True,
            "range": [0.0, 1.0],
            "line": 7,
            "column": 9,
        }
    ]
    _write_meta(
        shader,
        {
            "shader_id": "Imported/Structured",
            "shader_schema_format": "ShaderInfo",
            "properties": json.dumps(properties),
            "shader_hidden": True,
        },
    )

    assert inspector_shader_utils.parse_shader_id(str(shader)) == "Imported/Structured"
    assert inspector_shader_utils.parse_shader_properties(str(shader)) == properties
    assert inspector_shader_utils.is_shader_hidden(str(shader)) is True


def test_shader_range_is_preserved_in_material_property_metadata():
    mat_data = {"properties": {"amount": {"type": 0, "value": 0.25}}}
    inspector_shader_utils._apply_shader_props_to_mat(
        mat_data,
        [{
            "name": "amount",
            "type": "Float",
            "default": 0.5,
            "hdr": False,
            "range": [0.0, 4.0],
        }],
    )

    assert mat_data["properties"]["amount"]["value"] == 0.25
    assert mat_data["properties"]["amount"]["range"] == [0.0, 4.0]

    inspector_shader_utils._apply_shader_props_to_mat(
        mat_data,
        [{
            "name": "amount",
            "type": "Float",
            "default": 0.5,
            "hdr": False,
        }],
    )
    assert "range" not in mat_data["properties"]["amount"]


class _RangePropertyContext:
    def __init__(self):
        self.calls = []

    def align_text_to_frame_padding(self):
        pass

    def label(self, _label):
        pass

    def same_line(self, _width):
        pass

    def set_next_item_width(self, _width):
        pass

    def float_slider(self, label, value, minimum, maximum):
        self.calls.append(("float_slider", label, value, minimum, maximum))
        return 0.75

    def int_slider(self, label, value, minimum, maximum):
        self.calls.append(("int_slider", label, value, minimum, maximum))
        return 3

    def drag_float(self, *_args):
        raise AssertionError("A ranged Float must use float_slider")

    def drag_int(self, *_args):
        raise AssertionError("A ranged Int must use int_slider")


def test_material_inspector_uses_shader_range_sliders():
    ctx = _RangePropertyContext()
    float_prop = {"type": 0, "value": 0.25, "range": [0.0, 1.0]}
    int_prop = {"type": 4, "value": 1, "range": [0, 8]}

    assert inspector_material.render_material_property(
        ctx, "amount", float_prop, 0, float_prop["value"], 80.0
    ) is True
    assert inspector_material.render_material_property(
        ctx, "steps", int_prop, 4, int_prop["value"], 80.0
    ) is True

    assert float_prop["value"] == 0.75
    assert int_prop["value"] == 3
    assert ctx.calls == [
        ("float_slider", "##mp_amount", 0.25, 0.0, 1.0),
        ("int_slider", "##mp_steps", 1, 0, 8),
    ]


def test_inspector_keeps_legacy_annotation_fallback(tmp_path):
    shader = tmp_path / "legacy.vert"
    shader.write_text(
        "@shader_id: Legacy/Fallback\n"
        "@property: amount, Float, 0.25\n"
        "void main() {}\n",
        encoding="utf-8",
    )

    assert inspector_shader_utils.parse_shader_id(str(shader)) == "Legacy/Fallback"
    assert inspector_shader_utils.parse_shader_properties(str(shader)) == [
        {"name": "amount", "type": "Float", "default": 0.25, "hdr": False}
    ]
    assert inspector_shader_utils.is_shader_hidden(str(shader)) is False


def test_shader_catalog_cache_tracks_project_search_roots(monkeypatch, tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "first.frag").write_text(
        "@shader_id: Tests/First\nvoid main() {}\n", encoding="utf-8"
    )
    (second_root / "second.frag").write_text(
        "@shader_id: Tests/Second\nvoid main() {}\n", encoding="utf-8"
    )
    roots = [str(first_root)]
    monkeypatch.setattr(inspector_shader_utils, "_get_shader_search_roots", lambda: roots)
    inspector_shader_utils.bump_shader_property_generation()

    assert inspector_shader_utils._get_shader_catalog(".frag")["items"] == [
        ("Tests/First", "Tests/First")
    ]

    roots = [str(second_root)]
    assert inspector_shader_utils._get_shader_catalog(".frag")["items"] == [
        ("Tests/Second", "Tests/Second")
    ]


def test_mcp_catalog_consumes_native_shader_metadata(tmp_path):
    shader = tmp_path / "structured.frag"
    shader.write_text(
        'ShaderInfo { Name "Source/ShouldNotBeParsed" }\nvoid main() {}\n',
        encoding="utf-8",
    )
    _write_meta(
        shader,
        {
            "shader_id": "Imported/McpShader",
            "shader_schema_format": "ShaderInfo",
            "shader_schema_version": "1",
            "shader_hidden": False,
            "shader_lighting_type": "unlit",
            "shader_queue": "2010",
            "shader_imports": json.dumps(["lib/common"]),
            "shader_capabilities": json.dumps(["ForwardPlus"]),
            "shader_inputs": json.dumps([{"name": "uv", "type": "Float2"}]),
            "shader_outputs": json.dumps([]),
            "shader_entries": json.dumps({"Surface": "surface"}),
            "properties": json.dumps([]),
        },
    )

    annotations = mcp_api._parse_shader_annotations(str(shader))
    assert annotations["shader_id"] == "Imported/McpShader"
    assert annotations["imports"] == ["lib/common"]
    assert annotations["capabilities"] == ["ForwardPlus"]
    assert annotations["targets"] == ["Surface"]
    assert annotations["schema_format"] == "ShaderInfo"


def test_mcp_shader_examples_prefer_structured_schema():
    examples = mcp_api._shader_examples()
    assert "ShaderInfo {" in examples["surface_fragment"]
    assert "@shader_id" not in examples["surface_fragment"]
    assert "fullscreen_effect_fragment" not in examples


def test_material_shader_stage_validation_uses_imported_catalog(monkeypatch):
    monkeypatch.setattr(
        mcp_api,
        "_scan_shaders",
        lambda: [
            {"shader_id": "standard", "kind": "vertex"},
            {"shader_id": "Imported/McpShader", "kind": "fragment"},
        ],
    )

    mcp_material._require_shader_stage("standard", "vertex")
    mcp_material._require_shader_stage("imported/mcpshader", "fragment")

    try:
        mcp_material._require_shader_stage("standard", "fragment")
    except ValueError as exc:
        assert "not a fragment shader" in str(exc)
    else:
        raise AssertionError("A vertex shader was accepted as a fragment shader")
