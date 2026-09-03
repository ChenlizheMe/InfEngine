"""Hub-owned shared storage for downloaded InxPackage versions."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping
from urllib.parse import quote

from Infernux.engine.path_utils import resolved_path, same_path

from .package import PACKAGE_EXTENSION


_STAGING_NAME = re.compile(r"^[a-z0-9-]+-(\d+)-[a-z0-9_]+$")
PACKAGE_CACHE_ROOT_ENV = "INFERNUX_PACKAGE_CACHE_ROOT"
DATA_ROOT_ENV = "INFERNUX_DATA_ROOT"


def package_cache_root() -> str:
    """Return the Hub library shared by every Editor project."""

    configured = os.environ.get(PACKAGE_CACHE_ROOT_ENV, "").strip()
    if configured:
        return resolved_path(os.path.expandvars(os.path.expanduser(configured)))
    data_root = os.environ.get(DATA_ROOT_ENV, "").strip()
    if data_root:
        return resolved_path(
            os.path.join(
                os.path.expandvars(os.path.expanduser(data_root)),
                "Library",
                "Plugins",
            )
        )
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        if not local_app_data:
            raise RuntimeError("The Hub plugin library requires LOCALAPPDATA")
        data_root = Path(local_app_data)
    else:
        xdg_data_home = os.environ.get("XDG_DATA_HOME", "").strip()
        data_root = (
            Path(xdg_data_home).expanduser()
            if xdg_data_home
            else Path.home() / ".local" / "share"
        )
    return resolved_path(data_root / "InfernuxHub" / "Library" / "Plugins")


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _encoded_segment(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not normalized or normalized in {".", ".."}:
        raise ValueError(f"InxPackage cache {label} is invalid: {value!r}")
    return quote(normalized, safe="-._~")


class SharedPackageCache:
    """Store one package per reference/version without hashing the whole archive."""

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        staging_root: str | os.PathLike[str] | None = None,
    ) -> None:
        self.root = resolved_path(root or package_cache_root())
        self.package_root = resolved_path(os.path.join(self.root, "packages"))
        self.staging_root = resolved_path(
            staging_root or os.path.join(self.root, ".staging")
        )

    def _prune_staging(self) -> None:
        if not os.path.isdir(self.staging_root):
            return
        current_pid = os.getpid()
        for entry in Path(self.staging_root).iterdir():
            match = _STAGING_NAME.fullmatch(entry.name)
            if not match:
                continue
            owner_pid = int(match.group(1))
            if owner_pid == current_pid or _process_is_alive(owner_pid):
                continue
            if entry.is_dir():
                shutil.rmtree(entry)
            else:
                entry.unlink()

    @contextmanager
    def workspace(self, label: str):
        """Create one process-owned staging directory inside the shared cache."""

        normalized = str(label).strip().casefold().replace("_", "-")
        if not normalized or any(
            character not in "abcdefghijklmnopqrstuvwxyz0123456789-"
            for character in normalized
        ):
            raise ValueError(f"Invalid package staging label: {label!r}")
        os.makedirs(self.staging_root, exist_ok=True)
        self._prune_staging()
        workspace = tempfile.mkdtemp(
            prefix=f"{normalized}-{os.getpid()}-",
            dir=self.staging_root,
        )
        try:
            yield workspace
        finally:
            shutil.rmtree(workspace)

    @staticmethod
    def validate_location(location: str) -> str:
        value = str(location).replace("\\", "/").strip("/")
        path = PurePosixPath(value)
        if (
            not value
            or path.is_absolute()
            or not path.parts
            or path.parts[0] != "packages"
            or any(part in {"", ".", ".."} for part in path.parts)
            or path.suffix.casefold() != PACKAGE_EXTENSION
        ):
            raise ValueError(f"InxPackage cache location is invalid: {location!r}")
        return path.as_posix()

    def relative_path(self, reference: str, version: str) -> str:
        reference_parts = str(reference).split("/")
        if not reference_parts or any(not part.strip() for part in reference_parts):
            raise ValueError(f"InxPackage cache reference is invalid: {reference!r}")
        encoded_reference = "/".join(
            _encoded_segment(part, "reference") for part in reference_parts
        )
        encoded_version = _encoded_segment(version, "version")
        return f"packages/{encoded_reference}/{encoded_version}{PACKAGE_EXTENSION}"

    def path(self, reference: str, version: str) -> str:
        relative = self.relative_path(reference, version)
        return resolved_path(os.path.join(self.root, *relative.split("/")))

    def resolve(self, reference: str, version: str) -> str:
        destination = self.path(reference, version)
        return destination if os.path.isfile(destination) else ""

    def store(self, source: str, *, reference: str, version: str) -> str:
        source_path = resolved_path(source)
        destination = self.path(reference, version)
        if same_path(source_path, destination):
            return destination

        os.makedirs(os.path.dirname(destination), exist_ok=True)
        temporary = destination + f".tmp.{uuid.uuid4().hex}"
        try:
            shutil.copy2(source_path, temporary)
            os.replace(temporary, destination)
        finally:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass
        return destination

    @staticmethod
    def _project_references(project_root: str | os.PathLike[str]) -> set[str]:
        project = Path(resolved_path(project_root))
        if not project.is_dir():
            raise FileNotFoundError(
                f"Cannot clean the package cache while a registered project is unavailable: {project}"
            )
        registry_path = project / "ProjectSettings" / "InxPlugins.json"
        if not registry_path.is_file():
            return set()
        try:
            document = json.loads(registry_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(
                f"Cannot clean the package cache because a project registry is unreadable: {registry_path}"
            ) from exc
        if (
            not isinstance(document, Mapping)
            or document.get("$schema") != "infernux.plugin_registry"
            or not isinstance(document.get("packages"), list)
            or not isinstance(document.get("installed"), list)
        ):
            raise RuntimeError(
                f"Cannot clean the package cache because a project registry is invalid: {registry_path}"
            )
        locations: set[str] = set()
        for raw in [*document["packages"], *document["installed"]]:
            if not isinstance(raw, Mapping):
                raise RuntimeError(
                    f"Cannot clean the package cache because a package record is invalid: {registry_path}"
                )
            source = raw.get("source")
            if not isinstance(source, Mapping):
                continue
            if str(source.get("cache_scope", "")).casefold() != "hub":
                continue
            try:
                locations.add(
                    SharedPackageCache.validate_location(
                        str(source.get("cache_location", ""))
                    )
                )
            except ValueError as exc:
                raise RuntimeError(
                    f"Cannot clean the package cache because a project reference is invalid: {registry_path}"
                ) from exc
        return locations

    def prune_unreferenced(
        self,
        project_roots: Iterable[str | os.PathLike[str]],
        *,
        dry_run: bool = False,
    ) -> tuple[str, ...]:
        """Remove package versions unused by every explicitly registered project."""

        referenced: set[str] = set()
        for project_root in tuple(project_roots):
            referenced.update(self._project_references(project_root))

        candidates: list[Path] = []
        if os.path.isdir(self.package_root):
            for path in sorted(Path(self.package_root).rglob(f"*{PACKAGE_EXTENSION}")):
                relative = PurePosixPath(path.relative_to(self.root).as_posix()).as_posix()
                if relative not in referenced:
                    candidates.append(path)
        if not dry_run:
            for path in candidates:
                path.unlink()
        return tuple(str(path) for path in candidates)


__all__ = [
    "SharedPackageCache",
    "package_cache_root",
]
