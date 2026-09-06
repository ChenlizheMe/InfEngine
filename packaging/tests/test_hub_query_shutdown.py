"""Exercise real Qt teardown in a child process: a thread abort kills pytest too."""

import os
import io
from pathlib import Path
import subprocess
import sys

import pytest

import hub_updater


def test_update_catalog_request_has_a_network_timeout(monkeypatch):
    def open_catalog(request, *, timeout):
        assert timeout == 15
        return io.BytesIO(b"{}")

    monkeypatch.setattr(hub_updater.urllib.request, "urlopen", open_catalog)
    assert hub_updater._request_bytes("https://example.invalid/catalog") == b"{}"


@pytest.mark.parametrize("query", ["versions", "update"])
def test_quit_while_catalog_request_is_in_flight(query):
    script = r'''
import sys
import threading
import time
from types import SimpleNamespace
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QWidget
from install_queue import InstallQueue
from view.installs_view import InstallEditorDialog
from view import update_dialog
from hub_updater import HubUpdateCheck, HubUpdateStatus

app = QApplication([])
app.setQuitOnLastWindowClosed(False)
started = threading.Event()
completed = threading.Event()

def query(*args, **kwargs):
    started.set()
    time.sleep(0.3)
    completed.set()
    return ([] if sys.argv[1] == "versions" else
            HubUpdateCheck(HubUpdateStatus.UP_TO_DATE, "0.4.0", "0.4.0"))

if sys.argv[1] == "versions":
    owner = InstallEditorDialog(SimpleNamespace(list_versions=query), InstallQueue(app))
else:
    owner = QWidget()
    owner.install_queue = InstallQueue(app)
    controller = update_dialog.UpdateController(owner)
    update_dialog.check_for_update = query
    controller.check()

assert started.wait(2), "query did not start"
assert not completed.is_set(), "query finished before quit"
QTimer.singleShot(0, app.quit)
app.exec()
assert completed.is_set(), "Qt exited before its worker finished"
owner.deleteLater()
app.sendPostedEvents()
print("SHUTDOWN_OK", flush=True)
'''
    env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    result = subprocess.run(
        [sys.executable, "-c", script, query], env=env,
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SHUTDOWN_OK" in result.stdout
