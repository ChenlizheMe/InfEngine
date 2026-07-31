from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

@dataclass(frozen=True)
class RuntimeAcceptanceTest:
    id: str
    scene: str
    run_seconds: float
    timeout_seconds: float
    document: dict[str, Any]

@dataclass(frozen=True)
class RuntimeAcceptanceManifest:
    name: str
    path: str
    tests: tuple[RuntimeAcceptanceTest, ...]
    @staticmethod
    def load(path: str, *, project_root: str = ...) -> RuntimeAcceptanceManifest: ...

class RuntimeAcceptance:
    @classmethod
    def begin(cls, manifest_path: str, result_path: str = ...) -> dict[str, Any]: ...
    @classmethod
    def tick(cls, delta_time: float) -> dict[str, Any]: ...
    @classmethod
    def current_test(cls) -> dict[str, Any]: ...
    @classmethod
    def pass_current(cls, details: Mapping[str, Any] | None = ...) -> dict[str, Any]: ...
    @classmethod
    def fail_current(cls, error: str, details: Mapping[str, Any] | None = ...) -> dict[str, Any]: ...
    @classmethod
    def status(cls) -> dict[str, Any]: ...
    @classmethod
    def is_active(cls) -> bool: ...
    @classmethod
    def reset(cls) -> None: ...
