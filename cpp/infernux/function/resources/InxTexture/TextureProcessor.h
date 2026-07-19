#pragma once

#include "InxTexture.h"

#include <memory>

namespace infernux
{

enum class TextureCompression : uint32_t
{
    None = 0,
    Automatic,
    BC1,
    BC3,
    BC4,
    BC5,
};

enum class TextureCompressionQuality : uint32_t
{
    Fast = 0,
    Normal,
    High,
};

struct TextureProcessOptions
{
    bool generateMipmaps = true;
    TextureCompression compression = TextureCompression::Automatic;
    TextureCompressionQuality quality = TextureCompressionQuality::Normal;
};

/// Deterministic offline processing used by texture importers. The runtime
/// consumes the resulting concrete format and never regenerates mips or
/// recompresses a texture while loading it.
class TextureProcessor final
{
  public:
    [[nodiscard]] static std::shared_ptr<const TextureCpuData> Process(TextureCpuData source,
                                                                       const TextureProcessOptions &options);
};

} // namespace infernux
