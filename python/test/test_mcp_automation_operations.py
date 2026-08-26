from __future__ import annotations

import hashlib
import ast
import threading
from pathlib import Path

from Infernux.host import EditorAutomationHost, MainThreadCommandQueue
from infernux_mcp import session
from infernux_mcp.operations import build_operations


class _AutomationHost(EditorAutomationHost):
    def __init__(self, capture_path: Path):
        self.capture_path = capture_path
        self.events = []
        self.sequence = 0
        self.capture_enabled = False

    def queue_input(self, kind: str, **arguments):
        self.sequence += 1
        self.events.append((kind, arguments))
        return {
            "sequence": self.sequence,
            "last_processed_sequence": self.sequence,
            "pending_event_count": 0,
        }

    def input_status(self):
        return {
            "last_processed_sequence": self.sequence,
            "pending_event_count": 0,
        }

    def semantic_capture_enabled(self, enabled: bool):
        self.capture_enabled = bool(enabled)
        return self.capture_enabled

    def request_semantic_snapshot(self):
        return 9

    def semantic_snapshot(self):
        return {
            "capture_enabled": self.capture_enabled,
            "frame": 27,
            "request_sequence": 9,
            "targets": [
                {
                    "id": "button:main:run:1",
                    "semantic_id": "run",
                    "kind": "button",
                    "label": "Run",
                    "window": "Main",
                    "visible": True,
                },
                {
                    "id": "hidden",
                    "semantic_id": "hidden",
                    "kind": "button",
                    "label": "Hidden",
                    "window": "Main",
                    "visible": False,
                },
            ],
        }

    def request_capture(self, source: str, output_path: str):
        self.capture_path = Path(output_path)
        return 17

    def capture_status(self, capture_id: int):
        self.capture_path.parent.mkdir(parents=True, exist_ok=True)
        self.capture_path.write_bytes(b"engine-render-target")
        return {
            "capture_id": capture_id,
            "status": "completed",
            "output_path": str(self.capture_path),
        }

    def cancel_capture(self, capture_id: int):
        return capture_id == 17

    def console_read(self, limit=100, levels=()):
        return {
            "entries": [{"level": "WARN", "message": "native warning"}],
            "source": "native_console",
            "surface": "console",
            "status_bar": {"surface": "status_bar", "message": "profile"},
        }


def _operations(tmp_path):
    session.configure(
        str(tmp_path),
        {
            "profile": "global_validation",
            "session": {"build_profile": "debug_feedback"},
        },
    )
    queue = MainThreadCommandQueue()
    queue._main_thread_id = threading.get_ident()
    MainThreadCommandQueue._instance = queue
    host = _AutomationHost(tmp_path / "capture.png")
    EditorAutomationHost.set_provider(host)
    values = {item.schema.id: item.handler for item in build_operations(str(tmp_path))}
    return values, host


def test_input_semantic_ui_console_and_docs_are_schema_operations(tmp_path):
    operations, host = _operations(tmp_path)
    try:
        sent = operations["infernux.input.key"]("space", True)
        assert sent["delivered"] is True
        assert host.events == [("key", {"key": "space", "pressed": True, "repeat": False})]

        chord = operations["infernux.input.key.chord"](["ctrl", "s"])
        assert chord["delivered"] is True
        assert [event[1]["pressed"] for event in host.events[-4:]] == [True, True, False, False]
        assert [event[1]["key"] for event in host.events[-4:]] == ["ctrl", "s", "s", "ctrl"]

        clicked = operations["infernux.input.pointer.click"](40.0, 80.0)
        assert clicked["release_sequence"] > clicked["press_sequence"] > clicked["move_sequence"]
        assert [event[0] for event in host.events[-3:]] == [
            "pointer_move",
            "pointer_button",
            "pointer_button",
        ]
        assert operations["infernux.input.wait"](clicked["release_sequence"])["delivered"] is True

        snapshot = operations["infernux.ui.semantic.snapshot"](label="run")
        assert snapshot["frame"] == 27
        assert [item["semantic_id"] for item in snapshot["targets"]] == ["run"]
        waited = operations["infernux.ui.semantic.wait"](semantic_id="run")
        assert waited["matched"] is True

        console = operations["infernux.console.read"](limit=4, levels=["WARN"])
        assert console["entries"][0]["message"] == "native warning"
        assert console["status_bar"]["surface"] == "status_bar"

        docs = operations["infernux.docs.search"]("asset guid")
        assert docs["guides"][0]["id"] == "assets"
    finally:
        EditorAutomationHost.set_provider(None)


def test_capture_returns_review_artifact_metadata_without_pixels(tmp_path):
    operations, host = _operations(tmp_path)
    try:
        requested = operations["infernux.capture.request"]("game", "review.png")
        assert requested["capture_id"] == 17
        assert requested["pixel_origin"] == "engine_render_target"
        assert requested["pixel_access"] is False
        status = operations["infernux.capture.status"](17)
        assert status["terminal"] is True
        assert status["sha256"] == hashlib.sha256(b"engine-render-target").hexdigest()
        assert "output_path" not in status
        assert "pixels" not in status
        assert host.capture_path.name == "review.png"
    finally:
        EditorAutomationHost.set_provider(None)


def test_operation_handlers_depend_on_host_api_not_editor_implementation():
    plugin = (
        Path(__file__).parents[2]
        / "external"
        / "plugins"
        / "infernux_mcp"
        / "Editor"
        / "infernux_mcp"
    )
    forbidden = (
        "Infernux.engine",
        "Infernux.lib",
        "Infernux.core",
        "Infernux.components",
        "Infernux.particle",
    )
    violations = []
    for path in sorted(plugin.glob("*operations.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and str(node.module or "").startswith(forbidden):
                violations.append((path.name, node.lineno, node.module))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name.startswith(forbidden):
                        violations.append((path.name, node.lineno, alias.name))
    assert violations == []


def test_capture_surface_cannot_fall_back_to_operating_system_pixels():
    plugin = (
        Path(__file__).parents[2]
        / "external"
        / "plugins"
        / "infernux_mcp"
        / "Editor"
        / "infernux_mcp"
        / "capture_operations.py"
    )
    tree = ast.parse(plugin.read_text(encoding="utf-8"), filename=str(plugin))
    forbidden_roots = {"PIL", "pyautogui", "mss", "ImageGrab", "win32gui", "win32ui"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            imported.add(str(node.module or "").split(".")[0])
        elif isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
    assert imported.isdisjoint(forbidden_roots)

    native_sources = list((Path(__file__).parents[2] / "cpp").rglob("*Capture*.cpp"))
    native_text = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in native_sources)
    forbidden_native = ("BitBlt(", "PrintWindow(", "GetDC(", "XGetImage(", "CGWindowListCreateImage")
    assert all(symbol not in native_text for symbol in forbidden_native)


def test_player_schema_exposes_managed_input_and_motion_capture(tmp_path, monkeypatch):
    operations, _host = _operations(tmp_path)

    class _Supervisor:
        def player_send_mouse_button(self, button, pressed, x, y, *, timeout_seconds):
            return {"button": button, "pressed": pressed, "x": x, "y": y}

        def player_press_key(self, key, duration_seconds, **kwargs):
            return {"key": key, "duration_seconds": duration_seconds, **kwargs}

        def player_motion_capture_arm(self, object_names, **kwargs):
            return {"capture_id": "motion-1", "object_names": object_names, **kwargs}

        def player_motion_capture_status(self, capture_id, *, timeout_seconds):
            return {"capture_id": capture_id, "status": "sampling"}

        def player_motion_capture_cancel(self, capture_id, *, timeout_seconds):
            return {"capture_id": capture_id, "cancelled": True}

    monkeypatch.setattr(
        "infernux_mcp.player_operations.SupervisorSession.resume",
        lambda *args, **kwargs: _Supervisor(),
    )
    try:
        pointer = operations["infernux.player.validation.pointer.button"](0, True, 10, 20)
        assert pointer == {"button": 0, "pressed": True, "x": 10, "y": 20}
        press = operations["infernux.player.validation.key.press"]("space", 0.2)
        assert press["duration_seconds"] == 0.2
        armed = operations["infernux.player.validation.motion.arm"](["Ball"])
        assert armed["capture_id"] == "motion-1"
        assert operations["infernux.player.validation.motion.status"]("motion-1")["status"] == "sampling"
        assert operations["infernux.player.validation.motion.cancel"]("motion-1")["cancelled"] is True
    finally:
        EditorAutomationHost.set_provider(None)
