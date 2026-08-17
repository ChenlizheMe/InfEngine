#include "InxTexture.h"

#include <function/resources/InxResource/InxResourceMeta.h>

#include <core/log/InxLog.h>

namespace infernux
{

size_t InxTexture::GetRuntimeMemoryBytes() const noexcept
{
    return sizeof(*this) + m_guid.capacity() + m_filePath.capacity() + m_name.capacity() + m_textureType.capacity() +
           m_filterMode.capacity() + m_wrapMode.capacity();
}

// =============================================================================
// ApplyImportSettings — consume the immutable AssetDatabase metadata snapshot
// =============================================================================

void InxTexture::ApplyImportSettings(const InxResourceMeta &meta)
{
    if (meta.HasKey("texture_type")) {
        m_textureType = meta.GetDataAs<std::string>("texture_type");
    }
    if (meta.HasKey("srgb")) {
        m_srgb = meta.GetDataAs<bool>("srgb");
    }
    if (meta.HasKey("generate_mipmaps")) {
        m_generateMipmaps = meta.GetDataAs<bool>("generate_mipmaps");
    }
    if (meta.HasKey("max_size")) {
        m_maxSize = meta.GetDataAs<int>("max_size");
    }
    if (meta.HasKey("filter_mode")) {
        m_filterMode = meta.GetDataAs<std::string>("filter_mode");
    }
    if (meta.HasKey("wrap_mode")) {
        m_wrapMode = meta.GetDataAs<std::string>("wrap_mode");
    }
    if (meta.HasKey("aniso_level")) {
        const int importedLevel = meta.GetDataAs<int>("aniso_level");
        // Level 1 was the old importer default and never enabled Vulkan
        // anisotropic filtering. The current authoring contract treats that
        // legacy value as automatic device quality, matching the Python asset
        // settings reader and the TextureImporter migration.
        m_anisoLevel = importedLevel == 1 ? -1 : importedLevel;
    }
}

// =============================================================================
// Clone — Unity-style Object.Instantiate for textures
// =============================================================================

std::shared_ptr<InxTexture> InxTexture::Clone() const
{
    auto clone = std::make_shared<InxTexture>();

    // Copy metadata — the clone references the same image file on disk.
    clone->m_name = m_name + " (Instance)";
    clone->m_filePath = m_filePath; // Same source file
    // clone->m_guid intentionally left empty — runtime-only instance

    // Copy import settings
    clone->m_textureType = m_textureType;
    clone->m_srgb = m_srgb;
    clone->m_generateMipmaps = m_generateMipmaps;
    clone->m_maxSize = m_maxSize;
    clone->m_filterMode = m_filterMode;
    clone->m_wrapMode = m_wrapMode;
    clone->m_anisoLevel = m_anisoLevel;
    clone->m_dimension = m_dimension;
    clone->m_semantic = m_semantic;
    clone->m_format = m_format;
    clone->m_width = m_width;
    clone->m_height = m_height;
    clone->m_depth = m_depth;
    clone->m_mipCount = m_mipCount;
    clone->m_bakeBasis = m_bakeBasis;
    clone->m_valueMin = m_valueMin;
    clone->m_valueMax = m_valueMax;
    clone->m_hasArtifactDescription = m_hasArtifactDescription;
    clone->m_generation = m_generation;

    return clone;
}

} // namespace infernux
