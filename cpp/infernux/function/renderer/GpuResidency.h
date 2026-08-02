#pragma once

#include <cstddef>
#include <cstdint>
#include <string>
#include <vector>

namespace infernux
{

struct GpuResidencySnapshot
{
    uint64_t budgetBytes = 0;
    uint64_t allocatorAllocationBytes = 0;
    uint64_t allocatorBlockBytes = 0;
    uint64_t deviceLocalAllocationBytes = 0;
    uint64_t deviceLocalUsageBytes = 0;
    uint64_t deviceLocalBudgetBytes = 0;
    size_t allocatorAllocationCount = 0;

    uint64_t meshBytes = 0;
    uint64_t textureBytes = 0;
    uint64_t imguiTextureBytes = 0;
    uint64_t pendingImguiTextureBytes = 0;
    uint64_t stagingPoolBytes = 0;
    uint64_t pendingReadbackBytes = 0;
    size_t pendingReadbackCount = 0;
    size_t pendingGpuTransferCount = 0;
    bool uploadTimelineEnabled = false;
    uint64_t timelineUploadPublicationCount = 0;
    uint64_t requiredUploadTimelineValue = 0;
    uint64_t deviceWaitIdleCount = 0;
    uint64_t shaderHotReloadRetirementCount = 0;
    size_t pendingAsyncGraphicsSubmissionCount = 0;
    uint64_t asyncGraphicsSubmissionCount = 0;
    uint64_t renderTargetBytes = 0;
    uint64_t renderGraphBytes = 0;
    uint64_t transientPoolBytes = 0;
    uint64_t materialUboBytes = 0;
    uint64_t scheduledReleaseBytes = 0;

    size_t materialRenderDataCount = 0;
    size_t runtimeMaterialCount = 0;
    size_t assetMaterialCount = 0;
    size_t materialDescriptorSetCount = 0;
    size_t pendingMaterialTextureDescriptorSetCount = 0;
    size_t retiredMaterialDescriptorSetCount = 0;
    size_t materialDescriptorPoolCount = 0;
    size_t materialPipelineCount = 0;
    size_t shadowMaterialDescriptorSetCount = 0;
    size_t shadowMaterialDescriptorPoolCount = 0;
    uint64_t shadowMaterialBindingCacheHits = 0;
    uint64_t shadowMaterialBindingCacheMisses = 0;
    uint64_t shadowMaterialBindingRetirements = 0;
    size_t runtimeMeshEntryCount = 0;
    uint64_t runtimeMeshBytes = 0;

    uint64_t trackedBytes = 0;
    uint64_t unclassifiedBytes = 0;
    uint64_t effectiveAllocationBytes = 0;
    uint64_t overBudgetBytes = 0;
};

struct MaterialGpuResidencySnapshot
{
    uint64_t uboBytes = 0;
    size_t renderDataCount = 0;
    size_t runtimeMaterialCount = 0;
    size_t assetMaterialCount = 0;
    size_t descriptorSetCount = 0;
    size_t pendingTextureDescriptorSetCount = 0;
    size_t retiredDescriptorSetCount = 0;
    size_t descriptorPoolCount = 0;
    size_t pipelineCount = 0;
    size_t shadowDescriptorSetCount = 0;
    size_t shadowDescriptorPoolCount = 0;
    uint64_t shadowBindingCacheHits = 0;
    uint64_t shadowBindingCacheMisses = 0;
    uint64_t shadowBindingRetirements = 0;
};

struct GpuEvictionCandidate
{
    uint64_t lastUsedFrame = 0;
    uint64_t residentBytes = 0;
    bool valid = false;
};

enum class GpuAssetDomain : uint8_t
{
    Mesh,
    Texture,
};

struct GpuAssetResidencyRecord
{
    std::string guid;
    uint64_t runtimeVersion = 0;
    GpuAssetDomain domain = GpuAssetDomain::Texture;
    uint64_t residentBytes = 0;
    uint64_t lastUsedFrame = 0;
    bool pending = false;
    bool pinned = false;
};

} // namespace infernux
