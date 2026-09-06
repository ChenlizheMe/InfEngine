"""Thread-safe single-file script revision journal.

This module records immutable source candidates only. It never imports a
module or mutates the component registry; callers publish successful results
at their owner-thread reload safe point.
"""

from __future__ import annotations

import hashlib
import os
import threading
from dataclasses import dataclass
from typing import Iterable, Optional

from Infernux.engine.path_utils import path_key


def _path_key(path: str) -> str:
    return path_key(path)


def _source_bytes(source: bytes | bytearray | memoryview | str) -> bytes:
    if isinstance(source, str):
        return source.encode("utf-8")
    return bytes(source)


@dataclass(frozen=True, slots=True)
class ScriptRevision:
    path: str
    identity_key: str
    generation: int
    content_hash: str
    source: bytes


@dataclass(frozen=True, slots=True)
class ScriptRevisionRequest:
    revision: ScriptRevision

    @property
    def path(self) -> str:
        return self.revision.path

    @property
    def generation(self) -> int:
        return self.revision.generation


@dataclass(frozen=True, slots=True)
class ScriptRevisionDiagnostic:
    path: str
    generation: int
    content_hash: str
    messages: tuple[str, ...]
    phase: str = "validation"


@dataclass(frozen=True, slots=True)
class ScriptRevisionResult:
    request: ScriptRevisionRequest
    succeeded: bool
    diagnostic: Optional[ScriptRevisionDiagnostic] = None


@dataclass
class _RevisionState:
    next_generation: int = 0
    latest: Optional[ScriptRevision] = None
    pending: Optional[ScriptRevisionResult] = None
    claimed: Optional[ScriptRevisionResult] = None
    last_known_good: Optional[ScriptRevision] = None
    last_diagnostic: Optional[ScriptRevisionDiagnostic] = None


class ScriptRevisionJournal:
    """Coordinate source generations without touching live Python objects."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, _RevisionState] = {}

    @staticmethod
    def _resolve_force(
        force_new_generation: bool,
        force: bool | None,
    ) -> bool:
        if not isinstance(force_new_generation, bool):
            raise TypeError("force_new_generation must be bool")
        if force is None:
            return force_new_generation
        if not isinstance(force, bool):
            raise TypeError("force must be bool or None")
        if force_new_generation and not force:
            raise ValueError("force and force_new_generation disagree")
        return force

    def request(
        self,
        path: str,
        source: bytes | bytearray | memoryview | str,
        *,
        force_new_generation: bool = False,
        force: bool | None = None,
    ) -> Optional[ScriptRevisionRequest]:
        """Create a generation, or merge duplicate content unless forced."""
        display_path = os.fspath(path)
        identity_key = _path_key(display_path)
        payload = _source_bytes(source)
        content_hash = hashlib.sha256(payload).hexdigest()
        force_new_generation = self._resolve_force(force_new_generation, force)
        with self._lock:
            state = self._states.setdefault(identity_key, _RevisionState())
            if (
                not force_new_generation
                and state.latest is not None
                and state.latest.content_hash == content_hash
            ):
                return None
            state.next_generation += 1
            revision = ScriptRevision(
                path=display_path,
                identity_key=identity_key,
                generation=state.next_generation,
                content_hash=content_hash,
                source=payload,
            )
            state.latest = revision
            state.pending = None
            state.claimed = None
            state.last_diagnostic = None
            return ScriptRevisionRequest(revision=revision)

    def complete(
        self,
        request: ScriptRevisionRequest,
        *,
        succeeded: bool,
        messages: Iterable[str] = (),
        phase: str = "validation",
    ) -> bool:
        """Record a result, returning False when the request is stale."""
        revision = request.revision
        with self._lock:
            state = self._states.get(revision.identity_key)
            if state is None or state.latest != revision:
                return False
            diagnostic = None
            if not succeeded:
                diagnostic = ScriptRevisionDiagnostic(
                    path=revision.path,
                    generation=revision.generation,
                    content_hash=revision.content_hash,
                    messages=tuple(str(message) for message in messages),
                    phase=phase,
                )
            state.last_diagnostic = diagnostic
            state.pending = ScriptRevisionResult(request, bool(succeeded), diagnostic)
            return True

    @staticmethod
    def _batch_paths(paths: Iterable[str]) -> tuple[tuple[str, str], ...]:
        values = tuple(paths)
        if not values:
            return ()
        entries: list[tuple[str, str]] = []
        seen: set[str] = set()
        for path in values:
            display_path = os.fspath(path)
            key = _path_key(display_path)
            if key in seen:
                raise ValueError(f"batch contains duplicate path: {display_path!r}")
            seen.add(key)
            entries.append((key, display_path))
        return tuple(entries)

    def claim_ready_batch(
        self,
        paths: Iterable[str],
    ) -> tuple[ScriptRevisionResult, ...]:
        """Atomically claim successful current results for every path.

        A missing, failed, pending, stale, or already
        claimed member rejects the complete batch without changing any state.
        Results follow the caller's path order.
        """
        entries = self._batch_paths(paths)
        if not entries:
            return ()
        with self._lock:
            validated: list[tuple[_RevisionState, ScriptRevisionResult]] = []
            for key, display_path in entries:
                state = self._states.get(key)
                result = state.pending if state is not None else None
                if (
                    state is None
                    or result is None
                    or not result.succeeded
                    or state.latest != result.request.revision
                    or state.claimed is not None
                ):
                    return ()
                validated.append((state, result))
            for state, result in validated:
                state.pending = None
                state.claimed = result
            return tuple(result for _, result in validated)

    @staticmethod
    def _as_request(
        value: ScriptRevisionRequest | ScriptRevision,
    ) -> ScriptRevisionRequest:
        return value if isinstance(value, ScriptRevisionRequest) else ScriptRevisionRequest(value)

    def commit_published(
        self,
        request: ScriptRevisionRequest | ScriptRevision,
    ) -> bool:
        """Commit one claimed candidate after the actual publish succeeds."""
        if not isinstance(request, (ScriptRevisionRequest, ScriptRevision)):
            return self.commit_published_batch(request)
        request = self._as_request(request)
        revision = request.revision
        with self._lock:
            state = self._states.get(revision.identity_key)
            if state is None or state.latest != revision:
                return False
            if state.claimed is None or state.claimed.request != request:
                return False
            state.claimed = None
            state.last_known_good = revision
            state.last_diagnostic = None
            return True

    def commit_published_batch(
        self,
        requests: Iterable[ScriptRevisionRequest | ScriptRevision],
    ) -> bool:
        """Atomically commit every claimed request, or commit none."""
        values = tuple(self._as_request(value) for value in requests)
        if not values:
            return False
        with self._lock:
            keys: set[str] = set()
            states: list[tuple[_RevisionState, ScriptRevision]] = []
            for request in values:
                revision = request.revision
                if revision.identity_key in keys:
                    return False
                keys.add(revision.identity_key)
                state = self._states.get(revision.identity_key)
                if (
                    state is None
                    or state.latest != revision
                    or state.claimed is None
                    or state.claimed.request != request
                ):
                    return False
                states.append((state, revision))
            for state, revision in states:
                state.claimed = None
                state.last_known_good = revision
                state.last_diagnostic = None
            return True

    def release_claim(
        self,
        request: ScriptRevisionRequest | ScriptRevision,
    ) -> bool:
        """Release a failed or stale claim without changing the LKG."""
        if not isinstance(request, (ScriptRevisionRequest, ScriptRevision)):
            return self.release_claim_batch(request)
        request = self._as_request(request)
        revision = request.revision
        with self._lock:
            state = self._states.get(revision.identity_key)
            if state is None or state.claimed is None:
                return False
            if state.claimed.request != request:
                return False
            state.claimed = None
            return True

    def release_claim_batch(
        self,
        requests: Iterable[ScriptRevisionRequest | ScriptRevision],
    ) -> bool:
        """Atomically release every claimed request, or release none."""
        values = tuple(self._as_request(value) for value in requests)
        if not values:
            return False
        with self._lock:
            keys: set[str] = set()
            states: list[_RevisionState] = []
            for request in values:
                revision = request.revision
                if revision.identity_key in keys:
                    return False
                keys.add(revision.identity_key)
                state = self._states.get(revision.identity_key)
                if (
                    state is None
                    or state.latest != revision
                    or state.claimed is None
                    or state.claimed.request != request
                ):
                    return False
                states.append(state)
            for state in states:
                state.claimed = None
            return True

    def discard_batch(self, paths: Iterable[str]) -> bool:
        """Discard current pending/claimed candidates without advancing LKG."""
        entries = self._batch_paths(paths)
        if not entries:
            return False
        with self._lock:
            states: list[_RevisionState] = []
            for key, _ in entries:
                state = self._states.get(key)
                if (
                    state is None
                    or state.latest is None
                    or (state.pending is None and state.claimed is None)
                ):
                    return False
                states.append(state)
            for state in states:
                state.pending = None
                state.claimed = None
            return True

    def abort_batch(self, paths: Iterable[str]) -> bool:
        """Alias for :meth:`discard_batch` used by transaction callers."""
        return self.discard_batch(paths)

    def latest(self, path: str) -> Optional[ScriptRevision]:
        with self._lock:
            state = self._states.get(_path_key(path))
            return state.latest if state else None

    def last_known_good(self, path: str) -> Optional[ScriptRevision]:
        with self._lock:
            state = self._states.get(_path_key(path))
            return state.last_known_good if state else None

    def diagnostic(self, path: str) -> Optional[ScriptRevisionDiagnostic]:
        with self._lock:
            state = self._states.get(_path_key(path))
            return state.last_diagnostic if state else None
