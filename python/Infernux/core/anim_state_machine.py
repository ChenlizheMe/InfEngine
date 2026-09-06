"""
AnimStateMachine — data model for an animation finite-state machine.

An AnimStateMachine holds a graph of named states (each referencing an
animation clip) connected by transitions.  Serialized as ``.animfsm`` JSON
files.  Shared between 2D and 3D animation systems.

Usage::

    fsm = AnimStateMachine.load("Assets/Animations/player.animfsm")
    fsm.save("Assets/Animations/player.animfsm")
"""

from __future__ import annotations

import ast
import json
import math
import os
import operator
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from Infernux.graph.parameters import (
    GraphParameterCollection,
    GraphParameterDefinition,
)
from Infernux.graph.types import CoordinateSpace, TypeRef, ValueType


# ═══════════════════════════════════════════════════════════════════════════
# Safe transition-condition evaluator (replaces eval())
# ═══════════════════════════════════════════════════════════════════════════

class AnimConditionError(Exception):
    """Raised when a transition condition cannot be parsed/evaluated safely."""


_ANIM_CMP_OPS = {
    ast.Eq: operator.eq,
    ast.NotEq: operator.ne,
    ast.Lt: operator.lt,
    ast.LtE: operator.le,
    ast.Gt: operator.gt,
    ast.GtE: operator.ge,
}


def _anim_eval_node(node: ast.AST, ctx: Dict[str, Any]) -> Any:
    """Whitelist AST interpreter — NO eval, builtins, calls, attrs, or subscripts.

    Supported: and / or / not, unary +/-, comparison chains, names (looked up in
    ``ctx``; unknown → 0, Unity-like), and literal constants (number/str/bool/None).
    """
    if isinstance(node, ast.BoolOp):
        if isinstance(node.op, ast.And):
            return all(bool(_anim_eval_node(v, ctx)) for v in node.values)
        if isinstance(node.op, ast.Or):
            return any(bool(_anim_eval_node(v, ctx)) for v in node.values)
        raise AnimConditionError("unsupported boolean operator")
    if isinstance(node, ast.UnaryOp):
        operand = _anim_eval_node(node.operand, ctx)
        if isinstance(node.op, ast.Not):
            return not bool(operand)
        if isinstance(node.op, ast.USub):
            return -_anim_as_number(operand)
        if isinstance(node.op, ast.UAdd):
            return +_anim_as_number(operand)
        raise AnimConditionError("unsupported unary operator")
    if isinstance(node, ast.Compare):
        left = _anim_eval_node(node.left, ctx)
        for op, comparator in zip(node.ops, node.comparators):
            fn = _ANIM_CMP_OPS.get(type(op))
            if fn is None:
                raise AnimConditionError("unsupported comparison operator")
            right = _anim_eval_node(comparator, ctx)
            try:
                ok = fn(left, right)
            except TypeError:
                # Mixed-type compare (e.g. str vs number): treat as not-equal-ish.
                ok = fn(0.0, 1.0) if not isinstance(op, (ast.Eq,)) else False
            if not ok:
                return False
            left = right
        return True
    if isinstance(node, ast.Name):
        return ctx.get(node.id, 0)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float, str, bool)) or node.value is None:
            return node.value
        raise AnimConditionError("unsupported constant")
    raise AnimConditionError(f"unsupported expression: {type(node).__name__}")


def _anim_as_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


# Parsed-AST cache: transition conditions are authored once and evaluated every
# frame by every animator (Spirit / Skeletal / Timeline FSM).  Re-parsing the
# source each frame dominated the runtime cost, so we memoize the parsed body
# keyed on the (stripped) condition string.  ``None`` marks a string that failed
# to parse so we don't re-attempt (and keep raising) every frame.
_ANIM_COND_AST_CACHE: Dict[str, Any] = {}
_ANIM_COND_AST_CACHE_LIMIT = 1024

# Sentinel distinct from a cached ``None`` (failed-parse) entry.
_CACHE_MISS = object()


def _require_exact_fields(value: object, expected: set[str], location: str) -> dict:
    if type(value) is not dict:
        raise TypeError(f"{location} must be an object")
    actual = set(value)
    if actual != expected:
        raise ValueError(
            f"{location} fields mismatch; "
            f"missing={sorted(expected - actual)}, unknown={sorted(actual - expected)}"
        )
    return value


def _finite_number(value: object, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise TypeError(f"{location} must be a finite number")
    return float(value)


def evaluate_anim_condition(expr: str, context: Dict[str, Any]) -> bool:
    """Safely evaluate an FSM transition condition string against ``context``.

    Replaces ``eval()`` in the animators.  Handles the structured AND-chains the
    FSM editor produces (``(speed > 0.5) and (grounded == 1.0)``) plus reasonable
    hand-authored conditions (bare flags, ``not x``, ``state == "idle"``).
    Raises :class:`AnimConditionError` (or ``SyntaxError``) on malformed input so
    callers can log a warning, matching the previous eval()-based behaviour.

    The parsed AST is cached per condition string so steady-state evaluation is
    a tree walk only (no per-frame ``ast.parse``).
    """
    c = (expr or "").strip()
    if not c:
        return False
    body = _ANIM_COND_AST_CACHE.get(c, _CACHE_MISS)
    if body is _CACHE_MISS:
        try:
            body = ast.parse(c, mode="eval").body
        except SyntaxError:
            body = None
        if len(_ANIM_COND_AST_CACHE) < _ANIM_COND_AST_CACHE_LIMIT:
            _ANIM_COND_AST_CACHE[c] = body
    if body is None:
        # Preserve previous behaviour: surface malformed input to the caller.
        ast.parse(c, mode="eval")
    return bool(_anim_eval_node(body, context))


@dataclass(frozen=True, slots=True)
class AnimParameter(GraphParameterDefinition):
    """Animation-domain parameter using the shared Graph schema."""

    name: str = "NewVar"
    writable: bool = True

    def __post_init__(self) -> None:
        GraphParameterDefinition.__post_init__(self)
        if self.value_type.space is not CoordinateSpace.NONE:
            raise ValueError("animation parameters cannot carry coordinate spaces")
        kind = self.value_type.value_type
        if kind not in {ValueType.BOOL, ValueType.I32, ValueType.F32}:
            raise ValueError("animation parameter type must be bool, i32, or f32")
        if kind is ValueType.BOOL and type(self.default) is not bool:
            raise TypeError("animation bool default must be a bool")
        if kind is ValueType.I32 and (
            type(self.default) is not int or isinstance(self.default, bool)
        ):
            raise TypeError("animation int default must be an integer")
        if kind is ValueType.F32:
            if not isinstance(self.default, (int, float)) or isinstance(
                self.default, bool
            ):
                raise TypeError("animation float default must be numeric")
            object.__setattr__(self, "default", float(self.default))

    @classmethod
    def from_dict(cls, value: dict) -> "AnimParameter":
        return GraphParameterDefinition.from_dict.__func__(
            cls, value, "animation parameter"
        )


_ANIM_CONDITION_OPERATORS = {
    "==": operator.eq,
    "!=": operator.ne,
    "<": operator.lt,
    "<=": operator.le,
    ">": operator.gt,
    ">=": operator.ge,
}


@dataclass(frozen=True, slots=True)
class AnimCondition:
    """One Unity-style transition predicate bound to a stable parameter ID."""

    stable_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    parameter_id: str = ""
    operator: str = ">"
    threshold: float = 0.0

    def __post_init__(self) -> None:
        stable_id = str(self.stable_id).strip()
        parameter_id = str(self.parameter_id).strip()
        if not stable_id:
            raise ValueError("animation condition stable_id must not be empty")
        if not parameter_id:
            raise ValueError("animation condition parameter_id must not be empty")
        if self.operator not in _ANIM_CONDITION_OPERATORS:
            raise ValueError(f"unsupported animation condition operator: {self.operator!r}")
        threshold = _finite_number(
            self.threshold, "animation condition threshold"
        )
        object.__setattr__(self, "stable_id", stable_id)
        object.__setattr__(self, "parameter_id", parameter_id)
        object.__setattr__(self, "threshold", threshold)

    def to_dict(self) -> dict:
        return {
            "stable_id": self.stable_id,
            "parameter_id": self.parameter_id,
            "operator": self.operator,
            "threshold": self.threshold,
        }

    @classmethod
    def from_dict(cls, value: dict) -> "AnimCondition":
        _require_exact_fields(
            value,
            {"stable_id", "parameter_id", "operator", "threshold"},
            "animation condition",
        )
        return cls(
            stable_id=value["stable_id"],
            parameter_id=value["parameter_id"],
            operator=value["operator"],
            threshold=value["threshold"],
        )

    def evaluate(self, value: object) -> bool:
        try:
            return bool(_ANIM_CONDITION_OPERATORS[self.operator](value, self.threshold))
        except (TypeError, ValueError):
            return False


@dataclass
class AnimTransition:
    """A directed transition between two states."""

    stable_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    target_state: str = ""
    conditions: List[AnimCondition] = field(default_factory=list)
    duration: float = 0.0     # cross-fade / blend duration in seconds
    synchronize_normalized_time: bool = False

    def __post_init__(self) -> None:
        if type(self.stable_id) is not str or not self.stable_id:
            raise ValueError("animation transition stable_id must be a non-empty string")
        if type(self.conditions) is not list or any(
            not isinstance(condition, AnimCondition)
            for condition in self.conditions
        ):
            raise TypeError("animation transition conditions must be AnimCondition values")
        if type(self.synchronize_normalized_time) is not bool:
            raise TypeError("animation transition synchronize_normalized_time must be a bool")
        condition_ids = [condition.stable_id for condition in self.conditions]
        if len(condition_ids) != len(set(condition_ids)):
            raise ValueError("animation transition condition stable_ids must be unique")

    def to_dict(self) -> dict:
        return {
            "stable_id": self.stable_id,
            "target_state": self.target_state,
            "conditions": [condition.to_dict() for condition in self.conditions],
            "duration": self.duration,
            "synchronize_normalized_time": self.synchronize_normalized_time,
        }

    @classmethod
    def from_dict(cls, d: dict) -> AnimTransition:
        _require_exact_fields(
            d,
            {
                "stable_id",
                "target_state",
                "conditions",
                "duration",
                "synchronize_normalized_time",
            },
            "animation transition",
        )
        if (
            type(d["stable_id"]) is not str
            or not d["stable_id"]
            or type(d["target_state"]) is not str
        ):
            raise TypeError("animation transition identity fields must be strings")
        if type(d["conditions"]) is not list:
            raise TypeError("animation transition conditions must be an array")
        duration = _finite_number(d["duration"], "animation transition duration")
        if duration < 0.0:
            raise ValueError("animation transition duration must be non-negative")
        return cls(
            stable_id=d["stable_id"],
            target_state=d["target_state"],
            conditions=[AnimCondition.from_dict(value) for value in d["conditions"]],
            duration=duration,
            synchronize_normalized_time=d["synchronize_normalized_time"],
        )


@dataclass
class AnimState:
    """A single state inside the FSM, referencing a clip and holding outgoing transitions.

    A state is normally a single clip (``kind="clip"``).  A *blend* state
    (``kind="blend"``) is a single-in/single-out node that linearly blends two
    clips A and B by its own ``blend_value`` (0..1, "Lerp") — A reuses
    ``clip_guid``/``clip_path``; B uses ``clip_b_guid``/``clip_b_path``.  Each
    blend state owns its Lerp (not shared across nodes).
    """

    stable_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    name: str = "New State"
    kind: str = "clip"        # "clip" | "blend" | "timeline"
    clip_guid: str = ""       # GUID of the referenced .animclip2d / .animclip3d (clip A)
    clip_path: str = ""       # editor-only display hint; runtime resolves clip_guid
    # Blend-state second clip (B) + per-node Lerp (0..1) when kind == "blend".
    clip_b_guid: str = ""
    clip_b_path: str = ""
    blend_value: float = 0.5
    # Timeline-state reference when kind == "timeline" (.animtimeline asset).
    timeline_guid: str = ""
    timeline_path: str = ""
    speed: float = 1.0
    # 0..1: minimum normalized clip progress before outgoing transitions are considered.
    # 1.0 = must reach end of current clip segment (default; matches "play full clip then transition").
    exit_time_normalized: float = 1.0
    loop: bool = True         # whether to loop the clip in this state
    # If True, SpiritAnimator.play(state) restarts the clip when already in that state.
    # If False, play() is a no-op while that state is already playing (e.g. safe to call every frame).
    restart_same_clip: bool = False
    transitions: List[AnimTransition] = field(default_factory=list)
    # Visual position in the node editor (editor-only, persisted for convenience)
    position: List[float] = field(default_factory=lambda: [0.0, 0.0])
    # Optional custom node header color in editor RGBA.
    header_color: List[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if type(self.stable_id) is not str or not self.stable_id:
            raise ValueError("animation state stable_id must be a non-empty string")

    def to_dict(self) -> dict:
        return {
            "stable_id": self.stable_id,
            "name": self.name,
            "kind": self.kind,
            "clip_guid": self.clip_guid,
            "clip_path": self.clip_path,
            "clip_b_guid": self.clip_b_guid,
            "clip_b_path": self.clip_b_path,
            "blend_value": float(self.blend_value),
            "timeline_guid": self.timeline_guid,
            "timeline_path": self.timeline_path,
            "speed": self.speed,
            "exit_time_normalized": self.exit_time_normalized,
            "loop": self.loop,
            "restart_same_clip": self.restart_same_clip,
            "transitions": [t.to_dict() for t in self.transitions],
            "position": list(self.position),
            "header_color": list(self.header_color),
        }

    @classmethod
    def from_dict(cls, d: dict) -> AnimState:
        expected = {
            "stable_id", "name", "kind", "clip_guid", "clip_path", "clip_b_guid", "clip_b_path",
            "blend_value", "timeline_guid", "timeline_path", "speed",
            "exit_time_normalized", "loop", "restart_same_clip", "transitions",
            "position", "header_color",
        }
        _require_exact_fields(d, expected, "animation state")
        string_fields = (
            "stable_id", "name", "kind", "clip_guid", "clip_path", "clip_b_guid", "clip_b_path",
            "timeline_guid", "timeline_path",
        )
        if any(type(d[field]) is not str for field in string_fields) or not d["stable_id"]:
            raise TypeError("animation state identity and asset fields must be strings")
        if d["kind"] not in {"clip", "blend", "timeline"}:
            raise ValueError("animation state kind must be clip, blend, or timeline")
        blend_value = _finite_number(d["blend_value"], "animation state blend_value")
        exit_time = _finite_number(d["exit_time_normalized"], "animation state exit_time_normalized")
        speed = _finite_number(d["speed"], "animation state speed")
        if not 0.0 <= blend_value <= 1.0 or not 0.0 <= exit_time <= 1.0:
            raise ValueError("animation state normalized values must be in [0, 1]")
        if type(d["loop"]) is not bool or type(d["restart_same_clip"]) is not bool:
            raise TypeError("animation state loop fields must be bools")
        if type(d["transitions"]) is not list:
            raise TypeError("animation state transitions must be an array")
        if type(d["position"]) is not list or len(d["position"]) != 2:
            raise TypeError("animation state position must contain two numbers")
        position = [_finite_number(value, "animation state position") for value in d["position"]]
        raw_header = d["header_color"]
        if type(raw_header) is not list or len(raw_header) not in {0, 4}:
            raise TypeError("animation state header_color must be empty or contain four numbers")
        header_color = [_finite_number(value, "animation state header_color") for value in raw_header]
        if any(value < 0.0 or value > 1.0 for value in header_color):
            raise ValueError("animation state header_color values must be in [0, 1]")
        return cls(
            stable_id=d["stable_id"],
            name=d["name"],
            kind=d["kind"],
            clip_guid=d["clip_guid"],
            clip_path=d["clip_path"],
            clip_b_guid=d["clip_b_guid"],
            clip_b_path=d["clip_b_path"],
            blend_value=blend_value,
            timeline_guid=d["timeline_guid"],
            timeline_path=d["timeline_path"],
            speed=speed,
            exit_time_normalized=exit_time,
            loop=d["loop"],
            restart_same_clip=d["restart_same_clip"],
            transitions=[AnimTransition.from_dict(t) for t in d["transitions"]],
            position=position,
            header_color=header_color,
        )


@dataclass
class AnimStateMachine:
    """A finite-state machine describing animation states and transitions."""

    name: str = "New State Machine"
    default_state: str = ""                          # name of entry state
    mode: str = "2d"                                 # "2d" or "3d"
    states: List[AnimState] = field(default_factory=list)
    parameters: List[AnimParameter] = field(default_factory=list)
    entry_position: List[float] = field(default_factory=lambda: [-100.0, 50.0])
    file_path: str = field(default="", repr=False, compare=False)

    # ── Serialization ─────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "default_state": self.default_state,
            "mode": self.mode,
            "states": [s.to_dict() for s in self.states],
            "parameters": [p.to_dict() for p in self.parameters],
            "entry_position": [float(self.entry_position[0]), float(self.entry_position[1])],
        }

    @classmethod
    def from_dict(cls, d: dict) -> AnimStateMachine:
        _require_exact_fields(
            d,
            {"name", "default_state", "mode", "states", "parameters", "entry_position"},
            "animation state machine",
        )
        if type(d["name"]) is not str or type(d["default_state"]) is not str or type(d["mode"]) is not str:
            raise TypeError("animation state machine identity fields must be strings")
        if d["mode"] not in {"2d", "3d", "timeline"}:
            raise ValueError("animation state machine mode must be 2d, 3d, or timeline")
        if type(d["states"]) is not list:
            raise TypeError("animation state machine states must be an array")
        raw_params = d["parameters"]
        if type(raw_params) is not list:
            raise TypeError("animation state machine parameters must be an array")
        params = [AnimParameter.from_dict(item) for item in raw_params]
        entry_position = d["entry_position"]
        if (
            type(entry_position) is not list
            or len(entry_position) != 2
            or any(type(value) not in (int, float) for value in entry_position)
        ):
            raise TypeError("animation state machine entry_position must be vec2")
        states = [AnimState.from_dict(item) for item in d["states"]]
        state_names = [state.name for state in states]
        state_ids = [state.stable_id for state in states]
        GraphParameterCollection(params)
        parameter_ids = {parameter.stable_id for parameter in params}
        transition_ids = [
            transition.stable_id
            for state in states
            for transition in state.transitions
        ]
        if len(state_names) != len(set(state_names)):
            raise ValueError("animation state names must be unique")
        if len(state_ids) != len(set(state_ids)):
            raise ValueError("animation state stable_ids must be unique")
        if len(transition_ids) != len(set(transition_ids)):
            raise ValueError("animation transition stable_ids must be unique")
        known_states = set(state_names)
        if d["default_state"] and d["default_state"] not in known_states:
            raise ValueError("animation default_state must reference a declared state")
        if any(
            transition.target_state not in known_states
            for state in states
            for transition in state.transitions
        ):
            raise ValueError("animation transitions must reference declared states")
        if any(
            condition.parameter_id not in parameter_ids
            for state in states
            for transition in state.transitions
            for condition in transition.conditions
        ):
            raise ValueError(
                "animation transition conditions must reference declared parameters"
            )
        return cls(
            name=d["name"],
            default_state=d["default_state"],
            mode=d["mode"],
            states=states,
            parameters=params,
            entry_position=[float(entry_position[0]), float(entry_position[1])],
        )

    def copy(self) -> AnimStateMachine:
        return AnimStateMachine.from_dict(self.to_dict())

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, AnimStateMachine):
            return NotImplemented
        return self.to_dict() == other.to_dict()

    def parameter_by_id(self, stable_id: str) -> Optional[AnimParameter]:
        stable_id = str(stable_id or "")
        return next(
            (
                parameter
                for parameter in self.parameters
                if parameter.stable_id == stable_id
            ),
            None,
        )

    def parameter_by_name(self, name: str) -> Optional[AnimParameter]:
        name = str(name or "")
        return next(
            (parameter for parameter in self.parameters if parameter.name == name),
            None,
        )

    def evaluate_transition_conditions(
        self,
        transition: AnimTransition,
        values: Dict[str, object],
    ) -> bool:
        """Evaluate every structured condition against public name-keyed values."""
        if not transition.conditions:
            return False
        for condition in transition.conditions:
            parameter = self.parameter_by_id(condition.parameter_id)
            if parameter is None:
                return False
            value = values.get(parameter.name, parameter.default)
            if not condition.evaluate(value):
                return False
        return True

    def transition_parameter_names(
        self, transition: AnimTransition
    ) -> tuple[str, ...]:
        names = []
        for condition in transition.conditions:
            parameter = self.parameter_by_id(condition.parameter_id)
            if parameter is not None and parameter.name not in names:
                names.append(parameter.name)
        return tuple(names)

    # ── File I/O ──────────────────────────────────────────────────────

    def save(self, path: str = "") -> bool:
        target = path or self.file_path
        if not target:
            return False
        try:
            from Infernux.core.document_store import write_document_text
            write_document_text(target, json.dumps(self.to_dict(), indent=2, ensure_ascii=False) + "\n")
            return True
        except (OSError, RuntimeError):
            return False

    @classmethod
    def load(cls, path: str) -> Optional[AnimStateMachine]:
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            fsm = cls.from_dict(data)
            fsm.file_path = path
            fsm.name = os.path.splitext(os.path.basename(path))[0]
            return fsm
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None

    # ── Helpers ───────────────────────────────────────────────────────

    @property
    def state_count(self) -> int:
        return len(self.states)

    def get_state(self, name: str) -> Optional[AnimState]:
        for s in self.states:
            if s.name == name:
                return s
        return None

    def get_state_by_id(self, stable_id: str) -> Optional[AnimState]:
        for state in self.states:
            if state.stable_id == stable_id:
                return state
        return None

    def get_transition_by_id(
        self, stable_id: str
    ) -> Optional[tuple[AnimState, AnimTransition]]:
        for state in self.states:
            for transition in state.transitions:
                if transition.stable_id == stable_id:
                    return state, transition
        return None

    def add_state(self, name: str = "") -> AnimState:
        if not name:
            name = f"State {self.state_count}"
        state = AnimState(name=name)
        self.states.append(state)
        if not self.default_state:
            self.default_state = name
        return state

    def remove_state(self, name: str) -> bool:
        for i, s in enumerate(self.states):
            if s.name == name:
                self.states.pop(i)
                # Clean up transitions pointing to removed state
                for other in self.states:
                    other.transitions = [
                        t for t in other.transitions if t.target_state != name
                    ]
                if self.default_state == name:
                    self.default_state = self.states[0].name if self.states else ""
                return True
        return False
