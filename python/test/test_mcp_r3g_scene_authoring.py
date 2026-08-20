from __future__ import annotations

import pytest

from Infernux.engine.interaction import DocumentActionResult, DocumentActionStatus
from Infernux.mcp.tools import common, scene as scene_tools


def test_script_component_schema_handles_unresolved_asset_and_component_refs():
    from Infernux import AudioClip
    from Infernux.components import AudioSource, InxComponent

    class AudioManagerSchemaProbe(InxComponent):
        background_audio: AudioClip
        player: AudioSource

    fields = {
        field["name"]: field
        for field in scene_tools._component_field_schema(AudioManagerSchemaProbe)
    }

    assert fields["background_audio"]["type"] == "ASSET"
    assert fields["background_audio"]["asset_type"] == "AudioClip"
    assert fields["player"]["type"] == "COMPONENT"
    assert fields["player"]["component_type"] == "AudioSource"


def test_component_schema_resolves_lazy_builtin_enums_with_writable_documents():
    from Infernux.components import Camera, Light

    camera_fields = {
        field["name"]: field for field in scene_tools._component_field_schema(Camera)
    }
    light_fields = {
        field["name"]: field for field in scene_tools._component_field_schema(Light)
    }

    projection = camera_fields["projection_mode"]
    assert projection["enum_type"] == "CameraProjection"
    assert {item["name"] for item in projection["enum_values"]} == {
        "Perspective",
        "Orthographic",
    }
    assert projection["writable"] is True
    assert projection["readonly"] is False
    assert camera_fields["dithering"]["type"] == "BOOL"
    assert camera_fields["stop_nans"]["type"] == "BOOL"
    assert camera_fields["dithering"]["writable"] is True
    assert camera_fields["stop_nans"]["writable"] is True

    shadows = light_fields["shadows"]
    assert shadows["enum_type"] == "LightShadows"
    assert [item["name"] for item in shadows["enum_values"]] == [
        "NoShadows",
        "Hard",
        "Soft",
    ]


def test_batch_fields_read_cpp_property_values_before_command_comparison(
    scene, monkeypatch
):
    owner = scene.create_game_object("McpBatchBox")
    collider = owner.add_component("BoxCollider")
    assert collider is not None
    assert scene_tools._component_field_value(collider, "size") is not None

    class Service:
        def __init__(self):
            self.changes = []

        def execute_property_changes(self, changes, **_kwargs):
            self.changes = list(changes)
            for target, field, _old, new, _description in changes:
                setattr(target, field, new)
            return True

    service = Service()
    monkeypatch.setattr(scene_tools, "_component_command_service", lambda: service)

    changed = scene_tools._set_component_fields_through_editor_transaction(
        collider,
        "BoxCollider",
        {"is_trigger": True, "size": [2.0, 3.0, 4.0]},
    )

    assert changed["is_trigger"] is True
    assert changed["size"] == [2.0, 3.0, 4.0]
    assert {entry[1] for entry in service.changes} == {"is_trigger", "size"}


def test_component_add_rejects_base_types_from_creatability_metadata():
    from Infernux.ui import InxUIComponent, InxUIScreenComponent, UISelectable, UIButton

    for component_type in (InxUIComponent, InxUIScreenComponent, UISelectable):
        with pytest.raises(TypeError, match="abstract or not user-creatable"):
            scene_tools._require_creatable_component_type(component_type.__name__)

    assert scene_tools._require_creatable_component_type("UIButton") is UIButton


def test_scene_discard_is_explicit_and_uses_the_scene_document_controller(monkeypatch):
    class FakeMcp:
        def __init__(self):
            self.tools = {}

        def tool(self, *args, **kwargs):
            name = str(kwargs.get("name") or (args[0] if args else ""))

            def register(fn):
                self.tools[name] = fn
                return fn

            return register

    class FakeSceneFileManager:
        is_dirty = True
        current_scene_path = "C:/Project/Assets/Main.scene"
        document_id = "scene-doc"

        @classmethod
        def instance(cls):
            return cls.instance_value

    FakeSceneFileManager.instance_value = FakeSceneFileManager()

    class FakeRegistry:
        def __init__(self):
            self.requested = []

        def request_discard(self, document_id):
            self.requested.append(document_id)
            return DocumentActionResult(DocumentActionStatus.APPLIED)

    registry = FakeRegistry()
    monkeypatch.setattr(
        "Infernux.engine.scene_manager.SceneFileManager",
        FakeSceneFileManager,
    )
    monkeypatch.setattr(
        "Infernux.engine.interaction.DocumentRegistry.instance",
        classmethod(lambda cls: registry),
    )
    monkeypatch.setattr(scene_tools, "scene_status", lambda: {"dirty": False})
    monkeypatch.setattr(
        scene_tools,
        "main_thread",
        lambda _name, fn, **_kwargs: fn(),
    )

    mcp = FakeMcp()
    scene_tools.register_scene_tools(mcp)
    discard = mcp.tools["scene_discard"]

    with pytest.raises(ValueError, match="force=true"):
        discard()
    with pytest.raises(ValueError, match="requires a reason"):
        discard(force=True)

    result = discard(force=True, reason="R3G scene reset")
    assert result["discarded"] is True
    assert registry.requested == ["scene-doc"]


def test_mesh_renderer_material_slots_are_public_asset_fields(monkeypatch):
    class MaterialSlots:
        def get_material_guids(self):
            return ["material-guid"]

        def set_material(self, _slot, _value):
            pass

    fields = {
        item["name"]: item
        for item in scene_tools._component_field_schema(MaterialSlots)
    }
    assert fields["material"]["type"] == "MATERIAL"
    assert fields["material"]["asset_type"] == "Material"
    assert fields["materials"]["type"] == "LIST"
    assert fields["materials"]["element_type"] == "MATERIAL"

    monkeypatch.setattr(common, "get_asset_database", lambda: None)
    component = MaterialSlots()
    assert scene_tools._component_field_value(component, "materials") == [
        "material-guid"
    ]
    assert scene_tools._coerce_component_property_value(
        component,
        "material",
        "12345678-material-guid",
        "material-guid",
    ) == "12345678-material-guid"
    with pytest.raises(TypeError, match="Material asset reference"):
        scene_tools._coerce_component_property_value(
            component,
            "material",
            {"$type": "asset_ref", "asset_type": "Texture", "guid": "x", "path_hint": ""},
            "",
        )

    class Database:
        def get_path_from_guid(self, _guid):
            return ""

        def get_guid_from_path(self, _path):
            return ""

    monkeypatch.setattr(common, "get_asset_database", lambda: Database())
    with pytest.raises(ValueError, match="not registered"):
        scene_tools._coerce_component_property_value(
            component,
            "material",
            "unregistered-material-guid",
            "",
        )
