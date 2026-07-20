"""List field rendering helpers for the Inspector component renderers."""

from dataclasses import replace
from Infernux.components.component import InxComponent
from Infernux.lib import InxGUIContext
from .inspector_utils import (
    max_label_w, render_serialized_field, has_field_changed,
    render_compact_section_header, pretty_field_name,
    get_enum_members as _get_enum_members,
)
from .theme import Theme
from ._inspector_undo import _record_property
from ._inspector_references import (
    _create_reference_value_from_payload,
    _get_reference_display_name,
    _game_object_has_required_component,
    _create_component_ref_from_go,
    _picker_scene_gameobjects,
    _picker_assets,
    render_object_field,
)


def _make_list_default_element(metadata, element_type):
    from Infernux.components.serialized_field import FieldType
    from Infernux.math import Vector2, Vector3, vec4f

    if element_type == FieldType.INT:
        return 0
    if element_type == FieldType.FLOAT:
        return 0.0
    if element_type == FieldType.BOOL:
        return False
    if element_type == FieldType.STRING:
        return ""
    if element_type == FieldType.VEC2:
        return Vector2(0.0, 0.0)
    if element_type == FieldType.VEC3:
        return Vector3(0.0, 0.0, 0.0)
    if element_type == FieldType.VEC4:
        return vec4f(0.0, 0.0, 0.0, 0.0)
    if element_type == FieldType.COLOR:
        from Infernux.components.serialized_field import RGBA_DEFAULT
        return list(RGBA_DEFAULT)
    if element_type == FieldType.ENUM and metadata.enum_type is not None:
        members = _get_enum_members(metadata.enum_type)
        return members[0] if members else metadata.default
    if element_type == FieldType.GAME_OBJECT:
        from Infernux.components.ref_wrappers import GameObjectRef
        return GameObjectRef(persistent_id=0)
    if element_type == FieldType.MATERIAL:
        from Infernux.components.ref_wrappers import MaterialRef
        return MaterialRef(guid="")
    if element_type == FieldType.TEXTURE:
        from Infernux.core.asset_ref import TextureRef
        return TextureRef()
    if element_type == FieldType.SHADER:
        from Infernux.core.asset_ref import ShaderRef
        return ShaderRef()
    if element_type == FieldType.ASSET:
        from Infernux.core.asset_ref import AudioClipRef
        return AudioClipRef()
    if element_type == FieldType.SERIALIZABLE_OBJECT:
        elem_cls = metadata.element_class
        if elem_cls is not None:
            return elem_cls()
        return None
    if element_type == FieldType.COMPONENT:
        from Infernux.components.ref_wrappers import ComponentRef
        comp_type = getattr(metadata, 'component_type', '') or ''
        return ComponentRef(component_type=comp_type)
    return None


def _infer_list_element_type(metadata, current_value):
    from Infernux.components.serialized_field import infer_field_type_from_value, FieldType

    if metadata.element_type is not None:
        return metadata.element_type

    from Infernux.components.serialized_field import is_rgba_storage

    for container in (current_value, metadata.default):
        if is_rgba_storage(container):
            return FieldType.COLOR
        if not isinstance(container, (list, tuple)):
            continue
        for item in container:
            if item is None:
                continue
            inferred = infer_field_type_from_value(item)
            if inferred != FieldType.UNKNOWN:
                return inferred

    return FieldType.STRING


def _list_drag_drop_type(element_type, metadata=None):
    from Infernux.components.serialized_field import FieldType

    mapping = {
        FieldType.GAME_OBJECT: ["HIERARCHY_GAMEOBJECT", "PREFAB_GUID", "PREFAB_FILE"],
        FieldType.MATERIAL: "MATERIAL_FILE",
        FieldType.TEXTURE: "TEXTURE_FILE",
        FieldType.SHADER: "SHADER_FILE",
        FieldType.COMPONENT: "HIERARCHY_GAMEOBJECT",
    }
    if element_type == FieldType.ASSET:
        asset_type = getattr(metadata, "asset_type", None) if metadata else None
        if asset_type:
            from Infernux.core.asset_ref import get_asset_type_config
            cfg = get_asset_type_config(asset_type)
            if cfg:
                return cfg["drag_type"]
        return "AUDIO_FILE"  # fallback
    return mapping.get(element_type)


def _list_type_hint(element_type, metadata):
    from Infernux.components.serialized_field import FieldType

    if element_type == FieldType.GAME_OBJECT:
        return f"GameObject:{metadata.required_component}" if metadata.required_component else "GameObject"
    if element_type == FieldType.MATERIAL:
        return "Material"
    if element_type == FieldType.TEXTURE:
        return "Texture"
    if element_type == FieldType.SHADER:
        return "Shader"
    if element_type == FieldType.ASSET:
        asset_type = getattr(metadata, "asset_type", None) if metadata else None
        if asset_type:
            from Infernux.core.asset_ref import get_asset_type_config
            cfg = get_asset_type_config(asset_type)
            if cfg:
                return cfg["display"]
        return "AudioClip"
    if element_type == FieldType.COMPONENT:
        comp_type = getattr(metadata, 'component_type', '') or ''
        return comp_type or "Component"
    return "Element"


def _make_list_picker_providers(element_type, metadata):
    """Return ``(scene_items_fn_or_None, asset_items_fn_or_None)`` for a list element type."""
    from Infernux.components.serialized_field import FieldType

    if element_type in (FieldType.GAME_OBJECT, FieldType.COMPONENT):
        _rc = metadata.component_type if element_type == FieldType.COMPONENT else metadata.required_component
        return (lambda filt, _rc=_rc: _picker_scene_gameobjects(filt, required_component=_rc), None)

    if element_type == FieldType.MATERIAL:
        return (None, lambda filt: _picker_assets(filt, "*.mat"))
    if element_type == FieldType.TEXTURE:
        patterns = ("*.png", "*.jpg", "*.jpeg", "*.bmp", "*.tga", "*.gif", "*.psd", "*.hdr", "*.pic", "*.pnm", "*.pgm", "*.ppm")
        return (None, lambda filt, _patterns=patterns: sum(
            (_picker_assets(filt, pattern, assets_only=True) for pattern in _patterns), []))
    if element_type == FieldType.SHADER:
        return (None, lambda filt: _picker_assets(filt, "*.vert") + _picker_assets(filt, "*.frag"))
    if element_type == FieldType.ASSET:
        asset_type = getattr(metadata, "asset_type", None) if metadata else None
        if asset_type:
            from Infernux.core.asset_ref import get_asset_type_config
            cfg = get_asset_type_config(asset_type)
            if cfg:
                exts = cfg["extensions"]
                return (None, lambda filt, _exts=exts: sum(
                    (_picker_assets(filt, e) for e in _exts), []))
        return (None, lambda filt: _picker_assets(filt, "*.wav") + _picker_assets(filt, "*.mp3") + _picker_assets(filt, "*.ogg"))
    return (None, None)


def _create_list_pick_ref(element_type, value, required_component=None, metadata=None):
    """Create a reference wrapper from a picker selection for a list element."""
    from Infernux.components.serialized_field import FieldType

    if element_type == FieldType.GAME_OBJECT:
        from Infernux.components.ref_wrappers import GameObjectRef
        return GameObjectRef(value) if value is not None else None

    if element_type == FieldType.COMPONENT:
        return _create_component_ref_from_go(value, required_component or '')

    # Asset types: value is a file path
    if element_type == FieldType.MATERIAL:
        return _create_reference_value_from_payload(FieldType.MATERIAL, value)
    if element_type == FieldType.TEXTURE:
        return _create_reference_value_from_payload(FieldType.TEXTURE, value)
    if element_type == FieldType.SHADER:
        return _create_reference_value_from_payload(FieldType.SHADER, value)
    if element_type == FieldType.ASSET:
        return _create_reference_value_from_payload(FieldType.ASSET, value, metadata=metadata)
    return None


def _render_reference_list_item(ctx, field_name, index, item, items, metadata, element_type):
    """Render a single reference-type list element (GameObject, Material, etc.).

    Mutates *items* in-place on drop/pick/clear. Returns True if changed.
    """
    from Infernux.components.serialized_field import FieldType
    from .igui import IGUI
    changed = False
    _req = metadata.component_type if element_type == FieldType.COMPONENT else metadata.required_component

    def _replace_item(payload, _index=index, _req=_req, _meta=metadata):
        nonlocal changed
        value = _create_reference_value_from_payload(element_type, payload, _req, metadata=_meta)
        if value is not None:
            items[_index] = value
            changed = True

    _li_scene, _li_assets = _make_list_picker_providers(element_type, metadata)

    def _li_on_pick(value, _index=index, _et=element_type, _req=_req, _meta=metadata):
        nonlocal changed
        ref = _create_list_pick_ref(_et, value, _req, metadata=_meta)
        if ref is not None:
            items[_index] = ref
            changed = True

    def _li_on_clear(_index=index, _et=element_type):
        nonlocal changed
        items[_index] = _make_list_default_element(metadata, _et)
        changed = True

    IGUI.object_field(
        ctx,
        f"list_{field_name}_{index}",
        _get_reference_display_name(element_type, item),
        _list_type_hint(element_type, metadata),
        accept=_list_drag_drop_type(element_type, metadata),
        on_drop=_replace_item,
        picker_scene_items=_li_scene,
        picker_asset_items=_li_assets,
        on_pick=_li_on_pick,
        on_clear=_li_on_clear,
    )
    return changed


def _render_serializable_list_item(ctx, field_name, index, item, items, metadata):
    """Render a single SERIALIZABLE_OBJECT list element with nested fields.

    Mutates *items[index]* in-place on change. Returns True if changed.
    """
    import copy as _copy
    so_class = type(item) if item is not None else metadata.element_class
    so_label = f"[{index}]" + (f" ({so_class.__name__})" if so_class else "")
    if not render_compact_section_header(ctx, so_label, level="tertiary"):
        return False
    from Infernux.components.serialized_field import get_serialized_fields as _gsf
    if not so_class or item is None:
        return False
    so_fields = _gsf(so_class)
    so_lw = max_label_w(ctx, list(so_fields.keys())) if so_fields else 0.0
    elem_changes = {}
    for so_fn, so_meta in so_fields.items():
        if getattr(so_meta, "hidden", False):
            continue
        from Infernux.components.serialized_field import FieldType, get_raw_field_value
        reference_types = {
            FieldType.MATERIAL,
            FieldType.TEXTURE,
            FieldType.SHADER,
            FieldType.ASSET,
        }
        if so_meta.field_type in reference_types:
            so_val = get_raw_field_value(item, so_fn)
            new_val = _render_serializable_asset_reference(
                ctx,
                f"{field_name}_{index}_{so_fn}",
                so_fn,
                so_meta,
                so_val,
                so_lw,
            )
        else:
            so_val = getattr(item, so_fn, so_meta.default)
            new_val = render_serialized_field(
                ctx, f"##{field_name}_{index}_{so_fn}", so_fn,
                so_meta, so_val, so_lw,
            )
        if has_field_changed(so_meta.field_type, so_val, new_val):
            elem_changes[so_fn] = new_val
    if not elem_changes:
        return False
    edited = _copy.deepcopy(item)
    for fn, fv in elem_changes.items():
        setattr(edited, fn, fv)
    items[index] = edited
    return True


def _render_serializable_asset_reference(
    ctx,
    widget_id,
    field_name,
    metadata,
    current_value,
    label_width,
):
    """Render a resource ref nested inside a SerializableObject list item."""
    from Infernux.components.serialized_field import FieldType
    from Infernux.components._serialize_helpers import make_null_ref
    from ._inspector_references import (
        _create_asset_ref_from_payload,
        _create_reference_value_from_payload,
        _get_asset_ref_config,
        _get_reference_display_name,
        _picker_assets,
        _resolve_asset_config,
    )
    from .inspector_utils import field_label, pretty_field_name

    field_type = metadata.field_type
    if field_type == FieldType.ASSET:
        type_hint, drag_type, globs, prefix = _resolve_asset_config(metadata)
    else:
        type_hint, drag_type, globs, prefix = _get_asset_ref_config()[field_type]

    selected = current_value

    def _make_value(path):
        if field_type == FieldType.ASSET:
            return _create_asset_ref_from_payload(metadata, str(path))
        return _create_reference_value_from_payload(field_type, path)

    def _on_pick(path):
        nonlocal selected
        value = _make_value(path)
        if value is not None:
            selected = value

    def _on_clear():
        nonlocal selected
        selected = make_null_ref(field_type, metadata)

    assets_only = field_type == FieldType.TEXTURE

    def _picker(filt):
        values = []
        for glob in globs:
            values += _picker_assets(filt, glob, assets_only=assets_only)
        return values

    field_label(ctx, pretty_field_name(field_name), label_width)
    render_object_field(
        ctx,
        f"{prefix}_nested_ref_{widget_id}",
        _get_reference_display_name(field_type, current_value),
        type_hint,
        accept_drag_type=drag_type,
        on_drop_callback=_on_pick,
        picker_asset_items=_picker,
        on_pick=_on_pick,
        on_clear=_on_clear,
    )
    return selected


def _render_list_items_body(ctx, comp, field_name, metadata, items, element_type,
                            reference_types, button_spacing, current_value):
    """Render list item rows, reorder separators, and bottom drop zone. Returns True if changed."""
    from .igui import IGUI
    from .inspector_utils import render_serialized_field, has_field_changed
    from Infernux.components.serialized_field import FieldType
    from dataclasses import replace

    changed = False
    _list_drag_id = f"IGUI_LIST_{id(comp)}_{field_name}"
    move_from = None
    move_to = None

    def _make_reorder_cb(target_index):
        def _cb(payload):
            nonlocal move_from, move_to
            src = int(payload)
            if src != target_index and src != target_index - 1:
                move_from = src
                move_to = target_index
        return _cb

    # ── List body background ──
    body_state = IGUI.list_body_begin(ctx, f"list_body_{field_name}")

    IGUI.reorder_separator(ctx, f"##sep_{field_name}_before_0", _list_drag_id, _make_reorder_cb(0))

    remove_index = None
    element_meta = replace(metadata, field_type=element_type, default=_make_list_default_element(metadata, element_type))

    for index, item in enumerate(items):
        ctx.push_id_str(f"{field_name}_{index}")
        remove_clicked = IGUI.list_item_remove_button(ctx, f"{field_name}_{index}")
        ctx.same_line(0, button_spacing)

        if ctx.begin_drag_drop_source(0):
            ctx.set_drag_drop_payload(_list_drag_id, index)
            ctx.label(f"[{index}]")
            ctx.end_drag_drop_source()

        if element_type in reference_types:
            if _render_reference_list_item(ctx, field_name, index, item, items, metadata, element_type):
                changed = True
        elif element_type == FieldType.SERIALIZABLE_OBJECT:
            if _render_serializable_list_item(ctx, field_name, index, item, items, metadata):
                changed = True
        else:
            new_item = render_serialized_field(
                ctx, f"##{field_name}_{index}", "", element_meta, item, 0.0,
            )
            if has_field_changed(element_type, item, new_item):
                items[index] = new_item
                changed = True

        if remove_clicked:
            remove_index = index
        ctx.pop_id()
        IGUI.reorder_separator(ctx, f"##sep_{field_name}_after_{index}", _list_drag_id, _make_reorder_cb(index + 1))

    IGUI.list_body_end(ctx, body_state)

    if remove_index is not None:
        items.pop(remove_index)
        changed = True

    if move_from is not None and move_to is not None:
        elem = items.pop(move_from)
        insert_at = move_to if move_to < move_from else move_to - 1
        items.insert(insert_at, elem)
        changed = True

    return changed


def _render_list_field(
    ctx: InxGUIContext,
    comp,
    field_name: str,
    metadata,
    current_value,
    lw: float,
    *,
    display_name: str | None = None,
    header_drop_type: str | None = None,
    header_drop_factory=None,
):
    from Infernux.components.serialized_field import FieldType
    from .igui import IGUI

    items = list(current_value) if isinstance(current_value, list) else []
    element_type = _infer_list_element_type(metadata, items)
    changed = False
    button_spacing = Theme.INSPECTOR_INLINE_BTN_GAP

    reference_types = {
        FieldType.GAME_OBJECT,
        FieldType.MATERIAL,
        FieldType.TEXTURE,
        FieldType.SHADER,
        FieldType.ASSET,
        FieldType.COMPONENT,
    }

    # ── Callbacks for IGUI.list_header ──
    def _on_add():
        nonlocal changed
        items.append(_make_list_default_element(metadata, element_type))
        changed = True

    def _on_remove_last():
        nonlocal changed
        if items:
            items.pop()
            changed = True

    # Header drop callback (for reference types)
    _hdr_drag_type = header_drop_type
    if _hdr_drag_type is None and element_type in reference_types:
        _hdr_drag_type = _list_drag_drop_type(element_type, metadata)
    _hdr_req = (metadata.component_type if element_type == FieldType.COMPONENT
                else metadata.required_component) if element_type in reference_types else None

    def _header_drop(payload):
        nonlocal changed
        if header_drop_factory is not None:
            value = header_drop_factory(payload)
        else:
            value = _create_reference_value_from_payload(
                element_type, payload, _hdr_req, metadata=metadata,
            )
        if value is not None:
            items.append(value)
            changed = True

    # ── Unified list header: [label [N] ........... [-][+]] ──
    header_open = IGUI.list_header(
        ctx, display_name or pretty_field_name(field_name), len(items),
        on_add=_on_add,
        on_remove=_on_remove_last if items else None,
        accept_drop=_hdr_drag_type,
        on_header_drop=_header_drop if _hdr_drag_type else None,
    )

    if not header_open:
        if changed and not metadata.readonly:
            _record_property(comp, field_name, current_value, items, f"Set {field_name}")
            if hasattr(comp, '_call_on_validate'):
                comp._call_on_validate()
        return

    # The header itself remains a complete add/remove/drop target. Avoid
    # building an empty body and reorder separator until an item exists.
    if not items:
        if changed and not metadata.readonly:
            _record_property(comp, field_name, current_value, items, f"Set {field_name}")
            if hasattr(comp, '_call_on_validate'):
                comp._call_on_validate()
        return

    # ── Render items, reorder separators, bottom drop zone ──
    if _render_list_items_body(ctx, comp, field_name, metadata, items, element_type,
                               reference_types, button_spacing, current_value):
        changed = True

    if changed and not metadata.readonly:
        _record_property(comp, field_name, current_value, items, f"Set {field_name}")
        if hasattr(comp, '_call_on_validate'):
            comp._call_on_validate()
