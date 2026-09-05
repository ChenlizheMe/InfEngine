from types import SimpleNamespace
import threading

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QPushButton

from android_support import AndroidSupportStatus
from install_queue import InstallQueue
from view.installs_view import AndroidSupportView


@pytest.mark.parametrize("failure", [None, "kit extraction failed", ""])
def test_android_channel_install_is_queued_and_non_modal(tmp_path, failure):
    app = QApplication.instance() or QApplication([])
    owner = threading.get_ident()
    release = threading.Event()
    started = threading.Event()
    calls, ticks = [], []
    def install(**kwargs):
        calls.append(threading.get_ident())
        started.set()
        assert release.wait(5)
        if failure is not None:
            raise RuntimeError(failure)
        return str(tmp_path / "android")
    manager = SimpleNamespace(
        install=install, status=lambda: AndroidSupportStatus(False, tmp_path / "android"),
    )
    queue = InstallQueue(app)
    page = AndroidSupportView(manager, queue)
    page.show()
    job = page.install()
    assert calls == []
    timer = QTimer()
    timer.timeout.connect(lambda: ticks.append(threading.get_ident()) if started.is_set() else None)
    timer.start(10)
    try:
        for _ in range(500):
            QTest.qWait(10)
            if len(ticks) >= 3:
                break
        assert len(ticks) >= 3 and set(ticks) == {owner}
        assert page.isVisible()
        assert QApplication.activeModalWidget() is None
        assert calls and calls[0] != owner
        assert len(page.findChildren(QPushButton)) == 1  # No local archive import.
    finally:
        release.set()
        for _ in range(500):
            QTest.qWait(10)
            if not queue.busy:
                break
        timer.stop()
        page.close()
    assert not queue.busy
    assert job.state == ("succeeded" if failure is None else "failed")
