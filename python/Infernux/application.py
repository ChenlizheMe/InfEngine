"""Process-level runtime information exposed to project scripts."""

from __future__ import annotations

import os
import threading
import weakref
from typing import Any

_lock = threading.RLock()
_engine_ref: weakref.ReferenceType | None = None
_runtime_kind = "uninitialized"
_exit_code = 0


def _renderer_state_from_native(native) -> dict[str, Any]:
    if native is None:
        raise RuntimeError("Renderer telemetry requires a running graphical application.")
    frame = dict(getattr(native, "renderer_frame_snapshot", {}) or {})
    return {
        "frame": frame,
        "gpu_residency": dict(getattr(native, "gpu_residency_snapshot", {}) or {}),
        "msaa": dict(getattr(native, "msaa_state", {}) or {}),
        "submission_ready": bool(
            frame.get("game_camera_available")
            and frame.get("game_target_ready")
            and int(frame.get("game_draw_call_count", 0) or 0) > 0
        ),
    }


class Application:
    """Public process-level state shared by Editor and standalone players."""

    @staticmethod
    def is_editor() -> bool:
        with _lock:
            return _runtime_kind == "editor"

    @staticmethod
    def is_player() -> bool:
        with _lock:
            return _runtime_kind == "player"

    @staticmethod
    def data_path() -> str:
        """Return the active project root, or an empty string before startup."""
        from Infernux.engine.project_context import get_project_root

        root = get_project_root()
        return os.path.abspath(root) if root else ""

    @staticmethod
    def persistent_data_path() -> str:
        """Return the stable writable data root for the current application."""
        with _lock:
            is_player = _runtime_kind == "player"
        if is_player:
            packaged_root = os.environ.get("_INFERNUX_PLAYER_DATA_ROOT", "").strip()
            if packaged_root:
                return os.path.abspath(packaged_root)
        return Application.data_path()

    @staticmethod
    def renderer_state() -> dict[str, Any]:
        """Return stable renderer telemetry available in Editor and Player."""
        engine = Application._current_engine()
        native = engine.get_native_engine() if engine is not None else None
        return _renderer_state_from_native(native)

    @staticmethod
    def quit(exit_code: int = 0) -> bool:
        """Request standalone Player shutdown; Editor calls are ignored."""
        global _exit_code
        with _lock:
            if _runtime_kind != "player":
                return False
            engine = _engine_ref() if _engine_ref is not None else None
            if engine is None:
                return False
            _exit_code = max(0, min(255, int(exit_code)))
        engine.request_exit()
        return True

    @staticmethod
    def _bind_engine(engine, runtime_kind: str) -> None:
        global _engine_ref, _runtime_kind, _exit_code
        kind = str(runtime_kind).strip().lower()
        if kind not in {"editor", "player", "headless"}:
            raise ValueError(f"Unsupported application runtime kind: {runtime_kind}")
        with _lock:
            _engine_ref = weakref.ref(engine)
            _runtime_kind = kind
            _exit_code = 0

    @staticmethod
    def _unbind_engine(engine) -> None:
        global _engine_ref, _runtime_kind
        with _lock:
            current = _engine_ref() if _engine_ref is not None else None
            if current is not engine:
                return
            _engine_ref = None
            _runtime_kind = "uninitialized"

    @staticmethod
    def _current_engine():
        with _lock:
            return _engine_ref() if _engine_ref is not None else None

    @staticmethod
    def _requested_exit_code() -> int:
        with _lock:
            return _exit_code
