import os

_package_dir = os.environ.get("_INFERNUX_PACKAGED_RESOURCE_ROOT") or os.path.dirname(
    os.path.abspath(__file__)
)

# Default to package directory; overridden by activate_library() at launch.
icon_path = os.path.join(_package_dir, "icons", "icon.png")
engine_font_path = os.path.join(_package_dir, "fonts", "PingFangSC-Regular.ttf")
engine_lib_path = os.path.join(_package_dir, "..", "lib")
resources_path = _package_dir
file_type_icons_dir = os.path.join(_package_dir, "icons")
component_icons_dir = os.path.join(_package_dir, "icons", "components")


def get_package_resources_path() -> str:
    """Return the original package resources directory (sync source)."""
    return _package_dir


def activate_library(project_path: str) -> None:
    """Redirect all resource paths to ``<project>/Library/Resources``."""
    root = os.path.join(project_path, "Library", "Resources")
    g = globals()
    g["resources_path"] = root
    g["icon_path"] = os.path.join(root, "icons", "icon.png")
    g["engine_font_path"] = os.path.join(root, "fonts", "PingFangSC-Regular.ttf")
    g["file_type_icons_dir"] = os.path.join(root, "icons")
    g["component_icons_dir"] = os.path.join(root, "icons", "components")


__all__ = [
    "icon_path",
    "engine_font_path",
    "engine_lib_path",
    "resources_path",
    "file_type_icons_dir",
    "component_icons_dir",
    "get_package_resources_path",
    "activate_library",
]
