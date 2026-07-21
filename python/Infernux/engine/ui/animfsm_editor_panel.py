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
from Infernux.lib import InxGUIContext

from .asset_save_dialog import AssetSaveAsDialog
from .editor_panel import EditorPanel
from .node_graph_view import NodeGraphView
from ._inspector_references import render_object_field, _picker_assets
from .inspector_utils import field_label, max_label_w
from .panel_registry import editor_panel
from .theme import ImGuiCol, Theme


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
    if hasattr(ast, "Num") and isinstance(node, ast.Num):
        return float(node.n)
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
        self._view.on_nodes_deleted = self._on_nodes_deleted
        self._view.on_node_add_request = self._on_node_add_request
        self._view.on_link_dropped_empty = self._on_link_dropped_empty
        self._view.on_node_selected = self._on_node_selected
        self._view.on_canvas_drop = self._on_canvas_drop
        self._view.on_node_drag_start = self._on_node_drag_start
        self._view.on_node_drag_end = self._on_node_drag_end
        self._view.on_node_header_color_changed = self._on_node_header_color_changed

        # Currently selected node uid
        self._selected_uid: str = ""

        # Maps: state name ↔ node uid
        self._name_to_uid: Dict[str, str] = {}
        self._uid_to_name: Dict[str, str] = {}

        # Entry node uid
        self._entry_uid: str = ""

        # Guard to avoid re-entrant selection clearing
        self._clearing_selection: bool = False

        # Drag-to-create: pending context + popup search filter
        self._link_create_ctx: Optional[dict] = None
        self._open_link_create_popup: bool = False
        self._link_create_search: str = ""

        # Panel persistence: ``load_state`` may run from bootstrap or first render
        self._panel_state_restored_once: bool = False
        self._panel_restore_data: Optional[dict] = None
        self._undo_drag_snapshot: Optional[dict] = None
        self._undo_drag_node_position: Optional[Tuple[float, float]] = None
        self._clipboard_fsm_nodes: Optional[dict] = None

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
        self._selected_uid = ""
        self._dirty = False
        for state in fsm.states:
            if not state.clip_guid and state.clip_path:
                state.clip_guid = self._resolve_guid(state.clip_path)
            if state.clip_guid:
                state.clip_path = ""
        self._sync_graph_from_fsm()

    def _new_fsm(self):
        """Create a blank FSM for editing."""
        self._fsm = AnimStateMachine(name="New State Machine")
        self._file_path = ""
        self._selected_uid = ""
        self._dirty = True
        self._sync_graph_from_fsm()

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
        from Infernux.engine.ui.selection_manager import SelectionManager
        from .event_bus import EditorEventBus, EditorEvent
        SelectionManager.instance().add_listener(self._on_global_selection_changed)
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
        from Infernux.engine.ui.selection_manager import SelectionManager
        from .event_bus import EditorEventBus, EditorEvent
        SelectionManager.instance().remove_listener(self._on_global_selection_changed)
        bus = EditorEventBus.instance()
        bus.unsubscribe(EditorEvent.FILE_SELECTED, self._on_file_selected)
        try:
            from Infernux.engine.play_mode import PlayModeManager
            pmm = PlayModeManager.instance()
            if pmm:
                pmm.remove_state_change_listener(self._on_play_mode_changed)
        except Exception:
            pass
        try:
            from Infernux.engine.project_context import set_panel_dirty

            set_panel_dirty(self.window_id, False)
        except Exception:
            pass

    def _window_title_suffix(self) -> str:
        self._sync_project_dirty_flag()
        return " *" if self._dirty else ""

    def _sync_project_dirty_flag(self) -> None:
        try:
            from Infernux.engine.project_context import set_panel_dirty

            set_panel_dirty(self.window_id, bool(self._dirty))
        except Exception:
            pass

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
            self._selected_uid = ""
            self._sync_graph_from_fsm()
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

    # ── Undo (AnimStateMachine snapshots; edit mode only) ─────────────

    def _link_key_for_undo(self) -> Optional[Tuple[str, str]]:
        lid = (self._view.selected_link or "").strip()
        if not lid or not self._graph:
            return None
        lk = self._graph.find_link(lid)
        if lk is None:
            return None
        sn = self._uid_to_name.get(lk.source_node, "")
        dn = self._uid_to_name.get(lk.target_node, "")
        return (sn, dn) if sn and dn else None

    def _undo_snapshot(self) -> dict:
        """FSM data + graph selection (by state name so it survives graph rebuild)."""
        fsm_data: dict = {}
        if self._fsm is not None:
            fsm_data = copy.deepcopy(self._fsm.to_dict())
        selected_key = ""
        if self._selected_uid:
            if self._selected_uid == self._entry_uid:
                selected_key = "__entry__"
            else:
                selected_key = self._uid_to_name.get(self._selected_uid, "")
        return {
            "fsm": fsm_data,
            "ui": {
                "selected_state": selected_key,
                "link_key": self._link_key_for_undo(),
            },
        }

    def _apply_undo_snapshot(self, snap: dict) -> None:
        if not isinstance(snap, dict):
            return
        if "fsm" in snap:
            fsm_data = snap.get("fsm") or {}
            ui = snap.get("ui") or {}
        else:
            fsm_data = snap
            ui = {}
        if not fsm_data:
            self._new_fsm()
        else:
            self._fsm = AnimStateMachine.from_dict(fsm_data)
            self._sync_graph_from_fsm()
        self._dirty = True
        sel = (ui.get("selected_state") or "").strip() if isinstance(ui, dict) else ""
        if sel == "__entry__" and self._entry_uid:
            uid = self._entry_uid
            self._selected_uid = uid
            self._view.selected_nodes = [uid]
        elif sel and sel in self._name_to_uid:
            uid = self._name_to_uid[sel]
            self._selected_uid = uid
            self._view.selected_nodes = [uid]
        else:
            self._selected_uid = ""
            self._view.selected_nodes = []
        self._view.selected_link = ""
        lk_key = ui.get("link_key") if isinstance(ui, dict) else None
        if isinstance(lk_key, (list, tuple)) and len(lk_key) == 2:
            sn, dn = str(lk_key[0]), str(lk_key[1])
            if sn in self._name_to_uid and dn in self._name_to_uid:
                su = self._name_to_uid[sn]
                du = self._name_to_uid[dn]
                for lk in self._graph.links:
                    if lk.source_node == su and lk.target_node == du:
                        self._view.selected_link = lk.uid
                        break
        self._sync_project_dirty_flag()

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

    def _try_record_undo(self, description: str, before: dict, after: dict) -> None:
        from Infernux.engine.undo import record_node_graph_snapshot

        record_node_graph_snapshot(
            description=description,
            before_snapshot=before,
            after_snapshot=after,
            apply_snapshot=self._apply_undo_snapshot,
        )

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

        vars_w = min(_VARS_PANEL_W, max(140.0, avail_w * 0.22))
        detail_w = min(_DETAIL_PANEL_W, max(200.0, avail_w * 0.28))
        graph_w = max(120.0, avail_w - vars_w - detail_w - 8.0)

        # Left: declared parameters (for transition conditions)
        if ctx.begin_child("##fsm_vars_region", vars_w, avail_h, True):
            self._render_variables_panel(ctx)
        ctx.end_child()

        ctx.same_line()

        # Center: graph canvas
        if ctx.begin_child("##fsm_graph_region", graph_w, avail_h, False):
            self._view.render(ctx)
        ctx.end_child()

        ctx.same_line()

        # Right: state detail
        if ctx.begin_child("##fsm_detail_region", detail_w, avail_h, True):
            self._render_detail_panel(ctx)
        ctx.end_child()

        # Drag-to-create: searchable "create node and connect" menu
        self._draw_link_create_popup(ctx)

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
            self._execute_save,
            cancel_callback=self._cancel_pending_mode_switch,
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
            before = self._undo_snapshot()
            self._new_fsm()
            self._try_record_undo("New state machine", before, self._undo_snapshot())
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
            before = self._undo_snapshot()
            fsm.name = new_name
            self._dirty = True
            self._try_record_undo("Rename FSM", before, self._undo_snapshot())

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
        before = self._undo_snapshot()
        lk.data["condition"] = cond
        self._sync_transition_condition(lk)
        self._dirty = True
        self._try_record_undo("Edit transition condition", before, self._undo_snapshot())

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
            before = self._undo_snapshot()
            lk.data["duration"] = new_dur
            self._sync_transition_condition(lk)
            self._dirty = True
            self._try_record_undo("Edit transition duration", before, self._undo_snapshot())
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
        add_clicked = ctx.button("+##addrow", None, 28, 20)
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
        remove_clicked = can_remove and ctx.button("−##rmrow", None, 28, 20)
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
        """Rename a parameter in all transition expressions and graph link data."""
        if not old_name or old_name == new_name or self._fsm is None:
            return
        for lk in self._graph.links:
            ct = lk.data.get("cond_terms")
            if isinstance(ct, list) and ct:
                changed = False
                for t in ct:
                    if isinstance(t, dict) and t.get("name") == old_name:
                        t["name"] = new_name
                        changed = True
                if changed:
                    lk.data["cond_terms"] = ct
                    lk.data["condition"] = _encode_condition_model(ct)
                    self._sync_transition_condition(lk)
            elif lk.data.get("condition"):
                lk.data["condition"] = _replace_identifier_in_expr(
                    str(lk.data["condition"]), old_name, new_name
                )
                self._sync_transition_condition(lk)

    def _render_variables_panel(self, ctx: InxGUIContext):
        """Left rail: parameters usable in transition condition expressions."""
        fsm = self._fsm
        if fsm is None:
            return

        ctx.push_style_color(ImGuiCol.Text, 0.55, 0.56, 0.58, 1.0)
        ctx.label(t("animfsm_editor.section_parameters"))
        ctx.pop_style_color(1)
        ctx.separator()
        ctx.dummy(0, 4)

        add_parameter_label = t("animfsm_editor.add_parameter")
        add_parameter_pressed = ctx.button(add_parameter_label)
        ctx.record_semantic_item("button", add_parameter_label, True, "animfsm.parameters.add")
        if add_parameter_pressed:
            before = self._undo_snapshot()
            fsm.parameters.append(AnimParameter(name=f"var_{len(fsm.parameters)}", kind="float"))
            self._dirty = True
            self._try_record_undo("Add parameter", before, self._undo_snapshot())

        ctx.dummy(0, 4)
        remove_idx = -1
        kinds = ["bool", "float", "int"]
        for i, p in enumerate(fsm.parameters):
            if p.kind not in kinds:
                before = self._undo_snapshot()
                p.kind = "float"
                self._dirty = True
                self._try_record_undo("Fix parameter type", before, self._undo_snapshot())
            ctx.push_id(i)
            row_w = ctx.get_content_region_avail_width()
            COMBO_W = 72.0
            GAP = 8.0
            DEL_W = 22.0
            name_w = max(48.0, row_w - COMBO_W - GAP - DEL_W - GAP)

            ctx.set_next_item_width(COMBO_W)
            ki = kinds.index(p.kind) if p.kind in kinds else 1
            new_ki = ctx.combo("##pk", ki, [k.capitalize() for k in kinds], len(kinds))
            if new_ki != ki:
                before = self._undo_snapshot()
                p.kind = kinds[new_ki]
                self._dirty = True
                self._try_record_undo("Change parameter type", before, self._undo_snapshot())

            ctx.same_line(0, GAP)
            ctx.set_next_item_width(name_w)
            raw_name = ctx.text_input("##pname", p.name, 64)
            san = self._sanitize_param_identifier(raw_name)
            if san and san != p.name:
                before = self._undo_snapshot()
                self._rename_parameter_in_fsm(p.name, san)
                p.name = san
                self._dirty = True
                self._try_record_undo("Rename parameter", before, self._undo_snapshot())

            ctx.same_line(0, GAP)
            if ctx.button("−##prm_del", width=DEL_W, height=20):
                remove_idx = i

            ctx.set_next_item_width(-1)
            if p.kind == "bool":
                nb = ctx.checkbox("##pdef_bool", p.default_bool)
                if nb != p.default_bool:
                    before = self._undo_snapshot()
                    p.default_bool = nb
                    self._dirty = True
                    self._try_record_undo("Parameter default", before, self._undo_snapshot())
            elif p.kind == "float":
                nf = ctx.drag_float("##pdef_float", p.default_float, 0.01, -1.0e9, 1.0e9)
                if nf != p.default_float:
                    before = self._undo_snapshot()
                    p.default_float = nf
                    self._dirty = True
                    self._try_record_undo("Parameter default", before, self._undo_snapshot())
            else:
                ni = ctx.input_int("##pdef_int", p.default_int)
                if ni != p.default_int:
                    before = self._undo_snapshot()
                    p.default_int = ni
                    self._dirty = True
                    self._try_record_undo("Parameter default", before, self._undo_snapshot())

            ctx.dummy(0, 6)
            ctx.pop_id()

        if 0 <= remove_idx < len(fsm.parameters):
            before = self._undo_snapshot()
            fsm.parameters.pop(remove_idx)
            self._dirty = True
            self._try_record_undo("Remove parameter", before, self._undo_snapshot())

        # When a blend node is selected, surface its Lerp here too (mirrors the
        # detail panel) so the blend factor sits alongside the parameter list.
        node = self._graph.find_node(self._selected_uid)
        sel_state = None
        if node is not None and node.type_id == "anim_state":
            sel_state = fsm.get_state(self._uid_to_name.get(node.uid, ""))
        if sel_state is not None and getattr(sel_state, "kind", "clip") == "blend":
            ctx.dummy(0, 8)
            ctx.separator()
            ctx.push_style_color(ImGuiCol.Text, 0.55, 0.56, 0.58, 1.0)
            ctx.label(f"{sel_state.name} · {t('animfsm_editor.blend_lerp')}")
            ctx.pop_style_color(1)
            ctx.set_next_item_width(-1)
            cur = float(getattr(sel_state, "blend_value", 0.5) or 0.0)
            nv = max(0.0, min(1.0, ctx.drag_float("##sel_blend_lerp", cur, 0.005, 0.0, 1.0)))
            if nv != cur:
                before = self._undo_snapshot()
                sel_state.blend_value = nv
                self._dirty = True
                self._try_record_undo("Blend lerp", before, self._undo_snapshot())

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
        before = self._undo_snapshot() if record_undo else None
        p = (clip_path or "").strip()
        if p and not self._clip_path_matches_fsm_mode(p):
            return
        state.clip_b_guid = self._resolve_guid(p) if p else ""
        state.clip_b_path = "" if state.clip_b_guid else (p or "")
        self._clip_name_cache = {}
        self._dirty = True
        if record_undo and before is not None:
            self._try_record_undo("Assign blend clip B", before, self._undo_snapshot())

    def _clear_clip_b_from_state(self, state: AnimState, node=None, *, record_undo: bool = True):
        before = self._undo_snapshot() if record_undo else None
        state.clip_b_guid = ""
        state.clip_b_path = ""
        self._clip_name_cache = {}
        self._dirty = True
        if record_undo and before is not None:
            self._try_record_undo("Clear blend clip B", before, self._undo_snapshot())

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
            semantic_id=semantic_id,
        )

    def _render_detail_panel(self, ctx: InxGUIContext):
        fsm = self._fsm
        if fsm is None:
            return

        if self._render_selected_transition_detail(ctx):
            return

        node = self._graph.find_node(self._selected_uid)
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
                before = self._undo_snapshot()
                fsm.default_state = state.name
                self._update_entry_link()
                self._dirty = True
                self._try_record_undo("Set default state", before, self._undo_snapshot())
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
            before = self._undo_snapshot()
            if self._try_rename_state(state, new_name.strip()):
                self._dirty = True
                self._try_record_undo("Rename state", before, self._undo_snapshot())

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
                before = self._undo_snapshot()
                state.blend_value = max(0.0, min(1.0, float(new_lerp)))
                self._dirty = True
                self._try_record_undo("Change blend lerp", before, self._undo_snapshot())
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
            before = self._undo_snapshot()
            state.speed = new_speed
            self._dirty = True
            self._try_record_undo("Change playback speed", before, self._undo_snapshot())

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
            before = self._undo_snapshot()
            state.exit_time_normalized = max(0.0, min(1.0, new_exit_pct / 100.0))
            self._dirty = True
            self._try_record_undo("Change exit time", before, self._undo_snapshot())

        new_loop = self._detail_checkbox_row_right(
            ctx, lw, "animfsm_editor.loop", "##state_loop", state.loop,
            "animfsm.state.loop",
        )
        if new_loop != state.loop:
            before = self._undo_snapshot()
            state.loop = new_loop
            node.data["loop"] = state.loop
            self._dirty = True
            self._try_record_undo("Toggle loop", before, self._undo_snapshot())

        new_rs = self._detail_checkbox_row_right(
            ctx, lw, "animfsm_editor.restart_same_clip", "##state_restart_same", state.restart_same_clip,
            "animfsm.state.restart_same_clip",
        )
        if new_rs != state.restart_same_clip:
            before = self._undo_snapshot()
            state.restart_same_clip = new_rs
            node.data["restart_same_clip"] = state.restart_same_clip
            self._dirty = True
            self._try_record_undo("Toggle restart same clip", before, self._undo_snapshot())

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
            if ctx.button("×##del", None, 24, 20):
                remove_link_uid = lk.uid

            self._render_transition_condition_block(ctx, lk)
            ctx.end_group()
            ctx.dummy(0, 6)
            ctx.pop_id()

        if remove_link_uid:
            before = self._undo_snapshot()
            if self._remove_link_and_transition(remove_link_uid):
                self._dirty = True
                self._try_record_undo("Remove transition", before, self._undo_snapshot())

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
            before = self._undo_snapshot()
            state.exit_time_normalized = max(0.0, min(1.0, new_exit_pct / 100.0))
            self._dirty = True
            self._try_record_undo("Change transition exit time", before, self._undo_snapshot())
        ctx.dummy(0, 4)

    def _render_selected_transition_detail(self, ctx: InxGUIContext) -> bool:
        link_uid = (self._view.selected_link or "").strip()
        if not link_uid:
            return False
        lk = self._graph.find_link(link_uid)
        if lk is None:
            self._view.selected_link = ""
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
            before = self._undo_snapshot()
            if self._remove_link_and_transition(link_uid):
                self._view.selected_link = ""
                self._dirty = True
                self._try_record_undo("Remove transition", before, self._undo_snapshot())
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
        entry = self._graph.add_node("anim_entry", x=-100, y=50)
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
            node = self._graph.add_node("anim_state", x=px, y=py)
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
                lk = self._graph.add_link(src_uid, "out", dst_uid, "in")
                if lk:
                    lk.data["condition"] = tr.condition
                    lk.data["duration"] = float(getattr(tr, "duration", 0.0) or 0.0)
                    lk.data["cond_terms"] = parse_condition_string_to_model(tr.condition)
                    lk.data.pop("cond_joins", None)

    def _sync_fsm_positions(self):
        """Write node visual data (position/color) back to FSM states."""
        fsm = self._fsm
        if fsm is None:
            return
        for state in fsm.states:
            uid = self._name_to_uid.get(state.name, "")
            node = self._graph.find_node(uid) if uid else None
            if node:
                state.position = [node.pos_x, node.pos_y]
                raw = node.data.get("header_color", [])
                if isinstance(raw, (list, tuple)) and len(raw) >= 3:
                    try:
                        state.header_color = [
                            float(raw[0]),
                            float(raw[1]),
                            float(raw[2]),
                            float(raw[3]) if len(raw) >= 4 else 1.0,
                        ]
                    except (TypeError, ValueError):
                        state.header_color = []
                else:
                    state.header_color = []

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
                self._graph.add_link(self._entry_uid, "out", dst_uid, "in")

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
        uids = [u for u in self._view.selected_nodes if u and u != self._entry_uid]
        names = [self._uid_to_name.get(u) for u in uids]
        names = [n for n in names if n]
        if not names:
            return
        name_set = set(names)
        states_copy: List[dict] = []
        for n in names:
            st = fsm.get_state(n)
            if not st:
                continue
            d = st.to_dict()
            d["transitions"] = []
            states_copy.append(copy.deepcopy(d))
        transitions: List[Tuple[str, dict]] = []
        for n in names:
            st = fsm.get_state(n)
            if not st:
                continue
            for tr in st.transitions:
                if tr.target_state in name_set:
                    transitions.append((n, tr.to_dict()))
        self._clipboard_fsm_nodes = {"states": states_copy, "transitions": transitions}

    def _on_graph_paste(self) -> None:
        blob = self._clipboard_fsm_nodes
        fsm = self._fsm
        if not blob or not fsm or not blob.get("states"):
            return
        before = self._undo_snapshot()
        old_names = [s["name"] for s in blob["states"]]
        name_map: Dict[str, str] = {}
        for old in old_names:
            name_map[old] = self._unique_state_name(old)
        for sd in blob["states"]:
            new_n = name_map[sd["name"]]
            state = AnimState.from_dict(sd)
            state.name = new_n
            state.transitions = []
            pos = list(sd.get("position", [0.0, 0.0]))
            if len(pos) < 2:
                pos = [0.0, 0.0]
            state.position = [float(pos[0]) + 48.0, float(pos[1]) + 48.0]
            fsm.states.append(state)
        for src_old, tr_d in blob.get("transitions", []):
            src_new = name_map.get(src_old)
            if not src_new:
                continue
            tr = AnimTransition.from_dict(tr_d)
            tgt_old = tr.target_state
            tr.target_state = name_map.get(tgt_old, tgt_old)
            st = fsm.get_state(src_new)
            if st:
                st.transitions.append(tr)
        self._sync_graph_from_fsm()
        new_uids: List[str] = []
        for o in old_names:
            nn = name_map.get(o)
            if nn:
                uid = self._name_to_uid.get(nn)
                if uid:
                    new_uids.append(uid)
        if new_uids:
            self._view.selected_nodes = new_uids
            self._selected_uid = new_uids[0]
        self._dirty = True
        self._try_record_undo("Paste states", before, self._undo_snapshot())

    # ── Callbacks from NodeGraphView ──────────────────────────────────

    def _on_node_drag_start(self, uid: str) -> None:
        if uid == self._entry_uid:
            self._undo_drag_snapshot = None
            self._undo_drag_node_position = None
            return
        self._undo_drag_snapshot = self._undo_snapshot()
        node = self._graph.find_node(uid)
        self._undo_drag_node_position = (
            (float(node.pos_x), float(node.pos_y)) if node is not None else None
        )

    def _on_node_drag_end(self, uid: str) -> None:
        if uid == self._entry_uid:
            self._undo_drag_snapshot = None
            self._undo_drag_node_position = None
            return
        before = self._undo_drag_snapshot
        start_position = self._undo_drag_node_position
        self._undo_drag_snapshot = None
        self._undo_drag_node_position = None
        node = self._graph.find_node(uid)
        if before is None or start_position is None or node is None:
            return
        end_position = (float(node.pos_x), float(node.pos_y))
        if end_position == start_position:
            return
        self._sync_fsm_positions()
        after = self._undo_snapshot()
        if before == after:
            return
        self._dirty = True
        self._sync_project_dirty_flag()
        self._try_record_undo("Move state node", before, after)

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
        node.data["header_color"] = new_color

        state_name = self._uid_to_name.get(node_uid, "")
        if self._fsm is not None and state_name:
            state = self._fsm.get_state(state_name)
            if state is not None:
                state.header_color = [
                    float(new_color[0]),
                    float(new_color[1]),
                    float(new_color[2]),
                    float(new_color[3]),
                ]

        self._dirty = True
        self._sync_project_dirty_flag()

        # During color picker drag we only preview/apply changes.
        # Record exactly one undo command when the popup closes.
        if not commit_undo:
            return

        after = self._undo_snapshot()
        before = copy.deepcopy(after)
        fsm = before.get("fsm") or {}
        states = fsm.get("states") if isinstance(fsm, dict) else None
        if isinstance(states, list):
            for s in states:
                if str(s.get("name", "")) == state_name:
                    s["header_color"] = [
                        float(old_color[0]),
                        float(old_color[1]),
                        float(old_color[2]),
                        float(old_color[3]),
                    ]
                    break
        self._try_record_undo("Change state header color", before, after)

    def _on_link_created(self, src_node: str, src_pin: str, dst_node: str, dst_pin: str):
        """User created a connection by dragging between pins."""
        # Entry node connections change the default state
        if src_node == self._entry_uid:
            target_name = self._uid_to_name.get(dst_node, "")
            if target_name and self._fsm:
                before = self._undo_snapshot()
                self._fsm.default_state = target_name
                self._update_entry_link()
                self._dirty = True
                self._try_record_undo("Set default state", before, self._undo_snapshot())
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

        before = self._undo_snapshot()
        state.transitions.append(AnimTransition(target_state=dst_name))
        lk = self._graph.add_link(src_node, src_pin, dst_node, dst_pin)
        if lk:
            lk.data["cond_terms"] = []
            lk.data.pop("cond_joins", None)
        self._dirty = True
        self._try_record_undo("Add transition", before, self._undo_snapshot())

    def _on_link_deleted(self, link_uid: str):
        before = self._undo_snapshot()
        if self._remove_link_and_transition(link_uid):
            self._dirty = True
            self._try_record_undo("Remove transition", before, self._undo_snapshot())

    def _on_nodes_deleted(self, uids: List[str]):
        fsm = self._fsm
        if fsm is None:
            return
        before = self._undo_snapshot()
        changed = False
        for uid in uids:
            # Don't delete entry node
            if uid == self._entry_uid:
                continue
            changed = True
            name = self._uid_to_name.get(uid, "")
            if name:
                fsm.remove_state(name)
                del self._name_to_uid[name]
                del self._uid_to_name[uid]
            self._graph.remove_node(uid)
        if self._selected_uid in uids:
            self._selected_uid = ""
        if changed:
            self._update_entry_link()
            self._dirty = True
            self._try_record_undo("Delete state", before, self._undo_snapshot())

    def _on_node_add_request(self, type_id: str, x: float, y: float):
        if type_id != "anim_state":
            return
        fsm = self._fsm
        if fsm is None:
            return
        before = self._undo_snapshot()
        state = fsm.add_state()
        state.position = [x, y]
        if self._is_timeline_mode():
            state.kind = "timeline"
        node = self._graph.add_node("anim_state", x=x, y=y)
        node.data["label"] = state.name
        node.data["loop"] = state.loop
        node.data["restart_same_clip"] = state.restart_same_clip
        self._name_to_uid[state.name] = node.uid
        self._uid_to_name[node.uid] = state.name
        self._view.selected_nodes = [node.uid]
        self._selected_uid = node.uid
        self._update_entry_link()
        self._dirty = True
        self._try_record_undo("Add state", before, self._undo_snapshot())

    # ── Drag-to-create ────────────────────────────────────────────────
    def _on_link_dropped_empty(self, src_node: str, src_pin: str, src_kind, gx: float, gy: float):
        """Pin drag released over empty canvas → open a create-and-connect menu."""
        if self._fsm is None:
            return
        # Only outgoing pins start a forward transition into a new node.
        if src_kind != PinKind.OUTPUT:
            return
        self._link_create_ctx = {"src_node": src_node, "gx": float(gx), "gy": float(gy)}
        self._link_create_search = ""
        self._link_create_focus = True
        self._open_link_create_popup = True

    def _draw_link_create_popup(self, ctx: InxGUIContext):
        popup_id = "##fsm_link_create"
        if self._open_link_create_popup:
            ctx.open_popup(popup_id)
            self._open_link_create_popup = False
        if self._link_create_ctx is None:
            return
        created = None
        if ctx.begin_popup(popup_id):
            ctx.label(t("animfsm_editor.create_node_title"))
            ctx.separator()
            if getattr(self, "_link_create_focus", False):
                ctx.set_keyboard_focus_here()
                self._link_create_focus = False
            self._link_create_search = ctx.input_text_with_hint(
                "##fsm_link_create_search", t("animfsm_editor.create_search"),
                self._link_create_search, 128)
            flt = (self._link_create_search or "").strip().lower()
            if self._is_timeline_mode():
                options = (
                    (t("animfsm_editor.node_kind_timeline"), "timeline"),
                )
            else:
                options = (
                    (t("animfsm_editor.node_kind_clip"), "clip"),
                    (t("animfsm_editor.node_kind_blend"), "blend"),
                )
            for idx, (label, kind) in enumerate(options):
                if flt and flt not in label.lower() and flt not in kind:
                    continue
                ctx.push_id(idx)
                if ctx.selectable(label, False):
                    created = (kind, self._link_create_ctx)
                    self._link_create_ctx = None
                    ctx.close_current_popup()
                ctx.pop_id()
            ctx.end_popup()
        else:
            self._link_create_ctx = None
        # Defer the model mutation + graph rebuild until after the popup is fully
        # ended, so we never touch the graph while inside ImGui popup scope.
        if created is not None:
            self._create_state_from_link(created[0], created[1])

    def _create_state_from_link(self, kind: str, ctx_data: dict):
        """Create a new state at the drop point and connect it from the drag source."""
        fsm = self._fsm
        if fsm is None or not ctx_data:
            return
        gx = float(ctx_data.get("gx", 0.0))
        gy = float(ctx_data.get("gy", 0.0))
        src_node = str(ctx_data.get("src_node", ""))
        # Resolve the source state name *before* rebuilding (uids change on rebuild).
        from_entry = (src_node == self._entry_uid)
        src_name = "" if from_entry else self._uid_to_name.get(src_node, "")

        before = self._undo_snapshot()
        # Persist current node positions so the rebuild doesn't reset them.
        self._sync_fsm_positions()

        base_name = "Timeline" if kind == "timeline" else "State"
        state = fsm.add_state(self._unique_state_name(base_name))
        state.position = [gx, gy]
        if kind in ("blend", "timeline"):
            state.kind = kind

        if from_entry:
            fsm.default_state = state.name
        elif src_name:
            src_state = fsm.get_state(src_name)
            if src_state is not None and not any(
                    tr.target_state == state.name for tr in src_state.transitions):
                src_state.transitions.append(AnimTransition(target_state=state.name))

        # Rebuild graph from the model — same code path as loading an FSM, which
        # guarantees graph/FSM consistency (manual node/link drift caused hangs).
        self._sync_graph_from_fsm()
        new_uid = self._name_to_uid.get(state.name, "")
        if new_uid:
            self._view.selected_nodes = [new_uid]
            self._selected_uid = new_uid
        self._dirty = True
        self._try_record_undo("Create node from link", before, self._undo_snapshot())

    def _on_node_selected(self, uid: str):
        self._selected_uid = uid
        self._clear_external_selection()

    def _clear_external_selection(self):
        """Clear hierarchy / scene selection only; keep Project panel file selection."""
        if self._clearing_selection:
            return
        self._clearing_selection = True
        try:
            from Infernux.engine.ui.selection_manager import SelectionManager

            SelectionManager.instance().clear()
        finally:
            self._clearing_selection = False

    def _on_global_selection_changed(self):
        """SelectionManager listener — hierarchy/scene selected something."""
        if self._clearing_selection:
            return
        from Infernux.engine.ui.selection_manager import SelectionManager
        if SelectionManager.instance().get_ids():
            self._view.selected_nodes.clear()
            self._view.selected_link = ""
            self._selected_uid = ""

    def _on_file_selected(self, path):
        """EditorEvent.FILE_SELECTED — project panel selected a file."""
        if path:
            self._view.selected_nodes.clear()
            self._view.selected_link = ""
            self._selected_uid = ""

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
            before = self._undo_snapshot()
            state = self._fsm.add_state()
            state.position = [gx, gy]
            node = self._graph.add_node("anim_state", x=gx, y=gy)
            node.data["label"] = state.name
            self._name_to_uid[state.name] = node.uid
            self._uid_to_name[node.uid] = state.name
            self._assign_clip_to_state(state, p, node, record_undo=False)
            node.data["loop"] = state.loop
            node.data["restart_same_clip"] = state.restart_same_clip
            self._view.selected_nodes = [node.uid]
            self._selected_uid = node.uid
            self._update_entry_link()
            self._dirty = True
            self._try_record_undo("Drop clip to canvas", before, self._undo_snapshot())

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
            before = self._undo_snapshot()
            state = self._fsm.add_state(self._unique_state_name("Timeline"))
            state.position = [gx, gy]
            state.kind = "timeline"
            node = self._graph.add_node("anim_state", x=gx, y=gy)
            node.data["label"] = state.name
            self._name_to_uid[state.name] = node.uid
            self._uid_to_name[node.uid] = state.name
            self._assign_timeline_to_state(state, p, node, record_undo=False)
            node.data["loop"] = state.loop
            self._view.selected_nodes = [node.uid]
            self._selected_uid = node.uid
            self._update_entry_link()
            self._dirty = True
            self._try_record_undo("Drop timeline to canvas", before, self._undo_snapshot())

    def _assign_timeline_to_state(self, state: AnimState, path: str, node=None, *, record_undo: bool = True):
        before = self._undo_snapshot() if record_undo else None
        p = (path or "").strip()
        if p and not p.lower().endswith(".animtimeline"):
            return
        state.kind = "timeline"
        state.timeline_guid = self._resolve_guid(p) if p else ""
        state.timeline_path = "" if state.timeline_guid else (p or "")
        self._clip_name_cache = {}
        self._dirty = True
        if record_undo and before is not None:
            self._try_record_undo("Assign timeline", before, self._undo_snapshot())

    def _clear_timeline_from_state(self, state: AnimState, node=None, *, record_undo: bool = True):
        before = self._undo_snapshot() if record_undo else None
        state.timeline_guid = ""
        state.timeline_path = ""
        self._clip_name_cache = {}
        self._dirty = True
        if record_undo and before is not None:
            self._try_record_undo("Clear timeline", before, self._undo_snapshot())

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
        before = self._undo_snapshot() if record_undo else None
        state.clip_guid = ""
        state.clip_path = ""
        self._clip_name_cache = {}
        self._dirty = True
        if record_undo and before is not None:
            self._try_record_undo("Clear clip", before, self._undo_snapshot())

    def _try_rename_state(self, state: AnimState, new_name: str) -> bool:
        """Rename an FSM state and keep graph / transitions consistent."""
        if not new_name or state.name == new_name:
            return False
        fsm = self._fsm
        if fsm is None or fsm.get_state(new_name) is not None:
            return False

        old_name = state.name
        uid = self._name_to_uid.get(old_name, "")
        if not uid:
            return False

        state.name = new_name
        del self._name_to_uid[old_name]
        self._name_to_uid[new_name] = uid
        self._uid_to_name[uid] = new_name

        node = self._graph.find_node(uid)
        if node:
            node.data["label"] = new_name

        if fsm.default_state == old_name:
            fsm.default_state = new_name

        for s in fsm.states:
            for tr in s.transitions:
                if tr.target_state == old_name:
                    tr.target_state = new_name

        return True

    def _assign_clip_to_state(self, state: AnimState, clip_path: str, node=None, *, record_undo: bool = True):
        """Assign a clip path/guid to a state."""
        before = self._undo_snapshot() if record_undo else None
        p = (clip_path or "").strip()
        if p and not self._clip_path_matches_fsm_mode(p):
            return
        state.clip_guid = self._resolve_guid(p) if p else ""
        state.clip_path = "" if state.clip_guid else (p or "")
        self._clip_name_cache = {}
        self._dirty = True
        if record_undo and before is not None:
            self._try_record_undo("Assign clip", before, self._undo_snapshot())

    def _remove_link_and_transition(self, link_uid: str) -> bool:
        """Remove a graph link and the corresponding FSM transition."""
        lk = self._graph.find_link(link_uid)
        if lk is None:
            return False
        if lk.source_node == self._entry_uid:
            if self._fsm and self._uid_to_name.get(lk.target_node, "") == self._fsm.default_state:
                self._fsm.default_state = ""
            return self._graph.remove_link(link_uid)
        src_name = self._uid_to_name.get(lk.source_node, "")
        dst_name = self._uid_to_name.get(lk.target_node, "")
        if src_name and dst_name and self._fsm:
            state = self._fsm.get_state(src_name)
            if state:
                state.transitions = [
                    tr for tr in state.transitions if tr.target_state != dst_name
                ]
        return self._graph.remove_link(link_uid)

    def _sync_transition_condition(self, lk: GraphLink):
        """Sync condition from link.data back to FSM transition."""
        src_name = self._uid_to_name.get(lk.source_node, "")
        dst_name = self._uid_to_name.get(lk.target_node, "")
        if src_name and dst_name and self._fsm:
            state = self._fsm.get_state(src_name)
            if state:
                for tr in state.transitions:
                    if tr.target_state == dst_name:
                        tr.condition = lk.data.get("condition", "")
                        if "duration" in lk.data:
                            try:
                                tr.duration = max(0.0, float(lk.data.get("duration", 0.0) or 0.0))
                            except (TypeError, ValueError):
                                tr.duration = 0.0
                        break

    # ── Save ──────────────────────────────────────────────────────────

    def _do_save(self):
        fsm = self._fsm
        if fsm is None:
            return
        # Sync node positions back before saving
        self._sync_fsm_positions()
        target = self._file_path or fsm.file_path
        if target:
            self._execute_save(target)
        else:
            self._show_save_as_dialog()

    def handle_save_command(self, save_as: bool = False) -> bool:
        if save_as:
            if self._fsm is not None:
                self._show_save_as_dialog()
        else:
            self._do_save()
        return True

    def _show_save_as_dialog(self):
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

    def _execute_save(self, target: str) -> bool:
        fsm = self._fsm
        if fsm is None:
            return False
        if fsm.save(target):
            self._file_path = target
            self._dirty = False
            self._sync_project_dirty_flag()
            Debug.log(f"Saved animfsm: {target}")
            self._hot_reload_animators(target)
            if self._mode_switch_waiting_for_save and self._pending_mode_switch:
                self._commit_mode_switch(self._pending_mode_switch)
            return True
        else:
            Debug.log_error(f"Failed to save animfsm: {target}")
            return False

    def _discard_unsaved_changes(self) -> bool:
        target = self._file_path or (self._fsm.file_path if self._fsm is not None else "")
        if target:
            self._open_animfsm(target)
            return not self._dirty
        self._new_fsm()
        self._dirty = False
        self._sync_project_dirty_flag()
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
