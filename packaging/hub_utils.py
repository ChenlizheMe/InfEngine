"""Utility helpers shared across the Hub codebase."""

import json
import os
import sys
from enum import Enum


class HubLaunchContext(Enum):
    """Explicit policy boundary between source and installed Hub launches."""

    SOURCE = "source"
    INSTALLED = "installed"

    @classmethod
    def current(cls) -> "HubLaunchContext":
        return cls.INSTALLED if is_frozen() else cls.SOURCE

    @property
    def uses_installed_versions(self) -> bool:
        return self is HubLaunchContext.INSTALLED


def is_frozen() -> bool:
    """Return *True* inside a PyInstaller or Nuitka standalone build."""
    if getattr(sys, "frozen", False):
        return True

    # Nuitka defines ``__compiled__`` on the executable's main module.  It is
    # not guaranteed to copy that marker into imported modules such as this
    # one, so checking only ``globals()`` makes a standalone Hub look like a
    # source checkout.  The launcher then skips packaged-only startup work,
    # including the automatic update check.
    if "__compiled__" in globals():
        return True
    main_module = sys.modules.get("__main__")
    return bool(main_module and "__compiled__" in vars(main_module))


def get_bundle_dir() -> str:
    """Return the directory containing bundled data files."""
    if is_frozen():
        return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    return os.path.dirname(os.path.abspath(__file__))


def get_app_dir() -> str:
    """Return the executable directory for the running Hub."""
    if is_frozen():
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def get_inner_dir() -> str:
    """Return the Hub private data directory.

    In packaged builds this resolves next to the Hub executable. In source mode
    it resolves under packaging/InfernuxHubData so dev runs behave the same way.
    """
    return get_hub_data_dir()


def get_hub_data_dir() -> str:
    """Return the Hub application data directory next to the executable."""
    return os.path.join(get_app_dir(), "InfernuxHubData")


def get_hub_shared_data_dir(app_dir: str | None = None) -> str:
    """Mutable, reusable content beside the Hub, not in its application payload."""
    if app_dir is None:
        configured = os.environ.get("INFERNUX_SHARED_DATA_ROOT", "").strip()
        if configured:
            return os.path.abspath(os.path.expandvars(os.path.expanduser(configured)))
        app_dir = get_app_dir()
    return os.path.abspath(os.path.join(app_dir, "InfernuxHubData", "Shared"))


def get_hub_user_data_dir() -> str:
    """Return the per-user Hub data root shared by source and installed launches."""
    configured = os.environ.get("INFERNUX_DATA_ROOT", "").strip()
    if configured:
        return os.path.abspath(os.path.expandvars(os.path.expanduser(configured)))
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if not local_app_data:
            raise RuntimeError("Infernux Hub requires LOCALAPPDATA on Windows")
        return os.path.join(local_app_data, "InfernuxHub")
    xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    if xdg_data_home:
        return os.path.join(os.path.expanduser(xdg_data_home), "InfernuxHub")
    return os.path.expanduser("~/.local/share/InfernuxHub")


def get_project_lock_path(project_path: str) -> str:
    """Return the lock-file path that marks a project as opened by the engine."""
    return os.path.join(project_path, "ProjectSettings", ".infernux-engine-lock.json")


def is_pid_running(pid: int) -> bool:
    """Return True if *pid* currently exists."""
    if pid <= 0:
        return False

    if sys.platform == "win32":
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        ERROR_INVALID_PARAMETER = 87
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION,
            False,
            pid,
        )
        if not handle:
            error_code = ctypes.windll.kernel32.GetLastError()
            if error_code == ERROR_INVALID_PARAMETER:
                return False
            raise ctypes.WinError(error_code)
        try:
            exit_code = ctypes.c_ulong()
            if not ctypes.windll.kernel32.GetExitCodeProcess(
                handle,
                ctypes.byref(exit_code),
            ):
                raise ctypes.WinError(ctypes.windll.kernel32.GetLastError())
            return exit_code.value == STILL_ACTIVE
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_project_lock(project_path: str) -> dict | None:
    """Return active lock metadata for *project_path*, removing stale locks automatically."""
    lock_path = get_project_lock_path(project_path)
    if not os.path.isfile(lock_path):
        return None

    with open(lock_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"project lock must contain a JSON object: {lock_path}")
    pid = data.get("pid")
    token = data.get("token")
    if (
        isinstance(pid, bool)
        or not isinstance(pid, int)
        or pid <= 0
        or not isinstance(token, str)
        or not token
    ):
        raise ValueError(f"project lock has invalid process identity: {lock_path}")
    if not is_pid_running(pid):
        os.remove(lock_path)
        return None

    return data


def is_project_open(project_path: str) -> bool:
    """Return True if the project currently has a live engine process."""
    return read_project_lock(project_path) is not None


def write_project_lock(project_path: str, pid: int, token: str, mode: str, state: str) -> str:
    """Write/update the project lock file and return its path."""
    lock_path = get_project_lock_path(project_path)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    payload = {
        "pid": pid,
        "token": token,
        "mode": mode,
        "state": state,
        "project_path": os.path.abspath(project_path),
    }
    with open(lock_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    return lock_path


def merge_child_env_utf8(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for subprocesses: inherit current env and prefer UTF-8 on Windows."""
    merged = {**os.environ, **(extra or {})}
    merged.setdefault("INFERNUX_DATA_ROOT", get_hub_user_data_dir())
    merged.setdefault("INFERNUX_SHARED_DATA_ROOT", get_hub_shared_data_dir())
    merged.setdefault(
        "INFERNUX_PACKAGE_CACHE_ROOT",
        os.path.join(merged["INFERNUX_SHARED_DATA_ROOT"], "Library", "Plugins"),
    )
    if not merged.get("PIP_CACHE_DIR", "").strip():
        merged["PIP_CACHE_DIR"] = os.path.join(
            merged["INFERNUX_SHARED_DATA_ROOT"], "Cache", "Python", "Pip"
        )
    if sys.platform == "win32":
        merged.setdefault("PYTHONUTF8", "1")
        merged.setdefault("PYTHONIOENCODING", "utf-8")
    return merged


def remove_project_lock(project_path: str, token: str | None = None) -> None:
    """Remove the project lock if it exists and the token matches when provided."""
    lock_path = get_project_lock_path(project_path)
    if not os.path.isfile(lock_path):
        return

    if token is not None:
        with open(lock_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError(f"project lock must contain a JSON object: {lock_path}")
        current_token = data.get("token")
        if not isinstance(current_token, str) or not current_token:
            raise ValueError(f"project lock has invalid process identity: {lock_path}")
        if current_token != token:
            return

    os.remove(lock_path)
