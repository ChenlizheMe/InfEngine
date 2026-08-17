from __future__ import annotations

from typing import Iterable, Optional


class ScriptRevision:
    path: str
    identity_key: str
    generation: int
    content_hash: str
    source: bytes


class ScriptRevisionRequest:
    revision: ScriptRevision
    path: str
    generation: int


class ScriptRevisionDiagnostic:
    path: str
    generation: int
    content_hash: str
    messages: tuple[str, ...]
    phase: str


class ScriptRevisionResult:
    request: ScriptRevisionRequest
    succeeded: bool
    diagnostic: Optional[ScriptRevisionDiagnostic]


class ScriptRevisionJournal:
    def __init__(self) -> None: ...
    def request(
        self,
        path: str,
        source: bytes | bytearray | memoryview | str,
        *,
        force_new_generation: bool = ...,
        force: bool | None = ...,
    ) -> Optional[ScriptRevisionRequest]: ...
    def complete(self, request: ScriptRevisionRequest, *, succeeded: bool, messages: Iterable[str] = ..., phase: str = ...) -> bool: ...
    def claim_ready(self, path: str | Iterable[str] | None = ...) -> tuple[ScriptRevisionResult, ...]: ...
    def claim_ready_batch(self, paths: Iterable[str]) -> tuple[ScriptRevisionResult, ...]: ...
    def commit_published(self, request: ScriptRevisionRequest | ScriptRevision | Iterable[ScriptRevisionRequest | ScriptRevision]) -> bool: ...
    def commit_published_batch(self, requests: Iterable[ScriptRevisionRequest | ScriptRevision]) -> bool: ...
    def release_claim(self, request: ScriptRevisionRequest | ScriptRevision | Iterable[ScriptRevisionRequest | ScriptRevision]) -> bool: ...
    def release_claim_batch(self, requests: Iterable[ScriptRevisionRequest | ScriptRevision]) -> bool: ...
    def discard_batch(self, paths: Iterable[str]) -> bool: ...
    def abort_batch(self, paths: Iterable[str]) -> bool: ...
    def latest(self, path: str) -> Optional[ScriptRevision]: ...
    def last_known_good(self, path: str) -> Optional[ScriptRevision]: ...
    def diagnostic(self, path: str) -> Optional[ScriptRevisionDiagnostic]: ...
