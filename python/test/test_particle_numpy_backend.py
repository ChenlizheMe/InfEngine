from __future__ import annotations

from dataclasses import replace
import threading

import numpy as np
import pytest

from Infernux.graph import GraphDocument, GraphLinkRecord, GraphNodeRecord, PortKind
from Infernux.graph.types import AssetReference, CoordinateSpace
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
    PointCache,
    ParticleRuntimeCompatibility,
    ScalarRange,
    VectorField,
    VectorFieldBoundary,
    VectorFieldFilter,
    decode_particle_runtime_metadata,
    particle_random_f32,
)


class _FakePointCache:
    def __init__(self, positions, stable_ids=(0, 1, 2)):
        self.generation = 1
        self._positions = np.asarray(positions, dtype=np.float32)
        self._stable_ids = np.asarray(stable_ids, dtype=np.uint32)
        self.lookup_count = 0

    @property
    def point_count(self):
        return self._positions.shape[0]

    def channel_array(self, name):
        if name == "position":
            return self._positions
        if name == "stable_id":
            return self._stable_ids
        raise KeyError(name)

    def lookup_indices(self, stable_ids, point_indices):
        self.lookup_count += 1
        mapping = {int(value): index for index, value in enumerate(self._stable_ids)}
        for index, value in enumerate(stable_ids):
            point_indices[index] = mapping.get(int(value), 0xFFFFFFFF)

    def replace_positions(self, positions):
        self._positions = np.asarray(positions, dtype=np.float32)
        self.generation += 1


class _FakeVectorField:
    def __init__(self, volume, *, bake_basis=None):
        self.generation = 1
        self._volume = np.asarray(volume)
        self.bake_basis = tuple(
            bake_basis
            or (
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1.0,
                0.0,
                0.0,
                0.0,
                0.0,
                1.0,
            )
        )

    def volume_array(self):
        return self._volume

    def replace_volume(self, volume):
        self._volume = np.asarray(volume)
        self.generation += 1


def _compile_runtime(settings: EmitterSettings, *, system_seed: int = 0):
    asset = ParticleGraphAsset(
        stable_id="numpy-test",
        emitters=(ParticleEmitterAsset(stable_id="emitter", settings=settings),),
    )
    hir = ParticleGraphCompiler().compile(asset)
    kernel = ParticleKernelLowerer().lower(hir)
    program = NumpyParticleCompiler().compile(hir, kernel)
    return program, program.create_runtime(system_seed=system_seed)


def _compile_emitter_program(settings: EmitterSettings, *, stable_id: str = "emitter"):
    asset = ParticleGraphAsset(
        stable_id="numpy-migration-test",
        emitters=(ParticleEmitterAsset(stable_id=stable_id, settings=settings),),
    )
    hir = ParticleGraphCompiler().compile(asset)
    return NumpyParticleCompiler().compile(
        hir,
        ParticleKernelLowerer().lower(hir),
    ).emitters[0]


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


def test_numpy_plane_collision_resolves_penetration_bounce_and_tangent_friction():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord(
                "velocity",
                "particle.init.set_velocity",
                properties={"value": [1.0, -2.0, 0.0]},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.init", "out", "velocity", "in", PortKind.STREAM
            ),
        ),
    )
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "collision",
                "particle.update.collide_plane",
                properties={"radius": 0.25, "restitution": 0.5, "friction": 0.25},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "collision", "in", PortKind.STREAM
            ),
        ),
    )
    settings = EmitterSettings(
        capacity=1,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 1),),
        lifetime=ScalarRange(10.0, 10.0),
        initial_speed=ScalarRange(0.0, 0.0),
        gravity=(0.0, 0.0, 0.0),
    )
    asset = ParticleGraphAsset(
        emitters=(ParticleEmitterAsset(settings=settings, init=init, update=update),)
    )
    hir = ParticleGraphCompiler().compile(asset)
    program = NumpyParticleCompiler().compile(
        hir, ParticleKernelLowerer().lower(hir)
    )
    runtime = program.create_runtime()

    runtime.tick(0.0)

    np.testing.assert_allclose(
        runtime.attributes["builtin.position"][0], [0.0, 0.25, 0.0]
    )
    np.testing.assert_allclose(
        runtime.attributes["builtin.velocity"][0], [0.75, 1.0, 0.0]
    )


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


def test_numpy_cone_initial_velocity_broadcasts_per_particle_speed():
    settings = EmitterSettings(
        capacity=8,
        seed=42,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 4),),
        lifetime=ScalarRange(2.0, 2.0),
        initial_speed=ScalarRange(1.0, 3.0),
        gravity=(0.0, 0.0, 0.0),
        shape=EmitterShape(kind="cone", radius=0.2, angle_degrees=40.0),
    )
    _program, runtime = _compile_runtime(settings, system_seed=7)

    runtime.tick(0.0)

    velocity = runtime.attributes["builtin.velocity"][:4]
    speed = np.linalg.norm(velocity, axis=1)
    assert runtime.particle_count == 4
    assert np.isfinite(velocity).all()
    assert np.all(speed >= 1.0)
    assert np.all(speed <= 3.0)
    assert np.unique(speed).size > 1


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


def test_numpy_kill_if_is_composable_and_cannot_resurrect_a_dead_particle():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("kill-old", "particle.update.kill_if"),
            GraphNodeRecord("kill-impossible", "particle.update.kill_if"),
            GraphNodeRecord("age", "particle.attribute.read_f32"),
            GraphNodeRecord("half", "common.constant.f32", properties={"value": 0.5}),
            GraphNodeRecord("minus-one", "common.constant.f32", properties={"value": -1.0}),
            GraphNodeRecord("older", "common.compare.greater_than"),
            GraphNodeRecord("impossible", "common.compare.less_than"),
        ),
        links=(
            GraphLinkRecord("stream-a", "root.update", "out", "kill-old", "in", PortKind.STREAM),
            GraphLinkRecord("stream-b", "kill-old", "out", "kill-impossible", "in", PortKind.STREAM),
            GraphLinkRecord("older-a", "age", "value", "older", "a", PortKind.VALUE),
            GraphLinkRecord("older-b", "half", "value", "older", "b", PortKind.VALUE),
            GraphLinkRecord("kill-a", "older", "result", "kill-old", "condition", PortKind.VALUE),
            GraphLinkRecord("impossible-a", "age", "value", "impossible", "a", PortKind.VALUE),
            GraphLinkRecord("impossible-b", "minus-one", "value", "impossible", "b", PortKind.VALUE),
            GraphLinkRecord(
                "kill-b", "impossible", "result", "kill-impossible", "condition", PortKind.VALUE
            ),
        ),
    )
    settings = EmitterSettings(
        capacity=4,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 1),),
        lifetime=ScalarRange(10.0, 10.0),
        initial_speed=ScalarRange(0.0, 0.0),
        gravity=(0.0, 0.0, 0.0),
    )
    asset = ParticleGraphAsset(
        emitters=(ParticleEmitterAsset(stable_id="kill", settings=settings, update=update),)
    )
    hir = ParticleGraphCompiler().compile(asset)
    program = NumpyParticleCompiler().compile(hir, ParticleKernelLowerer().lower(hir))
    runtime = program.create_runtime()

    runtime.tick(0.0)
    assert runtime.particle_count == 1
    runtime.tick(0.6)

    assert runtime.particle_count == 0
    assert "logical_and" in program.emitters[0].update.source


def test_numpy_vector_noise_is_deterministic_finite_and_has_no_particle_loop():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("acceleration", "particle.update.acceleration"),
            GraphNodeRecord("position", "particle.attribute.read_vec3"),
            GraphNodeRecord("age", "particle.attribute.read_f32"),
            GraphNodeRecord(
                "noise",
                "common.noise.vector3d",
                properties={"seed": 41},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "acceleration", "in", PortKind.STREAM
            ),
            GraphLinkRecord("position", "position", "value", "noise", "position"),
            GraphLinkRecord("frequency", "age", "value", "noise", "frequency"),
            GraphLinkRecord("noise", "noise", "value", "acceleration", "value"),
        ),
    )
    settings = EmitterSettings(
        capacity=4,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 2),),
        lifetime=ScalarRange(10.0, 10.0),
        initial_speed=ScalarRange(0.0, 0.0),
        gravity=(0.0, 0.0, 0.0),
    )
    asset = ParticleGraphAsset(
        emitters=(ParticleEmitterAsset(stable_id="noise", settings=settings, update=update),)
    )
    hir = ParticleGraphCompiler().compile(asset)
    program = NumpyParticleCompiler().compile(hir, ParticleKernelLowerer().lower(hir))
    first = program.create_runtime(system_seed=3)
    second = program.create_runtime(system_seed=3)

    first.tick(0.0)
    second.tick(0.0)
    first.tick(0.25)
    second.tick(0.25)

    assert np.isfinite(first.attributes["builtin.position"][:2]).all()
    np.testing.assert_array_equal(
        first.attributes["builtin.position"][:2],
        second.attributes["builtin.position"][:2],
    )
    assert not np.allclose(first.attributes["builtin.position"][:2], 0.0)
    assert "_vector_noise_3d" in program.emitters[0].update.source
    assert "for instruction" not in program.emitters[0].update.source


def test_numpy_aot_executes_color_size_and_rotation_over_lifetime():
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
            GraphNodeRecord(
                "start-color",
                "common.constant.color",
                properties={"value": [1.0, 0.5, 0.0, 1.0]},
            ),
            GraphNodeRecord(
                "end-color",
                "common.constant.color",
                properties={"value": [0.0, 0.0, 0.0, 0.0]},
            ),
            GraphNodeRecord("color-over-life", "common.math.lerp"),
            GraphNodeRecord("size-over-life", "common.math.lerp", properties={"a": 2.0, "b": 0.5}),
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
    settings = EmitterSettings(
        capacity=4,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 4),),
        lifetime=ScalarRange(2.0, 2.0),
        initial_speed=ScalarRange(0.0, 0.0),
        gravity=(0.0, 0.0, 0.0),
    )
    asset = ParticleGraphAsset(
        emitters=(ParticleEmitterAsset(settings=settings, update=update),),
    )
    hir = ParticleGraphCompiler().compile(asset)
    program = NumpyParticleCompiler().compile(hir, ParticleKernelLowerer().lower(hir))
    runtime = program.create_runtime()

    runtime.tick(0.0)
    runtime.tick(1.0)

    np.testing.assert_allclose(
        runtime.attributes["builtin.color"][:4],
        [[0.5, 0.25, 0.0, 0.5]] * 4,
    )
    assert runtime.attributes["builtin.size"][:4] == pytest.approx([1.25] * 4)
    assert runtime.attributes["builtin.rotation"][:4] == pytest.approx(
        [np.pi / 2.0] * 4
    )
    assert runtime.instance_buffer()[:4, 8] == pytest.approx([np.pi / 2.0] * 4)


def test_numpy_rotate_operation_integrates_degrees_per_second():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "rotate",
                "particle.update.rotate",
                properties={"degrees_per_second": 180.0},
            ),
        ),
        links=(
            GraphLinkRecord("stream", "root.update", "out", "rotate", "in", PortKind.STREAM),
        ),
    )
    settings = EmitterSettings(
        capacity=1,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 1),),
        lifetime=ScalarRange(2.0, 2.0),
        initial_speed=ScalarRange(0.0, 0.0),
        gravity=(0.0, 0.0, 0.0),
    )
    asset = ParticleGraphAsset(
        emitters=(ParticleEmitterAsset(settings=settings, update=update),),
    )
    hir = ParticleGraphCompiler().compile(asset)
    program = NumpyParticleCompiler().compile(hir, ParticleKernelLowerer().lower(hir))
    runtime = program.create_runtime()

    runtime.tick(0.0)
    runtime.tick(0.5)

    assert runtime.attributes["builtin.rotation"][0] == pytest.approx(np.pi / 2.0)
    assert runtime.instance_buffer()[0, 8] == pytest.approx(np.pi / 2.0)


def test_numpy_mesh_orientation_matches_gpu_degree_semantics():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord(
                "orientation",
                "particle.attribute.set_orientation",
                properties={"degrees": [10.0, 20.0, 30.0]},
            ),
            GraphNodeRecord(
                "scale",
                "particle.attribute.set_scale",
                properties={"value": [2.0, 0.5, 1.5]},
            ),
        ),
        links=(
            GraphLinkRecord("init-stream", "root.init", "out", "orientation", "in", PortKind.STREAM),
            GraphLinkRecord("scale-stream", "orientation", "out", "scale", "in", PortKind.STREAM),
        ),
    )
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "rotate",
                "particle.update.rotate_orientation",
                properties={"degrees_per_second": [90.0, 180.0, 270.0]},
            ),
        ),
        links=(GraphLinkRecord("update-stream", "root.update", "out", "rotate", "in", PortKind.STREAM),),
    )
    settings = EmitterSettings(
        capacity=1,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 1),),
        lifetime=ScalarRange(2.0, 2.0),
        initial_speed=ScalarRange(0.0, 0.0),
        gravity=(0.0, 0.0, 0.0),
    )
    asset = ParticleGraphAsset(
        emitters=(ParticleEmitterAsset(settings=settings, init=init, update=update),)
    )
    hir = ParticleGraphCompiler().compile(asset)
    program = NumpyParticleCompiler().compile(hir, ParticleKernelLowerer().lower(hir))
    runtime = program.create_runtime()

    runtime.tick(0.0)
    runtime.tick(0.5)

    np.testing.assert_allclose(
        runtime.attributes["builtin.orientation"][0],
        np.radians([55.0, 110.0, 165.0]),
        rtol=1e-6,
        atol=1e-6,
    )
    np.testing.assert_allclose(runtime.attributes["builtin.scale"][0], [2.0, 0.5, 1.5])
    np.testing.assert_allclose(runtime.instance_buffer()[0, 9:12], [2.0, 0.5, 1.5])


def test_numpy_aot_samples_curve_and_gradient_without_runtime_graph_dispatch():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("set-size", "particle.attribute.set_size"),
            GraphNodeRecord("set-color", "particle.attribute.set_color"),
            GraphNodeRecord("age", "particle.attribute.read_f32"),
            GraphNodeRecord("curve", "common.curve.sample", properties={"curve": {
                "keys": [
                    {"time": 0.0, "value": 0.0, "in_tangent": 1.0, "out_tangent": 1.0},
                    {"time": 1.0, "value": 1.0, "in_tangent": 1.0, "out_tangent": 1.0},
                ],
                "pre_wrap": "clamp", "post_wrap": "clamp",
            }}),
            GraphNodeRecord("gradient", "common.gradient.sample", properties={"gradient": {
                "keys": [
                    {"time": 0.0, "color": [1.0, 0.0, 0.0, 1.0]},
                    {"time": 1.0, "color": [0.0, 0.0, 1.0, 0.0]},
                ],
                "mode": "linear",
            }}),
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
    settings = EmitterSettings(
        capacity=1, spawn_rate=0.0, bursts=(ParticleBurst(0.0, 1),),
        lifetime=ScalarRange(2.0, 2.0), initial_speed=ScalarRange(0.0, 0.0),
        gravity=(0.0, 0.0, 0.0),
    )
    asset = ParticleGraphAsset(emitters=(ParticleEmitterAsset(settings=settings, update=update),))
    hir = ParticleGraphCompiler().compile(asset)
    program = NumpyParticleCompiler().compile(hir, ParticleKernelLowerer().lower(hir))
    runtime = program.create_runtime()

    runtime.tick(0.0)
    runtime.tick(0.5)

    assert runtime.attributes["builtin.size"][0] == pytest.approx(0.5)
    np.testing.assert_allclose(runtime.attributes["builtin.color"][0], [0.5, 0.0, 0.5, 0.5])
    assert "_sample_curve" in program.emitters[0].update.source
    assert "_sample_gradient" in program.emitters[0].update.source


def test_numpy_curve_wrap_and_fixed_gradient_follow_portable_contract():
    from Infernux.particle.numpy_backend import (
        _prepare_curve,
        _prepare_gradient,
        _sample_curve,
        _sample_gradient,
    )

    curve = _prepare_curve({
        "keys": [
            {"time": 0.0, "value": 0.0, "in_tangent": 1.0, "out_tangent": 1.0},
            {"time": 1.0, "value": 1.0, "in_tangent": 1.0, "out_tangent": 1.0},
        ],
        "pre_wrap": "ping_pong",
        "post_wrap": "repeat",
    })
    curve_output = np.empty(4, dtype=np.float32)
    _sample_curve(curve_output, np.asarray([-0.25, 0.25, 1.25, 2.25]), curve)
    np.testing.assert_allclose(curve_output, [0.25, 0.25, 0.25, 0.25])

    gradient = _prepare_gradient({
        "keys": [
            {"time": 0.0, "color": [1.0, 0.0, 0.0, 1.0]},
            {"time": 0.5, "color": [0.0, 1.0, 0.0, 1.0]},
            {"time": 1.0, "color": [0.0, 0.0, 1.0, 1.0]},
        ],
        "mode": "fixed",
    })
    gradient_output = np.empty((4, 4), dtype=np.float32)
    _sample_gradient(gradient_output, np.asarray([-1.0, 0.25, 0.75, 2.0]), gradient)
    np.testing.assert_array_equal(
        gradient_output,
        [
            [1.0, 0.0, 0.0, 1.0],
            [1.0, 0.0, 0.0, 1.0],
            [0.0, 1.0, 0.0, 1.0],
            [0.0, 0.0, 1.0, 1.0],
        ],
    )


def test_numpy_point_cache_sampling_uses_typed_interface_and_refreshes_generation():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord("velocity", "particle.init.set_velocity"),
            GraphNodeRecord(
                "particle_id",
                "particle.attribute.read_u32",
                properties={"attribute": "builtin.id"},
            ),
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
            GraphLinkRecord(
                "id", "particle_id", "value", "sample", "index"
            ),
            GraphLinkRecord("value", "sample", "value", "velocity", "value"),
        ),
    )
    cache_to_world = (
        1.0,
        0.0,
        0.0,
        10.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )
    interface = PointCache(
        stable_id="spawn-points",
        cache=AssetReference(guid="fake-cache"),
        space=CoordinateSpace.WORLD,
        cache_to_space=cache_to_world,
        id_channel="stable_id",
    )
    settings = EmitterSettings(
        capacity=3,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 3),),
        initial_speed=ScalarRange(0.0, 0.0),
        gravity=(0.0, 0.0, 0.0),
    )
    asset = ParticleGraphAsset(
        emitters=(
            ParticleEmitterAsset(
                stable_id="point-cache-emitter",
                settings=settings,
                init=init,
                data_interfaces=(interface,),
            ),
        )
    )
    fake = _FakePointCache([[1, 2, 3], [4, 5, 6], [7, 8, 9]])
    hir = ParticleGraphCompiler().compile(asset)
    kernel = ParticleKernelLowerer().lower(hir)
    sample = next(
        instruction
        for instruction in kernel.emitters[0].init.instructions
        if instruction.opcode == "sample_point_cache"
    )
    assert sample.immediate_dict() == {
        "interface": "spawn-points",
        "channel": "$position",
        "lookup": "stable_id",
        "semantic": "position",
    }
    program = NumpyParticleCompiler().compile(
        hir,
        kernel,
        point_cache_resolver=lambda _interface: fake,
    )
    runtime = program.create_runtime()

    runtime.tick(0.0)
    np.testing.assert_array_equal(
        runtime.attributes["builtin.velocity"][:3],
        [[11, 2, 3], [14, 5, 6], [17, 8, 9]],
    )
    assert fake.lookup_count > 0

    fake.replace_positions([[2, 3, 4], [5, 6, 7], [8, 9, 10]])
    runtime.reset()
    runtime.tick(0.0)
    np.testing.assert_array_equal(
        runtime.attributes["builtin.velocity"][:3],
        [[12, 3, 4], [15, 6, 7], [18, 9, 10]],
    )


def _vector_field_runtime(
    fake,
    *,
    boundary=VectorFieldBoundary.CLAMP,
    filtering=VectorFieldFilter.NEAREST,
    field_to_space=None,
    vector_scale=1.0,
):
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("acceleration", "particle.update.acceleration"),
            GraphNodeRecord(
                "position",
                "particle.attribute.read_vec3",
                properties={"attribute": "builtin.position"},
            ),
            GraphNodeRecord(
                "sample",
                "particle.vector_field.sample",
                properties={"interface": "wind"},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream",
                "root.update",
                "out",
                "acceleration",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord("position", "position", "value", "sample", "position"),
            GraphLinkRecord("value", "sample", "value", "acceleration", "value"),
        ),
    )
    interface = VectorField(
        stable_id="wind",
        texture=AssetReference(guid="fake-vector-field"),
        field_to_space=field_to_space
        or (
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
        ),
        vector_scale=vector_scale,
        boundary=boundary,
        filtering=filtering,
    )
    settings = EmitterSettings(
        capacity=1,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 1),),
        lifetime=ScalarRange(10.0, 10.0),
        initial_speed=ScalarRange(0.0, 0.0),
        gravity=(0.0, 0.0, 0.0),
    )
    asset = ParticleGraphAsset(
        emitters=(
            ParticleEmitterAsset(
                stable_id="vector-field-emitter",
                settings=settings,
                update=update,
                data_interfaces=(interface,),
            ),
        )
    )
    hir = ParticleGraphCompiler().compile(asset)
    kernel = ParticleKernelLowerer().lower(hir)
    program = NumpyParticleCompiler().compile(
        hir,
        kernel,
        vector_field_resolver=lambda _interface: fake,
    )
    runtime = program.create_runtime()
    runtime.tick(0.0)
    return kernel, runtime


def _sample_vector_field(runtime, position):
    runtime.attributes["builtin.position"][0] = position
    runtime.attributes["builtin.velocity"][0] = 0.0
    runtime.tick(1.0)
    return runtime.attributes["builtin.velocity"][0].copy()


def test_numpy_vector_field_sampling_supports_nearest_boundaries_and_generation_refresh():
    volume = np.zeros((1, 1, 2, 4), dtype=np.float16)
    volume[0, 0, 0, :3] = (1.0, 2.0, 3.0)
    volume[0, 0, 1, :3] = (4.0, 5.0, 6.0)
    fake = _FakeVectorField(volume)
    kernel, runtime = _vector_field_runtime(fake)

    instruction = next(
        item
        for item in kernel.emitters[0].update.instructions
        if item.opcode == "sample_vector_field"
    )
    assert instruction.immediate_dict() == {"interface": "wind"}
    np.testing.assert_array_equal(_sample_vector_field(runtime, (0.75, 0.0, 0.0)), (4, 5, 6))

    zero_fake = _FakeVectorField(volume)
    _kernel, zero_runtime = _vector_field_runtime(
        zero_fake,
        boundary=VectorFieldBoundary.ZERO,
    )
    np.testing.assert_array_equal(_sample_vector_field(zero_runtime, (1.25, 0.0, 0.0)), (0, 0, 0))

    repeat_fake = _FakeVectorField(volume)
    _kernel, repeat_runtime = _vector_field_runtime(
        repeat_fake,
        boundary=VectorFieldBoundary.REPEAT,
    )
    np.testing.assert_array_equal(_sample_vector_field(repeat_runtime, (1.25, 0.0, 0.0)), (1, 2, 3))

    replacement = volume.copy()
    replacement[0, 0, 1, :3] = (7.0, 8.0, 9.0)
    fake.replace_volume(replacement)
    runtime.reset()
    runtime.tick(0.0)
    np.testing.assert_array_equal(_sample_vector_field(runtime, (0.75, 0.0, 0.0)), (7, 8, 9))


def test_numpy_vector_field_linear_sampling_composes_asset_and_interface_transforms():
    volume = np.zeros((1, 1, 2, 4), dtype=np.float32)
    volume[0, 0, 0, :3] = (0.0, 2.0, 0.0)
    volume[0, 0, 1, :3] = (2.0, 4.0, 0.0)
    field_to_space = (
        2.0,
        0.0,
        0.0,
        0.0,
        0.0,
        2.0,
        0.0,
        0.0,
        0.0,
        0.0,
        2.0,
        0.0,
        0.0,
        0.0,
        0.0,
        1.0,
    )
    _kernel, runtime = _vector_field_runtime(
        _FakeVectorField(volume),
        filtering=VectorFieldFilter.LINEAR,
        field_to_space=field_to_space,
        vector_scale=0.5,
    )

    # World x=1 maps to field x=0.5. The two texels interpolate to (1, 3, 0),
    # while the 2x basis and 0.5 vector scale cancel each other.
    np.testing.assert_allclose(_sample_vector_field(runtime, (1.0, 0.0, 0.0)), (1, 3, 0))


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
    assert paused.shape == (3, 12)

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


def test_numpy_runtime_parameter_reload_preserves_clock_particles_and_pause_state():
    _program, runtime = _compile_runtime(
        EmitterSettings(capacity=8, spawn_rate=2.0),
        system_seed=17,
    )
    runtime.tick(0.5)
    runtime.pause()
    particle_ids = runtime.attributes["builtin.id"][: runtime.particle_count].copy()

    migrated, compatibility = runtime.migrate_to(
        _compile_emitter_program(EmitterSettings(capacity=8, spawn_rate=6.0))
    )

    assert compatibility is ParticleRuntimeCompatibility.PARAMETER_ONLY
    assert migrated is not None
    assert migrated.is_playing is False
    assert migrated.particle_count == 1
    assert migrated.simulation_step == 1
    np.testing.assert_array_equal(
        migrated.attributes["builtin.id"][: migrated.particle_count],
        particle_ids,
    )
    migrated.play()
    migrated.tick(0.5)
    assert migrated.particle_count == 4
    assert migrated.simulation_step == 2


def test_numpy_runtime_kernel_reload_preserves_live_attribute_state():
    settings = EmitterSettings(
        capacity=4,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 1),),
        gravity=(0.0, 0.0, 0.0),
        initial_speed=ScalarRange(0.0, 0.0),
    )
    _program, runtime = _compile_runtime(settings)
    runtime.tick(0.0)
    position = runtime.attributes["builtin.position"][0].copy()

    migrated, compatibility = runtime.migrate_to(
        _compile_emitter_program(replace(settings, gravity=(0.0, -2.0, 0.0)))
    )

    assert compatibility is ParticleRuntimeCompatibility.KERNEL_COMPATIBLE
    assert migrated is not None
    np.testing.assert_array_equal(migrated.attributes["builtin.position"][0], position)
    migrated.tick(0.5)
    assert migrated.attributes["builtin.velocity"][0, 1] == pytest.approx(-1.0)


def test_numpy_runtime_layout_reload_migrates_capacity_and_rejects_burst_change():
    settings = EmitterSettings(
        capacity=4,
        spawn_rate=0.0,
        bursts=(ParticleBurst(0.0, 4),),
    )
    _program, runtime = _compile_runtime(settings)
    runtime.tick(0.0)

    migrated, compatibility = runtime.migrate_to(
        _compile_emitter_program(replace(settings, capacity=2))
    )

    assert compatibility is ParticleRuntimeCompatibility.LAYOUT_MIGRATABLE
    assert migrated is not None
    assert migrated.particle_count == 2
    np.testing.assert_array_equal(migrated.attributes["builtin.id"][:2], [0, 1])

    restarted, compatibility = runtime.migrate_to(
        _compile_emitter_program(
            replace(settings, bursts=(ParticleBurst(0.0, 2),))
        )
    )
    assert restarted is None
    assert compatibility is ParticleRuntimeCompatibility.EMITTER_RESTART


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

    assert instances.shape == (3, 12)
    np.testing.assert_array_equal(instances[:, 9:12], np.ones((3, 3), dtype=np.float32))
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


def test_gpu_controller_compatible_migration_preserves_schedule_and_pause():
    settings = EmitterSettings(
        capacity=16,
        spawn_rate=2.0,
        bursts=(ParticleBurst(0.0, 2),),
    )
    controller = GpuParticleEmitterController(settings)
    first = controller.tick(0.25)
    controller.pause()

    migrated = controller.migrate_to(replace(settings, spawn_rate=6.0))

    assert migrated.is_playing is False
    assert migrated.simulation_step == 1
    paused = migrated.tick(0.25)
    assert (paused.spawn_count, paused.spawn_base_id, paused.simulation_step) == (
        0,
        first.spawn_count,
        1,
    )
    migrated.play()
    resumed = migrated.tick(0.25)
    assert (resumed.spawn_count, resumed.spawn_base_id, resumed.simulation_step) == (
        2,
        first.spawn_count,
        1,
    )

    resized = migrated.migrate_to(replace(migrated.settings, capacity=32))
    assert resized.simulation_step == migrated.simulation_step
    assert resized.is_playing is True

    with pytest.raises(ValueError, match="emitter restart"):
        migrated.migrate_to(
            replace(migrated.settings, bursts=(ParticleBurst(0.0, 4),))
        )
