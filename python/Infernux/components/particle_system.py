"""ParticleGraph runtime component."""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import os
import struct
import threading
import time
from enum import Enum
from typing import Optional

import numpy as np

from Infernux.application import Application
from Infernux.core.asset_ref import ParticleGraphRef
from Infernux.debug import Debug
from Infernux.graph import (
    AssetReference,
    CoordinateSpace,
    ValueType,
    builtin_mesh_name,
)
from Infernux.graph.ramp import Curve, Gradient
from Infernux.particle import (
    GpuParticleEmitterController,
    EmitterShapeKind,
    ParticleArtifactRegistry,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleKernelLowerer,
    ParticleKernelProgram,
    ParticleRuntimeCompatibility,
    SdfVolume,
    VectorField,
    build_gpu_particle_migration,
    classify_emitter_update,
    decode_gpu_particle_spirv,
    decode_particle_runtime_metadata,
    pack_gpu_particle_parameters,
)
from Infernux.particle.data_interface import MeshResourceBinding
from Infernux.gizmos.gizmos import ICON_KIND_PARTICLE
from Infernux.lib import Vector3
from .component import InxComponent
from .decorators import add_component_menu, disallow_multiple
from .serialized_field import get_raw_field_value, serialized_field
from .value_document import is_component_ref_document


_RUNTIME_BATCH_IDS = itertools.count(1)
_RUNTIME_BATCH_ID_LOCK = threading.Lock()


def _is_skinned_mesh_source_document(value) -> bool:
    return is_component_ref_document(value, "SkinnedMeshRenderer")


def _normalize_mesh_source_value(value, parameter_name: str) -> dict:
    """Normalize one Mesh value without turning it into particle state.

    Mesh parameters intentionally accept either an asset reference or a live
    SkinnedMeshRenderer reference. Both are graph-level values consumed by
    Sample Mesh; neither form is a per-particle attribute.
    """
    from Infernux.components.ref_wrappers import ComponentRef

    if isinstance(value, ComponentRef):
        if value.component_type != "SkinnedMeshRenderer":
            raise TypeError(
                f"particle Mesh parameter {parameter_name!r} requires a Mesh asset "
                "or SkinnedMeshRenderer"
            )
        return value._serialize()
    if _is_skinned_mesh_source_document(value):
        return dict(value)
    if isinstance(value, AssetReference):
        return value.to_dict()
    if type(value) is dict:
        try:
            return AssetReference.from_dict(value).to_dict()
        except (TypeError, ValueError) as exc:
            raise TypeError(
                f"particle Mesh parameter {parameter_name!r} requires a Mesh asset "
                "or SkinnedMeshRenderer"
            ) from exc
    raise TypeError(
        f"particle Mesh parameter {parameter_name!r} requires a Mesh asset "
        "or SkinnedMeshRenderer"
    )


def _resolve_skinned_mesh_source(value, purpose: str):
    if not _is_skinned_mesh_source_document(value):
        return None
    from Infernux.components.ref_wrappers import ComponentRef

    reference = ComponentRef._from_dict(value)
    component = reference.resolve()
    if component is None:
        raise RuntimeError(f"{purpose} cannot resolve its SkinnedMeshRenderer")
    native_getter = getattr(component, "_get_bound_native_component", None)
    native = native_getter() if callable(native_getter) else component
    if native is None or type(native).__name__ != "SkinnedMeshRenderer":
        raise RuntimeError(f"{purpose} requires a live SkinnedMeshRenderer")
    return native


def _allocate_runtime_batch_id() -> int:
    """Return a process-unique native graph identity.

    Scene object/component IDs are local to a serialized scene and can be
    reused while the previous scene is still being retired. Native particle
    lifetime therefore needs a separate process identity.
    """
    with _RUNTIME_BATCH_ID_LOCK:
        return next(_RUNTIME_BATCH_IDS)


class ParticleBoundsMode(str, Enum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"


class ParticleOffscreenPolicy(str, Enum):
    ALWAYS_SIMULATE = "always_simulate"
    PAUSE_WHEN_OFFSCREEN = "pause_when_offscreen"


def _decode_gpu_state_value(raw: bytes, offset: int, value_type: ValueType):
    if value_type is ValueType.BOOL:
        return bool(struct.unpack_from("<I", raw, offset)[0])
    if value_type is ValueType.I32:
        return struct.unpack_from("<i", raw, offset)[0]
    if value_type is ValueType.U32:
        return struct.unpack_from("<I", raw, offset)[0]
    if value_type is ValueType.F32:
        return struct.unpack_from("<f", raw, offset)[0]
    component_count = {
        ValueType.VEC2: 2,
        ValueType.VEC3: 3,
        ValueType.VEC4: 4,
        ValueType.COLOR: 4,
    }.get(value_type)
    if component_count is not None:
        return tuple(struct.unpack_from(f"<{component_count}f", raw, offset))
    if value_type is ValueType.MAT3:
        return tuple(
            component
            for column in range(3)
            for component in struct.unpack_from("<3f", raw, offset + column * 16)
        )
    if value_type is ValueType.MAT4:
        return tuple(struct.unpack_from("<16f", raw, offset))
    raise RuntimeError(
        f"GPU particle state sample cannot decode {value_type.value!r} attributes"
    )


@disallow_multiple
@add_component_menu("VFX/Particle System")
class ParticleSystem(InxComponent):
    _display_name_key = "component.particle_system"
    _PREROLL_STEP_SECONDS = 1.0 / 60.0
    _MAX_PREROLL_STEPS = 4096

    # Scene icon: particle burst billboard at the system origin, so an emitter
    # stays selectable even when it is not currently emitting anything.
    _gizmo_icon_color = (0.62, 0.82, 1.0)
    _gizmo_icon_kind = ICON_KIND_PARTICLE
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
    random_seed: int = serialized_field(
        default=0,
        range=(0, 2147483647),
        display_name_key="particle_system.random_seed",
        tooltip="Fixed scene-instance seed. Emitter seeds remain part of the ParticleGraph asset.",
    )
    prewarm: bool = serialized_field(
        default=False,
        display_name_key="particle_system.prewarm",
        tooltip="Simulate one complete loop on the GPU before the first visible frame.",
    )
    offscreen_policy: ParticleOffscreenPolicy = serialized_field(
        default=ParticleOffscreenPolicy.ALWAYS_SIMULATE,
        display_name_key="particle_system.offscreen_policy",
        enum_labels=[
            "particle_system.offscreen_policy.always_simulate",
            "particle_system.offscreen_policy.pause_when_offscreen",
        ],
        tooltip="Always Simulate preserves gameplay timing. Pause When Offscreen skips GPU simulation when no camera sees the system.",
    )
    bounds_mode: ParticleBoundsMode = serialized_field(
        default=ParticleBoundsMode.AUTOMATIC,
        display_name_key="particle_system.bounds_mode",
        enum_labels=[
            "particle_system.bounds_mode.automatic",
            "particle_system.bounds_mode.manual",
        ],
        tooltip="Automatic reduces live GPU particles; Manual uses the local bounds below.",
    )
    manual_bounds_center: Vector3 = serialized_field(
        default=Vector3(0.0, 0.0, 0.0),
        display_name_key="particle_system.manual_bounds_center",
    )
    manual_bounds_size: Vector3 = serialized_field(
        default=Vector3(10.0, 10.0, 10.0),
        display_name_key="particle_system.manual_bounds_size",
        tooltip="Local-space full size. Negative components are treated as their absolute size.",
    )
    _parameter_overrides_json: str = serialized_field(default="{}", hidden=True)
    _emitter_overrides_json: str = serialized_field(default="{}", hidden=True)

    _gpu_controllers: list[GpuParticleEmitterController]
    _gpu_emitter_ids: list[int]
    _gpu_emitter_indices: list[int]
    _emitter_reload_compatibility: tuple[ParticleRuntimeCompatibility | None, ...]
    _particle_kernel = None
    _particle_gpu_layouts: tuple[dict, ...]
    _particle_metadata = None
    _particle_event_types: tuple[dict, ...]
    _artifact_revision: int = 0
    _artifact_registry_revision: int = 0
    _artifact_source_key: str = ""
    _graph_simulation_time_ticks: int = 0
    _emitter_to_world_cache: Optional[np.ndarray] = None
    _gpu_transform_buffers: dict[bool, np.ndarray]
    _batch_id: int = 0
    _gpu_diagnostic_requests: set[int]
    _gpu_view_diagnostic_requests: set[tuple[str, int, int]]
    _parameter_overrides: dict[str, object]
    _emitter_overrides: dict[str, dict[str, bool]]
    _prewarm_pending_emitters: set[str]
    _pending_seek_seconds: dict[str, float]
    _serialized_parameter_overrides_cache: str
    _serialized_emitter_overrides_cache: str
    _playing: bool = False
    _editor_preview_active: bool = False
    _editor_preview_play_requested: bool = True
    _editor_preview_muted_emitters: set[str]
    _editor_preview_solo_emitters: set[str]
    _emitter_mesh_gizmo_bounds: dict[str, tuple[float, ...]]
    _gpu_update_dirty: bool = True
    _compile_retry_at: float = 0.0
    _last_compile_error: str = ""
    _last_compile_error_log_at: float = 0.0
    _INSTANCE_OVERRIDE_FIELDS = frozenset(
        {"_parameter_overrides_json", "_emitter_overrides_json"}
    )

    def __setattr__(self, name, value):
        # Inspector/undo writes go through the serialized descriptor.  Mark
        # only the two instance-policy documents dirty here so update() can
        # skip two descriptor reads and JSON comparisons on stable frames.
        if name in type(self)._INSTANCE_OVERRIDE_FIELDS:
            self.__dict__["_instance_overrides_dirty"] = True
        super().__setattr__(name, value)
    _runtime_definition_signature: tuple
    _runtime_rebuild_pending: bool = False
    _output_materials: dict[tuple[str, str], object]

    def awake(self):
        if hasattr(self, "_gpu_controllers"):
            self._remove_native_batch()
        self._initialize_runtime_state(bool(self.play_on_awake))

    def _initialize_runtime_state(self, playing: bool) -> None:
        self._gpu_controllers = []
        self._gpu_emitter_ids = []
        self._gpu_emitter_indices = []
        self._emitter_reload_compatibility = ()
        self._particle_kernel = None
        self._particle_gpu_layouts = ()
        self._particle_metadata = None
        self._particle_event_types = ()
        self._artifact_revision = 0
        self._artifact_registry_revision = int(
            getattr(ParticleArtifactRegistry, "_revision", 0)
        )
        self._artifact_source_key = ""
        self._graph_simulation_time_ticks = 0
        self._emitter_to_world_cache = None
        self._gpu_transform_buffers = {}
        self._gpu_diagnostic_requests = set()
        self._gpu_view_diagnostic_requests = set()
        self._serialized_parameter_overrides_cache = str(
            get_raw_field_value(self, "_parameter_overrides_json") or "{}"
        )
        self._serialized_emitter_overrides_cache = str(
            get_raw_field_value(self, "_emitter_overrides_json") or "{}"
        )
        self._instance_overrides_dirty = False
        self._parameter_overrides = self._decode_parameter_overrides()
        self._emitter_overrides = self._decode_emitter_overrides()
        self._prewarm_pending_emitters = set()
        self._pending_seek_seconds = {}
        self._batch_id = _allocate_runtime_batch_id()
        self._playing = bool(playing)
        self._editor_preview_active = False
        self._editor_preview_play_requested = True
        self._editor_preview_muted_emitters = set()
        self._editor_preview_solo_emitters = set()
        self._emitter_mesh_gizmo_bounds = {}
        self._gpu_update_dirty = True
        self._compile_retry_at = 0.0
        self._last_compile_error = ""
        self._last_compile_error_log_at = 0.0
        self._runtime_definition_signature = self._definition_signature()
        self._runtime_rebuild_pending = False
        self._output_materials = {}

    def _ensure_runtime_state(self, *, playing: bool = False) -> None:
        if not hasattr(self, "_gpu_controllers"):
            self._initialize_runtime_state(playing)
            return

        # Inactive components may be deserialized without receiving awake().
        # Editor preview and diagnostics can still touch them, so the native
        # graph identity is part of the runtime-state invariant, not merely an
        # awake-time detail.
        if int(getattr(self, "_batch_id", 0)) <= 0:
            if getattr(self, "_gpu_controllers", ()) or getattr(
                self, "_gpu_emitter_ids", ()
            ):
                raise RuntimeError(
                    "ParticleSystem has resident GPU state without a valid graph identity"
                )
            self._initialize_runtime_state(
                bool(playing or getattr(self, "_playing", False))
            )
            return

        # Undo/redo restores serialized fields on the existing component. Older
        # or partially initialized instances can therefore retain native GPU
        # state while missing decoded authoring caches. Repair those caches
        # without destroying the live particle graph.
        if not hasattr(self, "_serialized_parameter_overrides_cache"):
            self._serialized_parameter_overrides_cache = str(
                get_raw_field_value(self, "_parameter_overrides_json") or "{}"
            )
            self._instance_overrides_dirty = True
        if not hasattr(self, "_serialized_emitter_overrides_cache"):
            self._serialized_emitter_overrides_cache = str(
                get_raw_field_value(self, "_emitter_overrides_json") or "{}"
            )
            self._instance_overrides_dirty = True
        if not hasattr(self, "_parameter_overrides"):
            self._parameter_overrides = self._decode_parameter_overrides()
        if not hasattr(self, "_emitter_overrides"):
            self._emitter_overrides = self._decode_emitter_overrides()
        if not hasattr(self, "_output_materials"):
            self._output_materials = {}
        if not hasattr(self, "_gpu_update_dirty"):
            self._gpu_update_dirty = True

    def _deserialized_play_state(self) -> bool:
        """Resolve the initial runtime state for a freshly restored instance."""
        playing = bool(getattr(self, "_playing", False))
        if int(getattr(self, "_batch_id", 0)) > 0:
            return playing
        try:
            from Infernux.engine.play_mode import PlayModeManager

            manager = PlayModeManager.instance()
            if manager is not None and manager.is_playing:
                return bool(get_raw_field_value(self, "play_on_awake"))
        except (ImportError, AttributeError, ReferenceError, RuntimeError):
            pass
        return playing

    def on_after_deserialize(self) -> None:
        """Reconcile serialized authoring state after load, undo, or redo."""
        self._ensure_runtime_state(playing=self._deserialized_play_state())
        self._sync_serialized_instance_overrides()
        self.on_validate()

    def _detach_native_binding_for_replacement(self) -> None:
        """Retire native GPU state before replacing the Python component mirror."""
        if hasattr(self, "_gpu_controllers"):
            self._remove_native_batch()
            self._clear_runtime_state()
        super()._detach_native_binding_for_replacement()

    def start(self):
        if not self._has_runtime():
            self._load_saved_artifact()

    def on_enable(self):
        if hasattr(self, "_gpu_controllers") and not self._has_runtime():
            self._load_saved_artifact()

    def play(self, emitter: int | str | None = None) -> bool:
        if not self._has_runtime() and not self._load_saved_artifact(force=True):
            return False
        if emitter is None:
            self._playing = True
            self._gpu_update_dirty = True
            runtimes = tuple(getattr(self, "_gpu_controllers", ()))
            for emitter_index, runtime in zip(
                getattr(self, "_gpu_emitter_indices", ()), runtimes
            ):
                runtime.play()
                self._set_gpu_emitter_playing(emitter_index, True)
            return bool(runtimes)
        emitter_index = self._resolve_emitter_index(emitter)
        if emitter_index is None:
            return False
        runtime = self._runtime_at(emitter_index)
        if runtime is None:
            return False
        self._playing = True
        self._gpu_update_dirty = True
        runtime.play()
        self._set_gpu_emitter_playing(emitter_index, True)
        return True

    def pause(self, emitter: int | str | None = None) -> bool:
        if emitter is None:
            self._playing = False
            self._gpu_update_dirty = True
            runtimes = tuple(getattr(self, "_gpu_controllers", ()))
            for emitter_index, runtime in zip(
                getattr(self, "_gpu_emitter_indices", ()), runtimes
            ):
                runtime.pause()
                self._set_gpu_emitter_playing(emitter_index, False)
            return bool(runtimes)
        emitter_index = self._resolve_emitter_index(emitter)
        if emitter_index is None:
            return False
        runtime = self._runtime_at(emitter_index)
        if runtime is None:
            return False
        runtime.pause()
        self._gpu_update_dirty = True
        self._set_gpu_emitter_playing(emitter_index, False)
        return True

    def stop(self, emitter: int | str | None = None) -> bool:
        if emitter is None:
            self._playing = False
            self._gpu_update_dirty = True
            self._graph_simulation_time_ticks = 0
            self._prewarm_pending_emitters.clear()
            self._pending_seek_seconds.clear()
            for emitter_index, runtime in zip(
                getattr(self, "_gpu_emitter_indices", ()),
                getattr(self, "_gpu_controllers", ()),
            ):
                runtime.reset(playing=False)
                self._set_gpu_emitter_playing(emitter_index, False)
            self._reset_gpu_emitters()
            return bool(self._gpu_controllers)
        emitter_index = self._resolve_emitter_index(emitter)
        if emitter_index is None:
            return False
        runtime = self._runtime_at(emitter_index)
        if runtime is None:
            return False
        runtime.reset(playing=False)
        self._gpu_update_dirty = True
        self._set_gpu_emitter_playing(emitter_index, False)
        stable_id = self._emitter_stable_id(emitter_index)
        self._prewarm_pending_emitters.discard(stable_id)
        self._pending_seek_seconds.pop(stable_id, None)
        self._reset_gpu_emitters(emitter_index)
        return True

    def has_parameter(self, name: str) -> bool:
        return self._find_exposed_parameter(name, compile_if_needed=True) is not None

    def exposed_parameter_schema(self) -> list[dict]:
        """Return the current instance-editable ParticleGraph parameter schema."""
        self._sync_serialized_instance_overrides()
        if getattr(self, "_particle_metadata", None) is None:
            self._load_saved_artifact()
        metadata = getattr(self, "_particle_metadata", None)
        result = []
        for parameter in getattr(metadata, "parameters", ()):
            if not parameter.exposed:
                continue
            value = self._parameter_overrides.get(
                parameter.stable_id, parameter.default
            )
            result.append(
                {
                    "stable_id": parameter.stable_id,
                    "name": parameter.name,
                    "type": parameter.value_type.value_type.value,
                    "default": copy.deepcopy(parameter.default),
                    "value": copy.deepcopy(value),
                    "category": parameter.category,
                    "tooltip": parameter.tooltip,
                }
            )
        return result

    def emitter_instance_schema(self) -> list[dict]:
        """Return per-component playback controls for every graph emitter."""
        self._sync_serialized_instance_overrides()
        if getattr(self, "_particle_metadata", None) is None:
            self._load_saved_artifact()
        metadata = getattr(self, "_particle_metadata", None)
        result = []
        for index, emitter in enumerate(getattr(metadata, "emitters", ())):
            options = self._emitter_instance_options(emitter.stable_id)
            result.append(
                {
                    "index": index,
                    "stable_id": emitter.stable_id,
                    "name": str(getattr(emitter, "name", emitter.stable_id)),
                    "enabled": options["enabled"],
                    "play_on_start": options["play_on_start"],
                }
            )
        return result

    def set_emitter_options(
        self,
        emitter: int | str,
        *,
        enabled: bool | None = None,
        play_on_start: bool | None = None,
    ) -> bool:
        """Set scene-instance emitter policy without modifying the graph asset."""
        if enabled is not None and type(enabled) is not bool:
            raise TypeError("particle emitter enabled must be a boolean")
        if play_on_start is not None and type(play_on_start) is not bool:
            raise TypeError("particle emitter play_on_start must be a boolean")
        self._sync_serialized_instance_overrides()
        emitter_index = self._resolve_emitter_index(emitter)
        if emitter_index is None:
            return False
        stable_id = self._emitter_stable_id(emitter_index)
        previous = self._emitter_instance_options(stable_id)
        updated = {
            "enabled": previous["enabled"] if enabled is None else enabled,
            "play_on_start": (
                previous["play_on_start"]
                if play_on_start is None
                else play_on_start
            ),
        }
        if updated == previous:
            return False
        self._emitter_overrides[stable_id] = updated
        self._store_emitter_overrides()
        self._apply_emitter_instance_options(emitter_index)
        self._gpu_update_dirty = True
        return True

    def set_parameter(self, name: str, value) -> None:
        parameter = self._require_exposed_parameter(name)
        normalized = self._normalize_parameter_value(parameter, value)
        current = self._parameter_overrides.get(parameter.stable_id, parameter.default)
        if current == normalized:
            return
        marker = object()
        previous = self._parameter_overrides.get(parameter.stable_id, marker)
        self._parameter_overrides[parameter.stable_id] = normalized
        if (
            parameter.value_type.value_type in {ValueType.TEXTURE2D, ValueType.MESH}
            and self._has_runtime()
            and not self._load_saved_artifact(force=True)
        ):
            if previous is marker:
                self._parameter_overrides.pop(parameter.stable_id, None)
            else:
                self._parameter_overrides[parameter.stable_id] = previous
            raise RuntimeError(
                f"particle resource parameter {parameter.name!r} could not rebuild the GPU binding"
            )
        self._store_parameter_overrides()
        self._gpu_update_dirty = True
        if parameter.value_type.value_type not in {ValueType.TEXTURE2D, ValueType.MESH}:
            self._upload_parameter_overrides()

    def get_parameter(self, name: str):
        parameter = self._require_exposed_parameter(name)
        value = self._parameter_overrides.get(parameter.stable_id, parameter.default)
        return copy.deepcopy(value)

    def reset_parameter(self, name: str) -> bool:
        parameter = self._require_exposed_parameter(name)
        if parameter.stable_id not in self._parameter_overrides:
            return False
        previous = self._parameter_overrides.pop(parameter.stable_id)
        if (
            parameter.value_type.value_type in {ValueType.TEXTURE2D, ValueType.MESH}
            and self._has_runtime()
            and not self._load_saved_artifact(force=True)
        ):
            self._parameter_overrides[parameter.stable_id] = previous
            return False
        self._store_parameter_overrides()
        self._gpu_update_dirty = True
        if parameter.value_type.value_type not in {ValueType.TEXTURE2D, ValueType.MESH}:
            self._upload_parameter_overrides()
        return True

    def set_texture(self, name: str, value) -> None:
        self._set_typed_parameter(name, value, ValueType.TEXTURE2D)

    def get_texture(self, name: str) -> AssetReference:
        value = self._get_typed_parameter(
            name, ValueType.TEXTURE2D, AssetReference().to_dict()
        )
        return AssetReference.from_dict(value)

    def set_curve(self, name: str, value: Curve | dict) -> None:
        self._set_typed_parameter(name, value, ValueType.CURVE)

    def get_curve(self, name: str) -> Curve:
        value = self._get_typed_parameter(
            name, ValueType.CURVE, Curve().to_dict()
        )
        return Curve.from_dict(value)

    def set_gradient(self, name: str, value: Gradient | dict) -> None:
        self._set_typed_parameter(name, value, ValueType.GRADIENT)

    def get_gradient(self, name: str) -> Gradient:
        value = self._get_typed_parameter(
            name, ValueType.GRADIENT, Gradient().to_dict()
        )
        return Gradient.from_dict(value)

    def set_bool(self, name: str, value: bool) -> None:
        self._set_typed_parameter(name, value, ValueType.BOOL)

    def get_bool(self, name: str, default: bool = False) -> bool:
        return bool(self._get_typed_parameter(name, ValueType.BOOL, default))

    def set_int(self, name: str, value: int) -> None:
        self._set_typed_parameter(name, value, ValueType.I32)

    def get_int(self, name: str, default: int = 0) -> int:
        return int(self._get_typed_parameter(name, ValueType.I32, default))

    def set_uint(self, name: str, value: int) -> None:
        self._set_typed_parameter(name, value, ValueType.U32)

    def get_uint(self, name: str, default: int = 0) -> int:
        return int(self._get_typed_parameter(name, ValueType.U32, default))

    def set_float(self, name: str, value: float) -> None:
        self._set_typed_parameter(name, value, ValueType.F32)

    def get_float(self, name: str, default: float = 0.0) -> float:
        return float(self._get_typed_parameter(name, ValueType.F32, default))

    def set_vector2(self, name: str, x: float, y: float) -> None:
        self._set_typed_parameter(name, [x, y], ValueType.VEC2)

    def get_vector2(self, name: str) -> tuple[float, float]:
        return tuple(self._get_typed_parameter(name, ValueType.VEC2, (0.0, 0.0)))

    def set_vector3(self, name: str, x: float, y: float, z: float) -> None:
        self._set_typed_parameter(name, [x, y, z], ValueType.VEC3)

    def get_vector3(self, name: str) -> tuple[float, float, float]:
        return tuple(
            self._get_typed_parameter(name, ValueType.VEC3, (0.0, 0.0, 0.0))
        )

    def set_vector4(self, name: str, x: float, y: float, z: float, w: float) -> None:
        self._set_typed_parameter(name, [x, y, z, w], ValueType.VEC4)

    def get_vector4(self, name: str) -> tuple[float, float, float, float]:
        return tuple(
            self._get_typed_parameter(name, ValueType.VEC4, (0.0, 0.0, 0.0, 0.0))
        )

    def set_color(
        self, name: str, r: float, g: float, b: float, a: float = 1.0
    ) -> None:
        self._set_typed_parameter(name, [r, g, b, a], ValueType.COLOR)

    def get_color(self, name: str) -> tuple[float, float, float, float]:
        return tuple(
            self._get_typed_parameter(name, ValueType.COLOR, (0.0, 0.0, 0.0, 1.0))
        )

    def runtime_event_schema(self) -> list[dict]:
        """Describe the graph-defined per-particle event schemas."""
        self._ensure_runtime_state()
        return [
            {
                key: (
                    [dict(field) for field in value]
                    if key == "fields"
                    else value
                )
                for key, value in event_type.items()
                if not key.startswith("_")
            }
            for event_type in getattr(self, "_particle_event_types", ())
        ]

    def runtime_diagnostics(self) -> dict:
        """Return the on-demand particle control-plane state without GPU readback."""
        self._ensure_runtime_state()
        metadata = getattr(self, "_particle_metadata", None)
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
        native = self._native_engine()
        emitters = []
        any_resident = False
        any_playing = False
        for index, emitter in enumerate(
            getattr(metadata, "emitters", ()) if metadata is not None else ()
        ):
            runtime = None
            emitter_id = 0
            if index in gpu:
                emitter_id, runtime = gpu[index]
            play_requested = bool(getattr(runtime, "is_playing", False))
            artifact_revision = 0
            state_preserved = False
            if emitter_id and native is not None:
                artifact_revision = int(
                    native._gpu_particle_artifact_revision(emitter_id)
                )
                state_preserved = bool(
                    native._gpu_particle_state_was_preserved(emitter_id)
                )
            resident = artifact_revision > 0
            enabled = self._emitter_instance_options(emitter.stable_id)["enabled"]
            playing = resident and enabled and play_requested
            any_resident = any_resident or resident
            any_playing = any_playing or playing
            item = {
                "index": index,
                "stable_id": emitter.stable_id,
                "name": str(getattr(emitter, "name", emitter.stable_id)),
                "enabled": enabled,
                "play_on_start": self._emitter_instance_options(
                    emitter.stable_id
                )["play_on_start"],
                "play_requested": play_requested,
                "resident": resident,
                "playing": playing,
                "simulation_step": int(getattr(runtime, "simulation_step", 0)),
                "simulation_time_ticks": int(
                    getattr(runtime, "simulation_time_ticks", 0)
                ),
                "simulation_time_seconds": float(
                    getattr(runtime, "simulation_time_ticks", 0)
                )
                / 1_000_000_000.0,
                "seek_pending_seconds": self._pending_seek_seconds.get(
                    str(emitter.stable_id)
                ),
                "reload_compatibility": (
                    compatibility[index].value
                    if index < len(compatibility)
                    and compatibility[index] is not None
                    else ""
                ),
            }
            if emitter_id:
                item["gpu_emitter_id"] = int(emitter_id)
                item["artifact_revision"] = artifact_revision
                item["state_preserved"] = state_preserved
            emitters.append(item)

        return {
            "batch_id": int(self._batch_id),
            "random_seed": self._instance_random_seed(),
            "play_requested": bool(self._playing),
            "resident": any_resident,
            "playing": any_playing,
            "editor_preview_active": bool(
                getattr(self, "_editor_preview_active", False)
            ),
            "editor_preview_play_requested": bool(
                getattr(self, "_editor_preview_play_requested", True)
            ),
            "editor_preview_controls_allowed": (
                self._editor_preview_controls_allowed()
            ),
            "artifact_revision": int(self._artifact_revision),
            "graph_simulation_time_ticks": int(
                getattr(self, "_graph_simulation_time_ticks", 0)
            ),
            "last_compile_error": str(self._last_compile_error),
            "parameters": self.exposed_parameter_schema(),
            "events": self.runtime_event_schema(),
            "emitters": emitters,
        }

    def request_gpu_diagnostics(
        self, sample_frames: int = 60, state_sample_count: int = 0
    ) -> int:
        """Request one asynchronous GPU counter, bounds, and optional state snapshot.

        Collision counters start at zero for every request and accumulate only
        across that request's sample window. State sampling is disabled by
        default and bounded to 64 live particles and 16 MiB of source state so
        ordinary simulation never pays a readback cost.
        """
        self._ensure_runtime_state()
        if type(sample_frames) is not int or not 1 <= sample_frames <= 4096:
            raise ValueError("sample_frames must be an integer between 1 and 4096")
        if type(state_sample_count) is not int or not 0 <= state_sample_count <= 64:
            raise ValueError("state_sample_count must be an integer between 0 and 64")
        native = self._native_engine()
        if native is None or not hasattr(native, "_request_gpu_particle_diagnostics"):
            raise RuntimeError("GPU particle diagnostics are unavailable")
        request_id = int(
            native._request_gpu_particle_diagnostics(
                self._batch_id, sample_frames, state_sample_count
            )
        )
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
            overflow_counts = tuple(
                int(value) for value in emitter.pop("event_overflow_counts", ())
            )
            enqueue_counts = tuple(
                int(value) for value in emitter.pop("event_enqueue_counts", ())
            )
            complete_counts = tuple(
                int(value) for value in emitter.pop("event_complete_counts", ())
            )
            event_types = getattr(self, "_particle_event_types", ())
            if not all(
                len(values) == len(event_types)
                for values in (overflow_counts, enqueue_counts, complete_counts)
            ):
                raise RuntimeError(
                    "GPU particle event diagnostics do not match the event ABI "
                    f"for emitter index {index}: expected {len(event_types)} "
                    "event slots, got "
                    f"overflow={len(overflow_counts)}, "
                    f"enqueue={len(enqueue_counts)}, "
                    f"complete={len(complete_counts)}"
                )
            emitter["event_diagnostics"] = [
                {
                    "event_type_index": event_index,
                    "stable_id": event_type["stable_id"],
                    "name": event_type["name"],
                    "queue_capacity": int(event_type["queue_capacity"]),
                    "enqueue_count": enqueue_counts[event_index],
                    "complete_count": complete_counts[event_index],
                    "overflow_count": overflow_counts[event_index],
                }
                for event_index, event_type in enumerate(event_types)
            ]
            self._decode_gpu_state_samples(index, emitter)

        return result

    def _decode_gpu_state_samples(self, emitter_index: int, emitter: dict) -> None:
        samples = emitter.get("state_samples", ())
        if not samples:
            emitter["state_samples"] = []
            return
        layouts = getattr(self, "_particle_gpu_layouts", ())
        kernel = getattr(self, "_particle_kernel", None)
        if not 0 <= emitter_index < len(layouts) or kernel is None:
            raise RuntimeError("GPU particle state samples have no matching compiled layout")
        layout = layouts[emitter_index]
        fields = layout.get("attribute_fields") if isinstance(layout, dict) else None
        stride = layout.get("state_stride") if isinstance(layout, dict) else None
        if type(fields) is not list or type(stride) is not int or stride <= 0:
            raise RuntimeError("GPU particle state sample layout is invalid")
        try:
            attribute_types = {
                stable_id: value_type.value_type
                for stable_id, value_type, _default in kernel.emitters[
                    emitter_index
                ].attributes
            }
        except (AttributeError, IndexError, TypeError) as exc:
            raise RuntimeError(
                "GPU particle state sample attribute schema is unavailable"
            ) from exc

        for sample in samples:
            words = sample.pop("raw_words", None)
            if (
                type(words) is not list
                or len(words) * 4 != stride
                or not all(type(word) is int and 0 <= word <= 0xFFFFFFFF for word in words)
            ):
                raise RuntimeError("GPU particle state sample payload is invalid")
            raw = struct.pack(f"<{len(words)}I", *words)
            decoded = {}
            for field in fields:
                if type(field) is not dict:
                    raise RuntimeError("GPU particle state sample field layout is invalid")
                stable_id = field.get("stable_id")
                offset = field.get("offset")
                byte_size = field.get("byte_size")
                value_type = attribute_types.get(stable_id)
                if (
                    type(stable_id) is not str
                    or type(offset) is not int
                    or type(byte_size) is not int
                    or value_type is None
                    or offset < 0
                    or byte_size <= 0
                    or offset + byte_size > len(raw)
                ):
                    raise RuntimeError("GPU particle state sample field does not match its ABI")
                decoded[stable_id] = _decode_gpu_state_value(raw, offset, value_type)
            sample["attributes"] = decoded

    def request_gpu_view_diagnostics(
        self, view: str, camera_component_id: int = 0
    ) -> int:
        """Request one asynchronous cull-and-draw snapshot for Scene or Game."""
        self._ensure_runtime_state()
        normalized_view = str(view).strip().lower()
        if normalized_view not in {"scene", "game"}:
            raise ValueError("particle diagnostic view must be 'scene' or 'game'")
        camera_component_id = int(camera_component_id)
        if camera_component_id < 0 or (
            normalized_view == "scene" and camera_component_id != 0
        ):
            raise ValueError(
                "camera_component_id must be zero for Scene diagnostics and non-negative for Game diagnostics"
            )
        native = self._native_engine()
        if native is None or not hasattr(
            native, "_request_gpu_particle_view_diagnostics"
        ):
            raise RuntimeError("GPU particle view diagnostics are unavailable")
        request_id = int(
            native._request_gpu_particle_view_diagnostics(
                self._batch_id, normalized_view, camera_component_id
            )
        )
        if request_id <= 0:
            raise RuntimeError("GPU particle view diagnostic request was rejected")
        self._gpu_view_diagnostic_requests.add(
            (normalized_view, camera_component_id, request_id)
        )
        return request_id

    def poll_gpu_view_diagnostics(
        self, view: str, request_id: int, camera_component_id: int = 0
    ) -> dict:
        """Poll a snapshot requested by :meth:`request_gpu_view_diagnostics`."""
        self._ensure_runtime_state()
        normalized_view = str(view).strip().lower()
        camera_component_id = int(camera_component_id)
        if (
            normalized_view not in {"scene", "game"}
            or type(request_id) is not int
            or (normalized_view, camera_component_id, request_id)
            not in self._gpu_view_diagnostic_requests
        ):
            raise ValueError(
                "GPU particle view diagnostic request does not belong to this component/view"
            )
        native = self._native_engine()
        if native is None or not hasattr(
            native, "_poll_gpu_particle_view_diagnostics"
        ):
            raise RuntimeError("GPU particle view diagnostics are unavailable")
        result = dict(
            native._poll_gpu_particle_view_diagnostics(
                normalized_view, request_id, camera_component_id
            )
        )
        if int(result.get("graph_instance_id", 0)) not in {0, self._batch_id}:
            raise RuntimeError(
                "GPU particle view diagnostic response belongs to another graph"
            )
        metadata = getattr(self, "_particle_metadata", None)
        emitter_names = tuple(
            emitter.stable_id for emitter in getattr(metadata, "emitters", ())
        )
        for output in result.get("outputs", ()):
            index = int(output.get("emitter_index", -1))
            output["emitter_stable_id"] = (
                emitter_names[index] if 0 <= index < len(emitter_names) else ""
            )
        return result

    def restart(
        self,
        emitter: int | str | None = None,
        *,
        honor_play_on_start: bool = False,
    ) -> bool:
        if emitter is None:
            self._playing = True
            self._gpu_update_dirty = True
            self._graph_simulation_time_ticks = 0
            restarted = False
            for index, runtime in zip(
                getattr(self, "_gpu_emitter_indices", ()),
                getattr(self, "_gpu_controllers", ()),
            ):
                should_play = self._emitter_should_play(
                    index, honor_play_on_start=honor_play_on_start
                )
                runtime.reset(playing=should_play)
                self._set_gpu_emitter_playing(index, should_play)
                self._pending_seek_seconds.pop(
                    self._emitter_stable_id(index), None
                )
                self._set_emitter_prewarm_pending(index, should_play)
                restarted = restarted or should_play
            self._reset_gpu_emitters()
            return restarted
        emitter_index = self._resolve_emitter_index(emitter)
        if emitter_index is None:
            return False
        runtime = self._runtime_at(emitter_index)
        if runtime is None:
            return False
        runtime.reset(playing=True)
        self._playing = True
        self._gpu_update_dirty = True
        self._set_gpu_emitter_playing(emitter_index, True)
        self._pending_seek_seconds.pop(
            self._emitter_stable_id(emitter_index), None
        )
        # Prewarm is a graph-wide startup transaction. Replaying one emitter
        # in isolation would let it observe a different parameter/event
        # history from the other emitters while sharing the same graph clock.
        self._prewarm_pending_emitters.discard(
            self._emitter_stable_id(emitter_index)
        )
        self._reset_gpu_emitters(emitter_index)
        return True

    def seek(
        self,
        time_seconds: float,
        emitter: int | str | None = None,
    ) -> bool:
        """Deterministically replay enabled GPU emitters from time zero."""
        if isinstance(time_seconds, bool):
            raise TypeError("particle seek time must be a number")
        time_seconds = float(time_seconds)
        if not math.isfinite(time_seconds) or time_seconds < 0.0:
            raise ValueError("particle seek time must be finite and non-negative")
        required_steps = int(math.ceil(time_seconds / self._PREROLL_STEP_SECONDS))
        if required_steps > self._MAX_PREROLL_STEPS:
            raise ValueError(
                f"particle seek requires {required_steps} fixed steps; maximum is "
                f"{self._MAX_PREROLL_STEPS}"
            )
        if not self._has_runtime() and not self._load_saved_artifact():
            return False

        if emitter is None:
            targets = list(getattr(self, "_gpu_emitter_indices", ()))
        else:
            emitter_index = self._resolve_emitter_index(emitter)
            targets = [] if emitter_index is None else [emitter_index]

        accepted = False
        for emitter_index in targets:
            if not self._emitter_is_enabled(emitter_index):
                continue
            runtime = self._runtime_at(emitter_index)
            if runtime is None:
                continue
            stable_id = self._emitter_stable_id(emitter_index)
            self._pending_seek_seconds[stable_id] = time_seconds
            self._prewarm_pending_emitters.discard(stable_id)
            self._reset_gpu_emitters(emitter_index)
            accepted = True
        if accepted:
            self._gpu_update_dirty = True
            from Infernux.lib import SceneManager

            SceneManager.instance().mark_temporal_discontinuity()
        return accepted

    def reset_simulation(self, emitter: int | str | None = None) -> bool:
        """Reset particle simulation time while preserving emitter play state."""
        return self.seek(0.0, emitter)

    def start_emitter(self, emitter: int | str) -> bool:
        return self.play(emitter)

    def pause_emitter(self, emitter: int | str) -> bool:
        return self.pause(emitter)

    def terminate_emitter(self, emitter: int | str) -> bool:
        return self.stop(emitter)

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
        self._sync_serialized_instance_overrides()
        self._apply_pending_runtime_rebuild()
        if not self._has_runtime() and not self._load_saved_artifact():
            return
        self._reload_published_artifact_if_needed()
        scaled_delta_time = float(delta_time) * float(self.simulation_speed)
        if scaled_delta_time < 0.0:
            return
        if self._gpu_controllers:
            if not self._playing and not getattr(self, "_gpu_update_dirty", True):
                # A paused graph keeps its last GPU draw registration. Only
                # inspect the emitter transform here so moving the owner while
                # paused still refreshes world-space particles without building
                # controllers, bounds, and a full batch every frame.
                emitter_to_world = self._emitter_matrix()
                if self._emitter_to_world_cache is not None and np.array_equal(
                    emitter_to_world, self._emitter_to_world_cache
                ):
                    return
            self._update_gpu_particle_graph(scaled_delta_time)

    def editor_preview_begin(self) -> bool:
        """Prepare this component for Scene View simulation outside Play mode."""
        if not self._editor_preview_controls_allowed():
            return False
        self._ensure_runtime_state(playing=True)
        self._editor_preview_active = True
        if self._editor_preview_play_requested:
            self._playing = True
        ready, rebuilt = self._ensure_editor_preview_runtime()
        if not ready:
            return False
        if self._editor_preview_play_requested:
            if rebuilt:
                # Scene View preview is explicit editor playback.  Emitter
                # Play On Start only controls runtime scene startup and must
                # not leave a selected effect partially stopped after the
                # Play Mode scene has been restored.
                self.restart(honor_play_on_start=False)
            else:
                self.play()
        return True

    def editor_preview_update(self, delta_time: float, speed: float = 1.0) -> bool:
        if not self._editor_preview_controls_allowed():
            return False
        if not getattr(self, "_editor_preview_active", False):
            return False
        if not getattr(self, "_editor_preview_play_requested", True):
            return self._gpu_runtime_resident()
        ready, rebuilt = self._ensure_editor_preview_runtime()
        if not ready:
            return False
        if rebuilt:
            self.restart(honor_play_on_start=False)
        self.update(max(0.0, float(delta_time)) * max(0.0, float(speed)))
        return self._gpu_runtime_resident()

    def editor_preview_play(self) -> bool:
        if not self._editor_preview_controls_allowed():
            return False
        self._ensure_runtime_state(playing=True)
        self._editor_preview_active = True
        self._editor_preview_play_requested = True
        self._playing = True
        ready, _rebuilt = self._ensure_editor_preview_runtime()
        if not ready:
            return False
        return self.play()

    def editor_preview_pause(self) -> bool:
        if not self._editor_preview_controls_allowed():
            return False
        if not getattr(self, "_editor_preview_active", False):
            return False
        self._editor_preview_play_requested = False
        return self.pause()

    def editor_preview_suspend(self) -> bool:
        """Pause for editor selection changes without changing user intent."""
        if not self._editor_preview_controls_allowed():
            return False
        if not getattr(self, "_editor_preview_active", False):
            return False
        return self.pause()

    def editor_preview_is_playing(self) -> bool:
        return bool(
            self._editor_preview_controls_allowed()
            and getattr(self, "_editor_preview_play_requested", True)
            and self._gpu_runtime_resident()
            and any(
                controller.is_playing
                for controller in getattr(self, "_gpu_controllers", ())
            )
        )

    def editor_preview_is_ready(self) -> bool:
        return bool(
            self._editor_preview_controls_allowed()
            and getattr(self, "_editor_preview_active", False)
            and self._gpu_runtime_resident()
        )

    def editor_preview_time_seconds(self) -> float:
        # Preview is a view over the same graph runtime used by Play Mode. Its
        # timeline must therefore expose the authoritative graph clock rather
        # than reconstructing time from individual emitter controllers.
        return int(getattr(self, "_graph_simulation_time_ticks", 0)) / 1_000_000_000.0

    def editor_preview_duration_seconds(self) -> float:
        metadata = getattr(self, "_particle_metadata", None)
        return max(
            (
                float(emitter.settings.start_delay) + float(emitter.settings.duration)
                for emitter in getattr(metadata, "emitters", ())
            ),
            default=1.0,
        )

    def editor_preview_seek(self, time_seconds: float) -> bool:
        if not self._editor_preview_controls_allowed():
            return False
        play_requested = bool(
            getattr(self, "_editor_preview_play_requested", True)
        )
        self._ensure_runtime_state(playing=play_requested)
        self._editor_preview_active = True
        self._playing = play_requested
        ready, _rebuilt = self._ensure_editor_preview_runtime()
        if not ready or not self.seek(time_seconds):
            return False
        # A paused preview does not tick, so submit the queued deterministic
        # replay immediately. The controller restores the prior play state.
        self.update(0.0)
        return self._gpu_runtime_resident()

    def editor_preview_stop(self) -> bool:
        if not self._editor_preview_controls_allowed():
            return False
        self._ensure_runtime_state(playing=False)
        self._editor_preview_active = True
        self._editor_preview_play_requested = False
        had_runtime = self.stop()
        # Resetting the simulation is recorded on the next GPU submission.
        # Removing the preview graph also drops its draw registrations now,
        # so Stop clears the Scene View in the same editor frame.
        self._remove_native_batch()
        self._playing = False
        return had_runtime

    def editor_preview_set_emitter_muted(
        self, emitter_index: int, muted: bool
    ) -> bool:
        if not self._editor_preview_controls_allowed():
            return False
        if not self._valid_emitter_index(emitter_index) or type(muted) is not bool:
            return False
        stable_id = self._emitter_stable_id(emitter_index)
        if muted:
            self._editor_preview_muted_emitters.add(stable_id)
            self._editor_preview_solo_emitters.discard(stable_id)
        else:
            self._editor_preview_muted_emitters.discard(stable_id)
        self._apply_editor_preview_visibility(emitter_index)
        self._gpu_update_dirty = True
        return True

    def editor_preview_set_emitter_solo(
        self, emitter_index: int, solo: bool
    ) -> bool:
        if not self._editor_preview_controls_allowed():
            return False
        if not self._valid_emitter_index(emitter_index) or type(solo) is not bool:
            return False
        stable_id = self._emitter_stable_id(emitter_index)
        if solo:
            self._editor_preview_solo_emitters.add(stable_id)
            self._editor_preview_muted_emitters.discard(stable_id)
        else:
            self._editor_preview_solo_emitters.discard(stable_id)
        self._apply_editor_preview_visibility()
        self._gpu_update_dirty = True
        return True

    def editor_preview_restart_emitter(self, emitter_index: int) -> bool:
        if not self._editor_preview_controls_allowed():
            return False
        if not getattr(self, "_editor_preview_active", False):
            return False
        self._editor_preview_play_requested = True
        resident = self._gpu_runtime_resident()
        if not resident:
            # A global Stop removes the GPU graph.  Recreate it paused so a
            # per-emitter restart cannot accidentally start its siblings.
            self._playing = False
        ready, _rebuilt = self._ensure_editor_preview_runtime()
        if not ready:
            return False
        self._playing = True
        return self.restart(emitter_index)

    def editor_preview_emitter_states(self) -> list[dict]:
        metadata = getattr(self, "_particle_metadata", None)
        result = []
        for index, emitter in enumerate(getattr(metadata, "emitters", ())):
            runtime = self._runtime_at(index)
            stable_id = emitter.stable_id
            result.append(
                {
                    "index": index,
                    "stable_id": stable_id,
                    "name": emitter.name,
                    "enabled": self._emitter_instance_options(stable_id)["enabled"],
                    "play_on_start": self._emitter_instance_options(stable_id)[
                        "play_on_start"
                    ],
                    "muted": stable_id in self._editor_preview_muted_emitters,
                    "solo": stable_id in self._editor_preview_solo_emitters,
                    "visible": self._editor_preview_emitter_visible(index),
                    "playing": bool(
                        self._emitter_is_enabled(index)
                        and getattr(runtime, "is_playing", False)
                    ),
                }
            )
        return result

    def editor_preview_end(self) -> None:
        if not self._editor_preview_controls_allowed():
            return
        if getattr(self, "_editor_preview_active", False):
            self.editor_preview_suspend()
            self._editor_preview_active = False
            self._editor_preview_muted_emitters.clear()
            self._editor_preview_solo_emitters.clear()
            self._apply_editor_preview_visibility()

    def on_draw_gizmos_selected(self) -> None:
        """Draw every enabled emitter shape using its current authored settings."""
        metadata = getattr(self, "_particle_metadata", None)
        try:
            transform = self.transform
        except (AttributeError, ReferenceError, RuntimeError):
            return
        if metadata is None or transform is None:
            return

        from Infernux.gizmos import Gizmos

        old_matrix = Gizmos.matrix
        old_color = Gizmos.color
        try:
            for index, emitter in enumerate(metadata.emitters):
                if not self._emitter_is_enabled(index):
                    continue
                shape = emitter.settings.shape
                Gizmos.matrix = (
                    self._sdf_emitter_shape_gizmo_matrix(emitter, shape)
                    if shape.kind is EmitterShapeKind.SDF
                    else self._emitter_shape_gizmo_matrix(shape.space.value)
                )
                Gizmos.color = self._emitter_gizmo_color(index)
                self._draw_emitter_shape_gizmo(Gizmos, shape)
            raw_bounds_mode = get_raw_field_value(self, "bounds_mode")
            if raw_bounds_mode is ParticleBoundsMode.MANUAL or str(raw_bounds_mode) == "manual":
                center = get_raw_field_value(self, "manual_bounds_center")
                size = get_raw_field_value(self, "manual_bounds_size")
                Gizmos.matrix = list(transform.local_to_world_matrix())
                Gizmos.color = (0.22, 0.82, 1.0)
                Gizmos.draw_wire_cube(
                    (float(center.x), float(center.y), float(center.z)),
                    (abs(float(size.x)), abs(float(size.y)), abs(float(size.z))),
                )
        finally:
            Gizmos.color = old_color
            Gizmos.matrix = old_matrix

    def on_disable(self):
        self._remove_native_batch()
        self._clear_runtime_state()

    def on_destroy(self):
        self._remove_native_batch()
        self._clear_runtime_state()

    def on_validate(self):
        signature = self._definition_signature()
        if signature == getattr(self, "_runtime_definition_signature", None):
            return
        self._runtime_definition_signature = signature
        self._runtime_rebuild_pending = True

    def _definition_signature(self) -> tuple:
        """Identify authored fields that require a new native particle program."""
        graph = get_raw_field_value(self, "graph")
        if isinstance(graph, ParticleGraphAsset):
            graph_key = ("memory", id(graph))
        else:
            graph_key = (
                "asset",
                str(getattr(graph, "guid", "") or ""),
                str(getattr(graph, "path_hint", "") or ""),
            )
        return graph_key, int(get_raw_field_value(self, "random_seed") or 0)

    def _apply_pending_runtime_rebuild(self) -> None:
        if not getattr(self, "_runtime_rebuild_pending", False):
            return
        self._runtime_rebuild_pending = False
        self._gpu_update_dirty = True
        graph = get_raw_field_value(self, "graph")
        if graph is None or not (
            isinstance(graph, ParticleGraphAsset)
            or bool(graph)
            or getattr(graph, "path_hint", "")
        ):
            self._remove_native_batch()
            self._clear_runtime_state()
            return
        # Artifact replacement is transactional: a failed load leaves the
        # previous valid native graph resident instead of blanking the frame.
        self._load_saved_artifact(force=True)

    def _load_saved_artifact(self, *, force: bool = False) -> bool:
        if self._try_get_game_object() is None:
            return False
        self._ensure_runtime_state(playing=bool(getattr(self, "_playing", False)))
        graph_ref = get_raw_field_value(self, "graph")
        now = time.monotonic()
        if not force and now < getattr(self, "_compile_retry_at", 0.0):
            path = self._particle_source_path(graph_ref)
            guid = getattr(graph_ref, "guid", "")
            if not path or ParticleArtifactRegistry.get(path, guid=guid) is None:
                return False
        if graph_ref is not None and (
            isinstance(graph_ref, ParticleGraphAsset)
            or bool(graph_ref)
            or getattr(graph_ref, "path_hint", "")
            or (
                callable(getattr(graph_ref, "resolve", None))
                and graph_ref.resolve() is not None
            )
        ):
            loaded = self._load_particle_graph_artifact(graph_ref)
            if loaded:
                self._compile_retry_at = 0.0
                self._last_compile_error = ""
            elif self._compile_retry_at <= now:
                self._compile_retry_at = now + 1.0
            return loaded
        return False

    def _report_compile_failure(self, exc: Exception) -> None:
        now = time.monotonic()
        message = str(exc)
        self._compile_retry_at = now + 1.0
        if (
            message != getattr(self, "_last_compile_error", "")
            or now - getattr(self, "_last_compile_error_log_at", 0.0) >= 5.0
        ):
            Debug.log_error(f"[ParticleSystem] ParticleGraph AOT load failed: {message}")
            self._last_compile_error = message
            self._last_compile_error_log_at = now

    def _load_particle_graph_artifact(self, graph_ref: ParticleGraphRef) -> bool:
        try:
            path = self._particle_source_path(graph_ref)
            if not path:
                raise RuntimeError(
                    "ParticleGraph runtime requires a saved AOT artifact; save "
                    "the ParticleGraph before Play"
                )
            guid = getattr(graph_ref, "guid", "")
            editor_source = Application.is_editor() and os.path.isfile(path)
            artifact = ParticleArtifactRegistry.get(path, guid=guid)
            recovery_attempted = False
            if artifact is None:
                runtime_load_error = None
                try:
                    artifact = ParticleArtifactRegistry.load_runtime_reference(
                        path, guid=guid
                    )
                except (OSError, RuntimeError, TypeError, ValueError) as exc:
                    runtime_load_error = exc
                if artifact is None:
                    if editor_source:
                        artifact = ParticleArtifactRegistry.compile_path(
                            path, guid=guid, force_recompile=True
                        )
                        recovery_attempted = True
                    elif runtime_load_error is not None:
                        raise runtime_load_error
            if artifact is None:
                raise RuntimeError(
                    "ParticleGraph AOT artifact is missing or stale; save the "
                    "ParticleGraph before Play"
                )

            while True:
                try:
                    hir = artifact.hir
                    kernel = ParticleKernelProgram.from_dict(artifact.kernel_ir)
                    revision = artifact.revision
                    source_key = artifact.source_key
                    metadata = decode_particle_runtime_metadata(hir)
                    self._reconcile_parameter_overrides(metadata.parameters)
                    self._reconcile_emitter_overrides(metadata.emitters)
                    previous_metadata = getattr(self, "_particle_metadata", None)
                    previous_kernel = getattr(self, "_particle_kernel", None)
                    reload_compatibility = [None] * len(metadata.emitters)
                    if previous_metadata is not None and previous_kernel is not None:
                        previous_emitters = {
                            emitter.stable_id: (emitter, kernel_emitter)
                            for emitter, kernel_emitter in (
                                zip(previous_metadata.emitters, previous_kernel.emitters)
                            )
                        }
                        for emitter_index, (emitter, kernel_emitter) in enumerate(
                            zip(metadata.emitters, kernel.emitters)
                        ):
                            previous = previous_emitters.get(emitter.stable_id)
                            if previous is None:
                                continue
                            previous_emitter, previous_kernel_emitter = previous
                            compatibility = classify_emitter_update(
                                previous_kernel_emitter,
                                kernel_emitter,
                                previous_emitter.settings,
                                emitter.settings,
                            )
                            reload_compatibility[emitter_index] = compatibility
                    self._publish_gpu_particle_graph(
                        artifact,
                        metadata,
                        kernel,
                        reload_compatibility,
                    )
                    break
                except (OSError, RuntimeError, TypeError, ValueError):
                    if recovery_attempted or not editor_source:
                        raise
                    artifact = ParticleArtifactRegistry.compile_path(
                        path, guid=guid, force_recompile=True
                    )
                    recovery_attempted = True
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._report_compile_failure(exc)
            return False

        self._particle_kernel = kernel
        self._particle_gpu_layouts = (
            tuple(artifact.gpu_glsl["emitters"])
            if artifact is not None
            else ()
        )
        self._particle_metadata = metadata
        self._emitter_reload_compatibility = tuple(reload_compatibility)
        self._artifact_revision = revision
        self._artifact_registry_revision = int(
            getattr(ParticleArtifactRegistry, "_revision", 0)
        )
        self._artifact_source_key = source_key
        self._emitter_to_world_cache = None
        return True

    def _publish_gpu_particle_graph(
        self, artifact, metadata, kernel, reload_compatibility
    ) -> None:
        if artifact is None:
            raise RuntimeError("GPU ParticleGraph execution requires an AOT artifact")
        native = self._native_engine()
        if native is None:
            raise RuntimeError("GPU ParticleGraph execution requires a graphical renderer")
        if not all(
            hasattr(native, name)
            for name in (
                "_replace_gpu_particle_graph",
                "_begin_gpu_particle_batch",
                "_reset_gpu_particle_emitter",
            )
        ):
            raise RuntimeError(
                "GPU ParticleGraph execution requires the complete native GPU particle interface"
            )

        glsl_emitters = artifact.gpu_glsl.get("emitters")
        if type(glsl_emitters) is not list or len(glsl_emitters) != len(metadata.emitters):
            raise RuntimeError("ParticleGraph GPU emitter metadata is incomplete")

        programs = []
        controllers = []
        preserve_states = []
        emitter_ids = []
        emitter_indices = []
        previous_controllers = {}
        previous_layouts = {}
        previous_runtime_metadata = {}
        instance_seed = self._instance_random_seed()
        previous_metadata = getattr(self, "_particle_metadata", None)
        if previous_metadata is not None:
            previous_runtime_metadata = {
                emitter.stable_id: emitter for emitter in previous_metadata.emitters
            }
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
            if any(output.output_type not in {"sprite", "mesh", "ribbon"} for output in emitter.outputs):
                raise RuntimeError("the GPU particle renderer received an unsupported output type")
            if (
                type(glsl_emitter) is not dict
                or glsl_emitter.get("stable_id") != emitter.stable_id
                or type(glsl_emitter.get("state_stride")) is not int
                or type(glsl_emitter.get("event_type_count")) is not int
                or glsl_emitter["event_type_count"] != len(kernel.events.event_types)
                or type(glsl_emitter.get("collision_enabled")) is not bool
                or glsl_emitter["collision_enabled"]
                != emitter.settings.collision_enabled
                or "continuation" not in glsl_emitter
            ):
                raise RuntimeError("ParticleGraph GPU layout does not match its runtime schedule")
            decoded = decode_gpu_particle_spirv(artifact.gpu_spirv, index)
            glsl_update_render_fusion = glsl_emitter.get("update_render_fusion")
            decoded_update_render_fusion = decoded.get("update_render_fusion")
            if type(glsl_update_render_fusion) is not dict or type(decoded_update_render_fusion) is not dict or glsl_update_render_fusion != decoded_update_render_fusion:
                raise RuntimeError("ParticleGraph GPU update_render_fusion metadata is missing or mismatched")
            continuation_source = glsl_emitter["continuation"]
            continuation_binary = decoded.get("continuation")
            if (continuation_source is None) != (continuation_binary is None):
                raise RuntimeError(
                    "ParticleGraph GPU continuation source and binary disagree"
                )
            continuation_program = None
            if continuation_source is not None:
                if (
                    type(continuation_source) is not dict
                    or type(continuation_binary) is not dict
                    or type(continuation_source.get("record_stride")) is not int
                    or type(continuation_source.get("lane_count")) is not int
                    or type(continuation_source.get("join_count")) is not int
                    or continuation_source["record_stride"]
                    != continuation_binary.get("record_stride")
                    or continuation_source["lane_count"]
                    != continuation_binary.get("lane_count")
                    or continuation_source["join_count"]
                    != continuation_binary.get("join_count")
                    or type(continuation_binary.get("stages")) is not dict
                    or set(continuation_binary["stages"])
                    != {"prepare", "classify", "dispatch"}
                ):
                    raise RuntimeError(
                        "ParticleGraph GPU continuation metadata is incomplete"
                    )
                continuation_capacity = (
                    int(emitter.settings.capacity)
                    * int(continuation_source["lane_count"])
                )
                if not 0 < continuation_capacity <= (1 << 24):
                    raise RuntimeError(
                        "ParticleGraph GPU continuation capacity exceeds the native bounded pool"
                    )
                continuation_program = {
                    "capacity": continuation_capacity,
                    "record_stride": continuation_source["record_stride"],
                    "lane_count": continuation_source["lane_count"],
                    "join_count": continuation_source["join_count"],
                    "prepare": continuation_binary["stages"]["prepare"],
                    "classify": continuation_binary["stages"]["classify"],
                    "dispatch": continuation_binary["stages"]["dispatch"],
                }
            emitter_id = self._gpu_emitter_id(emitter.stable_id)
            previous_controller = previous_controllers.get(emitter.stable_id)
            migration = None
            previous_layout = previous_layouts.get(emitter.stable_id)
            if (
                previous_controller is not None
                and isinstance(previous_layout, dict)
                and previous_layout.get("event_type_count")
                != glsl_emitter["event_type_count"]
            ):
                reload_compatibility[index] = (
                    ParticleRuntimeCompatibility.EMITTER_RESTART
                )
            if (
                previous_controller is not None
                and reload_compatibility[index]
                is ParticleRuntimeCompatibility.LAYOUT_MIGRATABLE
            ):
                try:
                    migration = build_gpu_particle_migration(
                        previous_layout,
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
                    "event_type_count": glsl_emitter["event_type_count"],
                    "collision_enabled": glsl_emitter["collision_enabled"],
                    "update_render_fusion": dict(glsl_update_render_fusion),
                    "continuation": continuation_program,
                    "parameter_words": list(
                        pack_gpu_particle_parameters(
                            kernel.parameters,
                            self._parameter_overrides,
                        )
                    ),
                    "preserve_state": preserve_state,
                    "migration": migration,
                    "data_interface_layout": self._gpu_data_interface_layout(
                        kernel.emitters[index], glsl_emitter, metadata.parameters
                    ),
                    "stages": decoded["stages"],
                    "billboard": decoded["billboard"],
                    "mesh_shaders": decoded["mesh"],
                    "outputs": [
                        {
                            "id": self._gpu_output_id(emitter.stable_id, output.output_id),
                            "stable_id": output.output_id,
                            "output_type": output.output_type,
                            "mesh": self._gpu_mesh_binding(
                                output,
                                metadata.parameters,
                                emitter.stable_id,
                            ),
                            "material": self._gpu_material_binding(output, emitter.stable_id),
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
                previous_emitter = previous_runtime_metadata.get(emitter.stable_id)
                preserve_play_state = bool(
                    previous_controller is not None and previous_emitter is not None
                )
                controller = GpuParticleEmitterController(
                    emitter.settings,
                    system_seed=instance_seed,
                    playing=(
                        previous_controller.is_playing
                        if preserve_play_state
                        else self._emitter_should_play(
                            index,
                            honor_play_on_start=True,
                            metadata=metadata,
                        )
                    ),
                )
            controllers.append(controller)
            preserve_states.append(preserve_state)
            emitter_ids.append(emitter_id)
            emitter_indices.append(index)

        removed = sorted(set(getattr(self, "_gpu_emitter_ids", ())) - set(emitter_ids))
        event_metadata = artifact.hir.get("events")
        if type(event_metadata) is not dict:
            raise RuntimeError("ParticleGraph event ABI metadata is missing")
        event_types = event_metadata.get("event_types")
        event_abi_hash = event_metadata.get("event_abi_hash")
        if (
            type(event_types) is not list
            or type(event_abi_hash) is not str
        ):
            raise RuntimeError("ParticleGraph event ABI metadata is invalid")
        runtime_event_types = self._build_runtime_event_schema(
            artifact.hir, kernel
        )
        error = native._replace_gpu_particle_graph(
            self._batch_id,
            programs,
            removed,
        )
        if error:
            raise RuntimeError(error)
        self._gpu_controllers = controllers
        self._gpu_emitter_ids = emitter_ids
        self._gpu_emitter_indices = emitter_indices
        self._particle_event_types = runtime_event_types
        for emitter_index, controller in zip(emitter_indices, controllers):
            self._set_gpu_emitter_playing(emitter_index, controller.is_playing)
        live_stable_ids = {str(emitter.stable_id) for emitter in metadata.emitters}
        self._prewarm_pending_emitters.intersection_update(live_stable_ids)
        self._pending_seek_seconds = {
            stable_id: seconds
            for stable_id, seconds in self._pending_seek_seconds.items()
            if stable_id in live_stable_ids
        }
        for index, (emitter, controller, preserved) in enumerate(
            zip(metadata.emitters, controllers, preserve_states)
        ):
            if not preserved:
                self._set_emitter_prewarm_pending(
                    index, controller.is_playing, metadata=metadata
                )

    @staticmethod
    def _build_runtime_event_schema(hir: dict, kernel) -> tuple[dict, ...]:
        events = hir.get("events")
        emitters = hir.get("emitters")
        if type(events) is not dict or type(emitters) is not list:
            raise RuntimeError("ParticleGraph runtime event metadata is incomplete")
        encoded_types = events.get("event_types")
        if type(encoded_types) is not list:
            raise RuntimeError("ParticleGraph runtime event metadata is invalid")
        result = []
        if len(encoded_types) != len(kernel.events.event_types):
            raise RuntimeError("ParticleGraph runtime event types do not match the GPU ABI")
        for event_type, kernel_type in zip(encoded_types, kernel.events.event_types):
            if (
                type(event_type) is not dict
                or event_type.get("stable_id") != kernel_type.stable_id
            ):
                raise RuntimeError("ParticleGraph runtime event type order is invalid")
            fields = event_type.get("fields")
            if type(fields) is not list:
                raise RuntimeError("ParticleGraph runtime event fields are invalid")
            result.append(
                {
                    "stable_id": event_type["stable_id"],
                    "name": event_type["name"],
                    "event_type_index": int(kernel_type.type_index),
                    "queue_capacity": int(event_type["queue_capacity"]),
                    "fields": tuple(
                        {
                            "stable_id": field["stable_id"],
                            "name": field["name"],
                            "type": dict(field["type"]),
                            "default": field["default"],
                        }
                        for field in fields
                    ),
                }
            )
        return tuple(result)

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
        bounds_mode, manual_bounds_lower, manual_bounds_upper = (
            self._gpu_bounds_request(emitter_to_world)
        )
        raw_offscreen_policy = get_raw_field_value(self, "offscreen_policy")
        try:
            offscreen_policy = (
                raw_offscreen_policy
                if isinstance(raw_offscreen_policy, ParticleOffscreenPolicy)
                else ParticleOffscreenPolicy(str(raw_offscreen_policy))
            )
        except ValueError:
            offscreen_policy = ParticleOffscreenPolicy.ALWAYS_SIMULATE
        transactional_controllers = []
        rollback_controllers = []
        completed_prewarm = set()
        completed_seek = set()
        instance_seed = self._instance_random_seed()
        live_stable_ids = {str(emitter.stable_id) for emitter in metadata.emitters}
        replay_seconds = [
            float(seconds)
            for stable_id, seconds in self._pending_seek_seconds.items()
            if stable_id in live_stable_ids
        ]
        graph_running = bool(getattr(self, "_playing", False))
        executing_any = graph_running and any(
            controller.is_playing and self._emitter_is_enabled(emitter_index)
            for emitter_index, controller in zip(
                self._gpu_emitter_indices, self._gpu_controllers
            )
        )
        graph_ticks = int(getattr(self, "_graph_simulation_time_ticks", 0))
        startup_prewarm_seconds = max(
            (
                float(emitter.settings.start_delay)
                + float(emitter.settings.duration)
                for emitter in metadata.emitters
                if str(emitter.stable_id) in self._prewarm_pending_emitters
                and bool(emitter.settings.loop)
            ),
            default=0.0,
        )
        graph_prewarm = bool(
            graph_running
            and bool(get_raw_field_value(self, "prewarm"))
            and graph_ticks == 0
            and startup_prewarm_seconds > 0.0
        )
        if graph_prewarm:
            replay_seconds.append(startup_prewarm_seconds)
        replay_end_ticks = max(
            [graph_ticks]
            + [
                int(round(seconds * 1_000_000_000.0))
                for seconds in replay_seconds
            ]
        )
        next_graph_ticks = replay_end_ticks
        if executing_any:
            next_graph_ticks = min(
                0xFFFFFFFFFFFFFFFF,
                replay_end_ticks
                + int(round(float(delta_time) * 1_000_000_000.0)),
            )
        for controller_slot, (emitter_id, emitter_index, controller) in enumerate(
            zip(
                self._gpu_emitter_ids,
                self._gpu_emitter_indices,
                self._gpu_controllers,
            )
        ):
            emitter = metadata.emitters[emitter_index]
            enabled = self._emitter_is_enabled(emitter_index)
            stable_id = str(emitter.stable_id)
            seek_seconds = self._pending_seek_seconds.get(stable_id)
            if seek_seconds is not None and not enabled:
                self._pending_seek_seconds.pop(stable_id, None)
                seek_seconds = None
            wants_prewarm = bool(
                graph_prewarm and enabled and controller.is_playing
            )
            preroll_steps = []
            force_render_after_preroll = False
            if seek_seconds is not None:
                was_playing = controller.is_playing
                working_controller = GpuParticleEmitterController(
                    emitter.settings,
                    playing=True,
                    system_seed=instance_seed,
                )
                preroll_steps = self._build_gpu_preroll_steps(
                    working_controller,
                    seek_seconds,
                    emitter_position,
                    operation="seek",
                    end_time_ticks=replay_end_ticks,
                )
                if not was_playing:
                    working_controller.pause()
                transactional_controllers.append(
                    (controller_slot, working_controller)
                )
                completed_seek.add(stable_id)
                force_render_after_preroll = True
            elif wants_prewarm and enabled and controller.is_playing:
                working_controller = copy.deepcopy(controller)
                preroll_steps = self._build_gpu_preroll_steps(
                    working_controller,
                    startup_prewarm_seconds,
                    emitter_position,
                    operation="prewarm",
                    end_time_ticks=replay_end_ticks,
                )
                transactional_controllers.append(
                    (controller_slot, working_controller)
                )
                completed_prewarm.add(stable_id)
            else:
                working_controller = controller
                rollback_controllers.append(
                    (working_controller, working_controller._checkpoint())
                )
            schedule = working_controller.tick(
                delta_time,
                emitter_position,
                enabled=enabled and graph_running,
                runtime_managed_playing=executing_any and seek_seconds is None,
                simulation_time_ticks=next_graph_ticks,
            )
            transforms = self._gpu_transform_buffer(
                emitter.settings.simulation_space.value == "local"
            )
            frame_items.append(
                {
                    "emitter_id": emitter_id,
                    "preroll_steps": preroll_steps,
                    "spawn_count": schedule.spawn_count,
                    "spawn_base_id": schedule.spawn_base_id,
                    "spawn_generation": schedule.spawn_generation,
                    "system_seed": schedule.system_seed,
                    "simulation_step": schedule.simulation_step,
                    "simulation_time_ticks": schedule.simulation_time_ticks,
                    "delta_time": schedule.delta_time,
                    "transforms": transforms,
                    "simulate": schedule.simulate,
                    "render": (schedule.render or force_render_after_preroll)
                    and enabled
                    and self._editor_preview_emitter_visible(emitter_index),
                    "offscreen_policy": offscreen_policy.value,
                    # A paused seek still needs one export-only GPU pass.
                    # Force visibility policy for that pass while keeping
                    # simulate=False, so state/time do not advance again.
                    "force_simulation": force_render_after_preroll
                    and not schedule.simulate,
                    "bounds_mode": bounds_mode,
                    "manual_bounds_lower": manual_bounds_lower,
                    "manual_bounds_upper": manual_bounds_upper,
                }
            )
        if frame_items:
            accepted = False
            try:
                accepted = bool(
                    native._begin_gpu_particle_batch(self._batch_id, frame_items)
                )
            finally:
                if not accepted:
                    for controller, checkpoint in rollback_controllers:
                        controller._restore(checkpoint)
            if accepted:
                if executing_any or replay_seconds:
                    self._graph_simulation_time_ticks = next_graph_ticks
                for controller_slot, controller in transactional_controllers:
                    self._gpu_controllers[controller_slot] = controller
                self._prewarm_pending_emitters.difference_update(completed_prewarm)
                for stable_id in completed_seek:
                    self._pending_seek_seconds.pop(stable_id, None)
                self._gpu_update_dirty = False

    def _build_gpu_preroll_steps(
        self,
        controller,
        total_seconds: float,
        emitter_position,
        *,
        operation: str,
        end_time_ticks: int,
    ) -> list[dict]:
        total_seconds = float(total_seconds)
        step_seconds = float(self._PREROLL_STEP_SECONDS)
        step_count = int(math.ceil(total_seconds / step_seconds))
        if step_count > self._MAX_PREROLL_STEPS:
            raise RuntimeError(
                f"particle {operation} requires {step_count} fixed steps; maximum is "
                f"{self._MAX_PREROLL_STEPS}"
            )
        result = []
        remaining = total_seconds
        total_ticks = int(round(total_seconds * 1_000_000_000.0))
        start_ticks = max(0, int(end_time_ticks) - total_ticks)
        elapsed_ticks = 0
        for _ in range(step_count):
            delta_time = min(step_seconds, remaining)
            elapsed_ticks += int(round(delta_time * 1_000_000_000.0))
            schedule = controller.tick(
                delta_time,
                emitter_position,
                enabled=True,
                simulation_time_ticks=min(
                    int(end_time_ticks), start_ticks + elapsed_ticks
                ),
            )
            result.append(
                {
                    "spawn_count": schedule.spawn_count,
                    "spawn_base_id": schedule.spawn_base_id,
                    "spawn_generation": schedule.spawn_generation,
                    "system_seed": schedule.system_seed,
                    "simulation_step": schedule.simulation_step,
                    "simulation_time_ticks": schedule.simulation_time_ticks,
                    "delta_time": schedule.delta_time,
                }
            )
            remaining = max(0.0, remaining - delta_time)
        return result

    def _gpu_bounds_request(
        self, emitter_to_world: np.ndarray
    ) -> tuple[str, list[float], list[float]]:
        raw_mode = get_raw_field_value(self, "bounds_mode")
        try:
            mode = (
                raw_mode
                if isinstance(raw_mode, ParticleBoundsMode)
                else ParticleBoundsMode(str(raw_mode))
            )
        except ValueError:
            mode = ParticleBoundsMode.AUTOMATIC
        if mode is ParticleBoundsMode.AUTOMATIC:
            return mode.value, [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]

        center_value = get_raw_field_value(self, "manual_bounds_center")
        size_value = get_raw_field_value(self, "manual_bounds_size")
        center = np.asarray(
            [center_value.x, center_value.y, center_value.z], dtype=np.float32
        )
        half_size = np.abs(
            np.asarray([size_value.x, size_value.y, size_value.z], dtype=np.float32)
        ) * np.float32(0.5)
        if not np.all(np.isfinite(center)) or not np.all(np.isfinite(half_size)):
            raise ValueError("particle manual bounds must contain finite values")

        signs = np.asarray(
            [
                (-1.0, -1.0, -1.0),
                (-1.0, -1.0, 1.0),
                (-1.0, 1.0, -1.0),
                (-1.0, 1.0, 1.0),
                (1.0, -1.0, -1.0),
                (1.0, -1.0, 1.0),
                (1.0, 1.0, -1.0),
                (1.0, 1.0, 1.0),
            ],
            dtype=np.float32,
        )
        local_corners = center + signs * half_size
        homogeneous = np.concatenate(
            [local_corners, np.ones((8, 1), dtype=np.float32)], axis=1
        )
        world_corners = (emitter_to_world @ homogeneous.T).T[:, :3]
        if not np.all(np.isfinite(world_corners)):
            raise ValueError("particle manual bounds transform produced non-finite values")
        lower = np.min(world_corners, axis=0)
        upper = np.max(world_corners, axis=0)
        return mode.value, lower.tolist(), upper.tolist()

    def _reload_published_artifact_if_needed(self) -> None:
        if not self._artifact_source_key:
            return
        registry_revision = int(getattr(ParticleArtifactRegistry, "_revision", 0))
        if registry_revision == getattr(self, "_artifact_registry_revision", 0):
            return
        self._artifact_registry_revision = registry_revision
        graph_ref = get_raw_field_value(self, "graph")
        path = self._particle_source_path(graph_ref)
        artifact = ParticleArtifactRegistry.get(
            path,
            guid=getattr(graph_ref, "guid", ""),
        )
        if artifact is not None and artifact.revision != self._artifact_revision:
            self._load_particle_graph_artifact(graph_ref)

    def _runtime_at(self, emitter_index: int):
        if type(emitter_index) is not int:
            return None
        indices = getattr(self, "_gpu_emitter_indices", ())
        runtimes = getattr(self, "_gpu_controllers", ())
        try:
            runtime_index = indices.index(emitter_index)
        except ValueError:
            return None
        return runtimes[runtime_index]

    def _resolve_emitter_index(self, emitter: int | str) -> int | None:
        """Resolve an emitter index, authored name, or stable ID without raising."""
        metadata = getattr(self, "_particle_metadata", None)
        emitters = getattr(metadata, "emitters", ())
        if type(emitter) is int:
            return emitter if 0 <= emitter < len(emitters) else None
        if type(emitter) is not str:
            return None
        selector = emitter.strip()
        if not selector:
            return None
        for index, candidate in enumerate(emitters):
            if candidate.name == selector or candidate.stable_id == selector:
                return index
        return None

    def _emitter_shape_gizmo_matrix(self, shape_space: str) -> list[float]:
        if shape_space != "world":
            return list(self.transform.local_to_world_matrix())
        position = self.transform.position
        return [
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
            float(position.x),
            float(position.y),
            float(position.z),
            1.0,
        ]

    def _sdf_emitter_shape_gizmo_matrix(self, emitter, shape) -> list[float]:
        interface = next(
            (
                item
                for item in emitter.data_interfaces
                if isinstance(item, SdfVolume)
                and item.stable_id == shape.sdf_interface
            ),
            None,
        )
        if interface is None:
            return self._emitter_shape_gizmo_matrix(shape.space.value)
        field_to_space = np.asarray(
            interface.field_to_space, dtype=np.float32
        ).reshape(4, 4)
        if interface.space is CoordinateSpace.WORLD:
            field_to_world = field_to_space
        else:
            emitter_to_world = np.asarray(
                self.transform.local_to_world_matrix(), dtype=np.float32
            ).reshape(4, 4, order="F")
            field_to_world = emitter_to_world @ field_to_space
        if not np.all(np.isfinite(field_to_world)):
            return self._emitter_shape_gizmo_matrix(shape.space.value)
        return field_to_world.reshape(-1, order="F").tolist()

    @staticmethod
    def _emitter_gizmo_color(index: int) -> tuple[float, float, float]:
        palette = (
            (1.0, 0.68, 0.22),
            (0.32, 0.82, 1.0),
            (0.56, 0.92, 0.42),
            (0.92, 0.48, 0.78),
        )
        return palette[index % len(palette)]

    def _draw_emitter_shape_gizmo(self, gizmos, shape) -> None:
        kind = shape.kind
        if kind is EmitterShapeKind.SPHERE:
            gizmos.draw_wire_sphere((0.0, 0.0, 0.0), float(shape.radius))
            return
        if kind is EmitterShapeKind.BOX:
            gizmos.draw_wire_cube((0.0, 0.0, 0.0), tuple(shape.dimensions))
            return
        if kind is EmitterShapeKind.CONE:
            ParticleSystem._draw_cone_emitter_gizmo(
                gizmos, float(shape.radius), float(shape.angle_degrees)
            )
            return
        if kind is EmitterShapeKind.MESH:
            bounds = self._mesh_emitter_gizmo_bounds(shape)
            if bounds is None:
                self._draw_emitter_origin_gizmo(gizmos)
                return
            minimum = bounds[:3]
            maximum = bounds[3:]
            center = tuple((low + high) * 0.5 for low, high in zip(minimum, maximum))
            size = tuple(max(0.0, high - low) for low, high in zip(minimum, maximum))
            gizmos.draw_wire_cube(center, size)
            return
        if kind is EmitterShapeKind.SDF:
            gizmos.draw_wire_cube((0.0, 0.0, 0.0), (1.0, 1.0, 1.0))
            return

        self._draw_emitter_origin_gizmo(gizmos)

    @staticmethod
    def _draw_emitter_origin_gizmo(gizmos) -> None:
        marker = 0.2
        gizmos.draw_line((-marker, 0.0, 0.0), (marker, 0.0, 0.0))
        gizmos.draw_line((0.0, -marker, 0.0), (0.0, marker, 0.0))
        gizmos.draw_line((0.0, 0.0, -marker), (0.0, 0.0, marker))

    def _mesh_emitter_gizmo_bounds(self, shape) -> tuple[float, ...] | None:
        reference = shape.mesh
        key = str(reference.guid or reference.path_hint).strip()
        if not key:
            return None
        cache = getattr(self, "_emitter_mesh_gizmo_bounds", None)
        if cache is None:
            cache = self._emitter_mesh_gizmo_bounds = {}
        if key in cache:
            return cache[key]
        try:
            mesh = self._resolve_mesh_shape(shape)
            bounds = tuple(float(value) for value in mesh.get_bounds())
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            return None
        if len(bounds) != 6 or not all(math.isfinite(value) for value in bounds):
            return None
        cache[key] = bounds
        return bounds

    @staticmethod
    def _draw_cone_emitter_gizmo(gizmos, radius: float, angle_degrees: float) -> None:
        segments = 24
        radius = max(0.0, radius)
        gizmos.draw_wire_arc((0.0, 0.0, 0.0), (0.0, 0.0, 1.0), radius, segments=segments)

        direction_length = max(1.0, radius * 2.0)
        if angle_degrees >= 90.0:
            gizmos.draw_wire_sphere((0.0, 0.0, 0.0), direction_length, segments=segments)
            return

        spread = math.tan(math.radians(max(0.0, angle_degrees))) * direction_length
        gizmos.draw_wire_arc(
            (0.0, 0.0, direction_length),
            (0.0, 0.0, 1.0),
            spread,
            segments=segments,
        )
        for index in range(8):
            angle = math.tau * index / 8.0
            end = (
                math.cos(angle) * spread,
                math.sin(angle) * spread,
                direction_length,
            )
            gizmos.draw_line((0.0, 0.0, 0.0), end)

    def _valid_emitter_index(self, emitter_index: int) -> bool:
        metadata = getattr(self, "_particle_metadata", None)
        return bool(
            type(emitter_index) is int
            and 0 <= emitter_index < len(getattr(metadata, "emitters", ()))
        )

    def _emitter_stable_id(self, emitter_index: int) -> str:
        metadata = getattr(self, "_particle_metadata", None)
        return str(metadata.emitters[emitter_index].stable_id)

    def _instance_random_seed(self) -> int:
        value = get_raw_field_value(self, "random_seed")
        if type(value) is not int or not 0 <= value <= 0x7FFFFFFF:
            raise ValueError("particle random_seed must be an integer from 0 to 2147483647")
        return value

    def _set_emitter_prewarm_pending(
        self, emitter_index: int, should_play: bool, *, metadata=None
    ) -> None:
        metadata = metadata or getattr(self, "_particle_metadata", None)
        emitters = getattr(metadata, "emitters", ())
        if not 0 <= emitter_index < len(emitters):
            return
        emitter = emitters[emitter_index]
        stable_id = str(emitter.stable_id)
        if (
            bool(get_raw_field_value(self, "prewarm"))
            and bool(should_play)
            and bool(emitter.settings.loop)
            and int(getattr(self, "_graph_simulation_time_ticks", 0)) == 0
        ):
            self._prewarm_pending_emitters.add(stable_id)
        else:
            self._prewarm_pending_emitters.discard(stable_id)

    def _editor_preview_emitter_visible(self, emitter_index: int) -> bool:
        if not self._editor_preview_controls_allowed():
            return True
        if not getattr(self, "_editor_preview_active", False):
            return True
        if not self._valid_emitter_index(emitter_index):
            return False
        stable_id = self._emitter_stable_id(emitter_index)
        if self._editor_preview_solo_emitters:
            return stable_id in self._editor_preview_solo_emitters
        return stable_id not in self._editor_preview_muted_emitters

    @staticmethod
    def _editor_preview_controls_allowed() -> bool:
        """Keep Scene preview controls completely outside Play Mode."""
        try:
            from Infernux.engine.play_mode import PlayModeManager

            manager = PlayModeManager.instance()
            return manager is None or bool(manager.is_edit_mode)
        except (AttributeError, RuntimeError):
            return False

    def _apply_editor_preview_visibility(
        self, emitter_index: int | None = None
    ) -> None:
        # GPU visibility is consumed when the next frame schedule is built;
        # simulation continues so unmuting does not restart the emitter.
        return

    def _emitter_is_enabled(self, emitter_index: int, *, metadata=None) -> bool:
        if type(emitter_index) is not int:
            return False
        metadata = metadata or getattr(self, "_particle_metadata", None)
        emitters = getattr(metadata, "emitters", ())
        if not 0 <= emitter_index < len(emitters):
            return False
        return self._emitter_instance_options(emitters[emitter_index].stable_id)[
            "enabled"
        ]

    def _emitter_should_play(
        self,
        emitter_index: int,
        *,
        honor_play_on_start: bool,
        metadata=None,
    ) -> bool:
        metadata = metadata or getattr(self, "_particle_metadata", None)
        emitters = getattr(metadata, "emitters", ())
        if not 0 <= emitter_index < len(emitters):
            return False
        emitter = emitters[emitter_index]
        options = self._emitter_instance_options(emitter.stable_id)
        return bool(
            self._playing
            and (options["play_on_start"] or not honor_play_on_start)
        )

    def _has_runtime(self) -> bool:
        return bool(getattr(self, "_gpu_controllers", ()))

    def _gpu_runtime_resident(self) -> bool:
        """Return whether every Python controller still has a native GPU peer."""
        emitter_ids = tuple(getattr(self, "_gpu_emitter_ids", ()))
        if not self._has_runtime() or not emitter_ids:
            return False
        native = self._native_engine()
        revision = getattr(native, "_gpu_particle_artifact_revision", None)
        if not callable(revision):
            return True
        try:
            return all(int(revision(emitter_id)) > 0 for emitter_id in emitter_ids)
        except (ReferenceError, RuntimeError, TypeError, ValueError):
            return False

    def _ensure_editor_preview_runtime(self) -> tuple[bool, bool]:
        """Re-publish stale GPU residency after Play Mode recreates the scene."""
        had_controllers = self._has_runtime()
        if had_controllers and self._gpu_runtime_resident():
            return True, False
        if had_controllers:
            # Scene restore can preserve Python fields after the native graph
            # was removed. Do not let those stale controllers suppress AOT
            # publication of the replacement graph.
            self._gpu_controllers = []
            self._gpu_emitter_ids = []
            self._gpu_emitter_indices = []
        loaded = self._load_saved_artifact(force=had_controllers)
        return bool(loaded and self._gpu_runtime_resident()), bool(loaded)

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
    def _particle_texture_guid(value) -> str:
        """Return a native texture identity without treating a path as a GUID."""
        if isinstance(value, AssetReference):
            reference = value
        elif isinstance(value, dict):
            reference = AssetReference.from_dict(value)
        else:
            token = str(value or "").strip()
            return token if token in {"white", "black", "normal"} else "white"

        guid = str(reference.guid or "").strip()
        if guid:
            return guid
        path_hint = str(reference.path_hint or "").strip()
        if not path_hint:
            return "white"
        try:
            from Infernux.core.asset_ref import _get_asset_database
            from Infernux.engine.path_utils import resolved_path
            from Infernux.engine.project_context import get_project_root

            path = path_hint
            if not os.path.isabs(path):
                project_root = get_project_root()
                if project_root:
                    path = os.path.join(project_root, path)
            database = _get_asset_database()
            if database:
                return database.get_guid_from_path(resolved_path(path)) or "white"
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
            pass
        return "white"

    def _gpu_material_binding(self, output, emitter_id: str = "") -> dict[str, object]:
        is_mesh = output.output_type == "mesh"
        state: dict[str, object] = {
            "render_queue": 2000 if is_mesh else 3000,
            "blend_enabled": not is_mesh,
            "depth_test_enabled": True,
            "depth_write_enabled": is_mesh,
            "native": None,
        }
        try:
            from Infernux.core.material import Material

            cache_key = (str(emitter_id), str(output.output_id))
            material = self._output_materials.get(cache_key)
            if (
                material is None
                or material.vert_shader_name != "Particle Sprite"
                or material.frag_shader_name != output.shader
            ):
                template = None if is_mesh else Material.get("ParticleSpriteMaterial")
                material = template.clone() if template is not None else Material.create_unlit(
                    f"ParticleOutput:{emitter_id}:{output.output_id}"
                )
                material.vert_shader_name = "Particle Sprite"
                material.frag_shader_name = str(output.shader)
                self._output_materials[cache_key] = material
            for binding in output.shader_properties:
                value = (
                    self._parameter_overrides.get(binding.parameter_id, binding.default)
                    if binding.parameter_id
                    else binding.default
                )
                kind = binding.value_type.value_type
                if kind is ValueType.TEXTURE2D:
                    material.native.set_texture_guid(
                        binding.name, self._particle_texture_guid(value)
                    )
                elif kind is ValueType.F32:
                    material.native.set_float(binding.name, float(value))
                elif kind is ValueType.I32:
                    material.native.set_int(binding.name, int(value))
                elif kind is ValueType.VEC2:
                    material.native.set_vector2(binding.name, value)
                elif kind is ValueType.VEC3:
                    material.native.set_vector3(binding.name, value)
                elif kind is ValueType.COLOR:
                    material.native.set_color(binding.name, value)
                elif kind is ValueType.VEC4:
                    material.native.set_vector4(binding.name, value)
                elif kind is ValueType.MAT4:
                    material.native.set_matrix(binding.name, value)
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

    def _gpu_mesh_binding(
        self,
        output,
        parameters=(),
        emitter_id: str = "",
    ) -> object:
        if output.output_type != "mesh":
            return None
        reference = output.mesh
        if output.mesh_parameter:
            parameter = next(
                (
                    item
                    for item in parameters
                    if item.stable_id == output.mesh_parameter
                ),
                None,
            )
            if (
                parameter is None
                or parameter.value_type.value_type is not ValueType.MESH
            ):
                raise RuntimeError(
                    f"ParticleGraph Mesh Output parameter {output.mesh_parameter!r} is missing"
                )
            value = self._parameter_overrides.get(
                output.mesh_parameter,
                parameter.default,
            )
            if _is_skinned_mesh_source_document(value):
                raise RuntimeError(
                    "ParticleGraph Static Mesh Output cannot consume a live "
                    "SkinnedMeshRenderer; use the Mesh asset itself"
                )
            reference = AssetReference.from_dict(value)
        return self._resolve_mesh_reference(
            reference, "ParticleGraph Mesh Output"
        )

    @classmethod
    def _resolve_mesh_reference(cls, reference, purpose: str):
        from Infernux.lib import AssetRegistry, get_builtin_primitive_mesh

        builtin_name = builtin_mesh_name(reference)
        if builtin_name:
            return get_builtin_primitive_mesh(builtin_name)

        registry = AssetRegistry.instance()
        native = registry.load_mesh_by_guid(reference.guid) if reference.guid else None
        path = cls._absolute_project_path(reference.path_hint)
        if native is None and path:
            native = registry.load_mesh(path)
        if native is None:
            identity = reference.guid or reference.path_hint or "<empty reference>"
            raise RuntimeError(f"{purpose} cannot load {identity!r}")
        return native

    @classmethod
    def _resolve_mesh_shape(cls, shape):
        return cls._resolve_mesh_reference(
            shape.mesh, "ParticleGraph Mesh emitter shape"
        )

    def _gpu_data_interface_layout(
        self, emitter, glsl_emitter, parameters
    ) -> dict[str, object]:
        layout = glsl_emitter.get("data_interface_layout")
        if type(layout) is not dict:
            raise RuntimeError("ParticleGraph GPU data interface layout is missing")
        volume_layouts = layout.get("volume_interfaces")
        if type(volume_layouts) is not list:
            raise RuntimeError("ParticleGraph GPU volume-interface layout is invalid")
        texture_layouts = layout.get("texture2d_parameters")
        if type(texture_layouts) is not list:
            raise RuntimeError("ParticleGraph GPU Texture2D parameter layout is invalid")
        mesh_layouts = layout.get("mesh_interfaces")
        if type(mesh_layouts) is not list:
            raise RuntimeError("ParticleGraph GPU Mesh resource layout is invalid")
        if not volume_layouts and not texture_layouts and not mesh_layouts:
            return dict(layout)

        from Infernux.lib import AssetRegistry

        registry = AssetRegistry.instance()
        result = dict(layout)
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
            reference = interface.texture
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
        parameter_by_id = {
            parameter.stable_id: parameter for parameter in parameters
        }
        decoded_textures = []
        for encoded in texture_layouts:
            if type(encoded) is not dict:
                raise RuntimeError(
                    "ParticleGraph GPU Texture2D parameter layout is invalid"
                )
            stable_id = str(encoded.get("stable_id") or "")
            parameter = parameter_by_id.get(stable_id)
            if (
                parameter is None
                or parameter.value_type.value_type is not ValueType.TEXTURE2D
            ):
                raise RuntimeError(
                    f"ParticleGraph GPU Texture2D parameter {stable_id!r} is missing"
                )
            default = AssetReference.from_dict(parameter.default)
            reference = AssetReference.from_dict(
                self._parameter_overrides.get(stable_id, default.to_dict())
            )
            native = (
                registry.load_texture_by_guid(reference.guid)
                if reference.guid
                else None
            )
            if reference.guid and (
                native is None or str(native.dimension).lower() != "2d"
            ):
                raise RuntimeError(
                    f"ParticleGraph Texture2D parameter {parameter.name!r} cannot load a Texture2D from {reference.guid!r}"
                )
            decoded = dict(encoded)
            decoded.update(
                texture_guid=reference.guid or "white",
                native=native,
            )
            decoded_textures.append(decoded)
        result["texture2d_parameters"] = decoded_textures
        mesh_by_id = {
            interface.stable_id: interface
            for interface in emitter.data_interfaces
            if isinstance(interface, MeshResourceBinding)
        }
        decoded_meshes = []
        for encoded in mesh_layouts:
            if type(encoded) is not dict:
                raise RuntimeError("ParticleGraph GPU Mesh resource layout is invalid")
            try:
                stable_id = str(encoded["stable_id"])
                interface = mesh_by_id.get(stable_id)
                reference = (
                    interface.mesh
                    if interface is not None
                    else AssetReference.from_dict(encoded["mesh"])
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    "ParticleGraph GPU Mesh resource reference is invalid"
                ) from exc
            if stable_id != "__emitter_shape_mesh" and interface is None:
                raise RuntimeError(
                    f"ParticleGraph GPU Mesh resource binding {stable_id!r} is missing"
                )
            decoded = dict(encoded)
            if interface is not None:
                reference = interface.mesh
                if interface.mesh_parameter:
                    parameter = parameter_by_id.get(interface.mesh_parameter)
                    if (
                        parameter is None
                        or parameter.value_type.value_type is not ValueType.MESH
                    ):
                        raise RuntimeError(
                            f"ParticleGraph Mesh resource parameter "
                            f"{interface.mesh_parameter!r} is missing"
                        )
                    authored = AssetReference.from_dict(parameter.default)
                    override = self._parameter_overrides.get(
                        interface.mesh_parameter, authored.to_dict()
                    )
                    skinned_source = _resolve_skinned_mesh_source(
                        override,
                        "ParticleGraph GPU Mesh resource binding",
                    )
                    if skinned_source is not None:
                        decoded.update(
                            source_kind="skinned_renderer",
                            native_skinned_renderer=skinned_source,
                            native=None,
                            space="world",
                            mesh_to_space=list(interface.mesh_to_space),
                            mesh=authored.to_dict(),
                            mesh_parameter=interface.mesh_parameter,
                        )
                        decoded_meshes.append(decoded)
                        continue
                    overridden = AssetReference.from_dict(override)
                    if overridden.guid or overridden.path_hint:
                        reference = overridden
                decoded.update(
                    source_kind="asset",
                    space=interface.space.value,
                    mesh_to_space=list(interface.mesh_to_space),
                    mesh=reference.to_dict(),
                    mesh_parameter=interface.mesh_parameter,
                )
            else:
                decoded["source_kind"] = "asset"
            decoded["native"] = self._resolve_mesh_reference(
                reference, "ParticleGraph GPU Mesh resource binding"
            )
            decoded_meshes.append(decoded)
        result["mesh_interfaces"] = decoded_meshes
        return result

    def _resolve_vector_field(self, emitter_id: str, interface: VectorField):
        from Infernux.lib import AssetRegistry

        reference = interface.texture
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

    def _set_gpu_emitter_playing(
        self, emitter_index: int, playing: bool
    ) -> bool:
        native = self._native_engine()
        if native is None or not hasattr(
            native, "_set_gpu_particle_emitter_playing"
        ):
            return False
        try:
            runtime_index = getattr(self, "_gpu_emitter_indices", ()).index(
                emitter_index
            )
        except ValueError:
            return False
        emitter_ids = getattr(self, "_gpu_emitter_ids", ())
        if not 0 <= runtime_index < len(emitter_ids):
            return False
        return bool(
            native._set_gpu_particle_emitter_playing(
                emitter_ids[runtime_index], bool(playing)
            )
        )

    def _remove_gpu_emitters(self) -> None:
        native = self._native_engine()
        emitter_ids = list(getattr(self, "_gpu_emitter_ids", ()))
        if (
            emitter_ids
            and native is not None
            and hasattr(native, "_replace_gpu_particle_graph")
        ):
            native._replace_gpu_particle_graph(self._batch_id, [], emitter_ids)
        self._gpu_emitter_ids = []
        self._gpu_emitter_indices = []
        self._gpu_controllers = []

    def _clear_runtime_state(self) -> None:
        self._gpu_controllers = []
        self._gpu_emitter_ids = []
        self._gpu_emitter_indices = []
        self._emitter_reload_compatibility = ()
        self._particle_kernel = None
        self._particle_gpu_layouts = ()
        self._particle_metadata = None
        self._particle_event_types = ()
        self._artifact_revision = 0
        self._artifact_registry_revision = int(
            getattr(ParticleArtifactRegistry, "_revision", 0)
        )
        self._artifact_source_key = ""
        self._graph_simulation_time_ticks = 0
        self._emitter_to_world_cache = None
        self._gpu_transform_buffers = {}
        self._compile_retry_at = 0.0
        self._last_compile_error = ""
        self._last_compile_error_log_at = 0.0

    def _decode_parameter_overrides(self) -> dict[str, object]:
        raw = get_raw_field_value(self, "_parameter_overrides_json")
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            Debug.log_warning("[ParticleSystem] Invalid serialized parameter overrides were discarded")
            return {}
        if type(value) is not dict or any(type(key) is not str for key in value):
            Debug.log_warning("[ParticleSystem] Invalid serialized parameter overrides were discarded")
            return {}
        return value

    def _decode_emitter_overrides(self) -> dict[str, dict[str, bool]]:
        raw = get_raw_field_value(self, "_emitter_overrides_json")
        try:
            value = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            Debug.log_warning(
                "[ParticleSystem] Invalid serialized emitter overrides were discarded"
            )
            return {}
        if type(value) is not dict:
            return {}
        result = {}
        for stable_id, options in value.items():
            if (
                type(stable_id) is str
                and type(options) is dict
                and type(options.get("enabled")) is bool
                and type(options.get("play_on_start")) is bool
            ):
                result[stable_id] = {
                    "enabled": options["enabled"],
                    "play_on_start": options["play_on_start"],
                }
        return result

    def _store_parameter_overrides(self) -> None:
        encoded = json.dumps(
            self._parameter_overrides,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if get_raw_field_value(self, "_parameter_overrides_json") != encoded:
            self._parameter_overrides_json = encoded
        self._serialized_parameter_overrides_cache = str(
            get_raw_field_value(self, "_parameter_overrides_json") or "{}"
        )
        self._parameter_overrides = self._decode_parameter_overrides()
        self._instance_overrides_dirty = False

    def _store_emitter_overrides(self) -> None:
        encoded = json.dumps(
            self._emitter_overrides,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        if get_raw_field_value(self, "_emitter_overrides_json") != encoded:
            self._emitter_overrides_json = encoded
        self._serialized_emitter_overrides_cache = str(
            get_raw_field_value(self, "_emitter_overrides_json") or "{}"
        )
        self._emitter_overrides = self._decode_emitter_overrides()
        self._instance_overrides_dirty = False

    def _sync_serialized_instance_overrides(self) -> None:
        if not self.__dict__.get("_instance_overrides_dirty", True):
            return
        if not hasattr(self, "_parameter_overrides"):
            self._ensure_runtime_state()
            return
        parameter_raw = str(
            get_raw_field_value(self, "_parameter_overrides_json") or "{}"
        )
        if parameter_raw != self._serialized_parameter_overrides_cache:
            previous = self._parameter_overrides
            self._serialized_parameter_overrides_cache = parameter_raw
            self._parameter_overrides = self._decode_parameter_overrides()
            if self._has_runtime():
                metadata = getattr(self, "_particle_metadata", None)
                resource_ids = {
                    parameter.stable_id
                    for parameter in getattr(metadata, "parameters", ())
                    if parameter.exposed
                    and parameter.value_type.value_type
                    in {ValueType.TEXTURE2D, ValueType.MESH}
                }
                resource_changed = any(
                    previous.get(stable_id) != self._parameter_overrides.get(stable_id)
                    for stable_id in resource_ids
                )
                if resource_changed:
                    self._load_saved_artifact(force=True)
                else:
                    self._upload_parameter_overrides()
        emitter_raw = str(
            get_raw_field_value(self, "_emitter_overrides_json") or "{}"
        )
        if emitter_raw != self._serialized_emitter_overrides_cache:
            self._serialized_emitter_overrides_cache = emitter_raw
            self._emitter_overrides = self._decode_emitter_overrides()
            for emitter_index in getattr(self, "_gpu_emitter_indices", ()):
                self._apply_emitter_instance_options(emitter_index)
        self._instance_overrides_dirty = False

    def _emitter_instance_options(self, stable_id: str) -> dict[str, bool]:
        options = getattr(self, "_emitter_overrides", {}).get(str(stable_id), {})
        return {
            "enabled": bool(options.get("enabled", True)),
            "play_on_start": bool(options.get("play_on_start", True)),
        }

    def _apply_emitter_instance_options(self, emitter_index: int) -> None:
        # Enabled is consumed with playback state when the next frame request
        # is built. No second user-facing Active state exists.
        return

    def _reconcile_parameter_overrides(self, parameters) -> None:
        self._ensure_runtime_state(playing=bool(getattr(self, "_playing", False)))
        by_id = {
            parameter.stable_id: parameter
            for parameter in parameters
            if parameter.exposed
        }
        reconciled = {}
        for stable_id, value in self._parameter_overrides.items():
            parameter = by_id.get(stable_id)
            if parameter is None:
                continue
            try:
                reconciled[stable_id] = self._normalize_parameter_value(
                    parameter, value
                )
            except (TypeError, ValueError):
                continue
        if reconciled != self._parameter_overrides:
            self._parameter_overrides = reconciled
            self._store_parameter_overrides()

    def _reconcile_emitter_overrides(self, emitters) -> None:
        self._ensure_runtime_state(playing=bool(getattr(self, "_playing", False)))
        valid_ids = {str(emitter.stable_id) for emitter in emitters}
        reconciled = {
            stable_id: options
            for stable_id, options in self._emitter_overrides.items()
            if stable_id in valid_ids
        }
        if reconciled != self._emitter_overrides:
            self._emitter_overrides = reconciled
            self._store_emitter_overrides()

    def _find_exposed_parameter(self, name: str, *, compile_if_needed: bool):
        if type(name) is not str or not name:
            return None
        metadata = getattr(self, "_particle_metadata", None)
        if metadata is None and compile_if_needed:
            self._ensure_runtime_state()
            self._load_saved_artifact(force=True)
            metadata = getattr(self, "_particle_metadata", None)
        parameters = getattr(metadata, "parameters", ())
        for parameter in parameters:
            if parameter.exposed and parameter.stable_id == name:
                return parameter
        for parameter in parameters:
            if parameter.exposed and parameter.name == name:
                return parameter
        return None

    def _require_exposed_parameter(self, name: str):
        parameter = self._find_exposed_parameter(name, compile_if_needed=True)
        if parameter is None:
            raise KeyError(f"ParticleGraph has no exposed parameter {name!r}")
        return parameter

    @staticmethod
    def _normalize_parameter_value(parameter, value):
        kind = parameter.value_type.value_type
        if kind is ValueType.CURVE:
            try:
                curve = value if isinstance(value, Curve) else Curve.from_dict(value)
            except (TypeError, ValueError) as exc:
                raise TypeError(
                    f"particle parameter {parameter.name!r} requires a Curve"
                ) from exc
            return curve.to_dict()
        if kind is ValueType.GRADIENT:
            try:
                gradient = (
                    value
                    if isinstance(value, Gradient)
                    else Gradient.from_dict(value)
                )
            except (TypeError, ValueError) as exc:
                raise TypeError(
                    f"particle parameter {parameter.name!r} requires a Gradient"
                ) from exc
            return gradient.to_dict()
        if kind is ValueType.MESH:
            return _normalize_mesh_source_value(value, parameter.name)
        if kind is ValueType.TEXTURE2D:
            if isinstance(value, AssetReference):
                reference = value
            elif type(value) is dict:
                reference = AssetReference.from_dict(value)
            else:
                raise TypeError(
                    f"particle parameter {parameter.name!r} requires an asset reference"
                )
            return reference.to_dict()
        if kind is ValueType.BOOL:
            if type(value) is not bool:
                raise TypeError(f"particle parameter {parameter.name!r} requires a bool")
            return value
        if kind in {ValueType.I32, ValueType.U32}:
            if type(value) is not int:
                raise TypeError(f"particle parameter {parameter.name!r} requires an integer")
            if kind is ValueType.U32 and value < 0:
                raise ValueError(f"particle parameter {parameter.name!r} cannot be negative")
            return int(value)
        dimension = {
            ValueType.F32: 1,
            ValueType.VEC2: 2,
            ValueType.VEC3: 3,
            ValueType.VEC4: 4,
            ValueType.COLOR: 4,
        }.get(kind)
        values = [value] if dimension == 1 else value
        if (
            dimension is None
            or not isinstance(values, (list, tuple))
            or len(values) != dimension
            or any(type(item) not in {int, float} for item in values)
        ):
            raise TypeError(f"particle parameter {parameter.name!r} has an invalid value")
        normalized = [float(item) for item in values]
        if not all(math.isfinite(item) for item in normalized):
            raise ValueError(f"particle parameter {parameter.name!r} must be finite")
        return normalized[0] if dimension == 1 else normalized

    def _set_typed_parameter(self, name: str, value, expected: ValueType) -> None:
        parameter = self._require_exposed_parameter(name)
        if parameter.value_type.value_type is not expected:
            raise TypeError(
                f"particle parameter {parameter.name!r} is {parameter.value_type.value_type.value}, "
                f"not {expected.value}"
            )
        self.set_parameter(parameter.stable_id, value)

    def _get_typed_parameter(self, name: str, expected: ValueType, default):
        parameter = self._find_exposed_parameter(name, compile_if_needed=True)
        if parameter is None:
            return default
        if parameter.value_type.value_type is not expected:
            raise TypeError(
                f"particle parameter {parameter.name!r} is {parameter.value_type.value_type.value}, "
                f"not {expected.value}"
            )
        return self._parameter_overrides.get(parameter.stable_id, parameter.default)

    def _upload_parameter_overrides(self) -> None:
        kernel = getattr(self, "_particle_kernel", None)
        if kernel is None or not self._has_runtime():
            return
        words = pack_gpu_particle_parameters(kernel.parameters, self._parameter_overrides)
        native = self._native_engine()
        if native is None or not hasattr(native, "_update_gpu_particle_parameters"):
            raise RuntimeError("GPU particle parameter updates require the native particle runtime")
        error = native._update_gpu_particle_parameters(self._batch_id, list(words))
        if error:
            raise RuntimeError(error)
        for emitter in getattr(self._particle_metadata, "emitters", ()):
            for output in emitter.outputs:
                self._gpu_material_binding(output, emitter.stable_id)

    @staticmethod
    def _native_engine():
        try:
            from Infernux.engine.play_mode import PlayModeManager

            manager = PlayModeManager.instance()
            return getattr(manager, "_native_engine", None) if manager else None
        except Exception:
            return None

    def _remove_native_batch(self) -> None:
        self._remove_gpu_emitters()


__all__ = ["ParticleSystem"]
