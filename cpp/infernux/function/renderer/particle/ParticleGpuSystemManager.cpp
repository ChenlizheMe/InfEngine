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
            std::shared_ptr<ParticleGpuBillboardRenderer> renderer;
        };

        uint64_t id = 0;
        uint64_t artifactRevision = 0;
        std::string stableId;
        std::unique_ptr<ParticleGpuRuntime> runtime;
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
    EmitterMap emitters;
    std::shared_ptr<GraphState> graphState;

    [[nodiscard]] std::shared_ptr<Emitter> CreateEmitter(const GpuParticleEmitterProgram &program,
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
        if (!IsSpirv(program.billboardVertexShader) || !IsSpirv(program.billboardFragmentShader)) {
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
        emitter->runtime = std::make_unique<ParticleGpuRuntime>();

        GpuEmitterDesc runtimeDesc;
        runtimeDesc.capacity = program.capacity;
        runtimeDesc.stateStride = program.stateStride;
        for (size_t index = 0; index < program.kernels.size(); ++index)
            runtimeDesc.kernels[index] = {program.kernels[index].data(), program.kernels[index].size()};
        auto &device = context->GetRhiDevice();
        if (!emitter->runtime->Create(device, runtimeDesc)) {
            SetError(error, "failed to create GPU particle simulation runtime");
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
            auto renderer = std::make_shared<ParticleGpuBillboardRenderer>();
            GpuBillboardRendererDesc rendererDesc;
            rendererDesc.vertexShader = {program.billboardVertexShader.data(), program.billboardVertexShader.size()};
            rendererDesc.fragmentShader = {program.billboardFragmentShader.data(),
                                           program.billboardFragmentShader.size()};
            rendererDesc.instances = emitter->runtime->InstanceBuffer();
            rendererDesc.material = output.material;
            if (!renderer->Create(device, rendererDesc)) {
                SetError(error,
                         "failed to create GPU particle billboard renderer for output '" + output.stableId + "'");
                return {};
            }
            emitter->outputs.push_back({output.id, output.stableId, std::move(renderer)});
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
            if (!scheduler->Attach(*state->graph, *emitter->runtime, prefix)) {
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

    [[nodiscard]] static std::vector<GpuParticleDrawEntry> BuildDrawEntries(const EmitterMap &candidateEmitters)
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
                entries.push_back({output.id, emitter->runtime->Capacity(), emitter->runtime->InstanceBuffer(),
                                   emitter->runtime->IndirectBuffer(), output.renderer});
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
                                          FrameDeletionQueue &deletionQueue, ParticleGpuDrawRegistry &drawRegistry)
{
    if (!m_impl || m_impl->context || !context.IsValid())
        return false;
    m_impl->context = &context;
    m_impl->pipelines = &pipelines;
    m_impl->deletionQueue = &deletionQueue;
    m_impl->drawRegistry = &drawRegistry;
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
        auto emitter = m_impl->CreateEmitter(program, error);
        if (!emitter)
            return false;
        candidates[program.id] = std::move(emitter);
    }
    auto candidateGraph = m_impl->BuildGraph(candidates, error);
    if (!candidateGraph)
        return false;
    if (!m_impl->drawRegistry->Replace(Impl::BuildDrawEntries(candidates))) {
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

bool ParticleGpuSystemManager::Remove(uint64_t id)
{
    if (!m_impl || !m_impl->context || id == 0 || m_impl->emitters.find(id) == m_impl->emitters.end())
        return false;
    Impl::EmitterMap candidates = m_impl->emitters;
    candidates.erase(id);
    auto candidateGraph = m_impl->BuildGraph(candidates, nullptr);
    if (!candidateGraph)
        return false;
    if (m_impl->drawRegistry && !m_impl->drawRegistry->Replace(Impl::BuildDrawEntries(candidates)))
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

size_t ParticleGpuSystemManager::ActiveOutputCount(uint64_t id) const
{
    if (!m_impl)
        return 0;
    const auto found = m_impl->emitters.find(id);
    return found != m_impl->emitters.end() ? found->second->outputs.size() : 0;
}

} // namespace infernux::particle
