from __future__ import annotations

import ast
import hashlib
import inspect
import os
from pathlib import Path

from Infernux.mcp import session
from Infernux.mcp.tools import capture


_FORBIDDEN_CAPTURE_IMPORTS = {
    "PIL",
    "ImageGrab",
    "mss",
    "pyautogui",
    "pyscreenshot",
    "win32gui",
    "win32ui",
}

_FORBIDDEN_NATIVE_CAPTURE_SYMBOLS = {
    "#include <windows.h>",
    "BitBlt",
    "PrintWindow",
    "GetWindowDC",
    "GetDC(",
    "CreateCompatibleDC",
    "CreateCompatibleBitmap",
    "DXGI_OUTDUPL",
    "DuplicateOutput",
}


class _FakeMcp:
    def __init__(self) -> None:
        self.tools: dict[str, object] = {}

    def tool(self, *args, **kwargs):
        name = str(kwargs.get("name") or (args[0] if args else ""))

        def decorator(fn):
            self.tools[name] = fn
            return fn

        return decorator


class _FakeNative:
    def __init__(self) -> None:
        self.output_path = ""
        self.cancelled: list[int] = []
        self.pick_request = ()

    def request_capture(self, source: str, output_path: str) -> int:
        assert source in {"scene", "game"}
        self.output_path = output_path
        return 17

    def query_capture(self, capture_id: int) -> dict:
        assert capture_id == 17
        return {
            "capture_id": 17,
            "source": "game",
            "status": "completed",
            "source_generation": 4,
            "engine_frame": 901,
            "width": 320,
            "height": 180,
            "output_path": self.output_path,
            "error": "",
        }

    def cancel_capture(self, capture_id: int) -> bool:
        self.cancelled.append(capture_id)
        return True

    def request_scene_object_pick(self, x, y, width, height) -> int:
        self.pick_request = (x, y, width, height)
        return 23

    def query_scene_object_pick(self, request_id: int) -> dict:
        assert request_id == 23
        return {
            "status": "completed",
            "object_id": 987654321,
            "error": "",
        }


def _config(*, recording_enabled: bool, build_profile: str = "debug_feedback") -> dict:
    return {
        "profile": "global_validation",
        "session": {
            "build_profile": build_profile,
            "recording_enabled": recording_enabled,
        },
    }


def _register(tmp_path, monkeypatch, *, recording_enabled=True, build_profile="debug_feedback"):
    session.configure(str(tmp_path), _config(recording_enabled=recording_enabled, build_profile=build_profile))
    native = _FakeNative()
    monkeypatch.setattr(capture, "_native_editor_engine", lambda: native)
    monkeypatch.setattr(capture, "main_thread", lambda _name, fn, **_kwargs: {"ok": True, "data": fn()})
    mcp = _FakeMcp()
    capture.register_capture_tools(mcp, str(tmp_path))
    return mcp, native


def test_capture_requires_debug_recording_opt_in(tmp_path, monkeypatch):
    disabled, _ = _register(tmp_path, monkeypatch, recording_enabled=False)
    result = disabled.tools["capture_request"]("game", "")
    assert result["ok"] is False
    assert result["error"]["code"] == "error.recording_disabled"

    release, _ = _register(tmp_path, monkeypatch, recording_enabled=True, build_profile="release_exploration")
    result = release.tools["capture_request"]("game", "")
    assert result["ok"] is False
    assert result["error"]["code"] == "error.capture_unavailable"


def test_capture_uses_session_review_path_and_never_returns_pixels(tmp_path, monkeypatch):
    mcp, native = _register(tmp_path, monkeypatch)

    requested = mcp.tools["capture_request"]("game", "race finish.png")
    assert requested["ok"] is True
    assert requested["data"]["capture_id"] == 17
    assert requested["data"]["pixel_origin"] == "engine_render_target"
    assert requested["data"]["os_capture_fallback"] is False
    assert requested["data"]["pixel_access"] is False
    assert requested["data"]["artifact_uri"].startswith("review/")
    assert os.path.commonpath([session.current().artifact_root, native.output_path]) == session.current().artifact_root
    assert native.output_path.endswith(os.path.join("review", "race-finish.png"))

    payload = b"engine-render-target-png"
    with open(native.output_path, "wb") as stream:
        stream.write(payload)
    on_main_thread = False

    def run_main(_name, fn, **_kwargs):
        nonlocal on_main_thread
        on_main_thread = True
        try:
            return {"ok": True, "data": fn()}
        finally:
            on_main_thread = False

    original_sha256 = capture._sha256_file

    def hash_off_main(path):
        assert on_main_thread is False
        return original_sha256(path)

    monkeypatch.setattr(capture, "main_thread", run_main)
    monkeypatch.setattr(capture, "_sha256_file", hash_off_main)
    status = mcp.tools["capture_status"](17)
    assert status["data"]["terminal"] is True
    assert status["data"]["byte_size"] == len(payload)
    assert status["data"]["sha256"] == hashlib.sha256(payload).hexdigest()
    assert status["data"]["pixel_origin"] == "engine_render_target"
    assert status["data"]["os_capture_fallback"] is False
    assert "output_path" not in status["data"]
    assert "pixels" not in status["data"]


def test_capture_cancel_routes_to_native_service(tmp_path, monkeypatch):
    mcp, native = _register(tmp_path, monkeypatch)

    result = mcp.tools["capture_cancel"](17)

    assert result == {"ok": True, "data": {"capture_id": 17, "cancelled": True}}
    assert native.cancelled == [17]


def test_scene_gpu_pick_uses_normalized_render_target_coordinates(tmp_path, monkeypatch):
    mcp, native = _register(tmp_path, monkeypatch, recording_enabled=False)

    requested = mcp.tools["scene_pick_request"](0.25, 0.75, 800, 600)
    assert requested["ok"] is True
    assert requested["data"]["request_id"] == 23
    assert requested["data"]["selection_changed"] is False
    assert native.pick_request == (199.75, 449.25, 800.0, 600.0)

    status = mcp.tools["scene_pick_status"](23)
    assert status["data"]["status"] == "completed"
    assert status["data"]["object_id"] == 987654321
    assert status["data"]["terminal"] is True
    assert status["data"]["selection_changed"] is False


def test_scene_gpu_pick_rejects_invalid_coordinates(tmp_path, monkeypatch):
    mcp, _native = _register(tmp_path, monkeypatch)

    for args in [(-0.1, 0.5, 800, 600), (0.5, 1.1, 800, 600), (0.5, 0.5, 0, 600)]:
        try:
            mcp.tools["scene_pick_request"](*args)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid Scene pick arguments were accepted: {args}")


def test_capture_module_cannot_import_screen_capture_backends():
    tree = ast.parse(inspect.getsource(capture))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])

    assert imported_roots.isdisjoint(_FORBIDDEN_CAPTURE_IMPORTS)


def test_capture_metadata_does_not_require_visible_editor_view(monkeypatch):
    metadata: dict[str, dict] = {}
    monkeypatch.setattr(capture, "register_tool_metadata", lambda name, **kwargs: metadata.setdefault(name, kwargs))

    capture._register_metadata()

    request = metadata["capture_request"]
    assert "Game source requires a scene camera" in request["preconditions"]
    assert all("Open the matching" not in message for message in request["recovery"])


def test_native_capture_service_cannot_use_platform_screen_capture():
    repo_root = Path(__file__).resolve().parents[2]
    capture_sources = [
        repo_root / "cpp/infernux/function/renderer/CaptureService.h",
        repo_root / "cpp/infernux/function/renderer/CaptureService.cpp",
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in capture_sources)

    assert not [symbol for symbol in _FORBIDDEN_NATIVE_CAPTURE_SYMBOLS if symbol in source]
    assert "ImageReadbackTicket" in source
    assert "record.snapshot.engineFrame = engineFrame;" in source
    assert "DisplayFloatToUnorm8" in source
    assert "std::pow(value, 1.0F / 2.4F)" not in source
    assert "value / (1.0F + value)" not in source

    renderer_source = (repo_root / "cpp/infernux/function/renderer/InxRenderer.cpp").read_text(encoding="utf-8")
    assert "RequestRenderTargetReadback(gameView), m_frameCount" in renderer_source

    resource_manager_source = (
        repo_root / "cpp/infernux/function/renderer/vk/VkResourceManager.cpp"
    ).read_text(encoding="utf-8")
    readback_body = resource_manager_source.split(
        "std::shared_ptr<ImageReadbackTicket> VkResourceManager::BeginImageReadback", 1
    )[1].split("GraphicsImageReadbackRecorder VkResourceManager::BeginGraphicsImageReadback", 1)[0]
    assert "BeginGraphicsImageReadback(width, height, format)" in readback_body
    assert "return recorder.Submit();" in readback_body
    assert "m_asyncReadback->EndAsync" not in readback_body
