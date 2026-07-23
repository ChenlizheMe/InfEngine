from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from Infernux.graph import GraphDocument, GraphLinkRecord, GraphNodeRecord, PortKind
from Infernux.graph.types import AssetReference, CoordinateSpace, TypeRef, ValueType
from Infernux.particle import (
    EmitterShape,
    KernelCompileError,
    KernelInstruction,
    KernelOperand,
    KernelStage,
    ParticleEmitterAsset,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleKernelFunction,
    ParticleKernelLowerer,
    ParticleKernelProgram,
    ParticleRuntimeCompatibility,
    PointCache,
    VectorField,
    classify_emitter_update,
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
        "builtin.size",
        "builtin.color",
        "builtin.rotation",
        "builtin.age",
        "builtin.lifetime",
        "builtin.id",
    ]


def test_mesh_orientation_lowers_degrees_to_radians_and_exports_vec3_state():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord(
                "orientation",
                "particle.attribute.set_orientation",
                properties={"degrees": [10.0, 20.0, 30.0]},
            ),
        ),
        links=(
            GraphLinkRecord(
                "init-stream", "root.init", "out", "orientation", "in", PortKind.STREAM
            ),
        ),
    )
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "angular-velocity",
                "particle.update.rotate_orientation",
                properties={"degrees_per_second": [90.0, 180.0, 270.0]},
            ),
        ),
        links=(
            GraphLinkRecord(
                "update-stream", "root.update", "out", "angular-velocity", "in", PortKind.STREAM
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
                "render-stream", "root.rendering", "out", "mesh", "in", PortKind.STREAM
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
    assert sum(instruction.opcode == "multiply" for instruction in emitter.update.instructions) >= 4


def test_ribbon_topology_attributes_lower_and_export_without_cpu_readback_contract():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord("strip", "particle.attribute.set_strip_id", properties={"value": 3}),
            GraphNodeRecord("order", "particle.attribute.set_ribbon_order", properties={"value": 9}),
            GraphNodeRecord("break", "particle.attribute.set_ribbon_break", properties={"value": True}),
        ),
        links=(
            GraphLinkRecord("a", "root.init", "out", "strip", "in", PortKind.STREAM),
            GraphLinkRecord("b", "strip", "out", "order", "in", PortKind.STREAM),
            GraphLinkRecord("c", "order", "out", "break", "in", PortKind.STREAM),
        ),
    )
    rendering = GraphDocument(
        "particle.rendering",
        nodes=(
            GraphNodeRecord("root.rendering", "particle.root.rendering"),
            GraphNodeRecord("ribbon", "particle.output.ribbon"),
        ),
        links=(
            GraphLinkRecord("render", "root.rendering", "out", "ribbon", "in", PortKind.STREAM),
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


def test_plane_collision_lowers_after_position_integration_with_portable_state_writes():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "collision",
                "particle.update.collide_plane",
                properties={"radius": 0.25, "restitution": 0.5, "friction": 0.25},
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "collision", "in", PortKind.STREAM
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
                "particle.update.collide_sphere",
                properties={
                    "center": [0.0, 1.0, 0.0],
                    "sphere_radius": 2.0,
                    "particle_radius": 0.25,
                },
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "collision", "in", PortKind.STREAM
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
            PointCache(
                stable_id="points",
                cache=AssetReference(path_hint="Assets/Face.pointcache"),
            ),
        ),
    )
    first = _lower(ParticleGraphAsset(stable_id="data-graph", emitters=(first_emitter,)))
    restored = ParticleKernelProgram.from_dict(first.to_dict())

    assert restored == first
    assert [value.stable_id for value in restored.emitters[0].data_interfaces] == [
        "points",
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
    ] == ["points", "wind"]

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


def test_vector_field_graph_lowers_to_typed_data_interface_access():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("acceleration", "particle.update.acceleration"),
            GraphNodeRecord("position", "particle.attribute.read_vec3"),
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
                PortKind.STREAM,
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
                            PointCache(
                                stable_id="wind",
                                cache=AssetReference(guid="wrong-resource"),
                            ),
                        ),
                    ),
                ),
            )
        )


def test_kernel_random_slots_are_unique_and_source_uid_independent():
    emitter = ParticleEmitterAsset(stable_id="random-emitter")
    settings = replace(
        emitter.settings,
        lifetime=replace(emitter.settings.lifetime, minimum=1.0, maximum=3.0),
        initial_speed=replace(emitter.settings.initial_speed, minimum=2.0, maximum=4.0),
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
                GraphNodeRecord(f"{prefix}.acceleration", "particle.update.acceleration"),
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
                    PortKind.STREAM,
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
    assert len(samples) == 2
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
            "random_slots": [0, 1, 2],
        }
    assert sum(
        instruction.opcode == "convert_space"
        for instruction in kernel_emitter.init.instructions
    ) == 2


def test_portable_random_reference_has_stable_golden_values():
    keys = (7, 42, 11, 1234, 2, 600, 9)

    assert particle_random_u32(*keys) == 0xA1940260
    assert particle_random_f32(*keys) == pytest.approx(0.6311646699905396, abs=0.0)
    assert 0.0 <= particle_random_f32(0, 0, 0, 0, 0, 0, 0) < 1.0


def test_kernel_opcode_contract_rejects_unknown_or_stage_invalid_operations():
    with pytest.raises(KernelCompileError, match="unknown particle kernel opcode"):
        KernelInstruction("backend_magic")

    with pytest.raises(KernelCompileError, match="not valid in the init stage"):
        ParticleKernelFunction(
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


def test_random_expression_preserves_authored_node_seed_in_kernel_ir():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord("lifetime", "particle.init.set_lifetime"),
            GraphNodeRecord("random", "common.random.f32"),
            GraphNodeRecord("seed", "common.constant.u32", properties={"value": 73}),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.init", "out", "lifetime", "in", PortKind.STREAM
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
