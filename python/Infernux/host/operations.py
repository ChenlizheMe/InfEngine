"""OperationSchema v0 and its transport-neutral execution registry."""

from __future__ import annotations

import copy
import fnmatch
import re
import threading
import time
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass
from enum import Enum
from typing import Any, Callable, Iterable, Mapping


OPERATION_SCHEMA_ID = "infernux.operation"
OPERATION_SCHEMA_VERSION = 0


class OperationKind(str, Enum):
    QUERY = "query"
    COMMAND = "command"
    WORKFLOW = "workflow"


class OperationError(RuntimeError):
    def __init__(self, code: str, message: str, *, details: object = None) -> None:
        self.code = str(code)
        self.details = details
        super().__init__(str(message))

    def envelope(self) -> dict[str, object]:
        result: dict[str, object] = {
            "ok": False,
            "error": {"code": self.code, "message": str(self)},
        }
        if self.details is not None:
            result["error"]["details"] = self.details  # type: ignore[index]
        return result


@dataclass(frozen=True, slots=True)
class OperationSchema:
    id: str
    version: int
    kind: OperationKind
    summary: str
    input_schema: Mapping[str, object]
    output_schema: Mapping[str, object]
    errors: tuple[Mapping[str, object], ...]
    thread: str
    side_effects: tuple[str, ...]
    reversible: bool
    capabilities: tuple[str, ...]
    cost: Mapping[str, object]
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not re.fullmatch(
            r"[a-z][a-z0-9_-]*(?:\.[a-z0-9][a-z0-9_-]*)+", self.id
        ):
            raise ValueError("OperationSchema id must be a dotted stable identifier")
        if self.version != OPERATION_SCHEMA_VERSION:
            raise ValueError("Unsupported OperationSchema version")
        if self.thread not in {"any", "owner", "mixed"}:
            raise ValueError("OperationSchema thread must be any, owner, or mixed")
        if self.input_schema.get("type") != "object":
            raise ValueError("OperationSchema input_schema must describe an object")
        if not isinstance(self.output_schema, Mapping):
            raise ValueError("OperationSchema output_schema must be an object")
        if any(not str(item.get("code", "")).strip() for item in self.errors):
            raise ValueError("OperationSchema errors must have stable codes")
        if any(not str(item).strip() for item in self.capabilities):
            raise ValueError("OperationSchema capabilities cannot be empty")

    def document(self) -> dict[str, object]:
        value = asdict(self)
        value["$schema"] = OPERATION_SCHEMA_ID
        value["kind"] = self.kind.value
        return value


@dataclass(frozen=True, slots=True)
class Operation:
    schema: OperationSchema
    handler: Callable[..., Any]
    owner: str


class OperationRegistry:
    _instance: "OperationRegistry | None" = None

    def __init__(self) -> None:
        self._operations: dict[str, Operation] = {}
        self._revision = 0
        self._lock = threading.RLock()
        self._listeners: dict[str, Callable[[Mapping[str, object]], None]] = {}

    @classmethod
    def instance(cls) -> "OperationRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def register(self, operation: Operation) -> None:
        key = operation.schema.id.casefold()
        with self._lock:
            current = self._operations.get(key)
            if current is not None and current.owner != operation.owner:
                raise OperationError(
                    "operation.conflict",
                    f"Operation is already owned by {current.owner}: {operation.schema.id}",
                )
            self._operations[key] = operation
            self._revision += 1
            revision = self._revision
        self._notify(
            {
                "revision": revision,
                "action": "registered",
                "operations": [operation.schema.id],
                "owner": operation.owner,
            }
        )

    def unregister_owner(self, owner: str) -> int:
        with self._lock:
            keys = [key for key, value in self._operations.items() if value.owner == owner]
            operation_ids = [self._operations[key].schema.id for key in keys]
            for key in keys:
                del self._operations[key]
            if keys:
                self._revision += 1
            revision = self._revision
        if keys:
            self._notify(
                {
                    "revision": revision,
                    "action": "unregistered",
                    "operations": sorted(operation_ids),
                    "owner": str(owner),
                }
            )
        return len(keys)

    def subscribe(self, callback: Callable[[Mapping[str, object]], None]) -> str:
        if not callable(callback):
            raise TypeError("OperationRegistry listener must be callable")
        token = uuid.uuid4().hex
        with self._lock:
            self._listeners[token] = callback
        return token

    def unsubscribe(self, token: str) -> bool:
        with self._lock:
            return self._listeners.pop(str(token), None) is not None

    def _notify(self, event: Mapping[str, object]) -> None:
        with self._lock:
            listeners = tuple(self._listeners.values())
        frozen = copy.deepcopy(dict(event))
        for callback in listeners:
            try:
                callback(copy.deepcopy(frozen))
            except Exception:
                # Registry mutation must never be rolled back because an
                # observer failed; transports can resynchronize by revision.
                continue

    def list(
        self, *, kind: OperationKind | str | None = None, capability: str = ""
    ) -> tuple[dict[str, object], ...]:
        expected_kind = OperationKind(kind) if kind else None
        with self._lock:
            values = tuple(self._operations.values())
        return tuple(
            item.schema.document()
            for item in sorted(values, key=lambda value: value.schema.id)
            if (expected_kind is None or item.schema.kind == expected_kind)
            and (not capability or capability in item.schema.capabilities)
        )

    def get(self, operation_id: str) -> Operation:
        with self._lock:
            result = self._operations.get(str(operation_id).casefold())
        if result is None:
            raise OperationError("operation.not_found", f"Unknown operation: {operation_id}")
        return result

    def search(self, query: str, *, limit: int = 50) -> tuple[dict[str, object], ...]:
        terms = [part.casefold() for part in str(query).split() if part]
        wildcard = any(character in str(query) for character in "*?[")
        scored: list[tuple[int, str, Operation]] = []
        with self._lock:
            values = tuple(self._operations.values())
        for operation in values:
            schema = operation.schema
            haystack = " ".join((schema.id, schema.summary, *schema.tags)).casefold()
            if wildcard:
                if not fnmatch.fnmatch(haystack, str(query).casefold()):
                    continue
                score = 1
            else:
                if not all(term in haystack for term in terms):
                    continue
                score = sum(4 if term in schema.id.casefold() else 1 for term in terms)
            scored.append((-score, schema.id, operation))
        return tuple(item.schema.document() for _, _, item in sorted(scored)[: max(0, int(limit))])

    def execute(
        self,
        operation_id: str,
        arguments: Mapping[str, object] | None = None,
        *,
        capabilities: Iterable[str] = (),
        expected_kind: OperationKind | str | None = None,
    ) -> object:
        operation = self.get(operation_id)
        if expected_kind and operation.schema.kind != OperationKind(expected_kind):
            raise OperationError(
                "operation.kind_mismatch",
                f"{operation_id} is {operation.schema.kind.value}, not {OperationKind(expected_kind).value}",
            )
        granted = set(str(item) for item in capabilities)
        missing = [item for item in operation.schema.capabilities if item not in granted and "*" not in granted]
        if missing:
            raise OperationError(
                "operation.permission_denied",
                f"Missing capabilities for {operation_id}: {', '.join(missing)}",
                details={"required": missing},
            )
        payload = dict(arguments or {})
        _validate_arguments(payload, operation.schema.input_schema)
        try:
            return operation.handler(**payload)
        except OperationError:
            raise
        except TypeError as exc:
            raise OperationError("operation.invalid_arguments", str(exc)) from exc
        except Exception as exc:
            raise OperationError(
                "operation.failed", f"{type(exc).__name__}: {exc}"
            ) from exc

    def execute_batch(
        self,
        calls: Iterable[Mapping[str, object]],
        *,
        capabilities: Iterable[str] = (),
        stop_on_error: bool = True,
    ) -> tuple[dict[str, object], ...]:
        results: list[dict[str, object]] = []
        for index, call in enumerate(calls):
            operation_id = str(call.get("operation", ""))
            try:
                value = self.execute(
                    operation_id,
                    call.get("arguments") if isinstance(call.get("arguments"), Mapping) else {},
                    capabilities=capabilities,
                )
                results.append({"ok": True, "index": index, "operation": operation_id, "result": value})
            except OperationError as exc:
                failure = {**exc.envelope(), "index": index, "operation": operation_id}
                results.append(failure)
                if stop_on_error:
                    break
        return tuple(results)


@dataclass(slots=True)
class _Job:
    id: str
    operation: str
    created_at: float
    future: Future


class OperationJobRegistry:
    """Bounded asynchronous operation execution owned by one Host service."""

    def __init__(
        self,
        registry: OperationRegistry,
        *,
        max_workers: int = 4,
        max_jobs: int = 256,
    ) -> None:
        self.registry = registry
        self._max_jobs = max(1, int(max_jobs))
        self._executor = ThreadPoolExecutor(
            max_workers=max(1, int(max_workers)), thread_name_prefix="InfernuxHostJob"
        )
        self._jobs: dict[str, _Job] = {}
        self._lock = threading.Lock()
        self._accepting = True

    def submit(
        self,
        operation_id: str,
        arguments: Mapping[str, object] | None = None,
        *,
        capabilities: Iterable[str] = (),
    ) -> str:
        with self._lock:
            if not self._accepting:
                raise OperationError("job.stopped", "Operation job service is stopping")
            if len(self._jobs) >= self._max_jobs:
                completed = sorted(
                    (
                        job
                        for job in self._jobs.values()
                        if job.future.done() or job.future.cancelled()
                    ),
                    key=lambda item: item.created_at,
                )
                for job in completed:
                    self._jobs.pop(job.id, None)
                    if len(self._jobs) < self._max_jobs:
                        break
            if len(self._jobs) >= self._max_jobs:
                raise OperationError(
                    "job.capacity",
                    f"Operation job service reached its {self._max_jobs}-job capacity",
                )
            job_id = uuid.uuid4().hex
            future = self._executor.submit(
                self.registry.execute,
                operation_id,
                dict(arguments or {}),
                capabilities=tuple(capabilities),
            )
            self._jobs[job_id] = _Job(job_id, operation_id, time.time(), future)
            return job_id

    def status(self, job_id: str) -> dict[str, object]:
        with self._lock:
            job = self._jobs.get(str(job_id))
        if job is None:
            raise OperationError("job.not_found", f"Unknown operation job: {job_id}")
        result: dict[str, object] = {
            "id": job.id,
            "operation": job.operation,
            "created_at": job.created_at,
            "done": job.future.done(),
            "cancelled": job.future.cancelled(),
        }
        if job.future.done() and not job.future.cancelled():
            try:
                result["result"] = job.future.result()
            except OperationError as exc:
                result.update(exc.envelope())
            except Exception as exc:
                result.update(
                    OperationError("job.failed", f"{type(exc).__name__}: {exc}").envelope()
                )
        return result

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(str(job_id))
        if job is None:
            raise OperationError("job.not_found", f"Unknown operation job: {job_id}")
        return job.future.cancel()

    def shutdown(
        self,
        *,
        wait: bool = True,
        cancel_futures: bool = True,
        timeout: float = 5.0,
    ) -> int:
        with self._lock:
            self._accepting = False
            futures = tuple(job.future for job in self._jobs.values())
        if cancel_futures:
            for future in futures:
                future.cancel()
        self._executor.shutdown(wait=False, cancel_futures=cancel_futures)
        if wait:
            deadline = time.monotonic() + max(float(timeout), 0.0)
            while any(not future.done() for future in futures):
                if time.monotonic() >= deadline:
                    break
                time.sleep(0.01)
        return sum(not future.done() for future in futures)


def _validate_arguments(arguments: Mapping[str, object], schema: Mapping[str, object]) -> None:
    properties = schema.get("properties", {})
    required = schema.get("required", ())
    if not isinstance(properties, Mapping) or not isinstance(required, (list, tuple)):
        raise OperationError("operation.invalid_schema", "Operation input schema is malformed")
    missing = [str(name) for name in required if name not in arguments]
    if missing:
        raise OperationError(
            "operation.invalid_arguments",
            f"Missing required arguments: {', '.join(missing)}",
        )
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(arguments) - set(properties))
        if unknown:
            raise OperationError(
                "operation.invalid_arguments",
                f"Unknown arguments: {', '.join(unknown)}",
            )
    for name, value in arguments.items():
        expected = properties.get(name)
        if not isinstance(expected, Mapping) or value is None:
            continue
        expected_type = expected.get("type")
        expected_types = (
            [str(item) for item in expected_type]
            if isinstance(expected_type, (list, tuple))
            else [str(expected_type)]
        )

        def matches(candidate: str) -> bool:
            accepted = {
                "string": str,
                "integer": int,
                "number": (int, float),
                "boolean": bool,
                "array": (list, tuple),
                "object": Mapping,
            }.get(candidate)
            return accepted is None or (
                isinstance(value, accepted)
                and not (candidate in {"integer", "number"} and isinstance(value, bool))
            )

        if not any(matches(candidate) for candidate in expected_types):
            expected_label = (
                "one of " + ", ".join(expected_types)
                if len(expected_types) > 1
                else expected_types[0]
            )
            raise OperationError(
                "operation.invalid_arguments",
                f"Argument {name} must be {expected_label}",
            )


__all__ = [
    "OPERATION_SCHEMA_ID",
    "OPERATION_SCHEMA_VERSION",
    "Operation",
    "OperationError",
    "OperationJobRegistry",
    "OperationKind",
    "OperationRegistry",
    "OperationSchema",
]
