import importlib.util
from pathlib import Path
import sys
import types

import pytest


class _Vector3:
    def __init__(self, x: float, y: float, z: float):
        self.x = x
        self.y = y
        self.z = z


class _FakeTransform:
    def __init__(self):
        self.position = None
        self.euler_angles = None


class _FakeLight:
    def __init__(self):
        self.light_type = None
        self.color = None
        self.intensity = None
        self.shadows = None
        self.shadow_bias = 0.0


class _FakeGameObject:
    def __init__(self, name: str):
        self.name = name
        self.tag = ""
        self.transform = _FakeTransform()
        self.components = {}

    def add_component(self, type_name: str):
        if type_name == "Light":
            component = _FakeLight()
        else:
            component = types.SimpleNamespace(type_name=type_name)
        self.components[type_name] = component
        return component


class _FakeScene:
    def __init__(self):
        self.created = []

    def create_game_object(self, name: str):
        game_object = _FakeGameObject(name)
        self.created.append(game_object)
        return game_object


def _load_scene_manager_module(monkeypatch):
    fake_infernux = types.ModuleType("Infernux")
    fake_infernux.__path__ = []

    fake_engine = types.ModuleType("Infernux.engine")
    fake_engine.__path__ = []

    fake_debug = types.ModuleType("Infernux.debug")
    fake_debug.Debug = type(
        "Debug",
        (),
        {
            "log": staticmethod(lambda *args, **kwargs: None),
            "log_warning": staticmethod(lambda *args, **kwargs: None),
            "log_error": staticmethod(lambda *args, **kwargs: None),
        },
    )

    fake_project_context = types.ModuleType("Infernux.engine.project_context")
    fake_project_context.get_project_root = lambda: None

    fake_path_utils = types.ModuleType("Infernux.engine.path_utils")
    fake_path_utils.safe_path = lambda path: path
    fake_path_utils.path_key = lambda path: str(path).lower()
    fake_path_utils.resolved_path = lambda path: str(path)

    monkeypatch.setitem(sys.modules, "Infernux", fake_infernux)
    monkeypatch.setitem(sys.modules, "Infernux.engine", fake_engine)
    monkeypatch.setitem(sys.modules, "Infernux.debug", fake_debug)
    monkeypatch.setitem(sys.modules, "Infernux.engine.project_context", fake_project_context)
    monkeypatch.setitem(sys.modules, "Infernux.engine.path_utils", fake_path_utils)

    # Fake mixin modules so relative imports in scene_manager.py succeed
    for mod_name, cls_name in [
        ("Infernux.engine._scene_prefab", "ScenePrefabMixin"),
        ("Infernux.engine._scene_save", "SceneSaveMixin"),
        ("Infernux.engine._scene_confirmation", "SceneConfirmationMixin"),
    ]:
        fake_mod = types.ModuleType(mod_name)
        setattr(fake_mod, cls_name, type(cls_name, (), {}))
        monkeypatch.setitem(sys.modules, mod_name, fake_mod)

    module_path = Path(__file__).resolve().parents[1] / "Infernux" / "engine" / "scene_manager.py"
    spec = importlib.util.spec_from_file_location(
        "Infernux.engine.scene_manager", module_path,
    )
    module = importlib.util.module_from_spec(spec)
    # Pre-register so relative imports can resolve the parent package
    monkeypatch.setitem(sys.modules, "Infernux.engine.scene_manager", module)
    assert spec is not None and spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_populate_default_objects_sets_light_shadow_bias(monkeypatch):
    scene_manager = _load_scene_manager_module(monkeypatch)
    fake_lib = types.ModuleType("Infernux.lib")
    fake_math = types.ModuleType("Infernux.math")

    class _LightType:
        Directional = "Directional"

    class _LightShadows:
        Soft = "Soft"

    fake_lib.LightType = _LightType
    fake_lib.LightShadows = _LightShadows
    fake_math.Vector3 = _Vector3

    monkeypatch.setitem(sys.modules, "Infernux.lib", fake_lib)
    monkeypatch.setitem(sys.modules, "Infernux.math", fake_math)

    scene = _FakeScene()

    scene_manager.SceneFileManager._populate_default_objects(scene)

    assert [game_object.name for game_object in scene.created] == ["Main Camera", "Directional Light"]
    light = scene.created[1].components["Light"]
    assert light.light_type == _LightType.Directional
    assert light.shadows == _LightShadows.Soft
    assert light.shadow_bias == pytest.approx(0.0)
