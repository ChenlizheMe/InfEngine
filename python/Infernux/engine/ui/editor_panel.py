"""
EditorPanel — Unified base class for editor panels.

All panels should inherit from this class and override
``on_render_content(ctx)``. The base class handles window frame management,
style push/pop, lifecycle hooks, and service access.

Creating a custom panel::

    from Infernux.engine.ui import EditorPanel, editor_panel, EditorEvent

    @editor_panel("My Debug Panel")
    class MyDebugPanel(EditorPanel):
        def on_enable(self):
            self.events.subscribe(EditorEvent.SELECTION_CHANGED, self._on_sel)

        def on_disable(self):
            self.events.unsubscribe(EditorEvent.SELECTION_CHANGED, self._on_sel)

        def on_render_content(self, ctx):
            ctx.text("Hello from my custom panel!")

        def _on_sel(self, obj):
            pass
"""

from __future__ import annotations

import os
from typing import Any, List, Optional, TYPE_CHECKING

from Infernux.engine.path_utils import lexical_path

from .closable_panel import ClosablePanel

if TYPE_CHECKING:
    from Infernux.lib import InxGUIContext
    from .editor_services import EditorServices
    from .event_bus import EditorEventBus


class EditorPanel(ClosablePanel):
    """Unified base class for editor panels.

    Provides:
    - ``self.services`` to access :class:`EditorServices`
    - ``self.events`` to access :class:`EditorEventBus`
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

    def __init__(self, title: str, window_id: Optional[str] = None):
        super().__init__(title, window_id)
        self._enable_called = False
        self._content_was_visible: Optional[bool] = None

    # Keys that should never be part of generic persisted panel state.
    _AUTO_STATE_SKIP_KEYS = {
        "_window_manager",
        "_enable_called",
        "_panel_was_focused",
        "_is_open",
        "_window_id",
        "_title",
        "_title_key",
        "_content_was_visible",
        "_dirty_registry_snapshot",
    }

    # Heuristic skip prefixes for runtime-only heavy references.
    _AUTO_STATE_SKIP_PREFIXES = (
        "_engine",
        "_window_manager",
        "_services",
        "_event",
        "_manager",
        "_graph",
        "_view",
        "_ctx",
        "_native",
    )

    # Hard limit so an auto-persisted panel cannot bloat panel_state.json.
    _AUTO_STATE_MAX_KEYS = 64
    _AUTO_STATE_MAX_ITEMS = 128
    _AUTO_STATE_MAX_STR_LEN = 4096
    _AUTO_STATE_MAX_DEPTH = 4

    # ------------------------------------------------------------------
    # Service and Event Access
    # ------------------------------------------------------------------

    @property
    def services(self) -> EditorServices:
        """Access editor subsystems."""
        from .editor_services import EditorServices
        return EditorServices.instance()

    @property
    def events(self) -> EditorEventBus:
        """Access the editor event bus."""
        from .event_bus import EditorEventBus
        return EditorEventBus.instance()

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

    def handle_save_command(self, save_as: bool = False) -> bool:
        """Handle a save shortcut for this panel's document.

        Return ``True`` only for panels that own an authoring document. The
        menu-bar router falls back to saving the scene for all other panels.
        """
        return False

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

    @classmethod
    def _auto_state_is_supported_scalar(cls, value: Any) -> bool:
        return value is None or isinstance(value, (bool, int, float, str))

    @classmethod
    def _auto_state_normalize_path_value(cls, key: str, value: str) -> str:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return text
        if "path" not in key.lower():
            return text
        if os.path.isabs(text):
            return lexical_path(text)
        try:
            from Infernux.engine.project_context import get_project_root

            root = get_project_root()
        except Exception:
            root = None
        if root:
            return lexical_path(os.path.join(root, text))
        return lexical_path(text)

    @classmethod
    def _auto_state_serialize_value(
        cls,
        key: str,
        value: Any,
        *,
        depth: int,
    ) -> tuple[bool, Any]:
        if depth > cls._AUTO_STATE_MAX_DEPTH:
            return False, None

        if cls._auto_state_is_supported_scalar(value):
            if isinstance(value, str):
                if len(value) > cls._AUTO_STATE_MAX_STR_LEN:
                    return False, None
                return True, cls._auto_state_normalize_path_value(key, value)
            return True, value

        if isinstance(value, (list, tuple)):
            if len(value) > cls._AUTO_STATE_MAX_ITEMS:
                return False, None
            out = []
            for item in value:
                ok, s = cls._auto_state_serialize_value(key, item, depth=depth + 1)
                if not ok:
                    return False, None
                out.append(s)
            return True, out

        if isinstance(value, dict):
            if len(value) > cls._AUTO_STATE_MAX_ITEMS:
                return False, None
            out_dict = {}
            for k, v in value.items():
                if not isinstance(k, str):
                    return False, None
                ok, s = cls._auto_state_serialize_value(k, v, depth=depth + 1)
                if not ok:
                    return False, None
                out_dict[k] = s
            return True, out_dict

        return False, None

    @classmethod
    def _auto_state_should_skip_key(cls, key: str) -> bool:
        if key in cls._AUTO_STATE_SKIP_KEYS:
            return True
        if key.startswith("__"):
            return True
        return any(key.startswith(prefix) for prefix in cls._AUTO_STATE_SKIP_PREFIXES)

    def _collect_auto_state(self) -> dict:
        """Collect a safe generic state snapshot for panels without custom persistence."""
        state: dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if len(state) >= self._AUTO_STATE_MAX_KEYS:
                break
            if self._auto_state_should_skip_key(key):
                continue
            if callable(value):
                continue
            ok, serialized = self._auto_state_serialize_value(key, value, depth=0)
            if ok:
                state[key] = serialized
        return state

    @classmethod
    def _resolve_auto_state_path_value(cls, key: str, value: Any) -> Any:
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return text
        if "path" not in key.lower():
            return text
        if os.path.isabs(text):
            return lexical_path(text)
        try:
            from Infernux.engine.project_context import get_project_root

            root = get_project_root()
        except Exception:
            root = None
        if root:
            return lexical_path(os.path.join(root, text))
        return text

    def _apply_auto_state(self, data: dict) -> None:
        """Apply generic persisted state for panels without custom persistence."""
        if not isinstance(data, dict):
            return
        for key, value in data.items():
            if self._auto_state_should_skip_key(key):
                continue
            if not hasattr(self, key):
                continue
            try:
                setattr(self, key, self._resolve_auto_state_path_value(key, value))
            except Exception:
                continue

    def save_state(self) -> dict:
        """Return a panel state dict for persistence.

        Default behavior auto-persists simple JSON-safe fields so custom
        panels gain basic restore behavior without extra code.
        """
        auto_state = self._collect_auto_state()
        if not auto_state:
            return {}
        return {"__auto_state__": auto_state}

    def load_state(self, data: dict) -> None:
        """Restore panel state from persisted data."""
        if not isinstance(data, dict):
            return
        if set(data) != {"__auto_state__"} or not isinstance(data["__auto_state__"], dict):
            raise ValueError("editor panel state requires the canonical __auto_state__ document")
        self._apply_auto_state(data["__auto_state__"])

    # ------------------------------------------------------------------
    # Unified Render Frame
    # ------------------------------------------------------------------

    def on_render(self, ctx) -> None:
        """Unified render frame for the panel.

        Subclasses should not override this method. Override the hook methods
        above instead.
        """
        if not self._is_open:
            return

        # Trigger on_enable once.
        if not self._enable_called:
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
            visible = self._begin_closable_window(ctx, self._window_flags())
            try:
                if visible:
                    self._content_was_visible = True
                    self._on_visible_pre(ctx)
                    self.on_render_content(ctx)
                else:
                    if self._content_was_visible is not False:
                        self._on_not_visible(ctx)
                    self._content_was_visible = False
            finally:
                ctx.end_window()
        finally:
            # Keep the ImGui style stack balanced even when panel code fails.
            self._pop_window_style(ctx)

        # Fire the close hook when the panel is closed.
        # Also reset _enable_called so a future reopen runs on_enable() again,
        # matching the documented "created or reopened" contract.
        if not self._is_open and self._enable_called:
            self._enable_called = False
            self._content_was_visible = None
            self.on_disable()
            self._dirty_registry_snapshot = None
