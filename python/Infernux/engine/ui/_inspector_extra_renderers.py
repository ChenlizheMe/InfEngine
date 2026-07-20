"""Extra Inspector renderers for specific component types (AudioSource, MeshRenderer)."""

import os

from Infernux.debug import Debug
from Infernux.engine.path_utils import path_key
from Infernux.lib import InxGUIContext
from Infernux.engine.i18n import t
from .inspector_utils import (
    max_label_w,
    field_label,
    float_close as _float_close,
    semantic_capture_enabled,
    inspector_component_semantic_id,
)
from .theme import Theme, ImGuiCol
from ._inspector_undo import (
    _notify_scene_modified, _record_track_volume, _record_material_slot,
    _record_generic_component,
)
from ._inspector_references import (
    _picker_assets, render_object_field,
)


# ============================================================================
# AudioSource extra renderer (per-track section only)
# ============================================================================


def _render_audio_source_extra(ctx: InxGUIContext, comp):
    """Extra Inspector section for AudioSource: per-track clip & volume.

    Source-level properties (volume, pitch, mute, spatial, etc.) are handled
    by the generic CppProperty renderer.  This function only renders the
    dynamic per-track section that cannot be expressed as CppProperty.
    """
    from Infernux.engine.play_mode import PlayModeManager, PlayModeState

    track_count = comp.track_count

    ctx.separator()
    ctx.label("Tracks")

    track_labels = ["Clip", "Volume"]
    track_lw = max_label_w(ctx, track_labels)

    for i in range(track_count):
        ctx.set_next_item_open(True)
        if ctx.collapsing_header(f"Track {i}"):
            # Track clip
            clip = comp.get_track_clip(i)
            clip_name = "None"
            if clip is not None:
                try:
                    clip_name = clip.name or "None"
                except (RuntimeError, AttributeError):
                    clip_name = "None"

            field_label(ctx, "Clip", track_lw)
            render_object_field(
                ctx,
                f"audio_track_clip_{i}",
                clip_name,
                "AudioClip",
                accept_drag_type="AUDIO_FILE",
                on_drop_callback=lambda payload, _c=comp, _i=i: _apply_track_audio_clip_drop(_c, _i, payload),
                picker_asset_items=_audio_clip_picker_items,
                on_pick=lambda path, _c=comp, _i=i: _apply_track_audio_clip_pick(_c, _i, path),
                on_clear=lambda _c=comp, _i=i: _clear_track_audio_clip(_c, _i),
                semantic_id=(inspector_component_semantic_id(comp, f"track_{i}.clip")
                             if semantic_capture_enabled(ctx) else ""),
            )

            # Track volume
            tv = comp.get_track_volume(i)
            field_label(ctx, "Volume", track_lw)
            new_tv = ctx.float_slider(f"##track_vol_{i}", float(tv), 0.0, 1.0)
            if not _float_close(float(new_tv), float(tv)):
                comp.set_track_volume(i, float(new_tv))
                _record_track_volume(comp, i, float(tv), float(new_tv))

            # Play / Stop buttons (only in play mode for feedback)
            pm = PlayModeManager.instance()
            if pm and pm.state != PlayModeState.EDIT:
                is_playing = comp.is_track_playing(i)
                if is_playing:
                    if ctx.button(f"Stop##track_stop_{i}"):
                        comp.stop(i)
                else:
                    if ctx.button(f"Play##track_play_{i}"):
                        comp.play(i)
                ctx.same_line()
                status = "Playing" if is_playing else ("Paused" if comp.is_track_paused(i) else "Stopped")
                ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
                ctx.label(status)
                ctx.pop_style_color(1)


def _audio_clip_picker_items(filter_text: str):
    items = []
    for pattern in ("*.wav", "*.mp3", "*.ogg"):
        items += _picker_assets(filter_text, pattern)
    return items


def _record_audio_track_change(comp, old_document) -> None:
    new_document = comp.serialize_document()
    if new_document != old_document:
        _record_generic_component(comp, old_document, new_document)


def _apply_track_audio_clip_pick(comp, track_index: int, file_path) -> None:
    """Assign a registered audio asset selected by the Inspector picker."""
    try:
        file_path = str(file_path)
        old_document = comp.serialize_document()

        from Infernux.lib import AssetRegistry
        registry = AssetRegistry.instance()
        adb = registry.get_asset_database()
        guid = adb.get_guid_from_path(file_path) if adb else ""
        if not guid:
            Debug.log_warning(f"Audio clip is not registered: {file_path}")
            return
        comp.set_track_clip_by_guid(track_index, guid)
        _record_audio_track_change(comp, old_document)
    except Exception as e:
        Debug.log_error(f"Audio clip assignment failed: {e}")


def _clear_track_audio_clip(comp, track_index: int) -> None:
    try:
        old_document = comp.serialize_document()
        comp.set_track_clip_by_guid(track_index, "")
        _record_audio_track_change(comp, old_document)
    except Exception as e:
        Debug.log_error(f"Audio clip clear failed: {e}")


def _apply_track_audio_clip_drop(comp, track_index: int, payload):
    """Handle an AUDIO_FILE drag-drop onto a track clip field."""
    try:
        file_path = str(payload) if not isinstance(payload, str) else payload

        from Infernux.lib import AssetRegistry
        registry = AssetRegistry.instance()
        adb = registry.get_asset_database()
        if adb and adb.get_guid_from_path(file_path):
            _apply_track_audio_clip_pick(comp, track_index, file_path)
            return

        # Fallback: load from file path directly
        from Infernux.core.audio_clip import AudioClip as PyAudioClip

        clip = PyAudioClip.load(file_path)
        if clip is None:
            return

        old_document = comp.serialize_document()
        comp.set_track_clip(track_index, clip.native)
        _record_audio_track_change(comp, old_document)
    except Exception as e:
        Debug.log_error(f"Audio clip drop failed: {e}")


# ============================================================================
# MeshRenderer extra renderer (material slots)
# ============================================================================

_PRIMITIVE_MESH_ITEMS = (
    ("Cube", "Cube"),
    ("Sphere", "Sphere"),
    ("Capsule", "Capsule"),
    ("Cylinder", "Cylinder"),
    ("Plane", "Plane"),
    ("Quad", "Quad"),
)

_MODEL_ASSET_GLOBS = ("*.fbx", "*.obj", "*.gltf", "*.glb", "*.dae", "*.3ds", "*.ply", "*.stl")


def _mesh_display_name(comp) -> str:
    try:
        if comp.has_inline_mesh():
            inline_name = getattr(comp, 'inline_mesh_name', '') or ''
            return inline_name if inline_name else "Inline Mesh"
    except Exception as exc:
        Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")

    try:
        if getattr(comp, 'has_mesh_asset', False):
            mesh_name = getattr(comp, 'mesh_name', '') or ''
            if mesh_name:
                return mesh_name
            guid = getattr(comp, 'mesh_asset_guid', '') or ''
            path = _path_from_guid(guid)
            return os.path.basename(path) if path else (guid or "Mesh")
    except Exception as exc:
        Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")

    source_path = getattr(comp, 'source_model_path', '') or ''
    if source_path:
        return os.path.basename(source_path)
    return "None"


def _get_asset_database():
    try:
        from Infernux.lib import AssetRegistry
        registry = AssetRegistry.instance()
        if registry:
            return registry.get_asset_database()
    except Exception as exc:
        Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")
    try:
        from Infernux.core.assets import AssetManager
        return getattr(AssetManager, '_asset_database', None)
    except Exception as exc:
        Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")
    return None


def _path_from_guid(guid: str) -> str:
    if not guid:
        return ""
    adb = _get_asset_database()
    if not adb:
        return ""
    try:
        return adb.get_path_from_guid(guid) or ""
    except Exception as exc:
        Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")
        return ""


def _guid_and_path_from_model_payload(payload):
    if isinstance(payload, (tuple, list)) and len(payload) >= 2:
        payload = payload[1]
    ref = str(payload) if not isinstance(payload, str) else payload
    if not ref:
        return "", ""

    adb = _get_asset_database()
    if not adb:
        return "", ref

    try:
        path = adb.get_path_from_guid(ref) or ""
        if path:
            return ref, path
    except Exception as exc:
        Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")

    try:
        guid = adb.get_guid_from_path(ref) or ""
        return guid, ref
    except Exception as exc:
        Debug.log(f"[Suppressed] {type(exc).__name__}: {exc}")
    return "", ref


def _mesh_picker_items(filter_text: str):
    filt = (filter_text or "").lower()
    items = []

    for display, primitive_name in _PRIMITIVE_MESH_ITEMS:
        label = f"Primitive/{display}"
        if not filt or filt in display.lower() or filt in label.lower():
            items.append((label, ("primitive", primitive_name)))

    seen_paths = set()
    for pattern in _MODEL_ASSET_GLOBS:
        for name, path in _picker_assets(filter_text, pattern):
            norm = path_key(str(path))
            if norm in seen_paths:
                continue
            seen_paths.add(norm)
            items.append((f"Model/{name}", ("model", path)))
    return items


def _record_mesh_renderer_change(comp, old_document: dict, description: str) -> None:
    try:
        new_document = comp.serialize_document()
    except Exception as exc:
        Debug.log_warning(f"Mesh assignment could not be serialized: {exc}")
        _notify_scene_modified()
        return
    if new_document != old_document:
        _record_generic_component(comp, old_document, new_document)


def _assign_primitive_mesh(comp, primitive_name: str) -> None:
    try:
        from Infernux.lib import PrimitiveType
        primitive_type = getattr(PrimitiveType, primitive_name)
    except Exception as exc:
        Debug.log_warning(f"Unknown primitive mesh '{primitive_name}': {exc}")
        return

    old_document = comp.serialize_document()
    if getattr(comp, 'type_name', '') == 'SkinnedMeshRenderer':
        if hasattr(comp, 'set_source_model_guid'):
            comp.set_source_model_guid("")
        if hasattr(comp, 'set_source_model_path'):
            comp.set_source_model_path("")
    comp.set_primitive_mesh(primitive_type)
    _record_mesh_renderer_change(comp, old_document, f"Set Mesh {primitive_name}")


def _assign_model_mesh(comp, payload) -> None:
    guid, path = _guid_and_path_from_model_payload(payload)
    if not guid:
        Debug.log_warning(f"Mesh assignment failed: model is not registered ({path or payload})")
        return

    old_document = comp.serialize_document()
    if getattr(comp, 'type_name', '') == 'SkinnedMeshRenderer' and hasattr(comp, 'set_source_model_guid'):
        comp.set_source_model_guid(guid)
    elif hasattr(comp, 'set_mesh_asset_guid'):
        comp.set_mesh_asset_guid(guid)
    _record_mesh_renderer_change(comp, old_document, "Set Mesh")


def _clear_mesh(comp) -> None:
    old_document = comp.serialize_document()
    if getattr(comp, 'type_name', '') == 'SkinnedMeshRenderer':
        if hasattr(comp, 'set_source_model_guid'):
            comp.set_source_model_guid("")
        if hasattr(comp, 'set_source_model_path'):
            comp.set_source_model_path("")
    if hasattr(comp, 'clear_mesh_asset'):
        comp.clear_mesh_asset()
    _record_mesh_renderer_change(comp, old_document, "Clear Mesh")


def _apply_mesh_pick(comp, picked_value) -> None:
    if isinstance(picked_value, (tuple, list)) and len(picked_value) >= 2:
        kind = picked_value[0]
        if kind == "primitive":
            _assign_primitive_mesh(comp, str(picked_value[1]))
            return
        if kind == "model":
            _assign_model_mesh(comp, picked_value)
            return
    _assign_model_mesh(comp, picked_value)


def _set_material_slot_from_path(comp, slot_idx: int, material_path) -> None:
    from Infernux.lib import AssetRegistry

    adb = AssetRegistry.instance().get_asset_database()
    if not adb:
        return
    guid = adb.get_guid_from_path(str(material_path))
    if not guid:
        return
    guids = comp.get_material_guids()
    old_guid = guids[slot_idx] or "" if slot_idx < len(guids) else ""
    comp.set_material(slot_idx, guid)
    _record_material_slot(comp, slot_idx, old_guid, guid, f"Set Material Slot {slot_idx}")


def _clear_material_slot(comp, slot_idx: int) -> None:
    guids = comp.get_material_guids()
    old_guid = guids[slot_idx] or "" if slot_idx < len(guids) else ""
    comp.set_material(slot_idx, "")
    _record_material_slot(comp, slot_idx, old_guid, "", f"Clear Material Slot {slot_idx}")


def _render_mesh_renderer_materials(ctx: InxGUIContext, comp):
    """Render material slot fields after MeshRenderer CppProperty fields."""
    from Infernux.components.builtin_component import BuiltinComponent

    # Ensure we have the Python wrapper
    if not isinstance(comp, BuiltinComponent):
        wrapper_cls = BuiltinComponent._builtin_registry.get(getattr(comp, "type_name", "")) \
            or BuiltinComponent._builtin_registry.get("MeshRenderer")
        go = getattr(comp, 'game_object', None)
        if wrapper_cls and go is not None:
            comp = wrapper_cls._get_or_create_wrapper(comp, go)
        else:
            return

    ctx.separator()
    labels = [t("inspector.mesh"), "Materials", "Element 0"]
    lw = max_label_w(ctx, labels)

    mesh_field_id = f"mesh_field_{getattr(comp, 'component_id', id(comp))}"
    mesh_display = _mesh_display_name(comp)

    # Material slots
    mat_count = getattr(comp, 'material_count', 0) or 1
    material_guids = comp.get_material_guids() if hasattr(comp, 'get_material_guids') else []
    slot_names = comp.get_material_slot_names() if hasattr(comp, 'get_material_slot_names') else []

    slot_rows = []
    for slot_idx in range(mat_count):
        # Determine slot label
        if slot_idx < len(slot_names) and slot_names[slot_idx]:
            slot_label = f"{slot_names[slot_idx]} (Slot {slot_idx})"
        else:
            slot_label = f"Element {slot_idx}"

        # Determine display name
        mat = None
        try:
            mat = comp.get_effective_material(slot_idx)
        except (RuntimeError, IndexError) as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            pass
        mat_path = getattr(mat, "file_path", "") if mat else ""
        is_embedded = isinstance(mat_path, str) and "::submat:" in mat_path
        is_default = ((slot_idx >= len(material_guids)) or (not material_guids[slot_idx])) and not is_embedded
        mat_name = getattr(mat, 'name', 'None') if mat else 'None'
        display_name = mat_name + (" (Default)" if is_default else "")
        slot_rows.append((slot_label, display_name))

    native_batch = getattr(ctx, "render_mesh_renderer_inspector_fields", None)
    if callable(native_batch):
        from .editor_icons import EditorIcons
        from .igui import IGUI

        picker_texture = EditorIcons.get_cached(Theme.ICON_IMG_PICKER)
        interactions = native_batch(
            mesh_field_id,
            t("inspector.mesh"),
            mesh_display,
            [row[0] for row in slot_rows],
            [row[1] for row in slot_rows],
            int(picker_texture or 0),
            lw,
        )
        for interaction in interactions:
            slot_idx = int(interaction.get("index", -1))
            payload = interaction.get("payload", "")
            if payload:
                if slot_idx < 0:
                    _assign_model_mesh(comp, payload)
                else:
                    _set_material_slot_from_path(comp, slot_idx, payload)
            if not interaction.get("popup_open", False):
                continue
            field_id = mesh_field_id if slot_idx < 0 else f"mat_{slot_idx}"
            ctx.push_id_str(field_id)
            try:
                if slot_idx < 0:
                    IGUI._render_object_picker_popup(
                        ctx, field_id, None, _mesh_picker_items,
                        lambda picked, _comp=comp: _apply_mesh_pick(_comp, picked),
                        lambda _comp=comp: _clear_mesh(_comp),
                    )
                else:
                    IGUI._render_object_picker_popup(
                        ctx, field_id, None,
                        lambda filt: _picker_assets(filt, "*.mat"),
                        lambda picked, _comp=comp, _slot=slot_idx:
                            _set_material_slot_from_path(_comp, _slot, picked),
                        lambda _comp=comp, _slot=slot_idx:
                            _clear_material_slot(_comp, _slot),
                    )
            finally:
                ctx.pop_id()
        return

    field_label(ctx, t("inspector.mesh"), lw)
    render_object_field(
        ctx, mesh_field_id, mesh_display, "Mesh",
        clickable=False,
        accept_drag_type=["MODEL_GUID", "MODEL_FILE"],
        on_drop_callback=lambda payload, _comp=comp: _assign_model_mesh(_comp, payload),
        picker_asset_items=_mesh_picker_items,
        on_pick=lambda picked, _comp=comp: _apply_mesh_pick(_comp, picked),
        on_clear=lambda _comp=comp: _clear_mesh(_comp),
    )

    field_label(ctx, "Materials", lw)
    ctx.label(f"Size: {mat_count}")

    for slot_idx, (slot_label, display_name) in enumerate(slot_rows):

        def _make_on_drop(s, _comp=comp):
            def _on_drop(mat_path):
                _set_material_slot_from_path(_comp, s, mat_path)
            return _on_drop

        def _make_on_pick(s, _comp=comp):
            def _on_pick(picked_path):
                _make_on_drop(s, _comp)(str(picked_path))
            return _on_pick

        def _make_on_clear(s, _comp=comp):
            def _on_clear():
                _clear_material_slot(_comp, s)
            return _on_clear

        field_label(ctx, slot_label, lw)
        render_object_field(
            ctx, f"mat_{slot_idx}", display_name, "Material",
            clickable=False,
            accept_drag_type="MATERIAL_FILE",
            on_drop_callback=_make_on_drop(slot_idx),
            picker_asset_items=lambda filt: _picker_assets(filt, "*.mat"),
            on_pick=_make_on_pick(slot_idx),
            on_clear=_make_on_clear(slot_idx),
        )
