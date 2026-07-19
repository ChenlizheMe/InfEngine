#include "ParticleGpuSystemManager.h"

#include "ParticleGpuDrawRegistry.h"

#include <function/renderer/FrameDeletionQueue.h>
#include <function/renderer/vk/RenderGraph.h>
#include <function/renderer/vk/VkDeviceContext.h>
#include <function/renderer/vk/VkPipelineManager.h>

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
        std::vector<uint32_t> billboardVertexShader;
        std::vector<uint32_t> billboardFragmentShader;
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
    FrameDeletionQueue *deletionQueue = nullptr;
    ParticleGpuDrawRegistry *drawRegistry = nullptr;
    GpuBillboardTextureResolver textureResolver;
    GpuBillboardTextureVersionResolver textureVersionResolver;
    std::shared_ptr<const GpuParticleCullProgramStorage> cullProgram;
    std::shared_ptr<const GpuParticleSortProgramStorage> sortProgram;
    std::shared_ptr<const GpuParticleBoundsProgramStorage> boundsProgram;
    EmitterMap emitters;
    std::shared_ptr<GraphState> graphState;

    [[nodiscard]] std::shared_ptr<ParticleGpuBillboardRenderer>
    CreateOutputRenderer(const Emitter &emitter, const std::shared_ptr<InxMaterial> &material,
                         const GpuBillboardMaterialState &fallbackMaterial,
                         const std::shared_ptr<const ShaderProgramArtifact> &shaderProgram) const
    {
        auto renderer = std::make_shared<ParticleGpuBillboardRenderer>();
        GpuBillboardRendererDesc rendererDesc;
        rendererDesc.vertexShader = {emitter.billboardVertexShader.data(), emitter.billboardVertexShader.size()};
        rendererDesc.fragmentShader = {emitter.billboardFragmentShader.data(), emitter.billboardFragmentShader.size()};
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
        emitter->runtime = std::make_unique<ParticleGpuRuntime>();

        GpuEmitterDesc runtimeDesc;
        runtimeDesc.capacity = program.capacity;
        runtimeDesc.stateStride = program.stateStride;
        for (size_t index = 0; index < program.kernels.size(); ++index)
            runtimeDesc.kernels[index] = {program.kernels[index].data(), program.kernels[index].size()};
        auto &device = context->GetRhiDevice();
        if (program.preserveState &&
            (!previous || previous->id != program.id || previous->stableId != program.stableId)) {
            SetError(error, "GPU particle state preservation requires the same live emitter identity");
            return {};
        }
        const bool runtimeCreated = program.preserveState
                                        ? emitter->runtime->CreateCompatible(device, runtimeDesc, *previous->runtime)
                                        : emitter->runtime->Create(device, runtimeDesc);
        if (!runtimeCreated) {
            SetError(error, program.preserveState
                                ? "GPU particle state ABI is incompatible with the requested hot reload"
                                : "failed to create GPU particle simulation runtime");
            return {};
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
            if (!scheduler->Attach(*state->graph, *emitter->runtime, *emitter->bounds, prefix)) {
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
};

ParticleGpuSystemManager::ParticleGpuSystemManager() : m_impl(std::make_unique<Impl>())
{
}

ParticleGpuSystemManager::~ParticleGpuSystemManager()
{
    Shutdown();
}

bool ParticleGpuSystemManager::Initialize(vk::VkDeviceContext &context, vk::VkPipelineManager &pipelines,
                                          FrameDeletionQueue &deletionQueue, ParticleGpuDrawRegistry &drawRegistry,
                                          GpuBillboardTextureResolver textureResolver,
                                          GpuBillboardTextureVersionResolver textureVersionResolver,
                                          const GpuParticleSortProgram &sortProgram,
                                          const GpuParticleCullProgram &cullProgram,
                                          const GpuParticleBoundsProgram &boundsProgram)
{
    if (!m_impl || m_impl->context || !context.IsValid() || !boundsProgram.IsValid())
        return false;
    m_impl->context = &context;
    m_impl->pipelines = &pipelines;
    m_impl->deletionQueue = &deletionQueue;
    m_impl->drawRegistry = &drawRegistry;
    m_impl->textureResolver = std::move(textureResolver);
    m_impl->textureVersionResolver = std::move(textureVersionResolver);
    auto boundsStorage = std::make_shared<GpuParticleBoundsProgramStorage>();
    if (!boundsStorage->Assign(boundsProgram)) {
        Shutdown();
        return false;
    }
    m_impl->boundsProgram = std::move(boundsStorage);
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
    m_impl->drawRegistry = nullptr;
    m_impl->textureResolver = {};
    m_impl->textureVersionResolver = {};
    m_impl->cullProgram.reset();
    m_impl->sortProgram.reset();
    m_impl->boundsProgram.reset();
    m_impl->deletionQueue = nullptr;
    m_impl->pipelines = nullptr;
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
    if (emitter == m_impl->emitters.end() || scheduler == m_impl->graphState->schedulerById.end() ||
        !emitter->second->runtime->UpdateTransforms(transforms))
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
    if (hasPending)
        m_impl->graphState->graph->Execute(commandBuffer);
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
