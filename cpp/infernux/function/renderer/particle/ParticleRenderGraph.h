#pragma once

#include "ParticleGpuBounds.h"
#include "ParticleGpuMigrator.h"
#include "ParticleGpuRuntime.h"

#include <function/renderer/vk/RenderGraph.h>

#include <cstdint>
#include <string>

namespace infernux::particle
{

struct GpuParticleFrameRequest
{
    uint64_t frameIndex = 0;
    uint32_t spawnCount = 0;
    uint32_t spawnBaseId = 0;
    uint32_t spawnGeneration = 0;
    uint32_t systemSeed = 0;
    uint32_t simulationStep = 0;
    float deltaTime = 0.0f;
    bool simulate = true;
    bool render = true;
};

struct GpuParticleGraphOutputs
{
    vk::ResourceHandle instances;
    vk::ResourceHandle renderIndices;
    vk::ResourceHandle indirectArguments;
    vk::ResourceHandle bounds;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return instances.IsValid() && renderIndices.IsValid() && indirectArguments.IsValid() && bounds.IsValid();
    }
};

/// Declares one GPU emitter's simulation and render-export stages in a
/// RenderGraph. BeginFrame arms a one-shot request: a second graph execution
/// in the same engine frame records no particle work, so multiple cameras
/// cannot accidentally advance the simulation more than once. This scheduler
/// and its ParticleGpuRuntime must outlive callbacks stored in the graph.
class ParticleRenderGraph
{
  public:
    ParticleRenderGraph() = default;
    ~ParticleRenderGraph() = default;

    ParticleRenderGraph(const ParticleRenderGraph &) = delete;
    ParticleRenderGraph &operator=(const ParticleRenderGraph &) = delete;
    ParticleRenderGraph(ParticleRenderGraph &&) = delete;
    ParticleRenderGraph &operator=(ParticleRenderGraph &&) = delete;

    [[nodiscard]] bool Attach(vk::RenderGraph &graph, ParticleGpuRuntime &runtime, ParticleGpuBounds &bounds,
                              const std::string &namePrefix, ParticleGpuMigrator *migration = nullptr);
    [[nodiscard]] bool BeginFrame(const GpuParticleFrameRequest &request) noexcept;
    void Reset() noexcept;
    [[nodiscard]] bool HasCompletedMigration() const noexcept
    {
        return m_migrationCompleted;
    }
    [[nodiscard]] bool ConsumeMigrationCompletion() noexcept;

    [[nodiscard]] bool IsAttached() const noexcept
    {
        return m_runtime != nullptr && m_outputs.IsValid();
    }
    [[nodiscard]] bool HasPendingFrame() const noexcept
    {
        return m_framePending;
    }
    [[nodiscard]] uint64_t LastConsumedFrame() const noexcept
    {
        return m_lastConsumedFrame;
    }
    [[nodiscard]] const GpuParticleGraphOutputs &Outputs() const noexcept
    {
        return m_outputs;
    }

  private:
    ParticleGpuRuntime *m_runtime = nullptr;
    ParticleGpuBounds *m_bounds = nullptr;
    ParticleGpuMigrator *m_migrator = nullptr;
    GpuParticleFrameRequest m_request{};
    GpuParticleGraphOutputs m_outputs{};
    bool m_bootstrapPending = true;
    bool m_migrationPending = false;
    bool m_migrationCompleted = false;
    bool m_framePending = false;
    bool m_hasConsumedFrame = false;
    uint64_t m_lastConsumedFrame = 0;
};

} // namespace infernux::particle
