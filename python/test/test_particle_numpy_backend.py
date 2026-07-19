from __future__ import annotations

from dataclasses import replace
import threading

import numpy as np
import pytest

from Infernux.graph import GraphDocument, GraphLinkRecord, GraphNodeRecord, PortKind
from Infernux.graph.types import CoordinateSpace
from Infernux.particle import (
    EmitterSettings,
    EmitterShape,
    ExecutionTarget,
    GpuParticleEmitterController,
    NumpyParticleBackendError,
    NumpyParticleCompiler,
    ParticleArtifactRegistry,
    ParticleBurst,
    ParticleEmitterAsset,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleKernelLowerer,
    ParticleKernelProgram,
    ScalarRange,
    decode_particle_runtime_metadata,
    particle_random_f32,
)


def _compile_runtime(settings: EmitterSettings, *, system_seed: int = 0):
    asset = ParticleGraphAsset(
        stable_id="numpy-test",
        emitters=(ParticleEmitterAsset(stable_id="emitter", settings=settings),),
    )
    hir = ParticleGraphCompiler().compile(asset)
    kernel = ParticleKernelLowerer().lower(hir)
    program = NumpyParticleCompiler().compile(hir, kernel)
    return program, program.create_runtime(system_seed=system_seed)


def test_numpy_aot_runtime_uses_dense_contiguous_storage_and_stable_output_buffer():
    settings = EmitterSettings(capacity=16, spawn_rate=10.0)
    program, runtime = _compile_runtime(settings)

    first = runtime.tick(0.1)
    second = runtime.tick(0.1)

    assert runtime.particle_count == 2
    assert first.flags.c_contiguous
    assert second.flags.c_contiguous
    assert np.shares_memory(first, second)
    assert second[:, 2] == pytest.approx([0.2, 0.1])
    assert second[:, 1] == pytest.approx([-0.2943, -0.0981], abs=1e-6)
    assert "for instruction" not in program.emitters[0].update.source
    assert "opcode" not in program.emitters[0].update.source


def test_numpy_random_matches_portable_scalar_golden_for_every_particle():
    settings = EmitterSettings(
        capacity=8,
        seed=42,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 4),),
        lifetime=ScalarRange(2.0, 6.0),
        initial_speed=ScalarRange(0.0, 0.0),
    )
    _program, runtime = _compile_runtime(settings, system_seed=7)

    runtime.tick(0.0)

    expected = np.asarray(
        [
            2.0 + 4.0 * particle_random_f32(7, 42, 0, particle_id, 0, 0, 0)
            for particle_id in range(4)
        ],
        dtype=np.float32,
    )
    np.testing.assert_array_equal(runtime.attributes["builtin.lifetime"][:4], expected)


def test_numpy_aot_executes_authored_random_expression_with_node_seed():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord("lifetime", "particle.init.set_lifetime"),
            GraphNodeRecord("random", "common.random.f32"),
            GraphNodeRecord("seed", "common.constant.u32", properties={"value": 73}),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.init", "out", "lifetime", "in", PortKind.STREAM
            ),
            GraphLinkRecord("value", "random", "value", "lifetime", "value"),
            GraphLinkRecord("seed", "seed", "value", "random", "seed"),
        ),
    )
    settings = EmitterSettings(
        capacity=4,
        seed=19,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 2),),
    )
    asset = ParticleGraphAsset(
        emitters=(ParticleEmitterAsset(stable_id="random", settings=settings, init=init),)
    )
    hir = ParticleGraphCompiler().compile(asset)
    program = NumpyParticleCompiler().compile(hir, ParticleKernelLowerer().lower(hir))
    runtime = program.create_runtime(system_seed=5)

    runtime.tick(0.0)

    expected = np.asarray(
        [particle_random_f32(5, 19, 73, particle_id, 0, 0, 0) for particle_id in range(2)],
        dtype=np.float32,
    )
    np.testing.assert_array_equal(runtime.attributes["builtin.lifetime"][:2], expected)


def test_numpy_sphere_sampling_is_bounded_and_repeatable_after_reset():
    settings = EmitterSettings(
        capacity=32,
        seed=81,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 32),),
        initial_speed=ScalarRange(0.0, 0.0),
        gravity=(0.0, 0.0, 0.0),
        shape=EmitterShape(
            "sphere",
            CoordinateSpace.WORLD,
            radius=3.0,
        ),
    )
    _program, runtime = _compile_runtime(settings, system_seed=9)
    runtime.tick(0.0)
    first = runtime.attributes["builtin.position"][:32].copy()

    runtime.reset()
    runtime.tick(0.0)
    second = runtime.attributes["builtin.position"][:32]

    assert np.linalg.norm(first, axis=1).max() <= 3.0
    assert np.count_nonzero(np.linalg.norm(first, axis=1) > 0.0) == 32
    np.testing.assert_array_equal(first, second)


def test_numpy_space_conversion_applies_translation_only_to_positions():
    settings = EmitterSettings(
        capacity=4,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 1),),
        gravity=(0.0, 0.0, 0.0),
        shape=EmitterShape("point", CoordinateSpace.EMITTER_LOCAL),
    )
    _program, runtime = _compile_runtime(settings)
    emitter_to_world = np.eye(4, dtype=np.float32)
    emitter_to_world[:3, 3] = [10.0, 20.0, 30.0]
    runtime.set_transforms(emitter_to_world, np.eye(4, dtype=np.float32))

    runtime.tick(0.0)

    np.testing.assert_array_equal(
        runtime.attributes["builtin.position"][0],
        np.asarray([10.0, 20.0, 30.0], dtype=np.float32),
    )
    np.testing.assert_array_equal(
        runtime.attributes["builtin.velocity"][0],
        np.asarray([0.0, 0.0, 1.0], dtype=np.float32),
    )


def test_numpy_runtime_honors_pause_capacity_and_non_finite_policy():
    settings = EmitterSettings(
        capacity=3,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 8),),
    )
    _program, runtime = _compile_runtime(settings)
    runtime.tick(0.0)
    assert runtime.particle_count == 3
    np.testing.assert_array_equal(runtime.attributes["builtin.id"][:3], [0, 1, 2])

    runtime.pause()
    paused_step = runtime.simulation_step
    paused = runtime.tick(1.0)
    assert runtime.simulation_step == paused_step
    assert paused.shape == (3, 9)

    runtime.play()
    runtime.attributes["builtin.position"][1, 0] = np.inf
    runtime.tick(0.0)
    assert runtime.particle_count == 2
    assert np.isfinite(runtime.attributes["builtin.position"][:2]).all()


def test_numpy_runtime_thread_ownership_can_only_move_while_paused():
    _program, runtime = _compile_runtime(EmitterSettings(capacity=2, spawn_rate=0.0))
    runtime.tick(0.0)
    with pytest.raises(NumpyParticleBackendError, match="pause"):
        runtime.release_thread_ownership()

    runtime.pause()
    runtime.release_thread_ownership()
    errors = []

    def run_on_worker():
        try:
            runtime.play()
            runtime.tick(0.0)
        except Exception as exc:  # pragma: no cover - assertion reports worker failures
            errors.append(exc)

    worker = threading.Thread(target=run_on_worker)
    worker.start()
    worker.join()

    assert errors == []


def test_numpy_compiler_rejects_mismatched_hir_and_kernel_programs():
    first = ParticleGraphCompiler().compile(ParticleGraphAsset(stable_id="first"))
    second_asset = ParticleGraphAsset(
        stable_id="second",
        emitters=(
            ParticleEmitterAsset(
                settings=replace(EmitterSettings(), spawn_rate=123.0),
            ),
        ),
    )
    second = ParticleGraphCompiler().compile(second_asset)

    with pytest.raises(NumpyParticleBackendError, match="behavior hashes"):
        NumpyParticleCompiler().compile(first, ParticleKernelLowerer().lower(second))


def test_numpy_compiler_selects_cpu_emitters_from_mixed_target_program():
    asset = ParticleGraphAsset(
        stable_id="mixed-backend-program",
        emitters=(
            ParticleEmitterAsset(
                stable_id="cpu-smoke",
                settings=EmitterSettings(target=ExecutionTarget.CPU),
            ),
            ParticleEmitterAsset(
                stable_id="gpu-sparks",
                settings=EmitterSettings(target=ExecutionTarget.GPU),
            ),
        ),
    )
    hir = ParticleGraphCompiler().compile(asset)
    kernel = ParticleKernelLowerer().lower(hir)

    program = NumpyParticleCompiler().compile(
        hir,
        kernel,
        emitter_ids={"cpu-smoke"},
    )

    assert [emitter.stable_id for emitter in program.emitters] == ["cpu-smoke"]
    with pytest.raises(NumpyParticleBackendError, match="unknown"):
        NumpyParticleCompiler().compile(hir, kernel, emitter_ids={"removed-emitter"})


def test_numpy_compiler_loads_save_time_particle_artifact_without_source_graph_compile(tmp_path):
    asset = ParticleGraphAsset(
        stable_id="artifact-runtime",
        emitters=(
            ParticleEmitterAsset(
                stable_id="smoke",
                settings=EmitterSettings(
                    capacity=12,
                    spawn_rate=0.0,
                    bursts=(ParticleBurst(0.0, 3),),
                ),
            ),
        ),
    )
    source = tmp_path / "Smoke.particlegraph"
    source.write_text(asset.canonical_json(), encoding="utf-8")
    ParticleArtifactRegistry.clear()
    artifact = ParticleArtifactRegistry.compile_path(str(source), guid="smoke-guid")

    program = NumpyParticleCompiler().compile(
        artifact.hir,
        ParticleKernelProgram.from_dict(artifact.kernel_ir),
    )
    runtime = program.create_runtime()

    instances = runtime.tick(0.0)

    assert instances.shape == (3, 9)
    assert program.emitters[0].settings.capacity == 12
    assert len(program.emitters[0].outputs) == 1
    assert program.emitters[0].outputs[0].output_type == "sprite"


def test_numpy_compiler_does_not_silently_run_an_explicit_gpu_emitter():
    asset = ParticleGraphAsset(
        emitters=(
            ParticleEmitterAsset(
                stable_id="gpu-only",
                settings=EmitterSettings(target=ExecutionTarget.GPU),
            ),
        ),
    )
    hir = ParticleGraphCompiler().compile(asset)

    with pytest.raises(NumpyParticleBackendError, match="requires the GPU backend"):
        NumpyParticleCompiler().compile(hir, ParticleKernelLowerer().lower(hir))


def test_runtime_metadata_is_backend_neutral_and_preserves_schedule():
    asset = ParticleGraphAsset(
        stable_id="runtime-metadata",
        emitters=(
            ParticleEmitterAsset(
                stable_id="gpu-only",
                settings=EmitterSettings(target=ExecutionTarget.GPU, capacity=64),
            ),
            ParticleEmitterAsset(
                stable_id="portable",
                settings=EmitterSettings(target=ExecutionTarget.AUTO, capacity=32),
            ),
        ),
    )

    metadata = decode_particle_runtime_metadata(ParticleGraphCompiler().compile(asset))

    assert metadata.schedule == ("gpu-only", "portable")
    assert metadata.emitters[0].settings.target is ExecutionTarget.GPU
    assert metadata.emitters[0].settings.capacity == 64
    assert metadata.emitters[0].outputs[0].output_type == "sprite"


def test_gpu_controller_schedules_bursts_pause_and_resume_without_particle_storage():
    controller = GpuParticleEmitterController(
        EmitterSettings(
            capacity=8,
            seed=17,
            spawn_rate=2.0,
            bursts=(ParticleBurst(0.0, 3), ParticleBurst(1.0, 2)),
        )
    )

    first = controller.tick(0.0)
    second = controller.tick(0.5)
    controller.pause()
    paused = controller.tick(10.0)
    controller.play()
    resumed = controller.tick(0.5)

    assert (first.spawn_count, first.spawn_base_id, first.simulation_step) == (3, 0, 0)
    assert (second.spawn_count, second.spawn_base_id, second.simulation_step) == (1, 3, 1)
    assert paused.spawn_count == 0
    assert paused.simulate is False
    assert paused.simulation_step == 2
    assert (resumed.spawn_count, resumed.spawn_base_id, resumed.simulation_step) == (3, 4, 2)
    assert resumed.system_seed == 17


def test_gpu_controller_caps_dispatch_work_and_resets_deterministically():
    controller = GpuParticleEmitterController(
        EmitterSettings(
            capacity=4,
            spawn_rate=0.0,
            bursts=(ParticleBurst(0.0, 100),),
        )
    )

    assert controller.tick(0.0).spawn_count == 4
    controller.reset(playing=False)
    assert controller.tick(1.0).spawn_count == 0
    controller.play()
    restarted = controller.tick(0.0)
    assert (restarted.spawn_count, restarted.spawn_base_id, restarted.simulation_step) == (4, 0, 0)
