from __future__ import annotations

import copy
from dataclasses import replace

import pytest

from Infernux.graph import GraphDocument, GraphLinkRecord, GraphNodeRecord, PortKind
from Infernux.graph.types import CoordinateSpace, TypeRef, ValueType
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
    assert "set_alive" in update_opcodes
    assert update_opcodes[-1] == "set_alive"
    render_exports = [
        instruction.immediate_dict()["attribute"]
        for instruction in emitter.rendering.instructions
        if instruction.opcode == "export_attribute"
    ]
    assert render_exports == [
        "builtin.position",
        "builtin.size",
        "builtin.color",
        "builtin.age",
        "builtin.lifetime",
        "builtin.id",
    ]


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
    slots = [
        instruction.immediate_dict()["random_slot"]
        for instruction in kernel.emitters[0].init.instructions
        if "random_slot" in instruction.immediate_dict()
    ]

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
        assert instruction.immediate_dict() | {"random_slot": 0} == {
            "shape": "cone",
            "shape_space": "emitter_local",
            "radius": 2.5,
            "angle_degrees": 35.0,
            "dimensions": [3.0, 4.0, 5.0],
            "random_slot": 0,
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
                    "set_alive",
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
