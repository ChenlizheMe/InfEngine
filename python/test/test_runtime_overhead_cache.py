"""Pure-Python regression tests for animation and particle idle-path caches."""
from __future__ import annotations

import numpy as np
import pytest

import Infernux.core.timeline_fsm_runtime as timeline_runtime_module
from Infernux.components.particle_system import ParticleSystem
from Infernux.components.skeletal_animator import SkeletalAnimator
from Infernux.components.spirit_animator import SpiritAnimator
from Infernux.core.animation_timeline import AnimationTimeline, TimelineKeyframe
from Infernux.core.anim_state_machine import AnimState, AnimStateMachine
from Infernux.core.timeline_fsm_runtime import TimelineFSMRuntime


_TIMELINE_PATHS = {}


@pytest.fixture(autouse=True)
def _asset_database(monkeypatch):
    _TIMELINE_PATHS.clear()

    class Database:
        @staticmethod
        def get_path_from_guid(guid):
            return _TIMELINE_PATHS.get(guid, "")

    monkeypatch.setattr(timeline_runtime_module, "_get_asset_database", lambda: Database())


class _Transform:
    def __init__(self):
        self.handle = object()
        self.calls = []
        self.local_position = type("V", (), {"x": 0.0, "y": 0.0, "z": 0.0})()
        self.local_euler_angles = self.local_position
        self.local_scale = type("V", (), {"x": 1.0, "y": 1.0, "z": 1.0})()

    def set_local_trs(self, *values):
        self.calls.append(tuple(values))


def _timeline_fsm(tmp_path):
    timeline = AnimationTimeline(
        duration=1.0,
        keyframes=[
            TimelineKeyframe(time=0.0),
            TimelineKeyframe(time=1.0, position=[1.0, 0.0, 0.0]),
        ],
    )
    path = tmp_path / "idle.animtimeline"
    assert timeline.save(str(path))
    state = AnimState(name="Idle", kind="timeline", loop=True)
    state.timeline_guid = "timeline-idle"
    state.timeline_path = str(path)
    _TIMELINE_PATHS[state.timeline_guid] = str(path)
    fsm = AnimStateMachine(mode="timeline", states=[state], default_state="Idle")
    return fsm


def test_timeline_fsm_does_not_sample_after_stop(tmp_path):
    runtime = TimelineFSMRuntime()
    runtime.set_fsm(_timeline_fsm(tmp_path))
    assert runtime.play()
    runtime.stop()

    timeline = runtime._timeline
    sample_calls = []
    original_sample = timeline.sample
    timeline.sample = lambda time: (sample_calls.append(time), original_sample(time))[1]
    runtime.update(1.0)

    assert runtime.needs_update is False
    assert sample_calls == []


def test_timeline_fsm_skips_duplicate_transform_upload(tmp_path):
    runtime = TimelineFSMRuntime()
    runtime.set_fsm(_timeline_fsm(tmp_path))
    transform = _Transform()
    assert runtime.play(transform=transform)
    initial_calls = len(transform.calls)

    runtime.update(0.0, transform)
    assert len(transform.calls) == initial_calls


def test_skeletal_animator_stopped_update_does_not_sync_native_pose():
    animator = SkeletalAnimator()
    animator._playing = False
    animator._current_timeline = None
    animator._current_clip = object()
    animator._sync_native_runtime_playback = lambda: (_ for _ in ()).throw(
        AssertionError("stopped animator must not submit a pose every frame")
    )

    animator.update(1.0 / 60.0)


def test_spirit_animator_stopped_timeline_update_is_idle():
    animator = SpiritAnimator()
    animator._playing = False
    animator._current_timeline = object()

    # The evaluator itself exits before touching the timeline when stopped.
    animator.update(1.0 / 60.0)


def test_particle_system_paused_frame_skips_full_gpu_update(monkeypatch):
    particle = ParticleSystem()
    particle._gpu_controllers = [object()]
    particle._playing = False
    particle._gpu_update_dirty = False
    particle._emitter_to_world_cache = np.eye(4, dtype=np.float32)
    particle._emitter_matrix = lambda: particle._emitter_to_world_cache.copy()
    particle._update_gpu_particle_graph = lambda _delta: (_ for _ in ()).throw(
        AssertionError("paused particle graph must not rebuild a GPU batch")
    )
    monkeypatch.setattr(particle, "_sync_serialized_instance_overrides", lambda: None)
    monkeypatch.setattr(particle, "_apply_pending_runtime_rebuild", lambda: None)
    monkeypatch.setattr(particle, "_has_runtime", lambda: True)
    monkeypatch.setattr(particle, "_reload_published_artifact_if_needed", lambda: None)

    particle.update(1.0 / 60.0)
