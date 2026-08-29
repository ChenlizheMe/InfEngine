from __future__ import annotations

import io
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest


PACKAGING_ROOT = Path(__file__).resolve().parents[1]
if str(PACKAGING_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGING_ROOT))

import embed_runtime_manager
from private_python_runtime import (
    extract_runtime_archive,
    is_current_private_runtime_root,
    is_private_runtime_root,
    remove_legacy_installer_artifacts,
    runtime_archive_for_machine,
    verify_runtime_archive,
)


def _write_runtime_archive(path: Path) -> None:
    payload = b"private python"
    info = tarfile.TarInfo("python/python.exe")
    info.size = len(payload)
    with tarfile.open(path, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))


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


def test_legacy_python_312_runtime_remains_addressable() -> None:
    artifact = runtime_archive_for_machine(
        system="win32", machine="AMD64", runtime="3.12"
    )

    assert artifact.name.startswith("cpython-3.12.13+20260805-")
    assert artifact.sha256 == "d731ce7dddcfad4a9521aac48626ca06326003fe4771a366e0fce6eb58709451"


def test_runtime_archive_is_extracted_into_an_owned_private_root(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.gz"
    destination = tmp_path / "hub-runtime" / "python313"
    _write_runtime_archive(archive)

    extract_runtime_archive(archive, destination)

    assert (destination / "python.exe").read_bytes() == b"private python"
    assert is_private_runtime_root(destination)


def test_runtime_archive_checksum_is_verified(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.gz"
    _write_runtime_archive(archive)

    with pytest.raises(RuntimeError, match="checksum mismatch"):
        verify_runtime_archive(archive, "0" * 64)


def test_unpinned_marker_is_not_current(tmp_path: Path) -> None:
    archive = tmp_path / "runtime.tar.gz"
    destination = tmp_path / "python313"
    _write_runtime_archive(archive)

    extract_runtime_archive(archive, destination)

    assert is_private_runtime_root(destination)
    assert not is_current_private_runtime_root(destination)


def test_runtime_archive_rejects_paths_outside_destination(tmp_path: Path) -> None:
    archive_path = tmp_path / "unsafe.tar.gz"
    payload = b"do not write"
    info = tarfile.TarInfo("../outside.txt")
    info.size = len(payload)
    with tarfile.open(archive_path, mode="w:gz") as archive:
        archive.addfile(info, io.BytesIO(payload))

    with pytest.raises(RuntimeError, match="Invalid private Python runtime archive"):
        extract_runtime_archive(archive_path, tmp_path / "python313")

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

    remove_legacy_installer_artifacts(tmp_path)

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
        lambda source, destination, **_kwargs: extract_runtime_archive(
            source, destination
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
