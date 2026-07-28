#pragma once

#include <array>
#include <cstddef>
#include <cstdint>

namespace infernux::particle
{

/// Canonical GPU ABI exported by the camera-independent Rendering stage.
///
/// Instances are addressed by persistent particle state slot. The compact
/// render-index stream contains the live slot IDs consumed by bounds, culling,
/// sorting and output renderers. This stable addressing lets one slot retain
/// its previous world-space center without a second history buffer.
struct alignas(16) GpuParticleRenderInstance
{
    std::array<float, 4> positionSize{};
    std::array<float, 4> color{};
    std::array<float, 4> rotationCustom{};
    std::array<float, 4> scaleCustom{};
    std::array<uint32_t, 4> ribbonData{};
    std::array<float, 4> customData{};
    // xyz: previous world-space center; w: current spawn generation bits.
    std::array<float, 4> previousPositionHistory{};
};

static_assert(alignof(GpuParticleRenderInstance) == 16);
static_assert(sizeof(GpuParticleRenderInstance) == 112);
static_assert(offsetof(GpuParticleRenderInstance, previousPositionHistory) == 96);

} // namespace infernux::particle
