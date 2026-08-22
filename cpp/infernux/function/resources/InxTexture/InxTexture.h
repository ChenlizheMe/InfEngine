#pragma once

#include <array>
#include <cstdint>
#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

namespace infernux
{

class InxResourceMeta;

enum class TextureDimension : uint32_t
{
    Texture2D = 1,
    Texture3D = 2,
};

enum class TextureSemantic : uint32_t
{
    Color = 1,
    Normal = 2,
    Data = 3,
    UserInterface = 4,
    Sprite = 5,
    VectorField = 6,
    SignedDistanceField = 7,
};

/// Concrete texel representation stored in .inxtex and uploaded to the RHI.
/// Color-space is part of the format so an sRGB decision cannot drift between
/// importer, runtime cache, and backend image creation.
enum class TextureFormat : uint32_t
{
    Rgba8UNorm = 1,
    Rgba8Srgb = 2,
    Rgba32Float = 3,
    BC1RgbaUNorm = 4,
    BC1RgbaSrgb = 5,
    BC3UNorm = 6,
    BC3Srgb = 7,
    BC4UNorm = 8,
    BC5UNorm = 9,
    BC6HUFloat = 10,
    BC7UNorm = 11,
    BC7Srgb = 12,
    Rgba4UNormPack16 = 13,
    Rgba16UNorm = 14,
    Rgba16Float = 15,
};

struct TextureMipLevel
{
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t depth = 1;
    uint64_t byteOffset = 0;
    uint64_t byteSize = 0;
    uint64_t rowPitch = 0;
    uint64_t slicePitch = 0;
};

struct TextureCpuData
{
    TextureDimension dimension = TextureDimension::Texture2D;
    TextureSemantic semantic = TextureSemantic::Color;
    TextureFormat format = TextureFormat::Rgba8Srgb;
    std::vector<TextureMipLevel> mipLevels;
    std::vector<uint8_t> bytes;
    std::array<float, 16> bakeBasis = {
        1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f,
    };
    std::array<float, 4> valueMin = {0.0f, 0.0f, 0.0f, 0.0f};
    std::array<float, 4> valueMax = {1.0f, 1.0f, 1.0f, 1.0f};

    [[nodiscard]] bool IsValid() const noexcept
    {
        return !mipLevels.empty() && !bytes.empty();
    }
};

/// Lightweight description of an imported texture artifact. Runtime texture
/// objects retain this metadata, never the texel payload used during upload.
struct TextureArtifactDescription
{
    TextureDimension dimension = TextureDimension::Texture2D;
    TextureSemantic semantic = TextureSemantic::Color;
    TextureFormat format = TextureFormat::Rgba8Srgb;
    std::vector<TextureMipLevel> mipLevels;
    std::array<float, 16> bakeBasis = {
        1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f,
    };
    std::array<float, 4> valueMin = {0.0f, 0.0f, 0.0f, 0.0f};
    std::array<float, 4> valueMax = {1.0f, 1.0f, 1.0f, 1.0f};
    uint64_t payloadBytes = 0;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return !mipLevels.empty() && payloadBytes != 0;
    }
};

[[nodiscard]] constexpr bool TextureFormatIsSrgb(TextureFormat format) noexcept
{
    return format == TextureFormat::Rgba8Srgb || format == TextureFormat::BC1RgbaSrgb ||
           format == TextureFormat::BC3Srgb || format == TextureFormat::BC7Srgb;
}

[[nodiscard]] constexpr bool TextureFormatIsBlockCompressed(TextureFormat format) noexcept
{
    return format >= TextureFormat::BC1RgbaUNorm && format <= TextureFormat::BC7Srgb;
}

[[nodiscard]] constexpr uint32_t TextureFormatBlockBytes(TextureFormat format) noexcept
{
    switch (format) {
    case TextureFormat::BC1RgbaUNorm:
    case TextureFormat::BC1RgbaSrgb:
    case TextureFormat::BC4UNorm:
        return 8;
    case TextureFormat::BC3UNorm:
    case TextureFormat::BC3Srgb:
    case TextureFormat::BC5UNorm:
    case TextureFormat::BC6HUFloat:
    case TextureFormat::BC7UNorm:
    case TextureFormat::BC7Srgb:
        return 16;
    default:
        return 0;
    }
}

[[nodiscard]] constexpr uint32_t TextureFormatBytesPerTexel(TextureFormat format) noexcept
{
    switch (format) {
    case TextureFormat::Rgba8UNorm:
    case TextureFormat::Rgba8Srgb:
        return 4;
    case TextureFormat::Rgba32Float:
        return 16;
    case TextureFormat::Rgba4UNormPack16:
        return 2;
    case TextureFormat::Rgba16UNorm:
    case TextureFormat::Rgba16Float:
        return 8;
    default:
        return 0;
    }
}

[[nodiscard]] constexpr const char *TextureFormatName(TextureFormat format) noexcept
{
    switch (format) {
    case TextureFormat::Rgba8UNorm:
        return "rgba8_unorm";
    case TextureFormat::Rgba8Srgb:
        return "rgba8_srgb";
    case TextureFormat::Rgba32Float:
        return "rgba32_float";
    case TextureFormat::BC1RgbaUNorm:
        return "bc1_rgba_unorm";
    case TextureFormat::BC1RgbaSrgb:
        return "bc1_rgba_srgb";
    case TextureFormat::BC3UNorm:
        return "bc3_unorm";
    case TextureFormat::BC3Srgb:
        return "bc3_srgb";
    case TextureFormat::BC4UNorm:
        return "bc4_unorm";
    case TextureFormat::BC5UNorm:
        return "bc5_unorm";
    case TextureFormat::BC6HUFloat:
        return "bc6h_ufloat";
    case TextureFormat::BC7UNorm:
        return "bc7_unorm";
    case TextureFormat::BC7Srgb:
        return "bc7_srgb";
    case TextureFormat::Rgba4UNormPack16:
        return "rgba4_unorm_pack16";
    case TextureFormat::Rgba16UNorm:
        return "rgba16_unorm";
    case TextureFormat::Rgba16Float:
        return "rgba16_float";
    }
    return "unknown";
}

/**
 * @brief Lightweight C++ asset representing a texture's import settings.
 *
 * InxTexture does NOT hold GPU resources (VkImage, VkImageView, etc.).
 * Those remain owned by the renderer's GPU texture cache (InxVkCoreModular).
 *
 * InxTexture is managed by AssetRegistry and provides:
 *   - Cached import settings from the .meta file (sRGB, mipmaps, texture_type)
 *   - GUID / file-path identity
 *   - In-place reload so all holders see updated metadata
 *
 * This decouples metadata reading from the per-frame render path,
 * avoiding repeated .meta file I/O in ResolveTextureForMaterial().
 */
class InxTexture
{
  public:
    InxTexture() = default;

    // ── Identity ────────────────────────────────────────────────────────────

    [[nodiscard]] const std::string &GetGuid() const
    {
        return m_guid;
    }
    void SetGuid(const std::string &guid)
    {
        m_guid = guid;
    }

    [[nodiscard]] const std::string &GetFilePath() const
    {
        return m_filePath;
    }
    void SetFilePath(const std::string &path)
    {
        m_filePath = path;
    }

    [[nodiscard]] const std::string &GetName() const
    {
        return m_name;
    }
    void SetName(const std::string &name)
    {
        m_name = name;
    }

    // ── Import settings (from .meta) ────────────────────────────────────────

    [[nodiscard]] const std::string &GetTextureType() const
    {
        return m_textureType;
    }
    void SetTextureType(const std::string &type)
    {
        m_textureType = type;
    }

    [[nodiscard]] bool IsSrgb() const
    {
        return m_hasArtifactDescription ? TextureFormatIsSrgb(m_format) : m_srgb;
    }
    void SetSrgb(bool srgb)
    {
        m_srgb = srgb;
    }

    [[nodiscard]] bool GenerateMipmaps() const
    {
        return m_generateMipmaps;
    }
    void SetGenerateMipmaps(bool gen)
    {
        m_generateMipmaps = gen;
    }

    [[nodiscard]] int GetMaxSize() const
    {
        return m_maxSize;
    }
    void SetMaxSize(int size)
    {
        m_maxSize = size;
    }

    [[nodiscard]] const std::string &GetFilterMode() const
    {
        return m_filterMode;
    }
    void SetFilterMode(const std::string &mode)
    {
        m_filterMode = mode;
    }

    [[nodiscard]] const std::string &GetWrapMode() const
    {
        return m_wrapMode;
    }
    void SetWrapMode(const std::string &mode)
    {
        m_wrapMode = mode;
    }

    [[nodiscard]] int GetAnisoLevel() const
    {
        return m_anisoLevel;
    }
    void SetAnisoLevel(int level)
    {
        m_anisoLevel = level;
    }

    [[nodiscard]] bool IsNormalMapMode() const
    {
        return m_textureType == "normal_map";
    }

    /// Determine whether this texture should use linear format (UNORM).
    /// Solely based on the srgb import setting — no hardcoded texture_type logic.
    [[nodiscard]] bool IsLinear() const
    {
        return !IsSrgb();
    }

    [[nodiscard]] TextureDimension GetDimension() const noexcept
    {
        return m_dimension;
    }
    [[nodiscard]] TextureSemantic GetSemantic() const noexcept
    {
        return m_semantic;
    }
    [[nodiscard]] TextureFormat GetFormat() const noexcept
    {
        return m_hasArtifactDescription ? m_format : (m_srgb ? TextureFormat::Rgba8Srgb : TextureFormat::Rgba8UNorm);
    }
    [[nodiscard]] uint32_t GetPixelWidth() const noexcept
    {
        return m_width;
    }
    [[nodiscard]] uint32_t GetPixelHeight() const noexcept
    {
        return m_height;
    }
    [[nodiscard]] uint32_t GetPixelDepth() const noexcept
    {
        return m_depth;
    }
    [[nodiscard]] uint32_t GetMipCount() const noexcept
    {
        return m_mipCount;
    }
    [[nodiscard]] const std::array<float, 16> &GetBakeBasis() const noexcept
    {
        return m_bakeBasis;
    }
    [[nodiscard]] const std::array<float, 4> &GetValueMin() const noexcept
    {
        return m_valueMin;
    }
    [[nodiscard]] const std::array<float, 4> &GetValueMax() const noexcept
    {
        return m_valueMax;
    }

    void ApplyImportSettings(const InxResourceMeta &metadata);

    [[nodiscard]] uint64_t GetGeneration() const noexcept
    {
        return m_generation;
    }
    void SetArtifactDescription(const TextureArtifactDescription &description)
    {
        if (!description.IsValid())
            throw std::invalid_argument("texture artifact description is invalid");
        m_dimension = description.dimension;
        m_semantic = description.semantic;
        m_format = description.format;
        m_bakeBasis = description.bakeBasis;
        m_valueMin = description.valueMin;
        m_valueMax = description.valueMax;
        m_mipCount = static_cast<uint32_t>(description.mipLevels.size());
        m_width = description.mipLevels.front().width;
        m_height = description.mipLevels.front().height;
        m_depth = description.mipLevels.front().depth;
        m_hasArtifactDescription = true;
        ++m_generation;
    }

    // ── Clone (Unity-style Object.Instantiate) ─────────────────────────────

    /// @brief Create a copy of this texture's identity-independent metadata.
    /// Pixel payload is never copied or retained by a runtime clone.
    [[nodiscard]] std::shared_ptr<InxTexture> Clone() const;
    [[nodiscard]] size_t GetRuntimeMemoryBytes() const noexcept;

  private:
    std::string m_guid;
    std::string m_filePath;
    std::string m_name;

    // Import settings — defaults match the engine convention (sRGB, mipmaps on)
    std::string m_textureType; // "normal_map", "mask", etc.  Empty = default (color)
    bool m_srgb = true;
    bool m_generateMipmaps = true;
    int m_maxSize = 2048;
    std::string m_filterMode = "bilinear"; // "point", "bilinear", "trilinear"
    std::string m_wrapMode = "repeat";     // "repeat", "clamp", "mirror"
    int m_anisoLevel = -1;                 // -1 = device max, 0 = off, 1-16 = explicit
    TextureDimension m_dimension = TextureDimension::Texture2D;
    TextureSemantic m_semantic = TextureSemantic::Color;
    TextureFormat m_format = TextureFormat::Rgba8Srgb;
    uint32_t m_width = 0;
    uint32_t m_height = 0;
    uint32_t m_depth = 1;
    uint32_t m_mipCount = 0;
    std::array<float, 16> m_bakeBasis = {
        1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f,
    };
    std::array<float, 4> m_valueMin = {0.0f, 0.0f, 0.0f, 0.0f};
    std::array<float, 4> m_valueMax = {1.0f, 1.0f, 1.0f, 1.0f};
    bool m_hasArtifactDescription = false;
    uint64_t m_generation = 0;
};

} // namespace infernux
