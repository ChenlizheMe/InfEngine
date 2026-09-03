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
