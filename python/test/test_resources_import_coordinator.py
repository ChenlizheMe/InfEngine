from __future__ import annotations

import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from Infernux.core.assets import AssetManager
from Infernux.engine.engine import Engine
from Infernux.engine.resources_manager import (
    ResourceChangeHandler,
    ResourcesManager,
    _AssetImportNotReady,
)
from Infernux.lib import AssetMutationResult, RuntimeMode


def _mutation(operation, path, guid=""):
    result = AssetMutationResult()
    result.succeeded = True
    result.database_committed = True
    result.changed = True
    result.operation = operation
    result.path = path
    result.guid = guid
    return result


class _AssetDatabaseProbe:
    def __init__(self):
        self.guid_by_path = {}
        self.queries = []
        self.mutations = []

    def get_guid_from_path(self, path):
        self.queries.append((path, threading.get_ident()))
        return self.guid_by_path.get(path, "")

    def import_asset(self, path):
        self.mutations.append(("import", path, threading.get_ident()))
        self.guid_by_path[path] = "created-guid"
        return _mutation("import", path, "created-guid")

    def contains_path(self, path):
        return path in self.guid_by_path

    def reimport_asset(self, path):
        self.mutations.append(("modified", path, threading.get_ident()))
        return _mutation("reimport", path, self.guid_by_path.get(path, ""))

    def delete_asset(self, path):
        self.mutations.append(("deleted", path, threading.get_ident()))
        self.guid_by_path.pop(path, None)
        return _mutation("delete", path)

    def move_asset(self, old_path, new_path):
        self.mutations.append(("moved", old_path, new_path, threading.get_ident()))
        guid = self.guid_by_path.pop(old_path, "")
        if guid:
            self.guid_by_path[new_path] = guid
        result = _mutation("move", new_path, guid)
        result.previous_path = old_path
        return result


class _EngineProbe:
    def __init__(self, asset_database):
        self.asset_database = asset_database

    def get_asset_database(self):
        return self.asset_database


def _event(path, *, destination=""):
    values = {"is_directory": False, "src_path": str(path)}
    if destination:
        values["dest_path"] = str(destination)
    return SimpleNamespace(**values)


def _patch_asset_manager(monkeypatch, calls):
    AssetManager._watcher_echo_suppression.clear()
    AssetManager._meta_write_suppression.clear()
    monkeypatch.setattr(
        AssetManager,
        "_get_registry",
        classmethod(lambda _cls: None),
    )
    monkeypatch.setattr(
        AssetManager,
        "invalidate",
        classmethod(lambda _cls, guid: calls.append(("invalidate", guid, threading.get_ident()))),
    )
    monkeypatch.setattr(
        AssetManager,
        "_emit_editor_asset_changed",
        classmethod(
            lambda _cls, path, event_type="modified": calls.append(
                (f"asset-{event_type}", path, threading.get_ident())
            )
        ),
    )
    monkeypatch.setattr(AssetManager, "_invalidate_project_panel_cache", classmethod(lambda _cls: None))


def test_python_bytecode_and_cache_sidecars_never_enter_import_queue(tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    cache = tmp_path / "Assets" / "Scripts" / "__pycache__"
    cache.mkdir(parents=True)
    bytecode = cache / "Controller.cpython-312.pyc"
    moved_bytecode = cache / "Controller.cpython-312.optimized.pyc"
    bytecode.write_bytes(b"cache")

    handler.on_created(_event(bytecode))
    handler.on_modified(_event(bytecode))
    handler.on_moved(_event(bytecode, destination=moved_bytecode))
    handler.on_deleted(_event(bytecode))
    handler.on_deleted(_event(Path(f"{bytecode}.meta")))

    assert handler.pending_count == 0
    assert handler.process_pending_reloads(force=True) == 0
    assert database.queries == []
    assert database.mutations == []


def test_reimport_meta_write_suppresses_meta_deleted_echo(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)

    owner = tmp_path / "newmaterial.mat"
    meta = tmp_path / "newmaterial.mat.meta"
    owner.write_text("content", encoding="utf-8")
    meta.write_text("meta", encoding="utf-8")
    owner_path = str(owner.resolve())
    database.guid_by_path[owner_path] = "stable-guid"

    assert AssetManager.reimport_asset(owner_path, database=database)
    assert [entry[0] for entry in database.mutations] == ["modified"]

    meta.unlink()
    handler.on_deleted(_event(meta))
    assert handler.process_pending_reloads(force=True) == 1
    # META_DELETED must be treated as DocumentStore echo, not a second reimport.
    assert [entry[0] for entry in database.mutations] == ["modified"]


def test_asset_manager_delete_clears_live_python_references_after_database_commit(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    calls = []
    _patch_asset_manager(monkeypatch, calls)
    asset = tmp_path / "RaceDust.particlegraph"
    asset.write_text("{}", encoding="utf-8")
    path = str(asset.resolve())
    database.guid_by_path[path] = "race-dust-guid"
    cleanup_calls = []
    monkeypatch.setattr(
        AssetManager,
        "_clear_deleted_live_references",
        classmethod(lambda _cls, guid, deleted_path: cleanup_calls.append((guid, deleted_path))),
    )

    assert AssetManager.delete_asset(path, database=database)

    assert [entry[0] for entry in database.mutations] == ["deleted"]
    assert cleanup_calls == [("race-dust-guid", path)]


def test_missing_meta_rebuild_imports_unregistered_owner(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)

    owner = tmp_path / "orphan.mat"
    meta = tmp_path / "orphan.mat.meta"
    owner.write_text("content", encoding="utf-8")
    handler.on_deleted(_event(meta))

    assert handler.process_pending_reloads(force=True) == 1
    assert [entry[0] for entry in database.mutations] == ["import"]
    assert database.guid_by_path[str(owner.resolve())] == "created-guid"


def test_material_save_suppresses_watcher_modified_echo(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)

    path = tmp_path / "live.mat"
    path.write_text("content", encoding="utf-8")
    path_str = str(path.resolve())
    database.guid_by_path[path_str] = "stable-guid"

    AssetManager.on_material_saved(path_str)
    handler.on_modified(_event(path))
    assert handler.process_pending_reloads(force=True) == 1
    assert database.mutations == []


def test_watcher_thread_only_submits_and_main_thread_commits(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)

    path = tmp_path / "asset.txt"
    path.write_text("content", encoding="utf-8")
    database.guid_by_path[str(path)] = "stable-guid"
    callback_thread_ids = []

    def submit_many():
        callback_thread_ids.append(threading.get_ident())
        for _ in range(50):
            handler.on_modified(_event(path))

    worker = threading.Thread(target=submit_many)
    worker.start()
    worker.join()

    assert database.mutations == []
    assert asset_calls == []
    assert callback_thread_ids[0] != threading.get_ident()

    assert handler.process_pending_reloads(force=True) == 1
    assert [entry[0] for entry in asset_calls] == ["invalidate", "asset-modified"]
    assert [entry[0] for entry in database.mutations] == ["modified"]
    assert asset_calls[0][-1] == threading.get_ident()
    assert database.mutations[0][-1] == threading.get_ident()


def test_modified_asset_failure_surfaces_runtime_compile_detail(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    path = tmp_path / "Tone.effect"
    path.write_text("{}", encoding="utf-8")
    resolved = str(path.resolve())
    database.guid_by_path[resolved] = "tone-guid"
    failure = AssetMutationResult()
    failure.succeeded = False
    failure.error = "tone mapping enum compile failed"
    monkeypatch.setattr(
        AssetManager,
        "reimport_asset",
        classmethod(lambda _cls, *_args, **_kwargs: failure),
    )

    with pytest.raises(_AssetImportNotReady, match="tone mapping enum compile failed"):
        handler._commit_modified(resolved)


def test_move_query_may_run_on_watcher_but_mutation_waits_for_owner(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)

    old_path = tmp_path / "old.txt"
    new_path = tmp_path / "new.txt"
    new_path.write_text("moved", encoding="utf-8")
    database.guid_by_path[str(old_path)] = "stable-guid"

    worker = threading.Thread(
        target=lambda: handler.on_moved(_event(old_path, destination=new_path))
    )
    worker.start()
    worker.join()

    assert database.queries[0][-1] == worker.ident
    assert database.mutations == []
    assert handler.process_pending_reloads(force=True) == 1
    assert database.mutations[0][0] == "moved"
    assert database.mutations[0][-1] == threading.get_ident()
    assert [entry[0] for entry in asset_calls] == ["invalidate", "asset-moved"]
    assert all(entry[-1] == threading.get_ident() for entry in asset_calls)


def test_document_store_atomic_replace_ignores_temp_events_and_reimports_target(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)

    target = tmp_path / "atomic.mat"
    temporary = tmp_path / "atomic.mat.tmp.123456.7"
    temporary.write_text("staged", encoding="utf-8")
    database.guid_by_path[str(target)] = "stable-guid"

    handler.on_created(_event(temporary))
    handler.on_modified(_event(temporary))
    target.write_text("published", encoding="utf-8")
    temporary.unlink()
    handler.on_moved(_event(temporary, destination=target))
    handler.on_deleted(_event(temporary))

    assert handler.pending_count == 1
    assert handler.process_pending_reloads(force=True) == 1
    assert [entry[0] for entry in database.mutations] == ["modified"]
    assert database.mutations[0][1] == str(target.resolve())
    assert [entry[0] for entry in asset_calls] == ["invalidate", "asset-modified"]
    assert all(str(temporary) not in str(entry) for entry in database.mutations)


def test_first_scene_save_as_imports_active_unregistered_target(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)
    monkeypatch.setattr(handler, "_is_active_scene_file", lambda _path: True)

    target = tmp_path / "MainMenu.scene"
    temporary = tmp_path / "MainMenu.scene.tmp.123456.9"
    target.write_text("{}", encoding="utf-8")

    handler.on_moved(_event(temporary, destination=target))

    assert handler.process_pending_reloads(force=True) == 1
    assert database.mutations == [("import", str(target.resolve()), threading.get_ident())]
    assert [entry[0] for entry in asset_calls] == ["asset-created"]


def test_active_registered_scene_watcher_echo_is_ignored(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)
    monkeypatch.setattr(handler, "_is_active_scene_file", lambda _path: True)

    target = tmp_path / "RaceTrack.scene"
    target.write_text("{}", encoding="utf-8")
    database.guid_by_path[str(target.resolve())] = "stable-guid"

    handler.on_modified(_event(target))

    assert handler.process_pending_reloads(force=True) == 1
    assert database.mutations == []
    assert asset_calls == []


def test_document_store_atomic_replace_does_not_delete_republished_target(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)

    target = tmp_path / "atomic.mat"
    temporary = tmp_path / "atomic.mat.tmp.123456.8"
    target.write_text("old", encoding="utf-8")
    temporary.write_text("new", encoding="utf-8")
    database.guid_by_path[str(target)] = "stable-guid"

    # MoveFileEx(REPLACE_EXISTING) may be observed as deletion of the old
    # target followed by publication of the DocumentStore temporary file.
    handler.on_deleted(_event(target))
    target.write_text("new", encoding="utf-8")
    temporary.unlink()
    handler.on_moved(_event(temporary, destination=target))

    assert handler.pending_count == 1
    assert handler.process_pending_reloads(force=True) == 1
    assert target.read_text(encoding="utf-8") == "new"
    assert [entry[0] for entry in database.mutations] == ["modified"]
    assert [entry[0] for entry in asset_calls] == ["invalidate", "asset-modified"]


def test_stale_delete_event_reimports_existing_target_instead_of_deleting_it(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)

    target = tmp_path / "atomic.mat"
    target.write_text("republished", encoding="utf-8")
    database.guid_by_path[str(target)] = "stable-guid"

    handler.on_deleted(_event(target))

    assert handler.process_pending_reloads(force=True) == 1
    assert target.is_file()
    assert [entry[0] for entry in database.mutations] == ["modified"]
    assert [entry[0] for entry in asset_calls] == ["invalidate", "asset-modified"]


def test_recreated_meta_sidecar_cancels_missing_meta_rebuild(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)

    owner = tmp_path / "asset.txt"
    meta = tmp_path / "asset.txt.meta"
    owner.write_text("content", encoding="utf-8")
    handler.on_deleted(_event(meta))
    meta.write_text("restored", encoding="utf-8")

    assert handler.process_pending_reloads(force=True) == 1
    assert database.mutations == []
    assert asset_calls == []


def test_explicit_import_suppresses_matching_watcher_echo(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)
    path = tmp_path / "created.txt"
    path.write_text("content", encoding="utf-8")

    AssetManager.import_asset(str(path), database=database)
    assert [entry[0] for entry in database.mutations] == ["import"]
    handler.on_created(_event(path))
    assert handler.process_pending_reloads(force=True) == 1
    assert [entry[0] for entry in database.mutations] == ["import"]


def test_real_edit_after_explicit_reimport_is_not_suppressed(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)
    path = tmp_path / "modified.txt"
    path.write_text("first", encoding="utf-8")
    database.guid_by_path[str(path)] = "stable-guid"

    assert AssetManager.reimport_asset(str(path), database=database)
    handler.on_modified(_event(path))
    handler.process_pending_reloads(force=True)
    assert [entry[0] for entry in database.mutations] == ["modified"]

    path.write_text("a genuinely newer and larger edit", encoding="utf-8")
    handler.on_modified(_event(path))
    handler.process_pending_reloads(force=True)
    assert [entry[0] for entry in database.mutations] == ["modified", "modified"]


def test_cleanup_drains_events_before_releasing_engine(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    engine = _EngineProbe(database)
    manager = ResourcesManager(str(tmp_path), engine)
    manager._event_handler = ResourceChangeHandler(engine)
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)

    path = tmp_path / "asset.txt"
    path.write_text("content", encoding="utf-8")
    database.guid_by_path[str(path)] = "stable-guid"
    manager._event_handler.on_modified(_event(path))
    manager.cleanup()

    assert [entry[0] for entry in database.mutations] == ["modified"]
    assert database.mutations[0][-1] == threading.get_ident()
    assert manager._engine is None
    assert ResourcesManager.instance() is None


def test_initial_script_scan_publishes_artifact_for_main_thread(monkeypatch, tmp_path):
    assets = tmp_path / "Assets"
    assets.mkdir()
    valid = assets / "valid.py"
    invalid = assets / "invalid.py"
    particle = assets / "smoke.particle.py"
    valid.write_text("value = 1\n", encoding="utf-8")
    invalid.write_text("def broken(:\n", encoding="utf-8")
    particle.write_text("this is checked by the particle compiler\n", encoding="utf-8")

    manager = ResourcesManager(str(tmp_path), _EngineProbe(_AssetDatabaseProbe()))
    commits = []
    monkeypatch.setattr(
        "Infernux.components.script_loader.set_script_error",
        lambda path, message: commits.append(("set", path, message, threading.get_ident())),
    )
    monkeypatch.setattr(
        "Infernux.components.script_loader._clear_script_error",
        lambda path: commits.append(("clear", path, threading.get_ident())),
    )

    worker = threading.Thread(target=manager._initial_script_scan)
    worker.start()
    worker.join()
    assert commits == []

    assert manager.process_pending_reloads() == 2
    assert {entry[0] for entry in commits} == {"set", "clear"}
    assert all(entry[-1] == threading.get_ident() for entry in commits)
    manager.cleanup()


def test_real_observer_is_owned_joinable_and_commits_on_main(monkeypatch, tmp_path):
    assets = tmp_path / "Assets"
    assets.mkdir()
    database = _AssetDatabaseProbe()
    manager = ResourcesManager(str(tmp_path), _EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)

    manager.start()
    deadline = time.monotonic() + 3.0
    while manager._observer is None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert manager._observer is not None
    assert manager._observer.daemon is False
    assert manager._thread.daemon is False

    path = assets / "watched.txt"
    path.write_text("content", encoding="utf-8")
    while (
        (manager._event_handler is None or manager._event_handler.pending_count == 0)
        and time.monotonic() < deadline
    ):
        time.sleep(0.01)

    assert manager._event_handler.pending_count > 0
    assert database.mutations == []
    manager.process_pending_reloads(force=True)
    assert database.mutations
    assert all(entry[-1] == threading.get_ident() for entry in database.mutations)

    manager.cleanup()
    assert manager._thread is None
    assert manager._observer is None


def test_engine_exit_drains_resources_before_native_cleanup():
    order = []
    engine = Engine.__new__(Engine)
    engine._mode = RuntimeMode.Headless
    engine._before_exit_callback = None
    engine._play_mode_manager = None
    engine._resources_manager = SimpleNamespace(cleanup=lambda: order.append("resources"))
    engine._engine = SimpleNamespace(cleanup=lambda: order.append("native"))
    engine._gui_objects = {}

    engine.exit()

    assert order == ["resources", "native"]
    assert engine._resources_manager is None
    assert engine._engine is None
