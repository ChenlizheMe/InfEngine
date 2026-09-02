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
    _record_property, _record_builtin_property,
)


# ── Tooltip / info-text helper ──


def _tooltip_and_info(ctx, metadata):
    """Show tooltip on hover and info text below the field if available."""
    if metadata.tooltip and ctx.is_item_hovered():
        ctx.set_tooltip(t(metadata.tooltip))
    if metadata.info_text:
        render_info_text(ctx, t(metadata.info_text))


# ── GUID / path resolution ──


def _portable_asset_path_hint(file_path: str) -> str:
    from Infernux.engine.path_utils import portable_path, relative_path
    from Infernux.engine.project_context import get_project_root

    path = _resolve_project_asset_path(file_path)
    if not path:
        return ""
    project_root = get_project_root()
    if project_root:
        try:
            return relative_path(path, project_root)
        except ValueError:
            pass
    return portable_path(path)


def _resolve_project_asset_path(file_path: str) -> str:
    """Resolve an asset path against the active project, never the process cwd."""
    import os
    from Infernux.engine.path_utils import resolved_path
    from Infernux.engine.project_context import get_project_root

    path = str(file_path or "").strip()
    if not path:
        return ""
    if not os.path.isabs(path):
        project_root = get_project_root()
        if project_root:
            path = os.path.join(project_root, path)
    return resolved_path(path)


def _is_project_asset_path(file_path: str) -> bool:
    """Return whether *file_path* belongs to the active project's Assets tree."""
    import os
    from Infernux.engine.path_utils import is_path_within
    from Infernux.engine.project_context import get_project_root

    project_root = get_project_root()
    if not project_root:
        return False
    return is_path_within(
        _resolve_project_asset_path(file_path),
        os.path.join(project_root, "Assets"),
    )


def _asset_guid_from_path(file_path: str) -> str:
    from Infernux.debug import Debug
    from Infernux.core.asset_types import read_meta_guid
    from Infernux.core.assets import AssetManager

    file_path = _resolve_project_asset_path(file_path)
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


def _resolve_guid_and_path(payload):
    """Resolve a picker, drag or clipboard payload to ``(guid, path_hint)``.

    Every asset assignment entry point uses this function so a canonical
    clipboard dictionary behaves exactly like a picker path or drag GUID.
    """
    from Infernux.debug import Debug
    import os
    if isinstance(payload, dict):
        guid = str(payload.get("guid") or "").strip()
        path_hint = str(payload.get("path_hint") or payload.get("path") or "").strip()
    else:
        guid = ""
        path_hint = str(payload or "").strip()
    try:
        from Infernux.core.assets import AssetManager

        adb = getattr(AssetManager, '_asset_database', None)
        if guid and not path_hint and adb:
            path_hint = str(adb.get_path_from_guid(guid) or "").strip()
        if path_hint:
            resolved = _resolve_project_asset_path(path_hint) or path_hint
            if os.path.isfile(resolved) or os.path.splitext(resolved)[1]:
                path_hint = _portable_asset_path_hint(resolved)
                guid = guid or _asset_guid_from_path(resolved)
            elif not guid and adb:
                reverse_path = str(adb.get_path_from_guid(path_hint) or "").strip()
                if reverse_path:
                    guid = path_hint
                    path_hint = _portable_asset_path_hint(reverse_path)
        if not guid and path_hint and adb:
            guid = str(adb.get_guid_from_path(path_hint) or "").strip()
    except Exception as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
    if not guid and path_hint and not os.path.splitext(path_hint)[1]:
        guid, path_hint = path_hint, ""
    return guid, path_hint


# ── Reference value creation ──


def _create_reference_value_from_payload(element_type, payload, required_component: str = None, metadata=None):
    from Infernux.components.fields import FieldType

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

    guid, file_path = _resolve_guid_and_path(payload)

    if element_type == FieldType.MATERIAL:
        from Infernux.core.material import Material
        from Infernux.components.ref_wrappers import MaterialRef

        mat = Material.load(file_path)
        if mat is None:
            return None
        return MaterialRef(mat, path_hint=_portable_asset_path_hint(file_path))

    if element_type == FieldType.TEXTURE:
        from Infernux.core.asset_ref import TextureRef
        return TextureRef(
            guid=guid,
            path_hint=_portable_asset_path_hint(file_path),
        )

    if element_type == FieldType.SHADER:
        from Infernux.core.asset_ref import ShaderRef
        return ShaderRef(
            guid=guid,
            path_hint=_portable_asset_path_hint(file_path),
        )

    if element_type == FieldType.ASSET:
        asset_type = str(
            getattr(metadata, "asset_type", "") if metadata else ""
        ).strip()
        if not asset_type:
            raise ValueError("ASSET reference payload requires an asset_type")
        from Infernux.core.asset_ref import create_asset_ref
        from Infernux.core.asset_reference_types import asset_type_registry

        descriptor = asset_type_registry.require(asset_type)
        return create_asset_ref(
            descriptor.type_id,
            guid=guid,
            path_hint=_portable_asset_path_hint(file_path),
        )

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
    from Infernux.components.fields import FieldType

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
    from Infernux.components.fields import get_serialized_fields, FieldType

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


def _render_nested_so(
    ctx: InxGUIContext, parent_id: str, so_fn: str, so_meta, so_val, so_lw: float,
    changes: dict,
):
    """Render a nested SerializableObject sub-field and collect changes."""
    import copy as _copy
    from Infernux.components.fields import get_serialized_fields, FieldType

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

    ``FieldType.ASSET`` entries are resolved dynamically from their mandatory
    ``asset_type`` metadata and are intentionally absent from this table.
    """
    global _ASSET_REF_CONFIG
    if _ASSET_REF_CONFIG is None:
        from Infernux.components.fields import FieldType
        from Infernux.core.asset_reference_types import asset_type_registry

        material = asset_type_registry.require("Material")
        texture = asset_type_registry.require("Texture")
        shader = asset_type_registry.require("Shader")
        _ASSET_REF_CONFIG = {
            FieldType.MATERIAL: (
                material.display_name,
                material.drag_types[0],
                material.patterns,
                material.widget_prefix,
            ),
            FieldType.TEXTURE:  (
                texture.display_name,
                texture.drag_types[0],
                texture.patterns,
                texture.widget_prefix,
            ),
            FieldType.SHADER: (
                shader.display_name,
                shader.drag_types[0],
                shader.patterns,
                shader.widget_prefix,
            ),
        }
    return _ASSET_REF_CONFIG


def _resolve_asset_config(metadata):
    """Return (display, drag_type, extensions, prefix) for an ASSET field,
    using the authoritative registry contract."""
    asset_type = str(
        getattr(metadata, "asset_type", "") if metadata else ""
    ).strip()
    if not asset_type:
        raise ValueError("ASSET Inspector fields require an explicit asset_type")
    from Infernux.core.asset_reference_types import asset_type_registry

    descriptor = asset_type_registry.require(asset_type)
    return (
        descriptor.display_name,
        descriptor.drag_types[0],
        descriptor.patterns,
        descriptor.widget_prefix,
    )


def _create_asset_ref_from_payload(metadata, payload):
    """Create the correct AssetRefBase subclass from a file path,
    using *metadata.asset_type* to select the right ref class."""
    asset_type = str(
        getattr(metadata, "asset_type", "") if metadata else ""
    ).strip()
    if not asset_type:
        raise ValueError("ASSET reference payload requires an explicit asset_type")
    guid, file_path = _resolve_guid_and_path(payload)
    from Infernux.core.asset_ref import create_asset_ref
    from Infernux.core.asset_reference_types import asset_type_registry

    descriptor = asset_type_registry.require(asset_type)
    return create_asset_ref(
        descriptor.type_id,
        guid=guid,
        path_hint=_portable_asset_path_hint(file_path),
    )


def _render_asset_reference_field(
    ctx, comp, field_name, metadata, current_value, field_type, lw,
    *, builtin_attr=None,
):
    """Render a MATERIAL / TEXTURE / SHADER / ASSET reference field."""
    from Infernux.components.fields import FieldType as _FT

    # For ASSET fields, resolve config from registry using metadata.asset_type
    if field_type == _FT.ASSET:
        type_hint, drag_type, _globs, prefix = _resolve_asset_config(metadata)
    else:
        type_hint, drag_type, _globs, prefix = _get_asset_ref_config()[field_type]

    display = _get_reference_display_name(field_type, current_value)

    from Infernux.engine.interaction import make_attribute_property_transaction

    attr_name = builtin_attr or field_name
    asset_type = (
        str(getattr(metadata, "asset_type", "") or "")
        if field_type == _FT.ASSET
        else type_hint
    )

    def _normalize_reference(candidate):
        if candidate is None:
            return None
        ref = (
            _create_asset_ref_from_payload(metadata, candidate)
            if field_type == _FT.ASSET
            else _create_reference_value_from_payload(field_type, candidate)
        )
        if ref is None:
            raise ValueError(f"could not resolve {asset_type} reference")
        return ref

    def _same_reference(left, right):
        from Infernux.core.asset_reference_types import AssetReferenceCodec

        return AssetReferenceCodec.normalize(
            asset_type, left
        ) == AssetReferenceCodec.normalize(asset_type, right)

    validate_callback = getattr(comp, "_call_on_validate", None)
    transaction = make_attribute_property_transaction(
        (comp,),
        attr_name,
        property_path=f"{type(comp).__name__}.{attr_name}",
        value_type=asset_type,
        description=f"Set {field_name}",
        read_only=bool(getattr(metadata, "readonly", False)),
        normalize=_normalize_reference,
        equivalent=_same_reference,
        publish=(validate_callback if callable(validate_callback) else None),
        clear_value=None,
    )

    label_key = str(getattr(metadata, "display_name_key", "") or "")
    label = t(label_key) if label_key else pretty_field_name(field_name)
    if label == label_key:
        label = pretty_field_name(field_name)
    field_label(ctx, label, lw)

    def _on_ping(_value=current_value):
        path = _resolve_asset_disk_path(_value)
        if path:
            ping_asset_in_project(path)

    render_asset_reference_field(
        ctx, f"{prefix}_ref_{field_name}", display, type_hint,
        accept_drag_type=drag_type,
        on_ping=_on_ping if current_value is not None and display != "None" else None,
        ping_path=_resolve_asset_disk_path(current_value),
        has_value=current_value is not None and display != "None",
        semantic_id=(inspector_component_semantic_id(comp, field_name)
                     if semantic_capture_enabled(ctx) else ""),
        asset_type=asset_type,
        reference_value=current_value,
        transaction=transaction,
    )


def _render_component_ref_inline(ctx, py_comp, field_name, metadata, lw):
    """Render a FieldType.COMPONENT reference field."""
    from Infernux.components.ref_wrappers import ComponentRef
    from Infernux.components.fields import get_raw_field_value, FieldType
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
        on_ping=(lambda _id=_comp_ref.go_id: ping_scene_object_in_hierarchy(_id))
        if _comp_ref.go_id else None,
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
    if isinstance(current_value, PrefabRef):
        _ping_reference = lambda _value=current_value: ping_asset_in_project(
            _resolve_asset_disk_path(_value)
        )
    else:
        _object_id = int(getattr(current_value, "persistent_id", 0) or 0)
        if not _object_id and _display_obj is not None:
            _object_id = int(getattr(_display_obj, "id", 0) or 0)
        _ping_reference = (
            lambda _id=_object_id: ping_scene_object_in_hierarchy(_id)
        ) if _object_id else None
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
        on_ping=_ping_reference,
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
        from Infernux.components.fields import FieldType
        if field_type == FieldType.COMPONENT:
            from Infernux.components.fields import get_raw_field_value
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
        from Infernux.components.fields import FieldType
        _apply_reference_drop(FieldType.GAME_OBJECT, comp, field_name, payload, required_component)


def _apply_builtin_audio_clip_drop(comp, cpp_attr: str, payload):
    """Handle an AUDIO_FILE drag-drop onto a built-in component AudioClip field."""
    try:
        supplied_guid = ""
        if isinstance(payload, dict):
            supplied_guid = str(payload.get("guid") or "").strip()
            file_path = str(
                payload.get("path_hint") or payload.get("path") or ""
            ).strip()
        else:
            file_path = str(payload)
        if supplied_guid and not file_path:
            from Infernux.lib import AssetRegistry

            database = AssetRegistry.instance().get_asset_database()
            file_path = (
                str(database.get_path_from_guid(supplied_guid) or "")
                if database else ""
            )
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


def _project_texture_guid_and_path(payload) -> tuple[str, str]:
    """Resolve a picker/drop payload to a project-owned texture GUID and path."""
    import os
    from Infernux.core.asset_types import IMAGE_EXTENSIONS
    from Infernux.core.assets import AssetManager

    supplied_guid = ""
    supplied_path = ""
    if isinstance(payload, dict):
        supplied_guid = str(payload.get("guid", "") or "").strip()
        supplied_path = str(payload.get("path_hint", "") or "").strip()
    else:
        token = str(payload or "").strip()
        if os.path.splitext(token)[1].lower() in IMAGE_EXTENSIONS:
            supplied_path = token
        else:
            supplied_guid = token

    path = _resolve_project_asset_path(supplied_path) if supplied_path else ""
    database = getattr(AssetManager, "_asset_database", None)
    if not path and supplied_guid and database is not None:
        try:
            path = _resolve_project_asset_path(
                database.get_path_from_guid(supplied_guid) or ""
            )
        except RuntimeError:
            path = ""

    extension = os.path.splitext(path)[1].lower()
    if (
        not path
        or extension not in IMAGE_EXTENSIONS
        or extension.startswith(".inx")
        or not _is_project_asset_path(path)
    ):
        return "", ""
    guid = supplied_guid or _asset_guid_from_path(path)
    return (guid, path) if guid else ("", "")


# ── Object field wrapper ──


def _resolve_asset_disk_path(value) -> str:
    """Best-effort absolute (or AssetDatabase) path for an asset reference value."""
    import os

    if value is None:
        return ""

    guid = str(getattr(value, "guid", "") or "")
    path_hint = str(getattr(value, "path_hint", "") or "")

    adb = None
    try:
        from Infernux.core.asset_ref import _get_asset_database
        adb = _get_asset_database()
    except Exception:
        adb = None

    if guid and adb is not None:
        try:
            resolved = adb.get_path_from_guid(guid) or ""
            if resolved:
                return resolved
        except Exception:
            pass

    resolved_obj = None
    if hasattr(value, "resolve"):
        try:
            resolved_obj = value.resolve()
        except Exception:
            resolved_obj = None
    elif not isinstance(value, str):
        resolved_obj = value

    if resolved_obj is not None:
        for attr in ("file_path", "source_path", "path"):
            candidate = getattr(resolved_obj, attr, None)
            if candidate:
                text = str(candidate)
                # Embedded sub-assets use virtual paths; ping the host file.
                for token in ("::submat:", "::subanim:", "::subbone:"):
                    if token in text:
                        text = text.split(token, 1)[0]
                        break
                if text:
                    return text

    if path_hint:
        if os.path.isfile(path_hint):
            return path_hint
        if adb is not None:
            try:
                # path_hint may be project-relative / portable.
                guid_from_hint = adb.get_guid_from_path(path_hint) or ""
                if guid_from_hint:
                    return adb.get_path_from_guid(guid_from_hint) or path_hint
            except Exception:
                pass
        return path_hint

    if isinstance(value, str) and value:
        return value
    return ""


def ping_asset_in_project(path: str) -> bool:
    """Focus FileManager for a referenced asset stored under ``Assets``."""
    disk_path = str(path or "").strip()
    if not disk_path:
        return False
    for token in ("::submat:", "::subanim:", "::subbone:"):
        if token in disk_path:
            disk_path = disk_path.split(token, 1)[0]
            break
    disk_path = _resolve_project_asset_path(disk_path)
    if not _is_project_asset_path(disk_path):
        return False
    try:
        from Infernux.engine.interaction import EditorInteractionCore, SelectionTarget

        core = EditorInteractionCore.instance()
        if core is None:
            return False
        return bool(core.navigation.locate(
            SelectionTarget.asset(disk_path),
            owner_id="project",
            reason="ping_asset",
            record_history=True,
        ))
    except Exception as exc:
        from Infernux.debug import Debug
        Debug.log_suppressed("ping_asset_in_project", exc)
        return False


def ping_scene_object_in_hierarchy(object_id: int) -> None:
    """Focus Hierarchy and select the referenced live scene object."""
    object_id = int(object_id or 0)
    if object_id <= 0:
        return
    try:
        from Infernux.engine.interaction import EditorInteractionCore, SelectionTarget

        core = EditorInteractionCore.instance()
        if core is None:
            return
        core.navigation.locate(
            SelectionTarget.scene_object(object_id),
            owner_id="hierarchy",
            reason="ping_scene_reference",
            record_history=True,
        )
    except Exception as exc:
        from Infernux.debug import Debug
        Debug.log_suppressed("ping_scene_object_in_hierarchy", exc)


def open_asset_reference(file_path: str) -> bool:
    """Open one referenced asset through the shared editor document service."""
    import os

    path = _resolve_project_asset_path(file_path)
    if not path:
        return False
    extension = os.path.splitext(path)[1].casefold()
    if extension in {".vert", ".frag", ".py"}:
        from Infernux.engine.project_context import get_project_root
        from .project_utils import open_file_with_system

        return bool(open_file_with_system(path, get_project_root() or ""))

    from Infernux.engine.interaction import (
        DocumentKind,
        DocumentOpenStatus,
        EditorInteractionCore,
    )

    document_kinds = {
        ".particlegraph": DocumentKind.PARTICLE_GRAPH,
        ".animfsm": DocumentKind.ANIMATION_FSM,
        ".timelinefsm": DocumentKind.ANIMATION_FSM,
        ".animtimeline": DocumentKind.TIMELINE,
        ".animclip2d": DocumentKind.ANIMATION_CLIP,
        ".mat": DocumentKind.MATERIAL,
        ".physicmaterial": DocumentKind.PHYSIC_MATERIAL,
        ".effect": DocumentKind.RENDER_EFFECT,
        ".effectgroup": DocumentKind.RENDER_EFFECT,
    }
    kind = document_kinds.get(extension)
    core = EditorInteractionCore.instance()
    if core is not None and kind is not None:
        result = core.document_open.open_resource(
            kind,
            path,
            title=os.path.basename(path),
        )
        if result.status is not DocumentOpenStatus.FAILED:
            return True

    # Imported assets without a dedicated authoring panel open in Inspector by
    # selecting/revealing them through the same typed navigation transaction.
    ping_asset_in_project(path)
    return True


def render_object_field(ctx: InxGUIContext, field_id: str, display_text: str,
                        type_hint: str, selected: bool = False, clickable: bool = True,
                        accept_drag_type: str = None, on_drop_callback=None,
                        picker_scene_items=None, picker_asset_items=None,
                        on_pick=None, on_clear=None, on_ping=None,
                        on_open=None,
                        ping_path=None,
                        has_value=None,
                        semantic_id: str = "") -> bool:
    """Render a Unity-style object field (selectable box showing an object reference)."""
    from .igui import IGUI
    return IGUI.object_field(
        ctx, field_id, display_text, type_hint,
        selected=selected, clickable=clickable,
        accept=accept_drag_type, on_drop=on_drop_callback,
        picker_scene_items=picker_scene_items,
        picker_asset_items=picker_asset_items,
        on_pick=on_pick, on_clear=on_clear, on_ping=on_ping,
        on_open=on_open,
        ping_path=ping_path,
        has_value=has_value,
        semantic_id=semantic_id,
    )


def render_asset_reference_field(
    ctx: InxGUIContext,
    field_id: str,
    display_text: str,
    type_hint: str,
    selected: bool = False,
    clickable: bool = True,
    accept_drag_type=None,
    on_assign=None,
    picker_scene_items=None,
    additional_asset_items=None,
    on_clear=None,
    on_ping=None,
    on_open=None,
    ping_path=None,
    semantic_id: str = "",
    asset_type: str = "",
    on_rejected=None,
    has_value=None,
    reference_value=None,
    transaction=None,
    alternate_compatibility=None,
    read_only: bool = False,
) -> bool:
    """Render an asset reference through the unique asset-field contract."""
    from .igui import IGUI

    return IGUI.asset_reference_field(
        ctx,
        field_id,
        display_text,
        type_hint,
        selected=selected,
        clickable=clickable,
        accept=accept_drag_type,
        on_assign=on_assign,
        picker_scene_items=picker_scene_items,
        additional_asset_items=additional_asset_items,
        on_clear=on_clear,
        on_ping=on_ping,
        on_open=on_open,
        ping_path=ping_path,
        has_value=has_value,
        semantic_id=semantic_id,
        asset_type=asset_type,
        on_rejected=on_rejected,
        reference_value=reference_value,
        transaction=transaction,
        alternate_compatibility=alternate_compatibility,
        read_only=read_only,
    )
