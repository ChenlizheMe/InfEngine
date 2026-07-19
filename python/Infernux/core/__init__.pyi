"""Type stubs for Infernux.core."""

from __future__ import annotations

from .material import Material as Material
from .texture import Texture as Texture
from .shader import Shader as Shader
from .audio_clip import AudioClip as AudioClip
from .physic_material import PhysicMaterial as PhysicMaterial
from .assets import AssetManager as AssetManager
from .vfx_system import (
    VfxAttribute as VfxAttribute,
    VfxEmitter as VfxEmitter,
    VfxRenderer as VfxRenderer,
    VfxSchemaError as VfxSchemaError,
    VfxSystem as VfxSystem,
)
from .parallel_backend import (
    ParallelBackend as ParallelBackend,
    ParallelBufferView as ParallelBufferView,
    ParallelCapabilities as ParallelCapabilities,
    ParallelTaskState as ParallelTaskState,
)
from .asset_types import (
    TextureCompression as TextureCompression,
    TextureCompressionQuality as TextureCompressionQuality,
    TextureImportSettings as TextureImportSettings,
    TextureType as TextureType,
    WrapMode as WrapMode,
    FilterMode as FilterMode,
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
    VfxSystemRef as VfxSystemRef,
    RenderEffectRef as RenderEffectRef,
)

__all__ = [
    "Material",
    "Texture",
    "Shader",
    "AudioClip",
    "PhysicMaterial",
    "AssetManager",
    "VfxAttribute",
    "VfxEmitter",
    "VfxRenderer",
    "VfxSchemaError",
    "VfxSystem",
    "ParallelBackend",
    "ParallelBufferView",
    "ParallelCapabilities",
    "ParallelTaskState",
    "TextureImportSettings",
    "TextureCompression",
    "TextureCompressionQuality",
    "TextureType",
    "WrapMode",
    "FilterMode",
    "ShaderAssetInfo",
    "FontAssetInfo",
    "AudioImportSettings",
    "AudioCompressionFormat",
    "MeshImportSettings",
    "TextureRef",
    "ShaderRef",
    "AudioClipRef",
    "PhysicMaterialRef",
    "VfxSystemRef",
    "RenderEffectRef",
]
