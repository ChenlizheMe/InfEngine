"""Backend-neutral particle emitter scheduling state."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Sequence

from .asset import EmitterSettings


@dataclass
class ParticleSpawnScheduleState:
    """Advance rate and burst sources without touching particle attributes."""

    settings: EmitterSettings
    system_seed: int = 0
    elapsed: float = 0.0
    accumulator: float = 0.0
    distance_accumulator: float = 0.0
    previous_position: tuple[float, float, float] | None = None
    started: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.settings, EmitterSettings):
            raise TypeError("particle spawn schedule requires EmitterSettings")
        if type(self.system_seed) is not int or not 0 <= self.system_seed <= 0xFFFFFFFF:
            raise ValueError("particle system seed must be an unsigned 32-bit integer")

    def reset(self) -> None:
        self.elapsed = 0.0
        self.accumulator = 0.0
        self.distance_accumulator = 0.0
        self.previous_position = None
        self.started = False

    def migrate_to(self, settings: EmitterSettings) -> "ParticleSpawnScheduleState":
        if not isinstance(settings, EmitterSettings):
            raise TypeError("particle spawn schedule migration requires EmitterSettings")
        if (
            settings.bursts != self.settings.bursts
            or settings.duration != self.settings.duration
            or settings.loop != self.settings.loop
            or settings.start_delay != self.settings.start_delay
        ):
            raise ValueError("particle schedule topology changes require an emitter restart")
        return ParticleSpawnScheduleState(
            settings,
            self.system_seed,
            elapsed=self.elapsed,
            accumulator=self.accumulator,
            distance_accumulator=self.distance_accumulator,
            previous_position=self.previous_position,
            started=self.started,
        )

    def observe_position(self, position: Sequence[float] | None) -> None:
        decoded = self._position(position)
        if decoded is not None:
            self.previous_position = decoded

    def advance(
        self,
        delta_time: float,
        position: Sequence[float] | None = None,
    ) -> int:
        delta_time = float(delta_time)
        if not math.isfinite(delta_time) or delta_time < 0.0:
            raise ValueError("particle delta_time must be finite and non-negative")
        previous = self.elapsed
        self.elapsed += delta_time
        current_position = self._position(position)
        active_seconds = self._active_seconds(previous, self.elapsed)
        self.accumulator += self.settings.spawn_rate * active_seconds
        spawn_count = int(self.accumulator)
        self.accumulator -= spawn_count

        if current_position is not None:
            if self.previous_position is not None and active_seconds > 0.0:
                distance = math.dist(self.previous_position, current_position)
                if delta_time > 0.0 and active_seconds < delta_time:
                    distance *= active_seconds / delta_time
                self.distance_accumulator += (
                    distance * self.settings.spawn_rate_over_distance
                )
                distance_count = int(self.distance_accumulator)
                self.distance_accumulator -= distance_count
                spawn_count += distance_count
            self.previous_position = current_position

        spawn_count += self._burst_count(previous, self.elapsed)
        self.started = True
        return spawn_count

    def _active_seconds(self, previous: float, current: float) -> float:
        begin = max(previous, self.settings.start_delay)
        if self.settings.loop:
            end = max(current, self.settings.start_delay)
        else:
            end = min(
                max(current, self.settings.start_delay),
                self.settings.start_delay + self.settings.duration,
            )
            begin = min(begin, self.settings.start_delay + self.settings.duration)
        return max(0.0, end - begin)

    def _burst_count(self, previous: float, current: float) -> int:
        local_previous = previous - self.settings.start_delay
        local_current = current - self.settings.start_delay
        if local_current < 0.0:
            return 0
        if not self.settings.loop and local_previous >= self.settings.duration:
            return 0

        first_loop = 0
        last_loop = 0
        if self.settings.loop:
            first_loop = max(0, int(math.floor(max(local_previous, 0.0) / self.settings.duration)))
            last_loop = max(0, int(math.floor(max(local_current, 0.0) / self.settings.duration)))

        result = 0
        for loop_index in range(first_loop, last_loop + 1):
            cycle_origin = loop_index * self.settings.duration
            for burst_index, burst in enumerate(self.settings.bursts):
                for burst_cycle in range(burst.cycles):
                    event_time = cycle_origin + burst.time + burst_cycle * burst.interval
                    if event_time > local_current:
                        continue
                    if not self.settings.loop and event_time > self.settings.duration:
                        continue
                    crossed = event_time > local_previous
                    if not self.started and event_time == 0.0 and local_previous <= 0.0:
                        crossed = True
                    if crossed and self._burst_passes(
                        burst.probability, burst_index, loop_index, burst_cycle
                    ):
                        result += burst.count
        return result

    def _burst_passes(
        self,
        probability: float,
        burst_index: int,
        loop_index: int,
        burst_cycle: int,
    ) -> bool:
        if probability <= 0.0:
            return False
        if probability >= 1.0:
            return True
        value = (
            int(self.settings.seed)
            ^ int(self.system_seed)
            ^ ((burst_index + 1) * 0x9E3779B9)
            ^ ((loop_index + 1) * 0x85EBCA6B)
            ^ ((burst_cycle + 1) * 0xC2B2AE35)
        ) & 0xFFFFFFFF
        value ^= value >> 16
        value = (value * 0x7FEB352D) & 0xFFFFFFFF
        value ^= value >> 15
        value = (value * 0x846CA68B) & 0xFFFFFFFF
        value ^= value >> 16
        return ((value >> 8) / float(1 << 24)) < probability

    @staticmethod
    def _position(position: Sequence[float] | None) -> tuple[float, float, float] | None:
        if position is None:
            return None
        if len(position) != 3:
            raise ValueError("particle emitter position requires three values")
        result = tuple(float(value) for value in position)
        if not all(math.isfinite(value) for value in result):
            raise ValueError("particle emitter position must be finite")
        return result


__all__ = ["ParticleSpawnScheduleState"]
