#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <string_view>

namespace infernux
{

class InxSkinnedMesh;

class SkinnedMeshArtifact final
{
  public:
    [[nodiscard]] static bool HasCurrentHeader(std::string_view bytes) noexcept;
    [[nodiscard]] static std::string Serialize(const InxSkinnedMesh &mesh, std::string_view sourceContentHash);
    [[nodiscard]] static std::string SerializeEmpty(std::string_view sourceContentHash);
    [[nodiscard]] static std::shared_ptr<InxSkinnedMesh> Deserialize(std::string_view bytes,
                                                                     std::string_view expectedSourceContentHash);
};

} // namespace infernux
