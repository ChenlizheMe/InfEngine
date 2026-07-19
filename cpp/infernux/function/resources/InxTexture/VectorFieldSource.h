#pragma once

#include "InxTexture.h"

#include <string_view>

namespace infernux
{

/// Strict authoring format for vector-field volumes. The decoded
/// data always uses a canonical x-fastest RGBA32F base level before the normal
/// texture processor produces the selected runtime format and mip chain.
class VectorFieldSource final
{
  public:
    [[nodiscard]] static TextureCpuData Decode(std::string_view document);
};

} // namespace infernux
