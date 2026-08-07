"""Declarative Inspector model for the canonical RenderStack component."""

from __future__ import annotations

from typing import TYPE_CHECKING

from Infernux.engine.i18n import t

from .inspector_declarative import (
    InspectorChoice,
    InspectorList,
    InspectorMessages,
    InspectorModel,
    InspectorSection,
    InspectorSerializedTarget,
    register_inline_asset_renderer,
    register_inspector_model_provider,
)
from .inspector_utils import format_display_name, render_compact_section_header

if TYPE_CHECKING:
    from Infernux.renderstack.render_stack import RenderStack


def build_renderstack_inspector_model(stack: "RenderStack") -> InspectorModel:
    """Build or reuse the data-only model consumed by the common Inspector."""
    from Infernux.renderstack.default_forward_pipeline import DefaultForwardPipeline

    default_pipeline_name = DefaultForwardPipeline.name
    topology = stack._build_full_topology_probe()
    catalog_signature = tuple(getattr(stack, "_pipeline_catalog_signature", ()))
    if not catalog_signature:
        catalog_signature = tuple(sorted(stack.discover_pipelines()))
        stack._pipeline_catalog_signature = catalog_signature
    pipeline_names = (default_pipeline_name,) + tuple(
        name for name in catalog_signature if name != default_pipeline_name
    )
    stage_signature = tuple(
        (stage.stable_id, stage.display_name, stage.scope.value)
        for stage in topology.effect_stages
    )
    cache_key = (
        type(stack.pipeline).__module__,
        type(stack.pipeline).__qualname__,
        stage_signature,
        pipeline_names,
    )
    cached = getattr(stack, "_inspector_declarative_model", None)
    if isinstance(cached, tuple) and cached[0] == cache_key:
        return cached[1]

    from Infernux.components.serialized_field import get_serialized_fields
    from Infernux.renderstack.effect_slot import EffectSlot

    list_metadata = get_serialized_fields(type(stack))["effect_slots"]
    effect_metadata = get_serialized_fields(EffectSlot)["effect"]

    def current_pipeline_index() -> int:
        current = stack.pipeline_class_name or default_pipeline_name
        return pipeline_names.index(current) if current in pipeline_names else 0

    def set_pipeline_index(index: int) -> None:
        selected = pipeline_names[int(index)]
        new_pipeline = "" if selected == default_pipeline_name else selected
        old_pipeline = stack.pipeline_class_name or ""
        if new_pipeline == old_pipeline:
            return
        from Infernux.engine.interaction import CommandSource, submit_renderstack_command

        submit_renderstack_command(
            "renderstack.set_pipeline",
            source=CommandSource.INLINE_EDIT,
            stack=stack,
            pipeline=new_pipeline,
        )

    def pipeline_parameter_changed(target, field_name, old_value, new_value) -> None:
        from Infernux.engine.interaction import CommandSource, submit_renderstack_command

        submit_renderstack_command(
            "renderstack.set_parameter",
            source=CommandSource.INLINE_EDIT,
            stack=stack,
            field=field_name,
            value=new_value,
            description=f"Set {format_display_name(field_name)}",
            old_value=old_value,
        )

    topology_controls = []
    stage_by_id = {stage.stable_id: stage for stage in topology.effect_stages}
    for kind, label in topology.topology_sequence:
        if kind != "effect_stage":
            # Passes, layers, composites and injection points are compiler
            # topology. RenderStack authors operate only on named mount points.
            continue

        stage = stage_by_id.get(label)
        if stage is None:
            continue
        def make_drop(payload, *, _stage_id=stage.stable_id):
            from Infernux.renderstack.effect_slot import EffectSlot
            from ._inspector_references import _create_asset_ref_from_payload

            reference = _create_asset_ref_from_payload(effect_metadata, str(payload))
            return EffectSlot(stage_id=_stage_id, effect=reference)

        def make_item_label(slot, index):
            hint = slot.effect_ref.path_hint if slot and slot.effect_ref else ""
            if hint:
                import os

                return f"{index + 1}. {os.path.splitext(os.path.basename(hint))[0]}"
            return f"{t('renderstack.effect_slot')} {index + 1}"

        def render_item(ctx, slot, index, widget_prefix, *, _stage_id=stage.stable_id):
            if slot is None or not slot.enabled or not slot.effect_ref:
                return
            _render_effect_slot_parameters(
                ctx,
                slot.effect_ref,
                widget_prefix=f"{_stage_id}_{widget_prefix}_{index}",
            )

        def stage_slots_changed(
            _target,
            _field_name,
            _old_slots,
            new_slots,
            *,
            _stage_id=stage.stable_id,
            _stage_name=stage.display_name,
        ) -> None:
            from Infernux.engine.interaction import CommandSource, submit_renderstack_command

            submit_renderstack_command(
                "renderstack.set_effect_slots",
                source=CommandSource.INLINE_EDIT,
                stack=stack,
                stage_id=_stage_id,
                slots=new_slots,
                description=f"Edit {_stage_name} Effects",
            )

        topology_controls.append(
            InspectorList(
                key=f"stage_{stage.stable_id}",
                label=stage.display_name,
                target=stack,
                field_name=f"effect_slots_{stage.stable_id}",
                metadata=list_metadata,
                value=lambda _stage_id=stage.stable_id: list(
                    stack.get_effect_stage_slots(_stage_id)
                ),
                accept_drop="RENDER_EFFECT_FILE",
                drop_factory=make_drop,
                item_label=make_item_label,
                item_renderer=render_item,
                on_change=stage_slots_changed,
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
                title=t("renderstack.effect_stages"),
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
                resource_controller=_resolve_effect_document_controller(effect),
            )


def _render_effect_slot_parameters(ctx, reference, widget_prefix: str) -> None:
    """Render one slot's shared asset parameters directly below that slot."""
    from Infernux.renderstack.render_effect_compiler import expand_render_effect_reference
    from .render_effect_inspector import render_render_effect_parameters

    try:
        effects = tuple(expand_render_effect_reference(reference))
    except (OSError, TypeError, ValueError):
        return
    for effect_index, effect in enumerate(effects):
        if len(effects) > 1 and not render_compact_section_header(
            ctx,
            f"{effect.name}##{widget_prefix}_{effect_index}",
            level="tertiary",
        ):
            continue
        render_render_effect_parameters(
            ctx,
            effect,
            widget_prefix=f"{widget_prefix}_{effect_index}",
            resource_controller=_resolve_effect_document_controller(effect),
        )


def _resolve_effect_document_controller(effect):
    """Bind shared Effect edits to their document even when its file is not selected."""
    file_path = str(getattr(effect, "file_path", "") or "")
    if not file_path:
        return None
    from Infernux.engine.interaction import (
        DocumentKind,
        ensure_editable_resource_document,
    )

    return ensure_editable_resource_document(
        category="render_effect",
        document_kind=DocumentKind.RENDER_EFFECT,
        file_path=file_path,
        resource=effect,
        guid=str(getattr(effect, "guid", "") or ""),
        autosave_debounce_sec=0.5,
    )


register_inline_asset_renderer("RenderEffect", _render_effect_assets)
register_inspector_model_provider("RenderStack", build_renderstack_inspector_model)
