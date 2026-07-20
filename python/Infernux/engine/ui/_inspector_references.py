"""Asset-reference fields, pickers, object fields and serializable-object
rendering helpers for the Inspector component renderers."""

from Infernux.lib import InxGUIContext
from Infernux.engine.i18n import t
from .inspector_utils import (
    max_label_w, field_label, render_serialized_field, has_field_changed,
    render_compact_section_header, render_info_text, pretty_field_name,
    semantic_capture_enabled, inspector_component_semantic_id,
)
from ._inspector_undo import (
    _record_property, _record_builtin_property, _notify_scene_modified,
)


# ── Tooltip / info-text helper ──


def _tooltip_and_info(ctx, metadata):
    """Show tooltip on hover and info text below the field if available."""
    if metadata.tooltip and ctx.is_item_hovered():
        ctx.set_tooltip(metadata.tooltip)
    if metadata.info_text:
        render_info_text(ctx, metadata.info_text)


# ── GUID / path resolution ──


def _asset_guid_from_path(file_path: str) -> str:
    from Infernux.debug import Debug
    from Infernux.core.asset_types import read_meta_guid
    from Infernux.core.assets import AssetManager

    guid = ""
    adb = getattr(AssetManager, '_asset_database', None)
    if adb:
        try:
            guid = adb.get_guid_from_path(file_path) or ""
        except RuntimeError as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
    # AssetDatabase paths may be project-relative or canonical absolute paths,
    # depending on the caller. The adjacent current-format meta remains the
    # source of truth when a path lookup has not produced an identity.
    return guid or read_meta_guid(file_path)


def _resolve_guid_and_path(payload: str):
    """Resolve a string payload to (guid, path_hint).

    If *payload* is an existing file path, the GUID is looked up from the
    asset database.  Otherwise *payload* is treated as a GUID and the path
    is resolved in reverse.
    """
    from Infernux.debug import Debug
    import os
    guid = ""
    path_hint = ""
    if os.path.isfile(payload):
        path_hint = payload
        guid = _asset_guid_from_path(payload)
    else:
        guid = payload
        try:
            from Infernux.core.assets import AssetManager
            adb = getattr(AssetManager, '_asset_database', None)
            if adb:
                path_hint = adb.get_path_from_guid(guid) or ""
        except Exception as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            pass
    return guid, path_hint


# ── Reference value creation ──


def _create_reference_value_from_payload(element_type, payload, required_component: str = None, metadata=None):
    from Infernux.components.serialized_field import FieldType

    if element_type == FieldType.GAME_OBJECT:
        # String payload = prefab drag (GUID or file path)
        if isinstance(payload, str):
            from Infernux.components.ref_wrappers import PrefabRef
            guid, path_hint = _resolve_guid_and_path(payload)
            return PrefabRef(guid=guid, path_hint=path_hint)

        # Int payload = scene hierarchy drag
        from Infernux.lib import SceneManager as _SM
        from Infernux.components.ref_wrappers import GameObjectRef

        scene = _SM.instance().get_active_scene()
        if scene is None:
            return None

        obj_id = int(payload) if not isinstance(payload, int) else payload
        game_object = scene.find_by_id(obj_id)
        if game_object is None:
            return None
        if required_component and not _game_object_has_required_component(game_object, required_component):
            return None
        return GameObjectRef(game_object)

    file_path = str(payload) if not isinstance(payload, str) else payload

    if element_type == FieldType.MATERIAL:
        from Infernux.core.material import Material
        from Infernux.components.ref_wrappers import MaterialRef

        mat = Material.load(file_path)
        if mat is None:
            return None
        return MaterialRef(mat, path_hint=file_path)

    if element_type == FieldType.TEXTURE:
        from Infernux.core.asset_ref import TextureRef
        return TextureRef(guid=_asset_guid_from_path(file_path), path_hint=file_path)

    if element_type == FieldType.SHADER:
        from Infernux.core.asset_ref import ShaderRef
        return ShaderRef(guid=_asset_guid_from_path(file_path), path_hint=file_path)

    if element_type == FieldType.ASSET:
        guid = _asset_guid_from_path(file_path)
        asset_type = getattr(metadata, "asset_type", None) if metadata else None
        if asset_type:
            from Infernux.core.asset_ref import get_asset_type_config
            cfg = get_asset_type_config(asset_type)
            if cfg:
                return cfg["ref_class"](guid=guid, path_hint=file_path)
        from Infernux.core.asset_ref import AudioClipRef
        return AudioClipRef(guid=guid, path_hint=file_path)

    if element_type == FieldType.COMPONENT:
        from Infernux.lib import SceneManager as _SM
        from Infernux.components.ref_wrappers import ComponentRef

        scene = _SM.instance().get_active_scene()
        if scene is None:
            return None
        obj_id = int(payload) if not isinstance(payload, int) else payload
        game_object = scene.find_by_id(obj_id)
        if game_object is None:
            return None

        comp_type = required_component or ''
        if comp_type:
            if not _game_object_has_required_component(game_object, comp_type):
                from Infernux.debug import Debug
                Debug.log_warning(
                    f"GameObject '{game_object.name}' has no '{comp_type}' component."
                )
                return None
        else:
            # No type filter — pick the first Python component on this GO
            from Infernux.components.component import InxComponent
            py_comps = InxComponent._active_instances.get(obj_id, [])
            if py_comps:
                comp_type = py_comps[0].__class__.__name__

        return ComponentRef(go_id=obj_id, component_type=comp_type)

    return None


def _get_reference_display_name(element_type, value) -> str:
    from Infernux.components.serialized_field import FieldType

    if value is None:
        return "None"

    if element_type == FieldType.COMPONENT:
        if hasattr(value, 'display_name'):
            return value.display_name
        return "None"

    if element_type == FieldType.GAME_OBJECT:
        from Infernux.components.ref_wrappers import PrefabRef
        if isinstance(value, PrefabRef):
            return value.name
        obj = value.resolve() if hasattr(value, 'resolve') else value
        return obj.name if obj and hasattr(obj, 'name') else "None"

    if hasattr(value, 'display_name'):
        return value.display_name

    if element_type == FieldType.MATERIAL:
        mat = value.resolve() if hasattr(value, 'resolve') else value
        return mat.name if mat and hasattr(mat, 'name') else "None"

    if element_type == FieldType.SHADER:
        resolved = value.resolve() if hasattr(value, 'resolve') else value
        if resolved and hasattr(resolved, 'source_path'):
            return resolved.source_path

    if hasattr(value, 'name'):
        return value.name or "None"

    return str(value)


# ── Serializable-object field rendering ──


def _render_serializable_object_field(
    ctx: InxGUIContext, comp, field_name: str, metadata, current_value, lw: float,
):
    """Render a SerializableObject field as an inline collapsible section."""
    import copy as _copy
    from Infernux.components.serialized_field import get_serialized_fields, FieldType

    so_class = type(current_value) if current_value is not None else getattr(metadata, 'serializable_class', None)
    if so_class is None:
        field_label(ctx, pretty_field_name(field_name), lw)
        ctx.label(t("inspector.unknown_type"))
        return

    header = f"{pretty_field_name(field_name)} ({so_class.__name__})"
    if not render_compact_section_header(ctx, header, level="secondary"):
        return

    so_fields = get_serialized_fields(so_class)
    so_lw = max_label_w(ctx, [pretty_field_name(k) for k in so_fields]) if so_fields else 0.0

    if current_value is None:
        current_value = so_class()
        _record_property(comp, field_name, None, current_value, f"Init {field_name}")

    changes: dict = {}
    for so_fn, so_meta in so_fields.items():
        so_val = getattr(current_value, so_fn, so_meta.default)

        if so_meta.field_type == FieldType.SERIALIZABLE_OBJECT:
            _render_nested_so(ctx, field_name, so_fn, so_meta, so_val, so_lw, changes)
        else:
            new_val = render_serialized_field(
                ctx, f"##{field_name}_{so_fn}", pretty_field_name(so_fn), so_meta, so_val, so_lw,
            )
            if has_field_changed(so_meta.field_type, so_val, new_val):
                changes[so_fn] = new_val

    if changes and not metadata.readonly:
        edited = _copy.deepcopy(current_value)
        for fn, fv in changes.items():
            setattr(edited, fn, fv)
        _record_property(comp, field_name, current_value, edited, f"Set {field_name}")
        if hasattr(comp, '_call_on_validate'):
            comp._call_on_validate()


def _render_nested_so(
    ctx: InxGUIContext, parent_id: str, so_fn: str, so_meta, so_val, so_lw: float,
    changes: dict,
):
    """Render a nested SerializableObject sub-field and collect changes."""
    import copy as _copy
    from Infernux.components.serialized_field import get_serialized_fields, FieldType

    so_class = type(so_val) if so_val is not None else getattr(so_meta, 'serializable_class', None)
    if so_class is None:
        ctx.label(f"{so_fn}: " + t("inspector.unknown_type"))
        return

    header = f"{pretty_field_name(so_fn)} ({so_class.__name__})"
    if not render_compact_section_header(ctx, header, level="tertiary"):
        return

    inner_fields = get_serialized_fields(so_class)
    inner_lw = max_label_w(ctx, [pretty_field_name(k) for k in inner_fields]) if inner_fields else 0.0

    if so_val is None:
        so_val = so_class()
        changes[so_fn] = so_val
        return

    inner_changes: dict = {}
    for ifn, imeta in inner_fields.items():
        ival = getattr(so_val, ifn, imeta.default)
        new_val = render_serialized_field(
            ctx, f"##{parent_id}_{so_fn}_{ifn}", pretty_field_name(ifn), imeta, ival, inner_lw,
        )
        if has_field_changed(imeta.field_type, ival, new_val):
            inner_changes[ifn] = new_val

    if inner_changes:
        edited = _copy.deepcopy(so_val)
        for fn, fv in inner_changes.items():
            setattr(edited, fn, fv)
        changes[so_fn] = edited


# ── Asset-type reference field configuration ──
_ASSET_REF_CONFIG = None  # lazy-initialized (needs FieldType import)


def _get_asset_ref_config():
    """Return the base config for the built-in reference FieldTypes.

    ``FieldType.ASSET`` entries are resolved dynamically via the
    ``asset_type`` metadata and the registry in ``asset_ref.py``.
    """
    global _ASSET_REF_CONFIG
    if _ASSET_REF_CONFIG is None:
        from Infernux.components.serialized_field import FieldType
        _ASSET_REF_CONFIG = {
            FieldType.MATERIAL: ("Material",  "MATERIAL_FILE", ("*.mat",),            "mat"),
            FieldType.TEXTURE:  (
                "Texture",
                "TEXTURE_FILE",
                ("*.png", "*.jpg", "*.jpeg", "*.bmp", "*.tga", "*.gif", "*.psd", "*.hdr", "*.pic", "*.pnm", "*.pgm", "*.ppm"),
                "tex",
            ),
            FieldType.SHADER:   ("Shader",    "SHADER_FILE",   ("*.vert", "*.frag"),   "shd"),
            # ASSET is kept as a fallback; _resolve_asset_config overrides it.
            FieldType.ASSET:    ("AudioClip", "AUDIO_FILE",    ("*.wav", "*.mp3", "*.ogg"), "aud"),
        }
    return _ASSET_REF_CONFIG


def _resolve_asset_config(metadata):
    """Return (display, drag_type, extensions, prefix) for an ASSET field,
    using the registry when *metadata.asset_type* is set."""
    asset_type = getattr(metadata, "asset_type", None) if metadata else None
    if asset_type:
        from Infernux.core.asset_ref import get_asset_type_config
        cfg = get_asset_type_config(asset_type)
        if cfg:
            return (cfg["display"], cfg["drag_type"],
                    cfg["extensions"], cfg["prefix"])
    # Fallback to default ASSET entry
    from Infernux.components.serialized_field import FieldType
    return _get_asset_ref_config()[FieldType.ASSET]


def _create_asset_ref_from_payload(metadata, file_path: str):
    """Create the correct AssetRefBase subclass from a file path,
    using *metadata.asset_type* to select the right ref class."""
    asset_type = getattr(metadata, "asset_type", None) if metadata else None
    guid = _asset_guid_from_path(file_path)
    if asset_type:
        from Infernux.core.asset_ref import get_asset_type_config
        cfg = get_asset_type_config(asset_type)
        if cfg:
            return cfg["ref_class"](guid=guid, path_hint=file_path)
    # Fallback
    from Infernux.core.asset_ref import AudioClipRef
    return AudioClipRef(guid=guid, path_hint=file_path)


def _render_asset_reference_field(
    ctx, comp, field_name, metadata, current_value, field_type, lw,
    *, builtin_attr=None,
):
    """Render a MATERIAL / TEXTURE / SHADER / ASSET reference field."""
    from Infernux.components.serialized_field import FieldType as _FT

    # For ASSET fields, resolve config from registry using metadata.asset_type
    if field_type == _FT.ASSET:
        type_hint, drag_type, globs, prefix = _resolve_asset_config(metadata)
    else:
        type_hint, drag_type, globs, prefix = _get_asset_ref_config()[field_type]

    display = _get_reference_display_name(field_type, current_value)

    def _record_change(old, new, description):
        if builtin_attr is not None:
            _record_builtin_property(comp, builtin_attr, old, new, description)
        else:
            _record_property(comp, field_name, old, new, description)

    def _on_pick(path, _fn=field_name, _comp=comp, _ft=field_type, _meta=metadata):
        if _ft == _FT.ASSET:
            ref = _create_asset_ref_from_payload(_meta, path)
        else:
            ref = _create_reference_value_from_payload(_ft, path)
        if ref is not None:
            old = getattr(_comp, builtin_attr or _fn, None)
            _record_change(old, ref, f"Set {_fn}")

    def _on_clear(_fn=field_name, _comp=comp):
        old = getattr(_comp, builtin_attr or _fn, None)
        _record_change(old, None, f"Clear {_fn}")

    _assets_only = (field_type == _FT.TEXTURE)

    def _picker(filt):
        result = []
        for g in globs:
            result += _picker_assets(filt, g, assets_only=_assets_only)
        return result

    def _on_drop(payload, _fn=field_name, _comp=comp, _ft=field_type, _meta=metadata):
        if _ft == _FT.ASSET:
            file_path = str(payload) if not isinstance(payload, str) else payload
            ref = _create_asset_ref_from_payload(_meta, file_path)
            if ref is not None:
                old = getattr(_comp, builtin_attr or _fn, None)
                _record_change(old, ref, f"Set {_fn}")
        else:
            _apply_reference_drop(_ft, _comp, _fn, payload)

    field_label(ctx, pretty_field_name(field_name), lw)
    render_object_field(
        ctx, f"{prefix}_ref_{field_name}", display, type_hint,
        accept_drag_type=drag_type,
        on_drop_callback=_on_drop,
        picker_asset_items=_picker,
        on_pick=_on_pick,
        on_clear=_on_clear,
        semantic_id=(inspector_component_semantic_id(comp, field_name)
                     if semantic_capture_enabled(ctx) else ""),
    )


def _render_component_ref_inline(ctx, py_comp, field_name, metadata, lw):
    """Render a FieldType.COMPONENT reference field."""
    from Infernux.components.ref_wrappers import ComponentRef
    from Infernux.components.serialized_field import get_raw_field_value, FieldType
    _comp_ref = get_raw_field_value(py_comp, field_name)
    if not isinstance(_comp_ref, ComponentRef):
        _comp_ref = ComponentRef()
    _display = _comp_ref.display_name
    _type_hint = metadata.component_type or "Component"
    _ct = metadata.component_type

    def _comp_scene(filt, _ct=_ct):
        return _picker_scene_gameobjects(filt, required_component=_ct)

    def _comp_on_pick(go, _fn=field_name, _comp=py_comp, _ct=_ct):
        ref = _create_component_ref_from_go(go, _ct)
        if ref is not None:
            old = get_raw_field_value(_comp, _fn)
            _record_property(_comp, _fn, old, ref, f"Set {_fn}")

    def _comp_on_clear(_fn=field_name, _comp=py_comp):
        old = get_raw_field_value(_comp, _fn)
        _record_property(_comp, _fn, old, ComponentRef(), f"Clear {_fn}")

    field_label(ctx, pretty_field_name(field_name), lw)
    render_object_field(
        ctx, f"comp_ref_{field_name}", _display, _type_hint,
        accept_drag_type=["HIERARCHY_GAMEOBJECT", "PREFAB_GUID", "PREFAB_FILE"],
        on_drop_callback=lambda payload, _fn=field_name, _comp=py_comp, _ct=metadata.component_type: _apply_reference_drop(FieldType.COMPONENT, _comp, _fn, payload, _ct),
        picker_scene_items=_comp_scene,
        on_pick=_comp_on_pick,
        on_clear=_comp_on_clear,
        semantic_id=(inspector_component_semantic_id(py_comp, field_name)
                     if semantic_capture_enabled(ctx) else ""),
    )


def _render_gameobject_ref_inline(ctx, py_comp, field_name, metadata, current_value, lw):
    """Render a FieldType.GAME_OBJECT reference field."""
    from Infernux.components.ref_wrappers import PrefabRef, GameObjectRef
    if isinstance(current_value, PrefabRef):
        display = current_value.name
        _type_hint_prefix = "Prefab"
    else:
        _display_obj = current_value
        if hasattr(current_value, 'resolve'):
            _display_obj = current_value.resolve()
        display = _display_obj.name if _display_obj and hasattr(_display_obj, 'name') else "None"
        _type_hint_prefix = "GameObject"
    _type_hint = _type_hint_prefix
    _req_comp = metadata.required_component
    if _req_comp:
        _type_hint = f"{_type_hint_prefix}:{_req_comp}"

    def _go_scene(filt, _rc=_req_comp):
        return _picker_scene_gameobjects(filt, required_component=_rc)

    def _go_on_pick(go, _fn=field_name, _comp=py_comp):
        ref = GameObjectRef(go)
        old = getattr(_comp, _fn, None)
        _record_property(_comp, _fn, old, ref, f"Set {_fn}")

    def _go_on_clear(_fn=field_name, _comp=py_comp):
        old = getattr(_comp, _fn, None)
        _record_property(_comp, _fn, old, None, f"Clear {_fn}")

    field_label(ctx, pretty_field_name(field_name), lw)
    render_object_field(
        ctx, f"go_ref_{field_name}", display, _type_hint,
        accept_drag_type=["HIERARCHY_GAMEOBJECT", "PREFAB_GUID", "PREFAB_FILE"],
        on_drop_callback=lambda payload, _fn=field_name, _comp=py_comp, _rc=_req_comp: _apply_gameobject_or_prefab_drop(_comp, _fn, payload, _rc),
        picker_scene_items=_go_scene,
        on_pick=_go_on_pick,
        on_clear=_go_on_clear,
        semantic_id=(inspector_component_semantic_id(py_comp, field_name)
                     if semantic_capture_enabled(ctx) else ""),
    )


# ── Drop handlers ──


def _apply_reference_drop(field_type, comp, field_name: str, payload, required_component: str = None):
    """Generic handler for reference-type drag-drop onto a field."""
    try:
        ref = _create_reference_value_from_payload(field_type, payload, required_component)
        if ref is None:
            return
        from Infernux.components.serialized_field import FieldType
        if field_type == FieldType.COMPONENT:
            from Infernux.components.serialized_field import get_raw_field_value
            old_val = get_raw_field_value(comp, field_name)
        else:
            old_val = getattr(comp, field_name, None)
        _record_property(comp, field_name, old_val, ref, f"Set {field_name}")
    except Exception as e:
        from Infernux.debug import Debug
        Debug.log_error(f"Reference drop failed for {field_name}: {e}")


def _apply_gameobject_or_prefab_drop(comp, field_name: str, payload, required_component: str = None):
    """Handle a HIERARCHY_GAMEOBJECT or PREFAB drag-drop onto a GAME_OBJECT field."""
    if isinstance(payload, str):
        try:
            from Infernux.components.ref_wrappers import PrefabRef
            guid, path_hint = _resolve_guid_and_path(payload)
            ref = PrefabRef(guid=guid, path_hint=path_hint)
            old_val = getattr(comp, field_name, None)
            _record_property(comp, field_name, old_val, ref, f"Set {field_name}")
        except Exception as e:
            from Infernux.debug import Debug
            Debug.log_error(f"Prefab drop failed: {e}")
    else:
        from Infernux.components.serialized_field import FieldType
        _apply_reference_drop(FieldType.GAME_OBJECT, comp, field_name, payload, required_component)


def _apply_builtin_audio_clip_drop(comp, cpp_attr: str, payload):
    """Handle an AUDIO_FILE drag-drop onto a built-in component AudioClip field."""
    try:
        file_path = str(payload) if not isinstance(payload, str) else payload
        from Infernux.core.audio_clip import AudioClip as PyAudioClip

        clip = PyAudioClip.load(file_path)
        if clip is None:
            return

        old_val = getattr(comp, cpp_attr)
        _record_builtin_property(comp, cpp_attr, old_val, clip.native, f"Set {cpp_attr}")
    except Exception as e:
        from Infernux.debug import Debug
        Debug.log_error(f"Audio clip drop failed: {e}")


# ── Picker item providers ──


def _game_object_has_required_component(game_object, required_component: str) -> bool:
    if game_object is None or not required_component:
        return False

    from Infernux.components.ref_wrappers import _resolve_component_on_game_object
    return _resolve_component_on_game_object(game_object, required_component) is not None


def _create_component_ref_from_go(game_object, component_type: str = ""):
    """Create a ComponentRef from a picked GameObject (for picker popup)."""
    from Infernux.components.ref_wrappers import ComponentRef, _infer_component_type_on_game_object
    if game_object is None:
        return None
    go_id = game_object.id
    ct = component_type or ''
    if ct:
        if not _game_object_has_required_component(game_object, ct):
            return None
    else:
        ct = _infer_component_type_on_game_object(game_object)
    return ComponentRef(go_id=go_id, component_type=ct)


def _picker_scene_gameobjects(filter_text: str, required_component: str = None):
    """Return ``[(name, go), ...]`` for all scene GameObjects matching *filter_text*."""
    from Infernux.lib import SceneManager
    scene = SceneManager.instance().get_active_scene()
    if not scene:
        return []
    items = []
    filt = filter_text.lower()
    for go in scene.get_all_objects():
        if filt and filt not in go.name.lower():
            continue
        if required_component and not _game_object_has_required_component(go, required_component):
            continue
        items.append((go.name, go))
    return items


def _picker_assets(filter_text: str, pattern: str, *, assets_only: bool = False):
    """Return ``[(display_name, path), ...]`` for assets matching *pattern*."""
    import os
    from Infernux.core.assets import AssetManager
    paths = AssetManager.find_assets(pattern)
    items = []
    filt = filter_text.lower()
    for p in paths:
        if assets_only:
            norm = p.replace("\\", "/")
            if "/Assets/" not in norm and not norm.startswith("Assets/"):
                continue
        name = os.path.basename(p)
        if filt and filt not in name.lower():
            continue
        items.append((name, p))
    return items


# ── Object field wrapper ──


def render_object_field(ctx: InxGUIContext, field_id: str, display_text: str,
                        type_hint: str, selected: bool = False, clickable: bool = True,
                        accept_drag_type: str = None, on_drop_callback=None,
                        picker_scene_items=None, picker_asset_items=None,
                        on_pick=None, on_clear=None, semantic_id: str = "") -> bool:
    """Render a Unity-style object field (selectable box showing an object reference)."""
    from .igui import IGUI
    return IGUI.object_field(
        ctx, field_id, display_text, type_hint,
        selected=selected, clickable=clickable,
        accept=accept_drag_type, on_drop=on_drop_callback,
        picker_scene_items=picker_scene_items,
        picker_asset_items=picker_asset_items,
        on_pick=on_pick, on_clear=on_clear,
        semantic_id=semantic_id,
    )
