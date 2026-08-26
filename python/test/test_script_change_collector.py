from __future__ import annotations

import threading

import pytest

from Infernux.engine.script_change_collector import (
    ScriptChangeCollector,
    ScriptFrontendArtifact,
)


def _submit(collector, path, source=b"value = 1\n", **kwargs):
    return collector.submit(str(path), source, origin="editor", **kwargs)


def test_submit_freezes_source_and_preserves_provenance(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    source = bytearray(b"value = 1\n")

    change = _submit(collector, path, source, transaction_id="tx-1", catalog_event="created", change_kind="created")
    source[:] = b"value = 9\n"

    assert change is not None
    assert change.source == b"value = 1\n"
    assert change.origin == "editor"
    assert change.transaction_id == "tx-1"
    assert change.catalog_event == "created"
    assert change.change_kind == "created"
    assert change.merged_count == 1


def test_duplicate_source_is_coalesced_and_metadata_is_merged(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"

    first = _submit(collector, path, transaction_id="tx-a", catalog_event="watch-1")
    second = collector.submit(
        str(path),
        b"value = 1\n",
        origin="watchdog",
        transaction_id="tx-b",
        catalog_event="watch-2",
    )

    assert first is not None
    assert second is None
    assert collector.pending_count == 1
    merged = collector.process_worker_batch()[0].change
    assert merged.merged_count == 2
    assert merged.merged_origins == ("watchdog",)
    assert merged.merged_transaction_ids == ("tx-b",)
    assert merged.merged_catalog_events == ("watch-2",)


def test_effective_catalog_event_preserves_explicit_created_over_watchdog_modified(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"

    first = collector.submit(
        str(path),
        b"value = 1\n",
        origin="automation",
        transaction_id="mcp-tx",
        catalog_event="created",
        change_kind="created",
    )
    duplicate = collector.submit(
        str(path),
        b"value = 1\n",
        origin="watchdog",
        transaction_id="watch-tx",
        catalog_event="modified",
        change_kind="modified",
    )

    assert first is not None
    assert duplicate is None
    merged = collector.process_worker_batch()[0].change
    assert merged.effective_catalog_event == "created"


def test_watcher_echo_with_same_source_is_merged_without_force(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"

    first = collector.submit(
        str(path),
        b"value = 1\n",
        origin="automation",
        transaction_id="mcp-tx",
        catalog_event="created",
        change_kind="created",
    )
    echo = collector.submit(
        str(path),
        b"value = 1\n",
        origin="watchdog",
        transaction_id="watch-tx",
        catalog_event="modified",
        change_kind="modified",
    )

    assert first is not None
    assert echo is None
    result = collector.process_worker_batch()[0]
    assert result.change.generation == first.generation
    assert result.change.merged_count == 2
    assert result.change.effective_catalog_event == "created"


@pytest.mark.parametrize(
    ("canonical_origin", "canonical_tx", "echo_origin", "echo_tx"),
    [
        ("editor", "editor-tx", "watchdog", "watchdog-tx"),
        ("watchdog", "watchdog-tx", "editor", "editor-tx"),
    ],
)
def test_duplicate_echo_keeps_canonical_transaction_ownership_and_cleans_up(
    tmp_path, canonical_origin, canonical_tx, echo_origin, echo_tx
):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"

    first = collector.submit(
        str(path),
        b"value = 1\n",
        origin=canonical_origin,
        transaction_id=canonical_tx,
    )
    echo = collector.submit(
        str(path),
        b"value = 1\n",
        origin=echo_origin,
        transaction_id=echo_tx,
    )

    assert first is not None
    assert echo is None
    result = collector.process_worker_batch()[0]
    assert result.change.transaction_id == canonical_tx
    assert result.change.merged_transaction_ids == (echo_tx,)

    assert collector.claim_ready_batch((str(path),), transaction_id=echo_tx) == ()
    ready = collector.claim_ready_batch((str(path),), transaction_id=canonical_tx)
    assert len(ready) == 1
    assert collector.commit_published_batch(ready, transaction_id=canonical_tx)
    assert collector.last_known_good(str(path)) == first.revision
    assert collector.pending_count == 0
    assert collector.completed_count == 0
    assert collector.drain_completed() == ()


def test_merged_echo_transaction_cannot_claim_or_commit_as_a_different_owner(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"

    first = _submit(collector, path, transaction_id="editor-tx")
    collector.submit(
        str(path),
        b"value = 1\n",
        origin="watchdog",
        transaction_id="watchdog-tx",
    )
    collector.process_worker_batch()

    assert collector.claim_ready_batch((str(path),), transaction_id="watchdog-tx") == ()
    assert collector.claim_ready_batch((str(path),), transaction_id="editor-tx")
    assert first is not None


def test_concurrent_editor_and_watchdog_echoes_publish_once_without_orphans(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    barrier = threading.Barrier(3)
    submitted = []

    def submit(origin, transaction_id):
        barrier.wait()
        submitted.append(
            collector.submit(
                str(path),
                b"value = 1\n",
                origin=origin,
                transaction_id=transaction_id,
            )
        )

    threads = [
        threading.Thread(target=submit, args=("editor", "editor-tx")),
        threading.Thread(target=submit, args=("watchdog", "watchdog-tx")),
    ]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()

    assert sum(value is not None for value in submitted) == 1
    collector.process_worker_batch()
    result = collector.drain_completed()[0]
    canonical_tx = result.change.transaction_id
    echo_tx = "watchdog-tx" if canonical_tx == "editor-tx" else "editor-tx"
    assert echo_tx in result.change.merged_transaction_ids
    ready = collector.claim_ready_batch((str(path),), transaction_id=canonical_tx)
    assert len(ready) == 1
    assert collector.commit_published_batch(ready, transaction_id=canonical_tx)
    assert collector.pending_count == 0
    assert collector.completed_count == 0
    assert collector.last_known_good(str(path)) == result.change.revision


def test_dependency_force_recompiles_same_source_and_old_claim_cannot_publish(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"

    first = collector.submit(
        str(path),
        b"value = 1\n",
        origin="editor",
        catalog_event="created",
        change_kind="created",
    )
    collector.process_worker_batch()
    old_ready = collector.claim_ready()[0]
    forced = collector.submit(
        str(path),
        b"value = 1\n",
        origin="dependency",
        transaction_id="dep-tx",
        catalog_event="dependency",
        change_kind="dependency",
        force_new_generation=True,
    )

    assert first is not None and forced is not None
    assert forced.generation == first.generation + 1
    assert collector.commit_published(old_ready) is False
    assert collector.last_known_good(str(path)) is None

    collector.process_worker_batch()
    ready = collector.claim_ready()[0]
    assert ready.generation == forced.generation
    assert collector.commit_published(ready) is True
    assert collector.last_known_good(str(path)) == forced.revision


@pytest.mark.parametrize(
    ("events", "expected"),
    [
        (("modified",), "modified"),
        (("initial", "dependency"), "initial"),
        ((None,), None),
    ],
)
def test_effective_catalog_event_handles_weak_and_empty_events(tmp_path, events, expected):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    _submit(collector, path, catalog_event=events[0])
    for event in events[1:]:
        collector.submit(
            str(path),
            b"value = 1\n",
            origin="watchdog",
            transaction_id=f"tx-{event}",
            catalog_event=event,
        )

    merged = collector.process_worker_batch()[0].change
    assert merged.effective_catalog_event == expected


def test_default_frontend_compiles_without_importing_or_executing_source(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    change = _submit(path=path, collector=collector, source=b"import helper\nfrom .pkg import Value\n")

    result = collector.process_worker_batch()[0]

    assert result.status == "completed"
    assert isinstance(result.artifact, ScriptFrontendArtifact)
    assert result.artifact.source == change.source
    assert [(item.module, item.level, item.imported) for item in result.artifact.imports] == [
        ("helper", 0, ("helper",)),
        ("pkg", 1, ("Value",)),
    ]
    assert not hasattr(__import__("__main__"), "helper")


def test_frontend_does_not_execute_top_level_code(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    _submit(path=path, collector=collector, source=b"raise RuntimeError('must not execute')\n")

    result = collector.process_worker_batch()[0]

    assert result.status == "failed"
    assert result.artifact is not None
    assert result.artifact.policy_report.is_rejected
    assert result.diagnostics[0].phase == "candidate_policy"
    assert "cannot be proven isolated" in result.diagnostics[0].message
    assert result.artifact is not None


def test_candidate_policy_report_is_attached_without_executing_source(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "policy_probe.py"
    source = b"import helper\nhelper.VALUE = 2\n"
    _submit(path=path, collector=collector, source=source)

    result = collector.process_worker_batch()[0]

    assert result.status == "failed"
    assert result.artifact is not None
    assert result.artifact.source == source
    assert result.artifact.policy_report.is_blocked
    assert result.artifact.policy_report.blocked[0].code == "NX-R1-STATIC-MODULE-WRITE"
    assert result.diagnostics[0].phase == "candidate_policy"
    assert result.diagnostics[0].code == "NX-R1-STATIC-MODULE-WRITE"
    assert result.diagnostics[0].operation == "helper.VALUE"
    assert collector.claim_ready() == ()
    assert collector.last_known_good(str(path)) is None


def test_blocked_candidate_does_not_advance_last_known_good_or_repeat_generation(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "policy_lkg_probe.py"
    valid = _submit(collector, path, source=b"value = 1\n")
    assert valid is not None
    first = collector.process_worker_batch()[0]
    ready = collector.claim_ready()[0]
    assert first.status == "completed"
    assert collector.commit_published(ready)
    assert collector.last_known_good(str(path)) == valid.revision

    blocked_source = b"import helper\nhelper.VALUE = 2\n"
    blocked = collector.submit(
        str(path),
        blocked_source,
        origin="editor",
        transaction_id="blocked-tx",
    )
    assert blocked is not None
    result = collector.process_worker_batch()[0]
    assert result.status == "failed"
    assert collector.claim_ready() == ()
    assert collector.last_known_good(str(path)) == valid.revision
    assert collector.submit(
        str(path),
        blocked_source,
        origin="watchdog",
        transaction_id="watchdog-echo",
    ) is None


def test_unknown_call_fails_closed_and_exact_bytes_are_retained(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "policy_guard_probe.py"
    source = "# coding: utf-8\nvalue = user_factory('中文')\n".encode("utf-8")
    change = _submit(collector, path, source=source)

    result = collector.process_worker_batch()[0]

    assert result.status == "failed"
    assert result.artifact is not None
    assert result.artifact.source == source
    assert result.artifact.policy_report.blocked == ()
    assert result.artifact.policy_report.requires_runtime_guard
    assert result.artifact.policy_report.is_rejected
    assert result.diagnostics[0].phase == "candidate_policy"
    assert "cannot be proven isolated" in result.diagnostics[0].message
    assert collector.claim_ready() == ()
    assert collector.last_known_good(str(path)) is None
    assert change is not None


def test_runtime_guard_candidate_does_not_call_custom_frontend(tmp_path):
    calls = []

    def frontend(source: bytes):
        calls.append(source)
        return "must not be reached"

    collector = ScriptChangeCollector(compile_source=frontend)
    path = tmp_path / "policy_guard_frontend.py"
    _submit(collector, path, source=b"value = user_factory()\n")

    result = collector.process_worker_batch()[0]

    assert result.status == "failed"
    assert result.artifact is not None
    assert result.artifact.payload is None
    assert calls == []
    assert collector.last_known_good(str(path)) is None


def test_numpy_mutator_fails_closed_and_does_not_advance_lkg(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "numpy_policy_probe.py"
    valid = _submit(collector, path, source=b"import numpy as np\nVALUE = np.zeros(2)\n")
    assert valid is not None
    assert collector.process_worker_batch()[0].status == "completed"
    assert collector.commit_published(collector.claim_ready()[0])

    _submit(
        collector,
        path,
        source=b"import numpy as np\nnp.seterr(all='ignore')\n",
        transaction_id="numpy-mutator",
    )
    result = collector.process_worker_batch()[0]

    assert result.status == "failed"
    assert result.artifact is not None
    assert result.artifact.policy_report.is_rejected
    assert collector.claim_ready() == ()
    assert collector.last_known_good(str(path)) == valid.revision


def test_frontend_receives_exact_bytes_and_custom_payload_is_retained(tmp_path):
    received = []

    def frontend(source: bytes):
        received.append(source)
        assert isinstance(source, bytes)
        return ("artifact", len(source))

    collector = ScriptChangeCollector(compile_source=frontend)
    path = tmp_path / "controller.py"
    _submit(collector, path, source=bytearray(b"value = 1\n"))

    result = collector.process_worker_batch()[0]

    assert received == [b"value = 1\n"]
    assert result.artifact is not None
    assert result.artifact.payload == ("artifact", 10)
    assert result.artifact.code is None


def test_syntax_failure_is_structured_and_never_claimed(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    _submit(collector, path, source=b"def broken(:\n")

    result = collector.process_worker_batch()[0]

    assert result.status == "failed"
    assert result.artifact is None
    assert len(result.diagnostics) == 1
    assert result.diagnostic is not None
    assert result.diagnostic.phase == "front_end"
    assert result.diagnostic.line == 1
    assert collector.claim_ready() == ()
    assert collector.completed_count == 1
    assert collector.drain_completed() == (result,)


def test_draining_notifications_does_not_remove_publish_candidate(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    _submit(collector, path)
    result = collector.process_worker_batch()[0]

    assert collector.drain_completed() == (result,)
    ready = collector.claim_ready()
    assert len(ready) == 1
    assert collector.commit_published(ready[0]) is True


def test_lkg_advances_only_after_owner_commit(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    change = _submit(collector, path)
    collector.process_worker_batch()

    ready = collector.claim_ready()
    assert len(ready) == 1
    assert ready[0].ready
    assert collector.last_known_good(str(path)) is None
    assert collector.commit_published(ready[0]) is True
    assert collector.last_known_good(str(path)) == change.revision
    assert collector.commit_published(ready[0]) is False


def test_release_claim_preserves_lkg_and_allows_new_candidate(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    first = _submit(collector, path, source=b"value = 1\n")
    collector.process_worker_batch()
    ready = collector.claim_ready()[0]
    assert collector.release_claim(ready) is True
    assert collector.last_known_good(str(path)) is None

    second = _submit(collector, path, source=b"value = 2\n")
    collector.process_worker_batch()
    ready_again = collector.claim_ready()
    assert second is not None and ready_again[0].generation > first.generation
    assert collector.commit_published(ready_again[0]) is True
    assert collector.last_known_good(str(path)) == second.revision


def test_newer_submission_makes_old_worker_result_stale(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    first = _submit(collector, path, source=b"value = 'A'\n")
    second = _submit(collector, path, source=b"value = 'B'\n")

    assert first is not None and second is not None
    results = collector.process_worker_batch()

    assert [result.status for result in results] == ["completed"]
    assert results[0].change.generation == second.generation
    assert collector.claim_ready()[0].change.generation == second.generation
    assert collector.last_known_good(str(path)) is None


def test_batch_limit_and_counts_are_explicit(tmp_path):
    collector = ScriptChangeCollector()
    for index in range(3):
        _submit(collector, tmp_path / f"{index}.py", source=f"value = {index}\n")

    assert collector.pending_count == 3
    assert len(collector.process_worker_batch(2)) == 2
    assert collector.pending_count == 1
    assert collector.completed_count == 2
    assert len(collector.drain_completed()) == 2
    assert collector.completed_count == 0
    assert len(collector.process_worker_batch()) == 1


def test_drain_completed_can_filter_without_losing_other_paths(tmp_path):
    collector = ScriptChangeCollector()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    _submit(collector, first_path)
    _submit(collector, second_path)
    collector.process_worker_batch()

    selected = collector.drain_completed(str(first_path))
    assert len(selected) == 1
    assert selected[0].change.path == str(first_path)
    remaining = collector.drain_completed()
    assert len(remaining) == 1
    assert remaining[0].change.path == str(second_path)


def test_clear_discards_claims_and_starts_a_fresh_epoch(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    old = _submit(collector, path)
    collector.process_worker_batch()
    ready = collector.claim_ready()[0]
    collector.clear()

    assert collector.pending_count == 0
    assert collector.completed_count == 0
    assert collector.commit_published(ready) is False
    fresh = _submit(collector, path, source=b"value = 2\n")
    assert fresh is not None
    assert fresh.generation == 1
    assert old.generation == 1


def test_shutdown_is_terminal_and_clears_work(tmp_path):
    collector = ScriptChangeCollector()
    _submit(collector, tmp_path / "controller.py")
    collector.shutdown()

    assert collector.is_shutdown
    assert collector.pending_count == 0
    assert collector.completed_count == 0
    assert collector.process_worker_batch() == ()
    with pytest.raises(RuntimeError, match="shut down"):
        _submit(collector, tmp_path / "new.py")


def test_clear_during_frontend_processing_does_not_resurrect_completed_work(tmp_path):
    collector = None

    def frontend(source: bytes):
        collector.clear()
        return source

    collector = ScriptChangeCollector(compile_source=frontend)
    _submit(collector, tmp_path / "controller.py")

    result = collector.process_worker_batch()[0]

    assert result.status == "stale"
    assert collector.pending_count == 0
    assert collector.completed_count == 0
    assert collector.drain_completed() == ()


def test_shutdown_during_frontend_processing_does_not_publish_result(tmp_path):
    collector = None

    def frontend(source: bytes):
        collector.shutdown()
        return source

    collector = ScriptChangeCollector(compile_source=frontend)
    _submit(collector, tmp_path / "controller.py")

    result = collector.process_worker_batch()[0]

    assert result.status == "stale"
    assert collector.is_shutdown
    assert collector.pending_count == 0
    assert collector.completed_count == 0


def test_metadata_validation_is_explicit(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    with pytest.raises(ValueError, match="origin"):
        collector.submit(str(path), b"", origin="unknown")
    with pytest.raises(ValueError, match="unsupported change kind"):
        collector.submit(str(path), b"", origin="editor", change_kind="unknown")
    with pytest.raises(ValueError, match="transaction_id"):
        collector.submit(str(path), b"", origin="editor", transaction_id="")


def test_submission_is_thread_safe_and_duplicate_content_is_queued_once(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    values = []

    def submit_one():
        values.append(_submit(collector, path))

    threads = [threading.Thread(target=submit_one) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(value is not None for value in values) == 1
    assert collector.pending_count == 1
    result = collector.process_worker_batch()[0]
    assert result.change.merged_count == 8


def _collector_results(collector, paths, transaction_id="tx-batch"):
    changes = [
        _submit(
            collector,
            path,
            source=f"value = {index}\n".encode(),
            transaction_id=transaction_id,
        )
        for index, path in enumerate(paths)
    ]
    assert all(change is not None for change in changes)
    collector.process_worker_batch()
    return changes


def test_batch_claim_and_commit_are_atomic_and_preserve_path_order(tmp_path):
    collector = ScriptChangeCollector()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    first, second = _collector_results(collector, (first_path, second_path))

    ready = collector.claim_ready_batch(
        (str(second_path), str(first_path)), transaction_id="tx-batch"
    )

    assert [item.change.path for item in ready] == [str(second_path), str(first_path)]
    assert collector.last_known_good(str(first_path)) is None
    assert collector.commit_published_batch(ready, transaction_id="tx-batch") is True
    assert collector.last_known_good(str(first_path)) == first.revision
    assert collector.last_known_good(str(second_path)) == second.revision


def test_batch_claim_missing_member_does_not_claim_anything(tmp_path):
    collector = ScriptChangeCollector()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    _collector_results(collector, (first_path,))
    _submit(collector, second_path, transaction_id="tx-batch")

    assert collector.claim_ready_batch(
        (str(first_path), str(second_path)), transaction_id="tx-batch"
    ) == ()
    assert len(collector.claim_ready(str(first_path))) == 1


def test_batch_claim_rejects_mixed_transactions_without_partial_claim(tmp_path):
    collector = ScriptChangeCollector()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    _collector_results(collector, (first_path,), transaction_id="tx-a")
    _collector_results(collector, (second_path,), transaction_id="tx-b")

    assert collector.claim_ready_batch(
        (str(first_path), str(second_path)), transaction_id="tx-a"
    ) == ()
    assert len(collector.claim_ready(str(first_path))) == 1
    assert len(collector.claim_ready(str(second_path))) == 1


def test_batch_claim_rejects_failed_result_without_partial_claim(tmp_path):
    collector = ScriptChangeCollector()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    _collector_results(collector, (first_path,), transaction_id="tx-failed")
    _submit(
        collector,
        second_path,
        source=b"def broken(:\n",
        transaction_id="tx-failed",
    )
    collector.process_worker_batch()

    assert collector.claim_ready_batch(
        (str(first_path), str(second_path)), transaction_id="tx-failed"
    ) == ()
    assert len(collector.claim_ready(str(first_path))) == 1


def test_batch_claim_rejects_stale_result_without_partial_claim(tmp_path):
    collector = ScriptChangeCollector()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    _collector_results(collector, (first_path, second_path), transaction_id="tx-stale")
    _submit(collector, first_path, source=b"value = 99\n", transaction_id="tx-stale")

    assert collector.claim_ready_batch(
        (str(first_path), str(second_path)), transaction_id="tx-stale"
    ) == ()
    assert len(collector.claim_ready(str(second_path))) == 1


def test_batch_commit_before_supersede_does_not_commit_other_member(tmp_path):
    collector = ScriptChangeCollector()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    _collector_results(collector, (first_path, second_path), transaction_id="tx-commit")
    ready = collector.claim_ready_batch(
        (str(first_path), str(second_path)), transaction_id="tx-commit"
    )
    _submit(collector, first_path, source=b"value = 100\n", transaction_id="tx-next")

    assert collector.commit_published_batch(ready, transaction_id="tx-commit") is False
    assert collector.last_known_good(str(first_path)) is None
    assert collector.last_known_good(str(second_path)) is None
    assert collector.release_claim(ready[1]) is True


def test_batch_release_failure_does_not_release_valid_claim(tmp_path):
    collector = ScriptChangeCollector()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    _collector_results(collector, (first_path, second_path), transaction_id="tx-release")
    ready = collector.claim_ready_batch(
        (str(first_path), str(second_path)), transaction_id="tx-release"
    )
    wrong_path = tmp_path / "wrong.py"
    wrong = _collector_results(collector, (wrong_path,), transaction_id="tx-release")[0]

    assert collector.release_claim_batch((ready[0], wrong), transaction_id="tx-release") is False
    assert collector.commit_published(ready[0]) is True
    assert collector.release_claim(ready[1]) is True


def test_discard_batch_clears_completed_failed_and_claimed_without_lkg(tmp_path):
    collector = ScriptChangeCollector()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    _collector_results(collector, (first_path,), transaction_id="tx-abort")
    _submit(
        collector,
        second_path,
        source=b"def broken(:\n",
        transaction_id="tx-abort",
    )
    collector.process_worker_batch()
    ready = collector.claim_ready_batch((str(first_path),), transaction_id="tx-abort")

    assert collector.abort_transaction(
        (str(first_path), str(second_path)), transaction_id="tx-abort"
    ) is True
    assert collector.last_known_good(str(first_path)) is None
    assert collector.last_known_good(str(second_path)) is None
    assert collector.completed_count == 0
    assert collector.drain_completed() == ()
    assert collector.claim_ready() == ()
    assert collector.commit_published(ready[0]) is False


def test_discard_batch_validation_failure_does_not_clear_anything(tmp_path):
    collector = ScriptChangeCollector()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    _collector_results(collector, (first_path,), transaction_id="tx-abort")

    assert collector.discard_batch(
        (str(first_path), str(second_path)), transaction_id="tx-abort"
    ) is False
    assert len(collector.claim_ready(str(first_path))) == 1


def test_publish_ready_batch_blocks_submit_until_owner_publish_and_commit_finish(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    first = _submit(collector, path, source=b"value = 1\n", transaction_id="tx-old")
    assert first is not None
    collector.process_worker_batch()
    publish_started = threading.Event()
    submit_started = threading.Event()
    submit_finished = threading.Event()
    submitted = []
    submit_thread = None

    def submit_new_revision():
        submit_started.set()
        submitted.append(
            _submit(
                collector,
                path,
                source=b"value = 2\n",
                transaction_id="tx-new",
            )
        )
        submit_finished.set()

    def publish(ready):
        nonlocal submit_thread
        assert [item.request for item in ready] == [first.request]
        publish_started.set()
        submit_thread = threading.Thread(target=submit_new_revision)
        submit_thread.start()
        assert submit_started.wait(1.0)
        assert not submit_finished.wait(0.1)
        with pytest.raises(RuntimeError, match="active script publication"):
            _submit(
                collector,
                path,
                source=b"recursive = True\n",
                transaction_id="tx-recursive",
            )
        return {"published_generation": ready[0].generation}

    token = collector.publish_ready_batch((str(path),), "tx-old", publish)

    assert publish_started.is_set()
    assert token == {"published_generation": first.generation}
    assert submit_thread is not None
    submit_thread.join(timeout=1.0)
    assert not submit_thread.is_alive()
    assert submit_finished.is_set()
    assert submitted[0] is not None
    assert submitted[0].generation == first.generation + 1
    assert collector.last_known_good(str(path)) == first.revision
    assert collector.pending_count == 1


def test_publish_ready_batch_false_releases_claim_and_does_not_advance_lkg(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    first = _submit(collector, path, transaction_id="tx-fail")
    assert first is not None
    collector.process_worker_batch()

    assert collector.publish_ready_batch(
        (str(path),), "tx-fail", lambda ready: False
    ) is False
    assert collector.last_known_good(str(path)) is None
    assert collector.claim_ready() == ()
    assert collector.completed_count == 0


def test_publish_ready_batch_exception_releases_claim_and_does_not_advance_lkg(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    first = _submit(collector, path, transaction_id="tx-exception")
    assert first is not None
    collector.process_worker_batch()

    def publish(_ready):
        raise RuntimeError("owner publish failed")

    with pytest.raises(RuntimeError, match="owner publish failed"):
        collector.publish_ready_batch((str(path),), "tx-exception", publish)
    assert collector.last_known_good(str(path)) is None
    assert collector.claim_ready() == ()
    assert collector.completed_count == 0


def test_publish_ready_batch_failure_allows_new_revision_to_queue_after_release(tmp_path):
    collector = ScriptChangeCollector()
    path = tmp_path / "controller.py"
    first = _submit(collector, path, transaction_id="tx-old")
    assert first is not None
    collector.process_worker_batch()
    submit_done = threading.Event()
    submitted = []
    submit_thread = None

    def publish(_ready):
        nonlocal submit_thread

        def submit_new():
            submitted.append(
                _submit(
                    collector,
                    path,
                    source=b"value = 2\n",
                    transaction_id="tx-new",
                )
            )
            submit_done.set()

        submit_thread = threading.Thread(target=submit_new)
        submit_thread.start()
        assert not submit_done.wait(0.1)
        return False

    assert collector.publish_ready_batch((str(path),), "tx-old", publish) is False
    assert submit_thread is not None
    submit_thread.join(timeout=1.0)
    assert submit_done.is_set()
    assert submitted[0] is not None
    assert collector.last_known_good(str(path)) is None
    assert collector.process_worker_batch()[0].generation == first.generation + 1
