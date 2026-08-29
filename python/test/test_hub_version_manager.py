"""Hub VersionManager — issue #43 regression tests.

Covers: atomic cancellable downloads, corrupted-wheel healing, unique temp
files under concurrency, and version listing that ignores broken installs.

These tests are pure-Python (no Qt, no network): GitHub access and the HTTP
stream are monkeypatched.
"""
from __future__ import annotations

import io
import os
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "packaging",
))

import version_manager as vm_mod
from version_manager import VersionManager, DownloadCancelled


def _make_wheel_bytes() -> bytes:
    """Minimal valid wheel = a zip with one entry."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("infernux/__init__.py", "")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, payload: bytes, chunk: int = 7):
        self._data = io.BytesIO(payload)
        self.headers = {"Content-Length": str(len(payload))}
        self._chunk = chunk

    def read(self, n):
        return self._data.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class _RuntimeInventory:
    def __init__(self, *versions: str):
        self._versions = list(versions)

    def installed_versions(self):
        return list(self._versions)

    def has_runtime(self, version):
        return str(version) in self._versions


@pytest.fixture()
def vm(tmp_path, monkeypatch):
    monkeypatch.setattr(vm_mod, "_VERSIONS_DIR", tmp_path / "versions")
    manager = VersionManager(_RuntimeInventory("3.12"))
    release = {
        "tag_name": "v9.9.9",
        "prerelease": False,
        "published_at": "2026-01-01T00:00:00Z",
        "assets": [{
            "name": "infernux-9.9.9-cp312-cp312-win_amd64.whl",
            "browser_download_url": "https://example.invalid/infernux-9.9.9.whl",
            "size": 128,
        }],
    }
    monkeypatch.setattr(manager, "_fetch_releases", lambda: [release])
    return manager


class TestDownload:
    def test_successful_download_is_valid_wheel(self, vm, monkeypatch):
        payload = _make_wheel_bytes()
        monkeypatch.setattr(vm_mod.urllib.request, "urlopen",
                            lambda req: _FakeResponse(payload))
        path = vm.download_version("9.9.9")
        assert os.path.isfile(path)
        assert zipfile.is_zipfile(path)
        assert vm.is_installed("9.9.9")

    def test_cancel_leaves_no_residue(self, vm, monkeypatch):
        # Payload spans many 64 KB chunks so the per-chunk cancel check
        # actually triggers mid-transfer.
        payload = _make_wheel_bytes() + b"\0" * (64 * 1024 * 6)
        monkeypatch.setattr(vm_mod.urllib.request, "urlopen",
                            lambda req: _FakeResponse(payload))
        calls = {"n": 0}

        def cancel_after_two_chunks():
            calls["n"] += 1
            return calls["n"] > 2

        with pytest.raises(DownloadCancelled):
            vm.download_version("9.9.9", should_cancel=cancel_after_two_chunks)

        ver_dir = vm_mod._VERSIONS_DIR / "9.9.9"
        # No partial wheel, no temp files, and ideally no empty dir at all.
        if ver_dir.exists():
            assert list(ver_dir.iterdir()) == []
        assert not vm.is_installed("9.9.9")
        assert "9.9.9" not in vm.installed_versions()

    def test_cancel_then_reinstall_succeeds(self, vm, monkeypatch):
        cancel_payload = _make_wheel_bytes() + b"\0" * (64 * 1024 * 6)
        monkeypatch.setattr(vm_mod.urllib.request, "urlopen",
                            lambda req: _FakeResponse(cancel_payload))
        flag = {"n": 0}

        def cancel_once():
            flag["n"] += 1
            return flag["n"] > 1

        with pytest.raises(DownloadCancelled):
            vm.download_version("9.9.9", should_cancel=cancel_once)

        # Second attempt with no cancellation must produce a valid install.
        good_payload = _make_wheel_bytes()
        monkeypatch.setattr(vm_mod.urllib.request, "urlopen",
                            lambda req: _FakeResponse(good_payload))
        path = vm.download_version("9.9.9")
        assert zipfile.is_zipfile(path)
        assert vm.is_installed("9.9.9")

    def test_truncated_transfer_rejected(self, vm, monkeypatch):
        monkeypatch.setattr(vm_mod.urllib.request, "urlopen",
                            lambda req: _FakeResponse(b"not-a-zip"))
        with pytest.raises(ValueError, match="not a valid wheel"):
            vm.download_version("9.9.9")
        assert not vm.is_installed("9.9.9")

    def test_existing_corrupted_wheel_is_replaced(self, vm, monkeypatch):
        ver_dir = vm_mod._VERSIONS_DIR / "9.9.9"
        ver_dir.mkdir(parents=True)
        bad = ver_dir / "infernux-9.9.9-cp312-cp312-win_amd64.whl"
        bad.write_bytes(b"garbage from an interrupted install")

        payload = _make_wheel_bytes()
        monkeypatch.setattr(vm_mod.urllib.request, "urlopen",
                            lambda req: _FakeResponse(payload))
        path = vm.download_version("9.9.9")
        assert zipfile.is_zipfile(path)


class TestListingHealsCorruption:
    def test_corrupted_wheel_not_listed_as_installed(self, vm):
        ver_dir = vm_mod._VERSIONS_DIR / "1.2.3"
        ver_dir.mkdir(parents=True)
        (ver_dir / "infernux-1.2.3-cp312-cp312-win_amd64.whl").write_bytes(b"junk")

        assert vm.get_wheel_path("1.2.3") is None
        assert "1.2.3" not in vm.installed_versions()
        # healing removed the junk file
        assert not list(ver_dir.glob("*.whl"))

    def test_valid_wheel_listed(self, vm):
        ver_dir = vm_mod._VERSIONS_DIR / "2.0.0"
        ver_dir.mkdir(parents=True)
        (ver_dir / "infernux-2.0.0-cp312-cp312-win_amd64.whl").write_bytes(_make_wheel_bytes())

        assert vm.get_wheel_path("2.0.0") is not None
        assert "2.0.0" in vm.installed_versions()

    def test_remove_version(self, vm):
        ver_dir = vm_mod._VERSIONS_DIR / "2.0.0"
        ver_dir.mkdir(parents=True)
        (ver_dir / "infernux-2.0.0-cp312-cp312-win_amd64.whl").write_bytes(_make_wheel_bytes())
        assert vm.remove_version("2.0.0") is True
        assert not ver_dir.exists()
        assert vm.remove_version("2.0.0") is False


def test_local_engine_install_requires_its_exact_python_runtime(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(vm_mod, "_VERSIONS_DIR", tmp_path / "versions")
    manager = VersionManager(_RuntimeInventory("3.12"))
    wheel = tmp_path / "infernux-0.4.0-cp313-cp313-win_amd64.whl"
    wheel.write_bytes(_make_wheel_bytes())

    with pytest.raises(
        ValueError,
        match=r"Infernux 0\.4\.0 requires Python 3\.13.*install Python 3\.13",
    ):
        manager.install_local_wheel(str(wheel))

    assert not (tmp_path / "versions" / "0.4.0").exists()


def test_release_with_conflicting_python_abis_is_rejected(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(vm_mod, "_VERSIONS_DIR", tmp_path / "versions")
    manager = VersionManager(_RuntimeInventory("3.12"))
    release = {
        "tag_name": "v0.4.0",
        "assets": [
            {
                "name": "infernux-0.4.0-cp313-cp313-win_amd64.whl",
                "browser_download_url": "https://example.invalid/cp313.whl",
                "size": 313,
            },
            {
                "name": "infernux-0.4.0-cp312-cp312-win_amd64.whl",
                "browser_download_url": "https://example.invalid/cp312.whl",
                "size": 312,
            },
        ],
    }
    monkeypatch.setattr(manager, "_fetch_releases", lambda: [release])

    [engine] = manager.list_versions()

    assert engine.python_version == ""
    assert [wheel.python_version for wheel in engine.wheel_options] == [
        "3.13",
        "3.12",
    ]
    assert "must target exactly one Python minor version" in engine.compatibility_error


def test_online_engine_stays_visible_but_install_is_blocked_without_python(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(vm_mod, "_VERSIONS_DIR", tmp_path / "versions")
    manager = VersionManager(_RuntimeInventory("3.12"))
    release = {
        "tag_name": "v0.4.0",
        "assets": [
            {
                "name": "infernux-0.4.0-cp313-cp313-win_amd64.whl",
                "browser_download_url": "https://example.invalid/cp313.whl",
                "size": 313,
            }
        ],
    }
    monkeypatch.setattr(manager, "_fetch_releases", lambda: [release])

    [engine] = manager.list_versions()

    assert engine.version == "0.4.0"
    assert engine.python_version == "3.13"
    assert manager.installation_block_reason(engine) == (
        "Infernux 0.4.0 requires Python 3.13. "
        "Please install Python 3.13 first."
    )
    monkeypatch.setattr(
        vm_mod.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("a blocked engine install must not start a download")
        ),
    )
    with pytest.raises(ValueError, match=r"requires Python 3\.13"):
        manager.download_version("0.4.0")


def test_cached_engine_wheels_are_resolved_by_exact_python_abi(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(vm_mod, "_VERSIONS_DIR", tmp_path / "versions")
    version_dir = tmp_path / "versions" / "0.4.0"
    version_dir.mkdir(parents=True)
    cp312 = version_dir / "infernux-0.4.0-cp312-cp312-win_amd64.whl"
    cp313 = version_dir / "infernux-0.4.0-cp313-cp313-win_amd64.whl"
    cp312.write_bytes(_make_wheel_bytes())
    cp313.write_bytes(_make_wheel_bytes())
    manager = VersionManager(_RuntimeInventory("3.12", "3.13"))

    assert manager.get_wheel_path("0.4.0", "3.12") == str(cp312)
    assert manager.get_wheel_path("0.4.0", "3.13") == str(cp313)
    assert manager.installed_python_versions("0.4.0") == ["3.13", "3.12"]
    with pytest.raises(ValueError, match="conflicting Python ABIs"):
        manager.python_version_for_engine("0.4.0")


def test_release_assets_are_filtered_by_host_platform(tmp_path, monkeypatch):
    monkeypatch.setattr(vm_mod, "_VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(
        vm_mod,
        "supported_wheel_platforms",
        lambda: frozenset({"win_amd64"}),
    )
    manager = VersionManager(_RuntimeInventory("3.13"))
    release = {
        "tag_name": "v0.4.0",
        "assets": [
            {
                "name": "infernux-0.4.0-cp313-cp313-win_amd64.whl",
                "browser_download_url": "https://example.invalid/windows.whl",
                "size": 101,
            },
            {
                "name": "infernux-0.4.0-cp313-cp313-manylinux_2_28_x86_64.whl",
                "browser_download_url": "https://example.invalid/linux.whl",
                "size": 202,
            },
        ],
    }
    monkeypatch.setattr(manager, "_fetch_releases", lambda: [release])

    [engine] = manager.list_versions()

    assert [wheel.filename for wheel in engine.wheel_options] == [
        "infernux-0.4.0-cp313-cp313-win_amd64.whl"
    ]
    assert engine.wheel_url == "https://example.invalid/windows.whl"
    assert engine.python_version == "3.13"


def test_local_engine_install_rejects_foreign_platform_before_copy(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(vm_mod, "_VERSIONS_DIR", tmp_path / "versions")
    monkeypatch.setattr(
        vm_mod,
        "supported_wheel_platforms",
        lambda: frozenset({"win_amd64"}),
    )
    manager = VersionManager(_RuntimeInventory("3.13"))
    wheel = tmp_path / "infernux-0.4.0-cp313-cp313-manylinux_2_28_x86_64.whl"
    wheel.write_bytes(_make_wheel_bytes())

    with pytest.raises(ValueError, match="not compatible with this platform"):
        manager.install_local_wheel(str(wheel))

    assert not (tmp_path / "versions" / "0.4.0").exists()
