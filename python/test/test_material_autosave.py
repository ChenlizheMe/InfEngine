from __future__ import annotations

import pytest

import Infernux.lib as native_lib
from Infernux.core.material import Material


class _NativeMaterial:
    def __init__(self, *, deleted: bool = False, save_result: bool = True):
        self.guid = "test-material"
        self.file_path = "Assets/Test.mat"
        self.name = "Test"
        self.deleted = deleted
        self.save_result = save_result
        self.save_count = 0

    def is_deleted(self) -> bool:
        return self.deleted

    def save(self) -> bool:
        self.save_count += 1
        return self.save_result

    def serialize_document(self) -> dict:
        return {
            "name": self.name,
            "shaders": {"vertex": {}, "fragment": {}},
            "renderState": {"renderQueue": 2000},
            "properties": {},
        }


class _AssetRegistry:
    current = None

    @classmethod
    def instance(cls):
        return cls.current


class _MaterialRegistry:
    def __init__(self, native=None, error: Exception | None = None):
        self.native = native
        self.error = error

    def load_material(self, _path: str):
        if self.error is not None:
            raise self.error
        return self.native

    def get_builtin_material(self, _name: str):
        if self.error is not None:
            raise self.error
        return self.native


def test_material_load_uses_the_asset_registry_as_its_only_loader(monkeypatch):
    native = _NativeMaterial()
    _AssetRegistry.current = _MaterialRegistry(native)
    monkeypatch.setattr(native_lib, "AssetRegistry", _AssetRegistry)

    loaded = Material.load("Assets/Test.mat")

    assert loaded is not None
    assert loaded.native is native


@pytest.mark.parametrize("method, argument", [("load", "bad.mat"), ("get", "Bad")])
def test_material_registry_failures_are_not_suppressed(
    monkeypatch, method: str, argument: str
):
    _AssetRegistry.current = _MaterialRegistry(error=RuntimeError("registry failed"))
    monkeypatch.setattr(native_lib, "AssetRegistry", _AssetRegistry)

    with pytest.raises(RuntimeError, match="registry failed"):
        getattr(Material, method)(argument)


def test_material_to_dict_preserves_the_canonical_native_document():
    material = Material(_NativeMaterial())

    document = material.to_dict()

    assert document == material.serialize_document()
    assert "shaders" in document
    assert "renderState" in document
    assert "properties" in document


def test_flush_drops_deleted_material_without_saving():
    native = _NativeMaterial(deleted=True)
    material = Material(native)
    material._save_pending = True
    Material._pending_saves[id(material)] = material

    material._flush_save()

    assert native.save_count == 0
    assert material._save_pending is False
    assert id(material) not in Material._pending_saves


def test_dispose_removes_material_from_pending_save_queue():
    native = _NativeMaterial()
    material = Material(native)
    material._save_pending = True
    Material._pending_saves[id(material)] = material

    material.dispose()

    assert material._save_pending is False
    assert id(material) not in Material._pending_saves


def test_pending_save_queue_keeps_distinct_wrappers_for_same_guid():
    first = Material(_NativeMaterial())
    second = Material(_NativeMaterial())

    first._auto_save()
    second._auto_save()

    assert Material._pending_saves == {}
    first._last_save_time = second._last_save_time = float("inf")
    first._auto_save()
    second._auto_save()

    assert set(Material._pending_saves) == {id(first), id(second)}
    Material._pending_saves.clear()


def test_failed_material_save_remains_pending_for_retry():
    native = _NativeMaterial(save_result=False)
    material = Material(native)

    material._flush_save()

    assert native.save_count == 1
    assert material._save_pending is True
    assert Material._pending_saves[id(material)] is material
    Material._pending_saves.clear()
