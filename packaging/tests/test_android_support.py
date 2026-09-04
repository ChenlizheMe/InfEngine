from __future__ import annotations

import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import android_support


def _create_support(root: Path) -> None:
    sdk = root / "sdk"
    for directory in (
        sdk / "platforms/android-36",
        sdk / "build-tools/36.0.0",
        sdk / "cmake/3.30.5",
    ):
        directory.mkdir(parents=True)
        (directory / "source.properties").write_text("installed", encoding="utf-8")
    (sdk / "licenses").mkdir(parents=True)
    (sdk / "licenses/android-sdk-license").write_text("accepted", encoding="utf-8")
    toolchain = sdk / "ndk/29.0.14206865/build/cmake/android.toolchain.cmake"
    toolchain.parent.mkdir(parents=True)
    toolchain.write_text("toolchain", encoding="utf-8")
    adb = sdk / "platform-tools" / ("adb.exe" if sys.platform == "win32" else "adb")
    adb.parent.mkdir(parents=True)
    adb.write_bytes(b"adb")

    jdk = root / "jdk"
    java = jdk / "bin" / ("java.exe" if sys.platform == "win32" else "java")
    java.parent.mkdir(parents=True)
    java.write_bytes(b"java")
    (jdk / "release").write_text('JAVA_VERSION="17.0.12"\n', encoding="utf-8")
    gradle = root / "gradle/bin" / ("gradle.bat" if sys.platform == "win32" else "gradle")
    gradle.parent.mkdir(parents=True)
    gradle.write_bytes(b"gradle")

    for abi in android_support.ANDROID_PYTHON_ABIS:
        prefix = root / "python" / abi
        for directory in (
            prefix / "include/python3.13",
            prefix / "lib/python3.13",
        ):
            directory.mkdir(parents=True)
            (directory / ".keep").write_bytes(b"runtime")
        (prefix / "infernux-android-python.json").write_text(
            json.dumps(
                {
                    "$schema": "infernux.android_python_runtime",
                    "kind": "infernux-android-cpython",
                    "target": {"abi": abi},
                    "cpython": {"version": "3.13.15"},
                    "toolchain": {"ndk_version": android_support.ANDROID_PYTHON_NDK},
                }
            ),
            encoding="utf-8",
        )

    (root / android_support.MANIFEST_NAME).write_text(
        json.dumps(
            {
                "$schema": android_support.ANDROID_SUPPORT_SCHEMA,
                "kind": android_support.ANDROID_SUPPORT_KIND,
                "version": android_support.ANDROID_SUPPORT_VERSION,
                "host": android_support.host_id(),
                "requirements": {
                    "android_api": android_support.ANDROID_API,
                    "build_tools": android_support.ANDROID_BUILD_TOOLS,
                    "cmake": android_support.ANDROID_CMAKE,
                    "ndk": android_support.ANDROID_NDK,
                    "python_ndk": android_support.ANDROID_PYTHON_NDK,
                    "jdk_major": android_support.ANDROID_JDK_MAJOR,
                    "gradle": android_support.GRADLE_VERSION,
                    "python": android_support.ANDROID_PYTHON_SERIES,
                    "python_abis": list(android_support.ANDROID_PYTHON_ABIS),
                },
                "paths": {
                    "sdk": "sdk",
                    "jdk": "jdk",
                    "gradle": "gradle",
                    "python": {
                        "arm64-v8a": "python/arm64-v8a",
                        "x86_64": "python/x86_64",
                    },
                },
                "total_bytes": 128,
            }
        ),
        encoding="utf-8",
    )


def _archive_tree(source: Path, destination: Path) -> None:
    with zipfile.ZipFile(destination, "w") as archive:
        for path in sorted(source.rglob("*")):
            if path.is_file():
                archive.write(path, path.relative_to(source).as_posix())


def test_installs_one_shared_android_support_archive_and_exports_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _create_support(source)
    archive = tmp_path / "android.inxkit"
    _archive_tree(source, archive)
    manager = android_support.AndroidSupportManager(tmp_path / "managed")
    monkeypatch.delenv("INFERNUX_ANDROID_SUPPORT_ROOT", raising=False)

    assert not manager.status().installed
    installed = manager.install_archive(archive)

    assert installed == str(manager.root)
    assert manager.status().installed
    assert Path(os.environ["INFERNUX_ANDROID_SUPPORT_ROOT"]) == manager.root
    environment = android_support.android_support_environment(manager.root)
    assert environment["ANDROID_SDK_ROOT"] == str(manager.root / "sdk")
    assert environment["INFERNUX_ANDROID_PYTHON_PREFIX_ARM64"].endswith(
        "python\\arm64-v8a" if sys.platform == "win32" else "python/arm64-v8a"
    )


def test_invalid_reinstall_preserves_the_current_platform_kit(tmp_path: Path) -> None:
    current = tmp_path / "managed"
    _create_support(current)
    before = (current / android_support.MANIFEST_NAME).read_bytes()
    broken = tmp_path / "broken.inxkit"
    with zipfile.ZipFile(broken, "w") as archive:
        archive.writestr(android_support.MANIFEST_NAME, "{}")

    manager = android_support.AndroidSupportManager(current)
    with pytest.raises(android_support.AndroidSupportError, match="manifest"):
        manager.install_archive(broken)

    assert (current / android_support.MANIFEST_NAME).read_bytes() == before
    assert manager.status().installed


def test_release_asset_requires_a_github_sha256_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            return json.dumps(
                [
                    {
                        "assets": [
                            {
                                "name": android_support.archive_name(),
                                "browser_download_url": "https://example.invalid/android.inxkit",
                                "size": 10,
                                "digest": None,
                            }
                        ]
                    }
                ]
            ).encode("utf-8")

    monkeypatch.setattr(android_support.urllib.request, "urlopen", lambda *_a, **_k: _Response())
    with pytest.raises(android_support.AndroidSupportError, match="immutable provenance"):
        android_support.AndroidSupportManager._release_asset()


def test_release_packager_creates_the_exact_hub_installable_asset(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.mkdir()
    _create_support(source)
    output = tmp_path / android_support.archive_name()
    command = [
        sys.executable,
        str(PACKAGING_DIR.parent / "scripts/setup/package_android_support.py"),
        "--sdk",
        str(source / "sdk"),
        "--jdk",
        str(source / "jdk"),
        "--gradle",
        str(source / "gradle"),
        "--python-arm64",
        str(source / "python/arm64-v8a"),
        "--python-x86-64",
        str(source / "python/x86_64"),
        "--output",
        str(output),
    ]

    subprocess.run(command, check=True, capture_output=True, text=True)
    first = output.read_bytes()
    subprocess.run(command, check=True, capture_output=True, text=True)

    assert output.read_bytes() == first
    manager = android_support.AndroidSupportManager(tmp_path / "installed")
    manager.install_archive(output)
    assert manager.status().installed
