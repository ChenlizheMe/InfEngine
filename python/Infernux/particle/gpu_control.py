"""Small CPU control plane for GPU-resident particle emitters."""

from __future__ import annotations

from dataclasses import dataclass
import math

from .asset import EmitterSettings
from .spawn_schedule import ParticleSpawnScheduleState


@dataclass(frozen=True)
class GpuParticleFrameSchedule:
    spawn_count: int
    spawn_base_id: int
    spawn_generation: int
    system_seed: int
    simulation_step: int
    simulation_time_ticks: int
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
        self._spawn_schedule = ParticleSpawnScheduleState(settings)
        self._simulation_step = 0
        self._simulation_time_ticks = 0
        self._next_particle_id = 0
        self._spawn_generation = 0
        self.reset(playing=playing)

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def simulation_step(self) -> int:
        return self._simulation_step

    @property
    def simulation_time_ticks(self) -> int:
        return self._simulation_time_ticks

    def play(self) -> None:
        self._playing = True

    def pause(self) -> None:
        self._playing = False

    def reset(self, *, playing: bool | None = None) -> None:
        if playing is not None:
            self._playing = bool(playing)
        self._spawn_schedule.reset()
        self._simulation_step = 0
        self._simulation_time_ticks = 0
        self._next_particle_id = 0
        self._spawn_generation = 0

    def migrate_to(self, settings: EmitterSettings) -> "GpuParticleEmitterController":
        """Apply compatible settings without resetting emitter-level scheduling."""
        if not isinstance(settings, EmitterSettings):
            raise TypeError("GPU particle controller migration requires EmitterSettings")
        if (
            settings.simulation_space != self.settings.simulation_space
            or settings.bursts != self.settings.bursts
            or settings.duration != self.settings.duration
            or settings.loop != self.settings.loop
            or settings.start_delay != self.settings.start_delay
        ):
            raise ValueError("GPU particle controller settings require an emitter restart")
        migrated = GpuParticleEmitterController(settings, playing=self._playing)
        migrated._spawn_schedule = self._spawn_schedule.migrate_to(settings)
        migrated._simulation_step = self._simulation_step
        migrated._simulation_time_ticks = self._simulation_time_ticks
        migrated._next_particle_id = self._next_particle_id
        migrated._spawn_generation = self._spawn_generation
        return migrated

    def tick(self, delta_time: float, emitter_position=None) -> GpuParticleFrameSchedule:
        delta_time = float(delta_time)
        if not math.isfinite(delta_time) or delta_time < 0.0:
            raise ValueError("particle delta_time must be finite and non-negative")

        spawn_count = 0
        base_id = self._next_particle_id
        generation = self._spawn_generation
        step = self._simulation_step
        if self._playing:
            spawn_count = min(
                self._spawn_schedule.advance(delta_time, emitter_position),
                self.settings.capacity,
            )
            base_id, generation = self._advance_particle_ids(spawn_count)
            self._simulation_step = (self._simulation_step + 1) & 0xFFFFFFFF
            elapsed_ticks = int(round(delta_time * 1_000_000_000.0))
            self._simulation_time_ticks = min(
                0xFFFFFFFFFFFFFFFF,
                self._simulation_time_ticks + elapsed_ticks,
            )
        else:
            self._spawn_schedule.observe_position(emitter_position)

        return GpuParticleFrameSchedule(
            spawn_count,
            base_id,
            generation,
            self.settings.seed,
            step,
            self._simulation_time_ticks,
            delta_time,
            self._playing,
        )

    def _advance_particle_ids(self, count: int) -> tuple[int, int]:
        base_id = self._next_particle_id
        generation = self._spawn_generation
        total = base_id + int(count)
        self._next_particle_id = total & 0xFFFFFFFF
        self._spawn_generation = (generation + (total >> 32)) & 0xFFFFFFFF
        return base_id, generation


__all__ = ["GpuParticleEmitterController", "GpuParticleFrameSchedule"]
