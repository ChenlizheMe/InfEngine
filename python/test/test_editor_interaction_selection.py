from Infernux.engine.interaction import (
    FocusService,
    InputContext,
    SelectionDomain,
    SelectionService,
    SelectionSnapshot,
    SelectionTarget,
)
from Infernux.engine.ui.selection_manager import SelectionManager

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


def test_legacy_selection_manager_is_a_typed_service_adapter():
    service = SelectionService()
    SelectionService.install(service)
    manager = SelectionManager()

    manager.box_select([2, 3])
    service.select(SelectionTarget.asset("Assets/Test.mat"), owner_id="project")

    assert manager.get_ids() == []
    assert manager.get_primary() == 0
    assert service.snapshot.domain is SelectionDomain.ASSET


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
