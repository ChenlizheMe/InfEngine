from Infernux.runtime_utf8 import configure_process_utf8

configure_process_utf8()

import atexit
import importlib
import json
import os
import time
import uuid

# ── Player mode detection ───────────────────────────────────────────
# Set by the Nuitka boot script BEFORE any Infernux imports.
# Guards editor-only imports to keep standalone builds fast and lean.
_PLAYER_MODE = os.environ.get("_INFERNUX_PLAYER_MODE")

from Infernux.lib import InxGUIRenderable, InxGUIContext, TextureLoader, TextureData
from Infernux.debug import Debug
from Infernux import resources as _resources
from .engine import Engine, LogLevel
from .play_mode import PlayModeManager, PlayModeState
from .scene_manager import SceneFileManager
from .path_utils import resolved_path

from .headless import run_headless

_EDITOR_UI_EXPORTS = {
    "MenuBarPanel", "ToolbarPanel", "HierarchyPanel",
    "InspectorPanel", "ConsolePanel", "SceneViewPanel", "GameViewPanel",
    "ProjectPanel", "WindowManager", "TagLayerSettingsPanel", "StatusBarPanel",
    "BuildSettingsPanel", "UIEditorPanel", "EditorPanel", "EditorServices",
    "EditorEventBus", "EditorEvent", "PanelRegistry", "editor_panel",
}


def __getattr__(name: str):
    if name == "ResourcesManager":
        value = importlib.import_module(".resources_manager", __name__).ResourcesManager
    elif name in _EDITOR_UI_EXPORTS:
        value = getattr(importlib.import_module(".ui", __name__), name)
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    globals()[name] = value
    return value


def _signal_engine_loaded() -> None:
    ready_file = os.environ.get("_INFERNUX_READY_FILE", "").strip()
    if ready_file:
        try:
            with open(ready_file, "w", encoding="utf-8") as f:
                f.write("ENGINE_LOADED\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            pass
    print("ENGINE_LOADED", flush=True)


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False

    if os.name == "nt":
        try:
            import ctypes

            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            STILL_ACTIVE = 259
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                pid,
            )
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == STILL_ACTIVE
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
            return False

    try:
        os.kill(pid, 0)
    except OSError as _exc:
        Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        return False
    return True


def _default_lock_path(project_path: str) -> str:
    return os.path.join(project_path, "ProjectSettings", ".infernux-engine-lock.json")


def _remove_project_lock(lock_path: str, token: str) -> None:
    if not lock_path or not os.path.isfile(lock_path):
        return
    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        data = None
    if data and data.get("token") != token:
        return
    last_error = None
    for attempt in range(20):
        try:
            os.remove(lock_path)
            return
        except FileNotFoundError:
            return
        except PermissionError as exc:
            last_error = exc
            if attempt < 19:
                time.sleep(0.05)
        except OSError as exc:
            last_error = exc
            break
    if last_error is not None:
        Debug.log(f"[Suppressed] {type(last_error).__name__}: {last_error}")


def _acquire_project_lock(project_path: str, mode: str) -> tuple[str, str]:
    lock_path = os.environ.get("_INFERNUX_PROJECT_LOCK_PATH", "").strip() or _default_lock_path(project_path)
    token = os.environ.get("_INFERNUX_PROJECT_LOCK_TOKEN", "").strip() or uuid.uuid4().hex

    if os.path.isfile(lock_path):
        try:
            with open(lock_path, "r", encoding="utf-8") as f:
                current = json.load(f)
        except (OSError, json.JSONDecodeError):
            current = None

        if current:
            current_pid = int(current.get("pid", 0) or 0)
            current_token = str(current.get("token", ""))
            if current_pid > 0 and _is_pid_running(current_pid):
                if current_token != token:
                    raise RuntimeError(
                        f"Project is already open in another Infernux process:\n{project_path}"
                    )
            else:
                _remove_project_lock(lock_path, current_token or token)

    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    payload = {
        "pid": os.getpid(),
        "token": token,
        "mode": mode,
        "state": "running",
        "project_path": resolved_path(project_path),
    }
    with open(lock_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)

    atexit.register(_remove_project_lock, lock_path, token)
    return lock_path, token


def release_engine(project_path: str, engine_log_level=LogLevel.Info):
    """Launch Infernux with Unity-style editor layout.

    Delegates to :class:`EditorBootstrap` for structured initialization.
    """
    import time
    from .bootstrap import EditorBootstrap

    from .library_sync import sync_resources
    sync_resources(project_path)
    _resources.activate_library(project_path)

    lock_path, lock_token = _acquire_project_lock(project_path, "editor")
    try:
        bootstrap = EditorBootstrap(project_path, engine_log_level)
        bootstrap.run()

        bootstrap.engine.set_window_icon(_resources.icon_path)

        # Signal the launcher splash to begin its fade-out, then wait for it
        # to finish before revealing the engine window.
        _signal_engine_loaded()
        time.sleep(0.6)

        bootstrap.engine.show()
        bootstrap.engine.run()
    finally:
        try:
            from Infernux.mcp import stop_server
            stop_server()
        except Exception as _exc:
            Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
        _remove_project_lock(lock_path, lock_token)

    # Force-terminate: this is a standalone engine child process.
    # Non-daemon native threads (C++ / watchdog emitters) may otherwise
    # keep the process alive forever, leaking thousands of zombie procs.
    os._exit(0)


def run_player(project_path: str, engine_log_level=LogLevel.Info):
    """Launch Infernux in standalone player mode (no editor chrome).

    Opens the project's first scene from BuildSettings.json, applies the
    display mode from BuildManifest.json (fullscreen borderless or windowed
    with a custom resolution), plays the splash sequence if configured, then
    enters play mode and runs until the window is closed.
    """
    import json
    import time
    from Infernux.application import Application
    from .player_bootstrap import PlayerBootstrap

    # Packaged/standalone games skip the project lock entirely — they
    # have their own self-contained Data folder and should never conflict
    # with an editor instance or another packaged game.
    is_packaged = os.environ.get("_INFERNUX_PLAYER_MODE") == "1"

    # Packaged players already carry Infernux/resources. Only development
    # players mirror those resources into the project's Library directory.
    if not is_packaged:
        from .library_sync import sync_resources
        sync_resources(project_path)
        _resources.activate_library(project_path)

    lock_path = lock_token = None
    if not is_packaged:
        lock_path, lock_token = _acquire_project_lock(project_path, "player")

    try:
        # Read optional BuildManifest for display & splash settings
        manifest_path = os.path.join(project_path, "BuildManifest.json")
        manifest = {}
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8", errors="replace") as _f:
                    manifest = json.load(_f)
            except Exception as _exc:
                Debug.log(f"[Suppressed] {type(_exc).__name__}: {_exc}")
                pass

        display_mode = manifest.get("display_mode", "fullscreen_borderless")
        window_width = manifest.get("window_width", 1920)
        window_height = manifest.get("window_height", 1080)
        window_resizable = manifest.get("window_resizable", True)
        splash_items = manifest.get("splash_items", [])
        game_name = manifest.get("game_name", "")

        bootstrap = PlayerBootstrap(
            project_path, engine_log_level,
            display_mode=display_mode,
            window_width=window_width,
            window_height=window_height,
            splash_items=splash_items,
        )
        bootstrap.run()

        # Set window title to game name (from manifest or folder name)
        title = game_name or os.path.basename(resolved_path(project_path))
        bootstrap.engine.set_window_title(title)

        if display_mode == "fullscreen_borderless":
            bootstrap.engine.set_fullscreen(True)
        else:
            # Windowed mode: don't maximize, respect the specified size
            bootstrap.engine.set_maximized(False)
            bootstrap.engine.set_resizable(window_resizable)

        bootstrap.engine.set_window_icon(_resources.icon_path)

        _signal_engine_loaded()
        time.sleep(0.3)

        bootstrap.engine.show()
        bootstrap.engine.run()
        exit_code = Application._requested_exit_code()
    finally:
        if lock_path and lock_token:
            _remove_project_lock(lock_path, lock_token)

    os._exit(exit_code)

__all__ = [
    "Engine",
    "LogLevel",
    "InxGUIRenderable",
    "InxGUIContext",
    "PlayModeManager",
    "PlayModeState",
    "SceneFileManager",
    "TextureLoader",
    "TextureData",
    "release_engine",
    "run_player",
    "run_headless",
]

if not _PLAYER_MODE:
    __all__ += [
        "ResourcesManager",
        "MenuBarPanel",
        "ToolbarPanel",
        "HierarchyPanel",
        "InspectorPanel",
        "ConsolePanel",
        "SceneViewPanel",
        "GameViewPanel",
        "UIEditorPanel",
        "ProjectPanel",
        "WindowManager",
        "TagLayerSettingsPanel",
        "StatusBarPanel",
        "BuildSettingsPanel",
        # Panel framework
        "EditorPanel",
        "EditorServices",
        "EditorEventBus",
        "EditorEvent",
        "PanelRegistry",
        "editor_panel",
    ]
