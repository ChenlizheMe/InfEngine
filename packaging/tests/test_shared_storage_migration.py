import errno
import json
from pathlib import Path

import pytest

import shared_storage_migration as migration


@pytest.fixture
def roots(tmp_path, monkeypatch):
    source, destination = tmp_path / "Old Hub Data", tmp_path / "Hub" / "Shared"
    source.mkdir()
    monkeypatch.setattr(migration, "get_hub_user_data_dir", lambda: str(source))
    monkeypatch.setattr(migration, "get_hub_shared_data_dir", lambda: str(destination))
    monkeypatch.delenv("INFERNUX_PACKAGE_CACHE_ROOT", raising=False)
    return source, destination


def put(root, relative, content="payload"):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_migrate_complete_units_and_keep_private_or_unfinished_data(roots):
    source, target = roots
    files = (
        "Library/Plugins/packages/vendor/plugin/1.0.0.inxpkg",
        "Runtimes/python313/.infernux-private-python-runtime.json",
        "Runtimes/python313/Lib/module.py",
        "Engines/0.4.0/infernux.whl",
        "PlatformKits/android/0.1.0/windows-x64/infernux-android-support.json",
        "PlatformKits/android/0.1.0/windows-x64/sdk/ndk/include/header.h",
        "Downloads/RuntimeBootstrap/cpython-3.13.tar.gz",
    )
    retained = (
        "State/projects.db", "Preferences/editor.json", "Updates/0.4.0/staged.zip",
        "Library/Plugins/.staging/download.tmp", "Runtimes/python314/partial.tmp",
        "Downloads/RuntimeBootstrap/cpython-3.14.tar.gz.part",
    )
    for relative in (*files, *retained):
        put(source, relative)
    plan = migration.inspect_legacy_storage()
    assert len(plan.items) == 5
    progress = []
    assert migration.migrate_legacy_storage(plan, progress=progress.append) == plan.items
    assert progress == [path.as_posix() for path in plan.items]
    for relative in files:
        assert not (source / relative).exists()
        assert (target / relative).read_text() == "payload"
    for relative in retained:
        assert (source / relative).read_text() == "payload"
        assert not (target / relative).exists()


def test_existing_runtime_is_not_merged_or_overwritten(roots):
    source, target = roots
    marker = "Runtimes/python313/.infernux-private-python-runtime.json"
    put(source, marker, "old")
    put(source, "Runtimes/python313/only_in_old.py")
    put(target, marker, "new")
    plan = migration.inspect_legacy_storage()
    assert plan.items == ()
    assert plan.conflicts == (Path("Runtimes/python313"),)
    assert migration.migrate_legacy_storage(plan) == ()
    assert (source / marker).read_text() == "old"
    assert (target / marker).read_text() == "new"
    assert not (target / "Runtimes/python313/only_in_old.py").exists()


def test_preview_is_read_only_and_changed_target_stops_before_move(roots):
    source, target = roots
    relative = "Library/Plugins/packages/demo/1.inxpkg"
    original = put(source, relative)
    plan = migration.inspect_legacy_storage()
    assert not target.exists()
    put(target, relative, "existing")
    with pytest.raises(RuntimeError, match="changed after"):
        migration.migrate_legacy_storage(plan)
    assert original.read_text() == "payload"
    assert (target / relative).read_text() == "existing"


@pytest.mark.parametrize("fail_copy", [False, True])
def test_cross_volume_copy_publishes_whole_unit_before_removing_source(
    roots, monkeypatch, fail_copy
):
    source, target = roots
    relative = Path("Runtimes/python313")
    put(source, relative / ".infernux-private-python-runtime.json")
    put(source, relative / "Lib/nested.py")
    original_rename = Path.rename

    def cross_volume(path, destination):
        if path == source / relative:
            raise OSError(errno.EXDEV, "different device")
        assert (source / relative / "Lib/nested.py").is_file()
        return original_rename(path, destination)

    monkeypatch.setattr(Path, "rename", cross_volume)
    if fail_copy:
        def broken_copy(src, dst, **_kwargs):
            Path(dst).mkdir()
            put(Path(dst), "partial")
            raise OSError("disk full")
        monkeypatch.setattr(migration.shutil, "copytree", broken_copy)
        with pytest.raises(RuntimeError, match="disk full"):
            migration.migrate_legacy_storage(migration.inspect_legacy_storage())
        assert (source / relative / "Lib/nested.py").is_file()
        assert not (target / relative).exists()
    else:
        migration.migrate_legacy_storage(migration.inspect_legacy_storage())
        assert not (source / relative).exists()
        assert (target / relative / "Lib/nested.py").read_text() == "payload"
    assert not list(target.rglob(".hub-migrate-*"))


def test_open_editor_blocks_migration(roots, monkeypatch):
    source, target = roots
    original = put(source, "Library/Plugins/packages/demo/1.inxpkg")
    project = source.parent / "Project"
    project.mkdir()
    monkeypatch.setattr(migration, "is_project_open", lambda _path: True)
    with pytest.raises(RuntimeError, match="Close the Editor"):
        migration.migrate_legacy_storage(migration.inspect_legacy_storage(), [str(project)])
    assert original.is_file()
    assert not target.exists()


def test_permission_failure_does_not_try_copy_fallback(roots, monkeypatch):
    source, target = roots
    original = put(source, "Library/Plugins/packages/demo/1.inxpkg")

    def locked(*_args):
        raise PermissionError("resource is in use")

    monkeypatch.setattr(Path, "rename", locked)
    monkeypatch.setattr(migration.shutil, "copy2", lambda *_args: pytest.fail("unexpected copy"))
    with pytest.raises(RuntimeError, match="resource is in use"):
        migration.migrate_legacy_storage(migration.inspect_legacy_storage())
    assert original.read_text() == "payload"
    assert not list(target.rglob("*.inxpkg"))


def test_explicit_package_cache_override_is_not_changed(roots, monkeypatch):
    source, _target = roots
    original = put(source, "Library/Plugins/packages/demo/1.inxpkg")
    monkeypatch.setenv("INFERNUX_PACKAGE_CACHE_ROOT", str(source / "Library/Plugins"))
    assert migration.inspect_legacy_storage().items == ()
    assert original.is_file()


def test_relocated_package_uses_the_same_relative_cache_reference(roots, monkeypatch):
    import plugin_library

    source, target = roots
    location = "packages/infernux/multiplatform_probe/0.1.0.inxpkg"
    project = source.parent / "Project"
    registry = put(project, "ProjectSettings/InxPlugins.json", json.dumps({
        "$schema": "infernux.plugin_registry",
        "packages": [{"source": {"cache_scope": "hub", "cache_location": location}}],
        "installed": [],
    }))
    before = registry.read_bytes()
    put(source / "Library/Plugins", location)
    migration.migrate_legacy_storage(migration.inspect_legacy_storage())
    monkeypatch.setattr(plugin_library, "get_hub_shared_data_dir", lambda: str(target))
    current = plugin_library.inspect_plugin_library([project])
    assert current.package_count == 1
    assert current.removable == ()
    assert (current.root / location).read_text() == "payload"
    assert registry.read_bytes() == before


def test_same_root_is_noop_and_nested_roots_are_rejected(roots, monkeypatch):
    source, _target = roots
    monkeypatch.setattr(migration, "get_hub_shared_data_dir", lambda: str(source))
    assert migration.inspect_legacy_storage().items == ()
    monkeypatch.setattr(migration, "get_hub_shared_data_dir", lambda: str(source / "Shared"))
    with pytest.raises(ValueError, match="must not overlap"):
        migration.inspect_legacy_storage()
