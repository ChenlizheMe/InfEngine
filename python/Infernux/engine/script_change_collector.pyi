from __future__ import annotations

from types import CodeType
from typing import Callable, Iterable

from Infernux.engine.runtime_script_revision import (
    ScriptRevision,
    ScriptRevisionDiagnostic,
    ScriptRevisionJournal,
    ScriptRevisionRequest,
)
from Infernux.engine.script_candidate_policy import ScriptCandidatePolicyReport

ORIGINS: frozenset[str]
CHANGE_KINDS: frozenset[str]


class ScriptChange:
    revision: ScriptRevision
    origin: str
    transaction_id: str
    catalog_event: str | None
    change_kind: str
    merged_count: int
    merged_origins: tuple[str, ...]
    merged_transaction_ids: tuple[str, ...]
    merged_catalog_events: tuple[str, ...]
    merged_change_kinds: tuple[str, ...]
    @property
    def path(self) -> str: ...
    @property
    def identity_key(self) -> str: ...
    @property
    def generation(self) -> int: ...
    @property
    def content_hash(self) -> str: ...
    @property
    def source(self) -> bytes: ...
    @property
    def request(self) -> ScriptRevisionRequest: ...
    @property
    def kind(self) -> str: ...
    @property
    def effective_catalog_event(self) -> str | None: ...


class ScriptFrontendImport:
    module: str
    level: int
    imported: tuple[str, ...]
    line: int
    column: int


class ScriptFrontendDiagnostic:
    path: str
    generation: int
    content_hash: str
    message: str
    phase: str
    severity: str
    code: str
    operation: str
    line: int | None
    column: int | None
    end_line: int | None
    end_column: int | None


class ScriptFrontendArtifact:
    path: str
    identity_key: str
    generation: int
    content_hash: str
    source: bytes
    code: CodeType | None
    imports: tuple[ScriptFrontendImport, ...]
    payload: object | None
    policy_report: ScriptCandidatePolicyReport


class ScriptChangeResult:
    change: ScriptChange
    status: str
    artifact: ScriptFrontendArtifact | None
    diagnostics: tuple[ScriptFrontendDiagnostic, ...]
    @property
    def succeeded(self) -> bool: ...
    @property
    def ready(self) -> bool: ...
    @property
    def request(self) -> ScriptRevisionRequest: ...
    @property
    def path(self) -> str: ...
    @property
    def generation(self) -> int: ...
    @property
    def content_hash(self) -> str: ...
    @property
    def source(self) -> bytes: ...
    @property
    def diagnostic(self) -> ScriptFrontendDiagnostic | None: ...


class ScriptChangeCollector:
    def __init__(
        self,
        journal: ScriptRevisionJournal | None = ...,
        *,
        compile_source: Callable[[bytes], object] | None = ...,
        frontend: Callable[[bytes], object] | None = ...,
    ) -> None: ...
    @property
    def journal(self) -> ScriptRevisionJournal: ...
    @property
    def pending_count(self) -> int: ...
    @property
    def completed_count(self) -> int: ...
    @property
    def is_shutdown(self) -> bool: ...
    def submit(
        self,
        path: str,
        source: bytes | bytearray | memoryview | str,
        *,
        origin: str,
        transaction_id: str | None = ...,
        catalog_event: str | None = ...,
        change_kind: str = ...,
        force_new_generation: bool = ...,
        force: bool | None = ...,
    ) -> ScriptChange | None: ...
    def process_worker_batch(self, max_items: int | None = ...) -> tuple[ScriptChangeResult, ...]: ...
    def drain_completed(self, path: str | None = ...) -> tuple[ScriptChangeResult, ...]: ...
    def claim_ready(self, path: str | Iterable[str] | None = ..., *, transaction_id: str | None = ...) -> tuple[ScriptChangeResult, ...]: ...
    def claim_ready_batch(self, expected_paths: Iterable[str], *, transaction_id: str) -> tuple[ScriptChangeResult, ...]: ...
    def publish_ready_batch(
        self,
        expected_paths: Iterable[str],
        transaction_id: str,
        publisher: Callable[[tuple[ScriptChangeResult, ...]], object],
        *,
        rollback: Callable[[object], object] | None = ...,
    ) -> object: ...
    def commit_published(self, value: ScriptChangeResult | ScriptChange | ScriptRevisionRequest | ScriptRevision | Iterable[ScriptChangeResult | ScriptChange | ScriptRevisionRequest | ScriptRevision], *, transaction_id: str | None = ...) -> bool: ...
    def commit_published_batch(self, values: Iterable[ScriptChangeResult | ScriptChange | ScriptRevisionRequest | ScriptRevision], *, transaction_id: str | None = ...) -> bool: ...
    def release_claim(self, value: ScriptChangeResult | ScriptChange | ScriptRevisionRequest | ScriptRevision | Iterable[ScriptChangeResult | ScriptChange | ScriptRevisionRequest | ScriptRevision], *, transaction_id: str | None = ...) -> bool: ...
    def release_claim_batch(self, values: Iterable[ScriptChangeResult | ScriptChange | ScriptRevisionRequest | ScriptRevision], *, transaction_id: str | None = ...) -> bool: ...
    def discard_batch(self, expected_paths: Iterable[str], *, transaction_id: str) -> bool: ...
    def abort_transaction(self, expected_paths: Iterable[str], *, transaction_id: str) -> bool: ...
    def latest(self, path: str) -> ScriptRevision | None: ...
    def last_known_good(self, path: str) -> ScriptRevision | None: ...
    def diagnostic(self, path: str) -> ScriptRevisionDiagnostic | None: ...
    def clear(self) -> None: ...
    def shutdown(self) -> None: ...
