"""Immutable core model for editor shortcut profiles.

The model deliberately has no UI or preferences-store dependency.  Shortcut
definitions come from the current :class:`ShortcutBinding` registry and keep
their stable ``binding_id`` identity.  User profiles only store chord
overrides (including an explicit disabled state), so every routing property
continues to be owned by the default binding definition.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import Any
import uuid

from .shortcuts import KeyChord, ShortcutBinding


SHORTCUT_PROFILES_SCHEMA = "infernux.shortcut_profiles"
DEFAULT_PROFILE_ID = "default"
DEFAULT_PROFILE_NAME = "Default"


class ShortcutProfileDiffKind(str, Enum):
    CREATE_PROFILE = "create_profile"
    RENAME_PROFILE = "rename_profile"
    DELETE_PROFILE = "delete_profile"
    ACTIVATE_PROFILE = "activate_profile"
    ASSIGN_BINDING = "assign_binding"
    RESET_BINDING = "reset_binding"
    RESET_PROFILE = "reset_profile"


@dataclass(frozen=True, slots=True)
class ShortcutOverrideSnapshot:
    """One explicit user override.

    ``chord is None`` is an intentional disabled override.  Absence from a
    profile's ``overrides`` tuple means that the default chord is inherited.
    """

    binding_id: str
    chord: KeyChord | None

    @property
    def disabled(self) -> bool:
        return self.chord is None


@dataclass(frozen=True, slots=True)
class ShortcutProfileSnapshot:
    profile_id: str
    name: str
    is_default: bool
    overrides: tuple[ShortcutOverrideSnapshot, ...] = ()

    def override_for(self, binding_id: str) -> ShortcutOverrideSnapshot | None:
        identity = str(binding_id or "").strip()
        return next(
            (override for override in self.overrides if override.binding_id == identity),
            None,
        )


@dataclass(frozen=True, slots=True)
class ShortcutBindingSnapshot:
    binding_id: str
    command_id: str
    default_chord: KeyChord
    effective_chord: KeyChord | None
    overridden: bool
    disabled: bool


@dataclass(frozen=True, slots=True)
class ShortcutProfilesSnapshot:
    active_profile_id: str
    profiles: tuple[ShortcutProfileSnapshot, ...]
    bindings: tuple[ShortcutBindingSnapshot, ...]

    def profile(self, profile_id: str) -> ShortcutProfileSnapshot:
        identity = str(profile_id or "").strip()
        for profile in self.profiles:
            if profile.profile_id == identity:
                return profile
        raise KeyError(f"unknown shortcut profile: {identity}")

    def binding(self, binding_id: str) -> ShortcutBindingSnapshot:
        identity = str(binding_id or "").strip()
        for binding in self.bindings:
            if binding.binding_id == identity:
                return binding
        raise KeyError(f"unknown shortcut binding: {identity}")


@dataclass(frozen=True, slots=True)
class ShortcutProfileDiff:
    kind: ShortcutProfileDiffKind
    before: ShortcutProfilesSnapshot
    after: ShortcutProfilesSnapshot
    profile_id: str
    binding_id: str = ""

    def __post_init__(self) -> None:
        if self.before == self.after:
            raise ValueError("shortcut profile diff cannot be a no-op")


@dataclass(frozen=True, slots=True)
class _ProfileState:
    profile_id: str
    name: str
    overrides: tuple[ShortcutOverrideSnapshot, ...] = ()

    def override_for(self, binding_id: str) -> ShortcutOverrideSnapshot | None:
        return next(
            (override for override in self.overrides if override.binding_id == binding_id),
            None,
        )

    def with_override(
        self, override: ShortcutOverrideSnapshot | None
    ) -> "_ProfileState":
        binding_id = "" if override is None else override.binding_id
        if override is None:
            raise ValueError("removing an override requires without_override")
        values = [
            current
            for current in self.overrides
            if current.binding_id != binding_id
        ]
        values.append(override)
        return replace(self, overrides=tuple(values))

    def without_override(self, binding_id: str) -> "_ProfileState":
        return replace(
            self,
            overrides=tuple(
                override
                for override in self.overrides
                if override.binding_id != binding_id
            ),
        )


_USE_EFFECTIVE_CHORD = object()


class ShortcutProfileModel:
    """Own current shortcut defaults and user profile overrides.

    ``load`` returns either the strict current JSON object or ``None`` when no
    profile data exists.  ``save`` receives a fresh, detached JSON object after
    every real mutation.  Saving happens before the new state is published, so
    a failing save leaves the model unchanged.
    """

    def __init__(
        self,
        default_bindings: Iterable[ShortcutBinding],
        *,
        load: Callable[[], object] | None = None,
        save: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if load is not None and not callable(load):
            raise TypeError("shortcut profile load must be callable")
        if save is not None and not callable(save):
            raise TypeError("shortcut profile save must be callable")

        defaults = tuple(default_bindings)
        if any(not isinstance(binding, ShortcutBinding) for binding in defaults):
            raise TypeError("shortcut defaults must contain ShortcutBinding values")
        identities = [binding.binding_id for binding in defaults]
        if len(set(identities)) != len(identities):
            raise ValueError("shortcut default binding_id values must be unique")

        self._defaults = defaults
        self._defaults_by_id = {
            binding.binding_id: binding for binding in self._defaults
        }
        self._profiles: dict[str, _ProfileState] = {}
        self._active_profile_id = DEFAULT_PROFILE_ID
        self._save = save
        self._revision = 0

        if load is not None:
            payload = load()
            if payload is not None:
                profiles, active_profile_id = self._decode(payload)
                self._profiles = profiles
                self._active_profile_id = active_profile_id

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def active_profile_id(self) -> str:
        return self._active_profile_id

    @property
    def snapshot(self) -> ShortcutProfilesSnapshot:
        return self._snapshot(self._profiles, self._active_profile_id)

    def profile(self, profile_id: str) -> ShortcutProfileSnapshot:
        return self.snapshot.profile(self._require_profile_id(profile_id))

    def binding(
        self,
        binding_id: str,
        *,
        profile_id: str | None = None,
    ) -> ShortcutBindingSnapshot:
        identity = self._require_binding_id(binding_id)
        target_profile = self._resolve_profile_id(profile_id)
        return self._binding_snapshot(identity, target_profile, self._profiles)

    def effective_bindings(
        self, profile_id: str | None = None
    ) -> tuple[ShortcutBinding, ...]:
        target_profile = self._resolve_profile_id(profile_id)
        result: list[ShortcutBinding] = []
        for binding in self._defaults:
            chord, disabled = self._effective_chord(
                binding.binding_id,
                target_profile,
                self._profiles,
            )
            if disabled:
                continue
            assert chord is not None
            result.append(replace(binding, chord=chord))
        return tuple(result)

    def conflicts_for(
        self,
        binding_id: str,
        chord: KeyChord | str | None | object = _USE_EFFECTIVE_CHORD,
        *,
        profile_id: str | None = None,
    ) -> tuple[ShortcutBinding, ...]:
        """Return conflicts using exactly ``ShortcutRouter.conflicts_for`` keys."""

        identity = self._require_binding_id(binding_id)
        target_profile = self._resolve_profile_id(profile_id)
        if chord is _USE_EFFECTIVE_CHORD:
            candidate_chord, disabled = self._effective_chord(
                identity,
                target_profile,
                self._profiles,
            )
            if disabled:
                return ()
        else:
            candidate_chord = self._coerce_chord(chord, allow_disabled=True)
            if candidate_chord is None:
                return ()

        assert candidate_chord is not None
        default = self._defaults_by_id[identity]
        candidate = replace(default, chord=candidate_chord)
        conflicts: list[ShortcutBinding] = []
        for other in self.effective_bindings(target_profile):
            if (
                other.binding_id != candidate.binding_id
                and other.chord == candidate.chord
                and other.phase is candidate.phase
                and other.scope is candidate.scope
                and other.owner_id == candidate.owner_id
                and other.priority == candidate.priority
            ):
                conflicts.append(other)
        return tuple(conflicts)

    def create_profile(
        self,
        name: str,
        *,
        profile_id: str | None = None,
    ) -> ShortcutProfileDiff:
        normalized_name = self._normalize_name(name)
        identity = uuid.uuid4().hex if profile_id is None else self._normalize_id(
            profile_id, "profile_id"
        )
        if identity == DEFAULT_PROFILE_ID or identity in self._profiles:
            raise ValueError(f"shortcut profile already exists: {identity}")
        self._require_unique_name(normalized_name)
        profiles = dict(self._profiles)
        profiles[identity] = _ProfileState(identity, normalized_name)
        return self._commit(
            ShortcutProfileDiffKind.CREATE_PROFILE,
            profiles,
            self._active_profile_id,
            profile_id=identity,
        )

    def rename_profile(
        self, profile_id: str, name: str
    ) -> ShortcutProfileDiff | None:
        identity = self._require_user_profile_id(profile_id)
        normalized_name = self._normalize_name(name)
        current = self._profiles[identity]
        if normalized_name == current.name:
            return None
        self._require_unique_name(normalized_name, excluding=identity)
        profiles = dict(self._profiles)
        profiles[identity] = replace(current, name=normalized_name)
        return self._commit(
            ShortcutProfileDiffKind.RENAME_PROFILE,
            profiles,
            self._active_profile_id,
            profile_id=identity,
        )

    def delete_profile(self, profile_id: str) -> ShortcutProfileDiff:
        identity = self._require_user_profile_id(profile_id)
        profiles = dict(self._profiles)
        del profiles[identity]
        active = (
            DEFAULT_PROFILE_ID
            if self._active_profile_id == identity
            else self._active_profile_id
        )
        return self._commit(
            ShortcutProfileDiffKind.DELETE_PROFILE,
            profiles,
            active,
            profile_id=identity,
        )

    def set_active_profile(
        self, profile_id: str
    ) -> ShortcutProfileDiff | None:
        identity = self._require_profile_id(profile_id)
        if identity == self._active_profile_id:
            return None
        return self._commit(
            ShortcutProfileDiffKind.ACTIVATE_PROFILE,
            dict(self._profiles),
            identity,
            profile_id=identity,
        )

    def assign(
        self,
        binding_id: str,
        chord: KeyChord | str | None,
        *,
        profile_id: str | None = None,
    ) -> ShortcutProfileDiff | None:
        """Assign a chord, or ``None`` to explicitly disable the binding."""

        identity = self._require_binding_id(binding_id)
        target_profile = self._require_user_profile_id(
            self._resolve_profile_id(profile_id)
        )
        normalized_chord = self._coerce_chord(chord, allow_disabled=True)
        state = self._profiles[target_profile]
        current = state.override_for(identity)

        # Assigning the inherited default is exactly a reset, not a redundant
        # override which could become stale when engine defaults evolve.
        if normalized_chord == self._defaults_by_id[identity].chord:
            if current is None:
                return None
            return self.reset_binding(identity, profile_id=target_profile)

        override = ShortcutOverrideSnapshot(identity, normalized_chord)
        if current == override:
            return None
        profiles = dict(self._profiles)
        profiles[target_profile] = state.with_override(override)
        return self._commit(
            ShortcutProfileDiffKind.ASSIGN_BINDING,
            profiles,
            self._active_profile_id,
            profile_id=target_profile,
            binding_id=identity,
        )

    def reset_binding(
        self,
        binding_id: str,
        *,
        profile_id: str | None = None,
    ) -> ShortcutProfileDiff | None:
        identity = self._require_binding_id(binding_id)
        target_profile = self._require_user_profile_id(
            self._resolve_profile_id(profile_id)
        )
        state = self._profiles[target_profile]
        if state.override_for(identity) is None:
            return None
        profiles = dict(self._profiles)
        profiles[target_profile] = state.without_override(identity)
        return self._commit(
            ShortcutProfileDiffKind.RESET_BINDING,
            profiles,
            self._active_profile_id,
            profile_id=target_profile,
            binding_id=identity,
        )

    def reset_profile(
        self, profile_id: str | None = None
    ) -> ShortcutProfileDiff | None:
        target_profile = self._require_user_profile_id(
            self._resolve_profile_id(profile_id)
        )
        state = self._profiles[target_profile]
        if not state.overrides:
            return None
        profiles = dict(self._profiles)
        profiles[target_profile] = replace(state, overrides=())
        return self._commit(
            ShortcutProfileDiffKind.RESET_PROFILE,
            profiles,
            self._active_profile_id,
            profile_id=target_profile,
        )

    def to_json_data(self) -> dict[str, Any]:
        return self._encode(self._profiles, self._active_profile_id)

    def restore_snapshot(self, snapshot: ShortcutProfilesSnapshot) -> bool:
        """Restore one exact profile snapshot for global Undo/Redo replay."""

        if not isinstance(snapshot, ShortcutProfilesSnapshot):
            raise TypeError("shortcut profile restore requires a profiles snapshot")
        default_profiles = tuple(
            profile for profile in snapshot.profiles if profile.is_default
        )
        if default_profiles != (
            ShortcutProfileSnapshot(
                DEFAULT_PROFILE_ID,
                DEFAULT_PROFILE_NAME,
                True,
            ),
        ):
            raise ValueError("shortcut profile snapshot has an invalid Default profile")

        profiles: dict[str, _ProfileState] = {}
        for profile in snapshot.profiles:
            if profile.is_default:
                continue
            identity = self._normalize_id(profile.profile_id, "profile_id")
            if identity == DEFAULT_PROFILE_ID or identity in profiles:
                raise ValueError(f"duplicate or reserved shortcut profile_id: {identity}")
            name = self._normalize_name(profile.name)
            self._require_unique_name_in_states(name, profiles)
            seen: set[str] = set()
            overrides: list[ShortcutOverrideSnapshot] = []
            for override in profile.overrides:
                if not isinstance(override, ShortcutOverrideSnapshot):
                    raise TypeError("shortcut profile overrides must be immutable snapshots")
                binding_id = self._require_binding_id(override.binding_id)
                if binding_id in seen:
                    raise ValueError(f"duplicate shortcut override binding_id: {binding_id}")
                seen.add(binding_id)
                chord = override.chord
                if chord is not None and not isinstance(chord, KeyChord):
                    raise TypeError("shortcut override chord must be a KeyChord or None")
                if chord == self._defaults_by_id[binding_id].chord:
                    raise ValueError(
                        f"shortcut override {binding_id} redundantly stores its default chord"
                    )
                overrides.append(ShortcutOverrideSnapshot(binding_id, chord))
            profiles[identity] = _ProfileState(identity, name, tuple(overrides))

        active_profile_id = self._normalize_id(
            snapshot.active_profile_id,
            "profile_id",
        )
        if active_profile_id != DEFAULT_PROFILE_ID and active_profile_id not in profiles:
            raise ValueError(
                f"active shortcut profile does not exist: {active_profile_id}"
            )
        normalized = self._snapshot(profiles, active_profile_id)
        if normalized != snapshot:
            raise ValueError("shortcut profile snapshot does not match current binding defaults")
        if normalized == self.snapshot:
            return False
        if self._save is not None:
            self._save(self._encode(profiles, active_profile_id))
        self._profiles = profiles
        self._active_profile_id = active_profile_id
        self._revision += 1
        return True

    def _commit(
        self,
        kind: ShortcutProfileDiffKind,
        profiles: dict[str, _ProfileState],
        active_profile_id: str,
        *,
        profile_id: str,
        binding_id: str = "",
    ) -> ShortcutProfileDiff:
        before = self._snapshot(self._profiles, self._active_profile_id)
        after = self._snapshot(profiles, active_profile_id)
        diff = ShortcutProfileDiff(
            kind,
            before,
            after,
            profile_id,
            binding_id,
        )
        if self._save is not None:
            self._save(self._encode(profiles, active_profile_id))
        self._profiles = profiles
        self._active_profile_id = active_profile_id
        self._revision += 1
        return diff

    def _snapshot(
        self,
        profiles: Mapping[str, _ProfileState],
        active_profile_id: str,
    ) -> ShortcutProfilesSnapshot:
        profile_snapshots = [
            ShortcutProfileSnapshot(
                DEFAULT_PROFILE_ID,
                DEFAULT_PROFILE_NAME,
                True,
            )
        ]
        profile_snapshots.extend(
            ShortcutProfileSnapshot(
                state.profile_id,
                state.name,
                False,
                state.overrides,
            )
            for state in profiles.values()
        )
        bindings = tuple(
            self._binding_snapshot(binding.binding_id, active_profile_id, profiles)
            for binding in self._defaults
        )
        return ShortcutProfilesSnapshot(
            active_profile_id,
            tuple(profile_snapshots),
            bindings,
        )

    def _binding_snapshot(
        self,
        binding_id: str,
        profile_id: str,
        profiles: Mapping[str, _ProfileState],
    ) -> ShortcutBindingSnapshot:
        default = self._defaults_by_id[binding_id]
        override = (
            None
            if profile_id == DEFAULT_PROFILE_ID
            else profiles[profile_id].override_for(binding_id)
        )
        effective = default.chord if override is None else override.chord
        return ShortcutBindingSnapshot(
            binding_id,
            default.command_id,
            default.chord,
            effective,
            override is not None,
            override is not None and override.disabled,
        )

    def _effective_chord(
        self,
        binding_id: str,
        profile_id: str,
        profiles: Mapping[str, _ProfileState],
    ) -> tuple[KeyChord | None, bool]:
        snapshot = self._binding_snapshot(binding_id, profile_id, profiles)
        return snapshot.effective_chord, snapshot.disabled

    def _resolve_profile_id(self, profile_id: str | None) -> str:
        return self._require_profile_id(
            self._active_profile_id if profile_id is None else profile_id
        )

    def _require_profile_id(self, profile_id: str) -> str:
        identity = self._normalize_id(profile_id, "profile_id")
        if identity != DEFAULT_PROFILE_ID and identity not in self._profiles:
            raise KeyError(f"unknown shortcut profile: {identity}")
        return identity

    def _require_user_profile_id(self, profile_id: str) -> str:
        identity = self._require_profile_id(profile_id)
        if identity == DEFAULT_PROFILE_ID:
            raise PermissionError("Default shortcut profile is read-only")
        return identity

    def _require_binding_id(self, binding_id: str) -> str:
        identity = self._normalize_id(binding_id, "binding_id")
        if identity not in self._defaults_by_id:
            raise KeyError(f"unknown shortcut binding: {identity}")
        return identity

    @staticmethod
    def _normalize_id(value: object, field_name: str) -> str:
        if not isinstance(value, str):
            raise TypeError(f"shortcut {field_name} must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError(f"shortcut {field_name} must not be empty")
        return normalized

    @staticmethod
    def _normalize_name(value: object) -> str:
        if not isinstance(value, str):
            raise TypeError("shortcut profile name must be a string")
        normalized = value.strip()
        if not normalized:
            raise ValueError("shortcut profile name must not be empty")
        return normalized

    def _require_unique_name(self, name: str, *, excluding: str = "") -> None:
        folded = name.casefold()
        if folded == DEFAULT_PROFILE_NAME.casefold():
            raise ValueError(f"shortcut profile name already exists: {name}")
        for profile_id, state in self._profiles.items():
            if profile_id != excluding and state.name.casefold() == folded:
                raise ValueError(f"shortcut profile name already exists: {name}")

    @staticmethod
    def _require_unique_name_in_states(
        name: str,
        profiles: Mapping[str, _ProfileState],
    ) -> None:
        folded = name.casefold()
        if folded == DEFAULT_PROFILE_NAME.casefold() or any(
            state.name.casefold() == folded for state in profiles.values()
        ):
            raise ValueError(f"shortcut profile name already exists: {name}")

    @staticmethod
    def _coerce_chord(
        value: KeyChord | str | None | object,
        *,
        allow_disabled: bool,
    ) -> KeyChord | None:
        if value is None:
            if allow_disabled:
                return None
            raise TypeError("shortcut chord must be a KeyChord or string")
        if isinstance(value, KeyChord):
            return value
        if isinstance(value, str):
            return KeyChord.parse(value)
        raise TypeError("shortcut chord must be a KeyChord, string, or None")

    def _encode(
        self,
        profiles: Mapping[str, _ProfileState],
        active_profile_id: str,
    ) -> dict[str, Any]:
        return {
            "schema": SHORTCUT_PROFILES_SCHEMA,
            "active_profile_id": active_profile_id,
            "defaults": [
                {
                    "binding_id": binding.binding_id,
                    "chord": binding.chord.display_name(),
                }
                for binding in self._defaults
            ],
            "profiles": [
                {
                    "profile_id": state.profile_id,
                    "name": state.name,
                    "overrides": [
                        {
                            "binding_id": override.binding_id,
                            "state": "disabled" if override.disabled else "assigned",
                            "chord": (
                                None
                                if override.chord is None
                                else override.chord.display_name()
                            ),
                        }
                        for override in state.overrides
                    ],
                }
                for state in profiles.values()
            ],
        }

    def _decode(
        self, payload: object
    ) -> tuple[dict[str, _ProfileState], str]:
        root = self._require_object(
            payload,
            {"schema", "active_profile_id", "defaults", "profiles"},
            "shortcut profiles",
        )
        if root["schema"] != SHORTCUT_PROFILES_SCHEMA:
            raise ValueError("shortcut profiles schema is invalid")

        defaults = self._require_array(root["defaults"], "shortcut profile defaults")
        persisted_default_ids: set[str] = set()
        for index, value in enumerate(defaults):
            item = self._require_object(
                value,
                {"binding_id", "chord"},
                f"shortcut profile default {index}",
            )
            binding_id = self._normalize_id(item["binding_id"], "binding_id")
            if binding_id in persisted_default_ids:
                raise ValueError(f"duplicate shortcut default binding_id: {binding_id}")
            persisted_default_ids.add(binding_id)
            self._decode_chord(item["chord"], f"shortcut default {binding_id}")
        if persisted_default_ids != set(self._defaults_by_id):
            raise ValueError(
                "shortcut profile defaults must match the current binding_id set"
            )

        profile_values = self._require_array(root["profiles"], "shortcut profiles")
        profiles: dict[str, _ProfileState] = {}
        names = {DEFAULT_PROFILE_NAME.casefold()}
        for index, value in enumerate(profile_values):
            item = self._require_object(
                value,
                {"profile_id", "name", "overrides"},
                f"shortcut profile {index}",
            )
            profile_id = self._normalize_id(item["profile_id"], "profile_id")
            if profile_id == DEFAULT_PROFILE_ID or profile_id in profiles:
                raise ValueError(f"duplicate or reserved shortcut profile_id: {profile_id}")
            name = self._normalize_name(item["name"])
            folded_name = name.casefold()
            if folded_name in names:
                raise ValueError(f"duplicate shortcut profile name: {name}")
            names.add(folded_name)

            override_values = self._require_array(
                item["overrides"], f"shortcut profile {profile_id} overrides"
            )
            overrides: list[ShortcutOverrideSnapshot] = []
            overridden_ids: set[str] = set()
            for override_index, override_value in enumerate(override_values):
                override = self._require_object(
                    override_value,
                    {"binding_id", "state", "chord"},
                    f"shortcut profile {profile_id} override {override_index}",
                )
                binding_id = self._normalize_id(
                    override["binding_id"], "binding_id"
                )
                if binding_id not in self._defaults_by_id:
                    raise ValueError(f"unknown shortcut override binding_id: {binding_id}")
                if binding_id in overridden_ids:
                    raise ValueError(f"duplicate shortcut override binding_id: {binding_id}")
                overridden_ids.add(binding_id)
                state = override["state"]
                if state == "assigned":
                    chord = self._decode_chord(
                        override["chord"],
                        f"shortcut override {binding_id}",
                    )
                    if chord == self._defaults_by_id[binding_id].chord:
                        raise ValueError(
                            f"shortcut override {binding_id} redundantly stores its default chord"
                        )
                elif state == "disabled":
                    if override["chord"] is not None:
                        raise ValueError(
                            f"disabled shortcut override {binding_id} requires null chord"
                        )
                    chord = None
                else:
                    raise ValueError(
                        f"shortcut override {binding_id} state is invalid"
                    )
                overrides.append(ShortcutOverrideSnapshot(binding_id, chord))
            profiles[profile_id] = _ProfileState(profile_id, name, tuple(overrides))

        active_profile_id = self._normalize_id(
            root["active_profile_id"], "active_profile_id"
        )
        if (
            active_profile_id != DEFAULT_PROFILE_ID
            and active_profile_id not in profiles
        ):
            raise ValueError(
                f"active shortcut profile does not exist: {active_profile_id}"
            )
        return profiles, active_profile_id

    @staticmethod
    def _require_object(
        value: object,
        fields: set[str],
        label: str,
    ) -> dict[str, Any]:
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError(
                f"{label} must contain exactly: {', '.join(sorted(fields))}"
            )
        return value

    @staticmethod
    def _require_array(value: object, label: str) -> list[Any]:
        if not isinstance(value, list):
            raise TypeError(f"{label} must be an array")
        return value

    @staticmethod
    def _decode_chord(value: object, label: str) -> KeyChord:
        if not isinstance(value, str):
            raise TypeError(f"{label} chord must be a string")
        chord = KeyChord.parse(value)
        if chord.display_name() != value:
            raise ValueError(f"{label} chord must use canonical spelling")
        return chord


__all__ = [
    "DEFAULT_PROFILE_ID",
    "DEFAULT_PROFILE_NAME",
    "SHORTCUT_PROFILES_SCHEMA",
    "ShortcutBindingSnapshot",
    "ShortcutOverrideSnapshot",
    "ShortcutProfileDiff",
    "ShortcutProfileDiffKind",
    "ShortcutProfileModel",
    "ShortcutProfileSnapshot",
    "ShortcutProfilesSnapshot",
]
