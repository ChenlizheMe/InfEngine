"""Shader stage surface is graphics-only and rejects compute explicitly."""

from __future__ import annotations

from Infernux.core.shader import Shader
from Infernux.engine.ui.project_file_ops import create_shader


def test_shader_control_api_rejects_compute():
    for operation in (
        lambda: Shader.is_loaded("parallel", "compute"),
        lambda: Shader.load_spirv("parallel", b"", "compute"),
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
        assert 'Name "stage_' + stage + '"' in source
        assert "@shader_id" not in source
