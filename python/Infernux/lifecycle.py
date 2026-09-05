"""Public early-import lifecycle for project and package Python scripts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
import os
import re
from typing import Any, Callable

from Infernux.engine.path_utils import (
    is_path_within,
    portable_relative_path,
    resolved_path,
)


def _resolve_package_path(
    project_root: str,
    package_reference: str,
    relative_path: str | os.PathLike[str],
) -> str:
    if not project_root:
        raise RuntimeError("Package resources require an active project")
    raw_reference = str(package_reference).strip()
    reference = portable_relative_path(raw_reference)
    if (
        "\\" in raw_reference
        or (len(reference) >= 2 and reference[1] == ":")
        or any(
            not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", part)
            for part in reference.split("/")
        )
    ):
        raise ValueError("InxPackage reference is invalid")
    raw_relative = os.fspath(relative_path)
    relative = portable_relative_path(raw_relative)
    if "\0" in raw_relative or (len(relative) >= 2 and relative[1] == ":"):
        raise ValueError(f"Package path must be portable and relative: {relative_path!r}")
    package_root = resolved_path(
        os.path.join(project_root, "Packages", *reference.split("/"))
    )
    target = resolved_path(os.path.join(package_root, *relative.split("/")))
    if not is_path_within(target, package_root, allow_root=False):
        raise ValueError(f"Package path escapes its package root: {relative_path!r}")
    from Infernux.engine.project_context import resolve_asset_path

    resolved = resolve_asset_path(
        target, project_root=project_root, allow_directory=True
    )
    if resolved is None:
        raise FileNotFoundError(
            f"Package resource is not available at runtime: "
            f"{package_reference}/{relative}"
        )
    return resolved


@dataclass(frozen=True, slots=True)
class PreloadContext:
    project_root: str
    source_path: str
    script_guid: str
    type_id: str
    package_reference: str = ""
    engine: Any = None
    runtime: bool = False
    _restart_callback: Callable[[str], None] | None = None

    def package_path(self, relative_path: str | os.PathLike[str]) -> str:
        """Resolve one installed-package resource in Editor or Player.

        The path is relative to this package's root, for example
        ``runtime/server.jar``.  Player builds preserve that package-relative
        identity even when their content is staged or extracted elsewhere, so
        lifecycle scripts never need to infer a location from ``__file__``.
        """

        if not self.package_reference:
            raise RuntimeError(
                "PreloadContext.package_path() is only available to an installed package"
            )
        return _resolve_package_path(
            self.project_root,
            self.package_reference,
            relative_path,
        )

    def require_restart(self, reason: str) -> None:
        """Declare process state that cannot be safely undone by ``unload``."""

        if self._restart_callback is not None:
            self._restart_callback(str(reason or "Native state cannot be unloaded safely"))


class InxPreload(ABC):
    """Opt one script into early import and explicit startup/shutdown hooks."""

    @abstractmethod
    def preload(self, context: PreloadContext) -> None:
        """Initialize before scenes and normal project script consumers."""

    def unload(self) -> None:
        """Release process-local state before reload, disable, or shutdown."""


__all__ = ["InxPreload", "PreloadContext"]
