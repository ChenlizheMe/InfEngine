from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from Infernux.engine.build import exporter_registry
from Infernux.plugins import InxPackage, PluginManager


def _module():
    script = (
        Path(__file__).parents[2]
        / "external"
        / "plugins"
        / "build_release_assets.py"
    )
    spec = importlib.util.spec_from_file_location("build_release_assets", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_android_package_installs_its_host_header_dependency(tmp_path, monkeypatch):
    root = Path(__file__).parents[2]
    package = tmp_path / "android.inxpkg"
    InxPackage.export_source(str(root / "external/plugins/infernux_android"), str(package))
    preview = InxPackage.inspect(str(package))
    manager = PluginManager(str(tmp_path / "project"), runtime=False)
    installed_lines = []
    monkeypatch.setattr(manager, "_install_pip_lines", lambda lines: installed_lines.extend(lines))
    manager._install_requirements(preview)
    assert [line.strip() for line in installed_lines if line.strip()] == ["pybind11==3.1.0"]


def test_script_uses_the_checked_out_protocol_from_any_working_directory(
    tmp_path,
):
    script = (
        Path(__file__).parents[2]
        / "external"
        / "plugins"
        / "build_release_assets.py"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr


class _Preview:
    def __init__(self, metadata):
        self.metadata = metadata


def _write_source(root: Path, name: str, reference: str, repository: str) -> dict:
    plugin = root / name
    plugin.mkdir(parents=True)
    manifest = {
        "reference": reference,
        "name": name,
        "version": "0.1.0",
        "intro": "test",
        "intros": {},
        "engine": ">=0.4,<0.5",
    }
    (plugin / "package").mkdir()
    (plugin / "package" / "inx_package.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return {
        "path": name,
        "repository": repository,
        "revision": "040/multiplatform_build",
        "subdirectory": f"external/plugins/{name}",
        "category": "Platform",
        "targets": [reference.rsplit("/", 1)[-1]],
        "default": False,
    }


def test_stages_only_repository_owned_packages_without_hashes(tmp_path, monkeypatch):
    module = _module()
    source = tmp_path / "plugins"
    source.mkdir()
    main_repository = "https://github.com/ChenlizheMe/Infernux"
    entries = [
        _write_source(source, "windows", "infernux/platform-windows", main_repository),
        _write_source(source, "web", "infernux/platform-web", main_repository),
        _write_source(
            source,
            "mcp",
            "infernux/mcp",
            "https://github.com/ChenlizheMe/infernux_mcp",
        ),
    ]
    catalog = source / "plugins.json"
    catalog.write_text(
        json.dumps(
            {"$schema": "infernux.official_plugin_sources", "plugins": entries}
        ),
        encoding="utf-8",
    )
    packages = tmp_path / "packages"
    packages.mkdir()
    metadata = {}
    for reference in ("infernux/platform-windows", "infernux/platform-web"):
        name = reference.replace("/", ".") + ".inxpkg"
        path = packages / name
        path.write_bytes(reference.encode("utf-8"))
        metadata[str(path)] = {
            "reference": reference,
            "version": "0.1.0",
            "engine": ">=0.4,<0.5",
        }
    monkeypatch.setattr(
        module.InxPackage,
        "inspect",
        lambda path: _Preview(metadata[path]),
    )

    output = tmp_path / "release"
    outputs = module.build(
        packages,
        output,
        catalog,
        repository=main_repository,
        release_tag="v0.4.0",
    )

    assert {path.name for path in outputs} == {
        "infernux.platform-windows.inxpkg",
        "infernux.platform-windows.release.json",
        "infernux.platform-web.inxpkg",
        "infernux.platform-web.release.json",
    }
    document = json.loads(
        (output / "infernux.platform-web.release.json").read_text(encoding="utf-8")
    )
    assert document == {
        "$schema": "infernux.plugin_release",
        "reference": "infernux/platform-web",
        "version": "0.1.0",
        "engine": ">=0.4,<0.5",
        "artifact": {"name": "infernux.platform-web.inxpkg"},
        "generator": "Infernux official plugin release assets",
        "release_tag": "v0.4.0",
    }
    assert "sha" not in json.dumps(document).casefold()
    assert not (output / "infernux.mcp.inxpkg").exists()


def test_official_catalog_preserves_explicit_source_revision(tmp_path, monkeypatch):
    module_path = (
        Path(__file__).parents[2] / "external/plugins/build_official_plugins.py"
    )
    spec = importlib.util.spec_from_file_location("build_official_plugins", module_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = tmp_path / "plugins"
    source.mkdir()
    entry = _write_source(
        source,
        "linux",
        "infernux/platform-linux",
        "https://github.com/ChenlizheMe/Infernux",
    )
    catalog = source / "plugins.json"
    catalog.write_text(
        json.dumps({"$schema": "infernux.official_plugin_sources", "plugins": [entry]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        module.InxPackage,
        "export_source",
        lambda *_args, **_kwargs: _Preview(
            {
                "reference": "infernux/platform-linux",
                "name": "Linux",
                    "version": "0.1.0",
                    "engine": ">=0.4,<0.5",
                    "pages": [],
            }
        ),
    )

    module.build(source, tmp_path / "output", catalog)

    registry = json.loads(
        (tmp_path / "output/official-registry.json").read_text(encoding="utf-8")
    )
    assert registry["packages"][0]["source"] == {
        "type": "github",
        "location": "https://github.com/ChenlizheMe/Infernux",
        "subdirectory": "external/plugins/linux",
        "revision": "040/multiplatform_build",
    }


def test_rejects_package_metadata_that_disagrees_with_source(tmp_path, monkeypatch):
    module = _module()
    source = tmp_path / "plugins"
    source.mkdir()
    repository = "https://github.com/ChenlizheMe/Infernux"
    entry = _write_source(source, "web", "infernux/platform-web", repository)
    catalog = source / "plugins.json"
    catalog.write_text(
        json.dumps(
            {"$schema": "infernux.official_plugin_sources", "plugins": [entry]}
        ),
        encoding="utf-8",
    )
    packages = tmp_path / "packages"
    packages.mkdir()
    package = packages / "infernux.platform-web.inxpkg"
    package.write_bytes(b"package")
    monkeypatch.setattr(
        module.InxPackage,
        "inspect",
        lambda _path: _Preview(
            {
                "reference": "infernux/platform-web",
                "version": "9.9.9",
                "engine": ">=0.4,<0.5",
            }
        ),
    )

    with pytest.raises(RuntimeError, match="version mismatch"):
        module.build(
            packages,
            tmp_path / "release",
            catalog,
            repository=repository,
            release_tag="v0.4.0",
        )


@pytest.mark.parametrize("tag", ["", "release/0.4.0", "../v0.4.0", "v0.4.0 beta"])
def test_rejects_unsafe_release_tags(tmp_path, tag):
    module = _module()
    with pytest.raises(ValueError, match="Invalid release tag"):
        module.build(
            tmp_path,
            tmp_path / "release",
            tmp_path / "plugins.json",
            repository="https://github.com/ChenlizheMe/Infernux",
            release_tag=tag,
        )


def test_release_workflow_uses_the_versioned_assets_without_hash_manifest():
    workflow = (
        Path(__file__).parents[2]
        / ".github"
        / "workflows"
        / "platform-plugin-release.yml"
    ).read_text(encoding="utf-8")
    assert "release:\n    types: [published]" in workflow
    assert "infernux-*-cp313-cp313-win_amd64.whl" in workflow
    assert "build_release_assets.py" in workflow
    assert "$env:RELEASE_INPUT_TAG" in workflow
    assert "${{ inputs.release_tag }}'" not in workflow


@pytest.mark.parametrize(
    ("source_directory", "reference", "target_ids"),
    [
        pytest.param(
            "infernux_windows",
            "infernux/platform-windows",
            ("windows-x64",),
            marks=pytest.mark.skipif(
                sys.platform != "win32", reason="Windows exporter requires Windows"
            ),
        ),
        pytest.param(
            "infernux_linux",
            "infernux/platform-linux",
            ("linux-x64",),
            marks=pytest.mark.skipif(
                not sys.platform.startswith("linux"),
                reason="Linux exporter requires Linux",
            ),
        ),
        (
            "infernux_android",
            "infernux/platform-android",
            ("android-arm64", "android-x64-emulator"),
        ),
        (
            "infernux_web",
            "infernux/platform-web",
            ("web-wasm32",),
        ),
    ],
)
def test_platform_package_registers_and_removes_its_build_targets(
    tmp_path,
    monkeypatch,
    source_directory,
    reference,
    target_ids,
):
    root = Path(__file__).parents[2]
    source = root / "external" / "plugins" / source_directory
    package = tmp_path / f"{reference.replace('/', '.')}.inxpkg"
    project = tmp_path / "project"
    (project / "Assets").mkdir(parents=True)
    (project / "ProjectSettings").mkdir()
    monkeypatch.setenv(
        "INFERNUX_PACKAGE_CACHE_ROOT",
        str(tmp_path / "hub-package-cache"),
    )
    if reference == "infernux/platform-android":
        # This test exercises package lifecycle registration, while the Hub
        # support gate and its filesystem contract have dedicated coverage.
        # Declare that prerequisite explicitly instead of bypassing the
        # production transaction guard.
        monkeypatch.setattr(
            "Infernux.plugins.platform_support.android_support_available",
            lambda _environ=None: True,
        )
        monkeypatch.setattr(
            "Infernux.plugins.platform_support.android_support_environment",
            lambda _environ=None: {},
        )

    InxPackage.export_source(str(source), str(package), profile="release")
    exporter_registry.clear()
    manager = PluginManager(str(project), runtime=False)
    try:
        for target_id in target_ids:
            with pytest.raises(KeyError, match="Unknown build target"):
                exporter_registry.resolve(target_id)

        state = manager.install_package(
            str(package),
            install_dependencies=False,
        )
        assert state.reference == reference
        assert state.loaded is True
        assert state.error == ""
        for target_id in target_ids:
            exporter, target = exporter_registry.resolve(target_id)
            assert exporter.exporter_id == reference
            assert target.id == target_id

        manager.uninstall(reference)
        for target_id in target_ids:
            with pytest.raises(KeyError, match="Unknown build target"):
                exporter_registry.resolve(target_id)
    finally:
        manager.shutdown()
        exporter_registry.clear()
