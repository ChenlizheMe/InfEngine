"""
Animation State Machine Editor — visual node-graph editor for .animfsm files.

Displays states as nodes with connections representing transitions.
Drag from an output pin to an input pin to create a transition.
Click a node to edit its properties in the right-side inspector.
Opened from the Animation menu or by double-clicking a .animfsm file
in the Project panel.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from typing import Dict, List, Optional, Tuple

from Infernux.engine.path_utils import path_key, resolved_path, same_path
from Infernux.core.anim_state_machine import (
    AnimStateMachine,
    AnimState,
    AnimTransition,
    AnimCondition,
    AnimParameter,
)
from Infernux.core.asset_ref import AnimationClipRef, AnimationClip3DRef
from Infernux.core.asset_reference_types import (
    AssetReferenceCodec,
    asset_type_registry,
    resolve_asset_reference_path,
)
from Infernux.core.node_graph import (
    GraphLink,
    GraphNode,
    NodeGraph,
    NodeGraphAuthoringState,
    NodeGraphElementKind,
    NodeGraphMutation,
    NodeGraphMutationKind,
    PinKind,
)
from Infernux.debug import Debug
from Infernux.graph.parameters import (
    GraphParameterAuthoringPolicy,
    GraphParameterCollection,
)
from Infernux.graph.parameter_transactions import GraphParameterTransaction
from Infernux.graph.types import TypeRef, ValueType
from Infernux.engine.i18n import t
from Infernux.engine.interaction import (
    BoundPanelCommand,
    GraphActionDiff,
    GraphElementKind,
    GraphElementRef,
    GraphMutation,
    GraphMutationKind,
    PanelCommandAdapter,
    PanelCommandSpec,
    PanelInteractionDescriptor,
    PanelViewStateField,
    PanelViewStateSchema,
)
from Infernux.lib import InxGUIContext

from .asset_save_dialog import AssetSaveAsDialog
from .animfsm_graph_authoring import (
    AnimFSMGraphAuthoringModel,
    FSM_ENTRY_NODE_TYPE_ID,
    FSM_STATE_NODE_TYPE_ID,
)
from .graph_details import GraphDetailContributor
from .node_graph_editor_panel import (
    GraphParameterDetailConfig,
    GraphWorkspaceAddAction,
    GraphWorkspaceEntry,
    GraphWorkspaceSection,
    NODE_GRAPH_PANEL_INTERACTION,
    NodeGraphEditorPanel,
)
from .node_graph_view import NodeCreationEntry
from ._inspector_references import render_asset_reference_field
from .inspector_utils import field_label, max_label_w
from .panel_registry import editor_panel
from .theme import ImGuiCol, Theme
from .igui import IGUI


def _bind_animfsm_panel(panel: object) -> PanelCommandAdapter:
    required = (
        "command_new_fsm",
        "can_new_fsm",
        "command_switch_mode",
        "can_switch_mode",
    )
    missing = tuple(
        name for name in required if not callable(getattr(panel, name, None))
    )
    if missing:
        raise TypeError(f"animation FSM panel interaction contract is missing: {missing}")

    base_factory = NODE_GRAPH_PANEL_INTERACTION.adapter_factory
    if base_factory is None:
        raise RuntimeError("node graph panel interaction has no adapter factory")
    base = base_factory(panel)
    handlers = {
        spec.command_id: base.handler(spec.command_id)
        for spec in NODE_GRAPH_PANEL_INTERACTION.commands
    }
    if any(handler is None for handler in handlers.values()):
        raise RuntimeError("node graph panel interaction adapter is incomplete")
    handlers["animfsm.new"] = BoundPanelCommand(
        lambda _context: panel.command_new_fsm(),
        lambda _context: panel.can_new_fsm(),
    )
    handlers["animfsm.switch_mode"] = BoundPanelCommand(
        lambda context: panel.command_switch_mode(
            str(context.payload.get("mode", "") or "")
        ),
        lambda context: panel.can_switch_mode(
            str(context.payload.get("mode", "") or "")
        ),
    )
    return PanelCommandAdapter(handlers)


_ANIMFSM_PANEL_INTERACTION = PanelInteractionDescriptor(
    commands=NODE_GRAPH_PANEL_INTERACTION.commands
    + (
        PanelCommandSpec("animfsm.new"),
        PanelCommandSpec("animfsm.switch_mode"),
    ),
    shortcuts=NODE_GRAPH_PANEL_INTERACTION.shortcuts,
    owned_selection_domains=NODE_GRAPH_PANEL_INTERACTION.owned_selection_domains,
    records_focus_history=NODE_GRAPH_PANEL_INTERACTION.records_focus_history,
    document_backed=NODE_GRAPH_PANEL_INTERACTION.document_backed,
    adapter_factory=_bind_animfsm_panel,
)


_FSM_ENTRY_NODE_ID = "animfsm.entry"
_FSM_ENTRY_LINK_ID = "animfsm.entry-link"
_FSM_GRAPH_ID = "animfsm.graph"


_OPS = ["<", ">", "<=", ">=", "==", "!="]


def _sanitize_animation_parameter_name(raw: str) -> str:
    """Keep ``[A-Za-z_][A-Za-z0-9_]*`` for condition variable names."""

    value = str(raw or "").strip()
    return "".join(
        character
        for index, character in enumerate(value)
        if (
            (index == 0 and (character.isalpha() or character == "_"))
            or (index > 0 and (character.isalnum() or character == "_"))
        )
    )


def _animation_parameter_default(kind: ValueType):
    return {
        ValueType.BOOL: False,
        ValueType.I32: 0,
        ValueType.F32: 0.0,
    }[ValueType(kind)]


_ANIMATION_PARAMETER_POLICY = GraphParameterAuthoringPolicy(
    AnimParameter,
    (ValueType.BOOL, ValueType.F32, ValueType.I32),
    _animation_parameter_default,
    writable_types=frozenset({ValueType.BOOL, ValueType.F32, ValueType.I32}),
    normalize_name=_sanitize_animation_parameter_name,
)


_FSM_PARAM_COLORS = {
    "bool": (0.78, 0.25, 0.31, 1.0),
    "float": (0.34, 0.72, 0.42, 1.0),
    "int": (0.30, 0.68, 0.52, 1.0),
}

# ═══════════════════════════════════════════════════════════════════════════
# Panel
# ═══════════════════════════════════════════════════════════════════════════

@editor_panel(
    "Animation State Machine Editor",
    type_id="animfsm_editor",
    title_key="panel.animfsm_editor",
    menu_path="Animation",
    interaction=_ANIMFSM_PANEL_INTERACTION,
)
class AnimFSMEditorPanel(NodeGraphEditorPanel):
    VIEW_STATE_SCHEMA = PanelViewStateSchema(
        "animfsm_editor.view",
        (
            PanelViewStateField("pan_x", "_view.pan_x", float),
            PanelViewStateField("pan_y", "_view.pan_y", float),
            PanelViewStateField("zoom", "_view.zoom", float),
        ),
    )
    """Node-graph editor for animation state machines."""

    window_id = "animfsm_editor"

    def __init__(self):
        super().__init__(
            title="Animation State Machine Editor",
            window_id=self.window_id,
            semantic_namespace="animfsm.graph",
        )
        from Infernux.engine.interaction import AuthoringDocumentController

        self._authoring_document_controller = AuthoringDocumentController(self)
        self._fsm: Optional[AnimStateMachine] = None
        self._file_path: str = ""
        self._save_as_dialog = AssetSaveAsDialog(
            "animfsm.save_as",
            "state machine",
            owner_id=self._window_id,
        )

        # Node graph
        self._graph = AnimFSMGraphAuthoringModel(
            AnimStateMachine(name="New State Machine"),
        )

        self._bind_node_graph_model(self._graph, preserve_selection=False)

        self._install_graph_selection_controller(
            contains=self._contains_graph_element,
        )
        # Maps: state name ↔ node uid
        self._name_to_uid: Dict[str, str] = {}
        self._uid_to_name: Dict[str, str] = {}

        # Entry node uid
        self._entry_uid: str = ""

        self._pending_save_ticket_id: str = ""

        # Start with a blank FSM
        self._new_fsm_immediate()

    # ── Public API ────────────────────────────────────────────────────

    def _normalize_fsm_path(self, path: str) -> str:
        """Return a normalized absolute FSM path when possible."""
        p = (path or "").strip()
        if not p:
            return ""
        if os.path.isabs(p):
            return resolved_path(p)
        try:
            from Infernux.engine.project_context import get_project_root

            root = get_project_root()
        except Exception:
            root = None
        if root:
            return resolved_path(os.path.join(root, p))
        return resolved_path(p)

    @staticmethod
    def _fsm_document_key(path: str):
        from Infernux.engine.interaction import DocumentKey, DocumentKind

        normalized = resolved_path(path)
        try:
            from Infernux.core.asset_types import read_meta_guid

            guid = read_meta_guid(normalized)
        except Exception:
            guid = ""
        if guid:
            return DocumentKey.asset(DocumentKind.ANIMATION_FSM, guid)
        return DocumentKey.resource(DocumentKind.ANIMATION_FSM, normalized)

    def _replace_fsm_document(self, *, resource_path: str, dirty: bool) -> None:
        from Infernux.engine.interaction import (
            DocumentCapability,
            DocumentKey,
            DocumentKind,
            DocumentRegistry,
        )

        path = self._normalize_fsm_path(resource_path) if resource_path else ""
        title = os.path.splitext(os.path.basename(path))[0] if path else "State Machine"
        registry = DocumentRegistry.instance()
        key = self._fsm_document_key(path) if path else DocumentKey.session(
            DocumentKind.ANIMATION_FSM
        )
        document, _created = registry.open_or_create(
            key,
            title,
            resource_path=path,
            revision=1 if dirty else 0,
            saved_revision=0,
            capabilities=(
                DocumentCapability.SAVE
                | DocumentCapability.SAVE_AS
                | DocumentCapability.DISCARD
            ),
            controller=self._authoring_document_controller,
        )
        self._bind_replaced_document(document.document_id, dirty=dirty)
        self._graph_selection.refresh()

    def resource_moved(
        self,
        *,
        document_id: str,
        source_path: str,
        destination_path: str,
        guid: str,
    ) -> None:
        del guid
        if document_id != self.document_id:
            return
        if self._file_path and same_path(self._file_path, source_path):
            self._file_path = resolved_path(destination_path)
            if self._fsm is not None:
                self._fsm.file_path = self._file_path
                self._fsm.name = os.path.splitext(os.path.basename(self._file_path))[0]
            self._persist_panel_state()

    def _fsm_document(self):
        from Infernux.engine.interaction import DocumentRegistry

        return DocumentRegistry.instance().get(self.document_id)

    def capture_document_restore_state(self, document_id: str) -> dict:
        if document_id != self.document_id or self._fsm is None:
            raise ValueError("FSM restore capture targeted another document")
        self._sync_fsm_from_graph()
        return {
            "fsm": self._fsm.to_dict(),
            "file_path": self._file_path,
        }

    def restore_document_restore_state(self, state: dict) -> None:
        self._fsm = AnimStateMachine.from_dict(copy.deepcopy(state["fsm"]))
        self._file_path = self._normalize_fsm_path(state.get("file_path", ""))
        self._fsm.file_path = self._file_path
        self._sync_graph_from_fsm()
        self._graph_selection.clear(record_history=False)

    def recover_incompatible_document_restore_state(
        self,
        state,
        error: Exception,
    ) -> bool:
        del error
        path = str(state.get("file_path", "")) if isinstance(state, dict) else ""
        if path and os.path.isfile(path):
            return self.open_document_resource_immediate(path)
        self._new_fsm_immediate()
        return True

    def capture_graph_diff_checkpoint(self):
        """Capture an exception-only checkpoint for atomic diff replay."""
        return (copy.deepcopy(self._fsm), self._graph.capture_authoring_state())

    def restore_graph_diff_checkpoint(self, checkpoint) -> None:
        self._fsm, graph_state = checkpoint
        self._graph.restore_authoring_state(graph_state)
        self._bind_node_graph_model(self._graph)

    def _node_graph_remap_clipboard_node(
        self,
        _old_id: str,
        new_id: str,
        payload: dict,
        _node_id_map,
    ) -> dict:
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            raise ValueError("Animation FSM clipboard node has no property payload")
        state_document = properties.get("fsm_state")
        if not isinstance(state_document, dict):
            raise ValueError("Animation FSM clipboard node has no state payload")
        state_document = copy.deepcopy(state_document)
        state_document["stable_id"] = new_id
        state_document["name"] = self._unique_state_name(
            str(state_document.get("name", "State"))
        )
        state_document["transitions"] = []
        properties = copy.deepcopy(properties)
        properties["fsm_state"] = state_document
        properties["label"] = state_document["name"]
        payload = copy.deepcopy(payload)
        payload["properties"] = properties
        return payload

    def _node_graph_remap_clipboard_link(
        self,
        _old_id: str,
        new_id: str,
        payload: dict,
        _node_id_map,
    ) -> dict:
        properties = payload.get("properties")
        if not isinstance(properties, dict):
            raise ValueError("Animation FSM clipboard link has no property payload")
        transition = properties.get("fsm_transition")
        if not isinstance(transition, dict):
            raise ValueError("Animation FSM clipboard link has no transition payload")
        transition = copy.deepcopy(transition)
        transition["stable_id"] = new_id
        properties = copy.deepcopy(properties)
        properties["fsm_transition"] = transition
        payload = copy.deepcopy(payload)
        payload["properties"] = properties
        return payload

    def _contains_graph_element(self, element: GraphElementRef) -> bool:
        if element.kind is GraphElementKind.GRAPH:
            return element.stable_id == _FSM_GRAPH_ID and self._fsm is not None
        if element.kind is GraphElementKind.NODE:
            return self._graph.find_node(element.stable_id) is not None
        if element.kind is GraphElementKind.LINK:
            return self._graph.find_link(element.stable_id) is not None
        if element.kind is GraphElementKind.PARAMETER:
            return self._fsm is not None and any(
                parameter.stable_id == element.stable_id
                for parameter in self._fsm.parameters
            )
        return False

    def _state_index_by_id(self, stable_id: str) -> int:
        if self._fsm is None:
            return -1
        return next(
            (
                index
                for index, state in enumerate(self._fsm.states)
                if state.stable_id == stable_id
            ),
            -1,
        )

    def _transition_document(self, stable_id: str) -> Optional[dict]:
        if self._fsm is None:
            return None
        found = self._fsm.get_transition_by_id(stable_id)
        if found is None:
            return None
        owner, transition = found
        return {
            "source_state_id": owner.stable_id,
            "transition": transition.to_dict(),
        }

    def _parameter_by_id(self, stable_id: str) -> Optional[AnimParameter]:
        if self._fsm is None:
            return None
        parameter = GraphParameterCollection(self._fsm.parameters).find(stable_id)
        return parameter if isinstance(parameter, AnimParameter) else None

    def _parameter_index_by_id(self, stable_id: str) -> int:
        if self._fsm is None:
            return -1
        return GraphParameterCollection(self._fsm.parameters).index_of(stable_id)

    def _selected_parameter(self) -> Optional[AnimParameter]:
        return self._parameter_by_id(
            self._graph_selection.primary_id(GraphElementKind.PARAMETER)
        )

    @staticmethod
    def _parameter_document_for_kind(
        parameter: AnimParameter, kind: ValueType
    ) -> dict:
        edit = _ANIMATION_PARAMETER_POLICY.update(
            GraphParameterCollection((parameter,)),
            parameter.stable_id,
            {"value_type": TypeRef(ValueType(kind))},
        )
        if not isinstance(edit.after, AnimParameter):
            raise RuntimeError("animation parameter policy returned an invalid value")
        return edit.after.to_dict()

    def _insert_parameter(self) -> bool:
        if self._fsm is None:
            return False
        edit = _ANIMATION_PARAMETER_POLICY.create(
            GraphParameterCollection(self._fsm.parameters),
            name=f"var_{len(self._fsm.parameters)}",
            value_type=ValueType.F32,
            writable=True,
        )
        parameter = edit.after
        if not isinstance(parameter, AnimParameter):
            raise RuntimeError("animation parameter policy returned an invalid value")
        ref = GraphElementRef(GraphElementKind.PARAMETER, parameter.stable_id)
        transaction = GraphParameterTransaction.begin(
            GraphParameterCollection(self._fsm.parameters)
        ).create(parameter)
        return self._execute_graph_parameter_transaction(
            "Add parameter",
            transaction,
            selection_after=(ref,),
        )

    def _remove_parameter(self, stable_id: str) -> bool:
        parameter = self._parameter_by_id(stable_id)
        index = self._parameter_index_by_id(stable_id)
        if parameter is None or index < 0:
            return False
        graph_before = self._graph.capture_authoring_state()
        for link in self._graph.links:
            document = self._graph_transition_document(link.uid)
            if document is None:
                continue
            conditions = [
                condition
                for condition in document["conditions"]
                if condition["parameter_id"] != stable_id
            ]
            if conditions == document["conditions"]:
                continue
            document["conditions"] = conditions
            link.data = self._transition_graph_properties(
                AnimTransition.from_dict(document)
            )
        graph_after = self._graph.capture_authoring_state()
        graph_mutations = self._node_graph_mutations(graph_before, graph_after)
        self._graph.restore_authoring_state(graph_before)
        selected = self._graph_selection.primary_id(GraphElementKind.PARAMETER)
        transaction = GraphParameterTransaction.begin(
            GraphParameterCollection(self._fsm.parameters)
        ).delete(stable_id)
        return self._execute_graph_parameter_transaction(
            "Remove parameter",
            transaction,
            mutations_before=graph_mutations,
            selection_after=() if selected == stable_id else None,
        )

    def _update_parameter_document(
        self,
        parameter: AnimParameter,
        after: dict,
        description: str,
        *,
        merge_key: str,
    ) -> bool:
        before = parameter.to_dict()
        if after == before:
            return False
        values = {
            key: copy.deepcopy(value)
            for key, value in after.items()
            if key != "stable_id" and before.get(key) != value
        }
        if not values:
            return False
        edit = _ANIMATION_PARAMETER_POLICY.update(
            GraphParameterCollection(self._fsm.parameters),
            parameter.stable_id,
            values,
        )
        replacement = edit.after
        if not isinstance(replacement, AnimParameter):
            raise RuntimeError("animation parameter policy returned an invalid value")
        transaction = GraphParameterTransaction.begin(
            GraphParameterCollection(self._fsm.parameters)
        ).update(replacement)
        return self._execute_graph_parameter_transaction(
            description,
            transaction,
            merge_key=merge_key,
        )

    def _update_graph_document(
        self,
        *,
        name: Optional[str] = None,
        mode: Optional[str] = None,
        description: str,
        merge_key: str,
    ) -> bool:
        if self._fsm is None:
            return False
        before = {"name": self._fsm.name, "mode": self._fsm.mode}
        after = dict(before)
        if name is not None:
            after["name"] = str(name)
        if mode is not None:
            after["mode"] = str(mode)
        if before == after:
            return False
        return self._execute_graph_mutations(
            description,
            (
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    GraphElementRef(GraphElementKind.GRAPH, _FSM_GRAPH_ID),
                    before=before,
                    after=after,
                ),
            ),
            merge_key=merge_key,
        )

    def _update_state_document(
        self,
        state: AnimState,
        after: dict,
        description: str,
        *,
        merge_key: str,
    ) -> bool:
        node = self._graph.find_node(state.stable_id)
        before_document = self._graph_state_document(state.stable_id)
        if node is None or before_document is None:
            return False
        after = copy.deepcopy(after)
        after["transitions"] = []
        if before_document == after:
            return False
        replacement = AnimState.from_dict(copy.deepcopy(after))
        if replacement.stable_id != state.stable_id:
            raise RuntimeError("animation state update changed stable identity")
        duplicate_uid = self._graph_state_uid_by_name(replacement.name)
        if duplicate_uid and duplicate_uid != state.stable_id:
            raise ValueError(f"animation state name already exists: {replacement.name}")
        replacement.position = [float(node.pos_x), float(node.pos_y)]
        before = self._graph.capture_authoring_state()
        node.data = self._state_graph_properties(replacement)
        if before_document.get("name") != replacement.name:
            for link in self._graph.links:
                if link.uid == _FSM_ENTRY_LINK_ID:
                    if link.target_node == node.uid:
                        link.data["default_state"] = replacement.name
                    continue
                if link.target_node != node.uid:
                    continue
                document = link.data.get("fsm_transition")
                if not isinstance(document, dict):
                    continue
                document = copy.deepcopy(document)
                document["target_state"] = replacement.name
                transition = AnimTransition.from_dict(document)
                link.data = self._transition_graph_properties(transition)
        return self._commit_node_graph_change(
            description,
            before,
            merge_key=merge_key,
        )

    def _update_state_fields(
        self,
        state: AnimState,
        description: str,
        *,
        merge_key: str,
        **changes,
    ) -> bool:
        after = self._graph_state_document(state.stable_id)
        if after is None:
            return False
        for key, value in changes.items():
            if key not in after:
                raise KeyError(f"unknown animation state field: {key}")
            after[key] = copy.deepcopy(value)
        return self._update_state_document(
            state,
            after,
            description,
            merge_key=merge_key,
        )

    def _insert_state(
        self,
        state: AnimState,
        description: str,
        *,
        make_default: bool,
    ) -> bool:
        if self._fsm is None or self._graph.find_node(state.stable_id) is not None:
            return False
        state_document = state.to_dict()
        state_document["transitions"] = []
        state = AnimState.from_dict(state_document)
        if self._graph_state_uid_by_name(state.name):
            raise ValueError(f"animation state name already exists: {state.name}")
        before = self._graph.capture_authoring_state()
        self._graph.add_node(
            FSM_STATE_NODE_TYPE_ID,
            canvas_x=float(state.position[0]),
            canvas_y=float(state.position[1]),
            uid=state.stable_id,
            **self._state_graph_properties(state),
        )
        if make_default:
            self._set_default_link_in_graph(state.stable_id)
        ref = GraphElementRef(GraphElementKind.NODE, state.stable_id)
        return self._commit_node_graph_change(
            description,
            before,
            selection_after=(ref,),
        )

    def _set_default_state(self, state_name: str, description: str) -> bool:
        if self._fsm is None or self._graph_default_state_name() == state_name:
            return False
        state_uid = self._graph_state_uid_by_name(state_name) if state_name else ""
        if state_name and not state_uid:
            return False
        before = self._graph.capture_authoring_state()
        self._set_default_link_in_graph(state_uid)
        return self._commit_node_graph_change(
            description,
            before,
        )

    def _insert_transition(
        self,
        owner: AnimState,
        transition: AnimTransition,
        description: str,
    ) -> bool:
        if self._fsm is None:
            return False
        owner_node = self._graph.find_node(owner.stable_id)
        target_uid = self._graph_state_uid_by_name(transition.target_state)
        if owner_node is None or not target_uid:
            return False
        if self._graph.find_link(transition.stable_id) is not None:
            return False
        before = self._graph.capture_authoring_state()
        link = self._graph.add_link(
            owner.stable_id,
            "out",
            target_uid,
            "in",
            uid=transition.stable_id,
            **self._transition_graph_properties(transition),
        )
        if link is None:
            return False
        return self._commit_node_graph_change(
            description,
            before,
        )

    def _remove_transition(self, stable_id: str, description: str) -> bool:
        if self._fsm is None:
            return False
        if self._graph.find_link(stable_id) is None:
            return False
        before = self._graph.capture_authoring_state()
        self._graph.remove_link(stable_id)
        selected = self._graph_selection.primary_id(GraphElementKind.LINK)
        return self._commit_node_graph_change(
            description,
            before,
            selection_after=() if selected == stable_id else None,
        )

    def _update_transition_document(
        self,
        stable_id: str,
        after_transition: dict,
        description: str,
        *,
        merge_key: str,
    ) -> bool:
        if self._fsm is None:
            return False
        link = self._graph.find_link(stable_id)
        before_transition = self._graph_transition_document(stable_id)
        if link is None or before_transition is None:
            return False
        if before_transition == after_transition:
            return False
        replacement = AnimTransition.from_dict(copy.deepcopy(after_transition))
        if replacement.stable_id != stable_id:
            raise RuntimeError("animation transition update changed stable identity")
        target_uid = self._graph_state_uid_by_name(replacement.target_state)
        if not target_uid:
            raise ValueError("animation transition target no longer exists")
        before = self._graph.capture_authoring_state()
        replaced = self._graph.replace_link(
            stable_id,
            link.source_node,
            link.source_pin,
            target_uid,
            link.target_pin,
        )
        if replaced is None:
            return False
        replaced.data = self._transition_graph_properties(replacement)
        return self._commit_node_graph_change(
            description,
            before,
            merge_key=merge_key,
        )

    def _update_transition_fields(
        self,
        stable_id: str,
        description: str,
        *,
        merge_key: str,
        **changes,
    ) -> bool:
        after = self._graph_transition_document(stable_id)
        if after is None:
            return False
        for key, value in changes.items():
            if key not in after:
                raise KeyError(f"unknown animation transition field: {key}")
            after[key] = copy.deepcopy(value)
        return self._update_transition_document(
            stable_id,
            after,
            description,
            merge_key=merge_key,
        )

    def _remove_states(self, stable_ids: Tuple[str, ...]) -> bool:
        if self._fsm is None:
            return False
        target_ids = {
            stable_id
            for stable_id in stable_ids
            if stable_id != self._entry_uid
            and self._graph.find_node(stable_id) is not None
        }
        if not target_ids:
            return False
        before = self._graph.capture_authoring_state()
        default_link = self._graph.find_link(_FSM_ENTRY_LINK_ID)
        default_removed = bool(default_link and default_link.target_node in target_ids)
        for stable_id in target_ids:
            self._graph.remove_node(stable_id)
        if default_removed:
            replacement = next(
                (
                    node.uid
                    for node in self._graph.nodes
                    if node.type_id == FSM_STATE_NODE_TYPE_ID
                ),
                "",
            )
            self._set_default_link_in_graph(replacement)
        selected = set(self._graph_selection.selected_ids(GraphElementKind.NODE))
        return self._commit_node_graph_change(
            "Delete state" if len(target_ids) == 1 else "Delete states",
            before,
            selection_after=() if selected & target_ids else None,
        )

    def apply_diff(self, diff: GraphActionDiff) -> None:
        """Apply one precise FSM authoring diff through stable identities."""
        if diff.document_id != self.document_id:
            raise RuntimeError("graph diff targets a different animation FSM document")
        fsm = self._fsm
        if fsm is None:
            raise RuntimeError("animation FSM document has no live model")
        graph_changed = False

        def flush_graph() -> None:
            nonlocal graph_changed
            if graph_changed:
                self._sync_fsm_from_graph()
                graph_changed = False

        for mutation in diff.mutations:
            if mutation.element.kind is GraphElementKind.PARAMETER:
                flush_graph()
                stable_id = mutation.element.stable_id
                index = self._parameter_index_by_id(stable_id)
                if mutation.kind is GraphMutationKind.INSERT:
                    if index >= 0 or not isinstance(mutation.after, dict):
                        raise RuntimeError(f"cannot insert animation parameter {stable_id}")
                    target_index = max(
                        0, min(int(mutation.after_index), len(fsm.parameters))
                    )
                    fsm.parameters.insert(
                        target_index, AnimParameter.from_dict(mutation.after)
                    )
                elif mutation.kind is GraphMutationKind.REMOVE:
                    if index < 0:
                        raise RuntimeError(f"animation parameter no longer exists: {stable_id}")
                    fsm.parameters.pop(index)
                elif mutation.kind is GraphMutationKind.UPDATE:
                    if index < 0 or not isinstance(mutation.after, dict):
                        raise RuntimeError(f"cannot update animation parameter {stable_id}")
                    replacement = AnimParameter.from_dict(mutation.after)
                    if replacement.stable_id != stable_id:
                        raise RuntimeError("animation parameter update changed stable identity")
                    fsm.parameters[index] = replacement
                elif mutation.kind is GraphMutationKind.MOVE:
                    if index < 0:
                        raise RuntimeError(
                            f"animation parameter no longer exists: {stable_id}"
                        )
                    target_index = int(mutation.after_index)
                    if target_index < 0 or target_index >= len(fsm.parameters):
                        raise RuntimeError(
                            f"animation parameter move target is invalid: {target_index}"
                        )
                    parameter = fsm.parameters.pop(index)
                    fsm.parameters.insert(target_index, parameter)
                else:
                    raise RuntimeError(
                        f"unsupported animation parameter mutation: {mutation.kind.value}"
                    )
                continue
            if mutation.element.kind is GraphElementKind.GRAPH:
                flush_graph()
                if (
                    mutation.element.stable_id != _FSM_GRAPH_ID
                    or mutation.kind is not GraphMutationKind.UPDATE
                    or not isinstance(mutation.after, dict)
                ):
                    raise RuntimeError("invalid animation FSM document mutation")
                name = mutation.after.get("name")
                mode = mutation.after.get("mode")
                if type(name) is not str or not name:
                    raise RuntimeError("animation FSM name must not be empty")
                if mode not in {"2d", "3d", "timeline"}:
                    raise RuntimeError("animation FSM mode is invalid")
                fsm.name = name
                fsm.mode = mode
                continue
            if mutation.element.kind in {
                GraphElementKind.NODE,
                GraphElementKind.LINK,
            }:
                self._graph.apply_authoring_mutation(
                    NodeGraphMutation(
                        NodeGraphMutationKind(mutation.kind.value),
                        NodeGraphElementKind(mutation.element.kind.value),
                        mutation.element.stable_id,
                        before=mutation.before,
                        after=mutation.after,
                        before_index=mutation.before_index,
                        after_index=mutation.after_index,
                    )
                )
                graph_changed = True
                continue
            raise RuntimeError(
                f"unsupported animation FSM graph mutation: "
                f"{mutation.element.kind.value}/{mutation.kind.value}"
            )
        flush_graph()

    def on_graph_diff_applied(self, _diff: GraphActionDiff) -> None:
        self._graph_selection.refresh()

    def open_document_resource_immediate(self, file_path: str):
        """Load an .animfsm file into the editor."""
        normalized_path = self._normalize_fsm_path(file_path)
        target_path = normalized_path or file_path
        fsm = AnimStateMachine.load(target_path)
        if fsm is None:
            Debug.log_warning(f"Failed to load animfsm: {target_path}")
            return
        if self.document_id:
            self.unbind_document()
        self._fsm = fsm
        self._file_path = target_path
        for state in fsm.states:
            if not state.clip_guid and state.clip_path:
                state.clip_guid = self._resolve_guid(state.clip_path)
            if state.clip_guid:
                state.clip_path = ""
        self._sync_graph_from_fsm()
        self._replace_fsm_document(resource_path=target_path, dirty=False)
        self._graph_selection.clear(record_history=False)

    def can_new_fsm(self) -> bool:
        from Infernux.engine.interaction import (
            DocumentRegistry,
            EditorInteractionCore,
        )

        document = DocumentRegistry.instance().get(self.document_id)
        if (
            self._fsm is None
            or document is None
            or self.window_id not in document.view_ids
            or self._save_as_dialog.is_open
            or bool(self._pending_save_ticket_id)
        ):
            return False
        core = EditorInteractionCore.instance()
        return core is None or not core.close_coordinator.is_active

    def command_new_fsm(self) -> bool:
        if not self.can_new_fsm():
            return False
        return self.request_document_replacement(self._new_fsm_immediate)

    def _new_fsm_immediate(self, *, mode: str = "2d"):
        """Create a blank FSM for editing."""
        if mode not in {"2d", "3d", "timeline"}:
            raise ValueError(f"unsupported animation FSM mode: {mode!r}")
        if self.document_id:
            self.unbind_document()
        self._fsm = AnimStateMachine(name="New State Machine", mode=mode)
        self._file_path = ""
        self._sync_graph_from_fsm()
        self._replace_fsm_document(resource_path="", dirty=True)
        self._graph_selection.clear(record_history=False)

    def can_switch_mode(self, mode: str) -> bool:
        return (
            mode in {"2d", "3d", "timeline"}
            and self._fsm is not None
            and self._fsm.mode != mode
            and self.can_new_fsm()
        )

    def command_switch_mode(self, mode: str) -> bool:
        if not self.can_switch_mode(mode):
            return False
        return self._switch_to_new_mode_resource(mode)

    def _switch_to_new_mode_resource(self, mode: str) -> bool:
        """Leave the current asset and start a new resource in *mode*."""
        accepted = self.request_document_replacement(
            lambda target_mode=mode: self._commit_mode_switch(target_mode),
        )
        if not accepted:
            Debug.log_warning(
                "[AnimFSM] Mode switch is waiting for another Editor modal."
            )
        return bool(accepted)

    def _commit_mode_switch(self, mode: str) -> None:
        self._new_fsm_immediate(mode=mode)

    # ── Lifecycle ──────────────────────────────────────────────────────

    def on_enable(self) -> None:
        super().on_enable()
        try:
            from Infernux.engine.play_mode import PlayModeManager
            pmm = PlayModeManager.instance()
            if pmm:
                pmm.add_state_change_listener(self._on_play_mode_changed)
        except Exception:
            pass

    def on_disable(self) -> None:
        super().on_disable()
        try:
            from Infernux.engine.play_mode import PlayModeManager
            pmm = PlayModeManager.instance()
            if pmm:
                pmm.remove_state_change_listener(self._on_play_mode_changed)
        except Exception:
            pass

    # ── State persistence ──────────────────────────────────────────────

    def save_state(self) -> dict:
        return super().save_state()

    def load_state(self, data: dict) -> None:
        super().load_state(data)

    def _initial_size(self):
        return (900, 600)

    def _empty_state_hint(self) -> str:
        return t("animfsm_editor.open_hint")

    def _empty_state_drop_types(self):
        return ["ANIMFSM_FILE", "TIMELINEFSM_FILE"]

    def _on_empty_state_drop(self, payload_type, payload):
        if payload_type in ("ANIMFSM_FILE", "TIMELINEFSM_FILE") and payload:
            from Infernux.engine.interaction import DocumentKind

            self.request_document_resource_open(DocumentKind.ANIMATION_FSM, payload)

    # ═══════════════════════════════════════════════════════════════════
    # Rendering
    # ═══════════════════════════════════════════════════════════════════

    def _before_node_graph_render(self, ctx: InxGUIContext) -> None:
        del ctx

    def _render_node_graph_toolbar(self, ctx: InxGUIContext) -> None:
        self._render_toolbar(ctx)

    def _render_node_graph_left_panel(self, ctx: InxGUIContext) -> None:
        self._render_variables_panel(ctx)

    def _node_graph_detail_contributors(
        self,
    ) -> tuple[GraphDetailContributor, ...]:
        return (
            GraphDetailContributor(
                "animfsm.transition",
                300,
                lambda: bool(
                    self._graph_selection.primary_id(GraphElementKind.LINK)
                ),
                lambda ctx: self._render_selected_transition_detail(ctx),
            ),
            GraphDetailContributor(
                "parameter",
                200,
                self._is_node_graph_parameter_detail_active,
                lambda ctx: self._render_node_graph_parameter_detail(ctx),
            ),
            GraphDetailContributor(
                "animfsm.state",
                0,
                lambda: True,
                self._render_fsm_state_detail,
            ),
        )

    def _after_node_graph_render(self, ctx: InxGUIContext) -> None:
        # Accept .animfsm / .timelinefsm file drops
        payload = ctx.accept_drag_drop_payload("ANIMFSM_FILE")
        if payload:
            from Infernux.engine.interaction import DocumentKind

            self.request_document_resource_open(DocumentKind.ANIMATION_FSM, payload)
        payload_tl = ctx.accept_drag_drop_payload("TIMELINEFSM_FILE")
        if payload_tl:
            from Infernux.engine.interaction import DocumentKind

            self.request_document_resource_open(DocumentKind.ANIMATION_FSM, payload_tl)


    # ── Toolbar ───────────────────────────────────────────────────────

    def _clip_ext_flags(self):
        """Return (has_2d_clip, has_3d_clip) from resolved state clip paths."""
        has_2d = False
        has_3d = False
        if not self._fsm:
            return has_2d, has_3d
        for state in self._fsm.states:
            path = self._resolved_clip_path_for_state(state)
            if not path:
                continue
            ext = os.path.splitext(path)[1].lower()
            if ext == ".animclip2d":
                has_2d = True
            elif ext == ".animclip3d":
                has_3d = True
        return has_2d, has_3d

    @staticmethod
    def _resolved_clip_path_for_state(state: AnimState) -> str:
        path = (state.clip_path or "").strip()
        if not path and state.clip_guid:
            try:
                from Infernux.core.assets import AssetManager

                adb = getattr(AssetManager, "_asset_database", None)
                if adb:
                    path = adb.get_path_from_guid(state.clip_guid) or ""
            except Exception:
                pass
        return path.replace("\\", "/") if path else ""

    def _render_toolbar(self, ctx: InxGUIContext):
        fsm = self._fsm
        if fsm is None:
            return

        new_label = t("animfsm_editor.new")
        new_pressed = ctx.button(new_label)
        ctx.record_semantic_item(
            "button", new_label, self.can_new_fsm(), "animfsm.toolbar.new"
        )
        if new_pressed:
            from Infernux.engine.interaction import CommandSource

            self._execute_node_graph_command(
                "animfsm.new",
                source=CommandSource.TOOLBAR,
            )
            return

        ctx.same_line(0, 8)
        save_label = t("animfsm_editor.save") if self._file_path else t("animfsm_editor.save_as")
        save_pressed = ctx.button(save_label)
        ctx.record_semantic_item("button", save_label, True, "animfsm.toolbar.save")
        if save_pressed:
            from Infernux.engine.interaction import CommandSource

            self._execute_node_graph_command("file.save", source=CommandSource.TOOLBAR)

        ctx.same_line(0, 16)
        ctx.label(f"{t('animfsm_editor.name')}:")
        ctx.same_line(0, 8)
        ctx.set_next_item_width(160)
        new_name = ctx.text_input("##fsm_name", fsm.name, 128)
        ctx.record_semantic_item(
            "text_input", t("animfsm_editor.name"), True, "animfsm.toolbar.name",
            None, None, new_name,
        )
        if new_name != fsm.name:
            candidate = new_name.strip()
            if candidate:
                self._update_graph_document(
                    name=candidate,
                    description="Rename FSM",
                    merge_key="animfsm:graph:name",
                )

        ctx.same_line(0, 16)
        ctx.label(f"{t('animfsm_editor.mode')}:")
        ctx.same_line(0, 8)
        # Modes represent different asset domains. Switching starts a new
        # resource after resolving unsaved changes instead of mutating an
        # incompatible graph in place.
        ctx.set_next_item_width(110)
        _MODES = ["2d", "3d", "timeline"]
        _LABELS = ["2D", "3D", t("animfsm_editor.mode_timeline")]
        mode_idx = _MODES.index(fsm.mode) if fsm.mode in _MODES else 0
        new_mode_idx = ctx.combo("##fsm_mode", mode_idx, _LABELS, 3)
        ctx.record_semantic_item(
            "combo", t("animfsm_editor.mode"), True, "animfsm.toolbar.mode",
            None, None, _MODES[new_mode_idx],
        )
        ctx.record_semantic_item(
            "status", "Asset Path", True, "animfsm.document.path",
            None, None, self._file_path,
        )
        ctx.record_semantic_item(
            "status", "Dirty", True, "animfsm.document.dirty", self._document_is_dirty(),
        )
        if new_mode_idx != mode_idx:
            from Infernux.engine.interaction import CommandSource

            self._execute_node_graph_command(
                "animfsm.switch_mode",
                source=CommandSource.TOOLBAR,
                payload={"mode": _MODES[new_mode_idx]},
            )

        if self._file_path:
            ctx.same_line(0, 12)
            ctx.label(self._file_path)

    @staticmethod
    def _sanitize_param_identifier(raw: str) -> str:
        return _sanitize_animation_parameter_name(raw)

    def _default_compare_term(self, fsm: AnimStateMachine) -> dict:
        p0 = fsm.parameters[0]
        return AnimCondition(parameter_id=p0.stable_id).to_dict()

    def _apply_condition_model(self, lk: GraphLink, terms: List[dict]) -> None:
        conditions = [AnimCondition.from_dict(dict(term)).to_dict() for term in terms]
        if conditions == list(lk.data.get("conditions", ())):
            return
        self._update_transition_fields(
            lk.uid,
            "Edit transition condition",
            merge_key=f"transition:{lk.uid}:conditions",
            conditions=conditions,
        )

    def _render_transition_duration_row(
        self,
        ctx: InxGUIContext,
        lk: GraphLink,
        semantic_prefix: str = "",
    ) -> None:
        """Crossfade/blend duration (seconds) for this transition into its target."""
        dur = float(lk.data.get("duration", 0.0) or 0.0)
        ctx.push_style_color(ImGuiCol.Text, 0.55, 0.56, 0.58, 1.0)
        ctx.label(t("animfsm_editor.transition_duration"))
        ctx.pop_style_color(1)
        ctx.set_next_item_width(-1)
        new_dur = ctx.drag_float("##trdur", dur, 0.01, 0.0, 60.0)
        if semantic_prefix:
            ctx.record_semantic_item(
                "drag_float",
                f"{t('animfsm_editor.transition_duration')}: {new_dur:g}",
                True,
                f"{semantic_prefix}.duration",
            )
        if new_dur != dur:
            new_dur = max(0.0, float(new_dur))
            self._update_transition_fields(
                lk.uid,
                "Edit transition duration",
                merge_key=f"transition:{lk.uid}:duration",
                duration=new_dur,
            )
        ctx.dummy(0, 4)

    def _render_transition_condition_block(
        self,
        ctx: InxGUIContext,
        lk: GraphLink,
        semantic_prefix: str = "",
    ) -> None:
        fsm = self._fsm
        if fsm is None:
            return
        # Crossfade duration applies to every transition regardless of condition
        # mode, so render it first (before the condition-specific early returns).
        self._render_transition_duration_row(ctx, lk, semantic_prefix)
        raw_terms = lk.data.get("conditions", ())
        terms = (
            [copy.deepcopy(term) for term in raw_terms]
            if isinstance(raw_terms, (list, tuple))
            else []
        )

        has_p = len(fsm.parameters) > 0
        names = [p.name for p in fsm.parameters]
        parameter_ids = [p.stable_id for p in fsm.parameters]
        mode_clip = t("animfsm_editor.cond_mode_clip_end")
        mode_param = t("animfsm_editor.cond_mode_parameter")
        clip_mode = len(terms) == 0

        if not has_p:
            ctx.push_style_color(ImGuiCol.Text, 0.55, 0.56, 0.58, 1.0)
            ctx.label(t("animfsm_editor.cond_no_parameters_hint"))
            ctx.pop_style_color(1)
            ctx.dummy(0, 2)
            ctx.set_next_item_width(-1)
            ctx.combo("##tmode", 0, [mode_clip, mode_param], 2)
            if semantic_prefix:
                ctx.record_semantic_item(
                    "combo",
                    mode_clip,
                    False,
                    f"{semantic_prefix}.condition_mode",
                )
            return

        ctx.set_next_item_width(-1)
        mid_idx = 0 if clip_mode else 1
        new_mid = ctx.combo("##tmode", mid_idx, [mode_clip, mode_param], 2)
        if semantic_prefix:
            ctx.record_semantic_item(
                "combo",
                mode_clip if mid_idx == 0 else mode_param,
                True,
                f"{semantic_prefix}.condition_mode",
            )
        if new_mid != mid_idx:
            if new_mid == 0:
                self._apply_condition_model(lk, [])
            else:
                self._apply_condition_model(lk, [self._default_compare_term(fsm)])
            return

        if clip_mode:
            return

        if not terms:
            self._apply_condition_model(lk, [self._default_compare_term(fsm)])
            return

        # One row: [param] [op] [float] — multiple rows are implicitly AND (Unity-style).
        for i in range(len(terms)):
            ctx.push_id(i)
            tm = terms[i]
            parameter_id = str(tm.get("parameter_id", parameter_ids[0]))
            if parameter_id not in parameter_ids:
                parameter_id = parameter_ids[0]
            pi = parameter_ids.index(parameter_id)
            ctx.set_next_item_width(88)
            new_pi = ctx.combo("##pn", pi, names, len(names))
            if semantic_prefix:
                ctx.record_semantic_item(
                    "combo",
                    names[pi],
                    True,
                    f"{semantic_prefix}.condition.{i}.parameter",
                )
            ctx.same_line(0, 4)
            op = str(tm.get("operator", ">"))
            if op not in _OPS:
                op = ">"
            oi = _OPS.index(op)
            ctx.set_next_item_width(48)
            new_oi = ctx.combo("##op", oi, _OPS, len(_OPS))
            if semantic_prefix:
                ctx.record_semantic_item(
                    "combo",
                    op,
                    True,
                    f"{semantic_prefix}.condition.{i}.operator",
                )
            ctx.same_line(0, 4)
            fv = float(tm.get("threshold", 0.0))
            ctx.set_next_item_width(-1)
            new_fv = ctx.drag_float("##fv", fv, 0.05, -1e9, 1e9)
            if semantic_prefix:
                ctx.record_semantic_item(
                    "drag_float",
                    f"{new_fv:g}",
                    True,
                    f"{semantic_prefix}.condition.{i}.value",
                )
            ctx.pop_id()

            if new_pi != pi:
                terms[i]["parameter_id"] = parameter_ids[new_pi]
                self._apply_condition_model(lk, terms)
                return
            if new_oi != oi or new_fv != fv:
                terms[i]["operator"] = _OPS[new_oi]
                terms[i]["threshold"] = float(new_fv)
                self._apply_condition_model(lk, terms)
                return

        ctx.dummy(0, 4)
        ctx.begin_group()
        add_clicked = IGUI._mini_icon_button(ctx, "##addrow", Theme.ICON_IMG_PLUS, Theme.ICON_PLUS)
        if semantic_prefix:
            ctx.record_semantic_item(
                "button", "Add Condition", True, f"{semantic_prefix}.condition.add",
            )
        if add_clicked:
            nt = list(terms)
            nt.append(dict(self._default_compare_term(fsm)))
            self._apply_condition_model(lk, nt)
        ctx.same_line(0, 6)
        can_remove = len(terms) > 1
        remove_clicked = can_remove and IGUI._mini_icon_button(
            ctx, "##rmrow", Theme.ICON_IMG_MINUS, Theme.ICON_MINUS
        )
        if semantic_prefix:
            ctx.record_semantic_item(
                "button",
                "Remove Condition",
                can_remove,
                f"{semantic_prefix}.condition.remove",
            )
        if remove_clicked:
            nt = list(terms)
            nt.pop()
            self._apply_condition_model(lk, nt)
        ctx.end_group()

    def _render_variables_panel(self, ctx: InxGUIContext):
        """Left overlay: parameter Blackboard using shared graph chrome."""
        fsm = self._fsm
        if fsm is None:
            return

        entries = tuple(
            GraphWorkspaceEntry(
                GraphElementRef(GraphElementKind.PARAMETER, parameter.stable_id),
                parameter.name,
                parameter.value_type.value_type.value,
                _FSM_PARAM_COLORS.get(
                    {
                        ValueType.BOOL: "bool",
                        ValueType.I32: "int",
                        ValueType.F32: "float",
                    }[parameter.value_type.value_type],
                    _FSM_PARAM_COLORS["float"],
                ),
                semantic_kind="animfsm_parameter",
                semantic_id=f"animfsm.parameter.{index}",
                semantic_string_value=parameter.value_type.value_type.value,
                can_rename=True,
                can_delete=True,
            )
            for index, parameter in enumerate(fsm.parameters)
        )
        self._render_graph_workspace_section(
            ctx,
            GraphWorkspaceSection(
                t("animfsm_editor.section_parameters"),
                "animfsm_parameter",
                entries,
                add_actions=(GraphWorkspaceAddAction("default", "Parameter"),),
                add_semantic_id="animfsm.parameters.add",
                rename_label=t("particle_graph_editor.rename_parameter"),
                delete_label=t("particle_graph_editor.remove_parameter"),
            ),
        )

    def _node_graph_workspace_add(
        self, section_id: str, action_id: str
    ) -> bool:
        if section_id == "animfsm_parameter" and action_id == "default":
            self._insert_parameter()
            return True
        return super()._node_graph_workspace_add(section_id, action_id)

    def _node_graph_workspace_rename(
        self, element: GraphElementRef, name: str
    ) -> bool:
        if element.kind is not GraphElementKind.PARAMETER:
            return super()._node_graph_workspace_rename(element, name)
        parameter = self._parameter_by_id(element.stable_id)
        sanitized = self._sanitize_param_identifier(name)
        if parameter is None or not sanitized:
            return False
        after = parameter.to_dict()
        after["name"] = sanitized
        return self._update_parameter_document(
            parameter,
            after,
            "Rename parameter",
            merge_key=f"parameter:{element.stable_id}:name",
        )

    def _node_graph_workspace_delete(self, element: GraphElementRef) -> bool:
        if element.kind is GraphElementKind.PARAMETER:
            self._remove_parameter(element.stable_id)
            return True
        return super()._node_graph_workspace_delete(element)

    def _node_graph_parameter_policy(self):
        return _ANIMATION_PARAMETER_POLICY

    def _node_graph_parameter_collection(self):
        return (
            GraphParameterCollection(self._fsm.parameters)
            if self._fsm is not None
            else None
        )

    def _node_graph_parameter_detail_config(self) -> GraphParameterDetailConfig:
        return GraphParameterDetailConfig(
            title=t("animfsm_editor.section_parameters"),
            name_label=t("animfsm_editor.state_name"),
            type_label=t("particle_graph_editor.parameter_type"),
            default_label=t("particle_graph_editor.parameter_default"),
            semantic_prefix="animfsm.parameter",
            show_exposed=False,
            show_writable=False,
            show_tooltip=False,
        )

    def _node_graph_commit_parameter_changes(
        self,
        stable_id: str,
        changes: dict,
    ) -> bool:
        parameter = self._parameter_by_id(stable_id)
        if parameter is None or self._fsm is None:
            return False
        edit = _ANIMATION_PARAMETER_POLICY.update(
            GraphParameterCollection(self._fsm.parameters),
            stable_id,
            changes,
        )
        if not isinstance(edit.after, AnimParameter):
            raise RuntimeError("animation parameter policy returned an invalid value")
        return self._update_parameter_document(
            parameter,
            edit.after.to_dict(),
            "Edit animation parameter",
            merge_key=f"parameter:{stable_id}:{','.join(sorted(changes))}",
        )

    # ── Detail panel (right side) ─────────────────────────────────────

    def _fsm_clip_asset_type(self) -> str:
        fsm = self._fsm
        mode = getattr(fsm, "mode", "2d") if fsm is not None else "2d"
        return "AnimationClip3D" if mode == "3d" else "AnimationClip"

    def _is_timeline_mode(self) -> bool:
        fsm = self._fsm
        return getattr(fsm, "mode", "2d") == "timeline" if fsm is not None else False

    @staticmethod
    def _embedded_clip3d_picker_items(filter_text: str) -> List[Tuple[str, str]]:
        """List model-embedded takes alongside standalone ``.animclip3d`` assets.

        The Project panel exposes an embedded take as ``<model-guid>::subanim:<n>``.
        Returning that same public virtual reference keeps object-picker assignment,
        drag-and-drop assignment, and runtime loading on one contract.
        """
        from Infernux.core.asset_types import read_meta_file, read_meta_guid
        from Infernux.engine.interaction import asset_reference_catalog

        filt = (filter_text or "").strip().lower()
        items: List[Tuple[str, str]] = []
        seen: set[str] = set()
        for _label, model_path in asset_reference_catalog.items("Mesh", ""):
            normalized = path_key(model_path)
            if normalized in seen:
                continue
            seen.add(normalized)
            meta = read_meta_file(model_path) or {}
            names_csv = meta.get("animation_names_csv") or ""
            if not isinstance(names_csv, str):
                continue
            take_names = [name.strip() for name in names_csv.split(",") if name.strip()]
            if not take_names:
                continue
            model_name = os.path.splitext(os.path.basename(model_path))[0]
            base = read_meta_guid(model_path) or model_path
            for index, take_name in enumerate(take_names):
                display = f"{model_name} | {take_name}"
                if filt and filt not in display.lower():
                    continue
                virtual_path = f"{base}::subanim:{index}"
                items.append((display, {
                    "asset_type": "AnimationClip3D",
                    "guid": "",
                    "path_hint": virtual_path,
                    "builtin": "",
                }))
        return items

    def _clip_ref_for_state(self, state: AnimState):
        """Build a clip ref (2D/3D) with path hint resolved for Inspector-style labels."""
        path = (state.clip_path or "").strip()
        if not path and state.clip_guid:
            try:
                from Infernux.core.assets import AssetManager

                adb = getattr(AssetManager, "_asset_database", None)
                if adb:
                    path = adb.get_path_from_guid(state.clip_guid) or ""
            except Exception:
                pass
        if self._fsm_clip_asset_type() == "AnimationClip3D":
            return AnimationClip3DRef(guid=state.clip_guid or "", path_hint=path)
        return AnimationClipRef(guid=state.clip_guid or "", path_hint=path)

    def _detail_checkbox_row_right(
        self,
        ctx: InxGUIContext,
        lw: float,
        label_key: str,
        wid: str,
        value: bool,
        semantic_id: str,
    ) -> bool:
        """Label left, checkbox square aligned to the right (inspector-style row)."""
        label = t(label_key)
        field_label(ctx, label, lw)
        ctx.same_line(0, 8)
        dx = ctx.get_content_region_avail_width() - Theme.INSPECTOR_CHECKBOX_SLOT_W
        if dx > 0:
            ctx.set_cursor_pos_x(ctx.get_cursor_pos_x() + dx)
        new_value = ctx.checkbox(wid, value)
        ctx.record_semantic_item(
            "checkbox", label, True, semantic_id, bool(new_value),
        )
        return new_value

    def _clip_b_ref_for_state(self, state: AnimState):
        """Build a clip ref for the blend node's second clip (B)."""
        path = (getattr(state, "clip_b_path", "") or "").strip()
        guid = getattr(state, "clip_b_guid", "") or ""
        if not path and guid:
            try:
                from Infernux.core.assets import AssetManager
                adb = getattr(AssetManager, "_asset_database", None)
                if adb:
                    path = adb.get_path_from_guid(guid) or ""
            except Exception:
                pass
        if self._fsm_clip_asset_type() == "AnimationClip3D":
            return AnimationClip3DRef(guid=guid, path_hint=path)
        return AnimationClipRef(guid=guid, path_hint=path)

    def _clip_b_display_name(self, state: AnimState, ref=None) -> str:
        guid = str(getattr(state, "clip_b_guid", "") or "")
        path = str(getattr(state, "clip_b_path", "") or "")
        cache = getattr(self, "_clip_name_cache", None)
        if cache is None:
            cache = {}
            self._clip_name_cache = cache
        ckey = ("B", guid, path)
        if ckey in cache:
            return cache[ckey]
        resolved = path
        if (not resolved or "::subanim:" not in resolved) and guid:
            try:
                from Infernux.core.assets import AssetManager
                adb = getattr(AssetManager, "_asset_database", None)
                if adb:
                    resolved = adb.get_path_from_guid(guid) or resolved
            except Exception:
                pass
        name = self._clip_name_from(
            guid, path, resolved, lambda: (ref or self._clip_b_ref_for_state(state)))
        cache[ckey] = name
        return name

    def _assign_clip_b_to_state(self, state: AnimState, clip_path, node=None, *, record_undo: bool = True):
        try:
            p = resolve_asset_reference_path(self._fsm_clip_asset_type(), clip_path)
        except (KeyError, TypeError, ValueError) as exc:
            Debug.log_error(f"Animation state clip B assignment rejected: {exc}")
            return
        guid = self._resolve_guid(p) if p else ""
        path = "" if guid else (p or "")
        if record_undo:
            self._update_state_fields(
                state,
                "Assign blend clip B",
                merge_key=f"state:{state.stable_id}:clip_b",
                clip_b_guid=guid,
                clip_b_path=path,
            )
        else:
            state.clip_b_guid = guid
            state.clip_b_path = path
        self._clip_name_cache = {}

    def _clear_clip_b_from_state(self, state: AnimState, node=None, *, record_undo: bool = True):
        if record_undo:
            self._update_state_fields(
                state,
                "Clear blend clip B",
                merge_key=f"state:{state.stable_id}:clip_b",
                clip_b_guid="",
                clip_b_path="",
            )
        else:
            state.clip_b_guid = ""
            state.clip_b_path = ""
        self._clip_name_cache = {}

    def _render_clip_b_reference_row(self, ctx: InxGUIContext, state: AnimState, node, lw: float) -> None:
        descriptor = asset_type_registry.require(self._fsm_clip_asset_type())
        type_hint = descriptor.display_name
        drag_type = descriptor.drag_types
        prefix = descriptor.widget_prefix
        ref = self._clip_b_ref_for_state(state)
        display = self._clip_b_display_name(state, ref)

        field_label(ctx, t("animfsm_editor.clip_b"), lw)
        clip_b_ping = str(getattr(ref, "path_hint", "") or getattr(state, "clip_b_path", "") or "").strip()
        if not clip_b_ping:
            try:
                from Infernux.core.assets import AssetManager
                adb = getattr(AssetManager, "_asset_database", None)
                guid_b = str(getattr(state, "clip_b_guid", "") or "")
                if adb and guid_b:
                    clip_b_ping = adb.get_path_from_guid(guid_b) or ""
            except Exception:
                pass
        render_asset_reference_field(
            ctx,
            f"{prefix}_fsm_clipb_{node.uid}",
            display,
            type_hint,
            accept_drag_type=drag_type,
            asset_type=descriptor.type_id,
            on_assign=lambda p, _st=state, _nd=node: self._assign_clip_b_to_state(_st, p, _nd),
            additional_asset_items=(
                self._embedded_clip3d_picker_items
                if self._fsm_clip_asset_type() == "AnimationClip3D"
                else None
            ),
            on_clear=lambda _st=state, _nd=node: self._clear_clip_b_from_state(_st, _nd),
            ping_path=clip_b_ping or None,
            has_value=bool(state.clip_b_guid or state.clip_b_path),
            reference_value=AssetReferenceCodec.normalize(descriptor.type_id, ref),
            semantic_id="animfsm.state.clip_b",
        )

    def _clip_display_name(self, state: AnimState, ref=None) -> str:
        """Human-readable clip name (take/file name) instead of a raw GUID."""
        guid = str(getattr(state, "clip_guid", "") or "")
        path = str(getattr(state, "clip_path", "") or "")
        cache = getattr(self, "_clip_name_cache", None)
        if cache is None:
            cache = {}
            self._clip_name_cache = cache
        ckey = (guid, path)
        if ckey in cache:
            return cache[ckey]

        resolved = self._resolved_clip_path_for_state(state)
        name = self._clip_name_from(
            guid, path, resolved, lambda: (ref or self._clip_ref_for_state(state)))
        cache[ckey] = name
        return name

    @staticmethod
    def _clip_name_from(guid: str, path: str, resolved: str, ref_factory) -> str:
        """Resolve a human-readable clip name from guid/path/resolved-path."""
        # Embedded FBX take "<base>::subanim:<i>": resolve the take's display name
        # (basename of the virtual path is just the model GUID, which looks wrong).
        emb = path if "::subanim:" in path else (resolved or "")
        if "::subanim:" in emb:
            try:
                from Infernux.core.animation_clip3d import AnimationClip3D
                ec = AnimationClip3D.from_embedded_take_virtual_path(emb)
                if ec is not None and getattr(ec, "take_name", ""):
                    return str(ec.take_name)
            except Exception:
                pass
        try:
            obj = ref_factory().resolve()
            if obj is not None:
                nm = str(getattr(obj, "take_name", "") or getattr(obj, "name", "") or "")
                if nm:
                    return nm
        except Exception:
            pass
        p = resolved or path
        if p and "::subanim:" not in p:
            base = os.path.basename(p)
            dot = base.rfind(".")
            return base[:dot] if dot > 0 else base
        return f"GUID:{guid[:8]}\u2026" if guid else "None"

    def _render_clip_reference_row(
        self,
        ctx: InxGUIContext,
        state: AnimState,
        node,
        lw: float,
        label: str = "",
        semantic_id: str = "animfsm.state.clip",
    ) -> None:
        """Same object-field UX as the main Inspector (basename, picker, drag-drop, clear)."""
        descriptor = asset_type_registry.require(self._fsm_clip_asset_type())
        type_hint = descriptor.display_name
        drag_type = descriptor.drag_types
        prefix = descriptor.widget_prefix

        ref = self._clip_ref_for_state(state)
        display = self._clip_display_name(state, ref)

        def _on_clear(_st=state, _nd=node):
            self._clear_clip_from_state(_st, _nd)

        field_label(ctx, label or t("animfsm_editor.clip_ref"), lw)
        clip_ping = self._resolved_clip_path_for_state(state)
        render_asset_reference_field(
            ctx,
            f"{prefix}_fsm_clip_{node.uid}",
            display,
            type_hint,
            asset_type=descriptor.type_id,
            accept_drag_type=drag_type,
            on_assign=lambda p, _st=state, _nd=node: self._assign_clip_to_state(
                _st, p, _nd,
            ),
            additional_asset_items=(
                self._embedded_clip3d_picker_items
                if self._fsm_clip_asset_type() == "AnimationClip3D"
                else None
            ),
            on_clear=_on_clear,
            ping_path=clip_ping or None,
            has_value=bool(state.clip_guid or state.clip_path),
            reference_value=AssetReferenceCodec.normalize(descriptor.type_id, ref),
            semantic_id=semantic_id,
        )

    def _render_fsm_state_detail(self, ctx: InxGUIContext):
        fsm = self._fsm
        if fsm is None:
            return

        node = self._graph.find_node(
            self._graph_selection.primary_id(GraphElementKind.NODE)
        )
        if node is None or node.type_id != FSM_STATE_NODE_TYPE_ID:
            ctx.push_style_color(ImGuiCol.Text, 0.50, 0.51, 0.53, 1.0)
            ctx.label(t("animfsm_editor.no_state_selected"))
            ctx.pop_style_color(1)
            return

        state_name = self._uid_to_name.get(node.uid, "")
        state = fsm.get_state(state_name)
        if state is None:
            return

        labels = [
            t("animfsm_editor.state_name"),
            t("animfsm_editor.clip_ref"),
            t("animfsm_editor.speed"),
            t("animfsm_editor.exit_time"),
            t("animfsm_editor.loop"),
            t("animfsm_editor.restart_same_clip"),
        ]
        lw = max_label_w(ctx, labels)

        is_default = (state.name == fsm.default_state)
        row_w = ctx.get_content_region_avail_width()
        base_x = ctx.get_cursor_pos_x()
        ctx.push_style_color(ImGuiCol.Text, 0.55, 0.56, 0.58, 1.0)
        ctx.label(t("animfsm_editor.section_state"))
        ctx.pop_style_color(1)
        ctx.same_line(0, 0)
        if is_default:
            badge = t("animfsm_editor.default_badge")
            tw = ctx.calc_text_width(badge)
            ctx.set_cursor_pos_x(base_x + row_w - tw)
            ctx.push_style_color(ImGuiCol.Text, 0.48, 0.65, 0.45, 1.0)
            ctx.label(badge)
            ctx.record_semantic_item(
                "status", "Default: true", False, "animfsm.state.default",
            )
            ctx.pop_style_color(1)
        else:
            set_lbl = t("animfsm_editor.set_default")
            btn_w = ctx.calc_text_width(set_lbl) + 24.0
            ctx.set_cursor_pos_x(base_x + row_w - btn_w)
            set_default_clicked = ctx.button(set_lbl)
            ctx.record_semantic_item(
                "button", "Default: false; Set Default", True, "animfsm.state.set_default",
            )
            if set_default_clicked:
                self._set_default_state(state.name, "Set default state")
        ctx.separator()
        ctx.dummy(0, 4)

        field_label(ctx, t("animfsm_editor.state_name"), lw)
        ctx.same_line(0, 8)
        ctx.set_next_item_width(-1)
        new_name = ctx.text_input("##state_name_edit", state.name, 128)
        ctx.record_semantic_item(
            "text_input", f"{t('animfsm_editor.state_name')}: {new_name}", True,
            "animfsm.state.name",
        )
        if new_name != state.name:
            candidate = new_name.strip()
            if candidate and fsm.get_state(candidate) is None:
                self._update_state_fields(
                    state,
                    "Rename state",
                    merge_key=f"state:{state.stable_id}:name",
                    name=candidate,
                )

        ctx.dummy(0, Theme.INSPECTOR_SECTION_GAP)
        ctx.push_style_color(ImGuiCol.Text, 0.55, 0.56, 0.58, 1.0)
        ctx.label(t("animfsm_editor.section_reference"))
        ctx.pop_style_color(1)
        ctx.separator()
        ctx.dummy(0, 4)

        # The node kind (plain Clip / A↔B Blend / Timeline) is fixed at creation
        # time (via the drag-to-create menu or by dropping an asset) and is
        # intentionally not editable here.
        kind = getattr(state, "kind", "clip")
        ctx.record_semantic_item(
            "status", f"State Kind: {kind}", False, "animfsm.state.kind",
        )

        if kind == "blend":
            # Symmetric A/B naming for blend nodes.
            self._render_clip_reference_row(
                ctx, state, node, lw,
                label=t("animfsm_editor.clip_a"),
                semantic_id="animfsm.state.clip_a",
            )
            self._render_clip_b_reference_row(ctx, state, node, lw)
            field_label(ctx, t("animfsm_editor.blend_lerp"), lw)
            ctx.same_line(0, 8)
            ctx.set_next_item_width(-1)
            new_lerp = ctx.drag_float("##blend_lerp", float(state.blend_value), 0.005, 0.0, 1.0)
            if new_lerp != state.blend_value:
                self._update_state_fields(
                    state,
                    "Change blend lerp",
                    merge_key=f"state:{state.stable_id}:blend_value",
                    blend_value=max(0.0, min(1.0, float(new_lerp))),
                )
        elif kind == "timeline":
            self._render_timeline_reference_row(ctx, state, node, lw)
        else:
            self._render_clip_reference_row(ctx, state, node, lw)

        ctx.dummy(0, Theme.INSPECTOR_SECTION_GAP)
        ctx.push_style_color(ImGuiCol.Text, 0.55, 0.56, 0.58, 1.0)
        ctx.label(t("animfsm_editor.section_playback"))
        ctx.pop_style_color(1)
        ctx.separator()
        ctx.dummy(0, 4)

        field_label(ctx, t("animfsm_editor.speed"), lw)
        ctx.same_line(0, 8)
        ctx.set_next_item_width(-1)
        new_speed = ctx.drag_float("##speed", state.speed, 0.01, 0.0, 10.0)
        ctx.record_semantic_item(
            "drag_float", f"{t('animfsm_editor.speed')}: {new_speed:g}", True,
            "animfsm.state.speed",
        )
        if new_speed != state.speed:
            self._update_state_fields(
                state,
                "Change playback speed",
                merge_key=f"state:{state.stable_id}:speed",
                speed=float(new_speed),
            )

        exit_pct = state.exit_time_normalized * 100.0
        field_label(ctx, t("animfsm_editor.exit_time"), lw)
        ctx.same_line(0, 8)
        ctx.set_next_item_width(-1)
        new_exit_pct = ctx.drag_float("##exit_time", exit_pct, 0.5, 0.0, 100.0)
        ctx.record_semantic_item(
            "drag_float", f"{t('animfsm_editor.exit_time')}: {new_exit_pct:g}", True,
            "animfsm.state.exit_time",
        )
        if new_exit_pct != exit_pct:
            self._update_state_fields(
                state,
                "Change exit time",
                merge_key=f"state:{state.stable_id}:exit_time",
                exit_time_normalized=max(0.0, min(1.0, new_exit_pct / 100.0)),
            )

        new_loop = self._detail_checkbox_row_right(
            ctx, lw, "animfsm_editor.loop", "##state_loop", state.loop,
            "animfsm.state.loop",
        )
        if new_loop != state.loop:
            self._update_state_fields(
                state,
                "Toggle loop",
                merge_key=f"state:{state.stable_id}:loop",
                loop=bool(new_loop),
            )

        new_rs = self._detail_checkbox_row_right(
            ctx, lw, "animfsm_editor.restart_same_clip", "##state_restart_same", state.restart_same_clip,
            "animfsm.state.restart_same_clip",
        )
        if new_rs != state.restart_same_clip:
            self._update_state_fields(
                state,
                "Toggle restart same clip",
                merge_key=f"state:{state.stable_id}:restart_same_clip",
                restart_same_clip=bool(new_rs),
            )

        ctx.dummy(0, Theme.INSPECTOR_SECTION_GAP)
        ctx.push_style_color(ImGuiCol.Text, 0.55, 0.56, 0.58, 1.0)
        ctx.label(t("animfsm_editor.section_transitions"))
        ctx.pop_style_color(1)
        ctx.separator()
        ctx.dummy(0, 4)

        outgoing_links = [
            lk for lk in self._graph.links
            if lk.source_node == node.uid and lk.source_pin == "out"
               and lk.target_pin == "in"
        ]
        remove_link_uid = ""
        for i, lk in enumerate(outgoing_links):
            target_name = self._uid_to_name.get(lk.target_node, "?")
            ctx.push_id(i)
            ctx.begin_group()
            ctx.label(f"→ {target_name}")
            ctx.same_line()
            if IGUI._mini_icon_button(ctx, "##del", Theme.ICON_IMG_REMOVE, Theme.ICON_REMOVE):
                remove_link_uid = lk.uid

            self._render_transition_condition_block(ctx, lk)
            ctx.end_group()
            ctx.dummy(0, 6)
            ctx.pop_id()

        if remove_link_uid:
            self._remove_transition(remove_link_uid, "Remove transition")

    def _render_transition_exit_time_row(self, ctx: InxGUIContext, lk: GraphLink) -> None:
        source_name = self._uid_to_name.get(lk.source_node, "")
        state = self._fsm.get_state(source_name) if self._fsm and source_name else None
        if state is None:
            return
        exit_pct = max(0.0, min(100.0, float(state.exit_time_normalized) * 100.0))
        ctx.push_style_color(ImGuiCol.Text, 0.55, 0.56, 0.58, 1.0)
        ctx.label(t("animfsm_editor.exit_time"))
        ctx.pop_style_color(1)
        ctx.set_next_item_width(-1)
        new_exit_pct = ctx.drag_float("##transition_exit_time", exit_pct, 0.5, 0.0, 100.0)
        ctx.record_semantic_item(
            "drag_float",
            f"{t('animfsm_editor.exit_time')}: {new_exit_pct:g}",
            True,
            "animfsm.transition.exit_time",
        )
        if new_exit_pct != exit_pct:
            self._update_state_fields(
                state,
                "Change transition exit time",
                merge_key=f"state:{state.stable_id}:exit_time",
                exit_time_normalized=max(0.0, min(1.0, new_exit_pct / 100.0)),
            )
        ctx.dummy(0, 4)

    def _render_selected_transition_detail(self, ctx: InxGUIContext) -> bool:
        link_uid = self._graph_selection.primary_id(GraphElementKind.LINK)
        if not link_uid:
            return False
        lk = self._graph.find_link(link_uid)
        if lk is None:
            self._graph_selection.clear(record_history=False)
            return False

        source_name = self._uid_to_name.get(lk.source_node, "Entry")
        target_name = self._uid_to_name.get(lk.target_node, "?")
        route_label = f"{source_name} -> {target_name}"
        ctx.record_semantic_item(
            "panel", f"Transition: {route_label}", False, "animfsm.transition.detail",
        )
        ctx.push_style_color(ImGuiCol.Text, 0.55, 0.56, 0.58, 1.0)
        ctx.label(t("animfsm_editor.section_transitions"))
        ctx.pop_style_color(1)
        ctx.separator()
        ctx.dummy(0, 4)
        ctx.label(route_label)
        ctx.record_semantic_item(
            "status", route_label, False, "animfsm.transition.route",
        )
        ctx.dummy(0, Theme.INSPECTOR_SECTION_GAP)

        if lk.source_node == self._entry_uid:
            return True

        self._render_transition_exit_time_row(ctx, lk)
        self._render_transition_condition_block(ctx, lk, "animfsm.transition")
        ctx.dummy(0, Theme.INSPECTOR_SECTION_GAP)
        delete_label = t("animfsm_editor.delete_transition")
        delete_clicked = ctx.button(delete_label)
        ctx.record_semantic_item(
            "button", delete_label, True, "animfsm.transition.delete",
        )
        if delete_clicked:
            self._remove_transition(link_uid, "Remove transition")
        return True

    # ═══════════════════════════════════════════════════════════════════
    # FSM ↔ Graph synchronization
    # ═══════════════════════════════════════════════════════════════════

    def _state_graph_properties(self, state: AnimState) -> dict:
        return self._graph.state_properties(state)

    def _transition_graph_properties(self, transition: AnimTransition) -> dict:
        return self._graph.transition_properties(transition)

    def _graph_state_document(self, stable_id: str) -> Optional[dict]:
        node = self._graph.find_node(stable_id)
        if node is None or node.type_id != FSM_STATE_NODE_TYPE_ID:
            return None
        document = node.data.get("fsm_state")
        if not isinstance(document, dict):
            return None
        result = copy.deepcopy(document)
        result["stable_id"] = node.uid
        result["position"] = [float(node.pos_x), float(node.pos_y)]
        result["transitions"] = []
        return result

    def _graph_state_name(self, stable_id: str) -> str:
        document = self._graph_state_document(stable_id)
        return str(document.get("name", "")) if document is not None else ""

    def _graph_state_uid_by_name(self, name: str) -> str:
        wanted = str(name or "")
        if not wanted:
            return ""
        for node in self._graph.nodes:
            if (
                node.type_id == FSM_STATE_NODE_TYPE_ID
                and self._graph_state_name(node.uid) == wanted
            ):
                return node.uid
        return ""

    def _graph_transition_document(self, stable_id: str) -> Optional[dict]:
        link = self._graph.find_link(stable_id)
        if link is None or stable_id == _FSM_ENTRY_LINK_ID:
            return None
        document = link.data.get("fsm_transition")
        if not isinstance(document, dict):
            return None
        result = copy.deepcopy(document)
        result["stable_id"] = link.uid
        result["target_state"] = self._graph_state_name(link.target_node)
        return result

    def _graph_default_state_name(self) -> str:
        link = self._graph.find_link(_FSM_ENTRY_LINK_ID)
        return self._graph_state_name(link.target_node) if link is not None else ""

    def _set_default_link_in_graph(self, state_uid: str) -> None:
        existing = self._graph.find_link(_FSM_ENTRY_LINK_ID)
        if not state_uid:
            if existing is not None:
                self._graph.remove_link(_FSM_ENTRY_LINK_ID)
            return
        state_name = self._graph_state_name(state_uid)
        if not state_name:
            raise ValueError("animation FSM default state does not exist in the graph")
        if existing is None:
            existing = self._graph.add_link(
                self._entry_uid,
                "out",
                state_uid,
                "in",
                uid=_FSM_ENTRY_LINK_ID,
            )
        else:
            existing = self._graph.replace_link(
                _FSM_ENTRY_LINK_ID,
                self._entry_uid,
                "out",
                state_uid,
                "in",
            )
        if existing is None:
            raise RuntimeError("animation FSM default-state link was rejected")
        existing.data = {"default_state": state_name}

    def _sync_graph_from_fsm(self):
        """Load an FSM asset through its NodeGraph domain adapter."""
        self._view.reset_interaction_state()
        self._name_to_uid.clear()
        self._uid_to_name.clear()
        fsm = self._fsm
        if fsm is None:
            return
        self._graph.load_fsm(fsm)
        self._name_to_uid = {state.name: state.stable_id for state in fsm.states}
        self._uid_to_name = {state.stable_id: state.name for state in fsm.states}
        self._entry_uid = _FSM_ENTRY_NODE_ID

    def _sync_fsm_from_graph(self) -> None:
        """Compile current NodeGraph authoring state into the FSM asset."""
        fsm = self._fsm
        if fsm is None:
            return
        self._graph.apply_to_fsm(fsm)
        self._name_to_uid = {state.name: state.stable_id for state in fsm.states}
        self._uid_to_name = {state.stable_id: state.name for state in fsm.states}
        self._entry_uid = _FSM_ENTRY_NODE_ID

    def _unique_state_name(self, want: str) -> str:
        base = (want or "State").strip() or "State"
        if not self._graph_state_uid_by_name(base):
            return base
        n = sum(
            node.type_id == FSM_STATE_NODE_TYPE_ID for node in self._graph.nodes
        )
        while True:
            cand = f"{base} {n}"
            if not self._graph_state_uid_by_name(cand):
                return cand
            n += 1

    # ── Callbacks from NodeGraphView ──────────────────────────────────

    def _node_graph_drag_description(self, _stable_id: str) -> str:
        return "Move state node"

    def _on_node_header_color_changed(
        self,
        node_uid: str,
        old_color: Tuple[float, float, float, float],
        new_color: Tuple[float, float, float, float],
        commit_undo: bool,
    ) -> None:
        node = self._graph.find_node(node_uid)
        if node is None or node_uid == self._entry_uid:
            return
        document = self._graph_state_document(node_uid)
        if document is None:
            return
        new_value = [float(component) for component in new_color]
        old_value = [float(component) for component in old_color]
        node.data["header_color"] = new_color
        document["header_color"] = new_value
        node.data["fsm_state"] = document

        # During color picker drag we only preview/apply changes.
        # Record exactly one undo command when the popup closes.
        if not commit_undo:
            return
        if new_value == old_value:
            return
        # The picker previews directly in the live model. Restore its committed
        # value before executing the precise command so revision and history only
        # advance once when the edit session closes.
        document["header_color"] = old_value
        node.data["fsm_state"] = document
        node.data["header_color"] = tuple(old_value)
        self._update_state_fields(
            AnimState.from_dict(document),
            "Change state header color",
            merge_key=f"state:{node_uid}:header_color",
            header_color=new_value,
        )

    def _on_link_created(self, src_node: str, src_pin: str, dst_node: str, dst_pin: str):
        """User created a connection by dragging between pins."""
        # Entry node connections change the default state
        if src_node == self._entry_uid:
            target_name = self._graph_state_name(dst_node)
            if target_name:
                self._set_default_state(target_name, "Set default state")
            return

        src_name = self._graph_state_name(src_node)
        dst_name = self._graph_state_name(dst_node)
        if not src_name or not dst_name or not self._fsm:
            return

        # Check for duplicate transition
        for link in self._graph.links:
            if link.source_node == src_node and link.target_node == dst_node:
                return

        transition = AnimTransition(target_state=dst_name)
        owner_document = self._graph_state_document(src_node)
        if owner_document is not None:
            self._insert_transition(
                AnimState.from_dict(owner_document), transition, "Add transition"
            )

    def _on_link_deleted(self, link_uid: str):
        self._remove_transition(link_uid, "Remove transition")

    def _on_link_replaced(
        self,
        link_uid: str,
        src_node: str,
        _src_pin: str,
        dst_node: str,
        _dst_pin: str,
    ) -> None:
        if self._fsm is None:
            return
        target_name = self._graph_state_name(dst_node)
        if not target_name:
            return
        ref = GraphElementRef(GraphElementKind.LINK, link_uid)
        if link_uid == _FSM_ENTRY_LINK_ID:
            if src_node != self._entry_uid:
                return
            self._set_default_state(target_name, "Change default state")
            return
        if src_node == self._entry_uid:
            return
        link = self._graph.find_link(link_uid)
        transition_document = self._graph_transition_document(link_uid)
        if link is None or transition_document is None:
            return
        if any(
            other.uid != link_uid
            and other.source_node == src_node
            and other.target_node == dst_node
            for other in self._graph.links
        ):
            return
        replacement = AnimTransition.from_dict(transition_document)
        replacement.target_state = target_name
        before = self._graph.capture_authoring_state()
        replaced = self._graph.replace_link(
            link_uid,
            src_node,
            "out",
            dst_node,
            "in",
        )
        if replaced is None:
            return
        replaced.data = self._transition_graph_properties(replacement)
        self._commit_node_graph_change(
            "Reconnect transition",
            before,
            selection_after=(ref,),
        )

    def _on_nodes_deleted(self, uids: List[str]):
        stable_ids = tuple(uid for uid in uids if uid != self._entry_uid)
        if stable_ids:
            self._remove_states(stable_ids)

    def _on_node_add_request(self, type_id: str, x: float, y: float):
        if type_id != FSM_STATE_NODE_TYPE_ID:
            return
        fsm = self._fsm
        if fsm is None:
            return
        state_count = sum(
            node.type_id == FSM_STATE_NODE_TYPE_ID for node in self._graph.nodes
        )
        state = AnimState(name=self._unique_state_name(f"State {state_count}"))
        state.position = [x, y]
        if self._is_timeline_mode():
            state.kind = "timeline"
        self._insert_state(
            state,
            "Add state",
            make_default=not bool(fsm.default_state),
        )

    # ── Shared create palette domain adapter ──────────────────────────
    def _node_creation_entries(self, request: dict) -> list[NodeCreationEntry]:
        if request.get("source_node") and request.get("source_kind") != PinKind.OUTPUT:
            return []
        if self._is_timeline_mode():
            options = ((t("animfsm_editor.node_kind_timeline"), "timeline"),)
        else:
            options = (
                (t("animfsm_editor.node_kind_clip"), "clip"),
                (t("animfsm_editor.node_kind_blend"), "blend"),
            )
        category = t("animfsm_editor.create_node_title")
        return [
            NodeCreationEntry(key=kind, label=label, category=category)
            for label, kind in options
        ]

    def _on_node_creation_selected(
        self, entry: NodeCreationEntry, request: dict
    ) -> None:
        self._create_state_from_link(entry.key, request)

    def _create_state_from_link(self, kind: str, ctx_data: dict):
        """Create a new state at the drop point and connect it from the drag source."""
        if self._fsm is None or not ctx_data:
            return
        gx = float(ctx_data.get("gx", 0.0))
        gy = float(ctx_data.get("gy", 0.0))
        src_node = str(ctx_data.get("source_node", ctx_data.get("src_node", "")))
        from_entry = (src_node == self._entry_uid)
        src_name = "" if from_entry else self._graph_state_name(src_node)

        base_name = "Timeline" if kind == "timeline" else "State"
        state = AnimState(name=self._unique_state_name(base_name))
        state.position = [gx, gy]
        if kind in ("blend", "timeline"):
            state.kind = kind
        before = self._graph.capture_authoring_state()
        self._graph.add_node(
            FSM_STATE_NODE_TYPE_ID,
            gx,
            gy,
            uid=state.stable_id,
            **self._state_graph_properties(state),
        )
        if from_entry:
            self._set_default_link_in_graph(state.stable_id)
        elif src_name:
            if not any(
                link.source_node == src_node and link.target_node == state.stable_id
                for link in self._graph.links
            ):
                transition = AnimTransition(target_state=state.name)
                self._graph.add_link(
                    src_node,
                    "out",
                    state.stable_id,
                    "in",
                    uid=transition.stable_id,
                    **self._transition_graph_properties(transition),
                )
        self._commit_node_graph_change(
            "Create node from link",
            before,
            selection_after=(
                GraphElementRef(GraphElementKind.NODE, state.stable_id),
            ),
        )

    def _on_play_mode_changed(self, event):
        """Keep editor drafts in memory when Play Mode changes.

        Entering Play must not turn a stale restored draft into an implicit
        disk write.  The runtime consumes the last explicitly saved asset;
        Ctrl+S and the panel save controls remain the only persistence paths.
        """
        del event

    def _on_canvas_drop(self, payload_type: str, payload, gx: float, gy: float):
        """Handle items dropped onto the node graph canvas (clip / timeline file paths)."""
        is_tl = self._is_timeline_mode()
        if payload_type == "ANIMTIMELINE_FILE":
            # Timelines only become nodes inside a Timeline FSM.
            if is_tl:
                self._drop_timeline_to_canvas(payload, gx, gy)
            return
        if is_tl:
            return  # Timeline FSMs don't accept animation clips.
        if payload_type not in ("ANIMCLIP_FILE", "ANIMCLIP3D_FILE"):
            return
        if not isinstance(payload, str):
            return
        p = payload.strip()
        if not p:
            return
        if not self._clip_path_matches_fsm_mode(p):
            return
        # Check if dropped on an existing state node
        for uid, name in self._uid_to_name.items():
            node = self._graph.find_node(uid)
            if node and abs(node.pos_x - gx) < 80 and abs(node.pos_y - gy) < 40:
                state = self._fsm.get_state(name) if self._fsm else None
                if state:
                    self._assign_clip_to_state(state, p, node, record_undo=True)
                return
        # Otherwise create a new state with this clip (state name stays independent)
        if self._fsm:
            state = AnimState(
                name=self._unique_state_name(f"State {self._fsm.state_count}")
            )
            state.position = [gx, gy]
            self._assign_clip_to_state(state, p, record_undo=False)
            self._insert_state(
                state,
                "Drop clip to canvas",
                make_default=not bool(self._fsm.default_state),
            )

    def _drop_timeline_to_canvas(self, payload, gx: float, gy: float):
        """Create or assign a Timeline node from a dropped ``.animtimeline`` path."""
        if not isinstance(payload, str):
            return
        p = payload.strip()
        if not p or not p.lower().endswith(".animtimeline"):
            return
        # Dropped onto an existing node → convert it into a timeline node.
        for uid, name in self._uid_to_name.items():
            node = self._graph.find_node(uid)
            if node and abs(node.pos_x - gx) < 80 and abs(node.pos_y - gy) < 40:
                state = self._fsm.get_state(name) if self._fsm else None
                if state:
                    self._assign_timeline_to_state(state, p, node, record_undo=True)
                return
        if self._fsm:
            state = AnimState(name=self._unique_state_name("Timeline"))
            state.position = [gx, gy]
            state.kind = "timeline"
            self._assign_timeline_to_state(state, p, record_undo=False)
            self._insert_state(
                state,
                "Drop timeline to canvas",
                make_default=not bool(self._fsm.default_state),
            )

    def _assign_timeline_to_state(self, state: AnimState, path, node=None, *, record_undo: bool = True):
        try:
            p = resolve_asset_reference_path("AnimationTimeline", path)
        except (KeyError, TypeError, ValueError) as exc:
            Debug.log_error(f"Animation timeline assignment rejected: {exc}")
            return
        guid = self._resolve_guid(p) if p else ""
        resolved_path = "" if guid else (p or "")
        if record_undo:
            self._update_state_fields(
                state,
                "Assign timeline",
                merge_key=f"state:{state.stable_id}:timeline",
                kind="timeline",
                timeline_guid=guid,
                timeline_path=resolved_path,
            )
        else:
            state.kind = "timeline"
            state.timeline_guid = guid
            state.timeline_path = resolved_path
        self._clip_name_cache = {}

    def _clear_timeline_from_state(self, state: AnimState, node=None, *, record_undo: bool = True):
        if record_undo:
            self._update_state_fields(
                state,
                "Clear timeline",
                merge_key=f"state:{state.stable_id}:timeline",
                timeline_guid="",
                timeline_path="",
            )
        else:
            state.timeline_guid = ""
            state.timeline_path = ""
        self._clip_name_cache = {}

    def _timeline_display_name(self, state: AnimState) -> str:
        guid = str(getattr(state, "timeline_guid", "") or "")
        path = str(getattr(state, "timeline_path", "") or "")
        resolved = path
        if not resolved and guid:
            try:
                from Infernux.core.assets import AssetManager
                adb = getattr(AssetManager, "_asset_database", None)
                if adb:
                    resolved = adb.get_path_from_guid(guid) or ""
            except Exception:
                resolved = path
        if resolved:
            base = os.path.basename(resolved)
            dot = base.rfind(".")
            return base[:dot] if dot > 0 else base
        return f"GUID:{guid[:8]}\u2026" if guid else "None"

    def _render_timeline_reference_row(self, ctx: InxGUIContext, state: AnimState, node, lw: float) -> None:
        display = self._timeline_display_name(state)

        field_label(ctx, t("animfsm_editor.timeline_ref"), lw)
        tl_ping = str(getattr(state, "timeline_path", "") or "").strip()
        if not tl_ping:
            try:
                from Infernux.core.assets import AssetManager
                adb = getattr(AssetManager, "_asset_database", None)
                tl_guid = str(getattr(state, "timeline_guid", "") or "")
                if adb and tl_guid:
                    tl_ping = adb.get_path_from_guid(tl_guid) or ""
            except Exception:
                pass
        render_asset_reference_field(
            ctx,
            f"atl_fsm_tl_{node.uid}",
            display,
            "Timeline",
            asset_type="AnimationTimeline",
            accept_drag_type=asset_type_registry.require("AnimationTimeline").drag_types,
            on_assign=lambda p, _st=state, _nd=node: self._assign_timeline_to_state(_st, p, _nd),
            on_clear=lambda _st=state, _nd=node: self._clear_timeline_from_state(_st, _nd),
            ping_path=tl_ping or None,
            has_value=bool(state.timeline_guid or state.timeline_path),
            reference_value={
                "asset_type": "AnimationTimeline",
                "guid": str(getattr(state, "timeline_guid", "") or ""),
                "path_hint": tl_ping,
            },
            semantic_id="animfsm.state.timeline",
        )

    # ── Helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _resolve_guid(path: str) -> str:
        """Resolve a file path to an asset GUID."""
        try:
            from Infernux.core.assets import AssetManager
            guid = AssetManager._get_guid_from_path(path)
            return guid or ""
        except Exception:
            return ""

    def _clear_clip_from_state(self, state: AnimState, node=None, *, record_undo: bool = True):
        if record_undo:
            self._update_state_fields(
                state,
                "Clear clip",
                merge_key=f"state:{state.stable_id}:clip",
                clip_guid="",
                clip_path="",
            )
        else:
            state.clip_guid = ""
            state.clip_path = ""
        self._clip_name_cache = {}

    def _assign_clip_to_state(self, state: AnimState, clip_path, node=None, *, record_undo: bool = True):
        """Assign a clip path/guid to a state."""
        try:
            p = resolve_asset_reference_path(self._fsm_clip_asset_type(), clip_path)
        except (KeyError, TypeError, ValueError) as exc:
            Debug.log_error(f"Animation state clip assignment rejected: {exc}")
            return
        guid = self._resolve_guid(p) if p else ""
        path = "" if guid else (p or "")
        if record_undo:
            self._update_state_fields(
                state,
                "Assign clip",
                merge_key=f"state:{state.stable_id}:clip",
                clip_guid=guid,
                clip_path=path,
            )
        else:
            state.clip_guid = guid
            state.clip_path = path
        self._clip_name_cache = {}

    # ── Save ──────────────────────────────────────────────────────────

    def _do_save(self):
        return self._request_document_save(save_as=False)

    def _request_document_save(self, *, save_as: bool) -> bool:
        from Infernux.engine.interaction import DocumentRegistry

        document = self._fsm_document()
        if document is None or self._fsm is None:
            return False
        return DocumentRegistry.instance().request_save(
            document.document_id,
            save_as=save_as,
        ).accepted

    def request_authoring_save_as(self, ticket):
        from Infernux.engine.interaction import (
            DocumentActionResult,
            DocumentActionStatus,
        )

        self._pending_save_ticket_id = ticket.ticket_id
        if self._show_save_as_dialog():
            return DocumentActionResult(DocumentActionStatus.PENDING)
        self._pending_save_ticket_id = ""
        return DocumentActionResult(
            DocumentActionStatus.REJECTED,
            "no project root is available",
        )

    def discard(self, *, document_id: str) -> bool:
        if document_id != self.document_id:
            return False
        return self._discard_unsaved_changes()

    def _show_save_as_dialog(self) -> bool:
        safe_name = (self._fsm.name or "NewStateMachine").replace(" ", "_")
        # Timeline-mode FSMs save as .timelinefsm (so TimelineAction can pick them up).
        if self._is_timeline_mode():
            ext = "timelinefsm"
            title = "Save Timeline State Machine"
        else:
            ext = "animfsm"
            title = "Save Animation State Machine"
        if not self._save_as_dialog.request(
            title=title,
            extension=ext,
            default_name=safe_name,
            current_path=self._file_path,
            save_callback=self._save_to,
            cancel_callback=self._cancel_pending_save,
        ):
            Debug.log_warning("[AnimFSM] No project root set - cannot save state machine.")
            return False
        return True

    def _save_to(self, target: str, *, ticket_id: str = "") -> bool:
        active_ticket_id = ticket_id or self._pending_save_ticket_id
        if not active_ticket_id:
            raise RuntimeError(
                "Animation FSM saves require a DocumentRegistry SaveTicket"
            )
        result = self._authoring_document_controller.continue_save_to_resource(
            active_ticket_id,
            target,
        )
        if result.accepted:
            self._pending_save_ticket_id = ""
        return result.accepted

    def capture_authoring_save_snapshot(self, target: str):
        from Infernux.engine.interaction import (
            AuthoringAssetSnapshot,
            document_content_token,
        )

        fsm = self._fsm
        if fsm is None:
            raise RuntimeError("Animation FSM has no authoring model")
        normalized = self._normalize_fsm_path(target)
        if not normalized:
            raise ValueError("Animation FSM save target is invalid")
        title = os.path.splitext(os.path.basename(normalized))[0]
        document = copy.deepcopy(fsm.to_dict())
        document["name"] = title
        AnimStateMachine.from_dict(document)
        return AuthoringAssetSnapshot(
            normalized,
            json.dumps(document, indent=2, ensure_ascii=False) + "\n",
            document_content_token(document),
            title,
            document,
        )

    def publish_authoring_save_snapshot(self, snapshot) -> str:
        if self._fsm is None:
            return "Animation FSM authoring model disappeared before publication"
        self._fsm.name = snapshot.title
        self._fsm.file_path = snapshot.target_path
        self._file_path = snapshot.target_path
        self._persist_panel_state()
        Debug.log(f"Saved animfsm: {snapshot.target_path}")
        publication_error = ""
        try:
            from Infernux.core.assets import AssetManager

            result = AssetManager.reimport_asset(snapshot.target_path)
            if not result:
                result = AssetManager.import_asset(snapshot.target_path)
            if not result:
                publication_error = str(
                    getattr(result, "error", "")
                    or f"animation FSM publication failed: {snapshot.target_path}"
                )
        except Exception as exc:
            publication_error = str(exc)
        self._hot_reload_animators(snapshot.target_path)
        return publication_error

    def current_authoring_content_token(self) -> str:
        from Infernux.engine.interaction import document_content_token

        if self._fsm is None:
            return ""
        return document_content_token(self._fsm.to_dict())

    def _cancel_pending_save(self) -> None:
        ticket_id = self._pending_save_ticket_id
        self._pending_save_ticket_id = ""
        if ticket_id:
            from Infernux.engine.interaction import DocumentRegistry

            DocumentRegistry.instance().complete_save(
                ticket_id,
                success=False,
                cancelled=True,
                message="save was cancelled",
            )
    def _discard_unsaved_changes(self) -> bool:
        target = self._file_path or (self._fsm.file_path if self._fsm is not None else "")
        if target:
            fsm = AnimStateMachine.load(target)
            if fsm is None:
                Debug.log_warning(f"Failed to discard animfsm changes: {target}")
                return False
            self._fsm = fsm
            self._file_path = self._normalize_fsm_path(target) or target
            for state in fsm.states:
                if not state.clip_guid and state.clip_path:
                    state.clip_guid = self._resolve_guid(state.clip_path)
                if state.clip_guid:
                    state.clip_path = ""
            self._sync_graph_from_fsm()
            self._graph_selection.clear(record_history=False)
            return True
        self._fsm = AnimStateMachine(name="New State Machine")
        self._file_path = ""
        self._sync_graph_from_fsm()
        self._graph_selection.clear(record_history=False)
        # DocumentRegistry.request_discard() restores the saved revision after
        # this controller has successfully restored its model.
        return True

    def _hot_reload_animators(self, fsm_path: str):
        """Reload running 2D/3D animators that reference this FSM."""
        try:
            from Infernux.engine.play_mode import PlayModeManager, PlayModeState
            pmm = PlayModeManager.instance()
            if not pmm or pmm.state != PlayModeState.PLAYING:
                return
            from Infernux.lib import SceneManager
            scene = SceneManager.instance().get_active_scene()
            if not scene:
                return
            from Infernux.components.spirit_animator import SpiritAnimator
            from Infernux.components.skeletal_animator import SkeletalAnimator
            from Infernux.components.timeline_action import TimelineAction
            for go in scene.get_all_objects():
                animator = go.get_component(SpiritAnimator)
                if animator and animator._fsm and same_path(
                        animator._fsm.file_path or "", fsm_path):
                    animator.reload_controller()
                skel = go.get_component(SkeletalAnimator)
                if skel and skel._fsm and same_path(
                        skel._fsm.file_path or "", fsm_path):
                    skel.reload_controller()
                ta = go.get_component(TimelineAction)
                if ta is not None:
                    rt = getattr(ta, "_runtime", None)
                    fsm_obj = getattr(rt, "_fsm", None) if rt is not None else None
                    if fsm_obj is not None and same_path(
                            getattr(fsm_obj, "file_path", "") or "", fsm_path):
                        ta.reload_controller()
        except Exception:
            pass
