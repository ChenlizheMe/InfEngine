from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QApplication, QDialog

from view import installs_view


@pytest.mark.parametrize("archive_path", ("", "android.inxkit"))
@pytest.mark.parametrize("failure", (None, "kit extraction failed", ""))
def test_android_install_keeps_ui_responsive_and_finishes_after_worker(archive_path, failure):
    app = QApplication.instance() or QApplication([])
    owner = threading.get_ident()
    release = threading.Event()
    started = threading.Event()
    calls = []
    ticks = []
    visible_during_install = []

    def install(kind, value):
        calls.append((kind, value, threading.get_ident()))
        started.set()
        assert release.wait(5), "test did not release installation worker"
        if failure is not None:
            raise RuntimeError(failure)
        return "/shared/android"

    manager = SimpleNamespace(
        install=lambda **kwargs: install("download", ""),
        install_archive=lambda path: install("archive", path),
    )
    dialog = installs_view.AndroidSupportInstallDialog(manager, archive_path=archive_path)
    worker_destroyed = threading.Event()
    dialog._worker.destroyed.connect(worker_destroyed.set, Qt.ConnectionType.DirectConnection)
    finished_running = []
    finished_after_cleanup = []
    dialog.finished.connect(lambda _result: finished_after_cleanup.append(worker_destroyed.is_set()))
    dialog.finished.connect(lambda _result: finished_running.append(
        dialog._thread is not None and dialog._thread.isRunning()))
    timer = QTimer()
    timer.setInterval(10)

    def heartbeat():
        if not started.is_set():
            return
        ticks.append(threading.get_ident())
        dialog.reject()
        visible_during_install.append(dialog.isVisible())
        if len(ticks) >= 3:
            timer.stop()
            release.set()

    timer.timeout.connect(heartbeat)
    timer.start()
    try:
        result = dialog.exec()
    finally:
        timer.stop()
        release.set()
    assert result == (QDialog.Rejected if failure is not None else QDialog.Accepted)
    assert calls == [("archive" if archive_path else "download", archive_path, calls[0][2])]
    assert calls[0][2] != owner
    assert len(ticks) >= 3 and set(ticks) == {owner}
    assert all(visible_during_install)
    assert finished_running == [False]
    assert finished_after_cleanup == [True]
    assert dialog.error_text == (failure or "")
    assert dialog.result_path == ("" if failure is not None else "/shared/android")
    dialog.deleteLater()
    app.processEvents()


def test_locate_android_routes_selected_bundle_to_background_dialog(monkeypatch):
    delegated = []
    errors = []

    def forbidden_inline(_path):
        raise AssertionError("archive extraction must not run inside the UI handler")

    page = SimpleNamespace(
        _android_support_manager=SimpleNamespace(install_archive=forbidden_inline),
        _install_android_support=lambda **kwargs: delegated.append(kwargs),
        refresh=lambda: None,
    )
    monkeypatch.setattr(installs_view.QFileDialog, "getOpenFileName", lambda *args: ("kit.inxkit", ""))
    monkeypatch.setattr(installs_view.QMessageBox, "critical", lambda *args: errors.append(args))
    installs_view.InstallsView._on_locate_android(page)
    assert not errors
    assert delegated == [{"archive_path": "kit.inxkit"}]
