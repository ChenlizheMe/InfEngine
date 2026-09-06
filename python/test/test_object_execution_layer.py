from __future__ import annotations

from types import SimpleNamespace

import pytest

import Infernux.lib as native_lib
from Infernux.engine.ui.object_execution_layer import ObjectExecutionLayer


def test_empty_selection_has_no_object():
    assert ObjectExecutionLayer.resolve_selected_object(0) is None


def test_selected_object_resolution_exposes_scene_manager_failure(monkeypatch):
    class BrokenSceneManager:
        @staticmethod
        def instance():
            raise RuntimeError("scene manager unavailable")

    monkeypatch.setattr(native_lib, "SceneManager", BrokenSceneManager)

    with pytest.raises(RuntimeError, match="scene manager unavailable"):
        ObjectExecutionLayer.resolve_selected_object(42)


def test_selected_object_is_resolved_from_active_scene(monkeypatch):
    selected = object()
    scene = SimpleNamespace(find_by_id=lambda object_id: selected if object_id == 42 else None)
    manager = SimpleNamespace(get_active_scene=lambda: scene)
    scene_manager = SimpleNamespace(instance=lambda: manager)
    monkeypatch.setattr(native_lib, "SceneManager", scene_manager)

    assert ObjectExecutionLayer.resolve_selected_object(42) is selected
