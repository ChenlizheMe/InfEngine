"""
Build Settings — dockable editor panel for managing game builds.

Features:
  * Scene list (drag-drop from Project panel or "Add Open Scene")
  * Output directory picker
  * Display mode: Fullscreen Borderless / Windowed (custom size)
  * Splash items list: images (fade in/out + duration) and videos
  * Build / Build & Run with background progress
"""

import os
import sys
import threading
import copy
from collections import deque
from Infernux.engine.path_utils import (
    is_path_within,
    portable_path,
    relative_path,
    resolved_path,
    same_path,
)
from typing import Dict, List, Optional

from Infernux.debug import Debug
from Infernux.engine.build_settings import (
    BUILD_SETTINGS_FILE,
    build_settings_path as _settings_path,
    load_build_settings,
)
from Infernux.engine.build_cancellation import BuildCancelled
from Infernux.engine.build import (
    BuildCancellationToken,
    BuildConfiguration,
    BuildProfile,
    BuildRequest,
    BuildService,
    BuildUnavailableError,
    build_progress_fraction,
    current_host_player_target,
    exporter_registry,
)
from Infernux.engine.project_context import get_project_root
from Infernux.engine.game_builder import (
    BuildOutputDirectoryError,
    GameBuilder,
)
from Infernux.engine.i18n import t
from Infernux.engine.interaction import (
    BoundPanelCommand,
    PanelCommandAdapter,
    PanelCommandSpec,
    PanelInteractionDescriptor,
    ensure_project_settings_document,
    normalize_build_settings,
)
from .editor_panel import EditorPanel
from .panel_registry import editor_panel
from .theme import Theme, ImGuiCol, ImGuiStyleVar
from ._dialogs import pick_folder_dialog, pick_file_dialog


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

_VIDEO_EXTS = {".mp4", ".avi", ".mov", ".mkv", ".webm"}
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tga"}
_ICON_EXTS = {".png", ".jpg", ".jpeg", ".ico"}

_DISPLAY_MODES_KEYS = ["build.fullscreen_borderless", "build.windowed"]
_DISPLAY_MODE_KEYS = ["fullscreen_borderless", "windowed"]
_ANDROID_ARTIFACTS = ["apk", "aab"]

# ---------------------------------------------------------------------------
# Drag-drop type & style constants
# ---------------------------------------------------------------------------

DRAG_DROP_SCENE = "SCENE_FILE"
DRAG_DROP_REORDER = "BUILD_REORDER"
_DRAG_TARGET_COLOR = Theme.DRAG_DROP_TARGET


def _bind_build_settings_panel(panel: object) -> PanelCommandAdapter:
    required = (
        "can_start_build",
        "command_start_build",
        "can_cancel_build",
        "command_cancel_build",
    )
    missing = tuple(name for name in required if not callable(getattr(panel, name, None)))
    if missing:
        raise TypeError(f"build settings interaction contract is missing: {missing}")
    return PanelCommandAdapter(
        {
            "build.start": BoundPanelCommand(
                lambda _context: panel.command_start_build(run_after=False),
                lambda _context: panel.can_start_build(),
            ),
            "build.start_and_run": BoundPanelCommand(
                lambda _context: panel.command_start_build(run_after=True),
                lambda _context: (
                    panel.can_start_build() and panel.can_run_after_build()
                ),
            ),
            "build.cancel": BoundPanelCommand(
                lambda _context: panel.command_cancel_build(),
                lambda _context: panel.can_cancel_build(),
            ),
        }
    )


_BUILD_SETTINGS_INTERACTION = PanelInteractionDescriptor(
    document_backed=True,
    commands=(
        PanelCommandSpec("build.start"),
        PanelCommandSpec("build.start_and_run"),
        PanelCommandSpec("build.cancel"),
    ),
    adapter_factory=_bind_build_settings_panel,
)


@editor_panel(
    "Build Settings",
    type_id="build_settings",
    title_key="menu.build_settings",
    menu_path="",
    interaction=_BUILD_SETTINGS_INTERACTION,
)
class BuildSettingsPanel(EditorPanel):
    """Build Settings utility surface hosted by the global panel lifecycle."""

    def __init__(self):
        super().__init__(title="Build Settings", window_id="build_settings")
        self._build_target: str = ""
        self._android_artifact: str = "apk"
        self._game_name: str = ""
        self._scenes: List[str] = []
        self._output_dir: str = ""
        self._icon_path: str = ""
        self._display_mode_idx: int = 0  # 0=fullscreen, 1=windowed
        self._window_width: int = 1280
        self._window_height: int = 720
        self._window_resizable: bool = True
        self._splash_items: List[Dict] = []
        self._settings_controller = None
        self._pending_settings_edits = deque()
        self._pending_settings_edits_lock = threading.Lock()
        self._load()

        # Build state
        self._building: bool = False
        self._build_progress: float = 0.0
        self._build_message: str = ""
        self._build_cancelled: bool = False
        self._build_error: Optional[str] = None
        self._build_output_dir: Optional[str] = None
        self._cancel_event: threading.Event = threading.Event()
        self._build_cancellation: BuildCancellationToken | None = None
        self._active_build_target: str = ""

    def _initial_size(self) -> tuple[float, float]:
        return 980.0, 720.0

    def on_enable(self) -> None:
        self._bind_project_settings_document()

    def _document_controller_for_registry(self):
        """Claim a persisted settings document with its real shared controller.

        Generic panel restoration runs before ``on_enable``.  Returning the
        panel there used to register a Project Settings document with a panel
        controller, after which the real controller quite correctly rejected
        the incompatible binding and the editor failed during startup.
        """
        root = get_project_root()
        if not root:
            return self
        controller = ensure_project_settings_document(root)
        self._settings_controller = controller
        return controller

    def on_disable(self) -> None:
        if self._settings_controller is not None:
            self._settings_controller.remove_listener(
                self._apply_project_settings_document
            )

    def _bind_project_settings_document(self) -> None:
        root = get_project_root()
        if not root:
            raise RuntimeError("Build Settings requires an active project")
        controller = ensure_project_settings_document(
            root,
            view_id=self.window_id,
        )
        if self._settings_controller is not None and self._settings_controller is not controller:
            self._settings_controller.remove_listener(
                self._apply_project_settings_document
            )
        self._settings_controller = controller
        controller.add_listener(self._apply_project_settings_document)
        self.bind_document(controller.document_id)
        self._apply_project_settings_document(controller.capture_document())

    def get_scene_list(self) -> List[str]:
        return list(self._scenes)

    def _available_build_targets(self):
        targets = exporter_registry.targets()
        desktop = current_host_player_target(targets)
        desktop_id = str(desktop.id) if desktop is not None else ""
        return tuple(
            sorted(
                targets,
                key=lambda item: (
                    str(item.id) != desktop_id,
                    item.platform,
                    item.display_name.casefold(),
                ),
            )
        )

    def _resolved_build_target(self):
        targets = self._available_build_targets()
        selected = str(getattr(self, "_build_target", "") or "")
        for target in targets:
            if target.id == selected:
                return target
        desktop = current_host_player_target(targets)
        if desktop is not None:
            for target in targets:
                if target.id == desktop.id:
                    return target
        return targets[0] if targets else None

    def _synchronize_build_target(self, *, persist: bool) -> object | None:
        target = self._resolved_build_target()
        identifier = str(target.id) if target is not None else ""
        if identifier == getattr(self, "_build_target", ""):
            return target
        self._build_target = identifier
        if persist and getattr(self, "_settings_controller", None) is not None:
            self._save()
        return target

    def _is_desktop_target(self, target_id: str | None = None) -> bool:
        desktop = current_host_player_target(exporter_registry.targets())
        identifier = str(
            target_id if target_id is not None else getattr(self, "_build_target", "")
        )
        return desktop is not None and identifier == desktop.id

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load(self):
        data = (
            self._settings_controller.section("build")
            if self._settings_controller is not None
            else normalize_build_settings(load_build_settings())
        )
        self._apply_build_settings(data)

    def _apply_project_settings_document(self, document: dict) -> None:
        self._apply_build_settings(document["build"])

    def _apply_build_settings(self, data: dict) -> None:
        self._build_target = data.get("build_target", "")
        self._android_artifact = data.get("android_artifact", "apk")
        self._game_name = data.get("game_name", "")
        self._scenes = list(data.get("scenes", []))
        self._output_dir = data.get("output_dir", "")
        self._icon_path = data.get("icon_path", "")
        mode_key = data.get("display_mode", "fullscreen_borderless")
        self._display_mode_idx = (
            _DISPLAY_MODE_KEYS.index(mode_key)
            if mode_key in _DISPLAY_MODE_KEYS else 0
        )
        self._window_width = data.get("window_width", 1280)
        self._window_height = data.get("window_height", 720)
        self._window_resizable = data.get("window_resizable", True)
        self._debug_mode = data.get("debug_mode", False)
        self._lto = data.get("lto", True)
        self._enable_jit = data.get("enable_jit", False)
        self._splash_items = list(data.get("splash_items", []))

    def _capture_build_settings(self) -> dict:
        return normalize_build_settings({
            "build_target": self._build_target,
            "android_artifact": self._android_artifact,
            "game_name": self._game_name,
            "scenes": self._scenes,
            "output_dir": self._output_dir,
            "icon_path": self._icon_path,
            "display_mode": _DISPLAY_MODE_KEYS[self._display_mode_idx],
            "window_width": self._window_width,
            "window_height": self._window_height,
            "window_resizable": self._window_resizable,
            "debug_mode": self._debug_mode,
            "lto": self._lto,
            "enable_jit": self._enable_jit,
            "splash_items": self._splash_items,
        })

    def _save(self):
        controller = self._settings_controller
        if controller is None:
            raise RuntimeError("Build Settings is not bound to Project Settings")
        following = self._capture_build_settings()
        previous = controller.section("build")
        changed_fields = sorted(
            key for key in following if following[key] != previous.get(key)
        )
        if not changed_fields:
            return False
        self.publish_interaction_ownership(reason="project_settings_edit")
        field_key = "+".join(changed_fields)
        return controller.apply_section(
            "build",
            following,
            edit_key=f"project_settings.build.{field_key}",
            description="Edit Build Settings",
            view_id=self.window_id,
        )

    # ------------------------------------------------------------------
    # Rendering
    # ------------------------------------------------------------------

    def on_render_content(self, ctx):
        self._drain_pending_settings_edits()
        self._render_body(ctx)

    def _enqueue_settings_edit(self, callback) -> None:
        with self._pending_settings_edits_lock:
            self._pending_settings_edits.append(callback)

    def _drain_pending_settings_edits(self) -> None:
        with self._pending_settings_edits_lock:
            callbacks = tuple(self._pending_settings_edits)
            self._pending_settings_edits.clear()
        for callback in callbacks:
            callback()

    # ------------------------------------------------------------------

    def _is_preflight_active(self) -> bool:
        from Infernux.engine.ui.build_preflight_progress import (
            BuildPreflightProgressService,
        )
        return bool(BuildPreflightProgressService.instance().is_active)

    def _footer_reserve_height(self) -> float:
        if self._building:
            return 56.0 if self._is_preflight_active() else 96.0
        if self._build_error:
            return 176.0
        if self._build_cancelled or self._build_output_dir:
            return 72.0
        return 52.0

    def _render_wrapped_message(self, ctx, message: str, *, color=None, height: Optional[float] = None) -> None:
        if color is not None:
            ctx.push_style_color(ImGuiCol.Text, *color)
        writer = getattr(ctx, "text_wrapped", None)
        if height is not None:
            if ctx.begin_child("##build_status_message", 0, float(height), True):
                if callable(writer):
                    writer(str(message))
                else:
                    ctx.label(str(message))
            ctx.end_child()
        elif callable(writer):
            writer(str(message))
        else:
            ctx.label(str(message))
        if color is not None:
            ctx.pop_style_color(1)

    def _render_action_row(self, ctx, buttons) -> None:
        gap = 8.0
        widths = [float(width) for _label, _callback, width, _semantic_id, _enabled in buttons]
        total = sum(widths) + gap * max(0, len(buttons) - 1)
        avail = float(ctx.get_content_region_avail_width())
        get_x = getattr(ctx, "get_cursor_pos_x", None)
        set_x = getattr(ctx, "set_cursor_pos_x", None)
        if callable(get_x) and callable(set_x) and avail > total:
            set_x(float(get_x()) + avail - total)
        for index, (label, callback, width, semantic_id, enabled) in enumerate(buttons):
            if index:
                ctx.same_line(0, gap)
            ctx.button(label, callback, width=float(width), height=30)
            ctx.record_semantic_item(
                "button",
                str(label).split("##", 1)[0].strip(),
                enabled,
                semantic_id,
            )

    def _render_body(self, ctx):
        ctx.dummy(0.0, 4.0)
        child_h = max(0, ctx.get_content_region_avail_height() - self._footer_reserve_height())
        # The worker can complete between BeginDisabled and EndDisabled.
        # Keep the pair governed by one immutable decision for this frame.
        building_this_frame = self._building
         
        ctx.push_style_color(ImGuiCol.ChildBg, 0.0, 0.0, 0.0, 0.0)
        ctx.push_style_var_float(ImGuiStyleVar.ChildBorderSize, 0.0)
        if building_this_frame:
            ctx.begin_disabled(True)
        if ctx.begin_child("##build_body", 0, child_h, False):
            self._render_target_section(ctx)
            ctx.separator()
            self._render_output_section(ctx)
            ctx.separator()
            self._render_display_section(ctx)
            ctx.separator()
            self._render_splash_section(ctx)
            ctx.separator()
            self._render_scene_section(ctx)
        ctx.end_child()
        if building_this_frame:
            ctx.end_disabled()
        ctx.pop_style_var(1)
        ctx.pop_style_color(1)

        ctx.separator()
        self._render_build_controls(ctx)

    def _render_target_section(self, ctx):
        targets = self._available_build_targets()
        target = self._synchronize_build_target(persist=True)
        ctx.label(t("build.target"))
        if not targets or target is None:
            ctx.record_semantic_item(
                "status",
                t("build.target"),
                False,
                "build_settings.target",
                string_value="",
            )
            ctx.label(t("build.no_targets"))
            return

        target_ids = [str(item.id) for item in targets]
        selected_index = target_ids.index(str(target.id))
        labels = [item.display_name for item in targets]
        next_index = ctx.combo("##build_target", selected_index, labels)
        next_index = max(0, min(len(targets) - 1, int(next_index)))
        selected = targets[next_index]
        ctx.record_semantic_item(
            "combo",
            t("build.target"),
            True,
            "build_settings.target",
            string_value=str(selected.id),
        )
        if str(selected.id) != self._build_target:
            self._build_target = str(selected.id)
            self._save()

        if selected.platform == "android":
            ctx.same_line(0, 20)
            ctx.label(t("build.android_artifact"))
            ctx.same_line(0, 8)
            artifact_index = _ANDROID_ARTIFACTS.index(self._android_artifact)
            next_artifact_index = ctx.combo(
                "##android_artifact",
                artifact_index,
                ["APK", "AAB"],
            )
            next_artifact = _ANDROID_ARTIFACTS[
                max(0, min(len(_ANDROID_ARTIFACTS) - 1, int(next_artifact_index)))
            ]
            ctx.record_semantic_item(
                "combo",
                t("build.android_artifact"),
                True,
                "build_settings.android_artifact",
                string_value=next_artifact,
            )
            if next_artifact != self._android_artifact:
                self._android_artifact = next_artifact
                self._save()

    # ------------------------------------------------------------------
    # OUTPUT DIRECTORY
    # ------------------------------------------------------------------

    def _render_output_section(self, ctx):
        ctx.label(t("build.game_name"))
        root = get_project_root()
        placeholder = os.path.basename(root) if root else "MyGame"
        ctx.set_next_item_width(300)
        new_name = ctx.text_input("##game_name", self._game_name, 256)
        ctx.record_semantic_item(
            "text_input",
            t("build.game_name"),
            True,
            "build_settings.game_name",
            string_value=new_name,
        )
        if new_name != self._game_name:
            self._game_name = new_name
            self._save()
        ctx.same_line(0, 20)
        new_debug = ctx.checkbox(t("build.debug_mode") + "##debug_mode", self._debug_mode)
        ctx.record_semantic_item(
            "checkbox",
            t("build.debug_mode"),
            True,
            "build_settings.debug_mode",
            bool_value=new_debug,
        )
        if new_debug != self._debug_mode:
            self._debug_mode = new_debug
            self._save()
        ctx.same_line(0, 20)
        new_lto = ctx.checkbox(t("build.lto") + "##lto", self._lto)
        ctx.record_semantic_item(
            "checkbox", t("build.lto"), True, "build_settings.lto", bool_value=new_lto
        )
        if new_lto != self._lto:
            self._lto = new_lto
            self._save()
        ctx.same_line(0, 20)
        new_jit = ctx.checkbox(t("build.enable_jit") + "##enable_jit", self._enable_jit)
        ctx.record_semantic_item(
            "checkbox",
            t("build.enable_jit"),
            True,
            "build_settings.enable_jit",
            bool_value=new_jit,
        )
        if new_jit != self._enable_jit:
            self._enable_jit = new_jit
            self._save()
        if not self._game_name:
            ctx.same_line()
            ctx.push_style_color(ImGuiCol.Text, 0.5, 0.5, 0.5, 1.0)
            ctx.label(t("build.game_name_hint").format(name=placeholder))
            ctx.pop_style_color(1)

        ctx.label(t("build.output_directory"))
        ctx.set_next_item_width(ctx.get_content_region_avail_width() - 84)
        new_val = ctx.text_input("##output_dir", self._output_dir, 512)
        ctx.record_semantic_item(
            "text_input",
            t("build.output_directory"),
            True,
            "build_settings.output_dir",
            string_value=new_val,
        )
        if new_val != self._output_dir:
            self._output_dir = new_val
            self._save()
        ctx.same_line()
        browse_output_label = t("build.browse")
        ctx.button(browse_output_label + "##browse_out", self._browse_output_dir, width=80)
        ctx.record_semantic_item(
            "button", browse_output_label, True, "build_settings.output_dir.browse"
        )
        ctx.push_style_color(ImGuiCol.Text, 0.5, 0.5, 0.5, 1.0)
        ctx.label(t("build.output_directory_hint").format(marker=GameBuilder.OUTPUT_MARKER_FILENAME))
        ctx.pop_style_color(1)

        ctx.label(t("build.icon"))
        clear_btn_w = 80 if self._icon_path else 0
        icon_input_w = ctx.get_content_region_avail_width() - 84 - (clear_btn_w + (4 if clear_btn_w else 0))
        ctx.set_next_item_width(max(120, icon_input_w))
        new_icon = ctx.text_input("##build_icon", self._icon_path, 512)
        ctx.record_semantic_item(
            "text_input",
            t("build.icon"),
            True,
            "build_settings.icon",
            string_value=new_icon,
        )
        if new_icon != self._icon_path:
            self._icon_path = new_icon
            self._save()
        ctx.same_line()
        browse_icon_label = t("build.browse")
        ctx.button(browse_icon_label + "##browse_icon", self._browse_icon_path, width=80)
        ctx.record_semantic_item(
            "button", browse_icon_label, True, "build_settings.icon.browse"
        )
        if self._icon_path:
            ctx.same_line(0, 4)
            clear_icon_label = t("build.clear_icon")
            ctx.button(clear_icon_label + "##clear_icon", self._clear_icon_path, width=80)
            ctx.record_semantic_item(
                "button", clear_icon_label, True, "build_settings.icon.clear"
            )
        else:
            ctx.push_style_color(ImGuiCol.Text, 0.5, 0.5, 0.5, 1.0)
            ctx.label("  " + t("build.icon_hint"))
            ctx.pop_style_color(1)

    def _browse_output_dir(self):
        def _do():
            try:
                folder = pick_folder_dialog("Choose Output Directory")
                if folder:
                    self._enqueue_settings_edit(
                        lambda value=folder: self._accept_output_dir(value)
                    )
            except Exception as exc:
                Debug.log_warning(f"Build Settings output directory browse failed: {exc}")
        threading.Thread(target=_do, daemon=True).start()

    def _browse_icon_path(self):
        def _do():
            try:
                path = pick_file_dialog(
                    "Choose Build Icon",
                    win32_filter="Images (*.png;*.jpg;*.jpeg;*.ico)\0*.png;*.jpg;*.jpeg;*.ico\0All Files (*.*)\0*.*\0\0",
                    tk_filetypes=[("Images", "*.png *.jpg *.jpeg *.ico"), ("All Files", "*.*")],
                )
                if path:
                    ext = os.path.splitext(path)[1].lower()
                    if ext not in _ICON_EXTS:
                        raise ValueError("Unsupported icon format")
                    self._enqueue_settings_edit(
                        lambda value=resolved_path(path): self._accept_icon_path(value)
                    )
            except Exception as exc:
                Debug.log_warning(f"Build Settings icon picker failed: {exc}")
        threading.Thread(target=_do, daemon=True).start()

    def _accept_output_dir(self, path: str) -> None:
        self._output_dir = str(path)
        self._save()

    def _accept_icon_path(self, path: str) -> None:
        self._icon_path = str(path)
        self._save()

    def _clear_icon_path(self):
        if not self._icon_path:
            return
        self._icon_path = ""
        self._save()

    # ------------------------------------------------------------------
    # DISPLAY MODE
    # ------------------------------------------------------------------

    def _render_display_section(self, ctx):
        ctx.label(t("build.display_mode"))
        display_modes = [t(k) for k in _DISPLAY_MODES_KEYS]
        new_idx = ctx.combo("##display_mode", self._display_mode_idx, display_modes)
        ctx.record_semantic_item(
            "combo",
            t("build.display_mode"),
            True,
            "build_settings.display_mode",
            string_value=_DISPLAY_MODE_KEYS[new_idx],
        )
        if new_idx != self._display_mode_idx:
            self._display_mode_idx = new_idx
            self._save()

        if self._display_mode_idx == 1:  # Windowed
            ctx.label(t("build.window_size"))
            new_w = ctx.input_int(t("build.width") + "##win_w", self._window_width, 16, 160)
            ctx.record_semantic_item(
                "int_input",
                t("build.width"),
                True,
                "build_settings.window.width",
                numeric_value=float(new_w),
            )
            if new_w != self._window_width:
                self._window_width = max(320, min(7680, new_w))
                self._save()
            ctx.same_line()
            new_h = ctx.input_int(t("build.height") + "##win_h", self._window_height, 16, 160)
            ctx.record_semantic_item(
                "int_input",
                t("build.height"),
                True,
                "build_settings.window.height",
                numeric_value=float(new_h),
            )
            if new_h != self._window_height:
                self._window_height = max(240, min(4320, new_h))
                self._save()

            new_resizable = ctx.checkbox(t("build.window_resizable") + "##resizable", self._window_resizable)
            ctx.record_semantic_item(
                "checkbox",
                t("build.window_resizable"),
                True,
                "build_settings.window.resizable",
                bool_value=new_resizable,
            )
            if new_resizable != self._window_resizable:
                self._window_resizable = new_resizable
                self._save()



    # ------------------------------------------------------------------
    # SPLASH ITEMS
    # ------------------------------------------------------------------

    def _render_splash_section(self, ctx):
        ctx.label(t("build.splash_sequence"))
        ctx.button(t("build.add_splash") + "##add_splash", self._browse_splash_file, width=200)

        remove_idx: Optional[int] = None

        for i, item in enumerate(self._splash_items):
            ctx.push_id(i + 10000)
            ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing, *Theme.BUILD_SETTINGS_ROW_SPC)

            fname = os.path.basename(item.get("path", "<none>"))
            item_type = item.get("type", "image")
            badge = "[IMG]" if item_type == "image" else "[VID]"
            source_exists = os.path.isfile(item.get("path", ""))

            # ── Row 1: name ──
            if not source_exists:
                ctx.push_style_color(ImGuiCol.Text, *Theme.ERROR_TEXT)
            ctx.label(f"  {i + 1}. {badge}  {fname}")
            if not source_exists:
                ctx.same_line(0, 8)
                ctx.label(t("build.source_missing"))
                ctx.pop_style_color(1)
            if ctx.is_item_hovered():
                ctx.set_tooltip(item.get("path", ""))

            # ── Row 2: numeric fields ──
            if item_type == "image":
                ctx.label(f"      {t('build.duration')} ({t('build.seconds_short')})")
                ctx.same_line(0, 8)
                ctx.set_next_item_width(120)
                new_dur = ctx.input_float(f"##dur{i}", item.get("duration", 3.0), 0.1, 1.0)
                if new_dur != item.get("duration", 3.0):
                    item["duration"] = max(0.1, new_dur)
                    self._save()
                ctx.same_line(0, 24)
            else:
                ctx.label("      ")
                ctx.same_line(0, 0)

            ctx.label(f"{t('build.fade_in')} ({t('build.seconds_short')})")
            ctx.same_line(0, 8)
            ctx.set_next_item_width(120)
            new_fi = ctx.input_float(f"##fi{i}", item.get("fade_in", 0.5), 0.1, 0.5)
            if new_fi != item.get("fade_in", 0.5):
                item["fade_in"] = max(0.0, new_fi)
                self._save()

            ctx.same_line(0, 24)
            ctx.label(f"{t('build.fade_out')} ({t('build.seconds_short')})")
            ctx.same_line(0, 8)
            ctx.set_next_item_width(120)
            new_fo = ctx.input_float(f"##fo{i}", item.get("fade_out", 0.5), 0.1, 0.5)
            if new_fo != item.get("fade_out", 0.5):
                item["fade_out"] = max(0.0, new_fo)
                self._save()

            # ── Row 3: action buttons ──
            ctx.label(" ")
            
            btn_w = 64
            btn_spc = 4
            num_btns = 1 + int(i > 0) + int(i < len(self._splash_items) - 1)
            btn_area = num_btns * btn_w + (num_btns - 1) * btn_spc + 24
            
            ctx.same_line(max(ctx.get_window_width() - btn_area, 200))
            
            if i > 0:
                def _up(idx=i):
                    self._splash_items[idx - 1], self._splash_items[idx] = (
                        self._splash_items[idx], self._splash_items[idx - 1]
                    )
                    self._save()
                ctx.button(t("build.move_up") + f"##sp_{i}", _up, width=btn_w)
                ctx.same_line(0, btn_spc)

            if i < len(self._splash_items) - 1:
                def _down(idx=i):
                    self._splash_items[idx], self._splash_items[idx + 1] = (
                        self._splash_items[idx + 1], self._splash_items[idx]
                    )
                    self._save()
                ctx.button(t("build.move_down") + f"##sp_{i}", _down, width=btn_w)
                ctx.same_line(0, btn_spc)

            def _rm(idx=i):
                nonlocal remove_idx
                remove_idx = idx
            ctx.button(t("build.remove") + f"##sp_{i}", _rm, width=btn_w)

            ctx.separator()

            ctx.pop_style_var(1)
            ctx.pop_id()

        if remove_idx is not None:
            del self._splash_items[remove_idx]
            self._save()

        if not self._splash_items:
            ctx.label("  " + t("build.no_splash_items"))

    def _browse_splash_file(self):
        def _do():
            try:
                path = pick_file_dialog(
                    "Add Splash Item",
                    win32_filter="Images (*.png;*.jpg;*.jpeg;*.bmp)\0*.png;*.jpg;*.jpeg;*.bmp\0Videos (*.mp4;*.avi;*.mov;*.mkv;*.webm)\0*.mp4;*.avi;*.mov;*.mkv;*.webm\0All Files (*.*)\0*.*\0\0",
                    tk_filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("Videos", "*.mp4 *.avi *.mov *.mkv *.webm"), ("All Files", "*.*")],
                )
                if path:
                    ext = os.path.splitext(path)[1].lower()
                    itype = "video" if ext in _VIDEO_EXTS else "image"
                    item = {
                        "type": itype,
                        "path": resolved_path(path),
                        "duration": 3.0 if itype == "image" else 0.0,
                        "fade_in": 0.5,
                        "fade_out": 0.5,
                    }
                    self._enqueue_settings_edit(
                        lambda value=item: self._accept_splash_item(value)
                    )
            except Exception as exc:
                Debug.log_warning(f"Build Settings splash picker failed: {exc}")
        threading.Thread(target=_do, daemon=True).start()

    def _accept_splash_item(self, item: dict) -> None:
        self._splash_items.append(copy.deepcopy(item))
        self._save()

    # ------------------------------------------------------------------
    # SCENE LIST
    # ------------------------------------------------------------------

    def _render_scene_section(self, ctx):
        ctx.label(t("build.scenes_in_build"))

        from Infernux.engine.scene_manager import SceneFileManager
        sfm = SceneFileManager.instance()
        can_add_open_scene = bool(sfm and sfm.current_scene_path)

        def _add_current():
            if sfm and sfm.current_scene_path:
                self._add_scene(sfm.current_scene_path)

        if not can_add_open_scene:
            ctx.begin_disabled(True)
        add_open_scene_label = t("build.add_open_scene")
        if ctx.button("  " + add_open_scene_label + "  "):
            _add_current()
        ctx.record_semantic_item(
            "button", add_open_scene_label, can_add_open_scene, "build_settings.scene.add_open"
        )
        if not can_add_open_scene:
            ctx.end_disabled()
            if ctx.is_item_hovered():
                ctx.set_tooltip("Please save the current scene before adding it to Build Settings.")

        remove_idx: Optional[int] = None
        swap_pair: Optional[tuple] = None

        for i, scene_path in enumerate(self._scenes):
            ctx.push_id(i)

            name = os.path.splitext(os.path.basename(scene_path))[0]
            root = get_project_root() or ""
            absolute_scene = resolved_path(
                scene_path
                if os.path.isabs(scene_path) or not root
                else os.path.join(root, scene_path)
            )
            try:
                rel = relative_path(absolute_scene, root) if root else scene_path
            except ValueError:
                # Windows cannot compute a relative path across drive letters.
                rel = absolute_scene

            ctx.push_style_var_vec2(ImGuiStyleVar.ItemSpacing, *Theme.BUILD_SETTINGS_ROW_SPC)
            
            # Use a fixed row height so selectable and buttons align
            row_h = 24
            ctx.selectable(f"  {i}    {name}    ({rel})##row", False, 16, 0, row_h)
            ctx.record_semantic_item(
                "selectable",
                name,
                True,
                f"build_settings.scene.{i}.row",
                string_value=portable_path(rel),
            )

            # Drag source — reorder
            if ctx.begin_drag_drop_source(0):
                ctx.set_drag_drop_payload(DRAG_DROP_REORDER, i)
                ctx.label(f"{i}: {name}")
                ctx.end_drag_drop_source()

            # Drop target
            from .igui import IGUI
            def _on_drop(dtype, payload, _i=i):
                nonlocal swap_pair
                if dtype == DRAG_DROP_REORDER:
                    swap_pair = (int(payload), _i)
                elif dtype == DRAG_DROP_SCENE:
                    self._add_scene(str(payload))
            IGUI.multi_drop_target(ctx, (DRAG_DROP_REORDER, DRAG_DROP_SCENE), _on_drop)

            btn_w = 64
            btn_spc = 4
            num_btns = 1 + int(i > 0) + int(i < len(self._scenes) - 1)
            btn_area = num_btns * btn_w + (num_btns - 1) * btn_spc + 24
            
            ctx.same_line(max(ctx.get_window_width() - btn_area, 200))
            if i > 0:
                def _up(idx=i):
                    self._scenes[idx - 1], self._scenes[idx] = self._scenes[idx], self._scenes[idx - 1]
                    self._save()
                ctx.button(t("build.move_up") + f"##{i}", _up, width=btn_w, height=row_h)
                ctx.record_semantic_item(
                    "button", t("build.move_up"), True, f"build_settings.scene.{i}.move_up"
                )
                ctx.same_line(0, btn_spc)

            if i < len(self._scenes) - 1:
                def _down(idx=i):
                    self._scenes[idx], self._scenes[idx + 1] = self._scenes[idx + 1], self._scenes[idx]
                    self._save()
                ctx.button(t("build.move_down") + f"##{i}", _down, width=btn_w, height=row_h)
                ctx.record_semantic_item(
                    "button", t("build.move_down"), True, f"build_settings.scene.{i}.move_down"
                )
                ctx.same_line(0, btn_spc)

            def _rm(idx=i):
                nonlocal remove_idx
                remove_idx = idx
            ctx.button(t("build.remove") + f"##{i}", _rm, width=btn_w, height=row_h)
            ctx.record_semantic_item(
                "button", t("build.remove"), True, f"build_settings.scene.{i}.remove"
            )

            ctx.pop_style_var(1)
            ctx.pop_id()

        if remove_idx is not None:
            del self._scenes[remove_idx]
            self._save()
        if swap_pair is not None:
            src, dst = swap_pair
            if 0 <= src < len(self._scenes) and 0 <= dst < len(self._scenes) and src != dst:
                item = self._scenes.pop(src)
                self._scenes.insert(dst, item)
                self._save()

        # Drop target for the entire scene section
        from .igui import IGUI
        IGUI.drop_target(ctx, DRAG_DROP_SCENE, lambda p: self._add_scene(str(p)))

        if not self._scenes:
            ctx.label("")
            ctx.label("  " + t("build.list_empty"))
            ctx.label("  " + t("build.drag_scenes_hint"))

    # ------------------------------------------------------------------
    # Build controls
    # ------------------------------------------------------------------

    def _render_build_controls(self, ctx):
        if self._building:
            ctx.record_semantic_item(
                "status",
                "Building",
                False,
                "build_settings.status",
                string_value="building",
            )
            progress_message = self._build_message or t("build.building")
            ctx.record_semantic_item(
                "status",
                "Build progress message",
                False,
                "build_settings.progress_message",
                string_value=progress_message,
            )
            # Preflight already owns the only visible slider. Drawing another
            # bar in this window stacks an outer track on the inner modal.
            if not self._is_preflight_active():
                ctx.record_semantic_item(
                    "status",
                    "Build progress",
                    False,
                    "build_settings.progress",
                    numeric_value=float(self._build_progress),
                )
                self._render_wrapped_message(ctx, progress_message)
                ctx.progress_bar(self._build_progress, -1.0, 20.0, "")
            else:
                self._render_wrapped_message(ctx, progress_message)
            cancel_label = t("build.cancel")
            can_cancel = self.can_cancel_build()
            self._render_action_row(
                ctx,
                (
                    (
                        "  " + cancel_label + "  ##cancel_build",
                        lambda: self._execute_build_command("build.cancel"),
                        120.0,
                        "build_settings.cancel",
                        can_cancel,
                    ),
                ),
            )
        elif self._build_cancelled:
            ctx.record_semantic_item(
                "status", "Cancelled", False, "build_settings.status", string_value="cancelled"
            )
            self._render_wrapped_message(ctx, t("build.cancelled"))
            self._render_action_row(
                ctx,
                (
                    (
                        "OK##dismiss_cancelled",
                        self._dismiss_build_cancelled,
                        80.0,
                        "build_settings.cancelled.dismiss",
                        True,
                    ),
                ),
            )
        elif self._build_error:
            ctx.record_semantic_item(
                "status", "Failed", False, "build_settings.status", string_value="failed"
            )
            ctx.record_semantic_item(
                "status",
                "Build error",
                False,
                "build_settings.error",
                string_value=str(self._build_error),
            )
            self._render_wrapped_message(
                ctx,
                t("build.failed").format(err=self._build_error),
                color=Theme.ERROR_TEXT,
                height=110.0,
            )
            self._render_action_row(
                ctx,
                (
                    (
                        "OK##dismiss_err",
                        self._dismiss_build_error,
                        80.0,
                        "build_settings.error.dismiss",
                        True,
                    ),
                ),
            )
        elif self._build_output_dir:
            ctx.record_semantic_item(
                "status", "Succeeded", False, "build_settings.status", string_value="succeeded"
            )
            ctx.record_semantic_item(
                "status",
                "Build output",
                False,
                "build_settings.result.output_dir",
                string_value=str(self._build_output_dir),
            )
            self._render_wrapped_message(
                ctx,
                t("build.succeeded").format(path=os.path.basename(self._build_output_dir) + "/"),
                color=Theme.SUCCESS_TEXT,
            )

            def _open_folder():
                import subprocess as _sp
                import sys as _sys
                if _sys.platform == "win32":
                    os.startfile(self._build_output_dir)
                elif _sys.platform == "darwin":
                    _sp.Popen(["open", self._build_output_dir])
                else:
                    _sp.Popen(["xdg-open", self._build_output_dir])

            open_folder_label = t("build.open_folder")
            self._render_action_row(
                ctx,
                (
                    (
                        open_folder_label,
                        _open_folder,
                        140.0,
                        "build_settings.result.open_folder",
                        True,
                    ),
                    (
                        "OK##dismiss_ok",
                        self._dismiss_build_result,
                        80.0,
                        "build_settings.result.dismiss",
                        True,
                    ),
                ),
            )
        else:
            ctx.record_semantic_item(
                "status", "Ready", False, "build_settings.status", string_value="ready"
            )
            can_build = self.can_start_build()
            can_build_and_run = can_build and self.can_run_after_build()

            # Align build buttons to the right
            ctx.same_line(max(ctx.get_window_width() - 360, 200))

            if not can_build:
                ctx.begin_disabled(True)
            ctx.button(
                "  " + t("build.build") + "  ",
                lambda: self._execute_build_command("build.start"),
                width=140,
                height=36,
            )
            ctx.record_semantic_item("button", t("build.build"), can_build, "build_settings.build")
            if not can_build:
                ctx.end_disabled()
            ctx.same_line(0, 16)
            if not can_build_and_run:
                ctx.begin_disabled(True)
            ctx.button(
                "  " + t("build.build_and_run") + "  ",
                lambda: self._execute_build_command("build.start_and_run"),
                width=160,
                height=36,
            )
            ctx.record_semantic_item(
                "button",
                t("build.build_and_run"),
                can_build_and_run,
                "build_settings.build_and_run",
            )
            if not can_build_and_run:
                ctx.end_disabled()

    def _dismiss_build_error(self):
        self._build_error = None

    def _dismiss_build_cancelled(self):
        self._build_cancelled = False

    def _dismiss_build_result(self):
        self._build_output_dir = None

    # ------------------------------------------------------------------
    # Build execution
    # ------------------------------------------------------------------

    def _execute_build_command(self, command_id: str) -> bool:
        from Infernux.engine.interaction import CommandSource

        return self.execute_owned_command(command_id, source=CommandSource.TOOLBAR)

    def can_cancel_build(self) -> bool:
        if not self._building:
            return False
        from Infernux.engine.ui.build_preflight_progress import (
            BuildPreflightProgressService,
        )
        return bool(
            BuildPreflightProgressService.instance().is_active
            or not self._cancel_event.is_set()
        )

    def command_cancel_build(self) -> bool:
        if not self.can_cancel_build():
            return False
        from Infernux.engine.ui.build_preflight_progress import (
            BuildPreflightProgressService,
        )
        preflight = BuildPreflightProgressService.instance()
        if preflight.is_active:
            preflight.cancel()
            return True
        self._cancel_event.set()
        cancellation = getattr(self, "_build_cancellation", None)
        if cancellation is not None:
            cancellation.cancel()
        return True

    def can_start_build(self) -> bool:
        return bool(
            not self._building
            and self._scenes
            and self._output_dir
            and self._resolved_build_target() is not None
        )

    def can_run_after_build(self) -> bool:
        target = self._resolved_build_target()
        return bool(target is not None and self._is_desktop_target(str(target.id)))

    def command_start_build(self, *, run_after: bool) -> bool:
        if not self.can_start_build():
            return False
        if run_after and not self.can_run_after_build():
            return False
        return self._do_build(run_after=bool(run_after))

    def _format_output_directory_error(self, exc: BuildOutputDirectoryError) -> str:
        if exc.reason == "required":
            return t("build.output_directory_error_required")
        if exc.reason == "path-is-file":
            return t("build.output_directory_error_path_is_file").format(path=exc.path)
        if exc.reason == "path-not-directory":
            return t("build.output_directory_error_not_directory").format(path=exc.path)

        found_line = ""
        if exc.entries:
            found_line = "\n\n" + t("build.output_directory_error_found").format(
                entries=", ".join(exc.entries[:5]) + (", ..." if len(exc.entries) > 5 else "")
            )

        return t("build.output_directory_error_not_empty").format(
            path=exc.path,
            marker=exc.marker_filename,
        ) + found_line

    def _show_output_directory_error(self, exc: BuildOutputDirectoryError) -> None:
        message = self._format_output_directory_error(exc)
        self._build_error = message

    def _on_build_progress(self, message: str, fraction: float):
        self._build_message = message
        self._build_progress = fraction
        from Infernux.engine.ui.engine_status import EngineStatus
        # The Build Settings window owns the only determinate slider.
        # The status bar keeps a text pulse so the editor is not frozen-looking
        # when the utility window is behind another panel.
        EngineStatus.set(
            message or "Building player...",
            -1.0,
            kind="activity",
            source="build",
            priority=20,
        )
        if self._cancel_event.is_set():
            raise BuildCancelled()

    def _prepare_asset_catalog_for_build(self) -> str:
        """Publish a durable asset catalog before the worker reads the project.

        Asset writes intentionally invalidate ``Library/AssetIndex.json`` until
        the native database has observed the new disk state.  Build startup is
        an ownership boundary: finish coalesced writes and rebuild the derived
        catalog on the editor thread before handing immutable inputs to the
        background builder.
        """
        database = self.services.asset_database
        project_root = get_project_root()
        from Infernux.engine.player_build_preflight import publish_player_asset_catalog

        return str(publish_player_asset_catalog(project_root, database)["path"])

    def _published_player_catalog_entries(self, catalog) -> list[dict]:
        """Return the exact AssetIndex snapshot published by editor preflight."""

        entries = None
        index_path = ""
        if isinstance(catalog, dict):
            index_path = str(catalog.get("path") or "")
            raw_entries = catalog.get("entries")
            if isinstance(raw_entries, list):
                entries = raw_entries
        else:
            index_path = str(catalog or "")
        if entries is None:
            if not index_path or not os.path.isfile(index_path):
                database = getattr(self.services, "asset_database", None)
                if database is not None:
                    database.flush_derived_index()
                    index_path = str(getattr(database, "asset_index_path", "") or "")
            if not index_path or not os.path.isfile(index_path):
                raise RuntimeError(
                    "Library/AssetIndex.json is missing after Player catalog "
                    "preflight. Save pending assets and try again."
                )
            from Infernux.engine.runtime_artifact_catalog import load_asset_index

            project_root = get_project_root()
            if not project_root:
                raise RuntimeError("No project root found")
            entries = load_asset_index(project_root)
        return [dict(item) for item in entries]

    def _make_build_request(self, catalog, target_id: str) -> BuildRequest:
        entries = self._published_player_catalog_entries(catalog)
        cancellation = BuildCancellationToken()
        self._build_cancellation = cancellation
        configuration = (
            BuildConfiguration.DEVELOPMENT
            if self._debug_mode
            else BuildConfiguration.RELEASE
        )
        return BuildRequest(
            get_project_root(),
            target_id,
            self._output_dir,
            BuildProfile(
                configuration=configuration,
                debug_symbols=self._debug_mode,
                compress_resources=not self._debug_mode,
                options={
                    "android_artifact": self._android_artifact,
                    "build_settings": self._capture_build_settings(),
                },
            ),
            asset_catalog_entries=entries,
            cancellation=cancellation,
            progress=self._on_platform_build_progress,
        )

    def _on_platform_build_progress(self, progress) -> None:
        self._build_message = progress.message or "Building player..."
        self._build_progress = max(
            self._build_progress,
            build_progress_fraction(progress),
        )
        from Infernux.engine.ui.engine_status import EngineStatus

        EngineStatus.set(
            self._build_message,
            -1.0,
            kind="activity",
            source="build",
            priority=20,
        )
        if self._cancel_event.is_set():
            cancellation = self._build_cancellation
            if cancellation is not None:
                cancellation.cancel()
            raise BuildCancelled("Build cancelled")

    @staticmethod
    def _build_failure_message(result) -> str:
        lines = [
            f"[{item.code}] {item.message}"
            for item in result.diagnostics
            if item.message
        ]
        if not lines:
            lines.append("The platform exporter did not produce a Player artifact.")
        if result.logs:
            lines.extend(("", "Last build output:", *result.logs[-12:]))
        return "\n".join(lines)

    def _launch_desktop_result(self, result) -> None:
        import subprocess

        output = str(result.manifest.get("output_dir", self._output_dir))
        game_name = self._game_name.strip() or os.path.basename(get_project_root())
        executable = game_name + (".exe" if sys.platform == "win32" else "")
        launcher = os.path.join(output, executable)
        if not os.path.isfile(launcher):
            raise RuntimeError(f"Built Player launcher is missing: {launcher}")
        subprocess.Popen([launcher], cwd=output)

    def _begin_asset_catalog_for_build(self):
        """Begin durable writes without blocking the build button callback."""
        from Infernux.core.assets import AssetManager

        AssetManager.begin_asset_write_flush()
        return {"database": None, "catalog_started": False, "stage": "writes"}

    def _poll_asset_catalog_for_build(self, state):
        """Advance durability and the worker-backed scan between frames."""
        from Infernux.core.assets import AssetManager
        from Infernux.renderstack.discovery import discover_effect_features

        if not AssetManager.asset_writes_idle():
            state["stage"] = "writes"
            return None
        database = state.get("database")
        if database is None:
            # Feature discovery has to import Python provider modules on the
            # editor thread, but it now occurs after the modal was presented
            # and never competes with a synchronous filesystem flush.
            discover_effect_features()
            database = self.services.asset_database
            if database is None:
                raise RuntimeError("The editor asset database is unavailable")
            database.begin_refresh()
            state["database"] = database
            state["catalog_started"] = True
            state["stage"] = "scan"
            return None
        if database.refresh_pending and not database.try_commit_refresh():
            state["stage"] = "scan"
            return None
        state["stage"] = "index"
        database.flush_derived_index()
        index_path = str(getattr(database, "asset_index_path", "") or "")
        if not index_path or not os.path.isfile(index_path):
            raise RuntimeError(
                "The editor could not publish the current Library/AssetIndex.json"
            )
        from Infernux.engine.runtime_artifact_catalog import load_asset_index

        project_root = get_project_root()
        if not project_root:
            raise RuntimeError("No project root found")
        from Infernux.particle.artifact import ParticleArtifactRegistry

        try:
            ParticleArtifactRegistry.ensure_project_compiled(
                project_root,
                raise_on_error=True,
            )
        except Exception as exc:
            raise RuntimeError(f"Particle artifact compile failed: {exc}") from exc
        database.flush_derived_index()
        # Snapshot now. A later document transaction can invalidate
        # AssetIndex.json before the worker starts; the frozen entries are
        # the published catalog, not the live file.
        return {
            "path": index_path,
            "entries": load_asset_index(project_root),
        }

    def _do_build(self, *, run_after: bool) -> bool:
        if self._building:
            return False
        target = self._synchronize_build_target(persist=True)
        if target is None:
            self._build_error = "No Player build target is currently available"
            return False
        target_id = str(target.id)
        if run_after and not self._is_desktop_target(target_id):
            self._build_error = "Build And Run is available only for the current desktop target"
            return False
        self._building = True
        self._build_progress = 0.0
        self._build_message = "Starting build..."
        self._build_cancelled = False
        self._build_error = None
        self._build_output_dir = None
        self._cancel_event.clear()
        self._build_cancellation = None
        self._active_build_target = target_id
        from Infernux.engine.ui.engine_status import EngineStatus
        EngineStatus.set(
            self._build_message,
            -1.0,
            kind="activity",
            source="build",
            priority=20,
        )

        if not get_project_root():
            self._building = False
            self._build_error = "No project root found"
            EngineStatus.flash(
                self._build_error,
                0.0,
                duration=2.5,
                source="build",
                priority=20,
            )
            return False

        def _fail_preflight(exc: Exception) -> bool:
            self._building = False
            self._build_cancellation = None
            self._active_build_target = ""
            if isinstance(exc, BuildOutputDirectoryError):
                self._show_output_directory_error(exc)
            else:
                self._build_error = str(exc)
            EngineStatus.flash(
                self._build_error or "Build preparation failed",
                0.0,
                duration=3.0,
                kind="error",
                source="build",
                priority=20,
            )
            return False

        def _start_worker(request: BuildRequest):
            def _run():
                try:
                    service = BuildService(exporter_registry)
                    plan = service.create_plan(request)
                    result = service.execute(request, plan)
                    if not result.success:
                        self._build_error = self._build_failure_message(result)
                        return
                    self._build_output_dir = request.output_dir
                    if run_after:
                        self._launch_desktop_result(result)
                except BuildCancelled:
                    self._build_cancelled = True
                except BuildUnavailableError as exc:
                    self._build_error = "\n".join(
                        f"[{item.code}] {item.message}"
                        for item in exc.diagnostics
                    )
                except BuildOutputDirectoryError as exc:
                    self._show_output_directory_error(exc)
                except Exception as exc:
                    self._build_error = str(exc)
                finally:
                    self._build_cancellation = None
                    self._active_build_target = ""
                    self._building = False
                    if self._build_cancelled:
                        EngineStatus.flash(
                            "Build cancelled",
                            -1.0,
                            duration=2.0,
                            kind="warning",
                            source="build",
                            priority=20,
                        )
                    elif self._build_error:
                        EngineStatus.flash(
                            "Build failed",
                            0.0,
                            duration=3.0,
                            source="build",
                            priority=20,
                        )
                    else:
                        EngineStatus.flash(
                            "Build completed",
                            1.0,
                            duration=2.0,
                            source="build",
                            priority=20,
                        )

            threading.Thread(target=_run, daemon=True).start()

        def _prepare_and_start(catalog):
            try:
                request = self._make_build_request(catalog, target_id)
            except Exception as exc:
                return _fail_preflight(exc)
            _start_worker(request)
            return True

        # A modal is deliberately presented before catalog work begins.  It
        # makes the build boundary explicit and blocks unrelated authoring
        # commands while the immutable Player catalog is being created.
        from Infernux.engine.ui.build_preflight_progress import (
            BuildPreflightProgressService,
        )

        def _complete_preflight(ok: bool, result: object, message: str) -> None:
            if not ok:
                self._building = False
                self._build_cancellation = None
                self._active_build_target = ""
                self._build_cancelled = message == "Build preparation cancelled"
                if not self._build_cancelled:
                    self._build_error = message or "Build preparation failed"
                return
            if result in (None, "", {}):
                _fail_preflight(RuntimeError("The asset catalog was not published"))
                return
            _prepare_and_start(result)

        if not BuildPreflightProgressService.instance().begin(
            begin_scan=self._begin_asset_catalog_for_build,
            poll_scan=self._poll_asset_catalog_for_build,
            complete=_complete_preflight,
        ):
            self._building = False
            self._active_build_target = ""
            self._build_error = "Another editor transaction is already running"
            EngineStatus.flash(self._build_error, 0.0, duration=2.5, kind="warning")
            return False
        return True

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _add_scene(self, path: str):
        abs_path = resolved_path(path)
        if not abs_path.lower().endswith(".scene"):
            return
        root = get_project_root()
        assets_root = resolved_path(os.path.join(root, "Assets")) if root else ""
        if not root or not is_path_within(abs_path, assets_root, allow_root=False):
            Debug.log_warning(
                f"Build scene must be inside the project Assets folder: {path}"
            )
            return
        stored_path = relative_path(abs_path, root).replace("\\", "/")
        for existing in self._scenes:
            existing_path = resolved_path(
                existing if os.path.isabs(existing) else os.path.join(root, existing)
            )
            if same_path(existing_path, abs_path):
                return
        self._scenes.append(stored_path)
        self._save()
        Debug.log_internal(f"Added scene to build list: {os.path.basename(path)}")
