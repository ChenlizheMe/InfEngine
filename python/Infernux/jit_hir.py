"""Small, conservative Typed HIR front-end for ``auto_parallel``.

This module is intentionally independent from :mod:`Infernux.jit` and from
the runtime JIT implementation.  It answers one question only: can a loop be
lowered to an independent range loop without changing Python semantics?

The implementation is deliberately fail-closed.  It does not try to prove
arbitrary Python code safe; unknown calls, indirect indexing, container
mutation, and control-flow which changes loop lifetime are diagnostics rather
than optimization opportunities.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import math
import textwrap
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping, Sequence


class _StrEnum(str, Enum):
    def __str__(self) -> str:
        return self.value


class ValueType(_StrEnum):
    UNKNOWN = "unknown"
    BOOL = "bool"
    INT = "int"
    FLOAT = "float"
    SCALAR = "scalar"
    ARRAY = "array"
    VOID = "void"


class BufferAccessKind(_StrEnum):
    READ = "read"
    WRITE = "write"
    READ_WRITE = "read_write"


class EffectKind(_StrEnum):
    PURE = "pure"
    BUFFER_READ = "buffer_read"
    BUFFER_WRITE = "buffer_write"
    REDUCTION = "reduction"
    PURE_CALL = "pure_call"
    UNKNOWN_CALL = "unknown_call"
    CONTAINER_MUTATION = "container_mutation"
    CONTROL_FLOW = "control_flow"
    ALIAS_RISK = "alias_risk"


class AliasRiskKind(_StrEnum):
    NONE = "none"
    POSSIBLE = "possible"
    HIGH = "high"
    UNKNOWN = "unknown"


class DiagnosticSeverity(_StrEnum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class DiagnosticCode(_StrEnum):
    PARSE_ERROR = "parse_error"
    FUNCTION_NOT_FOUND = "function_not_found"
    ASYNC_FUNCTION = "async_function"
    UNSUPPORTED_CONTROL_FLOW = "unsupported_control_flow"
    UNSUPPORTED_NESTED_LOOP = "unsupported_nested_loop"
    UNSUPPORTED_RANGE = "unsupported_range"
    NON_AFFINE_INDEX = "non_affine_index"
    INDIRECT_WRITE = "indirect_write"
    LOOP_CARRIED_READ = "loop_carried_read"
    LOOP_CARRIED_SCALAR = "loop_carried_scalar"
    UNKNOWN_CALL = "unknown_call"
    CONTAINER_MUTATION = "container_mutation"
    UNSUPPORTED_STATEMENT = "unsupported_statement"
    UNSUPPORTED_EXPRESSION = "unsupported_expression"
    ALIAS_RISK = "alias_risk"
    INVALID_REDUCTION = "invalid_reduction"
    INVALID_ARGUMENT = "invalid_argument"


@dataclass(frozen=True)
class SourceLocation:
    """A source span suitable for editor diagnostics and source rewriting."""

    line: int
    column: int
    end_line: int
    end_column: int

    @classmethod
    def from_node(cls, node: ast.AST) -> "SourceLocation":
        return cls(
            line=getattr(node, "lineno", 0),
            column=getattr(node, "col_offset", 0),
            end_line=getattr(node, "end_lineno", getattr(node, "lineno", 0)),
            end_column=getattr(node, "end_col_offset", getattr(node, "col_offset", 0)),
        )


@dataclass(frozen=True)
class TypeRef:
    value_type: ValueType
    shape: tuple[str, ...] = ()
    mutable: bool = False

    @property
    def is_scalar(self) -> bool:
        return self.value_type in {
            ValueType.BOOL,
            ValueType.INT,
            ValueType.FLOAT,
            ValueType.SCALAR,
        }


@dataclass(frozen=True)
class HIRArgument:
    name: str
    type_ref: TypeRef
    annotation: str | None = None
    location: SourceLocation | None = None


@dataclass(frozen=True)
class AffineExpr:
    """A small affine expression represented as ``sum(coeffs*x) + constant``."""

    coefficients: tuple[tuple[str, int], ...] = ()
    constant: int = 0

    @classmethod
    def variable(cls, name: str) -> "AffineExpr":
        return cls(((name, 1),), 0)

    @classmethod
    def literal(cls, value: int) -> "AffineExpr":
        return cls((), value)

    def coefficient(self, name: str) -> int:
        return dict(self.coefficients).get(name, 0)

    @property
    def variables(self) -> tuple[str, ...]:
        return tuple(name for name, _ in self.coefficients)

    def format(self) -> str:
        parts: list[str] = []
        for name, coefficient in self.coefficients:
            if coefficient == 1:
                parts.append(name)
            elif coefficient == -1:
                parts.append(f"-{name}")
            else:
                parts.append(f"{coefficient}*{name}")
        if self.constant or not parts:
            parts.append(str(self.constant))
        return " + ".join(parts).replace("+ -", "- ")


@dataclass(frozen=True)
class RangeSpec:
    start: ast.AST
    stop: ast.AST
    step: ast.AST
    start_affine: AffineExpr | None
    stop_affine: AffineExpr | None
    step_value: int | None
    location: SourceLocation

    @property
    def is_affine(self) -> bool:
        return self.start_affine is not None and self.stop_affine is not None and self.step_value not in (None, 0)


@dataclass(frozen=True)
class BufferAccess:
    buffer: str
    index: AffineExpr | None
    kind: BufferAccessKind
    location: SourceLocation
    syntax: str = ""
    same_iteration: bool = False
    unique: bool = False


@dataclass(frozen=True)
class Reduction:
    target: str
    operator: str
    value_source: str
    identity: str | None
    location: SourceLocation
    commutative: bool = True
    associative: bool = True


@dataclass(frozen=True)
class Effect:
    kind: EffectKind
    detail: str
    location: SourceLocation
    target: str | None = None


@dataclass(frozen=True)
class AliasRisk:
    kind: AliasRiskKind
    buffers: tuple[str, ...]
    detail: str
    location: SourceLocation | None = None


@dataclass(frozen=True)
class Diagnostic:
    severity: DiagnosticSeverity
    code: DiagnosticCode
    message: str
    location: SourceLocation | None = None
    loop_id: str | None = None

    @property
    def is_error(self) -> bool:
        return self.severity == DiagnosticSeverity.ERROR

    def format(self) -> str:
        prefix = self.code.value
        if self.location is not None:
            prefix += f" at {self.location.line}:{self.location.column}"
        return f"{prefix}: {self.message}"


class HIRStatementKind(_StrEnum):
    ASSIGN = "assign"
    AUG_ASSIGN = "aug_assign"
    EXPRESSION = "expression"
    IF = "if"
    CONTINUE = "continue"
    PASS = "pass"


class BasicBlockKind(_StrEnum):
    ENTRY = "entry"
    LINEAR = "linear"
    LOOP_HEADER = "loop_header"
    LOOP_BODY = "loop_body"
    EXIT = "exit"


@dataclass(frozen=True)
class HIRStatement:
    kind: HIRStatementKind
    target: str | None
    expression: str
    value_type: TypeRef
    location: SourceLocation


@dataclass(frozen=True)
class BasicBlock:
    """A stable structured CFG block used by lowering and diagnostics."""

    stable_id: str
    kind: BasicBlockKind
    source: str
    location: SourceLocation | None
    successors: tuple[str, ...]
    loop_id: str | None = None


@dataclass(frozen=True)
class RangeLoopHIR:
    """Typed summary of one candidate range loop."""

    stable_id: str
    function_name: str
    index_name: str
    range_spec: RangeSpec | None
    source_location: SourceLocation
    source: str
    statements: tuple[HIRStatement, ...]
    buffer_reads: tuple[BufferAccess, ...]
    buffer_writes: tuple[BufferAccess, ...]
    reductions: tuple[Reduction, ...]
    effects: tuple[Effect, ...]
    alias_risks: tuple[AliasRisk, ...]
    diagnostics: tuple[Diagnostic, ...]
    parallel_eligible: bool
    reason: str
    operation_cost: int = 1

    @property
    def loop_id(self) -> str:
        return self.stable_id

    @property
    def eligible(self) -> bool:
        return self.parallel_eligible

    @property
    def parallel_reason(self) -> str:
        return self.reason


@dataclass(frozen=True)
class FunctionHIR:
    name: str
    source: str
    arguments: tuple[HIRArgument, ...]
    loops: tuple[RangeLoopHIR, ...]
    diagnostics: tuple[Diagnostic, ...]
    statements: tuple[HIRStatement, ...] = ()
    blocks: tuple[BasicBlock, ...] = ()

    @property
    def eligible_loops(self) -> tuple[RangeLoopHIR, ...]:
        if any(diagnostic.is_error for diagnostic in self.diagnostics if diagnostic.loop_id is None):
            return ()
        return tuple(loop for loop in self.loops if loop.parallel_eligible)

    @property
    def parallel_eligible(self) -> bool:
        return bool(self.eligible_loops)

    @property
    def operation_cost(self) -> int:
        return sum(loop.operation_cost for loop in self.eligible_loops)

    @property
    def diagnostics_by_loop(self) -> Mapping[str, tuple[Diagnostic, ...]]:
        return {
            loop.stable_id: loop.diagnostics
            for loop in self.loops
        }

    @property
    def entry_block(self) -> BasicBlock:
        return self.blocks[0]

    @property
    def exit_block(self) -> BasicBlock:
        return self.blocks[-1]


# Familiar aliases for callers that prefer the long names.
HIRFunction = FunctionHIR
HIRRangeLoop = RangeLoopHIR
BufferReadWrite = BufferAccess
ReductionSpec = Reduction


_PURE_BUILTINS = frozenset({"abs", "min", "max", "round"})
_PURE_MATH = frozenset({
    "acos", "acosh", "asin", "asinh", "atan", "atan2", "atanh", "ceil",
    "copysign", "cos", "cosh", "degrees", "exp", "expm1", "fabs", "floor",
    "fmod", "frexp", "hypot", "isfinite", "isinf", "isnan", "ldexp", "log",
    "log10", "log1p", "log2", "pow", "radians", "sin", "sinh", "sqrt",
    "tan", "tanh", "trunc",
})
_PURE_NUMPY = frozenset({
    "abs", "absolute", "ceil", "clip", "cos", "exp", "fabs", "floor", "log",
    "log10", "log1p", "maximum", "minimum", "power", "sin", "sqrt", "tan",
    "tanh",
})
_MUTATING_METHODS = frozenset({
    "append", "clear", "extend", "insert", "pop", "remove", "reverse", "sort",
    "update", "setdefault", "discard", "add",
})
_REDUCTION_OPERATORS = frozenset({"+", "*", "min", "max"})


def _unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return ast.dump(node, include_attributes=False)


def _location(node: ast.AST) -> SourceLocation:
    return SourceLocation.from_node(node)


def _constant_int(node: ast.AST) -> int | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool):
        return int(node.value)
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.USub, ast.UAdd)):
        value = _constant_int(node.operand)
        if value is not None:
            return -value if isinstance(node.op, ast.USub) else value
    return None


def _combine_affine(left: AffineExpr, right: AffineExpr, sign: int) -> AffineExpr:
    coefficients = dict(left.coefficients)
    for name, value in right.coefficients:
        coefficients[name] = coefficients.get(name, 0) + sign * value
    return AffineExpr(
        tuple(sorted((name, value) for name, value in coefficients.items() if value)),
        left.constant + sign * right.constant,
    )


def _scale_affine(value: AffineExpr, factor: int) -> AffineExpr:
    return AffineExpr(
        tuple((name, coefficient * factor) for name, coefficient in value.coefficients if coefficient * factor),
        value.constant * factor,
    )


def _affine(node: ast.AST, *, known_calls: Mapping[str, str] | None = None) -> AffineExpr | None:
    if isinstance(node, ast.Name):
        return AffineExpr.variable(node.id)
    value = _constant_int(node)
    if value is not None:
        return AffineExpr.literal(value)
    if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Sub)):
        left = _affine(node.left, known_calls=known_calls)
        right = _affine(node.right, known_calls=known_calls)
        if left is not None and right is not None:
            return _combine_affine(left, right, 1 if isinstance(node.op, ast.Add) else -1)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mult):
        left_constant = _constant_int(node.left)
        right_constant = _constant_int(node.right)
        if left_constant is not None:
            value = _affine(node.right, known_calls=known_calls)
            return _scale_affine(value, left_constant) if value is not None else None
        if right_constant is not None:
            value = _affine(node.left, known_calls=known_calls)
            return _scale_affine(value, right_constant) if value is not None else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "len" and len(node.args) == 1:
        argument = node.args[0]
        if isinstance(argument, ast.Name):
            return AffineExpr(((f"len({argument.id})", 1),), 0)
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr in {"shape", "strides"}:
        if isinstance(node.value.value, ast.Name):
            index = node.slice
            index_value = _constant_int(index)
            if index_value is not None:
                return AffineExpr(((f"{node.value.value.id}.{node.value.attr}[{index_value}]", 1),), 0)
    return None


def _annotation_name(node: ast.AST | None) -> str | None:
    if node is None:
        return None
    return _unparse(node)


def _type_from_annotation(annotation: ast.AST | None) -> TypeRef:
    name = (_annotation_name(annotation) or "").lower()
    if name in {"int", "int32", "int64", "np.int32", "np.int64"}:
        return TypeRef(ValueType.INT)
    if name in {"float", "float32", "float64", "np.float32", "np.float64"}:
        return TypeRef(ValueType.FLOAT)
    if name in {"bool", "np.bool_"}:
        return TypeRef(ValueType.BOOL)
    if "ndarray" in name or "array" in name or "buffer" in name:
        return TypeRef(ValueType.ARRAY, mutable=True)
    return TypeRef(ValueType.UNKNOWN)


def _index_node(node: ast.Subscript) -> ast.AST:
    # Python 3.8+ stores the subscript expression directly in ``slice``.
    return node.slice


def _is_same_iteration_index(index: AffineExpr | None, loop_name: str) -> bool:
    return index is not None and index.coefficients == ((loop_name, 1),) and index.constant == 0


def _index_is_unique(index: AffineExpr | None, loop_name: str) -> bool:
    if index is None:
        return False
    coefficient = index.coefficient(loop_name)
    return coefficient != 0 and all(name == loop_name for name in index.variables)


def _call_name(node: ast.Call) -> tuple[str | None, str | None]:
    if isinstance(node.func, ast.Name):
        return node.func.id, None
    if isinstance(node.func, ast.Attribute):
        if isinstance(node.func.value, ast.Name):
            return node.func.value.id, node.func.attr
    return None, None


class _Analyzer:
    def __init__(self, tree: ast.Module, source: str, function: ast.FunctionDef | ast.AsyncFunctionDef):
        self.tree = tree
        self.source = source
        self.function = function
        self.diagnostics: list[Diagnostic] = []
        self.imports: dict[str, str] = {"math": "math", "np": "numpy", "numpy": "numpy"}
        self._collect_imports()
        self.arg_types: dict[str, TypeRef] = {}
        self.initialized_scalars: dict[str, str] = {}
        self._collect_arguments()
        self._collect_scalar_initializers()

    def _collect_imports(self) -> None:
        for node in self.tree.body:
            if isinstance(node, ast.Import):
                for item in node.names:
                    self.imports[item.asname or item.name.split(".")[0]] = item.name
            elif isinstance(node, ast.ImportFrom) and node.module in {"math", "numpy"}:
                for item in node.names:
                    self.imports[item.asname or item.name] = f"{node.module}.{item.name}"

    def _collect_arguments(self) -> None:
        arguments = [*self.function.args.posonlyargs, *self.function.args.args, *self.function.args.kwonlyargs]
        for argument in arguments:
            self.arg_types[argument.arg] = _type_from_annotation(argument.annotation)

    def _collect_scalar_initializers(self) -> None:
        for node in self.function.body:
            if isinstance(node, ast.For):
                break
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                if self._expr_type(node.value).is_scalar:
                    self.initialized_scalars[node.targets[0].id] = _unparse(node.value)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.value is not None:
                if self._expr_type(node.value).is_scalar:
                    self.initialized_scalars[node.target.id] = _unparse(node.value)

    def diagnostic(
        self,
        code: DiagnosticCode,
        message: str,
        node: ast.AST,
        *,
        loop_id: str | None = None,
    ) -> Diagnostic:
        value = Diagnostic(DiagnosticSeverity.ERROR, code, message, _location(node), loop_id)
        self.diagnostics.append(value)
        return value

    def _loop_id(self, loop: ast.For, ordinal: int) -> str:
        # The ordinal disambiguates identical loops while the AST dump makes
        # the ID independent of whitespace and source line shifts.
        payload = "|".join((self.function.name, str(ordinal), ast.dump(loop, include_attributes=False)))
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"{self.function.name}:loop:{digest}"

    def _range_spec(self, loop: ast.For, loop_id: str) -> RangeSpec | None:
        call = loop.iter
        if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name) or call.func.id != "range":
            self.diagnostic(DiagnosticCode.UNSUPPORTED_RANGE, "only range(...) loops are eligible", loop.iter, loop_id=loop_id)
            return None
        if not 1 <= len(call.args) <= 3 or call.keywords:
            self.diagnostic(DiagnosticCode.UNSUPPORTED_RANGE, "range must have one to three positional arguments", call, loop_id=loop_id)
            return None
        if len(call.args) == 1:
            start, stop, step = ast.Constant(0), call.args[0], ast.Constant(1)
        elif len(call.args) == 2:
            start, stop, step = call.args[0], call.args[1], ast.Constant(1)
        else:
            start, stop, step = call.args
        start_affine = _affine(start, known_calls=self.imports)
        stop_affine = _affine(stop, known_calls=self.imports)
        step_value = _constant_int(step)
        if start_affine is None or stop_affine is None or step_value in (None, 0):
            self.diagnostic(
                DiagnosticCode.UNSUPPORTED_RANGE,
                "range bounds must be affine and step must be a known non-zero integer",
                call,
                loop_id=loop_id,
            )
        return RangeSpec(start, stop, step, start_affine, stop_affine, step_value, _location(call))

    def _expr_type(self, node: ast.AST) -> TypeRef:
        if isinstance(node, ast.Constant):
            if isinstance(node.value, bool):
                return TypeRef(ValueType.BOOL)
            if isinstance(node.value, int):
                return TypeRef(ValueType.INT)
            if isinstance(node.value, float):
                return TypeRef(ValueType.FLOAT)
            return TypeRef(ValueType.UNKNOWN)
        if isinstance(node, ast.Name):
            return self.arg_types.get(node.id, TypeRef(ValueType.SCALAR))
        if isinstance(node, ast.Subscript):
            return TypeRef(ValueType.SCALAR)
        if isinstance(node, ast.UnaryOp):
            return self._expr_type(node.operand)
        if isinstance(node, ast.BinOp):
            left, right = self._expr_type(node.left), self._expr_type(node.right)
            if ValueType.FLOAT in {left.value_type, right.value_type}:
                return TypeRef(ValueType.FLOAT)
            if left.is_scalar and right.is_scalar:
                return TypeRef(ValueType.INT)
            return TypeRef(ValueType.UNKNOWN)
        if isinstance(node, ast.Compare) or isinstance(node, ast.BoolOp):
            return TypeRef(ValueType.BOOL)
        if isinstance(node, ast.Attribute):
            if node.attr in {"shape", "ndim", "size", "strides"}:
                return TypeRef(ValueType.INT)
            return TypeRef(ValueType.UNKNOWN)
        if isinstance(node, ast.Call):
            name, attr = _call_name(node)
            if name in _PURE_BUILTINS and attr is None:
                return TypeRef(ValueType.SCALAR)
            if attr in _PURE_MATH | _PURE_NUMPY:
                return TypeRef(ValueType.SCALAR)
            if name == "len" and attr is None:
                return TypeRef(ValueType.INT)
        return TypeRef(ValueType.UNKNOWN)

    def _is_pure_call(self, node: ast.Call) -> bool:
        name, attr = _call_name(node)
        if attr is None and name in _PURE_BUILTINS:
            return True
        module = self.imports.get(name or "")
        if attr is not None and (module == "math" or module == "numpy" or module == "np"):
            return attr in (_PURE_MATH if module == "math" else _PURE_NUMPY)
        if attr is None and name in self.imports and self.imports[name].startswith("math."):
            return name.split(".")[-1] in _PURE_MATH
        return name == "len" and len(node.args) == 1 and not node.keywords

    def _check_shape_access(self, node: ast.Attribute | ast.Subscript) -> bool:
        if isinstance(node, ast.Attribute) and node.attr in {"ndim", "size"} and isinstance(node.value, ast.Name):
            return True
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute):
            return node.value.attr == "shape" and isinstance(node.value.value, ast.Name) and _constant_int(node.slice) is not None
        return False

    def _visit_expression(
        self,
        node: ast.AST,
        loop: ast.For,
        loop_id: str,
        reads: list[BufferAccess],
        effects: list[Effect],
    ) -> None:
        if isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await)):
            self.diagnostic(
                DiagnosticCode.UNSUPPORTED_CONTROL_FLOW,
                f"{type(node).__name__} is not supported by the minimal HIR",
                node,
                loop_id=loop_id,
            )
            effects.append(Effect(EffectKind.CONTROL_FLOW, type(node).__name__, _location(node)))
            return
        if isinstance(node, ast.Subscript):
            if self._check_shape_access(node):
                return
            if not isinstance(node.value, ast.Name):
                self.diagnostic(DiagnosticCode.INDIRECT_WRITE, "buffer access base must be a named buffer", node, loop_id=loop_id)
            else:
                index = _affine(_index_node(node), known_calls=self.imports)
                loop_name = loop.target.id if isinstance(loop.target, ast.Name) else ""
                if index is None or any(name != loop_name for name in index.variables):
                    self.diagnostic(
                        DiagnosticCode.NON_AFFINE_INDEX,
                        f"read from {node.value.id}[...] is not indexed by the loop induction variable",
                        node,
                        loop_id=loop_id,
                    )
                same = _is_same_iteration_index(index, loop_name=loop_name)
                reads.append(BufferAccess(node.value.id, index, BufferAccessKind.READ, _location(node), _unparse(node), same, _index_is_unique(index, loop_name)))
                effects.append(Effect(EffectKind.BUFFER_READ, f"read {node.value.id}[{_unparse(_index_node(node))}]", _location(node), node.value.id))
            for child in ast.iter_child_nodes(node):
                self._visit_expression(child, loop, loop_id, reads, effects)
            return
        if isinstance(node, ast.Call):
            name, attr = _call_name(node)
            if attr in _MUTATING_METHODS:
                self.diagnostic(DiagnosticCode.CONTAINER_MUTATION, f"container mutation '{attr}()' is not parallel-safe", node, loop_id=loop_id)
                effects.append(Effect(EffectKind.CONTAINER_MUTATION, f"{attr}()", _location(node), name))
            elif not self._is_pure_call(node):
                self.diagnostic(DiagnosticCode.UNKNOWN_CALL, f"call '{_unparse(node.func)}' is not in the pure scalar allowlist", node, loop_id=loop_id)
                effects.append(Effect(EffectKind.UNKNOWN_CALL, f"unknown call {_unparse(node.func)}", _location(node)))
            else:
                effects.append(Effect(EffectKind.PURE_CALL, f"pure call {_unparse(node.func)}", _location(node)))
            for child in ast.iter_child_nodes(node):
                self._visit_expression(child, loop, loop_id, reads, effects)
            return
        if isinstance(node, ast.Attribute) and self._check_shape_access(node):
            return
        for child in ast.iter_child_nodes(node):
            self._visit_expression(child, loop, loop_id, reads, effects)

    def _write_access(
        self,
        target: ast.AST,
        loop: ast.For,
        loop_id: str,
        reads: list[BufferAccess],
        writes: list[BufferAccess],
        effects: list[Effect],
        *,
        aug: bool,
    ) -> None:
        if not isinstance(target, ast.Subscript) or not isinstance(target.value, ast.Name):
            if isinstance(target, (ast.Name, ast.Attribute)):
                return
            self.diagnostic(DiagnosticCode.INDIRECT_WRITE, "only direct buffer[i] writes are eligible", target, loop_id=loop_id)
            effects.append(Effect(EffectKind.BUFFER_WRITE, "indirect buffer write", _location(target)))
            return
        buffer_name = target.value.id
        index = _affine(_index_node(target), known_calls=self.imports)
        unique = _index_is_unique(index, loop.target.id if isinstance(loop.target, ast.Name) else "")
        same = _is_same_iteration_index(index, loop.target.id if isinstance(loop.target, ast.Name) else "")
        if not unique:
            self.diagnostic(
                DiagnosticCode.INDIRECT_WRITE,
                f"write to {buffer_name}[...] is not provably a unique affine index",
                target,
                loop_id=loop_id,
            )
        access_kind = BufferAccessKind.READ_WRITE if aug else BufferAccessKind.WRITE
        access = BufferAccess(buffer_name, index, access_kind, _location(target), _unparse(target), same, unique)
        writes.append(access)
        effects.append(Effect(EffectKind.BUFFER_WRITE, f"write {buffer_name}[{_unparse(_index_node(target))}]", _location(target), buffer_name))
        if aug:
            reads.append(BufferAccess(buffer_name, index, BufferAccessKind.READ, _location(target), _unparse(target), same, unique))
            effects.append(Effect(EffectKind.BUFFER_READ, f"read for update {buffer_name}[{_unparse(_index_node(target))}]", _location(target), buffer_name))

    def _is_scalar_reduction(
        self,
        node: ast.AugAssign,
        loop: ast.For,
        loop_id: str,
        statements: list[HIRStatement],
        reductions: list[Reduction],
        effects: list[Effect],
    ) -> bool:
        if not isinstance(node.target, ast.Name):
            return False
        operator_map = {ast.Add: "+", ast.Mult: "*"}
        operator = next((symbol for klass, symbol in operator_map.items() if isinstance(node.op, klass)), None)
        if operator is None:
            return False
        if operator not in _REDUCTION_OPERATORS:
            return False
        target = node.target.id
        # A reduction variable must not be a mutable array argument.
        if self.arg_types.get(target, TypeRef(ValueType.SCALAR)).value_type == ValueType.ARRAY:
            return False
        if target not in self.arg_types and target not in self.initialized_scalars:
            self.diagnostic(
                DiagnosticCode.INVALID_REDUCTION,
                f"scalar reduction '{target}' has no initializer before the loop",
                node,
                loop_id=loop_id,
            )
            return False
        reduction = Reduction(target, operator, _unparse(node.value), self.initialized_scalars.get(target), _location(node))
        reductions.append(reduction)
        effects.append(Effect(EffectKind.REDUCTION, f"{target} {operator}= ...", _location(node), target))
        statements.append(HIRStatement(HIRStatementKind.AUG_ASSIGN, target, _unparse(node.value), self._expr_type(node.value), _location(node)))
        return True

    def _statement(
        self,
        node: ast.stmt,
        loop: ast.For,
        loop_id: str,
        reads: list[BufferAccess],
        writes: list[BufferAccess],
        reductions: list[Reduction],
        effects: list[Effect],
        statements: list[HIRStatement],
    ) -> None:
        if isinstance(node, ast.Assign):
            if len(node.targets) != 1:
                self.diagnostic(DiagnosticCode.UNSUPPORTED_STATEMENT, "multiple assignment targets are not supported", node, loop_id=loop_id)
                return
            target = node.targets[0]
            if isinstance(target, ast.Subscript):
                self._write_access(target, loop, loop_id, reads, writes, effects, aug=False)
            elif isinstance(target, ast.Name):
                if self._contains_name(node.value, target.id):
                    self.diagnostic(DiagnosticCode.LOOP_CARRIED_SCALAR, f"scalar '{target.id}' carries a value between iterations", node, loop_id=loop_id)
                statements.append(HIRStatement(HIRStatementKind.ASSIGN, target.id, _unparse(node.value), self._expr_type(node.value), _location(node)))
            else:
                self.diagnostic(DiagnosticCode.INDIRECT_WRITE, "attribute or computed targets are not writable in a parallel loop", target, loop_id=loop_id)
            self._visit_expression(node.value, loop, loop_id, reads, effects)
            return
        if isinstance(node, ast.AugAssign):
            if self._is_scalar_reduction(node, loop, loop_id, statements, reductions, effects):
                self._visit_expression(node.value, loop, loop_id, reads, effects)
                return
            if isinstance(node.target, ast.Subscript):
                self._write_access(node.target, loop, loop_id, reads, writes, effects, aug=True)
                self._visit_expression(node.value, loop, loop_id, reads, effects)
                return
            self.diagnostic(DiagnosticCode.INVALID_REDUCTION, "augmented assignment is only valid for scalar reductions or direct buffer[i]", node, loop_id=loop_id)
            return
        if isinstance(node, ast.Expr):
            self._visit_expression(node.value, loop, loop_id, reads, effects)
            statements.append(HIRStatement(HIRStatementKind.EXPRESSION, None, _unparse(node.value), self._expr_type(node.value), _location(node)))
            return
        if isinstance(node, ast.If):
            self._visit_expression(node.test, loop, loop_id, reads, effects)
            for child in (*node.body, *node.orelse):
                self._statement(child, loop, loop_id, reads, writes, reductions, effects, statements)
            statements.append(HIRStatement(HIRStatementKind.IF, None, _unparse(node.test), TypeRef(ValueType.BOOL), _location(node)))
            return
        if isinstance(node, ast.Continue):
            statements.append(HIRStatement(HIRStatementKind.CONTINUE, None, "continue", TypeRef(ValueType.VOID), _location(node)))
            return
        if isinstance(node, ast.Pass):
            statements.append(HIRStatement(HIRStatementKind.PASS, None, "pass", TypeRef(ValueType.VOID), _location(node)))
            return
        if isinstance(node, (ast.For, ast.While)):
            self.diagnostic(DiagnosticCode.UNSUPPORTED_NESTED_LOOP, "nested loops are outside the minimal HIR", node, loop_id=loop_id)
            return
        if isinstance(node, (ast.Break, ast.Return, ast.Try, ast.Yield, ast.YieldFrom, ast.Await, ast.With, ast.AsyncWith, ast.Raise)):
            self.diagnostic(DiagnosticCode.UNSUPPORTED_CONTROL_FLOW, f"{type(node).__name__} is not allowed in a parallel loop", node, loop_id=loop_id)
            effects.append(Effect(EffectKind.CONTROL_FLOW, type(node).__name__, _location(node)))
            return
        self.diagnostic(DiagnosticCode.UNSUPPORTED_STATEMENT, f"statement '{type(node).__name__}' is not supported", node, loop_id=loop_id)

    @staticmethod
    def _contains_name(node: ast.AST, name: str) -> bool:
        return any(isinstance(item, ast.Name) and item.id == name and isinstance(item.ctx, ast.Load) for item in ast.walk(node))

    def _inspect_function_controls(self) -> None:
        if isinstance(self.function, ast.AsyncFunctionDef):
            self.diagnostic(DiagnosticCode.ASYNC_FUNCTION, "async functions cannot be lowered to this synchronous HIR", self.function)

    def _loop(self, node: ast.For, ordinal: int) -> RangeLoopHIR:
        loop_id = self._loop_id(node, ordinal)
        range_spec = self._range_spec(node, loop_id)
        index_name = node.target.id if isinstance(node.target, ast.Name) else "<non-name>"
        if not isinstance(node.target, ast.Name):
            self.diagnostic(DiagnosticCode.UNSUPPORTED_RANGE, "range induction target must be a single name", node.target, loop_id=loop_id)
        reads: list[BufferAccess] = []
        writes: list[BufferAccess] = []
        reductions: list[Reduction] = []
        effects: list[Effect] = []
        statements: list[HIRStatement] = []
        for statement in node.body:
            self._statement(statement, node, loop_id, reads, writes, reductions, effects, statements)

        # An access to a previous/future element of a buffer written in this
        # loop is a loop-carried dependence.  Same-element read/modify/write
        # remains safe because the write is unique for the current induction
        # value.
        written_names = {access.buffer for access in writes}
        for access in reads:
            if access.buffer in written_names and not access.same_iteration:
                self.diagnostic(
                    DiagnosticCode.LOOP_CARRIED_READ,
                    f"read {access.syntax} depends on another loop iteration",
                    _synthetic_node_location(access.location),
                    loop_id=loop_id,
                )

        # Multiple symbolic buffers may alias at runtime.  Elementwise
        # same-index access is safe; retaining the risk in HIR lets a future
        # transformer require explicit no-alias metadata for more aggressive
        # lowerings.
        buffer_names = sorted({access.buffer for access in (*reads, *writes)})
        alias_risks: list[AliasRisk] = []
        if len(buffer_names) > 1:
            risk = AliasRisk(
                AliasRiskKind.POSSIBLE,
                tuple(buffer_names),
                "distinct Python buffer arguments may alias; only same-index elementwise access is admitted",
                _location(node),
            )
            alias_risks.append(risk)
            effects.append(Effect(EffectKind.ALIAS_RISK, risk.detail, _location(node)))
            if any(not access.same_iteration for access in (*reads, *writes)):
                self.diagnostic(
                    DiagnosticCode.ALIAS_RISK,
                    "different buffers have a non-elementwise access and may alias at runtime",
                    node,
                    loop_id=loop_id,
                )

        diagnostics = tuple(item for item in self.diagnostics if item.loop_id == loop_id)
        error_codes = {item.code for item in diagnostics if item.is_error}
        if range_spec is None or not range_spec.is_affine:
            reason = "rejected: range bounds or step are not a provable affine range"
        elif error_codes:
            first = diagnostics[0]
            reason = f"rejected: {first.message}"
        elif not writes and not reductions:
            reason = "rejected: loop has no analyzable output write or scalar reduction"
        else:
            reason = "eligible: independent affine iterations with unique writes and/or standard scalar reductions"
        eligible = not error_codes and range_spec is not None and range_spec.is_affine and bool(writes or reductions)
        source = ast.get_source_segment(self.source, node) or _unparse(node)
        operation_cost = 0
        for item in ast.walk(node):
            if isinstance(item, (ast.BinOp, ast.UnaryOp, ast.Compare, ast.BoolOp)):
                operation_cost += 1
            elif isinstance(item, ast.Subscript):
                operation_cost += 2
            elif isinstance(item, ast.Call):
                operation_cost += 8
            elif isinstance(item, (ast.Assign, ast.AugAssign, ast.If)):
                operation_cost += 1
        return RangeLoopHIR(
            stable_id=loop_id,
            function_name=self.function.name,
            index_name=index_name,
            range_spec=range_spec,
            source_location=_location(node),
            source=source,
            statements=tuple(statements),
            buffer_reads=tuple(reads),
            buffer_writes=tuple(writes),
            reductions=tuple(reductions),
            effects=tuple(effects),
            alias_risks=tuple(alias_risks),
            diagnostics=diagnostics,
            parallel_eligible=eligible,
            reason=reason,
            operation_cost=max(1, operation_cost),
        )

    def _block_id(self, kind: BasicBlockKind, node: ast.AST | None, ordinal: int) -> str:
        syntax = ast.dump(node, include_attributes=False) if node is not None else kind.value
        payload = f"{self.function.name}|{kind.value}|{ordinal}|{syntax}"
        digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
        return f"{self.function.name}:block:{digest}"

    def _build_cfg(
        self,
        loop_nodes: Sequence[ast.For],
        loop_hirs: Sequence[RangeLoopHIR],
    ) -> tuple[BasicBlock, ...]:
        """Build a deterministic structured CFG for the admitted frontend.

        R7 intentionally admits top-level affine loops only. The CFG therefore
        represents normal statements as linear blocks and each loop as a
        header/body back-edge pair. Unsupported nested control remains inside
        the body block and is already rejected by effect analysis.
        """

        entry_id = f"{self.function.name}:block:entry"
        exit_id = f"{self.function.name}:block:exit"
        loop_by_node = {
            id(node): loop_hirs[index]
            for index, node in enumerate(loop_nodes)
        }
        drafts: list[tuple[str, BasicBlockKind, str, SourceLocation | None, str | None]] = []
        ordinal = 0
        for node in self.function.body:
            if isinstance(node, ast.For):
                loop_hir = loop_by_node[id(node)]
                header_id = f"{loop_hir.stable_id}:header"
                body_id = f"{loop_hir.stable_id}:body"
                drafts.append(
                    (
                        header_id,
                        BasicBlockKind.LOOP_HEADER,
                        _unparse(node.iter),
                        _location(node),
                        loop_hir.stable_id,
                    )
                )
                drafts.append(
                    (
                        body_id,
                        BasicBlockKind.LOOP_BODY,
                        "\n".join(_unparse(statement) for statement in node.body),
                        _location(node.body[0]) if node.body else _location(node),
                        loop_hir.stable_id,
                    )
                )
                ordinal += 2
                continue
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                continue
            drafts.append(
                (
                    self._block_id(BasicBlockKind.LINEAR, node, ordinal),
                    BasicBlockKind.LINEAR,
                    _unparse(node),
                    _location(node),
                    None,
                )
            )
            ordinal += 1

        blocks: list[BasicBlock] = []
        first_id = drafts[0][0] if drafts else exit_id
        blocks.append(BasicBlock(entry_id, BasicBlockKind.ENTRY, "", None, (first_id,)))
        for index, (stable_id, kind, source, location, loop_id) in enumerate(drafts):
            next_id = drafts[index + 1][0] if index + 1 < len(drafts) else exit_id
            if kind == BasicBlockKind.LOOP_HEADER:
                body_id = drafts[index + 1][0]
                after_body = drafts[index + 2][0] if index + 2 < len(drafts) else exit_id
                successors = (body_id, after_body)
            elif kind == BasicBlockKind.LOOP_BODY:
                successors = (drafts[index - 1][0],)
            else:
                successors = (next_id,)
            blocks.append(BasicBlock(stable_id, kind, source, location, successors, loop_id))
        blocks.append(BasicBlock(exit_id, BasicBlockKind.EXIT, "", None, ()))
        return tuple(blocks)

    def build(self) -> FunctionHIR:
        self._inspect_function_controls()
        # R7 intentionally supports one-dimensional top-level affine loops.
        # Nested loops are diagnosed by the owning top-level loop and never
        # become independent lowering candidates of their own.
        loops = tuple(node for node in self.function.body if isinstance(node, ast.For))
        loop_hirs = tuple(self._loop(node, ordinal) for ordinal, node in enumerate(loops))
        function_statements: list[HIRStatement] = []
        for node in self.function.body:
            if isinstance(node, ast.For):
                continue
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                continue
            if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
                function_statements.append(HIRStatement(HIRStatementKind.ASSIGN, node.targets[0].id, _unparse(node.value), self._expr_type(node.value), _location(node)))
        arguments = tuple(
            HIRArgument(argument.arg, self.arg_types.get(argument.arg, TypeRef(ValueType.UNKNOWN)), _annotation_name(argument.annotation), _location(argument))
            for argument in [*self.function.args.posonlyargs, *self.function.args.args, *self.function.args.kwonlyargs]
        )
        blocks = self._build_cfg(loops, loop_hirs)
        return FunctionHIR(
            self.function.name,
            self.source,
            arguments,
            loop_hirs,
            tuple(self.diagnostics),
            tuple(function_statements),
            blocks,
        )


def _synthetic_node_location(location: SourceLocation) -> ast.AST:
    node = ast.Constant(value=None)
    node.lineno = location.line
    node.col_offset = location.column
    node.end_lineno = location.end_line
    node.end_col_offset = location.end_column
    return node


def _parse(source: str) -> ast.Module:
    try:
        return ast.parse(textwrap.dedent(source), mode="exec")
    except SyntaxError as exc:
        location = SourceLocation(exc.lineno or 0, exc.offset or 0, exc.lineno or 0, exc.offset or 0)
        raise HIRParseError(f"{DiagnosticCode.PARSE_ERROR.value} at {location.line}:{location.column}: {exc.msg}") from exc


class HIRParseError(ValueError):
    """Raised only when Python source cannot be parsed into an AST."""


def _find_function(tree: ast.Module, function_name: str | None) -> ast.FunctionDef | ast.AsyncFunctionDef:
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    if function_name is not None:
        for function in functions:
            if function.name == function_name:
                return function
        raise LookupError(f"{DiagnosticCode.FUNCTION_NOT_FOUND.value}: {function_name}")
    if not functions:
        raise LookupError(f"{DiagnosticCode.FUNCTION_NOT_FOUND.value}: no top-level function")
    return functions[0]


def build_hir(source: str, function_name: str | None = None) -> FunctionHIR:
    """Parse source and build the minimal typed HIR for one function."""

    tree = _parse(source)
    function = _find_function(tree, function_name)
    return _Analyzer(tree, textwrap.dedent(source), function).build()


def analyze_source(source: str, function_name: str | None = None) -> FunctionHIR:
    """Alias for :func:`build_hir`, named for analysis-oriented callers."""

    return build_hir(source, function_name)


def analyze_function(function: Any, *, function_name: str | None = None) -> FunctionHIR:
    """Build HIR from a Python function or source string.

    ``inspect.getsource`` is used only to obtain source; the function is never
    executed.  Passing a source string is useful for generated kernels and
    keeps tests independent from inspect's file lookup rules.
    """

    if isinstance(function, str):
        return build_hir(function, function_name)
    try:
        source = textwrap.dedent(inspect.getsource(function))
    except (OSError, TypeError) as exc:
        raise TypeError("analyze_function needs a source string or an inspectable Python function") from exc
    return build_hir(source, function_name or getattr(function, "__name__", None))


def eligible_loops(function_or_source: Any, *, function_name: str | None = None) -> tuple[RangeLoopHIR, ...]:
    """Return only loops that the conservative analyzer can parallelize."""

    return analyze_function(function_or_source, function_name=function_name).eligible_loops


def hir_fingerprint(hir: FunctionHIR) -> str:
    """Return a stable identity for the normalized legality result."""

    payload = {
        "revision": 1,
        "name": hir.name,
        "source_ast": ast.dump(_parse(hir.source), include_attributes=False),
        "arguments": [
            (argument.name, argument.type_ref.value_type.value, argument.type_ref.shape)
            for argument in hir.arguments
        ],
        "loops": [
            {
                "id": loop.stable_id,
                "index": loop.index_name,
                "range": (
                    loop.range_spec.start_affine.format() if loop.range_spec and loop.range_spec.start_affine else None,
                    loop.range_spec.stop_affine.format() if loop.range_spec and loop.range_spec.stop_affine else None,
                    loop.range_spec.step_value if loop.range_spec else None,
                ),
                "reads": [
                    (item.buffer, item.index.format() if item.index else None, item.kind.value)
                    for item in loop.buffer_reads
                ],
                "writes": [
                    (item.buffer, item.index.format() if item.index else None, item.kind.value)
                    for item in loop.buffer_writes
                ],
                "reductions": [
                    (item.target, item.operator, item.identity)
                    for item in loop.reductions
                ],
                "eligible": loop.parallel_eligible,
                "operation_cost": loop.operation_cost,
                "diagnostics": [item.code.value for item in loop.diagnostics],
            }
            for loop in hir.loops
        ],
        "cfg": [
            (block.stable_id, block.kind.value, block.successors, block.loop_id)
            for block in hir.blocks
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "AffineExpr",
    "AliasRisk",
    "AliasRiskKind",
    "BufferAccess",
    "BufferAccessKind",
    "BufferReadWrite",
    "BasicBlock",
    "BasicBlockKind",
    "Diagnostic",
    "DiagnosticCode",
    "DiagnosticSeverity",
    "Effect",
    "EffectKind",
    "FunctionHIR",
    "HIRArgument",
    "HIRFunction",
    "HIRParseError",
    "HIRRangeLoop",
    "HIRStatement",
    "HIRStatementKind",
    "RangeLoopHIR",
    "RangeSpec",
    "Reduction",
    "ReductionSpec",
    "SourceLocation",
    "TypeRef",
    "ValueType",
    "analyze_function",
    "analyze_source",
    "build_hir",
    "eligible_loops",
    "hir_fingerprint",
]
