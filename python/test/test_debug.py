"""Tests for Infernux.debug — Debug logging, DebugConsole, LogEntry."""

from __future__ import annotations

from datetime import datetime
import sys

import pytest

from Infernux.debug import (
    Debug,
    DebugConsole,
    LogEntry,
    LogType,
    _sanitize_text,
)


# ═══════════════════════════════════════════════════════════════════════════
# LogType enum
# ═══════════════════════════════════════════════════════════════════════════

class TestLogType:
    def test_all_members_exist(self):
        assert LogType.LOG
        assert LogType.WARNING
        assert LogType.ERROR
        assert LogType.ASSERT
        assert LogType.EXCEPTION


# ═══════════════════════════════════════════════════════════════════════════
# LogEntry
# ═══════════════════════════════════════════════════════════════════════════

class TestLogEntry:
    def test_creation(self):
        entry = LogEntry(
            message="hello",
            log_type=LogType.LOG,
            timestamp=datetime(2025, 1, 1, 12, 0, 0),
        )
        assert entry.message == "hello"
        assert entry.log_type == LogType.LOG

    def test_formatted_time(self):
        entry = LogEntry(
            message="test",
            log_type=LogType.LOG,
            timestamp=datetime(2025, 1, 1, 14, 30, 15, 123456),
        )
        assert entry.get_formatted_time() == "14:30:15.123"

    def test_icons_for_all_types(self):
        for lt in LogType:
            entry = LogEntry(message="", log_type=lt, timestamp=datetime.now())
            assert isinstance(entry.get_icon(), str)
            assert len(entry.get_icon()) > 0


# ═══════════════════════════════════════════════════════════════════════════
# _sanitize_text
# ═══════════════════════════════════════════════════════════════════════════

class TestSanitizeText:
    def test_none_returns_empty(self):
        assert _sanitize_text(None) == ""

    def test_bytes_decoded(self):
        assert _sanitize_text(b"hello") == "hello"

    def test_null_byte_replaced(self):
        assert "\x00" not in _sanitize_text("abc\x00def")

    def test_normal_string_passthrough(self):
        assert _sanitize_text("normal") == "normal"


# ═══════════════════════════════════════════════════════════════════════════
# DebugConsole
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def console():
    """Provide a fresh DebugConsole (reset singleton)."""
    dc = DebugConsole()
    yield dc
    dc.clear()
    DebugConsole._instance = None


class TestDebugConsole:
    def test_singleton(self, console):
        assert DebugConsole.instance() is console

    def test_add_and_get_entries(self, console):
        entry = LogEntry(message="test", log_type=LogType.LOG, timestamp=datetime.now())
        console.log(entry)
        assert len(console.get_entries()) == 1
        assert console.get_entries()[0].message == "test"

    def test_counters_increment(self, console):
        console.log(LogEntry(message="a", log_type=LogType.LOG, timestamp=datetime.now()))
        console.log(LogEntry(message="b", log_type=LogType.WARNING, timestamp=datetime.now()))
        console.log(LogEntry(message="c", log_type=LogType.ERROR, timestamp=datetime.now()))
        assert console.log_count == 1
        assert console.warning_count == 1
        assert console.error_count == 1

    def test_clear_resets_everything(self, console):
        console.log(LogEntry(message="a", log_type=LogType.LOG, timestamp=datetime.now()))
        console.clear()
        assert len(console.get_entries()) == 0
        assert console.log_count == 0
        assert console.warning_count == 0
        assert console.error_count == 0

    def test_remove_source_entries_preserves_unrelated_history(self, console, tmp_path):
        script = tmp_path / "Player.py"
        other = tmp_path / "Other.py"
        console.log(LogEntry(
            message="old syntax error",
            log_type=LogType.ERROR,
            timestamp=datetime.now(),
            source_file=str(script),
        ))
        console.log(LogEntry(
            message="old warning",
            log_type=LogType.WARNING,
            timestamp=datetime.now(),
            source_file=(str(script).upper() if sys.platform == "win32" else str(script)),
        ))
        console.log(LogEntry(
            message="unrelated",
            log_type=LogType.ERROR,
            timestamp=datetime.now(),
            source_file=str(other),
        ))

        assert console.remove_source_entries(str(script)) == 2
        assert [entry.message for entry in console.get_entries()] == ["unrelated"]
        assert console.log_count == 0
        assert console.warning_count == 0
        assert console.error_count == 1

    def test_max_entries_trim(self, console):
        console._max_entries = 5
        for i in range(10):
            console.log(LogEntry(message=str(i), log_type=LogType.LOG, timestamp=datetime.now()))
        assert len(console.get_entries()) == 5
        assert console.get_entries()[0].message == "5"

    def test_listener_notified(self, console):
        received = []
        console.add_listener(lambda e: received.append(e))
        entry = LogEntry(message="x", log_type=LogType.LOG, timestamp=datetime.now())
        console.log(entry)
        assert len(received) == 1
        assert received[0] is entry

    def test_remove_listener(self, console):
        received = []
        cb = lambda e: received.append(e)
        console.add_listener(cb)
        console.remove_listener(cb)
        console.log(LogEntry(message="x", log_type=LogType.LOG, timestamp=datetime.now()))
        assert len(received) == 0

    def test_filtered_entries(self, console):
        console.log(LogEntry(message="a", log_type=LogType.LOG, timestamp=datetime.now()))
        console.log(LogEntry(message="b", log_type=LogType.WARNING, timestamp=datetime.now()))
        console.log(LogEntry(message="c", log_type=LogType.ERROR, timestamp=datetime.now()))

        logs_only = console.get_filtered_entries(show_logs=True, show_warnings=False, show_errors=False)
        assert len(logs_only) == 1
        assert logs_only[0].message == "a"

        errors_only = console.get_filtered_entries(show_logs=False, show_warnings=False, show_errors=True)
        assert len(errors_only) == 1
        assert errors_only[0].message == "c"


# ═══════════════════════════════════════════════════════════════════════════
# Debug static methods
# ═══════════════════════════════════════════════════════════════════════════

class TestDebugStaticMethods:
    def test_log(self, console):
        Debug.log("hello")
        entries = console.get_entries()
        assert any(e.message == "hello" and e.log_type == LogType.LOG for e in entries)

    def test_log_warning(self, console):
        Debug.log_warning("warn")
        entries = console.get_entries()
        assert any(e.log_type == LogType.WARNING for e in entries)

    def test_log_error(self, console):
        Debug.log_error("err")
        entries = console.get_entries()
        assert any(e.log_type == LogType.ERROR for e in entries)

    def test_log_with_context(self, console):
        ctx = object()
        Debug.log("ctx_test", context=ctx)
        entries = console.get_entries()
        assert entries[-1].context is ctx
