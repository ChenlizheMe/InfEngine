/**
 * @file VkCoreMaterial.cpp
 * @brief InxVkCoreModular — Material system, lighting, and buffer accessors
 *
 * Split from InxVkCoreModular.cpp for maintainability.
 * Contains: UpdateMaterialUBO, EnsureMaterialUBO, CreateBuffer,
 *           InitializeMaterialSystem, RefreshMaterialPipeline,
 *           SetAmbientColor, UpdateLightingUBO,
 *           GetObjectBuffer, GetUniformBuffer, GetShaderModule.
 */

#include "InxError.h"
#include "InxVkCoreModular.h"
#include "MsaaPolicy.h"
#include "VertexInputFilter.h"
#include "gui/GPUMaterialPreview.h"
#include "gui/GPUMeshPreview.h"
#include "vk/RhiVulkanTypes.h"
#include "vk/VkPipelineHelpers.h"
#include "vk/VkRenderUtils.h"

#include <function/renderer/shader/ShaderProgram.h>
#include <function/renderer/shader/ShaderReflection.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/resources/InxTexture/InxTexture.h>

#include <algorithm>
#include <array>
#include <glm/glm.hpp>
#include <unordered_set>

#include <cstring>

namespace infernux
{

namespace
{

template <typename Handle> uint64_t VulkanHandleBits(Handle handle)
{
    static_assert(sizeof(Handle) <= sizeof(uint64_t));
    uint64_t bits = 0;
    std::memcpy(&bits, &handle, sizeof(Handle));
    return bits;
}

void HashCombine(size_t &seed, uint64_t value)
{
    const size_t hash = std::hash<uint64_t>{}(value);
    seed ^= hash + 0x9e3779b97f4a7c15ull + (seed << 6) + (seed >> 2);
}

} // namespace

// ============================================================================
// Shared texture resolution for material Texture2D properties
// ============================================================================

void InxVkCoreModular::PumpPendingTextureLoads()
{
    auto &registry = AssetRegistry::Instance();
    for (auto pending = m_pendingTextureCpuLoads.begin(); pending != m_pendingTextureCpuLoads.end();) {
        try {
            if (!registry.TryCommitAssetLoad(pending->second)) {
                ++pending;
                continue;
            }
            m_materialPipelineManager.InvalidateMaterialsUsingTexture(pending->first);
        } catch (const std::exception &exception) {
            INXLOG_ERROR("Texture CPU load failed for GUID '", pending->first, "': ", exception.what());
        }
        pending = m_pendingTextureCpuLoads.erase(pending);
    }

    for (auto pending = m_pendingTextureGpuUploads.begin(); pending != m_pendingTextureGpuUploads.end();) {
        try {
            if (!m_resourceManager.TryPublishTextureUpload(pending->second.ticket)) {
                ++pending;
                continue;
            }
            auto texture = pending->second.ticket->GetTexture();
            if (!registry.IsLoaded(pending->second.guid) ||
                registry.GetAssetVersion(pending->second.guid) != pending->second.runtimeVersion) {
                pending = m_pendingTextureGpuUploads.erase(pending);
                continue;
            }
            (void)m_textureCache.Insert(pending->first, std::move(texture), m_ensureFrameCounter, false,
                                        pending->second.guid, pending->second.runtimeVersion);
            m_materialPipelineManager.InvalidateMaterialsUsingTexture(pending->second.guid);
            ++m_completedTextureUploadCount;
        } catch (const std::exception &exception) {
            INXLOG_ERROR("Texture GPU upload failed for GUID '", pending->second.guid, "': ", exception.what());
        }
        pending = m_pendingTextureGpuUploads.erase(pending);
    }
}

TextureResolveResult InxVkCoreModular::ResolveTextureForMaterial(const std::string &textureRef,
                                                                 const std::string &bindingName)
{
    // Material texture properties store asset GUIDs. Path normalization belongs
    // at the public material/asset boundary, never in the renderer.
    const std::string &textureGuid = textureRef;
    std::string texturePath;
    auto &registry = AssetRegistry::Instance();
    auto *adb = registry.GetAssetDatabase();
    if (adb)
        texturePath = adb->GetPathFromGuid(textureGuid);

    if (texturePath.empty()) {
        INXLOG_WARN("TextureResolver: texture reference '", textureRef, "' is not a resolvable asset GUID (binding='",
                    bindingName, "'). Texture properties must hold GUIDs.");
        return {TextureResolveStatus::Failed, {}};
    }

    auto infTex = registry.GetAsset<InxTexture>(textureGuid);
    if (!infTex) {
        auto pending = m_pendingTextureCpuLoads.find(textureGuid);
        if (pending == m_pendingTextureCpuLoads.end()) {
            try {
                pending = m_pendingTextureCpuLoads
                              .emplace(textureGuid, registry.BeginLoadAsset(textureGuid, ResourceType::Texture))
                              .first;
            } catch (const std::exception &exception) {
                INXLOG_ERROR("TextureResolver: failed to schedule CPU load for '", textureGuid,
                             "': ", exception.what());
                return {TextureResolveStatus::Failed, {}};
            }
        }
        try {
            if (!registry.TryCommitAssetLoad(pending->second))
                return {TextureResolveStatus::Pending, {}};
            m_pendingTextureCpuLoads.erase(pending);
            infTex = registry.GetAsset<InxTexture>(textureGuid);
        } catch (const std::exception &exception) {
            INXLOG_ERROR("TextureResolver: CPU load failed for '", textureGuid, "': ", exception.what());
            m_pendingTextureCpuLoads.erase(pending);
            return {TextureResolveStatus::Failed, {}};
        }
    }
    if (!infTex || !infTex->GetCpuData() || !infTex->GetCpuData()->IsValid()) {
        INXLOG_ERROR("TextureResolver: texture asset has no decoded CPU payload: ", textureGuid);
        return {TextureResolveStatus::Failed, {}};
    }

    const bool isLinearTexture = infTex->IsLinear();
    const uint64_t runtimeVersion = registry.GetAssetVersion(textureGuid);
    if (runtimeVersion == 0)
        throw std::logic_error("TextureResolver resolved a payload without a published runtime version");
    const bool normalMapMode = infTex->IsNormalMapMode();
    const std::string &filterMode = infTex->GetFilterMode();
    const std::string &wrapMode = infTex->GetWrapMode();
    const int anisoLevel = infTex->GetAnisoLevel();
    const VkFormat format = infTex->GetCpuData()->storage == TexturePixelStorage::Rgba32Float
                                ? VK_FORMAT_R32G32B32A32_SFLOAT
                                : (isLinearTexture ? VK_FORMAT_R8G8B8A8_UNORM : VK_FORMAT_R8G8B8A8_SRGB);

    // Map string settings to Vulkan enums
    VkFilter vkFilter = VK_FILTER_LINEAR;
    if (filterMode == "point")
        vkFilter = VK_FILTER_NEAREST;

    VkSamplerAddressMode vkAddressMode = VK_SAMPLER_ADDRESS_MODE_REPEAT;
    if (wrapMode == "clamp")
        vkAddressMode = VK_SAMPLER_ADDRESS_MODE_CLAMP_TO_EDGE;
    else if (wrapMode == "mirror")
        vkAddressMode = VK_SAMPLER_ADDRESS_MODE_MIRRORED_REPEAT;

    // Cache key uses GUID so that a renamed file still shares its cache entry
    std::string cacheKey = textureGuid + (isLinearTexture ? "::unorm" : "::srgb") +
                           (normalMapMode ? "::normalmap" : "::raw") + "::" + filterMode + "::" + wrapMode + "::aniso" +
                           std::to_string(anisoLevel);

    // Check texture cache (thread-safe)
    {
        auto cached = m_textureCache.FindAsset(cacheKey, textureGuid, runtimeVersion, m_ensureFrameCounter);
        if (cached) {
            return {TextureResolveStatus::Ready, {cached->GetView(), cached->GetSampler(), std::move(cached)}};
        }
    }

    auto pendingGpu = m_pendingTextureGpuUploads.find(cacheKey);
    if (pendingGpu == m_pendingTextureGpuUploads.end()) {
        try {
            auto ticket = m_resourceManager.BeginTextureUpload(*infTex->GetCpuData(), format, vkFilter, vkAddressMode,
                                                               anisoLevel);
            ++m_submittedTextureUploadCount;
            if (ticket->IsAsync())
                ++m_asyncTextureUploadCount;
            pendingGpu = m_pendingTextureGpuUploads
                             .emplace(cacheKey, PendingTextureGpuUpload{textureGuid, runtimeVersion, std::move(ticket)})
                             .first;
        } catch (const std::exception &exception) {
            INXLOG_ERROR("TextureResolver: failed to schedule GPU upload for '", textureGuid, "': ", exception.what());
            return {TextureResolveStatus::Failed, {}};
        }
    }
    try {
        if (!m_resourceManager.TryPublishTextureUpload(pendingGpu->second.ticket))
            return {TextureResolveStatus::Pending, {}};
        auto texture = pendingGpu->second.ticket->GetTexture();
        const VkImageView view = texture->GetView();
        const VkSampler sampler = texture->GetSampler();
        if (!registry.IsLoaded(textureGuid) ||
            registry.GetAssetVersion(textureGuid) != pendingGpu->second.runtimeVersion) {
            m_pendingTextureGpuUploads.erase(pendingGpu);
            return {TextureResolveStatus::Pending, {}};
        }
        auto resident = m_textureCache.Insert(cacheKey, std::move(texture), m_ensureFrameCounter, false, textureGuid,
                                              pendingGpu->second.runtimeVersion);
        m_pendingTextureGpuUploads.erase(pendingGpu);
        ++m_completedTextureUploadCount;
        return {TextureResolveStatus::Ready, {view, sampler, std::move(resident)}};
    } catch (const std::exception &exception) {
        INXLOG_ERROR("TextureResolver: GPU upload failed for '", textureGuid, "': ", exception.what());
        m_pendingTextureGpuUploads.erase(pendingGpu);
        return {TextureResolveStatus::Failed, {}};
    }
}

// ============================================================================
// Material UBO Management
// ============================================================================

namespace
{

/// Copy a typed material property into the UBO at a reflection-determined offset.
template <typename T>
void CopyPropertyToUBO(const MaterialProperty &prop, uint8_t *uboData, uint32_t offset, size_t uboSize)
{
    if (offset + sizeof(T) <= uboSize) {
        T value = std::get<T>(prop.value);
        std::memcpy(uboData + offset, &value, sizeof(T));
    }
}

/// Pack all properties of a given type sequentially with manual alignment (fallback path).
/// @param stride — bytes to advance after each copy (usually sizeof(T), except vec3 which uses 16).
template <typename T>
void PackPropertiesByType(const std::unordered_map<std::string, MaterialProperty> &properties,
                          MaterialPropertyType type, uint8_t *uboData, size_t &offset, size_t uboSize, size_t alignment,
                          size_t stride = 0)
{
    if (stride == 0)
        stride = sizeof(T);
    for (const auto &[name, prop] : properties) {
        if (prop.type != type)
            continue;
        offset = (offset + (alignment - 1)) & ~(alignment - 1);
        if (offset + sizeof(T) <= uboSize) {
            T value = std::get<T>(prop.value);
            std::memcpy(uboData + offset, &value, sizeof(T));
            offset += stride;
        }
    }
}

} // anonymous namespace

void InxVkCoreModular::UpdateMaterialUBO(InxMaterial &material)
{
    if (!material.IsPropertiesDirty()) {
        return;
    }

    if (m_materialPipelineManagerInitialized) {
        MaterialRenderData *renderData = m_materialPipelineManager.GetRenderData(material.GetMaterialKey());
        if (renderData && renderData->isValid && renderData->shaderProgram &&
            renderData->descriptorSet != VK_NULL_HANDLE &&
            m_materialPipelineManager.IsDescriptorSetLive(renderData->descriptorSet)) {
            m_materialPipelineManager.UpdateMaterialProperties(material.GetMaterialKey(), material);
            material.ClearPropertiesDirty();
            return;
        }
    }

    ShaderProgram *shaderProgram = material.GetPassShaderProgram(ShaderCompileTarget::Forward);
    const MaterialUBOLayout *uboLayout = shaderProgram ? shaderProgram->GetMaterialUBOLayout() : nullptr;

    if (!uboLayout || uboLayout->size == 0) {
        INXLOG_WARN("VkCoreMaterial: material '", material.GetName(),
                    "' has no UBO reflection layout — skipping UBO update");
        material.ClearPropertiesDirty();
        return;
    }
    size_t uboSize = uboLayout->size;

    const auto &properties = material.GetAllProperties();

    std::vector<uint8_t> uboData(uboSize, 0);

    if (uboLayout && !uboLayout->members.empty()) {
        for (const auto &[name, prop] : properties) {
            uint32_t memberOffset = 0;
            uint32_t memberSize = 0;

            if (!uboLayout->GetMemberInfo(name, memberOffset, memberSize)) {
                continue;
            }

            switch (prop.type) {
            case MaterialPropertyType::Float4:
            case MaterialPropertyType::Color:
                CopyPropertyToUBO<glm::vec4>(prop, uboData.data(), memberOffset, uboSize);
                break;
            case MaterialPropertyType::Float3:
                CopyPropertyToUBO<glm::vec3>(prop, uboData.data(), memberOffset, uboSize);
                break;
            case MaterialPropertyType::Float2:
                CopyPropertyToUBO<glm::vec2>(prop, uboData.data(), memberOffset, uboSize);
                break;
            case MaterialPropertyType::Float:
                CopyPropertyToUBO<float>(prop, uboData.data(), memberOffset, uboSize);
                break;
            case MaterialPropertyType::Int:
                CopyPropertyToUBO<int>(prop, uboData.data(), memberOffset, uboSize);
                break;
            default:
                break;
            }
        }
    } else {
        size_t offset = 0;
        PackPropertiesByType<glm::vec4>(properties, MaterialPropertyType::Float4, uboData.data(), offset, uboSize, 16);
        PackPropertiesByType<glm::vec3>(properties, MaterialPropertyType::Float3, uboData.data(), offset, uboSize, 16,
                                        16);
        PackPropertiesByType<glm::vec2>(properties, MaterialPropertyType::Float2, uboData.data(), offset, uboSize, 8);
        PackPropertiesByType<float>(properties, MaterialPropertyType::Float, uboData.data(), offset, uboSize, 4);
        PackPropertiesByType<int>(properties, MaterialPropertyType::Int, uboData.data(), offset, uboSize, 4);
    }

    if (material.HasUBO()) {
        void *matMappedData = material.GetUBOMappedData();
        if (matMappedData) {
            std::memcpy(matMappedData, uboData.data(), uboSize);
        }
    } else if (m_materialUboMapped) {
        std::memcpy(m_materialUboMapped, uboData.data(), uboSize);
    }

    material.ClearPropertiesDirty();
}

void InxVkCoreModular::EnsureMaterialUBO(std::shared_ptr<InxMaterial> material)
{
    if (!material) {
        return;
    }

    if (material->HasUBO()) {
        return;
    }

    VkBuffer uboBuffer = VK_NULL_HANDLE;
    VmaAllocation uboAllocation = VK_NULL_HANDLE;
    void *uboMappedData = nullptr;

    // Require reflection layout for UBO creation
    ShaderProgram *shaderProgram = material->GetPassShaderProgram(ShaderCompileTarget::Forward);
    const MaterialUBOLayout *uboLayout = shaderProgram ? shaderProgram->GetMaterialUBOLayout() : nullptr;
    if (!uboLayout || uboLayout->size == 0) {
        INXLOG_WARN("VkCoreMaterial: material '", material->GetName(),
                    "' has no UBO reflection layout — skipping UBO creation");
        return;
    }
    size_t uboSize = uboLayout->size;
    CreateBuffer(uboSize, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT,
                 VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT, uboBuffer, uboAllocation);

    VmaAllocator allocator = m_deviceContext.GetVmaAllocator();
    vmaMapMemory(allocator, uboAllocation, &uboMappedData);
    if (uboMappedData) {
        std::memset(uboMappedData, 0, uboSize);
    }

    material->SetUBOBuffer(allocator, uboBuffer, uboAllocation, uboMappedData);
}

// ============================================================================
// Material / Pipeline System
// ============================================================================

void InxVkCoreModular::CreateBuffer(VkDeviceSize size, VkBufferUsageFlags usage, VkMemoryPropertyFlags properties,
                                    VkBuffer &buffer, VmaAllocation &allocation)
{
    VkBufferCreateInfo bufferInfo{};
    bufferInfo.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    bufferInfo.size = size;
    bufferInfo.usage = usage;
    bufferInfo.sharingMode = VK_SHARING_MODE_EXCLUSIVE;

    VmaAllocator allocator = m_deviceContext.GetVmaAllocator();
    VmaAllocationCreateInfo allocCreateInfo{};

    if (properties & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) {
        if (properties & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT) {
            allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO;
            allocCreateInfo.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_SEQUENTIAL_WRITE_BIT;
            allocCreateInfo.requiredFlags = VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT;
        } else {
            allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE;
        }
    } else if (properties & VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT) {
        allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO;
        allocCreateInfo.requiredFlags = properties;
        if (usage & VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT) {
            allocCreateInfo.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_RANDOM_BIT;
        } else {
            allocCreateInfo.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_SEQUENTIAL_WRITE_BIT;
        }
    } else {
        allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO;
    }

    VkResult result = vmaCreateBuffer(allocator, &bufferInfo, &allocCreateInfo, &buffer, &allocation, nullptr);
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("CreateBuffer: failed to create buffer via VMA");
        buffer = VK_NULL_HANDLE;
        allocation = VK_NULL_HANDLE;
    }
}

void InxVkCoreModular::InitializeMaterialSystem()
{
    if (m_materialSystemInitialized) {
        return;
    }

    AssetRegistry::Instance().InitializeBuiltinMaterials();

    if (!m_materialPipelineManagerInitialized) {
        // Use SceneRenderTarget-compatible formats: HDR R16G16B16A16_SFLOAT color + device depth format
        VkFormat colorFormat = VK_FORMAT_R16G16B16A16_SFLOAT;
        VkFormat depthFormat = m_deviceContext.FindDepthFormat();
        const auto colorSamples = m_deviceContext.GetImageSampleCountMask(
            colorFormat, VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT);
        const auto resolveSamples = m_deviceContext.GetImageSampleCountMask(
            colorFormat, VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
                             VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT);
        const auto depthSamples =
            m_deviceContext.GetImageSampleCountMask(depthFormat, VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT);
        const auto supportedSamples = GetSceneTargetSampleCountMask(colorSamples, resolveSamples, depthSamples);
        const int requestedSamples = static_cast<int>(m_msaaSampleCount);
        if (!SupportsMsaaSampleCount(supportedSamples, requestedSamples)) {
            const int fallbackSamples = SelectSupportedMsaaAtOrBelow(supportedSamples, requestedSamples);
            if (fallbackSamples == 0) {
                throw std::runtime_error(
                    "No common MSAA sample count is supported by the scene color and depth formats");
            }
            INXLOG_WARN("Default ", requestedSamples,
                        "x MSAA is unsupported for the HDR color/depth target pair; using ", fallbackSamples,
                        "x. Runtime requests remain strict and will not silently downgrade.");
            m_msaaSampleCount = rhi::ToVkSampleCount(ToRhiSampleCount(fallbackSamples));
        }
        m_materialPipelineManager.Initialize(m_deviceContext.GetVmaAllocator(), GetDevice(), GetPhysicalDevice(),
                                             colorFormat, depthFormat, m_msaaSampleCount,
                                             m_shaderCache.GetProgramCache(), &m_deletionQueue,
                                             m_deviceContext.IsDescriptorIndexingEnabled());
        m_materialPipelineManagerInitialized = true;

        auto whiteTex = m_textureCache.Find("white", m_ensureFrameCounter);
        if (whiteTex) {
            m_materialPipelineManager.SetDefaultTexture(whiteTex->GetView(), whiteTex->GetSampler());
        }

        auto normalTex = m_textureCache.Find("_default_normal", m_ensureFrameCounter);
        if (normalTex) {
            m_materialPipelineManager.SetDefaultNormalTexture(normalTex->GetView(), normalTex->GetSampler());
        }

        // Set up texture resolver for material Texture2D properties
        // Delegates to ResolveTextureForMaterial which uses GUID-based cache keys.
        m_materialPipelineManager.SetTextureResolver(
            [this](const std::string &textureRef, const std::string &bindingName) -> TextureResolveResult {
                return ResolveTextureForMaterial(textureRef, bindingName);
            });
    }

    auto defaultMaterial = AssetRegistry::Instance().GetBuiltinMaterial("DefaultLit");
    if (defaultMaterial) {
        const std::string &vertId = defaultMaterial->GetVertShaderName();
        const std::string &fragId = defaultMaterial->GetFragShaderName();

        const auto *vertCode = m_shaderCache.FindVertCode(vertId);
        const auto *fragCode = m_shaderCache.FindFragCode(fragId);

        if (vertCode && fragCode) {
            VkBuffer lightingBuffer = m_lightingUbo ? m_lightingUbo->GetBuffer() : VK_NULL_HANDLE;
            m_materialPipelineManager.GetOrCreateRenderDataWithReflection(
                defaultMaterial, *vertCode, *fragCode, ShaderProgramKey{{vertId, fragId}, 0},
                m_sceneUbo ? m_sceneUbo->GetBuffer() : VK_NULL_HANDLE, sizeof(UniformBufferObject), lightingBuffer,
                sizeof(ShaderLightingUBO));
        } else {
            INXLOG_ERROR("InitializeMaterialSystem: SPIR-V shader codes not found for default material "
                         "(vert='",
                         vertId, "', frag='", fragId, "'). Reflection path requires shader code cache.");
        }
    }

    // Pre-build error material pipeline (unlit magenta-black checkerboard).
    auto errorMaterial = AssetRegistry::Instance().GetBuiltinMaterial("ErrorMaterial");
    if (errorMaterial) {
        const std::string &errVertId = errorMaterial->GetVertShaderName();
        const std::string &errFragId = errorMaterial->GetFragShaderName();

        const auto *errVertCode = m_shaderCache.FindVertCode(errVertId);
        const auto *errFragCode = m_shaderCache.FindFragCode(errFragId);

        if (errVertCode && errFragCode) {
            VkBuffer lightingBuffer = m_lightingUbo ? m_lightingUbo->GetBuffer() : VK_NULL_HANDLE;
            auto *renderData = m_materialPipelineManager.GetOrCreateRenderDataWithReflection(
                errorMaterial, *errVertCode, *errFragCode, ShaderProgramKey{{errVertId, errFragId}, 0},
                m_sceneUbo ? m_sceneUbo->GetBuffer() : VK_NULL_HANDLE, sizeof(UniformBufferObject), lightingBuffer,
                sizeof(ShaderLightingUBO));
            if (renderData && renderData->isValid) {
                INXLOG_INFO("Error material pipeline created successfully (shaders: ", errVertId, "/", errFragId, ")");
            } else {
                INXLOG_WARN("InitializeMaterialSystem: error material pipeline deferred to lazy build");
            }
        } else {
            INXLOG_WARN("InitializeMaterialSystem: error shader SPIR-V not yet in cache "
                        "(vert='",
                        errVertId, "', frag='", errFragId, "'), will be built lazily on first use");
        }
    }

    m_materialSystemInitialized = true;
}

void InxVkCoreModular::ReinitializeMaterialPipelines(VkSampleCountFlagBits newSampleCount)
{
    m_msaaSampleCount = newSampleCount;
    if (!m_materialPipelineManagerInitialized) {
        return;
    }

    // Shutdown existing pipelines (caller must have called WaitIdle already)
    m_deletionQueue.FlushAll();
    m_materialPipelineManager.Shutdown(/* skipWaitIdle */ true);
    m_materialPipelineManagerInitialized = false;

    // Re-initialize with new sample count
    VkFormat colorFormat = VK_FORMAT_R16G16B16A16_SFLOAT;
    VkFormat depthFormat = m_deviceContext.FindDepthFormat();
    m_materialPipelineManager.Initialize(m_deviceContext.GetVmaAllocator(), GetDevice(), GetPhysicalDevice(),
                                         colorFormat, depthFormat, newSampleCount, m_shaderCache.GetProgramCache(),
                                         &m_deletionQueue, m_deviceContext.IsDescriptorIndexingEnabled());
    m_materialPipelineManagerInitialized = true;

    // Restore default textures
    auto whiteTex = m_textureCache.Find("white", m_ensureFrameCounter);
    if (whiteTex) {
        m_materialPipelineManager.SetDefaultTexture(whiteTex->GetView(), whiteTex->GetSampler());
    }
    auto normalTex = m_textureCache.Find("_default_normal", m_ensureFrameCounter);
    if (normalTex) {
        m_materialPipelineManager.SetDefaultNormalTexture(normalTex->GetView(), normalTex->GetSampler());
    }

    // Restore texture resolver
    m_materialPipelineManager.SetTextureResolver(
        [this](const std::string &textureRef, const std::string &bindingName) -> TextureResolveResult {
            return ResolveTextureForMaterial(textureRef, bindingName);
        });

    // Preview render targets cache a render pass / framebuffer that must stay
    // compatible with the material pipelines' MSAA sample count.
    m_gpuMaterialPreview.reset();
    m_gpuMeshPreview.reset();
}

bool InxVkCoreModular::RefreshMaterialPipeline(std::shared_ptr<InxMaterial> material, const std::string &vertShaderName,
                                               const std::string &fragShaderName)
{
    return RefreshPreviewMaterialPipeline(material, vertShaderName, fragShaderName,
                                          m_sceneUbo ? m_sceneUbo->GetBuffer() : VK_NULL_HANDLE,
                                          m_lightingUbo ? m_lightingUbo->GetBuffer() : VK_NULL_HANDLE);
}

void InxVkCoreModular::ReleaseGpuPreviews()
{
    m_resourceManager.DrainAsyncGraphicsSubmissions();
    m_gpuMeshPreview.reset();
    m_gpuMaterialPreview.reset();
}

bool InxVkCoreModular::RefreshPreviewMaterialPipeline(std::shared_ptr<InxMaterial> material,
                                                      const std::string &vertShaderName,
                                                      const std::string &fragShaderName, VkBuffer sceneUbo,
                                                      VkBuffer lightingUbo)
{
    if (!material) {
        return false;
    }

    // Apply shader render-state annotations to the material before pipeline creation.
    // Fragment shader annotations take priority (they define the surface behaviour).
    const auto *renderMeta = m_shaderCache.GetRenderMeta(fragShaderName);
    if (renderMeta) {
        material->ApplyShaderRenderMeta(renderMeta->cullMode, renderMeta->depthWrite, renderMeta->depthTest,
                                        renderMeta->blend, renderMeta->queue, renderMeta->passTag, renderMeta->stencil,
                                        renderMeta->alphaClip);
    }

    const ShaderStagePair stages{vertShaderName, fragShaderName};
    const auto *artifact = m_shaderCache.FindProgramArtifact(stages);
    const auto *forward = artifact ? artifact->FindVariant(ShaderCompileTarget::Forward) : nullptr;
    const auto *vertCode = forward ? &forward->vertexSpirv : m_shaderCache.FindVertCode(vertShaderName);
    const auto *fragCode = forward ? &forward->fragmentSpirv : m_shaderCache.FindFragCode(fragShaderName);
    const ShaderProgramKey programKey = artifact ? artifact->key : ShaderProgramKey{stages, 0};

    if (vertCode && fragCode && m_materialPipelineManagerInitialized) {
        VkDeviceSize sceneUboSize = sizeof(UniformBufferObject);
        VkDeviceSize lightingUboSize = sizeof(ShaderLightingUBO);
        auto *renderData = m_materialPipelineManager.GetOrCreateRenderDataWithReflection(
            material, *vertCode, *fragCode, programKey, sceneUbo, sceneUboSize, lightingUbo, lightingUboSize);

        bool forwardOk = renderData && renderData->isValid;

        if (forwardOk && artifact) {
            // Optional programs are materialized by their first real pass.
            for (const auto target :
                 {ShaderCompileTarget::GBuffer, ShaderCompileTarget::Shadow, ShaderCompileTarget::Depth,
                  ShaderCompileTarget::Picking, ShaderCompileTarget::Motion})
                material->SetPassShaderProgram(target, nullptr);
        }

        if (forwardOk && m_shadowPipelineReady) {
            // Shadow resources are created lazily by DrawShadowCasters. Eagerly
            // allocating here makes every transient/runtime material consume a
            // descriptor even when it never reaches a shadow pass, and repeated
            // 2D animation material refreshes should not touch shadow resources.
            // The next real shadow draw resolves the shared pipeline and binding cache.
            material->SetPassPipeline(ShaderCompileTarget::Shadow, VK_NULL_HANDLE);
            material->SetPassDescriptorSet(ShaderCompileTarget::Shadow, VK_NULL_HANDLE);
        }

        return forwardOk;
    }

    static std::unordered_set<std::string> reportedMissingPrograms;
    const std::string missingProgramKey =
        vertShaderName + "|" + fragShaderName + (m_materialPipelineManagerInitialized ? "|ready" : "|initializing");
    if (reportedMissingPrograms.insert(missingProgramKey).second) {
        INXLOG_WARN("RefreshMaterialPipeline: shader codes not found or MPM not initialized for '", material->GetName(),
                    "' (vert='", vertShaderName, "', frag='", fragShaderName, "')");
    }

    // Dump available shader keys for debugging
    static int dumpCount = 0;
    if (dumpCount++ < 2) {
        std::string vertKeys, fragKeys;
        m_shaderCache.DumpAvailableKeys(vertKeys, fragKeys);
        INXLOG_WARN("  Available vert shaders:", vertKeys);
        INXLOG_WARN("  Available frag shaders:", fragKeys);
        INXLOG_WARN("  MPM initialized: ", m_materialPipelineManagerInitialized ? "true" : "false",
                    ", vertCode found: ", (vertCode ? "yes" : "no"), ", fragCode found: ", (fragCode ? "yes" : "no"));
    }
    return false;
}

// ============================================================================
// Lighting System
// ============================================================================

void InxVkCoreModular::SetAmbientColor(const glm::vec3 &color, float intensity)
{
    m_lightCollector.SetAmbientColor(color, intensity);
    INXLOG_DEBUG("SetAmbientColor: (", color.r, ", ", color.g, ", ", color.b, ") intensity=", intensity);
}

void InxVkCoreModular::UpdateLightingUBO(const glm::vec3 &cameraPosition)
{
    // Delegate to StageLightingUBO — the actual GPU write now happens
    // inline in the command buffer via CmdUpdateLightingUBO().
    StageLightingUBO(cameraPosition);
}

void InxVkCoreModular::StageLightingUBO(const glm::vec3 &cameraPosition)
{
    // Sync ambient color from skybox material properties
    auto skyMat = AssetRegistry::Instance().GetBuiltinMaterial("SkyboxProcedural");
    if (skyMat) {
        const auto *skyTopProp = skyMat->GetProperty("skyTopColor");
        const auto *horizonProp = skyMat->GetProperty("skyHorizonColor");
        const auto *groundProp = skyMat->GetProperty("groundColor");
        const auto *exposureProp = skyMat->GetProperty("exposure");
        if (skyTopProp && groundProp) {
            glm::vec3 skyTop = glm::vec3(std::get<glm::vec4>(skyTopProp->value));
            glm::vec3 ground = glm::vec3(std::get<glm::vec4>(groundProp->value));
            glm::vec3 equator;
            if (horizonProp) {
                equator = glm::vec3(std::get<glm::vec4>(horizonProp->value));
            } else {
                equator = glm::mix(ground, skyTop, 0.5f);
            }
            float exposure = 0.8f;
            if (exposureProp) {
                exposure = std::get<float>(exposureProp->value);
            }
            m_lightCollector.SetAmbientGradient(skyTop * exposure, equator * exposure, ground * exposure);
        }
    }

    // Build the shader-compatible UBO from collected lights
    m_lightCollector.BuildShaderLightingUBO();
    m_stagedLightingUBO = m_lightCollector.GetShaderLightingUBO();
    m_stagedLightingUBO.cameraPos = glm::vec4(cameraPosition, 1.0f);
    m_lightingUBODirty = true;
}

void InxVkCoreModular::CmdUpdateLightingCameraPos(VkCommandBuffer cmdBuf, const glm::vec3 &cameraPos)
{
    if (!m_lightingUbo)
        return;

    VkBuffer buffer = m_lightingUbo->GetBuffer();

    // cameraPos sits at offset 32 in ShaderLightingUBO (after lightCounts + ambientColor).
    constexpr VkDeviceSize cameraPosOffset = offsetof(ShaderLightingUBO, cameraPos);
    glm::vec4 cameraPosVec4(cameraPos, 1.0f);

    vkrender::CmdBarrierUniformReadToTransferWrite(cmdBuf);

    vkCmdUpdateBuffer(cmdBuf, buffer, cameraPosOffset, sizeof(glm::vec4), &cameraPosVec4);

    vkrender::CmdBarrierTransferWriteToUniformRead(cmdBuf);
}

void InxVkCoreModular::CmdUpdateLightingUBO(VkCommandBuffer cmdBuf)
{
    if (!m_lightingUBODirty || !m_lightingUbo)
        return;

    VkBuffer buffer = m_lightingUbo->GetBuffer();

    // Barrier: ensure previous shader reads from the lighting UBO are complete
    vkrender::CmdBarrierUniformReadToTransferWrite(cmdBuf);

    // Update the lighting UBO inline in the command buffer
    // vkCmdUpdateBuffer has a 65536-byte limit; ShaderLightingUBO is well within that.
    vkCmdUpdateBuffer(cmdBuf, buffer, 0, sizeof(ShaderLightingUBO), &m_stagedLightingUBO);

    // Barrier: ensure write is visible before subsequent shader reads
    vkrender::CmdBarrierTransferWriteToUniformRead(cmdBuf);

    m_lightingUBODirty = false;
}

void InxVkCoreModular::CmdUpdateShadowDataForCamera(VkCommandBuffer cmdBuf, const glm::mat4 *lightVPs,
                                                    uint32_t cascadeCount, const float *cascadeSplits,
                                                    float mapResolution)
{
    if (!m_lightingUbo)
        return;

    VkBuffer buffer = m_lightingUbo->GetBuffer();

    // Build the shadow portion we need to patch
    glm::mat4 vpData[NUM_SHADOW_CASCADES];
    for (uint32_t i = 0; i < NUM_SHADOW_CASCADES; ++i)
        vpData[i] = (i < cascadeCount) ? lightVPs[i] : glm::mat4(1.0f);

    glm::vec4 splitVec(cascadeCount > 0 ? cascadeSplits[0] : 0.0f, cascadeCount > 1 ? cascadeSplits[1] : 0.0f,
                       cascadeCount > 2 ? cascadeSplits[2] : 0.0f, cascadeCount > 3 ? cascadeSplits[3] : 0.0f);

    float cascadeRes = mapResolution * 0.5f;
    glm::vec4 params(mapResolution, cascadeCount > 0 ? 1.0f : 0.0f, static_cast<float>(cascadeCount), cascadeRes);

    // Offsets into ShaderLightingUBO
    constexpr VkDeviceSize vpOffset = offsetof(ShaderLightingUBO, lightVP);
    constexpr VkDeviceSize splitOffset = offsetof(ShaderLightingUBO, shadowCascadeSplits);
    constexpr VkDeviceSize paramsOffset = offsetof(ShaderLightingUBO, shadowMapParams);

    // Barrier before writes
    vkrender::CmdBarrierUniformReadToTransferWrite(cmdBuf);

    vkCmdUpdateBuffer(cmdBuf, buffer, vpOffset, sizeof(vpData), vpData);
    vkCmdUpdateBuffer(cmdBuf, buffer, splitOffset, sizeof(glm::vec4), &splitVec);
    vkCmdUpdateBuffer(cmdBuf, buffer, paramsOffset, sizeof(glm::vec4), &params);

    // Also update per-cascade shadow UBOs (used by shadow caster rendering)
    const uint32_t frameIndex = m_currentFrame % m_maxFramesInFlight;
    struct ShadowUBO
    {
        glm::mat4 model, view, proj;
    };
    for (uint32_t ci = 0; ci < cascadeCount && ci < NUM_SHADOW_CASCADES; ++ci) {
        uint32_t bufIdx = frameIndex * NUM_SHADOW_CASCADES + ci;
        if (bufIdx >= m_shadowUboBuffers.size())
            break;
        VkBuffer shadowBuffer = m_shadowUboBuffers[bufIdx];
        if (shadowBuffer == VK_NULL_HANDLE)
            continue;
        ShadowUBO ubo{glm::mat4(1.0f), glm::mat4(1.0f), lightVPs[ci]};
        vkCmdUpdateBuffer(cmdBuf, shadowBuffer, 0, sizeof(ShadowUBO), &ubo);
    }

    // Barrier after writes
    vkrender::CmdBarrierTransferWriteToUniformRead(cmdBuf);
}

void InxVkCoreModular::CmdRestoreEditorShadowData(VkCommandBuffer cmdBuf)
{
    if (!m_lightingUbo)
        return;

    VkBuffer buffer = m_lightingUbo->GetBuffer();

    // Restore lightVP, cascade splits, and shadow params from the staged
    // editor lighting UBO that was prepared at the start of this frame.
    constexpr VkDeviceSize vpOffset = offsetof(ShaderLightingUBO, lightVP);
    constexpr VkDeviceSize splitOffset = offsetof(ShaderLightingUBO, shadowCascadeSplits);
    constexpr VkDeviceSize paramsOffset = offsetof(ShaderLightingUBO, shadowMapParams);

    vkrender::CmdBarrierUniformReadToTransferWrite(cmdBuf);

    vkCmdUpdateBuffer(cmdBuf, buffer, vpOffset, sizeof(m_stagedLightingUBO.lightVP), m_stagedLightingUBO.lightVP);
    vkCmdUpdateBuffer(cmdBuf, buffer, splitOffset, sizeof(glm::vec4), &m_stagedLightingUBO.shadowCascadeSplits);
    vkCmdUpdateBuffer(cmdBuf, buffer, paramsOffset, sizeof(glm::vec4), &m_stagedLightingUBO.shadowMapParams);

    // Also restore per-cascade shadow UBOs to editor camera VPs
    const uint32_t frameIndex = m_currentFrame % m_maxFramesInFlight;
    const uint32_t cascadeCount = m_lightCollector.GetShadowCascadeCount();
    struct ShadowUBO
    {
        glm::mat4 model, view, proj;
    };
    for (uint32_t ci = 0; ci < cascadeCount && ci < NUM_SHADOW_CASCADES; ++ci) {
        uint32_t bufIdx = frameIndex * NUM_SHADOW_CASCADES + ci;
        if (bufIdx >= m_shadowUboBuffers.size())
            break;
        VkBuffer shadowBuffer = m_shadowUboBuffers[bufIdx];
        if (shadowBuffer == VK_NULL_HANDLE)
            continue;
        ShadowUBO ubo{glm::mat4(1.0f), glm::mat4(1.0f), m_lightCollector.GetShadowLightVP(ci)};
        vkCmdUpdateBuffer(cmdBuf, shadowBuffer, 0, sizeof(ShadowUBO), &ubo);
    }

    vkrender::CmdBarrierTransferWriteToUniformRead(cmdBuf);
}

// ============================================================================
// Buffer / Shader Accessors (for OutlineRenderer)
// ============================================================================

VkBuffer InxVkCoreModular::GetObjectVertexBuffer(uint64_t objectId) const
{
    auto it = m_perObjectBuffers.find(objectId);
    if (it != m_perObjectBuffers.end() && it->second.vertexBuffer)
        return it->second.vertexBuffer->GetBuffer();
    return VK_NULL_HANDLE;
}

VkBuffer InxVkCoreModular::GetObjectIndexBuffer(uint64_t objectId) const
{
    auto it = m_perObjectBuffers.find(objectId);
    if (it != m_perObjectBuffers.end() && it->second.indexBuffer)
        return it->second.indexBuffer->GetBuffer();
    return VK_NULL_HANDLE;
}

VkBuffer InxVkCoreModular::GetSceneUbo() const
{
    return m_sceneUbo ? m_sceneUbo->GetBuffer() : VK_NULL_HANDLE;
}

VkBuffer InxVkCoreModular::GetLightingUbo() const
{
    return m_lightingUbo ? m_lightingUbo->GetBuffer() : VK_NULL_HANDLE;
}

VkBuffer InxVkCoreModular::GetInstanceSSBO(size_t index) const
{
    if (index < m_instanceBuffers.size() && m_instanceBuffers[index].buffer)
        return m_instanceBuffers[index].buffer->GetBuffer();
    return VK_NULL_HANDLE;
}

VkShaderModule InxVkCoreModular::GetShaderModule(const std::string &name, const std::string &type) const
{
    return m_shaderCache.GetModule(name, type);
}

// ============================================================================
// Shared shadow material bindings and pipeline creation
// ============================================================================

VkDescriptorPool InxVkCoreModular::CreateShadowMaterialDescriptorPoolPage(uint32_t maxSets)
{
    std::array<VkDescriptorPoolSize, 2> poolSizes{};
    poolSizes[0].type = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    poolSizes[0].descriptorCount = maxSets * 2;
    poolSizes[1].type = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
    poolSizes[1].descriptorCount = maxSets * kMaxShadowMaterialTextures;

    VkDescriptorPoolCreateInfo poolInfo{};
    poolInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
    poolInfo.flags = VK_DESCRIPTOR_POOL_CREATE_FREE_DESCRIPTOR_SET_BIT;
    poolInfo.maxSets = maxSets;
    poolInfo.poolSizeCount = static_cast<uint32_t>(poolSizes.size());
    poolInfo.pPoolSizes = poolSizes.data();

    VkDescriptorPool pool = VK_NULL_HANDLE;
    if (vkCreateDescriptorPool(GetDevice(), &poolInfo, nullptr, &pool) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to create shadow material descriptor pool page (sets=", maxSets, ")");
        return VK_NULL_HANDLE;
    }
    m_shadowMaterialDescPools.push_back(pool);
    return pool;
}

InxVkCoreModular::ShadowDescriptorAllocation InxVkCoreModular::AllocateShadowMaterialDescriptorSet()
{
    if (m_shadowMaterialDescSetLayout == VK_NULL_HANDLE)
        return {};

    auto allocateFrom = [&](VkDescriptorPool pool) {
        ShadowDescriptorAllocation allocation{};
        allocation.ownerPool = pool;
        VkDescriptorSetAllocateInfo allocInfo{};
        allocInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
        allocInfo.descriptorPool = pool;
        allocInfo.descriptorSetCount = 1;
        allocInfo.pSetLayouts = &m_shadowMaterialDescSetLayout;
        const VkResult result = vkAllocateDescriptorSets(GetDevice(), &allocInfo, &allocation.descriptorSet);
        if (result != VK_SUCCESS)
            allocation.descriptorSet = VK_NULL_HANDLE;
        return std::pair{allocation, result};
    };

    if (!m_shadowMaterialDescPools.empty()) {
        auto [allocation, result] = allocateFrom(m_shadowMaterialDescPools.back());
        if (result == VK_SUCCESS)
            return allocation;
        if (result != VK_ERROR_OUT_OF_POOL_MEMORY && result != VK_ERROR_FRAGMENTED_POOL) {
            INXLOG_WARN("Shadow material descriptor allocation failed: ", static_cast<int>(result));
            return {};
        }
    }

    VkDescriptorPool newPage = CreateShadowMaterialDescriptorPoolPage(kShadowMaterialPoolPageSize);
    if (newPage == VK_NULL_HANDLE)
        return {};
    auto [allocation, result] = allocateFrom(newPage);
    if (result != VK_SUCCESS) {
        INXLOG_ERROR("Shadow material descriptor allocation failed after growing pool chain: ",
                     static_cast<int>(result));
        return {};
    }
    return allocation;
}

bool InxVkCoreModular::EnsureShadowMaterialDummyDescriptorSet()
{
    if (m_shadowMaterialDummyDescSet != VK_NULL_HANDLE)
        return true;
    if (m_shadowMaterialDescSetLayout == VK_NULL_HANDLE)
        return false;
    auto defaultTex = m_textureCache.Find("white", m_ensureFrameCounter);
    if (!defaultTex || defaultTex->GetView() == VK_NULL_HANDLE || defaultTex->GetSampler() == VK_NULL_HANDLE)
        return false;
    if (!m_sceneUbo)
        return false;
    const ShadowDescriptorAllocation allocation = AllocateShadowMaterialDescriptorSet();
    if (allocation.descriptorSet == VK_NULL_HANDLE)
        return false;
    const VkDescriptorSet set = allocation.descriptorSet;
    std::vector<VkDescriptorImageInfo> imageInfos(kMaxShadowMaterialTextures);
    std::vector<VkWriteDescriptorSet> writes;
    writes.reserve(kMaxShadowMaterialTextures + 2);
    for (uint32_t i = 0; i < kMaxShadowMaterialTextures; ++i) {
        imageInfos[i].imageLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        imageInfos[i].imageView = defaultTex->GetView();
        imageInfos[i].sampler = defaultTex->GetSampler();
        VkWriteDescriptorSet w{};
        w.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        w.dstSet = set;
        w.dstBinding = i;
        w.descriptorCount = 1;
        w.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
        w.pImageInfo = &imageInfos[i];
        writes.push_back(w);
    }
    VkDescriptorBufferInfo fragBi{};
    fragBi.buffer = m_sceneUbo->GetBuffer();
    fragBi.offset = 0;
    fragBi.range = 16;
    VkWriteDescriptorSet wf{};
    wf.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    wf.dstSet = set;
    wf.dstBinding = kMaxShadowMaterialTextures;
    wf.descriptorCount = 1;
    wf.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    wf.pBufferInfo = &fragBi;
    VkDescriptorBufferInfo vtxBi{};
    vtxBi.buffer = m_sceneUbo->GetBuffer();
    vtxBi.offset = 0;
    vtxBi.range = 16;
    VkWriteDescriptorSet wv{};
    wv.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    wv.dstSet = set;
    wv.dstBinding = 14;
    wv.descriptorCount = 1;
    wv.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    wv.pBufferInfo = &vtxBi;
    writes.push_back(wf);
    writes.push_back(wv);
    vkUpdateDescriptorSets(GetDevice(), static_cast<uint32_t>(writes.size()), writes.data(), 0, nullptr);
    m_shadowMaterialDummyDescSet = set;
    return true;
}

void InxVkCoreModular::RetireShadowMaterialBinding(ShadowMaterialBindingEntry entry)
{
    if (entry.descriptorSet == VK_NULL_HANDLE || entry.descriptorSet == m_shadowMaterialDummyDescSet ||
        entry.ownerPool == VK_NULL_HANDLE) {
        return;
    }

    const VkDevice device = GetDevice();
    ++m_shadowMaterialBindingRetirements;
    m_deletionQueue.Push([device, pool = entry.ownerPool, descriptorSet = entry.descriptorSet,
                          keepAlive = std::move(entry.textureKeepAlive)]() mutable {
        const VkResult result = vkFreeDescriptorSets(device, pool, 1, &descriptorSet);
        if (result != VK_SUCCESS)
            INXLOG_WARN("Failed to retire cached shadow material descriptor set: ", static_cast<int>(result));
        keepAlive.clear();
    });
}

void InxVkCoreModular::CollectUnusedShadowMaterialBindings()
{
    for (auto it = m_shadowMaterialBindingCache.begin(); it != m_shadowMaterialBindingCache.end();) {
        if (!it->second.owner.expired()) {
            ++it;
            continue;
        }
        ShadowMaterialBindingEntry retired = std::move(it->second);
        it = m_shadowMaterialBindingCache.erase(it);
        RetireShadowMaterialBinding(std::move(retired));
    }
}

VkDescriptorSet InxVkCoreModular::EnsureShadowMaterialBinding(const std::shared_ptr<InxMaterial> &material,
                                                              const MaterialDescriptorSet *forwardMaterialDesc,
                                                              const ShaderProgram *forwardProgram,
                                                              const ShaderProgram *shadowProgram,
                                                              uint64_t artifactRevision)
{
    if (!material)
        return VK_NULL_HANDLE;

    const bool hasVertexMaterialUBO = shadowProgram ? shadowProgram->HasVertexMaterialUBO()
                                                    : (forwardProgram && forwardProgram->HasVertexMaterialUBO());
    const bool hasVertexMaterialTextures =
        shadowProgram &&
        std::any_of(shadowProgram->GetDescriptorBindings().begin(), shadowProgram->GetDescriptorBindings().end(),
                    [](const MergedDescriptorBinding &binding) {
                        return binding.set == 2 && binding.type == VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER &&
                               (binding.stageFlags & VK_SHADER_STAGE_VERTEX_BIT) != 0;
                    });
    const bool hasAlphaClip = material->GetRenderState().alphaClipEnabled;
    const bool needsDescriptor = hasVertexMaterialUBO || hasVertexMaterialTextures || hasAlphaClip;
    const InxMaterial *identity = material.get();

    if (!needsDescriptor) {
        auto existing = m_shadowMaterialBindingCache.find(identity);
        if (existing != m_shadowMaterialBindingCache.end()) {
            ShadowMaterialBindingEntry retired = std::move(existing->second);
            m_shadowMaterialBindingCache.erase(existing);
            RetireShadowMaterialBinding(std::move(retired));
        }
        return EnsureShadowMaterialDummyDescriptorSet() ? m_shadowMaterialDummyDescSet : VK_NULL_HANDLE;
    }

    if (!forwardMaterialDesc || !forwardMaterialDesc->isValid) {
        INXLOG_WARN("EnsureShadowMaterialBinding: forward material resources are unavailable for '",
                    material->GetName(), "'");
        return VK_NULL_HANDLE;
    }
    if (hasVertexMaterialUBO &&
        (!forwardMaterialDesc->vertexMaterialUBO || !forwardMaterialDesc->vertexMaterialUBO->IsValid())) {
        INXLOG_WARN("EnsureShadowMaterialBinding: missing vertex material UBO for '", material->GetName(), "'");
        return VK_NULL_HANDLE;
    }
    if (hasAlphaClip && (!forwardMaterialDesc->materialUBO || !forwardMaterialDesc->materialUBO->IsValid())) {
        INXLOG_WARN("EnsureShadowMaterialBinding: missing alpha-clip material UBO for '", material->GetName(), "'");
        return VK_NULL_HANDLE;
    }

    auto defaultTex = m_textureCache.Find("white", m_ensureFrameCounter);
    if (!defaultTex || defaultTex->GetView() == VK_NULL_HANDLE || defaultTex->GetSampler() == VK_NULL_HANDLE ||
        !m_sceneUbo) {
        return VK_NULL_HANDLE;
    }

    std::vector<std::pair<uint32_t, MaterialDescriptorSet::TextureBinding>> sortedTextures;
    if (hasAlphaClip || hasVertexMaterialTextures) {
        sortedTextures.assign(forwardMaterialDesc->textureBindings.begin(), forwardMaterialDesc->textureBindings.end());
        std::sort(sortedTextures.begin(), sortedTextures.end(),
                  [](const auto &left, const auto &right) { return left.first < right.first; });
        if (sortedTextures.size() > kMaxShadowMaterialTextures) {
            INXLOG_ERROR("EnsureShadowMaterialBinding: material '", material->GetName(), "' requires ",
                         sortedTextures.size(), " shadow texture slots, maximum is ", kMaxShadowMaterialTextures);
            return VK_NULL_HANDLE;
        }
    }

    size_t resourceSignature = 0;
    HashCombine(resourceSignature, artifactRevision);
    HashCombine(resourceSignature, hasVertexMaterialUBO ? 1u : 0u);
    HashCombine(resourceSignature, hasVertexMaterialTextures ? 1u : 0u);
    HashCombine(resourceSignature, hasAlphaClip ? 1u : 0u);
    HashCombine(resourceSignature, VulkanHandleBits(forwardMaterialDesc->descriptorSet));
    HashCombine(resourceSignature, VulkanHandleBits(defaultTex->GetView()));
    HashCombine(resourceSignature, VulkanHandleBits(defaultTex->GetSampler()));
    if (forwardMaterialDesc->materialUBO) {
        HashCombine(resourceSignature, VulkanHandleBits(forwardMaterialDesc->materialUBO->GetBuffer()));
        HashCombine(resourceSignature, forwardMaterialDesc->materialUBO->GetSize());
    }
    if (forwardMaterialDesc->vertexMaterialUBO) {
        HashCombine(resourceSignature, VulkanHandleBits(forwardMaterialDesc->vertexMaterialUBO->GetBuffer()));
        HashCombine(resourceSignature, forwardMaterialDesc->vertexMaterialUBO->GetSize());
    }
    for (const auto &[binding, texture] : sortedTextures) {
        HashCombine(resourceSignature, binding);
        HashCombine(resourceSignature, VulkanHandleBits(texture.imageView));
        HashCombine(resourceSignature, VulkanHandleBits(texture.sampler));
    }

    auto existing = m_shadowMaterialBindingCache.find(identity);
    if (existing != m_shadowMaterialBindingCache.end()) {
        const auto owner = existing->second.owner.lock();
        if (owner.get() == identity && existing->second.resourceSignature == resourceSignature &&
            existing->second.descriptorSet != VK_NULL_HANDLE) {
            existing->second.materialVersion = material->GetVersion();
            ++m_shadowMaterialBindingCacheHits;
            return existing->second.descriptorSet;
        }
    }

    ++m_shadowMaterialBindingCacheMisses;
    const ShadowDescriptorAllocation allocation = AllocateShadowMaterialDescriptorSet();
    if (allocation.descriptorSet == VK_NULL_HANDLE) {
        INXLOG_WARN("EnsureShadowMaterialBinding: descriptor allocation failed for '", material->GetName(), "'");
        return VK_NULL_HANDLE;
    }

    std::vector<VkDescriptorImageInfo> imageInfos(kMaxShadowMaterialTextures);
    for (auto &imageInfo : imageInfos) {
        imageInfo.imageLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
        imageInfo.imageView = defaultTex->GetView();
        imageInfo.sampler = defaultTex->GetSampler();
    }
    for (size_t index = 0; index < sortedTextures.size(); ++index) {
        const auto &texture = sortedTextures[index].second;
        if (texture.imageView != VK_NULL_HANDLE && texture.sampler != VK_NULL_HANDLE) {
            imageInfos[index].imageView = texture.imageView;
            imageInfos[index].sampler = texture.sampler;
        }
    }

    const MaterialUBO *fragmentUbo = hasAlphaClip ? forwardMaterialDesc->materialUBO.get() : nullptr;
    const MaterialUBO *vertexUbo = hasVertexMaterialUBO ? forwardMaterialDesc->vertexMaterialUBO.get() : nullptr;
    VkDescriptorBufferInfo fragmentBuffer{};
    fragmentBuffer.buffer =
        fragmentUbo ? fragmentUbo->GetBuffer() : (vertexUbo ? vertexUbo->GetBuffer() : m_sceneUbo->GetBuffer());
    fragmentBuffer.offset = 0;
    fragmentBuffer.range = fragmentUbo ? fragmentUbo->GetSize() : (vertexUbo ? vertexUbo->GetSize() : 16);
    VkDescriptorBufferInfo vertexBuffer{};
    vertexBuffer.buffer = vertexUbo ? vertexUbo->GetBuffer() : fragmentBuffer.buffer;
    vertexBuffer.offset = 0;
    vertexBuffer.range = vertexUbo ? vertexUbo->GetSize() : fragmentBuffer.range;

    std::vector<VkWriteDescriptorSet> writes;
    writes.reserve(kMaxShadowMaterialTextures + 2);
    for (uint32_t index = 0; index < kMaxShadowMaterialTextures; ++index) {
        VkWriteDescriptorSet write{};
        write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        write.dstSet = allocation.descriptorSet;
        write.dstBinding = index;
        write.descriptorCount = 1;
        write.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
        write.pImageInfo = &imageInfos[index];
        writes.push_back(write);
    }
    VkWriteDescriptorSet fragmentWrite{};
    fragmentWrite.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    fragmentWrite.dstSet = allocation.descriptorSet;
    fragmentWrite.dstBinding = kMaxShadowMaterialTextures;
    fragmentWrite.descriptorCount = 1;
    fragmentWrite.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    fragmentWrite.pBufferInfo = &fragmentBuffer;
    writes.push_back(fragmentWrite);
    VkWriteDescriptorSet vertexWrite{};
    vertexWrite.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    vertexWrite.dstSet = allocation.descriptorSet;
    vertexWrite.dstBinding = 14;
    vertexWrite.descriptorCount = 1;
    vertexWrite.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
    vertexWrite.pBufferInfo = &vertexBuffer;
    writes.push_back(vertexWrite);
    vkUpdateDescriptorSets(GetDevice(), static_cast<uint32_t>(writes.size()), writes.data(), 0, nullptr);

    ShadowMaterialBindingEntry replacement{};
    replacement.owner = material;
    replacement.materialKey = material->GetMaterialKey();
    replacement.materialVersion = material->GetVersion();
    replacement.artifactRevision = artifactRevision;
    replacement.resourceSignature = resourceSignature;
    replacement.descriptorSet = allocation.descriptorSet;
    replacement.ownerPool = allocation.ownerPool;
    replacement.textureKeepAlive.push_back(defaultTex);
    for (const auto &[binding, texture] : sortedTextures) {
        (void)binding;
        if (texture.keepAlive)
            replacement.textureKeepAlive.push_back(texture.keepAlive);
    }

    if (existing != m_shadowMaterialBindingCache.end()) {
        ShadowMaterialBindingEntry retired = std::move(existing->second);
        existing->second = std::move(replacement);
        RetireShadowMaterialBinding(std::move(retired));
    } else {
        m_shadowMaterialBindingCache.emplace(identity, std::move(replacement));
    }
    return allocation.descriptorSet;
}

VkDescriptorSet InxVkCoreModular::EnsureMaterialShadowPipeline(const std::shared_ptr<InxMaterial> &material,
                                                               const std::string &vertShaderName,
                                                               const std::string &fragShaderName)
{
    // Shared shadow resources must be ready
    if (m_shadowCompatRenderPass == VK_NULL_HANDLE || m_shadowPipelineLayout == VK_NULL_HANDLE)
        return VK_NULL_HANDLE;

    if (!material)
        return VK_NULL_HANDLE;

    VkDevice device = GetDevice();

    std::string materialKey = material->GetMaterialKey();
    if (materialKey.empty()) {
        materialKey = material->GetName();
    }

    MaterialRenderData *forwardRenderData = m_materialPipelineManager.GetRenderData(materialKey);
    MaterialDescriptorSet *forwardMaterialDesc = forwardRenderData ? forwardRenderData->materialDescSet : nullptr;
    ShaderProgram *forwardProgram = forwardRenderData ? forwardRenderData->shaderProgram : nullptr;
    const ShaderStagePair stagePair{vertShaderName, fragShaderName};
    const ShaderProgramArtifact *linkedArtifact = m_shaderCache.FindProgramArtifact(stagePair);
    ShaderProgram *linkedShadowProgram = nullptr;
    if (linkedArtifact && linkedArtifact->FindVariant(ShaderCompileTarget::Shadow)) {
        linkedShadowProgram = m_shaderCache.MaterializeProgramVariant(stagePair, ShaderCompileTarget::Shadow);
    }
    material->SetPassDescriptorSet(ShaderCompileTarget::Shadow, VK_NULL_HANDLE);

    // Structured programs consume the semantic Shadow variant directly. The
    // suffix lookup remains only for legacy independently compiled stages.
    std::string shadowVertName = vertShaderName + "/shadow";
    std::string shadowFragName = fragShaderName + "/shadow";

    VkShaderModule vertModule =
        linkedShadowProgram ? linkedShadowProgram->GetVertexModule() : GetShaderModule(shadowVertName, "vertex");
    if (vertModule == VK_NULL_HANDLE && !linkedArtifact)
        vertModule = GetShaderModule(vertShaderName, "vertex");

    VkShaderModule fragModule =
        linkedShadowProgram ? linkedShadowProgram->GetFragmentModule() : GetShaderModule(shadowFragName, "fragment");
    if (fragModule == VK_NULL_HANDLE) {
        static std::unordered_set<std::string> s_warnedShadowFragShaders;
        const std::string missingName = linkedArtifact ? linkedArtifact->key.ToString() + ":Shadow" : shadowFragName;
        if (s_warnedShadowFragShaders.insert(missingName).second) {
            INXLOG_WARN("EnsureMaterialShadowPipeline: missing shadow fragment program '", missingName,
                        "' — materials using this shader will use default shadow pass");
        }
        return VK_NULL_HANDLE;
    }

    if (vertModule == VK_NULL_HANDLE || fragModule == VK_NULL_HANDLE) {
        static int s_missingShadowModuleWarnCount = 0;
        if (s_missingShadowModuleWarnCount++ < 16) {
            INXLOG_WARN("EnsureMaterialShadowPipeline: shader modules unavailable for material '", material->GetName(),
                        "' (vert='", shadowVertName, "' fallback='", vertShaderName, "', frag='", shadowFragName, "')");
        }
        return VK_NULL_HANDLE;
    }

    // ---- Shadow pipeline cache: share VkPipeline across materials with same shader + cull mode ----
    VkCullModeFlags matCullMode = material->GetRenderState().cullMode;
    std::string shadowShaderKey = vertShaderName + "|" + fragShaderName + "|";
    shadowShaderKey += linkedArtifact ? std::to_string(linkedArtifact->key.revision) + ":Shadow" : "legacy-shadow";
    shadowShaderKey += "|cull" + std::to_string(matCullMode);
    auto cacheIt = m_shadowPipelineCache.find(shadowShaderKey);
    if (cacheIt != m_shadowPipelineCache.end()) {
        material->SetPassPipeline(ShaderCompileTarget::Shadow, cacheIt->second);
        material->SetPassPipelineLayout(ShaderCompileTarget::Shadow, m_shadowPipelineLayout);
        material->SetPassShaderProgram(ShaderCompileTarget::Shadow, linkedShadowProgram);
        return EnsureShadowMaterialBinding(material, forwardMaterialDesc, forwardProgram, linkedShadowProgram,
                                           linkedArtifact ? linkedArtifact->key.revision : 0);
    }

    // Shader stages
    auto shaderStages = vkrender::MakeVertFragStages(vertModule, fragModule);

    // Vertex input — only attributes consumed by the shadow vertex shader (full mesh buffer still bound).
    auto bindingDesc = Vertex::getBindingDescription();
    ShaderReflection shadowVertRefl;
    bool haveVertRefl = linkedShadowProgram != nullptr;
    if (linkedShadowProgram)
        shadowVertRefl = linkedShadowProgram->GetVertexReflection();
    if (!haveVertRefl) {
        if (const auto *spv = m_shaderCache.FindVertCode(shadowVertName))
            haveVertRefl = shadowVertRefl.Reflect(*spv, VK_SHADER_STAGE_VERTEX_BIT);
    }
    if (!haveVertRefl && !linkedArtifact) {
        if (const auto *spv = m_shaderCache.FindVertCode(vertShaderName))
            haveVertRefl = shadowVertRefl.Reflect(*spv, VK_SHADER_STAGE_VERTEX_BIT);
    }
    if (!haveVertRefl && forwardProgram != nullptr && vertModule == forwardProgram->GetVertexModule()) {
        shadowVertRefl = forwardProgram->GetVertexReflection();
    }
    std::vector<VkVertexInputAttributeDescription> attrDescs = FilterVertexAttributesForReflection(shadowVertRefl);

    VkPipelineVertexInputStateCreateInfo vertexInput{};
    vertexInput.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;
    vertexInput.vertexBindingDescriptionCount = attrDescs.empty() ? 0u : 1u;
    vertexInput.pVertexBindingDescriptions = attrDescs.empty() ? nullptr : &bindingDesc;
    vertexInput.vertexAttributeDescriptionCount = static_cast<uint32_t>(attrDescs.size());
    vertexInput.pVertexAttributeDescriptions = attrDescs.empty() ? nullptr : attrDescs.data();

    auto inputAssembly = vkrender::MakeTriangleListInputAssembly();

    vkrender::DynamicViewportScissorState dynVpScissor;

    // Rasterization: use material cull mode + depth bias
    VkPipelineRasterizationStateCreateInfo rasterizer{};
    rasterizer.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;
    rasterizer.polygonMode = VK_POLYGON_MODE_FILL;
    rasterizer.lineWidth = 1.0f;
    // Respect the material's cull mode so double-sided surfaces (cull=None)
    // cast shadows from both faces.  Default front-face culling reduces
    // shadow acne for single-sided geometry.
    rasterizer.cullMode = (matCullMode == VK_CULL_MODE_NONE) ? VK_CULL_MODE_NONE : VK_CULL_MODE_FRONT_BIT;
    rasterizer.frontFace = VK_FRONT_FACE_CLOCKWISE;
    rasterizer.depthBiasEnable = VK_TRUE;
    rasterizer.depthBiasConstantFactor = 1.5f;
    rasterizer.depthBiasSlopeFactor = 1.0f;
    rasterizer.depthBiasClamp = 0.01f;

    auto multisampling = vkrender::MakeMultisampleState();

    VkPipelineDepthStencilStateCreateInfo depthStencil{};
    depthStencil.sType = VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;
    depthStencil.depthTestEnable = VK_TRUE;
    depthStencil.depthWriteEnable = VK_TRUE;
    depthStencil.depthCompareOp = VK_COMPARE_OP_LESS_OR_EQUAL;

    VkPipelineColorBlendStateCreateInfo colorBlend{};
    colorBlend.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO;
    colorBlend.attachmentCount = 0;

    VkGraphicsPipelineCreateInfo pipelineInfo{};
    pipelineInfo.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;
    pipelineInfo.stageCount = static_cast<uint32_t>(shaderStages.size());
    pipelineInfo.pStages = shaderStages.data();
    pipelineInfo.pVertexInputState = &vertexInput;
    pipelineInfo.pInputAssemblyState = &inputAssembly;
    pipelineInfo.pViewportState = &dynVpScissor.viewportState;
    pipelineInfo.pRasterizationState = &rasterizer;
    pipelineInfo.pMultisampleState = &multisampling;
    pipelineInfo.pDepthStencilState = &depthStencil;
    pipelineInfo.pColorBlendState = &colorBlend;
    pipelineInfo.pDynamicState = &dynVpScissor.dynamicState;
    pipelineInfo.layout = m_shadowPipelineLayout;
    pipelineInfo.renderPass = m_shadowCompatRenderPass;
    pipelineInfo.subpass = 0;

    VkPipeline shadowPipeline = VK_NULL_HANDLE;
    VkPipelineCache pipelineCache = m_materialPipelineManager.GetVkPipelineCache();
    if (vkCreateGraphicsPipelines(device, pipelineCache, 1, &pipelineInfo, nullptr, &shadowPipeline) != VK_SUCCESS) {
        INXLOG_WARN("Failed to create shared shadow pipeline for '", material->GetName(), "' (vert='", shadowVertName,
                    "', frag='", shadowFragName, "')");
        return VK_NULL_HANDLE;
    }

    m_shadowPipelineCache[shadowShaderKey] = shadowPipeline;
    material->SetPassPipeline(ShaderCompileTarget::Shadow, shadowPipeline);
    material->SetPassPipelineLayout(ShaderCompileTarget::Shadow, m_shadowPipelineLayout);
    material->SetPassShaderProgram(ShaderCompileTarget::Shadow, linkedShadowProgram);
    INXLOG_DEBUG("Created shared shadow pipeline for '", material->GetName(), "'");
    return EnsureShadowMaterialBinding(material, forwardMaterialDesc, forwardProgram, linkedShadowProgram,
                                       linkedArtifact ? linkedArtifact->key.revision : 0);
}

// ============================================================================
// GPU Material Preview
// ============================================================================

std::shared_ptr<vk::ImageReadbackTicket>
InxVkCoreModular::BeginMaterialPreviewGPU(const std::shared_ptr<InxMaterial> &material, int size)
{
    if (!material || size <= 0 || !m_materialPipelineManagerInitialized)
        return nullptr;

    if (!m_gpuMaterialPreview)
        m_gpuMaterialPreview = std::make_unique<GPUMaterialPreview>(this);

    return m_gpuMaterialPreview->BeginRenderToPixels(*material, size);
}

bool InxVkCoreModular::TryCompleteMaterialPreviewGPU(const std::shared_ptr<vk::ImageReadbackTicket> &ticket,
                                                     int outputSize, std::vector<unsigned char> &outPixels)
{
    if (!m_gpuMaterialPreview)
        return false;
    return m_gpuMaterialPreview->TryCompleteRenderToPixels(ticket, outputSize, outPixels);
}

std::shared_ptr<vk::ImageReadbackTicket>
InxVkCoreModular::BeginMeshPreviewGPU(const InxMesh &mesh, const std::vector<std::shared_ptr<InxMaterial>> &materials,
                                      int size)
{
    if (size <= 0 || !m_materialPipelineManagerInitialized)
        return nullptr;

    if (!m_gpuMeshPreview)
        m_gpuMeshPreview = std::make_unique<GPUMeshPreview>(this);

    return m_gpuMeshPreview->BeginRenderToPixels(mesh, materials, size);
}

bool InxVkCoreModular::TryCompleteMeshPreviewGPU(const std::shared_ptr<vk::ImageReadbackTicket> &ticket, int outputSize,
                                                 std::vector<unsigned char> &outPixels)
{
    if (!m_gpuMeshPreview)
        return false;
    return m_gpuMeshPreview->TryCompleteRenderToPixels(ticket, outputSize, outPixels);
}

uint64_t InxVkCoreModular::RenderMeshPreviewGPUImGuiCamera(const InxMesh &mesh,
                                                           const std::vector<std::shared_ptr<InxMaterial>> &materials,
                                                           int size, const glm::mat4 &view, const glm::mat4 &proj,
                                                           const glm::vec3 &cameraPos, bool cloneMaterials)
{
    if (size <= 0 || !m_materialPipelineManagerInitialized)
        return 0;

    if (!m_gpuMeshPreview)
        m_gpuMeshPreview = std::make_unique<GPUMeshPreview>(this);

    return m_gpuMeshPreview->RenderToImGuiTextureCamera(mesh, materials, size, view, proj, cameraPos, cloneMaterials);
}

} // namespace infernux
