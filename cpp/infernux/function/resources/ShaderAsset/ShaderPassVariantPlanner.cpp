#include "ShaderPassVariantPlanner.h"

#include <algorithm>
#include <cctype>
#include <string_view>

namespace infernux
{
namespace
{

bool EqualsInsensitive(std::string_view lhs, std::string_view rhs)
{
    return lhs.size() == rhs.size() && std::equal(lhs.begin(), lhs.end(), rhs.begin(), [](char left, char right) {
               return std::tolower(static_cast<unsigned char>(left)) == std::tolower(static_cast<unsigned char>(right));
           });
}

bool HasCapability(const ShaderDescriptor &descriptor, std::string_view capability)
{
    return std::any_of(descriptor.capabilities.begin(), descriptor.capabilities.end(),
                       [&](const std::string &candidate) { return EqualsInsensitive(candidate, capability); });
}

void Add(ShaderPassVariantPlan &plan, ShaderCompileTarget target, bool enabled, std::string reason,
         std::optional<ShaderCompileTarget> fallback = std::nullopt)
{
    plan.requirements.push_back({target, enabled, fallback, std::move(reason)});
}

} // namespace

const ShaderPassVariantRequirement *ShaderPassVariantPlan::Find(ShaderCompileTarget target) const noexcept
{
    const auto found = std::find_if(requirements.begin(), requirements.end(),
                                    [&](const auto &requirement) { return requirement.target == target; });
    return found != requirements.end() ? &*found : nullptr;
}

ShaderPassVariantPlan ShaderPassVariantPlanner::Plan(const ShaderDescriptor &vertex, const ShaderDescriptor &fragment,
                                                     const ShaderProgramInterfaceArtifact &interfaceArtifact,
                                                     bool shadingModelSupportsDeferred)
{
    ShaderPassVariantPlan plan;
    plan.stages = {vertex.shaderId, fragment.shaderId};
    if (!interfaceArtifact.IsValid()) {
        plan.diagnostics.push_back("pass variant planning requires a valid linked stage interface");
        return plan;
    }
    if (interfaceArtifact.vertex.shaderId != vertex.shaderId ||
        interfaceArtifact.fragment.shaderId != fragment.shaderId) {
        plan.diagnostics.push_back("pass variant planning received a stage interface for a different shader pair");
        return plan;
    }

    const bool transparent = EqualsInsensitive(fragment.surfaceOptions.surfaceType, "transparent");
    const bool forceForward = HasCapability(fragment, "ForceForward") || HasCapability(fragment, "ForwardOnly");
    const bool noDepth = HasCapability(fragment, "NoDepth") || HasCapability(fragment, "NoDepthPass");
    const bool noPicking = HasCapability(fragment, "NoPicking");
    const bool noMotion = HasCapability(vertex, "NoMotionVectors") || HasCapability(fragment, "NoMotionVectors");
    const bool noNormal = HasCapability(fragment, "NoNormal") || HasCapability(fragment, "NoNormalPass");
    const bool noBaseColor = HasCapability(fragment, "NoBaseColor") || HasCapability(fragment, "NoBaseColorPass");

    if (interfaceArtifact.domain == ShaderProgramDomain::ParticleSprite) {
        Add(plan, ShaderCompileTarget::Forward, true, "particle sprite materials require a Forward variant");
        Add(plan, ShaderCompileTarget::ForwardPlus, true,
            "particle sprite materials support the dedicated Particle Forward+ contract");
        Add(plan, ShaderCompileTarget::GBuffer, false, "particle sprites use the Forward fallback",
            ShaderCompileTarget::Forward);
        Add(plan, ShaderCompileTarget::Shadow, false,
            "particle shadow variants require the shared particle lighting contract");
        Add(plan, ShaderCompileTarget::Depth, false, "particle sprite depth variants are not published yet");
        Add(plan, ShaderCompileTarget::Picking, false, "particle outputs are not editor-pickable geometry");
        Add(plan, ShaderCompileTarget::Motion, !noMotion,
            noMotion ? "the particle shader explicitly disables motion vectors"
                     : "particle sprite outputs publish a camera-specific Motion variant");
        Add(plan, ShaderCompileTarget::Normal, false,
            "particle sprites do not participate in the opaque world-normal buffer");
        Add(plan, ShaderCompileTarget::BaseColor, false,
            "particle sprites do not participate in the opaque base-color buffer");
        return plan;
    }

    Add(plan, ShaderCompileTarget::Forward, true, "all linked material programs require a Forward variant");
    Add(plan, ShaderCompileTarget::ForwardPlus, true, "mesh material programs require a tiled Forward+ variant");
    const bool deferredCompatible = !transparent && !forceForward && shadingModelSupportsDeferred;
    const auto deferredFallback = !transparent && !forceForward && !shadingModelSupportsDeferred
                                      ? ShaderCompileTarget::ForwardPlus
                                      : ShaderCompileTarget::Forward;
    Add(plan, ShaderCompileTarget::GBuffer, deferredCompatible,
        transparent
            ? "transparent surfaces use the Forward fallback"
            : (forceForward ? "the fragment shader explicitly requires Forward"
                            : (!shadingModelSupportsDeferred ? "the shading model declares Unsupported [Deferred]"
                                                             : "opaque standard surfaces are Deferred candidates")),
        deferredCompatible ? std::nullopt : std::optional{deferredFallback});
    Add(plan, ShaderCompileTarget::Shadow, fragment.surfaceOptions.castShadows,
        fragment.surfaceOptions.castShadows ? "the fragment shader enables shadow casting"
                                            : "the fragment shader disables shadow casting");
    Add(plan, ShaderCompileTarget::Depth, !transparent && !noDepth,
        transparent ? "transparent surfaces do not enter the default depth prepass"
                    : (noDepth ? "the shader explicitly disables depth variants"
                               : "opaque surfaces participate in camera depth and depth prepasses"));
    Add(plan, ShaderCompileTarget::Picking, !noPicking,
        noPicking ? "the shader explicitly disables picking" : "renderable geometry requires object picking");
    Add(plan, ShaderCompileTarget::Motion, !noMotion,
        noMotion ? "the shader explicitly disables motion vectors"
                 : "object transforms and vertex deformation may contribute motion vectors");
    Add(plan, ShaderCompileTarget::Normal, !transparent && !noNormal,
        transparent ? "transparent surfaces do not enter the opaque normal buffer"
                    : (noNormal ? "the shader explicitly disables normal variants"
                                : "opaque geometry publishes world normals for screen-space effects"));
    Add(plan, ShaderCompileTarget::BaseColor, !transparent && !noBaseColor,
        transparent ? "transparent surfaces do not enter the opaque base-color buffer"
                    : (noBaseColor ? "the shader explicitly disables base-color variants"
                                   : "opaque geometry publishes linear albedo and alpha for the Geometry Stage"));
    return plan;
}

} // namespace infernux
