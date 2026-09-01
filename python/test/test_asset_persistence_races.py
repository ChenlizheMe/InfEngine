from __future__ import annotations

from types import SimpleNamespace

import pytest

from Infernux.core.assets import (
    AssetManager,
    _AssetRevisionState,
    _PendingDocumentWrite,
)
from Infernux.engine.interaction import DocumentKey, DocumentKind, DocumentRegistry
from Infernux.engine.path_utils import path_key
from Infernux.engine.resources_manager import ResourceChangeHandler, _AssetImportNotReady


@pytest.fixture(autouse=True)
def _clean_asset_write_ledger():
    maps = (
        AssetManager._asset_revision_states,
        AssetManager._material_save_snapshots,
        AssetManager._render_effect_save_snapshots,
        AssetManager._document_save_expected_states,
        AssetManager._document_write_metadata,
        AssetManager._pending_document_write_records,
        AssetManager._self_write_commits,
    )
    for mapping in maps:
        mapping.clear()
    yield
    for mapping in maps:
        mapping.clear()


class _Ticket:
    def __init__(self, *, status="succeeded", committed_file_state=None, complete=True):
        self.status = status
        self.committed_file_state = committed_file_state
        self.is_complete = complete
        self.error = ""


def test_material_save_does_not_invalidate_live_cache_before_async_commit(monkeypatch, tmp_path):
    path = str(tmp_path / "live.mat")
    saved = []
    submitted = _Ticket(committed_file_state=object())

    class _Material:
        file_path = path

        @staticmethod
        def serialize():
            return '{"name":"B"}'

    monkeypatch.setattr(
        AssetManager,
        "note_asset_edit",
        classmethod(lambda _cls, *args, **kwargs: None),
    )
    monkeypatch.setattr(
        AssetManager,
        "_submit_document_snapshot",
        classmethod(lambda _cls, *args, **kwargs: submitted),
    )
    monkeypatch.setattr(
        AssetManager,
        "on_material_saved",
        classmethod(lambda _cls, value: saved.append(value)),
    )

    assert AssetManager._save_material_resource(_Material()) is submitted
    assert saved == []


def test_material_save_accepts_controller_owned_path(monkeypatch, tmp_path):
    path = str(tmp_path / "controller-owned.mat")
    submitted = _Ticket(committed_file_state=object())

    class _MaterialWithoutPath:
        @staticmethod
        def serialize():
            return '{"name":"ControllerOwned"}'

    captured = []
    monkeypatch.setattr(
        AssetManager,
        "note_asset_edit",
        classmethod(lambda _cls, *args, **kwargs: None),
    )
    monkeypatch.setattr(
        AssetManager,
        "_submit_document_snapshot",
        classmethod(
            lambda _cls, file_path, snapshot, **kwargs: captured.append(
                (file_path, snapshot)
            )
            or submitted
        ),
    )

    assert (
        AssetManager._save_material_resource(_MaterialWithoutPath(), path)
        is submitted
    )
    assert captured == [(path, '{"name":"ControllerOwned"}')]


def test_material_save_queue_passes_its_stable_path_to_strategy(monkeypatch, tmp_path):
    path = str(tmp_path / "queued.mat")
    resource = object()
    calls = []
    AssetManager._ensure_execution_strategies()
    previous = AssetManager._save_handlers.get("material")
    AssetManager._scheduled_saves.clear()
    AssetManager._save_handlers["material"] = (
        lambda value, target: calls.append((value, target)) or True
    )
    try:
        AssetManager.schedule_asset_save("material", path, resource, debounce_sec=0.0)
        assert AssetManager.flush_scheduled_saves(force=True) is True
        assert calls == [(resource, path)]
    finally:
        AssetManager._scheduled_saves.clear()
        if previous is None:
            AssetManager._save_handlers.pop("material", None)
        else:
            AssetManager._save_handlers["material"] = previous


def test_only_latest_material_write_callback_can_publish_a_to_b_to_c(monkeypatch, tmp_path):
    path = str(tmp_path / "rapid.mat")
    normalized = path_key(path)
    callbacks = []
    state = _AssetRevisionState(
        edit_revision=3,
        requested_write_revision=3,
        content_token="C",
    )
    AssetManager._asset_revision_states[normalized] = state

    records = []
    for index, token in enumerate(("A", "B", "C"), start=1):
        ticket = _Ticket(committed_file_state=SimpleNamespace(exists=True, size=index, content_hash=index))
        records.append(
            _PendingDocumentWrite(
                path=normalized,
                ticket=ticket,
                edit_revision=index,
                requested_write_revision=index,
                commit_token=f"commit-{token}",
                snapshot=token,
                content_token=token,
                callback=lambda status, token=token: callbacks.append((token, status)),
            )
        )
    AssetManager._pending_document_write_records[normalized] = records

    AssetManager.poll_pending_asset_writes()

    assert callbacks == [("C", "succeeded")]
    assert state.content_token == "C"
    assert normalized not in AssetManager._pending_document_write_records


def test_self_write_watcher_is_acknowledged_by_committed_fingerprint(monkeypatch, tmp_path):
    path = str(tmp_path / "self.mat")
    normalized = path_key(path)
    ticket = _Ticket(committed_file_state="self")
    record = _PendingDocumentWrite(
        path=normalized,
        ticket=ticket,
        edit_revision=1,
        requested_write_revision=1,
        commit_token="self-commit",
        snapshot="B",
        content_token="B",
    )
    AssetManager._pending_document_write_records[normalized] = [record]
    monkeypatch.setattr(
        AssetManager,
        "_file_state_matches",
        classmethod(lambda _cls, _path, expected: expected == "self"),
    )

    assert AssetManager.local_write_event_state(path) == "ack"


def test_synchronous_local_commit_is_acknowledged_without_time_window(tmp_path):
    path = tmp_path / "scene.scene"
    path.write_text("editor", encoding="utf-8")
    state = AssetManager.register_local_commit(
        str(path),
        commit_token="scene-save-1",
        content_token="editor-token",
        edit_revision=4,
        document_id="scene-document",
    )

    assert state is not None
    assert AssetManager.local_write_event_state(str(path)) == "ack"
    assert AssetManager.local_write_event_state(str(path)) == "ack"


def test_external_rewrite_after_local_commit_is_not_swallowed(tmp_path):
    path = tmp_path / "scene.scene"
    path.write_text("editor", encoding="utf-8")
    AssetManager.register_local_commit(
        str(path),
        commit_token="scene-save-1",
        content_token="editor-token",
    )
    path.write_text("external", encoding="utf-8")

    assert AssetManager.local_write_event_state(str(path)) == "none"
    assert AssetManager.is_watcher_echo_suppressed("modified", str(path)) is False


def test_clean_scene_watcher_event_with_unchanged_identity_is_ignored(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "Course.scene"
    path.write_text("scene", encoding="utf-8")

    class _Controller:
        def __init__(self):
            self.reloads = 0

        def reload_from_resource(self, **_kwargs):
            self.reloads += 1
            return True

    controller = _Controller()
    documents = DocumentRegistry()
    document = documents.create(
        DocumentKind.SCENE,
        "Course",
        key=DocumentKey.resource(DocumentKind.SCENE, str(path)),
        resource_path=str(path),
        controller=controller,
    )
    monkeypatch.setattr(DocumentRegistry, "instance", classmethod(lambda _cls: documents))

    class _Database:
        def contains_path(self, value):
            return value == str(path.resolve())

        def get_guid_from_path(self, value):
            return "scene-guid" if value == str(path.resolve()) else ""

    class _Engine:
        def get_asset_database(self):
            return _Database()

    calls = []
    monkeypatch.setattr(
        AssetManager,
        "reimport_asset",
        classmethod(lambda _cls, *args, **kwargs: calls.append(args) or SimpleNamespace(succeeded=True)),
    )

    ResourceChangeHandler(_Engine())._commit_modified(str(path.resolve()))

    assert calls == []
    assert controller.reloads == 0
    assert document.state.value == "ready"
    assert not document.is_dirty


def test_real_external_scene_change_reimports_and_reloads_current_disk(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "Course.scene"
    path.write_text("disk-a", encoding="utf-8")

    class _Resource:
        value = "memory-a"

    resource = _Resource()

    class _Controller:
        def reload_from_resource(self, *, resource_path, **_kwargs):
            with open(resource_path, encoding="utf-8") as stream:
                resource.value = stream.read()
            return True

    documents = DocumentRegistry()
    document = documents.create(
        DocumentKind.SCENE,
        "Course",
        key=DocumentKey.resource(DocumentKind.SCENE, str(path)),
        resource_path=str(path),
        controller=_Controller(),
    )
    monkeypatch.setattr(DocumentRegistry, "instance", classmethod(lambda _cls: documents))

    class _Database:
        def contains_path(self, value):
            return value == str(path.resolve())

        def get_guid_from_path(self, value):
            return "scene-guid" if value == str(path.resolve()) else ""

    class _Engine:
        def get_asset_database(self):
            return _Database()

    path.write_text("disk-b", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        AssetManager,
        "reimport_asset",
        classmethod(
            lambda _cls, *args, **kwargs: calls.append(args)
            or SimpleNamespace(succeeded=True)
        ),
    )
    monkeypatch.setattr(
        AssetManager,
        "_publish_asset_content_change",
        classmethod(lambda _cls, *args, **kwargs: None),
    )

    ResourceChangeHandler(_Engine())._commit_modified(str(path.resolve()))

    assert len(calls) == 1
    assert resource.value == "disk-b"
    assert document.state.value == "ready"
    assert not document.is_dirty


def test_dirty_scene_external_change_preserves_memory_and_enters_conflict(
    monkeypatch,
    tmp_path,
):
    path = tmp_path / "Course.scene"
    path.write_text("disk-a", encoding="utf-8")

    class _Controller:
        reloads = 0

        def reload_from_resource(self, **_kwargs):
            self.reloads += 1
            return True

    controller = _Controller()
    documents = DocumentRegistry()
    document = documents.create(
        DocumentKind.SCENE,
        "Course",
        key=DocumentKey.resource(DocumentKind.SCENE, str(path)),
        resource_path=str(path),
        controller=controller,
    )
    documents.mark_changed(document.document_id, view_id="scene_view")
    monkeypatch.setattr(DocumentRegistry, "instance", classmethod(lambda _cls: documents))

    class _Database:
        def contains_path(self, value):
            return value == str(path.resolve())

        def get_guid_from_path(self, value):
            return "scene-guid" if value == str(path.resolve()) else ""

    class _Engine:
        def get_asset_database(self):
            return _Database()

    path.write_text("disk-b", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        AssetManager,
        "reimport_asset",
        classmethod(lambda _cls, *args, **kwargs: calls.append(args)),
    )

    ResourceChangeHandler(_Engine())._commit_modified(str(path.resolve()))

    assert calls == []
    assert controller.reloads == 0
    assert document.state.value == "conflict"
    assert document.is_dirty


def test_reload_external_reads_current_disk_content_and_cleans_document(tmp_path, monkeypatch):
    path = tmp_path / "graph.json"
    path.write_text('{"value": 2}', encoding="utf-8")

    class _Resource:
        def __init__(self):
            self.value = 0

        def deserialize_document(self, document):
            self.value = int(document["value"])
            return True

    resource = _Resource()

    class _Controller:
        def reload_from_resource(self, *, document_id, resource_path):
            assert document_id == document.document_id
            with open(resource_path, "r", encoding="utf-8") as stream:
                import json

                return resource.deserialize_document(json.load(stream))

    documents = DocumentRegistry()
    monkeypatch.setattr(DocumentRegistry, "instance", classmethod(lambda _cls: documents))
    document = documents.create(
        DocumentKind.GENERIC,
        "Graph",
        key=DocumentKey.resource(DocumentKind.GENERIC, str(path)),
        resource_path=str(path),
        controller=_Controller(),
        revision=2,
        saved_revision=1,
    )
    documents.mark_conflict(document.document_id)

    result = documents.request_reload_external(document.document_id)

    assert result.status.value == "applied"
    assert resource.value == 2
    assert document.is_dirty is False
    assert document.state.value == "ready"


def test_reload_without_controller_is_explicitly_rejected(tmp_path):
    path = tmp_path / "missing-controller.json"
    path.write_text("{}", encoding="utf-8")
    documents = DocumentRegistry()
    document = documents.create(
        DocumentKind.GENERIC,
        "Unbound",
        key=DocumentKey.resource(DocumentKind.GENERIC, str(path)),
        resource_path=str(path),
        revision=1,
        saved_revision=0,
    )
    documents.mark_conflict(document.document_id)

    result = documents.request_reload_external(document.document_id)

    assert result.status.value == "rejected"
    assert "cannot reload" in result.message


def test_pending_self_write_is_deferred_without_marking_conflict(monkeypatch, tmp_path):
    path = tmp_path / "pending.mat"
    path.write_text("new", encoding="utf-8")

    class _Database:
        def get_guid_from_path(self, value):
            return "pending-guid" if value == str(path.resolve()) else ""

        def contains_path(self, value):
            return value == str(path.resolve())

    class _Engine:
        def get_asset_database(self):
            return _Database()

    class _Documents:
        def preflight_external_resource_change(self, value):
            raise AssertionError(f"pending local write was treated as external: {value}")

    monkeypatch.setattr(
        AssetManager,
        "local_write_event_state",
        classmethod(lambda _cls, _value: "pending"),
    )
    monkeypatch.setattr(
        DocumentRegistry,
        "instance",
        classmethod(lambda _cls: _Documents()),
    )
    handler = ResourceChangeHandler(_Engine())

    with pytest.raises(_AssetImportNotReady, match="local document write is still pending"):
        handler._commit_modified(str(path.resolve()))


@pytest.mark.parametrize("registered", (False, True))
def test_registered_and_unregistered_imports_share_external_publish(monkeypatch, tmp_path, registered):
    path = tmp_path / ("registered.mat" if registered else "new.mat")
    path.write_text("external", encoding="utf-8")
    resolved = str(path.resolve())

    class _Database:
        def get_guid_from_path(self, value):
            return "asset-guid" if value == resolved and registered else ""

        def contains_path(self, value):
            return value == resolved and registered

    class _Engine:
        def get_asset_database(self):
            return _Database()

    published = []

    class _Documents:
        def durable_resource_content_changed(self, value):
            return True

        def preflight_external_resource_change(self, value):
            return value == resolved

        def has_pending_external_change_preflight(self, value):
            return value == resolved

        def publish_external_resource_change(self, value):
            published.append(value)

        def fail_external_resource_change(self, value, *, message=""):
            del value, message

    result = SimpleNamespace(succeeded=True, error="")
    monkeypatch.setattr(
        AssetManager,
        "local_write_event_state",
        classmethod(lambda _cls, _value: "none"),
    )
    monkeypatch.setattr(
        AssetManager,
        "reimport_asset",
        classmethod(lambda _cls, *args, **kwargs: result),
    )
    monkeypatch.setattr(
        AssetManager,
        "import_asset",
        classmethod(lambda _cls, *args, **kwargs: result),
    )
    monkeypatch.setattr(
        DocumentRegistry,
        "instance",
        classmethod(lambda _cls: _Documents()),
    )

    ResourceChangeHandler(_Engine())._commit_modified(resolved)

    assert published == [resolved]


def test_inspector_asset_write_replaces_a_changed_target(tmp_path):
    from Infernux.core.document_store import DocumentStore, capture_document_file_state

    path = tmp_path / "world.effectgroup"
    path.write_text("before", encoding="utf-8")
    expected = capture_document_file_state(str(path))
    AssetManager.set_document_save_expected_state(str(path), expected)
    state = AssetManager._asset_revision_state(path_key(str(path)))
    state.persisted_file_state = expected
    path.write_text("extern", encoding="utf-8")

    ticket = AssetManager._submit_document_snapshot(str(path), "inspector")
    ticket.wait()

    assert ticket.status == "succeeded"
    assert path.read_text(encoding="utf-8") == "inspector"
    DocumentStore.shutdown()
