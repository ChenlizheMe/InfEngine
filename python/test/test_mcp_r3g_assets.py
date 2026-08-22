from __future__ import annotations

from types import SimpleNamespace

import pytest

from Infernux.engine.interaction import DocumentKey, DocumentKind, DocumentRegistry
from Infernux.mcp.tools import assets as assets_module
from Infernux.mcp.tools import particle as particle_module


class _FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        name = str(kwargs.get("name") or (args[0] if args else ""))

        def _register(fn):
            self.tools[name] = fn
            return fn

        return _register


class _Meta:
    # Simulate a just-created meta object whose cached payload has not yet
    # populated the identity fields. The database mapping remains authoritative.
    guid = ""
    type = ""

    def get_guid(self):
        return ""

    def get_resource_type(self):
        return ""


class _AssetDatabase:
    def __init__(self, path, guid):
        self.path = str(path)
        self.guid = str(guid)
        self.meta = _Meta()

    def get_guid_from_path(self, path):
        return self.guid if str(path).casefold() == self.path.casefold() else ""

    def get_path_from_guid(self, guid):
        return self.path if str(guid) == self.guid else ""

    def get_meta_by_guid(self, guid):
        return self.meta if str(guid) == self.guid else None

    def get_meta_by_path(self, path):
        return self.meta if str(path).casefold() == self.path.casefold() else None

    def get_resource_type(self, path):
        return SimpleNamespace(name="ParticleGraph")


def _direct_main_thread(monkeypatch):
    monkeypatch.setattr(
        assets_module,
        "main_thread",
        lambda _operation, callback, **_kwargs: callback(),
    )
    monkeypatch.setattr(
        particle_module,
        "main_thread",
        lambda _operation, callback, **_kwargs: callback(),
    )


def test_asset_refresh_publishes_the_durable_asset_index(tmp_path, monkeypatch):
    calls = []
    index_path = tmp_path / "Library" / "AssetIndex.json"

    class _Database:
        asset_index_path = str(index_path)

        @staticmethod
        def refresh():
            calls.append("refresh")

        @staticmethod
        def flush_derived_index():
            calls.append("flush")

    _direct_main_thread(monkeypatch)
    monkeypatch.setattr(assets_module, "get_asset_database", lambda: _Database())
    mcp = _FakeMcp()
    assets_module.register_asset_tools(mcp, str(tmp_path))

    result = mcp.tools["asset_refresh"]()

    assert calls == ["refresh", "flush"]
    assert result == {
        "refreshed": True,
        "asset_index_path": str(index_path),
    }


def test_particle_graph_creation_is_immediately_visible_to_asset_meta(
    tmp_path, monkeypatch
):
    target = tmp_path / "Assets" / "Acceptance" / "Smoke.particlegraph"
    target.parent.mkdir(parents=True)
    database = _AssetDatabase(target, "smoke-guid")

    def create_graph(_project_path, _kind, _name, _directory, _shader_type):
        target.write_text("{}\n", encoding="utf-8")
        return {
            "kind": "particlegraph",
            "path": "Assets/Acceptance/Smoke.particlegraph",
            "guid": "smoke-guid",
            "created": True,
        }

    _direct_main_thread(monkeypatch)
    monkeypatch.setattr(assets_module, "get_asset_database", lambda: database)
    monkeypatch.setattr(assets_module, "_create_builtin", create_graph)

    mcp = _FakeMcp()
    assets_module.register_asset_tools(mcp, str(tmp_path))

    created = mcp.tools["asset_create_particle_graph"](
        "Smoke.particlegraph", "Assets/Acceptance"
    )
    meta = mcp.tools["asset_get_meta"](
        path="Assets/Acceptance/Smoke.particlegraph"
    )

    assert created["created"] is True
    assert meta == {
        "guid": "smoke-guid",
        "path": "Assets/Acceptance/Smoke.particlegraph",
        "type": "ParticleGraph",
    }


def test_particle_graph_open_and_inspect_share_the_accepted_document(
    tmp_path, monkeypatch
):
    graph = tmp_path / "Assets" / "Smoke.particlegraph"
    graph.parent.mkdir(parents=True)
    graph.write_text("{}\n", encoding="utf-8")

    class Panel:
        document_id = "particle-document"

        def authoring_snapshot(self):
            return {"file_path": str(graph), "dirty": False, "nodes": []}

        def authoring_document_state(self):
            return {"file_path": str(graph), "dirty": False}

    panel = Panel()
    registry = DocumentRegistry()
    registry.open_or_create(
        DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(graph)),
        "Smoke",
        resource_path=str(graph),
    )
    document = registry.documents[0]
    panel.document_id = document.document_id
    previous_registry = DocumentRegistry._instance
    DocumentRegistry._instance = registry
    try:
        _direct_main_thread(monkeypatch)
        monkeypatch.setattr(particle_module, "_open_particle_graph_panel", lambda _path: panel)
        monkeypatch.setattr(particle_module, "_require_particle_graph_panel", lambda: panel)
        mcp = _FakeMcp()
        particle_module.register_particle_tools(mcp, str(tmp_path))

        opened = mcp.tools["particle_graph_open_asset"]("Assets/Smoke.particlegraph")
        inspected = mcp.tools["particle_graph_inspect_editor"]()

        assert opened["file_path"] == inspected["file_path"]
        assert opened["file_path"].endswith("Smoke.particlegraph")
    finally:
        DocumentRegistry._instance = previous_registry


def test_particle_graph_open_uses_database_guid_for_document_identity(
    tmp_path, monkeypatch
):
    graph = tmp_path / "Assets" / "Guided.particlegraph"
    graph.parent.mkdir(parents=True)
    graph.write_text("{}\n", encoding="utf-8")
    database = _AssetDatabase(graph, "guided-guid")
    calls = []

    from Infernux.engine.interaction import DocumentOpenStatus
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.engine.ui.window_manager import WindowManager

    panel = ParticleGraphEditorPanel.__new__(ParticleGraphEditorPanel)
    panel._is_open = False
    panel._document_id = ""
    panel.authoring_document_state = lambda: {
        "file_path": str(graph),
        "dirty": False,
    }

    registry = DocumentRegistry()
    document, _ = registry.open_or_create(
        DocumentKey.asset(DocumentKind.PARTICLE_GRAPH, "guided-guid"),
        "Guided",
        resource_path=str(graph),
    )
    panel._document_id = document.document_id
    previous_registry = DocumentRegistry._instance
    previous_manager = WindowManager._instance
    DocumentRegistry._instance = registry

    class Manager:
        def get_window_instance(self, _window_id):
            return panel

        def focus_window(self, _window_id):
            return None

    class OpenService:
        def open_resource(self, *args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(status=DocumentOpenStatus.READY, message="")

    class Core:
        document_open = OpenService()

    manager = Manager()
    WindowManager._instance = manager
    monkeypatch.setattr(particle_module, "get_asset_database", lambda: database)
    monkeypatch.setattr(
        "Infernux.engine.interaction.EditorInteractionCore.instance",
        classmethod(lambda _cls: Core()),
    )
    try:
        opened = particle_module._open_particle_graph_panel(str(graph))
    finally:
        DocumentRegistry._instance = previous_registry
        WindowManager._instance = previous_manager

    assert opened is panel
    assert calls[0][1]["guid"] == "guided-guid"
    assert calls[0][1]["title"] == "Guided.particlegraph"


def test_particle_graph_open_reports_deferred_registration_without_hard_failure(
    tmp_path, monkeypatch
):
    graph = tmp_path / "Assets" / "Deferred.particlegraph"
    graph.parent.mkdir(parents=True)
    graph.write_text("{}\n", encoding="utf-8")

    _direct_main_thread(monkeypatch)

    def deferred(_path):
        raise particle_module._ParticleGraphOpenPending(
            "waiting for its DocumentRegistry entry"
        )

    monkeypatch.setattr(particle_module, "_open_particle_graph_panel", deferred)
    mcp = _FakeMcp()
    particle_module.register_particle_tools(mcp, str(tmp_path))

    result = mcp.tools["particle_graph_open_asset"]("Assets/Deferred.particlegraph")

    assert result["status"] == "pending"
    assert result["retryable"] is True
    assert result["document_registered"] is False


def test_particle_graph_deferred_retry_does_not_reload_the_panel(
    tmp_path, monkeypatch
):
    graph = tmp_path / "Assets" / "DeferredRetry.particlegraph"
    graph.parent.mkdir(parents=True)
    graph.write_text("{}\n", encoding="utf-8")

    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.engine.ui.window_manager import WindowManager

    panel = ParticleGraphEditorPanel.__new__(ParticleGraphEditorPanel)
    panel._is_open = True
    panel._document_id = ""
    panel.authoring_document_state = lambda: {
        "file_path": str(graph),
        "dirty": False,
    }
    panel.authoring_snapshot = lambda: {
        "file_path": str(graph),
        "dirty": False,
        "nodes": [],
    }

    class Manager:
        def get_window_instance(self, _window_id):
            return panel

        def focus_window(self, _window_id):
            return None

    previous_manager = WindowManager._instance
    WindowManager._instance = Manager()
    registry = DocumentRegistry()
    previous_registry = DocumentRegistry._instance
    DocumentRegistry._instance = registry
    _direct_main_thread(monkeypatch)
    try:
        mcp = _FakeMcp()
        particle_module.register_particle_tools(mcp, str(tmp_path))
        pending = mcp.tools["particle_graph_open_asset"](
            "Assets/DeferredRetry.particlegraph"
        )
        assert pending["status"] == "pending"

        document, _ = registry.open_or_create(
            DocumentKey.resource(DocumentKind.PARTICLE_GRAPH, str(graph)),
            "DeferredRetry",
            resource_path=str(graph),
        )
        panel._document_id = document.document_id
        opened = mcp.tools["particle_graph_open_asset"](
            "Assets/DeferredRetry.particlegraph"
        )
    finally:
        DocumentRegistry._instance = previous_registry
        WindowManager._instance = previous_manager

    assert opened["file_path"].endswith("DeferredRetry.particlegraph")


def test_asset_delete_allows_closed_non_active_scene_and_removes_through_transaction(
    tmp_path, monkeypatch
):
    assets = tmp_path / "Assets"
    assets.mkdir()
    scene = assets / "Other.scene"
    scene.write_text("{}\n", encoding="utf-8")
    (assets / "Other.scene.meta").write_text("meta\n", encoding="utf-8")
    calls = []

    class Commands:
        def delete(self, paths, **_kwargs):
            calls.append(tuple(paths))

    _direct_main_thread(monkeypatch)
    monkeypatch.setattr(assets_module, "get_asset_database", lambda: None)
    monkeypatch.setattr(assets_module, "_project_asset_commands", lambda _path: Commands())

    from Infernux.engine.scene_manager import SceneFileManager

    previous_manager = SceneFileManager._instance
    SceneFileManager._instance = SimpleNamespace(current_scene_path=str(assets / "Active.scene"))
    try:
        mcp = _FakeMcp()
        assets_module.register_asset_tools(mcp, str(tmp_path))
        result = mcp.tools["asset_delete"]("Assets/Other.scene")
    finally:
        SceneFileManager._instance = previous_manager

    assert result["deleted"] is True
    assert calls == [(str(scene.resolve()),)]


def test_asset_delete_rejects_project_files_outside_assets(tmp_path, monkeypatch):
    assets = tmp_path / "Assets"
    assets.mkdir()
    settings = tmp_path / "ProjectSettings"
    settings.mkdir()
    settings_file = settings / "BuildSettings.json"
    settings_file.write_text("{}\n", encoding="utf-8")
    _direct_main_thread(monkeypatch)

    mcp = _FakeMcp()
    assets_module.register_asset_tools(mcp, str(tmp_path))
    with pytest.raises(ValueError, match="inside Assets"):
        mcp.tools["asset_delete"]("ProjectSettings/BuildSettings.json")


@pytest.mark.parametrize("open_scene", ["active", "registered"])
def test_asset_delete_rejects_active_or_open_scene(tmp_path, monkeypatch, open_scene):
    assets = tmp_path / "Assets"
    assets.mkdir()
    scene = assets / "Blocked.scene"
    scene.write_text("{}\n", encoding="utf-8")

    _direct_main_thread(monkeypatch)
    monkeypatch.setattr(assets_module, "get_asset_database", lambda: None)
    from Infernux.engine.interaction import ProjectAssetCommandService, SelectionService

    commands = ProjectAssetCommandService(SelectionService())
    commands.configure(str(tmp_path), None)
    monkeypatch.setattr(assets_module, "_project_asset_commands", lambda _path: commands)

    from Infernux.engine.scene_manager import SceneFileManager

    previous_manager = SceneFileManager._instance
    previous_registry = DocumentRegistry._instance
    registry = DocumentRegistry()
    if open_scene == "registered":
        registry.open_or_create(
            DocumentKey.resource(DocumentKind.SCENE, str(scene)),
            "Blocked",
            resource_path=str(scene),
        )
        active_path = str(assets / "Other.scene")
    else:
        active_path = str(scene)
    DocumentRegistry._instance = registry
    SceneFileManager._instance = SimpleNamespace(current_scene_path=active_path)
    try:
        mcp = _FakeMcp()
        assets_module.register_asset_tools(mcp, str(tmp_path))
        with pytest.raises(ValueError, match="(active scene|open scene document)"):
            mcp.tools["asset_delete"]("Assets/Blocked.scene")
    finally:
        SceneFileManager._instance = previous_manager
        DocumentRegistry._instance = previous_registry
