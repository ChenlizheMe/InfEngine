"""
Animation State Machine Editor — visual node-graph editor for .animfsm files.

Displays states as nodes with connections representing transitions.
Drag from an output pin to an input pin to create a transition.
Click a node to edit its properties in the right-side inspector.
Opened from the Animation menu or by double-clicking a .animfsm file
in the Project panel.
"""

from __future__ import annotations

import ast
import copy
import os
import re
import threading
import uuid
from typing import Dict, List, Optional, Tuple

from Infernux.engine.path_utils import path_key, relative_path, resolved_path, same_path
from Infernux.core.anim_state_machine import (
    AnimStateMachine,
    AnimState,
    AnimTransition,
    AnimParameter,
)
from Infernux.core.asset_ref import AnimationClipRef, AnimationClip3DRef, get_asset_type_config
from Infernux.core.node_graph import (
    GraphLink,
    GraphNode,
    NodeGraph,
    NodeTypeDef,
    PinCategory,
    PinDef,
    PinKind,
    node_catalog,
)
from Infernux.debug import Debug
from Infernux.engine.i18n import t
from Infernux.engine.interaction import (
    ClipboardDomain,
    ClipboardItem,
    ClipboardService,
    GraphActionDiff,
    GraphElementKind,
    GraphElementRef,
    GraphMutation,
    GraphMutationKind,
    GraphSelectionController,
)
from Infernux.lib import InxGUIContext

from .asset_save_dialog import AssetSaveAsDialog
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
from .imgui_keys import KEY_DELETE
from .node_graph_view import NodeCreationEntry, NodeGraphView
from ._inspector_references import render_object_field, _picker_assets
from .inspector_utils import field_label, max_label_w
from .panel_registry import editor_panel
from .theme import ImGuiCol, Theme
from .igui import IGUI


_FSM_ENTRY_NODE_ID = "animfsm.entry"
_FSM_ENTRY_LINK_ID = "animfsm.entry-link"
_FSM_GRAPH_ID = "animfsm.graph"


# Legacy single-compare fallback when ast parse fails
_COND_NUM_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*(==|!=|<=|>=|<|>)\s*(-?[0-9]+(?:\.[0-9]*)?)\s*$"
)
_COND_NOT_RE = re.compile(r"^\s*not\s+([A-Za-z_][A-Za-z0-9_]*)\s*$")
_COND_BOOL_EQ_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*==\s*(True|False)\s*$"
)

_OPS = ["<", ">", "<=", ">=", "==", "!="]


def _fmt_rhs_float(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.8g}"


def _cmpop_to_str(op: ast.cmpop) -> Optional[str]:
    if isinstance(op, ast.Eq):
        return "=="
    if isinstance(op, ast.NotEq):
        return "!="
    if isinstance(op, ast.Lt):
        return "<"
    if isinstance(op, ast.LtE):
        return "<="
    if isinstance(op, ast.Gt):
        return ">"
    if isinstance(op, ast.GtE):
        return ">="
    return None


def _ast_to_float(node: ast.expr) -> Optional[float]:
    if isinstance(node, ast.Constant):
        v = node.value
        if isinstance(v, bool):
            return 1.0 if v else 0.0
        if isinstance(v, (int, float)):
            return float(v)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        inner = _ast_to_float(node.operand)
        if inner is not None:
            return -inner
    return None


def _compare_to_term(n: ast.Compare) -> Optional[dict]:
    if len(n.ops) != 1 or len(n.comparators) != 1:
        return None
    if not isinstance(n.left, ast.Name):
        return None
    op_s = _cmpop_to_str(n.ops[0])
    if not op_s:
        return None
    fv = _ast_to_float(n.comparators[0])
    if fv is None:
        return None
    return {"name": n.left.id, "op": op_s, "value": float(fv)}


def _flatten_and_only(node: ast.expr) -> List[ast.Compare]:
    """Only AND chains (Unity-style: multiple conditions are all required)."""
    if isinstance(node, ast.Compare):
        return [node]
    if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.And):
        out: List[ast.Compare] = []
        for v in node.values:
            sub = _flatten_and_only(v)
            if not sub:
                return []
            out.extend(sub)
        return out
    return []


def _encode_condition_model(terms: List[dict]) -> str:
    """Encode as left-associative ``and`` chain (implicit between rows)."""
    if not terms:
        return ""
    if len(terms) == 1:
        t = terms[0]
        return f"({t['name']} {t['op']} {_fmt_rhs_float(float(t['value']))})"
    expr = f"({terms[0]['name']} {terms[0]['op']} {_fmt_rhs_float(float(terms[0]['value']))})"
    for i in range(1, len(terms)):
        tn = terms[i]
        part = f"({tn['name']} {tn['op']} {_fmt_rhs_float(float(tn['value']))})"
        expr = f"({expr} and {part})"
    return expr


def _simple_condition_to_terms(cond: str) -> List[dict]:
    c = (cond or "").strip()
    if not c:
        return []
    m = _COND_NUM_RE.match(c)
    if m:
        name, op, num_s = m.group(1), m.group(2), m.group(3)
        try:
            v = float(num_s)
        except ValueError:
            return []
        return [{"name": name, "op": op, "value": v}]
    m2 = _COND_BOOL_EQ_RE.match(c)
    if m2:
        name, tf = m2.group(1), m2.group(2)
        return [{"name": name, "op": "==", "value": 1.0 if tf == "True" else 0.0}]
    m3 = _COND_NOT_RE.match(c)
    if m3:
        return [{"name": m3.group(1), "op": "==", "value": 0.0}]
    if re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", c):
        return [{"name": c, "op": "==", "value": 1.0}]
    return []


def parse_condition_string_to_model(cond: str) -> List[dict]:
    c = (cond or "").strip()
    if not c:
        return []
    try:
        tree = ast.parse(c, mode="eval")
        body = tree.body
        terms_ast = _flatten_and_only(body)
        if not terms_ast:
            return _simple_condition_to_terms(c)
        out: List[dict] = []
        for t in terms_ast:
            d = _compare_to_term(t)
            if not d:
                return _simple_condition_to_terms(c)
            out.append(d)
        return out
    except (SyntaxError, ValueError, TypeError):
        pass
    return _simple_condition_to_terms(c)


def _replace_identifier_in_expr(expr: str, old: str, new: str) -> str:
    if not old or old == new:
        return expr
    return re.sub(r"\b" + re.escape(old) + r"\b", new, expr)


# ═══════════════════════════════════════════════════════════════════════════
# Node type definition for animation states
# ═══════════════════════════════════════════════════════════════════════════

_STATE_TYPE = NodeTypeDef(
    type_id="anim_state",
    label="State",
    header_color=(0.20, 0.20, 0.22, 1.0),
    pins=[
        PinDef(
            id="in", label="In", kind=PinKind.INPUT,
            color=(0.50, 0.52, 0.55, 1.0), pin_category=PinCategory.EXEC,
        ),
        PinDef(
            id="out", label="Out", kind=PinKind.OUTPUT,
            color=(0.52, 0.54, 0.56, 1.0), pin_category=PinCategory.EXEC,
        ),
    ],
    min_width=172.0,
    body_bottom_pad=0.0,
)

_ENTRY_TYPE = NodeTypeDef(
    type_id="anim_entry",
    label="Entry",
    header_color=(0.22, 0.21, 0.23, 1.0),
    pins=[
        PinDef(
            id="out", label="Start", kind=PinKind.OUTPUT,
            color=Theme.APPLY_BUTTON, pin_category=PinCategory.EXEC,
        ),
    ],
    min_width=88.0,
    deletable=False,
)

_DETAIL_PANEL_W = 300.0
_VARS_PANEL_W = 236.0

_FSM_PARAM_COLORS = {
    "bool": (0.78, 0.25, 0.31, 1.0),
    "float": (0.34, 0.72, 0.42, 1.0),
    "int": (0.30, 0.68, 0.52, 1.0),
}

node_catalog.register("anim_fsm", [_STATE_TYPE, _ENTRY_TYPE])


# ═══════════════════════════════════════════════════════════════════════════
# Panel
# ═══════════════════════════════════════════════════════════════════════════

@editor_panel(
    "Animation State Machine Editor",
    type_id="animfsm_editor",
    title_key="panel.animfsm_editor",
    menu_path="Animation",
)
class AnimFSMEditorPanel(EditorPanel):
    """Node-graph editor for animation state machines."""

    window_id = "animfsm_editor"

    def __init__(self):
        super().__init__(title="Animation State Machine Editor", window_id="animfsm_editor")
        self._fsm: Optional[AnimStateMachine] = None
        self._file_path: str = ""
        self._dirty: bool = True
        self._save_as_dialog = AssetSaveAsDialog("animfsm.save_as", "state machine")
        self._pending_mode_switch: Optional[str] = None
        self._mode_switch_confirm_requested: bool = False
        self._mode_switch_waiting_for_save: bool = False

        # Node graph
        self._graph = NodeGraph(graph_kind="anim_fsm")

        self._view = NodeGraphView()
        self._view.semantic_namespace = "animfsm.graph"
        self._view.graph = self._graph
        self._view.on_link_created = self._on_link_created
        self._view.on_link_deleted = self._on_link_deleted
        self._view.on_link_replaced = self._on_link_replaced
        self._view.on_nodes_deleted = self._on_nodes_deleted
        self._view.on_node_add_request = self._on_node_add_request
        self._view.on_node_creation_entries = self._node_creation_entries
        self._view.on_node_creation_selected = self._on_node_creation_selected
        self._view.on_selection_changed = self._on_canvas_selection_changed
        self._view.on_canvas_drop = self._on_canvas_drop
        self._view.on_node_drag_start = self._on_node_drag_start
        self._view.on_node_drag_end = self._on_node_drag_end
        self._view.on_node_header_color_changed = self._on_node_header_color_changed

        self._graph_selection = GraphSelectionController(
            owner_id=self.window_id,
            document_id=lambda: self.document_id,
            contains=self._contains_graph_element,
            view=self._view,
        )
        self._left_overlay = FloatingOverlayState()
        self._right_overlay = FloatingOverlayState()

        # Maps: state name ↔ node uid
        self._name_to_uid: Dict[str, str] = {}
        self._uid_to_name: Dict[str, str] = {}

        # Entry node uid
        self._entry_uid: str = ""

        # Panel persistence: ``load_state`` may run from bootstrap or first render
        self._panel_state_restored_once: bool = False
        self._panel_restore_data: Optional[dict] = None
        self._undo_drag_node_position: Optional[Tuple[float, float]] = None
        self._pending_save_ticket_id: str = ""

        self._view.on_copy = self._on_graph_copy
        self._view.on_paste = self._on_graph_paste

        # Start with a blank FSM
        self._new_fsm()

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
            controller=self,
        )
        self.bind_document(document.document_id)
        self._dirty = document.is_dirty
        self._graph_selection.refresh()

    def _fsm_document(self):
        from Infernux.engine.interaction import DocumentRegistry

        return DocumentRegistry.instance().get(self.document_id)

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
        return next(
            (
                parameter
                for parameter in self._fsm.parameters
                if parameter.stable_id == stable_id
            ),
            None,
        )

    def _parameter_index_by_id(self, stable_id: str) -> int:
        if self._fsm is None:
            return -1
        return next(
            (
                index
                for index, parameter in enumerate(self._fsm.parameters)
                if parameter.stable_id == stable_id
            ),
            -1,
        )

    def _selected_parameter(self) -> Optional[AnimParameter]:
        return self._parameter_by_id(
            self._graph_selection.primary_id(GraphElementKind.PARAMETER)
        )

    @staticmethod
    def _parameter_document_for_kind(
        parameter: AnimParameter, kind: str
    ) -> dict:
        document = {
            "stable_id": parameter.stable_id,
            "name": parameter.name,
            "kind": kind,
        }
        if kind == "bool":
            document["default_bool"] = bool(parameter.default_bool)
        elif kind == "int":
            document["default_int"] = int(parameter.default_int)
        elif kind == "float":
            document["default_float"] = float(parameter.default_float)
        else:
            raise ValueError(f"unsupported animation parameter kind: {kind}")
        return document

    def _insert_parameter(self) -> bool:
        if self._fsm is None:
            return False
        parameter = AnimParameter(
            name=f"var_{len(self._fsm.parameters)}",
            kind="float",
        )
        ref = GraphElementRef(GraphElementKind.PARAMETER, parameter.stable_id)
        return self._execute_graph_mutations(
            "Add parameter",
            (
                GraphMutation(
                    GraphMutationKind.INSERT,
                    ref,
                    after=parameter.to_dict(),
                    after_index=len(self._fsm.parameters),
                ),
            ),
            selection_after=(ref,),
        )

    def _remove_parameter(self, stable_id: str) -> bool:
        parameter = self._parameter_by_id(stable_id)
        index = self._parameter_index_by_id(stable_id)
        if parameter is None or index < 0:
            return False
        selected = self._graph_selection.primary_id(GraphElementKind.PARAMETER)
        return self._execute_graph_mutations(
            "Remove parameter",
            (
                GraphMutation(
                    GraphMutationKind.REMOVE,
                    GraphElementRef(GraphElementKind.PARAMETER, stable_id),
                    before=parameter.to_dict(),
                    before_index=index,
                ),
            ),
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
        return self._execute_graph_mutations(
            description,
            (
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    GraphElementRef(
                        GraphElementKind.PARAMETER, parameter.stable_id
                    ),
                    before=before,
                    after=after,
                ),
            ),
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
        before: Optional[dict] = None,
    ) -> bool:
        before_document = state.to_dict() if before is None else copy.deepcopy(before)
        if before_document == after:
            return False
        return self._execute_graph_mutations(
            description,
            (
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    GraphElementRef(GraphElementKind.NODE, state.stable_id),
                    before=before_document,
                    after=after,
                ),
            ),
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
        after = state.to_dict()
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
        if self._fsm is None:
            return False
        state.transitions = []
        mutations: list[GraphMutation] = [
            GraphMutation(
                GraphMutationKind.INSERT,
                GraphElementRef(GraphElementKind.NODE, state.stable_id),
                after=state.to_dict(),
                after_index=len(self._fsm.states),
            )
        ]
        if make_default:
            mutations.append(
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    GraphElementRef(GraphElementKind.LINK, _FSM_ENTRY_LINK_ID),
                    before={"default_state": self._fsm.default_state},
                    after={"default_state": state.name},
                )
            )
        ref = GraphElementRef(GraphElementKind.NODE, state.stable_id)
        return self._execute_graph_mutations(
            description,
            tuple(mutations),
            selection_after=(ref,),
        )

    def _set_default_state(self, state_name: str, description: str) -> bool:
        if self._fsm is None or self._fsm.default_state == state_name:
            return False
        return self._execute_graph_mutations(
            description,
            (
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    GraphElementRef(GraphElementKind.LINK, _FSM_ENTRY_LINK_ID),
                    before={"default_state": self._fsm.default_state},
                    after={"default_state": state_name},
                ),
            ),
        )

    def _insert_transition(
        self,
        owner: AnimState,
        transition: AnimTransition,
        description: str,
    ) -> bool:
        return self._execute_graph_mutations(
            description,
            (
                GraphMutation(
                    GraphMutationKind.INSERT,
                    GraphElementRef(GraphElementKind.LINK, transition.stable_id),
                    after={
                        "source_state_id": owner.stable_id,
                        "transition": transition.to_dict(),
                    },
                    after_index=len(owner.transitions),
                ),
            ),
        )

    def _remove_transition(self, stable_id: str, description: str) -> bool:
        if self._fsm is None:
            return False
        if stable_id == _FSM_ENTRY_LINK_ID:
            if not self._fsm.default_state:
                return False
            mutation = GraphMutation(
                GraphMutationKind.UPDATE,
                GraphElementRef(GraphElementKind.LINK, stable_id),
                before={"default_state": self._fsm.default_state},
                after={"default_state": ""},
            )
        else:
            found = self._fsm.get_transition_by_id(stable_id)
            if found is None:
                return False
            owner, transition = found
            mutation = GraphMutation(
                GraphMutationKind.REMOVE,
                GraphElementRef(GraphElementKind.LINK, stable_id),
                before={
                    "source_state_id": owner.stable_id,
                    "transition": transition.to_dict(),
                },
                before_index=owner.transitions.index(transition),
            )
        selected = self._graph_selection.primary_id(GraphElementKind.LINK)
        return self._execute_graph_mutations(
            description,
            (mutation,),
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
        found = self._fsm.get_transition_by_id(stable_id)
        if found is None:
            return False
        owner, transition = found
        before = {
            "source_state_id": owner.stable_id,
            "transition": transition.to_dict(),
        }
        after = {
            "source_state_id": owner.stable_id,
            "transition": copy.deepcopy(after_transition),
        }
        if before == after:
            return False
        index = owner.transitions.index(transition)
        return self._execute_graph_mutations(
            description,
            (
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    GraphElementRef(GraphElementKind.LINK, stable_id),
                    before=before,
                    after=after,
                    before_index=index,
                    after_index=index,
                ),
            ),
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
        if self._fsm is None:
            return False
        found = self._fsm.get_transition_by_id(stable_id)
        if found is None:
            return False
        _, transition = found
        after = transition.to_dict()
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
        fsm = self._fsm
        if fsm is None:
            return False
        targets = [
            state for state in fsm.states if state.stable_id in set(stable_ids)
        ]
        if not targets:
            return False
        target_names = {state.name for state in targets}
        mutations: list[GraphMutation] = []
        for owner in fsm.states:
            for index, transition in enumerate(owner.transitions):
                if owner in targets or transition.target_state in target_names:
                    mutations.append(
                        GraphMutation(
                            GraphMutationKind.REMOVE,
                            GraphElementRef(
                                GraphElementKind.LINK, transition.stable_id
                            ),
                            before={
                                "source_state_id": owner.stable_id,
                                "transition": transition.to_dict(),
                            },
                            before_index=index,
                        )
                    )
        if fsm.default_state in target_names:
            replacement_default = next(
                (state.name for state in fsm.states if state not in targets), ""
            )
            mutations.append(
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    GraphElementRef(GraphElementKind.LINK, _FSM_ENTRY_LINK_ID),
                    before={"default_state": fsm.default_state},
                    after={"default_state": replacement_default},
                )
            )
        indexed_targets = sorted(
            ((fsm.states.index(state), state) for state in targets), reverse=True
        )
        for index, state in indexed_targets:
            document = state.to_dict()
            document["transitions"] = []
            mutations.append(
                GraphMutation(
                    GraphMutationKind.REMOVE,
                    GraphElementRef(GraphElementKind.NODE, state.stable_id),
                    before=document,
                    before_index=index,
                )
            )
        selected = set(self._graph_selection.selected_ids(GraphElementKind.NODE))
        return self._execute_graph_mutations(
            "Delete state" if len(targets) == 1 else "Delete states",
            tuple(mutations),
            selection_after=() if selected & set(stable_ids) else None,
        )

    def apply_diff(self, diff: GraphActionDiff) -> None:
        """Apply one precise FSM authoring diff through stable identities."""
        if diff.document_id != self.document_id:
            raise RuntimeError("graph diff targets a different animation FSM document")
        fsm = self._fsm
        if fsm is None:
            raise RuntimeError("animation FSM document has no live model")
        rebuild_graph = False
        for mutation in diff.mutations:
            if mutation.element.kind is GraphElementKind.PARAMETER:
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
                    current = fsm.parameters[index]
                    replacement = AnimParameter.from_dict(mutation.after)
                    if replacement.stable_id != stable_id:
                        raise RuntimeError("animation parameter update changed stable identity")
                    if current.name != replacement.name:
                        self._rename_parameter_in_fsm(current.name, replacement.name)
                        rebuild_graph = True
                    fsm.parameters[index] = replacement
                else:
                    raise RuntimeError(
                        f"unsupported animation parameter mutation: {mutation.kind.value}"
                    )
                continue
            if mutation.element.kind is GraphElementKind.GRAPH:
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
                rebuild_graph = True
                continue
            if mutation.element.kind is GraphElementKind.LINK:
                stable_id = mutation.element.stable_id
                if stable_id == _FSM_ENTRY_LINK_ID:
                    payload = (
                        mutation.after
                        if mutation.kind
                        in (GraphMutationKind.INSERT, GraphMutationKind.UPDATE)
                        else {"default_state": ""}
                    )
                    if not isinstance(payload, dict):
                        raise RuntimeError("animation entry link mutation is incomplete")
                    default_state = str(payload.get("default_state") or "")
                    if default_state and fsm.get_state(default_state) is None:
                        raise RuntimeError(
                            f"animation default state no longer exists: {default_state}"
                        )
                    fsm.default_state = default_state
                    rebuild_graph = True
                    continue
                found = fsm.get_transition_by_id(stable_id)
                if mutation.kind is GraphMutationKind.REMOVE:
                    if found is None:
                        raise RuntimeError(
                            f"animation transition no longer exists: {stable_id}"
                        )
                    owner, transition = found
                    owner.transitions.remove(transition)
                    rebuild_graph = True
                    continue
                payload = mutation.after
                if not isinstance(payload, dict):
                    raise RuntimeError("animation transition mutation is incomplete")
                transition_document = payload.get("transition")
                source_state_id = str(payload.get("source_state_id") or "")
                new_owner = fsm.get_state_by_id(source_state_id)
                if new_owner is None or not isinstance(transition_document, dict):
                    raise RuntimeError("animation transition mutation is incomplete")
                replacement = AnimTransition.from_dict(transition_document)
                if replacement.stable_id != stable_id:
                    raise RuntimeError("animation transition mutation changed stable identity")
                if fsm.get_state(replacement.target_state) is None:
                    raise RuntimeError("animation transition target no longer exists")
                if mutation.kind is GraphMutationKind.INSERT:
                    if found is not None:
                        raise RuntimeError(f"animation transition already exists: {stable_id}")
                elif mutation.kind is GraphMutationKind.UPDATE:
                    if found is None:
                        raise RuntimeError(
                            f"animation transition no longer exists: {stable_id}"
                        )
                    old_owner, current = found
                    old_owner.transitions.remove(current)
                else:
                    raise RuntimeError(
                        f"unsupported animation transition mutation: {mutation.kind.value}"
                    )
                target_index = max(
                    0,
                    min(int(mutation.after_index), len(new_owner.transitions)),
                )
                new_owner.transitions.insert(target_index, replacement)
                rebuild_graph = True
                continue
            if (
                mutation.element.kind is GraphElementKind.NODE
                and mutation.kind is GraphMutationKind.MOVE
            ):
                state = fsm.get_state_by_id(mutation.element.stable_id)
                if state is None or not isinstance(mutation.after, dict):
                    raise RuntimeError(
                        f"animation state no longer exists: {mutation.element.stable_id}"
                    )
                raw_position = mutation.after.get("position")
                if not isinstance(raw_position, (list, tuple)) or len(raw_position) != 2:
                    raise RuntimeError("animation state move requires a two-value position")
                state.position = [float(raw_position[0]), float(raw_position[1])]
                node = self._graph.find_node(state.stable_id)
                if node is not None:
                    node.pos_x, node.pos_y = state.position
                continue
            if mutation.element.kind is GraphElementKind.NODE:
                stable_id = mutation.element.stable_id
                index = self._state_index_by_id(stable_id)
                if mutation.kind is GraphMutationKind.INSERT:
                    if index >= 0 or not isinstance(mutation.after, dict):
                        raise RuntimeError(f"cannot insert animation state {stable_id}")
                    replacement = AnimState.from_dict(mutation.after)
                    if replacement.stable_id != stable_id:
                        raise RuntimeError("animation state insertion changed stable identity")
                    if fsm.get_state(replacement.name) is not None:
                        raise RuntimeError(
                            f"animation state name already exists: {replacement.name}"
                        )
                    target_index = max(
                        0, min(int(mutation.after_index), len(fsm.states))
                    )
                    fsm.states.insert(target_index, replacement)
                elif mutation.kind is GraphMutationKind.REMOVE:
                    if index < 0:
                        raise RuntimeError(f"animation state no longer exists: {stable_id}")
                    state = fsm.states[index]
                    referenced = any(
                        transition.target_state == state.name
                        for owner in fsm.states
                        for transition in owner.transitions
                    )
                    if state.transitions or referenced or fsm.default_state == state.name:
                        raise RuntimeError(
                            "animation state removal must include its links in the same diff"
                        )
                    fsm.states.pop(index)
                elif mutation.kind is GraphMutationKind.UPDATE:
                    if index < 0 or not isinstance(mutation.after, dict):
                        raise RuntimeError(f"cannot update animation state {stable_id}")
                    current = fsm.states[index]
                    replacement = AnimState.from_dict(mutation.after)
                    if replacement.stable_id != stable_id:
                        raise RuntimeError("animation state update changed stable identity")
                    duplicate = fsm.get_state(replacement.name)
                    if duplicate is not None and duplicate is not current:
                        raise RuntimeError(
                            f"animation state name already exists: {replacement.name}"
                        )
                    if current.name != replacement.name:
                        if fsm.default_state == current.name:
                            fsm.default_state = replacement.name
                        for owner in fsm.states:
                            for transition in owner.transitions:
                                if transition.target_state == current.name:
                                    transition.target_state = replacement.name
                    fsm.states[index] = replacement
                else:
                    raise RuntimeError(
                        f"unsupported animation state mutation: {mutation.kind.value}"
                    )
                rebuild_graph = True
                continue
            raise RuntimeError(
                f"unsupported animation FSM graph mutation: "
                f"{mutation.element.kind.value}/{mutation.kind.value}"
            )
        if rebuild_graph:
            self._sync_graph_from_fsm()

    def on_graph_diff_applied(self, _diff: GraphActionDiff) -> None:
        document = self._fsm_document()
        self._dirty = bool(document and document.is_dirty)
        self._graph_selection.refresh()

    def _execute_graph_mutations(
        self,
        description: str,
        mutations: Tuple[GraphMutation, ...],
        *,
        merge_key: str = "",
        selection_after: Optional[Tuple[GraphElementRef, ...]] = None,
    ) -> bool:
        from Infernux.engine.interaction import (
            DocumentRegistry,
            SelectionService,
            SelectionSnapshot,
        )
        from Infernux.engine.undo import GraphDiffCommand, UndoManager

        document = self._fsm_document()
        if document is None or not mutations:
            return False
        registry = DocumentRegistry.instance()
        diff = GraphActionDiff(
            document.document_id,
            tuple(mutations),
            before_revision=document.revision,
            after_revision=registry.reserve_content_revision(document.document_id),
        )
        command = GraphDiffCommand(description, diff, merge_key=merge_key)
        if selection_after is not None:
            service = SelectionService.instance()
            command.before_selection_snapshot = service.snapshot
            targets = tuple(
                element.selection_target(document.document_id)
                for element in selection_after
            )
            command.after_selection_snapshot = SelectionSnapshot.create(
                targets,
                primary=targets[-1] if targets else None,
                anchor=targets[0] if targets else None,
                owner_id=self.window_id if targets else "",
            )
        manager = UndoManager.instance()
        if manager is not None and self._animfsm_undo_enabled():
            applied = manager.execute(command)
        else:
            try:
                command.execute()
                applied = True
            except Exception as exc:
                Debug.log_error(f"Animation FSM graph edit failed: {exc}")
                applied = False
            finally:
                command.dispose()
        if applied and selection_after is not None:
            if selection_after:
                self._graph_selection.select(
                    selection_after,
                    reason="animfsm_graph_edit_selection",
                    record_history=False,
                )
            else:
                self._graph_selection.clear(
                    reason="animfsm_graph_edit_selection",
                    record_history=False,
                )
        return applied

    def _open_animfsm(self, file_path: str):
        """Load an .animfsm file into the editor."""
        normalized_path = self._normalize_fsm_path(file_path)
        target_path = normalized_path or file_path
        fsm = AnimStateMachine.load(target_path)
        if fsm is None:
            Debug.log_warning(f"Failed to load animfsm: {target_path}")
            return
        self._fsm = fsm
        self._file_path = target_path
        self._dirty = False
        for state in fsm.states:
            if not state.clip_guid and state.clip_path:
                state.clip_guid = self._resolve_guid(state.clip_path)
            if state.clip_guid:
                state.clip_path = ""
        self._sync_graph_from_fsm()
        self._replace_fsm_document(resource_path=target_path, dirty=False)
        self._graph_selection.clear(record_history=False)

    def _new_fsm(self):
        """Create a blank FSM for editing."""
        self._fsm = AnimStateMachine(name="New State Machine")
        self._file_path = ""
        self._dirty = True
        self._sync_graph_from_fsm()
        self._replace_fsm_document(resource_path="", dirty=True)
        self._graph_selection.clear(record_history=False)

    def _switch_to_new_mode_resource(self, mode: str) -> None:
        """Leave the current asset and start a new resource in *mode*."""
        if self._dirty:
            self._pending_mode_switch = mode
            self._mode_switch_confirm_requested = True
            return
        self._commit_mode_switch(mode)

    def _commit_mode_switch(self, mode: str) -> None:
        self._pending_mode_switch = None
        self._mode_switch_confirm_requested = False
        self._mode_switch_waiting_for_save = False
        self._new_fsm()
        self._fsm.mode = mode
        self._sync_graph_from_fsm()
        self._sync_project_dirty_flag()

    def _cancel_pending_mode_switch(self) -> None:
        self._pending_mode_switch = None
        self._mode_switch_confirm_requested = False
        self._mode_switch_waiting_for_save = False

    def _render_mode_switch_confirmation(self, ctx: InxGUIContext) -> None:
        target_mode = self._pending_mode_switch
        if not target_mode or self._mode_switch_waiting_for_save:
            return

        from .editor_modal import (
            EditorModalAction,
            begin_editor_modal,
            end_editor_modal,
            render_editor_modal_actions,
        )

        popup_id = f"{t('animfsm.mode_switch.title')}###animfsm_mode_switch_confirm"
        request_open = self._mode_switch_confirm_requested
        self._mode_switch_confirm_requested = False
        if not begin_editor_modal(
            ctx,
            popup_id=popup_id,
            title=t("animfsm.mode_switch.title"),
            semantic_id="animfsm.mode_switch.dialog",
            request_open=request_open,
        ):
            return

        ctx.text_wrapped(t("animfsm.mode_switch.message"))
        ctx.text_wrapped(t("animfsm.mode_switch.question"))

        def _save() -> None:
            self._mode_switch_waiting_for_save = True
            self._do_save()
            ctx.close_current_popup()

        def _discard() -> None:
            mode = self._pending_mode_switch
            if mode:
                self._commit_mode_switch(mode)
            ctx.close_current_popup()

        def _cancel() -> None:
            self._cancel_pending_mode_switch()
            ctx.close_current_popup()

        render_editor_modal_actions(
            ctx,
            [
                EditorModalAction(t("editor.unsaved.save"), "save", _save),
                EditorModalAction(t("editor.unsaved.dont_save"), "discard", _discard),
                EditorModalAction(t("editor.unsaved.cancel"), "cancel", _cancel),
            ],
            semantic_prefix="animfsm.mode_switch",
        )
        end_editor_modal(ctx)

    # ── Lifecycle ──────────────────────────────────────────────────────

    def on_enable(self) -> None:
        from .event_bus import EditorEventBus, EditorEvent
        self._graph_selection.bind()
        bus = EditorEventBus.instance()
        bus.subscribe(EditorEvent.FILE_SELECTED, self._on_file_selected)
        try:
            from Infernux.engine.play_mode import PlayModeManager
            pmm = PlayModeManager.instance()
            if pmm:
                pmm.add_state_change_listener(self._on_play_mode_changed)
        except Exception:
            pass

    def on_disable(self) -> None:
        from .event_bus import EditorEventBus, EditorEvent
        self._graph_selection.unbind()
        bus = EditorEventBus.instance()
        bus.unsubscribe(EditorEvent.FILE_SELECTED, self._on_file_selected)
        try:
            from Infernux.engine.play_mode import PlayModeManager
            pmm = PlayModeManager.instance()
            if pmm:
                pmm.remove_state_change_listener(self._on_play_mode_changed)
        except Exception:
            pass

    def _window_title_suffix(self) -> str:
        self._sync_project_dirty_flag()
        return " *" if self._dirty else ""

    def _sync_project_dirty_flag(self) -> None:
        from Infernux.engine.interaction import DocumentRegistry

        document = DocumentRegistry.instance().get(self.document_id)
        if document is None:
            return
        if self._dirty and not document.is_dirty:
            DocumentRegistry.instance().mark_changed(document.document_id)
        self._dirty = document.is_dirty

    # ── State persistence ──────────────────────────────────────────────

    def save_state(self) -> dict:
        """Persist open file path even if ``_open_animfsm`` is deferred to first frame."""
        data: dict = {}
        fp = self._normalize_fsm_path(self._file_path)
        if not fp and self._fsm is not None:
            fp = self._normalize_fsm_path(getattr(self._fsm, "file_path", "") or "")
        rel_fallback = ""
        if not fp and self._panel_restore_data:
            fp = self._normalize_fsm_path(self._panel_restore_data.get("file_path") or "")
            rel_fallback = (self._panel_restore_data.get("file_path_rel") or "").strip()
        if not fp and not rel_fallback:
            # Startup/load ordering can trigger a save before this panel has
            # finished restoring; preserve any existing persisted target path.
            try:
                from Infernux.engine.ui import panel_state as _ps

                prev = _ps.get(f"panel:{self.window_id}")
                if prev:
                    fp = self._normalize_fsm_path(prev.get("file_path") or "")
                    rel_fallback = (prev.get("file_path_rel") or "").strip()
            except Exception:
                pass
        if fp:
            data["file_path"] = fp
            try:
                from Infernux.engine.project_context import get_project_root

                root = get_project_root()
                if root:
                    data["file_path_rel"] = relative_path(fp, root)
            except (ValueError, OSError):
                pass
        elif rel_fallback:
            data["file_path_rel"] = rel_fallback
        if self._view:
            data["pan_x"] = self._view.pan_x
            data["pan_y"] = self._view.pan_y
            data["zoom"] = self._view.zoom
        data["dirty"] = bool(self._dirty)
        if self._dirty and self._fsm is not None:
            data["draft"] = self._fsm.to_dict()
        return data

    def _resolve_saved_fsm_path(self, data: dict) -> str:
        """Resolve persisted path using absolute path, then project-relative."""
        fp = self._normalize_fsm_path(data.get("file_path") or "")
        rel = (data.get("file_path_rel") or "").strip()
        if fp and os.path.isfile(fp):
            return fp
        if rel:
            try:
                from Infernux.engine.project_context import get_project_root

                root = get_project_root()
                if root:
                    cand = resolved_path(os.path.join(root, rel))
                    if os.path.isfile(cand):
                        return cand
            except (OSError, ValueError):
                pass
        return ""

    def load_state(self, data: dict) -> None:
        if not data:
            self._panel_restore_data = None
            self._panel_state_restored_once = True
            return
        self._panel_restore_data = dict(data)
        if self._view:
            self._view.pan_x = float(data.get("pan_x", self._view.pan_x))
            self._view.pan_y = float(data.get("pan_y", self._view.pan_y))
            self._view.zoom = float(data.get("zoom", self._view.zoom))
        draft = data.get("draft")
        if bool(data.get("dirty")) and isinstance(draft, dict):
            self._fsm = AnimStateMachine.from_dict(draft)
            self._file_path = self._normalize_fsm_path(data.get("file_path") or "")
            self._fsm.file_path = self._file_path
            self._dirty = True
            self._sync_graph_from_fsm()
            self._graph_selection.clear(record_history=False)
            self._panel_state_restored_once = True
            return
        self._panel_state_restored_once = False

    def _apply_pending_panel_restore(self) -> None:
        """Open saved .animfsm once project root can resolve relative paths."""
        if self._panel_state_restored_once:
            return
        data = self._panel_restore_data
        if not data:
            self._panel_state_restored_once = True
            return
        to_open = self._resolve_saved_fsm_path(data)
        if to_open:
            self._open_animfsm(to_open)
            self._panel_state_restored_once = True
            return
        fp = (data.get("file_path") or "").strip()
        rel = (data.get("file_path_rel") or "").strip()
        if not fp and not rel:
            self._panel_state_restored_once = True
            return
        try:
            from Infernux.engine.project_context import get_project_root

            root = get_project_root()
        except Exception:
            root = None
        if root is None:
            return
        self._panel_state_restored_once = True

    def _animfsm_undo_enabled(self) -> bool:
        from Infernux.engine.play_mode import PlayModeManager, PlayModeState
        from Infernux.engine.undo import UndoManager

        mgr = UndoManager.instance()
        if not mgr or not mgr.enabled:
            return False
        pmm = PlayModeManager.instance()
        if pmm and pmm.state != PlayModeState.EDIT:
            return False
        return True

    def _initial_size(self):
        return (900, 600)

    def _empty_state_hint(self) -> str:
        return t("animfsm_editor.open_hint")

    def _empty_state_drop_types(self):
        return ["ANIMFSM_FILE", "TIMELINEFSM_FILE"]

    def _on_empty_state_drop(self, payload_type, payload):
        if payload_type in ("ANIMFSM_FILE", "TIMELINEFSM_FILE") and payload:
            self._open_animfsm(payload)

    # ═══════════════════════════════════════════════════════════════════
    # Rendering
    # ═══════════════════════════════════════════════════════════════════

    # ImGuiKey / ImGuiMod constants
    def on_render_content(self, ctx: InxGUIContext):
        if not self._panel_state_restored_once:
            if self._panel_restore_data is None:
                from Infernux.engine.ui import panel_state as _ps

                data = _ps.get(f"panel:{self.window_id}")
                if data:
                    self.load_state(data)
                else:
                    self._panel_state_restored_once = True
            self._apply_pending_panel_restore()

        self._render_toolbar(ctx)
        ctx.separator()

        avail_w = ctx.get_content_region_avail_width()
        avail_h = ctx.get_content_region_avail_height()
        sidebar_w = min(_VARS_PANEL_W, max(180.0, avail_w * 0.18))
        detail_w = min(_DETAIL_PANEL_W, max(280.0, avail_w * 0.28))
        margin = 8.0
        default_h = min(
            max(160.0, avail_h * 0.52),
            max(160.0, avail_h - margin * 2.0),
        )
        if self._left_overlay.height <= 0.0:
            self._left_overlay.height = default_h
        if self._right_overlay.height <= 0.0:
            self._right_overlay.height = default_h
        update_overlay_resize_drag(
            ctx,
            self._left_overlay,
            avail_h=avail_h,
            margin=margin,
        )
        update_overlay_resize_drag(
            ctx,
            self._right_overlay,
            avail_h=avail_h,
            margin=margin,
        )

        graph_visible = ctx.begin_child("##fsm_graph_region", avail_w, avail_h, False)
        try:
            if graph_visible:
                self._view.render(ctx)
                render_floating_overlay(
                    ctx,
                    self._left_overlay,
                    child_id="##fsm_parameters",
                    x=margin,
                    y=margin,
                    width=sidebar_w,
                    render_fn=lambda: self._render_variables_panel(ctx),
                )
                render_floating_overlay(
                    ctx,
                    self._right_overlay,
                    child_id="##fsm_detail",
                    x=max(margin, avail_w - detail_w - margin),
                    y=margin,
                    width=detail_w,
                    render_fn=lambda: self._render_detail_panel(ctx),
                )
        finally:
            ctx.end_child()

        # Accept .animfsm / .timelinefsm file drops
        payload = ctx.accept_drag_drop_payload("ANIMFSM_FILE")
        if payload:
            self._open_animfsm(payload)
        payload_tl = ctx.accept_drag_drop_payload("TIMELINEFSM_FILE")
        if payload_tl:
            self._open_animfsm(payload_tl)

        self._render_mode_switch_confirmation(ctx)
        self._save_as_dialog.render(
            ctx,
            self._save_to,
            cancel_callback=self._cancel_pending_save,
        )

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
        ctx.record_semantic_item("button", new_label, True, "animfsm.toolbar.new")
        if new_pressed:
            self._new_fsm()
            return

        ctx.same_line(0, 8)
        save_label = t("animfsm_editor.save") if self._file_path else t("animfsm_editor.save_as")
        save_pressed = ctx.button(save_label)
        ctx.record_semantic_item("button", save_label, True, "animfsm.toolbar.save")
        if save_pressed:
            self._do_save()

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
            "status", "Dirty", True, "animfsm.document.dirty", bool(self._dirty),
        )
        if new_mode_idx != mode_idx:
            self._switch_to_new_mode_resource(_MODES[new_mode_idx])

        if self._file_path:
            ctx.same_line(0, 12)
            ctx.label(self._file_path)

    @staticmethod
    def _sanitize_param_identifier(raw: str) -> str:
        """Keep ``[A-Za-z_][A-Za-z0-9_]*`` for condition variable names."""
        s = (raw or "").strip()
        out: List[str] = []
        for i, ch in enumerate(s):
            if i == 0:
                if ch.isalpha() or ch == "_":
                    out.append(ch)
            else:
                if ch.isalnum() or ch == "_":
                    out.append(ch)
        return "".join(out)

    def _default_compare_term(self, fsm: AnimStateMachine) -> dict:
        p0 = fsm.parameters[0]
        return {"name": p0.name, "op": ">", "value": 0.0}

    def _apply_condition_model(self, lk: GraphLink, terms: List[dict]) -> None:
        cond = _encode_condition_model(terms)
        lk.data["cond_terms"] = [dict(x) for x in terms]
        lk.data.pop("cond_joins", None)
        old = str(lk.data.get("condition", "") or "")
        if cond == old:
            return
        self._update_transition_fields(
            lk.uid,
            "Edit transition condition",
            merge_key=f"transition:{lk.uid}:condition",
            condition=cond,
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
        cond = str(lk.data.get("condition", "") or "")
        if "cond_terms" not in lk.data:
            terms = parse_condition_string_to_model(cond)
            lk.data["cond_terms"] = terms
        else:
            terms = lk.data.get("cond_terms") or []
            if not isinstance(terms, list):
                terms = []
        lk.data.pop("cond_joins", None)

        has_p = len(fsm.parameters) > 0
        names = [p.name for p in fsm.parameters]
        mode_clip = t("animfsm_editor.cond_mode_clip_end")
        mode_param = t("animfsm_editor.cond_mode_parameter")
        clip_mode = (not cond.strip()) and len(terms) == 0

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
            pname = str(tm.get("name", names[0]))
            if pname not in names:
                pname = names[0]
            pi = names.index(pname)
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
            op = str(tm.get("op", ">"))
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
            fv = float(tm.get("value", 0.0))
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
                terms[i]["name"] = names[new_pi]
                self._apply_condition_model(lk, terms)
                return
            if new_oi != oi or new_fv != fv:
                terms[i]["op"] = _OPS[new_oi]
                terms[i]["value"] = float(new_fv)
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

    def _rename_parameter_in_fsm(self, old_name: str, new_name: str) -> None:
        """Rename a parameter in the authoritative transition expressions."""
        if not old_name or old_name == new_name or self._fsm is None:
            return
        for state in self._fsm.states:
            for transition in state.transitions:
                transition.condition = _replace_identifier_in_expr(
                    transition.condition,
                    old_name,
                    new_name,
                )

    def _render_variables_panel(self, ctx: InxGUIContext):
        """Left overlay: parameter list (selection only)."""
        fsm = self._fsm
        if fsm is None:
            return

        def _add_parameter() -> None:
            self._insert_parameter()

        render_workspace_add_header(
            ctx,
            t("animfsm_editor.section_parameters"),
            "##animfsm_parameter_add",
            on_add=_add_parameter,
            semantic_id="animfsm.parameters.add",
        )

        remove_id = ""
        kinds = ["bool", "float", "int"]
        for i, p in enumerate(fsm.parameters):
            selected = (
                p.stable_id
                == self._graph_selection.primary_id(GraphElementKind.PARAMETER)
            )
            clicked, rect = begin_workspace_entry(ctx, f"animfsm_param_{i}", selected)
            paint_workspace_entry(
                ctx,
                rect,
                primary=p.name,
                secondary=p.kind.capitalize(),
                dot_color=_FSM_PARAM_COLORS.get(p.kind, _FSM_PARAM_COLORS["float"]),
                selected=selected,
            )
            if clicked:
                self._graph_selection.select_one(
                    GraphElementKind.PARAMETER,
                    p.stable_id,
                    reason="animfsm_parameter_selection",
                    record_history=True,
                )
            if ctx.begin_popup_context_item(f"##animfsm_param_ctx_{i}"):
                if ctx.menu_item(t("particle_graph_editor.remove_parameter")):
                    remove_id = p.stable_id
                ctx.end_popup()
            finish_workspace_entry(ctx)
            ctx.record_semantic_item(
                "animfsm_parameter",
                p.name,
                True,
                f"animfsm.parameter.{i}",
                string_value=p.kind,
                bool_value=selected,
            )

        if remove_id:
            self._remove_parameter(remove_id)
            return

        selected_parameter = self._selected_parameter()
        if (
            selected_parameter is not None
            and ctx.is_window_focused(3)
            and not ctx.is_any_item_active()
            and ctx.is_key_pressed(KEY_DELETE)
        ):
            self._remove_parameter(selected_parameter.stable_id)

    def _render_parameter_detail_panel(self, ctx: InxGUIContext) -> bool:
        fsm = self._fsm
        if fsm is None:
            return False
        p = self._selected_parameter()
        if p is None:
            return False

        kinds = ["bool", "float", "int"]
        if p.kind not in kinds:
            raise RuntimeError(f"invalid animation parameter kind: {p.kind}")

        ctx.label(t("animfsm_editor.section_parameters"))
        ctx.separator()

        ctx.label(t("animfsm_editor.state_name"))
        ctx.set_next_item_width(-1)
        raw_name = ctx.text_input("##animfsm_param_name", p.name, 64)
        san = self._sanitize_param_identifier(raw_name)
        if san and san != p.name:
            after = p.to_dict()
            after["name"] = san
            self._update_parameter_document(
                p,
                after,
                "Rename parameter",
                merge_key=f"parameter:{p.stable_id}:name",
            )
            p = self._parameter_by_id(p.stable_id) or p

        ctx.label(t("particle_graph_editor.parameter_type"))
        ctx.set_next_item_width(-1)
        ki = kinds.index(p.kind) if p.kind in kinds else 1
        new_ki = ctx.combo("##animfsm_param_kind", ki, [k.capitalize() for k in kinds], len(kinds))
        if new_ki != ki:
            self._update_parameter_document(
                p,
                self._parameter_document_for_kind(p, kinds[new_ki]),
                "Change parameter type",
                merge_key=f"parameter:{p.stable_id}:kind",
            )
            p = self._parameter_by_id(p.stable_id) or p

        ctx.label(t("particle_graph_editor.parameter_default"))
        ctx.set_next_item_width(-1)
        if p.kind == "bool":
            nb = ctx.checkbox("##animfsm_param_def_bool", p.default_bool)
            if nb != p.default_bool:
                after = p.to_dict()
                after["default_bool"] = nb
                self._update_parameter_document(
                    p,
                    after,
                    "Parameter default",
                    merge_key=f"parameter:{p.stable_id}:default_bool",
                )
        elif p.kind == "float":
            nf = ctx.drag_float("##animfsm_param_def_float", p.default_float, 0.01, -1.0e9, 1.0e9)
            if nf != p.default_float:
                after = p.to_dict()
                after["default_float"] = nf
                self._update_parameter_document(
                    p,
                    after,
                    "Parameter default",
                    merge_key=f"parameter:{p.stable_id}:default_float",
                )
        else:
            ni = ctx.input_int("##animfsm_param_def_int", p.default_int)
            if ni != p.default_int:
                after = p.to_dict()
                after["default_int"] = ni
                self._update_parameter_document(
                    p,
                    after,
                    "Parameter default",
                    merge_key=f"parameter:{p.stable_id}:default_int",
                )
        return True

    # ── Detail panel (right side) ─────────────────────────────────────

    def _fsm_clip_asset_type(self) -> str:
        fsm = self._fsm
        mode = getattr(fsm, "mode", "2d") if fsm is not None else "2d"
        return "AnimationClip3D" if mode == "3d" else "AnimationClip"

    def _is_timeline_mode(self) -> bool:
        fsm = self._fsm
        return getattr(fsm, "mode", "2d") == "timeline" if fsm is not None else False

    def _clip_path_matches_fsm_mode(self, p: str) -> bool:
        """True if *p* is valid for current FSM mode: .animclip2d / .animclip3d file, or virtual ``::subanim:``."""
        p = (p or "").strip()
        if not p:
            return True
        expected_ext = ".animclip3d" if self._fsm_clip_asset_type() == "AnimationClip3D" else ".animclip2d"
        if os.path.splitext(p)[1].lower() == expected_ext:
            return True
        # FBX embedded take from Project panel — not a disk file, no file extension on the full path
        if expected_ext == ".animclip3d" and "::subanim:" in p:
            return True
        return False

    @staticmethod
    def _embedded_clip3d_picker_items(filter_text: str) -> List[Tuple[str, str]]:
        """List model-embedded takes alongside standalone ``.animclip3d`` assets.

        The Project panel exposes an embedded take as ``<model-guid>::subanim:<n>``.
        Returning that same public virtual reference keeps object-picker assignment,
        drag-and-drop assignment, and runtime loading on one contract.
        """
        from Infernux.core.asset_types import MESH_EXTENSIONS, read_meta_file, read_meta_guid
        from Infernux.core.assets import AssetManager

        filt = (filter_text or "").strip().lower()
        items: List[Tuple[str, str]] = []
        seen: set[str] = set()
        for ext in sorted(MESH_EXTENSIONS):
            for model_path in AssetManager.find_assets(f"*{ext}"):
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
                    items.append((display, f"{base}::subanim:{index}"))
        return items

    def _clip_picker_items(self, filter_text: str, extensions) -> List[Tuple[str, str]]:
        """Return compatible standalone clips and, for 3D FSMs, embedded takes."""
        result: List[Tuple[str, str]] = []
        for pattern in extensions:
            result.extend(_picker_assets(filter_text, pattern, assets_only=False))
        if self._fsm_clip_asset_type() == "AnimationClip3D":
            result.extend(self._embedded_clip3d_picker_items(filter_text))
        return result

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

    def _assign_clip_b_to_state(self, state: AnimState, clip_path: str, node=None, *, record_undo: bool = True):
        p = (clip_path or "").strip()
        if p and not self._clip_path_matches_fsm_mode(p):
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
        cfg = get_asset_type_config(self._fsm_clip_asset_type()) or {}
        type_hint = str(cfg.get("display", "AnimClip"))
        drag_type = cfg.get("drag_type", "ANIMCLIP_FILE")
        extensions = cfg.get("extensions", ("*.animclip2d", "*.animclip3d"))
        prefix = str(cfg.get("prefix", "aclip"))
        ref = self._clip_b_ref_for_state(state)
        display = self._clip_b_display_name(state, ref)

        def _picker(filt: str):
            return self._clip_picker_items(filt, extensions)

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
        render_object_field(
            ctx,
            f"{prefix}_fsm_clipb_{node.uid}",
            display,
            type_hint,
            accept_drag_type=drag_type,
            on_drop_callback=lambda p, _st=state, _nd=node: self._assign_clip_b_to_state(_st, str(p), _nd),
            picker_asset_items=_picker,
            on_pick=lambda path, _st=state, _nd=node: self._assign_clip_b_to_state(_st, path, _nd),
            on_clear=lambda _st=state, _nd=node: self._clear_clip_b_from_state(_st, _nd),
            ping_path=clip_b_ping or None,
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
        cfg = get_asset_type_config(self._fsm_clip_asset_type()) or {}
        type_hint = str(cfg.get("display", "AnimClip"))
        drag_type = cfg.get("drag_type", "ANIMCLIP_FILE")
        extensions = cfg.get("extensions", ("*.animclip2d", "*.animclip3d"))
        prefix = str(cfg.get("prefix", "aclip"))

        ref = self._clip_ref_for_state(state)
        display = self._clip_display_name(state, ref)

        def _picker(filt: str):
            return self._clip_picker_items(filt, extensions)

        def _on_pick(path: str, _st=state, _nd=node):
            self._assign_clip_to_state(_st, path, _nd)

        def _on_clear(_st=state, _nd=node):
            self._clear_clip_from_state(_st, _nd)

        field_label(ctx, label or t("animfsm_editor.clip_ref"), lw)
        clip_ping = self._resolved_clip_path_for_state(state)
        render_object_field(
            ctx,
            f"{prefix}_fsm_clip_{node.uid}",
            display,
            type_hint,
            accept_drag_type=drag_type,
            on_drop_callback=lambda p, _st=state, _nd=node: self._assign_clip_to_state(
                _st, str(p), _nd,
            ),
            picker_asset_items=_picker,
            on_pick=_on_pick,
            on_clear=_on_clear,
            ping_path=clip_ping or None,
            semantic_id=semantic_id,
        )

    def _render_detail_panel(self, ctx: InxGUIContext):
        fsm = self._fsm
        if fsm is None:
            return

        if self._render_selected_transition_detail(ctx):
            return

        if self._render_parameter_detail_panel(ctx):
            return

        node = self._graph.find_node(
            self._graph_selection.primary_id(GraphElementKind.NODE)
        )
        if node is None or node.type_id != "anim_state":
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

    def _sync_graph_from_fsm(self):
        """Rebuild the NodeGraph from the current AnimStateMachine."""
        self._view.reset_interaction_state()
        self._graph.clear()
        self._name_to_uid.clear()
        self._uid_to_name.clear()
        self._entry_uid = ""

        fsm = self._fsm
        if fsm is None:
            return

        # Create entry node
        entry = self._graph.add_node(
            "anim_entry",
            canvas_x=-100,
            canvas_y=50,
            uid=_FSM_ENTRY_NODE_ID,
        )
        entry.data["label"] = "Entry"
        self._entry_uid = entry.uid

        # Create state nodes
        y_offset = 0.0
        for state in fsm.states:
            px, py = state.position[0], state.position[1]
            if px == 0.0 and py == 0.0:
                px = 100.0
                py = y_offset
                y_offset += 80.0
            node = self._graph.add_node(
                "anim_state",
                canvas_x=px,
                canvas_y=py,
                uid=state.stable_id,
            )
            node.data["label"] = state.name
            node.data["loop"] = state.loop
            node.data["restart_same_clip"] = state.restart_same_clip
            if isinstance(state.header_color, (list, tuple)) and len(state.header_color) >= 3:
                try:
                    node.data["header_color"] = (
                        float(state.header_color[0]),
                        float(state.header_color[1]),
                        float(state.header_color[2]),
                        float(state.header_color[3]) if len(state.header_color) >= 4 else 1.0,
                    )
                except (TypeError, ValueError):
                    pass
            self._name_to_uid[state.name] = node.uid
            self._uid_to_name[node.uid] = state.name

        # Entry → default state link
        self._update_entry_link()

        # Create transition links
        for state in fsm.states:
            src_uid = self._name_to_uid.get(state.name, "")
            if not src_uid:
                continue
            for tr in state.transitions:
                dst_uid = self._name_to_uid.get(tr.target_state, "")
                if not dst_uid:
                    continue
                lk = self._graph.add_link(
                    src_uid,
                    "out",
                    dst_uid,
                    "in",
                    uid=tr.stable_id,
                )
                if lk:
                    lk.data["condition"] = tr.condition
                    lk.data["duration"] = float(getattr(tr, "duration", 0.0) or 0.0)
                    lk.data["cond_terms"] = parse_condition_string_to_model(tr.condition)
                    lk.data.pop("cond_joins", None)

    def _update_entry_link(self):
        """Ensure the entry node points to the current default state."""
        # Remove old entry links
        self._graph.links = [
            lk for lk in self._graph.links
            if lk.source_node != self._entry_uid
        ]
        fsm = self._fsm
        if fsm and fsm.default_state:
            dst_uid = self._name_to_uid.get(fsm.default_state, "")
            if dst_uid:
                self._graph.add_link(
                    self._entry_uid,
                    "out",
                    dst_uid,
                    "in",
                    uid=_FSM_ENTRY_LINK_ID,
                )

    def _unique_state_name(self, want: str) -> str:
        fsm = self._fsm
        if fsm is None:
            return want or "State"
        base = (want or "State").strip() or "State"
        if fsm.get_state(base) is None:
            return base
        n = fsm.state_count
        while True:
            cand = f"{base} {n}"
            if fsm.get_state(cand) is None:
                return cand
            n += 1

    def _on_graph_copy(self) -> None:
        fsm = self._fsm
        if fsm is None:
            return
        uids = [
            uid
            for uid in self._graph_selection.selected_ids(GraphElementKind.NODE)
            if uid != self._entry_uid
        ]
        names = [self._uid_to_name.get(u) for u in uids]
        names = [n for n in names if n]
        if not names:
            return
        name_set = set(names)
        items: List[ClipboardItem] = []
        for n in names:
            st = fsm.get_state(n)
            if not st:
                continue
            d = st.to_dict()
            d["transitions"] = [
                transition.to_dict()
                for transition in st.transitions
                if transition.target_state in name_set
            ]
            items.append(
                ClipboardItem(
                    st.stable_id,
                    self.document_id,
                    "animfsm_state",
                    d,
                )
            )
        if items:
            ClipboardService.instance().write(
                ClipboardDomain.GRAPH_ELEMENT,
                items,
                source_owner_id=self.window_id,
                reason="animfsm_copy_states",
            )

    def _on_graph_paste(self) -> None:
        payload = ClipboardService.instance().peek(ClipboardDomain.GRAPH_ELEMENT)
        fsm = self._fsm
        if payload is None or fsm is None:
            return
        state_documents = [
            copy.deepcopy(item.data)
            for item in payload.items
            if item.sub_kind == "animfsm_state" and isinstance(item.data, dict)
        ]
        if not state_documents:
            return
        old_names = [str(document.get("name") or "") for document in state_documents]
        if any(not name for name in old_names):
            return
        name_map: Dict[str, str] = {}
        for old in old_names:
            name_map[old] = self._unique_state_name(old)
        states_by_old_name: Dict[str, AnimState] = {}
        mutations: List[GraphMutation] = []
        selection: List[GraphElementRef] = []
        insertion_index = len(fsm.states)
        pending_transitions: List[Tuple[str, dict]] = []
        for sd in state_documents:
            old_name = sd["name"]
            new_n = name_map[old_name]
            state = AnimState.from_dict(sd)
            for transition in state.transitions:
                pending_transitions.append((old_name, transition.to_dict()))
            state.stable_id = uuid.uuid4().hex
            state.name = new_n
            state.transitions = []
            pos = list(sd.get("position", [0.0, 0.0]))
            if len(pos) < 2:
                pos = [0.0, 0.0]
            state.position = [float(pos[0]) + 48.0, float(pos[1]) + 48.0]
            states_by_old_name[sd["name"]] = state
            mutations.append(
                GraphMutation(
                    GraphMutationKind.INSERT,
                    GraphElementRef(GraphElementKind.NODE, state.stable_id),
                    after=state.to_dict(),
                    after_index=insertion_index,
                )
            )
            selection.append(GraphElementRef(GraphElementKind.NODE, state.stable_id))
            insertion_index += 1
        for source_name, tr_d in pending_transitions:
            source = states_by_old_name.get(source_name)
            if source is None:
                continue
            tr = AnimTransition.from_dict(tr_d)
            tr.stable_id = uuid.uuid4().hex
            tgt_old = tr.target_state
            tr.target_state = name_map.get(tgt_old, tgt_old)
            mutations.append(
                GraphMutation(
                    GraphMutationKind.INSERT,
                    GraphElementRef(GraphElementKind.LINK, tr.stable_id),
                    after={
                        "source_state_id": source.stable_id,
                        "transition": tr.to_dict(),
                    },
                    after_index=len(source.transitions),
                )
            )
            source.transitions.append(tr)
        self._execute_graph_mutations(
            "Paste states",
            tuple(mutations),
            selection_after=tuple(selection),
        )

    # ── Callbacks from NodeGraphView ──────────────────────────────────

    def _on_node_drag_start(self, uid: str) -> None:
        if uid == self._entry_uid:
            self._undo_drag_node_position = None
            return
        node = self._graph.find_node(uid)
        self._undo_drag_node_position = (
            (float(node.pos_x), float(node.pos_y)) if node is not None else None
        )

    def _on_node_drag_end(self, uid: str) -> None:
        if uid == self._entry_uid:
            self._undo_drag_node_position = None
            return
        start_position = self._undo_drag_node_position
        self._undo_drag_node_position = None
        node = self._graph.find_node(uid)
        if start_position is None or node is None:
            return
        end_position = (float(node.pos_x), float(node.pos_y))
        if end_position == start_position:
            return
        self._execute_graph_mutations(
            "Move state node",
            (
                GraphMutation(
                    GraphMutationKind.MOVE,
                    GraphElementRef(GraphElementKind.NODE, uid),
                    before={"position": list(start_position)},
                    after={"position": list(end_position)},
                ),
            ),
            merge_key=f"node:{uid}:position",
        )

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
        state_name = self._uid_to_name.get(node_uid, "")
        state = self._fsm.get_state(state_name) if self._fsm and state_name else None
        if state is None:
            return
        new_value = [float(component) for component in new_color]
        old_value = [float(component) for component in old_color]
        node.data["header_color"] = new_color
        state.header_color = new_value

        # During color picker drag we only preview/apply changes.
        # Record exactly one undo command when the popup closes.
        if not commit_undo:
            return
        if new_value == old_value:
            return
        # The picker previews directly in the live model. Restore its committed
        # value before executing the precise command so revision and history only
        # advance once when the edit session closes.
        state.header_color = old_value
        node.data["header_color"] = tuple(old_value)
        self._update_state_fields(
            state,
            "Change state header color",
            merge_key=f"state:{state.stable_id}:header_color",
            header_color=new_value,
        )

    def _on_link_created(self, src_node: str, src_pin: str, dst_node: str, dst_pin: str):
        """User created a connection by dragging between pins."""
        # Entry node connections change the default state
        if src_node == self._entry_uid:
            target_name = self._uid_to_name.get(dst_node, "")
            if target_name and self._fsm:
                self._set_default_state(target_name, "Set default state")
            return

        src_name = self._uid_to_name.get(src_node, "")
        dst_name = self._uid_to_name.get(dst_node, "")
        if not src_name or not dst_name or not self._fsm:
            return

        state = self._fsm.get_state(src_name)
        if state is None:
            return

        # Check for duplicate transition
        for tr in state.transitions:
            if tr.target_state == dst_name:
                return

        transition = AnimTransition(target_state=dst_name)
        self._insert_transition(state, transition, "Add transition")

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
        fsm = self._fsm
        if fsm is None:
            return
        target_name = self._uid_to_name.get(dst_node, "")
        if not target_name:
            return
        ref = GraphElementRef(GraphElementKind.LINK, link_uid)
        if link_uid == _FSM_ENTRY_LINK_ID:
            if src_node != self._entry_uid:
                return
            self._execute_graph_mutations(
                "Change default state",
                (
                    GraphMutation(
                        GraphMutationKind.UPDATE,
                        ref,
                        before={"default_state": fsm.default_state},
                        after={"default_state": target_name},
                    ),
                ),
                selection_after=(ref,),
            )
            return
        if src_node == self._entry_uid:
            return
        found = fsm.get_transition_by_id(link_uid)
        new_owner_name = self._uid_to_name.get(src_node, "")
        new_owner = fsm.get_state(new_owner_name)
        if found is None or new_owner is None:
            return
        old_owner, transition = found
        if any(
            other is not transition and other.target_state == target_name
            for other in new_owner.transitions
        ):
            return
        old_index = old_owner.transitions.index(transition)
        new_index = (
            old_index
            if old_owner is new_owner
            else len(new_owner.transitions)
        )
        replacement = AnimTransition.from_dict(transition.to_dict())
        replacement.target_state = target_name
        self._execute_graph_mutations(
            "Reconnect transition",
            (
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    ref,
                    before={
                        "source_state_id": old_owner.stable_id,
                        "transition": transition.to_dict(),
                    },
                    after={
                        "source_state_id": new_owner.stable_id,
                        "transition": replacement.to_dict(),
                    },
                    before_index=old_index,
                    after_index=new_index,
                ),
            ),
            selection_after=(ref,),
        )

    def _on_nodes_deleted(self, uids: List[str]):
        stable_ids = tuple(uid for uid in uids if uid != self._entry_uid)
        if stable_ids:
            self._remove_states(stable_ids)

    def _on_node_add_request(self, type_id: str, x: float, y: float):
        if type_id != "anim_state":
            return
        fsm = self._fsm
        if fsm is None:
            return
        state = AnimState(name=self._unique_state_name(f"State {fsm.state_count}"))
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
        fsm = self._fsm
        if fsm is None or not ctx_data:
            return
        gx = float(ctx_data.get("gx", 0.0))
        gy = float(ctx_data.get("gy", 0.0))
        src_node = str(ctx_data.get("source_node", ctx_data.get("src_node", "")))
        from_entry = (src_node == self._entry_uid)
        src_name = "" if from_entry else self._uid_to_name.get(src_node, "")

        base_name = "Timeline" if kind == "timeline" else "State"
        state = AnimState(name=self._unique_state_name(base_name))
        state.position = [gx, gy]
        if kind in ("blend", "timeline"):
            state.kind = kind
        mutations: List[GraphMutation] = [
            GraphMutation(
                GraphMutationKind.INSERT,
                GraphElementRef(GraphElementKind.NODE, state.stable_id),
                after=state.to_dict(),
                after_index=len(fsm.states),
            )
        ]
        if from_entry:
            mutations.append(
                GraphMutation(
                    GraphMutationKind.UPDATE,
                    GraphElementRef(GraphElementKind.LINK, _FSM_ENTRY_LINK_ID),
                    before={"default_state": fsm.default_state},
                    after={"default_state": state.name},
                )
            )
        elif src_name:
            src_state = fsm.get_state(src_name)
            if src_state is not None and not any(
                    tr.target_state == state.name for tr in src_state.transitions):
                transition = AnimTransition(target_state=state.name)
                mutations.append(
                    GraphMutation(
                        GraphMutationKind.INSERT,
                        GraphElementRef(
                            GraphElementKind.LINK, transition.stable_id
                        ),
                        after={
                            "source_state_id": src_state.stable_id,
                            "transition": transition.to_dict(),
                        },
                        after_index=len(src_state.transitions),
                    )
                )
        self._execute_graph_mutations(
            "Create node from link",
            tuple(mutations),
            selection_after=(
                GraphElementRef(GraphElementKind.NODE, state.stable_id),
            ),
        )

    def _on_canvas_selection_changed(
        self,
        node_ids: Tuple[str, ...],
        link_id: str,
        record_history: bool,
    ) -> None:
        self._graph_selection.accept_view_selection(
            node_ids,
            link_id,
            record_history=record_history,
        )

    def _on_file_selected(self, path):
        """EditorEvent.FILE_SELECTED — project panel selected a file."""
        if path:
            self._graph_selection.refresh()

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

    def _assign_timeline_to_state(self, state: AnimState, path: str, node=None, *, record_undo: bool = True):
        p = (path or "").strip()
        if p and not p.lower().endswith(".animtimeline"):
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

        def _picker(filt: str):
            return _picker_assets(filt, "*.animtimeline", assets_only=False)

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
        render_object_field(
            ctx,
            f"atl_fsm_tl_{node.uid}",
            display,
            "Timeline",
            accept_drag_type="ANIMTIMELINE_FILE",
            on_drop_callback=lambda p, _st=state, _nd=node: self._assign_timeline_to_state(_st, str(p), _nd),
            picker_asset_items=_picker,
            on_pick=lambda path, _st=state, _nd=node: self._assign_timeline_to_state(_st, path, _nd),
            on_clear=lambda _st=state, _nd=node: self._clear_timeline_from_state(_st, _nd),
            ping_path=tl_ping or None,
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

    def _assign_clip_to_state(self, state: AnimState, clip_path: str, node=None, *, record_undo: bool = True):
        """Assign a clip path/guid to a state."""
        p = (clip_path or "").strip()
        if p and not self._clip_path_matches_fsm_mode(p):
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

    def save(self, *, ticket, save_as: bool = False):
        from Infernux.engine.interaction import (
            DocumentActionResult,
            DocumentActionStatus,
        )

        target = self._file_path or (
            self._fsm.file_path if self._fsm is not None else ""
        )
        if save_as or not target:
            self._pending_save_ticket_id = ticket.ticket_id
            if self._show_save_as_dialog():
                return DocumentActionResult(DocumentActionStatus.PENDING)
            self._pending_save_ticket_id = ""
            return DocumentActionResult(
                DocumentActionStatus.REJECTED,
                "no project root is available",
            )
        return self._save_to(target, ticket_id=ticket.ticket_id)

    def discard(self, *, document_id: str) -> bool:
        if document_id != self.document_id:
            return False
        return self._discard_unsaved_changes()

    def handle_save_command(self, save_as: bool = False) -> bool:
        return self._request_document_save(save_as=save_as)

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
        ):
            Debug.log_warning("[AnimFSM] No project root set - cannot save state machine.")
            return False
        return True

    def _save_to(self, target: str, *, ticket_id: str = "") -> bool:
        fsm = self._fsm
        if fsm is None:
            return False
        if fsm.save(target):
            normalized = self._normalize_fsm_path(target)
            self._file_path = normalized
            fsm.file_path = normalized
            fsm.name = os.path.splitext(os.path.basename(normalized))[0]
            from Infernux.engine.interaction import DocumentRegistry

            registry = DocumentRegistry.instance()
            document = self._fsm_document()
            active_ticket_id = ticket_id or self._pending_save_ticket_id
            if active_ticket_id:
                registry.complete_save(
                    active_ticket_id,
                    success=True,
                    key=self._fsm_document_key(normalized),
                    resource_path=normalized,
                    title=fsm.name,
                )
                self._pending_save_ticket_id = ""
            elif document is not None:
                registry.rekey(
                    document.document_id,
                    self._fsm_document_key(normalized),
                    resource_path=normalized,
                )
                registry.update_metadata(document.document_id, title=fsm.name)
                registry.mark_saved(document.document_id)
            document = self._fsm_document()
            self._dirty = bool(document and document.is_dirty)
            Debug.log(f"Saved animfsm: {target}")
            self._hot_reload_animators(normalized)
            if self._mode_switch_waiting_for_save and self._pending_mode_switch:
                self._commit_mode_switch(self._pending_mode_switch)
            return True
        else:
            Debug.log_error(f"Failed to save animfsm: {target}")
            active_ticket_id = ticket_id or self._pending_save_ticket_id
            if active_ticket_id:
                from Infernux.engine.interaction import DocumentRegistry

                DocumentRegistry.instance().complete_save(
                    active_ticket_id,
                    success=False,
                    message=f"failed to save animation FSM: {target}",
                )
                self._pending_save_ticket_id = ""
            return False

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
        if self._mode_switch_waiting_for_save:
            self._cancel_pending_mode_switch()

    def _discard_unsaved_changes(self) -> bool:
        target = self._file_path or (self._fsm.file_path if self._fsm is not None else "")
        if target:
            self._open_animfsm(target)
            return not self._dirty
        self._fsm = AnimStateMachine(name="New State Machine")
        self._file_path = ""
        self._sync_graph_from_fsm()
        self._graph_selection.clear(record_history=False)
        from Infernux.engine.interaction import DocumentRegistry

        document = self._fsm_document()
        if document is not None:
            DocumentRegistry.instance().restore_content_revision(
                document.document_id, document.saved_revision
            )
        self._dirty = False
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
