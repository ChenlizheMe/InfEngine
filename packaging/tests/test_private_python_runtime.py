from __future__ import annotations

import hashlib
import io
import os
import sys
import subprocess
import tarfile
import zipfile
from pathlib import Path

import pytest


PACKAGING_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGING_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGING_ROOT))

import embed_runtime_manager
import stage_bundled_python_runtime
from private_python_runtime import (
    extract_runtime_archive,
    is_private_runtime_root,
    prune_runtime_staging_cache,
    runtime_archive_for_machine,
    verify_runtime_archive,
)


def _write_runtime_archive(path: Path) -> None:
    payload = b"private python"
    info = tarfile.TarInfo("python/python.exe")
    info.size = len(payload)
    with tarfile.open(path, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))


def _archive_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("module", [embed_runtime_manager, stage_bundled_python_runtime])
@pytest.mark.parametrize("nested", [False, True])
def test_runtime_discovery_only_uses_supported_root_layouts(
    tmp_path, monkeypatch, module, nested
):
    relative = Path("python.exe") if sys.platform == "win32" else Path("bin/python")
    executable = tmp_path / ("unrelated/tool" if nested else "") / relative
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_bytes(b"test interpreter")
    monkeypatch.setattr(
        stage_bundled_python_runtime,
        "_is_target_python",
        lambda path: Path(path).is_file(),
    )

    assert module._find_python_in_root(str(tmp_path)) == (
        None if nested else str(executable)
    )


def test_runtime_and_bootstrap_staging_share_the_hub_data_root(tmp_path, monkeypatch):
    root = tmp_path / "HubData"
    monkeypatch.setattr(
        embed_runtime_manager,
        "get_hub_shared_data_dir",
        lambda: str(root),
    )
    monkeypatch.setattr(
        stage_bundled_python_runtime,
        "get_hub_shared_data_dir",
        lambda: str(root),
    )

    assert embed_runtime_manager._default_runtime_dir() == str(root / "Runtimes")
    assert stage_bundled_python_runtime._bootstrap_root() == str(
        root / "Downloads" / "RuntimeBootstrap"
    )


def test_windows_runtime_uses_pinned_relocatable_archive() -> None:
    artifact = runtime_archive_for_machine(system="win32", machine="AMD64")

    assert artifact.name == (
        "cpython-3.13.15+20260825-x86_64-pc-windows-msvc-"
        "install_only.tar.gz"
    )
    assert artifact.url.startswith(
        "https://github.com/astral-sh/python-build-standalone/releases/download/"
    )
    assert artifact.url.endswith(artifact.name.replace("+", "%2B"))
    assert not artifact.name.endswith((".exe", ".msi", ".pkg"))
    assert artifact.sha256 == "82a792c25550a421b29f381eaeafa6dccd1ffcbd97a1b1507b202f5df877cecf"


def test_python_312_runtime_remains_addressable() -> None:
    artifact = runtime_archive_for_machine(
        system="win32", machine="AMD64", runtime="3.12"
    )

    assert artifact.name.startswith("cpython-3.12.13+20260805-")
    assert artifact.sha256 == "d731ce7dddcfad4a9521aac48626ca06326003fe4771a366e0fce6eb58709451"


@pytest.mark.parametrize(
    ("platform_name", "header_path", "library_directory", "library_name"),
    (
        ("win32", Path("include/Python.h"), "libs", "python313.lib"),
        (
            "linux",
            Path("include/python3.13/Python.h"),
            "lib",
            "libpython3.13.so.1.0",
        ),
        (
            "darwin",
            Path("include/python3.13/Python.h"),
            "lib",
            "libpython3.13.dylib",
        ),
    ),
)
def test_staged_runtime_dev_support_uses_the_target_platform_layout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    platform_name: str,
    header_path: Path,
    library_directory: str,
    library_name: str,
) -> None:
    monkeypatch.setattr(stage_bundled_python_runtime.sys, "platform", platform_name)
    (tmp_path / header_path).parent.mkdir(parents=True)
    (tmp_path / header_path).write_bytes(b"")
    (tmp_path / library_directory).mkdir()
    (tmp_path / library_directory / library_name).write_bytes(b"")

    assert stage_bundled_python_runtime._has_dev_support(str(tmp_path))
    assert embed_runtime_manager._has_build_support(str(tmp_path), "3.13")


@pytest.mark.parametrize("platform_name", ["win32", "linux", "darwin"])
def test_managed_runtime_preparation_uses_install_prefix(tmp_path, monkeypatch, platform_name):
    monkeypatch.setattr(embed_runtime_manager.sys, "platform", platform_name)
    manager = embed_runtime_manager.PythonRuntimeManager(runtime_dir=str(tmp_path))
    python = manager.private_runtime_python("3.13")
    observed = []
    monkeypatch.setattr(manager, "_ensure_runtime_build_support", lambda root, *a, **kw: observed.append(root))
    monkeypatch.setattr(manager, "_ensure_pip", lambda *a, **kw: None)
    monkeypatch.setattr(manager, "_ensure_runtime_packages", lambda *a, **kw: None)
    manager._prepare_managed_runtime(python, "3.13")
    assert observed == [manager.private_runtime_root("3.13")]
    packages = Path(embed_runtime_manager._site_packages_root(observed[0], "3.13"))
    relative = "Lib/site-packages" if platform_name == "win32" else "lib/python3.13/site-packages"
    assert packages == Path(observed[0]) / relative
    monkeypatch.setattr(manager, "get_runtime_path", lambda runtime: python)
    monkeypatch.setattr(embed_runtime_manager, "is_frozen", lambda: True)
    monkeypatch.setattr(embed_runtime_manager, "_has_build_support", lambda root, runtime: root == observed[0])
    monkeypatch.setattr(manager, "_has_modules", lambda *a: True)
    assert manager.ensure_runtime(version="3.13") == python


def test_broken_matching_bundle_does_not_fall_back_to_network(tmp_path, monkeypatch):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    with zipfile.ZipFile(bundle_dir / "runtime_bundle.zip", "w") as bundle:
        bundle.writestr("python313/broken.txt", "broken runtime")
    manager = embed_runtime_manager.PythonRuntimeManager(runtime_dir=str(tmp_path / "managed"))
    monkeypatch.setattr(manager, "bundled_runtime_dirs", lambda: [str(bundle_dir)])
    downloads = []
    monkeypatch.setattr(manager, "_extract_runtime_to_root", lambda *a, **kw: downloads.append(True))
    with pytest.raises(embed_runtime_manager.PythonRuntimeError, match="bundled Python 3.13"):
        manager._provision_managed_runtime("3.13")
    assert downloads == []


@pytest.mark.parametrize("success", [True, False])
def test_missing_pip_uses_bundled_ensurepip_without_network(tmp_path, monkeypatch, success):
    manager = embed_runtime_manager.PythonRuntimeManager(runtime_dir=str(tmp_path))
    commands = []
    def run(args, **kwargs):
        commands.append(args)
        return subprocess.CompletedProcess(args, 0 if success and args[2] == "ensurepip" else 1,
                                           stdout="", stderr="bootstrap failed")
    monkeypatch.setattr(embed_runtime_manager, "_run_command", run)
    monkeypatch.setattr(embed_runtime_manager, "_download_file", lambda *a, **kw: pytest.fail("pip bootstrap must be offline"))
    if success:
        manager._ensure_pip("python")
    else:
        with pytest.raises(embed_runtime_manager.PythonRuntimeError, match="bootstrap failed"):
            manager._ensure_pip("python")
    assert commands == [["python", "-m", "pip", "--version"], ["python", "-m", "ensurepip", "--upgrade"]]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable permissions")
def test_runtime_zip_preserves_executable_permissions(tmp_path, monkeypatch):
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    with zipfile.ZipFile(bundle_dir / "runtime_bundle.zip", "w") as bundle:
        info = zipfile.ZipInfo("python313/bin/python")
        info.create_system = 3
        info.external_attr = 0o100755 << 16
        bundle.writestr(info, "#!/bin/sh\nprintf '3.13\\n'\n")
    manager = embed_runtime_manager.PythonRuntimeManager(runtime_dir=str(tmp_path / "managed"))
    monkeypatch.setattr(manager, "bundled_runtime_dirs", lambda: [str(bundle_dir)])
    monkeypatch.setattr(embed_runtime_manager, "is_current_private_runtime_root", lambda root, **kw: Path(root).exists())
    python = manager._seed_runtime_from_bundle(version="3.13")
    assert python == manager.private_runtime_python("3.13")
    assert os.stat(python).st_mode & 0o111 == 0o111


def test_runtime_archive_is_extracted_into_an_owned_private_root(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.gz"
    destination = tmp_path / "hub-runtime" / "python313"
    _write_runtime_archive(archive)

    extract_runtime_archive(
        archive, destination, expected_sha256=_archive_sha256(archive)
    )

    assert (destination / "python.exe").read_bytes() == b"private python"
    assert is_private_runtime_root(destination)


def test_runtime_archive_checksum_is_verified(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.gz"
    _write_runtime_archive(archive)

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        verify_runtime_archive(archive, "0" * 64)


def test_extracted_runtime_marker_records_the_source_archive(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.gz"
    destination = tmp_path / "python313"
    _write_runtime_archive(archive)

    archive_sha256 = _archive_sha256(archive)
    extract_runtime_archive(
        archive, destination, expected_sha256=archive_sha256
    )

    assert is_private_runtime_root(destination)
    marker = (destination / ".infernux-private-python-runtime.json").read_text(
        encoding="utf-8"
    )
    assert archive_sha256 in marker


def test_runtime_archive_rejects_paths_outside_destination(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.tar.gz"
    payload = b"do not write"
    info = tarfile.TarInfo("../outside.txt")
    info.size = len(payload)
    with tarfile.open(archive_path, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))

    with pytest.raises(RuntimeError, match="Invalid private Python runtime archive"):
        extract_runtime_archive(
            archive_path,
            tmp_path / "python313",
            expected_sha256=_archive_sha256(archive_path),
        )

    assert not (tmp_path / "outside.txt").exists()


def test_stale_packaging_runtime_is_removed_from_current_installer(tmp_path: Path) -> None:
    legacy_installer = tmp_path / "python-3.12.8-amd64.exe"
    legacy_installer.write_bytes(b"installer")
    legacy_archive = (
        tmp_path
        / "cpython-3.12.13+20260805-x86_64-pc-windows-msvc-install_only.tar.gz"
    )
    legacy_archive.write_bytes(b"old archive")
    current_archive = tmp_path / runtime_archive_for_machine().name
    current_archive.write_bytes(b"current archive")
    runtime_python = tmp_path / "python312" / "python.exe"
    runtime_python.parent.mkdir()
    runtime_python.write_bytes(b"runtime")

    prune_runtime_staging_cache(tmp_path)

    assert not legacy_installer.exists()
    assert not legacy_archive.exists()
    assert current_archive.read_bytes() == b"current archive"
    assert not runtime_python.parent.exists()


def test_runtime_manager_refuses_non_hub_python_destination(tmp_path: Path) -> None:
    manager = embed_runtime_manager.PythonRuntimeManager(
        runtime_dir=str(tmp_path / "hub-runtime")
    )

    with pytest.raises(
        embed_runtime_manager.PythonRuntimeError,
        match="Hub-owned python313 directory",
    ):
        manager._extract_runtime_to_root(str(tmp_path / "user-python"))


def test_runtime_reinstall_forces_verified_bundle_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = embed_runtime_manager.PythonRuntimeManager(
        runtime_dir=str(tmp_path / "hub-runtime")
    )
    expected_python = str(tmp_path / "hub-runtime" / "python313" / "python.exe")
    observed: dict[str, object] = {}

    def seed_runtime(**kwargs):
        observed.update(kwargs)
        return expected_python

    monkeypatch.setattr(manager, "_seed_runtime_from_bundle", seed_runtime)
    monkeypatch.setattr(
        manager,
        "_prepare_managed_runtime",
        lambda python_exe, *_args, **_kwargs: observed.update(prepared=python_exe),
    )

    assert manager.reinstall_runtime() == expected_python
    assert observed["overwrite"] is True
    assert observed["prepared"] == expected_python


def test_runtime_manager_contains_no_python_installer_execution_path() -> None:
    forbidden = (
        "InstallAllUsers=",
        "TargetDir=",
        "Include_launcher=",
        "InstallLauncherAllUsers=",
        "python.org/ftp/python",
        '"installer", "-pkg"',
    )
    sources = [
        path
        for path in PACKAGING_ROOT.rglob("*.py")
        if "tests" not in path.parts and "runtime" not in path.parts
    ]

    for source_path in sources:
        source = source_path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in source, (
                f"{source_path.relative_to(PACKAGING_ROOT)} reintroduced a system "
                f"Python installer path: {token}"
            )


def test_runtime_extraction_does_not_touch_external_python(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime_dir = tmp_path / "hub-owned-runtime"
    private_root = runtime_dir / "python313"
    archive = tmp_path / "private-runtime.tar.gz"
    _write_runtime_archive(archive)

    external_python = tmp_path / "user-python" / "python.exe"
    external_python.parent.mkdir()
    external_python.write_bytes(b"user installation")

    manager = embed_runtime_manager.PythonRuntimeManager(runtime_dir=str(runtime_dir))
    monkeypatch.setattr(
        manager, "_ensure_runtime_archive", lambda *_args, **_kwargs: str(archive)
    )
    monkeypatch.setattr(
        embed_runtime_manager,
        "extract_runtime_archive",
        lambda source, destination, **kwargs: extract_runtime_archive(
            source,
            destination,
            expected_sha256=_archive_sha256(Path(source)),
            runtime=kwargs["runtime"],
        ),
    )
    monkeypatch.setattr(
        embed_runtime_manager,
        "_is_python_version",
        lambda path, _version: Path(path) == private_root / "python.exe",
    )
    monkeypatch.setattr(embed_runtime_manager, "_is_embedded_root", lambda _path: False)

    installed = manager._extract_runtime_to_root(str(private_root))

    assert Path(installed) == private_root / "python.exe"
    assert Path(installed).read_bytes() == b"private python"
    assert external_python.read_bytes() == b"user installation"


def test_runtime_manager_keeps_minor_versions_in_sibling_directories(
    tmp_path: Path,
) -> None:
    manager = embed_runtime_manager.PythonRuntimeManager(
        runtime_dir=str(tmp_path / "hub-runtime")
    )

    assert Path(manager.private_runtime_root("3.12")).name == "python312"
    assert Path(manager.private_runtime_root("3.13")).name == "python313"
    assert manager.default_version == "3.13"


def test_project_runtime_requires_explicitly_installed_target_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = embed_runtime_manager.PythonRuntimeManager(
        runtime_dir=str(tmp_path / "hub-runtime")
    )
    provisioned = []
    monkeypatch.setattr(
        manager,
        "_provision_managed_runtime",
        lambda *_args, **_kwargs: provisioned.append(True),
    )

    with pytest.raises(
        embed_runtime_manager.PythonRuntimeError,
        match="Python 3.12 is not installed",
    ):
        manager.create_project_runtime(
            str(tmp_path / "project" / ".runtime" / "python312"),
            version="3.12",
        )

    assert provisioned == []


def test_runtime_bundle_only_extracts_the_requested_python_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle_dir = tmp_path / "bundle"
    bundle_dir.mkdir()
    runtime_bundle = bundle_dir / "runtime_bundle.zip"
    with zipfile.ZipFile(runtime_bundle, "w") as bundle:
        bundle.writestr("python313/python.exe", b"python 313")

    managed_root = tmp_path / "managed"
    existing_python313 = managed_root / "python313" / "keep.txt"
    existing_python313.parent.mkdir(parents=True)
    existing_python313.write_text("untouched", encoding="utf-8")
    manager = embed_runtime_manager.PythonRuntimeManager(
        runtime_dir=str(managed_root),
        bundle_runtime_dir=str(bundle_dir),
    )
    monkeypatch.setattr(
        manager,
        "bundled_runtime_dirs",
        lambda: [],
    )

    assert manager._seed_runtime_from_bundle(version="3.12") is None
    assert existing_python313.read_text(encoding="utf-8") == "untouched"
    assert not (managed_root / "python312").exists()
