"""
Window Manager for Infernux Editor.
Manages window visibility, registration, and provides Window menu functionality.
"""
from collections import deque
from enum import Enum, auto
import time
from typing import Deque, Dict, Type, Callable, Optional, Any
from Infernux.lib import InxGUIRenderable


class WindowState(Enum):
    CLOSED = auto()
    OPENING = auto()
    OPEN = auto()
    FOCUS_REQUESTED = auto()
    FOCUSED = auto()
    CLOSING = auto()


_VISIBLE_STATES = frozenset({
    WindowState.OPENING,
    WindowState.OPEN,
    WindowState.FOCUS_REQUESTED,
    WindowState.FOCUSED,
})


class WindowInfo:
    """Information about a registered window type."""
    def __init__(self, 
                 window_class: Type[InxGUIRenderable],
                 display_name: str,
                 factory: Optional[Callable[[], InxGUIRenderable]] = None,
                 singleton: bool = True,
                 title_key: Optional[str] = None,
                 menu_path: str = "Window"):
        self.window_class = window_class
        self._display_name = display_name
        self.title_key = title_key
        self.factory = factory or (lambda: window_class())
        self.singleton = singleton  # If True, only one instance allowed
        self.menu_path = menu_path  # Slash-separated menu path, e.g. "Animation/2D Animation"

    @property
    def display_name(self) -> str:
        if self.title_key:
            from Infernux.engine.i18n import t
            return t(self.title_key)
        return self._display_name


class WindowManager:
    """Centralized window manager for the editor.

    Features:
    - Register window types for the Window menu.
    - Track open/closed state per window_id.
    - Create new window instances.
    - Persist panel state across editor restarts.

    Two distinct meanings of "closed" coexist on purpose:

    * **Builtin / singleton panels** stay registered with the native ImGui
      renderer for the entire editor session and only flip ``_is_open`` to
      hide their window. This is required because re-registering an
      ``InxGUIRenderable`` mid-frame races the docking layout.
    * **Dynamic panels** (e.g. animation / 2D-anim editors opened from the
      Window menu) are unregistered from the native renderer when closed and
      lazily re-created on the next ``open_window``.

    Always go through ``set_window_open`` / ``open_window`` / ``close_window``
    rather than mutating ``_is_open`` directly so persistence and pending
    register/unregister actions stay consistent.
    """

    _instance: Optional['WindowManager'] = None
    _RESET_REQUIRED_PANEL_IDS = {"inspector", "project"}
    _NATIVE_FOCUS_SYNC_INTERVAL_SECONDS = 1.0 / 60.0
    
    def __init__(self, engine, panel_interactions, register_panel_gui):
        if panel_interactions is None:
            raise ValueError("WindowManager requires a PanelInteractionRegistry")
        if not callable(register_panel_gui):
            raise TypeError("WindowManager requires a panel GUI registrar")
        self._engine = engine
        self._panel_interactions = panel_interactions
        self._register_panel_gui_callback = register_panel_gui
        self._registered_types: Dict[str, WindowInfo] = {}  # type_id -> WindowInfo
        self._window_states: Dict[str, WindowState] = {}
        self._window_instances: Dict[str, InxGUIRenderable] = {}  # window_id -> instance
        self._registered_instance_ids: set[str] = set()
        self._window_type_ids: Dict[str, str] = {}
        self._default_instances: Dict[str, InxGUIRenderable] = {}  # window_id -> original instance
        self._builtin_defaults: set = set()  # window_ids that should reopen on reset
        self._project_console_front_id = "project"
        self._on_state_changed: Optional[Callable[[], None]] = None
        self._type_change_listeners: list[Callable[[], None]] = []
        self._imgui_ini_path: Optional[str] = None
        self._pending_actions: Deque[Callable[[], None]] = deque()
        self._is_processing_actions = False
        self._window_restore_failures: Dict[str, str] = {}
        self._completed_restore_focus_requests: set[str] = set()
        self._pending_user_focus_requests: Dict[str, tuple[str, str, bool]] = {}
        self._pending_reopen_requests: Dict[
            str,
            tuple[str, Optional[str]],
        ] = {}
        self._next_native_focus_sync_at = 0.0
        WindowManager._instance = self
    
    @classmethod
    def instance(cls) -> Optional['WindowManager']:
        """Get the singleton instance."""
        return cls._instance

    def set_on_state_changed(self, callback: Optional[Callable[[], None]]):
        self._on_state_changed = callback

    @property
    def panel_interactions(self):
        """Registry that owns every live panel-view binding."""
        return self._panel_interactions

    def _bind_panel_interaction(
        self,
        window_id: str,
        type_id: str,
        instance: InxGUIRenderable,
    ) -> None:
        self._panel_interactions.bind_view(window_id, type_id, instance)

    def _register_panel_gui(
        self,
        window_id: str,
        instance: InxGUIRenderable,
    ) -> None:
        self._register_panel_gui_callback(window_id, instance)

    @staticmethod
    def _assign_panel_identity(
        instance: InxGUIRenderable,
        type_id: str,
        window_id: str,
    ) -> None:
        setter = getattr(instance, "set_panel_identity", None)
        if callable(setter):
            setter(type_id, window_id)

    def _bind_panel_lifecycle(
        self,
        instance: InxGUIRenderable,
        window_id: str,
    ) -> None:
        """Route native title-bar close intent through this manager."""
        if not hasattr(instance, "on_request_close"):
            return
        instance.on_request_close = (
            lambda target_id=window_id: self.close_window(target_id)
        )

    def _unbind_panel_interaction(self, window_id: str) -> None:
        self._panel_interactions.unbind_view(window_id)

    def _records_focus_history(self, type_id: str, view_id: str) -> bool:
        return self._panel_interactions.records_focus_history(
            type_id=type_id,
            view_id=view_id,
        )

    def is_document_backed_view(self, window_id: str, type_id: str = "") -> bool:
        """Return whether a panel must have a restorable document to reopen."""
        resolved_view = str(window_id or "").strip()
        resolved_type = str(
            type_id or self._window_type_ids.get(resolved_view, resolved_view)
        ).strip()
        return self._panel_interactions.is_document_backed(
            type_id=resolved_type,
            view_id=resolved_view,
        )

    @staticmethod
    def _instance_reports_open(instance: InxGUIRenderable) -> Optional[bool]:
        """Return the panel's real open state when available, else None."""
        if instance is None:
            return None
        probe = getattr(instance, "is_open", None)
        if callable(probe):
            return bool(probe())
        if probe is not None:
            return bool(probe)
        return None

    @staticmethod
    def _instance_reports_content_visible(
        instance: InxGUIRenderable,
        *,
        previous_frame: bool = False,
    ) -> bool:
        """Read authoritative dock-content visibility from native or Python panels."""
        if instance is None:
            return False
        name = "was_content_visible" if previous_frame else "is_content_visible"
        probe = getattr(instance, name, None)
        if callable(probe):
            return bool(probe())
        if probe is not None:
            return bool(probe)
        return False

    @staticmethod
    def _instance_has_content_visibility(instance: InxGUIRenderable) -> bool:
        """Return whether an instance owns an explicit presentation snapshot."""
        if instance is None:
            return False
        return getattr(instance, "is_content_visible", None) is not None

    @staticmethod
    def _native_window_content_visible(window_id: str) -> Optional[bool]:
        """Read the last complete ImGui-frame presentation snapshot."""
        try:
            from Infernux.lib import was_gui_window_content_presented

            value = was_gui_window_content_presented(str(window_id or ""))
        except (ImportError, RuntimeError):
            return None
        return None if value is None else bool(value)

    @staticmethod
    def _native_window_presented_dock_peer(window_id: str) -> str:
        """Return the previously selected tab sharing ``window_id``'s dock."""
        try:
            from Infernux.lib import get_gui_window_presented_dock_peer

            value = get_gui_window_presented_dock_peer(str(window_id or ""))
        except (ImportError, RuntimeError):
            return ""
        return str(value or "")

    @staticmethod
    def _set_instance_open(instance: InxGUIRenderable, is_open: bool) -> None:
        """Set open state for both Python and native panels."""
        if instance is None:
            raise ValueError("cannot set visibility on a missing window instance")
        setter = getattr(instance, "set_open", None)
        if callable(setter):
            setter(bool(is_open))
            return
        if hasattr(instance, "_is_open"):
            instance._is_open = bool(is_open)
            return
        raise TypeError(f"window instance {type(instance).__name__} has no visibility API")

    def _sync_instance_open_state(self, window_id: str) -> bool:
        """Refresh the lifecycle state from the panel's authoritative visibility."""
        inst = self._window_instances.get(window_id)
        reported = self._instance_reports_open(inst)
        if reported is False and self._window_states.get(window_id) in _VISIBLE_STATES:
            self._window_states[window_id] = WindowState.CLOSED
        return self._window_states.get(window_id, WindowState.CLOSED) in _VISIBLE_STATES

    def is_window_content_visible(self, window_id: str) -> bool:
        """Return true only when the panel is the visible tab in its dock node."""
        target_id = str(window_id or "").strip()
        if not target_id or not self.is_window_open(target_id):
            return False
        instance = self._window_instances.get(target_id)
        if self._instance_has_content_visibility(instance):
            return self._instance_reports_content_visible(instance)
        native_visibility = self._native_window_content_visible(target_id)
        if native_visibility is not None:
            return bool(native_visibility)
        return False

    def was_window_content_visible(self, window_id: str) -> bool:
        """Return the panel's dock-content visibility from its preceding frame."""
        target_id = str(window_id or "").strip()
        if not target_id:
            return False
        return self._instance_reports_content_visible(
            self._window_instances.get(target_id),
            previous_frame=True,
        )

    def _notify_state_changed(self):
        if self._on_state_changed is not None:
            self._on_state_changed()

    def add_type_change_listener(self, callback: Callable[[], None]) -> None:
        if callback not in self._type_change_listeners:
            self._type_change_listeners.append(callback)

    def remove_type_change_listener(self, callback: Callable[[], None]) -> None:
        try:
            self._type_change_listeners.remove(callback)
        except ValueError:
            pass

    def _notify_type_changed(self) -> None:
        for callback in tuple(self._type_change_listeners):
            callback()

    def observe_native_panel_focus(
        self,
        panel_id: str,
        focused: bool,
        *,
        view_id: str = "",
        document_id: str = "",
        user_activated: bool = False,
        source_instance: Optional[InxGUIRenderable] = None,
    ) -> None:
        """Project one native PanelHost focus observation into Interaction Core."""
        if panel_id not in self._window_states:
            raise KeyError(f"Unknown window id: {panel_id}")
        state = self._window_states[panel_id]
        if focused and state in {WindowState.CLOSING, WindowState.CLOSED}:
            # ImGui may publish one final focus sample after a dock tab has
            # begun closing.  Accepting it resurrects an active panel identity
            # whose view/document has already been detached.
            return
        from Infernux.engine.interaction import DocumentRegistry, FocusService

        resolved_view_id = view_id or panel_id
        resolved_document_id = str(document_id or "")
        if focused and not resolved_document_id:
            document = DocumentRegistry.instance().document_for_view(resolved_view_id)
            resolved_document_id = document.document_id if document is not None else ""

        # Native focus notifications also fire while ImGui restores docking
        # and while WindowManager fulfills a programmatic focus request. They
        # project the observed state only; pointer activation and editor
        # commands are the authoritative user-history producers.
        panel_type_id = self._window_type_ids.get(panel_id, panel_id)
        records_history = self._records_focus_history(
            panel_type_id,
            resolved_view_id,
        )
        # Native singleton factories may wire a replacement view before the
        # WindowManager registry finishes projecting that instance. The focus
        # producer must report its own visibility instead of asking a mutable
        # id registry which object happened to render the event.
        instance = source_instance or self._window_instances.get(panel_id)
        # Native focus publication may run either before or after the newly
        # selected dock tab submits its current-frame contents.  Only the
        # preceding completed frame tells us whether this click revealed new
        # editor content; consulting current visibility races with render
        # order and can silently consume a real A -> B tab transition without
        # journaling it.
        if self._instance_has_content_visibility(instance):
            content_already_visible = self._instance_reports_content_visible(
                instance,
                previous_frame=True,
            )
        else:
            native_visibility = self._native_window_content_visible(panel_id)
            content_already_visible = bool(native_visibility)
        record_focus_history = bool(
            user_activated and records_history and not content_already_visible
        )
        presentation_before_view_id = ""
        if record_focus_history:
            dock_peer = self._native_window_presented_dock_peer(panel_id)
            if dock_peer != panel_id and dock_peer in self._window_states:
                presentation_before_view_id = dock_peer
        FocusService.instance().observe_panel_focus(
            panel_type_id,
            bool(focused),
            view_id=resolved_view_id,
            document_id=resolved_document_id,
            reason=("pointer_panel_activation" if user_activated else "native_panel_focus"),
            # Clicking an already visible side-by-side panel only transfers
            # keyboard focus; it does not reveal editor content and therefore
            # has no user-visible state to undo. A newly revealed dock tab has
            # current visibility but not previous-frame visibility, so it is
            # still recorded as a real panel switch.
            record_history=record_focus_history,
            presentation_before_view_id=presentation_before_view_id,
        )
        if not focused:
            return
        for window_id, state in list(self._window_states.items()):
            if state is WindowState.FOCUSED and window_id != panel_id:
                self._window_states[window_id] = WindowState.OPEN
        self._window_states[panel_id] = WindowState.FOCUSED
        if panel_id in {"project", "console"}:
            self._project_console_front_id = panel_id
        self._notify_state_changed()

    def native_panel_focus_callback(
        self,
        panel_id: str,
        *,
        view_id: str = "",
        document_id: str = "",
        source_instance: Optional[InxGUIRenderable] = None,
    ):
        """Return the canonical bool callback expected by native EditorPanel."""
        def publish(focused, user_activated=False):
            # Native properties publish their current value as soon as the
            # callback is installed. Wiring deliberately happens before the
            # live view is registered, so that one bootstrap observation has
            # no window identity to update yet.
            if panel_id not in self._window_states:
                return
            self.observe_native_panel_focus(
                panel_id,
                focused,
                view_id=view_id,
                document_id=document_id,
                user_activated=bool(user_activated),
                source_instance=source_instance,
            )

        return publish

    def project_interaction_focus(self, snapshot) -> None:
        """Reflect the authoritative FocusService snapshot into window chrome."""
        target_id = str(snapshot.active_view_id or snapshot.active_panel_id or "")
        if not target_id or target_id not in self._window_states:
            return
        if self._window_states[target_id] in {WindowState.CLOSING, WindowState.CLOSED}:
            return
        changed = False
        for window_id, state in tuple(self._window_states.items()):
            if state is WindowState.FOCUSED and window_id != target_id:
                self._window_states[window_id] = WindowState.OPEN
                changed = True
        if self._window_states[target_id] is not WindowState.FOCUSED:
            self._window_states[target_id] = WindowState.FOCUSED
            changed = True
        if target_id in {"project", "console"}:
            if self._project_console_front_id != target_id:
                self._project_console_front_id = target_id
                changed = True
        if changed:
            self._notify_state_changed()

    def restore_panel_child_context(self, panel_id: str, context_id: str) -> bool:
        """Project an undo context into the owning panel's visible subview."""
        context_id = str(context_id or "")
        instance = self._window_instances.get(str(panel_id or ""))
        if instance is None:
            return not context_id
        restore = getattr(instance, "restore_child_context", None)
        if not callable(restore):
            return not context_id
        return bool(restore(context_id))

    def resolve_native_gui_panel_id(self, gui_window_id: str) -> str:
        """Resolve an ImGui root/child window id to its owning editor panel."""
        window_id = str(gui_window_id or "")
        if not window_id:
            return ""
        if window_id in self._window_states:
            return window_id

        # Child windows use the stable panel id as their namespace. Prefer the
        # longest match so future nested/dynamic panel ids remain unambiguous.
        candidates = (
            panel_id
            for panel_id in self._window_states
            if window_id.startswith(f"{panel_id}/")
        )
        return max(candidates, key=len, default="")

    def observe_native_gui_window_focus(self, gui_window_id: str) -> bool:
        """Project the authoritative ImGui focus id into Interaction Core."""
        panel_id = self.resolve_native_gui_panel_id(gui_window_id)
        if not panel_id:
            return False

        from Infernux.engine.interaction import DocumentRegistry, FocusService

        focus = FocusService.instance()
        document = DocumentRegistry.instance().document_for_view(panel_id)
        document_id = document.document_id if document is not None else ""
        if (
            focus.snapshot.active_view_id == panel_id
            and focus.snapshot.active_document_id == document_id
            and self._window_states.get(panel_id) is WindowState.FOCUSED
        ):
            return False
        self.observe_native_panel_focus(
            panel_id,
            True,
            view_id=panel_id,
            document_id=document_id,
        )
        return True

    def sync_native_gui_focus(self, now: Optional[float] = None) -> bool:
        """Poll completed ImGui focus at a UI-rate cadence, never per render frame."""
        current = time.monotonic() if now is None else float(now)
        if current < self._next_native_focus_sync_at:
            return False
        self._next_native_focus_sync_at = (
            current + self._NATIVE_FOCUS_SYNC_INTERVAL_SECONDS
        )

        from Infernux.lib import get_gui_focused_window_id

        return self.observe_native_gui_window_focus(get_gui_focused_window_id())

    def _request_focus(self, window_id: str, *, restore_request: bool = False) -> None:
        from Infernux.engine.interaction import FocusService

        self._window_restore_failures.pop(window_id, None)
        if restore_request:
            self._completed_restore_focus_requests.discard(window_id)
        self._window_states[window_id] = WindowState.FOCUS_REQUESTED
        FocusService.instance().request_panel_focus(window_id)

        def focus(target_id=window_id, confirms_restore=restore_request):
            # Interaction Core may project the requested target as FOCUSED
            # before this deferred native dock selection runs. FOCUSED here
            # means the authoritative editor context has advanced; it does
            # not prove that ImGui has revealed the requested tab yet.
            if self._window_states.get(target_id) not in {
                WindowState.FOCUS_REQUESTED,
                WindowState.FOCUSED,
            }:
                return
            try:
                self._select_docked_window(target_id)
                instance = self._window_instances.get(target_id)
                republish = getattr(instance, "republish_panel_focus", None)
                if callable(republish):
                    republish()
            except Exception as exc:
                self._window_states[target_id] = WindowState.OPEN
                self._completed_restore_focus_requests.discard(target_id)
                self._window_restore_failures[target_id] = str(exc)
                raise
            else:
                self._window_states[target_id] = WindowState.FOCUSED
                if confirms_restore:
                    self._completed_restore_focus_requests.add(target_id)

        self._enqueue_action(focus)

    def _select_docked_window(
        self, window_id: str, *, allow_during_modal: bool = False
    ) -> None:
        """Select and present one editor window through the canonical boundary."""
        target_id = str(window_id or "").strip()
        if not target_id:
            return
        selector = self._engine.select_docked_window
        if not allow_during_modal:
            selector(target_id)
            return

        selector(target_id, allow_during_modal=True)
    
    def register_window_type(self, 
                             type_id: str,
                             window_class: Type[InxGUIRenderable],
                             display_name: str,
                             factory: Optional[Callable[[], InxGUIRenderable]] = None,
                             singleton: bool = True,
                             title_key: Optional[str] = None,
                             menu_path: str = "Window"):
        """
        Register a window type that can be created from the Window menu.
        
        Args:
            type_id: Unique identifier for this window type
            window_class: The class of the window
            display_name: Display name shown in menus
            factory: Optional factory function to create instances
            singleton: If True, only one instance of this window is allowed
            title_key: Optional i18n key for dynamic title resolution
            menu_path: Slash-separated menu path (e.g. "Window", "Animation/2D Animation")
        """
        if not type_id:
            raise ValueError("window type_id cannot be empty")
        if type_id in self._registered_types:
            raise ValueError(f"Window type already registered: {type_id}")
        self._registered_types[type_id] = WindowInfo(
            window_class=window_class,
            display_name=display_name,
            factory=factory,
            singleton=singleton,
            title_key=title_key,
            menu_path=menu_path,
        )
        self._notify_type_changed()

    def unregister_window_type(self, type_id: str) -> bool:
        """Remove one dynamic window type and close all of its live views."""

        identifier = str(type_id or "").strip()
        if identifier not in self._registered_types:
            return False
        if identifier in self._builtin_defaults:
            raise RuntimeError(
                f"Builtin window types cannot be unregistered: {identifier}"
            )
        view_ids = [
            window_id
            for window_id, registered_type in self._window_type_ids.items()
            if registered_type == identifier
        ]
        if identifier in self._window_states and identifier not in view_ids:
            view_ids.append(identifier)
        for window_id in view_ids:
            if (
                self._window_states.get(window_id, WindowState.CLOSED)
                not in {WindowState.CLOSED, WindowState.CLOSING}
                and not self.close_window(window_id)
            ):
                return False
        self._registered_types.pop(identifier, None)
        self._default_instances.pop(identifier, None)
        self._notify_type_changed()
        return True
    
    def open_window(self, type_id: str, instance_id: Optional[str] = None) -> Optional[InxGUIRenderable]:
        """
        Open a window of the specified type.
        
        Args:
            type_id: The registered type ID
            instance_id: Optional specific instance ID (for non-singleton windows)
            
        Returns:
            The window instance.
        """
        if type_id not in self._registered_types:
            raise KeyError(f"Unknown window type: {type_id}")
        
        info = self._registered_types[type_id]
        window_id = instance_id or type_id
        self._window_restore_failures.pop(window_id, None)
        
        state = self._window_states.get(window_id, WindowState.CLOSED)
        existing = self._window_instances.get(window_id)
        if state is WindowState.CLOSING:
            # A title-bar close and a menu/open command can land in the same
            # GUI mutation interval.  Replacing CLOSING with OPENING here would
            # cancel native unregistration and then attempt to register the
            # same renderable twice.  Finish the close transaction first and
            # reopen from the resulting CLOSED state.
            self._pending_reopen_requests[window_id] = (type_id, instance_id)
            return existing
        if state in _VISIBLE_STATES:
            if existing is None:
                raise RuntimeError(f"Window '{window_id}' is {state.name} without an instance")
            # OPENING is a registration transaction, not a presented window.
            # Changing it to FOCUS_REQUESTED here makes the queued register
            # action reject its own lifecycle state. User focus is coalesced
            # by open_window_from_user and published after registration.
            if state is not WindowState.OPENING:
                self._request_focus(window_id)
            return existing
        if (
            existing is not None
            and self._default_instances.get(window_id) is existing
            and window_id in self._builtin_defaults
            and window_id in self._registered_instance_ids
        ):
            self._set_instance_open(existing, True)
            self._window_states[window_id] = WindowState.OPEN
            self._request_focus(window_id)
            self._notify_state_changed()
            return existing

        # Reuse the original default instance when reopening a closed built-in
        # singleton so panel state survives hide/show and restart restore.
        instance = self._window_instances.get(window_id)
        if instance is None and info.singleton:
            instance = self._default_instances.get(window_id)
        if instance is None:
            instance = info.factory()

        self._assign_panel_identity(instance, type_id, window_id)
        self._bind_panel_lifecycle(instance, window_id)
        if hasattr(instance, 'set_window_manager'):
            instance.set_window_manager(self)
        if hasattr(instance, 'open'):
            instance.open()
        else:
            self._set_instance_open(instance, True)
        try:
            self._bind_panel_interaction(window_id, type_id, instance)
        except Exception:
            self._set_instance_open(instance, False)
            raise
        self._window_instances[window_id] = instance
        self._window_type_ids[window_id] = type_id
        self._window_states[window_id] = WindowState.OPENING
        # Ensure singleton panels participate in save/load persistence
        # so their open state survives engine restarts.
        if info.singleton and window_id not in self._default_instances:
            self._default_instances[window_id] = instance
        self._notify_state_changed()

        def _register_instance(target_id=window_id, target_instance=instance):
            if self._window_states.get(target_id) is not WindowState.OPENING:
                return
            if self._window_instances.get(target_id) is not target_instance:
                return
            try:
                self._register_panel_gui(target_id, target_instance)
            except Exception as exc:
                self._window_states[target_id] = WindowState.CLOSED
                self._window_restore_failures[target_id] = str(exc)
                self._pending_user_focus_requests.pop(target_id, None)
                self._unbind_panel_interaction(target_id)
                if target_id not in self._builtin_defaults:
                    self._window_instances.pop(target_id, None)
                raise
            else:
                self._registered_instance_ids.add(target_id)
                self._window_states[target_id] = WindowState.OPEN
                pending_focus = self._pending_user_focus_requests.pop(
                    target_id,
                    None,
                )
                if pending_focus is not None:
                    reason, presentation_before_view_id, was_visible = pending_focus
                    self._request_focus(target_id)
                    self._publish_user_window_focus(
                        target_id,
                        reason=reason,
                        presentation_before_view_id=presentation_before_view_id,
                        was_visible=was_visible,
                    )

        self._enqueue_action(_register_instance)
        return instance

    def open_window_from_user(
        self,
        type_id: str,
        instance_id: Optional[str] = None,
        *,
        reason: str = "window_open_command",
    ) -> Optional[InxGUIRenderable]:
        """Open/reveal a panel and publish one explicit navigation action.

        ``open_window`` remains the lifecycle primitive used by startup,
        restore, and automation. Menu/shortcut commands use this method so a
        hidden dock tab is recorded before its first authoring edit, while an
        already visible side-by-side panel only receives keyboard focus.
        """
        target_type = str(type_id or "").strip()
        target_view = str(instance_id or target_type).strip()
        if not target_type or not target_view:
            raise ValueError("user window open requires a type and view id")
        was_visible = self.is_window_content_visible(target_view)
        presentation_before_view_id = ""
        if not was_visible:
            dock_peer = self._native_window_presented_dock_peer(target_view)
            if dock_peer != target_view and dock_peer in self._window_states:
                presentation_before_view_id = dock_peer
        instance = self.open_window(target_type, instance_id=instance_id)
        if instance is None:
            return None

        if self._window_states.get(target_view) in {
            WindowState.OPENING,
            WindowState.CLOSING,
        }:
            # Registration must remain authoritative until register_gui has
            # completed. Store only the latest equivalent user request; the
            # register action publishes one focus/history edge afterwards.
            self._pending_user_focus_requests[target_view] = (
                str(reason or "window_open_command"),
                presentation_before_view_id,
                was_visible,
            )
            return instance

        self._publish_user_window_focus(
            target_view,
            reason=str(reason or "window_open_command"),
            presentation_before_view_id=presentation_before_view_id,
            was_visible=was_visible,
        )
        return instance

    def _publish_user_window_focus(
        self,
        target_view: str,
        *,
        reason: str,
        presentation_before_view_id: str,
        was_visible: bool,
    ) -> None:
        """Commit one user-visible window reveal after lifecycle readiness."""

        from Infernux.engine.interaction import (
            DocumentRegistry,
            EditorInteractionCore,
            FocusService,
        )

        target_type = str(
            self._window_type_ids.get(target_view, target_view) or target_view
        )
        instance = self._window_instances.get(target_view)
        if instance is None:
            raise RuntimeError(
                f"cannot publish focus for missing editor window '{target_view}'"
            )
        document = DocumentRegistry.instance().document_for_view(target_view)
        document_id = document.document_id if document is not None else ""
        child_context = ""
        child_context_provider = getattr(instance, "current_child_context_id", None)
        if callable(child_context_provider):
            child_context = str(child_context_provider() or "")
        core = EditorInteractionCore.instance()
        records_history = bool(
            core is None
            or core.panels.records_focus_history(
                type_id=target_type,
                view_id=target_view,
            )
        )
        if was_visible or not records_history:
            # The lifecycle request above already asks ImGui to focus this
            # panel. Its native focus callback will project keyboard ownership
            # after the command transaction ends. Publishing here as well
            # would turn a focus-only click on an already visible panel into a
            # spurious context-only history item.
            return
        FocusService.instance().activate_panel(
            target_type,
            view_id=target_view,
            document_id=document_id,
            child_context_id=child_context,
            reason=str(reason or "window_open_command"),
            record_history=True,
            presentation_before_view_id=presentation_before_view_id,
        )
    
    def close_window(self, window_id: str) -> bool:
        """Close a window by its ID."""
        if window_id not in self._window_states:
            raise KeyError(f"Unknown window id: {window_id}")
        state = self._window_states[window_id]
        if state in {WindowState.CLOSED, WindowState.CLOSING}:
            return True
        instance = self._window_instances.get(window_id)
        if instance is None:
            raise RuntimeError(f"Window '{window_id}' has no instance to close")
        request_close = getattr(instance, "request_close", None)
        if callable(request_close) and not bool(request_close()):
            return False
        from Infernux.engine.interaction import EditorInteractionCore

        core = EditorInteractionCore.instance()
        if core is not None:
            core.modals.cancel_owner(window_id)
        self._set_instance_open(instance, False)

        def _finalize_close_lifecycle(
            target_id=window_id,
            target_instance=instance,
        ):
            current = self._window_instances.get(target_id)
            if current is None:
                current = self._default_instances.get(target_id)
            if current is not target_instance:
                return
            if self._instance_reports_open(target_instance) is not False:
                return
            finalize = getattr(target_instance, "_finalize_close_lifecycle", None)
            if callable(finalize):
                finalize()

        # Menu/history closes can unregister a dynamic renderable before it
        # submits another frame. Queue the same idempotent lifecycle edge used
        # by EditorPanel's title-bar path, before any unregister action.
        self._enqueue_action(_finalize_close_lifecycle)
        from Infernux.engine.interaction import FocusService

        panel_type_id = self._window_type_ids.get(window_id, window_id)
        FocusService.instance().deactivate_panel(
            panel_type_id,
            view_id=window_id,
        )
        # Built-in panels are hidden but remain live native views, so their
        # document binding must survive hide/show. Dynamic panels are actually
        # destroyed and must release their view before native unregistration.
        if window_id not in self._builtin_defaults:
            self._unbind_panel_interaction(window_id)
            unbind_document = getattr(instance, "unbind_document", None)
            if callable(unbind_document):
                unbind_document()
        self._notify_state_changed()

        if state is WindowState.OPENING and window_id not in self._registered_instance_ids:
            self._window_states[window_id] = WindowState.CLOSED
            if window_id not in self._builtin_defaults:
                self._window_instances.pop(window_id)
            return True

        if window_id in self._builtin_defaults:
            self._window_states[window_id] = WindowState.CLOSED
            return True

        self._window_states[window_id] = WindowState.CLOSING

        def _unregister_instance(target_id=window_id, target_instance=instance):
            if self._window_states.get(target_id) is not WindowState.CLOSING:
                return
            if self._window_instances.get(target_id) is not target_instance:
                raise RuntimeError(f"Window '{target_id}' instance changed while closing")
            self._engine.unregister_gui(target_id)
            self._registered_instance_ids.remove(target_id)
            self._window_instances.pop(target_id)
            self._window_states[target_id] = WindowState.CLOSED
            pending_reopen = self._pending_reopen_requests.pop(target_id, None)
            if pending_reopen is not None:
                reopen_type_id, reopen_instance_id = pending_reopen
                self.open_window(
                    reopen_type_id,
                    instance_id=reopen_instance_id,
                )

        self._enqueue_action(_unregister_instance)
        return True

    def close_deleted_resource_editors(self, resource_path: str) -> tuple[str, ...]:
        """Close authoring views whose durable non-scene asset was deleted."""
        from Infernux.engine.interaction import DocumentKind, DocumentRegistry

        registry = DocumentRegistry.instance()
        closed: list[str] = []
        for document in tuple(registry.documents_for_resource(resource_path)):
            if document.kind is DocumentKind.SCENE:
                continue
            view_ids = registry.retire_deleted_resource_document(
                document.document_id
            )
            for view_id in view_ids:
                if not self.is_document_backed_view(view_id):
                    registry.close_view(view_id, preserve_dormant=False)
                    continue
                instance = self.get_window_instance(view_id)
                if instance is None or view_id not in self._window_states:
                    registry.close_view(view_id, preserve_dormant=False)
                    continue
                retire = getattr(instance, "retire_deleted_document", None)
                if callable(retire):
                    retire(document.document_id)
                if self.close_window(view_id):
                    closed.append(view_id)
                    # Built-in authoring windows are hidden rather than
                    # destroyed, so explicitly release their deleted source.
                    if registry.document_for_view(view_id) is not None:
                        unbind = getattr(instance, "unbind_document", None)
                        if callable(unbind):
                            unbind()
                        else:
                            registry.close_view(
                                view_id,
                                preserve_dormant=False,
                            )
            if registry.get(document.document_id) is not None:
                registry.unregister(
                    document.document_id,
                    preserve_dormant=False,
                )
        return tuple(closed)

    def on_asset_mutation(self, change) -> None:
        """Project committed asset consequences into editor window lifecycle."""
        from Infernux.engine.interaction import (
            AssetMutationKind,
            iter_asset_mutations,
        )

        for mutation in iter_asset_mutations(change):
            if mutation.kind is AssetMutationKind.DELETED:
                self.close_deleted_resource_editors(mutation.source_path)
    
    def is_window_open(self, window_id: str) -> bool:
        """Check if a window is currently open."""
        if window_id in self._window_instances:
            return self._sync_instance_open_state(window_id)
        return self._window_states.get(window_id, WindowState.CLOSED) in _VISIBLE_STATES

    def focus_window(self, window_id: str) -> None:
        """Request focus for an already-open editor panel."""
        target_id = str(window_id).strip()
        if target_id not in self._window_states:
            raise KeyError(f"Unknown window id: {target_id}")
        if not self.is_window_open(target_id):
            raise RuntimeError(f"Editor window is not open: {target_id}")
        self._request_focus(target_id)

    def restore_close_confirmation_source(self, window_id: str) -> None:
        """Keep a vetoed close source visible underneath the shared modal.

        This is presentation-only lifecycle repair.  It intentionally does
        not publish another focus/history action: the user's close click has
        already established the owning panel and the confirmation modal will
        become the final keyboard-focus owner for the frame.
        """
        target_id = str(window_id or "").strip()
        if not target_id or not self.is_window_open(target_id):
            return
        self._select_docked_window(
            target_id,
            allow_during_modal=True,
        )
    
    def set_window_open(self, window_id: str, is_open: bool):
        """Set window open state (called by window when close button is clicked)."""
        if window_id not in self._window_states:
            raise KeyError(f"Unknown window id: {window_id}")
        if is_open:
            type_id = self._window_type_ids.get(window_id, window_id)
            self.open_window(type_id, instance_id=window_id)
            return
        self.close_window(window_id)
    
    def get_registered_types(self) -> Dict[str, WindowInfo]:
        """Get all registered window types."""
        return self._registered_types.copy()

    def window_type_id(self, window_id: str) -> str:
        """Return the registered panel type that owns one window instance."""
        identifier = str(window_id or "").strip()
        if not identifier:
            return ""
        return str(self._window_type_ids.get(identifier, identifier) or identifier)
    
    def get_open_windows(self) -> Dict[str, bool]:
        """Get all window open states."""
        for window_id in list(self._window_instances.keys()):
            self._sync_instance_open_state(window_id)
        return {
            window_id: state in _VISIBLE_STATES
            for window_id, state in self._window_states.items()
        }

    def presentation_snapshot(self) -> Dict[str, dict]:
        """Return read-only evidence used by interaction and MCP regression tests.

        The native value comes from the last completed ImGui frame; the
        instance values expose each panel host's own observation. Keeping both
        makes dock-visibility regressions diagnosable without screen scraping
        or injecting editor mutations.
        """
        result: Dict[str, dict] = {}
        window_ids = sorted(set(self._window_states) | set(self._window_instances))
        for window_id in window_ids:
            instance = self._window_instances.get(window_id)
            native = self._native_window_content_visible(window_id)
            result[window_id] = {
                "state": self._window_states.get(
                    window_id,
                    WindowState.CLOSED,
                ).value,
                "open": self.is_window_open(window_id),
                "content_visible": (
                    bool(native)
                    if native is not None
                    else self._instance_reports_content_visible(instance)
                ),
                "native_presented": native,
                "instance_presented": self._instance_reports_content_visible(instance),
                "instance_presented_previous": self._instance_reports_content_visible(
                    instance,
                    previous_frame=True,
                ),
                "presented_dock_peer": self._native_window_presented_dock_peer(
                    window_id
                ),
            }
        return result

    def get_window_instance(self, window_id: str) -> Optional[InxGUIRenderable]:
        """Return the live panel instance for routing editor commands."""
        return self._window_instances.get(window_id) or self._default_instances.get(window_id)

    def get_window_state(self, window_id: str) -> WindowState:
        """Return the explicit lifecycle state for a known window."""
        if window_id not in self._window_states:
            raise KeyError(f"Unknown window id: {window_id}")
        return self._window_states[window_id]

    def locate_window(self, window_id: str):
        """Return a stable locator without retaining the panel instance."""
        target_id = str(window_id or "").strip()
        if not target_id:
            return None
        if target_id not in self._window_states and target_id not in self._window_instances:
            return None
        from Infernux.engine.interaction import WindowLocator

        return WindowLocator(
            target_id,
            self._window_type_ids.get(target_id, target_id),
        )

    def restore_window(self, locator):
        """Reveal a missing/hidden panel without refocusing visible content."""
        from Infernux.engine.interaction import ContextRestoreStatus, FocusService

        if locator is None:
            return ContextRestoreStatus.READY
        window_id = str(getattr(locator, "window_id", "") or "").strip()
        type_id = str(getattr(locator, "type_id", "") or "").strip()
        if not window_id or not type_id:
            return ContextRestoreStatus.FAILED
        if window_id in self._window_restore_failures:
            return ContextRestoreStatus.FAILED
        known_type = self._window_type_ids.get(window_id)
        if known_type and known_type != type_id:
            return ContextRestoreStatus.FAILED
        is_builtin = (
            window_id in self._builtin_defaults
            and window_id in self._default_instances
        )
        if not is_builtin and type_id not in self._registered_types:
            return ContextRestoreStatus.FAILED

        # Context replay only owns presentation. If the requested panel is
        # already visible beside the current work, changing keyboard focus is
        # an invisible and disruptive extra undo step.
        if self.is_window_content_visible(window_id):
            self._completed_restore_focus_requests.discard(window_id)
            return ContextRestoreStatus.READY

        state = self._window_states.get(window_id, WindowState.CLOSED)
        if state is WindowState.FOCUSED:
            registered = window_id in self._registered_instance_ids
            focused = FocusService.instance().snapshot.active_view_id == window_id
            opened = self.is_window_open(window_id)
            if registered and focused and opened:
                if window_id in self._completed_restore_focus_requests:
                    self._completed_restore_focus_requests.discard(window_id)
                    return ContextRestoreStatus.READY
                # FOCUSED is also used after issuing the native dock request.
                # It is not proof that ImGui has presented the requested tab.
                # Queue one explicit native selection and wait for that call to
                # complete. This remains deterministic while the main window
                # is behind another application and produces no visible frame.
                self._request_focus(window_id, restore_request=True)
            return ContextRestoreStatus.PENDING
        if state in {
            WindowState.OPENING,
            WindowState.FOCUS_REQUESTED,
            WindowState.CLOSING,
        }:
            return ContextRestoreStatus.PENDING
        if state is WindowState.OPEN:
            self._request_focus(window_id, restore_request=True)
            return ContextRestoreStatus.PENDING

        if is_builtin:
            instance = self._default_instances[window_id]
            self._window_instances[window_id] = instance
            self._set_instance_open(instance, True)
            self._window_states[window_id] = WindowState.OPEN
            self._request_focus(window_id, restore_request=True)
            self._notify_state_changed()
            return ContextRestoreStatus.PENDING

        self.open_window(type_id, instance_id=window_id)

        # Registration and docking mutations must stay outside the active ImGui
        # traversal. Queue focus after registration instead of changing an
        # OPENING window to FOCUS_REQUESTED and cancelling its register action.
        self._enqueue_action(
            lambda target_id=window_id: self._request_focus(
                target_id,
                restore_request=True,
            )
        )
        return ContextRestoreStatus.PENDING

    def save_state(self) -> Dict[str, Any]:
        from Infernux.engine.interaction import DocumentRegistry, FocusService

        all_ids = set(self._default_instances.keys()) | set(self._window_states.keys())
        documents = DocumentRegistry.instance()
        return {
            "open_windows": {
                window_id: (
                    self._window_states.get(window_id, WindowState.CLOSED) in _VISIBLE_STATES
                    and not (
                        self.is_document_backed_view(window_id)
                        and documents.is_session_restore_suppressed(window_id)
                    )
                )
                for window_id in all_ids
            },
            "window_types": {
                window_id: self._window_type_ids.get(window_id, window_id)
                for window_id in all_ids
            },
            "active_window_id": FocusService.instance().snapshot.active_view_id or "",
            "project_console_front_id": self._project_console_front_id,
        }

    def load_state(self, data: Dict[str, Any]):
        if not data:
            return

        open_windows = data.get('open_windows', {}) or {}
        window_types = data.get('window_types', {}) or {}
        from Infernux.engine.interaction import DocumentRegistry

        documents = DocumentRegistry.instance()
        for window_id, is_open in open_windows.items():
            type_id = str(window_types.get(window_id, window_id) or window_id)
            self._window_type_ids[window_id] = type_id
            # Lazily create registered-type windows not yet in _default_instances.
            # If the user opened this panel in a prior session and it was saved
            # as open, restore it now.
            if window_id not in self._builtin_defaults:
                has_document = documents.has_pending_session_document(window_id)
                can_restore = not self.is_document_backed_view(
                    window_id,
                    type_id,
                ) or has_document
                if is_open and can_restore and type_id in self._registered_types:
                    self.open_window(type_id, instance_id=window_id)
                else:
                    self._window_states[window_id] = WindowState.CLOSED
                continue

            instance = self._default_instances[window_id]
            if hasattr(instance, 'set_window_manager'):
                instance.set_window_manager(self)

            if is_open:
                self._window_states[window_id] = WindowState.OPEN
                self._set_instance_open(instance, True)
                if self._window_instances.get(window_id) is None:
                    self._window_instances[window_id] = instance
            else:
                self._window_states[window_id] = WindowState.CLOSED
                self._set_instance_open(instance, False)

        # A document snapshot without a restored authoring View is not a
        # hidden workspace. It represents content the user already closed or
        # discarded and must not be written back into the next session.
        documents.prune_pending_session_views(
            view_id
            for view_id in documents.pending_session_view_ids()
            if self.is_window_open(view_id)
        )

        active_window_id = str(data.get('active_window_id', '') or '')
        project_console_front_id = str(data.get('project_console_front_id', '') or '')
        if project_console_front_id in {"project", "console"}:
            self._project_console_front_id = project_console_front_id

        focus_panel_id = ""
        if self.is_window_open(self._project_console_front_id):
            focus_panel_id = self._project_console_front_id
        elif active_window_id and self.is_window_open(active_window_id):
            focus_panel_id = active_window_id

        if focus_panel_id:
            self._select_docked_window(focus_panel_id)
            # Session restoration chooses the visible dock tab before ImGui
            # starts producing native focus transitions. Project that same
            # choice into Interaction Core immediately so shortcut routing and
            # semantic automation cannot retain a stale panel from layout
            # reconstruction.
            self.observe_native_panel_focus(
                focus_panel_id,
                True,
                view_id=focus_panel_id,
            )
    
    def register_existing_window(self, window_id: str, instance: InxGUIRenderable, type_id: Optional[str] = None):
        """
        Atomically adopt and register an already-created window instance.

        Startup-built panels use the same native registration ownership as
        lazily opened panels. A failed descriptor bind or native registration
        therefore cannot leave a half-visible WindowManager entry behind.
        """
        if not window_id:
            raise ValueError("window_id cannot be empty")
        if instance is None:
            raise ValueError("window instance cannot be None")
        if window_id in self._window_states:
            raise ValueError(f"Window already registered: {window_id}")
        resolved_type_id = type_id or window_id
        self._assign_panel_identity(instance, resolved_type_id, window_id)
        self._bind_panel_lifecycle(instance, window_id)
        self._bind_panel_interaction(window_id, resolved_type_id, instance)
        try:
            self._register_panel_gui(window_id, instance)
        except Exception:
            self._unbind_panel_interaction(window_id)
            raise
        self._window_instances[window_id] = instance
        self._window_states[window_id] = WindowState.OPEN
        self._default_instances[window_id] = instance
        self._registered_instance_ids.add(window_id)
        self._builtin_defaults.add(window_id)
        if window_id in {"project", "console"} and self._project_console_front_id not in {"project", "console"}:
            self._project_console_front_id = window_id
        
        # Store type_id association if provided
        self._window_type_ids[window_id] = resolved_type_id

    def set_imgui_ini_path(self, path: str):
        """Set the imgui.ini path used for docking layout persistence."""
        self._imgui_ini_path = path

    def reset_layout(self) -> bool:
        """Request a document-aware reset of the editor workspace layout."""
        from Infernux.engine.interaction import (
            DocumentRegistry,
        )
        from Infernux.engine.ui.dirty_panel_confirmation import (
            DirtyPanelConfirmationCoordinator,
        )

        target_view_ids = tuple(
            window_id
            for window_id, state in self._window_states.items()
            if state in _VISIBLE_STATES and window_id not in self._builtin_defaults
        )
        target_set = set(target_view_ids)
        documents = DocumentRegistry.instance()
        document_ids: list[str] = []
        seen_documents: set[str] = set()
        for view_id in target_view_ids:
            document = documents.document_for_view(view_id)
            if (
                document is None
                or not document.is_dirty
                or document.document_id in seen_documents
            ):
                continue
            # Reset Layout closes dynamic authoring views, not their documents.
            # A shared document that retains another view must therefore remain
            # dirty and must not prompt merely because one projection closes.
            if any(view_id not in target_set for view_id in document.view_ids):
                continue
            seen_documents.add(document.document_id)
            document_ids.append(document.document_id)

        return DirtyPanelConfirmationCoordinator.instance().request_reset_layout(
            tuple(document_ids),
            lambda: self._enqueue_action(
                lambda: self._begin_reset_layout(target_view_ids)
            ),
        )

    def _begin_reset_layout(self, target_view_ids: tuple[str, ...]) -> None:
        """Close the transaction's dynamic views through their formal lifecycle."""
        for window_id in target_view_ids:
            state = self._window_states.get(window_id, WindowState.CLOSED)
            if state is WindowState.CLOSED:
                continue
            if not self.close_window(window_id):
                return
        self._enqueue_action(lambda: self._finish_reset_layout(target_view_ids))

    def _finish_reset_layout(self, target_view_ids: tuple[str, ...]) -> None:
        """Clear native docking state only after every target View is retired."""
        incomplete = tuple(
            window_id
            for window_id in target_view_ids
            if self._window_states.get(window_id, WindowState.CLOSED)
            is not WindowState.CLOSED
        )
        if incomplete:
            from Infernux.debug import Debug

            Debug.log_error(
                "Layout reset aborted because editor views did not close: "
                + ", ".join(incomplete)
            )
            return
        self._engine.reset_imgui_layout()
        self._enqueue_action(self._apply_reset_layout)

    def process_pending_actions(self):
        """Run queued GUI mutations before ImGui starts building the next frame."""
        if self._is_processing_actions:
            return

        self._is_processing_actions = True
        try:
            while self._pending_actions:
                action = self._pending_actions.popleft()
                action()
        finally:
            self._is_processing_actions = False

    def _enqueue_action(self, action: Callable[[], None]):
        self._pending_actions.append(action)

    def _apply_reset_layout(self):
        # Ensure essential panels participate in reset even if their
        # default-instance registration was lost.
        for wid in self._RESET_REQUIRED_PANEL_IDS:
            if wid in self._registered_types:
                self._builtin_defaults.add(wid)
            if wid not in self._default_instances and wid in self._registered_types:
                self._default_instances[wid] = self._registered_types[wid].factory()

        # Force ALL builtin default panels to be open and registered. Dynamic
        # views were already retired by the document-aware close transaction.
        for window_id in self._builtin_defaults:
            instance = self._default_instances.get(window_id)
            if instance is None:
                continue
            was_registered = window_id in self._registered_instance_ids
            self._set_instance_open(instance, True)

            if hasattr(instance, 'set_window_manager'):
                instance.set_window_manager(self)

            if self._window_instances.get(window_id) is not instance:
                self._window_instances[window_id] = instance

            self._window_states[window_id] = WindowState.OPEN
            self._bind_panel_interaction(
                window_id,
                self._window_type_ids.get(window_id, window_id),
                instance,
            )
            if was_registered:
                self._engine.unregister_gui(window_id)
            self._register_panel_gui(window_id, instance)
            self._registered_instance_ids.add(window_id)

        self._project_console_front_id = "project"
        self.focus_window("scene_view")
        self._notify_state_changed()
