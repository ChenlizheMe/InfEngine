from pathlib import Path
from types import SimpleNamespace

from Infernux.engine import scene_manager as scene_manager_module
from Infernux.engine.scene_manager import SceneFileManager
from Infernux.engine import _scene_save as scene_save_module


class _AssetDatabase:
    def __init__(self, scene: Path, guid: str = "scene-guid") -> None:
        self.scene = str(scene)
        self.guid = guid

    def get_path_from_guid(self, guid: str) -> str:
        return self.scene if guid == self.guid else ""

    def get_guid_from_path(self, path: str) -> str:
        return self.guid if Path(path) == Path(self.scene) else ""


def _manager(database: _AssetDatabase) -> SceneFileManager:
    manager = SceneFileManager()
    manager._asset_database = database
    return manager


def test_last_scene_resolves_by_guid_and_removes_path_identity(tmp_path, monkeypatch):
    scene = tmp_path / "Assets" / "Scenes" / "Start.scene"
    scene.parent.mkdir(parents=True)
    scene.write_text("{}", encoding="utf-8")
    manager = _manager(_AssetDatabase(scene))
    opened: list[str] = []
    saved: list[dict] = []
    monkeypatch.setattr(
        scene_manager_module,
        "_load_editor_settings",
        lambda: {
            "lastOpenedSceneGuid": "scene-guid",
            "lastOpenedScene": "C:/old/project/Assets/Scenes/Start.scene",
        },
    )
    monkeypatch.setattr(
        scene_manager_module, "_save_editor_settings", lambda value: saved.append(value)
    )
    monkeypatch.setattr(scene_save_module, "_load_editor_settings", lambda: {
        "lastOpenedSceneGuid": "scene-guid",
        "lastOpenedScene": "C:/old/project/Assets/Scenes/Start.scene",
    })
    monkeypatch.setattr(
        scene_save_module, "_save_editor_settings", lambda value: saved.append(value)
    )
    monkeypatch.setattr(
        manager,
        "_do_open_scene",
        lambda path, record_navigation: opened.append(path) or True,
    )

    manager.load_last_scene_or_default()

    assert opened == [str(scene)]
    assert saved == [{"lastOpenedSceneGuid": "scene-guid"}]


def test_windows_last_scene_path_migrates_inside_linux_project(tmp_path, monkeypatch):
    scene = tmp_path / "Assets" / "Scenes" / "Start.scene"
    scene.parent.mkdir(parents=True)
    scene.write_text("{}", encoding="utf-8")
    manager = _manager(_AssetDatabase(scene))
    opened: list[str] = []
    saved: list[dict] = []
    monkeypatch.setattr(scene_manager_module, "_effective_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(
        scene_manager_module,
        "_load_editor_settings",
        lambda: {"lastOpenedScene": r"D:\Games\Ball\Assets\Scenes\Start.scene"},
    )
    monkeypatch.setattr(
        scene_manager_module, "_save_editor_settings", lambda value: saved.append(value)
    )
    monkeypatch.setattr(
        scene_save_module,
        "_load_editor_settings",
        lambda: {"lastOpenedScene": r"D:\Games\Ball\Assets\Scenes\Start.scene"},
    )
    monkeypatch.setattr(
        scene_save_module, "_save_editor_settings", lambda value: saved.append(value)
    )
    monkeypatch.setattr(
        manager,
        "_do_open_scene",
        lambda path, record_navigation: opened.append(path) or True,
    )

    manager.load_last_scene_or_default()

    assert opened == [str(scene)]
    assert saved == [{"lastOpenedSceneGuid": "scene-guid"}]


def test_scene_camera_state_is_keyed_by_asset_guid(tmp_path, monkeypatch):
    scene = tmp_path / "Assets" / "Scenes" / "Start.scene"
    scene.parent.mkdir(parents=True)
    scene.write_text("{}", encoding="utf-8")
    manager = _manager(_AssetDatabase(scene))
    manager._engine = SimpleNamespace(
        editor_camera=SimpleNamespace(
            position=SimpleNamespace(x=1.0, y=2.0, z=3.0),
            rotation=(4.0, 5.0),
            focus_point=SimpleNamespace(x=6.0, y=7.0, z=8.0),
            focus_distance=9.0,
        )
    )
    saved: list[dict] = []
    monkeypatch.setattr(
        scene_manager_module,
        "_load_editor_settings",
        lambda: {
            "sceneCameraStates": {
                r"c:\old\project\assets\scenes\start.scene": {"legacy": True},
                r"c:\old\project\assets\scenes\deleted.scene": {"stale": True},
            }
        },
    )
    monkeypatch.setattr(
        scene_manager_module, "_save_editor_settings", lambda value: saved.append(value)
    )

    manager._save_camera_state(str(scene))

    assert set(saved[0]["sceneCameraStates"]) == {"scene-guid"}
    assert str(scene) not in saved[0]["sceneCameraStates"]


def test_scene_camera_state_restore_migrates_windows_path(tmp_path, monkeypatch):
    scene = tmp_path / "Assets" / "Scenes" / "Start.scene"
    scene.parent.mkdir(parents=True)
    scene.write_text("{}", encoding="utf-8")
    manager = _manager(_AssetDatabase(scene))
    restored: list[tuple] = []
    manager._engine = SimpleNamespace(
        editor_camera=SimpleNamespace(
            restore_state=lambda *values: restored.append(values)
        )
    )
    legacy_state = {
        "position": [1.0, 2.0, 3.0],
        "focusPoint": [4.0, 5.0, 6.0],
        "focusDistance": 7.0,
        "yaw": 8.0,
        "pitch": 9.0,
    }
    settings = {
        "sceneCameraStates": {
            r"c:\old\project\assets\scenes\start.scene": legacy_state,
        }
    }
    saved: list[dict] = []
    monkeypatch.setattr(scene_manager_module, "_effective_project_root", lambda: str(tmp_path))
    monkeypatch.setattr(scene_manager_module, "_load_editor_settings", lambda: settings)
    monkeypatch.setattr(
        scene_manager_module, "_save_editor_settings", lambda value: saved.append(value)
    )

    manager._restore_camera_state(str(scene))

    assert restored == [(1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0)]
    assert set(settings["sceneCameraStates"]) == {"scene-guid"}
    assert saved == [settings]
