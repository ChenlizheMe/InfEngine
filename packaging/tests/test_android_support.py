from __future__ import annotations

import ast
import configparser
import json
import os
import re
import runpy
import shutil
import stat
import subprocess
import sys
import textwrap
import zipfile
from pathlib import Path

import pytest


PACKAGING_DIR = Path(__file__).resolve().parents[1]
if str(PACKAGING_DIR) not in sys.path:
    sys.path.insert(0, str(PACKAGING_DIR))

import android_support


@pytest.mark.parametrize("tag", ["", "v0.4.0"])
def test_kit_workflow_resolves_verification_ref_or_explicit_release(tmp_path, tag):
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("PowerShell is required to execute the release resolver")
    workflow = (PACKAGING_DIR.parent / ".github/workflows/platform-plugin-release.yml").read_text(
        encoding="utf-8"
    )
    step = workflow.split("      - name: Resolve release tag\n", 1)[1].split(
        "\n  build-android-python-support:", 1
    )[0]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    output = tmp_path / "outputs"
    check = tmp_path / "resolve.ps1"
    check.write_text(
        "function gh { $global:LASTEXITCODE = 0; Write-Output 'checked-release' }\n" + script,
        encoding="utf-8",
    )
    environment = dict(os.environ, RELEASE_EVENT_TAG="", RELEASE_INPUT_TAG=tag,
                       SOURCE_COMMIT="a" * 40, GITHUB_OUTPUT=str(output))
    result = subprocess.run([pwsh, "-NoProfile", "-File", str(check)],
                            env=environment, capture_output=True, text=True, check=True)
    assert output.read_text(encoding="utf-8").splitlines() == [
        f"tag={tag}", f"source-ref={tag or 'a' * 40}",
    ]
    assert ("checked-release" in result.stdout) == bool(tag)
    assert "if: needs.resolve-release.outputs.release-tag != ''" in workflow
    assert "name: android-support-${{ matrix.host }}" in workflow
    assert "--clobber" not in workflow


def test_release_kit_resolves_gradle_from_the_installed_launcher(tmp_path):
    workflow = (PACKAGING_DIR.parent / ".github/workflows/platform-plugin-release.yml").read_text(
        encoding="utf-8"
    )
    match = re.search(r"gradle_home=\"\$\(python -c '([^']+)'\)\"", workflow)
    assert match is not None, "Resolve the setup-gradle PATH entry, not human-readable --version output"
    gradle_home = tmp_path / "shared tools" / "gradle-8.12"
    launcher = gradle_home / "bin" / ("gradle.bat" if os.name == "nt" else "gradle")
    launcher.parent.mkdir(parents=True)
    launcher.write_text("exit 97\n", encoding="utf-8")
    launcher.chmod(0o755)
    environment = dict(os.environ, PATH=str(launcher.parent), PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", match.group(1)],
        env=environment, cwd=tmp_path, capture_output=True, encoding="utf-8", check=True,
    )
    assert Path(result.stdout.strip()) == gradle_home.resolve()


def test_android_python_producer_selects_java_before_sdk_setup():
    workflow = (PACKAGING_DIR.parent / ".github/workflows/platform-plugin-release.yml").read_text(
        encoding="utf-8"
    )
    producer = workflow.split("  build-android-python-support:", 1)[1].split(
        "  publish-android-support:", 1
    )[0]
    assert producer.index("actions/setup-java@v4") < producer.index("android-actions/setup-android@v3")
    assert 'java-version: "17"' in producer
    assert 'python-version: "3.13"' in producer
    assert "git submodule update --init --depth 1 external/plugins/infernux_android" in producer


@pytest.mark.parametrize("abi", ["arm64-v8a", "x86_64"])
def test_numpy_cross_file_uses_the_selected_target_python(tmp_path, abi):
    setup = PACKAGING_DIR.parent / "scripts/setup"
    script = (setup / "build_android_python_runtime.sh").read_text(encoding="utf-8")
    configure = script.split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
    library = tmp_path / "target Python" / abi / "libpython3.13.so"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"target library fixture")
    output = tmp_path / "cross.ini"
    subprocess.run(
        [sys.executable, "-c", configure,
         str(setup / "android_numpy_cross.ini"), str(output), str(library)],
        check=True,
    )
    config = configparser.ConfigParser()
    config.read(output, encoding="utf-8")
    assert ast.literal_eval(config["constants"]["python_runtime"]) == str(library.resolve())
    for language in ("c", "cpp"):
        assert config["built-in options"][f"{language}_link_args"] == "[python_runtime]"


def test_kit_file_identity_matches_the_build_host(tmp_path):
    script = PACKAGING_DIR.parent / "scripts/setup/package_android_support.py"
    files = runpy.run_path(str(script))["_files"]
    (tmp_path / "header.h").write_text("/* NDK header */", encoding="utf-8")
    roots = (("sdk/xt_CONNMARK", tmp_path), ("sdk/xt_connmark", tmp_path))
    if os.name == "nt":
        with pytest.raises(RuntimeError, match="Duplicate"):
            tuple(files(roots))
    else:
        assert len(tuple(files(roots))) == 2
    with pytest.raises(RuntimeError, match="Duplicate"):
        tuple(files((roots[0], roots[0])))


def test_sdk_setup_uses_a_shell_that_resolves_windows_batch_commands():
    workflow = (PACKAGING_DIR.parent / ".github/workflows/platform-plugin-release.yml").read_text(
        encoding="utf-8"
    )
    install = workflow.split("- name: Install the pinned Android build SDK", 1)[1].split(
        "- uses:", 1
    )[0]
    assert "shell: pwsh" in install
    assert "if ($LASTEXITCODE -ne 0)" in install


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


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable permissions")
def test_installed_kit_preserves_executables_without_privileged_mode_bits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    _create_support(source)
    java = source / "jdk/bin/java"
    java.write_text("#!/bin/sh\nprintf 'kit-java-ready\\n'\n", encoding="utf-8")
    java.chmod(0o4755)
    plain = source / "jdk/release"
    plain.chmod(0o644)
    archive = tmp_path / "android.inxkit"
    _archive_tree(source, archive)
    manager = android_support.AndroidSupportManager(tmp_path / "managed")
    # Environment publication is covered separately; do not leak this test's kit.
    monkeypatch.setattr(manager, "activate_environment", lambda: True)

    manager.install_archive(archive)

    executable = manager.root / "jdk/bin/java"
    assert stat.S_IMODE(executable.stat().st_mode) == 0o755
    assert stat.S_IMODE((manager.root / "jdk/release").stat().st_mode) == 0o644
    result = subprocess.run([str(executable)], check=True, capture_output=True, text=True)
    assert result.stdout == "kit-java-ready\n"


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
