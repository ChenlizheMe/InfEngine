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
        return cls(
            name=str(d.get("name", "")),
            x=int(d.get("x", 0)), y=int(d.get("y", 0)),
            w=int(d.get("w", 0)), h=int(d.get("h", 0)),
            pivot_x=float(d.get("pivot_x", 0.5)),
            pivot_y=float(d.get("pivot_y", 0.5)),
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
    sprite_frames: List[SpriteFrame] = field(default_factory=list)

    def _sync_derived_fields(self):
        """Re-derive settings from texture_type. Call after mutating texture_type.

        NORMAL_MAP forces sRGB off.
        SPRITE forces point filtering, clamp wrapping, no mipmaps.
        Other modes leave the current values unchanged.
        """
        if self.texture_type == TextureType.NORMAL_MAP:
            self.srgb = False
        elif self.texture_type == TextureType.SPRITE:
            self.filter_mode = FilterMode.POINT
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
        }
        if self.sprite_frames:
            d["sprite_frames"] = [f.to_dict() for f in self.sprite_frames]
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "TextureImportSettings":
        tt_str = d.get("texture_type", "default")
        tt_map = {"default": TextureType.DEFAULT, "normal_map": TextureType.NORMAL_MAP, "ui": TextureType.UI, "sprite": TextureType.SPRITE}
        tt = tt_map.get(tt_str, TextureType.DEFAULT)
        raw_frames = d.get("sprite_frames", [])
        # raw_frames may be a JSON string if C++ round-tripped the .meta
        if isinstance(raw_frames, str):
            try:
                import json as _json
                raw_frames = _json.loads(raw_frames)
            except Exception:
                raw_frames = []
        frames = [SpriteFrame.from_dict(f) for f in raw_frames] if raw_frames else []
        return cls(
            texture_type=tt,
            wrap_mode=WrapMode.from_string(d.get("wrap_mode", "repeat")),
            filter_mode=FilterMode.from_string(d.get("filter_mode", "linear")),
            generate_mipmaps=bool(d.get("generate_mipmaps", True)),
            srgb=bool(d.get("srgb", tt != TextureType.NORMAL_MAP)),
            max_size=int(d.get("max_size", 2048)),
            aniso_level=int(d.get("aniso_level", 1)),
            sprite_frames=frames,
        )

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
        fmt_str = d.get("compression_format", "pcm")
        fmt_map = {"pcm": AudioCompressionFormat.PCM, "vorbis": AudioCompressionFormat.VORBIS,
                    "adpcm": AudioCompressionFormat.ADPCM}
        return cls(
            force_mono=bool(d.get("force_mono", False)),
            load_in_background=bool(d.get("load_in_background", False)),
            quality=float(d.get("quality", 1.0)),
            compression_format=fmt_map.get(fmt_str, AudioCompressionFormat.PCM),
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
    if type(root) is not dict or set(root) != {"meta_version", "metadata"}:
        raise ValueError("meta document must contain exactly meta_version and metadata")
    if type(root["meta_version"]) is not int or root["meta_version"] != 2:
        raise ValueError("meta_version must be 2")
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

    Missing keys are back-filled with defaults (matching TextureImporter C++ defaults).
    """
    meta = read_meta_file(asset_path)
    if meta is None:
        return TextureImportSettings()
    return TextureImportSettings.from_dict(meta)


def write_texture_import_settings(asset_path: str, settings: TextureImportSettings) -> bool:
    """Write texture import settings back to the .meta file."""
    return write_meta_fields(asset_path, settings.to_dict())


def read_audio_import_settings(asset_path: str) -> AudioImportSettings:
    """Read audio import settings from the asset's .meta file.

    Missing keys are back-filled with defaults (matching AudioImporter C++ defaults).
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
    # Internal compatibility knob: Unity-like default for DCC-authored meshes is
    # to keep model/textures aligned without requiring per-asset UV flipping.
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
        importer_version = int(d.get("importer_version", 1) or 1)
        flip_uvs = bool(d.get("flip_uvs", True))
        # Migration: importer v1 defaulted to false, which breaks many Blender/DCC assets
        # in our Vulkan path. v2 aligns to Unity-like import expectations.
        if importer_version < 2:
            flip_uvs = True
        return cls(
            scale_factor=float(d.get("scale_factor", 0.01)),
            generate_normals=bool(d.get("generate_normals", True)),
            generate_tangents=bool(d.get("generate_tangents", True)),
            flip_uvs=flip_uvs,
            swap_uv_channels=bool(d.get("swap_uv_channels", False)),
            optimize_mesh=bool(d.get("optimize_mesh", True)),
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
})

# Shader extensions supported by ShaderImporter
SHADER_EXTENSIONS = frozenset({
    ".vert", ".frag",
})

# Material extension
MATERIAL_EXTENSIONS = frozenset({".mat"})
PHYSIC_MATERIAL_EXTENSIONS = frozenset({".physicmaterial"})
RENDER_EFFECT_EXTENSIONS = frozenset({".effect", ".effectgroup", ".effectstack"})
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

# VFX system authoring asset extension
VFXSYSTEM_EXTENSIONS = frozenset({".vfxsystem"})

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
    if ext in VFXSYSTEM_EXTENSIONS:
        return "vfxsystem"
    if ext in ANIMTIMELINE_EXTENSIONS:
        return "animtimeline"
    if ext in TIMELINEFSM_EXTENSIONS:
        return "timelinefsm"
    return None
