"""Tests for the C++ native ConsolePanel and Python Debug bridge."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from Infernux.debug import Debug, DebugConsole, LogEntry, LogType
from Infernux.lib import ConsolePanel, LogLevel, inflog_internal


# ═══════════════════════════════════════════════════════════════════════════
# Fixture
# ═══════════════════════════════════════════════════════════════════════════

@pytest.fixture
def native_console():
    """Create a fresh C++ ConsolePanel and wire it to a clean DebugConsole."""
    dc = DebugConsole()
    panel = ConsolePanel()
    dc.set_native_console(panel)
    yield panel
    dc.set_native_console(None)
    dc.clear()
    DebugConsole._instance = None


# ═══════════════════════════════════════════════════════════════════════════
# Basic C++ ConsolePanel
# ═══════════════════════════════════════════════════════════════════════════

class TestNativeConsolePanel:
    def test_creation(self):
        panel = ConsolePanel()
        assert panel is not None

    def test_initial_counts_zero(self):
        panel = ConsolePanel()
        assert panel.get_info_count() == 0
        assert panel.get_warning_count() == 0
        assert panel.get_error_count() == 0

    def test_log_from_python_info(self):
        panel = ConsolePanel()
        panel.log_from_python(LogLevel.Info, "hello")
        assert panel.get_info_count() == 1
        assert panel.get_warning_count() == 0
        assert panel.get_error_count() == 0

    def test_log_from_python_warning(self):
        panel = ConsolePanel()
        panel.log_from_python(LogLevel.Warn, "careful")
        assert panel.get_warning_count() == 1

    def test_log_from_python_error(self):
        panel = ConsolePanel()
        panel.log_from_python(LogLevel.Error, "oops")
        assert panel.get_error_count() == 1

    def test_clear_resets_counts(self):
        panel = ConsolePanel()
        panel.log_from_python(LogLevel.Info, "a")
        panel.log_from_python(LogLevel.Warn, "b")
        panel.log_from_python(LogLevel.Error, "c")
        panel.clear()
        assert panel.get_info_count() == 0
        assert panel.get_warning_count() == 0
        assert panel.get_error_count() == 0

    def test_remove_entries_from_source_is_selective(self, tmp_path):
        panel = ConsolePanel()
        script = tmp_path / "Player.py"
        other = tmp_path / "Other.py"
        panel.log_from_python(LogLevel.Error, "old error", source_file=str(script))
        panel.log_from_python(LogLevel.Warn, "old warning", source_file=str(script))
        panel.log_from_python(LogLevel.Error, "other error", source_file=str(other))

        assert panel.remove_entries_from_source(str(script)) == 2
        assert panel.get_info_count() == 0
        assert panel.get_warning_count() == 0
        assert panel.get_error_count() == 1
        visible = panel._get_visible_log_snapshot(10)
        assert [entry["message"] for entry in visible] == ["other error"]

    def test_large_pending_log_counts_are_incremental(self):
        panel = ConsolePanel()
        for index in range(20000):
            panel.log_from_python(LogLevel.Info, f"entry {index}")
        panel.log_from_python(LogLevel.Warn, "warning")
        panel.log_from_python(LogLevel.Error, "error")

        assert panel.get_info_count() == 20000
        assert panel.get_warning_count() == 1
        assert panel.get_error_count() == 1

    def test_filter_properties(self):
        panel = ConsolePanel()
        assert panel.show_info is True
        assert panel.show_warnings is True
        assert panel.show_errors is True
        assert panel.collapse is False
        assert panel.clear_on_play is True
        assert panel.error_pause is False
        assert panel.auto_scroll is True

        panel.show_info = False
        assert panel.show_info is False

    def test_is_open_default(self):
        panel = ConsolePanel()
        assert panel.is_open() is True
        panel.set_open(False)
        assert panel.is_open() is False

    def test_dynamic_attr(self):
        """Ensure py::dynamic_attr() works (needed by WindowManager)."""
        panel = ConsolePanel()
        panel._window_type_id = "console"
        assert panel._window_type_id == "console"

    def test_cpp_internal_log_does_not_surface(self):
        panel = ConsolePanel()
        inflog_internal("internal noise")
        assert panel.get_info_count() == 0
        assert panel.get_warning_count() == 0
        assert panel.get_error_count() == 0

    def test_console_toolbar_exposes_cached_text_search(self):
        source = Path("cpp/infernux/function/editor/ConsolePanel.cpp").read_text(encoding="utf-8")

        assert 'InputTextWithHint("##ConsoleSearch"' in source
        assert "MatchesCurrentFilters" in source
        assert "if (!m_cacheDirty && !m_filterDirty)" in source

    def test_console_commands_pass_source_before_payload(self):
        source = Path("cpp/infernux/function/editor/ConsolePanel.cpp").read_text(
            encoding="utf-8"
        )

        assert 'ExecuteEditorCommand("console.set_option", "pointer"' in source
        assert 'ExecuteEditorCommand("console.set_search", "inline_edit"' in source
        assert 'ExecuteEditorCommand("console.set_detail_height", "drag", argument)' in source
        assert 'ExecuteEditorCommand("console.set_option", std::string' not in source
        assert 'ExecuteEditorCommand("console.set_search", m_searchEditStart' not in source

    def test_console_user_selection_is_intent_only_until_global_projection(self):
        source = Path("cpp/infernux/function/editor/ConsolePanel.cpp").read_text(encoding="utf-8")
        select_uid = source[
            source.index("void ConsolePanel::SelectUid") : source.index("void ConsolePanel::PublishSelection")
        ]
        render_row = source[
            source.index("void ConsolePanel::RenderRow") : source.index("const ImVec4 &ConsolePanel::LevelColor")
        ]

        assert "m_selectedUid = uid" not in select_uid
        assert "m_selectedUid = ve.uid" not in render_row
        assert "PublishSelection(uid, recordHistory)" in select_uid
        assert "PublishSelection(ve.uid, true)" in render_row
        assert "ImGui::SetWindowFocus" not in select_uid

    def test_console_row_identity_is_not_truncated_by_visible_log_text(self):
        source = Path("cpp/infernux/function/editor/ConsolePanel.cpp").read_text(encoding="utf-8")
        render_row = source[
            source.index("void ConsolePanel::RenderRow") : source.index("const ImVec4 &ConsolePanel::LevelColor")
        ]

        assert "const std::string label" in render_row
        assert "std::to_string" in render_row
        assert "char label[" not in render_row


# ═══════════════════════════════════════════════════════════════════════════
# Debug → C++ bridge
# ═══════════════════════════════════════════════════════════════════════════

class TestDebugBridge:
    def test_debug_log_reaches_native(self, native_console):
        Debug.log("bridge test")
        assert native_console.get_info_count() == 1

    def test_debug_warning_reaches_native(self, native_console):
        Debug.log_warning("bridge warn")
        assert native_console.get_warning_count() == 1

    def test_debug_error_reaches_native(self, native_console):
        Debug.log_error("bridge error")
        assert native_console.get_error_count() == 1

    def test_debug_exception_reaches_native(self, native_console):
        try:
            raise ValueError("test exception")
        except ValueError as e:
            Debug.log_exception(e)
        assert native_console.get_error_count() == 1

    def test_disconnected_bridge_no_crash(self):
        """After disconnecting, Debug.log() should not crash."""
        dc = DebugConsole()
        panel = ConsolePanel()
        dc.set_native_console(panel)
        Debug.log("before disconnect")
        dc.set_native_console(None)
        Debug.log("should not crash")
        assert panel.get_info_count() == 1
        dc.clear()
        DebugConsole._instance = None

    def test_debug_internal_does_not_reach_native(self, native_console):
        Debug.log_internal("hidden internal")
        assert native_console.get_info_count() == 0
        assert native_console.get_warning_count() == 0
        assert native_console.get_error_count() == 0
