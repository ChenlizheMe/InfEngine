from __future__ import annotations

from contextlib import contextmanager
import threading

import pytest
from types import SimpleNamespace

from Infernux.engine.interaction import (
    DirectoryNavigationHistory,
    EditorActionJournal,
    EditorCommand,
    EditorCommandRegistry,
    FocusService,
    KeyChord,
    ShortcutBinding,
    ShortcutEvent,
    ShortcutRouter,
    CommandContext,
    CommandSource,
    FocusSnapshot,
    SelectionSnapshot,
)
from Infernux.engine.undo import UndoCommand, UndoManager
from Infernux.input import ImeInputState


@pytest.fixture(autouse=True)
def _restore_undo_manager():
    previous = UndoManager._instance
    yield
    UndoManager._instance = previous


@contextmanager
def _local_undo_manager(journal):
    """Keep this stress chain out of the process-wide editor singleton."""
    previous = UndoManager._instance
    manager = UndoManager(journal)
    UndoManager._instance = previous
    try:
        yield manager
    finally:
        UndoManager._instance = previous


def test_directory_back_forward_is_atomic_and_clears_forward_on_new_branch():
    history = DirectoryNavigationHistory(max_entries=3)
    current = ["root"]

    def apply(path: str):
        if path == "missing":
            return False
        current[0] = path
        return True

    history.sync(current[0])
    assert history.navigate("a", apply)
    assert history.navigate("b", apply)
    assert history.navigate("c", apply)
    assert history.snapshot.back_paths == ("root", "a", "b")

    assert not history.navigate("missing", apply)
    assert current[0] == "c"
    assert history.snapshot.current_path == "c"
    assert history.snapshot.forward_paths == ()

    assert history.back(apply)
    assert history.back(apply)
    assert current[0] == "a"
    assert history.snapshot.forward_paths == ("c", "b")
    assert history.forward(apply)
    assert current[0] == "b"
    assert history.navigate("branch", apply)
    assert not history.can_go_forward


def test_external_path_change_invalidates_stale_directory_history():
    history = DirectoryNavigationHistory()
    history.sync("root")
    assert history.navigate("child", lambda _path: True)
    history.sync("undo-restored")
    assert history.snapshot.back_paths == ()
    assert history.snapshot.forward_paths == ()


def test_project_panel_commands_drive_back_and_forward_history(tmp_path):
    from Infernux.engine.ui.core_panel_interactions import project_panel_interaction

    manager = UndoManager(EditorActionJournal())
    history = DirectoryNavigationHistory()
    root = str(tmp_path / "root")
    path_a = str(tmp_path / "a")
    path_b = str(tmp_path / "b")
    current = [root]
    panel = SimpleNamespace(
        begin_rename_selected_asset=lambda _path="": True,
        can_rename_selected_asset=lambda _path="": True,
        can_navigate_to_path=lambda path: bool(path),
        get_current_path=lambda: current[0],
        set_current_path=lambda path: current.__setitem__(0, path) is None,
        get_folder_expanded_paths=lambda: (),
        set_folder_expanded_paths=lambda _paths: None,
        get_model_expanded_paths=lambda: (),
        set_model_expanded_paths=lambda _paths: None,
        request_search_focus=lambda: None,
    )
    descriptor = project_panel_interaction(
        object(),
        object(),
        directory_history=history,
    )
    adapter = descriptor.adapter_factory(panel)
    context = lambda payload: CommandContext(
        CommandSource.POINTER,
        FocusSnapshot(active_panel_id="project"),
        SelectionSnapshot(),
        payload=payload,
    )

    navigate = adapter.handler("project.navigate_directory")
    back = adapter.handler("project.navigate_back")
    forward = adapter.handler("project.navigate_forward")
    assert navigate is not None and back is not None and forward is not None
    assert navigate.execute(context({"target_id": path_a}))
    assert navigate.execute(context({"target_id": path_b}))
    assert back.can_execute(context({}))
    assert back.execute(context({}))
    assert current[0] == path_a
    assert forward.execute(context({}))
    assert current[0] == path_b
    manager.action_journal.validate()


def test_ime_commit_deduplicates_only_tagged_replays_and_keeps_repeated_chinese():
    state = ImeInputState()
    state.begin_composition("composition-1")

    assert state.commit("中", commit_id="commit-1") == "中"
    assert state.commit("中", commit_id="commit-1") == ""
    assert state.commit("中", commit_id="commit-2") == "中"
    assert state.commit("哈哈") == "哈哈"

    assert not state.accept_key_down("A", repeat=True)
    assert not state.accept_key_down("A", text_input_active=True)
    assert state.accept_key_down("A", event_id="key-1")
    assert not state.accept_key_down("A", event_id="key-1")


def test_shortcut_router_treats_duplicate_event_delivery_as_one_edge():
    focus = FocusService()
    commands = EditorCommandRegistry(focus=focus)
    router = ShortcutRouter(commands, focus)
    calls = []
    commands.register(EditorCommand("test.action", lambda _context: calls.append(True)))
    router.register(
        ShortcutBinding("test.action", KeyChord.parse("Ctrl+S"), binding_id="test")
    )

    event = ShortcutEvent(KeyChord.parse("Ctrl+S"), event_id="edge-1")
    assert router.route(event).command_id == "test.action"
    assert router.route(event).status.value == "no_op"
    assert calls == [True]


class _LongAction(UndoCommand):
    marks_dirty = False

    def __init__(self, state: list[int], old: int, new: int):
        super().__init__(f"Set {new}")
        self._state = state
        self._old = old
        self._new = new
        self._owner_thread = threading.get_ident()

    def _assert_owner_thread(self) -> None:
        assert threading.get_ident() == self._owner_thread

    def execute(self) -> None:
        self._assert_owner_thread()
        self._state[0] = self._new

    def undo(self) -> None:
        self._assert_owner_thread()
        self._state[0] = self._old


def test_long_action_journal_undo_redo_and_branch_replacement_stay_consistent():
    with _local_undo_manager(EditorActionJournal(max_entries=64)) as manager:
        manager.MAX_STACK_DEPTH = 64
        state = [0]

        for value in range(1, 241):
            assert manager.execute(_LongAction(state, value - 1, value))
            manager.action_journal.validate()

        assert state[0] == 240
        assert manager.action_journal.cursor == 64
        assert len(manager.action_journal.entries) == 64

        undo_count = 0
        while manager.can_undo:
            manager.undo()
            undo_count += 1
            manager.action_journal.validate()
        assert undo_count == 64
        assert state[0] == 176
        assert manager.can_redo

        redo_count = 0
        while manager.can_redo:
            manager.redo()
            redo_count += 1
            manager.action_journal.validate()
        assert redo_count == 64
        assert state[0] == 240

        for _ in range(17):
            manager.undo()
        assert manager.can_redo
        assert manager.execute(_LongAction(state, 223, 999))
        manager.action_journal.validate()
        assert not manager.can_redo
        assert state[0] == 999


def test_action_journal_cannot_be_cleared_inside_a_user_action():
    manager = UndoManager()
    with pytest.raises(RuntimeError, match="active user action"):
        with manager.user_action("still publishing"):
            manager.clear()


def test_interaction_services_survive_long_mixed_navigation_ime_and_journal_use():
    history = DirectoryNavigationHistory(max_entries=128)
    current = ["root"]

    def apply(path: str) -> bool:
        current[0] = path
        return True

    history.sync(current[0])
    for index in range(1024):
        assert history.navigate(f"folder-{index}", apply)
    assert len(history.snapshot.back_paths) == 128
    for _ in range(96):
        assert history.back(apply)
    assert len(history.snapshot.forward_paths) == 96
    assert history.navigate("branch-after-long-history", apply)
    assert not history.can_go_forward

    ime = ImeInputState()
    ime.begin_composition("long-composition")
    committed = []
    for index in range(512):
        commit_id = f"commit-{index}"
        committed.append(ime.commit("中", commit_id=commit_id))
        assert ime.commit("中", commit_id=commit_id) == ""
    assert "".join(committed) == "中" * 512

    with _local_undo_manager(EditorActionJournal(max_entries=256)) as manager:
        manager.MAX_STACK_DEPTH = 256
        state = [0]
        for value in range(1, 4097):
            assert manager.execute(_LongAction(state, value - 1, value))
            if value % 127 == 0:
                manager.action_journal.validate()

        assert state[0] == 4096
        assert len(manager.action_journal.entries) == 256
        for _ in range(128):
            manager.undo()
        manager.action_journal.validate()
        assert state[0] == 3968
        for _ in range(64):
            manager.redo()
        manager.action_journal.validate()
        assert state[0] == 4032
        assert manager.execute(_LongAction(state, 4032, 9001))
        manager.action_journal.validate()
        assert state[0] == 9001
        assert not manager.can_redo
