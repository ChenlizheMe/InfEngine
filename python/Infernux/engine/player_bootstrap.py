"""
PlayerBootstrap — minimal startup sequence for standalone game playback.

Replaces :class:`EditorBootstrap` with a stripped-down path that:
  1. Creates the Engine (no editor panels)
  2. Loads tag/layer settings
  3. Creates the dedicated PlayerRuntimeSession
  4. Enables the game camera
  5. Registers the fullscreen PlayerGUI (with optional splash sequence)
  6. Loads the first scene from BuildSettings.json (or a Supervisor-approved Debug validation scene)
  7. Leaves the scene loaded but not playing. The native window stays hidden
     during engine loading; Play starts after an optional project splash

No undo, no selection, no hierarchy, no inspector, no docking layout.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import TYPE_CHECKING, Dict, List, Optional

import Infernux.resources as _resources
from Infernux.lib import LogLevel
from Infernux.engine.path_utils import (
    is_path_within,
    resolved_path,
)
from Infernux.engine.player_log import write_player_log as _plog
from Infernux.debug import Debug

if TYPE_CHECKING:
    from Infernux.engine.engine import Engine
    from Infernux.engine.player_gui import PlayerGUI
    from Infernux.engine.player_service_graph import (
        PlayerRuntimeAssetCatalog,
        RuntimeProductManifest,
    )

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
        game_name: str = "",
        window_icon: str = "",
        window_resizable: bool = True,
    ):
        self.project_path = project_path
        self.engine_log_level = engine_log_level
        self.display_mode = display_mode
        self.window_width = window_width
        self.window_height = window_height
        self.splash_items = splash_items or []
        self.game_name = game_name
        self.window_icon = window_icon
        self.window_resizable = window_resizable
        self.engine: Optional[Engine] = None
        self.runtime_session = None
        self._player_gui: Optional[PlayerGUI] = None
        self._runtime_manifest: Optional[RuntimeProductManifest] = None
        self._runtime_catalog: Optional[PlayerRuntimeAssetCatalog] = None
        self._runtime_manifest_document: Optional[dict] = None
        self._runtime_package_root: str = ""

    # ── Public entry point ─────────────────────────────────────────────

    def run(self):
        """Execute all bootstrap phases and start the main loop."""
        startup_started = time.perf_counter()
        phase_started = startup_started

        def phase(name: str, callback) -> None:
            nonlocal phase_started
            callback()
            now = time.perf_counter()
            _plog(f"[Startup] {name}: {(now - phase_started) * 1000.0:.1f} ms")
            phase_started = now

        phase("force player mode", self._force_player_mode)
        phase("load runtime contract", self._load_runtime_contract)
        phase("initialize engine", self._init_engine)
        phase("preload project plugins", self._load_plugins)
        self._pump_startup_events()
        phase("load runtime asset catalog", self._load_runtime_asset_catalog)
        self._pump_startup_events()
        phase("create runtime managers", self._create_managers)
        self._pump_startup_events()
        phase("setup game camera", self._setup_game_camera)
        phase("register player GUI", self._register_player_gui)
        self._pump_startup_events()
        phase("schedule initial scene", self._load_initial_scene)
        # Do not activate Play here. The caller reveals the initialized native
        # window after bootstrap; PlayerGUI starts Play after any project
        # splash has finished.
        if self.engine is not None:
            phase("prepare runtime scripts", self.engine.prepare_startup_refresh)
        self._pump_startup_events()
        _plog(
            f"[Startup] bootstrap ready: "
            f"{(time.perf_counter() - startup_started) * 1000.0:.1f} ms"
        )

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
        engine_module = sys.modules.get("Infernux.engine.engine")
        if engine_module is not None:
            engine_module._PLAYER_MODE = "1"

    def _load_runtime_contract(self) -> None:
        """Load the build-authored runtime contract without source discovery."""
        self._validate_runtime_manifest()
        self._apply_runtime_policy()

    def _validate_runtime_manifest(self) -> None:
        """Validate the small product contract before creating the window."""
        from Infernux.engine.player_service_graph import (
            RuntimeProductManifest,
        )

        data_root = os.environ.get("_INFERNUX_PLAYER_DATA_ROOT", "").strip()
        manifest_path = os.path.join(
            data_root or self.project_path,
            "Player.inxmanifest",
        )
        if not os.path.isfile(manifest_path):
            raise RuntimeError("Player.inxmanifest is required")
        try:
            with open(manifest_path, "r", encoding="utf-8") as stream:
                manifest = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Player runtime manifest is unreadable: {manifest_path}") from exc

        runtime_manifest = RuntimeProductManifest.from_document(manifest)
        runtime_manifest.require_service("player_bootstrap")
        runtime_manifest.require_service("runtime_asset_catalog")
        product = manifest.get("product", {})
        if product.get("single_entry_point") is not True:
            raise RuntimeError("Player package must have exactly one executable entry point")
        layout = str(product.get("layout", ""))
        required_artifacts = {
            "single_executable_native_packages": ("Runtime.inxrt", "Content.inxpkg"),
            "platform_native_packages": ("Content.inxpkg",),
        }.get(layout)
        if required_artifacts is None:
            raise RuntimeError(f"Player package layout is unsupported: {layout or '<missing>'}")
        required_root = data_root or os.path.dirname(manifest_path)
        for artifact in required_artifacts:
            if not os.path.isfile(os.path.join(required_root, artifact)):
                raise RuntimeError(f"Player package artifact is missing: {artifact}")

        self._runtime_manifest = runtime_manifest
        self._runtime_manifest_document = manifest
        self._runtime_package_root = required_root

    def _load_runtime_asset_catalog(self) -> None:
        """Load the immutable asset catalog before revealing the game window."""
        from Infernux.engine.player_service_graph import PlayerRuntimeAssetCatalog
        from Infernux.engine.runtime_artifact_catalog import (
            CATALOG_SCHEMA,
            runtime_artifact_id,
        )

        runtime_manifest = self._runtime_manifest
        if runtime_manifest is None:
            raise RuntimeError("Player runtime manifest is unavailable")
        required_root = self._runtime_package_root or self.project_path

        catalog_path = os.path.join(required_root, "Library", "RuntimeAssetCatalog.json")
        try:
            with open(catalog_path, "r", encoding="utf-8") as stream:
                catalog = json.load(stream)
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Runtime asset catalog is unreadable: {catalog_path}") from exc
        if (
            not isinstance(catalog, dict)
            or catalog.get("$schema") != CATALOG_SCHEMA
            or set(catalog) != {"$schema", "player_host", "packages", "artifacts"}
        ):
            raise RuntimeError("Unsupported runtime asset catalog schema")

        packages = catalog.get("packages")
        artifacts = catalog.get("artifacts")
        if not isinstance(packages, list) or not isinstance(artifacts, list):
            raise RuntimeError("Runtime asset catalog is missing packages or artifacts")
        for package in packages:
            if not isinstance(package, dict):
                raise RuntimeError("Runtime asset catalog contains an invalid package entry")
            if set(package) != {
                "path", "archive_bytes", "file_count", "raw_bytes", "stored_bytes", "codec"
            }:
                raise RuntimeError("Runtime asset catalog package uses an unsupported schema")
            package_path = self._catalog_path(required_root, package.get("path"))
            if not package_path or not os.path.isfile(package_path):
                raise RuntimeError("Runtime asset catalog references a missing package")
            validated_bytes = self._boot_validated_archive_bytes(
                str(package.get("path", ""))
            )
            if validated_bytes is None:
                raise RuntimeError(
                    "Runtime asset catalog package has no boot-validated native manifest: "
                    + str(package.get("path", ""))
                )
            if int(package.get("archive_bytes", -1)) != validated_bytes:
                raise RuntimeError("Runtime asset catalog package size disagrees with native manifest")
            if validated_bytes != os.path.getsize(package_path):
                raise RuntimeError("Runtime asset catalog package size mismatch")

        artifact_ids = set()
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise RuntimeError("Runtime asset catalog contains an invalid artifact entry")
            allowed_artifact_fields = {
                "runtime_artifact_id", "logical_type", "payload_kind", "package",
                "runtime_path", "content_bytes", "dependencies",
                "unresolved_dependencies",
            }
            if frozenset(artifact) not in {
                frozenset(allowed_artifact_fields),
                frozenset(allowed_artifact_fields | {"source_asset", "asset_guid"}),
            }:
                raise RuntimeError("Runtime asset catalog artifact uses an unsupported schema")
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
            asset_guid = artifact.get("asset_guid")
            if asset_guid is not None:
                if not isinstance(asset_guid, str) or not asset_guid:
                    raise RuntimeError("Runtime asset catalog contains an invalid asset GUID")
        for artifact in artifacts:
            for dependency in artifact.get("dependencies", []):
                if dependency not in artifact_ids:
                    raise RuntimeError("Runtime asset catalog contains an unknown dependency")

        records_path = os.path.join(
            self.project_path, "Library", "RuntimeAssetRecords.json"
        )
        try:
            with open(records_path, "r", encoding="utf-8") as records_stream:
                asset_records = json.load(records_stream)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError("Player runtime asset records are unreadable") from exc
        if (
            not isinstance(asset_records, dict)
            or asset_records.get("$schema") != "infernux.runtime_asset_records"
            or set(asset_records) != {"$schema", "entries"}
            or not isinstance(asset_records.get("entries"), list)
        ):
            raise RuntimeError("Player runtime asset records use an unsupported schema")
        runtime_catalog = PlayerRuntimeAssetCatalog.from_documents(
            self.project_path,
            catalog,
            asset_records,
        )

        type_registry_path = os.path.join(
            self.project_path, "Library", "RuntimeTypeRegistry.json"
        )
        runtime_manifest.require_service("runtime_type_registry")
        try:
            from Infernux.engine.runtime_type_registry import (
                install_runtime_type_registry,
            )

            install_runtime_type_registry(type_registry_path)
        except (OSError, json.JSONDecodeError, RuntimeError, ValueError) as exc:
            raise RuntimeError(
                f"Player runtime type registry is invalid: {type_registry_path}"
            ) from exc
        self._runtime_catalog = runtime_catalog

    def _apply_runtime_policy(self) -> None:
        """Apply the validated product policy once, before Engine creation."""
        from Infernux.engine.player_service_graph import (
            LoggingPolicy,
            RuntimeFlavor,
        )

        manifest = self._runtime_manifest
        if manifest is None:
            raise RuntimeError("Player runtime manifest is not loaded")
        expected_debug = manifest.flavor is RuntimeFlavor.PLAYER_DEBUG
        declared_debug = os.environ.get("_INFERNUX_PLAYER_DEBUG_BUILD", "").strip()
        if declared_debug not in {"0", "1"} or (declared_debug == "1") != expected_debug:
            raise RuntimeError("Player build flavor disagrees with RuntimeManifest")

        control_environment = (
            "_INFERNUX_PLAYER_CONTROL_FILE",
            "_INFERNUX_PLAYER_RESPONSE_FILE",
            "_INFERNUX_PLAYER_CONTROL_TOKEN",
            "_INFERNUX_PLAYER_ARTIFACT_ROOT",
        )
        if not expected_debug and any(
            os.environ.get(name, "").strip() for name in control_environment
        ):
            raise RuntimeError("PlayerRelease cannot enable the debug control channel")

        has_splash = manifest.service_graph.contains("splash_player")
        if has_splash != bool(self.splash_items):
            raise RuntimeError("Player splash configuration disagrees with the service graph")

        data_root = os.environ.get("_INFERNUX_PLAYER_DATA_ROOT", "").strip()
        parallel_module = os.path.join(
            data_root or self.project_path,
            "Modules",
            "Parallel.inxmod",
        )
        if os.path.isfile(parallel_module) != manifest.features.parallel:
            raise RuntimeError("Parallel.inxmod presence disagrees with RuntimeManifest")

        self.engine_log_level = (
            LogLevel.Debug
            if manifest.policy.logging is LoggingPolicy.DEBUG
            else LogLevel.Info
        )

    @staticmethod
    def _boot_validated_archive_bytes(package_path: str) -> Optional[int]:
        """Read the package size produced by the boot-time native manifest pass."""

        from Infernux.engine.runtime_artifact_catalog import package_kind

        kind = package_kind(package_path).upper()
        raw_bytes = os.environ.get(f"_INFERNUX_PLAYER_{kind}_ARCHIVE_BYTES", "")
        try:
            archive_bytes = int(raw_bytes)
        except (TypeError, ValueError):
            return None
        if archive_bytes < 0:
            return None
        return archive_bytes

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
        if self._runtime_manifest is None or self._runtime_catalog is None:
            return None
        self._runtime_manifest.require_service("runtime_asset_catalog")
        return self._runtime_catalog.resolve_scene(scene_reference)

    def _init_engine(self):
        if self._runtime_manifest is None:
            raise RuntimeError("Player runtime manifest is not loaded")
        self._runtime_manifest.require_service("engine")
        from Infernux.engine.engine import Engine

        self.engine = Engine(self.engine_log_level)
        self.engine._set_application_role("player")

        if getattr(self.engine, "_resources_manager", None) is not None:
            raise RuntimeError(
                "Player startup created the editor ResourcesManager/file watcher"
            )

        # Publish window chrome before native Init() so the hidden SDL window
        # can be revealed in its final state after bootstrap.
        if self.display_mode == "fullscreen_borderless":
            os.environ["_INFERNUX_PLAYER_FULLSCREEN"] = "1"
        else:
            os.environ.pop("_INFERNUX_PLAYER_FULLSCREEN", None)
        title = self.game_name or os.path.basename(resolved_path(self.project_path))
        if title:
            os.environ["_INFERNUX_PLAYER_WINDOW_TITLE"] = title
        if self.window_icon:
            os.environ["_INFERNUX_PLAYER_WINDOW_ICON"] = self.window_icon

        # For windowed mode, use the requested size;
        # for fullscreen borderless, start at a default size — native
        # Init() applies borderless fullscreen from the environment.
        if self.display_mode == "windowed":
            w, h = self.window_width, self.window_height
        else:
            w, h = 1920, 1080

        self.engine.init_renderer(
            width=w, height=h, project_path=self.project_path
        )
        native = self.engine.get_native_engine()
        if native is not None:
            try:
                timings = dict(native.startup_phase_timings_ms)
                _plog(
                    "[Startup.Native] "
                    + ", ".join(
                        f"{name}={float(duration):.1f}ms"
                        for name, duration in sorted(timings.items())
                    )
                )
            except Exception as exc:
                _plog(f"[Startup.Native] diagnostics unavailable: {exc}")
        self._pump_startup_events()

        # Explicitly tell C++ we are in player mode — no scene view rendering.
        # The C++ default is m_sceneViewVisible=true; without this call the
        # renderer would execute both scene and game render graphs every frame.
        self.engine.set_scene_view_visible(False)

        # Skip DockSpace / DockBuilder overhead — the player only registers a
        # single full-screen renderable, so the editor docking system is waste.
        self.engine.set_gui_player_mode(True)

        self.engine.set_gui_font(_resources.engine_font_path, 18)
        self._pump_startup_events()

    def _pump_startup_events(self) -> None:
        """Keep a visible Player window marked responsive during bootstrap."""
        engine = self.engine
        if engine is None:
            return
        pump = getattr(engine, "pump_events", None)
        if callable(pump) and pump() is False:
            raise RuntimeError("Player startup cancelled")

    def _load_plugins(self) -> None:
        from Infernux.plugins import PluginManager

        project_path = str(getattr(self, "project_path", "") or "")
        if not project_path:
            return
        self.plugin_manager = PluginManager.startup(
            project_path,
            engine=self.engine,
            runtime=True,
        )

    def _create_managers(self):
        if self.engine is None or self._runtime_manifest is None:
            raise RuntimeError("Player Engine or RuntimeManifest is unavailable")
        if self._runtime_catalog is None:
            raise RuntimeError("Player RuntimeAssetCatalog is unavailable")
        self._runtime_manifest.require_service("runtime_execution_scheduler")
        self._runtime_manifest.require_service("player_scene_service")
        self._runtime_manifest.require_service("player_runtime_session")
        self.runtime_session = self.engine.get_player_runtime()
        if self.runtime_session is None:
            raise RuntimeError("Player startup did not create a PlayerRuntimeSession")
        self.runtime_session.configure_runtime_contract(
            self._runtime_manifest,
            self._runtime_catalog,
        )
        leaked_services = [
            name
            for name in ("_resources_manager", "_play_mode_manager")
            if getattr(self.engine, name, None) is not None
        ]
        if leaked_services:
            raise RuntimeError(
                "Player Engine created Editor services: " + ", ".join(leaked_services)
            )


    def _setup_game_camera(self):
        if self.engine is None or self._runtime_manifest is None:
            raise RuntimeError("Player Engine is unavailable")
        self._runtime_manifest.require_service("engine")
        self.engine.set_game_camera_enabled(True)


    def _register_player_gui(self):
        if self.engine is None or self._runtime_manifest is None:
            raise RuntimeError("Player Engine or RuntimeManifest is unavailable")
        self._runtime_manifest.require_service("player_gui")
        from Infernux.engine.player_gui import PlayerGUI

        control_channel = None
        if self._runtime_manifest.service_graph.contains("player_control_debug"):
            from Infernux.engine.player_control import PlayerControlChannel

            control_channel = PlayerControlChannel.from_environment()
        self._player_gui = PlayerGUI(
            self.engine,
            splash_items=self.splash_items,
            data_root=self.project_path,
            control_channel=control_channel,
            activate_play=self._activate_initial_scene_for_play,
        )
        self.engine.register_gui("player_gui", self._player_gui)

    def _load_initial_scene(self):
        if self._runtime_manifest is None:
            raise RuntimeError("Player runtime manifest is not loaded")
        self._runtime_manifest.require_service("player_scene_service")
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
            # A packaged Player contains cooked scene artifacts rather than the
            # source ``Assets/*.scene`` documents.  The Supervisor already
            # constrains this value to BuildManifest scenes; the immutable
            # RuntimeAssetCatalog is the final authority inside the Player.
            if self._resolve_runtime_scene(requested_scene) is not None:
                first_scene = requested_scene
                Debug.log_internal(
                    "Loaded Supervisor validation scene: "
                    f"{os.path.basename(requested_scene)}"
                )
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
            detail = (
                str(getattr(self.runtime_session, "last_scene_error", ""))
                if self.runtime_session is not None
                else "Player runtime session is unavailable"
            )
            raise RuntimeError(
                f"Failed to load initial scene: {first_scene}: "
                f"{detail or 'scene transaction rejected the document'}"
            )

    def _enter_play_mode(self):
        """Activate the loaded Player scene once the window can present it."""
        if not self._activate_initial_scene_for_play():
            raise RuntimeError("Cannot activate the initial Player scene")

    def _activate_initial_scene_for_play(self):
        """Start the freshly loaded scene without rebuilding it a second time."""
        if self.runtime_session is None:
            Debug.log_error("No PlayerRuntimeSession available")
            return
        activated = self.runtime_session.activate()
        scene_service = getattr(self.runtime_session, "scene_service", None)
        active_scene = str(getattr(scene_service, "active_scene_path", "") or "")
        scheduler = getattr(self.runtime_session, "execution_scheduler", None)
        if scheduler is not None:
            try:
                snapshot = scheduler.phase_plan_snapshot()
                plans = {
                    phase: len(snapshot.get(phase, ()))
                    for phase in ("fixed_update", "update", "late_update")
                }
                _plog(
                    "[Startup] Play activation: "
                    f"active={bool(activated)}, scene={active_scene!r}, "
                    f"lifecycle_plans={plans}"
                )
            except Exception as exc:
                _plog(f"[Startup] lifecycle plan diagnostics failed: {exc}")
        return activated
