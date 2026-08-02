"""Type stubs for Infernux.core.asset_types."""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from typing import Any, Dict, FrozenSet, Optional


# ── Enums ──────────────────────────────────────────────────────────────

class TextureType(IntEnum):
    """Type classification for texture assets."""
    DEFAULT = 0
    NORMAL_MAP = 1
    UI = 2
    SPRITE = 3
    DATA = 4


class TextureCompression(IntEnum):
    NONE = 0
    AUTO = 1
    BC1 = 2
    BC3 = 3
    BC4 = 4
    BC5 = 5
    @classmethod
    def from_string(cls, value: str) -> TextureCompression: ...
    def to_string(self) -> str: ...


class TextureCompressionQuality(IntEnum):
    FAST = 0
    NORMAL = 1
    HIGH = 2
    @classmethod
    def from_string(cls, value: str) -> TextureCompressionQuality: ...
    def to_string(self) -> str: ...


class TextureFormat(IntEnum):
    AUTO = 0
    RGBA8 = 1
    RGBA4444 = 2
    RGBA16_UNORM = 3
    RGBA16_FLOAT = 4
    RGBA32_FLOAT = 5
    @classmethod
    def from_string(cls, value: str) -> TextureFormat: ...
    def to_string(self) -> str: ...


class WrapMode(IntEnum):
    """Texture edge wrapping mode."""
    REPEAT = 0
    CLAMP = 1
    MIRROR = 2
    @classmethod
    def from_string(cls, s: str) -> WrapMode:
        """Parse a wrap mode from its string name."""
        ...
    def to_string(self) -> str:
        """Return the string name of this wrap mode."""
        ...


class FilterMode(IntEnum):
    """Texture sampling filter mode."""
    POINT = 0
    BILINEAR = 1
    TRILINEAR = 2
    @classmethod
    def from_string(cls, s: str) -> FilterMode:
        """Parse a filter mode from its string name."""
        ...
    def to_string(self) -> str:
        """Return the string name of this filter mode."""
        ...


class AudioCompressionFormat(IntEnum):
    """Audio compression format for import settings."""
    PCM = 0
    VORBIS = 1
    ADPCM = 2


# ── Dataclasses ────────────────────────────────────────────────────────

@dataclass
class TextureImportSettings:
    """Unity-style texture import settings stored in .meta."""

    texture_type: TextureType = ...
    wrap_mode: WrapMode = ...
    filter_mode: FilterMode = ...
    generate_mipmaps: bool = ...
    srgb: bool = ...
    max_size: int = ...
    aniso_level: int = ...
    format: TextureFormat = ...
    compression: TextureCompression = ...
    compression_quality: TextureCompressionQuality = ...
    def to_dict(self) -> Dict[str, Any]:
        """Serialize settings to a dictionary."""
        ...
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> TextureImportSettings:
        """Create settings from a dictionary."""
        ...
    def copy(self) -> TextureImportSettings:
        """Create a shallow copy of these settings."""
        ...
    def __eq__(self, other: object) -> bool: ...


@dataclass
class ShaderAssetInfo:
    """Minimal shader asset model."""

    guid: str = ...
    source_path: str = ...
    shader_type: str = ...
    @classmethod
    def from_path(cls, path: str, guid: str = ...) -> ShaderAssetInfo:
        """Create shader asset info from a file path."""
        ...


@dataclass
class FontAssetInfo:
    """Minimal font asset model."""

    guid: str = ...
    source_path: str = ...
    font_type: str = ...
    @classmethod
    def from_path(cls, path: str, guid: str = ...) -> FontAssetInfo:
        """Create font asset info from a file path."""
        ...


@dataclass
class AudioImportSettings:
    """Unity-style audio import settings stored in .meta."""

    force_mono: bool = ...
    load_in_background: bool = ...
    quality: float = ...
    compression_format: AudioCompressionFormat = ...
    def to_dict(self) -> Dict[str, Any]:
        """Serialize settings to a dictionary."""
        ...
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> AudioImportSettings:
        """Create settings from a dictionary."""
        ...
    def copy(self) -> AudioImportSettings:
        """Create a shallow copy of these settings."""
        ...
    def __eq__(self, other: object) -> bool: ...


@dataclass
class MeshImportSettings:
    """Import settings for 3D model assets stored in .meta."""

    scale_factor: float = ...
    generate_normals: bool = ...
    generate_tangents: bool = ...
    flip_uvs: bool = ...
    optimize_mesh: bool = ...
    def to_dict(self) -> Dict[str, Any]:
        """Serialize settings to a dictionary."""
        ...
    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> MeshImportSettings:
        """Create settings from a dictionary."""
        ...
    def copy(self) -> MeshImportSettings:
        """Create a shallow copy of these settings."""
        ...
    def __eq__(self, other: object) -> bool: ...


# ── Extension sets ─────────────────────────────────────────────────────

IMAGE_EXTENSIONS: FrozenSet[str]
SHADER_EXTENSIONS: FrozenSet[str]
MATERIAL_EXTENSIONS: FrozenSet[str]
AUDIO_EXTENSIONS: FrozenSet[str]
FONT_EXTENSIONS: FrozenSet[str]
MESH_EXTENSIONS: FrozenSet[str]

# ── Meta-file utilities ────────────────────────────────────────────────

def read_meta_file(asset_path: str) -> Optional[Dict[str, Any]]:
    """Read and parse a .meta sidecar file for an asset."""
    ...
def write_meta_fields(asset_path: str, updates: Dict[str, Any]) -> bool:
    """Write updated fields to an asset's .meta file."""
    ...

# ── Import settings read/write ─────────────────────────────────────────

def read_texture_import_settings(asset_path: str) -> TextureImportSettings:
    """Read texture import settings from the asset's .meta file."""
    ...
def write_texture_import_settings(asset_path: str, settings: TextureImportSettings) -> bool:
    """Write texture import settings to the asset's .meta file."""
    ...
def read_audio_import_settings(asset_path: str) -> AudioImportSettings:
    """Read audio import settings from the asset's .meta file."""
    ...
def write_audio_import_settings(asset_path: str, settings: AudioImportSettings) -> bool:
    """Write audio import settings to the asset's .meta file."""
    ...
def read_mesh_import_settings(asset_path: str) -> MeshImportSettings:
    """Read mesh import settings from the asset's .meta file."""
    ...
def write_mesh_import_settings(asset_path: str, settings: MeshImportSettings) -> bool:
    """Write mesh import settings to the asset's .meta file."""
    ...

# ── Extension → category mapping ──────────────────────────────────────

def asset_category_from_extension(ext: str) -> Optional[str]:
    """Return the asset category string for a file extension."""
    ...
