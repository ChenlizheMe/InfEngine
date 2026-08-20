"""
PlayerGUI — fullscreen-borderless ImGui GUI for standalone game playback.

Registered as a single InxGUIRenderable that fills the entire window with
the game camera render target.  No editor chrome, no docking, no menus.

Optionally shows a **splash sequence** before revealing the game.  The
scene may finish loading while the window is black or showing splash;
Play starts only after that loading is done and the splash (if any) has
finished.  The game must not already be running when the player first
sees it.
"""

from __future__ import annotations

import os
import time
from typing import Callable, Dict, List, Optional

from Infernux.debug import Debug
from Infernux.lib import InxGUIRenderable, InxGUIContext
from Infernux.input import Input, KeyCode
from Infernux.engine.ui.viewport_utils import capture_viewport_info
from Infernux.ui.ui_event_system import UIEventProcessor
from Infernux.ui.ui_canvas_utils import collect_sorted_runtime_canvases


class PlayerGUI(InxGUIRenderable):
    """Renders the game camera output fullscreen with screen-space UI overlay."""

    def __init__(self, engine, *,
                 splash_items: Optional[List[Dict]] = None,
                 data_root: str = "",
                 control_channel=None,
                 activate_play: Optional[Callable[[], bool]] = None):
        super().__init__()
        self._engine = engine
        self._last_w = 0
        self._last_h = 0
        self._ui_event_processor = UIEventProcessor()
        self._last_frame_time = time.time()
        self._control = control_channel
        self._activate_play = activate_play
        self._play_started = False
        self._play_start_failed = False
        self._profile_frames = os.environ.get(
            "_INFERNUX_PLAYER_PROFILE_FRAMES", ""
        ).strip() == "1"
        self._next_profile_time = time.monotonic() + 2.0

        # Splash
        self._splash = None
        if splash_items:
            from Infernux.engine.splash_player import SplashPlayer
            self._splash = SplashPlayer(splash_items, data_root)

    # ------------------------------------------------------------------
    # InxGUIRenderable interface
    # ------------------------------------------------------------------

    def on_render(self, ctx: InxGUIContext):
        self._tick(ctx)

        # Full main viewport
        x0, y0, vp_w, vp_h = ctx.get_main_viewport_bounds()
        ctx.set_next_window_pos(x0, y0, 0, 0.0, 0.0)
        ctx.set_next_window_size(vp_w, vp_h, 0)

        # ImGui flags: NoTitleBar|NoResize|NoMove|NoScrollbar|NoCollapse
        #              |NoSavedSettings|NoNavInputs|NoNavFocus|NoDocking
        flags = (
            (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 5)
            | (1 << 8) | (1 << 16) | (1 << 17) | (1 << 19)
        )
        ctx.push_style_var_vec2(2, 0.0, 0.0)   # WindowPadding = (0,0)
        ctx.push_style_var_float(4, 0.0)        # WindowBorderSize = 0

        # ── Splash mode ───────────────────────────────────────────────
        if self._splash and not self._splash.is_finished:
            # Black background during splash
            ctx.push_style_color(2, 0.0, 0.0, 0.0, 1.0)  # ImGuiCol_WindowBg
            visible = ctx.begin_window("##PlayerFullscreen", True, flags)
            if visible:
                native = self._engine.get_native_engine()
                if native:
                    self._splash.update(ctx, native, x0, y0, vp_w, vp_h)
            ctx.end_window()
            ctx.pop_style_color(1)
            ctx.pop_style_var(2)

            if self._splash.is_finished:
                native = self._engine.get_native_engine()
                if native:
                    self._splash.cleanup(native)
                self._splash = None
            return

        # ── Normal game mode ──────────────────────────────────────────
        self.begin_play_when_ready()
        visible = ctx.begin_window("##PlayerFullscreen", True, flags)
        if visible:
            self._render_game(ctx, vp_w, vp_h)
        ctx.end_window()
        ctx.pop_style_var(2)  # WindowPadding + WindowBorderSize

    # ------------------------------------------------------------------

    def begin_play_when_ready(self) -> bool:
        """Start Play only after loading is done and splash (if any) has finished."""
        if self._play_started:
            return True
        if self._play_start_failed:
            return False
        if self._splash is not None and not self._splash.is_finished:
            return False
        activate = self._activate_play
        if activate is None:
            getter = getattr(self._engine, "get_player_runtime", None)
            session = getter() if callable(getter) else None
            if session is None:
                self._play_start_failed = True
                Debug.log_error(
                    "Player cannot start Play: runtime session is unavailable"
                )
                return False
            if getattr(session, "is_playing", False):
                self._play_started = True
                return True
            activate = getattr(session, "activate", None)
        if not callable(activate) or not activate():
            self._play_start_failed = True
            Debug.log_error("Player cannot start Play: initial scene is not ready")
            return False
        self._play_started = True
        return True

    def _tick(self, ctx):
        """Handle Player window input and debug-control polling.

        Play timing is owned by ``Engine.tick_play_mode``. This must not start
        the session before an optional project splash has finished.
        """
        # Standalone Players have no competing Editor viewport.  Establish the
        # gameplay-input contract before any early return caused by splash,
        # camera startup, a missing GUI texture, or background rendering.
        Input.set_game_focused(True)

        if self._engine:
            if (
                self._control is not None
                and self._control.poll(self._engine) == "shutdown"
            ):
                self._engine.request_exit()
                return

            # In player mode there's no MenuBarPanel, so we must handle
            # close requests (Alt+F4 / window X) directly.
            native = self._engine.get_native_engine()
            if native and native.is_close_requested():
                native.confirm_close()
                return

            if self._profile_frames and time.monotonic() >= self._next_profile_time:
                self._next_profile_time = time.monotonic() + 2.0
                try:
                    from Infernux.engine.player_bootstrap import _plog

                    snapshot = dict(native.renderer_frame_snapshot) if native else {}
                    _plog(f"[FrameProfile] {snapshot}")
                except Exception as exc:
                    Debug.log_suppressed("player_gui.frame_profile", exc)

    def _render_game(self, ctx: InxGUIContext, vp_w: float, vp_h: float):
        target_w = max(1, int(vp_w))
        target_h = max(1, int(vp_h))

        if target_w != self._last_w or target_h != self._last_h:
            self._engine.resize_game_render_target(target_w, target_h)
            self._last_w = target_w
            self._last_h = target_h

        game_tex = self._engine.get_game_texture_id()
        if game_tex == 0:
            ctx.label("Waiting for camera...")
            return

        ctx.image(game_tex, float(target_w), float(target_h), 0.0, 0.0, 1.0, 1.0)
        vp = capture_viewport_info(ctx)
        Input.set_game_viewport_origin(vp.image_min_x, vp.image_min_y)

        # Input: always game-focused in player mode.
        # Cursor lock is script-driven (Input.set_cursor_locked).
        game_hovered = ctx.is_window_hovered()

        # ESC safety: allow user to unlock cursor even if scripts forgot
        cursor_locked = Input.is_cursor_locked()
        if cursor_locked:
            if Input.get_key_down(KeyCode.ESCAPE):
                Input.set_cursor_locked(False)
                cursor_locked = False

        # Process UI events
        if game_hovered:
            self._process_ui_events(target_w, target_h)
        else:
            self._ui_event_processor.reset()

    def _process_ui_events(self, game_w: int, game_h: int):
        """Convert Input mouse state to per-canvas pointer events."""
        from Infernux.lib import SceneManager

        scene = SceneManager.instance().get_active_scene()
        if scene is None:
            return

        persistent_scene = SceneManager.instance().get_runtime_persistent_scene()
        canvases = collect_sorted_runtime_canvases(
            scene, persistent_scene, allow_stale_empty=True
        )
        if not canvases:
            self._ui_event_processor.reset()
            return

        # Mouse position in viewport pixels (relative to game image top-left).
        # In player mode display_scale is 1.0 (render target == viewport).
        gx, gy, scroll_x, scroll_y, mouse_held, mouse_down, mouse_up = Input.get_game_mouse_frame_state(0)

        # Build per-canvas positions in design (canvas) pixels
        canvas_positions = []
        for canvas in canvases:
            ref_w = float(canvas.reference_width)
            ref_h = float(canvas.reference_height)
            if ref_w < 1 or ref_h < 1:
                canvas_positions.append((0.0, 0.0))
                continue
            cx = gx * ref_w / float(game_w)
            cy = gy * ref_h / float(game_h)
            canvas_positions.append((cx, cy))

        scroll = (scroll_x, scroll_y)

        from Infernux.timing import Time
        dt = Time.unscaled_delta_time

        self._ui_event_processor.process(
            canvases, canvas_positions,
            mouse_down, mouse_up, mouse_held,
            scroll, dt,
        )
