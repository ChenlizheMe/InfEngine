"""
PanelRegistry — decorator-based panel registration system.

Provides the ``@editor_panel`` decorator that auto-registers panel classes
with the :class:`WindowManager` at startup, eliminating the need to
manually edit ``release_engine()`` for every new panel.

Usage::

    from Infernux.engine.interaction import PanelInteractionDescriptor
    from Infernux.engine.ui import EditorPanel, editor_panel

    @editor_panel(
        "My Panel",
        menu_path="Window/Custom",
        interaction=PanelInteractionDescriptor(),
    )
    class MyPanel(EditorPanel):
        def on_render_content(self, ctx):
            ctx.label("Hello!")

At startup, ``PanelRegistry.apply_all(window_manager)`` registers every
decorated class with the :class:`WindowManager` so it appears in the
Window menu and can be opened/closed.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Iterator
from typing import Callable, Dict, List, Optional, Type, TYPE_CHECKING

from Infernux.debug import Debug
from Infernux.engine.interaction import (
    PanelInteractionDescriptor,
    PanelInteractionRegistry,
)

if TYPE_CHECKING:
    from Infernux.lib import InxGUIRenderable
    from .window_manager import WindowManager


class _PanelRegistration:
    """Internal data-class for a pending panel registration."""

    __slots__ = (
        "panel_class",
        "type_id",
        "display_name",
        "title_key",
        "menu_path",
        "factory",
        "singleton",
        "interaction",
        "owner",
    )

    def __init__(
        self,
        panel_class: Type,
        type_id: str,
        display_name: str,
        menu_path: str,
        factory: Optional[Callable],
        singleton: bool,
        title_key: Optional[str] = None,
        interaction: Optional[PanelInteractionDescriptor] = None,
        owner: str = "",
    ):
        self.panel_class = panel_class
        self.type_id = type_id
        self.display_name = display_name
        self.title_key = title_key
        self.menu_path = menu_path
        self.factory = factory
        self.singleton = singleton
        self.interaction = interaction
        self.owner = str(owner or "")


class PanelRegistry:
    """Central registry of ``@editor_panel``-decorated classes.

    **Not instantiated** — all state is class-level.  Call
    :meth:`apply_all` once during engine startup to flush pending
    registrations into the :class:`WindowManager`.
    """

    _registrations: List[_PanelRegistration] = []
    _owner: ContextVar[str] = ContextVar("infernux_panel_owner", default="")
    _live_window_manager: Optional[WindowManager] = None
    _live_interaction_registry: Optional[PanelInteractionRegistry] = None

    # ------------------------------------------------------------------
    # API called by the decorator
    # ------------------------------------------------------------------

    @classmethod
    def _register(cls, reg: _PanelRegistration) -> None:
        if not reg.owner:
            reg.owner = cls._owner.get()
        if cls._live_window_manager is not None and any(
            item.type_id == reg.type_id for item in cls._registrations
        ):
            raise RuntimeError(
                f"duplicate editor panel type registration: {reg.type_id}"
            )
        cls._registrations.append(reg)
        if cls._live_window_manager is not None:
            try:
                cls._apply_registration(
                    reg,
                    cls._live_window_manager,
                    cls._live_interaction_registry,
                )
            except BaseException:
                cls._registrations.remove(reg)
                raise

    @classmethod
    @contextmanager
    def contribution_scope(cls, owner: str) -> Iterator[None]:
        """Attribute registrations performed during one preload import."""

        token = cls._owner.set(str(owner or ""))
        try:
            yield
        finally:
            cls._owner.reset(token)

    # ------------------------------------------------------------------
    # API called by release_engine()
    # ------------------------------------------------------------------

    @classmethod
    def apply_all(
        cls,
        window_manager: WindowManager,
        interaction_registry: Optional[PanelInteractionRegistry] = None,
        *,
        factory_overrides: Optional[Dict[str, Callable]] = None,
    ) -> int:
        """Register all pending panel classes with *window_manager*.

        Returns the number of panels registered.
        """
        overrides = factory_overrides or {}
        registrations = tuple(cls._registrations)
        type_ids = tuple(reg.type_id for reg in registrations)
        duplicate_type_ids = {
            type_id for type_id in type_ids if type_ids.count(type_id) > 1
        }
        if duplicate_type_ids:
            names = ", ".join(sorted(duplicate_type_ids))
            raise RuntimeError(f"duplicate editor panel type registrations: {names}")

        if interaction_registry is not None:
            missing = tuple(
                reg.type_id
                for reg in registrations
                if reg.interaction is None
                and interaction_registry.descriptor(reg.type_id) is None
            )
            if missing:
                names = ", ".join(sorted(missing))
                raise RuntimeError(
                    "editor panels require a formal interaction descriptor "
                    f"before registration: {names}"
                )
            # Establish every type contract before existing WindowManager
            # instances are rebound. This keeps registration one-way: type
            # authority first, live views second, presentation types last.
            for reg in registrations:
                if reg.interaction is not None:
                    interaction_registry.register_type(
                        reg.type_id,
                        reg.interaction,
                        replace=True,
                    )
            window_manager.set_panel_interaction_registry(interaction_registry)

        count = 0
        for reg in registrations:
            cls._apply_registration(
                reg,
                window_manager,
                interaction_registry,
                factory=overrides.get(reg.type_id, reg.factory),
            )
            count += 1
        return count

    @classmethod
    def bind_live(
        cls,
        window_manager: WindowManager,
        interaction_registry: Optional[PanelInteractionRegistry] = None,
    ) -> None:
        """Bind registrations made after editor startup to the live UI."""

        cls._live_window_manager = window_manager
        cls._live_interaction_registry = interaction_registry

    @classmethod
    def unbind_live(cls) -> None:
        cls._live_window_manager = None
        cls._live_interaction_registry = None

    @classmethod
    def remove_owner(cls, owner: str) -> bool:
        """Remove every panel type contributed by one preload source."""

        identifier = str(owner or "")
        registrations = [item for item in cls._registrations if item.owner == identifier]
        if not registrations:
            return True
        manager = cls._live_window_manager
        if manager is not None:
            for reg in reversed(registrations):
                if not manager.unregister_window_type(reg.type_id):
                    return False
        interaction_registry = cls._live_interaction_registry
        if interaction_registry is not None:
            for reg in reversed(registrations):
                interaction_registry.unregister_type(reg.type_id)
        cls._registrations = [
            item for item in cls._registrations if item.owner != identifier
        ]
        return True

    @classmethod
    def _apply_registration(
        cls,
        reg: _PanelRegistration,
        window_manager: WindowManager,
        interaction_registry: Optional[PanelInteractionRegistry],
        *,
        factory: Optional[Callable] = None,
    ) -> None:
        if interaction_registry is not None:
            if reg.interaction is None and interaction_registry.descriptor(reg.type_id) is None:
                raise RuntimeError(
                    "editor panels require a formal interaction descriptor before "
                    f"registration: {reg.type_id}"
                )
            if reg.interaction is not None:
                interaction_registry.register_type(
                    reg.type_id,
                    reg.interaction,
                    replace=True,
                )
        window_manager.register_window_type(
            type_id=reg.type_id,
            window_class=reg.panel_class,
            display_name=reg.display_name,
            factory=factory if factory is not None else reg.factory,
            singleton=reg.singleton,
            title_key=reg.title_key,
            menu_path=reg.menu_path,
        )
        Debug.log_internal(
            f"[PanelRegistry] Registered: {reg.display_name} ({reg.type_id})"
        )

    @classmethod
    def get_registrations(cls) -> List[_PanelRegistration]:
        """Return a copy of the registration list (for introspection)."""
        return list(cls._registrations)

    @classmethod
    def clear(cls) -> None:
        """Clear all registrations (for testing)."""
        cls._registrations.clear()


# ======================================================================
# Public decorator
# ======================================================================


def editor_panel(
    display_name: str,
    *,
    type_id: Optional[str] = None,
    title_key: Optional[str] = None,
    menu_path: str = "Window",
    factory: Optional[Callable] = None,
    singleton: bool = True,
    interaction: Optional[PanelInteractionDescriptor] = None,
):
    """Decorator to register a panel class with the editor.

    Args:
        display_name: Display name shown in the Window menu
            (e.g. ``"My Debug Panel"``).
        type_id: Unique identifier.  Defaults to the class name in
            lower_case (e.g. ``MyDebugPanel`` → ``mydebugpanel``).
        title_key: Optional i18n key for dynamic title resolution
            via ``t(title_key)``.  When set, the panel title and
            Window-menu label update automatically on locale change.
        menu_path: Menu path for grouping (default ``"Window"``).
            Slash-separated — ``"Animation/2D Animation"`` places the
            panel under *Animation → 2D Animation* in the menu bar.
        factory: Optional callable that returns a new panel instance.
            Defaults to ``panel_class()``.
        singleton: If *True* (default) only one instance is allowed.
        interaction: Declarative command, shortcut, and selection capabilities.

    Example::

        @editor_panel(
            "My Panel",
            interaction=PanelInteractionDescriptor(),
        )
        class MyPanel(EditorPanel):
            def on_render_content(self, ctx):
                ctx.label("Hello!")
    """

    def decorator(cls: Type) -> Type:
        tid = type_id or cls.__name__.lower()

        # Stamp class-level metadata (so WindowManager can read them)
        cls.WINDOW_TYPE_ID = tid
        cls.WINDOW_DISPLAY_NAME = display_name
        cls.WINDOW_TITLE_KEY = title_key
        cls._panel_menu_path = menu_path
        cls._panel_singleton = singleton
        cls.PANEL_INTERACTION = interaction

        PanelRegistry._register(
            _PanelRegistration(
                panel_class=cls,
                type_id=tid,
                display_name=display_name,
                menu_path=menu_path,
                factory=factory,
                singleton=singleton,
                title_key=title_key,
                interaction=interaction,
            )
        )
        return cls

    return decorator
