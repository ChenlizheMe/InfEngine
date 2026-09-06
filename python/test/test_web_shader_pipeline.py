from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
PLUGIN_EDITOR = (
    ROOT / "external" / "plugins" / "infernux_web" / "package" / "editor"
)


def test_raw_shader_compatibility_fixtures_are_not_shipped_as_authored_assets():
    plugin = ROOT / "external/plugins/infernux_web"
    fixtures = plugin / "tests/fixtures/shader_compat"
    for suffix in ("vert", "frag"):
        assert (fixtures / f"representative.{suffix}").read_text().startswith("#version 450")
        assert not tuple((plugin / "package").rglob(f"representative.{suffix}"))


@pytest.fixture
def shader_pipeline(monkeypatch):
    monkeypatch.syspath_prepend(str(PLUGIN_EDITOR))
    for name in tuple(sys.modules):
        if name == "infernux_web" or name.startswith("infernux_web."):
            sys.modules.pop(name)
    importlib.import_module("infernux_web")
    return importlib.import_module("infernux_web.shader_pipeline")


def test_web_shader_preparation_lowers_push_constants_and_splits_samplers(
    shader_pipeline,
):
    source = """#version 450
layout(push_constant) uniform DrawConstants {
    mat4 transform;
    vec4 tint;
} drawConstants;
layout(set = 2, binding = 7) uniform sampler2D colorTexture;
void main() {}
"""

    prepared = shader_pipeline.prepare_glsl_for_webgpu(source)

    assert "layout(push_constant)" not in prepared.source
    assert "layout(std140, set = 0, binding = 999)" in prepared.source
    assert prepared.uses_push_constant_uniform
    assert prepared.tint_sampler_mapping == "2,7:2,507"
    assert prepared.sampler_bindings == (
        shader_pipeline.WebSamplerBinding("colorTexture", 2, 7, 507),
    )


def test_web_shader_preparation_rejects_reserved_binding(shader_pipeline):
    with pytest.raises(
        shader_pipeline.WebShaderCompatibilityError,
        match="reserved by the Web shader ABI",
    ):
        shader_pipeline.prepare_glsl_for_webgpu(
            """#version 450
layout(set = 0, binding = 500) uniform sampler2D invalidTexture;
void main() {}
"""
        )


def test_web_shader_preparation_rejects_push_constant_arrays(shader_pipeline):
    with pytest.raises(
        shader_pipeline.WebShaderCompatibilityError,
        match="does not support array members",
    ):
        shader_pipeline.prepare_glsl_for_webgpu(
            """#version 450
layout(push_constant) uniform DrawConstants {
    vec4 values[2];
} drawConstants;
void main() {}
"""
        )


def test_web_shader_pipeline_uses_vulkan_11_and_remaps_sampler_binding(
    shader_pipeline, monkeypatch, tmp_path
):
    calls: list[tuple[str, ...]] = []

    def run(command, *, capture_output, text, check):
        arguments = tuple(str(item) for item in command)
        calls.append(arguments)
        output = (
            Path(arguments[arguments.index("--output-name") + 1])
            if "--output-name" in arguments
            else None
        )
        if output is not None:
            input_path = Path(arguments[-1])
            if input_path.suffix == ".wgsl":
                output.write_text(input_path.read_text(encoding="utf-8"), encoding="utf-8")
            else:
                output.write_text(
                    "@group(0) @binding(999) var<uniform> drawConstants : vec4<f32>;\n"
                    "@group(0) @binding(1) var sourceTexture_sampler : sampler;\n"
                    "@group(0) @binding(3) var sourceTexture_image : texture_2d<f32>;\n",
                    encoding="utf-8",
                )
        elif "-o" in arguments:
            Path(arguments[arguments.index("-o") + 1]).write_bytes(b"SPV")
        return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(shader_pipeline.subprocess, "run", run)
    compiled = shader_pipeline.compile_glsl_to_wgsl(
        """#version 450
layout(push_constant) uniform DrawConstants { vec4 tint; } drawConstants;
layout(set = 0, binding = 3) uniform sampler2D sourceTexture;
void main() {}
""",
        "fragment",
        glslang=tmp_path / "glslangValidator",
        tint=tmp_path / "tint",
    )

    assert compiled.stage == "frag"
    assert calls[0][1:4] == ("-V", "--target-env", "vulkan1.1")
    assert "-DINX_WEBGPU=1" in calls[0]
    assert "--sampler-mapping" not in calls[1]
    assert len(calls) == 3
    assert "@binding(503u) var sourceTexture_sampler" in compiled.wgsl


def test_web_shader_manifest_builds_current_runtime_catalog(
    shader_pipeline, monkeypatch, tmp_path
):
    source = tmp_path / "stage.vert"
    source.write_text("#version 450\nvoid main() {}\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "shaders": [
                    {
                        "name": "Fullscreen Triangle",
                        "stage": "vertex",
                        "source": source.name,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        shader_pipeline,
        "compile_glsl_to_wgsl",
        lambda source, stage, **_kwargs: shader_pipeline.CompiledWebShader(
            "@vertex fn main() {}\n",
            shader_pipeline.PreparedWebShader(source, (), False),
            "vert",
        ),
    )

    output = tmp_path / "output"
    catalog = shader_pipeline.compile_shader_manifest(manifest, output)

    assert catalog["$schema"] == "infernux.web_shader_catalog"
    assert "version" not in catalog
    entry = catalog["shaders"][0]
    assert entry["name"] == "Fullscreen Triangle"
    assert entry["stage"] == "vertex"
    assert entry["path"].endswith(".vert.wgsl")
    assert (output / entry["path"]).read_text(encoding="utf-8") == (
        "@vertex fn main() {}\n"
    )
    assert json.loads((output / "catalog.json").read_text(encoding="utf-8")) == catalog


def test_web_shader_manifest_rejects_duplicate_identity(
    shader_pipeline, tmp_path
):
    source = tmp_path / "stage.vert"
    source.write_text("#version 450\nvoid main() {}\n", encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "shaders": [
                    {"name": "Same", "stage": "vertex", "source": source.name},
                    {"name": "Same", "stage": "vertex", "source": source.name},
                ]
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(
        shader_pipeline.WebShaderCompatibilityError,
        match="Duplicate Web shader identity",
    ):
        shader_pipeline.compile_shader_manifest(manifest, tmp_path / "output")
