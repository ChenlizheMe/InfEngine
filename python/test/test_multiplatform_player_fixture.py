from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "multiplatform_player"


def test_multiplatform_player_fixture_has_a_buildable_camera_scene():
    settings = json.loads(
        (FIXTURE / "ProjectSettings" / "BuildSettings.json").read_text(
            encoding="utf-8"
        )
    )
    assert settings["scenes"] == ["Assets/Scenes/Main.scene"]
    scene = json.loads(
        (FIXTURE / settings["scenes"][0]).read_text(encoding="utf-8")
    )

    assert scene["mainCameraComponentId"] > 0
    components = [
        component
        for item in scene["objects"]
        for component in item.get("components", [])
    ]
    assert any(item["type_id"] == "native:infernux.Camera" for item in components)
    light = next(item for item in components if item["type_id"] == "native:infernux.Light")
    assert light["data"]["shadows"] != 0
    assert any(
        item["type_id"].endswith(":Scripts.Bootstrap:PlatformFixtureBootstrap")
        for item in components
    )
    render_probe = next(
        item for item in scene["objects"] if item["name"] == "Render Probe"
    )
    renderer = next(
        item
        for item in render_probe["components"]
        if item["type_id"] == "native:infernux.MeshRenderer"
    )
    assert renderer["data"]["inlineMeshName"] == "Cube"
    assert render_probe["transform"]["scale"] == [3.0, 3.0, 3.0]

    shadow_casters = {
        item["name"]: item
        for item in scene["objects"]
        if item["name"].startswith("Shadow Caster ")
    }
    assert set(shadow_casters) == {"Shadow Caster Left", "Shadow Caster Right"}
    assert all(
        item["components"][0]["data"]["castShadows"] is True
        for item in shadow_casters.values()
    )

    shadow_receiver = next(
        item for item in scene["objects"] if item["name"] == "Shadow Receiver"
    )
    receiver_renderer = next(
        item
        for item in shadow_receiver["components"]
        if item["type_id"] == "native:infernux.MeshRenderer"
    )
    assert receiver_renderer["data"]["receivesShadows"] is True
    assert shadow_receiver["transform"]["scale"] == [8.0, 0.4, 8.0]
    material_guid = renderer["data"]["materials"][0]
    material_meta = json.loads(
        (FIXTURE / "Assets" / "Materials" / "RenderProbe.mat.meta").read_text(
            encoding="utf-8"
        )
    )
    assert material_meta["metadata"]["guid"]["value"] == material_guid
    script = FIXTURE / "Assets" / "Scripts" / "Bootstrap.py"
    assert script.is_file()
    source = script.read_text(encoding="utf-8")
    compile(source, str(script), "exec")
    assert "import infernux as inx" in source
    assert "Infernux" not in source
    assert "add_component(inx.ui.UICanvas)" in source
    assert "add_component(inx.ui.UIImage)" in source
    assert "InputActionMap.standard_gameplay()" in source
    assert 'add_component("Rigidbody")' in source
    assert 'add_component("LineRenderer")' in source
    assert "self._body.add_force" in source
    assert "self._trail.set_positions" in source
    assert "Application.package_path" in source
    assert "add_component(inx.ui.UIText)" in source
    package_message = (
        FIXTURE
        / "Packages"
        / "infernux"
        / "multiplatform_probe"
        / "runtime"
        / "message.txt"
    )
    assert package_message.read_text(encoding="utf-8").strip() == (
        "Package resource reached UIText on every Player target."
    )
    registry = json.loads(
        (FIXTURE / "ProjectSettings" / "InxPlugins.json").read_text(
            encoding="utf-8"
        )
    )
    package = registry["installed"][0]
    assert package["reference"] == "infernux/multiplatform_probe"
    assert [item["logical_path"] for item in package["files"]] == [
        "runtime/lifecycle.py",
        "runtime/message.txt",
    ]
    lifecycle = package_message.with_name("lifecycle.py")
    lifecycle_source = lifecycle.read_text(encoding="utf-8")
    compile(lifecycle_source, str(lifecycle), "exec")
    assert 'context.package_path("runtime/message.txt")' in lifecycle_source
