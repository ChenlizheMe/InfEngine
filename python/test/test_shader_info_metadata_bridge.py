from __future__ import annotations

import json

from Infernux.engine.ui import inspector_shader_utils
from Infernux.engine.ui import inspector_material
from Infernux.mcp.tools import api as mcp_api
from Infernux.mcp.tools import material as mcp_material


def _write_meta(path, metadata):
    document = {
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


def test_inspector_normalizes_encoded_hdr_vector_metadata(tmp_path):
    shader = tmp_path / "encoded.frag"
    shader.write_text('ShaderInfo { Name "Encoded/Hdr" }\n', encoding="utf-8")
    _write_meta(
        shader,
        {
            "shader_id": "Encoded/Hdr",
            "properties": json.dumps([{
                "name": "emissionColor",
                "type": "Color",
                "default": "[0.0, 0.0, 0.0, 0.0], HDR",
                "hdr": False,
            }]),
        },
    )

    assert inspector_shader_utils.parse_shader_properties(str(shader)) == [{
        "name": "emissionColor",
        "type": "Color",
        "default": [0.0, 0.0, 0.0, 0.0],
        "hdr": True,
    }]


def test_shader_property_sync_replaces_incompatible_existing_value():
    mat_data = {
        "properties": {
            "emissionColor": {"type": 0, "value": "[0.0, 0.0, 0.0, 0.0], HDR"},
        },
    }
    inspector_shader_utils._apply_shader_props_to_mat(
        mat_data,
        [{
            "name": "emissionColor",
            "type": "Color",
            "default": "[0.1, 0.2, 0.3, 1.0], HDR",
            "hdr": False,
        }],
    )

    assert mat_data["properties"]["emissionColor"] == {
        "type": 7,
        "value": [0.1, 0.2, 0.3, 1.0],
        "hdr": True,
    }


class _StyleScopeContext:
    def __init__(self):
        self.calls = []

    def push_style_var_vec2(self, *_args):
        self.calls.append("push")

    def pop_style_var(self, count):
        self.calls.append(("pop", count))

    def begin_disabled(self, _disabled):
        self.calls.append("begin_disabled")

    def end_disabled(self):
        self.calls.append("end_disabled")


def test_material_inspector_balances_style_scope_after_render_error(monkeypatch):
    ctx = _StyleScopeContext()
    state = type("State", (), {
        "extra": {"native_mat": type("Mat", (), {"file_path": "asset::submat:0"})()},
        "file_path": "",
    })()

    def fail_render(*_args):
        raise ValueError("bad shader property")

    monkeypatch.setattr(inspector_material, "_render_material_body_impl", fail_render)
    try:
        inspector_material.render_material_body(ctx, None, state)
    except ValueError:
        pass
    else:
        raise AssertionError("expected the synthetic material render error")

    assert ctx.calls == [
        "push", "push", "begin_disabled", "end_disabled", ("pop", 2),
    ]


def test_material_shader_object_field_forwards_stable_semantic_id(monkeypatch):
    calls = []

    def render_object_field(*args, **kwargs):
        calls.append((args, kwargs))
        return False

    from Infernux.engine.ui import inspector_components

    monkeypatch.setattr(inspector_components, "render_object_field", render_object_field)
    inspector_material._render_obj_field(
        object(),
        "mat_frag",
        "unlit",
        "Frag",
        "SHADER_FILE",
        lambda _path: None,
        semantic_id="asset.material.shader.fragment",
    )

    assert calls[0][1]["semantic_id"] == "asset.material.shader.fragment"


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


def test_inspector_reads_structured_properties_without_meta(tmp_path):
    shader = tmp_path / "structured.vert"
    shader.write_text(
        'ShaderInfo {\n'
        '    Name "Structured/Fallback"\n'
        '    Properties {\n'
        '        Float amount = 0.25 Range(0.0, 1.0)\n'
        '        Color glow = [0.1, 0.2, 0.3, 1.0] HDR\n'
        '    }\n'
        '}\n'
        "void main() {}\n",
        encoding="utf-8",
    )

    assert inspector_shader_utils.parse_shader_id(str(shader)) == "Structured/Fallback"
    assert inspector_shader_utils.parse_shader_properties(str(shader)) == [
        {"name": "amount", "type": "Float", "default": 0.25, "hdr": False, "range": [0.0, 1.0]},
        {"name": "glow", "type": "Color", "default": [0.1, 0.2, 0.3, 1.0], "hdr": True},
    ]
    assert inspector_shader_utils.is_shader_hidden(str(shader)) is False


def test_inspector_preserves_internal_shader_property_metadata(tmp_path):
    shader = tmp_path / "internal_property.frag"
    shader.write_text(
        'ShaderInfo {\n'
        '    Name "Structured/InternalProperty"\n'
        '    Properties {\n'
        '        Float authored = 1.0\n'
        '        Texture2D implementationMap = white Internal\n'
        '    }\n'
        '}\n',
        encoding="utf-8",
    )

    properties = inspector_shader_utils.parse_shader_properties(str(shader))

    assert properties[0].get("internal", False) is False
    assert properties[1]["internal"] is True


def test_inspector_catalog_reads_structured_shader_without_meta(tmp_path):
    shader = tmp_path / "particle_sprite.vert"
    shader.write_text(
        '// ShaderInfo { Name "Commented Out" }\n'
        'ShaderInfo {\n'
        '    Name "Particle Sprite"\n'
        '    Capabilities [ParticleSprite]\n'
        '}\n',
        encoding="utf-8",
    )

    assert inspector_shader_utils.parse_shader_id(str(shader)) == "Particle Sprite"
    assert inspector_shader_utils.is_shader_hidden(str(shader)) is False


def test_inspector_catalog_reads_structured_hidden_flag_without_meta(tmp_path):
    shader = tmp_path / "internal.vert"
    shader.write_text(
        'ShaderInfo { Name "Internal/Pass" Hidden On }\n', encoding="utf-8"
    )

    assert inspector_shader_utils.parse_shader_id(str(shader)) == "Internal/Pass"
    assert inspector_shader_utils.is_shader_hidden(str(shader)) is True


def test_shader_catalog_cache_tracks_project_search_roots(monkeypatch, tmp_path):
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    (first_root / "first.frag").write_text(
        'ShaderInfo { Name "Tests/First" }\nvoid main() {}\n', encoding="utf-8"
    )
    (second_root / "second.frag").write_text(
        'ShaderInfo { Name "Tests/Second" }\nvoid main() {}\n', encoding="utf-8"
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
            "shader_hidden": False,
            "shader_lighting_type": "unlit",
            "shader_queue": "2010",
            "shader_imports": json.dumps(["lib/common"]),
            "shader_requirements": json.dumps(["Lighting"]),
            "shader_capabilities": json.dumps(["ForwardPlus"]),
            "shader_unsupported": json.dumps(["Deferred"]),
            "shader_inputs": json.dumps([{"name": "uv", "type": "Float2"}]),
            "shader_outputs": json.dumps([]),
            "shader_entries": json.dumps({"Surface": "surface"}),
            "properties": json.dumps([]),
        },
    )

    annotations = mcp_api._parse_shader_annotations(str(shader))
    assert annotations["shader_id"] == "Imported/McpShader"
    assert annotations["imports"] == ["lib/common"]
    assert annotations["requirements"] == ["Lighting"]
    assert annotations["capabilities"] == ["ForwardPlus"]
    assert annotations["unsupported"] == ["Deferred"]
    assert annotations["targets"] == ["Surface"]
    assert annotations["schema_format"] == "ShaderInfo"


def test_mcp_shader_examples_prefer_structured_schema():
    examples = mcp_api._shader_examples()
    assert "ShaderInfo {" in examples["surface_fragment"]
    assert "@" not in examples["surface_fragment"]
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
