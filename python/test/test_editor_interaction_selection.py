from Infernux.engine.interaction import (
    AssetMutation,
    AssetMutationKind,
    FocusService,
    InputContext,
    SelectionDomain,
    SelectionService,
    SelectionSnapshot,
    SelectionTarget,
)
import pytest


def test_selection_service_has_one_active_domain():
    service = SelectionService()
    service.select(SelectionTarget.scene_object(7), owner_id="hierarchy")

    service.select(SelectionTarget.asset("Assets/Test.mat"), owner_id="project")

    assert service.snapshot.domain is SelectionDomain.ASSET
    assert service.snapshot.primary == SelectionTarget.asset("Assets/Test.mat")


def test_selection_snapshot_rejects_mixed_domains():
    with pytest.raises(ValueError, match="cannot mix"):
        SelectionSnapshot.create(
            (
                SelectionTarget.scene_object(7),
                SelectionTarget.asset("Assets/Test.mat"),
            ),
            owner_id="invalid",
        )

    with pytest.raises(ValueError, match="requires an owner"):
        SelectionSnapshot.create(
            (SelectionTarget.scene_object(7),),
            owner_id="",
        )


def test_selection_targets_cover_every_planned_editor_domain():
    targets = (
        SelectionTarget.asset_subresource(
            "Assets/Robot.fbx", "mesh:body", sub_kind="submesh"
        ),
        SelectionTarget.component(42, 7, document_id="scene:main"),
        SelectionTarget.graph_element("graph:smoke", "node:1", sub_kind="node"),
        SelectionTarget.timeline_element(
            "timeline:intro", "key:8", sub_kind="keyframe"
        ),
        SelectionTarget.ui_element("scene:main", "button:play"),
        SelectionTarget.diagnostic_entry("console", "log:91"),
        SelectionTarget.settings_element(
            "settings:build", "scene:main", sub_kind="build_scene"
        ),
    )

    assert [target.domain for target in targets] == [
        SelectionDomain.ASSET_SUBRESOURCE,
        SelectionDomain.COMPONENT,
        SelectionDomain.GRAPH_ELEMENT,
        SelectionDomain.TIMELINE_ELEMENT,
        SelectionDomain.UI_ELEMENT,
        SelectionDomain.DIAGNOSTIC_ENTRY,
        SelectionDomain.SETTINGS_ELEMENT,
    ]
    assert targets[1].component_ids() == (42, 7)


def test_selection_snapshot_deduplication_preserves_anchor_identity():
    first = SelectionTarget.scene_object(1)
    second = SelectionTarget.scene_object(2)

    snapshot = SelectionSnapshot(
        "hierarchy",
        (first, first, second),
        primary_index=2,
        anchor_index=1,
    )

    assert snapshot.targets == (first, second)
    assert snapshot.primary == second
    assert snapshot.anchor == first


def test_selection_range_keeps_stable_anchor_and_clicked_primary():
    service = SelectionService()
    targets = [SelectionTarget.scene_object(value) for value in range(1, 6)]
    service.set_ordered_targets("hierarchy", targets)
    service.select(targets[1], owner_id="hierarchy")

    service.range_select(targets[4], owner_id="hierarchy")

    assert service.snapshot.targets == tuple(targets[1:])
    assert service.snapshot.anchor == targets[1]
    assert service.snapshot.primary == targets[4]


def test_same_domain_toggle_crosses_views_without_resetting_selection():
    service = SelectionService()
    first = SelectionTarget.scene_object(41)
    second = SelectionTarget.scene_object(42)
    service.select(first, owner_id="hierarchy")

    service.toggle(second, owner_id="scene_view")

    assert service.snapshot.targets == (first, second)
    assert service.snapshot.primary == second
    assert service.snapshot.anchor == first
    assert service.snapshot.owner_id == "scene_view"


def test_scene_object_projection_includes_component_owners_without_second_state():
    service = SelectionService()
    first = SelectionTarget.component(41, 101)
    second = SelectionTarget.component(42, 102)
    service.replace(
        (first, second),
        owner_id="inspector",
        primary=second,
        anchor=first,
        record_history=False,
    )

    assert service.scene_object_ids() == (41, 42)
    assert service.primary_scene_object_id() == 42
    assert service.is_scene_object_selected(41)
    assert service.snapshot.domain is SelectionDomain.COMPONENT


def test_scene_object_operations_publish_typed_selection_directly():
    service = SelectionService()
    service.set_ordered_scene_objects("hierarchy", [1, 2, 3, 4])
    service.select_scene_object(2, owner_id="hierarchy")
    service.range_select_scene_object(4, owner_id="hierarchy")

    assert service.scene_object_ids() == (2, 3, 4)
    assert service.primary_scene_object_id() == 4
    assert service.snapshot.anchor == SelectionTarget.scene_object(2)

    service.toggle_scene_object(3, owner_id="scene_view")
    assert service.scene_object_ids() == (2, 4)
    assert service.snapshot.owner_id == "scene_view"


def test_range_selection_can_reuse_same_domain_anchor_from_another_view():
    service = SelectionService()
    targets = [SelectionTarget.scene_object(value) for value in range(1, 6)]
    service.select(targets[1], owner_id="scene_view")
    service.set_ordered_targets("hierarchy", targets)

    service.range_select(targets[4], owner_id="hierarchy")

    assert service.snapshot.targets == tuple(targets[1:])
    assert service.snapshot.anchor == targets[1]
    assert service.snapshot.primary == targets[4]
    assert service.snapshot.owner_id == "hierarchy"


def test_selection_replay_does_not_request_another_history_entry():
    service = SelectionService()
    changes = []
    service.add_listener(changes.append)
    snapshot = SelectionSnapshot.create(
        (SelectionTarget.scene_object(42),),
        owner_id="scene_view",
    )

    service.apply_snapshot(snapshot, reason="undo", record_history=False)

    assert len(changes) == 1
    assert changes[0].record_history is False


def test_selection_reconcile_preserves_valid_order_and_repairs_primary_anchor():
    service = SelectionService()
    first = SelectionTarget.asset_subresource(
        "Assets/sheet.png", "1" * 32, sub_kind="sprite_frame"
    )
    second = SelectionTarget.asset_subresource(
        "Assets/sheet.png", "2" * 32, sub_kind="sprite_frame"
    )
    third = SelectionTarget.asset_subresource(
        "Assets/sheet.png", "3" * 32, sub_kind="sprite_frame"
    )
    service.replace(
        (first, second, third),
        owner_id="inspector",
        primary=second,
        anchor=second,
        record_history=False,
    )

    assert service.reconcile(
        lambda target: target != second,
        reason="test_reconcile",
        record_history=False,
    )
    assert service.snapshot.targets == (first, third)
    assert service.snapshot.primary == third
    assert service.snapshot.anchor == first


def test_asset_refresh_reconciles_sprite_selection_without_inspector_visibility(
    tmp_path,
    monkeypatch,
):
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.core import asset_types

    previous = SelectionService._instance
    service = SelectionService()
    asset_path = tmp_path / "sheet.png"
    asset_path.write_bytes(b"texture")
    stale = SelectionTarget.asset_subresource(
        str(asset_path),
        "1" * 32,
        sub_kind="sprite_frame",
    )
    service.select(stale, owner_id="inspector", record_history=False)
    monkeypatch.setattr(
        asset_types,
        "read_texture_import_settings",
        lambda _path: SimpleNamespace(
            texture_type=asset_types.TextureType.SPRITE,
            sprite_frames=(
                SimpleNamespace(stable_id="2" * 32),
            ),
        ),
    )
    try:
        BootstrapSelectionMixin()._on_asset_selection_source_changed(
            AssetMutation(AssetMutationKind.MODIFIED, str(asset_path))
        )
        assert service.snapshot.primary == SelectionTarget.asset(str(asset_path))
    finally:
        SelectionService._instance = previous


def test_timeline_editor_projects_stable_keyframe_selection():
    from Infernux.core.animation_timeline import TimelineKeyframe
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel

    previous = SelectionService._instance
    service = SelectionService()
    panel = AnimTimelineEditorPanel()
    key = TimelineKeyframe(time=0.5)
    panel._timeline.keyframes.append(key)
    try:
        panel.on_enable()
        panel._select_key(key)

        target = service.snapshot.primary
        assert target == SelectionTarget.timeline_element(
            panel.document_id,
            key.stable_id,
            sub_kind="keyframe",
        )
        assert panel._current_sel_key() is key

        service.select(SelectionTarget.asset("Assets/Test.mat"), owner_id="project")
        assert panel._current_sel_key() is None

        service.select(target, owner_id=panel.window_id, record_history=False)
        assert panel._current_sel_key() is key
    finally:
        panel.on_disable()
        SelectionService._instance = previous


def test_timeline_editor_drops_stale_keyframe_selection():
    from Infernux.engine.ui.animtimeline_editor_panel import AnimTimelineEditorPanel

    previous = SelectionService._instance
    service = SelectionService()
    panel = AnimTimelineEditorPanel()
    try:
        panel.on_enable()
        service.select(
            SelectionTarget.timeline_element(
                panel.document_id,
                "missing-key",
                sub_kind="keyframe",
            ),
            owner_id=panel.window_id,
            record_history=False,
        )

        assert panel._current_sel_key() is None
        assert service.snapshot.is_empty
    finally:
        panel.on_disable()
        SelectionService._instance = previous


def test_focus_service_owns_pending_and_active_panel_state():
    focus = FocusService()

    assert focus.request_panel_focus("project")
    assert focus.consume_panel_focus_request("project")
    assert not focus.consume_panel_focus_request("project")
    assert focus.activate_panel("project", child_context_id="project.search")
    assert focus.snapshot.active_panel_id == "project"
    assert focus.snapshot.child_context_id == "project.search"

    # A native WindowManager acknowledgement must not erase child focus.
    assert not focus.activate_panel("project")
    assert focus.snapshot.child_context_id == "project.search"


def test_focus_change_distinguishes_user_history_from_replay():
    focus = FocusService()
    changes = []
    focus.add_change_listener(changes.append)

    assert focus.activate_panel("project", reason="project_click")
    assert focus.apply_snapshot(
        FocusService().snapshot,
        reason="undo",
        record_history=False,
    )

    assert [change.reason for change in changes] == ["project_click", "undo"]
    assert [change.record_history for change in changes] == [True, False]


def test_bootstrap_records_focus_in_global_action_journal(monkeypatch):
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import (
        ContextRestoreStatus,
        EditorActionJournal,
        EditorContextSnapshot,
    )
    from Infernux.engine.undo import UndoManager

    focus = FocusService()
    selection = SelectionService()
    SelectionService.install(selection)
    journal = EditorActionJournal()
    manager = UndoManager(journal)
    focused = []

    class _WindowManager:
        @staticmethod
        def is_window_content_visible(_panel_id):
            return False

        @staticmethod
        def was_window_content_visible(_panel_id):
            return False

        @staticmethod
        def is_window_open(_panel_id):
            return True

        @staticmethod
        def focus_window(panel_id):
            focused.append(panel_id)

    bootstrap = BootstrapSelectionMixin()
    bootstrap.interaction_core = SimpleNamespace(
        focus=focus,
        capture_context=lambda **overrides: EditorContextSnapshot(
            overrides.get("focus", focus.snapshot),
            overrides.get("selection", selection.snapshot),
        ),
    )
    bootstrap.undo_manager = manager
    bootstrap.window_manager = _WindowManager()

    def _restore_context(context, _phase):
        if focus.snapshot != context.focus:
            bootstrap._apply_focus_snapshot(context.focus)
        if selection.snapshot != context.selection:
            selection.apply_snapshot(
                context.selection,
                reason="test_restore",
                record_history=False,
            )
        return ContextRestoreStatus.READY

    manager.set_context_hooks(
        bootstrap.interaction_core.capture_context,
        _restore_context,
    )
    focus.add_change_listener(bootstrap._on_global_focus_changed)

    assert focus.activate_panel("project", reason="project_click")
    assert manager.can_undo
    assert manager.undo_description == "Focus project"
    assert not journal.peek_undo().action.marks_dirty

    manager.undo()
    assert focus.snapshot.active_panel_id == ""
    assert not manager.can_undo
    assert manager.can_redo

    manager.redo()
    assert focus.snapshot.active_panel_id == "project"
    assert focused == ["project"]


def test_focus_history_keeps_keyboard_focus_and_replaced_dock_tab_separate():
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import (
        EditorActionJournal,
        EditorContextSnapshot,
        WindowLocator,
    )
    from Infernux.engine.undo import UndoManager

    focus = FocusService()
    focus.activate_panel("console", view_id="console", record_history=False)
    selection = SelectionService()
    SelectionService.install(selection)
    journal = EditorActionJournal()
    manager = UndoManager(journal)

    def capture_context(**overrides):
        snapshot = overrides.get("focus", focus.snapshot)
        view_id = snapshot.active_view_id or snapshot.active_panel_id
        return EditorContextSnapshot(
            snapshot,
            overrides.get("selection", selection.snapshot),
            window=WindowLocator(view_id, view_id) if view_id else None,
        )

    class _WindowManager:
        @staticmethod
        def locate_window(window_id):
            return WindowLocator(window_id, window_id)

    bootstrap = BootstrapSelectionMixin()
    bootstrap.interaction_core = SimpleNamespace(
        focus=focus,
        capture_context=capture_context,
    )
    bootstrap.undo_manager = manager
    bootstrap.window_manager = _WindowManager()
    focus.add_change_listener(bootstrap._on_global_focus_changed)

    assert focus.activate_panel(
        "particle_graph_editor",
        view_id="particle_graph_editor",
        record_history=True,
        presentation_before_view_id="scene_view",
    )

    entry = journal.peek_undo()
    assert entry is not None
    assert entry.before_context.focus.active_view_id == "console"
    assert entry.before_context.window.window_id == "scene_view"
    assert entry.after_context.focus.active_view_id == "particle_graph_editor"
    assert entry.after_context.window.window_id == "particle_graph_editor"


def test_context_restore_publishes_focus_intent_before_native_window_poll():
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import (
        ContextRestoreStatus,
        EditorContextSnapshot,
        WindowLocator,
    )

    focus = FocusService()
    focus.activate_panel("project", record_history=False)
    target_focus = type(focus.snapshot)(
        active_panel_id="particle_graph_editor",
        active_view_id="particle_graph_editor",
    )
    observed_focus = []

    class _WindowManager:
        @staticmethod
        def is_window_content_visible(_window_id):
            return False

        @staticmethod
        def restore_window(_locator):
            observed_focus.append(focus.snapshot)
            return ContextRestoreStatus.PENDING

    bootstrap = BootstrapSelectionMixin()
    bootstrap.interaction_core = SimpleNamespace(
        focus=focus,
        document_open=SimpleNamespace(),
    )
    bootstrap.window_manager = _WindowManager()

    status = bootstrap._restore_editor_context(
        EditorContextSnapshot(
            focus=target_focus,
            selection=SelectionService.instance().snapshot,
            window=WindowLocator("particle_graph_editor", "particle_graph_editor"),
        ),
        "undo_complete",
    )

    assert status is ContextRestoreStatus.PENDING
    assert observed_focus == [target_focus]
    assert focus.snapshot == target_focus


def test_visible_panel_context_restore_preserves_current_focus():
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import (
        ContextRestoreStatus,
        EditorContextSnapshot,
        WindowLocator,
    )

    focus = FocusService()
    focus.activate_panel(
        "scene_view",
        view_id="scene_view",
        record_history=False,
    )
    target_focus = type(focus.snapshot)(
        active_panel_id="inspector",
        active_view_id="inspector",
    )

    class _WindowManager:
        @staticmethod
        def is_window_content_visible(window_id):
            return window_id == "inspector"

        @staticmethod
        def restore_window(_locator):
            return ContextRestoreStatus.READY

        @staticmethod
        def restore_panel_child_context(_panel_id, _context_id):
            return True

    bootstrap = BootstrapSelectionMixin()
    bootstrap.interaction_core = SimpleNamespace(
        focus=focus,
        document_open=SimpleNamespace(),
    )
    bootstrap.window_manager = _WindowManager()

    status = bootstrap._restore_editor_context(
        EditorContextSnapshot(
            focus=target_focus,
            selection=SelectionService.instance().snapshot,
            window=WindowLocator("inspector", "inspector"),
        ),
        "undo_complete",
    )

    assert status is ContextRestoreStatus.READY
    assert focus.snapshot.active_view_id == "scene_view"


def test_already_visible_focus_change_is_not_recorded_in_global_journal():
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import EditorActionJournal, EditorContextSnapshot
    from Infernux.engine.undo import UndoManager

    focus = FocusService()
    selection = SelectionService()
    SelectionService.install(selection)
    journal = EditorActionJournal()
    manager = UndoManager(journal)

    class _WindowManager:
        @staticmethod
        def is_window_content_visible(panel_id):
            return panel_id in {"scene_view", "inspector"}

        @staticmethod
        def was_window_content_visible(panel_id):
            return panel_id in {"scene_view", "inspector"}

    bootstrap = BootstrapSelectionMixin()
    bootstrap.interaction_core = SimpleNamespace(
        focus=focus,
        capture_context=lambda **overrides: EditorContextSnapshot(
            overrides.get("focus", focus.snapshot),
            overrides.get("selection", selection.snapshot),
        ),
    )
    bootstrap.undo_manager = manager
    bootstrap.window_manager = _WindowManager()
    focus.add_change_listener(bootstrap._on_global_focus_changed)

    focus.activate_panel(
        "scene_view",
        view_id="scene_view",
        record_history=False,
    )
    assert focus.activate_panel(
        "inspector",
        view_id="inspector",
        reason="pointer_panel_activation",
        # Visibility is classified at the event producer. The history
        # listener consumes that decision and must not race a second probe.
        record_history=False,
    )
    assert not manager.can_undo


def test_revealed_dock_tab_focus_change_remains_in_global_journal():
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import EditorActionJournal, EditorContextSnapshot
    from Infernux.engine.undo import UndoManager

    focus = FocusService()
    selection = SelectionService()
    SelectionService.install(selection)
    journal = EditorActionJournal()
    manager = UndoManager(journal)

    class _WindowManager:
        @staticmethod
        def is_window_content_visible(panel_id):
            return panel_id == "particle_graph_editor"

        @staticmethod
        def was_window_content_visible(_panel_id):
            return False

    bootstrap = BootstrapSelectionMixin()
    bootstrap.interaction_core = SimpleNamespace(
        focus=focus,
        capture_context=lambda **overrides: EditorContextSnapshot(
            overrides.get("focus", focus.snapshot),
            overrides.get("selection", selection.snapshot),
        ),
    )
    bootstrap.undo_manager = manager
    bootstrap.window_manager = _WindowManager()
    focus.add_change_listener(bootstrap._on_global_focus_changed)

    assert focus.activate_panel(
        "particle_graph_editor",
        reason="pointer_panel_activation",
    )
    assert manager.can_undo
    assert manager.undo_description == "Focus particle_graph_editor"


def test_project_selection_intent_does_not_forge_panel_focus():
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin

    focus = FocusService()
    selection = SelectionService()
    SelectionService.install(selection)
    bootstrap = BootstrapSelectionMixin()
    bootstrap.interaction_core = SimpleNamespace(focus=focus)

    bootstrap._on_project_selection_changed(
        ("Assets/Smoke.particlegraph",),
        "Assets/Smoke.particlegraph",
    )

    assert focus.snapshot.active_panel_id == ""
    assert focus.snapshot.active_view_id == ""
    assert selection.snapshot.owner_id == "project"
    assert selection.snapshot.primary == SelectionTarget.asset(
        "Assets/Smoke.particlegraph"
    )


def test_native_panel_adapters_leave_focus_to_the_native_publisher():
    from pathlib import Path

    paths = (
        "python/Infernux/engine/bootstrap_hierarchy/_wire.py",
        "python/Infernux/engine/bootstrap_project.py",
        "python/Infernux/engine/bootstrap_inspector/_wire.py",
        "python/Infernux/engine/_bootstrap_selection.py",
        "python/Infernux/engine/ui/_scene_view_picking.py",
    )
    for path in paths:
        source = Path(path).read_text(encoding="utf-8")
        assert ".focus.activate_panel(" not in source, path

    selection_source = Path(paths[3]).read_text(encoding="utf-8")
    console_navigation = selection_source[
        selection_source.index("    def _navigate_console_entry_to_object") :
        selection_source.index("    def _record_selection_snapshot")
    ]
    assert "interaction_core.navigation.locate" in console_navigation
    assert "window_manager.open_window" not in console_navigation
    assert "SelectionService.instance().select" not in console_navigation


def test_hierarchy_pointer_commands_use_the_originating_view_context():
    from pathlib import Path

    source = Path(
        "python/Infernux/engine/bootstrap_hierarchy/_wire.py"
    ).read_text(encoding="utf-8")
    start = source.index("    def _execute_hierarchy_command")
    end = source.index("    def _render_context_menu", start)
    command_bridge = source[start:end]

    assert 'active_panel_id="hierarchy"' in command_bridge
    assert 'active_view_id="hierarchy"' in command_bridge
    assert "command_registry.execute_context(" in command_bridge
    assert "hp.execute_command = _execute_hierarchy_command" in source


def test_project_subresource_click_keeps_typed_row_identity():
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin

    focus = FocusService()
    selection = SelectionService()
    SelectionService.install(selection)
    bootstrap = BootstrapSelectionMixin()
    bootstrap.interaction_core = SimpleNamespace(focus=focus)

    row = "Assets/Robot.fbx::subanim:3"
    bootstrap._on_project_selection_changed((row,), row)

    assert selection.snapshot.primary == SelectionTarget.asset_subresource(
        "Assets/Robot.fbx",
        "3",
        sub_kind="subanimation",
    )


def test_native_panel_focus_loss_keeps_last_editor_command_context():
    focus = FocusService()
    changes = []
    focus.add_change_listener(changes.append)

    assert focus.observe_panel_focus("project", True, view_id="project")
    assert focus.snapshot.active_panel_id == "project"
    assert not focus.observe_panel_focus("project", False, view_id="project")
    assert focus.snapshot.active_panel_id == "project"

    assert focus.observe_panel_focus("hierarchy", True, view_id="hierarchy")
    assert focus.snapshot.active_panel_id == "hierarchy"
    assert [change.record_history for change in changes] == [False, False]


def test_project_click_records_focus_then_selection_as_distinct_user_actions():
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import EditorContextSnapshot
    from Infernux.engine.undo import UndoManager

    focus = FocusService()
    selection = SelectionService()
    SelectionService.install(selection)
    focus.activate_panel("scene_view", record_history=False)
    selection.select(
        SelectionTarget.scene_object(42),
        owner_id="scene_view",
        record_history=False,
    )
    manager = UndoManager()
    manager.set_context_hooks(
        lambda: EditorContextSnapshot(focus.snapshot, selection.snapshot),
        lambda context, _phase: (
            focus.apply_snapshot(context.focus, record_history=False),
            selection.apply_snapshot(context.selection, record_history=False),
        ),
    )
    bootstrap = BootstrapSelectionMixin()
    bootstrap.interaction_core = SimpleNamespace(
        focus=focus,
        capture_context=lambda **overrides: EditorContextSnapshot(
            overrides.get("focus", focus.snapshot),
            overrides.get("selection", selection.snapshot),
        ),
    )
    bootstrap._present_selection_snapshot = lambda _snapshot: None
    focus.add_change_listener(bootstrap._on_global_focus_changed)
    selection.add_listener(bootstrap._on_global_selection_changed)

    # Native EditorPanelFocusPublisher owns the first operation. The Project
    # selection adapter publishes only the second operation.
    focus.activate_panel(
        "project",
        view_id="project",
        reason="pointer_panel_activation",
        record_history=True,
    )
    bootstrap._on_project_selection_changed(
        ("Assets/Smoke.particlegraph",),
        "Assets/Smoke.particlegraph",
    )

    assert len(manager.action_journal.entries) == 2
    assert manager.undo_description == "Change Selection"
    manager.undo()
    assert focus.snapshot.active_panel_id == "project"
    assert selection.snapshot.primary == SelectionTarget.scene_object(42)
    manager.undo()
    assert focus.snapshot.active_panel_id == "scene_view"
    assert selection.snapshot.primary == SelectionTarget.scene_object(42)

def test_input_context_stack_honors_priority_and_modal_barrier():
    focus = FocusService()
    stack = focus.input_contexts
    stack.push(InputContext("global", "editor", priority=0))
    stack.push(InputContext("hierarchy", "hierarchy", priority=10))
    stack.push(InputContext("rename", "hierarchy", priority=20, blocks_lower=True))

    assert [context.context_id for context in stack.ordered()] == ["rename"]

    stack.remove("rename")
    assert [context.context_id for context in stack.ordered()] == [
        "hierarchy",
        "global",
    ]


def test_bootstrap_selection_projection_is_the_single_cross_panel_writer():
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin

    project_calls = []
    inspector_calls = []
    outlines = []
    bootstrap = BootstrapSelectionMixin()
    bootstrap.project_panel = SimpleNamespace(
        set_selected_files=lambda paths, primary, notify: project_calls.append(
            ("set", list(paths), primary, notify)
        ),
        clear_selection=lambda notify: project_calls.append(("clear", notify)),
    )
    bootstrap.inspector_panel = SimpleNamespace(
        set_selected_object_id=lambda object_id: inspector_calls.append(
            ("object", object_id)
        ),
        clear_selected_object=lambda: inspector_calls.append(("clear_object",)),
    )
    bootstrap._inspector_set_selected_file = (
        lambda path: inspector_calls.append(("file", path))
    )
    bootstrap._set_outline = (
        lambda primary, selected: outlines.append((primary, list(selected)))
    )

    asset = SelectionSnapshot.create(
        (SelectionTarget.asset("Assets/Smoke.mat"),),
        owner_id="project",
    )
    bootstrap._present_selection_snapshot(asset)

    asset_path = asset.primary.target_id
    assert project_calls == [("set", [asset_path], asset_path, False)]
    assert inspector_calls == [("file", asset_path)]
    assert outlines == [(0, [])]

    project_calls.clear()
    inspector_calls.clear()
    outlines.clear()
    scene = SelectionSnapshot.create(
        (SelectionTarget.scene_object(42),),
        owner_id="hierarchy",
    )
    bootstrap._present_selection_snapshot(scene)

    assert project_calls == [("clear", False)]
    assert inspector_calls == [("object", 42)]
    assert outlines == [(42, [42])]


def test_bootstrap_projects_subresources_and_all_component_owners(monkeypatch):
    from types import SimpleNamespace

    import Infernux.lib as native
    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin

    objects = {value: SimpleNamespace(id=value) for value in (41, 42)}

    class _Scene:
        world_id = 73
        structure_version = 1
        temporal_discontinuity_revision = 0

        @staticmethod
        def find_by_id(object_id):
            return objects.get(object_id)

    class _SceneManager:
        @staticmethod
        def instance():
            return _SceneManager()

        @staticmethod
        def get_active_scene():
            return _Scene()

    monkeypatch.setattr(native, "SceneManager", _SceneManager)

    project_calls = []
    inspector_calls = []
    component_projection = []
    outlines = []
    bootstrap = BootstrapSelectionMixin()
    bootstrap.project_panel = SimpleNamespace(
        set_selected_files=lambda paths, primary, notify: project_calls.append(
            (list(paths), primary, notify)
        ),
        clear_selection=lambda notify: project_calls.append(("clear", notify)),
    )
    bootstrap.inspector_panel = SimpleNamespace(
        set_selected_object_id=lambda object_id: inspector_calls.append(object_id),
        clear_selected_object=lambda: None,
        set_selected_component_ids=lambda ids: component_projection.append(list(ids)),
        clear_selected_components=lambda: component_projection.append([]),
    )
    bootstrap._inspector_set_selected_file = inspector_calls.append
    bootstrap._set_outline = (
        lambda primary, selected: outlines.append((primary, list(selected)))
    )
    bootstrap.event_bus = SimpleNamespace(emit=lambda *_args: None)

    subresource = SelectionSnapshot.create(
        (
            SelectionTarget.asset_subresource(
                "Assets/Robot.fbx", "mesh:body", sub_kind="submesh"
            ),
        ),
        owner_id="project",
    )
    bootstrap._present_selection_snapshot(subresource)
    asset_path = subresource.primary.document_id
    assert project_calls == [([asset_path], asset_path, False)]
    assert inspector_calls == [asset_path]

    project_calls.clear()
    inspector_calls.clear()
    components = SelectionSnapshot.create(
        (
            SelectionTarget.component(41, 1),
            SelectionTarget.component(42, 2),
        ),
        owner_id="inspector",
        primary=SelectionTarget.component(42, 2),
    )
    bootstrap._present_selection_snapshot(components)
    assert project_calls == [("clear", False)]
    assert inspector_calls == [42]
    assert outlines[-1] == (42, [41, 42])
    assert component_projection[-1] == [1, 2]


def test_inspector_component_header_publishes_stable_global_targets():
    from types import SimpleNamespace

    from Infernux.engine.bootstrap_inspector._wire import (
        _publish_component_selection,
    )

    service = SelectionService()
    changes = []
    service.add_listener(changes.append)
    bootstrap = SimpleNamespace(
        scene_file_manager=SimpleNamespace(document_id="scene:active")
    )

    assert _publish_component_selection(
        bootstrap,
        [41, 42],
        [101, 102],
        False,
    )
    assert service.snapshot.targets == (
        SelectionTarget.component(
            41, 101, document_id="scene:active", sub_kind="script"
        ),
        SelectionTarget.component(
            42, 102, document_id="scene:active", sub_kind="script"
        ),
    )
    assert service.snapshot.owner_id == "inspector"
    assert service.snapshot.primary == service.snapshot.targets[-1]
    assert service.snapshot.anchor == service.snapshot.targets[0]
    assert changes[-1].record_history is True


def test_console_selection_callback_uses_typed_global_authority():
    from Infernux.engine._bootstrap_panels import BootstrapPanelsMixin

    service = SelectionService()
    changes = []
    service.add_listener(changes.append)

    BootstrapPanelsMixin._on_console_selection_changed(73, True)
    assert service.snapshot.owner_id == "console"
    assert service.snapshot.primary == SelectionTarget.diagnostic_entry(
        "console",
        "73",
        sub_kind="log",
    )
    assert changes[-1].record_history is True

    BootstrapPanelsMixin._on_console_selection_changed(0, False)
    assert service.snapshot == SelectionSnapshot()
    assert changes[-1].record_history is False

    asset = SelectionTarget.asset("Assets/Smoke.mat")
    service.select(asset, owner_id="project", record_history=False)
    BootstrapPanelsMixin._on_console_selection_changed(0, False)
    assert service.snapshot.primary == asset


def test_bootstrap_projects_diagnostic_selection_into_console():
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin

    projected = []
    bootstrap = BootstrapSelectionMixin()
    bootstrap.console = SimpleNamespace(
        set_selection_snapshot=lambda uid: projected.append(uid)
    )
    bootstrap.project_panel = SimpleNamespace(
        clear_selection=lambda _notify: None,
        set_selected_files=lambda *_args: None,
    )
    bootstrap.inspector_panel = SimpleNamespace(
        clear_selected_object=lambda: None,
        set_selected_object_id=lambda _object_id: None,
    )
    bootstrap._inspector_set_selected_file = lambda _path: None
    bootstrap._set_outline = lambda _primary, _selected: None
    bootstrap.event_bus = SimpleNamespace(emit=lambda *_args: None)

    diagnostic = SelectionSnapshot.create(
        (
            SelectionTarget.diagnostic_entry(
                "console",
                "91",
                sub_kind="log",
            ),
        ),
        owner_id="console",
    )
    bootstrap._present_selection_snapshot(diagnostic)
    bootstrap._present_selection_snapshot(
        SelectionSnapshot.create(
            (SelectionTarget.asset("Assets/Test.mat"),),
            owner_id="project",
        )
    )

    assert projected == [91, 0]


def test_typed_selection_undo_replays_without_legacy_domain_loss():
    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin

    service = SelectionService()
    focus_requests = []

    class WindowManager:
        @staticmethod
        def focus_window(window_id):
            focus_requests.append(window_id)

    bootstrap = BootstrapSelectionMixin()
    bootstrap.window_manager = WindowManager()
    bootstrap._prev_selection_snapshot = SelectionSnapshot()

    graph = SelectionSnapshot.create(
        (SelectionTarget.graph_element("graph:smoke", "node:7", sub_kind="node"),),
        owner_id="particle_graph",
    )
    bootstrap._apply_selection_snapshot(graph)

    assert service.snapshot == graph
    assert bootstrap._prev_selection_snapshot == graph

    component = SelectionSnapshot.create(
        (SelectionTarget.component(42, 7),),
        owner_id="inspector",
    )
    bootstrap._apply_selection_snapshot(component)
    assert service.snapshot == component
    assert bootstrap._prev_selection_snapshot == component

    subresource = SelectionSnapshot.create(
        (
            SelectionTarget.asset_subresource(
                "Assets/Robot.fbx", "mesh:body", sub_kind="submesh"
            ),
        ),
        owner_id="project",
    )
    bootstrap._apply_selection_snapshot(subresource)
    assert service.snapshot == subresource
    assert bootstrap._prev_selection_snapshot == subresource
    assert focus_requests == []


def test_scene_pick_reveals_through_navigation_without_activating_hierarchy():
    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.undo import UndoManager

    previous_core = EditorInteractionCore._instance
    previous_manager = UndoManager._instance
    previous_selection = SelectionService._instance
    core = EditorInteractionCore()
    UndoManager(core.action_journal)
    requests = []
    core.navigation.register(
        SelectionDomain.SCENE_OBJECT,
        lambda target, request: requests.append((target, request)) or True,
    )
    core.panels.register_selection_authority(
        "scene_view",
        (SelectionDomain.SCENE_OBJECT,),
    )
    bootstrap = BootstrapSelectionMixin()
    bootstrap.interaction_core = core
    try:
        bootstrap._on_scene_view_picked(41, False)

        assert core.selection.snapshot.owner_id == "scene_view"
        assert core.selection.snapshot.primary == SelectionTarget.scene_object(41)
        assert requests[0][0] == SelectionTarget.scene_object(41)
        assert requests[0][1].activate_panel is False
    finally:
        core.shutdown()
        EditorInteractionCore._instance = previous_core
        UndoManager._instance = previous_manager
        SelectionService._instance = previous_selection


def test_ui_editor_projects_directly_from_typed_selection(monkeypatch):
    from types import SimpleNamespace

    import Infernux.lib as native
    from Infernux.engine._bootstrap_wiring import BootstrapWiringMixin

    selected_object = SimpleNamespace(id=42)

    class _Scene:
        @staticmethod
        def find_by_id(object_id):
            return selected_object if object_id == 42 else None

    class _SceneManager:
        @staticmethod
        def instance():
            return _SceneManager()

        @staticmethod
        def get_active_scene():
            return _Scene()

    monkeypatch.setattr(native, "SceneManager", _SceneManager)

    projected = []
    callbacks = {}
    ui_editor = SimpleNamespace(
        set_on_request_ui_mode=lambda callback: callbacks.__setitem__(
            "mode", callback
        ),
        project_global_selection=projected.append,
    )
    bootstrap = BootstrapWiringMixin()
    bootstrap.ui_editor = ui_editor
    bootstrap.hierarchy = SimpleNamespace(set_ui_mode=lambda _enabled: None)
    bootstrap.scene_view = SimpleNamespace()
    bootstrap.game_view = SimpleNamespace()
    bootstrap.window_manager = None

    service = SelectionService()
    bootstrap._wire_ui_editor()
    assert projected == [None]

    service.select(
        SelectionTarget.scene_object(42),
        owner_id="scene_view",
        record_history=False,
    )
    assert projected[-1] is selected_object

    service.select(
        SelectionTarget.asset("Assets/Test.mat"),
        owner_id="project",
        record_history=False,
    )
    assert projected[-1] is None

def test_ui_editor_component_is_a_revision_cached_global_selection_projection(
    monkeypatch,
):
    from types import SimpleNamespace

    import Infernux.lib as native
    import Infernux.ui.inx_ui_screen_component as screen_component_module
    from Infernux.engine.ui.ui_editor_panel import UIEditorPanel

    class _ScreenComponent:
        pass

    first_component = _ScreenComponent()
    second_component = _ScreenComponent()
    objects = {
        41: SimpleNamespace(id=41, get_py_components=lambda: [first_component]),
        42: SimpleNamespace(id=42, get_py_components=lambda: [second_component]),
    }

    class _Scene:
        world_id = 73
        structure_version = 1
        temporal_discontinuity_revision = 0

        @staticmethod
        def find_by_id(object_id):
            return objects.get(object_id)

    class _SceneManager:
        @staticmethod
        def instance():
            return _SceneManager()

        @staticmethod
        def get_active_scene():
            return _Scene()

    monkeypatch.setattr(native, "SceneManager", _SceneManager)
    monkeypatch.setattr(
        screen_component_module,
        "InxUIScreenComponent",
        _ScreenComponent,
    )

    service = SelectionService()
    panel = UIEditorPanel.__new__(UIEditorPanel)
    panel._selected_element_cache_revision = -1
    panel._selected_element_cache_object_id = 0
    panel._selected_element_cache_scene_key = None
    panel._selected_element_cache = None

    service.select_scene_object(
        41,
        owner_id="ui_editor",
        record_history=False,
    )
    assert panel._selected_element_comp is first_component
    objects[41] = SimpleNamespace(id=41, get_py_components=lambda: [second_component])
    assert panel._selected_element_comp is first_component

    _Scene.temporal_discontinuity_revision += 1
    assert panel._selected_element_comp is second_component

    service.select_scene_object(
        42,
        owner_id="hierarchy",
        record_history=False,
    )
    assert panel._selected_element_comp is second_component

    service.clear(record_history=False)
    assert panel._selected_element_comp is None
