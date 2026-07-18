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
#include "VertexInputFilter.h"
#include "gui/GPUMaterialPreview.h"
#include "gui/GPUMeshPreview.h"
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
#include <glm/glm.hpp>
#include <set>
#include <unordered_set>

#include <cstring>

namespace infernux
{

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

    if (m_materialPipelineManagerInitialized && material.GetPassShaderProgram(ShaderCompileTarget::Forward) &&
        material.GetPassDescriptorSet(ShaderCompileTarget::Forward) != VK_NULL_HANDLE) {
        m_materialPipelineManager.UpdateMaterialProperties(material.GetMaterialKey(), material);
        material.ClearPropertiesDirty();
        return;
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
    const auto *vertCode = artifact ? &artifact->vertexSpirv : m_shaderCache.FindVertCode(vertShaderName);
    const auto *fragCode = artifact ? &artifact->fragmentSpirv : m_shaderCache.FindFragCode(fragShaderName);
    const ShaderProgramKey programKey = artifact ? artifact->key : ShaderProgramKey{stages, 0};

    if (vertCode && fragCode && m_materialPipelineManagerInitialized) {
        VkDeviceSize sceneUboSize = sizeof(UniformBufferObject);
        VkDeviceSize lightingUboSize = sizeof(ShaderLightingUBO);
        auto *renderData = m_materialPipelineManager.GetOrCreateRenderDataWithReflection(
            material, *vertCode, *fragCode, programKey, sceneUbo, sceneUboSize, lightingUbo, lightingUboSize);

        bool forwardOk = renderData && renderData->isValid;

        if (forwardOk && m_shadowPipelineReady) {
            // Shadow resources are created lazily by DrawShadowCasters. Eagerly
            // allocating here makes every transient/runtime material consume a
            // descriptor even when it never reaches a shadow pass, and repeated
            // 2D animation material refreshes eventually exhaust the fixed pool.
            // The next real shadow draw will rebuild the pass if needed.
            material->SetPassPipeline(ShaderCompileTarget::Shadow, VK_NULL_HANDLE);
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
// Per-material shadow pipeline creation
// ============================================================================

bool InxVkCoreModular::EnsureShadowMaterialDummyDescriptorSet()
{
    if (m_shadowMaterialDummyDescSet != VK_NULL_HANDLE)
        return true;
    if (m_shadowMaterialDescPool == VK_NULL_HANDLE || m_shadowMaterialDescSetLayout == VK_NULL_HANDLE)
        return false;
    VkDevice device = GetDevice();
    auto defaultTex = m_textureCache.Find("white", m_ensureFrameCounter);
    if (!defaultTex || defaultTex->GetView() == VK_NULL_HANDLE || defaultTex->GetSampler() == VK_NULL_HANDLE)
        return false;
    if (!m_sceneUbo)
        return false;
    VkDescriptorSetAllocateInfo allocInfo{};
    allocInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
    allocInfo.descriptorPool = m_shadowMaterialDescPool;
    allocInfo.descriptorSetCount = 1;
    allocInfo.pSetLayouts = &m_shadowMaterialDescSetLayout;
    VkDescriptorSet set = VK_NULL_HANDLE;
    if (vkAllocateDescriptorSets(device, &allocInfo, &set) != VK_SUCCESS)
        return false;
    static constexpr uint32_t kMaxShadowTextures = 8;
    std::vector<VkDescriptorImageInfo> imageInfos(kMaxShadowTextures);
    std::vector<VkWriteDescriptorSet> writes;
    writes.reserve(kMaxShadowTextures + 2);
    for (uint32_t i = 0; i < kMaxShadowTextures; ++i) {
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
    wf.dstBinding = kMaxShadowTextures;
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
    vkUpdateDescriptorSets(device, static_cast<uint32_t>(writes.size()), writes.data(), 0, nullptr);
    m_shadowMaterialDummyDescSet = set;
    return true;
}

void InxVkCoreModular::CreateMaterialShadowPipeline(std::shared_ptr<InxMaterial> material,
                                                    const std::string &vertShaderName,
                                                    const std::string &fragShaderName)
{
    // Shared shadow resources must be ready
    if (m_shadowCompatRenderPass == VK_NULL_HANDLE || m_shadowPipelineLayout == VK_NULL_HANDLE)
        return;

    VkDevice device = GetDevice();

    std::string materialKey = material->GetMaterialKey();
    if (materialKey.empty()) {
        materialKey = material->GetName();
    }

    MaterialRenderData *forwardRenderData = m_materialPipelineManager.GetRenderData(materialKey);
    MaterialDescriptorSet *forwardMaterialDesc = forwardRenderData ? forwardRenderData->materialDescSet : nullptr;
    ShaderProgram *forwardProgram = forwardRenderData ? forwardRenderData->shaderProgram : nullptr;
    bool hasVertexMaterialUBO = forwardProgram && forwardProgram->HasVertexMaterialUBO();
    bool hasAlphaClip = material->GetRenderState().alphaClipEnabled;
    bool needsShadowMaterialDesc = hasVertexMaterialUBO || hasAlphaClip;

    auto retireOldShadowDescriptorSet = [&](VkDescriptorSet descriptorSet) {
        if (descriptorSet == VK_NULL_HANDLE || descriptorSet == m_shadowMaterialDummyDescSet ||
            m_shadowMaterialDescPool == VK_NULL_HANDLE) {
            return;
        }

        // Runtime texture/material invalidation can recreate shadow descriptor
        // sets while recorded command buffers still reference older handles.
        // Reclaim after all frames that could reference the set have retired.
        VkDevice retireDevice = device;
        VkDescriptorPool retirePool = m_shadowMaterialDescPool;
        m_deletionQueue.Push([retireDevice, retirePool, descriptorSet] {
            VkResult result = vkFreeDescriptorSets(retireDevice, retirePool, 1, &descriptorSet);
            if (result != VK_SUCCESS)
                INXLOG_WARN("Failed to retire shadow material descriptor set: ", static_cast<int>(result));
        });
    };

    if (needsShadowMaterialDesc) {
        if (hasVertexMaterialUBO && (!forwardMaterialDesc || !forwardMaterialDesc->vertexMaterialUBO ||
                                     !forwardMaterialDesc->vertexMaterialUBO->IsValid())) {
            INXLOG_WARN("CreateMaterialShadowPipeline: missing forward vertex material UBO for material '",
                        material->GetName(), "'");
            return;
        }

        VkDescriptorSet oldShadowDescSet = material->GetPassDescriptorSet(ShaderCompileTarget::Shadow);
        if (oldShadowDescSet != VK_NULL_HANDLE && oldShadowDescSet != m_shadowMaterialDummyDescSet) {

            retireOldShadowDescriptorSet(oldShadowDescSet);
            material->SetPassDescriptorSet(ShaderCompileTarget::Shadow, VK_NULL_HANDLE);
        }

        VkDescriptorSetAllocateInfo allocInfo{};
        allocInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
        allocInfo.descriptorPool = m_shadowMaterialDescPool;
        allocInfo.descriptorSetCount = 1;
        allocInfo.pSetLayouts = &m_shadowMaterialDescSetLayout;

        VkDescriptorSet shadowMaterialDescSet = VK_NULL_HANDLE;
        if (vkAllocateDescriptorSets(device, &allocInfo, &shadowMaterialDescSet) != VK_SUCCESS) {
            INXLOG_WARN("CreateMaterialShadowPipeline: failed to allocate shadow material descriptor set for '",
                        material->GetName(), "'");
            return;
        }

        std::vector<VkWriteDescriptorSet> writes;
        // Keep descriptor infos alive until vkUpdateDescriptorSets.
        // We allocate max capacity upfront to prevent reallocation (which
        // would invalidate pointers stored in VkWriteDescriptorSet).
        std::vector<VkDescriptorBufferInfo> bufferInfos;
        std::vector<VkDescriptorImageInfo> imageInfos;

        // Shadow material desc layout has up to 8 texture slots + 2 UBOs.
        // We must write ALL declared bindings to avoid validation errors
        // (Vulkan requires every binding to be updated unless PARTIALLY_BOUND).
        static constexpr uint32_t kMaxShadowTextures = 8;
        bufferInfos.reserve(4);                 // vtx UBO + frag UBO + up to 2 dummies
        imageInfos.reserve(kMaxShadowTextures); // up to 8 texture slots

        // Collect sorted texture bindings for alpha-clip (needed for both
        // texture writes and to determine fragment MaterialProperties binding)
        std::vector<std::pair<uint32_t, MaterialDescriptorSet::TextureBinding>> sortedTexBindings;
        uint32_t shadowTexCount = 0;
        if (hasAlphaClip && forwardMaterialDesc) {
            sortedTexBindings.assign(forwardMaterialDesc->textureBindings.begin(),
                                     forwardMaterialDesc->textureBindings.end());
            std::sort(sortedTexBindings.begin(), sortedTexBindings.end(),
                      [](const auto &a, const auto &b) { return a.first < b.first; });
        }

        // --- Phase 1: collect all buffer/image infos (no pointer-taking yet) ---

        // (a) Vertex MaterialProperties UBO at binding 14
        size_t vtxUboInfoIdx = SIZE_MAX;
        if (hasVertexMaterialUBO && forwardMaterialDesc && forwardMaterialDesc->vertexMaterialUBO &&
            forwardMaterialDesc->vertexMaterialUBO->IsValid()) {
            vtxUboInfoIdx = bufferInfos.size();
            VkDescriptorBufferInfo bi{};
            bi.buffer = forwardMaterialDesc->vertexMaterialUBO->GetBuffer();
            bi.offset = 0;
            bi.range = forwardMaterialDesc->vertexMaterialUBO->GetSize();
            bufferInfos.push_back(bi);
        }

        // (b) Alpha-clip textures (bindings 0..N-1)
        std::vector<uint32_t> texShadowBindings;
        for (const auto &[fwdBinding, texBinding] : sortedTexBindings) {
            if (texBinding.imageView == VK_NULL_HANDLE || texBinding.sampler == VK_NULL_HANDLE)
                continue;
            VkDescriptorImageInfo ii{};
            ii.imageLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
            ii.imageView = texBinding.imageView;
            ii.sampler = texBinding.sampler;
            texShadowBindings.push_back(shadowTexCount);
            imageInfos.push_back(ii);
            ++shadowTexCount;
        }

        // (c) Fragment MaterialProperties UBO at binding = shadowTexCount
        size_t fragUboInfoIdx = SIZE_MAX;
        if (hasAlphaClip && forwardMaterialDesc && forwardMaterialDesc->materialUBO &&
            forwardMaterialDesc->materialUBO->IsValid()) {
            fragUboInfoIdx = bufferInfos.size();
            VkDescriptorBufferInfo bi{};
            bi.buffer = forwardMaterialDesc->materialUBO->GetBuffer();
            bi.offset = 0;
            bi.range = forwardMaterialDesc->materialUBO->GetSize();
            bufferInfos.push_back(bi);
        }

        // --- Phase 2: build VkWriteDescriptorSet with stable pointers ---

        if (vtxUboInfoIdx != SIZE_MAX) {
            VkWriteDescriptorSet w{};
            w.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            w.dstSet = shadowMaterialDescSet;
            w.dstBinding = 14;
            w.descriptorCount = 1;
            w.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
            w.pBufferInfo = &bufferInfos[vtxUboInfoIdx];
            writes.push_back(w);
        }

        for (size_t ti = 0; ti < texShadowBindings.size(); ++ti) {
            VkWriteDescriptorSet w{};
            w.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            w.dstSet = shadowMaterialDescSet;
            w.dstBinding = texShadowBindings[ti];
            w.descriptorCount = 1;
            w.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
            w.pImageInfo = &imageInfos[ti];
            writes.push_back(w);
        }

        if (fragUboInfoIdx != SIZE_MAX) {
            VkWriteDescriptorSet w{};
            w.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            w.dstSet = shadowMaterialDescSet;
            w.dstBinding = 8; // Must match kMaxShadowTextures in EnsureShadowPipeline layout
            w.descriptorCount = 1;
            w.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
            w.pBufferInfo = &bufferInfos[fragUboInfoIdx];
            writes.push_back(w);
        }

        // --- Phase 3: fill unused bindings with defaults to satisfy validation ---
        // Vulkan requires every binding in the layout to be updated before use.
        // Use the default white texture for unused sampler slots and a dummy
        // buffer for the fragment UBO slot if it wasn't written.
        auto defaultTex = m_textureCache.Find("white", m_ensureFrameCounter);
        if (defaultTex) {
            std::set<uint32_t> writtenTexBindings(texShadowBindings.begin(), texShadowBindings.end());
            for (uint32_t i = 0; i < kMaxShadowTextures; ++i) {
                if (writtenTexBindings.count(i))
                    continue;
                VkDescriptorImageInfo ii{};
                ii.imageLayout = VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL;
                ii.imageView = defaultTex->GetView();
                ii.sampler = defaultTex->GetSampler();
                imageInfos.push_back(ii);

                VkWriteDescriptorSet w{};
                w.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
                w.dstSet = shadowMaterialDescSet;
                w.dstBinding = i;
                w.descriptorCount = 1;
                w.descriptorType = VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER;
                w.pImageInfo = &imageInfos.back();
                writes.push_back(w);
            }
        }

        // Fragment UBO at binding 8: if not written, use the vertex UBO buffer
        // as a dummy (the shader won't actually read it for non-alpha-clip).
        if (fragUboInfoIdx == SIZE_MAX && vtxUboInfoIdx != SIZE_MAX) {
            // Reuse vertex UBO buffer info as a valid placeholder
            VkDescriptorBufferInfo dummyBi{};
            dummyBi.buffer = bufferInfos[vtxUboInfoIdx].buffer;
            dummyBi.offset = 0;
            dummyBi.range = bufferInfos[vtxUboInfoIdx].range;
            bufferInfos.push_back(dummyBi);

            VkWriteDescriptorSet w{};
            w.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            w.dstSet = shadowMaterialDescSet;
            w.dstBinding = 8;
            w.descriptorCount = 1;
            w.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
            w.pBufferInfo = &bufferInfos.back();
            writes.push_back(w);
        } else if (fragUboInfoIdx == SIZE_MAX && m_sceneUbo) {
            // Fallback: use scene UBO as a valid placeholder
            VkDescriptorBufferInfo dummyBi{};
            dummyBi.buffer = m_sceneUbo->GetBuffer();
            dummyBi.offset = 0;
            dummyBi.range = 16; // minimum valid range
            bufferInfos.push_back(dummyBi);

            VkWriteDescriptorSet w{};
            w.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
            w.dstSet = shadowMaterialDescSet;
            w.dstBinding = 8;
            w.descriptorCount = 1;
            w.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
            w.pBufferInfo = &bufferInfos.back();
            writes.push_back(w);
        }

        // Vertex UBO at binding 14: if not written, use a dummy buffer
        if (vtxUboInfoIdx == SIZE_MAX) {
            VkBuffer dummyBuf = VK_NULL_HANDLE;
            VkDeviceSize dummyRange = 16;
            if (fragUboInfoIdx != SIZE_MAX) {
                dummyBuf = bufferInfos[fragUboInfoIdx].buffer;
                dummyRange = bufferInfos[fragUboInfoIdx].range;
            } else if (m_sceneUbo) {
                dummyBuf = m_sceneUbo->GetBuffer();
            }
            if (dummyBuf != VK_NULL_HANDLE) {
                VkDescriptorBufferInfo dummyBi{};
                dummyBi.buffer = dummyBuf;
                dummyBi.offset = 0;
                dummyBi.range = dummyRange;
                bufferInfos.push_back(dummyBi);

                VkWriteDescriptorSet w{};
                w.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
                w.dstSet = shadowMaterialDescSet;
                w.dstBinding = 14;
                w.descriptorCount = 1;
                w.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
                w.pBufferInfo = &bufferInfos.back();
                writes.push_back(w);
            }
        }

        if (!writes.empty()) {
            vkUpdateDescriptorSets(device, static_cast<uint32_t>(writes.size()), writes.data(), 0, nullptr);
        }

        material->SetPassDescriptorSet(ShaderCompileTarget::Shadow, shadowMaterialDescSet);
    } else {
        VkDescriptorSet old = material->GetPassDescriptorSet(ShaderCompileTarget::Shadow);
        if (old != VK_NULL_HANDLE && old != m_shadowMaterialDummyDescSet) {
            retireOldShadowDescriptorSet(old);
        }
        if (EnsureShadowMaterialDummyDescriptorSet()) {
            material->SetPassDescriptorSet(ShaderCompileTarget::Shadow, m_shadowMaterialDummyDescSet);
        } else {
            material->SetPassDescriptorSet(ShaderCompileTarget::Shadow, VK_NULL_HANDLE);
        }
    }

    // Vertex shader: prefer shadow vertex variant, fall back to forward pass vertex shader
    std::string shadowVertName = vertShaderName + "/shadow";
    std::string shadowFragName = fragShaderName + "/shadow";

    VkShaderModule vertModule = GetShaderModule(shadowVertName, "vertex");
    if (vertModule == VK_NULL_HANDLE)
        vertModule = GetShaderModule(vertShaderName, "vertex");

    // Fragment shader: only create a per-material shadow pipeline when a
    // generated shadow variant exists for this material.
    VkShaderModule fragModule = GetShaderModule(shadowFragName, "fragment");
    if (fragModule == VK_NULL_HANDLE) {
        static std::unordered_set<std::string> s_warnedShadowFragShaders;
        if (s_warnedShadowFragShaders.insert(shadowFragName).second) {
            INXLOG_WARN("CreateMaterialShadowPipeline: missing shadow fragment module '", shadowFragName,
                        "' — materials using this shader will use default shadow pass");
        }
        return;
    }

    if (vertModule == VK_NULL_HANDLE || fragModule == VK_NULL_HANDLE) {
        static int s_missingShadowModuleWarnCount = 0;
        if (s_missingShadowModuleWarnCount++ < 16) {
            INXLOG_WARN("CreateMaterialShadowPipeline: shader modules unavailable for material '", material->GetName(),
                        "' (vert='", shadowVertName, "' fallback='", vertShaderName, "', frag='", shadowFragName, "')");
        }
        return;
    }

    // ---- Shadow pipeline cache: share VkPipeline across materials with same shader + cull mode ----
    VkCullModeFlags matCullMode = material->GetRenderState().cullMode;
    std::string shadowShaderKey = shadowVertName + "|" + shadowFragName + "|cull" + std::to_string(matCullMode);
    auto cacheIt = m_shadowPipelineCache.find(shadowShaderKey);
    if (cacheIt != m_shadowPipelineCache.end()) {
        material->SetPassPipeline(ShaderCompileTarget::Shadow, cacheIt->second);
        return;
    }

    // Shader stages
    auto shaderStages = vkrender::MakeVertFragStages(vertModule, fragModule);

    // Vertex input — only attributes consumed by the shadow vertex shader (full mesh buffer still bound).
    auto bindingDesc = Vertex::getBindingDescription();
    ShaderReflection shadowVertRefl;
    bool haveVertRefl = false;
    if (const auto *spv = m_shaderCache.FindVertCode(shadowVertName)) {
        haveVertRefl = shadowVertRefl.Reflect(*spv, VK_SHADER_STAGE_VERTEX_BIT);
    }
    if (!haveVertRefl) {
        if (const auto *spv = m_shaderCache.FindVertCode(vertShaderName)) {
            haveVertRefl = shadowVertRefl.Reflect(*spv, VK_SHADER_STAGE_VERTEX_BIT);
        }
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
        INXLOG_WARN("Failed to create per-material shadow pipeline for '", material->GetName(), "' (vert='",
                    shadowVertName, "', frag='", shadowFragName, "')");
        return;
    }

    m_shadowPipelineCache[shadowShaderKey] = shadowPipeline;
    material->SetPassPipeline(ShaderCompileTarget::Shadow, shadowPipeline);
    INXLOG_DEBUG("Created per-material shadow pipeline for '", material->GetName(), "'");
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
