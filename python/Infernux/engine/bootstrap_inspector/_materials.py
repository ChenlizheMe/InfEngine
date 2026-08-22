"""Material-section rendering callback wiring."""
from __future__ import annotations

from Infernux.debug import Debug


class _InlineMaterialPanelAdapter:
    """Stable adapter shared by inline material rows in one Inspector."""

    def __init__(self, state, engine, inspector_support):
        self._state = state
        self._engine = engine
        self._inspector_support = inspector_support

    @property
    def _inline_material_cache(self):
        return self._state["cache"]

    @_inline_material_cache.setter
    def _inline_material_cache(self, value):
        self._state["cache"] = value

    @property
    def _inline_material_exec_layer(self):
        return self._state["exec_layer"]

    @_inline_material_exec_layer.setter
    def _inline_material_exec_layer(self, value):
        self._state["exec_layer"] = value

    def _get_native_engine(self):
        return self._engine.get_native_engine()

    def _ensure_material_file_path(self, material):
        return self._inspector_support.ensure_material_file_path(material)


def _collect_material_renderers(items, native_map, obj):
    """Collect renderer tuples (MeshRenderer / SpriteRenderer) and their signature parts."""
    from Infernux.components.builtin_component import BuiltinComponent

    _RENDERER_TYPES = {"MeshRenderer", "SkinnedMeshRenderer", "SpriteRenderer"}

    renderers = []
    signature_parts = []
    for item in items:
        if not item.is_native or item.type_name not in _RENDERER_TYPES:
            continue
        renderer = native_map.get(item.component_id)
        if renderer is None:
            continue
        wclass = BuiltinComponent._builtin_registry.get(item.type_name)
        if wclass is not None and not isinstance(renderer, BuiltinComponent):
            try:
                renderer = wclass._get_or_create_wrapper(renderer, obj)
            except Exception as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        mat_count = getattr(renderer, 'material_count', 0) or 1
        try:
            material_guids = tuple(renderer.get_material_guids() or [])
        except Exception:
            material_guids = ()
        try:
            slot_names = tuple(renderer.get_material_slot_names() or [])
        except Exception:
            slot_names = ()
        renderers.append((renderer, mat_count, material_guids, slot_names))
        signature_parts.append((
            getattr(renderer, 'component_id', id(renderer)),
            mat_count, material_guids, slot_names,
        ))
    return renderers, tuple(signature_parts)


def _rebuild_material_entries(renderers):
    """Build the valid_entries list from collected renderers."""
    valid_entries = []
    for renderer, mat_count, material_guids, slot_names in renderers:
        renderer_type = getattr(renderer, "type_name", "") or ""
        for slot_idx in range(mat_count):
            try:
                mat = renderer.get_effective_material(slot_idx)
            except Exception:
                mat = None
            if mat is None:
                continue
            if slot_idx < len(slot_names) and slot_names[slot_idx]:
                label = f"{slot_names[slot_idx]} (Slot {slot_idx})"
            else:
                label = f"Element {slot_idx}"
            mat_path = getattr(mat, "file_path", "") if mat is not None else ""
            is_embedded = isinstance(mat_path, str) and "::submat:" in mat_path
            is_default = (slot_idx >= len(material_guids) or not material_guids[slot_idx]) and not is_embedded
            valid_entries.append({
                "label": label,
                "material": mat,
                "is_default": is_default,
                "is_embedded": is_embedded,
                "renderer_type": renderer_type,
            })
    return valid_entries


def wire_material_sections(ip, _t, engine, _inspector_support,
                           get_cached_maps, current_scene_versions,
                           mat_cache):
    """Wire material-section rendering callback onto *ip*."""
    _inline_material_state = {"cache": {}, "exec_layer": None}
    inline_material_adapter = _InlineMaterialPanelAdapter(
        _inline_material_state, engine, _inspector_support,
    )

    _material_section_heights = {}
    _material_render_error = {"fingerprint": ""}

    def _render_material_sections_live(ctx, obj_id):
        from Infernux.components.builtin_component import BuiltinComponent
        from Infernux.engine.ui import inspector_material as mat_ui
        from Infernux.engine.ui.inspector_utils import render_compact_section_header, render_info_text
        from Infernux.engine.ui.theme import Theme, ImGuiCol, ImGuiStyleVar

        scene, items, native_map, _py_map = get_cached_maps(obj_id)
        obj = scene.find_by_id(obj_id) if scene else None
        if obj is None:
            return

        renderers, signature = _collect_material_renderers(
            items, native_map, obj)

        if not renderers:
            return

        if not render_compact_section_header(
            ctx, "Materials##obj_mat_sections", level="primary", default_open=True
        ):
            return

        _scene, scene_version, structure_version = current_scene_versions(obj_id)
        if (
            mat_cache["object_id"] == obj_id
            and mat_cache["scene_version"] == scene_version
            and mat_cache["structure_version"] == structure_version
            and mat_cache["signature"] == signature
        ):
            valid_entries = mat_cache["entries"]
        else:
            valid_entries = _rebuild_material_entries(renderers)
            mat_cache["object_id"] = obj_id
            mat_cache["scene_version"] = scene_version
            mat_cache["structure_version"] = structure_version
            mat_cache["signature"] = signature
            mat_cache["entries"] = valid_entries

        owner_name = getattr(obj, 'name', '') or ''
        multiple_renderers = len(renderers) > 1

        if not valid_entries:
            return

        ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding, *Theme.INSPECTOR_FRAME_PAD)
        ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing, *Theme.INSPECTOR_ITEM_SPC)
        try:
            for index, entry in enumerate(valid_entries):
                title = entry["label"]
                if multiple_renderers and owner_name:
                    title = f"{owner_name} / {title}"
                if not render_compact_section_header(
                    ctx, f"{title}##mat_entry_{index}", level="secondary", default_open=True
                ):
                    continue
                lock_inline_material_body = (
                    entry.get("renderer_type") == "SpriteRenderer"
                    and entry["is_default"]
                ) or bool(entry.get("is_embedded", False))
                ctx.push_id(index)
                try:
                    if lock_inline_material_body:
                        ctx.begin_disabled(True)
                    try:
                        mat_ui.render_inline_material_body(
                            ctx, inline_material_adapter, entry["material"],
                            cache_key=f"obj_mat_{obj_id}_{index}")
                    finally:
                        if lock_inline_material_body:
                            ctx.end_disabled()
                finally:
                    ctx.pop_id()

                if index != len(valid_entries) - 1:
                    ctx.separator()
        finally:
            ctx.pop_style_var(2)

        try:
            native = engine.get_native_engine()
            if native is not None and hasattr(native, "pump_preview_tasks"):
                native.pump_preview_tasks()
        except Exception:
            pass

    def _render_material_sections(ctx, obj_id):
        cached_height = _material_section_heights.get(obj_id, 0.0)
        visibility_query = getattr(ctx, "is_virtualized_region_visible", None)
        if cached_height > 0.0 and callable(visibility_query) and not visibility_query(cached_height):
            ctx.dummy(0.0, cached_height)
            return

        start_y = ctx.get_cursor_pos_y()
        try:
            try:
                _render_material_sections_live(ctx, obj_id)
                _material_render_error["fingerprint"] = ""
            except Exception as exc:
                # Python callbacks must never unwind through the native
                # Inspector frame.  Besides losing the rest of the panel, an
                # escaping exception prevents ImGui child recovery and can
                # turn one missing optional module into an error every frame.
                fingerprint = f"{type(exc).__name__}: {exc}"
                if _material_render_error["fingerprint"] != fingerprint:
                    Debug.log_error(
                        "Inspector material section failed; the rest of the "
                        f"Inspector remains available: {fingerprint}"
                    )
                    _material_render_error["fingerprint"] = fingerprint
                try:
                    ctx.text_wrapped("Material Inspector is temporarily unavailable.")
                except Exception:
                    pass
        finally:
            measured_height = max(0.0, ctx.get_cursor_pos_y() - start_y)
            if measured_height > 0.0:
                _material_section_heights[obj_id] = measured_height
            else:
                _material_section_heights.pop(obj_id, None)

    ip.render_material_sections = _render_material_sections
