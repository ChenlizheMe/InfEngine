from __future__ import annotations

import pytest

from Infernux.engine.interaction import (
    CommandStatus,
    EditorCommand,
    EditorCommandRegistry,
)


def _raise(message: str):
    raise RuntimeError(message)


def test_can_execute_failure_is_not_reported_as_disabled():
    registry = EditorCommandRegistry()
    registry.register(
        EditorCommand(
            "probe.can_execute",
            lambda _context: True,
            can_execute=lambda _context: _raise("predicate failed"),
        )
    )

    with pytest.raises(RuntimeError, match="predicate failed"):
        registry.can_execute("probe.can_execute")


def test_checked_failure_is_not_reported_as_unchecked():
    registry = EditorCommandRegistry()
    registry.register(
        EditorCommand(
            "probe.checked",
            lambda _context: True,
            is_checked=lambda _context: _raise("checked query failed"),
        )
    )

    with pytest.raises(RuntimeError, match="checked query failed"):
        registry.is_checked("probe.checked")


def test_disabled_reason_failure_is_not_replaced_with_generic_text():
    registry = EditorCommandRegistry()
    registry.register(
        EditorCommand(
            "probe.disabled_reason",
            lambda _context: True,
            can_execute=lambda _context: False,
            disabled_reason=lambda _context: _raise("reason query failed"),
        )
    )

    with pytest.raises(RuntimeError, match="reason query failed"):
        registry.disabled_reason("probe.disabled_reason")


def test_execute_failure_has_one_structured_error_surface():
    registry = EditorCommandRegistry()
    registry.register(
        EditorCommand(
            "probe.execute",
            lambda _context: _raise("handler failed"),
        )
    )

    result = registry.execute("probe.execute")

    assert result.status is CommandStatus.FAILED
    assert result.message == "handler failed"
