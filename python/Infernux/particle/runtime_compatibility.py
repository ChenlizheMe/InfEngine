"""Compatibility classification for compile-then-publish particle reloads."""

from __future__ import annotations

import json
from enum import Enum

from .asset import EmitterSettings
from .kernel_ir import ParticleEmitterKernelIR


class ParticleRuntimeCompatibility(str, Enum):
    PARAMETER_ONLY = "parameter_only"
    KERNEL_COMPATIBLE = "kernel_compatible"
    LAYOUT_MIGRATABLE = "layout_migratable"
    EMITTER_RESTART = "emitter_restart"
    SYSTEM_RESTART_REQUIRED = "system_restart_required"


def classify_emitter_update(
    previous_kernel: ParticleEmitterKernelIR,
    next_kernel: ParticleEmitterKernelIR,
    previous_settings: EmitterSettings,
    next_settings: EmitterSettings,
) -> ParticleRuntimeCompatibility:
    """Classify one stable emitter update without depending on a backend."""
    if previous_kernel.stable_id != next_kernel.stable_id:
        return ParticleRuntimeCompatibility.SYSTEM_RESTART_REQUIRED
    if (
        previous_settings.simulation_space != next_settings.simulation_space
        or previous_settings.bursts != next_settings.bursts
        or previous_settings.duration != next_settings.duration
        or previous_settings.loop != next_settings.loop
        or previous_settings.start_delay != next_settings.start_delay
    ):
        return ParticleRuntimeCompatibility.EMITTER_RESTART

    if _event_runtime_abi(previous_kernel) != _event_runtime_abi(next_kernel):
        return ParticleRuntimeCompatibility.EMITTER_RESTART

    previous_schema = {
        stable_id: value_type
        for stable_id, value_type, _default in previous_kernel.attributes
    }
    next_schema = {
        stable_id: value_type
        for stable_id, value_type, _default in next_kernel.attributes
    }
    for stable_id in previous_schema.keys() & next_schema.keys():
        if previous_schema[stable_id] != next_schema[stable_id]:
            return ParticleRuntimeCompatibility.EMITTER_RESTART
    if (
        previous_schema.keys() != next_schema.keys()
        or previous_settings.capacity != next_settings.capacity
    ):
        return ParticleRuntimeCompatibility.LAYOUT_MIGRATABLE

    previous_interfaces = _data_interface_abi(previous_kernel)
    next_interfaces = _data_interface_abi(next_kernel)
    if previous_interfaces != next_interfaces:
        return ParticleRuntimeCompatibility.KERNEL_COMPATIBLE

    previous_code = _kernel_code(previous_kernel)
    next_code = _kernel_code(next_kernel)
    if previous_code == next_code:
        return ParticleRuntimeCompatibility.PARAMETER_ONLY
    return ParticleRuntimeCompatibility.KERNEL_COMPATIBLE


def _kernel_code(kernel: ParticleEmitterKernelIR) -> tuple[object, ...]:
    return (
        kernel.random_seed,
        kernel.init.to_dict(include_source=False),
        kernel.update.to_dict(include_source=False),
        kernel.rendering.to_dict(include_source=False),
        tuple(flow.to_dict(include_source=False) for flow in kernel.flows),
        tuple(
            suspension.to_dict(include_source=False)
            for suspension in kernel.suspensions
        ),
    )


def _event_runtime_abi(kernel: ParticleEmitterKernelIR) -> str:
    event_flows = tuple(
        flow.flow_id
        for flow in kernel.flows
        if flow.lifecycle_stage.value == "event"
    )
    event_attributes = tuple(
        (stable_id, value_type.to_dict())
        for stable_id, value_type, _default in kernel.attributes
        if stable_id.startswith("internal.event.")
    )
    event_instructions = []
    for instruction in kernel.update.instructions:
        if instruction.opcode not in {
            "event_begin",
            "event_complete",
            "event_enqueue",
            "event_payload",
        }:
            continue
        immediate = instruction.immediate_dict()
        immediate.pop("default", None)
        event_instructions.append(
            {
                "opcode": instruction.opcode,
                "result_type": (
                    instruction.result_type.to_dict()
                    if instruction.result_type is not None
                    else None
                ),
                "immediate": immediate,
            }
        )
    return json.dumps(
        {
            "flows": event_flows,
            "attributes": event_attributes,
            "instructions": event_instructions,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _data_interface_abi(
    kernel: ParticleEmitterKernelIR,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (interface.stable_id, interface.kind)
        for interface in kernel.data_interfaces
    )


__all__ = ["ParticleRuntimeCompatibility", "classify_emitter_update"]
