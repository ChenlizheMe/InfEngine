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
    assert any(item["type_id"] == "native:infernux.Light" for item in components)
    renderer = next(
        item for item in components if item["type_id"] == "native:infernux.MeshRenderer"
    )
    assert renderer["data"]["inlineMeshName"] == "Cube"
    material_guid = renderer["data"]["materials"][0]
    material_meta = json.loads(
        (FIXTURE / "Assets" / "Materials" / "RenderProbe.mat.meta").read_text(
            encoding="utf-8"
        )
    )
    assert material_meta["metadata"]["guid"]["value"] == material_guid
    script = FIXTURE / "Assets" / "Scripts" / "Bootstrap.py"
    assert script.is_file()
    compile(script.read_text(encoding="utf-8"), str(script), "exec")
