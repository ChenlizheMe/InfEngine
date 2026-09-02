"""
EditorBootstrap — structured editor initialization.

Breaks the monolithic ``release_engine()`` startup path into explicit
startup steps. Each step is a separate method, closures become instance
methods, and panel/manager references live on the bootstrap instance.
"""

from __future__ import annotations

import logging
import os
import pathlib
import threading
import time
from typing import Optional

import Infernux.resources as _resources
from Infernux.debug import Debug
from Infernux.engine.engine import Engine, LogLevel
from Infernux.engine.resources_manager import ResourcesManager
from Infernux.engine.play_mode import PlayModeManager, PlayModeState
from Infernux.engine.scene_manager import SceneFileManager
from Infernux.engine.path_utils import resolved_path
from Infernux.engine.ui import (
    SceneViewPanel,
    GameViewPanel,
    WindowManager,
    TagLayerSettingsPanel,
    BuildSettingsPanel,
    UIEditorPanel,
    EditorPanel,
    EditorServices,
    PanelRegistry,
    editor_panel,
)
from Infernux.engine.ui import panel_state as _panel_state

_log = logging.getLogger("Infernux.bootstrap")

_TOTAL_STEPS = 13


def _iter_project_material_paths(project_path: str):
    """Yield user material assets without walking project runtimes or caches."""
    assets_root = os.path.join(resolved_path(project_path), "Assets")
    if not os.path.isdir(assets_root):
        return

    for dirpath, dirnames, filenames in os.walk(assets_root):
        dirnames[:] = [name for name in dirnames if not name.startswith(".")]
        for name in filenames:
            if name.lower().endswith(".mat"):
                yield os.path.join(dirpath, name)


def _signal_progress(current_step: int, total: int, message: str) -> None:
    """Write bootstrap progress to the launcher splash via the ready-file."""
    ready_file = os.environ.get("_INFERNUX_READY_FILE", "").strip()
    if not ready_file:
        return
    try:
        with open(ready_file, "w", encoding="utf-8") as f:
            f.write(f"LOADING:{current_step}/{total}:{message}\n")
            f.flush()
            os.fsync(f.fileno())
    except OSError as exc:
        Debug.log_suppressed("EditorBootstrap.write_loading_progress", exc)

from ._bootstrap_panels import BootstrapPanelsMixin
from ._bootstrap_selection import BootstrapSelectionMixin
from ._bootstrap_wiring import BootstrapWiringMixin


class EditorBootstrap(BootstrapPanelsMixin, BootstrapSelectionMixin, BootstrapWiringMixin):
    """Orchestrates the full editor startup sequence."""

    _instance: Optional["EditorBootstrap"] = None

    def __init__(self, project_path: str, engine_log_level=LogLevel.Info):
        EditorBootstrap._instance = self
        self.project_path = project_path
        self.engine_log_level = engine_log_level

        # Managers
        self.engine: Optional[Engine] = None
        self.undo_manager = None
        self.scene_file_manager: Optional[SceneFileManager] = None
        self.window_manager: Optional[WindowManager] = None
        self.services: Optional[EditorServices] = None

        # Panels
        self.shortcut_input = None
        self.menu_bar = None
        self.toolbar = None
        self.hierarchy = None
        self.inspector_panel = None  # C++ InspectorPanel (native)
        self.project_panel = None
        self.console = None  # C++ ConsolePanel (native)
        self.status_bar = None
        self.scene_view: Optional[SceneViewPanel] = None
        self.game_view: Optional[GameViewPanel] = None
        self.ui_editor: Optional[UIEditorPanel] = None

        # Last authoritative typed selection published to the journal.
        self._prev_selection_snapshot = None

        # Progress tracking for launcher splash
        self._progress_step = 0
        self._progress_started_at = time.perf_counter()
        self._progress_phase_started_at = self._progress_started_at
        self._progress_phase_message = ""

    @classmethod
    def instance(cls) -> Optional["EditorBootstrap"]:
        return cls._instance

    # ── Public entry point ─────────────────────────────────────────────

    def run(self):
        """Execute all bootstrap steps and start the main loop."""
        self._report_progress("Checking project requirements\u2026")
        self._ensure_project_requirements()

        self._report_progress("Initializing renderer\u2026")
        self._init_engine()

        self._report_progress("Creating managers\u2026")
        self._create_managers()

        self._report_progress("Preloading project plugins\u2026")
        self._load_plugins()

        self._report_progress("Loading layout\u2026")
        self._setup_layout_persistence()

        self._report_progress("Registering window types\u2026")
        self._register_window_types()

        self._report_progress("Creating editor panels\u2026")
        self._create_panels()

        self._report_progress("Wiring selection system\u2026")
        self._wire_selection_system()

        self._report_progress("Setting up UI editor\u2026")
        self._wire_ui_editor()

        self._report_progress("Preparing scene system\u2026")
        self._setup_scene_change_cleanup()

        self._report_progress("Loading scene\u2026")
        self._load_initial_scene()

        self._report_progress("Compiling builtin shaders\u2026")
        self._prewarm_builtin_pipelines()

        self._report_progress("Refreshing project resources\u2026")
        if self.engine:
            self.engine.prepare_startup_refresh()
            # Lifecycle scripts created without a .meta sidecar only enter the
            # GUID catalog with this refresh commit; load them now instead of
            # on the next editor restart.
            if self.plugin_manager is not None:
                self.plugin_manager.catch_up_preloads()

        self._finish_progress()

        if self.engine:
            try:
                self.engine.set_game_camera_enabled(True)
            except Exception as _exc:
                pass
            try:
                ne = self.engine.get_native_engine()
                if ne:
                    ne.request_full_speed_frame()
            except Exception as _exc:
                pass

    def _report_progress(self, message: str):
        """Notify the launcher splash of the current bootstrap step."""
        now = time.perf_counter()
        if self._progress_phase_message:
            self._log_startup_profile(
                "Editor startup phase "
                f"'{self._progress_phase_message}' completed in "
                f"{(now - self._progress_phase_started_at) * 1000.0:.1f} ms"
            )
        self._progress_step += 1
        self._progress_phase_started_at = now
        self._progress_phase_message = message
        _signal_progress(self._progress_step, _TOTAL_STEPS, message)

    def _finish_progress(self) -> None:
        now = time.perf_counter()
        if self._progress_phase_message:
            self._log_startup_profile(
                "Editor startup phase "
                f"'{self._progress_phase_message}' completed in "
                f"{(now - self._progress_phase_started_at) * 1000.0:.1f} ms"
            )
        self._log_startup_profile(
            f"Editor startup completed in {(now - self._progress_started_at) * 1000.0:.1f} ms"
        )

    @staticmethod
    def _log_startup_profile(message: str) -> None:
        if os.environ.get("INFERNUX_PROFILE_STARTUP", "").strip() == "1":
            Debug.log(message)

    def _ensure_particle_artifacts(self) -> None:
        """Compile missing or stale particle Library products before the first frame."""
        from Infernux.particle.artifact import ParticleArtifactRegistry

        ParticleArtifactRegistry.ensure_project_compiled(
            self.project_path,
            raise_on_error=True,
        )

    def _ensure_project_requirements(self):
        from Infernux.engine.project_requirements import ensure_project_requirements

        ensure_project_requirements(self.project_path, auto_install=True)

    def _init_engine(self):
        self.engine = Engine(self.engine_log_level)
        self.engine.init_renderer(
            width=1600, height=900, project_path=self.project_path
        )
        native = self.engine.get_native_engine()
        if native is not None:
            timings = dict(native.startup_phase_timings_ms)
            self._log_startup_profile(
                "Native renderer startup phases: "
                + ", ".join(
                    f"{name}={float(milliseconds):.1f}ms"
                    for name, milliseconds in sorted(timings.items())
                )
            )
        # Match Unity's default UI text density more closely. Player bootstrap
        # uses the same base size; explicit project UIText sizes stay unchanged.
        self.engine.set_gui_font(_resources.engine_font_path, 18)

    def _prewarm_builtin_pipelines(self):
        """Compile builtin material shader programs during startup.

        Without this, the first mesh drawn with a builtin material (e.g. the
        first primitive created in a fresh session) pays the full glslang
        compilation of every pass variant (Forward / Forward+ / GBuffer /
        Shadow / Depth / Picking / Motion) synchronously on the render
        thread — a multi-second stall. Moving it here folds the cost into
        the splash screen, where the launcher already shows progress.
        """
        if not self.engine:
            return
        native = self.engine.get_native_engine()
        if native is None:
            return
        try:
            from Infernux.lib import AssetRegistry
        except Exception as exc:
            Debug.log_suppressed("EditorBootstrap.builtin_pipeline_prewarm.import", exc)
            return
        for name in ("DefaultLit", "SkyboxProcedural"):
            try:
                material = AssetRegistry.instance().get_builtin_material(name)
                if material is not None:
                    native.refresh_material_pipeline(material)
            except Exception as exc:
                Debug.log_suppressed(f"EditorBootstrap.builtin_pipeline_prewarm[{name}]", exc)

    def _prewarm_material_previews(self):
        """Prewarm material preview textures once at startup.

        Uses the same Python preview API path as inspector runtime so first click
        can hit the exact same cache key (size/tag) and avoid a second load.
        """
        if not self.engine:
            return

        native = self.engine.get_native_engine()
        if native is None:
            return

        material_paths = list(_iter_project_material_paths(self.project_path))

        if not material_paths:
            return

        # Route through the same preview cache API used by inspector.
        try:
            from Infernux.engine.ui.asset_resource_preview import get_resource_preview_texture_id
        except Exception as exc:
            Debug.log_suppressed("EditorBootstrap.material_preview_prewarm.import", exc)
            return

        class _BootstrapPreviewPanel:
            def __init__(self, native_engine):
                self._native_engine = native_engine

            def get_native_engine(self):
                return self._native_engine

        preview_panel = _BootstrapPreviewPanel(native)

        for mat_path in material_paths:
            try:
                # Keep cache_tag empty to match first inspector draw.
                get_resource_preview_texture_id(
                    preview_panel,
                    mat_path,
                    preview_size=256,
                    cache_tag="",
                    material_async=False,
                )
            except Exception as exc:
                Debug.log_suppressed(
                    f"EditorBootstrap.material_preview_prewarm[{os.path.basename(mat_path)}]",
                    exc,
                )
                continue

    def _create_managers(self):
        from Infernux.engine.interaction import EditorInteractionCore
        from Infernux.engine.undo import UndoManager

        self.interaction_core = EditorInteractionCore()
        from Infernux.particle.artifact import ParticleArtifactRegistry
        from Infernux.engine.ui import project_file_ops

        self.interaction_core.asset_mutations.add_observer(
            ParticleArtifactRegistry.on_asset_mutation
        )
        self.interaction_core.asset_mutations.add_observer(
            project_file_ops.on_asset_mutation
        )
        self.undo_manager = UndoManager(self.interaction_core.action_journal)

        self.scene_file_manager = SceneFileManager()
        self.scene_file_manager.set_asset_database(self.engine.get_asset_database())
        self.scene_file_manager.set_engine(self.engine.get_native_engine())

        self.window_manager = WindowManager(
            self.engine,
            self.interaction_core.panels,
            self.engine._register_editor_panel_gui,
        )
        self.interaction_core.asset_mutations.add_observer(
            self.window_manager.on_asset_mutation
        )

        self.services = EditorServices()
        self.services._engine = self.engine
        self.services._scene_file_manager = self.scene_file_manager
        self.services._play_mode_manager = self.engine._play_mode_manager
        self.services._window_manager = self.window_manager
        self.services._interaction_core = self.interaction_core
        self.services._asset_database = self.engine.get_asset_database()
        self.services._project_path = self.project_path

        # Pin editor UI icons early so Inspector/object fields never bind
        # descriptors from the unpinned texture-preview LRU cache.
        try:
            from Infernux.engine.ui.editor_icons import EditorIcons

            EditorIcons.preload(self.engine.get_native_engine())
        except Exception as exc:
            Debug.log_warning(f"EditorIcons preload skipped: {exc}")

    def _load_plugins(self):
        from Infernux.plugins import PluginManager

        self.plugin_manager = PluginManager.startup(
            self.project_path,
            engine=self.engine,
            runtime=False,
        )

    def _wire_toolbar_callbacks_on(self, tb, engine):
        """Shared helper: attach play/camera/grid callbacks to a ToolbarPanel."""
        pmm = engine._play_mode_manager if engine else None
        from Infernux.lib import PlayState
        from Infernux.engine.play_mode import PlayModeState
        def _get_play_state():
            if not pmm:
                return PlayState.Edit
            state = pmm.state
            if state == PlayModeState.PLAYING:
                return PlayState.Playing
            elif state == PlayModeState.PAUSED:
                return PlayState.Paused
            return PlayState.Edit
        def _get_play_time_str():
            if not pmm:
                return "00:00.000"
            t = pmm.total_play_time
            return f"{int(t//60):02d}:{t%60:06.3f}"

        from Infernux.engine.interaction import CommandSource

        command_registry = self.interaction_core.commands
        camera_view_id = self.interaction_core.panels.view_command_target(
            type_id="toolbar"
        )
        if not camera_view_id:
            raise RuntimeError(
                "toolbar interaction descriptor must declare its View Command target"
            )
        tb.execute_command = lambda command_id, source, _argument: command_registry.execute(
            command_id,
            source=CommandSource(source),
        ).accepted
        tb.can_execute_command = lambda command_id, _argument: (
            command_registry.can_execute(
                command_id,
                command_registry.context(CommandSource.TOOLBAR),
            )
        )
        tb.get_play_state = _get_play_state
        tb.get_play_time_str = _get_play_time_str

        native = engine.get_native_engine() if engine else None
        if native:
            tb.is_show_grid = lambda: native.is_show_grid()

        def _sync_camera():
            cam = engine.editor_camera if engine else None
            if not cam:
                return tb.get_camera_settings()
            return {
                "orthographic": bool(cam.orthographic),
                "fov": float(cam.fov),
                "orthographic_size": float(cam.orthographic_size),
                "rotation_speed": float(cam.rotation_speed),
                "pan_speed": float(cam.pan_speed),
                "zoom_speed": float(cam.zoom_speed),
                "move_speed": float(cam.move_speed),
                "move_speed_boost": float(cam.move_speed_boost),
            }
        def _camera_snapshot(settings):
            return {
                "orthographic": bool(settings["orthographic"]),
                "fov": float(settings["fov"]),
                "orthographic_size": float(settings["orthographic_size"]),
                "rotation_speed": float(settings["rotation_speed"]),
                "pan_speed": float(settings["pan_speed"]),
                "zoom_speed": float(settings["zoom_speed"]),
                "move_speed": float(settings["move_speed"]),
                "move_speed_boost": float(settings["move_speed_boost"]),
            }

        active_camera_edits = set()

        def _apply_camera(settings):
            cam = engine.editor_camera if engine else None
            if not cam:
                return
            snapshot = _camera_snapshot(settings)
            cam.orthographic = snapshot["orthographic"]
            cam.fov = snapshot["fov"]
            cam.orthographic_size = snapshot["orthographic_size"]
            cam.rotation_speed = snapshot["rotation_speed"]
            cam.pan_speed = snapshot["pan_speed"]
            cam.zoom_speed = snapshot["zoom_speed"]
            cam.move_speed = snapshot["move_speed"]
            cam.move_speed_boost = snapshot["move_speed_boost"]
            edits = self.interaction_core.continuous_edits
            for edit_key in tuple(active_camera_edits):
                edits.update(edit_key, snapshot)

        def _camera_edit_key(field_name):
            return f"toolbar.camera.{str(field_name or '').strip()}"

        def _begin_camera_edit(field_name, initial_settings):
            edit_key = _camera_edit_key(field_name)
            if not edit_key.rsplit(".", 1)[-1]:
                raise ValueError("camera edit field must not be empty")
            initial = _camera_snapshot(initial_settings)
            descriptions = {
                "projection": "Change Scene Camera Projection",
                "toolbar.field_of_view": "Change Scene Camera Field of View",
                "toolbar.orthographic_size": "Change Scene Camera Orthographic Size",
                "toolbar.rotation_sensitivity": "Change Scene Camera Rotation Sensitivity",
                "toolbar.pan_speed": "Change Scene Camera Pan Speed",
                "toolbar.zoom_speed": "Change Scene Camera Zoom Speed",
                "toolbar.move_speed": "Change Scene Camera Move Speed",
                "toolbar.speed_boost": "Change Scene Camera Speed Boost",
                "reset": "Reset Scene Camera Settings",
            }
            description = descriptions.get(str(field_name), "Change Scene Camera Settings")
            self.interaction_core.continuous_edits.begin(
                edit_key,
                owner_id="toolbar",
                description=description,
                initial_value=initial,
                on_commit=lambda session: self.interaction_core.view_commands.set_value(
                    session.initial_value,
                    session.current_value,
                    _apply_camera,
                    description=session.description,
                    owner_view_id=camera_view_id,
                ),
                on_cancel=lambda session: _apply_camera(session.initial_value),
            )
            active_camera_edits.add(edit_key)

        def _end_camera_edit(field_name, final_settings):
            edit_key = _camera_edit_key(field_name)
            self.interaction_core.continuous_edits.update(
                edit_key,
                _camera_snapshot(final_settings),
            )
            active_camera_edits.discard(edit_key)
            self.interaction_core.continuous_edits.commit(edit_key)

        tb.sync_camera_from_engine = _sync_camera
        tb.apply_camera_to_engine = _apply_camera
        tb.begin_camera_edit = _begin_camera_edit
        tb.end_camera_edit = _end_camera_edit


    # ── Native panel callback wiring ───────────────────────────────────

    def _inspector_set_selected_file(self, path):
        """Compute asset category and call C++ SetSelectedFile."""
        if path:
            import os
            from Infernux.core.asset_types import asset_category_from_extension
            if "::submat:" in path:
                cat = "material"
            elif "::subanim:" in path:
                cat = "animclip3d"
            else:
                ext = os.path.splitext(path)[1].lower()
                cat = asset_category_from_extension(ext) or ""
            self.inspector_panel.set_selected_file(path, cat)
        else:
            self.inspector_panel.clear_selected_file()

    def _setup_scene_change_cleanup(self):
        def on_scene_changed():
            document_id = self.scene_file_manager.document_id
            if document_id:
                for view in (self.scene_view, self.game_view, self.ui_editor):
                    view.bind_document(document_id)
            from Infernux.engine.interaction import SelectionService
            SelectionService.instance().clear(
                reason="scene_changed",
                record_history=False,
            )
            self.scene_view._fly_to_active = False
            self.scene_view._fly_to_last_obj_id = 0
            self.scene_view._fly_to_close = False

        self.scene_file_manager.set_on_scene_changed(on_scene_changed)

    def _setup_layout_persistence(self):
        project_name = os.path.basename(self.project_path)

        docs_dir = None
        if os.name == "nt":
            try:
                import ctypes
                import ctypes.wintypes
                buf = ctypes.create_unicode_buffer(ctypes.wintypes.MAX_PATH)
                ctypes.windll.shell32.SHGetFolderPathW(None, 0x0005, None, 0, buf)
                if buf.value:
                    docs_dir = pathlib.Path(buf.value)
            except (OSError, ValueError) as _exc:
                pass
        if docs_dir is None:
            docs_dir = pathlib.Path.home() / "Documents"

        layout_dir = docs_dir / "Infernux" / project_name
        os.makedirs(layout_dir, exist_ok=True)
        _panel_state.init(str(layout_dir))

        imgui_ini_path = str(layout_dir / "imgui.ini")
        self.window_manager.set_imgui_ini_path(imgui_ini_path)

    def _persist_editor_state(self, *, include_scene_draft: bool = False):
        if bool(getattr(self, "_suspend_persist_state", False)):
            return
        if self.console is None or self.project_panel is None or self.window_manager is None:
            return
        if self.toolbar is not None:
            _panel_state.put("toolbar", {
                "camera_settings": self.toolbar.get_camera_settings(),
            })
        if self.console is not None:
            _panel_state.put("console", {
                "show_info": self.console.show_info,
                "show_warnings": self.console.show_warnings,
                "show_errors": self.console.show_errors,
                "collapse": self.console.collapse,
                "clear_on_play": self.console.clear_on_play,
                "error_pause": self.console.error_pause,
                "auto_scroll": self.console.auto_scroll,
            })
        _panel_state.put("project", {"current_path": self.project_panel.get_current_path()})
        _panel_state.put("window_manager", self.window_manager.save_state())
        if include_scene_draft and self.scene_file_manager:
            scene_state = self.scene_file_manager.save_session_state()
            if scene_state.get("dirty"):
                _panel_state.put("scene_session", scene_state)
            else:
                _panel_state.delete("scene_session")
        elif self.scene_file_manager and not self.scene_file_manager.is_dirty:
            _panel_state.delete("scene_session")
        if include_scene_draft:
            # This is the only persistence boundary allowed to capture the
            # global authoring session. Individual panels publish view state
            # only; they never serialize documents or flush panel_state.
            from Infernux.engine.interaction import DocumentRegistry

            documents = DocumentRegistry.instance()
            document_session = documents.capture_session_state()
            if document_session["documents"]:
                _panel_state.put("document_session", document_session)
            else:
                _panel_state.delete("document_session")
        # Scene/Game views are runtime-driven and must not persist panel payloads.
        _panel_state.delete("panel:scene_view")
        _panel_state.delete("panel:game_view")

        # Persist individual panel states for every window id we still track
        # (singletons live in _default_instances; dynamically opened ids may only
        # appear in _window_instances until closed — those must still save).
        wm = self.window_manager
        from Infernux.engine.interaction import DocumentRegistry

        documents = DocumentRegistry.instance()

        _panel_state.prune_document_view_states(
            is_document_backed=lambda view_id: wm.is_document_backed_view(
                view_id,
                wm.window_type_id(view_id),
            ),
            has_restorable_document=lambda view_id: (
                documents.document_for_view(view_id) is not None
                and not documents.is_session_restore_suppressed(view_id)
            ) or documents.has_pending_session_document(view_id),
        )
        seen_ids: set[str] = set()
        for wid in set(wm._default_instances.keys()) | set(wm._window_instances.keys()):
            if wid in seen_ids:
                continue
            seen_ids.add(wid)
            if wid in {"scene_view", "game_view"}:
                continue
            if (
                wm.is_document_backed_view(wid)
                and documents.is_session_restore_suppressed(wid)
            ):
                _panel_state.delete(f"panel:{wid}")
                continue
            inst = wm._window_instances.get(wid) or wm._default_instances.get(wid)
            if inst is None:
                continue
            if hasattr(inst, "save_state") and callable(inst.save_state):
                try:
                    data = inst.save_state()
                    if data:
                        _panel_state.put(f"panel:{wid}", data)
                    else:
                        _panel_state.delete(f"panel:{wid}")
                except Exception:
                    pass

        _panel_state.save()

    def _load_initial_scene(self):
        import Infernux.renderstack  # noqa: F401 — ensure RenderStack is discoverable
        self.scene_file_manager.load_last_scene_or_default()
        self.scene_file_manager.restore_session_state(_panel_state.get("scene_session"))
