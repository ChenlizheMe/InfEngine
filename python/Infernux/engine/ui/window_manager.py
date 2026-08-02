"""
Window Manager for Infernux Editor.
Manages window visibility, registration, and provides Window menu functionality.
"""
from collections import deque
from enum import Enum, auto
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
    
    def __init__(self, engine):
        self._engine = engine
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
        WindowManager._instance = self
    
    @classmethod
    def instance(cls) -> Optional['WindowManager']:
        """Get the singleton instance."""
        return cls._instance

    def set_on_state_changed(self, callback: Optional[Callable[[], None]]):
        self._on_state_changed = callback

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

    def note_panel_focus(self, panel_id: str):
        if panel_id not in self._window_states:
            raise KeyError(f"Unknown window id: {panel_id}")
        from Infernux.engine.interaction import FocusService

        FocusService.instance().activate_panel(panel_id)
        for window_id, state in list(self._window_states.items()):
            if state is WindowState.FOCUSED and window_id != panel_id:
                self._window_states[window_id] = WindowState.OPEN
        self._window_states[panel_id] = WindowState.FOCUSED
        if panel_id in {"project", "console"}:
            self._project_console_front_id = panel_id
        self._notify_state_changed()

    def _request_focus(self, window_id: str) -> None:
        from Infernux.engine.interaction import FocusService

        self._window_states[window_id] = WindowState.FOCUS_REQUESTED
        FocusService.instance().request_panel_focus(window_id)

        def focus(target_id=window_id):
            if self._window_states.get(target_id) is not WindowState.FOCUS_REQUESTED:
                return
            self._engine.select_docked_window(target_id)
            self._window_states[target_id] = WindowState.FOCUSED

        self._enqueue_action(focus)
    
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
        
        state = self._window_states.get(window_id, WindowState.CLOSED)
        existing = self._window_instances.get(window_id)
        if state in _VISIBLE_STATES:
            if existing is None:
                raise RuntimeError(f"Window '{window_id}' is {state.name} without an instance")
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

        if hasattr(instance, 'set_window_manager'):
            instance.set_window_manager(self)
        if hasattr(instance, 'open'):
            instance.open()
        else:
            self._set_instance_open(instance, True)
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
            self._engine.register_gui(target_id, target_instance)
            self._registered_instance_ids.add(target_id)
            self._window_states[target_id] = WindowState.OPEN

        self._enqueue_action(_register_instance)
        return instance
    
    def close_window(self, window_id: str):
        """Close a window by its ID."""
        if window_id not in self._window_states:
            raise KeyError(f"Unknown window id: {window_id}")
        state = self._window_states[window_id]
        if state in {WindowState.CLOSED, WindowState.CLOSING}:
            return
        instance = self._window_instances.get(window_id)
        if instance is None:
            raise RuntimeError(f"Window '{window_id}' has no instance to close")
        request_close = getattr(instance, "request_close", None)
        if callable(request_close) and not bool(request_close()):
            return
        self._set_instance_open(instance, False)
        from Infernux.engine.interaction import FocusService

        FocusService.instance().deactivate_panel(window_id)
        try:
            from Infernux.engine.project_context import clear_panel_tracking

            clear_panel_tracking(window_id)
        except Exception:
            pass
        self._notify_state_changed()

        if state is WindowState.OPENING and window_id not in self._registered_instance_ids:
            self._window_states[window_id] = WindowState.CLOSED
            if window_id not in self._builtin_defaults:
                self._window_instances.pop(window_id)
            return

        if window_id in self._builtin_defaults:
            self._window_states[window_id] = WindowState.CLOSED
            return

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

        self._enqueue_action(_unregister_instance)
    
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
    
    def get_open_windows(self) -> Dict[str, bool]:
        """Get all window open states."""
        for window_id in list(self._window_instances.keys()):
            self._sync_instance_open_state(window_id)
        return {
            window_id: state in _VISIBLE_STATES
            for window_id, state in self._window_states.items()
        }

    def get_window_instance(self, window_id: str) -> Optional[InxGUIRenderable]:
        """Return the live panel instance for routing editor commands."""
        return self._window_instances.get(window_id) or self._default_instances.get(window_id)

    def get_window_state(self, window_id: str) -> WindowState:
        """Return the explicit lifecycle state for a known window."""
        if window_id not in self._window_states:
            raise KeyError(f"Unknown window id: {window_id}")
        return self._window_states[window_id]

    def save_state(self) -> Dict[str, Any]:
        from .closable_panel import ClosablePanel

        all_ids = set(self._default_instances.keys()) | set(self._window_states.keys())
        return {
            "open_windows": {
                window_id: self._window_states.get(window_id, WindowState.CLOSED) in _VISIBLE_STATES
                for window_id in all_ids
            },
            "active_panel_id": ClosablePanel.get_active_panel_id() or "",
            "project_console_front_id": self._project_console_front_id,
        }

    def load_state(self, data: Dict[str, Any]):
        if not data:
            return

        open_windows = data.get('open_windows', {}) or {}
        for window_id, is_open in open_windows.items():
            # Lazily create registered-type windows not yet in _default_instances.
            # If the user opened this panel in a prior session and it was saved
            # as open, restore it now.
            if window_id not in self._builtin_defaults:
                if is_open and window_id in self._registered_types:
                    self.open_window(window_id)
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

        active_panel_id = str(data.get('active_panel_id', '') or '')
        project_console_front_id = str(data.get('project_console_front_id', '') or '')
        if project_console_front_id in {"project", "console"}:
            self._project_console_front_id = project_console_front_id

        focus_panel_id = ""
        if self.is_window_open(self._project_console_front_id):
            focus_panel_id = self._project_console_front_id
        elif active_panel_id and self.is_window_open(active_panel_id):
            focus_panel_id = active_panel_id

        if focus_panel_id:
            self._engine.select_docked_window(focus_panel_id)
    
    def register_existing_window(self, window_id: str, instance: InxGUIRenderable, type_id: Optional[str] = None):
        """
        Register an already-created window instance.
        Used when windows are created directly (e.g., at startup).
        """
        if not window_id:
            raise ValueError("window_id cannot be empty")
        if instance is None:
            raise ValueError("window instance cannot be None")
        if window_id in self._window_states:
            raise ValueError(f"Window already registered: {window_id}")
        self._window_instances[window_id] = instance
        self._window_states[window_id] = WindowState.OPEN
        self._default_instances[window_id] = instance
        self._registered_instance_ids.add(window_id)
        self._builtin_defaults.add(window_id)
        if window_id in {"project", "console"} and self._project_console_front_id not in {"project", "console"}:
            self._project_console_front_id = window_id
        
        # Store type_id association if provided
        if type_id:
            self._window_type_ids[window_id] = type_id
        else:
            self._window_type_ids[window_id] = window_id

    def set_imgui_ini_path(self, path: str):
        """Set the imgui.ini path used for docking layout persistence."""
        self._imgui_ini_path = path

    def reset_layout(self):
        """Reset to default layout: re-open default panels, clear ImGui docking state."""
        self._enqueue_action(self._begin_reset_layout)

    def _begin_reset_layout(self):
        """Phase 1: clear docking/layout state, then rebuild next frame."""
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

        # 1. Close any dynamically-opened windows (not part of builtin default set)
        dynamic_ids = [
            wid for wid, state in self._window_states.items()
            if state in _VISIBLE_STATES and wid not in self._builtin_defaults
        ]
        for wid in dynamic_ids:
            self._window_states[wid] = WindowState.CLOSED
            # Only unregister windows that are currently tracked as active instances.
            if wid in self._window_instances:
                self._engine.unregister_gui(wid)
                self._registered_instance_ids.remove(wid)
            self._window_instances.pop(wid, None)

        # 2. Force ALL builtin default panels to be open and registered
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
            if was_registered:
                self._engine.unregister_gui(window_id)
            self._engine.register_gui(window_id, instance)
            self._registered_instance_ids.add(window_id)

        self._project_console_front_id = "project"
        self._notify_state_changed()
