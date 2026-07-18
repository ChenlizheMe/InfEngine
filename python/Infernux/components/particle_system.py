"""ParticleGraph runtime component with legacy VFX asset compatibility."""

from __future__ import annotations

from typing import Optional

import numpy as np

from Infernux.core.asset_ref import ParticleGraphRef, VfxSystemRef
from Infernux.core.vfx_system import VfxSystem
from Infernux.debug import Debug
from Infernux.particle import (
    NumpyParticleCompiler,
    NumpyParticleEmitterRuntime,
    ParticleArtifactRegistry,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleKernelLowerer,
    ParticleKernelProgram,
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
    emitter_index: int = serialized_field(default=0, range=(0, 1024), hidden=True)
    simulation_speed: float = serialized_field(default=1.0, range=(0.0, 10.0))
    play_on_awake: bool = serialized_field(default=True)

    _runtime: Optional[CpuParticleRuntime] = None
    _runtimes: list[NumpyParticleEmitterRuntime]
    _particle_program = None
    _artifact_revision: int = 0
    _artifact_source_key: str = ""
    _emitter_to_world_cache: Optional[np.ndarray] = None
    _batch_id: int = 0
    _submitted_batch_ids: set[int]
    _playing: bool = False

    def awake(self):
        self._runtime = None
        self._runtimes = []
        self._particle_program = None
        self._artifact_revision = 0
        self._artifact_source_key = ""
        self._emitter_to_world_cache = None
        self._submitted_batch_ids = set()
        self._batch_id = (int(self.game_object.id) << 16) ^ int(self.component_id)
        if self._batch_id == 0:
            self._batch_id = int(self.component_id) or 1
        self._playing = bool(self.play_on_awake)

    def start(self):
        self._compile_asset()

    def play(self, emitter_index: int | None = None) -> None:
        if emitter_index is None:
            self._playing = True
            for runtime in getattr(self, "_runtimes", ()):
                runtime.play()
            return
        runtime = self._runtime_at(emitter_index)
        if runtime is not None:
            runtime.play()

    def pause(self, emitter_index: int | None = None) -> None:
        if emitter_index is None:
            self._playing = False
            for runtime in getattr(self, "_runtimes", ()):
                runtime.pause()
            return
        runtime = self._runtime_at(emitter_index)
        if runtime is not None:
            runtime.pause()

    def stop(self, emitter_index: int | None = None) -> None:
        if emitter_index is None:
            self._playing = False
            for index, runtime in enumerate(getattr(self, "_runtimes", ())):
                runtime.reset()
                runtime.pause()
                self._remove_emitter_batches(index)
            if self._runtime is not None:
                self._runtime.reset()
                self._remove_native_batch()
            return
        runtime = self._runtime_at(emitter_index)
        if runtime is not None:
            runtime.reset()
            runtime.pause()
            self._remove_emitter_batches(emitter_index)

    def restart(self, emitter_index: int | None = None) -> None:
        if emitter_index is None:
            self._playing = True
            for runtime in getattr(self, "_runtimes", ()):
                runtime.reset()
                runtime.play()
            if self._runtime is not None:
                self._runtime.reset()
            return
        runtime = self._runtime_at(emitter_index)
        if runtime is not None:
            runtime.reset()
            runtime.play()

    def update(self, delta_time: float):
        if not self._has_runtime() and not self._compile_asset():
            return
        self._reload_published_artifact_if_needed()
        scaled_delta_time = float(delta_time) * float(self.simulation_speed)
        if scaled_delta_time < 0.0:
            return
        if self._runtimes:
            self._update_particle_graph(scaled_delta_time)
        elif self._playing and self._runtime is not None:
            self._update_legacy_vfx(scaled_delta_time)

    def on_disable(self):
        self._remove_native_batch()

    def on_destroy(self):
        self._remove_native_batch()

    def on_validate(self):
        self._remove_native_batch()
        self._runtime = None
        self._runtimes = []
        self._particle_program = None
        self._artifact_revision = 0
        self._artifact_source_key = ""
        self._emitter_to_world_cache = None

    def _compile_asset(self) -> bool:
        graph_ref = get_raw_field_value(self, "graph")
        if graph_ref is not None and (
            bool(graph_ref) or graph_ref.path_hint or graph_ref.resolve() is not None
        ):
            return self._compile_particle_graph(graph_ref)
        return self._compile_legacy_vfx()

    def _compile_particle_graph(self, graph_ref: ParticleGraphRef) -> bool:
        try:
            path = self._particle_source_path(graph_ref)
            if path:
                artifact = ParticleArtifactRegistry.get(path, guid=graph_ref.guid)
                if artifact is None:
                    artifact = ParticleArtifactRegistry.compile_path(path, guid=graph_ref.guid)
                kernel = ParticleKernelProgram.from_dict(artifact.kernel_ir)
                program = NumpyParticleCompiler().compile(artifact.hir, kernel)
                revision = artifact.revision
                source_key = artifact.source_key
            else:
                asset = graph_ref.resolve()
                if not isinstance(asset, ParticleGraphAsset):
                    return False
                hir = ParticleGraphCompiler().compile(asset)
                program = NumpyParticleCompiler().compile(hir, ParticleKernelLowerer().lower(hir))
                revision = 0
                source_key = ""
            runtimes = [emitter.create_runtime() for emitter in program.emitters]
            if not self._playing:
                for runtime in runtimes:
                    runtime.pause()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            Debug.log_error(f"[ParticleSystem] ParticleGraph compile failed: {exc}")
            return False

        self._remove_native_batch()
        self._runtime = None
        self._particle_program = program
        self._runtimes = runtimes
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
        self._runtime = CpuParticleRuntime(artifact)
        self._material_guid = emitter.renderer.material
        return True

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
        for emitter_index, (emitter, runtime) in enumerate(
            zip(self._particle_program.emitters, self._runtimes)
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
        artifact = ParticleArtifactRegistry.get(path, guid=graph_ref.guid)
        if artifact is not None and artifact.revision != self._artifact_revision:
            self._compile_particle_graph(graph_ref)

    def _runtime_at(self, emitter_index: int):
        if type(emitter_index) is not int:
            return None
        runtimes = getattr(self, "_runtimes", ())
        return runtimes[emitter_index] if 0 <= emitter_index < len(runtimes) else None

    def _has_runtime(self) -> bool:
        return bool(getattr(self, "_runtimes", ())) or self._runtime is not None

    def _emitter_matrix(self) -> np.ndarray:
        flat = self.transform.local_to_world_matrix()
        return np.asarray(flat, dtype=np.float32).reshape((4, 4), order="F")

    @staticmethod
    def _particle_source_path(graph_ref: ParticleGraphRef) -> str:
        if graph_ref is None:
            return ""
        if graph_ref.guid:
            try:
                from Infernux.core.asset_ref import _get_asset_database

                database = _get_asset_database()
                if database:
                    return database.get_path_from_guid(graph_ref.guid) or graph_ref.path_hint
            except (AttributeError, RuntimeError):
                pass
        return graph_ref.path_hint

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
        if program is None or not 0 <= emitter_index < len(program.emitters):
            return
        native = self._native_engine()
        for output_index in range(len(program.emitters[emitter_index].outputs)):
            batch_id = self._output_batch_id(emitter_index, output_index)
            if native is not None:
                native.remove_particle_batch(batch_id)
            self._submitted_batch_ids.discard(batch_id)

    @staticmethod
    def _native_engine():
        try:
            from Infernux.engine.play_mode import PlayModeManager

            manager = PlayModeManager.instance()
            return getattr(manager, "_native_engine", None) if manager else None
        except Exception:
            return None

    def _remove_native_batch(self) -> None:
        native = self._native_engine()
        batch_ids = set(getattr(self, "_submitted_batch_ids", set()))
        if self._batch_id:
            batch_ids.add(self._batch_id)
        if native is not None:
            for batch_id in batch_ids:
                native.remove_particle_batch(batch_id)
        self._submitted_batch_ids = set()


__all__ = ["ParticleSystem"]
