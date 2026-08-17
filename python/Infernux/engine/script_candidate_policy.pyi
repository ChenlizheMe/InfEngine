from __future__ import annotations

import ast
from typing import Union


class ScriptCandidatePolicyIssue:
    code: str
    operation: str
    message: str
    line: int
    column: int
    end_line: int | None
    end_column: int | None


class ScriptCandidatePolicyReport:
    blocked: tuple[ScriptCandidatePolicyIssue, ...]
    runtime_guard_required: tuple[ScriptCandidatePolicyIssue, ...]

    @property
    def is_blocked(self) -> bool: ...

    @property
    def requires_runtime_guard(self) -> bool: ...

    @property
    def is_rejected(self) -> bool: ...


def analyze_script_candidate_tree(tree: ast.AST) -> ScriptCandidatePolicyReport: ...


def analyze_script_candidate(
    source: Union[bytes, bytearray, memoryview, str],
    *,
    filename: str = ...,
) -> ScriptCandidatePolicyReport: ...
