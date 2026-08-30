"""
Unity-style Game View panel — renders the scene through the game camera.

The Game View uses a separate render target from the Scene View.
It displays what the player would see through the scene's main Camera component.
"""

import os
import configparser
from time import perf_counter as _pc
from typing import Optional
from Infernux.lib import InxGUIContext, SceneManager as _SM
from Infernux.engine.i18n import t
from Infernux.engine.interaction import (
    ContinuousEditService,
    PanelInteractionDescriptor,
    ViewCommandService,
)
from Infernux.input import Input, KeyCode
from Infernux.timing import Time
from Infernux.engine.play_mode import PlayModeManager
from Infernux.engine.runtime_change_journal import (
    RuntimeChangeDomain,
    runtime_change_journal,
)
from Infernux.engine.project_context import get_project_root
from Infernux.engine.project_view_settings import (
    load_project_view_settings,
    write_project_view_settings_section,
)
from Infernux.ui.ui_texture_cache import get_shared_cache as _get_tex_cache
from Infernux.ui.ui_render_dispatch import dispatch as _ui_dispatch
from Infernux.ui.ui_event_system import UIEventProcessor
from Infernux.ui.ui_button import UIButton
from Infernux.ui.inx_ui_screen_component import clear_rect_cache
from .game_input_policy import should_process_game_ui_events, should_route_game_input
from .runtime_canvas_snapshot import (
    collect_sorted_runtime_canvas_snapshot,
    runtime_canvas_snapshot_token,
)
from .editor_panel import EditorPanel
from .closable_panel import ClosablePanel
from .panel_registry import editor_panel
from .theme import Theme, ImGuiStyleVar
from .viewport_utils import capture_viewport_info
from Infernux.debug import Debug

_GAME_VIEWPORT_SEMANTIC_ID = "game_view.viewport"
_GAME_VIEW_FPS_SEMANTIC_ID = "game_view.fps"
_GAME_UI_BUTTON_SEMANTIC_PREFIX = "game_view.ui_button."


def _canvas_sort_order(canvas):
    return getattr(canvas, "sort_order", 0)


@editor_panel(
    "Game",
    type_id="game_view",
    title_key="panel.game",
    interaction=PanelInteractionDescriptor(),
)
class GameViewPanel(EditorPanel):
    """
    Unity-style Game View panel that renders the scene's main Camera output.
    
    The game camera is automatically bound to the first Camera component found
    in the scene (Scene.main_camera). When no camera is present, a helpful
    message is displayed instead.
    """
    
    WINDOW_TYPE_ID = "game_view"
    WINDOW_DISPLAY_NAME = "Game"

    def _document_is_dirty(self) -> bool:
        """Game View is a read-only projection of the Scene document."""
        return False

    _RESOLUTION_PRESETS = [
        ("1920\u00d71080", 1920, 1080),
        ("1280\u00d7720", 1280, 720),
        ("2560\u00d71440", 2560, 1440),
        ("3840\u00d72160", 3840, 2160),
        ("1080\u00d71920 Portrait", 1080, 1920),
        ("Custom", 1920, 1080),
    ]
    _PRESET_NAMES = [p[0] for p in _RESOLUTION_PRESETS]
    
    def __init__(self, title: str = "Game", engine=None, play_mode_manager: Optional[PlayModeManager] = None):
        super().__init__(title, window_id="game_view")
        self._engine = engine
        self._play_mode_manager = play_mode_manager
        if self._engine and self._play_mode_manager is None:
            self._play_mode_manager = self._engine.get_play_mode_manager()
        self.__is_playing = False
        
        # Game render target size tracking
        self._last_game_width = 0
        self._last_game_height = 0
        self._game_camera_was_enabled = False
        self._cached_game_texture_id = 0
        self._cached_game_texture_scene = None
        self._cached_game_camera_signature = None
        self._cached_game_texture_render_revision = None
        self._cached_game_texture_target_generation = -1
        self._game_texture_refresh_required = True

        # Retain the active scene's screen-space canvases across GUI builds.
        # The shared collector already caches its DFS, but asking it on every
        # Game View tick still crosses the Python/native scene boundary and
        # repeats version checks.  The cache is invalidated by scene identity
        # or structure_version; visual field changes are handled by the UI
        # render revision and do not require rediscovering canvases.
        self._cached_ui_scene = None
        self._cached_ui_snapshot_token = None
        self._cached_ui_canvases = ()
        self._cached_ui_sort_signature = ()

        # Focus tracking for view-owned interactions
        self._was_focused: bool = False
        self._on_focus_gained = None   # callback() when panel gains focus

        # UI event processor — dispatches pointer events to UI elements
        self._ui_event_processor = UIEventProcessor()

        # Game resolution selection (Unity-like)
        self._selected_resolution_idx = 0
        self._custom_width = 1920
        self._custom_height = 1080
        self._display_scale = 0.5
        self._fit_mode = True            # When True, scale auto-adjusts to fill area
        self._settings_loaded = False

        # Sample the renderer's real frame serial once per second. UI rendering
        # has its own 60 Hz cadence and must not be counted as engine frames.
        self._fps_sample_time = None
        self._fps_sample_frame = None
        self._fps_next_sample_time = None
        self._display_fps = 0.0
        self._display_frame_ms = 0.0
        # Game-only FPS (excludes editor panel overhead)
        self._display_game_fps = 0.0
        self._display_game_frame_ms = 0.0

    def _set_game_render_active(self, active: bool) -> None:
        """Keep C++ game rendering in lockstep with actual panel visibility.

        Hidden dock tabs do not execute the panel body, so all per-frame Game
        view maintenance stops. If C++ keeps rendering the Game graph anyway,
        it can submit work against stale Game-view state and trigger
        VK_ERROR_DEVICE_LOST when the tab is switched away or closed.
        """
        active = bool(active)
        if not active:
            Input.set_game_focused(False)

        if not self._engine:
            if not active:
                self._game_camera_was_enabled = False
            return

        if active and not self._game_camera_was_enabled:
            self._engine.set_game_camera_enabled(True)
            self._game_camera_was_enabled = True
        elif not active and self._game_camera_was_enabled:
            self._engine.set_game_camera_enabled(False)
            self._game_camera_was_enabled = False
    
    def set_engine(self, engine):
        self._engine = engine
        self._invalidate_game_texture_cache()
        if self._engine:
            self._play_mode_manager = self._engine.get_play_mode_manager()
    
    def set_play_mode_manager(self, manager: PlayModeManager):
        self._play_mode_manager = manager
    
    def _is_playing(self) -> bool:
        if self._play_mode_manager:
            return self._play_mode_manager.is_playing
        return self.__is_playing
    
    def _is_paused(self) -> bool:
        if self._play_mode_manager:
            return self._play_mode_manager.is_paused
        return False
    
    def _settings_ini_path(self) -> Optional[str]:
        root = get_project_root()
        if not root:
            return None
        return os.path.join(root, "ProjectSettings", "GameView.ini")

    def _load_resolution_settings(self):
        if self._settings_loaded:
            return
        self._settings_loaded = True

        path = self._settings_ini_path()
        if not path:
            return
        if not os.path.isfile(path):
            self._save_resolution_settings()
            return

        try:
            cp = load_project_view_settings(path)
        except (OSError, configparser.Error) as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            return
        if "GameView" not in cp:
            return

        section = cp["GameView"]
        self._selected_resolution_idx = max(0, min(len(self._RESOLUTION_PRESETS) - 1,
                                                   section.getint("preset_index", fallback=0)))
        self._custom_width = max(64, section.getint("custom_width", fallback=1920))
        self._custom_height = max(64, section.getint("custom_height", fallback=1080))
        self._display_scale = max(0.1, min(2.0, section.getfloat("display_scale", fallback=0.5)))
        self._fit_mode = section.getboolean("fit_mode", fallback=True)

    def _save_resolution_settings(self):
        path = self._settings_ini_path()
        if not path:
            return

        write_project_view_settings_section(path, "GameView", {
            "preset_index": str(self._selected_resolution_idx),
            "custom_width": str(max(64, int(self._custom_width))),
            "custom_height": str(max(64, int(self._custom_height))),
            "display_scale": f"{self._display_scale:.3f}",
            "fit_mode": str(self._fit_mode),
        })

    def _capture_view_state(self):
        return (
            int(self._selected_resolution_idx),
            int(self._custom_width),
            int(self._custom_height),
            round(float(self._display_scale), 3),
            bool(self._fit_mode),
        )

    def _apply_view_state(self, state, *, persist: bool = True):
        preset, width, height, scale, fit_mode = state
        self._selected_resolution_idx = max(
            0,
            min(len(self._RESOLUTION_PRESETS) - 1, int(preset)),
        )
        self._custom_width = max(64, min(8192, int(width)))
        self._custom_height = max(64, min(8192, int(height)))
        self._display_scale = max(0.1, min(2.0, round(float(scale), 3)))
        self._fit_mode = bool(fit_mode)
        if persist:
            self._save_resolution_settings()

    def _commit_view_state(self, before, description: str) -> bool:
        after = self._capture_view_state()
        if before == after:
            return False
        recorded = ViewCommandService.require().set_value(
            before,
            after,
            self._apply_view_state,
            description=description,
        )
        if not recorded:
            # Keep persistence functional when editor history is unavailable.
            self._save_resolution_settings()
        return True

    def _track_continuous_view_edit(
        self,
        ctx,
        key: str,
        before,
        *,
        changed: bool,
        description: str,
    ) -> None:
        is_active = getattr(ctx, "is_item_active", None)
        is_deactivated = getattr(ctx, "is_item_deactivated_after_edit", None)
        active = bool(is_active()) if callable(is_active) else False
        deactivated = bool(is_deactivated()) if callable(is_deactivated) else False
        edits = ContinuousEditService.instance()
        session_key = f"{self.window_id}:view:{key}"
        session = edits.get(session_key)

        if active and session is None:
            edits.commit_owner(self.window_id)
            session = edits.begin(
                session_key,
                owner_id=self.window_id,
                description=description,
                initial_value=before,
                on_commit=self._commit_continuous_view_edit,
                on_cancel=self._cancel_continuous_view_edit,
            )
        if changed and session is not None:
            edits.update(session_key, self._capture_view_state())

        if deactivated:
            if edits.get(session_key) is not None:
                edits.commit(session_key)
            elif changed:
                self._commit_view_state(before, description)
        elif changed and not active:
            # Keyboard submission and lightweight test contexts are discrete.
            self._commit_view_state(before, description)

    def _commit_continuous_view_edit(self, session) -> bool:
        self._apply_view_state(session.current_value, persist=False)
        return self._commit_view_state(session.initial_value, session.description)

    def _cancel_continuous_view_edit(self, session) -> None:
        self._apply_view_state(session.initial_value, persist=False)

    def _commit_pending_view_edits(self) -> None:
        ContinuousEditService.instance().commit_owner(self.window_id)

    def _set_resolution_preset(self, preset_index: int) -> bool:
        before = self._capture_view_state()
        self._selected_resolution_idx = max(
            0,
            min(len(self._RESOLUTION_PRESETS) - 1, int(preset_index)),
        )
        return self._commit_view_state(before, "Change Game View Resolution")

    def _current_target_resolution(self):
        _, w, h = self._RESOLUTION_PRESETS[self._selected_resolution_idx]
        if self._selected_resolution_idx == len(self._RESOLUTION_PRESETS) - 1:
            return max(64, int(self._custom_width)), max(64, int(self._custom_height))
        return int(w), int(h)

    def _fit_scale(self):
        """Toggle Fit mode on."""
        before = self._capture_view_state()
        self._fit_mode = True
        self._commit_view_state(before, "Fit Game View")

    @staticmethod
    def _fit_into_region(src_w: int, src_h: int, region_w: float, region_h: float):
        if src_w <= 0 or src_h <= 0 or region_w <= 0 or region_h <= 0:
            return 0.0, 0.0
        scale = min(region_w / float(src_w), region_h / float(src_h))
        return float(src_w) * scale, float(src_h) * scale

    # ------------------------------------------------------------------
    # EditorPanel hooks
    # ------------------------------------------------------------------

    def _initial_size(self):
        return (Theme.UI_EDITOR_INIT_WINDOW_W, Theme.UI_EDITOR_INIT_WINDOW_H)

    def _window_flags(self) -> int:
        return Theme.WINDOW_FLAGS_VIEWPORT | Theme.WINDOW_FLAGS_NO_SCROLL

    def save_state(self) -> dict:
        # Game view state is runtime-driven and must not be serialized.
        return {}

    def load_state(self, data: dict) -> None:
        # Intentionally ignore persisted data for Game View.
        return

    def on_enable(self):
        # Runtime caches must always start from a cold state after open.
        self._last_game_width = 0
        self._last_game_height = 0
        self._game_camera_was_enabled = False
        self._invalidate_game_texture_cache()
        self._invalidate_ui_scene_cache()

    def on_disable(self):
        self._commit_pending_view_edits()
        self._set_game_render_active(False)

    def _on_not_visible(self, ctx):
        self._commit_pending_view_edits()
        self._was_focused = False
        Input.set_game_focused(False)
        self._ui_event_processor.reset()
        from Infernux.acceptance import RuntimeAcceptance

        if RuntimeAcceptance.is_active():
            # RuntimeAcceptance owns the Game render path even while automated
            # scene loads transiently hide or move focus away from this dock.
            self._set_game_render_active(True)
            return
        # Disable game rendering when the panel is invisible (e.g. another
        # tab like UI Editor covers this dock).  Without this, C++ keeps
        # submitting draw commands against a stale game render target, which
        # can trigger VK_ERROR_DEVICE_LOST.  Rendering is re-enabled in
        # on_render_content() once the panel becomes visible again.
        self._set_game_render_active(False)

    def _on_visible_pre(self, ctx):
        self._load_resolution_settings()
        focused = ClosablePanel.get_active_view_id() == self.window_id
        if focused and not self._was_focused:
            if self._on_focus_gained:
                self._on_focus_gained()
        self._was_focused = focused

    def _invalidate_ui_scene_cache(self) -> None:
        """Drop retained scene/UI discovery state after a panel reset."""
        self._cached_ui_scene = None
        self._cached_ui_snapshot_token = None
        self._cached_ui_canvases = ()
        self._cached_ui_sort_signature = ()

    def _invalidate_game_texture_cache(self) -> None:
        """Refresh the editor texture handle at the next visible frame."""
        self._cached_game_texture_id = 0
        self._cached_game_texture_scene = None
        self._cached_game_camera_signature = None
        self._cached_game_texture_render_revision = None
        self._cached_game_texture_target_generation = -1
        self._game_texture_refresh_required = True

    @staticmethod
    def _game_camera_signature(scene):
        if scene is None:
            return None
        try:
            camera = scene.main_camera
        except (AttributeError, RuntimeError):
            camera = None
        if camera is None:
            # Scene.main_camera is the authored preference, while native game
            # rendering also accepts the first active Camera as a fallback.
            # Retain the structure revision in the no-preference state so a
            # newly attached Camera invalidates a cached "no camera" result.
            return (
                "scene_structure",
                int(getattr(scene, "structure_version", 0) or 0),
            )
        try:
            owner = camera.game_object
        except (AttributeError, RuntimeError):
            owner = None
        return (
            int(getattr(camera, "component_id", 0) or id(camera)),
            bool(getattr(camera, "enabled", True)),
            int(getattr(owner, "id", 0) or 0),
            bool(getattr(owner, "active_in_hierarchy", True)),
        )

    @staticmethod
    def _game_texture_render_revision() -> tuple[int, int]:
        journal = runtime_change_journal()
        return (
            journal.domain_revision(RuntimeChangeDomain.RENDER_STACK),
            journal.domain_revision(RuntimeChangeDomain.RENDER_STATE),
        )

    def _get_game_texture_id(self, scene) -> int:
        """Return the retained ImGui handle for the current Game target.

        The native getter resolves the active camera stack. Native camera
        discovery is deliberately frame-local, so calling it from the editor
        panel every UI frame turns a large scene into an O(object-count) panel
        cost. The render target handle itself is stable until resize or render
        configuration replacement, and camera availability has a cheap stable
        signature exposed by Scene.main_camera.
        """
        camera_signature = self._game_camera_signature(scene)
        render_revision = self._game_texture_render_revision()
        generation_getter = getattr(
            self._engine,
            "get_game_render_target_generation",
            None,
        )
        target_generation = (
            int(generation_getter() or 0) if callable(generation_getter) else 0
        )
        refresh = (
            self._game_texture_refresh_required
            or scene is not self._cached_game_texture_scene
            or camera_signature != self._cached_game_camera_signature
            or render_revision != self._cached_game_texture_render_revision
            or target_generation != self._cached_game_texture_target_generation
        )
        if refresh:
            self._cached_game_texture_id = int(
                self._engine.get_game_texture_id() or 0
            )
            self._cached_game_texture_scene = scene
            self._cached_game_camera_signature = camera_signature
            self._cached_game_texture_render_revision = render_revision
            self._cached_game_texture_target_generation = target_generation
            # A configured camera can precede lazy render-target readiness by
            # one frame. Retry only that bounded startup case.
            self._game_texture_refresh_required = (
                self._cached_game_texture_id == 0
                and camera_signature is not None
                and camera_signature[0] != "scene_structure"
            )
        return self._cached_game_texture_id

    def _get_scene_and_canvases(self):
        """Return the active scene and its sorted screen-space canvases.

        Game View is rebuilt at the editor UI cadence, while canvas discovery
        only changes with the active scene epoch or Canvas membership revision.
        Keeping this snapshot at the panel boundary avoids repeated scene and
        canvas queries without hiding any UI updates: property changes are
        still observed by the runtime UI revision and input uses the current
        cached canvas objects.
        """
        scene_manager = _SM.instance()
        scene = scene_manager.get_active_scene()
        get_persistent_scene = getattr(
            scene_manager, "get_runtime_persistent_scene", None
        )
        persistent_scene = (
            get_persistent_scene() if callable(get_persistent_scene) else None
        )
        if scene is None:
            if self._cached_ui_scene is not None:
                self._invalidate_ui_scene_cache()
            return None, ()

        scene_identity = (scene, persistent_scene)
        snapshot_token = runtime_canvas_snapshot_token(scene, persistent_scene)
        if (
            scene_identity != self._cached_ui_scene
            or snapshot_token != self._cached_ui_snapshot_token
        ):
            canvases = collect_sorted_runtime_canvas_snapshot(scene, persistent_scene)
            # Rectangles are retained by the UI components themselves.  Only
            # clear them when the scene snapshot changes; doing this every GUI
            # build defeats the cache and needlessly invalidates layout work.
            clear_rect_cache(snapshot_token)
            self._cached_ui_scene = scene_identity
            self._cached_ui_snapshot_token = snapshot_token
            # The shared collector owns initial ordering and already promises
            # a sorted result. Retain it directly instead of sorting twice.
            self._cached_ui_canvases = tuple(canvases)
            self._cached_ui_sort_signature = tuple(
                _canvas_sort_order(canvas)
                for canvas in self._cached_ui_canvases
            )

        elif self._cached_ui_canvases:
            # Canvas.sort_order is a visual ordering property, not a scene
            # structure mutation.  Re-read only the existing bounded list and
            # re-sort locally when it changes; no hierarchy traversal is
            # needed, and the retained tuple never remains in stale order.
            sort_signature = tuple(
                _canvas_sort_order(canvas)
                for canvas in self._cached_ui_canvases
            )
            if sort_signature != self._cached_ui_sort_signature:
                self._cached_ui_canvases = tuple(
                    sorted(self._cached_ui_canvases, key=_canvas_sort_order)
                )
                self._cached_ui_sort_signature = tuple(
                    _canvas_sort_order(canvas)
                    for canvas in self._cached_ui_canvases
                )
        return scene, self._cached_ui_canvases

    def on_render_content(self, ctx: InxGUIContext):
        if not self._engine:
            ctx.label(t("game_view.engine_not_init"))
            return

        # Ensure native Game rendering is enabled once the panel has rendered.
        # Do not disable it on transient dock/tab invisibility because scene
        # switches can temporarily interrupt panel visibility for a frame.
        self._set_game_render_active(True)

        self._render_resolution_toolbar(ctx)
        target_w, target_h, fit_scale = self._render_scale_toolbar(ctx)
        self._render_fps_counter(ctx)

        ctx.new_line()

        self._render_game_viewport(ctx, target_w, target_h, fit_scale)

    def _render_resolution_toolbar(self, ctx):
        """Resolution preset combo and optional custom width/height inputs."""
        old_idx = self._selected_resolution_idx
        ctx.set_next_item_width(140)
        selected_idx = ctx.combo("##Resolution", old_idx, self._PRESET_NAMES, -1)
        if selected_idx != old_idx:
            self._set_resolution_preset(selected_idx)

        if self._selected_resolution_idx == len(self._RESOLUTION_PRESETS) - 1:
            ctx.same_line(0, 8)
            width_before = self._capture_view_state()
            ctx.set_next_item_width(56)
            new_width = int(ctx.drag_int("##CW", self._custom_width, 1.0, 64, 8192))
            width_changed = new_width != self._custom_width
            self._custom_width = new_width
            self._track_continuous_view_edit(
                ctx,
                "custom_width",
                width_before,
                changed=width_changed,
                description="Change Game View Width",
            )
            ctx.same_line(0, 2)
            ctx.label(Theme.ICON_REMOVE)
            ctx.same_line(0, 2)
            height_before = self._capture_view_state()
            ctx.set_next_item_width(56)
            new_height = int(ctx.drag_int("##CH", self._custom_height, 1.0, 64, 8192))
            height_changed = new_height != self._custom_height
            self._custom_height = new_height
            self._track_continuous_view_edit(
                ctx,
                "custom_height",
                height_before,
                changed=height_changed,
                description="Change Game View Height",
            )

    def _render_scale_toolbar(self, ctx):
        """Scale slider, percentage label, and Fit button.

        Returns ``(target_w, target_h, fit_scale)``.
        """
        avail_width = ctx.get_content_region_avail_width()
        avail_height = ctx.get_content_region_avail_height()
        target_w, target_h = self._current_target_resolution()

        fit_scale = 1.0
        if target_w > 0 and target_h > 0 and avail_width > 0 and avail_height > 0:
            fit_scale = min(avail_width / float(target_w), avail_height / float(target_h))
            fit_scale = max(0.01, fit_scale)

        if self._fit_mode:
            self._display_scale = fit_scale

        ctx.same_line(0, 12)
        pct = int(round(self._display_scale * 100))
        scale_label_x = ctx.get_cursor_pos_x()
        scale_label_w, _ = ctx.calc_text_size("200%")
        ctx.label(f"{pct}%")
        ctx.same_line(scale_label_x + scale_label_w + 4.0)
        ctx.set_next_item_width(230)
        scale_before = self._capture_view_state()
        old_scale = self._display_scale
        new_scale = round(ctx.float_slider("##Scale", old_scale, 0.10, 2.0), 3)
        scale_changed = abs(old_scale - new_scale) > 0.001
        self._display_scale = new_scale
        if scale_changed:
            self._fit_mode = False
        self._track_continuous_view_edit(
            ctx,
            "display_scale",
            scale_before,
            changed=scale_changed,
            description="Change Game View Scale",
        )
        ctx.same_line(0, 6)
        ctx.align_text_to_frame_padding()
        fit_label = t("game_view.fit")
        fit_w = max(44.0, ctx.calc_text_width(fit_label) + 12.0)
        color_count = Theme.push_inline_button_style(ctx, active=self._fit_mode)
        ctx.push_style_var_float(ImGuiStyleVar.FrameBorderSize, 0.0)
        ctx.button(f"{fit_label}##game_view_fit", self._fit_scale, width=fit_w, height=0)
        ctx.pop_style_var(1)
        ctx.pop_style_color(color_count)

        return target_w, target_h, fit_scale

    def _render_fps_counter(self, ctx):
        """FPS counter (right-aligned, Unity-style)."""
        is_playing = self._is_playing()
        now = _pc()
        snapshot = None
        # The native snapshot is only needed when the displayed one-second
        # sample can change. Avoid a pybind property read on every GUI tick.
        if is_playing and (
            self._fps_next_sample_time is None
            or now >= self._fps_next_sample_time
        ):
            native_engine = getattr(self._engine, '_engine', None)
            snapshot = getattr(native_engine, 'renderer_frame_snapshot', None)
            if snapshot is not None:
                frame = int(snapshot.get('frame', 0))
                if (
                    self._fps_sample_time is None
                    or self._fps_sample_frame is None
                    or frame < self._fps_sample_frame
                ):
                    self._fps_sample_time = now
                    self._fps_sample_frame = frame
                else:
                    elapsed = now - self._fps_sample_time
                    if elapsed >= 1.0:
                        completed_frames = frame - self._fps_sample_frame
                        self._display_fps = completed_frames / elapsed
                        self._display_frame_ms = (
                            elapsed * 1000.0 / completed_frames
                            if completed_frames > 0 else 0.0
                        )
                        game_frame_ms = float(snapshot.get('game_only_frame_ms', 0.0))
                        self._display_game_frame_ms = max(game_frame_ms, 0.0)
                        self._display_game_fps = (
                            1000.0 / game_frame_ms if game_frame_ms > 0.0 else 0.0
                        )
                        self._fps_sample_time = now
                        self._fps_sample_frame = frame
                self._fps_next_sample_time = now + 1.0
        elif not is_playing:
            self._fps_sample_time = None
            self._fps_sample_frame = None
            self._fps_next_sample_time = None
            self._display_fps = 0.0
            self._display_frame_ms = 0.0

        fps_text = (
            f"FPS: {self._display_fps:.0f} ({self._display_frame_ms:.1f} ms)"
            if is_playing and self._fps_sample_time is not None
            else "FPS: --"
        )
        if fps_text != getattr(self, '_cached_fps_text', None):
            self._cached_fps_text = fps_text
            self._cached_fps_text_w, _ = ctx.calc_text_size(fps_text)
        text_w = self._cached_fps_text_w
        window_width = ctx.get_window_width()
        fps_x = max(window_width - text_w - 24.0, 360.0)
        if fps_x + text_w <= window_width - 12.0:
            ctx.same_line(fps_x)
            ctx.label(fps_text)
            if bool(getattr(ctx, "semantic_capture_enabled", False)):
                ctx.record_semantic_item(
                    "performance",
                    fps_text,
                    False,
                    _GAME_VIEW_FPS_SEMANTIC_ID,
                )

    def _route_game_input(self, ctx, target_w, target_h,
                          viewport_hovered, viewport_clicked, canvases):
        """Handle focus, cursor lock, and UI event routing after viewport render."""
        if viewport_clicked:
            self._activate_panel(ctx, focus_window=True)

        is_playing = self._is_playing()
        panel_focused = ClosablePanel.get_active_view_id() == self.window_id

        cursor_locked = Input.is_cursor_locked()
        if cursor_locked:
            if Input.get_key_down(KeyCode.ESCAPE):
                Input.set_cursor_locked(False)
                cursor_locked = False

        game_input_active = should_route_game_input(
            is_playing=is_playing,
            panel_focused=panel_focused,
            cursor_locked=cursor_locked,
        )
        # Focus must be visible to Input before querying the screen UI mouse
        # state below. Otherwise a Game View's first pointer down/up can be
        # gated away while the invisible viewport overlay owns the ImGui item.
        Input.set_game_focused(game_input_active)

        if not is_playing and cursor_locked:
            Input.set_cursor_locked(False)

        if should_process_game_ui_events(
            is_playing=is_playing,
            panel_focused=panel_focused,
            cursor_locked=cursor_locked,
        ):
            self._process_ui_events(target_w, target_h, canvases=canvases)
        else:
            self._ui_event_processor.reset()

    def _render_game_viewport(self, ctx, target_w, target_h, fit_scale):
        """Render the game texture, screen UI, and route input events."""
        # Recompute fit after the toolbar row has consumed layout height.
        viewport_avail_width = ctx.get_content_region_avail_width()
        viewport_avail_height = ctx.get_content_region_avail_height()
        if self._fit_mode and target_w > 0 and target_h > 0 and viewport_avail_width > 0 and viewport_avail_height > 0:
            fit_scale = min(viewport_avail_width / float(target_w), viewport_avail_height / float(target_h))
            self._display_scale = max(0.01, fit_scale)

        draw_w = float(target_w) * self._display_scale
        draw_h = float(target_h) * self._display_scale

        if target_w != self._last_game_width or target_h != self._last_game_height:
            self._game_texture_refresh_required = True
            self._engine.resize_game_render_target(target_w, target_h)
            self._last_game_width = target_w
            self._last_game_height = target_h

        # Pre-fetch scene + canvases once (used by both render and events).
        # Canvas membership has its own revision and is unrelated to ordinary
        # 3D scene topology changes.
        _scene, _canvases = self._get_scene_and_canvases()
        game_texture_id = self._get_game_texture_id(_scene)

        viewport_hovered = False
        viewport_clicked = False

        if ctx.begin_child("##GameViewportRegion", 0, 0, False):
            avail_width = ctx.get_content_region_avail_width()
            avail_height = ctx.get_content_region_avail_height()

            if self._fit_mode and target_w > 0 and target_h > 0 and avail_width > 0 and avail_height > 0:
                # Leave a tiny epsilon so float rounding does not create a child scrollbar.
                fit_region_w = max(0.0, avail_width - 1.0)
                fit_region_h = max(0.0, avail_height - 1.0)
                draw_w, draw_h = self._fit_into_region(target_w, target_h, fit_region_w, fit_region_h)
                self._display_scale = max(0.01, min(draw_w / float(target_w), draw_h / float(target_h)))

            cursor_start_x = ctx.get_cursor_pos_x()
            cursor_start_y = ctx.get_cursor_pos_y()

            if game_texture_id != 0:
                pad_x = max(0.0, (avail_width - draw_w) * 0.5)
                pad_y = max(0.0, (avail_height - draw_h) * 0.5)
                ctx.set_cursor_pos_x(cursor_start_x + pad_x)
                ctx.set_cursor_pos_y(cursor_start_y + pad_y)
                ctx.image(game_texture_id, float(draw_w), float(draw_h), 0.0, 0.0, 1.0, 1.0)

                # Images are visual-only ImGui items. Overlay an invisible
                # button on the same rectangle so real clicks, including MCP
                # synthetic input, activate the Game View's interaction item.
                ctx.set_cursor_pos_x(cursor_start_x + pad_x)
                ctx.set_cursor_pos_y(cursor_start_y + pad_y)
                viewport_clicked = ctx.invisible_button(
                    "##GameViewportInput", float(draw_w), float(draw_h)
                )
                vp = capture_viewport_info(ctx)
                # Expose the interactive overlay, so MCP focuses the same
                # surface a player clicks before sending game input.
                ctx.record_semantic_item(
                    "viewport", "Game Viewport", True, _GAME_VIEWPORT_SEMANTIC_ID
                )
                # The overlay has a stable item id. The child-window check
                # still prevents input from reaching this panel through a
                # floating Editor window.
                viewport_hovered = ctx.is_item_hovered() and ctx.is_window_hovered()
                # ImGui buttons normally report activation on release. Screen
                # UI needs the matching press frame too, so focus the Game View
                # as soon as the pointer is pressed over its real viewport.
                viewport_pressed = bool(ctx.is_mouse_button_clicked(0))
                viewport_clicked = viewport_hovered and (viewport_clicked or viewport_pressed)
                Input.set_game_viewport_origin(vp.image_min_x, vp.image_min_y)

                self._render_screen_ui(ctx, vp.image_min_x, vp.image_min_y,
                                       float(draw_w), float(draw_h),
                                       vp.image_min_x, vp.image_min_y,
                                       vp.image_min_x + float(draw_w),
                                       vp.image_min_y + float(draw_h),
                                       scene=_scene, canvases=_canvases)

            else:
                Input.set_game_viewport_origin(0.0, 0.0)
                ctx.label("")
                ctx.label("  " + t("game_view.no_camera"))
                ctx.label("  " + t("game_view.no_camera_detail"))
                ctx.label("")
                ctx.label("  " + t("game_view.create_camera_hint_1"))
                ctx.label("  " + t("game_view.create_camera_hint_2"))
        ctx.end_child()

        self._route_game_input(ctx, target_w, target_h,
                               viewport_hovered, viewport_clicked, _canvases)

    # ------------------------------------------------------------------
    # Screen-space UI overlay
    # ------------------------------------------------------------------

    def _render_screen_ui(self, ctx: InxGUIContext, vp_x: float, vp_y: float,
                          vp_w: float, vp_h: float,
                          clip_min_x: float = 0.0, clip_min_y: float = 0.0,
                          clip_max_x: float = 1e9, clip_max_y: float = 1e9,
                          scene=None, canvases=None):
        """Present UI semantics and the disabled-renderer fallback.

        GPU ScreenUI command submission is owned by
        :class:`RuntimeScreenUISubmission` at camera render submission.
        This panel only maps the resulting game image to editor interaction.
        """
        if not self._engine or scene is None:
            return

        game_w = self._last_game_width
        game_h = self._last_game_height
        if game_w < 1 or game_h < 1:
            return

        if canvases is None:
            scene_manager = _SM.instance()
            get_persistent_scene = getattr(
                scene_manager, "get_runtime_persistent_scene", None
            )
            canvases = collect_sorted_runtime_canvas_snapshot(
                scene,
                get_persistent_scene() if callable(get_persistent_scene) else None,
            )
        if not canvases:
            return

        renderer = self._engine.get_screen_ui_renderer()
        use_overlay = renderer is not None and not renderer.is_enabled()
        get_texture_id = None
        if use_overlay:
            get_texture_id = _get_tex_cache().get_bound(self._engine)

        for canvas in canvases:
            self._render_canvas_screen_ui(
                ctx, canvas, use_overlay, get_texture_id,
                game_w, game_h, vp_x, vp_y, vp_w, vp_h,
            )

    def _render_canvas_screen_ui(self, ctx, canvas, use_overlay,
                                 get_texture_id, game_w, game_h,
                                 vp_x, vp_y, vp_w, vp_h):
        """Present one canvas without mutating native ScreenUI commands."""
        from Infernux.ui.enums import RenderMode

        canvas_go = canvas.game_object
        if canvas_go is not None and not canvas_go.active_in_hierarchy:
            return
        if not getattr(canvas, 'enabled', True):
            return
        if canvas.render_mode not in (RenderMode.CameraOverlay, RenderMode.ScreenOverlay):
            return

        ref_w = float(canvas.reference_width)
        ref_h = float(canvas.reference_height)
        if ref_w < 1 or ref_h < 1:
            return

        scale_x, scale_y, _ = canvas.compute_scale(float(game_w), float(game_h))
        logical_w, logical_h = canvas.compute_logical_size(
            float(game_w), float(game_h)
        )
        semantic_capture_enabled = bool(getattr(ctx, "semantic_capture_enabled", False))

        for elem in canvas._get_elements():
            elem_go = elem.game_object
            if elem_go is not None and not elem_go.active_in_hierarchy:
                continue
            if not getattr(elem, 'enabled', True):
                continue

            ex, ey, ew, eh = elem.get_rect(logical_w, logical_h)
            if semantic_capture_enabled:
                vx, vy, vw, vh = elem.get_visual_rect(logical_w, logical_h)
                self._record_game_ui_button_semantic(
                    ctx,
                    elem,
                    vp_x + vx * scale_x * (vp_w / float(game_w)),
                    vp_y + vy * scale_y * (vp_h / float(game_h)),
                    vw * scale_x * (vp_w / float(game_w)),
                    vh * scale_y * (vp_h / float(game_h)),
                )

            if use_overlay:
                ovl_scale_x = scale_x * vp_w / float(game_w)
                ovl_scale_y = scale_y * vp_h / float(game_h)
                _ui_dispatch(
                    elem, "editor",
                    ctx=ctx,
                    base_sx=vp_x + ex * ovl_scale_x,
                    base_sy=vp_y + ey * ovl_scale_y,
                    base_sw=ew * ovl_scale_x,
                    base_sh=eh * ovl_scale_y,
                    zoom=min(ovl_scale_x, ovl_scale_y),
                    get_tex_id=get_texture_id,
                )

    def _record_game_ui_button_semantic(self, ctx, elem, x, y, width, height):
        if not isinstance(elem, UIButton):
            return
        recorder = getattr(ctx, "record_semantic_rect", None)
        if not callable(recorder):
            return
        go = getattr(elem, "game_object", None)
        object_id = int(getattr(go, "id", 0) or 0)
        if object_id <= 0:
            return
        label = str(getattr(elem, "label", "") or getattr(go, "name", "Button")).strip() or "Button"
        enabled = self._is_playing() and bool(getattr(elem, "interactable", True))
        enabled = enabled and bool(getattr(elem, "raycast_target", True))
        recorder(
            "game_ui_button",
            label,
            float(x),
            float(y),
            max(float(width), 0.0),
            max(float(height), 0.0),
            enabled,
            f"{_GAME_UI_BUTTON_SEMANTIC_PREFIX}{object_id}",
        )

    # ------------------------------------------------------------------
    # UI event processing
    # ------------------------------------------------------------------

    def _process_ui_events(self, game_w: int, game_h: int, canvases=None):
        """Convert Input mouse state to per-canvas pointer events."""
        if canvases is None:
            from Infernux.lib import SceneManager
            scene_manager = SceneManager.instance()
            scene = scene_manager.get_active_scene()
            if scene is None:
                return
            get_persistent_scene = getattr(
                scene_manager, "get_runtime_persistent_scene", None
            )
            canvases = collect_sorted_runtime_canvas_snapshot(
                scene,
                get_persistent_scene() if callable(get_persistent_scene) else None,
            )
        if not canvases:
            return

        # Mouse position in viewport pixels (relative to game image top-left)
        vp_x, vp_y, scroll_x, scroll_y, mouse_held, mouse_down, mouse_up = Input.get_game_mouse_frame_state(0)
        display_scale = self._display_scale
        if display_scale < 1e-6:
            return

        # Convert viewport pixels → game-resolution pixels
        game_px = vp_x / display_scale
        game_py = vp_y / display_scale

        # Build per-canvas positions in design (canvas) pixels
        canvas_positions = []
        for canvas in canvases:
            ref_w = float(canvas.reference_width)
            ref_h = float(canvas.reference_height)
            if ref_w < 1 or ref_h < 1:
                canvas_positions.append((0.0, 0.0))
                continue
            scale_x, scale_y, _ = canvas.compute_scale(float(game_w), float(game_h))
            cx = game_px / max(scale_x, 1e-6)
            cy = game_py / max(scale_y, 1e-6)
            canvas_positions.append((cx, cy))

        scroll = (scroll_x, scroll_y)

        from Infernux.timing import Time
        dt = Time.unscaled_delta_time

        self._ui_event_processor.process(
            canvases, canvas_positions,
            mouse_down, mouse_up, mouse_held,
            scroll, dt,
        )
