from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

import Infernux.plugins.manager as plugin_manager_module
import Infernux.plugins.preload as preload_module
from Infernux.engine import player_package_native
from Infernux.plugins import (
    InxPackage,
    PackageConflictError,
    PluginManager,
    PluginRegistry,
    localized_intro,
    parse_markdown_blocks,
    player_file_exported,
    split_markdown_images,
)
from Infernux.plugins.official import (
    OfficialCatalogError,
    install_bundled_packages,
    sync_official_registry,
)
from Infernux.plugins.content import normalize_page_descriptor
from Infernux.plugins.project_index import project_guid_paths


class _FakeInxPack:
    archives: dict[str, dict[str, bytes]] = {}

    @classmethod
    def _write(cls, sources, destination, compression_level=None, profile="development"):
        entries = {
            str(logical).replace("\\", "/"): Path(source).read_bytes()
            for logical, source in sorted(sources)
        }
        path = os.path.abspath(destination)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"INXPKG-v2-test")
        cls.archives[path] = entries
        return cls._manifest(path)

    @classmethod
    def _manifest(cls, path):
        entries = cls.archives[os.path.abspath(path)]
        return {
            "format": "infernux-native-inxpack",
            "revision": 0x00010000,
            "file_count": len(entries),
            "files": [
                {
                    "path": name,
                    "raw_bytes": len(payload),
                    "stored_bytes": len(payload),
                    "sha256": hashlib.sha256(payload).hexdigest(),
                }
                for name, payload in entries.items()
            ],
        }

    @classmethod
    def _read_manifest(cls, path):
        return cls._manifest(path)

    @classmethod
    def _read_entry(cls, path, entry):
        return cls.archives[os.path.abspath(path)][entry]

    _inxpack_write = _write
    _inxpack_read_manifest = _read_manifest
    _inxpack_read_entry = _read_entry


@pytest.fixture(autouse=True)
def _fake_inxpack():
    _FakeInxPack.archives.clear()
    player_package_native.set_test_backend(_FakeInxPack)
    yield
    manager = PluginManager.instance()
    if manager is not None:
        manager.shutdown()
    player_package_native.set_test_backend(None)


def _project(path: Path) -> Path:
    (path / "Assets").mkdir(parents=True)
    (path / "ProjectSettings").mkdir(parents=True)
    return path


def _meta(guid: str) -> str:
    return json.dumps({"metadata": {"guid": {"type": "string", "value": guid}}})


def _source(
    path: Path, reference: str, *, version: str = "1.0.0", engine: str = ""
) -> Path:
    path.mkdir(parents=True)
    (path / "InxPackage.json").write_text(
        json.dumps(
            {
                "reference": reference,
                "name": reference,
                "version": version,
                "engine": engine,
                "requirements": "requirements.txt",
            }
        ),
        encoding="utf-8",
    )
    return path


def _export(source: Path, package: Path) -> Path:
    InxPackage.export_source(str(source), str(package))
    return package


def test_manifestless_folder_export_has_one_generated_plugin_directory(tmp_path):
    project = _project(tmp_path / "project")
    materials = project / "Assets" / "Materials"
    materials.mkdir()
    asset = materials / "Neon.mat"
    asset.write_text("material", encoding="utf-8")
    guid = "0123456789abcdef0123456789abcdef"
    (materials / "Neon.mat.meta").write_text(_meta(guid), encoding="utf-8")

    package = tmp_path / "Materials.inxpkg"
    preview = InxPackage.export(str(project), [str(materials)], str(package))

    assert preview.metadata["reference"] == "materials"
    assert preview.logical_entries == ("Neon.mat",)
    assert preview.project_entries == ("Assets/Plugins/materials/Neon.mat",)
    assert preview.file_records[0]["guid"] == guid


def test_v2_layout_routes_code_control_content_and_nested_packages(tmp_path):
    source = _source(tmp_path / "source", "aabbc/physics/jolt")
    (source / "Runtime").mkdir()
    (source / "Runtime" / "backend.py").write_text("BACKEND = 'jolt'\n", encoding="utf-8")
    (source / "Editor").mkdir()
    (source / "Editor" / "panel.py").write_text("PANEL = True\n", encoding="utf-8")
    (source / "README.md").write_text("# Jolt\n\nPhysics backend.\n", encoding="utf-8")
    (source / "LICENSE").write_text("license", encoding="utf-8")
    (source / "Scenes").mkdir()
    (source / "Scenes" / "Demo.scene").write_text("scene", encoding="utf-8")
    (source / "Variants").mkdir()
    (source / "Variants" / "Legacy.inxpkg").write_bytes(b"opaque nested package")

    preview = InxPackage.inspect(str(_export(source, tmp_path / "Jolt.inxpkg")))

    assert preview.metadata["$schema"] == "infernux.inxpackage"
    assert preview.metadata["format_version"] == 2
    assert len({item["guid"] for item in preview.file_records}) == len(preview.file_records)
    assert "Packages/aabbc/physics/jolt/Runtime/backend.py" in preview.project_entries
    assert "Packages/aabbc/physics/jolt/Editor/panel.py" in preview.project_entries
    assert "Packages/aabbc/physics/jolt/README.md" in preview.project_entries
    assert "Assets/Plugins/aabbc/physics/jolt/Scenes/Demo.scene" in preview.project_entries
    assert "Assets/Plugins/aabbc/physics/jolt/Variants/Legacy.inxpkg" in preview.project_entries

    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.install_package(str(tmp_path / "Jolt.inxpkg"), install_dependencies=False)
    assert (project / "Packages/aabbc/physics/jolt/Runtime/backend.py").is_file()
    assert (project / "Assets/Plugins/aabbc/physics/jolt/Scenes/Demo.scene").is_file()
    assert {item["reference"] for item in manager.registry.installed()} == {
        "aabbc/physics/jolt"
    }


def test_export_preserves_meta_guid_and_is_deterministic(tmp_path):
    source = _source(tmp_path / "source", "vendor/deterministic")
    asset = source / "Texture.bin"
    asset.write_bytes(b"texture")
    guid = "0123456789abcdef0123456789abcdef"
    (source / "Texture.bin.meta").write_text(_meta(guid), encoding="utf-8")

    first = InxPackage.inspect(str(_export(source, tmp_path / "first.inxpkg")))
    second = InxPackage.inspect(str(_export(source, tmp_path / "second.inxpkg")))
    first_record = next(item for item in first.file_records if item["logical_path"] == "Texture.bin")
    second_record = next(item for item in second.file_records if item["logical_path"] == "Texture.bin")
    assert first_record == second_record
    assert first_record["guid"] == guid
    assert first.metadata["control_guid"] == second.metadata["control_guid"]


def test_generated_guid_is_stable_when_package_content_changes(tmp_path):
    source = _source(tmp_path / "source", "vendor/stable-identity")
    asset = source / "Runtime" / "backend.py"
    asset.parent.mkdir()
    asset.write_text("VALUE = 1\n", encoding="utf-8")

    first = InxPackage.inspect(str(_export(source, tmp_path / "first.inxpkg")))
    first_record = next(
        item
        for item in first.file_records
        if item["logical_path"] == "Runtime/backend.py"
    )
    asset.write_text("VALUE = 2\n", encoding="utf-8")
    second = InxPackage.inspect(str(_export(source, tmp_path / "second.inxpkg")))
    second_record = next(
        item
        for item in second.file_records
        if item["logical_path"] == "Runtime/backend.py"
    )

    assert first_record["guid"] == second_record["guid"]
    assert first_record["sha256"] != second_record["sha256"]


def test_engine_compatibility_is_validated_and_enforced_before_install(tmp_path):
    invalid = _source(tmp_path / "invalid", "vendor/invalid-engine", engine=">=oops")
    (invalid / "Data.bin").write_bytes(b"payload")
    with pytest.raises(ValueError, match="engine compatibility is invalid"):
        _export(invalid, tmp_path / "invalid.inxpkg")

    incompatible = _source(
        tmp_path / "incompatible", "vendor/future-engine", engine=">=99"
    )
    (incompatible / "Data.bin").write_bytes(b"payload")
    package = _export(incompatible, tmp_path / "future.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    with pytest.raises(RuntimeError, match="current engine is 0.4.0"):
        manager.install_package(str(package), install_dependencies=False)
    assert manager.registry.installed() == ()
    assert not (project / "Assets/Plugins/vendor/future-engine/Data.bin").exists()


@pytest.mark.parametrize("directory", ["runtime", "RUNTIME", "editor"])
def test_reserved_layout_requires_canonical_casing(tmp_path, directory):
    source = _source(tmp_path / "source", "vendor/case")
    (source / directory).mkdir()
    (source / directory / "entry.py").write_text("pass\n", encoding="utf-8")
    with pytest.raises(ValueError, match="canonical casing"):
        _export(source, tmp_path / "bad.inxpkg")


def test_docs_has_no_special_layout_role(tmp_path):
    source = _source(tmp_path / "source", "vendor/docs")
    (source / "Docs").mkdir()
    (source / "Docs" / "Guide.md").write_text("ordinary asset", encoding="utf-8")
    package = _export(source, tmp_path / "docs.inxpkg")
    preview = InxPackage.inspect(str(package))
    assert "Assets/Plugins/vendor/docs/Docs/Guide.md" in preview.project_entries
    assert all(page["path"] != "Docs/Guide.md" for page in preview.metadata["pages"])


def test_manifest_cannot_register_information_pages_outside_fixed_layout(tmp_path):
    source = _source(tmp_path / "source", "vendor/strict-pages")
    (source / "Docs").mkdir()
    (source / "Docs" / "Guide.md").write_text("not a plugin page", encoding="utf-8")
    manifest_path = source / "InxPackage.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pages"] = [
        {"id": "guide", "title": "Guide", "path": "Docs/Guide.md"}
    ]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="page path is invalid"):
        _export(source, tmp_path / "invalid-pages.inxpkg")


@pytest.mark.parametrize("locale", ["zh", "zh-cn", "en", "en-US"])
def test_plugin_page_descriptor_rejects_locale_aliases(locale):
    with pytest.raises(ValueError, match="page locale must be zh-CN"):
        normalize_page_descriptor(
            {
                "id": "intro",
                "title": "Description",
                "path": "README.zh-CN.md",
                "locale": locale,
            }
        )


def test_guid_reuse_transfers_ownership_on_uninstall(tmp_path):
    guid = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    packages = []
    for reference in ("aabbc/physics", "aabbc/physics/jolt"):
        source = _source(tmp_path / reference.replace("/", "-"), reference)
        asset = source / "Shared.bin"
        asset.write_bytes(b"same bytes")
        (source / "Shared.bin.meta").write_text(_meta(guid), encoding="utf-8")
        packages.append(_export(source, tmp_path / f"{reference.replace('/', '-')}.inxpkg"))

    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.install_package(str(packages[0]), install_dependencies=False)
    manager.install_package(str(packages[1]), install_dependencies=False)
    original = project / "Assets/Plugins/aabbc/physics/Shared.bin"
    child_record = manager.registry.installed_record("aabbc/physics/jolt")
    shared = next(item for item in child_record["files"] if item["guid"] == guid)
    assert shared["owned"] is False
    assert Path(project, *shared["path_hint"].split("/")).resolve() == original.resolve()

    manager.uninstall("aabbc/physics")
    assert original.is_file()
    child_record = manager.registry.installed_record("aabbc/physics/jolt")
    shared = next(item for item in child_record["files"] if item["guid"] == guid)
    assert shared["owned"] is True
    manager.uninstall("aabbc/physics/jolt")


def test_uninstall_dependency_blocker_is_case_insensitive(tmp_path):
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.registry.record_install(
        {"reference": "vendor/x"},
        files=[],
        control={
            "guid": "dependency-control-guid",
            "path_hint": "Packages/vendor/x/InxPackage.json",
            "owned": False,
        },
    )
    manager.registry.record_install(
        {"reference": "vendor/consumer"},
        files=[],
        control={
            "guid": "consumer-control-guid",
            "path_hint": "Packages/vendor/consumer/InxPackage.json",
            "owned": False,
        },
        dependencies=["Vendor/X"],
    )

    with pytest.raises(RuntimeError, match="vendor/consumer"):
        manager.uninstall("VENDOR/X")

    assert manager.registry.installed_record("vendor/x") is not None


def test_invalid_official_catalog_does_not_mutate_project_registry(tmp_path):
    project = _project(tmp_path / "project")
    registry = PluginRegistry(str(project))
    registry.add_package(
        "vendor/local",
        source={"type": "git", "location": "https://example.invalid/local.git"},
    )
    registry.add_package(
        "infernux/previous",
        source={"type": "local", "location": "old.inxpkg", "official": True},
    )
    resources = tmp_path / "resources"
    official = resources / "official_packages"
    official.mkdir(parents=True)
    (official / "broken.inxpkg").write_bytes(b"broken")
    (official / "official-registry.json").write_text(
        json.dumps(
            {
                "$schema": "infernux.official_plugin_registry",
                "catalog_version": 1,
                "packages": [
                    {
                        "reference": "infernux/broken",
                        "artifact": "broken.inxpkg",
                        "artifact_sha256": "0" * 64,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    before = registry.load()

    with pytest.raises(OfficialCatalogError, match="hash mismatch"):
        sync_official_registry(str(project), resources_root=str(resources))

    assert registry.load() == before


def test_startup_degrades_when_official_catalog_is_unavailable(tmp_path):
    project = _project(tmp_path / "project")
    registry = PluginRegistry(str(project))
    registry.add_package(
        "vendor/local",
        source={"type": "git", "location": "https://example.invalid/local.git"},
    )

    manager = PluginManager.startup(str(project))

    assert "Official plugin catalog is unavailable" in manager.official_catalog_error
    assert manager.registry.find("vendor/local") is not None


def test_resources_root_inxpackages_are_mandatory_and_idempotent(tmp_path):
    source = _source(tmp_path / "source", "infernux/platform-fixture")
    (source / "Runtime").mkdir()
    (source / "Runtime" / "fixture.py").write_text(
        "PLATFORM_FIXTURE = True\n", encoding="utf-8"
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    package = _export(source, resources / "infernux.platform-fixture.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project), runtime=False)

    first = install_bundled_packages(
        str(project), resources_root=str(resources), manager=manager
    )
    second = install_bundled_packages(
        str(project), resources_root=str(resources), manager=manager
    )

    assert [state.reference for state in first] == ["infernux/platform-fixture"]
    assert [state.reference for state in second] == ["infernux/platform-fixture"]
    record = manager.registry.installed_record("infernux/platform-fixture")
    assert record is not None
    assert record["source"]["location"] == str(package.resolve())
    assert record["source"]["builtin"] is True
    assert (project / "Packages/infernux/platform-fixture/Runtime/fixture.py").is_file()


def test_startup_installs_resources_root_inxpackages_without_official_catalog(
    tmp_path, monkeypatch
):
    source = _source(tmp_path / "source", "infernux/platform-fixture")
    (source / "Runtime").mkdir()
    (source / "Runtime" / "fixture.py").write_text(
        "PLATFORM_FIXTURE = True\n", encoding="utf-8"
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    _export(source, resources / "infernux.platform-fixture.inxpkg")
    project = _project(tmp_path / "project")
    monkeypatch.setattr(
        "Infernux.resources.get_package_resources_path", lambda: str(resources)
    )

    manager = PluginManager.startup(str(project), runtime=False)

    assert manager.registry.installed_record("infernux/platform-fixture") is not None
    assert "Official plugin catalog is unavailable" in manager.official_catalog_error


def test_duplicate_resources_root_package_reference_is_rejected_before_install(
    tmp_path,
):
    source = _source(tmp_path / "source", "infernux/platform-fixture")
    (source / "Runtime").mkdir()
    (source / "Runtime" / "fixture.py").write_text(
        "PLATFORM_FIXTURE = True\n", encoding="utf-8"
    )
    resources = tmp_path / "resources"
    resources.mkdir()
    _export(source, resources / "first.inxpkg")
    _export(source, resources / "second.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project), runtime=False)

    with pytest.raises(OfficialCatalogError, match="duplicated"):
        install_bundled_packages(
            str(project), resources_root=str(resources), manager=manager
        )

    assert manager.registry.installed() == ()


def test_guid_conflict_is_rejected_without_partial_files(tmp_path):
    first = _source(tmp_path / "first", "vendor/first")
    (first / "Shared.bin").write_bytes(b"one")
    (first / "Shared.bin.meta").write_text(
        _meta("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"), encoding="utf-8"
    )
    second = _source(tmp_path / "second", "vendor/second")
    (second / "Shared.bin").write_bytes(b"two")
    (second / "Shared.bin.meta").write_text(
        _meta("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"), encoding="utf-8"
    )
    first_package = _export(first, tmp_path / "first.inxpkg")
    second_package = _export(second, tmp_path / "second.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.install_package(str(first_package), install_dependencies=False)
    with pytest.raises(PackageConflictError, match="different content"):
        manager.install_package(str(second_package), install_dependencies=False)
    assert manager.registry.installed_record("vendor/second") is None
    assert not (project / "Assets/Plugins/vendor/second/Shared.bin").exists()


def test_target_path_with_different_guid_is_rejected_without_overwrite(tmp_path):
    source = _source(tmp_path / "source", "vendor/path-conflict")
    payload = source / "Data.bin"
    payload.write_bytes(b"package bytes")
    (source / "Data.bin.meta").write_text(
        _meta("11111111111111111111111111111111"), encoding="utf-8"
    )
    package = _export(source, tmp_path / "path-conflict.inxpkg")
    project = _project(tmp_path / "project")
    occupied = project / "Assets/Plugins/vendor/path-conflict/Data.bin"
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(b"user bytes")
    Path(str(occupied) + ".meta").write_text(
        _meta("22222222222222222222222222222222"), encoding="utf-8"
    )
    manager = PluginManager(str(project))

    with pytest.raises(PackageConflictError, match="occupied by another GUID"):
        manager.install_package(str(package), install_dependencies=False)

    assert occupied.read_bytes() == b"user bytes"
    assert manager.registry.installed() == ()


def test_identical_orphaned_payload_is_adopted_with_package_guid(tmp_path):
    source = _source(tmp_path / "source", "vendor/adopt-identical")
    payload = source / "Runtime" / "backend.py"
    payload.parent.mkdir()
    payload.write_text("VALUE = 7\n", encoding="utf-8")
    package = _export(source, tmp_path / "adopt-identical.inxpkg")
    preview = InxPackage.inspect(str(package))
    expected = next(
        item
        for item in preview.file_records
        if item["logical_path"] == "Runtime/backend.py"
    )

    project = _project(tmp_path / "project")
    occupied = project / "Packages/vendor/adopt-identical/Runtime/backend.py"
    occupied.parent.mkdir(parents=True)
    occupied.write_bytes(payload.read_bytes())
    previous_guid = "22222222222222222222222222222222"
    Path(str(occupied) + ".meta").write_text(
        _meta(previous_guid), encoding="utf-8"
    )
    manager = PluginManager(str(project))

    manager.install_package(str(package), install_dependencies=False)

    document = json.loads(
        Path(str(occupied) + ".meta").read_text(encoding="utf-8")
    )
    assert document["metadata"]["guid"]["value"] == expected["guid"]
    record = manager.registry.installed_record("vendor/adopt-identical")
    installed = next(
        item
        for item in record["files"]
        if item["logical_path"] == "Runtime/backend.py"
    )
    assert installed["owned"] is True


def test_install_rolls_back_files_and_registry_if_registration_fails(tmp_path, monkeypatch):
    source = _source(tmp_path / "source", "vendor/rollback")
    (source / "Data.bin").write_bytes(b"payload")
    package = _export(source, tmp_path / "rollback.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    original = manager.registry.record_install

    def fail_after_save(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("simulated post-save failure")

    monkeypatch.setattr(manager.registry, "record_install", fail_after_save)
    with pytest.raises(RuntimeError, match="post-save"):
        manager.install_package(str(package), install_dependencies=False)
    assert manager.registry.installed() == ()
    assert not (project / "Assets/Plugins/vendor/rollback/Data.bin").exists()
    assert not (project / "Packages/vendor/rollback/InxPackage.json").exists()


def test_rejected_parent_install_removes_new_plugin_dependencies(tmp_path, monkeypatch):
    project = _project(tmp_path / "project")
    child_source = _source(tmp_path / "child", "vendor/child")
    (child_source / "Child.bin").write_bytes(b"child")
    child_package = _export(child_source, tmp_path / "child.inxpkg")
    parent_source = _source(tmp_path / "parent", "vendor/parent")
    (parent_source / "requirements.txt").write_text(
        "vendor/child\n", encoding="utf-8"
    )
    (parent_source / "Parent.bin").write_bytes(b"parent")
    parent_package = _export(parent_source, tmp_path / "parent.inxpkg")

    manager = PluginManager(str(project))
    manager.registry.add_package(
        "vendor/child",
        source={"type": "local", "location": str(child_package)},
    )
    original_record_install = manager.registry.record_install

    def reject_parent(metadata, **kwargs):
        if metadata.get("reference") == "vendor/parent":
            raise RuntimeError("parent registry failure")
        return original_record_install(metadata, **kwargs)

    monkeypatch.setattr(manager.registry, "record_install", reject_parent)
    with pytest.raises(RuntimeError, match="parent registry failure"):
        manager.install_package(str(parent_package))

    assert manager.registry.installed() == ()
    assert not (project / "Assets/Plugins/vendor/child/Child.bin").exists()
    assert not (project / "Assets/Plugins/vendor/parent/Parent.bin").exists()


def test_direct_package_uses_verified_cache_for_offline_reinstall_and_lock(tmp_path):
    source = _source(tmp_path / "source", "vendor/offline")
    (source / "Data.bin").write_bytes(b"offline payload")
    package = _export(source, tmp_path / "offline.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    first = manager.install_package(str(package), install_dependencies=False)
    record = manager.registry.installed_record("vendor/offline")
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    cache = project / f"Library/InxPackageCache/{digest}.inxpkg"
    assert first.loaded is True
    assert cache.is_file()
    _FakeInxPack.archives[str(cache.resolve())] = dict(
        _FakeInxPack.archives[str(package.resolve())]
    )
    assert record["package_sha256"] == digest
    assert record["transaction_id"]
    lock = json.loads(
        (project / "ProjectSettings/InxPackages.lock.json").read_text(encoding="utf-8")
    )
    assert lock["packages"][0]["package_sha256"] == digest

    manager.uninstall("vendor/offline")
    package.unlink()
    reinstalled = manager.install_reference("vendor/offline", install_dependencies=False)
    assert reinstalled.loaded is True
    assert manager.registry.installed_record("vendor/offline")["source"][
        "cache_location"
    ].endswith(f"{digest}.inxpkg")


def test_arbitrary_pip_syntax_is_project_scoped_and_written_to_lock(tmp_path, monkeypatch):
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    monkeypatch.setattr(manager, "_project_python_executable", lambda: "project-python")
    calls = []
    environment = {}

    def run(command, cwd=None):
        calls.append((command, cwd))
        if command[2:5] == ["pip", "list", "--format=json"]:
            return type("Result", (), {"stdout": json.dumps([
                {"name": name, "version": version}
                for name, version in environment.items()
            ])})()
        environment["demo"] = "2.0"
        return type("Result", (), {"stdout": "installed wheel"})()

    monkeypatch.setattr(manager, "_run_process", run)
    result = manager.install_pip(
        'pip install --index-url https://packages.example/simple "demo[vision]>=2"'
    )
    assert result["command"] == [
        "project-python",
        "-m",
        "pip",
        "install",
        "--index-url",
        "https://packages.example/simple",
        "demo[vision]>=2",
    ]
    lock = json.loads(
        (project / "ProjectSettings/InxPackages.lock.json").read_text(encoding="utf-8")
    )
    assert lock["python"][0]["syntax"].startswith("pip install")
    assert lock["python"][0]["output"] == "installed wheel"
    assert lock["python_dependencies"][0]["name"] == "demo"
    assert lock["python_dependencies"][0]["owners"][0]["reference"] == "@project"


def test_direct_local_github_git_and_http_sources_converge_on_same_inventory(
    tmp_path, monkeypatch
):
    source = _source(tmp_path / "source", "vendor/distributed")
    (source / "Runtime").mkdir()
    (source / "Runtime" / "api.py").write_text("VALUE = 7\n", encoding="utf-8")
    (source / "Scene.scene").write_text("scene", encoding="utf-8")
    package = _export(source, tmp_path / "distributed.inxpkg")
    expected_archive = dict(_FakeInxPack.archives[str(package.resolve())])

    inventories = []
    cases = (
        ("direct", str(package)),
        ("local", {"type": "local", "location": str(source)}),
        ("github", {"type": "github", "location": "https://github.test/vendor/repo.git"}),
        ("git", {"type": "git", "location": "ssh://git.internal/vendor/repo.git"}),
        ("http", {"type": "url", "location": "https://packages.internal/distributed.inxpkg"}),
    )
    for label, descriptor in cases:
        project = _project(tmp_path / f"project-{label}")
        manager = PluginManager(str(project))

        def run_process(command, cwd=None):
            if command[:2] == ["git", "clone"]:
                checkout = Path(command[-1])
                shutil.copytree(source, checkout, dirs_exist_ok=True)
            return type("Result", (), {"stdout": ""})()

        monkeypatch.setattr(manager, "_run_process", run_process)

        def retrieve(_url, destination):
            shutil.copy2(package, destination)
            _FakeInxPack.archives[str(Path(destination).resolve())] = dict(expected_archive)

        monkeypatch.setattr(plugin_manager_module, "_download_url_package", retrieve)
        state = manager.install_source(descriptor, install_dependencies=False)
        assert state.reference == "vendor/distributed"
        record = manager.registry.installed_record("vendor/distributed")
        inventories.append(
            tuple(
                sorted(
                    (item["logical_path"], item["guid"], item["sha256"])
                    for item in record["files"]
                )
            )
        )
        manager.shutdown()
    assert all(inventory == inventories[0] for inventory in inventories)


def test_http_package_download_streams_without_forced_time_or_size_limits(
    tmp_path, monkeypatch
):
    class Response:
        def __init__(self, chunks, content_length=""):
            self.headers = {"Content-Length": content_length} if content_length else {}
            self._chunks = iter(chunks)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _size):
            return next(self._chunks, b"")

    observed = {}

    def urlopen(request):
        observed["url"] = request.full_url
        return Response((b"1234", b"5678", b"9"), content_length="999999999999")

    monkeypatch.setattr(plugin_manager_module.urllib.request, "urlopen", urlopen)
    destination = tmp_path / "download.inxpkg"
    plugin_manager_module._download_url_package(
        "https://packages.example/plugin.inxpkg",
        str(destination),
    )
    assert observed == {"url": "https://packages.example/plugin.inxpkg"}
    assert destination.read_bytes() == b"123456789"

    class FailedResponse(Response):
        def read(self, _size):
            chunk = next(self._chunks, None)
            if chunk is None:
                raise OSError("connection lost")
            return chunk

    def interrupted(_request):
        return FailedResponse((b"partial",))

    monkeypatch.setattr(
        plugin_manager_module.urllib.request,
        "urlopen",
        interrupted,
    )
    with pytest.raises(OSError, match="connection lost"):
        plugin_manager_module._download_url_package(
            "https://packages.example/interrupted.inxpkg",
            str(destination),
        )
    assert destination.read_bytes() == b"123456789"


def test_uninstall_follows_guid_after_user_moves_asset_and_preserves_modification(tmp_path):
    source = _source(tmp_path / "source", "vendor/movable")
    (source / "Data.bin").write_bytes(b"payload")
    package = _export(source, tmp_path / "movable.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.install_package(str(package), install_dependencies=False)
    old = project / "Assets/Plugins/vendor/movable/Data.bin"
    moved = project / "Assets/UserLayout/Moved.bin"
    moved.parent.mkdir(parents=True)
    old.replace(moved)
    Path(str(old) + ".meta").replace(Path(str(moved) + ".meta"))
    manager.uninstall("vendor/movable")
    assert not moved.exists()

    manager.install_package(str(package), install_dependencies=False)
    old.write_bytes(b"user modified")
    result = manager.uninstall("vendor/movable")
    assert old.is_file()
    assert str(old) in result["preserved_modified_files"]


def test_uninstall_file_failure_rolls_back_payload_meta_and_registry(tmp_path, monkeypatch):
    source = _source(tmp_path / "source", "vendor/uninstall-rollback")
    (source / "Data.bin").write_bytes(b"payload")
    package = _export(source, tmp_path / "uninstall-rollback.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.install_package(str(package), install_dependencies=False)
    payload = project / "Assets/Plugins/vendor/uninstall-rollback/Data.bin"
    meta = Path(str(payload) + ".meta")
    original_remove = os.remove
    failed = False

    def fail_once(path):
        nonlocal failed
        if not failed and Path(path).resolve() == meta.resolve():
            failed = True
            raise PermissionError("simulated locked metadata")
        return original_remove(path)

    monkeypatch.setattr("Infernux.plugins.manager.os.remove", fail_once)
    with pytest.raises(PermissionError, match="locked metadata"):
        manager.uninstall("vendor/uninstall-rollback")

    assert payload.read_bytes() == b"payload"
    assert meta.is_file()
    assert manager.registry.installed_record("vendor/uninstall-rollback") is not None


def test_runtime_native_payload_uses_package_route_and_player_policy(tmp_path):
    source = _source(tmp_path / "source", "vendor/native-runtime")
    runtime = source / "Runtime"
    runtime.mkdir()
    (runtime / "backend.pyd").write_bytes(b"native-extension-fixture")
    package = _export(source, tmp_path / "native-runtime.inxpkg")
    preview = InxPackage.inspect(str(package))
    record = next(
        item for item in preview.file_records if item["logical_path"] == "Runtime/backend.pyd"
    )
    assert record["role"] == "runtime"
    assert player_file_exported(preview.metadata, "Runtime/backend.pyd") is True

    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.install_package(str(package), install_dependencies=False)
    installed = project / "Packages/vendor/native-runtime/Runtime/backend.pyd"
    assert installed.read_bytes() == b"native-extension-fixture"
    assert Path(str(installed) + ".meta").is_file()


def test_requirements_failure_rolls_back_new_plugin_dependencies(tmp_path, monkeypatch):
    dependency = _source(tmp_path / "dependency", "vendor/requirement-child")
    (dependency / "Child.bin").write_bytes(b"child")
    dependency_package = _export(dependency, tmp_path / "requirement-child.inxpkg")
    parent = _source(tmp_path / "parent", "vendor/requirement-parent")
    (parent / "requirements.txt").write_text(
        "vendor/requirement-child\nbroken-wheel==1\n", encoding="utf-8"
    )
    (parent / "Parent.bin").write_bytes(b"parent")
    parent_package = _export(parent, tmp_path / "requirement-parent.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.registry.add_package(
        "vendor/requirement-child",
        source={"type": "local", "location": str(dependency_package)},
    )
    monkeypatch.setattr(manager, "_project_python_executable", lambda: "project-python")
    monkeypatch.setattr(
        manager,
        "_run_process",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated pip resolution failure")
        ),
    )

    with pytest.raises(RuntimeError, match="pip resolution failure"):
        manager.install_package(str(parent_package))

    assert manager.registry.installed() == ()
    assert not (project / "Assets/Plugins/vendor/requirement-child/Child.bin").exists()
    assert not (project / "Assets/Plugins/vendor/requirement-parent/Parent.bin").exists()


def test_pip_side_effect_rolls_back_when_file_registry_transaction_fails(
    tmp_path, monkeypatch
):
    source = _source(tmp_path / "source", "vendor/pip-rollback")
    (source / "requirements.txt").write_text(
        "shared-wheel==1.0\n", encoding="utf-8"
    )
    (source / "Data.bin").write_bytes(b"payload")
    package = _export(source, tmp_path / "pip-rollback.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    environment: dict[str, str] = {}
    commands: list[list[str]] = []

    def run(command, cwd=None):
        commands.append(list(command))
        if command[2:5] == ["pip", "list", "--format=json"]:
            return type("Result", (), {"stdout": json.dumps([
                {"name": name, "version": version}
                for name, version in environment.items()
            ])})()
        if command[2:4] == ["pip", "install"]:
            environment["shared-wheel"] = "1.0"
        elif command[2:4] == ["pip", "uninstall"]:
            for name in command[5:]:
                environment.pop(name, None)
        return type("Result", (), {"stdout": "ok"})()

    monkeypatch.setattr(manager, "_project_python_executable", lambda: "project-python")
    monkeypatch.setattr(manager, "_run_process", run)
    monkeypatch.setattr(
        manager.registry,
        "record_install",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("simulated registry rejection")
        ),
    )

    with pytest.raises(RuntimeError, match="registry rejection"):
        manager.install_package(str(package))

    assert environment == {}
    assert manager.registry.installed() == ()
    assert any(command[2:4] == ["pip", "uninstall"] for command in commands)
    assert not (project / "Assets/Plugins/vendor/pip-rollback/Data.bin").exists()


def test_shared_pip_distribution_is_removed_only_after_last_plugin_owner(
    tmp_path, monkeypatch
):
    packages = []
    for label in ("first", "second"):
        source = _source(tmp_path / label, f"vendor/{label}")
        (source / "requirements.txt").write_text(
            "shared-wheel==1.0\n", encoding="utf-8"
        )
        (source / f"{label}.bin").write_bytes(label.encode("utf-8"))
        packages.append(_export(source, tmp_path / f"{label}.inxpkg"))
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    environment: dict[str, str] = {}
    commands: list[list[str]] = []

    def run(command, cwd=None):
        commands.append(list(command))
        if command[2:5] == ["pip", "list", "--format=json"]:
            return type("Result", (), {"stdout": json.dumps([
                {"name": name, "version": version}
                for name, version in environment.items()
            ])})()
        if command[2:4] == ["pip", "install"]:
            environment["shared-wheel"] = "1.0"
        elif command[2:4] == ["pip", "uninstall"]:
            for name in command[5:]:
                environment.pop(name, None)
        return type("Result", (), {"stdout": "ok"})()

    monkeypatch.setattr(manager, "_project_python_executable", lambda: "project-python")
    monkeypatch.setattr(manager, "_run_process", run)
    manager.install_package(str(packages[0]))
    manager.install_package(str(packages[1]))
    ledger = manager.registry.load()["python_dependencies"]
    assert [owner["reference"] for owner in ledger[0]["owners"]] == [
        "vendor/first",
        "vendor/second",
    ]

    manager.uninstall("vendor/first")
    assert environment == {"shared-wheel": "1.0"}
    assert not any(
        command[2:4] == ["pip", "uninstall"] for command in commands
    )
    manager.uninstall("vendor/second")
    assert environment == {}
    assert sum(
        command[2:4] == ["pip", "uninstall"] for command in commands
    ) == 1
    assert manager.registry.load()["python_dependencies"] == []


def test_startup_restores_installed_plugin_requirements_in_new_environment(
    tmp_path, monkeypatch
):
    source = _source(tmp_path / "source", "vendor/portable-python")
    (source / "requirements.txt").write_text(
        "shared-wheel>=1,<2\n", encoding="utf-8"
    )
    (source / "Data.bin").write_bytes(b"payload")
    package = _export(source, tmp_path / "portable-python.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    environment: dict[str, str] = {}
    commands: list[list[str]] = []

    def run(command, cwd=None):
        commands.append(list(command))
        if command[2:5] == ["pip", "list", "--format=json"]:
            return type(
                "Result",
                (),
                {
                    "stdout": json.dumps(
                        [
                            {"name": name, "version": version}
                            for name, version in environment.items()
                        ]
                    )
                },
            )()
        if command[2:4] == ["pip", "install"]:
            environment["shared-wheel"] = "1.5"
        return type("Result", (), {"stdout": "ok"})()

    monkeypatch.setattr(manager, "_project_python_executable", lambda: "project-python")
    monkeypatch.setattr(manager, "_run_process", run)
    manager.install_package(str(package))
    environment.clear()
    commands.clear()

    assert manager._reconcile_python_requirements_for_startup() == (
        "vendor/portable-python",
    )
    assert environment == {"shared-wheel": "1.5"}
    assert sum(command[2:4] == ["pip", "install"] for command in commands) == 1
    ledger = manager.registry.load()["python_dependencies"]
    assert ledger[0]["owners"] == [
        {
            "reference": "vendor/portable-python",
            "requirements": ["shared-wheel>=1,<2"],
        }
    ]
    assert ledger[0]["baseline_version"] == ""
    assert ledger[0]["installed_version"] == "1.5"

    commands.clear()
    assert manager._reconcile_python_requirements_for_startup() == ()
    assert not any(command[2:4] == ["pip", "install"] for command in commands)


def test_reference_identity_is_casefolded_across_platforms(tmp_path):
    lower = _source(tmp_path / "lower", "Vendor/CaseIdentity")
    upper = _source(tmp_path / "upper", "vendor/caseidentity")
    (lower / "Data.bin").write_bytes(b"one")
    (upper / "Data.bin").write_bytes(b"two")
    lower_package = _export(lower, tmp_path / "lower.inxpkg")
    upper_package = _export(upper, tmp_path / "upper.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.install_package(str(lower_package), install_dependencies=False)

    with pytest.raises(RuntimeError, match="already installed with different content"):
        manager.install_package(str(upper_package), install_dependencies=False)

    installed = manager.registry.installed()
    assert len(installed) == 1
    assert installed[0]["reference"] == "Vendor/CaseIdentity"


def test_failed_preload_unload_aborts_uninstall_and_requires_restart(tmp_path):
    source = _source(tmp_path / "source", "vendor/stubborn")
    runtime = source / "Runtime"
    runtime.mkdir()
    (runtime / "service.py").write_text(
        "from Infernux.lifecycle import InxPreload\n"
        "class StubbornService(InxPreload):\n"
        "    def preload(self, context): pass\n"
        "    def unload(self): raise RuntimeError('service still running')\n",
        encoding="utf-8",
    )
    package = _export(source, tmp_path / "stubborn.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.install_package(str(package), install_dependencies=False)
    script = project / "Packages/vendor/stubborn/Runtime/service.py"

    with pytest.raises(RuntimeError, match="uninstall aborted"):
        manager.uninstall("vendor/stubborn")

    assert script.is_file()
    assert manager.registry.installed_record("vendor/stubborn") is not None
    failed_state = next(iter(manager.preloads.states.values()))
    assert failed_state.loaded is True
    assert failed_state.restart_required is True
    assert "service still running" in failed_state.error

    failed_state.instance.unload = lambda: None
    manager.uninstall("vendor/stubborn")
    assert not script.exists()


def test_package_preload_supports_relative_imports(tmp_path):
    source = _source(tmp_path / "source", "vendor/relative-preload")
    package_module = source / "Editor" / "infernux_relative_preload"
    package_module.mkdir(parents=True)
    (package_module / "__init__.py").write_text("", encoding="utf-8")
    (package_module / "service.py").write_text(
        "VALUE = 'relative-import-ready'\n", encoding="utf-8"
    )
    (package_module / "lifecycle.py").write_text(
        "from pathlib import Path\n"
        "from Infernux.lifecycle import InxPreload\n"
        "from .service import VALUE\n"
        "class RelativePreload(InxPreload):\n"
        "    def preload(self, context):\n"
        "        Path(context.project_root, 'relative-preload.txt').write_text(VALUE)\n",
        encoding="utf-8",
    )
    package = _export(source, tmp_path / "relative-preload.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))

    state = manager.install_package(str(package), install_dependencies=False)

    assert state.loaded
    assert state.error == ""
    assert (project / "relative-preload.txt").read_text(encoding="utf-8") == (
        "relative-import-ready"
    )


def test_stubborn_plugin_does_not_block_unrelated_plugin_lifecycle(
    tmp_path, monkeypatch
):
    stubborn = _source(tmp_path / "stubborn", "vendor/stubborn-slice")
    (stubborn / "Runtime").mkdir()
    (stubborn / "Runtime" / "service.py").write_text(
        "from Infernux.lifecycle import InxPreload\n"
        "class Stubborn(InxPreload):\n"
        "    def preload(self, context): pass\n"
        "    def unload(self): raise RuntimeError('restart only me')\n",
        encoding="utf-8",
    )
    safe = _source(tmp_path / "safe", "vendor/safe-slice")
    (safe / "Runtime").mkdir()
    (safe / "Runtime" / "service.py").write_text(
        "from pathlib import Path\n"
        "from Infernux.lifecycle import InxPreload\n"
        "class Safe(InxPreload):\n"
        "    def preload(self, context): self.root=context.project_root\n"
        "    def unload(self): Path(self.root, 'safe-unloaded.txt').write_text('yes')\n",
        encoding="utf-8",
    )
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.install_package(
        str(_export(stubborn, tmp_path / "stubborn-slice.inxpkg")),
        install_dependencies=False,
    )
    manager.install_package(
        str(_export(safe, tmp_path / "safe-slice.inxpkg")),
        install_dependencies=False,
    )
    stubborn_state = next(
        state
        for state in manager.preloads.states.values()
        if state.package_reference == "vendor/stubborn-slice"
    )
    stubborn_instance = stubborn_state.instance
    monkeypatch.setattr(
        manager.preloads,
        "_source_paths",
        lambda: pytest.fail("targeted lifecycle performed a full AST scan"),
    )

    manager.set_enabled("vendor/safe-slice", False)
    assert stubborn_state.instance is stubborn_instance
    manager.set_enabled("vendor/safe-slice", True)
    manager.uninstall("vendor/safe-slice")
    assert stubborn_state.instance is stubborn_instance
    assert (project / "safe-unloaded.txt").is_file()

    stubborn_state.instance.unload = lambda: None
    manager.shutdown()


def test_direct_manager_construction_does_not_replace_active_singleton(tmp_path):
    active_project = _project(tmp_path / "active")
    detached_project = _project(tmp_path / "detached")
    active = PluginManager.startup(str(active_project))
    detached = PluginManager(str(detached_project))
    assert PluginManager.instance() is active
    detached.shutdown()
    assert PluginManager.instance() is active


def test_native_guid_index_avoids_meta_walk_and_drives_preload_catalog(
    tmp_path, monkeypatch
):
    project = _project(tmp_path / "project")
    script = project / "Assets" / "indexed.py"
    script.write_text(
        "from Infernux.lifecycle import InxPreload\n"
        "class Indexed(InxPreload):\n"
        "    def preload(self, context): pass\n",
        encoding="utf-8",
    )
    guid = "1234567890abcdef1234567890abcdef"
    Path(str(script) + ".meta").write_text(_meta(guid), encoding="utf-8")

    class Database:
        project_root = str(project)

        @staticmethod
        def get_all_guids():
            return [guid]

        @staticmethod
        def get_path_from_guid(value):
            return str(script) if value == guid else ""

    class Engine:
        @staticmethod
        def get_asset_database():
            return Database()

    monkeypatch.setattr(
        "Infernux.plugins.project_index._scan_guid_paths",
        lambda _root: pytest.fail("native GUID catalog fell back to os.walk"),
    )
    paths, native = project_guid_paths(str(project), engine=Engine())
    assert native is True
    assert paths == {guid: str(script.resolve())}
    manager = PluginManager(str(project), engine=Engine())
    manager.reload_all()
    assert [state.type_name for state in manager.preloads.states.values()] == [
        "Indexed"
    ]


def test_static_preload_discovery_imports_only_lifecycle_candidates(tmp_path):
    project = _project(tmp_path / "project")
    marker = project / "ordinary-imported.txt"
    (project / "Assets" / "ordinary.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('wrong')\n",
        encoding="utf-8",
    )
    lifecycle = project / "Assets" / "startup.py"
    lifecycle.write_text(
        "from abc import abstractmethod\n"
        "from pathlib import Path\n"
        "from Infernux.lifecycle import InxPreload as Lifecycle\n"
        "class Foundation(Lifecycle):\n"
        "    @abstractmethod\n"
        "    def configure(self): ...\n"
        "    def preload(self, context): ...\n"
        "class Startup(Foundation):\n"
        "    def configure(self): return None\n"
        "    def preload(self, context):\n"
        "        Path(context.project_root, 'preloaded.txt').write_text(context.script_guid)\n"
        "    def unload(self):\n"
        "        Path(__file__).with_name('unloaded.txt').write_text('yes')\n",
        encoding="utf-8",
    )
    manager = PluginManager.startup(str(project))
    states = manager.preloads.snapshots()
    assert len(states) == 1
    assert states[0]["type_name"] == "Startup"
    assert states[0]["loaded"] is True
    assert not marker.exists()
    first_guid = (project / "preloaded.txt").read_text(encoding="utf-8")
    manager.reload_all()
    assert (project / "Assets" / "unloaded.txt").is_file()
    assert (project / "preloaded.txt").read_text(encoding="utf-8") == first_guid


def test_reload_all_reuses_unchanged_ast_catalog_and_parses_only_changes(
    tmp_path, monkeypatch
):
    project = _project(tmp_path / "project")
    lifecycle = project / "Assets" / "startup.py"
    lifecycle.write_text(
        "from Infernux.lifecycle import InxPreload\n"
        "class Startup(InxPreload):\n"
        "    def preload(self, context): pass\n",
        encoding="utf-8",
    )
    ordinary = project / "Assets" / "gameplay.py"
    ordinary.write_text("VALUE = 1\n", encoding="utf-8")
    manager = PluginManager.startup(str(project))
    original = preload_module._read_declarations
    parsed: list[str] = []

    def record(path, project_root):
        parsed.append(path)
        return original(path, project_root)

    monkeypatch.setattr(preload_module, "_read_declarations", record)
    manager.reload_all()
    assert parsed == []

    ordinary.write_text("VALUE = 200\n", encoding="utf-8")
    manager.reload_all()
    assert parsed == [str(ordinary.resolve())]


def test_ordinary_script_change_does_not_reload_preloads_or_rescan_catalog(
    tmp_path, monkeypatch
):
    project = _project(tmp_path / "project")
    lifecycle = project / "Assets" / "startup.py"
    lifecycle.write_text(
        "from pathlib import Path\n"
        "from Infernux.lifecycle import InxPreload\n"
        "class Startup(InxPreload):\n"
        "    def preload(self, context): Path(context.project_root, 'loaded.txt').write_text('yes')\n"
        "    def unload(self): Path(__file__).with_name('unloaded.txt').write_text('yes')\n",
        encoding="utf-8",
    )
    ordinary = project / "Assets" / "gameplay.py"
    ordinary.write_text("VALUE = 1\n", encoding="utf-8")
    manager = PluginManager.startup(str(project))
    state = next(iter(manager.preloads.states.values()))
    original_instance = state.instance
    monkeypatch.setattr(
        manager.preloads,
        "_source_paths",
        lambda: pytest.fail("incremental reload performed a full source walk"),
    )

    ordinary.write_text("VALUE = 2\n", encoding="utf-8")
    manager._on_script_catalog_changed(str(ordinary), "modified")

    assert next(iter(manager.preloads.states.values())).instance is original_instance
    assert not (project / "Assets" / "unloaded.txt").exists()


def test_preload_change_reloads_only_its_dependency_slice(tmp_path, monkeypatch):
    project = _project(tmp_path / "project")

    def write_preload(path: Path, label: str, revision: int) -> None:
        path.write_text(
            "from pathlib import Path\n"
            "from Infernux.lifecycle import InxPreload\n"
            f"class {label}(InxPreload):\n"
            f"    revision = {revision}\n"
            "    def preload(self, context): pass\n"
            f"    def unload(self): Path(__file__).with_name('{label}.unloaded').write_text('yes')\n",
            encoding="utf-8",
        )

    first = project / "Assets" / "first.py"
    second = project / "Assets" / "second.py"
    write_preload(first, "First", 1)
    write_preload(second, "Second", 1)
    manager = PluginManager.startup(str(project))
    before = {
        state.type_name: state.instance for state in manager.preloads.states.values()
    }
    monkeypatch.setattr(
        manager.preloads,
        "_source_paths",
        lambda: pytest.fail("incremental reload performed a full source walk"),
    )

    write_preload(first, "First", 2)
    manager._on_script_catalog_changed(str(first), "modified")
    after = {
        state.type_name: state.instance for state in manager.preloads.states.values()
    }

    assert after["First"] is not before["First"]
    assert after["Second"] is before["Second"]
    assert (project / "Assets" / "First.unloaded").is_file()
    assert not (project / "Assets" / "Second.unloaded").exists()


def test_preload_cross_module_inheritance_move_disable_and_restart_diagnostic(tmp_path):
    source = _source(tmp_path / "source", "vendor/lifecycle")
    runtime = source / "Runtime"
    runtime.mkdir()
    (runtime / "foundation.py").write_text(
        "from abc import abstractmethod\n"
        "from Infernux.lifecycle import InxPreload\n"
        "class Foundation(InxPreload):\n"
        "    @abstractmethod\n"
        "    def configure(self): ...\n"
        "    def preload(self, context): ...\n",
        encoding="utf-8",
    )
    (runtime / "startup.py").write_text(
        "from pathlib import Path\n"
        "from foundation import Foundation\n"
        "class Startup(Foundation):\n"
        "    def configure(self): return None\n"
        "    def preload(self, context):\n"
        "        Path(context.project_root, 'package-loaded.txt').write_text(context.script_guid)\n"
        "        context.require_restart('native test state')\n"
        "    def unload(self):\n"
        "        return None\n",
        encoding="utf-8",
    )
    package = _export(source, tmp_path / "lifecycle.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    state = manager.install_package(str(package), install_dependencies=False)
    assert state.loaded is True
    assert state.restart_required is True
    assert state.lifecycle[0]["restart_reason"] == "native test state"
    original_identity = state.lifecycle[0]["identity"]

    old = project / "Packages/vendor/lifecycle/Runtime/startup.py"
    moved = project / "Packages/vendor/lifecycle/Runtime/boot.py"
    old.replace(moved)
    Path(str(old) + ".meta").replace(Path(str(moved) + ".meta"))
    state = manager.reload("vendor/lifecycle")
    assert state.lifecycle[0]["identity"] == original_identity

    disabled = manager.set_enabled("vendor/lifecycle", False)
    assert disabled.enabled is False
    assert disabled.loaded is False
    assert disabled.lifecycle == ()
    manager.set_enabled("vendor/lifecycle", True)
    assert (project / "package-loaded.txt").is_file()


def test_package_dependency_preload_and_reverse_unload_order(tmp_path):
    def lifecycle_source(reference: str, label: str) -> Path:
        source = _source(tmp_path / label, reference)
        runtime = source / "Runtime"
        runtime.mkdir()
        (runtime / "startup.py").write_text(
            "from pathlib import Path\n"
            "from Infernux.lifecycle import InxPreload\n"
            f"LABEL = {label!r}\n"
            "class Startup(InxPreload):\n"
            "    def preload(self, context):\n"
            "        path=Path(context.project_root, 'order.log')\n"
            "        with path.open('a', encoding='utf-8') as stream: stream.write('preload:' + LABEL + '\\n')\n"
            "    def unload(self):\n"
            "        path=Path(__file__).parents[4] / 'order.log'\n"
            "        with path.open('a', encoding='utf-8') as stream: stream.write('unload:' + LABEL + '\\n')\n",
            encoding="utf-8",
        )
        return source

    dependency = lifecycle_source("vendor/dependency", "dependency")
    parent = lifecycle_source("vendor/parent", "parent")
    (parent / "requirements.txt").write_text("vendor/dependency\n", encoding="utf-8")
    dependency_package = _export(dependency, tmp_path / "dependency.inxpkg")
    parent_package = _export(parent, tmp_path / "parent.inxpkg")
    project = _project(tmp_path / "project")
    registry = PluginRegistry(str(project))
    registry.add_package(
        "vendor/dependency",
        source={"type": "local", "location": str(dependency_package)},
    )
    manager = PluginManager(str(project))
    manager.install_package(str(parent_package))
    with pytest.raises(RuntimeError, match="required by enabled plugins"):
        manager.set_enabled("vendor/dependency", False)
    (project / "order.log").write_text("", encoding="utf-8")
    manager.reload_all()
    assert (project / "order.log").read_text(encoding="utf-8").splitlines() == [
        "unload:parent",
        "unload:dependency",
        "preload:dependency",
        "preload:parent",
    ]


def test_manifest_dependencies_are_installed_and_recorded(tmp_path):
    dependency = _source(tmp_path / "dependency", "vendor/manifest-child")
    (dependency / "Child.bin").write_bytes(b"child")
    dependency_package = _export(dependency, tmp_path / "manifest-child.inxpkg")
    parent = _source(tmp_path / "parent", "vendor/manifest-parent")
    manifest_path = parent / "InxPackage.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dependencies"] = ["vendor/manifest-child"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (parent / "Parent.bin").write_bytes(b"parent")
    parent_package = _export(parent, tmp_path / "manifest-parent.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.registry.add_package(
        "vendor/manifest-child",
        source={"type": "local", "location": str(dependency_package)},
    )

    manager.install_package(str(parent_package))

    assert {item["reference"] for item in manager.registry.installed()} == {
        "vendor/manifest-child",
        "vendor/manifest-parent",
    }
    installed = manager.registry.installed_record("vendor/manifest-parent")
    assert installed["dependencies"] == ["vendor/manifest-child"]
    with pytest.raises(RuntimeError, match="required by"):
        manager.uninstall("vendor/manifest-child")


def test_manifest_dependency_failure_rolls_back_new_dependencies(tmp_path, monkeypatch):
    dependency = _source(tmp_path / "dependency", "vendor/rollback-child")
    (dependency / "Child.bin").write_bytes(b"child")
    dependency_package = _export(dependency, tmp_path / "rollback-child.inxpkg")
    parent = _source(tmp_path / "parent", "vendor/rollback-parent")
    manifest_path = parent / "InxPackage.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dependencies"] = ["vendor/rollback-child"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    (parent / "Parent.bin").write_bytes(b"parent")
    parent_package = _export(parent, tmp_path / "rollback-parent.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.registry.add_package(
        "vendor/rollback-child",
        source={"type": "local", "location": str(dependency_package)},
    )
    original_plan = manager._plan_install

    def fail_parent(preview, selected):
        if preview.metadata["reference"] == "vendor/rollback-parent":
            raise RuntimeError("simulated parent planning failure")
        return original_plan(preview, selected)

    monkeypatch.setattr(manager, "_plan_install", fail_parent)

    with pytest.raises(RuntimeError, match="planning failure"):
        manager.install_package(str(parent_package))

    assert manager.registry.installed() == ()
    assert not (project / "Assets/Plugins/vendor/rollback-child/Child.bin").exists()


def test_runtime_plugin_panel_is_registered_and_removed_with_package(tmp_path):
    from Infernux.engine.interaction import PanelInteractionRegistry
    from Infernux.engine.ui.panel_registry import PanelRegistry

    class WindowManagerStub:
        def __init__(self):
            self.registered = []
            self.removed = []

        def register_window_type(self, **kwargs):
            self.registered.append(kwargs["type_id"])

        def unregister_window_type(self, type_id):
            self.removed.append(type_id)
            return True

    source = _source(tmp_path / "source", "vendor/live-panel")
    runtime = source / "Runtime"
    runtime.mkdir()
    (runtime / "startup.py").write_text(
        "from Infernux.engine.interaction import PanelInteractionDescriptor\n"
        "from Infernux.engine.ui.panel_registry import editor_panel\n"
        "from Infernux.lifecycle import InxPreload\n"
        "@editor_panel('Live Tool', type_id='vendor.live_tool', "
        "interaction=PanelInteractionDescriptor())\n"
        "class LiveTool: pass\n"
        "class Startup(InxPreload):\n"
        "    def preload(self, context): pass\n",
        encoding="utf-8",
    )
    package = _export(source, tmp_path / "live-panel.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    windows = WindowManagerStub()
    interactions = PanelInteractionRegistry()
    PanelRegistry.bind_live(windows, interactions)
    try:
        manager.install_package(str(package), install_dependencies=False)
        assert windows.registered == ["vendor.live_tool"]
        assert interactions.descriptor("vendor.live_tool") is not None

        manager.uninstall("vendor/live-panel")
        assert windows.removed == ["vendor.live_tool"]
        assert interactions.descriptor("vendor.live_tool") is None
    finally:
        owner = next(
            (
                item.owner
                for item in PanelRegistry.get_registrations()
                if item.type_id == "vendor.live_tool"
            ),
            "",
        )
        if owner:
            PanelRegistry.remove_owner(owner)
        PanelRegistry.unbind_live()


def test_requirements_choose_official_inxpackage_before_pip(tmp_path, monkeypatch):
    dependency = _source(tmp_path / "dependency", "vendor/dependency", version="2.1.0")
    (dependency / "Runtime").mkdir()
    (dependency / "Runtime" / "api.py").write_text("VALUE = 1\n", encoding="utf-8")
    dep_package = _export(dependency, tmp_path / "dependency.inxpkg")
    parent = _source(tmp_path / "parent", "vendor/parent")
    (parent / "requirements.txt").write_text(
        "vendor/dependency>=2\nthird-party-wheel==1.0\n", encoding="utf-8"
    )
    (parent / "Data.bin").write_bytes(b"parent")
    parent_package = _export(parent, tmp_path / "parent.inxpkg")
    project = _project(tmp_path / "project")
    registry = PluginRegistry(str(project))
    registry.add_package(
        "vendor/dependency",
        version="2.1.0",
        source={"type": "local", "location": str(dep_package)},
    )
    manager = PluginManager(str(project))
    calls = []
    monkeypatch.setattr(manager, "_project_python_executable", lambda: "project-python")
    monkeypatch.setattr(
        manager,
        "_run_process",
        lambda command, cwd=None: calls.append((command, cwd))
        or type(
            "Result",
            (),
            {"stdout": "[]" if command[2:5] == ["pip", "list", "--format=json"] else ""},
        )(),
    )
    manager.install_package(str(parent_package))
    assert {item["reference"] for item in manager.registry.installed()} == {
        "vendor/dependency",
        "vendor/parent",
    }
    install_calls = [
        command for command, _cwd in calls
        if command[2:4] == ["pip", "install"]
    ]
    assert len(install_calls) == 1
    assert install_calls[0][:4] == ["project-python", "-m", "pip", "install"]


def test_readme_license_pages_markdown_and_relative_images(tmp_path):
    source = _source(tmp_path / "source", "vendor/documented")
    (source / "README.md").write_text(
        "# Plugin\n\nIntro paragraph.\n\n## Gallery\n\n![Preview](Images/preview.png)\n",
        encoding="utf-8",
    )
    (source / "LICENSE").write_text("License text", encoding="utf-8")
    (source / "Images").mkdir()
    (source / "Images" / "preview.png").write_bytes(b"image")
    (source / "InxPluginPages").mkdir()
    (source / "InxPluginPages" / "Guide.md").write_text(
        "### Guide\n\nSteps", encoding="utf-8"
    )
    package = _export(source, tmp_path / "documented.inxpkg")
    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.install_package(str(package), install_dependencies=False)
    record = manager.registry.installed_record("vendor/documented")
    pages = manager.content_pages(record)
    assert [page["id"] for page in pages] == ["intro", "guide", "license"]
    blocks = parse_markdown_blocks(pages[0]["content"])
    assert [block["level"] for block in blocks if block["kind"] == "heading"] == [1, 2]
    image = next(block for block in split_markdown_images(pages[0]["content"]) if block["kind"] == "image")
    assert Path(manager.content_asset_path(record, pages[0], image["source"])).is_file()


def test_plugin_content_uses_one_strict_zh_cn_suffix_layout(tmp_path):
    source = _source(tmp_path / "source", "vendor/localized")
    (source / "README.md").write_text(
        "# Localized\n\nEnglish summary.\n", encoding="utf-8"
    )
    (source / "README.zh-CN.md").write_text(
        "# 本地化插件\n\n中文摘要。\n", encoding="utf-8"
    )
    (source / "README.zh.md").write_text(
        "# Unsupported\n\nMust not become a translation.\n", encoding="utf-8"
    )
    (source / "LICENSE").write_text("English license", encoding="utf-8")
    (source / "LICENSE.zh-CN.md").write_text("中文许可证", encoding="utf-8")
    pages_root = source / "InxPluginPages"
    pages_root.mkdir()
    (pages_root / "Guide.md").write_text(
        "# Guide\n\nEnglish guide.", encoding="utf-8"
    )
    (pages_root / "Guide.zh-CN.md").write_text(
        "# 指南\n\n中文指南。", encoding="utf-8"
    )

    package = _export(source, tmp_path / "localized.inxpkg")
    preview = InxPackage.inspect(str(package))
    page_keys = [
        (page["id"], page.get("locale", ""))
        for page in preview.metadata["pages"]
    ]
    assert page_keys == [
        ("intro", ""),
        ("intro", "zh-CN"),
        ("guide", ""),
        ("guide", "zh-CN"),
        ("license", ""),
        ("license", "zh-CN"),
    ]
    assert all(page["path"] != "README.zh.md" for page in preview.metadata["pages"])
    assert preview.metadata["intros"] == {"zh-CN": "中文摘要。"}
    assert localized_intro(preview.metadata, "zh") == "中文摘要。"
    assert localized_intro(preview.metadata, "en") == "English summary."

    project = _project(tmp_path / "project")
    manager = PluginManager(str(project))
    manager.install_package(str(package), install_dependencies=False)
    record = manager.registry.installed_record("vendor/localized")
    chinese = manager.content_pages(record, locale="zh")
    english = manager.content_pages(record, locale="en")
    assert [(page["id"], page.get("locale", "")) for page in chinese] == [
        ("intro", "zh-CN"),
        ("guide", "zh-CN"),
        ("license", "zh-CN"),
    ]
    assert [page["content"] for page in chinese] == [
        "# 本地化插件\n\n中文摘要。\n",
        "# 指南\n\n中文指南。",
        "中文许可证",
    ]
    assert [page.get("locale", "") for page in english] == ["", "", ""]


def test_player_policy_is_structural_and_custom_rules_are_rejected():
    assert player_file_exported({}, "Runtime/game.py") is True
    assert player_file_exported({}, "Editor/tool.py") is False
    assert player_file_exported({}, "README.md") is False
    assert player_file_exported({}, "Scenes/Demo.scene") is True
