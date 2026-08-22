"""Engine resource path constants.

Provides pre-resolved absolute paths to bundled assets (icons, fonts, etc.)
used by the editor and player at runtime.

Example::

    from Infernux.resources import icon_path, file_type_icons_dir
    print(icon_path)           # .../resources/icons/icon.png
    print(file_type_icons_dir) # .../resources/icons/
"""

from __future__ import annotations

icon_path: str
"""Absolute path to the engine window icon (``resources/icons/icon.png``)."""

engine_font_path: str
"""Absolute path to the default Simplified Chinese UI font (``PingFangSC-Regular.ttf``)."""

engine_lib_path: str
"""Absolute path to the native library directory (``lib/``)."""

resources_path: str
"""Absolute path to the ``resources/`` directory itself."""

file_type_icons_dir: str
"""Absolute path to ``resources/icons/`` — file-type icon PNGs for the project panel."""

component_icons_dir: str
"""Absolute path to ``resources/icons/components/`` — component icon PNGs for the inspector."""

__all__ = [
    "icon_path",
    "engine_font_path",
    "engine_lib_path",
    "resources_path",
    "file_type_icons_dir",
    "component_icons_dir",
]

def get_package_resources_path() -> str:
    """Return the absolute path to the package's resources directory."""
    ...

def activate_library(project_path: str) -> None:
    """Activate the engine library for the given project path."""
    ...
