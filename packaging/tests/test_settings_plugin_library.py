from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication, QMessageBox

from i18n import configure_language, language_mode
from plugin_library import PluginLibraryStats
from view import settings_view


@pytest.fixture(scope="module", autouse=True)
def application():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture(autouse=True)
def english_ui():
    previous = language_mode()
    configure_language("en")
    yield
    configure_language(previous)


class _Database:
    def __init__(self, *projects):
        self.projects = tuple(SimpleNamespace(path=str(path)) for path in projects)
        self.settings = {}

    def all_projects(self):
        return list(self.projects)

    def get_setting(self, key, default=""):
        return self.settings.get(key, default)

    def set_setting(self, key, value):
        self.settings[key] = value


def test_settings_show_the_shared_plugin_library_and_cleanup_capacity(
    tmp_path, monkeypatch
):
    project = tmp_path / "Project"
    removable = tmp_path / "unused.inxpkg"
    stats = PluginLibraryStats(
        root=tmp_path / "Hub" / "Library" / "Plugins",
        package_count=3,
        total_bytes=3 * 1024 * 1024,
        removable=(removable,),
        removable_bytes=1024 * 1024,
    )
    monkeypatch.setattr(settings_view, "inspect_plugin_library", lambda roots: stats)

    view = settings_view.SettingsView(_Database(project))

    assert "3 packages" in view.storage_description.text()
    assert "3.0 MiB" in view.storage_description.text()
    assert str(stats.root) in view.storage_description.text()
    assert view.clean_plugins_button.isEnabled()
    assert "1.0 MiB" in view.clean_plugins_button.toolTip()


def test_settings_cleanup_rechecks_references_before_deleting(tmp_path, monkeypatch):
    project = tmp_path / "Project"
    before = PluginLibraryStats(
        root=tmp_path / "Hub" / "Library" / "Plugins",
        package_count=1,
        total_bytes=7,
        removable=(tmp_path / "unused.inxpkg",),
        removable_bytes=7,
    )
    after = PluginLibraryStats(
        root=before.root,
        package_count=0,
        total_bytes=0,
        removable=(),
        removable_bytes=0,
    )
    observed = []
    states = iter((before, before, after))
    monkeypatch.setattr(
        settings_view,
        "inspect_plugin_library",
        lambda roots: observed.append(tuple(roots)) or next(states),
    )
    monkeypatch.setattr(
        settings_view,
        "prune_unreferenced_packages",
        lambda roots: observed.append(("prune", *tuple(roots))) or after,
    )
    monkeypatch.setattr(QMessageBox, "question", lambda *_args: QMessageBox.Yes)

    view = settings_view.SettingsView(_Database(project))
    view._clean_plugin_library()

    assert observed == [
        (str(project),),
        (str(project),),
        ("prune", str(project)),
        (str(project),),
    ]
    assert not view.clean_plugins_button.isEnabled()
    assert "0 packages" in view.storage_description.text()


def test_settings_disable_cleanup_when_a_registered_project_is_unavailable(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        settings_view,
        "inspect_plugin_library",
        lambda _roots: (_ for _ in ()).throw(FileNotFoundError("missing project")),
    )

    view = settings_view.SettingsView(_Database(tmp_path / "Missing"))

    assert not view.clean_plugins_button.isEnabled()
    assert "missing project" in view.storage_description.text()


def test_migration_dialog_keeps_gui_responsive_and_cannot_close_mid_copy(monkeypatch):
    import threading
    from PySide6.QtCore import QTimer
    from view import storage_migration_dialog

    observations = []
    gate = threading.Event()

    def migrate(plan, roots, *, progress):
        assert roots == ("project",)
        progress("Runtimes/python313")
        assert gate.wait(5)
        return ("Runtimes/python313",)

    monkeypatch.setattr(storage_migration_dialog, "migrate_legacy_storage", migrate)
    dialog = storage_migration_dialog.StorageMigrationDialog(None, ("project",))

    def interact():
        observations.append(dialog.worker.isRunning())
        dialog.reject()
        observations.append(dialog.isVisible())
        dialog.close()
        observations.append(dialog.isVisible())
        gate.set()

    QTimer.singleShot(40, interact)
    dialog.exec()
    assert observations == [True, True, True]
    assert not dialog.worker.isRunning()
    assert dialog.worker.result == ("Runtimes/python313",)
    assert not dialog.worker.error


def test_migration_preview_requires_explicit_confirmation(tmp_path, monkeypatch):
    from pathlib import Path
    from shared_storage_migration import MigrationPlan

    monkeypatch.setattr(settings_view, "inspect_plugin_library", lambda roots: PluginLibraryStats(
        root=tmp_path, package_count=0, total_bytes=0, removable=(), removable_bytes=0,
    ))
    plan = MigrationPlan(tmp_path / "old", tmp_path / "new", (Path("Runtimes/python313"),), ())
    monkeypatch.setattr(settings_view, "inspect_legacy_storage", lambda: plan)
    previews = []

    def reject_preview(message):
        previews.append((message.text(), message.detailedText(), message.defaultButton().text()))
        return QMessageBox.No

    monkeypatch.setattr(QMessageBox, "exec", reject_preview)
    monkeypatch.setattr(settings_view, "StorageMigrationDialog", lambda *args: pytest.fail("No consent"))
    view = settings_view.SettingsView(_Database())
    view.migrate_storage_button.click()
    assert len(previews) == 1
    assert str(plan.source) in previews[0][0]
    assert str(plan.destination) in previews[0][0]
    assert "Runtimes/python313" in previews[0][1]
    assert "No" in previews[0][2]
