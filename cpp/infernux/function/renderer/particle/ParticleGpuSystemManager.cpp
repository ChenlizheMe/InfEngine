#include "ParticleGpuSystemManager.h"

#include "ParticleGpuDrawRegistry.h"

#include <core/log/InxLog.h>
#include <function/renderer/FrameDeletionQueue.h>
#include <function/renderer/vk/RenderGraph.h>
#include <function/renderer/vk/VkDeviceContext.h>
#include <function/renderer/vk/VkPipelineManager.h>
#include <function/renderer/vk/VkResourceManager.h>

#include <algorithm>
#include <map>
#include <memory>
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

} // namespace

struct ParticleGpuSystemManager::Impl
{
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

    struct Emitter
    {
        struct Output
        {
            uint64_t id = 0;
            std::string stableId;
            std::shared_ptr<InxMaterial> material;
            std::shared_ptr<const ShaderProgramArtifact> shaderProgram;
            GpuBillboardMaterialState fallbackMaterial;
            ParticleOutputSemantics semantics;
            std::shared_ptr<ParticleGpuBillboardRenderer> renderer;
        };

        uint64_t id = 0;
        uint64_t artifactRevision = 0;
        std::string stableId;
        bool statePreservedOnPublish = false;
        std::unique_ptr<ParticleGpuRuntime> runtime;
        std::unique_ptr<ParticleGpuBounds> bounds;
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
        std::vector<uint32_t> billboardPickingFragmentShader;
        std::vector<Output> outputs;
    };

    struct GraphState
    {
        std::unique_ptr<vk::RenderGraph> graph;
        std::vector<std::unique_ptr<ParticleRenderGraph>> schedulers;
        std::unordered_map<uint64_t, ParticleRenderGraph *> schedulerById;
    };

    using EmitterMap = std::map<uint64_t, std::shared_ptr<Emitter>>;

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
    EmitterMap emitters;
    std::shared_ptr<GraphState> graphState;
    mutable std::unordered_map<const InxPointCache *, PointCacheUpload> pointCacheUploads;

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
        for (size_t index = 0; index < program.kernels.size(); ++index)
            runtimeDesc.kernels[index] = {program.kernels[index].data(), program.kernels[index].size()};
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

        runtimeDesc.vectorFields.metadataBinding = program.vectorFields.metadataBinding;
        runtimeDesc.vectorFields.interfaceStrideWords = program.vectorFields.interfaceStrideWords;
        runtimeDesc.vectorFields.vectorFields.reserve(program.vectorFields.vectorFields.size());
        vectorFields.reserve(program.vectorFields.vectorFields.size());
        vectorFieldGenerations.reserve(program.vectorFields.vectorFields.size());
        stableIds.clear();
        for (const auto &field : program.vectorFields.vectorFields) {
            const auto cpuData = field.texture ? field.texture->GetCpuData() : nullptr;
            if (field.stableId.empty() || !stableIds.insert(field.stableId).second || !field.texture || !cpuData ||
                !cpuData->IsValid() || cpuData->dimension != TextureDimension::Texture3D ||
                cpuData->semantic != TextureSemantic::VectorField || field.texture->GetGuid().empty() ||
                !vectorFieldTextureResolver) {
                SetError(error, "GPU particle Vector Field bindings require unique identities and loaded VectorField "
                                "Texture3D assets");
                return false;
            }
            auto lease = vectorFieldTextureResolver(field.texture->GetGuid(), field.linearFiltering, field.repeat);
            if (lease.status != GpuBillboardTextureStatus::Ready || !lease.texture.IsValid() ||
                !lease.sampler.IsValid() || !lease.keepAlive) {
                SetError(error, lease.status == GpuBillboardTextureStatus::Pending
                                    ? "GPU particle Vector Field texture upload is pending"
                                    : "GPU particle Vector Field texture upload failed");
                return false;
            }
            GpuVectorFieldDesc runtimeField;
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

    [[nodiscard]] std::shared_ptr<ParticleGpuBillboardRenderer>
    CreateOutputRenderer(const Emitter &emitter, const std::shared_ptr<InxMaterial> &material,
                         const GpuBillboardMaterialState &fallbackMaterial,
                         const std::shared_ptr<const ShaderProgramArtifact> &shaderProgram) const
    {
        auto renderer = std::make_shared<ParticleGpuBillboardRenderer>();
        GpuBillboardRendererDesc rendererDesc;
        rendererDesc.vertexShader = {emitter.billboardVertexShader.data(), emitter.billboardVertexShader.size()};
        rendererDesc.fragmentShader = {emitter.billboardFragmentShader.data(), emitter.billboardFragmentShader.size()};
        rendererDesc.pickingFragmentShader = {emitter.billboardPickingFragmentShader.data(),
                                              emitter.billboardPickingFragmentShader.size()};
        rendererDesc.shaderProgram = shaderProgram;
        rendererDesc.instances = emitter.runtime->InstanceBuffer();
        rendererDesc.renderIndices = emitter.runtime->RenderIndexBuffer();
        rendererDesc.material = material;
        rendererDesc.fallbackMaterial = fallbackMaterial;
        rendererDesc.textureResolver = textureResolver;
        rendererDesc.textureVersionResolver = textureVersionResolver;
        rendererDesc.deletionQueue = deletionQueue;
        return renderer->Create(context->GetRhiDevice(), rendererDesc) ? renderer : nullptr;
    }

    [[nodiscard]] std::shared_ptr<Emitter> CreateEmitter(const GpuParticleEmitterProgram &program,
                                                         const std::shared_ptr<Emitter> &previous,
                                                         std::string *error) const
    {
        if (program.id == 0 || program.stableId.empty() || program.artifactRevision == 0 || program.capacity == 0 ||
            program.stateStride == 0) {
            SetError(error, "GPU particle program identity, revision, capacity, and state stride must be valid");
            return {};
        }
        for (const auto &kernel : program.kernels) {
            if (!IsSpirv(kernel)) {
                SetError(error, "GPU particle program contains invalid compute SPIR-V");
                return {};
            }
        }
        const bool needsLegacyBillboard =
            std::any_of(program.outputs.begin(), program.outputs.end(),
                        [](const GpuParticleOutputProgram &output) { return !output.shaderProgram; });
        if (needsLegacyBillboard &&
            (!IsSpirv(program.billboardVertexShader) || !IsSpirv(program.billboardFragmentShader))) {
            SetError(error, "GPU particle program contains invalid billboard SPIR-V");
            return {};
        }
        if (program.outputs.empty()) {
            SetError(error, "GPU particle program requires at least one rendering output");
            return {};
        }

        auto emitter = std::make_shared<Emitter>();
        emitter->id = program.id;
        emitter->artifactRevision = program.artifactRevision;
        emitter->stableId = program.stableId;
        emitter->statePreservedOnPublish = program.preserveState;
        emitter->billboardVertexShader = program.billboardVertexShader;
        emitter->billboardFragmentShader = program.billboardFragmentShader;
        emitter->billboardPickingFragmentShader = program.billboardPickingFragmentShader;
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
            (!previous || previous->id != program.id || previous->stableId != program.stableId)) {
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

        std::unordered_set<uint64_t> outputIds;
        std::unordered_set<std::string> outputStableIds;
        outputIds.reserve(program.outputs.size());
        outputStableIds.reserve(program.outputs.size());
        emitter->outputs.reserve(program.outputs.size());
        for (const auto &output : program.outputs) {
            if (output.id == 0 || output.stableId.empty() || !outputIds.insert(output.id).second ||
                !outputStableIds.insert(output.stableId).second) {
                SetError(error, "GPU particle output identity must be valid and unique per emitter");
                return {};
            }
            if (!output.semantics.IsValid()) {
                SetError(error, "GPU particle output '" + output.stableId +
                                    "' cannot receive shadows while scene lighting is disabled");
                return {};
            }
            if (output.semantics.sortMode != ParticleSortMode::None && !output.shaderProgram) {
                SetError(error, "GPU particle output '" + output.stableId +
                                    "' sorting requires a linked ParticleSprite material");
                return {};
            }
            if (output.semantics.sortMode != ParticleSortMode::None && (!sortProgram || !sortProgram->IsValid())) {
                SetError(error, "GPU particle sorting kernels are unavailable");
                return {};
            }
            if (output.shaderProgram && (!cullProgram || !cullProgram->IsValid())) {
                SetError(error, "GPU particle view-culling kernels are unavailable");
                return {};
            }
            auto renderer =
                CreateOutputRenderer(*emitter, output.material, output.fallbackMaterial, output.shaderProgram);
            if (!renderer) {
                SetError(error,
                         "failed to create GPU particle billboard renderer for output '" + output.stableId + "'");
                return {};
            }
            emitter->outputs.push_back({output.id, output.stableId, output.material, output.shaderProgram,
                                        output.fallbackMaterial, output.semantics, std::move(renderer)});
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

    [[nodiscard]] std::shared_ptr<GraphState> BuildGraph(const EmitterMap &candidateEmitters, std::string *error) const
    {
        auto state = std::make_shared<GraphState>();
        state->graph = std::make_unique<vk::RenderGraph>();
        state->graph->Initialize(context, pipelines, deletionQueue);
        state->schedulers.reserve(candidateEmitters.size());
        state->schedulerById.reserve(candidateEmitters.size());
        for (const auto &[id, emitter] : candidateEmitters) {
            auto scheduler = std::make_unique<ParticleRenderGraph>();
            const std::string prefix = "GpuParticle/" + std::to_string(id);
            if (!scheduler->Attach(*state->graph, *emitter->runtime, *emitter->bounds, prefix,
                                   emitter->migration.get())) {
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
                entry.capacity = emitter->runtime->Capacity();
                entry.instances = emitter->runtime->InstanceBuffer();
                entry.renderIndices = output.renderer->RenderIndexBuffer();
                entry.indirectArguments = emitter->runtime->IndirectBuffer();
                entry.bounds = emitter->bounds->BoundsBuffer();
                entry.renderer = output.renderer;
                entry.cullProgram = output.shaderProgram ? cullProgram : nullptr;
                entry.sortProgram = output.semantics.sortMode == ParticleSortMode::None ? nullptr : sortProgram;
                entry.semantics = output.semantics;
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
            deletionQueue->Push([oldGraph = std::move(oldGraph), oldEmitters = std::move(oldEmitters)]() mutable {
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
    const GpuParticleMigrationProgram &migrationProgram)
{
    if (!m_impl || m_impl->context || !context.IsValid() || !boundsProgram.IsValid() || !migrationProgram.IsValid())
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
    m_impl->graphState = m_impl->BuildGraph({}, nullptr);
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
    m_impl->graphState.reset();
    m_impl->emitters.clear();
    m_impl->pointCacheUploads.clear();
    m_impl->drawRegistry = nullptr;
    m_impl->textureResolver = {};
    m_impl->textureVersionResolver = {};
    m_impl->cullProgram.reset();
    m_impl->sortProgram.reset();
    m_impl->boundsProgram.reset();
    m_impl->migrationProgram.reset();
    m_impl->deletionQueue = nullptr;
    m_impl->pipelines = nullptr;
    m_impl->resources = nullptr;
    m_impl->context = nullptr;
}

bool ParticleGpuSystemManager::CreateOrReplace(const GpuParticleEmitterProgram &program, std::string *error)
{
    return CreateOrReplaceBatch({program}, error);
}

bool ParticleGpuSystemManager::CreateOrReplaceBatch(const std::vector<GpuParticleEmitterProgram> &programs,
                                                    std::string *error)
{
    return ApplyBatch(programs, {}, error);
}

bool ParticleGpuSystemManager::ApplyBatch(const std::vector<GpuParticleEmitterProgram> &programs,
                                          const std::vector<uint64_t> &removeIds, std::string *error)
{
    if (error)
        error->clear();
    if (!m_impl || !m_impl->context || !m_impl->drawRegistry) {
        SetError(error, "GPU particle manager is not initialized");
        return false;
    }
    if (programs.empty() && removeIds.empty()) {
        SetError(error, "GPU particle update batch cannot be empty");
        return false;
    }

    Impl::EmitterMap candidates = m_impl->emitters;
    std::unordered_set<uint64_t> batchIds;
    batchIds.reserve(programs.size() + removeIds.size());
    for (const uint64_t id : removeIds) {
        if (id == 0 || !batchIds.insert(id).second) {
            SetError(error, "GPU particle update batch contains invalid or duplicate removal ids");
            return false;
        }
        candidates.erase(id);
    }
    for (const auto &program : programs) {
        if (!batchIds.insert(program.id).second) {
            SetError(error, "GPU particle update batch contains duplicate or conflicting emitter ids");
            return false;
        }
        const auto previous = m_impl->emitters.find(program.id);
        auto emitter = m_impl->CreateEmitter(
            program, previous != m_impl->emitters.end() ? previous->second : std::shared_ptr<Impl::Emitter>{}, error);
        if (!emitter)
            return false;
        candidates[program.id] = std::move(emitter);
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
    m_impl->graphState = std::move(candidateGraph);
    m_impl->emitters = std::move(candidates);
    m_impl->Retire(std::move(oldGraph), std::move(oldEmitters));
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
        return std::any_of(entry.second->outputs.begin(), entry.second->outputs.end(),
                           [&](const auto &output) { return output.material.get() == material.get(); });
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
        std::shared_ptr<ParticleGpuBillboardRenderer> previousRenderer;
        std::shared_ptr<ParticleGpuBillboardRenderer> nextRenderer;
    };
    std::vector<Replacement> replacements;
    for (const auto &[id, emitter] : m_impl->emitters) {
        (void)id;
        for (auto &output : emitter->outputs) {
            if (output.material.get() != material.get())
                continue;
            const bool sameProgram =
                (!output.shaderProgram && !shaderProgram) ||
                (output.shaderProgram && shaderProgram && output.shaderProgram->key == shaderProgram->key);
            if (sameProgram)
                continue;
            if (output.semantics.sortMode != ParticleSortMode::None && !shaderProgram) {
                SetError(error,
                         "GPU particle sorted output '" + output.stableId + "' cannot use a legacy billboard material");
                return false;
            }
            auto renderer = m_impl->CreateOutputRenderer(*emitter, material, output.fallbackMaterial, shaderProgram);
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

    std::vector<std::shared_ptr<ParticleGpuBillboardRenderer>> retired;
    retired.reserve(replacements.size());
    for (auto &replacement : replacements)
        retired.push_back(std::move(replacement.previousRenderer));
    if (m_impl->deletionQueue) {
        m_impl->deletionQueue->Push([retired = std::move(retired)]() mutable { retired.clear(); });
    }
    return true;
}

bool ParticleGpuSystemManager::Remove(uint64_t id)
{
    if (!m_impl || !m_impl->context || id == 0 || m_impl->emitters.find(id) == m_impl->emitters.end())
        return false;
    Impl::EmitterMap candidates = m_impl->emitters;
    candidates.erase(id);
    auto candidateGraph = m_impl->BuildGraph(candidates, nullptr);
    if (!candidateGraph)
        return false;
    if (m_impl->drawRegistry && !m_impl->drawRegistry->Replace(m_impl->BuildDrawEntries(candidates)))
        return false;

    auto oldGraph = std::move(m_impl->graphState);
    auto oldEmitters = std::move(m_impl->emitters);
    m_impl->graphState = std::move(candidateGraph);
    m_impl->emitters = std::move(candidates);
    m_impl->Retire(std::move(oldGraph), std::move(oldEmitters));
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
    m_impl->graphState = std::move(candidateGraph);
    m_impl->emitters.clear();
    m_impl->Retire(std::move(oldGraph), std::move(oldEmitters));
}

bool ParticleGpuSystemManager::BeginFrame(uint64_t id, const GpuParticleFrameRequest &request,
                                          const GpuParticleTransforms &transforms)
{
    if (!m_impl || !m_impl->graphState)
        return false;
    const auto emitter = m_impl->emitters.find(id);
    const auto scheduler = m_impl->graphState->schedulerById.find(id);
    if (emitter == m_impl->emitters.end() || scheduler == m_impl->graphState->schedulerById.end())
        return false;
    (void)m_impl->RefreshDataInterfaces(*emitter->second);
    if (!emitter->second->runtime->UpdateTransforms(transforms))
        return false;
    return scheduler->second->BeginFrame(request);
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
    const bool hasPending = std::any_of(m_impl->graphState->schedulers.begin(), m_impl->graphState->schedulers.end(),
                                        [](const auto &scheduler) { return scheduler->HasPendingFrame(); });
    if (hasPending) {
        m_impl->graphState->graph->Execute(commandBuffer);
        m_impl->RetireCompletedMigrations();
    }
}

bool ParticleGpuSystemManager::Contains(uint64_t id) const
{
    return m_impl && m_impl->emitters.find(id) != m_impl->emitters.end();
}

size_t ParticleGpuSystemManager::Size() const
{
    return m_impl ? m_impl->emitters.size() : 0;
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

} // namespace infernux::particle
