"""Public early-import lifecycle for project and package Python scripts."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable


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
