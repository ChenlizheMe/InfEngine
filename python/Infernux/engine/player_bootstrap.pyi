"""PlayerBootstrap — standalone player initialization.

Handles engine init, scene loading, camera setup, and play-mode entry
for headless (no-editor) player builds.

Invoked via :func:`Infernux.engine.run_player`.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from Infernux.engine.engine import Engine, LogLevel
from Infernux.engine.player_service_graph import (
    PlayerRuntimeAssetCatalog,
    RuntimeProductManifest,
)


class PlayerBootstrap:
    """Orchestrates the standalone player startup sequence."""

    engine: Optional[Engine]
    project_path: str
    engine_log_level: LogLevel
    display_mode: str
    window_width: int
    window_height: int
    splash_items: list
    runtime_session: Optional[object]
    _runtime_manifest: Optional[RuntimeProductManifest]
    _runtime_catalog: Optional[PlayerRuntimeAssetCatalog]

    def __init__(
        self,
        project_path: str,
        engine_log_level: LogLevel = ...,
        display_mode: str = "fullscreen_borderless",
        window_width: int = 1920,
        window_height: int = 1080,
        splash_items: Optional[List[Dict]] = None,
    ) -> None: ...

    def run(self) -> None:
        """Execute all player bootstrap phases."""
        ...
