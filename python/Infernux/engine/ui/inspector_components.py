"""Component rendering functions for the Inspector panel.

Split into sub-modules for maintainability:
- ``_inspector_undo``         — undo recording helpers
- ``_inspector_references``   — asset-reference fields, pickers, object fields
- ``_inspector_list_field``   — list field rendering
- ``_inspector_extra_renderers`` — AudioSource / MeshRenderer extra UI
"""

import json
import math
import time as _time
from dataclasses import replace
from types import SimpleNamespace
from Infernux.components.component import InxComponent
from Infernux.lib import InxGUIContext
from Infernux.engine.i18n import t
from . import inspector_support as _inspector_support
from .inspector_utils import (
    max_label_w, field_label, render_serialized_field, has_field_changed,
    render_compact_section_header, render_info_text, render_component_header,
    render_inspector_checkbox, pretty_field_name,
    float_close as _float_close,
    get_enum_members as _get_enum_members,
    get_enum_member_name as _get_enum_member_name,
    get_enum_member_value as _get_enum_member_value,
    find_enum_index as _find_enum_index,
    DRAG_SPEED_DEFAULT, DRAG_SPEED_FINE, DRAG_SPEED_INT,
    build_scalar_desc, is_batch_renderable,
    semantic_capture_enabled, inspector_component_semantic_id,
    record_inspector_component_item,
)
from .theme import Theme, ImGuiCol

# ── Sub-module imports (re-exported for backward compatibility) ──
from ._inspector_undo import (  # noqa: F401
    _notify_scene_modified, _is_python_component_entry,
    _record_property, _record_material_slot, _record_generic_component,
    _record_builtin_property, _TrackVolumeCommand, _record_track_volume,
)
from ._inspector_references import (  # noqa: F401
    _tooltip_and_info, _asset_guid_from_path, _resolve_guid_and_path,
    _create_reference_value_from_payload, _get_reference_display_name,
    _render_serializable_object_field, _render_nested_so,
    _get_asset_ref_config, _render_asset_reference_field,
    _render_component_ref_inline, _render_gameobject_ref_inline,
    _apply_reference_drop, _apply_gameobject_or_prefab_drop,
    _apply_builtin_audio_clip_drop,
    _game_object_has_required_component, _create_component_ref_from_go,
    _picker_scene_gameobjects, _picker_assets,
    render_object_field,
)
from ._inspector_list_field import (  # noqa: F401
    _make_list_default_element, _infer_list_element_type,
    _list_drag_drop_type, _list_type_hint,
    _render_reference_list_item, _render_serializable_list_item,
    _render_list_field,
    _make_list_picker_providers, _create_list_pick_ref,
)
from ._inspector_extra_renderers import (  # noqa: F401
    _render_audio_source_extra, _render_mesh_renderer_materials,
    _render_particle_system_parameters,
)

# _render_info_text is now render_info_text from inspector_utils
_render_info_text = render_info_text



def _is_in_play_mode() -> bool:
    """Return True if the engine is currently in runtime (play/pause) mode."""
    from Infernux.engine.play_mode import PlayModeManager, PlayModeState
    pm = PlayModeManager.instance()
    if pm and pm.state != PlayModeState.EDIT:
        return True
    return False


# ============================================================================
# Component renderer registry
# ============================================================================

_COMPONENT_RENDERERS: dict = {}   # type_name -> render_fn(ctx, comp)
_PY_COMPONENT_RENDERERS: dict = {}  # type_name -> render_fn(ctx, py_comp)
_PY_COMPONENT_EXTRA_RENDERERS: dict = {}  # appended after generic serialized fields
_COMPONENT_EXTRA_RENDERERS: dict = {}  # type_name -> render_fn(ctx, comp) appended after generic
_COMPONENT_VALUE_CACHE: dict = {}
_COMPONENT_VALUE_CACHE_TTL_S = 0.20
_COMPONENT_VALUE_CACHE_MAX = 1024


def _get_component_cache_id(comp) -> int:
    return getattr(comp, 'component_id', None) or id(comp)


def _begin_component_value_cache(kind: str, comp):
    """Return (cache_entry, refresh_values) for a component scalar-value cache."""
    in_play = _is_in_play_mode()

    key = (kind, _get_component_cache_id(comp))
    now = _time.monotonic()
    entry = _COMPONENT_VALUE_CACHE.get(key)
    missing = entry is None

    if in_play:
        # In play mode the undo system doesn't fire so generation never bumps.
        # Rely solely on TTL-based expiry – values refresh ~5 times/sec,
        # and the builtin plan is reused between refreshes.
        generation = entry.get("generation", -1) if entry else -1
        generation_changed = False
    else:
        generation = _inspector_support.get_inspector_value_generation()
        generation_changed = False if missing else entry.get("generation") != generation

    ttl_expired = False if missing else (now - entry.get("refreshed_at", 0.0)) >= _COMPONENT_VALUE_CACHE_TTL_S
    refresh_values = missing or generation_changed or ttl_expired
    if refresh_values:
        if missing:
            _record_profile_count(f"{kind}Cache_missMissing_count")
        if generation_changed:
            _record_profile_count(f"{kind}Cache_missGeneration_count")
        if ttl_expired:
            _record_profile_count(f"{kind}Cache_missTtl_count")
        entry = {
            "generation": generation,
            "refreshed_at": now,
            "values": {},
        }
        if len(_COMPONENT_VALUE_CACHE) >= _COMPONENT_VALUE_CACHE_MAX:
            _COMPONENT_VALUE_CACHE.clear()
        _COMPONENT_VALUE_CACHE[key] = entry
    else:
        _record_profile_count(f"{kind}Cache_hit_count")
    return entry, refresh_values


def _get_cached_component_value(cache_entry, refresh_values: bool, field_key, getter):
    values = cache_entry["values"]
    if refresh_values or field_key not in values:
        values[field_key] = getter()
    return values[field_key]


def _invalidate_component_value_cache(cache_entry) -> None:
    cache_entry["generation"] = _inspector_support.get_inspector_value_generation()
    cache_entry["refreshed_at"] = _time.monotonic()
    cache_entry["values"] = {}


def _invalidate_component_render_cache(cache_entry) -> None:
    _invalidate_component_value_cache(cache_entry)
    cache_entry.pop("builtin_plan", None)
    cache_entry.pop("py_plan", None)


_PROFILE_ENABLED = _inspector_support.is_inspector_profile_enabled()


def _profile_start() -> float:
    return _time.perf_counter() if _PROFILE_ENABLED else 0.0


def _record_profile_timing(bucket: str, start_time: float) -> None:
    if _PROFILE_ENABLED:
        _inspector_support.record_inspector_profile_timing(
            bucket, (_time.perf_counter() - start_time) * 1000.0,
        )


def _record_profile_count(bucket: str, amount: float = 1.0) -> None:
    if _PROFILE_ENABLED:
        _inspector_support.record_inspector_profile_count(bucket, amount)


def _build_builtin_cached_plan(ctx: InxGUIContext, comp, props, lw, skip_fields, cache_entry, refresh_values):
    """Build a cached render plan for a BuiltinComponent inspector body."""
    from Infernux.components.serialized_field import FieldType

    ops = []
    batch_descs = []
    batch_info = []
    capture_semantics = semantic_capture_enabled(ctx)

    def _flush_batch():
        nonlocal batch_descs, batch_info
        if not batch_descs:
            return
        ops.append({
            "kind": "batch",
            "plan": ctx.create_property_batch_plan(batch_descs),
            "info": batch_info,
        })
        batch_descs = []
        batch_info = []

    for py_name, cpp_prop in props:
        if skip_fields and py_name in skip_fields:
            continue

        meta = cpp_prop.metadata
        cpp_attr = cpp_prop.cpp_attr

        if getattr(meta, 'hidden', False):
            continue

        if meta.visible_when is not None:
            try:
                if not meta.visible_when(comp):
                    continue
            except (RuntimeError, TypeError) as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                pass

        current = _get_cached_component_value(
            cache_entry, refresh_values, cpp_attr,
            lambda _attr=cpp_attr: getattr(comp, _attr),
        )

        if meta.readonly:
            _flush_batch()
            ops.append({
                "kind": "readonly",
                "py_name": py_name,
                "current": current,
            })
            continue

        if meta.field_type == FieldType.ASSET and cpp_attr == "clip":
            _flush_batch()
            display_name = "None"
            if current is not None and hasattr(current, "name"):
                try:
                    display_name = current.name or "None"
                except (RuntimeError, AttributeError):
                    display_name = "None"
            ops.append({
                "kind": "asset_clip",
                "py_name": py_name,
                "cpp_attr": cpp_attr,
                "display_name": display_name,
            })
            continue

        if meta.field_type == FieldType.ASSET:
            _flush_batch()
            ops.append({
                "kind": "asset_reference",
                "py_name": py_name,
                "cpp_attr": cpp_attr,
                "meta": meta,
                "current": current,
            })
            continue

        hdr = meta.header or ""
        spc = meta.space if meta.space and meta.space > 0 else 0.0
        desc = build_scalar_desc(
            f"##{py_name}", pretty_field_name(py_name), meta, current,
            header_text=hdr, space_before=spc,
            semantic_id=(inspector_component_semantic_id(comp, py_name)
                         if capture_semantics else ""),
        )
        if desc is not None:
            enum_members = None
            if meta.field_type == FieldType.ENUM:
                enum_cls = meta.enum_type
                if isinstance(enum_cls, str):
                    import Infernux.lib as _lib
                    enum_cls = getattr(_lib, enum_cls, None)
                if enum_cls is not None:
                    enum_members = _get_enum_members(enum_cls)
            batch_descs.append(desc)
            batch_info.append((py_name, cpp_attr, meta, current, enum_members))
            continue

        _flush_batch()
        ops.append({
            "kind": "fallback_scalar",
            "py_name": py_name,
            "cpp_attr": cpp_attr,
            "meta": meta,
            "current": current,
            "header": hdr,
            "space": spc,
        })

    _flush_batch()
    return {
        "lw": lw,
        "skip_fields": tuple(sorted(skip_fields)) if skip_fields else (),
        "semantic_capture": capture_semantics,
        "ops": ops,
    }


def _replay_builtin_cached_plan(ctx: InxGUIContext, comp, plan: dict, cache_entry) -> bool:
    """Replay a cached BuiltinComponent render plan. Returns True if edited."""
    lw = plan["lw"]
    edited = False

    for op in plan["ops"]:
        kind = op["kind"]

        if kind == "batch":
            changes = ctx.render_property_batch_plan(op["plan"], lw)
            if changes:
                _apply_batch_changes_builtin(comp, changes, op["info"])
                edited = True
            continue

        if kind == "readonly":
            ctx.label(f"{op['py_name']}: {op['current']}")
            continue

        if kind == "asset_clip":
            field_label(ctx, pretty_field_name(op["py_name"]), lw)
            render_object_field(
                ctx,
                f"audio_clip_{op['py_name']}",
                op["display_name"],
                "AudioClip",
                accept_drag_type="AUDIO_FILE",
                on_drop_callback=lambda payload, _comp=comp, _attr=op["cpp_attr"]: _apply_builtin_audio_clip_drop(_comp, _attr, payload),
            )
            continue

        if kind == "asset_reference":
            from Infernux.components.serialized_field import FieldType

            _render_asset_reference_field(
                ctx, comp, op["py_name"], op["meta"], op["current"],
                FieldType.ASSET, lw, builtin_attr=op["cpp_attr"],
            )
            continue

        if kind == "fallback_scalar":
            if op["header"]:
                ctx.separator()
                ctx.label(op["header"])
            if op["space"] > 0:
                ctx.dummy(0, op["space"])
            new_value = render_serialized_field(
                ctx, f"##{op['py_name']}", pretty_field_name(op["py_name"]),
                op["meta"], op["current"], lw,
            )
            record_inspector_component_item(
                ctx, comp, op["py_name"], "inspector_field", pretty_field_name(op["py_name"]),
            )
            if has_field_changed(op["meta"].field_type, op["current"], new_value):
                _record_builtin_property(comp, op["cpp_attr"], op["current"], new_value,
                                         f"Set {op['py_name']}")
                edited = True

    if edited:
        _invalidate_component_render_cache(cache_entry)
    return edited


def register_component_renderer(type_name: str, render_fn):
    """Register a custom Inspector renderer for a C++ component type.

    Args:
        type_name: The value returned by ``comp.type_name`` (e.g. "Camera").
        render_fn: ``fn(ctx: InxGUIContext, comp) -> None``
    """
    _COMPONENT_RENDERERS[type_name] = render_fn


def register_component_extra_renderer(type_name: str, render_fn):
    """Register extra Inspector UI appended after generic CppProperty rendering.

    Unlike ``register_component_renderer`` (which *replaces* the entire renderer),
    this appends additional UI *after* the generic CppProperty fields.  Use this
    when a component's standard properties can be handled generically but it needs
    extra custom sections (e.g. AudioSource per-track UI).

    Args:
        type_name: The value returned by ``comp.type_name``.
        render_fn: ``fn(ctx: InxGUIContext, comp) -> None``
    """
    _COMPONENT_EXTRA_RENDERERS[type_name] = render_fn


def register_py_component_renderer(type_name: str, render_fn):
    """Register a custom Inspector renderer for a Python component type.

    When registered, ``render_py_component()`` will use the custom renderer
    instead of the generic serialize-based renderer.

    Args:
        type_name: The ``type_name`` of the InxComponent (e.g. "RenderStack").
        render_fn: ``fn(ctx: InxGUIContext, py_comp) -> None``
    """
    _PY_COMPONENT_RENDERERS[type_name] = render_fn


def register_py_component_extra_renderer(type_name: str, render_fn):
    """Append dynamic Inspector UI after a Python component's serialized fields."""
    _PY_COMPONENT_EXTRA_RENDERERS[type_name] = render_fn


def render_component(ctx: InxGUIContext, comp):
    """Unified entry point — dispatches to a custom renderer, then wraps
    BuiltinComponent subclasses into their Python wrapper and renders via
    the same ``render_py_component`` path that user scripts use, and
    finally falls back to the generic serialize-based renderer."""
    # 1. Central full-replacement renderers (e.g. Transform)
    renderer = _COMPONENT_RENDERERS.get(comp.type_name)
    if renderer:
        _record_profile_count("bodyNativeCustom_count")
        _t0 = _profile_start()
        try:
            renderer(ctx, comp)
        finally:
            _record_profile_timing("bodyNativeCustom", _t0)
        return

    # 2. BuiltinComponent wrapper — delegate to render_inspector()
    from Infernux.components.builtin_component import BuiltinComponent
    wrapper_cls = BuiltinComponent._builtin_registry.get(comp.type_name)
    if wrapper_cls:
        raw_cpp = comp
        try:
            if not isinstance(comp, BuiltinComponent):
                go = getattr(comp, 'game_object', None)
                if go is not None:
                    _wrap_t0 = _profile_start()
                    comp = wrapper_cls._get_or_create_wrapper(comp, go)
                    _record_profile_timing("bodyBuiltinWrap", _wrap_t0)
                else:
                    _record_profile_count("bodyCppGeneric_count")
                    _generic_t0 = _profile_start()
                    render_cpp_component_generic(ctx, raw_cpp)
                    _record_profile_timing("bodyCppGeneric", _generic_t0)
                    return
            _record_profile_count("bodyBuiltinTotal_count")
            _builtin_t0 = _profile_start()
            try:
                comp.render_inspector(ctx)
            finally:
                _record_profile_timing("bodyBuiltinTotal", _builtin_t0)
        except Exception as exc:
            import traceback
            from Infernux.debug import Debug
            tb_str = traceback.format_exc()
            Debug.log_warning(
                f"[Inspector] render_component fallback for "
                f"{getattr(raw_cpp, 'type_name', '?')}: {exc}\n{tb_str}"
            )
            try:
                _record_profile_count("bodyCppGeneric_count")
                _generic_t0 = _profile_start()
                render_cpp_component_generic(ctx, raw_cpp)
                _record_profile_timing("bodyCppGeneric", _generic_t0)
            except Exception as fallback_exc:
                Debug.log_warning(
                    f"[Inspector] fallback also failed for "
                    f"{getattr(raw_cpp, 'type_name', '?')}: {fallback_exc}"
                )
        return

    # 3. Fallback — generic property table
    _record_profile_count("bodyCppGeneric_count")
    _generic_t0 = _profile_start()
    render_cpp_component_generic(ctx, comp)
    _record_profile_timing("bodyCppGeneric", _generic_t0)


def _multi_value_equal(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return _float_close(float(a), float(b))
        except (TypeError, ValueError):
            return False
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_multi_value_equal(x, y) for x, y in zip(a, b))
    if hasattr(a, "x") and hasattr(b, "x"):
        try:
            return _multi_value_equal((a[0], a[1], a[2]), (b[0], b[1], b[2]))
        except Exception:
            pass
    return a == b


def _all_multi_values_equal(values) -> bool:
    if not values:
        return True
    first = values[0]
    return all(_multi_value_equal(first, value) for value in values[1:])


def _format_multi_value(value) -> str:
    if value is None:
        return "None"
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, float):
        return f"{value:.3f}"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str):
        return value
    if hasattr(value, "name"):
        try:
            name = value.name
            if name:
                return str(name)
        except Exception:
            pass
    try:
        if len(value) in (2, 3, 4):
            parts = []
            for i in range(len(value)):
                v = value[i]
                parts.append(f"{float(v):.3f}" if isinstance(v, (float, int)) else str(v))
            return "(" + ", ".join(parts) + ")"
    except Exception:
        pass
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(value)


def _render_multi_rows(ctx: InxGUIContext, rows):
    if not rows:
        return
    lw = max_label_w(ctx, [label for label, _value in rows])
    for label, value in rows:
        field_label(ctx, label, lw)
        ctx.label(value)


def _multi_enum_members(metadata):
    from Infernux.components.serialized_field import FieldType
    if metadata.field_type != FieldType.ENUM:
        return None
    enum_cls = metadata.enum_type
    if isinstance(enum_cls, str):
        import Infernux.lib as _lib
        enum_cls = getattr(_lib, enum_cls, None)
    return _get_enum_members(enum_cls) if enum_cls is not None else None


def _render_multi_batch(ctx: InxGUIContext, descriptors, label_width, apply_change):
    if not descriptors:
        return
    batch_descs = [entry[0] for entry in descriptors]
    changes = ctx.render_property_batch(batch_descs, label_width)
    if not changes:
        return
    for idx_key, raw_value in changes.items():
        idx = int(idx_key)
        _desc, metadata, enum_members, apply_token = descriptors[idx]
        new_value = _convert_batch_value(metadata.field_type, raw_value, enum_members)
        apply_change(apply_token, metadata, new_value)


def _apply_multi_builtin_change(wrapped, token, metadata, new_value):
    py_name, cpp_attr = token
    for comp in wrapped:
        try:
            old_value = getattr(comp, cpp_attr)
        except Exception:
            continue
        if has_field_changed(metadata.field_type, old_value, new_value):
            _record_builtin_property(comp, cpp_attr, old_value, new_value, f"Set {py_name}")


def _apply_multi_py_change(comps, field_name, metadata, new_value):
    for comp in comps:
        try:
            old_value = getattr(comp, field_name, metadata.default)
        except Exception:
            old_value = metadata.default
        if has_field_changed(metadata.field_type, old_value, new_value) and not metadata.readonly:
            _record_property(comp, field_name, old_value, new_value, f"Set {field_name}")
            if hasattr(comp, '_call_on_validate'):
                comp._call_on_validate()


def _metadata_for_json_value(value):
    from Infernux.components.serialized_field import FieldType
    field_type = None
    current = value
    if isinstance(value, bool):
        field_type = FieldType.BOOL
    elif isinstance(value, int) and not isinstance(value, bool):
        field_type = FieldType.INT
    elif isinstance(value, float):
        field_type = FieldType.FLOAT
    elif isinstance(value, str):
        field_type = FieldType.STRING
    elif isinstance(value, list) and len(value) in (2, 3, 4) and all(isinstance(v, (int, float)) for v in value):
        if len(value) == 2:
            from Infernux.lib import Vector2
            field_type = FieldType.VEC2
            current = Vector2(float(value[0]), float(value[1]))
        elif len(value) == 3:
            from Infernux.lib import Vector3
            field_type = FieldType.VEC3
            current = Vector3(float(value[0]), float(value[1]), float(value[2]))
        else:
            from Infernux.lib import vec4f
            field_type = FieldType.VEC4
            current = vec4f(float(value[0]), float(value[1]), float(value[2]), float(value[3]))
    if field_type is None:
        return None, value
    metadata = SimpleNamespace(
        field_type=field_type,
        readonly=False,
        range=None,
        slider=False,
        drag_speed=None,
        multiline=False,
        enum_type=None,
        enum_labels=None,
        tooltip="",
    )
    return metadata, current


def _json_value_from_batch(metadata, new_value):
    from Infernux.components.serialized_field import FieldType
    if metadata.field_type in (FieldType.VEC2, FieldType.VEC3, FieldType.VEC4):
        count = 2 if metadata.field_type == FieldType.VEC2 else 3 if metadata.field_type == FieldType.VEC3 else 4
        return [float(new_value[i]) for i in range(count)]
    return new_value


def _apply_multi_json_change(comps, key, metadata, new_value):
    json_value = _json_value_from_batch(metadata, new_value)
    for comp in comps:
        try:
            original_document = comp.serialize_document()
            data = dict(original_document)
        except Exception:
            continue
        old_value = data.get(key)
        if _multi_value_equal(old_value, json_value):
            continue
        data[key] = json_value
        _record_generic_component(comp, original_document, data)


def render_multi_component(ctx: InxGUIContext, comps, *, is_native: bool):
    """Render common component fields for Inspector multi-selection.

    Mixed scalar values are rendered by the same serialized-field controls as
    normal values, with ``--`` drawn inside the field. Editing that field writes
    the new value to every selected component.
    """
    comps = [c for c in comps if c is not None]
    if not comps:
        return

    from Infernux.components.builtin_component import BuiltinComponent
    from Infernux.components.serialized_field import get_serialized_fields

    first = comps[0]
    wrapper_cls = BuiltinComponent._builtin_registry.get(getattr(first, "type_name", "")) if is_native else None
    if wrapper_cls:
        wrapped = []
        for comp in comps:
            try:
                if isinstance(comp, BuiltinComponent):
                    wrapped.append(comp)
                else:
                    go = getattr(comp, "game_object", None)
                    wrapped.append(wrapper_cls._get_or_create_wrapper(comp, go) if go is not None else comp)
            except Exception:
                wrapped.append(comp)
        props = _collect_cpp_properties(wrapper_cls)
        labels = [pretty_field_name(name) for name, _ in props]
        lw = max_label_w(ctx, labels) if labels else 0.0
        descriptors = []
        rows = []
        for py_name, cpp_prop in props:
            meta = cpp_prop.metadata
            if getattr(meta, 'hidden', False):
                continue

            if meta.visible_when is not None:
                try:
                    if not all(meta.visible_when(comp) for comp in wrapped):
                        continue
                except Exception:
                    pass
            values = []
            for comp in wrapped:
                try:
                    values.append(getattr(comp, cpp_prop.cpp_attr))
                except Exception:
                    values.append(None)
            mixed = not _all_multi_values_equal(values)
            if meta.readonly:
                display = _format_multi_value(values[0]) if not mixed else "--"
                rows.append((pretty_field_name(py_name), display))
                continue
            desc = build_scalar_desc(
                f"##multi_{py_name}", pretty_field_name(py_name), meta, values[0], mixed=mixed,
            )
            if desc is None:
                display = _format_multi_value(values[0]) if not mixed else "--"
                rows.append((pretty_field_name(py_name), display))
                continue
            descriptors.append((desc, meta, _multi_enum_members(meta), (py_name, cpp_prop.cpp_attr)))
        _render_multi_batch(
            ctx, descriptors, lw,
            lambda token, meta, value: _apply_multi_builtin_change(wrapped, token, meta, value),
        )
        _render_multi_rows(ctx, rows)
        return

    if not is_native and isinstance(first, InxComponent):
        fields = get_serialized_fields(first.__class__)
        labels = [pretty_field_name(name) for name in fields]
        lw = max_label_w(ctx, labels) if labels else 0.0
        descriptors = []
        rows = []
        for field_name, metadata in fields.items():
            if getattr(metadata, 'hidden', False):
                continue

            if metadata.visible_when is not None:
                try:
                    if not all(metadata.visible_when(comp) for comp in comps):
                        continue
                except Exception:
                    pass
            values = []
            for comp in comps:
                try:
                    values.append(getattr(comp, field_name, metadata.default))
                except Exception:
                    values.append(metadata.default)
            mixed = not _all_multi_values_equal(values)
            if metadata.readonly:
                display = _format_multi_value(values[0]) if not mixed else "--"
                rows.append((pretty_field_name(field_name), display))
                continue
            desc = build_scalar_desc(
                f"##multi_{field_name}", pretty_field_name(field_name), metadata, values[0], mixed=mixed,
            )
            if desc is None:
                display = _format_multi_value(values[0]) if not mixed else "--"
                rows.append((pretty_field_name(field_name), display))
                continue
            descriptors.append((desc, metadata, _multi_enum_members(metadata), field_name))
        _render_multi_batch(
            ctx, descriptors, lw,
            lambda token, meta, value: _apply_multi_py_change(comps, token, meta, value),
        )
        _render_multi_rows(ctx, rows)
        return

    ignore_keys = {"type", "enabled", "component_id"}
    serialized = []
    for comp in comps:
        try:
            serialized.append(comp.serialize_document())
        except Exception:
            serialized.append({})
    common_keys = [k for k in serialized[0].keys() if k not in ignore_keys]
    for data in serialized[1:]:
        common_keys = [k for k in common_keys if k in data and k not in ignore_keys]
    labels = [pretty_field_name(key) for key in common_keys]
    lw = max_label_w(ctx, labels) if labels else 0.0
    descriptors = []
    rows = []
    for key in common_keys:
        values = [data.get(key) for data in serialized]
        mixed = not _all_multi_values_equal(values)
        metadata, current_value = _metadata_for_json_value(values[0])
        if metadata is None:
            display = _format_multi_value(values[0]) if not mixed else "--"
            rows.append((pretty_field_name(key), display))
            continue
        desc = build_scalar_desc(
            f"##multi_{key}", pretty_field_name(key), metadata, current_value, mixed=mixed,
        )
        if desc is None:
            display = _format_multi_value(values[0]) if not mixed else "--"
            rows.append((pretty_field_name(key), display))
            continue
        descriptors.append((desc, metadata, None, key))
    _render_multi_batch(
        ctx, descriptors, lw,
        lambda token, meta, value: _apply_multi_json_change(comps, token, meta, value),
    )
    _render_multi_rows(ctx, rows)


# ============================================================================
# Built-in component renderers
# ============================================================================


def render_transform_component(ctx: InxGUIContext, trans):
    """Render Transform component fields (Position, Rotation, Scale).
    
    Displays LOCAL values in the inspector (matching Unity convention where
    the Inspector shows localPosition / localEulerAngles / localScale).
    Uses a dedicated C++ function to render all 3 vector3 controls in one
    bridge call — no tuple/descriptor overhead.
    """
    from Infernux.lib import Vector3
    lw = max_label_w(ctx, ["Position", "Rotation", "Scale"])

    pos = trans.local_position
    px, py, pz = pos[0], pos[1], pos[2]
    rot = trans.local_euler_angles
    rx, ry, rz = rot[0], rot[1], rot[2]
    scl = trans.local_scale
    sx, sy, sz = scl[0], scl[1], scl[2]

    r = ctx.render_transform_fields(
        px, py, pz, rx, ry, rz, sx, sy, sz,
        DRAG_SPEED_DEFAULT, DRAG_SPEED_DEFAULT, DRAG_SPEED_FINE, lw,
    )
    npx, npy, npz = r[0], r[1], r[2]
    nrx, nry, nrz = r[3], r[4], r[5]
    nsx, nsy, nsz = r[6], r[7], r[8]

    if any(not _float_close(a, b) for a, b in [(npx, px), (npy, py), (npz, pz)]):
        _record_property(trans, "local_position", pos, Vector3(npx, npy, npz), "Set Position")
    if any(not _float_close(a, b) for a, b in [(nrx, rx), (nry, ry), (nrz, rz)]):
        _record_property(trans, "local_euler_angles", rot, Vector3(nrx, nry, nrz), "Set Rotation")
    if any(not _float_close(a, b) for a, b in [(nsx, sx), (nsy, sy), (nsz, sz)]):
        _record_property(trans, "local_scale", scl, Vector3(nsx, nsy, nsz), "Set Scale")


# ============================================================================
# BuiltinComponent property-setter renderer
# ============================================================================

_CPP_PROPS_CACHE: dict = {}  # wrapper_cls -> list[(attr_name, CppProperty)]


def _collect_cpp_properties(wrapper_cls):
    """Collect CppProperty descriptors from *wrapper_cls* MRO (top→base).

    Returns a list of ``(python_attr_name, CppProperty)`` in definition
    order, skipping duplicates.  Results are cached per class.
    """
    cached = _CPP_PROPS_CACHE.get(wrapper_cls)
    if cached is not None:
        return cached
    seen = set()
    result = []
    # Walk the MRO in reverse so that the most-derived class wins
    for cls in reversed(wrapper_cls.__mro__):
        for attr_name, attr in cls.__dict__.items():
            if attr_name.startswith("_"):
                continue
            if getattr(attr, "_is_cpp_property", False) and attr_name not in seen:
                seen.add(attr_name)
                result.append((attr_name, attr))
    _CPP_PROPS_CACHE[wrapper_cls] = result
    return result


def render_builtin_via_setters(ctx: InxGUIContext, comp, wrapper_cls, *, skip_fields=None):
    """Render a C++ component by iterating CppProperty descriptors.

    If *comp* is a raw C++ component, it is wrapped in a BuiltinComponent
    wrapper so that CppProperty converters (e.g. COLOR get/set) are applied.

    Uses C++ batch property renderer for scalar fields to minimize pybind11
    overhead.  Non-scalar fields (ASSET refs) are rendered individually.

    Args:
        skip_fields: Optional set of Python attribute names to skip.
    """
    from Infernux.components.serialized_field import FieldType
    from Infernux.components.builtin_component import BuiltinComponent

    # Ensure we have a Python wrapper for correct CppProperty behavior
    if not isinstance(comp, BuiltinComponent):
        go = getattr(comp, 'game_object', None)
        if go is not None:
            comp = wrapper_cls._get_or_create_wrapper(comp, go)

    props = _collect_cpp_properties(wrapper_cls)
    if not props:
        # Fallback — no descriptors found
        render_cpp_component_generic(ctx, comp)
        return

    labels = [pretty_field_name(name) for name, _ in props]
    lw = max_label_w(ctx, labels)
    cache_entry, refresh_values = _begin_component_value_cache("builtin", comp)
    skip_key = tuple(sorted(skip_fields)) if skip_fields else ()
    capture_semantics = semantic_capture_enabled(ctx)
    plan = None if refresh_values else cache_entry.get("builtin_plan")
    if (plan is None or plan.get("skip_fields") != skip_key
            or plan.get("semantic_capture", False) != capture_semantics):
        if plan is None:
            _record_profile_count("bodyBuiltinPlanMiss_count")
        else:
            _record_profile_count("bodyBuiltinPlanSkipMismatch_count")
        _record_profile_count("bodyBuiltinPlanBuild_count")
        _plan_t0 = _profile_start()
        plan = _build_builtin_cached_plan(ctx, comp, props, lw, skip_fields, cache_entry, refresh_values)
        _record_profile_timing("bodyBuiltinPlanBuild", _plan_t0)
        cache_entry["builtin_plan"] = plan
    else:
        _record_profile_count("bodyBuiltinPlanHit_count")

    _record_profile_count("bodyBuiltinPlanReplay_count")
    _replay_t0 = _profile_start()
    _replay_builtin_cached_plan(ctx, comp, plan, cache_entry)
    _record_profile_timing("bodyBuiltinPlanReplay", _replay_t0)

    # Append extra renderer if registered (e.g. AudioSource per-track section)
    extra = _COMPONENT_EXTRA_RENDERERS.get(getattr(comp, 'type_name', ''))
    if extra:
        _record_profile_count("bodyBuiltinExtra_count")
        _extra_t0 = _profile_start()
        extra(ctx, comp)
        _record_profile_timing("bodyBuiltinExtra", _extra_t0)


def _convert_batch_value(field_type, raw_value, enum_members):
    """Convert a raw value from C++ batch renderer to the correct Python type."""
    from Infernux.components.serialized_field import FieldType
    if field_type == FieldType.VEC2:
        from Infernux.lib import Vector2
        return Vector2(raw_value[0], raw_value[1])
    if field_type == FieldType.VEC3:
        from Infernux.lib import Vector3
        return Vector3(raw_value[0], raw_value[1], raw_value[2])
    if field_type == FieldType.VEC4:
        from Infernux.lib import vec4f
        return vec4f(raw_value[0], raw_value[1], raw_value[2], raw_value[3])
    if field_type == FieldType.ENUM and enum_members:
        idx = int(raw_value)
        if 0 <= idx < len(enum_members):
            return enum_members[idx]
    if field_type == FieldType.COLOR:
        from Infernux.components.serialized_field import normalize_rgba
        return normalize_rgba([
            raw_value[0], raw_value[1], raw_value[2], raw_value[3],
        ])
    return raw_value


def _apply_batch_changes_builtin(comp, changes: dict, batch_info: list):
    """Apply changes from C++ batch renderer to a BuiltinComponent."""
    for idx_key, raw_value in changes.items():
        idx = int(idx_key)
        py_name, cpp_attr, meta, old_value, enum_members = batch_info[idx]
        new_value = _convert_batch_value(meta.field_type, raw_value, enum_members)
        _record_builtin_property(comp, cpp_attr, old_value, new_value, f"Set {py_name}")


def _apply_batch_changes_py(py_comp, changes: dict, batch_info: list):
    """Apply changes from C++ batch renderer to a Python InxComponent."""
    for idx_key, raw_value in changes.items():
        idx = int(idx_key)
        field_name, meta, old_value, enum_members = batch_info[idx]
        new_value = _convert_batch_value(meta.field_type, raw_value, enum_members)
        if has_field_changed(meta.field_type, old_value, new_value) and not meta.readonly:
            from ._inspector_undo import _record_python_component_document_edit
            _record_python_component_document_edit(
                py_comp,
                lambda _name=field_name, _value=new_value: setattr(py_comp, _name, _value),
                f"Set {field_name}",
                edit_key=field_name,
                validate=True,
            )


def render_cpp_component_generic(ctx: InxGUIContext, comp):
    """Render generic fields for a native component document."""
    original_document = comp.serialize_document()
    data = dict(original_document)

    ignore_keys = {"type", "enabled", "component_id"}
    changed = False

    visible_keys = [k for k in data if k not in ignore_keys]
    lw = max_label_w(ctx, [pretty_field_name(k) for k in visible_keys]) if visible_keys else 0.0

    for key, value in data.items():
        if key in ignore_keys:
            continue

        new_value = value
        if isinstance(value, bool):
            new_value = render_inspector_checkbox(ctx, pretty_field_name(key), bool(value))
        elif isinstance(value, int):
            field_label(ctx, pretty_field_name(key), lw)
            new_value = int(ctx.drag_int(f"##{key}", int(value), DRAG_SPEED_INT, -1000000, 1000000))
        elif isinstance(value, float):
            field_label(ctx, pretty_field_name(key), lw)
            new_value = float(ctx.drag_float(f"##{key}", float(value), DRAG_SPEED_DEFAULT, -1e6, 1e6))
        elif isinstance(value, str):
            field_label(ctx, pretty_field_name(key), lw)
            new_value = ctx.text_input(f"##{key}", value, 256)
        elif isinstance(value, list):
            if len(value) == 2 and all(isinstance(v, (int, float)) for v in value):
                nx, ny = ctx.vector2(pretty_field_name(key), float(value[0]), float(value[1]), DRAG_SPEED_DEFAULT, lw)
                new_value = [nx, ny]
            elif len(value) == 3 and all(isinstance(v, (int, float)) for v in value):
                nx, ny, nz = ctx.vector3(pretty_field_name(key), float(value[0]), float(value[1]), float(value[2]), DRAG_SPEED_DEFAULT, lw)
                new_value = [nx, ny, nz]
            elif len(value) == 4 and all(isinstance(v, (int, float)) for v in value):
                nx, ny, nz, nw = ctx.vector4(pretty_field_name(key), float(value[0]), float(value[1]), float(value[2]), float(value[3]), DRAG_SPEED_DEFAULT, lw)
                new_value = [nx, ny, nz, nw]
            else:
                ctx.label(f"{pretty_field_name(key)}: {value}")
        else:
            ctx.label(f"{key}: {value}")

        # Detect change — use tolerance for floats to avoid phantom edits
        if isinstance(value, float) and isinstance(new_value, float):
            value_changed = not _float_close(new_value, value)
        elif isinstance(value, list) and isinstance(new_value, list):
            value_changed = any(
                not _float_close(float(a), float(b))
                for a, b in zip(new_value, value)
                if isinstance(a, (int, float)) and isinstance(b, (int, float))
            ) or (len(new_value) != len(value))
        else:
            value_changed = (new_value != value)

        if value_changed:
            data[key] = new_value
            changed = True

    if changed:
        _record_generic_component(comp, original_document, data)


# ── Asset-type reference field configuration ──
_ASSET_REF_CONFIG = None  # lazy-initialized (needs FieldType import)


def _try_custom_py_renderer(ctx, py_comp):
    """Try custom on_inspector_gui override or registered renderer. Returns True if handled."""
    on_gui = getattr(type(py_comp), 'on_inspector_gui', None)
    if on_gui is not None:
        from Infernux.components.component import InxComponent
        if on_gui is not InxComponent.on_inspector_gui:
            _record_profile_count("bodyPyCustom_count")
            _py_custom_t0 = _profile_start()
            try:
                py_comp.on_inspector_gui(ctx)
            finally:
                _record_profile_timing("bodyPyCustom", _py_custom_t0)
            return True
    renderer = _PY_COMPONENT_RENDERERS.get(py_comp.type_name)
    if renderer:
        _record_profile_count("bodyPyCustom_count")
        _py_custom_t0 = _profile_start()
        try:
            renderer(ctx, py_comp)
        finally:
            _record_profile_timing("bodyPyCustom", _py_custom_t0)
        return True
    return False


def _get_py_field_value(py_comp, field_name, metadata, cache_entry, refresh_values, _REF_TYPES):
    """Get the current value for a Python component field."""
    try:
        if metadata.field_type in _REF_TYPES:
            from Infernux.components.serialized_field import get_raw_field_value
            return get_raw_field_value(py_comp, field_name)
        return _get_cached_component_value(
            cache_entry, refresh_values, field_name,
            lambda _fn=field_name, _default=metadata.default: getattr(py_comp, _fn, _default),
        )
    except RuntimeError:
        return metadata.default


def _serialized_field_label(field_name, metadata) -> str:
    key = str(getattr(metadata, "display_name_key", "") or "")
    if key:
        translated = t(key)
        if translated != key:
            return translated
    return pretty_field_name(field_name)


def _render_py_nonscalar_field(ctx, py_comp, field_name, metadata, current_value, lw, flush_fn):
    """Render a non-scalar field (list, serializable object, component ref, etc.). Returns True if handled."""
    from Infernux.components.serialized_field import FieldType
    ft = metadata.field_type
    if ft == FieldType.LIST:
        flush_fn()
        from Infernux.components.serialized_field import get_raw_field_value
        _raw_list = get_raw_field_value(py_comp, field_name)
        _render_list_field(ctx, py_comp, field_name, metadata, _raw_list, lw)
        _tooltip_and_info(ctx, metadata)
        return True
    if ft == FieldType.SERIALIZABLE_OBJECT:
        flush_fn()
        _render_serializable_object_field(ctx, py_comp, field_name, metadata, current_value, lw)
        _tooltip_and_info(ctx, metadata)
        return True
    if ft == FieldType.COMPONENT:
        flush_fn()
        _render_component_ref_inline(ctx, py_comp, field_name, metadata, lw)
        _tooltip_and_info(ctx, metadata)
        return True
    if ft == FieldType.GAME_OBJECT:
        flush_fn()
        _render_gameobject_ref_inline(ctx, py_comp, field_name, metadata, current_value, lw)
        _tooltip_and_info(ctx, metadata)
        return True
    if ft in _get_asset_ref_config():
        flush_fn()
        _render_asset_reference_field(ctx, py_comp, field_name, metadata, current_value, metadata.field_type, lw)
        _tooltip_and_info(ctx, metadata)
        return True
    return False


def render_py_component(ctx: InxGUIContext, py_comp):
    """Render a Python InxComponent's serialized fields."""
    from .inspector_declarative import get_inspector_model, render_inspector_model

    declarative_model = get_inspector_model(py_comp)
    if declarative_model is not None:
        render_inspector_model(ctx, py_comp, declarative_model)
        return
    if _try_custom_py_renderer(ctx, py_comp):
        return

    from Infernux.components.serialized_field import get_serialized_fields, FieldType

    fields = get_serialized_fields(py_comp.__class__)
    lw = max_label_w(
        ctx,
        [_serialized_field_label(name, metadata) for name, metadata in fields.items()],
    ) if fields else 0.0
    cache_entry, refresh_values = _begin_component_value_cache("py", py_comp)
    capture_semantics = semantic_capture_enabled(ctx)
    _record_profile_count("bodyPyGenericTotal_count")
    _py_generic_t0 = _profile_start()

    batch_descs = []
    batch_info = []

    def _flush():
        nonlocal batch_descs, batch_info, refresh_values
        if not batch_descs:
            return
        _record_profile_count("bodyPyGenericBatch_count")
        _batch_t0 = _profile_start()
        changes = ctx.render_property_batch(batch_descs, lw)
        _record_profile_timing("bodyPyGenericBatch", _batch_t0)
        if changes:
            _apply_batch_changes_py(py_comp, changes, batch_info)
            _invalidate_component_value_cache(cache_entry)
            refresh_values = True
        batch_descs = []
        batch_info = []

    _current_group: str = ""
    _group_visible: bool = True
    _REF_TYPES = (
        FieldType.GAME_OBJECT, FieldType.MATERIAL, FieldType.TEXTURE,
        FieldType.SHADER, FieldType.ASSET, FieldType.COMPONENT,
    )

    for field_name, metadata in fields.items():
        field_group = metadata.group or ""
        if field_group != _current_group:
            _flush()
            _current_group = field_group
            if field_group:
                _group_visible = render_compact_section_header(ctx, field_group, level="secondary")
            else:
                _group_visible = True
        if not _group_visible:
            continue

        if getattr(metadata, 'hidden', False):
            continue

        if metadata.visible_when is not None:
            try:
                if not metadata.visible_when(py_comp):
                    continue
            except (RuntimeError, TypeError) as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                pass

        current_value = _get_py_field_value(py_comp, field_name, metadata, cache_entry, refresh_values, _REF_TYPES)

        if metadata.readonly and metadata.field_type in (FieldType.INT, FieldType.FLOAT, FieldType.STRING, FieldType.BOOL):
            _flush()
            field_label(ctx, _serialized_field_label(field_name, metadata), lw)
            ctx.label(f"{current_value}")
            _tooltip_and_info(ctx, metadata)
            continue

        if _render_py_nonscalar_field(ctx, py_comp, field_name, metadata, current_value, lw, _flush):
            continue

        # ── Scalar field → try batch ──
        hdr = metadata.header or ""
        spc = metadata.space if metadata.space and metadata.space > 0 else 0.0

        desc = build_scalar_desc(
            f"##{field_name}", _serialized_field_label(field_name, metadata), metadata, current_value,
            header_text=hdr, space_before=spc,
            semantic_id=(inspector_component_semantic_id(py_comp, field_name)
                         if capture_semantics else ""),
        )
        if desc is not None:
            enum_members = None
            if metadata.field_type == FieldType.ENUM:
                enum_cls = metadata.enum_type
                if isinstance(enum_cls, str):
                    import Infernux.lib as _lib
                    enum_cls = getattr(_lib, enum_cls, None)
                if enum_cls is not None:
                    enum_members = _get_enum_members(enum_cls)
            batch_descs.append(desc)
            batch_info.append((field_name, metadata, current_value, enum_members))
        else:
            _flush()
            if hdr:
                ctx.separator()
                ctx.label(hdr)
            if spc > 0:
                ctx.dummy(0, spc)
            new_value = render_serialized_field(
                ctx, f"##{field_name}", _serialized_field_label(field_name, metadata), metadata, current_value, lw,
            )
            record_inspector_component_item(
                ctx, py_comp, field_name, "inspector_field", _serialized_field_label(field_name, metadata),
            )
            if has_field_changed(metadata.field_type, current_value, new_value) and not metadata.readonly:
                from ._inspector_undo import _record_python_component_document_edit
                _record_python_component_document_edit(
                    py_comp,
                    lambda _name=field_name, _value=new_value: setattr(py_comp, _name, _value),
                    f"Set {field_name}",
                    edit_key=field_name,
                    validate=True,
                )
                _invalidate_component_value_cache(cache_entry)
                refresh_values = True
            _tooltip_and_info(ctx, metadata)

        if desc is not None and metadata.info_text:
            _render_info_text(ctx, metadata.info_text)

    _flush()
    extra_renderer = _PY_COMPONENT_EXTRA_RENDERERS.get(py_comp.type_name)
    if extra_renderer is not None:
        extra_renderer(ctx, py_comp)
    _record_profile_timing("bodyPyGenericTotal", _py_generic_t0)


# ============================================================================
# Auto-register built-in component renderers
# ============================================================================
register_component_renderer("Transform", render_transform_component)
register_component_extra_renderer("AudioSource", _render_audio_source_extra)
register_component_extra_renderer("MeshRenderer", _render_mesh_renderer_materials)
register_component_extra_renderer("SkinnedMeshRenderer", _render_mesh_renderer_materials)
register_py_component_extra_renderer("ParticleSystem", _render_particle_system_parameters)

# Registers UI component inspectors.
from . import inspector_ui_components
from . import inspector_renderstack
