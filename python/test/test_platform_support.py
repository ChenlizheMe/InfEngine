from __future__ import annotations

import json
import os
import platform
from pathlib import Path

import pytest

from Infernux.plugins.platform_support import (
    ANDROID_SUPPORT_REQUIRED_MESSAGE,
    android_support_available,
    plugin_install_block_reason,
    require_plugin_support,
)
from Infernux.plugins import InxPackage, PluginManager


def _host_id() -> str:
    if os.name == "nt":
        return "windows-x64"
    return "linux-x64" if platform.system().casefold() == "linux" else ""


def _support_root(root: Path) -> None:
    sdk = root / "sdk"
    (sdk / "platforms/android-36").mkdir(parents=True)
    (sdk / "build-tools/36.0.0").mkdir(parents=True)
    toolchain = sdk / "ndk/29.0.14206865/build/cmake/android.toolchain.cmake"
    toolchain.parent.mkdir(parents=True)
    toolchain.write_bytes(b"toolchain")
    adb = sdk / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb")
    adb.parent.mkdir(parents=True)
    adb.write_bytes(b"adb")
    java = root / "jdk/bin" / ("java.exe" if os.name == "nt" else "java")
    java.parent.mkdir(parents=True)
    java.write_bytes(b"java")
    gradle = root / "gradle/bin" / ("gradle.bat" if os.name == "nt" else "gradle")
    gradle.parent.mkdir(parents=True)
    gradle.write_bytes(b"gradle")
    for abi in ("arm64-v8a", "x86_64"):
        runtime = root / "python" / abi / "infernux-android-python.json"
        runtime.parent.mkdir(parents=True)
        runtime.write_text("{}", encoding="utf-8")
    (root / "infernux-android-support.json").write_text(
        json.dumps(
            {
                "$schema": "infernux.android_support",
                "kind": "infernux-android-support",
                "version": "0.1.0",
                "host": _host_id(),
                "requirements": {},
                "paths": {
                    "sdk": "sdk",
                    "jdk": "jdk",
                    "gradle": "gradle",
                    "python": {
                        "arm64-v8a": "python/arm64-v8a",
                        "x86_64": "python/x86_64",
                    },
                },
                "total_bytes": 1,
            }
        ),
        encoding="utf-8",
    )


def test_android_plugin_is_blocked_before_hub_support_is_installed(tmp_path: Path) -> None:
    environment = {"INFERNUX_ANDROID_SUPPORT_ROOT": str(tmp_path / "missing")}

    assert plugin_install_block_reason("vendor/other", environment) == ""
    assert (
        plugin_install_block_reason("infernux/platform-android", environment)
        == ANDROID_SUPPORT_REQUIRED_MESSAGE
    )
    with pytest.raises(RuntimeError, match="Infernux Hub"):
        require_plugin_support("infernux/platform-android", environment)


def test_android_plugin_is_unblocked_only_by_a_complete_hub_support_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "android"
    root.mkdir()
    _support_root(root)
    environment = {"INFERNUX_ANDROID_SUPPORT_ROOT": str(root)}

    assert android_support_available(environment)
    assert plugin_install_block_reason("infernux/platform-android", environment) == ""

    (root / "sdk/platform-tools" / ("adb.exe" if os.name == "nt" else "adb")).unlink()
    assert not android_support_available(environment)


def test_plugin_manager_cannot_bypass_the_hub_android_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    (source / "inx_package.json").write_text(
        json.dumps(
            {
                "reference": "infernux/platform-android",
                "name": "Android",
                "version": "0.1.0",
                "engine": "",
            }
        ),
        encoding="utf-8",
    )
    (source / "editor").mkdir()
    (source / "editor/plugin.py").write_text("VALUE = 1\n", encoding="utf-8")
    package = tmp_path / "android.inxpkg"
    InxPackage.export_source(str(source), str(package))
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv(
        "INFERNUX_ANDROID_SUPPORT_ROOT", str(tmp_path / "missing-support")
    )

    with pytest.raises(RuntimeError, match="Infernux Hub"):
        PluginManager(str(project)).install_package(
            str(package), install_dependencies=False
        )

    assert not (project / "Packages/infernux/platform-android").exists()


def test_plugin_panel_marks_android_import_unavailable_before_click(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from Infernux.engine.ui.plugin_panel import PluginPanel

    class _Registry:
        @staticmethod
        def installed():
            return []

        @staticmethod
        def available():
            return [
                {
                    "reference": "infernux/platform-android",
                    "name": "Android",
                    "source": {"type": "github", "official": True},
                }
            ]

    class _Manager:
        registry = _Registry()
        states = {}

        @staticmethod
        def cached_reference_path(_reference):
            return ""

    monkeypatch.setenv(
        "INFERNUX_ANDROID_SUPPORT_ROOT", str(tmp_path / "missing-support")
    )
    rows = PluginPanel()._visible_rows(_Manager())

    assert rows[0]["_install_block_reason"] == ANDROID_SUPPORT_REQUIRED_MESSAGE
