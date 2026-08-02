#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <string_view>

namespace infernux
{

class InxMesh;

class MeshArtifact final
{
  public:
    [[nodiscard]] static bool HasCurrentHeader(std::string_view bytes) noexcept;
    [[nodiscard]] static std::string Serialize(const InxMesh &mesh, std::string_view sourceContentHash);
    [[nodiscard]] static std::shared_ptr<InxMesh> Deserialize(std::string_view bytes,
                                                              std::string_view expectedSourceContentHash);
};

} // namespace infernux
