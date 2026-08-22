"""
EditorPanel — Unified base class for editor panels.

All panels should inherit from this class and override
``on_render_content(ctx)``. The base class handles window frame management,
style push/pop, lifecycle hooks, and service access.

Creating a custom panel::

    from Infernux.engine.ui import EditorPanel, editor_panel
    from Infernux.engine.interaction import (
        PanelInteractionDescriptor,
        SelectionService,
    )

    @editor_panel(
        "My Debug Panel",
        interaction=PanelInteractionDescriptor(),
    )
    class MyDebugPanel(EditorPanel):
        def on_enable(self):
            SelectionService.instance().add_listener(self._on_sel)

        def on_disable(self):
            SelectionService.instance().remove_listener(self._on_sel)

        def on_render_content(self, ctx):
            ctx.label("Hello from my custom panel!")

        def _on_sel(self, obj):
            pass
"""

from __future__ import annotations

from typing import List, Optional, TYPE_CHECKING

from Infernux.engine.interaction import PanelViewStateSchema

from .closable_panel import ClosablePanel

if TYPE_CHECKING:
    from Infernux.lib import InxGUIContext
    from .editor_services import EditorServices


class EditorPanel(ClosablePanel):
    """Unified base class for editor panels.

    Provides:
    - ``self.services`` to access :class:`EditorServices`
    - ``on_enable()`` when the panel is created or reopened
    - ``on_disable()`` when the panel closes
    - ``on_render_content(ctx)`` for the panel body

    Overridable hooks:
    - ``_window_flags()`` returns ImGui window flags
    - ``_initial_size()`` returns the initial window size or ``None``
    - ``_push_window_style(ctx)`` pushes styles before ``begin_window``
    - ``_pop_window_style(ctx)`` pops styles after ``end_window``
    - ``_on_visible_pre(ctx)`` runs before ``on_render_content``
    - ``save_state() / load_state(data)`` persist panel state
    """

    VIEW_STATE_SCHEMA: PanelViewStateSchema | None = None

    def __init__(self, title: str, window_id: Optional[str] = None):
        super().__init__(title, window_id)
        self._enable_called = False
        self._content_was_visible: Optional[bool] = None
        self._content_visible_previous_frame = False
        self._content_hovered = False
        self._persisted_view_state_loaded = False
        self._content_render_error_signature: tuple[type[BaseException], str] | None = None

    def is_content_visible(self) -> bool:
        """Return whether this panel's current dock tab is presenting content."""
        return bool(self._is_open and self._content_was_visible)

    def was_content_visible(self) -> bool:
        """Return content visibility from the panel's preceding render frame."""
        return bool(self._content_visible_previous_frame)

    def is_content_hovered(self) -> bool:
        """Return whether the presented panel owns the current pointer target."""
        return bool(
            self._is_open
            and self._content_was_visible
            and self._content_hovered
        )

    # ------------------------------------------------------------------
    # Service and Event Access
    # ------------------------------------------------------------------

    @property
    def services(self) -> EditorServices:
        """Access editor subsystems."""
        from .editor_services import EditorServices
        return EditorServices.instance()

    def execute_owned_command(
        self,
        command_id: str,
        *,
        source=None,
        payload=None,
    ) -> bool:
        """Execute a global command after publishing this panel's ownership.

        Toolbar clicks can arrive before the native focus edge is published for
        the frame.  Every panel command must therefore bind its panel, view,
        document, and child context synchronously before command routing reads
        the global editor context.
        """
        from Infernux.engine.interaction import CommandSource

        registry = self.services.command_registry
        if registry is None:
            return False
        self.publish_interaction_ownership(reason="panel_owned_command")
        return registry.execute(
            str(command_id),
            source=CommandSource.TOOLBAR if source is None else source,
            payload=payload,
        ).accepted

    def publish_interaction_ownership(
        self,
        *,
        reason: str = "panel_owned_edit",
        record_history: bool = True,
    ) -> bool:
        """Publish this panel before an edit reaches global history.

        ImGui may report a newly selected dock tab and the first edited widget
        in adjacent callbacks. Authoring code must not depend on that callback
        order: the edit synchronously establishes its panel/document/subview
        context, while the last completed presentation snapshot decides
        whether revealing the tab deserves its own undo step.
        """
        from Infernux.engine.interaction import EditorInteractionCore, FocusService

        should_record = bool(record_history)
        core = EditorInteractionCore.instance()
        if core is not None:
            should_record = bool(
                should_record
                and core.panels.records_focus_history(
                    type_id=self.panel_type_id,
                    view_id=self.window_id,
                )
            )
        if should_record and self._window_manager is not None:
            should_record = not self._window_manager.is_window_content_visible(
                self.window_id
            )
        return FocusService.instance().activate_panel(
            self.panel_type_id,
            view_id=self.window_id,
            document_id=self.document_id,
            child_context_id=self.current_child_context_id(),
            reason=str(reason or "panel_owned_edit"),
            record_history=should_record,
        )

    # ------------------------------------------------------------------
    # Lifecycle Hooks
    # ------------------------------------------------------------------

    def on_enable(self) -> None:
        """Called once when the panel is first rendered.

        Subscribe to events here.
        """
        pass

    def on_disable(self) -> None:
        """Called when the panel is closed.

        Unsubscribe here.
        """
        pass

    # ------------------------------------------------------------------
    # Window Configuration Hooks
    # ------------------------------------------------------------------

    def _window_flags(self) -> int:
        """Return ImGui window flags for this panel.

        The default is 0.
        """
        return 0

    def _initial_size(self) -> Optional[tuple[float, float]]:
        """Return the initial window size ``(w, h)``.

        Return ``None`` to use the ImGui default.
        """
        return None

    def _push_window_style(self, ctx) -> None:
        """Push style vars and colors before ``begin_window``.

        Subclasses must pop the same number in ``_pop_window_style``.
        """
        pass

    def _pop_window_style(self, ctx) -> None:
        """Pop style vars and colors after ``end_window``.

        The pop count must match ``_push_window_style``.
        """
        pass

    def _on_visible_pre(self, ctx) -> None:
        """Run after ``begin_window`` succeeds and before content rendering.

        Useful for one-shot per-frame setup such as focus tracking.
        """
        pass

    def _on_not_visible(self, ctx) -> None:
        """Run when ``begin_window`` returns ``False``.

        Useful for resource management such as pausing render targets.
        """
        pass

    def _pre_render(self, ctx) -> None:
        """Run in ``on_render`` before the window begins.

        Use this for per-frame work that must happen outside the window frame.
        """
        pass

    # ------------------------------------------------------------------
    # Content Rendering
    # ------------------------------------------------------------------

    def on_render_content(self, ctx: InxGUIContext) -> None:
        """Render panel content.

        Override this instead of ``on_render``.
        """
        pass

    # ------------------------------------------------------------------
    # Unified Empty State
    # ------------------------------------------------------------------

    def _render_empty_state(
        self,
        ctx: InxGUIContext,
        hint: Optional[str] = None,
        *,
        drop_types: Optional[List[str]] = None,
        on_drop=None,
        min_height: float = 220.0,
    ) -> None:
        """Draw a centered bordered hint box — the standard "nothing loaded" UI.

        This is the canonical empty-state rendering that all panels should
        use so every editor window has a consistent look.

        Args:
            ctx: The ImGui context.
            hint: Text shown inside the drop zone.  Falls back to
                ``_empty_state_hint()``.
            drop_types: Accepted drag-and-drop payload types.
                Falls back to ``_empty_state_drop_types()``.
            on_drop: Callback ``(payload_type, payload)`` when a drop is
                accepted.  Falls back to ``_on_empty_state_drop``.
            min_height: Minimum height of the empty-state region.
        """
        from .igui import IGUI

        hint = hint or self._empty_state_hint()
        drop_types = drop_types if drop_types is not None else self._empty_state_drop_types()
        on_drop = on_drop or getattr(self, '_on_empty_state_drop', None)

        avail_w = ctx.get_content_region_avail_width()
        empty_h = max(ctx.get_content_region_avail_height(), min_height)

        ctx.begin_child(f"##{self._window_id}_empty_state", avail_w, empty_h, True)
        try:
            region_w = ctx.get_content_region_avail_width()
            region_h = ctx.get_content_region_avail_height()
            zone_w = min(max(region_w - 28.0, 220.0), 460.0)
            zone_h = min(max(region_h - 36.0, 140.0), 250.0)
            start_x = ctx.get_cursor_pos_x() + (region_w - zone_w) * 0.5
            start_y = ctx.get_cursor_pos_y() + (region_h - zone_h) * 0.5

            ctx.set_cursor_pos_x(start_x)
            ctx.set_cursor_pos_y(start_y)
            ctx.invisible_button(f"##{self._window_id}_drop_zone", zone_w, zone_h)

            bx0 = ctx.get_item_rect_min_x()
            by0 = ctx.get_item_rect_min_y()
            bx1 = ctx.get_item_rect_max_x()
            by1 = ctx.get_item_rect_max_y()
            ctx.draw_rect(bx0, by0, bx1, by1, 0.55, 0.55, 0.55, 0.55, 2.0, 8.0)
            ctx.draw_text_aligned(
                bx0, by0, bx1, by1,
                hint,
                0.72, 0.72, 0.72, 0.95,
                0.5, 0.5,
            )

            if drop_types and on_drop:
                IGUI.multi_drop_target(ctx, drop_types, on_drop, outline=True)
        finally:
            ctx.end_child()

    def _empty_state_hint(self) -> str:
        """Return the hint text for the default empty state.

        Override to customize the empty-state message.
        """
        from Infernux.engine.i18n import t
        return t("panel.empty_hint")

    def _empty_state_drop_types(self) -> List[str]:
        """Return accepted drop-target payload types for the empty state.

        Override to accept specific drop types.  Return ``[]`` to disable
        the drop zone.
        """
        return []

    # ------------------------------------------------------------------
    # State Persistence
    # ------------------------------------------------------------------

    def save_state(self) -> dict:
        """Capture only presentation state declared by ``VIEW_STATE_SCHEMA``."""
        schema = self.VIEW_STATE_SCHEMA
        return {} if schema is None else schema.capture(self)

    def load_state(self, data: dict) -> None:
        """Restore only presentation state declared by ``VIEW_STATE_SCHEMA``."""
        if not data:
            return
        schema = self.VIEW_STATE_SCHEMA
        if schema is None:
            raise ValueError(
                f"panel '{self.panel_type_id}' does not declare persisted view state"
            )
        schema.restore(self, data)

    def _persist_panel_state(self) -> None:
        """Publish this panel's view state to the shared in-memory snapshot.

        ``EditorBootstrap._persist_editor_state`` is the sole authority for
        capturing the global document session and flushing ``panel_state`` to
        disk.  A panel must never turn one local document action into a second
        global persistence path.
        """
        from . import panel_state

        key = f"panel:{self.window_id}"
        data = self.save_state()
        if data:
            panel_state.put(key, data)
        else:
            panel_state.delete(key)

    def _load_persisted_view_state_once(self) -> None:
        if self._persisted_view_state_loaded:
            return
        self._persisted_view_state_loaded = True
        from . import panel_state

        data = panel_state.get(f"panel:{self.window_id}")
        if not data:
            return
        try:
            self.load_state(data)
        except (AttributeError, TypeError, ValueError) as exc:
            # This project is intentionally destructive during early API
            # development. Stale editor view state is discarded, never
            # interpreted through a compatibility path.
            from Infernux.debug import Debug

            Debug.log_warning(
                f"Discarded incompatible Panel View State for "
                f"'{self.panel_type_id}': {exc}"
            )
            panel_state.delete(f"panel:{self.window_id}")

    # ------------------------------------------------------------------
    # Unified Render Frame
    # ------------------------------------------------------------------

    def on_render(self, ctx) -> None:
        """Unified render frame for the panel.

        Subclasses should not override this method. Override the hook methods
        above instead.
        """
        if not self._is_open:
            self._content_hovered = False
            return

        # Trigger on_enable once.
        if not self._enable_called:
            self.restore_persisted_session_document()
            self._load_persisted_view_state_once()
            self._enable_called = True
            self._dirty_registry_snapshot = None
            self.on_enable()

        # Apply the initial size on first use if provided.
        init_size = self._initial_size()
        if init_size is not None:
            from .theme import Theme
            ctx.set_next_window_size(init_size[0], init_size[1], Theme.COND_FIRST_USE_EVER)

        # Run pre-frame logic before the window begins.
        self._pre_render(ctx)

        # Push window styles.
        self._push_window_style(ctx)
        try:
            self._content_visible_previous_frame = bool(self._content_was_visible)
            visible = self._begin_closable_window(ctx, self._window_flags())
            try:
                if visible:
                    self._on_visible_pre(ctx)
                    try:
                        self.on_render_content(ctx)
                    except Exception as exc:
                        # A scripting error in one panel must not abort the
                        # complete ImGui frame. ImGui's native error recovery
                        # restores any unfinished child/style stacks when the
                        # window ends; keep retrying so transient asset state
                        # can heal without requiring the panel to be reopened.
                        signature = (type(exc), str(exc))
                        if signature != self._content_render_error_signature:
                            from Infernux.debug import Debug

                            Debug.log_error(
                                f"Panel '{self.panel_type_id}' render failed: {exc}"
                            )
                        self._content_render_error_signature = signature
                    else:
                        self._content_render_error_signature = None
                else:
                    if self._content_was_visible is not False:
                        self._on_not_visible(ctx)
            finally:
                self._content_was_visible = bool(
                    getattr(self, "_content_presented_this_frame", visible)
                )
                popup_probe = getattr(
                    ctx,
                    "is_pointer_activation_blocked_by_popup",
                    None,
                )
                popup_blocks_pointer = bool(
                    popup_probe() if callable(popup_probe) else False
                )
                hover_probe = getattr(ctx, "is_window_hovered", None)
                self._content_hovered = bool(
                    self._content_was_visible
                    and not popup_blocks_pointer
                    and callable(hover_probe)
                    and hover_probe(1)
                )
                ctx.end_window()
        finally:
            # Keep the ImGui style stack balanced even when panel code fails.
            self._pop_window_style(ctx)

        # Fire the close hook when the panel is closed.
        # Also reset _enable_called so a future reopen runs on_enable() again,
        # matching the documented "created or reopened" contract.
        if not self._is_open and self._enable_called:
            self._finalize_close_lifecycle()

    def _finalize_close_lifecycle(self) -> None:
        """Publish one close lifecycle edge, regardless of close source.

        Title-bar closes finish inside :meth:`on_render`, while menu and
        history-driven closes may unregister the renderable before another
        frame is submitted.  WindowManager calls this same idempotent method
        from its deferred close action so both paths observe exactly one
        ``on_disable`` callback.
        """
        if not self._enable_called:
            return
        self._enable_called = False
        self._content_visible_previous_frame = bool(self._content_was_visible)
        self._content_was_visible = None
        self._content_hovered = False
        self.on_disable()
        self._dirty_registry_snapshot = None


class FloatingEditorPanel(EditorPanel):
    """Non-dockable utility panel hosted by the normal Editor lifecycle.

    Settings and build windows used to be rendered by a MenuBar-owned side
    list.  This base keeps their fixed dialog presentation while giving them
    the same WindowManager, focus, command and close semantics as every other
    Editor panel.
    """

    def __init__(
        self,
        title: str,
        window_id: str,
        *,
        size: tuple[float, float],
    ) -> None:
        super().__init__(title, window_id)
        self._dialog_size = (float(size[0]), float(size[1]))
        self._dialog_positioned = False

    def _window_flags(self) -> int:
        from .theme import Theme

        return Theme.WINDOW_FLAGS_DIALOG

    def _initial_size(self) -> tuple[float, float]:
        return self._dialog_size

    def _pre_render(self, ctx) -> None:
        if self._dialog_positioned:
            return
        from .theme import Theme

        x0, y0, width, height = ctx.get_main_viewport_bounds()
        dialog_width, dialog_height = self._dialog_size
        ctx.set_next_window_pos(
            x0 + (width - dialog_width) * 0.5,
            y0 + (height - dialog_height) * 0.5,
            Theme.COND_ALWAYS,
            0.0,
            0.0,
        )
        self._dialog_positioned = True

    def save_state(self) -> dict:
        # Utility settings already have authoritative project/user stores.
        # Persisting arbitrary instance fields would create a second source of
        # truth; WindowManager separately persists only the open/closed state.
        return {}

    def load_state(self, data: dict) -> None:
        if data:
            raise ValueError("floating utility panels do not persist instance state")
