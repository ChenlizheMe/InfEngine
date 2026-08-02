"""project_file_ops — create / delete / rename project assets.

Usage::

    from Infernux.engine.ui.project_file_ops import (
        create_script, create_material, delete_item, do_rename,
    )
"""

from __future__ import annotations

from typing import Optional


# ── Template strings ────────────────────────────────────────────────

SCRIPT_TEMPLATE: str
VERTEX_SHADER_TEMPLATE: str
FRAGMENT_SHADER_TEMPLATE: str
SCENE_TEMPLATE: str
MATERIAL_TEMPLATE: str


# ── Public API ──────────────────────────────────────────────────────

def get_unique_name(
    current_path: str, base_name: str, extension: str = "",
) -> str:
    """Return a filename that doesn't collide with existing entries."""
    ...

def create_folder(current_path: str, folder_name: str) -> None: ...
def create_script(
    current_path: str,
    script_name: str,
    asset_database: Optional[object] = None,
) -> None: ...
def create_shader(
    current_path: str,
    shader_name: str,
    shader_type: str,
    asset_database: Optional[object] = None,
) -> None: ...
def create_scene(
    current_path: str,
    scene_name: str,
    asset_database: Optional[object] = None,
) -> None: ...
def create_material(
    current_path: str,
    material_name: str,
    asset_database: Optional[object] = None,
) -> None: ...
def create_prefab_from_gameobject(
    game_object: object,
    current_path: str,
    asset_database: Optional[object] = None,
) -> None: ...
def delete_item(
    item_path: str, asset_database: Optional[object] = None,
) -> bool: ...
def do_rename(
    old_path: str,
    new_name: str,
    asset_database: Optional[object] = None,
) -> None: ...

def move_path(
    old_path: str,
    new_path: str,
    asset_database: Optional[object] = None,
) -> None: ...

def move_item_to_directory(
    item_path: str,
    dest_dir: str,
    asset_database: Optional[object] = None,
) -> None: ...
