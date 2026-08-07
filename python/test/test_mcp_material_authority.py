from __future__ import annotations

import copy
import os

from Infernux.engine.interaction import ActionOrigin
from Infernux.mcp.tools import material as material_tools


class _Material:
    def __init__(self, value: float = 1.0) -> None:
        self.value = float(value)

    def clone(self):
        return copy.deepcopy(self)

    def serialize_document(self):
        return {"value": self.value}


class _Controller:
    def __init__(self, resource: _Material) -> None:
        self.resource = resource
        self.calls = []

    def apply_document(self, document, **kwargs):
        self.calls.append((copy.deepcopy(document), dict(kwargs)))
        self.resource.value = float(document["value"])
        return True


def test_mcp_material_edit_uses_resource_document_with_automation_origin(monkeypatch):
    material = _Material()
    controller = _Controller(material)
    monkeypatch.setattr(
        material_tools,
        "_editable_material",
        lambda _project, _path: (material, controller),
    )

    result = material_tools._apply_material_edit(
        "Project",
        "Assets/Test.mat",
        lambda candidate: setattr(candidate, "value", 4.0),
        edit_key="property:value",
        description="Set Material value",
    )

    assert result is material
    assert material.value == 4.0
    document, kwargs = controller.calls[0]
    assert document == {"value": 4.0}
    assert kwargs["view_id"] == "automation"
    assert kwargs["origin"] is ActionOrigin.AUTOMATION


def test_mcp_material_setters_have_no_direct_save_or_asset_reload_path():
    source_path = os.path.join(
        os.path.dirname(__file__),
        "..",
        "Infernux",
        "mcp",
        "tools",
        "material.py",
    )
    with open(source_path, "r", encoding="utf-8") as stream:
        source = stream.read()
    setters = source[
        source.index('    @mcp.tool(name="material_set_property")') :
        source.index("def _builtin_material_key")
    ]

    assert ".save(" not in setters
    assert "notify_asset_changed" not in setters
    assert "_load_material(project_path, path)" not in setters
    assert "_apply_material_edit(" in setters
