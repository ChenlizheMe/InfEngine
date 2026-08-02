"""
EditorServices — centralized service locator for editor subsystems.

Provides a single access point for all core editor services (engine,
undo, scene management, asset database, etc.) so that panels and user
scripts can access them without manual injection.

Usage inside an EditorPanel::

    class MyPanel(EditorPanel):
        def on_render_content(self, ctx):
            engine = self.services.engine
            undo   = self.services.undo_manager
            scene  = self.services.native_engine
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from Infernux.engine.engine import Engine
    from Infernux.engine.undo import UndoManager
    from Infernux.engine.scene_manager import SceneFileManager
    from Infernux.engine.play_mode import PlayModeManager
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.ui.window_manager import WindowManager


class EditorServices:
    """Singleton service locator for the editor.

    Created once in ``release_engine()`` and populated with references to
    all core subsystems.  Panels access it via ``EditorServices.instance()``
    or, more conveniently, ``self.services`` on :class:`EditorPanel`.
    """

    _instance: Optional[EditorServices] = None

    def __init__(self) -> None:
        self._engine: Optional[Engine] = None
        self._undo_manager: Optional[UndoManager] = None
        self._scene_file_manager: Optional[SceneFileManager] = None
        self._play_mode_manager: Optional[PlayModeManager] = None
        self._window_manager: Optional[WindowManager] = None
        self._interaction_core: Optional[EditorInteractionCore] = None
        self._asset_database = None  # C++ AssetDatabase
        self._project_path: Optional[str] = None
        EditorServices._instance = self

    # ------------------------------------------------------------------
    # Singleton access
    # ------------------------------------------------------------------

    @classmethod
    def instance(cls) -> EditorServices:
        """Return the singleton.  Creates one if needed (for import-time safety)."""
        if cls._instance is None:
            cls()
        return cls._instance  # type: ignore[return-value]

    # ------------------------------------------------------------------
    # Read-only properties
    # ------------------------------------------------------------------

    @property
    def engine(self) -> Optional[Engine]:
        """The Python :class:`Engine` wrapper."""
        return self._engine

    @property
    def native_engine(self):
        """The underlying C++ ``Infernux`` instance."""
        return self._engine.get_native_engine() if self._engine else None

    @property
    def undo_manager(self) -> Optional[UndoManager]:
        return self._undo_manager

    @property
    def scene_file_manager(self) -> Optional[SceneFileManager]:
        return self._scene_file_manager

    @property
    def play_mode_manager(self) -> Optional[PlayModeManager]:
        return self._play_mode_manager

    @property
    def window_manager(self) -> Optional[WindowManager]:
        return self._window_manager

    @property
    def interaction_core(self) -> Optional[EditorInteractionCore]:
        return self._interaction_core

    @property
    def asset_database(self):
        """The C++ ``AssetDatabase`` instance (or *None*)."""
        return self._asset_database

    @property
    def project_path(self) -> Optional[str]:
        """Absolute path to the open project root."""
        return self._project_path
