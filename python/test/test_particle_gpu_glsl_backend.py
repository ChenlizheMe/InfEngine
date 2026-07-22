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
    PointCache,
    VectorField,
    build_gpu_particle_migration,
    compile_gpu_particle_spirv,
    standard_particle_attributes,
    validate_gpu_particle_spirv,
)
from Infernux.graph import GraphDocument, GraphLinkRecord, GraphNodeRecord, PortKind
from Infernux.graph.types import AssetReference, CoordinateSpace, TypeRef, ValueType


def _gpu_source():
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(stable_id="gpu-particle")
    )
    kernel = ParticleKernelLowerer().lower(hir)
    return GpuParticleGlslLowerer().lower(kernel)


def _kill_if_gpu_source():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("kill", "particle.update.kill_if"),
            GraphNodeRecord("age", "particle.attribute.read_f32"),
            GraphNodeRecord("limit", "common.constant.f32", properties={"value": 0.5}),
            GraphNodeRecord("older", "common.compare.greater_than"),
        ),
        links=(
            GraphLinkRecord("stream", "root.update", "out", "kill", "in", PortKind.STREAM),
            GraphLinkRecord("a", "age", "value", "older", "a", PortKind.VALUE),
            GraphLinkRecord("b", "limit", "value", "older", "b", PortKind.VALUE),
            GraphLinkRecord("condition", "older", "result", "kill", "condition", PortKind.VALUE),
        ),
    )
    emitter = ParticleEmitterAsset(stable_id="kill", update=update)
    hir = ParticleGraphCompiler().compile(ParticleGraphAsset(emitters=(emitter,)))
    return GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir))


def _noise_gpu_source():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("acceleration", "particle.update.acceleration"),
            GraphNodeRecord("position", "particle.attribute.read_vec3"),
            GraphNodeRecord(
                "noise",
                "common.noise.vector3d",
                properties={"frequency": 2.0, "seed": 17},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "acceleration", "in", PortKind.STREAM
            ),
            GraphLinkRecord("position", "position", "value", "noise", "position"),
            GraphLinkRecord("noise", "noise", "value", "acceleration", "value"),
        ),
    )
    emitter = ParticleEmitterAsset(stable_id="noise", update=update)
    hir = ParticleGraphCompiler().compile(ParticleGraphAsset(emitters=(emitter,)))
    return GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir))


def test_gpu_kill_if_accumulates_death_and_feeds_the_existing_free_list():
    source = _kill_if_gpu_source().emitters[0].update

    assert "particle_alive = particle_alive && !(" in source
    assert " > " in source
    assert "inx_push_free" in source


def test_gpu_vector_noise_uses_the_portable_hash_and_compiles_to_spirv():
    source = _noise_gpu_source()
    update = source.emitters[0].update

    assert "inx_noise_hash" in update
    assert "inx_vector_noise_3d" in update
    payload = compile_gpu_particle_spirv(source)
    assert validate_gpu_particle_spirv(payload, source) is payload


def _point_cache_gpu_source():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord("velocity", "particle.init.set_velocity"),
            GraphNodeRecord("particle_id", "particle.attribute.read_u32"),
            GraphNodeRecord(
                "sample",
                "particle.point_cache.sample_position",
                properties={
                    "interface": "spawn-points",
                    "channel": "$position",
                    "lookup": "stable_id",
                    "semantic": "position",
                },
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.init", "out", "velocity", "in", PortKind.STREAM
            ),
            GraphLinkRecord("id", "particle_id", "value", "sample", "index"),
            GraphLinkRecord("value", "sample", "value", "velocity", "value"),
        ),
    )
    emitter = ParticleEmitterAsset(
        stable_id="point-cache-emitter",
        init=init,
        data_interfaces=(
            PointCache(
                stable_id="spawn-points",
                cache=AssetReference(guid="point-cache-guid"),
                space=CoordinateSpace.WORLD,
                id_channel="stable_id",
            ),
        ),
    )
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(stable_id="point-cache-gpu", emitters=(emitter,))
    )
    return GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir))


def _vector_field_gpu_source(*, boundary="zero", filtering="linear"):
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("acceleration", "particle.update.acceleration"),
            GraphNodeRecord("position", "particle.attribute.read_vec3"),
            GraphNodeRecord(
                "sample",
                "particle.vector_field.sample",
                properties={"interface": "wind"},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "acceleration", "in", PortKind.STREAM
            ),
            GraphLinkRecord("position", "position", "value", "sample", "position"),
            GraphLinkRecord("value", "sample", "value", "acceleration", "value"),
        ),
    )
    emitter = ParticleEmitterAsset(
        stable_id="vector-field-emitter",
        update=update,
        data_interfaces=(
            VectorField(
                stable_id="wind",
                texture=AssetReference(guid="wind-texture-guid"),
                boundary=boundary,
                filtering=filtering,
            ),
        ),
    )
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(stable_id="vector-field-gpu", emitters=(emitter,))
    )
    return GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir))


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


def test_gpu_lowerer_emits_lifecycle_divide_lerp_rotation_and_attribute_stores():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("set-color", "particle.attribute.set_color"),
            GraphNodeRecord("set-size", "particle.attribute.set_size"),
            GraphNodeRecord("set-rotation", "particle.attribute.set_rotation"),
            GraphNodeRecord("age", "particle.attribute.read_f32"),
            GraphNodeRecord(
                "lifetime",
                "particle.attribute.read_f32",
                properties={"attribute": "builtin.lifetime"},
            ),
            GraphNodeRecord("normalized-age", "common.math.divide"),
            GraphNodeRecord("start-color", "common.constant.color"),
            GraphNodeRecord(
                "end-color",
                "common.constant.color",
                properties={"value": [0.0, 0.0, 0.0, 0.0]},
            ),
            GraphNodeRecord("color-over-life", "common.math.lerp"),
            GraphNodeRecord("size-over-life", "common.math.lerp", properties={"a": 1.0, "b": 0.0}),
            GraphNodeRecord(
                "rotation-over-life",
                "common.math.lerp",
                properties={"a": 0.0, "b": 3.141592653589793},
            ),
        ),
        links=(
            GraphLinkRecord("stream-color", "root.update", "out", "set-color", "in", PortKind.STREAM),
            GraphLinkRecord("stream-size", "set-color", "out", "set-size", "in", PortKind.STREAM),
            GraphLinkRecord(
                "stream-rotation", "set-size", "out", "set-rotation", "in", PortKind.STREAM
            ),
            GraphLinkRecord("age-divide", "age", "value", "normalized-age", "a"),
            GraphLinkRecord("life-divide", "lifetime", "value", "normalized-age", "b"),
            GraphLinkRecord("color-a", "start-color", "value", "color-over-life", "a"),
            GraphLinkRecord("color-b", "end-color", "value", "color-over-life", "b"),
            GraphLinkRecord("color-t", "normalized-age", "result", "color-over-life", "t"),
            GraphLinkRecord("size-t", "normalized-age", "result", "size-over-life", "t"),
            GraphLinkRecord(
                "rotation-t", "normalized-age", "result", "rotation-over-life", "t"
            ),
            GraphLinkRecord("set-color-value", "color-over-life", "result", "set-color", "value"),
            GraphLinkRecord("set-size-value", "size-over-life", "result", "set-size", "value"),
            GraphLinkRecord(
                "set-rotation-value",
                "rotation-over-life",
                "result",
                "set-rotation",
                "value",
            ),
        ),
    )
    asset = ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    hir = ParticleGraphCompiler().compile(asset)
    source = GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir)).emitters[0].update

    assert " / " in source
    assert "mix(" in source
    assert ".a_builtin_color = " in source
    assert ".a_builtin_size = " in source
    assert ".a_builtin_rotation = " in source

    rendering = GpuParticleGlslLowerer().lower(
        ParticleKernelLowerer().lower(hir)
    ).emitters[0].rendering
    assert "rotation_custom = vec4(" in rendering
    assert ".a_builtin_rotation" in rendering


def test_gpu_curve_and_gradient_sampling_emit_valid_vulkan_glsl():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("set-size", "particle.attribute.set_size"),
            GraphNodeRecord("set-color", "particle.attribute.set_color"),
            GraphNodeRecord("age", "particle.attribute.read_f32"),
            GraphNodeRecord(
                "curve",
                "common.curve.sample",
                properties={
                    "curve": {
                        "keys": [
                            {"time": 0.0, "value": 0.0, "in_tangent": 0.0, "out_tangent": 1.0},
                            {"time": 1.0, "value": 1.0, "in_tangent": 1.0, "out_tangent": 0.0},
                        ],
                        "pre_wrap": "ping_pong",
                        "post_wrap": "repeat",
                    }
                },
            ),
            GraphNodeRecord("gradient", "common.gradient.sample"),
        ),
        links=(
            GraphLinkRecord("stream-size", "root.update", "out", "set-size", "in", PortKind.STREAM),
            GraphLinkRecord("stream-color", "set-size", "out", "set-color", "in", PortKind.STREAM),
            GraphLinkRecord("age-curve", "age", "value", "curve", "t"),
            GraphLinkRecord("age-gradient", "age", "value", "gradient", "t"),
            GraphLinkRecord("curve-size", "curve", "value", "set-size", "value"),
            GraphLinkRecord("gradient-color", "gradient", "color", "set-color", "value"),
        ),
    )
    asset = ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    hir = ParticleGraphCompiler().compile(asset)
    emitter = GpuParticleGlslLowerer().lower(ParticleKernelLowerer().lower(hir)).emitters[0]

    assert "_time =" in emitter.update
    assert "mix(vec4(" in emitter.update
    assert "mod(" in emitter.update
    assert "abs(" in emitter.update
    compiled = native._compile_compute_glsl_batch({"update": emitter.update}, "particle-curve-gradient-test")
    assert set(compiled) == {"update"}


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


def test_gpu_point_cache_lowering_emits_stable_set_one_layout_and_valid_spirv():
    emitter = _point_cache_gpu_source().emitters[0]
    layout = emitter.data_interface_layout

    assert layout["version"] == 1
    assert layout["sample_count"] == 1
    assert layout["point_caches"][0]["stable_id"] == "spawn-points"
    assert layout["point_caches"][0]["data_binding"] == 1
    assert layout["point_caches"][0]["lookup_binding"] == 2
    sample = layout["point_caches"][0]["samples"][0]
    assert sample == {
        "sample_index": 0,
        "interface": "spawn-points",
        "channel": "$position",
        "value_type": "vec3",
        "lookup": "stable_id",
        "semantic": "position",
    }
    assert "set = 1, binding = 0" in emitter.init
    assert "inx_pc_resolve_0" in emitter.init
    assert "inx_sample_point_cache_0" in emitter.init

    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), "particle-point-cache-test"
    )
    assert set(compiled) == set(emitter.stages())


def test_gpu_vector_field_lowering_emits_rhi_set_two_layout_and_valid_spirv():
    emitter = _vector_field_gpu_source().emitters[0]
    layout = emitter.data_interface_layout

    assert layout["vector_field_metadata_binding"] == 0
    assert layout["vector_field_stride_words"] == 32
    assert layout["vector_fields"] == [
        {
            "stable_id": "wind",
            "interface_index": 0,
            "texture_binding": 1,
            "boundary": "zero",
            "filtering": "linear",
        }
    ]
    assert "set = 2, binding = 0" in emitter.update
    assert "set = 2, binding = 1" in emitter.update
    assert "uniform sampler3D inx_vf_texture_0" in emitter.update
    assert "any(lessThan(uvw" in emitter.update
    assert "inx_sample_vector_field_0" in emitter.update

    compiled = native._compile_compute_glsl_batch(
        emitter.stages(), "particle-vector-field-test"
    )
    assert set(compiled) == set(emitter.stages())


def test_gpu_vector_field_repeat_nearest_policy_is_preserved_for_rhi_sampler():
    emitter = _vector_field_gpu_source(boundary="repeat", filtering="nearest").emitters[0]
    interface = emitter.data_interface_layout["vector_fields"][0]

    assert interface["boundary"] == "repeat"
    assert interface["filtering"] == "nearest"
    assert "any(lessThan(uvw" not in emitter.update


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
