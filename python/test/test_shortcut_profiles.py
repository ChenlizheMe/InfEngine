from __future__ import annotations

from dataclasses import FrozenInstanceError
import copy

import pytest

from Infernux.engine.interaction.shortcut_profiles import (
    DEFAULT_PROFILE_ID,
    SHORTCUT_PROFILES_SCHEMA,
    ShortcutProfileDiffKind,
    ShortcutProfileModel,
)
from Infernux.engine.interaction.shortcuts import (
    KeyChord,
    ShortcutBinding,
    ShortcutPhase,
    ShortcutRouter,
    ShortcutScope,
)


def _bindings() -> tuple[ShortcutBinding, ...]:
    return (
        ShortcutBinding(
            "file.save",
            KeyChord.parse("Ctrl+S"),
            binding_id="file.save.global",
        ),
        ShortcutBinding(
            "edit.delete",
            KeyChord.parse("Delete"),
            ShortcutScope.PANEL,
            "hierarchy",
            priority=10,
            binding_id="hierarchy.delete",
        ),
        ShortcutBinding(
            "edit.rename",
            KeyChord.parse("F2"),
            ShortcutScope.PANEL,
            "hierarchy",
            priority=10,
            binding_id="hierarchy.rename",
        ),
    )


class _MemoryPersistence:
    def __init__(self, payload=None):
        self.payload = copy.deepcopy(payload)
        self.saved: list[dict] = []

    def load(self):
        return copy.deepcopy(self.payload)

    def save(self, payload):
        self.payload = copy.deepcopy(payload)
        self.saved.append(copy.deepcopy(payload))


def _model(*, persistence: _MemoryPersistence | None = None):
    persistence = persistence or _MemoryPersistence()
    return (
        ShortcutProfileModel(
            _bindings(),
            load=persistence.load,
            save=persistence.save,
        ),
        persistence,
    )


def test_default_snapshot_is_read_only_and_uses_stable_binding_identity():
    model, _persistence = _model()

    snapshot = model.snapshot

    assert snapshot.active_profile_id == DEFAULT_PROFILE_ID
    assert snapshot.profile(DEFAULT_PROFILE_ID).is_default is True
    assert tuple(binding.binding_id for binding in snapshot.bindings) == (
        "file.save.global",
        "hierarchy.delete",
        "hierarchy.rename",
    )
    assert snapshot.binding("file.save.global").default_chord == KeyChord.parse(
        "Ctrl+S"
    )
    with pytest.raises(FrozenInstanceError):
        snapshot.active_profile_id = "changed"
    with pytest.raises(AttributeError):
        snapshot.profiles.append("changed")


def test_profile_crud_activation_and_diff_snapshots_are_exact():
    model, persistence = _model()

    created = model.create_profile("  Maya  ", profile_id="maya")
    assert created.kind is ShortcutProfileDiffKind.CREATE_PROFILE
    assert created.before.active_profile_id == DEFAULT_PROFILE_ID
    assert created.after.profile("maya").name == "Maya"
    assert model.revision == 1

    activated = model.set_active_profile("maya")
    assert activated is not None
    assert activated.kind is ShortcutProfileDiffKind.ACTIVATE_PROFILE
    assert model.active_profile_id == "maya"

    renamed = model.rename_profile("maya", "Maya 2026")
    assert renamed is not None
    assert renamed.kind is ShortcutProfileDiffKind.RENAME_PROFILE
    assert model.profile("maya").name == "Maya 2026"

    deleted = model.delete_profile("maya")
    assert deleted.kind is ShortcutProfileDiffKind.DELETE_PROFILE
    assert model.active_profile_id == DEFAULT_PROFILE_ID
    assert tuple(profile.profile_id for profile in model.snapshot.profiles) == (
        DEFAULT_PROFILE_ID,
    )
    assert len(persistence.saved) == 4


def test_default_profile_is_read_only_for_every_mutating_operation():
    model, _persistence = _model()

    with pytest.raises(PermissionError, match="read-only"):
        model.rename_profile(DEFAULT_PROFILE_ID, "Other")
    with pytest.raises(PermissionError, match="read-only"):
        model.delete_profile(DEFAULT_PROFILE_ID)
    with pytest.raises(PermissionError, match="read-only"):
        model.assign("file.save.global", "Ctrl+Shift+S")
    with pytest.raises(PermissionError, match="read-only"):
        model.reset_binding("file.save.global")
    with pytest.raises(PermissionError, match="read-only"):
        model.reset_profile()


def test_assign_disable_reset_binding_and_reset_profile():
    model, persistence = _model()
    model.create_profile("Custom", profile_id="custom")
    model.set_active_profile("custom")

    assigned = model.assign("file.save.global", "Ctrl+Shift+S")
    assert assigned is not None
    assert assigned.kind is ShortcutProfileDiffKind.ASSIGN_BINDING
    binding = model.binding("file.save.global")
    assert binding.default_chord == KeyChord.parse("Ctrl+S")
    assert binding.effective_chord == KeyChord.parse("Ctrl+Shift+S")
    assert binding.overridden is True
    assert binding.disabled is False

    disabled = model.assign("hierarchy.delete", None)
    assert disabled is not None
    assert model.binding("hierarchy.delete").disabled is True
    assert tuple(
        binding.binding_id for binding in model.effective_bindings()
    ) == ("file.save.global", "hierarchy.rename")

    reset = model.reset_binding("file.save.global")
    assert reset is not None
    assert reset.kind is ShortcutProfileDiffKind.RESET_BINDING
    assert model.binding("file.save.global").effective_chord == KeyChord.parse(
        "Ctrl+S"
    )
    assert model.binding("file.save.global").overridden is False

    reset_all = model.reset_profile()
    assert reset_all is not None
    assert reset_all.kind is ShortcutProfileDiffKind.RESET_PROFILE
    assert model.profile("custom").overrides == ()
    assert len(persistence.saved) == 6


def test_assigning_default_chord_normalizes_to_reset():
    model, _persistence = _model()
    model.create_profile("Custom", profile_id="custom")
    model.set_active_profile("custom")
    model.assign("file.save.global", "Ctrl+Shift+S")

    diff = model.assign("file.save.global", "Ctrl+S")

    assert diff is not None
    assert diff.kind is ShortcutProfileDiffKind.RESET_BINDING
    assert model.profile("custom").overrides == ()


def test_no_op_operations_do_not_increment_revision_or_persist():
    model, persistence = _model()
    model.create_profile("Custom", profile_id="custom")
    model.set_active_profile("custom")
    baseline_revision = model.revision
    baseline_saves = len(persistence.saved)

    assert model.set_active_profile("custom") is None
    assert model.rename_profile("custom", "Custom") is None
    assert model.assign("file.save.global", "Ctrl+S") is None
    assert model.reset_binding("file.save.global") is None
    assert model.reset_profile() is None

    assert model.revision == baseline_revision
    assert len(persistence.saved) == baseline_saves


def test_profile_and_binding_inputs_are_strict():
    model, _persistence = _model()
    model.create_profile("Custom", profile_id="custom")

    with pytest.raises(ValueError, match="must not be empty"):
        model.create_profile(" ")
    with pytest.raises(ValueError, match="already exists"):
        model.create_profile("custom", profile_id="other")
    with pytest.raises(ValueError, match="already exists"):
        model.create_profile("Other", profile_id="custom")
    with pytest.raises(KeyError, match="unknown shortcut profile"):
        model.set_active_profile("missing")
    with pytest.raises(KeyError, match="unknown shortcut binding"):
        model.assign("missing", "F1", profile_id="custom")
    with pytest.raises(ValueError, match="exactly one"):
        model.assign("file.save.global", "Ctrl+S+Z", profile_id="custom")
    with pytest.raises(TypeError, match="KeyChord"):
        model.assign("file.save.global", 12, profile_id="custom")


def test_conflicts_match_shortcut_router_for_same_effective_bindings():
    model, _persistence = _model()
    model.create_profile("Custom", profile_id="custom")
    model.set_active_profile("custom")
    model.assign("hierarchy.rename", "Delete")

    profile_conflicts = model.conflicts_for("hierarchy.rename")
    router = ShortcutRouter()
    for binding in model.effective_bindings():
        router.register(binding)
    router_conflicts = router.conflicts_for(
        next(
            binding
            for binding in model.effective_bindings()
            if binding.binding_id == "hierarchy.rename"
        )
    )

    assert profile_conflicts == router_conflicts
    assert tuple(binding.binding_id for binding in profile_conflicts) == (
        "hierarchy.delete",
    )


@pytest.mark.parametrize(
    ("field", "different_value"),
    [
        ("phase", ShortcutPhase.RELEASE),
        ("scope", ShortcutScope.CHILD_CONTEXT),
        ("owner_id", "inspector"),
        ("priority", 11),
    ],
)
def test_conflicts_require_phase_scope_owner_and_priority_to_match(
    field, different_value
):
    base = _bindings()[1]
    values = {
        "phase": base.phase,
        "scope": base.scope,
        "owner_id": base.owner_id,
        "priority": base.priority,
    }
    values[field] = different_value
    bindings = (
        base,
        ShortcutBinding(
            "other.command",
            KeyChord.parse("Delete"),
            values["scope"],
            values["owner_id"],
            phase=values["phase"],
            priority=values["priority"],
            binding_id=f"other.{field}",
        ),
    )
    model = ShortcutProfileModel(bindings)

    assert model.conflicts_for(base.binding_id) == ()


def test_disabled_bindings_and_disabled_candidate_never_conflict():
    model, _persistence = _model()
    model.create_profile("Custom", profile_id="custom")
    model.set_active_profile("custom")
    model.assign("hierarchy.delete", None)

    assert model.conflicts_for("hierarchy.delete") == ()
    assert model.conflicts_for("hierarchy.rename", None) == ()


def test_persistence_round_trip_saves_defaults_overrides_and_active_profile():
    first, persistence = _model()
    first.create_profile("Custom", profile_id="custom")
    first.set_active_profile("custom")
    first.assign("file.save.global", "Alt+S")
    first.assign("hierarchy.delete", None)

    payload = persistence.payload
    assert payload["schema"] == SHORTCUT_PROFILES_SCHEMA
    assert "version" not in payload
    assert payload["active_profile_id"] == "custom"
    assert payload["defaults"] == [
        {"binding_id": "file.save.global", "chord": "Ctrl+S"},
        {"binding_id": "hierarchy.delete", "chord": "DELETE"},
        {"binding_id": "hierarchy.rename", "chord": "F2"},
    ]
    assert payload["profiles"][0]["overrides"] == [
        {
            "binding_id": "file.save.global",
            "state": "assigned",
            "chord": "Alt+S",
        },
        {
            "binding_id": "hierarchy.delete",
            "state": "disabled",
            "chord": None,
        },
    ]

    second = ShortcutProfileModel(_bindings(), load=persistence.load)
    assert second.snapshot == first.snapshot
    assert second.to_json_data() == payload


def _valid_payload():
    model = ShortcutProfileModel(_bindings())
    payload = model.to_json_data()
    payload["profiles"] = [
        {"profile_id": "custom", "name": "Custom", "overrides": []}
    ]
    payload["active_profile_id"] = "custom"
    return payload


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(extra=True),
        lambda data: data.update(schema="other.shortcut_profiles"),
        lambda data: data.update(version=0),
        lambda data: data.update(version=True),
        lambda data: data.update(active_profile_id="missing"),
        lambda data: data["defaults"].pop(),
        lambda data: data["defaults"][0].update(chord="control+s"),
        lambda data: data["profiles"][0].update(extra=True),
        lambda data: data["profiles"].append(copy.deepcopy(data["profiles"][0])),
        lambda data: data["profiles"][0]["overrides"].append(
            {"binding_id": "missing", "state": "disabled", "chord": None}
        ),
        lambda data: data["profiles"][0]["overrides"].append(
            {
                "binding_id": "file.save.global",
                "state": "assigned",
                "chord": "Ctrl+S",
            }
        ),
        lambda data: data["profiles"][0]["overrides"].append(
            {
                "binding_id": "file.save.global",
                "state": "disabled",
                "chord": "F1",
            }
        ),
    ],
)
def test_persistence_schema_rejects_invalid_or_legacy_documents(mutate):
    payload = _valid_payload()
    mutate(payload)

    with pytest.raises((TypeError, ValueError)):
        ShortcutProfileModel(_bindings(), load=lambda: payload)


def test_saved_payload_and_public_json_are_detached_from_internal_state():
    model, persistence = _model()
    model.create_profile("Custom", profile_id="custom")
    public_payload = model.to_json_data()

    public_payload["profiles"][0]["name"] = "Corrupted"
    persistence.payload["profiles"][0]["name"] = "Also Corrupted"

    assert model.profile("custom").name == "Custom"


def test_save_failure_is_transactional():
    def fail(_payload):
        raise OSError("disk unavailable")

    model = ShortcutProfileModel(_bindings(), save=fail)
    before = model.snapshot

    with pytest.raises(OSError, match="disk unavailable"):
        model.create_profile("Custom", profile_id="custom")

    assert model.snapshot == before
    assert model.revision == 0


def test_exact_snapshot_restore_supports_global_undo_redo():
    model, persistence = _model()
    before = model.snapshot
    model.create_profile("Custom", profile_id="custom")
    model.set_active_profile("custom")
    model.assign("file.save.global", "Alt+S")
    after = model.snapshot

    assert model.restore_snapshot(before) is True
    assert model.snapshot == before
    assert model.restore_snapshot(after) is True
    assert model.snapshot == after
    assert model.restore_snapshot(after) is False
    assert persistence.payload == model.to_json_data()


def test_snapshot_restore_rejects_foreign_binding_defaults():
    model, _persistence = _model()
    foreign = ShortcutProfileModel(
        (
            ShortcutBinding(
                "file.save",
                KeyChord.parse("Alt+S"),
                binding_id="file.save.global",
            ),
            *_bindings()[1:],
        )
    ).snapshot

    with pytest.raises(ValueError, match="current binding defaults"):
        model.restore_snapshot(foreign)


def test_constructor_rejects_duplicate_binding_identity_and_bad_callables():
    duplicate = ShortcutBinding(
        "other.save",
        KeyChord.parse("Alt+S"),
        binding_id="file.save.global",
    )
    with pytest.raises(ValueError, match="must be unique"):
        ShortcutProfileModel((*_bindings(), duplicate))
    with pytest.raises(TypeError, match="load must be callable"):
        ShortcutProfileModel(_bindings(), load=object())
    with pytest.raises(TypeError, match="save must be callable"):
        ShortcutProfileModel(_bindings(), save=object())
