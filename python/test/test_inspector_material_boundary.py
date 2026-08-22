from __future__ import annotations

import builtins

from Infernux.engine.bootstrap_inspector._materials import wire_material_sections


class _InspectorPanel:
    render_material_sections = None


class _Context:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def get_cursor_pos_y(self) -> float:
        return 0.0

    def text_wrapped(self, message: str) -> None:
        self.messages.append(message)


def test_material_section_import_failure_stays_inside_inspector(monkeypatch) -> None:
    panel = _InspectorPanel()
    original_import = builtins.__import__

    def _import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "Infernux.engine.ui" and "inspector_material" in fromlist:
            raise ImportError("missing inspector_material test fixture")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", _import)
    wire_material_sections(
        panel,
        None,
        object(),
        object(),
        lambda _object_id: (None, (), {}, {}),
        lambda _object_id: (None, 0, 0),
        {
            "object_id": 0,
            "scene_version": -1,
            "structure_version": -1,
            "signature": (),
            "entries": [],
        },
    )

    context = _Context()
    panel.render_material_sections(context, 42)

    assert context.messages == ["Material Inspector is temporarily unavailable."]

