"""
PlayerBootstrap — minimal startup sequence for standalone game playback.

Replaces :class:`EditorBootstrap` with a stripped-down path that:
  1. Creates the Engine (no editor panels)
  2. Loads tag/layer settings
  3. Sets up SceneFileManager + PlayModeManager (scene loading needs them)
  4. Enables the game camera
  5. Registers the fullscreen PlayerGUI (with optional splash sequence)
  6. Loads the first scene from BuildSettings.json (or a Supervisor-approved Debug validation scene)
  7. Enters play mode
  8. Runs the main loop

No undo, no selection, no hierarchy, no inspector, no docking layout.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, List, Optional

import Infernux.resources as _resources
from Infernux.engine.engine import Engine, LogLevel
from Infernux.engine.scene_manager import SceneFileManager
from Infernux.engine.play_mode import PlayModeManager
from Infernux.engine.player_gui import PlayerGUI
from Infernux.engine.path_utils import is_path_within, resolved_path, safe_path as _safe_path
from Infernux.debug import Debug

_log = logging.getLogger("Infernux.player")


def _plog(msg):
    """Write to player.log (only available in packaged builds)."""
    path = os.environ.get("_INFERNUX_PLAYER_LOG")
    if not path:
        # Fallback: write into Data/Logs/ next to the executable
        import sys as _sys
        _exe = getattr(_sys, 'executable', '') or ''
        _d = os.path.dirname(resolved_path(_exe))
        _logs_dir = os.path.join(_d, "Data", "Logs")
        os.makedirs(_logs_dir, exist_ok=True)
        path = os.path.join(_logs_dir, "player.log")
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(str(msg) + "\n")
    except OSError as exc:
        Debug.log_suppressed("player_bootstrap.write_player_log", exc)


class PlayerBootstrap:
    """Orchestrates the standalone player startup sequence."""

    def __init__(
        self,
        project_path: str,
        engine_log_level=LogLevel.Info,
        *,
        display_mode: str = "fullscreen_borderless",
        window_width: int = 1920,
        window_height: int = 1080,
        splash_items: Optional[List[Dict]] = None,
    ):
        self.project_path = project_path
        self.engine_log_level = engine_log_level
        self.display_mode = display_mode
        self.window_width = window_width
        self.window_height = window_height
        self.splash_items = splash_items or []
        self.engine: Optional[Engine] = None
        self.scene_file_manager: Optional[SceneFileManager] = None
        self._player_gui: Optional[PlayerGUI] = None

    # ── Public entry point ─────────────────────────────────────────────

    def run(self):
        """Execute all bootstrap phases and start the main loop."""
        self._ensure_project_requirements()
        self._init_engine()
        self._create_managers()
        self._setup_game_camera()
        self._register_player_gui()
        self._load_initial_scene()
        self._enter_play_mode()

    def _ensure_project_requirements(self):
        try:
            from Infernux.engine.project_requirements import ensure_project_requirements

            ensure_project_requirements(self.project_path, auto_install=False)
        except ImportError:
            # Optional packaging helper missing — runtime continues without
            # auto-install; happens in slim distribution variants.
            pass

    def _init_engine(self):
        self.engine = Engine(self.engine_log_level)
        self.engine._set_application_role("player")

        # For windowed mode, use the requested size;
        # for fullscreen borderless, start at a default size — the
        # caller will switch to fullscreen after bootstrap.
        if self.display_mode == "windowed":
            w, h = self.window_width, self.window_height
        else:
            w, h = 1920, 1080

        self.engine.init_renderer(
            width=w, height=h, project_path=self.project_path
        )

        # Explicitly tell C++ we are in player mode — no scene view rendering.
        # The C++ default is m_sceneViewVisible=true; without this call the
        # renderer would execute both scene and game render graphs every frame.
        self.engine.set_scene_view_visible(False)

        # Skip DockSpace / DockBuilder overhead — the player only registers a
        # single full-screen renderable, so the editor docking system is waste.
        self.engine.set_gui_player_mode(True)

        self.engine.set_gui_font(_resources.engine_font_path, 16)



    def _create_managers(self):
        self.scene_file_manager = SceneFileManager()
        self.scene_file_manager.set_asset_database(self.engine.get_asset_database())
        self.scene_file_manager.set_engine(self.engine.get_native_engine())

        # PlayModeManager is already created inside Engine.init_renderer()
        pm = self.engine.get_play_mode_manager()
        if pm:
            pm.set_asset_database(self.engine.get_asset_database())


    def _setup_game_camera(self):
        self.engine.set_game_camera_enabled(True)


    def _register_player_gui(self):
        self._player_gui = PlayerGUI(
            self.engine,
            splash_items=self.splash_items,
            data_root=self.project_path,
        )
        self.engine.register_gui("player_gui", self._player_gui)

    def _load_initial_scene(self):
        import json as _json
        bs_path = os.path.join(
            self.project_path, "ProjectSettings", "BuildSettings.json"
        )
        data = {}
        if os.path.isfile(bs_path):
            try:
                with open(bs_path, "r", encoding="utf-8", errors="replace") as _f:
                    data = _json.load(_f)
            except Exception as exc:
                Debug.log_suppressed("player_bootstrap.load_build_manifest", exc)
        scenes = data.get("scenes", [])
        if not scenes:
            Debug.log_warning("No scenes in BuildSettings.json — starting with empty scene")
            return

        first_scene = scenes[0]
        requested_scene = os.environ.get("_INFERNUX_PLAYER_START_SCENE", "").strip()
        if requested_scene:
            candidate = resolved_path(
                requested_scene if os.path.isabs(requested_scene) else os.path.join(self.project_path, requested_scene)
            )
            try:
                is_inside_project = is_path_within(candidate, self.project_path)
            except ValueError:
                is_inside_project = False
            if is_inside_project and os.path.splitext(candidate)[1].lower() == ".scene" and os.path.isfile(candidate):
                first_scene = candidate
                Debug.log_internal(f"Loaded Supervisor validation scene: {os.path.basename(first_scene)}")
            else:
                Debug.log_warning("Ignored invalid Supervisor Player start-scene override")
        # Resolve relative paths against project root (packaged builds
        # store scene paths relative to the game folder)
        if not os.path.isabs(first_scene):
            first_scene = os.path.join(self.project_path, first_scene)

        if not os.path.isfile(first_scene):
            raise RuntimeError(f"First scene file not found: {first_scene}")

        if self.scene_file_manager:
            if not self.scene_file_manager._do_open_scene(first_scene):
                raise RuntimeError(f"Failed to load initial scene: {first_scene}")
            from Infernux.lib import SceneManager as _NativeSM

            scene = _NativeSM.instance().get_active_scene()
            if scene is None:
                raise RuntimeError("Initial scene load completed without an active scene")
            Debug.log_internal(
                f"Loaded initial scene: {os.path.basename(first_scene)} "
                f"(objects={len(scene.get_all_objects())}, camera={scene.main_camera is not None})"
            )

    def _enter_play_mode(self):
        """Activate the initial scene on the first safe main-loop frame."""
        from Infernux.engine.deferred_task import DeferredTaskRunner

        runner = DeferredTaskRunner.instance()
        queued = runner.submit(
            "Player Startup",
            [("Starting game...", 1.0, self._activate_initial_scene_for_play)],
        )
        if not queued:
            raise RuntimeError("Cannot start Player while another deferred task is active")

    def _activate_initial_scene_for_play(self):
        """Start the freshly loaded scene without rebuilding it a second time."""
        from Infernux.lib import SceneManager as _NativeSM
        from Infernux.renderstack.render_stack import RenderStack
        from Infernux.timing import Time
        from Infernux.engine.play_mode import PlayModeState

        pm = self.engine.get_play_mode_manager()
        if not pm:
            Debug.log_error("No PlayModeManager available")
            return

        sm = _NativeSM.instance()
        scene = sm.get_active_scene()
        if not scene:
            Debug.log_warning("No active scene — play mode skipped")
            return

        # Capture a typed document (player never restores it, but PM needs it).
        snapshot = scene.serialize_document()
        if not snapshot:
            Debug.log_error("Scene serialization failed - play mode skipped")
            return False
        pm._scene_backup = snapshot

        Time._reset()
        old_state = pm._state
        pm._state = PlayModeState.PLAYING
        pm._last_frame_time = __import__("time").time()
        pm._notify_state_change(old_state, PlayModeState.PLAYING)

        RenderStack._active_instance = None
        scene.set_playing(True)
        try:
            from Infernux.components.builtin.sprite_renderer import SpriteRenderer

            SpriteRenderer.init_all_in_scene(scene)
        except Exception as exc:
            Debug.log_internal(f"Player SpriteRenderer init: {exc}")

        # This is the same lifecycle boundary used after a runtime scene load.
        # The initial load already published fresh Python components, so a
        # second Scene transaction here only creates competing object graphs.
        sm.play()
        Debug.log_internal("Player: Play mode activated")
        return True
