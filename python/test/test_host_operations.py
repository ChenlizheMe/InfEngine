from __future__ import annotations

import threading
import time
from dataclasses import replace

import pytest

from Infernux.host import (
    MainThreadCommandQueue,
    Operation,
    OperationError,
    OperationJobRegistry,
    OperationKind,
    OperationRegistry,
    OperationSchema,
    capability_granted,
)


def test_capability_grants_accept_exact_names_and_fnmatch_patterns():
    assert capability_granted("scene.write", ("scene.write",))
    assert capability_granted("scene.write", ("*",))
    assert capability_granted("scene.write", ("scene.*",))
    assert capability_granted("scene.write", ("*.write",))
    assert not capability_granted("scene.write", ("*.read", "material.write"))
    assert not capability_granted("scene.write", ())
    # Non-pattern grants never match other capabilities partially.
    assert not capability_granted("scene.write", ("scene",))


def _schema(
    operation_id: str = "test.math.add",
    *,
    kind: OperationKind = OperationKind.QUERY,
) -> OperationSchema:
    return OperationSchema(
        id=operation_id,
        kind=kind,
        summary="Add two values.",
        input_schema={
            "type": "object",
            "properties": {
                "left": {"type": "number"},
                "right": {"type": "number"},
            },
            "required": ["left", "right"],
            "additionalProperties": False,
        },
        output_schema={"type": "number"},
        errors=({"code": "test.failed"},),
        thread="any",
        side_effects=(),
        reversible=False,
        capabilities=("math.read",),
        cost={"class": "constant"},
        tags=("math", "addition"),
    )


def test_operation_registry_revision_events_conflicts_and_cache_resync():
    registry = OperationRegistry()
    events = []
    token = registry.subscribe(events.append)
    start_revision = registry.revision
    registry.register(Operation(_schema(), lambda left, right: left + right, "owner-a"))

    assert registry.revision == start_revision + 1
    assert events == [
        {
            "revision": start_revision + 1,
            "action": "registered",
            "operations": ["test.math.add"],
            "owner": "owner-a",
        }
    ]
    assert registry.search("math addition")[0]["id"] == "test.math.add"
    with pytest.raises(OperationError, match="already owned") as conflict:
        registry.register(Operation(_schema(), lambda left, right: 0, "owner-b"))
    assert conflict.value.code == "operation.conflict"

    assert registry.unregister_owner("owner-a") == 1
    assert events[-1]["revision"] == start_revision + 2
    assert events[-1]["action"] == "unregistered"
    assert registry.unsubscribe(token) is True
    assert registry.unsubscribe(token) is False


def test_operation_execution_validates_capability_kind_and_json_arguments():
    registry = OperationRegistry()
    registry.register(Operation(_schema(), lambda left, right: left + right, "owner"))

    assert registry.execute(
        "test.math.add", {"left": 2, "right": 3.5}, capabilities=("math.read",)
    ) == 5.5
    with pytest.raises(OperationError) as denied:
        registry.execute("test.math.add", {"left": 2, "right": 3})
    assert denied.value.code == "operation.permission_denied"
    with pytest.raises(OperationError) as wrong_kind:
        registry.execute(
            "test.math.add",
            {"left": 2, "right": 3},
            capabilities=("*",),
            expected_kind=OperationKind.COMMAND,
        )
    assert wrong_kind.value.code == "operation.kind_mismatch"
    for arguments in (
        {"left": 1},
        {"left": 1, "right": 2, "extra": 3},
        {"left": True, "right": 2},
    ):
        with pytest.raises(OperationError) as invalid:
            registry.execute("test.math.add", arguments, capabilities=("*",))
        assert invalid.value.code == "operation.invalid_arguments"


def test_operation_execution_accepts_json_schema_union_types():
    schema = replace(_schema("test.input.key"), input_schema={
        "type": "object",
        "properties": {"key": {"type": ["string", "integer"]}},
        "required": ["key"],
        "additionalProperties": False,
    })
    registry = OperationRegistry()
    registry.register(Operation(schema, lambda key: key, "owner"))

    assert registry.execute("test.input.key", {"key": "d"}, capabilities=("*",)) == "d"
    assert registry.execute("test.input.key", {"key": 7}, capabilities=("*",)) == 7
    with pytest.raises(OperationError, match="one of string, integer"):
        registry.execute("test.input.key", {"key": False}, capabilities=("*",))


def test_operation_batch_has_structured_stop_and_continue_semantics():
    registry = OperationRegistry()
    registry.register(Operation(_schema(), lambda left, right: left + right, "owner"))
    calls = (
        {"operation": "test.math.add", "arguments": {"left": 1, "right": 2}},
        {"operation": "missing.operation", "arguments": {}},
        {"operation": "test.math.add", "arguments": {"left": 4, "right": 5}},
    )

    stopped = registry.execute_batch(calls, capabilities=("*",), stop_on_error=True)
    continued = registry.execute_batch(calls, capabilities=("*",), stop_on_error=False)
    assert len(stopped) == 2
    assert stopped[1]["error"]["code"] == "operation.not_found"
    assert len(continued) == 3
    assert continued[2]["result"] == 9


def test_job_registry_is_bounded_and_shutdown_reports_running_work():
    registry = OperationRegistry()
    entered = threading.Event()
    release = threading.Event()

    def block(left, right):
        entered.set()
        release.wait(2)
        return left + right

    registry.register(Operation(_schema(), block, "owner"))
    jobs = OperationJobRegistry(registry, max_workers=1, max_jobs=1)
    job_id = jobs.submit(
        "test.math.add", {"left": 1, "right": 2}, capabilities=("*",)
    )
    assert entered.wait(1)
    with pytest.raises(OperationError) as capacity:
        jobs.submit("test.math.add", {"left": 3, "right": 4}, capabilities=("*",))
    assert capacity.value.code == "job.capacity"
    assert jobs.shutdown(timeout=0.01) == 1
    release.set()
    deadline = time.monotonic() + 1
    while not jobs.status(job_id)["done"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert jobs.shutdown(timeout=1) == 0


def test_main_thread_queue_release_cancels_pending_and_clears_owner():
    queue = MainThreadCommandQueue()
    queue.drain(0)
    assert queue.wait_until_ready(0) is True
    result = []
    errors = []

    def worker():
        future = queue.submit("pending", lambda: result.append("ran"), timeout_ms=1000)
        try:
            future.result(1)
        except BaseException as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.monotonic() + 1
    while queue._queue.empty() and time.monotonic() < deadline:
        time.sleep(0.005)
    assert queue.release_owner("owner stopped") == 1
    thread.join(1)
    assert not thread.is_alive()
    assert result == []
    assert len(errors) == 1
    assert isinstance(errors[0], TimeoutError)
    assert "owner stopped" in str(errors[0])
    assert queue.wait_until_ready(0) is False
