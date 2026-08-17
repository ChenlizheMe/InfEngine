"""Player-only scene ownership and deferred runtime loading."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Optional

from Infernux.debug import Debug
from Infernux.engine.path_utils import resolved_path
from Infernux.engine.runtime_scene_transaction import SceneDocumentTransaction

if TYPE_CHECKING:
    from Infernux.engine.player_service_graph import PlayerRuntimeAssetCatalog


class PlayerSceneService:
    """Own Player scene transactions without editor document services."""

    def __init__(self, *, asset_database: Any = None) -> None:
        self._asset_database = asset_database
        self._runtime_catalog: Any = None
        self._active_scene_path: Optional[str] = None
        self._pending_scene_path: Optional[str] = None
        self._transaction: Optional[SceneDocumentTransaction] = None
        self._transaction_path: Optional[str] = None
        self._wait_for_ready = False
        self._hold_for_activation = False
        self._request_generation = 0
        self._transaction_generation = 0
        self._last_error = ""

    @property
    def active_scene_path(self) -> Optional[str]:
        return self._active_scene_path

    @property
    def is_load_pending(self) -> bool:
        return self._pending_scene_path is not None or self._transaction is not None

    @property
    def last_error(self) -> str:
        return self._last_error

    @property
    def is_prepared(self) -> bool:
        return bool(
            self._hold_for_activation
            and self._transaction is not None
            and self._transaction.status == "ready_to_commit"
        )

    def bind_runtime_catalog(self, runtime_catalog: PlayerRuntimeAssetCatalog) -> None:
        """Bind the immutable catalog before any Player scene is loaded."""
        from Infernux.engine.player_service_graph import PlayerRuntimeAssetCatalog

        if not isinstance(runtime_catalog, PlayerRuntimeAssetCatalog):
            raise TypeError("PlayerSceneService requires a RuntimeAssetCatalog")
        if self._runtime_catalog is not None and self._runtime_catalog is not runtime_catalog:
            raise RuntimeError("PlayerSceneService runtime catalog is already bound")
        if self.is_load_pending or self._active_scene_path is not None:
            raise RuntimeError("PlayerSceneService catalog cannot change after scene loading")
        self._runtime_catalog = runtime_catalog

    def load_initial(self, path: str) -> bool:
        """Synchronously load the first Player scene before activation."""
        target = self._validated_scene_path(path)
        if target is None:
            self._last_error = f"scene file is unavailable: {path}"
            return False
        self._last_error = ""
        transaction = self._new_transaction(target)
        if not transaction.run_to_completion(raise_on_failure=False):
            self._last_error = transaction.error or "scene transaction failed"
            Debug.log_error(
                f"Player scene load failed for '{target}': {self._last_error}"
            )
            return False
        self._publish_completed_scene(target, start_for_play=False)
        return True

    def request_load(self, path: str) -> bool:
        """Queue a scene replacement for the next safe Player frame boundary."""
        target = self._validated_scene_path(path)
        if target is None:
            self._last_error = f"scene file is unavailable: {path}"
            return False
        generation = self._begin_load_request()
        if generation is None:
            return False
        self._last_error = ""
        self._pending_scene_path = target
        Debug.log_internal(
            f"Player scene load queued: {os.path.basename(target)}"
        )
        return True

    def request_prepared_load(
        self, path: str, *, hold_for_activation: bool = False
    ) -> bool:
        """Start preparing a replacement now and publish it when ready."""
        target = self._validated_scene_path(path)
        if target is None:
            self._last_error = f"scene file is unavailable: {path}"
            return False
        generation = self._begin_load_request()
        if generation is None:
            return False
        self._last_error = ""
        transaction = self._new_transaction(target)
        try:
            transaction.start()
        except Exception as exc:
            self._last_error = str(exc) or type(exc).__name__
            Debug.log_error(f"Player scene preparation could not start: {exc}")
            return False
        self._transaction = transaction
        self._transaction_path = target
        self._transaction_generation = generation
        self._wait_for_ready = True
        self._hold_for_activation = bool(hold_for_activation)
        Debug.log_internal(
            f"Player scene background preparation started: {os.path.basename(target)}"
        )
        return True

    def activate_prepared_load(self) -> bool:
        """Release a held prepared transaction for its owner-thread commit."""
        if not self.is_prepared:
            return False
        self._hold_for_activation = False
        return True

    def process_pending_load(self) -> None:
        """Advance deferred loading, committing a prepared scene when ready."""
        if self._transaction is None:
            target = self._pending_scene_path
            if target is None:
                return
            self._pending_scene_path = None
            transaction = self._new_transaction(target)
            try:
                transaction.start()
            except Exception as exc:
                self._last_error = str(exc) or type(exc).__name__
                Debug.log_error(f"Player scene load could not start: {exc}")
                return
            self._transaction = transaction
            self._transaction_path = target
            self._transaction_generation = self._request_generation
            self._wait_for_ready = False
            return

        transaction = self._transaction
        if self._transaction_generation != self._request_generation:
            if not transaction.is_complete:
                transaction.cancel()
            self._clear_pending_load()
            return
        if self._hold_for_activation and transaction.status == "ready_to_commit":
            return
        # Keep Player loading genuinely asynchronous too.  A prepared-load
        # request may spend many frames in worker-backed resource preflight;
        # spinning here would freeze the Player until those jobs finish.
        if not transaction.poll():
            return

        target = self._transaction_path
        self._clear_pending_load()
        if not transaction.succeeded:
            self._last_error = transaction.error or "scene transaction failed"
            Debug.log_error(
                f"Player scene load failed for '{target}': {self._last_error}"
            )
            return
        assert target is not None
        self._publish_completed_scene(target, start_for_play=True)

    def cancel_pending_load(self) -> None:
        self._request_generation += 1
        if self._transaction is not None:
            self._transaction.cancel()
        self._clear_pending_load()

    def _clear_pending_load(self) -> None:
        self._pending_scene_path = None
        self._transaction = None
        self._transaction_path = None
        self._transaction_generation = 0
        self._wait_for_ready = False
        self._hold_for_activation = False

    def _begin_load_request(self) -> Optional[int]:
        transaction = self._transaction
        if transaction is not None and not transaction.is_complete:
            try:
                if not transaction.cancel():
                    self._last_error = "the active scene transaction is already committing"
                    return None
            except Exception as exc:
                self._last_error = str(exc) or type(exc).__name__
                return None
        self._clear_pending_load()
        self._request_generation += 1
        return self._request_generation

    def _new_transaction(self, path: str) -> SceneDocumentTransaction:
        from Infernux.lib import SceneManager

        scene_manager = SceneManager.instance()
        scene = scene_manager.get_active_scene()
        if scene is None:
            scene = scene_manager.create_scene("PlayerScene")
        return SceneDocumentTransaction(
            scene,
            path=path,
            asset_database=self._asset_database,
            clear_registries=True,
            before_commit=getattr(
                scene_manager, "prepare_active_scene_replacement", None
            ),
        )

    def _validated_scene_path(self, path: str) -> Optional[str]:
        if self._runtime_catalog is None:
            Debug.log_error("Player scene load rejected: RuntimeAssetCatalog is not bound")
            return None
        target = self._runtime_catalog.resolve_scene(path)
        if target is None:
            Debug.log_warning(f"Player scene is absent from RuntimeAssetCatalog: {path}")
            return None
        return resolved_path(target)

    def _publish_completed_scene(self, path: str, *, start_for_play: bool) -> None:
        from Infernux.lib import SceneManager

        scene_manager = SceneManager.instance()
        scene = scene_manager.get_active_scene()
        self._active_scene_path = path
        self._last_error = ""
        from Infernux.timing import Time
        Time._reset_frame_delta()
        if start_for_play:
            scene_manager._start_active_scene_for_play()
        Debug.log_internal(
            f"Player loaded scene: {os.path.basename(path)} "
            f"(objects={len(scene.get_all_objects()) if scene is not None else 0}, "
            f"camera={scene is not None and scene.main_camera is not None})"
        )


__all__ = ["PlayerSceneService"]
