"""PanelRegistry — decorator-based panel registration system.

Example::

    from Infernux.engine.interaction import PanelInteractionDescriptor
    from Infernux.engine.ui.panel_registry import editor_panel, PanelRegistry

    @editor_panel(
        "Debug Tools",
        type_id="my_debug",
        menu_path="Window/Debug",
        interaction=PanelInteractionDescriptor(),
    )
    class MyPanel(EditorPanel):
        def on_render_content(self, ctx):
            ctx.text("Hello!")
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional, Type

from Infernux.engine.ui.window_manager import WindowManager
from Infernux.engine.interaction import PanelInteractionDescriptor, PanelInteractionRegistry


class _PanelRegistration:
    panel_class: Type
    type_id: str
    display_name: str
    title_key: Optional[str]
    menu_path: str
    factory: Optional[Callable]
    singleton: bool
    interaction: Optional[PanelInteractionDescriptor]

    def __init__(
        self,
        panel_class: Type,
        type_id: str,
        display_name: str,
        menu_path: str,
        factory: Optional[Callable],
        singleton: bool,
        title_key: Optional[str] = ...,
        interaction: Optional[PanelInteractionDescriptor] = ...,
    ) -> None: ...


class PanelRegistry:
    """Global registry of @editor_panel-decorated panel classes."""

    @classmethod
    def get_registrations(cls) -> List[_PanelRegistration]:
        """Return all registered panel definitions."""
        ...

    @classmethod
    def apply_all(
        cls,
        window_manager: WindowManager,
        interaction_registry: Optional[PanelInteractionRegistry] = ...,
        *,
        factory_overrides: Optional[Dict[str, Callable]] = ...,
    ) -> int:
        """Flush all registrations into *window_manager*.

        Returns:
            Number of panels registered.
        """
        ...

    @classmethod
    def clear(cls) -> None:
        """Clear all registrations (for testing)."""
        ...


def editor_panel(
    display_name: str,
    *,
    type_id: Optional[str] = ...,
    title_key: Optional[str] = ...,
    menu_path: str = ...,
    factory: Optional[Callable] = ...,
    singleton: bool = ...,
    interaction: Optional[PanelInteractionDescriptor] = ...,
) -> Callable[[Type], Type]:
    """Class decorator that registers an :class:`EditorPanel` subclass.

    Args:
        type_id: Unique string identifier for the panel type.
        display_name: Human-readable name shown in the Window menu.
        menu_path: Optional ``"Window/SubMenu/Name"`` menu placement.
        factory: Optional zero-arg callable that creates instances.

    Returns:
        The decorated class (unchanged).
    """
    ...
