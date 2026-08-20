from __future__ import annotations

import os
import shutil

from Infernux.engine.interaction import (
    ActionOrigin,
    EditorActionJournal,
    ProjectAssetCommandService,
    SelectionService,
)
from Infernux.engine.undo import UndoManager
from Infernux.mcp.tools import renderstack as renderstack_tools


class _FakeMcp:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, *, name: str):
        def _register(callback):
            self.tools[name] = callback
            return callback

        return _register


def test_render_effect_create_is_one_automation_action_with_atomic_parent_tree(
    tmp_path,
    monkeypatch,
):
    from Infernux.engine.ui import project_file_ops

    assets = tmp_path / "Assets"
    assets.mkdir()
    effect = assets / "Rendering" / "Post" / "ACES Tone Mapping.effect"

    def create_folder(current_path, folder_name):
        destination = os.path.join(current_path, folder_name)
        if os.path.exists(destination):
            return False, "folder already exists"
        os.makedirs(destination)
        return True, ""

    def create_render_effect(current_path, effect_name, feature_type, _database):
        assert feature_type == "tone_mapping"
        destination = os.path.join(current_path, effect_name + ".effect")
        if os.path.exists(destination):
            return False, "effect already exists"
        with open(destination, "w", encoding="utf-8") as stream:
            stream.write(feature_type)
        return True, ""

    def delete_item(path, _database):
        if os.path.isdir(path):
            shutil.rmtree(path)
        else:
            os.remove(path)
        return True

    monkeypatch.setattr(project_file_ops, "create_folder", create_folder)
    monkeypatch.setattr(project_file_ops, "create_render_effect", create_render_effect)
    monkeypatch.setattr(project_file_ops, "delete_item", delete_item)
    monkeypatch.setattr(renderstack_tools, "get_asset_database", lambda: None)
    monkeypatch.setattr(
        renderstack_tools,
        "main_thread",
        lambda _name, callback, **_kwargs: callback(),
    )

    previous_manager = UndoManager.instance()
    previous_service = ProjectAssetCommandService.instance()
    journal = EditorActionJournal()
    manager = UndoManager(journal)
    service = ProjectAssetCommandService(SelectionService())
    service.configure(str(tmp_path), None)
    mcp = _FakeMcp()
    renderstack_tools.register_renderstack_tools(mcp, str(tmp_path))
    try:
        result = mcp.tools["render_effect_create"](
            "Assets/Rendering/Post/ACES Tone Mapping",
            "tone_mapping",
        )

        assert result == {
            "path": "Assets/Rendering/Post/ACES Tone Mapping.effect",
            "feature_type": "tone_mapping",
            "created": True,
        }
        assert effect.read_text(encoding="utf-8") == "tone_mapping"
        entries = journal.applied_entries()
        assert len(entries) == 1
        assert entries[0].origin is ActionOrigin.AUTOMATION

        manager.undo()
        assert not effect.exists()
        assert not (assets / "Rendering").exists()

        manager.redo()
        assert effect.read_text(encoding="utf-8") == "tone_mapping"
    finally:
        service.shutdown()
        ProjectAssetCommandService._instance = previous_service
        UndoManager._instance = previous_manager


def test_render_effect_create_has_no_raw_directory_or_untracked_create_path():
    source = open(renderstack_tools.__file__, "r", encoding="utf-8").read()
    create_tool = source[
        source.index('    @mcp.tool(name="render_effect_create")') :
        source.index('    @mcp.tool(name="renderstack_find_or_create")')
    ]

    assert "os.makedirs" not in create_tool
    assert "ProjectAssetCommandService.instance()" in create_tool
    assert "origin=ActionOrigin.AUTOMATION" in create_tool
    assert "return service.create(" in create_tool

