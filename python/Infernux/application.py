"""Process-level runtime information exposed to project scripts."""

from __future__ import annotations

import os
import threading
import weakref
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from Infernux.engine.path_utils import is_path_within, resolved_path

_lock = threading.RLock()
_engine_ref: weakref.ReferenceType | None = None
_runtime_kind = "uninitialized"
_exit_code = 0


def _renderer_state_from_native(native) -> dict[str, Any]:
    if native is None:
        raise RuntimeError("Renderer telemetry requires a running graphical application.")
    frame = dict(getattr(native, "renderer_frame_snapshot", {}) or {})
    game_graph_executed = bool(
        frame.get("game_render_graph_current_executed")
        and int(frame.get("game_render_graph_execution_count", 0) or 0) > 0
    )
    return {
        "frame": frame,
        "gpu_residency": dict(getattr(native, "gpu_residency_snapshot", {}) or {}),
        "msaa": dict(getattr(native, "msaa_state", {}) or {}),
        "submission_ready": bool(
            frame.get("game_camera_available")
            and frame.get("game_target_ready")
            and (
                int(frame.get("game_draw_call_count", 0) or 0) > 0
                or game_graph_executed
            )
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
    def is_headless() -> bool:
        """Return whether the process is running the renderer-free host."""
        with _lock:
            return _runtime_kind == "headless"

    @staticmethod
    def data_path() -> str:
        """Return the active project root, or an empty string before startup."""
        from Infernux.engine.project_context import get_project_root

        root = get_project_root()
        return resolved_path(root) if root else ""

    @staticmethod
    def persistent_data_path() -> str:
        """Return the stable writable data root for the current application."""
        with _lock:
            is_player = _runtime_kind == "player"
        if is_player:
            packaged_root = os.environ.get("_INFERNUX_PLAYER_DATA_ROOT", "").strip()
            if packaged_root:
                return resolved_path(packaged_root)
        return Application.data_path()

    @staticmethod
    def asset_path(path: str) -> str:
        """Resolve an ``Assets/...`` reference in Editor or packaged Player."""
        from Infernux.engine.project_context import resolve_asset_path

        resolved = resolve_asset_path(path)
        if not resolved:
            raise FileNotFoundError(f"Project asset is not available at runtime: {path}")
        return resolved

    @staticmethod
    def open_url(target: str) -> bool:
        """Open one absolute local file or HTTP(S) URL with the platform handler.

        Project content must first be resolved with :meth:`asset_path`.  Keeping
        resolution and opening separate makes the exact URL observable in tests
        and avoids silently interpreting arbitrary relative paths.
        """
        value = str(target or "").strip()
        if not value:
            raise ValueError("URL target cannot be empty")
        if os.path.isabs(value):
            local_path = resolved_path(value)
            if not os.path.isfile(local_path):
                raise FileNotFoundError(f"Local URL target is not available: {value}")
            canonical = Path(local_path).as_uri()
        else:
            parsed = urlsplit(value)
            if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
                if parsed.scheme:
                    raise ValueError(
                        "Only absolute local files and HTTP(S) URLs can be opened"
                    )
                raise ValueError(
                    "Local URL target must be absolute; resolve project content with "
                    "Application.asset_path() first"
                )
            canonical = value

        engine = Application._current_engine()
        if engine is None:
            raise RuntimeError("Opening a URL requires a running graphical application")
        if not bool(engine.open_url(canonical)):
            raise RuntimeError(f"The platform URL handler rejected: {canonical}")
        return True

    @staticmethod
    def renderer_state() -> dict[str, Any]:
        """Return stable renderer telemetry available in Editor and Player."""
        engine = Application._current_engine()
        native = engine.get_native_engine() if engine is not None else None
        return _renderer_state_from_native(native)

    @staticmethod
    def request_render_target_capture(source: str, output_path: str) -> int:
        """Queue an engine-native Scene/Game capture under persistent data."""
        normalized_source = str(source).strip().lower()
        if normalized_source not in {"scene", "game"}:
            raise ValueError("Render target capture source must be 'scene' or 'game'")
        root = Application.persistent_data_path()
        if not root:
            raise RuntimeError("Render target capture requires an active project")
        target = resolved_path(output_path)
        if not is_path_within(target, root, allow_root=False):
            raise ValueError("Render target capture output must stay under persistent_data_path")
        engine = Application._current_engine()
        if engine is None:
            raise RuntimeError("Render target capture requires a running graphical application")
        os.makedirs(os.path.dirname(target), exist_ok=True)
        return int(engine.request_capture(normalized_source, target))

    @staticmethod
    def query_render_target_capture(capture_id: int) -> dict[str, Any]:
        """Poll a capture requested with request_render_target_capture()."""
        engine = Application._current_engine()
        if engine is None:
            raise RuntimeError("Render target capture requires a running graphical application")
        return dict(engine.query_capture(int(capture_id)))

    @staticmethod
    def cancel_render_target_capture(capture_id: int) -> bool:
        """Cancel an unfinished render-target capture."""
        engine = Application._current_engine()
        if engine is None:
            return False
        return bool(engine.cancel_capture(int(capture_id)))

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
