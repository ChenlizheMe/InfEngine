#include "MaterialPipelineManager.h"
#include "InxRenderStruct.h"
#include "MsaaPolicy.h"
#include "VertexInputFilter.h"
#include "vk/MaterialRenderStateVulkan.h"
#include "vk/RhiVulkanTypes.h"
#include "vk/VkPipelineHelpers.h"
#include "vk/VkRenderUtils.h"
#include <algorithm>
#include <array>
#include <core/log/InxLog.h>
#include <platform/filesystem/InxPath.h>
#include <stdexcept>

namespace infernux
{

namespace
{

/// Clear all Forward-pass Vulkan handles on a material to prevent stale references.
void ClearForwardPassHandles(InxMaterial *material)
{
    material->SetPassPipeline(ShaderCompileTarget::Forward, VK_NULL_HANDLE);
    material->SetPassPipelineLayout(ShaderCompileTarget::Forward, VK_NULL_HANDLE);
    material->SetPassDescriptorSet(ShaderCompileTarget::Forward, VK_NULL_HANDLE);
    material->SetPassShaderProgram(ShaderCompileTarget::Forward, nullptr);
}

void HashCombine(size_t &hash, size_t value)
{
    hash ^= value + static_cast<size_t>(0x9e3779b97f4a7c15ull) + (hash << 6u) + (hash >> 2u);
}

} // namespace

size_t MaterialPassRenderDataKeyHash::operator()(const MaterialPassRenderDataKey &key) const noexcept
{
    size_t hash = std::hash<std::string>{}(key.materialKey);
    HashCombine(hash, ShaderProgramVariantKeyHash{}(key.programKey));
    HashCombine(hash, MaterialPassPipelineDescriptorHash{}(key.pipeline));
    HashCombine(hash, key.renderStateHash);
    return hash;
}

void MaterialPipelineManager::SyncMaterialForwardPass(InxMaterial *material, VkPipeline pipeline,
                                                      VkPipelineLayout layout, VkDescriptorSet descSet,
                                                      ShaderProgramPublication program)
{
    material->SetPassPipeline(ShaderCompileTarget::Forward, pipeline);
    material->SetPassPipelineLayout(ShaderCompileTarget::Forward, layout);
    material->SetPassDescriptorSet(ShaderCompileTarget::Forward, descSet);
    material->SetPassShaderProgram(ShaderCompileTarget::Forward, std::move(program));
    material->ClearPipelineDirty();
}

bool MaterialPipelineManager::IsPipelineSharedByOthers(const std::string &excludeName, VkPipeline pipeline) const
{
    for (const auto &[name, data] : m_renderDataMap) {
        if (name == excludeName || !data)
            continue;
        if (data->pipeline == pipeline)
            return true;
    }
    for (const auto &[key, data] : m_passRenderDataMap) {
        (void)key;
        if (data && data->pipeline == pipeline)
            return true;
    }
    return false;
}

void MaterialPipelineManager::DestroyNonForwardPipelines(InxMaterial *material, bool deferred)
{
    for (int i = 1; i < static_cast<int>(ShaderCompileTarget::Count); ++i) {
        const auto pass = static_cast<ShaderCompileTarget>(i);
        // Shadow pipelines are owned by the shadow pipeline cache in
        // InxVkCoreModular — only clear the material's reference here.
        if (pass == ShaderCompileTarget::Shadow) {
            material->SetPassPipeline(pass, VK_NULL_HANDLE);
            material->SetPassPipelineLayout(pass, VK_NULL_HANDLE);
            material->SetPassDescriptorSet(pass, VK_NULL_HANDLE);
            material->SetPassShaderProgram(pass, nullptr);
            continue;
        }
        VkPipeline pp = material->GetPassPipeline(pass);
        if (pp != VK_NULL_HANDLE) {
            if (deferred && m_deletionQueue) {
                const VkDevice device = m_device;
                m_deletionQueue->Retire([device, pp] { vkDestroyPipeline(device, pp, nullptr); });
            } else {
                vkDestroyPipeline(m_device, pp, nullptr);
            }
            material->SetPassPipeline(pass, VK_NULL_HANDLE);
        }
        material->SetPassPipelineLayout(pass, VK_NULL_HANDLE);
        material->SetPassShaderProgram(pass, nullptr);
    }
}

MaterialPipelineManager::~MaterialPipelineManager()
{
    Shutdown();
}

void MaterialPipelineManager::Initialize(VmaAllocator allocator, VkDevice device, VkPhysicalDevice physicalDevice,
                                         VkFormat colorFormat, VkFormat depthFormat, VkSampleCountFlagBits sampleCount,
                                         ShaderProgramCache &shaderProgramCache, GpuRetirementQueue *deletionQueue,
                                         bool descriptorIndexingEnabled, vk::VkDescriptorManager *descriptorManager,
                                         uint64_t shaderDeviceContractKey)
{
    ++m_publicationGeneration;
    m_device = device;
    m_allocator = allocator;
    m_physicalDevice = physicalDevice;
    m_colorFormat = colorFormat;
    m_depthFormat = depthFormat;
    m_sampleCount = sampleCount;
    m_shaderProgramCache = &shaderProgramCache;
    m_shaderDeviceContractKey = shaderDeviceContractKey;
    m_deletionQueue = deletionQueue;

    // Initialize shader program cache
    m_shaderProgramCache->Initialize(device);
    m_shaderProgramCache->SetDeviceContractKey(m_shaderDeviceContractKey);

    // Plumb the descriptor-indexing decision through to BOTH the layouts
    // (created lazily by ShaderProgram::CreateDescriptorSetLayouts on shader
    // load) AND the pool (created here in Initialize). Setting the static
    // flag before Initialize() guarantees the very first ShaderProgram and
    // the descriptor pool agree on whether UPDATE_AFTER_BIND is on, which
    // Vulkan validation requires.
    ShaderProgram::SetUpdateAfterBindEnabled(descriptorIndexingEnabled);
    m_descriptorManager.SetUpdateAfterBindEnabled(descriptorIndexingEnabled);
    m_descriptorManager.Initialize(allocator, device, physicalDevice, descriptorManager);
    m_descriptorManager.SetRetirementQueue(deletionQueue);

    // Create Vulkan pipeline cache for faster recreation
    VkPipelineCacheCreateInfo cacheCreateInfo{};
    cacheCreateInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_CACHE_CREATE_INFO;
    if (vkCreatePipelineCache(m_device, &cacheCreateInfo, nullptr, &m_vkPipelineCache) != VK_SUCCESS) {
        INXLOG_WARN("Failed to create VkPipelineCache, pipeline recreation may be slower");
        m_vkPipelineCache = VK_NULL_HANDLE;
    }
}

void MaterialPipelineManager::Shutdown(bool skipWaitIdle)
{
    if (m_device == VK_NULL_HANDLE) {
        return;
    }

    ++m_publicationGeneration;

    if (!skipWaitIdle) {
        vkDeviceWaitIdle(m_device);
    }

    // Shutdown descriptor manager
    m_descriptorManager.Shutdown();

    // Shutdown shader program cache
    if (m_shaderProgramCache) {
        m_shaderProgramCache->Shutdown();
        m_shaderProgramCache = nullptr;
    }

    m_passRenderDataMap.clear();

    // Destroy all pipelines
    for (auto &[hash, pipeline] : m_pipelineCache) {
        if (pipeline != VK_NULL_HANDLE) {
            vkDestroyPipeline(m_device, pipeline, nullptr);
        }
    }
    m_pipelineCache.clear();

    // Clear stale Vulkan handles on material objects before dropping render data.
    // Otherwise materials may keep dangling pipeline/layout/descriptor handles
    // and attempt to use them on subsequent frames after MSAA reinitialization.
    for (auto &[name, data] : m_renderDataMap) {
        (void)name;
        if (!data || !data->material) {
            continue;
        }
        DestroyNonForwardPipelines(data->material.get());
        data->material->CleanupUBO(m_device);
        data->material->ClearAllPassPipelines();
    }

    // Clear render data. Shader modules stored in MaterialRenderData are
    // shallow copies of the handles owned by ShaderProgram, which were
    // already destroyed by ShaderProgramCache::Shutdown() above — do NOT
    // destroy them again here (double-free).
    m_renderDataMap.clear();
    m_failedForwardPipelineHashes.clear();

    // Destroy Vulkan pipeline cache
    if (m_vkPipelineCache != VK_NULL_HANDLE) {
        vkDestroyPipelineCache(m_device, m_vkPipelineCache, nullptr);
        m_vkPipelineCache = VK_NULL_HANDLE;
    }

    m_passRenderDataMap.clear();

    m_defaultRenderData = nullptr;
    m_device = VK_NULL_HANDLE;
    m_allocator = VK_NULL_HANDLE;
    m_physicalDevice = VK_NULL_HANDLE;
    m_deletionQueue = nullptr;
}

bool MaterialPipelineManager::ReconfigureSampleCount(VkSampleCountFlagBits sampleCount)
{
    if (sampleCount == m_sampleCount)
        return true;
    if (m_device == VK_NULL_HANDLE || !m_deletionQueue)
        throw std::logic_error("Material pipeline MSAA reconfiguration requires an initialized retirement queue");

    struct PreparedGeneration
    {
        std::unordered_map<size_t, VkPipeline> pipelines;
        std::unordered_map<std::string, std::pair<VkPipeline, size_t>> forward;
        std::unordered_map<MaterialPassRenderDataKey, std::unique_ptr<MaterialPassRenderData>,
                           MaterialPassRenderDataKeyHash>
            passes;
    } prepared;

    const auto destroyPrepared = [&]() {
        for (const auto &[hash, pipeline] : prepared.pipelines) {
            (void)hash;
            if (pipeline != VK_NULL_HANDLE)
                vkDestroyPipeline(m_device, pipeline, nullptr);
        }
        prepared.pipelines.clear();
    };

    const MaterialPassPipelineDescriptor newDefault =
        GetDefaultPassPipelineDescriptorFor(sampleCount, ShaderCompileTarget::Forward);
    auto createOrReusePipeline = [&](size_t hash, const ShaderProgram *program, const RenderState &state,
                                     const MaterialPassPipelineDescriptor &pipeline) -> VkPipeline {
        auto cached = prepared.pipelines.find(hash);
        if (cached != prepared.pipelines.end())
            return cached->second;
        VkPipeline created = CreatePipelineWithProgram(program, state, pipeline);
        if (created != VK_NULL_HANDLE)
            prepared.pipelines.emplace(hash, created);
        return created;
    };

    for (const auto &[name, data] : m_renderDataMap) {
        if (!data || !data->isValid || !data->material || !data->shaderProgram)
            continue;
        size_t hash = data->material->GetPipelineHash();
        hash = FoldPassPipelineHash(
            hash, newDefault,
            ShaderProgramVariantKey{data->programKey, ShaderCompileTarget::Forward, m_shaderDeviceContractKey});
        const VkPipeline pipeline =
            createOrReusePipeline(hash, data->shaderProgram.get(), data->material->GetRenderState(), newDefault);
        if (pipeline == VK_NULL_HANDLE) {
            INXLOG_ERROR("MaterialPipelineManager: failed to prepare ", static_cast<int>(sampleCount),
                         "x MSAA pipeline generation for material '", name, "'");
            destroyPrepared();
            return false;
        }
        prepared.forward.emplace(name, std::make_pair(pipeline, hash));
    }

    const rhi::SampleCount oldSamples = ToRhiSampleCount(static_cast<int>(m_sampleCount));
    const rhi::SampleCount newSamples = ToRhiSampleCount(static_cast<int>(sampleCount));
    for (const auto &[oldKey, oldData] : m_passRenderDataMap) {
        if (!oldData || !oldData->isValid || !oldData->material || !oldData->shaderProgram)
            continue;

        MaterialPassPipelineDescriptor pipeline = oldKey.pipeline;
        const bool followsSceneSamples =
            pipeline.target == ShaderCompileTarget::Forward || pipeline.target == ShaderCompileTarget::ForwardPlus ||
            pipeline.target == ShaderCompileTarget::GBuffer || pipeline.target == ShaderCompileTarget::Depth ||
            pipeline.target == ShaderCompileTarget::Normal || pipeline.target == ShaderCompileTarget::BaseColor;
        if (followsSceneSamples && pipeline.samples == oldSamples)
            pipeline.samples = newSamples;

        MaterialPassRenderDataKey key = oldKey;
        key.pipeline = pipeline;
        auto data = std::make_unique<MaterialPassRenderData>(*oldData);
        data->key = key;
        data->pipelineHash = FoldPassPipelineHash(data->material->GetPipelineHash(), pipeline, key.programKey);
        data->pipeline = createOrReusePipeline(data->pipelineHash, data->shaderProgram.get(),
                                               data->material->GetRenderState(), pipeline);
        if (data->pipeline == VK_NULL_HANDLE) {
            INXLOG_ERROR("MaterialPipelineManager: failed to prepare ", static_cast<int>(sampleCount),
                         "x semantic pass generation for material '", key.materialKey, "'");
            destroyPrepared();
            return false;
        }
        prepared.passes.emplace(std::move(key), std::move(data));
    }

    std::unordered_set<VkPipeline> retiredPipelines;
    retiredPipelines.reserve(m_renderDataMap.size() + m_passRenderDataMap.size());
    for (const auto &[name, data] : m_renderDataMap) {
        if (data && data->pipeline != VK_NULL_HANDLE && prepared.forward.find(name) != prepared.forward.end())
            retiredPipelines.insert(data->pipeline);
    }
    for (const auto &[key, data] : m_passRenderDataMap) {
        (void)key;
        if (data && data->pipeline != VK_NULL_HANDLE)
            retiredPipelines.insert(data->pipeline);
    }

    m_sampleCount = sampleCount;

    for (auto &[name, replacement] : prepared.forward) {
        auto existing = m_renderDataMap.find(name);
        if (existing == m_renderDataMap.end() || !existing->second)
            continue;
        auto &data = *existing->second;
        data.pipeline = replacement.first;
        data.pipelineHash = replacement.second;
        SyncMaterialForwardPass(data.material.get(), data.pipeline, data.pipelineLayout, data.descriptorSet,
                                data.shaderProgram);
    }
    m_passRenderDataMap = std::move(prepared.passes);
    m_failedForwardPipelineHashes.clear();
    for (auto &[hash, pipeline] : prepared.pipelines)
        m_pipelineCache[hash] = pipeline;
    prepared.pipelines.clear();

    for (VkPipeline pipeline : retiredPipelines)
        RetirePipelineIfUnreferenced(pipeline);

    ++m_publicationGeneration;

    return true;
}

VkShaderModule MaterialPipelineManager::CreateShaderModule(const std::vector<char> &code)
{
    if (code.empty()) {
        INXLOG_ERROR("Cannot create shader module from empty code");
        return VK_NULL_HANDLE;
    }

    VkShaderModuleCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;
    createInfo.codeSize = code.size();
    createInfo.pCode = reinterpret_cast<const uint32_t *>(code.data());

    VkShaderModule shaderModule;
    if (vkCreateShaderModule(m_device, &createInfo, nullptr, &shaderModule) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create shader module");
        return VK_NULL_HANDLE;
    }

    return shaderModule;
}

MaterialRenderData *MaterialPipelineManager::GetRenderData(const std::string &materialName)
{
    auto it = m_renderDataMap.find(materialName);
    if (it != m_renderDataMap.end() && it->second->isValid) {
        return it->second.get();
    }
    return nullptr;
}

bool MaterialPipelineManager::HasRenderData(const std::string &materialName) const
{
    auto it = m_renderDataMap.find(materialName);
    return it != m_renderDataMap.end() && it->second->isValid;
}

MaterialRenderData *MaterialPipelineManager::GetDefaultRenderData()
{
    return m_defaultRenderData;
}

MaterialPassPipelineDescriptor
MaterialPipelineManager::GetDefaultPassPipelineDescriptor(ShaderCompileTarget target) const
{
    return GetDefaultPassPipelineDescriptorFor(m_sampleCount, target);
}

MaterialPassPipelineDescriptor
MaterialPipelineManager::GetDefaultPassPipelineDescriptorFor(VkSampleCountFlagBits sampleCount,
                                                             ShaderCompileTarget target) const
{
    MaterialPassPipelineDescriptor pipeline;
    pipeline.target = target;
    pipeline.samples = ToRhiSampleCount(static_cast<int>(sampleCount));
    pipeline.depthFormat =
        m_depthFormat == VK_FORMAT_UNDEFINED ? rhi::PixelFormat::Undefined : rhi::FromVkFormat(m_depthFormat);
    switch (target) {
    case ShaderCompileTarget::Depth:
    case ShaderCompileTarget::Shadow:
        break;
    case ShaderCompileTarget::Picking:
        pipeline.colorFormats = {rhi::PixelFormat::RG32UInt};
        pipeline.samples = rhi::SampleCount::One;
        break;
    case ShaderCompileTarget::Forward:
    case ShaderCompileTarget::ForwardPlus:
        pipeline.colorFormats = {rhi::FromVkFormat(m_colorFormat)};
        break;
    case ShaderCompileTarget::GBuffer:
        pipeline.colorFormats = {
            rhi::PixelFormat::RGBA16SFloat, rhi::PixelFormat::RGBA16SFloat, rhi::PixelFormat::RGBA8UNorm,
            rhi::PixelFormat::RGBA16SFloat, rhi::PixelFormat::RG32UInt,
        };
        pipeline.samples = rhi::SampleCount::One;
        break;
    case ShaderCompileTarget::Motion:
        pipeline.colorFormats = {rhi::PixelFormat::RG16SFloat};
        pipeline.samples = rhi::SampleCount::One;
        break;
    case ShaderCompileTarget::Normal:
    case ShaderCompileTarget::BaseColor:
        pipeline.colorFormats = {rhi::PixelFormat::RGBA16SFloat};
        break;
    case ShaderCompileTarget::Count:
        break;
    }
    return pipeline;
}

bool MaterialPipelineManager::IsMaterialDescriptorSetCompatible(const ShaderProgram &forward, const ShaderProgram &pass)
{
    std::vector<const MergedDescriptorBinding *> forwardBindings;
    std::vector<const MergedDescriptorBinding *> passBindings;
    for (const auto &binding : forward.GetDescriptorBindings()) {
        if (binding.set == 0)
            forwardBindings.push_back(&binding);
    }
    for (const auto &binding : pass.GetDescriptorBindings()) {
        if (binding.set == 0)
            passBindings.push_back(&binding);
    }
    if (forwardBindings.size() != passBindings.size())
        return false;
    for (size_t i = 0; i < forwardBindings.size(); ++i) {
        const auto &lhs = *forwardBindings[i];
        const auto &rhs = *passBindings[i];
        if (lhs.binding != rhs.binding || lhs.type != rhs.type || lhs.descriptorCount != rhs.descriptorCount ||
            lhs.stageFlags != rhs.stageFlags || lhs.name != rhs.name)
            return false;
    }
    return true;
}

size_t MaterialPipelineManager::FoldPassPipelineHash(size_t baseHash, const MaterialPassPipelineDescriptor &pipeline,
                                                     const ShaderProgramVariantKey &programKey)
{
    HashCombine(baseHash, MaterialPassPipelineDescriptorHash{}(pipeline));
    HashCombine(baseHash, ShaderProgramVariantKeyHash{}(programKey));
    return baseHash;
}

MaterialPassRenderData *
MaterialPipelineManager::GetOrCreatePassRenderData(std::shared_ptr<InxMaterial> material,
                                                   ShaderProgramPublication programPublication,
                                                   const MaterialPassPipelineDescriptor &pipeline)
{
    if (!programPublication)
        return nullptr;
    const ShaderProgram &program = *programPublication;
    if (!material || !pipeline.IsValid() || program.GetCompileTarget() != pipeline.target)
        return nullptr;

    MaterialRenderData *forward = GetRenderData(material->GetMaterialKey());
    if (!forward || !forward->isValid || !forward->shaderProgram || forward->descriptorSet == VK_NULL_HANDLE ||
        !IsDescriptorSetLive(forward->descriptorSet))
        return nullptr;
    if (!IsMaterialDescriptorSetCompatible(*forward->shaderProgram, program)) {
        INXLOG_ERROR("Material pass '", ShaderCompileTargetName(pipeline.target), "' changed set-0 ABI for '",
                     material->GetName(), "'; linked variants must preserve the Forward material layout");
        return nullptr;
    }

    const ShaderProgramVariantKey programKey = program.GetVariantKey();
    MaterialPassRenderDataKey key{material->GetMaterialKey(), programKey, pipeline, material->GetRenderState().Hash()};
    auto existing = m_passRenderDataMap.find(key);
    if (existing != m_passRenderDataMap.end() && existing->second && existing->second->isValid) {
        auto &cached = *existing->second;
        if (cached.shaderProgram.get() == &program && cached.pipelineLayout == program.GetPipelineLayout()) {
            // Pass pipelines borrow set 0 from the Forward generation. Refresh
            // that borrowed handle on every cache hit so an unusual invalidation
            // order cannot resurrect a retired descriptor.
            cached.material = material;
            cached.descriptorSet = forward->descriptorSet;
            return &cached;
        }

        const VkPipeline stalePipeline = cached.pipeline;
        m_passRenderDataMap.erase(existing);
        RetirePipelineIfUnreferenced(stalePipeline);
    }

    auto data = std::make_unique<MaterialPassRenderData>();
    data->key = key;
    data->material = material;
    data->shaderProgram = std::move(programPublication);
    data->pipelineLayout = program.GetPipelineLayout();
    data->descriptorSet = forward->descriptorSet;
    data->pipelineHash = FoldPassPipelineHash(material->GetPipelineHash(), pipeline, programKey);

    data->pipeline = GetCachedPipeline(data->pipelineHash);
    if (data->pipeline == VK_NULL_HANDLE) {
        data->pipeline = CreatePipelineWithProgram(&program, material->GetRenderState(), pipeline);
        if (data->pipeline == VK_NULL_HANDLE)
            return nullptr;
        m_pipelineCache[data->pipelineHash] = data->pipeline;
    }
    data->isValid = true;

    MaterialPassRenderData *result = data.get();
    m_passRenderDataMap.emplace(std::move(key), std::move(data));
    return result;
}

VkPipeline MaterialPipelineManager::GetCachedPipeline(size_t pipelineHash) const
{
    auto it = m_pipelineCache.find(pipelineHash);
    if (it != m_pipelineCache.end()) {
        return it->second;
    }
    return VK_NULL_HANDLE;
}

// ============================================================================
// New Shader Reflection API
// ============================================================================

MaterialRenderData *MaterialPipelineManager::GetOrCreateRenderDataWithReflection(
    std::shared_ptr<InxMaterial> material, const std::vector<char> &vertShaderCode,
    const std::vector<char> &fragShaderCode, const ShaderProgramKey &programKey)
{
    if (!material) {
        INXLOG_ERROR("Cannot create render data for null material");
        return nullptr;
    }

    const std::string name = material->GetMaterialKey();
    size_t currentHash = material->GetPipelineHash();
    currentHash = FoldPassPipelineHash(
        currentHash, GetDefaultPassPipelineDescriptor(),
        ShaderProgramVariantKey{programKey, ShaderCompileTarget::Forward, m_shaderDeviceContractKey});

    // A changed configuration is prepared transactionally below. The current
    // generation stays active until both the new pipeline and descriptor set
    // have been created successfully.
    auto it = m_renderDataMap.find(name);
    MaterialRenderData *lastKnownGood = nullptr;
    if (it != m_renderDataMap.end()) {
        if (it->second->isValid) {
            if (it->second->pipelineHash == currentHash) {
                // Sync Vulkan handles to the (possibly new) material object.
                // This is critical when the default material is replaced
                // with a freshly-deserialized one that has null handles.
                SyncMaterialForwardPass(material.get(), it->second->pipeline, it->second->pipelineLayout,
                                        it->second->descriptorSet, it->second->shaderProgram);
                it->second->material = material; // update cached reference
                return it->second.get();
            }
            lastKnownGood = it->second.get();
            const auto failed = m_failedForwardPipelineHashes.find(name);
            if (failed != m_failedForwardPipelineHashes.end() && failed->second == currentHash) {
                SyncMaterialForwardPass(material.get(), lastKnownGood->pipeline, lastKnownGood->pipelineLayout,
                                        lastKnownGood->descriptorSet, lastKnownGood->shaderProgram);
                lastKnownGood->material = material;
                return lastKnownGood;
            }
            INXLOG_INFO("Material '", name, "' config changed, preparing replacement pipeline");
        } else {
            // Render data exists but is invalid (previous creation attempt failed).
            // Only retry if the material config actually changed (user might have fixed it).
            if (it->second->pipelineHash == currentHash) {
                // Config unchanged since last failure — don't spam retries every frame.
                return nullptr;
            }
            INXLOG_INFO("Material '", name, "' config changed after failure, retrying pipeline creation");
        }
    }

    const auto keepLastKnownGood = [&]() -> MaterialRenderData * {
        m_failedForwardPipelineHashes[name] = currentHash;
        if (!lastKnownGood) {
            auto failed = m_renderDataMap.find(name);
            if (failed == m_renderDataMap.end()) {
                auto failedData = std::make_unique<MaterialRenderData>();
                failedData->material = material;
                failedData->pipelineHash = currentHash;
                failedData->programKey = programKey;
                failedData->isValid = false;
                m_renderDataMap[name] = std::move(failedData);
            } else {
                failed->second->material = material;
                failed->second->pipelineHash = currentHash;
                failed->second->programKey = programKey;
                failed->second->isValid = false;
            }
            ClearForwardPassHandles(material.get());
            return nullptr;
        }
        SyncMaterialForwardPass(material.get(), lastKnownGood->pipeline, lastKnownGood->pipelineLayout,
                                lastKnownGood->descriptorSet, lastKnownGood->shaderProgram);
        lastKnownGood->material = material;
        return lastKnownGood;
    };

    // Get or create shader program (with reflection)
    ShaderProgramPublication program =
        m_shaderProgramCache->GetOrCreateProgram(programKey, vertShaderCode, fragShaderCode);
    if (!program || !program->IsValid()) {
        INXLOG_ERROR("Failed to get shader program for material: ", name);
        return keepLastKnownGood();
    }

    // Create new render data
    auto renderData = std::make_unique<MaterialRenderData>();
    renderData->material = material;
    renderData->pipelineHash = currentHash;
    renderData->programKey = programKey;
    renderData->shaderProgram = program;
    renderData->vertModule = program->GetVertexModule();
    renderData->fragModule = program->GetFragmentModule();
    renderData->pipelineLayout = program->GetPipelineLayout();

    // Build the pipeline before touching the descriptor cache. Replacing a
    // descriptor retires the previous set, so descriptor mutation cannot be
    // allowed to precede a pipeline creation that may fail.
    size_t pipelineKey = renderData->pipelineHash;
    VkPipeline cachedPipeline = GetCachedPipeline(pipelineKey);

    if (cachedPipeline != VK_NULL_HANDLE) {
        renderData->pipeline = cachedPipeline;
        renderData->isValid = true;
    } else {
        // Create new pipeline using shader program
        renderData->pipeline = CreatePipelineWithProgram(program.get(), material->GetRenderState());

        if (renderData->pipeline == VK_NULL_HANDLE) {
            INXLOG_ERROR("Failed to create pipeline for material: ", name);
            return keepLastKnownGood();
        }

        renderData->isValid = true;

        // Cache the pipeline — guard against hash collisions that would
        // silently overwrite (and leak) a different VkPipeline handle.
        auto cacheIt = m_pipelineCache.find(pipelineKey);
        if (cacheIt != m_pipelineCache.end() && cacheIt->second != renderData->pipeline) {
            if (!IsPipelineSharedByOthers("", cacheIt->second)) {
                const VkPipeline stalePipeline = cacheIt->second;
                if (m_deletionQueue) {
                    const VkDevice device = m_device;
                    m_deletionQueue->Retire(
                        [device, stalePipeline] { vkDestroyPipeline(device, stalePipeline, nullptr); });
                } else {
                    vkDestroyPipeline(m_device, stalePipeline, nullptr);
                }
            }
        }
        m_pipelineCache[pipelineKey] = renderData->pipeline;
    }

    renderData->materialDescSet = m_descriptorManager.GetOrCreateDescriptorSet(*material, *program);
    if (!renderData->materialDescSet || renderData->materialDescSet->descriptorSet == VK_NULL_HANDLE) {
        INXLOG_ERROR("Failed to create descriptor set for material: ", name);
        return keepLastKnownGood();
    }
    renderData->descriptorSet = renderData->materialDescSet->descriptorSet;

    // Commit only after the full replacement is valid. Semantic pass entries
    // borrow Forward set 0 and are invalidated at this same commit point.
    RemovePassRenderData(name);
    m_failedForwardPipelineHashes.erase(name);
    SyncMaterialForwardPass(material.get(), renderData->pipeline, renderData->pipelineLayout, renderData->descriptorSet,
                            program);

    MaterialRenderData *result = renderData.get();
    m_renderDataMap[name] = std::move(renderData);

    return result;
}

VkPipeline MaterialPipelineManager::CreatePipelineWithProgram(const ShaderProgram *program,
                                                              const RenderState &renderState)
{
    return CreatePipelineWithProgram(program, renderState, GetDefaultPassPipelineDescriptor());
}

VkPipeline MaterialPipelineManager::CreatePipelineWithProgram(const ShaderProgram *program,
                                                              const RenderState &renderState,
                                                              const MaterialPassPipelineDescriptor &pipelineDesc)
{
    if (!program || !program->IsValid() || !pipelineDesc.IsValid() ||
        program->GetCompileTarget() != pipelineDesc.target) {
        INXLOG_ERROR("Invalid shader program for pipeline creation");
        return VK_NULL_HANDLE;
    }

    RenderState effectiveState = renderState;
    if (pipelineDesc.target != ShaderCompileTarget::Forward &&
        pipelineDesc.target != ShaderCompileTarget::ForwardPlus) {
        effectiveState.blendEnable = false;
        effectiveState.depthTestEnable = pipelineDesc.depthFormat != rhi::PixelFormat::Undefined;
        effectiveState.depthWriteEnable = pipelineDesc.depthFormat != rhi::PixelFormat::Undefined;
    }
    if (pipelineDesc.depthReadOnly)
        effectiveState.depthWriteEnable = false;
    if (pipelineDesc.target == ShaderCompileTarget::Normal || pipelineDesc.target == ShaderCompileTarget::BaseColor) {
        // The normal pass replays the visible opaque geometry against the
        // camera depth attachment.  Exact equality is unnecessarily brittle:
        // a semantic shader variant can produce a sub-ULP clip-depth change
        // even though it covers the same surface.  LESS_OR_EQUAL preserves
        // hidden-surface rejection while allowing that visible surface to
        // publish its normal.
        effectiveState.depthCompareOp = MaterialCompareOp::LessOrEqual;
    }
    if (!rhi::IsStencilFormat(pipelineDesc.depthFormat))
        effectiveState.stencilTestEnable = false;

    // Shader stages
    auto shaderStages = vkrender::MakeVertFragStages(program->GetVertexModule(), program->GetFragmentModule());

    // Dynamic state
    vkrender::DynamicViewportScissorState dynVpScissor;

    // Vertex input - expose only attributes consumed by this vertex shader.
    // This keeps shaders that do not use skinning inputs from producing Vulkan
    // validation warnings for locations 5/6.
    auto bindingDescription = vk::GetVertexBindingDescription();
    auto attributeDescriptions = FilterVertexAttributesForReflection(program->GetVertexReflection());

    VkPipelineVertexInputStateCreateInfo vertexInputInfo{};
    vertexInputInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;
    vertexInputInfo.vertexBindingDescriptionCount = attributeDescriptions.empty() ? 0u : 1u;
    vertexInputInfo.pVertexBindingDescriptions = attributeDescriptions.empty() ? nullptr : &bindingDescription;
    vertexInputInfo.vertexAttributeDescriptionCount = static_cast<uint32_t>(attributeDescriptions.size());
    vertexInputInfo.pVertexAttributeDescriptions =
        attributeDescriptions.empty() ? nullptr : attributeDescriptions.data();

    // Input assembly
    VkPipelineInputAssemblyStateCreateInfo inputAssembly{};
    inputAssembly.sType = VK_STRUCTURE_TYPE_PIPELINE_INPUT_ASSEMBLY_STATE_CREATE_INFO;
    inputAssembly.topology = vk::ToVkPrimitiveTopology(effectiveState.topology);
    inputAssembly.primitiveRestartEnable = VK_FALSE;

    // Multisampling
    auto multisampling = vkrender::MakeMultisampleState(rhi::ToVkSampleCount(pipelineDesc.samples));

    // Rasterizer
    VkPipelineRasterizationStateCreateInfo rasterizer{};
    rasterizer.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;
    rasterizer.depthClampEnable = VK_FALSE;
    rasterizer.rasterizerDiscardEnable = VK_FALSE;
    rasterizer.polygonMode = vk::ToVkPolygonMode(effectiveState.polygonMode);
    rasterizer.lineWidth = effectiveState.lineWidth;
    rasterizer.cullMode = vk::ToVkCullMode(effectiveState.cullMode);
    rasterizer.frontFace = vk::ToVkFrontFace(effectiveState.frontFace);
    rasterizer.depthBiasEnable = effectiveState.depthBiasEnable ? VK_TRUE : VK_FALSE;
    rasterizer.depthBiasConstantFactor = effectiveState.depthBiasConstantFactor;
    rasterizer.depthBiasSlopeFactor = effectiveState.depthBiasSlopeFactor;
    rasterizer.depthBiasClamp = effectiveState.depthBiasClamp;

    // Color blending — create one blend attachment per color output for MRT.
    // Opaque forward passes also need alpha writes so intermediate scene
    // layers can preserve coverage for later fullscreen composites.
    VkPipelineColorBlendAttachmentState colorBlendAttachment{};
    colorBlendAttachment.colorWriteMask =
        VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT | VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT;
    colorBlendAttachment.blendEnable = effectiveState.blendEnable ? VK_TRUE : VK_FALSE;
    colorBlendAttachment.srcColorBlendFactor = vk::ToVkBlendFactor(effectiveState.srcColorBlendFactor);
    colorBlendAttachment.dstColorBlendFactor = vk::ToVkBlendFactor(effectiveState.dstColorBlendFactor);
    colorBlendAttachment.colorBlendOp = vk::ToVkBlendOp(effectiveState.colorBlendOp);
    colorBlendAttachment.srcAlphaBlendFactor = vk::ToVkBlendFactor(effectiveState.srcAlphaBlendFactor);
    colorBlendAttachment.dstAlphaBlendFactor = vk::ToVkBlendFactor(effectiveState.dstAlphaBlendFactor);
    colorBlendAttachment.alphaBlendOp = vk::ToVkBlendOp(effectiveState.alphaBlendOp);

    std::vector<VkPipelineColorBlendAttachmentState> blendAttachments(pipelineDesc.colorFormats.size(),
                                                                      colorBlendAttachment);

    VkPipelineColorBlendStateCreateInfo colorBlending{};
    colorBlending.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO;
    colorBlending.logicOpEnable = VK_FALSE;
    colorBlending.attachmentCount = static_cast<uint32_t>(blendAttachments.size());
    colorBlending.pAttachments = blendAttachments.data();

    // Depth stencil
    VkPipelineDepthStencilStateCreateInfo depthStencil{};
    depthStencil.sType = VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;
    depthStencil.depthTestEnable = effectiveState.depthTestEnable ? VK_TRUE : VK_FALSE;
    depthStencil.depthWriteEnable = effectiveState.depthWriteEnable ? VK_TRUE : VK_FALSE;
    depthStencil.depthCompareOp = vk::ToVkCompareOp(effectiveState.depthCompareOp);
    depthStencil.depthBoundsTestEnable = VK_FALSE;
    depthStencil.stencilTestEnable = effectiveState.stencilTestEnable ? VK_TRUE : VK_FALSE;
    depthStencil.front = vk::ToVkStencilOpState(effectiveState.stencilFront);
    depthStencil.back = vk::ToVkStencilOpState(effectiveState.stencilBack);

    // Create pipeline with shader program's layout
    VkGraphicsPipelineCreateInfo pipelineInfo{};
    pipelineInfo.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;
    pipelineInfo.stageCount = static_cast<uint32_t>(shaderStages.size());
    pipelineInfo.pStages = shaderStages.data();
    pipelineInfo.pVertexInputState = &vertexInputInfo;
    pipelineInfo.pInputAssemblyState = &inputAssembly;
    pipelineInfo.pViewportState = &dynVpScissor.viewportState;
    pipelineInfo.pRasterizationState = &rasterizer;
    pipelineInfo.pMultisampleState = &multisampling;
    pipelineInfo.pDepthStencilState = &depthStencil;
    pipelineInfo.pColorBlendState = &colorBlending;
    pipelineInfo.pDynamicState = &dynVpScissor.dynamicState;
    pipelineInfo.layout = program->GetPipelineLayout(); // Use program's layout!
    std::array<VkFormat, rhi::GraphicsRenderingSignature::MaxColorTargets> dynamicColorFormats{};
    VkPipelineRenderingCreateInfo dynamicRenderingInfo{};
    if (!rhi::BuildVkPipelineRenderingInfo(pipelineDesc.RenderingSignature(), dynamicColorFormats,
                                           dynamicRenderingInfo)) {
        INXLOG_ERROR("Invalid Dynamic Rendering signature for ", ShaderCompileTargetName(pipelineDesc.target),
                     " material pipeline");
        return VK_NULL_HANDLE;
    }
    pipelineInfo.pNext = &dynamicRenderingInfo;
    pipelineInfo.renderPass = VK_NULL_HANDLE;
    pipelineInfo.subpass = 0;
    pipelineInfo.basePipelineHandle = VK_NULL_HANDLE;

    VkPipeline pipeline;
    if (vkCreateGraphicsPipelines(m_device, m_vkPipelineCache, 1, &pipelineInfo, nullptr, &pipeline) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create graphics pipeline with shader program");
        return VK_NULL_HANDLE;
    }

    return pipeline;
}

void MaterialPipelineManager::UpdateMaterialProperties(const std::string &materialName, const InxMaterial &material)
{
    m_descriptorManager.UpdateMaterialUBO(materialName, material);

    // Re-resolve Texture2D properties in case set_texture was called
    auto it = m_renderDataMap.find(materialName);
    if (it != m_renderDataMap.end() && it->second && it->second->shaderProgram) {
        m_descriptorManager.ResolveTextureProperties(materialName, material, *it->second->shaderProgram);
        RefreshPublishedDescriptorHandle(materialName);
    }
}

void MaterialPipelineManager::BindMaterialTexture(const std::string &materialName, uint32_t binding,
                                                  VkImageView imageView, VkSampler sampler)
{
    m_descriptorManager.BindTexture(materialName, binding, imageView, sampler);
    RefreshPublishedDescriptorHandle(materialName);
}

void MaterialPipelineManager::RefreshPublishedDescriptorHandle(const std::string &materialName)
{
    const auto forward = m_renderDataMap.find(materialName);
    if (forward == m_renderDataMap.end() || !forward->second || !forward->second->materialDescSet)
        return;

    const VkDescriptorSet published = forward->second->materialDescSet->descriptorSet;
    if (published == VK_NULL_HANDLE || !m_descriptorManager.IsDescriptorSetLive(published))
        return;

    const bool changed = forward->second->descriptorSet != published;
    forward->second->descriptorSet = published;
    for (auto &[key, passData] : m_passRenderDataMap) {
        if (key.materialKey == materialName && passData)
            passData->descriptorSet = published;
    }
    if (changed)
        ++m_publicationGeneration;
}

void MaterialPipelineManager::SetDefaultTexture(VkImageView imageView, VkSampler sampler,
                                                std::shared_ptr<const rhi::TextureGpuView> gpuView)
{
    m_descriptorManager.SetDefaultTexture(imageView, sampler, std::move(gpuView));
}

void MaterialPipelineManager::SetDefaultNormalTexture(VkImageView imageView, VkSampler sampler,
                                                      std::shared_ptr<const rhi::TextureGpuView> gpuView)
{
    m_descriptorManager.SetDefaultNormalTexture(imageView, sampler, std::move(gpuView));
}

void MaterialPipelineManager::InvalidateMaterialsUsingShader(const std::string &shaderId)
{
    INXLOG_INFO("Invalidating materials using shader: ", shaderId);

    // Helper to extract shader name from path
    auto extractShaderName = [](const std::string &path) -> std::string {
        if (path.empty())
            return "";
        return PortablePathStem(path);
    };

    std::vector<std::string> materialsToRemove;

    for (auto &[name, data] : m_renderDataMap) {
        if (!data || !data->material)
            continue;

        // Check if this material uses the specified shader (vert or frag)
        const std::string &vertName = data->material->GetVertShaderName();
        const std::string &fragName = data->material->GetFragShaderName();

        // Check exact match or shader name match against both vert and frag
        bool matches = (vertName == shaderId) || (extractShaderName(vertName) == shaderId) || (fragName == shaderId) ||
                       (extractShaderName(fragName) == shaderId);

        if (matches) {
            materialsToRemove.push_back(name);
        }
    }

    // Remove render data for affected materials (force recreation)
    for (const auto &name : materialsToRemove) {
        RemoveRenderData(name);
    }

    INXLOG_INFO("Invalidated ", materialsToRemove.size(), " materials using shader '", shaderId, "'");
}

void MaterialPipelineManager::InvalidateMaterialsUsingProgramPair(const ShaderStagePair &stages)
{
    if (!stages.IsValid())
        throw std::invalid_argument("Material program-pair invalidation requires both shader identifiers");

    std::vector<std::string> materialsToRemove;
    for (const auto &[name, data] : m_renderDataMap) {
        if (data && data->programKey.stages == stages)
            materialsToRemove.push_back(name);
    }
    for (const auto &name : materialsToRemove)
        RemoveRenderData(name);

    INXLOG_INFO("Invalidated ", materialsToRemove.size(), " materials using shader program '", stages.ToString(), "'");
}

uint32_t MaterialPipelineManager::RefreshMaterialsUsingTexture(const std::string &textureGuid)
{
    // Texture2D properties hold validated GUIDs or builtin tokens, so equality is sufficient.
    if (textureGuid.empty()) {
        return 0;
    }

    std::vector<std::string> materialsToRefresh;
    materialsToRefresh.reserve(m_renderDataMap.size());

    for (const auto &[name, data] : m_renderDataMap) {
        if (!data || !data->material) {
            continue;
        }

        bool matches = false;
        for (const auto &[propName, prop] : data->material->GetAllProperties()) {
            (void)propName;
            if (prop.type != MaterialPropertyType::Texture2D) {
                continue;
            }

            const auto *value = std::get_if<std::string>(&prop.value);
            if (value && *value == textureGuid) {
                matches = true;
                break;
            }
        }

        if (matches) {
            materialsToRefresh.push_back(name);
        }
    }

    for (const auto &name : materialsToRefresh) {
        const auto found = m_renderDataMap.find(name);
        if (found != m_renderDataMap.end() && found->second && found->second->material)
            UpdateMaterialProperties(name, *found->second->material);
    }

    if (!materialsToRefresh.empty()) {
        INXLOG_INFO("Refreshed ", materialsToRefresh.size(), " material texture publications for GUID '", textureGuid,
                    "'");
    }

    return static_cast<uint32_t>(materialsToRefresh.size());
}

void MaterialPipelineManager::InvalidateAllMaterialPipelines()
{
    RemoveAllPassRenderData();
    uint32_t count = 0;
    for (auto &[name, data] : m_renderDataMap) {
        if (!data || !data->material)
            continue;
        data->material->MarkPipelineDirty();
        ++count;
    }
    // MarkPipelineDirty is intentionally a material-local flag and does not
    // change InxMaterial::GetVersion(). Publish a manager generation as well so
    // renderer-side resolved-pass caches cannot reuse the old pipeline.
    ++m_publicationGeneration;
    // if (count > 0) {
    //     INXLOG_INFO("InvalidateAllMaterialPipelines: marked ", count, " materials dirty");
    // }
}

void MaterialPipelineManager::RemovePassRenderData(const std::string &materialKey)
{
    bool removed = false;
    std::unordered_set<VkPipeline> pipelines;
    for (auto it = m_passRenderDataMap.begin(); it != m_passRenderDataMap.end();) {
        if (it->first.materialKey == materialKey) {
            removed = true;
            if (it->second && it->second->pipeline != VK_NULL_HANDLE)
                pipelines.insert(it->second->pipeline);
            it = m_passRenderDataMap.erase(it);
        } else {
            ++it;
        }
    }
    for (VkPipeline pipeline : pipelines)
        RetirePipelineIfUnreferenced(pipeline);
    if (removed)
        ++m_publicationGeneration;
}

void MaterialPipelineManager::RemoveAllPassRenderData()
{
    std::unordered_set<VkPipeline> pipelines;
    pipelines.reserve(m_passRenderDataMap.size());
    for (const auto &[key, data] : m_passRenderDataMap) {
        (void)key;
        if (data && data->pipeline != VK_NULL_HANDLE)
            pipelines.insert(data->pipeline);
    }
    m_passRenderDataMap.clear();
    for (VkPipeline pipeline : pipelines)
        RetirePipelineIfUnreferenced(pipeline);
    if (!pipelines.empty())
        ++m_publicationGeneration;
}

void MaterialPipelineManager::RetirePipelineIfUnreferenced(VkPipeline pipeline)
{
    if (pipeline == VK_NULL_HANDLE || IsPipelineSharedByOthers("", pipeline))
        return;

    for (auto it = m_pipelineCache.begin(); it != m_pipelineCache.end();) {
        if (it->second == pipeline)
            it = m_pipelineCache.erase(it);
        else
            ++it;
    }

    if (m_deletionQueue) {
        const VkDevice device = m_device;
        m_deletionQueue->Retire([device, pipeline] { vkDestroyPipeline(device, pipeline, nullptr); });
    } else {
        vkDestroyPipeline(m_device, pipeline, nullptr);
    }
}

void MaterialPipelineManager::RemoveRenderData(const std::string &materialName)
{
    m_failedForwardPipelineHashes.erase(materialName);
    RemovePassRenderData(materialName);
    // Also remove the cached descriptor set so that pipeline re-creation
    // builds a fresh one with current texture bindings (avoids stale refs).
    m_descriptorManager.RemoveDescriptorSet(materialName);

    auto it = m_renderDataMap.find(materialName);
    if (it == m_renderDataMap.end()) {
        return;
    }

    auto &data = it->second;
    if (data) {
        if (data->material) {
            ClearForwardPassHandles(data->material.get());
            DestroyNonForwardPipelines(data->material.get(), true);
            RetireMaterialUBO(data->material->DetachUBO());
        }

        // Only destroy the pipeline if no other render data shares it.
        if (data->pipeline != VK_NULL_HANDLE) {
            if (!IsPipelineSharedByOthers(materialName, data->pipeline)) {
                for (auto pipeIt = m_pipelineCache.begin(); pipeIt != m_pipelineCache.end();) {
                    if (pipeIt->second == data->pipeline) {
                        pipeIt = m_pipelineCache.erase(pipeIt);
                    } else {
                        ++pipeIt;
                    }
                }
                if (m_deletionQueue) {
                    const VkDevice device = m_device;
                    const VkPipeline pipeline = data->pipeline;
                    m_deletionQueue->Retire([device, pipeline] { vkDestroyPipeline(device, pipeline, nullptr); });
                } else {
                    vkDestroyPipeline(m_device, data->pipeline, nullptr);
                }
            }
        }
    }

    m_renderDataMap.erase(it);
    ++m_publicationGeneration;
}

void MaterialPipelineManager::RetireMaterialUBO(InxMaterial::DetachedUBO resource)
{
    if (resource.buffer == VK_NULL_HANDLE)
        return;
    if (resource.allocator == VK_NULL_HANDLE || resource.allocation == VK_NULL_HANDLE)
        throw std::logic_error("Material UBO has incomplete VMA ownership");
    if (m_deletionQueue) {
        m_deletionQueue->Retire(
            [resource] { vmaDestroyBuffer(resource.allocator, resource.buffer, resource.allocation); });
        return;
    }
    vmaDestroyBuffer(resource.allocator, resource.buffer, resource.allocation);
}

size_t MaterialPipelineManager::CollectUnusedRenderData()
{
    // Forward and semantic pass caches all keep strong references to the same
    // material. Comparing use_count() with one makes every material that has
    // ever rendered a Shadow/Depth/Picking pass immortal. Count only references
    // owned by this manager; a material is live when somebody outside these
    // caches still owns it.
    std::unordered_map<const InxMaterial *, size_t> internalReferences;
    internalReferences.reserve(m_renderDataMap.size());
    for (const auto &[name, data] : m_renderDataMap) {
        (void)name;
        if (data && data->material)
            ++internalReferences[data->material.get()];
    }
    for (const auto &[key, data] : m_passRenderDataMap) {
        (void)key;
        if (data && data->material)
            ++internalReferences[data->material.get()];
    }

    std::vector<std::string> unused;
    unused.reserve(m_renderDataMap.size());
    for (const auto &[name, data] : m_renderDataMap) {
        if (!data || !data->material || data.get() == m_defaultRenderData)
            continue;
        // Registry-owned builtins naturally have another strong owner. Do not
        // use IsBuiltin() as a lifetime signal: runtime materials created from
        // a built-in shader carry that flag too and still need reclamation.
        const auto internal = internalReferences.find(data->material.get());
        const size_t internalCount = internal == internalReferences.end() ? 0u : internal->second;
        if (data->material.use_count() <= internalCount)
            unused.push_back(name);
    }
    for (const auto &name : unused)
        RemoveRenderData(name);
    return unused.size();
}

MaterialGpuResidencySnapshot MaterialPipelineManager::GetResidencySnapshot() const
{
    MaterialGpuResidencySnapshot snapshot;
    snapshot.renderDataCount = m_renderDataMap.size();
    snapshot.pipelineCount = m_pipelineCache.size();
    snapshot.descriptorSetCount = m_descriptorManager.GetDescriptorSetCount();
    snapshot.pendingTextureDescriptorSetCount = m_descriptorManager.GetPendingTextureDescriptorSetCount();
    snapshot.retiredDescriptorSetCount = m_descriptorManager.GetRetiredDescriptorSetCount();
    snapshot.descriptorPoolCount = m_descriptorManager.GetDescriptorPoolCount();
    for (const auto &[key, data] : m_renderDataMap) {
        (void)key;
        if (!data || !data->material)
            continue;
        if (data->material->GetGuid().empty())
            ++snapshot.runtimeMaterialCount;
        else
            ++snapshot.assetMaterialCount;
        const VmaAllocation allocation = data->material->GetUBOAllocation();
        if (allocation == VK_NULL_HANDLE)
            continue;
        VmaAllocationInfo info{};
        vmaGetAllocationInfo(m_allocator, allocation, &info);
        snapshot.uboBytes += info.size;
    }
    return snapshot;
}

} // namespace infernux
