#pragma once

#include "InxTexture.h"

#include <cstdint>
#include <memory>
#include <string>
#include <string_view>

namespace infernux
{

class TextureArtifact final
{
  public:
    static constexpr uint32_t FormatVersion = 3;

    [[nodiscard]] static std::string Serialize(const TextureCpuData &texture, std::string_view sourceContentHash);
    [[nodiscard]] static std::shared_ptr<const TextureCpuData> Deserialize(std::string_view bytes,
                                                                           std::string_view expectedSourceContentHash);
};

} // namespace infernux
