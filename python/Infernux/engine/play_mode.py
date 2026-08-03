"""
PlayMode - Runtime/Editor mode manager for Infernux.

Manages the play mode state machine:
- Edit Mode: Normal editor state, scene changes are persistent
- Play Mode: Runtime simulation, scene changes are temporary
- Pause Mode: Runtime paused, can step frame by frame

Handles:
- Scene state save/restore for play mode isolation (Unity-style)
- Delta time management
- Python component recreation after scene restore
"""

import time
import os
from enum import Enum, auto
from typing import Optional, List, Dict, Any, Callable, TYPE_CHECKING
from dataclasses import dataclass
from Infernux.debug import Debug, LogType
from Infernux.engine.project_context import resolve_script_path

if TYPE_CHECKING:
    from Infernux.lib import SceneManager, Scene, GameObject
    from Infernux.components.component import InxComponent


class PlayModeState(Enum):
    """Play mode states."""
    EDIT = auto()      # Normal editor mode
    PLAYING = auto()   # Runtime playing
    PAUSED = auto()    # Runtime paused


@dataclass
class PlayModeEvent:
    """Event data for play mode state changes."""
    old_state: PlayModeState
    new_state: PlayModeState
    timestamp: float


def _get_scene_manager():
    """Get the SceneManager singleton from C++ bindings."""
    from Infernux.lib import SceneManager
    return SceneManager.instance()

from ._play_mode_serialization import PlayModeSerializationMixin


class PlayModeManager(PlayModeSerializationMixin):
    """
    Manages the runtime/editor play mode.
    
    Implements Unity-style scene isolation:
    - On Play: Serialize entire scene state (C++ objects + Python components)
    - During Play: All changes are runtime-only
    - On Stop: Deserialize to restore original scene state
    
    Handles:
    - State transitions (Edit ↔ Play ↔ Pause)
    - Scene state save/restore via C++ serialization
    - Python component recreation after restore
    - Timing for UI display
    - (Lifecycle is driven by C++)
    
    Usage:
        play_mode = PlayModeManager()
        
        # Start play mode
        play_mode.enter_play_mode()
        
        # In game loop
        play_mode.tick(delta_time)
        
        # Stop and restore
        play_mode.exit_play_mode()
    """
    
    _instance: Optional['PlayModeManager'] = None
    
    def __init__(self):
        self._state = PlayModeState.EDIT
        
        # Timing
        self._last_frame_time: float = 0.0
        self._delta_time: float = 0.0
        self._time_scale: float = 1.0
        self._total_play_time: float = 0.0
        
        # Typed scene document captured before entering play mode.
        self._scene_backup: Optional[Any] = None
        # Original scene file path (to restore correct scene on Stop)
        self._scene_path_backup: Optional[str] = None
        self._scene_document_id_backup: str = ""
        self._scene_revision_backup: int = 0
        self._scene_saved_revision_backup: int = 0
        self._scene_document_state_backup = None
        
        # Event listeners
        self._state_change_listeners: List[Callable[[PlayModeEvent], None]] = []
        
        # Store singleton reference
        PlayModeManager._instance = self

        # Asset database for GUID-based script lookup
        self._asset_database = None
        self._runtime_hidden_object_ids: set[int] = set()
        self._runtime_hidden_listeners: list[Callable[[], None]] = []
        self._last_rebuild_timings_ms: dict[str, float] = {}

        # C++ engine handle for renderer-level play mode signalling
        self._native_engine = None
        # Debug automation installs this gate only while a bounded frame task
        # is active. Normal editor frames pay only the inactive None check.
        self._debug_frame_pause_gate: Optional[dict] = None
    
    @classmethod
    def instance(cls) -> Optional['PlayModeManager']:
        """Get the singleton instance if it exists."""
        return cls._instance
    
    def _get_scene_manager(self):
        """Get the SceneManager singleton."""
        return _get_scene_manager()

    def set_asset_database(self, asset_database):
        """Set AssetDatabase for GUID-based script resolution."""
        self._asset_database = asset_database

    def clear_runtime_hidden_object_ids(self):
        if not self._runtime_hidden_object_ids:
            return
        self._runtime_hidden_object_ids.clear()
        self._notify_runtime_hidden_changed()

    def register_runtime_hidden_object(self, game_object) -> None:
        if game_object is None:
            return
        try:
            object_id = int(game_object.id)
        except Exception as exc:
            Debug.log_suppressed("PlayModeManager.register_runtime_hidden_object", exc)
            return
        if object_id > 0:
            previous_count = len(self._runtime_hidden_object_ids)
            self._runtime_hidden_object_ids.add(object_id)
            if len(self._runtime_hidden_object_ids) != previous_count:
                self._notify_runtime_hidden_changed()

    def add_runtime_hidden_listener(self, callback: Callable[[], None]) -> None:
        if callback not in self._runtime_hidden_listeners:
            self._runtime_hidden_listeners.append(callback)

    def remove_runtime_hidden_listener(self, callback: Callable[[], None]) -> None:
        try:
            self._runtime_hidden_listeners.remove(callback)
        except ValueError:
            pass

    def _notify_runtime_hidden_changed(self) -> None:
        for callback in tuple(self._runtime_hidden_listeners):
            try:
                callback()
            except Exception as exc:
                Debug.log_suppressed("PlayModeManager.runtime_hidden_listener", exc)

    def get_runtime_hidden_object_ids(self) -> set[int]:
        return set(self._runtime_hidden_object_ids)

    def is_runtime_hidden_object_id(self, object_id: int) -> bool:
        try:
            return int(object_id) in self._runtime_hidden_object_ids
        except Exception as exc:
            Debug.log_suppressed("PlayModeManager.is_runtime_hidden_object_id", exc)
            return False
    
    # ========================================================================
    # Properties
    # ========================================================================
    
    @property
    def state(self) -> PlayModeState:
        """Current play mode state."""
        return self._state
    
    @property
    def is_playing(self) -> bool:
        """True if in play or paused mode."""
        return self._state in (PlayModeState.PLAYING, PlayModeState.PAUSED)
    
    @property
    def is_paused(self) -> bool:
        """True if currently paused."""
        return self._state == PlayModeState.PAUSED
    
    @property
    def is_edit_mode(self) -> bool:
        """True if in edit mode."""
        return self._state == PlayModeState.EDIT
    
    @property
    def delta_time(self) -> float:
        """Time since last frame in seconds."""
        return self._delta_time
    
    @property
    def time_scale(self) -> float:
        """Time scale factor (1.0 = normal speed)."""
        return self._time_scale
    
    @time_scale.setter
    def time_scale(self, value: float):
        """Set the native gameplay time scale."""
        from Infernux.timing import Time
        Time.time_scale = value
        self._time_scale = Time.time_scale
    
    @property
    def total_play_time(self) -> float:
        """Total time elapsed since entering play mode."""
        return self._total_play_time
    
    # ========================================================================
    # State Transitions
    # ========================================================================
    
    def enter_play_mode(self) -> bool:
        """
        Enter play mode from edit mode.
        Saves scene state and initializes components.
        
        Returns:
            True if successfully entered play mode
        """
        if self._state != PlayModeState.EDIT:
            Debug.log_warning("Cannot enter play mode: not in edit mode")
            return False

        # Block play mode while editing a prefab
        from Infernux.engine.scene_manager import SceneFileManager
        sfm = SceneFileManager.instance()
        if sfm and sfm.is_prefab_mode:
            Debug.log_warning("Cannot enter Play mode while in Prefab Mode. Exit Prefab Mode first.")
            return False

        # Pre-flight check: block play if any script has load errors
        from Infernux.components.script_loader import has_script_errors, get_script_errors
        if has_script_errors():
            errors = get_script_errors()
            for path, tb in errors.items():
                Debug.log_error(
                    f"Cannot enter Play Mode — script error in "
                    f"{os.path.basename(path)}:\n{tb.splitlines()[-1]}",
                    source_file=path,
                )
            Debug.log_error(
                f"Play Mode blocked: {len(errors)} script(s) have errors. "
                "Fix all script errors before playing."
            )
            return False

        Debug.log_internal("▶ Entering Play Mode...")

        from Infernux.engine.deferred_task import DeferredTaskRunner
        runner = DeferredTaskRunner.instance()
        if runner.is_busy:
            Debug.log_warning("Cannot enter play mode: a deferred task is already running")
            return False

        # ── Step functions (closures capture self) ───────────────────
        def step_enter():
            """Save scene, rebuild from snapshot, and activate play — all in one frame."""
            transition_started = time.perf_counter()
            sprite_init_started = transition_started
            try:
                from Infernux.components.builtin.sprite_renderer import SpriteRenderer
                SpriteRenderer.init_all_in_scene()
            except Exception as exc:
                Debug.log_suppressed("PlayModeManager.step_enter.SpriteRenderer.init_all_in_scene", exc)
            sprite_init_ms = (time.perf_counter() - sprite_init_started) * 1000.0
            # 1. Serialize scene + init timing (do not clear undo — asset editors keep history)
            snapshot_started = time.perf_counter()
            self._save_scene_state()
            snapshot_ms = (time.perf_counter() - snapshot_started) * 1000.0
            self._last_frame_time = time.time()
            self._total_play_time = 0.0
            self._delta_time = 0.0
            try:
                from Infernux.timing import Time
                Time._reset()
            except ImportError:
                # Time module not yet importable during early bootstrap — benign.
                pass
            except Exception as exc:
                Debug.log_suppressed("PlayModeManager.step_enter.Time._reset", exc)
            from Infernux.components.builtin_component import BuiltinComponent
            BuiltinComponent._clear_cache()

            # 2. Transition state early so that "clear on play" fires
            #    BEFORE Python components are restored (which triggers
            #    Awake → OnEnable and may produce user-visible logs).
            old_state = self._state
            self._state = PlayModeState.PLAYING
            try:
                from Infernux.core.material import Material
                Material._suppress_auto_save = True
            except ImportError:
                # Material module not yet importable — benign during bootstrap.
                pass
            try:
                from Infernux.renderstack.render_effect import RenderEffect
                RenderEffect._suppress_auto_save = True
            except ImportError:
                pass
            notify_started = time.perf_counter()
            self._notify_state_change(old_state, self._state)
            notify_ms = (time.perf_counter() - notify_started) * 1000.0

            # 3. Recreate the scripting domain while retaining the unchanged
            #    native graph. Stop Mode still restores the full snapshot.
            rebuild_started = time.perf_counter()
            if not self._prepare_active_scene_for_play(self._scene_backup):
                Debug.log_error("Failed to rebuild runtime scene for Play Mode")
                self._state = PlayModeState.EDIT
                try:
                    from Infernux.core.material import Material
                    Material._suppress_auto_save = False
                except ImportError:
                    pass
                try:
                    from Infernux.renderstack.render_effect import RenderEffect
                    RenderEffect._suppress_auto_save = False
                except ImportError:
                    pass
                try:
                    self._rebuild_active_scene(self._scene_backup, for_play=False, restore_scene_path=True)
                except Exception as exc:
                    Debug.log_error(f"Failed to restore scene after play-mode build failure: {exc}")
                self._notify_state_change(PlayModeState.PLAYING, PlayModeState.EDIT)
                return False
            rebuild_ms = (time.perf_counter() - rebuild_started) * 1000.0

            # 4. Enter C++ play mode (Scene::Start drives remaining lifecycle)
            scene_manager = self._get_scene_manager()
            native_start_started = time.perf_counter()
            if scene_manager:
                scene_manager.play()
            native_start_ms = (time.perf_counter() - native_start_started) * 1000.0
            total_ms = (time.perf_counter() - transition_started) * 1000.0
            Debug.log_internal(
                "[Perf] PlayMode enter: "
                f"total={total_ms:.1f}ms snapshot={snapshot_ms:.1f}ms "
                f"rebuild={rebuild_ms:.1f}ms nativeStart={native_start_ms:.1f}ms "
                f"spriteInit={sprite_init_ms:.1f}ms notify={notify_ms:.1f}ms"
            )
            Debug.log_internal("[OK] Play Mode started (C++ lifecycle update path)")

        def on_done(ok):
            from Infernux.engine.ui.engine_status import EngineStatus
            if ok:
                EngineStatus.flash("已启动 Playing", 1.0, duration=1.5)
            else:
                EngineStatus.flash("启动失败 Play Failed", 0.0, duration=2.0)

        runner.submit("Enter Play Mode", [
            ("启动运行模式 Entering play mode...", 0.5, step_enter),
        ], on_done=on_done)
        return True
    
    def exit_play_mode(self, on_complete: Optional[Callable[[bool], None]] = None) -> bool:
        """
        Exit play mode and return to edit mode.
        Restores scene state to before play mode.
        
        Returns:
            True if successfully exited play mode
        """
        if self._state == PlayModeState.EDIT:
            Debug.log_warning("Cannot exit play mode: already in edit mode")
            return False
        
        Debug.log_internal("■ Exiting Play Mode...")

        from Infernux.engine.deferred_task import DeferredTaskRunner
        runner = DeferredTaskRunner.instance()
        if runner.is_busy:
            Debug.log_warning("Cannot exit play mode: a deferred task is already running")
            return False

        # ── Immediate actions (same frame as button click) ───────────
        # 1. Stop C++ gameplay loop immediately so no further Update /
        #    FixedUpdate / LateUpdate runs on the play-mode scene.
        #    This prevents an extra simulation frame between the Stop
        #    click and the deferred restore, eliminating a class of bugs
        #    where user scripts modify state after the user expected
        #    simulation to end.
        old_state = self._state
        scene_manager = self._get_scene_manager()
        if scene_manager:
            scene_manager.stop()

        # 2. Transition Python state to EDIT immediately so:
        #    - PlayModeManager.tick() becomes a no-op (no timing / scene loads)
        #    - Toolbar shows "Play" right away
        #    - No deferred scene loads from user scripts are processed
        self._state = PlayModeState.EDIT
        self._cancel_debug_frame_pause_gate()

        # Re-enable material auto-save now that play mode is over.
        try:
            from Infernux.core.material import Material
            Material._suppress_auto_save = False
        except ImportError:
            # Material module not yet importable — benign during teardown.
            pass
        try:
            from Infernux.renderstack.render_effect import RenderEffect
            RenderEffect._suppress_auto_save = False
        except ImportError:
            pass

        # 3. Discard any pending runtime scene load queued by user scripts
        #    during the last play frame — we're about to restore the backup.
        try:
            from Infernux.scene import SceneManager as _SceneMgr
            _SceneMgr._pending_scene_load = None
            transaction = _SceneMgr._active_scene_transaction
            if transaction is not None and not transaction.is_complete:
                transaction.cancel()
            _SceneMgr._active_scene_transaction = None
            _SceneMgr._active_scene_load_path = None
            _SceneMgr._active_scene_file_manager = None
        except Exception as exc:
            Debug.log_suppressed("PlayModeManager.exit_play_mode.discard_pending_load", exc)

        from Infernux.components.builtin_component import BuiltinComponent
        BuiltinComponent._clear_cache()

        # ── Deferred step (single frame to avoid flicker) ─────────

        def step_exit():
            """Restore scene from backup and finalize — all in one frame."""
            transition_started = time.perf_counter()
            # 1. Deserialize backup snapshot and recreate Python components
            rebuild_started = time.perf_counter()
            restore_ok = self._rebuild_active_scene(
                self._scene_backup, for_play=False, restore_scene_path=True
            )
            rebuild_ms = (time.perf_counter() - rebuild_started) * 1000.0
            if not restore_ok:
                Debug.log_error(
                    "Failed to restore scene after exiting Play Mode "
                    "— editor may be in a degraded state"
                )

            # The rebuild above restores the authored document identity and
            # its exact revision/saved-revision pair. A second dirty-baseline
            # write here would duplicate ownership and discard revision data.
            notify_started = time.perf_counter()
            self._notify_state_change(old_state, PlayModeState.EDIT)
            notify_ms = (time.perf_counter() - notify_started) * 1000.0
            total_ms = (time.perf_counter() - transition_started) * 1000.0
            phase_text = " ".join(
                f"{name}={duration:.1f}ms"
                for name, duration in self._last_rebuild_timings_ms.items()
            )
            Debug.log_internal(
                "[Perf] PlayMode exit: "
                f"total={total_ms:.1f}ms rebuild={rebuild_ms:.1f}ms "
                f"notify={notify_ms:.1f}ms {phase_text}"
            )

        def on_done(ok):
            from Infernux.engine.ui.engine_status import EngineStatus
            if ok:
                EngineStatus.flash("已停止 Stopped ■", 1.0, duration=1.5)
            else:
                EngineStatus.flash("停止失败 Stop Failed", 0.0, duration=2.0)
            if on_complete:
                try:
                    on_complete(ok)
                except Exception as exc:
                    Debug.log_error(f"exit_play_mode on_complete callback failed: {exc}")

        runner.submit("Exit Play Mode", [
            ("恢复编辑模式 Restoring edit mode...", 0.5, step_exit),
        ], on_done=on_done)
        return True
    
    def pause(self) -> bool:
        """
        Pause play mode.
        
        Returns:
            True if successfully paused
        """
        if self._state != PlayModeState.PLAYING:
            Debug.log_warning("Cannot pause: not currently playing")
            return False
        
        scene_manager = self._get_scene_manager()
        if scene_manager:
            scene_manager.pause()

        old_state = self._state
        self._state = PlayModeState.PAUSED
        
        Debug.log_internal("⏸ Play Mode Paused")
        self._notify_state_change(old_state, self._state)
        return True
    
    def resume(self) -> bool:
        """
        Resume from pause.
        
        Returns:
            True if successfully resumed
        """
        if self._state != PlayModeState.PAUSED:
            Debug.log_warning("Cannot resume: not currently paused")
            return False
        
        # Reset timing to avoid large delta_time after unpause
        self._last_frame_time = time.time()
        
        scene_manager = self._get_scene_manager()
        if scene_manager:
            scene_manager.play()

        old_state = self._state
        self._state = PlayModeState.PLAYING
        
        Debug.log_internal("▶ Play Mode Resumed")
        self._notify_state_change(old_state, self._state)
        return True
    
    def toggle_pause(self) -> bool:
        """Toggle between playing and paused states."""
        if self._state == PlayModeState.PLAYING:
            return self.pause()
        elif self._state == PlayModeState.PAUSED:
            return self.resume()
        return False
    
    def step_frame(self):
        """
        Execute a single frame while paused.
        Useful for debugging frame-by-frame.
        """
        if self._state != PlayModeState.PAUSED:
            Debug.log_warning("Step only works when paused")
            return
        
        scene_manager = self._get_scene_manager()
        if scene_manager:
            dt = self._delta_time if self._delta_time > 0 else (1.0 / 60.0)
            scene_manager.step(dt)
            Debug.log_internal(f"[Step] Stepped one frame (dt={dt:.4f}s)")

    def _arm_debug_frame_pause_gate(
        self,
        frame_count: int,
        completion_event,
        *,
        pause_on_complete: bool,
        hold_frame_count: int = 0,
        hold_complete_event=None,
        hold_complete_callback=None,
    ) -> None:
        frames = int(frame_count)
        if frames < 1:
            raise ValueError("frame_count must be positive")
        hold_frames = int(hold_frame_count)
        if hold_frames < 0 or hold_frames > frames:
            raise ValueError("hold_frame_count must be between 0 and frame_count")
        self._cancel_debug_frame_pause_gate()
        self._debug_frame_pause_gate = {
            "remaining": frames,
            "target": frames,
            "completion_event": completion_event,
            "pause_on_complete": bool(pause_on_complete),
            "hold_frame_count": hold_frames,
            "hold_complete_event": hold_complete_event,
            "hold_complete_callback": hold_complete_callback,
            "hold_complete": False,
        }

    def _cancel_debug_frame_pause_gate(self) -> None:
        gate = self._debug_frame_pause_gate
        self._debug_frame_pause_gate = None
        if gate is not None:
            event = gate.get("completion_event")
            if event is not None:
                event.set()

    def _advance_debug_frame_pause_gate(self) -> bool:
        gate = self._debug_frame_pause_gate
        if gate is None or self._state != PlayModeState.PLAYING:
            return False
        remaining = int(gate.get("remaining", 0))
        if remaining > 0:
            remaining -= 1
            gate["remaining"] = remaining
            completed = int(gate.get("target", 0)) - remaining
            hold_frames = int(gate.get("hold_frame_count", 0))
            if hold_frames and completed >= hold_frames and not bool(gate.get("hold_complete")):
                gate["hold_complete"] = True
                callback = gate.get("hold_complete_callback")
                if callback is not None:
                    callback()
                event = gate.get("hold_complete_event")
                if event is not None:
                    event.set()
            return False

        self._debug_frame_pause_gate = None
        if bool(gate.get("pause_on_complete")):
            self.pause()
        event = gate.get("completion_event")
        if event is not None:
            event.set()
        return True

    def _prepare_active_scene_for_play(self, snapshot: Optional[Any]) -> bool:
        """Refresh Python component instances while preserving native objects."""
        if not snapshot:
            Debug.log_warning("Cannot prepare scene for Play Mode: empty snapshot")
            return False
        scene_manager = self._get_scene_manager()
        scene = scene_manager.get_active_scene() if scene_manager else None
        if scene is None:
            Debug.log_warning("Cannot prepare scene for Play Mode: no active scene")
            return False

        try:
            from Infernux.engine.component_restore import replace_scene_python_components_for_play
            from Infernux.renderstack.render_stack import RenderStack

            replace_scene_python_components_for_play(
                scene,
                snapshot,
                asset_database=self._asset_database,
            )
            self.clear_runtime_hidden_object_ids()
            RenderStack._active_instance = None
            scene.set_playing(True)
            return True
        except Exception as exc:
            Debug.log_internal(f"Fast Play Mode preparation failed; rebuilding scene: {exc}")
            return self._rebuild_active_scene(snapshot, for_play=True)
    
    # ========================================================================
    # Game Loop Integration
    # ========================================================================
    
    def tick(self, external_delta_time: float = None):
        """
        Called every frame by the engine.
        Updates timing and processes deferred scene loads.
        
        Args:
            external_delta_time: Optional externally provided delta time.
                                If None, calculates from wall clock.
        """
        if self._state == PlayModeState.EDIT:
            return

        if self._debug_frame_pause_gate is not None:
            if self._advance_debug_frame_pause_gate() and self._state == PlayModeState.PAUSED:
                return

        # --- Process deferred scene loads (must run outside C++ iteration) ---
        from Infernux.scene import SceneManager as _SceneMgr
        _SceneMgr.process_pending_load()
        
        if self._state == PlayModeState.PAUSED:
            # Don't update timing when paused
            return
        
        # Calculate delta time
        current_time = time.time()
        if external_delta_time is not None:
            raw_dt = external_delta_time
        else:
            raw_dt = current_time - self._last_frame_time
        
        self._last_frame_time = current_time

        # Sync time_scale from the static Time class (user may set Time.time_scale)
        try:
            from Infernux.timing import Time
            self._time_scale = Time.time_scale
            Time._tick(raw_dt)
            # Read back computed values so PlayModeManager stays in sync
            self._delta_time = Time.delta_time
            self._total_play_time = Time.time
            # Read game-only frame cost from C++ (previous frame's measurement)
            if self._native_engine is not None:
                try:
                    Time._game_delta_time = self._native_engine.get_game_only_frame_ms() / 1000.0
                except Exception as exc:
                    Debug.log_suppressed("PlayModeManager.tick.read_game_only_frame_ms", exc)
        except ImportError:
            self._delta_time = min(raw_dt * self._time_scale, 0.1)
            self._total_play_time += self._delta_time
        except Exception as exc:
            Debug.log_warning(f"Time sync failed: {exc}")
            self._delta_time = min(raw_dt * self._time_scale, 0.1)
            self._total_play_time += self._delta_time
        
        # NOTE: Lifecycle update is driven by C++ only.

    def _rebuild_active_scene(
        self,
        snapshot: Optional[Any],
        *,
        for_play: bool,
        restore_scene_path: bool = False,
    ) -> bool:
        """Restore *snapshot* into the active scene and recreate Python components.

        This is the core of the unified component mode: play/edit transitions no
        longer try to reset lifecycle flags on existing objects. Instead, the
        active scene is rebuilt from serialized data, producing a fresh native
        component graph and fresh Python component instances.
        """
        if not snapshot:
            Debug.log_warning("Cannot rebuild scene: empty snapshot")
            return False

        scene_manager = self._get_scene_manager()
        if not scene_manager:
            Debug.log_warning("Cannot rebuild scene: no SceneManager")
            return False

        scene = scene_manager.get_active_scene()
        if not scene:
            Debug.log_warning("Cannot rebuild scene: no active scene")
            return False

        from Infernux.renderstack.render_stack import RenderStack

        def before_commit():
            # The incoming scene owns a fresh RenderStack instance. Clear the
            # previous scene's singleton before component deserialization so
            # on_after_deserialize() can promote the replacement naturally.
            RenderStack._active_instance = None

        def after_publish():
            self.clear_runtime_hidden_object_ids()
            RenderStack.refresh_active_instance(scene)
            if for_play:
                scene.set_playing(True)
            try:
                from Infernux.components.builtin.sprite_renderer import SpriteRenderer
                SpriteRenderer.init_all_in_scene()
            except Exception as exc:
                Debug.log_internal(f"SpriteRenderer init after rebuild: {exc}")

        from Infernux.engine.scene_document_transaction import SceneDocumentTransaction
        transaction = SceneDocumentTransaction(
            scene,
            document=snapshot,
            asset_database=self._asset_database,
            clear_registries=True,
            borrow_document=True,
            before_commit=before_commit,
            after_publish=after_publish,
        )
        if not transaction.run_to_completion(raise_on_failure=False):
            self._last_rebuild_timings_ms = transaction.phase_timings_ms
            Debug.log_error(f"Cannot rebuild scene: document transaction failed: {transaction.error}")
            return False
        self._last_rebuild_timings_ms = transaction.phase_timings_ms

        if restore_scene_path:
            self._restore_scene_file_path()

        return True

    # ========================================================================
    # Python component helpers (serialization / reload)
    # ========================================================================

    def reload_components_from_script(self, file_path: str):
        """
        Reload all Python components that originate from the given script file.

        This is intended for Edit mode live-updates when a script changes.
        """
        if self._state != PlayModeState.EDIT:
            return
        if not self._asset_database:
            return

        script_path_abs = resolve_script_path(file_path)
        if not script_path_abs or not os.path.exists(script_path_abs):
            return
        scene_manager = self._get_scene_manager()
        if not scene_manager:
            return
        scene = scene_manager.get_active_scene()
        if not scene:
            return

        from Infernux.components.script_loader import (
            create_component_instance,
            load_all_components_from_file,
        )

        reloaded_count = 0
        target_guid = None
        if self._asset_database:
            target_guid = self._asset_database.get_guid_from_path(script_path_abs)
        if not target_guid:
            return

        pending_reload: list[tuple[int, Any, Dict[str, Any]]] = []
        for obj in scene.get_all_objects():
            if not hasattr(obj, "get_py_components"):
                continue
            py_components = list(obj.get_py_components())

            for comp in py_components:
                comp_guid = getattr(comp, "_script_guid", None)
                if comp_guid != target_guid:
                    continue
                try:
                    state = self._serialize_py_component(comp)
                except Exception as exc:
                    Debug.log_error(
                        f"Failed to snapshot component '{getattr(comp, 'type_name', type(comp).__name__)}' "
                        f"before reloading {os.path.basename(script_path_abs)}: {exc}"
                    )
                    continue
                pending_reload.append((obj.id, comp, state))

        if not pending_reload:
            return

        try:
            reloaded_classes = load_all_components_from_file(script_path_abs)
        except Exception as exc:
            Debug.log_error(
                f"Failed to reload component classes from {os.path.basename(script_path_abs)}: {exc}"
            )
            return

        if not reloaded_classes:
            return

        reloaded_by_name = {cls.__name__: cls for cls in reloaded_classes}

        for object_id, old_comp, state in pending_reload:
            obj = scene.find_by_id(object_id)
            if obj is None:
                continue

            target_type_name = state.get("type_name") or getattr(old_comp, "type_name", type(old_comp).__name__)
            component_class = reloaded_by_name.get(target_type_name)
            if component_class is None and len(reloaded_classes) == 1:
                # Class rename inside a one-component script file.
                component_class = reloaded_classes[0]

            if component_class is None:
                Debug.log_error(
                    f"Failed to reload component '{target_type_name}' from {os.path.basename(script_path_abs)}: "
                    f"type not found after reload"
                )
                continue

            # Rebind to the AssetDatabase GUID so renames that preserve the GUID
            # do not look like a type identity change.
            from Infernux.components.component_identity import bind_asset_script_guid
            bind_asset_script_guid(component_class, target_guid)

            try:
                new_comp = create_component_instance(component_class)
            except Exception as exc:
                Debug.log_error(
                    f"Failed to recreate component '{target_type_name}' from {os.path.basename(script_path_abs)}: {exc}"
                )
                new_comp = None

            if new_comp is None:
                continue

            new_comp._script_guid = target_guid
            new_comp._script_path = script_path_abs

            try:
                self._apply_py_component_state(new_comp, state)
            except Exception as exc:
                Debug.log_error(
                    f"Failed to apply state to reloaded component '{target_type_name}': {exc}"
                )

            replace = getattr(obj, "replace_py_component", None)
            if callable(replace):
                if replace(old_comp, new_comp) is None:
                    Debug.log_error(
                        f"Failed to replace reloaded component '{target_type_name}' on object {object_id}"
                    )
                    continue
            else:
                if hasattr(obj, "remove_py_component"):
                    obj.remove_py_component(old_comp)
                obj.add_py_component(new_comp)
            reloaded_count += 1

        if reloaded_count > 0:
            try:
                from Infernux.engine.undo import _bump_inspector_structure
                _bump_inspector_structure()
            except Exception as exc:
                Debug.log_suppressed("PlayModeManager.reload_components.bump_inspector_structure", exc)
            Debug.log_internal(f"Reloaded {reloaded_count} component(s) from {os.path.basename(script_path_abs)}")

    def mark_components_missing_for_script(self, script_guid: str, file_path: str) -> int:
        """Replace live instances of a deleted script with field-preserving placeholders."""
        target_guid = str(script_guid or "").strip()
        if not target_guid:
            return 0

        scene_manager = self._get_scene_manager()
        scene = scene_manager.get_active_scene() if scene_manager else None
        if scene is None:
            return 0

        from Infernux.components.missing_script import MissingScript, create_missing_script_component

        replacements = []
        for obj in scene.get_all_objects():
            if not hasattr(obj, "get_py_components"):
                continue
            for component in list(obj.get_py_components()):
                if isinstance(component, MissingScript):
                    continue
                if (getattr(component, "_script_guid", "") or "") != target_guid:
                    continue
                state = self._serialize_py_component(component)
                replacements.append((obj.id, component, state))

        replaced = 0
        for object_id, old_component, state in replacements:
            obj = scene.find_by_id(object_id)
            if obj is None:
                continue
            fields = dict(state.get("fields", {}))
            fields["__type_name__"] = state["type_name"]
            fields["__component_id__"] = state["component_id"]
            missing = create_missing_script_component(
                type_name=state["type_name"],
                script_guid=target_guid,
                type_guid=state["type_guid"],
                module_name=state.get("module_name", ""),
                qualified_name=state.get("qualified_name", ""),
                fields=fields,
                error=f"Script asset is missing: {file_path}",
            )
            missing.enabled = bool(state.get("enabled", True))
            missing._script_path = str(file_path or "")
            missing._deserialize_fields_document(fields, _skip_on_after_deserialize=True)
            replace = getattr(obj, "replace_py_component", None)
            if callable(replace):
                if replace(old_component, missing) is None:
                    Debug.log_error(
                        f"Failed to preserve missing component '{state['type_name']}' on object {object_id}"
                    )
                    continue
            else:
                if hasattr(obj, "remove_py_component"):
                    obj.remove_py_component(old_component)
                obj.add_py_component(missing)
            replaced += 1

        if replaced:
            try:
                from Infernux.engine.undo import _bump_inspector_structure
                _bump_inspector_structure()
            except Exception as exc:
                Debug.log_suppressed("PlayModeManager.mark_components_missing.bump_inspector_structure", exc)
            Debug.log_internal(
                f"Marked {replaced} component(s) missing after deleting {os.path.basename(file_path)}"
            )
        return replaced

    # ========================================================================
    # Scene Snapshot (for runtime isolation)
    # ========================================================================

    # ========================================================================
    # Python Component Restoration (after C++ scene deserialize)
    # ========================================================================

    # ========================================================================
    # Scene State Management  
    # ========================================================================
    
    def _save_scene_state(self):
        """
        Save scene state before entering play mode.
        Uses the typed C++ Scene document which includes:
        - All GameObjects with their hierarchy
        - Transform data
        - C++ components (MeshRenderer, etc.)
        - Python component metadata (script GUID, fields)
        Also saves the current scene file path so we can return to
        the correct scene if the user switches scenes during play.
        """
        scene_manager = self._get_scene_manager()
        if not scene_manager:
            Debug.log_warning("Cannot save scene state: no SceneManager")
            return
        
        scene = scene_manager.get_active_scene()
        if scene:
            capture_native = getattr(scene, "_capture_play_mode_snapshot", None)
            self._scene_backup = (
                capture_native() if callable(capture_native) else scene.serialize_document()
            )
            # Remember which scene file was open
            from Infernux.engine.scene_manager import SceneFileManager
            sfm = SceneFileManager.instance()
            if sfm:
                self._scene_path_backup = sfm.current_scene_path
                self._scene_document_id_backup = sfm.document_id
                from Infernux.engine.interaction import DocumentRegistry

                document = DocumentRegistry.instance().get(sfm.document_id)
                if document is not None:
                    self._scene_revision_backup = document.revision
                    self._scene_saved_revision_backup = document.saved_revision
                    self._scene_document_state_backup = document.state
            else:
                self._scene_document_id_backup = ""
            Debug.log_internal("Scene state saved (typed C++ document)")
        else:
            Debug.log_warning("No active scene to save")

    def _restore_scene_file_path(self):
        """Restore the exact editor Scene document identity after Play Mode."""
        if not self._scene_document_id_backup:
            return
        from Infernux.engine.scene_manager import SceneFileManager
        sfm = SceneFileManager.instance()
        if sfm is None:
            return
        path_changed = sfm.current_scene_path != self._scene_path_backup
        if path_changed:
            Debug.log_internal(
                f"Restoring editor scene path: "
                f"{os.path.basename(self._scene_path_backup or 'Untitled')}"
            )
        sfm._current_scene_path = self._scene_path_backup
        from Infernux.engine.interaction import DocumentRegistry, DocumentState

        registry = DocumentRegistry.instance()
        document = registry.get(self._scene_document_id_backup)
        if document is not None:
            sfm._scene_document_id = document.document_id
            registry.restore_revision_state(
                document.document_id,
                revision=self._scene_revision_backup,
                saved_revision=self._scene_saved_revision_backup,
                state=self._scene_document_state_backup or DocumentState.READY,
            )
        if path_changed:
            if self._scene_path_backup:
                sfm._restore_camera_state(self._scene_path_backup)
            if sfm._on_scene_changed:
                sfm._on_scene_changed()
        # A runtime transition from an older engine may already have persisted
        # its destination. Always reassert the authored scene at the Stop
        # boundary so the next Editor launch returns to the pre-play document.
        if self._scene_path_backup:
            sfm._remember_last_scene(self._scene_path_backup)
    
    # ========================================================================
    # Event System
    # ========================================================================
    
    def add_state_change_listener(self, callback: Callable[[PlayModeEvent], None]):
        """Add a listener for play mode state changes."""
        if callback not in self._state_change_listeners:
            self._state_change_listeners.append(callback)
    
    def remove_state_change_listener(self, callback: Callable[[PlayModeEvent], None]):
        """Remove a state change listener."""
        if callback in self._state_change_listeners:
            self._state_change_listeners.remove(callback)
    
    def _notify_state_change(self, old_state: PlayModeState, new_state: PlayModeState):
        """Notify all listeners of state change."""
        # Tell the C++ renderer whether we're in play mode so it can
        # bypass the editor FPS cap and idle sleep.
        is_playing = new_state != PlayModeState.EDIT
        if self._native_engine is not None:
            try:
                self._native_engine.set_play_mode_rendering(is_playing)
            except Exception as exc:
                Debug.log_suppressed("PlayModeManager._notify_state_change.set_play_mode_rendering", exc)

        event = PlayModeEvent(
            old_state=old_state,
            new_state=new_state,
            timestamp=time.time()
        )
        
        for listener in self._state_change_listeners:
            listener(event)
    

