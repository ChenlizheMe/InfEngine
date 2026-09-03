import json
from types import SimpleNamespace

from PySide6.QtCore import QEventLoop, QThread, QTimer
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox, QWidget

import view.update_dialog as update_dialog
from hub_updater import HubUpdateCheck, HubUpdateStatus


def test_opt_in_update_trace_records_result(tmp_path, monkeypatch):
    trace = tmp_path / "trace.json"
    monkeypatch.setenv("INFERNUX_HUB_UPDATE_TRACE", str(trace))

    update_dialog._write_update_trace(
        {"status": "update-available", "target_version": "1.2.3"}
    )

    assert json.loads(trace.read_text(encoding="utf-8")) == {
        "status": "update-available",
        "target_version": "1.2.3",
    }


def _app():
    return QApplication.instance() or QApplication([])


def _up_to_date() -> HubUpdateCheck:
    return HubUpdateCheck(HubUpdateStatus.UP_TO_DATE, "0.4.0", "0.4.0")


def test_manual_update_check_returns_to_the_main_thread(monkeypatch):
    """A user-facing result must never create a dialog in the worker thread."""
    app = _app()
    window = QWidget()
    controller = update_dialog.UpdateController(window)
    observed = {}
    loop = QEventLoop()

    monkeypatch.setattr(update_dialog, "check_for_update", _up_to_date)

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

    monkeypatch.setattr(update_dialog, "check_for_update", _up_to_date)
    controller.check_finished.connect(loop.quit)
    QTimer.singleShot(3000, loop.quit)
    controller.check(silent=True)
    loop.exec()
    controller.thread.wait(1000)

    assert completed == [True]


def test_unsupported_hub_offers_the_platform_installer(monkeypatch):
    _app()
    window = QWidget()
    window.show()
    observed = {"quit": False, "question": False, "opened": "", "finished": False}
    window.app = SimpleNamespace(quit=lambda: observed.__setitem__("quit", True))
    controller = update_dialog.UpdateController(window)
    controller._completion_pending = True
    controller.check_finished.connect(lambda: observed.__setitem__("finished", True))
    result = HubUpdateCheck(
        HubUpdateStatus.UNSUPPORTED_CURRENT_VERSION,
        "0.3.7",
        "0.4.0",
        installer_url="https://example.invalid/InfernuxHubInstaller-0.4.0-windows-x64.exe",
    )

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: observed.__setitem__("question", True) or QMessageBox.Yes,
    )
    monkeypatch.setattr(
        update_dialog.QDesktopServices,
        "openUrl",
        lambda url: observed.__setitem__("opened", url.toString()),
    )

    controller._checked(result)

    assert observed == {
        "quit": False,
        "question": True,
        "opened": "https://example.invalid/InfernuxHubInstaller-0.4.0-windows-x64.exe",
        "finished": True,
    }
    assert not window.isHidden()


def test_available_update_quits_after_success(monkeypatch):
    _app()
    window = QWidget()
    window.show()
    observed = {"quit": False, "question": False}
    window.app = SimpleNamespace(quit=lambda: observed.__setitem__("quit", True))
    controller = update_dialog.UpdateController(window)
    controller._completion_pending = True
    finished = []
    controller.check_finished.connect(lambda: finished.append(True))
    update = SimpleNamespace(target_version="0.4.0")
    result = HubUpdateCheck(
        HubUpdateStatus.UPDATE_AVAILABLE,
        "0.3.7",
        "0.4.0",
        update=update,
    )

    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda *_args: observed.__setitem__("question", True) or QMessageBox.Yes,
    )

    class _AcceptedUpdateDialog:
        def __init__(self, *_args):
            pass

        def exec(self):
            return QDialog.Accepted

    monkeypatch.setattr(update_dialog, "UpdateProgressDialog", _AcceptedUpdateDialog)

    controller._checked(result)

    assert observed == {"quit": True, "question": True}
    assert finished == []
    assert window.isHidden()
