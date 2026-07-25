#include "ParticleGpuSystemManager.h"

#include "ParticleGpuDrawRegistry.h"
#include "ParticleGpuMeshRenderer.h"

#include <core/log/InxLog.h>
#include <function/renderer/FrameDeletionQueue.h>
#include <function/renderer/rhi/RhiBuffer.h>
#include <function/renderer/vk/RenderGraph.h>
#include <function/renderer/vk/VkDeviceContext.h>
#include <function/renderer/vk/VkPipelineManager.h>
#include <function/renderer/vk/VkResourceManager.h>
#include <function/renderer/vk/VulkanRhiDevice.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <limits>
#include <map>
#include <memory>
#include <mutex>
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

struct alignas(16) PackedParticleMeshTriangle
{
    uint32_t first = 0;
    uint32_t second = 0;
    uint32_t third = 0;
    float cumulativeArea = 0.0f;
};

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
    };

    struct PointCacheResources
    {
        std::shared_ptr<rhi::BufferResource> data;
        std::shared_ptr<rhi::BufferResource> lookup;
    };

    struct PointCacheUpload
    {
        std::weak_ptr<InxPointCache> owner;
        uint64_t generation = 0;
        std::shared_ptr<const PointCacheCpuData> cpuData;
        std::shared_ptr<vk::BufferUploadTicket> dataTicket;
        std::shared_ptr<vk::BufferUploadTicket> lookupTicket;
        std::shared_ptr<PointCacheResources> resources;
        bool failed = false;
    };

    struct MeshResources
    {
        std::shared_ptr<rhi::BufferResource> vertices;
        std::shared_ptr<rhi::BufferResource> indices;
        std::shared_ptr<rhi::BufferResource> samplingTriangles;
        uint32_t vertexCount = 0;
        uint32_t indexCount = 0;
        uint32_t samplingTriangleCount = 0;
    };

    struct MeshUpload
    {
        std::weak_ptr<InxMesh> owner;
        uint64_t contentHash = 0;
        std::shared_ptr<vk::BufferUploadTicket> vertexTicket;
        std::shared_ptr<vk::BufferUploadTicket> indexTicket;
        std::shared_ptr<vk::BufferUploadTicket> samplingTriangleTicket;
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
        std::vector<std::shared_ptr<InxPointCache>> pointCaches;
        std::vector<uint64_t> pointCacheGenerations;
        std::vector<uint64_t> observedPointCacheGenerations;
        std::vector<std::shared_ptr<InxTexture>> vectorFields;
        std::vector<uint64_t> vectorFieldGenerations;
        std::vector<uint64_t> observedVectorFieldGenerations;
        std::vector<uint32_t> billboardVertexShader;
        std::vector<uint32_t> billboardFragmentShader;
        std::vector<uint32_t> billboardForwardPlusFragmentShader;
        std::vector<uint32_t> billboardPickingFragmentShader;
        std::vector<uint32_t> meshVertexShader;
        std::vector<uint32_t> meshFragmentShader;
        std::vector<uint32_t> meshForwardPlusFragmentShader;
        std::vector<uint32_t> meshPickingFragmentShader;
        std::vector<Output> outputs;
        bool hasFrameRequest = false;
        uint64_t lastFrameIndex = 0;
        uint32_t lastSpawnCount = 0;
        bool lastSimulate = false;
        bool lastRender = false;
    };

    struct EventFrameState
    {
        std::shared_ptr<ParticleGpuEventDomain> domain;
        bool pending = false;
        bool active = false;
        bool simulate = false;
        std::vector<GpuParticleFrameRequest> emitterRequests;
    };

    struct GraphState
    {
        std::unique_ptr<vk::RenderGraph> graph;
        std::vector<std::unique_ptr<ParticleRenderGraph>> schedulers;
        std::unordered_map<uint64_t, ParticleRenderGraph *> schedulerById;
        std::unordered_map<uint64_t, std::shared_ptr<EventFrameState>> eventFrames;
    };

    using EmitterMap = std::map<uint64_t, std::shared_ptr<Emitter>>;
    using EventDomainMap = std::unordered_map<uint64_t, std::shared_ptr<ParticleGpuEventDomain>>;

    vk::VkDeviceContext *context = nullptr;
    vk::VkPipelineManager *pipelines = nullptr;
    vk::VkResourceManager *resources = nullptr;
    FrameDeletionQueue *deletionQueue = nullptr;
    ParticleGpuDrawRegistry *drawRegistry = nullptr;
    GpuBillboardTextureResolver textureResolver;
    GpuBillboardTextureVersionResolver textureVersionResolver;
    GpuParticleVectorFieldTextureResolver vectorFieldTextureResolver;
    std::shared_ptr<const GpuParticleCullProgramStorage> cullProgram;
    std::shared_ptr<const GpuParticleSortProgramStorage> sortProgram;
    std::shared_ptr<const GpuParticleBoundsProgramStorage> boundsProgram;
    std::shared_ptr<const GpuParticleMigrationProgramStorage> migrationProgram;
    std::shared_ptr<const GpuParticleEventProgramStorage> eventProgram;
    std::shared_ptr<const GpuParticleRibbonProgramStorage> ribbonTopologyProgram;
    std::shared_ptr<const GpuParticleRibbonRenderProgramStorage> ribbonRenderProgram;
    EmitterMap emitters;
    EventDomainMap eventDomains;
    std::shared_ptr<GraphState> graphState;
    mutable std::unordered_map<const InxPointCache *, PointCacheUpload> pointCacheUploads;
    mutable std::unordered_map<const InxMesh *, MeshUpload> meshUploads;
    std::shared_ptr<DiagnosticState> diagnosticState = std::make_shared<DiagnosticState>();
    std::vector<PendingDiagnostic> pendingDiagnostics;
    uint64_t nextDiagnosticRequestId = 1;

    void RecordDiagnostics(VkCommandBuffer commandBuffer)
    {
        if (pendingDiagnostics.empty() || !context || !deletionQueue || commandBuffer == VK_NULL_HANDLE)
            return;

        auto requests = std::move(pendingDiagnostics);
        pendingDiagnostics.clear();
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
                uint64_t offset = 0;
            };
            struct EventCapture
            {
                GpuParticleEventDiagnostic diagnostic;
                uint64_t readOffset = 0;
                uint64_t writeOffset = 0;
                bool hasInput = false;
            };

            std::vector<EmitterCapture> emitterCaptures;
            for (const auto &[id, emitter] : emitters) {
                if (!emitter || emitter->graphInstanceId != request.graphInstanceId || !emitter->runtime)
                    continue;
                EmitterCapture capture;
                capture.diagnostic.emitterId = id;
                capture.diagnostic.emitterIndex = emitter->sourceProgram.graphEmitterIndex;
                capture.diagnostic.capacity = emitter->runtime->Capacity();
                emitterCaptures.push_back(capture);
            }
            std::sort(emitterCaptures.begin(), emitterCaptures.end(), [](const auto &lhs, const auto &rhs) {
                return lhs.diagnostic.emitterIndex < rhs.diagnostic.emitterIndex;
            });

            const auto domainFound = eventDomains.find(request.graphInstanceId);
            const std::shared_ptr<ParticleGpuEventDomain> domain =
                domainFound != eventDomains.end() ? domainFound->second : nullptr;
            std::vector<EventCapture> eventCaptures;
            if (domain && domain->IsValid()) {
                eventCaptures.reserve(domain->ChannelCount());
                for (uint32_t channelIndex = 0; channelIndex < domain->ChannelCount(); ++channelIndex) {
                    const auto *channel = domain->Channel(channelIndex);
                    if (!channel)
                        continue;
                    EventCapture capture;
                    capture.diagnostic.channelIndex = channelIndex;
                    capture.diagnostic.stableEventTypeHash = channel->stableEventTypeHash;
                    capture.diagnostic.sourceEmitterIndex = channel->sourceEmitterIndex;
                    capture.diagnostic.targetEmitterIndex = channel->targetEmitterIndex;
                    capture.diagnostic.eventTypeIndex = channel->eventTypeIndex;
                    capture.diagnostic.spawnCount = channel->spawnCount;
                    capture.diagnostic.preparedEpoch = domain->PreparedEpoch();
                    capture.diagnostic.readPageIndex = domain->CurrentReadPageIndex();
                    capture.diagnostic.writePageIndex = domain->CurrentWritePageIndex();
                    capture.hasInput = domain->HasPreparedInput();
                    eventCaptures.push_back(capture);
                }
            }

            const uint64_t emitterBytes = emitterCaptures.size() * 16u;
            const uint64_t eventBytes = eventCaptures.size() * sizeof(GpuParticleEventCounter) * 2u;
            const uint64_t totalBytes = emitterBytes + eventBytes;
            if (emitterCaptures.empty() || totalBytes == 0) {
                std::scoped_lock lock(diagnosticState->mutex);
                auto &snapshot = diagnosticState->snapshots[request.requestId];
                snapshot.status = GpuParticleDiagnosticStatus::Failed;
                snapshot.error = "GPU particle graph has no resident emitters";
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
            for (auto &capture : emitterCaptures) {
                capture.offset = offset;
                const auto emitter = emitters.find(capture.diagnostic.emitterId);
                transfer.CopyBuffer(emitter->second->runtime->CounterBuffer(), readbackHandle, {0, offset, 16});
                offset += 16;
            }
            if (domain && !eventCaptures.empty()) {
                const auto *readPage = domain->Page(domain->CurrentReadPageIndex());
                const auto *writePage = domain->Page(domain->CurrentWritePageIndex());
                for (auto &capture : eventCaptures) {
                    const uint64_t sourceOffset =
                        static_cast<uint64_t>(capture.diagnostic.channelIndex) * sizeof(GpuParticleEventCounter);
                    capture.readOffset = offset;
                    transfer.CopyBuffer(readPage->counters, readbackHandle,
                                        {sourceOffset, offset, sizeof(GpuParticleEventCounter)});
                    offset += sizeof(GpuParticleEventCounter);
                    capture.writeOffset = offset;
                    transfer.CopyBuffer(writePage->counters, readbackHandle,
                                        {sourceOffset, offset, sizeof(GpuParticleEventCounter)});
                    offset += sizeof(GpuParticleEventCounter);
                }
            }

            const auto state = diagnosticState;
            auto *readbackDevice = static_cast<rhi::Device *>(&device);
            deletionQueue->Push([state, readback, emitterCaptures = std::move(emitterCaptures),
                                 eventCaptures = std::move(eventCaptures), request, readbackDevice]() mutable {
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
                        std::array<uint32_t, 4> counters{};
                        std::memcpy(counters.data(), bytes.data() + capture.offset, sizeof(counters));
                        capture.diagnostic.freeCount = std::min(counters[0], capture.diagnostic.capacity);
                        capture.diagnostic.aliveCount = capture.diagnostic.capacity - capture.diagnostic.freeCount;
                        capture.diagnostic.visibleCount = counters[1];
                        capture.diagnostic.droppedCount = counters[2];
                        result.emitters.push_back(capture.diagnostic);
                    }
                    for (auto &capture : eventCaptures) {
                        GpuParticleEventCounter read{};
                        GpuParticleEventCounter write{};
                        std::memcpy(&read, bytes.data() + capture.readOffset, sizeof(read));
                        std::memcpy(&write, bytes.data() + capture.writeOffset, sizeof(write));
                        capture.diagnostic.producedCount = write.writeCount;
                        capture.diagnostic.producerDroppedCount = write.droppedCount;
                        capture.diagnostic.consumedCount = capture.hasInput ? read.consumeCount : 0u;
                        capture.diagnostic.targetDroppedCount = capture.hasInput ? read.reserved : 0u;
                        const uint64_t requested =
                            static_cast<uint64_t>(capture.diagnostic.consumedCount) * capture.diagnostic.spawnCount;
                        capture.diagnostic.spawnedCount =
                            requested - std::min<uint64_t>(requested, capture.diagnostic.targetDroppedCount);
                        result.events.push_back(capture.diagnostic);
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
        std::vector<PackedParticleMeshTriangle> samplingTriangles;
        samplingTriangles.reserve(sourceIndices.size() / 3);
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
            samplingTriangles.push_back({first, second, third, static_cast<float>(cumulativeArea)});
        }
        if (samplingTriangles.empty() || !std::isfinite(cumulativeArea) || cumulativeArea <= 0.0) {
            SetError(error, "GPU particle Mesh sampling requires non-degenerate triangles");
            return {};
        }
        const float inverseArea = static_cast<float>(1.0 / cumulativeArea);
        for (auto &triangle : samplingTriangles)
            triangle.cumulativeArea *= inverseArea;
        samplingTriangles.back().cumulativeArea = 1.0f;
        uint64_t contentHash = 1469598103934665603ull;
        contentHash = HashBytes(contentHash, vertices.data(), vertices.size() * sizeof(PackedParticleMeshVertex));
        contentHash = HashBytes(contentHash, sourceIndices.data(), sourceIndices.size() * sizeof(uint32_t));
        contentHash = HashBytes(contentHash, samplingTriangles.data(),
                                samplingTriangles.size() * sizeof(PackedParticleMeshTriangle));

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
                    {samplingTriangles.data(), samplingTriangles.size() * sizeof(PackedParticleMeshTriangle),
                     rhi::BufferUsage::Storage});
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
            if (!verticesReady || !indicesReady || !samplingReady) {
                SetError(error, "GPU particle Mesh upload is pending");
                return {};
            }
            auto result = std::make_shared<MeshResources>();
            result->vertices = resources->GetPublishedRhiBuffer(upload.vertexTicket);
            result->indices = resources->GetPublishedRhiBuffer(upload.indexTicket);
            result->samplingTriangles = resources->GetPublishedRhiBuffer(upload.samplingTriangleTicket);
            result->vertexCount = static_cast<uint32_t>(sourceVertices.size());
            result->indexCount = static_cast<uint32_t>(sourceIndices.size());
            result->samplingTriangleCount = static_cast<uint32_t>(samplingTriangles.size());
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

    [[nodiscard]] std::shared_ptr<PointCacheResources>
    ResolvePointCacheResources(const std::shared_ptr<InxPointCache> &cache, std::string *error) const
    {
        if (!resources || !cache || !cache->GetCpuData()) {
            SetError(error, "GPU particle Point Cache resource service is unavailable");
            return {};
        }

        for (auto it = pointCacheUploads.begin(); it != pointCacheUploads.end();) {
            if (it->second.owner.expired())
                it = pointCacheUploads.erase(it);
            else
                ++it;
        }

        const auto cpuData = cache->GetCpuData();
        const uint64_t generation = cache->GetGeneration();
        auto &upload = pointCacheUploads[cache.get()];
        const auto owner = upload.owner.lock();
        if (owner.get() != cache.get() || upload.generation != generation || upload.cpuData != cpuData) {
            upload = {};
            upload.owner = cache;
            upload.generation = generation;
            upload.cpuData = cpuData;
            try {
                upload.dataTicket = resources->BeginBufferUpload(
                    {cpuData->bytes.data(), cpuData->bytes.size(), rhi::BufferUsage::Storage});
                const PointCacheIdLookupEntry identityLookup = {0, UINT32_MAX};
                const bool hashedLookup = cpuData->idLookupMode == PointCacheIdLookupMode::Hash;
                upload.lookupTicket = resources->BeginBufferUpload(
                    {hashedLookup ? static_cast<const void *>(cpuData->idLookup.data())
                                  : static_cast<const void *>(&identityLookup),
                     hashedLookup ? cpuData->idLookup.size() * sizeof(PointCacheIdLookupEntry) : sizeof(identityLookup),
                     rhi::BufferUsage::Storage});
            } catch (const std::exception &exception) {
                upload.failed = true;
                SetError(error, std::string("GPU particle Point Cache upload failed: ") + exception.what());
                return {};
            }
        }
        if (upload.failed) {
            SetError(error, "GPU particle Point Cache upload failed");
            return {};
        }
        if (upload.resources)
            return upload.resources;

        try {
            const bool dataReady = resources->TryPublishBufferUpload(upload.dataTicket);
            const bool lookupReady = resources->TryPublishBufferUpload(upload.lookupTicket);
            if (!dataReady || !lookupReady) {
                SetError(error, "GPU particle Point Cache upload is pending");
                return {};
            }
            auto result = std::make_shared<PointCacheResources>();
            result->data = resources->GetPublishedRhiBuffer(upload.dataTicket);
            result->lookup = resources->GetPublishedRhiBuffer(upload.lookupTicket);
            if (!result->data || !result->data->IsValid() || !result->lookup || !result->lookup->IsValid()) {
                upload.failed = true;
                SetError(error, "GPU particle Point Cache upload produced invalid RHI buffers");
                return {};
            }
            upload.resources = result;
            return result;
        } catch (const std::exception &exception) {
            upload.failed = true;
            SetError(error, std::string("GPU particle Point Cache publication failed: ") + exception.what());
            return {};
        }
    }

    [[nodiscard]] bool BuildRuntimeDesc(const GpuParticleEmitterProgram &program, GpuEmitterDesc &runtimeDesc,
                                        std::vector<std::shared_ptr<InxPointCache>> &pointCaches,
                                        std::vector<uint64_t> &pointCacheGenerations,
                                        std::vector<std::shared_ptr<InxTexture>> &vectorFields,
                                        std::vector<uint64_t> &vectorFieldGenerations, std::string *error) const
    {
        runtimeDesc.capacity = program.capacity;
        runtimeDesc.stateStride = program.stateStride;
        runtimeDesc.eventOutputStageMask = program.eventOutputStageMask;
        for (size_t index = 0; index < program.kernels.size(); ++index)
            runtimeDesc.kernels[index] = {program.kernels[index].data(), program.kernels[index].size()};
        runtimeDesc.eventInitKernel = {program.eventInitKernel.data(), program.eventInitKernel.size()};
        runtimeDesc.pointCaches.metadataBinding = program.pointCaches.metadataBinding;
        runtimeDesc.pointCaches.interfaceStrideWords = program.pointCaches.interfaceStrideWords;
        runtimeDesc.pointCaches.sampleStrideWords = program.pointCaches.sampleStrideWords;
        runtimeDesc.pointCaches.sampleCount = program.pointCaches.sampleCount;
        runtimeDesc.pointCaches.pointCaches.reserve(program.pointCaches.pointCaches.size());
        pointCaches.reserve(program.pointCaches.pointCaches.size());
        pointCacheGenerations.reserve(program.pointCaches.pointCaches.size());
        std::unordered_set<std::string> stableIds;
        for (const auto &pointCache : program.pointCaches.pointCaches) {
            if (pointCache.stableId.empty() || !stableIds.insert(pointCache.stableId).second || !pointCache.cache ||
                !pointCache.cache->GetCpuData()) {
                SetError(error, "GPU particle Point Cache bindings must have unique identities and loaded data");
                return false;
            }
            GpuPointCacheDesc runtimePointCache;
            runtimePointCache.interfaceIndex = pointCache.interfaceIndex;
            runtimePointCache.dataBinding = pointCache.dataBinding;
            runtimePointCache.lookupBinding = pointCache.lookupBinding;
            runtimePointCache.worldSpace = pointCache.worldSpace;
            runtimePointCache.cacheToSpace = pointCache.cacheToSpace;
            runtimePointCache.data = pointCache.cache->GetCpuData();
            const auto gpuResources = ResolvePointCacheResources(pointCache.cache, error);
            if (!gpuResources)
                return false;
            runtimePointCache.dataBuffer = gpuResources->data->GetBuffer();
            runtimePointCache.lookupBuffer = gpuResources->lookup->GetBuffer();
            runtimePointCache.keepAlive = gpuResources;
            runtimePointCache.samples = pointCache.samples;
            runtimeDesc.pointCaches.pointCaches.push_back(std::move(runtimePointCache));
            pointCaches.push_back(pointCache.cache);
            pointCacheGenerations.push_back(pointCache.cache->GetGeneration());
        }
        if (program.meshShape) {
            if (!program.meshShape->mesh) {
                SetError(error, "GPU particle Mesh emitter shape requires a loaded Mesh asset");
                return false;
            }
            const auto meshResources = ResolveMeshResources(program.meshShape->mesh, error);
            if (!meshResources)
                return false;
            GpuMeshShapeDesc runtimeMeshShape;
            runtimeMeshShape.metadataOffsetWords = program.meshShape->metadataOffsetWords;
            runtimeMeshShape.vertexBinding = program.meshShape->vertexBinding;
            runtimeMeshShape.triangleBinding = program.meshShape->triangleBinding;
            runtimeMeshShape.vertexCount = meshResources->vertexCount;
            runtimeMeshShape.triangleCount = meshResources->samplingTriangleCount;
            runtimeMeshShape.vertices = meshResources->vertices->GetBuffer();
            runtimeMeshShape.triangles = meshResources->samplingTriangles->GetBuffer();
            runtimeMeshShape.keepAlive = meshResources;
            runtimeDesc.meshShape = std::move(runtimeMeshShape);
        }

        runtimeDesc.vectorFields.metadataBinding = program.vectorFields.metadataBinding;
        runtimeDesc.vectorFields.interfaceStrideWords = program.vectorFields.interfaceStrideWords;
        runtimeDesc.vectorFields.vectorFields.reserve(program.vectorFields.vectorFields.size());
        vectorFields.reserve(program.vectorFields.vectorFields.size());
        vectorFieldGenerations.reserve(program.vectorFields.vectorFields.size());
        stableIds.clear();
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
                !lease.sampler.IsValid() || !lease.keepAlive) {
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
            runtimeField.keepAlive = std::move(lease.keepAlive);
            runtimeDesc.vectorFields.vectorFields.push_back(std::move(runtimeField));
            vectorFields.push_back(field.texture);
            vectorFieldGenerations.push_back(field.texture->GetGeneration());
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
            desc.material = output.material;
            desc.fallbackMaterial = output.fallbackMaterial;
            desc.semantics = output.semantics;
            desc.uvMode = output.ribbonUvMode;
            desc.uvScale = output.ribbonUvScale;
            return renderer->Create(context->GetRhiDevice(), desc) ? renderer : nullptr;
        }
        if (output.type == GpuParticleOutputType::Mesh) {
            const auto meshResources = ResolveMeshResources(output.mesh, error);
            if (!meshResources)
                return {};
            auto renderer = std::make_shared<ParticleGpuMeshRenderer>();
            GpuMeshRendererDesc desc;
            desc.vertexShader = {emitter.meshVertexShader.data(), emitter.meshVertexShader.size()};
            desc.fragmentShader = {emitter.meshFragmentShader.data(), emitter.meshFragmentShader.size()};
            desc.forwardPlusFragmentShader = {emitter.meshForwardPlusFragmentShader.data(),
                                              emitter.meshForwardPlusFragmentShader.size()};
            desc.pickingFragmentShader = {emitter.meshPickingFragmentShader.data(),
                                          emitter.meshPickingFragmentShader.size()};
            desc.instances = emitter.runtime->InstanceBuffer();
            desc.renderIndices = emitter.runtime->RenderIndexBuffer();
            desc.mesh = output.mesh;
            desc.meshVertices = meshResources->vertices->GetBuffer();
            desc.meshIndices = meshResources->indices->GetBuffer();
            desc.indexCount = meshResources->indexCount;
            desc.meshBufferKeepAlive = meshResources;
            desc.material = output.material;
            desc.fallbackMaterial = output.fallbackMaterial;
            desc.semantics = output.semantics;
            return renderer->Create(context->GetRhiDevice(), desc) ? renderer : nullptr;
        }
        auto renderer = std::make_shared<ParticleGpuBillboardRenderer>();
        GpuBillboardRendererDesc rendererDesc;
        rendererDesc.vertexShader = {emitter.billboardVertexShader.data(), emitter.billboardVertexShader.size()};
        rendererDesc.fragmentShader = {emitter.billboardFragmentShader.data(), emitter.billboardFragmentShader.size()};
        rendererDesc.forwardPlusFragmentShader = {emitter.billboardForwardPlusFragmentShader.data(),
                                                  emitter.billboardForwardPlusFragmentShader.size()};
        rendererDesc.pickingFragmentShader = {emitter.billboardPickingFragmentShader.data(),
                                              emitter.billboardPickingFragmentShader.size()};
        rendererDesc.shaderProgram = output.shaderProgram;
        rendererDesc.instances = emitter.runtime->InstanceBuffer();
        rendererDesc.renderIndices = emitter.runtime->RenderIndexBuffer();
        rendererDesc.material = output.material;
        rendererDesc.fallbackMaterial = output.fallbackMaterial;
        rendererDesc.semantics = output.semantics;
        rendererDesc.flipbookColumns = output.flipbookColumns;
        rendererDesc.flipbookRows = output.flipbookRows;
        rendererDesc.textureResolver = textureResolver;
        rendererDesc.textureVersionResolver = textureVersionResolver;
        rendererDesc.deletionQueue = deletionQueue;
        return renderer->Create(context->GetRhiDevice(), rendererDesc) ? renderer : nullptr;
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
        const bool needsLegacyBillboard =
            std::any_of(program.outputs.begin(), program.outputs.end(), [](const GpuParticleOutputProgram &output) {
                return output.type == GpuParticleOutputType::Sprite && !output.shaderProgram;
            });
        const bool needsLitLegacyBillboard =
            std::any_of(program.outputs.begin(), program.outputs.end(), [](const GpuParticleOutputProgram &output) {
                return output.type == GpuParticleOutputType::Sprite && !output.shaderProgram &&
                       output.semantics.receiveSceneLighting;
            });
        if (needsLegacyBillboard &&
            (!IsSpirv(program.billboardVertexShader) || !IsSpirv(program.billboardFragmentShader) ||
             (needsLitLegacyBillboard && !IsSpirv(program.billboardForwardPlusFragmentShader)))) {
            SetError(error, "GPU particle program contains invalid billboard SPIR-V");
            return {};
        }
        const bool needsMesh = std::any_of(program.outputs.begin(), program.outputs.end(), [](const auto &output) {
            return output.type == GpuParticleOutputType::Mesh;
        });
        const bool needsRibbon = std::any_of(program.outputs.begin(), program.outputs.end(), [](const auto &output) {
            return output.type == GpuParticleOutputType::Ribbon;
        });
        const bool needsLitMesh =
            std::any_of(program.outputs.begin(), program.outputs.end(), [](const GpuParticleOutputProgram &output) {
                return output.type == GpuParticleOutputType::Mesh && output.semantics.receiveSceneLighting;
            });
        if (needsMesh && (!IsSpirv(program.meshVertexShader) || !IsSpirv(program.meshFragmentShader) ||
                          (needsLitMesh && !IsSpirv(program.meshForwardPlusFragmentShader)) ||
                          !IsSpirv(program.meshPickingFragmentShader))) {
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

        auto emitter = std::make_shared<Emitter>();
        emitter->id = program.id;
        emitter->graphInstanceId = program.graphInstanceId;
        emitter->artifactRevision = program.artifactRevision;
        emitter->stableId = program.stableId;
        emitter->statePreservedOnPublish = program.preserveState;
        emitter->billboardVertexShader = program.billboardVertexShader;
        emitter->billboardFragmentShader = program.billboardFragmentShader;
        emitter->billboardForwardPlusFragmentShader = program.billboardForwardPlusFragmentShader;
        emitter->billboardPickingFragmentShader = program.billboardPickingFragmentShader;
        emitter->meshVertexShader = program.meshVertexShader;
        emitter->meshFragmentShader = program.meshFragmentShader;
        emitter->meshForwardPlusFragmentShader = program.meshForwardPlusFragmentShader;
        emitter->meshPickingFragmentShader = program.meshPickingFragmentShader;
        emitter->runtime = std::make_unique<ParticleGpuRuntime>();

        GpuEmitterDesc runtimeDesc;
        if (!BuildRuntimeDesc(program, runtimeDesc, emitter->pointCaches, emitter->pointCacheGenerations,
                              emitter->vectorFields, emitter->vectorFieldGenerations, error))
            return {};
        emitter->observedPointCacheGenerations = emitter->pointCacheGenerations;
        emitter->observedVectorFieldGenerations = emitter->vectorFieldGenerations;
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
        boundsDesc.sourceIndirectArguments = emitter->runtime->IndirectBuffer();
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
            ribbonDesc.program = ribbonTopologyProgram->View();
            emitter->ribbonTopology = std::make_shared<ParticleGpuRibbonTopology>();
            if (!emitter->ribbonTopology->Create(device, ribbonDesc)) {
                SetError(error, "failed to create GPU-resident Ribbon topology");
                return {};
            }
        }

        std::unordered_set<uint64_t> outputIds;
        std::unordered_set<std::string> outputStableIds;
        outputIds.reserve(program.outputs.size());
        outputStableIds.reserve(program.outputs.size());
        emitter->outputs.reserve(program.outputs.size());
        for (const auto &output : program.outputs) {
            if (output.semantics.castShadows && output.type != GpuParticleOutputType::Mesh) {
                SetError(error, "only static mesh particle outputs can cast shadows");
                return {};
            }
            if (output.id == 0 || output.stableId.empty() || !outputIds.insert(output.id).second ||
                !outputStableIds.insert(output.stableId).second) {
                SetError(error, "GPU particle output identity must be valid and unique per emitter");
                return {};
            }
            if (!output.semantics.IsValid()) {
                SetError(error, "GPU particle output '" + output.stableId + "' has invalid rendering semantics");
                return {};
            }
            if (output.type == GpuParticleOutputType::Sprite && output.shaderProgram &&
                output.semantics.receiveSceneLighting &&
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
                (output.semantics.castShadows || output.semantics.softParticles ||
                 output.semantics.receiveSceneLighting || output.semantics.receiveShadows ||
                 !std::isfinite(output.ribbonUvScale) || output.ribbonUvScale <= 0.0f)) {
                SetError(error, "GPU particle Ribbon output '" + output.stableId +
                                    "' has unsupported lighting, shadow, soft-particle, or UV semantics");
                return {};
            }
            if (output.type != GpuParticleOutputType::Ribbon && output.semantics.sortMode != ParticleSortMode::None &&
                (!sortProgram || !sortProgram->IsValid())) {
                SetError(error, "GPU particle sorting kernels are unavailable");
                return {};
            }
            if ((output.shaderProgram || output.type == GpuParticleOutputType::Mesh) &&
                (!cullProgram || !cullProgram->IsValid())) {
                SetError(error, "GPU particle view-culling kernels are unavailable");
                return {};
            }
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

    [[nodiscard]] bool RefreshDataInterfaces(Emitter &emitter)
    {
        if (emitter.pointCaches.empty() && emitter.vectorFields.empty())
            return true;
        std::vector<uint64_t> currentPointCacheGenerations;
        currentPointCacheGenerations.reserve(emitter.pointCaches.size());
        std::vector<uint64_t> currentVectorFieldGenerations;
        currentVectorFieldGenerations.reserve(emitter.vectorFields.size());
        bool changed = false;
        for (size_t index = 0; index < emitter.pointCaches.size(); ++index) {
            const uint64_t generation = emitter.pointCaches[index] ? emitter.pointCaches[index]->GetGeneration() : 0;
            currentPointCacheGenerations.push_back(generation);
            changed = changed || generation != emitter.observedPointCacheGenerations[index];
        }
        for (size_t index = 0; index < emitter.vectorFields.size(); ++index) {
            const uint64_t generation = emitter.vectorFields[index] ? emitter.vectorFields[index]->GetGeneration() : 0;
            currentVectorFieldGenerations.push_back(generation);
            changed = changed || generation != emitter.observedVectorFieldGenerations[index];
        }
        if (!changed)
            return true;

        std::string error;
        GpuEmitterDesc runtimeDesc;
        std::vector<std::shared_ptr<InxPointCache>> pointCaches;
        std::vector<uint64_t> pointCacheGenerations;
        std::vector<std::shared_ptr<InxTexture>> vectorFields;
        std::vector<uint64_t> vectorFieldGenerations;
        auto replacement = std::make_unique<ParticleGpuRuntime>();
        const bool descriptorValid =
            BuildRuntimeDesc(emitter.sourceProgram, runtimeDesc, pointCaches, pointCacheGenerations, vectorFields,
                             vectorFieldGenerations, &error);
        const bool replacementCreated =
            descriptorValid && replacement->CreateCompatible(context->GetRhiDevice(), runtimeDesc, *emitter.runtime);
        if (!replacementCreated || !deletionQueue || !emitter.runtime->AdoptCompatibleRevision(*replacement)) {
            const bool uploadPending = error == "GPU particle Point Cache upload is pending" ||
                                       error == "GPU particle Vector Field texture upload is pending";
            if (error != "GPU particle Point Cache upload is pending")
                emitter.observedPointCacheGenerations = std::move(currentPointCacheGenerations);
            if (error != "GPU particle Vector Field texture upload is pending")
                emitter.observedVectorFieldGenerations = std::move(currentVectorFieldGenerations);
            if (!uploadPending)
                INXLOG_WARN("GPU particle data-interface refresh kept last-known-good emitter '", emitter.stableId,
                            "': ", error.empty() ? "failed to create a compatible RHI revision" : error);
            return false;
        }

        emitter.pointCaches = std::move(pointCaches);
        emitter.pointCacheGenerations = pointCacheGenerations;
        emitter.observedPointCacheGenerations = std::move(pointCacheGenerations);
        emitter.vectorFields = std::move(vectorFields);
        emitter.vectorFieldGenerations = vectorFieldGenerations;
        emitter.observedVectorFieldGenerations = std::move(vectorFieldGenerations);
        auto retired = std::shared_ptr<ParticleGpuRuntime>(std::move(replacement));
        deletionQueue->Push([retired = std::move(retired)]() mutable { retired.reset(); });
        return true;
    }

    [[nodiscard]] std::shared_ptr<GraphState> BuildGraph(const EmitterMap &candidateEmitters,
                                                         const EventDomainMap &candidateEventDomains,
                                                         std::string *error) const
    {
        auto state = std::make_shared<GraphState>();
        state->graph = std::make_unique<vk::RenderGraph>();
        state->graph->Initialize(context, pipelines, deletionQueue);
        auto *graph = state->graph.get();
        state->eventFrames.reserve(candidateEventDomains.size());
        for (const auto &[graphInstanceId, domain] : candidateEventDomains) {
            if (!domain || !domain->IsValid()) {
                SetError(error, "GPU particle event graph contains an invalid event domain");
                return {};
            }
            auto frame = std::make_shared<EventFrameState>();
            frame->domain = domain;
            state->eventFrames.emplace(graphInstanceId, frame);
            const std::string prefix = "GpuParticleEvent/" + std::to_string(graphInstanceId);
            state->graph->AddComputePass(prefix + "/Prepare", [domain, frame, prefix, graph](vk::PassBuilder &builder) {
                auto channelTable =
                    builder.ImportBuffer(prefix + "/Channels", domain->ChannelTable(),
                                         uint64_t(domain->ChannelCount()) * sizeof(GpuParticleEventChannelRecord));
                builder.ReadStorageBuffer(channelTable);
                for (uint32_t pageIndex = 0; pageIndex < domain->PageCount(); ++pageIndex) {
                    const auto *page = domain->Page(pageIndex);
                    if (!page)
                        continue;
                    auto counters =
                        builder.ImportBuffer(prefix + "/Counters" + std::to_string(pageIndex), page->counters,
                                             uint64_t(domain->ChannelCount()) * sizeof(GpuParticleEventCounter));
                    auto indirect = builder.ImportBuffer(
                        prefix + "/Indirect" + std::to_string(pageIndex), page->indirectArguments,
                        uint64_t(domain->ChannelCount()) * sizeof(GpuParticleEventDispatchArguments));
                    builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
                    builder.ReadWrite(indirect, rhi::PipelineStage::ComputeShader);
                    graph->SetResourceInitialState(counters, rhi::TextureLayout::Undefined, rhi::Access::ShaderWrite,
                                                   rhi::PipelineStage::ComputeShader);
                    graph->SetResourceInitialState(indirect, rhi::TextureLayout::Undefined, rhi::Access::ShaderWrite,
                                                   rhi::PipelineStage::ComputeShader);
                }
                builder.SetSideEffect();
                return [domain, frame](vk::RenderContext &context) {
                    if (!frame->pending)
                        return;
                    frame->pending = false;
                    if (!frame->simulate) {
                        frame->active = false;
                        return;
                    }
                    domain->RecordPrepare(context.GetComputeCommandEncoder());
                    frame->active = true;
                };
            });

            std::unordered_map<uint32_t, std::shared_ptr<Emitter>> emittersByDenseIndex;
            for (const auto &[emitterId, emitter] : candidateEmitters) {
                (void)emitterId;
                if (emitter && emitter->graphInstanceId == graphInstanceId)
                    emittersByDenseIndex.emplace(emitter->sourceProgram.graphEmitterIndex, emitter);
            }
            frame->emitterRequests.resize(emittersByDenseIndex.size());
            for (uint32_t channelIndex = 0; channelIndex < domain->ChannelCount(); ++channelIndex) {
                const uint32_t targetIndex = domain->ChannelTargetEmitterIndex(channelIndex);
                const auto target = emittersByDenseIndex.find(targetIndex);
                if (target == emittersByDenseIndex.end() || !target->second || !target->second->runtime) {
                    SetError(error, "GPU particle event channel has no target emitter runtime");
                    return {};
                }
                const auto targetEmitter = target->second;
                const std::string routePrefix = prefix + "/Route" + std::to_string(channelIndex);
                state->graph->AddComputePass(routePrefix + "/Allocate", [domain, frame, targetEmitter, channelIndex,
                                                                         targetIndex, routePrefix,
                                                                         graph](vk::PassBuilder &builder) {
                    auto channelTable =
                        builder.ImportBuffer(routePrefix + "/Channels", domain->ChannelTable(),
                                             uint64_t(domain->ChannelCount()) * sizeof(GpuParticleEventChannelRecord));
                    builder.ReadStorageBuffer(channelTable);
                    for (uint32_t pageIndex = 0; pageIndex < domain->PageCount(); ++pageIndex) {
                        const auto *page = domain->Page(pageIndex);
                        if (!page)
                            continue;
                        auto counters =
                            builder.ImportBuffer(routePrefix + "/Counters" + std::to_string(pageIndex), page->counters,
                                                 uint64_t(domain->ChannelCount()) * sizeof(GpuParticleEventCounter));
                        auto indirect = builder.ImportBuffer(
                            routePrefix + "/Indirect" + std::to_string(pageIndex), page->indirectArguments,
                            uint64_t(domain->ChannelCount()) * sizeof(GpuParticleEventDispatchArguments));
                        auto spawnIndices =
                            builder.ImportBuffer(routePrefix + "/SpawnIndices" + std::to_string(pageIndex),
                                                 page->spawnIndices, domain->SpawnIndexBufferBytes());
                        builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
                        builder.ReadIndirectBuffer(indirect);
                        builder.WriteStorageBuffer(spawnIndices);
                        graph->SetResourceInitialState(counters, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader);
                        graph->SetResourceInitialState(indirect, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader);
                        graph->SetResourceInitialState(spawnIndices, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader);
                    }
                    auto freeList =
                        builder.ImportBuffer(routePrefix + "/TargetFreeList", targetEmitter->runtime->FreeListBuffer(),
                                             uint64_t(targetEmitter->runtime->Capacity()) * sizeof(uint32_t));
                    auto targetCounters = builder.ImportBuffer(routePrefix + "/TargetCounters",
                                                               targetEmitter->runtime->CounterBuffer(), 16);
                    builder.ReadWrite(freeList, rhi::PipelineStage::ComputeShader);
                    builder.ReadWrite(targetCounters, rhi::PipelineStage::ComputeShader);
                    graph->SetResourceInitialState(freeList, rhi::TextureLayout::Undefined, rhi::Access::ShaderWrite,
                                                   rhi::PipelineStage::ComputeShader);
                    graph->SetResourceInitialState(targetCounters, rhi::TextureLayout::Undefined,
                                                   rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader);
                    builder.SetSideEffect();
                    return [domain, frame, channelIndex, targetIndex](vk::RenderContext &context) {
                        if (frame->active && targetIndex < frame->emitterRequests.size() &&
                            frame->emitterRequests[targetIndex].simulate)
                            domain->RecordAllocate(context.GetComputeCommandEncoder(), channelIndex);
                    };
                });

                const bool isLastChannel = channelIndex + 1u == domain->ChannelCount();
                state->graph->AddComputePass(routePrefix + "/Init", [domain, frame, targetEmitter, channelIndex,
                                                                     targetIndex, routePrefix, isLastChannel,
                                                                     graph](vk::PassBuilder &builder) {
                    auto channelTable =
                        builder.ImportBuffer(routePrefix + "/InitChannels", domain->ChannelTable(),
                                             uint64_t(domain->ChannelCount()) * sizeof(GpuParticleEventChannelRecord));
                    builder.ReadStorageBuffer(channelTable);
                    for (uint32_t pageIndex = 0; pageIndex < domain->PageCount(); ++pageIndex) {
                        const auto *page = domain->Page(pageIndex);
                        if (!page)
                            continue;
                        auto records = builder.ImportBuffer(routePrefix + "/Records" + std::to_string(pageIndex),
                                                            page->records, domain->RecordBufferBytes());
                        auto counters = builder.ImportBuffer(
                            routePrefix + "/InitCounters" + std::to_string(pageIndex), page->counters,
                            uint64_t(domain->ChannelCount()) * sizeof(GpuParticleEventCounter));
                        auto indirect = builder.ImportBuffer(
                            routePrefix + "/InitIndirect" + std::to_string(pageIndex), page->indirectArguments,
                            uint64_t(domain->ChannelCount()) * sizeof(GpuParticleEventDispatchArguments));
                        auto spawnIndices =
                            builder.ImportBuffer(routePrefix + "/InitSpawnIndices" + std::to_string(pageIndex),
                                                 page->spawnIndices, domain->SpawnIndexBufferBytes());
                        builder.ReadWrite(records, rhi::PipelineStage::ComputeShader);
                        builder.ReadWrite(counters, rhi::PipelineStage::ComputeShader);
                        builder.ReadStorageBuffer(spawnIndices);
                        builder.ReadIndirectBuffer(indirect);
                        graph->SetResourceInitialState(records, rhi::TextureLayout::Undefined, rhi::Access::ShaderWrite,
                                                       rhi::PipelineStage::ComputeShader);
                        graph->SetResourceInitialState(counters, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader);
                        graph->SetResourceInitialState(indirect, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader);
                        graph->SetResourceInitialState(spawnIndices, rhi::TextureLayout::Undefined,
                                                       rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader);
                    }
                    auto states = builder.ImportBuffer(
                        routePrefix + "/TargetStates", targetEmitter->runtime->StateBuffer(),
                        uint64_t(targetEmitter->runtime->Capacity()) * targetEmitter->runtime->StateStride());
                    auto freeList = builder.ImportBuffer(
                        routePrefix + "/InitTargetFreeList", targetEmitter->runtime->FreeListBuffer(),
                        uint64_t(targetEmitter->runtime->Capacity()) * sizeof(uint32_t));
                    auto targetCounters = builder.ImportBuffer(routePrefix + "/InitTargetCounters",
                                                               targetEmitter->runtime->CounterBuffer(), 16);
                    auto transforms =
                        builder.ImportBuffer(routePrefix + "/TargetTransforms",
                                             targetEmitter->runtime->TransformBuffer(), sizeof(GpuParticleTransforms));
                    builder.ReadWrite(states, rhi::PipelineStage::ComputeShader);
                    builder.ReadWrite(freeList, rhi::PipelineStage::ComputeShader);
                    builder.ReadWrite(targetCounters, rhi::PipelineStage::ComputeShader);
                    builder.ReadUniformBuffer(transforms);
                    graph->SetResourceInitialState(states, rhi::TextureLayout::Undefined, rhi::Access::ShaderWrite,
                                                   rhi::PipelineStage::ComputeShader);
                    graph->SetResourceInitialState(freeList, rhi::TextureLayout::Undefined, rhi::Access::ShaderWrite,
                                                   rhi::PipelineStage::ComputeShader);
                    graph->SetResourceInitialState(targetCounters, rhi::TextureLayout::Undefined,
                                                   rhi::Access::ShaderWrite, rhi::PipelineStage::ComputeShader);
                    builder.SetSideEffect();
                    return [domain, frame, targetEmitter, channelIndex, targetIndex,
                            isLastChannel](vk::RenderContext &context) {
                        if (!frame->active)
                            return;
                        if (domain->HasPreparedInput() && targetIndex < frame->emitterRequests.size() &&
                            frame->emitterRequests[targetIndex].simulate) {
                            const auto &request = frame->emitterRequests[targetIndex];
                            targetEmitter->runtime->RecordEventInit(
                                context.GetComputeCommandEncoder(), domain->CurrentEventInputGroup(channelIndex),
                                domain->CurrentIndirectArguments(),
                                static_cast<uint64_t>(channelIndex) * sizeof(GpuParticleEventDispatchArguments),
                                channelIndex, request.systemSeed, request.simulationStep, request.deltaTime,
                                domain->CurrentEventOutputGroup());
                        }
                        if (isLastChannel)
                            frame->active = false;
                    };
                });
            }
        }
        state->schedulers.reserve(candidateEmitters.size());
        state->schedulerById.reserve(candidateEmitters.size());
        for (const auto &[id, emitter] : candidateEmitters) {
            auto scheduler = std::make_unique<ParticleRenderGraph>();
            const std::string prefix = "GpuParticle/" + std::to_string(id);
            const auto eventDomain = candidateEventDomains.find(emitter->graphInstanceId);
            ParticleGpuEventDomain *eventDomainPointer =
                eventDomain != candidateEventDomains.end() ? eventDomain->second.get() : nullptr;
            if (!scheduler->Attach(*state->graph, *emitter->runtime, *emitter->bounds, prefix, emitter->migration.get(),
                                   emitter->ribbonTopology.get(), eventDomainPointer)) {
                SetError(error, "failed to attach GPU particle emitter to the simulation graph");
                return {};
            }
            state->schedulerById.emplace(id, scheduler.get());
            state->schedulers.push_back(std::move(scheduler));
        }
        if ((!candidateEmitters.empty() || !candidateEventDomains.empty()) && !state->graph->Compile()) {
            SetError(error, "failed to compile the GPU particle simulation graph");
            return {};
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
                entry.ownerObjectId = emitter->sourceProgram.ownerObjectId;
                entry.ownerLayerMask = emitter->sourceProgram.ownerLayerMask;
                entry.capacity = emitter->runtime->Capacity();
                entry.instances = emitter->runtime->InstanceBuffer();
                entry.renderIndices = output.renderer->RenderIndexBuffer();
                entry.indirectArguments = output.type == GpuParticleOutputType::Ribbon
                                              ? emitter->ribbonTopology->DrawIndirectBuffer()
                                              : emitter->runtime->IndirectBuffer();
                entry.bounds = emitter->bounds->BoundsBuffer();
                entry.renderer = output.renderer;
                entry.cullProgram = (output.type != GpuParticleOutputType::Ribbon &&
                                     (output.shaderProgram || output.type == GpuParticleOutputType::Mesh ||
                                      output.semantics.sortMode != ParticleSortMode::None))
                                        ? cullProgram
                                        : nullptr;
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

    void Retire(std::shared_ptr<GraphState> oldGraph, EmitterMap oldEmitters,
                std::vector<std::shared_ptr<ParticleGpuEventDomain>> oldEventDomains = {})
    {
        if (oldEmitters.empty() && oldEventDomains.empty())
            return;
        if (deletionQueue) {
            deletionQueue->Push([oldGraph = std::move(oldGraph), oldEmitters = std::move(oldEmitters),
                                 oldEventDomains = std::move(oldEventDomains)]() mutable {
                oldGraph.reset();
                oldEmitters.clear();
                oldEventDomains.clear();
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

        auto replacementGraph = BuildGraph(emitters, eventDomains, nullptr);
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
            deletionQueue->Push([graph = std::move(retiredGraph), emitters = std::move(graphLifetime),
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
    FrameDeletionQueue &deletionQueue, ParticleGpuDrawRegistry &drawRegistry,
    GpuBillboardTextureResolver textureResolver, GpuBillboardTextureVersionResolver textureVersionResolver,
    GpuParticleVectorFieldTextureResolver vectorFieldTextureResolver, const GpuParticleSortProgram &sortProgram,
    const GpuParticleCullProgram &cullProgram, const GpuParticleBoundsProgram &boundsProgram,
    const GpuParticleMigrationProgram &migrationProgram, const GpuParticleEventProgram &eventProgram,
    const GpuParticleRibbonProgram &ribbonTopologyProgram, const GpuParticleRibbonRenderProgram &ribbonRenderProgram)
{
    if (!m_impl || m_impl->context || !context.IsValid() || !boundsProgram.IsValid() || !migrationProgram.IsValid() ||
        !eventProgram.IsValid() || ribbonTopologyProgram.IsValid() != ribbonRenderProgram.IsValid())
        return false;
    m_impl->context = &context;
    m_impl->pipelines = &pipelines;
    m_impl->resources = &resources;
    m_impl->deletionQueue = &deletionQueue;
    m_impl->drawRegistry = &drawRegistry;
    m_impl->textureResolver = std::move(textureResolver);
    m_impl->textureVersionResolver = std::move(textureVersionResolver);
    m_impl->vectorFieldTextureResolver = std::move(vectorFieldTextureResolver);
    auto boundsStorage = std::make_shared<GpuParticleBoundsProgramStorage>();
    if (!boundsStorage->Assign(boundsProgram)) {
        Shutdown();
        return false;
    }
    m_impl->boundsProgram = std::move(boundsStorage);
    auto migrationStorage = std::make_shared<GpuParticleMigrationProgramStorage>();
    if (!migrationStorage->Assign(migrationProgram)) {
        Shutdown();
        return false;
    }
    m_impl->migrationProgram = std::move(migrationStorage);
    auto eventStorage = std::make_shared<GpuParticleEventProgramStorage>();
    if (!eventStorage->Assign(eventProgram)) {
        Shutdown();
        return false;
    }
    m_impl->eventProgram = std::move(eventStorage);
    if (ribbonTopologyProgram.IsValid()) {
        auto ribbonTopologyStorage = std::make_shared<GpuParticleRibbonProgramStorage>();
        auto ribbonRenderStorage = std::make_shared<GpuParticleRibbonRenderProgramStorage>();
        if (!ribbonTopologyStorage->Assign(ribbonTopologyProgram) ||
            !ribbonRenderStorage->Assign(ribbonRenderProgram)) {
            Shutdown();
            return false;
        }
        m_impl->ribbonTopologyProgram = std::move(ribbonTopologyStorage);
        m_impl->ribbonRenderProgram = std::move(ribbonRenderStorage);
    }
    if (cullProgram.IsValid()) {
        auto storage = std::make_shared<GpuParticleCullProgramStorage>();
        if (!storage->Assign(cullProgram)) {
            Shutdown();
            return false;
        }
        m_impl->cullProgram = std::move(storage);
    }
    if (sortProgram.IsValid()) {
        auto storage = std::make_shared<GpuParticleSortProgramStorage>();
        if (!storage->Assign(sortProgram)) {
            Shutdown();
            return false;
        }
        m_impl->sortProgram = std::move(storage);
    }
    m_impl->graphState = m_impl->BuildGraph({}, {}, nullptr);
    if (!m_impl->graphState) {
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
    {
        std::scoped_lock lock(m_impl->diagnosticState->mutex);
        for (const auto &request : m_impl->pendingDiagnostics) {
            auto &snapshot = m_impl->diagnosticState->snapshots[request.requestId];
            snapshot.status = GpuParticleDiagnosticStatus::Failed;
            snapshot.error = "GPU particle manager shut down before diagnostic recording";
        }
    }
    m_impl->pendingDiagnostics.clear();
    m_impl->graphState.reset();
    m_impl->emitters.clear();
    m_impl->eventDomains.clear();
    m_impl->pointCacheUploads.clear();
    m_impl->meshUploads.clear();
    m_impl->drawRegistry = nullptr;
    m_impl->textureResolver = {};
    m_impl->textureVersionResolver = {};
    m_impl->cullProgram.reset();
    m_impl->sortProgram.reset();
    m_impl->boundsProgram.reset();
    m_impl->migrationProgram.reset();
    m_impl->eventProgram.reset();
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
    if (program.emitters.empty() && program.removeEmitterIds.empty() && !program.eventDomain) {
        SetError(error, "GPU particle update batch cannot be empty");
        return false;
    }
    if (program.eventDomain && program.eventDomain->graphInstanceId != program.graphInstanceId) {
        SetError(error, "GPU particle event domain does not belong to the published graph");
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
        auto emitter = m_impl->CreateEmitter(
            emitterProgram, previous != m_impl->emitters.end() ? previous->second : std::shared_ptr<Impl::Emitter>{},
            error);
        if (!emitter)
            return false;
        candidates[emitterProgram.id] = std::move(emitter);
    }

    if (program.eventDomain) {
        const size_t graphEmitterCount =
            static_cast<size_t>(std::count_if(candidates.begin(), candidates.end(), [&](const auto &entry) {
                return entry.second && entry.second->graphInstanceId == program.graphInstanceId;
            }));
        if (graphEmitterCount != program.emitters.size()) {
            SetError(error, "GPU particle event publication requires the complete graph emitter set");
            return false;
        }
        std::vector<bool> denseEmitterIndices(program.emitters.size(), false);
        for (const auto &emitter : program.emitters) {
            if (emitter.graphEmitterIndex >= denseEmitterIndices.size() ||
                denseEmitterIndices[emitter.graphEmitterIndex]) {
                SetError(error, "GPU particle event publication requires unique dense emitter indices");
                return false;
            }
            denseEmitterIndices[emitter.graphEmitterIndex] = true;
        }
        for (const auto &channel : program.eventDomain->channels) {
            if (channel.sourceEmitterIndex >= program.emitters.size() ||
                channel.targetEmitterIndex >= program.emitters.size()) {
                SetError(error, "GPU particle event channel references an emitter outside the published graph");
                return false;
            }
        }
    }

    std::shared_ptr<ParticleGpuEventDomain> candidateEventDomain;
    const auto currentEventDomain = m_impl->eventDomains.find(program.graphInstanceId);
    if (program.eventDomain) {
        std::unordered_set<uint32_t> targetEmitterIndices;
        targetEmitterIndices.reserve(program.eventDomain->channels.size());
        for (const auto &channel : program.eventDomain->channels)
            targetEmitterIndices.insert(channel.targetEmitterIndex);
        std::vector<GpuParticleEventTargetDesc> eventTargets;
        eventTargets.reserve(targetEmitterIndices.size());
        for (const auto &emitterProgram : program.emitters) {
            if (targetEmitterIndices.find(emitterProgram.graphEmitterIndex) == targetEmitterIndices.end())
                continue;
            const auto candidate = candidates.find(emitterProgram.id);
            if (candidate == candidates.end() || !candidate->second || !candidate->second->runtime) {
                SetError(error, "GPU particle event target runtime is missing");
                return false;
            }
            eventTargets.push_back({emitterProgram.graphEmitterIndex, candidate->second->runtime->Capacity(),
                                    candidate->second->runtime->FreeListBuffer(),
                                    candidate->second->runtime->CounterBuffer(),
                                    candidate->second->runtime->EventInputLayout()});
        }
        const uint32_t expectedPages =
            std::max(program.eventDomain->framesInFlight, ParticleGpuEventDomain::MinimumPageCount);
        if (currentEventDomain != m_impl->eventDomains.end() &&
            currentEventDomain->second->EventAbiHash() == program.eventDomain->eventAbiHash &&
            currentEventDomain->second->PageCount() == expectedPages &&
            currentEventDomain->second->MatchesTargets(eventTargets)) {
            candidateEventDomain = currentEventDomain->second;
        } else {
            candidateEventDomain = std::make_shared<ParticleGpuEventDomain>();
            if (!candidateEventDomain->Create(m_impl->context->GetRhiDevice(), *program.eventDomain,
                                              m_impl->eventProgram->View(), eventTargets)) {
                SetError(error, "failed to create GPU particle event domain");
                return false;
            }
        }
    }
    Impl::EventDomainMap candidateEventDomains = m_impl->eventDomains;
    if (candidateEventDomain)
        candidateEventDomains[program.graphInstanceId] = candidateEventDomain;
    else
        candidateEventDomains.erase(program.graphInstanceId);
    auto candidateGraph = m_impl->BuildGraph(candidates, candidateEventDomains, error);
    if (!candidateGraph)
        return false;
    if (!m_impl->drawRegistry->Replace(m_impl->BuildDrawEntries(candidates))) {
        SetError(error, "failed to publish GPU particle draw entries");
        return false;
    }

    auto oldGraph = std::move(m_impl->graphState);
    auto oldEmitters = std::move(m_impl->emitters);
    std::vector<std::shared_ptr<ParticleGpuEventDomain>> retiredEventDomains;
    if (currentEventDomain != m_impl->eventDomains.end() && currentEventDomain->second != candidateEventDomain)
        retiredEventDomains.push_back(currentEventDomain->second);
    if (candidateEventDomain)
        m_impl->eventDomains[program.graphInstanceId] = std::move(candidateEventDomain);
    else
        m_impl->eventDomains.erase(program.graphInstanceId);
    m_impl->graphState = std::move(candidateGraph);
    m_impl->emitters = std::move(candidates);
    m_impl->Retire(std::move(oldGraph), std::move(oldEmitters), std::move(retiredEventDomains));
    return true;
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
        return std::any_of(entry.second->outputs.begin(), entry.second->outputs.end(), [&](const auto &output) {
            return output.type == GpuParticleOutputType::Sprite && output.material.get() == material.get();
        });
    });
    if (!referenced)
        return true;
    if (shaderProgram && (!shaderProgram->IsValid() || shaderProgram->domain != ShaderProgramDomain::ParticleSprite)) {
        SetError(error, "GPU particle material must resolve to a valid ParticleSprite shader program");
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
            if (output.type != GpuParticleOutputType::Sprite || output.material.get() != material.get())
                continue;
            const bool sameProgram =
                (!output.shaderProgram && !shaderProgram) ||
                (output.shaderProgram && shaderProgram && output.shaderProgram->key == shaderProgram->key);
            if (sameProgram)
                continue;
            if (shaderProgram && output.semantics.receiveSceneLighting &&
                !shaderProgram->FindVariant(ShaderCompileTarget::ForwardPlus)) {
                SetError(error, "GPU particle output '" + output.stableId +
                                    "' requires a linked Particle Forward+ shader variant when Receive Scene "
                                    "Lighting is enabled");
                return false;
            }
            if (output.semantics.sortMode != ParticleSortMode::None && !shaderProgram) {
                SetError(error,
                         "GPU particle sorted output '" + output.stableId + "' cannot use a legacy billboard material");
                return false;
            }
            GpuParticleOutputProgram candidate;
            candidate.id = output.id;
            candidate.stableId = output.stableId;
            candidate.type = output.type;
            candidate.material = material;
            candidate.shaderProgram = shaderProgram;
            candidate.fallbackMaterial = output.fallbackMaterial;
            candidate.semantics = output.semantics;
            candidate.flipbookColumns = output.flipbookColumns;
            candidate.flipbookRows = output.flipbookRows;
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
        m_impl->deletionQueue->Push([retired = std::move(retired)]() mutable { retired.clear(); });
    }
    return true;
}

void ParticleGpuSystemManager::Clear()
{
    if (!m_impl || !m_impl->context || (m_impl->emitters.empty() && m_impl->eventDomains.empty()))
        return;
    auto candidateGraph = m_impl->BuildGraph({}, {}, nullptr);
    if (!candidateGraph)
        return;
    if (m_impl->drawRegistry && !m_impl->drawRegistry->Replace({}))
        return;
    auto oldGraph = std::move(m_impl->graphState);
    auto oldEmitters = std::move(m_impl->emitters);
    std::vector<std::shared_ptr<ParticleGpuEventDomain>> oldEventDomains;
    oldEventDomains.reserve(m_impl->eventDomains.size());
    for (auto &[id, domain] : m_impl->eventDomains) {
        (void)id;
        oldEventDomains.push_back(std::move(domain));
    }
    m_impl->eventDomains.clear();
    m_impl->graphState = std::move(candidateGraph);
    m_impl->emitters.clear();
    m_impl->Retire(std::move(oldGraph), std::move(oldEmitters), std::move(oldEventDomains));
}

bool ParticleGpuSystemManager::BeginFrame(uint64_t id, const GpuParticleFrameRequest &request,
                                          const GpuParticleTransforms &transforms)
{
    if (!m_impl)
        return false;
    const auto emitter = m_impl->emitters.find(id);
    if (emitter == m_impl->emitters.end())
        return false;
    return BeginFrameBatch(emitter->second->graphInstanceId, {{id, request, transforms}});
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
    };
    std::vector<PreparedItem> prepared;
    prepared.reserve(items.size());
    std::unordered_set<uint64_t> emitterIds;
    emitterIds.reserve(items.size());
    const uint64_t frameIndex = items.front().request.frameIndex;
    const auto eventFrame = m_impl->graphState->eventFrames.find(graphInstanceId);
    if (eventFrame != m_impl->graphState->eventFrames.end()) {
        if (!eventFrame->second || eventFrame->second->pending || eventFrame->second->active)
            return false;
        const size_t graphEmitterCount = static_cast<size_t>(
            std::count_if(m_impl->emitters.begin(), m_impl->emitters.end(), [graphInstanceId](const auto &entry) {
                return entry.second->graphInstanceId == graphInstanceId;
            }));
        if (items.size() != graphEmitterCount)
            return false;
    }

    for (const auto &item : items) {
        const auto emitter = m_impl->emitters.find(item.emitterId);
        const auto scheduler = m_impl->graphState->schedulerById.find(item.emitterId);
        if (item.emitterId == 0 || !emitterIds.insert(item.emitterId).second || item.request.frameIndex != frameIndex ||
            !IsFinite(item.transforms) || emitter == m_impl->emitters.end() ||
            scheduler == m_impl->graphState->schedulerById.end() || emitter->second->graphInstanceId != graphInstanceId)
            return false;
        prepared.push_back({&item, emitter->second, scheduler->second});
    }

    std::vector<GpuParticleFrameRequest> eventEmitterRequests;
    if (eventFrame != m_impl->graphState->eventFrames.end()) {
        if (eventFrame->second->emitterRequests.size() != prepared.size())
            return false;
        eventEmitterRequests.resize(prepared.size());
        std::vector<bool> assigned(prepared.size(), false);
        for (const auto &entry : prepared) {
            const uint32_t denseIndex = entry.emitter->sourceProgram.graphEmitterIndex;
            if (denseIndex >= eventEmitterRequests.size() || assigned[denseIndex])
                return false;
            assigned[denseIndex] = true;
            eventEmitterRequests[denseIndex] = entry.item->request;
        }
    }

    for (const auto &entry : prepared)
        (void)m_impl->RefreshDataInterfaces(*entry.emitter);

    for (const auto &entry : prepared) {
        if (!entry.scheduler->CanBeginFrame(entry.item->request))
            return false;
    }
    for (const auto &entry : prepared) {
        if (!entry.emitter->runtime->UpdateTransforms(entry.item->transforms))
            return false;
    }
    for (const auto &entry : prepared) {
        if (!entry.scheduler->BeginFrame(entry.item->request))
            return false;
    }
    for (const auto &entry : prepared) {
        entry.emitter->hasFrameRequest = true;
        entry.emitter->lastFrameIndex = entry.item->request.frameIndex;
        entry.emitter->lastSpawnCount = entry.item->request.spawnCount;
        entry.emitter->lastSimulate = entry.item->request.simulate;
        entry.emitter->lastRender = entry.item->request.render;
    }
    if (eventFrame != m_impl->graphState->eventFrames.end()) {
        eventFrame->second->emitterRequests = std::move(eventEmitterRequests);
        eventFrame->second->simulate = std::any_of(
            prepared.begin(), prepared.end(), [](const PreparedItem &entry) { return entry.item->request.simulate; });
        eventFrame->second->pending = true;
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
    scheduler->second->Reset();
    return true;
}

void ParticleGpuSystemManager::Execute(VkCommandBuffer commandBuffer)
{
    if (!m_impl || !m_impl->graphState || !m_impl->graphState->graph || commandBuffer == VK_NULL_HANDLE)
        return;
    const bool hasPendingEmitter =
        std::any_of(m_impl->graphState->schedulers.begin(), m_impl->graphState->schedulers.end(),
                    [](const auto &scheduler) { return scheduler->HasPendingFrame(); });
    const bool hasPendingEvent =
        std::any_of(m_impl->graphState->eventFrames.begin(), m_impl->graphState->eventFrames.end(),
                    [](const auto &entry) { return entry.second && entry.second->pending; });
    const bool hasPending = hasPendingEmitter || hasPendingEvent;
    if (hasPending) {
        m_impl->graphState->graph->Execute(commandBuffer);
        m_impl->RetireCompletedMigrations();
    }
    m_impl->RecordDiagnostics(commandBuffer);
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

uint64_t ParticleGpuSystemManager::ActivePointCacheGeneration(uint64_t id, uint32_t interfaceIndex) const
{
    if (!m_impl)
        return 0;
    const auto found = m_impl->emitters.find(id);
    if (found == m_impl->emitters.end() || interfaceIndex >= found->second->pointCacheGenerations.size())
        return 0;
    return found->second->pointCacheGenerations[interfaceIndex];
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

uint64_t ParticleGpuSystemManager::ActiveEventAbiHash(uint64_t graphInstanceId) const
{
    if (!m_impl)
        return 0;
    const auto found = m_impl->eventDomains.find(graphInstanceId);
    return found != m_impl->eventDomains.end() ? found->second->EventAbiHash() : 0;
}

uint64_t ParticleGpuSystemManager::ActiveEventDomainSerial(uint64_t graphInstanceId) const
{
    if (!m_impl)
        return 0;
    const auto found = m_impl->eventDomains.find(graphInstanceId);
    return found != m_impl->eventDomains.end() && found->second ? found->second->InstanceSerial() : 0;
}

uint32_t ParticleGpuSystemManager::ActiveEventPageCount(uint64_t graphInstanceId) const
{
    if (!m_impl)
        return 0;
    const auto found = m_impl->eventDomains.find(graphInstanceId);
    return found != m_impl->eventDomains.end() ? found->second->PageCount() : 0;
}

uint64_t ParticleGpuSystemManager::RequestDiagnostics(uint64_t graphInstanceId)
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
    if (m_impl->pendingDiagnostics.size() >= 8) {
        snapshot.status = GpuParticleDiagnosticStatus::Failed;
        snapshot.error = "Too many GPU particle diagnostic requests are pending";
    } else if (!m_impl->context || !m_impl->deletionQueue || !hasEmitter) {
        snapshot.status = GpuParticleDiagnosticStatus::Failed;
        snapshot.error =
            hasEmitter ? "GPU particle diagnostics are unavailable" : "GPU particle graph has no resident emitters";
    } else {
        snapshot.status = GpuParticleDiagnosticStatus::Pending;
        m_impl->pendingDiagnostics.push_back({requestId, graphInstanceId});
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
