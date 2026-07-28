#pragma once

#include "ParticleGpuCuller.h"

#include <cstdint>
#include <string>
#include <vector>

namespace infernux::particle
{

enum class GpuParticleViewDiagnosticStatus : uint8_t
{
    Unknown,
    Pending,
    Completed,
    Failed,
};

struct GpuParticleViewOutputDiagnostic
{
    uint64_t outputId = 0;
    uint64_t emitterId = 0;
    uint32_t emitterIndex = 0;
    std::string outputStableId;
    uint32_t capacity = 0;
    uint32_t sourceCount = 0;
    uint32_t visibleCount = 0;
    uint32_t drawVertexCount = 0;
    uint32_t drawInstanceCount = 0;
    bool boundsValid = false;
    bool coarseRejected = false;
    GpuParticleCullMode cullMode = GpuParticleCullMode::Instances;
};

struct GpuParticleViewDiagnosticSnapshot
{
    uint64_t requestId = 0;
    uint64_t graphInstanceId = 0;
    GpuParticleViewDiagnosticStatus status = GpuParticleViewDiagnosticStatus::Unknown;
    std::vector<GpuParticleViewOutputDiagnostic> outputs;
    std::string error;
};

} // namespace infernux::particle
