from __future__ import annotations


def test_preferences_commands_are_non_dirty_and_undoable(monkeypatch):
    from Infernux.engine import i18n, ide_preference
    from Infernux.engine.interaction import CommandSource, EditorInteractionCore
    from Infernux.engine.undo import UndoManager

    state = {"locale": "zh", "ide": "vscode"}
    monkeypatch.setattr(i18n, "get_locale", lambda: state["locale"])
    monkeypatch.setattr(
        i18n,
        "set_locale",
        lambda value: state.__setitem__("locale", value),
    )
    monkeypatch.setattr(ide_preference, "get_ide", lambda: state["ide"])
    monkeypatch.setattr(
        ide_preference,
        "set_ide",
        lambda value: state.__setitem__("ide", value),
    )

    previous_manager = UndoManager.instance()
    core = EditorInteractionCore()
    manager = UndoManager(core.action_journal)
    try:
        locale_result = core.commands.execute(
            "preferences.set_locale",
            source=CommandSource.POINTER,
            payload={"value": "en"},
        )
        ide_result = core.commands.execute(
            "preferences.set_ide",
            source=CommandSource.POINTER,
            payload={"value": "pycharm"},
        )

        assert locale_result.accepted
        assert ide_result.accepted
        assert state == {"locale": "en", "ide": "pycharm"}
        entries = manager.action_journal.applied_entries()
        assert [entry.action.description for entry in entries] == [
            "Set Editor Language",
            "Set Preferred IDE",
        ]
        assert all(entry.action.marks_dirty is False for entry in entries)

        manager.undo()
        assert state == {"locale": "en", "ide": "vscode"}
        manager.undo()
        assert state == {"locale": "zh", "ide": "vscode"}
        manager.redo()
        manager.redo()
        assert state == {"locale": "en", "ide": "pycharm"}
    finally:
        core.shutdown()
        UndoManager._instance = previous_manager


def test_preferences_commands_reject_unknown_values():
    from Infernux.engine.interaction import CommandSource, EditorInteractionCore

    core = EditorInteractionCore()
    try:
        context = core.commands.context(
            CommandSource.POINTER,
            {"value": "unknown"},
        )
        assert not core.commands.can_execute("preferences.set_locale", context)
        assert not core.commands.can_execute("preferences.set_ide", context)
    finally:
        core.shutdown()


def test_shortcut_profile_commands_publish_router_and_share_global_history():
    import copy

    from Infernux.engine.interaction import (
        CommandSource,
        EditorInteractionCore,
        KeyChord,
        ShortcutBinding,
    )
    from Infernux.engine.undo import UndoManager

    persisted = {"value": None}

    def load():
        return copy.deepcopy(persisted["value"])

    def save(value):
        persisted["value"] = copy.deepcopy(value)

    previous_manager = UndoManager.instance()
    core = EditorInteractionCore()
    manager = UndoManager(core.action_journal)
    defaults = (
        ShortcutBinding(
            "file.save",
            KeyChord.parse("Ctrl+S"),
            binding_id="default.file.save",
        ),
        ShortcutBinding(
            "file.save_as",
            KeyChord.parse("Ctrl+Shift+S"),
            binding_id="default.file.save_as",
        ),
    )
    try:
        model = core.preferences.bind_shortcuts(
            defaults,
            core.shortcuts,
            load=load,
            save=save,
        )
        assert tuple(binding.chord for binding in core.shortcuts.bindings) == (
            KeyChord.parse("Ctrl+S"),
            KeyChord.parse("Ctrl+Shift+S"),
        )

        assert core.commands.execute(
            "preferences.shortcut.create_profile",
            source=CommandSource.POINTER,
            payload={"name": "Custom", "profile_id": "custom"},
        ).accepted
        assert core.commands.execute(
            "preferences.shortcut.activate_profile",
            source=CommandSource.POINTER,
            payload={"profile_id": "custom"},
        ).accepted
        assert core.commands.execute(
            "preferences.shortcut.assign",
            source=CommandSource.POINTER,
            payload={"binding_id": "default.file.save", "chord": "Alt+S"},
        ).accepted

        assert model.binding("default.file.save").effective_chord == KeyChord.parse(
            "Alt+S"
        )
        assert tuple(binding.chord for binding in core.shortcuts.bindings) == (
            KeyChord.parse("Alt+S"),
            KeyChord.parse("Ctrl+Shift+S"),
        )
        assert manager.action_journal.applied_entries()[-1].action.marks_dirty is False

        manager.undo()
        assert model.binding("default.file.save").effective_chord == KeyChord.parse(
            "Ctrl+S"
        )
        assert core.shortcuts.bindings[0].chord == KeyChord.parse("Ctrl+S")
        manager.redo()
        assert core.shortcuts.bindings[0].chord == KeyChord.parse("Alt+S")
        assert persisted["value"] == model.to_json_data()
    finally:
        core.shutdown()
        UndoManager._instance = previous_manager


def test_shortcut_profile_command_rejects_conflict_without_partial_publication():
    from Infernux.engine.interaction import (
        CommandSource,
        CommandStatus,
        EditorInteractionCore,
        KeyChord,
        ShortcutBinding,
    )

    core = EditorInteractionCore()
    defaults = (
        ShortcutBinding(
            "file.save",
            KeyChord.parse("Ctrl+S"),
            binding_id="default.file.save",
        ),
        ShortcutBinding(
            "file.save_as",
            KeyChord.parse("Ctrl+Shift+S"),
            binding_id="default.file.save_as",
        ),
    )
    try:
        model = core.preferences.bind_shortcuts(
            defaults,
            core.shortcuts,
            load=lambda: None,
            save=lambda _value: None,
        )
        core.preferences.create_shortcut_profile("Custom", profile_id="custom")
        core.preferences.activate_shortcut_profile("custom")
        before = model.snapshot

        result = core.commands.execute(
            "preferences.shortcut.assign",
            source=CommandSource.POINTER,
            payload={
                "binding_id": "default.file.save",
                "chord": "Ctrl+Shift+S",
            },
        )

        assert result.status is CommandStatus.FAILED
        assert "conflicts with" in result.message
        assert model.snapshot == before
        assert tuple(core.shortcuts.bindings) == defaults
    finally:
        core.shutdown()
