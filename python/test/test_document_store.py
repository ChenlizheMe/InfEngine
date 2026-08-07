import pytest

from Infernux.core.document_store import (
    DocumentStore,
    capture_document_file_state,
    submit_document_text,
    write_document_text,
)


@pytest.fixture(autouse=True)
def _fresh_document_store():
    DocumentStore.shutdown()
    yield
    DocumentStore.shutdown()


def test_native_store_writes_ordered_generations(tmp_path):
    path = tmp_path / "scene.json"
    store = DocumentStore.instance()

    first = store.submit(str(path), "first")
    first.wait()
    second = store.submit(str(path), "second")
    second.wait()

    assert first.generation + 1 == second.generation
    assert first.is_complete is True
    assert first.status == "succeeded"
    assert path.read_text(encoding="utf-8") == "second"
    metrics = store.get_metrics(str(path))
    assert metrics.latest_submitted_generation == second.generation
    assert metrics.latest_succeeded_generation == second.generation
    assert metrics.latest_failed_generation == 0
    assert metrics.pending_generation == 0
    assert metrics.active_generation == 0


def test_backup_contains_previous_complete_generation(tmp_path):
    path = tmp_path / "scene.json"
    write_document_text(str(path), "first")
    write_document_text(str(path), "second", create_backup=True)

    assert path.read_text(encoding="utf-8") == "second"
    assert (tmp_path / "scene.json.bak").read_text(encoding="utf-8") == "first"


def test_shutdown_drains_accepted_write_and_store_can_restart(tmp_path):
    first_path = tmp_path / "first.json"
    store = DocumentStore.instance()
    ticket = store.submit(str(first_path), "accepted")

    store.shutdown()

    ticket.wait()
    assert first_path.read_text(encoding="utf-8") == "accepted"

    second_path = tmp_path / "second.json"
    restarted = DocumentStore.instance()
    restarted.write_and_wait(str(second_path), "new lifetime")
    assert second_path.read_text(encoding="utf-8") == "new lifetime"


def test_native_write_failure_reaches_ticket(tmp_path):
    missing_parent = tmp_path / "missing" / "failed.json"
    ticket = DocumentStore.instance().submit(str(missing_parent), "data")

    with pytest.raises(RuntimeError, match="atomic write failed"):
        ticket.wait()
    assert ticket.status == "failed"
    assert "atomic write failed" in ticket.error


def test_conditional_write_does_not_overwrite_external_content(tmp_path):
    path = tmp_path / "conditional.json"
    path.write_text("before", encoding="utf-8")
    expected = capture_document_file_state(str(path))
    path.write_text("extern", encoding="utf-8")

    ticket = submit_document_text(
        str(path),
        "editor",
        expected_file_state=expected,
    )

    with pytest.raises(RuntimeError, match="changed outside the editor"):
        ticket.wait()
    assert ticket.status == "failed"
    assert path.read_text(encoding="utf-8") == "extern"
