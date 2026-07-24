"""Backend-neutral runtime metadata decoded from Particle HIR."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping

from Infernux.graph.types import AssetReference

from .asset import EmitterSettings
from .data_interface import (
    ParticleDataInterface,
    particle_data_interface_from_dict,
)
from .hir import ParticleOutputDescriptor, ParticleProgramHIR


class ParticleRuntimeMetadataError(ValueError):
    pass


@dataclass(frozen=True)
class ParticleEmitterRuntimeMetadata:
    stable_id: str
    settings: EmitterSettings
    outputs: tuple[ParticleOutputDescriptor, ...]
    data_interfaces: tuple[ParticleDataInterface, ...] = ()


@dataclass(frozen=True)
class ParticleProgramRuntimeMetadata:
    behavior_hash: str
    emitters: tuple[ParticleEmitterRuntimeMetadata, ...]

    @property
    def schedule(self) -> tuple[str, ...]:
        return tuple(emitter.stable_id for emitter in self.emitters)


def decode_particle_runtime_metadata(
    hir: ParticleProgramHIR | Mapping[str, Any],
) -> ParticleProgramRuntimeMetadata:
    if isinstance(hir, ParticleProgramHIR):
        by_id = {
            emitter.stable_id: ParticleEmitterRuntimeMetadata(
                emitter.stable_id,
                emitter.settings,
                tuple(emitter.render_plan.outputs),
                tuple(emitter.data_interfaces),
            )
            for emitter in hir.emitters
        }
        return _ordered_metadata(hir.behavior_hash, tuple(hir.schedule.emitter_ids), by_id)
    if not isinstance(hir, Mapping):
        raise ParticleRuntimeMetadataError(
            "Particle HIR must be a compiled program or artifact mapping"
        )

    behavior_hash = hir.get("behavior_hash")
    schedule_value = hir.get("schedule")
    emitters_value = hir.get("emitters")
    if (
        type(behavior_hash) is not str
        or type(schedule_value) is not list
        or not all(type(value) is str and value for value in schedule_value)
        or type(emitters_value) is not list
    ):
        raise ParticleRuntimeMetadataError("Particle artifact HIR header is invalid")

    by_id: dict[str, ParticleEmitterRuntimeMetadata] = {}
    for index, encoded in enumerate(emitters_value):
        location = f"particle artifact HIR emitters[{index}]"
        if type(encoded) is not dict:
            raise ParticleRuntimeMetadataError(f"{location} must be an object")
        stable_id = encoded.get("stable_id")
        settings_value = encoded.get("settings")
        render_plan = encoded.get("render_plan")
        data_interfaces_value = encoded.get("data_interfaces")
        if type(stable_id) is not str or not stable_id or stable_id in by_id:
            raise ParticleRuntimeMetadataError(f"{location} stable_id is invalid")
        if (
            type(settings_value) is not dict
            or type(render_plan) is not list
            or type(data_interfaces_value) is not list
        ):
            raise ParticleRuntimeMetadataError(f"{location} runtime metadata is invalid")
        try:
            settings = EmitterSettings.from_dict(settings_value, f"{location}.settings")
            outputs = tuple(
                _decode_output(value, f"{location}.render_plan[{output_index}]")
                for output_index, value in enumerate(render_plan)
            )
            data_interfaces = tuple(
                particle_data_interface_from_dict(
                    value, f"{location}.data_interfaces[{interface_index}]"
                )
                for interface_index, value in enumerate(data_interfaces_value)
            )
        except (TypeError, ValueError) as exc:
            raise ParticleRuntimeMetadataError(str(exc)) from exc
        if not outputs:
            raise ParticleRuntimeMetadataError(f"{location} requires a rendering output")
        by_id[stable_id] = ParticleEmitterRuntimeMetadata(
            stable_id, settings, outputs, data_interfaces
        )
    return _ordered_metadata(behavior_hash, tuple(schedule_value), by_id)


def _ordered_metadata(
    behavior_hash: str,
    schedule: tuple[str, ...],
    by_id: Mapping[str, ParticleEmitterRuntimeMetadata],
) -> ParticleProgramRuntimeMetadata:
    if len(schedule) != len(set(schedule)) or set(schedule) != set(by_id):
        raise ParticleRuntimeMetadataError(
            "Particle HIR schedule and emitter metadata do not match"
        )
    return ParticleProgramRuntimeMetadata(
        behavior_hash,
        tuple(by_id[stable_id] for stable_id in schedule),
    )


def _decode_output(value: Any, location: str) -> ParticleOutputDescriptor:
    if type(value) is not dict or set(value) != {
        "output_id",
        "output_type",
        "mesh",
        "material",
        "receive_scene_lighting",
        "receive_shadows",
        "cast_shadows",
        "soft_particles",
        "soft_distance",
        "sort_mode",
        "ribbon_uv_mode",
        "ribbon_uv_scale",
        "flipbook_columns",
        "flipbook_rows",
        "sprite_alignment",
        "alignment_axis",
    }:
        raise ParticleRuntimeMetadataError(f"{location} is invalid")
    if (
        type(value["output_id"]) is not str
        or not value["output_id"]
        or type(value["output_type"]) is not str
        or not value["output_type"]
        or type(value["receive_scene_lighting"]) is not bool
        or type(value["receive_shadows"]) is not bool
        or type(value["cast_shadows"]) is not bool
        or type(value["soft_particles"]) is not bool
        or type(value["soft_distance"]) not in {int, float}
        or not math.isfinite(float(value["soft_distance"]))
        or float(value["soft_distance"]) <= 0.0
        or type(value["sort_mode"]) is not str
        or type(value["ribbon_uv_mode"]) is not str
        or type(value["ribbon_uv_scale"]) not in {int, float}
        or not math.isfinite(float(value["ribbon_uv_scale"]))
        or float(value["ribbon_uv_scale"]) <= 0.0
        or type(value["flipbook_columns"]) is not int
        or type(value["flipbook_rows"]) is not int
        or not 1 <= value["flipbook_columns"] <= 4096
        or not 1 <= value["flipbook_rows"] <= 4096
        or value["flipbook_columns"] * value["flipbook_rows"] > 65536
        or value["sprite_alignment"]
        not in {"camera_plane", "camera_position", "axis", "velocity"}
        or type(value["alignment_axis"]) is not list
        or len(value["alignment_axis"]) != 3
        or not all(type(item) in {int, float} and math.isfinite(float(item)) for item in value["alignment_axis"])
        or (
            value["sprite_alignment"] == "axis"
            and sum(float(item) * float(item) for item in value["alignment_axis"]) <= 1.0e-12
        )
    ):
        raise ParticleRuntimeMetadataError(f"{location} fields are invalid")
    return ParticleOutputDescriptor(
        value["output_id"],
        value["output_type"],
        AssetReference.from_dict(value["mesh"]),
        AssetReference.from_dict(value["material"]),
        value["receive_scene_lighting"],
        value["receive_shadows"],
        value["cast_shadows"],
        value["soft_particles"],
        float(value["soft_distance"]),
        value["sort_mode"],
        value["ribbon_uv_mode"],
        float(value["ribbon_uv_scale"]),
        value["flipbook_columns"],
        value["flipbook_rows"],
        value["sprite_alignment"],
        tuple(float(item) for item in value["alignment_axis"]),
    )


__all__ = [
    "ParticleEmitterRuntimeMetadata",
    "ParticleProgramRuntimeMetadata",
    "ParticleRuntimeMetadataError",
    "decode_particle_runtime_metadata",
]
