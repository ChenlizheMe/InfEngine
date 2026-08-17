from __future__ import annotations

from types import SimpleNamespace

import pytest

from Infernux.core.anim_state_machine import AnimParameter, AnimStateMachine
from Infernux.engine.interaction import (
    EditorCommand,
    EditorCommandRegistry,
    FocusService,
    GraphElementKind,
    GraphElementRef,
    SelectionService,
)
from Infernux.engine.ui import animfsm_editor_panel as animfsm_module
from Infernux.engine.ui.animfsm_editor_panel import AnimFSMEditorPanel
from Infernux.engine.ui.animfsm_graph_authoring import (
    FSM_ENTRY_NODE_DEF,
    FSM_ENTRY_NODE_TYPE_ID,
    FSM_STATE_NODE_DEF,
    FSM_STATE_NODE_TYPE_ID,
)
from Infernux.engine.ui.node_graph_view import NodeCreationEntry, NodeGraphView
from Infernux.engine.ui.graph_document_authoring import _canvas_definition
from Infernux.graph.registry import COMMON_NODE_REGISTRY
import Infernux.particle.nodes  # noqa: F401 - registers particle node definitions


def test_animfsm_node_schema_has_one_strict_definition_authority():
    from Infernux.core.node_graph import node_catalog

    panel = AnimFSMEditorPanel()

    assert FSM_STATE_NODE_DEF.type_id == FSM_STATE_NODE_TYPE_ID
    assert FSM_ENTRY_NODE_DEF.type_id == FSM_ENTRY_NODE_TYPE_ID
    assert panel._graph.get_type(FSM_STATE_NODE_TYPE_ID) is not None
    assert panel._graph.get_type(FSM_ENTRY_NODE_TYPE_ID) is not None
    assert panel._graph.get_type("anim_state") is None
    assert node_catalog.get_type("anim_fsm", "anim_state") is None


def test_node_graph_projects_parameter_transactions_to_exact_mutations():
    from Infernux.engine.interaction import GraphMutationKind
    from Infernux.engine.ui.node_graph_editor_panel import NodeGraphEditorPanel
    from Infernux.graph.parameter_transactions import GraphParameterTransaction
    from Infernux.graph.parameters import (
        GraphParameterCollection,
        GraphParameterDefinition,
    )

    first = GraphParameterDefinition(stable_id="first", name="First")
    second = GraphParameterDefinition(stable_id="second", name="Second")
    updated = first.with_updates({"name": "Renamed"})
    transaction = (
        GraphParameterTransaction.begin(GraphParameterCollection((first,)))
        .create(second)
        .update(updated)
        .move("second", 0)
        .delete("first")
    )

    mutations = NodeGraphEditorPanel._node_graph_parameter_mutations(transaction)

    assert [mutation.kind for mutation in mutations] == [
        GraphMutationKind.INSERT,
        GraphMutationKind.UPDATE,
        GraphMutationKind.MOVE,
        GraphMutationKind.REMOVE,
    ]
    assert [mutation.element.stable_id for mutation in mutations] == [
        "second",
        "first",
        "second",
        "first",
    ]
    assert mutations[2].before_index == 1
    assert mutations[2].after_index == 0


@pytest.fixture(autouse=True)
def _isolate_animfsm_panel_dirty_tracking():
    from Infernux.engine.interaction import DocumentRegistry
    from Infernux.engine.undo import UndoManager

    previous_manager = UndoManager.instance()
    UndoManager()
    DocumentRegistry.instance().close_view("animfsm_editor")
    try:
        yield
    finally:
        DocumentRegistry.instance().close_view("animfsm_editor")
        UndoManager._instance = previous_manager


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
    def get_content_region_avail_width() -> float:
        return 320.0

    @staticmethod
    def get_cursor_pos_x() -> float:
        return 0.0

    @staticmethod
    def set_cursor_pos_x(_value: float) -> None:
        pass

    @staticmethod
    def push_style_var_vec2(*_args) -> None:
        pass

    @staticmethod
    def pop_style_var(*_args) -> None:
        pass

    @staticmethod
    def begin_disabled(*_args) -> None:
        pass

    @staticmethod
    def end_disabled() -> None:
        pass

    @staticmethod
    def get_item_rect_min_x() -> float:
        return 0.0

    @staticmethod
    def get_item_rect_min_y() -> float:
        return 0.0

    @staticmethod
    def get_item_rect_max_x() -> float:
        return 22.0

    @staticmethod
    def get_item_rect_max_y() -> float:
        return 22.0

    @staticmethod
    def draw_image_rect(*_args) -> None:
        pass

    @staticmethod
    def draw_text_aligned(*_args) -> None:
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


    @staticmethod
    def end_popup() -> None:
        pass


def _install_node_graph_context_commands() -> None:
    registry = EditorCommandRegistry(
        focus=FocusService(),
        selection=SelectionService(),
    )
    for command_id, label, shortcut in (
        ("edit.copy", "Copy", "Ctrl+C"),
        ("edit.cut", "Cut", "Ctrl+X"),
        ("edit.paste", "Paste", "Ctrl+V"),
        ("edit.duplicate", "Duplicate", "Ctrl+D"),
        ("edit.delete", "Delete", "Delete"),
        ("graph.center_view", "Center View", ""),
        ("graph.reset_zoom", "Reset Zoom", ""),
        ("graph.add_node", "Add Node", ""),
        ("graph.create_node", "Create Node", ""),
        ("graph.workspace.add", "Add Graph Workspace Item", ""),
    ):
        registry.register(
            EditorCommand(
                command_id,
                lambda _context: False,
                display_name=label,
                default_shortcut=shortcut,
                can_execute=lambda _context: False,
            )
        )


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
    def push_style_var_vec2(*_args) -> None:
        pass

    @staticmethod
    def pop_style_var(*_args) -> None:
        pass

    @staticmethod
    def get_item_rect_min_x() -> float:
        return 0.0

    @staticmethod
    def get_item_rect_min_y() -> float:
        return 0.0

    @staticmethod
    def get_item_rect_max_x() -> float:
        return 22.0

    @staticmethod
    def get_item_rect_max_y() -> float:
        return 22.0

    @staticmethod
    def draw_image_rect(*_args) -> None:
        pass

    @staticmethod
    def draw_text_aligned(*_args) -> None:
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
    def checkbox(_label, value):
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


class _GraphParameterDetailContext:
    def __init__(self, *, name: str, default: float) -> None:
        self.name = name
        self.default = float(default)

    @staticmethod
    def label(*_args) -> None:
        pass

    @staticmethod
    def separator() -> None:
        pass

    @staticmethod
    def set_next_item_width(*_args) -> None:
        pass

    def text_input(self, label: str, value: str, _length: int) -> str:
        return self.name if "parameter_name" in label else value

    @staticmethod
    def combo(_label: str, index: int, *_args) -> int:
        return index

    def drag_float(self, label: str, value: float, *_args) -> float:
        return self.default if "default" in label else value

    @staticmethod
    def checkbox(_label: str, value: bool) -> bool:
        return value

    @staticmethod
    def input_int(_label: str, value: int) -> int:
        return value

    @staticmethod
    def input_uint(_label: str, value: int) -> int:
        return value

    @staticmethod
    def begin_disabled(_disabled: bool = True) -> None:
        pass

    @staticmethod
    def end_disabled() -> None:
        pass


def test_animfsm_toolbar_exposes_stable_semantic_ids():
    panel = AnimFSMEditorPanel()
    panel._fsm = AnimStateMachine(name="Locomotion")
    panel._fsm.mode = "3d"
    panel._file_path = "Assets/Locomotion.animfsm"
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
    assert by_id["animfsm.document.dirty"][4] is False


def test_animfsm_parameter_add_exposes_stable_semantic_id():
    _install_node_graph_context_commands()
    panel = AnimFSMEditorPanel()
    ctx = _ToolbarContext()
    ctx.push_style_color = lambda *_args: None
    ctx.pop_style_color = lambda *_args: None
    ctx.separator = lambda: None
    ctx.dummy = lambda *_args: None

    panel._render_variables_panel(ctx)

    assert "animfsm.parameters.add" in {item[3] for item in ctx.semantic_items}


def test_fsm_and_particle_parameters_share_the_base_detail_drawer():
    from Infernux.engine.ui.node_graph_editor_panel import NodeGraphEditorPanel
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel
    from Infernux.graph.types import TypeRef, ValueType

    fsm_panel = AnimFSMEditorPanel()
    fsm_parameter = AnimParameter(
        name="Speed",
        value_type=TypeRef(ValueType.F32),
        default=1.0,
    )
    fsm_panel._fsm.parameters.append(fsm_parameter)
    fsm_panel._graph_selection.select(
        (GraphElementRef(GraphElementKind.PARAMETER, fsm_parameter.stable_id),),
        reason="test",
        record_history=False,
    )

    particle_panel = ParticleGraphEditorPanel()
    particle = particle_panel.add_authoring_parameter(
        "Speed",
        "f32",
        1.0,
    )
    particle_panel._graph_selection.select(
        (GraphElementRef(GraphElementKind.PARAMETER, particle["stable_id"]),),
        reason="test",
        record_history=False,
    )

    assert fsm_panel._render_node_graph_parameter_detail.__func__ is (
        NodeGraphEditorPanel._render_node_graph_parameter_detail
    )
    assert particle_panel._render_node_graph_parameter_detail.__func__ is (
        NodeGraphEditorPanel._render_node_graph_parameter_detail
    )
    assert fsm_panel._render_node_graph_parameter_detail(
        _GraphParameterDetailContext(name="Velocity", default=2.5)
    )
    assert particle_panel._render_node_graph_parameter_detail(
        _GraphParameterDetailContext(name="Velocity", default=2.5)
    )

    updated_fsm = fsm_panel._parameter_by_id(fsm_parameter.stable_id)
    updated_particle = next(
        value
        for value in particle_panel.asset.parameters
        if value.stable_id == particle["stable_id"]
    )
    assert (updated_fsm.name, updated_fsm.default) == ("Velocity", 2.5)
    assert (updated_particle.name, updated_particle.default) == ("Velocity", 2.5)


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


def test_node_graph_canvas_unwinds_clip_and_child_when_inline_render_raises():
    events: list[str] = []
    ctx = SimpleNamespace(
        get_content_region_avail_width=lambda: 640.0,
        get_content_region_avail_height=lambda: 360.0,
        begin_child=lambda *_args: True,
        end_child=lambda: events.append("end_child"),
        set_scroll_x=lambda _value: None,
        set_scroll_y=lambda _value: None,
        is_window_hovered=lambda: True,
        get_window_pos_x=lambda: 12.0,
        get_window_pos_y=lambda: 24.0,
        semantic_capture_enabled=False,
        push_draw_list_clip_rect=lambda *_args: events.append("push_clip"),
        pop_draw_list_clip_rect=lambda: events.append("pop_clip"),
        draw_filled_rect=lambda *_args: None,
        get_mouse_pos_x=lambda: 20.0,
        get_mouse_pos_y=lambda: 30.0,
    )
    view = NodeGraphView()
    view.graph = SimpleNamespace(nodes=[], links=[])
    view._submit_canvas_background_region = lambda *_args: True
    view._draw_grid = lambda *_args: None
    view._compute_layouts = lambda: None
    view._hit_test_pin = lambda *_args: (None, None, None)
    view._draw_links = lambda *_args: None
    view._draw_nodes = lambda *_args: None

    def _raise_inline(_ctx):
        raise RuntimeError("inline render failed")

    view._draw_inline_fields = _raise_inline

    with pytest.raises(RuntimeError, match="inline render failed"):
        view.render(ctx)

    assert events == ["push_clip", "pop_clip", "end_child"]


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
        def set_window_font_scale(_value: float) -> None:
            pass

        @staticmethod
        def push_style_color(*_args) -> None:
            pass

        @staticmethod
        def pop_style_color(_count: int = 1) -> None:
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

        @staticmethod
        def draw_text_aligned(*_args) -> None:
            pass

        @staticmethod
        def draw_filled_rect(*_args) -> None:
            pass

        @staticmethod
        def get_mouse_pos_x() -> float:
            return -1.0

        @staticmethod
        def get_mouse_pos_y() -> float:
            return -1.0

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


def test_node_graph_inline_scalar_tolerates_stale_vector_without_mutating_document():
    class _InlineFloatContext:
        def __init__(self) -> None:
            self.received = None
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
        def set_window_font_scale(_value: float) -> None:
            pass

        @staticmethod
        def push_style_color(*_args) -> None:
            pass

        @staticmethod
        def pop_style_color(_count: int = 1) -> None:
            pass

        def drag_float(self, _label: str, value: float, *_args) -> float:
            self.received = value
            return value

        @staticmethod
        def is_item_hovered() -> bool:
            return False

        @staticmethod
        def is_item_active() -> bool:
            return False

        def record_semantic_item(self, *args, **kwargs) -> None:
            self.semantic_items.append((args, kwargs))

        @staticmethod
        def draw_text_aligned(*_args) -> None:
            pass

        @staticmethod
        def draw_filled_rect(*_args) -> None:
            pass

        @staticmethod
        def get_mouse_pos_x() -> float:
            return -1.0

        @staticmethod
        def get_mouse_pos_y() -> float:
            return -1.0

    original = [2.5, 4.0, 8.0]
    node = SimpleNamespace(uid="dynamic", data={"value": list(original)})
    layout = SimpleNamespace(node=node, sx=0.0, w=200.0)
    field = SimpleNamespace(
        id="value",
        default=0.0,
        label="Value",
        data_type="f32",
        enum_values=(),
    )
    view = NodeGraphView()
    view._origin_x = 0.0
    view._origin_y = 0.0
    view.zoom = 1.0
    view._semantic_capture_active = True
    ctx = _InlineFloatContext()

    view._draw_inline_field(ctx, layout, field, 40.0)

    assert ctx.received == 2.5
    assert node.data["value"] == original
    assert ctx.semantic_items == [
        (
            (
                "drag_float",
                "Value",
                True,
                "node_graph.inline.dynamic.value",
            ),
            {"numeric_value": 2.5},
        )
    ]


def test_node_graph_inline_u32_accepts_full_unsigned_range():
    class _InlineUIntContext:
        def __init__(self) -> None:
            self.received = None

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
        def set_window_font_scale(_value: float) -> None:
            pass

        @staticmethod
        def push_style_color(*_args) -> None:
            pass

        @staticmethod
        def pop_style_color(_count: int = 1) -> None:
            pass

        def input_uint(self, _label: str, value: int) -> int:
            self.received = value
            return value

        @staticmethod
        def is_item_hovered() -> bool:
            return False

        @staticmethod
        def is_item_active() -> bool:
            return False

        @staticmethod
        def record_semantic_item(*_args, **_kwargs) -> None:
            pass

        @staticmethod
        def draw_text_aligned(*_args) -> None:
            pass

        @staticmethod
        def draw_filled_rect(*_args) -> None:
            pass

        @staticmethod
        def get_mouse_pos_x() -> float:
            return -1.0

        @staticmethod
        def get_mouse_pos_y() -> float:
            return -1.0

    node = SimpleNamespace(uid="collision", data={"layer_mask": 0xFFFFFFFF})
    layout = SimpleNamespace(node=node, sx=0.0, w=200.0)
    field = SimpleNamespace(
        id="layer_mask",
        default=0xFFFFFFFF,
        label="Layer Mask",
        data_type="u32",
        enum_values=(),
    )
    view = NodeGraphView()
    view._origin_x = 0.0
    view._origin_y = 0.0
    view.zoom = 1.0
    view._semantic_capture_active = True
    ctx = _InlineUIntContext()

    view._draw_inline_field(ctx, layout, field, 40.0)

    assert ctx.received == 0xFFFFFFFF
    assert node.data["layer_mask"] == 0xFFFFFFFF


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


def test_animfsm_dirty_mode_switch_uses_global_document_replacement(monkeypatch):
    from Infernux.engine.interaction import DocumentRegistry
    from Infernux.engine.ui.dirty_panel_confirmation import (
        DirtyPanelConfirmationCoordinator,
    )

    pending = {}

    class _Coordinator:
        @staticmethod
        def request_document_replace(
            document_id,
            on_complete,
            on_cancel=None,
            *,
            owner_id="",
        ):
            pending.update(
                document_id=document_id,
                on_complete=on_complete,
                on_cancel=on_cancel,
                owner_id=owner_id,
            )
            return True

    monkeypatch.setattr(
        DirtyPanelConfirmationCoordinator,
        "instance",
        classmethod(lambda cls: _Coordinator()),
    )
    panel = AnimFSMEditorPanel()
    original = panel._fsm
    DocumentRegistry.instance().mark_changed(
        panel.document_id,
        view_id=panel.window_id,
    )

    assert panel.command_switch_mode("3d")

    assert panel._fsm is original
    assert pending["document_id"] == panel.document_id
    assert pending["owner_id"] == panel._window_id

    pending["on_complete"]()
    assert panel._fsm is not original
    assert panel._fsm.mode == "3d"


def test_animfsm_clean_mode_switch_starts_blank_and_clears_stale_selection():
    from Infernux.engine.interaction import DocumentRegistry

    panel = AnimFSMEditorPanel()
    panel._fsm.add_state("SavedState")
    panel._sync_graph_from_fsm()
    selected_uid = panel._name_to_uid["SavedState"]
    panel._view.selected_nodes = [selected_uid]
    panel._view.selected_link = "stale-link"
    DocumentRegistry.instance().mark_saved(panel.document_id)

    assert panel.command_switch_mode("3d")

    assert panel._fsm.mode == "3d"
    assert panel._fsm.states == []
    assert panel._view.selected_nodes == []
    assert panel._view.selected_link == ""


def test_animfsm_mode_switch_is_a_formal_panel_command():
    from Infernux.engine.ui.animfsm_editor_panel import _ANIMFSM_PANEL_INTERACTION

    command_ids = {spec.command_id for spec in _ANIMFSM_PANEL_INTERACTION.commands}
    assert "animfsm.switch_mode" in command_ids


def test_animfsm_selection_only_click_does_not_mark_resource_dirty():
    panel = AnimFSMEditorPanel()
    panel._fsm.add_state("State 0")
    panel._sync_graph_from_fsm()
    uid = panel._name_to_uid["State 0"]
    from Infernux.engine.interaction import DocumentRegistry

    DocumentRegistry.instance().mark_saved(panel.document_id)

    panel._on_canvas_selection_changed((uid,), "", True)
    panel._on_node_drag_start(uid)
    panel._on_node_drag_end(uid)

    assert panel._graph_selection.primary_id(GraphElementKind.NODE) == uid
    assert panel._document_is_dirty() is False


def test_animfsm_entry_move_uses_shared_node_graph_undo_and_persists():
    from Infernux.engine.undo import UndoManager

    panel = AnimFSMEditorPanel()
    manager = UndoManager.instance()
    entry = panel._graph.find_node(panel._entry_uid)
    assert entry is not None
    before = (entry.pos_x, entry.pos_y)

    panel._on_node_drag_start(entry.uid)
    entry.pos_x += 125.0
    entry.pos_y -= 35.0
    panel._on_node_drag_end(entry.uid)

    assert panel._fsm.entry_position == pytest.approx([25.0, 15.0])
    assert manager.can_undo
    manager.undo()
    restored = panel._graph.find_node(panel._entry_uid)
    assert restored is not None
    assert (restored.pos_x, restored.pos_y) == pytest.approx(before)
    assert panel._fsm.entry_position == pytest.approx(list(before))
    manager.redo()
    assert panel._fsm.entry_position == pytest.approx([25.0, 15.0])


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
        "render_asset_reference_field",
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
    parameter = panel._fsm.parameters[0]
    link.data["conditions"] = [
        {
            "stable_id": "condition-replay-trigger",
            "parameter_id": parameter.stable_id,
            "operator": ">",
            "threshold": 0.0,
        },
    ]
    panel._graph_selection.select_one(
        GraphElementKind.LINK, link.uid, record_history=False
    )
    ctx = _TransitionDetailContext(transition_exit_time=0.0)

    panel._render_node_graph_detail_panel(ctx)

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


def test_graph_detail_host_orders_contributors_and_consumes_first_match():
    from Infernux.engine.ui.graph_details import (
        GraphDetailContributor,
        GraphDetailHost,
    )

    rendered = []
    contributors = (
        GraphDetailContributor(
            "fallback", 0, lambda: True, lambda _ctx: rendered.append("fallback")
        ),
        GraphDetailContributor(
            "selected", 50, lambda: True, lambda _ctx: rendered.append("selected")
        ),
        GraphDetailContributor(
            "inactive", 100, lambda: False, lambda _ctx: rendered.append("inactive")
        ),
    )

    assert GraphDetailHost.render(SimpleNamespace(), contributors) == "selected"
    assert rendered == ["selected"]


def test_graph_detail_host_rejects_duplicate_contributor_ids():
    from Infernux.engine.ui.graph_details import (
        GraphDetailContributor,
        GraphDetailHost,
    )

    duplicate = GraphDetailContributor("same", 0, lambda: True, lambda _ctx: None)
    with pytest.raises(ValueError, match="unique"):
        GraphDetailHost.ordered((duplicate, duplicate))


def test_graph_domains_use_common_detail_host_instead_of_overriding_panel_dispatch():
    from Infernux.engine.ui.particle_graph_editor_panel import ParticleGraphEditorPanel

    assert "_render_node_graph_detail_panel" not in AnimFSMEditorPanel.__dict__
    assert "_render_node_graph_detail_panel" not in ParticleGraphEditorPanel.__dict__


def test_node_graph_context_menu_uses_the_host_namespace():
    _install_node_graph_context_commands()
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


def test_node_graph_canvas_background_does_not_submit_an_interactive_item():
    view = NodeGraphView()
    view.semantic_namespace = "particle_graph.canvas"
    view._semantic_capture_active = True
    view._canvas_window_hovered = True
    view._origin_x = 12.0
    view._origin_y = 34.0
    calls = []
    ctx = SimpleNamespace(
        set_cursor_pos_x=lambda value: calls.append(("cursor_x", value)),
        set_cursor_pos_y=lambda value: calls.append(("cursor_y", value)),
        dummy=lambda width, height: calls.append(("dummy", width, height)),
        record_semantic_rect=lambda *args: calls.append(("semantic", *args)),
    )

    hovered = view._submit_canvas_background_region(ctx, 640.0, 360.0)

    assert hovered is True
    assert all(call[0] not in {"dummy", "invisible_button"} for call in calls)
    assert (
        "semantic",
        "node_graph_canvas",
        "Node Graph",
        12.0,
        34.0,
        640.0,
        360.0,
        True,
        "particle_graph.canvas.canvas",
    ) in calls


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


def test_node_graph_reconnect_released_on_empty_requests_disconnect():
    from Infernux.core.node_graph import NodeGraph
    from Infernux.core.node_graph import NodeTypeDef, PinDef, PinKind

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
    original = graph.add_link(source.uid, "out", target.uid, "in")
    deleted = []
    replaced = []
    view = NodeGraphView()
    view.graph = graph
    view.on_link_deleted = deleted.append
    view.on_link_replaced = lambda *args: replaced.append(args)
    view._reconnect_link_uid = original.uid
    view._drag_src_node = source.uid
    view._drag_src_pin = "out"
    view._drag_src_kind = PinKind.OUTPUT
    view._hit_test_pin = lambda _x, _y: ("", None, PinKind.INPUT)

    view._try_complete_link(100.0, 100.0)

    assert graph.find_link(original.uid) is original
    assert deleted == [original.uid]
    assert replaced == []


def test_node_graph_add_command_exposes_unchecked_domain_semantic():
    _install_node_graph_context_commands()
    view = NodeGraphView()
    view.semantic_namespace = "vfx.graph"
    view.graph = type("Graph", (), {"registered_types": lambda _self: []})()
    ctx = _MenuContext(menu_open=True)

    view._draw_context_menu(ctx)

    by_id = {item[3]: item for item in ctx.semantic_items}
    assert by_id["vfx.graph.context.add_node"][4] == {"bool_value": False}


def test_animfsm_uses_shared_node_creation_palette_for_domain_variants():
    from Infernux.core.node_graph import PinKind

    panel = AnimFSMEditorPanel()

    assert panel._view.on_link_dropped_empty is None
    entries = panel._view._creation_entries(
        {"source_node": panel._entry_uid, "source_kind": PinKind.OUTPUT}
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


def test_node_graph_width_expands_for_long_input_labels():
    typedef = SimpleNamespace(
        label="Sprite Output: Particle Six-Way Smoke",
        min_width=210.0,
        show_header_color_swatch=True,
        input_pins=lambda: [SimpleNamespace(label="positiveAxesMap")],
        output_pins=lambda: [],
    )
    node = SimpleNamespace(data={}, type_id="output")
    view = NodeGraphView()

    assert view._natural_node_width(node, typedef) > typedef.min_width


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

    assert recorded[0][0:2] == ("node_graph_node", "State 0")
    assert recorded[0][4:6] == (12.0, 12.0)
    assert recorded[0][6:] == (True, "animfsm.graph.node.state-uid")
    node_center_x = recorded[0][2] + recorded[0][4] * 0.5
    node_center_y = recorded[0][3] + recorded[0][5] * 0.5
    assert node_center_x == 82.0
    assert node_center_y < 34.0 + 36.0
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
    countdown_target = by_semantic_id["animfsm.graph.node.countdown"]
    assert countdown_target[6] is True
    countdown_target_center_x = countdown_target[2] + countdown_target[4] * 0.5
    assert countdown_target_center_x < upper.sx
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
    assert recorded[0][2:6] == (8.0, 18.0, 24.0, 24.0)
    assert recorded[1][2:6] == (108.0, 18.0, 24.0, 24.0)


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
    from Infernux.engine.interaction import asset_reference_catalog

    model_path = "Assets/Models/Racer.fbx"
    monkeypatch.setattr(
        asset_reference_catalog,
        "items",
        lambda asset_type, _query: (("Racer.fbx", model_path),)
        if asset_type == "Mesh"
        else (),
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

    assert items == [(
        "Racer | Drive",
        {
            "asset_type": "AnimationClip3D",
            "builtin": "",
            "guid": "",
            "path_hint": f"{'a' * 32}::subanim:1",
        },
    )]


def test_animfsm_new_document_and_dirty_draft_round_trip_through_registry_session():
    from Infernux.engine.interaction import DocumentRegistry

    panel = AnimFSMEditorPanel()
    assert panel._document_is_dirty() is False
    panel._fsm.name = "Recovered FSM"
    panel._fsm.parameters.append(AnimParameter(name="speed"))
    DocumentRegistry.instance().mark_changed(
        panel.document_id,
        view_id=panel.window_id,
    )
    assert panel._document_is_dirty() is True
    view_state = panel.save_state()
    session_state = DocumentRegistry.instance().capture_session_state()

    restored_registry = DocumentRegistry()
    assert restored_registry.queue_session_restore(session_state) == 1
    restored = AnimFSMEditorPanel()
    assert restored.restore_persisted_session_document()
    restored.load_state(view_state)

    assert restored._document_is_dirty() is True
    assert restored._fsm.name == "Recovered FSM"
    assert [parameter.name for parameter in restored._fsm.parameters] == ["speed"]


def test_animfsm_entering_play_does_not_implicitly_save_dirty_draft(monkeypatch):
    from Infernux.engine.interaction import DocumentRegistry

    panel = AnimFSMEditorPanel()
    save_calls = []
    monkeypatch.setattr(panel, "_do_save", lambda: save_calls.append(True))
    DocumentRegistry.instance().mark_changed(
        panel.document_id,
        view_id=panel.window_id,
    )

    panel._on_play_mode_changed(SimpleNamespace(new_state="playing"))

    assert save_calls == []
    assert panel._document_is_dirty() is True
