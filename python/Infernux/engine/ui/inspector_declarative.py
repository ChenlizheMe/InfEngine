"""Declarative controls rendered by the common component Inspector path."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from Infernux.lib import InxGUIContext

from .inspector_utils import (
    field_label,
    has_field_changed,
    max_label_w,
    pretty_field_name,
    render_compact_section_header,
    render_serialized_field,
)
from .theme import ImGuiCol, ImGuiStyleVar, ImGuiTreeNodeFlags, Theme


@dataclass(frozen=True)
class InspectorChoice:
    key: str
    label: str
    options: Callable[[], Sequence[str]]
    current_index: Callable[[], int]
    on_change: Callable[[int], None]


@dataclass(frozen=True)
class InspectorSerializedTarget:
    key: str
    target: Callable[[], Any]
    owner: Any
    title: str = ""
    on_change: Callable[[Any, str, Any, Any], None] | None = None


@dataclass(frozen=True)
class InspectorReadOnlyRow:
    key: str
    label: str
    level: str = "tertiary"


@dataclass(frozen=True)
class InspectorList:
    key: str
    label: str
    target: Any
    field_name: str
    metadata: Any
    value: Callable[[], list]
    accept_drop: str | None = None
    drop_factory: Callable[[Any], Any] | None = None
    item_label: Callable[[Any, int], str] | None = None
    item_renderer: Callable[[InxGUIContext, Any, int, str], None] | None = None
    on_change: Callable[[Any, str, list, list], None] | None = None


@dataclass(frozen=True)
class InspectorInlineAssets:
    key: str
    asset_type: str
    references: Callable[[], Iterable[Any]]


@dataclass(frozen=True)
class InspectorMessages:
    key: str
    title: str
    messages: Callable[[], Iterable[str]]
    warning: bool = False


InspectorControl = (
    InspectorChoice
    | InspectorSerializedTarget
    | InspectorReadOnlyRow
    | InspectorList
    | InspectorInlineAssets
    | InspectorMessages
)


@dataclass(frozen=True)
class InspectorSection:
    key: str
    controls: tuple[InspectorControl, ...] = field(default_factory=tuple)
    title: str = ""
    level: str = "secondary"
    separator_before: bool = False


@dataclass(frozen=True)
class InspectorModel:
    key: str
    sections: tuple[InspectorSection, ...]


_INLINE_ASSET_RENDERERS: dict[str, Callable[[InxGUIContext, Iterable[Any], str], None]] = {}
_MODEL_PROVIDERS: dict[str, Callable[[Any], InspectorModel]] = {}


def register_inline_asset_renderer(
    asset_type: str,
    renderer: Callable[[InxGUIContext, Iterable[Any], str], None],
) -> None:
    """Register a resource editor used by any declarative component model."""
    _INLINE_ASSET_RENDERERS[str(asset_type)] = renderer


def register_inspector_model_provider(
    component_type: str,
    provider: Callable[[Any], InspectorModel],
) -> None:
    """Register a data-model provider without giving the component draw code."""
    _MODEL_PROVIDERS[str(component_type)] = provider


def get_inspector_model(component) -> InspectorModel | None:
    provider = _MODEL_PROVIDERS.get(str(getattr(component, "type_name", type(component).__name__)))
    return provider(component) if provider is not None else None


def render_inspector_model(ctx: InxGUIContext, component, model: InspectorModel) -> bool:
    """Render one declarative model. Returns True when structure changed."""
    structure_changed = False
    ctx.push_style_var_vec2(ImGuiStyleVar.FramePadding, *Theme.INSPECTOR_FRAME_PAD)
    ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing, *Theme.INSPECTOR_ITEM_SPC)
    try:
        for section in model.sections:
            if section.separator_before:
                ctx.separator()
            if section.title and not render_compact_section_header(
                ctx,
                f"{section.title}##{model.key}_{section.key}",
                level=section.level,
            ):
                continue
            for control in section.controls:
                if isinstance(control, InspectorChoice):
                    options = tuple(control.options())
                    if not options:
                        continue
                    current = max(0, min(int(control.current_index()), len(options) - 1))
                    lw = max_label_w(ctx, [control.label])
                    field_label(ctx, control.label, lw)
                    selected = ctx.combo(f"##{model.key}_{control.key}", current, list(options), -1)
                    if selected != current:
                        control.on_change(int(selected))
                        structure_changed = True
                        return structure_changed
                elif isinstance(control, InspectorSerializedTarget):
                    _render_serialized_target(ctx, control)
                elif isinstance(control, InspectorReadOnlyRow):
                    _render_read_only_row(ctx, control, model.key)
                elif isinstance(control, InspectorList):
                    from ._inspector_list_field import _render_list_field

                    # Different declarative controls may expose the same
                    # property name (RenderStack stages all expose ``slots``).
                    # Scope nested widgets and picker popups to this control.
                    ctx.push_id_str(control.key)
                    try:
                        _render_list_field(
                            ctx,
                            control.target,
                            control.field_name,
                            control.metadata,
                            control.value(),
                            0.0,
                            display_name=control.label,
                            header_drop_type=control.accept_drop,
                            header_drop_factory=control.drop_factory,
                            item_label=control.item_label,
                            item_renderer=control.item_renderer,
                            on_change=control.on_change,
                        )
                    finally:
                        ctx.pop_id()
                elif isinstance(control, InspectorInlineAssets):
                    renderer = _INLINE_ASSET_RENDERERS.get(control.asset_type)
                    if renderer is not None:
                        renderer(ctx, control.references(), f"{model.key}_{control.key}")
                elif isinstance(control, InspectorMessages):
                    _render_messages(ctx, control, model.key)
    finally:
        ctx.pop_style_var(2)
    return structure_changed


def _render_serialized_target(ctx: InxGUIContext, control: InspectorSerializedTarget) -> None:
    from Infernux.components.fields import get_serialized_fields

    target = control.target()
    if target is None:
        return
    fields = get_serialized_fields(type(target))
    if not fields:
        return
    if control.title:
        ctx.label(control.title)
    labels = [pretty_field_name(name) for name, meta in fields.items() if not meta.hidden]
    label_width = max_label_w(ctx, labels) if labels else 0.0
    current_group = ""
    group_visible = True
    for field_name, metadata in fields.items():
        if metadata.hidden:
            continue
        field_group = metadata.group or ""
        if field_group != current_group:
            current_group = field_group
            group_visible = (
                render_compact_section_header(ctx, field_group, level="tertiary")
                if field_group else True
            )
        if not group_visible:
            continue
        if metadata.header:
            ctx.label(metadata.header)
        if metadata.space > 0:
            ctx.dummy(0.0, metadata.space)
        current = getattr(target, field_name, metadata.default)
        display_name = pretty_field_name(field_name)
        updated = render_serialized_field(
            ctx,
            f"##{control.key}_{field_name}",
            display_name,
            metadata,
            current,
            label_width,
        )
        if has_field_changed(metadata.field_type, current, updated) and not metadata.readonly:
            if control.on_change is not None:
                control.on_change(target, field_name, current, updated)
            else:
                raise RuntimeError(
                    f"Serialized Inspector field '{field_name}' has no transaction handler"
                )
        if metadata.tooltip and ctx.is_item_hovered():
            ctx.set_tooltip(metadata.tooltip)


def _render_read_only_row(ctx: InxGUIContext, control: InspectorReadOnlyRow, model_key: str) -> None:
    level = control.level
    if level == "primary":
        base = Theme.INSPECTOR_HEADER_PRIMARY
        hovered = Theme.INSPECTOR_HEADER_PRIMARY_HOVERED
        active = Theme.INSPECTOR_HEADER_PRIMARY_ACTIVE
    elif level == "list":
        base = Theme.INSPECTOR_HEADER_LIST
        hovered = Theme.INSPECTOR_HEADER_LIST_HOVERED
        active = Theme.INSPECTOR_HEADER_LIST_ACTIVE
    else:
        base = Theme.INSPECTOR_HEADER_TERTIARY
        hovered = Theme.INSPECTOR_HEADER_TERTIARY_HOVERED
        active = Theme.INSPECTOR_HEADER_TERTIARY_ACTIVE
    ctx.push_style_color(ImGuiCol.Header, *base)
    ctx.push_style_color(ImGuiCol.HeaderHovered, *hovered)
    ctx.push_style_color(ImGuiCol.HeaderActive, *active)
    ctx.push_style_color(ImGuiCol.Text, *Theme.META_TEXT)
    try:
        ctx.tree_node_ex(
            f"{control.label}##{model_key}_{control.key}",
            ImGuiTreeNodeFlags.NoTreePushOnOpen
            | ImGuiTreeNodeFlags.Leaf
            | ImGuiTreeNodeFlags.Bullet
            | ImGuiTreeNodeFlags.SpanAvailWidth,
        )
    finally:
        ctx.pop_style_color(4)


def _render_messages(ctx: InxGUIContext, control: InspectorMessages, model_key: str) -> None:
    messages = tuple(str(message) for message in control.messages() if str(message))
    if not messages:
        return
    color = Theme.WARNING_TEXT if control.warning else Theme.META_TEXT
    if not render_compact_section_header(
        ctx,
        f"{control.title} [{len(messages)}]##{model_key}_{control.key}",
        level="secondary",
        text_color=color,
    ):
        return
    for message in messages:
        ctx.text_wrapped(message)
