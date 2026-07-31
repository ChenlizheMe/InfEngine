#include "ParticleGpuSystemManager.h"

#include "ParticleGpuCollisionScene.h"
#include "ParticleGpuDrawRegistry.h"
#include "ParticleGpuMeshRenderer.h"

#include <core/log/InxLog.h>
#include <function/renderer/rhi/GpuRetirementQueue.h>
#include <function/renderer/rhi/RhiBuffer.h>
#include <function/renderer/vk/RenderGraph.h>
#include <function/renderer/vk/VkDeviceContext.h>
#include <function/renderer/vk/VkPipelineManager.h>
#include <function/renderer/vk/VkResourceManager.h>
#include <function/renderer/vk/VulkanRhiDevice.h>
#include <function/resources/InxSkinnedMesh/InxSkinnedMesh.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
#include <numeric>
#include <set>
#include <unordered_map>
#include <unordered_set>
#include <utility>

namespace infernux::particle
{

namespace
{

void SetError(std::string *error, std::string message)
{
    if (error)
        *error = std::move(message);
}

bool IsSpirv(const std::vector<uint32_t> &words)
{
    return words.size() >= 5 && words.front() == 0x07230203u;
}

bool IsFinite(const GpuParticleTransforms &transforms) noexcept
{
    const auto finite = [](const auto &matrix) {
        return std::all_of(matrix.begin(), matrix.end(), [](float value) { return std::isfinite(value); });
    };
    return finite(transforms.emitterToWorld) && finite(transforms.worldToEmitter) &&
           finite(transforms.simulationToWorld) && finite(transforms.worldToSimulation);
}

struct alignas(16) PackedParticleMeshVertex
{
    std::array<float, 4> position{};
    std::array<float, 4> normal{};
    std::array<float, 4> tangent{};
    std::array<float, 4> color{};
    std::array<float, 4> uv{};
};

struct alignas(16) PackedParticleMeshPrimitive
{
    uint32_t first = 0;
    uint32_t second = 0;
    uint32_t thirdOrCdf = 0;
    uint32_t cdfOrPadding = 0;
};

struct alignas(16) PackedParticleSkinInfluence
{
    std::array<uint32_t, 4> bones{};
    std::array<float, 4> weights{};
};

uint32_t FloatBits(float value) noexcept
{
    uint32_t bits = 0;
    static_assert(sizeof(bits) == sizeof(value));
    std::memcpy(&bits, &value, sizeof(bits));
    return bits;
}

uint64_t HashBytes(uint64_t hash, const void *data, size_t byteSize) noexcept
{
    constexpr uint64_t prime = 1099511628211ull;
    const auto *bytes = static_cast<const uint8_t *>(data);
    for (size_t index = 0; index < byteSize; ++index) {
        hash ^= bytes[index];
        hash *= prime;
    }
    return hash;
}

} // namespace

struct ParticleGpuSystemManager::Impl
{
    struct DiagnosticState
    {
        mutable std::mutex mutex;
        std::unordered_map<uint64_t, GpuParticleDiagnosticSnapshot> snapshots;
    };

    struct PendingDiagnostic
    {
        uint64_t requestId = 0;
        uint64_t graphInstanceId = 0;
        uint32_t remainingSampleFrames = 0;
        uint32_t stateSampleCount = 0;
        bool resetPending = true;
    };

    struct MeshResources
    {
        std::shared_ptr<rhi::BufferResource> vertices;
        std::shared_ptr<rhi::BufferResource> indices;
        std::shared_ptr<rhi::BufferResource> samplingTriangles;
        std::shared_ptr<rhi::BufferResource> skinInfluences;
        uint32_t vertexCount = 0;
        uint32_t indexCount = 0;
        uint32_t samplingTriangleCount = 0;
        uint32_t samplingEdgeCount = 0;
    };

    struct MeshUpload
    {
        std::weak_ptr<InxMesh> owner;
        uint64_t contentHash = 0;
        std::shared_ptr<vk::BufferUploadTicket> vertexTicket;
        std::shared_ptr<vk::BufferUploadTicket> indexTicket;
        std::shared_ptr<vk::BufferUploadTicket> samplingTriangleTicket;
        std::shared_ptr<vk::BufferUploadTicket> skinInfluenceTicket;
        std::shared_ptr<MeshResources> resources;
        bool failed = false;
    };

    struct Emitter
    {
        struct Output
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
            std::shared_ptr<ParticleGpuOutputRenderer> renderer;
        };

        uint64_t id = 0;
        uint64_t graphInstanceId = 0;
        uint64_t artifactRevision = 0;
        std::string stableId;
        bool statePreservedOnPublish = false;
        std::unique_ptr<ParticleGpuRuntime> runtime;
        std::unique_ptr<ParticleGpuBounds> bounds;
        std::shared_ptr<ParticleGpuRibbonTopology> ribbonTopology;
        std::shared_ptr<ParticleGpuMigrator> migration;
        std::shared_ptr<Emitter> migrationSource;
        GpuParticleEmitterProgram sourceProgram;
        std::vector<std::shared_ptr<InxTexture>> vectorFields;
        std::vector<uint64_t> vectorFieldGenerations;
        std::vector<uint64_t> observedVectorFieldGenerations;
        std::vector<std::shared_ptr<InxMesh>> meshes;
        std::vector<uint64_t> observedMeshGenerations;
        std::vector<uint32_t> billboardVertexShader;
        std::vector<uint32_t> billboardPickingFragmentShader;
        std::vector<uint32_t> billboardMotionVertexShader;
        std::vector<uint32_t> billboardMotionFragmentShader;
        std::vector<uint32_t> meshVertexShader;
        std::vector<uint32_t> meshShadowFragmentShader;
        std::vector<uint32_t> meshPickingFragmentShader;
        std::vector<uint32_t> meshMotionVertexShader;
        std::vector<uint32_t> meshMotionFragmentShader;
        std::vector<Output> outputs;
        bool hasFrameRequest = false;
        uint64_t lastFrameIndex = 0;
        uint32_t lastSpawnCount = 0;
        bool lastSimulate = false;
        bool lastRender = false;
        std::vector<GpuParticleFrameRequest> queuedFrameRequests;
        GpuParticleOffscreenPolicy lastOffscreenPolicy = GpuParticleOffscreenPolicy::AlwaysSimulate;
        GpuParticleBoundsMode lastBoundsMode = GpuParticleBoundsMode::Automatic;
    };

    struct GraphState
    {
        std::unique_ptr<vk::RenderGraph> graph;
        std::unordered_map<uint64_t, std::shared_ptr<ParticleGpuGraphSpawnDomain>> spawnDomains;
        std::vector<std::unique_ptr<ParticleRenderGraph>> schedulers;
        std::unordered_map<uint64_t, ParticleRenderGraph *> schedulerById;
        uint64_t generation = 0;
        uint32_t renderExportBatch = rhi::InvalidSubmissionBatchIndex;
        bool asyncRecordingActive = false;
    };

    using EmitterMap = std::map<uint64_t, std::shared_ptr<Emitter>>;
    vk::VkDeviceContext *context = nullptr;
    vk::VkPipelineManager *pipelines = nullptr;
    vk::VkResourceManager *resources = nullptr;
    GpuRetirementQueue *deletionQueue = nullptr;
    ParticleGpuDrawRegistry *drawRegistry = nullptr;
    GpuBillboardTextureResolver textureResolver;
    GpuParticleVectorFieldTextureResolver vectorFieldTextureResolver;
    GpuParticleSkinnedMeshResolver skinnedMeshResolver;
    std::shared_ptr<const GpuParticleCullProgramStorage> cullProgram;
    std::shared_ptr<const GpuParticleSortProgramStorage> sortProgram;
    std::shared_ptr<const GpuParticleBoundsProgramStorage> boundsProgram;
    std::shared_ptr<const GpuParticleMigrationProgramStorage> migrationProgram;
    std::shared_ptr<const GpuParticleSpawnProgramStorage> spawnProgram;
    std::shared_ptr<const GpuParticleRibbonProgramStorage> ribbonTopologyProgram;
    std::shared_ptr<const GpuParticleRibbonRenderProgramStorage> ribbonRenderProgram;
    std::unique_ptr<ParticleGpuCollisionScene> collisionScene;
    EmitterMap emitters;
    std::shared_ptr<GraphState> graphState;
    mutable uint64_t nextGraphGeneration = 1;
    mutable std::unordered_map<const InxMesh *, MeshUpload> meshUploads;
    std::shared_ptr<DiagnosticState> diagnosticState = std::make_shared<DiagnosticState>();
    std::vector<PendingDiagnostic> pendingDiagnostics;
    uint64_t nextDiagnosticRequestId = 1;

    void FailDiagnostics(uint64_t graphInstanceId, const char *reason) noexcept
    {
        pendingDiagnostics.erase(std::remove_if(pendingDiagnostics.begin(), pendingDiagnostics.end(),
                                                [graphInstanceId](const auto &request) {
                                                    return graphInstanceId == 0 ||
                                                           request.graphInstanceId == graphInstanceId;
                                                }),
                                 pendingDiagnostics.end());

        std::scoped_lock lock(diagnosticState->mutex);
        for (auto &[requestId, snapshot] : diagnosticState->snapshots) {
            (void)requestId;
            if (snapshot.status != GpuParticleDiagnosticStatus::Pending ||
                (graphInstanceId != 0 && snapshot.graphInstanceId != graphInstanceId))
                continue;
            snapshot.status = GpuParticleDiagnosticStatus::Failed;
            snapshot.error = reason;
        }
    }

    [[nodiscard]] bool HasQueuedFrameRequests() const noexcept
    {
        return std::any_of(emitters.begin(), emitters.end(),
                           [](const auto &entry) { return !entry.second->queuedFrameRequests.empty(); });
    }

    [[nodiscard]] bool ArmNextQueuedFrameRequests()
    {
        struct Pending
        {
            std::shared_ptr<Emitter> emitter;
            ParticleRenderGraph *scheduler = nullptr;
            ParticleGpuGraphSpawnDomain *spawnDomain = nullptr;
        };
        std::vector<Pending> pending;
        for (const auto &[id, emitter] : emitters) {
            if (!emitter || emitter->queuedFrameRequests.empty())
                continue;
            const auto scheduler = graphState->schedulerById.find(id);
            const auto domain = graphState->spawnDomains.find(emitter->graphInstanceId);
            if (scheduler == graphState->schedulerById.end() || domain == graphState->spawnDomains.end() ||
                !scheduler->second->CanBeginFrame(emitter->queuedFrameRequests.front()))
                return false;
            pending.push_back({emitter, scheduler->second, domain->second.get()});
        }
        if (pending.empty())
            return false;
        for (const auto &entry : pending) {
            const auto &request = entry.emitter->queuedFrameRequests.front();
            if (!entry.spawnDomain->SetEmitterAcceptingBurstRequests(entry.emitter->sourceProgram.graphEmitterIndex,
                                                                     request.simulate))
                return false;
        }
        for (const auto &entry : pending) {
            if (!entry.scheduler->BeginFrame(entry.emitter->queuedFrameRequests.front()))
                return false;
        }
        for (const auto &entry : pending)
            entry.emitter->queuedFrameRequests.erase(entry.emitter->queuedFrameRequests.begin());
        return true;
    }

    void ClearQueuedFrameRequests() noexcept
    {
        for (const auto &[id, emitter] : emitters) {
            (void)id;
            if (emitter)
                emitter->queuedFrameRequests.clear();
        }
    }

    [[nodiscard]] bool RecordCollisionUpload(VkCommandBuffer commandBuffer)
    {
        if (!collisionScene || !collisionScene->HasPendingUpload())
            return true;
        vk::VulkanTransferCommandContext transferContext;
        const auto transfer = context->GetRhiDevice().MakeTransferCommandEncoder(transferContext, commandBuffer);
        if (!collisionScene->RecordPendingUpload(transfer))
            return false;
        VkMemoryBarrier barrier{};
        barrier.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER;
        barrier.srcAccessMask = VK_ACCESS_TRANSFER_WRITE_BIT;
        barrier.dstAccessMask = VK_ACCESS_SHADER_READ_BIT;
        vkCmdPipelineBarrier(commandBuffer, VK_PIPELINE_STAGE_TRANSFER_BIT, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, 0, 1,
                             &barrier, 0, nullptr, 0, nullptr);
        return true;
    }

    void RecordDiagnostics(VkCommandBuffer commandBuffer)
    {
        if (pendingDiagnostics.empty() || !context || !deletionQueue || commandBuffer == VK_NULL_HANDLE)
            return;

        std::vector<PendingDiagnostic> requests;
        for (auto it = pendingDiagnostics.begin(); it != pendingDiagnostics.end();) {
            if (it->remainingSampleFrames != 0) {
                ++it;
                continue;
            }
            requests.push_back(*it);
            it = pendingDiagnostics.erase(it);
        }
        if (requests.empty())
            return;
        auto &device = context->GetRhiDevice();
        vk::VulkanTransferCommandContext transferContext;
        const auto transfer = device.MakeTransferCommandEncoder(transferContext, commandBuffer);

        VkMemoryBarrier barrier{};
        barrier.sType = VK_STRUCTURE_TYPE_MEMORY_BARRIER;
        barrier.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT;
        barrier.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
        vkCmdPipelineBarrier(commandBuffer, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 1,
                             &barrier, 0, nullptr, 0, nullptr);

        for (const auto &request : requests) {
            struct EmitterCapture
            {
                GpuParticleEmitterDiagnostic diagnostic;
                uint64_t counterOffset = 0;
                uint64_t counterBytes = 0;
                uint64_t boundsOffset = 0;
                uint64_t spawnMetadataOffset = 0;
                uint64_t stateOffset = 0;
                uint64_t stateBytes = 0;
                uint32_t stateStride = 0;
                uint32_t stateSampleCount = 0;
            };
            std::vector<EmitterCapture> emitterCaptures;
            bool invalidStateLayout = false;
            for (const auto &[id, emitter] : emitters) {
                if (!emitter || emitter->graphInstanceId != request.graphInstanceId || !emitter->runtime)
                    continue;
                EmitterCapture capture;
                capture.diagnostic.emitterId = id;
                capture.diagnostic.emitterIndex = emitter->sourceProgram.graphEmitterIndex;
                capture.diagnostic.capacity = emitter->runtime->Capacity();
                capture.diagnostic.boundsMode = emitter->lastBoundsMode;
                capture.diagnostic.eventOverflowCounts.resize(emitter->runtime->EventTypeCount());
                capture.diagnostic.eventEnqueueCounts.resize(emitter->runtime->EventTypeCount());
                capture.diagnostic.eventCompleteCounts.resize(emitter->runtime->EventTypeCount());
                capture.counterBytes = emitter->runtime->CounterBufferByteSize();
                if (request.stateSampleCount > 0) {
                    capture.stateStride = emitter->runtime->StateStride();
                    capture.stateSampleCount = request.stateSampleCount;
                    if (capture.stateStride == 0 || capture.stateStride % sizeof(uint32_t) != 0 ||
                        capture.diagnostic.capacity > std::numeric_limits<uint64_t>::max() / capture.stateStride) {
                        std::scoped_lock lock(diagnosticState->mutex);
                        auto &snapshot = diagnosticState->snapshots[request.requestId];
                        snapshot.status = GpuParticleDiagnosticStatus::Failed;
                        snapshot.error = "GPU particle state layout is invalid for diagnostic sampling";
                        invalidStateLayout = true;
                        break;
                    }
                    capture.stateBytes = static_cast<uint64_t>(capture.diagnostic.capacity) * capture.stateStride;
                }
                emitterCaptures.push_back(capture);
            }
            if (invalidStateLayout)
                continue;
            std::sort(emitterCaptures.begin(), emitterCaptures.end(), [](const auto &lhs, const auto &rhs) {
                return lhs.diagnostic.emitterIndex < rhs.diagnostic.emitterIndex;
            });

            uint64_t totalBytes = 0;
            for (const auto &capture : emitterCaptures)
                totalBytes += capture.counterBytes + ParticleGpuBounds::BoundsBufferBytes +
                              sizeof(GpuParticleSpawnMetadata) + capture.stateBytes;
            constexpr uint64_t MaxDiagnosticStateReadbackBytes = 16ull * 1024ull * 1024ull;
            const uint64_t stateReadbackBytes =
                std::accumulate(emitterCaptures.begin(), emitterCaptures.end(), uint64_t{0},
                                [](uint64_t total, const auto &capture) { return total + capture.stateBytes; });
            if (stateReadbackBytes > MaxDiagnosticStateReadbackBytes) {
                std::scoped_lock lock(diagnosticState->mutex);
                auto &snapshot = diagnosticState->snapshots[request.requestId];
                snapshot.status = GpuParticleDiagnosticStatus::Failed;
                snapshot.error = "GPU particle state diagnostic exceeds the 16 MiB bounded readback budget";
                continue;
            }
            if (emitterCaptures.empty() || totalBytes == 0) {
                std::scoped_lock lock(diagnosticState->mutex);
                auto &snapshot = diagnosticState->snapshots[request.requestId];
                snapshot.status = GpuParticleDiagnosticStatus::Inactive;
                snapshot.error = "GPU particle graph is not resident";
                continue;
            }

            const auto readbackHandle = device.CreateBuffer(
                {totalBytes, rhi::BufferUsageFlags::TransferDestination, rhi::BufferMemory::Readback});
            if (!readbackHandle.IsValid()) {
                std::scoped_lock lock(diagnosticState->mutex);
                auto &snapshot = diagnosticState->snapshots[request.requestId];
                snapshot.status = GpuParticleDiagnosticStatus::Failed;
                snapshot.error = "Failed to allocate GPU particle diagnostic readback buffer";
                continue;
            }
            auto readback = std::make_shared<rhi::BufferResource>(device, readbackHandle, totalBytes);

            uint64_t offset = 0;
            const auto spawnDomain = graphState ? graphState->spawnDomains.find(request.graphInstanceId)
                                                : decltype(graphState->spawnDomains)::const_iterator{};
            for (auto &capture : emitterCaptures) {
                capture.counterOffset = offset;
                const auto emitter = emitters.find(capture.diagnostic.emitterId);
                transfer.CopyBuffer(emitter->second->runtime->CounterBuffer(), readbackHandle,
                                    {0, offset, capture.counterBytes});
                offset += capture.counterBytes;
                capture.boundsOffset = offset;
                transfer.CopyBuffer(emitter->second->bounds->BoundsBuffer(), readbackHandle,
                                    {0, offset, ParticleGpuBounds::BoundsBufferBytes});
                offset += ParticleGpuBounds::BoundsBufferBytes;
                capture.spawnMetadataOffset = offset;
                if (graphState && spawnDomain != graphState->spawnDomains.end()) {
                    transfer.CopyBuffer(spawnDomain->second->MetadataBuffer(), readbackHandle,
                                        {spawnDomain->second->MetadataOffset(capture.diagnostic.emitterIndex), offset,
                                         sizeof(GpuParticleSpawnMetadata)});
                }
                offset += sizeof(GpuParticleSpawnMetadata);
                if (capture.stateBytes > 0) {
                    capture.stateOffset = offset;
                    transfer.CopyBuffer(emitter->second->runtime->StateBuffer(), readbackHandle,
                                        {0, offset, capture.stateBytes});
                    offset += capture.stateBytes;
                }
            }
            const auto state = diagnosticState;
            auto *readbackDevice = static_cast<rhi::Device *>(&device);
            deletionQueue->Retire([state, readback, emitterCaptures = std::move(emitterCaptures), request,
                                   readbackDevice]() mutable {
                std::vector<uint8_t> bytes(static_cast<size_t>(readback->GetByteSize()));
                GpuParticleDiagnosticSnapshot result;
                result.requestId = request.requestId;
                result.graphInstanceId = request.graphInstanceId;
                if (!readbackDevice->ReadBuffer(readback->GetBuffer(), 0, bytes.data(), bytes.size())) {
                    result.status = GpuParticleDiagnosticStatus::Failed;
                    result.error = "Failed to read completed GPU particle diagnostic buffer";
                } else {
                    result.status = GpuParticleDiagnosticStatus::Completed;
                    for (auto &capture : emitterCaptures) {
                        std::array<uint32_t, ParticleGpuRuntime::BaseCounterWordCount> counters{};
                        std::memcpy(counters.data(), bytes.data() + capture.counterOffset, sizeof(counters));
                        capture.diagnostic.freeCount = std::min(counters[0], capture.diagnostic.capacity);
                        capture.diagnostic.aliveCount = capture.diagnostic.capacity - capture.diagnostic.freeCount;
                        capture.diagnostic.visibleCount = counters[1];
                        capture.diagnostic.droppedCount = counters[2];
                        capture.diagnostic.collisionHitCount = counters[4];
                        capture.diagnostic.collisionResponseCount = counters[5];
                        capture.diagnostic.collisionTriggerCount = counters[6];
                        capture.diagnostic.collisionEnterCount = counters[7];
                        capture.diagnostic.collisionStayCount = counters[8];
                        capture.diagnostic.collisionExitCount = counters[9];
                        std::memcpy(&capture.diagnostic.collisionMaxOutwardSpeed, &counters[10], sizeof(float));
                        std::memcpy(&capture.diagnostic.collisionMaxTangentSpeed, &counters[11], sizeof(float));
                        capture.diagnostic.collisionCandidateOverflowCount = counters[12];
                        if (!capture.diagnostic.eventOverflowCounts.empty()) {
                            const size_t eventCount = capture.diagnostic.eventOverflowCounts.size();
                            const size_t eventBytes = eventCount * sizeof(uint32_t);
                            const auto *eventCounters = bytes.data() + capture.counterOffset +
                                                        ParticleGpuRuntime::EventCounterWordOffset * sizeof(uint32_t);
                            std::memcpy(capture.diagnostic.eventOverflowCounts.data(), eventCounters, eventBytes);
                            std::memcpy(capture.diagnostic.eventEnqueueCounts.data(), eventCounters + eventBytes,
                                        eventBytes);
                            std::memcpy(capture.diagnostic.eventCompleteCounts.data(), eventCounters + eventBytes * 2u,
                                        eventBytes);
                        }
                        std::array<uint32_t, 8> encodedBounds{};
                        std::memcpy(encodedBounds.data(), bytes.data() + capture.boundsOffset, sizeof(encodedBounds));
                        capture.diagnostic.boundsValid = encodedBounds[6] != 0u;
                        if (capture.diagnostic.boundsValid) {
                            const auto decodeOrderedFloat = [](uint32_t ordered) {
                                const uint32_t bits =
                                    (ordered & 0x80000000u) != 0u ? (ordered ^ 0x80000000u) : ~ordered;
                                float value = 0.0f;
                                std::memcpy(&value, &bits, sizeof(value));
                                return value;
                            };
                            for (size_t axis = 0; axis < 3; ++axis) {
                                capture.diagnostic.boundsLower[axis] = decodeOrderedFloat(encodedBounds[axis]);
                                capture.diagnostic.boundsUpper[axis] = decodeOrderedFloat(encodedBounds[axis + 3]);
                            }
                        }
                        GpuParticleSpawnMetadata spawnMetadata{};
                        std::memcpy(&spawnMetadata, bytes.data() + capture.spawnMetadataOffset, sizeof(spawnMetadata));
                        capture.diagnostic.preparedSpawnCount = spawnMetadata.count;
                        capture.diagnostic.preparedSpawnBaseId = spawnMetadata.baseId;
                        capture.diagnostic.preparedSpawnGeneration = spawnMetadata.generation;
                        capture.diagnostic.spawnOverflowCount = spawnMetadata.overflowCount;
                        if (capture.stateBytes > 0 && capture.stateSampleCount > 0) {
                            const size_t wordsPerState = capture.stateStride / sizeof(uint32_t);
                            for (uint32_t slot = 0; slot < capture.diagnostic.capacity &&
                                                    capture.diagnostic.stateSamples.size() < capture.stateSampleCount;
                                 ++slot) {
                                const auto *stateBytes = bytes.data() + capture.stateOffset +
                                                         static_cast<uint64_t>(slot) * capture.stateStride;
                                uint32_t lifecycleFlags = 0;
                                uint32_t spawnGeneration = 0;
                                std::memcpy(&lifecycleFlags, stateBytes, sizeof(lifecycleFlags));
                                if ((lifecycleFlags & 1u) == 0u)
                                    continue;
                                std::memcpy(&spawnGeneration, stateBytes + sizeof(uint32_t), sizeof(spawnGeneration));
                                GpuParticleEmitterDiagnostic::StateSample sample;
                                sample.slotIndex = slot;
                                sample.lifecycleFlags = lifecycleFlags;
                                sample.spawnGeneration = spawnGeneration;
                                sample.words.resize(wordsPerState);
                                std::memcpy(sample.words.data(), stateBytes, capture.stateStride);
                                capture.diagnostic.stateSamples.push_back(std::move(sample));
                            }
                        }
                        result.emitters.push_back(capture.diagnostic);
                    }
                }
                std::scoped_lock lock(state->mutex);
                const auto found = state->snapshots.find(request.requestId);
                if (found != state->snapshots.end() && found->second.status == GpuParticleDiagnosticStatus::Pending)
                    found->second = std::move(result);
            });
        }
    }

    [[nodiscard]] std::shared_ptr<MeshResources> ResolveMeshResources(const std::shared_ptr<InxMesh> &mesh,
                                                                      std::string *error) const
    {
        if (!resources || !mesh) {
            SetError(error, "GPU particle Mesh resource service is unavailable");
            return {};
        }
        for (auto it = meshUploads.begin(); it != meshUploads.end();) {
            if (it->second.owner.expired())
                it = meshUploads.erase(it);
            else
                ++it;
        }

        const auto &sourceVertices = mesh->GetVertices();
        const auto &sourceIndices = mesh->GetIndices();
        if (sourceVertices.empty() || sourceIndices.empty() || sourceIndices.size() % 3 != 0 ||
            sourceIndices.size() > static_cast<size_t>(std::numeric_limits<uint32_t>::max()) ||
            std::any_of(sourceIndices.begin(), sourceIndices.end(),
                        [&](uint32_t index) { return index >= sourceVertices.size(); })) {
            SetError(error, "GPU particle Mesh output requires valid indexed geometry");
            return {};
        }

        std::vector<PackedParticleMeshVertex> vertices;
        vertices.reserve(sourceVertices.size());
        for (const auto &source : sourceVertices) {
            PackedParticleMeshVertex vertex;
            vertex.position = {source.pos.x, source.pos.y, source.pos.z, 1.0f};
            vertex.normal = {source.normal.x, source.normal.y, source.normal.z, 0.0f};
            vertex.tangent = {source.tangent.x, source.tangent.y, source.tangent.z, source.tangent.w};
            vertex.color = {source.color.x, source.color.y, source.color.z, 1.0f};
            vertex.uv = {source.texCoord.x, source.texCoord.y, 0.0f, 0.0f};
            vertices.push_back(vertex);
        }
        std::vector<PackedParticleSkinInfluence> skinInfluences;
        if (const auto &skinned = mesh->GetSkinnedData(); skinned) {
            if (skinned->influences.size() != sourceVertices.size()) {
                SetError(error, "GPU particle Skinned Mesh influence count does not match its vertices");
                return {};
            }
            skinInfluences.reserve(skinned->influences.size());
            for (const auto &source : skinned->influences) {
                PackedParticleSkinInfluence influence;
                influence.bones = source.boneIndex;
                influence.weights = source.weight;
                skinInfluences.push_back(influence);
            }
        }
        std::vector<PackedParticleMeshPrimitive> samplingPrimitives;
        samplingPrimitives.reserve(sourceIndices.size());
        double cumulativeArea = 0.0;
        for (size_t index = 0; index < sourceIndices.size(); index += 3) {
            const uint32_t first = sourceIndices[index];
            const uint32_t second = sourceIndices[index + 1];
            const uint32_t third = sourceIndices[index + 2];
            const glm::vec3 edgeA = sourceVertices[second].pos - sourceVertices[first].pos;
            const glm::vec3 edgeB = sourceVertices[third].pos - sourceVertices[first].pos;
            const double area = static_cast<double>(glm::length(glm::cross(edgeA, edgeB))) * 0.5;
            if (!std::isfinite(area) || area <= 1.0e-12)
                continue;
            cumulativeArea += area;
            samplingPrimitives.push_back({first, second, third, FloatBits(static_cast<float>(cumulativeArea))});
        }
        const size_t samplingTriangleCount = samplingPrimitives.size();
        if (samplingTriangleCount == 0 || !std::isfinite(cumulativeArea) || cumulativeArea <= 0.0) {
            SetError(error, "GPU particle Mesh sampling requires non-degenerate triangles");
            return {};
        }
        const float inverseArea = static_cast<float>(1.0 / cumulativeArea);
        for (size_t index = 0; index < samplingTriangleCount; ++index) {
            float cumulative = 0.0f;
            std::memcpy(&cumulative, &samplingPrimitives[index].cdfOrPadding, sizeof(cumulative));
            samplingPrimitives[index].cdfOrPadding = FloatBits(cumulative * inverseArea);
        }
        samplingPrimitives[samplingTriangleCount - 1].cdfOrPadding = FloatBits(1.0f);

        std::set<std::pair<uint32_t, uint32_t>> uniqueEdges;
        for (size_t index = 0; index < sourceIndices.size(); index += 3) {
            const std::array<uint32_t, 3> triangle = {sourceIndices[index], sourceIndices[index + 1],
                                                      sourceIndices[index + 2]};
            for (size_t edge = 0; edge < triangle.size(); ++edge) {
                const uint32_t first = triangle[edge];
                const uint32_t second = triangle[(edge + 1) % triangle.size()];
                if (first != second)
                    uniqueEdges.emplace(std::min(first, second), std::max(first, second));
            }
        }
        double cumulativeLength = 0.0;
        for (const auto &[first, second] : uniqueEdges) {
            const double length =
                static_cast<double>(glm::length(sourceVertices[second].pos - sourceVertices[first].pos));
            if (!std::isfinite(length) || length <= 1.0e-12)
                continue;
            cumulativeLength += length;
            samplingPrimitives.push_back({first, second, FloatBits(static_cast<float>(cumulativeLength)), 0u});
        }
        const size_t samplingEdgeCount = samplingPrimitives.size() - samplingTriangleCount;
        if (samplingEdgeCount == 0 || !std::isfinite(cumulativeLength) || cumulativeLength <= 0.0) {
            SetError(error, "GPU particle Mesh edge sampling requires non-degenerate edges");
            return {};
        }
        const float inverseLength = static_cast<float>(1.0 / cumulativeLength);
        for (size_t index = samplingTriangleCount; index < samplingPrimitives.size(); ++index) {
            float cumulative = 0.0f;
            std::memcpy(&cumulative, &samplingPrimitives[index].thirdOrCdf, sizeof(cumulative));
            samplingPrimitives[index].thirdOrCdf = FloatBits(cumulative * inverseLength);
        }
        samplingPrimitives.back().thirdOrCdf = FloatBits(1.0f);
        uint64_t contentHash = 1469598103934665603ull;
        contentHash = HashBytes(contentHash, vertices.data(), vertices.size() * sizeof(PackedParticleMeshVertex));
        if (!skinInfluences.empty())
            contentHash = HashBytes(contentHash, skinInfluences.data(),
                                    skinInfluences.size() * sizeof(PackedParticleSkinInfluence));
        contentHash = HashBytes(contentHash, sourceIndices.data(), sourceIndices.size() * sizeof(uint32_t));
        contentHash = HashBytes(contentHash, samplingPrimitives.data(),
                                samplingPrimitives.size() * sizeof(PackedParticleMeshPrimitive));

        auto &upload = meshUploads[mesh.get()];
        const auto owner = upload.owner.lock();
        if (owner.get() != mesh.get() || upload.contentHash != contentHash) {
            upload = {};
            upload.owner = mesh;
            upload.contentHash = contentHash;
            try {
                upload.vertexTicket = resources->BeginBufferUpload(
                    {vertices.data(), vertices.size() * sizeof(PackedParticleMeshVertex), rhi::BufferUsage::Storage});
                upload.indexTicket = resources->BeginBufferUpload(
                    {sourceIndices.data(), sourceIndices.size() * sizeof(uint32_t), rhi::BufferUsage::Storage});
                upload.samplingTriangleTicket = resources->BeginBufferUpload(
                    {samplingPrimitives.data(), samplingPrimitives.size() * sizeof(PackedParticleMeshPrimitive),
                     rhi::BufferUsage::Storage});
                if (!skinInfluences.empty()) {
                    upload.skinInfluenceTicket = resources->BeginBufferUpload(
                        {skinInfluences.data(), skinInfluences.size() * sizeof(PackedParticleSkinInfluence),
                         rhi::BufferUsage::Storage});
                }
            } catch (const std::exception &exception) {
                upload.failed = true;
                SetError(error, std::string("GPU particle Mesh upload failed: ") + exception.what());
                return {};
            }
        }
        if (upload.failed) {
            SetError(error, "GPU particle Mesh upload failed");
            return {};
        }
        if (upload.resources)
            return upload.resources;
        try {
            const bool verticesReady = resources->TryPublishBufferUpload(upload.vertexTicket);
            const bool indicesReady = resources->TryPublishBufferUpload(upload.indexTicket);
            const bool samplingReady = resources->TryPublishBufferUpload(upload.samplingTriangleTicket);
            const bool influencesReady =
                !upload.skinInfluenceTicket || resources->TryPublishBufferUpload(upload.skinInfluenceTicket);
            if (!verticesReady || !indicesReady || !samplingReady || !influencesReady) {
                SetError(error, "GPU particle Mesh upload is pending");
                return {};
            }
            auto result = std::make_shared<MeshResources>();
            result->vertices = resources->GetPublishedRhiBuffer(upload.vertexTicket);
            result->indices = resources->GetPublishedRhiBuffer(upload.indexTicket);
            result->samplingTriangles = resources->GetPublishedRhiBuffer(upload.samplingTriangleTicket);
            if (upload.skinInfluenceTicket)
                result->skinInfluences = resources->GetPublishedRhiBuffer(upload.skinInfluenceTicket);
            result->vertexCount = static_cast<uint32_t>(sourceVertices.size());
            result->indexCount = static_cast<uint32_t>(sourceIndices.size());
            result->samplingTriangleCount = static_cast<uint32_t>(samplingTriangleCount);
            result->samplingEdgeCount = static_cast<uint32_t>(samplingEdgeCount);
            if (!result->vertices || !result->vertices->IsValid() || !result->indices || !result->indices->IsValid() ||
                !result->samplingTriangles || !result->samplingTriangles->IsValid()) {
                upload.failed = true;
                SetError(error, "GPU particle Mesh upload produced invalid RHI buffers");
                return {};
            }
            upload.resources = result;
            return result;
        } catch (const std::exception &exception) {
            upload.failed = true;
            SetError(error, std::string("GPU particle Mesh publication failed: ") + exception.what());
            return {};
        }
    }

    static void CaptureMeshRevisions(const GpuParticleEmitterProgram &program,
                                     std::vector<std::shared_ptr<InxMesh>> &meshes, std::vector<uint64_t> &generations)
    {
        meshes.clear();
        generations.clear();
        std::unordered_set<const InxMesh *> seen;
        const auto append = [&](const std::shared_ptr<InxMesh> &mesh) {
            if (!mesh || !seen.insert(mesh.get()).second)
                return;
            meshes.push_back(mesh);
            generations.push_back(mesh->GetGeneration());
        };
        for (const auto &interface : program.meshInterfaces)
            append(interface.mesh);
        for (const auto &output : program.outputs)
            append(output.mesh);
    }

    [[nodiscard]] bool BuildRuntimeDesc(const GpuParticleEmitterProgram &program, GpuEmitterDesc &runtimeDesc,
                                        std::vector<std::shared_ptr<InxTexture>> &vectorFields,
                                        std::vector<uint64_t> &vectorFieldGenerations, std::string *error) const
    {
        runtimeDesc.capacity = program.capacity;
        runtimeDesc.stateStride = program.stateStride;
        runtimeDesc.eventTypeCount = program.eventTypeCount;
        runtimeDesc.collisionEnabled = program.collisionEnabled;
        runtimeDesc.parameterWords = program.parameterWords;
        for (size_t index = 0; index < program.kernels.size(); ++index)
            runtimeDesc.kernels[index] = {program.kernels[index].data(), program.kernels[index].size()};
        runtimeDesc.continuation.capacity = program.continuationCapacity;
        if (program.continuationCapacity > 0) {
            runtimeDesc.continuation.recordStride = program.continuationRecordStride;
            runtimeDesc.continuation.laneCount = program.continuationLaneCount;
            runtimeDesc.continuation.joinCount = program.continuationJoinCount;
            runtimeDesc.continuation.program.prepare = {
                program.continuationKernels[static_cast<size_t>(GpuParticleContinuationKernelStage::Prepare)].data(),
                program.continuationKernels[static_cast<size_t>(GpuParticleContinuationKernelStage::Prepare)].size(),
            };
            runtimeDesc.continuation.program.classify = {
                program.continuationKernels[static_cast<size_t>(GpuParticleContinuationKernelStage::Classify)].data(),
                program.continuationKernels[static_cast<size_t>(GpuParticleContinuationKernelStage::Classify)].size(),
            };
            runtimeDesc.continuation.program.dispatch = {
                program.continuationKernels[static_cast<size_t>(GpuParticleContinuationKernelStage::Dispatch)].data(),
                program.continuationKernels[static_cast<size_t>(GpuParticleContinuationKernelStage::Dispatch)].size(),
            };
        }
        if (program.collisionEnabled) {
            if (!collisionScene || !collisionScene->IsValid()) {
                SetError(error, "GPU particle collision scene is not initialized");
                return false;
            }
            runtimeDesc.collisionSceneHeader = collisionScene->HeaderBuffer();
            runtimeDesc.collisionSceneColliders = collisionScene->ColliderBuffer();
            runtimeDesc.collisionSceneGridOffsets = collisionScene->GridOffsetBuffer();
            runtimeDesc.collisionSceneGridColliderIndices = collisionScene->GridColliderIndexBuffer();
            runtimeDesc.collisionSceneMeshVertices = collisionScene->MeshVertexBuffer();
            runtimeDesc.collisionSceneMeshIndices = collisionScene->MeshIndexBuffer();
            runtimeDesc.collisionSceneMeshBvhNodes = collisionScene->MeshBvhBuffer();
        }
        runtimeDesc.meshInterfaces.reserve(program.meshInterfaces.size());
        for (const auto &mesh : program.meshInterfaces) {
            if (!mesh.mesh) {
                SetError(error, "GPU particle Mesh resource binding requires a loaded Mesh asset");
                return false;
            }
            const auto meshResources = ResolveMeshResources(mesh.mesh, error);
            if (!meshResources)
                return false;
            GpuMeshInterfaceDesc runtimeMesh;
            runtimeMesh.stableId = mesh.stableId;
            runtimeMesh.interfaceIndex = mesh.interfaceIndex;
            runtimeMesh.metadataOffsetWords = mesh.metadataOffsetWords;
            runtimeMesh.vertexBinding = mesh.vertexBinding;
            runtimeMesh.triangleBinding = mesh.triangleBinding;
            runtimeMesh.influenceBinding = mesh.influenceBinding;
            runtimeMesh.paletteBinding = mesh.paletteBinding;
            runtimeMesh.worldSpace = mesh.worldSpace;
            runtimeMesh.meshToSpace = mesh.meshToSpace;
            runtimeMesh.vertexCount = meshResources->vertexCount;
            runtimeMesh.triangleCount = meshResources->samplingTriangleCount;
            runtimeMesh.edgeCount = meshResources->samplingEdgeCount;
            runtimeMesh.vertices = meshResources->vertices->GetBuffer();
            runtimeMesh.triangles = meshResources->samplingTriangles->GetBuffer();
            if (mesh.skinnedRenderer.IsValid()) {
                if (!skinnedMeshResolver) {
                    SetError(error, "GPU particle SkinnedMeshRenderer resolver is unavailable");
                    return false;
                }
                const auto snapshot = skinnedMeshResolver(mesh.skinnedRenderer);
                if (!snapshot || !snapshot->mesh || snapshot->mesh.get() != mesh.mesh.get() || !snapshot->model ||
                    !snapshot->currentPalette || snapshot->currentPalette->empty() ||
                    snapshot->currentPalette->size() != snapshot->model->bones.size() ||
                    !meshResources->skinInfluences || !meshResources->skinInfluences->IsValid()) {
                    SetError(error, "GPU particle SkinnedMeshRenderer source has no valid live skin pose");
                    return false;
                }
                runtimeMesh.worldSpace = true;
                runtimeMesh.meshToSpace = snapshot->sourceToWorld;
                runtimeMesh.boneCount = static_cast<uint32_t>(snapshot->currentPalette->size());
                runtimeMesh.poseRevision = snapshot->revision;
                runtimeMesh.influences = meshResources->skinInfluences->GetBuffer();
                runtimeMesh.initialPalette.assign(snapshot->currentPalette->begin(), snapshot->currentPalette->end());
            }
            runtimeMesh.keepAlive = meshResources;
            runtimeDesc.meshInterfaces.push_back(std::move(runtimeMesh));
        }

        runtimeDesc.vectorFields.metadataBinding = program.vectorFields.metadataBinding;
        runtimeDesc.vectorFields.interfaceStrideWords = program.vectorFields.interfaceStrideWords;
        runtimeDesc.vectorFields.vectorFields.reserve(program.vectorFields.vectorFields.size());
        runtimeDesc.vectorFields.textureParameters.reserve(program.vectorFields.textureParameters.size());
        const size_t sampledTextureCount =
            program.vectorFields.vectorFields.size() + program.vectorFields.textureParameters.size();
        vectorFields.reserve(sampledTextureCount);
        vectorFieldGenerations.reserve(sampledTextureCount);
        std::unordered_set<std::string> stableIds;
        for (const auto &field : program.vectorFields.vectorFields) {
            const auto cpuData = field.texture ? field.texture->GetCpuData() : nullptr;
            const TextureSemantic expectedSemantic = field.kind == GpuVectorFieldDesc::Kind::SignedDistanceField
                                                         ? TextureSemantic::SignedDistanceField
                                                         : TextureSemantic::VectorField;
            if (field.stableId.empty() || !stableIds.insert(field.stableId).second || !field.texture || !cpuData ||
                !cpuData->IsValid() || cpuData->dimension != TextureDimension::Texture3D ||
                cpuData->semantic != expectedSemantic || field.texture->GetGuid().empty() ||
                !vectorFieldTextureResolver) {
                SetError(error, "GPU particle volume bindings require unique identities and matching Texture3D assets");
                return false;
            }
            auto lease = vectorFieldTextureResolver(field.texture->GetGuid(), field.linearFiltering, field.repeat);
            if (lease.status != GpuBillboardTextureStatus::Ready || !lease.texture.IsValid() ||
                !lease.sampler.IsValid() || !lease.gpuView || !lease.gpuView->IsValid()) {
                SetError(error, lease.status == GpuBillboardTextureStatus::Pending
                                    ? "GPU particle volume texture upload is pending"
                                    : "GPU particle volume texture upload failed");
                return false;
            }
            GpuVectorFieldDesc runtimeField;
            runtimeField.kind = field.kind;
            runtimeField.interfaceIndex = field.interfaceIndex;
            runtimeField.textureBinding = field.textureBinding;
            runtimeField.worldSpace = field.worldSpace;
            runtimeField.fieldToSpace = field.fieldToSpace;
            runtimeField.vectorScale = field.vectorScale;
            runtimeField.texture = lease.texture;
            runtimeField.sampler = lease.sampler;
            runtimeField.keepAlive = std::move(lease.gpuView);
            runtimeDesc.vectorFields.vectorFields.push_back(std::move(runtimeField));
            vectorFields.push_back(field.texture);
            vectorFieldGenerations.push_back(field.texture->GetGeneration());
        }
        std::unordered_set<std::string> parameterIds;
        for (size_t index = 0; index < program.vectorFields.textureParameters.size(); ++index) {
            const auto &parameter = program.vectorFields.textureParameters[index];
            if (parameter.stableId.empty() || !parameterIds.insert(parameter.stableId).second ||
                parameter.resourceIndex != index || parameter.textureGuid.empty() || !textureResolver) {
                SetError(error, "GPU particle Texture2D parameter bindings require unique identities");
                return false;
            }
            auto lease = textureResolver(parameter.textureGuid, parameter.stableId);
            if (lease.status != GpuBillboardTextureStatus::Ready || !lease.texture.IsValid() ||
                !lease.sampler.IsValid() || !lease.gpuView || !lease.gpuView->IsValid()) {
                SetError(error, lease.status == GpuBillboardTextureStatus::Pending
                                    ? "GPU particle sampled texture upload is pending"
                                    : "GPU particle Texture2D parameter upload failed");
                return false;
            }
            GpuTexture2DParameterDesc runtimeParameter;
            runtimeParameter.resourceIndex = parameter.resourceIndex;
            runtimeParameter.parameterSlot = parameter.parameterSlot;
            runtimeParameter.textureBinding = parameter.textureBinding;
            runtimeParameter.texture = lease.texture;
            runtimeParameter.sampler = lease.sampler;
            runtimeParameter.keepAlive = std::move(lease.gpuView);
            runtimeDesc.vectorFields.textureParameters.push_back(std::move(runtimeParameter));
            vectorFields.push_back(parameter.texture);
            vectorFieldGenerations.push_back(parameter.texture ? parameter.texture->GetGeneration() : 0);
        }
        return true;
    }

    [[nodiscard]] std::shared_ptr<ParticleGpuOutputRenderer>
    CreateOutputRenderer(const Emitter &emitter, const GpuParticleOutputProgram &output, std::string *error) const
    {
        if (output.type == GpuParticleOutputType::Ribbon) {
            if (!emitter.ribbonTopology || !ribbonRenderProgram || !ribbonRenderProgram->IsValid()) {
                SetError(error, "GPU particle Ribbon resources are unavailable");
                return {};
            }
            auto renderer = std::make_shared<ParticleGpuRibbonRenderer>();
            GpuRibbonRendererDesc desc;
            desc.program = ribbonRenderProgram->View();
            desc.topology = emitter.ribbonTopology;
            desc.shaderProgram = output.shaderProgram;
            desc.material = output.material;
            desc.fallbackMaterial = output.fallbackMaterial;
            desc.semantics = output.semantics;
            desc.uvMode = output.ribbonUvMode;
            desc.uvScale = output.ribbonUvScale;
            desc.textureResolver = textureResolver;
            desc.deletionQueue = deletionQueue;
            return renderer->Create(context->GetRhiDevice(), desc) ? renderer : nullptr;
        }
        if (output.type == GpuParticleOutputType::Mesh) {
            const auto meshResources = ResolveMeshResources(output.mesh, error);
            if (!meshResources)
                return {};
            auto renderer = std::make_shared<ParticleGpuMeshRenderer>();
            GpuMeshRendererDesc desc;
            desc.vertexShader = {emitter.meshVertexShader.data(), emitter.meshVertexShader.size()};
            desc.shadowFragmentShader = {emitter.meshShadowFragmentShader.data(),
                                         emitter.meshShadowFragmentShader.size()};
            desc.pickingFragmentShader = {emitter.meshPickingFragmentShader.data(),
                                          emitter.meshPickingFragmentShader.size()};
            desc.motionVertexShader = {emitter.meshMotionVertexShader.data(), emitter.meshMotionVertexShader.size()};
            desc.motionFragmentShader = {emitter.meshMotionFragmentShader.data(),
                                         emitter.meshMotionFragmentShader.size()};
            desc.instances = emitter.runtime->InstanceBuffer();
            desc.renderIndices = emitter.runtime->RenderIndexBuffer();
            desc.mesh = output.mesh;
            desc.meshVertices = meshResources->vertices->GetBuffer();
            desc.meshIndices = meshResources->indices->GetBuffer();
            desc.indexCount = meshResources->indexCount;
            desc.meshBufferKeepAlive = meshResources;
            desc.shaderProgram = output.shaderProgram;
            desc.material = output.material;
            desc.fallbackMaterial = output.fallbackMaterial;
            desc.semantics = output.semantics;
            desc.textureResolver = textureResolver;
            desc.deletionQueue = deletionQueue;
            return renderer->Create(context->GetRhiDevice(), desc) ? renderer : nullptr;
        }
        auto renderer = std::make_shared<ParticleGpuBillboardRenderer>();
        GpuBillboardRendererDesc rendererDesc;
        rendererDesc.vertexShader = {emitter.billboardVertexShader.data(), emitter.billboardVertexShader.size()};
        rendererDesc.pickingFragmentShader = {emitter.billboardPickingFragmentShader.data(),
                                              emitter.billboardPickingFragmentShader.size()};
        rendererDesc.motionVertexShader = {emitter.billboardMotionVertexShader.data(),
                                           emitter.billboardMotionVertexShader.size()};
        rendererDesc.motionFragmentShader = {emitter.billboardMotionFragmentShader.data(),
                                             emitter.billboardMotionFragmentShader.size()};
        rendererDesc.shaderProgram = output.shaderProgram;
        rendererDesc.instances = emitter.runtime->InstanceBuffer();
        rendererDesc.renderIndices = emitter.runtime->RenderIndexBuffer();
        rendererDesc.material = output.material;
        rendererDesc.fallbackMaterial = output.fallbackMaterial;
        rendererDesc.semantics = output.semantics;
        rendererDesc.flipbookColumns = output.flipbookColumns;
        rendererDesc.flipbookRows = output.flipbookRows;
        rendererDesc.textureResolver = textureResolver;
        rendererDesc.deletionQueue = deletionQueue;
        return renderer->Create(context->GetRhiDevice(), rendererDesc) ? renderer : nullptr;
    }

    [[nodiscard]] bool PreflightEmitterProgram(const GpuParticleEmitterProgram &program,
                                               const std::shared_ptr<Emitter> &previous, std::string *error) const
    {
        if (program.id == 0 || program.graphInstanceId == 0 || program.stableId.empty() ||
            program.artifactRevision == 0 || program.capacity == 0 || program.stateStride == 0) {
            SetError(error,
                     "GPU particle program graph identity, emitter identity, revision, capacity, and state stride "
                     "must be valid");
            return false;
        }
        if (!std::all_of(program.kernels.begin(), program.kernels.end(),
                         [](const auto &kernel) { return IsSpirv(kernel); })) {
            SetError(error, "GPU particle program contains invalid compute SPIR-V");
            return false;
        }
        const bool hasContinuationKernels =
            std::any_of(program.continuationKernels.begin(), program.continuationKernels.end(),
                        [](const auto &kernel) { return !kernel.empty(); });
        if ((program.continuationCapacity == 0) != !hasContinuationKernels ||
            program.continuationCapacity > ParticleGpuContinuationRuntime::MaximumCapacity ||
            (program.continuationCapacity > 0 &&
             (program.continuationRecordStride < sizeof(GpuParticleContinuationRecord) ||
              program.continuationRecordStride > ParticleGpuContinuationRuntime::MaximumRecordStride ||
              program.continuationRecordStride % 16 != 0)) ||
            (program.continuationCapacity > 0 &&
             (program.continuationLaneCount == 0 ||
              program.continuationLaneCount > ParticleGpuContinuationRuntime::MaximumLaneCount ||
              program.continuationJoinCount > ParticleGpuContinuationRuntime::MaximumJoinCount)) ||
            (program.continuationCapacity > 0 &&
             !std::all_of(program.continuationKernels.begin(), program.continuationKernels.end(),
                          [](const auto &kernel) { return IsSpirv(kernel); }))) {
            SetError(error,
                     "GPU particle continuation capacity, record stride, static lane/join counts and "
                     "Prepare/Classify/Dispatch SPIR-V must be supplied together and satisfy the bounded runtime "
                     "contract");
            return false;
        }
        if (program.outputs.empty()) {
            SetError(error, "GPU particle program requires at least one rendering output");
            return false;
        }

        const bool needsMesh = std::any_of(program.outputs.begin(), program.outputs.end(), [](const auto &output) {
            return output.type == GpuParticleOutputType::Mesh;
        });
        const bool needsRibbon = std::any_of(program.outputs.begin(), program.outputs.end(), [](const auto &output) {
            return output.type == GpuParticleOutputType::Ribbon;
        });
        if (needsMesh && (!IsSpirv(program.meshVertexShader) || !IsSpirv(program.meshShadowFragmentShader) ||
                          !IsSpirv(program.meshPickingFragmentShader) || !IsSpirv(program.meshMotionVertexShader) ||
                          !IsSpirv(program.meshMotionFragmentShader))) {
            SetError(error, "GPU particle program contains invalid mesh output SPIR-V");
            return false;
        }
        if (needsRibbon && (!ribbonTopologyProgram || !ribbonTopologyProgram->IsValid() || !ribbonRenderProgram ||
                            !ribbonRenderProgram->IsValid())) {
            SetError(error, "GPU particle Ribbon topology or render shaders are unavailable");
            return false;
        }

        std::unordered_set<uint64_t> outputIds;
        std::unordered_set<std::string> outputStableIds;
        outputIds.reserve(program.outputs.size());
        outputStableIds.reserve(program.outputs.size());
        for (const auto &output : program.outputs) {
            if (output.id == 0 || output.stableId.empty() || !outputIds.insert(output.id).second ||
                !outputStableIds.insert(output.stableId).second) {
                SetError(error, "GPU particle output identity must be valid and unique per emitter");
                return false;
            }
            if (!output.shaderProgram || !output.shaderProgram->IsValid() ||
                output.shaderProgram->domain != ShaderProgramDomain::ParticleSprite) {
                SetError(error, "GPU particle surface outputs require a valid linked ParticleSprite shader");
                return false;
            }
            if (output.semantics.castShadows && output.type != GpuParticleOutputType::Mesh) {
                SetError(error, "only static mesh particle outputs can cast shadows");
                return false;
            }
            if (!output.semantics.IsValid()) {
                SetError(error, "GPU particle output '" + output.stableId + "' has invalid rendering semantics");
                return false;
            }
            if (output.semantics.receiveSceneLighting &&
                !output.shaderProgram->FindVariant(ShaderCompileTarget::ForwardPlus)) {
                SetError(error, "GPU particle output '" + output.stableId +
                                    "' requires a linked Particle Forward+ shader variant when Receive Scene "
                                    "Lighting is enabled");
                return false;
            }
            if (output.type == GpuParticleOutputType::Mesh && (!output.mesh || output.semantics.softParticles)) {
                SetError(error, "GPU particle mesh output '" + output.stableId +
                                    "' requires a loaded mesh and currently does not support soft fading");
                return false;
            }
            if (output.type == GpuParticleOutputType::Ribbon &&
                (output.semantics.castShadows || !std::isfinite(output.ribbonUvScale) ||
                 output.ribbonUvScale <= 0.0f)) {
                SetError(error, "GPU particle Ribbon output '" + output.stableId +
                                    "' cannot cast shadows and requires a positive finite UV scale");
                return false;
            }
            if (output.type != GpuParticleOutputType::Ribbon && output.semantics.sortMode != ParticleSortMode::None &&
                (!sortProgram || !sortProgram->IsValid())) {
                SetError(error, "GPU particle sorting kernels are unavailable");
                return false;
            }
        }
        if (!cullProgram || !cullProgram->IsValid()) {
            SetError(error, "GPU particle view-culling kernels are unavailable");
            return false;
        }
        if (program.preserveState &&
            (!previous || previous->id != program.id || previous->graphInstanceId != program.graphInstanceId ||
             previous->stableId != program.stableId)) {
            SetError(error, "GPU particle state preservation requires the same live emitter identity");
            return false;
        }
        return true;
    }

    [[nodiscard]] std::shared_ptr<Emitter> CreateEmitter(const GpuParticleEmitterProgram &program,
                                                         const std::shared_ptr<Emitter> &previous,
                                                         std::string *error) const
    {
        if (program.id == 0 || program.graphInstanceId == 0 || program.stableId.empty() ||
            program.artifactRevision == 0 || program.capacity == 0 || program.stateStride == 0) {
            SetError(error,
                     "GPU particle program graph identity, emitter identity, revision, capacity, and state stride "
                     "must be valid");
            return {};
        }
        for (const auto &kernel : program.kernels) {
            if (!IsSpirv(kernel)) {
                SetError(error, "GPU particle program contains invalid compute SPIR-V");
                return {};
            }
        }
        const bool hasContinuationKernels =
            std::any_of(program.continuationKernels.begin(), program.continuationKernels.end(),
                        [](const auto &kernel) { return !kernel.empty(); });
        if ((program.continuationCapacity == 0) != !hasContinuationKernels ||
            program.continuationCapacity > ParticleGpuContinuationRuntime::MaximumCapacity ||
            (program.continuationCapacity > 0 &&
             (program.continuationRecordStride < sizeof(GpuParticleContinuationRecord) ||
              program.continuationRecordStride > ParticleGpuContinuationRuntime::MaximumRecordStride ||
              program.continuationRecordStride % 16 != 0)) ||
            (program.continuationCapacity > 0 &&
             (program.continuationLaneCount == 0 ||
              program.continuationLaneCount > ParticleGpuContinuationRuntime::MaximumLaneCount ||
              program.continuationJoinCount > ParticleGpuContinuationRuntime::MaximumJoinCount)) ||
            (program.continuationCapacity > 0 &&
             !std::all_of(program.continuationKernels.begin(), program.continuationKernels.end(),
                          [](const auto &kernel) { return IsSpirv(kernel); }))) {
            SetError(error,
                     "GPU particle continuation capacity, record stride, static lane/join counts and "
                     "Prepare/Classify/Dispatch SPIR-V must be supplied together and satisfy the bounded runtime "
                     "contract");
            return {};
        }
        const bool needsSurfaceShader = !program.outputs.empty();
        if (needsSurfaceShader) {
            for (const auto &output : program.outputs) {
                if (!output.shaderProgram || !output.shaderProgram->IsValid() ||
                    output.shaderProgram->domain != ShaderProgramDomain::ParticleSprite) {
                    SetError(error, "GPU particle surface outputs require a valid linked ParticleSprite shader");
                    return {};
                }
            }
        }
        const bool needsMesh = std::any_of(program.outputs.begin(), program.outputs.end(), [](const auto &output) {
            return output.type == GpuParticleOutputType::Mesh;
        });
        const bool needsRibbon = std::any_of(program.outputs.begin(), program.outputs.end(), [](const auto &output) {
            return output.type == GpuParticleOutputType::Ribbon;
        });
        if (needsMesh && (!IsSpirv(program.meshVertexShader) || !IsSpirv(program.meshShadowFragmentShader) ||
                          !IsSpirv(program.meshPickingFragmentShader) || !IsSpirv(program.meshMotionVertexShader) ||
                          !IsSpirv(program.meshMotionFragmentShader))) {
            SetError(error, "GPU particle program contains invalid mesh output SPIR-V");
            return {};
        }
        if (needsRibbon && (!ribbonTopologyProgram || !ribbonTopologyProgram->IsValid() || !ribbonRenderProgram ||
                            !ribbonRenderProgram->IsValid())) {
            SetError(error, "GPU particle Ribbon topology or render shaders are unavailable");
            return {};
        }
        if (program.outputs.empty()) {
            SetError(error, "GPU particle program requires at least one rendering output");
            return {};
        }

        std::unordered_set<uint64_t> outputIds;
        std::unordered_set<std::string> outputStableIds;
        outputIds.reserve(program.outputs.size());
        outputStableIds.reserve(program.outputs.size());
        for (const auto &output : program.outputs) {
            if (output.id == 0 || output.stableId.empty() || !outputIds.insert(output.id).second ||
                !outputStableIds.insert(output.stableId).second) {
                SetError(error, "GPU particle output identity must be valid and unique per emitter");
                return {};
            }
            if (output.semantics.castShadows && output.type != GpuParticleOutputType::Mesh) {
                SetError(error, "only static mesh particle outputs can cast shadows");
                return {};
            }
            if (!output.semantics.IsValid()) {
                SetError(error, "GPU particle output '" + output.stableId + "' has invalid rendering semantics");
                return {};
            }
            if (output.semantics.receiveSceneLighting &&
                !output.shaderProgram->FindVariant(ShaderCompileTarget::ForwardPlus)) {
                SetError(error, "GPU particle output '" + output.stableId +
                                    "' requires a linked Particle Forward+ shader variant when Receive Scene "
                                    "Lighting is enabled");
                return {};
            }
            if (output.type == GpuParticleOutputType::Mesh && (!output.mesh || output.semantics.softParticles)) {
                SetError(error, "GPU particle mesh output '" + output.stableId +
                                    "' requires a loaded mesh and currently does not support soft fading");
                return {};
            }
            if (output.type == GpuParticleOutputType::Ribbon &&
                (output.semantics.castShadows || !std::isfinite(output.ribbonUvScale) ||
                 output.ribbonUvScale <= 0.0f)) {
                SetError(error, "GPU particle Ribbon output '" + output.stableId +
                                    "' cannot cast shadows and requires a positive finite UV scale");
                return {};
            }
            if (output.type != GpuParticleOutputType::Ribbon && output.semantics.sortMode != ParticleSortMode::None &&
                (!sortProgram || !sortProgram->IsValid())) {
                SetError(error, "GPU particle sorting kernels are unavailable");
                return {};
            }
        }
        if (!cullProgram || !cullProgram->IsValid()) {
            SetError(error, "GPU particle view-culling kernels are unavailable");
            return {};
        }

        auto emitter = std::make_shared<Emitter>();
        emitter->id = program.id;
        emitter->graphInstanceId = program.graphInstanceId;
        emitter->artifactRevision = program.artifactRevision;
        emitter->stableId = program.stableId;
        emitter->statePreservedOnPublish = program.preserveState;
        emitter->billboardVertexShader = program.billboardVertexShader;
        emitter->billboardPickingFragmentShader = program.billboardPickingFragmentShader;
        emitter->billboardMotionVertexShader = program.billboardMotionVertexShader;
        emitter->billboardMotionFragmentShader = program.billboardMotionFragmentShader;
        emitter->meshVertexShader = program.meshVertexShader;
        emitter->meshShadowFragmentShader = program.meshShadowFragmentShader;
        emitter->meshPickingFragmentShader = program.meshPickingFragmentShader;
        emitter->meshMotionVertexShader = program.meshMotionVertexShader;
        emitter->meshMotionFragmentShader = program.meshMotionFragmentShader;
        emitter->runtime = std::make_unique<ParticleGpuRuntime>();

        GpuEmitterDesc runtimeDesc;
        if (!BuildRuntimeDesc(program, runtimeDesc, emitter->vectorFields, emitter->vectorFieldGenerations, error))
            return {};
        emitter->observedVectorFieldGenerations = emitter->vectorFieldGenerations;
        CaptureMeshRevisions(program, emitter->meshes, emitter->observedMeshGenerations);
        emitter->sourceProgram = program;
        auto &device = context->GetRhiDevice();
        if (program.preserveState &&
            (!previous || previous->id != program.id || previous->graphInstanceId != program.graphInstanceId ||
             previous->stableId != program.stableId)) {
            SetError(error, "GPU particle state preservation requires the same live emitter identity");
            return {};
        }
        const bool requiresMigration = program.preserveState && program.migration.has_value();
        const bool runtimeCreated = requiresMigration ? emitter->runtime->Create(device, runtimeDesc)
                                    : program.preserveState
                                        ? emitter->runtime->CreateCompatible(device, runtimeDesc, *previous->runtime)
                                        : emitter->runtime->Create(device, runtimeDesc);
        if (!runtimeCreated) {
            SetError(error, program.preserveState
                                ? "GPU particle state ABI is incompatible with the requested hot reload"
                                : "failed to create GPU particle simulation runtime");
            return {};
        }
        if (requiresMigration) {
            const auto &migration = *program.migration;
            if (!migrationProgram || !migrationProgram->IsValid() ||
                migration.sourceStride != previous->runtime->StateStride() ||
                migration.destinationStride != emitter->runtime->StateStride()) {
                SetError(error, "GPU particle migration descriptor does not match the live state ABI");
                return {};
            }
            GpuParticleMigrationDesc migrationDesc;
            migrationDesc.sourceCapacity = previous->runtime->Capacity();
            migrationDesc.destinationCapacity = emitter->runtime->Capacity();
            migrationDesc.sourceStride = migration.sourceStride;
            migrationDesc.destinationStride = migration.destinationStride;
            migrationDesc.sourceStates = previous->runtime->StateBuffer();
            migrationDesc.sourceCounters = previous->runtime->CounterBuffer();
            migrationDesc.sourceCounterByteSize = previous->runtime->CounterBufferByteSize();
            migrationDesc.destinationStates = emitter->runtime->StateBuffer();
            migrationDesc.destinationFreeList = emitter->runtime->FreeListBuffer();
            migrationDesc.destinationCounters = emitter->runtime->CounterBuffer();
            migrationDesc.copyRanges = migration.copyRanges;
            migrationDesc.defaultStateWords = migration.defaultStateWords;
            migrationDesc.program = migrationProgram->View();
            emitter->migration = std::make_shared<ParticleGpuMigrator>();
            if (!emitter->migration->Create(device, migrationDesc)) {
                SetError(error, "failed to create GPU particle state migrator");
                return {};
            }
            emitter->migrationSource = previous;
        }
        if (!boundsProgram || !boundsProgram->IsValid()) {
            SetError(error, "GPU particle bounds kernels are unavailable");
            return {};
        }
        emitter->bounds = std::make_unique<ParticleGpuBounds>();
        GpuParticleBoundsDesc boundsDesc;
        boundsDesc.capacity = emitter->runtime->Capacity();
        boundsDesc.instances = emitter->runtime->InstanceBuffer();
        boundsDesc.sourceIndices = emitter->runtime->RenderIndexBuffer();
        boundsDesc.sourceIndirectArguments = emitter->runtime->IndirectBuffer();
        boundsDesc.simulationControl = emitter->runtime->SimulationControlBuffer();
        boundsDesc.program = boundsProgram->View();
        if (!emitter->bounds->Create(device, boundsDesc)) {
            SetError(error, "failed to create GPU particle bounds reducer");
            return {};
        }
        if (needsRibbon) {
            GpuParticleRibbonDesc ribbonDesc;
            ribbonDesc.capacity = emitter->runtime->Capacity();
            ribbonDesc.instances = emitter->runtime->InstanceBuffer();
            ribbonDesc.sourceIndices = emitter->runtime->RenderIndexBuffer();
            ribbonDesc.sourceIndirectArguments = emitter->runtime->IndirectBuffer();
            ribbonDesc.simulationControl = emitter->runtime->SimulationControlBuffer();
            ribbonDesc.program = ribbonTopologyProgram->View();
            emitter->ribbonTopology = std::make_shared<ParticleGpuRibbonTopology>();
            if (!emitter->ribbonTopology->Create(device, ribbonDesc)) {
                SetError(error, "failed to create GPU-resident Ribbon topology");
                return {};
            }
        }

        emitter->outputs.reserve(program.outputs.size());
        for (const auto &output : program.outputs) {
            auto renderer = CreateOutputRenderer(*emitter, output, error);
            if (!renderer) {
                if (!error || error->empty())
                    SetError(error,
                             "failed to create GPU particle output renderer for output '" + output.stableId + "'");
                return {};
            }
            Emitter::Output emitterOutput;
            emitterOutput.id = output.id;
            emitterOutput.stableId = output.stableId;
            emitterOutput.type = output.type;
            emitterOutput.mesh = output.mesh;
            emitterOutput.material = output.material;
            emitterOutput.shaderProgram = output.shaderProgram;
            emitterOutput.fallbackMaterial = output.fallbackMaterial;
            emitterOutput.semantics = output.semantics;
            if (output.type == GpuParticleOutputType::Ribbon)
                emitterOutput.semantics.sortMode = ParticleSortMode::None;
            emitterOutput.ribbonUvMode = output.ribbonUvMode;
            emitterOutput.ribbonUvScale = output.ribbonUvScale;
            emitterOutput.flipbookColumns = output.flipbookColumns;
            emitterOutput.flipbookRows = output.flipbookRows;
            emitterOutput.renderer = std::move(renderer);
            emitter->outputs.push_back(std::move(emitterOutput));
        }
        return emitter;
    }

    [[nodiscard]] bool RefreshResources(Emitter &emitter)
    {
        if (emitter.vectorFields.empty() && emitter.meshes.empty())
            return true;
        std::vector<uint64_t> currentVectorFieldGenerations;
        currentVectorFieldGenerations.reserve(emitter.vectorFields.size());
        bool textureChanged = emitter.vectorFields.size() != emitter.observedVectorFieldGenerations.size();
        for (size_t index = 0; index < emitter.vectorFields.size(); ++index) {
            const uint64_t generation = emitter.vectorFields[index] ? emitter.vectorFields[index]->GetGeneration() : 0;
            currentVectorFieldGenerations.push_back(generation);
            textureChanged = textureChanged || index >= emitter.observedVectorFieldGenerations.size() ||
                             generation != emitter.observedVectorFieldGenerations[index];
        }
        std::vector<uint64_t> currentMeshGenerations;
        currentMeshGenerations.reserve(emitter.meshes.size());
        bool meshChanged = emitter.meshes.size() != emitter.observedMeshGenerations.size();
        for (size_t index = 0; index < emitter.meshes.size(); ++index) {
            const uint64_t generation = emitter.meshes[index] ? emitter.meshes[index]->GetGeneration() : 0;
            currentMeshGenerations.push_back(generation);
            meshChanged = meshChanged || index >= emitter.observedMeshGenerations.size() ||
                          generation != emitter.observedMeshGenerations[index];
        }
        if (!textureChanged && !meshChanged)
            return true;

        std::string error;
        GpuEmitterDesc runtimeDesc;
        std::vector<std::shared_ptr<InxTexture>> vectorFields;
        std::vector<uint64_t> vectorFieldGenerations;
        auto replacement = std::make_unique<ParticleGpuRuntime>();
        const bool descriptorValid =
            BuildRuntimeDesc(emitter.sourceProgram, runtimeDesc, vectorFields, vectorFieldGenerations, &error);
        const bool replacementCreated =
            descriptorValid && replacement->CreateCompatible(context->GetRhiDevice(), runtimeDesc, *emitter.runtime);
        const auto rememberHardFailure = [&]() {
            emitter.observedVectorFieldGenerations = currentVectorFieldGenerations;
            emitter.observedMeshGenerations = currentMeshGenerations;
        };
        const auto uploadPending = [&]() { return error.find("upload is pending") != std::string::npos; };
        if (!replacementCreated || !deletionQueue) {
            if (!uploadPending())
                rememberHardFailure();
            if (!uploadPending())
                INXLOG_WARN("GPU particle resource refresh kept last-known-good emitter '", emitter.stableId,
                            "': ", error.empty() ? "failed to create a compatible RHI revision" : error);
            return false;
        }

        struct RendererReplacement
        {
            size_t outputIndex = 0;
            std::shared_ptr<ParticleGpuOutputRenderer> previous;
            std::shared_ptr<ParticleGpuOutputRenderer> next;
        };
        std::vector<RendererReplacement> renderers;
        if (meshChanged) {
            for (size_t index = 0; index < emitter.outputs.size(); ++index) {
                const auto &output = emitter.outputs[index];
                if (output.type != GpuParticleOutputType::Mesh)
                    continue;
                GpuParticleOutputProgram candidate;
                candidate.id = output.id;
                candidate.stableId = output.stableId;
                candidate.type = output.type;
                candidate.mesh = output.mesh;
                candidate.material = output.material;
                candidate.shaderProgram = output.shaderProgram;
                candidate.fallbackMaterial = output.fallbackMaterial;
                candidate.semantics = output.semantics;
                candidate.ribbonUvMode = output.ribbonUvMode;
                candidate.ribbonUvScale = output.ribbonUvScale;
                candidate.flipbookColumns = output.flipbookColumns;
                candidate.flipbookRows = output.flipbookRows;
                auto next = CreateOutputRenderer(emitter, candidate, &error);
                if (!next) {
                    if (!uploadPending())
                        rememberHardFailure();
                    if (!uploadPending())
                        INXLOG_WARN("GPU particle Mesh output refresh kept last-known-good emitter '", emitter.stableId,
                                    "': ", error.empty() ? "renderer creation failed" : error);
                    return false;
                }
                renderers.push_back({index, output.renderer, std::move(next)});
            }
        }

        if (!emitter.runtime->AdoptCompatibleRevision(*replacement)) {
            rememberHardFailure();
            INXLOG_WARN("GPU particle resource refresh kept last-known-good emitter '", emitter.stableId,
                        "': compatible runtime adoption failed");
            return false;
        }
        for (auto &renderer : renderers)
            emitter.outputs[renderer.outputIndex].renderer = renderer.next;

        if (!renderers.empty() && (!drawRegistry || !drawRegistry->Replace(BuildDrawEntries(emitters)))) {
            for (auto &renderer : renderers)
                emitter.outputs[renderer.outputIndex].renderer = renderer.previous;
            const bool restored = emitter.runtime->AdoptCompatibleRevision(*replacement);
            rememberHardFailure();
            INXLOG_WARN("GPU particle resource refresh kept last-known-good emitter '", emitter.stableId,
                        "': draw registry publication failed", restored ? "" : " and runtime rollback failed");
            return false;
        }

        emitter.vectorFields = std::move(vectorFields);
        emitter.vectorFieldGenerations = vectorFieldGenerations;
        emitter.observedVectorFieldGenerations = std::move(vectorFieldGenerations);
        CaptureMeshRevisions(emitter.sourceProgram, emitter.meshes, emitter.observedMeshGenerations);

        std::vector<std::shared_ptr<ParticleGpuOutputRenderer>> retiredRenderers;
        retiredRenderers.reserve(renderers.size());
        for (auto &renderer : renderers)
            retiredRenderers.push_back(std::move(renderer.previous));
        auto retiredRuntime = std::shared_ptr<ParticleGpuRuntime>(std::move(replacement));
        deletionQueue->Retire(
            [retiredRuntime = std::move(retiredRuntime), retiredRenderers = std::move(retiredRenderers)]() mutable {
                retiredRenderers.clear();
                retiredRuntime.reset();
            });
        return true;
    }

    [[nodiscard]] std::shared_ptr<GraphState> BuildGraph(const EmitterMap &candidateEmitters, std::string *error) const
    {
        auto state = std::make_shared<GraphState>();
        state->generation = nextGraphGeneration++;
        state->graph = std::make_unique<vk::RenderGraph>();
        state->graph->Initialize(context, pipelines, deletionQueue);
        auto *graph = state->graph.get();
        std::map<uint64_t, std::vector<std::shared_ptr<Emitter>>> emittersByGraph;
        for (const auto &[id, emitter] : candidateEmitters) {
            (void)id;
            if (!emitter || emitter->graphInstanceId == 0) {
                SetError(error, "GPU particle graph contains an emitter without graph ownership");
                return {};
            }
            emittersByGraph[emitter->graphInstanceId].push_back(emitter);
        }
        for (auto &[graphInstanceId, graphEmitters] : emittersByGraph) {
            std::sort(graphEmitters.begin(), graphEmitters.end(), [](const auto &lhs, const auto &rhs) {
                return lhs->sourceProgram.graphEmitterIndex < rhs->sourceProgram.graphEmitterIndex;
            });
            for (uint32_t slot = 0; slot < graphEmitters.size(); ++slot) {
                if (graphEmitters[slot]->sourceProgram.graphEmitterIndex != slot) {
                    SetError(error, "GPU particle graph emitter indices must be dense and start at zero");
                    return {};
                }
            }
            auto domain = std::make_shared<ParticleGpuGraphSpawnDomain>();
            if (!spawnProgram || !spawnProgram->IsValid() ||
                !domain->Create(context->GetRhiDevice(), graphInstanceId, static_cast<uint32_t>(graphEmitters.size()),
                                spawnProgram->View())) {
                SetError(error, "failed to create the GPU particle graph spawn domain");
                return {};
            }
            for (const auto &emitter : graphEmitters) {
                if (!domain->RegisterEmitter(emitter->sourceProgram.graphEmitterIndex, *emitter->runtime)) {
                    SetError(error, "failed to bind an emitter to the GPU particle graph spawn domain");
                    return {};
                }
            }
            const std::string prefix = "GpuParticleGraph/" + std::to_string(graphInstanceId);
            if (!domain->Attach(*graph, prefix)) {
                SetError(error, "failed to attach the GPU particle graph spawn prepass");
                return {};
            }
            state->spawnDomains.emplace(graphInstanceId, std::move(domain));
        }
        state->schedulers.reserve(candidateEmitters.size());
        state->schedulerById.reserve(candidateEmitters.size());
        for (const auto &[id, emitter] : candidateEmitters) {
            auto scheduler = std::make_unique<ParticleRenderGraph>();
            const std::string prefix = "GpuParticle/" + std::to_string(id);
            const auto domain = state->spawnDomains.find(emitter->graphInstanceId);
            if (domain == state->spawnDomains.end() ||
                !scheduler->Attach(*state->graph, *emitter->runtime, *emitter->bounds, *domain->second,
                                   emitter->sourceProgram.graphEmitterIndex, prefix, emitter->migration.get(),
                                   emitter->ribbonTopology.get())) {
                SetError(error, "failed to attach GPU particle emitter to the simulation graph");
                return {};
            }
            state->schedulerById.emplace(id, scheduler.get());
            state->schedulers.push_back(std::move(scheduler));
        }
        if (!candidateEmitters.empty() && !state->graph->Compile()) {
            SetError(error, "failed to compile the GPU particle simulation graph");
            return {};
        }
        if (!state->schedulers.empty()) {
            uint32_t firstExportPass = UINT32_MAX;
            for (const auto &scheduler : state->schedulers)
                firstExportPass = (std::min)(firstExportPass, scheduler->RenderExportPassId());
            const auto &plan = state->graph->GetSubmissionPlan();
            for (const auto &batch : plan.batches) {
                if (std::find(batch.workItems.begin(), batch.workItems.end(), firstExportPass) !=
                    batch.workItems.end()) {
                    state->renderExportBatch = batch.index;
                    break;
                }
            }
        }
        return state;
    }

    [[nodiscard]] std::vector<GpuParticleDrawEntry> BuildDrawEntries(const EmitterMap &candidateEmitters) const
    {
        std::vector<GpuParticleDrawEntry> entries;
        size_t outputCount = 0;
        for (const auto &[id, emitter] : candidateEmitters) {
            (void)id;
            outputCount += emitter->outputs.size();
        }
        entries.reserve(outputCount);
        for (const auto &[id, emitter] : candidateEmitters) {
            (void)id;
            for (const auto &output : emitter->outputs) {
                GpuParticleDrawEntry entry;
                entry.id = output.id;
                entry.emitterId = emitter->id;
                entry.graphInstanceId = emitter->graphInstanceId;
                entry.emitterIndex = emitter->sourceProgram.graphEmitterIndex;
                entry.outputStableId = output.stableId;
                entry.ownerObjectId = emitter->sourceProgram.ownerObjectId;
                entry.ownerLayerMask = emitter->sourceProgram.ownerLayerMask;
                entry.capacity = emitter->runtime->Capacity();
                entry.instances = emitter->runtime->InstanceBuffer();
                entry.renderIndices = output.renderer->RenderIndexBuffer();
                entry.indirectArguments = output.type == GpuParticleOutputType::Ribbon
                                              ? emitter->ribbonTopology->DrawIndirectBuffer()
                                              : emitter->runtime->IndirectBuffer();
                entry.bounds = emitter->bounds->BoundsBuffer();
                entry.simulationControl = emitter->runtime->SimulationControlBuffer();
                entry.renderer = output.renderer;
                entry.cullProgram = cullProgram;
                entry.cullMode = output.type == GpuParticleOutputType::Ribbon ? GpuParticleCullMode::RibbonSegments
                                                                              : GpuParticleCullMode::Instances;
                entry.sortProgram =
                    output.type == GpuParticleOutputType::Ribbon || output.semantics.sortMode == ParticleSortMode::None
                        ? nullptr
                        : sortProgram;
                entry.semantics = output.semantics;
                if (output.type == GpuParticleOutputType::Ribbon) {
                    entry.semantics.sortMode = ParticleSortMode::None;
                    entry.semantics.castShadows = false;
                }
                entries.push_back(std::move(entry));
            }
        }
        return entries;
    }

    void Retire(std::shared_ptr<GraphState> oldGraph, EmitterMap oldEmitters)
    {
        if (oldEmitters.empty())
            return;
        if (deletionQueue) {
            deletionQueue->Retire([oldGraph = std::move(oldGraph), oldEmitters = std::move(oldEmitters)]() mutable {
                oldGraph.reset();
                oldEmitters.clear();
            });
        }
    }

    void RetireCompletedMigrations()
    {
        if (!graphState)
            return;
        std::vector<uint64_t> completedIds;
        for (const auto &[id, emitter] : emitters) {
            (void)emitter;
            const auto scheduler = graphState->schedulerById.find(id);
            if (scheduler != graphState->schedulerById.end() && scheduler->second->HasCompletedMigration())
                completedIds.push_back(id);
        }
        if (completedIds.empty())
            return;

        struct RetiredMigration
        {
            std::shared_ptr<ParticleGpuMigrator> migrator;
            std::shared_ptr<Emitter> source;
        };
        std::vector<RetiredMigration> retired;
        retired.reserve(completedIds.size());
        for (const uint64_t id : completedIds) {
            auto &emitter = emitters.at(id);
            retired.push_back({std::move(emitter->migration), std::move(emitter->migrationSource)});
        }

        auto replacementGraph = BuildGraph(emitters, nullptr);
        if (!replacementGraph) {
            for (size_t index = 0; index < completedIds.size(); ++index) {
                auto &emitter = emitters.at(completedIds[index]);
                emitter->migration = std::move(retired[index].migrator);
                emitter->migrationSource = std::move(retired[index].source);
            }
            return;
        }

        for (const uint64_t id : completedIds) {
            const auto scheduler = graphState->schedulerById.find(id);
            if (scheduler != graphState->schedulerById.end())
                (void)scheduler->second->ConsumeMigrationCompletion();
        }
        auto retiredGraph = std::move(graphState);
        graphState = std::move(replacementGraph);
        EmitterMap graphLifetime = emitters;
        if (deletionQueue) {
            deletionQueue->Retire([graph = std::move(retiredGraph), emitters = std::move(graphLifetime),
                                   migrations = std::move(retired)]() mutable {
                graph.reset();
                emitters.clear();
                migrations.clear();
            });
        }
    }
};

ParticleGpuSystemManager::ParticleGpuSystemManager() : m_impl(std::make_unique<Impl>())
{
}

ParticleGpuSystemManager::~ParticleGpuSystemManager()
{
    Shutdown();
}

bool ParticleGpuSystemManager::Initialize(
    vk::VkDeviceContext &context, vk::VkPipelineManager &pipelines, vk::VkResourceManager &resources,
    GpuRetirementQueue &deletionQueue, ParticleGpuDrawRegistry &drawRegistry,
    GpuBillboardTextureResolver textureResolver, GpuParticleVectorFieldTextureResolver vectorFieldTextureResolver,
    GpuParticleSkinnedMeshResolver skinnedMeshResolver, const GpuParticleSortProgram &sortProgram,
    const GpuParticleCullProgram &cullProgram, const GpuParticleBoundsProgram &boundsProgram,
    const GpuParticleMigrationProgram &migrationProgram, const GpuParticleSpawnProgram &spawnProgram,
    const GpuParticleRibbonProgram &ribbonTopologyProgram, const GpuParticleRibbonRenderProgram &ribbonRenderProgram,
    uint32_t framesInFlight)
{
    if (!m_impl || m_impl->context || !context.IsValid() || !boundsProgram.IsValid() || !migrationProgram.IsValid() ||
        !spawnProgram.IsValid() || framesInFlight == 0 ||
        ribbonTopologyProgram.IsValid() != ribbonRenderProgram.IsValid()) {
        INXLOG_ERROR("ParticleGpuSystemManager initialization contract rejected: impl=", m_impl != nullptr,
                     " already_initialized=", m_impl && m_impl->context != nullptr, " context=", context.IsValid(),
                     " bounds=", boundsProgram.IsValid(), " migration=", migrationProgram.IsValid(),
                     " spawn=", spawnProgram.IsValid(), " ribbon_topology=", ribbonTopologyProgram.IsValid(),
                     " ribbon_render=", ribbonRenderProgram.IsValid(), " frames=", framesInFlight);
        return false;
    }
    m_impl->context = &context;
    m_impl->pipelines = &pipelines;
    m_impl->resources = &resources;
    m_impl->deletionQueue = &deletionQueue;
    m_impl->drawRegistry = &drawRegistry;
    m_impl->textureResolver = std::move(textureResolver);
    m_impl->vectorFieldTextureResolver = std::move(vectorFieldTextureResolver);
    m_impl->skinnedMeshResolver = std::move(skinnedMeshResolver);
    m_impl->collisionScene = std::make_unique<ParticleGpuCollisionScene>();
    if (!m_impl->collisionScene->Create(context.GetRhiDevice(), ParticleGpuCollisionScene::DefaultCapacity,
                                        framesInFlight)) {
        INXLOG_ERROR("ParticleGpuSystemManager failed to create the collision scene");
        Shutdown();
        return false;
    }
    auto boundsStorage = std::make_shared<GpuParticleBoundsProgramStorage>();
    if (!boundsStorage->Assign(boundsProgram)) {
        INXLOG_ERROR("ParticleGpuSystemManager failed to retain the bounds program");
        Shutdown();
        return false;
    }
    m_impl->boundsProgram = std::move(boundsStorage);
    auto migrationStorage = std::make_shared<GpuParticleMigrationProgramStorage>();
    if (!migrationStorage->Assign(migrationProgram)) {
        INXLOG_ERROR("ParticleGpuSystemManager failed to retain the migration program");
        Shutdown();
        return false;
    }
    m_impl->migrationProgram = std::move(migrationStorage);
    auto spawnStorage = std::make_shared<GpuParticleSpawnProgramStorage>();
    if (!spawnStorage->Assign(spawnProgram)) {
        INXLOG_ERROR("ParticleGpuSystemManager failed to retain the graph spawn program");
        Shutdown();
        return false;
    }
    m_impl->spawnProgram = std::move(spawnStorage);
    if (ribbonTopologyProgram.IsValid()) {
        auto ribbonTopologyStorage = std::make_shared<GpuParticleRibbonProgramStorage>();
        auto ribbonRenderStorage = std::make_shared<GpuParticleRibbonRenderProgramStorage>();
        if (!ribbonTopologyStorage->Assign(ribbonTopologyProgram) ||
            !ribbonRenderStorage->Assign(ribbonRenderProgram)) {
            INXLOG_ERROR("ParticleGpuSystemManager failed to retain the ribbon programs");
            Shutdown();
            return false;
        }
        m_impl->ribbonTopologyProgram = std::move(ribbonTopologyStorage);
        m_impl->ribbonRenderProgram = std::move(ribbonRenderStorage);
    }
    if (cullProgram.IsValid()) {
        auto storage = std::make_shared<GpuParticleCullProgramStorage>();
        if (!storage->Assign(cullProgram)) {
            INXLOG_ERROR("ParticleGpuSystemManager failed to retain the cull program");
            Shutdown();
            return false;
        }
        m_impl->cullProgram = std::move(storage);
    }
    if (sortProgram.IsValid()) {
        auto storage = std::make_shared<GpuParticleSortProgramStorage>();
        if (!storage->Assign(sortProgram)) {
            INXLOG_ERROR("ParticleGpuSystemManager failed to retain the sort program");
            Shutdown();
            return false;
        }
        m_impl->sortProgram = std::move(storage);
    }
    std::string graphError;
    m_impl->graphState = m_impl->BuildGraph({}, &graphError);
    if (!m_impl->graphState) {
        INXLOG_ERROR("ParticleGpuSystemManager failed to create the initial graph: ", graphError);
        Shutdown();
        return false;
    }
    return true;
}

void ParticleGpuSystemManager::Shutdown() noexcept
{
    if (!m_impl)
        return;
    if (m_impl->drawRegistry)
        m_impl->drawRegistry->Clear();
    m_impl->FailDiagnostics(0, "GPU particle manager shut down before diagnostic recording completed");
    m_impl->graphState.reset();
    m_impl->emitters.clear();
    m_impl->meshUploads.clear();
    m_impl->collisionScene.reset();
    m_impl->drawRegistry = nullptr;
    m_impl->textureResolver = {};
    m_impl->vectorFieldTextureResolver = {};
    m_impl->skinnedMeshResolver = {};
    m_impl->cullProgram.reset();
    m_impl->sortProgram.reset();
    m_impl->boundsProgram.reset();
    m_impl->migrationProgram.reset();
    m_impl->spawnProgram.reset();
    m_impl->ribbonTopologyProgram.reset();
    m_impl->ribbonRenderProgram.reset();
    m_impl->deletionQueue = nullptr;
    m_impl->pipelines = nullptr;
    m_impl->resources = nullptr;
    m_impl->context = nullptr;
}

bool ParticleGpuSystemManager::ApplyGraph(const GpuParticleGraphProgram &program, std::string *error)
{
    if (error)
        error->clear();
    if (!m_impl || !m_impl->context || !m_impl->drawRegistry) {
        SetError(error, "GPU particle manager is not initialized");
        return false;
    }
    if (program.graphInstanceId == 0) {
        SetError(error, "GPU particle graph publication requires a valid graph instance id");
        return false;
    }
    if (program.emitters.empty() && program.removeEmitterIds.empty()) {
        SetError(error, "GPU particle update batch cannot be empty");
        return false;
    }

    Impl::EmitterMap candidates = m_impl->emitters;
    std::unordered_set<uint64_t> batchIds;
    batchIds.reserve(program.emitters.size() + program.removeEmitterIds.size());
    for (const uint64_t id : program.removeEmitterIds) {
        if (id == 0 || !batchIds.insert(id).second) {
            SetError(error, "GPU particle update batch contains invalid or duplicate removal ids");
            return false;
        }
        const auto existing = m_impl->emitters.find(id);
        if (existing != m_impl->emitters.end() && existing->second->graphInstanceId != program.graphInstanceId) {
            SetError(error, "GPU particle graph cannot remove another graph's emitter");
            return false;
        }
        candidates.erase(id);
    }
    for (const auto &emitterProgram : program.emitters) {
        if (emitterProgram.graphInstanceId != program.graphInstanceId) {
            SetError(error, "GPU particle update batch mixes multiple graph instances");
            return false;
        }
        if (!batchIds.insert(emitterProgram.id).second) {
            SetError(error, "GPU particle update batch contains duplicate or conflicting emitter ids");
            return false;
        }
        const auto previous = m_impl->emitters.find(emitterProgram.id);
        if (previous != m_impl->emitters.end() && previous->second->graphInstanceId != program.graphInstanceId) {
            SetError(error, "GPU particle emitter id is already owned by another graph");
            return false;
        }
        if (!m_impl->PreflightEmitterProgram(
                emitterProgram,
                previous != m_impl->emitters.end() ? previous->second : std::shared_ptr<Impl::Emitter>{}, error))
            return false;
    }
    for (const auto &emitterProgram : program.emitters) {
        const auto previous = m_impl->emitters.find(emitterProgram.id);
        auto emitter = m_impl->CreateEmitter(
            emitterProgram, previous != m_impl->emitters.end() ? previous->second : std::shared_ptr<Impl::Emitter>{},
            error);
        if (!emitter)
            return false;
        candidates[emitterProgram.id] = std::move(emitter);
    }

    auto candidateGraph = m_impl->BuildGraph(candidates, error);
    if (!candidateGraph)
        return false;
    if (!m_impl->drawRegistry->Replace(m_impl->BuildDrawEntries(candidates))) {
        SetError(error, "failed to publish GPU particle draw entries");
        return false;
    }

    auto oldGraph = std::move(m_impl->graphState);
    auto oldEmitters = std::move(m_impl->emitters);
    m_impl->FailDiagnostics(program.graphInstanceId,
                            "GPU particle graph changed before diagnostic recording completed");
    m_impl->graphState = std::move(candidateGraph);
    m_impl->emitters = std::move(candidates);
    m_impl->Retire(std::move(oldGraph), std::move(oldEmitters));
    return true;
}

bool ParticleGpuSystemManager::UpdateGraphParameters(uint64_t graphInstanceId,
                                                     const std::vector<uint32_t> &parameterWords, std::string *error)
{
    if (error)
        error->clear();
    if (!m_impl || !m_impl->context || graphInstanceId == 0 || parameterWords.empty() ||
        parameterWords.size() % 4 != 0) {
        SetError(error, "GPU particle parameter update is invalid");
        return false;
    }
    std::vector<std::shared_ptr<Impl::Emitter>> targets;
    for (const auto &[id, emitter] : m_impl->emitters) {
        (void)id;
        if (emitter && emitter->graphInstanceId == graphInstanceId)
            targets.push_back(emitter);
    }
    if (targets.empty()) {
        SetError(error, "GPU particle graph instance is not active");
        return false;
    }
    if (std::any_of(targets.begin(), targets.end(), [&](const auto &emitter) {
            return !emitter->runtime || emitter->sourceProgram.parameterWords.size() != parameterWords.size();
        })) {
        SetError(error, "GPU particle parameter layout does not match the active graph");
        return false;
    }
    for (const auto &emitter : targets) {
        if (!emitter->runtime->UpdateParameters(parameterWords)) {
            SetError(error, "GPU particle parameter upload failed");
            return false;
        }
    }
    for (const auto &emitter : targets)
        emitter->sourceProgram.parameterWords = parameterWords;
    return true;
}

bool ParticleGpuSystemManager::PublishCollisionScene(const GpuParticleCollisionSceneSnapshot &snapshot,
                                                     std::string *error)
{
    if (!m_impl || !m_impl->collisionScene) {
        SetError(error, "GPU particle collision scene is not initialized");
        return false;
    }
    return m_impl->collisionScene->Publish(snapshot, error);
}

uint64_t ParticleGpuSystemManager::CollisionSceneRevision() const noexcept
{
    return m_impl && m_impl->collisionScene ? m_impl->collisionScene->PublishedRevision() : 0;
}

uint32_t ParticleGpuSystemManager::CollisionSceneColliderCount() const noexcept
{
    return m_impl && m_impl->collisionScene ? m_impl->collisionScene->PublishedColliderCount() : 0;
}

bool ParticleGpuSystemManager::RefreshMaterialProgram(const std::shared_ptr<InxMaterial> &material,
                                                      std::shared_ptr<const ShaderProgramArtifact> shaderProgram,
                                                      std::string *error)
{
    if (error)
        error->clear();
    if (!m_impl || !m_impl->context || !m_impl->drawRegistry) {
        SetError(error, "GPU particle manager is not initialized");
        return false;
    }
    if (!material) {
        SetError(error, "GPU particle material refresh requires a live material");
        return false;
    }
    const bool referenced = std::any_of(m_impl->emitters.begin(), m_impl->emitters.end(), [&](const auto &entry) {
        return std::any_of(entry.second->outputs.begin(), entry.second->outputs.end(),
                           [&](const auto &output) { return output.material.get() == material.get(); });
    });
    if (!referenced)
        return true;
    if (shaderProgram && (!shaderProgram->IsValid() || shaderProgram->domain != ShaderProgramDomain::ParticleSprite)) {
        SetError(error, "GPU particle material must resolve to a valid linked ParticleSprite shader program");
        return false;
    }

    struct Replacement
    {
        Impl::Emitter::Output *output = nullptr;
        std::shared_ptr<const ShaderProgramArtifact> previousProgram;
        std::shared_ptr<ParticleGpuOutputRenderer> previousRenderer;
        std::shared_ptr<ParticleGpuOutputRenderer> nextRenderer;
    };
    std::vector<Replacement> replacements;
    for (const auto &[id, emitter] : m_impl->emitters) {
        (void)id;
        for (auto &output : emitter->outputs) {
            if (output.material.get() != material.get())
                continue;
            const bool sameProgram =
                output.shaderProgram && shaderProgram && output.shaderProgram->key == shaderProgram->key;
            if (sameProgram)
                continue;
            if (!shaderProgram || !shaderProgram->IsValid() ||
                shaderProgram->domain != ShaderProgramDomain::ParticleSprite) {
                SetError(error, "GPU particle surface output requires a valid linked ParticleSprite shader program");
                return false;
            }
            if (output.semantics.receiveSceneLighting &&
                !shaderProgram->FindVariant(ShaderCompileTarget::ForwardPlus)) {
                SetError(error, "GPU particle output '" + output.stableId +
                                    "' requires a linked Particle Forward+ shader variant when Receive Scene "
                                    "Lighting is enabled");
                return false;
            }
            GpuParticleOutputProgram candidate;
            candidate.id = output.id;
            candidate.stableId = output.stableId;
            candidate.type = output.type;
            candidate.mesh = output.mesh;
            candidate.material = material;
            candidate.shaderProgram = shaderProgram;
            candidate.fallbackMaterial = output.fallbackMaterial;
            candidate.semantics = output.semantics;
            candidate.flipbookColumns = output.flipbookColumns;
            candidate.flipbookRows = output.flipbookRows;
            candidate.ribbonUvMode = output.ribbonUvMode;
            candidate.ribbonUvScale = output.ribbonUvScale;
            auto renderer = m_impl->CreateOutputRenderer(*emitter, candidate, error);
            if (!renderer) {
                SetError(error, "failed to refresh GPU particle material for output '" + output.stableId + "'");
                return false;
            }
            replacements.push_back({&output, output.shaderProgram, output.renderer, std::move(renderer)});
        }
    }
    if (replacements.empty())
        return true;

    for (auto &replacement : replacements) {
        replacement.output->shaderProgram = shaderProgram;
        replacement.output->renderer = replacement.nextRenderer;
    }
    if (!m_impl->drawRegistry->Replace(m_impl->BuildDrawEntries(m_impl->emitters))) {
        for (auto &replacement : replacements) {
            replacement.output->shaderProgram = std::move(replacement.previousProgram);
            replacement.output->renderer = std::move(replacement.previousRenderer);
        }
        SetError(error, "failed to publish refreshed GPU particle material draw entries");
        return false;
    }

    std::vector<std::shared_ptr<ParticleGpuOutputRenderer>> retired;
    retired.reserve(replacements.size());
    for (auto &replacement : replacements)
        retired.push_back(std::move(replacement.previousRenderer));
    if (m_impl->deletionQueue) {
        m_impl->deletionQueue->Retire([retired = std::move(retired)]() mutable { retired.clear(); });
    }
    return true;
}

void ParticleGpuSystemManager::Clear()
{
    if (!m_impl || !m_impl->context || m_impl->emitters.empty())
        return;
    auto candidateGraph = m_impl->BuildGraph({}, nullptr);
    if (!candidateGraph)
        return;
    if (m_impl->drawRegistry && !m_impl->drawRegistry->Replace({}))
        return;
    auto oldGraph = std::move(m_impl->graphState);
    auto oldEmitters = std::move(m_impl->emitters);
    m_impl->FailDiagnostics(0, "GPU particle graph was cleared before diagnostic recording completed");
    m_impl->graphState = std::move(candidateGraph);
    m_impl->emitters.clear();
    m_impl->Retire(std::move(oldGraph), std::move(oldEmitters));
}

bool ParticleGpuSystemManager::BeginFrame(uint64_t id, const GpuParticleFrameRequest &request,
                                          const GpuParticleTransforms &transforms)
{
    if (!m_impl)
        return false;
    const auto emitter = m_impl->emitters.find(id);
    if (emitter == m_impl->emitters.end())
        return false;
    return BeginFrameBatch(emitter->second->graphInstanceId, {{id, {}, request, transforms}});
}

bool ParticleGpuSystemManager::BeginFrameBatch(uint64_t graphInstanceId,
                                               const std::vector<GpuParticleBatchFrameItem> &items)
{
    if (!m_impl || !m_impl->graphState || graphInstanceId == 0 || items.empty())
        return false;

    struct PreparedItem
    {
        const GpuParticleBatchFrameItem *item = nullptr;
        std::shared_ptr<Impl::Emitter> emitter;
        ParticleRenderGraph *scheduler = nullptr;
        std::vector<GpuParticleFrameRequest> sequence;
    };
    std::vector<PreparedItem> prepared;
    prepared.reserve(items.size());
    std::unordered_set<uint64_t> emitterIds;
    emitterIds.reserve(items.size());
    const uint64_t frameIndex = items.front().request.frameIndex;
    const bool resetCollisionDiagnostics = std::any_of(
        m_impl->pendingDiagnostics.begin(), m_impl->pendingDiagnostics.end(), [graphInstanceId](const auto &request) {
            return request.graphInstanceId == graphInstanceId && request.resetPending;
        });
    const bool collectCollisionDiagnostics = std::any_of(
        m_impl->pendingDiagnostics.begin(), m_impl->pendingDiagnostics.end(), [graphInstanceId](const auto &request) {
            return request.graphInstanceId == graphInstanceId && !request.resetPending &&
                   request.remainingSampleFrames != 0;
        });
    const auto spawnDomain = m_impl->graphState->spawnDomains.find(graphInstanceId);
    if (spawnDomain == m_impl->graphState->spawnDomains.end())
        return false;

    for (const auto &item : items) {
        const auto emitter = m_impl->emitters.find(item.emitterId);
        const auto scheduler = m_impl->graphState->schedulerById.find(item.emitterId);
        if (item.emitterId == 0 || !emitterIds.insert(item.emitterId).second || item.request.frameIndex != frameIndex ||
            !IsFinite(item.transforms) || emitter == m_impl->emitters.end() ||
            scheduler == m_impl->graphState->schedulerById.end() ||
            emitter->second->graphInstanceId != graphInstanceId || !emitter->second->queuedFrameRequests.empty() ||
            item.prerollRequests.size() > 4096u)
            return false;
        PreparedItem decoded{&item, emitter->second, scheduler->second, item.prerollRequests};
        decoded.sequence.push_back(item.request);
        decoded.sequence.back().collectCollisionDiagnostics = collectCollisionDiagnostics;
        decoded.sequence.back().resetCollisionDiagnostics = resetCollisionDiagnostics;
        for (size_t substep = 0; substep < decoded.sequence.size(); ++substep) {
            const auto &request = decoded.sequence[substep];
            if (request.frameIndex != frameIndex || request.substepIndex != substep ||
                !ParticleRenderGraph::IsFrameRequestValid(request))
                return false;
        }
        if (scheduler->second->HasResetPending()) {
            for (auto &request : decoded.sequence)
                ++request.substepIndex;
            auto resetRequest = item.request;
            resetRequest.substepIndex = 0;
            resetRequest.spawnCount = 0;
            resetRequest.deltaTime = 0.0f;
            resetRequest.simulate = false;
            resetRequest.render = false;
            resetRequest.forceSimulation = true;
            decoded.sequence.insert(decoded.sequence.begin(), resetRequest);
        }
        prepared.push_back(std::move(decoded));
    }

    for (const auto &entry : prepared)
        (void)m_impl->RefreshResources(*entry.emitter);

    for (const auto &entry : prepared) {
        if (!entry.scheduler->CanBeginFrame(entry.sequence.front()))
            return false;
    }
    for (const auto &entry : prepared) {
        std::vector<GpuSkinnedMeshFrameData> skinnedSources;
        for (const auto &mesh : entry.emitter->sourceProgram.meshInterfaces) {
            if (!mesh.skinnedRenderer.IsValid())
                continue;
            if (!m_impl->skinnedMeshResolver)
                return false;
            const auto snapshot = m_impl->skinnedMeshResolver(mesh.skinnedRenderer);
            // A scene object may be destroyed between authoring and this frame.
            // Retain the last valid pose until the graph parameter is changed;
            // never invalidate a resident bind group mid-frame.
            if (!snapshot || !snapshot->mesh || snapshot->mesh.get() != mesh.mesh.get() || !snapshot->currentPalette ||
                snapshot->currentPalette->empty())
                continue;
            skinnedSources.push_back(
                {mesh.interfaceIndex, snapshot->revision, snapshot->sourceToWorld, snapshot->currentPalette});
        }
        if (!entry.emitter->runtime->UpdateSkinnedMeshSources(skinnedSources))
            return false;
        if (!entry.emitter->runtime->UpdateTransforms(entry.item->transforms))
            return false;
    }
    std::vector<bool> previousAcceptance;
    previousAcceptance.reserve(prepared.size());
    for (const auto &entry : prepared) {
        previousAcceptance.push_back(entry.emitter->lastSimulate);
        if (!spawnDomain->second->SetEmitterAcceptingBurstRequests(entry.emitter->sourceProgram.graphEmitterIndex,
                                                                   entry.sequence.front().simulate)) {
            for (size_t rollback = 0; rollback < previousAcceptance.size() - 1; ++rollback) {
                (void)spawnDomain->second->SetEmitterAcceptingBurstRequests(
                    prepared[rollback].emitter->sourceProgram.graphEmitterIndex, previousAcceptance[rollback]);
            }
            return false;
        }
    }
    for (const auto &entry : prepared) {
        if (!entry.scheduler->BeginFrame(entry.sequence.front())) {
            for (size_t rollback = 0; rollback < prepared.size(); ++rollback) {
                (void)spawnDomain->second->SetEmitterAcceptingBurstRequests(
                    prepared[rollback].emitter->sourceProgram.graphEmitterIndex, previousAcceptance[rollback]);
            }
            return false;
        }
    }
    for (const auto &entry : prepared) {
        entry.emitter->queuedFrameRequests.assign(entry.sequence.begin() + 1, entry.sequence.end());
        entry.emitter->hasFrameRequest = true;
        entry.emitter->lastFrameIndex = entry.item->request.frameIndex;
        entry.emitter->lastSpawnCount = entry.item->request.spawnCount;
        entry.emitter->lastSimulate = entry.item->request.simulate;
        entry.emitter->lastRender = entry.item->request.render;
        entry.emitter->lastOffscreenPolicy = entry.item->request.offscreenPolicy;
        entry.emitter->lastBoundsMode = entry.item->request.boundsMode;
    }
    if (resetCollisionDiagnostics) {
        for (auto &request : m_impl->pendingDiagnostics) {
            if (request.graphInstanceId == graphInstanceId && request.resetPending)
                request.resetPending = false;
        }
    } else if (collectCollisionDiagnostics) {
        for (auto &request : m_impl->pendingDiagnostics) {
            if (request.graphInstanceId == graphInstanceId && request.remainingSampleFrames != 0)
                --request.remainingSampleFrames;
        }
    }
    return true;
}

bool ParticleGpuSystemManager::Reset(uint64_t id)
{
    if (!m_impl || !m_impl->graphState)
        return false;
    const auto scheduler = m_impl->graphState->schedulerById.find(id);
    if (scheduler == m_impl->graphState->schedulerById.end())
        return false;
    const auto emitter = m_impl->emitters.find(id);
    if (emitter == m_impl->emitters.end())
        return false;
    const auto domain = m_impl->graphState->spawnDomains.find(emitter->second->graphInstanceId);
    if (domain == m_impl->graphState->spawnDomains.end() ||
        !domain->second->SetEmitterAcceptingBurstRequests(emitter->second->sourceProgram.graphEmitterIndex, false))
        return false;
    emitter->second->lastSimulate = false;
    emitter->second->queuedFrameRequests.clear();
    scheduler->second->Reset();
    m_impl->FailDiagnostics(emitter->second->graphInstanceId,
                            "GPU particle graph was reset before diagnostic recording completed");
    return true;
}

void ParticleGpuSystemManager::Execute(VkCommandBuffer commandBuffer)
{
    if (!m_impl || !m_impl->graphState || !m_impl->graphState->graph || commandBuffer == VK_NULL_HANDLE)
        return;
    if (!m_impl->RecordCollisionUpload(commandBuffer))
        INXLOG_ERROR("GPU particle collision scene upload failed");
    bool hasPendingEmitter = std::any_of(m_impl->graphState->schedulers.begin(), m_impl->graphState->schedulers.end(),
                                         [](const auto &scheduler) { return scheduler->HasPendingFrame(); });
    while (hasPendingEmitter) {
        m_impl->graphState->graph->Execute(commandBuffer, rhi::QueueRole::Compute);
        m_impl->RetireCompletedMigrations();
        if (!m_impl->HasQueuedFrameRequests())
            break;
        if (!m_impl->ArmNextQueuedFrameRequests()) {
            INXLOG_ERROR("GPU particle preroll sequence could not arm its next fixed step");
            m_impl->ClearQueuedFrameRequests();
            break;
        }
        hasPendingEmitter = true;
    }
    m_impl->RecordDiagnostics(commandBuffer);
}

bool ParticleGpuSystemManager::CanExecuteAsync() const noexcept
{
    if (!m_impl || !m_impl->context || !m_impl->context->HasIndependentComputeQueue() || !m_impl->graphState ||
        !m_impl->graphState->graph)
        return false;
    const auto &plan = m_impl->graphState->graph->GetSubmissionPlan();
    const uint32_t boundary = m_impl->graphState->renderExportBatch;
    if (boundary == rhi::InvalidSubmissionBatchIndex || boundary == 0 || boundary >= plan.batches.size())
        return false;
    return !m_impl->HasQueuedFrameRequests() &&
           std::all_of(m_impl->emitters.begin(), m_impl->emitters.end(), [](const auto &entry) {
               return !entry.second->hasFrameRequest ||
                      entry.second->lastOffscreenPolicy == GpuParticleOffscreenPolicy::AlwaysSimulate;
           });
}

uint64_t ParticleGpuSystemManager::AsyncExecutionGeneration() const noexcept
{
    return m_impl && m_impl->graphState ? m_impl->graphState->generation : 0;
}

bool ParticleGpuSystemManager::RecordAsyncSimulation(VkCommandBuffer commandBuffer)
{
    if (!CanExecuteAsync() || commandBuffer == VK_NULL_HANDLE || m_impl->graphState->asyncRecordingActive)
        return false;
    if (!m_impl->RecordCollisionUpload(commandBuffer)) {
        INXLOG_ERROR("GPU particle collision scene upload failed during async simulation");
        return false;
    }
    auto &state = *m_impl->graphState;
    state.graph->BeginExecution();
    for (uint32_t batch = 0; batch < state.renderExportBatch; ++batch) {
        if (state.graph->GetSubmissionPlan().batches[batch].queue != rhi::QueueRole::Compute ||
            !state.graph->RecordSubmissionBatch(batch, commandBuffer)) {
            INXLOG_ERROR("GPU particle async simulation rejected non-Compute batch ", batch);
            return false;
        }
    }
    state.asyncRecordingActive = true;
    return true;
}

bool ParticleGpuSystemManager::RecordAsyncExport(VkCommandBuffer commandBuffer)
{
    if (!m_impl || !m_impl->graphState || !m_impl->graphState->graph || commandBuffer == VK_NULL_HANDLE ||
        !m_impl->graphState->asyncRecordingActive)
        return false;
    auto &state = *m_impl->graphState;
    const auto &plan = state.graph->GetSubmissionPlan();
    bool recorded = true;
    for (uint32_t batch = state.renderExportBatch; batch < plan.batches.size(); ++batch) {
        if (plan.batches[batch].queue != rhi::QueueRole::Compute ||
            !state.graph->RecordSubmissionBatch(batch, commandBuffer)) {
            INXLOG_ERROR("GPU particle async export rejected non-Compute batch ", batch);
            recorded = false;
            break;
        }
    }
    state.asyncRecordingActive = false;
    if (!recorded)
        return false;
    m_impl->RetireCompletedMigrations();
    m_impl->RecordDiagnostics(commandBuffer);
    return true;
}

bool ParticleGpuSystemManager::Contains(uint64_t id) const
{
    return m_impl && m_impl->emitters.find(id) != m_impl->emitters.end();
}

size_t ParticleGpuSystemManager::Size() const
{
    return m_impl ? m_impl->emitters.size() : 0;
}

GpuParticleTelemetrySnapshot ParticleGpuSystemManager::TelemetrySnapshot() const
{
    GpuParticleTelemetrySnapshot snapshot;
    if (!m_impl)
        return snapshot;

    snapshot.systemCount = m_impl->emitters.size();
    for (const auto &[id, emitter] : m_impl->emitters) {
        (void)id;
        snapshot.outputCount += emitter->outputs.size();
        snapshot.totalCapacity += emitter->sourceProgram.capacity;
        const auto continuation = emitter->runtime->ContinuationTelemetry();
        if (continuation.capacity > 0) {
            ++snapshot.continuationSystemCount;
            snapshot.totalContinuationCapacity += continuation.capacity;
            snapshot.maximumContinuationProgramGeneration =
                std::max(snapshot.maximumContinuationProgramGeneration, continuation.programGeneration);
            snapshot.continuationPrepareRecordCalls += continuation.prepareRecordCalls;
            snapshot.continuationClassifyRecordCalls += continuation.classifyRecordCalls;
            snapshot.continuationDispatchRecordCalls += continuation.dispatchRecordCalls;
            snapshot.continuationResetPendingCount += continuation.resetPending ? 1u : 0u;
        }
        const auto contacts = emitter->runtime->ContactTelemetry();
        if (contacts.particleCapacity > 0) {
            ++snapshot.contactRuntimeSystemCount;
            snapshot.totalContactRecordCapacity += contacts.contactRecordCapacity;
            snapshot.totalContactWorkItemCapacity += contacts.workItemCapacity;
            snapshot.totalContactResidentBytes += contacts.contactBytes + contacts.hashBytes +
                                                  contacts.particleIndexBytes + contacts.particleStateBytes +
                                                  contacts.workItemBytes + contacts.continuationSnapshotBytes +
                                                  sizeof(GpuParticleContactCounters) + sizeof(std::array<uint32_t, 4>);
            snapshot.contactPrepareRecordCalls += emitter->runtime->ContactPrepareRecordCalls();
            snapshot.contactSolveRecordCalls += emitter->runtime->ContactSolveRecordCalls();
        }
        if (emitter->hasFrameRequest)
            snapshot.lastScheduledFrame = std::max(snapshot.lastScheduledFrame, emitter->lastFrameIndex);
    }
    for (const auto &[id, emitter] : m_impl->emitters) {
        (void)id;
        if (!emitter->hasFrameRequest || emitter->lastFrameIndex != snapshot.lastScheduledFrame)
            continue;
        ++snapshot.scheduledSystemCount;
        snapshot.simulatingSystemCount += emitter->lastSimulate ? 1u : 0u;
        snapshot.renderingSystemCount += emitter->lastRender ? 1u : 0u;
        snapshot.requestedSpawnCount += emitter->lastSpawnCount;
    }
    if (m_impl->collisionScene) {
        snapshot.collisionSceneRevision = m_impl->collisionScene->PublishedRevision();
        snapshot.collisionSceneColliderCount = m_impl->collisionScene->PublishedColliderCount();
        snapshot.collisionSceneTopologyRevision = m_impl->collisionScene->PublishedTopologyRevision();
        snapshot.collisionSceneMeshVertexCount = m_impl->collisionScene->PublishedMeshVertexCount();
        snapshot.collisionSceneMeshIndexCount = m_impl->collisionScene->PublishedMeshIndexCount();
        snapshot.collisionSceneMeshBvhNodeCount = m_impl->collisionScene->PublishedMeshBvhNodeCount();
    }
    return snapshot;
}

uint64_t ParticleGpuSystemManager::ActiveArtifactRevision(uint64_t id) const
{
    if (!m_impl)
        return 0;
    const auto found = m_impl->emitters.find(id);
    return found != m_impl->emitters.end() ? found->second->artifactRevision : 0;
}

bool ParticleGpuSystemManager::ActiveStateWasPreserved(uint64_t id) const
{
    if (!m_impl)
        return false;
    const auto found = m_impl->emitters.find(id);
    return found != m_impl->emitters.end() && found->second->statePreservedOnPublish;
}

size_t ParticleGpuSystemManager::ActiveOutputCount(uint64_t id) const
{
    if (!m_impl)
        return 0;
    const auto found = m_impl->emitters.find(id);
    return found != m_impl->emitters.end() ? found->second->outputs.size() : 0;
}

uint64_t ParticleGpuSystemManager::ActiveVectorFieldGeneration(uint64_t id, uint32_t interfaceIndex) const
{
    if (!m_impl)
        return 0;
    const auto found = m_impl->emitters.find(id);
    if (found == m_impl->emitters.end() || interfaceIndex >= found->second->vectorFieldGenerations.size())
        return 0;
    return found->second->vectorFieldGenerations[interfaceIndex];
}

int32_t ParticleGpuSystemManager::ActiveOutputRenderQueue(uint64_t emitterId, uint64_t outputId) const
{
    if (!m_impl || outputId == 0)
        return -1;
    const auto emitter = m_impl->emitters.find(emitterId);
    if (emitter == m_impl->emitters.end())
        return -1;
    const auto output = std::find_if(emitter->second->outputs.begin(), emitter->second->outputs.end(),
                                     [outputId](const auto &candidate) { return candidate.id == outputId; });
    return output != emitter->second->outputs.end() ? output->renderer->RenderQueue() : -1;
}

std::optional<ParticleOutputSemantics> ParticleGpuSystemManager::ActiveOutputSemantics(uint64_t emitterId,
                                                                                       uint64_t outputId) const
{
    if (!m_impl || outputId == 0)
        return std::nullopt;
    const auto emitter = m_impl->emitters.find(emitterId);
    if (emitter == m_impl->emitters.end())
        return std::nullopt;
    const auto output = std::find_if(emitter->second->outputs.begin(), emitter->second->outputs.end(),
                                     [outputId](const auto &candidate) { return candidate.id == outputId; });
    return output != emitter->second->outputs.end() ? std::optional{output->semantics} : std::nullopt;
}

uint64_t ParticleGpuSystemManager::RequestDiagnostics(uint64_t graphInstanceId, uint32_t sampleFrames,
                                                      uint32_t stateSampleCount)
{
    if (!m_impl)
        return 0;
    const uint64_t requestId = m_impl->nextDiagnosticRequestId++;
    GpuParticleDiagnosticSnapshot snapshot;
    snapshot.requestId = requestId;
    snapshot.graphInstanceId = graphInstanceId;
    const bool hasEmitter =
        graphInstanceId != 0 &&
        std::any_of(m_impl->emitters.begin(), m_impl->emitters.end(), [graphInstanceId](const auto &entry) {
            return entry.second && entry.second->graphInstanceId == graphInstanceId;
        });
    if (sampleFrames == 0 || sampleFrames > 4096u) {
        snapshot.status = GpuParticleDiagnosticStatus::Failed;
        snapshot.error = "GPU particle diagnostic sample frames must be between 1 and 4096";
    } else if (stateSampleCount > 64u) {
        snapshot.status = GpuParticleDiagnosticStatus::Failed;
        snapshot.error = "GPU particle diagnostic state sample count must be between 0 and 64";
    } else if (m_impl->pendingDiagnostics.size() >= 8) {
        snapshot.status = GpuParticleDiagnosticStatus::Failed;
        snapshot.error = "Too many GPU particle diagnostic requests are pending";
    } else if (std::any_of(
                   m_impl->pendingDiagnostics.begin(), m_impl->pendingDiagnostics.end(),
                   [graphInstanceId](const auto &request) { return request.graphInstanceId == graphInstanceId; })) {
        snapshot.status = GpuParticleDiagnosticStatus::Failed;
        snapshot.error = "A GPU particle diagnostic request is already pending for this graph";
    } else if (!hasEmitter) {
        snapshot.status = GpuParticleDiagnosticStatus::Inactive;
        snapshot.error = "GPU particle graph is not resident";
    } else if (!m_impl->context || !m_impl->deletionQueue) {
        snapshot.status = GpuParticleDiagnosticStatus::Failed;
        snapshot.error = "GPU particle diagnostics are unavailable";
    } else {
        snapshot.status = GpuParticleDiagnosticStatus::Pending;
        m_impl->pendingDiagnostics.push_back({requestId, graphInstanceId, sampleFrames, stateSampleCount, true});
    }
    std::scoped_lock lock(m_impl->diagnosticState->mutex);
    if (m_impl->diagnosticState->snapshots.size() >= 128) {
        auto oldest = m_impl->diagnosticState->snapshots.end();
        for (auto it = m_impl->diagnosticState->snapshots.begin(); it != m_impl->diagnosticState->snapshots.end();
             ++it) {
            if (it->second.status == GpuParticleDiagnosticStatus::Pending)
                continue;
            if (oldest == m_impl->diagnosticState->snapshots.end() || it->first < oldest->first)
                oldest = it;
        }
        if (oldest != m_impl->diagnosticState->snapshots.end())
            m_impl->diagnosticState->snapshots.erase(oldest);
    }
    m_impl->diagnosticState->snapshots[requestId] = std::move(snapshot);
    return requestId;
}

GpuParticleDiagnosticSnapshot ParticleGpuSystemManager::QueryDiagnostics(uint64_t requestId) const
{
    if (!m_impl || requestId == 0)
        return {};
    std::scoped_lock lock(m_impl->diagnosticState->mutex);
    const auto found = m_impl->diagnosticState->snapshots.find(requestId);
    return found != m_impl->diagnosticState->snapshots.end() ? found->second : GpuParticleDiagnosticSnapshot{};
}

} // namespace infernux::particle
