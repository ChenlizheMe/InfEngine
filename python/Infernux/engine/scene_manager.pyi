"""SceneFileManager — scene load / save / new / prefab-mode lifecycle.

Central authority for scene file operations: opening, saving, creating,
prefab editing mode, save confirmation dialogs, and camera state
persistence.

Example::

    sfm = SceneFileManager()
    sfm.set_asset_database(asset_db)
    sfm.set_engine(native_engine)
    sfm.open_scene("/path/to/scene.infscene")
    sfm.save_current_scene()
"""

from __future__ import annotations

from typing import Callable, Optional


class SceneFileManager:
    """Manages the active scene lifecycle (load, save, new, prefab mode)."""

    @classmethod
    def instance(cls) -> Optional[SceneFileManager]:
        """Return the singleton, or ``None`` if not yet created."""
        ...

    def __init__(self) -> None: ...

    def set_asset_database(self, asset_db: object) -> None: ...
    def set_engine(self, engine: object) -> None: ...

    @property
    def current_scene_path(self) -> Optional[str]:
        """Absolute path of the currently loaded scene, or ``None``."""
        ...

    @property
    def is_dirty(self) -> bool:
        """``True`` if the scene has unsaved modifications."""
        ...

    @property
    def is_loading(self) -> bool:
        """``True`` while a scene load operation is in progress."""
        ...

    def mark_dirty(self) -> None:
        """Mark the current scene as having unsaved changes."""
        ...

    def clear_dirty(self) -> None: ...

    def set_on_scene_changed(self, cb: Callable[[], None]) -> None:
        """Register a callback fired after a new scene finishes loading.

        Args:
            cb: Callable with no arguments.
        """
        ...

    def save_current_scene(self) -> bool:
        """Save the current scene to its existing path.

        Returns:
            ``True`` on success. ``False`` if no path is set (use ``save_scene_as``).
        """
        ...

    def save_scene_as(self) -> None:
        """Open a save-as dialog then save."""
        ...

    def open_scene(self, path: str) -> bool:
        """Open a scene from *path*, prompting to save if needed.

        Args:
            path: Absolute path to an ``.infscene`` file.
        """
        ...

    def new_scene(self) -> None:
        """Create a blank scene, prompting to save the current one first."""
        ...

    def request_close(self) -> None:
        """Request engine close, prompting to save if dirty."""
        ...

    def load_last_scene_or_default(self) -> None:
        """Load the most recently opened scene, or create a default."""
        ...

    def poll_pending_save(self) -> None:
        """Tick the save-confirmation popup state machine (per frame)."""
        ...

    def poll_deferred_load(self) -> None:
        """Tick deferred scene open/new operations (per frame)."""
        ...

    def get_display_name(self) -> str:
        """Human-readable name for the current scene (filename sans extension)."""
        ...

        """Render the "Save changes?" confirmation modal if active."""
        ...

    def open_prefab_mode(self, prefab_path: str) -> None:
        """Enter prefab editing mode for *prefab_path*."""
        ...

    def exit_prefab_mode(self) -> bool:
        """Leave prefab editing mode and return to the previous scene."""
        ...

    @property
    def is_prefab_mode(self) -> bool:
        """True when editing a prefab."""
        ...
    @property
    def prefab_mode_path(self) -> Optional[str]:
        """Path to the prefab being edited, or None."""
        ...
    @property
    def prefab_envelope(self) -> dict:
        """The serialized prefab envelope data."""
        ...

    def sync_all_prefab_instances(self, scene: object = ...) -> None:
        """Sync all prefab instances in the scene to their latest on-disk data."""
        ...
