from __future__ import annotations

from dataclasses import replace
import threading

import numpy as np
import pytest

from Infernux.graph.types import CoordinateSpace
from Infernux.particle import (
    EmitterSettings,
    EmitterShape,
    NumpyParticleBackendError,
    NumpyParticleCompiler,
    ParticleBurst,
    ParticleEmitterAsset,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleKernelLowerer,
    ScalarRange,
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
