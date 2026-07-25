from __future__ import annotations

from types import SimpleNamespace

import pytest

from Infernux.core.anim_state_machine import AnimParameter, AnimStateMachine
from Infernux.engine.ui import animfsm_editor_panel as animfsm_module
from Infernux.engine.ui.animfsm_editor_panel import AnimFSMEditorPanel
from Infernux.engine.ui.node_graph_view import NodeCreationEntry, NodeGraphView
from Infernux.engine.ui.graph_document_authoring import _canvas_definition
from Infernux.graph.registry import COMMON_NODE_REGISTRY
import Infernux.particle.nodes  # noqa: F401 - registers particle node definitions


@pytest.fixture(autouse=True)
def _isolate_animfsm_panel_dirty_tracking():
    from Infernux.engine.project_context import clear_panel_tracking

    clear_panel_tracking("animfsm_editor")
    try:
        yield
    finally:
        clear_panel_tracking("animfsm_editor")


class _ToolbarContext:
    def __init__(self) -> None:
        self.semantic_items: list[tuple] = []

    @staticmethod
    def button(*_args, **_kwargs) -> bool:
        return False

    @staticmethod
    def same_line(*_args) -> None:
        pass

    @staticmethod
    def label(_text: str) -> None:
        pass

    @staticmethod
    def set_next_item_width(_width: float) -> None:
        pass

    @staticmethod
    def text_input(_label: str, value: str, _length: int) -> str:
        return value

    @staticmethod
    def combo(_label: str, index: int, _items: list[str], _count: int) -> int:
        return index

    def record_semantic_item(self, *args) -> None:
        self.semantic_items.append(args)


class _MenuContext:
    def __init__(self, menu_open: bool = False) -> None:
        self.semantic_items: list[tuple] = []
        self.menu_open = menu_open

    @staticmethod
    def get_mouse_pos_x() -> float:
        return 0.0

    @staticmethod
    def get_mouse_pos_y() -> float:
        return 0.0

    def begin_menu(self, _label: str) -> bool:
        return self.menu_open

    @staticmethod
    def end_menu() -> None:
        pass

    @staticmethod
    def menu_item(*_args) -> bool:
        return False

    @staticmethod
    def separator() -> None:
        pass

    def record_semantic_item(
        self, kind: str, label: str, enabled: bool, semantic_id: str, **values
    ) -> None:
        self.semantic_items.append((kind, label, enabled, semantic_id, values))


class _ConfirmationContext:
    def __init__(self, clicked: str = "") -> None:
        self.clicked = clicked
        self.semantic_items: list[tuple[str, str, bool, str]] = []
        self.semantic_windows: list[tuple[str, str, str]] = []
        self.opened = ""
        self.closed = False

    def open_popup(self, popup_id: str) -> None:
        self.opened = popup_id

    @staticmethod
    def begin_popup_modal(_popup_id: str, _flags: int) -> bool:
        return True

    def record_semantic_window(self, kind: str, label: str, semantic_id: str) -> None:
        self.semantic_windows.append((kind, label, semantic_id))

    @staticmethod
    def label(_text: str) -> None:
        pass

    @staticmethod
    def text_wrapped(_text: str) -> None:
        pass

    @staticmethod
    def spacing() -> None:
        pass

    @staticmethod
    def separator() -> None:
        pass

    def button(self, label: str, callback, **_kwargs) -> bool:
        if label.startswith(self.clicked):
            callback()
            return True
        return False

    def record_semantic_item(self, kind: str, label: str, enabled: bool, semantic_id: str) -> None:
        self.semantic_items.append((kind, label, enabled, semantic_id))

    @staticmethod
    def same_line() -> None:
        pass

    def close_current_popup(self) -> None:
        self.closed = True

    @staticmethod
    def end_popup() -> None:
        pass


class _DetailCheckboxContext:
    def __init__(self) -> None:
        self.semantic_items = []

    @staticmethod
    def same_line(*_args) -> None:
        pass

    @staticmethod
    def get_content_region_avail_width() -> float:
        return 100.0

    @staticmethod
    def get_cursor_pos_x() -> float:
        return 0.0

    @staticmethod
    def set_cursor_pos_x(_value: float) -> None:
        pass

    @staticmethod
    def checkbox(_label: str, value: bool) -> bool:
        return value

    def record_semantic_item(self, *args) -> None:
        self.semantic_items.append(args)


class _TransitionDetailContext:
    def __init__(self, transition_exit_time: float | None = None) -> None:
        self.semantic_items = []
        self.transition_exit_time = transition_exit_time

    @staticmethod
    def push_style_color(*_args) -> None:
        pass

    @staticmethod
    def pop_style_color(*_args) -> None:
        pass

    @staticmethod
    def label(*_args) -> None:
        pass

    @staticmethod
    def separator() -> None:
        pass

    @staticmethod
    def dummy(*_args) -> None:
        pass

    @staticmethod
    def set_next_item_width(*_args) -> None:
        pass

    def drag_float(self, label, value, *_args):
        if label == "##transition_exit_time" and self.transition_exit_time is not None:
            return self.transition_exit_time
        return value

    @staticmethod
    def combo(_label, index, *_args):
        return index

    @staticmethod
    def push_id(*_args) -> None:
        pass

    @staticmethod
    def pop_id() -> None:
        pass

    @staticmethod
    def same_line(*_args) -> None:
        pass

    @staticmethod
    def begin_group() -> None:
        pass

    @staticmethod
    def end_group() -> None:
        pass

    @staticmethod
    def button(*_args, **_kwargs) -> bool:
        return False

    def record_semantic_item(self, *args) -> None:
        self.semantic_items.append(args)


def test_animfsm_toolbar_exposes_stable_semantic_ids():
    panel = AnimFSMEditorPanel.__new__(AnimFSMEditorPanel)
    panel._fsm = AnimStateMachine(name="Locomotion")
    panel._fsm.mode = "3d"
    panel._file_path = "Assets/Locomotion.animfsm"
    panel._dirty = True
    ctx = _ToolbarContext()

    panel._render_toolbar(ctx)

    semantic_ids = {item[3] for item in ctx.semantic_items}
    assert {
        "animfsm.toolbar.new",
        "animfsm.toolbar.save",
        "animfsm.toolbar.name",
        "animfsm.toolbar.mode",
        "animfsm.document.path",
        "animfsm.document.dirty",
    } <= semantic_ids
    by_id = {item[3]: item for item in ctx.semantic_items}
    assert by_id["animfsm.toolbar.name"][6] == "Locomotion"
    assert by_id["animfsm.toolbar.mode"][6] == "3d"
    assert by_id["animfsm.document.path"][6] == "Assets/Locomotion.animfsm"
    assert by_id["animfsm.document.dirty"][4] is True


def test_animfsm_parameter_add_exposes_stable_semantic_id():
    panel = AnimFSMEditorPanel.__new__(AnimFSMEditorPanel)
    panel._fsm = AnimStateMachine(name="Locomotion")
    panel._graph = SimpleNamespace(find_node=lambda _uid: None)
    panel._selected_uid = ""
    ctx = _ToolbarContext()
    ctx.push_style_color = lambda *_args: None
    ctx.pop_style_color = lambda *_args: None
    ctx.separator = lambda: None
    ctx.dummy = lambda *_args: None

    panel._render_variables_panel(ctx)

    assert "animfsm.parameters.add" in {item[3] for item in ctx.semantic_items}


def test_node_graph_inline_overlay_submits_layout_item_after_cursor_restore():
    class _InlineOverlayContext:
        def __init__(self) -> None:
            self.cursor_x = 17.0
            self.cursor_y = 29.0
            self.dummy_calls: list[tuple[float, float]] = []

        def get_cursor_pos_x(self) -> float:
            return self.cursor_x

        def get_cursor_pos_y(self) -> float:
            return self.cursor_y

        def set_cursor_pos_x(self, value: float) -> None:
            self.cursor_x = value

        def set_cursor_pos_y(self, value: float) -> None:
            self.cursor_y = value

        @staticmethod
        def set_window_font_scale(_value: float) -> None:
            pass

        @staticmethod
        def push_style_var_vec2(_style, _x: float, _y: float) -> None:
            pass

        @staticmethod
        def push_style_var_float(_style, _value: float) -> None:
            pass

        @staticmethod
        def pop_style_var(_count: int = 1) -> None:
            pass

        def dummy(self, width: float, height: float) -> None:
            self.dummy_calls.append((width, height))

    view = NodeGraphView()
    view.graph = SimpleNamespace(nodes=[])
    view.zoom = 1.0
    view._layouts = {}
    ctx = _InlineOverlayContext()

    view._draw_inline_fields(ctx)

    assert (ctx.cursor_x, ctx.cursor_y) == (17.0, 29.0)
    assert ctx.dummy_calls == [(0.0, 0.0)]


def test_node_graph_inline_enum_records_combo_semantics():
    class _InlineEnumContext:
        def __init__(self) -> None:
            self.semantic_items: list[tuple] = []

        @staticmethod
        def set_cursor_pos_x(_value: float) -> None:
            pass

        @staticmethod
        def set_cursor_pos_y(_value: float) -> None:
            pass

        @staticmethod
        def push_id_str(_value: str) -> None:
            pass

        @staticmethod
        def pop_id() -> None:
            pass

        @staticmethod
        def set_next_item_width(_value: float) -> None:
            pass

        @staticmethod
        def combo(_label: str, index: int, _items: list[str], _count: int) -> int:
            return index

        @staticmethod
        def is_item_hovered() -> bool:
            return False

        @staticmethod
        def is_item_active() -> bool:
            return False

        def record_semantic_item(self, *args, **kwargs) -> None:
            self.semantic_items.append((args, kwargs))

    node = SimpleNamespace(uid="sprite", data={"alignment": "camera_plane"})
    layout = SimpleNamespace(node=node, sx=0.0, w=200.0)
    field = SimpleNamespace(
        id="alignment",
        default="camera_plane",
        label="Alignment",
        data_type="string",
        enum_values=("camera_plane", "camera_position", "axis", "velocity"),
    )
    view = NodeGraphView()
    view._origin_x = 0.0
    view._origin_y = 0.0
    view.zoom = 1.0
    view._semantic_capture_active = True
    ctx = _InlineEnumContext()

    view._draw_inline_field(ctx, layout, field, 40.0)

    assert ctx.semantic_items == [
        (
            (
                "combo",
                "Alignment",
                True,
                "node_graph.inline.sprite.alignment",
            ),
            {"string_value": "camera_plane"},
        )
    ]


def test_particle_sprite_canvas_preserves_enum_and_conditional_field_metadata():
    definition = COMMON_NODE_REGISTRY.get("particle.output.sprite")
    assert definition is not None
    canvas = _canvas_definition(definition)
    fields = {field.id: field for field in canvas.inline_fields}

    assert fields["alignment"].enum_values == (
        "camera_plane",
        "camera_position",
        "axis",
        "velocity",
    )
    assert fields["alignment_axis"].visible_when_field == "alignment"
    assert fields["alignment_axis"].visible_when_value == "axis"


def test_animfsm_dirty_mode_switch_defers_to_editor_owned_confirmation():
    panel = AnimFSMEditorPanel.__new__(AnimFSMEditorPanel)
    panel._dirty = True
    panel._pending_mode_switch = None
    panel._mode_switch_confirm_requested = False
    panel._mode_switch_waiting_for_save = False

    panel._switch_to_new_mode_resource("3d")

    assert panel._pending_mode_switch == "3d"
    assert panel._mode_switch_confirm_requested is True
    assert panel._mode_switch_waiting_for_save is False


def test_animfsm_mode_switch_confirmation_is_semantic_and_cancelable():
    panel = AnimFSMEditorPanel.__new__(AnimFSMEditorPanel)
    panel._pending_mode_switch = "3d"
    panel._mode_switch_confirm_requested = True
    panel._mode_switch_waiting_for_save = False
    ctx = _ConfirmationContext(clicked=animfsm_module.t("editor.unsaved.cancel"))

    panel._render_mode_switch_confirmation(ctx)

    assert ctx.opened.endswith("###animfsm_mode_switch_confirm")
    assert ctx.closed is True
    assert panel._pending_mode_switch is None
    assert {item[3] for item in ctx.semantic_items} == {
        "animfsm.mode_switch.save",
        "animfsm.mode_switch.discard",
        "animfsm.mode_switch.cancel",
    }
    assert ctx.semantic_windows == [
        (
            "modal",
            animfsm_module.t("animfsm.mode_switch.title"),
            "animfsm.mode_switch.dialog",
        )
    ]


def test_animfsm_clean_mode_switch_starts_blank_and_clears_stale_selection():
    panel = AnimFSMEditorPanel()
    panel._fsm.add_state("SavedState")
    panel._sync_graph_from_fsm()
    selected_uid = panel._name_to_uid["SavedState"]
    panel._view.selected_nodes = [selected_uid]
    panel._view.selected_link = "stale-link"
    panel._dirty = False

    panel._switch_to_new_mode_resource("3d")

    assert panel._fsm.mode == "3d"
    assert panel._fsm.states == []
    assert panel._view.selected_nodes == []
    assert panel._view.selected_link == ""


def test_animfsm_selection_only_click_does_not_mark_resource_dirty():
    panel = AnimFSMEditorPanel()
    panel._fsm.add_state("State 0")
    panel._sync_graph_from_fsm()
    uid = panel._name_to_uid["State 0"]
    panel._dirty = False

    panel._view.selected_nodes = [uid]
    panel._on_node_selected(uid)
    panel._on_node_drag_start(uid)
    panel._on_node_drag_end(uid)

    assert panel._selected_uid == uid
    assert panel._dirty is False


def test_animfsm_detail_checkboxes_publish_distinct_values(monkeypatch):
    panel = AnimFSMEditorPanel.__new__(AnimFSMEditorPanel)
    ctx = _DetailCheckboxContext()
    monkeypatch.setattr(animfsm_module, "field_label", lambda *_args: None)

    assert panel._detail_checkbox_row_right(
        ctx, 20.0, "animfsm_editor.loop", "##loop", True, "animfsm.state.loop",
    ) is True
    assert panel._detail_checkbox_row_right(
        ctx,
        20.0,
        "animfsm_editor.restart_same_clip",
        "##restart",
        False,
        "animfsm.state.restart_same_clip",
    ) is False

    by_id = {item[3]: item for item in ctx.semantic_items}
    assert by_id["animfsm.state.loop"][4] is True
    assert by_id["animfsm.state.restart_same_clip"][4] is False


def test_animfsm_clip_reference_publishes_domain_semantic(monkeypatch):
    panel = AnimFSMEditorPanel()
    state = panel._fsm.add_state("Countdown")
    panel._sync_graph_from_fsm()
    node = panel._graph.find_node(panel._name_to_uid["Countdown"])
    captured = {}

    monkeypatch.setattr(animfsm_module, "field_label", lambda *_args: None)
    monkeypatch.setattr(
        animfsm_module,
        "render_object_field",
        lambda *_args, **kwargs: captured.update(kwargs) or False,
    )

    panel._render_clip_reference_row(SimpleNamespace(), state, node, 20.0)

    assert captured["semantic_id"] == "animfsm.state.clip"


def test_animfsm_selected_link_renders_transition_detail_semantics():
    panel = AnimFSMEditorPanel()
    panel._fsm.add_state("Countdown")
    panel._fsm.add_state("Replay")
    panel._fsm.parameters.append(AnimParameter(name="ReplayTrigger"))
    panel._sync_graph_from_fsm()
    source_uid = panel._name_to_uid["Countdown"]
    target_uid = panel._name_to_uid["Replay"]
    panel._on_link_created(source_uid, "out", target_uid, "in")
    link = next(
        lk for lk in panel._graph.links
        if lk.source_node == source_uid and lk.target_node == target_uid
    )
    link.data["condition"] = "ReplayTrigger > 0"
    link.data["cond_terms"] = [
        {"name": "ReplayTrigger", "op": ">", "value": 0.0},
    ]
    panel._selected_uid = ""
    panel._view.selected_link = link.uid
    ctx = _TransitionDetailContext(transition_exit_time=0.0)

    panel._render_detail_panel(ctx)

    semantic_ids = {item[3] for item in ctx.semantic_items}
    assert {
        "animfsm.transition.detail",
        "animfsm.transition.route",
        "animfsm.transition.exit_time",
        "animfsm.transition.duration",
        "animfsm.transition.condition_mode",
        "animfsm.transition.condition.0.parameter",
        "animfsm.transition.condition.0.operator",
        "animfsm.transition.condition.0.value",
        "animfsm.transition.condition.add",
        "animfsm.transition.condition.remove",
        "animfsm.transition.delete",
    } <= semantic_ids
    assert panel._fsm.get_state("Countdown").exit_time_normalized == 0.0


def test_node_graph_context_menu_uses_the_host_namespace():
    view = NodeGraphView()
    view.semantic_namespace = "animfsm.graph"
    view.graph = type("Graph", (), {"registered_types": lambda _self: []})()
    ctx = _MenuContext()

    view._draw_context_menu(ctx)

    semantic_ids = {item[3] for item in ctx.semantic_items}
    assert {
        "animfsm.graph.context.add_node",
        "animfsm.graph.context.center_view",
        "animfsm.graph.context.reset_zoom",
    } <= semantic_ids


def test_node_graph_inline_float32_round_trip_does_not_emit_a_change():
    import struct

    node = SimpleNamespace(uid="node", data={"gravity": [0.0, -9.81, 0.0]})
    view = NodeGraphView()
    changes = []
    view.on_node_data_changed = lambda *args: changes.append(args)
    float32 = lambda value: struct.unpack("f", struct.pack("f", value))[0]

    view._commit_inline_value(
        node,
        "gravity",
        [float32(value) for value in node.data["gravity"]],
    )

    assert changes == []

    view._commit_inline_value(node, "gravity", [0.0, -8.5, 0.0])
    assert changes == [("node", "gravity", [0.0, -9.81, 0.0], [0.0, -8.5, 0.0])]


def test_node_graph_drop_on_occupied_input_requests_atomic_replacement():
    from Infernux.core.node_graph import (
        NodeGraph,
        NodeTypeDef,
        PinDef,
        PinKind,
    )

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
    first = graph.add_node("source", uid="first")
    second = graph.add_node("source", uid="second")
    target = graph.add_node("target", uid="target")
    original = graph.add_link(first.uid, "out", target.uid, "in")

    replaced = []
    view = NodeGraphView()
    view.graph = graph
    view.on_link_replaced = lambda *args: replaced.append(args)
    view._drag_src_node = second.uid
    view._drag_src_pin = "out"
    view._drag_src_kind = PinKind.OUTPUT
    view._hit_test_pin = lambda _x, _y: (target.uid, "in", PinKind.INPUT)

    view._try_complete_link(100.0, 100.0)

    assert replaced == [(original.uid, second.uid, "out", target.uid, "in")]


def test_node_graph_open_add_menu_preserves_open_state_on_domain_semantic():
    view = NodeGraphView()
    view.semantic_namespace = "vfx.graph"
    view.graph = type("Graph", (), {"registered_types": lambda _self: []})()
    ctx = _MenuContext(menu_open=True)

    view._draw_context_menu(ctx)

    by_id = {item[3]: item for item in ctx.semantic_items}
    assert by_id["vfx.graph.context.add_node"][4] == {"bool_value": True}


def test_animfsm_uses_shared_node_creation_palette_for_domain_variants():
    panel = AnimFSMEditorPanel()

    assert panel._view.on_link_dropped_empty is None
    entries = panel._view._creation_entries(
        {"source_node": panel._entry_uid, "source_kind": animfsm_module.PinKind.OUTPUT}
    )

    assert [entry.key for entry in entries] == ["clip", "blend"]
    assert all(isinstance(entry, NodeCreationEntry) for entry in entries)


def test_node_graph_center_view_fits_full_node_bounds_inside_canvas():
    typedef = SimpleNamespace(
        min_width=170.0,
        body_bottom_pad=0.0,
        input_pins=lambda: [object(), object(), object()],
        output_pins=lambda: [object()],
    )
    nodes = [
        SimpleNamespace(uid="left", type_id="node", pos_x=40.0, pos_y=80.0),
        SimpleNamespace(uid="right", type_id="node", pos_x=700.0, pos_y=80.0),
    ]
    view = NodeGraphView()
    view.graph = SimpleNamespace(nodes=nodes, get_type=lambda _type_id: typedef)
    view._canvas_w = 500.0
    view._canvas_h = 300.0
    view.zoom = 1.0

    view.center_on_nodes()

    left = nodes[0].pos_x * view.zoom + view.pan_x
    right = (nodes[1].pos_x + typedef.min_width) * view.zoom + view.pan_x
    assert 0.3 <= view.zoom < 1.0
    assert left >= 31.0
    assert right <= 469.0


def test_node_graph_exports_drawn_nodes_as_explicit_semantic_rects():
    view = NodeGraphView()
    view.semantic_namespace = "animfsm.graph"
    layout = SimpleNamespace(
        node=SimpleNamespace(uid="state-uid", data={"label": "State 0"}),
        typedef=SimpleNamespace(label="Animation State"),
        sx=12.0,
        sy=34.0,
        w=140.0,
        h=72.0,
        input_pins=[],
        output_pins=[],
    )
    view._layouts = {"state-uid": layout}
    view._draw_one_node = lambda _ctx, _layout: None
    recorded = []
    ctx = SimpleNamespace(
        record_semantic_rect=lambda *args: recorded.append(args)
    )

    view._draw_nodes(ctx)

    assert recorded[0] == (
        "node_graph_node",
        "State 0",
        12.0,
        34.0,
        140.0,
        72.0,
        True,
        "animfsm.graph.node.state-uid",
    )
    assert recorded[1][0] == "node_graph_node_drag_handle"
    assert recorded[1][6:] == (True, "animfsm.graph.node.state-uid.drag")


def test_node_graph_drag_handle_uses_a_reachable_point_when_nodes_overlap():
    view = NodeGraphView()
    view.semantic_namespace = "animfsm.graph"
    typedef = SimpleNamespace(label="Animation State")
    lower = SimpleNamespace(
        node=SimpleNamespace(uid="countdown", data={"label": "Countdown"}),
        typedef=typedef,
        sx=100.0,
        sy=100.0,
        w=155.0,
        h=62.0,
        input_pins=[],
        output_pins=[],
    )
    upper = SimpleNamespace(
        node=SimpleNamespace(uid="replay", data={"label": "Replay"}),
        typedef=typedef,
        sx=116.0,
        sy=100.0,
        w=139.0,
        h=62.0,
        input_pins=[],
        output_pins=[],
    )
    view._layouts = {"countdown": lower, "replay": upper}
    view._draw_one_node = lambda _ctx, _layout: None
    recorded = []
    ctx = SimpleNamespace(record_semantic_rect=lambda *args: recorded.append(args))

    view._draw_nodes(ctx)

    by_semantic_id = {item[7]: item for item in recorded}
    assert by_semantic_id["animfsm.graph.node.countdown"][6] is False
    countdown_handle = by_semantic_id["animfsm.graph.node.countdown.drag"]
    assert countdown_handle[6] is True
    handle_center_x = countdown_handle[2] + countdown_handle[4] * 0.5
    assert handle_center_x < upper.sx
    assert by_semantic_id["animfsm.graph.node.replay"][6] is True


def test_node_graph_exports_input_and_output_ports_as_semantic_rects():
    view = NodeGraphView()
    view.semantic_namespace = "animfsm.graph"
    view.zoom = 1.0
    layout = SimpleNamespace(
        node=SimpleNamespace(uid="state-uid"),
        input_pins=[
            SimpleNamespace(pin_def=SimpleNamespace(id="in", label="In"), cx=20.0, cy=30.0)
        ],
        output_pins=[
            SimpleNamespace(pin_def=SimpleNamespace(id="out", label="Out"), cx=120.0, cy=30.0)
        ],
    )
    recorded = []
    ctx = SimpleNamespace(record_semantic_rect=lambda *args: recorded.append(args))

    view._record_pin_semantics(ctx, layout, "Countdown")

    assert [item[0] for item in recorded] == ["node_graph_port", "node_graph_port"]
    assert [item[7] for item in recorded] == [
        "animfsm.graph.port.state-uid.input.in",
        "animfsm.graph.port.state-uid.output.out",
    ]
    assert recorded[0][2:6] == (9.0, 19.0, 22.0, 22.0)
    assert recorded[1][2:6] == (109.0, 19.0, 22.0, 22.0)


def test_node_graph_exports_link_hit_point_as_semantic_rect():
    view = NodeGraphView()
    view.semantic_namespace = "animfsm.graph"
    source_node = SimpleNamespace(uid="source", data={"label": "Countdown"})
    target_node = SimpleNamespace(uid="target", data={"label": "Replay"})
    link = SimpleNamespace(
        uid="link-uid",
        source_node="source",
        source_pin="out",
        target_node="target",
        target_pin="in",
    )
    view.graph = SimpleNamespace(
        links=[link],
        find_node=lambda uid: source_node if uid == "source" else target_node,
    )
    view._layouts = {
        "source": SimpleNamespace(
            output_pins=[SimpleNamespace(pin_def=SimpleNamespace(id="out"), cx=100.0, cy=50.0)],
            input_pins=[],
            sx=20.0,
            sy=20.0,
            w=80.0,
            h=60.0,
        ),
        "target": SimpleNamespace(
            output_pins=[],
            input_pins=[SimpleNamespace(pin_def=SimpleNamespace(id="in"), cx=220.0, cy=70.0)],
            sx=220.0,
            sy=40.0,
            w=80.0,
            h=60.0,
        ),
    }
    view._hit_test_link = lambda *_args: "link-uid"
    view._draw_link_with_arrow = lambda *_args: None
    recorded = []
    ctx = SimpleNamespace(
        get_mouse_pos_x=lambda: 0.0,
        get_mouse_pos_y=lambda: 0.0,
        record_semantic_rect=lambda *args: recorded.append(args),
    )

    view._semantic_capture_active = False
    view._draw_links(ctx)
    assert recorded == []

    view._semantic_capture_active = True
    view._draw_links(ctx)

    assert len(recorded) == 1
    assert recorded[0][0] == "node_graph_link"
    assert recorded[0][1] == "Countdown to Replay"
    assert recorded[0][7] == "animfsm.graph.link.link-uid"
    assert recorded[0][4:6] == (14.0, 14.0)


def test_animfsm_3d_clip_picker_includes_embedded_model_takes(monkeypatch):
    from Infernux.core import asset_types
    from Infernux.core.assets import AssetManager

    model_path = "Assets/Models/Racer.fbx"
    monkeypatch.setattr(
        AssetManager,
        "find_assets",
        classmethod(lambda _cls, pattern: [model_path] if pattern == "*.fbx" else []),
    )
    monkeypatch.setattr(
        asset_types,
        "read_meta_file",
        lambda path: {"animation_names_csv": "Idle, Drive"} if path == model_path else {},
    )
    monkeypatch.setattr(
        asset_types,
        "read_meta_guid",
        lambda path: "a" * 32 if path == model_path else "",
    )

    items = AnimFSMEditorPanel._embedded_clip3d_picker_items("drive")

    assert items == [("Racer | Drive", f"{'a' * 32}::subanim:1")]


def test_animfsm_new_document_and_dirty_draft_round_trip():
    panel = AnimFSMEditorPanel()
    assert panel._dirty is True
    panel._fsm.name = "Recovered FSM"
    panel._fsm.parameters.append(AnimParameter(name="speed"))

    restored = AnimFSMEditorPanel()
    restored.load_state(panel.save_state())

    assert restored._dirty is True
    assert restored._fsm.name == "Recovered FSM"
    assert [parameter.name for parameter in restored._fsm.parameters] == ["speed"]


def test_animfsm_entering_play_does_not_implicitly_save_dirty_draft(monkeypatch):
    panel = AnimFSMEditorPanel()
    panel._dirty = True
    save_calls = []
    monkeypatch.setattr(panel, "_do_save", lambda: save_calls.append(True))

    panel._on_play_mode_changed(SimpleNamespace(new_state="playing"))

    assert save_calls == []
    assert panel._dirty is True
