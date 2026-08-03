"""Shared editor shell for every node-authored asset.

The shell owns the canvas, floating workspace panels, resize behavior, and
standard view callbacks. Domain editors provide data, detail renderers, and
compile/save adapters without rebuilding editor interaction infrastructure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from Infernux.core.node_graph import (
    NodeGraph,
    NodeGraphAuthoringState,
    NodeGraphElementKind,
)
from Infernux.engine.interaction import (
    GraphElementKind,
    GraphElementRef,
    GraphMutation,
    GraphMutationKind,
)
from Infernux.debug import Debug
from Infernux.lib import InxGUIContext

from .editor_panel import EditorPanel
from .floating_workspace_panel import (
    FloatingOverlayState,
    begin_workspace_entry,
    finish_workspace_entry,
    paint_workspace_entry,
    render_floating_overlay,
    render_workspace_add_header,
    update_overlay_resize_drag,
)
from .imgui_keys import KEY_DELETE, KEY_ESCAPE, KEY_F2
from .node_graph_view import NodeGraphView


@dataclass(frozen=True, slots=True)
class GraphWorkspaceDrag:
    """Pure-data drag payload emitted by a Blackboard row."""

    payload_type: str
    payload: str
    preview_label: str


@dataclass(frozen=True, slots=True)
class GraphWorkspaceAddAction:
    """One stable add command advertised by a workspace collection."""

    action_id: str
    label: str


@dataclass(frozen=True, slots=True)
class GraphWorkspaceEntry:
    """Domain-neutral row description for a graph workspace collection."""

    element: GraphElementRef
    primary: str
    secondary: str = ""
    dot_color: tuple[float, float, float, float] = (0.7, 0.7, 0.7, 1.0)
    semantic_kind: str = "graph_element"
    semantic_id: str = ""
    semantic_string_value: str = ""
    semantic_numeric_value: Optional[float] = None
    selected: Optional[bool] = None
    can_rename: bool = False
    can_delete: bool = False
    drag: Optional[GraphWorkspaceDrag] = None


@dataclass(frozen=True, slots=True)
class GraphWorkspaceSection:
    """One Blackboard page rendered by :class:`NodeGraphEditorPanel`."""

    title: str
    section_id: str
    entries: tuple[GraphWorkspaceEntry, ...]
    add_actions: tuple[GraphWorkspaceAddAction, ...] = ()
    add_semantic_id: str = ""
    rename_label: str = "Rename"
    delete_label: str = "Delete"


class NodeGraphEditorPanel(EditorPanel):
    """Particle-style workspace shared by FSM, Particle, and future graphs."""

    def __init__(
        self,
        *,
        title: str,
        window_id: str,
        semantic_namespace: str,
    ) -> None:
        super().__init__(title=title, window_id=window_id)
        self._view = NodeGraphView()
        self._view.semantic_namespace = semantic_namespace
        self._left_overlay = FloatingOverlayState()
        self._right_overlay = FloatingOverlayState()
        self._workspace_rename_element: Optional[GraphElementRef] = None
        self._workspace_rename_buffer = ""
        self._workspace_rename_focus = False
        self._install_node_graph_view_callbacks()

    @staticmethod
    def _node_graph_workspace_shortcut_pressed(
        ctx: InxGUIContext, key: int
    ) -> bool:
        is_focused = getattr(ctx, "is_window_focused", None)
        is_active = getattr(ctx, "is_any_item_active", None)
        is_pressed = getattr(ctx, "is_key_pressed", None)
        return bool(
            callable(is_focused)
            and callable(is_active)
            and callable(is_pressed)
            # This method runs while the Blackboard child is current. Exact
            # focus prevents its F2/Delete commands from leaking into the
            # canvas or the Detail overlay of the same root panel.
            and is_focused(0)
            and not is_active()
            and is_pressed(key)
        )

    def _cancel_node_graph_workspace_rename(self) -> None:
        self._workspace_rename_element = None
        self._workspace_rename_buffer = ""
        self._workspace_rename_focus = False

    def _begin_node_graph_workspace_rename(
        self, entry: GraphWorkspaceEntry
    ) -> None:
        if not entry.can_rename:
            return
        self._node_graph_workspace_activate(entry.element)
        self._workspace_rename_element = entry.element
        self._workspace_rename_buffer = entry.primary
        self._workspace_rename_focus = True

    @staticmethod
    def _run_node_graph_workspace_action(label: str, callback, *args) -> bool:
        if callback is None:
            return False
        try:
            return callback(*args) is not False
        except (KeyError, TypeError, ValueError) as exc:
            Debug.log_warning(f"Graph workspace {label} rejected: {exc}")
            return False

    def _node_graph_workspace_activate(self, element: GraphElementRef) -> bool:
        selection = getattr(self, "_graph_selection", None)
        if selection is None:
            raise RuntimeError("Graph workspace has no selection controller")
        return selection.select(
            (element,),
            reason="graph_workspace_selection",
            record_history=True,
        )

    def _node_graph_workspace_add(
        self, section_id: str, action_id: str
    ) -> bool:
        raise NotImplementedError(
            f"Graph workspace add is not implemented: {section_id}/{action_id}"
        )

    def _node_graph_workspace_rename(
        self, element: GraphElementRef, name: str
    ) -> bool:
        raise NotImplementedError(
            f"Graph workspace rename is not implemented: {element}"
        )

    def _node_graph_workspace_delete(self, element: GraphElementRef) -> bool:
        raise NotImplementedError(
            f"Graph workspace delete is not implemented: {element}"
        )

    def _render_graph_workspace_section(
        self,
        ctx: InxGUIContext,
        section: GraphWorkspaceSection,
    ) -> None:
        """Render shared Blackboard rows and defer mutating callbacks safely."""
        pending_add: Optional[str] = None

        def request_add(action_id: str) -> None:
            nonlocal pending_add
            pending_add = action_id

        add_actions = tuple(section.add_actions)
        popup_id = (
            f"##graph_workspace_add_{section.section_id}"
            if len(add_actions) > 1
            else ""
        )

        def build_add_popup(popup_ctx: InxGUIContext) -> None:
            for action in add_actions:
                if popup_ctx.menu_item(action.label):
                    request_add(action.action_id)
                    popup_ctx.close_current_popup()

        render_workspace_add_header(
            ctx,
            section.title,
            section.section_id,
            popup_id=popup_id,
            on_add=(
                (lambda: request_add(add_actions[0].action_id))
                if len(add_actions) == 1
                else None
            ),
            build_popup=build_add_popup if len(add_actions) > 1 else None,
            disabled=not add_actions,
            semantic_id=section.add_semantic_id,
        )

        primary = getattr(getattr(self, "_graph_selection", None), "primary", None)
        pending_select: Optional[GraphWorkspaceEntry] = None
        pending_rename: Optional[GraphWorkspaceEntry] = None
        pending_delete: Optional[GraphWorkspaceEntry] = None
        pending_commit: Optional[tuple[GraphWorkspaceEntry, str]] = None

        entries = tuple(section.entries)
        for index, entry in enumerate(entries):
            selected = (
                entry.selected
                if entry.selected is not None
                else entry.element == primary
            )
            renaming = self._workspace_rename_element == entry.element
            clicked, rect = begin_workspace_entry(
                ctx,
                f"graph_workspace_{section.section_id}_{entry.element.kind.value}_{entry.element.stable_id}",
                bool(selected),
            )
            paint_workspace_entry(
                ctx,
                rect,
                primary="" if renaming else entry.primary,
                secondary="" if renaming else entry.secondary,
                dot_color=entry.dot_color,
                selected=bool(selected),
            )
            if renaming:
                cursor_x = ctx.get_cursor_pos_x()
                cursor_y = ctx.get_cursor_pos_y()
                ctx.set_cursor_pos_x(rect[0] - ctx.get_window_pos_x() + 18.0)
                ctx.set_cursor_pos_y(rect[1] - ctx.get_window_pos_y() + 2.0)
                ctx.set_next_item_width(max(24.0, rect[2] - rect[0] - 26.0))
                if self._workspace_rename_focus:
                    ctx.set_keyboard_focus_here()
                    self._workspace_rename_focus = False
                self._workspace_rename_buffer = ctx.input_text_with_hint(
                    f"##graph_workspace_rename_{entry.element.kind.value}_{entry.element.stable_id}",
                    "",
                    self._workspace_rename_buffer,
                    256,
                    1 << 6,
                )
                is_key_pressed = getattr(ctx, "is_key_pressed", None)
                if callable(is_key_pressed) and is_key_pressed(KEY_ESCAPE):
                    self._cancel_node_graph_workspace_rename()
                elif ctx.is_item_deactivated_after_edit():
                    pending_commit = (entry, self._workspace_rename_buffer.strip())
                ctx.set_cursor_pos_x(cursor_x)
                ctx.set_cursor_pos_y(cursor_y)

            recorder = getattr(ctx, "record_semantic_item", None)
            if entry.semantic_id and callable(recorder):
                kwargs = {"bool_value": bool(selected)}
                if entry.semantic_string_value:
                    kwargs["string_value"] = entry.semantic_string_value
                if entry.semantic_numeric_value is not None:
                    kwargs["numeric_value"] = float(entry.semantic_numeric_value)
                recorder(
                    entry.semantic_kind,
                    entry.primary,
                    True,
                    entry.semantic_id,
                    **kwargs,
                )
            if clicked:
                pending_select = entry
            is_item_hovered = getattr(ctx, "is_item_hovered", None)
            is_mouse_double_clicked = getattr(
                ctx, "is_mouse_double_clicked", None
            )
            if (
                not renaming
                and entry.can_rename
                and callable(is_item_hovered)
                and callable(is_mouse_double_clicked)
                and is_item_hovered()
                and is_mouse_double_clicked(0)
            ):
                pending_rename = entry
            begin_drag_source = getattr(ctx, "begin_drag_drop_source", None)
            if (
                entry.drag is not None
                and callable(begin_drag_source)
                and begin_drag_source()
            ):
                ctx.set_drag_drop_payload_str(
                    entry.drag.payload_type, entry.drag.payload
                )
                ctx.label(entry.drag.preview_label)
                ctx.end_drag_drop_source()
            if ctx.begin_popup_context_item(
                f"##graph_workspace_context_{section.section_id}_{index}_{entry.element.stable_id}"
            ):
                if entry.can_rename and ctx.menu_item(section.rename_label):
                    pending_rename = entry
                if entry.can_delete and ctx.menu_item(section.delete_label):
                    pending_delete = entry
                ctx.end_popup()
            finish_workspace_entry(ctx)

        if pending_commit is not None:
            entry, name = pending_commit
            if name and self._run_node_graph_workspace_action(
                "rename",
                self._node_graph_workspace_rename,
                entry.element,
                name,
            ):
                self._cancel_node_graph_workspace_rename()
        elif pending_delete is not None:
            if self._run_node_graph_workspace_action(
                "delete",
                self._node_graph_workspace_delete,
                pending_delete.element,
            ):
                if self._workspace_rename_element == pending_delete.element:
                    self._cancel_node_graph_workspace_rename()
        elif pending_rename is not None:
            self._begin_node_graph_workspace_rename(pending_rename)
        elif pending_select is not None:
            self._run_node_graph_workspace_action(
                "selection",
                self._node_graph_workspace_activate,
                pending_select.element,
            )
        elif pending_add is not None:
            self._run_node_graph_workspace_action(
                "add",
                self._node_graph_workspace_add,
                section.section_id,
                pending_add,
            )

        selected_entry = next(
            (
                entry
                for entry in entries
                if (
                    entry.selected
                    if entry.selected is not None
                    else entry.element == primary
                )
            ),
            None,
        )
        if (
            self._workspace_rename_element is None
            and selected_entry is not None
            and self._node_graph_workspace_shortcut_pressed(ctx, KEY_DELETE)
            and selected_entry.can_delete
        ):
            self._run_node_graph_workspace_action(
                "delete",
                self._node_graph_workspace_delete,
                selected_entry.element,
            )
        elif (
            self._workspace_rename_element is None
            and selected_entry is not None
            and self._node_graph_workspace_shortcut_pressed(ctx, KEY_F2)
        ):
            self._begin_node_graph_workspace_rename(selected_entry)

    def _node_graph_authoring_model(self) -> Optional[NodeGraph]:
        graph = self._view.graph
        return graph if isinstance(graph, NodeGraph) else None

    def _node_graph_authoring_identity(
        self, _kind: NodeGraphElementKind, stable_id: str
    ) -> str:
        return stable_id

    def _node_graph_mutations(
        self,
        before: NodeGraphAuthoringState,
        after: NodeGraphAuthoringState,
    ) -> tuple[GraphMutation, ...]:
        return tuple(
            GraphMutation(
                GraphMutationKind(mutation.kind.value),
                GraphElementRef(
                    GraphElementKind(mutation.element_kind.value),
                    mutation.stable_id,
                ),
                before=mutation.before,
                after=mutation.after,
                before_index=mutation.before_index,
                after_index=mutation.after_index,
            )
            for mutation in NodeGraph.diff_authoring_states(before, after)
        )

    def _commit_node_graph_change(
        self,
        description: str,
        before: NodeGraphAuthoringState,
        *,
        selection_after: Optional[tuple[GraphElementRef, ...]] = None,
        merge_key: str = "",
    ) -> bool:
        """Commit one staged graph edit through the shared precise Undo path."""
        graph = self._node_graph_authoring_model()
        if graph is None:
            return False
        after = graph.capture_authoring_state()
        local_mutations = NodeGraph.diff_authoring_states(before, after)
        if not local_mutations:
            return False
        global_after = graph.capture_authoring_state(
            identity=self._node_graph_authoring_identity
        )
        graph.apply_authoring_mutations(
            NodeGraph.invert_authoring_mutations(local_mutations)
        )
        global_before = graph.capture_authoring_state(
            identity=self._node_graph_authoring_identity
        )
        return self._execute_graph_mutations(
            description,
            self._node_graph_mutations(global_before, global_after),
            merge_key=merge_key,
            selection_after=selection_after,
        )

    def _install_node_graph_view_callbacks(self) -> None:
        bindings = {
            "on_node_add_request": ("_on_node_add_request", "_on_node_add"),
            "on_node_creation_entries": ("_node_creation_entries",),
            "on_node_creation_requested": ("_on_node_creation_requested",),
            "on_node_creation_selected": ("_on_node_creation_selected",),
            "on_nodes_deleted": ("_on_nodes_deleted",),
            "on_link_created": ("_on_link_created",),
            "on_link_deleted": ("_on_link_deleted",),
            "on_link_replaced": ("_on_link_replaced",),
            "on_node_drag_start": ("_on_node_drag_start",),
            "on_node_drag_end": ("_on_node_drag_end",),
            "on_selection_changed": ("_on_canvas_selection_changed",),
            "on_node_data_changed": ("_on_node_data_changed",),
            "on_node_header_color_changed": (
                "_on_node_header_color_changed",
            ),
            "on_canvas_drop": ("_on_canvas_drop",),
            "on_copy": ("_on_graph_copy",),
            "on_paste": ("_on_graph_paste",),
        }
        for callback_name, candidates in bindings.items():
            callback = next(
                (
                    candidate
                    for name in candidates
                    if callable(candidate := getattr(self, name, None))
                ),
                None,
            )
            if callback is not None:
                setattr(self._view, callback_name, callback)

    def _before_node_graph_render(self, ctx: InxGUIContext) -> None:
        del ctx

    def _render_node_graph_toolbar(self, ctx: InxGUIContext) -> None:
        del ctx

    def _render_node_graph_left_panel(self, ctx: InxGUIContext) -> None:
        del ctx

    def _render_node_graph_detail_panel(self, ctx: InxGUIContext) -> None:
        del ctx

    def _after_node_graph_render(self, ctx: InxGUIContext) -> None:
        del ctx

    def _node_graph_canvas_id(self) -> str:
        return f"##{self.window_id}_graph_workspace"

    def _node_graph_left_child_id(self) -> str:
        return f"##{self.window_id}_graph_left"

    def _node_graph_detail_child_id(self) -> str:
        return f"##{self.window_id}_graph_detail"

    def _node_graph_defer_canvas_drop_target(self) -> bool:
        return False

    def _node_graph_left_width(self, available_width: float) -> float:
        return min(240.0, max(180.0, available_width * 0.18))

    def _node_graph_detail_width(self, available_width: float) -> float:
        return min(380.0, max(320.0, available_width * 0.30))

    def _render_node_graph_workspace(self, ctx: InxGUIContext) -> None:
        available_w = ctx.get_content_region_avail_width()
        available_h = ctx.get_content_region_avail_height()
        left_w = self._node_graph_left_width(available_w)
        detail_w = self._node_graph_detail_width(available_w)
        margin = 8.0
        max_overlay_h = max(1.0, available_h - margin * 2.0)
        default_h = min(max(160.0, available_h * 0.52), max_overlay_h)
        if self._left_overlay.height <= 0.0:
            self._left_overlay.height = default_h
        if self._right_overlay.height <= 0.0:
            self._right_overlay.height = default_h
        self._left_overlay.height = update_overlay_resize_drag(
            ctx,
            self._left_overlay,
            avail_h=available_h,
            margin=margin,
        )
        self._right_overlay.height = update_overlay_resize_drag(
            ctx,
            self._right_overlay,
            avail_h=available_h,
            margin=margin,
        )

        graph_visible = ctx.begin_child(
            self._node_graph_canvas_id(), available_w, available_h, False
        )
        try:
            if not graph_visible:
                return
            defer_drop = self._node_graph_defer_canvas_drop_target()
            self._view.render(ctx, defer_canvas_drop_target=defer_drop)
            render_floating_overlay(
                ctx,
                self._left_overlay,
                child_id=self._node_graph_left_child_id(),
                x=margin,
                y=margin,
                width=left_w,
                max_height=max_overlay_h,
                render_fn=lambda: self._render_node_graph_left_panel(ctx),
            )
            render_floating_overlay(
                ctx,
                self._right_overlay,
                child_id=self._node_graph_detail_child_id(),
                x=max(margin, available_w - detail_w - margin),
                y=margin,
                width=detail_w,
                max_height=max_overlay_h,
                render_fn=lambda: self._render_node_graph_detail_panel(ctx),
            )
            if defer_drop:
                self._view.render_canvas_drop_target(ctx)
        finally:
            ctx.end_child()

    def on_render_content(self, ctx: InxGUIContext) -> None:
        self._before_node_graph_render(ctx)
        self._render_node_graph_toolbar(ctx)
        ctx.separator()
        self._render_node_graph_workspace(ctx)
        self._after_node_graph_render(ctx)


__all__ = [
    "GraphWorkspaceAddAction",
    "GraphWorkspaceDrag",
    "GraphWorkspaceEntry",
    "GraphWorkspaceSection",
    "NodeGraphEditorPanel",
]
