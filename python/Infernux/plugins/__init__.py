"""Public API for Infernux project packages and preload lifecycles."""

from Infernux.lifecycle import InxPreload, PreloadContext
from .content import (
    PLUGIN_PAGES_DIRECTORY,
    localized_intro,
    markdown_to_plain_text,
    parse_markdown_blocks,
    resolve_plugin_page_asset,
    split_markdown_images,
)
from .manager import PackageConflictError, PluginManager, PluginState
from .cache import SharedPackageCache, package_cache_root
from .package import (
    InxPackage,
    InxPackagePreview,
    normalize_player_rules,
    player_file_exported,
)
from .registry import PluginRegistry
from .platform_support import plugin_install_block_reason

__all__ = [
    "InxPackage",
    "InxPackagePreview",
    "InxPreload",
    "PLUGIN_PAGES_DIRECTORY",
    "PackageConflictError",
    "SharedPackageCache",
    "PreloadContext",
    "PluginManager",
    "PluginRegistry",
    "PluginState",
    "package_cache_root",
    "markdown_to_plain_text",
    "localized_intro",
    "parse_markdown_blocks",
    "resolve_plugin_page_asset",
    "split_markdown_images",
    "normalize_player_rules",
    "player_file_exported",
    "plugin_install_block_reason",
]
