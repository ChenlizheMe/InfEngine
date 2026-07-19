#pragma once

#include "ParticleGpuBillboardRenderer.h"
#include "ParticleGpuBounds.h"
#include "ParticleGpuCuller.h"
#include "ParticleGpuMigrator.h"
#include "ParticleGpuSorter.h"
#include "ParticleOutputSemantics.h"
#include "ParticleRenderGraph.h"

#include <core/types/ShaderProgramArtifact.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include <vulkan/vulkan.h>

namespace infernux
{

class FrameDeletionQueue;
namespace vk
{
class VkDeviceContext;
class VkPipelineManager;
} // namespace vk

namespace particle
{

class ParticleGpuDrawRegistry;

struct GpuParticleOutputProgram
{
    uint64_t id = 0;
    std::string stableId;
    std::shared_ptr<InxMaterial> material;
    std::shared_ptr<const ShaderProgramArtifact> shaderProgram;
    GpuBillboardMaterialState fallbackMaterial;
    ParticleOutputSemantics semantics;
};

struct GpuParticleEmitterProgram
{
    uint64_t id = 0;
    uint64_t artifactRevision = 0;
    std::string stableId;
    uint32_t capacity = 0;
    uint32_t stateStride = 0;
    bool preserveState = false;
    struct StateMigration
    {
        uint32_t sourceStride = 0;
        uint32_t destinationStride = 0;
        std::vector<GpuParticleMigrationRange> copyRanges;
        std::vector<uint32_t> defaultStateWords;
    };
    std::optional<StateMigration> migration;
    std::array<std::vector<uint32_t>, static_cast<size_t>(GpuKernelStage::Count)> kernels;
    std::vector<uint32_t> billboardVertexShader;
    std::vector<uint32_t> billboardFragmentShader;
    std::vector<GpuParticleOutputProgram> outputs;
};

/// Owns all GPU particle emitters and their once-per-engine-frame simulation
/// graph. Camera graphs consume only the exported instance/indirect buffers.
class ParticleGpuSystemManager
{
  public:
    ParticleGpuSystemManager();
    ~ParticleGpuSystemManager();

    ParticleGpuSystemManager(const ParticleGpuSystemManager &) = delete;
    ParticleGpuSystemManager &operator=(const ParticleGpuSystemManager &) = delete;
    ParticleGpuSystemManager(ParticleGpuSystemManager &&) = delete;
    ParticleGpuSystemManager &operator=(ParticleGpuSystemManager &&) = delete;

    [[nodiscard]] bool Initialize(vk::VkDeviceContext &context, vk::VkPipelineManager &pipelines,
                                  FrameDeletionQueue &deletionQueue, ParticleGpuDrawRegistry &drawRegistry,
                                  GpuBillboardTextureResolver textureResolver = {},
                                  GpuBillboardTextureVersionResolver textureVersionResolver = {},
                                  const GpuParticleSortProgram &sortProgram = {},
                                  const GpuParticleCullProgram &cullProgram = {},
                                  const GpuParticleBoundsProgram &boundsProgram = {},
                                  const GpuParticleMigrationProgram &migrationProgram = {});
    void Shutdown() noexcept;

    /// Compile-then-publish replacement. The active emitter remains untouched
    /// when any resource, pipeline, or graph compilation step fails.
    [[nodiscard]] bool CreateOrReplace(const GpuParticleEmitterProgram &program, std::string *error = nullptr);
    /// Atomically replace every supplied emitter after all runtimes, renderers,
    /// and the aggregate simulation graph have compiled successfully.
    [[nodiscard]] bool CreateOrReplaceBatch(const std::vector<GpuParticleEmitterProgram> &programs,
                                            std::string *error = nullptr);
    /// Apply replacements and removals as one publication transaction. This is
    /// used when a saved ParticleGraph adds or removes emitters during reload.
    [[nodiscard]] bool ApplyBatch(const std::vector<GpuParticleEmitterProgram> &programs,
                                  const std::vector<uint64_t> &removeIds, std::string *error = nullptr);
    /// Replace only render resources that reference this live material. The
    /// simulation runtime and all surviving particles remain untouched.
    [[nodiscard]] bool RefreshMaterialProgram(const std::shared_ptr<InxMaterial> &material,
                                              std::shared_ptr<const ShaderProgramArtifact> shaderProgram,
                                              std::string *error = nullptr);
    [[nodiscard]] bool Remove(uint64_t id);
    void Clear();

    [[nodiscard]] bool BeginFrame(uint64_t id, const GpuParticleFrameRequest &request,
                                  const GpuParticleTransforms &transforms);
    [[nodiscard]] bool Reset(uint64_t id);
    void Execute(VkCommandBuffer commandBuffer);

    [[nodiscard]] bool Contains(uint64_t id) const;
    [[nodiscard]] size_t Size() const;
    [[nodiscard]] uint64_t ActiveArtifactRevision(uint64_t id) const;
    [[nodiscard]] bool ActiveStateWasPreserved(uint64_t id) const;
    [[nodiscard]] size_t ActiveOutputCount(uint64_t id) const;
    [[nodiscard]] int32_t ActiveOutputRenderQueue(uint64_t emitterId, uint64_t outputId) const;
    [[nodiscard]] std::optional<ParticleOutputSemantics> ActiveOutputSemantics(uint64_t emitterId,
                                                                               uint64_t outputId) const;

  private:
    struct Impl;
    std::unique_ptr<Impl> m_impl;
};

} // namespace particle
} // namespace infernux
