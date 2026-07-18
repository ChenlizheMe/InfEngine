from __future__ import annotations

from typing import Any, List, Optional, Tuple

from Infernux.lib import ImageReadbackTicket, InxGUIRenderable, LogLevel, RuntimeMode
from Infernux.engine.play_mode import PlayModeManager


class Engine:
    """High-level engine facade for initialization, rendering, and editor integration."""

    def __init__(self, engine_log_level: LogLevel = ..., mode: RuntimeMode = ...) -> None: ...
    def init_renderer(self, width: int, height: int, project_path: str) -> None:
        """Initialize the Vulkan renderer with the given window size and project path."""
        ...
    def run(self) -> None:
        """Enter the main engine loop."""
        ...
    def init_headless(self, project_path: str) -> None:
        """Initialize assets, scene, physics, and workers without a graphics stack."""
        ...
    def tick(self, delta_time: float) -> None:
        """Advance one deterministic headless frame."""
        ...
    def request_exit(self) -> None:
        """Request the native run loop to stop without cleaning up."""
        ...
    def exit(self) -> None:
        """Request engine shutdown."""
        ...
    def tick_play_mode(self, external_delta_time: Optional[float] = ...) -> float:
        """Advance play-mode timing before the native scene update."""
        ...
    def set_gui_font(self, font_path: str, font_size: int = ...) -> None:
        """Set the ImGui font from a TTF file."""
        ...
    def set_log_level(self, engine_log_level: LogLevel) -> None:
        """Set the engine log verbosity level."""
        ...
    def register_gui(self, name: str, gui_object: InxGUIRenderable) -> None:
        """Register an ImGui renderable panel by name."""
        ...
    def unregister_gui(self, name: str) -> None:
        """Unregister an ImGui renderable panel."""
        ...
    def select_docked_window(self, window_id: str) -> None:
        """Select and focus a docked window by its stable ``window_id``."""
        ...
    def reset_imgui_layout(self) -> None:
        """Reset the ImGui docking layout to defaults."""
        ...
    def show(self) -> None:
        """Show the engine window."""
        ...
    def hide(self) -> None:
        """Hide the engine window."""
        ...
    def set_window_icon(self, icon_path: str) -> None:
        """Set the window icon from an image file."""
        ...
    def get_native_engine(self) -> Any:
        """Get the native C++ engine instance."""
        ...
    def get_resource_preview_manager(self) -> Any:
        """Get the resource preview manager."""
        ...
    def get_asset_database(self) -> Any:
        """Get the asset database instance."""
        ...

    # Editor Camera — property-based access
    @property
    def editor_camera(self) -> Any:
        """Get the editor camera controller (EditorCamera object)."""
        ...

    def process_scene_view_input(
        self, delta_time: float,
        right_mouse_down: bool, middle_mouse_down: bool,
        mouse_delta_x: float, mouse_delta_y: float, scroll_delta: float,
        key_w: bool, key_a: bool, key_s: bool, key_d: bool,
        key_q: bool, key_e: bool, key_shift: bool,
    ) -> None:
        """Process WASD + mouse input for the editor scene camera."""
        ...

    # Scene Render Target API
    def get_scene_texture_id(self) -> int:
        """Get the texture ID for the scene render target."""
        ...
    def resize_scene_render_target(self, width: int, height: int) -> None:
        """Resize the scene viewport render target."""
        ...

    # Game Render Target API
    def get_game_texture_id(self) -> int:
        """Get the texture ID for the game render target."""
        ...
    def resize_game_render_target(self, width: int, height: int) -> None:
        """Resize the game viewport render target."""
        ...
    def request_render_target_readback(self, game_view: bool = True) -> ImageReadbackTicket:
        """Request a non-blocking readback of the latest scene or game target."""
        ...
    def request_capture(self, source: str, output_path: str) -> int: ...
    def query_capture(self, capture_id: int) -> dict: ...
    def cancel_capture(self, capture_id: int) -> bool: ...
    def set_game_camera_enabled(self, enabled: bool) -> None:
        """Enable or disable the game camera rendering."""
        ...
    def get_last_game_render_ms(self) -> float:
        """Get last frame's game view render time in ms (game camera pipeline only)."""
        ...
    def get_game_only_frame_ms(self) -> float:
        """Get game-only frame cost in ms (SceneUpdate + PrepareFrame + GameRender)."""
        ...
    def get_scene_update_ms(self) -> float:
        """Get SceneManager::Update + LateUpdate time in ms."""
        ...
    def get_gui_build_ms(self) -> float:
        """Get GUI::BuildFrame (all ImGui panels) time in ms."""
        ...
    def get_prepare_frame_ms(self) -> float:
        """Get PrepareFrame (collect/cull renderables) time in ms."""
        ...
    def get_screen_ui_renderer(self) -> Any:
        """Get the screen UI renderer instance."""
        ...

    # Scene Picking API
    def pick_scene_object_ids(self, screen_x: float, screen_y: float, viewport_width: float, viewport_height: float) -> List[int]:
        """Pick ordered candidate object IDs at screen coordinates."""
        ...
    def request_scene_object_pick(self, screen_x: float, screen_y: float, viewport_width: float, viewport_height: float) -> int: ...
    def query_scene_object_pick(self, request_id: int) -> dict: ...

    # Editor Tools API
    def set_editor_tool_highlight(self, axis: int) -> None:
        """Set the highlighted gizmo axis (0=None, 1=X, 2=Y, 3=Z)."""
        ...
    def set_editor_tool_mode(self, mode: int) -> None:
        """Set the active editor tool mode (translate, rotate, scale)."""
        ...
    def get_editor_tool_mode(self) -> int:
        """Get the active editor tool mode."""
        ...
    def set_editor_tool_local_mode(self, local: bool) -> None:
        """Set whether the editor tool operates in local or world space."""
        ...
    def screen_to_world_ray(self, screen_x: float, screen_y: float, viewport_width: float, viewport_height: float) -> Tuple[float, float, float, float, float, float]:
        """Convert screen coordinates to a world-space ray (ox, oy, oz, dx, dy, dz)."""
        ...

    # Editor Gizmos API
    def get_selected_object_id(self) -> int:
        """Get the currently selected object ID (0 if none)."""
        ...
    def set_show_grid(self, show: bool) -> None:
        """Show or hide the editor grid."""
        ...
    def is_show_grid(self) -> bool:
        """Returns True if the editor grid is visible."""
        ...

    # Render Pipeline API
    def set_render_pipeline(self, asset_or_pipeline: Any = ...) -> None:
        """Set a custom render pipeline. Pass None for the default pipeline."""
        ...

    # Scene view visibility
    def set_scene_view_visible(self, visible: bool) -> None:
        """Show or hide the scene view panel."""
        ...
    def get_play_mode_manager(self) -> PlayModeManager:
        """Get the PlayModeManager instance."""
        ...

    # Window management
    def get_display_scale(self) -> float:
        """Return the OS display scale factor (e.g. 2.0 for 200% scaling)."""
        ...
    def set_fullscreen(self, fullscreen: bool) -> None:
        """Set the window to fullscreen or windowed mode."""
        ...
    def set_window_title(self, title: str) -> None:
        """Set the window title bar text."""
        ...
    def is_window_minimized(self) -> bool:
        """Return whether the editor window is minimized or occluded."""
        ...
    def set_maximized(self, maximized: bool) -> None:
        """Maximize or restore the window."""
        ...
    def set_resizable(self, resizable: bool) -> None:
        """Enable or disable window resizing."""
        ...
    def set_present_mode(self, mode: int) -> None:
        """Set swapchain present mode (0=IMMEDIATE, 1=MAILBOX, 2=FIFO, 3=FIFO_RELAXED)."""
        ...
    def get_present_mode(self) -> int:
        """Get the current swapchain present mode."""
        ...
    def pick_gizmo_axis(self, screen_x: float, screen_y: float, viewport_width: float, viewport_height: float) -> int:
        """Lightweight gizmo axis proximity test for hover highlighting."""
        ...
