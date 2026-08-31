from __future__ import annotations

import importlib

import Infernux
import infernux as inx


def test_lowercase_namespace_exposes_gameplay_api() -> None:
    assert inx.__version__
    assert inx.InxComponent.__module__.startswith("Infernux.")
    assert inx.GameObject.__module__.startswith("Infernux.")
    assert inx.Vector3.__module__.startswith("Infernux.")
    assert inx.InxComponent is Infernux.InxComponent
    assert inx.GameObject is Infernux.GameObject


def test_lowercase_namespace_lazily_forwards_subsystems() -> None:
    assert "input" in inx.__all__
    assert "ui" in inx.__all__
    assert inx.input.__name__ == "Infernux.input"
    assert inx.renderstack.__name__ == "Infernux.renderstack"


def test_lowercase_namespace_reload_preserves_runtime_type_identity() -> None:
    component_type = inx.InxComponent
    reloaded = importlib.reload(inx)
    assert reloaded.InxComponent is component_type
    assert reloaded.InxComponent is Infernux.InxComponent
