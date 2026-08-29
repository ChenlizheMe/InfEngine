from __future__ import annotations

import sys
import time
import io
from pathlib import Path


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from PySide6.QtWidgets import QApplication

from splash_screen import EngineSplashScreen
import splash_screen
import viewmodel.control_pane_viewmodel as control_pane_viewmodel
from viewmodel.control_pane_viewmodel import LaunchPreparationWorker


class _FinishedProcess:
    returncode = 1

    @staticmethod
    def poll():
        return 1


class _RunningProcess:
    @staticmethod
    def poll():
        return None


def _app():
    return QApplication.instance() or QApplication([])


def test_loading_marker_does_not_hide_process_failure(tmp_path: Path):
    _app()
    splash = EngineSplashScreen("", "Test")
    ready = tmp_path / "ready.flag"
    ready.write_text("LOADING:1/3:Loading", encoding="utf-8")
    splash._ready_file = str(ready)
    splash._process = _FinishedProcess()
    splash._launch_started_at = time.monotonic()
    failures = []
    splash._show_failure = lambda title, detail: failures.append((title, detail))

    splash._poll_launch_state()

    assert failures
    splash._spin_timer.stop()


def test_running_process_without_ready_signal_times_out(tmp_path: Path):
    _app()
    splash = EngineSplashScreen("", "Test")
    splash._ready_file = str(tmp_path / "missing.flag")
    splash._process = _RunningProcess()
    splash._launch_started_at = time.monotonic() - splash._STARTUP_TIMEOUT_SECONDS - 1
    timed_out = []
    splash._show_timeout = lambda: timed_out.append(True)

    splash._poll_launch_state()

    assert timed_out == [True]
    splash._spin_timer.stop()


def test_launch_lock_tracks_engine_pid_not_hub_pid(tmp_path: Path, monkeypatch):
    _app()
    (tmp_path / "ProjectSettings").mkdir()

    class Process:
        pid = 424242
        stderr = io.BytesIO()

        @staticmethod
        def poll():
            return None

        @staticmethod
        def terminate():
            return None

    captured = []
    monkeypatch.setattr(splash_screen.subprocess, "Popen", lambda *_args, **_kwargs: Process())
    monkeypatch.setattr(
        splash_screen,
        "write_project_lock",
        lambda project, pid, token, mode, state: captured.append((project, pid, state)),
    )
    monkeypatch.setattr(splash_screen, "remove_project_lock", lambda *_args, **_kwargs: None)
    splash = EngineSplashScreen("", "Test")

    splash.launch(sys.executable, "pass", str(tmp_path), detached=True)

    assert captured == [(str(tmp_path), 424242, "launching")]
    splash._poll_timer.stop()
    splash._spin_timer.stop()


def test_frozen_launch_preparation_does_not_cold_start_python_twice(
    tmp_path: Path,
    monkeypatch,
):
    runtime_python = tmp_path / ".runtime" / "python312" / "python.exe"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"")

    class VersionManager:
        @staticmethod
        def read_project_version(_path):
            return "0.3.7"

        @staticmethod
        def is_installed(_version, _python_version=None):
            return True

    monkeypatch.setattr(control_pane_viewmodel, "is_frozen", lambda: True)
    monkeypatch.setattr(control_pane_viewmodel, "is_project_open", lambda _path: False)
    monkeypatch.setattr(
        control_pane_viewmodel.ProjectModel,
        "_get_project_python",
        staticmethod(lambda _path: str(runtime_python)),
    )
    monkeypatch.setattr(
        control_pane_viewmodel.ProjectModel,
        "validate_python_runtime",
        staticmethod(
            lambda _path: (_ for _ in ()).throw(
                AssertionError("the editor process is the native import check")
            )
        ),
    )

    worker = LaunchPreparationWorker(object(), VersionManager(), str(tmp_path))
    finished = []
    errors = []
    worker.finished.connect(finished.append)
    worker.error.connect(errors.append)

    worker.run()

    assert errors == []
    assert finished == [str(runtime_python)]
