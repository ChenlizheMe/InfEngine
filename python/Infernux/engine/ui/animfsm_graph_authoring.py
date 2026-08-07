"""Animation FSM domain adapter for the shared :class:`NodeGraph` core."""

from __future__ import annotations

import copy

from Infernux.core.anim_state_machine import (
    AnimState,
    AnimStateMachine,
    AnimTransition,
)
from Infernux.core.node_graph import NodeGraph
from Infernux.graph.registry import (
    NodeDef,
    NodePresentation,
    PortDef,
    PortDirection,
    PortKind,
)

from .graph_document_authoring import _canvas_definition


FSM_ENTRY_NODE_ID = "animfsm.entry"
FSM_ENTRY_LINK_ID = "animfsm.entry-link"
FSM_STATE_NODE_TYPE_ID = "animfsm.state"
FSM_ENTRY_NODE_TYPE_ID = "animfsm.entry"

FSM_STATE_NODE_DEF = NodeDef(
    type_id=FSM_STATE_NODE_TYPE_ID,
    display_name="State",
    ports=(
        PortDef(
            "in",
            PortDirection.INPUT,
            PortKind.EXEC,
            display_name="In",
            max_connections=-1,
        ),
        PortDef("out", PortDirection.OUTPUT, PortKind.EXEC, display_name="Out"),
    ),
    presentation=NodePresentation(
        header_color=(0.20, 0.20, 0.22, 1.0),
        min_width=172.0,
        body_bottom_pad=0.0,
        visual_style="graph",
        show_header_color_swatch=False,
    ),
)

FSM_ENTRY_NODE_DEF = NodeDef(
    type_id=FSM_ENTRY_NODE_TYPE_ID,
    display_name="Entry",
    ports=(
        PortDef("out", PortDirection.OUTPUT, PortKind.EXEC, display_name="Start"),
    ),
    presentation=NodePresentation(
        header_color=(0.22, 0.21, 0.23, 1.0),
        min_width=88.0,
        deletable=False,
        body_bottom_pad=0.0,
        visual_style="graph",
        show_header_color_swatch=False,
    ),
)


class AnimFSMGraphAuthoringModel(NodeGraph):
    """NodeGraph authoring source with an explicit FSM projection boundary."""

    def __init__(
        self,
        fsm: AnimStateMachine,
    ) -> None:
        super().__init__(graph_kind="anim_fsm")
        self.register_type(_canvas_definition(FSM_STATE_NODE_DEF))
        self.register_type(_canvas_definition(FSM_ENTRY_NODE_DEF))
        self.load_fsm(fsm)

    @staticmethod
    def state_properties(state: AnimState) -> dict:
        document = state.to_dict()
        document["transitions"] = []
        properties = {
            "label": state.name,
            "loop": state.loop,
            "restart_same_clip": state.restart_same_clip,
            "fsm_state": document,
        }
        if isinstance(state.header_color, (list, tuple)) and len(state.header_color) >= 3:
            try:
                properties["header_color"] = (
                    float(state.header_color[0]),
                    float(state.header_color[1]),
                    float(state.header_color[2]),
                    float(state.header_color[3]) if len(state.header_color) >= 4 else 1.0,
                )
            except (TypeError, ValueError):
                pass
        return properties

    def transition_properties(self, transition: AnimTransition) -> dict:
        return {
            "conditions": [
                condition.to_dict() for condition in transition.conditions
            ],
            "duration": float(getattr(transition, "duration", 0.0) or 0.0),
            "fsm_transition": transition.to_dict(),
        }

    def load_fsm(self, fsm: AnimStateMachine) -> None:
        """Replace authoring state from a loaded/saved domain asset."""
        self.clear()
        self.add_node(
            FSM_ENTRY_NODE_TYPE_ID,
            canvas_x=float(fsm.entry_position[0]),
            canvas_y=float(fsm.entry_position[1]),
            uid=FSM_ENTRY_NODE_ID,
            label="Entry",
        )
        y_offset = 0.0
        names: dict[str, str] = {}
        for state in fsm.states:
            px, py = float(state.position[0]), float(state.position[1])
            if px == 0.0 and py == 0.0:
                px, py = 100.0, y_offset
                y_offset += 80.0
            self.add_node(
                FSM_STATE_NODE_TYPE_ID,
                canvas_x=px,
                canvas_y=py,
                uid=state.stable_id,
                **self.state_properties(state),
            )
            names[state.name] = state.stable_id

        default_uid = names.get(fsm.default_state, "")
        if default_uid:
            self.add_link(
                FSM_ENTRY_NODE_ID,
                "out",
                default_uid,
                "in",
                uid=FSM_ENTRY_LINK_ID,
                default_state=fsm.default_state,
            )
        for state in fsm.states:
            source_uid = names.get(state.name, "")
            for transition in state.transitions:
                target_uid = names.get(transition.target_state, "")
                if not source_uid or not target_uid:
                    continue
                self.add_link(
                    source_uid,
                    "out",
                    target_uid,
                    "in",
                    uid=transition.stable_id,
                    **self.transition_properties(transition),
                )

    def apply_to_fsm(self, fsm: AnimStateMachine) -> None:
        """Project the current NodeGraph into an existing runtime asset."""
        existing_states = {state.stable_id: state for state in fsm.states}
        existing_transitions = {
            transition.stable_id: transition
            for state in fsm.states
            for transition in state.transitions
        }
        states: list[AnimState] = []
        states_by_id: dict[str, AnimState] = {}
        for node in self.nodes:
            if node.uid == FSM_ENTRY_NODE_ID or node.type_id != FSM_STATE_NODE_TYPE_ID:
                continue
            document = node.data.get("fsm_state")
            if not isinstance(document, dict):
                raise RuntimeError(f"animation state node {node.uid!r} has no domain payload")
            decoded = AnimState.from_dict(copy.deepcopy(document))
            if decoded.stable_id != node.uid:
                raise RuntimeError("animation state graph identity does not match its payload")
            state = existing_states.get(decoded.stable_id, decoded)
            if state is not decoded:
                state.__dict__.update(copy.deepcopy(decoded.__dict__))
            state.position = [float(node.pos_x), float(node.pos_y)]
            state.transitions = []
            states.append(state)
            states_by_id[state.stable_id] = state

        default_state = ""
        for link in self.links:
            if link.uid == FSM_ENTRY_LINK_ID:
                target = states_by_id.get(link.target_node)
                default_state = target.name if target is not None else ""
                continue
            owner = states_by_id.get(link.source_node)
            target = states_by_id.get(link.target_node)
            document = link.data.get("fsm_transition")
            if owner is None or target is None or not isinstance(document, dict):
                raise RuntimeError(
                    f"animation transition link {link.uid!r} has invalid endpoints or payload"
                )
            decoded = AnimTransition.from_dict(copy.deepcopy(document))
            if decoded.stable_id != link.uid:
                raise RuntimeError(
                    "animation transition graph identity does not match its payload"
                )
            transition = existing_transitions.get(decoded.stable_id, decoded)
            if transition is not decoded:
                transition.__dict__.update(copy.deepcopy(decoded.__dict__))
            transition.target_state = target.name
            owner.transitions.append(transition)

        fsm.states = states
        fsm.default_state = default_state
        entry = self.find_node(FSM_ENTRY_NODE_ID)
        if entry is None:
            raise RuntimeError("animation FSM graph has no Entry node")
        fsm.entry_position = [float(entry.pos_x), float(entry.pos_y)]


__all__ = [
    "AnimFSMGraphAuthoringModel",
    "FSM_ENTRY_LINK_ID",
    "FSM_ENTRY_NODE_ID",
    "FSM_ENTRY_NODE_DEF",
    "FSM_ENTRY_NODE_TYPE_ID",
    "FSM_STATE_NODE_DEF",
    "FSM_STATE_NODE_TYPE_ID",
]
