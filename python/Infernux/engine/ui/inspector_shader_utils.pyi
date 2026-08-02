"""inspector_shader_utils — shader file parsing and property sync.

Usage::

    from Infernux.engine.ui.inspector_shader_utils import (
        parse_shader_properties,
        get_shader_candidates,
        sync_properties_from_shader,
    )
"""

from __future__ import annotations

from typing import Dict, List, Optional


def bump_shader_property_generation() -> None:
    """Increment the global shader-property generation counter."""
    ...

def get_shader_property_generation() -> int:
    """Return the current shader-property generation counter."""
    ...

def parse_shader_id(filepath: str) -> str:
    """Extract the shader identifier from a ``.vert`` / ``.frag`` path."""
    ...

def parse_shader_properties(filepath: str) -> list:
    """Parse ``// [property …]`` annotations from a shader source file.

    Returns:
        List of property definition dicts.
    """
    ...

def is_shader_hidden(filepath: str) -> bool:
    """Return ``True`` if the shader is annotated as hidden."""
    ...

def get_shader_file_path(shader_id: str, ext: str) -> str:
    """Resolve a shader ID and extension to an absolute file path."""
    ...

def shader_ref_id(value: object) -> str:
    """Return the shader ID from a legacy string or structured reference."""
    ...

def make_shader_reference(value: object, ext: str) -> Dict[str, str]:
    """Create a canonical material shader asset reference."""
    ...

def shader_display_from_value(value: object, items: object) -> str:
    """Look up the human-readable display name for a shader value."""
    ...

def get_shader_candidates(
    ext: str, cache: Optional[Dict] = None,
) -> list:
    """Return a list of available shaders with the given extension.

    Args:
        ext: File extension (e.g. ``".frag"``).
        cache: Optional dict for caching between calls.
    """
    ...

def sync_properties_from_shader(
    mat_data: dict,
    shader_id: str,
    ext: str,
    remove_unknown: bool = False,
) -> None:
    """Synchronise material property keys with the shader's annotations."""
    ...

def get_material_property_display_order(mat_data: dict) -> List[str]:
    """Return the display-ordered list of property keys for a material."""
    ...

def sync_all_shader_properties(
    mat_data: dict,
    vert_shader_id: str,
    frag_shader_id: str,
    remove_unknown: bool = False,
) -> None:
    """Synchronise material properties with both vertex and fragment shader annotations."""
    ...

def get_all_shader_property_names(
    vert_shader_id: str,
    frag_shader_id: str,
) -> list:
    """Return all property names from both vertex and fragment shaders."""
    ...
