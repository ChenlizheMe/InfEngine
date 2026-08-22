#pragma once

namespace infernux
{

enum class ShaderProgramDomain : unsigned char
{
    Mesh = 0,
    ParticleSprite,

    Count,
};

[[nodiscard]] constexpr const char *ShaderProgramDomainName(ShaderProgramDomain domain) noexcept
{
    switch (domain) {
    case ShaderProgramDomain::Mesh:
        return "Mesh";
    case ShaderProgramDomain::ParticleSprite:
        return "ParticleSprite";
    case ShaderProgramDomain::Count:
        return "Count";
    }
    return "Unknown";
}

// ============================================================================
// ShaderCompileTarget — identifies which rendering pass variant to compile for.
//
// Shared between InxShaderLoader (compile-time variant generation) and
// InxMaterial (per-pass pipeline storage).  Defined in a lightweight header
// to avoid pulling in heavy shader-compiler includes through InxMaterial.h.
// ============================================================================

enum class ShaderCompileTarget : int
{
    Forward = 0,     // Standard forward rendering (default)
    GBuffer = 1,     // Deferred GBuffer output
    Shadow = 2,      // Depth-only shadow caster
    Depth = 3,       // Camera depth/depth-prepass output
    Picking = 4,     // Stable object identity output
    Motion = 5,      // Motion-vector output
    ForwardPlus = 6, // Tiled/clustered forward lighting
    Normal = 7,      // World-space normal output for screen-space effects
    BaseColor = 8,   // Linear surface albedo + alpha output for the Geometry Stage

    Count // Sentinel — number of targets; must be last
};

[[nodiscard]] constexpr const char *ShaderCompileTargetName(ShaderCompileTarget target) noexcept
{
    switch (target) {
    case ShaderCompileTarget::Forward:
        return "Forward";
    case ShaderCompileTarget::GBuffer:
        return "GBuffer";
    case ShaderCompileTarget::Shadow:
        return "Shadow";
    case ShaderCompileTarget::Depth:
        return "Depth";
    case ShaderCompileTarget::Picking:
        return "Picking";
    case ShaderCompileTarget::Motion:
        return "Motion";
    case ShaderCompileTarget::ForwardPlus:
        return "ForwardPlus";
    case ShaderCompileTarget::Normal:
        return "Normal";
    case ShaderCompileTarget::BaseColor:
        return "BaseColor";
    case ShaderCompileTarget::Count:
        return "Count";
    }
    return "Unknown";
}

/// Geometry variants that consume InstanceAuxBuffer for object identity,
/// transform history or the engine-owned layer mask must upload that stream
/// before drawing. Keep this contract beside the compile target enum so the
/// shader linker and draw path cannot silently diverge.
[[nodiscard]] constexpr bool ShaderCompileTargetUsesInstanceAuxiliary(ShaderCompileTarget target) noexcept
{
    switch (target) {
    case ShaderCompileTarget::Forward:
    case ShaderCompileTarget::ForwardPlus:
    case ShaderCompileTarget::GBuffer:
    case ShaderCompileTarget::Picking:
    case ShaderCompileTarget::Motion:
        return true;
    case ShaderCompileTarget::Normal:
    case ShaderCompileTarget::BaseColor:
    case ShaderCompileTarget::Shadow:
    case ShaderCompileTarget::Depth:
    case ShaderCompileTarget::Count:
        return false;
    }
    return false;
}

} // namespace infernux
