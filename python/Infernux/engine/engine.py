import gc
import os
import time
import weakref

from Infernux.lib import Infernux, InxGUIRenderable, LogLevel, RuntimeMode, lib_dir
from Infernux.engine.play_mode import PlayModeManager
from Infernux.engine.project_context import set_project_root
from Infernux.engine.path_utils import safe_path as _safe_path
from Infernux.debug import Debug

_PLAYER_MODE = os.environ.get("_INFERNUX_PLAYER_MODE")


class Engine():
    def __init__(self, engine_log_level=LogLevel.Info, mode=RuntimeMode.Graphical):
        self._mode = RuntimeMode(mode)
        self._application_role = (
            "headless" if self._mode == RuntimeMode.Headless
            else "player" if _PLAYER_MODE
            else "editor"
        )
        self._engine = Infernux(_safe_path(lib_dir), self._mode)
        self.set_log_level(engine_log_level)
        self._gui_objects = {}
        self._play_mode_manager = None
        self._render_pipeline = None  # prevents GC of pybind11 trampoline
        self._last_frame_time = time.time()
        self._gizmos_collector = None  # lazy-init GizmosCollector
        self._scene_view_visible = self._mode == RuntimeMode.Graphical and not _PLAYER_MODE
        self._next_reload_poll_time = 0.0
        self._next_gizmo_collect_time = 0.0
        self._reload_poll_interval = 0.1   # 10 Hz is enough for watcher events
        self._gizmo_collect_interval_play = 0.0
        self._gizmo_collect_interval_edit = 1.0 / 60.0
        self._gizmos_uploaded = False
        self._resources_manager = None  # Set in init_renderer (editor only)
        self._before_exit_callback = None
        self._editor_frame_sync_callback = None
        from Infernux.application import Application
        Application._bind_engine(self, self._application_role)

    def _set_application_role(self, runtime_kind: str):
        self._application_role = str(runtime_kind).strip().lower()
        from Infernux.application import Application
        Application._bind_engine(self, self._application_role)

    @staticmethod
    def _parse_present_mode(value) -> int | None:
        if value is None:
            return None
        if isinstance(value, int):
            return value if 0 <= value <= 3 else None

        text = str(value).strip().lower()
        if not text:
            return None

        if text.isdigit():
            mode = int(text)
            return mode if 0 <= mode <= 3 else None

        aliases = {
            "immediate": 0,
            "mailbox": 1,
            "fifo": 2,
            "fifo_relaxed": 3,
            "vsync_off": 0,
            "off": 0,
            "vsync_on": 2,
            "on": 2,
        }
        return aliases.get(text)

    def _apply_startup_present_mode(self):
        raw = (os.environ.get("INFERNUX_PRESENT_MODE")
               or os.environ.get("_INFERNUX_PRESENT_MODE"))
        mode = self._parse_present_mode(raw)
        if mode is None:
            return
        try:
            self.set_present_mode(mode)
            Debug.log_internal(f"Startup present mode override applied: {mode}")
        except Exception as exc:
            Debug.log_warning(f"Failed to apply startup present mode override '{raw}': {exc}")

    def init_renderer(self, width, height, project_path):
        if self._mode != RuntimeMode.Graphical:
            raise RuntimeError("init_renderer is unavailable in headless mode")
        self._apply_project_settings(project_path)
        from Infernux.resources import resources_path
        self._engine.init_renderer(
            width, height,
            _safe_path(project_path),
            _safe_path(resources_path),
        )
        self._apply_startup_present_mode()
        set_project_root(project_path)

        # Synchronize C++ scene-view flag with Python's initial state.
        # C++ defaults to false; editor sets True, player keeps False.
        self._engine.set_scene_view_visible(bool(self._scene_view_visible))

        if not _PLAYER_MODE:
            from Infernux.engine.resources_manager import ResourcesManager
            self._resources_manager = ResourcesManager(
                project_path=project_path, engine=self._engine
            )
        
        # Load project materials (default material from project's .mat file)
        self._load_project_materials(project_path)
        
        # Initialize AssetManager singleton (GUID ↔ path resolution for refs)
        from Infernux.core.assets import AssetManager
        AssetManager.initialize(self)
        Debug.log_internal("AssetManager initialized")

        # Initialize PlayModeManager (SceneManager will be set later via binding)
        self._play_mode_manager = PlayModeManager()
        self._play_mode_manager.set_asset_database(self.get_asset_database())
        self._play_mode_manager._native_engine = self._engine
        self._install_pre_scene_time_callback()
        Debug.log_internal("PlayModeManager initialized")

        # Auto-activate Python SRP rendering path
        # All rendering passes (opaque, skybox, transparent) are driven by Python
        from Infernux.renderstack import RenderStackPipeline
        self.set_render_pipeline(RenderStackPipeline())
        Debug.log_internal("RenderStackPipeline activated (Python SRP path)")

    def init_headless(self, project_path):
        if self._mode != RuntimeMode.Headless:
            raise RuntimeError("init_headless requires RuntimeMode.Headless")

        self._apply_project_settings(project_path)
        from Infernux.resources import resources_path
        self._engine.init_headless(
            _safe_path(project_path),
            _safe_path(resources_path),
        )
        set_project_root(project_path)

        from Infernux.core.assets import AssetManager
        AssetManager.initialize(self)

        self._play_mode_manager = PlayModeManager()
        self._play_mode_manager.set_asset_database(self.get_asset_database())
        self._play_mode_manager._native_engine = self._engine
        self._install_pre_scene_time_callback()
        Debug.log_internal("Headless engine initialized")

    def _install_pre_scene_time_callback(self):
        """Advance Python timing before native gameplay callbacks run."""
        engine_ref = weakref.ref(self)

        def _pre_scene_tick(delta_time):
            engine = engine_ref()
            if engine is not None:
                engine.tick_play_mode(float(delta_time))

        self._engine.set_pre_scene_update_callback(_pre_scene_tick)

    @staticmethod
    def _apply_project_settings(project_path):
        from Infernux.physics import settings as physics_settings

        settings = physics_settings.load(project_path)
        physics_settings.apply(settings)

        tag_layer_path = os.path.join(project_path, "ProjectSettings", "TagLayerSettings.json")
        if os.path.isfile(tag_layer_path):
            from Infernux.lib import TagLayerManager

            if not TagLayerManager.instance().load_from_file(_safe_path(tag_layer_path)):
                raise RuntimeError(f"Invalid tag/layer settings document: {tag_layer_path}")
    
    def _load_project_materials(self, project_path):
        """Load all .mat files from the project via AssetRegistry.

        Scans the project's ``materials/`` directory for ``.mat`` files.
        The first file named ``default_lit.mat`` is promoted to the
        engine-wide default material; every other ``.mat`` file is
        loaded via AssetRegistry so that scene deserialization can find it.
        """
        from Infernux.lib import AssetRegistry

        registry = AssetRegistry.instance()

        # Collect candidate directories
        search_dirs = []
        materials_dir = os.path.join(project_path, "materials")
        if os.path.isdir(materials_dir):
            search_dirs.append(materials_dir)

        # Also scan Library/Resources/materials for built-in materials
        library_mat_dir = os.path.join(project_path, "Library", "Resources", "materials")
        if os.path.isdir(library_mat_dir):
            search_dirs.append(library_mat_dir)

        default_loaded = False
        extra_count = 0

        for mat_dir in search_dirs:
            for fname in os.listdir(mat_dir):
                if not fname.endswith(".mat"):
                    continue
                mat_path = os.path.join(mat_dir, fname)

                # Load the default material via AssetRegistry (replaces builtin DefaultLit)
                if fname == "default_lit.mat" and not default_loaded:
                    if registry.load_builtin_material_from_file("DefaultLit", mat_path):
                        Debug.log_internal(f"Loaded default material from: {mat_path}")
                        default_loaded = True
                    else:
                        Debug.log_warning(f"Failed to load default material from: {mat_path}")
                else:
                    # Load via AssetRegistry (unified cache)
                    native = registry.load_material(mat_path)
                    if native:
                        extra_count += 1

        if not default_loaded:
            Debug.log_internal("No project default material found, using engine default")
        if extra_count:
            Debug.log_internal(f"Loaded {extra_count} additional project material(s)")

    def run(self):
        if self._mode == RuntimeMode.Headless:
            self._engine.run()
            self.exit()
            return

        if self._resources_manager:
            self._resources_manager.start()

        # Install a pre-GUI callback so that DeferredTaskRunner steps
        # (which may mutate the scene via deserialize) execute BEFORE
        # any ImGui panel renders.  This prevents panels from accessing
        # destroyed pybind11 objects during play-mode Stop.
        def _pre_gui_tick():
            from Infernux.engine.deferred_task import DeferredTaskRunner
            try:
                DeferredTaskRunner.instance().tick()
            except Exception as exc:
                Debug.log_suppressed("Engine.pre_gui_tick.DeferredTaskRunner", exc)
            if not _PLAYER_MODE:
                try:
                    from Infernux.mcp.threading import MainThreadCommandQueue
                    MainThreadCommandQueue.instance().drain()
                except Exception as exc:
                    Debug.log_suppressed("Engine.pre_gui_tick.MainThreadCommandQueue", exc)
            try:
                from Infernux.engine.ui.window_manager import WindowManager
                manager = WindowManager.instance()
                if manager is not None:
                    manager.process_pending_actions()
            except Exception as exc:
                Debug.log_suppressed("Engine.pre_gui_tick.WindowManager", exc)
        self._engine.set_pre_gui_callback(_pre_gui_tick)

        # Install a post-draw callback that runs AFTER GPU submit + present.
        # poll_deferred_load (heavy scene loading) is moved here so it executes
        # between frames, sandwiched by SDL_PumpEvents() in C++.  This prevents
        # Windows from flagging the application as "Not Responding" during long
        # scene loads that previously ran inside BuildFrame().
        #
        # Manual GC collection also runs here at controlled intervals.
        # Automatic GC is disabled below to prevent unpredictable ~5ms
        # pauses inside the hot UI/render path.  With 1000+ scene objects,
        # CPython's default gen0 threshold (700) triggers collections on
        # nearly every frame inside random timing windows.
        _gc_frame = [0]  # mutable counter for closure
        def _post_draw_tick():
            try:
                from Infernux.core.assets import AssetManager
                AssetManager.flush_pending_gpu_texture_reloads()
                AssetManager.poll_pending_asset_writes()
            except Exception as exc:
                Debug.log_suppressed("Engine.post_draw_tick.flush_texture_reloads", exc)

            from Infernux.engine.scene_manager import SceneFileManager
            sfm = SceneFileManager.instance()
            if sfm is not None:
                try:
                    sfm.poll_pending_save()
                except Exception as exc:
                    Debug.log_suppressed("Engine.post_draw_tick.poll_pending_save", exc)
                try:
                    sfm.poll_deferred_load()
                except Exception as exc:
                    Debug.log_suppressed("Engine.post_draw_tick.poll_deferred_load", exc)
            # Periodic manual GC: gen0 every 120 frames (~1s at 120fps),
            # gen1 every 600 frames (~5s), full every 3000 frames (~25s).
            _gc_frame[0] += 1
            _f = _gc_frame[0]
            if _f % 3000 == 0:
                gc.collect(2)
            elif _f % 600 == 0:
                gc.collect(1)
            elif _f % 120 == 0:
                gc.collect(0)
        self._engine.set_post_draw_callback(_post_draw_tick)

        # Disable automatic GC to eliminate unpredictable pauses during
        # rendering.  Manual collection runs in _post_draw_tick above.
        gc.disable()
        Debug.log_internal("Engine started (automatic GC disabled, manual collection active)")
        self._engine.run()
        gc.enable()  # Restore automatic GC for shutdown cleanup
        # C++ Run() returned (main loop ended, but Cleanup not yet called).
        # Optimised shutdown order:
        #  1. Signal ResourcesManager to stop (non-blocking).
        #  2. Run C++ Cleanup (Vulkan teardown — the heavy part).
        #  3. Join the ResourcesManager thread (should already have exited
        #     during step 2, so the join returns instantly).
        self.exit()

    def tick(self, delta_time: float):
        if self._mode != RuntimeMode.Headless:
            raise RuntimeError("tick is only available in headless mode")

        from Infernux.engine.deferred_task import DeferredTaskRunner
        DeferredTaskRunner.instance().tick()

        from Infernux.mcp.threading import MainThreadCommandQueue
        MainThreadCommandQueue.instance().drain()

        self._engine.tick(float(delta_time))

    def request_exit(self):
        """Request a safe close, preserving graphical Editor confirmations."""
        if self._mode == RuntimeMode.Graphical and self._application_role == "editor":
            try:
                from Infernux.engine.scene_manager import SceneFileManager

                manager = SceneFileManager.instance()
                if manager is not None:
                    manager.request_close()
                    return
            except Exception as exc:
                Debug.log_suppressed("Engine.request_exit.scene_manager", exc)
        if self._engine:
            self._engine.exit()
    
    def tick_play_mode(self, external_delta_time: float | None = None):
        """
        Called each frame to update play mode timing only.
        Lifecycle updates are driven by C++.
        """
        current_time = time.time()
        delta_time = (
            current_time - self._last_frame_time
            if external_delta_time is None
            else float(external_delta_time)
        )
        self._last_frame_time = current_time

        if self._editor_frame_sync_callback is not None:
            self._editor_frame_sync_callback()
        
        # Process pending script reloads on the main thread, but throttle polling.
        rm = self._resources_manager
        if rm and current_time >= self._next_reload_poll_time:
            try:
                rm.process_pending_reloads()
            except Exception as exc:
                from Infernux.debug import Debug
                Debug.log_error(f"Script reload error: {exc}")
            self._next_reload_poll_time = current_time + self._reload_poll_interval
        
        pmm = self._play_mode_manager
        is_playing = pmm is not None and pmm.is_playing

        # The editor owns play transitions through PlayModeManager. Headless
        # callers may drive the native SceneManager directly, so keep Time
        # valid for that composition as well.
        if is_playing:
            pmm.tick(delta_time)
        else:
            from Infernux.lib import SceneManager
            native_scene_manager = SceneManager.instance()
            if native_scene_manager.is_playing() and not native_scene_manager.is_paused():
                from Infernux.timing import Time
                Time._tick(delta_time)

        # Flush throttled material saves — skip during play mode
        if not is_playing:
            self._flush_pending_material_saves()

        # Collect/upload gizmos only when Scene View is visible.
        if self._scene_view_visible:
            interval = self._gizmo_collect_interval_play if is_playing else self._gizmo_collect_interval_edit
            if interval <= 0.0 or current_time >= self._next_gizmo_collect_time:
                self._tick_gizmos()
                self._next_gizmo_collect_time = current_time + interval if interval > 0.0 else current_time
        else:
            self._clear_uploaded_gizmos()
        
        return delta_time

    def _tick_gizmos(self):
        """Collect component gizmos and upload to C++ each frame."""
        if self._gizmos_collector is None:
            from Infernux.gizmos.collector import GizmosCollector
            self._gizmos_collector = GizmosCollector()
        self._gizmos_collector.collect_and_upload(self)
        self._gizmos_uploaded = True

    @staticmethod
    def _flush_pending_material_saves():
        """Flush throttled mutable rendering-asset saves."""
        try:
            from Infernux.core.material import Material
            Material.flush_all_pending()
        except Exception as exc:
            Debug.log_suppressed("Engine._flush_pending_material_saves", exc)
        try:
            from Infernux.renderstack.render_effect import RenderEffect
            RenderEffect.flush_all_pending()
        except Exception as exc:
            Debug.log_suppressed("Engine._flush_pending_effect_saves", exc)

    def _clear_uploaded_gizmos(self):
        """Clear uploaded gizmo buffers once when Scene View is hidden."""
        if not self._gizmos_uploaded:
            return
        native = self.get_native_engine()
        if not native:
            self._gizmos_uploaded = False
            return
        native.clear_component_gizmos()
        native.clear_component_gizmo_icons()
        self._gizmos_uploaded = False

    def set_scene_view_visible(self, visible: bool):
        """Called by SceneView panel to gate expensive gizmo updates and skip scene rendering."""
        visible = bool(visible)
        if self._scene_view_visible == visible:
            return
        self._scene_view_visible = visible
        # Gate C++ scene graph execution
        if self._engine is not None:
            self._engine.set_scene_view_visible(visible)
        if visible:
            self._next_gizmo_collect_time = 0.0
        else:
            self._clear_uploaded_gizmos()
    
    def get_play_mode_manager(self) -> PlayModeManager:
        """Get the play mode manager for controlling play/pause/stop."""
        return self._play_mode_manager

    def set_before_exit_callback(self, callback):
        """Register a callback invoked right before native engine cleanup."""
        self._before_exit_callback = callback
    
    def exit(self):
        """
        Clean up and exit the engine completely.

        Shutdown order keeps all file-system commits ahead of native teardown:
          0. Force-stop play mode (destroy Python components cleanly)
          1. Stop ResourcesManager and drain coalesced events on the main thread
          2. C++ Cleanup (GPU drain + resource destruction)
        """
        # Dirty-panel decisions are completed by SceneFileManager's non-blocking
        # Editor modal before native close is confirmed. This call is now only a
        # teardown audit and must never open a platform dialog.
        if self._mode == RuntimeMode.Graphical and not self._confirm_dirty_panels_before_exit():
            return

        if callable(self._before_exit_callback):
            try:
                self._before_exit_callback()
            except Exception as exc:
                Debug.log_suppressed("Engine.exit.before_exit_callback", exc)

        # Safety net: if cleanup hangs (C++ deadlock, thread stuck), force-kill
        # the process after a generous timeout so we never leave zombie procs.
        import threading as _th
        shutdown_complete = _th.Event()

        def _force_exit():
            if not shutdown_complete.wait(15):
                os._exit(1)

        _th.Thread(target=_force_exit, daemon=True, name="ShutdownWatchdog").start()

        # 0. If still in play mode, tear down Python components before C++
        #    objects are destroyed.  Without this, the C++ renderer
        #    destruction can trigger PyComponentProxy::OnDestroy callbacks
        #    on already-invalid Python state, and physics/audio may block.
        self._shutdown_play_mode()

        try:
            from Infernux.core.assets import AssetManager
            AssetManager.flush_all_asset_writes()
        except Exception as exc:
            Debug.log_error(f"Failed to flush pending asset writes during shutdown: {exc}")

        # 1. Stop the observer and commit any events it already delivered while
        #    AssetDatabase, AssetRegistry, renderer, and editor caches are alive.
        if self._resources_manager:
            self._resources_manager.cleanup()

        # 2. C++ Cleanup — destroys renderer, Vulkan device, etc.
        if self._engine:
            self._engine.cleanup()
        
        # Clear all references
        self._gui_objects.clear()
        self._engine = None
        self._resources_manager = None
        from Infernux.application import Application
        Application._unbind_engine(self)
        shutdown_complete.set()

    def _shutdown_play_mode(self):
        """Immediately tear down play-mode components for a clean shutdown.

        Unlike ``exit_play_mode()`` (which uses deferred tasks to restore the
        saved scene across several frames), this performs only the minimal
        cleanup needed so that the subsequent C++ ``Cleanup()`` does not
        encounter live Python component state.
        """
        from Infernux.engine.play_mode import PlayModeState

        pmm = self._play_mode_manager
        if not pmm or pmm.state == PlayModeState.EDIT:
            return

        Debug.log_internal("Shutting down Play Mode for engine exit…")

        # 1. Stop the C++ simulation loop (no more Update/FixedUpdate calls)
        try:
            from Infernux.lib import SceneManager as _NativeSceneMgr
            sm = _NativeSceneMgr.instance()
            if sm:
                sm.stop()
        except Exception as exc:
            Debug.log_suppressed("Engine._shutdown_play_mode.SceneManager.stop", exc)

        # 2. Destroy all live Python components (on_destroy + GC helpers)
        try:
            from Infernux.components.component import InxComponent
            for comp_list in list(InxComponent._active_instances.values()):
                for comp in list(comp_list):
                    try:
                        comp._call_on_destroy()
                    except Exception as exc:
                        Debug.log_suppressed(
                            f"Engine._shutdown_play_mode.on_destroy[{type(comp).__name__}]",
                            exc,
                        )
            InxComponent._clear_all_instances()
        except Exception as exc:
            Debug.log_suppressed("Engine._shutdown_play_mode.clear_all_instances", exc)

        # 3. Flip state to EDIT so nothing else treats us as playing
        pmm._state = PlayModeState.EDIT

    def _confirm_dirty_panels_before_exit(self) -> bool:
        """Audit dirty state after native close confirmation without prompting."""
        try:
            from Infernux.engine.project_context import get_dirty_panel_entries

            entries = list(get_dirty_panel_entries())
            if entries:
                titles = ", ".join(str(entry.get("title") or entry.get("panel_id") or "Panel") for entry in entries)
                Debug.log_warning(
                    "Engine teardown reached with dirty panel registry entries after "
                    f"close confirmation: {titles}"
                )
            return True
        except Exception as exc:
            Debug.log_suppressed("Engine._confirm_dirty_panels_before_exit", exc)
            return True

    def set_gui_font(self, font_path, font_size=16):
        self._engine.set_gui_font(_safe_path(font_path), font_size)

    def set_gui_player_mode(self, enabled: bool):
        """Skip DockSpace/layout overhead in standalone player builds."""
        self._engine.set_gui_player_mode(bool(enabled))

    def get_display_scale(self) -> float:
        """Return the OS display scale factor (e.g. 2.0 for 200% Windows scaling)."""
        return self._engine.get_display_scale()

    def set_log_level(self, engine_log_level):
        self._engine.set_log_level(engine_log_level)

    def register_gui(
        self, name: str, gui_object: InxGUIRenderable, *, priority: int = 0
    ):
        self._engine.register_gui_renderable(name, gui_object, int(priority))
        self._gui_objects[name] = gui_object

    def unregister_gui(self, name: str):
        self._engine.unregister_gui_renderable(name)
        self._gui_objects.pop(name, None)

    def select_docked_window(self, window_id: str):
        self._engine.select_docked_window(window_id)

    def reset_imgui_layout(self):
        """Clear ImGui docking layout (in-memory + on disk)."""
        self._engine.reset_imgui_layout()
    
    def show(self):
        self._engine.show()

    def hide(self):
        self._engine.hide()

    def is_window_minimized(self) -> bool:
        """Return whether the editor window is minimized or occluded."""
        return bool(self._engine and self._engine.is_window_minimized())

    def set_window_icon(self, icon_path):
        """Set the editor window icon from a PNG file."""
        self._engine.set_window_icon(_safe_path(icon_path))

    def set_fullscreen(self, fullscreen: bool):
        """Set the window to fullscreen or windowed mode."""
        if self._engine:
            self._engine.set_fullscreen(fullscreen)

    def set_window_title(self, title: str):
        """Set the window title bar text."""
        if self._engine:
            self._engine.set_window_title(title)

    def set_maximized(self, maximized: bool):
        """Maximize or restore the window."""
        if self._engine:
            self._engine.set_maximized(maximized)

    def set_resizable(self, resizable: bool):
        """Enable or disable window resizing."""
        if self._engine:
            self._engine.set_resizable(resizable)

    def set_present_mode(self, mode: int):
        """Set swapchain present mode: 0=IMMEDIATE, 1=MAILBOX, 2=FIFO, 3=FIFO_RELAXED."""
        if self._engine:
            self._engine.set_present_mode(int(mode))

    def get_present_mode(self) -> int:
        """Get current swapchain present mode: 0=IMMEDIATE, 1=MAILBOX, 2=FIFO, 3=FIFO_RELAXED."""
        if self._engine:
            return int(self._engine.get_present_mode())
        return 1
    
    def get_native_engine(self):
        """Get the underlying native Infernux instance for direct API access."""
        return self._engine
    
    def get_resource_preview_manager(self):
        """Get the resource preview manager for file previews in Inspector."""
        if self._engine:
            return self._engine.get_resource_preview_manager()
        return None

    def get_asset_database(self):
        """Get the asset database instance for project asset operations."""
        if self._engine:
            return self._engine.get_asset_database()
        return None

    # ========================================================================
    # Editor Camera — property-based access (EditorCamera object)
    # ========================================================================

    @property
    def editor_camera(self):
        """Get the editor camera controller (EditorCamera object with
        properties: position, rotation, fov, near_clip, far_clip,
        focus_point, focus_distance; methods: reset(), focus_on(),
        restore_state(), world_to_screen_point())."""
        if self._engine:
            return self._engine.editor_camera
        return None

    def process_scene_view_input(self, delta_time: float, right_mouse_down: bool, middle_mouse_down: bool,
                                  mouse_delta_x: float, mouse_delta_y: float, scroll_delta: float,
                                  key_w: bool, key_a: bool, key_s: bool, key_d: bool,
                                  key_q: bool, key_e: bool, key_shift: bool):
        """Process scene view input for editor camera control."""
        if self._engine:
            self._engine.process_scene_view_input(
                delta_time, right_mouse_down, middle_mouse_down,
                mouse_delta_x, mouse_delta_y, scroll_delta,
                key_w, key_a, key_s, key_d, key_q, key_e, key_shift
            )

    # ========================================================================
    # Scene Render Target API - for offscreen scene rendering
    # ========================================================================

    def get_scene_texture_id(self) -> int:
        """Get scene render target texture ID for ImGui display."""
        if self._engine:
            return self._engine.get_scene_texture_id()
        return 0

    def resize_scene_render_target(self, width: int, height: int):
        """Resize the scene render target to match viewport size."""
        if self._engine:
            self._engine.resize_scene_render_target(width, height)

    # ========================================================================
    # Game Render Target API - for game camera rendering
    # ========================================================================

    def get_game_texture_id(self) -> int:
        """Get game render target texture ID for ImGui display."""
        if self._engine:
            return self._engine.get_game_texture_id()
        return 0

    def resize_game_render_target(self, width: int, height: int):
        """Resize the game render target to match game viewport size."""
        if self._engine:
            self._engine.resize_game_render_target(width, height)

    def request_render_target_readback(self, game_view: bool = True):
        """Return a non-blocking ticket for the latest submitted render target."""
        return self._engine.request_render_target_readback(game_view)

    def request_capture(self, source: str, output_path: str) -> int:
        """Capture a Scene or Game render target to an engine-encoded PNG."""
        return int(self._engine.request_capture(source, output_path))

    def query_capture(self, capture_id: int) -> dict:
        """Return status and renderer metadata for an engine capture."""
        return dict(self._engine.query_capture(capture_id))

    def cancel_capture(self, capture_id: int) -> bool:
        """Cancel an unfinished engine capture."""
        return bool(self._engine.cancel_capture(capture_id))

    def set_game_camera_enabled(self, enabled: bool):
        """Enable or disable game camera rendering."""
        if self._engine:
            self._engine.set_game_camera_enabled(enabled)

    def get_last_game_render_ms(self) -> float:
        """Get last frame's game view render time (CPU command recording) in ms.

        Measures ONLY the game camera render pipeline, excluding editor panels,
        scene view, etc.  Use this for a game-only FPS counter.
        """
        if self._engine:
            return self._engine.get_last_game_render_ms()
        return 0.0

    def get_screen_ui_renderer(self):
        """Get the GPU screen-space UI renderer (None before game RT init)."""
        if self._engine:
            return self._engine.get_screen_ui_renderer()
        return None

    # ========================================================================
    # Editor Tools API — highlight + ray for Python-side gizmo interaction
    # ========================================================================

    def pick_gizmo_axis(self, screen_x: float, screen_y: float,
                        viewport_width: float, viewport_height: float) -> int:
        """Lightweight gizmo axis proximity test for hover highlighting."""
        if self._engine:
            return self._engine.pick_gizmo_axis(screen_x, screen_y, viewport_width, viewport_height)
        return 0

    def set_editor_tool_highlight(self, axis: int):
        """Set the highlighted (hovered) gizmo axis. 0=None, 1=X, 2=Y, 3=Z."""
        if self._engine:
            self._engine.set_editor_tool_highlight(axis)

    def set_editor_tool_mode(self, mode: int):
        """Set the active editor tool mode. 0=None, 1=Translate, 2=Rotate, 3=Scale."""
        if self._engine:
            self._engine.set_editor_tool_mode(mode)

    def get_editor_tool_mode(self) -> int:
        """Get the active editor tool mode. 0=None, 1=Translate, 2=Rotate, 3=Scale."""
        if self._engine:
            return self._engine.get_editor_tool_mode()
        return 0

    def set_editor_tool_local_mode(self, local: bool):
        """Enable/disable local coordinate mode for editor tool gizmos."""
        if self._engine:
            self._engine.set_editor_tool_local_mode(local)

    def screen_to_world_ray(self, screen_x: float, screen_y: float,
                            viewport_width: float, viewport_height: float):
        """Build a world-space ray from screen coordinates.

        Returns (origin_x, origin_y, origin_z, dir_x, dir_y, dir_z).
        """
        if self._engine:
            return self._engine.screen_to_world_ray(screen_x, screen_y,
                                                     viewport_width, viewport_height)
        return (0.0, 0.0, 0.0, 0.0, 0.0, 1.0)

    # ========================================================================
    # Editor Gizmos API - for toggling visual aids in scene view
    # ========================================================================

    def get_selected_object_id(self) -> int:
        """Get the currently selected object ID (0 if none)."""
        if self._engine:
            return self._engine.get_selected_object_id()
        return 0

    def set_show_grid(self, show: bool):
        """Set visibility of ground grid."""
        if self._engine:
            self._engine.set_show_grid(show)

    def is_show_grid(self) -> bool:
        """Get visibility of ground grid."""
        if self._engine:
            return self._engine.is_show_grid()
        return False

    def pick_scene_object_ids(self, screen_x: float, screen_y: float, viewport_width: float, viewport_height: float):
        """Pick ordered scene object candidate IDs at screen coordinates (for editor cycling selection)."""
        if self._engine is None:
            return []
        return list(self._engine.pick_scene_object_ids(screen_x, screen_y, viewport_width, viewport_height))

    def request_scene_object_pick(self, screen_x: float, screen_y: float, viewport_width: float, viewport_height: float) -> int:
        if self._engine is None:
            return 0
        return int(self._engine.request_scene_object_pick(screen_x, screen_y, viewport_width, viewport_height))

    def query_scene_object_pick(self, request_id: int) -> dict:
        if self._engine is None:
            return {"request_id": int(request_id), "status": "unknown", "object_id": 0,
                    "error": "Engine is unavailable"}
        return dict(self._engine.query_scene_object_pick(int(request_id)))

    # ========================================================================
    # Render Pipeline API (SRP)
    # ========================================================================

    def set_render_pipeline(self, asset_or_pipeline=None):
        """
        Set a custom render pipeline.

        Args:
            asset_or_pipeline: A RenderPipelineAsset (calls create_pipeline()),
                               a RenderPipeline instance, or None to revert to
                               the default C++ rendering path.
        """
        if self._engine is None:
            return

        if asset_or_pipeline is None:
            self._render_pipeline = None
            self._engine.set_render_pipeline(None)
            return

        # If it's an asset, create the pipeline from it
        if hasattr(asset_or_pipeline, "create_pipeline"):
            pipeline = asset_or_pipeline.create_pipeline()
        else:
            pipeline = asset_or_pipeline

        # MUST keep a Python-side reference! Without this, the Python wrapper
        # gets GC'd (ref count → 0), pybind11 removes the C++ → Python mapping
        # from registered_instances, and get_override() can't find the Python
        # object → "pure virtual function" error.
        self._render_pipeline = pipeline
        self._engine.set_render_pipeline(pipeline)
