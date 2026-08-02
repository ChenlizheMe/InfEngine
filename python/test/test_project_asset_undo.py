from __future__ import annotations

import os
import shutil

import pytest

from Infernux.engine.interaction import EditorActionJournal
from Infernux.engine.undo import (
    ProjectAssetCopyCommand,
    ProjectAssetDeleteCommand,
    ProjectAssetMoveCommand,
    ProjectAssetPasteCommand,
    ProjectAssetRenameCommand,
    UndoManager,
)
from Infernux.engine.ui.project_file_ops import plan_asset_paste


def _filesystem_move(source: str, destination: str, _database):
    os.replace(source, destination)
    return destination


def _filesystem_delete(path: str, _database):
    if os.path.isdir(path):
        shutil.rmtree(path)
    else:
        os.remove(path)
    meta = path + ".meta"
    if os.path.exists(meta):
        os.remove(meta)
    return True


def _successful_import(_path: str, _database):
    return True


def _filesystem_copy(source: str, destination: str, _database):
    shutil.copy2(source, destination)
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


def test_project_asset_delete_preserves_file_and_guid_meta_across_replay(tmp_path):
    asset = tmp_path / "Assets" / "Smoke.mat"
    asset.parent.mkdir()
    asset.write_text("material", encoding="utf-8")
    meta = asset.with_name(asset.name + ".meta")
    meta.write_text('{"guid":"stable-guid"}', encoding="utf-8")
    backup_root = tmp_path / "Library" / "EditorUndo"
    changed = []
    command = ProjectAssetDeleteCommand(
        [str(asset)],
        project_root=str(tmp_path),
        backup_root=str(backup_root),
        delete_fn=_filesystem_delete,
        import_fn=_successful_import,
        on_deleted=lambda: changed.append("deleted"),
        on_restored=lambda: changed.append("restored"),
    )

    command.execute()
    assert not asset.exists()
    assert not meta.exists()

    command.undo()
    assert asset.read_text(encoding="utf-8") == "material"
    assert meta.read_text(encoding="utf-8") == '{"guid":"stable-guid"}'

    command.redo()
    assert not asset.exists()
    assert changed == ["deleted", "restored", "deleted"]

    command.dispose()
    assert not any(backup_root.glob("asset-delete-*"))


def test_project_asset_delete_deduplicates_nested_selected_roots(tmp_path):
    folder = tmp_path / "Assets" / "Folder"
    nested = folder / "Nested.mat"
    folder.mkdir(parents=True)
    nested.write_text("nested", encoding="utf-8")
    nested_meta = nested.with_name(nested.name + ".meta")
    nested_meta.write_text("guid", encoding="utf-8")
    imported = []
    command = ProjectAssetDeleteCommand(
        [str(folder), str(nested)],
        project_root=str(tmp_path),
        delete_fn=_filesystem_delete,
        import_fn=lambda path, _database: imported.append(path) or True,
    )

    command.execute()
    command.undo()

    assert nested.read_text(encoding="utf-8") == "nested"
    assert nested_meta.read_text(encoding="utf-8") == "guid"
    assert imported == [str(nested)]
    command.dispose()


def test_project_asset_delete_rolls_back_batch_failure(tmp_path):
    assets = tmp_path / "Assets"
    assets.mkdir()
    first = assets / "A.mat"
    second = assets / "B.mat"
    first.write_text("A", encoding="utf-8")
    second.write_text("B", encoding="utf-8")

    def _fail_after_one(path: str, database):
        if os.path.basename(path) == "A.mat":
            return False
        return _filesystem_delete(path, database)

    command = ProjectAssetDeleteCommand(
        [str(first), str(second)],
        project_root=str(tmp_path),
        delete_fn=_fail_after_one,
        import_fn=_successful_import,
    )

    with pytest.raises(RuntimeError, match="asset delete failed"):
        command.execute()

    assert first.read_text(encoding="utf-8") == "A"
    assert second.read_text(encoding="utf-8") == "B"
    command.dispose()


def test_failed_asset_delete_undo_keeps_cursor_and_external_file(tmp_path):
    asset = tmp_path / "Assets" / "Original.mat"
    asset.parent.mkdir()
    asset.write_text("original", encoding="utf-8")
    command = ProjectAssetDeleteCommand(
        [str(asset)],
        project_root=str(tmp_path),
        delete_fn=_filesystem_delete,
        import_fn=_successful_import,
    )
    manager = UndoManager(EditorActionJournal())
    assert manager.execute(command)
    asset.write_text("external", encoding="utf-8")

    manager.undo()

    assert manager.action_journal.cursor == 1
    assert asset.read_text(encoding="utf-8") == "external"
    manager.clear()


def test_project_asset_delete_restores_real_asset_database_guid(tmp_path, engine):
    import json

    assets = tmp_path / "Assets"
    assets.mkdir()
    database = engine.get_asset_database()
    asset = assets / "Stable.effect"
    asset.write_text(
        json.dumps(
            {
                "$schema": "infernux.render_effect",
                "feature_type": "infernux.post.bloom",
                "parameters": {},
                "dependencies": [],
            }
        ),
        encoding="utf-8",
    )
    guid = database.import_asset(str(asset)).guid
    assert guid

    command = ProjectAssetDeleteCommand(
        [str(asset)],
        project_root=str(tmp_path),
        asset_database=database,
    )

    command.execute()
    assert not database.contains_guid(guid)
    command.undo()

    assert database.get_guid_from_path(str(asset)) == guid
    assert database.get_path_from_guid(guid)
    command.dispose()


def test_plan_asset_paste_deduplicates_roots_and_reserves_unique_names(tmp_path):
    assets = tmp_path / "Assets"
    source = assets / "Source"
    destination = assets / "Destination"
    nested = source / "Nested.mat"
    source.mkdir(parents=True)
    destination.mkdir()
    nested.write_text("nested", encoding="utf-8")
    (destination / "Source").mkdir()

    planned = plan_asset_paste(
        [str(nested), str(source), str(source)],
        str(destination),
        cut=False,
    )

    assert planned == [(str(source), str(destination / "Source1"))]


def test_plan_asset_paste_skips_same_directory_cut_and_rejects_descendant(tmp_path):
    assets = tmp_path / "Assets"
    folder = assets / "Folder"
    nested = folder / "Nested"
    nested.mkdir(parents=True)
    asset = folder / "Smoke.mat"
    asset.write_text("smoke", encoding="utf-8")

    assert plan_asset_paste([str(asset)], str(folder), cut=True) == []
    with pytest.raises(ValueError, match="into itself"):
        plan_asset_paste([str(folder)], str(nested), cut=False)


def test_project_asset_move_rolls_back_a_partial_failure(tmp_path):
    source = tmp_path / "Source.mat"
    destination = tmp_path / "Destination.mat"
    source.write_text("material", encoding="utf-8")
    calls = []

    def _move_then_fail(old: str, new: str, _database):
        calls.append((old, new))
        os.replace(old, new)
        if len(calls) == 1:
            raise RuntimeError("registry notification failed")
        return new

    command = ProjectAssetMoveCommand(
        str(source),
        str(destination),
        move_fn=_move_then_fail,
    )

    with pytest.raises(RuntimeError, match="registry notification failed"):
        command.execute()

    assert source.read_text(encoding="utf-8") == "material"
    assert not destination.exists()
    assert calls == [
        (str(source), str(destination)),
        (str(destination), str(source)),
    ]


def test_project_asset_copy_preserves_generated_identity_across_replay(tmp_path):
    source = tmp_path / "Source.mat"
    destination = tmp_path / "Copied.mat"
    source.write_text("material", encoding="utf-8")

    def _copy_with_identity(old: str, new: str, _database):
        shutil.copy2(old, new)
        with open(new + ".meta", "w", encoding="utf-8") as stream:
            stream.write('{"guid":"copied-guid"}')
        return new

    command = ProjectAssetCopyCommand(
        str(source),
        str(destination),
        project_root=str(tmp_path),
        copy_fn=_copy_with_identity,
        delete_fn=_filesystem_delete,
        import_fn=_successful_import,
    )

    command.execute()
    command.undo()
    assert not destination.exists()

    command.redo()
    assert destination.read_text(encoding="utf-8") == "material"
    assert (tmp_path / "Copied.mat.meta").read_text(encoding="utf-8") == (
        '{"guid":"copied-guid"}'
    )
    command.dispose()


def test_project_asset_copy_rolls_back_failed_initial_copy(tmp_path):
    source = tmp_path / "Source.mat"
    destination = tmp_path / "Copied.mat"
    source.write_text("material", encoding="utf-8")

    def _partial_copy(old: str, new: str, _database):
        shutil.copy2(old, new)
        raise RuntimeError("import failed")

    command = ProjectAssetCopyCommand(
        str(source),
        str(destination),
        project_root=str(tmp_path),
        copy_fn=_partial_copy,
        delete_fn=_filesystem_delete,
        import_fn=_successful_import,
    )

    with pytest.raises(RuntimeError, match="import failed"):
        command.execute()

    assert source.exists()
    assert not destination.exists()
    command.dispose()


def test_project_asset_paste_is_one_global_action(tmp_path):
    first = tmp_path / "First.mat"
    second = tmp_path / "Second.mat"
    target = tmp_path / "Target"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")
    target.mkdir()
    destinations = [target / first.name, target / second.name]
    selected = []
    command = ProjectAssetPasteCommand(
        [
            ProjectAssetMoveCommand(
                str(first), str(destinations[0]), move_fn=_filesystem_move
            ),
            ProjectAssetMoveCommand(
                str(second), str(destinations[1]), move_fn=_filesystem_move
            ),
        ],
        [str(path) for path in destinations],
        on_applied=lambda paths: selected.append(list(paths)),
    )
    manager = UndoManager(EditorActionJournal())

    assert manager.execute(command)
    assert len(manager.action_journal.entries) == 1
    assert selected == [[str(path) for path in destinations]]
    assert all(path.exists() for path in destinations)

    manager.undo()
    assert first.exists() and second.exists()
    assert not any(path.exists() for path in destinations)

    manager.redo()
    assert all(path.exists() for path in destinations)
    manager.clear()


def test_project_asset_paste_rolls_back_when_selection_commit_fails(tmp_path):
    source = tmp_path / "Source.mat"
    destination = tmp_path / "Destination.mat"
    source.write_text("material", encoding="utf-8")
    command = ProjectAssetPasteCommand(
        [
            ProjectAssetMoveCommand(
                str(source), str(destination), move_fn=_filesystem_move
            )
        ],
        [str(destination)],
        on_applied=lambda _paths: (_ for _ in ()).throw(RuntimeError("selection failed")),
    )

    with pytest.raises(RuntimeError, match="selection failed"):
        command.execute()

    assert source.exists()
    assert not destination.exists()
