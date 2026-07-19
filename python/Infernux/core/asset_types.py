"""
Unified asset data models for Material, Texture, and Shader.

Provides dataclass-based models for asset identity, texture import settings,
and shader asset info.  These are the "data contracts" shared between the
AssetManager, Inspector asset editors, and serialized-field references.
"""

from __future__ import annotations

import json
import math
import os
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any, Dict, List, Optional

# Shared IO thread pool for background file writes (meta, fallback saves).
# Max 2 workers: meta writes and material-save fallback are infrequent.
_io_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="asset-io")


# ═══════════════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════════════

class TextureType(IntEnum):
    DEFAULT = 0
    NORMAL_MAP = 1
    UI = 2
    SPRITE = 3
    DATA = 4
    VECTOR_FIELD = 5


class TextureCompression(IntEnum):
    NONE = 0
    AUTO = 1
    BC1 = 2
    BC3 = 3
    BC4 = 4
    BC5 = 5

    @classmethod
    def from_string(cls, value: str) -> "TextureCompression":
        return {
            "none": cls.NONE,
            "auto": cls.AUTO,
            "bc1": cls.BC1,
            "bc3": cls.BC3,
            "bc4": cls.BC4,
            "bc5": cls.BC5,
        }.get(str(value).lower(), cls.AUTO)

    def to_string(self) -> str:
        return ("none", "auto", "bc1", "bc3", "bc4", "bc5")[self.value]


class TextureCompressionQuality(IntEnum):
    FAST = 0
    NORMAL = 1
    HIGH = 2

    @classmethod
    def from_string(cls, value: str) -> "TextureCompressionQuality":
        return {
            "fast": cls.FAST,
            "normal": cls.NORMAL,
            "high": cls.HIGH,
        }.get(str(value).lower(), cls.NORMAL)

    def to_string(self) -> str:
        return ("fast", "normal", "high")[self.value]


class TextureFormat(IntEnum):
    AUTO = 0
    RGBA8 = 1
    RGBA4444 = 2
    RGBA16_UNORM = 3
    RGBA16_FLOAT = 4
    RGBA32_FLOAT = 5

    @classmethod
    def from_string(cls, value: str) -> "TextureFormat":
        return {
            "auto": cls.AUTO,
            "rgba8": cls.RGBA8,
            "rgba4444": cls.RGBA4444,
            "rgba16_unorm": cls.RGBA16_UNORM,
            "rgba16_float": cls.RGBA16_FLOAT,
            "rgba32_float": cls.RGBA32_FLOAT,
        }.get(str(value).lower(), cls.AUTO)

    def to_string(self) -> str:
        return (
            "auto", "rgba8", "rgba4444", "rgba16_unorm", "rgba16_float", "rgba32_float",
        )[self.value]


class WrapMode(IntEnum):
    REPEAT = 0
    CLAMP = 1
    MIRROR = 2

    @classmethod
    def from_string(cls, s: str) -> "WrapMode":
        _MAP = {"repeat": cls.REPEAT, "clamp": cls.CLAMP, "mirror": cls.MIRROR}
        return _MAP.get(s.lower(), cls.REPEAT)

    def to_string(self) -> str:
        return ("repeat", "clamp", "mirror")[self.value]


class FilterMode(IntEnum):
    POINT = 0
    BILINEAR = 1
    TRILINEAR = 2

    @classmethod
    def from_string(cls, s: str) -> "FilterMode":
        _MAP = {
            "point": cls.POINT, "nearest": cls.POINT,
            "bilinear": cls.BILINEAR, "linear": cls.BILINEAR,
            "trilinear": cls.TRILINEAR,
        }
        return _MAP.get(s.lower(), cls.BILINEAR)

    def to_string(self) -> str:
        return ("point", "linear", "trilinear")[self.value]


# ═══════════════════════════════════════════════════════════════════════════
# Sprite Frame
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class SpriteFrame:
    """One rectangular region inside a sprite-sheet texture."""
    name: str = ""
    x: int = 0
    y: int = 0
    w: int = 0
    h: int = 0
    pivot_x: float = 0.5
    pivot_y: float = 0.5

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "x": self.x, "y": self.y,
                "w": self.w, "h": self.h,
                "pivot_x": self.pivot_x, "pivot_y": self.pivot_y}

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SpriteFrame":
        expected = {"name", "x", "y", "w", "h", "pivot_x", "pivot_y"}
        if type(d) is not dict or set(d) != expected:
            raise ValueError("sprite frame must use the complete current field set")
        if type(d["name"]) is not str:
            raise TypeError("sprite frame name must be a string")
        if any(type(d[field]) is not int for field in ("x", "y", "w", "h")):
            raise TypeError("sprite frame rectangle must use integers")
        if d["w"] < 0 or d["h"] < 0:
            raise ValueError("sprite frame dimensions must be non-negative")
        pivots = (d["pivot_x"], d["pivot_y"])
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
               for value in pivots):
            raise TypeError("sprite frame pivots must be finite numbers")
        return cls(
            name=d["name"], x=d["x"], y=d["y"], w=d["w"], h=d["h"],
            pivot_x=float(d["pivot_x"]), pivot_y=float(d["pivot_y"]),
        )


# ═══════════════════════════════════════════════════════════════════════════
# Texture Import Settings
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class TextureImportSettings:
    """Unity-style texture import settings — stored in .meta alongside the image."""

    texture_type: TextureType = TextureType.DEFAULT
    wrap_mode: WrapMode = WrapMode.REPEAT
    filter_mode: FilterMode = FilterMode.BILINEAR
    generate_mipmaps: bool = True
    srgb: bool = True
    max_size: int = 2048
    aniso_level: int = 1
    format: TextureFormat = TextureFormat.AUTO
    compression: TextureCompression = TextureCompression.AUTO
    compression_quality: TextureCompressionQuality = TextureCompressionQuality.NORMAL
    sprite_frames: List[SpriteFrame] = field(default_factory=list)

    def _sync_derived_fields(self):
        """Re-derive settings from texture_type. Call after mutating texture_type.

        NORMAL_MAP forces sRGB off.
        UI and SPRITE default to clamp wrapping with no mipmaps; sprites also use point filtering.
        Other modes leave the current values unchanged.
        """
        if self.texture_type in {TextureType.NORMAL_MAP, TextureType.DATA, TextureType.VECTOR_FIELD}:
            self.srgb = False
        elif self.texture_type in {TextureType.UI, TextureType.SPRITE}:
            if self.texture_type == TextureType.SPRITE:
                self.filter_mode = FilterMode.POINT
            else:
                self.filter_mode = FilterMode.BILINEAR
            self.wrap_mode = WrapMode.CLAMP
            self.generate_mipmaps = False
            self.srgb = True

    # ── Serialization ──────────────────────────────────────────────────

    def to_dict(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "texture_type": self.texture_type.name.lower(),
            "wrap_mode": self.wrap_mode.to_string(),
            "filter_mode": self.filter_mode.to_string(),
            "generate_mipmaps": self.generate_mipmaps,
            "srgb": self.srgb,
            "max_size": self.max_size,
            "aniso_level": self.aniso_level,
            "texture_format": self.format.to_string(),
            "texture_compression": self.compression.to_string(),
            "texture_compression_quality": self.compression_quality.to_string(),
        }
        if self.sprite_frames:
            d["sprite_frames"] = [f.to_dict() for f in self.sprite_frames]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TextureImportSettings":
        required = {
            "texture_type", "wrap_mode", "filter_mode", "generate_mipmaps", "srgb",
            "max_size", "aniso_level", "texture_format", "texture_compression",
            "texture_compression_quality",
        }
        if type(d) is not dict:
            raise TypeError("texture import settings must be an object")
        missing = required - set(d)
        if missing:
            raise ValueError(f"texture import settings are missing current fields: {sorted(missing)}")
        string_fields = (
            "texture_type", "wrap_mode", "filter_mode", "texture_format",
            "texture_compression", "texture_compression_quality",
        )
        if any(type(d[field]) is not str for field in string_fields):
            raise TypeError("texture import setting enum fields must be strings")
        if type(d["generate_mipmaps"]) is not bool or type(d["srgb"]) is not bool:
            raise TypeError("texture import setting flags must be bools")
        if type(d["max_size"]) is not int or d["max_size"] <= 0:
            raise ValueError("texture max_size must be a positive integer")
        if type(d["aniso_level"]) is not int or not 1 <= d["aniso_level"] <= 16:
            raise ValueError("texture aniso_level must be an integer in [1, 16]")
        tt_str = d["texture_type"]
        tt_map = {
            "default": TextureType.DEFAULT,
            "normal_map": TextureType.NORMAL_MAP,
            "ui": TextureType.UI,
            "sprite": TextureType.SPRITE,
            "data": TextureType.DATA,
            "vector_field": TextureType.VECTOR_FIELD,
        }
        if tt_str not in tt_map:
            raise ValueError(f"unsupported texture_type: {tt_str}")
        tt = tt_map[tt_str]
        raw_frames = d.get("sprite_frames", [])
        if type(raw_frames) is not list:
            raise TypeError("texture sprite_frames must be an array")
        frames = [SpriteFrame.from_dict(f) for f in raw_frames] if raw_frames else []
        enum_values = {
            "wrap_mode": {"repeat", "clamp", "mirror"},
            "filter_mode": {"point", "linear", "trilinear"},
            "texture_format": {"auto", "rgba8", "rgba4444", "rgba16_unorm", "rgba16_float", "rgba32_float"},
            "texture_compression": {"none", "auto", "bc1", "bc3", "bc4", "bc5"},
            "texture_compression_quality": {"fast", "normal", "high"},
        }
        for field_name, allowed in enum_values.items():
            if d[field_name] not in allowed:
                raise ValueError(f"unsupported {field_name}: {d[field_name]}")
        result = cls(
            texture_type=tt,
            wrap_mode=WrapMode.from_string(d["wrap_mode"]),
            filter_mode=FilterMode.from_string(d["filter_mode"]),
            generate_mipmaps=d["generate_mipmaps"], srgb=d["srgb"],
            max_size=d["max_size"], aniso_level=d["aniso_level"],
            format=TextureFormat.from_string(d["texture_format"]),
            compression=TextureCompression.from_string(d["texture_compression"]),
            compression_quality=TextureCompressionQuality.from_string(d["texture_compression_quality"]),
            sprite_frames=frames,
        )
        if result.format != TextureFormat.AUTO:
            result.compression = TextureCompression.NONE
        return result

    def copy(self) -> "TextureImportSettings":
        """Return a deep copy (sprite_frames are duplicated)."""
        return TextureImportSettings(
            texture_type=self.texture_type,
            wrap_mode=self.wrap_mode,
            filter_mode=self.filter_mode,
            generate_mipmaps=self.generate_mipmaps,
            srgb=self.srgb,
            max_size=self.max_size,
            aniso_level=self.aniso_level,
            format=self.format,
            compression=self.compression,
            compression_quality=self.compression_quality,
            sprite_frames=[SpriteFrame(**f.__dict__) for f in self.sprite_frames],
        )

    def __eq__(self, other):
        if not isinstance(other, TextureImportSettings):
            return NotImplemented
        return (self.texture_type == other.texture_type
                and self.wrap_mode == other.wrap_mode
                and self.filter_mode == other.filter_mode
                and self.generate_mipmaps == other.generate_mipmaps
                and self.srgb == other.srgb
                and self.max_size == other.max_size
                and self.aniso_level == other.aniso_level
                and self.format == other.format
                and self.compression == other.compression
                and self.compression_quality == other.compression_quality
                and self.sprite_frames == other.sprite_frames)


# ═══════════════════════════════════════════════════════════════════════════
# Shader Asset Info (minimal — path-only editing)
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class ShaderAssetInfo:
    """Minimal shader asset model — currently supports path viewing/editing only."""

    guid: str = ""
    source_path: str = ""
    shader_type: str = ""  # "vertex" or "fragment"

    @classmethod
    def from_path(cls, path: str, guid: str = "") -> "ShaderAssetInfo":
        ext = os.path.splitext(path)[1].lower()
        _type_map = {".vert": "vertex", ".frag": "fragment"}
        return cls(guid=guid, source_path=path, shader_type=_type_map.get(ext, "unknown"))


@dataclass
class FontAssetInfo:
    """Minimal font asset model for Inspector display and UI font selection."""

    guid: str = ""
    source_path: str = ""
    font_type: str = ""

    @classmethod
    def from_path(cls, path: str, guid: str = "") -> "FontAssetInfo":
        ext = os.path.splitext(path)[1].lower()
        type_map = {
            ".ttf": "truetype",
            ".otf": "opentype",
        }
        return cls(guid=guid, source_path=path, font_type=type_map.get(ext, "unknown"))


# ═══════════════════════════════════════════════════════════════════════════
# Audio Clip Import Settings
# ═══════════════════════════════════════════════════════════════════════════


class AudioCompressionFormat(IntEnum):
    PCM = 0
    VORBIS = 1
    ADPCM = 2


@dataclass
class AudioImportSettings:
    """Unity-style audio import settings — stored in .meta alongside audio files."""

    force_mono: bool = False
    load_in_background: bool = False
    quality: float = 1.0
    compression_format: AudioCompressionFormat = AudioCompressionFormat.PCM

    def to_dict(self) -> Dict[str, Any]:
        return {
            "force_mono": self.force_mono,
            "load_in_background": self.load_in_background,
            "quality": self.quality,
            "compression_format": self.compression_format.name.lower(),
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AudioImportSettings":
        required = {"force_mono", "load_in_background", "quality", "compression_format"}
        if type(d) is not dict or not required.issubset(d):
            raise ValueError("audio import settings must use the complete current field set")
        if type(d["force_mono"]) is not bool or type(d["load_in_background"]) is not bool:
            raise TypeError("audio import setting flags must be bools")
        quality = d["quality"]
        if isinstance(quality, bool) or not isinstance(quality, (int, float)) or not math.isfinite(quality):
            raise TypeError("audio quality must be a finite number")
        if not 0.0 <= float(quality) <= 1.0:
            raise ValueError("audio quality must be in [0, 1]")
        fmt_str = d["compression_format"]
        fmt_map = {"pcm": AudioCompressionFormat.PCM, "vorbis": AudioCompressionFormat.VORBIS,
                    "adpcm": AudioCompressionFormat.ADPCM}
        if type(fmt_str) is not str or fmt_str not in fmt_map:
            raise ValueError(f"unsupported audio compression_format: {fmt_str}")
        return cls(
            force_mono=d["force_mono"], load_in_background=d["load_in_background"],
            quality=float(quality), compression_format=fmt_map[fmt_str],
        )

    def copy(self) -> "AudioImportSettings":
        return AudioImportSettings(
            force_mono=self.force_mono,
            load_in_background=self.load_in_background,
            quality=self.quality,
            compression_format=self.compression_format,
        )

    def __eq__(self, other):
        if not isinstance(other, AudioImportSettings):
            return NotImplemented
        return (self.force_mono == other.force_mono
                and self.load_in_background == other.load_in_background
                and self.quality == other.quality
                and self.compression_format == other.compression_format)


# ═══════════════════════════════════════════════════════════════════════════
# Meta-file utilities (read / write .meta JSON directly)
# ═══════════════════════════════════════════════════════════════════════════

def _load_strict_meta_root(meta_path: str) -> Dict[str, Any]:
    with open(meta_path, "r", encoding="utf-8") as f:
        root = json.load(f)
    if type(root) is not dict or set(root) != {"metadata"}:
        raise ValueError("meta document must contain exactly metadata")
    entries = root["metadata"]
    if type(entries) is not dict:
        raise TypeError("metadata must be an object")
    for key, entry in entries.items():
        if not isinstance(key, str) or not key or type(entry) is not dict or set(entry) != {"type", "value"}:
            raise ValueError(f"invalid metadata entry: {key!r}")
        tag = entry["type"]
        value = entry["value"]
        if type(tag) is not str:
            raise TypeError(f"metadata type tag must be a string: {key}")
        valid = (
            (tag == "string" and type(value) is str)
            or (tag == "int" and type(value) is int)
            or (tag == "bool" and type(value) is bool)
            or (tag == "size_t" and type(value) is int and value >= 0)
            or (tag == "float" and type(value) in (int, float) and math.isfinite(value))
            or (tag == "enum infernux::ResourceType" and type(value) is str)
            or (tag == "json_array" and type(value) is list)
            or (tag == "json_object" and type(value) is dict)
        )
        if not valid:
            raise TypeError(f"metadata value does not match type '{tag}': {key}")
    return root


def read_meta_file(asset_path: str) -> Optional[Dict[str, Any]]:
    """Read a .meta file for *asset_path* and return flat key→value dict.

    Returns ``None`` if the meta file doesn't exist or can't be parsed.
    The dict maps metadata keys to their Python values (str/int/bool/float).
    """
    meta_path = asset_path + ".meta"
    if not os.path.isfile(meta_path):
        return None
    try:
        entries = _load_strict_meta_root(meta_path)["metadata"]
        result: Dict[str, Any] = {}
        for key, entry in entries.items():
            result[key] = entry.get("value")
        return result
    except Exception as e:
        from Infernux.debug import Debug
        Debug.log_warning(f"Failed to read meta file '{meta_path}': {e}")
        return None


def read_meta_guid(asset_path: str) -> str:
    """Return the asset GUID stored in the current ``.meta`` schema."""
    meta_path = asset_path + ".meta"
    if not os.path.isfile(meta_path):
        return ""
    try:
        meta = read_meta_file(asset_path)
        if meta:
            guid = meta.get("guid")
            if isinstance(guid, str):
                return guid.strip()
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        pass
    return ""


def write_meta_fields(asset_path: str, updates: Dict[str, Any]) -> bool:
    """Update specific fields in a .meta file, preserving everything else.

    *updates* maps key→value.  The type tag is inferred from the Python type.
    Returns True on success.
    """
    meta_path = asset_path + ".meta"
    if not os.path.isfile(meta_path):
        return False
    try:
        root = _load_strict_meta_root(meta_path)
        entries = root["metadata"]
        for key, value in updates.items():
            type_tag = _python_type_to_meta_tag(value)
            entries[key] = {"type": type_tag, "value": value}
        blob = json.dumps(root, indent=4) + "\n"
        from Infernux.core.document_store import write_document_text
        write_document_text(meta_path, blob)
        return True
    except Exception as e:
        from Infernux.debug import Debug
        Debug.log_warning(f"Failed to write meta file '{meta_path}': {e}")
        return False


def write_meta_fields_async(asset_path: str, updates: Dict[str, Any]) -> "Future[bool]":
    """Fire-and-forget version of :func:`write_meta_fields`.

    Submits the read-modify-write to the shared IO thread pool and returns
    a :class:`~concurrent.futures.Future`.  Callers that need the result
    before proceeding can call ``future.result()``.
    """
    return _io_pool.submit(write_meta_fields, asset_path, updates)


def _python_type_to_meta_tag(value) -> str:
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, list):
        return "json_array"
    if isinstance(value, dict):
        return "json_object"
    if isinstance(value, str):
        return "string"
    raise TypeError(f"unsupported metadata value type: {type(value).__name__}")


def read_texture_import_settings(asset_path: str) -> TextureImportSettings:
    """Read texture import settings from the asset's .meta file.

    A missing sidecar uses defaults; an existing sidecar must contain the current fields.
    """
    meta = read_meta_file(asset_path)
    settings = TextureImportSettings() if meta is None else TextureImportSettings.from_dict(meta)
    if os.path.splitext(asset_path)[1].lower() == ".inxvfield":
        settings.texture_type = TextureType.VECTOR_FIELD
        settings.srgb = False
        settings.compression = TextureCompression.NONE
        if settings.format not in {TextureFormat.RGBA16_FLOAT, TextureFormat.RGBA32_FLOAT}:
            settings.format = TextureFormat.RGBA16_FLOAT
    return settings


def write_texture_import_settings(asset_path: str, settings: TextureImportSettings) -> bool:
    """Write texture import settings back to the .meta file."""
    canonical = settings.copy()
    if os.path.splitext(asset_path)[1].lower() == ".inxvfield":
        canonical.texture_type = TextureType.VECTOR_FIELD
        canonical.srgb = False
        canonical.compression = TextureCompression.NONE
        if canonical.format not in {TextureFormat.RGBA16_FLOAT, TextureFormat.RGBA32_FLOAT}:
            canonical.format = TextureFormat.RGBA16_FLOAT
    return write_meta_fields(asset_path, canonical.to_dict())


def read_audio_import_settings(asset_path: str) -> AudioImportSettings:
    """Read audio import settings from the asset's .meta file.

    A missing sidecar uses defaults; an existing sidecar must contain the current fields.
    """
    meta = read_meta_file(asset_path)
    if meta is None:
        return AudioImportSettings()
    return AudioImportSettings.from_dict(meta)


def write_audio_import_settings(asset_path: str, settings: AudioImportSettings) -> bool:
    """Write audio import settings back to the .meta file."""
    return write_meta_fields(asset_path, settings.to_dict())


# ═══════════════════════════════════════════════════════════════════════════
# Mesh Import Settings
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class MeshImportSettings:
    """Import settings for 3D model assets — stored in .meta alongside the source file."""

    scale_factor: float = 0.01
    generate_normals: bool = True
    generate_tangents: bool = True
    # DCC-authored meshes keep model/textures aligned without per-asset UV flipping.
    flip_uvs: bool = True
    # Unity-style public setting: swap primary/secondary UV channels.
    swap_uv_channels: bool = False
    optimize_mesh: bool = True

    def to_dict(self) -> Dict[str, Any]:
        return {
            "scale_factor": self.scale_factor,
            "generate_normals": self.generate_normals,
            "generate_tangents": self.generate_tangents,
            "flip_uvs": self.flip_uvs,
            "swap_uv_channels": self.swap_uv_channels,
            "optimize_mesh": self.optimize_mesh,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "MeshImportSettings":
        required = {
            "scale_factor", "generate_normals", "generate_tangents", "flip_uvs",
            "swap_uv_channels", "optimize_mesh",
        }
        if type(d) is not dict or set(d) != required:
            raise ValueError("mesh import settings must use the complete current field set")
        scale = d["scale_factor"]
        if isinstance(scale, bool) or not isinstance(scale, (int, float)) or not math.isfinite(scale) or scale <= 0.0:
            raise ValueError("mesh scale_factor must be a positive finite number")
        bool_fields = required - {"scale_factor"}
        if any(type(d[field]) is not bool for field in bool_fields):
            raise TypeError("mesh import setting flags must be bools")
        return cls(
            scale_factor=float(scale), generate_normals=d["generate_normals"],
            generate_tangents=d["generate_tangents"], flip_uvs=d["flip_uvs"],
            swap_uv_channels=d["swap_uv_channels"], optimize_mesh=d["optimize_mesh"],
        )

    def copy(self) -> "MeshImportSettings":
        return MeshImportSettings(
            scale_factor=self.scale_factor,
            generate_normals=self.generate_normals,
            generate_tangents=self.generate_tangents,
            flip_uvs=self.flip_uvs,
            swap_uv_channels=self.swap_uv_channels,
            optimize_mesh=self.optimize_mesh,
        )

    def __eq__(self, other):
        if not isinstance(other, MeshImportSettings):
            return NotImplemented
        return (self.scale_factor == other.scale_factor
                and self.generate_normals == other.generate_normals
                and self.generate_tangents == other.generate_tangents
                and self.flip_uvs == other.flip_uvs
                and self.swap_uv_channels == other.swap_uv_channels
                and self.optimize_mesh == other.optimize_mesh)


def read_mesh_import_settings(asset_path: str) -> MeshImportSettings:
    """Read mesh import settings from the asset's .meta file."""
    meta = read_meta_file(asset_path)
    if meta is None:
        return MeshImportSettings()
    return MeshImportSettings.from_dict(meta)


def write_mesh_import_settings(asset_path: str, settings: MeshImportSettings) -> bool:
    """Write mesh import settings back to the .meta file."""
    return write_meta_fields(asset_path, settings.to_dict())


# ═══════════════════════════════════════════════════════════════════════════
# Extension → asset type mapping (shared across AssetManager & Inspector)
# ═══════════════════════════════════════════════════════════════════════════

# Image extensions supported by InxTextureLoader / stb_image
IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".bmp", ".tga", ".gif", ".psd", ".hdr", ".pic", ".pnm", ".pgm", ".ppm",
    ".inxvfield",
})

# Shader extensions supported by ShaderImporter
SHADER_EXTENSIONS = frozenset({
    ".vert", ".frag",
})

# Material extension
MATERIAL_EXTENSIONS = frozenset({".mat"})
PHYSIC_MATERIAL_EXTENSIONS = frozenset({".physicmaterial"})
RENDER_EFFECT_EXTENSIONS = frozenset({".effect", ".effectgroup"})
PARTICLE_GRAPH_EXTENSIONS = frozenset({".particlegraph"})
POINT_CACHE_EXTENSIONS = frozenset({".pointcache"})

# Audio extensions supported by AudioImporter
AUDIO_EXTENSIONS = frozenset({".wav"})

# Font extensions recognized by the editor asset pipeline.
FONT_EXTENSIONS = frozenset({".ttf", ".otf"})

# 3D model extensions supported by ModelImporter / MeshLoader
MESH_EXTENSIONS = frozenset({
    ".fbx", ".obj", ".gltf", ".glb", ".dae", ".3ds", ".ply", ".stl",
})

# Prefab extension
PREFAB_EXTENSIONS = frozenset({".prefab"})

# Animation clip extension
ANIMCLIP_EXTENSIONS = frozenset({".animclip2d"})

# 3D animation clip extension (skeletal takes embedded in model files)
ANIMCLIP3D_EXTENSIONS = frozenset({".animclip3d"})

# Animation state machine extension
ANIMFSM_EXTENSIONS = frozenset({".animfsm"})

# Transform timeline extension
ANIMTIMELINE_EXTENSIONS = frozenset({".animtimeline"})

# Timeline state machine extension (FSM whose nodes are all timelines)
TIMELINEFSM_EXTENSIONS = frozenset({".timelinefsm"})


def asset_category_from_extension(ext: str) -> Optional[str]:
    """Return the editor asset category for a file extension."""
    ext = ext.lower()
    if ext in MATERIAL_EXTENSIONS:
        return "material"
    if ext in PHYSIC_MATERIAL_EXTENSIONS:
        return "physic_material"
    if ext in RENDER_EFFECT_EXTENSIONS:
        return "render_effect"
    if ext in PARTICLE_GRAPH_EXTENSIONS:
        return "particle_graph"
    if ext in POINT_CACHE_EXTENSIONS:
        return "point_cache"
    if ext in IMAGE_EXTENSIONS:
        return "texture"
    if ext in SHADER_EXTENSIONS:
        return "shader"
    if ext in AUDIO_EXTENSIONS:
        return "audio"
    if ext in FONT_EXTENSIONS:
        return "font"
    if ext in MESH_EXTENSIONS:
        return "mesh"
    if ext in PREFAB_EXTENSIONS:
        return "prefab"
    if ext in ANIMCLIP_EXTENSIONS:
        return "animclip"
    if ext in ANIMCLIP3D_EXTENSIONS:
        return "animclip3d"
    if ext in ANIMFSM_EXTENSIONS:
        return "animfsm"
    if ext in ANIMTIMELINE_EXTENSIONS:
        return "animtimeline"
    if ext in TIMELINEFSM_EXTENSIONS:
        return "timelinefsm"
    return None
