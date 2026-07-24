"""NumPy AOT CPU backend for validated particle Kernel SSA programs."""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
from typing import Any, Callable, Collection, Mapping

import numpy as np

from Infernux.graph.types import CoordinateSpace, TypeRef, ValueType
from Infernux.graph.ramp import Curve, Gradient

from .asset import EmitterSettings, ExecutionTarget
from .data_interface import (
    PointCache,
    SdfFilter,
    SdfVolume,
    VectorField,
    VectorFieldBoundary,
    VectorFieldFilter,
)
from .hir import ParticleOutputDescriptor, ParticleProgramHIR
from .kernel_ir import (
    KernelCompileError,
    ParticleEmitterKernelIR,
    ParticleKernelFunction,
    ParticleKernelProgram,
)
from .runtime_metadata import (
    ParticleRuntimeMetadataError,
    decode_particle_runtime_metadata,
)
from .runtime_compatibility import (
    ParticleRuntimeCompatibility,
    classify_emitter_update,
)


PARTICLE_INSTANCE_FLOATS = 12


class NumpyParticleBackendError(RuntimeError):
    pass


def _point_cache_channel_name(interface: PointCache, authored_name: str) -> str:
    aliases = {
        "$position": interface.position_channel,
        "$normal": interface.normal_channel,
        "$color": interface.color_channel,
        "$id": interface.id_channel,
    }
    return aliases.get(authored_name, authored_name)


class _NumpyPointCacheBinding:
    def __init__(self, interface: PointCache, asset: Any) -> None:
        if asset is None or not hasattr(asset, "channel_array"):
            raise NumpyParticleBackendError(
                f"point cache interface {interface.stable_id!r} did not resolve to an InxPointCache"
            )
        self.interface = interface
        self.asset = asset
        self._generation = -1
        self._channels: dict[str, np.ndarray] = {}
        self._expected_types: dict[str, TypeRef] = {}
        self._cache_to_space = np.asarray(
            interface.cache_to_space, dtype=np.float32
        ).reshape(4, 4)
        self.refresh()

    def refresh(self) -> None:
        generation = int(self.asset.generation)
        if generation == self._generation:
            return
        self._generation = generation
        self._channels = {}

    def require_channel(self, authored_name: str, value_type: TypeRef) -> None:
        name = _point_cache_channel_name(self.interface, authored_name)
        previous = self._expected_types.get(name)
        if previous is not None and previous != value_type:
            raise NumpyParticleBackendError(
                f"point cache channel {name!r} is sampled with incompatible types"
            )
        self._expected_types[name] = value_type
        self.channel(authored_name, value_type)

    def channel(self, authored_name: str, value_type: TypeRef) -> np.ndarray:
        self.refresh()
        name = _point_cache_channel_name(self.interface, authored_name)
        channel = self._channels.get(name)
        if channel is None:
            try:
                channel = self.asset.channel_array(name)
            except (KeyError, RuntimeError, ValueError) as exc:
                raise NumpyParticleBackendError(
                    f"point cache interface {self.interface.stable_id!r} has no usable channel {name!r}"
                ) from exc
            self._channels[name] = channel
        expected_dtype = np.dtype(_dtype(value_type))
        components = _component_count(value_type)
        expected_shape = (
            (int(self.asset.point_count),)
            if components == 1
            else (int(self.asset.point_count), components)
        )
        if channel.dtype != expected_dtype or channel.shape != expected_shape:
            raise NumpyParticleBackendError(
                f"point cache channel {name!r} does not match {value_type.value_type.value}"
            )
        return channel

    def cache_to_simulation(self, context: "_RuntimeContext") -> np.ndarray:
        return (
            context.conversion(
                self.interface.space.value,
                CoordinateSpace.SIMULATION.value,
            )
            @ self._cache_to_space
        )


def _default_point_cache_resolver(interface: PointCache):
    from Infernux.lib import AssetRegistry

    registry = AssetRegistry.instance()
    reference = interface.cache
    asset = (
        registry.load_point_cache_by_guid(reference.guid)
        if reference.guid
        else registry.load_point_cache(reference.path_hint)
        if reference.path_hint
        else None
    )
    if asset is None:
        identity = reference.guid or reference.path_hint or "<empty reference>"
        raise NumpyParticleBackendError(
            f"point cache interface {interface.stable_id!r} cannot load {identity!r}"
        )
    return asset


class _NumpyVectorFieldBinding:
    def __init__(self, interface: VectorField, asset: Any) -> None:
        if asset is None or not hasattr(asset, "volume_array"):
            raise NumpyParticleBackendError(
                f"vector field interface {interface.stable_id!r} did not resolve to a Texture3D"
            )
        self.interface = interface
        self.asset = asset
        self._generation = -1
        self._volume = np.empty((0, 0, 0, 4), dtype=np.float32)
        self._field_to_space = np.asarray(
            interface.field_to_space, dtype=np.float32
        ).reshape(4, 4)
        if not np.isfinite(self._field_to_space).all():
            raise NumpyParticleBackendError(
                f"vector field interface {interface.stable_id!r} transform is not finite"
            )
        self._space_to_field = np.empty((4, 4), dtype=np.float32)
        self._vector_to_space = np.empty((3, 3), dtype=np.float32)
        self.refresh()

    @property
    def volume(self) -> np.ndarray:
        self.refresh()
        return self._volume

    def refresh(self) -> None:
        generation = int(self.asset.generation)
        if generation == self._generation:
            return
        try:
            source = np.asarray(self.asset.volume_array())
            bake_basis = np.asarray(self.asset.bake_basis, dtype=np.float32).reshape(4, 4)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise NumpyParticleBackendError(
                f"vector field interface {self.interface.stable_id!r} has no usable volume generation"
            ) from exc
        if (
            source.ndim != 4
            or source.shape[3] != 4
            or source.dtype not in {np.dtype(np.float16), np.dtype(np.float32)}
            or min(source.shape[:3], default=0) <= 0
            or not np.isfinite(source).all()
            or not np.isfinite(bake_basis).all()
        ):
            raise NumpyParticleBackendError(
                f"vector field interface {self.interface.stable_id!r} volume is invalid"
            )
        source_to_space = self._field_to_space @ bake_basis
        try:
            self._space_to_field = np.linalg.inv(source_to_space).astype(np.float32)
        except np.linalg.LinAlgError as exc:
            raise NumpyParticleBackendError(
                f"vector field interface {self.interface.stable_id!r} transform is singular"
            ) from exc
        self._vector_to_space = np.ascontiguousarray(
            source_to_space[:3, :3] * np.float32(self.interface.vector_scale)
        )
        # CPU execution keeps one optimized immutable copy per published asset
        # generation. Sampling never converts or decodes the full volume per frame.
        self._volume = np.ascontiguousarray(source, dtype=np.float32)
        self._generation = generation

    def sampling_matrices(self, context: "_RuntimeContext") -> tuple[np.ndarray, np.ndarray]:
        simulation_to_space = context.conversion(
            CoordinateSpace.SIMULATION.value,
            self.interface.space.value,
        )
        space_to_simulation = context.conversion(
            self.interface.space.value,
            CoordinateSpace.SIMULATION.value,
        )
        return (
            self._space_to_field @ simulation_to_space,
            space_to_simulation[:3, :3] @ self._vector_to_space,
        )


def _default_vector_field_resolver(interface: VectorField):
    from Infernux.lib import AssetRegistry

    reference = interface.texture
    if not reference.guid:
        identity = reference.path_hint or "<empty reference>"
        raise NumpyParticleBackendError(
            f"vector field interface {interface.stable_id!r} requires an imported texture GUID; got {identity!r}"
        )
    asset = AssetRegistry.instance().load_texture_by_guid(reference.guid)
    if asset is None:
        raise NumpyParticleBackendError(
            f"vector field interface {interface.stable_id!r} cannot load {reference.guid!r}"
        )
    return asset


class _NumpySdfBinding:
    def __init__(self, interface: SdfVolume, asset: Any) -> None:
        if asset is None or not hasattr(asset, "volume_array"):
            raise NumpyParticleBackendError(
                f"SDF interface {interface.stable_id!r} did not resolve to a Texture3D"
            )
        self.interface = interface
        self.asset = asset
        self._generation = -1
        self._volume = np.empty((0, 0, 0, 4), dtype=np.float32)
        self._field_to_space = np.asarray(
            interface.field_to_space, dtype=np.float32
        ).reshape(4, 4)
        self._space_to_field = np.empty((4, 4), dtype=np.float32)
        self._field_to_space_with_bake = np.empty((4, 4), dtype=np.float32)
        self.refresh()

    @property
    def volume(self) -> np.ndarray:
        self.refresh()
        return self._volume

    def refresh(self) -> None:
        generation = int(self.asset.generation)
        if generation == self._generation:
            return
        try:
            source = np.asarray(self.asset.volume_array())
            bake_basis = np.asarray(self.asset.bake_basis, dtype=np.float32).reshape(4, 4)
        except (TypeError, ValueError, RuntimeError) as exc:
            raise NumpyParticleBackendError(
                f"SDF interface {self.interface.stable_id!r} has no usable volume generation"
            ) from exc
        if (
            source.ndim != 4
            or source.shape[3] != 4
            or source.dtype not in {np.dtype(np.float16), np.dtype(np.float32)}
            or min(source.shape[:3], default=0) <= 1
            or not np.isfinite(source).all()
            or not np.isfinite(bake_basis).all()
        ):
            raise NumpyParticleBackendError(
                f"SDF interface {self.interface.stable_id!r} volume is invalid"
            )
        self._field_to_space_with_bake = self._field_to_space @ bake_basis
        try:
            self._space_to_field = np.linalg.inv(
                self._field_to_space_with_bake
            ).astype(np.float32)
        except np.linalg.LinAlgError as exc:
            raise NumpyParticleBackendError(
                f"SDF interface {self.interface.stable_id!r} transform is singular"
            ) from exc
        self._volume = np.ascontiguousarray(source, dtype=np.float32)
        self._generation = generation

    def sampling_matrices(
        self, context: "_RuntimeContext"
    ) -> tuple[np.ndarray, np.ndarray, np.float32]:
        simulation_to_space = context.conversion(
            CoordinateSpace.SIMULATION.value,
            self.interface.space.value,
        )
        space_to_simulation = context.conversion(
            self.interface.space.value,
            CoordinateSpace.SIMULATION.value,
        )
        field_to_simulation = (
            space_to_simulation @ self._field_to_space_with_bake
        )
        linear = field_to_simulation[:3, :3]
        try:
            normal_to_simulation = np.linalg.inv(linear).T.astype(np.float32)
        except np.linalg.LinAlgError as exc:
            raise NumpyParticleBackendError(
                f"SDF interface {self.interface.stable_id!r} normal transform is singular"
            ) from exc
        minimum_scale = np.float32(
            min(float(np.linalg.norm(linear[:, axis])) for axis in range(3))
            * self.interface.distance_scale
        )
        if not np.isfinite(minimum_scale) or minimum_scale <= 0.0:
            raise NumpyParticleBackendError(
                f"SDF interface {self.interface.stable_id!r} distance transform is invalid"
            )
        return (
            self._space_to_field @ simulation_to_space,
            normal_to_simulation,
            minimum_scale,
        )


def _default_sdf_resolver(interface: SdfVolume):
    from Infernux.lib import AssetRegistry

    reference = interface.texture
    if not reference.guid:
        identity = reference.path_hint or "<empty reference>"
        raise NumpyParticleBackendError(
            f"SDF interface {interface.stable_id!r} requires an imported texture GUID; got {identity!r}"
        )
    asset = AssetRegistry.instance().load_texture_by_guid(reference.guid)
    if asset is None:
        raise NumpyParticleBackendError(
            f"SDF interface {interface.stable_id!r} cannot load {reference.guid!r}"
        )
    return asset


class _RuntimeContext:
    def __init__(self, system_seed: int) -> None:
        if type(system_seed) is not int or not 0 <= system_seed <= 0xFFFFFFFF:
            raise NumpyParticleBackendError("system_seed must be an unsigned 32-bit integer")
        self.system_seed = system_seed
        self.delta_time = 0.0
        self.simulation_step = 0
        self._conversions: dict[tuple[CoordinateSpace, CoordinateSpace], np.ndarray] = {}
        self.set_transforms(None, None)

    def set_transforms(self, emitter_to_world, simulation_to_world) -> None:
        identity = np.eye(4, dtype=np.float32)
        emitter = _matrix4(emitter_to_world, identity)
        simulation = _matrix4(simulation_to_world, identity)
        space_to_world = {
            CoordinateSpace.NONE: identity,
            CoordinateSpace.EMITTER_LOCAL: emitter,
            CoordinateSpace.SIMULATION: simulation,
            CoordinateSpace.WORLD: identity,
        }
        conversions = {}
        for source_space, source_to_world in space_to_world.items():
            for target_space, target_to_world in space_to_world.items():
                if source_space is CoordinateSpace.NONE or target_space is CoordinateSpace.NONE:
                    conversions[(source_space, target_space)] = identity
                else:
                    conversions[(source_space, target_space)] = (
                        np.linalg.inv(target_to_world).astype(np.float32) @ source_to_world
                    )
        self._conversions = conversions

    def conversion(self, source: str, target: str) -> np.ndarray:
        source_space = CoordinateSpace(source)
        target_space = CoordinateSpace(target)
        try:
            return self._conversions[(source_space, target_space)]
        except KeyError as exc:
            raise NumpyParticleBackendError(
                f"NumPy backend cannot convert {source_space.value} to {target_space.value}"
            ) from exc


@dataclass(frozen=True)
class _StageLayout:
    scratch_types: tuple[TypeRef, ...]
    export_types: tuple[tuple[str, TypeRef], ...]
    export_aliases: tuple[tuple[str, str], ...]


class _StageWorkspace:
    def __init__(self, layout: _StageLayout, capacity: int) -> None:
        self.scratch = tuple(_allocate_array(capacity, value_type) for value_type in layout.scratch_types)
        self.exports = {
            stable_id: _allocate_array(capacity, value_type)
            for stable_id, value_type in layout.export_types
        }
        self.random_u32_a = np.empty(capacity, dtype=np.uint32)
        self.random_u32_b = np.empty(capacity, dtype=np.uint32)
        self.sample_ids = np.empty(capacity, dtype=np.uint32)
        self.sample_indices = np.empty(capacity, dtype=np.uint32)
        self.sample_valid = np.empty(capacity, dtype=np.bool_)
        self.sample_vector = np.empty((capacity, 3), dtype=np.float32)
        self.field_coordinates = np.empty((capacity, 3), dtype=np.float32)
        self.field_base = np.empty((capacity, 3), dtype=np.int64)
        self.field_next = np.empty((capacity, 3), dtype=np.int64)
        self.field_corner = np.empty((capacity, 3), dtype=np.int64)
        self.field_fraction = np.empty((capacity, 3), dtype=np.float32)
        self.field_linear_index = np.empty(capacity, dtype=np.int64)
        self.field_texel = np.empty((capacity, 4), dtype=np.float32)
        self.field_weight = np.empty(capacity, dtype=np.float32)
        self.field_valid = np.empty(capacity, dtype=np.bool_)
        self.field_component_valid = np.empty((capacity, 3), dtype=np.bool_)
        self.sdf_coordinates = np.empty((capacity, 3), dtype=np.float32)
        self.sdf_normal = np.empty((capacity, 3), dtype=np.float32)
        self.sdf_distance = np.empty(capacity, dtype=np.float32)
        self.sdf_sample_a = np.empty(capacity, dtype=np.float32)
        self.sdf_sample_b = np.empty(capacity, dtype=np.float32)
        self.random_float = tuple(
            np.empty(capacity, dtype=np.float32) for _index in range(6)
        )


@dataclass(frozen=True)
class NumpyStageExecutable:
    stage: str
    source: str
    layout: _StageLayout
    function: Any

    def create_workspace(self, capacity: int) -> _StageWorkspace:
        return _StageWorkspace(self.layout, capacity)


@dataclass(frozen=True)
class NumpyParticleEmitterProgram:
    stable_id: str
    settings: EmitterSettings
    kernel: ParticleEmitterKernelIR
    init: NumpyStageExecutable
    update: NumpyStageExecutable
    rendering: NumpyStageExecutable
    outputs: tuple[ParticleOutputDescriptor, ...]
    point_caches: tuple[tuple[str, _NumpyPointCacheBinding], ...] = ()
    vector_fields: tuple[tuple[str, _NumpyVectorFieldBinding], ...] = ()
    sdf_volumes: tuple[tuple[str, _NumpySdfBinding], ...] = ()

    def create_runtime(self, *, system_seed: int = 0) -> "NumpyParticleEmitterRuntime":
        return NumpyParticleEmitterRuntime(self, system_seed=system_seed)


@dataclass(frozen=True)
class NumpyParticleProgram:
    kernel_hash: str
    emitters: tuple[NumpyParticleEmitterProgram, ...]

    def create_runtime(
        self,
        emitter_index: int = 0,
        *,
        system_seed: int = 0,
    ) -> "NumpyParticleEmitterRuntime":
        if type(emitter_index) is not int or not 0 <= emitter_index < len(self.emitters):
            raise IndexError("particle emitter index is out of range")
        return self.emitters[emitter_index].create_runtime(system_seed=system_seed)


class NumpyParticleCompiler:
    """Compile validated Kernel SSA once into branch-free stage call sequences."""

    def compile(
        self,
        hir: ParticleProgramHIR | Mapping[str, Any],
        kernel: ParticleKernelProgram,
        *,
        emitter_ids: Collection[str] | None = None,
        point_cache_resolver: Callable[[PointCache], Any] | None = None,
        vector_field_resolver: Callable[[VectorField], Any] | None = None,
        sdf_resolver: Callable[[SdfVolume], Any] | None = None,
    ) -> NumpyParticleProgram:
        try:
            metadata = decode_particle_runtime_metadata(hir)
        except ParticleRuntimeMetadataError as exc:
            raise NumpyParticleBackendError(str(exc)) from exc
        if metadata.behavior_hash != kernel.source_behavior_hash:
            raise NumpyParticleBackendError("Particle HIR and Kernel IR behavior hashes differ")
        kernel_ids = tuple(emitter.stable_id for emitter in kernel.emitters)
        if metadata.schedule != kernel_ids:
            raise NumpyParticleBackendError("Particle HIR and Kernel IR emitter schedules differ")
        selected_ids = None
        if emitter_ids is not None:
            selected_ids = frozenset(emitter_ids)
            if any(type(stable_id) is not str or not stable_id for stable_id in selected_ids):
                raise NumpyParticleBackendError(
                    "selected particle emitter ids must be non-empty strings"
                )
            unknown = selected_ids.difference(kernel_ids)
            if unknown:
                raise NumpyParticleBackendError(
                    f"selected particle emitter ids are unknown: {sorted(unknown)}"
                )
        emitter_inputs = {emitter.stable_id: emitter for emitter in metadata.emitters}
        programs = []
        for emitter in kernel.emitters:
            if selected_ids is not None and emitter.stable_id not in selected_ids:
                continue
            runtime_metadata = emitter_inputs[emitter.stable_id]
            settings = runtime_metadata.settings
            outputs = runtime_metadata.outputs
            if settings.target is ExecutionTarget.GPU:
                raise NumpyParticleBackendError(
                    f"emitter {emitter.stable_id!r} explicitly requires the GPU backend"
                )
            flipbook_output = next(
                (
                    output
                    for output in outputs
                    if output.output_type == "sprite"
                    and (output.flipbook_columns != 1 or output.flipbook_rows != 1)
                ),
                None,
            )
            if flipbook_output is not None:
                raise NumpyParticleBackendError(
                    f"sprite output {flipbook_output.output_id!r} uses a flipbook grid; "
                    "Sprite flipbooks currently require the GPU backend"
                )
            referenced_point_caches = {
                instruction.immediate_dict()["interface"]
                for function in (emitter.init, emitter.update, emitter.rendering)
                for instruction in function.instructions
                if instruction.opcode == "sample_point_cache"
            }
            interface_by_id = {
                interface.stable_id: interface
                for interface in emitter.data_interfaces
                if isinstance(interface, PointCache)
            }
            resolver = point_cache_resolver or _default_point_cache_resolver
            point_caches = {
                stable_id: _NumpyPointCacheBinding(
                    interface_by_id[stable_id], resolver(interface_by_id[stable_id])
                )
                for stable_id in sorted(referenced_point_caches)
            }
            referenced_vector_fields = {
                instruction.immediate_dict()["interface"]
                for function in (emitter.init, emitter.update, emitter.rendering)
                for instruction in function.instructions
                if instruction.opcode == "sample_vector_field"
            }
            vector_field_by_id = {
                interface.stable_id: interface
                for interface in emitter.data_interfaces
                if isinstance(interface, VectorField)
            }
            field_resolver = vector_field_resolver or _default_vector_field_resolver
            vector_fields = {
                stable_id: _NumpyVectorFieldBinding(
                    vector_field_by_id[stable_id],
                    field_resolver(vector_field_by_id[stable_id]),
                )
                for stable_id in sorted(referenced_vector_fields)
            }
            referenced_sdfs = {
                instruction.immediate_dict()["interface"]
                for function in (emitter.init, emitter.update, emitter.rendering)
                for instruction in function.instructions
                if instruction.opcode in {"collide_sdf_position", "collide_sdf_velocity"}
            }
            sdf_by_id = {
                interface.stable_id: interface
                for interface in emitter.data_interfaces
                if isinstance(interface, SdfVolume)
            }
            resolve_sdf = sdf_resolver or _default_sdf_resolver
            sdf_volumes = {
                stable_id: _NumpySdfBinding(
                    sdf_by_id[stable_id], resolve_sdf(sdf_by_id[stable_id])
                )
                for stable_id in sorted(referenced_sdfs)
            }
            programs.append(
                NumpyParticleEmitterProgram(
                    emitter.stable_id,
                    settings,
                    emitter,
                    _compile_stage(emitter.init, emitter.random_seed, point_caches, vector_fields, sdf_volumes),
                    _compile_stage(emitter.update, emitter.random_seed, point_caches, vector_fields, sdf_volumes),
                    _compile_stage(emitter.rendering, emitter.random_seed, point_caches, vector_fields, sdf_volumes),
                    outputs,
                    tuple(point_caches.items()),
                    tuple(vector_fields.items()),
                    tuple(sdf_volumes.items()),
                )
            )
        return NumpyParticleProgram(kernel.kernel_hash, tuple(programs))


class NumpyParticleEmitterRuntime:
    """Dense SoA runtime with per-instance storage and explicit thread ownership."""

    def __init__(self, program: NumpyParticleEmitterProgram, *, system_seed: int = 0) -> None:
        self.program = program
        self.settings = program.settings
        self.capacity = self.settings.capacity
        self.attributes = {
            stable_id: _allocate_array(self.capacity, value_type)
            for stable_id, value_type, _default in program.kernel.attributes
        }
        self._attribute_defaults = {
            stable_id: _constant_value(default, value_type)
            for stable_id, value_type, default in program.kernel.attributes
        }
        self.alive = np.zeros(self.capacity, dtype=np.bool_)
        self.spawn_generation = np.zeros(self.capacity, dtype=np.uint32)
        self._ordinal = np.arange(self.capacity, dtype=np.uint32)
        self._finite_mask = np.empty(self.capacity, dtype=np.bool_)
        max_components = max(
            (_component_count(value_type) for _stable_id, value_type, _default in program.kernel.attributes),
            default=1,
        )
        self._component_mask = np.empty((self.capacity, max_components), dtype=np.bool_)
        self._instance_buffer = np.empty(
            (self.capacity, PARTICLE_INSTANCE_FLOATS), dtype=np.float32
        )
        self._workspaces = {
            "init": program.init.create_workspace(self.capacity),
            "update": program.update.create_workspace(self.capacity),
            "rendering": program.rendering.create_workspace(self.capacity),
        }
        self._render_aliases = dict(program.rendering.layout.export_aliases)
        self._context = _RuntimeContext(system_seed)
        self._active_count = 0
        self._spawn_accumulator = 0.0
        self._elapsed = 0.0
        self._burst_states: list[list[float | int]] = []
        self._next_particle_id = 0
        self._spawn_epoch = 0
        self._playing = True
        self._owner_thread: int | None = None
        self.reset()
        self._owner_thread = None

    @property
    def particle_count(self) -> int:
        return self._active_count

    @property
    def simulation_step(self) -> int:
        return self._context.simulation_step

    @property
    def is_playing(self) -> bool:
        return self._playing

    def play(self) -> None:
        self._claim_thread()
        self._playing = True

    def pause(self) -> None:
        self._claim_thread()
        self._playing = False

    def release_thread_ownership(self) -> None:
        if self._playing:
            raise NumpyParticleBackendError("pause the particle runtime before moving it to another thread")
        self._claim_thread()
        self._owner_thread = None

    def set_transforms(self, emitter_to_world=None, simulation_to_world=None) -> None:
        self._claim_thread()
        self._context.set_transforms(emitter_to_world, simulation_to_world)

    def reset(self) -> None:
        self._claim_thread()
        self.alive.fill(False)
        self._active_count = 0
        self._spawn_accumulator = 0.0
        self._elapsed = 0.0
        self._context.simulation_step = 0
        self._next_particle_id = 0
        self._spawn_epoch = 0
        self._burst_states = [
            [float(burst.time), int(burst.cycles), int(burst.count), float(burst.interval)]
            for burst in self.settings.bursts
        ]

    def migrate_to(
        self, program: NumpyParticleEmitterProgram
    ) -> tuple["NumpyParticleEmitterRuntime" | None, ParticleRuntimeCompatibility]:
        """Create a compatible runtime revision while preserving live state."""
        if not isinstance(program, NumpyParticleEmitterProgram):
            raise TypeError("particle runtime migration requires an emitter program")
        self._claim_thread()
        compatibility = classify_emitter_update(
            self.program.kernel,
            program.kernel,
            self.settings,
            program.settings,
        )
        if compatibility in {
            ParticleRuntimeCompatibility.EMITTER_RESTART,
            ParticleRuntimeCompatibility.SYSTEM_RESTART_REQUIRED,
        }:
            return None, compatibility

        migrated = NumpyParticleEmitterRuntime(
            program,
            system_seed=self._context.system_seed,
        )
        count = min(self._active_count, migrated.capacity)
        previous_schema = {
            stable_id: value_type
            for stable_id, value_type, _default in self.program.kernel.attributes
        }
        next_schema = {
            stable_id: value_type
            for stable_id, value_type, _default in program.kernel.attributes
        }
        for stable_id, target in migrated.attributes.items():
            if (
                stable_id in self.attributes
                and previous_schema.get(stable_id) == next_schema[stable_id]
            ):
                np.copyto(target[:count], self.attributes[stable_id][:count])
            elif count:
                np.copyto(
                    target[:count],
                    migrated._attribute_defaults[stable_id],
                    casting="unsafe",
                )
        migrated.alive[:count] = True
        np.copyto(
            migrated.spawn_generation[:count],
            self.spawn_generation[:count],
        )
        migrated._active_count = count
        migrated._spawn_accumulator = self._spawn_accumulator
        migrated._elapsed = self._elapsed
        migrated._context.delta_time = self._context.delta_time
        migrated._context.simulation_step = self._context.simulation_step
        migrated._context._conversions = {
            key: value.copy() for key, value in self._context._conversions.items()
        }
        migrated._next_particle_id = self._next_particle_id
        migrated._spawn_epoch = self._spawn_epoch
        migrated._burst_states = [list(state) for state in self._burst_states]
        migrated._playing = self._playing
        migrated._owner_thread = self._owner_thread
        return migrated, compatibility

    def tick(self, delta_time: float) -> np.ndarray:
        self._claim_thread()
        delta_time = float(delta_time)
        if not math.isfinite(delta_time) or delta_time < 0.0:
            raise ValueError("particle delta_time must be finite and non-negative")
        self._refresh_data_interfaces()
        if not self._playing:
            return self.instance_buffer()

        self._context.delta_time = delta_time
        previous_elapsed = self._elapsed
        self._elapsed += delta_time
        spawn_count = self._scheduled_spawn_count(previous_elapsed, self._elapsed, delta_time)
        spawn_count = min(spawn_count, self.capacity - self._active_count)
        if spawn_count > 0:
            start = self._active_count
            self._initialize_range(start, spawn_count)
            self._active_count += spawn_count
            self._compact_dead()

        if self._active_count:
            workspace = self._workspaces["update"]
            self.program.update.function(
                self,
                workspace,
                self._context,
                0,
                self._active_count,
            )
            self._kill_non_finite(self.program.update, 0, self._active_count)
            self._compact_dead()

        instances = self.instance_buffer()
        self._context.simulation_step = (self._context.simulation_step + 1) & 0xFFFFFFFF
        return instances

    def instance_buffer(self) -> np.ndarray:
        self._refresh_data_interfaces()
        count = self._active_count
        if count == 0:
            return self._instance_buffer[:0]
        workspace = self._workspaces["rendering"]
        self.program.rendering.function(self, workspace, self._context, 0, count)
        exports = workspace.exports
        position_id = self._render_aliases.get("builtin.position")
        size_id = self._render_aliases.get("builtin.size")
        scale_id = self._render_aliases.get("builtin.scale")
        color_id = self._render_aliases.get("builtin.color")
        rotation_id = self._render_aliases.get("builtin.rotation")
        position = (
            self.attributes[position_id]
            if position_id is not None
            else exports.get("builtin.position", self.attributes["builtin.position"])
        )
        size = (
            self.attributes[size_id]
            if size_id is not None
            else exports.get("builtin.size", self.attributes["builtin.size"])
        )
        scale = (
            self.attributes[scale_id]
            if scale_id is not None
            else exports.get("builtin.scale")
        )
        color = (
            self.attributes[color_id]
            if color_id is not None
            else exports.get("builtin.color", self.attributes["builtin.color"])
        )
        rotation = (
            self.attributes[rotation_id]
            if rotation_id is not None
            else exports.get("builtin.rotation", self.attributes["builtin.rotation"])
        )
        output = self._instance_buffer[:count]
        np.copyto(output[:, 0:3], position[:count], casting="unsafe")
        np.copyto(output[:, 3], size[:count], casting="unsafe")
        np.copyto(output[:, 4:8], color[:count], casting="unsafe")
        np.copyto(output[:, 8], rotation[:count], casting="unsafe")
        if scale is None:
            output[:, 9:12] = 1.0
        else:
            np.copyto(output[:, 9:12], scale[:count], casting="unsafe")
        return output

    def _refresh_data_interfaces(self) -> None:
        for _stable_id, binding in self.program.point_caches:
            binding.refresh()
        for _stable_id, binding in self.program.vector_fields:
            binding.refresh()

    def _scheduled_spawn_count(self, previous: float, current: float, delta_time: float) -> int:
        self._spawn_accumulator += self.settings.spawn_rate * delta_time
        spawn_count = int(self._spawn_accumulator)
        self._spawn_accumulator -= spawn_count
        for state in self._burst_states:
            next_time, remaining, count, interval = state
            while remaining > 0 and next_time <= current:
                if next_time > previous or (previous == 0.0 and next_time == 0.0):
                    spawn_count += int(count)
                remaining -= 1
                next_time += float(interval)
                if interval == 0.0 and remaining > 0:
                    spawn_count += int(count) * int(remaining)
                    remaining = 0
            state[0] = next_time
            state[1] = remaining
        return spawn_count

    def _initialize_range(self, start: int, count: int) -> None:
        particle_slice = slice(start, start + count)
        init_writes = set(self.program.kernel.init.written_attributes)
        for stable_id, target in self.attributes.items():
            if stable_id not in init_writes and stable_id != "builtin.id":
                np.copyto(
                    target[particle_slice],
                    self._attribute_defaults[stable_id],
                    casting="unsafe",
                )
        self.alive[particle_slice] = True
        self._assign_particle_ids(start, count)
        workspace = self._workspaces["init"]
        self.program.init.function(self, workspace, self._context, start, count)
        self._kill_non_finite(self.program.init, start, count)

    def _assign_particle_ids(self, start: int, count: int) -> None:
        target = self.attributes.get("builtin.id")
        if target is None:
            return
        first_count = min(count, 0x100000000 - self._next_particle_id)
        first = slice(start, start + first_count)
        np.add(
            self._ordinal[:first_count],
            np.uint32(self._next_particle_id),
            out=target[first],
        )
        self.spawn_generation[first] = np.uint32(self._spawn_epoch)
        remaining = count - first_count
        if remaining:
            self._spawn_epoch = (self._spawn_epoch + 1) & 0xFFFFFFFF
            second = slice(start + first_count, start + count)
            np.copyto(target[second], self._ordinal[:remaining])
            self.spawn_generation[second] = np.uint32(self._spawn_epoch)
            self._next_particle_id = remaining
        else:
            self._next_particle_id = (self._next_particle_id + count) & 0xFFFFFFFF
            if self._next_particle_id == 0 and count:
                self._spawn_epoch = (self._spawn_epoch + 1) & 0xFFFFFFFF

    def _kill_non_finite(
        self,
        executable: NumpyStageExecutable,
        start: int,
        count: int,
    ) -> None:
        particle_slice = slice(start, start + count)
        alive = self.alive[particle_slice]
        written_attributes = executable_stage_writes(executable, self.program.kernel)
        for stable_id in self.program.kernel.attributes:
            attribute_id = stable_id[0]
            if attribute_id not in written_attributes:
                continue
            values = self.attributes[attribute_id][particle_slice]
            if values.dtype.kind != "f":
                continue
            if values.ndim == 1:
                np.isfinite(values, out=self._finite_mask[:count])
            else:
                components = values.shape[1]
                np.isfinite(values, out=self._component_mask[:count, :components])
                np.all(
                    self._component_mask[:count, :components],
                    axis=1,
                    out=self._finite_mask[:count],
                )
            np.logical_and(alive, self._finite_mask[:count], out=alive)

    def _compact_dead(self) -> None:
        count = self._active_count
        mask = self.alive[:count]
        survivors = int(np.count_nonzero(mask))
        if survivors == count:
            return
        for values in self.attributes.values():
            np.compress(mask, values[:count], axis=0, out=values[:survivors])
        np.compress(
            mask,
            self.spawn_generation[:count],
            axis=0,
            out=self.spawn_generation[:survivors],
        )
        self.alive[:survivors] = True
        self.alive[survivors:count] = False
        self._active_count = survivors

    def _claim_thread(self) -> None:
        current = threading.get_ident()
        if self._owner_thread is None:
            self._owner_thread = current
        elif self._owner_thread != current:
            raise NumpyParticleBackendError(
                "NumPy particle runtime is owned by another thread"
            )


def _compile_stage(
    function: ParticleKernelFunction,
    emitter_seed: int,
    point_caches: Mapping[str, _NumpyPointCacheBinding],
    vector_fields: Mapping[str, _NumpyVectorFieldBinding],
    sdf_volumes: Mapping[str, _NumpySdfBinding],
) -> NumpyStageExecutable:
    constants: list[Any] = []
    attributes: list[str] = []
    shape_parameters: list[dict[str, Any]] = []
    conversions: list[dict[str, Any]] = []
    point_cache_samples: list[
        tuple[_NumpyPointCacheBinding, str, TypeRef, str, str]
    ] = []
    vector_field_samples: list[_NumpyVectorFieldBinding] = []
    sdf_samples: list[tuple[_NumpySdfBinding, bool]] = []
    curves: list[tuple[Any, ...]] = []
    gradients: list[tuple[Any, ...]] = []
    scratch_types: list[TypeRef] = []
    export_types: list[tuple[str, TypeRef]] = []
    export_aliases: list[tuple[str, str]] = []
    values: dict[str, str] = {}
    value_attributes: dict[str, str] = {}
    lines = [
        "def _kernel(state, workspace, context, start, count):",
        "    if count <= 0:",
        "        return",
        "    particle_slice = slice(start, start + count)",
        "    scratch = workspace.scratch",
        "    exports = workspace.exports",
    ]

    def result_buffer(instruction) -> str:
        scratch_index = len(scratch_types)
        scratch_types.append(instruction.result_type)
        name = f"v{len(values)}"
        lines.append(f"    {name} = scratch[{scratch_index}][:count]")
        values[instruction.result_id] = name
        return name

    def operand_names(instruction) -> list[str]:
        try:
            return [values[operand.value_id] for operand in instruction.operands]
        except KeyError as exc:
            raise KernelCompileError("NumPy AOT compiler received invalid SSA ordering") from exc

    def broadcast_operand(instruction, index: int, name: str) -> str:
        if (
            _component_count(instruction.result_type) > 1
            and _component_count(instruction.operands[index].value_type) == 1
        ):
            return f"_broadcast_particle_scalar({name})"
        return name

    for instruction in function.instructions:
        opcode = instruction.opcode
        immediates = instruction.immediate_dict()
        if opcode == "constant":
            name = f"v{len(values)}"
            constants.append(_constant_value(immediates["value"], instruction.result_type))
            lines.append(f"    {name} = _constants[{len(constants) - 1}]")
            values[instruction.result_id] = name
        elif opcode == "load_attribute":
            name = f"v{len(values)}"
            attributes.append(immediates["attribute"])
            lines.append(
                f"    {name} = state.attributes[_attributes[{len(attributes) - 1}]][particle_slice]"
            )
            values[instruction.result_id] = name
            value_attributes[instruction.result_id] = immediates["attribute"]
        elif opcode == "load_uniform":
            name = f"v{len(values)}"
            lines.append(f"    {name} = context.{immediates['name']}")
            values[instruction.result_id] = name
        elif opcode in {
            "add",
            "subtract",
            "multiply",
            "divide",
            "less_than",
            "less_equal",
            "greater_than",
            "greater_equal",
        }:
            operands = operand_names(instruction)
            output = result_buffer(instruction)
            ufunc = {
                "add": "add",
                "subtract": "subtract",
                "multiply": "multiply",
                "divide": "divide",
                "less_than": "less",
                "less_equal": "less_equal",
                "greater_than": "greater",
                "greater_equal": "greater_equal",
            }[opcode]
            left = broadcast_operand(instruction, 0, operands[0])
            right = broadcast_operand(instruction, 1, operands[1])
            lines.append(f"    np.{ufunc}({left}, {right}, out={output})")
        elif opcode == "lerp":
            operands = operand_names(instruction)
            output = result_buffer(instruction)
            first = broadcast_operand(instruction, 0, operands[0])
            second = broadcast_operand(instruction, 1, operands[1])
            factor = broadcast_operand(instruction, 2, operands[2])
            lines.append(
                f"    np.add({first}, ({second} - {first}) * {factor}, out={output})"
            )
        elif opcode == "normalized_age":
            age, lifetime = operand_names(instruction)
            output = result_buffer(instruction)
            lines.extend(
                (
                    f"    np.maximum({lifetime}, np.float32(0.000001), out={output})",
                    f"    np.divide({age}, {output}, out={output})",
                    f"    np.clip({output}, np.float32(0.0), np.float32(1.0), out={output})",
                )
            )
        elif opcode == "logical_not":
            source = operand_names(instruction)[0]
            output = result_buffer(instruction)
            lines.append(f"    np.logical_not({source}, out={output})")
        elif opcode == "normalize":
            source = operand_names(instruction)[0]
            output = result_buffer(instruction)
            lines.append(f"    _normalize({output}, {source}, workspace.random_float[5][:count])")
        elif opcode == "random_f32":
            low, high, node_seed = operand_names(instruction)
            output = result_buffer(instruction)
            lines.append(
                f"    _random_range({output}, {low}, {high}, {node_seed}, "
                f"{emitter_seed}, {immediates['random_slot']}, state, particle_slice, context, workspace)"
            )
        elif opcode == "sample_curve":
            source = operand_names(instruction)[0]
            output = result_buffer(instruction)
            curves.append(_prepare_curve(immediates["curve"]))
            lines.append(
                f"    _sample_curve({output}, {source}, _curves[{len(curves) - 1}])"
            )
        elif opcode == "sample_gradient":
            source = operand_names(instruction)[0]
            output = result_buffer(instruction)
            gradients.append(_prepare_gradient(immediates["gradient"]))
            lines.append(
                f"    _sample_gradient({output}, {source}, _gradients[{len(gradients) - 1}])"
            )
        elif opcode in {"value_noise_3d", "vector_noise_3d"}:
            position, frequency, seed = operand_names(instruction)
            output = result_buffer(instruction)
            helper = "_value_noise_3d" if opcode == "value_noise_3d" else "_vector_noise_3d"
            lines.append(
                f"    {helper}({output}, {position}, {frequency}, {seed}, workspace, count)"
            )
        elif opcode.startswith("sample_shape_"):
            output = result_buffer(instruction)
            shape_parameters.append(immediates)
            mode = "position" if opcode.endswith("position") else "direction"
            lines.append(
                f"    _sample_shape({output}, _shape_parameters[{len(shape_parameters) - 1}], "
                f"'{mode}', {emitter_seed}, state, particle_slice, context, workspace)"
            )
        elif opcode == "sample_point_cache":
            index_or_id = operand_names(instruction)[0]
            output = result_buffer(instruction)
            try:
                binding = point_caches[immediates["interface"]]
            except KeyError as exc:
                raise NumpyParticleBackendError(
                    f"NumPy backend has no point cache binding {immediates['interface']!r}"
                ) from exc
            binding.require_channel(immediates["channel"], instruction.result_type)
            point_cache_samples.append(
                (
                    binding,
                    immediates["channel"],
                    instruction.result_type,
                    immediates["lookup"],
                    immediates["semantic"],
                )
            )
            lines.append(
                f"    _sample_point_cache({output}, {index_or_id}, "
                f"_point_cache_samples[{len(point_cache_samples) - 1}], context, workspace)"
            )
        elif opcode == "sample_vector_field":
            position = operand_names(instruction)[0]
            output = result_buffer(instruction)
            try:
                binding = vector_fields[immediates["interface"]]
            except KeyError as exc:
                raise NumpyParticleBackendError(
                    f"NumPy backend has no vector field binding {immediates['interface']!r}"
                ) from exc
            vector_field_samples.append(binding)
            lines.append(
                f"    _sample_vector_field({output}, {position}, "
                f"_vector_field_samples[{len(vector_field_samples) - 1}], context, workspace)"
            )
        elif opcode == "convert_space":
            source = operand_names(instruction)[0]
            output = result_buffer(instruction)
            conversions.append(immediates)
            lines.append(
                f"    _convert_space({output}, {source}, "
                f"_conversions[{len(conversions) - 1}], context)"
            )
        elif opcode == "store_attribute":
            source = operand_names(instruction)[0]
            attributes.append(immediates["attribute"])
            lines.append(
                f"    np.copyto(state.attributes[_attributes[{len(attributes) - 1}]][particle_slice], "
                f"{source}, casting='unsafe')"
            )
        elif opcode == "kill_if":
            source = operand_names(instruction)[0]
            lines.append(
                f"    np.logical_and(state.alive[particle_slice], np.logical_not({source}), "
                f"out=state.alive[particle_slice])"
            )
        elif opcode in {
            "collide_plane_position",
            "collide_plane_velocity",
            "collide_sphere_position",
            "collide_sphere_velocity",
        }:
            operands = operand_names(instruction)
            output = result_buffer(instruction)
            helper = f"_{opcode}"
            lines.append(
                f"    {helper}({output}, {', '.join(operands)}, workspace, count)"
            )
        elif opcode in {"collide_sdf_position", "collide_sdf_velocity"}:
            operands = operand_names(instruction)
            output = result_buffer(instruction)
            try:
                binding = sdf_volumes[immediates["interface"]]
            except KeyError as exc:
                raise NumpyParticleBackendError(
                    f"NumPy backend has no SDF binding {immediates['interface']!r}"
                ) from exc
            sdf_samples.append((binding, immediates["inverted"]))
            lines.append(
                f"    _{opcode}({output}, {', '.join(operands)}, "
                f"_sdf_samples[{len(sdf_samples) - 1}], context, workspace, count)"
            )
        elif opcode == "export_attribute":
            source = operand_names(instruction)[0]
            stable_id = immediates["attribute"]
            source_attribute = value_attributes.get(instruction.operands[0].value_id)
            if source_attribute is not None:
                export_aliases.append((stable_id, source_attribute))
            else:
                export_types.append((stable_id, instruction.operands[0].value_type))
                lines.append(
                    f"    np.copyto(exports[{stable_id!r}][:count], {source}, casting='unsafe')"
                )
        else:
            raise NumpyParticleBackendError(f"NumPy backend does not implement {opcode!r}")

    source = "\n".join(lines) + "\n"
    namespace = {
        "np": np,
        "_attributes": tuple(attributes),
        "_constants": tuple(constants),
        "_shape_parameters": tuple(shape_parameters),
        "_conversions": tuple(conversions),
        "_point_cache_samples": tuple(point_cache_samples),
        "_vector_field_samples": tuple(vector_field_samples),
        "_sdf_samples": tuple(sdf_samples),
        "_curves": tuple(curves),
        "_gradients": tuple(gradients),
        "_convert_space": _convert_space,
        "_normalize": _normalize,
        "_collide_plane_position": _collide_plane_position,
        "_collide_plane_velocity": _collide_plane_velocity,
        "_collide_sphere_position": _collide_sphere_position,
        "_collide_sphere_velocity": _collide_sphere_velocity,
        "_collide_sdf_position": _collide_sdf_position,
        "_collide_sdf_velocity": _collide_sdf_velocity,
        "_random_range": _random_range,
        "_sample_shape": _sample_shape,
        "_sample_point_cache": _sample_point_cache,
        "_sample_vector_field": _sample_vector_field,
        "_sample_curve": _sample_curve,
        "_sample_gradient": _sample_gradient,
        "_value_noise_3d": _value_noise_3d,
        "_vector_noise_3d": _vector_noise_3d,
        "_broadcast_particle_scalar": _broadcast_particle_scalar,
    }
    exec(compile(source, f"<particle-numpy-{function.stage.value}>", "exec"), namespace)
    return NumpyStageExecutable(
        function.stage.value,
        source,
        _StageLayout(
            tuple(scratch_types),
            tuple(export_types),
            tuple(export_aliases),
        ),
        namespace["_kernel"],
    )


def _broadcast_particle_scalar(value):
    if isinstance(value, np.ndarray) and value.ndim == 1:
        return value[:, None]
    return value


def _prepare_curve(value):
    curve = Curve.from_dict(value)
    return (
        np.asarray([key.time for key in curve.keys], dtype=np.float32),
        np.asarray([key.value for key in curve.keys], dtype=np.float32),
        np.asarray([key.in_tangent for key in curve.keys], dtype=np.float32),
        np.asarray([key.out_tangent for key in curve.keys], dtype=np.float32),
        curve.pre_wrap,
        curve.post_wrap,
    )


def _prepare_gradient(value):
    gradient = Gradient.from_dict(value)
    return (
        np.asarray([key.time for key in gradient.keys], dtype=np.float32),
        np.asarray([key.color for key in gradient.keys], dtype=np.float32),
        gradient.mode,
    )


def _wrapped_ramp_time(source, first, last, pre_wrap, post_wrap):
    values = np.asarray(source, dtype=np.float32)
    result = values.copy()
    span = np.float32(last - first)
    if span <= 0.0:
        result.fill(first)
        return result

    def apply(mask, mode, clamp_value):
        if mode == "clamp":
            np.copyto(result, np.float32(clamp_value), where=mask)
            return
        offset = np.mod(values - np.float32(first), span)
        if mode == "repeat":
            np.copyto(result, np.float32(first) + offset, where=mask)
            return
        period = span * np.float32(2.0)
        folded = np.mod(values - np.float32(first), period)
        folded = np.where(folded <= span, folded, period - folded)
        np.copyto(result, np.float32(first) + folded, where=mask)

    before = values < np.float32(first)
    apply(before, pre_wrap, first)
    after = values > np.float32(last)
    apply(after, post_wrap, last)
    return result


def _sample_curve(output, source, prepared) -> None:
    times, values, in_tangents, out_tangents, pre_wrap, post_wrap = prepared
    if len(times) == 1:
        output.fill(values[0])
        return
    source_values = np.broadcast_to(np.asarray(source, dtype=np.float32), output.shape)
    t = _wrapped_ramp_time(source_values, times[0], times[-1], pre_wrap, post_wrap)
    indices = np.searchsorted(times, t, side="right") - 1
    np.clip(indices, 0, len(times) - 2, out=indices)
    next_indices = indices + 1
    t0 = times[indices]
    dt = times[next_indices] - t0
    u = (t - t0) / dt
    u2 = u * u
    u3 = u2 * u
    np.copyto(
        output,
        (2.0 * u3 - 3.0 * u2 + 1.0) * values[indices]
        + (u3 - 2.0 * u2 + u) * out_tangents[indices] * dt
        + (-2.0 * u3 + 3.0 * u2) * values[next_indices]
        + (u3 - u2) * in_tangents[next_indices] * dt,
        casting="unsafe",
    )


def _sample_gradient(output, source, prepared) -> None:
    times, colors, mode = prepared
    source_values = np.broadcast_to(
        np.asarray(source, dtype=np.float32), (output.shape[0],)
    )
    t = np.clip(source_values, times[0], times[-1])
    if len(times) == 1:
        np.copyto(output, colors[0])
        return
    indices = np.searchsorted(times, t, side="right") - 1
    np.clip(indices, 0, len(times) - 2, out=indices)
    if mode == "fixed":
        np.copyto(output, colors[indices])
        np.copyto(output, colors[-1], where=(t >= times[-1])[:, None])
        return
    next_indices = indices + 1
    factor = (t - times[indices]) / (times[next_indices] - times[indices])
    np.copyto(
        output,
        colors[indices] + (colors[next_indices] - colors[indices]) * factor[:, None],
        casting="unsafe",
    )


def _noise_hash(output, temporary, float_output, coordinates, seed, seed_xor) -> None:
    np.copyto(output, coordinates[:, 0], casting="unsafe")
    np.multiply(output, np.uint32(0x8DA6B343), out=output)
    np.copyto(temporary, coordinates[:, 1], casting="unsafe")
    np.multiply(temporary, np.uint32(0xD8163841), out=temporary)
    np.bitwise_xor(output, temporary, out=output)
    np.copyto(temporary, coordinates[:, 2], casting="unsafe")
    np.multiply(temporary, np.uint32(0xCB1AB31F), out=temporary)
    np.bitwise_xor(output, temporary, out=output)
    np.bitwise_xor(output, seed, out=output)
    if seed_xor:
        np.bitwise_xor(output, np.uint32(seed_xor), out=output)
    np.right_shift(output, np.uint32(16), out=temporary)
    np.bitwise_xor(output, temporary, out=output)
    np.multiply(output, np.uint32(0x7FEB352D), out=output)
    np.right_shift(output, np.uint32(15), out=temporary)
    np.bitwise_xor(output, temporary, out=output)
    np.multiply(output, np.uint32(0x846CA68B), out=output)
    np.right_shift(output, np.uint32(16), out=temporary)
    np.bitwise_xor(output, temporary, out=output)
    np.right_shift(output, np.uint32(8), out=temporary)
    np.copyto(float_output, temporary, casting="unsafe")
    np.multiply(float_output, np.float32(1.0 / 16777216.0), out=float_output)


def _value_noise_3d(output, position, frequency, seed, workspace, count, seed_xor=0) -> None:
    fraction = workspace.field_fraction[:count]
    floor_values = workspace.field_coordinates[:count]
    base = workspace.field_base[:count]
    corner = workspace.field_corner[:count]
    smooth = workspace.sample_vector[:count]
    values = tuple(value[:count] for value in workspace.random_float)
    hash_value = workspace.random_u32_a[:count]
    hash_temporary = workspace.random_u32_b[:count]

    frequency_values = np.asarray(frequency)
    if frequency_values.ndim:
        frequency_values = frequency_values[:, None]
    np.multiply(position, frequency_values, out=fraction)
    np.floor(fraction, out=floor_values)
    np.copyto(base, floor_values, casting="unsafe")
    np.subtract(fraction, floor_values, out=fraction)
    np.multiply(fraction, fraction, out=smooth)
    np.multiply(fraction, np.float32(-2.0), out=floor_values)
    np.add(floor_values, np.float32(3.0), out=floor_values)
    np.multiply(smooth, floor_values, out=smooth)

    for index, (y_offset, z_offset) in enumerate(((0, 0), (1, 0), (0, 1), (1, 1))):
        np.copyto(corner, base)
        if y_offset:
            np.add(corner[:, 1], 1, out=corner[:, 1])
        if z_offset:
            np.add(corner[:, 2], 1, out=corner[:, 2])
        _noise_hash(hash_value, hash_temporary, values[4], corner, seed, seed_xor)
        np.add(corner[:, 0], 1, out=corner[:, 0])
        _noise_hash(hash_value, hash_temporary, values[5], corner, seed, seed_xor)
        np.subtract(values[5], values[4], out=values[5])
        np.multiply(values[5], smooth[:, 0], out=values[5])
        np.add(values[4], values[5], out=values[index])

    np.subtract(values[1], values[0], out=values[4])
    np.multiply(values[4], smooth[:, 1], out=values[4])
    np.add(values[0], values[4], out=values[4])
    np.subtract(values[3], values[2], out=values[5])
    np.multiply(values[5], smooth[:, 1], out=values[5])
    np.add(values[2], values[5], out=values[5])
    np.subtract(values[5], values[4], out=output)
    np.multiply(output, smooth[:, 2], out=output)
    np.add(output, values[4], out=output)


def _vector_noise_3d(output, position, frequency, seed, workspace, count) -> None:
    for component, seed_xor in enumerate((0x00000000, 0x9E3779B9, 0x85EBCA6B)):
        _value_noise_3d(
            output[:, component],
            position,
            frequency,
            seed,
            workspace,
            count,
            seed_xor,
        )
    np.multiply(output, np.float32(2.0), out=output)
    np.subtract(output, np.float32(1.0), out=output)


def _random_range(
    output,
    low,
    high,
    node_seed,
    emitter_seed,
    random_slot,
    state,
    particle_slice,
    context,
    workspace,
) -> None:
    count = output.shape[0]
    _random01(
        output,
        state,
        particle_slice,
        context,
        emitter_seed,
        node_seed,
        random_slot,
        workspace,
    )
    temporary = workspace.random_float[4][:count]
    np.subtract(high, low, out=temporary)
    np.multiply(output, temporary, out=output)
    np.add(output, low, out=output)


def _random01(
    output,
    state,
    particle_slice,
    context,
    emitter_seed,
    node_seed,
    random_slot,
    workspace,
) -> None:
    count = output.shape[0]
    value = workspace.random_u32_a[:count]
    temporary = workspace.random_u32_b[:count]
    value.fill(np.uint32(0x811C9DC5))
    keys = (
        context.system_seed,
        emitter_seed,
        node_seed,
        state.attributes["builtin.id"][particle_slice],
        state.spawn_generation[particle_slice],
        context.simulation_step,
        random_slot,
    )
    for key in keys:
        np.bitwise_xor(value, key, out=value)
        np.multiply(value, np.uint32(0x01000193), out=value)
        np.right_shift(value, np.uint32(16), out=temporary)
        np.bitwise_xor(value, temporary, out=value)
    np.right_shift(value, np.uint32(16), out=temporary)
    np.bitwise_xor(value, temporary, out=value)
    np.multiply(value, np.uint32(0x7FEB352D), out=value)
    np.right_shift(value, np.uint32(15), out=temporary)
    np.bitwise_xor(value, temporary, out=value)
    np.multiply(value, np.uint32(0x846CA68B), out=value)
    np.right_shift(value, np.uint32(16), out=temporary)
    np.bitwise_xor(value, temporary, out=value)
    np.right_shift(value, np.uint32(8), out=temporary)
    np.copyto(output, temporary, casting="unsafe")
    np.multiply(output, np.float32(1.0 / 16777216.0), out=output)


def _sample_shape(
    output,
    parameters,
    mode,
    emitter_seed,
    state,
    particle_slice,
    context,
    workspace,
) -> None:
    count = output.shape[0]
    random_values = workspace.random_float
    slots = parameters["random_slots"]
    for index in range(3):
        _random01(
            random_values[index][:count],
            state,
            particle_slice,
            context,
            emitter_seed,
            0,
            slots[index],
            workspace,
        )
    u, v, w = (random_values[index][:count] for index in range(3))
    shape = parameters["shape"]
    radius = np.float32(parameters["radius"])
    if shape == "point":
        output.fill(0.0)
        if mode == "direction":
            output[:, 2].fill(1.0)
    elif shape == "box" and mode == "position":
        dimensions = np.asarray(parameters["dimensions"], dtype=np.float32)
        np.subtract(u, np.float32(0.5), out=output[:, 0])
        np.subtract(v, np.float32(0.5), out=output[:, 1])
        np.subtract(w, np.float32(0.5), out=output[:, 2])
        np.multiply(output, dimensions, out=output)
    elif shape == "cone" and mode == "position":
        np.sqrt(u, out=random_values[3][:count])
        np.multiply(random_values[3][:count], radius, out=random_values[3][:count])
        np.multiply(v, np.float32(2.0 * math.pi), out=random_values[4][:count])
        np.cos(random_values[4][:count], out=output[:, 0])
        np.sin(random_values[4][:count], out=output[:, 1])
        np.multiply(output[:, 0], random_values[3][:count], out=output[:, 0])
        np.multiply(output[:, 1], random_values[3][:count], out=output[:, 1])
        output[:, 2].fill(0.0)
    else:
        if shape == "cone":
            cosine_limit = np.float32(math.cos(math.radians(parameters["angle_degrees"])))
            np.multiply(u, np.float32(1.0 - cosine_limit), out=output[:, 2])
            np.add(output[:, 2], cosine_limit, out=output[:, 2])
        else:
            np.multiply(u, np.float32(2.0), out=output[:, 2])
            np.subtract(output[:, 2], np.float32(1.0), out=output[:, 2])
        np.multiply(v, np.float32(2.0 * math.pi), out=random_values[3][:count])
        np.multiply(output[:, 2], output[:, 2], out=random_values[4][:count])
        np.subtract(np.float32(1.0), random_values[4][:count], out=random_values[4][:count])
        np.maximum(random_values[4][:count], np.float32(0.0), out=random_values[4][:count])
        np.sqrt(random_values[4][:count], out=random_values[4][:count])
        np.cos(random_values[3][:count], out=output[:, 0])
        np.sin(random_values[3][:count], out=output[:, 1])
        np.multiply(output[:, 0], random_values[4][:count], out=output[:, 0])
        np.multiply(output[:, 1], random_values[4][:count], out=output[:, 1])
        if mode == "position":
            np.cbrt(w, out=random_values[5][:count])
            np.multiply(random_values[5][:count], radius, out=random_values[5][:count])
            np.multiply(output, random_values[5][:count, None], out=output)


def _normalize(output, source, length_scratch) -> None:
    output.fill(0.0)
    np.multiply(source, source, out=output)
    np.sum(output, axis=1, out=length_scratch)
    np.sqrt(length_scratch, out=length_scratch)
    np.divide(
        source,
        length_scratch[:, None],
        out=output,
        where=length_scratch[:, None] > 0.0,
    )


def _collision_normal(normal, workspace, count):
    normalized = workspace.sample_vector[:count]
    source = np.broadcast_to(normal, (count, 3))
    _normalize(normalized, source, workspace.random_float[0][:count])
    return normalized


def _collide_plane_position(
    output,
    position,
    velocity,
    point,
    normal,
    radius,
    restitution,
    friction,
    workspace,
    count,
) -> None:
    del velocity, restitution, friction
    normalized = _collision_normal(normal, workspace, count)
    delta = workspace.field_fraction[:count]
    np.subtract(position, point, out=delta)
    np.multiply(delta, normalized, out=delta)
    distance = workspace.random_float[1][:count]
    np.sum(delta, axis=1, out=distance)
    clamped_radius = workspace.random_float[2][:count]
    np.maximum(radius, 0.0, out=clamped_radius)
    np.subtract(distance, clamped_radius, out=distance)
    penetrating = workspace.sample_valid[:count]
    np.less(distance, 0.0, out=penetrating)
    correction = workspace.field_coordinates[:count]
    np.negative(distance, out=distance)
    np.multiply(normalized, distance[:, None], out=correction)
    np.copyto(output, position, casting="unsafe")
    np.add(output, correction, out=output, where=penetrating[:, None])


def _collide_plane_velocity(
    output,
    position,
    velocity,
    point,
    normal,
    radius,
    restitution,
    friction,
    workspace,
    count,
) -> None:
    normalized = _collision_normal(normal, workspace, count)
    temporary = workspace.field_fraction[:count]
    np.subtract(position, point, out=temporary)
    np.multiply(temporary, normalized, out=temporary)
    distance = workspace.random_float[1][:count]
    np.sum(temporary, axis=1, out=distance)
    clamped_radius = workspace.random_float[2][:count]
    np.maximum(radius, 0.0, out=clamped_radius)
    penetrating = workspace.sample_valid[:count]
    np.less(distance, clamped_radius, out=penetrating)

    normal_component = workspace.field_coordinates[:count]
    np.multiply(velocity, normalized, out=normal_component)
    normal_speed = workspace.random_float[3][:count]
    np.sum(normal_component, axis=1, out=normal_speed)
    moving_into_plane = workspace.field_valid[:count]
    np.less(normal_speed, 0.0, out=moving_into_plane)
    np.logical_and(penetrating, moving_into_plane, out=penetrating)

    np.multiply(normalized, normal_speed[:, None], out=temporary)
    np.subtract(velocity, temporary, out=temporary)
    clamped_friction = workspace.random_float[4][:count]
    np.clip(friction, 0.0, 1.0, out=clamped_friction)
    np.subtract(1.0, clamped_friction, out=clamped_friction)
    np.multiply(temporary, clamped_friction[:, None], out=temporary)

    clamped_restitution = workspace.random_float[5][:count]
    np.clip(restitution, 0.0, 1.0, out=clamped_restitution)
    np.negative(normal_speed, out=normal_speed)
    np.multiply(clamped_restitution, normal_speed, out=clamped_restitution)
    np.multiply(normalized, clamped_restitution[:, None], out=normal_component)
    np.add(temporary, normal_component, out=temporary)
    np.copyto(output, velocity, casting="unsafe")
    np.copyto(output, temporary, where=penetrating[:, None])


def _sphere_collision_normal(position, velocity, center, workspace, count):
    normal = workspace.sample_vector[:count]
    delta = workspace.field_fraction[:count]
    np.subtract(position, center, out=delta)
    distance = workspace.random_float[0][:count]
    _normalize(normal, delta, distance)

    degenerate = workspace.sample_valid[:count]
    np.less_equal(distance, np.float32(1.0e-6), out=degenerate)
    fallback = workspace.field_coordinates[:count]
    fallback_length = workspace.random_float[1][:count]
    _normalize(fallback, velocity, fallback_length)
    np.negative(fallback, out=fallback)
    stationary = workspace.field_valid[:count]
    np.less_equal(fallback_length, np.float32(1.0e-6), out=stationary)
    np.copyto(fallback, (0.0, 1.0, 0.0), where=stationary[:, None])
    np.copyto(normal, fallback, where=degenerate[:, None])
    return normal, distance


def _collide_sphere_position(
    output,
    position,
    velocity,
    center,
    sphere_radius,
    particle_radius,
    restitution,
    friction,
    workspace,
    count,
) -> None:
    del restitution, friction
    normal, distance = _sphere_collision_normal(
        position, velocity, center, workspace, count
    )
    combined_radius = workspace.random_float[2][:count]
    np.maximum(sphere_radius, 0.0, out=combined_radius)
    np.maximum(particle_radius, 0.0, out=workspace.random_float[3][:count])
    np.add(combined_radius, workspace.random_float[3][:count], out=combined_radius)
    penetrating = workspace.sample_valid[:count]
    np.less(distance, combined_radius, out=penetrating)
    correction = workspace.field_fraction[:count]
    np.subtract(combined_radius, distance, out=distance)
    np.multiply(normal, distance[:, None], out=correction)
    np.copyto(output, position, casting="unsafe")
    np.add(output, correction, out=output, where=penetrating[:, None])


def _collide_sphere_velocity(
    output,
    position,
    velocity,
    center,
    sphere_radius,
    particle_radius,
    restitution,
    friction,
    workspace,
    count,
) -> None:
    normal, distance = _sphere_collision_normal(
        position, velocity, center, workspace, count
    )
    combined_radius = workspace.random_float[2][:count]
    np.maximum(sphere_radius, 0.0, out=combined_radius)
    np.maximum(particle_radius, 0.0, out=workspace.random_float[3][:count])
    np.add(combined_radius, workspace.random_float[3][:count], out=combined_radius)
    collision = workspace.sample_valid[:count]
    np.less(distance, combined_radius, out=collision)

    normal_component = workspace.field_fraction[:count]
    np.multiply(velocity, normal, out=normal_component)
    normal_speed = workspace.random_float[3][:count]
    np.sum(normal_component, axis=1, out=normal_speed)
    moving_inward = workspace.field_valid[:count]
    np.less(normal_speed, 0.0, out=moving_inward)
    np.logical_and(collision, moving_inward, out=collision)

    tangent = workspace.field_coordinates[:count]
    np.multiply(normal, normal_speed[:, None], out=tangent)
    np.subtract(velocity, tangent, out=tangent)
    clamped_friction = workspace.random_float[4][:count]
    np.clip(friction, 0.0, 1.0, out=clamped_friction)
    np.subtract(1.0, clamped_friction, out=clamped_friction)
    np.multiply(tangent, clamped_friction[:, None], out=tangent)

    clamped_restitution = workspace.random_float[5][:count]
    np.clip(restitution, 0.0, 1.0, out=clamped_restitution)
    np.negative(normal_speed, out=normal_speed)
    np.multiply(clamped_restitution, normal_speed, out=clamped_restitution)
    np.multiply(normal, clamped_restitution[:, None], out=normal_component)
    np.add(tangent, normal_component, out=tangent)
    np.copyto(output, velocity, casting="unsafe")
    np.copyto(output, tangent, where=collision[:, None])


def _sample_sdf_trilinear(output, coordinates, binding, workspace, count) -> None:
    volume = binding.volume
    depth, height, width, _components = volume.shape
    dimensions = (width, height, depth)
    base = workspace.field_base[:count]
    next_index = workspace.field_next[:count]
    fraction = workspace.field_fraction[:count]
    np.multiply(coordinates, dimensions, out=fraction)
    np.subtract(fraction, np.float32(0.5), out=fraction)
    np.floor(fraction, out=base, casting="unsafe")
    np.subtract(fraction, base, out=fraction)
    np.clip(base, (0, 0, 0), (width - 1, height - 1, depth - 1), out=base)
    np.add(base, 1, out=next_index)
    np.minimum(next_index, (width - 1, height - 1, depth - 1), out=next_index)

    output.fill(0.0)
    flat = volume.reshape(-1, 4)
    corner = workspace.field_corner[:count]
    linear_index = workspace.field_linear_index[:count]
    texel = workspace.field_texel[:count]
    weight = workspace.field_weight[:count]
    for z in (0, 1):
        for y in (0, 1):
            for x in (0, 1):
                np.copyto(corner[:, 0], next_index[:, 0] if x else base[:, 0])
                np.copyto(corner[:, 1], next_index[:, 1] if y else base[:, 1])
                np.copyto(corner[:, 2], next_index[:, 2] if z else base[:, 2])
                _field_linear_indices(linear_index, corner, width, height)
                np.take(flat, linear_index, axis=0, out=texel)
                np.copyto(weight, fraction[:, 0] if x else 1.0 - fraction[:, 0])
                np.multiply(
                    weight,
                    fraction[:, 1] if y else 1.0 - fraction[:, 1],
                    out=weight,
                )
                np.multiply(
                    weight,
                    fraction[:, 2] if z else 1.0 - fraction[:, 2],
                    out=weight,
                )
                np.add(output, texel[:, 0] * weight, out=output)


def _sample_sdf_collision(position, sample, context, workspace, count):
    binding, inverted = sample
    volume = binding.volume
    depth, height, width, _components = volume.shape
    simulation_to_field, normal_to_simulation, distance_scale = (
        binding.sampling_matrices(context)
    )
    coordinates = workspace.sdf_coordinates[:count]
    np.matmul(position, simulation_to_field[:3, :3].T, out=coordinates)
    np.add(coordinates, simulation_to_field[:3, 3], out=coordinates)
    np.add(coordinates, np.float32(0.5), out=coordinates)

    component_valid = workspace.field_component_valid[:count]
    valid = workspace.field_valid[:count]
    np.greater_equal(coordinates, np.float32(0.0), out=component_valid)
    np.all(component_valid, axis=1, out=valid)
    np.less_equal(coordinates, np.float32(1.0), out=component_valid)
    np.logical_and(valid, np.all(component_valid, axis=1), out=valid)
    np.clip(coordinates, np.float32(0.0), np.float32(1.0), out=coordinates)

    distance = workspace.sdf_distance[:count]
    _sample_sdf_trilinear(distance, coordinates, binding, workspace, count)
    np.multiply(distance, distance_scale, out=distance)

    gradient = workspace.sdf_normal[:count]
    offset_coordinates = workspace.field_coordinates[:count]
    sample_a = workspace.sdf_sample_a[:count]
    sample_b = workspace.sdf_sample_b[:count]
    texel_steps = (1.0 / width, 1.0 / height, 1.0 / depth)
    for axis, step in enumerate(texel_steps):
        np.copyto(offset_coordinates, coordinates)
        np.subtract(offset_coordinates[:, axis], np.float32(step), out=offset_coordinates[:, axis])
        np.clip(offset_coordinates, np.float32(0.0), np.float32(1.0), out=offset_coordinates)
        _sample_sdf_trilinear(sample_a, offset_coordinates, binding, workspace, count)
        np.copyto(offset_coordinates, coordinates)
        np.add(offset_coordinates[:, axis], np.float32(step), out=offset_coordinates[:, axis])
        np.clip(offset_coordinates, np.float32(0.0), np.float32(1.0), out=offset_coordinates)
        _sample_sdf_trilinear(sample_b, offset_coordinates, binding, workspace, count)
        np.subtract(sample_b, sample_a, out=gradient[:, axis])
        np.divide(gradient[:, axis], np.float32(2.0 * step), out=gradient[:, axis])

    np.matmul(gradient, normal_to_simulation.T, out=offset_coordinates)
    _normalize(gradient, offset_coordinates, workspace.sdf_sample_a[:count])
    if inverted:
        np.negative(distance, out=distance)
        np.negative(gradient, out=gradient)
    return gradient, distance, valid


def _collide_sdf_position(
    output,
    position,
    velocity,
    particle_radius,
    restitution,
    friction,
    sample,
    context,
    workspace,
    count,
) -> None:
    del velocity, restitution, friction
    normal, distance, valid = _sample_sdf_collision(
        position, sample, context, workspace, count
    )
    radius = workspace.random_float[0][:count]
    np.maximum(particle_radius, 0.0, out=radius)
    penetration = workspace.random_float[1][:count]
    np.subtract(radius, distance, out=penetration)
    colliding = workspace.sample_valid[:count]
    np.greater(penetration, 0.0, out=colliding)
    np.logical_and(colliding, valid, out=colliding)
    correction = workspace.field_fraction[:count]
    np.multiply(normal, penetration[:, None], out=correction)
    np.copyto(output, position, casting="unsafe")
    np.add(output, correction, out=output, where=colliding[:, None])


def _collide_sdf_velocity(
    output,
    position,
    velocity,
    particle_radius,
    restitution,
    friction,
    sample,
    context,
    workspace,
    count,
) -> None:
    normal, distance, valid = _sample_sdf_collision(
        position, sample, context, workspace, count
    )
    radius = workspace.random_float[0][:count]
    np.maximum(particle_radius, 0.0, out=radius)
    colliding = workspace.sample_valid[:count]
    np.less(distance, radius, out=colliding)
    np.logical_and(colliding, valid, out=colliding)

    normal_component = workspace.field_fraction[:count]
    np.multiply(velocity, normal, out=normal_component)
    normal_speed = workspace.random_float[1][:count]
    np.sum(normal_component, axis=1, out=normal_speed)
    moving_into_surface = workspace.field_valid[:count]
    np.less(normal_speed, 0.0, out=moving_into_surface)
    np.logical_and(colliding, moving_into_surface, out=colliding)

    tangent = workspace.field_coordinates[:count]
    np.multiply(normal, normal_speed[:, None], out=tangent)
    np.subtract(velocity, tangent, out=tangent)
    clamped_friction = workspace.random_float[2][:count]
    np.clip(friction, 0.0, 1.0, out=clamped_friction)
    np.subtract(1.0, clamped_friction, out=clamped_friction)
    np.multiply(tangent, clamped_friction[:, None], out=tangent)

    clamped_restitution = workspace.random_float[3][:count]
    np.clip(restitution, 0.0, 1.0, out=clamped_restitution)
    np.negative(normal_speed, out=normal_speed)
    np.multiply(clamped_restitution, normal_speed, out=clamped_restitution)
    np.multiply(normal, clamped_restitution[:, None], out=normal_component)
    np.add(tangent, normal_component, out=tangent)
    np.copyto(output, velocity, casting="unsafe")
    np.copyto(output, tangent, where=colliding[:, None])


def _sample_point_cache(output, index_or_id, sample, context, workspace) -> None:
    binding, channel_name, value_type, lookup, semantic = sample
    channel = binding.channel(channel_name, value_type)
    count = output.shape[0]
    sample_ids = workspace.sample_ids[:count]
    point_indices = workspace.sample_indices[:count]
    valid = workspace.sample_valid[:count]
    np.copyto(sample_ids, index_or_id, casting="unsafe")
    if lookup == "stable_id":
        binding.asset.lookup_indices(sample_ids, point_indices)
    else:
        np.copyto(point_indices, sample_ids)

    point_count = int(binding.asset.point_count)
    np.less(point_indices, point_count, out=valid)
    np.minimum(point_indices, point_count - 1, out=point_indices)
    np.take(channel, point_indices, axis=0, out=output)
    mask = valid if output.ndim == 1 else valid[:, None]
    np.multiply(output, mask, out=output, casting="unsafe")

    if semantic == "raw":
        return
    matrix = binding.cache_to_simulation(context)
    linear = matrix[:3, :3]
    if semantic == "normal":
        try:
            linear = np.linalg.inv(linear).T.astype(np.float32)
        except np.linalg.LinAlgError as exc:
            raise NumpyParticleBackendError(
                f"point cache interface {binding.interface.stable_id!r} has a singular normal transform"
            ) from exc
    transformed = workspace.sample_vector[:count]
    np.matmul(output, linear.T, out=transformed)
    if semantic == "position":
        np.add(transformed, matrix[:3, 3], out=transformed)
    np.copyto(output, transformed)


def _sample_vector_field(output, position, binding, context, workspace) -> None:
    volume = binding.volume
    depth, height, width, _components = volume.shape
    count = output.shape[0]
    coordinates = workspace.field_coordinates[:count]
    field_from_simulation, simulation_from_vector = binding.sampling_matrices(context)
    np.matmul(position, field_from_simulation[:3, :3].T, out=coordinates)
    np.add(coordinates, field_from_simulation[:3, 3], out=coordinates)

    boundary = binding.interface.boundary
    valid = workspace.field_valid[:count]
    if boundary is VectorFieldBoundary.ZERO:
        component_valid = workspace.field_component_valid[:count]
        np.greater_equal(coordinates, np.float32(0.0), out=component_valid)
        np.all(component_valid, axis=1, out=valid)
        np.less_equal(coordinates, np.float32(1.0), out=component_valid)
        np.logical_and(valid, np.all(component_valid, axis=1), out=valid)
        np.clip(coordinates, np.float32(0.0), np.float32(1.0), out=coordinates)
    elif boundary is VectorFieldBoundary.CLAMP:
        valid.fill(True)
        np.clip(coordinates, np.float32(0.0), np.float32(1.0), out=coordinates)
    else:
        valid.fill(True)
        np.mod(coordinates, np.float32(1.0), out=coordinates)

    dimensions = np.asarray((width, height, depth), dtype=np.float32)
    integer_dimensions = np.asarray((width, height, depth), dtype=np.int64)
    base = workspace.field_base[:count]
    next_index = workspace.field_next[:count]
    fraction = workspace.field_fraction[:count]
    flat = volume.reshape(-1, 4)
    linear_index = workspace.field_linear_index[:count]
    texel = workspace.field_texel[:count]

    if binding.interface.filtering is VectorFieldFilter.NEAREST:
        np.multiply(coordinates, dimensions, out=fraction)
        np.floor(fraction, out=fraction)
        np.copyto(base, fraction, casting="unsafe")
        if boundary is VectorFieldBoundary.REPEAT:
            np.mod(base, integer_dimensions, out=base)
        else:
            np.minimum(base, integer_dimensions - 1, out=base)
        _field_linear_indices(linear_index, base, width, height)
        np.take(flat, linear_index, axis=0, out=texel)
        np.copyto(output, texel[:, :3])
    else:
        np.multiply(coordinates, dimensions, out=fraction)
        np.subtract(fraction, np.float32(0.5), out=fraction)
        np.floor(fraction, out=base, casting="unsafe")
        np.subtract(fraction, base, out=fraction)
        np.add(base, 1, out=next_index)
        if boundary is VectorFieldBoundary.REPEAT:
            np.mod(base, integer_dimensions, out=base)
            np.mod(next_index, integer_dimensions, out=next_index)
        else:
            np.clip(base, 0, integer_dimensions - 1, out=base)
            np.clip(next_index, 0, integer_dimensions - 1, out=next_index)
        output.fill(0.0)
        weight = workspace.field_weight[:count]
        z_weight = workspace.random_float[0][:count]
        y_weight = workspace.random_float[1][:count]
        x_weight = workspace.random_float[2][:count]
        corner_weight = workspace.random_float[3][:count]
        corner = workspace.field_corner[:count]
        for z_choice in (0, 1):
            if z_choice:
                np.copyto(z_weight, fraction[:, 2])
                z_indices = next_index[:, 2]
            else:
                np.subtract(np.float32(1.0), fraction[:, 2], out=z_weight)
                z_indices = base[:, 2]
            for y_choice in (0, 1):
                if y_choice:
                    np.copyto(y_weight, fraction[:, 1])
                    y_indices = next_index[:, 1]
                else:
                    np.subtract(np.float32(1.0), fraction[:, 1], out=y_weight)
                    y_indices = base[:, 1]
                np.multiply(z_weight, y_weight, out=weight)
                for x_choice in (0, 1):
                    if x_choice:
                        np.copyto(x_weight, fraction[:, 0])
                        x_indices = next_index[:, 0]
                    else:
                        np.subtract(np.float32(1.0), fraction[:, 0], out=x_weight)
                        x_indices = base[:, 0]
                    np.multiply(weight, x_weight, out=corner_weight)
                    corner[:, 0] = x_indices
                    corner[:, 1] = y_indices
                    corner[:, 2] = z_indices
                    _field_linear_indices(linear_index, corner, width, height)
                    np.take(flat, linear_index, axis=0, out=texel)
                    np.multiply(
                        texel[:, :3],
                        corner_weight[:, None],
                        out=workspace.sample_vector[:count],
                    )
                    np.add(output, workspace.sample_vector[:count], out=output)

    np.multiply(output, valid[:, None], out=output)
    transformed = workspace.sample_vector[:count]
    np.matmul(output, simulation_from_vector.T, out=transformed)
    np.copyto(output, transformed)


def _field_linear_indices(output, indices, width: int, height: int) -> None:
    np.multiply(indices[:, 2], height, out=output)
    np.add(output, indices[:, 1], out=output)
    np.multiply(output, width, out=output)
    np.add(output, indices[:, 0], out=output)


def _convert_space(output, source, parameters, context) -> None:
    matrix = context.conversion(parameters["from"], parameters["to"])
    np.matmul(source, matrix[:3, :3].T, out=output)
    if parameters["semantic"] == "position":
        np.add(output, matrix[:3, 3], out=output)


def _matrix4(value, fallback: np.ndarray) -> np.ndarray:
    if value is None:
        return fallback.copy()
    result = np.asarray(value, dtype=np.float32)
    if result.shape != (4, 4) or not np.isfinite(result).all():
        raise NumpyParticleBackendError("particle transforms must be finite 4x4 matrices")
    return np.ascontiguousarray(result)


def _component_count(value_type: TypeRef) -> int:
    return {
        ValueType.VEC2: 2,
        ValueType.VEC3: 3,
        ValueType.VEC4: 4,
        ValueType.COLOR: 4,
        ValueType.MAT3: 9,
        ValueType.MAT4: 16,
    }.get(value_type.value_type, 1)


def _dtype(value_type: TypeRef):
    return {
        ValueType.BOOL: np.bool_,
        ValueType.I32: np.int32,
        ValueType.U32: np.uint32,
        ValueType.F32: np.float32,
        ValueType.VEC2: np.float32,
        ValueType.VEC3: np.float32,
        ValueType.VEC4: np.float32,
        ValueType.COLOR: np.float32,
        ValueType.MAT3: np.float32,
        ValueType.MAT4: np.float32,
    }.get(value_type.value_type)


def _allocate_array(capacity: int, value_type: TypeRef) -> np.ndarray:
    dtype = _dtype(value_type)
    if dtype is None:
        raise NumpyParticleBackendError(f"NumPy backend cannot store {value_type.value_type.value}")
    components = _component_count(value_type)
    shape = (capacity,) if components == 1 else (capacity, components)
    return np.empty(shape, dtype=dtype)


def _constant_value(value: Any, value_type: TypeRef):
    dtype = _dtype(value_type)
    if dtype is None:
        raise NumpyParticleBackendError(f"NumPy backend cannot lower constant {value_type}")
    components = _component_count(value_type)
    result = np.asarray(value, dtype=dtype)
    if components == 1:
        return result.reshape(()).item()
    return np.ascontiguousarray(result.reshape(components))


def executable_stage_writes(
    executable: NumpyStageExecutable,
    kernel: ParticleEmitterKernelIR,
) -> frozenset[str]:
    function = {
        "init": kernel.init,
        "update": kernel.update,
        "rendering": kernel.rendering,
    }[executable.stage]
    return frozenset(function.written_attributes)


__all__ = [
    "NumpyParticleBackendError",
    "NumpyParticleCompiler",
    "NumpyParticleEmitterProgram",
    "NumpyParticleEmitterRuntime",
    "NumpyParticleProgram",
    "NumpyStageExecutable",
    "PARTICLE_INSTANCE_FLOATS",
]
