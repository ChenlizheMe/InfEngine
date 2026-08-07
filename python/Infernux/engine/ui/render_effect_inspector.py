"""Shared Inspector controls for material-like RenderEffect assets."""

from __future__ import annotations

import copy

from Infernux.components.serialized_field import get_serialized_fields
from Infernux.engine.ui.inspector_utils import (
    has_field_changed,
    max_label_w,
    pretty_field_name,
    render_serialized_field,
)
from Infernux.engine.ui.theme import Theme


def _inspector_parameter_instance(effect, feature):
    """Return a typed parameter view rebuilt only after live asset changes."""
    cache_key = (feature.type_id, id(feature.effect_class), effect.revision)
    cached = getattr(effect, "_inspector_parameter_cache", None)
    if isinstance(cached, tuple) and cached[0] == cache_key:
        return cached[1]
    instance = feature.instantiate(effect)
    effect._inspector_parameter_cache = (cache_key, instance)
    return instance


def apply_render_effect_parameter_edit(
    effect,
    field_name: str,
    value,
    *,
    resource_controller=None,
) -> bool:
    """Apply one typed parameter edit to the shared asset with Undo support."""
    from Infernux.renderstack.render_effect_compiler import get_render_effect_feature

    feature = get_render_effect_feature(effect.feature_type)
    fields = get_serialized_fields(feature.effect_class)
    metadata = fields.get(field_name)
    if metadata is None or metadata.readonly:
        return False

    instance = _inspector_parameter_instance(effect, feature)
    current_value = getattr(instance, field_name, metadata.default)
    if not has_field_changed(metadata.field_type, current_value, value):
        return False
    setattr(instance, field_name, value)

    old_document = effect.to_dict()
    new_document = copy.deepcopy(old_document)
    new_document["parameters"][field_name] = instance.get_params_dict()[field_name]

    if resource_controller is not None:
        return bool(
            resource_controller.apply_document(
                new_document,
                view_id="inspector",
                edit_key=field_name,
                description=f"Set RenderEffect {pretty_field_name(field_name)}",
            )
        )

    # Inspector mutations must always belong to a formal resource Document.
    # Runtime scripts may still use RenderEffect.set_* directly.
    return False


def render_render_effect_parameters(
    ctx,
    effect,
    *,
    widget_prefix: str = "effect",
    resource_controller=None,
) -> bool:
    """Render and edit parameters from the feature's serialized schema."""
    from Infernux.renderstack.render_effect_compiler import get_render_effect_feature

    feature = get_render_effect_feature(effect.feature_type)
    fields = get_serialized_fields(feature.effect_class)
    if not fields:
        return False
    instance = _inspector_parameter_instance(effect, feature)
    labels = [pretty_field_name(name) for name in fields]
    label_width = max(Theme.INSPECTOR_MIN_LABEL_WIDTH, max_label_w(ctx, labels))
    changed = False
    for field_name, metadata in fields.items():
        if metadata.hidden:
            continue
        display_name = pretty_field_name(field_name)
        current_value = getattr(instance, field_name, metadata.default)
        new_value = render_serialized_field(
            ctx,
            f"##{widget_prefix}_{field_name}",
            display_name,
            metadata,
            current_value,
            label_width,
        )
        # The renderer already has the live value and metadata. Avoid entering
        # the edit/Undo path (which instantiates the feature again) for every
        # unchanged field on every Inspector frame.
        if (
            has_field_changed(metadata.field_type, current_value, new_value)
            and apply_render_effect_parameter_edit(
                effect,
                field_name,
                new_value,
                resource_controller=resource_controller,
            )
        ):
            changed = True
        if metadata.tooltip and ctx.is_item_hovered():
            ctx.set_tooltip(metadata.tooltip)
    return changed
