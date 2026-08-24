import json

from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

import view.update_dialog as update_dialog


def test_opt_in_update_trace_records_result(tmp_path, monkeypatch):
    trace = tmp_path / "trace.json"
    monkeypatch.setenv("INFERNUX_HUB_UPDATE_TRACE", str(trace))

    update_dialog._write_update_trace(
        {"status": "update_available", "target_version": "1.2.3"}
    )

    assert json.loads(trace.read_text(encoding="utf-8")) == {
        "status": "update_available",
        "target_version": "1.2.3",
    }


def _app():
    return QApplication.instance() or QApplication([])


def test_manual_update_check_returns_to_the_main_thread(monkeypatch):
    """A user-facing result must never create a dialog in the worker thread."""
    app = _app()
    window = QWidget()
    controller = update_dialog.UpdateController(window)
    observed = {}
    loop = QEventLoop()

    monkeypatch.setattr(update_dialog, "check_for_update", lambda: None)

    def show_information(*_args):
        observed["main_thread"] = QThread.currentThread() == app.thread()
        QTimer.singleShot(0, loop.quit)
        return QMessageBox.Ok

    monkeypatch.setattr(QMessageBox, "information", show_information)
    QTimer.singleShot(3000, loop.quit)
    controller.check(silent=False)
    loop.exec()
    controller.thread.wait(1000)

    assert observed["main_thread"] is True
