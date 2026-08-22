from __future__ import annotations

import builtins

from Infernux.application import Application
from Infernux.engine import component_restore


def test_player_component_restore_does_not_import_editor_gizmos(monkeypatch):
    monkeypatch.setattr(Application, "is_editor", staticmethod(lambda: False))
    original_import = builtins.__import__

    def reject_gizmos(name, *args, **kwargs):
        if name == "Infernux.gizmos" or name.startswith("Infernux.gizmos."):
            raise AssertionError("Player component restore imported editor Gizmos")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_gizmos)

    component_restore._notify_editor_scene_changed()
