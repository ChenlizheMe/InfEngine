#pragma once

#include <cstdint>

namespace infernux::particle
{

enum class ParticleSortMode : uint8_t
{
    None,
    BackToFront,
    FrontToBack,
};

/// Camera-dependent rendering choices authored by one Particle Output. These
/// are deliberately separate from the once-per-frame simulation program.
struct ParticleOutputSemantics
{
    bool receiveSceneLighting = false;
    bool receiveShadows = false;
    ParticleSortMode sortMode = ParticleSortMode::BackToFront;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return !receiveShadows || receiveSceneLighting;
    }
};

} // namespace infernux::particle
