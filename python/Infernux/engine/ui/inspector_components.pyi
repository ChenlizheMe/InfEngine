"""inspector_components — component rendering dispatch and built-in renderers.

Usage::

    from Infernux.engine.ui.inspector_components import (
        render_component,
        register_component_renderer,
    )

    register_component_renderer("MyComp", my_render_fn)
    render_component(ctx, comp)
"""

from __future__ import annotations

from typing import Optional

from Infernux.lib import InxGUIContext


def register_component_renderer(type_name: str, render_fn: object) -> None:
    """Register a custom render function for a C++ component type."""
    ...

def register_component_extra_renderer(type_name: str, render_fn: object) -> None:
    """Register an *extra* section renderer appended below the main inspector."""
    ...

def register_py_component_renderer(type_name: str, render_fn: object) -> None:
    """Register a custom render function for a Python component type."""
    ...

def render_component(ctx: InxGUIContext, comp: object) -> None:
    """Render the full inspector UI for *comp* (dispatch to registered renderers)."""
    ...

def render_transform_component(ctx: InxGUIContext, trans: object) -> None:
    """Render the Transform component inspector (position / rotation / scale)."""
    ...

def render_builtin_via_setters(
    ctx: InxGUIContext, comp: object, wrapper_cls: type,
) -> None:
    """Generic renderer for C++ components with Python property wrappers."""
    ...

def render_cpp_component_generic(ctx: InxGUIContext, comp: object) -> None:
    """Fallback renderer for C++ components without a custom renderer."""
    ...

def render_py_component(ctx: InxGUIContext, py_comp: object) -> None:
    """Render the inspector UI for a Python script component."""
    ...

def render_object_field(
    ctx: InxGUIContext,
    field_id: str,
    display_text: str,
    type_hint: str,
    selected: bool = False,
    clickable: bool = True,
    accept_drag_type: Optional[str] = None,
    on_drop_callback: Optional[object] = None,
    picker_scene_items: Optional[object] = None,
    picker_asset_items: Optional[object] = None,
    on_pick: Optional[object] = None,
    on_clear: Optional[object] = None,
    on_ping: Optional[object] = None,
    on_open: Optional[object] = None,
    ping_path: Optional[str] = None,
    has_value: Optional[bool] = None,
    semantic_id: str = "",
) -> bool:
    """Render an object reference field with drag-drop and picker support."""
    ...

def render_asset_reference_field(
    ctx: InxGUIContext,
    field_id: str,
    display_text: str,
    type_hint: str,
    selected: bool = False,
    clickable: bool = True,
    accept_drag_type: Optional[str | list[str] | tuple[str, ...]] = None,
    on_assign: Optional[object] = None,
    picker_scene_items: Optional[object] = None,
    additional_asset_items: Optional[object] = None,
    on_clear: Optional[object] = None,
    on_ping: Optional[object] = None,
    on_open: Optional[object] = None,
    ping_path: Optional[str] = None,
    semantic_id: str = "",
    asset_type: str = "",
    on_rejected: Optional[object] = None,
    has_value: Optional[bool] = None,
    reference_value: object = None,
    transaction: object = None,
    alternate_compatibility: Optional[object] = None,
    read_only: bool = False,
) -> bool: ...
