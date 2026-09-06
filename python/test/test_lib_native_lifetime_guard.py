from __future__ import annotations

import pytest

import Infernux.lib as lib_module

from Infernux.lib import (
    GameObject,
    InvalidNativeObjectError,
    Vector3,
    _install_native_lifetime_guard,
    _is_native_lifetime_error,
)


class _FakeDeadGameObject:
    @property
    def id(self):
        raise RuntimeError("Access violation - no RTTI data!")

    @property
    def transform(self):
        raise RuntimeError("Access violation - no RTTI data!")

    def get_transform(self):
        raise RuntimeError("Access violation - no RTTI data!")

    def get_children(self):
        raise RuntimeError("Access violation - no RTTI data!")

    def set_parent(self, parent):
        raise RuntimeError("Access violation - no RTTI data!")


class _FakeDeadComponent:
    @property
    def component_id(self):
        raise RuntimeError("Access violation - no RTTI data!")

    @property
    def enabled(self):
        raise RuntimeError("Access violation - no RTTI data!")

    @enabled.setter
    def enabled(self, value):
        raise RuntimeError("Access violation - no RTTI data!")

    def serialize(self):
        raise RuntimeError("Access violation - no RTTI data!")


class _FakeDeadTransform(_FakeDeadComponent):
    @property
    def position(self):
        raise RuntimeError("Access violation - no RTTI data!")

    @position.setter
    def position(self, value):
        raise RuntimeError("Access violation - no RTTI data!")

    def local_to_world_matrix(self):
        raise RuntimeError("Access violation - no RTTI data!")


class _FakeQuat:
    def __init__(self, x, y, z, w):
        self.x = x
        self.y = y
        self.z = z
        self.w = w


class _FakeLiveTransform:
    def __init__(self):
        self.position = None
        self.rotation = None
        self.local_position = Vector3(1.0, 2.0, 3.0)
        self.local_rotation = _FakeQuat(0.0, 0.0, 0.0, 1.0)
        self.local_scale = Vector3(1.0, 1.0, 1.0)


class _FakeClone:
    def __init__(self):
        self.transform = _FakeLiveTransform()
        self.parent_calls = []

    def set_parent(self, parent, world_position_stays=True):
        self.parent_calls.append((parent, world_position_stays))


for _cls in (_FakeDeadGameObject, _FakeDeadComponent, _FakeDeadTransform):
    _install_native_lifetime_guard(_cls)


class TestNativeLifetimeErrorClassifier:
    def test_detects_access_violation(self):
        assert _is_native_lifetime_error(RuntimeError("Access violation - no RTTI data!")) is True

    def test_ignores_other_runtime_errors(self):
        assert _is_native_lifetime_error(RuntimeError("some other runtime problem")) is False


class TestGuardedGameObject:
    def test_invalid_id_raises(self):
        with pytest.raises(InvalidNativeObjectError):
            _FakeDeadGameObject().id

    def test_invalid_transform_raises(self):
        go = _FakeDeadGameObject()
        with pytest.raises(InvalidNativeObjectError):
            go.transform
        with pytest.raises(InvalidNativeObjectError):
            go.get_transform()

    def test_invalid_children_raises(self):
        with pytest.raises(InvalidNativeObjectError):
            _FakeDeadGameObject().get_children()

    def test_invalid_game_object_is_falsey(self):
        assert bool(_FakeDeadGameObject()) is False


class TestGuardedComponent:
    def test_invalid_component_id_raises(self):
        with pytest.raises(InvalidNativeObjectError):
            _FakeDeadComponent().component_id

    def test_invalid_enabled_raises(self):
        with pytest.raises(InvalidNativeObjectError):
            _FakeDeadComponent().enabled

    def test_invalid_serialize_raises(self):
        with pytest.raises(InvalidNativeObjectError):
            _FakeDeadComponent().serialize()

    def test_invalid_setattr_raises(self):
        comp = _FakeDeadComponent()
        with pytest.raises(InvalidNativeObjectError):
            comp.enabled = True


class TestGuardedTransform:
    def test_invalid_position_raises(self):
        with pytest.raises(InvalidNativeObjectError):
            _FakeDeadTransform().position

    def test_invalid_matrix_raises(self):
        with pytest.raises(InvalidNativeObjectError):
            _FakeDeadTransform().local_to_world_matrix()

    def test_invalid_transform_is_falsey(self):
        assert bool(_FakeDeadTransform()) is False


class TestInstantiateOverloads:
    def test_instantiate_source_resolution_errors_are_not_suppressed(self):
        class BrokenReference:
            def resolve(self):
                raise RuntimeError("reference resolution failed")

        with pytest.raises(RuntimeError, match="reference resolution failed"):
            lib_module._resolve_game_object_instantiate_source(BrokenReference())

    def test_game_object_instantiate_accepts_prefab_ref_source(self, monkeypatch):
        clone = _FakeClone()
        prefab_ref = object()

        monkeypatch.setattr(lib_module, "_resolve_game_object_instantiate_source", lambda original: ("prefab", original))
        def instantiate_prefab(original, parent, world_space, configure_created):
            assert original is prefab_ref
            assert parent is None
            assert world_space is True
            configure_created(clone)
            return clone

        monkeypatch.setattr(lib_module, "_instantiate_prefab_reference", instantiate_prefab)

        assert GameObject.instantiate(prefab_ref) is clone

    def test_game_object_instantiate_applies_position_rotation_and_parent(self, monkeypatch):
        clone = _FakeClone()
        source = object()
        parent = object()
        position = Vector3(9.0, 8.0, 7.0)
        rotation = _FakeQuat(0.0, 0.0, 0.0, 1.0)

        monkeypatch.setattr(lib_module, "_resolve_game_object_instantiate_source", lambda original: ("game_object", original))
        monkeypatch.setattr(lib_module, "_coerce_parent_game_object", lambda original: original)
        calls = []

        def instantiate_native(original, target_parent, world_space, configure_created):
            calls.append((original, target_parent, world_space))
            configure_created(clone)
            return clone

        monkeypatch.setattr(lib_module, "_native_game_object_instantiate", instantiate_native)

        result = GameObject.instantiate(source, position, rotation, parent)

        assert result is clone
        assert calls == [(source, parent, True)]
        assert clone.parent_calls == []
        assert clone.transform.position is position
        assert clone.transform.rotation is rotation
