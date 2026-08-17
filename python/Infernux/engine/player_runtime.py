"""Minimal runtime session used by the packaged Player."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Optional

from Infernux.debug import Debug
from Infernux.engine.player_scene import PlayerSceneService
from Infernux.timing import Time

if TYPE_CHECKING:
    from Infernux.engine.player_service_graph import (
        PlayerRuntimeAssetCatalog,
        RuntimeProductManifest,
    )


class PlayerRuntimeSession:
    """Own the standalone Player scene without editor document services."""

    def __init__(
        self,
        *,
        asset_database: Any = None,
        native_engine: Any = None,
        scheduler: Any = None,
        scene_service: Any = None,
    ):
        self._asset_database = asset_database
        self._native_engine = native_engine
        if scheduler is None:
            from Infernux.components._component_lifecycle import RuntimeExecutionScheduler
            scheduler = RuntimeExecutionScheduler(name="player")
        self._execution_scheduler = scheduler
        self._scene_service = scene_service or PlayerSceneService(
            asset_database=asset_database
        )
        self._scene_service_installed = False
        self._runtime_manifest: Optional[RuntimeProductManifest] = None
        self._runtime_catalog: Optional[PlayerRuntimeAssetCatalog] = None
        self._state = "stopped"
        self._last_frame_time = time.time()

    @property
    def is_playing(self) -> bool:
        return self._state == "playing"

    @property
    def is_paused(self) -> bool:
        return self._state == "paused"

    @property
    def state(self) -> str:
        return self._state

    @property
    def active_scene_path(self) -> Optional[str]:
        return self._scene_service.active_scene_path

    @property
    def last_scene_error(self) -> str:
        return str(getattr(self._scene_service, "last_error", ""))

    @property
    def execution_scheduler(self) -> Any:
        """Return the shared on-demand phase-plan service for diagnostics."""
        return self._execution_scheduler

    @property
    def runtime_manifest(self) -> Optional[RuntimeProductManifest]:
        return self._runtime_manifest

    def configure_runtime_contract(
        self,
        runtime_manifest: RuntimeProductManifest,
        runtime_catalog: PlayerRuntimeAssetCatalog,
    ) -> None:
        """Bind the build-authored Player product before loading a scene."""
        from Infernux.engine.player_service_graph import (
            PlayerRuntimeAssetCatalog,
            RuntimeProductManifest,
        )

        if not isinstance(runtime_manifest, RuntimeProductManifest):
            raise TypeError("PlayerRuntimeSession requires a RuntimeProductManifest")
        if not isinstance(runtime_catalog, PlayerRuntimeAssetCatalog):
            raise TypeError("PlayerRuntimeSession requires a RuntimeAssetCatalog")
        if not runtime_manifest.flavor.is_player:
            raise RuntimeError("EditorDevelopment cannot configure PlayerRuntimeSession")
        for service_id in (
            "runtime_asset_catalog",
            "runtime_type_registry",
            "player_scene_service",
            "player_runtime_session",
        ):
            runtime_manifest.require_service(service_id)
        if self._runtime_manifest is not None:
            if (
                self._runtime_manifest is runtime_manifest
                and self._runtime_catalog is runtime_catalog
            ):
                return
            raise RuntimeError("PlayerRuntimeSession runtime contract is already configured")
        if self._state != "stopped":
            raise RuntimeError("PlayerRuntimeSession cannot be configured after activation")
        bind_catalog = getattr(self._scene_service, "bind_runtime_catalog", None)
        if not callable(bind_catalog):
            raise RuntimeError("Player scene service cannot bind RuntimeAssetCatalog")
        bind_catalog(runtime_catalog)
        from Infernux.engine.project_context import set_runtime_asset_resolver

        set_runtime_asset_resolver(runtime_catalog.resolve_asset)
        self._runtime_manifest = runtime_manifest
        self._runtime_catalog = runtime_catalog
        self._validate_player_boundary()

    def _validate_player_boundary(self) -> None:
        forbidden = (
            "_scene_backup",
            "_scene_dirty_backup",
            "_resources_manager",
            "_script_compiler",
            "_selection_manager",
            "_undo_manager",
            "_preview_service",
            "_import_coordinator",
        )
        leaked = [name for name in forbidden if hasattr(self, name)]
        if leaked:
            raise RuntimeError(
                "PlayerRuntimeSession contains Editor state: " + ", ".join(leaked)
            )

    def load_scene(self, path: str) -> bool:
        """Load a scene through the runtime transaction path."""
        if self._runtime_manifest is None or self._runtime_catalog is None:
            Debug.log_error("Player scene load rejected: runtime contract is not configured")
            return False
        return bool(self._scene_service.load_initial(path))

    def activate(self) -> bool:
        """Start the loaded scene without creating an editor play snapshot."""
        if self._runtime_manifest is None or self._runtime_catalog is None:
            Debug.log_error("Player activation rejected: runtime contract is not configured")
            return False
        from Infernux.lib import SceneManager
        from Infernux.renderstack.render_stack import RenderStack

        scene = SceneManager.instance().get_active_scene()
        if scene is None:
            Debug.log_warning("Player cannot start without an active scene")
            return False
        Time._reset()
        self._last_frame_time = time.time()
        RenderStack._active_instance = None
        from Infernux.scene import SceneManager as RuntimeSceneManager

        # Install the packaged scene owner before native ``play()`` dispatches
        # Awake/Start.  Startup scripts are allowed to prepare or load another
        # BuildSettings scene immediately; those logical authoring paths only
        # become loadable through the RuntimeAssetCatalog-backed service.
        RuntimeSceneManager.install_runtime_service(self._scene_service)
        self._scene_service_installed = True
        try:
            scene.set_playing(True)
            self._refresh_loaded_scene(scene)
            SceneManager.instance().play()
        except Exception:
            RuntimeSceneManager.remove_runtime_service(self._scene_service)
            self._scene_service_installed = False
            raise
        self._state = "playing"
        Debug.log_internal("Player runtime session activated")
        return True

    def tick(self, external_delta_time: Optional[float] = None) -> float:
        """Advance one Player frame at the safe pre-scene boundary.

        Scene callbacks can queue a runtime load while the native scene is
        being iterated. The editor advances that transaction before the next
        native scene tick; Player must do the same without constructing any
        editor-only scene services.
        """
        if not self.is_playing:
            return 0.0
        self._scene_service.process_pending_load()

        current_time = time.time()
        delta_time = (
            current_time - self._last_frame_time
            if external_delta_time is None
            else float(external_delta_time)
        )
        self._last_frame_time = current_time
        Time._tick(delta_time)
        if self._native_engine is not None:
            try:
                Time._game_delta_time = (
                    self._native_engine.get_game_only_frame_ms() / 1000.0
                )
            except Exception as exc:
                Debug.log_suppressed("PlayerRuntimeSession.read_game_only_frame_ms", exc)
        return delta_time

    def shutdown(self) -> None:
        """Stop runtime callbacks without restoring or saving editor state."""
        if self._state != "stopped":
            try:
                from Infernux.lib import SceneManager

                SceneManager.instance().stop()
            except Exception as exc:
                Debug.log_suppressed("PlayerRuntimeSession.stop_scene", exc)
        try:
            from Infernux.components.component import InxComponent

            for comp_list in list(InxComponent._active_instances.values()):
                for component in list(comp_list):
                    try:
                        component._call_on_destroy()
                    except Exception as exc:
                        Debug.log_suppressed(
                            f"PlayerRuntimeSession.on_destroy[{type(component).__name__}]",
                            exc,
                        )
            InxComponent._clear_all_instances()
        except Exception as exc:
            Debug.log_suppressed("PlayerRuntimeSession.clear_components", exc)
        self._execution_scheduler.clear()
        self._scene_service.cancel_pending_load()
        if self._scene_service_installed:
            try:
                from Infernux.scene import SceneManager as RuntimeSceneManager

                RuntimeSceneManager.remove_runtime_service(self._scene_service)
            except Exception as exc:
                Debug.log_suppressed("PlayerRuntimeSession.remove_scene_service", exc)
            self._scene_service_installed = False
        from Infernux.engine.project_context import set_runtime_asset_resolver

        set_runtime_asset_resolver(None)
        self._state = "stopped"

    @staticmethod
    def _refresh_loaded_scene(scene: Any) -> None:
        try:
            from Infernux.components.builtin.sprite_renderer import SpriteRenderer

            SpriteRenderer.init_all_in_scene(scene)
        except Exception as exc:
            Debug.log_internal(f"Player SpriteRenderer init: {exc}")
