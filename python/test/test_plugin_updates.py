from __future__ import annotations

import json
from pathlib import Path

import pytest

from Infernux.plugins import InxPackage, PackageConflictError, PackageUpdateConflict, PluginManager
from test_inxpackage_plugins import _fake_inxpack, _project, _source


@pytest.fixture
def installed(tmp_path, monkeypatch):
    project = _project(tmp_path / "project")
    source = _source(tmp_path / "source", "vendor/plugin")
    runtime = source / "runtime"
    runtime.mkdir()
    (runtime / "note.txt").write_text("first", encoding="utf-8")
    (runtime / "retired.txt").write_text("retired", encoding="utf-8")
    (runtime / "retired.py").write_text("value = 1\n", encoding="utf-8")
    manager = PluginManager(str(project), runtime=True)
    # The test archive backend is in-memory; retain the original archive identity.
    monkeypatch.setattr(manager, "_cache_package", lambda path, ref, version: (path, f"{ref}/{version}.inxpkg"))
    old = tmp_path / "first.inxpkg"
    InxPackage.export_source(str(source), str(old))
    manager.install_package(str(old), install_dependencies=False)
    return manager, source, project / "Packages/vendor/plugin"


def next_package(source, version="2.0.0"):
    manifest = source / "inx_package.json"
    metadata = json.loads(manifest.read_text(encoding="utf-8"))
    metadata["version"] = version
    manifest.write_text(json.dumps(metadata), encoding="utf-8")
    (source / "runtime/note.txt").write_text("second", encoding="utf-8")
    (source / "runtime/retired.txt").unlink(missing_ok=True)
    (source / "runtime/retired.py").unlink(missing_ok=True)
    (source / "runtime/new.txt").write_text("new", encoding="utf-8")
    package = source.parent / f"next-{version}.inxpkg"
    InxPackage.export_source(str(source), str(package))
    return str(package)


def test_update_preserves_identity_disabled_state_and_user_files(installed):
    manager, source, root = installed
    before = manager.registry.installed_record("vendor/plugin")
    manager.set_enabled("vendor/plugin", False)
    extra = root / "runtime/author.txt"
    extra.write_text("my work", encoding="utf-8")
    package = next_package(source)
    state = manager.install_package(package, update=True, install_dependencies=False)
    after = manager.registry.installed_record("vendor/plugin")
    assert state.enabled is False
    assert after["version"] == "2.0.0"
    assert after["control"]["guid"] == before["control"]["guid"]
    old = next(item for item in before["files"] if item["logical_path"] == "runtime/note.txt")
    new = next(item for item in after["files"] if item["logical_path"] == "runtime/note.txt")
    assert new["guid"] == old["guid"]
    assert (root / "runtime/note.txt").read_text() == "second"
    assert (root / "runtime/new.txt").read_text() == "new"
    assert not (root / "runtime/retired.txt").exists()
    assert not (root / "runtime/retired.txt.meta").exists()
    assert extra.read_text() == "my work"
    manager.uninstall("vendor/plugin")
    assert extra.read_text() == "my work"


def test_local_edits_require_explicit_overwrite_and_leave_pin_unchanged(installed):
    manager, source, root = installed
    note = root / "runtime/note.txt"
    note.write_text("local change", encoding="utf-8")
    package = next_package(source)
    registry_before = Path(manager.registry.path).read_bytes()
    with pytest.raises(PackageUpdateConflict) as caught:
        manager.install_package(package, update=True, install_dependencies=False)
    assert "Packages/vendor/plugin/runtime/note.txt" in caught.value.paths
    assert Path(manager.registry.path).read_bytes() == registry_before
    assert note.read_text() == "local change"
    manager.install_package(package, update=True, overwrite_modified=True, install_dependencies=False)
    assert note.read_text() == "second"


def test_user_moved_asset_is_updated_at_its_guid_location(installed):
    manager, source, root = installed
    note = root / "runtime/note.txt"
    moved = root / "runtime/moved.txt"
    note.rename(moved)
    Path(str(note) + ".meta").rename(str(moved) + ".meta")
    manager.install_package(next_package(source), update=True, install_dependencies=False)
    assert moved.read_text() == "second"
    assert not note.exists()


def test_regenerated_guid_is_rejected_even_with_overwrite_consent(installed):
    manager, source, root = installed
    meta = source / "runtime/note.txt.meta"
    document = json.loads((root / "runtime/note.txt.meta").read_bytes())
    document["metadata"]["guid"]["value"] = "a" * 32
    meta.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(PackageConflictError, match="retain its GUID"):
        manager.install_package(next_package(source), update=True,
                                overwrite_modified=True, install_dependencies=False)
    assert (root / "runtime/note.txt").read_text() == "first"


def test_update_registry_failure_restores_previous_files_and_pin(installed, monkeypatch):
    manager, source, root = installed
    before = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    registry_before = Path(manager.registry.path).read_bytes()
    original = manager.registry.record_install

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise OSError("publication failed")

    monkeypatch.setattr(manager.registry, "record_install", fail)
    with pytest.raises(OSError, match="publication failed"):
        manager.install_package(next_package(source), update=True, install_dependencies=False)
    after = {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}
    assert after == before
    assert Path(manager.registry.path).read_bytes() == registry_before


def test_removed_local_edits_and_importer_settings_require_consent(installed):
    manager, source, root = installed
    meta = root / "runtime/retired.txt.meta"
    document = json.loads(meta.read_bytes())
    document["importer_settings"] = {"custom": True}
    meta.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(PackageUpdateConflict) as caught:
        manager.install_package(next_package(source), update=True, install_dependencies=False)
    assert any(path.endswith("retired.txt.meta") for path in caught.value.paths)
    assert meta.exists()


def test_retained_local_importer_settings_survive_update(installed):
    manager, source, root = installed
    meta = root / "runtime/note.txt.meta"
    document = json.loads(meta.read_bytes())
    document["importer_settings"] = {"custom": True}
    meta.write_text(json.dumps(document), encoding="utf-8")
    manager.install_package(next_package(source), update=True, install_dependencies=False)
    assert json.loads(meta.read_bytes())["importer_settings"] == {"custom": True}


def test_missing_original_cache_does_not_silently_overwrite(installed):
    manager, source, root = installed
    record = manager.registry.installed_record("vendor/plugin")
    Path(record["package_path"]).unlink()
    with pytest.raises(PackageUpdateConflict):
        manager.install_package(next_package(source), update=True, install_dependencies=False)
    assert (root / "runtime/note.txt").read_text() == "first"


def test_bytecode_is_removed_with_retired_source(installed):
    manager, source, root = installed
    # An owned source is required; a user-added script and its cache remain untouched.
    extra = root / "runtime/author.py"
    extra.write_text("value = 1\n", encoding="utf-8")
    import py_compile
    bytecode = Path(py_compile.compile(str(extra), doraise=True))
    owned_bytecode = Path(py_compile.compile(str(root / "runtime/retired.py"), doraise=True))
    manager.install_package(next_package(source), update=True, install_dependencies=False)
    assert extra.exists()
    assert bytecode.exists()
    assert not owned_bytecode.exists()


def test_publisher_importer_updates_apply_when_not_locally_modified(installed):
    manager, source, root = installed
    meta = source / "runtime/note.txt.meta"
    document = json.loads((root / "runtime/note.txt.meta").read_bytes())
    document["importer_settings"] = {"publisher": "new default"}
    meta.write_text(json.dumps(document), encoding="utf-8")
    manager.install_package(next_package(source), update=True, install_dependencies=False)
    assert json.loads((root / "runtime/note.txt.meta").read_bytes())["importer_settings"] == {"publisher": "new default"}


def test_exact_update_download_does_not_change_the_project_pin(installed, monkeypatch):
    from Infernux.plugins import github_releases
    manager, source, root = installed
    document = manager.registry.load()
    document["installed"][0]["source"] = {"type": "github", "location": "https://github.com/vendor/plugin"}
    manager.registry.save(document)
    package = next_package(source)
    seen = []

    def resolve(repository, workspace, **kwargs):
        seen.append((repository, kwargs["release_tag"], kwargs["expected_reference"]))
        return github_releases.GitHubReleasePackage(package, {"version": "2.0.0", "release_tag": "v2.0.0"})

    monkeypatch.setattr(github_releases, "resolve_github_release", resolve)
    before = Path(manager.registry.path).read_bytes()
    result = manager.download_update("vendor/plugin", "v2.0.0")
    assert seen == [("https://github.com/vendor/plugin", "v2.0.0", "vendor/plugin")]
    assert result["source"]["type"] == "github"
    assert result["source"]["release_tag"] == "v2.0.0"
    assert Path(manager.registry.path).read_bytes() == before
    assert (root / "runtime/note.txt").read_text() == "first"


def test_local_author_update_does_not_download_remote(installed):
    manager, source, root = installed
    with pytest.raises(ValueError, match="Local author packages"):
        manager.download_update("vendor/plugin", "v2.0.0")


def test_version_view_keeps_download_separate_from_publication(installed, monkeypatch):
    from Infernux.engine.ui.plugin_versions import PluginVersionsView
    from Infernux.engine.ui.plugin_install_progress import PluginInstallProgressService
    from types import SimpleNamespace
    manager, source, root = installed
    package = next_package(source)
    operations = []
    monkeypatch.setattr(PluginInstallProgressService, "_instance", SimpleNamespace(
        begin=lambda **kwargs: (operations.append(kwargs), True)[1],
    ))
    monkeypatch.setattr(manager, "download_update", lambda ref, tag, **kwargs: {
        "reference": ref, "path": package, "source": {"type": "github", "location": "https://github.com/vendor/plugin"},
    })
    view = PluginVersionsView()
    view.download(manager, "vendor/plugin", "v2.0.0")
    operation = operations[0]
    release = operation["work"](lambda *args: None)
    assert manager.registry.installed_record("vendor/plugin")["version"] == "1.0.0"
    (root / "runtime/note.txt").write_text("local", encoding="utf-8")
    operation["complete"](True, release, "")
    assert "vendor/plugin" in view.pending
    assert (root / "runtime/note.txt").read_text() == "local"
    view.publish(manager, "vendor/plugin", overwrite=True)
    assert "vendor/plugin" not in view.pending
    assert (root / "runtime/note.txt").read_text() == "second"


def test_shared_removed_asset_ownership_transfers_to_remaining_plugin(installed):
    manager, source, root = installed
    current = manager.registry.installed_record("vendor/plugin")
    shared = next(item for item in current["files"] if item["logical_path"] == "runtime/retired.txt")
    manager.registry.record_install(
        {"reference": "vendor/other", "version": "1.0.0"},
        files=[{**shared, "owned": False}],
        control={"guid": "f" * 32, "owned": True, "path_hint": "Packages/vendor/other/inx_package.json"},
    )
    manager.install_package(next_package(source), update=True, install_dependencies=False)
    other = manager.registry.installed_record("vendor/other")
    assert other["files"][0]["owned"] is True
    assert (root / "runtime/retired.txt").exists()


def test_shared_asset_replacement_cannot_be_forced(installed):
    manager, source, root = installed
    current = manager.registry.installed_record("vendor/plugin")
    shared = next(item for item in current["files"] if item["logical_path"] == "runtime/note.txt")
    manager.registry.record_install(
        {"reference": "vendor/other", "version": "1.0.0"},
        files=[{**shared, "owned": False}],
        control={"guid": "f" * 32, "owned": True, "path_hint": "Packages/vendor/other/inx_package.json"},
    )
    with pytest.raises(PackageConflictError, match="shared asset"):
        manager.install_package(next_package(source), update=True,
                                overwrite_modified=True, install_dependencies=False)
    assert (root / "runtime/note.txt").read_text() == "first"


def test_selective_import_does_not_reintroduce_unselected_members(tmp_path, monkeypatch):
    manager = PluginManager(str(_project(tmp_path / "project")), runtime=True)
    monkeypatch.setattr(manager, "_cache_package", lambda path, ref, version: (path, f"{version}.inxpkg"))
    source = _source(tmp_path / "source", "vendor/plugin")
    (source / "runtime").mkdir()
    (source / "runtime/note.txt").write_text("first", encoding="utf-8")
    (source / "runtime/omitted.txt").write_text("not selected", encoding="utf-8")
    package = tmp_path / "first.inxpkg"
    InxPackage.export_source(str(source), str(package))
    manager.install_package(str(package), selected=["runtime/note.txt"], install_dependencies=False)
    manager.install_package(next_package(source), update=True, install_dependencies=False)
    files = manager.registry.installed_record("vendor/plugin")["files"]
    assert {item["logical_path"] for item in files} == {"runtime/note.txt", "runtime/new.txt"}


def test_update_relinquishes_dropped_python_dependency_and_preserves_other_owner(installed, monkeypatch):
    manager, source, root = installed
    document = manager.registry.load()
    document["installed"][0]["python_requirements"] = [{"name": "old-dep", "requirement": "old-dep==1"}]
    document["python_dependencies"] = [{
        "name": "old-dep", "managed": True, "baseline_version": "", "installed_version": "1",
        "owners": [{"reference": "vendor/plugin", "requirements": ["old-dep==1"]},
                   {"reference": "vendor/other", "requirements": ["old-dep==1"]}],
    }]
    manager.registry.save(document)
    monkeypatch.setattr(manager, "_project_python_executable", lambda: "python")
    monkeypatch.setattr(manager, "_python_environment_snapshot", lambda executable: {"old-dep": "1"})
    monkeypatch.setattr(manager, "_run_process", lambda *args, **kwargs: pytest.fail("The other owner still needs old-dep"))
    manager.install_package(next_package(source), update=True)
    assert manager.registry.installed_record("vendor/plugin")["python_requirements"] == []
    ledger = manager.registry.load()["python_dependencies"]
    assert ledger[0]["owners"] == [{"reference": "vendor/other", "requirements": ["old-dep==1"]}]


def test_update_failure_restores_released_python_environment(installed, monkeypatch):
    manager, source, root = installed
    document = manager.registry.load()
    document["installed"][0]["python_requirements"] = [{"name": "old-dep", "requirement": "old-dep==1"}]
    document["python_dependencies"] = [{
        "name": "old-dep", "managed": True, "baseline_version": "", "installed_version": "1",
        "owners": [{"reference": "vendor/plugin", "requirements": ["old-dep==1"]}],
    }]
    manager.registry.save(document)
    environment = {"old-dep": "1"}
    monkeypatch.setattr(manager, "_project_python_executable", lambda: "python")
    monkeypatch.setattr(manager, "_python_environment_snapshot", lambda executable: dict(environment))
    monkeypatch.setattr(manager, "_run_process", lambda *args, **kwargs: environment.pop("old-dep"))
    monkeypatch.setattr(manager, "_restore_python_environment", lambda baseline, **kwargs: environment.update(baseline))

    def fail(*args, **kwargs):
        assert environment == {}
        raise OSError("publication failed")

    monkeypatch.setattr(manager.registry, "record_install", fail)
    with pytest.raises(OSError, match="publication failed"):
        manager.install_package(next_package(source), update=True)
    assert environment == {"old-dep": "1"}
    assert manager.registry.load() == document


def test_update_with_real_binary_archives_and_shared_cache(tmp_path):
    from Infernux.engine import player_package_native
    player_package_native.set_test_backend(None)
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project), runtime=True)
    source = _source(tmp_path / "source", "vendor/plugin")
    (source / "runtime").mkdir()
    (source / "runtime/note.txt").write_text("first", encoding="utf-8")
    package = tmp_path / "first.inxpkg"
    InxPackage.export_source(str(source), str(package))
    manager.install_package(str(package), install_dependencies=False)
    manager.install_package(next_package(source), update=True, install_dependencies=False)
    assert (project / "Packages/vendor/plugin/runtime/note.txt").read_text() == "second"
    installed = manager.registry.installed_record("vendor/plugin")
    assert InxPackage.inspect(installed["package_path"]).metadata["version"] == "2.0.0"
    assert str(tmp_path / "hub-package-cache").replace("\\", "/") in installed["package_path"].replace("\\", "/")


def test_publisher_rename_moves_unmodified_location_but_keeps_guid(installed):
    manager, source, root = installed
    next_package(source)
    (source / "runtime/note.txt").rename(source / "runtime/renamed.txt")
    (source / "runtime/renamed.txt.meta").write_bytes((root / "runtime/note.txt.meta").read_bytes())
    package = source.parent / "renamed.inxpkg"
    InxPackage.export_source(str(source), str(package))
    manager.install_package(str(package), update=True, install_dependencies=False)
    assert not (root / "runtime/note.txt").exists()
    assert (root / "runtime/renamed.txt").read_text() == "second"


def test_conflicting_python_upgrade_does_not_break_other_installed_package(installed, monkeypatch):
    from Infernux.plugins.manager import _PipInstallEffect
    manager, source, root = installed
    document = manager.registry.load()
    document["python_dependencies"] = [{
        "name": "shared-dep", "managed": True, "baseline_version": "", "installed_version": "1",
        "owners": [{"reference": "vendor/other", "requirements": ["shared-dep==1"]}],
    }]
    manager.registry.save(document)
    effect = _PipInstallEffect(
        {"shared-dep": "1"}, {"shared-dep": "2"},
        ({"name": "shared-dep", "requirement": "shared-dep==2"},), ("pip",), "",
    )
    monkeypatch.setattr(manager, "_install_requirements", lambda *args, **kwargs: ((), effect))
    restored = []
    monkeypatch.setattr(manager, "_restore_python_environment", lambda baseline, **kwargs: restored.append(baseline))
    with pytest.raises(PackageConflictError, match="Python requirements conflict"):
        manager.install_package(next_package(source), update=True)
    assert restored == [{"shared-dep": "1"}]
    assert (root / "runtime/note.txt").read_text() == "first"
    assert manager.registry.load() == document
