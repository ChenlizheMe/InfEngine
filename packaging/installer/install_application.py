from __future__ import annotations

import ctypes
import logging
import os
import shutil
import signal
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable

from installer.payload import (
    HUB_EXECUTABLE,
    HUB_PAYLOAD_ARCHIVE,
    extract_payload_archive,
)
from installer_safety import (
    install_target_error,
    is_recognized_install_dir,
    write_install_marker,
)


_LOGGER = logging.getLogger(__name__)
_SHARED_RESOURCES = Path("InfernuxHubData/Shared")


def _normalized_path(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _is_within(path: str | os.PathLike[str], directory: str | os.PathLike[str]) -> bool:
    normalized_path = _normalized_path(path)
    normalized_directory = _normalized_path(directory)
    try:
        return (
            os.path.commonpath((normalized_path, normalized_directory))
            == normalized_directory
        )
    except ValueError:
        return False


def _directory_has_files(path: Path) -> bool:
    return path.is_dir() and next(path.iterdir(), None) is not None


def _running_installer_is_inside(install_dir: Path) -> bool:
    candidates = [sys.executable]
    if sys.argv and sys.argv[0]:
        candidates.append(sys.argv[0])
    return any(
        os.path.exists(candidate) and _is_within(candidate, install_dir)
        for candidate in candidates
    )


def _windows_pids_for_executable(executable_path: Path) -> list[int]:
    if sys.platform != "win32":
        return []

    from ctypes import wintypes

    process_query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)

    enum_processes = psapi.EnumProcesses
    enum_processes.argtypes = [
        ctypes.POINTER(wintypes.DWORD),
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    enum_processes.restype = wintypes.BOOL

    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE

    query_image_name = kernel32.QueryFullProcessImageNameW
    query_image_name.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    query_image_name.restype = wintypes.BOOL

    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    capacity = 4096
    while True:
        process_ids = (wintypes.DWORD * capacity)()
        bytes_written = wintypes.DWORD()
        if not enum_processes(
            process_ids, ctypes.sizeof(process_ids), ctypes.byref(bytes_written)
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        count = bytes_written.value // ctypes.sizeof(wintypes.DWORD)
        if count < capacity:
            break
        capacity *= 2

    expected = _normalized_path(executable_path)
    result: list[int] = []
    for process_id in process_ids[:count]:
        if not process_id or process_id == os.getpid():
            continue
        process = open_process(process_query_limited_information, False, process_id)
        if not process:
            continue
        try:
            capacity_chars = wintypes.DWORD(32768)
            image_name = ctypes.create_unicode_buffer(capacity_chars.value)
            if query_image_name(process, 0, image_name, ctypes.byref(capacity_chars)):
                if _normalized_path(image_name.value) == expected:
                    result.append(int(process_id))
        finally:
            close_handle(process)
    return result


def _linux_pids_for_executable(executable_path: Path) -> list[int]:
    if sys.platform != "linux":
        return []
    expected = _normalized_path(executable_path)
    result: list[int] = []
    for process_dir in Path("/proc").iterdir():
        if not process_dir.name.isdigit() or int(process_dir.name) == os.getpid():
            continue
        executable = process_dir / "exe"
        try:
            resolved = os.readlink(executable)
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
        if _normalized_path(resolved.removesuffix(" (deleted)")) == expected:
            result.append(int(process_dir.name))
    return result


def _pids_for_executable(executable_path: Path) -> list[int]:
    if sys.platform == "win32":
        return _windows_pids_for_executable(executable_path)
    if sys.platform == "linux":
        return _linux_pids_for_executable(executable_path)
    raise RuntimeError(f"Infernux Hub has no process contract for {sys.platform}")


def _request_windows_close(process_ids: list[int]) -> None:
    if sys.platform != "win32" or not process_ids:
        return

    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    target_processes = set(process_ids)
    wm_close = 0x0010

    enum_windows_proc = ctypes.WINFUNCTYPE(
        wintypes.BOOL, wintypes.HWND, wintypes.LPARAM
    )
    enum_windows = user32.EnumWindows
    enum_windows.argtypes = [enum_windows_proc, wintypes.LPARAM]
    enum_windows.restype = wintypes.BOOL
    get_window_process = user32.GetWindowThreadProcessId
    get_window_process.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    get_window_process.restype = wintypes.DWORD
    post_message = user32.PostMessageW
    post_message.argtypes = [
        wintypes.HWND,
        wintypes.UINT,
        wintypes.WPARAM,
        wintypes.LPARAM,
    ]
    post_message.restype = wintypes.BOOL

    @enum_windows_proc
    def request_close(window: int, _parameter: int) -> bool:
        process_id = wintypes.DWORD()
        get_window_process(window, ctypes.byref(process_id))
        if process_id.value in target_processes:
            post_message(window, wm_close, 0, 0)
        return True

    enum_windows(request_close, 0)


def _terminate_windows_processes(process_ids: list[int]) -> None:
    if sys.platform != "win32" or not process_ids:
        return

    from ctypes import wintypes

    process_terminate = 0x0001
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    open_process = kernel32.OpenProcess
    open_process.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    open_process.restype = wintypes.HANDLE
    terminate_process = kernel32.TerminateProcess
    terminate_process.argtypes = [wintypes.HANDLE, wintypes.UINT]
    terminate_process.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL

    for process_id in process_ids:
        process = open_process(process_terminate, False, process_id)
        if not process:
            continue
        try:
            terminate_process(process, 1)
        finally:
            close_handle(process)


def _wait_for_hub_exit(executable_path: Path, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _pids_for_executable(executable_path):
            return True
        time.sleep(0.1)
    return not _pids_for_executable(executable_path)


def close_running_hub(
    executable_path: str | os.PathLike[str],
    progress: Callable[[str], None] | None = None,
) -> None:
    executable = Path(executable_path)
    process_ids = _pids_for_executable(executable)
    if not process_ids:
        return

    if progress:
        progress("Closing the running Infernux Hub...")
    if sys.platform == "win32":
        _request_windows_close(process_ids)
    else:
        for process_id in process_ids:
            os.kill(process_id, signal.SIGTERM)
    if _wait_for_hub_exit(executable, 8.0):
        return

    remaining = _pids_for_executable(executable)
    if sys.platform == "win32":
        _terminate_windows_processes(remaining)
    else:
        for process_id in remaining:
            os.kill(process_id, signal.SIGKILL)
    if not _wait_for_hub_exit(executable, 5.0):
        raise RuntimeError(
            "The running Infernux Hub could not be closed. Close it manually, then run the installer again."
        )


class HubInstallTransaction:
    """Stage and atomically replace an Infernux Hub application directory."""

    def __init__(
        self,
        payload_dir: str | os.PathLike[str],
        install_dir: str | os.PathLike[str],
        *,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.payload_dir = Path(payload_dir).resolve()
        self.install_dir = Path(install_dir).resolve()
        self.progress = progress
        self._stage_dir: Path | None = None
        self._backup_dir: Path | None = None
        self._activated = False
        self._committed = False
        self._shared_transferred = False

    @property
    def staged_dir(self) -> str:
        if self._stage_dir is None:
            raise RuntimeError("The Hub payload has not been staged yet.")
        return os.fspath(self._stage_dir)

    def __enter__(self) -> HubInstallTransaction:
        return self

    def __exit__(self, _exception_type, _exception, _traceback) -> None:
        if not self._committed:
            self.rollback()

    def prepare(self) -> None:
        if self._stage_dir is not None:
            raise RuntimeError("The Hub payload has already been staged.")
        if not self.payload_dir.is_dir():
            raise RuntimeError(f"Hub payload directory not found: {self.payload_dir}")
        if self.install_dir.exists() and not self.install_dir.is_dir():
            raise RuntimeError(
                f"The install location is not a directory: {self.install_dir}"
            )
        if (
            self.payload_dir == self.install_dir
            or _is_within(self.payload_dir, self.install_dir)
            or _is_within(self.install_dir, self.payload_dir)
        ):
            raise RuntimeError(
                "The embedded Hub payload cannot be installed over its own directory."
            )

        safety_error = install_target_error(os.fspath(self.install_dir))
        if safety_error:
            raise RuntimeError(safety_error)
        if _directory_has_files(self.install_dir) and not is_recognized_install_dir(
            os.fspath(self.install_dir)
        ):
            raise RuntimeError(
                "The selected directory contains files but is not a recognized Infernux Hub installation. "
                "Choose the existing Hub application directory or an empty folder."
            )
        if self.install_dir.exists() and _running_installer_is_inside(self.install_dir):
            raise RuntimeError(
                "The installer is running from inside the Hub installation directory. "
                "Move the installer elsewhere and run it again."
            )

        source_executable = self.payload_dir / HUB_EXECUTABLE
        payload_archive = self.payload_dir / HUB_PAYLOAD_ARCHIVE
        if not source_executable.is_file() and not payload_archive.is_file():
            raise RuntimeError(
                f"The installer payload contains neither {HUB_EXECUTABLE} nor {HUB_PAYLOAD_ARCHIVE}."
            )

        self.install_dir.parent.mkdir(parents=True, exist_ok=True)
        stage_path = tempfile.mkdtemp(
            prefix=f".{self.install_dir.name}.install-",
            dir=os.fspath(self.install_dir.parent),
        )
        self._stage_dir = Path(stage_path)
        if source_executable.is_file():
            shutil.copytree(self.payload_dir, self._stage_dir, dirs_exist_ok=True)
        else:
            extract_payload_archive(payload_archive, self._stage_dir)
        write_install_marker(os.fspath(self._stage_dir))

        if (self._stage_dir / _SHARED_RESOURCES).exists():
            raise RuntimeError("Hub payload must not contain user-owned shared resources")

        staged_executable = self._stage_dir / HUB_EXECUTABLE
        if not staged_executable.is_file():
            raise RuntimeError(f"The staged Hub payload is missing {HUB_EXECUTABLE}.")

    def activate(self) -> None:
        if self._stage_dir is None:
            raise RuntimeError("The Hub payload must be staged before activation.")
        if self._activated:
            raise RuntimeError("The Hub installation has already been activated.")

        existing_executable = self.install_dir / HUB_EXECUTABLE
        close_running_hub(existing_executable, self.progress)

        if self.install_dir.exists():
            self._backup_dir = self.install_dir.parent / (
                f".{self.install_dir.name}.backup-{uuid.uuid4().hex}"
            )
            os.replace(self.install_dir, self._backup_dir)

        try:
            os.replace(self._stage_dir, self.install_dir)
            self._stage_dir = None
            self._activated = True
        except Exception:
            if (
                self._backup_dir is not None
                and self._backup_dir.exists()
                and not self.install_dir.exists()
            ):
                os.replace(self._backup_dir, self.install_dir)
                self._backup_dir = None
            raise

        if self._backup_dir is not None:
            previous_shared = self._backup_dir / _SHARED_RESOURCES
            if previous_shared.exists():
                shared = self.install_dir / _SHARED_RESOURCES
                shared.parent.mkdir(parents=True, exist_ok=True)
                os.replace(previous_shared, shared)
                self._shared_transferred = True

        installed_executable = self.install_dir / HUB_EXECUTABLE
        if not installed_executable.is_file():
            raise RuntimeError(
                "The installed Infernux Hub executable is missing after activation."
            )

    def commit(self) -> None:
        if not self._activated:
            raise RuntimeError(
                "The Hub installation must be activated before it can be committed."
            )
        self._committed = True
        self._shared_transferred = False
        if self._backup_dir is not None:
            try:
                shutil.rmtree(self._backup_dir)
            except OSError as exc:
                _LOGGER.warning(
                    "Could not remove old Hub installation backup at %s: %s",
                    self._backup_dir,
                    exc,
                )
            self._backup_dir = None

    def rollback(self) -> None:
        if self._shared_transferred:
            # Return the same tree, including any runtime installed during
            # activation. Never delete shared data with the failed app image.
            os.replace(
                self.install_dir / _SHARED_RESOURCES,
                self._backup_dir / _SHARED_RESOURCES,
            )
            self._shared_transferred = False
        if self._activated and self.install_dir.exists():
            if self._backup_dir is not None and self._backup_dir.exists():
                failed_dir = (
                    self.install_dir.parent
                    / f".{self.install_dir.name}.failed-{uuid.uuid4().hex}"
                )
                os.replace(self.install_dir, failed_dir)
                os.replace(self._backup_dir, self.install_dir)
                self._backup_dir = None
                shutil.rmtree(failed_dir, ignore_errors=True)
            else:
                shutil.rmtree(self.install_dir, ignore_errors=True)
            self._activated = False
        elif (
            self._backup_dir is not None
            and self._backup_dir.exists()
            and not self.install_dir.exists()
        ):
            os.replace(self._backup_dir, self.install_dir)
            self._backup_dir = None

        if self._stage_dir is not None:
            shutil.rmtree(self._stage_dir, ignore_errors=True)
            self._stage_dir = None
