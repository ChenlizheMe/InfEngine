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
#include "TextureUploadBuilder.h"
#include "VertexInputFilter.h"
#include "gui/GPUMaterialPreview.h"
#include "gui/GPUMeshPreview.h"
#include "vk/DescriptorBindTrace.h"
#include "vk/MaterialRenderStateVulkan.h"
#include "vk/RhiVulkanTypes.h"
#include "vk/VkPipelineHelpers.h"
#include "vk/VkRenderUtils.h"

#include <core/types/ColorSpace.h>
#include <function/renderer/shader/ShaderProgram.h>
#include <function/renderer/shader/ShaderReflection.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/resources/InxTexture/InxTexture.h>
#include <function/scene/Scene.h>
#include <function/scene/SceneManager.h>

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

ShadowViewGpuData PackShadowView(const lighting::ShadowView &view, uint32_t atlasSize)
{
    ShadowViewGpuData result{};
    result.viewProjection = view.viewProjection;
    result.atlasScaleOffset = view.atlas.ScaleOffset(atlasSize);
    result.depthTexel = glm::vec4(view.nearPlane, view.farPlane, view.worldUnitsPerTexel, view.filterRadiusTexels);
    result.splitData = glm::vec4(view.splitNear, view.splitFar, 0.0f, 0.0f);
    result.metadata = glm::uvec4(static_cast<uint32_t>(view.type), view.subView, 0u, 0u);
    return result;
}

} // namespace

// ============================================================================
// Shared texture resolution for material Texture2D properties
// ============================================================================

void InxVkCoreModular::PumpPendingTextureLoads()
{
    auto &registry = AssetRegistry::Instance();
    for (auto pending = m_pendingTextureAssetLoads.begin(); pending != m_pendingTextureAssetLoads.end();) {
        try {
            if (!registry.TryCommitAssetLoad(pending->second)) {
                ++pending;
                continue;
            }
            m_materialPipelineManager.RefreshMaterialsUsingTexture(pending->first);
        } catch (const std::exception &exception) {
            INXLOG_ERROR("Texture CPU load failed for GUID '", pending->first, "': ", exception.what());
        }
        pending = m_pendingTextureAssetLoads.erase(pending);
    }

    for (auto pending = m_pendingTextureGpuUploads.begin(); pending != m_pendingTextureGpuUploads.end();) {
        try {
            if (!m_resourceManager.TryPublishTextureUpload(pending->second.ticket)) {
                ++pending;
                continue;
            }
            auto texture = pending->second.ticket->GetTexture();
            if (registry.GetAssetVersion(pending->second.guid) != pending->second.runtimeVersion) {
                pending = m_pendingTextureGpuUploads.erase(pending);
                continue;
            }
            (void)m_textureCache.Insert(pending->first, std::move(texture), m_ensureFrameCounter, false,
                                        pending->second.guid, pending->second.runtimeVersion);
            m_materialPipelineManager.RefreshMaterialsUsingTexture(pending->second.guid);
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
    return ResolveTextureAsset(textureRef, bindingName, TextureDimension::Texture2D, nullptr, nullptr);
}

TextureResolveResult InxVkCoreModular::ResolveTextureForVectorField(const std::string &textureGuid,
                                                                    bool linearFiltering, bool repeat)
{
    return ResolveTextureAsset(textureGuid, "ParticleVectorField", TextureDimension::Texture3D,
                               linearFiltering ? "bilinear" : "point", repeat ? "repeat" : "clamp");
}

TextureResolveResult InxVkCoreModular::ResolveTextureAsset(const std::string &textureGuid,
                                                           const std::string &bindingName,
                                                           TextureDimension expectedDimension,
                                                           const char *filterOverride, const char *wrapOverride)
{
    // Material texture properties store asset GUIDs. Path normalization belongs
    // at the public material/asset boundary, never in the renderer.
    std::string texturePath;
    auto &registry = AssetRegistry::Instance();
    auto *adb = registry.GetAssetDatabase();
    if (adb)
        texturePath = adb->GetPathFromGuid(textureGuid);

    if (texturePath.empty()) {
        INXLOG_WARN("TextureResolver: texture reference '", textureGuid, "' is not a resolvable asset GUID (binding='",
                    bindingName, "'). Texture properties must hold GUIDs.");
        return {TextureResolveStatus::Failed, {}};
    }

    // A published GPU view remains authoritative after its decoded CPU asset
    // has been evicted. Resolve it first so CPU residency policy cannot turn a
    // stable material into a per-frame decode/upload loop.
    const std::string filterIdentity = filterOverride ? filterOverride : "asset";
    const std::string wrapIdentity = wrapOverride ? wrapOverride : "asset";
    const std::string cacheKey = textureGuid + "::dim" + std::to_string(static_cast<uint32_t>(expectedDimension)) +
                                 "::filter=" + filterIdentity + "::wrap=" + wrapIdentity;
    const uint64_t publishedRuntimeVersion = registry.GetAssetVersion(textureGuid);
    if (publishedRuntimeVersion != 0) {
        auto cachedSlot =
            m_textureCache.FindAsset(cacheKey, textureGuid, publishedRuntimeVersion, m_ensureFrameCounter);
        auto cached = cachedSlot ? cachedSlot->Acquire() : nullptr;
        if (cached && cached->IsValid()) {
            auto &rhiDevice = m_backend.Device().GetRhiDevice();
            return {TextureResolveStatus::Ready,
                    {rhiDevice.Resolve(cached->GetView()), rhiDevice.Resolve(cached->GetSampler()),
                     std::move(cachedSlot), std::move(cached)}};
        }
    }

    auto infTex = registry.GetAsset<InxTexture>(textureGuid);
    if (!infTex) {
        auto pending = m_pendingTextureAssetLoads.find(textureGuid);
        if (pending == m_pendingTextureAssetLoads.end()) {
            try {
                pending = m_pendingTextureAssetLoads
                              .emplace(textureGuid, registry.BeginLoadAsset(textureGuid, ResourceType::Texture))
                              .first;
            } catch (const std::exception &exception) {
                INXLOG_ERROR("TextureResolver: failed to schedule texture metadata load for '", textureGuid,
                             "': ", exception.what());
                return {TextureResolveStatus::Failed, {}};
            }
        }
        try {
            if (!registry.TryCommitAssetLoad(pending->second))
                return {TextureResolveStatus::Pending, {}};
            m_pendingTextureAssetLoads.erase(pending);
            infTex = registry.GetAsset<InxTexture>(textureGuid);
        } catch (const std::exception &exception) {
            INXLOG_ERROR("TextureResolver: metadata load failed for '", textureGuid, "': ", exception.what());
            m_pendingTextureAssetLoads.erase(pending);
            return {TextureResolveStatus::Failed, {}};
        }
    }
    if (!infTex || infTex->GetMipCount() == 0) {
        INXLOG_ERROR("TextureResolver: texture asset has no current imported artifact description: ", textureGuid);
        return {TextureResolveStatus::Failed, {}};
    }
    if (infTex->GetDimension() != expectedDimension) {
        INXLOG_ERROR("TextureResolver: binding '", bindingName, "' expected texture dimension ",
                     static_cast<uint32_t>(expectedDimension), " but asset uses ",
                     static_cast<uint32_t>(infTex->GetDimension()), ": ", textureGuid);
        return {TextureResolveStatus::Failed, {}};
    }

    const uint64_t runtimeVersion = registry.GetAssetVersion(textureGuid);
    if (runtimeVersion == 0)
        throw std::logic_error("TextureResolver resolved a payload without a published runtime version");
    const std::string filterMode = filterOverride ? filterOverride : infTex->GetFilterMode();
    const std::string wrapMode = wrapOverride ? wrapOverride : infTex->GetWrapMode();
    const int anisoLevel = infTex->GetAnisoLevel();
    rhi::SamplerDesc sampler;
    sampler.maxLod = static_cast<float>(infTex->GetMipCount() - 1);
    const auto &capabilities = m_backend.Device().GetCapabilities();
    const float deviceMaxAnisotropy =
        capabilities.features.samplerAnisotropy ? (std::max)(1.0f, capabilities.limits.maxSamplerAnisotropy) : 1.0f;
    const float requestedAnisotropy = anisoLevel < 0 ? deviceMaxAnisotropy : static_cast<float>(anisoLevel);
    sampler.maxAnisotropy = (std::min)((std::max)(1.0f, requestedAnisotropy), deviceMaxAnisotropy);
    if (filterMode == "point") {
        sampler.minFilter = rhi::FilterMode::Nearest;
        sampler.magFilter = rhi::FilterMode::Nearest;
        sampler.mipFilter = rhi::FilterMode::Nearest;
        sampler.maxAnisotropy = 1.0f;
    } else if (filterMode == "trilinear") {
        sampler.mipFilter = rhi::FilterMode::Linear;
    } else if (filterMode == "linear" || filterMode == "bilinear") {
        sampler.mipFilter = rhi::FilterMode::Nearest;
    } else {
        INXLOG_ERROR("TextureResolver: unsupported filter mode '", filterMode, "' for texture ", textureGuid);
        return {TextureResolveStatus::Failed, {}};
    }

    if (wrapMode == "clamp")
        sampler.addressU = sampler.addressV = sampler.addressW = rhi::AddressMode::ClampToEdge;
    else if (wrapMode == "mirror")
        sampler.addressU = sampler.addressV = sampler.addressW = rhi::AddressMode::MirroredRepeat;

    // Cache key uses GUID so that a renamed file still shares its cache entry
    // The cache key identifies a stable consumer slot, not the mutable import
    // result. Format, color space and asset-controlled sampler settings may
    // change and must publish a new revision into the same slot.
    // Check texture cache again after CPU resolution: another consumer may
    // have completed publication while this request was pending.
    {
        auto cachedSlot = m_textureCache.FindAsset(cacheKey, textureGuid, runtimeVersion, m_ensureFrameCounter);
        auto cached = cachedSlot ? cachedSlot->Acquire() : nullptr;
        if (cached && cached->IsValid()) {
            auto &rhiDevice = m_backend.Device().GetRhiDevice();
            return {TextureResolveStatus::Ready,
                    {rhiDevice.Resolve(cached->GetView()), rhiDevice.Resolve(cached->GetSampler()),
                     std::move(cachedSlot), std::move(cached)}};
        }
    }

    auto pendingGpu = m_pendingTextureGpuUploads.find(cacheKey);
    if (pendingGpu == m_pendingTextureGpuUploads.end()) {
        auto pendingStaging = m_pendingTextureStagingLoads.find(cacheKey);
        if (pendingStaging == m_pendingTextureStagingLoads.end()) {
            try {
                pendingStaging =
                    m_pendingTextureStagingLoads.emplace(cacheKey, registry.BeginTextureUploadStaging(textureGuid))
                        .first;
            } catch (const std::exception &exception) {
                INXLOG_ERROR("TextureResolver: failed to schedule upload staging for '", textureGuid,
                             "': ", exception.what());
                return {TextureResolveStatus::Failed, {}};
            }
        }
        try {
            auto staging = registry.TryConsumeTextureUploadStaging(pendingStaging->second);
            if (!staging)
                return {TextureResolveStatus::Pending, {}};
            m_pendingTextureStagingLoads.erase(pendingStaging);
            TextureUploadBatch upload(*staging, sampler);
            auto ticket = m_resourceManager.BeginTextureUpload(upload.GetRequest());
            ++m_submittedTextureUploadCount;
            if (ticket->IsAsync())
                ++m_asyncTextureUploadCount;
            pendingGpu = m_pendingTextureGpuUploads
                             .emplace(cacheKey, PendingTextureGpuUpload{textureGuid, runtimeVersion, std::move(ticket)})
                             .first;
        } catch (const std::exception &exception) {
            m_pendingTextureStagingLoads.erase(cacheKey);
            INXLOG_ERROR("TextureResolver: failed to schedule GPU upload for '", textureGuid, "': ", exception.what());
            return {TextureResolveStatus::Failed, {}};
        }
    }
    try {
        if (!m_resourceManager.TryPublishTextureUpload(pendingGpu->second.ticket))
            return {TextureResolveStatus::Pending, {}};
        auto texture = pendingGpu->second.ticket->GetTexture();
        auto &rhiDevice = m_backend.Device().GetRhiDevice();
        const VkImageView view = rhiDevice.Resolve(texture->GetView());
        const VkSampler nativeSampler = rhiDevice.Resolve(texture->GetSampler());
        if (registry.GetAssetVersion(textureGuid) != pendingGpu->second.runtimeVersion) {
            m_pendingTextureGpuUploads.erase(pendingGpu);
            return {TextureResolveStatus::Pending, {}};
        }
        auto residentSlot = m_textureCache.Insert(cacheKey, std::move(texture), m_ensureFrameCounter, false,
                                                  textureGuid, pendingGpu->second.runtimeVersion);
        auto resident = residentSlot ? residentSlot->Acquire() : nullptr;
        if (!resident || !resident->IsValid())
            throw std::logic_error("GPU texture cache published an invalid view");
        m_pendingTextureGpuUploads.erase(pendingGpu);
        ++m_completedTextureUploadCount;
        return {TextureResolveStatus::Ready, {view, nativeSampler, std::move(residentSlot), std::move(resident)}};
    } catch (const std::exception &exception) {
        INXLOG_ERROR("TextureResolver: GPU upload failed for '", textureGuid, "': ", exception.what());
        m_pendingTextureGpuUploads.erase(pendingGpu);
        return {TextureResolveStatus::Failed, {}};
    }
}

std::shared_ptr<const rhi::TextureGpuView>
InxVkCoreModular::ResolveTextureForEditorPreview(const std::string &textureGuid)
{
    TextureResolveResult resolved =
        ResolveTextureAsset(textureGuid, "EditorTexturePreview", TextureDimension::Texture2D, nullptr, nullptr);
    return resolved.status == TextureResolveStatus::Ready ? std::move(resolved.binding.gpuView) : nullptr;
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
        // Authored Color properties are sRGB; shading runs in linear space.
        if constexpr (std::is_same_v<T, glm::vec4>) {
            if (prop.type == MaterialPropertyType::Color)
                value = inx::color::SrgbToLinear(value);
        }
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
    const bool hasPendingTextures = m_materialPipelineManagerInitialized &&
                                    m_materialPipelineManager.HasPendingTextureProperties(material.GetMaterialKey());
    if (!material.IsPropertiesDirty() && !hasPendingTextures) {
        return;
    }

    if (m_materialPipelineManagerInitialized) {
        MaterialRenderData *renderData = m_materialPipelineManager.GetRenderData(material.GetMaterialKey());
        if (renderData && renderData->isValid && renderData->shaderProgram &&
            renderData->descriptorSet != VK_NULL_HANDLE &&
            m_materialPipelineManager.IsDescriptorSetLive(renderData->descriptorSet)) {
            m_materialPipelineManager.UpdateMaterialProperties(material.GetMaterialKey(), material);
            if (!m_materialPipelineManager.HasPendingTextureProperties(material.GetMaterialKey())) {
                material.ClearPropertiesDirty();
            }
            return;
        }
    }

    const ShaderProgram *shaderProgram = material.GetPassShaderProgram(ShaderCompileTarget::Forward);
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
    const ShaderProgram *shaderProgram = material->GetPassShaderProgram(ShaderCompileTarget::Forward);
    const MaterialUBOLayout *uboLayout = shaderProgram ? shaderProgram->GetMaterialUBOLayout() : nullptr;
    if (!uboLayout || uboLayout->size == 0) {
        INXLOG_WARN("VkCoreMaterial: material '", material->GetName(),
                    "' has no UBO reflection layout — skipping UBO creation");
        return;
    }
    size_t uboSize = uboLayout->size;
    CreateBuffer(uboSize, VK_BUFFER_USAGE_UNIFORM_BUFFER_BIT,
                 VK_MEMORY_PROPERTY_HOST_VISIBLE_BIT | VK_MEMORY_PROPERTY_HOST_COHERENT_BIT, uboBuffer, uboAllocation);

    VmaAllocator allocator = m_backend.Device().GetVmaAllocator();
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

    VmaAllocator allocator = m_backend.Device().GetVmaAllocator();
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
        // A retry/reinitialization must not carry shader-program publications
        // owned by the previous manager generation into the new one.
        ReleaseMaterialPassResolutionCache();

        // Use SceneRenderTarget-compatible formats: HDR R16G16B16A16_SFLOAT color + device depth format
        VkFormat colorFormat = VK_FORMAT_R16G16B16A16_SFLOAT;
        VkFormat depthFormat = m_backend.Device().FindDepthFormat();
        const auto colorSamples = m_backend.Device().GetImageSampleCountMask(
            colorFormat, VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT);
        const auto resolveSamples = m_backend.Device().GetImageSampleCountMask(
            colorFormat, VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
                             VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT);
        const auto depthSamples =
            m_backend.Device().GetImageSampleCountMask(depthFormat, VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT);
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
        m_materialPipelineManager.Initialize(
            m_backend.Device().GetVmaAllocator(), GetDevice(), GetPhysicalDevice(), colorFormat, depthFormat,
            m_msaaSampleCount, m_shaderCache.GetProgramCache(), &m_deletionQueue,
            m_backend.Device().IsDescriptorIndexingEnabled(),
            m_backend.Device().GetRhiDevice().GetCapabilityState().dynamicRendering.IsEnabled(),
            &m_backend.Device().GetRhiDevice().GetDescriptorManager(),
            rhi::ComputeDeviceShaderContractKey(m_backend.Device().GetRhiDevice().GetCapabilityState()));
        m_materialPipelineManagerInitialized = true;

        auto &materialDescriptors = m_materialPipelineManager.GetDescriptorManager();
        auto &rhiDevice = m_backend.Device().GetRhiDevice();
        materialDescriptors.SetBindlessMaterialMode(rhiDevice.GetBindlessTextureTableBinding().IsValid());
        materialDescriptors.SetBindlessTextureResolver(
            [device = &rhiDevice](const std::shared_ptr<const rhi::TextureGpuView> &view) -> rhi::ResourceIndex {
                return device->PublishBindlessTexture(view);
            });

        auto whiteSlot = m_textureCache.Find("white", m_ensureFrameCounter);
        auto whiteTex = whiteSlot ? whiteSlot->Acquire() : nullptr;
        if (whiteTex) {
            auto &rhiDevice = m_backend.Device().GetRhiDevice();
            m_materialPipelineManager.SetDefaultTexture(rhiDevice.Resolve(whiteTex->GetView()),
                                                        rhiDevice.Resolve(whiteTex->GetSampler()), whiteTex);
        }

        auto normalSlot = m_textureCache.Find("_default_normal", m_ensureFrameCounter);
        auto normalTex = normalSlot ? normalSlot->Acquire() : nullptr;
        if (normalTex) {
            auto &rhiDevice = m_backend.Device().GetRhiDevice();
            m_materialPipelineManager.SetDefaultNormalTexture(rhiDevice.Resolve(normalTex->GetView()),
                                                              rhiDevice.Resolve(normalTex->GetSampler()), normalTex);
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
            m_materialPipelineManager.GetOrCreateRenderDataWithReflection(defaultMaterial, *vertCode, *fragCode,
                                                                          ShaderProgramKey{{vertId, fragId}, 0});
        } else {
            // Headless/bootstrap users can prepare the renderer before a
            // project shader catalog exists.  This is not a failed material:
            // the normal material publication path reflects DefaultLit as
            // soon as Standard|Lit enters the shader cache.
            INXLOG_DEBUG("InitializeMaterialSystem: default material shader SPIR-V not yet in cache "
                         "(vert='",
                         vertId, "', frag='", fragId, "'), deferring reflection until first use");
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
            auto *renderData = m_materialPipelineManager.GetOrCreateRenderDataWithReflection(
                errorMaterial, *errVertCode, *errFragCode, ShaderProgramKey{{errVertId, errFragId}, 0});
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

bool InxVkCoreModular::CommitMaterialPipelineGeneration(VkSampleCountFlagBits newSampleCount)
{
    if (!m_materialPipelineManagerInitialized) {
        m_msaaSampleCount = newSampleCount;
        return true;
    }

    if (!m_materialPipelineManager.ReconfigureSampleCount(newSampleCount))
        return false;
    m_msaaSampleCount = newSampleCount;

    // Preview render targets cache a render pass / framebuffer that must stay
    // compatible with the material pipelines' MSAA sample count. Keep the old
    // previews alive until every queue that may reference them has completed.
    if (m_gpuMaterialPreview) {
        std::shared_ptr<GPUMaterialPreview> retired(m_gpuMaterialPreview.release());
        m_deletionQueue.Retire([retired = std::move(retired)] {});
    }
    if (m_gpuMeshPreview) {
        std::shared_ptr<GPUMeshPreview> retired(m_gpuMeshPreview.release());
        m_deletionQueue.Retire([retired = std::move(retired)] {});
    }
    return true;
}

bool InxVkCoreModular::RefreshMaterialPipeline(std::shared_ptr<InxMaterial> material, const std::string &vertShaderName,
                                               const std::string &fragShaderName)
{
    return RefreshPreviewMaterialPipeline(material, vertShaderName, fragShaderName);
}

void InxVkCoreModular::ReleaseGpuPreviews()
{
    m_resourceManager.DrainAsyncGraphicsSubmissions();
    m_gpuMeshPreview.reset();
    m_gpuMaterialPreview.reset();
}

bool InxVkCoreModular::RefreshPreviewMaterialPipeline(std::shared_ptr<InxMaterial> material,
                                                      const std::string &vertShaderName,
                                                      const std::string &fragShaderName, bool reportDomainMismatch)
{
    if (!material) {
        return false;
    }

    const ShaderStagePair stages{vertShaderName, fragShaderName};
    const auto *artifact = m_shaderCache.FindProgramArtifact(stages);
    if (artifact && artifact->domain != ShaderProgramDomain::Mesh) {
        MaterialRenderData *previous = m_materialPipelineManager.GetRenderData(material->GetMaterialKey());
        const bool hasLastKnownGood = previous && previous->isValid && previous->pipeline != VK_NULL_HANDLE &&
                                      previous->pipelineLayout != VK_NULL_HANDLE &&
                                      previous->descriptorSet != VK_NULL_HANDLE &&
                                      m_materialPipelineManager.IsDescriptorSetLive(previous->descriptorSet);

        if (reportDomainMismatch) {
            static std::unordered_set<std::string> reportedDomainMismatches;
            const std::string materialName =
                material->GetName().empty() ? material->GetMaterialKey() : material->GetName();
            const std::string failureKey = material->GetMaterialKey() + "|" + stages.ToString() + "|Mesh";
            if (reportedDomainMismatches.insert(failureKey).second) {
                INXLOG_ERROR("Material shader domain mismatch: material '", materialName, "' uses shader program '",
                             stages.ToString(), "' with domain '", ShaderProgramDomainName(artifact->domain),
                             "', but MeshRenderer geometry requires domain 'Mesh'. Use a Mesh-domain vertex/fragment "
                             "shader pair, or use this material through ParticleSystem. ",
                             hasLastKnownGood ? "The rejected rebuild did not replace the previous valid GPU pipeline."
                                              : "This material cannot be rendered by MeshRenderer.");
            }
        }

        // Shader selection dirties the material before its domain is known. A
        // previous geometry generation remains valid and is intentionally kept
        // active until the user selects another shader pair.
        if (hasLastKnownGood) {
            material->ClearPipelineDirty();
            return true;
        }
        return false;
    }

    // Material documents intentionally store only authored values. Resolve
    // omitted values from the linked shader contract before any descriptor or
    // preview pipeline observes the material, so passive Project thumbnails,
    // Inspector previews and scene rendering all start from the same snapshot.
    if (artifact)
        material->SynchronizeShaderPropertyDefaults(*artifact);

    // Apply shader render-state annotations to the material before pipeline creation.
    // Fragment shader annotations take priority (they define the surface behaviour).
    const auto *renderMeta = m_shaderCache.GetRenderMeta(fragShaderName);
    if (renderMeta) {
        material->ApplyShaderRenderMeta(renderMeta->cullMode, renderMeta->depthWrite, renderMeta->depthTest,
                                        renderMeta->blend, renderMeta->queue, renderMeta->passTag, renderMeta->stencil,
                                        renderMeta->alphaClip);
    }

    const auto *forward = artifact ? artifact->FindVariant(ShaderCompileTarget::Forward) : nullptr;
    const auto *vertCode = forward ? &forward->vertexSpirv : m_shaderCache.FindVertCode(vertShaderName);
    const auto *fragCode = forward ? &forward->fragmentSpirv : m_shaderCache.FindFragCode(fragShaderName);
    const ShaderProgramKey programKey = artifact ? artifact->key : ShaderProgramKey{stages, 0};

    if (vertCode && fragCode && m_materialPipelineManagerInitialized) {
        auto *renderData =
            m_materialPipelineManager.GetOrCreateRenderDataWithReflection(material, *vertCode, *fragCode, programKey);

        bool forwardOk = renderData && renderData->isValid;

        if (forwardOk && artifact) {
            // Optional programs are materialized by their first real pass.
            for (const auto target :
                 {ShaderCompileTarget::ForwardPlus, ShaderCompileTarget::GBuffer, ShaderCompileTarget::Shadow,
                  ShaderCompileTarget::Depth, ShaderCompileTarget::Picking, ShaderCompileTarget::Motion,
                  ShaderCompileTarget::Normal, ShaderCompileTarget::BaseColor})
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
        // INXLOG_WARN("RefreshMaterialPipeline: shader codes not found or MPM not initialized for '",
        // material->GetName(),
        //             "' (vert='", vertShaderName, "', frag='", fragShaderName, "')");
    }

    // Dump available shader keys for debugging
    static int dumpCount = 0;
    if (dumpCount++ < 2) {
        std::string vertKeys, fragKeys;
        m_shaderCache.DumpAvailableKeys(vertKeys, fragKeys);
        // INXLOG_WARN("  Available vert shaders:", vertKeys);
        // INXLOG_WARN("  Available frag shaders:", fragKeys);
        // INXLOG_WARN("  MPM initialized: ", m_materialPipelineManagerInitialized ? "true" : "false",
        //             ", vertCode found: ", (vertCode ? "yes" : "no"), ", fragCode found: ", (fragCode ? "yes" :
        //             "no"));
    }
    return false;
}

MaterialPassRenderData *InxVkCoreModular::GetOrCreatePreviewMaterialPass(std::shared_ptr<InxMaterial> material,
                                                                         bool useDynamicRendering)
{
    if (!material)
        return nullptr;

    auto &pipelineManager = GetMaterialPipelineManager();
    auto *forward = pipelineManager.GetRenderData(material->GetMaterialKey());
    if (!forward || !forward->isValid || !forward->shaderProgram)
        return nullptr;

    auto descriptor = pipelineManager.GetDefaultPassPipelineDescriptor(ShaderCompileTarget::Forward);
    if (useDynamicRendering)
        descriptor.renderingMode = MaterialPassRenderingMode::DynamicRendering;
    return pipelineManager.GetOrCreatePassRenderData(std::move(material), forward->shaderProgram, descriptor);
}

// ============================================================================
// Lighting System
// ============================================================================

void InxVkCoreModular::SetAmbientColor(const glm::vec3 &color, float intensity)
{
    m_lightCollector.SetAmbientColor(color, intensity);
    INXLOG_DEBUG("SetAmbientColor: (", color.r, ", ", color.g, ", ", color.b, ") intensity=", intensity);
}

void InxVkCoreModular::UpdateLightingState()
{
    // Environment lighting follows the active scene's settings (Unity-style
    // Lighting > Environment): ambient is derived from the skybox material,
    // an explicit gradient, or a flat color.
    Scene *activeScene = SceneManager::Instance().GetActiveScene();
    const SceneEnvironmentSettings env = activeScene ? activeScene->GetEnvironment() : SceneEnvironmentSettings{};
    using AmbientSource = SceneEnvironmentSettings::AmbientSource;

    // The builtin procedural sky has no backing asset — its parameters are
    // scene data. Push them onto the shared builtin material so the skybox
    // draw and the ambient sync below both see the scene's values (only
    // writing on change to avoid dirtying the material UBO every frame).
    if (env.skyboxMaterialGuid.empty()) {
        if (auto builtinSky = AssetRegistry::Instance().GetBuiltinMaterial("SkyboxProcedural")) {
            const auto syncColor = [&](const char *name, const glm::vec3 &value) {
                const glm::vec4 desired(value, 1.0f);
                const auto *prop = builtinSky->GetProperty(name);
                if (!prop || !std::holds_alternative<glm::vec4>(prop->value) ||
                    std::get<glm::vec4>(prop->value) != desired)
                    builtinSky->SetColor(name, desired);
            };
            syncColor("skyTopColor", env.skyTopColor);
            syncColor("skyHorizonColor", env.skyHorizonColor);
            syncColor("groundColor", env.skyGroundColor);
            const auto *exposureProp = builtinSky->GetProperty("exposure");
            if (!exposureProp || !std::holds_alternative<float>(exposureProp->value) ||
                std::get<float>(exposureProp->value) != env.skyExposure)
                builtinSky->SetFloat("exposure", env.skyExposure);
        }
    }

    switch (static_cast<AmbientSource>(env.ambientSource)) {
    case AmbientSource::Gradient:
        m_lightCollector.SetAmbientGradient(env.ambientSkyColor, env.ambientEquatorColor, env.ambientGroundColor,
                                            env.ambientIntensity);
        break;
    case AmbientSource::Color:
        m_lightCollector.SetAmbientColor(env.ambientColor, env.ambientIntensity);
        break;
    case AmbientSource::Skybox:
    default: {
        std::shared_ptr<InxMaterial> skyMat = activeScene
                                                  ? activeScene->ResolveSkyboxMaterial()
                                                  : AssetRegistry::Instance().GetBuiltinMaterial("SkyboxProcedural");
        bool applied = false;
        if (skyMat) {
            const auto *skyTopProp = skyMat->GetProperty("skyTopColor");
            const auto *horizonProp = skyMat->GetProperty("skyHorizonColor");
            const auto *groundProp = skyMat->GetProperty("groundColor");
            const auto *exposureProp = skyMat->GetProperty("exposure");
            if (skyTopProp && groundProp) {
                const glm::vec3 skyTop = glm::vec3(std::get<glm::vec4>(skyTopProp->value));
                const glm::vec3 ground = glm::vec3(std::get<glm::vec4>(groundProp->value));
                const glm::vec3 equator =
                    horizonProp ? glm::vec3(std::get<glm::vec4>(horizonProp->value)) : glm::mix(ground, skyTop, 0.5f);
                float exposure = 1.0f;
                if (exposureProp && std::holds_alternative<float>(exposureProp->value))
                    exposure = std::get<float>(exposureProp->value);
                m_lightCollector.SetAmbientGradient(skyTop, equator, ground, exposure * env.ambientIntensity);
                applied = true;
            }
        }
        // Custom sky materials without the procedural color properties fall
        // back to a neutral gradient scaled by the ambient intensity.
        if (!applied)
            m_lightCollector.SetAmbientGradient(glm::vec3(0.5f), glm::vec3(0.4f), glm::vec3(0.25f),
                                                env.ambientIntensity);
        break;
    }
    }

    // Build the shader-compatible UBO from collected lights
    m_lightCollector.BuildShaderLightingUBO();
    const uint32_t frameIndex = m_currentFrame % m_maxFramesInFlight;
    if (!m_canonicalLightGpuBuffer.Update(frameIndex, m_lightCollector.GetCanonicalLightSnapshot()))
        INXLOG_ERROR("Failed to upload canonical light snapshot for frame slot ", frameIndex);
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

VkBuffer InxVkCoreModular::GetFallbackMaterialUbo() const
{
    return m_materialUbo ? m_materialUbo->GetBuffer() : VK_NULL_HANDLE;
}

VkBuffer InxVkCoreModular::GetInstanceSSBO(size_t index) const
{
    if (index < m_instanceBuffers.size() && m_instanceBuffers[index].buffer)
        return m_instanceBuffers[index].buffer->GetBuffer();
    return VK_NULL_HANDLE;
}

VkShaderModule InxVkCoreModular::GetShaderModule(const std::string &name, const std::string &type)
{
    if (!EnsureShaderAvailable(name, type))
        return VK_NULL_HANDLE;
    return m_shaderCache.GetModule(name, type);
}

// ============================================================================
// Shared shadow material bindings and pipeline creation
// ============================================================================

InxVkCoreModular::ShadowDescriptorAllocation InxVkCoreModular::AllocateShadowMaterialDescriptorSet()
{
    if (m_shadowMaterialDescSetLayout == VK_NULL_HANDLE)
        return {};
    ShadowDescriptorAllocation allocation{};
    allocation.descriptorLease = m_backend.Device().GetRhiDevice().GetDescriptorManager().Allocate(
        m_shadowMaterialDescSetLayout, vk::DescriptorArena::Persistent);
    allocation.descriptorSet = allocation.descriptorLease.set;
    return allocation;
}

bool InxVkCoreModular::EnsureShadowMaterialDummyDescriptorSet()
{
    if (m_shadowMaterialDummyDescSet != VK_NULL_HANDLE)
        return true;
    if (m_shadowMaterialDescSetLayout == VK_NULL_HANDLE)
        return false;
    auto defaultSlot = m_textureCache.Find("white", m_ensureFrameCounter);
    auto defaultTex = defaultSlot ? defaultSlot->Acquire() : nullptr;
    if (!defaultTex)
        return false;
    auto &rhiDevice = m_backend.Device().GetRhiDevice();
    const VkImageView defaultView = rhiDevice.Resolve(defaultTex->GetView());
    const VkSampler defaultSampler = rhiDevice.Resolve(defaultTex->GetSampler());
    if (defaultView == VK_NULL_HANDLE || defaultSampler == VK_NULL_HANDLE)
        return false;
    if (!m_materialUbo)
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
        imageInfos[i].imageView = defaultView;
        imageInfos[i].sampler = defaultSampler;
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
    fragBi.buffer = m_materialUbo->GetBuffer();
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
    vtxBi.buffer = m_materialUbo->GetBuffer();
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
    m_shadowMaterialDummyLease = allocation.descriptorLease;
    return true;
}

void InxVkCoreModular::RetireShadowMaterialBinding(ShadowMaterialBindingEntry entry)
{
    if (entry.descriptorSet == VK_NULL_HANDLE || entry.descriptorSet == m_shadowMaterialDummyDescSet ||
        !entry.descriptorLease.IsValid()) {
        return;
    }

    m_backend.Device().GetRhiDevice().GetDescriptorManager().Retire(entry.descriptorLease);
    ++m_shadowMaterialBindingRetirements;
    m_deletionQueue.Retire([keepAlive = std::move(entry.textureKeepAlive)]() mutable { keepAlive.clear(); });
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
    const bool usesBindlessTextures = shadowProgram && shadowProgram->UsesBindlessTextureABI();
    const bool needsDescriptor =
        hasVertexMaterialUBO || hasVertexMaterialTextures || hasAlphaClip || usesBindlessTextures;
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
    if (usesBindlessTextures &&
        (!forwardMaterialDesc->usesBindlessTextureABI || !forwardMaterialDesc->textureIndexUBO ||
         !forwardMaterialDesc->textureIndexUBO->IsValid())) {
        INXLOG_WARN("EnsureShadowMaterialBinding: missing bindless texture indices for '", material->GetName(), "'");
        return VK_NULL_HANDLE;
    }

    auto defaultSlot = m_textureCache.Find("white", m_ensureFrameCounter);
    auto defaultTex = defaultSlot ? defaultSlot->Acquire() : nullptr;
    if (!defaultTex || !m_materialUbo) {
        return VK_NULL_HANDLE;
    }
    auto &rhiDevice = m_backend.Device().GetRhiDevice();
    const VkImageView defaultView = rhiDevice.Resolve(defaultTex->GetView());
    const VkSampler defaultSampler = rhiDevice.Resolve(defaultTex->GetSampler());
    if (defaultView == VK_NULL_HANDLE || defaultSampler == VK_NULL_HANDLE)
        return VK_NULL_HANDLE;

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
    HashCombine(resourceSignature, usesBindlessTextures ? 1u : 0u);
    HashCombine(resourceSignature, VulkanHandleBits(forwardMaterialDesc->descriptorSet));
    HashCombine(resourceSignature, VulkanHandleBits(defaultView));
    HashCombine(resourceSignature, VulkanHandleBits(defaultSampler));
    if (forwardMaterialDesc->materialUBO) {
        HashCombine(resourceSignature, VulkanHandleBits(forwardMaterialDesc->materialUBO->GetBuffer()));
        HashCombine(resourceSignature, forwardMaterialDesc->materialUBO->GetSize());
    }
    if (forwardMaterialDesc->vertexMaterialUBO) {
        HashCombine(resourceSignature, VulkanHandleBits(forwardMaterialDesc->vertexMaterialUBO->GetBuffer()));
        HashCombine(resourceSignature, forwardMaterialDesc->vertexMaterialUBO->GetSize());
    }
    if (usesBindlessTextures && forwardMaterialDesc->textureIndexUBO) {
        HashCombine(resourceSignature, VulkanHandleBits(forwardMaterialDesc->textureIndexUBO->GetBuffer()));
        HashCombine(resourceSignature, forwardMaterialDesc->textureIndexUBO->GetSize());
    }
    for (const auto &[binding, texture] : sortedTextures) {
        HashCombine(resourceSignature, binding);
        HashCombine(resourceSignature, VulkanHandleBits(texture.imageView));
        HashCombine(resourceSignature, VulkanHandleBits(texture.sampler));
    }

    const auto markBindlessShadowResourcesUsed = [&]() {
        if (!usesBindlessTextures)
            return;
        auto &rhiDevice = m_backend.Device().GetRhiDevice();
        rhiDevice.MarkBindlessTexturesUsed(forwardMaterialDesc->bindlessTextureIndices.empty()
                                               ? nullptr
                                               : forwardMaterialDesc->bindlessTextureIndices.data(),
                                           forwardMaterialDesc->bindlessTextureIndices.size());
    };

    auto existing = m_shadowMaterialBindingCache.find(identity);
    if (existing != m_shadowMaterialBindingCache.end()) {
        const auto owner = existing->second.owner.lock();
        if (owner.get() == identity && existing->second.resourceSignature == resourceSignature &&
            existing->second.descriptorSet != VK_NULL_HANDLE) {
            existing->second.materialVersion = material->GetVersion();
            ++m_shadowMaterialBindingCacheHits;
            markBindlessShadowResourcesUsed();
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
        imageInfo.imageView = defaultView;
        imageInfo.sampler = defaultSampler;
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
        fragmentUbo ? fragmentUbo->GetBuffer() : (vertexUbo ? vertexUbo->GetBuffer() : m_materialUbo->GetBuffer());
    fragmentBuffer.offset = 0;
    fragmentBuffer.range = fragmentUbo ? fragmentUbo->GetSize() : (vertexUbo ? vertexUbo->GetSize() : 16);
    VkDescriptorBufferInfo vertexBuffer{};
    vertexBuffer.buffer = vertexUbo ? vertexUbo->GetBuffer() : fragmentBuffer.buffer;
    vertexBuffer.offset = 0;
    vertexBuffer.range = vertexUbo ? vertexUbo->GetSize() : fragmentBuffer.range;

    std::vector<VkWriteDescriptorSet> writes;
    writes.reserve(kMaxShadowMaterialTextures + 3);
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
    VkDescriptorBufferInfo textureIndexBuffer{};
    VkWriteDescriptorSet textureIndexWrite{};
    if (usesBindlessTextures) {
        textureIndexBuffer.buffer = forwardMaterialDesc->textureIndexUBO->GetBuffer();
        textureIndexBuffer.offset = 0;
        textureIndexBuffer.range = forwardMaterialDesc->textureIndexUBO->GetSize();
        textureIndexWrite.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
        textureIndexWrite.dstSet = allocation.descriptorSet;
        textureIndexWrite.dstBinding = ShaderProgram::MaterialTextureIndexBinding;
        textureIndexWrite.descriptorCount = 1;
        textureIndexWrite.descriptorType = VK_DESCRIPTOR_TYPE_UNIFORM_BUFFER;
        textureIndexWrite.pBufferInfo = &textureIndexBuffer;
        writes.push_back(textureIndexWrite);
    }
    vkUpdateDescriptorSets(GetDevice(), static_cast<uint32_t>(writes.size()), writes.data(), 0, nullptr);

    markBindlessShadowResourcesUsed();

    ShadowMaterialBindingEntry replacement{};
    replacement.owner = material;
    replacement.materialKey = material->GetMaterialKey();
    replacement.materialVersion = material->GetVersion();
    replacement.artifactRevision = artifactRevision;
    replacement.resourceSignature = resourceSignature;
    replacement.descriptorSet = allocation.descriptorSet;
    replacement.descriptorLease = allocation.descriptorLease;
    replacement.textureKeepAlive.push_back(defaultTex);
    for (const auto &[binding, texture] : sortedTextures) {
        (void)binding;
        if (texture.gpuView)
            replacement.textureKeepAlive.push_back(texture.gpuView);
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
                                                               const std::string &fragShaderName,
                                                               VkRenderPass compatibleRenderPass, VkFormat depthFormat)
{
    // Shared shadow resources must be ready
    if (m_shadowPipelineLayout == VK_NULL_HANDLE || depthFormat == VK_FORMAT_UNDEFINED)
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
    const ShaderProgram *forwardProgram = forwardRenderData ? forwardRenderData->shaderProgram.get() : nullptr;
    const ShaderStagePair stagePair{vertShaderName, fragShaderName};
    const ShaderProgramArtifact *linkedArtifact = m_shaderCache.FindProgramArtifact(stagePair);
    ShaderProgramPublication linkedShadowPublication;
    if (linkedArtifact && linkedArtifact->FindVariant(ShaderCompileTarget::Shadow)) {
        linkedShadowPublication = m_shaderCache.MaterializeProgramVariant(stagePair, ShaderCompileTarget::Shadow);
    }
    const ShaderProgram *linkedShadowProgram = linkedShadowPublication.get();
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
    VkCullModeFlags matCullMode = vk::ToVkCullMode(material->GetRenderState().cullMode);
    std::string shadowShaderKey = vertShaderName + "|" + fragShaderName + "|";
    shadowShaderKey += linkedArtifact ? std::to_string(linkedArtifact->key.revision) + ":Shadow" : "legacy-shadow";
    shadowShaderKey += "|cull" + std::to_string(matCullMode);
    shadowShaderKey += "|depth" + std::to_string(static_cast<uint32_t>(depthFormat));
    shadowShaderKey += "|renderPass" + std::to_string(VulkanHandleBits(compatibleRenderPass));
    auto cacheIt = m_shadowPipelineCache.find(shadowShaderKey);
    if (cacheIt != m_shadowPipelineCache.end()) {
        material->SetPassPipeline(ShaderCompileTarget::Shadow, cacheIt->second);
        material->SetPassPipelineLayout(ShaderCompileTarget::Shadow, m_shadowPipelineLayout);
        material->SetPassShaderProgram(ShaderCompileTarget::Shadow, linkedShadowPublication);
        return EnsureShadowMaterialBinding(material, forwardMaterialDesc, forwardProgram, linkedShadowProgram,
                                           linkedArtifact ? linkedArtifact->key.revision : 0);
    }

    // Shader stages
    auto shaderStages = vkrender::MakeVertFragStages(vertModule, fragModule);

    // Vertex input — only attributes consumed by the shadow vertex shader (full mesh buffer still bound).
    auto bindingDesc = vk::GetVertexBindingDescription();
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
    const std::array<VkDynamicState, 3> shadowDynamicStates = {
        VK_DYNAMIC_STATE_VIEWPORT,
        VK_DYNAMIC_STATE_SCISSOR,
        VK_DYNAMIC_STATE_DEPTH_BIAS,
    };
    dynVpScissor.dynamicState.dynamicStateCount = static_cast<uint32_t>(shadowDynamicStates.size());
    dynVpScissor.dynamicState.pDynamicStates = shadowDynamicStates.data();

    // Directional depth bias is applied dynamically in raster space. Unlike a
    // world-space caster translation this follows polygon slope without
    // detaching contact shadows by a full cascade texel. Local lights retain
    // their perspective-scaled vertex bias and set the dynamic values to zero.
    VkPipelineRasterizationStateCreateInfo rasterizer{};
    rasterizer.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;
    rasterizer.polygonMode = VK_POLYGON_MODE_FILL;
    rasterizer.lineWidth = 1.0f;
    rasterizer.cullMode = static_cast<VkCullModeFlags>(matCullMode);
    rasterizer.frontFace = VK_FRONT_FACE_CLOCKWISE;
    rasterizer.depthBiasEnable = VK_TRUE;
    rasterizer.depthBiasConstantFactor = 0.0f;
    rasterizer.depthBiasSlopeFactor = 0.0f;
    rasterizer.depthBiasClamp = 0.0f;

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
    std::array<VkFormat, rhi::GraphicsRenderingSignature::MaxColorTargets> dynamicColorFormats{};
    VkPipelineRenderingCreateInfo dynamicRenderingInfo{};
    if (compatibleRenderPass == VK_NULL_HANDLE) {
        rhi::GraphicsRenderingSignature signature;
        signature.depthFormat = rhi::FromVkFormat(depthFormat);
        signature.stencilFormat =
            rhi::IsStencilFormat(signature.depthFormat) ? signature.depthFormat : rhi::PixelFormat::Undefined;
        signature.samples = rhi::SampleCount::One;
        if (!rhi::BuildVkPipelineRenderingInfo(signature, dynamicColorFormats, dynamicRenderingInfo)) {
            INXLOG_WARN("Failed to build Dynamic Rendering signature for shadow pipeline");
            return VK_NULL_HANDLE;
        }
        pipelineInfo.pNext = &dynamicRenderingInfo;
    }
    pipelineInfo.renderPass = compatibleRenderPass;
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
    material->SetPassShaderProgram(ShaderCompileTarget::Shadow, linkedShadowPublication);
    INXLOG_DEBUG("Created shared shadow pipeline for '", material->GetName(), "'");
    return EnsureShadowMaterialBinding(material, forwardMaterialDesc, forwardProgram, linkedShadowProgram,
                                       linkedArtifact ? linkedArtifact->key.revision : 0);
}

// ============================================================================
// GPU Material Preview
// ============================================================================

std::shared_ptr<vk::ImageReadbackTicket>
InxVkCoreModular::BeginMaterialPreviewGPU(const std::shared_ptr<InxMaterial> &material, int size, bool *texturePending)
{
    if (!material || size <= 0 || !m_materialPipelineManagerInitialized)
        return nullptr;

    if (!m_gpuMaterialPreview)
        m_gpuMaterialPreview = std::make_unique<GPUMaterialPreview>(this);

    return m_gpuMaterialPreview->BeginRenderToPixels(*material, size, texturePending);
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

uint64_t InxVkCoreModular::GetMeshPreviewDisplayTextureId() const
{
    return m_gpuMeshPreview ? m_gpuMeshPreview->GetDisplayTextureId() : 0;
}

} // namespace infernux
