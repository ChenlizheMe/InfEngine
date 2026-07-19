from __future__ import annotations

import copy
import shutil
import struct
import subprocess

import pytest

from Infernux.lib import _Infernux as native
from Infernux.particle import (
    GpuParticleGlslLowerer,
    GpuParticleCompileError,
    ParticleAttribute,
    ParticleEmitterAsset,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleKernelLowerer,
    build_gpu_particle_migration,
    compile_gpu_particle_spirv,
    standard_particle_attributes,
    validate_gpu_particle_spirv,
)
from Infernux.graph.types import TypeRef, ValueType


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
    assert {
        stable_id for stable_id, _field, _type, _offset, _size in emitter.attribute_fields
    } >= {
        "builtin.position",
        "builtin.velocity",
        "builtin.color",
        "builtin.id",
    }
    assert emitter.state_stride == 80
    document = emitter.to_dict()
    assert document["state_stride"] == 80
    assert all(
        field["offset"] >= 8 and field["byte_size"] % 4 == 0
        for field in document["attribute_fields"]
    )


def test_gpu_layout_migration_descriptor_copies_stable_fields_and_packs_defaults():
    stable_id = "layout-emitter"
    previous_asset = ParticleGraphAsset(
        stable_id="previous-layout",
        emitters=(ParticleEmitterAsset(stable_id=stable_id),),
    )
    next_asset = ParticleGraphAsset(
        stable_id="next-layout",
        emitters=(
            ParticleEmitterAsset(
                stable_id=stable_id,
                attributes=(
                    *standard_particle_attributes(),
                    ParticleAttribute(
                        "custom.temperature",
                        "temperature",
                        TypeRef(ValueType.F32),
                        3.5,
                    ),
                ),
            ),
        ),
    )
    previous_kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(previous_asset)
    )
    next_kernel = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(next_asset)
    )
    previous_layout = GpuParticleGlslLowerer().lower(previous_kernel).emitters[0].to_dict()
    next_layout = GpuParticleGlslLowerer().lower(next_kernel).emitters[0].to_dict()

    migration = build_gpu_particle_migration(
        previous_layout,
        next_layout,
        next_kernel.emitters[0],
    )

    assert migration["source_stride"] == previous_layout["state_stride"]
    assert migration["destination_stride"] == next_layout["state_stride"]
    assert len(migration["copy_ranges"]) == len(standard_particle_attributes())
    temperature = next(
        field
        for field in next_layout["attribute_fields"]
        if field["stable_id"] == "custom.temperature"
    )
    default_word = migration["default_state_words"][temperature["offset"] // 4]
    assert struct.unpack("<f", struct.pack("<I", default_word))[0] == pytest.approx(3.5)
    assert all(
        item["source_offset"] % 4 == 0
        and item["destination_offset"] % 4 == 0
        and item["byte_size"] % 4 == 0
        for item in migration["copy_ranges"]
    )

    stale_layout = copy.deepcopy(previous_layout)
    stale_layout["attribute_fields"][0].pop("offset")
    with pytest.raises(GpuParticleCompileError, match="layout entry"):
        build_gpu_particle_migration(stale_layout, next_layout, next_kernel.emitters[0])


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


def test_engine_compute_compiler_batches_generated_particle_stages():
    emitter = _gpu_source().emitters[0]
    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), "particle-test"
    )

    assert set(compiled) == set(emitter.stages())
    for spirv in compiled.values():
        assert isinstance(spirv, bytes)
        assert len(spirv) > 20
        assert int.from_bytes(spirv[:4], "little") == 0x07230203


def test_persisted_particle_spirv_is_compressed_and_integrity_checked():
    source = _gpu_source()
    payload = compile_gpu_particle_spirv(source)

    assert validate_gpu_particle_spirv(payload, source) is payload
    descriptor = payload["emitters"][0]["stages"]["update"]
    assert descriptor["byte_size"] > 20
    assert len(descriptor["sha256"]) == 64
    assert len(descriptor["zlib_base64"]) < descriptor["byte_size"] * 2

    corrupted = copy.deepcopy(payload)
    corrupted["emitters"][0]["stages"]["update"]["sha256"] = "0" * 64
    with pytest.raises(GpuParticleCompileError, match="integrity validation"):
        validate_gpu_particle_spirv(corrupted, source)
