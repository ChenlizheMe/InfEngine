from types import SimpleNamespace
from unittest.mock import MagicMock

from Infernux.core.node_graph import (
    NodeGraph,
    NodeInlineFieldDef,
    NodeTypeDef,
    PinDef,
    PinKind,
)
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
from Infernux.engine.ui.inspector_utils import render_color_value_bar
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


class _ColorBarProbe:
    def __init__(self):
        self.size = None

    def invisible_button(self, _label, width, height):
        self.size = (width, height)
        return False

    @staticmethod
    def get_item_rect_min_x():
        return 0.0

    @staticmethod
    def get_item_rect_min_y():
        return 0.0

    def get_item_rect_max_x(self):
        return float(self.size[0])

    def get_item_rect_max_y(self):
        return float(self.size[1])

    @staticmethod
    def is_item_hovered():
        return False

    @staticmethod
    def is_mouse_button_down(_button):
        return False

    @staticmethod
    def draw_filled_rect(*_args):
        pass

    @staticmethod
    def draw_rect(*_args):
        pass

    @staticmethod
    def begin_popup(_popup_id):
        return False


def test_shared_color_bar_supports_node_sized_rgba_fields():
    ctx = _ColorBarProbe()

    value = render_color_value_bar(
        ctx,
        "node_color",
        [0.1, 0.2, 0.3],
        allow_hdr=True,
        width=144.0,
        height=21.0,
    )

    assert ctx.size == (144.0, 21.0)
    assert value == [0.1, 0.2, 0.3, 1.0]


def test_node_inline_color_uses_color_bar_instead_of_xyzw(monkeypatch):
    import Infernux.engine.ui.node_graph_view as node_graph_view

    calls = []

    def render_color(ctx, widget_id, value, **kwargs):
        calls.append((ctx, widget_id, list(value), kwargs))
        return list(value)

    monkeypatch.setattr(node_graph_view, "render_color_value_bar", render_color)
    view = NodeGraphView()
    ctx = MagicMock()
    ctx.get_mouse_pos_x.return_value = 0.0
    ctx.get_mouse_pos_y.return_value = 0.0
    ctx.is_item_active.return_value = False
    ctx.is_item_hovered.return_value = False
    node = SimpleNamespace(
        uid="color-node",
        data={"color": [0.2, 0.4, 0.6, 0.8]},
    )
    layout = SimpleNamespace(node=node, sx=240.0, sy=80.0, w=180.0)
    field = SimpleNamespace(
        id="color",
        label="Color",
        data_type="color",
        default=[1.0, 1.0, 1.0, 1.0],
        enum_values=(),
    )

    view._draw_inline_field(ctx, layout, field, 120.0)

    assert calls == [
        (
            ctx,
            "##value_color-node_color",
            [0.2, 0.4, 0.6, 0.8],
            {
                "allow_hdr": True,
                "default_hdr_enabled": True,
                "width": 150.0,
                "height": 21.0,
            },
        )
    ]
    ctx.drag_float.assert_not_called()


def test_node_inline_color_follows_field_hdr_flag(monkeypatch):
    import Infernux.engine.ui.node_graph_view as node_graph_view

    calls = []

    def render_color(ctx, widget_id, value, **kwargs):
        calls.append(kwargs)
        return list(value)

    monkeypatch.setattr(node_graph_view, "render_color_value_bar", render_color)
    view = NodeGraphView()
    ctx = MagicMock()
    ctx.get_mouse_pos_x.return_value = 0.0
    ctx.get_mouse_pos_y.return_value = 0.0
    ctx.is_item_active.return_value = False
    ctx.is_item_hovered.return_value = False
    node = SimpleNamespace(uid="color-node", data={"color": [0.2, 0.4, 0.6, 0.8]})
    layout = SimpleNamespace(node=node, sx=240.0, sy=80.0, w=180.0)
    field = SimpleNamespace(
        id="color",
        label="Color",
        data_type="color",
        default=[1.0, 1.0, 1.0, 1.0],
        enum_values=(),
        hdr=False,
    )

    view._draw_inline_field(ctx, layout, field, 120.0)

    assert calls[0]["allow_hdr"] is False
    assert calls[0]["default_hdr_enabled"] is False


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


def test_reconnect_gesture_lifts_existing_input_from_its_output_source():
    link = SimpleNamespace(uid="link", source_node="source", source_pin="out")
    view = NodeGraphView()

    view._begin_link_reconnect(link, 120.0, 80.0)

    assert view._dragging_pin
    assert view._reconnect_link_uid == "link"
    assert (view._drag_src_node, view._drag_src_pin) == ("source", "out")
    assert view._drag_src_kind is PinKind.OUTPUT
    assert (view._drag_end_x, view._drag_end_y) == (120.0, 80.0)


def test_dragging_a_connected_input_pin_starts_the_shared_reconnect_gesture():
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
    target = graph.add_node("target", uid="target")
    link = graph.add_link(source.uid, "out", target.uid, "in", uid="link")
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    view._canvas_window_hovered = False
    view._inline_control_hovered = False
    view._hit_test_header_color_swatch = lambda *_args: ""
    view._hit_test_pin = lambda *_args: (target.uid, "in", PinKind.INPUT)
    ctx = SimpleNamespace(
        get_mouse_pos_x=lambda: 240.0,
        get_mouse_pos_y=lambda: 120.0,
        is_key_pressed=lambda _key: False,
        is_mouse_button_clicked=lambda button: button == 0,
    )

    view._handle_interaction(ctx, True, 640.0, 360.0)

    assert view._link_drag_uid == link.uid
    assert not view._dragging_pin

    moved = SimpleNamespace(
        get_mouse_pos_x=lambda: 260.0,
        get_mouse_pos_y=lambda: 120.0,
        is_key_pressed=lambda _key: False,
        is_mouse_button_down=lambda button: button == 0,
    )
    view._handle_interaction(moved, True, 640.0, 360.0)

    assert view._link_drag_uid == ""
    assert view._dragging_pin
    assert view._reconnect_link_uid == link.uid
    assert (view._drag_src_node, view._drag_src_pin) == (source.uid, "out")


def test_clicking_a_connected_input_without_drag_keeps_the_link_connected():
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
    target = graph.add_node("target", uid="target")
    link = graph.add_link(source.uid, "out", target.uid, "in", uid="link")
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    view._canvas_window_hovered = False
    view._inline_control_hovered = False
    view._hit_test_header_color_swatch = lambda *_args: ""
    view._hit_test_pin = lambda *_args: (target.uid, "in", PinKind.INPUT)
    pressed = SimpleNamespace(
        get_mouse_pos_x=lambda: 240.0,
        get_mouse_pos_y=lambda: 120.0,
        is_key_pressed=lambda _key: False,
        is_mouse_button_clicked=lambda button: button == 0,
    )

    view._handle_interaction(pressed, True, 640.0, 360.0)
    released = SimpleNamespace(
        get_mouse_pos_x=lambda: 240.0,
        get_mouse_pos_y=lambda: 120.0,
        is_key_pressed=lambda _key: False,
        is_mouse_button_down=lambda _button: False,
    )
    view._handle_interaction(released, True, 640.0, 360.0)

    assert view._link_drag_uid == ""
    assert not view._dragging_pin
    assert graph.links == [link]


def test_dragging_the_output_half_of_a_link_starts_the_same_reconnect_gesture():
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
    target = graph.add_node("target", uid="target")
    link = graph.add_link(source.uid, "out", target.uid, "in", uid="link")
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    view._canvas_window_hovered = False
    view._inline_control_hovered = False
    view._hit_test_header_color_swatch = lambda *_args: ""
    view._hit_test_pin = lambda *_args: ("", None, PinKind.OUTPUT)
    view._hit_test_node = lambda *_args: ""
    view._hit_test_link_with_progress = lambda *_args: (link.uid, 0.25)
    pressed = SimpleNamespace(
        get_mouse_pos_x=lambda: 140.0,
        get_mouse_pos_y=lambda: 100.0,
        is_key_pressed=lambda _key: False,
        is_mouse_button_clicked=lambda button: button == 0,
    )

    view._handle_interaction(pressed, True, 640.0, 360.0)

    assert view._link_drag_uid == link.uid
    moved = SimpleNamespace(
        get_mouse_pos_x=lambda: 160.0,
        get_mouse_pos_y=lambda: 100.0,
        is_key_pressed=lambda _key: False,
        is_mouse_button_down=lambda button: button == 0,
    )
    view._handle_interaction(moved, True, 640.0, 360.0)

    assert view._link_drag_uid == ""
    assert view._dragging_pin
    assert view._reconnect_link_uid == link.uid
    assert (view._drag_src_node, view._drag_src_pin) == (source.uid, "out")


def test_input_half_of_a_link_only_selects_and_does_not_arm_reconnect():
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
    target = graph.add_node("target", uid="target")
    link = graph.add_link(source.uid, "out", target.uid, "in", uid="link")
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    view._canvas_window_hovered = False
    view._inline_control_hovered = False
    view._hit_test_header_color_swatch = lambda *_args: ""
    view._hit_test_pin = lambda *_args: ("", None, PinKind.OUTPUT)
    view._hit_test_node = lambda *_args: ""
    view._hit_test_link_with_progress = lambda *_args: (link.uid, 0.75)
    view.on_selection_changed = (
        lambda nodes, selected_link, _record: view.project_selection(
            nodes, selected_link
        )
    )
    pressed = SimpleNamespace(
        get_mouse_pos_x=lambda: 240.0,
        get_mouse_pos_y=lambda: 120.0,
        is_key_pressed=lambda _key: False,
        is_mouse_button_clicked=lambda button: button == 0,
    )

    view._handle_interaction(pressed, True, 640.0, 360.0)

    assert view.selected_link == link.uid
    assert view._link_drag_uid == ""
    assert not view._dragging_pin


def test_reconnect_released_on_empty_space_disconnects_through_host():
    view = NodeGraphView()
    view._reconnect_link_uid = "link"
    view._drag_src_node = "source"
    view._drag_src_pin = "out"
    view._drag_src_kind = PinKind.OUTPUT
    view._hit_test_pin = lambda *_args: ("", None, PinKind.OUTPUT)
    deleted = []
    view.on_link_deleted = deleted.append

    view._try_complete_link(320.0, 180.0)

    assert deleted == ["link"]


def test_reconnect_hides_the_original_link_while_it_is_held():
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
    target = graph.add_node("target", uid="target")
    link = graph.add_link(source.uid, "out", target.uid, "in", uid="link")
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    view._layouts = {
        source.uid: SimpleNamespace(
            output_pins=[
                SimpleNamespace(pin_def=SimpleNamespace(id="out"), cx=100.0, cy=100.0)
            ],
            input_pins=[],
        ),
        target.uid: SimpleNamespace(
            output_pins=[],
            input_pins=[
                SimpleNamespace(pin_def=SimpleNamespace(id="in"), cx=300.0, cy=180.0)
            ],
        ),
    }
    drawn = []
    view._draw_link_with_arrow = lambda *_args: drawn.append(True)
    ctx = SimpleNamespace(get_mouse_pos_x=lambda: 0.0, get_mouse_pos_y=lambda: 0.0)

    view._begin_link_reconnect(link, 150.0, 120.0)
    view._draw_links(ctx)

    assert drawn == []


def test_link_hit_progress_uses_the_curve_output_half():
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
    target = graph.add_node("target", uid="target")
    link = graph.add_link(source.uid, "out", target.uid, "in", uid="link")
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    view.zoom = 1.0
    view._layouts = {
        source.uid: SimpleNamespace(
            output_pins=[
                SimpleNamespace(
                    pin_def=SimpleNamespace(id="out"), cx=100.0, cy=100.0
                )
            ],
            input_pins=[],
        ),
        target.uid: SimpleNamespace(
            output_pins=[],
            input_pins=[
                SimpleNamespace(
                    pin_def=SimpleNamespace(id="in"), cx=300.0, cy=180.0
                )
            ],
        ),
    }

    output_hit, output_progress = view._hit_test_link_with_progress(132.0, 104.0, 12.0)
    input_hit, input_progress = view._hit_test_link_with_progress(268.0, 176.0, 12.0)

    assert output_hit == link.uid
    assert output_progress <= 0.5
    assert input_hit == link.uid
    assert input_progress > 0.5


def test_link_hit_chooses_nearest_curve_instead_of_first_matching_curve():
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
    source_a = graph.add_node("source", uid="source_a")
    source_b = graph.add_node("source", uid="source_b")
    target_a = graph.add_node("target", uid="target_a")
    target_b = graph.add_node("target", uid="target_b")
    first = graph.add_link(source_a.uid, "out", target_a.uid, "in", uid="first")
    nearest = graph.add_link(source_b.uid, "out", target_b.uid, "in", uid="nearest")
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    view.zoom = 1.0

    def layout(output_y=None, input_y=None):
        return SimpleNamespace(
            output_pins=(
                [SimpleNamespace(pin_def=SimpleNamespace(id="out"), cx=100.0, cy=output_y)]
                if output_y is not None
                else []
            ),
            input_pins=(
                [SimpleNamespace(pin_def=SimpleNamespace(id="in"), cx=300.0, cy=input_y)]
                if input_y is not None
                else []
            ),
        )

    view._layouts = {
        source_a.uid: layout(output_y=100.0),
        target_a.uid: layout(input_y=100.0),
        source_b.uid: layout(output_y=104.0),
        target_b.uid: layout(input_y=104.0),
    }

    hit_uid, _progress = view._hit_test_link_with_progress(200.0, 104.0, 8.0)

    assert first.uid != nearest.uid
    assert hit_uid == nearest.uid


def test_reconnect_dropped_back_on_original_input_is_a_no_op():
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
    target = graph.add_node("target", uid="target")
    link = graph.add_link(source.uid, "out", target.uid, "in", uid="link")
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    view._reconnect_link_uid = link.uid
    view._drag_src_node = source.uid
    view._drag_src_pin = "out"
    view._drag_src_kind = PinKind.OUTPUT
    view._hit_test_pin = lambda *_args: (target.uid, "in", PinKind.INPUT)
    replaced = []
    created = []
    view.on_link_replaced = lambda *args: replaced.append(args)
    view.on_link_created = lambda *args: created.append(args)

    view._try_complete_link(300.0, 100.0)

    assert replaced == []
    assert created == []


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
        is_key_pressed=lambda _key: False,
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


def test_wiring_an_inline_input_does_not_move_the_node_body():
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
            pins=[PinDef("value", "Value", PinKind.INPUT, data_type="float")],
            inline_fields=[
                NodeInlineFieldDef("value", "Value", "float", default=0.0),
            ],
        )
    )
    source = graph.add_node("source", 40.0, 80.0, uid="source")
    target = graph.add_node("target", 220.0, 80.0, uid="target")
    view = NodeGraphView()
    view.bind_graph(graph, preserve_selection=False)
    view._origin_x = 0.0
    view._origin_y = 0.0
    view._compute_layouts()
    before = view.get_layout(target.uid)
    assert before is not None
    assert before.left_reserve > 0.0

    graph.add_link(source.uid, "out", target.uid, "value")
    view._compute_layouts()
    after = view.get_layout(target.uid)
    assert after is not None
    assert (after.sx, after.sy) == (before.sx, before.sy)
    assert after.left_reserve == before.left_reserve
    assert (target.pos_x, target.pos_y) == (220.0, 80.0)
