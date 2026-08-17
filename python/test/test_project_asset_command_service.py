from __future__ import annotations

import os
import shutil
import tempfile

import pytest

from Infernux.engine.interaction import (
    ActionOrigin,
    ClipboardService,
    EditorActionJournal,
    ProjectAssetCommandService,
    ProjectAssetInteractionService,
    SelectionService,
    SelectionTarget,
)
from Infernux.engine.undo import UndoManager


@pytest.fixture()
def project_asset_commands(tmp_path, monkeypatch):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.ui import project_file_ops

    assets = tmp_path / "Assets"
    assets.mkdir()

    def delete_item(path, _database):
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        meta = str(path) + ".meta"
        if os.path.exists(meta):
            os.remove(meta)
        return True

    def move_batch(moves, _database, **_kwargs):
        results = []
        for source, destination in moves:
            os.replace(source, destination)
            source_meta = source + ".meta"
            if os.path.exists(source_meta):
                os.replace(source_meta, destination + ".meta")
            results.append(destination)
        return tuple(results)

    def rename(source, new_name, _database, **_kwargs):
        destination = os.path.join(os.path.dirname(source), new_name)
        return move_batch(((source, destination),), None)[0]

    def copy(source, destination, _database):
        if os.path.isdir(source):
            shutil.copytree(source, destination)
        else:
            shutil.copy2(source, destination)
        return destination

    monkeypatch.setattr(project_file_ops, "delete_item", delete_item)
    monkeypatch.setattr(project_file_ops, "move_paths_batch", move_batch)
    monkeypatch.setattr(project_file_ops, "do_rename", rename)
    monkeypatch.setattr(project_file_ops, "copy_path_as_new_asset", copy)
    monkeypatch.setattr(
        AssetManager,
        "import_asset",
        staticmethod(lambda _path, database=None: True),
    )

    journal = EditorActionJournal()
    manager = UndoManager(journal)
    service = ProjectAssetCommandService(SelectionService())
    service.configure(str(tmp_path), None)
    try:
        yield service, manager, journal, assets
    finally:
        service.shutdown()


def test_project_asset_service_records_automation_rename_once(project_asset_commands):
    service, manager, journal, assets = project_asset_commands
    source = assets / "Before.txt"
    source.write_text("content", encoding="utf-8")

    destination = service.rename(
        str(source),
        "After.txt",
        origin=ActionOrigin.AUTOMATION,
    )

    assert destination == str((assets / "After.txt").resolve())
    assert not source.exists()
    assert (assets / "After.txt").read_text(encoding="utf-8") == "content"
    entries = journal.applied_entries()
    assert len(entries) == 1
    assert entries[0].origin is ActionOrigin.AUTOMATION

    manager.undo()
    assert source.read_text(encoding="utf-8") == "content"
    assert not (assets / "After.txt").exists()


@pytest.mark.parametrize("is_directory", (False, True))
def test_project_asset_service_selects_renamed_asset_or_folder(
    project_asset_commands,
    is_directory,
):
    service, _manager, _journal, assets = project_asset_commands
    selection = SelectionService.instance()
    source = assets / ("BeforeFolder" if is_directory else "Before.txt")
    if is_directory:
        source.mkdir()
    else:
        source.write_text("content", encoding="utf-8")

    destination = service.rename(
        str(source),
        "AfterFolder" if is_directory else "After.txt",
    )

    snapshot = selection.snapshot
    target = SelectionTarget.asset(destination)
    assert snapshot.targets == (target,)
    assert snapshot.primary == target
    assert snapshot.owner_id == "project"


def test_project_asset_interactions_own_clipboard_transfer_and_delete(
    project_asset_commands,
):
    service, _manager, journal, assets = project_asset_commands
    clipboard = ClipboardService()
    interactions = ProjectAssetInteractionService(service, clipboard)
    opened = []
    revealed = []
    interactions.configure(
        unique_name=lambda _directory, base, _extension: base,
        create=lambda _kind, _directory, _name, _variant: (False, "unused"),
        open_asset=lambda kind, path: opened.append((kind, path)) or True,
        reveal=lambda path: revealed.append(path) or True,
        read_external_clipboard=lambda: (),
        request_delete=lambda paths, callback: callback(list(paths)),
    )

    source = assets / "Source.txt"
    source.write_text("source", encoding="utf-8")
    target = assets / "Target"
    target.mkdir()

    assert interactions.copy((str(source),), cut=False)
    pasted = interactions.paste(str(target))
    assert pasted == (str((target / "Source.txt").resolve()),)
    assert (target / "Source.txt").read_text(encoding="utf-8") == "source"

    assert interactions.request_delete(pasted)
    assert not (target / "Source.txt").exists()
    assert len(journal.applied_entries()) == 2

    interactions.shutdown()


def test_project_asset_interactions_create_open_and_reveal_without_panel_business_callbacks(
    project_asset_commands,
):
    service, _manager, _journal, assets = project_asset_commands
    clipboard = ClipboardService()
    interactions = ProjectAssetInteractionService(service, clipboard)
    opened = []
    revealed = []

    def create(_kind, directory, name, _variant):
        path = os.path.join(directory, f"{name}.txt")
        with open(path, "w", encoding="utf-8") as stream:
            stream.write("created")
        return True, ""

    interactions.configure(
        unique_name=lambda _directory, base, _extension: base,
        create=create,
        open_asset=lambda kind, path: opened.append((kind, path)) or True,
        reveal=lambda path: revealed.append(path) or True,
        read_external_clipboard=lambda: (),
        request_delete=lambda _paths, _callback: False,
    )

    created = interactions.create("text", str(assets), "Created", ".txt")
    assert created == str((assets / "Created.txt").resolve())
    assert interactions.open("system", created)
    assert interactions.reveal(created)
    assert opened == [("system", created)]
    assert revealed == [created]

    interactions.shutdown()


def test_project_asset_service_overwrite_move_is_one_atomic_action(project_asset_commands):
    service, manager, journal, assets = project_asset_commands
    source = assets / "Source.txt"
    destination = assets / "Destination.txt"
    source.write_text("new", encoding="utf-8")
    destination.write_text("old", encoding="utf-8")

    result = service.move(
        str(source),
        str(destination),
        overwrite=True,
        origin=ActionOrigin.AUTOMATION,
    )

    assert result == str(destination.resolve())
    assert destination.read_text(encoding="utf-8") == "new"
    assert not source.exists()
    assert len(journal.applied_entries()) == 1

    manager.undo()
    assert source.read_text(encoding="utf-8") == "new"
    assert destination.read_text(encoding="utf-8") == "old"

    manager.redo()
    assert destination.read_text(encoding="utf-8") == "new"
    assert not source.exists()


def test_project_asset_service_create_and_delete_share_global_history(project_asset_commands):
    service, manager, journal, assets = project_asset_commands
    created = assets / "Created.txt"

    def creator():
        created.write_text("created", encoding="utf-8")
        return True, ""

    assert service.create(
        str(assets),
        creator,
        origin=ActionOrigin.AUTOMATION,
    ) == (True, "")
    assert created.exists()

    service.delete((str(created),), origin=ActionOrigin.AUTOMATION)
    assert not created.exists()
    assert len(journal.applied_entries()) == 2

    manager.undo()
    assert created.read_text(encoding="utf-8") == "created"
    manager.undo()
    assert not created.exists()


def test_external_drop_is_one_atomic_import_without_sidecar_identity(
    project_asset_commands,
):
    service, manager, journal, assets = project_asset_commands
    (assets / "Smoke.png").write_bytes(b"existing")

    with tempfile.TemporaryDirectory() as external_root:
        external = os.path.realpath(external_root)
        smoke = os.path.join(external, "Smoke.png")
        config = os.path.join(external, "Config.json")
        sidecar = smoke + ".meta"
        with open(smoke, "wb") as stream:
            stream.write(b"imported")
        with open(config, "w", encoding="utf-8") as stream:
            stream.write('{"quality":"high"}')
        with open(sidecar, "w", encoding="utf-8") as stream:
            stream.write('{"guid":"foreign"}')

        imported = service.import_external(
            (smoke, sidecar, config),
            str(assets),
        )

        expected_smoke = str((assets / "Smoke1.png").resolve())
        expected_config = str((assets / "Config.json").resolve())
        assert imported == (expected_smoke, expected_config)
        assert (assets / "Smoke1.png").read_bytes() == b"imported"
        assert (assets / "Config.json").read_text(encoding="utf-8") == (
            '{"quality":"high"}'
        )
        assert not (assets / "Smoke.png.meta").exists()
        assert not (assets / "Smoke1.png.meta").exists()
        assert len(journal.applied_entries()) == 1
        assert journal.applied_entries()[0].action.description == "Import External Assets"

        manager.undo()
        assert not (assets / "Smoke1.png").exists()
        assert not (assets / "Config.json").exists()
        assert (assets / "Smoke.png").read_bytes() == b"existing"

        manager.redo()
        assert (assets / "Smoke1.png").read_bytes() == b"imported"
        assert (assets / "Config.json").read_text(encoding="utf-8") == (
            '{"quality":"high"}'
        )
        assert not (assets / "Smoke1.png.meta").exists()


def test_external_import_validation_uses_the_same_project_boundary(
    project_asset_commands,
    tmp_path,
):
    service, _manager, _journal, assets = project_asset_commands
    external = tmp_path.parent / "ExternalAsset.png"
    external.write_bytes(b"asset")
    sidecar = tmp_path.parent / "ExternalAsset.png.meta"
    sidecar.write_text("{}", encoding="utf-8")

    assert service.can_import_external((str(external),), str(assets))
    assert not service.can_import_external((str(sidecar),), str(assets))
    assert not service.can_import_external(
        (str(external),),
        str(tmp_path / "MissingDirectory"),
    )
    assert not service.can_import_external(
        (str(external),),
        str(tmp_path.parent),
    )


def test_native_project_drop_has_no_filesystem_copy_fallback():
    source_path = os.path.realpath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "cpp",
            "infernux",
            "function",
            "editor",
            "ProjectPanel.cpp",
        )
    )
    text = open(source_path, "r", encoding="utf-8").read()
    receive = text[
        text.index("void ProjectPanel::ReceiveDroppedFiles") : text.index(
            "void ProjectPanel::BeginRename"
        )
    ]

    assert 'ExecuteEditorCommand("asset.import_external"' in receive
    assert "MakeAssetImportCommandArgument(paths, m_currentPath)" in receive
    assert "importExternalPaths" not in receive
    assert "fs::copy(" not in receive
    assert "fs::copy_file(" not in receive
    assert "copyItemToPath" not in receive

    assert "ProjectPanel::PasteAssets" not in text
    assert "pasteAssetClipboard" not in text
    assert "moveAssetPaths" not in text
    assert "deleteItems" not in text

    interactions = open(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "Infernux",
            "engine",
            "interaction",
            "project_assets.py",
        ),
        "r",
        encoding="utf-8",
    ).read()
    assert "class ProjectAssetInteractionService" in interactions
    assert "self._assets.import_external(" in interactions
    assert "self._assets.transfer_to_directory(" in interactions


def test_mcp_structural_asset_tools_have_no_raw_filesystem_fallbacks():
    source = (
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "Infernux",
            "mcp",
            "tools",
            "assets.py",
        )
    )
    text = open(source, "r", encoding="utf-8").read()
    structural = text[text.index('    @mcp.tool(name="asset_delete")') : text.index('    @mcp.tool(name="asset_get_meta")')]

    assert "project_file_ops import delete_item" not in structural
    assert "project_file_ops import move_path" not in structural
    assert "project_file_ops import do_rename" not in structural
    assert "project_file_ops import copy_path_as_new_asset" not in structural
    assert "shutil.rmtree" not in structural
    assert "os.rename" not in structural
    assert "_project_asset_commands(project_path)" in structural
