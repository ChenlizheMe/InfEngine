from __future__ import annotations

import json
import socket
import time
import uuid
from pathlib import Path

import pytest

from Infernux.host import OperationRegistry
from infernux_mcp import server
from infernux_mcp import capabilities
from infernux_mcp.adapter import (
    MAX_GATEWAY_TOOLS,
    adapter_status,
    register_gateways,
    shutdown_adapter,
)


class _FakeMCP:
    def __init__(self) -> None:
        self.tools = {}

    def tool(self, *args, **kwargs):
        name = kwargs.get("name") or (args[0] if args else "")

        def decorate(fn):
            self.tools[str(name or fn.__name__)] = fn
            return fn

        return decorate


def test_capability_config_discards_unknown_schema_fields(tmp_path):
    settings = tmp_path / "ProjectSettings"
    settings.mkdir()
    path = settings / "mcp_capabilities.json"
    path.write_text(
        json.dumps(
            {
                "profile": "developer_assist",
                "unknown_section": {"unused": True},
                "limits": {"batch_max_steps": 12, "unknown_limit": 99},
            }
        ),
        encoding="utf-8",
    )

    loaded = capabilities.configure(str(tmp_path), write_default=True)
    persisted = json.loads(path.read_text(encoding="utf-8"))

    assert "unknown_section" not in loaded
    assert "unknown_section" not in persisted
    assert "unknown_limit" not in persisted["limits"]
    assert persisted["limits"]["batch_max_steps"] == 12


def test_default_mcp_surface_is_schema_gateway_not_flat_tools(tmp_path):
    (tmp_path / "Assets").mkdir()
    (tmp_path / "ProjectSettings").mkdir()
    mcp = _FakeMCP()
    try:
        state = register_gateways(mcp, str(tmp_path), {})

        assert state["operation_count"] == 76
        assert state["gateway_count"] == 14
        assert 0.0 < state["registration_ms"] < 5000.0
        assert 0 < state["compact_schema_bytes"] < 128 * 1024
        assert state["compact_schema_bytes"] < state["full_schema_bytes"] < 512 * 1024
        assert len(mcp.tools) <= MAX_GATEWAY_TOOLS
        assert set(mcp.tools) == {
            "mcp_ping",
            "operation_schema_list",
            "operation_schema_get",
            "operation_schema_search",
            "operation_query_execute",
            "operation_command_execute",
            "operation_workflow_invoke",
            "operation_execute",
            "operation_batch_execute",
            "operation_job_submit",
            "operation_job_status",
            "operation_job_cancel",
            "host_capabilities",
            "host_session_status",
        }
        documents = OperationRegistry.instance().list()
        required = {
            "$schema",
            "id",
            "version",
            "kind",
            "input_schema",
            "output_schema",
            "errors",
            "thread",
            "side_effects",
            "reversible",
            "capabilities",
            "cost",
        }
        assert len(documents) == 76
        assert all(required <= set(document) for document in documents)
        operation_ids = {document["id"] for document in documents}
        assert {
            "infernux.project.info",
            "infernux.mcp.checkpoint.list",
            "infernux.mcp.checkpoint.status",
            "infernux.mcp.supervisor.shutdown",
            "infernux.mcp.attempt.start",
            "infernux.mcp.attempt.stop",
            "infernux.mcp.blocker.report",
            "infernux.scene.object.create",
            "infernux.scene.object.transform.set",
            "infernux.scene.component.property.set",
            "infernux.asset.create",
            "infernux.asset.move",
            "infernux.material.property.set",
            "infernux.material.slot.assign",
            "infernux.particle.graph.document.replace",
            "infernux.camera.editor.state.set",
            "infernux.runtime.play",
            "infernux.input.key",
            "infernux.ui.semantic.snapshot",
            "infernux.capture.request",
            "infernux.player.validation.launch",
            "infernux.docs.search",
            "infernux.console.read",
        } <= operation_ids
        assert not any(
            operation_id.startswith(
                (
                    "authoring_",
                    "assets_",
                    "camera_",
                    "particle_",
                    "runtime_",
                    "mcp_session_",
                )
            )
            for operation_id in operation_ids
        )
    finally:
        shutdown_adapter()
    assert OperationRegistry.instance().list() == ()
    assert adapter_status()["active"] is False


def test_schema_search_and_execution_use_formal_operation_ids(tmp_path):
    (tmp_path / "Assets").mkdir()
    (tmp_path / "ProjectSettings").mkdir()
    mcp = _FakeMCP()
    try:
        register_gateways(mcp, str(tmp_path), {})
        expected = {
            "project": "infernux.project.info",
            "checkpoint": "infernux.mcp.checkpoint.list",
            "attempt": "infernux.mcp.attempt.start",
            "shutdown": "infernux.mcp.supervisor.shutdown",
        }
        for query, operation in expected.items():
            result = mcp.tools["operation_schema_search"](query, 200)
            assert result["ok"] is True
            assert operation in {
                item["id"] for item in result["data"]["operations"]
            }

        missing_search = mcp.tools["operation_schema_search"]("missing.operation", 200)
        assert missing_search["ok"] is True
        assert missing_search["data"]["operations"] == []

        result = mcp.tools["operation_query_execute"](
            "infernux.mcp.checkpoint.list", {}
        )
        assert result["ok"] is True
        assert "checkpoints" in result["data"]["result"]
        missing_execute = mcp.tools["operation_query_execute"]("missing.operation", {})
        assert missing_execute["ok"] is False
        assert missing_execute["error"]["code"] == "operation.not_found"
        mismatch = mcp.tools["operation_command_execute"](
            "infernux.mcp.checkpoint.list", {}
        )
        assert mismatch["ok"] is False
        assert mismatch["error"]["code"] == "operation.kind_mismatch"
        rejected = mcp.tools["operation_command_execute"](
            "infernux.mcp.supervisor.shutdown",
            {"lease_token": "not-a-live-lease"},
        )
        assert rejected["ok"] is False
        assert rejected["error"]["code"] == "mcp.supervisor_lease"
        host_status = mcp.tools["host_session_status"]()
        assert host_status["ok"] is True
        assert host_status["data"]["session"]["project_root"] == str(tmp_path)
    finally:
        shutdown_adapter()


def test_schema_gateway_can_create_and_transform_real_scene_object(tmp_path, scene):
    from Infernux.engine.hierarchy_creation_service import HierarchyCreationService
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.undo import UndoManager
    from Infernux.host import MainThreadCommandQueue

    (tmp_path / "Assets").mkdir()
    (tmp_path / "ProjectSettings").mkdir()
    previous_creation = HierarchyCreationService._instance
    previous_undo = UndoManager._instance
    core = EditorInteractionCore()
    undo = UndoManager(core.action_journal)
    creation = HierarchyCreationService()
    HierarchyCreationService._instance = creation
    queue = MainThreadCommandQueue.instance()
    queue.drain()
    mcp = _FakeMCP()
    try:
        register_gateways(mcp, str(tmp_path), {})
        created = mcp.tools["operation_command_execute"](
            "infernux.scene.object.create",
            {"kind": "empty", "name": "SchemaCreated"},
        )
        assert created["ok"] is True
        object_id = created["data"]["result"]["id"]
        assert scene.find_by_id(object_id).name == "SchemaCreated"

        transformed = mcp.tools["operation_command_execute"](
            "infernux.scene.object.transform.set",
            {
                "object_id": object_id,
                "position": [1.0, 2.0, 3.0],
                "rotation": [10.0, 20.0, 30.0],
                "scale": [2.0, 2.0, 2.0],
            },
        )
        assert transformed["ok"] is True

        hierarchy = mcp.tools["operation_query_execute"](
            "infernux.scene.hierarchy.get",
            {},
        )
        assert hierarchy["ok"] is True
        authored = next(
            item
            for item in hierarchy["data"]["result"]["objects"]
            if item["id"] == object_id
        )
        assert authored["transform"] == {
            "position": [1.0, 2.0, 3.0],
            "rotation": [10.0, 20.0, 30.0],
            "scale": [2.0, 2.0, 2.0],
        }
        assert len(undo.action_journal.applied_entries()) == 2
    finally:
        shutdown_adapter()
        core.shutdown()
        UndoManager._instance = previous_undo
        HierarchyCreationService._instance = previous_creation
        queue.release_owner("MCP editing operation test finished")


def test_schema_gateway_can_edit_real_material_document(engine):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.ui import project_file_ops
    from Infernux.engine.undo import UndoManager
    from Infernux.host import MainThreadCommandQueue
    from Infernux.plugins import PluginManager

    database = engine.get_asset_database()
    project_root = Path(database.project_root)
    assets = project_root / "Assets"
    assets.mkdir(exist_ok=True)
    name = f"McpMaterial_{uuid.uuid4().hex}"
    path = assets / f"{name}.mat"
    created, error = project_file_ops.create_material(
        str(assets),
        name,
        database,
    )
    assert created, error
    guid = database.get_guid_from_path(str(path))
    assert guid

    previous_plugins = PluginManager._instance
    previous_database = AssetManager._asset_database
    previous_undo = UndoManager._instance
    core = EditorInteractionCore()
    core.project_assets.configure(str(project_root), database)
    undo = UndoManager(core.action_journal)
    manager = PluginManager(str(project_root), engine=engine)
    PluginManager._instance = manager
    AssetManager._asset_database = database
    queue = MainThreadCommandQueue.instance()
    queue.drain()
    mcp = _FakeMCP()
    try:
        register_gateways(mcp, str(project_root), {})
        changed = mcp.tools["operation_command_execute"](
            "infernux.material.property.set",
            {
                "asset_guid": guid,
                "pointer": "/properties/baseColor/value",
                "value": [0.2, 0.4, 0.6, 1.0],
            },
        )
        assert changed["ok"] is True
        assert changed["data"]["result"]["document"]["properties"]["baseColor"]["value"] == [
            0.2,
            0.4,
            0.6,
            1.0,
        ]
        for _ in range(100):
            AssetManager.poll_pending_asset_writes()
            document = json.loads(path.read_text(encoding="utf-8"))
            if document["properties"]["baseColor"]["value"] == pytest.approx(
                [0.2, 0.4, 0.6, 1.0]
            ):
                break
            time.sleep(0.01)
        assert document["properties"]["baseColor"]["value"] == pytest.approx(
            [0.2, 0.4, 0.6, 1.0]
        )
        assert len(undo.action_journal.applied_entries()) == 1
    finally:
        shutdown_adapter()
        manager.shutdown()
        core.shutdown()
        UndoManager._instance = previous_undo
        PluginManager._instance = previous_plugins
        AssetManager._asset_database = previous_database
        queue.release_owner("MCP material editing operation test finished")
        database.delete_asset(str(path))
        path.unlink(missing_ok=True)
        Path(str(path) + ".meta").unlink(missing_ok=True)


def test_schema_gateway_can_validate_and_edit_real_particle_graph(engine):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.ui import project_file_ops
    from Infernux.engine.undo import UndoManager
    from Infernux.host import MainThreadCommandQueue
    from Infernux.particle.asset import ParticleGraphAsset
    from Infernux.plugins import PluginManager

    database = engine.get_asset_database()
    project_root = Path(database.project_root)
    assets = project_root / "Assets"
    assets.mkdir(exist_ok=True)
    name = f"McpParticle_{uuid.uuid4().hex}"
    path = assets / f"{name}.particlegraph"
    created, error = project_file_ops.create_particlegraph(
        str(assets),
        name,
        database,
    )
    assert created, error
    guid = database.get_guid_from_path(str(path))
    assert guid

    previous_plugins = PluginManager._instance
    previous_database = AssetManager._asset_database
    previous_undo = UndoManager._instance
    core = EditorInteractionCore()
    core.project_assets.configure(str(project_root), database)
    undo = UndoManager(core.action_journal)
    manager = PluginManager(str(project_root), engine=engine)
    PluginManager._instance = manager
    AssetManager._asset_database = database
    queue = MainThreadCommandQueue.instance()
    queue.drain()
    mcp = _FakeMCP()
    try:
        register_gateways(mcp, str(project_root), {})
        changed = mcp.tools["operation_command_execute"](
            "infernux.particle.graph.property.set",
            {
                "asset_guid": guid,
                "pointer": "/name",
                "value": "Schema Particle Graph",
            },
        )
        assert changed["ok"] is True
        result = changed["data"]["result"]
        assert result["document"]["name"] == "Schema Particle Graph"
        assert len(result["semantic_hash"]) == 64
        assert ParticleGraphAsset.load(str(path)).name == "Schema Particle Graph"
        assert len(undo.action_journal.applied_entries()) == 1
    finally:
        shutdown_adapter()
        manager.shutdown()
        core.shutdown()
        UndoManager._instance = previous_undo
        PluginManager._instance = previous_plugins
        AssetManager._asset_database = previous_database
        queue.release_owner("MCP Particle Graph editing operation test finished")
        database.delete_asset(str(path))
        path.unlink(missing_ok=True)
        Path(str(path) + ".meta").unlink(missing_ok=True)


def test_schema_gateway_can_create_move_and_delete_guid_asset(engine):
    from Infernux.core.assets import AssetManager
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.undo import UndoManager
    from Infernux.host import MainThreadCommandQueue
    from Infernux.plugins import PluginManager

    database = engine.get_asset_database()
    project_root = Path(database.project_root)
    assets = project_root / "Assets"
    assets.mkdir(exist_ok=True)
    name = f"McpAsset_{uuid.uuid4().hex}"
    original = assets / f"{name}.mat"
    moved = assets / f"{name}_Moved.mat"

    previous_plugins = PluginManager._instance
    previous_database = AssetManager._asset_database
    previous_undo = UndoManager._instance
    core = EditorInteractionCore()
    core.project_assets.configure(str(project_root), database)
    undo = UndoManager(core.action_journal)
    manager = PluginManager(str(project_root), engine=engine)
    PluginManager._instance = manager
    AssetManager._asset_database = database
    queue = MainThreadCommandQueue.instance()
    queue.drain()
    mcp = _FakeMCP()
    try:
        register_gateways(mcp, str(project_root), {})
        created = mcp.tools["operation_command_execute"](
            "infernux.asset.create",
            {
                "kind": "material",
                "directory": str(assets),
                "name": name,
            },
        )
        assert created["ok"] is True
        guid = created["data"]["result"]["asset"]["guid"]
        assert guid and original.is_file()

        moved_result = mcp.tools["operation_command_execute"](
            "infernux.asset.move",
            {"asset_guid": guid, "destination": str(moved)},
        )
        assert moved_result["ok"] is True
        assert moved_result["data"]["result"]["asset"]["guid"] == guid
        assert moved.is_file() and not original.exists()

        deleted = mcp.tools["operation_command_execute"](
            "infernux.asset.delete",
            {"asset_guids": [guid]},
        )
        assert deleted["ok"] is True
        assert not moved.exists()
        assert len(undo.action_journal.applied_entries()) == 3
    finally:
        shutdown_adapter()
        manager.shutdown()
        core.shutdown()
        UndoManager._instance = previous_undo
        PluginManager._instance = previous_plugins
        AssetManager._asset_database = previous_database
        queue.release_owner("MCP GUID asset operation test finished")
        for candidate in (original, moved):
            database.delete_asset(str(candidate))
            candidate.unlink(missing_ok=True)
            Path(str(candidate) + ".meta").unlink(missing_ok=True)


def test_server_start_rejects_occupied_port_without_false_loaded_state(tmp_path):
    (tmp_path / "Assets").mkdir()
    (tmp_path / "ProjectSettings").mkdir()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        reservation.listen(1)
        port = reservation.getsockname()[1]
        with pytest.raises(RuntimeError, match="failed before becoming ready"):
            server.start_server(str(tmp_path), port=port)
    assert server.is_running() is False
    assert adapter_status()["active"] is False
