from Infernux.engine.interaction import (
    ClipboardDomain,
    ClipboardItem,
    ClipboardService,
    DocumentRegistry,
    EditorContextSnapshot,
    GraphElementKind,
    GraphElementRef,
    SelectionService,
)
from Infernux.core.anim_state_machine import AnimCondition, AnimState, AnimTransition
from Infernux.engine.ui.animfsm_graph_authoring import AnimFSMGraphAuthoringModel
from Infernux.engine.ui.animfsm_editor_panel import AnimFSMEditorPanel
from Infernux.engine.ui.node_graph_editor_panel import NodeGraphEditorPanel
from Infernux.engine.undo import UndoManager


def _panel_with_history():
    DocumentRegistry()
    selection = SelectionService()
    ClipboardService()
    manager = UndoManager()
    manager.set_context_hooks(
        lambda: EditorContextSnapshot(selection=selection.snapshot),
        lambda context, phase: (
            selection.apply_snapshot(
                context.selection,
                reason=phase,
                record_history=False,
            ),
            True,
        )[1],
    )
    panel = AnimFSMEditorPanel()
    panel._graph_selection.bind(selection)
    return panel, manager


def test_animfsm_uses_the_shared_node_graph_editor_and_domain_adapter():
    panel, _manager = _panel_with_history()

    assert isinstance(panel, NodeGraphEditorPanel)
    assert isinstance(panel._graph, AnimFSMGraphAuthoringModel)
    assert panel._view.on_link_created == panel._on_link_created
    assert panel._view.on_nodes_deleted == panel._on_nodes_deleted
    assert not hasattr(panel._view, "on_copy")
    assert not hasattr(panel._view, "on_paste")
    assert not hasattr(panel._view, "on_can_execute_edit_command")
    assert not hasattr(panel._view, "on_execute_edit_command")


def test_animfsm_disk_discard_reconciles_document_revision(tmp_path):
    from Infernux.core.anim_state_machine import AnimStateMachine
    from Infernux.engine.interaction import DocumentActionStatus

    target = tmp_path / "Saved.animfsm"
    assert AnimStateMachine(name="Saved").save(str(target))
    panel, _manager = _panel_with_history()
    panel.open_document_resource_immediate(str(target))
    registry = DocumentRegistry.instance()
    document = registry.require(panel.document_id)
    registry.mark_changed(document.document_id)

    result = registry.request_discard(document.document_id)

    assert result.status is DocumentActionStatus.APPLIED
    assert registry.require(panel.document_id).is_dirty is False
    assert panel._document_is_dirty() is False


def test_animfsm_title_refresh_cannot_redirty_a_saved_document():
    panel, _manager = _panel_with_history()
    registry = DocumentRegistry.instance()
    document = registry.require(panel.document_id)
    registry.mark_changed(document.document_id)
    registry.mark_saved(document.document_id)

    assert panel._window_title_suffix() == ""
    assert document.is_dirty is False
    assert panel._document_is_dirty() is False


def test_animfsm_stages_structure_in_node_graph_without_mutating_fsm(monkeypatch):
    panel, _manager = _panel_with_history()
    state = AnimState(name="Graph First")
    observed = {}

    def reject_command(_description, mutations, **_kwargs):
        observed["mutation_count"] = len(mutations)
        observed["fsm_states"] = tuple(panel._fsm.states)
        observed["graph_node"] = panel._graph.find_node(state.stable_id)
        return False

    monkeypatch.setattr(panel, "_execute_graph_mutations", reject_command)

    assert not panel._insert_state(state, "Add Graph First", make_default=True)
    assert observed["mutation_count"] == 2
    assert observed["fsm_states"] == ()
    assert observed["graph_node"] is None


def test_node_graph_host_rejects_mutation_when_history_is_disabled():
    panel, manager = _panel_with_history()
    document = panel._fsm_document()
    initial_revision = document.revision
    manager.enabled = False

    assert not panel._insert_parameter()
    assert panel._fsm.parameters == []
    assert document.revision == initial_revision
    assert manager.action_journal.entries == ()


def test_animfsm_parameter_edit_uses_stable_diff_and_document_revision():
    panel, manager = _panel_with_history()
    document = panel._fsm_document()
    initial_revision = document.revision

    assert panel._insert_parameter()
    parameter = panel._fsm.parameters[0]
    parameter_id = parameter.stable_id
    insert_revision = document.revision
    assert insert_revision > initial_revision
    assert panel._graph_selection.primary_id(GraphElementKind.PARAMETER) == parameter_id

    after = parameter.to_dict()
    after["name"] = "speed"
    assert panel._update_parameter_document(
        parameter,
        after,
        "Rename parameter",
        merge_key=f"parameter:{parameter_id}:name",
    )
    rename_revision = document.revision
    assert panel._fsm.parameters[0].name == "speed"
    assert rename_revision > insert_revision

    manager.undo()
    assert panel._fsm.parameters[0].name == "var_0"
    assert document.revision == insert_revision

    manager.undo()
    assert panel._fsm.parameters == []
    assert document.revision == initial_revision

    manager.redo()
    assert panel._fsm.parameters[0].stable_id == parameter_id
    manager.redo()
    assert panel._fsm.parameters[0].name == "speed"
    assert document.revision == rename_revision


def test_node_graph_edit_records_revealed_panel_before_its_mutation():
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import FocusService
    from Infernux.engine.undo import GlobalFocusCommand

    DocumentRegistry()
    selection = SelectionService()
    focus = FocusService()
    manager = UndoManager()
    panel = AnimFSMEditorPanel()
    panel._graph_selection.bind(selection)

    class _HiddenGraphWindow:
        @staticmethod
        def is_window_content_visible(_window_id):
            return False

    panel._window_manager = _HiddenGraphWindow()
    bootstrap = BootstrapSelectionMixin()
    bootstrap.window_manager = panel._window_manager
    bootstrap.interaction_core = SimpleNamespace(
        focus=focus,
        capture_context=lambda **overrides: EditorContextSnapshot(
            overrides.get("focus", focus.snapshot),
            overrides.get("selection", selection.snapshot),
        ),
    )
    focus.add_change_listener(bootstrap._on_global_focus_changed)
    focus.activate_panel("scene_view", view_id="scene_view", record_history=False)
    manager.set_context_hooks(
        bootstrap.interaction_core.capture_context,
        lambda context, _phase: (
            focus.apply_snapshot(context.focus, record_history=False),
            selection.apply_snapshot(context.selection, record_history=False),
            True,
        )[-1],
    )

    assert panel._insert_parameter()

    entries = manager.action_journal.entries
    assert len(entries) == 2
    assert isinstance(entries[0].action, GlobalFocusCommand)
    assert entries[0].after_context.focus.active_view_id == panel.window_id
    assert entries[1].before_context.focus.active_view_id == panel.window_id
    assert entries[1].after_context.focus.active_view_id == panel.window_id

    manager.undo()
    assert panel._fsm.parameters == []
    assert focus.snapshot.active_view_id == panel.window_id
    manager.undo()
    assert focus.snapshot.active_view_id == "scene_view"


def test_node_graph_edit_scene_switch_and_scene_edit_undo_in_exact_order():
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.interaction import FocusService
    from Infernux.engine.undo import GlobalFocusCommand, LambdaCommand

    DocumentRegistry()
    selection = SelectionService()
    focus = FocusService()
    manager = UndoManager()
    panel = AnimFSMEditorPanel()
    panel._graph_selection.bind(selection)

    class _VisibleGraphWindow:
        @staticmethod
        def is_window_content_visible(window_id):
            return window_id == panel.window_id

    panel._window_manager = _VisibleGraphWindow()
    bootstrap = BootstrapSelectionMixin()
    bootstrap.window_manager = panel._window_manager
    bootstrap.interaction_core = SimpleNamespace(
        focus=focus,
        capture_context=lambda **overrides: EditorContextSnapshot(
            overrides.get("focus", focus.snapshot),
            overrides.get("selection", selection.snapshot),
        ),
    )
    focus.add_change_listener(bootstrap._on_global_focus_changed)
    focus.activate_panel(
        panel.window_id,
        view_id=panel.window_id,
        document_id=panel.document_id,
        record_history=False,
    )
    manager.set_context_hooks(
        bootstrap.interaction_core.capture_context,
        lambda context, _phase: (
            focus.apply_snapshot(context.focus, record_history=False),
            selection.apply_snapshot(context.selection, record_history=False),
            True,
        )[-1],
    )

    assert panel._insert_parameter()
    scene_value = {"x": 0.0}
    focus.activate_panel("scene_view", view_id="scene_view", record_history=True)
    assert manager.execute(
        LambdaCommand(
            "Move Camera",
            undo_fn=lambda: scene_value.update(x=0.0),
            redo_fn=lambda: scene_value.update(x=1.0),
        )
    )

    entries = manager.action_journal.entries
    assert [entry.action.description for entry in entries] == [
        "Add parameter",
        "Focus scene_view",
        "Move Camera",
    ]
    assert isinstance(entries[1].action, GlobalFocusCommand)

    manager.undo()
    assert scene_value["x"] == 0.0
    assert focus.snapshot.active_view_id == "scene_view"
    manager.undo()
    assert focus.snapshot.active_view_id == panel.window_id
    manager.undo()
    assert panel._fsm.parameters == []
    assert focus.snapshot.active_view_id == panel.window_id
    assert len(manager.action_journal.entries) == 3


def test_animfsm_parameter_rename_preserves_transition_reference_and_undo():
    panel, manager = _panel_with_history()
    source = AnimState(name="Source")
    target = AnimState(name="Target")
    assert panel._insert_state(source, "Add Source", make_default=True)
    assert panel._insert_state(target, "Add Target", make_default=False)
    assert panel._insert_parameter()
    parameter = panel._fsm.parameters[0]
    old_parameter = parameter.to_dict()
    old_parameter["name"] = "speed"
    assert panel._update_parameter_document(
        parameter,
        old_parameter,
        "Rename parameter",
        merge_key=f"parameter:{parameter.stable_id}:name",
    )
    transition = AnimTransition(
        target_state="Target",
        conditions=[
            AnimCondition(
                parameter_id=parameter.stable_id,
                operator=">",
                threshold=1.0,
            )
        ],
    )
    assert panel._insert_transition(source, transition, "Connect")
    baseline_revision = panel._fsm_document().revision
    manager.clear()

    renamed = panel._fsm.parameters[0].to_dict()
    renamed["name"] = "velocity"
    assert panel._update_parameter_document(
        panel._fsm.parameters[0],
        renamed,
        "Rename parameter",
        merge_key=f"parameter:{parameter.stable_id}:name",
    )
    assert panel._fsm.states[0].transitions[0].conditions[0].parameter_id == (
        parameter.stable_id
    )
    rename_diff = manager.action_journal.applied_entries()[-1].action.diff
    assert [mutation.element.kind for mutation in rename_diff.mutations] == [
        GraphElementKind.PARAMETER,
    ]

    manager.undo()
    assert panel._fsm.parameters[0].name == "speed"
    assert panel._fsm.states[0].transitions[0].conditions[0].parameter_id == (
        parameter.stable_id
    )
    assert panel._fsm_document().revision == baseline_revision


def test_animfsm_parameter_remove_atomically_removes_conditions_and_undo_restores():
    panel, manager = _panel_with_history()
    source = AnimState(name="Source")
    target = AnimState(name="Target")
    assert panel._insert_state(source, "Add Source", make_default=True)
    assert panel._insert_state(target, "Add Target", make_default=False)
    assert panel._insert_parameter()
    parameter = panel._fsm.parameters[0]
    condition = AnimCondition(
        parameter_id=parameter.stable_id,
        operator=">",
        threshold=0.5,
    )
    transition = AnimTransition(target_state="Target", conditions=[condition])
    assert panel._insert_transition(source, transition, "Connect")
    manager.clear()

    assert panel._remove_parameter(parameter.stable_id)
    assert panel._fsm.parameters == []
    assert panel._fsm.states[0].transitions[0].conditions == []

    manager.undo()
    assert panel._fsm.parameters[0].stable_id == parameter.stable_id
    assert panel._fsm.states[0].transitions[0].conditions == [condition]


def test_animfsm_node_move_does_not_snapshot_the_whole_fsm():
    panel, manager = _panel_with_history()
    state = panel._fsm.add_state("Idle")
    state.position = [10.0, 20.0]
    panel._sync_graph_from_fsm()
    node = panel._graph.find_node(state.stable_id)
    document = panel._fsm_document()
    initial_revision = document.revision

    panel._on_node_drag_start(node.uid)
    node.pos_x = 110.0
    node.pos_y = 220.0
    panel._on_node_drag_end(node.uid)

    assert state.position == [110.0, 220.0]
    assert document.revision > initial_revision
    assert len(manager.action_journal.applied_entries()[-1].action.diff.mutations) == 1

    manager.undo()
    assert state.position == [10.0, 20.0]
    assert (node.pos_x, node.pos_y) == (10.0, 20.0)
    assert document.revision == initial_revision


def test_animfsm_structural_undo_uses_shared_node_graph_payloads():
    panel, manager = _panel_with_history()
    state = AnimState(name="Shared Core", position=[64.0, 96.0])

    assert panel._insert_state(state, "Add Shared Core", make_default=True)

    mutations = manager.action_journal.applied_entries()[-1].action.diff.mutations
    node_insert = next(
        mutation
        for mutation in mutations
        if mutation.element.kind is GraphElementKind.NODE
    )
    assert node_insert.after["type_id"] == "animfsm.state"
    assert node_insert.after["position"] == [64.0, 96.0]
    assert node_insert.after["properties"]["fsm_state"]["stable_id"] == state.stable_id
    assert not hasattr(
        manager.action_journal.applied_entries()[-1].action, "before_snapshot"
    )


def test_animfsm_state_delete_undo_restores_tree_links_default_and_selection():
    panel, manager = _panel_with_history()
    idle = AnimState(name="Idle")
    run = AnimState(name="Run")
    jump = AnimState(name="Jump")
    assert panel._insert_state(idle, "Add Idle", make_default=True)
    assert panel._insert_state(run, "Add Run", make_default=False)
    assert panel._insert_state(jump, "Add Jump", make_default=False)
    idle_to_run = AnimTransition(target_state="Run")
    run_to_jump = AnimTransition(target_state="Jump")
    jump_to_run = AnimTransition(target_state="Run")
    assert panel._insert_transition(idle, idle_to_run, "Idle to Run")
    assert panel._insert_transition(run, run_to_jump, "Run to Jump")
    assert panel._insert_transition(jump, jump_to_run, "Jump to Run")
    assert panel._set_default_state("Run", "Default Run")
    panel._graph_selection.select_one(
        GraphElementKind.NODE,
        run.stable_id,
        record_history=False,
    )

    baseline = panel._fsm.to_dict()
    baseline_revision = panel._fsm_document().revision
    manager.clear()

    assert panel._remove_states((run.stable_id,))
    assert [state.name for state in panel._fsm.states] == ["Idle", "Jump"]
    assert panel._fsm.default_state == "Idle"
    assert all(
        transition.target_state != "Run"
        for state in panel._fsm.states
        for transition in state.transitions
    )
    assert panel._graph_selection.elements == ()
    assert len(manager.action_journal.applied_entries()[-1].action.diff.mutations) == 5

    manager.undo()

    assert panel._fsm.to_dict() == baseline
    assert panel._fsm_document().revision == baseline_revision
    assert panel._graph_selection.primary == GraphElementRef(
        GraphElementKind.NODE,
        run.stable_id,
    )
    assert panel._graph.find_link(idle_to_run.stable_id) is not None
    assert panel._graph.find_link(run_to_jump.stable_id) is not None
    assert panel._graph.find_link(jump_to_run.stable_id) is not None


def test_animfsm_state_rename_propagates_references_and_undoes_once():
    panel, manager = _panel_with_history()
    source = AnimState(name="Source")
    target = AnimState(name="Target")
    assert panel._insert_state(source, "Add Source", make_default=True)
    assert panel._insert_state(target, "Add Target", make_default=False)
    transition = AnimTransition(target_state="Target")
    assert panel._insert_transition(source, transition, "Connect")
    assert panel._set_default_state("Target", "Default Target")
    baseline_revision = panel._fsm_document().revision
    manager.clear()

    assert panel._update_state_fields(
        target,
        "Rename state",
        merge_key=f"state:{target.stable_id}:name",
        name="Destination",
    )
    assert panel._fsm.default_state == "Destination"
    assert panel._fsm.states[0].transitions[0].target_state == "Destination"

    manager.undo()
    assert panel._fsm.default_state == "Target"
    assert panel._fsm.states[0].transitions[0].target_state == "Target"
    assert panel._fsm_document().revision == baseline_revision


def test_animfsm_transition_reconnect_is_one_precise_undo_step():
    panel, manager = _panel_with_history()
    source = AnimState(name="Source")
    other_source = AnimState(name="Other Source")
    target = AnimState(name="Target")
    other_target = AnimState(name="Other Target")
    for index, state in enumerate((source, other_source, target, other_target)):
        assert panel._insert_state(
            state,
            f"Add {state.name}",
            make_default=index == 0,
        )
    transition = AnimTransition(target_state="Target")
    assert panel._insert_transition(source, transition, "Connect")
    baseline_revision = panel._fsm_document().revision
    manager.clear()

    panel._on_link_replaced(
        transition.stable_id,
        other_source.stable_id,
        "out",
        other_target.stable_id,
        "in",
    )

    owner, current = panel._fsm.get_transition_by_id(transition.stable_id)
    assert owner.stable_id == other_source.stable_id
    assert current.target_state == "Other Target"
    assert len(manager.action_journal.applied_entries()[-1].action.diff.mutations) == 1

    manager.undo()
    owner, current = panel._fsm.get_transition_by_id(transition.stable_id)
    assert owner.stable_id == source.stable_id
    assert current.target_state == "Target"
    assert panel._fsm_document().revision == baseline_revision


def test_animfsm_copy_paste_uses_global_typed_clipboard_and_is_atomic():
    panel, manager = _panel_with_history()
    first = AnimState(name="First", position=[10.0, 20.0])
    second = AnimState(name="Second", position=[30.0, 40.0])
    assert panel._insert_state(first, "Add First", make_default=True)
    assert panel._insert_state(second, "Add Second", make_default=False)
    assert panel._insert_parameter()
    parameter = panel._fsm.parameters[0]
    transition = AnimTransition(
        target_state="Second",
        conditions=[
            AnimCondition(parameter_id=parameter.stable_id, operator=">", threshold=0.0)
        ],
    )
    assert panel._insert_transition(first, transition, "Connect")
    panel._graph_selection.select(
        (
            GraphElementRef(GraphElementKind.NODE, first.stable_id),
            GraphElementRef(GraphElementKind.NODE, second.stable_id),
        ),
        record_history=False,
    )
    panel._on_graph_copy()

    payload = ClipboardService.instance().peek(ClipboardDomain.GRAPH_ELEMENT)
    assert payload is not None
    assert len(payload.items) == 1
    assert payload.items[0].sub_kind == "node_graph_subgraph"
    assert len(payload.items[0].data.state.nodes) == 2
    assert len(payload.items[0].data.state.links) == 1
    baseline = panel._fsm.to_dict()
    baseline_revision = panel._fsm_document().revision
    manager.clear()

    panel._on_graph_paste()

    assert [state.name for state in panel._fsm.states] == [
        "First",
        "Second",
        "First 2",
        "Second 2",
    ]
    pasted_first = panel._fsm.get_state("First 2")
    pasted_second = panel._fsm.get_state("Second 2")
    assert pasted_first.position == [58.0, 68.0]
    assert pasted_second.position == [78.0, 88.0]
    assert [item.target_state for item in pasted_first.transitions] == ["Second 2"]
    assert len(manager.action_journal.applied_entries()[-1].action.diff.mutations) == 3

    manager.undo()
    assert panel._fsm.to_dict() == baseline
    assert panel._fsm_document().revision == baseline_revision


def test_animfsm_paste_rejects_another_graph_domain_payload():
    panel, manager = _panel_with_history()
    ClipboardService.instance().write(
        ClipboardDomain.GRAPH_ELEMENT,
        (ClipboardItem("particle-node", "particle:one", "particle_node", {}),),
        source_owner_id="particle_graph_editor",
    )
    baseline = panel._fsm.to_dict()
    manager.clear()

    panel._on_graph_paste()

    assert panel._fsm.to_dict() == baseline
    assert manager.can_undo is False


def test_animfsm_duplicate_and_cut_use_shared_graph_commands():
    panel, manager = _panel_with_history()
    first = AnimState(name="First", position=[10.0, 20.0])
    second = AnimState(name="Second", position=[30.0, 40.0])
    assert panel._insert_state(first, "Add First", make_default=True)
    assert panel._insert_state(second, "Add Second", make_default=False)
    transition = AnimTransition(target_state="Second")
    assert panel._insert_transition(first, transition, "Connect")
    panel._graph_selection.select(
        (
            GraphElementRef(GraphElementKind.NODE, first.stable_id),
            GraphElementRef(GraphElementKind.NODE, second.stable_id),
        ),
        record_history=False,
    )
    manager.clear()

    assert panel.command_edit_duplicate()
    assert [state.name for state in panel._fsm.states] == [
        "First",
        "Second",
        "First 2",
        "Second 2",
    ]
    assert manager.undo_description == "Duplicate graph nodes"
    manager.undo()
    assert [state.name for state in panel._fsm.states] == ["First", "Second"]

    panel._graph_selection.select(
        (
            GraphElementRef(GraphElementKind.NODE, first.stable_id),
            GraphElementRef(GraphElementKind.NODE, second.stable_id),
        ),
        record_history=False,
    )
    manager.clear()
    assert panel.command_edit_cut()
    assert panel._fsm.states == []
    assert manager.undo_description == "Delete states"
    manager.undo()
    assert [state.name for state in panel._fsm.states] == ["First", "Second"]
