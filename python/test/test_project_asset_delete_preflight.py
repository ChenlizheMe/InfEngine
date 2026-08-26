from __future__ import annotations

from types import SimpleNamespace

import pytest

from Infernux.engine.interaction import (
    ClipboardService,
    DocumentKey,
    DocumentKind,
    DocumentRegistry,
    ProjectAssetCommandService,
    ProjectAssetInteractionService,
    SelectionService,
)
from Infernux.engine.undo import ProjectAssetDeleteCommand


def _service(tmp_path):
    selection = SelectionService()
    service = ProjectAssetCommandService(selection)
    service.configure(str(tmp_path), None)
    return service


def _scene_manager(monkeypatch, path):
    from Infernux.engine.scene_manager import SceneFileManager

    monkeypatch.setattr(
        SceneFileManager,
        "_instance",
        SimpleNamespace(current_scene_path=str(path)),
    )


def test_delete_service_rejects_active_scene_before_undo_command(tmp_path, monkeypatch):
    assets = tmp_path / "Assets"
    assets.mkdir()
    scene = assets / "R3GSokoban.scene"
    scene.write_text("{}\n", encoding="utf-8")
    _scene_manager(monkeypatch, scene)

    service = _service(tmp_path)
    monkeypatch.setattr(
        service,
        "_execute",
        lambda *_args, **_kwargs: pytest.fail("active scene must fail before command creation"),
    )

    with pytest.raises(ValueError, match="active scene"):
        service.delete((scene,))
    assert scene.is_file()


def test_visible_project_delete_request_and_mcp_service_share_scene_guard(
    tmp_path, monkeypatch
):
    assets = tmp_path / "Assets"
    assets.mkdir()
    scene = assets / "R3GSokoban.scene"
    scene.write_text("{}\n", encoding="utf-8")
    _scene_manager(monkeypatch, scene)

    service = _service(tmp_path)
    callback_calls = []
    interaction = ProjectAssetInteractionService(service, ClipboardService())
    interaction.configure(
        unique_name=lambda *_args: "unused",
        create=lambda *_args: None,
        open_asset=lambda *_args: True,
        reveal=lambda *_args: True,
        read_external_clipboard=lambda: (),
        request_delete=lambda paths, callback: callback_calls.append(paths) or True,
    )

    assert interaction.request_delete((scene,)) is False
    assert callback_calls == []
    with pytest.raises(ValueError, match="active scene"):
        service.preflight_delete((scene,))


def test_delete_service_rejects_scene_open_inside_deleted_directory(tmp_path):
    assets = tmp_path / "Assets"
    scenes = assets / "Scenes"
    scenes.mkdir(parents=True)
    scene = scenes / "Checkpoint.scene"
    scene.write_text("{}\n", encoding="utf-8")

    registry = DocumentRegistry()
    registry.open_or_create(
        DocumentKey.resource(DocumentKind.SCENE, str(scene)),
        "Checkpoint",
        resource_path=str(scene),
    )
    service = _service(tmp_path)
    with pytest.raises(ValueError, match="open scene"):
        service.preflight_delete((scenes,))


def test_non_scene_open_document_is_left_to_unified_close_contract(tmp_path):
    assets = tmp_path / "Assets"
    assets.mkdir()
    material = assets / "Open.mat"
    material.write_text("material\n", encoding="utf-8")

    registry = DocumentRegistry()
    document, _ = registry.open_or_create(
        DocumentKey.resource(DocumentKind.MATERIAL, str(material)),
        "Open",
        resource_path=str(material),
    )
    service = _service(tmp_path)
    assert service.preflight_delete((material,)) == (str(material.resolve()),)
    assert registry.get(document.document_id) is not None


def test_delete_command_restores_asset_and_meta_for_undo(tmp_path):
    assets = tmp_path / "Assets"
    assets.mkdir()
    asset = assets / "Undo.mat"
    meta = assets / "Undo.mat.meta"
    asset.write_text("material\n", encoding="utf-8")
    meta.write_text("guid: undo\n", encoding="utf-8")

    def delete(path, _database):
        asset_path = str(path)
        __import__("os").remove(asset_path)
        if __import__("os").path.exists(asset_path + ".meta"):
            __import__("os").remove(asset_path + ".meta")
        return True

    command = ProjectAssetDeleteCommand(
        [str(asset)],
        project_root=str(tmp_path),
        backup_root=str(tmp_path / "Library" / "EditorUndo"),
        delete_fn=delete,
        import_fn=lambda _path, _database: True,
    )
    command.execute()
    assert not asset.exists()
    assert not meta.exists()
    command.undo()
    assert asset.read_text(encoding="utf-8") == "material\n"
    assert meta.read_text(encoding="utf-8") == "guid: undo\n"
    command.dispose()


def test_asset_delete_registers_deleted_watcher_echo(monkeypatch, tmp_path):
    from Infernux.core.assets import AssetManager

    asset = tmp_path / "Echo.mat"
    asset.write_text("material\n", encoding="utf-8")

    class Database:
        @staticmethod
        def get_guid_from_path(path):
            return "echo-guid" if str(path) == str(asset) else ""

        @staticmethod
        def delete_asset(_path):
            return SimpleNamespace(guid="echo-guid")

    monkeypatch.setattr(AssetManager, "_get_registry", classmethod(lambda _cls: None))
    monkeypatch.setattr(AssetManager, "invalidate", classmethod(lambda _cls, _guid: None))
    monkeypatch.setattr(AssetManager, "_publish_asset_content_change", classmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(AssetManager, "_remove_material_pipeline", classmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(AssetManager, "_invalidate_shader_authoring_cache", classmethod(lambda *_args, **_kwargs: None))
    monkeypatch.setattr(AssetManager, "_invalidate_project_panel_cache", classmethod(lambda _cls: None))
    AssetManager._watcher_echo_suppression.clear()

    result = AssetManager.delete_asset(str(asset), database=Database())
    assert result
    assert AssetManager.is_watcher_echo_suppressed("deleted", str(asset)) is True
    AssetManager._watcher_echo_suppression.clear()
