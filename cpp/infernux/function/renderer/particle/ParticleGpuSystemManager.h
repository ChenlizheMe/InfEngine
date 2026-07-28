#pragma once

#include "ParticleGpuBillboardRenderer.h"
#include "ParticleGpuBounds.h"
#include "ParticleGpuCollisionScene.h"
#include "ParticleGpuCuller.h"
#include "ParticleGpuEventDomain.h"
#include "ParticleGpuMeshRenderer.h"
#include "ParticleGpuMigrator.h"
#include "ParticleGpuRibbonRenderer.h"
#include "ParticleGpuRibbonTopology.h"
#include "ParticleGpuSorter.h"
#include "ParticleOutputSemantics.h"
#include "ParticleRenderGraph.h"

#include <core/types/ShaderProgramArtifact.h>
#include <function/resources/InxTexture/InxTexture.h>

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

class GpuRetirementQueue;
namespace vk
{
class VkDeviceContext;
class VkPipelineManager;
class VkResourceManager;
} // namespace vk

namespace particle
{

class ParticleGpuDrawRegistry;

enum class GpuParticleOutputType : uint8_t
{
    Sprite,
    Mesh,
    Ribbon,
};

struct GpuParticleOutputProgram
{
    uint64_t id = 0;
    std::string stableId;
    GpuParticleOutputType type = GpuParticleOutputType::Sprite;
    std::shared_ptr<InxMesh> mesh;
    std::shared_ptr<InxMaterial> material;
    std::shared_ptr<const ShaderProgramArtifact> shaderProgram;
    GpuBillboardMaterialState fallbackMaterial;
    ParticleOutputSemantics semantics;
    ParticleRibbonUvMode ribbonUvMode = ParticleRibbonUvMode::Stretch;
    float ribbonUvScale = 1.0f;
    uint32_t flipbookColumns = 1;
    uint32_t flipbookRows = 1;
};

struct GpuParticleMeshShapeProgram
{
    uint32_t metadataOffsetWords = 0;
    uint32_t vertexBinding = 14;
    uint32_t triangleBinding = 15;
    std::shared_ptr<InxMesh> mesh;
};

struct GpuParticleVectorFieldProgram
{
    GpuVectorFieldDesc::Kind kind = GpuVectorFieldDesc::Kind::VectorField;
    std::string stableId;
    uint32_t interfaceIndex = 0;
    uint32_t textureBinding = 0;
    bool worldSpace = true;
    bool linearFiltering = true;
    bool repeat = false;
    std::array<float, 16> fieldToSpace{};
    float vectorScale = 1.0f;
    std::shared_ptr<InxTexture> texture;
};

struct GpuParticleTexture2DParameterProgram
{
    std::string stableId;
    uint32_t resourceIndex = 0;
    uint32_t parameterSlot = 0;
    uint32_t textureBinding = 0;
    std::string textureGuid;
    std::shared_ptr<InxTexture> texture;
};

struct GpuParticleVectorFieldLayoutProgram
{
    uint32_t metadataBinding = 0;
    uint32_t interfaceStrideWords = 32;
    std::vector<GpuParticleVectorFieldProgram> vectorFields;
    std::vector<GpuParticleTexture2DParameterProgram> textureParameters;
};

using GpuParticleVectorFieldTextureResolver =
    std::function<GpuBillboardTextureLease(const std::string &textureGuid, bool linearFiltering, bool repeat)>;

struct GpuParticleEmitterProgram
{
    uint64_t id = 0;
    uint64_t graphInstanceId = 0;
    uint32_t graphEmitterIndex = 0;
    uint64_t ownerObjectId = 0;
    uint32_t ownerLayerMask = 1u;
    uint64_t artifactRevision = 0;
    std::string stableId;
    uint32_t capacity = 0;
    uint32_t stateStride = 0;
    uint32_t eventOutputStageMask = 0;
    std::vector<uint32_t> parameterWords;
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
    std::vector<uint32_t> eventInitKernel;
    uint32_t continuationCapacity = 0;
    uint32_t continuationRecordStride = 0;
    uint32_t continuationLaneCount = 0;
    uint32_t continuationJoinCount = 0;
    std::array<std::vector<uint32_t>, static_cast<size_t>(GpuParticleContinuationKernelStage::Count)>
        continuationKernels;
    std::optional<GpuParticleMeshShapeProgram> meshShape;
    GpuParticleVectorFieldLayoutProgram vectorFields;
    std::vector<uint32_t> billboardVertexShader;
    std::vector<uint32_t> billboardFragmentShader;
    std::vector<uint32_t> billboardForwardPlusFragmentShader;
    std::vector<uint32_t> billboardPickingFragmentShader;
    std::vector<uint32_t> billboardMotionVertexShader;
    std::vector<uint32_t> billboardMotionFragmentShader;
    std::vector<uint32_t> meshVertexShader;
    std::vector<uint32_t> meshFragmentShader;
    std::vector<uint32_t> meshForwardPlusFragmentShader;
    std::vector<uint32_t> meshPickingFragmentShader;
    std::vector<uint32_t> meshMotionVertexShader;
    std::vector<uint32_t> meshMotionFragmentShader;
    std::vector<GpuParticleOutputProgram> outputs;
};

struct GpuParticleBatchFrameItem
{
    uint64_t emitterId = 0;
    GpuParticleFrameRequest request;
    GpuParticleTransforms transforms;
};

/// One authoritative publication transaction for a live ParticleGraph.
/// Event transport belongs to the graph, never to an individual emitter.
struct GpuParticleGraphProgram
{
    uint64_t graphInstanceId = 0;
    std::vector<GpuParticleEmitterProgram> emitters;
    std::vector<uint64_t> removeEmitterIds;
    std::optional<GpuParticleEventDomainDesc> eventDomain;
};

/// CPU-side scheduling telemetry for the resident GPU particle world.
/// This deliberately avoids reading particle counters back from the GPU.
struct GpuParticleTelemetrySnapshot
{
    size_t systemCount = 0;
    size_t outputCount = 0;
    uint64_t totalCapacity = 0;
    uint64_t lastScheduledFrame = 0;
    size_t scheduledSystemCount = 0;
    size_t simulatingSystemCount = 0;
    size_t renderingSystemCount = 0;
    uint64_t requestedSpawnCount = 0;
    size_t continuationSystemCount = 0;
    uint64_t totalContinuationCapacity = 0;
    uint32_t maximumContinuationProgramGeneration = 0;
    uint64_t continuationPrepareRecordCalls = 0;
    uint64_t continuationClassifyRecordCalls = 0;
    uint64_t continuationDispatchRecordCalls = 0;
    size_t continuationResetPendingCount = 0;
    uint64_t collisionSceneRevision = 0;
    uint32_t collisionSceneColliderCount = 0;
    uint64_t collisionSceneTopologyRevision = 0;
    uint32_t collisionSceneMeshVertexCount = 0;
    uint32_t collisionSceneMeshIndexCount = 0;
    uint32_t collisionSceneMeshBvhNodeCount = 0;
};

enum class GpuParticleDiagnosticStatus : uint8_t
{
    Pending,
    Completed,
    Inactive,
    Failed,
    Unknown,
};

struct GpuParticleEmitterDiagnostic
{
    uint64_t emitterId = 0;
    uint32_t emitterIndex = 0;
    uint32_t capacity = 0;
    uint32_t freeCount = 0;
    uint32_t aliveCount = 0;
    uint32_t visibleCount = 0;
    uint32_t droppedCount = 0;
    GpuParticleBoundsMode boundsMode = GpuParticleBoundsMode::Automatic;
    bool boundsValid = false;
    std::array<float, 3> boundsLower{};
    std::array<float, 3> boundsUpper{};
};

struct GpuParticleEventDiagnostic
{
    uint32_t channelIndex = 0;
    uint64_t stableEventTypeHash = 0;
    uint32_t sourceEmitterIndex = 0;
    uint32_t targetEmitterIndex = 0;
    uint32_t eventTypeIndex = 0;
    uint32_t spawnCount = 0;
    uint64_t preparedEpoch = 0;
    uint32_t readPageIndex = 0;
    uint32_t writePageIndex = 0;
    uint32_t producedCount = 0;
    uint32_t producerDroppedCount = 0;
    uint32_t consumedCount = 0;
    uint32_t targetDroppedCount = 0;
    uint64_t spawnedCount = 0;
};

struct GpuParticleDiagnosticSnapshot
{
    uint64_t requestId = 0;
    uint64_t graphInstanceId = 0;
    GpuParticleDiagnosticStatus status = GpuParticleDiagnosticStatus::Unknown;
    std::vector<GpuParticleEmitterDiagnostic> emitters;
    std::vector<GpuParticleEventDiagnostic> events;
    std::string error;
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

    [[nodiscard]] bool Initialize(
        vk::VkDeviceContext &context, vk::VkPipelineManager &pipelines, vk::VkResourceManager &resources,
        GpuRetirementQueue &deletionQueue, ParticleGpuDrawRegistry &drawRegistry,
        GpuBillboardTextureResolver textureResolver = {},
        GpuParticleVectorFieldTextureResolver vectorFieldTextureResolver = {},
        const GpuParticleSortProgram &sortProgram = {}, const GpuParticleCullProgram &cullProgram = {},
        const GpuParticleBoundsProgram &boundsProgram = {}, const GpuParticleMigrationProgram &migrationProgram = {},
        const GpuParticleEventProgram &eventProgram = {}, const GpuParticleRibbonProgram &ribbonTopologyProgram = {},
        const GpuParticleRibbonRenderProgram &ribbonRenderProgram = {}, uint32_t framesInFlight = 2);
    void Shutdown() noexcept;

    /// Compile then publish one complete graph transaction. The active graph
    /// remains untouched when any runtime, renderer, event ABI, or RenderGraph
    /// compilation step fails.
    [[nodiscard]] bool ApplyGraph(const GpuParticleGraphProgram &program, std::string *error = nullptr);
    /// Update graph-instance parameters in place. No shader, pipeline, or
    /// particle-state resource is rebuilt.
    [[nodiscard]] bool UpdateGraphParameters(uint64_t graphInstanceId, const std::vector<uint32_t> &parameterWords,
                                             std::string *error = nullptr);
    /// Queue ABI-packed gameplay events for a graph route. Records are copied
    /// into the next GPU event page at the simulation boundary.
    [[nodiscard]] bool QueueExternalEvents(uint64_t graphInstanceId, uint32_t channelIndex,
                                           const std::vector<uint32_t> &recordWords, uint32_t recordCount,
                                           std::string *error = nullptr);
    /// Publish a scene-owned collider snapshot. No GPU work is recorded until
    /// the next particle simulation boundary.
    [[nodiscard]] bool PublishCollisionScene(const GpuParticleCollisionSceneSnapshot &snapshot,
                                             std::string *error = nullptr);
    [[nodiscard]] uint64_t CollisionSceneRevision() const noexcept;
    [[nodiscard]] uint32_t CollisionSceneColliderCount() const noexcept;
    /// Replace only render resources that reference this live material. The
    /// simulation runtime and all surviving particles remain untouched.
    [[nodiscard]] bool RefreshMaterialProgram(const std::shared_ptr<InxMaterial> &material,
                                              std::shared_ptr<const ShaderProgramArtifact> shaderProgram,
                                              std::string *error = nullptr);
    void Clear();

    [[nodiscard]] bool BeginFrame(uint64_t id, const GpuParticleFrameRequest &request,
                                  const GpuParticleTransforms &transforms);
    /// Atomically arms all GPU emitters belonging to one ParticleGraph
    /// instance. No scheduler is armed until the complete batch validates and
    /// all transform uploads succeed.
    [[nodiscard]] bool BeginFrameBatch(uint64_t graphInstanceId, const std::vector<GpuParticleBatchFrameItem> &items);
    [[nodiscard]] bool Reset(uint64_t id);
    void Execute(VkCommandBuffer commandBuffer);
    /// Record the pre-export simulation phase on an independent Compute queue.
    [[nodiscard]] bool RecordAsyncSimulation(VkCommandBuffer commandBuffer);
    /// Record Rendering/Bounds after the current Graphics frame has consumed
    /// the previous exported particle output.
    [[nodiscard]] bool RecordAsyncExport(VkCommandBuffer commandBuffer);
    [[nodiscard]] bool CanExecuteAsync() const noexcept;
    /// Changes whenever a simulation graph is published. Frame scheduling
    /// uses this to synchronously prime newly exported render data once.
    [[nodiscard]] uint64_t AsyncExecutionGeneration() const noexcept;

    [[nodiscard]] bool Contains(uint64_t id) const;
    [[nodiscard]] size_t Size() const;
    [[nodiscard]] GpuParticleTelemetrySnapshot TelemetrySnapshot() const;
    [[nodiscard]] uint64_t ActiveArtifactRevision(uint64_t id) const;
    [[nodiscard]] bool ActiveStateWasPreserved(uint64_t id) const;
    [[nodiscard]] size_t ActiveOutputCount(uint64_t id) const;
    [[nodiscard]] uint64_t ActiveVectorFieldGeneration(uint64_t id, uint32_t interfaceIndex) const;
    [[nodiscard]] int32_t ActiveOutputRenderQueue(uint64_t emitterId, uint64_t outputId) const;
    [[nodiscard]] std::optional<ParticleOutputSemantics> ActiveOutputSemantics(uint64_t emitterId,
                                                                               uint64_t outputId) const;
    [[nodiscard]] uint64_t ActiveEventAbiHash(uint64_t graphInstanceId) const;
    [[nodiscard]] uint64_t ActiveEventDomainSerial(uint64_t graphInstanceId) const;
    [[nodiscard]] uint32_t ActiveEventPageCount(uint64_t graphInstanceId) const;
    /// Arm one counter-and-bounds snapshot. No transfer or readback resource
    /// exists until this method is called, and completion never stalls the renderer.
    [[nodiscard]] uint64_t RequestDiagnostics(uint64_t graphInstanceId);
    [[nodiscard]] GpuParticleDiagnosticSnapshot QueryDiagnostics(uint64_t requestId) const;

  private:
    struct Impl;
    std::unique_ptr<Impl> m_impl;
};

} // namespace particle
} // namespace infernux
