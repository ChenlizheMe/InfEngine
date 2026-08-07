"""Type stubs for Infernux.core."""

from __future__ import annotations

from .material import Material as Material
from .texture import Texture as Texture
from .shader import Shader as Shader
from .audio_clip import AudioClip as AudioClip
from .physic_material import PhysicMaterial as PhysicMaterial
from .animation_clip import AnimationClip as AnimationClip, AnimationFrame as AnimationFrame
from .assets import AssetManager as AssetManager
from .parallel_backend import (
    ParallelBackend as ParallelBackend,
    ParallelBufferView as ParallelBufferView,
    ParallelCapabilities as ParallelCapabilities,
    ParallelTaskState as ParallelTaskState,
)
from .asset_types import (
    TextureCompression as TextureCompression,
    TextureCompressionQuality as TextureCompressionQuality,
    TextureFormat as TextureFormat,
    TextureImportSettings as TextureImportSettings,
    TextureType as TextureType,
    WrapMode as WrapMode,
    FilterMode as FilterMode,
    SpriteFrame as SpriteFrame,
    ShaderAssetInfo as ShaderAssetInfo,
    FontAssetInfo as FontAssetInfo,
    AudioImportSettings as AudioImportSettings,
    AudioCompressionFormat as AudioCompressionFormat,
    MeshImportSettings as MeshImportSettings,
)
from .asset_ref import (
    TextureRef as TextureRef,
    ShaderRef as ShaderRef,
    AudioClipRef as AudioClipRef,
    PhysicMaterialRef as PhysicMaterialRef,
    RenderEffectRef as RenderEffectRef,
)
from .asset_reference_types import AssetReferenceType as AssetReferenceType, AssetTypeRegistry as AssetTypeRegistry, asset_type_registry as asset_type_registry

__all__ = [
    "Material",
    "Texture",
    "Shader",
    "AudioClip",
    "PhysicMaterial",
    "AnimationClip",
    "AnimationFrame",
    "AssetManager",
    "ParallelBackend",
    "ParallelBufferView",
    "ParallelCapabilities",
    "ParallelTaskState",
    "TextureImportSettings",
    "TextureCompression",
    "TextureCompressionQuality",
    "TextureFormat",
    "TextureType",
    "WrapMode",
    "FilterMode",
    "SpriteFrame",
    "ShaderAssetInfo",
    "FontAssetInfo",
    "AudioImportSettings",
    "AudioCompressionFormat",
    "MeshImportSettings",
    "TextureRef",
    "ShaderRef",
    "AudioClipRef",
    "PhysicMaterialRef",
    "RenderEffectRef",
    "AssetReferenceType",
    "AssetTypeRegistry",
    "asset_type_registry",
]
