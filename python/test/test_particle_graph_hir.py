from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from Infernux.graph import GraphDocument, GraphLinkRecord, GraphNodeRecord, PortKind
from Infernux.particle import (
    EmitterSettings,
    EmitterShape,
    KernelCompileError,
    MeshEmissionMode,
    ParticleCompileError,
    ParticleEmitterAsset,
    ParticleAttribute,
    ParticleEventField,
    ParticleEventRoute,
    ParticleEventType,
    ParticleGraphAsset,
    ParticleGraphCompiler,
    ParticleGraphSchemaError,
    ParticleParameter,
    ParticleKernelLowerer,
    ParticleStage,
    ParticleScriptCompiler,
    ParticleScriptError,
    ParticleArtifactError,
    ParticleArtifactRegistry,
    ParticleBurst,
    ParticleRuntimeMetadataError,
    SdfVolume,
    SimulationSpace,
    VectorField,
    decode_particle_runtime_metadata,
)
from Infernux.graph import AssetReference, CoordinateSpace, TypeRef, ValueType
from Infernux.particle.nodes import particle_event_output_type_id


def _operations(stage):
    return tuple(stage.flow.iter_operations())


def test_default_particle_graph_has_three_immutable_stage_roots_and_output():
    asset = ParticleGraphAsset()
    emitter = asset.emitters[0]

    assert emitter.init.nodes[0].uid == "root.init"
    assert emitter.update.nodes[0].uid == "root.update"
    assert emitter.rendering.nodes[0].uid == "root.rendering"
    assert emitter.rendering.nodes[1].type_id == "particle.output.sprite"
    assert [node.type_id for node in emitter.init.nodes[1:]] == [
        "particle.attribute.lifetime",
        "particle.attribute.velocity",
    ]
    assert emitter.init.nodes[1].properties["composition"] == "set"
    assert emitter.init.nodes[2].properties["composition"] == "set"
    assert [node.type_id for node in emitter.update.nodes[1:]] == [
        "particle.attribute.velocity"
    ]
    assert emitter.update.nodes[1].properties["composition"] == "add"

    restored = ParticleGraphAsset.from_json(asset.canonical_json())
    assert restored == asset
    assert restored.semantic_hash() == asset.semantic_hash()
    hir = ParticleGraphCompiler().compile(asset)
    assert "builtin.orientation" not in {
        attribute.stable_id for attribute in hir.emitters[0].attributes
    }


def test_particle_exec_fan_out_is_preserved_in_hir_and_allows_disjoint_writes():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "move",
                "particle.attribute.position",
                properties={"value": [1.0, 2.0, 3.0]},
            ),
            GraphNodeRecord(
                "grow",
                "particle.attribute.size",
                properties={"value": 2.0},
            ),
        ),
        links=(
            GraphLinkRecord(
                "exec-move",
                "root.update",
                "out",
                "move",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "exec-grow",
                "root.update",
                "out",
                "grow",
                "in",
                PortKind.EXEC,
            ),
        ),
    )

    stage = ParticleGraphCompiler().compile(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    ).emitters[0].update

    assert {edge.link_uid for edge in stage.flow.edges} == {"exec-move", "exec-grow"}
    root = next(block for block in stage.flow.blocks if block.node_uid == "root.update")
    assert root.operations == ()
    assert set(root.outgoing_edges) == {"exec-move", "exec-grow"}
    assert [
        block.operations[0].source_node_uid
        for block in stage.flow.blocks
        if block.operations
    ] == [
        "grow",
        "move",
    ]
    assert len(stage.flow.lanes) == 3
    branch_lanes = {
        edge.link_uid: stage.flow.lanes[edge.lane_index].stable_id
        for edge in stage.flow.edges
    }
    assert branch_lanes["exec-grow"] != branch_lanes["exec-move"]


def test_particle_execution_lane_identity_ignores_link_order_and_link_uid():
    nodes = (
        GraphNodeRecord("root.update", "particle.root.update"),
        GraphNodeRecord("move", "particle.attribute.position"),
        GraphNodeRecord("grow", "particle.attribute.size"),
    )

    def compile_links(links):
        asset = ParticleGraphAsset(
            stable_id="stable-lane-asset",
            emitters=(
                ParticleEmitterAsset(
                    stable_id="stable-lane-emitter",
                    update=GraphDocument("particle.update", nodes=nodes, links=links),
                ),
            ),
        )
        program = ParticleGraphCompiler().compile(asset)
        flow = program.emitters[0].update.flow
        semantic_lanes = {
            (
                edge.source_node_uid,
                edge.source_port_id,
                edge.target_node_uid,
                edge.target_port_id,
            ): flow.lanes[edge.lane_index].stable_id
            for edge in flow.edges
        }
        return program.behavior_hash, semantic_lanes

    first = (
        GraphLinkRecord("link-a", "root.update", "out", "move", "in", PortKind.EXEC),
        GraphLinkRecord("link-b", "root.update", "out", "grow", "in", PortKind.EXEC),
    )
    reordered_and_renamed = (
        GraphLinkRecord("unrelated-z", "root.update", "out", "grow", "in", PortKind.EXEC),
        GraphLinkRecord("unrelated-y", "root.update", "out", "move", "in", PortKind.EXEC),
    )

    first_hash, first_lanes = compile_links(first)
    second_hash, second_lanes = compile_links(reordered_and_renamed)

    assert first_lanes == second_lanes
    assert first_hash == second_hash


def test_particle_exec_parallel_writes_require_explicit_order_or_merge():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("first", "particle.attribute.size"),
            GraphNodeRecord("second", "particle.attribute.size"),
        ),
        links=(
            GraphLinkRecord(
                "exec-first",
                "root.update",
                "out",
                "first",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "exec-second",
                "root.update",
                "out",
                "second",
                "in",
                PortKind.EXEC,
            ),
        ),
    )

    with pytest.raises(
        ParticleCompileError,
        match="parallel particle Exec branches have an unordered state dependency.*builtin.size",
    ):
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
        )


def test_particle_exec_parallel_read_write_dependency_requires_order_or_join():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("write-position", "particle.attribute.position"),
            GraphNodeRecord(
                "read-position",
                "particle.attribute.get",
                properties={"attribute": "builtin.position"},
            ),
            GraphNodeRecord("accelerate", "particle.attribute.velocity"),
        ),
        links=(
            GraphLinkRecord(
                "exec-write",
                "root.update",
                "out",
                "write-position",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "exec-read",
                "root.update",
                "out",
                "accelerate",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "value-read",
                "read-position",
                "value",
                "accelerate",
                "value",
                PortKind.VALUE,
            ),
        ),
    )

    with pytest.raises(
        ParticleCompileError,
        match="unordered state dependency.*builtin.position",
    ):
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
        )


def test_particle_exec_multiple_inputs_require_an_explicit_join():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("left", "particle.attribute.size"),
            GraphNodeRecord("right", "particle.attribute.position"),
            GraphNodeRecord("tail", "particle.update.kill_if"),
        ),
        links=(
            GraphLinkRecord("root-left", "root.update", "out", "left", "in", PortKind.EXEC),
            GraphLinkRecord("root-right", "root.update", "out", "right", "in", PortKind.EXEC),
            GraphLinkRecord("left-tail", "left", "out", "tail", "in", PortKind.EXEC),
            GraphLinkRecord("right-tail", "right", "out", "tail", "in", PortKind.EXEC),
        ),
    )

    with pytest.raises(ParticleCompileError, match="use an explicit Join All node"):
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
        )


def test_particle_join_rejects_an_input_from_an_unreachable_exec_branch():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("reachable", "particle.attribute.size"),
            GraphNodeRecord("orphan", "particle.attribute.position"),
            GraphNodeRecord("join", "particle.control.join_all"),
            GraphNodeRecord("tail", "particle.update.kill_if"),
        ),
        links=(
            GraphLinkRecord(
                "root-reachable",
                "root.update",
                "out",
                "reachable",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "reachable-join",
                "reachable",
                "out",
                "join",
                "in0",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "orphan-join",
                "orphan",
                "out",
                "join",
                "in1",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "join-tail", "join", "out", "tail", "in", PortKind.EXEC
            ),
        ),
    )

    with pytest.raises(
        ParticleCompileError,
        match="Join All node 'join' has an input from unreachable Exec node 'orphan'",
    ):
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
        )


def test_particle_exec_join_all_continues_after_every_parallel_branch():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("left", "particle.attribute.size"),
            GraphNodeRecord("right", "particle.attribute.position"),
            GraphNodeRecord("join", "particle.control.join_all"),
            GraphNodeRecord("tail", "particle.update.kill_if"),
        ),
        links=(
            GraphLinkRecord("root-left", "root.update", "out", "left", "in", PortKind.EXEC),
            GraphLinkRecord("root-right", "root.update", "out", "right", "in", PortKind.EXEC),
            GraphLinkRecord("left-join", "left", "out", "join", "in0", PortKind.EXEC),
            GraphLinkRecord("right-join", "right", "out", "join", "in1", PortKind.EXEC),
            GraphLinkRecord("join-tail", "join", "out", "tail", "in", PortKind.EXEC),
        ),
    )

    program = ParticleGraphCompiler().compile(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    )
    stage = program.emitters[0].update
    schedule = [
        block.node_uid
        for block in stage.flow.blocks
        if block.node_uid != stage.flow.entry_node_uid
    ]

    assert schedule.index("join") > schedule.index("left")
    assert schedule.index("join") > schedule.index("right")
    assert schedule.index("tail") > schedule.index("join")
    assert len(stage.flow.joins) == 1
    join = stage.flow.joins[0]
    assert join.node_uid == "join"
    assert len(join.input_lane_indices) == 2
    assert join.output_lane_index not in join.input_lane_indices
    kernel_program = ParticleKernelLowerer().lower(program)
    kernel_emitter = kernel_program.emitters[0]
    kernel = kernel_emitter.update
    assert all(
        instruction.source.node_uid != "join"
        for instruction in kernel.instructions
    )
    update_flow = next(
        flow
        for flow in kernel_emitter.flows
        if flow.lifecycle_stage is ParticleStage.UPDATE
    )
    assert [lane.index for lane in update_flow.lanes] == list(
        range(len(update_flow.lanes))
    )
    assert update_flow.lanes[0].parent_index == -1
    assert all(
        0 <= lane.parent_index < lane.index
        for lane in update_flow.lanes[1:]
    )
    blocks = {block.source_node_uid: block for block in update_flow.blocks}
    assert blocks["left"].lane_index != blocks["right"].lane_index
    assert blocks["join"].instruction_begin == blocks["join"].instruction_end
    assert blocks["tail"].instruction_begin >= blocks["join"].instruction_end
    assert [block.source_node_uid for block in update_flow.blocks] == [
        "root.update",
        "left",
        "right",
        "join",
        "tail",
    ]
    assert len(update_flow.joins) == 1
    kernel_join = update_flow.joins[0]
    assert kernel_join.source_node_uid == "join"
    assert set(kernel_join.input_lane_indices) == {
        blocks["left"].lane_index,
        blocks["right"].lane_index,
    }
    assert kernel_join.output_lane_index == blocks["join"].lane_index

    restored = type(kernel_program).from_dict(kernel_program.to_dict())
    assert restored == kernel_program
    assert restored.kernel_hash == kernel_program.kernel_hash

    stale = kernel_program.to_dict()
    stale["emitters"][0].pop("flows")
    with pytest.raises(KernelCompileError, match="kernel emitter keys"):
        type(kernel_program).from_dict(stale)

    changed_topology = kernel_program.to_dict()
    serialized_update_flow = next(
        flow
        for flow in changed_topology["emitters"][0]["flows"]
        if flow["lifecycle_stage"] == "update"
    )
    branch_lane_indices = sorted(
        (blocks["left"].lane_index, blocks["right"].lane_index)
    )
    serialized_update_flow["lanes"][branch_lane_indices[1]][
        "parent_index"
    ] = branch_lane_indices[0]
    with pytest.raises(KernelCompileError, match="kernel hash"):
        type(kernel_program).from_dict(changed_topology)


def test_particle_if_activates_only_the_selected_exec_branch():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("condition", "common.constant.bool", properties={"value": True}),
            GraphNodeRecord("if", "particle.control.if"),
            GraphNodeRecord("true-size", "particle.attribute.size", properties={"value": 2.0}),
            GraphNodeRecord("false-size", "particle.attribute.size", properties={"value": 0.5}),
        ),
        links=(
            GraphLinkRecord("root-if", "root.update", "out", "if", "in", PortKind.EXEC),
            GraphLinkRecord("condition-if", "condition", "value", "if", "condition"),
            GraphLinkRecord("if-true", "if", "true", "true-size", "in", PortKind.EXEC),
            GraphLinkRecord("if-false", "if", "false", "false-size", "in", PortKind.EXEC),
        ),
    )

    program = ParticleGraphCompiler().compile(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    )
    stage = program.emitters[0].update
    by_uid = {
        operation.source_node_uid: operation
        for operation in stage.flow.iter_operations()
    }

    assert by_uid["if"].opcode == "control.if"
    assert by_uid["true-size"].execution_predicates[0].expected is True
    assert by_uid["false-size"].execution_predicates[0].expected is False
    assert next(edge for edge in stage.flow.edges if edge.link_uid == "if-true").predicate_expected is True
    assert next(edge for edge in stage.flow.edges if edge.link_uid == "if-false").predicate_expected is False

    kernel = ParticleKernelLowerer().lower(program).emitters[0].update
    opcodes = [instruction.opcode for instruction in kernel.instructions]
    assert opcodes.count("begin_if") == 2
    assert opcodes.count("end_if") == 2
    assert "logical_not" in opcodes


def test_particle_join_all_rejects_mutually_exclusive_if_branches():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "condition",
                "common.constant.bool",
                properties={"value": True},
            ),
            GraphNodeRecord("if", "particle.control.if"),
            GraphNodeRecord("true-size", "particle.attribute.size"),
            GraphNodeRecord("false-position", "particle.attribute.position"),
            GraphNodeRecord("join", "particle.control.join_all"),
            GraphNodeRecord("tail", "particle.attribute.color"),
        ),
        links=(
            GraphLinkRecord(
                "root-if", "root.update", "out", "if", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "condition-if", "condition", "value", "if", "condition"
            ),
            GraphLinkRecord(
                "if-true", "if", "true", "true-size", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "if-false",
                "if",
                "false",
                "false-position",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "true-join",
                "true-size",
                "out",
                "join",
                "in0",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "false-join",
                "false-position",
                "out",
                "join",
                "in1",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "join-tail", "join", "out", "tail", "in", PortKind.EXEC
            ),
        ),
    )

    with pytest.raises(
        ParticleCompileError,
        match="cannot join mutually exclusive or differently predicated branches",
    ):
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
        )


def test_particle_waits_emit_stable_suspension_resume_descriptors():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("frame-count", "common.constant.i32", properties={"value": 3}),
            GraphNodeRecord("seconds", "common.constant.f32", properties={"value": 0.25}),
            GraphNodeRecord("wait.frames", "particle.control.wait_frames"),
            GraphNodeRecord("wait.seconds", "particle.control.wait_seconds"),
            GraphNodeRecord("tail", "particle.attribute.size"),
        ),
        links=(
            GraphLinkRecord(
                "root-wait-frames",
                "root.update",
                "out",
                "wait.frames",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "frames-value",
                "frame-count",
                "value",
                "wait.frames",
                "frames",
                PortKind.VALUE,
            ),
            GraphLinkRecord(
                "wait-frames-seconds",
                "wait.frames",
                "out",
                "wait.seconds",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "seconds-value",
                "seconds",
                "value",
                "wait.seconds",
                "seconds",
                PortKind.VALUE,
            ),
            GraphLinkRecord(
                "wait-seconds-tail",
                "wait.seconds",
                "out",
                "tail",
                "in",
                PortKind.EXEC,
            ),
        ),
    )

    stage = ParticleGraphCompiler().compile(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    ).emitters[0].update

    assert [item.kind.value for item in stage.flow.suspensions] == [
        "frames",
        "seconds",
    ]
    frame_wait, second_wait = stage.flow.suspensions
    assert frame_wait.resume_program_counter == 1
    assert frame_wait.resume_node_uid == "wait.seconds"
    assert frame_wait.value_id
    assert frame_wait.literal == 1
    assert second_wait.resume_program_counter == 2
    assert second_wait.resume_node_uid == "tail"
    assert second_wait.value_id
    assert second_wait.literal == pytest.approx(0.1)
    assert frame_wait.lane_stable_id == stage.flow.lanes[frame_wait.lane_index].stable_id
    assert second_wait.lane_stable_id == stage.flow.lanes[second_wait.lane_index].stable_id

    from Infernux.particle.artifact import _program_to_dict

    serialized = _program_to_dict(
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
        )
    )
    suspension_payload = serialized["emitters"][0]["update"]["flow"]["suspensions"]
    assert [item["resume_program_counter"] for item in suspension_payload] == [1, 2]
    assert [item["resume_node_uid"] for item in suspension_payload] == [
        "wait.seconds",
        "tail",
    ]

    kernel_program = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
        )
    )
    kernel_emitter = kernel_program.emitters[0]
    assert [item.resume_program_counter for item in kernel_emitter.suspensions] == [
        1,
        2,
    ]
    assert [item.stage_resume_program_counter for item in kernel_emitter.suspensions] == [
        1,
        2,
    ]
    assert [item.lifecycle_stage for item in kernel_emitter.suspensions] == [
        ParticleStage.UPDATE,
        ParticleStage.UPDATE,
    ]
    for item in kernel_emitter.suspensions:
        update_flow = next(
            flow
            for flow in kernel_emitter.flows
            if flow.lifecycle_stage is ParticleStage.UPDATE
        )
        blocks = {block.source_node_uid: block for block in update_flow.blocks}
        instruction = kernel_emitter.update.instructions[
            item.suspend_instruction_index
        ]
        assert instruction.opcode == f"suspend_{item.kind.value}"
        assert instruction.immediate_dict()["resume_program_counter"] == (
            item.resume_program_counter
        )
        assert blocks[item.source_node_uid].instruction_begin <= (
            item.suspend_instruction_index
        ) < blocks[item.source_node_uid].instruction_end
        assert item.resume_instruction_index == blocks[
            item.resume_node_uid
        ].instruction_begin

    restored_kernel = type(kernel_program).from_dict(kernel_program.to_dict())
    assert restored_kernel == kernel_program


def test_particle_wait_can_finish_a_lifecycle_continuation_and_rejects_rendering():
    no_continuation = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("wait", "particle.control.wait_frames"),
        ),
        links=(
            GraphLinkRecord(
                "root-wait",
                "root.update",
                "out",
                "wait",
                "in",
                PortKind.EXEC,
            ),
        ),
    )
    program = ParticleGraphCompiler().compile(
        ParticleGraphAsset(
            emitters=(ParticleEmitterAsset(update=no_continuation),)
        )
    )
    suspension = program.emitters[0].update.flow.suspensions[0]
    assert suspension.resume_node_uid == ""

    kernel = ParticleKernelLowerer().lower(program)
    kernel_suspension = kernel.emitters[0].suspensions[0]
    assert kernel_suspension.resume_node_uid == ""
    assert kernel_suspension.resume_instruction_index == -1
    assert type(kernel).from_dict(kernel.to_dict()) == kernel

    rendering = GraphDocument(
        "particle.rendering",
        nodes=(
            GraphNodeRecord("root.rendering", "particle.root.rendering"),
            GraphNodeRecord("wait", "particle.control.wait_seconds"),
            GraphNodeRecord("output", "particle.output.sprite"),
        ),
        links=(
            GraphLinkRecord(
                "root-wait",
                "root.rendering",
                "out",
                "wait",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "wait-output",
                "wait",
                "out",
                "output",
                "in",
                PortKind.EXEC,
            ),
        ),
    )
    with pytest.raises(ParticleCompileError, match="Rendering cannot contain Wait"):
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(
                emitters=(ParticleEmitterAsset(rendering=rendering),)
            )
        )


def test_particle_until_repeats_the_preceding_operation_and_keeps_wait_distinct():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "velocity.down",
                "particle.attribute.velocity",
                properties={
                    "composition": "add",
                    "value": [0.0, -1.0, 0.0],
                },
            ),
            GraphNodeRecord(
                "until.down",
                "particle.control.until_seconds",
                properties={"seconds": 3.0},
            ),
            GraphNodeRecord(
                "velocity.side",
                "particle.attribute.velocity",
                properties={
                    "composition": "add",
                    "value": [1.0, 0.0, 0.0],
                },
            ),
            GraphNodeRecord(
                "until.side",
                "particle.control.until_frames",
                properties={"frames": 5},
            ),
        ),
        links=(
            GraphLinkRecord(
                "root-down", "root.update", "out", "velocity.down", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "down-until", "velocity.down", "out", "until.down", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "until-side", "until.down", "out", "velocity.side", "in", PortKind.EXEC
            ),
            GraphLinkRecord(
                "side-until", "velocity.side", "out", "until.side", "in", PortKind.EXEC
            ),
        ),
    )

    program = ParticleGraphCompiler().compile(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    )
    stage = program.emitters[0].update
    assert [item.kind.value for item in stage.flow.suspensions] == [
        "until_seconds",
        "until_frames",
    ]
    assert [item.resume_node_uid for item in stage.flow.suspensions] == [
        "velocity.down",
        "velocity.side",
    ]
    kernel = ParticleKernelLowerer().lower(program).emitters[0]
    assert [
        kernel.update.instructions[item.suspend_instruction_index].opcode
        for item in kernel.suspensions
    ] == ["until_seconds", "until_frames"]
    assert [item.resume_node_uid for item in kernel.suspensions] == [
        "velocity.down",
        "velocity.side",
    ]


def test_particle_delta_time_is_an_explicit_update_value_not_attribute_semantics():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "gravity",
                "common.constant.vec3",
                properties={"value": [0.0, -9.8, 0.0]},
            ),
            GraphNodeRecord("delta", "particle.context.delta_time"),
            GraphNodeRecord("step", "common.math.multiply"),
            GraphNodeRecord(
                "velocity",
                "particle.attribute.velocity",
                properties={"composition": "add"},
            ),
        ),
        links=(
            GraphLinkRecord(
                "gravity-step", "gravity", "value", "step", "a", PortKind.VALUE
            ),
            GraphLinkRecord(
                "delta-step", "delta", "value", "step", "b", PortKind.VALUE
            ),
            GraphLinkRecord(
                "step-velocity", "step", "result", "velocity", "value", PortKind.VALUE
            ),
            GraphLinkRecord(
                "root-velocity", "root.update", "out", "velocity", "in", PortKind.EXEC
            ),
        ),
    )
    program = ParticleGraphCompiler().compile(
        ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
    )
    stage = program.emitters[0].update
    assert any(
        instruction.opcode == "delta_time"
        for instruction in stage.expressions.instructions
    )
    operation = next(
        item
        for item in stage.flow.iter_operations()
        if item.source_node_uid == "velocity"
    )
    assert operation.parameter_dict()["composition"] == "add"

    kernel = ParticleKernelLowerer().lower(program).emitters[0].update
    explicit_delta = next(
        instruction
        for instruction in kernel.instructions
        if instruction.source.node_uid == "delta"
    )
    assert explicit_delta.opcode == "load_uniform"
    assert explicit_delta.immediate_dict() == {"name": "delta_time"}

    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord("delta", "particle.context.delta_time"),
            GraphNodeRecord("size", "particle.attribute.size"),
        ),
        links=(
            GraphLinkRecord(
                "delta-size", "delta", "value", "size", "value", PortKind.VALUE
            ),
            GraphLinkRecord(
                "root-size", "root.init", "out", "size", "in", PortKind.EXEC
            ),
        ),
    )
    with pytest.raises(ParticleCompileError, match="Delta Time is only valid"):
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(init=init),))
        )


def test_kernel_continuation_program_counters_are_unique_across_lifecycle_stages():
    def waiting_stage(stage: str):
        return GraphDocument(
            f"particle.{stage}",
            nodes=(
                GraphNodeRecord(f"root.{stage}", f"particle.root.{stage}"),
                GraphNodeRecord(f"wait.{stage}", "particle.control.wait_frames"),
                GraphNodeRecord(f"tail.{stage}", "particle.attribute.size"),
            ),
            links=(
                GraphLinkRecord(
                    f"root-wait.{stage}",
                    f"root.{stage}",
                    "out",
                    f"wait.{stage}",
                    "in",
                    PortKind.EXEC,
                ),
                GraphLinkRecord(
                    f"wait-tail.{stage}",
                    f"wait.{stage}",
                    "out",
                    f"tail.{stage}",
                    "in",
                    PortKind.EXEC,
                ),
            ),
        )

    emitter = ParticleEmitterAsset(
        settings=EmitterSettings(collision_enabled=True),
        init=waiting_stage("init"),
        collision_enter=waiting_stage("collision_enter"),
    )
    kernel_emitter = ParticleKernelLowerer().lower(
        ParticleGraphCompiler().compile(ParticleGraphAsset(emitters=(emitter,)))
    ).emitters[0]

    assert [item.lifecycle_stage for item in kernel_emitter.suspensions] == [
        ParticleStage.INIT,
        ParticleStage.COLLISION_ENTER,
    ]
    assert [item.stage_resume_program_counter for item in kernel_emitter.suspensions] == [
        1,
        1,
    ]
    assert [item.resume_program_counter for item in kernel_emitter.suspensions] == [
        1,
        2,
    ]


def _collision_lifecycle_graph(stage: str, size: float):
    operation_uid = f"{stage.removeprefix('collision_')}.size"
    return GraphDocument(
        f"particle.{stage}",
        nodes=(
            GraphNodeRecord(f"root.{stage}", f"particle.root.{stage}"),
            GraphNodeRecord(
                operation_uid,
                "particle.attribute.size",
                properties={"value": size},
            ),
        ),
        links=(
            GraphLinkRecord(
                f"{stage}.exec",
                f"root.{stage}",
                "out",
                operation_uid,
                "in",
                PortKind.EXEC,
            ),
        ),
    )


def _collision_lifecycle_stages():
    return {
        "collision_enter": _collision_lifecycle_graph("collision_enter", 2.0),
        "collision_stay": _collision_lifecycle_graph("collision_stay", 3.0),
        "collision_exit": _collision_lifecycle_graph("collision_exit", 1.0),
    }


def test_collision_lifecycle_roots_are_first_class_emitter_stages():
    emitter = ParticleEmitterAsset(
        settings=EmitterSettings(collision_enabled=True),
        **_collision_lifecycle_stages(),
    )
    program = ParticleGraphCompiler().compile(
        ParticleGraphAsset(emitters=(emitter,))
    )
    hir = program.emitters[0]

    assert hir.collision_enter.stage is ParticleStage.COLLISION_ENTER
    assert hir.collision_stay.stage is ParticleStage.COLLISION_STAY
    assert hir.collision_exit.stage is ParticleStage.COLLISION_EXIT
    runtime_conditions = {
        operation.source_node_uid: operation.execution_predicates[-1].runtime_condition
        for stage in (hir.collision_enter, hir.collision_stay, hir.collision_exit)
        if stage is not None
        for operation in stage.flow.iter_operations()
    }
    assert runtime_conditions == {
        "enter.size": "collision_enter",
        "stay.size": "collision_stay",
        "exit.size": "collision_exit",
    }

    kernel_program = ParticleKernelLowerer().lower(program)
    kernel_emitter = kernel_program.emitters[0]
    kernel = kernel_emitter.update
    opcodes = [instruction.opcode for instruction in kernel.instructions]
    assert opcodes.count("collide_scene") == 1
    assert opcodes.count("begin_if") == 3
    assert opcodes.count("end_if") == 3
    assert "builtin.collision_active" in kernel.written_attributes

    flow_by_stage = {
        flow.lifecycle_stage: flow for flow in kernel_emitter.flows
    }
    assert set(flow_by_stage) == {
        ParticleStage.INIT,
        ParticleStage.UPDATE,
        ParticleStage.COLLISION_ENTER,
        ParticleStage.COLLISION_STAY,
        ParticleStage.COLLISION_EXIT,
        ParticleStage.RENDERING,
    }
    expected_nodes = {
        ParticleStage.COLLISION_ENTER: "enter.size",
        ParticleStage.COLLISION_STAY: "stay.size",
        ParticleStage.COLLISION_EXIT: "exit.size",
    }
    for lifecycle_stage, source_node_uid in expected_nodes.items():
        flow = flow_by_stage[lifecycle_stage]
        assert flow.kernel_stage.value == "update"
        assert flow.entry_node_uid == f"root.{lifecycle_stage.value}"
        assert [lane.index for lane in flow.lanes] == list(range(len(flow.lanes)))
        block = next(
            block
            for block in flow.blocks
            if block.source_node_uid == source_node_uid
        )
        assert block.instruction_begin < block.instruction_end
        assert all(
            instruction.source.node_uid == source_node_uid
            for instruction in kernel.instructions[
                block.instruction_begin : block.instruction_end
            ]
        )

    restored = type(kernel_program).from_dict(kernel_program.to_dict())
    assert restored == kernel_program
    assert restored.kernel_hash == kernel_program.kernel_hash


def test_disabled_collision_keeps_authored_roots_dormant():
    emitter = ParticleEmitterAsset(
        settings=EmitterSettings(collision_enabled=False),
        **_collision_lifecycle_stages(),
    )
    hir = ParticleGraphCompiler().compile(
        ParticleGraphAsset(emitters=(emitter,))
    ).emitters[0]

    assert hir.collision_enter is None
    assert hir.collision_stay is None
    assert hir.collision_exit is None
    assert not any(
        operation.source_node_uid.endswith(".size")
        for operation in hir.update.flow.iter_operations()
    )
    assert "builtin.collision_active" not in {
        attribute.stable_id for attribute in hir.attributes
    }


def test_particle_script_collision_methods_compile_as_lifecycle_stages():
    source = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class CollisionGraph(ParticleScript):
    class Sparks(ParticleEmitter):
        stable_id = "sparks"
        settings = EmitterSettings(collision_enabled=True)

        def init(self, ctx, particles):
            pass

        def update(self, ctx, particles):
            pass

        def collision_enter(self, ctx, particles):
            particles.set_size(2.0)

        def collision_stay(self, ctx, particles):
            particles.set_size(3.0)

        def collision_exit(self, ctx, particles):
            particles.set_size(1.0)

        def rendering(self, ctx, particles):
            particles.sprite()
'''

    program = ParticleScriptCompiler().compile(
        source, source_name="Collision.particle.py"
    )

    emitter = program.emitters[0]
    assert emitter.collision_enter.stage is ParticleStage.COLLISION_ENTER
    assert emitter.collision_stay.stage is ParticleStage.COLLISION_STAY
    assert emitter.collision_exit.stage is ParticleStage.COLLISION_EXIT


def test_particle_script_collision_methods_require_collision_setting():
    source = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class CollisionGraph(ParticleScript):
    class Sparks(ParticleEmitter):
        stable_id = "sparks"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            pass

        def update(self, ctx, particles):
            pass

        def collision_enter(self, ctx, particles):
            particles.set_size(2.0)

        def rendering(self, ctx, particles):
            particles.sprite()
'''

    with pytest.raises(
        ParticleScriptError,
        match="collision lifecycle methods require",
    ):
        ParticleScriptCompiler().parse(
            source, source_name="CollisionDisabled.particle.py"
        )


def test_graph_parameters_have_stable_slots_and_default_only_hot_updates():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "wind",
                "particle.parameter.get",
                properties={"parameter": "wind"},
            ),
            GraphNodeRecord("accelerate", "particle.attribute.velocity"),
        ),
        links=(
            GraphLinkRecord(
                "stream",
                "root.update",
                "out",
                "accelerate",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "wind-value",
                "wind",
                "value",
                "accelerate",
                "value",
                PortKind.VALUE,
            ),
        ),
    )
    parameter = ParticleParameter(
        "wind",
        "Wind",
        TypeRef(ValueType.VEC3),
        [1.0, 0.0, 0.0],
    )
    asset = ParticleGraphAsset(
        parameters=(parameter,),
        emitters=(ParticleEmitterAsset(update=update),),
    )

    program = ParticleGraphCompiler().compile(asset)
    kernel = ParticleKernelLowerer().lower(program)
    changed = ParticleGraphCompiler().compile(
        replace(asset, parameters=(replace(parameter, default=[3.0, 2.0, 1.0]),))
    )

    assert program.parameters[0].slot == 0
    assert program.parameters[0].stable_id == "wind"
    assert program.behavior_hash == changed.behavior_hash
    assert program.semantic_hash != changed.semantic_hash
    load = next(
        instruction
        for instruction in kernel.emitters[0].update.instructions
        if instruction.opcode == "load_parameter"
    )
    assert load.result_type == TypeRef(ValueType.VEC3)
    assert load.immediate_dict() == {"parameter": "wind"}
    assert kernel.to_dict() == type(kernel).from_dict(kernel.to_dict()).to_dict()


def test_particle_graph_rejects_removed_cpu_execution_target():
    document = ParticleGraphAsset().to_dict()
    settings = document["emitters"][0]["settings"]
    assert "target" not in settings

    settings["target"] = "cpu"
    with pytest.raises(ParticleGraphSchemaError, match=r"unknown=\['target'\]"):
        ParticleGraphAsset.from_dict(document)


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
                PortKind.EXEC,
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


def test_collision_and_event_nodes_preserve_authored_exec_order():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("collision.first", "particle.update.collide_scene"),
            GraphNodeRecord(
                "event.output",
                particle_event_output_type_id("route", "update"),
                properties={"condition": True},
            ),
            GraphNodeRecord(
                "size.after.collision",
                "particle.attribute.size",
                properties={"value": 2.0},
            ),
            GraphNodeRecord("collision.second", "particle.update.collide_scene"),
        ),
        links=(
            GraphLinkRecord(
                "first",
                "root.update",
                "out",
                "collision.first",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "event",
                "collision.first",
                "out",
                "event.output",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "second",
                "event.output",
                "out",
                "size.after.collision",
                "in",
                PortKind.EXEC,
            ),
            GraphLinkRecord(
                "third",
                "size.after.collision",
                "out",
                "collision.second",
                "in",
                PortKind.EXEC,
            ),
        ),
    )
    source = ParticleEmitterAsset(stable_id="source", update=update)
    target = ParticleEmitterAsset(stable_id="target")
    asset = ParticleGraphAsset(
        emitters=(source, target),
        event_types=(ParticleEventType("event", "Event", 16),),
        event_routes=(
            ParticleEventRoute("route", "event", "source", "update", "target"),
        ),
    )

    hir = ParticleGraphCompiler().compile(asset)
    assert [
        operation.opcode
        for operation in hir.emitters[0].update.flow.iter_operations()
    ] == [
        "collision.scene",
        "event.emit",
        "attribute.modify_size",
        "collision.scene",
    ]
    kernel = ParticleKernelLowerer().lower(hir)
    opcodes = [
        instruction.opcode
        for instruction in kernel.emitters[0].update.instructions
    ]
    first_collision = opcodes.index("collide_scene")
    event = opcodes.index("event_append")
    size_write = next(
        index
        for index, instruction in enumerate(kernel.emitters[0].update.instructions)
        if instruction.opcode == "store_attribute"
        and instruction.immediate_dict()["attribute"] == "builtin.size"
        and instruction.source.node_uid == "size.after.collision"
    )
    second_collision = opcodes.index("collide_scene", first_collision + 1)
    assert first_collision < event < size_write < second_collision


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
        "collision-field",
    ]
    assert hir.emitters[0].data_interfaces[0].boundary.value == "repeat"

    with pytest.raises(ParticleGraphSchemaError, match="stable ids must be unique"):
        ParticleEmitterAsset(
            data_interfaces=(
                VectorField(stable_id="duplicate"),
                SdfVolume(stable_id="duplicate"),
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
    assert _operations(smoke.init)[0].opcode == "emitter.sample_shape"
    assert _operations(smoke.update)[0].opcode == "attribute.modify_velocity"
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
                PortKind.EXEC,
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
                    PortKind.EXEC,
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


def test_particle_sprite_output_compiles_alignment_and_rejects_invalid_axis():
    def compile_alignment(alignment, axis=(0.0, 1.0, 0.0)):
        rendering = GraphDocument(
            "particle.rendering",
            nodes=(
                GraphNodeRecord("root.rendering", "particle.root.rendering"),
                GraphNodeRecord(
                    "output.sprite",
                    "particle.output.sprite",
                    properties={"alignment": alignment, "alignment_axis": list(axis)},
                ),
            ),
            links=(
                GraphLinkRecord(
                    "root-to-sprite",
                    "root.rendering",
                    "out",
                    "output.sprite",
                    "in",
                    PortKind.EXEC,
                ),
            ),
        )
        return ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(rendering=rendering),))
        )

    output = compile_alignment("axis", (0.0, 0.0, 1.0)).emitters[0].render_plan.outputs[0]
    assert output.sprite_alignment == "axis"
    assert output.alignment_axis == (0.0, 0.0, 1.0)

    with pytest.raises(ParticleCompileError, match="alignment must be"):
        compile_alignment("screen")
    with pytest.raises(ParticleCompileError, match="alignment axis"):
        compile_alignment("axis", (0.0, 0.0, 0.0))


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
                PortKind.EXEC,
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
                PortKind.EXEC,
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
                PortKind.EXEC,
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
                PortKind.EXEC,
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


def test_particle_graph_exec_order_lowers_to_stage_operations():
    init = GraphDocument(
        "particle.init",
        nodes=(
            GraphNodeRecord("root.init", "particle.root.init"),
            GraphNodeRecord(
                "velocity",
                "particle.attribute.velocity",
                properties={"value": [0.0, 2.0, 0.0]},
            ),
            GraphNodeRecord(
                "lifetime",
                "particle.attribute.lifetime",
                properties={"value": 3.0},
            ),
        ),
        links=(
            GraphLinkRecord("l1", "root.init", "out", "velocity", "in", PortKind.EXEC),
            GraphLinkRecord("l2", "velocity", "out", "lifetime", "in", PortKind.EXEC),
        ),
    )
    emitter = ParticleEmitterAsset(init=init)
    hir = ParticleGraphCompiler().compile(ParticleGraphAsset(emitters=(emitter,))).emitters[0]

    assert [operation.opcode for operation in _operations(hir.init)] == [
        "emitter.sample_shape",
        "attribute.modify_velocity",
        "attribute.modify_lifetime",
    ]


def test_particle_stage_value_links_use_common_typed_expression_ir():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("gravity", "particle.attribute.velocity"),
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
            GraphLinkRecord("s1", "root.update", "out", "gravity", "in", PortKind.EXEC),
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
    assert _operations(hir.update)[-1].value_bindings == (("value", "normalize.result"),)


def test_particle_update_can_author_color_and_size_over_lifetime():
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord("set-color", "particle.attribute.color"),
            GraphNodeRecord("set-size", "particle.attribute.size"),
            GraphNodeRecord(
                "age",
                "particle.attribute.get",
                properties={"attribute": "builtin.age"},
            ),
            GraphNodeRecord(
                "lifetime",
                "particle.attribute.get",
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
            GraphLinkRecord("stream-color", "root.update", "out", "set-color", "in", PortKind.EXEC),
            GraphLinkRecord("stream-size", "set-color", "out", "set-size", "in", PortKind.EXEC),
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

    assert [operation.opcode for operation in _operations(hir.update)[-2:]] == [
        "attribute.modify_color",
        "attribute.modify_size",
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
                GraphNodeRecord(f"{prefix}.gravity", "particle.attribute.velocity"),
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
                    PortKind.EXEC,
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

    obsolete = copy.deepcopy(asset.to_dict())
    del obsolete["emitters"][0]["settings"]["duration"]
    with pytest.raises(ParticleGraphSchemaError, match="keys mismatch"):
        ParticleGraphAsset.from_dict(obsolete)

    obsolete_lifecycle = copy.deepcopy(asset.to_dict())
    del obsolete_lifecycle["emitters"][0]["enabled"]
    with pytest.raises(ParticleGraphSchemaError, match="keys mismatch"):
        ParticleGraphAsset.from_dict(obsolete_lifecycle)


def test_particle_emitter_lifecycle_is_top_level_behavior_metadata():
    source = ParticleGraphAsset(
        emitters=(
            ParticleEmitterAsset(
                stable_id="manual",
                enabled=True,
                play_on_start=False,
            ),
        )
    )
    restored = ParticleGraphAsset.from_json(source.canonical_json())
    hir = ParticleGraphCompiler().compile(restored)
    metadata = decode_particle_runtime_metadata(hir)

    assert restored.emitters[0].settings.to_dict().keys() == EmitterSettings().to_dict().keys()
    assert hir.emitters[0].enabled is True
    assert hir.emitters[0].play_on_start is False
    assert metadata.emitters[0].enabled is True
    assert metadata.emitters[0].play_on_start is False
    assert metadata.emitters[0].name == "Emitter"
    assert source.semantic_hash() != replace(
        source,
        emitters=(replace(source.emitters[0], enabled=False),),
    ).semantic_hash()


def test_particle_python_construction_cannot_bypass_schema_invariants():
    with pytest.raises(ParticleGraphSchemaError, match="exactly 3"):
        ParticleAttribute("custom.wind", "wind", TypeRef(ValueType.VEC3), [1.0, 2.0])
    with pytest.raises(ParticleGraphSchemaError, match="bursts"):
        EmitterSettings(bursts=(object(),))
    with pytest.raises(ParticleGraphSchemaError, match="within the emitter duration"):
        EmitterSettings(
            duration=1.0,
            bursts=(ParticleBurst(0.75, 1, cycles=2, interval=0.5),),
        )
    with pytest.raises(ParticleGraphSchemaError, match="emitters are invalid"):
        ParticleGraphAsset(emitters=(object(),))


def test_particle_material_reference_uses_strict_guid_and_path_hint_shape():
    value = ParticleGraphAsset().to_dict()
    material = value["emitters"][0]["stages"]["rendering"]["nodes"][1]["properties"]
    material["material"] = "ambiguous-material"

    restored = ParticleGraphAsset.from_dict(value)
    with pytest.raises(ParticleCompileError, match="guid and path_hint"):
        ParticleGraphCompiler().compile(restored)


def test_mesh_emitter_shape_requires_an_asset_at_aot_compile_time():
    emitter = ParticleEmitterAsset(
        settings=EmitterSettings(shape=EmitterShape(kind="mesh"))
    )

    hir = ParticleGraphCompiler().compile(ParticleGraphAsset(emitters=(emitter,)))
    with pytest.raises(KernelCompileError, match="mesh shape requires a mesh asset"):
        ParticleKernelLowerer().lower(hir)


def test_particle_script_mesh_shape_matches_graph_asset_contract():
    source = '''\
from Infernux.particle import AssetReference, EmitterShape, ParticleScript, ParticleEmitter, EmitterSettings

class MeshShapeGraph(ParticleScript):
    class SurfaceEmitter(ParticleEmitter):
        stable_id = "surface-emitter"
        settings = EmitterSettings(
            shape=EmitterShape(
                kind="mesh",
                mesh=AssetReference(path_hint="Assets/Models/source.fbx"),
                mesh_mode="surface",
            ),
        )

        def init(self, ctx, particles):
            pass

        def update(self, ctx, particles):
            pass

        def rendering(self, ctx, particles):
            particles.sprite()
'''

    asset = ParticleScriptCompiler().parse(
        source, source_name="MeshShape.particle.py"
    )

    shape = asset.emitters[0].settings.shape
    assert shape.kind.value == "mesh"
    assert shape.mesh == AssetReference(path_hint="Assets/Models/source.fbx")
    assert shape.mesh_mode is MeshEmissionMode.SURFACE


PARTICLE_SCRIPT_SOURCE = '''\
from Infernux.particle import AssetReference, ParticleScript, ParticleEmitter, EmitterSettings, VectorField

class SmokeGraph(ParticleScript):
    stable_id = "smoke-graph"

    class Smoke(ParticleEmitter):
        stable_id = "smoke"
        enabled = True
        play_on_start = False
        settings = EmitterSettings(
            capacity=100000,
            simulation_space="world",
            spawn_rate=20000.0,
            spawn_rate_over_distance=3.0,
            duration=4.0,
            loop=False,
            start_delay=0.25,
        )
        data_interfaces = (
            VectorField(
                stable_id="wind-field",
                name="Wind",
                texture=AssetReference(path_hint="Assets/Fields/Wind.vectorfield"),
                space="world",
                boundary="repeat",
            ),
        )

        def init(self, ctx, particles):
            particles.set_velocity((0.0, 1.0, 0.0))
            particles.set_lifetime(6.0)
            particles.set_rotation(0.25)

        def update(self, ctx, particles):
            particles .add_velocity((0.0, -0.2, 0.0))
            particles .add_rotation(180.0)

        def rendering(self, ctx, particles):
            particles.set_lifetime(8.0)
            particles.set_flipbook_frame(particles.normalized_age * 63.0)
            particles.sprite(
                material=AssetReference(guid="six-way-smoke-guid"),
                receive_scene_lighting=True,
                receive_shadows=True,
                sort="back_to_front",
                alignment="velocity",
            )
'''


def test_particle_script_compiles_without_execution_to_same_hir_contract():
    compiler = ParticleScriptCompiler()
    asset = compiler.parse(PARTICLE_SCRIPT_SOURCE, source_name="Smoke.particle.py")
    program = compiler.compile(PARTICLE_SCRIPT_SOURCE, source_name="Smoke.particle.py")
    emitter = program.emitters[0]

    assert asset.stable_id == "smoke-graph"
    assert program.schedule.emitter_ids == ("smoke",)
    assert emitter.settings.spawn_rate_over_distance == 3.0
    assert emitter.settings.duration == 4.0
    assert emitter.settings.loop is False
    assert emitter.settings.start_delay == 0.25
    assert emitter.enabled is True
    assert emitter.play_on_start is False
    assert [operation.opcode for operation in _operations(emitter.init)] == [
        "emitter.sample_shape",
        "attribute.modify_velocity",
        "attribute.modify_lifetime",
        "attribute.modify_rotation",
    ]
    assert [operation.opcode for operation in _operations(emitter.update)[-2:]] == [
        "attribute.modify_velocity",
        "attribute.modify_rotation",
    ]
    assert [operation.opcode for operation in _operations(emitter.rendering)[:2]] == [
        "attribute.modify_lifetime",
        "attribute.modify_flipbook_frame",
    ]
    assert any(
        instruction.opcode == "normalized_age"
        for instruction in emitter.rendering.expressions.instructions
    )
    assert emitter.render_plan.outputs[0].receive_scene_lighting is True
    assert emitter.render_plan.outputs[0].receive_shadows is True
    assert emitter.render_plan.outputs[0].sprite_alignment == "velocity"
    assert [interface.stable_id for interface in emitter.data_interfaces] == [
        "wind-field",
    ]
    assert program.behavior_hash == ParticleGraphCompiler().compile(asset).behavior_hash


def test_particle_script_wait_and_until_share_the_graph_continuation_contract():
    source = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class TimedMotion(ParticleScript):
    stable_id = "timed-motion"

    class Emitter(ParticleEmitter):
        stable_id = "emitter"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            particles.set_lifetime(8.0)
            ctx.wait_seconds(0.25)
            particles.set_size(2.0)
            ctx.wait_frames(2)

        def update(self, ctx, particles):
            particles.add_velocity((0.0, -1.0, 0.0))
            ctx.until_seconds(3.0)
            particles.add_velocity((1.0, 1.0, 0.0))
            ctx.until_frames(5)

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    compiler = ParticleScriptCompiler()
    asset = compiler.parse(source, source_name="TimedMotion.particle.py")
    emitter_asset = asset.emitters[0]

    assert [node.type_id for node in emitter_asset.init.nodes[1:]] == [
        "particle.attribute.lifetime",
        "particle.control.wait_seconds",
        "particle.attribute.size",
        "particle.control.wait_frames",
    ]
    assert [node.type_id for node in emitter_asset.update.nodes[1:]] == [
        "particle.attribute.velocity",
        "particle.control.until_seconds",
        "particle.attribute.velocity",
        "particle.control.until_frames",
    ]
    assert emitter_asset.update.nodes[1].properties["composition"] == "add"
    assert emitter_asset.update.nodes[3].properties["composition"] == "add"

    program = compiler.compile(source, source_name="TimedMotion.particle.py")
    emitter = program.emitters[0]
    assert [item.kind.value for item in emitter.init.flow.suspensions] == [
        "seconds",
        "frames",
    ]
    assert [item.resume_node_uid for item in emitter.init.flow.suspensions] == [
        "init.2.set_size",
        "",
    ]
    assert [item.kind.value for item in emitter.update.flow.suspensions] == [
        "until_seconds",
        "until_frames",
    ]
    assert [item.resume_node_uid for item in emitter.update.flow.suspensions] == [
        "update.0.add_velocity",
        "update.2.add_velocity",
    ]


def test_particle_script_exposes_delta_time_as_a_pure_common_graph_value():
    source = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class Gravity(ParticleScript):
    class Emitter(ParticleEmitter):
        stable_id = "emitter"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            particles.set_lifetime(8.0)

        def update(self, ctx, particles):
            particles.add_velocity((0.0, -9.8, 0.0) * ctx.delta_time)

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    compiler = ParticleScriptCompiler()
    asset = compiler.parse(source, source_name="Gravity.particle.py")
    update = asset.emitters[0].update
    assert "particle.context.delta_time" in {
        node.type_id for node in update.nodes
    }
    assert "common.constant.vec3" in {node.type_id for node in update.nodes}
    assert "common.math.multiply" in {node.type_id for node in update.nodes}

    kernel = ParticleKernelLowerer().lower(
        compiler.compile(source, source_name="Gravity.particle.py")
    ).emitters[0].update
    assert any(
        instruction.opcode == "load_uniform"
        and instruction.source.node_uid.endswith(".delta_time")
        for instruction in kernel.instructions
    )


def test_particle_script_if_else_expands_continuations_into_mutually_exclusive_lanes():
    source = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class ConditionalMotion(ParticleScript):
    class Emitter(ParticleEmitter):
        stable_id = "emitter"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            particles.set_lifetime(8.0)

        def update(self, ctx, particles):
            if particles.age < 1.0:
                particles.add_velocity((0.0, 1.0, 0.0))
                ctx.wait_frames(2)
            else:
                particles.multiply_size(0.5)
            particles.add_color((0.1, 0.2, 0.3, 0.0))

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    compiler = ParticleScriptCompiler()
    asset = compiler.parse(source, source_name="ConditionalMotion.particle.py")
    update = asset.emitters[0].update

    assert [node.type_id for node in update.nodes[1:]] == [
        "particle.attribute.get",
        "common.constant.f32",
        "common.compare.less_than",
        "particle.control.if",
        "particle.attribute.velocity",
        "particle.control.wait_frames",
        "particle.attribute.color",
        "particle.attribute.size",
        "particle.attribute.color",
    ]
    assert sum(node.type_id == "particle.attribute.color" for node in update.nodes) == 2

    emitter = compiler.compile(
        source, source_name="ConditionalMotion.particle.py"
    ).emitters[0]
    operations = tuple(emitter.update.flow.iter_operations())
    colors = [
        operation
        for operation in operations
        if operation.opcode == "attribute.modify_color"
    ]
    assert len(colors) == 2
    assert {color.execution_predicates[-1].expected for color in colors} == {
        False,
        True,
    }
    assert {color.execution_predicates[-1].source_node_uid for color in colors} == {
        "update.0.if"
    }
    assert emitter.update.flow.suspensions[0].resume_node_uid == "update.3.add_color"


def test_particle_script_rejects_imperative_loops_in_stage_control_flow():
    source = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class InvalidLoop(ParticleScript):
    class Emitter(ParticleEmitter):
        stable_id = "emitter"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            pass

        def update(self, ctx, particles):
            while particles.age < 1.0:
                particles.add_size(0.1)

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    with pytest.raises(
        ParticleScriptError,
        match="stage bodies only allow particle operation calls and if/else",
    ):
        ParticleScriptCompiler().parse(
            source,
            source_name="InvalidLoop.particle.py",
        )


def test_particle_script_rejects_wait_and_until_in_rendering():
    source = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class InvalidRenderingWait(ParticleScript):
    class Emitter(ParticleEmitter):
        stable_id = "emitter"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            pass

        def update(self, ctx, particles):
            pass

        def rendering(self, ctx, particles):
            ctx.wait_seconds(1.0)
            particles.sprite()
'''
    with pytest.raises(ParticleScriptError, match="rendering cannot contain Wait/Until"):
        ParticleScriptCompiler().parse(
            source,
            source_name="InvalidRenderingWait.particle.py",
        )


def test_particle_script_parameters_share_graph_hir_and_gpu_abi():
    source = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings, Parameter

class ParameterGraph(ParticleScript):
    stable_id = "parameter-graph"
    parameters = (
        Parameter(
            "wind-id",
            "Wind",
            "vec3",
            (1.0, 2.0, 3.0),
            category="Motion",
            tooltip="World-space wind velocity",
        ),
    )

    class Smoke(ParticleEmitter):
        stable_id = "smoke"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            particles.set_velocity(ctx.parameter("Wind"))

        def update(self, ctx, particles):
            particles .add_velocity(ctx.parameter("wind-id"))

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    compiler = ParticleScriptCompiler()
    asset = compiler.parse(source, source_name="Parameters.particle.py")
    program = compiler.compile(source, source_name="Parameters.particle.py")
    graph_program = ParticleGraphCompiler().compile(asset)
    kernel = ParticleKernelLowerer().lower(program)

    assert asset.parameters == (
        ParticleParameter(
            "wind-id",
            "Wind",
            TypeRef(ValueType.VEC3),
            [1.0, 2.0, 3.0],
            category="Motion",
            tooltip="World-space wind velocity",
        ),
    )
    assert program.behavior_hash == graph_program.behavior_hash
    assert program.parameters[0].stable_id == "wind-id"
    assert program.parameters[0].slot == 0
    assert kernel.parameters[0].stable_id == "wind-id"
    assert any(
        instruction.opcode == "load_parameter"
        and instruction.immediate_dict()["parameter"] == "wind-id"
        for stage in (program.emitters[0].init, program.emitters[0].update)
        for instruction in stage.expressions.instructions
    )


def test_particle_script_rejects_unknown_parameter_reads():
    source = '''\
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class InvalidParameterGraph(ParticleScript):
    class Smoke(ParticleEmitter):
        stable_id = "smoke"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            particles.set_velocity(ctx.parameter("missing"))

        def update(self, ctx, particles):
            pass

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    with pytest.raises(ParticleScriptError, match="unknown particle parameter 'missing'"):
        ParticleScriptCompiler().parse(source, source_name="InvalidParameter.particle.py")


def test_particle_script_texture2d_sample_shares_graph_hir_and_gpu_resource_abi():
    source = '''\
from Infernux.particle import AssetReference, ParticleScript, ParticleEmitter, EmitterSettings, Parameter

class TextureParameterGraph(ParticleScript):
    stable_id = "texture-parameter-graph"
    parameters = (
        Parameter(
            "texture-id",
            "Particle Texture",
            "texture2d",
            AssetReference(path_hint="Assets/Smoke.tga"),
        ),
        Parameter("uv-id", "Sample UV", "vec2", (0.5, 0.5)),
    )

    class Smoke(ParticleEmitter):
        stable_id = "smoke"
        settings = EmitterSettings()

        def init(self, ctx, particles):
            particles.set_color(ctx.sample_texture2d(
                ctx.parameter("Particle Texture"),
                ctx.parameter("Sample UV"),
            ))

        def update(self, ctx, particles):
            pass

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    compiler = ParticleScriptCompiler()
    asset = compiler.parse(source, source_name="TextureParameter.particle.py")
    program = compiler.compile(source, source_name="TextureParameter.particle.py")
    kernel = ParticleKernelLowerer().lower(program)

    assert asset.parameters[0].value_type == TypeRef(ValueType.TEXTURE2D)
    assert asset.parameters[0].default == {
        "guid": "",
        "path_hint": "Assets/Smoke.tga",
    }
    sample = next(
        instruction
        for instruction in kernel.emitters[0].init.instructions
        if instruction.opcode == "sample_texture2d"
    )
    assert len(sample.operands) == 2
    assert sample.result_type == TypeRef(ValueType.COLOR)


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
                alignment="velocity",
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
        "particles .add_rotation(180.0)",
        "particles .add_orientation((90.0, 180.0, 270.0))",
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
    assert _operations(emitter.init)[-2].opcode == "attribute.modify_orientation"
    assert _operations(emitter.init)[-1].opcode == "attribute.modify_scale"
    assert _operations(emitter.update)[-1].opcode == "attribute.modify_orientation"
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
    assert [operation.opcode for operation in _operations(emitter.init)[-3:]] == [
        "attribute.modify_strip_id",
        "attribute.modify_ribbon_order",
        "attribute.modify_ribbon_break",
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
            particles .add_velocity((0.0, -9.81, 0.0))
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

    assert _operations(emitter.update)[-1].opcode == "collision.plane"
    assert _operations(emitter.update)[-1].parameter_dict() == {
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
                "stream", "root.update", "out", "collision", "in", PortKind.EXEC
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

    assert _operations(emitter.update)[-1].opcode == "collision.sphere"
    assert _operations(emitter.update)[-1].parameter_dict() == {
        "center": [1.0, 2.0, 3.0],
        "friction": 0.25,
        "particle_radius": 0.1,
        "restitution": 0.7,
        "sphere_radius": 2.0,
    }
    assert emitter == ParticleGraphCompiler().compile(asset).emitters[0]


def test_scene_collision_graph_and_script_share_gpu_scene_contract():
    source = '''
from Infernux.particle import ParticleScript, ParticleEmitter, EmitterSettings

class CollisionGraph(ParticleScript):
    class Sparks(ParticleEmitter):
        stable_id = "sparks"
        settings = EmitterSettings(capacity=1024)

        def init(self, ctx, particles):
            particles.set_velocity((0.0, 0.0, 0.0))

        def update(self, ctx, particles):
            particles.collide_scene(
                particle_radius=0.15,
                layer_mask=5,
                include_triggers=True,
                restitution_scale=0.8,
                friction_scale=0.6,
            )

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    compiler = ParticleScriptCompiler()
    asset = compiler.parse(source, source_name="SceneCollision.particle.py")
    emitter = compiler.compile(
        source, source_name="SceneCollision.particle.py"
    ).emitters[0]

    assert _operations(emitter.update)[-1].opcode == "collision.scene"
    assert _operations(emitter.update)[-1].parameter_dict() == {
        "friction_scale": 0.6,
        "include_triggers": True,
        "layer_mask": 5,
        "particle_radius": 0.15,
        "restitution_scale": 0.8,
    }
    assert emitter == ParticleGraphCompiler().compile(asset).emitters[0]


def test_particle_script_scene_collision_events_read_current_hit_and_normal():
    source = '''
from Infernux.particle import (
    ParticleScript, ParticleEmitter, EmitterSettings,
    EventField, EventType, EventRoute,
)

class CollisionEvents(ParticleScript):
    event_types = (
        EventType(
            stable_id="impact",
            name="Impact",
            capacity_per_step=64,
            fields=(
                EventField(
                    "normal", "Normal", "vec3", (0.0, 1.0, 0.0),
                    space="simulation",
                ),
            ),
        ),
    )
    event_routes = (
        EventRoute(
            stable_id="impact-route",
            event_type_id="impact",
            source_emitter_id="source",
            source_stage="update",
            target_emitter_id="target",
        ),
    )

    class Source(ParticleEmitter):
        stable_id = "source"
        settings = EmitterSettings(capacity=1024)

        def init(self, ctx, particles):
            pass

        def update(self, ctx, particles):
            particles.collide_scene()
            particles.emit_event(
                route="impact-route",
                condition=particles.collision_hit,
                payload={"normal": particles.collision_normal},
            )

        def rendering(self, ctx, particles):
            particles.sprite()

    class Target(ParticleEmitter):
        stable_id = "target"
        settings = EmitterSettings(capacity=1024)

        def init(self, ctx, particles):
            pass

        def update(self, ctx, particles):
            pass

        def rendering(self, ctx, particles):
            particles.sprite()
'''
    program = ParticleScriptCompiler().compile(
        source, source_name="CollisionEvents.particle.py"
    )
    assert [
        operation.opcode
        for operation in program.emitters[0].update.flow.iter_operations()
    ] == [
        "collision.scene",
        "event.emit",
    ]

    kernel = ParticleKernelLowerer().lower(program)
    instructions = kernel.emitters[0].update.instructions
    collision_index = next(
        index for index, instruction in enumerate(instructions)
        if instruction.opcode == "collide_scene"
    )
    event_index = next(
        index for index, instruction in enumerate(instructions)
        if instruction.opcode == "event_append"
    )
    collision = instructions[collision_index]
    assert collision.immediate_dict()["hit_attribute"] == "builtin.collision_hit"
    assert collision.immediate_dict()["normal_attribute"] == "builtin.collision_normal"
    assert any(
        collision_index < index < event_index
        and instruction.opcode == "load_attribute"
        and instruction.immediate_dict()["attribute"] == "builtin.collision_hit"
        for index, instruction in enumerate(instructions)
    )
    assert any(
        collision_index < index < event_index
        and instruction.opcode == "load_attribute"
        and instruction.immediate_dict()["attribute"] == "builtin.collision_normal"
        for index, instruction in enumerate(instructions)
    )


@pytest.mark.parametrize(
    "properties, message",
    [
        ({"particle_radius": -0.1}, "particle_radius must be non-negative"),
        ({"layer_mask": -1}, "layer_mask must be non-negative"),
        ({"include_triggers": 1}, "include_triggers must be a bool"),
        ({"restitution_scale": -0.1}, "restitution_scale must be finite and non-negative"),
        ({"friction_scale": -0.1}, "friction_scale must be finite and non-negative"),
    ],
)
def test_scene_collision_rejects_invalid_static_parameters(properties, message):
    update = GraphDocument(
        "particle.update",
        nodes=(
            GraphNodeRecord("root.update", "particle.root.update"),
            GraphNodeRecord(
                "collision",
                "particle.update.collide_scene",
                properties=properties,
            ),
        ),
        links=(
            GraphLinkRecord(
                "stream", "root.update", "out", "collision", "in", PortKind.EXEC
            ),
        ),
    )
    with pytest.raises(ParticleCompileError, match=message):
        ParticleGraphCompiler().compile(
            ParticleGraphAsset(emitters=(ParticleEmitterAsset(update=update),))
        )


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

    assert _operations(emitter.update)[-1].opcode == "collision.sdf"
    assert _operations(emitter.update)[-1].parameter_dict() == {
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
                "stream", "root.update", "out", "collision", "in", PortKind.EXEC
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
                "render-stream", "root.rendering", "out", "ribbon", "in", PortKind.EXEC
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
        "particles .add_velocity((0.0, -0.2, 0.0))",
        'particles .add_velocity(ctx.sample_vector_field("wind-field", particles.position))',
    )
    asset = ParticleScriptCompiler().parse(source, source_name="Wind.particle.py")
    update = asset.emitters[0].update

    assert [node.type_id for node in update.nodes] == [
        "particle.root.update",
        "particle.attribute.get",
        "particle.vector_field.sample",
        "particle.attribute.velocity",
        "particle.attribute.rotation",
    ]
    assert any(
        link.source_node.endswith("sample_vector_field")
        and link.target_node.endswith("add_velocity")
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
        "from Infernux.particle import AssetReference, ParticleScript, ParticleEmitter, EmitterSettings, VectorField",
        "from Infernux.particle import AssetReference, ParticleScript, ParticleEmitter, EmitterSettings, VectorField, Curve, CurveKey, Gradient, GradientKey",
    ).replace(
        "particles .add_velocity((0.0, -0.2, 0.0))",
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
        "particles .add_velocity((0.0, -0.2, 0.0))",
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


def test_particle_script_boolean_primitives_lower_to_common_graph_ops():
    source = PARTICLE_SCRIPT_SOURCE.replace(
        "particles .add_velocity((0.0, -0.2, 0.0))",
        "particles.kill_if(not ((particles.age == 0.5) or (particles.lifetime != 1.0 and False)))",
    )

    asset = ParticleScriptCompiler().parse(source, source_name="Boolean.particle.py")
    update = asset.emitters[0].update
    kernel = ParticleKernelLowerer().lower(ParticleGraphCompiler().compile(asset))
    node_types = {node.type_id for node in update.nodes}
    opcodes = {instruction.opcode for instruction in kernel.emitters[0].update.instructions}

    assert {
        "common.constant.bool",
        "common.compare.equal",
        "common.compare.not_equal",
        "common.logic.and",
        "common.logic.or",
        "common.logic.not",
    } <= node_types
    assert {
        "equal",
        "not_equal",
        "logical_and",
        "logical_or",
        "logical_not",
    } <= opcodes


def test_particle_script_noise_compiles_to_the_shared_portable_kernel_ops():
    source = PARTICLE_SCRIPT_SOURCE.replace(
        "particles .add_velocity((0.0, -0.2, 0.0))",
        "particles .add_velocity(ctx.vector_noise_3d(particles.position, frequency=2.5, seed=17))",
    ).replace(
        "particles .add_rotation(180.0)",
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
        "particles .add_velocity((0.0, -0.2, 0.0))",
        f"particles .add_velocity({replacement})",
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
                "        def update(self, ctx, particles):\n            particles .add_velocity((0.0, -0.2, 0.0))\n            particles .add_rotation(180.0)\n\n",
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
    ] == ["wind-field"]
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
        "vertex",
        "fragment",
        "forward_plus_fragment",
        "picking_fragment",
        "motion_vertex",
        "motion_fragment",
    }
    assert set(graph_artifact.gpu_spirv["mesh"]) == {
        "vertex",
        "fragment",
        "forward_plus_fragment",
        "picking_fragment",
        "motion_vertex",
        "motion_fragment",
    }
    from Infernux.particle import decode_gpu_particle_spirv

    decoded = decode_gpu_particle_spirv(graph_artifact.gpu_spirv, 0)
    assert decoded["stable_id"] == "smoke"
    assert set(decoded["stages"]) == set(graph_artifact.gpu_glsl["emitters"][0]["stages"])
    assert set(decoded["billboard"]) == {
        "vertex",
        "fragment",
        "forward_plus_fragment",
        "picking_fragment",
        "motion_vertex",
        "motion_fragment",
    }
    assert set(decoded["mesh"]) == {
        "vertex",
        "fragment",
        "forward_plus_fragment",
        "picking_fragment",
        "motion_vertex",
        "motion_fragment",
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
    stale_hir["emitters"][0]["render_plan"][0].pop("sprite_alignment")
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
                GraphNodeRecord("invalid.acceleration", "particle.attribute.velocity"),
        ),
        update.links
        + (
            GraphLinkRecord(
                    "root-to-invalid-acceleration",
                    "root.update",
                    "out",
                    "invalid.acceleration",
                "in",
                PortKind.EXEC,
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


def test_particle_aot_rebuilds_persisted_artifact_with_stale_gpu_layout(
    tmp_path, monkeypatch
):
    project = tmp_path / "Project"
    source = project / "Assets" / "Smoke.particlegraph"
    source.parent.mkdir(parents=True)
    graph = ParticleGraphAsset(stable_id="stale-gpu-layout-smoke")
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
    layout = payload["gpu_glsl"]["emitters"][0]["data_interface_layout"]
    layout["obsolete_interface_table"] = []
    artifact_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    ParticleArtifactRegistry.clear()
    rebuilt = ParticleArtifactRegistry.compile_path(str(source))

    rebuilt_layout = rebuilt.gpu_glsl["emitters"][0]["data_interface_layout"]
    assert "obsolete_interface_table" not in rebuilt_layout
    persisted_layout = json.loads(artifact_path.read_text(encoding="utf-8"))["gpu_glsl"][
        "emitters"
    ][0]["data_interface_layout"]
    assert "obsolete_interface_table" not in persisted_layout
