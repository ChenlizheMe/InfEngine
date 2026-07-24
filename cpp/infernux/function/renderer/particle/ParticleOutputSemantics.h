#pragma once

#include <array>
#include <cmath>
#include <cstdint>

namespace infernux::particle
{

enum class ParticleSortMode : uint8_t
{
    None,
    BackToFront,
    FrontToBack,
};

enum class ParticleSpriteAlignment : uint8_t
{
    CameraPlane,
    CameraPosition,
    Axis,
    Velocity,
};

/// Camera-dependent rendering choices authored by one Particle Output. These
/// are deliberately separate from the once-per-frame simulation program.
struct ParticleOutputSemantics
{
    bool receiveSceneLighting = false;
    bool receiveShadows = false;
    bool castShadows = false;
    bool softParticles = false;
    float softDistance = 1.0f;
    ParticleSortMode sortMode = ParticleSortMode::BackToFront;
    ParticleSpriteAlignment spriteAlignment = ParticleSpriteAlignment::CameraPlane;
    std::array<float, 3> alignmentAxis{0.0f, 1.0f, 0.0f};

    [[nodiscard]] bool IsValid() const noexcept
    {
        const float axisLengthSquared = alignmentAxis[0] * alignmentAxis[0] + alignmentAxis[1] * alignmentAxis[1] +
                                        alignmentAxis[2] * alignmentAxis[2];
        return (!receiveShadows || receiveSceneLighting) && std::isfinite(softDistance) && softDistance > 0.0f &&
               std::isfinite(alignmentAxis[0]) && std::isfinite(alignmentAxis[1]) && std::isfinite(alignmentAxis[2]) &&
               spriteAlignment <= ParticleSpriteAlignment::Velocity &&
               (spriteAlignment != ParticleSpriteAlignment::Axis || axisLengthSquared > 1.0e-12f);
    }
};

} // namespace infernux::particle
