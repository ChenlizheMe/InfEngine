from __future__ import annotations

import pytest

from Infernux.core.anim_state_machine import AnimStateMachine
from Infernux.core.animation_timeline import AnimationTimeline
from Infernux.mcp.tools.authoring import (
    _audio_snapshot,
    _fsm_summary,
    _timeline_summary,
    import_external_binary,
)


def test_external_binary_import_rejects_project_source_and_outside_destination(tmp_path):
    project = tmp_path / "Project"
    assets = project / "Assets"
    assets.mkdir(parents=True)
    source = project / "inside.wav"
    source.write_bytes(b"source")

    with pytest.raises(ValueError, match="outside the target project"):
        import_external_binary(str(project), str(source), "Assets/inside.wav")

    external = tmp_path / "outside.wav"
    external.write_bytes(b"source")
    with pytest.raises(ValueError, match="inside Assets"):
        import_external_binary(str(project), str(external), "../outside.wav")


def test_external_binary_import_is_atomic_and_honors_overwrite(tmp_path, monkeypatch):
    project = tmp_path / "Project"
    assets = project / "Assets"
    assets.mkdir(parents=True)
    external = tmp_path / "source.wav"
    external.write_bytes(b"audio-data")
    imported = []
    monkeypatch.setattr(
        "Infernux.mcp.tools.authoring.notify_asset_changed",
        lambda path, action: imported.append((path, action)),
    )

    (assets / "Audio").mkdir()
    result = import_external_binary(
        str(project), str(external), "Assets/Audio/source.wav"
    )
    assert result["imported"] is True
    assert result["overwritten"] is False
    assert (assets / "Audio" / "source.wav").read_bytes() == b"audio-data"
    assert imported[-1][1] == "created"

    with pytest.raises(FileExistsError):
        import_external_binary(str(project), str(external), "Assets/Audio/source.wav")

    external.write_bytes(b"new-audio")
    result = import_external_binary(
        str(project), str(external), "Assets/Audio/source.wav", overwrite=True
    )
    assert result["overwritten"] is True
    assert (assets / "Audio" / "source.wav").read_bytes() == b"new-audio"
    assert imported[-1][1] == "modified"


class _FakeAudioSource:
    track_count = 1
    volume = 0.8
    pitch = 1.0
    mute = False
    loop = True
    play_on_awake = False
    component_id = 12

    def get_track_clip_guid(self, index):
        return "audio-guid" if index == 0 else ""

    def get_track_volume(self, index):
        return 0.75

    def is_track_playing(self, index):
        return False

    def is_track_paused(self, index):
        return False


class _FakeObject:
    id = 42
    name = "CourseAudio"


def test_public_model_summaries_keep_timeline_and_fsm_contracts():
    timeline = AnimationTimeline(name="CourseMotion", duration=3.0)
    fsm = AnimStateMachine(name="CourseTimeline", mode="timeline")
    state = fsm.add_state("Idle")
    state.kind = "timeline"
    state.timeline_path = "Assets/Animations/course.animtimeline"

    assert _timeline_summary(timeline, "Assets/Animations/course.animtimeline") == {
        "path": "Assets/Animations/course.animtimeline",
        "name": "CourseMotion",
        "duration": 3.0,
        "apply_mode": "additive",
        "keyframe_count": 0,
    }
    summary = _fsm_summary(fsm, "Assets/Animations/course.timelinefsm")
    assert summary["mode"] == "timeline"
    assert summary["states"][0]["kind"] == "timeline"
    assert summary["states"][0]["timeline_path"].endswith("course.animtimeline")


def test_audio_inspection_uses_public_track_methods():
    snapshot = _audio_snapshot(_FakeObject(), _FakeAudioSource())

    assert snapshot["component_type"] == "AudioSource"
    assert snapshot["tracks"] == [{
        "index": 0,
        "guid": "audio-guid",
        "volume": 0.75,
        "playing": False,
        "paused": False,
    }]
