from Infernux.engine.interaction import (
    ContextRestoreStatus,
    EditorInteractionCore,
    NavigationService,
    SelectionService,
    SelectionTarget,
)
from Infernux.engine.undo import UndoManager


def _restore_context(core, snapshot, _phase):
    core.focus.apply_snapshot(snapshot.focus, record_history=False)
    core.selection.apply_snapshot(snapshot.selection, record_history=False)
    return ContextRestoreStatus.READY


def test_navigation_records_panel_focus_before_target_selection():
    previous_core = EditorInteractionCore._instance
    previous_manager = UndoManager._instance
    previous_selection = SelectionService._instance
    core = EditorInteractionCore()
    manager = UndoManager(core.action_journal)
    manager.set_context_hooks(
        core.capture_context,
        lambda snapshot, phase: _restore_context(core, snapshot, phase),
    )
    target = SelectionTarget.asset("Assets/Smoke.particlegraph")
    presented = []
    core.navigation.register(
        target.domain,
        lambda value, _request: (
            presented.append(value),
            core.focus.activate_panel("project", reason="navigate_asset"),
            True,
        )[-1],
    )
    from Infernux.engine.undo import GlobalFocusCommand, GlobalSelectionCommand

    core.focus.add_change_listener(
        lambda change: manager.record(
            GlobalFocusCommand(change.before, change.after),
            before_context=core.capture_context(
                focus=change.before,
                selection=core.selection.snapshot,
            ),
            after_context=core.capture_context(
                focus=change.after,
                selection=core.selection.snapshot,
            ),
        )
        if change.record_history and not manager.is_executing
        else None
    )
    core.selection.add_listener(
        lambda change: manager.record(
            GlobalSelectionCommand(change.before, change.after)
        )
        if change.record_history and not manager.is_executing
        else None
    )
    try:
        from Infernux.engine.interaction import SelectionDomain

        core.panels.register_selection_authority("project", (SelectionDomain.ASSET,))
        assert core.navigation.locate(target, owner_id="project")
        assert presented == [target]
        assert core.focus.snapshot.active_panel_id == "project"
        assert core.selection.snapshot.primary == target
        # Window presentation deliberately separates history. It must never
        # merge into the target mutation/navigation operation.
        assert len(manager.action_journal.entries) == 2
        assert manager.undo_description == "Change Selection"

        manager.undo()
        assert core.focus.snapshot.active_panel_id == "project"
        assert core.selection.snapshot.primary is None

        manager.undo()
        assert core.focus.snapshot.active_panel_id == ""
        assert core.selection.snapshot.primary is None

        manager.redo()
        assert core.focus.snapshot.active_panel_id == "project"
        assert core.selection.snapshot.primary is None

        manager.redo()
        assert core.focus.snapshot.active_panel_id == "project"
        assert core.selection.snapshot.primary == target
    finally:
        core.shutdown()
        EditorInteractionCore._instance = previous_core
        UndoManager._instance = previous_manager
        SelectionService._instance = previous_selection


def test_navigation_succeeds_when_target_is_already_selected():
    previous_manager = UndoManager._instance
    previous_selection = SelectionService._instance
    selection = SelectionService()
    manager = UndoManager()
    navigation = NavigationService(selection)
    target = SelectionTarget.scene_object(42)
    selection.select(target, owner_id="scene_view", record_history=False)
    navigation.register(target.domain, lambda _target, _request: True)
    try:
        assert navigation.locate(target, owner_id="inspector")
        assert selection.snapshot.primary == target
        assert selection.snapshot.owner_id == "inspector"
    finally:
        UndoManager._instance = previous_manager
        SelectionService._instance = previous_selection


def test_navigation_adapter_rejection_does_not_change_selection():
    previous_manager = UndoManager._instance
    previous_selection = SelectionService._instance
    selection = SelectionService()
    manager = UndoManager()
    navigation = NavigationService(selection)
    target = SelectionTarget.asset("Assets/Missing.mat")
    navigation.register(target.domain, lambda _target, _request: False)
    try:
        assert not navigation.locate(target, owner_id="inspector")
        assert selection.snapshot.primary is None
        assert manager.action_journal.entries == ()
    finally:
        UndoManager._instance = previous_manager
        SelectionService._instance = previous_selection


def test_navigation_fails_closed_without_action_journal():
    previous_manager = UndoManager._instance
    previous_selection = SelectionService._instance
    selection = SelectionService()
    navigation = NavigationService(selection)
    target = SelectionTarget.asset("Assets/Test.mat")
    calls = []
    navigation.register(
        target.domain,
        lambda value, _request: calls.append(value) or True,
    )
    UndoManager._instance = None
    try:
        assert not navigation.locate(target, owner_id="inspector")
        assert calls == []
        assert selection.snapshot.primary is None
    finally:
        UndoManager._instance = previous_manager
        SelectionService._instance = previous_selection


def test_bootstrap_navigation_delegates_window_presentation_to_manager():
    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin

    calls = []

    class _WindowManager:
        @staticmethod
        def open_window_from_user(panel_id, *, reason):
            calls.append((panel_id, reason))
            return object()

    bootstrap = BootstrapSelectionMixin()
    bootstrap.window_manager = _WindowManager()

    assert bootstrap._focus_navigation_panel("project")
    assert calls == [("project", "navigate_panel")]


def test_explicit_asset_navigation_changes_project_directory_as_non_dirty_view_state(
    tmp_path,
):
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import SelectionDomain
    from Infernux.engine.path_utils import lexical_path

    previous_core = EditorInteractionCore._instance
    previous_manager = UndoManager._instance
    previous_selection = SelectionService._instance
    core = EditorInteractionCore()
    manager = UndoManager(core.action_journal)
    first = tmp_path / "Assets" / "First"
    second = tmp_path / "Assets" / "Second"
    first.mkdir(parents=True)
    second.mkdir()
    asset = second / "Target.mat"
    asset.write_text("{}", encoding="utf-8")
    state = {"path": lexical_path(first)}
    panel = SimpleNamespace(
        get_current_path=lambda: state["path"],
        can_navigate_to_path=lambda path: lexical_path(path).startswith(
            lexical_path(tmp_path / "Assets")
        ),
        set_current_path=lambda path: bool(
            state.__setitem__("path", lexical_path(path)) is None
        ),
    )

    class _WindowManager:
        @staticmethod
        def open_window_from_user(panel_id, *, reason):
            assert (panel_id, reason) == ("project", "navigate_panel")
            return panel

        @staticmethod
        def open_window(panel_id):
            assert panel_id == "project"
            return panel

    bootstrap = BootstrapSelectionMixin()
    bootstrap.interaction_core = core
    bootstrap.project_panel = panel
    bootstrap.window_manager = _WindowManager()
    core.navigation.register(
        SelectionDomain.ASSET,
        bootstrap._present_asset_navigation_target,
    )
    core.panels.register_selection_authority(
        "project",
        (SelectionDomain.ASSET,),
    )
    try:
        target = SelectionTarget.asset(str(asset))
        assert core.navigation.locate(target, owner_id="project")
        assert state["path"] == lexical_path(second)
        assert core.selection.snapshot.primary == target
        entry = manager.action_journal.applied_entries()[0]
        assert entry.action.description == "Navigate Project"
        assert entry.action.marks_dirty is False

        manager.undo()
        assert state["path"] == lexical_path(first)
    finally:
        core.shutdown()
        EditorInteractionCore._instance = previous_core
        UndoManager._instance = previous_manager
        SelectionService._instance = previous_selection


def test_asset_navigation_rejects_existing_engine_path(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from Infernux.engine import project_context
    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import SelectionTarget
    from Infernux.engine.path_utils import lexical_path

    project = tmp_path / "Project"
    (project / "Assets").mkdir(parents=True)
    builtin = tmp_path / "Infernux" / "lib" / "standard.vert"
    builtin.parent.mkdir(parents=True)
    builtin.write_text("builtin", encoding="ascii")
    monkeypatch.setattr(project_context, "_project_root", str(project))

    panel = SimpleNamespace(
        get_current_path=lambda: lexical_path(project / "Assets"),
        can_navigate_to_path=lambda path: True,
        set_current_path=lambda path: True,
    )
    bootstrap = BootstrapSelectionMixin()
    bootstrap.project_panel = panel

    assert not bootstrap._present_asset_navigation_target(
        SelectionTarget.asset(str(builtin)),
        SimpleNamespace(activate_panel=False, record_history=False),
    )


def test_asset_navigation_can_restore_the_assets_folder(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from Infernux.engine import project_context
    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import SelectionTarget
    from Infernux.engine.path_utils import lexical_path

    project = tmp_path / "Project"
    assets = project / "Assets"
    asset = assets / "Materials" / "Test.mat"
    asset.parent.mkdir(parents=True)
    asset.write_text("{}", encoding="ascii")
    monkeypatch.setattr(project_context, "_project_root", str(project))

    state = {"path": lexical_path(tmp_path / "External")}
    panel = SimpleNamespace(
        get_current_path=lambda: state["path"],
        can_navigate_to_path=lambda path: True,
        set_current_path=lambda path: bool(
            state.__setitem__("path", lexical_path(path)) is None
        ),
    )
    bootstrap = BootstrapSelectionMixin()
    bootstrap.project_panel = panel

    assert bootstrap._present_asset_navigation_target(
        SelectionTarget.asset(str(asset)),
        SimpleNamespace(activate_panel=False, record_history=False),
    )
    assert state["path"] == lexical_path(asset.parent)
