from types import SimpleNamespace

from Infernux.core.node_graph import NodeGraph, NodeTypeDef, PinDef, PinKind
from Infernux.engine.interaction import (
    CommandSource,
    EditorCommand,
    EditorCommandRegistry,
    EditorInteractionCore,
    FocusService,
    GraphElementKind,
    GraphElementRef,
    SelectionService,
    TransientInteractionService,
)
from Infernux.engine.ui.node_graph_editor_panel import (
    GraphWorkspaceEntry,
    NODE_GRAPH_PANEL_INTERACTION,
    NodeGraphEditorPanel,
)
from Infernux.engine.ui.node_graph_view import NodeGraphView
from Infernux.engine.i18n import t


class _ContextMenuProbe:
    def __init__(self, *, invoke=None, mouse=(0.0, 0.0)) -> None:
        self.items = []
        self.semantic_items = []
        self._invoke = invoke or (lambda label: label.startswith("Delete"))
        self._mouse = mouse

    def get_mouse_pos_x(self) -> float:
        return float(self._mouse[0])

    def get_mouse_pos_y(self) -> float:
        return float(self._mouse[1])

    def menu_item(self, label, shortcut, selected, enabled):
        self.items.append((label, shortcut, selected, enabled))
        return bool(enabled and self._invoke(label))

    def record_semantic_item(self, *args, **kwargs) -> None:
        self.semantic_items.append((args, kwargs))

    @staticmethod
    def separator() -> None:
        pass

    @staticmethod
    def close_current_popup() -> None:
        pass


def test_node_click_selection_replaces_without_modifier():
    assert NodeGraphView._selection_after_node_click(
        ["first", "second"],
        "third",
        additive=False,
    ) == ("third",)


def test_selection_projection_reports_real_change_only():
    view = NodeGraphView()

    assert view.project_selection(("node",)) is True
    assert view.project_selection(("node",)) is False


def test_selection_intent_does_not_mutate_view_when_authority_rejects():
    view = NodeGraphView()
    view.on_selection_changed = lambda *_args: False

    assert view.request_selection(("node",)) is False
    assert view.selected_nodes == []
    assert view.selected_link == ""


def test_selection_intent_changes_only_through_authoritative_projection():
    view = NodeGraphView()

    def accept(nodes, link, _record_history):
        return view.project_selection(nodes, link)

    view.on_selection_changed = accept
    assert view.request_selection(("node",)) is True
    assert view.selected_nodes == ["node"]


def test_ctrl_node_click_adds_and_removes_without_losing_order():
    selected = NodeGraphView._selection_after_node_click(
        ["first"],
        "second",
        additive=True,
    )
    assert selected == ("first", "second")

    assert NodeGraphView._selection_after_node_click(
        list(selected),
        "first",
        additive=True,
    ) == ("second",)


def test_node_graph_view_exposes_one_context_command_gateway():
    view = NodeGraphView()

    assert not hasattr(view, "on_can_execute_edit_command")
    assert not hasattr(view, "on_execute_edit_command")


def test_context_menu_disables_commands_rejected_by_the_global_registry():
    graph = NodeGraph()
    graph.register_type(NodeTypeDef("value", "Value"))
    node = graph.add_node("value", uid="node")
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    view.project_selection((node.uid,))
    registry = EditorCommandRegistry(
        focus=FocusService(),
        selection=SelectionService(),
    )
    for command_id, label in (
        ("edit.copy", "Copy"),
        ("edit.cut", "Cut"),
        ("edit.paste", "Paste"),
        ("edit.duplicate", "Duplicate"),
        ("edit.delete", "Delete"),
        ("graph.center_view", "Center View"),
        ("graph.reset_zoom", "Reset Zoom"),
        ("graph.add_node", "Add Node"),
    ):
        registry.register(
            EditorCommand(
                command_id,
                lambda _context: True,
                display_name=label,
                can_execute=lambda _context: False,
            )
        )
    ctx = _ContextMenuProbe()

    view._draw_context_menu(ctx)

    assert graph.find_node(node.uid) is node
    assert view.selected_nodes == [node.uid]
    delete = next(item for item in ctx.items if item[0] == "Delete")
    assert delete[3] is False
    assert all(item[3] is False for item in ctx.items[:-2])


def test_context_add_node_routes_coordinates_and_creation_payload_through_commands():
    core = EditorInteractionCore()
    from Infernux.engine.ui.editor_services import EditorServices

    EditorServices.instance()._interaction_core = core

    class _Panel(NodeGraphEditorPanel):
        def __init__(self):
            self.created = []
            super().__init__(
                title="Test Graph",
                window_id="test_graph",
                semantic_namespace="test.graph",
            )

        def _on_node_add(self, type_id, x, y):
            self.created.append((type_id, x, y))
            return self._view.graph.add_node(type_id, x, y)

    panel = _Panel()
    core.panels.register_type("test_graph", NODE_GRAPH_PANEL_INTERACTION)
    core.panels.bind_view(panel.window_id, "test_graph", panel)
    core.focus.activate_panel("test_graph", view_id=panel.window_id)
    graph = NodeGraph()
    graph.register_type(NodeTypeDef("value", "Value"))
    panel._bind_node_graph_model(graph, preserve_selection=False)
    for command_id, label in (
        ("edit.copy", "Copy"),
        ("edit.cut", "Cut"),
        ("edit.paste", "Paste"),
        ("edit.duplicate", "Duplicate"),
        ("edit.delete", "Delete"),
    ):
        core.commands.register(
            EditorCommand(
                command_id,
                lambda _context: False,
                display_name=label,
                can_execute=lambda _context: False,
            )
        )
    panel._view.pan_x = 0.0
    panel._view.pan_y = 0.0
    panel._view.zoom = 1.0
    panel._view._origin_x = 0.0
    panel._view._origin_y = 0.0

    ctx = _ContextMenuProbe(
        invoke=lambda label: label == t("node_graph.add_node"),
        mouse=(125.0, 75.0),
    )
    panel._view._draw_context_menu(ctx)

    request = panel._view._node_create_request
    assert request is not None
    assert (request["gx"], request["gy"]) == (125.0, 75.0)
    assert not core.commands.can_execute(
        "graph.add_node",
        core.commands.context(
            CommandSource.CONTEXT_MENU,
            {"gx": "invalid", "gy": 75.0},
        ),
    )

    entry = panel._view._creation_entries(request)[0]
    payload = panel._view._creation_command_payload(entry, request)
    assert payload == {
        "entry_key": "value",
        "type_id": "value",
        "gx": 125.0,
        "gy": 75.0,
        "source_node": "",
        "source_pin": "",
        "source_kind": PinKind.OUTPUT.value,
    }
    assert panel._view.on_node_creation_command(payload)
    assert panel.created == [("value", 125.0, 75.0)]
    assert len(graph.nodes) == 1


def test_inline_and_header_color_edits_fail_closed_without_host_callbacks():
    graph = NodeGraph()
    graph.register_type(NodeTypeDef("value", "Value"))
    node = graph.add_node("value", uid="node", value=1.0)
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)

    view._commit_inline_value(node, "value", 2.0)

    assert node.data == {"value": 1.0}

    view._header_color_popup_node_uid = node.uid
    view._layouts[node.uid] = SimpleNamespace()
    view._resolve_node_header_color = lambda _layout: (1.0, 1.0, 1.0, 1.0)
    color_context = SimpleNamespace(
        begin_popup=lambda _popup_id: True,
        color_picker=lambda *_args: (True, 0.2, 0.3, 0.4, 1.0),
        end_popup=lambda: None,
    )

    view._draw_header_color_popup(color_context)

    assert "header_color" not in node.data


def test_link_gestures_fail_closed_without_host_mutation_callbacks():
    graph = NodeGraph()
    graph.register_type(
        NodeTypeDef(
            "source",
            "Source",
            pins=[PinDef("out", "Out", PinKind.OUTPUT, data_type="float")],
        )
    )
    graph.register_type(
        NodeTypeDef(
            "target",
            "Target",
            pins=[PinDef("in", "In", PinKind.INPUT, data_type="float")],
        )
    )
    source = graph.add_node("source", uid="source")
    replacement = graph.add_node("source", uid="replacement")
    target = graph.add_node("target", uid="target")
    empty_target = graph.add_node("target", uid="empty-target")
    original = graph.add_link(source.uid, "out", target.uid, "in")
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    view._reconnect_link_uid = original.uid
    view._drag_src_node = replacement.uid
    view._drag_src_pin = "out"
    view._drag_src_kind = PinKind.OUTPUT
    view._hit_test_pin = lambda _x, _y: (target.uid, "in", PinKind.INPUT)

    view._try_complete_link(100.0, 100.0)

    assert graph.links == [original]
    assert (original.source_node, original.target_node) == (source.uid, target.uid)

    view._reconnect_link_uid = ""
    view._hit_test_pin = lambda _x, _y: (empty_target.uid, "in", PinKind.INPUT)
    view._try_complete_link(100.0, 100.0)

    assert graph.links == [original]


def test_palette_creation_fails_closed_without_a_host_mutation_callback():
    graph = NodeGraph()
    graph.register_type(NodeTypeDef("value", "Value"))
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    entry = view._default_creation_entries({})[0]

    view._create_from_palette(
        entry,
        {
            "gx": 12.0,
            "gy": 24.0,
            "source_node": "",
            "source_pin": "",
            "source_kind": PinKind.OUTPUT,
        },
    )

    assert graph.nodes == []


def test_palette_auto_connect_fails_before_partial_creation_without_link_route():
    graph = NodeGraph()
    graph.register_type(
        NodeTypeDef(
            "source",
            "Source",
            pins=[PinDef("out", "Out", PinKind.OUTPUT, data_type="float")],
        )
    )
    graph.register_type(
        NodeTypeDef(
            "target",
            "Target",
            pins=[PinDef("in", "In", PinKind.INPUT, data_type="float")],
        )
    )
    source = graph.add_node("source", uid="source")
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    added = []
    view.on_node_add_request = lambda *_args: added.append(_args)
    entry = next(
        item
        for item in view._default_creation_entries(
            {
                "source_node": source.uid,
                "source_pin": "out",
                "source_kind": PinKind.OUTPUT,
            }
        )
        if item.type_id == "target"
    )

    view._create_from_palette(
        entry,
        {
            "gx": 12.0,
            "gy": 24.0,
            "source_node": source.uid,
            "source_pin": "out",
            "source_kind": PinKind.OUTPUT,
        },
    )

    assert added == []
    assert graph.nodes == [source]


def test_node_drag_fails_closed_without_a_complete_transaction_route():
    graph = NodeGraph()
    graph.register_type(NodeTypeDef("value", "Value"))
    node = graph.add_node("value", uid="node")
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    view._dragging_node = True
    view._drag_node_id = node.uid
    ctx = SimpleNamespace(
        get_mouse_pos_x=lambda: 100.0,
        get_mouse_pos_y=lambda: 100.0,
        is_mouse_button_down=lambda _button: True,
        get_mouse_drag_delta_x=lambda _button: 20.0,
        get_mouse_drag_delta_y=lambda _button: 10.0,
        reset_mouse_drag_delta=lambda _button: None,
    )

    view._handle_interaction(ctx, True, 640.0, 360.0)

    assert (node.pos_x, node.pos_y) == (0.0, 0.0)
    assert not view._dragging_node
    assert view._drag_node_id == ""


def test_bind_graph_preserves_only_stable_elements_that_still_exist():
    first = NodeGraph()
    first.register_type(NodeTypeDef("value", "Value"))
    first.add_node("value", uid="keep")
    first.add_node("value", uid="drop")
    view = NodeGraphView()
    view.bind_graph(first, preserve_selection=False)
    view.project_selection(("keep", "drop"))

    replacement = NodeGraph()
    replacement.register_type(NodeTypeDef("value", "Value"))
    replacement.add_node("value", uid="keep")
    view.bind_graph(replacement)

    assert view.graph is replacement
    assert view.selected_nodes == ["keep"]
    assert view.selected_link == ""


def test_bind_graph_can_explicitly_clear_selection():
    graph = NodeGraph()
    graph.register_type(NodeTypeDef("value", "Value"))
    graph.add_node("value", uid="node")
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    view.project_selection(("node",))

    view.bind_graph(graph, preserve_selection=False)

    assert view.selected_nodes == []


def test_workspace_rename_and_delete_use_shared_graph_edit_commands():
    class _Panel(NodeGraphEditorPanel):
        def __init__(self):
            super().__init__(
                title="Test Graph",
                window_id="test_graph",
                semantic_namespace="test.graph",
            )
            self.deleted = []
            self._view.graph = NodeGraph()

        def _node_graph_workspace_delete(self, element):
            self.deleted.append(element)
            return True

    focus = FocusService()
    focus.activate_panel("test_graph", record_history=False)
    transients = TransientInteractionService(focus)
    panel = _Panel()
    element = GraphElementRef(GraphElementKind.PARAMETER, "speed")
    entry = GraphWorkspaceEntry(
        element,
        "Speed",
        can_rename=True,
        can_delete=True,
    )
    panel._workspace_entries[element] = entry
    panel._workspace_entry_sections[element] = "parameters"
    panel._workspace_section_elements["parameters"] = {element}
    panel._workspace_collection("parameters").set_items(("parameter:speed",))
    panel._graph_selection = type(
        "Selection",
        (),
        {
            "primary": element,
            "elements": (element,),
            "select": lambda self, *_args, **_kwargs: True,
        },
    )()

    assert panel.can_edit_rename()
    assert panel.command_edit_rename()
    assert panel._workspace_collection("parameters").rename_session.item_id == "parameter:speed"
    assert transients.active is not None
    assert transients.active.kind == "graph_workspace_rename"
    assert focus.snapshot.child_context_id == transients.CONTEXT_ID

    assert transients.cancel_active()
    assert panel._workspace_collection("parameters").rename_session is None
    assert focus.snapshot.child_context_id == ""
    assert panel.can_edit_delete()
    assert panel.command_edit_delete()
    assert panel.deleted == [element]


def test_context_menu_capability_query_does_not_mutate_global_focus():
    from Infernux.engine.interaction import ContextMenuBuilder, ContextMenuCommand

    focus = FocusService()
    focus.activate_panel("project", record_history=False)
    registry = EditorCommandRegistry(focus=focus, selection=SelectionService())
    registry.register(EditorCommand("edit.copy", lambda _context: True))

    resolved = ContextMenuBuilder(registry).resolve(
        (ContextMenuCommand("edit.copy"),)
    )

    assert resolved[0].enabled
    assert focus.snapshot.active_panel_id == "project"


def test_node_drag_is_a_global_transient_and_cancel_restores_the_graph() -> None:
    class _Panel(NodeGraphEditorPanel):
        def __init__(self):
            super().__init__(
                title="Test Graph",
                window_id="test_graph",
                semantic_namespace="test.graph",
            )

    focus = FocusService()
    focus.activate_panel("test_graph", record_history=False)
    transients = TransientInteractionService(focus)
    panel = _Panel()
    graph = NodeGraph()
    graph.register_type(NodeTypeDef("value", "Value"))
    node = graph.add_node("value", uid="node")
    panel._view.bind_graph(graph, preserve_selection=False)
    panel._view._dragging_node = True
    panel._view._drag_node_id = node.uid

    panel._on_node_drag_start(node.uid)
    node.pos_x = 120.0
    node.pos_y = 48.0

    assert transients.active is not None
    assert transients.active.kind == "graph_node_drag"
    assert transients.cancel_active()
    restored = graph.find_node(node.uid)
    assert restored is not None
    assert (restored.pos_x, restored.pos_y) == (0.0, 0.0)
    assert not panel._view._dragging_node
    assert panel._node_graph_drag_snapshot is None


def test_completed_node_drag_releases_global_transient_before_commit() -> None:
    class _Panel(NodeGraphEditorPanel):
        def __init__(self):
            super().__init__(
                title="Test Graph",
                window_id="test_graph",
                semantic_namespace="test.graph",
            )
            self.committed = []

        def _commit_node_graph_change(self, description, before, **kwargs):
            self.committed.append((description, before, kwargs))
            return True

    focus = FocusService()
    focus.activate_panel("test_graph", record_history=False)
    transients = TransientInteractionService(focus)
    panel = _Panel()
    graph = NodeGraph()
    graph.register_type(NodeTypeDef("value", "Value"))
    node = graph.add_node("value", uid="node")
    panel._view.bind_graph(graph, preserve_selection=False)

    panel._on_node_drag_start(node.uid)
    node.pos_x = 10.0
    panel._on_node_drag_end(node.uid)

    assert transients.active is None
    assert panel._node_graph_drag_cancel_token == ""
    assert len(panel.committed) == 1
