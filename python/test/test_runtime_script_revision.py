from __future__ import annotations

import pytest

from Infernux.engine.path_utils import path_key
from Infernux.engine.runtime_script_revision import ScriptRevisionJournal


def test_generation_is_monotonic_and_duplicate_content_is_coalesced(tmp_path):
    journal = ScriptRevisionJournal()
    path = tmp_path / "controller.py"

    first = journal.request(str(path), "value = 1")
    duplicate = journal.request(str(path), "value = 1")
    second = journal.request(str(path), "value = 2")

    assert first is not None
    assert duplicate is None
    assert second is not None
    assert first.revision.path == str(path)
    assert first.revision.identity_key == path_key(path)
    assert second.generation == first.generation + 1
    assert second.revision.content_hash != first.revision.content_hash


def test_force_new_generation_recompiles_identical_source(tmp_path):
    journal = ScriptRevisionJournal()
    path = tmp_path / "controller.py"

    first = journal.request(str(path), "value = 1")
    duplicate = journal.request(str(path), "value = 1")
    forced = journal.request(str(path), "value = 1", force_new_generation=True)

    assert first is not None
    assert duplicate is None
    assert forced is not None
    assert forced.generation == first.generation + 1
    assert forced.revision.content_hash == first.revision.content_hash


def test_force_alias_and_conflicting_force_flags_are_explicit(tmp_path):
    journal = ScriptRevisionJournal()
    path = tmp_path / "controller.py"

    first = journal.request(str(path), "value = 1")
    forced = journal.request(str(path), "value = 1", force=True)

    assert first is not None and forced is not None
    assert forced.generation == first.generation + 1
    with pytest.raises(ValueError, match="disagree"):
        journal.request(
            str(path),
            "value = 1",
            force_new_generation=True,
            force=False,
        )


def test_stale_result_is_dropped_when_a_newer_generation_exists(tmp_path):
    journal = ScriptRevisionJournal()
    path = tmp_path / "controller.py"

    first = journal.request(str(path), "value = 'A'")
    second = journal.request(str(path), "value = 'B'")
    third = journal.request(str(path), "value = 'C'")

    assert first is not None and second is not None and third is not None
    assert journal.complete(first, succeeded=True) is False
    assert journal.complete(second, succeeded=True) is False
    assert journal.complete(third, succeeded=True) is True
    claimed = journal.claim_ready()

    assert [result.request.generation for result in claimed] == [third.generation]
    assert journal.last_known_good(str(path)) is None
    assert journal.commit_published(claimed[0].request) is True
    assert journal.last_known_good(str(path)) == third.revision


def test_failed_candidate_keeps_last_known_good(tmp_path):
    journal = ScriptRevisionJournal()
    path = tmp_path / "controller.py"

    good = journal.request(str(path), "value = 1")
    assert good is not None
    assert journal.complete(good, succeeded=True)
    claimed = journal.claim_ready()
    assert claimed[0].request == good
    assert journal.commit_published(good) is True

    bad = journal.request(str(path), "def broken(:\n")
    assert bad is not None
    assert journal.complete(bad, succeeded=False, messages=("invalid syntax",))
    assert journal.claim_ready() == ()
    assert journal.last_known_good(str(path)) == good.revision
    diagnostic = journal.diagnostic(str(path))
    assert diagnostic is not None
    assert diagnostic.generation == bad.generation
    assert diagnostic.messages == ("invalid syntax",)


def test_success_is_not_published_until_safe_point(tmp_path):
    journal = ScriptRevisionJournal()
    path = tmp_path / "controller.py"

    request = journal.request(str(path), "value = 1")
    assert request is not None
    assert journal.complete(request, succeeded=True)
    assert journal.last_known_good(str(path)) is None

    assert journal.claim_ready(str(path))[0].request == request
    assert journal.last_known_good(str(path)) is None
    assert journal.commit_published(request) is True
    assert journal.last_known_good(str(path)) == request.revision


def test_stale_claim_cannot_commit_after_newer_generation(tmp_path):
    journal = ScriptRevisionJournal()
    path = tmp_path / "controller.py"

    first = journal.request(str(path), "value = 'A'")
    assert first is not None
    assert journal.complete(first, succeeded=True)
    assert journal.claim_ready()[0].request == first

    second = journal.request(str(path), "value = 'B'")
    assert second is not None
    assert journal.commit_published(first) is False
    assert journal.last_known_good(str(path)) is None
    assert journal.complete(second, succeeded=True) is True
    assert journal.claim_ready()[0].request == second
    assert journal.commit_published(second) is True
    assert journal.last_known_good(str(path)) == second.revision


def test_forced_generation_invalidates_old_pending_and_claimed_results(tmp_path):
    journal = ScriptRevisionJournal()
    path = tmp_path / "controller.py"

    pending = journal.request(str(path), "value = 1")
    assert pending is not None
    forced = journal.request(str(path), "value = 1", force_new_generation=True)
    assert forced is not None
    assert journal.complete(pending, succeeded=True) is False
    assert journal.claim_ready() == ()

    assert journal.complete(forced, succeeded=True) is True
    claimed = journal.claim_ready()[0]
    assert journal.commit_published(claimed.request) is True

    old = journal.request(str(path), "value = 2")
    assert old is not None
    assert journal.complete(old, succeeded=True) is True
    old_claim = journal.claim_ready()[0]
    newer = journal.request(str(path), "value = 2", force_new_generation=True)
    assert newer is not None
    assert journal.commit_published(old_claim.request) is False
    assert journal.last_known_good(str(path)) == forced.revision


def test_publish_failure_can_release_claim_without_advancing_lkg(tmp_path):
    journal = ScriptRevisionJournal()
    path = tmp_path / "controller.py"

    request = journal.request(str(path), "value = 1")
    assert request is not None
    assert journal.complete(request, succeeded=True)
    assert journal.claim_ready()[0].request == request
    try:
        raise RuntimeError("publish callback failed")
    except RuntimeError:
        assert journal.release_claim(request) is True

    assert journal.last_known_good(str(path)) is None
    assert journal.commit_published(request) is False


def _journal_ready(journal, path, source):
    request = journal.request(str(path), source)
    assert request is not None
    assert journal.complete(request, succeeded=True)
    return request


def test_batch_claim_and_commit_are_atomic_and_preserve_requested_order(tmp_path):
    journal = ScriptRevisionJournal()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    first = _journal_ready(journal, first_path, "value = 1")
    second = _journal_ready(journal, second_path, "value = 2")

    claimed = journal.claim_ready_batch((str(second_path), str(first_path)))

    assert [item.request for item in claimed] == [second, first]
    assert journal.last_known_good(str(first_path)) is None
    assert journal.last_known_good(str(second_path)) is None
    assert journal.commit_published_batch((second, first)) is True
    assert journal.last_known_good(str(first_path)) == first.revision
    assert journal.last_known_good(str(second_path)) == second.revision


def test_batch_claim_missing_member_does_not_claim_anything(tmp_path):
    journal = ScriptRevisionJournal()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    _journal_ready(journal, first_path, "value = 1")

    assert journal.claim_ready_batch((str(first_path), str(second_path))) == ()
    assert journal.claim_ready(str(first_path))


def test_batch_claim_conflict_does_not_claim_other_members(tmp_path):
    journal = ScriptRevisionJournal()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    first = _journal_ready(journal, first_path, "value = 1")
    _journal_ready(journal, second_path, "value = 2")
    assert journal.claim_ready(str(first_path))[0].request == first

    assert journal.claim_ready_batch((str(first_path), str(second_path))) == ()
    second_claim = journal.claim_ready(str(second_path))
    assert len(second_claim) == 1


def test_batch_commit_stale_member_does_not_advance_any_lkg(tmp_path):
    journal = ScriptRevisionJournal()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    first = _journal_ready(journal, first_path, "value = 1")
    second = _journal_ready(journal, second_path, "value = 2")
    assert journal.claim_ready_batch((str(first_path), str(second_path)))

    newer = journal.request(str(first_path), "value = 3")
    assert newer is not None
    assert journal.commit_published_batch((first, second)) is False
    assert journal.last_known_good(str(first_path)) is None
    assert journal.last_known_good(str(second_path)) is None
    assert journal.release_claim(second) is True


def test_batch_release_failure_does_not_release_valid_claim(tmp_path):
    journal = ScriptRevisionJournal()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    first = _journal_ready(journal, first_path, "value = 1")
    second = _journal_ready(journal, second_path, "value = 2")
    assert journal.claim_ready_batch((str(first_path), str(second_path)))

    wrong = _journal_ready(journal, tmp_path / "wrong.py", "value = 9")
    assert journal.release_claim_batch((first, wrong)) is False
    assert journal.commit_published(first) is True
    assert journal.release_claim(second) is True


def test_batch_discard_clears_pending_and_claimed_without_advancing_lkg(tmp_path):
    journal = ScriptRevisionJournal()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    first = _journal_ready(journal, first_path, "value = 1")
    failed = journal.request(str(second_path), "def broken(:")
    assert failed is not None
    assert journal.complete(failed, succeeded=False, messages=("syntax",))
    assert journal.claim_ready_batch((str(first_path),))

    assert journal.discard_batch((str(first_path), str(second_path))) is True
    assert journal.last_known_good(str(first_path)) is None
    assert journal.last_known_good(str(second_path)) is None
    assert journal.claim_ready() == ()
    assert journal.release_claim(first) is False


def test_batch_discard_missing_member_does_not_clear_anything(tmp_path):
    journal = ScriptRevisionJournal()
    first_path = tmp_path / "first.py"
    second_path = tmp_path / "second.py"
    _journal_ready(journal, first_path, "value = 1")

    assert journal.discard_batch((str(first_path), str(second_path))) is False
    assert journal.claim_ready(str(first_path))
