"""Versioned Python runtime identities and download catalog for Infernux Hub."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping


_VERSION_PATTERN = re.compile(r"^\s*(\d+)\.(\d+)(?:\.(\d+))?\s*$")


@dataclass(frozen=True, order=True)
class PythonRuntimeId:
    """A Python ABI identity at the major/minor boundary."""

    major: int
    minor: int

    @classmethod
    def parse(cls, value: str | "PythonRuntimeId") -> "PythonRuntimeId":
        if isinstance(value, cls):
            return value
        match = _VERSION_PATTERN.fullmatch(str(value or ""))
        if match is None:
            raise ValueError(
                f"Invalid Python runtime version {value!r}; expected major.minor."
            )
        return cls(int(match.group(1)), int(match.group(2)))

    @property
    def series(self) -> str:
        return f"{self.major}.{self.minor}"

    @property
    def directory_name(self) -> str:
        return f"python{self.major}{self.minor}"

    @property
    def cp_tag(self) -> str:
        return f"cp{self.major}{self.minor}"

    @property
    def unix_library_stem(self) -> str:
        return f"python{self.series}"

    @property
    def windows_library_stem(self) -> str:
        return f"python{self.major}{self.minor}"


@dataclass(frozen=True)
class PythonRuntimeRelease:
    runtime_id: PythonRuntimeId
    patch_version: str
    build_release: str
    archive_sha256: Mapping[str, str]

    def __post_init__(self) -> None:
        match = _VERSION_PATTERN.fullmatch(self.patch_version)
        if match is None or match.group(3) is None:
            raise ValueError(
                f"Runtime patch version must be major.minor.patch: {self.patch_version!r}"
            )
        if PythonRuntimeId(int(match.group(1)), int(match.group(2))) != self.runtime_id:
            raise ValueError(
                f"Runtime {self.runtime_id.series} cannot use patch {self.patch_version}."
            )


_PYTHON_312 = PythonRuntimeRelease(
    runtime_id=PythonRuntimeId(3, 12),
    patch_version="3.12.13",
    build_release="20260805",
    archive_sha256={
        "x86_64-pc-windows-msvc": "d731ce7dddcfad4a9521aac48626ca06326003fe4771a366e0fce6eb58709451",
        "i686-pc-windows-msvc": "8ba10b61abc62e2f6ec0863d8496c077c4431e8621a812e0bd3f8cae8cd5dbec",
        "aarch64-pc-windows-msvc": "78fbbffa040de2dd6e4c97001103cacf5770743c02b2493ea9eda711ea41743c",
        "x86_64-apple-darwin": "718a89c781a7fb0a8cf7cd37c8cad0f91968438493285aa878f51228dcc9c7ed",
        "aarch64-apple-darwin": "b8caf71c009e95507a306ba7ff18335e840b678d23b4d79ec026527553a99e5d",
        "x86_64-unknown-linux-gnu": "919043a06d8136147b24077c3bb32ec058e66c586ce5465b0f0eb018f242a655",
        "aarch64-unknown-linux-gnu": "c2083943c86dbb21ca0211238362fd922de7b0475688f26c135cf5d20a1c2f48",
    },
)

_PYTHON_313 = PythonRuntimeRelease(
    runtime_id=PythonRuntimeId(3, 13),
    patch_version="3.13.15",
    build_release="20260825",
    archive_sha256={
        "x86_64-pc-windows-msvc": "82a792c25550a421b29f381eaeafa6dccd1ffcbd97a1b1507b202f5df877cecf",
        "i686-pc-windows-msvc": "c910aee31e4f729b93c8f8f1a03097ff88450b7e46b465232a307ce6c0382f63",
        "aarch64-pc-windows-msvc": "b15a161f9431eabbe4b9f445752aed3572260b011c10b858962f0f2508078fa8",
        "x86_64-apple-darwin": "40eb292bb37f32639b1eb5736bef702081a2151eda1bb4e6171345a157babfa6",
        "aarch64-apple-darwin": "d681f7cebf4885637242cba807d22f476b9ea8555ac2dc7307172426dbf161e1",
        "x86_64-unknown-linux-gnu": "8a70011ae25276a9925f89304cdc086466cd269ee6cfe68a9506694ca5ff4f9c",
        "aarch64-unknown-linux-gnu": "b298e34164582305be9629a0da50701358195ce30b639f5ed4bbc50c4768f048",
    },
)


DEFAULT_PYTHON_RUNTIME = PythonRuntimeId(3, 13)
SUPPORTED_PYTHON_RUNTIMES: tuple[PythonRuntimeId, ...] = (
    DEFAULT_PYTHON_RUNTIME,
    PythonRuntimeId(3, 12),
)
_RELEASES = {
    release.runtime_id: release for release in (_PYTHON_313, _PYTHON_312)
}


def runtime_release(
    runtime: str | PythonRuntimeId = DEFAULT_PYTHON_RUNTIME,
) -> PythonRuntimeRelease:
    runtime_id = PythonRuntimeId.parse(runtime)
    try:
        return _RELEASES[runtime_id]
    except KeyError as exc:
        supported = ", ".join(item.series for item in SUPPORTED_PYTHON_RUNTIMES)
        raise ValueError(
            f"Python {runtime_id.series} is not in the Hub runtime catalog. "
            f"Supported versions: {supported}."
        ) from exc


def runtime_directory_name(
    runtime: str | PythonRuntimeId = DEFAULT_PYTHON_RUNTIME,
) -> str:
    return PythonRuntimeId.parse(runtime).directory_name


__all__ = [
    "DEFAULT_PYTHON_RUNTIME",
    "PythonRuntimeId",
    "PythonRuntimeRelease",
    "SUPPORTED_PYTHON_RUNTIMES",
    "runtime_directory_name",
    "runtime_release",
]
