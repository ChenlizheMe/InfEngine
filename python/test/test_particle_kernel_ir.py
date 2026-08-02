from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from Infernux.graph import GraphDocument, GraphLinkRecord, GraphNodeRecord, PortKind
from Infernux.graph.types import AssetReference, CoordinateSpace, TypeRef, ValueType
from Infernux.particle import (
    EmitterSettings,
    EmitterShape,
    KernelCompileError,
    KernelInstruction,
    KernelOperand,
    KernelStage,
    ParticleEmitterAsset,
    ParticleEventFlow,
    ParticleEventType,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleKernelFunction,
    ParticleKernelLowerer,
    ParticleKernelProgram,
    ParticleRuntimeCompatibility,
    SdfVolume,
    VectorField,
    classify_emitter_update,
    default_event_graph,
    particle_random_f32,
    particle_random_u32,
)


def _lower(asset: ParticleGraphAsset):
    return ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))


def test_default_particle_program_lowers_to_explicit_three_stage_kernel_ir():
    emitter = _lower(ParticleGraphAsset(stable_id="default-kernel")).emitters[0]

    assert emitter.init.stage is KernelStage.INIT
    assert emitter.update.stage is KernelStage.UPDATE
    assert emitter.rendering.stage is KernelStage.RENDERING
    assert {
        "builtin.position",
        "builtin.velocity",
        "builtin.age",
        "builtin.lifetime",
    }.issubset(emitter.init.written_attributes)
    update_opcodes = [instruction.opcode for instruction in emitter.update.instructions]
    assert update_opcodes.count("load_uniform") == 1
    assert "store_attribute" in update_opcodes
    assert "kill_if" in update_opcodes
    assert update_opcodes[-1] == "kill_if"
    assert update_opcodes[-2] == "logical_not"
    render_exports = [
        instruction.immediate_dict()["attribute"]
        for instruction in emitter.rendering.instructions
        if instruction.opcode == "export_attribute"
    ]
    assert render_exports == [
        "builtin.position",
        "builtin.velocity",
        "builtin.size",
        "builtin.color",
        "builtin.rotation",
        "builtin.age",
        "builtin.lifetime",
        "builtin.id",
    ]


def test_update_stage_can_rewrite_lifetime_velocity_and_flipbook_frame():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "velocity",
                "particle.attribute.velocity",
                properties={"value": [1.0, 2.0, 3.0]},
            ),
            GraphNodeRecord(
                "lifetime",
                "particle.attribute.lifetime",
                properties={"value": 8.0},
            ),
            GraphNodeRecord(
                "flipbook",
                "particle.attribute.flipbook_frame",
                properties={"value": 4.0},
            ),
        ),
        links=(
            GraphLinkRecord("velocity-stream", "root.update", "out", "velocity", "in", PortKind.EXEC),
            GraphLinkRecord("lifetime-stream", "velocity", "out", "lifetime", "in", PortKind.EXEC),
            GraphLinkRecord("flipbook-stream", "lifetime", "out", "flipbook", "in", PortKind.EXEC),
        ),
    )
    emitter = _lower(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    ).emitters[0]
    update_writes = {
        instruction.immediate_dict()["attribute"]
        for instruction in emitter.update.instructions
        if instruction.opcode == "store_attribute"
    }
    render_exports = {
        instruction.immediate_dict()["attribute"]
        for instruction in emitter.rendering.instructions
        if instruction.opcode == "export_attribute"
    }

    assert {
        "builtin.velocity",
        "builtin.lifetime",
        "builtin.flipbook_frame",
    }.issubset(update_writes)
    assert "builtin.flipbook_frame" in render_exports


def test_rendering_stage_can_rewrite_particle_attributes_before_export():
    rendering = GraphDocument(
        "particle.rendering",
        nodes=(
            GraphNodeRecord("root.rendering", "particle.root.rendering"),
            GraphNodeRecord(
                "lifetime",
                "particle.attribute.lifetime",
                properties={"value": 12.0},
            ),
            GraphNodeRecord(
                "flipbook",
                "particle.attribute.flipbook_frame",
                properties={"value": 7.5},
            ),
            GraphNodeRecord("output", "particle.output.sprite"),
        ),
        links=(
            GraphLinkRecord(
                "lifetime-stream", "root.rendering", "out", "lifetime", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "flipbook-stream", "lifetime", "out", "flipbook", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "output-stream", "flipbook", "out", "output", "in", PortKind.EXEC
            ),
        ),
    )
    emitter = _lower(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(rendering=rendering),))
    ).emitters[0]
    instructions = emitter.rendering.instructions
    stores = [
        instruction.immediate_dict()["attribute"]
        for instruction in instructions
        if instruction.opcode == "store_attribute"
    ]
    exports = [
        instruction.immediate_dict()["attribute"]
        for instruction in instructions
        if instruction.opcode == "export_attribute"
    ]

    assert stores == ["builtin.lifetime", "builtin.flipbook_frame"]
    assert "builtin.lifetime" in exports
    assert "builtin.flipbook_frame" in exports
    assert max(
        index for index, instruction in enumerate(instructions) if instruction.opcode == "store_attribute"
    ) < min(
        index for index, instruction in enumerate(instructions) if instruction.opcode == "export_attribute"
    )


def test_mesh_orientation_lowers_degrees_to_radians_and_exports_vec3_state():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord(
                "orientation",
                "particle.attribute.orientation",
                properties={"degrees": [10.0, 20.0, 30.0]},
            ),
        ),
        links=(
            GraphLinkRecord(
                "init-stream", "root.init", "out", "orientation", "in", PortKind.EXEC
            ),
        ),
    )
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
                GraphNodeRecord(
                    "angular-velocity",
                    "particle.attribute.orientation",
                    properties={
                        "composition": "add",
                        "degrees": [90.0, 180.0, 270.0],
                    },
            ),
        ),
        links=(
            GraphLinkRecord(
                "update-stream", "root.update", "out", "angular-velocity", "in", PortKind.EXEC
            ),
        ),
    )
    rendering = GraphDocument(
        "particle.rendering",
        nodes=(
            GraphNodeRecord("root.rendering", "particle.root.rendering"),
            GraphNodeRecord(
                "mesh",
                "particle.output.mesh",
                properties={"mesh": AssetReference(guid="mesh-guid").to_dict()},
            ),
        ),
        links=(
            GraphLinkRecord(
                "render-stream", "root.rendering", "out", "mesh", "in", PortKind.EXEC
            ),
        ),
    )

    emitter = _lower(
        ParticleGraphAsset(
            emitters=(ParticleEmitterAsset(init=init, update=update, rendering=rendering),)
        )
    ).emitters[0]

    assert emitter.attributes[-2] == (
        "builtin.orientation",
        TypeRef(ValueType.VEC3),
        [0.0, 0.0, 0.0],
    )
    assert emitter.attributes[-1] == (
        "builtin.scale",
        TypeRef(ValueType.VEC3),
        [1.0, 1.0, 1.0],
    )
    assert "builtin.orientation" in emitter.init.written_attributes
    assert "builtin.orientation" in emitter.update.written_attributes
    exports = [
        instruction.immediate_dict()["attribute"]
        for instruction in emitter.rendering.instructions
        if instruction.opcode == "export_attribute"
    ]
    assert "builtin.orientation" in exports
    assert sum(instruction.opcode == "multiply" for instruction in emitter.update.instructions) >= 2
    assert any(instruction.opcode == "add" for instruction in emitter.update.instructions)


def test_attribute_composition_is_explicit_and_never_implicitly_uses_delta_time():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "set-velocity",
                "particle.attribute.velocity",
                properties={"composition": "set", "value": [0.0, 1.0, 0.0]},
            ),
            GraphNodeRecord(
                "add-velocity",
                "particle.attribute.velocity",
                properties={"composition": "add", "value": [1.0, 0.0, 0.0]},
            ),
            GraphNodeRecord(
                "multiply-velocity",
                "particle.attribute.velocity",
                properties={"composition": "multiply", "value": [0.5, 1.0, 1.0]},
            ),
        ),
        links=(
            GraphLinkRecord(
                "root-set", "root.update", "out", "set-velocity", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "set-add", "set-velocity", "out", "add-velocity", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "add-multiply",
                "add-velocity",
                "out",
                "multiply-velocity",
                "in",
                PortKind.EXEC,
            ),
        ),
    )

    program = ParticleGraphCompiler().compile(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    )
    operations = tuple(program.emitters[0].update.flow.iter_operations())
    assert [operation.parameter_dict()["composition"] for operation in operations] == [
        "set",
        "add",
        "multiply",
    ]
    kernel = ParticleKernelLowerer().lower(program).emitters[0].update
    by_node = {
        node_uid: [
            instruction.opcode
            for instruction in kernel.instructions
            if instruction.source.node_uid == node_uid
        ]
        for node_uid in ("set-velocity", "add-velocity", "multiply-velocity")
    }
    assert "load_attribute" not in by_node["set-velocity"]
    assert "add" in by_node["add-velocity"]
    assert "multiply" in by_node["multiply-velocity"]
    assert all("load_uniform" not in opcodes for opcodes in by_node.values())


def test_ribbon_topology_attributes_lower_and_export_without_cpu_readback_contract():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord("strip", "particle.attribute.strip_id", properties={"value": 3}),
            GraphNodeRecord("order", "particle.attribute.ribbon_order", properties={"value": 9}),
            GraphNodeRecord("break", "particle.attribute.ribbon_break", properties={"value": True}),
        ),
        links=(
            GraphLinkRecord("a", "root.init", "out", "strip", "in", PortKind.EXEC),
            GraphLinkRecord("b", "strip", "out", "order", "in", PortKind.EXEC),
            GraphLinkRecord("c", "order", "out", "break", "in", PortKind.EXEC),
        ),
    )
    rendering = GraphDocument(
        "particle.rendering",
        nodes=(
            GraphNodeRecord("root.rendering", "particle.root.rendering"),
            GraphNodeRecord("ribbon", "particle.output.ribbon"),
        ),
        links=(
            GraphLinkRecord("render", "root.rendering", "out", "ribbon", "in", PortKind.EXEC),
        ),
    )
    emitter = _lower(
        ParticleGraphAsset(
            emitters=(ParticleEmitterAsset(init=init, rendering=rendering),)
        )
    ).emitters[0]

    topology = {
        "builtin.ribbon_strip_id": TypeRef(ValueType.U32),
        "builtin.ribbon_order": TypeRef(ValueType.U32),
        "builtin.ribbon_break": TypeRef(ValueType.BOOL),
    }
    attribute_types = {
        stable_id: value_type for stable_id, value_type, _ in emitter.attributes
    }
    assert {stable_id: attribute_types[stable_id] for stable_id in topology} == topology
    assert set(topology).issubset(emitter.init.written_attributes)
    exports = {
        instruction.immediate_dict()["attribute"]
        for instruction in emitter.rendering.instructions
        if instruction.opcode == "export_attribute"
    }
    assert set(topology).issubset(exports)


def test_stage_expressions_read_state_after_prior_exec_writes():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord(
                "set-size",
                "particle.attribute.size",
                properties={"value": 2.0},
            ),
            GraphNodeRecord(
                "read-size",
                "particle.attribute.get",
                properties={"attribute": "builtin.size"},
            ),
            GraphNodeRecord("set-rotation", "particle.attribute.rotation"),
        ),
        links=(
            GraphLinkRecord(
                "exec-size", "root.init", "out", "set-size", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "exec-rotation",
                "set-size",
                "out",
                "set-rotation",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "size-value",
                "read-size",
                "value",
                "set-rotation",
                "value",
                PortKind.VALUE,
            ),
        ),
    )

    instructions = _lower(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(init=init),))
    ).emitters[0].init.instructions
    size_store = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opcode == "store_attribute"
        and instruction.immediate_dict()["attribute"] == "builtin.size"
        and instruction.source.node_uid == "set-size"
    )
    size_read = next(
        index
        for index, instruction in enumerate(instructions)
        if instruction.opcode == "load_attribute"
        and instruction.immediate_dict()["attribute"] == "builtin.size"
        and instruction.source.node_uid == "read-size"
    )

    assert size_store < size_read


def test_kernel_math_promotes_unspaced_constants_into_simulation_space():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "position",
                "particle.attribute.get",
                properties={"attribute": "builtin.position"},
            ),
            GraphNodeRecord("noise", "common.noise.vector3d"),
            GraphNodeRecord(
                "scale",
                "common.constant.vec3",
                properties={"value": [0.2, 0.06, 0.2]},
            ),
            GraphNodeRecord("multiply", "common.math.multiply"),
            GraphNodeRecord(
                "buoyancy",
                "common.constant.vec3",
                properties={"value": [0.0, 0.12, 0.0]},
            ),
            GraphNodeRecord("add", "common.math.add"),
            GraphNodeRecord("acceleration", "particle.attribute.velocity"),
        ),
        links=(
            GraphLinkRecord("stream", "root.update", "out", "acceleration", "in", PortKind.EXEC),
            GraphLinkRecord("position", "position", "value", "noise", "position"),
            GraphLinkRecord("noise", "noise", "value", "multiply", "a"),
            GraphLinkRecord("scale", "scale", "value", "multiply", "b"),
            GraphLinkRecord("scaled", "multiply", "result", "add", "a"),
            GraphLinkRecord("buoyancy", "buoyancy", "value", "add", "b"),
            GraphLinkRecord("result", "add", "result", "acceleration", "value"),
        ),
    )

    kernel = _lower(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    ).emitters[0].update
    simulation = TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION)
    math = [
        instruction
        for instruction in kernel.instructions
        if instruction.opcode in {"multiply", "add"}
        and instruction.source.node_uid in {"multiply", "add"}
    ]

    assert [instruction.result_type for instruction in math] == [simulation, simulation]


def test_plane_collision_lowers_after_position_integration_with_portable_state_writes():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "collision",
                "particle.collision.plane",
                properties={"radius": 0.25, "restitution": 0.5, "friction": 0.25},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "collision", "in", PortKind.EXEC
            ),
        ),
    )
    emitter = _lower(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    ).emitters[0]
    instructions = emitter.update.instructions
    opcodes = [instruction.opcode for instruction in instructions]

    position_collision = opcodes.index("collide_plane_position")
    velocity_collision = opcodes.index("collide_plane_velocity")
    integrate_store = max(
        index
        for index, instruction in enumerate(instructions[:position_collision])
        if instruction.opcode == "store_attribute"
        and instruction.immediate_dict()["attribute"] == "builtin.position"
    )
    assert integrate_store < position_collision < velocity_collision
    assert {
        instruction.immediate_dict()["attribute"]
        for instruction in instructions[velocity_collision + 1 :]
        if instruction.opcode == "store_attribute"
    }.issuperset({"builtin.position", "builtin.velocity"})


def test_sphere_collision_lowers_after_position_integration_with_typed_operands():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "collision",
                "particle.collision.sphere",
                properties={
                    "center": [0.0, 1.0, 0.0],
                    "sphere_radius": 2.0,
                    "particle_radius": 0.25,
                },
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "collision", "in", PortKind.EXEC
            ),
        ),
    )
    emitter = _lower(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    ).emitters[0]
    instructions = emitter.update.instructions
    opcodes = [instruction.opcode for instruction in instructions]
    position_collision = opcodes.index("collide_sphere_position")
    velocity_collision = opcodes.index("collide_sphere_velocity")

    assert position_collision < velocity_collision
    assert len(instructions[position_collision].operands) == 7
    assert [operand.value_type.value_type for operand in instructions[position_collision].operands] == [
        ValueType.VEC3,
        ValueType.VEC3,
        ValueType.VEC3,
        ValueType.F32,
        ValueType.F32,
        ValueType.F32,
        ValueType.F32,
    ]
    assert any(
        instruction.opcode == "store_attribute"
        and instruction.immediate_dict()["attribute"] == "builtin.position"
        for instruction in instructions[:position_collision]
    )


def test_data_interface_abi_round_trips_and_resource_rebind_preserves_state():
    first_emitter = ParticleEmitterAsset(
        stable_id="data-emitter",
        data_interfaces=(
            VectorField(
                stable_id="wind",
                texture=AssetReference(path_hint="Assets/WindA.vectorfield"),
            ),
            SdfVolume(
                stable_id="collision",
                texture=AssetReference(path_hint="Assets/Collision.inxsdf"),
            ),
        ),
    )
    first = _lower(ParticleGraphAsset(stable_id="data-graph", emitters=(first_emitter,)))
    restored = ParticleKernelProgram.from_dict(first.to_dict())

    assert restored == first
    assert [value.stable_id for value in restored.emitters[0].data_interfaces] == [
        "collision",
        "wind",
    ]

    rebound_emitter = replace(
        first_emitter,
        data_interfaces=(
            replace(
                first_emitter.data_interfaces[0],
                texture=AssetReference(path_hint="Assets/WindB.vectorfield"),
            ),
            first_emitter.data_interfaces[1],
        ),
    )
    rebound = _lower(
        ParticleGraphAsset(stable_id="data-graph", emitters=(rebound_emitter,))
    )

    assert rebound.kernel_hash != first.kernel_hash
    assert (
        classify_emitter_update(
            first.emitters[0],
            rebound.emitters[0],
            first_emitter.settings,
            rebound_emitter.settings,
        )
        is ParticleRuntimeCompatibility.PARAMETER_ONLY
    )

    reordered = _lower(
        ParticleGraphAsset(
            stable_id="data-graph",
            emitters=(
                replace(
                    first_emitter,
                    data_interfaces=tuple(reversed(first_emitter.data_interfaces)),
                ),
            ),
        )
    )
    assert reordered.kernel_hash == first.kernel_hash
    assert [
        interface.stable_id for interface in reordered.emitters[0].data_interfaces
    ] == ["collision", "wind"]

    extended_emitter = replace(
        first_emitter,
        data_interfaces=first_emitter.data_interfaces
        + (VectorField(stable_id="turbulence"),),
    )
    extended = _lower(
        ParticleGraphAsset(stable_id="data-graph", emitters=(extended_emitter,))
    )
    assert (
        classify_emitter_update(
            first.emitters[0],
            extended.emitters[0],
            first_emitter.settings,
            extended_emitter.settings,
        )
        is ParticleRuntimeCompatibility.KERNEL_COMPATIBLE
    )


def test_enabling_collision_is_a_layout_migratable_kernel_change():
    previous_emitter = ParticleEmitterAsset(
        stable_id="collision-toggle",
        settings=EmitterSettings(collision_enabled=False),
    )
    next_emitter = replace(
        previous_emitter,
        settings=replace(previous_emitter.settings, collision_enabled=True),
    )
    previous = _lower(ParticleGraphAsset(emitters=(previous_emitter,))).emitters[0]
    current = _lower(ParticleGraphAsset(emitters=(next_emitter,))).emitters[0]

    assert (
        classify_emitter_update(
            previous,
            current,
            previous_emitter.settings,
            next_emitter.settings,
        )
        is ParticleRuntimeCompatibility.LAYOUT_MIGRATABLE
    )


def _attribute_cache_emitter(*fields):
    nodes = [GraphNodeRecord("root.init", "particle.root.init")]
    links = []
    for stable_id, name, value_type, value in fields:
        nodes.append(
            GraphNodeRecord(
                stable_id,
                "particle.attribute.cache",
                properties={
                    "name": name,
                    "value_type": value_type,
                    "value_space": "none",
                    "composition": "set",
                    "value": value,
                },
            )
        )
        links.append(
            GraphLinkRecord(
                f"root-{stable_id}",
                "root.init",
                "out",
                stable_id,
                "in",
                PortKind.EXEC,
            )
        )
    return ParticleEmitterAsset(
        stable_id="cache-hot-reload",
        init=GraphDocument("particle.init", nodes=nodes, links=links),
    )


def test_attribute_cache_addition_migrates_but_storage_type_change_restarts():
    previous_emitter = _attribute_cache_emitter(
        ("phase", "Phase", "f32", 0.5),
    )
    extended_emitter = _attribute_cache_emitter(
        ("phase", "Phase", "f32", 0.5),
        ("heat", "Heat", "f32", 0.25),
    )
    changed_type_emitter = _attribute_cache_emitter(
        ("phase", "Phase", "f32", 0.5),
        ("heat", "Heat", "vec3", [0.25, 0.5, 0.75]),
    )
    previous = _lower(ParticleGraphAsset(emitters=(previous_emitter,))).emitters[0]
    extended = _lower(ParticleGraphAsset(emitters=(extended_emitter,))).emitters[0]
    changed_type = _lower(
        ParticleGraphAsset(emitters=(changed_type_emitter,))
    ).emitters[0]

    assert (
        classify_emitter_update(
            previous,
            extended,
            previous_emitter.settings,
            extended_emitter.settings,
        )
        is ParticleRuntimeCompatibility.LAYOUT_MIGRATABLE
    )
    assert (
        classify_emitter_update(
            extended,
            changed_type,
            extended_emitter.settings,
            changed_type_emitter.settings,
        )
        is ParticleRuntimeCompatibility.EMITTER_RESTART
    )


def test_event_queue_abi_change_restarts_the_owning_emitter():
    event_flow = ParticleEventFlow("impact", default_event_graph("impact"))
    previous_emitter = ParticleEmitterAsset(
        stable_id="event-abi",
        event_flows=(event_flow,),
    )
    next_emitter = replace(previous_emitter)
    previous = _lower(
        ParticleGraphAsset(
            emitters=(previous_emitter,),
            event_types=(ParticleEventType("impact", "Impact", 4),),
        )
    ).emitters[0]
    current = _lower(
        ParticleGraphAsset(
            emitters=(next_emitter,),
            event_types=(ParticleEventType("impact", "Impact", 8),),
        )
    ).emitters[0]

    assert (
        classify_emitter_update(
            previous,
            current,
            previous_emitter.settings,
            next_emitter.settings,
        )
        is ParticleRuntimeCompatibility.EMITTER_RESTART
    )


def test_emitter_seed_change_rebuilds_kernel_without_discarding_state():
    previous_emitter = ParticleEmitterAsset(
        stable_id="seeded-emitter",
        settings=EmitterSettings(seed=11),
    )
    next_emitter = replace(
        previous_emitter,
        settings=replace(previous_emitter.settings, seed=17),
    )
    previous = _lower(ParticleGraphAsset(emitters=(previous_emitter,))).emitters[0]
    current = _lower(ParticleGraphAsset(emitters=(next_emitter,))).emitters[0]

    assert previous.random_seed != current.random_seed
    assert (
        classify_emitter_update(
            previous,
            current,
            previous_emitter.settings,
            next_emitter.settings,
        )
        is ParticleRuntimeCompatibility.KERNEL_COMPATIBLE
    )


def test_collision_filter_and_material_scales_are_kernel_compatible_changes():
    previous_emitter = ParticleEmitterAsset(
        stable_id="collision-settings",
        settings=EmitterSettings(collision_enabled=True),
    )
    next_emitter = replace(
        previous_emitter,
        settings=replace(
            previous_emitter.settings,
            collision_layer_mask=0x15,
            collision_include_triggers=False,
            collision_bounce_scale=1.5,
            collision_friction_scale=0.25,
        ),
    )
    previous = _lower(ParticleGraphAsset(emitters=(previous_emitter,))).emitters[0]
    current = _lower(ParticleGraphAsset(emitters=(next_emitter,))).emitters[0]

    assert (
        classify_emitter_update(
            previous,
            current,
            previous_emitter.settings,
            next_emitter.settings,
        )
        is ParticleRuntimeCompatibility.KERNEL_COMPATIBLE
    )


def test_vector_field_graph_lowers_to_typed_data_interface_access():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("acceleration", "particle.attribute.velocity"),
            GraphNodeRecord(
                "position",
                "particle.attribute.get",
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
                PortKind.EXEC,
            ),
            GraphLinkRecord("position", "position", "value", "sample", "position"),
            GraphLinkRecord("value", "sample", "value", "acceleration", "value"),
        ),
    )
    emitter = ParticleEmitterAsset(
        stable_id="vector-field-kernel",
        update=update,
        data_interfaces=(
            VectorField(
                stable_id="wind",
                texture=AssetReference(guid="wind-texture"),
            ),
        ),
    )

    kernel = _lower(ParticleGraphAsset(emitters=(emitter,))).emitters[0]
    sample = next(
        instruction
        for instruction in kernel.update.instructions
        if instruction.opcode == "sample_vector_field"
    )
    simulation_vector = TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION)
    assert sample.immediate_dict() == {"interface": "wind"}
    assert sample.operands[0].value_type == simulation_vector
    assert sample.result_type == simulation_vector

    with pytest.raises(KernelCompileError, match="unknown data interface"):
        _lower(
            ParticleGraphAsset(
                emitters=(replace(emitter, data_interfaces=()),),
            )
        )

    with pytest.raises(KernelCompileError, match="not a VectorField"):
        _lower(
            ParticleGraphAsset(
                emitters=(
                    replace(
                        emitter,
                        data_interfaces=(
                            SdfVolume(
                                stable_id="wind",
                                texture=AssetReference(guid="wrong-resource"),
                            ),
                        ),
                    ),
                ),
            )
        )


def test_sdf_graph_samples_typed_distance_and_gradient_from_one_volume_interface():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "position",
                "particle.attribute.get",
                properties={"attribute": "builtin.position"},
            ),
            GraphNodeRecord(
                "distance",
                "particle.sdf.sample_distance",
                properties={"interface": "shape"},
            ),
            GraphNodeRecord(
                "gradient",
                "particle.sdf.sample_gradient",
                properties={"interface": "shape"},
            ),
            GraphNodeRecord("size", "particle.attribute.size"),
            GraphNodeRecord("velocity", "particle.attribute.velocity"),
        ),
        links=(
            GraphLinkRecord("root-size", "root.update", "out", "size", "in", PortKind.EXEC),
            GraphLinkRecord("size-velocity", "size", "out", "velocity", "in", PortKind.EXEC),
            GraphLinkRecord("position-distance", "position", "value", "distance", "position"),
            GraphLinkRecord("position-gradient", "position", "value", "gradient", "position"),
            GraphLinkRecord("distance-size", "distance", "distance", "size", "value"),
            GraphLinkRecord("gradient-velocity", "gradient", "gradient", "velocity", "value"),
        ),
    )
    emitter = ParticleEmitterAsset(
        stable_id="sdf-sample-kernel",
        update=update,
        data_interfaces=(
            SdfVolume(
                stable_id="shape",
                texture=AssetReference(guid="sdf-texture"),
            ),
        ),
    )

    kernel = _lower(ParticleGraphAsset(emitters=(emitter,))).emitters[0]
    samples = {
        instruction.opcode: instruction
        for instruction in kernel.update.instructions
        if instruction.opcode in {"sample_sdf_distance", "sample_sdf_gradient"}
    }
    simulation_vector = TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION)
    assert samples["sample_sdf_distance"].result_type == TypeRef(ValueType.F32)
    assert samples["sample_sdf_gradient"].result_type == simulation_vector
    assert all(
        instruction.operands[0].value_type == simulation_vector
        and instruction.immediate_dict() == {"interface": "shape"}
        for instruction in samples.values()
    )

    with pytest.raises(KernelCompileError, match="unknown data interface"):
        _lower(ParticleGraphAsset(emitters=(replace(emitter, data_interfaces=()),)))

    with pytest.raises(KernelCompileError, match="not a SdfVolume"):
        _lower(
            ParticleGraphAsset(
                emitters=(
                    replace(
                        emitter,
                        data_interfaces=(
                            VectorField(
                                stable_id="shape",
                                texture=AssetReference(guid="wrong-resource"),
                            ),
                        ),
                    ),
                ),
            )
        )


@pytest.mark.parametrize(
    ("output", "mode", "expected_type"),
    [
        ("position", "surface", TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION)),
        ("normal", "edge", TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION)),
        ("tangent", "vertex", TypeRef(ValueType.VEC4, CoordinateSpace.SIMULATION)),
        ("uv", "surface", TypeRef(ValueType.VEC2)),
        ("barycentric", "edge", TypeRef(ValueType.VEC3)),
    ],
)
def test_sample_mesh_input_lowers_typed_sampling_outputs(output, mode, expected_type):
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "sample",
                "particle.mesh.sample",
                properties={
                    "mesh": AssetReference(guid="surface-mesh").to_dict(),
                    "mode": mode,
                },
            ),
            GraphNodeRecord(
                "write",
                "particle.attribute.cache",
                properties={
                    "name": f"Sample {output}",
                    "value_type": expected_type.value_type.value,
                    "value_space": expected_type.space.value,
                    "composition": "set",
                },
            ),
        ),
        links=(
            GraphLinkRecord("exec", "root.update", "out", "write", "in", PortKind.EXEC),
            GraphLinkRecord("sample-value", "sample", output, "write", "value"),
        ),
    )
    emitter = ParticleEmitterAsset(
        stable_id="mesh-data-kernel",
        update=update,
    )

    kernel = _lower(ParticleGraphAsset(emitters=(emitter,))).emitters[0]
    sample = next(
        instruction
        for instruction in kernel.update.instructions
        if instruction.opcode == "sample_mesh"
    )

    immediates = sample.immediate_dict()
    assert immediates["interface"].startswith("sample.mesh.")
    assert immediates["mode"] == mode
    assert immediates["output"] == output
    assert immediates["seed"] == 0
    assert sample.operands == ()
    assert sample.result_type == expected_type


def test_sample_mesh_seed_is_stable_per_particle_slot_without_sample_connection():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "sample",
                "particle.mesh.sample",
                properties={
                    "mesh": AssetReference(guid="surface-mesh").to_dict(),
                    "mode": "surface",
                    "seed": 123,
                },
            ),
            GraphNodeRecord(
                "write",
                "particle.attribute.position",
            ),
        ),
        links=(
            GraphLinkRecord("exec", "root.update", "out", "write", "in", PortKind.EXEC),
            GraphLinkRecord("position", "sample", "position", "write", "value", PortKind.VALUE),
        ),
    )

    sample = next(
        instruction
        for instruction in _lower(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
        ).emitters[0].update.instructions
        if instruction.opcode == "sample_mesh"
    )

    assert sample.operands == ()
    assert sample.immediate_dict()["seed"] == 123


def test_kernel_random_slots_are_unique_and_source_uid_independent():
    emitter = ParticleEmitterAsset(stable_id="random-emitter")
    settings = replace(
        emitter.settings,
        shape=EmitterShape("sphere", radius=2.0),
    )
    first = ParticleGraphAsset(
        stable_id="random-graph",
        emitters=(replace(emitter, settings=settings),),
    )
    kernel = _lower(first)
    slots = []
    for instruction in kernel.emitters[0].init.instructions:
        immediates = instruction.immediate_dict()
        if "random_slot" in immediates:
            slots.append(immediates["random_slot"])
        slots.extend(immediates.get("random_slots", ()))

    assert slots == list(range(len(slots)))

    moved = first.to_dict()
    for stage in moved["emitters"][0]["stages"].values():
        if stage is None:
            continue
        for index, node in enumerate(stage["nodes"]):
            node["position"] = [float(index * 700), 350.0]
    second = ParticleGraphAsset.from_dict(moved)
    assert _lower(second).kernel_hash == kernel.kernel_hash


def test_expression_source_ids_do_not_change_kernel_semantics():
    def update_graph(prefix: str) -> GraphDocument:
        return GraphDocument(
            "particle.update",
            nodes=(
                GraphNodeRecord("root.update", "particle.root.update"),
                GraphNodeRecord(f"{prefix}.acceleration", "particle.attribute.velocity"),
                GraphNodeRecord(
                    f"{prefix}.constant",
                    "common.constant.vec3",
                    properties={"value": [0.0, -2.0, 0.0]},
                ),
            ),
            links=(
                GraphLinkRecord(
                    f"{prefix}.stream",
                    "root.update",
                    "out",
                    f"{prefix}.acceleration",
                    "in",
                    PortKind.EXEC,
                ),
                GraphLinkRecord(
                    f"{prefix}.value",
                    f"{prefix}.constant",
                    "value",
                    f"{prefix}.acceleration",
                    "value",
                ),
            ),
        )

    first = ParticleGraphAsset(
        stable_id="same-kernel",
        emitters=(ParticleEmitterAsset(stable_id="emitter", update=update_graph("first")),),
    )
    second = ParticleGraphAsset(
        stable_id="same-kernel",
        emitters=(ParticleEmitterAsset(stable_id="emitter", update=update_graph("second")),),
    )

    assert ParticleGraphCompiler().compile(first).behavior_hash == ParticleGraphCompiler().compile(second).behavior_hash
    assert _lower(first).kernel_hash == _lower(second).kernel_hash


def test_kernel_function_rejects_undefined_or_duplicate_ssa_values():
    f32 = TypeRef(ValueType.F32)
    with pytest.raises(KernelCompileError, match="undefined SSA"):
        ParticleKernelFunction(
            KernelStage.UPDATE,
            (
                KernelInstruction(
                    "add",
                    "%0",
                    f32,
                    (
                        KernelOperand(f32, value_id="%missing"),
                        KernelOperand(f32, value_id="%missing"),
                    ),
                ),
            ),
            (),
            (),
        )

    constant = KernelInstruction("constant", "%0", f32, immediates=(("value", 1.0),))
    with pytest.raises(KernelCompileError, match="duplicate SSA"):
        ParticleKernelFunction(
            KernelStage.UPDATE,
            (constant, constant),
            (),
            (),
        )


def test_shape_settings_and_authored_space_are_explicit_in_kernel_ir():
    emitter = ParticleEmitterAsset(stable_id="shape-emitter")
    settings = replace(
        emitter.settings,
        seed=42,
        shape=EmitterShape(
            "cone",
            CoordinateSpace.EMITTER_LOCAL,
            radius=2.5,
            angle_degrees=35.0,
            dimensions=(3.0, 4.0, 5.0),
        ),
    )
    kernel_emitter = _lower(
        ParticleGraphAsset(emitters=(replace(emitter, settings=settings),))
    ).emitters[0]
    samples = [
        instruction
        for instruction in kernel_emitter.init.instructions
        if instruction.opcode.startswith("sample_shape_")
    ]

    assert kernel_emitter.random_seed == 42
    assert len(samples) == 1
    for instruction in samples:
        assert instruction.result_type == TypeRef(
            ValueType.VEC3, CoordinateSpace.EMITTER_LOCAL
        )
        assert instruction.immediate_dict() | {"random_slots": [0, 1, 2]} == {
            "shape": "cone",
            "shape_space": "emitter_local",
            "radius": 2.5,
            "angle_degrees": 35.0,
            "dimensions": [3.0, 4.0, 5.0],
            "mesh": AssetReference().to_dict(),
            "mesh_mode": "surface",
            "sdf_interface": "",
            "sdf_mode": "surface",
            "random_slots": [0, 1, 2],
        }
    assert sum(
        instruction.opcode == "convert_space"
        for instruction in kernel_emitter.init.instructions
    ) == 1


@pytest.mark.parametrize("mode", ("surface", "volume"))
def test_sdf_emitter_shape_lowers_to_simulation_space_without_hidden_particle_state(mode):
    interface = SdfVolume(
        stable_id="spawn-field",
        texture=AssetReference(guid="sdf-texture-guid"),
    )
    emitter = ParticleEmitterAsset(
        stable_id="sdf-shape-emitter",
        settings=EmitterSettings(
            shape=EmitterShape(
                kind="sdf",
                sdf_interface=interface.stable_id,
                sdf_mode=mode,
            )
        ),
        data_interfaces=(interface,),
    )

    kernel_emitter = _lower(ParticleGraphAsset(emitters=(emitter,))).emitters[0]
    sample = next(
        instruction
        for instruction in kernel_emitter.init.instructions
        if instruction.opcode == "sample_shape_position"
    )

    assert sample.result_type == TypeRef(ValueType.VEC3, CoordinateSpace.SIMULATION)
    assert sample.immediate_dict()["shape"] == "sdf"
    assert sample.immediate_dict()["sdf_interface"] == interface.stable_id
    assert sample.immediate_dict()["sdf_mode"] == mode
    assert not any(
        instruction.opcode == "convert_space"
        for instruction in kernel_emitter.init.instructions
    )


def test_portable_random_reference_has_stable_golden_values():
    keys = (7, 42, 11, 1234, 2, 600, 9)

    assert particle_random_u32(*keys) == 0xA1940260
    assert particle_random_f32(*keys) == pytest.approx(0.6311646699905396, abs=0.0)
    assert 0.0 <= particle_random_f32(0, 0, 0, 0, 0, 0, 0) < 1.0


def test_kernel_opcode_contract_rejects_unknown_and_accepts_lifecycle_portable_operations():
    with pytest.raises(KernelCompileError, match="unknown particle kernel opcode"):
        KernelInstruction("backend_magic")

    function = ParticleKernelFunction(
        KernelStage.INIT,
        (
            KernelInstruction(
                "kill_if",
                operands=(KernelOperand(TypeRef(ValueType.BOOL), literal=True),),
            ),
        ),
        (),
        (),
    )
    assert function.instructions[0].opcode == "kill_if"


def test_random_expression_preserves_authored_node_seed_in_kernel_ir():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord("lifetime", "particle.attribute.lifetime"),
            GraphNodeRecord("random", "common.random.f32"),
            GraphNodeRecord("seed", "common.constant.u32", properties={"value": 73}),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.init", "out", "lifetime", "in", PortKind.EXEC
            ),
            GraphLinkRecord("value", "random", "value", "lifetime", "value"),
            GraphLinkRecord("seed", "seed", "value", "random", "seed"),
        ),
    )
    emitter = ParticleEmitterAsset(stable_id="random-expression", init=init)
    kernel = _lower(ParticleGraphAsset(emitters=(emitter,))).emitters[0]
    random_instruction = next(
        instruction
        for instruction in kernel.init.instructions
        if instruction.opcode == "random_f32" and instruction.source.node_uid == "random"
    )
    seed_operand = random_instruction.operands[2]
    seed_instruction = next(
        instruction
        for instruction in kernel.init.instructions
        if instruction.result_id == seed_operand.value_id
    )

    assert seed_operand.value_type == TypeRef(ValueType.U32)
    assert seed_instruction.immediate_dict()["value"] == 73


def test_persisted_kernel_ir_is_strictly_revalidated_and_hash_checked():
    program = _lower(ParticleGraphAsset(stable_id="persisted-kernel"))
    restored = ParticleKernelProgram.from_dict(program.to_dict())

    assert restored == program

    corrupted = copy.deepcopy(program.to_dict())
    corrupted["emitters"][0]["update"]["instructions"][0]["immediates"][0][1] = "unknown"
    with pytest.raises(KernelCompileError, match="uniform name or result type"):
        ParticleKernelProgram.from_dict(corrupted)

    corrupted = copy.deepcopy(program.to_dict())
    corrupted["emitters"][0]["random_seed"] += 1
    with pytest.raises(KernelCompileError, match="hash does not match"):
        ParticleKernelProgram.from_dict(corrupted)
