from __future__ import annotations

import os

import pytest

from Infernux.engine.interaction import EditorActionJournal
from Infernux.engine.undo import ProjectAssetRenameCommand, UndoManager


def _filesystem_move(source: str, destination: str, _database):
    os.replace(source, destination)
    return destination


def test_project_asset_rename_command_replays_both_directions(tmp_path):
    old_path = tmp_path / "Old.mat"
    new_path = tmp_path / "New.mat"
    old_path.write_text("material", encoding="utf-8")
    changed = []
    command = ProjectAssetRenameCommand(
        str(old_path),
        str(new_path),
        move_fn=_filesystem_move,
        on_changed=lambda: changed.append(True),
    )

    command.execute()
    assert new_path.read_text(encoding="utf-8") == "material"
    assert not old_path.exists()

    command.undo()
    assert old_path.read_text(encoding="utf-8") == "material"
    assert not new_path.exists()

    command.redo()
    assert new_path.exists()
    assert changed == [True, True, True]


def test_project_asset_rename_refuses_to_overwrite_external_change(tmp_path):
    old_path = tmp_path / "Old.mat"
    new_path = tmp_path / "New.mat"
    old_path.write_text("original", encoding="utf-8")
    new_path.write_text("external", encoding="utf-8")
    command = ProjectAssetRenameCommand(
        str(old_path),
        str(new_path),
        move_fn=_filesystem_move,
    )

    with pytest.raises(RuntimeError, match="external change"):
        command.execute()

    assert old_path.read_text(encoding="utf-8") == "original"
    assert new_path.read_text(encoding="utf-8") == "external"


def test_project_asset_rename_rejects_cross_directory_use(tmp_path):
    source_dir = tmp_path / "Source"
    destination_dir = tmp_path / "Destination"
    source_dir.mkdir()
    destination_dir.mkdir()

    with pytest.raises(ValueError, match="between directories"):
        ProjectAssetRenameCommand(
            str(source_dir / "Old.mat"),
            str(destination_dir / "New.mat"),
            move_fn=_filesystem_move,
        )


def test_failed_asset_rename_undo_keeps_global_cursor_and_files(tmp_path):
    old_path = tmp_path / "Old.mat"
    new_path = tmp_path / "New.mat"
    old_path.write_text("original", encoding="utf-8")
    command = ProjectAssetRenameCommand(
        str(old_path),
        str(new_path),
        move_fn=_filesystem_move,
    )
    command.execute()
    old_path.write_text("external", encoding="utf-8")
    manager = UndoManager(EditorActionJournal())
    manager.record(command)

    manager.undo()

    assert manager.action_journal.cursor == 1
    assert old_path.read_text(encoding="utf-8") == "external"
    assert new_path.read_text(encoding="utf-8") == "original"
