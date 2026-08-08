"""
PlayerBootstrap — minimal startup sequence for standalone game playback.

Replaces :class:`EditorBootstrap` with a stripped-down path that:
  1. Creates the Engine (no editor panels)
  2. Loads tag/layer settings
  3. Creates the dedicated PlayerRuntimeSession
  4. Enables the game camera
  5. Registers the fullscreen PlayerGUI (with optional splash sequence)
  6. Loads the first scene from BuildSettings.json (or a Supervisor-approved Debug validation scene)
  7. Enters play mode
  8. Runs the main loop

No undo, no selection, no hierarchy, no inspector, no docking layout.
"""

from __future__ import annotations

import logging
import hashlib
import json
import os
from typing import Dict, List, Optional

import Infernux.resources as _resources
from Infernux.engine.engine import Engine, LogLevel
from Infernux.engine.player_gui import PlayerGUI
from Infernux.engine.path_utils import (
    is_path_within,
    relative_path,
    resolved_path,
    safe_path as _safe_path,
)
from Infernux.engine.runtime_artifact_catalog import (
    CATALOG_SCHEMA,
    CATALOG_VERSION,
    package_kind,
    runtime_artifact_id,
)
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
        self.runtime_session = None
        self._player_gui: Optional[PlayerGUI] = None
        self._runtime_catalog: Optional[Dict] = None

    # ── Public entry point ─────────────────────────────────────────────

    def run(self):
        """Execute all bootstrap phases and start the main loop."""
        self._force_player_mode()
        self._ensure_project_requirements()
        self._init_engine()
        self._create_managers()
        self._setup_game_camera()
        self._register_player_gui()
        self._load_initial_scene()
        self._enter_play_mode()

    @staticmethod
    def _force_player_mode() -> None:
        """Establish the no-editor boundary before constructing ``Engine``.

        Packaged boots set this variable before importing Infernux. A
        development Player may enter through an already imported package, so
        update the engine module's cached mode flag as well. This prevents
        ``Engine.init_renderer`` from constructing the editor
        ``ResourcesManager`` and its file watcher in either path.
        """
        os.environ["_INFERNUX_PLAYER_MODE"] = "1"
        try:
            from Infernux.engine import engine as engine_module
            engine_module._PLAYER_MODE = "1"
        except Exception as exc:
            raise RuntimeError("Unable to establish Infernux Player mode") from exc

    def _ensure_project_requirements(self):
        self._validate_runtime_manifest()
        try:
            from Infernux.engine.project_requirements import ensure_project_requirements

            ensure_project_requirements(self.project_path, auto_install=False)
        except ImportError:
            # Optional packaging helper missing — runtime continues without
            # auto-install; happens in slim distribution variants.
            pass

    def _validate_runtime_manifest(self) -> None:
        """Require the current Player.inxmanifest before engine startup."""
        data_root = os.environ.get("_INFERNUX_PLAYER_DATA_ROOT", "").strip()
        manifest_path = os.path.join(
            data_root or self.project_path,
            "Player.inxmanifest",
        )
        if not os.path.isfile(manifest_path):
            raise RuntimeError(
                "Player.inxmanifest is missing; legacy Player runtime manifests "
                "are not supported"
            )
        try:
            with open(manifest_path, "r", encoding="utf-8") as stream:
                manifest = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Player runtime manifest is unreadable: {manifest_path}") from exc

        if manifest.get("$schema") != "infernux.player_runtime_manifest":
            raise RuntimeError("Unsupported Player runtime manifest schema")
        audit = manifest.get("audit", {})
        if audit.get("passed") is not True:
            raise RuntimeError("Player runtime package audit did not pass")
        if audit.get("legacy_zip_files") or audit.get("legacy_inxpack_files"):
            raise RuntimeError("Legacy Player containers are not supported")
        if audit.get("player_host_gap") or audit.get("library_artifact_gap"):
            raise RuntimeError("Player host/library artifact verification is incomplete")
        product = manifest.get("product", {})
        if product.get("single_entry_point") is not True:
            raise RuntimeError("Player package must have exactly one executable entry point")
        required_root = data_root or os.path.dirname(manifest_path)
        for artifact in ("Runtime.inxrt", "Content.inxpkg"):
            if not os.path.isfile(os.path.join(required_root, artifact)):
                raise RuntimeError(f"Player package artifact is missing: {artifact}")

        catalog_path = os.path.join(required_root, "Library", "RuntimeAssetCatalog.json")
        try:
            with open(catalog_path, "r", encoding="utf-8") as stream:
                catalog = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Runtime asset catalog is unreadable: {catalog_path}") from exc
        if catalog.get("$schema") != CATALOG_SCHEMA:
            raise RuntimeError("Unsupported runtime asset catalog schema")
        if catalog.get("catalog_version") != CATALOG_VERSION:
            raise RuntimeError("Unsupported runtime asset catalog version")

        expected_catalog_hash = str(
            audit.get("runtime_asset_catalog_sha256", "")
        ).casefold()
        actual_catalog_hash = self._sha256_file(catalog_path)
        if expected_catalog_hash and expected_catalog_hash != actual_catalog_hash:
            raise RuntimeError("Runtime asset catalog checksum does not match Player manifest")

        packages = catalog.get("packages")
        artifacts = catalog.get("artifacts")
        if not isinstance(packages, list) or not isinstance(artifacts, list):
            raise RuntimeError("Runtime asset catalog is missing packages or artifacts")
        for package in packages:
            if not isinstance(package, dict):
                raise RuntimeError("Runtime asset catalog contains an invalid package entry")
            package_path = self._catalog_path(required_root, package.get("path"))
            if not package_path or not os.path.isfile(package_path):
                raise RuntimeError("Runtime asset catalog references a missing package")
            validated = self._validated_archive_summary(str(package.get("path", "")))
            if validated is None:
                raise RuntimeError(
                    "Runtime asset catalog package has no boot-validated native manifest"
                )
            validated_hash, validated_bytes = validated
            if int(package.get("archive_bytes", -1)) != validated_bytes:
                raise RuntimeError("Runtime asset catalog package size disagrees with native manifest")
            if str(package.get("archive_sha256", "")).casefold() != validated_hash:
                raise RuntimeError("Runtime asset catalog package checksum disagrees with native manifest")
            if validated_bytes != os.path.getsize(package_path):
                raise RuntimeError("Runtime asset catalog package size mismatch")

        artifact_ids = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise RuntimeError("Runtime asset catalog contains an invalid artifact entry")
            artifact_id = artifact.get("runtime_artifact_id")
            package = artifact.get("package")
            runtime_path = artifact.get("runtime_path")
            if not all(isinstance(value, str) and value for value in (artifact_id, package, runtime_path)):
                raise RuntimeError("Runtime asset catalog artifact identity is incomplete")
            if artifact_id != runtime_artifact_id(package, runtime_path):
                raise RuntimeError("Runtime asset catalog contains an unstable artifact ID")
            if artifact_id in artifact_ids:
                raise RuntimeError("Runtime asset catalog contains duplicate artifact IDs")
            artifact_ids.add(artifact_id)
            digest = str(artifact.get("content_sha256", ""))
            if len(digest) != 64 or any(char not in "0123456789abcdefABCDEF" for char in digest):
                raise RuntimeError("Runtime asset catalog contains an invalid artifact checksum")
        for artifact in artifacts:
            for dependency in artifact.get("dependencies", []):
                if dependency not in artifact_ids:
                    raise RuntimeError("Runtime asset catalog contains an unknown dependency")
        self._runtime_catalog = catalog

    @staticmethod
    def _sha256_file(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _validated_archive_summary(package_path: str) -> Optional[tuple[str, int]]:
        """Read the summary produced by the boot-time native manifest pass."""

        kind = package_kind(package_path).upper()
        digest = os.environ.get(f"_INFERNUX_PLAYER_{kind}_ARCHIVE_SHA256", "").casefold()
        raw_bytes = os.environ.get(f"_INFERNUX_PLAYER_{kind}_ARCHIVE_BYTES", "")
        try:
            archive_bytes = int(raw_bytes)
        except (TypeError, ValueError):
            return None
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            return None
        if archive_bytes < 0:
            return None
        return digest, archive_bytes

    @staticmethod
    def _catalog_path(data_root: str, value: object) -> Optional[str]:
        if not isinstance(value, str) or not value:
            return None
        normalized = value.replace("\\", "/")
        root_name = os.path.basename(resolved_path(data_root)).replace("\\", "/")
        prefix = root_name + "/"
        if not normalized.startswith(prefix):
            return None
        candidate = resolved_path(os.path.join(data_root, normalized[len(prefix):]))
        return candidate if is_path_within(candidate, data_root, allow_root=False) else None

    def _resolve_runtime_scene(self, scene_reference: str) -> Optional[str]:
        if self._runtime_catalog is None:
            return None
        normalized = scene_reference.replace("\\", "/").lstrip("./")
        if os.path.isabs(scene_reference):
            candidate = resolved_path(scene_reference)
            if not is_path_within(candidate, self.project_path, allow_root=False):
                return None
            normalized = relative_path(candidate, self.project_path)
        for artifact in self._runtime_catalog.get("artifacts", []):
            if not isinstance(artifact, dict) or artifact.get("logical_type") != "scene":
                continue
            if package_kind(str(artifact.get("package", ""))) != "content":
                continue
            if artifact.get("runtime_path") == normalized:
                candidate = resolved_path(os.path.join(self.project_path, normalized))
                if is_path_within(candidate, self.project_path, allow_root=False) and os.path.isfile(candidate):
                    return candidate
        return None

    def _init_engine(self):
        self.engine = Engine(self.engine_log_level)
        self.engine._set_application_role("player")

        if getattr(self.engine, "_resources_manager", None) is not None:
            raise RuntimeError(
                "Player startup created the editor ResourcesManager/file watcher"
            )

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

        self.engine.set_gui_font(_resources.engine_font_path, 18)



    def _create_managers(self):
        self.runtime_session = self.engine.get_player_runtime()
        if self.runtime_session is None:
            raise RuntimeError("Player startup did not create a PlayerRuntimeSession")


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
        catalog_scene = self._resolve_runtime_scene(first_scene)
        if catalog_scene is None:
            raise RuntimeError(
                f"Build scene is not reachable through RuntimeAssetCatalog: {first_scene}"
            )
        first_scene = catalog_scene

        if not os.path.isfile(first_scene):
            raise RuntimeError(f"First scene file not found: {first_scene}")

        if self.runtime_session is None or not self.runtime_session.load_scene(first_scene):
            raise RuntimeError(f"Failed to load initial scene: {first_scene}")

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
        if self.runtime_session is None:
            Debug.log_error("No PlayerRuntimeSession available")
            return
        return self.runtime_session.activate()
