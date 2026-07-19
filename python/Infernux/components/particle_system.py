"""ParticleGraph runtime component with legacy VFX asset compatibility."""

from __future__ import annotations

import hashlib
import os
from typing import Optional

import numpy as np

from Infernux.core.asset_ref import ParticleGraphRef, VfxSystemRef
from Infernux.core.vfx_system import VfxSystem
from Infernux.debug import Debug
from Infernux.particle import (
    ExecutionTarget,
    GpuParticleEmitterController,
    NumpyParticleCompiler,
    NumpyParticleEmitterRuntime,
    ParticleArtifactRegistry,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleKernelLowerer,
    ParticleKernelProgram,
    ParticleRuntimeCompatibility,
    decode_gpu_particle_spirv,
    decode_particle_runtime_metadata,
)
from Infernux.vfx import CpuParticleRuntime, VfxCompileError, VfxGraphCompiler

from .component import InxComponent
from .decorators import add_component_menu, disallow_multiple
from .serialized_field import get_raw_field_value, serialized_field


@disallow_multiple
@add_component_menu("VFX/Particle System")
class ParticleSystem(InxComponent):
    graph: ParticleGraphRef = serialized_field(
        default=None,
        asset_type="ParticleGraph",
        tooltip="ParticleGraph or ParticleScript asset",
    )
    # Kept only so scenes authored before ParticleGraph remain loadable.
    system: VfxSystemRef = serialized_field(default=None, asset_type="VfxSystem", hidden=True)
    simulation_target: ExecutionTarget = serialized_field(default=ExecutionTarget.AUTO)
    simulation_speed: float = serialized_field(default=1.0, range=(0.0, 10.0))
    play_on_awake: bool = serialized_field(default=True)

    _runtime: Optional[CpuParticleRuntime] = None
    _runtimes: list[NumpyParticleEmitterRuntime]
    _cpu_emitter_indices: list[int]
    _gpu_controllers: list[GpuParticleEmitterController]
    _gpu_emitter_ids: list[int]
    _gpu_emitter_indices: list[int]
    _emitter_runtime_targets: tuple[ExecutionTarget, ...]
    _emitter_reload_compatibility: tuple[ParticleRuntimeCompatibility | None, ...]
    _runtime_target: ExecutionTarget | None = None
    _particle_program = None
    _particle_metadata = None
    _artifact_revision: int = 0
    _artifact_source_key: str = ""
    _emitter_to_world_cache: Optional[np.ndarray] = None
    _gpu_transform_buffers: dict[bool, np.ndarray]
    _batch_id: int = 0
    _submitted_batch_ids: set[int]
    _playing: bool = False
    _legacy_emitter_index: int = 0

    @property
    def emitter_index(self) -> int:
        """Legacy VfxSystem selector; ParticleGraph always owns all emitters."""
        return int(getattr(self, "_legacy_emitter_index", 0))

    @emitter_index.setter
    def emitter_index(self, value: int) -> None:
        if type(value) is not int or value < 0:
            raise ValueError("legacy emitter_index must be a non-negative integer")
        self._legacy_emitter_index = value

    def awake(self):
        self._runtime = None
        self._runtimes = []
        self._cpu_emitter_indices = []
        self._gpu_controllers = []
        self._gpu_emitter_ids = []
        self._gpu_emitter_indices = []
        self._emitter_runtime_targets = ()
        self._emitter_reload_compatibility = ()
        self._runtime_target = None
        self._particle_program = None
        self._particle_metadata = None
        self._artifact_revision = 0
        self._artifact_source_key = ""
        self._emitter_to_world_cache = None
        self._gpu_transform_buffers = {}
        self._submitted_batch_ids = set()
        self._batch_id = (int(self.game_object.id) << 16) ^ int(self.component_id)
        if self._batch_id == 0:
            self._batch_id = int(self.component_id) or 1
        self._playing = bool(self.play_on_awake)

    def _serialize_fields_document(self) -> dict:
        document = super()._serialize_fields_document()
        graph_ref = get_raw_field_value(self, "graph")
        legacy_ref = get_raw_field_value(self, "system")
        has_graph = isinstance(graph_ref, ParticleGraphAsset) or bool(graph_ref) or bool(
            getattr(graph_ref, "path_hint", "")
        )
        has_legacy = isinstance(legacy_ref, VfxSystem) or bool(legacy_ref) or bool(
            getattr(legacy_ref, "path_hint", "")
        )
        if not has_graph and has_legacy:
            document["emitter_index"] = self.emitter_index
        return document

    def _deserialize_fields_document(
        self, data: dict, *, _skip_on_after_deserialize: bool = False
    ) -> None:
        if not isinstance(data, dict):
            raise TypeError("Python component fields document must be an object")
        legacy_index = data.get("emitter_index", 0)
        if type(legacy_index) is not int or legacy_index < 0:
            raise ValueError("legacy emitter_index must be a non-negative integer")
        super()._deserialize_fields_document(
            data,
            _skip_on_after_deserialize=_skip_on_after_deserialize,
        )
        self._legacy_emitter_index = legacy_index

    def start(self):
        if not self._has_runtime():
            self._compile_asset()

    def on_enable(self):
        if hasattr(self, "_runtimes") and not self._has_runtime():
            self._compile_asset()

    def play(self, emitter_index: int | None = None) -> bool:
        if emitter_index is None:
            self._playing = True
            runtimes = tuple(getattr(self, "_runtimes", ())) + tuple(
                getattr(self, "_gpu_controllers", ())
            )
            for runtime in runtimes:
                runtime.play()
            return bool(runtimes)
        runtime = self._runtime_at(emitter_index)
        if runtime is None:
            return False
        runtime.play()
        return True

    def pause(self, emitter_index: int | None = None) -> bool:
        if emitter_index is None:
            self._playing = False
            runtimes = tuple(getattr(self, "_runtimes", ())) + tuple(
                getattr(self, "_gpu_controllers", ())
            )
            for runtime in runtimes:
                runtime.pause()
            return bool(runtimes)
        runtime = self._runtime_at(emitter_index)
        if runtime is None:
            return False
        runtime.pause()
        return True

    def stop(self, emitter_index: int | None = None) -> bool:
        if emitter_index is None:
            self._playing = False
            for index, runtime in zip(
                getattr(self, "_cpu_emitter_indices", ()),
                getattr(self, "_runtimes", ()),
            ):
                runtime.reset()
                runtime.pause()
                self._remove_emitter_batches(index)
            for runtime in getattr(self, "_gpu_controllers", ()):
                runtime.reset(playing=False)
            self._reset_gpu_emitters()
            if self._runtime is not None:
                self._runtime.reset()
                self._remove_native_batch()
            return bool(self._runtimes or self._gpu_controllers or self._runtime)
        runtime = self._runtime_at(emitter_index)
        if runtime is None:
            return False
        if self.emitter_runtime_target(emitter_index) is ExecutionTarget.GPU:
            runtime.reset(playing=False)
            self._reset_gpu_emitters(emitter_index)
        else:
            runtime.reset()
            runtime.pause()
            self._remove_emitter_batches(emitter_index)
        return True

    def restart(self, emitter_index: int | None = None) -> bool:
        if emitter_index is None:
            self._playing = True
            for runtime in getattr(self, "_runtimes", ()):
                runtime.reset()
                runtime.play()
            for runtime in getattr(self, "_gpu_controllers", ()):
                runtime.reset(playing=True)
            self._reset_gpu_emitters()
            if self._runtime is not None:
                self._runtime.reset()
            return bool(self._runtimes or self._gpu_controllers or self._runtime)
        runtime = self._runtime_at(emitter_index)
        if runtime is None:
            return False
        if self.emitter_runtime_target(emitter_index) is ExecutionTarget.GPU:
            runtime.reset(playing=True)
            self._reset_gpu_emitters(emitter_index)
        else:
            runtime.reset()
            runtime.play()
        return True

    def start_emitter(self, emitter_index: int) -> bool:
        return self.play(emitter_index)

    def pause_emitter(self, emitter_index: int) -> bool:
        return self.pause(emitter_index)

    def terminate_emitter(self, emitter_index: int) -> bool:
        return self.stop(emitter_index)

    def emitter_runtime_target(self, emitter_index: int) -> ExecutionTarget | None:
        """Return the active backend for an emitter index, or ``None`` if invalid."""
        if type(emitter_index) is not int:
            return None
        targets = getattr(self, "_emitter_runtime_targets", ())
        return targets[emitter_index] if 0 <= emitter_index < len(targets) else None

    def emitter_reload_compatibility(
        self, emitter_index: int
    ) -> ParticleRuntimeCompatibility | None:
        """Return the compatibility class used by the last published reload."""
        if type(emitter_index) is not int:
            return None
        compatibility = getattr(self, "_emitter_reload_compatibility", ())
        return (
            compatibility[emitter_index]
            if 0 <= emitter_index < len(compatibility)
            else None
        )

    def update(self, delta_time: float):
        if not self._has_runtime() and not self._compile_asset():
            return
        self._reload_published_artifact_if_needed()
        scaled_delta_time = float(delta_time) * float(self.simulation_speed)
        if scaled_delta_time < 0.0:
            return
        if self._gpu_controllers:
            self._update_gpu_particle_graph(scaled_delta_time)
        if self._runtimes:
            self._update_particle_graph(scaled_delta_time)
        elif self._playing and self._runtime is not None:
            self._update_legacy_vfx(scaled_delta_time)

    def on_disable(self):
        self._remove_native_batch()
        self._clear_runtime_state()

    def on_destroy(self):
        self._remove_native_batch()
        self._clear_runtime_state()

    def on_validate(self):
        self._remove_native_batch()
        self._clear_runtime_state()

    def _compile_asset(self) -> bool:
        graph_ref = get_raw_field_value(self, "graph")
        if graph_ref is not None and (
            isinstance(graph_ref, ParticleGraphAsset)
            or bool(graph_ref)
            or getattr(graph_ref, "path_hint", "")
            or (
                callable(getattr(graph_ref, "resolve", None))
                and graph_ref.resolve() is not None
            )
        ):
            return self._compile_particle_graph(graph_ref)
        return self._compile_legacy_vfx()

    def _compile_particle_graph(self, graph_ref: ParticleGraphRef) -> bool:
        try:
            path = self._particle_source_path(graph_ref)
            artifact = None
            if path:
                guid = getattr(graph_ref, "guid", "")
                artifact = ParticleArtifactRegistry.get(path, guid=guid)
                if artifact is None:
                    artifact = ParticleArtifactRegistry.compile_path(path, guid=guid)
                hir = artifact.hir
                kernel = ParticleKernelProgram.from_dict(artifact.kernel_ir)
                revision = artifact.revision
                source_key = artifact.source_key
            else:
                asset = (
                    graph_ref
                    if isinstance(graph_ref, ParticleGraphAsset)
                    else graph_ref.resolve()
                )
                if not isinstance(asset, ParticleGraphAsset):
                    return False
                hir = ParticleGraphCompiler().compile(asset)
                kernel = ParticleKernelLowerer().lower(hir)
                revision = 0
                source_key = ""
            metadata = decode_particle_runtime_metadata(hir)
            targets = self._select_runtime_targets(metadata, artifact)
            previous_metadata = getattr(self, "_particle_metadata", None)
            previous_program = getattr(self, "_particle_program", None)
            previous_cpu = (
                {
                    emitter.stable_id: runtime
                    for emitter, runtime in zip(
                        previous_program.emitters,
                        getattr(self, "_runtimes", ()),
                    )
                }
                if previous_program is not None
                else {}
            )
            previous_ids = (
                set(previous_metadata.schedule)
                if previous_metadata is not None
                else set()
            )
            reload_compatibility = [None] * len(metadata.emitters)
            cpu_indices = [
                index
                for index, target in enumerate(targets)
                if target is ExecutionTarget.CPU
            ]
            if cpu_indices:
                cpu_emitter_ids = {
                    metadata.emitters[index].stable_id for index in cpu_indices
                }
                program = NumpyParticleCompiler().compile(
                    hir,
                    kernel,
                    emitter_ids=cpu_emitter_ids,
                )
                runtimes = []
                for emitter_index, emitter in zip(cpu_indices, program.emitters):
                    previous = previous_cpu.get(emitter.stable_id)
                    runtime = None
                    if previous is not None:
                        runtime, compatibility = previous.migrate_to(emitter)
                        reload_compatibility[emitter_index] = compatibility
                    if runtime is None:
                        runtime = emitter.create_runtime()
                        should_play = (
                            previous.is_playing if previous is not None else self._playing
                        )
                        if not should_play:
                            runtime.pause()
                    runtimes.append(runtime)
            else:
                program = None
                runtimes = []
            for emitter_index, emitter in enumerate(metadata.emitters):
                if (
                    targets[emitter_index] is ExecutionTarget.GPU
                    and emitter.stable_id in previous_ids
                ):
                    reload_compatibility[emitter_index] = (
                        ParticleRuntimeCompatibility.EMITTER_RESTART
                    )
            if any(target is ExecutionTarget.GPU for target in targets):
                self._publish_gpu_particle_graph(artifact, metadata, targets)
            else:
                self._remove_gpu_emitters()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            Debug.log_error(f"[ParticleSystem] ParticleGraph compile failed: {exc}")
            return False

        self._remove_cpu_batches()
        self._runtime = None
        self._particle_program = program
        self._runtimes = runtimes
        self._cpu_emitter_indices = cpu_indices
        self._particle_metadata = metadata
        self._emitter_runtime_targets = targets
        self._emitter_reload_compatibility = tuple(reload_compatibility)
        self._runtime_target = (
            targets[0]
            if targets and all(target is targets[0] for target in targets)
            else ExecutionTarget.AUTO
        )
        self._artifact_revision = revision
        self._artifact_source_key = source_key
        self._emitter_to_world_cache = None
        return True

    def _compile_legacy_vfx(self) -> bool:
        system = self.system
        if system is None or not isinstance(system, VfxSystem):
            return False
        index = int(self.emitter_index)
        if index < 0 or index >= len(system.emitters):
            Debug.log_error(f"[ParticleSystem] Emitter index {index} is out of range")
            return False
        emitter = system.emitters[index]
        try:
            artifact = VfxGraphCompiler().compile(emitter)
        except VfxCompileError as exc:
            Debug.log_error(f"[ParticleSystem] VFX compile failed: {exc}")
            return False
        self._remove_gpu_emitters()
        self._remove_cpu_batches()
        self._runtime = CpuParticleRuntime(artifact)
        self._runtimes = []
        self._cpu_emitter_indices = []
        self._particle_program = None
        self._particle_metadata = None
        self._material_guid = emitter.renderer.material
        self._emitter_runtime_targets = ()
        self._emitter_reload_compatibility = ()
        self._runtime_target = ExecutionTarget.CPU
        return True

    def _select_runtime_targets(self, metadata, artifact) -> tuple[ExecutionTarget, ...]:
        override = ExecutionTarget(get_raw_field_value(self, "simulation_target"))
        native = self._native_engine()
        gpu_available = bool(
            artifact is not None
            and native is not None
            and hasattr(native, "_replace_gpu_particle_emitters")
            and hasattr(native, "_begin_gpu_particle_frame")
        )
        targets = []
        for index, emitter in enumerate(metadata.emitters):
            declared = emitter.settings.target
            if (
                override is not ExecutionTarget.AUTO
                and declared is not ExecutionTarget.AUTO
                and override is not declared
            ):
                raise RuntimeError(
                    f"ParticleSystem requests {override.value}, but emitter {index} "
                    f"({emitter.stable_id}) requires {declared.value}"
                )
            target = override if override is not ExecutionTarget.AUTO else declared
            if target is ExecutionTarget.AUTO:
                target = ExecutionTarget.GPU if gpu_available else ExecutionTarget.CPU
            if target is ExecutionTarget.GPU and not gpu_available:
                raise RuntimeError(
                    f"emitter {index} ({emitter.stable_id}) requires GPU execution, "
                    "which needs a saved AOT artifact and graphical renderer"
                )
            targets.append(target)
        return tuple(targets)

    def _publish_gpu_particle_graph(self, artifact, metadata, targets) -> None:
        if artifact is None:
            raise RuntimeError("GPU ParticleGraph execution requires an AOT artifact")
        native = self._native_engine()
        if native is None:
            raise RuntimeError("GPU ParticleGraph execution requires a graphical renderer")

        glsl_emitters = artifact.gpu_glsl.get("emitters")
        if type(glsl_emitters) is not list or len(glsl_emitters) != len(metadata.emitters):
            raise RuntimeError("ParticleGraph GPU emitter metadata is incomplete")

        programs = []
        controllers = []
        emitter_ids = []
        emitter_indices = []
        previous_playing = {}
        previous_metadata = getattr(self, "_particle_metadata", None)
        if previous_metadata is not None:
            previous_playing = {
                previous_metadata.emitters[emitter_index].stable_id: controller.is_playing
                for emitter_index, controller in zip(
                    getattr(self, "_gpu_emitter_indices", ()),
                    getattr(self, "_gpu_controllers", ()),
                )
                if 0 <= emitter_index < len(previous_metadata.emitters)
            }
        for index, (emitter, glsl_emitter) in enumerate(
            zip(metadata.emitters, glsl_emitters)
        ):
            if targets[index] is not ExecutionTarget.GPU:
                continue
            if any(output.output_type != "sprite" for output in emitter.outputs):
                raise RuntimeError("the current GPU particle renderer supports Sprite Output only")
            if (
                type(glsl_emitter) is not dict
                or glsl_emitter.get("stable_id") != emitter.stable_id
                or type(glsl_emitter.get("state_stride")) is not int
            ):
                raise RuntimeError("ParticleGraph GPU layout does not match its runtime schedule")
            decoded = decode_gpu_particle_spirv(artifact.gpu_spirv, index)
            emitter_id = self._gpu_emitter_id(emitter.stable_id)
            programs.append(
                {
                    "id": emitter_id,
                    "artifact_revision": artifact.revision,
                    "stable_id": emitter.stable_id,
                    "capacity": emitter.settings.capacity,
                    "state_stride": glsl_emitter["state_stride"],
                    "stages": decoded["stages"],
                    "billboard": decoded["billboard"],
                    "outputs": [
                        {
                            "id": self._gpu_output_id(emitter.stable_id, output.output_id),
                            "stable_id": output.output_id,
                            "material": self._gpu_material_binding(output),
                            "receive_scene_lighting": output.receive_scene_lighting,
                            "receive_shadows": output.receive_shadows,
                            "sort_mode": output.sort_mode,
                        }
                        for output in emitter.outputs
                    ],
                }
            )
            controllers.append(
                GpuParticleEmitterController(
                    emitter.settings,
                    playing=previous_playing.get(emitter.stable_id, self._playing),
                )
            )
            emitter_ids.append(emitter_id)
            emitter_indices.append(index)

        removed = sorted(set(getattr(self, "_gpu_emitter_ids", ())) - set(emitter_ids))
        error = native._replace_gpu_particle_emitters(programs, removed)
        if error:
            raise RuntimeError(error)
        self._gpu_controllers = controllers
        self._gpu_emitter_ids = emitter_ids
        self._gpu_emitter_indices = emitter_indices

    def _update_gpu_particle_graph(self, delta_time: float) -> None:
        native = self._native_engine()
        metadata = self._particle_metadata
        if native is None or metadata is None:
            return
        emitter_to_world = self._emitter_matrix()
        if self._emitter_to_world_cache is None or not np.array_equal(
            emitter_to_world, self._emitter_to_world_cache
        ):
            self._emitter_to_world_cache = emitter_to_world
            self._gpu_transform_buffers = {}

        for emitter_id, emitter_index, controller in zip(
            self._gpu_emitter_ids,
            self._gpu_emitter_indices,
            self._gpu_controllers,
        ):
            emitter = metadata.emitters[emitter_index]
            schedule = controller.tick(delta_time)
            transforms = self._gpu_transform_buffer(
                emitter.settings.simulation_space.value == "local"
            )
            native._begin_gpu_particle_frame(
                emitter_id,
                schedule.spawn_count,
                schedule.spawn_base_id,
                schedule.spawn_generation,
                schedule.system_seed,
                schedule.simulation_step,
                schedule.delta_time,
                transforms,
                schedule.simulate,
                schedule.render,
            )

    def _update_particle_graph(self, delta_time: float) -> None:
        native = self._native_engine()
        if self._particle_program is None:
            return
        emitter_to_world = self._emitter_matrix()
        transforms_changed = self._emitter_to_world_cache is None or not np.array_equal(
            emitter_to_world, self._emitter_to_world_cache
        )
        if transforms_changed:
            self._emitter_to_world_cache = emitter_to_world
        for emitter_index, emitter, runtime in zip(
            self._cpu_emitter_indices,
            self._particle_program.emitters,
            self._runtimes,
        ):
            if transforms_changed:
                simulation_to_world = (
                    emitter_to_world if emitter.settings.simulation_space.value == "local" else None
                )
                runtime.set_transforms(emitter_to_world, simulation_to_world)
            instances = runtime.tick(delta_time)
            if native is None:
                continue
            for output_index, output in enumerate(emitter.outputs):
                batch_id = self._output_batch_id(emitter_index, output_index)
                native.submit_particle_instances(
                    batch_id,
                    instances,
                    self._output_material_guid(output.material),
                    validate=False,
                )
                self._submitted_batch_ids.add(batch_id)

    def _update_legacy_vfx(self, delta_time: float) -> None:
        instances = self._runtime.tick(delta_time)
        native = self._native_engine()
        if native is None:
            return
        position = self.transform.position
        native.submit_particle_instances(
            self._batch_id,
            instances,
            self._material_guid,
            float(position.x),
            float(position.y),
            float(position.z),
        )
        self._submitted_batch_ids.add(self._batch_id)

    def _reload_published_artifact_if_needed(self) -> None:
        if not self._artifact_source_key:
            return
        graph_ref = get_raw_field_value(self, "graph")
        path = self._particle_source_path(graph_ref)
        artifact = ParticleArtifactRegistry.get(
            path,
            guid=getattr(graph_ref, "guid", ""),
        )
        if artifact is not None and artifact.revision != self._artifact_revision:
            self._compile_particle_graph(graph_ref)

    def _runtime_at(self, emitter_index: int):
        if type(emitter_index) is not int:
            return None
        target = self.emitter_runtime_target(emitter_index)
        if target is ExecutionTarget.GPU:
            indices = getattr(self, "_gpu_emitter_indices", ())
            runtimes = getattr(self, "_gpu_controllers", ())
        elif target is ExecutionTarget.CPU:
            indices = getattr(self, "_cpu_emitter_indices", ())
            runtimes = getattr(self, "_runtimes", ())
        else:
            return None
        try:
            runtime_index = indices.index(emitter_index)
        except ValueError:
            return None
        return runtimes[runtime_index]

    def _has_runtime(self) -> bool:
        return bool(
            getattr(self, "_runtimes", ()) or getattr(self, "_gpu_controllers", ())
        ) or self._runtime is not None

    def _emitter_matrix(self) -> np.ndarray:
        flat = self.transform.local_to_world_matrix()
        return np.asarray(flat, dtype=np.float32).reshape((4, 4), order="F")

    def _gpu_transform_buffer(self, local_simulation: bool) -> np.ndarray:
        cached = self._gpu_transform_buffers.get(local_simulation)
        if cached is not None:
            return cached
        emitter_to_world = self._emitter_to_world_cache
        if emitter_to_world is None:
            emitter_to_world = self._emitter_matrix()
        try:
            world_to_emitter = np.linalg.inv(emitter_to_world).astype(np.float32)
        except np.linalg.LinAlgError:
            world_to_emitter = np.linalg.pinv(emitter_to_world).astype(np.float32)
        identity = np.identity(4, dtype=np.float32)
        simulation_to_world = emitter_to_world if local_simulation else identity
        world_to_simulation = world_to_emitter if local_simulation else identity
        result = np.ascontiguousarray(
            np.concatenate(
                [
                    emitter_to_world.reshape(16, order="F"),
                    world_to_emitter.reshape(16, order="F"),
                    simulation_to_world.reshape(16, order="F"),
                    world_to_simulation.reshape(16, order="F"),
                ]
            ),
            dtype=np.float32,
        )
        self._gpu_transform_buffers[local_simulation] = result
        return result

    @staticmethod
    def _particle_source_path(graph_ref: ParticleGraphRef) -> str:
        if graph_ref is None:
            return ""
        guid = getattr(graph_ref, "guid", "")
        path_hint = getattr(graph_ref, "path_hint", "")
        if guid:
            try:
                from Infernux.core.asset_ref import _get_asset_database

                database = _get_asset_database()
                if database:
                    return database.get_path_from_guid(guid) or path_hint
            except (AttributeError, RuntimeError):
                pass
        return path_hint

    def _gpu_emitter_id(self, stable_id: str) -> int:
        identity = f"{int(self._batch_id) & 0xFFFFFFFFFFFFFFFF}:{stable_id}"
        value = int.from_bytes(
            hashlib.blake2b(identity.encode("utf-8"), digest_size=8).digest(),
            "little",
        )
        return value or 1

    def _gpu_output_id(self, emitter_stable_id: str, output_stable_id: str) -> int:
        identity = (
            f"{int(self._batch_id) & 0xFFFFFFFFFFFFFFFF}:"
            f"{emitter_stable_id}:{output_stable_id}"
        )
        value = int.from_bytes(
            hashlib.blake2b(identity.encode("utf-8"), digest_size=8).digest(),
            "little",
        )
        return value or 1

    @staticmethod
    def _gpu_material_binding(output) -> dict[str, object]:
        state: dict[str, object] = {
            "render_queue": 3000,
            "blend_enabled": True,
            "depth_test_enabled": True,
            "depth_write_enabled": False,
            "native": None,
        }
        material_ref = output.material
        path = material_ref.path_hint
        if material_ref.guid:
            try:
                from Infernux.core.asset_ref import _get_asset_database

                database = _get_asset_database()
                if database:
                    path = database.get_path_from_guid(material_ref.guid) or path
            except (AttributeError, RuntimeError):
                pass
        material = None
        if not path:
            try:
                from Infernux.core.material import Material

                material = Material.get("ParticleSpriteMaterial")
            except (AttributeError, RuntimeError):
                pass
        if path and not os.path.isabs(path):
            try:
                from Infernux.engine.project_context import get_project_root

                project_root = get_project_root()
                if project_root:
                    path = os.path.join(project_root, path)
            except (AttributeError, RuntimeError):
                pass
        try:
            from Infernux.core.material import Material

            material = material or (Material.load(path) if path else None)
            if material is not None:
                state.update(
                    render_queue=int(material.render_queue),
                    blend_enabled=bool(material.blend_enable),
                    depth_test_enabled=bool(material.depth_test_enable),
                    depth_write_enabled=bool(material.depth_write_enable),
                    native=material.native,
                )
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass
        return state

    @staticmethod
    def _output_material_guid(material) -> str:
        if material.guid:
            return material.guid
        if material.path_hint:
            try:
                from Infernux.core.assets import AssetManager

                return AssetManager._get_guid_from_path(material.path_hint) or ""
            except (AttributeError, RuntimeError):
                return ""
        return ""

    def _output_batch_id(self, emitter_index: int, output_index: int) -> int:
        value = int(self._batch_id) & 0xFFFFFFFFFFFFFFFF
        value ^= ((emitter_index + 1) * 0x9E3779B185EBCA87) & 0xFFFFFFFFFFFFFFFF
        value ^= ((output_index + 1) * 0xC2B2AE3D27D4EB4F) & 0xFFFFFFFFFFFFFFFF
        return value or 1

    def _remove_emitter_batches(self, emitter_index: int) -> None:
        program = getattr(self, "_particle_program", None)
        if program is None:
            return
        try:
            runtime_index = getattr(self, "_cpu_emitter_indices", ()).index(
                emitter_index
            )
        except ValueError:
            return
        native = self._native_engine()
        for output_index in range(len(program.emitters[runtime_index].outputs)):
            batch_id = self._output_batch_id(emitter_index, output_index)
            if native is not None:
                native.remove_particle_batch(batch_id)
            self._submitted_batch_ids.discard(batch_id)

    def _reset_gpu_emitters(self, emitter_index: int | None = None) -> None:
        native = self._native_engine()
        if native is None or not hasattr(native, "_reset_gpu_particle_emitter"):
            return
        emitter_ids = getattr(self, "_gpu_emitter_ids", ())
        if emitter_index is None:
            selected = emitter_ids
        else:
            try:
                runtime_index = getattr(self, "_gpu_emitter_indices", ()).index(
                    emitter_index
                )
            except ValueError:
                return
            selected = (emitter_ids[runtime_index],)
        for emitter_id in selected:
            native._reset_gpu_particle_emitter(emitter_id)

    def _remove_gpu_emitters(self) -> None:
        native = self._native_engine()
        if native is not None and hasattr(native, "_remove_gpu_particle_emitter"):
            for emitter_id in getattr(self, "_gpu_emitter_ids", ()):
                native._remove_gpu_particle_emitter(emitter_id)
        self._gpu_emitter_ids = []
        self._gpu_emitter_indices = []
        self._gpu_controllers = []

    def _remove_cpu_batches(self) -> None:
        native = self._native_engine()
        batch_ids = set(getattr(self, "_submitted_batch_ids", set()))
        if self._batch_id:
            batch_ids.add(self._batch_id)
        if native is not None:
            for batch_id in batch_ids:
                native.remove_particle_batch(batch_id)
        self._submitted_batch_ids = set()

    def _clear_runtime_state(self) -> None:
        self._runtime = None
        self._runtimes = []
        self._cpu_emitter_indices = []
        self._gpu_controllers = []
        self._gpu_emitter_ids = []
        self._gpu_emitter_indices = []
        self._emitter_runtime_targets = ()
        self._emitter_reload_compatibility = ()
        self._runtime_target = None
        self._particle_program = None
        self._particle_metadata = None
        self._artifact_revision = 0
        self._artifact_source_key = ""
        self._emitter_to_world_cache = None
        self._gpu_transform_buffers = {}

    @staticmethod
    def _native_engine():
        try:
            from Infernux.engine.play_mode import PlayModeManager

            manager = PlayModeManager.instance()
            return getattr(manager, "_native_engine", None) if manager else None
        except Exception:
            return None

    def _remove_native_batch(self) -> None:
        self._remove_cpu_batches()
        self._remove_gpu_emitters()


__all__ = ["ParticleSystem"]
