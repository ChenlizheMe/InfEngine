import pytest

from Infernux.core.assets import AssetManager
from Infernux.engine.play_mode import PlayModeManager, ScriptReloadOutcome
from Infernux.engine.resources_manager import (
    ResourceChangeHandler,
    ResourcesManager,
    _AssetImportNotReady,
)


class _Database:
    def __init__(self):
        self.guids = {}

    def get_guid_from_path(self, path):
        return self.guids.get(str(path), "move-guid")


class _Engine:
    def __init__(self, database):
        self.database = database

    def get_asset_database(self):
        return self.database


class _Batch:
    committed = True
    rolled_back = False


class _PlayModeProbe:
    def __init__(self, *, playing=False):
        self.is_playing = playing
        self.is_paused = False
        self.revisions = []

    def reload_components_from_script_result(self, _path, *, source, code=None):
        del source, code
        return ScriptReloadOutcome(True, False, 0)

    def prepare_script_reload_batch(self, revisions):
        self.revisions = tuple(revisions)
        return _Batch()

    def commit_script_reload_batch(self, _batch):
        return ScriptReloadOutcome(True, False, 0)

    def rollback_script_reload_batch(self, _batch):
        return None

    def finalize_script_reload_batch(self, _batch):
        return None

    def prepare_edit_script_reload_batch(self, revisions):
        self.revisions = tuple(revisions)
        return _Batch()

    def commit_edit_script_reload_batch(self, _batch):
        return 0

    def rollback_edit_script_reload_batch(self, _batch):
        return None


def _manager_and_handler(tmp_path):
    assets = tmp_path / "Assets"
    assets.mkdir()
    database = _Database()
    manager = ResourcesManager(str(tmp_path), _Engine(database))
    handler = ResourceChangeHandler(_Engine(database), project_path=str(tmp_path))
    manager._event_handler = handler
    return manager, handler


def _patch_move(monkeypatch, *, result=True):
    calls = []

    def move(_cls, old_path, new_path, **_kwargs):
        calls.append((old_path, new_path))
        return result

    monkeypatch.setattr(AssetManager, "move_asset", classmethod(move))
    return calls


def test_valid_script_move_stages_old_removal_and_notifies_once(monkeypatch, tmp_path):
    manager, handler = _manager_and_handler(tmp_path)
    old_path = tmp_path / "Assets" / "Mover.py"
    new_path = tmp_path / "Assets" / "Moved.py"
    old_path.write_text("value = 1\n", encoding="utf-8")
    handler.dependency_graph.index_assets()
    old_path.rename(new_path)
    events = []
    manager.register_script_catalog_callback(lambda path, event: events.append((path, event)))
    probe = _PlayModeProbe()
    monkeypatch.setattr(
        PlayModeManager,
        "instance",
        classmethod(lambda _cls: probe),
    )
    unregister_calls = []
    monkeypatch.setattr(
        "Infernux.components.registry.unregister_component_script",
        lambda path: unregister_calls.append(path),
    )
    _patch_move(monkeypatch)

    handler._commit_moved(str(old_path), str(new_path))
    assert unregister_calls == []
    assert probe.revisions == []
    handler.process_pending_reloads(force=True)
    handler.process_pending_reloads(force=True)

    assert probe.revisions[0].retire_script_paths == (str(old_path),)
    assert handler.dependency_graph.module_for_path(str(old_path)) is None
    assert handler.dependency_graph.module_for_path(str(new_path)) is not None
    assert events == [(str(new_path), "moved")]


def test_invalid_destination_does_not_touch_registry_or_graph(monkeypatch, tmp_path):
    _manager, handler = _manager_and_handler(tmp_path)
    old_path = tmp_path / "Assets" / "Mover.py"
    new_path = tmp_path / "Assets" / "Missing.py"
    old_path.write_text("value = 1\n", encoding="utf-8")
    handler.dependency_graph.index_assets()
    calls = _patch_move(monkeypatch)
    unregister_calls = []
    monkeypatch.setattr(
        "Infernux.components.registry.unregister_component_script",
        lambda path: unregister_calls.append(path),
    )

    with pytest.raises(_AssetImportNotReady, match="moved file is not ready"):
        handler._commit_moved(str(old_path), str(new_path))

    assert calls == []
    assert unregister_calls == []
    assert handler.dependency_graph.module_for_path(str(old_path)) is not None
    assert handler.dependency_graph.module_for_path(str(new_path)) is None


def test_failed_moved_candidate_keeps_old_graph_and_lkg(monkeypatch, tmp_path):
    _manager, handler = _manager_and_handler(tmp_path)
    old_path = tmp_path / "Assets" / "Mover.py"
    new_path = tmp_path / "Assets" / "Moved.py"
    old_path.write_text("value = 1\n", encoding="utf-8")
    handler.dependency_graph.index_assets()
    handler._check_script(str(old_path), origin="editor")
    handler.process_pending_reloads(force=True)
    old_lkg = handler._script_change_collector.last_known_good(str(old_path))
    assert old_lkg is not None

    old_path.rename(new_path)
    new_path.write_text("def broken(:\n", encoding="utf-8")
    probe = _PlayModeProbe()
    monkeypatch.setattr(
        PlayModeManager,
        "instance",
        classmethod(lambda _cls: probe),
    )
    _patch_move(monkeypatch)

    handler._commit_moved(str(old_path), str(new_path))
    handler.process_pending_reloads(force=True)

    assert handler.dependency_graph.module_for_path(str(old_path)) is not None
    assert handler.dependency_graph.module_for_path(str(new_path)) is None
    assert handler._script_change_collector.last_known_good(str(old_path)) == old_lkg


@pytest.mark.parametrize("playing", [False, True])
def test_move_uses_existing_edit_or_play_reload_batch(monkeypatch, tmp_path, playing):
    _manager, handler = _manager_and_handler(tmp_path)
    old_path = tmp_path / "Assets" / "Mover.py"
    new_path = tmp_path / "Assets" / "Moved.py"
    new_path.write_text("value = 2\n", encoding="utf-8")
    probe = _PlayModeProbe(playing=playing)
    monkeypatch.setattr(
        PlayModeManager,
        "instance",
        classmethod(lambda _cls: probe),
    )

    handler._submit_moved_script(str(old_path), str(new_path), origin="editor")
    handler.process_pending_reloads(force=True)
    handler.process_pending_reloads(force=True)

    assert probe.revisions
    assert probe.revisions[0].retire_script_paths == (str(old_path),)


def test_graph_move_transaction_rolls_back_as_one_state(tmp_path):
    assets = tmp_path / "Assets"
    assets.mkdir()
    old_path = assets / "Mover.py"
    new_path = assets / "Moved.py"
    old_path.write_text("value = 1\n", encoding="utf-8")

    from Infernux.engine.script_dependency_graph import ScriptDependencyGraph

    graph = ScriptDependencyGraph(str(tmp_path))
    graph.index_assets()
    new_path.write_text("value = 2\n", encoding="utf-8")
    staged = graph.stage_transaction(
        {str(new_path): new_path.read_bytes()},
        removals=(str(old_path),),
    )
    graph.commit_transaction(staged)
    assert graph.module_for_path(str(old_path)) is None
    assert graph.module_for_path(str(new_path)) is not None

    graph.rollback_transaction(staged)
    assert graph.module_for_path(str(old_path)) is not None
    assert graph.module_for_path(str(new_path)) is None
