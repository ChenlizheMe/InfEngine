"""Read-only Android toolchain discovery and diagnostics."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Mapping

from Infernux.engine.build import (
    BuildDiagnostic,
    BuildTargetId,
    CapabilityReport,
    DiagnosticSeverity,
)


ANDROID_API = 36
ANDROID_BUILD_TOOLS = "36.0.0"
ANDROID_CMAKE = "3.30.5"
ANDROID_NDK = "29.0.14206865"
ANDROID_JDK_MAJOR = 17
ANDROID_GRADLE_PLUGIN = "8.10.1"
GRADLE_VERSION = "8.12"


def inspect_android_toolchain(
    target: BuildTargetId | str,
    environ: Mapping[str, str] | None = None,
) -> CapabilityReport:
    values = os.environ if environ is None else environ
    target_id = BuildTargetId(target)
    diagnostics: list[BuildDiagnostic] = []
    details: dict[str, object] = {
        "target": str(target_id),
        "android_api": ANDROID_API,
        "build_tools_version": ANDROID_BUILD_TOOLS,
        "cmake_version": ANDROID_CMAKE,
        "ndk_version": ANDROID_NDK,
        "jdk_major": ANDROID_JDK_MAJOR,
        "android_gradle_plugin_version": ANDROID_GRADLE_PLUGIN,
        "gradle_version": GRADLE_VERSION,
    }

    sdk_root = _environment_path(values, "ANDROID_SDK_ROOT", "ANDROID_HOME")
    java_home = _environment_path(values, "JAVA_HOME")
    details["sdk_root"] = str(sdk_root) if sdk_root else ""
    details["java_home"] = str(java_home) if java_home else ""
    source_root = _source_root(values)
    details["source_root"] = str(source_root) if source_root else ""

    if sdk_root is None:
        diagnostics.append(
            _error(
                "android.sdk.environment",
                "Set ANDROID_SDK_ROOT to the Android SDK directory.",
            )
        )
    else:
        _require_directory(
            sdk_root / "platforms" / f"android-{ANDROID_API}",
            "android.sdk.platform",
            f"Install Android platform android-{ANDROID_API}.",
            diagnostics,
        )
        _require_directory(
            sdk_root / "build-tools" / ANDROID_BUILD_TOOLS,
            "android.sdk.build-tools",
            f"Install Android build-tools {ANDROID_BUILD_TOOLS}.",
            diagnostics,
        )
        _require_directory(
            sdk_root / "cmake" / ANDROID_CMAKE,
            "android.sdk.cmake",
            f"Install Android CMake {ANDROID_CMAKE}.",
            diagnostics,
        )
        ndk_root = sdk_root / "ndk" / ANDROID_NDK
        _require_file(
            ndk_root / "build" / "cmake" / "android.toolchain.cmake",
            "android.ndk.toolchain",
            f"Install Android NDK {ANDROID_NDK}.",
            diagnostics,
        )
        details["ndk_root"] = str(ndk_root)
        adb = sdk_root / "platform-tools" / _executable("adb")
        _require_file(
            adb,
            "android.sdk.adb",
            "Install Android SDK platform-tools.",
            diagnostics,
        )
        details["adb"] = str(adb)

        if target_id == "android-x64-emulator":
            emulator = sdk_root / "emulator" / _executable("emulator")
            _require_file(
                emulator,
                "android.sdk.emulator",
                "Install the Android Emulator package.",
                diagnostics,
            )
            details["emulator"] = str(emulator)
            avd_home = _avd_home(values)
            avds = _available_avds(avd_home)
            details["avd_home"] = str(avd_home) if avd_home else ""
            details["avds"] = avds
            if not avds:
                diagnostics.append(
                    _error(
                        "android.emulator.avd",
                        "Create an x86_64 AOSP Android Virtual Device.",
                    )
                )

    if java_home is None:
        diagnostics.append(
            _error("android.jdk.environment", "Set JAVA_HOME to a JDK 17 directory.")
        )
    else:
        java = java_home / "bin" / _executable("java")
        _require_file(
            java,
            "android.jdk.java",
            "JAVA_HOME does not contain the Java launcher.",
            diagnostics,
        )
        major = _read_jdk_major(java_home)
        details["jdk_detected_major"] = major
        if major != ANDROID_JDK_MAJOR:
            diagnostics.append(
                _error(
                    "android.jdk.version",
                    f"JDK {ANDROID_JDK_MAJOR} is required; detected {major or 'unknown'}.",
                )
            )

    if source_root is None:
        diagnostics.append(
            _error(
                "android.source.checkout",
                "Set INFERNUX_SOURCE_ROOT to an Infernux source checkout for Android host bring-up.",
            )
        )
    else:
        _require_file(
            source_root / "CMakeLists.txt",
            "android.source.root",
            "INFERNUX_SOURCE_ROOT is not an Infernux source checkout.",
            diagnostics,
        )
        _require_file(
            source_root / "external" / "SDL" / "CMakeLists.txt",
            "android.source.sdl",
            "The Infernux source checkout does not contain SDL3 sources.",
            diagnostics,
        )
        gradle = _gradle_command(values, source_root)
        details["gradle"] = str(gradle)
        _require_file(
            gradle,
            "android.gradle.launcher",
            "Install Gradle 8.12 or restore the SDL3 Gradle wrapper.",
            diagnostics,
        )

    return CapabilityReport(
        not any(item.severity is DiagnosticSeverity.ERROR for item in diagnostics),
        tuple(diagnostics),
        details,
    )


def _environment_path(values: Mapping[str, str], *names: str) -> Path | None:
    for name in names:
        value = str(values.get(name, "") or "").strip()
        if value:
            return Path(value).expanduser().resolve()
    return None


def _source_root(values: Mapping[str, str]) -> Path | None:
    explicit = _environment_path(values, "INFERNUX_SOURCE_ROOT")
    if explicit is not None:
        return explicit
    path = Path(__file__).resolve()
    for parent in path.parents:
        if (
            (parent / "CMakeLists.txt").is_file()
            and (parent / "external" / "SDL" / "CMakeLists.txt").is_file()
        ):
            return parent
    return None


def _gradle_command(values: Mapping[str, str], source_root: Path) -> Path:
    gradle_home = _environment_path(values, "INFERNUX_GRADLE_HOME", "GRADLE_HOME")
    if gradle_home is not None:
        return gradle_home / "bin" / ("gradle.bat" if os.name == "nt" else "gradle")
    return (
        source_root
        / "external"
        / "SDL"
        / "android-project"
        / ("gradlew.bat" if os.name == "nt" else "gradlew")
    )


def _avd_home(values: Mapping[str, str]) -> Path | None:
    explicit = _environment_path(values, "ANDROID_AVD_HOME")
    if explicit is not None:
        return explicit
    user_home = _environment_path(values, "ANDROID_USER_HOME")
    if user_home is not None:
        direct = user_home / "avd"
        return direct if direct.is_dir() else user_home
    home = str(values.get("USERPROFILE", "") or values.get("HOME", "")).strip()
    return Path(home).expanduser().resolve() / ".android" / "avd" if home else None


def _available_avds(avd_home: Path | None) -> list[str]:
    if avd_home is None or not avd_home.is_dir():
        return []
    return sorted(path.stem for path in avd_home.glob("*.ini") if path.is_file())


def _read_jdk_major(java_home: Path) -> int:
    release_path = java_home / "release"
    try:
        release = release_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    match = re.search(r'^JAVA_VERSION="(?P<major>\d+)', release, re.MULTILINE)
    return int(match.group("major")) if match else 0


def _require_directory(
    path: Path,
    code: str,
    message: str,
    diagnostics: list[BuildDiagnostic],
) -> None:
    if not path.is_dir():
        diagnostics.append(_error(code, message, path=str(path)))


def _require_file(
    path: Path,
    code: str,
    message: str,
    diagnostics: list[BuildDiagnostic],
) -> None:
    if not path.is_file():
        diagnostics.append(_error(code, message, path=str(path)))


def _error(code: str, message: str, **detail: object) -> BuildDiagnostic:
    return BuildDiagnostic(
        DiagnosticSeverity.ERROR,
        code,
        message,
        source="infernux/platform-android",
        detail=detail,
    )


def _executable(stem: str) -> str:
    return f"{stem}.exe" if os.name == "nt" else stem


__all__ = [
    "ANDROID_API",
    "ANDROID_BUILD_TOOLS",
    "ANDROID_CMAKE",
    "ANDROID_JDK_MAJOR",
    "ANDROID_GRADLE_PLUGIN",
    "ANDROID_NDK",
    "GRADLE_VERSION",
    "inspect_android_toolchain",
]
