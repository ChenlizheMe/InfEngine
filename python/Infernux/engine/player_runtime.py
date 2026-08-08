"""Minimal runtime session used by the packaged Player."""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from Infernux.debug import Debug
from Infernux.engine.path_utils import resolved_path
from Infernux.engine.scene_document_transaction import SceneDocumentTransaction
from Infernux.timing import Time


class PlayerRuntimeSession:
    """Own the standalone Player scene without editor document services."""

    def __init__(
        self,
        *,
        asset_database: Any = None,
        native_engine: Any = None,
        scheduler: Any = None,
    ):
        self._asset_database = asset_database
        self._native_engine = native_engine
        if scheduler is None:
            from Infernux.components._component_lifecycle import RuntimeExecutionScheduler
            scheduler = RuntimeExecutionScheduler(name="player")
        self._execution_scheduler = scheduler
        self._state = "stopped"
        self._last_frame_time = time.time()
        self._active_scene_path: Optional[str] = None

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
        return self._active_scene_path

    @property
    def execution_scheduler(self) -> Any:
        """Return the shared on-demand phase-plan service for diagnostics."""
        return self._execution_scheduler

    def load_scene(self, path: str) -> bool:
        """Load a scene through the runtime transaction path."""
        from Infernux.lib import SceneManager

        target = resolved_path(path)
        if not target or not os.path.isfile(target):
            Debug.log_warning(f"Player scene file not found: {path}")
            return False

        scene_manager = SceneManager.instance()
        scene = scene_manager.get_active_scene()
        if scene is None:
            scene = scene_manager.create_scene("PlayerScene")
        transaction = SceneDocumentTransaction(
            scene,
            path=target,
            asset_database=self._asset_database,
            clear_registries=True,
        )
        if not transaction.run_to_completion(raise_on_failure=False):
            Debug.log_error(
                f"Player scene load failed for '{target}': {transaction.error}"
            )
            return False

        self._active_scene_path = target
        Debug.log_internal(
            f"Player loaded scene: {os.path.basename(target)} "
            f"(objects={len(scene.get_all_objects())}, camera={scene.main_camera is not None})"
        )
        return True

    def activate(self) -> bool:
        """Start the loaded scene without creating an editor play snapshot."""
        from Infernux.lib import SceneManager
        from Infernux.renderstack.render_stack import RenderStack

        scene = SceneManager.instance().get_active_scene()
        if scene is None:
            Debug.log_warning("Player cannot start without an active scene")
            return False
        Time._reset()
        self._last_frame_time = time.time()
        RenderStack._active_instance = None
        scene.set_playing(True)
        self._refresh_loaded_scene(scene)
        SceneManager.instance().play()
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
        from Infernux.scene import SceneManager as RuntimeSceneManager

        if RuntimeSceneManager.is_scene_load_pending():
            RuntimeSceneManager.process_pending_load()

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
        self._state = "stopped"

    @staticmethod
    def _refresh_loaded_scene(scene: Any) -> None:
        try:
            from Infernux.components.builtin.sprite_renderer import SpriteRenderer

            SpriteRenderer.init_all_in_scene(scene)
        except Exception as exc:
            Debug.log_internal(f"Player SpriteRenderer init: {exc}")
