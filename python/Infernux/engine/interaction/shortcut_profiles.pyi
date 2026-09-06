from collections.abc import Callable, Iterable
from enum import Enum
from typing import Any

from .shortcuts import KeyChord, ShortcutBinding

SHORTCUT_PROFILES_SCHEMA: str
DEFAULT_PROFILE_ID: str
DEFAULT_PROFILE_NAME: str

class ShortcutProfileDiffKind(str, Enum):
    CREATE_PROFILE: ShortcutProfileDiffKind
    RENAME_PROFILE: ShortcutProfileDiffKind
    DELETE_PROFILE: ShortcutProfileDiffKind
    ACTIVATE_PROFILE: ShortcutProfileDiffKind
    ASSIGN_BINDING: ShortcutProfileDiffKind
    RESET_BINDING: ShortcutProfileDiffKind
    RESET_PROFILE: ShortcutProfileDiffKind

class ShortcutOverrideSnapshot:
    binding_id: str
    chord: KeyChord | None
    def __init__(self, binding_id: str, chord: KeyChord | None) -> None: ...
    @property
    def disabled(self) -> bool: ...

class ShortcutProfileSnapshot:
    profile_id: str
    name: str
    is_default: bool
    overrides: tuple[ShortcutOverrideSnapshot, ...]
    def __init__(
        self,
        profile_id: str,
        name: str,
        is_default: bool,
        overrides: tuple[ShortcutOverrideSnapshot, ...] = ...,
    ) -> None: ...
    def override_for(self, binding_id: str) -> ShortcutOverrideSnapshot | None: ...

class ShortcutBindingSnapshot:
    binding_id: str
    command_id: str
    default_chord: KeyChord
    effective_chord: KeyChord | None
    overridden: bool
    disabled: bool
    def __init__(
        self,
        binding_id: str,
        command_id: str,
        default_chord: KeyChord,
        effective_chord: KeyChord | None,
        overridden: bool,
        disabled: bool,
    ) -> None: ...

class ShortcutProfilesSnapshot:
    active_profile_id: str
    profiles: tuple[ShortcutProfileSnapshot, ...]
    bindings: tuple[ShortcutBindingSnapshot, ...]
    def __init__(
        self,
        active_profile_id: str,
        profiles: tuple[ShortcutProfileSnapshot, ...],
        bindings: tuple[ShortcutBindingSnapshot, ...],
    ) -> None: ...
    def profile(self, profile_id: str) -> ShortcutProfileSnapshot: ...
    def binding(self, binding_id: str) -> ShortcutBindingSnapshot: ...

class ShortcutProfileDiff:
    kind: ShortcutProfileDiffKind
    before: ShortcutProfilesSnapshot
    after: ShortcutProfilesSnapshot
    profile_id: str
    binding_id: str
    def __init__(
        self,
        kind: ShortcutProfileDiffKind,
        before: ShortcutProfilesSnapshot,
        after: ShortcutProfilesSnapshot,
        profile_id: str,
        binding_id: str = ...,
    ) -> None: ...

class ShortcutProfileModel:
    def __init__(
        self,
        default_bindings: Iterable[ShortcutBinding],
        *,
        load: Callable[[], object] | None = ...,
        save: Callable[[dict[str, Any]], None] | None = ...,
    ) -> None: ...
    @property
    def revision(self) -> int: ...
    @property
    def active_profile_id(self) -> str: ...
    @property
    def snapshot(self) -> ShortcutProfilesSnapshot: ...
    def profile(self, profile_id: str) -> ShortcutProfileSnapshot: ...
    def binding(
        self,
        binding_id: str,
        *,
        profile_id: str | None = ...,
    ) -> ShortcutBindingSnapshot: ...
    def effective_bindings(
        self,
        profile_id: str | None = ...,
    ) -> tuple[ShortcutBinding, ...]: ...
    def conflicts_for(
        self,
        binding_id: str,
        chord: KeyChord | str | None | object = ...,
        *,
        profile_id: str | None = ...,
    ) -> tuple[ShortcutBinding, ...]: ...
    def create_profile(
        self,
        name: str,
        *,
        profile_id: str | None = ...,
    ) -> ShortcutProfileDiff: ...
    def rename_profile(
        self,
        profile_id: str,
        name: str,
    ) -> ShortcutProfileDiff | None: ...
    def delete_profile(self, profile_id: str) -> ShortcutProfileDiff: ...
    def set_active_profile(
        self,
        profile_id: str,
    ) -> ShortcutProfileDiff | None: ...
    def assign(
        self,
        binding_id: str,
        chord: KeyChord | str | None,
        *,
        profile_id: str | None = ...,
    ) -> ShortcutProfileDiff | None: ...
    def reset_binding(
        self,
        binding_id: str,
        *,
        profile_id: str | None = ...,
    ) -> ShortcutProfileDiff | None: ...
    def reset_profile(
        self,
        profile_id: str | None = ...,
    ) -> ShortcutProfileDiff | None: ...
    def to_json_data(self) -> dict[str, Any]: ...
    def restore_snapshot(self, snapshot: ShortcutProfilesSnapshot) -> bool: ...

__all__: list[str]
