"""Hub-owned, content-addressed storage for immutable InxPackage blobs."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path

from Infernux.engine.path_utils import resolved_path

from .package import PACKAGE_EXTENSION


PACKAGE_CACHE_ROOT_ENV = "INFERNUX_PACKAGE_CACHE_ROOT"


def package_cache_root() -> str:
    """Return the global cache shared by Hub, Editors, and projects."""

    configured = os.environ.get(PACKAGE_CACHE_ROOT_ENV, "").strip()
    if configured:
        return resolved_path(os.path.expandvars(os.path.expanduser(configured)))
    return resolved_path(Path.home() / ".infernux" / "packages")


def package_file_sha256(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class PackageBlobCache:
    """Store each verified package payload once, keyed by SHA-256."""

    def __init__(self, root: str | os.PathLike[str] | None = None) -> None:
        self.root = resolved_path(root or package_cache_root())
        self.blob_root = resolved_path(os.path.join(self.root, "blobs", "sha256"))

    @staticmethod
    def validate_digest(digest: str) -> str:
        value = str(digest or "").strip().casefold()
        if len(value) != 64 or any(
            character not in "0123456789abcdef" for character in value
        ):
            raise ValueError("InxPackage cache digest must be a SHA-256 hex value")
        return value

    def relative_path(self, digest: str) -> str:
        value = self.validate_digest(digest)
        return f"blobs/sha256/{value}{PACKAGE_EXTENSION}"

    def path(self, digest: str) -> str:
        return resolved_path(
            os.path.join(self.root, *self.relative_path(digest).split("/"))
        )

    def resolve(self, digest: str) -> str:
        destination = self.path(digest)
        if not os.path.isfile(destination):
            return ""
        if package_file_sha256(destination) != self.validate_digest(digest):
            return ""
        return destination

    def store(self, source: str, *, digest: str | None = None) -> str:
        source_path = resolved_path(source)
        expected = self.validate_digest(digest or package_file_sha256(source_path))
        if package_file_sha256(source_path) != expected:
            raise RuntimeError("InxPackage source hash mismatch")
        current = self.resolve(expected)
        if current:
            return current

        destination = self.path(expected)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        temporary = destination + f".tmp.{uuid.uuid4().hex}"
        try:
            shutil.copy2(source_path, temporary)
            if package_file_sha256(temporary) != expected:
                raise RuntimeError("InxPackage cache copy hash mismatch")
            os.replace(temporary, destination)
        finally:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass
        return destination


__all__ = [
    "PACKAGE_CACHE_ROOT_ENV",
    "PackageBlobCache",
    "package_cache_root",
    "package_file_sha256",
]
