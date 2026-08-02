"""Tests for Infernux.components.ref_wrappers — GameObjectRef, ComponentRef."""

import copy
import json

from Infernux.components.component import InxComponent
from Infernux.components.ref_wrappers import (
    GameObjectRef,
    PrefabRef,
    ComponentRef,
    _resolve_component_on_game_object,
    _infer_component_type_on_game_object,
)
from Infernux.lib import GameObject


# ══════════════════════════════════════════════════════════════════════
# GameObjectRef
# ══════════════════════════════════════════════════════════════════════

class TestGameObjectRef:
    def test_empty_ref_is_falsy(self):
        ref = GameObjectRef()
        assert not ref
        assert ref.persistent_id == 0

    def test_from_persistent_id(self):
        ref = GameObjectRef(persistent_id=42)
        assert ref.persistent_id == 42

    def test_eq_none_when_empty(self):
        ref = GameObjectRef()
        assert ref == None  # noqa: E711 — intentional None comparison

    def test_eq_by_persistent_id(self):
        a = GameObjectRef(persistent_id=7)
        b = GameObjectRef(persistent_id=7)
        assert a == b

    def test_neq_by_persistent_id(self):
        a = GameObjectRef(persistent_id=7)
        b = GameObjectRef(persistent_id=8)
        assert a != b

    def test_hash_by_persistent_id(self):
        a = GameObjectRef(persistent_id=7)
        b = GameObjectRef(persistent_id=7)
        assert hash(a) == hash(b)

    def test_copy(self):
        ref = GameObjectRef(persistent_id=99)
        ref2 = copy.copy(ref)
        assert ref2.persistent_id == 99
        assert ref2 is not ref

    def test_deepcopy(self):
        ref = GameObjectRef(persistent_id=99)
        ref2 = copy.deepcopy(ref)
        assert ref2.persistent_id == 99
        assert ref2 is not ref

    def test_repr_none(self):
        ref = GameObjectRef()
        r = repr(ref)
        assert "None" in r
        assert "0" in r

    def test_getattr_returns_none_when_empty(self):
        ref = GameObjectRef()
        assert ref.name is None
        assert ref.transform is None


class TestGameObjectAlias:
    def test_game_object_property_returns_self_for_live_object(self):
        fake = type("FakeGameObject", (), {"id": 12})()

        assert GameObject.game_object.fget(fake) is fake

    def test_game_object_property_returns_none_for_invalid_object(self):
        fake = type("FakeGameObject", (), {"id": 0})()

        assert GameObject.game_object.fget(fake) is None


class _FakeAssetDatabase:
    def __init__(self, path: str = ""):
        self.path = path

    def get_path_from_guid(self, _guid: str) -> str:
        return self.path


class TestPrefabRef:
    def test_name_uses_prefab_root_object_name(self, monkeypatch, tmp_path):
        prefab_path = tmp_path / "enemy.prefab"
        prefab_path.write_text(
            json.dumps({"root_object": {"name": "EnemyRoot"}}),
            encoding="utf-8",
        )
        fake_db = _FakeAssetDatabase(str(prefab_path))
        monkeypatch.setattr(
            "Infernux.components.ref_wrappers._get_prefab_asset_database",
            lambda: fake_db,
        )

        ref = PrefabRef(guid="enemy-guid", path_hint="stale.prefab")

        assert ref.path_hint == str(prefab_path)
        assert ref.name == "EnemyRoot"

    def test_name_tracks_prefab_rename_and_root_name_change(self, monkeypatch, tmp_path):
        old_path = tmp_path / "enemy.prefab"
        old_path.write_text(
            json.dumps({"root_object": {"name": "EnemyRoot"}}),
            encoding="utf-8",
        )
        fake_db = _FakeAssetDatabase(str(old_path))
        monkeypatch.setattr(
            "Infernux.components.ref_wrappers._get_prefab_asset_database",
            lambda: fake_db,
        )

        ref = PrefabRef(guid="enemy-guid", path_hint=str(old_path))
        assert ref.name == "EnemyRoot"

        new_path = tmp_path / "boss.prefab"
        old_path.rename(new_path)
        new_path.write_text(
            json.dumps({"root_object": {"name": "BossRoot"}}),
            encoding="utf-8",
        )
        fake_db.path = str(new_path)

        assert ref.path_hint == str(new_path)
        assert ref.name == "BossRoot"
        assert str(new_path) in repr(ref)

    def test_game_object_alias_returns_self(self):
        ref = PrefabRef(guid="enemy-guid", path_hint="enemy.prefab")

        assert ref.game_object is ref


# ══════════════════════════════════════════════════════════════════════
# ComponentRef
# ══════════════════════════════════════════════════════════════════════

class TestComponentRef:
    def test_empty_ref_is_falsy(self):
        ref = ComponentRef()
        assert not ref
        assert ref.go_id == 0
        assert ref.component_type == ""

    def test_with_go_id_and_type(self):
        ref = ComponentRef(go_id=10, component_type="Light")
        assert ref.go_id == 10
        assert ref.component_type == "Light"

    def test_eq_by_go_id_and_type(self):
        a = ComponentRef(go_id=5, component_type="Camera")
        b = ComponentRef(go_id=5, component_type="Camera")
        assert a == b

    def test_neq_different_type(self):
        a = ComponentRef(go_id=5, component_type="Camera")
        b = ComponentRef(go_id=5, component_type="Light")
        assert a != b

    def test_hash(self):
        a = ComponentRef(go_id=5, component_type="Camera")
        b = ComponentRef(go_id=5, component_type="Camera")
        assert hash(a) == hash(b)

    def test_copy(self):
        ref = ComponentRef(go_id=3, component_type="X")
        ref2 = copy.copy(ref)
        assert ref2.go_id == 3
        assert ref2.component_type == "X"

    def test_serialize_round_trip(self):
        ref = ComponentRef(go_id=42, component_type="Rigidbody")
        data = ref._serialize()
        assert data["$type"] == "component_ref"

        restored = ComponentRef._from_dict(data)
        assert restored.go_id == 42
        assert restored.component_type == "Rigidbody"

    def test_display_name_unresolved(self):
        ref = ComponentRef()
        assert ref.display_name == "None"

    def test_repr_unresolved(self):
        ref = ComponentRef(go_id=1, component_type="Test")
        r = repr(ref)
        assert "None" in r
        assert "Test" in r

    def test_getattr_returns_none_when_unresolved(self):
        ref = ComponentRef(go_id=1, component_type="Missing")
        assert ref.some_method is None


class _FakeGameObject:
    def __init__(self, go_id: int):
        self.id = go_id

    def get_cpp_component(self, _type_name: str):
        return None

    def get_py_component(self, _component_cls):
        return None

    def get_components(self):
        return []


class _FakePythonComponent:
    def __init__(self, game_object, type_name: str):
        self.game_object = game_object
        self.type_name = type_name
        self._is_destroyed = False


class TestInternalComponentResolution:
    def test_resolve_component_uses_active_instances(self):
        go = _FakeGameObject(101)
        comp = _FakePythonComponent(go, "_FakePythonComponent")
        old_instances = InxComponent._active_instances
        InxComponent._active_instances = {101: [comp]}
        try:
            assert _resolve_component_on_game_object(go, "_FakePythonComponent") is comp
        finally:
            InxComponent._active_instances = old_instances

    def test_infer_component_type_uses_first_live_component(self):
        go = _FakeGameObject(202)
        comp = _FakePythonComponent(go, "ExampleComponent")
        old_instances = InxComponent._active_instances
        InxComponent._active_instances = {202: [comp]}
        try:
            assert _infer_component_type_on_game_object(go) == "ExampleComponent"
        finally:
            InxComponent._active_instances = old_instances
