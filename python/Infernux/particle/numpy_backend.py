"""NumPy AOT CPU backend for validated particle Kernel SSA programs."""

from __future__ import annotations

from dataclasses import dataclass
import math
import threading
from typing import Any, Collection, Mapping

import numpy as np

from Infernux.graph.types import CoordinateSpace, TypeRef, ValueType

from .asset import EmitterSettings, ExecutionTarget
from .hir import ParticleOutputDescriptor, ParticleProgramHIR
from .kernel_ir import (
    KernelCompileError,
    ParticleEmitterKernelIR,
    ParticleKernelFunction,
    ParticleKernelProgram,
)
from .runtime_metadata import (
    ParticleRuntimeMetadataError,
    decode_particle_runtime_metadata,
)


PARTICLE_INSTANCE_FLOATS = 9


class NumpyParticleBackendError(RuntimeError):
    pass


class _RuntimeContext:
    def __init__(self, system_seed: int) -> None:
        if type(system_seed) is not int or not 0 <= system_seed <= 0xFFFFFFFF:
            raise NumpyParticleBackendError("system_seed must be an unsigned 32-bit integer")
        self.system_seed = system_seed
        self.delta_time = 0.0
        self.simulation_step = 0
        self._conversions: dict[tuple[CoordinateSpace, CoordinateSpace], np.ndarray] = {}
        self.set_transforms(None, None)

    def set_transforms(self, emitter_to_world, simulation_to_world) -> None:
        identity = np.eye(4, dtype=np.float32)
        emitter = _matrix4(emitter_to_world, identity)
        simulation = _matrix4(simulation_to_world, identity)
        space_to_world = {
            CoordinateSpace.NONE: identity,
            CoordinateSpace.EMITTER_LOCAL: emitter,
            CoordinateSpace.SIMULATION: simulation,
            CoordinateSpace.WORLD: identity,
        }
        conversions = {}
        for source_space, source_to_world in space_to_world.items():
            for target_space, target_to_world in space_to_world.items():
                if source_space is CoordinateSpace.NONE or target_space is CoordinateSpace.NONE:
                    conversions[(source_space, target_space)] = identity
                else:
                    conversions[(source_space, target_space)] = (
                        np.linalg.inv(target_to_world).astype(np.float32) @ source_to_world
                    )
        self._conversions = conversions

    def conversion(self, source: str, target: str) -> np.ndarray:
        source_space = CoordinateSpace(source)
        target_space = CoordinateSpace(target)
        try:
            return self._conversions[(source_space, target_space)]
        except KeyError as exc:
            raise NumpyParticleBackendError(
                f"NumPy backend cannot convert {source_space.value} to {target_space.value}"
            ) from exc


@dataclass(frozen=True)
class _StageLayout:
    scratch_types: tuple[TypeRef, ...]
    export_types: tuple[tuple[str, TypeRef], ...]
    export_aliases: tuple[tuple[str, str], ...]


class _StageWorkspace:
    def __init__(self, layout: _StageLayout, capacity: int) -> None:
        self.scratch = tuple(_allocate_array(capacity, value_type) for value_type in layout.scratch_types)
        self.exports = {
            stable_id: _allocate_array(capacity, value_type)
            for stable_id, value_type in layout.export_types
        }
        self.random_u32_a = np.empty(capacity, dtype=np.uint32)
        self.random_u32_b = np.empty(capacity, dtype=np.uint32)
        self.random_float = tuple(
            np.empty(capacity, dtype=np.float32) for _index in range(6)
        )


@dataclass(frozen=True)
class NumpyStageExecutable:
    stage: str
    source: str
    layout: _StageLayout
    function: Any

    def create_workspace(self, capacity: int) -> _StageWorkspace:
        return _StageWorkspace(self.layout, capacity)


@dataclass(frozen=True)
class NumpyParticleEmitterProgram:
    stable_id: str
    settings: EmitterSettings
    kernel: ParticleEmitterKernelIR
    init: NumpyStageExecutable
    update: NumpyStageExecutable
    rendering: NumpyStageExecutable
    outputs: tuple[ParticleOutputDescriptor, ...]

    def create_runtime(self, *, system_seed: int = 0) -> "NumpyParticleEmitterRuntime":
        return NumpyParticleEmitterRuntime(self, system_seed=system_seed)


@dataclass(frozen=True)
class NumpyParticleProgram:
    kernel_hash: str
    emitters: tuple[NumpyParticleEmitterProgram, ...]

    def create_runtime(
        self,
        emitter_index: int = 0,
        *,
        system_seed: int = 0,
    ) -> "NumpyParticleEmitterRuntime":
        if type(emitter_index) is not int or not 0 <= emitter_index < len(self.emitters):
            raise IndexError("particle emitter index is out of range")
        return self.emitters[emitter_index].create_runtime(system_seed=system_seed)


class NumpyParticleCompiler:
    """Compile validated Kernel SSA once into branch-free stage call sequences."""

    def compile(
        self,
        hir: ParticleProgramHIR | Mapping[str, Any],
        kernel: ParticleKernelProgram,
        *,
        emitter_ids: Collection[str] | None = None,
    ) -> NumpyParticleProgram:
        try:
            metadata = decode_particle_runtime_metadata(hir)
        except ParticleRuntimeMetadataError as exc:
            raise NumpyParticleBackendError(str(exc)) from exc
        if metadata.behavior_hash != kernel.source_behavior_hash:
            raise NumpyParticleBackendError("Particle HIR and Kernel IR behavior hashes differ")
        kernel_ids = tuple(emitter.stable_id for emitter in kernel.emitters)
        if metadata.schedule != kernel_ids:
            raise NumpyParticleBackendError("Particle HIR and Kernel IR emitter schedules differ")
        selected_ids = None
        if emitter_ids is not None:
            selected_ids = frozenset(emitter_ids)
            if any(type(stable_id) is not str or not stable_id for stable_id in selected_ids):
                raise NumpyParticleBackendError(
                    "selected particle emitter ids must be non-empty strings"
                )
            unknown = selected_ids.difference(kernel_ids)
            if unknown:
                raise NumpyParticleBackendError(
                    f"selected particle emitter ids are unknown: {sorted(unknown)}"
                )
        emitter_inputs = {emitter.stable_id: emitter for emitter in metadata.emitters}
        programs = []
        for emitter in kernel.emitters:
            if selected_ids is not None and emitter.stable_id not in selected_ids:
                continue
            runtime_metadata = emitter_inputs[emitter.stable_id]
            settings = runtime_metadata.settings
            outputs = runtime_metadata.outputs
            if settings.target is ExecutionTarget.GPU:
                raise NumpyParticleBackendError(
                    f"emitter {emitter.stable_id!r} explicitly requires the GPU backend"
                )
            programs.append(
                NumpyParticleEmitterProgram(
                    emitter.stable_id,
                    settings,
                    emitter,
                    _compile_stage(emitter.init, emitter.random_seed),
                    _compile_stage(emitter.update, emitter.random_seed),
                    _compile_stage(emitter.rendering, emitter.random_seed),
                    outputs,
                )
            )
        return NumpyParticleProgram(kernel.kernel_hash, tuple(programs))


class NumpyParticleEmitterRuntime:
    """Dense SoA runtime with per-instance storage and explicit thread ownership."""

    def __init__(self, program: NumpyParticleEmitterProgram, *, system_seed: int = 0) -> None:
        self.program = program
        self.settings = program.settings
        self.capacity = self.settings.capacity
        self.attributes = {
            stable_id: _allocate_array(self.capacity, value_type)
            for stable_id, value_type, _default in program.kernel.attributes
        }
        self._attribute_defaults = {
            stable_id: _constant_value(default, value_type)
            for stable_id, value_type, default in program.kernel.attributes
        }
        self.alive = np.zeros(self.capacity, dtype=np.bool_)
        self.spawn_generation = np.zeros(self.capacity, dtype=np.uint32)
        self._ordinal = np.arange(self.capacity, dtype=np.uint32)
        self._finite_mask = np.empty(self.capacity, dtype=np.bool_)
        max_components = max(
            (_component_count(value_type) for _stable_id, value_type, _default in program.kernel.attributes),
            default=1,
        )
        self._component_mask = np.empty((self.capacity, max_components), dtype=np.bool_)
        self._instance_buffer = np.empty(
            (self.capacity, PARTICLE_INSTANCE_FLOATS), dtype=np.float32
        )
        self._workspaces = {
            "init": program.init.create_workspace(self.capacity),
            "update": program.update.create_workspace(self.capacity),
            "rendering": program.rendering.create_workspace(self.capacity),
        }
        self._render_aliases = dict(program.rendering.layout.export_aliases)
        self._context = _RuntimeContext(system_seed)
        self._active_count = 0
        self._spawn_accumulator = 0.0
        self._elapsed = 0.0
        self._burst_states: list[list[float | int]] = []
        self._next_particle_id = 0
        self._spawn_epoch = 0
        self._playing = True
        self._owner_thread: int | None = None
        self.reset()
        self._owner_thread = None

    @property
    def particle_count(self) -> int:
        return self._active_count

    @property
    def simulation_step(self) -> int:
        return self._context.simulation_step

    def play(self) -> None:
        self._claim_thread()
        self._playing = True

    def pause(self) -> None:
        self._claim_thread()
        self._playing = False

    def release_thread_ownership(self) -> None:
        if self._playing:
            raise NumpyParticleBackendError("pause the particle runtime before moving it to another thread")
        self._claim_thread()
        self._owner_thread = None

    def set_transforms(self, emitter_to_world=None, simulation_to_world=None) -> None:
        self._claim_thread()
        self._context.set_transforms(emitter_to_world, simulation_to_world)

    def reset(self) -> None:
        self._claim_thread()
        self.alive.fill(False)
        self._active_count = 0
        self._spawn_accumulator = 0.0
        self._elapsed = 0.0
        self._context.simulation_step = 0
        self._next_particle_id = 0
        self._spawn_epoch = 0
        self._burst_states = [
            [float(burst.time), int(burst.cycles), int(burst.count), float(burst.interval)]
            for burst in self.settings.bursts
        ]

    def tick(self, delta_time: float) -> np.ndarray:
        self._claim_thread()
        delta_time = float(delta_time)
        if not math.isfinite(delta_time) or delta_time < 0.0:
            raise ValueError("particle delta_time must be finite and non-negative")
        if not self._playing:
            return self.instance_buffer()

        self._context.delta_time = delta_time
        previous_elapsed = self._elapsed
        self._elapsed += delta_time
        spawn_count = self._scheduled_spawn_count(previous_elapsed, self._elapsed, delta_time)
        spawn_count = min(spawn_count, self.capacity - self._active_count)
        if spawn_count > 0:
            start = self._active_count
            self._initialize_range(start, spawn_count)
            self._active_count += spawn_count
            self._compact_dead()

        if self._active_count:
            workspace = self._workspaces["update"]
            self.program.update.function(
                self,
                workspace,
                self._context,
                0,
                self._active_count,
            )
            self._kill_non_finite(self.program.update, 0, self._active_count)
            self._compact_dead()

        instances = self.instance_buffer()
        self._context.simulation_step = (self._context.simulation_step + 1) & 0xFFFFFFFF
        return instances

    def instance_buffer(self) -> np.ndarray:
        count = self._active_count
        if count == 0:
            return self._instance_buffer[:0]
        workspace = self._workspaces["rendering"]
        self.program.rendering.function(self, workspace, self._context, 0, count)
        exports = workspace.exports
        position_id = self._render_aliases.get("builtin.position")
        size_id = self._render_aliases.get("builtin.size")
        color_id = self._render_aliases.get("builtin.color")
        position = (
            self.attributes[position_id]
            if position_id is not None
            else exports.get("builtin.position", self.attributes["builtin.position"])
        )
        size = (
            self.attributes[size_id]
            if size_id is not None
            else exports.get("builtin.size", self.attributes["builtin.size"])
        )
        color = (
            self.attributes[color_id]
            if color_id is not None
            else exports.get("builtin.color", self.attributes["builtin.color"])
        )
        output = self._instance_buffer[:count]
        np.copyto(output[:, 0:3], position[:count], casting="unsafe")
        np.copyto(output[:, 3], size[:count], casting="unsafe")
        np.copyto(output[:, 4:8], color[:count], casting="unsafe")
        output[:, 8].fill(0.0)
        return output

    def _scheduled_spawn_count(self, previous: float, current: float, delta_time: float) -> int:
        self._spawn_accumulator += self.settings.spawn_rate * delta_time
        spawn_count = int(self._spawn_accumulator)
        self._spawn_accumulator -= spawn_count
        for state in self._burst_states:
            next_time, remaining, count, interval = state
            while remaining > 0 and next_time <= current:
                if next_time > previous or (previous == 0.0 and next_time == 0.0):
                    spawn_count += int(count)
                remaining -= 1
                next_time += float(interval)
                if interval == 0.0 and remaining > 0:
                    spawn_count += int(count) * int(remaining)
                    remaining = 0
            state[0] = next_time
            state[1] = remaining
        return spawn_count

    def _initialize_range(self, start: int, count: int) -> None:
        particle_slice = slice(start, start + count)
        init_writes = set(self.program.kernel.init.written_attributes)
        for stable_id, target in self.attributes.items():
            if stable_id not in init_writes and stable_id != "builtin.id":
                np.copyto(
                    target[particle_slice],
                    self._attribute_defaults[stable_id],
                    casting="unsafe",
                )
        self.alive[particle_slice] = True
        self._assign_particle_ids(start, count)
        workspace = self._workspaces["init"]
        self.program.init.function(self, workspace, self._context, start, count)
        self._kill_non_finite(self.program.init, start, count)

    def _assign_particle_ids(self, start: int, count: int) -> None:
        target = self.attributes.get("builtin.id")
        if target is None:
            return
        first_count = min(count, 0x100000000 - self._next_particle_id)
        first = slice(start, start + first_count)
        np.add(
            self._ordinal[:first_count],
            np.uint32(self._next_particle_id),
            out=target[first],
        )
        self.spawn_generation[first] = np.uint32(self._spawn_epoch)
        remaining = count - first_count
        if remaining:
            self._spawn_epoch = (self._spawn_epoch + 1) & 0xFFFFFFFF
            second = slice(start + first_count, start + count)
            np.copyto(target[second], self._ordinal[:remaining])
            self.spawn_generation[second] = np.uint32(self._spawn_epoch)
            self._next_particle_id = remaining
        else:
            self._next_particle_id = (self._next_particle_id + count) & 0xFFFFFFFF
            if self._next_particle_id == 0 and count:
                self._spawn_epoch = (self._spawn_epoch + 1) & 0xFFFFFFFF

    def _kill_non_finite(
        self,
        executable: NumpyStageExecutable,
        start: int,
        count: int,
    ) -> None:
        particle_slice = slice(start, start + count)
        alive = self.alive[particle_slice]
        written_attributes = executable_stage_writes(executable, self.program.kernel)
        for stable_id in self.program.kernel.attributes:
            attribute_id = stable_id[0]
            if attribute_id not in written_attributes:
                continue
            values = self.attributes[attribute_id][particle_slice]
            if values.dtype.kind != "f":
                continue
            if values.ndim == 1:
                np.isfinite(values, out=self._finite_mask[:count])
            else:
                components = values.shape[1]
                np.isfinite(values, out=self._component_mask[:count, :components])
                np.all(
                    self._component_mask[:count, :components],
                    axis=1,
                    out=self._finite_mask[:count],
                )
            np.logical_and(alive, self._finite_mask[:count], out=alive)

    def _compact_dead(self) -> None:
        count = self._active_count
        mask = self.alive[:count]
        survivors = int(np.count_nonzero(mask))
        if survivors == count:
            return
        for values in self.attributes.values():
            np.compress(mask, values[:count], axis=0, out=values[:survivors])
        np.compress(
            mask,
            self.spawn_generation[:count],
            axis=0,
            out=self.spawn_generation[:survivors],
        )
        self.alive[:survivors] = True
        self.alive[survivors:count] = False
        self._active_count = survivors

    def _claim_thread(self) -> None:
        current = threading.get_ident()
        if self._owner_thread is None:
            self._owner_thread = current
        elif self._owner_thread != current:
            raise NumpyParticleBackendError(
                "NumPy particle runtime is owned by another thread"
            )


def _compile_stage(function: ParticleKernelFunction, emitter_seed: int) -> NumpyStageExecutable:
    constants: list[Any] = []
    attributes: list[str] = []
    shape_parameters: list[dict[str, Any]] = []
    conversions: list[dict[str, Any]] = []
    scratch_types: list[TypeRef] = []
    export_types: list[tuple[str, TypeRef]] = []
    export_aliases: list[tuple[str, str]] = []
    values: dict[str, str] = {}
    value_attributes: dict[str, str] = {}
    lines = [
        "def _kernel(state, workspace, context, start, count):",
        "    if count <= 0:",
        "        return",
        "    particle_slice = slice(start, start + count)",
        "    scratch = workspace.scratch",
        "    exports = workspace.exports",
    ]

    def result_buffer(instruction) -> str:
        scratch_index = len(scratch_types)
        scratch_types.append(instruction.result_type)
        name = f"v{len(values)}"
        lines.append(f"    {name} = scratch[{scratch_index}][:count]")
        values[instruction.result_id] = name
        return name

    def operand_names(instruction) -> list[str]:
        try:
            return [values[operand.value_id] for operand in instruction.operands]
        except KeyError as exc:
            raise KernelCompileError("NumPy AOT compiler received invalid SSA ordering") from exc

    for instruction in function.instructions:
        opcode = instruction.opcode
        immediates = instruction.immediate_dict()
        if opcode == "constant":
            name = f"v{len(values)}"
            constants.append(_constant_value(immediates["value"], instruction.result_type))
            lines.append(f"    {name} = _constants[{len(constants) - 1}]")
            values[instruction.result_id] = name
        elif opcode == "load_attribute":
            name = f"v{len(values)}"
            attributes.append(immediates["attribute"])
            lines.append(
                f"    {name} = state.attributes[_attributes[{len(attributes) - 1}]][particle_slice]"
            )
            values[instruction.result_id] = name
            value_attributes[instruction.result_id] = immediates["attribute"]
        elif opcode == "load_uniform":
            name = f"v{len(values)}"
            lines.append(f"    {name} = context.{immediates['name']}")
            values[instruction.result_id] = name
        elif opcode in {"add", "multiply", "less_than"}:
            operands = operand_names(instruction)
            output = result_buffer(instruction)
            ufunc = {"add": "add", "multiply": "multiply", "less_than": "less"}[opcode]
            lines.append(f"    np.{ufunc}({operands[0]}, {operands[1]}, out={output})")
        elif opcode == "normalize":
            source = operand_names(instruction)[0]
            output = result_buffer(instruction)
            lines.append(f"    _normalize({output}, {source}, workspace.random_float[5][:count])")
        elif opcode == "random_f32":
            low, high, node_seed = operand_names(instruction)
            output = result_buffer(instruction)
            lines.append(
                f"    _random_range({output}, {low}, {high}, {node_seed}, "
                f"{emitter_seed}, {immediates['random_slot']}, state, particle_slice, context, workspace)"
            )
        elif opcode.startswith("sample_shape_"):
            output = result_buffer(instruction)
            shape_parameters.append(immediates)
            mode = "position" if opcode.endswith("position") else "direction"
            lines.append(
                f"    _sample_shape({output}, _shape_parameters[{len(shape_parameters) - 1}], "
                f"'{mode}', {emitter_seed}, state, particle_slice, context, workspace)"
            )
        elif opcode == "convert_space":
            source = operand_names(instruction)[0]
            output = result_buffer(instruction)
            conversions.append(immediates)
            lines.append(
                f"    _convert_space({output}, {source}, "
                f"_conversions[{len(conversions) - 1}], context)"
            )
        elif opcode == "store_attribute":
            source = operand_names(instruction)[0]
            attributes.append(immediates["attribute"])
            lines.append(
                f"    np.copyto(state.attributes[_attributes[{len(attributes) - 1}]][particle_slice], "
                f"{source}, casting='unsafe')"
            )
        elif opcode == "set_alive":
            source = operand_names(instruction)[0]
            lines.append(f"    np.copyto(state.alive[particle_slice], {source}, casting='unsafe')")
        elif opcode == "export_attribute":
            source = operand_names(instruction)[0]
            stable_id = immediates["attribute"]
            source_attribute = value_attributes.get(instruction.operands[0].value_id)
            if source_attribute is not None:
                export_aliases.append((stable_id, source_attribute))
            else:
                export_types.append((stable_id, instruction.operands[0].value_type))
                lines.append(
                    f"    np.copyto(exports[{stable_id!r}][:count], {source}, casting='unsafe')"
                )
        else:
            raise NumpyParticleBackendError(f"NumPy backend does not implement {opcode!r}")

    source = "\n".join(lines) + "\n"
    namespace = {
        "np": np,
        "_attributes": tuple(attributes),
        "_constants": tuple(constants),
        "_shape_parameters": tuple(shape_parameters),
        "_conversions": tuple(conversions),
        "_convert_space": _convert_space,
        "_normalize": _normalize,
        "_random_range": _random_range,
        "_sample_shape": _sample_shape,
    }
    exec(compile(source, f"<particle-numpy-{function.stage.value}>", "exec"), namespace)
    return NumpyStageExecutable(
        function.stage.value,
        source,
        _StageLayout(
            tuple(scratch_types),
            tuple(export_types),
            tuple(export_aliases),
        ),
        namespace["_kernel"],
    )


def _random_range(
    output,
    low,
    high,
    node_seed,
    emitter_seed,
    random_slot,
    state,
    particle_slice,
    context,
    workspace,
) -> None:
    count = output.shape[0]
    _random01(
        output,
        state,
        particle_slice,
        context,
        emitter_seed,
        node_seed,
        random_slot,
        workspace,
    )
    temporary = workspace.random_float[4][:count]
    np.subtract(high, low, out=temporary)
    np.multiply(output, temporary, out=output)
    np.add(output, low, out=output)


def _random01(
    output,
    state,
    particle_slice,
    context,
    emitter_seed,
    node_seed,
    random_slot,
    workspace,
) -> None:
    count = output.shape[0]
    value = workspace.random_u32_a[:count]
    temporary = workspace.random_u32_b[:count]
    value.fill(np.uint32(0x811C9DC5))
    keys = (
        context.system_seed,
        emitter_seed,
        node_seed,
        state.attributes["builtin.id"][particle_slice],
        state.spawn_generation[particle_slice],
        context.simulation_step,
        random_slot,
    )
    for key in keys:
        np.bitwise_xor(value, key, out=value)
        np.multiply(value, np.uint32(0x01000193), out=value)
        np.right_shift(value, np.uint32(16), out=temporary)
        np.bitwise_xor(value, temporary, out=value)
    np.right_shift(value, np.uint32(16), out=temporary)
    np.bitwise_xor(value, temporary, out=value)
    np.multiply(value, np.uint32(0x7FEB352D), out=value)
    np.right_shift(value, np.uint32(15), out=temporary)
    np.bitwise_xor(value, temporary, out=value)
    np.multiply(value, np.uint32(0x846CA68B), out=value)
    np.right_shift(value, np.uint32(16), out=temporary)
    np.bitwise_xor(value, temporary, out=value)
    np.right_shift(value, np.uint32(8), out=temporary)
    np.copyto(output, temporary, casting="unsafe")
    np.multiply(output, np.float32(1.0 / 16777216.0), out=output)


def _sample_shape(
    output,
    parameters,
    mode,
    emitter_seed,
    state,
    particle_slice,
    context,
    workspace,
) -> None:
    count = output.shape[0]
    random_values = workspace.random_float
    slots = parameters["random_slots"]
    for index in range(3):
        _random01(
            random_values[index][:count],
            state,
            particle_slice,
            context,
            emitter_seed,
            0,
            slots[index],
            workspace,
        )
    u, v, w = (random_values[index][:count] for index in range(3))
    shape = parameters["shape"]
    radius = np.float32(parameters["radius"])
    if shape == "point":
        output.fill(0.0)
        if mode == "direction":
            output[:, 2].fill(1.0)
    elif shape == "box" and mode == "position":
        dimensions = np.asarray(parameters["dimensions"], dtype=np.float32)
        np.subtract(u, np.float32(0.5), out=output[:, 0])
        np.subtract(v, np.float32(0.5), out=output[:, 1])
        np.subtract(w, np.float32(0.5), out=output[:, 2])
        np.multiply(output, dimensions, out=output)
    elif shape == "cone" and mode == "position":
        np.sqrt(u, out=random_values[3][:count])
        np.multiply(random_values[3][:count], radius, out=random_values[3][:count])
        np.multiply(v, np.float32(2.0 * math.pi), out=random_values[4][:count])
        np.cos(random_values[4][:count], out=output[:, 0])
        np.sin(random_values[4][:count], out=output[:, 1])
        np.multiply(output[:, 0], random_values[3][:count], out=output[:, 0])
        np.multiply(output[:, 1], random_values[3][:count], out=output[:, 1])
        output[:, 2].fill(0.0)
    else:
        if shape == "cone":
            cosine_limit = np.float32(math.cos(math.radians(parameters["angle_degrees"])))
            np.multiply(u, np.float32(1.0 - cosine_limit), out=output[:, 2])
            np.add(output[:, 2], cosine_limit, out=output[:, 2])
        else:
            np.multiply(u, np.float32(2.0), out=output[:, 2])
            np.subtract(output[:, 2], np.float32(1.0), out=output[:, 2])
        np.multiply(v, np.float32(2.0 * math.pi), out=random_values[3][:count])
        np.multiply(output[:, 2], output[:, 2], out=random_values[4][:count])
        np.subtract(np.float32(1.0), random_values[4][:count], out=random_values[4][:count])
        np.maximum(random_values[4][:count], np.float32(0.0), out=random_values[4][:count])
        np.sqrt(random_values[4][:count], out=random_values[4][:count])
        np.cos(random_values[3][:count], out=output[:, 0])
        np.sin(random_values[3][:count], out=output[:, 1])
        np.multiply(output[:, 0], random_values[4][:count], out=output[:, 0])
        np.multiply(output[:, 1], random_values[4][:count], out=output[:, 1])
        if mode == "position":
            np.cbrt(w, out=random_values[5][:count])
            np.multiply(random_values[5][:count], radius, out=random_values[5][:count])
            np.multiply(output, random_values[5][:count, None], out=output)


def _normalize(output, source, length_scratch) -> None:
    output.fill(0.0)
    np.multiply(source, source, out=output)
    np.sum(output, axis=1, out=length_scratch)
    np.sqrt(length_scratch, out=length_scratch)
    np.divide(
        source,
        length_scratch[:, None],
        out=output,
        where=length_scratch[:, None] > 0.0,
    )


def _convert_space(output, source, parameters, context) -> None:
    matrix = context.conversion(parameters["from"], parameters["to"])
    np.matmul(source, matrix[:3, :3].T, out=output)
    if parameters["semantic"] == "position":
        np.add(output, matrix[:3, 3], out=output)


def _matrix4(value, fallback: np.ndarray) -> np.ndarray:
    if value is None:
        return fallback.copy()
    result = np.asarray(value, dtype=np.float32)
    if result.shape != (4, 4) or not np.isfinite(result).all():
        raise NumpyParticleBackendError("particle transforms must be finite 4x4 matrices")
    return np.ascontiguousarray(result)


def _component_count(value_type: TypeRef) -> int:
    return {
        ValueType.VEC2: 2,
        ValueType.VEC3: 3,
        ValueType.VEC4: 4,
        ValueType.COLOR: 4,
        ValueType.MAT3: 9,
        ValueType.MAT4: 16,
    }.get(value_type.value_type, 1)


def _dtype(value_type: TypeRef):
    return {
        ValueType.BOOL: np.bool_,
        ValueType.I32: np.int32,
        ValueType.U32: np.uint32,
        ValueType.F32: np.float32,
        ValueType.VEC2: np.float32,
        ValueType.VEC3: np.float32,
        ValueType.VEC4: np.float32,
        ValueType.COLOR: np.float32,
        ValueType.MAT3: np.float32,
        ValueType.MAT4: np.float32,
    }.get(value_type.value_type)


def _allocate_array(capacity: int, value_type: TypeRef) -> np.ndarray:
    dtype = _dtype(value_type)
    if dtype is None:
        raise NumpyParticleBackendError(f"NumPy backend cannot store {value_type.value_type.value}")
    components = _component_count(value_type)
    shape = (capacity,) if components == 1 else (capacity, components)
    return np.empty(shape, dtype=dtype)


def _constant_value(value: Any, value_type: TypeRef):
    dtype = _dtype(value_type)
    if dtype is None:
        raise NumpyParticleBackendError(f"NumPy backend cannot lower constant {value_type}")
    components = _component_count(value_type)
    result = np.asarray(value, dtype=dtype)
    if components == 1:
        return result.reshape(()).item()
    return np.ascontiguousarray(result.reshape(components))


def executable_stage_writes(
    executable: NumpyStageExecutable,
    kernel: ParticleEmitterKernelIR,
) -> frozenset[str]:
    function = {
        "init": kernel.init,
        "update": kernel.update,
        "rendering": kernel.rendering,
    }[executable.stage]
    return frozenset(function.written_attributes)


__all__ = [
    "NumpyParticleBackendError",
    "NumpyParticleCompiler",
    "NumpyParticleEmitterProgram",
    "NumpyParticleEmitterRuntime",
    "NumpyParticleProgram",
    "NumpyStageExecutable",
    "PARTICLE_INSTANCE_FLOATS",
]
