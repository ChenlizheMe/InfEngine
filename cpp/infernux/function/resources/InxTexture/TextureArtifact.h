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
    [[nodiscard]] static bool HasCurrentHeader(std::string_view bytes) noexcept;
    [[nodiscard]] static TextureArtifactDescription ReadDescription(const std::string &artifactPath,
                                                                    std::string_view expectedSourceContentHash);
    [[nodiscard]] static std::string Serialize(const TextureCpuData &texture, std::string_view sourceContentHash);
    [[nodiscard]] static std::shared_ptr<const TextureCpuData> Deserialize(std::string_view bytes,
                                                                           std::string_view expectedSourceContentHash);
};

} // namespace infernux
