"""Shader stage surface is graphics-only and rejects compute explicitly."""

from __future__ import annotations

from Infernux.core.shader import Shader
from Infernux.engine.ui.project_file_ops import create_shader


def test_camera_helpers_use_shader_linker_per_view_camera_contract():
    from pathlib import Path

    source = Path("python/Infernux/resources/shaders/lib/camera.glsl").read_text(
        encoding="utf-8"
    )
    camera_helpers = source[source.index("vec3 getCameraPosition()") :]
    camera_helpers = camera_helpers[: camera_helpers.index("// Camera near plane")]
    assert "INX_SHADING_CAMERA_POSITION" in camera_helpers
    assert "_Globals._WorldSpaceCameraPos" not in camera_helpers


def test_shader_control_api_rejects_compute():
    for operation in (
        lambda: Shader.is_loaded("parallel", "compute"),
        lambda: Shader.reload("parallel", "compute"),
    ):
        try:
            operation()
        except ValueError as exc:
            assert "external parallel backend" in str(exc)
        else:
            raise AssertionError("compute shader stage was accepted")


def test_shader_file_creation_rejects_compute(tmp_path):
    ok, error = create_shader(str(tmp_path), "Parallel", "comp")
    assert not ok
    assert "external parallel backend" in error
    assert not (tmp_path / "Parallel.comp").exists()


def test_shader_file_creation_accepts_graphics_stages(tmp_path):
    for stage in ("vert", "frag"):
        ok, error = create_shader(str(tmp_path), f"Stage_{stage}", stage)
        assert ok, error
        source = (tmp_path / f"Stage_{stage}.{stage}").read_text(encoding="utf-8")
        assert "ShaderInfo {" in source
        assert f'Name "Stage {stage.title()}"' in source
        assert "@" not in source


def test_pbr_specular_highlights_reaches_direct_light_brdf():
    from pathlib import Path

    shader_root = Path("python/Infernux/resources/shaders")
    shading_model = (shader_root / "pbr.shadingmodel").read_text(encoding="utf-8")
    lighting = (shader_root / "lighting.glsl").read_text(encoding="utf-8")
    pbr = (shader_root / "pbr.glsl").read_text(encoding="utf-8")

    assert "s.specularHighlights,\n                                   ctx.viewDepth" in shading_model
    assert "float specularHighlights, float viewDepth, float shadow" in lighting
    assert "energyCompensation,\n                                   specularHighlights" in lighting
    assert "energyCompensation * clamp(specularHighlights, 0.0, 1.0)" in pbr


def test_surface_passes_fall_back_to_geometric_normal():
    from pathlib import Path

    shader_root = Path("python/Infernux/resources/shaders")
    surface = (shader_root / "surface.glsl").read_text(encoding="utf-8")
    assert "s.normalWS = vec3(0.0);" in surface
    assert "vec3 ResolveSurfaceNormal(" in surface

    template_root = shader_root / "_templates"
    for template_name in (
        "surface_main.glsl",
        "surface_main_gbuffer.glsl",
        "surface_main_normal.glsl",
        "particle_sprite_surface_main.glsl",
    ):
        source = (template_root / template_name).read_text(encoding="utf-8")
        surface_call = source.index("${SURFACE_CALL}")
        normal_resolve = source.index("ResolveSurfaceNormal", surface_call)
        assert normal_resolve > surface_call


def test_gizmo_icon_shader_applies_component_vertex_tint():
    from pathlib import Path

    source = Path("python/Infernux/resources/shaders/gizmo_icon.frag").read_text(
        encoding="utf-8"
    )
    assert "texColor.rgb * v_Color * material.baseColor.rgb" in source
