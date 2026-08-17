from __future__ import annotations

from types import CodeType, ModuleType
from typing import Iterable


class CandidateImportError(ImportError): ...


class CandidateModuleSpec:
    name: str
    file_path: str
    source: bytes | str | None
    code: CodeType | None
    namespace: bool
    def __init__(
        self,
        name: str,
        file_path: str,
        source: bytes | str | None = ...,
        code: CodeType | None = ...,
        namespace: bool = ...,
    ) -> None: ...


class CandidateImportTransaction:
    def __init__(self, *, trusted_modules: Iterable[str] = ...) -> None: ...
    @property
    def modules(self) -> dict[str, ModuleType]: ...
    @property
    def publishable_modules(self) -> tuple[ModuleType, ...]: ...
    @property
    def loaded_module_names(self) -> tuple[str, ...]: ...
    def register(
        self,
        name: str,
        file_path: str,
        *,
        source: bytes | str | None = ...,
        code: CodeType | None = ...,
    ) -> None: ...
    def module_for(self, name: str) -> ModuleType | None: ...
    def load(self, name: str) -> ModuleType: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...
