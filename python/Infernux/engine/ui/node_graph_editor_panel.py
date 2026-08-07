"""Shared editor shell for every node-authored asset.

The shell owns the canvas, floating workspace panels, resize behavior, and
standard view callbacks. Domain editors provide data, detail renderers, and
compile/save adapters without rebuilding editor interaction infrastructure.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Callable, Mapping, Optional
import uuid

from Infernux.core.node_graph import (
    NodeGraph,
    NodeGraphAuthoringState,
    NodeGraphClipboardState,
    NodeGraphElementKind,
    PinKind,
)
from Infernux.engine.interaction import (
    BoundPanelCommand,
    ClipboardDomain,
    ClipboardItem,
    ClipboardPayload,
    ClipboardService,
    CommandContext,
    CollectionInteractionModel,
    CommandSource,
    ContextMenuBuilder,
    ContextMenuCommand,
    GraphActionDiff,
    GraphElementKind,
    GraphElementRef,
    GraphMutation,
    GraphMutationKind,
    GraphSelectionController,
    KeyChord,
    PanelCommandAdapter,
    PanelCommandSpec,
    PanelInteractionDescriptor,
    PanelShortcutSpec,
    ResolvedContextMenuCommand,
    SelectionDomain,
    SelectionService,
    TransientInteractionService,
    ViewCommandService,
)
from Infernux.debug import Debug
from Infernux.graph.parameters import (
    GraphParameterAuthoringPolicy,
    GraphParameterCollection,
    GraphParameterDefinition,
)
from Infernux.graph.parameter_transactions import GraphParameterTransaction
from Infernux.graph.types import CoordinateSpace, TypeRef, ValueType
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
from .node_graph_view import NodeGraphView
from .graph_details import GraphDetailContributor, GraphDetailHost
from .inspector_utils import preserve_ui_float_precision


_GRAPH_WORKSPACE_CONTEXT_MENU_BUILDER = ContextMenuBuilder()

def _bind_node_graph_panel(panel: object) -> PanelCommandAdapter:
    required = (
        "can_edit_copy",
        "can_edit_cut",
        "can_edit_paste",
        "can_edit_delete",
        "can_edit_rename",
        "can_edit_duplicate",
        "command_edit_copy",
        "command_edit_cut",
        "command_edit_paste",
        "command_edit_delete",
        "command_edit_rename",
        "command_edit_duplicate",
        "can_graph_center_view",
        "can_graph_reset_zoom",
        "command_graph_center_view",
        "command_graph_reset_zoom",
        "can_graph_add_node",
        "command_graph_add_node",
        "can_graph_create_node",
        "command_graph_create_node",
        "can_graph_workspace_add",
        "command_graph_workspace_add",
    )
    missing = tuple(name for name in required if not callable(getattr(panel, name, None)))
    if missing:
        raise TypeError(f"node graph panel interaction contract is missing: {missing}")
    return PanelCommandAdapter(
        {
            "edit.copy": BoundPanelCommand(
                lambda _context: panel.command_edit_copy(),
                lambda _context: panel.can_edit_copy(),
            ),
            "edit.cut": BoundPanelCommand(
                lambda _context: panel.command_edit_cut(),
                lambda _context: panel.can_edit_cut(),
            ),
            "edit.paste": BoundPanelCommand(
                lambda _context: panel.command_edit_paste(),
                lambda _context: panel.can_edit_paste(),
            ),
            "edit.delete": BoundPanelCommand(
                lambda _context: panel.command_edit_delete(),
                lambda _context: panel.can_edit_delete(),
            ),
            "edit.rename": BoundPanelCommand(
                lambda _context: panel.command_edit_rename(),
                lambda _context: panel.can_edit_rename(),
            ),
            "edit.duplicate": BoundPanelCommand(
                lambda _context: panel.command_edit_duplicate(),
                lambda _context: panel.can_edit_duplicate(),
            ),
            "edit.deselect": BoundPanelCommand(
                lambda _context: SelectionService.instance().clear(
                    reason="node_graph_deselect",
                    record_history=True,
                ),
                lambda context: bool(context.selection.targets),
            ),
            "graph.center_view": BoundPanelCommand(
                lambda _context: panel.command_graph_center_view(),
                lambda _context: panel.can_graph_center_view(),
            ),
            "graph.reset_zoom": BoundPanelCommand(
                lambda _context: panel.command_graph_reset_zoom(),
                lambda _context: panel.can_graph_reset_zoom(),
            ),
            "graph.add_node": BoundPanelCommand(
                panel.command_graph_add_node,
                panel.can_graph_add_node,
            ),
            "graph.create_node": BoundPanelCommand(
                panel.command_graph_create_node,
                panel.can_graph_create_node,
            ),
            "graph.workspace.add": BoundPanelCommand(
                panel.command_graph_workspace_add,
                panel.can_graph_workspace_add,
            ),
        }
    )


NODE_GRAPH_PANEL_INTERACTION = PanelInteractionDescriptor(
    document_backed=True,
    owned_selection_domains=frozenset({SelectionDomain.GRAPH_ELEMENT}),
    commands=(
        PanelCommandSpec("edit.copy"),
        PanelCommandSpec("edit.cut"),
        PanelCommandSpec("edit.paste"),
        PanelCommandSpec("edit.delete"),
        PanelCommandSpec("edit.rename"),
        PanelCommandSpec("edit.duplicate"),
        PanelCommandSpec("edit.deselect"),
        PanelCommandSpec("graph.center_view"),
        PanelCommandSpec("graph.reset_zoom"),
        PanelCommandSpec("graph.add_node"),
        PanelCommandSpec("graph.create_node"),
        PanelCommandSpec("graph.workspace.add"),
    ),
    shortcuts=tuple(
        PanelShortcutSpec(command_id, KeyChord.parse(chord))
        for command_id, chord in (
            ("edit.copy", "Ctrl+C"),
            ("edit.cut", "Ctrl+X"),
            ("edit.paste", "Ctrl+V"),
            ("edit.delete", "Delete"),
            ("edit.rename", "F2"),
            ("edit.duplicate", "Ctrl+D"),
            ("edit.deselect", "Escape"),
        )
    ),
    adapter_factory=_bind_node_graph_panel,
)


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
    enabled: bool = True
    disabled_reason: str = ""


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


@dataclass(frozen=True, slots=True)
class GraphParameterDetailConfig:
    """Presentation labels and capabilities for the shared parameter drawer."""

    title: str = "Parameter"
    name_label: str = "Name"
    type_label: str = "Type"
    space_label: str = "Space"
    default_label: str = "Default"
    exposed_label: str = "Exposed"
    writable_label: str = "Writable"
    tooltip_label: str = "Tooltip"
    semantic_prefix: str = "node_graph.parameter"
    show_exposed: bool = True
    show_writable: bool = True
    show_tooltip: bool = True
    value_type_label: Callable[[ValueType], str] = lambda kind: kind.value
    coordinate_space_label: Callable[[CoordinateSpace], str] = (
        lambda space: space.value
    )


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
        self._workspace_rename_cancel_token = ""
        self._node_graph_drag_cancel_token = ""
        self._workspace_entries: dict[GraphElementRef, GraphWorkspaceEntry] = {}
        self._workspace_entry_sections: dict[GraphElementRef, str] = {}
        self._workspace_section_elements: dict[str, set[GraphElementRef]] = {}
        self._workspace_add_actions: dict[
            str, dict[str, GraphWorkspaceAddAction]
        ] = {}
        self._workspace_collections: dict[str, CollectionInteractionModel] = {}
        self._node_graph_drag_snapshot: Optional[NodeGraphAuthoringState] = None
        self._install_node_graph_view_callbacks()

    def on_enable(self) -> None:
        selection = getattr(self, "_graph_selection", None)
        if selection is None:
            raise RuntimeError("Graph editor has no selection controller")
        selection.bind()

    def on_disable(self) -> None:
        self._cancel_node_graph_workspace_rename()
        selection = getattr(self, "_graph_selection", None)
        if selection is not None:
            selection.unbind()

    def _install_graph_selection_controller(
        self,
        *,
        contains,
        element_from_view=None,
        element_to_view=None,
        on_changed=None,
    ) -> GraphSelectionController:
        selection = GraphSelectionController(
            owner_id=self.window_id,
            document_id=lambda: self.document_id,
            contains=contains,
            view=self._view,
            element_from_view=element_from_view,
            element_to_view=element_to_view,
            on_changed=on_changed,
        )
        self._graph_selection = selection
        return selection

    def _bind_node_graph_model(
        self,
        graph: Optional[NodeGraph],
        *,
        preserve_selection: bool = True,
    ) -> None:
        self._view.bind_graph(graph, preserve_selection=preserve_selection)
        selection = getattr(self, "_graph_selection", None)
        if selection is not None:
            selection.refresh()

    def _capture_node_graph_view_state(self) -> tuple[float, float, float]:
        return (
            float(self._view.pan_x),
            float(self._view.pan_y),
            float(self._view.zoom),
        )

    def _apply_node_graph_view_state(
        self,
        state: tuple[float, float, float],
    ) -> None:
        self._view.pan_x = float(state[0])
        self._view.pan_y = float(state[1])
        self._view.zoom = float(state[2])

    def can_graph_center_view(self) -> bool:
        graph = self._view.graph
        return bool(graph is not None and graph.nodes)

    def can_graph_reset_zoom(self) -> bool:
        return bool(self._view.graph is not None and abs(self._view.zoom - 1.0) > 1e-9)

    def command_graph_center_view(self) -> bool:
        if not self.can_graph_center_view():
            return False
        before = self._capture_node_graph_view_state()
        self._view.center_on_nodes()
        after = self._capture_node_graph_view_state()
        self._apply_node_graph_view_state(before)
        return ViewCommandService.require().set_value(
            before,
            after,
            self._apply_node_graph_view_state,
            description="Center Node Graph View",
        )

    def command_graph_reset_zoom(self) -> bool:
        if not self.can_graph_reset_zoom():
            return False
        before = self._capture_node_graph_view_state()
        after = (before[0], before[1], 1.0)
        return ViewCommandService.require().set_value(
            before,
            after,
            self._apply_node_graph_view_state,
            description="Reset Node Graph Zoom",
        )

    @staticmethod
    def _node_creation_request_from_payload(
        payload: Mapping[str, object],
    ) -> Optional[dict]:
        try:
            gx = float(payload["gx"])
            gy = float(payload["gy"])
            source_kind = PinKind(
                payload.get("source_kind", PinKind.OUTPUT.value)
            )
        except (KeyError, TypeError, ValueError):
            return None
        return {
            "gx": gx,
            "gy": gy,
            "source_node": str(payload.get("source_node", "") or ""),
            "source_pin": str(payload.get("source_pin", "") or ""),
            "source_kind": source_kind,
        }

    def _can_apply_node_creation(self, request: Mapping[str, object]) -> bool:
        return bool(
            self._view.graph is not None
            and (
                self._view.on_node_creation_selected is not None
                or self._view.on_node_add_request is not None
            )
            and (
                not request.get("source_node")
                or self._view.on_link_created is not None
            )
        )

    def can_graph_add_node(self, context: CommandContext) -> bool:
        request = self._node_creation_request_from_payload(context.payload)
        return bool(request is not None and self._can_apply_node_creation(request))

    def command_graph_add_node(self, context: CommandContext) -> bool:
        request = self._node_creation_request_from_payload(context.payload)
        if request is None or not self._can_apply_node_creation(request):
            return False
        self._view._request_node_creation(**request)
        return True

    def _node_creation_entry_from_payload(
        self,
        payload: Mapping[str, object],
        request: dict,
    ):
        entry_key = str(payload.get("entry_key", "") or "").strip()
        type_id = str(payload.get("type_id", "") or "").strip()
        if not entry_key:
            return None
        return next(
            (
                entry
                for entry in self._view._creation_entries(request)
                if entry.key == entry_key
                and (not type_id or entry.type_id == type_id)
            ),
            None,
        )

    def can_graph_create_node(self, context: CommandContext) -> bool:
        request = self._node_creation_request_from_payload(context.payload)
        if request is None or not self._can_apply_node_creation(request):
            return False
        entry = self._node_creation_entry_from_payload(context.payload, request)
        return bool(entry is not None and entry.enabled)

    def command_graph_create_node(self, context: CommandContext) -> bool:
        request = self._node_creation_request_from_payload(context.payload)
        if request is None or not self._can_apply_node_creation(request):
            return False
        entry = self._node_creation_entry_from_payload(context.payload, request)
        if entry is None or not entry.enabled:
            return False
        self._view._create_from_palette(entry, request)
        return True

    def _execute_node_creation_command(self, payload: Mapping[str, object]) -> bool:
        registry = self.services.command_registry
        if registry is None:
            return False
        self.publish_interaction_ownership(reason="graph_create_node")
        return registry.execute(
            "graph.create_node",
            source=CommandSource.PALETTE,
            payload=payload,
        ).accepted

    def can_graph_workspace_add(self, context: CommandContext) -> bool:
        section_id = str(context.payload.get("section_id", "") or "").strip()
        action_id = str(context.payload.get("action_id", "") or "").strip()
        action = self._workspace_add_actions.get(section_id, {}).get(action_id)
        return bool(
            action is not None
            and action.enabled
            and type(self)._node_graph_workspace_add
            is not NodeGraphEditorPanel._node_graph_workspace_add
        )

    def command_graph_workspace_add(self, context: CommandContext) -> bool:
        if not self.can_graph_workspace_add(context):
            return False
        return self._run_node_graph_workspace_action(
            "add",
            self._node_graph_workspace_add,
            str(context.payload["section_id"]),
            str(context.payload["action_id"]),
        )

    def _cancel_node_graph_workspace_rename(self) -> None:
        if self._workspace_rename_cancel_token:
            TransientInteractionService.instance().end(
                self._workspace_rename_cancel_token
            )
            self._workspace_rename_cancel_token = ""
        for collection in self._workspace_collections.values():
            collection.cancel_rename()

    @staticmethod
    def _workspace_collection_item_id(element: GraphElementRef) -> str:
        return f"{element.kind.value}:{element.stable_id}"

    def _workspace_collection(self, section_id: str) -> CollectionInteractionModel:
        key = str(section_id or "").strip()
        if not key:
            raise ValueError("graph workspace section ID must be non-empty")
        collection = self._workspace_collections.get(key)
        if collection is None:
            collection = CollectionInteractionModel()
            self._workspace_collections[key] = collection
        return collection

    def _workspace_collection_for_entry(
        self,
        entry: GraphWorkspaceEntry,
    ) -> CollectionInteractionModel:
        section_id = self._workspace_entry_sections.get(entry.element, "")
        if not section_id:
            raise KeyError(f"Graph workspace entry is not projected: {entry.element}")
        return self._workspace_collection(section_id)

    def _begin_node_graph_workspace_rename(
        self, entry: GraphWorkspaceEntry
    ) -> None:
        if not entry.can_rename:
            return
        self._cancel_node_graph_workspace_rename()
        self._node_graph_workspace_activate(entry.element)
        collection = self._workspace_collection_for_entry(entry)
        collection.begin_rename(
            self._workspace_collection_item_id(entry.element),
            entry.primary,
        )
        self._workspace_rename_cancel_token = (
            TransientInteractionService.instance().begin(
                self.window_id,
                self._cancel_node_graph_workspace_rename,
                kind="graph_workspace_rename",
                priority=100,
                token_id=f"{self.window_id}:graph_workspace_rename",
            )
        )

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
        pending_add: Optional[ResolvedContextMenuCommand] = None

        add_actions = tuple(section.add_actions)
        self._workspace_add_actions[section.section_id] = {
            action.action_id: action for action in add_actions
        }

        def add_command(action: GraphWorkspaceAddAction) -> ContextMenuCommand:
            semantic_id = section.add_semantic_id
            if len(add_actions) > 1 and semantic_id:
                semantic_id = f"{semantic_id}.{action.action_id}"
            return ContextMenuCommand(
                "graph.workspace.add",
                label=action.label,
                payload={
                    "section_id": section.section_id,
                    "action_id": action.action_id,
                },
                semantic_id=semantic_id,
            )

        def request_add(action: GraphWorkspaceAddAction) -> None:
            nonlocal pending_add
            resolved = _GRAPH_WORKSPACE_CONTEXT_MENU_BUILDER.resolve(
                (add_command(action),)
            )
            if resolved and resolved[0].enabled:
                pending_add = resolved[0]

        popup_id = (
            f"##graph_workspace_add_{section.section_id}"
            if len(add_actions) > 1
            else ""
        )

        def build_add_popup(popup_ctx: InxGUIContext) -> None:
            nonlocal pending_add
            pending_add = _GRAPH_WORKSPACE_CONTEXT_MENU_BUILDER.render_deferred(
                popup_ctx,
                tuple(add_command(action) for action in add_actions),
            )

        resolved_add_actions = tuple(
            resolved
            for action in add_actions
            for resolved in _GRAPH_WORKSPACE_CONTEXT_MENU_BUILDER.resolve(
                (add_command(action),)
            )
        )

        render_workspace_add_header(
            ctx,
            section.title,
            section.section_id,
            popup_id=popup_id,
            on_add=(
                (lambda: request_add(add_actions[0]))
                if len(add_actions) == 1
                else None
            ),
            build_popup=build_add_popup if len(add_actions) > 1 else None,
            disabled=not any(item.enabled for item in resolved_add_actions),
            semantic_id=section.add_semantic_id,
        )

        graph_selection = getattr(self, "_graph_selection", None)
        primary = getattr(graph_selection, "primary", None)
        pending_select: Optional[GraphWorkspaceEntry] = None
        pending_rename: Optional[GraphWorkspaceEntry] = None
        pending_commit: Optional[tuple[GraphWorkspaceEntry, str]] = None

        entries = tuple(section.entries)
        current_elements = {entry.element for entry in entries}
        for retired in self._workspace_section_elements.get(section.section_id, set()) - current_elements:
            if self._workspace_entry_sections.get(retired) == section.section_id:
                self._workspace_entries.pop(retired, None)
                self._workspace_entry_sections.pop(retired, None)
        self._workspace_section_elements[section.section_id] = current_elements
        collection = self._workspace_collection(section.section_id)
        collection.set_items(
            self._workspace_collection_item_id(entry.element) for entry in entries
        )
        selected_elements = set(getattr(graph_selection, "elements", ()))
        visual_selected_ids = tuple(
            self._workspace_collection_item_id(entry.element)
            for entry in entries
            if (
                bool(entry.selected)
                if entry.selected is not None
                else entry.element in selected_elements
            )
        )
        primary_item_id = (
            self._workspace_collection_item_id(primary)
            if primary in current_elements
            and self._workspace_collection_item_id(primary) in visual_selected_ids
            else (visual_selected_ids[-1] if visual_selected_ids else "")
        )
        collection.project_selection(
            visual_selected_ids,
            primary_id=primary_item_id,
            preserve_anchor=True,
        )
        for index, entry in enumerate(entries):
            self._workspace_entries[entry.element] = entry
            self._workspace_entry_sections[entry.element] = section.section_id
            item_id = self._workspace_collection_item_id(entry.element)
            selected = item_id in collection.selection.selected_ids
            rename_session = collection.rename_session
            renaming = bool(rename_session and rename_session.item_id == item_id)
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
                if collection.consume_rename_focus():
                    ctx.set_keyboard_focus_here()
                edited_name = ctx.input_text_with_hint(
                    f"##graph_workspace_rename_{entry.element.kind.value}_{entry.element.stable_id}",
                    "",
                    collection.rename_session.buffer,
                    256,
                    1 << 6,
                )
                collection.update_rename(edited_name)
                if ctx.is_item_deactivated_after_edit():
                    candidate = collection.rename_candidate()
                    pending_commit = (entry, candidate[1] if candidate else "")
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
                if entry.element != primary:
                    if self._node_graph_workspace_activate(entry.element):
                        collection.activate(item_id)
                        primary = entry.element
                menu_result = _GRAPH_WORKSPACE_CONTEXT_MENU_BUILDER.render(
                    ctx,
                    (
                        ContextMenuCommand(
                            "edit.rename",
                            label=section.rename_label,
                            hide_when_disabled=True,
                        ),
                        ContextMenuCommand(
                            "edit.delete",
                            label=section.delete_label,
                            hide_when_disabled=True,
                        ),
                    ),
                )
                ctx.end_popup()
                if menu_result is not None and menu_result.result.accepted:
                    finish_workspace_entry(ctx)
                    return
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
        elif pending_rename is not None:
            self._begin_node_graph_workspace_rename(pending_rename)
        elif pending_select is not None:
            selected = self._run_node_graph_workspace_action(
                "selection",
                self._node_graph_workspace_activate,
                pending_select.element,
            )
            if selected:
                collection.activate(
                    self._workspace_collection_item_id(pending_select.element)
                )
        elif pending_add is not None:
            _GRAPH_WORKSPACE_CONTEXT_MENU_BUILDER.execute_resolved(pending_add)

    def _node_graph_authoring_model(self) -> Optional[NodeGraph]:
        graph = self._view.graph
        return graph if isinstance(graph, NodeGraph) else None

    def _node_graph_local_element_id(self, element: GraphElementRef) -> str:
        converter = getattr(self, "_graph_element_to_view", None)
        return str(converter(element) if callable(converter) else element.stable_id)

    def _node_graph_element_from_local(
        self, kind: GraphElementKind, stable_id: str
    ) -> GraphElementRef:
        converter = getattr(self, "_graph_element_from_view", None)
        return (
            converter(kind, stable_id)
            if callable(converter)
            else GraphElementRef(kind, stable_id)
        )

    def _node_graph_can_copy_node(self, stable_id: str) -> bool:
        graph = self._node_graph_authoring_model()
        node = graph.find_node(stable_id) if graph is not None else None
        definition = graph.get_type(node.type_id) if graph is not None and node else None
        return node is not None and (definition is None or definition.deletable)

    def _node_graph_clipboard_node_identity(
        self, _old_id: str, _payload: dict
    ) -> str:
        return uuid.uuid4().hex[:8]

    def _node_graph_clipboard_link_identity(
        self, _old_id: str, _payload: dict
    ) -> str:
        return uuid.uuid4().hex[:8]

    def _node_graph_remap_clipboard_node(
        self,
        _old_id: str,
        _new_id: str,
        payload: dict,
        _node_id_map,
    ) -> dict:
        return payload

    def _node_graph_remap_clipboard_link(
        self,
        _old_id: str,
        _new_id: str,
        payload: dict,
        _node_id_map,
    ) -> dict:
        return payload

    def _node_graph_copy(self) -> bool:
        """Publish one typed subgraph payload through the global clipboard."""
        graph = self._node_graph_authoring_model()
        selection = getattr(self, "_graph_selection", None)
        if graph is None or selection is None:
            return False
        local_ids = tuple(
            stable_id
            for element in selection.elements
            if element.kind is GraphElementKind.NODE
            and (stable_id := self._node_graph_local_element_id(element))
            and self._node_graph_can_copy_node(stable_id)
        )
        if not local_ids:
            return False
        clipboard = graph.capture_authoring_subgraph(local_ids)
        ClipboardService.instance().write(
            ClipboardDomain.GRAPH_ELEMENT,
            (
                ClipboardItem(
                    local_ids[0],
                    getattr(self, "document_id", ""),
                    "node_graph_subgraph",
                    clipboard,
                ),
            ),
            source_owner_id=self.window_id,
            reason="node_graph_copy",
        )
        return True

    def _node_graph_copyable_local_ids(self) -> tuple[str, ...]:
        selection = getattr(self, "_graph_selection", None)
        if selection is None:
            return ()
        return tuple(
            stable_id
            for element in selection.elements
            if element.kind is GraphElementKind.NODE
            and (stable_id := self._node_graph_local_element_id(element))
            and self._node_graph_can_copy_node(stable_id)
        )

    @staticmethod
    def _node_graph_clipboard_state(
        payload: Optional[ClipboardPayload],
    ) -> Optional[NodeGraphClipboardState]:
        if payload is None or len(payload.items) != 1:
            return None
        item = payload.items[0]
        if item.sub_kind != "node_graph_subgraph" or not isinstance(
            item.data, NodeGraphClipboardState
        ):
            return None
        return item.data

    def _node_graph_paste_state(
        self,
        clipboard: NodeGraphClipboardState,
        *,
        description: str,
    ) -> bool:
        """Apply one subgraph payload as a single graph transaction."""
        graph = self._node_graph_authoring_model()
        if graph is None:
            return False
        if clipboard.graph_kind != (graph.graph_kind or "node_graph"):
            return False
        before = graph.capture_authoring_state()
        node_identity = self._node_graph_clipboard_node_identity
        link_identity = self._node_graph_clipboard_link_identity
        try:
            result = graph.paste_authoring_subgraph(
                clipboard,
                node_identity=node_identity,
                link_identity=link_identity,
                node_payload=self._node_graph_remap_clipboard_node,
                link_payload=self._node_graph_remap_clipboard_link,
            )
        except (KeyError, RuntimeError, TypeError, ValueError) as exc:
            Debug.log_warning(f"Node Graph paste rejected: {exc}")
            return False
        selected = tuple(
            self._node_graph_element_from_local(GraphElementKind.NODE, stable_id)
            for stable_id in result.node_ids
        )
        if self._commit_node_graph_change(
            description,
            before,
            selection_after=selected,
        ):
            return True
        graph.restore_authoring_state(before)
        return False

    def _node_graph_paste(self) -> bool:
        """Paste through NodeGraph's atomic ID-remapping implementation."""
        clipboard = self._node_graph_clipboard_state(
            ClipboardService.instance().peek(ClipboardDomain.GRAPH_ELEMENT)
        )
        return bool(
            clipboard is not None
            and self._node_graph_paste_state(
                clipboard,
                description="Paste graph nodes",
            )
        )

    def _node_graph_delete_selection(self) -> bool:
        """Delete the selected graph elements through domain mutation hooks."""
        graph = self._node_graph_authoring_model()
        selection = getattr(self, "_graph_selection", None)
        if graph is None or selection is None:
            return False
        node_ids = self._node_graph_copyable_local_ids()
        link_ids = tuple(
            stable_id
            for element in selection.elements
            if element.kind is GraphElementKind.LINK
            and (stable_id := self._node_graph_local_element_id(element))
            and graph.find_link(stable_id) is not None
        )
        if not node_ids and not link_ids:
            return False
        before = graph.capture_authoring_state()
        delete_nodes = getattr(self, "_on_nodes_deleted", None)
        delete_link = getattr(self, "_on_link_deleted", None)
        if node_ids and callable(delete_nodes):
            delete_nodes(list(node_ids))
        elif link_ids and callable(delete_link):
            for stable_id in link_ids:
                delete_link(stable_id)
        else:
            return False
        changed = graph.capture_authoring_state() != before
        if changed:
            selection.clear(
                reason="node_graph_delete",
                record_history=False,
            )
        return changed

    def can_edit_copy(self) -> bool:
        return bool(self._node_graph_copyable_local_ids())

    def can_edit_cut(self) -> bool:
        return self.can_edit_copy()

    def can_edit_paste(self) -> bool:
        graph = self._node_graph_authoring_model()
        clipboard = self._node_graph_clipboard_state(
            ClipboardService.instance().peek(ClipboardDomain.GRAPH_ELEMENT)
        )
        return bool(
            graph is not None
            and clipboard is not None
            and clipboard.graph_kind == (graph.graph_kind or "node_graph")
        )

    def can_edit_delete(self) -> bool:
        graph = self._node_graph_authoring_model()
        selection = getattr(self, "_graph_selection", None)
        if graph is None or selection is None:
            return False
        workspace_entry = self._selected_workspace_entry()
        return bool(
            workspace_entry is not None
            and workspace_entry.can_delete
            or
            self._node_graph_copyable_local_ids()
            or any(
                element.kind is GraphElementKind.LINK
                and bool(
                    stable_id := self._node_graph_local_element_id(element)
                )
                and graph.find_link(stable_id) is not None
                for element in selection.elements
            )
        )

    def can_edit_duplicate(self) -> bool:
        return self.can_edit_copy()

    def can_edit_rename(self) -> bool:
        entry = self._selected_workspace_entry()
        return bool(entry is not None and entry.can_rename)

    def command_edit_copy(self) -> bool:
        return self._node_graph_copy()

    def command_edit_cut(self) -> bool:
        return self._node_graph_copy() and self._node_graph_delete_selection()

    def command_edit_paste(self) -> bool:
        return self._node_graph_paste()

    def command_edit_delete(self) -> bool:
        entry = self._selected_workspace_entry()
        if entry is not None and entry.can_delete:
            deleted = self._run_node_graph_workspace_action(
                "delete",
                self._node_graph_workspace_delete,
                entry.element,
            )
            collection = self._workspace_collection_for_entry(entry)
            rename = collection.rename_session
            if deleted and rename is not None and rename.item_id == self._workspace_collection_item_id(entry.element):
                self._cancel_node_graph_workspace_rename()
            return deleted
        return self._node_graph_delete_selection()

    def command_edit_rename(self) -> bool:
        entry = self._selected_workspace_entry()
        if entry is None or not entry.can_rename:
            return False
        self._begin_node_graph_workspace_rename(entry)
        return True

    def command_edit_duplicate(self) -> bool:
        graph = self._node_graph_authoring_model()
        if graph is None:
            return False
        clipboard = graph.capture_authoring_subgraph(
            self._node_graph_copyable_local_ids()
        )
        return self._node_graph_paste_state(
            clipboard,
            description="Duplicate graph nodes",
        )

    def _selected_workspace_entry(self) -> Optional[GraphWorkspaceEntry]:
        selection = getattr(self, "_graph_selection", None)
        primary = selection.primary if selection is not None else None
        if primary is None or primary.kind in {
            GraphElementKind.NODE,
            GraphElementKind.LINK,
        }:
            return None
        return self._workspace_entries.get(primary)

    def _execute_node_graph_command(
        self,
        command_id: str,
        *,
        source: CommandSource = CommandSource.CONTEXT_MENU,
        payload: Optional[Mapping[str, object]] = None,
    ) -> bool:
        return self.execute_owned_command(command_id, source=source, payload=payload)

    def _on_graph_copy(self) -> None:
        self._node_graph_copy()

    def _on_graph_paste(self) -> None:
        self._node_graph_paste()

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

    def _node_graph_document(self):
        from Infernux.engine.interaction import DocumentRegistry

        return DocumentRegistry.instance().get(self.document_id)

    @staticmethod
    def _node_graph_parameter_mutations(
        transaction: GraphParameterTransaction,
    ) -> tuple[GraphMutation, ...]:
        """Project portable parameter diffs into the shared graph journal."""
        if not isinstance(transaction, GraphParameterTransaction):
            raise TypeError("graph parameter edit requires a parameter transaction")
        mutations = []
        for diff in transaction.diffs:
            before = diff.before.to_dict() if diff.before is not None else None
            after = diff.after.to_dict() if diff.after is not None else None
            if diff.before is None:
                kind = GraphMutationKind.INSERT
            elif diff.after is None:
                kind = GraphMutationKind.REMOVE
            elif diff.before == diff.after:
                kind = GraphMutationKind.MOVE
            else:
                kind = GraphMutationKind.UPDATE
            mutations.append(
                GraphMutation(
                    kind,
                    GraphElementRef(GraphElementKind.PARAMETER, diff.stable_id),
                    before=before,
                    after=after,
                    before_index=(
                        -1 if diff.before_index is None else diff.before_index
                    ),
                    after_index=(
                        -1 if diff.after_index is None else diff.after_index
                    ),
                )
            )
        return tuple(mutations)

    def _execute_graph_parameter_transaction(
        self,
        description: str,
        transaction: GraphParameterTransaction,
        *,
        mutations_before: tuple[GraphMutation, ...] = (),
        mutations_after: tuple[GraphMutation, ...] = (),
        merge_key: str = "",
        selection_after: Optional[tuple[GraphElementRef, ...]] = None,
    ) -> bool:
        parameter_mutations = self._node_graph_parameter_mutations(transaction)
        return self._execute_graph_mutations(
            description,
            (*mutations_before, *parameter_mutations, *mutations_after),
            merge_key=merge_key,
            selection_after=selection_after,
        )

    @staticmethod
    def _node_graph_undo_enabled() -> bool:
        from Infernux.engine.interaction import AuthoringMutationService

        return AuthoringMutationService.require().can_record(require_edit_mode=True)

    def _execute_graph_mutations(
        self,
        description: str,
        mutations: tuple[GraphMutation, ...],
        *,
        merge_key: str = "",
        selection_after: Optional[tuple[GraphElementRef, ...]] = None,
    ) -> bool:
        """Execute one domain diff through the shared graph transaction host."""
        from Infernux.engine.interaction import (
            AuthoringMutationService,
            SelectionService,
            SelectionSnapshot,
        )
        from Infernux.engine.undo import GraphDiffCommand

        document = self._node_graph_document()
        if document is None or not mutations:
            return False
        # FSM, Particle Graph, and future graph domains all pass through this
        # Publish the owning view before the shared journal captures context.
        self.publish_interaction_ownership(reason="node_graph_owned_edit")
        mutations_service = AuthoringMutationService.require()
        before_selection = None
        after_selection = None
        if selection_after is not None:
            selection = SelectionService.instance()
            before_selection = selection.snapshot
            targets = tuple(
                element.selection_target(document.document_id)
                for element in selection_after
            )
            after_selection = SelectionSnapshot.create(
                targets,
                primary=targets[-1] if targets else None,
                anchor=targets[0] if targets else None,
                owner_id=self.window_id if targets else "",
            )
        applied = mutations_service.execute_command(
            document.document_id,
            lambda before_revision, after_revision: GraphDiffCommand(
                description,
                GraphActionDiff(
                    document.document_id,
                    tuple(mutations),
                    before_revision=before_revision,
                    after_revision=after_revision,
                ),
                merge_key=merge_key,
            ),
            view_id=self.window_id,
            before_selection=before_selection,
            after_selection=after_selection,
            require_edit_mode=True,
        )
        if applied and selection_after is not None:
            selection = getattr(self, "_graph_selection", None)
            if selection is None:
                raise RuntimeError("Graph editor has no selection controller")
            if selection_after:
                selection.select(
                    selection_after,
                    reason="node_graph_edit_selection",
                    record_history=False,
                )
            else:
                selection.clear(
                    reason="node_graph_edit_selection",
                    record_history=False,
                )
        return applied

    def _node_graph_can_drag_node(self, _stable_id: str) -> bool:
        return True

    def _node_graph_drag_description(self, _stable_id: str) -> str:
        return "Move graph node"

    def _node_graph_drag_merge_key(self, stable_id: str) -> str:
        return f"node:{stable_id}:position"

    def _on_node_drag_start(self, stable_id: str) -> None:
        if self._node_graph_drag_cancel_token:
            TransientInteractionService.instance().end(
                self._node_graph_drag_cancel_token
            )
            self._node_graph_drag_cancel_token = ""
        graph = self._node_graph_authoring_model()
        if graph is None or not self._node_graph_can_drag_node(stable_id):
            self._node_graph_drag_snapshot = None
            return
        self._node_graph_drag_snapshot = graph.capture_authoring_state()
        self._node_graph_drag_cancel_token = (
            TransientInteractionService.instance().begin(
                self.window_id,
                self._cancel_node_graph_drag,
                kind="graph_node_drag",
                priority=100,
                token_id=f"{self.window_id}:graph_node_drag",
            )
        )

    def _cancel_node_graph_drag(self) -> bool:
        self._node_graph_drag_cancel_token = ""
        before = self._node_graph_drag_snapshot
        self._node_graph_drag_snapshot = None
        self._view.cancel_node_drag()
        graph = self._node_graph_authoring_model()
        if before is None or graph is None:
            return False
        graph.restore_authoring_state(before)
        return True

    def _on_node_drag_end(self, stable_id: str) -> None:
        if self._node_graph_drag_cancel_token:
            TransientInteractionService.instance().end(
                self._node_graph_drag_cancel_token
            )
            self._node_graph_drag_cancel_token = ""
        before = self._node_graph_drag_snapshot
        self._node_graph_drag_snapshot = None
        if before is None or not self._node_graph_can_drag_node(stable_id):
            return
        self._commit_node_graph_change(
            self._node_graph_drag_description(stable_id),
            before,
            merge_key=self._node_graph_drag_merge_key(stable_id),
        )

    def _on_canvas_selection_changed(
        self,
        node_ids: tuple[str, ...],
        link_id: str,
        record_history: bool,
    ) -> bool:
        selection = getattr(self, "_graph_selection", None)
        if selection is None:
            raise RuntimeError("Graph editor has no selection controller")
        accepted = selection.accept_view_selection(
            node_ids,
            link_id,
            record_history=record_history,
        )
        if accepted:
            self._on_node_graph_selection_accepted(node_ids, link_id)
        return accepted

    def _on_node_graph_selection_accepted(
        self,
        _node_ids: tuple[str, ...],
        _link_id: str,
    ) -> None:
        pass

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
            "on_node_creation_command": ("_execute_node_creation_command",),
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
            "on_view_gesture_committed": (
                "_on_node_graph_view_gesture_committed",
            ),
            "on_canvas_drop": ("_on_canvas_drop",),
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

    def _on_node_graph_view_gesture_committed(
        self,
        kind: str,
        before: tuple[float, float, float],
        after: tuple[float, float, float],
    ) -> bool:
        description = {
            "pan": "Pan Node Graph View",
            "zoom": "Zoom Node Graph View",
        }.get(str(kind), "Change Node Graph View")
        return ViewCommandService.require().set_value(
            before,
            after,
            self._apply_node_graph_view_state,
            description=description,
        )

    def _render_node_graph_toolbar(self, ctx: InxGUIContext) -> None:
        del ctx

    def _render_node_graph_left_panel(self, ctx: InxGUIContext) -> None:
        del ctx

    def _render_node_graph_detail_panel(self, ctx: InxGUIContext) -> None:
        GraphDetailHost.render(ctx, self._node_graph_detail_contributors())

    def _node_graph_detail_contributors(
        self,
    ) -> tuple[GraphDetailContributor, ...]:
        return (
            GraphDetailContributor(
                "parameter",
                100,
                self._is_node_graph_parameter_detail_active,
                lambda ctx: self._render_node_graph_parameter_detail(ctx),
            ),
        )

    def _is_node_graph_parameter_detail_active(self) -> bool:
        selection = getattr(self, "_graph_selection", None)
        element = selection.primary if selection is not None else None
        return bool(
            element is not None
            and element.kind is GraphElementKind.PARAMETER
        )

    def _node_graph_parameter_policy(
        self,
    ) -> Optional[GraphParameterAuthoringPolicy]:
        return None

    def _node_graph_parameter_collection(
        self,
    ) -> Optional[GraphParameterCollection]:
        return None

    def _node_graph_parameter_detail_config(self) -> GraphParameterDetailConfig:
        return GraphParameterDetailConfig()

    def _node_graph_commit_parameter_changes(
        self,
        stable_id: str,
        changes: dict,
    ) -> bool:
        del stable_id, changes
        raise NotImplementedError("Graph parameter editing is not implemented")

    def _render_node_graph_parameter_default(
        self,
        ctx: InxGUIContext,
        parameter: GraphParameterDefinition,
        value_type: ValueType,
        value,
        *,
        label: str,
        semantic_prefix: str,
    ):
        """Render portable scalar/vector defaults shared by every graph."""
        widget_id = (
            f"##{self.window_id}_graph_parameter_{parameter.stable_id}_default"
        )
        if value_type is ValueType.BOOL:
            return bool(ctx.checkbox(widget_id, bool(value)))
        if value_type in {ValueType.I32, ValueType.U32}:
            method = ctx.input_uint if value_type is ValueType.U32 else ctx.input_int
            return int(method(widget_id, int(value)))
        if value_type is ValueType.F32:
            return preserve_ui_float_precision(
                float(ctx.drag_float(widget_id, float(value), 0.05, -1.0e7, 1.0e7)),
                value,
            )
        if value_type in {
            ValueType.VEC2,
            ValueType.VEC3,
            ValueType.VEC4,
            ValueType.COLOR,
            ValueType.MAT3,
            ValueType.MAT4,
        }:
            components = list(value)
            edited = [
                float(
                    ctx.drag_float(
                        f"{label} {('XYZW'[index] if index < 4 else index)}"
                        f"{widget_id}_{index}",
                        float(component),
                        0.05,
                        -1.0e7,
                        1.0e7,
                    )
                )
                for index, component in enumerate(components)
            ]
            return preserve_ui_float_precision(edited, value)
        del semantic_prefix
        return copy.deepcopy(value)

    def _render_node_graph_parameter_detail(self, ctx: InxGUIContext) -> bool:
        """Render the selected Blackboard parameter through one shared contract."""
        selection = getattr(self, "_graph_selection", None)
        element = selection.primary if selection is not None else None
        if element is None or element.kind is not GraphElementKind.PARAMETER:
            return False
        policy = self._node_graph_parameter_policy()
        collection = self._node_graph_parameter_collection()
        if policy is None or collection is None:
            raise RuntimeError("Graph parameter selection has no authoring policy")
        parameter = collection.find(element.stable_id)
        if parameter is None:
            selection.clear(
                reason="node_graph_drop_missing_parameter",
                record_history=False,
            )
            return False

        config = self._node_graph_parameter_detail_config()
        ctx.label(config.title)
        ctx.separator()
        changes = {}

        ctx.label(config.name_label)
        ctx.set_next_item_width(-1)
        name = ctx.text_input(
            f"##{self.window_id}_graph_parameter_name",
            parameter.name,
            128,
        ).strip()
        if name and name != parameter.name:
            changes["name"] = name

        kinds = tuple(policy.value_types)
        current_kind = parameter.value_type.value_type
        if current_kind not in kinds:
            raise RuntimeError(
                f"Graph parameter uses unsupported type: {current_kind.value}"
            )
        ctx.label(config.type_label)
        ctx.set_next_item_width(-1)
        type_index = kinds.index(current_kind)
        selected_index = ctx.combo(
            f"##{self.window_id}_graph_parameter_type",
            type_index,
            [config.value_type_label(kind) for kind in kinds],
            len(kinds),
        )
        selected_kind = kinds[max(0, min(selected_index, len(kinds) - 1))]

        spaces = tuple(
            policy.allowed_spaces.get(selected_kind, (CoordinateSpace.NONE,))
        )
        selected_space = (
            parameter.value_type.space
            if selected_kind is current_kind and parameter.value_type.space in spaces
            else spaces[0]
        )
        if len(spaces) > 1:
            ctx.label(config.space_label)
            ctx.set_next_item_width(-1)
            space_index = ctx.combo(
                f"##{self.window_id}_graph_parameter_space",
                spaces.index(selected_space),
                [config.coordinate_space_label(space) for space in spaces],
                len(spaces),
            )
            selected_space = spaces[
                max(0, min(space_index, len(spaces) - 1))
            ]
        edited_type = TypeRef(selected_kind, selected_space)
        if edited_type != parameter.value_type:
            changes["value_type"] = edited_type

        default = (
            copy.deepcopy(policy.default_for_type(selected_kind))
            if selected_kind is not current_kind
            else copy.deepcopy(parameter.default)
        )
        ctx.label(config.default_label)
        edited_default = self._render_node_graph_parameter_default(
            ctx,
            parameter,
            selected_kind,
            default,
            label=config.default_label,
            semantic_prefix=f"{config.semantic_prefix}.{parameter.stable_id}.default",
        )
        if edited_default != default:
            changes["default"] = edited_default

        if config.show_exposed:
            exposed = bool(
                ctx.checkbox(
                    f"{config.exposed_label}##{self.window_id}_graph_parameter_exposed",
                    parameter.exposed,
                )
            )
            if exposed != parameter.exposed:
                changes["exposed"] = exposed
        if config.show_writable:
            writable_supported = (
                policy.writable_types is None
                or selected_kind in policy.writable_types
            )
            if not writable_supported:
                ctx.begin_disabled(True)
            writable = bool(
                ctx.checkbox(
                    f"{config.writable_label}##{self.window_id}_graph_parameter_writable",
                    parameter.writable if writable_supported else False,
                )
            )
            if not writable_supported:
                ctx.end_disabled()
            if writable_supported and writable != parameter.writable:
                changes["writable"] = writable
            elif not writable_supported and parameter.writable:
                changes["writable"] = False
        if config.show_tooltip:
            tooltip = ctx.text_input(
                f"{config.tooltip_label}##{self.window_id}_graph_parameter_tooltip",
                parameter.tooltip,
                512,
            )
            if tooltip != parameter.tooltip:
                changes["tooltip"] = tooltip

        if changes:
            try:
                self._node_graph_commit_parameter_changes(
                    parameter.stable_id,
                    changes,
                )
            except (KeyError, TypeError, ValueError) as exc:
                Debug.log_warning(f"Graph parameter edit rejected: {exc}")
        return True

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
    "GraphParameterDetailConfig",
    "GraphWorkspaceAddAction",
    "GraphWorkspaceDrag",
    "GraphWorkspaceEntry",
    "GraphWorkspaceSection",
    "NodeGraphEditorPanel",
]
