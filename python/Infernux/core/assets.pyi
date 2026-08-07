"""Type stubs for AssetManager."""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional, Type
from Infernux.lib import AssetMutationResult

_META_SUPPRESSION_TIMEOUT: float
_DEFAULT_DEBOUNCE_SEC: float


class AssetManager:
    """Python-side asset loading & caching manager (singleton pattern)."""

    @classmethod
    def initialize(cls, engine: Any) -> None:
        """Initialize the asset manager with the engine instance."""
        ...
    @classmethod
    def load(cls, path: str, asset_type: Optional[Type] = ...) -> Optional[Any]:
        """Load an asset from a file path."""
        ...
    @classmethod
    def load_by_guid(cls, guid: str, asset_type: Optional[Type] = ...) -> Optional[Any]:
        """Load an asset by its globally unique identifier."""
        ...
    @classmethod
    def find_assets(cls, pattern: str, asset_type: Optional[Type] = ...) -> List[str]:
        """Find asset paths matching a glob pattern."""
        ...
    @classmethod
    def invalidate(cls, guid: str) -> None:
        """Remove a cached asset by GUID, forcing reload on next access."""
        ...
    @classmethod
    def invalidate_path(cls, path: str) -> None:
        """Remove a cached asset by path, forcing reload on next access."""
        ...
    @classmethod
    def flush(cls) -> None:
        """Clear the entire asset cache."""
        ...
    @classmethod
    def register_import_strategy(cls, asset_category: str, apply_fn: Callable[[str, object], bool]) -> None:
        """Register a custom import strategy for an asset category."""
        ...
    @classmethod
    def register_save_strategy(cls, asset_category: str, save_fn: Callable[[object], object]) -> None:
        """Register a custom save strategy for an asset category."""
        ...
    @classmethod
    def apply_import_settings(cls, asset_category: str, path: str, settings_obj: Any) -> bool:
        """Apply import settings to an asset and reimport it."""
        ...
    @classmethod
    def import_asset(cls, path: str, *, database: Any = ..., suppress_watcher_echo: bool = ...) -> AssetMutationResult:
        """Import a new asset and publish its creation."""
        ...
    @classmethod
    def reimport_asset(cls, path: str, *, database: Any = ..., suppress_watcher_echo: bool = ...) -> AssetMutationResult:
        """Reimport an asset from disk."""
        ...
    @classmethod
    def move_asset(cls, old_path: str, new_path: str, *, database: Any = ...,
                     suppress_watcher_echo: bool = ..., origin: Any = ...,
                     operation_id: str = ..., publish_interaction: bool = ...) -> AssetMutationResult:
        """Move or rename an asset, updating all references."""
        ...
    @classmethod
    def move_assets_batch(cls, moves: Any, *, database: Any = ..., suppress_watcher_echo: bool = ...) -> Any: ...
    @classmethod
    def delete_asset(cls, path: str, *, database: Any = ..., suppress_watcher_echo: bool = ..., guid_hint: str = ...) -> AssetMutationResult:
        """Delete an asset after evicting loaded state."""
        ...
    @classmethod
    def is_meta_watcher_suppressed(cls, path: str) -> bool: ...
    @classmethod
    def is_watcher_echo_suppressed(cls, event_type: str, path: str, destination: str = ...) -> bool: ...
    @classmethod
    def invalidate_project_panel_cache(cls) -> None: ...
    @classmethod
    def schedule_save(cls, key: str, save_fn: Callable[[], object], debounce_sec: float = ...) -> None:
        """Schedule a debounced save operation."""
        ...
    @classmethod
    def schedule_asset_save(cls, asset_category: str, key: str, resource_obj: Any, debounce_sec: float = ...) -> None:
        """Schedule a debounced save for a specific asset."""
        ...
    @classmethod
    def cancel_scheduled_save(cls, key: str) -> bool: ...
    @classmethod
    def flush_scheduled_saves(cls, key: Optional[str] = ..., *, force: bool = ...) -> object:
        """Flush pending scheduled saves immediately."""
        ...
    @classmethod
    def poll_pending_asset_writes(cls) -> None: ...
    @classmethod
    def flush_all_asset_writes(cls) -> None: ...
    @classmethod
    def on_material_saved(cls, path: str) -> None:
        """Invalidate caches that depend on a material asset's file contents."""
        ...
