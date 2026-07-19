"""Small CPU control plane for GPU-resident particle emitters."""

from __future__ import annotations

from dataclasses import dataclass
import math

from .asset import EmitterSettings


@dataclass(frozen=True)
class GpuParticleFrameSchedule:
    spawn_count: int
    spawn_base_id: int
    spawn_generation: int
    system_seed: int
    simulation_step: int
    delta_time: float
    simulate: bool
    render: bool = True


class GpuParticleEmitterController:
    """Schedules emitter-level work without storing or reading back particles."""

    def __init__(self, settings: EmitterSettings, *, playing: bool = True) -> None:
        if not isinstance(settings, EmitterSettings):
            raise TypeError("GPU particle controller requires EmitterSettings")
        self.settings = settings
        self._playing = bool(playing)
        self._spawn_accumulator = 0.0
        self._elapsed = 0.0
        self._simulation_step = 0
        self._next_particle_id = 0
        self._spawn_generation = 0
        self._burst_states: list[list[float | int]] = []
        self.reset(playing=playing)

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def simulation_step(self) -> int:
        return self._simulation_step

    def play(self) -> None:
        self._playing = True

    def pause(self) -> None:
        self._playing = False

    def reset(self, *, playing: bool | None = None) -> None:
        if playing is not None:
            self._playing = bool(playing)
        self._spawn_accumulator = 0.0
        self._elapsed = 0.0
        self._simulation_step = 0
        self._next_particle_id = 0
        self._spawn_generation = 0
        self._burst_states = [
            [float(burst.time), int(burst.cycles), int(burst.count), float(burst.interval)]
            for burst in self.settings.bursts
        ]

    def migrate_to(self, settings: EmitterSettings) -> "GpuParticleEmitterController":
        """Apply compatible settings without resetting emitter-level scheduling."""
        if not isinstance(settings, EmitterSettings):
            raise TypeError("GPU particle controller migration requires EmitterSettings")
        if (
            settings.capacity != self.settings.capacity
            or settings.simulation_space != self.settings.simulation_space
            or settings.bursts != self.settings.bursts
        ):
            raise ValueError("GPU particle controller settings require an emitter restart")
        migrated = GpuParticleEmitterController(settings, playing=self._playing)
        migrated._spawn_accumulator = self._spawn_accumulator
        migrated._elapsed = self._elapsed
        migrated._simulation_step = self._simulation_step
        migrated._next_particle_id = self._next_particle_id
        migrated._spawn_generation = self._spawn_generation
        migrated._burst_states = [list(state) for state in self._burst_states]
        return migrated

    def tick(self, delta_time: float) -> GpuParticleFrameSchedule:
        delta_time = float(delta_time)
        if not math.isfinite(delta_time) or delta_time < 0.0:
            raise ValueError("particle delta_time must be finite and non-negative")

        spawn_count = 0
        base_id = self._next_particle_id
        generation = self._spawn_generation
        step = self._simulation_step
        if self._playing:
            previous = self._elapsed
            self._elapsed += delta_time
            spawn_count = min(
                self._scheduled_spawn_count(previous, self._elapsed, delta_time),
                self.settings.capacity,
            )
            base_id, generation = self._advance_particle_ids(spawn_count)
            self._simulation_step = (self._simulation_step + 1) & 0xFFFFFFFF

        return GpuParticleFrameSchedule(
            spawn_count,
            base_id,
            generation,
            self.settings.seed,
            step,
            delta_time,
            self._playing,
        )

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

    def _advance_particle_ids(self, count: int) -> tuple[int, int]:
        base_id = self._next_particle_id
        generation = self._spawn_generation
        total = base_id + int(count)
        self._next_particle_id = total & 0xFFFFFFFF
        self._spawn_generation = (generation + (total >> 32)) & 0xFFFFFFFF
        return base_id, generation


__all__ = ["GpuParticleEmitterController", "GpuParticleFrameSchedule"]
