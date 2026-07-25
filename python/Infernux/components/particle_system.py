"""ParticleGraph runtime component."""

from __future__ import annotations

import hashlib
import os
import time
from typing import Optional

import numpy as np

from Infernux.core.asset_ref import ParticleGraphRef
from Infernux.debug import Debug
from Infernux.graph import AssetReference
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
    PointCache,
    SdfVolume,
    VectorField,
    build_gpu_particle_migration,
    classify_emitter_update,
    decode_gpu_particle_spirv,
    decode_particle_runtime_metadata,
)
from .component import InxComponent
from .decorators import add_component_menu, disallow_multiple
from .serialized_field import get_raw_field_value, serialized_field


@disallow_multiple
@add_component_menu("VFX/Particle System")
class ParticleSystem(InxComponent):
    _display_name_key = "component.particle_system"
    graph: ParticleGraphRef = serialized_field(
        default=None,
        asset_type="ParticleGraph",
        tooltip="ParticleGraph or ParticleScript asset",
        display_name_key="particle_system.graph",
    )
    simulation_speed: float = serialized_field(
        default=1.0,
        range=(0.0, 10.0),
        display_name_key="particle_system.simulation_speed",
    )
    play_on_awake: bool = serialized_field(
        default=True,
        display_name_key="particle_system.play_on_awake",
    )

    _runtimes: list[NumpyParticleEmitterRuntime]
    _cpu_emitter_indices: list[int]
    _gpu_controllers: list[GpuParticleEmitterController]
    _gpu_emitter_ids: list[int]
    _gpu_emitter_indices: list[int]
    _emitter_runtime_targets: tuple[ExecutionTarget, ...]
    _emitter_reload_compatibility: tuple[ParticleRuntimeCompatibility | None, ...]
    _runtime_target: ExecutionTarget | None = None
    _particle_program = None
    _particle_kernel = None
    _particle_gpu_layouts: tuple[dict, ...]
    _particle_metadata = None
    _artifact_revision: int = 0
    _artifact_source_key: str = ""
    _emitter_to_world_cache: Optional[np.ndarray] = None
    _gpu_transform_buffers: dict[bool, np.ndarray]
    _batch_id: int = 0
    _submitted_batch_ids: set[int]
    _gpu_diagnostic_requests: set[int]
    _data_interface_overrides: dict[str, AssetReference]
    _playing: bool = False
    _editor_preview_active: bool = False
    _compile_retry_at: float = 0.0
    _last_compile_error: str = ""
    _last_compile_error_log_at: float = 0.0

    def awake(self):
        if hasattr(self, "_submitted_batch_ids"):
            self._remove_native_batch()
        self._initialize_runtime_state(bool(self.play_on_awake))

    def _initialize_runtime_state(self, playing: bool) -> None:
        self._runtimes = []
        self._cpu_emitter_indices = []
        self._gpu_controllers = []
        self._gpu_emitter_ids = []
        self._gpu_emitter_indices = []
        self._emitter_runtime_targets = ()
        self._emitter_reload_compatibility = ()
        self._runtime_target = None
        self._particle_program = None
        self._particle_kernel = None
        self._particle_gpu_layouts = ()
        self._particle_metadata = None
        self._artifact_revision = 0
        self._artifact_source_key = ""
        self._emitter_to_world_cache = None
        self._gpu_transform_buffers = {}
        self._submitted_batch_ids = set()
        self._gpu_diagnostic_requests = set()
        self._data_interface_overrides = {}
        self._batch_id = (int(self.game_object.id) << 16) ^ int(self.component_id)
        if self._batch_id == 0:
            self._batch_id = int(self.component_id) or 1
        self._playing = bool(playing)
        self._editor_preview_active = False
        self._compile_retry_at = 0.0
        self._last_compile_error = ""
        self._last_compile_error_log_at = 0.0

    def _ensure_runtime_state(self, *, playing: bool = False) -> None:
        if not hasattr(self, "_submitted_batch_ids"):
            self._initialize_runtime_state(playing)

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
            return bool(self._runtimes or self._gpu_controllers)
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

    def runtime_diagnostics(self) -> dict:
        """Return the on-demand particle control-plane state without GPU readback."""
        self._ensure_runtime_state()
        metadata = getattr(self, "_particle_metadata", None)
        targets = tuple(getattr(self, "_emitter_runtime_targets", ()))
        compatibility = tuple(
            getattr(self, "_emitter_reload_compatibility", ())
        )
        gpu = {
            index: (emitter_id, controller)
            for index, emitter_id, controller in zip(
                getattr(self, "_gpu_emitter_indices", ()),
                getattr(self, "_gpu_emitter_ids", ()),
                getattr(self, "_gpu_controllers", ()),
            )
        }
        cpu = {
            index: runtime
            for index, runtime in zip(
                getattr(self, "_cpu_emitter_indices", ()),
                getattr(self, "_runtimes", ()),
            )
        }
        native = self._native_engine()
        emitters = []
        for index, emitter in enumerate(
            getattr(metadata, "emitters", ()) if metadata is not None else ()
        ):
            target = targets[index] if index < len(targets) else None
            runtime = cpu.get(index)
            emitter_id = 0
            if index in gpu:
                emitter_id, runtime = gpu[index]
            item = {
                "index": index,
                "stable_id": emitter.stable_id,
                "name": str(getattr(emitter, "name", emitter.stable_id)),
                "target": target.value if target is not None else "",
                "playing": bool(getattr(runtime, "is_playing", False)),
                "simulation_step": int(getattr(runtime, "simulation_step", 0)),
                "reload_compatibility": (
                    compatibility[index].value
                    if index < len(compatibility)
                    and compatibility[index] is not None
                    else ""
                ),
            }
            if emitter_id:
                item["gpu_emitter_id"] = int(emitter_id)
                if native is not None:
                    item["artifact_revision"] = int(
                        native._gpu_particle_artifact_revision(emitter_id)
                    )
                    item["state_preserved"] = bool(
                        native._gpu_particle_state_was_preserved(emitter_id)
                    )
            emitters.append(item)

        event_abi_hash = 0
        event_domain_serial = 0
        if native is not None:
            if hasattr(native, "_gpu_particle_event_abi_hash"):
                event_abi_hash = int(
                    native._gpu_particle_event_abi_hash(self._batch_id)
                )
            if hasattr(native, "_gpu_particle_event_domain_serial"):
                event_domain_serial = int(
                    native._gpu_particle_event_domain_serial(self._batch_id)
                )
        return {
            "batch_id": int(self._batch_id),
            "playing": bool(self._playing),
            "artifact_revision": int(self._artifact_revision),
            "runtime_target": (
                self._runtime_target.value if self._runtime_target is not None else ""
            ),
            "event_abi_hash": event_abi_hash,
            "event_domain_serial": event_domain_serial,
            "last_compile_error": str(self._last_compile_error),
            "emitters": emitters,
        }

    def request_gpu_diagnostics(self) -> int:
        """Request one asynchronous GPU counter snapshot for this graph."""
        self._ensure_runtime_state()
        native = self._native_engine()
        if native is None or not hasattr(native, "_request_gpu_particle_diagnostics"):
            raise RuntimeError("GPU particle diagnostics are unavailable")
        request_id = int(native._request_gpu_particle_diagnostics(self._batch_id))
        if request_id <= 0:
            raise RuntimeError("GPU particle diagnostic request was rejected")
        self._gpu_diagnostic_requests.add(request_id)
        return request_id

    def poll_gpu_diagnostics(self, request_id: int) -> dict:
        """Poll a snapshot requested by :meth:`request_gpu_diagnostics`."""
        self._ensure_runtime_state()
        if type(request_id) is not int or request_id not in self._gpu_diagnostic_requests:
            raise ValueError("GPU particle diagnostic request does not belong to this component")
        native = self._native_engine()
        if native is None or not hasattr(native, "_poll_gpu_particle_diagnostics"):
            raise RuntimeError("GPU particle diagnostics are unavailable")
        result = dict(native._poll_gpu_particle_diagnostics(request_id))
        if int(result.get("graph_instance_id", 0)) not in {0, self._batch_id}:
            raise RuntimeError("GPU particle diagnostic response belongs to another graph")

        metadata = getattr(self, "_particle_metadata", None)
        emitter_names = tuple(
            emitter.stable_id for emitter in getattr(metadata, "emitters", ())
        )
        for emitter in result.get("emitters", ()):
            index = int(emitter.get("emitter_index", -1))
            emitter["stable_id"] = (
                emitter_names[index] if 0 <= index < len(emitter_names) else ""
            )

        kernel = getattr(self, "_particle_kernel", None)
        event_abi = getattr(kernel, "events", None)
        routes = tuple(getattr(event_abi, "routes", ()))
        event_types = {
            event_type.type_index: event_type
            for event_type in getattr(event_abi, "event_types", ())
        }
        for event in result.get("events", ()):
            channel = int(event.get("channel_index", -1))
            if not 0 <= channel < len(routes):
                continue
            route = routes[channel]
            event_type = event_types.get(route.event_type_index)
            event["route_stable_id"] = route.stable_id
            event["event_type_stable_id"] = (
                event_type.stable_id if event_type is not None else ""
            )
            event["source_emitter_stable_id"] = (
                emitter_names[route.source_emitter_index]
                if 0 <= route.source_emitter_index < len(emitter_names)
                else ""
            )
            event["target_emitter_stable_id"] = (
                emitter_names[route.target_emitter_index]
                if 0 <= route.target_emitter_index < len(emitter_names)
                else ""
            )
        return result

    def restart(self, emitter_index: int | None = None) -> bool:
        if emitter_index is None:
            self._playing = True
            for runtime in getattr(self, "_runtimes", ()):
                runtime.reset()
                runtime.play()
            for runtime in getattr(self, "_gpu_controllers", ()):
                runtime.reset(playing=True)
            self._reset_gpu_emitters()
            return bool(self._runtimes or self._gpu_controllers)
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

    def set_data_interface_asset(
        self,
        stable_id: str,
        *,
        guid: str = "",
        path_hint: str = "",
    ) -> bool:
        """Override one Data Interface asset for this ParticleSystem instance."""
        if type(stable_id) is not str or not stable_id.strip():
            return False
        try:
            reference = AssetReference(guid, path_hint)
        except (TypeError, ValueError):
            return False
        if not reference.guid and not reference.path_hint:
            return False

        stable_id = stable_id.strip()
        metadata = getattr(self, "_particle_metadata", None)
        if metadata is not None and not any(
            interface.stable_id == stable_id
            for emitter in metadata.emitters
            for interface in emitter.data_interfaces
        ):
            return False
        overrides = getattr(self, "_data_interface_overrides", None)
        if overrides is None:
            overrides = {}
            self._data_interface_overrides = overrides
        marker = object()
        previous = overrides.get(stable_id, marker)
        overrides[stable_id] = reference
        if self._has_runtime() and not self._compile_asset(force=True):
            if previous is marker:
                overrides.pop(stable_id, None)
            else:
                overrides[stable_id] = previous
            return False
        return True

    def clear_data_interface_asset(self, stable_id: str) -> bool:
        """Restore the ParticleGraph asset used by one Data Interface."""
        if type(stable_id) is not str:
            return False
        stable_id = stable_id.strip()
        overrides = getattr(self, "_data_interface_overrides", None)
        if not stable_id or not overrides or stable_id not in overrides:
            return False
        previous = overrides.pop(stable_id)
        if self._has_runtime() and not self._compile_asset(force=True):
            overrides[stable_id] = previous
            return False
        return True

    def data_interface_asset(self, stable_id: str) -> AssetReference | None:
        """Return this instance's override, or ``None`` when it uses the graph default."""
        if type(stable_id) is not str:
            return None
        return getattr(self, "_data_interface_overrides", {}).get(stable_id.strip())

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

    def editor_preview_begin(self) -> bool:
        """Prepare this component for Scene View simulation outside Play mode."""
        self._ensure_runtime_state(playing=True)
        self._editor_preview_active = True
        if not self._has_runtime() and not self._compile_asset():
            return False
        self.restart()
        return True

    def editor_preview_update(self, delta_time: float, speed: float = 1.0) -> bool:
        if not getattr(self, "_editor_preview_active", False):
            return False
        self.update(max(0.0, float(delta_time)) * max(0.0, float(speed)))
        return self._has_runtime()

    def editor_preview_play(self) -> bool:
        self._ensure_runtime_state(playing=True)
        self._editor_preview_active = True
        if not self._has_runtime() and not self._compile_asset():
            return False
        return self.play()

    def editor_preview_pause(self) -> bool:
        if not getattr(self, "_editor_preview_active", False):
            return False
        return self.pause()

    def editor_preview_stop(self) -> bool:
        self._ensure_runtime_state(playing=False)
        self._editor_preview_active = True
        had_runtime = self.stop()
        self._remove_native_batch()
        self._clear_runtime_state()
        self._playing = False
        self._editor_preview_active = True
        return had_runtime

    def editor_preview_end(self) -> None:
        if getattr(self, "_editor_preview_active", False):
            self.pause()
            self._editor_preview_active = False

    def on_disable(self):
        self._remove_native_batch()
        self._clear_runtime_state()

    def on_destroy(self):
        self._remove_native_batch()
        self._clear_runtime_state()

    def on_validate(self):
        self._remove_native_batch()
        self._clear_runtime_state()

    def _compile_asset(self, *, force: bool = False) -> bool:
        now = time.monotonic()
        if not force and now < getattr(self, "_compile_retry_at", 0.0):
            return False
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
            compiled = self._compile_particle_graph(graph_ref)
            if compiled:
                self._compile_retry_at = 0.0
                self._last_compile_error = ""
            elif self._compile_retry_at <= now:
                self._compile_retry_at = now + 1.0
            return compiled
        return False

    def _report_compile_failure(self, exc: Exception) -> None:
        now = time.monotonic()
        message = str(exc)
        self._compile_retry_at = now + 1.0
        if (
            message != getattr(self, "_last_compile_error", "")
            or now - getattr(self, "_last_compile_error_log_at", 0.0) >= 5.0
        ):
            Debug.log_error(f"[ParticleSystem] ParticleGraph compile failed: {message}")
            self._last_compile_error = message
            self._last_compile_error_log_at = now

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
            previous_kernel = getattr(self, "_particle_kernel", None)
            previous_targets = getattr(self, "_emitter_runtime_targets", ())
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
            reload_compatibility = [None] * len(metadata.emitters)
            if previous_metadata is not None and previous_kernel is not None:
                previous_emitters = {
                    emitter.stable_id: (
                        emitter,
                        kernel_emitter,
                        previous_targets[index]
                        if index < len(previous_targets)
                        else None,
                    )
                    for index, (emitter, kernel_emitter) in enumerate(
                        zip(previous_metadata.emitters, previous_kernel.emitters)
                    )
                }
                for emitter_index, (emitter, kernel_emitter) in enumerate(
                    zip(metadata.emitters, kernel.emitters)
                ):
                    previous = previous_emitters.get(emitter.stable_id)
                    if previous is None:
                        continue
                    previous_emitter, previous_kernel_emitter, previous_target = previous
                    compatibility = classify_emitter_update(
                        previous_kernel_emitter,
                        kernel_emitter,
                        previous_emitter.settings,
                        emitter.settings,
                    )
                    if previous_target is not targets[emitter_index]:
                        compatibility = ParticleRuntimeCompatibility.EMITTER_RESTART
                    reload_compatibility[emitter_index] = compatibility
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
                    point_cache_resolver=self._resolve_point_cache,
                    vector_field_resolver=self._resolve_vector_field,
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
            if any(target is ExecutionTarget.GPU for target in targets):
                self._publish_gpu_particle_graph(
                    artifact,
                    metadata,
                    kernel,
                    targets,
                    reload_compatibility,
                )
            else:
                self._remove_gpu_emitters()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._report_compile_failure(exc)
            return False

        self._remove_cpu_batches()
        self._particle_program = program
        self._particle_kernel = kernel
        self._particle_gpu_layouts = (
            tuple(artifact.gpu_glsl["emitters"])
            if artifact is not None
            else ()
        )
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

    def _select_runtime_targets(self, metadata, artifact) -> tuple[ExecutionTarget, ...]:
        native = self._native_engine()
        gpu_available = bool(
            artifact is not None
            and native is not None
            and hasattr(native, "_replace_gpu_particle_graph")
            and hasattr(native, "_begin_gpu_particle_batch")
        )
        targets = []
        for index, emitter in enumerate(metadata.emitters):
            declared = emitter.settings.target
            target = declared
            if target is ExecutionTarget.AUTO:
                target = ExecutionTarget.GPU if gpu_available else ExecutionTarget.CPU
            if target is ExecutionTarget.GPU and not gpu_available:
                raise RuntimeError(
                    f"emitter {index} ({emitter.stable_id}) requires GPU execution, "
                    "which needs a saved AOT artifact and graphical renderer"
                )
            targets.append(target)
        return tuple(targets)

    def _publish_gpu_particle_graph(
        self, artifact, metadata, kernel, targets, reload_compatibility
    ) -> None:
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
        previous_controllers = {}
        previous_layouts = {}
        previous_metadata = getattr(self, "_particle_metadata", None)
        if previous_metadata is not None:
            previous_controllers = {
                previous_metadata.emitters[emitter_index].stable_id: controller
                for emitter_index, controller in zip(
                    getattr(self, "_gpu_emitter_indices", ()),
                    getattr(self, "_gpu_controllers", ()),
                )
                if 0 <= emitter_index < len(previous_metadata.emitters)
            }
            previous_layouts = {
                emitter.stable_id: layout
                for emitter, layout in zip(
                    previous_metadata.emitters,
                    getattr(self, "_particle_gpu_layouts", ()),
                )
            }
        for index, (emitter, glsl_emitter) in enumerate(
            zip(metadata.emitters, glsl_emitters)
        ):
            if targets[index] is not ExecutionTarget.GPU:
                continue
            if any(output.output_type not in {"sprite", "mesh", "ribbon"} for output in emitter.outputs):
                raise RuntimeError("the GPU particle renderer received an unsupported output type")
            if (
                type(glsl_emitter) is not dict
                or glsl_emitter.get("stable_id") != emitter.stable_id
                or type(glsl_emitter.get("state_stride")) is not int
                or type(glsl_emitter.get("event_output_stages")) is not list
            ):
                raise RuntimeError("ParticleGraph GPU layout does not match its runtime schedule")
            decoded = decode_gpu_particle_spirv(artifact.gpu_spirv, index)
            emitter_id = self._gpu_emitter_id(emitter.stable_id)
            previous_controller = previous_controllers.get(emitter.stable_id)
            migration = None
            if (
                previous_controller is not None
                and reload_compatibility[index]
                is ParticleRuntimeCompatibility.LAYOUT_MIGRATABLE
            ):
                try:
                    migration = build_gpu_particle_migration(
                        previous_layouts[emitter.stable_id],
                        glsl_emitter,
                        kernel.emitters[index],
                    )
                except (KeyError, TypeError, ValueError):
                    reload_compatibility[index] = (
                        ParticleRuntimeCompatibility.EMITTER_RESTART
                    )
            preserve_state = (
                previous_controller is not None
                and reload_compatibility[index]
                in {
                    ParticleRuntimeCompatibility.PARAMETER_ONLY,
                    ParticleRuntimeCompatibility.KERNEL_COMPATIBLE,
                    ParticleRuntimeCompatibility.LAYOUT_MIGRATABLE,
                }
            )
            programs.append(
                {
                    "id": emitter_id,
                    "graph_instance_id": self._batch_id,
                    "graph_emitter_index": index,
                    "owner_object_id": int(self.game_object.id),
                    "owner_layer_mask": 1 << int(self.game_object.layer),
                    "artifact_revision": artifact.revision,
                    "stable_id": emitter.stable_id,
                    "capacity": emitter.settings.capacity,
                    "state_stride": glsl_emitter["state_stride"],
                    "event_output_stages": list(glsl_emitter["event_output_stages"]),
                    "preserve_state": preserve_state,
                    "migration": migration,
                    "data_interface_layout": self._gpu_data_interface_layout(
                        kernel.emitters[index], glsl_emitter
                    ),
                    "stages": decoded["stages"],
                    "billboard": decoded["billboard"],
                    "mesh_shaders": decoded["mesh"],
                    "outputs": [
                        {
                            "id": self._gpu_output_id(emitter.stable_id, output.output_id),
                            "stable_id": output.output_id,
                            "output_type": output.output_type,
                            "mesh": self._gpu_mesh_binding(output),
                            "material": self._gpu_material_binding(output),
                            "receive_scene_lighting": output.receive_scene_lighting,
                            "receive_shadows": output.receive_shadows,
                            "cast_shadows": output.cast_shadows,
                            "soft_particles": output.soft_particles,
                            "soft_distance": output.soft_distance,
                            "sort_mode": output.sort_mode,
                            "ribbon_uv_mode": output.ribbon_uv_mode,
                            "ribbon_uv_scale": output.ribbon_uv_scale,
                            "flipbook_columns": output.flipbook_columns,
                            "flipbook_rows": output.flipbook_rows,
                            "sprite_alignment": output.sprite_alignment,
                            "alignment_axis": list(output.alignment_axis),
                        }
                        for output in emitter.outputs
                    ],
                }
            )
            if preserve_state:
                controller = previous_controller.migrate_to(emitter.settings)
            else:
                controller = GpuParticleEmitterController(
                    emitter.settings,
                    playing=(
                        previous_controller.is_playing
                        if previous_controller is not None
                        else self._playing
                    ),
                )
            controllers.append(controller)
            emitter_ids.append(emitter_id)
            emitter_indices.append(index)

        removed = sorted(set(getattr(self, "_gpu_emitter_ids", ())) - set(emitter_ids))
        event_domain = None
        event_metadata = artifact.hir.get("events")
        if type(event_metadata) is not dict:
            raise RuntimeError("ParticleGraph event ABI metadata is missing")
        event_routes = event_metadata.get("routes")
        event_types = event_metadata.get("event_types")
        event_abi_hash = event_metadata.get("event_abi_hash")
        if (
            type(event_routes) is not list
            or type(event_types) is not list
            or type(event_abi_hash) is not str
        ):
            raise RuntimeError("ParticleGraph event ABI metadata is invalid")
        if event_routes:
            if any(target is not ExecutionTarget.GPU for target in targets):
                raise RuntimeError(
                    "graph-local particle events currently require every emitter in the graph to run on the GPU"
                )
            abi_u64 = int(event_abi_hash[:16], 16) or 1
            event_domain = {
                "event_abi_hash": abi_u64,
                "channels": [
                    {
                        "stable_event_type_hash": event_types[
                            route["event_type_index"]
                        ]["stable_type_hash"],
                        "source_emitter_index": route["source_emitter_index"],
                        "target_emitter_index": route["target_emitter_index"],
                        "event_type_index": route["event_type_index"],
                        "payload_stride_words": route["payload_stride_words"],
                        "capacity": route["capacity"],
                        "spawn_count": route["spawn_count"],
                    }
                    for route in event_routes
                ],
            }
        error = native._replace_gpu_particle_graph(
            self._batch_id,
            programs,
            removed,
            event_domain,
        )
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

        frame_items = []
        emitter_position = tuple(float(value) for value in emitter_to_world[0:3, 3])
        for emitter_id, emitter_index, controller in zip(
            self._gpu_emitter_ids,
            self._gpu_emitter_indices,
            self._gpu_controllers,
        ):
            emitter = metadata.emitters[emitter_index]
            schedule = controller.tick(delta_time, emitter_position)
            transforms = self._gpu_transform_buffer(
                emitter.settings.simulation_space.value == "local"
            )
            frame_items.append(
                {
                    "emitter_id": emitter_id,
                    "spawn_count": schedule.spawn_count,
                    "spawn_base_id": schedule.spawn_base_id,
                    "spawn_generation": schedule.spawn_generation,
                    "system_seed": schedule.system_seed,
                    "simulation_step": schedule.simulation_step,
                    "delta_time": schedule.delta_time,
                    "transforms": transforms,
                    "simulate": schedule.simulate,
                    "render": schedule.render,
                }
            )
        if frame_items:
            native._begin_gpu_particle_batch(self._batch_id, frame_items)

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
            instances = runtime.tick(
                delta_time,
                tuple(float(value) for value in emitter_to_world[0:3, 3]),
            )
            if native is None:
                continue
            if emitter.settings.simulation_space.value == "local" and len(instances):
                instances = self._local_instances_to_world(instances, emitter_to_world)
            for output_index, output in enumerate(emitter.outputs):
                batch_id = self._output_batch_id(emitter_index, output_index)
                native.submit_particle_instances(
                    batch_id,
                    instances,
                    self._output_material_guid(output.material),
                    validate=False,
                    owner_object_id=int(self.game_object.id),
                )
                self._submitted_batch_ids.add(batch_id)

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
        )

    def _emitter_matrix(self) -> np.ndarray:
        flat = self.transform.local_to_world_matrix()
        return np.asarray(flat, dtype=np.float32).reshape((4, 4), order="F")

    @staticmethod
    def _local_instances_to_world(instances: np.ndarray, emitter_to_world: np.ndarray) -> np.ndarray:
        result = np.array(instances, dtype=np.float32, copy=True, order="C")
        positions = result[:, 0:3]
        linear = emitter_to_world[0:3, 0:3]
        translation = emitter_to_world[0:3, 3]
        result[:, 0:3] = positions @ linear.T + translation
        axis_scales = np.linalg.norm(linear, axis=0)
        if axis_scales.size:
            result[:, 9:12] *= axis_scales
        return result

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
        is_mesh = output.output_type == "mesh"
        state: dict[str, object] = {
            "render_queue": 2000 if is_mesh else 3000,
            "blend_enabled": not is_mesh,
            "depth_test_enabled": True,
            "depth_write_enabled": is_mesh,
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
        if not path and not is_mesh:
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
        if not is_mesh and bool(getattr(output, "soft_particles", False)):
            from Infernux.lib import EngineConfig

            state.update(
                render_queue=max(
                    int(state["render_queue"]),
                    int(EngineConfig.get().transparent_queue_min),
                ),
                blend_enabled=True,
                depth_write_enabled=False,
            )
        return state

    @classmethod
    def _gpu_mesh_binding(cls, output) -> object:
        if output.output_type != "mesh":
            return None
        from Infernux.lib import AssetRegistry

        reference = output.mesh
        registry = AssetRegistry.instance()
        native = registry.load_mesh_by_guid(reference.guid) if reference.guid else None
        path = cls._absolute_project_path(reference.path_hint)
        if native is None and path:
            native = registry.load_mesh(path)
        if native is None:
            identity = reference.guid or reference.path_hint or "<empty reference>"
            raise RuntimeError(f"ParticleGraph Mesh Output cannot load {identity!r}")
        return native

    def _gpu_data_interface_layout(self, emitter, glsl_emitter) -> dict[str, object]:
        layout = glsl_emitter.get("data_interface_layout")
        if type(layout) is not dict:
            raise RuntimeError("ParticleGraph GPU data interface layout is missing")
        point_cache_layouts = layout.get("point_caches")
        if type(point_cache_layouts) is not list:
            raise RuntimeError("ParticleGraph GPU Point Cache layout is invalid")
        volume_layouts = layout.get("volume_interfaces")
        if type(volume_layouts) is not list:
            raise RuntimeError("ParticleGraph GPU volume-interface layout is invalid")
        if not point_cache_layouts and not volume_layouts:
            return dict(layout)

        interfaces = {
            interface.stable_id: interface
            for interface in emitter.data_interfaces
            if isinstance(interface, PointCache)
        }
        from Infernux.lib import AssetRegistry

        registry = AssetRegistry.instance()
        decoded_point_caches = []
        for encoded in point_cache_layouts:
            if type(encoded) is not dict:
                raise RuntimeError("ParticleGraph GPU Point Cache layout is invalid")
            stable_id = encoded.get("stable_id")
            interface = interfaces.get(stable_id)
            if interface is None:
                raise RuntimeError(
                    f"ParticleGraph GPU Point Cache interface {stable_id!r} is missing"
                )
            reference = self._data_interface_reference(interface)
            native = (
                registry.load_point_cache_by_guid(reference.guid)
                if reference.guid
                else None
            )
            path = reference.path_hint
            if native is None and path:
                if not os.path.isabs(path):
                    try:
                        from Infernux.engine.project_context import get_project_root

                        project_root = get_project_root()
                        if project_root:
                            path = os.path.join(project_root, path)
                    except (AttributeError, RuntimeError):
                        pass
                native = registry.load_point_cache(path)
            if native is None:
                identity = reference.guid or reference.path_hint or "<empty reference>"
                raise RuntimeError(
                    f"ParticleGraph GPU Point Cache {stable_id!r} cannot load {identity!r}"
                )

            aliases = {
                "$position": interface.position_channel,
                "$normal": interface.normal_channel,
                "$color": interface.color_channel,
                "$id": interface.id_channel,
            }
            samples = []
            for sample in encoded.get("samples", ()):
                if type(sample) is not dict:
                    raise RuntimeError("ParticleGraph GPU Point Cache sample is invalid")
                decoded_sample = dict(sample)
                decoded_sample["channel"] = aliases.get(
                    decoded_sample.get("channel"), decoded_sample.get("channel")
                )
                if not decoded_sample["channel"]:
                    raise RuntimeError(
                        f"ParticleGraph GPU Point Cache {stable_id!r} resolves an empty channel"
                    )
                samples.append(decoded_sample)
            decoded = dict(encoded)
            decoded.update(
                space=interface.space.value,
                cache_to_space=list(interface.cache_to_space),
                native=native,
                samples=samples,
            )
            decoded_point_caches.append(decoded)

        result = dict(layout)
        result["point_caches"] = decoded_point_caches
        volumes = {
            interface.stable_id: interface
            for interface in emitter.data_interfaces
            if isinstance(interface, (VectorField, SdfVolume))
        }
        decoded_volumes = []
        for encoded in volume_layouts:
            if type(encoded) is not dict:
                raise RuntimeError("ParticleGraph GPU volume-interface layout is invalid")
            stable_id = encoded.get("stable_id")
            interface = volumes.get(stable_id)
            if interface is None:
                raise RuntimeError(
                    f"ParticleGraph GPU volume interface {stable_id!r} is missing"
                )
            reference = self._data_interface_reference(interface)
            if not reference.guid:
                identity = reference.path_hint or "<empty reference>"
                raise RuntimeError(
                    f"ParticleGraph GPU volume interface {stable_id!r} requires an imported texture GUID; got {identity!r}"
                )
            native = registry.load_texture_by_guid(reference.guid)
            if native is None:
                raise RuntimeError(
                    f"ParticleGraph GPU volume interface {stable_id!r} cannot load {reference.guid!r}"
                )
            expected_semantic = (
                "vector_field" if isinstance(interface, VectorField) else "signed_distance_field"
            )
            if native.dimension != "3d" or native.semantic != expected_semantic:
                raise RuntimeError(
                    f"ParticleGraph GPU volume interface {stable_id!r} requires a {expected_semantic} Texture3D"
                )
            field_to_space = np.asarray(interface.field_to_space, dtype=np.float32).reshape(4, 4)
            bake_basis = np.asarray(native.bake_basis, dtype=np.float32).reshape(4, 4)
            decoded = dict(encoded)
            decoded.update(
                texture_guid=reference.guid,
                space=interface.space.value,
                field_to_space=(field_to_space @ bake_basis).reshape(-1).tolist(),
                filtering=interface.filtering.value,
                native=native,
            )
            if isinstance(interface, VectorField):
                decoded.update(
                    vector_scale=interface.vector_scale,
                    boundary=interface.boundary.value,
                )
            else:
                decoded.update(distance_scale=interface.distance_scale)
            decoded_volumes.append(decoded)
        result["volume_interfaces"] = decoded_volumes
        return result

    def _data_interface_reference(self, interface) -> AssetReference:
        override = getattr(self, "_data_interface_overrides", {}).get(
            interface.stable_id
        )
        if override is not None:
            return override
        return interface.cache if isinstance(interface, PointCache) else interface.texture

    def _resolve_point_cache(self, interface: PointCache):
        from Infernux.lib import AssetRegistry

        reference = self._data_interface_reference(interface)
        registry = AssetRegistry.instance()
        native = (
            registry.load_point_cache_by_guid(reference.guid)
            if reference.guid
            else None
        )
        path = self._absolute_project_path(reference.path_hint)
        if native is None and path:
            native = registry.load_point_cache(path)
        if native is None:
            identity = reference.guid or reference.path_hint or "<empty reference>"
            raise RuntimeError(
                f"ParticleGraph Point Cache {interface.stable_id!r} cannot load {identity!r}"
            )
        return native

    def _resolve_vector_field(self, interface: VectorField):
        from Infernux.lib import AssetRegistry

        reference = self._data_interface_reference(interface)
        if not reference.guid:
            identity = reference.path_hint or "<empty reference>"
            raise RuntimeError(
                f"ParticleGraph Vector Field {interface.stable_id!r} requires an imported texture GUID; got {identity!r}"
            )
        native = AssetRegistry.instance().load_texture_by_guid(reference.guid)
        if native is None or native.dimension != "3d" or native.semantic != "vector_field":
            raise RuntimeError(
                f"ParticleGraph Vector Field {interface.stable_id!r} cannot load a VectorField Texture3D from {reference.guid!r}"
            )
        return native

    @staticmethod
    def _absolute_project_path(path: str) -> str:
        if not path or os.path.isabs(path):
            return path
        try:
            from Infernux.engine.project_context import get_project_root

            project_root = get_project_root()
            return os.path.join(project_root, path) if project_root else path
        except (AttributeError, RuntimeError):
            return path

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
        emitter_ids = list(getattr(self, "_gpu_emitter_ids", ()))
        if (
            emitter_ids
            and native is not None
            and hasattr(native, "_replace_gpu_particle_graph")
        ):
            native._replace_gpu_particle_graph(self._batch_id, [], emitter_ids, None)
        self._gpu_emitter_ids = []
        self._gpu_emitter_indices = []
        self._gpu_controllers = []

    def _remove_cpu_batches(self) -> None:
        native = self._native_engine()
        batch_ids = set(getattr(self, "_submitted_batch_ids", set()))
        if native is not None:
            for batch_id in batch_ids:
                native.remove_particle_batch(batch_id)
        self._submitted_batch_ids = set()

    def _clear_runtime_state(self) -> None:
        self._runtimes = []
        self._cpu_emitter_indices = []
        self._gpu_controllers = []
        self._gpu_emitter_ids = []
        self._gpu_emitter_indices = []
        self._emitter_runtime_targets = ()
        self._emitter_reload_compatibility = ()
        self._runtime_target = None
        self._particle_program = None
        self._particle_kernel = None
        self._particle_gpu_layouts = ()
        self._particle_metadata = None
        self._artifact_revision = 0
        self._artifact_source_key = ""
        self._emitter_to_world_cache = None
        self._gpu_transform_buffers = {}
        self._compile_retry_at = 0.0
        self._last_compile_error = ""
        self._last_compile_error_log_at = 0.0

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
