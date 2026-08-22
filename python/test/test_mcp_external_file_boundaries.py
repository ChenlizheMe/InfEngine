from __future__ import annotations

import inspect
import pytest

# The current editor package initializes Undo before EditorInteractionCore in
# production. Preserve that import order when this file is run by itself.
import Infernux.engine.undo  # noqa: F401

from Infernux.mcp.project_tools import transactions as file_transactions
from Infernux.mcp.tools import assets as assets_module
from Infernux.mcp.tools import common
from Infernux.mcp.tools import transactions as transaction_tools


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        name = str(kwargs.get("name") or (args[0] if args else ""))

        def _register(fn):
            self.tools[name] = fn
            return fn

        return _register


@pytest.fixture
def asset_tools(tmp_path, monkeypatch):
    (tmp_path / "Assets").mkdir()
    monkeypatch.setattr(
        assets_module,
        "main_thread",
        lambda _operation, callback, **_kwargs: callback(),
    )
    monkeypatch.setattr(assets_module, "notify_asset_changed", lambda *_args: None)
    mcp = _FakeMcp()
    assets_module.register_asset_tools(mcp, str(tmp_path))
    return mcp.tools


@pytest.mark.parametrize(
    "relative_path",
    [
        "Assets/code.py",
        "Assets/types.pyi",
        "Assets/common.glsl",
        "Assets/mesh.vert",
        "Assets/surface.frag",
        "Assets/sim.comp",
        "Assets/lighting.shadingmodel",
        "Assets/readme.md",
        "Assets/data.json",
        "Assets/native.cpp",
        "Assets/.gitignore",
    ],
)
def test_external_source_edit_path_accepts_explicit_source_formats(tmp_path, relative_path):
    (tmp_path / "Assets").mkdir()

    resolved = common.require_external_source_edit_path(
        str(tmp_path), relative_path, "test"
    )

    assert resolved == str((tmp_path / relative_path).resolve())


@pytest.mark.parametrize(
    "extension",
    [
        ".mat",
        ".particlegraph",
        ".effect",
        ".effectgroup",
        ".scene",
        ".prefab",
        ".animclip2d",
        ".animclip3d",
        ".animfsm",
        ".animtimeline",
        ".timelinefsm",
        ".physicmaterial",
        ".meta",
    ],
)
def test_external_source_edit_path_rejects_editor_owned_assets(tmp_path, extension):
    (tmp_path / "Assets").mkdir()

    with pytest.raises(ValueError, match="Editor-owned asset"):
        common.require_external_source_edit_path(
            str(tmp_path), f"Assets/Owned{extension}", "test"
        )


@pytest.mark.parametrize(
    "relative_path",
    [
        "ProjectSettings/BuildSettings.json",
        "Library/Generated.json",
        ".infernux/mcp_transactions/state.json",
    ],
)
def test_external_source_edit_path_rejects_editor_owned_project_directories(
    tmp_path, relative_path
):
    with pytest.raises(ValueError, match="Editor-owned project data"):
        common.require_external_source_edit_path(
            str(tmp_path), relative_path, "test"
        )


@pytest.mark.parametrize(
    ("tool_name", "extension", "arguments"),
    [
        ("asset_write_text", ".mat", ("replacement",)),
        ("asset_edit_text", ".particlegraph", ("old", "new")),
        ("asset_write_json", ".effect", ({"value": 1},)),
        (
            "asset_patch_text",
            ".animtimeline",
            ([{"old": "old", "new": "new"}],),
        ),
    ],
)
def test_generic_asset_writers_reject_editor_owned_formats(
    tmp_path,
    asset_tools,
    tool_name,
    extension,
    arguments,
):
    target = tmp_path / "Assets" / f"Owned{extension}"
    target.write_text("old", encoding="utf-8")

    with pytest.raises(ValueError, match="Editor-owned asset"):
        asset_tools[tool_name](f"Assets/Owned{extension}", *arguments)

    assert target.read_text(encoding="utf-8") == "old"


def test_external_text_write_requires_preexisting_parent(asset_tools, tmp_path):
    with pytest.raises(FileNotFoundError, match="asset_ensure_folder"):
        asset_tools["asset_write_text"]("Assets/New/Code.py", "value = 1\n")

    assert not (tmp_path / "Assets" / "New").exists()


def test_resolve_project_directories_has_no_filesystem_side_effect(tmp_path):
    target = common.resolve_project_dir(str(tmp_path), "Assets/Nested/Source")
    assets = common.project_assets_dir(str(tmp_path))
    resolved_asset = common.resolve_asset_path(str(tmp_path), "Assets/Test.py")

    assert target == str((tmp_path / "Assets" / "Nested" / "Source").resolve())
    assert assets == str((tmp_path / "Assets").resolve())
    assert resolved_asset == str((tmp_path / "Assets" / "Test.py").resolve())
    assert not (tmp_path / "Assets").exists()


def test_snapshot_tracking_skips_document_registry_managed_source(
    tmp_path, monkeypatch
):
    from Infernux.engine.interaction import DocumentKind, DocumentRegistry

    source = tmp_path / "Assets" / "Live.py"
    source.parent.mkdir()
    source.write_text("old\n", encoding="utf-8")
    DocumentRegistry.instance().create(
        DocumentKind.GENERIC,
        "Live.py",
        resource_path=str(source),
    )
    calls = []
    monkeypatch.setattr(
        file_transactions,
        "record_path_before_change",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    tracked = common.track_project_path_before_change(
        str(tmp_path), str(source), "write_text"
    )

    assert tracked is False
    assert calls == []


def test_snapshot_tracking_rejects_structural_operations(tmp_path, monkeypatch):
    source = tmp_path / "Assets" / "MoveMe.py"
    source.parent.mkdir()
    source.write_text("old\n", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        file_transactions,
        "record_path_before_change",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    assert common.track_project_path_before_change(
        str(tmp_path), str(source), "move"
    ) is False
    assert calls == []


def test_transaction_rollback_restores_external_source(tmp_path, monkeypatch):
    source = tmp_path / "Assets" / "Code.py"
    source.parent.mkdir()
    source.write_text("old\n", encoding="utf-8")
    file_transactions._ACTIVE = None
    file_transactions._LAST = None
    try:
        file_transactions.begin(str(tmp_path), label="source edit")
        file_transactions.record_path_before_change(
            str(tmp_path), str(source), operation="write_text"
        )
        source.write_text("new\n", encoding="utf-8")
        main_thread_calls = []
        notifications = []
        monkeypatch.setattr(
            transaction_tools,
            "main_thread",
            lambda name, callback: (
                main_thread_calls.append(name)
                or common.ok(callback())
            ),
        )
        monkeypatch.setattr(
            transaction_tools,
            "notify_asset_changed",
            lambda path, action: notifications.append((path, action)),
        )
        mcp = _FakeMcp()
        transaction_tools.register_transaction_tools(mcp, str(tmp_path))

        result = mcp.tools["transaction_rollback"]()

        assert result["ok"] is True
        assert source.read_text(encoding="utf-8") == "old\n"
        assert main_thread_calls == ["transaction_rollback"]
        assert notifications == [(str(source), "modified")]
    finally:
        if file_transactions._ACTIVE is not None:
            file_transactions.commit()


@pytest.mark.parametrize(
    ("relative_path", "operation"),
    [
        ("Assets/Owned.mat", "write_text"),
        ("Assets/MoveMe.py", "move"),
    ],
)
def test_transaction_rollback_fails_closed_for_unsafe_legacy_events(
    tmp_path, relative_path, operation
):
    target = tmp_path / relative_path
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    file_transactions._ACTIVE = None
    file_transactions._LAST = None
    try:
        file_transactions.begin(str(tmp_path), label="unsafe legacy event")
        file_transactions.record_path_before_change(
            str(tmp_path), str(target), operation=operation
        )

        with pytest.raises(RuntimeError, match="Refusing MCP file snapshot rollback"):
            transaction_tools._validate_external_source_rollback(str(tmp_path))
    finally:
        if file_transactions._ACTIVE is not None:
            file_transactions.commit()


def test_builtin_creation_owns_missing_directory_tree_in_one_editor_action(
    tmp_path, monkeypatch
):
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.undo import UndoManager

    assets = tmp_path / "Assets"
    assets.mkdir()
    previous_manager = UndoManager._instance
    manager = UndoManager()
    core = EditorInteractionCore()
    core.project_assets.configure(str(tmp_path), None)
    monkeypatch.setattr(
        assets_module,
        "main_thread",
        lambda _operation, callback, **_kwargs: callback(),
    )
    monkeypatch.setattr(assets_module, "get_asset_database", lambda: None)
    mcp = _FakeMcp()
    assets_module.register_asset_tools(mcp, str(tmp_path))

    try:
        created = mcp.tools["asset_create_script"](
            "Mover", "Assets/Nested/Source"
        )

        script = assets / "Nested" / "Source" / "Mover.py"
        assert created["path"] == "Assets/Nested/Source/Mover.py"
        assert script.is_file()
        assert len(manager.action_journal.applied_entries()) == 1

        manager.undo()
        assert not (assets / "Nested").exists()

        manager.redo()
        assert script.is_file()
    finally:
        core.shutdown()
        manager.clear()
        UndoManager._instance = previous_manager


def test_asset_tools_only_snapshot_external_content_edits():
    source = inspect.getsource(assets_module)
    common_source = inspect.getsource(common)

    assert source.count("track_project_path_before_change(") == 3
    assert "def write_external_source_text(" in common_source
    assert "track_project_path_before_change(project_path, file_path, operation)" in common_source
    assert 'operation: str = "write_text"' in common_source
    for operation in ("edit_text", "write_json", "patch_text"):
        assert f'"{operation}"' in source
    for operation in (
        "ensure_folder",
        "delete",
        "overwrite",
        "move",
        "rename",
        "copy",
        "create_builtin",
    ):
        assert f'track_project_path_before_change(project_path, target, "{operation}")' not in source
