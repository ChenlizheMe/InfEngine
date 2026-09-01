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
import sys
import types
from enum import Enum, auto
from typing import Optional, List, Dict, Any, Callable, Iterable, TYPE_CHECKING
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


@dataclass(frozen=True)
class ScriptReloadOutcome:
    """Result of applying one validated script revision to live components."""

    success: bool
    had_live_targets: bool
    reloaded_count: int = 0
    error: str = ""


@dataclass(frozen=True)
class ScriptReloadBatchInput:
    """One frontend script revision offered to the Play/Pause batch API."""

    file_path: str
    script_guid: str = ""
    source: bytes | str | None = None
    code: types.CodeType | None = None
    retire_script_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScriptReloadBatchMember:
    file_path: str
    script_guid: str
    had_live_targets: bool
    target_count: int


@dataclass
class ScriptReloadBatch:
    """Prepared multi-script transaction owned by one PlayModeManager."""

    transaction: object
    members: tuple[ScriptReloadBatchMember, ...]

    @property
    def had_live_targets(self) -> bool:
        return bool(getattr(self.transaction, "had_live_targets", False))

    @property
    def committed(self) -> bool:
        return bool(getattr(self.transaction, "committed", False))

    @property
    def rolled_back(self) -> bool:
        return bool(getattr(self.transaction, "rolled_back", False))

    def commit(self) -> dict[type, tuple[str, ...]]:
        return self.transaction.commit()

    def rollback(self) -> None:
        self.transaction.rollback()

    def finalize(self) -> None:
        finalize = getattr(self.transaction, "finalize", None)
        if callable(finalize):
            finalize()


@dataclass(frozen=True)
class EditComponentReloadMember:
    """One fully prepared Edit-mode replacement, before scene mutation."""

    object_id: int
    old_component: object
    new_component: object
    component_index: int


@dataclass(frozen=True)
class _MissingScriptRecoveryMember:
    object_id: int
    component_index: int
    missing_component: object
    file_path: str
    script_guid: str
    state: dict
    enabled: bool
    awake_called: bool
    has_started: bool
    execution_order: int


class _MissingScriptRecoveryTransaction:
    """Publish real types and atomically replace matching MissingScript values."""

    def __init__(
        self,
        manager: "PlayModeManager",
        body_transaction: object,
        members: tuple[_MissingScriptRecoveryMember, ...],
    ) -> None:
        self.manager = manager
        self.body_transaction = body_transaction
        self.members = members
        self.had_live_targets = bool(
            getattr(body_transaction, "had_live_targets", False) or members
        )
        self._prepared: list[tuple[_MissingScriptRecoveryMember, object]] = []
        self._replaced: list[tuple[_MissingScriptRecoveryMember, object]] = []
        self._committed = False
        self._rolled_back = False
        self._finalized = False

    @property
    def committed(self) -> bool:
        return self._committed and not self._rolled_back

    @property
    def rolled_back(self) -> bool:
        return self._rolled_back

    @property
    def finalized(self) -> bool:
        return self._finalized

    @property
    def recovered_count(self) -> int:
        return len(self._replaced) if self.committed else 0

    def _candidate_type(self, member: _MissingScriptRecoveryMember) -> type:
        candidates = []
        for _path, component_types in getattr(
            self.body_transaction, "registry_entries", ()
        ):
            for component_type in component_types:
                if str(getattr(component_type, "_asset_script_guid_", "") or "") != member.script_guid:
                    continue
                candidates.append(component_type)

        type_guid = str(member.state.get("type_guid") or "")
        exact_guid = [
            component_type
            for component_type in candidates
            if str(component_type._get_type_guid() or "") == type_guid
        ]
        if len(exact_guid) == 1:
            return exact_guid[0]

        qualified_name = str(member.state.get("qualified_name") or "")
        exact_qualified = [
            component_type
            for component_type in candidates
            if component_type.__qualname__ == qualified_name
        ]
        if len(exact_qualified) == 1:
            return exact_qualified[0]

        type_name = str(member.state.get("type_name") or "")
        exact_name = [
            component_type
            for component_type in candidates
            if component_type.__name__ == type_name
        ]
        if len(exact_name) == 1:
            return exact_name[0]
        if not exact_name:
            raise RuntimeError(
                f"restored script does not define component type '{type_name}'"
            )
        raise RuntimeError(
            f"restored script defines ambiguous component type '{type_name}'"
        )

    def _prepare_replacements(self) -> None:
        from Infernux.components.script_loader import create_component_instance

        for member in self.members:
            component_type = self._candidate_type(member)
            instance = create_component_instance(component_type)
            instance._script_guid = member.script_guid
            instance._script_path = member.file_path
            self.manager._apply_py_component_state(instance, member.state)
            self.manager._copy_replacement_lifecycle_state(
                member.missing_component,
                instance,
                enabled=member.enabled,
                awake_called=member.awake_called,
                has_started=member.has_started,
                execution_order=member.execution_order,
            )
            self._prepared.append((member, instance))

    @staticmethod
    def _discard_prepared(instances: Iterable[tuple[_MissingScriptRecoveryMember, object]]) -> None:
        for _member, instance in instances:
            detach = getattr(instance, "_detach_native_binding_for_replacement", None)
            if callable(detach):
                detach()

    def commit(self) -> dict[type, tuple[str, ...]]:
        if self._rolled_back:
            raise RuntimeError("MissingScript recovery transaction has been rolled back")
        if self._committed:
            return {}
        from Infernux.engine.runtime_dispatch import assert_runtime_dispatch_safe_point

        try:
            assert_runtime_dispatch_safe_point()
            changed_by_type = self.body_transaction.commit()
            self._prepare_replacements()
            for member, instance in self._prepared:
                scene = self.manager._get_active_scene_for_script_reload()
                if scene is None:
                    raise RuntimeError("active scene disappeared during MissingScript recovery")
                obj = scene.find_by_id(member.object_id)
                if obj is None:
                    raise RuntimeError(
                        f"GameObject {member.object_id} disappeared during MissingScript recovery"
                    )
                self.manager._replace_edit_component_exact(
                    obj,
                    member.missing_component,
                    instance,
                    member.component_index,
                )
                self.manager._copy_replacement_lifecycle_state(
                    member.missing_component,
                    instance,
                    enabled=member.enabled,
                    awake_called=member.awake_called,
                    has_started=member.has_started,
                    execution_order=member.execution_order,
                )
                self._replaced.append((member, instance))
            for _member, instance in self._replaced:
                callback = getattr(instance, "_call_on_after_deserialize", None)
                if callable(callback):
                    callback()
            self._committed = True
            return changed_by_type
        except Exception:
            self.rollback()
            raise

    def rollback(self) -> None:
        if self._rolled_back:
            return
        if self._finalized:
            raise RuntimeError(
                "finalized MissingScript recovery transaction cannot be rolled back"
            )
        rollback_errors = []
        scene = self.manager._get_active_scene_for_script_reload()
        for member, instance in reversed(self._replaced):
            try:
                if scene is None:
                    raise RuntimeError("active scene disappeared")
                obj = scene.find_by_id(member.object_id)
                if obj is None:
                    raise RuntimeError(f"GameObject {member.object_id} disappeared")
                self.manager._replace_edit_component_exact(
                    obj,
                    instance,
                    member.missing_component,
                    member.component_index,
                )
                self.manager._copy_replacement_lifecycle_state(
                    instance,
                    member.missing_component,
                    enabled=member.enabled,
                    awake_called=member.awake_called,
                    has_started=member.has_started,
                    execution_order=member.execution_order,
                )
            except Exception as exc:
                rollback_errors.append(str(exc))
        if rollback_errors:
            raise RuntimeError(
                "failed to restore MissingScript scene state: "
                + "; ".join(rollback_errors)
            )
        # Release candidate-owned instance state while its provisional schema
        # is still published, then roll back module/registry/dispatch/CDS.
        self._discard_prepared(self._prepared)
        self.body_transaction.rollback()
        self._prepared.clear()
        self._replaced.clear()
        self._committed = False
        self._rolled_back = True

    def finalize(self) -> None:
        if self._rolled_back:
            raise RuntimeError("MissingScript recovery transaction has been rolled back")
        if not self._committed:
            raise RuntimeError("MissingScript recovery transaction must commit first")
        if self._finalized:
            return
        self.body_transaction.finalize()
        self._prepared.clear()
        self._replaced.clear()
        self._finalized = True


class ScriptDeleteBatch:
    """Transactional conversion of live script components to MissingScript."""

    def __init__(
        self,
        manager: "PlayModeManager",
        members: tuple[EditComponentReloadMember, ...],
        *,
        retired_types: tuple[type, ...] = (),
    ):
        self.manager = manager
        self.members = members
        self._replaced: list[EditComponentReloadMember] = []
        self._committed = False
        self._rolled_back = False
        self.retired_types = tuple(retired_types)
        self._dispatch_publication = None

    @property
    def committed(self) -> bool:
        return self._committed and not self._rolled_back

    @property
    def rolled_back(self) -> bool:
        return self._rolled_back

    @property
    def had_live_targets(self) -> bool:
        return bool(self.members)

    def commit(self) -> int:
        if self._rolled_back:
            raise RuntimeError("script delete batch has been rolled back")
        if self._committed:
            return len(self._replaced)
        try:
            from Infernux.engine.runtime_dispatch import assert_runtime_dispatch_safe_point

            # Reject before replacing the first live component.  Waiting
            # until dispatch retirement would expose a half-deleted scene to
            # an active lifecycle frame before the compensating rollback.
            assert_runtime_dispatch_safe_point()
            for member in self.members:
                scene = self.manager._get_active_scene_for_script_reload()
                if scene is None:
                    raise RuntimeError("active scene disappeared during script deletion")
                obj = scene.find_by_id(member.object_id)
                if obj is None:
                    raise RuntimeError(
                        f"GameObject {member.object_id} disappeared during script deletion"
                    )
                self.manager._replace_edit_component_exact(
                    obj,
                    member.old_component,
                    member.new_component,
                    member.component_index,
                )
                self._replaced.append(member)
            from Infernux.engine.runtime_dispatch import publish_runtime_dispatch_epoch

            self._dispatch_publication = publish_runtime_dispatch_epoch(
                (),
                retired_types=self.retired_types,
                defer_commit=True,
            )
            self._dispatch_publication.commit()
            self._committed = True
            return len(self._replaced)
        except Exception:
            self.rollback()
            raise

    def rollback(self) -> None:
        if self._rolled_back:
            return
        errors = []
        try:
            if self._dispatch_publication is not None:
                self._dispatch_publication.rollback()
            scene = self.manager._get_active_scene_for_script_reload()
            if scene is not None:
                for member in reversed(self._replaced):
                    obj = scene.find_by_id(member.object_id)
                    if obj is None:
                        errors.append(f"GameObject {member.object_id} disappeared")
                        continue
                    try:
                        self.manager._replace_edit_component_exact(
                            obj,
                            member.new_component,
                            member.old_component,
                            member.component_index,
                        )
                    except Exception as exc:
                        errors.append(str(exc))
        finally:
            self._replaced.clear()
            self._committed = False
            self._rolled_back = True
        if errors:
            raise RuntimeError("failed to roll back script deletion: " + "; ".join(errors))


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

        # These modules are stable process-level services.  Cache their
        # classes once at manager construction so the frame callback does not
        # repeat import resolution before every timing/load check.
        from Infernux.scene import SceneManager as _SceneManagerAPI
        from Infernux.timing import Time as _TimeAPI
        self._scene_manager_api = _SceneManagerAPI
        self._time_api = _TimeAPI
        
        # Timing
        self._last_frame_time: float = 0.0
        self._delta_time: float = 0.0
        self._time_scale: float = 1.0
        self._total_play_time: float = 0.0
        # Monotonic within one Play session.  A paused editor frame does not
        # advance Time.frame_count and may not consume a fixed physics step,
        # so automation needs an explicit completion sequence for Step.
        self._step_sequence: int = 0
        
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
        self._last_transition_timings_ms: dict[str, Any] = {}

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
        object_id = int(game_object.id)
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
            callback()

    def get_runtime_hidden_object_ids(self) -> set[int]:
        return set(self._runtime_hidden_object_ids)

    def is_runtime_hidden_object_id(self, object_id: int) -> bool:
        return int(object_id) in self._runtime_hidden_object_ids
    
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

    @property
    def step_sequence(self) -> int:
        """Number of completed paused Step commands in this Play session."""
        return self._step_sequence

    @property
    def last_transition_timings_ms(self) -> dict[str, Any]:
        """Timing breakdown for the most recently completed Play Mode transition."""
        timings = dict(self._last_transition_timings_ms)
        phases = timings.get("phases")
        if isinstance(phases, dict):
            timings["phases"] = dict(phases)
        return timings
    
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

        from Infernux.engine.deferred_task import DeferredTaskRunner
        runner = DeferredTaskRunner.instance()
        if runner.is_busy:
            if runner.active_task_name == "Enter Play Mode":
                return True
            Debug.log_warning(
                "Cannot enter play mode while deferred task "
                f"'{runner.active_task_name or runner.active_step_label or 'unknown'}' is running"
            )
            return False

        Debug.log_internal("▶ Entering Play Mode...")

        # ── Step functions (closures capture self) ───────────────────
        def step_enter():
            """Save scene, rebuild from snapshot, and activate play — all in one frame."""
            transition_started = time.perf_counter()
            sprite_init_started = transition_started
            from Infernux.components.builtin.sprite_renderer import SpriteRenderer
            SpriteRenderer.init_all_in_scene()
            sprite_init_ms = (time.perf_counter() - sprite_init_started) * 1000.0
            # 1. Serialize scene + init timing (do not clear undo — asset editors keep history)
            snapshot_started = time.perf_counter()
            self._save_scene_state()
            snapshot_ms = (time.perf_counter() - snapshot_started) * 1000.0
            self._last_frame_time = time.time()
            self._total_play_time = 0.0
            self._delta_time = 0.0
            self._step_sequence = 0
            try:
                from Infernux.timing import Time
                Time._reset()
            except ImportError:
                # Time module not yet importable during early bootstrap — benign.
                pass
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
                self._invalidate_native_gpu_view_state()
                return False
            rebuild_ms = (time.perf_counter() - rebuild_started) * 1000.0

            # 4. Drain retired edit-domain particle graphs, then enter C++
            #    play. Waiting after Start would FlushRetired while the new
            #    graphs already exist and can recycle the same Vulkan handles.
            scene_manager = self._get_scene_manager()
            native_start_started = time.perf_counter()
            self._invalidate_native_gpu_view_state()
            if scene_manager:
                scene_manager.play()
            self._mark_native_scene_temporal_discontinuity()
            native_start_ms = (time.perf_counter() - native_start_started) * 1000.0
            total_ms = (time.perf_counter() - transition_started) * 1000.0
            self._last_transition_timings_ms = {
                "transition": "enter",
                "total": total_ms,
                "snapshot": snapshot_ms,
                "rebuild": rebuild_ms,
                "native_start": native_start_ms,
                "sprite_init": sprite_init_ms,
                "notify": notify_ms,
            }
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
            if runner.active_task_name == "Exit Play Mode":
                return True
            Debug.log_warning(
                "Cannot exit play mode while deferred task "
                f"'{runner.active_task_name or runner.active_step_label or 'unknown'}' is running"
            )
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
        from Infernux.scene import SceneManager as _SceneMgr
        _SceneMgr._scene_load_generation += 1
        transaction = _SceneMgr._active_scene_transaction
        if transaction is not None and not transaction.is_complete:
            transaction.cancel()
        _SceneMgr._clear_runtime_load_state()

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
            from Infernux.components.builtin_component import BuiltinComponent
            BuiltinComponent._clear_cache()
            self._invalidate_native_gpu_view_state()
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
            self._last_transition_timings_ms = {
                "transition": "exit",
                "total": total_ms,
                "rebuild": rebuild_ms,
                "notify": notify_ms,
                "phases": dict(self._last_rebuild_timings_ms),
            }
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
            self._step_sequence += 1
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
        # The common path has no pending request.  Avoid crossing into the
        # scene-load service until a script actually queued a load or an
        # existing transaction needs polling.
        scene_manager_api = self._scene_manager_api
        if (
            scene_manager_api._pending_scene_load is not None
            or scene_manager_api._active_scene_transaction is not None
        ):
            scene_manager_api.process_pending_load()
        
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
            Time = self._time_api
            self._time_scale = Time.time_scale
            Time._tick(raw_dt)
            # Read back computed values so PlayModeManager stays in sync
            self._delta_time = Time.delta_time
            self._total_play_time = Time.time
            # Read game-only frame cost from C++ (previous frame's measurement)
            if self._native_engine is not None:
                Time._game_delta_time = self._native_engine.get_game_only_frame_ms() / 1000.0
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
            prefer_loaded_types=True,
            before_commit=before_commit,
            after_publish=after_publish,
        )
        if not transaction.run_to_completion(raise_on_failure=False):
            self._last_rebuild_timings_ms = transaction.phase_timings_ms
            Debug.log_error(f"Cannot rebuild scene: document transaction failed: {transaction.error}")
            return False
        self._last_rebuild_timings_ms = transaction.phase_timings_ms

        # A play/edit transition replaces native objects while preserving
        # their authored IDs. Cached editor projections therefore cannot use
        # identity alone to detect the replacement. Publish the rebuild once
        # so Inspector, render extraction, and other revision consumers all
        # observe the fresh graph on the next frame.
        from Infernux.engine.runtime_change_journal import (
            RuntimeChangeDomain,
            runtime_change_journal,
        )

        change_journal = runtime_change_journal()
        change_journal.publish(RuntimeChangeDomain.SCENE_TOPOLOGY, broad=True)
        change_journal.publish(RuntimeChangeDomain.COMPONENT_STRUCTURE, broad=True)
        change_journal.publish(RuntimeChangeDomain.TRANSFORM_LOCAL, broad=True)
        change_journal.publish(RuntimeChangeDomain.TRANSFORM_WORLD, broad=True)
        try:
            from Infernux.engine.ui.inspector_snapshot import invalidate_rebuilt_scene

            invalidate_rebuilt_scene()
        except ImportError:
            # Player/headless distributions intentionally omit editor UI.
            pass

        if restore_scene_path:
            self._restore_scene_file_path()

        return True

    # ========================================================================
    # Python component helpers (serialization / reload)
    # ========================================================================

    def reload_components_from_script_result(
        self,
        file_path: str,
        *,
        source: bytes | str | None = None,
        code: types.CodeType | None = None,
    ) -> ScriptReloadOutcome:
        """
        Reload all Python components and report whether publication was applied.

        Edit, Play, and Pause all publish through the same stable-class
        transaction.  Existing component/native identities and cross-component
        references therefore never depend on which editor mode observed the
        source revision.
        """
        if self._state in (
            PlayModeState.EDIT,
            PlayModeState.PLAYING,
            PlayModeState.PAUSED,
        ):
            return self._reload_play_component_body(
                file_path,
                source=source,
                code=code,
            )
        return ScriptReloadOutcome(True, False)

    def _get_active_scene_for_script_reload(self):
        scene_manager = self._get_scene_manager()
        return scene_manager.get_active_scene() if scene_manager else None

    def _replace_edit_component_exact(
        self,
        obj,
        old_component,
        new_component,
        component_index: int,
    ) -> None:
        """Replace one component while preserving native identity/order."""
        replace = getattr(obj, "replace_py_component", None)
        if callable(replace):
            try:
                result = replace(old_component, new_component)
                if result is not new_component:
                    raise RuntimeError(
                        f"component replacement was rejected on GameObject {getattr(obj, 'id', '?')}"
                    )
                return
            except Exception:
                # A native binding may have published before reporting a
                # failure. Best-effort immediate reversal keeps the batch
                # boundary intact; the outer transaction also reverses prior
                # successful members.
                try:
                    attached = tuple(obj.get_py_components() or ())
                    if new_component in attached and old_component not in attached:
                        restored = replace(new_component, old_component)
                        if restored is not old_component:
                            raise RuntimeError("native replacement reversal was rejected")
                except Exception as rollback_exc:
                    raise RuntimeError(
                        f"component replacement failed and could not be reversed: {rollback_exc}"
                    )
                raise

        remove = getattr(obj, "remove_py_component", None)
        add = getattr(obj, "add_py_component", None)
        if not callable(remove) or not callable(add):
            raise RuntimeError("GameObject does not support transactional component replacement")
        if old_component not in tuple(obj.get_py_components() or ()):
            raise RuntimeError("old component is no longer attached")
        if remove(old_component) is False:
            raise RuntimeError("GameObject rejected removal of the old component")
        add(new_component)
        current = list(obj.get_py_components() or ())
        if new_component not in current or old_component in current:
            raise RuntimeError("GameObject did not publish the replacement component")
        # Test doubles and Python-only objects may expose their ordered list;
        # restore that order without changing the native replacement path.
        ordered = getattr(obj, "_components", None)
        if isinstance(ordered, list):
            ordered.remove(new_component)
            ordered.insert(min(component_index, len(ordered)), new_component)

    @staticmethod
    def _copy_replacement_lifecycle_state(
        source,
        destination,
        *,
        enabled: bool | None = None,
        awake_called: bool | None = None,
        has_started: bool | None = None,
        execution_order: int | None = None,
    ) -> None:
        """Stage lifecycle mirrors without invoking user lifecycle methods."""
        destination._enabled = bool(
            getattr(source, "_enabled", True) if enabled is None else enabled
        )
        destination._awake_called = bool(
            getattr(source, "_awake_called", False)
            if awake_called is None
            else awake_called
        )
        destination._has_started = bool(
            getattr(source, "_has_started", False)
            if has_started is None
            else has_started
        )
        destination._is_destroyed = False
        destination._execution_order = int(
            getattr(source, "_execution_order", 0)
            if execution_order is None
            else execution_order
        )

    def prepare_edit_script_reload_batch(
        self,
        revisions: Iterable[ScriptReloadBatchInput],
    ) -> ScriptReloadBatch:
        """Route Edit authoring through the stable-class reload transaction."""
        if self._state != PlayModeState.EDIT:
            raise RuntimeError("Edit component reload requires Edit mode")
        if not self._asset_database:
            raise RuntimeError("Edit component reload requires an asset database")
        return self.prepare_script_reload_batch(revisions)

    def commit_edit_script_reload_batch(self, batch: ScriptReloadBatch) -> int:
        if not isinstance(batch, ScriptReloadBatch):
            raise TypeError("batch must be a ScriptReloadBatch")
        outcome = self.commit_script_reload_batch(batch)
        if not outcome.success:
            raise RuntimeError(outcome.error or "Edit script reload was rejected")
        # This direct Edit API has no outer dependency/LKG transaction.  Its
        # successful return is therefore the durable edge and must close the
        # provisional native schema transaction itself.  ResourcesManager uses
        # the lower-level commit/finalize pair so it can finalize after LKG.
        try:
            self.finalize_script_reload_batch(batch)
        except Exception:
            if not bool(getattr(batch.transaction, "finalized", False)):
                self.rollback_script_reload_batch(batch)
            raise
        return sum(member.target_count for member in batch.members)

    def rollback_edit_script_reload_batch(self, batch: ScriptReloadBatch) -> None:
        if not isinstance(batch, ScriptReloadBatch):
            raise TypeError("batch must be a ScriptReloadBatch")
        self.rollback_script_reload_batch(batch)

    def prepare_script_delete_batch(
        self,
        script_guid: str,
        file_path: str,
    ) -> ScriptDeleteBatch:
        """Stage missing-script replacements for Edit or Play mode."""
        if self._state not in (PlayModeState.EDIT, PlayModeState.PLAYING, PlayModeState.PAUSED):
            raise RuntimeError("script deletion requires Edit, Play, or Pause mode")
        scene = self._get_active_scene_for_script_reload()
        if scene is None:
            raise RuntimeError("no active scene for script deletion")
        from Infernux.components.missing_script import create_missing_script_component
        from Infernux.components.registry import component_types_for_script_path

        members = []
        target_guid = str(script_guid or "").strip()
        for obj in scene.get_all_objects():
            if not hasattr(obj, "get_py_components"):
                continue
            for index, component in enumerate(list(obj.get_py_components() or ())):
                if str(getattr(component, "_script_guid", "") or "") != target_guid:
                    continue
                state = self._serialize_py_component(component)
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
                missing._deserialize_fields_document(
                    fields,
                    _skip_on_after_deserialize=True,
                )
                self._copy_replacement_lifecycle_state(component, missing)
                if not callable(getattr(obj, "replace_py_component", None)) and not (
                    callable(getattr(obj, "remove_py_component", None))
                    and callable(getattr(obj, "add_py_component", None))
                ):
                    raise RuntimeError(
                        f"GameObject {obj.id} cannot transactionally replace Python components"
                    )
                members.append(
                    EditComponentReloadMember(
                        obj.id,
                        component,
                        missing,
                        index,
                    )
                )
        return ScriptDeleteBatch(
            self,
            tuple(members),
            retired_types=component_types_for_script_path(file_path),
        )

    def commit_script_delete_batch(self, batch: ScriptDeleteBatch) -> int:
        if not isinstance(batch, ScriptDeleteBatch):
            raise TypeError("batch must be a ScriptDeleteBatch")
        return batch.commit()

    def rollback_script_delete_batch(self, batch: ScriptDeleteBatch) -> None:
        if not isinstance(batch, ScriptDeleteBatch):
            raise TypeError("batch must be a ScriptDeleteBatch")
        batch.rollback()

    def prepare_script_reload_batch(
        self,
        revisions: Iterable[ScriptReloadBatchInput],
    ) -> ScriptReloadBatch:
        """Collect active-scene targets and stage one stable-class batch."""
        profile_started = time.perf_counter()
        profile_marks: list[tuple[str, float]] = []

        def mark(label: str) -> None:
            profile_marks.append((label, time.perf_counter()))

        if self._state not in (
            PlayModeState.EDIT,
            PlayModeState.PLAYING,
            PlayModeState.PAUSED,
        ):
            raise ScriptReloadRejected(
                "batch body reload requires Edit, Play, or Pause mode"
            )

        from Infernux.components.script_loader import (
            ComponentBodyReloadRequest,
            ScriptReloadRejected,
            stage_component_body_reload_batch,
        )

        scene_manager = self._get_scene_manager()
        scene = scene_manager.get_active_scene() if scene_manager else None
        targets_by_guid: dict[str, dict[type, list[object]]] = {}
        missing_by_guid: dict[str, list[tuple[int, int, object, dict]]] = {}
        from Infernux.components.missing_script import MissingScript

        if scene is not None:
            for obj in scene.get_all_objects():
                if not hasattr(obj, "get_py_components"):
                    continue
                for index, component in enumerate(obj.get_py_components() or ()):
                    guid = str(getattr(component, "_script_guid", "") or "")
                    if not guid:
                        continue
                    if isinstance(component, MissingScript):
                        missing_by_guid.setdefault(guid, []).append(
                            (
                                obj.id,
                                index,
                                component,
                                self._serialize_py_component(component),
                            )
                        )
                        continue
                    targets_by_guid.setdefault(guid, {}).setdefault(
                        type(component),
                        [],
                    ).append(component)
        mark("scene_targets")

        inputs = tuple(revisions)
        if not inputs:
            raise ValueError("at least one script revision is required")
        requests = []
        members = []
        recovery_members = []
        seen_guids: set[str] = set()
        for revision in inputs:
            if not isinstance(revision, ScriptReloadBatchInput):
                raise TypeError("revisions must contain ScriptReloadBatchInput values")
            path = resolve_script_path(revision.file_path)
            guid = revision.script_guid
            if not guid and self._asset_database and path:
                guid = self._asset_database.get_guid_from_path(path) or ""
            guid = str(guid or "")
            if guid and guid in seen_guids:
                raise ScriptReloadRejected(f"duplicate script GUID in reload batch: {guid}")
            if guid:
                seen_guids.add(guid)
            grouped = targets_by_guid.get(guid, {})
            missing = tuple(missing_by_guid.get(guid, ()))
            instances_by_type = {
                component_type: tuple(instances)
                for component_type, instances in grouped.items()
            }
            target_types = tuple(instances_by_type)
            requests.append(
                ComponentBodyReloadRequest(
                    file_path=path or revision.file_path,
                    target_types=target_types,
                    instances_by_type=instances_by_type,
                    script_guid=guid,
                    source=revision.source,
                    code=revision.code,
                    retire_script_paths=tuple(revision.retire_script_paths),
                )
            )
            members.append(
                ScriptReloadBatchMember(
                    file_path=path or revision.file_path,
                    script_guid=guid,
                    had_live_targets=bool(target_types or missing),
                    target_count=(
                        sum(len(values) for values in instances_by_type.values())
                        + len(missing)
                    ),
                )
            )
            for object_id, index, component, state in missing:
                recovery_members.append(
                    _MissingScriptRecoveryMember(
                        object_id=object_id,
                        component_index=index,
                        missing_component=component,
                        file_path=path or revision.file_path,
                        script_guid=guid,
                        state=state,
                        enabled=bool(state.get("enabled", True)),
                        awake_called=bool(getattr(component, "_awake_called", False)),
                        has_started=bool(getattr(component, "_has_started", False)),
                        execution_order=int(getattr(component, "_execution_order", 0)),
                    )
                )
        mark("requests")

        transaction = stage_component_body_reload_batch(tuple(requests))
        mark("stage_bodies")
        if recovery_members:
            transaction = _MissingScriptRecoveryTransaction(
                self,
                transaction,
                tuple(recovery_members),
            )
            mark("missing_recovery")
        elapsed_ms = (time.perf_counter() - profile_started) * 1000.0
        if elapsed_ms >= 10.0:
            previous = profile_started
            pieces = []
            for label, current in profile_marks:
                pieces.append(f"{label}={(current - previous) * 1000.0:.2f}ms")
                previous = current
            Debug.log_internal(
                f"[ScriptReloadProfile] prepare_batch={elapsed_ms:.2f}ms "
                f"members={len(inputs)} " + " ".join(pieces)
            )
        return ScriptReloadBatch(transaction, tuple(members))

    def commit_script_reload_batch(
        self,
        batch: ScriptReloadBatch,
    ) -> ScriptReloadOutcome:
        """Commit one prepared batch and refresh dispatch only after all plans apply."""
        if not isinstance(batch, ScriptReloadBatch):
            raise TypeError("batch must be a ScriptReloadBatch")
        try:
            changed_by_type = batch.commit()
        except Exception as exc:
            return ScriptReloadOutcome(
                False,
                batch.had_live_targets,
                0,
                str(exc),
            )
        if changed_by_type:
            from Infernux.engine.undo import _bump_inspector_structure
            _bump_inspector_structure()
            Debug.log_internal(
                f"Reloaded {len(changed_by_type)} component type(s) in one script batch"
            )
        recovered_count = int(getattr(batch.transaction, "recovered_count", 0) or 0)
        return ScriptReloadOutcome(
            True,
            batch.had_live_targets,
            len(changed_by_type) + recovered_count,
            "",
        )

    def rollback_script_reload_batch(self, batch: ScriptReloadBatch) -> None:
        """Explicitly undo a successful or failed batch publication."""
        if not isinstance(batch, ScriptReloadBatch):
            raise TypeError("batch must be a ScriptReloadBatch")
        batch.rollback()

    def finalize_script_reload_batch(self, batch: ScriptReloadBatch) -> None:
        """Finalize native schema publication after the outer LKG commit."""
        if not isinstance(batch, ScriptReloadBatch):
            raise TypeError("batch must be a ScriptReloadBatch")
        batch.finalize()

    def _reload_play_component_body(
        self,
        file_path: str,
        *,
        source: bytes | str | None = None,
        code: types.CodeType | None = None,
    ) -> ScriptReloadOutcome:
        """Apply a body-only reload and report publication success explicitly."""
        if not self._asset_database:
            return ScriptReloadOutcome(True, False)

        script_path_abs = resolve_script_path(file_path)
        if not script_path_abs or not os.path.exists(script_path_abs):
            return ScriptReloadOutcome(True, False)
        target_guid = self._asset_database.get_guid_from_path(script_path_abs)
        if not target_guid:
            return ScriptReloadOutcome(True, False)

        from Infernux.components.script_loader import (
            ScriptReloadRejected,
            get_script_error_by_path,
            set_script_error,
        )

        try:
            batch = self.prepare_script_reload_batch((
                ScriptReloadBatchInput(
                    file_path=script_path_abs,
                    script_guid=target_guid,
                    source=source,
                    code=code,
                ),
            ))
            outcome = self.commit_script_reload_batch(batch)
            if not outcome.success:
                raise ScriptReloadRejected(outcome.error or "batch body reload failed")
            self.finalize_script_reload_batch(batch)
            return outcome
        except Exception as exc:
            diagnostic = get_script_error_by_path(script_path_abs)
            message = diagnostic or str(exc)
            if isinstance(exc, ScriptReloadRejected) or not diagnostic:
                set_script_error(script_path_abs, message)
            Debug.log_error(
                f"Script hot reload rejected for {os.path.basename(script_path_abs)}: {message}",
                source_file=script_path_abs,
            )
            return ScriptReloadOutcome(False, True, 0, message)


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
            self._copy_replacement_lifecycle_state(component, missing)
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
            from Infernux.engine.undo import _bump_inspector_structure
            _bump_inspector_structure()
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
    
    def _mark_native_scene_temporal_discontinuity(self) -> None:
        """Ask the renderer to drop Game/Scene caches on the next frame."""
        from Infernux.lib import SceneManager as NativeSceneManager

        native = NativeSceneManager.instance()
        if native is not None:
            native.mark_temporal_discontinuity()

    def _invalidate_native_gpu_view_state(self) -> None:
        """Drain GPU work after Play/Stop retires native particle graphs.

        Game RenderGraphs keep camera IDs across a same-scene rebuild. The
        next frame must not reuse compiled particle cullers or cached Game
        submissions that still import the retired buffers. Call this after
        the old graphs are retired and before new ones are created.
        """
        self._mark_native_scene_temporal_discontinuity()
        engine = self._native_engine
        if engine is None:
            return
        wait = getattr(engine, "wait_for_gpu_idle", None)
        if callable(wait):
            wait()

    def _notify_state_change(self, old_state: PlayModeState, new_state: PlayModeState):
        """Notify all listeners of state change."""
        # Tell the C++ renderer whether we're in play mode so it can
        # bypass the editor FPS cap and idle sleep.
        is_playing = new_state != PlayModeState.EDIT
        if self._native_engine is not None:
            self._native_engine.set_play_mode_rendering(is_playing)

        event = PlayModeEvent(
            old_state=old_state,
            new_state=new_state,
            timestamp=time.time()
        )
        
        for listener in self._state_change_listeners:
            listener(event)
    
