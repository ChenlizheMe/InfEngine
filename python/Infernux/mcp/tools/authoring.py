"""Small, public Editor authoring tools for assets and scene components.

This module deliberately stays on the public Python model/component surface.
It does not manufacture serialized documents: animation assets are written by
their model ``save`` methods and binary assets enter through AssetManager's
normal import notification.
"""

from __future__ import annotations

import os
import tempfile
import math
from typing import Any

from Infernux.core.asset_types import AUDIO_EXTENSIONS
from Infernux.engine.path_utils import (
    is_path_within,
    relative_path,
    resolved_path,
    same_path,
)
from Infernux.mcp.tools.common import (
    ensure_not_active_scene_file,
    find_game_object,
    get_asset_database,
    main_thread,
    notify_asset_changed,
    register_tool_metadata,
    require_existing_parent_directory,
    require_knowledge_token,
    resolve_asset_path,
)


_BINARY_ASSET_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".jpe", ".bmp", ".tga", ".gif", ".psd",
    ".hdr", ".pic", ".pnm", ".pgm", ".ppm", ".inxvfield", ".inxsdf",
    ".fbx", ".obj", ".gltf", ".glb", ".dae", ".3ds", ".ply", ".stl",
    ".x", ".b3d", ".ase", ".blend", ".bvh", ".cob", ".c4d", ".csm",
    ".dxf", ".hmp", ".ifc", ".iqm", ".irrmesh", ".lwo", ".lws",
    ".m3d", ".md2", ".md3", ".md4", ".md5mesh", ".mdc", ".mmd",
    ".ms3d", ".nff", ".off", ".ogex", ".x3d", ".ttf", ".otf",
}) | AUDIO_EXTENSIONS


def _copy_file_atomically(source: str, target: str, *, overwrite: bool) -> int:
    """Copy *source* into *target* without exposing a partial destination."""
    if not overwrite and os.path.exists(target):
        raise FileExistsError(f"Destination already exists: {target}")

    parent = os.path.dirname(target)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".mcp-import-",
        suffix=".tmp",
        dir=parent,
    )
    copied = 0
    try:
        with os.fdopen(descriptor, "wb") as output, open(source, "rb") as input_file:
            while True:
                chunk = input_file.read(1024 * 1024)
                if not chunk:
                    break
                output.write(chunk)
                copied += len(chunk)
            output.flush()
            os.fsync(output.fileno())

        if overwrite:
            os.replace(temporary, target)
            temporary = ""
        else:
            # A hard-link is an atomic create-if-absent operation on the
            # filesystems used by the Editor. Keep a checked replace fallback
            # for filesystems that do not expose hard links.
            try:
                os.link(temporary, target)
                os.remove(temporary)
                temporary = ""
            except FileExistsError:
                raise
            except OSError:
                if os.path.exists(target):
                    raise FileExistsError(f"Destination appeared during import: {target}")
                os.replace(temporary, target)
                temporary = ""
        return copied
    finally:
        if temporary:
            try:
                os.remove(temporary)
            except OSError:
                pass


def import_external_binary(
    project_path: str,
    source_path: str,
    destination_path: str,
    *,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Validate, atomically copy, and import one external binary asset."""
    project_root = resolved_path(project_path)
    assets_root = resolved_path(os.path.join(project_root, "Assets"))
    source = resolved_path(source_path)
    if not os.path.isabs(str(source_path or "")):
        raise ValueError("source_path must be an explicit absolute external path")
    if not os.path.isfile(source):
        raise FileNotFoundError(f"External source file not found: {source_path}")
    if is_path_within(source, project_root):
        raise ValueError("source_path must be outside the target project")
    extension = os.path.splitext(source)[1].lower()
    if extension not in _BINARY_ASSET_EXTENSIONS:
        raise ValueError(
            f"Unsupported binary asset extension '{extension}'. "
            "Use a format supported by the project's asset importers."
        )

    target = resolve_asset_path(project_root, destination_path)
    if same_path(target, assets_root):
        raise ValueError("destination_path must name an asset file below Assets/")
    if os.path.splitext(target)[1].lower() != extension:
        raise ValueError("destination_path must keep the source asset extension")
    require_existing_parent_directory(project_root, target)
    ensure_not_active_scene_file(project_root, target, "import")

    existed = os.path.exists(target)
    size = _copy_file_atomically(source, target, overwrite=overwrite)
    try:
        notify_asset_changed(target, "modified" if existed else "created")
    except Exception:
        # The file is complete and may be retried through the normal importer;
        # surface the importer failure instead of reporting a false success.
        raise
    return {
        "source_path": source,
        "path": relative_path(target, project_root),
        "bytes": size,
        "overwritten": bool(existed),
        "imported": True,
    }


def _audio_source(object_id: int, ordinal: int):
    from Infernux.mcp.tools.scene import _find_component

    obj = find_game_object(object_id)
    source = _find_component(obj, "AudioSource", int(ordinal))
    if source is None:
        raise FileNotFoundError(
            f"AudioSource ordinal {ordinal} was not found on GameObject {object_id}."
        )
    return obj, source


def _audio_track_snapshot(source, track_index: int) -> dict[str, Any]:
    count = int(source.track_count)
    if track_index < 0 or track_index >= count:
        raise IndexError(f"track_index must be in [0, {count})")
    return {
        "index": int(track_index),
        "guid": str(source.get_track_clip_guid(track_index) or ""),
        "volume": float(source.get_track_volume(track_index)),
        "playing": bool(source.is_track_playing(track_index)),
        "paused": bool(source.is_track_paused(track_index)),
    }


def _audio_snapshot(obj, source) -> dict[str, Any]:
    count = int(source.track_count)
    return {
        "object_id": int(obj.id),
        "object_name": str(obj.name),
        "component_type": "AudioSource",
        "component_id": int(getattr(source, "component_id", 0) or 0),
        "track_count": count,
        "volume": float(source.volume),
        "pitch": float(source.pitch),
        "mute": bool(source.mute),
        "loop": bool(source.loop),
        "play_on_awake": bool(source.play_on_awake),
        "tracks": [_audio_track_snapshot(source, index) for index in range(count)],
    }


def _resolve_timeline_path(project_path: str, path: str) -> str:
    target = resolve_asset_path(project_path, path)
    if not target.lower().endswith(".animtimeline"):
        raise ValueError("timeline_path must point to a .animtimeline asset")
    if not os.path.isfile(target):
        raise FileNotFoundError(f"Timeline asset not found: {path}")
    return target


def _asset_guid(path: str) -> str:
    database = get_asset_database()
    if database is None:
        return ""
    return str(database.get_guid_from_path(path) or "")


def _save_model_asset(model, target: str, *, action: str) -> None:
    save = getattr(model, "save", None)
    if not callable(save) or not bool(save(target)):
        raise RuntimeError(f"The public asset model could not save '{target}'")
    notify_asset_changed(target, action)


def _document_status(path: str) -> dict[str, Any]:
    """Describe the live document state for a newly-created resource."""
    status = {
        "open": False,
        "dirty": False,
        "document_id": "",
    }
    try:
        from Infernux.engine.interaction import DocumentRegistry

        registry = DocumentRegistry.instance()
        if registry is None:
            return status
        target = resolved_path(path)
        for document in registry.documents:
            resource = str(getattr(document, "resource_path", "") or "")
            if not resource or not same_path(resource, target):
                continue
            status.update({
                "open": True,
                "dirty": bool(getattr(document, "is_dirty", False)),
                "document_id": str(getattr(document, "document_id", "") or ""),
            })
            break
    except (ImportError, AttributeError, RuntimeError, TypeError, ValueError):
        pass
    return status


def _create_editor_model_asset(
    project_path: str,
    target: str,
    model: Any,
    *,
    overwrite: bool,
    description: str,
) -> dict[str, Any]:
    """Create an Editor-owned model through the canonical asset command."""
    from Infernux.engine.interaction import ActionOrigin, ProjectAssetCommandService

    service = ProjectAssetCommandService.instance()
    if service is None:
        raise RuntimeError("Editor Project asset command service is unavailable")
    if not service.configured:
        service.configure(project_path, get_asset_database())
    elif not same_path(service.project_root, project_path):
        raise RuntimeError("Editor Project asset commands are bound to another project")

    target = resolved_path(target)
    existed = os.path.exists(target)

    def _creator():
        if not bool(model.save(target)):
            return False, f"The public asset model could not save '{target}'"
        try:
            # The command owns the mutation; this notification publishes the
            # new file to AssetDatabase and the normal resource reload path.
            notify_asset_changed(target, "created")
        except Exception:
            try:
                os.remove(target)
            except OSError:
                pass
            raise
        return True, ""

    service.create(
        os.path.dirname(target),
        _creator,
        description=description,
        origin=ActionOrigin.AUTOMATION,
        replace_path=target if overwrite and existed else "",
    )
    database = get_asset_database()
    guid = str(database.get_guid_from_path(target) or "") if database is not None else ""
    return {
        "path": relative_path(target, project_path),
        "guid": guid,
        "document": _document_status(target),
        "asset_registered": bool(guid),
    }


def _resolve_imported_animated_model(
    project_path: str,
    *,
    model_path: str,
    model_guid: str,
    take_name: str,
) -> dict[str, Any]:
    """Resolve one imported model and validate one importer-reported take."""
    from Infernux.core.asset_types import MESH_EXTENSIONS, read_meta_file

    if bool(str(model_path).strip()) == bool(str(model_guid).strip()):
        raise ValueError("Provide exactly one of model_path or model_guid")
    take = str(take_name or "").strip()
    if not take:
        raise ValueError("take_name must not be empty")

    database = get_asset_database()
    if database is None:
        raise RuntimeError("AssetDatabase is not available")
    if model_guid:
        source = str(database.get_path_from_guid(str(model_guid).strip()) or "")
        if not source:
            raise FileNotFoundError(f"Animated model GUID was not found: {model_guid}")
    else:
        source = resolve_asset_path(project_path, model_path)
    source = resolved_path(source)
    assets_root = resolved_path(os.path.join(project_path, "Assets"))
    if not is_path_within(source, assets_root):
        raise ValueError("The animated model must be an asset below Assets/")
    if not os.path.isfile(source):
        raise FileNotFoundError(f"Animated model file not found: {model_path or model_guid}")
    if os.path.splitext(source)[1].lower() not in MESH_EXTENSIONS:
        raise ValueError("The source asset must be an imported 3D model")

    canonical_guid = str(database.get_guid_from_path(source) or "").strip()
    if not canonical_guid:
        raise ValueError("The source model is not registered in AssetDatabase")
    if model_guid and canonical_guid != str(model_guid).strip():
        raise ValueError("AssetDatabase returned a different GUID for the source model")

    meta = read_meta_file(source) or {}
    raw_names = meta.get("animation_names_csv") or ""
    names = [item.strip() for item in raw_names.split(",") if item.strip()] if isinstance(raw_names, str) else []
    if not names:
        # A newly discovered model can already have authoritative runtime skin
        # data while its asynchronous importer has not enriched the sidecar
        # metadata yet.  Do not reject a valid first-import take solely because
        # the optional metadata cache is lagging behind the AssetRegistry.
        from Infernux.lib import AssetRegistry

        runtime_mesh = AssetRegistry.instance().load_mesh_by_guid(canonical_guid)
        if runtime_mesh is not None and bool(getattr(runtime_mesh, "has_skinned_data", False)):
            names = [
                str(item).strip()
                for item in getattr(runtime_mesh, "skinned_animation_names", ())
                if str(item).strip()
            ]
    if take not in names:
        available = ", ".join(names) if names else "none"
        raise ValueError(f"Animation take '{take}' was not imported from the model; available takes: {available}")
    raw_bones = meta.get("bone_names_csv") or ""
    bones = [item.strip() for item in raw_bones.split(",") if item.strip()] if isinstance(raw_bones, str) else []
    return {
        "path": source,
        "guid": canonical_guid,
        "take_name": take,
        "bind_pose_bone_names": bones,
    }


def _resolve_animation_clip_reference(
    project_path: str,
    *,
    clip_path: str,
    clip_guid: str,
) -> tuple[str, str, Any]:
    """Resolve a registered .animclip3d using the public asset identity."""
    from Infernux.core.animation_clip3d import AnimationClip3D

    if bool(str(clip_path).strip()) == bool(str(clip_guid).strip()):
        raise ValueError("Provide exactly one of clip_path or clip_guid")
    database = get_asset_database()
    if database is None:
        raise RuntimeError("AssetDatabase is not available")
    target = (
        str(database.get_path_from_guid(str(clip_guid).strip()) or "")
        if clip_guid
        else resolve_asset_path(project_path, clip_path)
    )
    target = resolved_path(target)
    if not os.path.isfile(target) or not target.lower().endswith(".animclip3d"):
        raise FileNotFoundError(f"AnimationClip3D asset not found: {clip_path or clip_guid}")
    guid = str(database.get_guid_from_path(target) or "").strip()
    if not guid:
        raise ValueError("The animation clip is not registered in AssetDatabase")
    if clip_guid and guid != str(clip_guid).strip():
        raise ValueError("AssetDatabase returned a different GUID for the animation clip")
    clip = AnimationClip3D.load(target)
    if clip is None or not clip.is_valid_reference:
        raise ValueError("The AnimationClip3D asset is invalid or has no model reference")
    return target, guid, clip


def _resolve_sprite_texture_reference(
    project_path: str,
    *,
    texture_path: str,
    texture_guid: str,
) -> tuple[str, str, Any]:
    """Resolve one registered Sprite texture and its persistent frame contract."""

    from Infernux.core.asset_types import TextureType, read_texture_import_settings

    if bool(str(texture_path).strip()) == bool(str(texture_guid).strip()):
        raise ValueError("Provide exactly one of texture_path or texture_guid")
    database = get_asset_database()
    if database is None:
        raise RuntimeError("AssetDatabase is not available")
    target = (
        str(database.get_path_from_guid(str(texture_guid).strip()) or "")
        if texture_guid
        else resolve_asset_path(project_path, texture_path)
    )
    target = resolved_path(target)
    if not os.path.isfile(target):
        raise FileNotFoundError("Sprite texture asset was not found")
    canonical_guid = str(database.get_guid_from_path(target) or "").strip()
    if not canonical_guid:
        raise ValueError("Sprite texture is not registered in AssetDatabase")
    settings = read_texture_import_settings(target)
    if settings.texture_type is not TextureType.SPRITE:
        raise ValueError("Texture must be imported as Sprite")
    if not settings.sprite_frames:
        raise ValueError("Sprite texture has no persistent SpriteFrame subresources")
    return target, canonical_guid, settings


def _resolve_animation_clip2d_reference(
    project_path: str,
    *,
    clip_path: str,
    clip_guid: str,
) -> tuple[str, str, Any]:
    """Resolve one registered AnimationClip2D through AssetDatabase identity."""

    from Infernux.core.animation_clip import AnimationClip

    if bool(str(clip_path).strip()) == bool(str(clip_guid).strip()):
        raise ValueError("Provide exactly one of clip_path or clip_guid")
    database = get_asset_database()
    if database is None:
        raise RuntimeError("AssetDatabase is not available")
    target = (
        str(database.get_path_from_guid(str(clip_guid).strip()) or "")
        if clip_guid
        else resolve_asset_path(project_path, clip_path)
    )
    target = resolved_path(target)
    if not os.path.isfile(target) or not target.lower().endswith(".animclip2d"):
        raise FileNotFoundError("AnimationClip2D asset was not found")
    canonical_guid = str(database.get_guid_from_path(target) or "").strip()
    if not canonical_guid:
        raise ValueError("AnimationClip2D is not registered in AssetDatabase")
    clip = AnimationClip.load(target)
    if clip is None:
        raise ValueError("AnimationClip2D could not be loaded")
    return target, canonical_guid, clip


def _vec3_input(value: list[float] | None, default: list[float], label: str) -> list[float]:
    raw = list(default if value is None else value)
    if len(raw) != 3 or any(
        not isinstance(item, (int, float))
        or isinstance(item, bool)
        or not math.isfinite(float(item))
        for item in raw
    ):
        raise ValueError(f"{label} must contain three finite numbers")
    return [float(item) for item in raw]


def _timeline_summary(timeline, target: str) -> dict[str, Any]:
    return {
        "path": target,
        "name": str(timeline.name),
        "duration": float(timeline.duration),
        "apply_mode": str(timeline.apply_mode),
        "keyframe_count": len(timeline.keyframes),
    }


def _fsm_summary(fsm, target: str) -> dict[str, Any]:
    return {
        "path": target,
        "name": str(fsm.name),
        "mode": str(fsm.mode),
        "default_state": str(fsm.default_state),
        "state_count": int(fsm.state_count),
        "states": [state.to_dict() for state in fsm.states],
    }


def register_authoring_tools(mcp, project_path: str) -> None:
    """Register Editor-only authoring tools under the existing asset policy."""
    metadata = {
        "asset_import_external_binary": (
            "Copy an external supported binary asset into Assets atomically and trigger the standard importer.",
            "assets/import", ["asset", "import", "binary"],
        ),
        "audio_source_inspect": (
            "Inspect one AudioSource and its public multi-track playback state.",
            "audio/authoring", ["audio", "audiosource", "inspect"],
        ),
        "audio_source_configure_track": (
            "Configure an AudioSource track using its public component API.",
            "audio/authoring", ["audio", "audiosource", "track"],
        ),
        "audio_source_play": ("Start one AudioSource track.", "audio/authoring", ["audio", "play"]),
        "audio_source_pause": ("Pause one AudioSource track.", "audio/authoring", ["audio", "pause"]),
        "audio_source_stop": ("Stop one AudioSource track.", "audio/authoring", ["audio", "stop"]),
        "animation_timeline_create": (
            "Create and save a public AnimationTimeline asset.",
            "animation/authoring", ["animation", "timeline", "create"],
        ),
        "animation_timeline_add_keyframe": (
            "Edit an AnimationTimeline through its public model and save it.",
            "animation/authoring", ["animation", "timeline", "keyframe"],
        ),
        "animation_clip2d_create": (
            "Create an AnimationClip2D from persistent SpriteFrame subresources.",
            "animation/authoring", ["animation", "animclip2d", "sprite", "create"],
        ),
        "animation_fsm2d_create": (
            "Create a 2D AnimStateMachine from registered AnimationClip2D assets.",
            "animation/authoring", ["animation", "animfsm", "2d", "state"],
        ),
        "timeline_fsm_create": (
            "Create a timeline-mode state machine and optionally reference a timeline asset.",
            "animation/authoring", ["animation", "timelinefsm", "create"],
        ),
        "timeline_fsm_add_state": (
            "Add a timeline state to a saved timeline-mode state machine.",
            "animation/authoring", ["animation", "timelinefsm", "state"],
        ),
        "animation_clip3d_create_from_model": (
            "Create an AnimationClip3D from an imported model take through the Editor asset command.",
            "animation/authoring", ["animation", "animclip3d", "model", "take"],
        ),
        "animation_fsm3d_create": (
            "Create a 3D AnimStateMachine with an optional looping default clip state.",
            "animation/authoring", ["animation", "animfsm", "3d", "state"],
        ),
    }
    for name, (summary, category, tags) in metadata.items():
        register_tool_metadata(
            name,
            summary=summary,
            category=category,
            tags=tags,
            preconditions=["The Editor is in developer_assist mode.", "The target project is active."],
            side_effects=["Changes project assets or the active scene component through a public Editor API."],
            recovery=["Inspect the returned error and retry only after its path/component preconditions are satisfied."],
            risk_level="medium",
        )

    @mcp.tool(name="asset_import_external_binary")
    def asset_import_external_binary(
        source_path: str,
        destination_path: str,
        overwrite: bool = False,
    ) -> dict:
        """Import one supported binary file from outside the project into Assets/."""
        return main_thread(
            "asset_import_external_binary",
            lambda: import_external_binary(
                project_path, source_path, destination_path, overwrite=overwrite
            ),
            arguments={"source_path": source_path, "destination_path": destination_path, "overwrite": overwrite},
        )

    @mcp.tool(name="audio_source_inspect")
    def audio_source_inspect(object_id: int, ordinal: int = 0, knowledge_token: str = "") -> dict:
        """Inspect an AudioSource through its public API."""
        def _inspect():
            require_knowledge_token("audio", knowledge_token, required_tool="audio_guide")
            obj, source = _audio_source(object_id, ordinal)
            return _audio_snapshot(obj, source)

        return main_thread("audio_source_inspect", _inspect, arguments={"object_id": object_id, "ordinal": ordinal, "knowledge_token": knowledge_token})

    @mcp.tool(name="audio_source_configure_track")
    def audio_source_configure_track(
        object_id: int,
        track_index: int,
        track_count: int | None = None,
        clip_path: str = "",
        clip_guid: str = "",
        volume: float | None = None,
        ordinal: int = 0,
        knowledge_token: str = "",
    ) -> dict:
        """Configure one AudioSource track without editing serialized JSON."""
        def _configure():
            require_knowledge_token("audio", knowledge_token, required_tool="audio_guide")
            obj, source = _audio_source(object_id, ordinal)
            if track_count is not None:
                count = int(track_count)
                if count < 1 or count > 16:
                    raise ValueError("track_count must be in the public range 1..16")
                source.track_count = count
            index = int(track_index)
            _audio_track_snapshot(source, index)
            if clip_guid and clip_path:
                raise ValueError("Provide clip_guid or clip_path, not both")
            if clip_guid:
                source.set_track_clip_by_guid(index, str(clip_guid))
            elif clip_path:
                clip = resolve_asset_path(project_path, clip_path)
                if not os.path.isfile(clip):
                    raise FileNotFoundError(f"Audio asset not found: {clip_path}")
                if os.path.splitext(clip)[1].lower() not in AUDIO_EXTENSIONS:
                    raise ValueError("clip_path must point to a WAV, OGG, MP3, or FLAC asset")
                guid = _asset_guid(clip)
                if guid:
                    source.set_track_clip_by_guid(index, guid)
                else:
                    from Infernux.core.audio_clip import AudioClip
                    loaded = AudioClip.load(clip)
                    if loaded is None:
                        raise RuntimeError(f"AudioClip could not load '{clip}'")
                    source.set_track_clip(index, loaded)
            if volume is not None:
                source.set_track_volume(index, float(volume))
            return _audio_snapshot(obj, source)

        return main_thread("audio_source_configure_track", _configure, arguments={"object_id": object_id, "track_index": track_index, "track_count": track_count, "clip_path": clip_path, "clip_guid": clip_guid, "volume": volume, "ordinal": ordinal, "knowledge_token": knowledge_token})

    def _audio_control(action: str, object_id: int, track_index: int, ordinal: int, knowledge_token: str) -> dict:
        def _control():
            require_knowledge_token("audio", knowledge_token, required_tool="audio_guide")
            obj, source = _audio_source(object_id, ordinal)
            _audio_track_snapshot(source, int(track_index))
            getattr(source, action)(int(track_index))
            return _audio_snapshot(obj, source)
        return main_thread(f"audio_source_{action}", _control, arguments={"object_id": object_id, "track_index": track_index, "ordinal": ordinal, "knowledge_token": knowledge_token})

    @mcp.tool(name="audio_source_play")
    def audio_source_play(object_id: int, track_index: int = 0, ordinal: int = 0, knowledge_token: str = "") -> dict:
        return _audio_control("play", object_id, track_index, ordinal, knowledge_token)

    @mcp.tool(name="audio_source_pause")
    def audio_source_pause(object_id: int, track_index: int = 0, ordinal: int = 0, knowledge_token: str = "") -> dict:
        return _audio_control("pause", object_id, track_index, ordinal, knowledge_token)

    @mcp.tool(name="audio_source_stop")
    def audio_source_stop(object_id: int, track_index: int = 0, ordinal: int = 0, knowledge_token: str = "") -> dict:
        return _audio_control("stop", object_id, track_index, ordinal, knowledge_token)

    @mcp.tool(name="animation_timeline_create")
    def animation_timeline_create(
        path: str,
        duration: float = 2.0,
        apply_mode: str = "additive",
        overwrite: bool = False,
    ) -> dict:
        """Create a .animtimeline through AnimationTimeline.save()."""
        def _create():
            from Infernux.core.animation_timeline import (
                APPLY_MODES,
                AnimationTimeline,
            )
            target = resolve_asset_path(project_path, path)
            if not target.lower().endswith(".animtimeline"):
                raise ValueError("path must end with .animtimeline")
            require_existing_parent_directory(project_path, target)
            if os.path.exists(target) and not overwrite:
                raise FileExistsError(f"Asset already exists: {path}")
            duration_value = float(duration)
            if not math.isfinite(duration_value) or duration_value < 0.0:
                raise ValueError("duration must be finite and non-negative")
            if apply_mode not in APPLY_MODES:
                raise ValueError(f"apply_mode must be one of {APPLY_MODES}")
            timeline = AnimationTimeline(
                name=os.path.splitext(os.path.basename(target))[0],
                duration=duration_value,
                apply_mode=str(apply_mode),
            )
            _save_model_asset(timeline, target, action="modified" if os.path.exists(target) else "created")
            return _timeline_summary(timeline, target)
        return main_thread("animation_timeline_create", _create, arguments={"path": path, "duration": duration, "apply_mode": apply_mode, "overwrite": overwrite})

    @mcp.tool(name="animation_timeline_add_keyframe")
    def animation_timeline_add_keyframe(
        path: str,
        time_seconds: float,
        position: list[float] | None = None,
        rotation: list[float] | None = None,
        scale: list[float] | None = None,
        interp: str = "linear",
    ) -> dict:
        """Append a typed transform keyframe and save the timeline model."""
        def _edit():
            from Infernux.core.animation_timeline import (
                INTERP_MODES,
                AnimationTimeline,
                TimelineKeyframe,
            )
            target = resolve_asset_path(project_path, path)
            if not target.lower().endswith(".animtimeline"):
                raise ValueError("path must end with .animtimeline")
            timeline = AnimationTimeline.load(target)
            if timeline is None:
                raise FileNotFoundError(f"Timeline asset could not be loaded: {path}")
            if not math.isfinite(float(time_seconds)):
                raise ValueError("time_seconds must be finite")
            if interp not in INTERP_MODES:
                raise ValueError(f"interp must be one of {INTERP_MODES}")
            timeline.keyframes.append(TimelineKeyframe(
                time=float(time_seconds),
                position=_vec3_input(position, [0.0, 0.0, 0.0], "position"),
                rotation=_vec3_input(rotation, [0.0, 0.0, 0.0], "rotation"),
                scale=_vec3_input(scale, [1.0, 1.0, 1.0], "scale"),
                interp=str(interp),
            ))
            _save_model_asset(timeline, target, action="modified")
            return _timeline_summary(timeline, target)
        return main_thread("animation_timeline_add_keyframe", _edit, arguments={"path": path, "time_seconds": time_seconds, "interp": interp})

    @mcp.tool(name="animation_clip2d_create")
    def animation_clip2d_create(
        path: str,
        sprite_frame_ids: list[str],
        texture_path: str = "",
        texture_guid: str = "",
        fps: float = 12.0,
        loop: bool = True,
        overwrite: bool = False,
    ) -> dict:
        """Create a .animclip2d using stable SpriteFrame IDs."""

        def _create():
            from Infernux.core.animation_clip import AnimationClip, AnimationFrame

            target = resolve_asset_path(project_path, path)
            if not target.lower().endswith(".animclip2d"):
                raise ValueError("path must end with .animclip2d")
            require_existing_parent_directory(project_path, target)
            if os.path.exists(target) and not overwrite:
                raise FileExistsError(f"Asset already exists: {path}")
            source, source_guid, settings = _resolve_sprite_texture_reference(
                project_path,
                texture_path=texture_path,
                texture_guid=texture_guid,
            )
            requested = [str(value or "").strip() for value in sprite_frame_ids]
            if not requested or any(not value for value in requested):
                raise ValueError("sprite_frame_ids must contain at least one stable ID")
            available = {frame.stable_id for frame in settings.sprite_frames}
            missing = sorted(set(requested) - available)
            if missing:
                raise ValueError(
                    f"SpriteFrame IDs do not belong to the source texture: {missing}"
                )
            fps_value = float(fps)
            if not math.isfinite(fps_value) or fps_value <= 0.0:
                raise ValueError("fps must be a positive finite number")
            clip = AnimationClip(
                name=os.path.splitext(os.path.basename(target))[0],
                authoring_texture_guid=source_guid,
                authoring_texture_path=relative_path(source, project_path),
                frames=[
                    AnimationFrame(sprite_frame_id=frame_id)
                    for frame_id in requested
                ],
                fps=fps_value,
                loop=bool(loop),
            )
            result = _create_editor_model_asset(
                project_path,
                target,
                clip,
                overwrite=bool(overwrite),
                description="Create AnimationClip2D",
            )
            result.update({
                "texture_guid": source_guid,
                "texture_path": relative_path(source, project_path),
                "frame_count": len(clip.frames),
                "sprite_frame_ids": requested,
                "fps": fps_value,
                "loop": bool(loop),
            })
            return result

        return main_thread(
            "animation_clip2d_create",
            _create,
            arguments={
                "path": path,
                "texture_path": texture_path,
                "texture_guid": texture_guid,
                "frame_count": len(sprite_frame_ids),
                "fps": fps,
                "loop": loop,
                "overwrite": overwrite,
            },
        )

    @mcp.tool(name="animation_fsm2d_create")
    def animation_fsm2d_create(
        path: str,
        states: list[dict],
        default_state: str = "",
        overwrite: bool = False,
    ) -> dict:
        """Create a 2D .animfsm from named AnimationClip2D state records."""

        def _create():
            from Infernux.core.anim_state_machine import AnimStateMachine

            target = resolve_asset_path(project_path, path)
            if not target.lower().endswith(".animfsm"):
                raise ValueError("path must end with .animfsm")
            require_existing_parent_directory(project_path, target)
            if os.path.exists(target) and not overwrite:
                raise FileExistsError(f"Asset already exists: {path}")
            if not states:
                raise ValueError("states must contain at least one state")

            fsm = AnimStateMachine(
                name=os.path.splitext(os.path.basename(target))[0],
                mode="2d",
            )
            seen_names: set[str] = set()
            for index, value in enumerate(states):
                if type(value) is not dict:
                    raise TypeError("states entries must be objects")
                allowed = {
                    "name", "clip_path", "clip_guid", "loop", "speed", "position",
                }
                unknown = set(value) - allowed
                if unknown:
                    raise ValueError(
                        f"unsupported animation state fields: {sorted(unknown)}"
                    )
                name = str(value.get("name") or "").strip()
                if not name or name in seen_names:
                    raise ValueError(
                        "animation state names must be non-empty and unique"
                    )
                seen_names.add(name)
                clip_target, guid, _clip = _resolve_animation_clip2d_reference(
                    project_path,
                    clip_path=str(value.get("clip_path") or ""),
                    clip_guid=str(value.get("clip_guid") or ""),
                )
                state = fsm.add_state(name)
                state.clip_guid = guid
                state.clip_path = relative_path(clip_target, project_path)
                state.loop = bool(value.get("loop", True))
                state.speed = float(value.get("speed", 1.0))
                position = value.get("position", [160.0 + index * 220.0, 80.0])
                if type(position) is not list or len(position) != 2:
                    raise ValueError(
                        "animation state position must contain two numbers"
                    )
                state.position = [float(position[0]), float(position[1])]

            selected_default = str(default_state or fsm.states[0].name).strip()
            if selected_default not in seen_names:
                raise ValueError("default_state must reference a declared state")
            fsm.default_state = selected_default
            result = _create_editor_model_asset(
                project_path,
                target,
                fsm,
                overwrite=bool(overwrite),
                description="Create 2D Animation State Machine",
            )
            result.update(
                _fsm_summary(fsm, relative_path(target, project_path))
            )
            return result

        return main_thread(
            "animation_fsm2d_create",
            _create,
            arguments={
                "path": path,
                "state_count": len(states),
                "default_state": default_state,
                "overwrite": overwrite,
            },
        )

    @mcp.tool(name="timeline_fsm_create")
    def timeline_fsm_create(
        path: str,
        state_name: str = "Timeline",
        timeline_path: str = "",
        overwrite: bool = False,
    ) -> dict:
        """Create a timeline-mode .timelinefsm using AnimStateMachine.save()."""
        def _create():
            from Infernux.core.anim_state_machine import AnimStateMachine
            target = resolve_asset_path(project_path, path)
            if not target.lower().endswith(".timelinefsm"):
                raise ValueError("path must end with .timelinefsm")
            require_existing_parent_directory(project_path, target)
            if os.path.exists(target) and not overwrite:
                raise FileExistsError(f"Asset already exists: {path}")
            fsm = AnimStateMachine(
                name=os.path.splitext(os.path.basename(target))[0],
                mode="timeline",
            )
            state = fsm.add_state(str(state_name or "Timeline"))
            if timeline_path:
                timeline = _resolve_timeline_path(project_path, timeline_path)
                state.kind = "timeline"
                state.timeline_path = timeline
                state.timeline_guid = _asset_guid(timeline)
            _save_model_asset(fsm, target, action="modified" if os.path.exists(target) else "created")
            return _fsm_summary(fsm, target)
        return main_thread("timeline_fsm_create", _create, arguments={"path": path, "state_name": state_name, "timeline_path": timeline_path, "overwrite": overwrite})

    @mcp.tool(name="timeline_fsm_add_state")
    def timeline_fsm_add_state(path: str, state_name: str, timeline_path: str = "") -> dict:
        """Add a timeline state and save the public FSM model."""
        def _edit():
            from Infernux.core.anim_state_machine import AnimStateMachine
            target = resolve_asset_path(project_path, path)
            if not target.lower().endswith(".timelinefsm"):
                raise ValueError("path must end with .timelinefsm")
            fsm = AnimStateMachine.load(target)
            if fsm is None:
                raise FileNotFoundError(f"Timeline FSM could not be loaded: {path}")
            if fsm.mode != "timeline":
                raise ValueError("The state machine must use timeline mode")
            state = fsm.add_state(str(state_name))
            if timeline_path:
                timeline = _resolve_timeline_path(project_path, timeline_path)
                state.kind = "timeline"
                state.timeline_path = timeline
                state.timeline_guid = _asset_guid(timeline)
            _save_model_asset(fsm, target, action="modified")
            return _fsm_summary(fsm, target)
        return main_thread("timeline_fsm_add_state", _edit, arguments={"path": path, "state_name": state_name, "timeline_path": timeline_path})

    @mcp.tool(name="animation_clip3d_create_from_model")
    def animation_clip3d_create_from_model(
        path: str,
        take_name: str,
        model_path: str = "",
        model_guid: str = "",
        overwrite: bool = False,
    ) -> dict:
        """Create a .animclip3d that points at one imported model animation take."""
        def _create():
            from Infernux.core.animation_clip3d import AnimationClip3D

            target = resolve_asset_path(project_path, path)
            if not target.lower().endswith(".animclip3d"):
                raise ValueError("path must end with .animclip3d")
            require_existing_parent_directory(project_path, target)
            if os.path.exists(target) and not overwrite:
                raise FileExistsError(f"Asset already exists: {path}")
            source = _resolve_imported_animated_model(
                project_path,
                model_path=model_path,
                model_guid=model_guid,
                take_name=take_name,
            )
            clip = AnimationClip3D(
                name=os.path.splitext(os.path.basename(target))[0],
                source_model_guid=source["guid"],
                source_model_path=source["path"],
                take_name=source["take_name"],
                bind_pose_bone_names=source["bind_pose_bone_names"],
            )
            result = _create_editor_model_asset(
                project_path,
                target,
                clip,
                overwrite=bool(overwrite),
                description="Create AnimationClip3D",
            )
            result.update({
                "source_model_guid": source["guid"],
                "source_model_path": relative_path(source["path"], project_path),
                "take_name": source["take_name"],
            })
            return result

        return main_thread(
            "animation_clip3d_create_from_model",
            _create,
            arguments={
                "path": path,
                "take_name": take_name,
                "model_path": model_path,
                "model_guid": model_guid,
                "overwrite": overwrite,
            },
        )

    @mcp.tool(name="animation_fsm3d_create")
    def animation_fsm3d_create(
        path: str,
        clip_path: str = "",
        clip_guid: str = "",
        state_name: str = "Default",
        overwrite: bool = False,
    ) -> dict:
        """Create a 3D .animfsm and optionally bind one looping default state."""
        def _create():
            from Infernux.core.anim_state_machine import AnimStateMachine

            target = resolve_asset_path(project_path, path)
            if not target.lower().endswith(".animfsm"):
                raise ValueError("path must end with .animfsm")
            require_existing_parent_directory(project_path, target)
            if os.path.exists(target) and not overwrite:
                raise FileExistsError(f"Asset already exists: {path}")

            fsm = AnimStateMachine(
                name=os.path.splitext(os.path.basename(target))[0],
                mode="3d",
            )
            if clip_path or clip_guid:
                clip_target, guid, _clip = _resolve_animation_clip_reference(
                    project_path,
                    clip_path=clip_path,
                    clip_guid=clip_guid,
                )
                name = str(state_name or "Default").strip()
                if not name:
                    raise ValueError("state_name must not be empty")
                state = fsm.add_state(name)
                state.kind = "clip"
                state.clip_guid = guid
                state.clip_path = clip_target
                state.loop = True
                state.restart_same_clip = False

            result = _create_editor_model_asset(
                project_path,
                target,
                fsm,
                overwrite=bool(overwrite),
                description="Create 3D Animation State Machine",
            )
            result.update({
                "mode": fsm.mode,
                "default_state": fsm.default_state,
                "state_count": fsm.state_count,
                "states": [
                    {
                        "stable_id": state.stable_id,
                        "name": state.name,
                        "kind": state.kind,
                        "clip_guid": state.clip_guid,
                        "clip_path": relative_path(state.clip_path, project_path) if state.clip_path else "",
                        "loop": bool(state.loop),
                    }
                    for state in fsm.states
                ],
            })
            return result

        return main_thread(
            "animation_fsm3d_create",
            _create,
            arguments={
                "path": path,
                "clip_path": clip_path,
                "clip_guid": clip_guid,
                "state_name": state_name,
                "overwrite": overwrite,
            },
        )
