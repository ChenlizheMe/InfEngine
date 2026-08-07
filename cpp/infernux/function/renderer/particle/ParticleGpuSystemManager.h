#pragma once

#include "ParticleGpuBillboardRenderer.h"
#include "ParticleGpuBounds.h"
#include "ParticleGpuCollisionScene.h"
#include "ParticleGpuCuller.h"
#include "ParticleGpuMeshRenderer.h"
#include "ParticleGpuMigrator.h"
#include "ParticleGpuRibbonRenderer.h"
#include "ParticleGpuRibbonTopology.h"
#include "ParticleGpuSorter.h"
#include "ParticleOutputSemantics.h"
#include "ParticleRenderGraph.h"

#include <core/types/ShaderProgramArtifact.h>
#include <function/resources/InxTexture/InxTexture.h>
#include <function/scene/ObjectHandle.h>

#include <array>
#include <cstddef>
#include <cstdint>
#include <functional>
#include <limits>
#include <memory>
#include <optional>
#include <string>
#include <vector>

#include <glm/glm.hpp>
#include <vulkan/vulkan.h>

namespace infernux
{

class GpuRetirementQueue;
class InxSkinnedMesh;
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

struct GpuParticleMeshInterfaceProgram
{
    std::string stableId;
    uint32_t interfaceIndex = 0;
    uint32_t metadataOffsetWords = 0;
    uint32_t vertexBinding = 1;
    uint32_t triangleBinding = 2;
    uint32_t influenceBinding = 3;
    uint32_t paletteBinding = 4;
    bool worldSpace = false;
    std::array<float, 16> meshToSpace{};
    std::shared_ptr<InxMesh> mesh;
    ObjectHandle skinnedRenderer;
};

struct GpuParticleSkinnedMeshSnapshot
{
    std::shared_ptr<InxMesh> mesh;
    std::shared_ptr<const InxSkinnedMesh> model;
    std::shared_ptr<const std::vector<glm::mat4>> currentPalette;
    std::shared_ptr<const std::vector<glm::mat4>> previousPalette;
    std::array<float, 16> sourceToWorld{};
    uint64_t revision = 0;
};

using GpuParticleSkinnedMeshResolver =
    std::function<std::optional<GpuParticleSkinnedMeshSnapshot>(const ObjectHandle &renderer)>;

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
    uint32_t eventTypeCount = 0;
    bool collisionEnabled = false;
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
    uint32_t continuationCapacity = 0;
    uint32_t continuationRecordStride = 0;
    uint32_t continuationLaneCount = 0;
    uint32_t continuationJoinCount = 0;
    std::array<std::vector<uint32_t>, static_cast<size_t>(GpuParticleContinuationKernelStage::Count)>
        continuationKernels;
    std::vector<GpuParticleMeshInterfaceProgram> meshInterfaces;
    GpuParticleVectorFieldLayoutProgram vectorFields;
    std::vector<uint32_t> billboardVertexShader;
    std::vector<uint32_t> billboardPickingFragmentShader;
    std::vector<uint32_t> billboardMotionVertexShader;
    std::vector<uint32_t> billboardMotionFragmentShader;
    std::vector<uint32_t> meshVertexShader;
    std::vector<uint32_t> meshShadowFragmentShader;
    std::vector<uint32_t> meshPickingFragmentShader;
    std::vector<uint32_t> meshMotionVertexShader;
    std::vector<uint32_t> meshMotionFragmentShader;
    std::vector<GpuParticleOutputProgram> outputs;
};

struct GpuParticleBatchFrameItem
{
    uint64_t emitterId = 0;
    /// Ordered fixed-step simulation requests that must complete before the
    /// visible frame request. Used by both Prewarm and deterministic Seek.
    std::vector<GpuParticleFrameRequest> prerollRequests;
    GpuParticleFrameRequest request;
    GpuParticleTransforms transforms;
};

/// One authoritative publication transaction for a live ParticleGraph.
struct GpuParticleGraphProgram
{
    uint64_t graphInstanceId = 0;
    std::vector<GpuParticleEmitterProgram> emitters;
    std::vector<uint64_t> removeEmitterIds;
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
    size_t contactRuntimeSystemCount = 0;
    uint64_t totalContactRecordCapacity = 0;
    uint64_t totalContactWorkItemCapacity = 0;
    uint64_t totalContactResidentBytes = 0;
    uint64_t contactPrepareRecordCalls = 0;
    uint64_t contactSolveRecordCalls = 0;
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
    struct StateSample
    {
        uint32_t slotIndex = 0;
        uint32_t lifecycleFlags = 0;
        uint32_t spawnGeneration = 0;
        std::vector<uint32_t> words;
    };

    uint64_t emitterId = 0;
    uint32_t emitterIndex = 0;
    uint32_t capacity = 0;
    uint32_t freeCount = 0;
    uint32_t aliveCount = 0;
    uint32_t visibleCount = 0;
    uint32_t droppedCount = 0;
    uint32_t collisionHitCount = 0;
    uint32_t collisionResponseCount = 0;
    uint32_t collisionTriggerCount = 0;
    uint32_t collisionEnterCount = 0;
    uint32_t collisionStayCount = 0;
    uint32_t collisionExitCount = 0;
    float collisionMaxOutwardSpeed = 0.0f;
    float collisionMaxTangentSpeed = 0.0f;
    uint32_t collisionCandidateOverflowCount = 0;
    uint32_t contactOverflowCount = 0;
    uint32_t contactWorkItemOverflowCount = 0;
    uint32_t contactCurrentSimulationStep = 0;
    uint32_t contactResetSerial = 0;
    uint32_t contactCurrentRecordCount = 0;
    uint32_t contactWorkItemCount = 0;
    uint32_t contactMaxPerParticle = 0;
    uint32_t multiContactParticleCount = 0;
    uint32_t contactRetainedOrderHash = 0;
    uint32_t contactDroppedOrderHash = 0;
    uint32_t contactMinParticleIndex = std::numeric_limits<uint32_t>::max();
    uint32_t contactMaxParticleIndex = 0;
    uint32_t preparedSpawnCount = 0;
    uint32_t preparedSpawnBaseId = 0;
    uint32_t preparedSpawnGeneration = 0;
    uint32_t spawnOverflowCount = 0;
    uint32_t acceptedSpawnTotal = 0;
    uint32_t queuedBurstCount = 0;
    uint32_t consumingBurstCount = 0;
    bool acceptingBurstRequests = false;
    bool gpuEmitterPlaying = false;
    std::vector<uint32_t> eventOverflowCounts;
    std::vector<uint32_t> eventEnqueueCounts;
    std::vector<uint32_t> eventCompleteCounts;
    GpuParticleBoundsMode boundsMode = GpuParticleBoundsMode::Automatic;
    bool boundsValid = false;
    std::array<float, 3> boundsLower{};
    std::array<float, 3> boundsUpper{};
    std::vector<StateSample> stateSamples;
};

struct GpuParticleDiagnosticSnapshot
{
    uint64_t requestId = 0;
    uint64_t graphInstanceId = 0;
    GpuParticleDiagnosticStatus status = GpuParticleDiagnosticStatus::Unknown;
    std::vector<GpuParticleEmitterDiagnostic> emitters;
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

    [[nodiscard]] bool
    Initialize(vk::VkDeviceContext &context, vk::VkPipelineManager &pipelines, vk::VkResourceManager &resources,
               GpuRetirementQueue &deletionQueue, ParticleGpuDrawRegistry &drawRegistry,
               GpuBillboardTextureResolver textureResolver = {},
               GpuParticleVectorFieldTextureResolver vectorFieldTextureResolver = {},
               GpuParticleSkinnedMeshResolver skinnedMeshResolver = {}, const GpuParticleSortProgram &sortProgram = {},
               const GpuParticleCullProgram &cullProgram = {}, const GpuParticleBoundsProgram &boundsProgram = {},
               const GpuParticleMigrationProgram &migrationProgram = {},
               const GpuParticleSpawnProgram &spawnProgram = {},
               const GpuParticleRibbonProgram &ribbonTopologyProgram = {},
               const GpuParticleRibbonRenderProgram &ribbonRenderProgram = {}, uint32_t framesInFlight = 2);
    void Shutdown() noexcept;

    /// Compile then publish one complete graph transaction. The active graph
    /// remains untouched when any runtime, renderer, or RenderGraph
    /// compilation step fails.
    [[nodiscard]] bool ApplyGraph(const GpuParticleGraphProgram &program, std::string *error = nullptr);
    /// Update graph-instance parameters in place. No shader, pipeline, or
    /// particle-state resource is rebuilt.
    [[nodiscard]] bool UpdateGraphParameters(uint64_t graphInstanceId, const std::vector<uint32_t> &parameterWords,
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
    /// Update one emitter's graph-owned runtime playing state without rebuilding resources.
    [[nodiscard]] bool SetEmitterPlaying(uint64_t id, bool playing);
    [[nodiscard]] bool Reset(uint64_t id);
    void Execute(VkCommandBuffer commandBuffer);
    /// Record the pre-export simulation phase on an independent Compute queue.
    [[nodiscard]] bool RecordAsyncSimulation(VkCommandBuffer commandBuffer);
    /// Record Rendering/Bounds after the current Graphics frame has consumed
    /// the previous exported particle output.
    [[nodiscard]] bool RecordAsyncExport(VkCommandBuffer commandBuffer);
    [[nodiscard]] bool CanExecuteAsync() const noexcept;
    /// Returns true only when the frame compute callback would record useful
    /// particle work (simulation, collision upload, or diagnostics).
    [[nodiscard]] bool HasPendingGpuWork() const noexcept;
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
    /// Arm one counter-and-bounds snapshot. Collision counters are sampled by
    /// the next update and accumulate only across diagnostic sample frames for
    /// the current resident state. No transfer or readback resource exists
    /// until this method is called, and completion never stalls the renderer.
    [[nodiscard]] uint64_t RequestDiagnostics(uint64_t graphInstanceId, uint32_t sampleFrames = 60,
                                              uint32_t stateSampleCount = 0);
    [[nodiscard]] GpuParticleDiagnosticSnapshot QueryDiagnostics(uint64_t requestId) const;

  private:
    struct Impl;
    std::unique_ptr<Impl> m_impl;
};

} // namespace particle
} // namespace infernux
