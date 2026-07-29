from Infernux.particle import EmitterSettings, ParticleBurst
from Infernux.particle.gpu_control import GpuParticleEmitterController
from Infernux.particle.spawn_schedule import ParticleSpawnScheduleState


def _combined_settings() -> EmitterSettings:
    return EmitterSettings(
        capacity=100,
        spawn_rate=2.0,
        spawn_rate_over_distance=3.0,
        duration=10.0,
        bursts=(ParticleBurst(time=0.5, count=4, cycles=2, interval=0.5),),
    )


def test_time_distance_and_periodic_bursts_are_additive():
    schedule = ParticleSpawnScheduleState(_combined_settings())

    assert schedule.advance(0.5, (0.0, 0.0, 0.0)) == 5
    assert schedule.advance(0.5, (1.0, 0.0, 0.0)) == 8


def test_disabled_emitter_does_not_simulate_render_or_advance_its_clock():
    controller = GpuParticleEmitterController(_combined_settings())

    frame = controller.tick(
        0.5,
        (0.0, 0.0, 0.0),
        enabled=False,
    )

    assert frame.spawn_count == 0
    assert frame.simulate is False
    assert frame.render is False
    assert controller.simulation_step == 0
    assert controller.simulation_time_ticks == 0

    resumed = controller.tick(0.5, (1.0, 0.0, 0.0), enabled=True)
    assert resumed.spawn_count == 8
    assert resumed.simulate is True
    assert controller.simulation_step == 1


def test_controller_reserves_ids_for_gpu_queued_bursts():
    settings = EmitterSettings(capacity=64, spawn_rate=1.0)
    controller = GpuParticleEmitterController(settings)

    first = controller.tick(1.0)
    second = controller.tick(1.0)

    assert first.spawn_count == 1
    assert first.spawn_base_id == 0
    assert second.spawn_count == 1
    assert second.spawn_base_id == 64


def test_instance_seed_is_stable_and_independent_from_emitter_seed():
    settings = EmitterSettings(
        capacity=64,
        spawn_rate=0.0,
        duration=16.0,
        loop=True,
        seed=7,
        bursts=(
            ParticleBurst(
                time=0.0,
                count=1,
                cycles=16,
                interval=1.0,
                probability=0.5,
            ),
        ),
    )
    first = GpuParticleEmitterController(settings, system_seed=23)
    replay = GpuParticleEmitterController(settings, system_seed=23)
    variant = GpuParticleEmitterController(settings, system_seed=24)

    first_counts = [first.tick(1.0).spawn_count for _ in range(16)]
    replay_counts = [replay.tick(1.0).spawn_count for _ in range(16)]
    variant_counts = [variant.tick(1.0).spawn_count for _ in range(16)]

    assert first_counts == replay_counts
    assert first_counts != variant_counts
    assert first.tick(0.0).system_seed == 23
