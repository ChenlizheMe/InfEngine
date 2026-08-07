"""
EditorWindow — base class for custom Python editor windows.

Provides a simple subclassing API for creating dockable tool windows
that appear in the editor's *Window* menu.

Usage::

    from Infernux.engine.interaction import PanelInteractionDescriptor
    from Infernux.engine.ui.editor_window import EditorWindow, editor_window

    @editor_window(
        "My Tool",
        menu_path="Window/Tools",
        interaction=PanelInteractionDescriptor(),
    )
    class MyToolWindow(EditorWindow):

        def on_render_content(self, ctx):
            ctx.label("Hello from My Tool!")
            if ctx.button("Click me"):
                print("clicked")
"""

from __future__ import annotations

from typing import Optional, Callable, Type, TYPE_CHECKING

from Infernux.engine.interaction import PanelInteractionDescriptor
from .editor_panel import EditorPanel
from .panel_registry import editor_panel

if TYPE_CHECKING:
    from Infernux.lib import InxGUIContext


class EditorWindow(EditorPanel):
    """Base class for custom Python editor windows.

    Subclass and override :meth:`on_render_content` to build your UI.
    Use the :func:`@editor_window <editor_window>` decorator on the
    subclass to register it with the editor menu system automatically.

    Attributes set by the ``@editor_window`` decorator (do not set manually):

    * ``WINDOW_TYPE_ID``       — unique string id
    * ``WINDOW_DISPLAY_NAME``  — human-readable title shown in the Window menu
    * ``WINDOW_TITLE_KEY``     — optional i18n key
    * ``WINDOW_MENU_PATH``     — menu path (default ``"Window"``)
    """

    # Subclasses may set a default size (width, height) that is applied
    # the first time the window opens.  ``None`` means "use ImGui default".
    INITIAL_SIZE: Optional[tuple[float, float]] = None

    # Subclasses may set extra ImGui window flags.
    WINDOW_FLAGS: int = 0

    def __init__(self):
        title = getattr(self, 'WINDOW_DISPLAY_NAME', None) or self.__class__.__name__
        wid = getattr(self, 'WINDOW_TYPE_ID', None) or self.__class__.__name__.lower()
        super().__init__(title, window_id=wid)

    # ── Convenience overrides ──────────────────────────────────────────

    def _window_flags(self) -> int:
        return self.WINDOW_FLAGS

    def _initial_size(self) -> Optional[tuple[float, float]]:
        return self.INITIAL_SIZE

    # ── User API ───────────────────────────────────────────────────────

    def on_render_content(self, ctx: "InxGUIContext") -> None:  # noqa: D401
        """Override this to render your window's content."""
        pass


# =====================================================================
# @editor_window decorator
# =====================================================================

def editor_window(
    display_name: str,
    *,
    type_id: Optional[str] = None,
    title_key: Optional[str] = None,
    menu_path: str = "Window",
    singleton: bool = True,
    interaction: PanelInteractionDescriptor,
) -> Callable[[Type[EditorWindow]], Type[EditorWindow]]:
    """Class decorator that registers an :class:`EditorWindow` subclass.

    The window will appear in the editor's *Window* menu (or a sub-menu
    given by *menu_path*) and can be opened/closed at runtime.

    Args:
        display_name: Human-readable name shown in the menu.
        type_id:      Unique string id (defaults to ``cls.__name__.lower()``).
        title_key:    Optional i18n translation key for the title.
        menu_path:    Menu path (e.g. ``"Window/Tools"``).  Default ``"Window"``.
        singleton:    If ``True`` (default), only one instance allowed at a time.
        interaction:  Required Interaction Core capability descriptor.

    Example::

        @editor_window(
            "Shader Graph",
            menu_path="Window/Rendering",
            interaction=PanelInteractionDescriptor(),
        )
        class ShaderGraphWindow(EditorWindow):
            INITIAL_SIZE = (800, 600)

            def on_render_content(self, ctx):
                ctx.label("Shader Graph Editor")
    """

    def decorator(cls: Type[EditorWindow]) -> Type[EditorWindow]:
        if not (isinstance(cls, type) and issubclass(cls, EditorWindow)):
            raise TypeError(
                f"@editor_window can only be applied to EditorWindow subclasses, "
                f"got {cls!r}"
            )

        if not isinstance(interaction, PanelInteractionDescriptor):
            raise TypeError(
                "@editor_window requires a PanelInteractionDescriptor"
            )
        return editor_panel(
            display_name,
            type_id=type_id,
            title_key=title_key,
            menu_path=menu_path,
            singleton=singleton,
            interaction=interaction,
        )(cls)

    return decorator
