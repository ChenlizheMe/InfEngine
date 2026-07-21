from __future__ import annotations

import importlib

from Infernux.core.assets import AssetManager
from Infernux.lib import AssetMutationErrorCode, AssetMutationResult


def _result(operation, path, *, succeeded=True, guid="guid", previous_path=""):
    result = AssetMutationResult()
    result.operation = operation
    result.path = path
    result.previous_path = previous_path
    result.guid = guid
    result.succeeded = succeeded
    result.database_committed = succeeded
    result.changed = succeeded
    if not succeeded:
        result.error_code = AssetMutationErrorCode.NOT_FOUND
        result.error = "not found"
    return result


class _Database:
    def __init__(self, order):
        self.order = order
        self.paths = {"old.txt": "guid"}

    def get_guid_from_path(self, path):
        return self.paths.get(path, "")

    def import_asset(self, path):
        self.order.append("db-import")
        self.paths[path] = "new-guid"
        return _result("import", path, guid="new-guid")

    def reimport_asset(self, path):
        self.order.append("db-reimport")
        return _result("reimport", path, succeeded=path in self.paths)

    def get_meta_by_guid(self, guid):
        return _Metadata() if guid == "guid" else None

    def delete_asset(self, path):
        self.order.append("db-delete")
        self.paths.pop(path, None)
        return _result("delete", path)

    def move_asset(self, old_path, new_path):
        self.order.append("db-move")
        guid = self.paths.pop(old_path, "")
        if not guid:
            return _result("move", new_path, succeeded=False, previous_path=old_path)
        self.paths[new_path] = guid
        return _result("move", new_path, guid=guid, previous_path=old_path)


class _Registry:
    def __init__(self, order):
        self.order = order

    def is_loaded(self, guid):
        return guid == "guid"

    def reload_asset(self, guid):
        self.order.append("registry-reload")
        return True

    def remove_asset(self, guid):
        self.order.append("registry-remove")

    def update_loaded_asset_path(self, old_path, new_path):
        self.order.append("registry-move")


class _Metadata:
    def has_key(self, key):
        return key == "shader_id"

    def get_string(self, key):
        assert key == "shader_id"
        return "previous-shader"


class _NativeEngine:
    has_renderer = True

    def __init__(self, order):
        self.order = order

    def reload_shader_runtime(self, path, previous_shader_id):
        assert path == "old.vert"
        assert previous_shader_id == "previous-shader"
        self.order.append("shader-runtime")
        return ""


class _FailingNativeEngine(_NativeEngine):
    def reload_shader_runtime(self, path, previous_shader_id):
        super().reload_shader_runtime(path, previous_shader_id)
        return "shader compile failed"


def _isolate_side_effects(monkeypatch, order):
    AssetManager._watcher_echo_suppression.clear()
    registry = _Registry(order)
    monkeypatch.setattr(AssetManager, "_get_registry", classmethod(lambda _cls: registry))
    monkeypatch.setattr(AssetManager, "invalidate", classmethod(lambda _cls, _guid: order.append("py-evict")))
    monkeypatch.setattr(
        AssetManager,
        "_emit_editor_asset_changed",
        classmethod(lambda _cls, _path, event="modified": order.append(f"editor-{event}")),
    )
    monkeypatch.setattr(AssetManager, "_invalidate_project_panel_cache", classmethod(lambda _cls: None))


def test_reimport_rebuilds_database_before_registry_reload(monkeypatch):
    order = []
    database = _Database(order)
    _isolate_side_effects(monkeypatch, order)

    result = AssetManager.reimport_asset("old.txt", database=database)
    assert result and result.guid == "guid"
    assert order == ["db-reimport", "registry-reload", "py-evict", "editor-modified"]


def test_delete_evicts_registry_before_database_event(monkeypatch):
    order = []
    database = _Database(order)
    _isolate_side_effects(monkeypatch, order)

    result = AssetManager.delete_asset("old.txt", database=database)
    assert result and result.database_committed
    assert order == ["registry-remove", "py-evict", "db-delete", "editor-deleted"]


def test_move_commits_mapping_before_patching_loaded_path(monkeypatch):
    order = []
    database = _Database(order)
    _isolate_side_effects(monkeypatch, order)

    result = AssetManager.move_asset("old.txt", "new.txt", database=database)
    assert result and result.previous_path == "old.txt"
    assert order == ["db-move", "registry-move", "py-evict", "editor-moved"]


def test_programmatic_script_move_explicitly_hot_reloads_after_guid_move(monkeypatch):
    order = []
    database = _Database(order)
    database.paths = {"old.py": "guid"}
    _isolate_side_effects(monkeypatch, order)

    class _Resources:
        @staticmethod
        def reload_moved_script(old_path, new_path):
            order.append(("script-reload", old_path, new_path))

    from Infernux.engine.resources_manager import ResourcesManager

    monkeypatch.setattr(
        ResourcesManager,
        "instance",
        classmethod(lambda _cls: _Resources()),
    )

    result = AssetManager.move_asset("old.py", "new.py", database=database)

    assert result and result.guid == "guid"
    assert order == [
        "db-move",
        "registry-move",
        "py-evict",
        "editor-moved",
        ("script-reload", "old.py", "new.py"),
    ]


def test_shader_reimport_mutates_database_once_before_runtime_compile(monkeypatch):
    order = []
    database = _Database(order)
    database.paths = {"old.vert": "guid"}
    _isolate_side_effects(monkeypatch, order)
    native = _NativeEngine(order)
    monkeypatch.setattr(AssetManager, "_native_engine", classmethod(lambda _cls: native))
    shader_utils = importlib.import_module("Infernux.engine.ui.inspector_shader_utils")
    monkeypatch.setattr(
        shader_utils,
        "bump_shader_property_generation",
        lambda: order.append("shader-authoring-cache"),
    )

    result = AssetManager.reimport_asset("old.vert", database=database)
    assert result and result.guid == "guid"
    assert order == [
        "db-reimport",
        "shader-runtime",
        "shader-authoring-cache",
        "py-evict",
        "editor-modified",
    ]


def test_shader_import_invalidates_authoring_cache_without_runtime(monkeypatch):
    order = []
    database = _Database(order)
    _isolate_side_effects(monkeypatch, order)
    monkeypatch.setattr(AssetManager, "_prime_material_preview", classmethod(lambda _cls, _path: None))
    shader_utils = importlib.import_module("Infernux.engine.ui.inspector_shader_utils")
    monkeypatch.setattr(
        shader_utils,
        "bump_shader_property_generation",
        lambda: order.append("shader-authoring-cache"),
    )

    result = AssetManager.import_asset("new.frag", database=database)

    assert result and result.guid == "new-guid"
    assert order == ["db-import", "shader-authoring-cache", "editor-created"]


def test_shader_runtime_failure_reports_committed_database_state(monkeypatch):
    order = []
    database = _Database(order)
    database.paths = {"old.vert": "guid"}
    _isolate_side_effects(monkeypatch, order)
    monkeypatch.setattr(
        AssetManager,
        "_native_engine",
        classmethod(lambda _cls: _FailingNativeEngine(order)),
    )

    result = AssetManager.reimport_asset("old.vert", database=database)

    assert not result
    assert result.database_committed is True
    assert result.error_code == AssetMutationErrorCode.RUNTIME_APPLY_FAILED
    assert result.error == "shader compile failed"
    assert order == ["db-reimport", "shader-runtime"]
