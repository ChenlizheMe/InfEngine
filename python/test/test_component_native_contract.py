from __future__ import annotations

import pytest

import Infernux.components._component_native as native_module
from Infernux.components import InxComponent
from Infernux.components._component_native import ComponentNativeMixin


def test_native_component_liveness_only_treats_runtime_failure_as_destroyed():
    class DestroyedComponent:
        @property
        def component_id(self):
            raise RuntimeError("destroyed")

    class InvalidComponent:
        component_id = "not-an-id"

    assert ComponentNativeMixin._is_native_component_alive(DestroyedComponent()) is False
    with pytest.raises(ValueError, match="invalid literal"):
        ComponentNativeMixin._is_native_component_alive(InvalidComponent())


def test_native_game_object_liveness_exposes_invalid_identity(monkeypatch):
    class GameObject:
        id = "not-an-id"

    monkeypatch.setattr(native_module, "GameObject", GameObject)

    with pytest.raises(ValueError, match="invalid literal"):
        ComponentNativeMixin._is_native_game_object_alive(GameObject())


@pytest.mark.parametrize("missing", ["component_id", "execution_order", "enabled"])
def test_native_binding_requires_complete_lifecycle_state(missing):
    values = {
        "component_id": 7,
        "execution_order": 0,
        "enabled": True,
    }
    del values[missing]
    native = type("NativeComponent", (), values)()
    component = InxComponent()

    with pytest.raises(AttributeError, match=missing):
        component._bind_native_component(native)
