import json
from types import SimpleNamespace

from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

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


def test_update_check_completion_is_emitted_after_no_update(monkeypatch):
    _app()
    window = QWidget()
    controller = update_dialog.UpdateController(window)
    completed = []
    controller.check_finished.connect(lambda: completed.append(True))
    loop = QEventLoop()

    monkeypatch.setattr(update_dialog, "check_for_update", lambda: None)
    controller.check_finished.connect(loop.quit)
    QTimer.singleShot(3000, loop.quit)
    controller.check(silent=True)
    loop.exec()
    controller.thread.wait(1000)

    assert completed == [True]


def test_required_runtime_catalog_update_starts_without_confirmation(monkeypatch):
    _app()
    window = QWidget()
    window.show()
    observed = {
        "quit": False,
        "information": False,
        "question": False,
        "finished": False,
    }
    window.app = SimpleNamespace(quit=lambda: observed.__setitem__("quit", True))
    controller = update_dialog.UpdateController(window)
    controller._completion_pending = True
    controller.check_finished.connect(
        lambda: observed.__setitem__("finished", True)
    )
    update = SimpleNamespace(target_version="0.4.0", required=True)

    monkeypatch.setattr(
        QMessageBox,
        "information",
        lambda *_args: observed.__setitem__("information", True),
    )
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: observed.__setitem__("question", True),
    )

    class _RejectedUpdateDialog:
        def __init__(self, *_args):
            pass

        def exec(self):
            return QDialog.Rejected

    monkeypatch.setattr(update_dialog, "UpdateProgressDialog", _RejectedUpdateDialog)

    controller._checked(update)

    assert observed == {
        "quit": False,
        "information": True,
        "question": False,
        "finished": True,
    }
    assert not window.isHidden()


def test_required_runtime_catalog_update_quits_after_success(monkeypatch):
    _app()
    window = QWidget()
    window.show()
    observed = {"quit": False, "question": False}
    window.app = SimpleNamespace(quit=lambda: observed.__setitem__("quit", True))
    controller = update_dialog.UpdateController(window)
    controller._completion_pending = True
    finished = []
    controller.check_finished.connect(lambda: finished.append(True))
    update = SimpleNamespace(target_version="0.4.0", required=True)

    monkeypatch.setattr(QMessageBox, "information", lambda *_args: None)
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: observed.__setitem__("question", True),
    )

    class _AcceptedUpdateDialog:
        def __init__(self, *_args):
            pass

        def exec(self):
            return QDialog.Accepted

    monkeypatch.setattr(update_dialog, "UpdateProgressDialog", _AcceptedUpdateDialog)

    controller._checked(update)

    assert observed == {"quit": True, "question": False}
    assert finished == []
    assert window.isHidden()
