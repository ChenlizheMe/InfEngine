from __future__ import annotations

import copy

import pytest

from Infernux.graph import GraphDocument, GraphLinkRecord, GraphNodeRecord, PortKind
from Infernux.particle import (
    EmitterSettings,
    ExecutionTarget,
    KernelCompileError,
    ParticleCompileError,
    ParticleEmitterAsset,
    ParticleAttribute,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleGraphSchemaError,
    ParticleKernelLowerer,
    ParticleStage,
    ParticleScriptCompiler,
    ParticleScriptError,
    ParticleArtifactError,
    ParticleArtifactRegistry,
    PointCache,
    ScalarRange,
    SimulationSpace,
    VectorField,
)
from Infernux.graph import AssetReference, TypeRef, ValueType


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
        ),
    )
    asset = ParticleGraphAsset(stable_id="data-graph", emitters=(emitter,))

    restored = ParticleGraphAsset.from_json(asset.canonical_json())
    hir = ParticleGraphCompiler().compile(restored)

    assert restored == asset
    assert [interface.stable_id for interface in hir.emitters[0].data_interfaces] == [
        "wind-field",
        "morph-points",
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
            lifetime=ScalarRange(4.0, 8.0),
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
    assert smoke.init.operations[0].opcode == "settings.initialize"
    assert smoke.update.operations[0].opcode == "settings.gravity"
    assert smoke.render_plan.outputs[0].material == AssetReference(guid="six-way-smoke-guid")
    assert smoke.render_plan.outputs[0].receive_scene_lighting is True
    assert smoke.render_plan.outputs[0].receive_shadows is True


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


def test_particle_graph_stream_order_lowers_to_stage_operations():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord(
                "velocity",
                "particle.init.set_velocity",
                properties={"value": [0.0, 2.0, 0.0]},
            ),
            GraphNodeRecord(
                "lifetime",
                "particle.init.set_lifetime",
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
        "settings.initialize",
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
from Infernux.particle import AssetReference, ParticleScript, ParticleEmitter, EmitterSettings, ScalarRange, VectorField, PointCache

class SmokeGraph(ParticleScript):
    stable_id = "smoke-graph"

    class Smoke(ParticleEmitter):
        stable_id = "smoke"
        settings = EmitterSettings(
            capacity=100000,
            target="gpu",
            simulation_space="world",
            spawn_rate=20000.0,
            lifetime=ScalarRange(4.0, 8.0),
            initial_speed=ScalarRange(0.4, 1.2),
            gravity=(0.0, -0.2, 0.0),
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

        def update(self, ctx, particles):
            particles.acceleration((0.0, -0.2, 0.0))

        def rendering(self, ctx, particles):
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
        "settings.initialize",
        "attribute.set_velocity",
        "attribute.set_lifetime",
    ]
    assert emitter.update.operations[-1].opcode == "integrate.acceleration"
    assert emitter.render_plan.outputs[0].receive_scene_lighting is True
    assert emitter.render_plan.outputs[0].receive_shadows is True
    assert [interface.stable_id for interface in emitter.data_interfaces] == [
        "wind-field",
        "morph-points",
    ]
    assert program.behavior_hash == ParticleGraphCompiler().compile(asset).behavior_hash


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
                "        def update(self, ctx, particles):\n            particles.acceleration((0.0, -0.2, 0.0))\n\n",
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
        "vertex", "fragment", "picking_fragment"
    }
    from Infernux.particle import decode_gpu_particle_spirv

    decoded = decode_gpu_particle_spirv(graph_artifact.gpu_spirv, 0)
    assert decoded["stable_id"] == "smoke"
    assert set(decoded["stages"]) == set(graph_artifact.gpu_glsl["emitters"][0]["stages"])
    assert set(decoded["billboard"]) == {"vertex", "fragment", "picking_fragment"}
    assert all(binary[:4] == b"\x03\x02#\x07" for binary in decoded["stages"].values())
    assert all(binary[:4] == b"\x03\x02#\x07" for binary in decoded["billboard"].values())
    assert script_artifact.hir["emitters"][0]["render_plan"][0][
        "receive_scene_lighting"
    ] is True
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
