#pragma once

#include <core/types/ShaderProgramArtifact.h>
#include <core/types/ShaderTypes.h>
#include <function/resources/ShaderAsset/ShaderDescriptor.h>
#include <function/resources/ShaderAsset/ShaderStageLinker.h>

#include <optional>
#include <string>
#include <vector>

namespace infernux
{

struct ShaderPassVariantRequirement
{
    ShaderCompileTarget target = ShaderCompileTarget::Forward;
    bool enabled = false;
    std::optional<ShaderCompileTarget> fallback;
    std::string reason;
};

struct ShaderPassVariantPlan
{
    ShaderStagePair stages;
    std::vector<ShaderPassVariantRequirement> requirements;
    std::vector<std::string> diagnostics;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return stages.IsValid() && diagnostics.empty();
    }

    [[nodiscard]] const ShaderPassVariantRequirement *Find(ShaderCompileTarget target) const noexcept;
};

class ShaderPassVariantPlanner final
{
  public:
    [[nodiscard]] static ShaderPassVariantPlan Plan(const ShaderDescriptor &vertex, const ShaderDescriptor &fragment,
                                                    const ShaderProgramInterfaceArtifact &interfaceArtifact);
};

} // namespace infernux
