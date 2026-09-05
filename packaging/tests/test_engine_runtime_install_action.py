from types import SimpleNamespace

import pytest
import version_manager
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog

from i18n import configure_language, language_mode
from version_manager import EngineVersion, VersionManager
from view.installs_view import InstallEditorDialog, PythonRuntimeInstallDialog
from view import installs_view


@pytest.fixture
def engine_dialog(tmp_path, monkeypatch):
    app = QApplication.instance() or QApplication([])
    previous_language = language_mode()
    configure_language("en")
    installed = set()
    monkeypatch.setattr(version_manager, "_VERSIONS_DIR", tmp_path / "engines")
    manager = VersionManager(
        runtime_manager=SimpleNamespace(has_runtime=lambda version: version in installed),
    )
    engine = EngineVersion(
        tag="v0.4.0", version="0.4.0", python_version="3.13",
        wheel_url="https://example.invalid/infernux.whl",
    )
    monkeypatch.setattr(manager, "list_versions", lambda **kwargs: [engine])
    dialog = InstallEditorDialog(manager)
    try:
        for _ in range(500):
            app.processEvents()
            if not dialog._fetch_thread.isRunning():
                break
            QTest.qWait(10)
        assert not dialog._fetch_thread.isRunning()
        yield dialog, engine, installed
    finally:
        dialog._fetch_thread.quit()
        dialog._fetch_thread.wait()
        dialog.close()
        configure_language(previous_language)


@pytest.mark.parametrize("installed_successfully", [False, True])
def test_missing_runtime_requires_explicit_action_and_preserves_engine_selection(
    engine_dialog, installed_successfully,
):
    dialog, engine, installed = engine_dialog
    requests = []

    def install(version):
        requests.append(version)
        if installed_successfully:
            installed.add(version)

    dialog.runtime_install_requested.connect(install)
    dialog._select(engine)
    assert requests == []
    assert not dialog._btn_install.isEnabled()
    assert not dialog._btn_runtime.isHidden()
    assert "3.13" in dialog._btn_runtime.text()

    dialog._btn_runtime.click()
    assert requests == ["3.13"]
    assert dialog._selected is engine
    assert dialog._btn_install.isEnabled() is installed_successfully
    assert dialog._btn_runtime.isHidden() is installed_successfully
    assert dialog._dl_thread is None  # Runtime installation does not install the engine.


def test_incompatible_engine_does_not_offer_runtime_install(engine_dialog):
    dialog, engine, installed = engine_dialog
    dialog._select(engine)
    engine.compatibility_error = "Unsupported host architecture"
    dialog._select(engine)
    assert dialog._btn_runtime.isHidden()
    assert not dialog._btn_install.isEnabled()
    assert dialog._status.text() == engine.compatibility_error


def test_existing_runtime_needs_no_dependency_install_action(engine_dialog):
    dialog, engine, installed = engine_dialog
    installed.add("3.13")
    dialog._select(engine)
    assert dialog._btn_runtime.isHidden()
    assert dialog._btn_install.isEnabled()


def test_install_page_connects_dependency_action_to_existing_runtime_installer(
    engine_dialog, monkeypatch,
):
    dialog, engine, installed = engine_dialog
    runtime_manager = SimpleNamespace(
        default_version="3.13", supported_versions=lambda: ["3.13"],
        get_runtime_path=lambda version: "/managed/python" if version in installed else "",
        has_runtime=lambda version: version in installed,
    )
    calls = []

    class RuntimeInstaller:
        result_path = "/managed/python"

        def __init__(self, manager, version, parent, *, reinstall):
            assert manager is runtime_manager
            assert not reinstall
            calls.append(version)

        def exec(self):
            installed.add(calls[-1])
            return QDialog.Accepted

    def select_and_install():
        dialog._select(engine)
        dialog._btn_runtime.click()
        return QDialog.Rejected  # Closing the selection must not install an engine.

    monkeypatch.setattr(installs_view, "InstallEditorDialog", lambda *args, **kwargs: dialog)
    monkeypatch.setattr(installs_view, "PythonRuntimeInstallDialog", RuntimeInstaller)
    monkeypatch.setattr(installs_view.QMessageBox, "information", lambda *args: None)
    monkeypatch.setattr(dialog, "exec", select_and_install)
    page = installs_view.InstallsView(dialog._vm, runtime_manager)
    try:
        page._on_install_editor()
        assert calls == ["3.13"]
        assert dialog._btn_install.isEnabled()
        assert dialog._dl_thread is None
    finally:
        page.close()


@pytest.mark.parametrize("succeeds", [False, True])
def test_runtime_dialog_finishes_only_after_worker_thread_cleanup(succeeds):
    app = QApplication.instance() or QApplication([])

    def prepare(**kwargs):
        if not succeeds:
            raise RuntimeError("Runtime installation failed")
        return "/managed/python"

    dialog = PythonRuntimeInstallDialog(SimpleNamespace(ensure_runtime=prepare), "3.13")
    cleaned_up = []
    dialog.finished.connect(lambda _: cleaned_up.append(dialog._thread is None))
    try:
        result = dialog.exec()
        assert result == (QDialog.Accepted if succeeds else QDialog.Rejected)
        assert cleaned_up == [True]
    finally:
        if dialog._thread is not None:
            dialog._thread.quit()
            dialog._thread.wait()
