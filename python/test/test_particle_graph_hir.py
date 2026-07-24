from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from Infernux.graph import GraphDocument, GraphLinkRecord, GraphNodeRecord, PortKind
from Infernux.particle import (
    EmitterSettings,
    ExecutionTarget,
    KernelCompileError,
    ParticleCompileError,
    ParticleEmitterAsset,
    ParticleAttribute,
    ParticleEventField,
    ParticleEventRoute,
    ParticleEventType,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleGraphSchemaError,
    ParticleKernelLowerer,
    ParticleStage,
    ParticleScriptCompiler,
    ParticleScriptError,
    ParticleArtifactError,
    ParticleArtifactRegistry,
    ParticleRuntimeMetadataError,
    PointCache,
    SdfVolume,
    SimulationSpace,
    VectorField,
    decode_particle_runtime_metadata,
)
from Infernux.graph import AssetReference, CoordinateSpace, TypeRef, ValueType
from Infernux.particle.nodes import particle_event_output_type_id


def test_default_particle_graph_has_three_immutable_stage_roots_and_output():
    asset = ParticleGraphAsset()
    emitter = asset.emitters[0]

    assert emitter.init.nodes[0].uid == "root.init"
    assert emitter.update.nodes[0].uid == "root.update"
    assert emitter.rendering.nodes[0].uid == "root.rendering"
    assert emitter.rendering.nodes[1].type_id == "particle.output.sprite"

    restored = ParticleGraphAsset.from_json(asset.canonical_json())
    assert restored == asset
    assert restored.semantic_hash() == asset.semantic_hash()
    hir = ParticleGraphCompiler().compile(asset)
    assert "builtin.orientation" not in {
        attribute.stable_id for attribute in hir.emitters[0].attributes
    }


def test_particle_event_routes_compile_to_stable_typed_dense_abi():
    source = ParticleEmitterAsset(stable_id="source", name="Source")
    target = ParticleEmitterAsset(stable_id="target", name="Target")
    impact = ParticleEventType(
        "impact",
        "Impact",
        4096,
        (
            ParticleEventField(
                "position",
                "Position",
                TypeRef(ValueType.VEC3, CoordinateSpace.WORLD),
                [0.0, 0.0, 0.0],
            ),
            ParticleEventField(
                "color",
                "Color",
                TypeRef(ValueType.COLOR),
                [1.0, 1.0, 1.0, 1.0],
            ),
        ),
    )
    route = ParticleEventRoute(
        "source-impact-target",
        "impact",
        "source",
        "update",
        "target",
        3,
    )
    asset = ParticleGraphAsset(
        stable_id="event-graph",
        emitters=(source, target),
        event_types=(impact,),
        event_routes=(route,),
    )

    restored = ParticleGraphAsset.from_json(asset.canonical_json())
    assert restored == asset
    program = ParticleGraphCompiler().compile(asset)
    assert len(program.events.event_types) == 1
    assert len(program.events.routes) == 1
    event_type = program.events.event_types[0]
    lowered_route = program.events.routes[0]
    assert event_type.payload_stride_words == 7
    assert [(field.word_offset, field.word_count) for field in event_type.fields] == [
        (0, 3),
        (3, 4),
    ]
    assert event_type.stable_type_hash != 0
    assert program.events.event_abi_u64 != 0
    assert lowered_route.source_emitter_index == 0
    assert lowered_route.target_emitter_index == 1
    assert lowered_route.event_type_index == 0
    assert lowered_route.source_stage is ParticleStage.UPDATE
    assert lowered_route.spawn_count == 3

    changed = replace(
        asset,
        event_types=(replace(impact, capacity_per_step=8192),),
    )
    assert (
        ParticleGraphCompiler().compile(changed).events.event_abi_hash
        != program.events.event_abi_hash
    )


def test_particle_event_routes_keep_distinct_channels_for_the_same_endpoints():
    source = ParticleEmitterAsset(stable_id="source", name="Source")
    target = ParticleEmitterAsset(stable_id="target", name="Target")
    event_type = ParticleEventType("impact", "Impact", 32)
    routes = (
        ParticleEventRoute(
            "impact-after-init", "impact", "source", "init", "target", 1
        ),
        ParticleEventRoute(
            "impact-after-update", "impact", "source", "update", "target", 4
        ),
    )

    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(
            emitters=(source, target),
            event_types=(event_type,),
            event_routes=routes,
        )
    )
    kernel = ParticleKernelLowerer().lower(hir)

    assert [route.stable_id for route in hir.events.routes] == [
        "impact-after-init",
        "impact-after-update",
    ]
    assert [route.source_stage for route in kernel.events.routes] == [
        ParticleStage.INIT,
        ParticleStage.UPDATE,
    ]
    assert [route.spawn_count for route in kernel.events.routes] == [1, 4]


def test_particle_event_routes_reject_implicit_feedback_cycles():
    first = ParticleEmitterAsset(stable_id="first", name="First")
    second = ParticleEmitterAsset(stable_id="second", name="Second")
    event_type = ParticleEventType("event", "Event", 16)

    with pytest.raises(ParticleGraphSchemaError, match="explicit delay"):
        ParticleGraphAsset(
            emitters=(first, second),
            event_types=(event_type,),
            event_routes=(
                ParticleEventRoute("first-second", "event", "first", "update", "second"),
                ParticleEventRoute("second-first", "event", "second", "update", "first"),
            ),
        )


def _event_output_stage(route_id: str, source_stage: str = "update") -> GraphDocument:
    return GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "event.output",
                particle_event_output_type_id(route_id, source_stage),
                properties={"condition": True},
            ),
        ),
        links=(
            GraphLinkRecord(
                "event.stream",
                "root.update",
                "out",
                "event.output",
                "in",
                PortKind.STREAM,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("route", "match"),
    (
        (None, "unknown node type"),
        (
            ParticleEventRoute("route", "event", "other", "update", "source"),
            "belongs to emitter",
        ),
        (
            ParticleEventRoute("route", "event", "source", "init", "other"),
            "belongs to the init stage",
        ),
    ),
)
def test_particle_event_output_requires_a_matching_source_route(route, match):
    route_id = "missing" if route is None else route.stable_id
    node_stage = route.source_stage if route is not None else "update"
    source = ParticleEmitterAsset(
        stable_id="source",
        update=_event_output_stage(route_id, node_stage),
    )
    other = ParticleEmitterAsset(stable_id="other")
    asset = ParticleGraphAsset(
        emitters=(source, other),
        event_types=(ParticleEventType("event", "Event", 16),),
        event_routes=() if route is None else (route,),
    )

    with pytest.raises(ParticleCompileError, match=match):
        ParticleGraphCompiler().compile(asset)


def test_particle_event_schema_fingerprint_invalidates_stage_expression_programs():
    source = ParticleEmitterAsset(
        stable_id="source",
        update=_event_output_stage("route"),
    )
    target = ParticleEmitterAsset(stable_id="target")
    route = ParticleEventRoute("route", "event", "source", "update", "target")

    def compile_default(default: float):
        event_type = ParticleEventType(
            "event",
            "Event",
            16,
            (ParticleEventField("weight", "Weight", TypeRef(ValueType.F32), default),),
        )
        return ParticleGraphCompiler().compile(
            ParticleGraphAsset(
                emitters=(source, target),
                event_types=(event_type,),
                event_routes=(route,),
            )
        )

    first = compile_default(1.0)
    second = compile_default(2.0)
    assert first.emitters[0].update.expressions.semantic_hash != (
        second.emitters[0].update.expressions.semantic_hash
    )


def test_particle_graph_persists_only_builtin_default_overrides_and_custom_attributes():
    attributes = list(ParticleEmitterAsset().attributes)
    color_index = next(
        index
        for index, attribute in enumerate(attributes)
        if attribute.stable_id == "builtin.color"
    )
    color = attributes[color_index]
    attributes[color_index] = ParticleAttribute(
        color.stable_id,
        color.name,
        color.value_type,
        [1.0, 0.25, 0.1, 1.0],
    )
    attributes.append(
        ParticleAttribute("custom.temperature", "temperature", TypeRef(ValueType.F32), 3.5)
    )
    asset = ParticleGraphAsset(
        emitters=(ParticleEmitterAsset(attributes=tuple(attributes)),)
    )

    document = asset.to_dict()
    emitter_document = document["emitters"][0]
    assert "attributes" not in emitter_document
    assert emitter_document["attribute_defaults"] == {
        "builtin.color": [1.0, 0.25, 0.1, 1.0]
    }
    assert [item["stable_id"] for item in emitter_document["custom_attributes"]] == [
        "custom.temperature"
    ]
    assert ParticleGraphAsset.from_dict(document) == asset

    stale = copy.deepcopy(document)
    stale["emitters"][0]["attributes"] = stale["emitters"][0].pop(
        "custom_attributes"
    )
    with pytest.raises(ParticleGraphSchemaError, match="keys mismatch"):
        ParticleGraphAsset.from_dict(stale)


@pytest.mark.parametrize(
    "value_type, default",
    [
        (ValueType.STRING, "text"),
        (ValueType.ASSET_REF, {"guid": "asset-guid"}),
        (
            ValueType.CURVE,
            {
                "keys": [
                    {
                        "time": 0.0,
                        "value": 1.0,
                        "in_tangent": 0.0,
                        "out_tangent": 0.0,
                    }
                ],
                "pre_wrap": "clamp",
                "post_wrap": "clamp",
            },
        ),
        (
            ValueType.GRADIENT,
            {
                "keys": [{"time": 0.0, "color": [1.0, 1.0, 1.0, 1.0]}],
                "mode": "linear",
            },
        ),
    ],
)
def test_particle_attributes_reject_property_only_types(value_type, default):
    with pytest.raises(ParticleGraphSchemaError, match="numeric storage type"):
        ParticleAttribute("custom.invalid", "invalid", TypeRef(value_type), default)


def test_particle_graph_rejects_unknown_field():
    value = ParticleGraphAsset(stable_id="current-particle").to_dict()
    value["unknown"] = 1

    with pytest.raises(ParticleGraphSchemaError, match="keys mismatch"):
        ParticleGraphAsset.from_dict(value)


def test_particle_data_interfaces_round_trip_with_stable_identity_and_space():
    emitter = ParticleEmitterAsset(
        stable_id="data-emitter",
        data_interfaces=(
            VectorField(
                stable_id="wind-field",
                name="Wind",
                texture=AssetReference(path_hint="Assets/Fields/Wind.vectorfield"),
                space="world",
                boundary="repeat",
                filtering="linear",
                vector_scale=2.5,
            ),
            PointCache(
                stable_id="morph-points",
                name="Morph Points",
                cache=AssetReference(path_hint="Assets/Caches/Face.pointcache"),
                space="emitter_local",
                id_channel="stable_id",
            ),
            SdfVolume(
                stable_id="collision-field",
                name="Collision",
                texture=AssetReference(path_hint="Assets/Fields/Collision.inxsdf"),
                distance_scale=1.5,
            ),
        ),
    )
    asset = ParticleGraphAsset(stable_id="data-graph", emitters=(emitter,))

    restored = ParticleGraphAsset.from_json(asset.canonical_json())
    hir = ParticleGraphCompiler().compile(restored)

    assert restored == asset
    assert [interface.stable_id for interface in hir.emitters[0].data_interfaces] == [
        "wind-field",
        "morph-points",
        "collision-field",
    ]
    assert hir.emitters[0].data_interfaces[0].boundary.value == "repeat"

    with pytest.raises(ParticleGraphSchemaError, match="stable ids must be unique"):
        ParticleEmitterAsset(
            data_interfaces=(
                VectorField(stable_id="duplicate"),
                PointCache(stable_id="duplicate"),
            )
        )


@pytest.mark.parametrize("stage", ["init", "update", "rendering"])
def test_particle_graph_rejects_deleted_or_replaced_stage_root(stage):
    value = ParticleGraphAsset().to_dict()
    stage_document = value["emitters"][0]["stages"][stage]
    stage_document["nodes"] = [
        node for node in stage_document["nodes"] if not node["type_id"].startswith("particle.root.")
    ]
    stage_document["links"] = [
        link
        for link in stage_document["links"]
        if not link["source_node"].startswith("root.")
        and not link["target_node"].startswith("root.")
    ]

    with pytest.raises(ParticleGraphSchemaError, match="mandatory root"):
        ParticleGraphAsset.from_dict(value)


def test_particle_graph_compiler_builds_multi_emitter_schedule_and_render_plan():
    first = ParticleEmitterAsset(
        stable_id="smoke",
        name="Smoke",
        settings=EmitterSettings(
            capacity=100_000,
            target=ExecutionTarget.GPU,
            simulation_space=SimulationSpace.WORLD,
            spawn_rate=20_000.0,
        ),
    )
    rendering = first.rendering
    sprite = rendering.nodes[1]
    first = ParticleEmitterAsset(
        stable_id=first.stable_id,
        name=first.name,
        settings=first.settings,
        attributes=first.attributes,
        init=first.init,
        update=first.update,
        rendering=GraphDocument(
            rendering.domain,
            (
                rendering.nodes[0],
                GraphNodeRecord(
                    sprite.uid,
                    sprite.type_id,
                    properties={
                        "material": AssetReference(guid="six-way-smoke-guid").to_dict(),
                        "receive_scene_lighting": True,
                        "receive_shadows": True,
                        "soft_particles": True,
                        "soft_distance": 0.35,
                        "sort": "back_to_front",
                    },
                ),
            ),
            rendering.links,
        ),
    )
    sparks = ParticleEmitterAsset(stable_id="sparks", name="Sparks")
    program = ParticleGraphCompiler().compile(
        ParticleGraphAsset(stable_id="fire", name="Fire", emitters=(first, sparks))
    )

    assert program.schedule.emitter_ids == ("smoke", "sparks")
    smoke = program.emitters[0]
    assert smoke.init.stage is ParticleStage.INIT
    assert smoke.init.operations[0].opcode == "emitter.sample_shape"
    assert smoke.update.operations[0].opcode == "integrate.acceleration"
    assert smoke.render_plan.outputs[0].material == AssetReference(guid="six-way-smoke-guid")
    assert smoke.render_plan.outputs[0].receive_scene_lighting is True
    assert smoke.render_plan.outputs[0].receive_shadows is True
    assert smoke.render_plan.outputs[0].cast_shadows is False
    assert smoke.render_plan.outputs[0].soft_particles is True
    assert smoke.render_plan.outputs[0].soft_distance == pytest.approx(0.35)


def test_particle_graph_compiler_rejects_rendering_without_output():
    emitter = ParticleEmitterAsset(
        rendering=GraphDocument(
            "particle.rendering",
            (GraphNodeRecord("root.rendering", "particle.root.rendering"),),
        )
    )
    asset = ParticleGraphAsset(emitters=(emitter,))

    with pytest.raises(ParticleCompileError, match="at least one output"):
        ParticleGraphCompiler().compile(asset)


def test_particle_sprite_output_rejects_static_mesh_shadow_property():
    rendering = GraphDocument(
        "particle.rendering",
        nodes=(
            GraphNodeRecord("root.rendering", "particle.root.rendering"),
            GraphNodeRecord(
                "output.sprite",
                "particle.output.sprite",
                properties={"cast_shadows": True},
            ),
        ),
        links=(
            GraphLinkRecord(
                "root-to-sprite",
                "root.rendering",
                "out",
                "output.sprite",
                "in",
                PortKind.STREAM,
            ),
        ),
    )

    with pytest.raises(ParticleCompileError, match="unknown properties"):
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(rendering=rendering),))
        )


def test_particle_sprite_output_compiles_valid_flipbook_grid_and_rejects_invalid_grid():
    def compile_grid(columns, rows):
        rendering = GraphDocument(
            "particle.rendering",
            nodes=(
                GraphNodeRecord("root.rendering", "particle.root.rendering"),
                GraphNodeRecord(
                    "output.sprite",
                    "particle.output.sprite",
                    properties={"flipbook_columns": columns, "flipbook_rows": rows},
                ),
            ),
            links=(
                GraphLinkRecord(
                    "root-to-sprite",
                    "root.rendering",
                    "out",
                    "output.sprite",
                    "in",
                    PortKind.STREAM,
                ),
            ),
        )
        return ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(rendering=rendering),))
        )

    output = compile_grid(8, 4).emitters[0].render_plan.outputs[0]
    assert output.flipbook_columns == 8
    assert output.flipbook_rows == 4

    with pytest.raises(ParticleCompileError, match="flipbook grid"):
        compile_grid(0, 4)
    with pytest.raises(ParticleCompileError, match="flipbook grid"):
        compile_grid(4096, 4096)


def test_particle_graph_compiles_static_mesh_output_with_explicit_asset():
    rendering = GraphDocument(
        "particle.rendering",
        nodes=(
            GraphNodeRecord("root.rendering", "particle.root.rendering"),
            GraphNodeRecord(
                "output.mesh",
                "particle.output.mesh",
                properties={
                    "mesh": AssetReference(
                        guid="mesh-guid", path_hint="Assets/Models/Debris.fbx"
                    ).to_dict(),
                    "material": AssetReference(guid="debris-material-guid").to_dict(),
                    "receive_scene_lighting": False,
                    "receive_shadows": False,
                    "sort": "none",
                },
            ),
        ),
        links=(
            GraphLinkRecord(
                "root-to-mesh",
                "root.rendering",
                "out",
                "output.mesh",
                "in",
                PortKind.STREAM,
            ),
        ),
    )

    program = ParticleGraphCompiler().compile(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(rendering=rendering),))
    )
    compiled_emitter = program.emitters[0]
    output = compiled_emitter.render_plan.outputs[0]

    assert output.output_type == "mesh"
    assert output.mesh == AssetReference(
        guid="mesh-guid", path_hint="Assets/Models/Debris.fbx"
    )
    assert output.material == AssetReference(guid="debris-material-guid")
    assert output.soft_particles is False
    assert output.cast_shadows is False
    assert output.sort_mode == "none"
    orientation = next(
        attribute
        for attribute in compiled_emitter.attributes
        if attribute.stable_id == "builtin.orientation"
    )
    assert orientation.value_type == TypeRef(ValueType.VEC3)
    assert orientation.default == [0.0, 0.0, 0.0]


def test_particle_graph_rejects_static_mesh_output_without_mesh_asset():
    rendering = GraphDocument(
        "particle.rendering",
        nodes=(
            GraphNodeRecord("root.rendering", "particle.root.rendering"),
            GraphNodeRecord("output.mesh", "particle.output.mesh"),
        ),
        links=(
            GraphLinkRecord(
                "root-to-mesh",
                "root.rendering",
                "out",
                "output.mesh",
                "in",
                PortKind.STREAM,
            ),
        ),
    )

    with pytest.raises(ParticleCompileError, match="requires a mesh asset"):
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(rendering=rendering),))
        )


def test_particle_graph_compiles_lit_shadow_receiving_static_mesh_output():
    rendering = GraphDocument(
        "particle.rendering",
        nodes=(
            GraphNodeRecord("root.rendering", "particle.root.rendering"),
            GraphNodeRecord(
                "output.mesh",
                "particle.output.mesh",
                properties={
                    "mesh": AssetReference(guid="mesh-guid").to_dict(),
                    "receive_scene_lighting": True,
                    "receive_shadows": True,
                    "cast_shadows": True,
                },
            ),
        ),
        links=(
            GraphLinkRecord(
                "root-to-mesh",
                "root.rendering",
                "out",
                "output.mesh",
                "in",
                PortKind.STREAM,
            ),
        ),
    )

    graph_asset = ParticleGraphAsset(
        emitters=(ParticleEmitterAsset(rendering=rendering),)
    )
    restored_graph = ParticleGraphAsset.from_json(graph_asset.canonical_json())
    output = ParticleGraphCompiler().compile(restored_graph).emitters[0].render_plan.outputs[0]

    assert output.receive_scene_lighting is True
    assert output.receive_shadows is True
    assert output.cast_shadows is True


def test_particle_graph_rejects_sorted_static_mesh_output():
    properties = {
        "mesh": AssetReference(guid="mesh-guid").to_dict(),
        "sort": "back_to_front",
    }
    rendering = GraphDocument(
        "particle.rendering",
        nodes=(
            GraphNodeRecord("root.rendering", "particle.root.rendering"),
            GraphNodeRecord(
                "output.mesh", "particle.output.mesh", properties=properties
            ),
        ),
        links=(
            GraphLinkRecord(
                "root-to-mesh",
                "root.rendering",
                "out",
                "output.mesh",
                "in",
                PortKind.STREAM,
            ),
        ),
    )

    with pytest.raises(
        ParticleCompileError,
        match="currently supports unsorted, non-soft rendering only",
    ):
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(rendering=rendering),))
        )


def test_particle_graph_stream_order_lowers_to_stage_operations():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord(
                "velocity",
                "particle.attribute.set_velocity",
                properties={"value": [0.0, 2.0, 0.0]},
            ),
            GraphNodeRecord(
                "lifetime",
                "particle.attribute.set_lifetime",
                properties={"value": 3.0},
            ),
        ),
        links=(
            GraphLinkRecord("l1", "root.init", "out", "velocity", "in", PortKind.STREAM),
            GraphLinkRecord("l2", "velocity", "out", "lifetime", "in", PortKind.STREAM),
        ),
    )
    emitter = ParticleEmitterAsset(init=init)
    hir = ParticleGraphCompiler().compile(ParticleGraphAsset(emitters=(emitter,))).emitters[0]

    assert [operation.opcode for operation in hir.init.operations] == [
        "emitter.sample_shape",
        "attribute.set_velocity",
        "attribute.set_lifetime",
    ]


def test_particle_stage_value_links_use_common_typed_expression_ir():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("gravity", "particle.update.acceleration"),
            GraphNodeRecord(
                "a",
                "common.constant.vec3",
                properties={"value": [0.0, -4.0, 0.0]},
            ),
            GraphNodeRecord(
                "b",
                "common.constant.vec3",
                properties={"value": [1.0, 0.0, 0.0]},
            ),
            GraphNodeRecord("add", "common.math.add"),
            GraphNodeRecord("normalize", "common.vector.normalize"),
        ),
        links=(
            GraphLinkRecord("s1", "root.update", "out", "gravity", "in", PortKind.STREAM),
            GraphLinkRecord("v1", "a", "value", "add", "a"),
            GraphLinkRecord("link_b", "b", "value", "add", "b"),
            GraphLinkRecord("v3", "add", "result", "normalize", "value"),
            GraphLinkRecord("v4", "normalize", "result", "gravity", "value"),
        ),
    )
    emitter = ParticleEmitterAsset(update=update)
    hir = ParticleGraphCompiler().compile(ParticleGraphAsset(emitters=(emitter,))).emitters[0]

    assert [instruction.opcode for instruction in hir.update.expressions.instructions] == [
        "constant",
        "constant",
        "add",
        "normalize",
    ]
    assert hir.update.operations[-1].value_bindings == (("value", "normalize.result"),)


def test_particle_update_can_author_color_and_size_over_lifetime():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("set-color", "particle.attribute.set_color"),
            GraphNodeRecord("set-size", "particle.attribute.set_size"),
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
                properties={"value": [0.1, 0.0, 0.0, 0.0]},
            ),
            GraphNodeRecord("color-over-life", "common.math.lerp"),
            GraphNodeRecord("size-over-life", "common.math.lerp", properties={"a": 1.0, "b": 0.0}),
        ),
        links=(
            GraphLinkRecord("stream-color", "root.update", "out", "set-color", "in", PortKind.STREAM),
            GraphLinkRecord("stream-size", "set-color", "out", "set-size", "in", PortKind.STREAM),
            GraphLinkRecord("age-divide", "age", "value", "normalized-age", "a"),
            GraphLinkRecord("life-divide", "lifetime", "value", "normalized-age", "b"),
            GraphLinkRecord("color-a", "start-color", "value", "color-over-life", "a"),
            GraphLinkRecord("color-b", "end-color", "value", "color-over-life", "b"),
            GraphLinkRecord("color-t", "normalized-age", "result", "color-over-life", "t"),
            GraphLinkRecord("size-t", "normalized-age", "result", "size-over-life", "t"),
            GraphLinkRecord("set-color-value", "color-over-life", "result", "set-color", "value"),
            GraphLinkRecord("set-size-value", "size-over-life", "result", "set-size", "value"),
        ),
    )

    emitter = ParticleEmitterAsset(update=update)
    program = ParticleGraphCompiler().compile(ParticleGraphAsset(emitters=(emitter,)))
    hir = program.emitters[0]
    kernel = ParticleKernelLowerer().lower(program).emitters[0]

    assert [operation.opcode for operation in hir.update.operations[-2:]] == [
        "attribute.set_color",
        "attribute.set_size",
    ]
    assert {instruction.opcode for instruction in kernel.update.instructions} >= {
        "divide",
        "lerp",
        "store_attribute",
    }
    assert {"builtin.color", "builtin.size"} <= set(kernel.update.written_attributes)


def test_particle_behavior_hash_ignores_graph_node_identity_and_layout():
    def make_update(prefix: str, offset: float) -> GraphDocument:
        return GraphDocument(
            "particle.update",
            nodes=(
                GraphNodeRecord("root.update", "particle.root.update"),
                GraphNodeRecord(f"{prefix}.gravity", "particle.update.acceleration"),
                GraphNodeRecord(
                    f"{prefix}.value",
                    "common.constant.vec3",
                    (offset, offset),
                    {"value": [0.0, -1.0, 0.0]},
                ),
            ),
            links=(
                GraphLinkRecord(
                    f"{prefix}.stream",
                    "root.update",
                    "out",
                    f"{prefix}.gravity",
                    "in",
                    PortKind.STREAM,
                ),
                GraphLinkRecord(
                    f"{prefix}.value-link",
                    f"{prefix}.value",
                    "value",
                    f"{prefix}.gravity",
                    "value",
                ),
            ),
        )

    first = ParticleGraphAsset(
        stable_id="graph",
        emitters=(ParticleEmitterAsset(stable_id="emitter", update=make_update("first", 0.0)),),
    )
    second = ParticleGraphAsset(
        stable_id="graph",
        emitters=(ParticleEmitterAsset(stable_id="emitter", update=make_update("second", 500.0)),),
    )
    first_hir = ParticleGraphCompiler().compile(first)
    second_hir = ParticleGraphCompiler().compile(second)

    assert first_hir.semantic_hash != second_hir.semantic_hash
    assert first_hir.behavior_hash == second_hir.behavior_hash


def test_particle_graph_schema_is_strict_and_semantic_hash_ignores_positions():
    asset = ParticleGraphAsset()
    value = asset.to_dict()
    value["future"] = True
    with pytest.raises(ParticleGraphSchemaError, match="keys mismatch"):
        ParticleGraphAsset.from_dict(value)

    moved = copy.deepcopy(asset.to_dict())
    moved["emitters"][0]["stages"]["rendering"]["nodes"][0]["position"] = [500.0, 200.0]
    restored = ParticleGraphAsset.from_dict(moved)
    assert restored.semantic_hash() == asset.semantic_hash()


def test_particle_python_construction_cannot_bypass_schema_invariants():
    with pytest.raises(ParticleGraphSchemaError, match="exactly 3"):
        ParticleAttribute("custom.wind", "wind", TypeRef(ValueType.VEC3), [1.0, 2.0])
    with pytest.raises(ParticleGraphSchemaError, match="bursts"):
        EmitterSettings(bursts=(object(),))
    with pytest.raises(ParticleGraphSchemaError, match="emitters are invalid"):
        ParticleGraphAsset(emitters=(object(),))


def test_particle_material_reference_uses_strict_guid_and_path_hint_shape():
    value = ParticleGraphAsset().to_dict()
    material = value["emitters"][0]["stages"]["rendering"]["nodes"][1]["properties"]
    material["material"] = "ambiguous-material"

    restored = ParticleGraphAsset.from_dict(value)
    with pytest.raises(ParticleCompileError, match="guid and path_hint"):
        ParticleGraphCompiler().compile(restored)


PARTICLE_SCRIPT_SOURCE = '''\
from Infernux.particle import AssetReference, ParticleScript, ParticleEmitter, EmitterSettings, VectorField, PointCache

class SmokeGraph(ParticleScript):
    stable_id = "smoke-graph"

    class Smoke(ParticleEmitter):
        stable_id = "smoke"
        settings = EmitterSettings(
            capacity=100000,
            target="gpu",
            simulation_space="world",
            spawn_rate=20000.0,
        )
        data_interfaces = (
            VectorField(
                stable_id="wind-field",
                name="Wind",
                texture=AssetReference(path_hint="Assets/Fields/Wind.vectorfield"),
                space="world",
                boundary="repeat",
            ),
            PointCache(
                stable_id="morph-points",
                name="Morph Points",
                cache=AssetReference(path_hint="Assets/Caches/Face.pointcache"),
                space="emitter_local",
                id_channel="stable_id",
            ),
        )

        def init(self, ctx, particles):
            particles.set_velocity((0.0, 1.0, 0.0))
            particles.set_lifetime(6.0)
            particles.set_rotation(0.25)

        def update(self, ctx, particles):
            particles.acceleration((0.0, -0.2, 0.0))
            particles.rotate(180.0)

        def rendering(self, ctx, particles):
            particles.set_lifetime(8.0)
            particles.set_flipbook_frame(3.5)
            particles.sprite(
                material=AssetReference(guid="six-way-smoke-guid"),
                receive_scene_lighting=True,
                receive_shadows=True,
                sort="back_to_front",
            )
'''


def test_particle_script_compiles_without_execution_to_same_hir_contract():
    compiler = ParticleScriptCompiler()
    asset = compiler.parse(PARTICLE_SCRIPT_SOURCE, source_name="Smoke.particle.py")
    program = compiler.compile(PARTICLE_SCRIPT_SOURCE, source_name="Smoke.particle.py")
    emitter = program.emitters[0]

    assert asset.stable_id == "smoke-graph"
    assert program.schedule.emitter_ids == ("smoke",)
    assert [operation.opcode for operation in emitter.init.operations] == [
        "emitter.sample_shape",
        "attribute.set_velocity",
        "attribute.set_lifetime",
        "attribute.set_rotation",
    ]
    assert [operation.opcode for operation in emitter.update.operations[-2:]] == [
        "integrate.acceleration",
        "integrate.angular_velocity",
    ]
    assert [operation.opcode for operation in emitter.rendering.operations[:2]] == [
        "attribute.set_lifetime",
        "attribute.set_flipbook_frame",
    ]
    assert emitter.render_plan.outputs[0].receive_scene_lighting is True
    assert emitter.render_plan.outputs[0].receive_shadows is True
    assert [interface.stable_id for interface in emitter.data_interfaces] == [
        "wind-field",
        "morph-points",
    ]
    assert program.behavior_hash == ParticleGraphCompiler().compile(asset).behavior_hash


PARTICLE_SCRIPT_EVENT_SOURCE = '''\
from Infernux.particle import (
    ParticleScript, ParticleEmitter, EmitterSettings,
    EventField, EventType, EventRoute,
)

class ImpactGraph(ParticleScript):
    stable_id = "impact-graph"
    event_types = (
        EventType(
            stable_id="impact",
            name="Impact",
            capacity_per_step=128,
            fields=(
                EventField("energy", "Energy", "f32", 1.0),
                EventField("tint", "Tint", "color", (1.0, 1.0, 1.0, 1.0)),
            ),
        ),
    )
    event_routes = (
        EventRoute(
            stable_id="source-to-sparks",
            event_type_id="impact",
            source_emitter_id="source",
            source_stage="update",
            target_emitter_id="sparks",
            spawn_count=3,
        ),
    )

    class Source(ParticleEmitter):
        stable_id = "source"
        settings = EmitterSettings(capacity=1024)

        def init(self, ctx, particles):
            particles.set_size(2.0)

        def update(self, ctx, particles):
            particles.emit_event(
                route="source-to-sparks",
                condition=particles.age >= particles.lifetime,
                payload={
                    "energy": particles.size,
                    "tint": particles.color,
                },
            )

        def rendering(self, ctx, particles):
            particles.sprite()

    class Sparks(ParticleEmitter):
        stable_id = "sparks"
        settings = EmitterSettings(capacity=2048)

        def init(self, ctx, particles):
            particles.set_size(ctx.event_payload(
                route="source-to-sparks", field="energy"
            ))
            particles.set_color(ctx.event_payload(
                route="source-to-sparks", field="tint"
            ))

        def update(self, ctx, particles):
            pass

        def rendering(self, ctx, particles):
            particles.sprite()
'''


def test_particle_script_typed_events_lower_through_the_graph_event_abi():
    compiler = ParticleScriptCompiler()
    asset = compiler.parse(
        PARTICLE_SCRIPT_EVENT_SOURCE, source_name="Impact.particle.py"
    )
    hir = ParticleGraphCompiler().compile(asset)
    kernel = ParticleKernelLowerer().lower(hir)

    assert [value.stable_id for value in asset.event_types] == ["impact"]
    assert [value.stable_id for value in asset.event_routes] == [
        "source-to-sparks"
    ]
    assert hir.events.routes[0].spawn_count == 3
    assert kernel.events.routes[0].source_stage == "update"
    assert any(
        instruction.opcode == "event_append"
        for instruction in kernel.emitters[0].update.instructions
    )
    payloads = [
        instruction
        for instruction in kernel.emitters[1].init.instructions
        if instruction.opcode == "event_payload"
    ]
    assert [value.result_type.value_type.value for value in payloads] == [
        "f32",
        "color",
    ]


@pytest.mark.parametrize(
    ("source", "message"),
    [
        (
            PARTICLE_SCRIPT_EVENT_SOURCE.replace(
                'source_stage="update"', 'source_stage="init"'
            ),
            "does not originate",
        ),
        (
            PARTICLE_SCRIPT_EVENT_SOURCE.replace(
                '"energy": particles.size,', '"missing": particles.size,'
            ),
            "unknown event payload field",
        ),
        (
            PARTICLE_SCRIPT_EVENT_SOURCE.replace(
                "        def update(self, ctx, particles):\n            pass",
                '''        def update(self, ctx, particles):
            particles.set_size(ctx.event_payload(
                route="source-to-sparks", field="energy"
            ))''',
            ),
            "event_payload is only available in Init",
        ),
    ],
)
def test_particle_script_typed_events_reject_invalid_routes_and_payloads(
    source, message
):
    with pytest.raises(ParticleScriptError, match=message):
        ParticleScriptCompiler().parse(source, source_name="InvalidEvent.particle.py")


def test_particle_script_static_mesh_output_matches_graph_contract():
    source = PARTICLE_SCRIPT_SOURCE.replace(
        '''particles.sprite(
                material=AssetReference(guid="six-way-smoke-guid"),
                receive_scene_lighting=True,
                receive_shadows=True,
                sort="back_to_front",
            )''',
        '''particles.mesh(
                mesh=AssetReference(guid="mesh-guid", path_hint="Assets/Models/Debris.fbx"),
                material=AssetReference(guid="debris-material-guid"),
                cast_shadows=True,
                sort="none",
            )''',
    )
    source = source.replace(
        "particles.set_rotation(0.25)",
        "particles.set_orientation((10.0, 20.0, 30.0))\n            particles.set_scale((2.0, 0.5, 1.5))",
    ).replace(
        "particles.rotate(180.0)",
        "particles.rotate_orientation((90.0, 180.0, 270.0))",
    )

    emitter = ParticleScriptCompiler().compile(
        source, source_name="MeshOutput.particle.py"
    ).emitters[0]
    output = emitter.render_plan.outputs[0]

    assert output.output_type == "mesh"
    assert output.mesh.guid == "mesh-guid"
    assert output.cast_shadows is True
    default_output = ParticleScriptCompiler().compile(
        source.replace("                cast_shadows=True,\n", ""),
        source_name="MeshOutputDefault.particle.py",
    ).emitters[0].render_plan.outputs[0]
    assert default_output.cast_shadows is False
    graph_program = ParticleGraphCompiler().compile(
        ParticleScriptCompiler().parse(source, source_name="MeshOutput.particle.py")
    )
    assert graph_program.emitters[0].render_plan.outputs[0] == output
    assert output.material.guid == "debris-material-guid"
    assert output.sort_mode == "none"
    assert emitter.init.operations[-2].opcode == "attribute.set_orientation"
    assert emitter.init.operations[-1].opcode == "attribute.set_scale"
    assert emitter.update.operations[-1].opcode == "integrate.angular_velocity_3d"
    assert "builtin.orientation" in {
        attribute.stable_id for attribute in emitter.attributes
    }
    assert "builtin.scale" in {
        attribute.stable_id for attribute in emitter.attributes
    }


def test_ribbon_output_has_stable_topology_attributes_and_script_parity():
    source = '''
from Infernux.particle import AssetReference, ParticleScript, ParticleEmitter, EmitterSettings

class TrailGraph(ParticleScript):
    class Trail(ParticleEmitter):
        stable_id = "trail"
        settings = EmitterSettings(capacity=4096)

        def init(self, ctx, particles):
            particles.set_strip_id(7)
            particles.set_ribbon_order(11)
            particles.break_ribbon(False)

        def update(self, ctx, particles):
            particles.set_ribbon_order(12)

        def rendering(self, ctx, particles):
            particles.ribbon(
                material=AssetReference(path_hint="Assets/Materials/Trail.mat"),
                uv_mode="repeat",
                uv_scale=2.5,
            )
'''
    compiler = ParticleScriptCompiler()
    asset = compiler.parse(source, source_name="Trail.particle.py")
    program = compiler.compile(source, source_name="Trail.particle.py")
    graph_program = ParticleGraphCompiler().compile(asset)
    emitter = program.emitters[0]
    output = emitter.render_plan.outputs[0]

    assert program.behavior_hash == graph_program.behavior_hash
    assert output.output_type == "ribbon"
    assert output.ribbon_uv_mode == "repeat"
    assert output.ribbon_uv_scale == pytest.approx(2.5)
    assert output.sort_mode == "none"
    assert [operation.opcode for operation in emitter.init.operations[-3:]] == [
        "attribute.set_strip_id",
        "attribute.set_ribbon_order",
        "attribute.set_ribbon_break",
    ]
    assert {
        "builtin.ribbon_strip_id",
        "builtin.ribbon_order",
        "builtin.ribbon_break",
    }.issubset({attribute.stable_id for attribute in emitter.attributes})


def test_plane_collision_graph_and_script_share_terminal_update_contract():
    source = '''
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class CollisionGraph(ParticleScript):
    class Sparks(ParticleEmitter):
        stable_id = "sparks"
        settings = EmitterSettings(capacity=1024)

        def init(self, ctx, particles):
            particles.set_velocity((1.0, -2.0, 0.0))

        def update(self, ctx, particles):
            particles.acceleration((0.0, -9.81, 0.0))
            particles.collide_plane(
                point=(0.0, 0.0, 0.0),
                normal=(0.0, 1.0, 0.0),
                radius=0.1,
                restitution=0.6,
                friction=0.2,
            )

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    compiler = ParticleScriptCompiler()
    asset = compiler.parse(source, source_name="Collision.particle.py")
    emitter = compiler.compile(source, source_name="Collision.particle.py").emitters[0]

    assert emitter.update.operations[-1].opcode == "collision.plane"
    assert emitter.update.operations[-1].parameter_dict() == {
        "friction": 0.2,
        "normal": [0.0, 1.0, 0.0],
        "point": [0.0, 0.0, 0.0],
        "radius": 0.1,
        "restitution": 0.6,
    }
    assert emitter == ParticleGraphCompiler().compile(asset).emitters[0]


@pytest.mark.parametrize(
    "properties, message",
    [
        ({"normal": [0.0, 0.0, 0.0]}, "normal must be non-zero"),
        ({"radius": -0.1}, "radius must be non-negative"),
        ({"restitution": 1.1}, "restitution must be between 0 and 1"),
        ({"friction": -0.1}, "friction must be between 0 and 1"),
    ],
)
def test_plane_collision_rejects_invalid_static_parameters(properties, message):
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "collision",
                "particle.update.collide_plane",
                properties=properties,
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "collision", "in", PortKind.STREAM
            ),
        ),
    )
    with pytest.raises(ParticleCompileError, match=message):
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
        )


def test_sphere_collision_graph_and_script_share_terminal_update_contract():
    source = '''
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class CollisionGraph(ParticleScript):
    class Sparks(ParticleEmitter):
        stable_id = "sparks"
        settings = EmitterSettings(capacity=1024)

        def init(self, ctx, particles):
            particles.set_velocity((0.0, 0.0, 0.0))

        def update(self, ctx, particles):
            particles.collide_sphere(
                center=(1.0, 2.0, 3.0),
                sphere_radius=2.0,
                particle_radius=0.1,
                restitution=0.7,
                friction=0.25,
            )

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    compiler = ParticleScriptCompiler()
    asset = compiler.parse(source, source_name="SphereCollision.particle.py")
    emitter = compiler.compile(
        source, source_name="SphereCollision.particle.py"
    ).emitters[0]

    assert emitter.update.operations[-1].opcode == "collision.sphere"
    assert emitter.update.operations[-1].parameter_dict() == {
        "center": [1.0, 2.0, 3.0],
        "friction": 0.25,
        "particle_radius": 0.1,
        "restitution": 0.7,
        "sphere_radius": 2.0,
    }
    assert emitter == ParticleGraphCompiler().compile(asset).emitters[0]


def test_sdf_collision_graph_and_script_share_typed_data_interface_contract():
    source = '''
from Infernux.particle import AssetReference, ParticleScript, ParticleEmitter, EmitterSettings, SdfVolume

class CollisionGraph(ParticleScript):
    class Sparks(ParticleEmitter):
        stable_id = "sparks"
        settings = EmitterSettings(capacity=1024)
        data_interfaces = (
            SdfVolume(
                stable_id="collision-field",
                texture=AssetReference(guid="sdf-texture"),
            ),
        )

        def init(self, ctx, particles):
            particles.set_velocity((0.0, 0.0, 0.0))

        def update(self, ctx, particles):
            particles.collide_sdf(
                interface="collision-field",
                particle_radius=0.1,
                restitution=0.7,
                friction=0.25,
                inverted=True,
            )

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    compiler = ParticleScriptCompiler()
    asset = compiler.parse(source, source_name="SdfCollision.particle.py")
    emitter = compiler.compile(
        source, source_name="SdfCollision.particle.py"
    ).emitters[0]

    assert emitter.update.operations[-1].opcode == "collision.sdf"
    assert emitter.update.operations[-1].parameter_dict() == {
        "friction": 0.25,
        "interface": "collision-field",
        "inverted": True,
        "particle_radius": 0.1,
        "restitution": 0.7,
    }
    assert emitter == ParticleGraphCompiler().compile(asset).emitters[0]

    value = asset.to_dict()
    value["emitters"][0]["stages"]["update"]["nodes"][1]["properties"]["interface"] = "missing"
    with pytest.raises(ParticleCompileError, match="unknown SdfVolume"):
        ParticleGraphCompiler().compile(ParticleGraphAsset.from_dict(value))


@pytest.mark.parametrize(
    "properties, message",
    [
        ({"sphere_radius": -0.1}, "sphere_radius must be non-negative"),
        ({"particle_radius": -0.1}, "particle_radius must be non-negative"),
        ({"restitution": 1.1}, "restitution must be between 0 and 1"),
        ({"friction": -0.1}, "friction must be between 0 and 1"),
    ],
)
def test_sphere_collision_rejects_invalid_static_parameters(properties, message):
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "collision",
                "particle.update.collide_sphere",
                properties=properties,
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "collision", "in", PortKind.STREAM
            ),
        ),
    )
    with pytest.raises(ParticleCompileError, match=message):
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
        )


@pytest.mark.parametrize(
    "properties, message",
    [
        ({"sort": "back_to_front"}, "requires sort='none'"),
        ({"uv_mode": "projected"}, "requires uv_mode"),
        ({"uv_scale": 0.0}, "finite positive uv_scale"),
    ],
)
def test_ribbon_output_rejects_ambiguous_topology_and_uv_settings(properties, message):
    rendering = GraphDocument(
        "particle.rendering",
        nodes=(
            GraphNodeRecord("root.rendering", "particle.root.rendering"),
            GraphNodeRecord("ribbon", "particle.output.ribbon", properties=properties),
        ),
        links=(
            GraphLinkRecord(
                "render-stream", "root.rendering", "out", "ribbon", "in", PortKind.STREAM
            ),
        ),
    )
    asset = ParticleGraphAsset(
        emitters=(ParticleEmitterAsset(rendering=rendering),)
    )
    with pytest.raises(ParticleCompileError, match=message):
        ParticleGraphCompiler().compile(asset)


def test_particle_script_vector_field_expression_matches_graph_kernel_contract():
    source = PARTICLE_SCRIPT_SOURCE.replace(
        "particles.acceleration((0.0, -0.2, 0.0))",
        'particles.acceleration(ctx.sample_vector_field("wind-field", particles.position))',
    )
    asset = ParticleScriptCompiler().parse(source, source_name="Wind.particle.py")
    update = asset.emitters[0].update

    assert [node.type_id for node in update.nodes] == [
        "particle.root.update",
        "particle.attribute.read_vec3",
        "particle.vector_field.sample",
        "particle.update.acceleration",
        "particle.update.rotate",
    ]
    assert any(
        link.source_node.endswith("sample_vector_field")
        and link.target_node.endswith("acceleration")
        and link.kind is PortKind.VALUE
        for link in update.links
    )

    hir = ParticleGraphCompiler().compile(asset)
    kernel = ParticleKernelLowerer().lower(hir)
    sample = next(
        instruction
        for instruction in kernel.emitters[0].update.instructions
        if instruction.opcode == "sample_vector_field"
    )
    assert sample.immediate_dict() == {"interface": "wind-field"}


def test_particle_script_curve_and_gradient_compile_to_shared_kernel_operations():
    source = PARTICLE_SCRIPT_SOURCE.replace(
        "from Infernux.particle import AssetReference, ParticleScript, ParticleEmitter, EmitterSettings, VectorField, PointCache",
        "from Infernux.particle import AssetReference, ParticleScript, ParticleEmitter, EmitterSettings, VectorField, PointCache, Curve, CurveKey, Gradient, GradientKey",
    ).replace(
        "particles.acceleration((0.0, -0.2, 0.0))",
        """particles.set_size(ctx.sample_curve(
                Curve(keys=(CurveKey(0.0, 0.0, 1.0, 1.0), CurveKey(1.0, 1.0, 1.0, 1.0))),
                particles.age / particles.lifetime,
            ))
            particles.set_color(ctx.sample_gradient(
                Gradient(keys=(GradientKey(0.0, (1.0, 0.0, 0.0, 1.0)), GradientKey(1.0, (0.0, 0.0, 1.0, 0.0)))),
                particles.age / particles.lifetime,
            ))""",
    )
    asset = ParticleScriptCompiler().parse(source, source_name="Ramp.particle.py")
    kernel = ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))
    opcodes = [instruction.opcode for instruction in kernel.emitters[0].update.instructions]

    assert "sample_curve" in opcodes
    assert "sample_gradient" in opcodes
    assert opcodes.count("divide") == 2
    assert opcodes.count("store_attribute") >= 4


def test_particle_script_comparisons_and_kill_if_lower_to_composable_lifecycle_ops():
    source = PARTICLE_SCRIPT_SOURCE.replace(
        "particles.acceleration((0.0, -0.2, 0.0))",
        "particles.kill_if(particles.age >= 0.5)",
    )

    asset = ParticleScriptCompiler().parse(source, source_name="Kill.particle.py")
    update = asset.emitters[0].update
    kernel = ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))
    opcodes = [instruction.opcode for instruction in kernel.emitters[0].update.instructions]

    assert any(node.type_id == "common.compare.greater_equal" for node in update.nodes)
    assert any(node.type_id == "particle.update.kill_if" for node in update.nodes)
    assert "greater_equal" in opcodes
    assert opcodes.count("kill_if") == 2


def test_particle_script_noise_compiles_to_the_shared_portable_kernel_ops():
    source = PARTICLE_SCRIPT_SOURCE.replace(
        "particles.acceleration((0.0, -0.2, 0.0))",
        "particles.acceleration(ctx.vector_noise_3d(particles.position, frequency=2.5, seed=17))",
    ).replace(
        "particles.rotate(180.0)",
        "particles.set_size(ctx.value_noise_3d(particles.position, 4.0, 23))",
    )

    asset = ParticleScriptCompiler().parse(source, source_name="Noise.particle.py")
    kernel = ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))
    opcodes = [instruction.opcode for instruction in kernel.emitters[0].update.instructions]

    assert "vector_noise_3d" in opcodes
    assert "value_noise_3d" in opcodes
    vector_noise = next(
        node
        for node in asset.emitters[0].update.nodes
        if node.type_id == "common.noise.vector3d"
    )
    assert vector_noise.properties == {"frequency": 2.5, "seed": 17}


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        (
            'ctx.sample_vector_field("missing", particles.position)',
            "unknown data interface",
        ),
        (
            'ctx.read_internal_wheel(particles.position)',
            "unsupported particle context expression",
        ),
        (
            'ctx.sample_vector_field("wind-field", particles.private_state)',
            "unsupported particle attribute",
        ),
    ],
)
def test_particle_script_vector_field_expression_rejects_unknown_or_private_access(replacement, message):
    source = PARTICLE_SCRIPT_SOURCE.replace(
        "particles.acceleration((0.0, -0.2, 0.0))",
        f"particles.acceleration({replacement})",
    )

    if message == "unknown data interface":
        asset = ParticleScriptCompiler().parse(source, source_name="InvalidWind.particle.py")
        with pytest.raises(KernelCompileError, match=message):
            ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))
    else:
        with pytest.raises(ParticleScriptError, match=message):
            ParticleScriptCompiler().parse(source, source_name="InvalidWind.particle.py")


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ('sort="distance"', "unsupported sort mode"),
        (
            "receive_scene_lighting=False,\n                receive_shadows=True,\n                sort=\"back_to_front\"",
            "cannot receive shadows",
        ),
    ],
)
def test_particle_output_rejects_invalid_render_semantics(replacement, message):
    source = PARTICLE_SCRIPT_SOURCE.replace(
        'receive_scene_lighting=True,\n                receive_shadows=True,\n                sort="back_to_front"',
        replacement,
    )

    with pytest.raises(ParticleCompileError, match=message):
        ParticleScriptCompiler().compile(source, source_name="InvalidOutput.particle.py")


@pytest.mark.parametrize(
    ("source", "message"),
    [
        ("open('side-effect.txt', 'w')\n" + PARTICLE_SCRIPT_SOURCE, "unsupported top-level"),
        (
            PARTICLE_SCRIPT_SOURCE.replace("particles.set_lifetime(6.0)", "particles.set_lifetime(get_lifetime())"),
            "literal data",
        ),
        (
            PARTICLE_SCRIPT_SOURCE.replace(
                "        def update(self, ctx, particles):\n            particles.acceleration((0.0, -0.2, 0.0))\n            particles.rotate(180.0)\n\n",
                "",
            ),
            "missing=['update']",
        ),
    ],
)
def test_particle_script_rejects_executable_or_incomplete_python(source, message):
    with pytest.raises(ParticleScriptError, match=message.replace("[", r"\[").replace("]", r"\]")):
        ParticleScriptCompiler().parse(source, source_name="Invalid.particle.py")


def test_particle_graph_and_script_save_to_equivalent_aot_artifacts(tmp_path, monkeypatch):
    from Infernux.engine import project_context

    ParticleArtifactRegistry.clear()
    monkeypatch.setattr(project_context, "get_project_root", lambda: str(tmp_path))
    graph_path = tmp_path / "Assets" / "Smoke.particlegraph"
    script_path = tmp_path / "Assets" / "Smoke.particle.py"
    graph_path.parent.mkdir()
    script_path.write_text(PARTICLE_SCRIPT_SOURCE, encoding="utf-8")
    graph_asset = ParticleScriptCompiler().parse(
        PARTICLE_SCRIPT_SOURCE,
        source_name=str(script_path),
    )
    graph_asset.save(str(graph_path))

    graph_artifact = ParticleArtifactRegistry.get(str(graph_path))
    script_artifact = ParticleArtifactRegistry.compile_path(str(script_path))

    assert graph_artifact is not None
    assert graph_artifact.behavior_hash == script_artifact.behavior_hash
    assert graph_artifact.hir["schedule"] == ["smoke"]
    assert graph_artifact.kernel_ir["$schema"] == "infernux.particle_kernel_ir"
    assert graph_artifact.kernel_ir["source_behavior_hash"] == graph_artifact.behavior_hash
    assert graph_artifact.kernel_ir["kernel_hash"] == script_artifact.kernel_ir["kernel_hash"]
    assert graph_artifact.gpu_glsl["$schema"] == "infernux.particle_gpu_glsl"
    assert graph_artifact.gpu_glsl["kernel_hash"] == graph_artifact.kernel_ir["kernel_hash"]
    assert [
        value["stable_id"]
        for value in graph_artifact.gpu_glsl["emitters"][0]["data_interfaces"]
    ] == ["morph-points", "wind-field"]
    assert set(graph_artifact.gpu_glsl["emitters"][0]["stages"]) == {
        "bootstrap",
        "init",
        "event_init",
        "update",
        "render_reset",
        "rendering",
    }
    assert graph_artifact.gpu_spirv["target"] == "vulkan1.2-spirv1.5"
    assert graph_artifact.gpu_spirv["kernel_hash"] == graph_artifact.kernel_ir["kernel_hash"]
    assert set(graph_artifact.gpu_spirv["emitters"][0]["stages"]) == set(
        graph_artifact.gpu_glsl["emitters"][0]["stages"]
    )
    assert set(graph_artifact.gpu_spirv["billboard"]) == {
        "vertex", "fragment", "forward_plus_fragment", "picking_fragment"
    }
    assert set(graph_artifact.gpu_spirv["mesh"]) == {
        "vertex", "fragment", "forward_plus_fragment", "picking_fragment"
    }
    from Infernux.particle import decode_gpu_particle_spirv

    decoded = decode_gpu_particle_spirv(graph_artifact.gpu_spirv, 0)
    assert decoded["stable_id"] == "smoke"
    assert set(decoded["stages"]) == set(graph_artifact.gpu_glsl["emitters"][0]["stages"])
    assert set(decoded["billboard"]) == {
        "vertex", "fragment", "forward_plus_fragment", "picking_fragment"
    }
    assert set(decoded["mesh"]) == {
        "vertex", "fragment", "forward_plus_fragment", "picking_fragment"
    }
    assert all(binary[:4] == b"\x03\x02#\x07" for binary in decoded["stages"].values())
    assert all(binary[:4] == b"\x03\x02#\x07" for binary in decoded["billboard"].values())
    assert all(binary[:4] == b"\x03\x02#\x07" for binary in decoded["mesh"].values())
    assert script_artifact.hir["emitters"][0]["render_plan"][0][
        "receive_scene_lighting"
    ] is True
    assert script_artifact.hir["emitters"][0]["render_plan"][0]["cast_shadows"] is False
    runtime_metadata = decode_particle_runtime_metadata(script_artifact.hir)
    assert runtime_metadata.emitters[0].outputs[0].cast_shadows is False
    stale_hir = copy.deepcopy(script_artifact.hir)
    stale_hir["emitters"][0]["render_plan"][0].pop("cast_shadows")
    with pytest.raises(ParticleRuntimeMetadataError, match="is invalid"):
        decode_particle_runtime_metadata(stale_hir)
    assert graph_artifact.artifact_path.endswith("smoke-graph.inxparticle")


def test_particle_aot_failure_preserves_last_known_good_and_cache_hit(tmp_path, monkeypatch):
    from Infernux.engine import project_context
    from Infernux.particle import artifact as artifact_module

    ParticleArtifactRegistry.clear()
    monkeypatch.setattr(project_context, "get_project_root", lambda: str(tmp_path))
    path = tmp_path / "Assets" / "Smoke.particlegraph"
    path.parent.mkdir()
    asset = ParticleGraphAsset(stable_id="smoke-cache")
    asset.save(str(path))
    published = ParticleArtifactRegistry.get(str(path))
    assert published is not None

    path.write_text('{"broken": true}', encoding="utf-8")
    with pytest.raises(ParticleArtifactError, match="AOT compile failed"):
        ParticleArtifactRegistry.compile_path(str(path))
    assert ParticleArtifactRegistry.get(str(path)) == published

    asset.save(str(path))
    current = ParticleArtifactRegistry.get(str(path))
    ParticleArtifactRegistry.clear()

    def fail_compile(_self, _asset):
        raise AssertionError("matching particle artifact should bypass HIR compilation")

    monkeypatch.setattr(artifact_module.ParticleGraphCompiler, "compile", fail_compile)
    restored = ParticleArtifactRegistry.compile_path(str(path))

    assert restored.source_hash == current.source_hash
    assert restored.behavior_hash == current.behavior_hash


def test_particle_graph_save_does_not_replace_valid_source_with_invalid_draft(tmp_path):
    path = tmp_path / "Smoke.particlegraph"
    valid = ParticleGraphAsset(stable_id="atomic-particle-save")
    valid.save(str(path))
    source_before = path.read_bytes()

    emitter = valid.emitters[0]
    update = emitter.update
    invalid_update = GraphDocument(
        update.domain,
        update.nodes
        + (
            GraphNodeRecord("noise", "common.noise.vector3d", properties={}),
                GraphNodeRecord("invalid.acceleration", "particle.update.acceleration"),
        ),
        update.links
        + (
            GraphLinkRecord(
                    "root-to-invalid-acceleration",
                    "root.update",
                    "out",
                    "invalid.acceleration",
                "in",
                PortKind.STREAM,
            ),
            GraphLinkRecord(
                "noise-to-acceleration",
                "noise",
                "value",
                    "invalid.acceleration",
                "value",
                PortKind.VALUE,
            ),
        ),
        update.metadata,
    )
    invalid = replace(valid, emitters=(replace(emitter, update=invalid_update),))

    with pytest.raises(ParticleArtifactError, match="required input noise.position"):
        invalid.save(str(path))

    assert path.read_bytes() == source_before


def test_particle_aot_rebuilds_persisted_artifact_with_stale_hir_contract(
    tmp_path, monkeypatch
):
    project = tmp_path / "Project"
    source = project / "Assets" / "Smoke.particlegraph"
    source.parent.mkdir(parents=True)
    graph = ParticleGraphAsset(stable_id="stale-hir-smoke")
    source.write_text(
        json.dumps(graph.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "Infernux.engine.project_context.get_project_root", lambda: str(project)
    )

    ParticleArtifactRegistry.clear()
    current = ParticleArtifactRegistry.compile_path(str(source))
    artifact_path = Path(current.artifact_path)
    payload = json.loads(artifact_path.read_text(encoding="utf-8"))
    output = payload["hir"]["emitters"][0]["render_plan"][0]
    output.pop("soft_particles")
    output.pop("soft_distance")
    artifact_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    ParticleArtifactRegistry.clear()
    rebuilt = ParticleArtifactRegistry.compile_path(str(source))

    rebuilt_output = rebuilt.hir["emitters"][0]["render_plan"][0]
    assert rebuilt_output["soft_particles"] is False
    assert rebuilt_output["soft_distance"] == 1.0
    persisted_output = json.loads(artifact_path.read_text(encoding="utf-8"))["hir"][
        "emitters"
    ][0]["render_plan"][0]
    assert persisted_output["soft_particles"] is False
    assert persisted_output["soft_distance"] == 1.0
