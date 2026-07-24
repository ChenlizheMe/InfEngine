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
    case ShaderCompileTarget::Count:
        return "Count";
    }
    return "Unknown";
}

} // namespace infernux
