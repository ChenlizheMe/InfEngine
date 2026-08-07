"""WindowManager — manages dockable editor windows.

Handles window registration, opening/closing, and ImGui layout persistence.

Example::

    wm = WindowManager(engine)
    wm.register_window_type("my_panel", "My Panel", factory=MyPanel)
    wm.open_window("my_panel")
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Callable, Dict, Optional

from Infernux.lib import InxGUIRenderable


class WindowState(Enum):
    CLOSED: WindowState
    OPENING: WindowState
    OPEN: WindowState
    FOCUS_REQUESTED: WindowState
    FOCUSED: WindowState
    CLOSING: WindowState


class WindowInfo:
    """Metadata for a registered window type."""

    window_class: type
    title_key: Optional[str]
    factory: Callable
    singleton: bool

    def __init__(
        self,
        window_class: type,
        display_name: str,
        factory: Optional[Callable] = ...,
        singleton: bool = ...,
        title_key: Optional[str] = ...,
    ) -> None: ...

    @property
    def display_name(self) -> str: ...


class WindowManager:
    """Manages dockable editor windows and their lifecycle."""

    @classmethod
    def instance(cls) -> Optional[WindowManager]:
        """Return the singleton, or ``None``."""
        ...

    def __init__(self, engine: object) -> None: ...

    def set_panel_interaction_registry(self, registry: object) -> None: ...
    def is_document_backed_view(self, window_id: str, type_id: str = "") -> bool: ...

    def register_window_type(
        self,
        type_id: str,
        display_name: str,
        factory: Optional[Callable] = None,
        menu_path: Optional[str] = None,
    ) -> None:
        """Register a window type that can be opened from the Window menu.

        Args:
            type_id: Unique identifier.
            display_name: Name shown in the UI.
            factory: Zero-arg callable returning an ``InxGUIRenderable``.
            menu_path: Optional ``"Window/…"`` menu path.
        """
        ...

    def open_window(self, type_id: str, instance_id: Optional[str] = None) -> Optional[InxGUIRenderable]:
        """Open a window of *type_id* (creating it if needed).

        Returns:
            The window instance, or ``None`` on failure.
        """
        ...

    def open_window_from_user(self, type_id: str, instance_id: Optional[str] = None, *, reason: str = ...) -> Optional[InxGUIRenderable]: ...

    def close_window(self, window_id: str) -> bool: ...

    def is_window_open(self, window_id: str) -> bool: ...
    def is_window_content_visible(self, window_id: str) -> bool: ...
    def was_window_content_visible(self, window_id: str) -> bool: ...
    def focus_window(self, window_id: str) -> None: ...
    def restore_close_confirmation_source(self, window_id: str) -> None: ...

    def set_window_open(self, window_id: str, is_open: bool) -> None: ...

    def get_registered_types(self) -> Dict[str, WindowInfo]: ...
    def window_type_id(self, window_id: str) -> str: ...

    def get_open_windows(self) -> Dict[str, bool]: ...
    def presentation_snapshot(self) -> Dict[str, dict]: ...
    def get_window_instance(self, window_id: str) -> Optional[InxGUIRenderable]: ...
    def get_window_state(self, window_id: str) -> WindowState: ...
    def observe_native_panel_focus(self, panel_id: str, focused: bool, *, view_id: str = ..., document_id: str = ..., user_activated: bool = ..., source_instance: Optional[InxGUIRenderable] = ...) -> None: ...
    def native_panel_focus_callback(self, panel_id: str, *, view_id: str = ..., document_id: str = ..., source_instance: Optional[InxGUIRenderable] = ...) -> Callable[[bool, bool], None]: ...
    def project_interaction_focus(self, snapshot) -> None: ...
    def restore_panel_child_context(self, panel_id: str, context_id: str) -> bool: ...
    def resolve_native_gui_panel_id(self, gui_window_id: str) -> str: ...
    def observe_native_gui_window_focus(self, gui_window_id: str) -> bool: ...
    def sync_native_gui_focus(self, now: Optional[float] = None) -> bool: ...
    def restore_window(self, locator: Any) -> Any: ...

    def register_existing_window(
        self,
        window_id: str,
        instance: InxGUIRenderable,
        type_id: Optional[str] = None,
    ) -> None:
        """Register an already-created window instance."""
        ...

    def set_imgui_ini_path(self, path: str) -> None:
        """Set the path for ImGui layout persistence."""
        ...

    def reset_layout(self) -> bool:
        """Reset the ImGui docking layout to defaults."""
        ...

    def process_pending_actions(self) -> None:
        """Apply queued open/close/layout mutations before the next GUI frame."""
        ...
