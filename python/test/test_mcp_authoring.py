from __future__ import annotations

import inspect

import pytest

from Infernux.core.animation_clip3d import AnimationClip3D
from Infernux.core.anim_state_machine import AnimStateMachine
from Infernux.core.animation_timeline import AnimationTimeline
from Infernux.mcp.tools.authoring import (
    _audio_snapshot,
    _create_editor_model_asset,
    _fsm_summary,
    _resolve_animation_clip_reference,
    _resolve_imported_animated_model,
    _timeline_summary,
    import_external_binary,
    register_authoring_tools,
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


class _FakeAssetDatabase:
    def __init__(self, path, guid):
        self.path = str(path)
        self.guid = str(guid)

    def get_path_from_guid(self, guid):
        return self.path if str(guid) == self.guid else ""

    def get_guid_from_path(self, path):
        return self.guid if str(path) == self.path else ""


def test_animclip3d_model_resolution_requires_imported_take(monkeypatch, tmp_path):
    project = tmp_path / "Project"
    model = project / "Assets" / "Models" / "hero.fbx"
    model.parent.mkdir(parents=True)
    model.write_bytes(b"fbx")
    database = _FakeAssetDatabase(model, "model-guid")
    monkeypatch.setattr("Infernux.mcp.tools.authoring.get_asset_database", lambda: database)
    monkeypatch.setattr(
        "Infernux.core.asset_types.read_meta_file",
        lambda _path: {
            "animation_names_csv": "Idle,Run",
            "bone_names_csv": "Root,Spine",
        },
    )

    resolved = _resolve_imported_animated_model(
        str(project), model_path="Assets/Models/hero.fbx", model_guid="", take_name="Run"
    )
    assert resolved == {
        "path": str(model.resolve()),
        "guid": "model-guid",
        "take_name": "Run",
        "bind_pose_bone_names": ["Root", "Spine"],
    }

    with pytest.raises(ValueError, match="available takes: Idle, Run"):
        _resolve_imported_animated_model(
            str(project), model_path="", model_guid="model-guid", take_name="Jump"
        )


def test_editor_animation_asset_creation_uses_asset_command_and_import(monkeypatch, tmp_path):
    project = tmp_path / "Project"
    target = project / "Assets" / "Animations" / "Run.animclip3d"
    target.parent.mkdir(parents=True)
    database = _FakeAssetDatabase(target, "clip-guid")
    calls = []

    class _FakeCommandService:
        _service = None

        @classmethod
        def instance(cls):
            return cls._service

        def __init__(self):
            self.project_root = str(project.resolve())
            self.configured = True

        def create(self, current_path, creator, **kwargs):
            calls.append((str(current_path), kwargs))
            return creator()

    _FakeCommandService._service = _FakeCommandService()
    monkeypatch.setattr(
        "Infernux.engine.interaction.ProjectAssetCommandService",
        _FakeCommandService,
    )
    monkeypatch.setattr("Infernux.mcp.tools.authoring.get_asset_database", lambda: database)
    monkeypatch.setattr(
        "Infernux.mcp.tools.authoring.notify_asset_changed",
        lambda path, action: calls.append((str(path), action)),
    )

    result = _create_editor_model_asset(
        str(project),
        str(target),
        AnimationClip3D(name="Run", take_name="Run", source_model_guid="model-guid"),
        overwrite=False,
        description="Create AnimationClip3D",
    )

    assert result["path"] == "Assets/Animations/Run.animclip3d"
    assert result["guid"] == "clip-guid"
    assert result["document"] == {"open": False, "dirty": False, "document_id": ""}
    assert calls[0][1]["description"] == "Create AnimationClip3D"
    assert (str(target), "created") in calls
    assert target.is_file()


def test_animfsm3d_clip_reference_and_tool_schema_are_public_and_stable(monkeypatch, tmp_path):
    project = tmp_path / "Project"
    clip_path = project / "Assets" / "Animations" / "Run.animclip3d"
    clip_path.parent.mkdir(parents=True)
    clip = AnimationClip3D(name="Run", take_name="Run", source_model_guid="model-guid")
    assert clip.save(str(clip_path))
    database = _FakeAssetDatabase(clip_path, "clip-guid")
    monkeypatch.setattr("Infernux.mcp.tools.authoring.get_asset_database", lambda: database)

    resolved_path, guid, loaded = _resolve_animation_clip_reference(
        str(project), clip_path="Assets/Animations/Run.animclip3d", clip_guid=""
    )
    assert resolved_path == str(clip_path.resolve())
    assert guid == "clip-guid"
    assert loaded.take_name == "Run"

    class _FakeMcp:
        def __init__(self):
            self.tools = {}

        def tool(self, *, name):
            def decorate(function):
                self.tools[name] = function
                return function
            return decorate

    fake_mcp = _FakeMcp()
    register_authoring_tools(fake_mcp, str(project))
    clip_tool = fake_mcp.tools["animation_clip3d_create_from_model"]
    fsm_tool = fake_mcp.tools["animation_fsm3d_create"]
    assert list(inspect.signature(clip_tool).parameters) == [
        "path", "take_name", "model_path", "model_guid", "overwrite"
    ]
    assert list(inspect.signature(fsm_tool).parameters) == [
        "path", "clip_path", "clip_guid", "state_name", "overwrite"
    ]

    fsm = AnimStateMachine(name="Controller", mode="3d")
    state = fsm.add_state("Run")
    state.clip_guid = guid
    state.clip_path = str(clip_path.resolve())
    state.loop = True
    assert fsm.mode == "3d"
    assert fsm.default_state == "Run"
    assert state.stable_id
    assert state.loop is True
