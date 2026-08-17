from __future__ import annotations

from typing import ClassVar, Union, Optional, List

from Infernux.lib import TagLayerManager as TagLayerManager


class GameObjectQuery:
    """Static methods for finding GameObjects in the active scene."""

    @staticmethod
    def find(name: str) -> Optional[object]:
        """Find a GameObject by name."""
        ...
    @staticmethod
    def find_with_tag(tag: str) -> Optional[object]:
        """Find the first GameObject with the given tag."""
        ...
    @staticmethod
    def find_game_objects_with_tag(tag: str) -> list:
        """Find all GameObjects with the given tag."""
        ...
    @staticmethod
    def find_game_objects_in_layer(layer: int) -> list:
        """Find all GameObjects in the specified layer."""
        ...
    @staticmethod
    def find_by_id(object_id: int) -> Optional[object]:
        """Find a GameObject by its unique ID."""
        ...


class LayerMask:
    """Utility for working with layer-based filtering."""

    @staticmethod
    def get_mask(*layer_names: str) -> int:
        """Get a layer mask from one or more layer names."""
        ...
    @staticmethod
    def layer_to_name(layer: int) -> str:
        """Get the name of a layer by its index."""
        ...
    @staticmethod
    def name_to_layer(name: str) -> int:
        """Get the index of a layer by its name."""
        ...


class SceneManager:
    """Manages scene loading, unloading, and queries."""

    _pending_scene_load: Optional[str]
    active_scene: ClassVar[Optional[object]]

    @staticmethod
    def get_active_scene() -> Optional[object]:
        """Get the currently active scene."""
        ...
    @staticmethod
    def get_scene_by_name(name: str) -> Optional[str]:
        """Get a scene path by its name."""
        ...
    @staticmethod
    def get_scene_by_build_index(build_index: int) -> Optional[str]:
        """Get a scene path by its build index."""
        ...
    @staticmethod
    def get_scene_at(index: int) -> Optional[str]:
        """Get a scene path by its index in the scene list."""
        ...
    @staticmethod
    def load_scene(scene: Union[int, str]) -> bool:
        """Load a scene by file path or build index."""
        ...
    @staticmethod
    def wait_for_load_scene(scene: Union[int, str]) -> bool:
        """Prepare a scene asynchronously and switch when it is ready."""
        ...
    @staticmethod
    def prepare_scene(scene: Union[int, str]) -> bool:
        """Prepare a scene asynchronously without publishing it."""
        ...
    @staticmethod
    def is_scene_prepared() -> bool:
        """Return whether a held scene is ready to publish."""
        ...
    @staticmethod
    def activate_prepared_scene() -> bool:
        """Publish the scene previously prepared by prepare_scene."""
        ...
    @staticmethod
    def process_pending_load() -> None:
        """Process any pending scene load request."""
        ...
    @staticmethod
    def is_scene_load_pending() -> bool:
        """Return whether a deferred runtime scene load is queued or executing."""
        ...
    @staticmethod
    def get_scene_count() -> int:
        """Get the total number of scenes in the build."""
        ...
    @staticmethod
    def get_scene_name(build_index: int) -> Optional[str]:
        """Get a scene name by build index."""
        ...
    @staticmethod
    def get_scene_path(build_index: int) -> Optional[str]:
        """Get a scene file path by build index."""
        ...
    @staticmethod
    def get_build_index(name: str) -> int:
        """Get the build index of a scene by name."""
        ...
    @staticmethod
    def get_all_scene_names() -> List[str]:
        """Get a list of all scene names in the build."""
        ...
    @staticmethod
    def dont_destroy_on_load(game_object: object) -> None:
        """Mark a game object so it survives scene loads."""
        ...


__all__ = [
    "GameObjectQuery",
    "LayerMask",
    "TagLayerManager",
    "SceneManager",
]
