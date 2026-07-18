"""Read-only migration from v1 VfxSystem assets to ParticleGraph."""

from __future__ import annotations

import hashlib

from Infernux.core.vfx_system import VfxSystem
from Infernux.graph.document import GraphDocument, GraphLinkRecord, GraphNodeRecord
from Infernux.graph.registry import PortKind
from Infernux.graph.types import AssetReference

from .asset import (
    EmitterSettings,
    ParticleEmitterAsset,
    ParticleGraphAsset,
    ScalarRange,
    default_stage_graph,
)


def migrate_vfx_system(system: VfxSystem, *, source_guid: str = "") -> ParticleGraphAsset:
    emitters = tuple(
        _migrate_emitter(emitter, index, source_guid)
        for index, emitter in enumerate(system.emitters)
    )
    stable_id = _stable_id(source_guid or system.file_path or system.name, "particle-graph")
    return ParticleGraphAsset(stable_id, system.name, emitters)


def _migrate_emitter(emitter, index: int, source_identity: str) -> ParticleEmitterAsset:
    from Infernux.vfx.compiler import VfxGraphCompiler

    compiled = VfxGraphCompiler().compile(emitter)
    spawn_rate = 0.0
    bursts = []
    lifetime = ScalarRange(5.0, 5.0)
    initial_speed = ScalarRange(0.0, 0.0)
    gravity = (0.0, 0.0, 0.0)
    init_nodes = [GraphNodeRecord("root.init", "particle.root.init")]
    init_links = []
    update_nodes = [GraphNodeRecord("root.update", "particle.root.update")]
    update_links = []

    previous = "root.init"
    for instruction in compiled.spawn:
        params = instruction.parameter_dict()
        if instruction.opcode == "spawn_rate":
            spawn_rate = float(params["rate"])
        elif instruction.opcode == "burst":
            from .asset import ParticleBurst

            bursts.append(ParticleBurst(0.0, int(params["count"])))
    for operation_index, instruction in enumerate(compiled.initialize):
        params = instruction.parameter_dict()
        if instruction.opcode == "set_velocity":
            value = tuple(float(component) for component in params["value"])
            speed = sum(component * component for component in value) ** 0.5
            initial_speed = ScalarRange(speed, speed)
            node_type = "particle.init.set_velocity"
            properties = {"value": list(value)}
        elif instruction.opcode == "set_lifetime":
            value = float(params["value"])
            lifetime = ScalarRange(value, value)
            node_type = "particle.init.set_lifetime"
            properties = {"value": value}
        else:
            continue
        uid = f"legacy.init.{operation_index}"
        init_nodes.append(GraphNodeRecord(uid, node_type, properties=properties))
        init_links.append(
            GraphLinkRecord(
                f"legacy.init.link.{operation_index}",
                previous,
                "out",
                uid,
                "in",
                PortKind.STREAM,
            )
        )
        previous = uid

    previous = "root.update"
    for operation_index, instruction in enumerate(compiled.update):
        if instruction.opcode != "gravity":
            continue
        strength = float(instruction.parameter_dict()["strength"])
        gravity = (0.0, strength, 0.0)
        uid = f"legacy.update.{operation_index}"
        update_nodes.append(
            GraphNodeRecord(uid, "particle.update.acceleration", properties={"value": list(gravity)})
        )
        update_links.append(
            GraphLinkRecord(
                f"legacy.update.link.{operation_index}",
                previous,
                "out",
                uid,
                "in",
                PortKind.STREAM,
            )
        )
        previous = uid

    rendering = default_stage_graph("rendering")
    sprite = rendering.nodes[1]
    rendering = GraphDocument(
        rendering.domain,
        (
            rendering.nodes[0],
            GraphNodeRecord(
                sprite.uid,
                sprite.type_id,
                sprite.position,
                {
                    "material": _legacy_asset_reference(emitter.renderer.material).to_dict(),
                    "receive_scene_lighting": False,
                    "receive_shadows": False,
                    "sort": "back_to_front",
                },
            ),
        ),
        rendering.links,
    )
    settings = EmitterSettings(
        capacity=compiled.capacity,
        spawn_rate=spawn_rate,
        bursts=tuple(bursts),
        lifetime=lifetime,
        initial_speed=initial_speed,
        gravity=gravity,
    )
    stable_id = _stable_id(source_identity or emitter.name, f"emitter-{index}")
    return ParticleEmitterAsset(
        stable_id=stable_id,
        name=emitter.name,
        settings=settings,
        init=GraphDocument("particle.init", tuple(init_nodes), tuple(init_links)),
        update=GraphDocument("particle.update", tuple(update_nodes), tuple(update_links)),
        rendering=rendering,
    )


def _stable_id(identity: str, suffix: str) -> str:
    return hashlib.sha256(f"{identity}:{suffix}".encode("utf-8")).hexdigest()[:32]


def _legacy_asset_reference(value) -> AssetReference:
    guid = str(getattr(value, "guid", "") or "").strip()
    path_hint = str(getattr(value, "path_hint", "") or "").strip()
    if not guid and not path_hint:
        raw = str(value or "").strip()
        if "/" in raw or "\\" in raw or raw.lower().endswith(".mat"):
            path_hint = raw
        else:
            guid = raw
    return AssetReference(guid, path_hint)


__all__ = ["migrate_vfx_system"]
