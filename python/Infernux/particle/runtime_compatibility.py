"""Compatibility classification for compile-then-publish particle reloads."""

from __future__ import annotations

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
    ):
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


def _kernel_code(kernel: ParticleEmitterKernelIR) -> tuple[dict, dict, dict]:
    return (
        kernel.init.to_dict(include_source=False),
        kernel.update.to_dict(include_source=False),
        kernel.rendering.to_dict(include_source=False),
    )


def _data_interface_abi(
    kernel: ParticleEmitterKernelIR,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (interface.stable_id, interface.kind)
        for interface in kernel.data_interfaces
    )


__all__ = ["ParticleRuntimeCompatibility", "classify_emitter_update"]
