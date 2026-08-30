from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from Infernux.engine import player_package_native
from Infernux.plugins import InxPackage, PluginManager
from Infernux.plugins.content import parse_markdown_blocks
from Infernux.plugins.official import install_default_libraries


def _native_available() -> bool:
    try:
        player_package_native._backend()
        return not player_package_native.using_test_backend()
    except Exception:
        return False


def _source(path: Path, reference: str) -> Path:
    path.mkdir(parents=True)
    (path / "InxPackage.json").write_text(
        json.dumps(
            {
                "reference": reference,
                "name": reference,
                "version": "1.0.0",
                "requirements": "requirements.txt",
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.mark.skipif(not _native_available(), reason="native InxPack backend unavailable")
def test_native_inxpackage_installs_only_explicit_nested_requirement(tmp_path):
    child = _source(tmp_path / "child", "native/child")
    (child / "child.txt").write_text("child payload", encoding="utf-8")
    child_package = tmp_path / "Child.inxpkg"
    InxPackage.export_source(str(child), str(child_package))

    parent = _source(tmp_path / "parent", "native/parent")
    (parent / "vendor").mkdir()
    shutil.copy2(child_package, parent / "vendor" / "Child.inxpkg")
    (parent / "parent.txt").write_text("parent payload", encoding="utf-8")
    package_without_requirement = tmp_path / "ParentOptional.inxpkg"
    InxPackage.export_source(str(parent), str(package_without_requirement))

    project = tmp_path / "project"
    (project / "Assets").mkdir(parents=True)
    (project / "ProjectSettings").mkdir()
    manager = PluginManager(str(project))
    manager.install_package(str(package_without_requirement), install_dependencies=True)
    assert {item["reference"] for item in manager.registry.installed()} == {
        "native/parent"
    }
    assert (project / "Assets/Plugins/native/parent/vendor/Child.inxpkg").is_file()
    manager.uninstall("native/parent")

    (parent / "requirements.txt").write_text(
        "vendor/Child.inxpkg\n", encoding="utf-8"
    )
    required_package = tmp_path / "ParentRequired.inxpkg"
    InxPackage.export_source(str(parent), str(required_package))
    manager.install_package(str(required_package), install_dependencies=True)
    assert {item["reference"] for item in manager.registry.installed()} == {
        "native/child",
        "native/parent",
    }
    assert (
        project / "Assets/Plugins/native/child/child.txt"
    ).read_text(encoding="utf-8") == "child payload"
    with pytest.raises(RuntimeError, match="required by"):
        manager.uninstall("native/child")
    manager.uninstall("native/parent")
    manager.uninstall("native/child")


@pytest.mark.skipif(not _native_available(), reason="native InxPack backend unavailable")
def test_official_mcp_default_install_uninstall_stays_absent_and_reinstalls(
    tmp_path, monkeypatch
):
    repository = Path(__file__).parents[2]
    # This scenario intentionally proves that unload removes every plugin
    # module from sys.modules.  Run that destructive interpreter-state check
    # in a child process so pytest modules imported during collection cannot
    # retain stale references to the deliberately unloaded module objects.
    if os.environ.get("INFERNUX_MCP_NATIVE_TEST_CHILD") != "1":
        environment = os.environ.copy()
        environment["INFERNUX_MCP_NATIVE_TEST_CHILD"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                f"{Path(__file__).resolve()}::{test_official_mcp_default_install_uninstall_stays_absent_and_reinstalls.__name__}",
                "-q",
            ],
            cwd=repository,
            env=environment,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=120,
            check=False,
        )
        assert result.returncode == 0, result.stdout + "\n" + result.stderr
        return

    resources = repository / "python" / "Infernux" / "resources"
    artifact = resources / "official_packages" / "infernux.mcp.inxpkg"
    preview = InxPackage.inspect(str(artifact))
    assert preview.metadata["reference"] == "infernux/mcp"
    assert preview.metadata["format_version"] == 2
    assert "preload" not in preview.metadata
    assert "plugin_root" not in preview.metadata
    assert {item["role"] for item in preview.file_records} == {"editor", "control"}
    assert {
        (page["id"], page.get("locale", "")) for page in preview.metadata["pages"]
    } >= {
        ("intro", ""),
        ("intro", "zh-CN"),
        ("operations", ""),
        ("operations", "zh-CN"),
        ("trust", ""),
        ("trust", "zh-CN"),
    }
    assert {
        item["logical_path"]
        for item in preview.file_records
        if item["logical_path"].startswith("InxPluginPages/media/")
    } >= {
        "InxPluginPages/media/agent-loop.png",
        "InxPluginPages/media/system-overview.png",
        "InxPluginPages/media/trust-gates.png",
    }

    project = tmp_path / "project"
    (project / "Assets").mkdir(parents=True)
    (project / "Assets" / "protocol-probe.txt").write_text("probe", encoding="utf-8")
    (project / "ProjectSettings").mkdir()
    mirrored = project / "Library" / "Resources" / "official_packages"
    mirrored.mkdir(parents=True)
    shutil.copy2(artifact, mirrored / artifact.name)

    monkeypatch.setattr(
        PluginManager,
        "_project_python_executable",
        lambda self: sys.executable,
    )
    monkeypatch.setattr(
        PluginManager,
        "_run_process",
        staticmethod(
            lambda command, cwd=None: type(
                "Result",
                (),
                {
                    "stdout": (
                        "[]"
                        if command[2:5] == ["pip", "list", "--format=json"]
                        else "already satisfied"
                    )
                },
            )()
        ),
    )
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    monkeypatch.setenv("INFERNUX_MCP_PORT", str(port))
    for name in tuple(sys.modules):
        if name == "infernux_mcp" or name.startswith("infernux_mcp."):
            sys.modules.pop(name, None)

    manager = PluginManager(str(project), runtime=False)
    states = install_default_libraries(
        str(project),
        resources_root=str(resources),
        manager=manager,
    )
    assert len(states) == 1
    assert states[0].reference == "infernux/mcp"
    assert states[0].loaded is True
    assert PluginManager.instance() is None
    assert (
        project / "Packages/infernux/mcp/Editor/infernux_mcp/lifecycle.py"
    ).is_file()
    assert (
        project / "Packages/infernux/mcp/Editor/infernux_mcp/scene_operations.py"
    ).is_file()
    assert (
        project / "Packages/infernux/mcp/Editor/infernux_mcp/material_operations.py"
    ).is_file()
    assert {item["reference"] for item in manager.registry.available()} == {
        "infernux/mcp",
        "infernux/platform-android",
        "infernux/platform-web",
    }
    record = manager.registry.installed_record("infernux/mcp")
    localized_pages = manager.content_pages(record, locale="zh")
    assert [page["id"] for page in localized_pages] == [
        "intro",
        "operations",
        "trust",
        "license",
    ]
    for page in localized_pages:
        for block in parse_markdown_blocks(page["content"]):
            if block["kind"] == "image":
                assert Path(
                    manager.content_asset_path(record, page, block["source"])
                ).is_file()

    health = None
    for _ in range(100):
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{port}/health", timeout=0.25
            ) as response:
                health = json.load(response)
                break
        except Exception:
            time.sleep(0.05)
    assert health and health["transport"] == "streamable-http"

    from Infernux.host import MainThreadCommandQueue
    from infernux_mcp.client import _json_value, create_loopback_client
    from infernux_mcp.supervisor import SupervisorSession

    supervisor_probe = SupervisorSession(str(project), mcp_port=port)
    observed_session = supervisor_probe._read_host_session_status(timeout_seconds=10.0)
    observed_checkpoints = supervisor_probe._call_mcp_operation(
        "operation_query_execute",
        "infernux.mcp.checkpoint.list",
        {},
        timeout_seconds=10.0,
    )
    assert Path(observed_session["project_root"]).resolve() == project.resolve()
    assert observed_checkpoints == {"checkpoints": []}

    pump_stop = threading.Event()

    def pump_owner_thread():
        queue = MainThreadCommandQueue.instance()
        while not pump_stop.is_set():
            queue.drain()
            time.sleep(0.002)

    pump = threading.Thread(target=pump_owner_thread, name="MCPProtocolTestOwner")
    pump.start()

    async def protocol_probe():
        async with create_loopback_client(
            f"http://127.0.0.1:{port}/mcp", timeout_seconds=10
        ) as client:
            tools = list(await client.list_tools())
            search = _json_value(
                (
                    await client.call_tool(
                        "operation_schema_search", {"query": "checkpoint", "limit": 20}
                    )
                ).data
            )
            checkpoints = _json_value(
                (
                    await client.call_tool(
                        "operation_query_execute",
                        {
                            "operation": "infernux.mcp.checkpoint.list",
                            "arguments": {},
                        },
                    )
                ).data
            )
            authoring = _json_value(
                (
                    await client.call_tool(
                        "operation_schema_search",
                        {"query": "scene create authoring", "limit": 20},
                    )
                ).data
            )
            capabilities = _json_value(
                (await client.call_tool("host_capabilities", {})).data
            )
            return [tool.name for tool in tools], search, checkpoints, authoring, capabilities

    try:
        (
            tool_names,
            search_result,
            checkpoint_result,
            authoring_result,
            capabilities_result,
        ) = asyncio.run(protocol_probe())
    finally:
        pump_stop.set()
        pump.join(2)
        MainThreadCommandQueue.instance().release_owner("Protocol test finished")
    assert len(tool_names) == 14
    assert search_result["ok"] is True
    assert any(
        item["id"] == "infernux.mcp.checkpoint.list"
        for item in search_result["data"]["operations"]
    )
    assert checkpoint_result["ok"] is True
    assert checkpoint_result["data"]["result"] == {"checkpoints": []}
    assert authoring_result["ok"] is True
    assert any(
        item["id"] == "infernux.scene.object.create"
        for item in authoring_result["data"]["operations"]
    )
    assert capabilities_result["ok"] is True
    assert capabilities_result["data"]["operation_count"] == 82

    manager.uninstall("infernux/mcp")
    assert manager.registry.installed() == ()
    for discovery_path in (
        "mcp.json",
        ".cursor/mcp.json",
        ".mcp.json",
        ".vscode/mcp.json",
        ".trae/mcp.json",
        ".gemini/settings.json",
    ):
        assert not (project / discovery_path).exists()
    assert not any(
        name == "infernux_mcp" or name.startswith("infernux_mcp.")
        for name in sys.modules
    )
    manager = PluginManager.startup(str(project), runtime=False)
    assert manager.registry.installed() == ()

    reinstalled = manager.install_reference("infernux/mcp")
    assert reinstalled.loaded is True
    manager.uninstall("infernux/mcp")
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind(("127.0.0.1", port))
