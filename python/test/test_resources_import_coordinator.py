from __future__ import annotations

import inspect
import threading
import time
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from Infernux.core.assets import AssetManager
from Infernux.engine.engine import Engine
from Infernux.engine.import_coordinator import AssetFsEventKind
from Infernux.engine.interaction import ActionOrigin, action_origin_scope
from Infernux.engine.resources_manager import (
    ResourceChangeHandler,
    ResourcesManager,
    _AssetImportNotReady,
)
from Infernux.engine.path_utils import path_key
from Infernux.debug import Debug
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
    refresh_pending = False

    def __init__(self):
        self.guid_by_path = {}
        self.queries = []
        self.mutations = []

    def complete_pending_refresh(self):
        if not self.refresh_pending:
            raise AssertionError("completion requires pending refresh work")
        self.refresh_pending = False

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
        self.editor_wakes = 0
        self.full_speed_requests = 0

    def get_asset_database(self):
        return self.asset_database

    def request_editor_wake(self):
        self.editor_wakes += 1

    def request_full_speed_frame(self):
        self.full_speed_requests += 1


def _event(path, *, destination=""):
    values = {"is_directory": False, "src_path": str(path)}
    if destination:
        values["dest_path"] = str(destination)
    return SimpleNamespace(**values)


def _run_script_publication_owner(handler: ResourceChangeHandler) -> int:
    if not handler._frontend_worker_running:
        handler.process_script_worker()
    return handler._drain_script_results()


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
        "_publish_asset_content_change",
        classmethod(
            lambda _cls, path, event_type="modified", **_kwargs: calls.append(
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


def test_script_submission_wakes_the_frontend_worker(tmp_path):
    database = _AssetDatabaseProbe()
    wakes = []
    handler = ResourceChangeHandler(
        _EngineProbe(database),
        frontend_wake=lambda: wakes.append("wake"),
    )
    script = tmp_path / "FastReload.py"
    script.write_text("value = 1\n", encoding="utf-8")

    assert handler._check_script(str(script), origin="editor") is not None
    assert wakes == ["wake"]


def test_script_watchdog_event_has_no_debounce_and_wakes_idle_editor(tmp_path):
    database = _AssetDatabaseProbe()
    engine = _EngineProbe(database)
    handler = ResourceChangeHandler(engine)
    script = tmp_path / "ImmediateReload.py"
    script.write_text("value = 1\n", encoding="utf-8")

    handler.on_modified(_event(script))

    assert len(handler._coordinator.drain(now=time.monotonic() + 0.001)) == 1
    assert engine.editor_wakes == 1


def test_watcher_event_waits_for_asset_database_refresh_commit(tmp_path, monkeypatch):
    database = _AssetDatabaseProbe()
    database.refresh_pending = True
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset = tmp_path / "DeferredDuringRefresh.png"
    asset.write_bytes(b"asset")
    dispatched = []
    errors = []
    monkeypatch.setattr(handler, "_dispatch_event", dispatched.append)
    monkeypatch.setattr(Debug, "log_error", errors.append)

    handler._coordinator.submit(AssetFsEventKind.MODIFIED, str(asset.resolve()))

    assert handler.process_pending_reloads(force=True) == 1
    assert dispatched == []
    assert errors == []
    assert handler.pending_count == 1

    database.refresh_pending = False
    assert handler.process_pending_reloads(force=True) == 1
    assert len(dispatched) == 1
    assert dispatched[0].attempt == 0
    assert errors == []


def test_script_frontend_completion_wakes_owner_for_publication(tmp_path):
    database = _AssetDatabaseProbe()
    engine = _EngineProbe(database)
    handler = ResourceChangeHandler(engine)
    script = tmp_path / "PreparedInBackground.py"
    script.write_text("value = 1\n", encoding="utf-8")
    assert handler._check_script(str(script), origin="editor") is not None

    assert handler.process_script_worker() == 1
    assert engine.editor_wakes == 1


def test_deleted_script_event_preserves_queued_guid_for_missing_component_replacement(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    calls = []
    _patch_asset_manager(monkeypatch, calls)
    script = tmp_path / "Attached.py"
    path = str(script.resolve())
    database.guid_by_path[path] = "attached-guid"
    marked = []

    class _PlayMode:
        @staticmethod
        def mark_components_missing_for_script(guid, deleted_path):
            marked.append((guid, deleted_path))

    from Infernux.engine.play_mode import PlayModeManager
    monkeypatch.setattr(PlayModeManager, "instance", classmethod(lambda _cls: _PlayMode()))

    handler._coordinator.submit(
        AssetFsEventKind.DELETED,
        path,
        guid_hint="attached-guid",
    )
    assert handler.process_pending_reloads(force=True) == 1
    assert marked == [("attached-guid", path)]


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


def test_stale_meta_deleted_event_after_asset_delete_is_ignored(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)

    owner = tmp_path / "DeletedScript.py"
    meta = tmp_path / "DeletedScript.py.meta"
    owner.write_text("value = 1\n", encoding="utf-8")
    meta.write_text("meta", encoding="utf-8")

    owner.unlink()
    meta.unlink()
    handler._coordinator.submit(AssetFsEventKind.META_DELETED, str(owner.resolve()))

    assert handler.process_pending_reloads(force=True) == 1
    assert database.mutations == []


def test_stale_meta_deleted_event_after_atomic_replace_is_ignored(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)

    owner = tmp_path / "StableScript.py"
    meta = tmp_path / "StableScript.py.meta"
    owner.write_text("value = 1\n", encoding="utf-8")
    meta.write_text("replacement meta", encoding="utf-8")
    handler._coordinator.submit(AssetFsEventKind.META_DELETED, str(owner.resolve()))

    assert handler.process_pending_reloads(force=True) == 1
    assert database.mutations == []


def test_asset_manager_delete_preserves_serialized_reference_identity(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    calls = []
    _patch_asset_manager(monkeypatch, calls)
    asset = tmp_path / "RaceDust.particlegraph"
    asset.write_text("{}", encoding="utf-8")
    path = str(asset.resolve())
    database.guid_by_path[path] = "race-dust-guid"
    assert AssetManager.delete_asset(path, database=database)

    assert [entry[0] for entry in database.mutations] == ["deleted"]
    source = inspect.getsource(AssetManager.delete_asset)
    assert "_clear_deleted_live_references" not in source


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


def test_material_preview_invalidation_does_not_hide_external_edit(monkeypatch, tmp_path):
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
    assert database.mutations == [
        ("modified", path_str, threading.get_ident())
    ]


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
    # A move is published exclusively by AssetMutationService as one typed
    # relocation; it must not also masquerade as a content-change event.
    assert [entry[0] for entry in asset_calls] == ["invalidate"]
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
    target = tmp_path / "MainMenu.scene"
    temporary = tmp_path / "MainMenu.scene.tmp.123456.9"
    target.write_text("{}", encoding="utf-8")

    handler.on_moved(_event(temporary, destination=target))

    assert handler.process_pending_reloads(force=True) == 1
    assert database.mutations == [("import", str(target.resolve()), threading.get_ident())]
    assert [entry[0] for entry in asset_calls] == ["asset-created"]


def test_active_registered_scene_external_edit_is_reimported(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)
    target = tmp_path / "RaceTrack.scene"
    target.write_text("{}", encoding="utf-8")
    database.guid_by_path[str(target.resolve())] = "stable-guid"

    handler.on_modified(_event(target))

    assert handler.process_pending_reloads(force=True) == 1
    assert database.mutations == [
        ("modified", str(target.resolve()), threading.get_ident())
    ]
    assert [entry[0] for entry in asset_calls] == ["invalidate", "asset-modified"]


def test_dirty_asset_document_allows_external_reimport_before_publication(
    monkeypatch,
    tmp_path,
):
    from Infernux.engine.interaction import (
        DocumentCapability,
        DocumentKey,
        DocumentKind,
        DocumentRegistry,
        DocumentState,
    )

    class _ReloadableController:
        def __init__(self):
            self.reloaded = False

        def reload_from_resource(self, *, document_id: str, resource_path: str):
            del document_id, resource_path
            self.reloaded = True
            return True

    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)
    target = tmp_path / "Dirty.effect"
    effect_document = {
        "$schema": "infernux.render_effect",
        "feature_type": "infernux.post.bloom",
        "parameters": {},
        "dependencies": [],
    }
    import json

    target.write_text(json.dumps(effect_document), encoding="utf-8")
    target_path = str(target.resolve())
    database.guid_by_path[target_path] = "dirty-guid"
    registry = DocumentRegistry()
    controller = _ReloadableController()
    document = registry.create(
        DocumentKind.RENDER_EFFECT,
        "Dirty",
        key=DocumentKey.resource(DocumentKind.RENDER_EFFECT, target_path),
        resource_path=target_path,
        revision=1,
        saved_revision=0,
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
        controller=controller,
    )

    target.write_text(json.dumps(effect_document, indent=2), encoding="utf-8")
    handler.on_modified(_event(target))

    assert handler.process_pending_reloads(force=True) == 1
    assert database.mutations == [
        ("modified", target_path, threading.get_ident())
    ]
    assert [entry[0] for entry in asset_calls] == ["asset-modified"]
    assert controller.reloaded
    assert document.external_revision == 1
    assert document.state is DocumentState.READY
    assert not document.is_dirty


def test_dirty_scene_still_blocks_external_reimport_for_user_arbitration(
    monkeypatch,
    tmp_path,
):
    from Infernux.engine.interaction import (
        DocumentCapability,
        DocumentKey,
        DocumentKind,
        DocumentRegistry,
        DocumentState,
    )

    class _SceneController:
        @staticmethod
        def reload_from_resource(*, document_id: str, resource_path: str):
            del document_id, resource_path
            return True

    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(_EngineProbe(database))
    asset_calls = []
    _patch_asset_manager(monkeypatch, asset_calls)
    target = tmp_path / "Dirty.scene"
    target.write_text("{}", encoding="utf-8")
    target_path = str(target.resolve())
    database.guid_by_path[target_path] = "scene-guid"
    registry = DocumentRegistry()
    document = registry.create(
        DocumentKind.SCENE,
        "Dirty",
        key=DocumentKey.resource(DocumentKind.SCENE, target_path),
        resource_path=target_path,
        revision=1,
        saved_revision=0,
        capabilities=DocumentCapability.SAVE | DocumentCapability.DISCARD,
        controller=_SceneController(),
    )

    target.write_text('{"changed": true}', encoding="utf-8")
    handler.on_modified(_event(target))

    assert handler.process_pending_reloads(force=True) == 1
    assert database.mutations == []
    assert asset_calls == []
    assert document.state is DocumentState.CONFLICT
    assert document.is_dirty


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


def test_worker_code_is_forwarded_to_owner_script_publish(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    engine = _EngineProbe(database)
    manager = ResourcesManager(str(tmp_path), engine)
    handler = ResourceChangeHandler(engine)
    manager._event_handler = handler
    path = tmp_path / "worker_code.py"
    path.write_text("value = 1\n", encoding="utf-8")
    published = []
    handler._publish_valid_script = lambda *args, **kwargs: published.append(kwargs) or True

    handler._check_script(str(path), origin="editor")
    assert _run_script_publication_owner(handler) == 0

    assert len(published) == 1
    assert published[0]["source"] == path.read_bytes()
    assert isinstance(published[0]["code"], types.CodeType)


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
    # Initial scan is one ready-barrier transaction: an invalid member keeps
    # every valid member out of the live registry until the whole scan passes.
    assert {entry[0] for entry in commits} == {"set"}
    assert all(entry[-1] == threading.get_ident() for entry in commits)
    manager.cleanup()


def test_prepare_startup_finishes_refresh_before_the_watcher_loop(monkeypatch, tmp_path):
    assets = tmp_path / "Assets"
    assets.mkdir()
    (assets / "valid.py").write_text("value = 1\n", encoding="utf-8")
    (assets / "broken.py").write_text("def broken(:\n", encoding="utf-8")

    manager = ResourcesManager(str(tmp_path), _EngineProbe(_AssetDatabaseProbe()))
    published = []
    monkeypatch.setattr(
        "Infernux.components.script_loader.set_script_error",
        lambda path, message: published.append(("set", path, message)),
    )
    monkeypatch.setattr(
        "Infernux.components.script_loader._clear_script_error",
        lambda path: published.append(("clear", path)),
    )
    started = []
    monkeypatch.setattr(
        manager,
        "start",
        lambda **kwargs: started.append(kwargs.get("skip_initial_scan")),
    )

    manager.prepare_startup()

    assert started == [True]
    assert manager._startup_prepared is True
    assert manager._event_handler is not None
    assert manager._event_handler._initial_scan_transaction_id is None
    assert manager.process_pending_reloads(force=True) == 0
    assert {entry[0] for entry in published} == {"set"}
    manager.cleanup()


def test_background_asset_refresh_uses_native_transaction_result(tmp_path):
    class Database(_AssetDatabaseProbe):
        refresh_pending = True

        def __init__(self):
            super().__init__()
            self.commit_attempts = 0
            self.refresh_restarts = 0

        def try_commit_refresh(self):
            self.commit_attempts += 1
            if self.commit_attempts == 1:
                self.refresh_restarts += 1
                return False
            self.refresh_pending = False
            return True

    database = Database()
    manager = ResourcesManager(str(tmp_path), _EngineProbe(database))
    manager._startup_prepared = True

    assert manager.process_pending_reloads() == 0
    assert database.refresh_restarts == 1
    assert database.refresh_pending is True

    assert manager.process_pending_reloads() == 0
    assert database.refresh_pending is False
    manager.cleanup()


def test_initial_script_scan_includes_assets_and_installed_packages(monkeypatch, tmp_path):
    assets = tmp_path / "Assets"
    package_root = tmp_path / "Packages" / "vendor" / "gameplay"
    runtime = package_root / "Runtime"
    assets.mkdir()
    runtime.mkdir(parents=True)
    asset_script = assets / "asset_component.py"
    package_script = runtime / "package_component.py"
    asset_script.write_text("value = 1\n", encoding="utf-8")
    package_script.write_text("value = 2\n", encoding="utf-8")
    (package_root / "InxPackage.json").write_text("{}", encoding="utf-8")

    manager = ResourcesManager(str(tmp_path), _EngineProbe(_AssetDatabaseProbe()))
    handler = manager._ensure_event_handler()
    submitted = []
    monkeypatch.setattr(
        handler,
        "_check_script",
        lambda path, **kwargs: submitted.append((path, kwargs)) or object(),
    )

    manager._initial_script_scan()

    assert {Path(path) for path, _kwargs in submitted} == {
        asset_script,
        package_script,
    }
    transaction_ids = {kwargs["transaction_id"] for _path, kwargs in submitted}
    assert len(transaction_ids) == 1
    manager.cleanup()


def test_package_runtime_script_publishes_with_isolated_graph_identity(
    monkeypatch,
    tmp_path,
):
    (tmp_path / "Assets").mkdir()
    package_root = tmp_path / "Packages" / "vendor" / "gameplay"
    runtime = package_root / "Runtime"
    runtime.mkdir(parents=True)
    (package_root / "InxPackage.json").write_text("{}", encoding="utf-8")
    script = runtime / "package_component.py"
    script.write_text("value = 1\n", encoding="utf-8")

    manager = ResourcesManager(str(tmp_path), _EngineProbe(_AssetDatabaseProbe()))
    handler = manager._ensure_event_handler()
    published = []
    monkeypatch.setattr(
        handler,
        "_publish_valid_script",
        lambda path, **kwargs: published.append((path, kwargs)) or True,
    )

    assert manager.submit_script_change(str(script), origin="editor") is not None
    assert manager.process_pending_reloads(force=True) == 0
    assert [Path(path) for path, _kwargs in published] == [script]
    record = handler.dependency_graph.module_for_path(script)
    assert record is not None
    assert record.id.module_name == (
        "_infernux_packages.vendor.gameplay.runtime.package_component"
    )
    manager.cleanup()


def test_initial_scan_supersede_restarts_barrier_with_current_sources(
    monkeypatch, tmp_path
):
    first = tmp_path / "Assets" / "first.py"
    second = tmp_path / "Assets" / "second.py"
    first.parent.mkdir()
    first.write_text("value = 'old-first'\n", encoding="utf-8")
    second.write_text("value = 'old-second'\n", encoding="utf-8")
    handler = ResourceChangeHandler(
        _EngineProbe(_AssetDatabaseProbe()), project_path=str(tmp_path)
    )
    published = []
    monkeypatch.setattr(
        handler,
        "_publish_valid_script",
        lambda path, **kwargs: published.append((path, kwargs["source"])) or True,
    )

    initial = handler.begin_script_transaction(
        (str(first), str(second)), initial_scan=True
    )
    handler._check_script(
        str(first),
        origin="initial_scan",
        change_kind="initial_scan",
        transaction_id=initial,
    )
    handler._check_script(
        str(second),
        origin="initial_scan",
        change_kind="initial_scan",
        transaction_id=initial,
    )
    assert handler.process_script_worker(max_items=1) == 1
    handler._drain_script_results()
    assert published == []

    first.write_text("value = 'new-first'\n", encoding="utf-8")
    handler._check_script(str(first), origin="editor", force=True)
    assert handler.process_script_worker(max_items=1) == 1
    handler._drain_script_results()

    # The old barrier is gone and has been replaced by one transaction.  The
    # replacement reads both files, including the newer first source.
    assert handler._initial_scan_transaction_id is not None
    assert handler._initial_scan_transaction_id != initial
    assert published == []
    assert handler.process_script_worker() == 2
    handler._drain_script_results()

    assert {path_key(path) for path, _source in published} == {
        path_key(str(first)),
        path_key(str(second)),
    }
    assert dict((path_key(path), source) for path, source in published)[
        path_key(str(first))
    ] == first.read_bytes()
    assert handler._initial_scan_transaction_id is None


def test_restarted_initial_scan_enters_lkg_and_dependency_graph(
    monkeypatch, tmp_path
):
    script = tmp_path / "Assets" / "controller.py"
    script.parent.mkdir()
    script.write_text("value = 'old'\n", encoding="utf-8")
    handler = ResourceChangeHandler(
        _EngineProbe(_AssetDatabaseProbe()), project_path=str(tmp_path)
    )
    monkeypatch.setattr(handler, "_publish_valid_script", lambda *_args, **_kwargs: True)
    initial = handler.begin_script_transaction((str(script),), initial_scan=True)
    handler._check_script(
        str(script),
        origin="initial_scan",
        change_kind="initial_scan",
        transaction_id=initial,
    )
    script.write_text("value = 'new'\n", encoding="utf-8")
    handler._check_script(str(script), origin="editor", force=True)
    handler.process_script_worker()
    handler._drain_script_results()
    handler.process_script_worker()
    handler._drain_script_results()

    latest = handler._script_change_collector.last_known_good(str(script))
    record = handler.dependency_graph.module_for_path(str(script))
    assert latest is not None
    assert latest.source == script.read_bytes()
    assert record is not None
    assert record.source_hash == latest.content_hash
    assert handler._initial_scan_transaction_id is None


def test_initial_scan_supersede_does_not_block_following_transaction(
    monkeypatch, tmp_path
):
    initial_path = tmp_path / "Assets" / "initial.py"
    ordinary_path = tmp_path / "Assets" / "ordinary.py"
    initial_path.parent.mkdir()
    initial_path.write_text("value = 'initial-old'\n", encoding="utf-8")
    ordinary_path.write_text("value = 'ordinary'\n", encoding="utf-8")
    handler = ResourceChangeHandler(_EngineProbe(_AssetDatabaseProbe()))
    monkeypatch.setattr(handler, "_publish_valid_script", lambda *_args, **_kwargs: True)

    initial = handler.begin_script_transaction(
        (str(initial_path),), initial_scan=True
    )
    handler._check_script(
        str(initial_path),
        origin="initial_scan",
        change_kind="initial_scan",
        transaction_id=initial,
    )
    initial_path.write_text("value = 'initial-new'\n", encoding="utf-8")
    handler._check_script(str(initial_path), origin="editor", force=True)
    handler._check_script(str(ordinary_path), origin="editor", force=True)
    handler.process_script_worker()
    handler._drain_script_results()

    # The ordinary result remains queued behind the restarted ready barrier,
    # rather than being lost or leaving the old barrier permanently active.
    assert handler._initial_scan_transaction_id is not None
    assert handler._script_change_collector.last_known_good(str(ordinary_path)) is None
    handler.process_script_worker()
    handler._drain_script_results()
    assert handler._script_change_collector.last_known_good(str(ordinary_path)) is not None
    assert handler._initial_scan_transaction_id is None


def test_failed_restarted_initial_source_releases_barrier_and_keeps_diagnostic(
    tmp_path,
):
    script = tmp_path / "Assets" / "broken.py"
    script.parent.mkdir()
    script.write_text("value = 'old'\n", encoding="utf-8")
    handler = ResourceChangeHandler(_EngineProbe(_AssetDatabaseProbe()))
    published = []
    handler._publish_valid_script = (
        lambda *_args, **_kwargs: published.append(True) or True
    )

    initial = handler.begin_script_transaction((str(script),), initial_scan=True)
    handler._check_script(
        str(script),
        origin="initial_scan",
        change_kind="initial_scan",
        transaction_id=initial,
    )
    script.write_text("def broken(:\n", encoding="utf-8")
    handler._check_script(str(script), origin="editor", force=True)
    handler.process_script_worker()
    handler._drain_script_results()
    handler.process_script_worker()
    handler._drain_script_results()

    assert published == []
    assert handler._initial_scan_transaction_id is None
    diagnostic = handler._script_change_collector.diagnostic(str(script))
    assert diagnostic is not None
    assert diagnostic.messages == ("invalid syntax",)


def test_restarted_initial_scan_removes_deleted_members_and_ignores_late_old_result(
    monkeypatch, tmp_path
):
    first = tmp_path / "Assets" / "first.py"
    deleted = tmp_path / "Assets" / "deleted.py"
    first.parent.mkdir()
    first.write_text("value = 'first'\n", encoding="utf-8")
    deleted.write_text("value = 'deleted'\n", encoding="utf-8")
    handler = ResourceChangeHandler(_EngineProbe(_AssetDatabaseProbe()))
    published = []
    handler._publish_valid_script = (
        lambda path, **_kwargs: published.append(path) or True
    )

    initial = handler.begin_script_transaction(
        (str(first), str(deleted)), initial_scan=True
    )
    handler._check_script(
        str(first),
        origin="initial_scan",
        change_kind="initial_scan",
        transaction_id=initial,
    )
    handler._check_script(
        str(deleted),
        origin="initial_scan",
        change_kind="initial_scan",
        transaction_id=initial,
    )
    assert handler.process_script_worker(max_items=1) == 1
    handler._drain_script_results()

    deleted.unlink()
    first.write_text("value = 'new-first'\n", encoding="utf-8")
    handler._check_script(str(first), origin="editor", force=True)
    handler._restart_initial_scan_transaction(
        handler._script_transactions[initial]
    )
    replacement_id = handler._initial_scan_transaction_id
    replacement = handler._script_transactions[replacement_id]
    assert replacement.expected_paths == [str(first)]

    # The old deleted member was still queued.  It is consumed as retired
    # work and cannot recreate the old initial barrier.
    handler.process_script_worker()
    handler._drain_script_results()
    assert handler._initial_scan_transaction_id is None
    assert [path_key(path) for path in published] == [path_key(str(first))]


def test_script_revision_is_published_only_by_resources_safe_point(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    engine = _EngineProbe(database)
    manager = ResourcesManager(str(tmp_path), engine)
    handler = ResourceChangeHandler(engine)
    manager._event_handler = handler

    path = tmp_path / "controller.py"
    path.write_text("value = 1\n", encoding="utf-8")

    registry_calls = []
    monkeypatch.setattr(
        "Infernux.components.registry.register_component_script",
        lambda value, **_kwargs: registry_calls.append(value),
    )
    monkeypatch.setattr(
        "Infernux.components.script_loader._clear_script_error",
        lambda _path: None,
    )
    handler._dispatch_event = lambda _event: handler._check_script(str(path))
    handler.on_modified(_event(path))

    assert registry_calls == []
    assert manager.process_pending_reloads(force=True) == 1
    assert registry_calls == [str(path)]
    assert handler._script_change_collector.journal.last_known_good(str(path)) is not None


def test_frontend_worker_does_not_publish_or_update_graph(monkeypatch, tmp_path):
    assets = tmp_path / "Assets"
    assets.mkdir()
    database = _AssetDatabaseProbe()
    engine = _EngineProbe(database)
    manager = ResourcesManager(str(tmp_path), engine)
    handler = ResourceChangeHandler(engine, project_path=str(tmp_path))
    manager._event_handler = handler

    path = assets / "worker_probe.py"
    path.write_text("value = 1\n", encoding="utf-8")
    registered = []
    monkeypatch.setattr(
        "Infernux.components.registry.register_component_script",
        lambda value, **_kwargs: registered.append(value),
    )
    monkeypatch.setattr(
        "Infernux.components.script_loader._clear_script_error",
        lambda _path: None,
    )

    worker_id = []
    handler._check_script(str(path), origin="watchdog")

    def pump_frontend():
        worker_id.append(threading.get_ident())
        assert handler.process_script_worker() == 1

    worker = threading.Thread(target=pump_frontend)
    worker.start()
    worker.join()

    assert worker_id[0] != threading.get_ident()
    assert handler.dependency_graph_snapshot().modules == ()
    assert registered == []

    assert manager.process_pending_reloads() == 0
    assert handler.dependency_graph.module_for_path(str(path)) is not None
    assert registered == [str(path)]


def test_internal_asset_script_ingress_uses_collector_and_origin_mapping(
    monkeypatch, tmp_path
):
    assets = tmp_path / "Assets"
    assets.mkdir()
    database = _AssetDatabaseProbe()
    manager = ResourcesManager(str(tmp_path), _EngineProbe(database))
    calls = []
    _patch_asset_manager(monkeypatch, calls)
    script = assets / "EditorScript.py"
    script.write_text("value = 1\n", encoding="utf-8")
    path = str(script.resolve())

    with action_origin_scope(ActionOrigin.AUTOMATION):
        assert AssetManager.import_asset(path, database=database)
    handler = manager._event_handler
    assert handler is not None
    handler.process_script_worker()
    first = handler._script_change_collector.drain_completed()
    assert len(first) == 1
    assert first[0].change.origin == "automation"

    script.write_text("value = 2\n", encoding="utf-8")
    with action_origin_scope(ActionOrigin.EXTERNAL):
        assert AssetManager.reimport_asset(path, database=database)
    handler.process_script_worker()
    second = handler._script_change_collector.drain_completed()
    assert len(second) == 1
    assert second[0].change.origin == "watchdog"

    script.write_text("value = 3\n", encoding="utf-8")
    with action_origin_scope(ActionOrigin.USER):
        AssetManager._submit_internal_script_change(path, catalog_event="modified")
    handler.process_script_worker()
    third = handler._script_change_collector.drain_completed()
    assert len(third) == 1
    assert third[0].change.origin == "editor"


def test_internal_script_ingress_and_watcher_echo_publish_once(monkeypatch, tmp_path):
    assets = tmp_path / "Assets"
    assets.mkdir()
    database = _AssetDatabaseProbe()
    manager = ResourcesManager(str(tmp_path), _EngineProbe(database))
    _patch_asset_manager(monkeypatch, [])
    script = assets / "EditorScript.py"
    script.write_text("value = 1\n", encoding="utf-8")
    path = str(script.resolve())
    AssetManager._suppress_watcher_echo("modified", path)

    with action_origin_scope(ActionOrigin.AUTOMATION):
        AssetManager._submit_internal_script_change(path, catalog_event="modified")
    manager._event_handler.on_modified(_event(path))
    handler = manager._event_handler
    monkeypatch.setattr(
        handler,
        "_publish_valid_script",
        lambda *_args, **_kwargs: True,
    )
    assert manager.process_pending_reloads(force=True) == 1
    latest = handler._script_change_collector.journal.last_known_good(path)
    assert latest is not None
    assert latest.generation == 1


def test_duplicate_script_transaction_echo_keeps_canonical_publication_owner(
    monkeypatch, tmp_path
):
    assets = tmp_path / "Assets"
    assets.mkdir()
    database = _AssetDatabaseProbe()
    handler = ResourceChangeHandler(
        _EngineProbe(database),
        project_path=str(tmp_path),
    )
    script = assets / "SnakeController.py"
    script.write_text("value = 1\n", encoding="utf-8")
    path = str(script.resolve())

    first = handler._check_script(
        path,
        origin="automation",
        transaction_id="canonical-write",
    )
    echo = handler._check_script(
        path,
        origin="watchdog",
        transaction_id="watcher-echo",
    )
    assert first is not None
    assert echo is None

    monkeypatch.setattr(
        handler,
        "_publish_valid_script",
        lambda *_args, **_kwargs: True,
    )
    assert handler.process_script_worker() == 1
    # The return value counts startup-scan members, not ordinary publications.
    assert handler._drain_script_results() == 0
    latest = handler._script_change_collector.last_known_good(path)
    assert latest is not None
    assert latest.generation == first.generation
    assert handler._script_transactions == {}


def test_published_helper_queues_dependents_with_one_transaction_without_cascade(
    monkeypatch, tmp_path
):
    assets = tmp_path / "Assets"
    assets.mkdir()
    helper = assets / "helper.py"
    dependent = assets / "dependent.py"
    helper.write_text("value = 1\n", encoding="utf-8")
    dependent.write_text("from helper import value\n", encoding="utf-8")
    database = _AssetDatabaseProbe()
    engine = _EngineProbe(database)
    manager = ResourcesManager(str(tmp_path), engine)
    handler = ResourceChangeHandler(engine, project_path=str(tmp_path))
    manager._event_handler = handler
    handler.dependency_graph.index_assets()
    monkeypatch.setattr(handler, "_publish_valid_script", lambda *_args, **_kwargs: True)

    submitted = []
    original_submit = handler._script_change_collector.submit

    def submit(*args, **kwargs):
        submitted.append((args, dict(kwargs)))
        return original_submit(*args, **kwargs)

    monkeypatch.setattr(handler._script_change_collector, "submit", submit)
    helper.write_text("value = 2\n", encoding="utf-8")
    source_change = handler._check_script(str(helper), origin="editor")
    handler.process_pending_reloads(force=True)

    dependency_calls = [
        kwargs
        for _args, kwargs in submitted
        if kwargs.get("origin") == "dependency"
    ]
    assert len(dependency_calls) == 1
    assert dependency_calls[0]["change_kind"] == "dependency"
    assert dependency_calls[0]["catalog_event"] is None
    assert dependency_calls[0]["force"] is True
    assert dependency_calls[0]["transaction_id"] == source_change.transaction_id

    handler.process_pending_reloads(force=True)
    assert len(
        [kwargs for _args, kwargs in submitted if kwargs.get("origin") == "dependency"]
    ) == 1


def test_failed_source_does_not_advance_published_dependency_graph(
    monkeypatch, tmp_path
):
    assets = tmp_path / "Assets"
    assets.mkdir()
    helper = assets / "helper.py"
    dependent = assets / "dependent.py"
    helper.write_text("value = 1\n", encoding="utf-8")
    dependent.write_text("from helper import value\n", encoding="utf-8")
    database = _AssetDatabaseProbe()
    engine = _EngineProbe(database)
    manager = ResourcesManager(str(tmp_path), engine)
    handler = ResourceChangeHandler(engine, project_path=str(tmp_path))
    manager._event_handler = handler
    handler.dependency_graph.index_assets()
    before = handler.dependency_graph.module_for_path(str(helper)).source_hash
    monkeypatch.setattr(
        handler._script_change_collector,
        "_compile_source",
        lambda _source: (_ for _ in ()).throw(SyntaxError("broken")),
    )
    helper.write_text("def broken(:\n", encoding="utf-8")
    handler._check_script(str(helper), origin="editor")
    handler.process_pending_reloads(force=True)

    after = handler.dependency_graph.module_for_path(str(helper)).source_hash
    assert after == before
    assert handler._script_change_collector.journal.last_known_good(str(helper)) is None


def test_resource_script_validation_failure_does_not_publish_candidate(
    monkeypatch, tmp_path
):
    database = _AssetDatabaseProbe()
    engine = _EngineProbe(database)
    manager = ResourcesManager(str(tmp_path), engine)
    handler = ResourceChangeHandler(engine)
    manager._event_handler = handler

    path = tmp_path / "controller.py"
    path.write_text("value = 1\n", encoding="utf-8")
    class CompilerError:
        file_path = str(path)
        line_number = 1
        message = "invalid syntax"

        def __str__(self):
            return "controller.py:1:0: invalid syntax"

    compiler_error = CompilerError()
    monkeypatch.setattr(
        handler._script_change_collector,
        "_compile_source",
        lambda _source: (_ for _ in ()).throw(SyntaxError("invalid syntax")),
    )
    monkeypatch.setattr(
        "Infernux.components.script_loader.set_script_error",
        lambda _path, _message: None,
    )
    handler._dispatch_event = lambda _event: handler._check_script(str(path))
    handler.on_modified(_event(path))

    assert manager.process_pending_reloads(force=True) == 1
    assert handler._script_change_collector.journal.last_known_good(str(path)) is None
    assert handler._script_change_collector.journal.diagnostic(str(path)).messages == (
        "invalid syntax",
    )


def test_script_failure_keeps_already_published_registry_entry(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    engine = _EngineProbe(database)
    manager = ResourcesManager(str(tmp_path), engine)
    handler = ResourceChangeHandler(engine)
    manager._event_handler = handler
    path = tmp_path / "controller.py"
    path.write_text("value = 1\n", encoding="utf-8")

    registered = []
    unregistered = []
    monkeypatch.setattr(
        "Infernux.components.registry.register_component_script",
        lambda value, **_kwargs: registered.append(value),
    )
    monkeypatch.setattr(
        "Infernux.components.registry.unregister_component_script",
        lambda value: unregistered.append(value),
    )
    monkeypatch.setattr(
        "Infernux.components.script_loader._clear_script_error",
        lambda _path: None,
    )
    monkeypatch.setattr(
        "Infernux.components.script_loader.set_script_error",
        lambda _path, _message: None,
    )

    handler._dispatch_event = lambda _event: handler._check_script(str(path))
    handler.on_modified(_event(path))
    manager.process_pending_reloads(force=True)
    assert registered == [str(path)]
    assert unregistered == []

    path.write_text("def broken(:\n", encoding="utf-8")
    handler.on_modified(_event(path))
    manager.process_pending_reloads(force=True)

    assert registered == [str(path)]
    assert unregistered == []
    assert handler._script_change_collector.journal.last_known_good(str(path)).generation == 1


def test_disk_change_after_check_drops_candidate_before_publish(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    engine = _EngineProbe(database)
    manager = ResourcesManager(str(tmp_path), engine)
    handler = ResourceChangeHandler(engine)
    manager._event_handler = handler
    path = tmp_path / "controller.py"
    path.write_text("value = 'A'\n", encoding="utf-8")
    published = []
    handler._publish_valid_script = lambda *args, **kwargs: published.append(
        kwargs["source"]
    ) or True

    handler._check_script(str(path))
    path.write_text("value = 'B'\n", encoding="utf-8")
    _run_script_publication_owner(handler)

    assert published == []
    assert handler._script_change_collector.journal.last_known_good(str(path)) is None


def test_publish_exception_does_not_advance_lkg(monkeypatch, tmp_path):
    database = _AssetDatabaseProbe()
    engine = _EngineProbe(database)
    manager = ResourcesManager(str(tmp_path), engine)
    handler = ResourceChangeHandler(engine)
    manager._event_handler = handler
    path = tmp_path / "controller.py"
    path.write_text("value = 1\n", encoding="utf-8")

    def fail_publish(*_args, **_kwargs):
        raise RuntimeError("publish callback failed")

    handler._publish_valid_script = fail_publish
    handler._check_script(str(path))
    _run_script_publication_owner(handler)

    assert handler._script_change_collector.journal.last_known_good(str(path)) is None


def test_reload_rejection_keeps_lkg_and_live_body_at_resources_safe_point(
    monkeypatch, tmp_path
):
    from Infernux.engine.play_mode import PlayModeManager, ScriptReloadOutcome

    database = _AssetDatabaseProbe()
    engine = _EngineProbe(database)
    manager = ResourcesManager(str(tmp_path), engine)
    handler = ResourceChangeHandler(engine)
    manager._event_handler = handler
    path = tmp_path / "controller.py"
    path.write_text("value = 'A'\n", encoding="utf-8")
    monkeypatch.setattr(
        "Infernux.components.registry.register_component_script",
        lambda _path, **_kwargs: None,
    )
    monkeypatch.setattr(
        "Infernux.components.script_loader._clear_script_error",
        lambda _path: None,
    )

    live_body = {"value": "old"}
    publish_results = iter(
        (
            ScriptReloadOutcome(True, True, 1),
            ScriptReloadOutcome(False, True, 0, "schema rejected"),
        )
    )

    class _PlayModeProbe:
        def prepare_script_reload_batch(self, revisions):
            return SimpleNamespace(revisions=tuple(revisions))

        def commit_script_reload_batch(self, batch):
            outcome = next(publish_results)
            if outcome.success:
                live_body["value"] = (
                    batch.revisions[0].source.decode("utf-8").split("'")[1]
                )
            return outcome

        def rollback_script_reload_batch(self, _batch):
            return None

        def finalize_script_reload_batch(self, _batch):
            return None

    monkeypatch.setattr(
        PlayModeManager,
        "instance",
        classmethod(lambda _cls: _PlayModeProbe()),
    )

    handler._check_script(str(path))
    _run_script_publication_owner(handler)
    first_lkg = handler._script_change_collector.journal.last_known_good(str(path))
    assert first_lkg is not None
    assert first_lkg.generation == 1
    assert live_body["value"] == "A"

    path.write_text("value = 'B'\n", encoding="utf-8")
    handler._check_script(str(path))
    _run_script_publication_owner(handler)

    current_lkg = handler._script_change_collector.journal.last_known_good(str(path))
    assert current_lkg == first_lkg
    assert live_body["value"] == "A"


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
    engine._process_owned_exit = False
    engine._before_exit_callback = None
    engine._play_mode_manager = None
    engine._runtime_scene_manager = None
    engine._runtime_scheduler = SimpleNamespace(clear=lambda: None)
    engine._resources_manager = SimpleNamespace(cleanup=lambda: order.append("resources"))
    engine._engine = SimpleNamespace(cleanup=lambda: order.append("native"))
    engine._gui_objects = {}

    engine.exit()

    assert order == ["resources", "native"]
    assert engine._resources_manager is None
    assert engine._engine is None


def test_dependency_transaction_publishes_helper_and_dependent_only_after_both_ready(
    monkeypatch, tmp_path
):
    assets = tmp_path / "Assets"
    assets.mkdir()
    helper = assets / "helper.py"
    dependent = assets / "dependent.py"
    helper.write_text("value = 1\n", encoding="utf-8")
    dependent.write_text("from helper import value\n", encoding="utf-8")

    handler = ResourceChangeHandler(
        _EngineProbe(_AssetDatabaseProbe()), project_path=str(tmp_path)
    )
    handler.dependency_graph.index_assets()
    published = []
    monkeypatch.setattr(
        handler,
        "_publish_valid_script",
        lambda path, **_kwargs: published.append(path) or True,
    )

    helper.write_text("value = 2\n", encoding="utf-8")
    handler._check_script(str(helper), origin="editor")
    assert handler.process_script_worker() == 1
    handler._drain_script_results()
    assert published == []

    # The root has staged the closure and queued the dependent, but publication
    # is still blocked until the dependent frontend result is ready.
    assert handler.process_script_worker() == 1
    handler._drain_script_results()
    assert published == [str(helper.resolve()), str(dependent.resolve())]
    assert handler._script_change_collector.last_known_good(str(helper)) is not None
    assert handler._script_change_collector.last_known_good(str(dependent)) is not None


def test_dependency_failure_aborts_root_without_live_graph_or_lkg_change(
    monkeypatch, tmp_path
):
    assets = tmp_path / "Assets"
    assets.mkdir()
    helper = assets / "helper.py"
    dependent = assets / "dependent.py"
    helper.write_text("value = 1\n", encoding="utf-8")
    dependent.write_text("from helper import value\n", encoding="utf-8")
    handler = ResourceChangeHandler(
        _EngineProbe(_AssetDatabaseProbe()), project_path=str(tmp_path)
    )
    handler.dependency_graph.index_assets()
    before = handler.dependency_graph.module_for_path(str(helper)).source_hash
    published = []
    monkeypatch.setattr(
        handler,
        "_publish_valid_script",
        lambda path, **_kwargs: published.append(path) or True,
    )
    def compile_with_dependent_failure(source):
        if b"broken" in source:
            raise SyntaxError("dependent syntax failure")
        return compile(source, "<transaction-test>", "exec")

    handler._script_change_collector._compile_source = compile_with_dependent_failure
    helper.write_text("value = 2\n", encoding="utf-8")
    dependent.write_text("def broken(:\n", encoding="utf-8")
    handler._check_script(str(helper), origin="editor")
    assert handler.process_script_worker() == 1
    handler._drain_script_results()
    assert handler.process_script_worker() == 1
    handler._drain_script_results()

    assert published == []
    assert handler.dependency_graph.module_for_path(str(helper)).source_hash == before
    assert handler._script_change_collector.last_known_good(str(helper)) is None
    assert handler._script_change_collector.last_known_good(str(dependent)) is None


def test_disk_supersede_aborts_transaction_without_publishing(tmp_path):
    path = tmp_path / "controller.py"
    path.write_text("value = 'A'\n", encoding="utf-8")
    handler = ResourceChangeHandler(_EngineProbe(_AssetDatabaseProbe()))
    published = []
    handler._publish_valid_script = lambda path, **_kwargs: published.append(path) or True

    handler._check_script(str(path), origin="editor")
    path.write_text("value = 'B'\n", encoding="utf-8")
    assert handler.process_script_worker() == 1
    handler._drain_script_results()

    assert published == []
    assert handler._script_change_collector.last_known_good(str(path)) is None


def test_second_member_publish_failure_rolls_back_batch_and_lkg(monkeypatch, tmp_path):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("value = 1\n", encoding="utf-8")
    second.write_text("value = 2\n", encoding="utf-8")
    handler = ResourceChangeHandler(_EngineProbe(_AssetDatabaseProbe()))
    tx = handler.begin_script_transaction((str(first), str(second)))
    attempted = []

    def publish(path, **_kwargs):
        attempted.append(path)
        if path_key(path) == path_key(str(second)):
            raise RuntimeError("second member rejected")
        return True

    handler._publish_valid_script = publish
    handler._check_script(str(first), origin="editor", transaction_id=tx)
    handler._check_script(str(second), origin="editor", transaction_id=tx)
    assert handler.process_script_worker() == 2
    handler._drain_script_results()

    assert attempted == [str(first.resolve()), str(second.resolve())]
    assert handler._script_change_collector.last_known_good(str(first)) is None
    assert handler._script_change_collector.last_known_good(str(second)) is None
    assert handler._script_change_collector.pending_count == 0


def test_edit_dependency_closure_uses_shared_stable_owner_batch(monkeypatch, tmp_path):
    from Infernux.engine.play_mode import PlayModeManager

    first = tmp_path / "first_edit.py"
    second = tmp_path / "second_edit.py"
    first.write_text("value = 1\n", encoding="utf-8")
    second.write_text("value = 2\n", encoding="utf-8")
    database = _AssetDatabaseProbe()
    database.guid_by_path[str(first.resolve())] = "first-edit-guid"
    database.guid_by_path[str(second.resolve())] = "second-edit-guid"
    handler = ResourceChangeHandler(_EngineProbe(database))
    transaction_id = handler.begin_script_transaction((str(first), str(second)))

    class _Batch:
        committed = False

    class _EditOwner:
        is_playing = False
        is_paused = False

        def __init__(self):
            self.prepared = []
            self.rolled_back = []
            self.finalized = []

        def prepare_script_reload_batch(self, revisions):
            self.prepared.append(tuple(revisions))
            return _Batch()

        def commit_script_reload_batch(self, batch):
            batch.committed = True
            return SimpleNamespace(success=True)

        def rollback_script_reload_batch(self, batch):
            self.rolled_back.append(batch)

        def finalize_script_reload_batch(self, batch):
            self.finalized.append(batch)

    owner = _EditOwner()
    monkeypatch.setattr(
        PlayModeManager,
        "instance",
        classmethod(lambda _cls: owner),
    )

    handler._check_script(str(first), origin="editor", transaction_id=transaction_id)
    handler._check_script(str(second), origin="editor", transaction_id=transaction_id)
    assert handler.process_script_worker() == 2
    handler._drain_script_results()

    assert len(owner.prepared) == 1
    assert tuple(item.file_path for item in owner.prepared[0]) == (
        str(first.resolve()),
        str(second.resolve()),
    )
    assert owner.rolled_back == []
    assert len(owner.finalized) == 1
    assert handler._script_change_collector.last_known_good(str(first)) is not None
    assert handler._script_change_collector.last_known_good(str(second)) is not None


def test_edit_dependency_batch_failure_rolls_back_once_and_keeps_lkg_empty(
    monkeypatch,
    tmp_path,
):
    from Infernux.engine.play_mode import PlayModeManager

    first = tmp_path / "first_edit_failure.py"
    second = tmp_path / "second_edit_failure.py"
    first.write_text("value = 1\n", encoding="utf-8")
    second.write_text("value = 2\n", encoding="utf-8")
    database = _AssetDatabaseProbe()
    database.guid_by_path[str(first.resolve())] = "first-edit-failure-guid"
    database.guid_by_path[str(second.resolve())] = "second-edit-failure-guid"
    handler = ResourceChangeHandler(_EngineProbe(database))
    transaction_id = handler.begin_script_transaction((str(first), str(second)))

    class _Batch:
        committed = False

    class _FailingEditOwner:
        is_playing = False
        is_paused = False

        def __init__(self):
            self.batch = _Batch()
            self.rollback_count = 0

        def prepare_script_reload_batch(self, _revisions):
            return self.batch

        def commit_script_reload_batch(self, _batch):
            return SimpleNamespace(success=False, error="second Edit member rejected")

        def rollback_script_reload_batch(self, batch):
            assert batch is self.batch
            self.rollback_count += 1

    owner = _FailingEditOwner()
    monkeypatch.setattr(
        PlayModeManager,
        "instance",
        classmethod(lambda _cls: owner),
    )

    handler._check_script(str(first), origin="editor", transaction_id=transaction_id)
    handler._check_script(str(second), origin="editor", transaction_id=transaction_id)
    assert handler.process_script_worker() == 2
    handler._drain_script_results()

    assert owner.rollback_count == 1
    assert handler._script_change_collector.last_known_good(str(first)) is None
    assert handler._script_change_collector.last_known_good(str(second)) is None


def test_superseded_old_transaction_discards_only_surviving_members(
    tmp_path,
):
    first = tmp_path / "first.py"
    second = tmp_path / "second.py"
    first.write_text("value = 'old-first'\n", encoding="utf-8")
    second.write_text("value = 'old-second'\n", encoding="utf-8")
    handler = ResourceChangeHandler(_EngineProbe(_AssetDatabaseProbe()))
    old_tx = handler.begin_script_transaction((str(first), str(second)))
    new_tx = handler.begin_script_transaction((str(first),))

    handler._check_script(str(first), origin="editor", transaction_id=old_tx)
    handler._check_script(str(second), origin="editor", transaction_id=old_tx)
    assert handler.process_script_worker(max_items=1) == 1
    handler._drain_script_results()

    first.write_text("value = 'new-first'\n", encoding="utf-8")
    handler._check_script(
        str(first),
        origin="editor",
        transaction_id=new_tx,
        force=True,
    )

    # Finish only the old transaction's second member.  The new generation
    # remains pending while the old transaction is pruned.
    assert handler.process_script_worker(max_items=1) == 1
    handler._drain_script_results()
    assert handler._script_change_collector.pending_count == 1
    assert (
        handler._script_change_collector.latest(str(first)).source.splitlines()
        == [b"value = 'new-first'"]
    )
    assert handler._script_change_collector.last_known_good(str(second)) is None


def test_graph_commit_failure_does_not_rollback_uncommitted_graph_stage(
    monkeypatch, tmp_path
):
    assets = tmp_path / "Assets"
    assets.mkdir()
    script = assets / "controller.py"
    script.write_text("value = 1\n", encoding="utf-8")
    handler = ResourceChangeHandler(
        _EngineProbe(_AssetDatabaseProbe()), project_path=str(tmp_path)
    )
    handler.dependency_graph.index_assets()
    monkeypatch.setattr(
        handler,
        "_publish_valid_script",
        lambda _path, **_kwargs: True,
    )

    rollback_calls = []
    graph = handler.dependency_graph
    monkeypatch.setattr(
        graph,
        "commit_transaction",
        lambda _stage: (_ for _ in ()).throw(RuntimeError("graph commit failed")),
    )
    monkeypatch.setattr(
        graph,
        "rollback_transaction",
        lambda stage: rollback_calls.append(stage),
    )
    errors = []
    monkeypatch.setattr(
        Debug,
        "log_error",
        lambda message, *args, **kwargs: errors.append(str(message)),
    )

    handler._check_script(str(script), origin="editor")
    assert handler.process_script_worker() == 1
    handler._drain_script_results()

    assert rollback_calls == []
    assert not any("Script dependency graph rollback failed" in message for message in errors)


def test_lkg_commit_failure_rolls_back_live_and_committed_dependency_graph(
    monkeypatch,
    tmp_path,
):
    from Infernux.engine.play_mode import PlayModeManager

    assets = tmp_path / "Assets"
    assets.mkdir()
    script = assets / "controller.py"
    script.write_text("value = 1\n", encoding="utf-8")
    database = _AssetDatabaseProbe()
    database.guid_by_path[str(script.resolve())] = "lkg-rollback-guid"
    handler = ResourceChangeHandler(_EngineProbe(database), project_path=str(tmp_path))
    handler.dependency_graph.index_assets()
    before_record = handler.dependency_graph.module_for_path(str(script))
    assert before_record is not None
    before_hash = before_record.source_hash
    script.write_text("value = 2\n", encoding="utf-8")

    class _Batch:
        committed = False

    class _Owner:
        is_playing = True
        is_paused = False

        def __init__(self):
            self.batch = _Batch()
            self.commit_count = 0
            self.rollback_count = 0
            self.finalize_count = 0

        def prepare_script_reload_batch(self, _revisions):
            return self.batch

        def commit_script_reload_batch(self, batch):
            assert batch is self.batch
            self.commit_count += 1
            batch.committed = True
            return SimpleNamespace(success=True)

        def rollback_script_reload_batch(self, batch):
            assert batch is self.batch
            self.rollback_count += 1
            batch.committed = False

        def finalize_script_reload_batch(self, _batch):
            self.finalize_count += 1

    owner = _Owner()
    monkeypatch.setattr(
        PlayModeManager,
        "instance",
        classmethod(lambda _cls: owner),
    )
    monkeypatch.setattr(
        handler._script_change_collector,
        "commit_published_batch",
        lambda *_args, **_kwargs: False,
    )

    handler._check_script(str(script), origin="editor")
    assert handler.process_script_worker() == 1
    handler._drain_script_results()

    assert owner.commit_count == 1
    assert owner.rollback_count == 1
    assert owner.finalize_count == 0
    assert handler._script_change_collector.last_known_good(str(script)) is None
    after_record = handler.dependency_graph.module_for_path(str(script))
    assert after_record is not None
    assert after_record.source_hash == before_hash


def test_durable_lkg_retries_finalize_without_republishing_or_rollback(
    monkeypatch,
    tmp_path,
):
    from Infernux.engine.play_mode import PlayModeManager

    script = tmp_path / "durable_finalize_retry.py"
    script.write_text("value = 1\n", encoding="utf-8")
    database = _AssetDatabaseProbe()
    database.guid_by_path[str(script.resolve())] = "durable-finalize-guid"
    handler = ResourceChangeHandler(_EngineProbe(database))

    class _Batch:
        committed = False

    class _Owner:
        is_playing = True
        is_paused = False

        def __init__(self):
            self.batch = _Batch()
            self.commit_count = 0
            self.rollback_count = 0
            self.finalize_count = 0

        def prepare_script_reload_batch(self, _revisions):
            return self.batch

        def commit_script_reload_batch(self, batch):
            assert batch is self.batch
            self.commit_count += 1
            batch.committed = True
            return SimpleNamespace(success=True)

        def rollback_script_reload_batch(self, _batch):
            self.rollback_count += 1

        def finalize_script_reload_batch(self, batch):
            assert batch is self.batch
            self.finalize_count += 1
            if self.finalize_count == 1:
                raise RuntimeError("simulated native finalize interruption")

    owner = _Owner()
    monkeypatch.setattr(
        PlayModeManager,
        "instance",
        classmethod(lambda _cls: owner),
    )

    handler._check_script(str(script), origin="editor")
    assert handler.process_script_worker() == 1
    handler._drain_script_results()

    assert owner.commit_count == 1
    assert owner.rollback_count == 0
    assert owner.finalize_count == 1
    assert handler._script_change_collector.last_known_good(str(script)) is not None
    assert len(handler._script_transactions) == 1

    handler._drain_script_results()

    assert owner.commit_count == 1
    assert owner.rollback_count == 0
    assert owner.finalize_count == 2
    assert handler._script_transactions == {}
