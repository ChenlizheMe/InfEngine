from __future__ import annotations

from enum import Enum, auto
from typing import Any, Callable, Iterable, List, Optional
from dataclasses import dataclass
from types import CodeType


class PlayModeState(Enum):
    """Current state of the play mode lifecycle."""

    EDIT = auto()
    PLAYING = auto()
    PAUSED = auto()


@dataclass
class PlayModeEvent:
    """Event data for play mode state transitions."""
    old_state: PlayModeState
    new_state: PlayModeState
    timestamp: float


@dataclass(frozen=True)
class ScriptReloadOutcome:
    """Result of applying one validated script revision to live components."""

    success: bool
    had_live_targets: bool
    reloaded_count: int
    error: str


@dataclass(frozen=True)
class ScriptReloadBatchInput:
    file_path: str
    script_guid: str = ...
    source: bytes | str | None = ...
    code: CodeType | None = ...
    retire_script_paths: tuple[str, ...] = ...


@dataclass(frozen=True)
class ScriptReloadBatchMember:
    file_path: str
    script_guid: str
    had_live_targets: bool
    target_count: int


@dataclass
class ScriptReloadBatch:
    transaction: object
    members: tuple[ScriptReloadBatchMember, ...]
    had_live_targets: bool
    committed: bool
    rolled_back: bool
    def commit(self) -> dict[type, tuple[str, ...]]: ...
    def rollback(self) -> None: ...
    def finalize(self) -> None: ...


@dataclass(frozen=True)
class EditComponentReloadMember:
    object_id: int
    old_component: object
    new_component: object
    component_index: int


class ScriptDeleteBatch:
    members: tuple[EditComponentReloadMember, ...]
    had_live_targets: bool
    committed: bool
    rolled_back: bool
    def commit(self) -> int: ...
    def rollback(self) -> None: ...


class PlayModeManager:
    """Controls the play/pause/stop lifecycle of the editor."""

    def __init__(self) -> None: ...

    @classmethod
    def instance(cls) -> Optional[PlayModeManager]:
        """Get the singleton PlayModeManager, or None."""
        ...
    def set_asset_database(self, asset_database: Any) -> None:
        """Set the asset database used for scene serialization."""
        ...

    @property
    def state(self) -> PlayModeState:
        """The current play mode state."""
        ...
    @property
    def is_playing(self) -> bool:
        """Returns True if the editor is in play mode."""
        ...
    @property
    def is_paused(self) -> bool:
        """Returns True if play mode is paused."""
        ...
    @property
    def is_edit_mode(self) -> bool:
        """Returns True if the editor is in edit mode."""
        ...
    @property
    def delta_time(self) -> float:
        """The time in seconds since the last play mode tick."""
        ...
    @property
    def time_scale(self) -> float:
        """The timescale factor for play mode."""
        ...
    @time_scale.setter
    def time_scale(self, value: float) -> None: ...
    @property
    def total_play_time(self) -> float:
        """Total elapsed time since entering play mode."""
        ...
    @property
    def step_sequence(self) -> int:
        """Number of completed paused Step commands in this Play session."""
        ...

    def enter_play_mode(self) -> bool:
        """Enter play mode. Returns True on success."""
        ...
    def exit_play_mode(self, on_complete: Optional[Callable[[bool], None]] = ...) -> bool:
        """Exit play mode and restore the scene. Returns True on success."""
        ...
    def pause(self) -> bool:
        """Pause play mode. Returns True on success."""
        ...
    def resume(self) -> bool:
        """Resume paused play mode. Returns True on success."""
        ...
    def toggle_pause(self) -> bool:
        """Toggle between paused and playing. Returns True on success."""
        ...
    def step_frame(self) -> None:
        """Advance one frame while paused."""
        ...
    def tick(self, external_delta_time: Optional[float] = ...) -> None:
        """Advance the play mode clock by one tick."""
        ...

    def add_state_change_listener(self, callback: Callable[[PlayModeEvent], None]) -> None:
        """Register a callback for play mode state changes."""
        ...
    def remove_state_change_listener(self, callback: Callable[[PlayModeEvent], None]) -> None:
        """Unregister a state change callback."""
        ...
    def clear_runtime_hidden_object_ids(self) -> None: ...
    def register_runtime_hidden_object(self, game_object: Any) -> None: ...
    def get_runtime_hidden_object_ids(self) -> set[int]: ...
    def add_runtime_hidden_listener(self, callback: Callable[[], None]) -> None: ...
    def remove_runtime_hidden_listener(self, callback: Callable[[], None]) -> None: ...
    def reload_components_from_script(
        self,
        file_path: str,
        *,
        source: bytes | str | None = ...,
        code: CodeType | None = ...,
    ) -> int: ...
    def reload_components_from_script_result(
        self,
        file_path: str,
        *,
        source: bytes | str | None = ...,
        code: CodeType | None = ...,
    ) -> ScriptReloadOutcome:
        """Hot-reload components and report whether the candidate was applied."""
        ...
    def prepare_script_reload_batch(
        self,
        revisions: Iterable[ScriptReloadBatchInput],
    ) -> ScriptReloadBatch: ...
    def commit_script_reload_batch(
        self,
        batch: ScriptReloadBatch,
    ) -> ScriptReloadOutcome: ...
    def rollback_script_reload_batch(self, batch: ScriptReloadBatch) -> None: ...
    def finalize_script_reload_batch(self, batch: ScriptReloadBatch) -> None: ...
    def prepare_edit_script_reload_batch(
        self,
        revisions: Iterable[ScriptReloadBatchInput],
    ) -> ScriptReloadBatch: ...
    def commit_edit_script_reload_batch(self, batch: ScriptReloadBatch) -> int: ...
    def rollback_edit_script_reload_batch(self, batch: ScriptReloadBatch) -> None: ...
    def prepare_script_delete_batch(
        self,
        script_guid: str,
        file_path: str,
    ) -> ScriptDeleteBatch: ...
    def commit_script_delete_batch(self, batch: ScriptDeleteBatch) -> int: ...
    def rollback_script_delete_batch(self, batch: ScriptDeleteBatch) -> None: ...
    def mark_components_missing_for_script(self, script_guid: str, file_path: str) -> int:
        """Replace instances of a deleted script with preserving placeholders."""
        ...
