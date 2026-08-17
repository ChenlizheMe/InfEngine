"""MCP Console source selection and surface-separation contracts."""

from pathlib import Path

from Infernux.mcp.tools import console as console_tools


class _FakeMcp:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        name = kwargs.get("name") or (args[0] if args else "")

        def register(fn):
            self.tools[name] = fn
            return fn

        return register


class _NativeConsole:
    def _get_visible_log_snapshot(self, limit):
        assert limit == 4
        return [
            {
                "time": "12:00:00.000",
                "level": "WARN",
                "message": "real native console warning",
                "source_file": "engine.cpp",
                "source_line": 12,
                "stack_trace": "",
                "uid": 7,
                "latest_uid": 7,
                "count": 1,
            }
        ]

    def get_view_option(self, option):
        return option == "show_warnings"

    def get_search_query(self):
        return ""

    def _get_status_snapshot(self):
        return ("[Profile] frame=1.0ms", "warning", 0, 1, 0, 7)


def _registered(monkeypatch, panel):
    fake = _FakeMcp()
    monkeypatch.setattr(console_tools, "_native_console", lambda: panel)
    monkeypatch.setattr(console_tools, "main_thread", lambda _name, callback: callback())
    console_tools.register_console_tools(fake)
    return fake.tools["console_read"]


def test_console_read_uses_native_console_and_separates_status_bar(monkeypatch):
    read = _registered(monkeypatch, _NativeConsole())

    result = read(limit=4)

    assert result["source"] == "native_console"
    assert result["surface"] == "console"
    assert [entry["message"] for entry in result["entries"]] == [
        "real native console warning"
    ]
    assert result["status_bar"]["surface"] == "status_bar"
    assert result["status_bar"]["message"].startswith("[Profile]")
    assert result["status_bar"]["message"] not in {
        entry["message"] for entry in result["entries"]
    }


def test_console_read_normalizes_warning_filter_for_native_and_python(monkeypatch):
    read = _registered(monkeypatch, _NativeConsole())
    assert len(read(limit=4, levels=["WARNING"])["entries"]) == 1
    assert len(read(limit=4, levels=["WARN"])["entries"]) == 1
    assert read(limit=4, levels=["ERROR"])["entries"] == []


def test_console_read_falls_back_to_python_debug_when_native_snapshot_is_unavailable(monkeypatch):
    from Infernux.debug import DebugConsole, LogEntry, LogType
    from datetime import datetime

    console = DebugConsole()
    console.log(LogEntry("python warning", LogType.WARNING, datetime.now()))
    monkeypatch.setattr(console_tools, "_native_console", lambda: object())
    monkeypatch.setattr(console_tools, "main_thread", lambda _name, callback: callback())
    monkeypatch.setattr(
        console_tools,
        "_native_console",
        lambda: object(),
    )
    fake = _FakeMcp()
    console_tools.register_console_tools(fake)

    result = fake.tools["console_read"](levels=["WARN"])

    assert result["source"] == "python_debug_fallback"
    assert result["entries"][0]["level"] == "WARN"
    assert result["entries"][0]["message"] == "python warning"
    console.clear()
    DebugConsole._instance = None


def test_native_console_snapshot_bridge_is_explicit_and_bounded():
    repository_root = Path(__file__).resolve().parents[2]
    header = (repository_root / "cpp/infernux/function/editor/ConsolePanel.h").read_text(
        encoding="utf-8"
    )
    binding = (repository_root / "cpp/infernux/tools/pybinding/BindingGUI.cpp").read_text(
        encoding="utf-8"
    )

    assert "GetVisibleLogSnapshot(size_t limit)" in header
    assert '"_get_visible_log_snapshot"' in binding
    assert "GetVisibleLogSnapshot(limit)" in binding
