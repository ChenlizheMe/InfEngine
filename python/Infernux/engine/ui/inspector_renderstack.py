"""Custom Inspector renderer for the RenderStack component.

Displays the pipeline topology with injection points, each having an
'Add Effect' button to add post-processing effects via a categorised popup menu
(similar to Unity's GlobalVolume system).

Mounted effects are displayed as collapsible sections with editable
parameters, regardless of whether the effect is enabled or disabled.
"""

from __future__ import annotations

import time as _time
from typing import Dict, List, TYPE_CHECKING

from Infernux.lib import InxGUIContext
from Infernux.engine.i18n import t
from . import inspector_support as _inspector_support
from .inspector_utils import (
    max_label_w, field_label, render_serialized_field, has_field_changed,
    render_compact_section_header, render_info_text, render_inspector_checkbox, pretty_field_name,
    format_display_name, inspector_component_semantic_id, semantic_capture_enabled,
    DRAG_SPEED_DEFAULT, MIN_LABEL_WIDTH,
)
from .theme import Theme, ImGuiCol, ImGuiStyleVar, ImGuiTreeNodeFlags

if TYPE_CHECKING:
    from Infernux.renderstack.render_stack import RenderStack, PassEntry


class _EffectStageSlotsAdapter:
    """Expose one stage list as an undoable property for the shared list UI."""

    def __init__(self, stack: "RenderStack", stage_id: str):
        self._stack = stack
        self._stage_id = stage_id

    @property
    def slots(self):
        return list(self._stack.get_effect_stage_slots(self._stage_id))

    @slots.setter
    def slots(self, value) -> None:
        self._stack.set_effect_stage_slots(self._stage_id, value)


def _effect_stage_adapter(stack: "RenderStack", stage_id: str) -> _EffectStageSlotsAdapter:
    adapters = getattr(stack, "_inspector_effect_stage_adapters", None)
    if not isinstance(adapters, dict):
        adapters = {}
        stack._inspector_effect_stage_adapters = adapters
    adapter = adapters.get(stage_id)
    if adapter is None:
        adapter = _EffectStageSlotsAdapter(stack, stage_id)
        adapters[stage_id] = adapter
    return adapter


def _undo_manager():
    from Infernux.engine.undo import UndoManager
    return UndoManager.instance()


def _record_profile_timing(bucket: str, start_time: float) -> None:
    _inspector_support.record_inspector_profile_timing(
        bucket, (_time.perf_counter() - start_time) * 1000.0,
    )


def _record_profile_count(bucket: str, amount: float = 1.0) -> None:
    _inspector_support.record_inspector_profile_count(bucket, amount)


def _record_stack_field(stack: "RenderStack", target, field_name: str,
                        old_value, new_value, description: str) -> None:
    from Infernux.engine.undo import RenderStackFieldCommand

    mgr = _undo_manager()
    if mgr and not mgr.is_executing and mgr.enabled:
        mgr.record(RenderStackFieldCommand(stack, target, field_name, old_value, new_value, description))


def _renderstack_semantic_id(stack: "RenderStack", suffix: str) -> str:
    """Build a stable semantic ID for RenderStack-owned Inspector controls."""
    return inspector_component_semantic_id(stack, f"renderstack.{suffix}")


def _record_renderstack_field_semantic(
    ctx: InxGUIContext,
    stack: "RenderStack",
    suffix: str,
    display_name: str,
    metadata,
    value,
) -> None:
    """Describe the previously rendered serialized-field widget on capture frames."""
    if not semantic_capture_enabled(ctx):
        return

    from Infernux.components.serialized_field import FieldType

    semantic_id = _renderstack_semantic_id(stack, suffix)
    if not semantic_id:
        return

    field_type = metadata.field_type
    kind = {
        FieldType.FLOAT: "float_slider" if getattr(metadata, "slider", False) else "drag_float",
        FieldType.INT: "int_slider" if getattr(metadata, "slider", False) else "drag_int",
        FieldType.BOOL: "checkbox",
        FieldType.STRING: "text_area" if getattr(metadata, "multiline", False) else "text_input",
        FieldType.VEC2: "vector",
        FieldType.VEC3: "vector",
        FieldType.VEC4: "vector",
        FieldType.ENUM: "combo",
        FieldType.COLOR: "color_edit",
    }.get(field_type, "property")
    enabled = not bool(getattr(metadata, "readonly", False))
    bool_value = bool(value) if field_type == FieldType.BOOL else None
    numeric_value = float(value) if field_type in (FieldType.FLOAT, FieldType.INT) else None
    string_value = str(value) if field_type in (FieldType.STRING, FieldType.ENUM) else None
    ctx.record_semantic_item(
        kind,
        display_name,
        enabled,
        semantic_id,
        bool_value,
        numeric_value,
        string_value,
    )


def _snapshot_injection_orders(stack: "RenderStack", injection_point: str) -> Dict[str, int]:
    return {
        entry.render_pass.name: entry.order
        for entry in stack.get_passes_at(injection_point)
    }


def _compute_reordered_orders(stack: "RenderStack", dragged_name: str, target_name: str):
    dragged_entry = None
    target_entry = None
    for entry in stack.pass_entries:
        if entry.render_pass.name == dragged_name:
            dragged_entry = entry
        if entry.render_pass.name == target_name:
            target_entry = entry
    if dragged_entry is None or target_entry is None:
        return None, None, None
    if dragged_entry.render_pass.injection_point != target_entry.render_pass.injection_point:
        return None, None, None

    injection_point = dragged_entry.render_pass.injection_point
    old_orders = _snapshot_injection_orders(stack, injection_point)
    ordered_names = [
        entry.render_pass.name
        for entry in stack.get_passes_at(injection_point)
        if entry.render_pass.name != dragged_name
    ]
    try:
        index = ordered_names.index(target_name)
    except ValueError:
        return None, None, None
    ordered_names.insert(index, dragged_name)
    new_orders = {name: (idx + 1) * 10 for idx, name in enumerate(ordered_names)}
    return injection_point, old_orders, new_orders


def _compute_insert_after_orders(stack: "RenderStack", dragged_name: str,
                                 after_name: str):
    """Compute new orders with *dragged_name* placed immediately AFTER *after_name*.

    Returns ``(injection_point, old_orders, new_orders)`` or ``(None, None, None)``.
    """
    dragged_entry = None
    after_entry = None
    for entry in stack.pass_entries:
        if entry.render_pass.name == dragged_name:
            dragged_entry = entry
        if entry.render_pass.name == after_name:
            after_entry = entry
    if dragged_entry is None or after_entry is None:
        return None, None, None
    if dragged_entry.render_pass.injection_point != after_entry.render_pass.injection_point:
        return None, None, None

    injection_point = dragged_entry.render_pass.injection_point
    old_orders = _snapshot_injection_orders(stack, injection_point)
    ordered_names = [
        entry.render_pass.name
        for entry in stack.get_passes_at(injection_point)
        if entry.render_pass.name != dragged_name
    ]
    try:
        index = ordered_names.index(after_name)
    except ValueError:
        return None, None, None
    ordered_names.insert(index + 1, dragged_name)
    new_orders = {name: (idx + 1) * 10 for idx, name in enumerate(ordered_names)}
    return injection_point, old_orders, new_orders

def _get_pass_candidates(ip_name: str, inspector_state: dict | None = None) -> Dict[str, type]:
    """Return all RenderPass classes valid for this injection point."""
    if inspector_state is not None:
        return inspector_state["pass_candidates_by_ip"].get(ip_name, {})
    from Infernux.renderstack.discovery import discover_passes

    candidates: Dict[str, type] = {}
    for name, cls in discover_passes().items():
        if cls.injection_point != ip_name:
            continue
        candidates[name] = cls
    return candidates


def _get_addable_pass_candidates(stack: "RenderStack", ip_name: str, inspector_state: dict | None = None) -> Dict[str, type]:
    """Return candidates that are not already mounted on the stack."""
    if inspector_state is not None:
        return inspector_state["addable_candidates_by_ip"].get(ip_name, {})
    mounted_names = {e.render_pass.name for e in stack.pass_entries}
    return {
        name: cls
        for name, cls in _get_pass_candidates(ip_name, inspector_state).items()
        if name not in mounted_names
    }


def _get_renderstack_inspector_state(stack: "RenderStack") -> dict:
    state = getattr(stack, "_inspector_renderstack_cache", None)

    state_t0 = _time.perf_counter()

    # ── Fast pre-check: topology_probe is cached by _build_full_topology_probe().
    # If the id() matches, no pipeline/pass/topology change has occurred.
    # Only then check the (much cheaper) mounted_signature. ──
    topology_probe = stack._build_full_topology_probe()
    topology_token = id(topology_probe)

    if (
        isinstance(state, dict)
        and state.get("topology_token") == topology_token
    ):
        # Topology cache alive (not invalidated).  Only check mounted state
        # (add/remove/reorder/toggle — the only mutations between invalidations).
        mounted_signature = tuple(
            (entry.render_pass.injection_point, entry.render_pass.name, entry.order, bool(entry.enabled))
            for entry in stack.pass_entries
        )
        if state.get("mounted_signature") == mounted_signature:
            _record_profile_count("renderstackStateHit_count")
            return state
    else:
        mounted_signature = tuple(
            (entry.render_pass.injection_point, entry.render_pass.name, entry.order, bool(entry.enabled))
            for entry in stack.pass_entries
        )

    _record_profile_count("renderstackStateMiss_count")

    # Full rebuild — discovery dicts and signature tuples only on miss.
    pipelines = stack.discover_pipelines()
    pipeline_signature = tuple(sorted(pipelines.keys()))

    from Infernux.renderstack.discovery import discover_passes
    passes = discover_passes()
    pass_signature = tuple(sorted(passes.keys()))

    pass_candidates_by_ip: dict[str, dict[str, type]] = {}
    for name, cls in passes.items():
        ip_name = getattr(cls, "injection_point", "") or ""
        if not ip_name:
            continue
        pass_candidates_by_ip.setdefault(ip_name, {})[name] = cls

    mounted_names = {entry.render_pass.name for entry in stack.pass_entries}
    addable_candidates_by_ip = {
        ip_name: {
            name: cls
            for name, cls in candidates.items()
            if name not in mounted_names
        }
        for ip_name, candidates in pass_candidates_by_ip.items()
    }

    ip_entries: dict[str, list] = {}
    for entry in stack.pass_entries:
        ip_entries.setdefault(entry.render_pass.injection_point, []).append(entry)
    for entries in ip_entries.values():
        entries.sort(key=lambda entry: entry.order)

    state = {
        "pipeline_signature": pipeline_signature,
        "pass_signature": pass_signature,
        "topology_token": topology_token,
        "mounted_signature": mounted_signature,
        "pipeline_names": ["Default Forward"] + sorted(
            name for name in pipelines if name != "Default Forward"
        ),
        "topology_probe": topology_probe,
        "display_to_name": {ip.display_name: ip.name for ip in topology_probe.injection_points},
        "pass_candidates_by_ip": pass_candidates_by_ip,
        "addable_candidates_by_ip": addable_candidates_by_ip,
        "ip_entries": ip_entries,
    }
    stack._inspector_renderstack_cache = state
    _record_profile_timing("renderstackStateBuild", state_t0)
    return state


def _render_pass_bar(ctx: InxGUIContext, label: str, uid: int) -> None:
    """Render a standard pipeline pass as a flat read-only bar."""
    display_label = format_display_name(label)
    ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding, *Theme.INSPECTOR_HEADER_PRIMARY_FRAME_PAD)
    ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing, *Theme.INSPECTOR_HEADER_ITEM_SPC)
    ctx.push_style_var_float(ImGuiStyleVar.FrameBorderSize, Theme.INSPECTOR_HEADER_BORDER_SIZE)
    ctx.set_window_font_scale(Theme.INSPECTOR_HEADER_PRIMARY_FONT_SCALE)
    ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
    ctx.push_style_color(ImGuiCol.Header, *Theme.INSPECTOR_HEADER_PRIMARY)
    ctx.push_style_color(ImGuiCol.HeaderHovered, *Theme.INSPECTOR_HEADER_PRIMARY_HOVERED)
    ctx.push_style_color(ImGuiCol.HeaderActive, *Theme.INSPECTOR_HEADER_PRIMARY_ACTIVE)
    ctx.tree_node_ex(
        f"{display_label}##pass_{uid}",
        ImGuiTreeNodeFlags.NoTreePushOnOpen
        | ImGuiTreeNodeFlags.Leaf
        | ImGuiTreeNodeFlags.Bullet
        | ImGuiTreeNodeFlags.SpanAvailWidth,
    )
    ctx.pop_style_color(4)
    ctx.set_window_font_scale(1.0)
    ctx.pop_style_var(3)


def render_renderstack_inspector(ctx: InxGUIContext, stack: "RenderStack") -> None:
    inspector_state = _get_renderstack_inspector_state(stack)
    ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding, *Theme.INSPECTOR_FRAME_PAD)
    ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing, *Theme.INSPECTOR_ITEM_SPC)
    section_t0 = _time.perf_counter()
    _render_pipeline(ctx, stack, inspector_state)
    _record_profile_timing("renderstackPipeline", section_t0)
    section_t0 = _time.perf_counter()
    _render_pipeline_params(ctx, stack)
    _record_profile_timing("renderstackPipelineParams", section_t0)
    ctx.separator()
    section_t0 = _time.perf_counter()
    _render_topology_with_effects(ctx, stack, inspector_state)
    _record_profile_timing("renderstackTopology", section_t0)
    ctx.pop_style_var(2)


# -- Pipeline selector ---------------------------------------------------

def _render_pipeline(ctx: InxGUIContext, stack: "RenderStack", inspector_state: dict | None = None) -> None:
    lw = max_label_w(ctx, [t("renderstack.pipeline")])
    names = inspector_state["pipeline_names"] if inspector_state is not None else [
        "Default Forward"
    ] + sorted(n for n in stack.discover_pipelines() if n != "Default Forward")
    cur = stack.pipeline_class_name or "Default Forward"
    if cur not in names:
        stack.set_pipeline("")
        cur = "Default Forward"
    idx = names.index(cur)
    field_label(ctx, t("renderstack.pipeline"), lw)
    new_idx = ctx.combo("##rs_pipeline", idx, names, -1)
    if new_idx != idx:
        sel = names[new_idx]
        new_pipeline = "" if sel == "Default Forward" else sel
        from Infernux.engine.undo import RenderStackSetPipelineCommand
        mgr = _undo_manager()
        if mgr and not mgr.is_executing and mgr.enabled:
            mgr.execute(RenderStackSetPipelineCommand(stack, stack.pipeline_class_name or "", new_pipeline))
        else:
            stack.set_pipeline(new_pipeline)


# -- Pipeline parameters -------------------------------------------------

def _render_pipeline_params(ctx: InxGUIContext, stack: "RenderStack") -> None:
    """Render editable serialized fields exposed by the current pipeline."""
    from Infernux.components.serialized_field import get_serialized_fields

    pipeline = stack.pipeline
    fields = get_serialized_fields(pipeline.__class__)
    if not fields:
        return

    ctx.separator()
    ctx.label(t("renderstack.pipeline_settings"))

    display_names = [pretty_field_name(n) for n in fields.keys()]
    lw = max_label_w(ctx, display_names) if fields else 0.0

    _current_group: str = ""
    _group_visible: bool = True

    for field_name, metadata in fields.items():
        # ── Collapsible group management ──
        field_group = getattr(metadata, 'group', "") or ""
        if field_group != _current_group:
            _current_group = field_group
            if field_group:
                _group_visible = render_compact_section_header(ctx, field_group, level="secondary")
            else:
                _group_visible = True

        if not _group_visible:
            continue

        if metadata.header:
            ctx.separator()
            ctx.label(metadata.header)
        if metadata.space > 0:
            ctx.dummy(0, metadata.space)

        current_value = getattr(pipeline, field_name, metadata.default)
        display_name = pretty_field_name(field_name)

        # ── Unified field renderer ──
        new_value = render_serialized_field(
            ctx, f"##pp_{field_name}", display_name, metadata, current_value, lw,
        )
        _record_renderstack_field_semantic(
            ctx,
            stack,
            f"pipeline.parameter.{field_name}",
            display_name,
            metadata,
            new_value,
        )

        if has_field_changed(metadata.field_type, current_value, new_value) and not metadata.readonly:
            setattr(pipeline, field_name, new_value)
            stack.invalidate_graph()
            _record_stack_field(
                stack,
                pipeline,
                field_name,
                current_value,
                new_value,
                f"Set {display_name}",
            )

        if metadata.tooltip and ctx.is_item_hovered():
            ctx.set_tooltip(metadata.tooltip)

        info = getattr(metadata, 'info_text', "")
        if info:
            render_info_text(ctx, info)


# =====================================================================
# Topology + Effects (main section)
# =====================================================================

def _render_topology_with_effects(ctx: InxGUIContext, stack: "RenderStack", inspector_state: dict | None = None) -> None:
    """Render topology sequence as thin coloured bars.

    Each injection point gets a [+] button that opens a popup for adding
    effects.  Mounted effects appear as collapsible sections below the
    injection point, with parameters and enable/disable toggle.
    """
    g = inspector_state["topology_probe"] if inspector_state is not None else stack._build_full_topology_probe()
    seq = g.topology_sequence

    if not seq:
        ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
        ctx.label("  " + t("renderstack.empty_topology"))
        ctx.pop_style_color(1)
        return

    # Build mounted-effects lookup: injection_point → [PassEntry] sorted by order
    ip_entries = inspector_state["ip_entries"] if inspector_state is not None else {}
    if inspector_state is None:
        entries = stack.pass_entries
        for e in entries:
            ip = e.render_pass.injection_point
            ip_entries.setdefault(ip, []).append(e)
        for ip in ip_entries:
            ip_entries[ip].sort(key=lambda e: e.order)

    # Map display labels back to injection point names
    display_to_name = inspector_state["display_to_name"] if inspector_state is not None else {
        ip.display_name: ip.name for ip in g.injection_points
    }
    effect_stage_by_id = {stage.stable_id: stage for stage in g.effect_stages}

    # Reduce vertical spacing between bars
    ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing, *Theme.INSPECTOR_SUBITEM_SPC)

    # Running counter to guarantee unique IDs even when labels collide
    _uid_counter = 0

    for kind, label in seq:
        _uid_counter += 1
        if kind == "ip":
            ip_name = display_to_name.get(label, label)
            _render_injection_point_row(ctx, stack, ip_name, label, _uid_counter, ip_entries.get(ip_name, []), inspector_state)
        elif kind == "effect_stage":
            stage = effect_stage_by_id.get(label)
            if stage is not None:
                _render_effect_stage_row(ctx, stack, stage, _uid_counter)
        else:
            # Regular pipeline pass — thin bar
            _render_pass_bar(ctx, label, _uid_counter)

    _render_orphan_effect_slots(ctx, stack)
    _render_effect_compile_errors(ctx, stack)

    ctx.pop_style_var(1)


def _render_effect_stage_row(ctx: InxGUIContext, stack: "RenderStack", stage, uid: int) -> None:
    """Render one pipeline-declared stage as an ordered EffectSlot list."""
    from Infernux.components.serialized_field import get_serialized_fields
    from Infernux.renderstack.effect_slot import EffectSlot
    from ._inspector_list_field import _render_list_field
    from ._inspector_references import _create_asset_ref_from_payload

    slots = list(stack.get_effect_stage_slots(stage.stable_id))
    list_metadata = get_serialized_fields(type(stack))["effect_slots"]
    effect_metadata = get_serialized_fields(EffectSlot)["effect"]
    adapter = _effect_stage_adapter(stack, stage.stable_id)

    def _slot_from_drop(payload):
        reference = _create_asset_ref_from_payload(effect_metadata, str(payload))
        return EffectSlot(stage_id=stage.stable_id, effect=reference)

    ctx.push_id_str(f"effect_stage_{uid}_{stage.stable_id}")
    try:
        _render_list_field(
            ctx,
            adapter,
            "slots",
            list_metadata,
            slots,
            0.0,
            display_name=stage.display_name,
            header_drop_type="RENDER_EFFECT_FILE",
            header_drop_factory=_slot_from_drop,
        )
        if semantic_capture_enabled(ctx):
            ctx.record_semantic_item(
                "renderstack_effect_stage",
                stage.display_name,
                True,
                _renderstack_semantic_id(stack, f"effect_stage.{stage.stable_id}"),
                None,
                float(len(slots)),
            )
    finally:
        ctx.pop_id()


def _render_orphan_effect_slots(ctx: InxGUIContext, stack: "RenderStack") -> None:
    """Expose preserved bindings whose pipeline stage no longer exists."""
    orphans = stack.orphan_effect_slots
    if not orphans:
        return
    ctx.dummy(0, 4)
    if not render_compact_section_header(
        ctx,
        f"Missing Effect Stages [{len(orphans)}]##orphan_effect_stages",
        text_color=Theme.WARNING_TEXT,
        level="secondary",
    ):
        return
    for slot in orphans:
        reference = slot.effect_ref
        asset_name = reference.path_hint or reference.guid or "None"
        ctx.label(f"{slot.stage_id}: {asset_name}")
        if semantic_capture_enabled(ctx):
            ctx.record_semantic_item(
                "missing_effect_stage",
                slot.stage_id,
                False,
                _renderstack_semantic_id(stack, f"orphan_effect_slot.{slot.slot_id}"),
                bool(slot.enabled),
                None,
                asset_name,
            )


def _render_effect_compile_errors(ctx: InxGUIContext, stack: "RenderStack") -> None:
    errors = stack.effect_compile_errors
    if not errors:
        return
    ctx.dummy(0, 4)
    if not render_compact_section_header(
        ctx,
        f"Effect Compile Errors [{len(errors)}]##effect_compile_errors",
        text_color=Theme.WARNING_TEXT,
        level="secondary",
    ):
        return
    for index, error in enumerate(errors):
        ctx.text_wrapped(error)
        if semantic_capture_enabled(ctx):
            ctx.record_semantic_item(
                "render_effect_compile_error",
                "Effect Compile Error",
                False,
                _renderstack_semantic_id(stack, f"effect_compile_error.{index}"),
                None,
                None,
                error,
            )


def _render_injection_point_row(
    ctx: InxGUIContext,
    stack: "RenderStack",
    ip_name: str,
    display_label: str,
    uid: int,
    mounted: List,
    inspector_state: dict | None = None,
) -> None:
    """Render an injection point as a collapsible bar."""
    popup_id = f"Popup_{uid}_{ip_name}"
    has_addable_passes = bool(_get_addable_pass_candidates(stack, ip_name, inspector_state))
    formatted_label = format_display_name(display_label)

    header_open = render_compact_section_header(
        ctx,
        f"[{formatted_label}]##ip_hdr_{uid}_{ip_name}",
        text_color=Theme.TEXT,
        level="primary",
    )

    if header_open:
        if mounted:
            ctx.dummy(0, 2)
            ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing, *Theme.INSPECTOR_ITEM_SPC)

            # Separator before first effect (drop here → insert before first)
            first_name = mounted[0].render_pass.name
            _render_effect_separator(ctx, stack,
                                     f"##sep_top_{uid}_{ip_name}",
                                     after_name=None, before_name=first_name)

            for idx, entry in enumerate(mounted):
                _render_mounted_effect(ctx, stack, entry, uid * 100 + idx)

                # Separator after each effect
                next_name = mounted[idx + 1].render_pass.name if idx + 1 < len(mounted) else None
                _render_effect_separator(ctx, stack,
                                         f"##sep_{uid}_{idx}_{entry.render_pass.name}",
                                         after_name=entry.render_pass.name,
                                         before_name=next_name)

            ctx.pop_style_var(1)
            ctx.dummy(0, 4)

        # "Add Pass" button at the bottom of this injection point
        ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding, 0.0, 4.0)
        _btn_x = ctx.get_cursor_pos_x()
        ctx.set_cursor_pos_x(Theme.INSPECTOR_ACTION_ALIGN_X)
        if not has_addable_passes:
            ctx.begin_disabled(True)
        ctx.button(
            f"{t('renderstack.add_pass')}##add_{uid}_{ip_name}",
            lambda: ctx.open_popup(popup_id),
            -1,
            0,
        )
        if not has_addable_passes:
            ctx.end_disabled()
        if semantic_capture_enabled(ctx):
            ctx.record_semantic_item(
                "button",
                t("renderstack.add_pass"),
                has_addable_passes,
                _renderstack_semantic_id(stack, f"injection.{ip_name}.add"),
            )
        ctx.set_cursor_pos_x(_btn_x)
        ctx.pop_style_var(1)

        # Popup for adding passes
        if ctx.begin_popup(popup_id):
            _render_add_pass_popup(ctx, stack, ip_name, uid, inspector_state)
            ctx.end_popup()


def _render_add_pass_popup(
    ctx: InxGUIContext,
    stack: "RenderStack",
    ip_name: str,
    uid: int,
    inspector_state: dict | None = None,
) -> None:
    """Render the categorised pass-selection popup.

    Groups:
      - Post-processing: FullScreenEffect subclasses (grouped by menu_path)
      - Geometry: GeometryPass subclasses
      - Other: remaining RenderPass subclasses
    """
    from Infernux.renderstack.fullscreen_effect import FullScreenEffect
    from Infernux.renderstack.geometry_pass import GeometryPass

    candidates = _get_pass_candidates(ip_name, inspector_state)

    if not candidates:
        ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
        ctx.label("  " + t("renderstack.no_passes"))
        ctx.pop_style_color(1)
        return

    mounted_names = {e.render_pass.name for e in stack.pass_entries}

    # Classify into: effects (with menu_path subcategories), geometry, other
    effect_categorized: Dict[str, List] = {}  # subcategory → [(leaf, full_name, cls)]
    effect_uncategorized: List = []
    geometry_passes: List = []
    other_passes: List = []

    for name, cls in sorted(candidates.items()):
        if issubclass(cls, FullScreenEffect):
            menu_path = getattr(cls, 'menu_path', '') or ''
            if menu_path:
                parts = menu_path.split('/')
                category = parts[0]
                leaf = parts[-1] if len(parts) > 1 else name
                effect_categorized.setdefault(category, []).append((leaf, name, cls))
            else:
                effect_uncategorized.append((name, name, cls))
        elif issubclass(cls, GeometryPass):
            geometry_passes.append((name, name, cls))
        else:
            other_passes.append((name, name, cls))

    def _render_items(items):
        for leaf, full_name, cls in items:
            already = full_name in mounted_names
            if already:
                ctx.begin_disabled(True)
            selected = ctx.selectable(f"  {leaf}##add_{uid}_{full_name}")
            if semantic_capture_enabled(ctx):
                ctx.record_semantic_item(
                    "menu_item",
                    leaf,
                    not already,
                    _renderstack_semantic_id(
                        stack,
                        f"injection.{ip_name}.candidate.{cls.__name__}",
                    ),
                )
            if selected:
                _add_pass(stack, cls)
                ctx.close_current_popup()
            if already:
                ctx.end_disabled()

    # Post-processing section
    if effect_categorized or effect_uncategorized:
        for cat in sorted(effect_categorized.keys()):
            ctx.label(cat)
            ctx.separator()
            _render_items(effect_categorized[cat])
        if effect_uncategorized:
            if effect_categorized:
                ctx.dummy(0, 4)
            ctx.label(t("renderstack.post_processing"))
            ctx.separator()
            _render_items(effect_uncategorized)

    # Geometry section
    if geometry_passes:
        if effect_categorized or effect_uncategorized:
            ctx.dummy(0, 4)
        ctx.label(t("renderstack.geometry"))
        ctx.separator()
        _render_items(geometry_passes)

    # Other section
    if other_passes:
        if effect_categorized or effect_uncategorized or geometry_passes:
            ctx.dummy(0, 4)
        ctx.label(t("renderstack.other"))
        ctx.separator()
        _render_items(other_passes)


def _add_pass(stack: "RenderStack", cls: type) -> None:
    """Instantiate and mount a pass onto the stack."""
    from Infernux.engine.undo import RenderStackAddPassCommand

    mgr = _undo_manager()
    if mgr and not mgr.is_executing and mgr.enabled:
        mgr.execute(RenderStackAddPassCommand(stack, cls))
        return

    inst = cls()
    success = stack.add_pass(inst)
    if not success:
        import sys
        print(f"[RenderStack] add_pass failed for '{inst.name}' at injection point '{inst.injection_point}'", file=sys.stderr)


# =====================================================================
# Mounted effect rendering (collapsible section with parameters)
# =====================================================================

_DRAG_DROP_EFFECT_TYPE = "RENDERSTACK_EFFECT"


def _render_effect_separator(
    ctx: InxGUIContext,
    stack: "RenderStack",
    sep_id: str,
    after_name: str | None,
    before_name: str | None,
) -> None:
    """Render an invisible drop zone between / around effects.

    Uses ``IGUI.reorder_separator`` for the white insertion line.
    """
    from .igui import IGUI

    def _on_drop(payload):
        dragged = str(payload)
        anchor = after_name if after_name is not None else before_name
        if anchor and dragged != anchor:
            from Infernux.engine.undo import RenderStackMovePassCommand

            if after_name is not None:
                _ip, old_orders, new_orders = _compute_insert_after_orders(stack, dragged, after_name)
            else:
                _ip, old_orders, new_orders = _compute_reordered_orders(stack, dragged, before_name)
            if old_orders and new_orders and old_orders != new_orders:
                mgr = _undo_manager()
                if mgr and not mgr.is_executing and mgr.enabled:
                    mgr.execute(RenderStackMovePassCommand(stack, old_orders, new_orders))
                else:
                    for name, order in new_orders.items():
                        stack.reorder_pass(name, order)

    IGUI.reorder_separator(ctx, sep_id, _DRAG_DROP_EFFECT_TYPE, _on_drop)


def _render_mounted_effect(
    ctx: InxGUIContext,
    stack: "RenderStack",
    entry,
    uid: int = 0,
) -> None:
    """Render a single mounted effect as a standard InxComponent-like section.

    Supports drag-and-drop reordering within the same injection point.
    """
    from Infernux.renderstack.fullscreen_effect import FullScreenEffect

    rp = entry.render_pass
    effect_name = rp.name
    is_effect = isinstance(rp, FullScreenEffect)
    display_name = format_display_name(effect_name)

    ctx.push_id_str(f"fx_{uid}_{effect_name}")

    capture_semantics = semantic_capture_enabled(ctx)
    semantic_base = f"pass.{type(rp).__name__}" if capture_semantics else ""

    new_enabled = render_inspector_checkbox(ctx, f"##en_{uid}_{effect_name}", entry.enabled)
    if capture_semantics:
        ctx.record_semantic_item(
            "renderstack_pass_enabled",
            f"{display_name} Enabled",
            True,
            _renderstack_semantic_id(stack, f"{semantic_base}.enabled"),
            bool(new_enabled),
        )
    if new_enabled != entry.enabled:
        from Infernux.engine.undo import RenderStackTogglePassCommand

        mgr = _undo_manager()
        if mgr and not mgr.is_executing and mgr.enabled:
            mgr.execute(RenderStackTogglePassCommand(stack, effect_name, entry.enabled, new_enabled))
        else:
            stack.set_pass_enabled(effect_name, new_enabled)

    ctx.same_line(0, Theme.INSPECTOR_HEADER_ITEM_SPC[0])
    text_color = Theme.TEXT if entry.enabled else Theme.META_TEXT
    header_open = render_compact_section_header(
        ctx,
        f"{display_name}##hdr_{uid}",
        text_color=text_color,
        level="secondary",
    )
    if capture_semantics:
        ctx.record_semantic_item(
            "renderstack_pass_header",
            display_name,
            True,
            _renderstack_semantic_id(stack, f"{semantic_base}.header"),
        )
        ctx.record_semantic_item(
            "status",
            f"{display_name} Order",
            False,
            _renderstack_semantic_id(stack, f"{semantic_base}.order"),
            None,
            float(entry.order),
        )

    # Drag source — start dragging this effect
    if ctx.begin_drag_drop_source(0):
        ctx.set_drag_drop_payload_str(_DRAG_DROP_EFFECT_TYPE, effect_name)
        ctx.label(display_name)
        ctx.end_drag_drop_source()

    # Right-click context menu for removal
    if ctx.begin_popup_context_item(f"ctx_{uid}_{effect_name}"):
        remove_selected = ctx.selectable(f"{t('renderstack.remove')}##{uid}")
        if capture_semantics:
            ctx.record_semantic_item(
                "menu_item",
                t("renderstack.remove"),
                True,
                _renderstack_semantic_id(stack, f"{semantic_base}.remove"),
            )
        if remove_selected:
            from Infernux.engine.undo import RenderStackRemovePassCommand

            mgr = _undo_manager()
            if mgr and not mgr.is_executing and mgr.enabled:
                mgr.execute(RenderStackRemovePassCommand(stack, effect_name))
            else:
                stack.remove_pass(effect_name)
            ctx.close_current_popup()
        ctx.end_popup()

    if header_open:
        if not entry.enabled:
            ctx.begin_disabled(True)

        if is_effect:
            _render_effect_params(ctx, stack, rp, uid)
        else:
            ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
            ctx.label(f"  injection: {rp.injection_point}")
            ctx.label(f"  order: {entry.order}")
            ctx.pop_style_color(1)

        if not entry.enabled:
            ctx.end_disabled()

    ctx.pop_id()


# =====================================================================
# Effect parameter rendering
# =====================================================================

def _render_effect_params(
    ctx: InxGUIContext,
    stack: "RenderStack",
    effect,
    uid: int = 0,
) -> None:
    """Render editable serialized fields for a FullScreenEffect instance."""
    fields = dict(getattr(effect.__class__, '_serialized_fields_', {}))
    if not fields:
        return

    labels = [pretty_field_name(name) for name in fields.keys()]
    lw = max(Theme.INSPECTOR_MIN_LABEL_WIDTH, max_label_w(ctx, labels)) if fields else Theme.INSPECTOR_MIN_LABEL_WIDTH

    for field_name, metadata in fields.items():
        wid = f"##ef_{uid}_{field_name}"
        display_name = pretty_field_name(field_name)

        if metadata.header:
            ctx.separator()
            ctx.label(metadata.header)
        if getattr(metadata, 'space', 0) > 0:
            ctx.dummy(0, metadata.space)

        current_value = getattr(effect, field_name, metadata.default)

        # ── Unified field renderer ──
        new_value = render_serialized_field(
            ctx, wid, display_name, metadata, current_value, lw,
        )
        _record_renderstack_field_semantic(
            ctx,
            stack,
            f"pass.{type(effect).__name__}.parameter.{field_name}",
            display_name,
            metadata,
            new_value,
        )

        if has_field_changed(metadata.field_type, current_value, new_value) and not metadata.readonly:
            setattr(effect, field_name, new_value)
            stack.invalidate_graph()
            _record_stack_field(
                stack,
                effect,
                field_name,
                current_value,
                new_value,
                f"Set {display_name}",
            )

        if metadata.tooltip and ctx.is_item_hovered():
            ctx.set_tooltip(metadata.tooltip)
