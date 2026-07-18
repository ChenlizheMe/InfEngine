from __future__ import annotations

import shutil
import subprocess

import pytest

from Infernux.particle import (
    GpuParticleGlslLowerer,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleKernelLowerer,
)


def _gpu_source():
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(stable_id="gpu-particle")
    )
    kernel = ParticleKernelLowerer().lower(hir)
    return GpuParticleGlslLowerer().lower(kernel)


def test_gpu_lowerer_emits_resident_compute_lifecycle_and_indirect_output():
    program = _gpu_source()
    assert len(program.emitters) == 1
    emitter = program.emitters[0]

    assert set(emitter.stages()) == {
        "bootstrap",
        "init",
        "update",
        "render_reset",
        "rendering",
    }
    assert "buffer ParticleStates" in emitter.update
    assert "inx_pop_free" in emitter.init
    assert "atomicAdd(indirect_args.instance_count, 1u)" in emitter.rendering
    assert "layout(push_constant)" in emitter.rendering
    assert "Vk" not in "\n".join(emitter.stages().values())
    assert {stable_id for stable_id, _field, _type in emitter.attribute_fields} >= {
        "builtin.position",
        "builtin.velocity",
        "builtin.color",
        "builtin.id",
    }


def test_generated_gpu_particle_kernels_compile_to_vulkan_spirv(tmp_path):
    validator = shutil.which("glslangValidator")
    if validator is None:
        pytest.skip("Vulkan SDK glslangValidator is unavailable")

    emitter = _gpu_source().emitters[0]
    for stage, source in emitter.stages().items():
        source_path = tmp_path / f"{stage}.comp"
        output_path = tmp_path / f"{stage}.spv"
        source_path.write_text(source, encoding="utf-8")
        result = subprocess.run(
            [validator, "-V", str(source_path), "-o", str(output_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert output_path.stat().st_size > 20
