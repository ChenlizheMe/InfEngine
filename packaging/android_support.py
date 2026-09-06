"""Hub-owned Android Platform Kit discovery and installation.

The Android platform plugin remains a small project package.  Heavy, reusable
host tools live once under the Hub data root and are exposed to every Editor
process through a stable environment contract.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import tempfile
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Mapping

from hub_utils import get_hub_shared_data_dir


ANDROID_SUPPORT_SCHEMA = "infernux.android_support"
ANDROID_SUPPORT_KIND = "infernux-android-support"
ANDROID_SUPPORT_VERSION = "0.1.0"
ANDROID_API = 36
ANDROID_BUILD_TOOLS = "36.0.0"
ANDROID_CMAKE = "3.30.5"
ANDROID_NDK = "29.0.14206865"
ANDROID_PYTHON_NDK = "27.3.13750724"
ANDROID_JDK_MAJOR = 17
GRADLE_VERSION = "8.12"
ANDROID_PYTHON_SERIES = "3.13"
ANDROID_PYTHON_ABIS = ("arm64-v8a", "x86_64")
MANIFEST_NAME = "infernux-android-support.json"
GITHUB_RELEASES_URL = (
    "https://api.github.com/repos/ChenlizheMe/Infernux/releases?per_page=30"
)


class AndroidSupportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class AndroidSupportStatus:
    installed: bool
    root: Path
    version: str = ""
    total_bytes: int = 0
    error: str = ""


def host_id() -> str:
    machine = platform.machine().casefold()
    if machine not in {"amd64", "x86_64"}:
        raise AndroidSupportError(
            f"Android compatibility is not published for this host architecture: {machine}"
        )
    if os.name == "nt":
        return "windows-x64"
    if os.name == "posix" and platform.system().casefold() == "linux":
        return "linux-x64"
    raise AndroidSupportError(
        f"Android compatibility is not published for this host: {platform.system()}"
    )


def archive_name() -> str:
    return f"infernux-android-support-{ANDROID_SUPPORT_VERSION}-{host_id()}.inxkit"


def default_android_support_root() -> Path:
    return (
        Path(get_hub_shared_data_dir())
        / "PlatformKits"
        / "android"
        / ANDROID_SUPPORT_VERSION
        / host_id()
    ).resolve()


def create_android_support_manifest(*, total_bytes: int) -> dict[str, object]:
    if type(total_bytes) is not int or total_bytes < 0:
        raise ValueError("Android compatibility size must be a non-negative integer")
    return {
        "$schema": ANDROID_SUPPORT_SCHEMA,
        "kind": ANDROID_SUPPORT_KIND,
        "version": ANDROID_SUPPORT_VERSION,
        "host": host_id(),
        "requirements": {
            "android_api": ANDROID_API,
            "build_tools": ANDROID_BUILD_TOOLS,
            "cmake": ANDROID_CMAKE,
            "ndk": ANDROID_NDK,
            "python_ndk": ANDROID_PYTHON_NDK,
            "jdk_major": ANDROID_JDK_MAJOR,
            "gradle": GRADLE_VERSION,
            "python": ANDROID_PYTHON_SERIES,
            "python_abis": list(ANDROID_PYTHON_ABIS),
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
        "total_bytes": total_bytes,
    }


def _portable_relative(value: object, *, label: str) -> Path:
    text = str(value or "").replace("\\", "/").strip("/")
    path = PurePosixPath(text)
    if (
        not text
        or path.is_absolute()
        or any(part in {"", ".", ".."} for part in path.parts)
        or (path.parts and ":" in path.parts[0])
    ):
        raise AndroidSupportError(f"Android compatibility {label} path is unsafe")
    return Path(*path.parts)


def _read_manifest(root: Path) -> dict[str, object]:
    manifest_path = root / MANIFEST_NAME
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise AndroidSupportError(
            f"Android compatibility manifest is unreadable: {manifest_path}"
        ) from exc
    expected = {
        "$schema",
        "kind",
        "version",
        "host",
        "requirements",
        "paths",
        "total_bytes",
    }
    if type(document) is not dict or set(document) != expected:
        raise AndroidSupportError(
            f"Android compatibility manifest does not match the current contract: {manifest_path}"
        )
    if (
        document["$schema"] != ANDROID_SUPPORT_SCHEMA
        or document["kind"] != ANDROID_SUPPORT_KIND
        or document["version"] != ANDROID_SUPPORT_VERSION
        or document["host"] != host_id()
    ):
        raise AndroidSupportError(
            f"Android compatibility has the wrong version or host: {manifest_path}"
        )
    requirements = document["requirements"]
    if type(requirements) is not dict or requirements != {
        "android_api": ANDROID_API,
        "build_tools": ANDROID_BUILD_TOOLS,
        "cmake": ANDROID_CMAKE,
        "ndk": ANDROID_NDK,
        "python_ndk": ANDROID_PYTHON_NDK,
        "jdk_major": ANDROID_JDK_MAJOR,
        "gradle": GRADLE_VERSION,
        "python": ANDROID_PYTHON_SERIES,
        "python_abis": list(ANDROID_PYTHON_ABIS),
    }:
        raise AndroidSupportError(
            f"Android compatibility requirements are stale: {manifest_path}"
        )
    paths = document["paths"]
    if type(paths) is not dict or set(paths) != {
        "sdk",
        "jdk",
        "gradle",
        "python",
    }:
        raise AndroidSupportError(
            f"Android compatibility paths are invalid: {manifest_path}"
        )
    python_paths = paths["python"]
    if type(python_paths) is not dict or set(python_paths) != set(ANDROID_PYTHON_ABIS):
        raise AndroidSupportError(
            f"Android compatibility Python paths are invalid: {manifest_path}"
        )
    if type(document["total_bytes"]) is not int or document["total_bytes"] < 0:
        raise AndroidSupportError(
            f"Android compatibility size is invalid: {manifest_path}"
        )
    return document


def _require_file(path: Path, label: str) -> None:
    if not path.is_file():
        raise AndroidSupportError(f"Android compatibility is missing {label}: {path}")


def _require_directory(path: Path, label: str) -> None:
    if not path.is_dir():
        raise AndroidSupportError(f"Android compatibility is missing {label}: {path}")


def validate_android_support(root: Path) -> dict[str, object]:
    root = root.expanduser().resolve()
    document = _read_manifest(root)
    paths = document["paths"]
    assert isinstance(paths, Mapping)
    sdk = root / _portable_relative(paths["sdk"], label="SDK")
    jdk = root / _portable_relative(paths["jdk"], label="JDK")
    gradle = root / _portable_relative(paths["gradle"], label="Gradle")
    python_paths = paths["python"]
    assert isinstance(python_paths, Mapping)

    _require_directory(sdk / "platforms" / f"android-{ANDROID_API}", "SDK platform")
    _require_directory(sdk / "build-tools" / ANDROID_BUILD_TOOLS, "build-tools")
    _require_directory(sdk / "cmake" / ANDROID_CMAKE, "Android CMake")
    _require_file(
        sdk / "ndk" / ANDROID_NDK / "build" / "cmake" / "android.toolchain.cmake",
        "NDK toolchain",
    )
    _require_file(
        sdk / "platform-tools" / ("adb.exe" if os.name == "nt" else "adb"),
        "adb",
    )
    _require_file(jdk / "bin" / ("java.exe" if os.name == "nt" else "java"), "JDK")
    try:
        release_text = (jdk / "release").read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise AndroidSupportError(f"Android compatibility JDK release is unreadable: {jdk}") from exc
    match = re.search(r'^JAVA_VERSION="(?P<major>\d+)', release_text, re.MULTILINE)
    if match is None or int(match.group("major")) != ANDROID_JDK_MAJOR:
        raise AndroidSupportError(f"Android compatibility requires JDK {ANDROID_JDK_MAJOR}: {jdk}")
    _require_file(
        gradle / "bin" / ("gradle.bat" if os.name == "nt" else "gradle"),
        "Gradle",
    )

    for abi in ANDROID_PYTHON_ABIS:
        prefix = root / _portable_relative(python_paths[abi], label=f"Python {abi}")
        _require_directory(prefix / f"include/python{ANDROID_PYTHON_SERIES}", f"Python headers ({abi})")
        _require_directory(prefix / f"lib/python{ANDROID_PYTHON_SERIES}", f"Python stdlib ({abi})")
        runtime_manifest = prefix / "infernux-android-python.json"
        try:
            runtime = json.loads(runtime_manifest.read_text(encoding="utf-8"))
            runtime_abi = runtime["target"]["abi"]
            runtime_python = runtime["cpython"]["version"]
            runtime_ndk = runtime["toolchain"]["ndk_version"]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise AndroidSupportError(
                f"Android compatibility Python manifest is invalid: {runtime_manifest}"
            ) from exc
        if (
            runtime.get("$schema") != "infernux.android_python_runtime"
            or runtime.get("kind") != "infernux-android-cpython"
            or runtime_abi != abi
            or not str(runtime_python).startswith(ANDROID_PYTHON_SERIES + ".")
            or runtime_ndk != ANDROID_PYTHON_NDK
        ):
            raise AndroidSupportError(
                f"Android compatibility Python runtime is incompatible: {runtime_manifest}"
            )
    return document


def android_support_environment(root: Path) -> dict[str, str]:
    document = validate_android_support(root)
    paths = document["paths"]
    assert isinstance(paths, Mapping)
    python_paths = paths["python"]
    assert isinstance(python_paths, Mapping)
    sdk = root / _portable_relative(paths["sdk"], label="SDK")
    return {
        "INFERNUX_ANDROID_SUPPORT_ROOT": str(root),
        "ANDROID_SDK_ROOT": str(sdk),
        "ANDROID_HOME": str(sdk),
        "JAVA_HOME": str(root / _portable_relative(paths["jdk"], label="JDK")),
        "INFERNUX_GRADLE_HOME": str(
            root / _portable_relative(paths["gradle"], label="Gradle")
        ),
        "INFERNUX_ANDROID_PYTHON_PREFIX_ARM64": str(
            root / _portable_relative(python_paths["arm64-v8a"], label="Python arm64")
        ),
        "INFERNUX_ANDROID_PYTHON_PREFIX_X86_64": str(
            root / _portable_relative(python_paths["x86_64"], label="Python x86_64")
        ),
    }


class AndroidSupportManager:
    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root else default_android_support_root()

    def status(self) -> AndroidSupportStatus:
        if not self.root.exists():
            return AndroidSupportStatus(False, self.root)
        try:
            document = validate_android_support(self.root)
        except AndroidSupportError as exc:
            return AndroidSupportStatus(False, self.root, error=str(exc))
        return AndroidSupportStatus(
            True,
            self.root,
            version=str(document["version"]),
            total_bytes=int(document["total_bytes"]),
        )

    def activate_environment(self) -> bool:
        status = self.status()
        if not status.installed:
            return False
        os.environ.update(android_support_environment(self.root))
        return True

    def install(
        self,
        *,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> str:
        url, expected_sha256, expected_size = self._release_asset()
        self.root.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix="android-support-", suffix=".inxkit", dir=self.root.parent
        )
        os.close(fd)
        try:
            digest = hashlib.sha256()
            downloaded = 0
            # Bound each connection for GB-sized channel assets. Integrity is
            # checked once for the complete file; failed ranges are not retried.
            with open(temporary, "wb") as stream:
                while downloaded < expected_size:
                    end = min(downloaded + 32 * 1024 * 1024, expected_size) - 1
                    request = urllib.request.Request(
                        url,
                        headers={
                            "Accept": "application/octet-stream",
                            "User-Agent": "Infernux-Hub/1.0",
                            "Range": f"bytes={downloaded}-{end}",
                        },
                    )
                    with urllib.request.urlopen(request, timeout=120) as response:
                        expected_range = f"bytes {downloaded}-{end}/{expected_size}"
                        if (
                            response.status != 206
                            or response.headers.get("Content-Range") != expected_range
                        ):
                            raise AndroidSupportError(
                                "Android compatibility server returned an invalid download range"
                            )
                        while downloaded <= end:
                            block = response.read(min(1024 * 1024, end + 1 - downloaded))
                            if not block:
                                raise AndroidSupportError(
                                    "Incomplete Android compatibility download: "
                                    f"received {downloaded}/{expected_size} bytes"
                                )
                            stream.write(block)
                            digest.update(block)
                            downloaded += len(block)
                            if on_progress is not None:
                                on_progress(downloaded, expected_size)
            actual_sha256 = digest.hexdigest()
            if actual_sha256 != expected_sha256:
                raise AndroidSupportError(
                    "Downloaded Android compatibility archive does not match its release digest: "
                    f"received {downloaded}/{expected_size} bytes; SHA-256 {actual_sha256}"
                )
            return self.install_archive(temporary)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def install_archive(self, archive: str | os.PathLike[str]) -> str:
        source = Path(archive).expanduser().resolve()
        if not source.is_file() or not zipfile.is_zipfile(source):
            raise AndroidSupportError(f"Android compatibility archive is invalid: {source}")
        self.root.parent.mkdir(parents=True, exist_ok=True)
        staging = self.root.parent / f".{self.root.name}.staging-{uuid.uuid4().hex}"
        backup = self.root.parent / f".{self.root.name}.backup-{uuid.uuid4().hex}"
        try:
            staging.mkdir()
            with zipfile.ZipFile(source) as bundle:
                for member in bundle.infolist():
                    relative = _portable_relative(member.filename, label="archive member")
                    if stat.S_ISLNK(member.external_attr >> 16):
                        raise AndroidSupportError(
                            f"Android compatibility archive contains a symbolic link: {member.filename}"
                        )
                    destination = (staging / relative).resolve()
                    if staging not in destination.parents and destination != staging:
                        raise AndroidSupportError(
                            f"Android compatibility archive escapes its root: {member.filename}"
                        )
                    if member.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(member) as reader, open(destination, "wb") as writer:
                        shutil.copyfileobj(reader, writer)
                    if os.name == "posix" and member.create_system == 3:
                        # Release archives carry the host tool executable bits.
                        # Keep ordinary permissions, never setuid/setgid/sticky.
                        destination.chmod((member.external_attr >> 16) & 0o777)
            validate_android_support(staging)
            if self.root.exists():
                os.replace(self.root, backup)
            try:
                os.replace(staging, self.root)
            except BaseException:
                if backup.exists() and not self.root.exists():
                    os.replace(backup, self.root)
                raise
            if backup.exists():
                shutil.rmtree(backup)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
            if backup.exists() and self.root.exists():
                shutil.rmtree(backup)
        self.activate_environment()
        return str(self.root)

    @staticmethod
    def _release_asset() -> tuple[str, str, int]:
        request = urllib.request.Request(
            GITHUB_RELEASES_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "Infernux-Hub/1.0",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                releases = json.load(response)
        except Exception as exc:
            raise AndroidSupportError(
                "The Android compatibility release catalog could not be reached"
            ) from exc
        expected_name = archive_name()
        for release in releases if isinstance(releases, list) else ():
            if not isinstance(release, Mapping):
                continue
            for asset in release.get("assets", ()):
                if not isinstance(asset, Mapping) or asset.get("name") != expected_name:
                    continue
                digest = str(asset.get("digest", ""))
                url = str(asset.get("browser_download_url", ""))
                size = asset.get("size")
                if (
                    not re.fullmatch(r"sha256:[0-9a-f]{64}", digest)
                    or not url.startswith("https://")
                    or type(size) is not int
                    or size <= 0
                ):
                    raise AndroidSupportError(
                        f"Android compatibility release asset is missing immutable provenance: {expected_name}"
                    )
                return url, digest.removeprefix("sha256:"), size
        raise AndroidSupportError(
            f"No compatible Android support asset is published: {expected_name}"
        )


__all__ = [
    "ANDROID_SUPPORT_VERSION",
    "AndroidSupportError",
    "AndroidSupportManager",
    "AndroidSupportStatus",
    "android_support_environment",
    "archive_name",
    "create_android_support_manifest",
    "default_android_support_root",
    "host_id",
    "validate_android_support",
]
