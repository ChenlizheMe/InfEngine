"""Public MCP registration, API, client, and asset-state contracts."""

from __future__ import annotations

import json

import pytest

from Infernux.mcp import capabilities, session
from Infernux.mcp import client as client_module
from Infernux.mcp.client import create_loopback_client
from Infernux.mcp.threading import MainThreadCommandQueue
from Infernux.mcp.tools import register_all_tools
from Infernux.mcp.tools import api, common, material, project, runtime


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        name = str(kwargs.get("name") or (args[0] if args else ""))

        def _register(fn):
            self.tools[name] = fn
            return fn

        return _register


class _AssetDatabase:
    def __init__(self, path: str, guid: str) -> None:
        self.path = path
        self.guid = guid
        self.refresh_pending = False
        self.query_generation = 7

    def get_path_from_guid(self, guid: str) -> str:
        return self.path if guid == self.guid else ""

    def get_guid_from_path(self, path: str) -> str:
        return self.guid if project._normalize(path) == project._normalize(self.path) else ""

    def contains_path(self, path: str) -> bool:
        return bool(self.get_guid_from_path(path))

    def contains_guid(self, guid: str) -> bool:
        return guid == self.guid


class _MaterialPropertyRecorder:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}

    def set_float(self, name: str, value: float) -> None:
        self.values[name] = value


class _Vector4:
    x = 0.25
    y = 0.5
    z = 0.75
    w = 0.125


def test_mcp_value_serialization_preserves_vector4_w_component():
    assert common.serialize_value(_Vector4()) == [0.25, 0.5, 0.75, 0.125]


def test_runtime_error_window_ignores_preexisting_errors():
    old = {"time": "00:00:01", "level": "ERROR", "message": "old"}
    new = {"time": "00:00:02", "level": "ERROR", "message": "new"}

    assert runtime._new_runtime_errors([old, new], [old]) == [new]
    assert runtime._new_runtime_errors([old], [old]) == []


def test_material_property_writer_rejects_render_state_names():
    recorder = _MaterialPropertyRecorder()

    with pytest.raises(ValueError, match="render state"):
        material._set_one(recorder, "blend_enable", 1.0, "float")

    material._set_one(recorder, "emission_strength", 2.0, "float")
    assert recorder.values == {"emission_strength": 2.0}


def _registered_mcp(tmp_path, profile: str) -> _FakeMcp:
    settings = tmp_path / "ProjectSettings"
    settings.mkdir()
    (settings / "mcp_capabilities.json").write_text(
        json.dumps({"profile": profile}), encoding="utf-8"
    )
    config = capabilities.configure(str(tmp_path), write_default=False)
    session.configure(str(tmp_path), config)
    fake = _FakeMcp()
    register_all_tools(fake, str(tmp_path), config)
    return fake


def _registered_tools(tmp_path, profile: str) -> set[str]:
    return set(_registered_mcp(tmp_path, profile).tools)


def test_developer_assist_exposes_scripts_and_semantic_scene_authoring(tmp_path):
    tools = _registered_tools(tmp_path, "developer_assist")

    assert {
        "mcp_session_status",
        "mcp_checkpoint_status",
        "mcp_supervisor_shutdown",
        "mcp_attempt_start",
        "mcp_attempt_stop",
        "editor_save_focused",
        "editor_save_document",
        "project_script_write",
        "project_build_scenes_get",
        "project_build_scenes_set",
        "public_api_validate_script",
        "scene_new",
        "scene_save",
        "hierarchy_create_object",
        "component_ensure",
        "ui_bind_click",
        "material_set_render_queue",
        "material_set_surface_type",
        "render_effect_create",
        "asset_create_particle_graph",
        "particle_graph_inspect_editor",
        "particle_graph_list_node_types",
        "particle_graph_set_node_asset",
        "particle_graph_set_rendering_output",
        "particle_graph_reload_editor",
        "scene_pick_request",
        "scene_pick_status",
        "runtime_wait",
        "runtime_run_for",
        "runtime_read_errors",
    } <= tools
    assert "mcp_report_blocker" not in tools
    assert "input_key" not in tools
    assert "editor_ui_click" not in tools


def test_global_validation_exposes_blocker_tools_without_script_or_scene_mutation(tmp_path):
    tools = _registered_tools(tmp_path, "global_validation")

    assert {
        "mcp_session_status",
        "mcp_checkpoint_status",
        "mcp_supervisor_shutdown",
        "mcp_blocker_template",
        "mcp_report_blocker",
        "project_asset_state",
        "project_wait_for_asset",
        "project_build_scenes_get",
        "runtime_assert",
        "particle_system_inspect_runtime",
        "particle_system_start_emitter",
        "particle_system_pause_emitter",
        "particle_system_terminate_emitter",
        "particle_system_restart_emitter",
        "input_key",
        "input_text",
        "editor_ui_snapshot",
        "editor_ui_wait_for_window_focus",
        "editor_ui_click",
        "editor_ui_double_click",
        "editor_ui_set_checkbox",
        "editor_ui_hover",
    } <= tools
    assert "project_script_write" not in tools
    assert "project_build_scenes_set" not in tools
    assert "release_whl_read_source" not in tools
    assert "scene_new" not in tools
    assert "editor_select" not in tools
    assert "editor_play" not in tools


def test_global_validation_discovery_only_describes_registered_tools(tmp_path):
    fake = _registered_mcp(tmp_path, "global_validation")

    verbose = fake.tools["mcp_list_tools_verbose"]()
    names = {item["name"] for item in verbose["data"]["tools"]}
    unavailable_help = fake.tools["mcp_help"]("scene_new")
    concept = fake.tools["engine_concept_get"]("GameObject")
    capability_groups = fake.tools["mcp_capabilities"]()["data"]["groups"]

    assert "editor_ui_snapshot" in names
    assert "scene_new" not in names
    assert "hierarchy_create_object" not in names
    assert "asset_write_text" not in names
    assert unavailable_help["data"]["found"] is False
    assert concept["data"]["tools"] == []
    assert "scene" not in capability_groups


def test_global_validation_trace_records_final_runtime_response_once(tmp_path, monkeypatch):
    fake = _registered_mcp(tmp_path, "global_validation")
    monkeypatch.setattr(runtime, "_run_on_main", lambda _name, fn: fn())

    session.start_attempt("runtime oracle trace coverage", "before-runtime-oracle")
    response = fake.tools["runtime_assert"]([{"kind": "unknown"}])
    stopped = session.stop_attempt()

    with open(tmp_path / stopped["trace_path"], "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert response["data"]["passed"] is False
    assert [step["tool"] for step in saved["steps"]] == ["runtime_assert"]
    assert saved["steps"][0]["arguments"] == {"assertions": [{"kind": "unknown"}]}
    assert saved["steps"][0]["result"]["data"]["passed"] is False


def test_developer_assist_attempt_traces_lint_clean_script_write(tmp_path):
    fake = _registered_mcp(tmp_path, "developer_assist")

    fake.tools["mcp_attempt_start"]("write public project behavior", "before-script-write")
    response = fake.tools["project_script_write"](
        "Racing/Hud.py",
        "from Infernux.components import Component\n",
    )
    stopped = fake.tools["mcp_attempt_stop"]()

    with open(tmp_path / stopped["data"]["trace_path"], "r", encoding="utf-8") as f:
        saved = json.load(f)
    assert response["ok"] is True
    assert [step["tool"] for step in saved["steps"]] == ["project_script_write"]
    assert saved["context"]["mode"] == "developer_assist"


# Public API/client and project-asset contracts live here with profile policy
# checks: they use the same isolated fake registry and do not need separate
# pytest modules.

def _api_tools() -> dict[str, object]:
    fake = _FakeMcp()
    api.register_api_tools(fake)
    return fake.tools


def test_api_subsystems_does_not_eagerly_build_static_index(monkeypatch):
    monkeypatch.setattr(api, "_API_INDEX", None)
    monkeypatch.setattr(api, "_api_index", lambda: (_ for _ in ()).throw(AssertionError("unexpected index build")))

    response = _api_tools()["api_subsystems"]()

    assert response["ok"] is True
    assert response["data"]["python_api"]["state"] == "cold"
    assert any(item["name"] == "scripting" for item in response["data"]["subsystems"])


def test_api_get_input_returns_stable_public_imports_without_index(monkeypatch):
    monkeypatch.setattr(api, "_API_INDEX", None)
    monkeypatch.setattr(api, "_api_index", lambda: (_ for _ in ()).throw(AssertionError("unexpected index build")))

    response = _api_tools()["api_get"]("input")

    assert response["ok"] is True
    data = response["data"]
    assert data["kind"] == "subsystem"
    assert "from Infernux.input import Input, KeyCode" in data["stable_imports"]
    assert any("KeyCode.W" in item for item in data["concepts"])


def test_api_search_exact_guide_avoids_runtime_reflection(monkeypatch):
    monkeypatch.setattr(api, "_API_INDEX", None)
    monkeypatch.setattr(api, "_api_index", lambda: (_ for _ in ()).throw(AssertionError("unexpected index build")))
    monkeypatch.setattr(api, "_symbol_doc", lambda _: (_ for _ in ()).throw(AssertionError("unexpected reflection")))

    response = _api_tools()["api_search"]("input", limit=1)

    assert response["ok"] is True
    assert response["data"]["search_mode"] == "guide_exact"
    assert response["data"]["matches"] == [{
        "kind": "subsystem",
        "name": "input",
        "summary": api.SUBSYSTEM_GUIDES["input"]["summary"],
        "score": 100,
    }]


def test_api_search_public_symbol_alias_uses_its_guide_without_index(monkeypatch):
    monkeypatch.setattr(api, "_API_INDEX", None)
    monkeypatch.setattr(api, "_api_index", lambda: (_ for _ in ()).throw(AssertionError("unexpected index build")))

    response = _api_tools()["api_search"]("KeyCode", limit=1)

    assert response["ok"] is True
    assert response["data"]["matches"][0]["name"] == "input"


def test_api_search_uses_static_index_without_runtime_symbol_docs(monkeypatch):
    index = {
        "symbols": {
            "Mover": {
                "name": "Mover",
                "qualname": "Infernux.example.Mover",
                "module": "Infernux.example",
                "doc": "Moves a vehicle.",
                "methods": [{"name": "move"}],
                "properties": [],
                "attributes": [],
            },
            "Infernux.example.Mover": {
                "name": "Mover",
                "qualname": "Infernux.example.Mover",
                "module": "Infernux.example",
                "doc": "Moves a vehicle.",
                "methods": [{"name": "move"}],
                "properties": [],
                "attributes": [],
            },
        },
        "modules": {},
    }
    monkeypatch.setattr(api, "_api_index", lambda: index)
    monkeypatch.setattr(api, "_symbol_doc", lambda _: (_ for _ in ()).throw(AssertionError("unexpected reflection")))
    monkeypatch.setattr(api, "_scan_shaders", lambda: (_ for _ in ()).throw(AssertionError("unexpected shader scan")))

    response = _api_tools()["api_search"]("move")

    assert response["ok"] is True
    assert response["data"]["search_mode"] == "static_index"
    assert [item["name"] for item in response["data"]["matches"] if item["kind"] == "symbol"] == ["Mover"]


def test_loopback_client_rejects_network_endpoints():
    with pytest.raises(ValueError, match="Loopback MCP client"):
        create_loopback_client("http://0.0.0.0:9713/mcp")


def test_loopback_client_builds_a_fastmcp_client_without_proxy_inheritance():
    client = create_loopback_client("http://127.0.0.1:9713/mcp")

    assert client.transport.url == "http://127.0.0.1:9713/mcp"
    assert client.transport.httpx_client_factory is not None


def test_cli_emits_machine_readable_json(monkeypatch, capsys):
    async def fake_run(args):
        return {"command": args.command, "tools": ["mcp_session_status"]}

    monkeypatch.setattr(client_module, "_run_cli", fake_run)

    assert client_module.main(["list-tools"]) == 0
    assert '"mcp_session_status"' in capsys.readouterr().out


def test_cli_parses_describe_command(monkeypatch, capsys):
    async def fake_run(args):
        return {"command": args.command, "tool": args.tool}

    monkeypatch.setattr(client_module, "_run_cli", fake_run)

    assert client_module.main(["describe", "mcp_attempt_start"]) == 0
    assert '"tool": "mcp_attempt_start"' in capsys.readouterr().out


def test_expired_main_thread_command_never_executes():
    queue = MainThreadCommandQueue()
    executed: list[bool] = []
    future = queue.submit("expired", lambda: executed.append(True), timeout_ms=1)

    future._deadline = 0.0
    queue.drain()

    assert executed == []
    with pytest.raises(TimeoutError):
        future.result(timeout=0.01)


def test_wait_timeout_cancels_queued_command_before_drain():
    queue = MainThreadCommandQueue()
    executed: list[bool] = []
    future = queue.submit("cancelled", lambda: executed.append(True), timeout_ms=1000)

    with pytest.raises(TimeoutError):
        future.result(timeout=0.001)
    queue.drain()

    assert executed == []


def test_project_asset_state_requires_disk_meta_and_database_identity(tmp_path, monkeypatch):
    assets = tmp_path / "Assets"
    assets.mkdir()
    asset = assets / "Checkpoint.prefab"
    asset.write_text("prefab", encoding="utf-8")
    (assets / "Checkpoint.prefab.meta").write_text("meta", encoding="utf-8")
    database = _AssetDatabase(str(asset), "prefab-guid")
    monkeypatch.setattr(project, "get_asset_database", lambda: database)

    state = project._read_asset_state(str(tmp_path), str(asset), "prefab-guid")

    assert state["requested_path"] == "Assets/Checkpoint.prefab"
    assert state["database_path"] == "Assets/Checkpoint.prefab"
    assert state["database_guid"] == "prefab-guid"
    assert state["mapping_consistent"] is True
    assert project._asset_expectation_met(state, True) is True

    guid_state = project._read_asset_state(str(tmp_path), "", "prefab-guid")
    assert guid_state["database_path"] == "Assets/Checkpoint.prefab"
    assert project._asset_expectation_met(guid_state, True) is True


def test_project_asset_state_rejects_stale_database_mapping_after_delete(tmp_path, monkeypatch):
    assets = tmp_path / "Assets"
    assets.mkdir()
    asset = assets / "Deleted.prefab"
    database = _AssetDatabase(str(asset), "deleted-guid")
    monkeypatch.setattr(project, "get_asset_database", lambda: database)

    state = project._read_asset_state(str(tmp_path), str(asset), "deleted-guid")

    assert state["path_exists"] is False
    assert state["database_contains_path"] is True
    assert project._asset_expectation_met(state, False) is False

    database.path = ""
    database.guid = ""
    state = project._read_asset_state(str(tmp_path), str(asset), "deleted-guid")
    assert project._asset_expectation_met(state, False) is True


def test_project_asset_state_settles_directories_without_file_meta_or_guid(tmp_path, monkeypatch):
    folder = tmp_path / "Assets" / "VFX"
    folder.mkdir(parents=True)
    database = _AssetDatabase("", "")
    monkeypatch.setattr(project, "get_asset_database", lambda: database)

    state = project._read_asset_state(str(tmp_path), str(folder), "")

    assert state["path_exists"] is True
    assert state["path_kind"] == "directory"
    assert state["meta_exists"] is False
    assert state["mapping_consistent"] is False
    assert project._asset_expectation_met(state, True) is True


def test_project_build_scenes_set_validates_and_preserves_other_settings(tmp_path, monkeypatch):
    assets = tmp_path / "Assets" / "Scenes"
    assets.mkdir(parents=True)
    start = assets / "Start.scene"
    gallery = assets / "VFX Gallery.scene"
    start.write_text("{}", encoding="utf-8")
    gallery.write_text("{}", encoding="utf-8")
    settings = tmp_path / "ProjectSettings"
    settings.mkdir()
    build_settings = settings / "BuildSettings.json"
    build_settings.write_text(
        json.dumps({"game_name": "Gallery", "lto": True, "scenes": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(project, "track_project_path_before_change", lambda *_args, **_kwargs: None)

    result = project._set_build_scenes(
        str(tmp_path),
        ["Assets/Scenes/Start.scene", "Assets/Scenes/VFX Gallery.scene"],
    )

    saved = json.loads(build_settings.read_text(encoding="utf-8"))
    assert result["scenes"] == [
        "Assets/Scenes/Start.scene",
        "Assets/Scenes/VFX Gallery.scene",
    ]
    assert result["startup_scene"] == "Assets/Scenes/Start.scene"
    assert all(entry["exists"] and entry["valid_path"] for entry in result["entries"])
    assert saved["game_name"] == "Gallery"
    assert saved["lto"] is True
    assert saved["scenes"] == [
        "Assets/Scenes/Start.scene",
        "Assets/Scenes/VFX Gallery.scene",
    ]

    with pytest.raises(ValueError, match="Duplicate build scene"):
        project._set_build_scenes(
            str(tmp_path),
            ["Assets/Scenes/Start.scene", "Assets/Scenes/Start.scene"],
        )
    with pytest.raises(ValueError, match="must reference a .scene"):
        project._set_build_scenes(str(tmp_path), ["Assets/Scenes/NotAScene.txt"])


def test_asset_identity_uses_samefile_for_alias_paths(tmp_path, monkeypatch):
    asset = tmp_path / "Assets" / "Alias.prefab"
    asset.parent.mkdir()
    asset.write_text("prefab", encoding="utf-8")
    alias = str(asset).upper()
    monkeypatch.setattr(project.os.path, "exists", lambda value: value in {str(asset), alias})
    monkeypatch.setattr(project.os.path, "samefile", lambda first, second: {first, second} == {str(asset), alias})

    assert project._same_path(str(asset), alias) is True
