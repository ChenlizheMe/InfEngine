"""EditorServices — centralized service locator for editor subsystems.

Example inside an EditorPanel::

    engine = self.services.engine
    undo   = self.services.undo_manager
    path   = self.services.project_path
"""

from __future__ import annotations

from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from Infernux.engine.engine import Engine
    from Infernux.engine.undo import UndoManager
    from Infernux.engine.scene_manager import SceneFileManager
    from Infernux.engine.play_mode import PlayModeManager
    from Infernux.engine.interaction import EditorInteractionCore
    from Infernux.engine.ui.window_manager import WindowManager


class EditorServices:
    """Singleton service locator for the editor."""

    def __init__(self) -> None: ...

    @classmethod
    def instance(cls) -> EditorServices:
        """Return the singleton (creates one if needed)."""
        ...

    @property
    def engine(self) -> Optional[Engine]:
        """The Python :class:`Engine` wrapper."""
        ...

    @property
    def native_engine(self) -> object:
        """The underlying C++ ``Infernux`` instance."""
        ...

    @property
    def undo_manager(self) -> Optional[UndoManager]: ...

    @property
    def scene_file_manager(self) -> Optional[SceneFileManager]: ...

    @property
    def play_mode_manager(self) -> Optional[PlayModeManager]: ...

    @property
    def window_manager(self) -> Optional[WindowManager]: ...

    @property
    def interaction_core(self) -> Optional[EditorInteractionCore]: ...

    @property
    def document_registry(self) -> object: ...

    @property
    def asset_database(self) -> object:
        """The C++ ``AssetDatabase`` instance (or ``None``)."""
        ...

    @property
    def project_path(self) -> Optional[str]:
        """Absolute path to the open project root."""
        ...
