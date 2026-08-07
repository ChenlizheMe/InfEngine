"""
Unified asset execution layer for Inspector asset workflows.

This module centralizes how asset inspectors execute apply/revert/import/save
operations, while distinguishing two access modes:

- READ_ONLY_RESOURCE:
    Resource files are treated as source assets (e.g. Texture/Audio/Shader).
    User edits import metadata and then applies via reimport.

- READ_WRITE_RESOURCE:
    Asset data is directly editable and persisted (e.g. Material).
    Changes are autosaved with debounce.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from Infernux.core.assets import AssetManager


class AssetAccessMode(Enum):
    READ_ONLY_RESOURCE = "read_only_resource"
    READ_WRITE_RESOURCE = "read_write_resource"


class AssetExecutionLayer:
    """Shared execution policy for inspector asset operations."""

    def __init__(
        self,
        asset_category: str,
        file_path: str,
        access_mode: AssetAccessMode,
        autosave_debounce_sec: float = 0.35,
    ):
        self.asset_category = asset_category
        self.file_path = file_path
        self.access_mode = access_mode
        self._autosave_debounce_sec = autosave_debounce_sec

    def refresh_binding(self, asset_category: str, file_path: str):
        self.asset_category = asset_category
        self.file_path = file_path

    # ------------------------------------------------------------------
    # Read-only resource execution (import settings + reimport)
    # ------------------------------------------------------------------

    def apply_import_settings(self, settings_obj) -> bool:
        if self.access_mode != AssetAccessMode.READ_ONLY_RESOURCE:
            return False

        return AssetManager.apply_import_settings(self.asset_category, self.file_path, settings_obj)

    def reimport_asset(self):
        AssetManager.reimport_asset(self.file_path)

    def move_asset_path(self, new_path: str) -> bool:
        """Request one editor-owned, undoable workspace relocation.

        Asset inspectors must not mutate the native catalog directly. A path
        edit is the same user action as a Project-panel move and therefore goes
        through the shared command/transaction pipeline.
        """
        from Infernux.engine.interaction import ProjectAssetCommandService

        service = ProjectAssetCommandService.instance()
        if service is None or not service.configured:
            return False
        try:
            self.file_path = service.move(self.file_path, new_path)
        except (OSError, RuntimeError, ValueError):
            return False
        return True

    # ------------------------------------------------------------------
    # Read-write resource execution (autosave)
    # ------------------------------------------------------------------

    def schedule_rw_save(self, resource_obj):
        if self.access_mode != AssetAccessMode.READ_WRITE_RESOURCE:
            return
        AssetManager.schedule_asset_save(
            self.asset_category,
            self.file_path,
            resource_obj,
            debounce_sec=self._autosave_debounce_sec,
        )

    def flush_rw_autosave(self, *, force: bool = False):
        if self.access_mode != AssetAccessMode.READ_WRITE_RESOURCE:
            return False
        return AssetManager.flush_scheduled_saves(self.file_path, force=force)

    def cancel_rw_autosave(self) -> bool:
        if self.access_mode != AssetAccessMode.READ_WRITE_RESOURCE:
            return False
        return AssetManager.cancel_scheduled_save(self.file_path)


def get_asset_execution_layer(
    current_layer: Optional[AssetExecutionLayer],
    asset_category: str,
    file_path: str,
    access_mode: AssetAccessMode,
    autosave_debounce_sec: float = 0.35,
) -> AssetExecutionLayer:
    """Reuse existing layer when file/mode match, else create a new one."""
    if current_layer is not None:
        if (
            current_layer.file_path == file_path
            and current_layer.asset_category == asset_category
            and current_layer.access_mode == access_mode
        ):
            current_layer.refresh_binding(asset_category, file_path)
            return current_layer
    return AssetExecutionLayer(asset_category, file_path, access_mode, autosave_debounce_sec=autosave_debounce_sec)
