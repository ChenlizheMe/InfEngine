#pragma once

#include "InxTexture.h"

#include <string_view>

namespace infernux
{

/// Strict, text-authored signed-distance volume. Distances are expressed in
/// canonical field coordinates whose sampled domain is [-0.5, 0.5]^3.
class SignedDistanceFieldSource final
{
  public:
    [[nodiscard]] static TextureCpuData Decode(std::string_view document);
};

} // namespace infernux
