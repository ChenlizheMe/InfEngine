from __future__ import annotations

import sys
import time
import io
import subprocess
from pathlib import Path
from types import SimpleNamespace


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

from PySide6.QtWidgets import QApplication
from PySide6.QtWidgets import QMessageBox

from launcher import GameEngineLauncher
from hub_utils import HubLaunchContext
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


def test_stderr_reader_reaps_editor_after_successful_startup():
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stderr.buffer.write(b'editor stopped')"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    owner = SimpleNamespace(_process=process, _stderr_chunks=[])
    try:
        EngineSplashScreen._drain_stderr(owner)
        assert process.returncode == 0
        assert owner._stderr_chunks == [b"editor stopped"]
        assert process.stderr.closed
    finally:
        process.wait(timeout=10)


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


def test_launch_failure_keeps_traceback_in_scrollable_details(monkeypatch):
    _app()
    splash = EngineSplashScreen("", "Test")
    detail = "The engine process exited with an error:\n\n" + "A long traceback line\n" * 80
    observed = []

    def inspect_dialog(box):
        observed.append((box.text(), box.detailedText()))
        return 0

    monkeypatch.setattr(QMessageBox, "exec", inspect_dialog)
    monkeypatch.setattr(splash, "_fade_out_and_close", lambda: None)
    try:
        splash._show_failure("Engine Launch Failed", detail)
        assert observed == [(detail.split("\n", 1)[0], detail)]
    finally:
        splash._spin_timer.stop()
        splash.close()


def test_launch_lock_tracks_engine_pid_not_hub_pid(tmp_path: Path, monkeypatch):
    _app()
    (tmp_path / "ProjectSettings").mkdir()

    class Process:
        pid = 424242
        stderr = io.BytesIO()

        @staticmethod
        def wait():
            return 0

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
    runtime_python = tmp_path / ".runtime" / "python313" / "python.exe"
    runtime_python.parent.mkdir(parents=True)
    runtime_python.write_bytes(b"")

    class VersionManager:
        @staticmethod
        def read_project_version(_path):
            return "0.3.7"

        @staticmethod
        def is_installed(_version, _python_version=None):
            return True

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

    worker = LaunchPreparationWorker(
        object(), VersionManager(), str(tmp_path), HubLaunchContext.INSTALLED
    )
    finished = []
    errors = []
    worker.finished.connect(finished.append)
    worker.error.connect(errors.append)

    worker.run()

    assert errors == []
    assert finished == [str(runtime_python)]


def test_source_launch_preparation_uses_current_python_without_catalog_gate(
    tmp_path: Path, monkeypatch
):
    (tmp_path / "ProjectSettings").mkdir()

    class Model:
        @staticmethod
        def _install_infernux_in_runtime(*_args, **_kwargs):
            raise AssertionError(
                "source launches must not locate or install an Infernux wheel"
            )

        @staticmethod
        def _create_vscode_workspace(_project_path):
            pass

    class VersionManager:
        @staticmethod
        def read_project_version(_path):
            return "9.9.9"

        @staticmethod
        def is_installed(*_args):
            raise AssertionError("source launches must not query installed Hub versions")

    monkeypatch.setattr(control_pane_viewmodel, "is_project_open", lambda _path: False)
    monkeypatch.setattr(
        control_pane_viewmodel.ProjectModel,
        "get_project_python_version",
        staticmethod(lambda _path: f"{sys.version_info.major}.{sys.version_info.minor}"),
    )
    worker = LaunchPreparationWorker(
        Model(), VersionManager(), str(tmp_path), HubLaunchContext.SOURCE
    )
    finished = []
    errors = []
    worker.finished.connect(finished.append)
    worker.error.connect(errors.append)

    worker.run()

    assert errors == []
    assert finished == [sys.executable]


def test_upgraded_hub_requires_the_new_default_runtime(monkeypatch):
    _app()
    observed = {"message": "", "page": None, "finished": False}
    launcher = SimpleNamespace(
        runtime_manager=SimpleNamespace(
            default_version="3.13",
            has_runtime=lambda _version: False,
        ),
        _show_runtime_installs=lambda: observed.__setitem__("page", "runtime"),
        _finish_startup=lambda: observed.__setitem__("finished", True),
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda _parent, _title, message: observed.__setitem__("message", message),
    )
    monkeypatch.setattr(
        "launcher.QTimer.singleShot",
        lambda _delay, callback: callback(),
    )

    GameEngineLauncher._bootstrap_python_runtime(launcher)

    assert observed["message"] == ""
    assert observed["page"] == "runtime"
    assert observed["finished"] is True


def test_packaged_hub_checks_for_updates_before_runtime_bootstrap():
    observed = []
    launcher = SimpleNamespace(
        _startup_update_pending=False,
        update_controller=SimpleNamespace(
            check=lambda **kwargs: observed.append(("update", kwargs))
        ),
        _bootstrap_python_runtime=lambda: observed.append(("runtime", {})),
    )

    GameEngineLauncher._bootstrap_hub(launcher)

    assert launcher._startup_update_pending is True
    assert observed == [("update", {"silent": True})]

    GameEngineLauncher._on_startup_update_check_finished(launcher)

    assert launcher._startup_update_pending is False
    assert observed == [
        ("update", {"silent": True}),
        ("runtime", {}),
    ]


def test_fresh_installer_runtime_skips_the_upgrade_requirement(monkeypatch):
    observed = {"warning": False, "finished": False}
    launcher = SimpleNamespace(
        runtime_manager=SimpleNamespace(
            default_version="3.13",
            has_runtime=lambda _version: True,
        ),
        sidebar=SimpleNamespace(select_page=lambda _page: None),
        _finish_startup=lambda: observed.__setitem__("finished", True),
    )
    monkeypatch.setattr(
        QMessageBox,
        "warning",
        lambda *_args: observed.__setitem__("warning", True),
    )
    monkeypatch.setattr(
        "launcher.QTimer.singleShot",
        lambda _delay, callback: callback(),
    )

    GameEngineLauncher._bootstrap_python_runtime(launcher)

    assert observed == {"warning": False, "finished": True}
