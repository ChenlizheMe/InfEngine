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


def test_legacy_selection_adapter_rebinds_to_replaced_authority(monkeypatch):
    monkeypatch.setattr(SelectionService, "_instance", None)
    monkeypatch.setattr(SelectionManager, "_instance", None)

    original = SelectionService()
    adapter = SelectionManager.instance()
    notifications = []
    adapter.add_listener(lambda: notifications.append(adapter.get_ids()))

    replacement = SelectionService()
    assert SelectionManager.instance() is adapter
    assert adapter._selection is replacement

    replacement.select(
        SelectionTarget.scene_object(42),
        owner_id="hierarchy",
        record_history=False,
    )
    assert adapter.get_ids() == [42]
    assert notifications == [[42]]

    original.select(
        SelectionTarget.scene_object(7),
        owner_id="hierarchy",
        record_history=False,
    )
    assert adapter.get_ids() == [42]
    assert notifications == [[42]]


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


def test_bootstrap_selection_projection_is_the_single_cross_panel_writer(monkeypatch):
    from types import SimpleNamespace

    import Infernux.lib as native
    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin
    from Infernux.engine.ui.event_bus import EditorEvent

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

    project_calls = []
    inspector_calls = []
    outlines = []
    events = []
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
    bootstrap.event_bus = SimpleNamespace(
        emit=lambda event, value: events.append((event, value))
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
    assert events == [(EditorEvent.FILE_SELECTED, asset_path)]

    project_calls.clear()
    inspector_calls.clear()
    outlines.clear()
    events.clear()
    scene = SelectionSnapshot.create(
        (SelectionTarget.scene_object(42),),
        owner_id="hierarchy",
    )
    bootstrap._present_selection_snapshot(scene)

    assert project_calls == [("clear", False)]
    assert inspector_calls == [("object", 42)]
    assert outlines == [(42, [42])]
    assert events == [(EditorEvent.SELECTION_CHANGED, selected_object)]


def test_bootstrap_projects_subresources_and_all_component_owners(monkeypatch):
    from types import SimpleNamespace

    import Infernux.lib as native
    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin

    objects = {value: SimpleNamespace(id=value) for value in (41, 42)}

    class _Scene:
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
    bootstrap = BootstrapSelectionMixin()
    bootstrap.window_manager = None
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


def test_scene_box_selection_preserves_primary_and_anchor():
    from types import SimpleNamespace

    from Infernux.engine._bootstrap_selection import BootstrapSelectionMixin

    service = SelectionService()
    first = SelectionTarget.scene_object(41)
    second = SelectionTarget.scene_object(42)
    scene_snapshot = SelectionSnapshot.create(
        (first, second),
        owner_id="scene_view",
        primary=first,
        anchor=second,
    )
    service.apply_snapshot(scene_snapshot, record_history=False)

    revealed = []
    bootstrap = BootstrapSelectionMixin()
    bootstrap.hierarchy = SimpleNamespace(expand_to_object=revealed.append)

    bootstrap._on_box_select_done(None)

    assert service.snapshot.owner_id == "scene_view"
    assert service.snapshot.targets == (first, second)
    assert service.snapshot.primary == first
    assert service.snapshot.anchor == second
    assert revealed == [41]


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
        set_on_selection_changed=lambda callback: callbacks.__setitem__(
            "selection", callback
        ),
        notify_hierarchy_selection=projected.append,
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

    callbacks["selection"](selected_object)
    assert service.snapshot.owner_id == "ui_editor"
    assert service.snapshot.primary == SelectionTarget.scene_object(42)
