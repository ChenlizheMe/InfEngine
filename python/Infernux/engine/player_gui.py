"""
PlayerGUI — fullscreen-borderless ImGui GUI for standalone game playback.

Registered as a single InxGUIRenderable that fills the entire window with
the game camera render target.  No editor chrome, no docking, no menus.

Optionally shows a **splash sequence** before revealing the game.  During
splash the game scene loads and starts in the background; when the sequence
finishes the game view is made visible instantly.
"""

from __future__ import annotations

import time
from typing import Dict, List, Optional

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
                 control_channel=None):
        super().__init__()
        self._engine = engine
        self._last_w = 0
        self._last_h = 0
        self._ui_event_processor = UIEventProcessor()
        self._last_frame_time = time.time()
        self._control = control_channel

        # Splash
        self._splash = None
        if splash_items:
            from Infernux.engine.splash_player import SplashPlayer
            self._splash = SplashPlayer(splash_items, data_root)

    # ------------------------------------------------------------------
    # InxGUIRenderable interface
    # ------------------------------------------------------------------

    def on_render(self, ctx: InxGUIContext):
        # Per-frame tick (play-mode timing + deferred tasks) — always run,
        # even during splash so the game world initialises behind the scenes.
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
        visible = ctx.begin_window("##PlayerFullscreen", True, flags)
        if visible:
            self._render_game(ctx, vp_w, vp_h)
        ctx.end_window()
        ctx.pop_style_var(2)  # WindowPadding + WindowBorderSize

    # ------------------------------------------------------------------

    def _tick(self, ctx):
        """Drive play-mode timing and deferred tasks each frame."""
        # DeferredTaskRunner is now ticked by InxRenderer's pre-GUI callback
        # (before BuildFrame) so scene mutations complete before panels render.

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
