"""Declarative Inspector model for the canonical RenderStack component."""

from __future__ import annotations

from typing import TYPE_CHECKING

from Infernux.engine.i18n import t

from .inspector_declarative import (
    InspectorChoice,
    InspectorInlineAssets,
    InspectorList,
    InspectorMessages,
    InspectorModel,
    InspectorReadOnlyRow,
    InspectorSection,
    InspectorSerializedTarget,
    register_inline_asset_renderer,
    register_inspector_model_provider,
)
from .inspector_utils import format_display_name, render_compact_section_header

if TYPE_CHECKING:
    from Infernux.renderstack.render_stack import RenderStack


class _EffectStageSlotsAdapter:
    """Expose one stage's slots as a normal undoable list property."""

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


def build_renderstack_inspector_model(stack: "RenderStack") -> InspectorModel:
    """Build or reuse the data-only model consumed by the common Inspector."""
    topology = stack._build_full_topology_probe()
    pipelines = stack.discover_pipelines()
    pipeline_names = ("Default Forward",) + tuple(
        sorted(name for name in pipelines if name != "Default Forward")
    )
    cache_key = (id(topology), pipeline_names)
    cached = getattr(stack, "_inspector_declarative_model", None)
    if isinstance(cached, tuple) and cached[0] == cache_key:
        return cached[1]

    from Infernux.components.serialized_field import get_serialized_fields
    from Infernux.renderstack.effect_slot import EffectSlot

    list_metadata = get_serialized_fields(type(stack))["effect_slots"]
    effect_metadata = get_serialized_fields(EffectSlot)["effect"]

    def current_pipeline_index() -> int:
        current = stack.pipeline_class_name or "Default Forward"
        return pipeline_names.index(current) if current in pipeline_names else 0

    def set_pipeline_index(index: int) -> None:
        selected = pipeline_names[int(index)]
        new_pipeline = "" if selected == "Default Forward" else selected
        old_pipeline = stack.pipeline_class_name or ""
        if new_pipeline == old_pipeline:
            return
        from Infernux.engine.undo import RenderStackSetPipelineCommand, UndoManager

        manager = UndoManager.instance()
        if manager and manager.enabled and not manager.is_executing:
            manager.execute(RenderStackSetPipelineCommand(stack, old_pipeline, new_pipeline))
        else:
            stack.set_pipeline(new_pipeline)

    def pipeline_parameter_changed(target, field_name, old_value, new_value) -> None:
        # Pipeline instances are not scene components themselves. Mirror their
        # live values into the owning RenderStack immediately so dirty-state,
        # save, undo and MCP inspection all observe the same value.
        stack.sync_pipeline_parameters()
        stack.invalidate_graph()
        from Infernux.engine.undo import RenderStackFieldCommand, UndoManager

        manager = UndoManager.instance()
        if manager and manager.enabled and not manager.is_executing:
            manager.record(
                RenderStackFieldCommand(
                    stack,
                    target,
                    field_name,
                    old_value,
                    new_value,
                    f"Set {format_display_name(field_name)}",
                )
            )

    topology_controls = []
    stage_by_id = {stage.stable_id: stage for stage in topology.effect_stages}
    uid = 0
    for kind, label in topology.topology_sequence:
        if kind == "ip":
            # Injection points are pipeline implementation details. Effects are
            # mounted only through explicit EffectStages.
            continue
        uid += 1
        if kind != "effect_stage":
            topology_controls.append(
                InspectorReadOnlyRow(
                    key=f"pass_{uid}",
                    label=format_display_name(label),
                    level="tertiary",
                )
            )
            continue

        stage = stage_by_id.get(label)
        if stage is None:
            continue
        adapter = _effect_stage_adapter(stack, stage.stable_id)

        def make_drop(payload, *, _stage_id=stage.stable_id):
            from Infernux.renderstack.effect_slot import EffectSlot
            from ._inspector_references import _create_asset_ref_from_payload

            reference = _create_asset_ref_from_payload(effect_metadata, str(payload))
            return EffectSlot(stage_id=_stage_id, effect=reference)

        topology_controls.append(
            InspectorList(
                key=f"stage_{stage.stable_id}",
                label=stage.display_name,
                target=adapter,
                field_name="slots",
                metadata=list_metadata,
                value=lambda _adapter=adapter: _adapter.slots,
                accept_drop="RENDER_EFFECT_FILE",
                drop_factory=make_drop,
            )
        )
        topology_controls.append(
            InspectorInlineAssets(
                key=f"stage_assets_{stage.stable_id}",
                asset_type="RenderEffect",
                references=lambda _adapter=adapter: (
                    slot.effect_ref for slot in _adapter.slots if slot.enabled
                ),
            )
        )

    topology_controls.extend(
        (
            InspectorMessages(
                key="orphan_stages",
                title=t("renderstack.missing_effect_stages"),
                messages=lambda: (
                    f"{slot.stage_id}: "
                    f"{slot.effect_ref.path_hint or slot.effect_ref.guid or 'None'}"
                    for slot in stack.orphan_effect_slots
                ),
                warning=True,
            ),
            InspectorMessages(
                key="compile_errors",
                title=t("renderstack.effect_compile_errors"),
                messages=lambda: stack.effect_compile_errors,
                warning=True,
            ),
        )
    )

    model = InspectorModel(
        key=f"renderstack_{id(stack)}",
        sections=(
            InspectorSection(
                key="pipeline",
                controls=(
                    InspectorChoice(
                        key="pipeline",
                        label=t("renderstack.pipeline"),
                        options=lambda: pipeline_names,
                        current_index=current_pipeline_index,
                        on_change=set_pipeline_index,
                    ),
                    InspectorSerializedTarget(
                        key="pipeline_parameters",
                        target=lambda: stack.pipeline,
                        owner=stack,
                        title=t("renderstack.pipeline_settings"),
                        on_change=pipeline_parameter_changed,
                    ),
                ),
            ),
            InspectorSection(
                key="topology",
                title=t("renderstack.topology"),
                level="secondary",
                separator_before=True,
                controls=tuple(topology_controls),
            ),
        ),
    )
    stack._inspector_declarative_model = (cache_key, model)
    return model


def _render_effect_assets(ctx, references, widget_prefix: str) -> None:
    """Render shared Effect parameters through the common inline-asset hook."""
    from Infernux.renderstack.render_effect_compiler import expand_render_effect_reference
    from .render_effect_inspector import render_render_effect_parameters

    seen = set()
    for reference_index, reference in enumerate(tuple(references)):
        if not reference:
            continue
        try:
            effects = expand_render_effect_reference(reference)
        except (OSError, TypeError, ValueError):
            continue
        for effect_index, effect in enumerate(effects):
            identity = effect.guid or effect.file_path or id(effect)
            if identity in seen:
                continue
            seen.add(identity)
            label = effect.name
            if len(effects) > 1:
                label = f"{effect_index + 1}. {label}"
            if not render_compact_section_header(
                ctx,
                f"{label}##{widget_prefix}_{reference_index}_{effect_index}",
                level="tertiary",
            ):
                continue
            render_render_effect_parameters(
                ctx,
                effect,
                widget_prefix=f"{widget_prefix}_{reference_index}_{effect_index}",
            )


register_inline_asset_renderer("RenderEffect", _render_effect_assets)
register_inspector_model_provider("RenderStack", build_renderstack_inspector_model)
