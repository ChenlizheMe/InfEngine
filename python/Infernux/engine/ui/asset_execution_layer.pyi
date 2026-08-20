"""asset_execution_layer — asset loading / saving / reimporting layer."""

from __future__ import annotations

from enum import Enum


class AssetAccessMode(Enum):
    """How the inspector accesses the underlying asset."""

    READ_ONLY_RESOURCE = "read_only"
    READ_WRITE_RESOURCE = "read_write"


class AssetExecutionLayer:
    """Manages loading, saving, reimporting, and moving assets.

    Usage::

        layer = AssetExecutionLayer(cat, path, AssetAccessMode.READ_WRITE_RESOURCE)
        layer.schedule_rw_save(resource_obj)
    """

    def __init__(
        self,
        asset_category: object,
        file_path: str,
        access_mode: AssetAccessMode,
        autosave_debounce_sec: float = 0.35,
    ) -> None: ...

    asset_category: object
    file_path: str
    access_mode: AssetAccessMode
    def refresh_binding(self, asset_category: object, file_path: str) -> None: ...
    def apply_import_settings(self, settings_obj: object) -> bool: ...
    def reimport_asset(self) -> None: ...
    def move_asset_path(self, new_path: str) -> bool: ...
    def schedule_rw_save(self, resource_obj: object) -> None: ...
    def flush_rw_autosave(self, *, force: bool = ...) -> object: ...
    def cancel_rw_autosave(self) -> bool: ...


def get_asset_execution_layer(
    current_layer: object,
    asset_category: object,
    file_path: str,
    access_mode: AssetAccessMode,
    autosave_debounce_sec: float = 0.35,
) -> AssetExecutionLayer:
    """Retrieve or create an ``AssetExecutionLayer`` for the given asset."""
    ...
