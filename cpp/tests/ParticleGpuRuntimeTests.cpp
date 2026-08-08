#include <core/config/EngineConfig.h>
#include <core/types/ColorSpace.h>
#include <function/renderer/SceneDepthResolver.h>
#include <function/renderer/VkTextureCache.h>
#include <function/renderer/particle/ParticleGpuBillboardRenderer.h>
#include <function/renderer/particle/ParticleGpuBounds.h>
#include <function/renderer/particle/ParticleGpuCollisionScene.h>
#include <function/renderer/particle/ParticleGpuCuller.h>
#include <function/renderer/particle/ParticleGpuDrawRegistry.h>
#include <function/renderer/particle/ParticleGpuMeshRenderer.h>
#include <function/renderer/particle/ParticleGpuMigrator.h>
#include <function/renderer/particle/ParticleGpuRibbonRenderer.h>
#include <function/renderer/particle/ParticleGpuRibbonTopology.h>
#include <function/renderer/particle/ParticleGpuRuntime.h>
#include <function/renderer/particle/ParticleGpuSorter.h>
#include <function/renderer/particle/ParticleRenderGraph.h>
#include <function/renderer/rhi/GpuRetirementQueue.h>
#include <function/renderer/rhi/RhiBuffer.h>
#include <function/resources/InxMaterial/InxMaterial.h>

#include <glm/gtc/matrix_transform.hpp>

#include <algorithm>
#include <array>
#include <cassert>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <string_view>
#include <unordered_map>
#include <vector>

namespace
{

using namespace infernux;

constexpr size_t RequiredGpuKernelStageCount = static_cast<size_t>(particle::GpuKernelStage::Count) - 1u;
constexpr size_t FusedGpuKernelStageCount = static_cast<size_t>(particle::GpuKernelStage::Count);

std::array<float, 4> ExpectedLinearTint(const glm::vec4 &authoredSrgb)
{
    const glm::vec4 linear = inx::color::SrgbToLinear(authoredSrgb);
    return {linear.x, linear.y, linear.z, linear.w};
}

float FloatFromBits(uint32_t bits)
{
    float value = 0.0f;
    std::memcpy(&value, &bits, sizeof(value));
    return value;
}

bool NearlyEqual(float left, float right)
{
    return std::abs(left - right) <= 1.0e-6f;
}

struct FakeDevice final : rhi::Device
{
    std::vector<rhi::BufferDesc> buffers;
    std::vector<rhi::TextureDesc> textures;
    std::vector<rhi::TextureViewDesc> textureViews;
    std::vector<rhi::SamplerDesc> samplers;
    uint32_t shaderCreates = 0;
    std::vector<rhi::BindingLayoutDesc> layouts;
    std::vector<rhi::BindGroupDesc> bindGroups;
    std::vector<uint32_t> layoutEntryCounts;
    std::vector<uint32_t> groupBufferCounts;
    std::vector<uint32_t> groupTextureCounts;
    std::vector<rhi::GraphicsPipelineDesc> graphicsPipelineDescs;
    std::vector<rhi::ComputePipelineDesc> computePipelineDescs;
    std::vector<std::vector<uint8_t>> initialBufferBytes;
    uint32_t layoutCreates = 0;
    uint32_t groupCreates = 0;
    uint32_t graphicsPipelineCreates = 0;
    uint32_t pipelineCreates = 0;
    uint32_t bufferReleases = 0;
    uint32_t textureReleases = 0;
    uint32_t samplerReleases = 0;
    uint32_t shaderReleases = 0;
    uint32_t layoutReleases = 0;
    uint32_t groupReleases = 0;
    uint32_t graphicsPipelineReleases = 0;
    uint32_t pipelineReleases = 0;
    uint32_t writes = 0;
    uint32_t readbacks = 0;
    std::vector<std::vector<uint8_t>> writtenBytes;
    std::vector<uint64_t> writtenOffsets;
    std::vector<rhi::BufferHandle> writtenBuffers;
    uint32_t nextIndex = 1;

    rhi::BufferHandle CreateBuffer(const rhi::BufferDesc &desc) override
    {
        buffers.push_back(desc);
        const auto *begin = static_cast<const uint8_t *>(desc.initialData);
        initialBufferBytes.emplace_back();
        if (begin && desc.initialDataBytes)
            initialBufferBytes.back().assign(begin, begin + desc.initialDataBytes);
        return {nextIndex++, 1};
    }

    rhi::TextureHandle CreateTexture(const rhi::TextureDesc &desc) override
    {
        textures.push_back(desc);
        return {nextIndex++, 1};
    }

    rhi::TextureViewHandle CreateTextureView(const rhi::TextureViewDesc &desc) override
    {
        textureViews.push_back(desc);
        return {nextIndex++, 1};
    }

    rhi::SamplerHandle CreateSampler(const rhi::SamplerDesc &desc) override
    {
        samplers.push_back(desc);
        return {nextIndex++, 1};
    }

    rhi::ShaderModuleHandle CreateShaderModule(const rhi::ShaderModuleDesc &desc) override
    {
        assert(desc.spirv && desc.wordCount > 0);
        ++shaderCreates;
        return {nextIndex++, 1};
    }

    rhi::BindingLayoutHandle CreateBindingLayout(const rhi::BindingLayoutDesc &desc) override
    {
        layouts.push_back(desc);
        layoutEntryCounts.push_back(desc.entryCount);
        ++layoutCreates;
        return {nextIndex++, 1};
    }

    rhi::BindGroupHandle CreateBindGroup(const rhi::BindGroupDesc &desc) override
    {
        assert(desc.layout.IsValid());
        bindGroups.push_back(desc);
        groupBufferCounts.push_back(desc.bufferCount);
        groupTextureCounts.push_back(desc.textureCount);
        ++groupCreates;
        return {nextIndex++, 1};
    }

    rhi::ComputePipelineHandle CreateComputePipeline(const rhi::ComputePipelineDesc &desc) override
    {
        assert(desc.computeShader.IsValid() &&
               (desc.bindingLayoutCount == 1 || desc.bindingLayoutCount == 2 || desc.bindingLayoutCount == 4 ||
                desc.bindingLayoutCount == 5 || desc.bindingLayoutCount == 6 || desc.bindingLayoutCount == 7 ||
                desc.bindingLayoutCount == 8) &&
               (desc.pushConstantBytes == 16 || desc.pushConstantBytes == 32 || desc.pushConstantBytes == 48 ||
                desc.pushConstantBytes == 64 || desc.pushConstantBytes == 80 || desc.pushConstantBytes == 112));
        computePipelineDescs.push_back(desc);
        ++pipelineCreates;
        return {nextIndex++, 1};
    }

    rhi::GraphicsPipelineHandle CreateGraphicsPipeline(const rhi::GraphicsPipelineDesc &desc) override
    {
        graphicsPipelineDescs.push_back(desc);
        ++graphicsPipelineCreates;
        return {nextIndex++, 1};
    }

    bool WriteBuffer(rhi::BufferHandle handle, uint64_t offset, const void *data, uint64_t byteSize) override
    {
        assert(handle.IsValid() && data && byteSize > 0);
        const auto *begin = static_cast<const uint8_t *>(data);
        writtenBytes.emplace_back(begin, begin + byteSize);
        writtenOffsets.push_back(offset);
        writtenBuffers.push_back(handle);
        ++writes;
        return true;
    }

    bool ReadBuffer(rhi::BufferHandle, uint64_t, void *, uint64_t) override
    {
        ++readbacks;
        return false;
    }

    void Release(rhi::BufferHandle handle) noexcept override
    {
        bufferReleases += handle.IsValid() ? 1u : 0u;
    }
    void Release(rhi::TextureHandle handle) noexcept override
    {
        textureReleases += handle.IsValid() ? 1u : 0u;
    }
    void Release(rhi::TextureViewHandle handle) noexcept override
    {
        textureReleases += handle.IsValid() ? 1u : 0u;
    }
    void Release(rhi::SamplerHandle handle) noexcept override
    {
        samplerReleases += handle.IsValid() ? 1u : 0u;
    }
    void Release(rhi::ShaderModuleHandle handle) noexcept override
    {
        shaderReleases += handle.IsValid() ? 1u : 0u;
    }
    void Release(rhi::BindingLayoutHandle handle) noexcept override
    {
        layoutReleases += handle.IsValid() ? 1u : 0u;
    }
    void Release(rhi::BindGroupHandle handle) noexcept override
    {
        groupReleases += handle.IsValid() ? 1u : 0u;
    }
    void Release(rhi::GraphicsPipelineHandle handle) noexcept override
    {
        graphicsPipelineReleases += handle.IsValid() ? 1u : 0u;
    }
    void Release(rhi::ComputePipelineHandle handle) noexcept override
    {
        pipelineReleases += handle.IsValid() ? 1u : 0u;
    }
};

using TestTextureSlots = std::unordered_map<std::string, std::shared_ptr<rhi::TextureGpuViewSlot>>;

particle::GpuBillboardTextureLease AcquireTestTexture(FakeDevice &device, TestTextureSlots &slots,
                                                      const std::string &sourceId, uint64_t revision,
                                                      rhi::TextureViewHandle view, rhi::SamplerHandle sampler)
{
    const std::string canonicalSourceId = sourceId.empty() ? "white" : sourceId;
    auto &slot = slots[canonicalSourceId];
    if (!slot)
        slot = std::make_shared<rhi::TextureGpuViewSlot>(canonicalSourceId);
    auto publication = slot->Acquire();
    if (!publication || publication->GetRevision() != revision) {
        auto owner = std::shared_ptr<const void>(new uint32_t(static_cast<uint32_t>(revision)),
                                                 [&device, view, sampler](const void *value) {
                                                     device.Release(view);
                                                     device.Release(sampler);
                                                     delete static_cast<const uint32_t *>(value);
                                                 });
        auto next = std::make_shared<const rhi::TextureGpuView>(canonicalSourceId, revision, rhi::TextureHandle{}, view,
                                                                sampler, 1, std::move(owner));
        (void)slot->Publish(next);
        publication = std::move(next);
    }
    return {particle::GpuBillboardTextureStatus::Ready, publication->GetView(), publication->GetSampler(), slot,
            std::move(publication)};
}

struct CommandTrace
{
    std::vector<rhi::ComputePipelineHandle> pipelines;
    std::vector<rhi::BindGroupHandle> groups;
    std::vector<uint32_t> groupSets;
    std::vector<particle::GpuParticlePushConstants> constants;
    std::vector<uint32_t> dispatches;
    std::vector<rhi::BufferHandle> indirectBuffers;
    std::vector<uint64_t> indirectOffsets;

    static void BindPipeline(void *context, rhi::ComputePipelineHandle pipeline)
    {
        static_cast<CommandTrace *>(context)->pipelines.push_back(pipeline);
    }
    static void BindGroup(void *context, rhi::ComputePipelineHandle, uint32_t setIndex, rhi::BindGroupHandle group)
    {
        assert(setIndex < rhi::ComputePipelineDesc::MaxBindingLayouts && group.IsValid());
        auto &trace = *static_cast<CommandTrace *>(context);
        trace.groups.push_back(group);
        trace.groupSets.push_back(setIndex);
    }
    static void PushConstants(void *context, rhi::ComputePipelineHandle, uint32_t byteSize, const void *data)
    {
        assert(byteSize == sizeof(particle::GpuParticlePushConstants));
        particle::GpuParticlePushConstants value;
        std::memcpy(&value, data, sizeof(value));
        static_cast<CommandTrace *>(context)->constants.push_back(value);
    }
    static void Dispatch(void *context, uint32_t x, uint32_t y, uint32_t z)
    {
        assert(y == 1 && z == 1);
        static_cast<CommandTrace *>(context)->dispatches.push_back(x);
    }
    static void DispatchIndirect(void *context, rhi::BufferHandle buffer, uint64_t offset)
    {
        auto &trace = *static_cast<CommandTrace *>(context);
        trace.indirectBuffers.push_back(buffer);
        trace.indirectOffsets.push_back(offset);
    }
};

struct ContinuationTrace
{
    std::vector<rhi::ComputePipelineHandle> pipelines;
    std::vector<rhi::BindGroupHandle> groups;
    std::vector<uint32_t> groupSets;
    std::vector<particle::GpuParticleContinuationConstants> constants;
    std::vector<std::array<uint32_t, 3>> dispatches;
    std::vector<rhi::BufferHandle> indirectBuffers;
    std::vector<uint64_t> indirectOffsets;

    static void BindPipeline(void *context, rhi::ComputePipelineHandle pipeline)
    {
        static_cast<ContinuationTrace *>(context)->pipelines.push_back(pipeline);
    }
    static void BindGroup(void *context, rhi::ComputePipelineHandle, uint32_t setIndex, rhi::BindGroupHandle group)
    {
        assert(setIndex < rhi::ComputePipelineDesc::MaxBindingLayouts && group.IsValid());
        auto &trace = *static_cast<ContinuationTrace *>(context);
        trace.groups.push_back(group);
        trace.groupSets.push_back(setIndex);
    }
    static void PushConstants(void *context, rhi::ComputePipelineHandle, uint32_t byteSize, const void *data)
    {
        assert(byteSize == sizeof(particle::GpuParticleContinuationConstants));
        particle::GpuParticleContinuationConstants value;
        std::memcpy(&value, data, sizeof(value));
        static_cast<ContinuationTrace *>(context)->constants.push_back(value);
    }
    static void Dispatch(void *context, uint32_t x, uint32_t y, uint32_t z)
    {
        static_cast<ContinuationTrace *>(context)->dispatches.push_back({x, y, z});
    }
    static void DispatchIndirect(void *context, rhi::BufferHandle buffer, uint64_t offset)
    {
        auto &trace = *static_cast<ContinuationTrace *>(context);
        trace.indirectBuffers.push_back(buffer);
        trace.indirectOffsets.push_back(offset);
    }
};

struct DepthResolveTrace
{
    rhi::ComputePipelineHandle pipeline;
    rhi::BindGroupHandle group;
    std::array<uint32_t, 4> constants{};
    std::array<uint32_t, 3> dispatch{};

    static void BindPipeline(void *context, rhi::ComputePipelineHandle pipeline)
    {
        static_cast<DepthResolveTrace *>(context)->pipeline = pipeline;
    }
    static void BindGroup(void *context, rhi::ComputePipelineHandle, uint32_t setIndex, rhi::BindGroupHandle group)
    {
        assert(setIndex == 0);
        static_cast<DepthResolveTrace *>(context)->group = group;
    }
    static void PushConstants(void *context, rhi::ComputePipelineHandle, uint32_t byteSize, const void *data)
    {
        assert(byteSize == 16);
        std::memcpy(static_cast<DepthResolveTrace *>(context)->constants.data(), data, byteSize);
    }
    static void Dispatch(void *context, uint32_t x, uint32_t y, uint32_t z)
    {
        static_cast<DepthResolveTrace *>(context)->dispatch = {x, y, z};
    }
    static void DispatchIndirect(void *, rhi::BufferHandle, uint64_t)
    {
    }
};

struct EventTransferTrace
{
    struct Copy
    {
        rhi::BufferHandle source;
        rhi::BufferHandle destination;
        rhi::BufferCopyRegion region;
    };
    std::vector<Copy> copies;

    static void CopyBuffer(void *context, rhi::BufferHandle source, rhi::BufferHandle destination,
                           const rhi::BufferCopyRegion &region)
    {
        static_cast<EventTransferTrace *>(context)->copies.push_back({source, destination, region});
    }
    static void CopyTexture(void *, rhi::TextureHandle, rhi::TextureHandle, const rhi::TextureCopyRegion &)
    {
    }
    static void ResolveTexture(void *, rhi::TextureHandle, rhi::TextureHandle, const rhi::TextureResolveRegion &)
    {
    }
};

struct GraphicsTrace
{
    std::vector<rhi::GraphicsPipelineHandle> pipelines;
    std::vector<rhi::BindGroupHandle> groups;
    std::vector<uint32_t> groupSets;
    std::vector<particle::GpuBillboardViewConstants> constants;
    std::vector<rhi::BufferHandle> indirectBuffers;

    static void BindPipeline(void *context, rhi::GraphicsPipelineHandle pipeline)
    {
        static_cast<GraphicsTrace *>(context)->pipelines.push_back(pipeline);
    }
    static void BindGroup(void *context, rhi::GraphicsPipelineHandle, uint32_t setIndex, rhi::BindGroupHandle group)
    {
        assert(setIndex <= 2);
        auto &trace = *static_cast<GraphicsTrace *>(context);
        trace.groups.push_back(group);
        trace.groupSets.push_back(setIndex);
    }
    static void PushConstants(void *context, rhi::GraphicsPipelineHandle, rhi::ShaderStage stages, uint32_t byteSize,
                              const void *data)
    {
        assert(stages == (rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment) &&
               byteSize == sizeof(particle::GpuBillboardViewConstants));
        particle::GpuBillboardViewConstants value;
        std::memcpy(&value, data, sizeof(value));
        static_cast<GraphicsTrace *>(context)->constants.push_back(value);
    }
    static void Draw(void *, uint32_t, uint32_t, uint32_t, uint32_t)
    {
    }
    static void DrawIndirect(void *context, rhi::BufferHandle buffer, uint64_t offset, uint32_t drawCount,
                             uint32_t stride)
    {
        assert(offset == 0 && drawCount == 1 && stride == 16);
        static_cast<GraphicsTrace *>(context)->indirectBuffers.push_back(buffer);
    }
};

struct MigrationTrace
{
    std::vector<rhi::ComputePipelineHandle> pipelines;
    std::vector<particle::GpuParticleMigrationConstants> constants;
    std::vector<uint32_t> dispatches;

    static void BindPipeline(void *context, rhi::ComputePipelineHandle pipeline)
    {
        static_cast<MigrationTrace *>(context)->pipelines.push_back(pipeline);
    }
    static void BindGroup(void *, rhi::ComputePipelineHandle, uint32_t setIndex, rhi::BindGroupHandle group)
    {
        assert(setIndex == 0 && group.IsValid());
    }
    static void PushConstants(void *context, rhi::ComputePipelineHandle, uint32_t byteSize, const void *data)
    {
        assert(byteSize == sizeof(particle::GpuParticleMigrationConstants));
        particle::GpuParticleMigrationConstants value;
        std::memcpy(&value, data, sizeof(value));
        static_cast<MigrationTrace *>(context)->constants.push_back(value);
    }
    static void Dispatch(void *context, uint32_t x, uint32_t y, uint32_t z)
    {
        assert(y == 1 && z == 1);
        static_cast<MigrationTrace *>(context)->dispatches.push_back(x);
    }
    static void DispatchIndirect(void *, rhi::BufferHandle, uint64_t)
    {
    }
};

struct SortTrace
{
    std::vector<rhi::ComputePipelineHandle> pipelines;
    std::vector<rhi::BindGroupHandle> groups;
    std::vector<particle::GpuParticleSortConstants> constants;
    std::vector<uint32_t> dispatches;
    std::vector<rhi::BufferHandle> indirectDispatches;

    static void BindPipeline(void *context, rhi::ComputePipelineHandle pipeline)
    {
        static_cast<SortTrace *>(context)->pipelines.push_back(pipeline);
    }
    static void BindGroup(void *context, rhi::ComputePipelineHandle, uint32_t setIndex, rhi::BindGroupHandle group)
    {
        assert(setIndex == 0);
        static_cast<SortTrace *>(context)->groups.push_back(group);
    }
    static void PushConstants(void *context, rhi::ComputePipelineHandle, uint32_t byteSize, const void *data)
    {
        assert(byteSize == sizeof(particle::GpuParticleSortConstants));
        particle::GpuParticleSortConstants value;
        std::memcpy(&value, data, sizeof(value));
        static_cast<SortTrace *>(context)->constants.push_back(value);
    }
    static void Dispatch(void *context, uint32_t x, uint32_t y, uint32_t z)
    {
        assert(y == 1 && z == 1);
        static_cast<SortTrace *>(context)->dispatches.push_back(x);
    }
    static void DispatchIndirect(void *context, rhi::BufferHandle buffer, uint64_t offset)
    {
        assert(offset == 0);
        static_cast<SortTrace *>(context)->indirectDispatches.push_back(buffer);
    }
};

struct RibbonTrace
{
    std::vector<rhi::ComputePipelineHandle> pipelines;
    std::vector<rhi::BindGroupHandle> groups;
    std::vector<particle::GpuParticleRibbonConstants> constants;
    std::vector<uint32_t> dispatches;
    std::vector<rhi::BufferHandle> indirectDispatches;

    static void BindPipeline(void *context, rhi::ComputePipelineHandle pipeline)
    {
        static_cast<RibbonTrace *>(context)->pipelines.push_back(pipeline);
    }
    static void BindGroup(void *context, rhi::ComputePipelineHandle, uint32_t setIndex, rhi::BindGroupHandle group)
    {
        assert(setIndex == 0 && group.IsValid());
        static_cast<RibbonTrace *>(context)->groups.push_back(group);
    }
    static void PushConstants(void *context, rhi::ComputePipelineHandle, uint32_t byteSize, const void *data)
    {
        assert(byteSize == sizeof(particle::GpuParticleRibbonConstants));
        particle::GpuParticleRibbonConstants value;
        std::memcpy(&value, data, sizeof(value));
        static_cast<RibbonTrace *>(context)->constants.push_back(value);
    }
    static void Dispatch(void *context, uint32_t x, uint32_t y, uint32_t z)
    {
        assert(y == 1 && z == 1);
        static_cast<RibbonTrace *>(context)->dispatches.push_back(x);
    }
    static void DispatchIndirect(void *context, rhi::BufferHandle buffer, uint64_t offset)
    {
        assert(offset == 0 && buffer.IsValid());
        static_cast<RibbonTrace *>(context)->indirectDispatches.push_back(buffer);
    }
};

struct CullTrace
{
    std::vector<rhi::ComputePipelineHandle> pipelines;
    std::vector<rhi::BindGroupHandle> groups;
    std::vector<particle::GpuParticleCullConstants> constants;
    std::vector<uint32_t> dispatches;
    std::vector<rhi::BufferHandle> indirectDispatches;

    static void BindPipeline(void *context, rhi::ComputePipelineHandle pipeline)
    {
        static_cast<CullTrace *>(context)->pipelines.push_back(pipeline);
    }
    static void BindGroup(void *context, rhi::ComputePipelineHandle, uint32_t setIndex, rhi::BindGroupHandle group)
    {
        assert(setIndex == 0);
        static_cast<CullTrace *>(context)->groups.push_back(group);
    }
    static void PushConstants(void *context, rhi::ComputePipelineHandle, uint32_t byteSize, const void *data)
    {
        assert(byteSize == sizeof(particle::GpuParticleCullConstants));
        particle::GpuParticleCullConstants value;
        std::memcpy(&value, data, sizeof(value));
        static_cast<CullTrace *>(context)->constants.push_back(value);
    }
    static void Dispatch(void *context, uint32_t x, uint32_t y, uint32_t z)
    {
        assert(y == 1 && z == 1);
        static_cast<CullTrace *>(context)->dispatches.push_back(x);
    }
    static void DispatchIndirect(void *context, rhi::BufferHandle buffer, uint64_t offset)
    {
        assert(offset == 0);
        static_cast<CullTrace *>(context)->indirectDispatches.push_back(buffer);
    }
};

struct BoundsTrace
{
    std::vector<rhi::ComputePipelineHandle> pipelines;
    std::vector<rhi::BindGroupHandle> groups;
    std::vector<particle::GpuParticleBoundsConstants> constants;
    std::vector<uint32_t> dispatches;
    std::vector<rhi::BufferHandle> indirectDispatches;

    static void BindPipeline(void *context, rhi::ComputePipelineHandle pipeline)
    {
        static_cast<BoundsTrace *>(context)->pipelines.push_back(pipeline);
    }
    static void BindGroup(void *context, rhi::ComputePipelineHandle, uint32_t setIndex, rhi::BindGroupHandle group)
    {
        assert(setIndex == 0);
        static_cast<BoundsTrace *>(context)->groups.push_back(group);
    }
    static void PushConstants(void *context, rhi::ComputePipelineHandle, uint32_t byteSize, const void *data)
    {
        assert(byteSize == sizeof(particle::GpuParticleBoundsConstants));
        particle::GpuParticleBoundsConstants value;
        std::memcpy(&value, data, sizeof(value));
        static_cast<BoundsTrace *>(context)->constants.push_back(value);
    }
    static void Dispatch(void *context, uint32_t x, uint32_t y, uint32_t z)
    {
        assert(y == 1 && z == 1);
        static_cast<BoundsTrace *>(context)->dispatches.push_back(x);
    }
    static void DispatchIndirect(void *context, rhi::BufferHandle buffer, uint64_t offset)
    {
        assert(offset == 0);
        static_cast<BoundsTrace *>(context)->indirectDispatches.push_back(buffer);
    }
};

} // namespace

int main()
{
    {
        FakeDevice continuationDevice;
        const auto ownerLayout = continuationDevice.CreateBindingLayout({});
        rhi::BindGroupDesc ownerGroupDesc;
        ownerGroupDesc.layout = ownerLayout;
        const auto ownerGroup = continuationDevice.CreateBindGroup(ownerGroupDesc);
        const auto emptyLayout = continuationDevice.CreateBindingLayout({});
        rhi::BindGroupDesc emptyGroupDesc;
        emptyGroupDesc.layout = emptyLayout;
        const auto emptyGroup = continuationDevice.CreateBindGroup(emptyGroupDesc);
        std::array<uint32_t, 5> continuationShader = {0x07230203u, 0u, 0u, 0u, 0u};
        particle::GpuParticleContinuationDesc continuationDesc;
        continuationDesc.capacity = 513;
        continuationDesc.particleCapacity = 257;
        continuationDesc.recordStride = 96;
        continuationDesc.laneCount = 5;
        continuationDesc.joinCount = 2;
        continuationDesc.initialProgramGeneration = 41;
        continuationDesc.ownerLayout = ownerLayout;
        continuationDesc.ownerGroup = ownerGroup;
        continuationDesc.dataInterfaceLayout = emptyLayout;
        continuationDesc.dataInterfaceGroup = emptyGroup;
        continuationDesc.vectorFieldLayout = emptyLayout;
        continuationDesc.vectorFieldGroup = emptyGroup;
        continuationDesc.graphSpawnLayout = {0xffe0u, 1};
        continuationDesc.emptyLayout = emptyLayout;
        continuationDesc.emptyGroup = emptyGroup;
        continuationDesc.collisionSceneLayout = emptyLayout;
        continuationDesc.collisionSceneGroup = emptyGroup;
        continuationDesc.contactLayout = emptyLayout;
        continuationDesc.contactGroup = emptyGroup;
        continuationDesc.program = {
            {continuationShader.data(), continuationShader.size()},
            {continuationShader.data(), continuationShader.size()},
            {continuationShader.data(), continuationShader.size()},
        };

        auto current = std::make_shared<particle::ParticleGpuContinuationRuntime>();
        assert(current->Create(continuationDevice, continuationDesc));
        assert(current->IsValid() && current->Capacity() == 513 && current->ParticleCapacity() == 257 &&
               current->ProgramGeneration() == 41 && current->ResetSerial() == 1 && current->RecordStride() == 96 &&
               current->LaneCount() == 5 && current->JoinCount() == 2);
        assert(continuationDevice.buffers.size() == 10);
        assert(
            continuationDevice.buffers[0].byteSize ==
                uint64_t(continuationDesc.capacity) * continuationDesc.recordStride &&
            continuationDevice.buffers[1].byteSize == uint64_t(continuationDesc.capacity) * sizeof(uint32_t) &&
            continuationDevice.buffers[2].byteSize == continuationDevice.buffers[1].byteSize &&
            continuationDevice.buffers[3].byteSize == continuationDevice.buffers[1].byteSize &&
            continuationDevice.buffers[4].byteSize == continuationDevice.buffers[1].byteSize &&
            continuationDevice.buffers[5].byteSize == sizeof(particle::GpuParticleContinuationCounters) &&
            continuationDevice.buffers[6].byteSize == particle::ParticleGpuContinuationRuntime::IndirectBufferBytes &&
            continuationDevice.buffers[7].byteSize == particle::ParticleGpuContinuationRuntime::IndirectBufferBytes &&
            continuationDevice.buffers[8].byteSize ==
                uint64_t(continuationDesc.particleCapacity) * continuationDesc.laneCount * sizeof(uint32_t) &&
            continuationDevice.buffers[9].byteSize == uint64_t(continuationDesc.particleCapacity) *
                                                          continuationDesc.joinCount *
                                                          sizeof(particle::GpuParticleContinuationJoinState));
        for (size_t index = 0; index < continuationDevice.buffers.size(); ++index) {
            assert(rhi::HasBufferUsage(continuationDevice.buffers[index].usage, rhi::BufferUsageFlags::Storage));
            assert(continuationDevice.buffers[index].memory == rhi::BufferMemory::DeviceLocal &&
                   continuationDevice.buffers[index].initialData == nullptr &&
                   continuationDevice.buffers[index].initialDataBytes == 0 &&
                   continuationDevice.buffers[index].queueAccess == rhi::QueueAccessFlags::Compute);
        }
        assert(rhi::HasBufferUsage(continuationDevice.buffers[5].usage, rhi::BufferUsageFlags::TransferSource));
        assert(rhi::HasBufferUsage(continuationDevice.buffers[6].usage, rhi::BufferUsageFlags::Indirect) &&
               rhi::HasBufferUsage(continuationDevice.buffers[7].usage, rhi::BufferUsageFlags::Indirect));
        assert(continuationDevice.layouts.back().entryCount == 10);
        assert(continuationDevice.bindGroups.back().bufferCount == 10);
        assert(continuationDevice.pipelineCreates == 3);
        assert(continuationDevice.computePipelineDescs.size() == 3);
        assert(continuationDevice.computePipelineDescs[0].bindingLayoutCount == 1);
        assert(continuationDevice.computePipelineDescs[1].bindingLayoutCount == 1);
        assert(continuationDevice.computePipelineDescs[2].bindingLayoutCount == 8);

        ContinuationTrace continuationTrace;
        const rhi::ComputeCommandEncoder::DispatchTable continuationDispatch = {
            &ContinuationTrace::BindPipeline, &ContinuationTrace::BindGroup, &ContinuationTrace::PushConstants,
            &ContinuationTrace::Dispatch, &ContinuationTrace::DispatchIndirect};
        const rhi::ComputeCommandEncoder continuationEncoder(&continuationTrace, &continuationDispatch);
        current->RequestReset();
        assert(current->ProgramGeneration() == 42 && current->ResetSerial() == 2 && current->Telemetry().resetPending);
        constexpr uint64_t Clock = 0x1122334455667788ull;
        assert(!current->RecordClassify(continuationEncoder, 17, Clock));
        const rhi::BindGroupHandle continuationSpawnGroup{0xffe1u, 1};
        assert(!current->RecordDispatch(continuationEncoder, 17, Clock, 99, 0.125f, continuationSpawnGroup));
        assert(current->RecordPrepare(continuationEncoder, 17, Clock));
        assert(!current->RecordDispatch(continuationEncoder, 17, Clock, 99, 0.125f, continuationSpawnGroup));
        assert(current->RecordClassify(continuationEncoder, 17, Clock));
        assert(!current->RecordClassify(continuationEncoder, 17, Clock));
        assert(current->RecordDispatch(continuationEncoder, 17, Clock, 99, 0.125f, continuationSpawnGroup));
        assert(!current->RecordDispatch(continuationEncoder, 17, Clock, 99, 0.125f, continuationSpawnGroup));
        const std::vector<std::array<uint32_t, 3>> expectedContinuationDispatches = {{{6, 1, 1}}};
        assert(continuationTrace.pipelines.size() == 3 &&
               continuationTrace.groupSets == std::vector<uint32_t>({0, 0, 0, 1, 2, 3, 4, 5, 6, 7}) &&
               continuationTrace.dispatches == expectedContinuationDispatches);
        assert(continuationTrace.indirectBuffers ==
               std::vector<rhi::BufferHandle>(
                   {current->Resources().classifyIndirectArguments, current->Resources().dispatchIndirectArguments}));
        assert(continuationTrace.indirectOffsets == std::vector<uint64_t>({0, 0}));
        assert(continuationTrace.constants.size() == 3 && continuationTrace.constants[0].capacity == 513 &&
               continuationTrace.constants[0].particleCapacity == 257 &&
               continuationTrace.constants[0].programGeneration == 42 &&
               continuationTrace.constants[0].simulationStep == 17 && continuationTrace.constants[0].resetSerial == 2 &&
               continuationTrace.constants[0].resetRequested == 1 &&
               continuationTrace.constants[0].elapsedTimeLow == 0x55667788u &&
               continuationTrace.constants[0].elapsedTimeHigh == 0x11223344u &&
               continuationTrace.constants[1].resetRequested == 0 &&
               continuationTrace.constants[2].resetRequested == 0 && continuationTrace.constants[2].systemSeed == 99 &&
               continuationTrace.constants[2].deltaTime == 0.125f);
        const auto telemetry = current->Telemetry();
        assert(telemetry.capacity == 513 && telemetry.particleCapacity == 257 && telemetry.recordStride == 96 &&
               telemetry.laneCount == 5 && telemetry.joinCount == 2 && telemetry.programGeneration == 42 &&
               telemetry.resetSerial == 2 && telemetry.recordBytes == continuationDevice.buffers[0].byteSize &&
               telemetry.queueBytes == continuationDevice.buffers[1].byteSize &&
               telemetry.laneSlotBytes == continuationDevice.buffers[8].byteSize &&
               telemetry.joinStateBytes == continuationDevice.buffers[9].byteSize &&
               telemetry.prepareRecordCalls == 1 && telemetry.classifyRecordCalls == 1 &&
               telemetry.dispatchRecordCalls == 1 && !telemetry.resetPending && telemetry.gpuCountersOnly &&
               telemetry.gpuCounters == current->Resources().counters &&
               telemetry.gpuClassifyIndirectArguments == current->Resources().classifyIndirectArguments &&
               telemetry.gpuDispatchIndirectArguments == current->Resources().dispatchIndirectArguments);
        assert(continuationDevice.readbacks == 0);

        auto replacement = std::make_shared<particle::ParticleGpuContinuationRuntime>();
        assert(replacement->CreateCompatible(continuationDevice, continuationDesc, *current));
        assert(replacement->IsValid() && replacement->SharesStorageWith(*current) &&
               replacement->ProgramGeneration() == 43 && replacement->ResetSerial() == 1 &&
               continuationDevice.buffers.size() == 10 && continuationDevice.pipelineCreates == 6);

        GpuRetirementQueue retirement;
        retirement.BindSerialSource([] { return rhi::SubmissionSerial{9}; });
        const uint32_t pipelineReleasesBeforeRetire = continuationDevice.pipelineReleases;
        retirement.Retire([retired = std::move(current)]() mutable { retired.reset(); });
        assert(retirement.PendingCount() == 1 && continuationDevice.bufferReleases == 0 &&
               continuationDevice.pipelineReleases == pipelineReleasesBeforeRetire);
        assert(retirement.Collect(8) == 0 && continuationDevice.bufferReleases == 0);
        assert(retirement.Collect(9) == 1 && continuationDevice.bufferReleases == 0 &&
               continuationDevice.pipelineReleases == pipelineReleasesBeforeRetire + 3);
        replacement->Destroy();
        assert(continuationDevice.bufferReleases == 10 && continuationDevice.pipelineReleases == 6 &&
               continuationDevice.groupReleases == 2 && continuationDevice.layoutReleases == 2);
        continuationDevice.Release(ownerGroup);
        continuationDevice.Release(ownerLayout);
        continuationDevice.Release(emptyGroup);
        continuationDevice.Release(emptyLayout);

        FakeDevice invalidDevice;
        const auto invalidOwnerLayout = invalidDevice.CreateBindingLayout({});
        rhi::BindGroupDesc invalidOwnerGroupDesc;
        invalidOwnerGroupDesc.layout = invalidOwnerLayout;
        continuationDesc.ownerLayout = invalidOwnerLayout;
        continuationDesc.ownerGroup = invalidDevice.CreateBindGroup(invalidOwnerGroupDesc);
        continuationDesc.capacity = particle::ParticleGpuContinuationRuntime::MaximumCapacity + 1;
        particle::ParticleGpuContinuationRuntime invalid;
        assert(!invalid.Create(invalidDevice, continuationDesc) && invalidDevice.buffers.empty());
    }

    {
        FakeDevice runtimeDevice;
        std::array<uint32_t, 5> shader = {0x07230203u, 0u, 0u, 0u, 0u};
        particle::GpuEmitterDesc runtimeDesc;
        runtimeDesc.capacity = 64;
        runtimeDesc.stateStride = 16;
        runtimeDesc.eventTypeCount = 5;
        for (auto &kernel : runtimeDesc.kernels)
            kernel = {shader.data(), shader.size()};
        runtimeDesc.kernels[static_cast<size_t>(particle::GpuKernelStage::UpdateRenderingFused)] = {};
        runtimeDesc.collisionSceneHeader = {901, 1};
        runtimeDesc.collisionSceneColliders = {902, 1};
        runtimeDesc.collisionSceneGridOffsets = {903, 1};
        runtimeDesc.collisionSceneGridColliderIndices = {904, 1};
        runtimeDesc.collisionSceneMeshVertices = {905, 1};
        runtimeDesc.collisionSceneMeshIndices = {906, 1};
        runtimeDesc.collisionSceneMeshBvhNodes = {907, 1};
        runtimeDesc.continuation.capacity = 32;
        runtimeDesc.continuation.laneCount = 3;
        runtimeDesc.continuation.joinCount = 1;
        runtimeDesc.continuation.initialProgramGeneration = 7;
        runtimeDesc.continuation.program = {
            {shader.data(), shader.size()},
            {shader.data(), shader.size()},
            {shader.data(), shader.size()},
        };

        particle::ParticleGpuRuntime current;
        particle::ParticleGpuRuntime replacement;
        assert(current.Create(runtimeDevice, runtimeDesc));
        assert(current.IsValid() && current.HasContinuations() && current.ContinuationTelemetry().capacity == 32 &&
               current.ContinuationTelemetry().programGeneration == 7 && current.EventTypeCount() == 5 &&
               current.CounterBufferByteSize() == 112);
        assert(replacement.CreateCompatible(runtimeDevice, runtimeDesc, current));
        assert(replacement.IsValid() && replacement.HasContinuations() && replacement.SharesStateWith(current) &&
               replacement.SharesContinuationStateWith(current) &&
               replacement.ContinuationTelemetry().programGeneration == 8);
        auto incompatibleEventDesc = runtimeDesc;
        incompatibleEventDesc.eventTypeCount = 6;
        particle::ParticleGpuRuntime incompatibleEventRuntime;
        assert(!incompatibleEventRuntime.CreateCompatible(runtimeDevice, incompatibleEventDesc, current));
        auto incompatibleCollisionDesc = runtimeDesc;
        incompatibleCollisionDesc.collisionEnabled = true;
        particle::ParticleGpuRuntime incompatibleCollisionRuntime;
        assert(!incompatibleCollisionRuntime.CreateCompatible(runtimeDevice, incompatibleCollisionDesc, current));
        const auto continuationRecords = current.ContinuationResources().records;
        assert(current.AdoptCompatibleRevision(replacement));
        assert(current.ContinuationTelemetry().programGeneration == 8 &&
               replacement.ContinuationTelemetry().programGeneration == 7 &&
               current.ContinuationResources().records == continuationRecords &&
               replacement.ContinuationResources().records == continuationRecords);
        const uint32_t buffersBeforeRetiredRevision = runtimeDevice.bufferReleases;
        replacement.Destroy();
        assert(runtimeDevice.bufferReleases == buffersBeforeRetiredRevision);
        current.RequestContinuationReset();
        assert(current.ContinuationTelemetry().programGeneration == 9 && current.ContinuationTelemetry().resetPending);
        current.Destroy();
        assert(runtimeDevice.bufferReleases == buffersBeforeRetiredRevision + 18);
    }

    {
        FakeDevice cacheDevice;
        VkTextureCache cache;
        const auto makeTexture = [&](uint32_t base, uint64_t bytes) {
            return std::make_shared<rhi::TextureResource>(cacheDevice, rhi::TextureHandle{base, 1},
                                                          rhi::TextureViewHandle{base + 1, 1},
                                                          rhi::SamplerHandle{base + 2, 1}, bytes);
        };

        auto slot = cache.Insert("asset::linear", makeTexture(100, 100), 1, false, "texture-guid", 1);
        assert(slot && slot->Acquire() && slot->Acquire()->GetRevision() == 1 && cache.GetEntryCount() == 1 &&
               cache.GetResidentBytes() == 100);
        auto retainedRevisionOne = slot->Acquire();
        assert(cache.RequestAssetRevision("texture-guid", 2) == 1 && slot->GetRequestedRevision() == 2 &&
               slot->NeedsRefresh());
        auto replacementSlot = cache.Insert("asset::linear", makeTexture(200, 200), 2, false, "texture-guid", 2);
        assert(replacementSlot == slot && replacementSlot->Acquire()->GetRevision() == 2 &&
               !replacementSlot->NeedsRefresh() && cache.GetResidentBytes() == 300 &&
               cache.GetRetiredLeaseCount() == 1 && cache.GetRetiredLeaseBytes() == 100);
        assert(!cache.FindAsset("asset::linear", "texture-guid", 1, 3));
        assert(cache.FindAsset("asset::linear", "texture-guid", 2, 3) == slot);

        retainedRevisionOne.reset();
        assert(cache.GetResidentBytes() == 200 && cache.GetRetiredLeaseCount() == 0 &&
               cacheDevice.textureReleases == 2 && cacheDevice.samplerReleases == 1);

        auto externalSlot = slot;
        assert(cache.EvictByPrefix("asset") == 1 && cache.GetEntryCount() == 0 && cache.GetResidentBytes() == 200 &&
               cache.GetRetiredLeaseCount() == 1);
        slot.reset();
        replacementSlot.reset();
        externalSlot.reset();
        assert(cache.GetResidentBytes() == 0 && cache.GetRetiredLeaseCount() == 0 && cacheDevice.textureReleases == 4 &&
               cacheDevice.samplerReleases == 2);
    }

    {
        FakeDevice resolveDevice;
        std::array<uint32_t, 5> shader = {0x07230203u, 0u, 0u, 0u, 0u};
        SceneDepthResolver resolver;
        assert(resolver.Initialize(resolveDevice, shader.data(), shader.size()));
        assert(resolveDevice.layouts.size() == 1 && resolveDevice.layouts[0].entryCount == 2);
        assert(resolveDevice.layouts[0].entries[0].type == rhi::BindingType::CombinedTextureSampler);
        assert(resolveDevice.layouts[0].entries[1].type == rhi::BindingType::StorageTexture);

        DepthResolveTrace trace;
        const rhi::ComputeCommandEncoder::DispatchTable dispatch = {
            &DepthResolveTrace::BindPipeline, &DepthResolveTrace::BindGroup, &DepthResolveTrace::PushConstants,
            &DepthResolveTrace::Dispatch, &DepthResolveTrace::DispatchIndirect};
        const rhi::ComputeCommandEncoder encoder(&trace, &dispatch);
        const rhi::TextureViewHandle source{101, 1};
        const rhi::TextureViewHandle destination{102, 1};
        assert(resolver.Record(encoder, source, destination, 1919, 1079, 4));
        assert(resolveDevice.groupCreates == 1 && resolveDevice.bindGroups[0].textureCount == 2);
        assert(resolveDevice.bindGroups[0].textures[0].depthRead);
        assert(resolveDevice.bindGroups[0].textures[1].type == rhi::BindingType::StorageTexture);
        assert((trace.constants == std::array<uint32_t, 4>{1919, 1079, 4, 0}));
        assert((trace.dispatch == std::array<uint32_t, 3>{240, 135, 1}));
        assert(resolver.Record(encoder, source, destination, 1919, 1079, 4));
        assert(resolveDevice.groupCreates == 1);
        const auto groups = resolver.TakeBindGroups();
        assert(groups.size() == 1 && groups[0].IsValid());
        resolveDevice.Release(groups[0]);
        resolver.Destroy();
    }

    {
        FakeDevice migrationDevice;
        std::array<std::array<uint32_t, 5>, 2> migrationWords{};
        for (auto &shader : migrationWords)
            shader[0] = 0x07230203u;
        particle::GpuParticleMigrationDesc migrationDesc;
        migrationDesc.sourceCapacity = 512;
        migrationDesc.destinationCapacity = 256;
        migrationDesc.sourceStride = 64;
        migrationDesc.destinationStride = 80;
        migrationDesc.sourceStates = {900, 1};
        migrationDesc.sourceCounters = {901, 1};
        migrationDesc.sourceCounterByteSize = 80;
        migrationDesc.destinationStates = {902, 1};
        migrationDesc.destinationFreeList = {903, 1};
        migrationDesc.destinationCounters = {904, 1};
        migrationDesc.copyRanges = {{4, 4, 3, 0}, {8, 12, 4, 0}};
        migrationDesc.defaultStateWords.resize(20, 0u);
        migrationDesc.defaultStateWords[16] = 0x3f800000u;
        migrationDesc.program = {
            {migrationWords[0].data(), migrationWords[0].size()},
            {migrationWords[1].data(), migrationWords[1].size()},
        };

        particle::ParticleGpuMigrator migrator;
        assert(migrator.Create(migrationDevice, migrationDesc));
        assert(migrator.IsValid() && !migrator.WasRecorded());
        assert(migrationDevice.buffers.size() == 2 && migrationDevice.writes == 2);
        assert(migrator.Constants().sourceStrideWords == 16 && migrator.Constants().destinationStrideWords == 20 &&
               migrator.Constants().copyRangeCount == 2 && migrator.Constants().invocationCount == 512);

        MigrationTrace trace;
        const rhi::ComputeCommandEncoder::DispatchTable dispatch = {
            &MigrationTrace::BindPipeline, &MigrationTrace::BindGroup, &MigrationTrace::PushConstants,
            &MigrationTrace::Dispatch, &MigrationTrace::DispatchIndirect};
        const rhi::ComputeCommandEncoder encoder(&trace, &dispatch);
        migrator.RecordMigrate(encoder);
        assert(!migrator.WasRecorded() && trace.pipelines.empty());
        migrator.RecordReset(encoder);
        migrator.RecordMigrate(encoder);
        migrator.RecordMigrate(encoder);
        assert(migrator.WasRecorded());
        assert(trace.pipelines.size() == 2 && trace.constants.size() == 2);
        assert(trace.dispatches == std::vector<uint32_t>({1, 2}));
        migrator.Destroy();
        assert(migrationDevice.bufferReleases == 2 && migrationDevice.pipelineReleases == 2 &&
               migrationDevice.groupReleases == 1 && migrationDevice.layoutReleases == 1);

        migrationDesc.copyRanges.clear();
        const size_t buffersBeforeEmptyMigration = migrationDevice.buffers.size();
        assert(migrator.Create(migrationDevice, migrationDesc));
        assert(migrator.Constants().copyRangeCount == 0);
        assert(migrationDevice.buffers.size() == buffersBeforeEmptyMigration + 2 &&
               migrationDevice.buffers[buffersBeforeEmptyMigration].byteSize ==
                   sizeof(particle::GpuParticleMigrationRange) &&
               migrationDevice.buffers[buffersBeforeEmptyMigration + 1].byteSize ==
                   migrationDesc.defaultStateWords.size() * sizeof(uint32_t));
        migrator.Destroy();

        migrationDesc.copyRanges = {{15, 4, 2, 0}};
        assert(!migrator.Create(migrationDevice, migrationDesc));
    }

    {
        FakeDevice collisionDevice;
        particle::ParticleGpuCollisionScene collisionScene;
        assert(collisionScene.Create(collisionDevice, 2, 2));
        assert(collisionScene.IsValid() && collisionScene.Capacity() == 2 && collisionScene.PublishedRevision() == 0 &&
               collisionScene.HasPendingUpload());
        assert(collisionDevice.buffers.size() == 21 && collisionDevice.writes == 2);

        particle::GpuParticleCollisionSceneSnapshot collisionSnapshot;
        collisionSnapshot.revision = 2;
        collisionSnapshot.topologyRevision = 1;
        collisionSnapshot.staticColliders.resize(1);
        collisionSnapshot.staticColliders[0].identity = {1u, 0u, 1u, 0u};
        collisionSnapshot.staticColliders[0].metadata[0] =
            static_cast<uint32_t>(particle::GpuParticleColliderType::Sphere);
        std::string collisionError;
        assert(collisionScene.Publish(collisionSnapshot, &collisionError) && collisionError.empty() &&
               collisionScene.HasPendingUpload() && collisionDevice.writes == 6);
        EventTransferTrace collisionTransferTrace;
        const rhi::TransferCommandEncoder::DispatchTable collisionTransferDispatch = {
            &EventTransferTrace::CopyBuffer,
            &EventTransferTrace::CopyTexture,
            &EventTransferTrace::ResolveTexture,
        };
        const rhi::TransferCommandEncoder collisionTransferEncoder(&collisionTransferTrace, &collisionTransferDispatch);
        assert(collisionScene.RecordPendingUpload(collisionTransferEncoder));
        assert(!collisionScene.HasPendingUpload() && collisionScene.PublishedColliderCount() == 1 &&
               collisionTransferTrace.copies.size() == 4 &&
               collisionTransferTrace.copies[0].region.byteSize == sizeof(particle::GpuParticleCollisionSceneHeader) &&
               collisionTransferTrace.copies[1].region.byteSize == 2 * sizeof(uint32_t) &&
               collisionTransferTrace.copies[2].region.byteSize == sizeof(uint32_t) &&
               collisionTransferTrace.copies[3].region.byteSize == sizeof(particle::GpuParticleColliderRecord));
        assert(collisionScene.PublishedStaticColliderCount() == 1 &&
               collisionScene.PublishedDynamicColliderCount() == 0 &&
               collisionScene.PublishedGridReferenceCount() == 1);
        assert(collisionScene.PublishedRevision() == 2);
        const uint32_t writesBeforeDuplicate = collisionDevice.writes;
        assert(collisionScene.Publish(collisionSnapshot, &collisionError) &&
               collisionDevice.writes == writesBeforeDuplicate);

        collisionSnapshot.revision = 3;
        collisionSnapshot.dynamicColliders.resize(1);
        collisionSnapshot.dynamicColliders[0].identity = {2u, 0u, 2u, 0u};
        collisionSnapshot.dynamicColliders[0].metadata[0] =
            static_cast<uint32_t>(particle::GpuParticleColliderType::Box);
        collisionSnapshot.dynamicColliders[0].worldAabbMin = {8.0f, 0.0f, 0.0f, 0.0f};
        collisionSnapshot.dynamicColliders[0].worldAabbMax = {9.0f, 1.0f, 1.0f, 0.0f};
        collisionSnapshot.dynamicColliders[0].previousWorldAabbMin = {-9.0f, 0.0f, 0.0f, 0.0f};
        collisionSnapshot.dynamicColliders[0].previousWorldAabbMax = {-8.0f, 1.0f, 1.0f, 0.0f};
        const uint32_t writesBeforeDynamic = collisionDevice.writes;
        assert(collisionScene.Publish(collisionSnapshot, &collisionError) &&
               collisionDevice.writes == writesBeforeDynamic + 4);
        EventTransferTrace dynamicTransferTrace;
        const rhi::TransferCommandEncoder dynamicTransferEncoder(&dynamicTransferTrace, &collisionTransferDispatch);
        assert(collisionScene.RecordPendingUpload(dynamicTransferEncoder));
        assert(dynamicTransferTrace.copies.size() == 4 &&
               dynamicTransferTrace.copies[3].region.sourceOffset == sizeof(particle::GpuParticleColliderRecord) &&
               dynamicTransferTrace.copies[3].region.destinationOffset == sizeof(particle::GpuParticleColliderRecord) &&
               dynamicTransferTrace.copies[3].region.byteSize == sizeof(particle::GpuParticleColliderRecord));
        assert(collisionScene.PublishedColliderCount() == 2 && collisionScene.PublishedStaticColliderCount() == 1 &&
               collisionScene.PublishedDynamicColliderCount() == 1);
        assert(collisionScene.PublishedGridReferenceCount() > 2);
        assert(collisionDevice.writtenBuffers[writesBeforeDynamic] != collisionDevice.writtenBuffers[0]);

        collisionSnapshot.revision = 4;
        collisionSnapshot.topologyRevision = 2;
        collisionSnapshot.replaceMeshTopology = true;
        collisionSnapshot.staticColliders[0].metadata[0] =
            static_cast<uint32_t>(particle::GpuParticleColliderType::Mesh);
        collisionSnapshot.staticColliders[0].identity = {0x1234u, 0u, 0u, 0u};
        particle::GpuParticleCollisionMeshGeometry triangleGeometry;
        triangleGeometry.identity = 0x1234u;
        triangleGeometry.positions = {
            {{-1.0f, 0.0f, -1.0f, 0.0f}, {1.0f, 0.0f, -1.0f, 0.0f}, {0.0f, 0.0f, 1.0f, 0.0f}}};
        triangleGeometry.indices = {0u, 1u, 2u};
        collisionSnapshot.meshGeometries = {triangleGeometry};
        const uint32_t writesBeforeTopology = collisionDevice.writes;
        assert(collisionScene.Publish(collisionSnapshot, &collisionError) && collisionError.empty() &&
               collisionDevice.writes == writesBeforeTopology + 8);
        EventTransferTrace topologyTransferTrace;
        const rhi::TransferCommandEncoder topologyTransferEncoder(&topologyTransferTrace, &collisionTransferDispatch);
        assert(collisionScene.RecordPendingUpload(topologyTransferEncoder));
        assert(topologyTransferTrace.copies.size() == 7);
        assert(topologyTransferTrace.copies[4].region.byteSize == 3u * sizeof(std::array<float, 4>));
        assert(topologyTransferTrace.copies[5].region.byteSize == 3u * sizeof(uint32_t));
        assert(topologyTransferTrace.copies[6].region.byteSize == sizeof(particle::GpuParticleCollisionBvhNode));
        assert(collisionScene.PublishedTopologyRevision() == 2 && collisionScene.PublishedMeshVertexCount() == 3 &&
               collisionScene.PublishedMeshIndexCount() == 3 && collisionScene.PublishedMeshBvhNodeCount() == 1);
        collisionSnapshot.replaceMeshTopology = false;
        collisionSnapshot.meshGeometries.clear();

        auto rejectedTopology = collisionSnapshot;
        rejectedTopology.revision = 5;
        rejectedTopology.topologyRevision = 3;
        rejectedTopology.replaceMeshTopology = true;
        auto invalidGeometry = triangleGeometry;
        invalidGeometry.indices[2] = 9u;
        rejectedTopology.meshGeometries = {invalidGeometry};
        const uint32_t writesBeforeRejectedTopology = collisionDevice.writes;
        assert(!collisionScene.Publish(rejectedTopology, &collisionError) && !collisionError.empty() &&
               collisionDevice.writes == writesBeforeRejectedTopology && !collisionScene.HasPendingUpload());
        assert(collisionScene.PublishedRevision() == 4 && collisionScene.PublishedTopologyRevision() == 2 &&
               collisionScene.PublishedMeshVertexCount() == 3 && collisionScene.PublishedMeshIndexCount() == 3 &&
               collisionScene.PublishedMeshBvhNodeCount() == 1);

        rejectedTopology.meshGeometries = {triangleGeometry, triangleGeometry};
        assert(!collisionScene.Publish(rejectedTopology, &collisionError) && !collisionError.empty() &&
               collisionDevice.writes == writesBeforeRejectedTopology && !collisionScene.HasPendingUpload());

        rejectedTopology.topologyRevision = 2;
        rejectedTopology.meshGeometries = {triangleGeometry};
        assert(!collisionScene.Publish(rejectedTopology, &collisionError) && !collisionError.empty() &&
               collisionDevice.writes == writesBeforeRejectedTopology && !collisionScene.HasPendingUpload());

        collisionSnapshot.revision = 5;
        collisionSnapshot.dynamicColliders[0].worldAabbMin[0] = 7.0f;
        const uint32_t writesBeforeTransformOnly = collisionDevice.writes;
        assert(collisionScene.Publish(collisionSnapshot, &collisionError) && collisionError.empty() &&
               collisionDevice.writes == writesBeforeTransformOnly + 4);
        EventTransferTrace transformOnlyTransferTrace;
        const rhi::TransferCommandEncoder transformOnlyTransferEncoder(&transformOnlyTransferTrace,
                                                                       &collisionTransferDispatch);
        assert(collisionScene.RecordPendingUpload(transformOnlyTransferEncoder));
        assert(transformOnlyTransferTrace.copies.size() == 4 &&
               transformOnlyTransferTrace.copies[3].region.byteSize == sizeof(particle::GpuParticleColliderRecord));
        assert(collisionScene.PublishedRevision() == 5 && collisionScene.PublishedTopologyRevision() == 2 &&
               collisionScene.PublishedMeshVertexCount() == 3 && collisionScene.PublishedMeshIndexCount() == 3 &&
               collisionScene.PublishedMeshBvhNodeCount() == 1);

        collisionSnapshot.revision = 3;
        assert(!collisionScene.Publish(collisionSnapshot, &collisionError) && !collisionError.empty());

        collisionSnapshot.revision = 6;
        collisionSnapshot.staticColliders.resize(2);
        collisionSnapshot.dynamicColliders.resize(1);
        assert(!collisionScene.Publish(collisionSnapshot, &collisionError) && !collisionError.empty());
        collisionScene.Destroy();
        assert(!collisionScene.IsValid() && collisionDevice.bufferReleases == 21);

        FakeDevice sortedCollisionDevice;
        particle::ParticleGpuCollisionScene sortedCollisionScene;
        assert(sortedCollisionScene.Create(sortedCollisionDevice, 3, 2));
        particle::GpuParticleCollisionSceneSnapshot sortedSnapshot;
        sortedSnapshot.revision = 2;
        sortedSnapshot.topologyRevision = 1;
        sortedSnapshot.staticColliders.resize(2);
        sortedSnapshot.staticColliders[0].identity = {20u, 0u, 2u, 0u};
        sortedSnapshot.staticColliders[1].identity = {10u, 0u, 1u, 0u};
        sortedSnapshot.dynamicColliders.resize(1);
        sortedSnapshot.dynamicColliders[0].identity = {15u, 0u, 3u, 0u};
        assert(sortedCollisionScene.Publish(sortedSnapshot, &collisionError) && collisionError.empty());
        const auto staticWrite = std::find_if(
            sortedCollisionDevice.writtenBytes.begin(), sortedCollisionDevice.writtenBytes.end(),
            [](const auto &bytes) { return bytes.size() == 2u * sizeof(particle::GpuParticleColliderRecord); });
        assert(staticWrite != sortedCollisionDevice.writtenBytes.end());
        std::array<particle::GpuParticleColliderRecord, 2> sortedStatic{};
        std::memcpy(sortedStatic.data(), staticWrite->data(), staticWrite->size());
        assert(sortedStatic[0].identity[0] == 10u && sortedStatic[1].identity[0] == 20u);

        sortedSnapshot.revision = 3;
        sortedSnapshot.replaceMeshTopology = false;
        sortedSnapshot.staticColliders[1].identity = sortedSnapshot.staticColliders[0].identity;
        assert(!sortedCollisionScene.Publish(sortedSnapshot, &collisionError));
        assert(collisionError.find("duplicate collider identity") != std::string::npos);
        sortedCollisionScene.Destroy();
    }

    {
        FakeDevice sharingDevice;
        std::array<std::array<uint32_t, 4>, static_cast<size_t>(particle::GpuKernelStage::Count)> sharingWords{};
        particle::GpuEmitterDesc sharingDesc;
        sharingDesc.capacity = 128;
        sharingDesc.stateStride = 32;
        sharingDesc.eventTypeCount = 3;
        sharingDesc.collisionSceneHeader = {0xfff0u, 1};
        sharingDesc.collisionSceneColliders = {0xfff1u, 1};
        sharingDesc.collisionSceneGridOffsets = {0xfff2u, 1};
        sharingDesc.collisionSceneGridColliderIndices = {0xfff3u, 1};
        sharingDesc.collisionSceneMeshVertices = {0xfff4u, 1};
        sharingDesc.collisionSceneMeshIndices = {0xfff5u, 1};
        sharingDesc.collisionSceneMeshBvhNodes = {0xfff6u, 1};
        for (size_t index = 0; index < sharingWords.size(); ++index) {
            sharingWords[index][0] = 0x07230203;
            sharingDesc.kernels[index] = {sharingWords[index].data(), sharingWords[index].size()};
        }
        sharingDesc.kernels[static_cast<size_t>(particle::GpuKernelStage::UpdateRenderingFused)] = {};

        particle::ParticleGpuRuntime previous;
        particle::ParticleGpuRuntime compatible;
        assert(previous.Create(sharingDevice, sharingDesc));
        assert(previous.EventTypeCount() == 3 && previous.CounterBufferByteSize() == 96);
        assert(previous.NeedsBootstrap());
        CommandTrace sharingTrace;
        const rhi::ComputeCommandEncoder::DispatchTable sharingDispatch = {
            &CommandTrace::BindPipeline, &CommandTrace::BindGroup, &CommandTrace::PushConstants,
            &CommandTrace::Dispatch, &CommandTrace::DispatchIndirect};
        const rhi::ComputeCommandEncoder sharingEncoder(&sharingTrace, &sharingDispatch);
        const rhi::BindGroupHandle sharingSpawnGroup{0xffe2u, 1};
        previous.RecordBootstrap(sharingEncoder, 13, sharingSpawnGroup);
        assert(!previous.NeedsBootstrap());
        const auto state = previous.StateBuffer();
        const auto counters = previous.CounterBuffer();
        assert(compatible.CreateCompatible(sharingDevice, sharingDesc, previous));
        assert(compatible.SharesStateWith(previous));
        assert(!compatible.NeedsBootstrap());
        assert(compatible.StateBuffer() == state && compatible.CounterBuffer() == counters);
        assert(sharingDevice.buffers.size() == 8);
        assert(previous.AdoptCompatibleRevision(compatible));
        assert(previous.IsValid() && compatible.IsValid() && previous.SharesStateWith(compatible));
        compatible.RequestBootstrap();
        assert(previous.NeedsBootstrap() && compatible.NeedsBootstrap());
        compatible.RecordBootstrap(sharingEncoder, 13, sharingSpawnGroup);
        assert(!previous.NeedsBootstrap() && !compatible.NeedsBootstrap());
        previous.Destroy();
        assert(compatible.IsValid() && sharingDevice.bufferReleases == 0);
        compatible.Destroy();
        assert(sharingDevice.bufferReleases == 8 &&
               sharingDevice.pipelineReleases == 2u * RequiredGpuKernelStageCount && sharingDevice.groupReleases == 4 &&
               sharingDevice.layoutReleases == 6);
    }

    {
        FakeDevice collisionRuntimeDevice;
        std::array<std::array<uint32_t, 4>, static_cast<size_t>(particle::GpuKernelStage::Count)> collisionWords{};
        particle::GpuEmitterDesc collisionDesc;
        collisionDesc.capacity = 32;
        collisionDesc.stateStride = 32;
        collisionDesc.collisionEnabled = true;
        collisionDesc.collisionSceneHeader = {0xffd0u, 1};
        collisionDesc.collisionSceneColliders = {0xffd1u, 1};
        collisionDesc.collisionSceneGridOffsets = {0xffd2u, 1};
        collisionDesc.collisionSceneGridColliderIndices = {0xffd3u, 1};
        collisionDesc.collisionSceneMeshVertices = {0xffd4u, 1};
        collisionDesc.collisionSceneMeshIndices = {0xffd5u, 1};
        collisionDesc.collisionSceneMeshBvhNodes = {0xffd6u, 1};
        for (size_t index = 0; index < collisionWords.size(); ++index) {
            collisionWords[index][0] = 0x07230203u;
            collisionDesc.kernels[index] = {collisionWords[index].data(), collisionWords[index].size()};
        }
        collisionDesc.kernels[static_cast<size_t>(particle::GpuKernelStage::UpdateRenderingFused)] = {};

        particle::ParticleGpuRuntime collisionRuntime;
        assert(collisionRuntime.Create(collisionRuntimeDevice, collisionDesc));
        assert(collisionRuntime.CollisionEnabled() && collisionRuntime.HasContactRuntime() &&
               !collisionRuntime.SupportsFusedUpdateRendering());
        const auto contactTelemetry = collisionRuntime.ContactTelemetry();
        assert(contactTelemetry.particleCapacity == 32 && contactTelemetry.contactsPerParticle == 8 &&
               contactTelemetry.contactRecordCapacity == 256 && contactTelemetry.contactHashCapacity == 512 &&
               contactTelemetry.workItemCapacity == 512 && contactTelemetry.continuationSnapshotCapacity == 1 &&
               contactTelemetry.continuationJoinCapacity == 2 &&
               contactTelemetry.contactBytes == 2u * 256u * sizeof(particle::GpuParticleContactRecord) &&
               contactTelemetry.hashBytes == 2u * 512u * sizeof(particle::GpuParticleContactHashSlot) &&
               contactTelemetry.workItemBytes == 512u * sizeof(particle::GpuParticleContactWorkItem) &&
               contactTelemetry.continuationJoinBytes == 2u * sizeof(particle::GpuParticleContactJoinState));
        assert(collisionRuntimeDevice.layoutEntryCounts == std::vector<uint32_t>({8, 0, 5, 7, 9}));
        assert(collisionRuntimeDevice.groupBufferCounts == std::vector<uint32_t>({8, 0, 7, 9}));
        assert(collisionRuntimeDevice.computePipelineDescs.size() == RequiredGpuKernelStageCount);
        for (const auto &pipeline : collisionRuntimeDevice.computePipelineDescs)
            assert(pipeline.bindingLayoutCount == 8);

        CommandTrace contactTrace;
        const rhi::ComputeCommandEncoder::DispatchTable contactDispatch = {
            &CommandTrace::BindPipeline, &CommandTrace::BindGroup, &CommandTrace::PushConstants,
            &CommandTrace::Dispatch, &CommandTrace::DispatchIndirect};
        const rhi::ComputeCommandEncoder contactEncoder(&contactTrace, &contactDispatch);
        const rhi::BindGroupHandle contactSpawnGroup{0xffcfu, 1};
        collisionRuntime.RecordContactPrepare(contactEncoder, 17u, contactSpawnGroup);
        assert(contactTrace.pipelines.size() == 1 && contactTrace.groups.size() == 8 &&
               contactTrace.groupSets == std::vector<uint32_t>({0, 1, 2, 3, 4, 5, 6, 7}) &&
               contactTrace.constants.size() == 1 && contactTrace.constants[0].simulationStep == 17u &&
               contactTrace.constants[0].invocationCount == 512u &&
               contactTrace.dispatches == std::vector<uint32_t>({2u}));
        assert(collisionRuntime.ContactPrepareRecordCalls() == 1u);
        collisionRuntime.RecordContactSolve(contactEncoder, 17u, contactSpawnGroup);
        assert(contactTrace.pipelines.size() == 2 && contactTrace.groups.size() == 16 &&
               contactTrace.constants.size() == 2 && contactTrace.constants[1].simulationStep == 17u &&
               contactTrace.constants[1].invocationCount == 32u &&
               contactTrace.dispatches == std::vector<uint32_t>({2u, 1u}));
        assert(collisionRuntime.ContactSolveRecordCalls() == 1u);
        collisionRuntime.RecordContactDispatch(contactEncoder, 19u, 17u, 1.0f / 60.0f, contactSpawnGroup, true);
        assert(contactTrace.pipelines.size() == 3 && contactTrace.groups.size() == 24 &&
               contactTrace.constants.size() == 3 && contactTrace.constants[2].systemSeed == 19u &&
               contactTrace.constants[2].simulationStep == 17u && contactTrace.constants[2].diagnosticFlags == 1u &&
               contactTrace.indirectBuffers ==
                   std::vector<rhi::BufferHandle>({collisionRuntime.ContactResources().dispatchIndirect}) &&
               contactTrace.indirectOffsets == std::vector<uint64_t>({0u}));
        assert(collisionRuntime.ContactDispatchRecordCalls() == 1u);
        collisionRuntime.RecordContactPrepare(contactEncoder, 18u, contactSpawnGroup, true);
        assert(contactTrace.constants.size() == 4 && contactTrace.constants[3].simulationStep == 18u &&
               contactTrace.constants[3].diagnosticFlags == 4u &&
               contactTrace.dispatches == std::vector<uint32_t>({2u, 1u, 2u}));
        assert(collisionRuntime.ContactPrepareRecordCalls() == 2u);

        particle::ParticleGpuRuntime compatibleCollisionRuntime;
        assert(compatibleCollisionRuntime.CreateCompatible(collisionRuntimeDevice, collisionDesc, collisionRuntime));
        assert(compatibleCollisionRuntime.SharesStateWith(collisionRuntime) &&
               compatibleCollisionRuntime.SharesContactStateWith(collisionRuntime));
        const auto contactRecords = collisionRuntime.ContactResources().contactRecords;
        assert(collisionRuntime.AdoptCompatibleRevision(compatibleCollisionRuntime));
        assert(collisionRuntime.ContactResources().contactRecords == contactRecords &&
               compatibleCollisionRuntime.ContactResources().contactRecords == contactRecords);
        compatibleCollisionRuntime.Destroy();
        collisionRuntime.Destroy();

        auto missingCollisionBuffer = collisionDesc;
        missingCollisionBuffer.collisionSceneMeshBvhNodes = {};
        assert(!collisionRuntime.Create(collisionRuntimeDevice, missingCollisionBuffer));
    }

    {
        FakeDevice meshInterfaceDevice;
        std::array<uint32_t, 5> shader = {0x07230203u, 0u, 0u, 0u, 0u};
        particle::GpuEmitterDesc meshInterfaceDesc;
        meshInterfaceDesc.capacity = 64;
        meshInterfaceDesc.stateStride = 16;
        for (auto &kernel : meshInterfaceDesc.kernels)
            kernel = {shader.data(), shader.size()};
        meshInterfaceDesc.kernels[static_cast<size_t>(particle::GpuKernelStage::UpdateRenderingFused)] = {};

        const auto meshLease = std::make_shared<uint32_t>(1u);
        particle::GpuMeshInterfaceDesc firstMesh;
        firstMesh.stableId = "surface-a";
        firstMesh.interfaceIndex = 0;
        firstMesh.metadataOffsetWords = 0;
        firstMesh.vertexBinding = 1;
        firstMesh.triangleBinding = 2;
        firstMesh.vertexCount = 24;
        firstMesh.triangleCount = 12;
        firstMesh.edgeCount = 30;
        firstMesh.vertices = {0xfa01u, 1};
        firstMesh.triangles = {0xfa02u, 1};
        firstMesh.keepAlive = meshLease;
        firstMesh.meshToSpace = {
            -2.0f, 0.0f, 0.0f, 0.0f, 0.0f, 3.0f, 0.0f, 0.0f, 0.0f, 0.0f, 4.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f,
        };

        auto secondMesh = firstMesh;
        secondMesh.stableId = "surface-b";
        secondMesh.interfaceIndex = 1;
        secondMesh.metadataOffsetWords = 32;
        secondMesh.vertexBinding = 5;
        secondMesh.triangleBinding = 6;
        secondMesh.influenceBinding = 7;
        secondMesh.paletteBinding = 8;
        secondMesh.worldSpace = true;
        secondMesh.vertexCount = 36;
        secondMesh.triangleCount = 18;
        secondMesh.edgeCount = 48;
        secondMesh.vertices = {0xfa03u, 1};
        secondMesh.triangles = {0xfa04u, 1};
        secondMesh.boneCount = 1;
        secondMesh.poseRevision = 4;
        secondMesh.influences = {0xfa05u, 1};
        secondMesh.initialPalette = {glm::mat4(1.0f)};
        meshInterfaceDesc.meshInterfaces = {firstMesh, secondMesh};

        particle::ParticleGpuRuntime meshInterfaceRuntime;
        assert(meshInterfaceRuntime.Create(meshInterfaceDevice, meshInterfaceDesc));
        assert(meshInterfaceRuntime.IsValid());
        assert(std::find(meshInterfaceDevice.layoutEntryCounts.begin(), meshInterfaceDevice.layoutEntryCounts.end(),
                         9u) != meshInterfaceDevice.layoutEntryCounts.end());
        assert(std::find(meshInterfaceDevice.groupBufferCounts.begin(), meshInterfaceDevice.groupBufferCounts.end(),
                         9u) != meshInterfaceDevice.groupBufferCounts.end());

        particle::GpuParticleTransforms transforms;
        const std::array<float, 16> identity = {
            1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f,
        };
        transforms.emitterToWorld = identity;
        transforms.worldToEmitter = identity;
        transforms.simulationToWorld = identity;
        transforms.worldToSimulation = identity;
        auto updatedPalette = std::make_shared<const std::vector<glm::mat4>>(
            std::vector<glm::mat4>{glm::translate(glm::mat4(1.0f), glm::vec3(0.0f, 2.0f, 0.0f))});
        auto movedSource = identity;
        movedSource[3] = 5.0f;
        assert(meshInterfaceRuntime.UpdateSkinnedMeshSources({{1u, 5u, movedSource, updatedPalette}}));
        assert(meshInterfaceRuntime.UpdateTransforms(transforms));
        assert(meshInterfaceDevice.writes == 3);
        assert(meshInterfaceDevice.writtenBytes.back().size() == 64u * sizeof(uint32_t));
        std::array<uint32_t, 64> metadata{};
        std::memcpy(metadata.data(), meshInterfaceDevice.writtenBytes.back().data(), sizeof(metadata));
        assert(metadata[0] == 24u && metadata[1] == 12u && metadata[2] == 30u);
        assert(metadata[32] == 36u && metadata[33] == 18u && metadata[34] == 48u && metadata[35] == 1u);
        assert(NearlyEqual(FloatFromBits(metadata[32 + 4 + 12]), 5.0f));
        assert(NearlyEqual(FloatFromBits(metadata[4]), -2.0f));
        assert(NearlyEqual(FloatFromBits(metadata[9]), 3.0f));
        assert(NearlyEqual(FloatFromBits(metadata[14]), 4.0f));
        assert(NearlyEqual(FloatFromBits(metadata[20]), -0.5f));
        assert(NearlyEqual(FloatFromBits(metadata[25]), 1.0f / 3.0f));
        assert(NearlyEqual(FloatFromBits(metadata[30]), 0.25f));
        meshInterfaceRuntime.Destroy();

        auto invalidMeshDesc = meshInterfaceDesc;
        invalidMeshDesc.meshInterfaces[1].metadataOffsetWords = 0;
        assert(!meshInterfaceRuntime.Create(meshInterfaceDevice, invalidMeshDesc));
    }

    FakeDevice device;
    GpuRetirementQueue deletionQueue;
    deletionQueue.BindSerialSource([] { return rhi::SubmissionSerial{1}; });
    std::array<std::array<uint32_t, 4>, static_cast<size_t>(particle::GpuKernelStage::Count)> words{};
    particle::GpuEmitterDesc desc;
    desc.capacity = 1000;
    desc.stateStride = 64;
    desc.eventTypeCount = 5;
    desc.collisionSceneHeader = {0xfff2u, 1};
    desc.collisionSceneColliders = {0xfff3u, 1};
    desc.collisionSceneGridOffsets = {0xfff4u, 1};
    desc.collisionSceneGridColliderIndices = {0xfff5u, 1};
    desc.collisionSceneMeshVertices = {0xfff6u, 1};
    desc.collisionSceneMeshIndices = {0xfff7u, 1};
    desc.collisionSceneMeshBvhNodes = {0xfff8u, 1};
    for (size_t index = 0; index < words.size(); ++index) {
        words[index][0] = 0x07230203;
        desc.kernels[index] = {words[index].data(), words[index].size()};
    }
    desc.kernels[static_cast<size_t>(particle::GpuKernelStage::UpdateRenderingFused)] = {};

    particle::ParticleGpuRuntime runtime;
    assert(runtime.Create(device, desc));
    assert(runtime.IsValid() && runtime.Capacity() == 1000 && runtime.StateStride() == 64 &&
           runtime.EventTypeCount() == 5 && !runtime.CollisionEnabled() && !runtime.SupportsFusedUpdateRendering() &&
           runtime.CounterBufferByteSize() == 112);
    assert(device.buffers.size() == 8);
    assert(device.buffers[0].byteSize == 64000);
    assert(rhi::HasBufferUsage(device.buffers[0].usage, rhi::BufferUsageFlags::Storage) &&
           rhi::HasBufferUsage(device.buffers[0].usage, rhi::BufferUsageFlags::TransferSource));
    assert(device.buffers[2].byteSize == 112);
    assert(rhi::HasBufferUsage(device.buffers[2].usage, rhi::BufferUsageFlags::TransferSource));
    assert(device.buffers[3].byteSize ==
           static_cast<uint64_t>(desc.capacity) * particle::ParticleGpuRuntime::RenderInstanceStride);
    assert(rhi::HasBufferUsage(device.buffers[4].usage, rhi::BufferUsageFlags::Indirect));
    assert(device.buffers[5].byteSize == 4000 && device.buffers[5].usage == rhi::BufferUsageFlags::Storage);
    assert(device.buffers[6].byteSize == sizeof(particle::GpuParticleTransforms) &&
           device.buffers[6].memory == rhi::BufferMemory::Upload);
    assert(device.buffers[7].byteSize == sizeof(particle::GpuParticleSimulationControl) &&
           device.buffers[7].usage == (rhi::BufferUsageFlags::Storage | rhi::BufferUsageFlags::TransferSource) &&
           device.buffers[7].memory == rhi::BufferMemory::DeviceLocal && device.initialBufferBytes[7].empty());
    assert(device.shaderCreates == RequiredGpuKernelStageCount && device.shaderReleases == RequiredGpuKernelStageCount);
    assert(device.layoutCreates == 3 && device.groupCreates == 2 &&
           device.pipelineCreates == RequiredGpuKernelStageCount);

    particle::GpuParticleTransforms transforms;
    assert(runtime.UpdateTransforms(transforms));
    assert(device.writes == 1);

    CommandTrace trace;
    const rhi::ComputeCommandEncoder::DispatchTable dispatch = {&CommandTrace::BindPipeline, &CommandTrace::BindGroup,
                                                                &CommandTrace::PushConstants, &CommandTrace::Dispatch,
                                                                &CommandTrace::DispatchIndirect};
    const rhi::ComputeCommandEncoder encoder(&trace, &dispatch);
    const rhi::BindGroupHandle graphSpawnGroup{0xffe3u, 1};
    const rhi::BufferHandle spawnMetadata{0xffe4u, 1};
    runtime.RecordBootstrap(encoder, 7, graphSpawnGroup);
    runtime.RecordInitIndirect(encoder, 300, 100, 2, 7, 9, 1.0f / 60.0f, graphSpawnGroup, spawnMetadata,
                               sizeof(uint32_t) * 4u);
    runtime.RecordUpdate(encoder, 7, 9, 1.0f / 60.0f, graphSpawnGroup, true);
    runtime.RecordRenderReset(encoder, graphSpawnGroup, true);
    runtime.RecordRendering(encoder, 7, 9, graphSpawnGroup);
    assert(trace.pipelines.size() == 5 && trace.groups.size() == 40 && trace.constants.size() == 5);
    assert(trace.groupSets == std::vector<uint32_t>({0, 1, 2, 3, 4, 5, 6, 7, 0, 1, 2, 3, 4, 5, 6, 7, 0, 1, 2, 3,
                                                     4, 5, 6, 7, 0, 1, 2, 3, 4, 5, 6, 7, 0, 1, 2, 3, 4, 5, 6, 7}));
    assert(trace.dispatches == std::vector<uint32_t>({4, 4, 1, 4}));
    assert(trace.indirectBuffers == std::vector<rhi::BufferHandle>({spawnMetadata}));
    assert(trace.indirectOffsets == std::vector<uint64_t>({sizeof(uint32_t) * 4u}));
    assert(trace.constants[1].spawnBaseId == 100 && trace.constants[1].spawnGeneration == 2);
    assert(trace.constants[2].simulationStep == 9);
    assert(trace.constants[1].diagnosticFlags == 0u);
    assert(trace.constants[2].diagnosticFlags == 1u);
    assert(trace.constants[3].diagnosticFlags == 2u);

    auto fusedDesc = desc;
    fusedDesc.supportsFusedUpdateRendering = true;
    fusedDesc.kernels[static_cast<size_t>(particle::GpuKernelStage::UpdateRenderingFused)] = {
        words[static_cast<size_t>(particle::GpuKernelStage::UpdateRenderingFused)].data(),
        words[static_cast<size_t>(particle::GpuKernelStage::UpdateRenderingFused)].size()};
    particle::ParticleGpuRuntime missingFusedRuntime;
    auto missingFusedDesc = fusedDesc;
    missingFusedDesc.kernels[static_cast<size_t>(particle::GpuKernelStage::UpdateRenderingFused)] = {};
    assert(!missingFusedRuntime.CreateCompatible(device, missingFusedDesc, runtime));
    particle::ParticleGpuRuntime fusedRuntime;
    assert(fusedRuntime.CreateCompatible(device, fusedDesc, runtime));
    assert(fusedRuntime.SupportsFusedUpdateRendering());
    trace = {};
    fusedRuntime.RecordUpdateRenderingFused(encoder, 7, 9, 1.0f / 60.0f, graphSpawnGroup);
    assert(trace.pipelines.size() == 1 && trace.dispatches == std::vector<uint32_t>({4}) &&
           trace.constants.size() == 1 && trace.constants[0].simulationStep == 9);
    assert(runtime.AdoptCompatibleRevision(fusedRuntime));
    assert(runtime.SupportsFusedUpdateRendering() && !fusedRuntime.SupportsFusedUpdateRendering());
    particle::GpuParticleFrameRequest routeRequest;
    assert(particle::ParticleRenderGraph::ShouldUseFusedUpdateRendering(routeRequest, runtime));
    routeRequest.render = false;
    assert(!particle::ParticleRenderGraph::ShouldUseFusedUpdateRendering(routeRequest, runtime));
    routeRequest.render = true;
    routeRequest.simulate = false;
    assert(!particle::ParticleRenderGraph::ShouldUseFusedUpdateRendering(routeRequest, runtime));

    FakeDevice boundsDevice;
    std::array<std::array<uint32_t, 5>, 3> boundsWords{};
    for (auto &shader : boundsWords)
        shader[0] = 0x07230203u;
    particle::GpuParticleBoundsDesc boundsDesc;
    boundsDesc.capacity = runtime.Capacity();
    boundsDesc.instances = runtime.InstanceBuffer();
    boundsDesc.sourceIndices = runtime.RenderIndexBuffer();
    boundsDesc.sourceIndirectArguments = runtime.IndirectBuffer();
    boundsDesc.simulationControl = runtime.SimulationControlBuffer();
    boundsDesc.program = {
        {boundsWords[0].data(), boundsWords[0].size()},
        {boundsWords[1].data(), boundsWords[1].size()},
        {boundsWords[2].data(), boundsWords[2].size()},
    };
    particle::GpuParticleBoundsProgramStorage boundsProgramStorage;
    assert(boundsProgramStorage.Assign(boundsDesc.program) && boundsProgramStorage.IsValid());
    assert(boundsProgramStorage.View().prepare.words != boundsDesc.program.prepare.words &&
           boundsProgramStorage.View().reset.words != boundsDesc.program.reset.words &&
           boundsProgramStorage.View().reset.wordCount == boundsDesc.program.reset.wordCount);
    particle::ParticleGpuBounds bounds;
    assert(bounds.Create(boundsDevice, boundsDesc));
    assert(bounds.IsValid() && bounds.Capacity() == 1000 && bounds.InstanceBuffer() == runtime.InstanceBuffer() &&
           bounds.SourceIndexBuffer() == runtime.RenderIndexBuffer() &&
           bounds.SourceIndirectBuffer() == runtime.IndirectBuffer());
    assert(boundsDevice.buffers.size() == 2 &&
           boundsDevice.buffers[0].byteSize == particle::ParticleGpuBounds::BoundsBufferBytes &&
           rhi::HasBufferUsage(boundsDevice.buffers[0].usage, rhi::BufferUsageFlags::Storage) &&
           rhi::HasBufferUsage(boundsDevice.buffers[0].usage, rhi::BufferUsageFlags::TransferSource) &&
           boundsDevice.buffers[1].byteSize == particle::ParticleGpuBounds::DispatchBufferBytes &&
           rhi::HasBufferUsage(boundsDevice.buffers[1].usage, rhi::BufferUsageFlags::Storage) &&
           rhi::HasBufferUsage(boundsDevice.buffers[1].usage, rhi::BufferUsageFlags::Indirect));
    assert(boundsDevice.layouts.size() == 1 && boundsDevice.layouts[0].entryCount == 6 &&
           boundsDevice.bindGroups.size() == 1 && boundsDevice.bindGroups[0].bufferCount == 6 &&
           boundsDevice.shaderCreates == 3 && boundsDevice.shaderReleases == 3 && boundsDevice.pipelineCreates == 3);

    BoundsTrace boundsTrace;
    const rhi::ComputeCommandEncoder::DispatchTable boundsDispatch = {
        &BoundsTrace::BindPipeline, &BoundsTrace::BindGroup, &BoundsTrace::PushConstants, &BoundsTrace::Dispatch,
        &BoundsTrace::DispatchIndirect};
    const rhi::ComputeCommandEncoder boundsEncoder(&boundsTrace, &boundsDispatch);
    bounds.RecordPrepare(boundsEncoder, particle::GpuParticleOffscreenPolicy::PauseWhenOffscreen, false);
    bounds.RecordPrepare(boundsEncoder, particle::GpuParticleOffscreenPolicy::AlwaysSimulate, false);
    const size_t alwaysSimulatePrepareCount = boundsTrace.dispatches.size();
    bounds.RecordPrepare(boundsEncoder, particle::GpuParticleOffscreenPolicy::AlwaysSimulate, false);
    assert(boundsTrace.dispatches.size() == alwaysSimulatePrepareCount &&
           "AlwaysSimulate prepare must be cached until a control input changes");
    bounds.RecordPrepare(boundsEncoder, particle::GpuParticleOffscreenPolicy::AlwaysSimulate, true);
    const size_t forcedPrepareCount = boundsTrace.dispatches.size();
    assert(forcedPrepareCount == alwaysSimulatePrepareCount + 1 &&
           "forceSimulation changes must invalidate the AlwaysSimulate prepare cache");
    bounds.RecordPrepare(boundsEncoder, particle::GpuParticleOffscreenPolicy::AlwaysSimulate, true);
    assert(boundsTrace.dispatches.size() == forcedPrepareCount &&
           "unchanged forceSimulation must reuse the prepared control state");
    bounds.RecordPrepare(boundsEncoder, particle::GpuParticleOffscreenPolicy::AlwaysSimulate, false);
    const size_t unforcedPrepareCount = boundsTrace.dispatches.size();
    assert(unforcedPrepareCount == forcedPrepareCount + 1 &&
           "clearing forceSimulation must invalidate the AlwaysSimulate prepare cache");
    bounds.RecordPrepare(boundsEncoder, particle::GpuParticleOffscreenPolicy::AlwaysSimulate, false);
    assert(boundsTrace.dispatches.size() == unforcedPrepareCount &&
           "an unchanged unforced AlwaysSimulate state must remain cached");
    bounds.RecordPrepare(boundsEncoder, particle::GpuParticleOffscreenPolicy::PauseWhenOffscreen, false);
    bounds.RecordReset(boundsEncoder, particle::GpuParticleBoundsMode::Automatic, {}, {});
    const std::array<float, 3> manualLower = {-4.0f, -2.0f, 1.0f};
    const std::array<float, 3> manualUpper = {8.0f, 6.0f, 9.0f};
    bounds.RecordReset(boundsEncoder, particle::GpuParticleBoundsMode::Manual, manualLower, manualUpper);
    bounds.RecordReduce(boundsEncoder);
    assert(boundsTrace.pipelines.size() == 8 && boundsTrace.groups.size() == 8 && boundsTrace.constants.size() == 8);
    assert(boundsTrace.dispatches == std::vector<uint32_t>({1, 1, 1, 1, 1, 1, 1}));
    assert(boundsTrace.indirectDispatches == std::vector<rhi::BufferHandle>({bounds.DispatchBuffer()}));
    assert(boundsTrace.constants[0].capacity == 1000 &&
           boundsTrace.constants[0].offscreenPolicy == particle::GpuParticleOffscreenPolicy::PauseWhenOffscreen &&
           boundsTrace.constants[1].capacity == 1000 &&
           boundsTrace.constants[1].offscreenPolicy == particle::GpuParticleOffscreenPolicy::AlwaysSimulate &&
           boundsTrace.constants[2].capacity == 1000 &&
           boundsTrace.constants[2].offscreenPolicy == particle::GpuParticleOffscreenPolicy::AlwaysSimulate &&
           boundsTrace.constants[2].forceSimulation == 1 && boundsTrace.constants[3].capacity == 1000 &&
           boundsTrace.constants[3].offscreenPolicy == particle::GpuParticleOffscreenPolicy::AlwaysSimulate &&
           boundsTrace.constants[3].forceSimulation == 0 && boundsTrace.constants[4].capacity == 1000 &&
           boundsTrace.constants[4].offscreenPolicy == particle::GpuParticleOffscreenPolicy::PauseWhenOffscreen &&
           boundsTrace.constants[5].capacity == 1000 &&
           boundsTrace.constants[5].boundsMode == particle::GpuParticleBoundsMode::Automatic &&
           boundsTrace.constants[6].capacity == 1000 &&
           boundsTrace.constants[6].boundsMode == particle::GpuParticleBoundsMode::Manual &&
           std::equal(manualLower.begin(), manualLower.end(), boundsTrace.constants[6].manualLower.begin()) &&
           std::equal(manualUpper.begin(), manualUpper.end(), boundsTrace.constants[6].manualUpper.begin()) &&
           boundsTrace.constants[7].capacity == 1000);
    bounds.Destroy();
    assert(!bounds.IsValid() && boundsDevice.bufferReleases == 2 && boundsDevice.groupReleases == 1 &&
           boundsDevice.layoutReleases == 1 && boundsDevice.pipelineReleases == 3);

    FakeDevice cullDevice;
    std::array<std::array<uint32_t, 5>, 3> cullWords{};
    for (auto &shader : cullWords)
        shader[0] = 0x07230203u;
    particle::GpuParticleCullerDesc cullerDesc;
    cullerDesc.capacity = runtime.Capacity();
    cullerDesc.vertexCount = 6;
    cullerDesc.instances = runtime.InstanceBuffer();
    cullerDesc.sourceIndirectArguments = runtime.IndirectBuffer();
    cullerDesc.sourceIndices = runtime.RenderIndexBuffer();
    cullerDesc.bounds = {800, 1};
    cullerDesc.simulationControl = runtime.SimulationControlBuffer();
    cullerDesc.program = {
        {cullWords[0].data(), cullWords[0].size()},
        {cullWords[1].data(), cullWords[1].size()},
        {cullWords[2].data(), cullWords[2].size()},
    };
    particle::GpuParticleCullProgramStorage cullProgramStorage;
    assert(cullProgramStorage.Assign(cullerDesc.program) && cullProgramStorage.IsValid());
    assert(cullProgramStorage.View().reset.words != cullerDesc.program.reset.words &&
           cullProgramStorage.View().reset.wordCount == cullerDesc.program.reset.wordCount);
    const std::string_view cullSource = particle::GpuParticleCullShaderSources::Cull();
    const std::string_view cullResetSource = particle::GpuParticleCullShaderSources::Reset();
    assert(cullSource.find("shared uint local_visible_count;") != std::string_view::npos &&
           cullSource.find("shared uint global_base;") != std::string_view::npos &&
           cullSource.find("atomicAdd(local_visible_count, 1u)") != std::string_view::npos &&
           cullSource.find("atomicAdd(draw_instance_count, local_visible_count)") != std::string_view::npos &&
           cullSource.find("atomicAdd(draw_instance_count, 1u)") == std::string_view::npos &&
           cullSource.find("barrier();") != std::string_view::npos &&
           cullSource.find("source_index >= source_count) return") == std::string_view::npos &&
           cullSource.find("ribbon_segments ? source_index : source_indices[source_index]") == std::string_view::npos &&
           cullSource.find("output_value = source_index") != std::string_view::npos &&
           cullSource.find("output_value = particle_index") != std::string_view::npos &&
           cullSource.find("bounds_fully_inside") != std::string_view::npos &&
           cullSource.find("draw_instance_count & 0x80000000u") != std::string_view::npos &&
           cullResetSource.find("bounds_fully_inside") != std::string_view::npos &&
           cullResetSource.find("draw_instance_count = 0x80000000u") != std::string_view::npos &&
           particle::GpuParticleCullShaderSources::Finalize().find("draw_instance_count & 0x7fffffffu") !=
               std::string_view::npos);
    particle::ParticleGpuCuller sceneCuller;
    particle::ParticleGpuCuller gameCuller;
    assert(sceneCuller.Create(cullDevice, cullerDesc));
    assert(gameCuller.Create(cullDevice, cullerDesc));
    assert(sceneCuller.IsValid() && gameCuller.IsValid() && sceneCuller.Capacity() == 1000 &&
           sceneCuller.InstanceBuffer() == runtime.InstanceBuffer() &&
           sceneCuller.SourceIndirectBuffer() == runtime.IndirectBuffer() &&
           sceneCuller.SourceIndexBuffer() == runtime.RenderIndexBuffer() &&
           sceneCuller.Mode() == particle::GpuParticleCullMode::Instances &&
           sceneCuller.BoundsBuffer() == cullerDesc.bounds);
    assert(sceneCuller.VisibleIndexBuffer() != gameCuller.VisibleIndexBuffer() &&
           sceneCuller.DrawIndirectBuffer() != gameCuller.DrawIndirectBuffer() &&
           sceneCuller.SortDispatchBuffer() != gameCuller.SortDispatchBuffer());
    assert(cullDevice.buffers.size() == 6 && cullDevice.buffers[0].byteSize == 4000 &&
           cullDevice.buffers[0].usage == rhi::BufferUsageFlags::Storage && cullDevice.buffers[1].byteSize == 16 &&
           rhi::HasBufferUsage(cullDevice.buffers[1].usage, rhi::BufferUsageFlags::Indirect) &&
           cullDevice.buffers[2].byteSize == sizeof(particle::GpuParticleCullDispatchState) &&
           rhi::HasBufferUsage(cullDevice.buffers[2].usage, rhi::BufferUsageFlags::Indirect) &&
           rhi::HasBufferUsage(cullDevice.buffers[2].usage, rhi::BufferUsageFlags::TransferSource));
    assert(cullDevice.layouts.size() == 2 && cullDevice.layouts[0].entryCount == 8 &&
           cullDevice.bindGroups.size() == 2 && cullDevice.bindGroups[0].bufferCount == 8 &&
           cullDevice.shaderCreates == 6 && cullDevice.shaderReleases == 6 && cullDevice.pipelineCreates == 6);

    CullTrace cullTrace;
    const rhi::ComputeCommandEncoder::DispatchTable cullDispatch = {&CullTrace::BindPipeline, &CullTrace::BindGroup,
                                                                    &CullTrace::PushConstants, &CullTrace::Dispatch,
                                                                    &CullTrace::DispatchIndirect};
    const rhi::ComputeCommandEncoder cullEncoder(&cullTrace, &cullDispatch);
    std::array<float, particle::ParticleGpuCuller::PlaneCount * 4> frustumPlanes{};
    for (size_t index = 0; index < frustumPlanes.size(); ++index)
        frustumPlanes[index] = static_cast<float>(index + 1);
    sceneCuller.RecordReset(cullEncoder, frustumPlanes);
    sceneCuller.RecordCull(cullEncoder, frustumPlanes);
    sceneCuller.RecordFinalize(cullEncoder);
    assert(cullTrace.pipelines.size() == 3 && cullTrace.groups.size() == 3 && cullTrace.constants.size() == 3);
    assert(cullTrace.dispatches == std::vector<uint32_t>({1, 1}));
    assert(cullTrace.indirectDispatches == std::vector<rhi::BufferHandle>({sceneCuller.SortDispatchBuffer()}));
    assert(cullTrace.constants[0].capacity == 1000 && cullTrace.constants[0].frustumPlanes == frustumPlanes &&
           cullTrace.constants[1].capacity == 1000 && cullTrace.constants[1].frustumPlanes == frustumPlanes &&
           cullTrace.constants[2].capacity == 1000 &&
           cullTrace.constants[0].mode == particle::GpuParticleCullMode::Instances);
    sceneCuller.Destroy();
    gameCuller.Destroy();
    assert(!sceneCuller.IsValid() && !gameCuller.IsValid() && cullDevice.bufferReleases == 6 &&
           cullDevice.groupReleases == 2 && cullDevice.layoutReleases == 2 && cullDevice.pipelineReleases == 6);

    FakeDevice sortDevice;
    std::array<std::array<uint32_t, 5>, 5> sortWords{};
    for (auto &shader : sortWords)
        shader[0] = 0x07230203u;
    particle::GpuParticleSorterDesc sorterDesc;
    sorterDesc.capacity = runtime.Capacity();
    sorterDesc.instances = runtime.InstanceBuffer();
    sorterDesc.indirectArguments = runtime.IndirectBuffer();
    sorterDesc.sourceIndices = runtime.RenderIndexBuffer();
    sorterDesc.dispatchArguments = {700, 1};
    sorterDesc.program = {
        {sortWords[0].data(), sortWords[0].size()}, {sortWords[1].data(), sortWords[1].size()},
        {sortWords[2].data(), sortWords[2].size()}, {sortWords[3].data(), sortWords[3].size()},
        {sortWords[4].data(), sortWords[4].size()},
    };
    particle::GpuParticleSortProgramStorage sortProgramStorage;
    assert(sortProgramStorage.Assign(sorterDesc.program) && sortProgramStorage.IsValid());
    assert(sortProgramStorage.View().generate.words != sorterDesc.program.generate.words &&
           sortProgramStorage.View().generate.wordCount == sorterDesc.program.generate.wordCount);
    particle::ParticleGpuSorter sorter;
    assert(sorter.Create(sortDevice, sorterDesc));
    particle::ParticleGpuSorter gameViewSorter;
    assert(gameViewSorter.Create(sortDevice, sorterDesc));
    const rhi::BufferHandle expectedSortDispatch{700, 1};
    assert(sorter.IsValid() && sorter.Capacity() == 1000 && sorter.BlockCount() == 4 &&
           sorter.SourceIndexBuffer() == runtime.RenderIndexBuffer() &&
           sorter.DispatchBuffer() == expectedSortDispatch && sorter.SortedIndices() == sorter.IndexBuffer(0) &&
           gameViewSorter.IsValid() && gameViewSorter.SortedIndices() != sorter.SortedIndices());
    assert(sortDevice.buffers.size() == 14 && sortDevice.buffers[0].byteSize == 8000 &&
           sortDevice.buffers[4].byteSize == 256 && sortDevice.buffers[5].byteSize == 256 &&
           sortDevice.buffers[6].byteSize == 64);
    assert(sortDevice.layouts.size() == 2 && sortDevice.layouts[0].entryCount == 11 &&
           sortDevice.bindGroups.size() == 4 && sortDevice.bindGroups[0].bufferCount == 11 &&
           sortDevice.bindGroups[1].bufferCount == 11);
    assert(sortDevice.shaderCreates == 10 && sortDevice.shaderReleases == 10 && sortDevice.pipelineCreates == 10);

    SortTrace sortTrace;
    const rhi::ComputeCommandEncoder::DispatchTable sortDispatch = {&SortTrace::BindPipeline, &SortTrace::BindGroup,
                                                                    &SortTrace::PushConstants, &SortTrace::Dispatch,
                                                                    &SortTrace::DispatchIndirect};
    const rhi::ComputeCommandEncoder sortEncoder(&sortTrace, &sortDispatch);
    std::array<float, 16> sortView{};
    sortView[0] = sortView[5] = sortView[10] = sortView[15] = 1.0f;
    // Infernux uses GLM's left-handed view convention: visible depth grows
    // along +Z. Back-to-front therefore reverses the ordered float key so the
    // radix/odd-even sort emits farther particles before nearer particles.
    sorter.RecordGenerate(sortEncoder, sortView, particle::ParticleSortMode::BackToFront);
    sorter.RecordHistogram(sortEncoder, 0);
    sorter.RecordScan(sortEncoder, 0);
    sorter.RecordScatter(sortEncoder, 0);
    sorter.RecordHistogram(sortEncoder, 1);
    assert(sortTrace.dispatches == std::vector<uint32_t>({1}));
    assert(sortTrace.indirectDispatches ==
           std::vector<rhi::BufferHandle>({sorterDesc.dispatchArguments, sorterDesc.dispatchArguments,
                                           sorterDesc.dispatchArguments, sorterDesc.dispatchArguments}));
    assert(sortTrace.constants.size() == 5 && sortTrace.constants[0].view == sortView &&
           sortTrace.constants[0].descending == 1 && sortTrace.constants[0].capacity == 1000 &&
           sortTrace.constants[0].blockCount == 4 && sortTrace.constants[1].digitShift == 0 &&
           sortTrace.constants[4].digitShift == 4);
    assert(sortTrace.groups[0] == sortTrace.groups[1] && sortTrace.groups[1] == sortTrace.groups[2] &&
           sortTrace.groups[2] == sortTrace.groups[3] && sortTrace.groups[4] != sortTrace.groups[3]);

    SortTrace gameSortTrace;
    const rhi::ComputeCommandEncoder gameSortEncoder(&gameSortTrace, &sortDispatch);
    auto gameSortView = sortView;
    gameSortView[12] = 12.0f;
    gameViewSorter.RecordGenerate(gameSortEncoder, gameSortView, particle::ParticleSortMode::FrontToBack);
    assert(gameSortTrace.constants.size() == 1 && gameSortTrace.constants[0].view == gameSortView &&
           gameSortTrace.constants[0].descending == 0 && gameSortTrace.groups[0] != sortTrace.groups[0] &&
           gameSortTrace.indirectDispatches == std::vector<rhi::BufferHandle>({sorterDesc.dispatchArguments}));
    sorter.Destroy();
    gameViewSorter.Destroy();
    assert(!sorter.IsValid() && !gameViewSorter.IsValid() && sortDevice.bufferReleases == 14 &&
           sortDevice.groupReleases == 4 && sortDevice.layoutReleases == 2 && sortDevice.pipelineReleases == 10);

    static_assert(particle::ParticleGpuSorter::SmallSortCapacity == 1024);
    FakeDevice smallSortDevice;
    auto smallSorterDesc = sorterDesc;
    smallSorterDesc.capacity = particle::ParticleGpuSorter::SmallSortCapacity;
    particle::ParticleGpuSorter smallSorter;
    assert(smallSorter.Create(smallSortDevice, smallSorterDesc));
    assert(smallSorter.IsValid() && smallSorter.UsesSmallSort() && smallSorter.BlockCount() == 4);
    SortTrace smallSortTrace;
    const rhi::ComputeCommandEncoder smallSortEncoder(&smallSortTrace, &sortDispatch);
    smallSorter.RecordSmall(smallSortEncoder, sortView, particle::ParticleSortMode::BackToFront);
    assert(smallSortTrace.dispatches == std::vector<uint32_t>({1}) && smallSortTrace.indirectDispatches.empty());
    assert(smallSortTrace.constants.size() == 1 && smallSortTrace.constants[0].view == sortView &&
           smallSortTrace.constants[0].descending == 1 &&
           smallSortTrace.constants[0].capacity == particle::ParticleGpuSorter::SmallSortCapacity &&
           smallSortTrace.constants[0].blockCount == 4);
    smallSorter.Destroy();
    assert(!smallSorter.IsValid() && smallSortDevice.bufferReleases == 7 && smallSortDevice.groupReleases == 2 &&
           smallSortDevice.layoutReleases == 1 && smallSortDevice.pipelineReleases == 5);

    FakeDevice radixThresholdDevice;
    auto radixThresholdDesc = sorterDesc;
    radixThresholdDesc.capacity = particle::ParticleGpuSorter::SmallSortCapacity + 1u;
    particle::ParticleGpuSorter radixThresholdSorter;
    assert(radixThresholdSorter.Create(radixThresholdDevice, radixThresholdDesc));
    assert(radixThresholdSorter.IsValid() && !radixThresholdSorter.UsesSmallSort() &&
           radixThresholdSorter.BlockCount() == 5);
    radixThresholdSorter.Destroy();

    FakeDevice ribbonDevice;
    std::array<std::array<uint32_t, 5>, 5> ribbonTopologyWords{};
    for (auto &shader : ribbonTopologyWords)
        shader[0] = 0x07230203u;
    particle::GpuParticleRibbonDesc ribbonDesc;
    ribbonDesc.capacity = runtime.Capacity();
    ribbonDesc.instances = runtime.InstanceBuffer();
    ribbonDesc.sourceIndices = runtime.RenderIndexBuffer();
    ribbonDesc.sourceIndirectArguments = runtime.IndirectBuffer();
    ribbonDesc.simulationControl = runtime.SimulationControlBuffer();
    ribbonDesc.program = {
        {ribbonTopologyWords[0].data(), ribbonTopologyWords[0].size()},
        {ribbonTopologyWords[1].data(), ribbonTopologyWords[1].size()},
        {ribbonTopologyWords[2].data(), ribbonTopologyWords[2].size()},
        {ribbonTopologyWords[3].data(), ribbonTopologyWords[3].size()},
        {ribbonTopologyWords[4].data(), ribbonTopologyWords[4].size()},
    };
    particle::GpuParticleRibbonProgramStorage ribbonTopologyStorage;
    assert(ribbonTopologyStorage.Assign(ribbonDesc.program) && ribbonTopologyStorage.IsValid());
    auto ribbonTopology = std::make_shared<particle::ParticleGpuRibbonTopology>();
    assert(ribbonTopology->Create(ribbonDevice, ribbonDesc));
    assert(ribbonTopology->IsValid() && ribbonTopology->Capacity() == 1000 && ribbonTopology->BlockCount() == 4 &&
           ribbonTopology->InstanceBuffer() == runtime.InstanceBuffer() &&
           ribbonTopology->SourceIndexBuffer() == runtime.RenderIndexBuffer() &&
           ribbonTopology->SourceIndirectBuffer() == runtime.IndirectBuffer() &&
           ribbonTopology->SortedIndexBuffer() == ribbonTopology->IndexBuffer(0));
    assert(ribbonDevice.buffers.size() == 7 && ribbonDevice.buffers[0].byteSize == 4000 &&
           ribbonDevice.buffers[1].byteSize == 4000 && ribbonDevice.buffers[2].byteSize == 16 &&
           rhi::HasBufferUsage(ribbonDevice.buffers[2].usage, rhi::BufferUsageFlags::Indirect) &&
           ribbonDevice.buffers[3].byteSize == 12 &&
           rhi::HasBufferUsage(ribbonDevice.buffers[3].usage, rhi::BufferUsageFlags::Indirect) &&
           ribbonDevice.buffers[4].byteSize == 256 && ribbonDevice.buffers[5].byteSize == 256 &&
           ribbonDevice.buffers[6].byteSize == 64);
    assert(ribbonDevice.layouts.size() == 1 && ribbonDevice.layouts[0].entryCount == 11 &&
           ribbonDevice.bindGroups.size() == 2 && ribbonDevice.bindGroups[0].bufferCount == 11 &&
           ribbonDevice.shaderCreates == 5 && ribbonDevice.shaderReleases == 5 && ribbonDevice.pipelineCreates == 5);

    particle::GpuParticleCullerDesc ribbonCullerDesc;
    ribbonCullerDesc.capacity = runtime.Capacity();
    ribbonCullerDesc.vertexCount = 6;
    ribbonCullerDesc.instances = runtime.InstanceBuffer();
    ribbonCullerDesc.sourceIndirectArguments = ribbonTopology->DrawIndirectBuffer();
    ribbonCullerDesc.sourceIndices = ribbonTopology->SortedIndexBuffer();
    ribbonCullerDesc.bounds = {801, 1};
    ribbonCullerDesc.simulationControl = runtime.SimulationControlBuffer();
    ribbonCullerDesc.mode = particle::GpuParticleCullMode::RibbonSegments;
    ribbonCullerDesc.program = cullProgramStorage.View();
    particle::ParticleGpuCuller ribbonCuller;
    assert(ribbonCuller.Create(ribbonDevice, ribbonCullerDesc));
    assert(ribbonCuller.IsValid() && ribbonCuller.Mode() == particle::GpuParticleCullMode::RibbonSegments &&
           ribbonCuller.SourceIndexBuffer() == ribbonTopology->SortedIndexBuffer() &&
           ribbonCuller.SourceIndirectBuffer() == ribbonTopology->DrawIndirectBuffer());

    RibbonTrace ribbonTrace;
    const rhi::ComputeCommandEncoder::DispatchTable ribbonDispatch = {
        &RibbonTrace::BindPipeline, &RibbonTrace::BindGroup, &RibbonTrace::PushConstants, &RibbonTrace::Dispatch,
        &RibbonTrace::DispatchIndirect};
    const rhi::ComputeCommandEncoder ribbonEncoder(&ribbonTrace, &ribbonDispatch);
    ribbonTopology->RecordReset(ribbonEncoder);
    ribbonTopology->RecordInitialize(ribbonEncoder);
    ribbonTopology->RecordHistogram(ribbonEncoder, 0);
    ribbonTopology->RecordScan(ribbonEncoder, 0);
    ribbonTopology->RecordScatter(ribbonEncoder, 0);
    ribbonTopology->RecordHistogram(ribbonEncoder, 8);
    ribbonTopology->RecordHistogram(ribbonEncoder, 16);
    ribbonTopology->RecordHistogram(ribbonEncoder, 23);
    assert(ribbonTrace.dispatches == std::vector<uint32_t>({1, 1}));
    assert(ribbonTrace.indirectDispatches.size() == 6 &&
           std::all_of(ribbonTrace.indirectDispatches.begin(), ribbonTrace.indirectDispatches.end(),
                       [&](auto buffer) { return buffer == ribbonTopology->DispatchBuffer(); }));
    assert(ribbonTrace.constants.size() == 8 && ribbonTrace.constants[0].capacity == 1000 &&
           ribbonTrace.constants[0].blockCount == 4 && ribbonTrace.constants[2].keyField == 2 &&
           ribbonTrace.constants[2].digitShift == 0 && ribbonTrace.constants[5].keyField == 1 &&
           ribbonTrace.constants[6].keyField == 0 && ribbonTrace.constants[7].keyField == 0 &&
           ribbonTrace.constants[7].digitShift == 28);

    std::array<std::array<uint32_t, 5>, 4> ribbonRenderWords{};
    for (auto &shader : ribbonRenderWords)
        shader[0] = 0x07230203u;
    particle::GpuParticleRibbonRenderProgram ribbonRenderProgram = {
        {ribbonRenderWords[0].data(), ribbonRenderWords[0].size()},
        {ribbonRenderWords[1].data(), ribbonRenderWords[1].size()},
        {ribbonRenderWords[2].data(), ribbonRenderWords[2].size()},
        {ribbonRenderWords[3].data(), ribbonRenderWords[3].size()},
    };
    particle::GpuParticleRibbonRenderProgramStorage ribbonRenderStorage;
    assert(ribbonRenderStorage.Assign(ribbonRenderProgram) && ribbonRenderStorage.IsValid());
    particle::GpuRibbonRendererDesc ribbonRendererDesc;
    ribbonRendererDesc.program = ribbonRenderStorage.View();
    ribbonRendererDesc.topology = ribbonTopology;
    auto ribbonArtifact = std::make_shared<ShaderProgramArtifact>();
    ribbonArtifact->key = {{"Tests/ParticleRibbon", "Tests/ParticleUnlit"}, 1};
    ribbonArtifact->domain = ShaderProgramDomain::ParticleSprite;
    ribbonArtifact->compatibilitySignature = 1;
    ribbonArtifact->materialBufferSize = 16;
    ribbonArtifact->properties = {
        {"opacity", "Float", "1.0", "", ShaderProgramStageMask::Fragment, false, std::nullopt, 0, std::nullopt, 4, 4},
    };
    ShaderProgramArtifact::PassVariant ribbonForward;
    ribbonForward.compatibilitySignature = ribbonArtifact->compatibilitySignature;
    ribbonForward.vertexSpirv.resize(5 * sizeof(uint32_t));
    ribbonForward.fragmentSpirv.resize(5 * sizeof(uint32_t));
    const uint32_t ribbonSpirvMagic = 0x07230203u;
    std::memcpy(ribbonForward.vertexSpirv.data(), &ribbonSpirvMagic, sizeof(ribbonSpirvMagic));
    std::memcpy(ribbonForward.fragmentSpirv.data(), &ribbonSpirvMagic, sizeof(ribbonSpirvMagic));
    ribbonArtifact->variants.push_back(ribbonForward);
    auto ribbonMotion = ribbonForward;
    ribbonMotion.target = ShaderCompileTarget::Motion;
    ribbonArtifact->variants.push_back(std::move(ribbonMotion));
    assert(ribbonArtifact->IsValid());
    ribbonRendererDesc.shaderProgram = ribbonArtifact;
    ribbonRendererDesc.material = std::make_shared<InxMaterial>("ribbon-surface-parameters");
    ribbonRendererDesc.material->SetFloat("opacity", 0.625f);
    ribbonRendererDesc.textureResolver = [](const std::string &, const std::string &) {
        return particle::GpuBillboardTextureLease{};
    };
    ribbonRendererDesc.deletionQueue = &deletionQueue;
    ribbonRendererDesc.semantics.sortMode = particle::ParticleSortMode::None;
    ribbonRendererDesc.uvMode = particle::ParticleRibbonUvMode::Repeat;
    ribbonRendererDesc.uvScale = 2.5f;
    particle::ParticleGpuRibbonRenderer ribbonRenderer;
    const uint32_t ribbonWritesBeforeCreate = ribbonDevice.writes;
    assert(ribbonRenderer.Create(ribbonDevice, ribbonRendererDesc));
    assert(ribbonDevice.writes == ribbonWritesBeforeCreate + 1 && ribbonDevice.writtenBytes.back().size() == 16);
    float uploadedRibbonOpacity = 0.0f;
    std::memcpy(&uploadedRibbonOpacity, ribbonDevice.writtenBytes.back().data(), sizeof(uploadedRibbonOpacity));
    assert(uploadedRibbonOpacity == 0.625f);
    assert(ribbonRenderer.IsValid() && !ribbonRenderer.CanCastShadows() &&
           ribbonRenderer.InstanceBuffer() == runtime.InstanceBuffer() &&
           ribbonRenderer.RenderIndexBuffer() == ribbonTopology->SortedIndexBuffer());
    const auto ribbonViewIndices = ribbonCuller.VisibleIndexBuffer();

    GraphicsTrace ribbonGraphicsTrace;
    const rhi::GraphicsCommandEncoder::Dispatch ribbonGraphicsDispatch = {
        &GraphicsTrace::BindPipeline, &GraphicsTrace::BindGroup, &GraphicsTrace::PushConstants, &GraphicsTrace::Draw,
        &GraphicsTrace::DrawIndirect};
    const rhi::GraphicsCommandEncoder ribbonGraphicsEncoder(&ribbonGraphicsTrace, &ribbonGraphicsDispatch);
    particle::GpuParticleViewConstants ribbonView;
    MaterialPassPipelineDescriptor ribbonPass;
    ribbonPass.target = ShaderCompileTarget::Forward;
    ribbonPass.colorFormats = {rhi::PixelFormat::RGBA8UNorm};
    ribbonPass.depthFormat = rhi::PixelFormat::D32SFloat;
    ribbonPass.samples = rhi::SampleCount::One;
    const rhi::RenderTargetLayoutHandle ribbonTarget{910, 1};
    const particle::GpuParticlePerViewBindings ribbonPerView{{911, 1}, {912, 1}};
    assert(ribbonRenderer.RecordDraw(ribbonGraphicsEncoder, ribbonTarget, ribbonPass,
                                     ribbonTopology->DrawIndirectBuffer(), ribbonView, {}, {}, true, ribbonPerView));
    assert(ribbonGraphicsTrace.groupSets == std::vector<uint32_t>({0, 1, 2}));
    assert(ribbonGraphicsTrace.indirectBuffers ==
           std::vector<rhi::BufferHandle>({ribbonTopology->DrawIndirectBuffer()}));
    assert(ribbonGraphicsTrace.constants.size() == 1 &&
           ribbonGraphicsTrace.constants[0].alignmentReference[0] == 2.5f &&
           ribbonGraphicsTrace.constants[0].alignmentReference[1] == 1.0f &&
           ribbonGraphicsTrace.constants[0].alignmentReference[3] == -1.0f);
    assert(ribbonRenderer.RecordDraw(ribbonGraphicsEncoder, ribbonTarget, ribbonPass,
                                     ribbonTopology->DrawIndirectBuffer(), ribbonView, ribbonViewIndices, {}, true,
                                     ribbonPerView));
    assert(ribbonGraphicsTrace.groupSets == std::vector<uint32_t>({0, 1, 2, 0, 1, 2}));
    assert(ribbonGraphicsTrace.constants.back().alignmentReference[2] == 1.0f);
    ribbonPass.target = ShaderCompileTarget::Picking;
    ribbonPass.colorFormats = {rhi::PixelFormat::RG32UInt};
    assert(ribbonRenderer.RecordPickingDraw(ribbonGraphicsEncoder, ribbonTarget, ribbonPass,
                                            ribbonTopology->DrawIndirectBuffer(), ribbonView, 0x123456789abcdef0ull));
    ribbonPass.target = ShaderCompileTarget::Motion;
    ribbonPass.colorFormats = {rhi::PixelFormat::RG16SFloat};
    assert(ribbonRenderer.RecordDraw(ribbonGraphicsEncoder, ribbonTarget, ribbonPass,
                                     ribbonTopology->DrawIndirectBuffer(), ribbonView));
    const auto floatBits = [](float value) {
        uint32_t bits = 0;
        std::memcpy(&bits, &value, sizeof(bits));
        return bits;
    };
    assert(ribbonGraphicsTrace.constants.size() == 4 &&
           floatBits(ribbonGraphicsTrace.constants[2].materialTint[0]) == 0x9abcdef0u &&
           floatBits(ribbonGraphicsTrace.constants[2].materialTint[1]) == 0x12345678u &&
           ribbonGraphicsTrace.constants[2].materialTint[3] == 1.0f);
    ribbonRenderer.Destroy();
    ribbonCuller.Destroy();
    ribbonTopology->Destroy();
    assert(!ribbonRenderer.IsValid() && !ribbonTopology->IsValid());

    const auto instanceBuffer = runtime.InstanceBuffer();
    const auto renderIndexBuffer = runtime.RenderIndexBuffer();
    const auto indirectBuffer = runtime.IndirectBuffer();
    const uint32_t textureReleasesBeforeBillboard = device.textureReleases;
    const uint32_t samplerReleasesBeforeBillboard = device.samplerReleases;
    const size_t layoutsBeforeBillboard = device.layoutEntryCounts.size();
    const size_t groupsBeforeBillboard = device.groupBufferCounts.size();
    const uint32_t groupCreatesBeforeBillboard = device.groupCreates;
    const uint32_t groupReleasesBeforeBillboard = device.groupReleases;
    std::array<uint32_t, 4> billboardVertex = {0x07230203};
    std::array<uint32_t, 4> billboardFragment = {0x07230203};
    auto billboardArtifact = std::make_shared<ShaderProgramArtifact>();
    billboardArtifact->key = {{"Tests/ParticleSprite", "Tests/ParticleUnlit"}, 1};
    billboardArtifact->domain = ShaderProgramDomain::ParticleSprite;
    billboardArtifact->usesParticleSceneDepthBinding = true;
    billboardArtifact->compatibilitySignature = 1;
    billboardArtifact->properties = {
        {"texSampler", "Texture2D", "", "white", ShaderProgramStageMask::Fragment, false, std::nullopt, std::nullopt, 0,
         0, 0},
    };
    ShaderProgramArtifact::PassVariant billboardForward;
    billboardForward.compatibilitySignature = billboardArtifact->compatibilitySignature;
    billboardForward.vertexSpirv.resize(5 * sizeof(uint32_t));
    billboardForward.fragmentSpirv.resize(5 * sizeof(uint32_t));
    const uint32_t billboardSpirvMagic = 0x07230203u;
    std::memcpy(billboardForward.vertexSpirv.data(), &billboardSpirvMagic, sizeof(billboardSpirvMagic));
    std::memcpy(billboardForward.fragmentSpirv.data(), &billboardSpirvMagic, sizeof(billboardSpirvMagic));
    billboardArtifact->variants.push_back(billboardForward);
    auto billboardMotion = billboardForward;
    billboardMotion.target = ShaderCompileTarget::Motion;
    billboardArtifact->variants.push_back(std::move(billboardMotion));
    assert(billboardArtifact->IsValid());
    particle::GpuBillboardRendererDesc billboardDesc;
    billboardDesc.shaderProgram = billboardArtifact;
    billboardDesc.vertexShader = {billboardVertex.data(), billboardVertex.size()};
    billboardDesc.motionVertexShader = {billboardVertex.data(), billboardVertex.size()};
    billboardDesc.motionFragmentShader = {billboardFragment.data(), billboardFragment.size()};
    billboardDesc.instances = instanceBuffer;
    billboardDesc.renderIndices = renderIndexBuffer;
    billboardDesc.flipbookColumns = 4;
    billboardDesc.flipbookRows = 2;
    billboardDesc.semantics.spriteAlignment = particle::ParticleSpriteAlignment::Axis;
    billboardDesc.semantics.alignmentAxis = {0.0f, 0.0f, 1.0f};
    billboardDesc.fallbackMaterial.renderQueue = 3100;
    billboardDesc.material = std::make_shared<InxMaterial>("live-particle-material");
    auto liveMaterialState = billboardDesc.material->GetRenderState();
    liveMaterialState.renderQueue = 3100;
    liveMaterialState.blendEnable = true;
    liveMaterialState.depthWriteEnable = false;
    billboardDesc.material->SetRenderState(liveMaterialState);
    billboardDesc.material->SetTextureGuid("texSampler", "white");
    uint32_t textureResolveCount = 0;
    bool normalTextureReady = false;
    TestTextureSlots textureSlots;
    billboardDesc.textureResolver = [&device, &textureResolveCount, &normalTextureReady,
                                     &textureSlots](const std::string &textureGuid, const std::string &name) {
        assert(name == "texSampler");
        ++textureResolveCount;
        if (textureGuid == "normal" && !normalTextureReady)
            return particle::GpuBillboardTextureLease{particle::GpuBillboardTextureStatus::Pending};
        const uint32_t identity = textureGuid == "normal" ? 2u : 1u;
        return AcquireTestTexture(device, textureSlots, textureGuid, identity, {400u + identity, 1},
                                  {500u + identity, 1});
    };
    billboardDesc.deletionQueue = &deletionQueue;

    particle::ParticleGpuBillboardRenderer billboard;
    assert(billboard.Create(device, billboardDesc));
    assert(billboard.IsValid() && billboard.RenderQueue() == 3100 && billboard.InstanceBuffer() == instanceBuffer);
    const std::array<uint32_t, 3> billboardLayoutEntryCounts = {2, 0, 2};
    const std::array<uint32_t, 3> billboardGroupBufferCounts = {0, 0, 2};
    const std::array<uint32_t, 3> billboardGroupTextureCounts = {0, 2, 0};
    assert(device.layoutEntryCounts.size() == layoutsBeforeBillboard + billboardLayoutEntryCounts.size() &&
           std::equal(billboardLayoutEntryCounts.begin(), billboardLayoutEntryCounts.end(),
                      device.layoutEntryCounts.begin() + layoutsBeforeBillboard));
    assert(device.groupBufferCounts.size() == groupsBeforeBillboard + billboardGroupBufferCounts.size() &&
           device.groupTextureCounts.size() == groupsBeforeBillboard + billboardGroupTextureCounts.size() &&
           std::equal(billboardGroupBufferCounts.begin(), billboardGroupBufferCounts.end(),
                      device.groupBufferCounts.begin() + groupsBeforeBillboard) &&
           std::equal(billboardGroupTextureCounts.begin(), billboardGroupTextureCounts.end(),
                      device.groupTextureCounts.begin() + groupsBeforeBillboard));
    const auto &billboardSurfaceGroup = device.bindGroups[device.bindGroups.size() - 2];
    assert(billboardSurfaceGroup.textures[0].binding == 2 && billboardSurfaceGroup.textures[1].binding == 15 &&
           !billboardSurfaceGroup.textures[1].depthRead);
    assert(textureResolveCount == 1);

    MaterialPassPipelineDescriptor forwardPass;
    forwardPass.colorFormats = {rhi::PixelFormat::RGBA16SFloat};
    forwardPass.depthFormat = rhi::PixelFormat::D32SFloat;
    forwardPass.samples = rhi::SampleCount::Four;
    GraphicsTrace graphicsTrace;
    const rhi::GraphicsCommandEncoder::Dispatch graphicsDispatch = {
        &GraphicsTrace::BindPipeline, &GraphicsTrace::BindGroup, &GraphicsTrace::PushConstants, &GraphicsTrace::Draw,
        &GraphicsTrace::DrawIndirect};
    const rhi::GraphicsCommandEncoder graphicsEncoder(&graphicsTrace, &graphicsDispatch);
    particle::GpuBillboardViewConstants view;
    view.cameraRight[0] = 1.0f;
    view.cameraUp[1] = 1.0f;
    const rhi::RenderTargetLayoutHandle firstTarget{100, 1};
    const particle::GpuParticlePerViewBindings perView{{800, 1}, {801, 1}};
    assert(
        billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {}, true, perView));
    assert(
        billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {}, true, perView));
    assert(device.graphicsPipelineCreates == 1 && device.graphicsPipelineDescs.size() == 1);
    const auto &graphicsDesc = device.graphicsPipelineDescs.front();
    assert(graphicsDesc.pushConstantBytes == sizeof(view));
    assert(graphicsDesc.pushConstantStages == (rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment));
    assert(graphicsDesc.bindingLayoutCount == 3 && graphicsDesc.bindingLayouts[1] == perView.layout);
    assert(graphicsDesc.samples == rhi::SampleCount::Four);
    assert(graphicsDesc.depth.testEnabled && !graphicsDesc.depth.writeEnabled);
    assert(graphicsDesc.colorTargetCount == 1 && graphicsDesc.colorTargets[0].blendEnabled &&
           !graphicsDesc.colorTargets[0].premultipliedAlpha);
    assert(graphicsTrace.pipelines.size() == 2 && graphicsTrace.groups.size() == 6 &&
           graphicsTrace.constants.size() == 2 && graphicsTrace.indirectBuffers.size() == 2);
    assert(graphicsTrace.constants.back().renderingControl[2] == 4.0f &&
           graphicsTrace.constants.back().renderingControl[3] == 2.0f);
    assert((graphicsTrace.constants.back().alignmentReference == std::array<float, 4>{0.0f, 0.0f, 1.0f, 2.0f}));

    billboardDesc.material->SetRenderQueue(3150);
    billboardDesc.material->SetColor("baseColor", glm::vec4(0.25f, 0.5f, 0.75f, 0.8f));
    assert(billboard.RenderQueue() == 3150);
    assert(
        billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {}, true, perView));
    assert(device.graphicsPipelineCreates == 1 && device.graphicsPipelineReleases == 0);
    assert(textureResolveCount == 1 && device.groupCreates == groupCreatesBeforeBillboard + 3u);
    assert(graphicsTrace.constants.back().materialTint == (std::array<float, 4>{1.0f, 1.0f, 1.0f, 1.0f}));
    assert(textureResolveCount == 1 && device.groupCreates == groupCreatesBeforeBillboard + 3u);
    billboardDesc.material->SetTextureGuid("texSampler", "normal");
    assert(
        billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {}, true, perView));
    assert(textureResolveCount == 3 && device.groupCreates == groupCreatesBeforeBillboard + 4u &&
           device.groupReleases == groupReleasesBeforeBillboard);
    assert(device.textureReleases == textureReleasesBeforeBillboard &&
           device.samplerReleases == samplerReleasesBeforeBillboard);
    assert(
        billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {}, true, perView));
    assert(textureResolveCount == 4 && device.groupCreates == groupCreatesBeforeBillboard + 4u);
    normalTextureReady = true;
    assert(
        billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {}, true, perView));
    assert(textureResolveCount == 5 && device.groupCreates == groupCreatesBeforeBillboard + 5u &&
           device.groupReleases == groupReleasesBeforeBillboard);
    (void)deletionQueue.Collect(1);
    assert(device.groupReleases == groupReleasesBeforeBillboard + 2u &&
           device.textureReleases == textureReleasesBeforeBillboard &&
           device.samplerReleases == samplerReleasesBeforeBillboard);

    auto normalSlot = textureSlots.at("normal");
    auto normalRevisionTwoOwner = normalSlot->Acquire();
    assert(normalRevisionTwoOwner && normalRevisionTwoOwner->GetRevision() == 2);
    std::weak_ptr<const rhi::TextureGpuView> normalRevisionTwo = normalRevisionTwoOwner;
    normalSlot->RequestRevision(3);
    normalTextureReady = false;
    assert(
        billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {}, true, perView));
    assert(textureResolveCount == 6 && device.groupCreates == groupCreatesBeforeBillboard + 5u &&
           !normalRevisionTwo.expired());
    auto normalRevisionThree = AcquireTestTexture(device, textureSlots, "normal", 3, {403, 1}, {503, 1});
    assert(normalRevisionThree.gpuSlot == normalSlot && normalRevisionThree.gpuView->GetRevision() == 3);
    normalRevisionTwoOwner.reset();
    assert(!normalRevisionTwo.expired());
    const uint32_t resolvesBeforeSlotPublication = textureResolveCount;
    assert(
        billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {}, true, perView));
    assert(textureResolveCount == resolvesBeforeSlotPublication &&
           device.groupCreates == groupCreatesBeforeBillboard + 6u && !normalRevisionTwo.expired());
    (void)deletionQueue.Collect(1);
    assert(device.groupReleases == groupReleasesBeforeBillboard + 3u && normalRevisionTwo.expired() &&
           device.textureReleases == textureReleasesBeforeBillboard + 1 &&
           device.samplerReleases == samplerReleasesBeforeBillboard + 1);
    normalRevisionThree = {};
    normalSlot.reset();
    liveMaterialState = billboardDesc.material->GetRenderState();
    liveMaterialState.blendEnable = false;
    liveMaterialState.depthWriteEnable = true;
    billboardDesc.material->SetRenderState(liveMaterialState);
    assert(
        billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {}, true, perView));
    assert(device.graphicsPipelineCreates == 2 && device.graphicsPipelineReleases == 0);
    const auto &updatedGraphicsDesc = device.graphicsPipelineDescs.back();
    assert(!updatedGraphicsDesc.colorTargets[0].blendEnabled && updatedGraphicsDesc.depth.writeEnabled);
    liveMaterialState.blendEnable = true;
    liveMaterialState.depthWriteEnable = false;
    liveMaterialState.srcColorBlendFactor = VK_BLEND_FACTOR_ONE;
    liveMaterialState.dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
    billboardDesc.material->SetRenderState(liveMaterialState);
    assert(
        billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {}, true, perView));
    assert(device.graphicsPipelineCreates == 3 && device.graphicsPipelineReleases == 0);
    assert(device.graphicsPipelineDescs.back().colorTargets[0].premultipliedAlpha);
    assert(graphicsTrace.constants.back().renderingControl[1] == 1.0f);

    MaterialPassPipelineDescriptor unsupportedPass = forwardPass;
    unsupportedPass.target = ShaderCompileTarget::GBuffer;
    assert(!billboard.RecordDraw(graphicsEncoder, firstTarget, unsupportedPass, indirectBuffer, view));
    billboard.Destroy();
    assert(!billboard.IsValid() && device.graphicsPipelineReleases == 3);

    {
        FakeDevice litDevice;
        auto litDesc = billboardDesc;
        auto litArtifact = std::make_shared<ShaderProgramArtifact>(*billboardArtifact);
        litArtifact->key.revision = 2;
        auto billboardForwardPlus = billboardForward;
        billboardForwardPlus.target = ShaderCompileTarget::ForwardPlus;
        litArtifact->variants.push_back(std::move(billboardForwardPlus));
        assert(litArtifact->IsValid());
        litDesc.shaderProgram = std::move(litArtifact);
        litDesc.semantics.receiveSceneLighting = true;
        litDesc.semantics.softParticles = true;
        litDesc.semantics.softDistance = 0.25f;
        litDesc.semantics.sortMode = particle::ParticleSortMode::BackToFront;
        litDesc.material->SetFloat("softness", 0.3f);
        litDesc.deletionQueue = nullptr;
        particle::ParticleGpuBillboardRenderer litBillboard;
        assert(litBillboard.Create(litDevice, litDesc));

        auto forwardPlusPass = forwardPass;
        forwardPlusPass.target = ShaderCompileTarget::ForwardPlus;
        GraphicsTrace litTrace;
        const rhi::GraphicsCommandEncoder litEncoder(&litTrace, &graphicsDispatch);
        const rhi::TextureViewHandle litSceneDepth{902, 1};
        assert(!litBillboard.RecordDraw(litEncoder, firstTarget, forwardPlusPass, indirectBuffer, view, {},
                                        litSceneDepth));
        const particle::GpuParticlePerViewBindings lighting{{900, 1}, {901, 1}};
        assert(litBillboard.RecordDraw(litEncoder, firstTarget, forwardPlusPass, indirectBuffer, view, {},
                                       litSceneDepth, true, lighting));
        assert(litDevice.graphicsPipelineDescs.size() == 1);
        const auto &litPipeline = litDevice.graphicsPipelineDescs.front();
        assert(litPipeline.bindingLayoutCount == 3 && litPipeline.bindingLayouts[1] == lighting.layout);
        assert(litTrace.groupSets == std::vector<uint32_t>({0, 1, 2}) && litTrace.groups[1] == lighting.group);
        assert(litTrace.constants.size() == 1 && litTrace.constants[0].lightingControl[0] == 1.0f &&
               litTrace.constants[0].lightingControl[1] == 1.0f && litTrace.constants[0].lightingControl[2] == 0.3f &&
               litTrace.constants[0].cameraUp[3] == 1.0f && litTrace.constants[0].cameraRight[3] == 0.25f);
        litBillboard.Destroy();
    }

    FakeDevice softDevice;
    auto softDesc = billboardDesc;
    softDesc.semantics.softParticles = true;
    softDesc.semantics.softDistance = 0.5f;
    softDesc.deletionQueue = nullptr;
    particle::ParticleGpuBillboardRenderer softBillboard;
    assert(softBillboard.Create(softDevice, softDesc) && softBillboard.RequiresSceneDepth());
    liveMaterialState = billboardDesc.material->GetRenderState();
    liveMaterialState.renderQueue = 2000;
    liveMaterialState.blendEnable = false;
    liveMaterialState.depthWriteEnable = true;
    billboardDesc.material->SetRenderState(liveMaterialState);
    assert(softBillboard.RenderQueue() == EngineConfig::Get().transparentQueueMin);
    const rhi::TextureViewHandle sceneDepthView{990, 1};
    assert(softBillboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, sceneDepthView,
                                    false, perView));
    assert(!softDevice.bindGroups.back().textures[1].depthRead);
    auto singleSamplePass = forwardPass;
    singleSamplePass.samples = rhi::SampleCount::One;
    assert(!softBillboard.RecordDraw(graphicsEncoder, firstTarget, singleSamplePass, indirectBuffer, view));
    GraphicsTrace softTrace;
    const rhi::GraphicsCommandEncoder softEncoder(&softTrace, &graphicsDispatch);
    assert(softBillboard.RecordDraw(softEncoder, firstTarget, singleSamplePass, indirectBuffer, view, {},
                                    sceneDepthView, true, perView));
    assert(softDevice.bindGroups.size() == 5 && softDevice.bindGroups.back().textureCount == 2);
    assert(softDevice.graphicsPipelineDescs.back().colorTargets[0].blendEnabled);
    assert(!softDevice.graphicsPipelineDescs.back().depth.writeEnabled);
    assert(softDevice.bindGroups.back().textures[1].binding == 15 &&
           softDevice.bindGroups.back().textures[1].texture == sceneDepthView &&
           softDevice.bindGroups.back().textures[1].depthRead);
    assert(softTrace.constants.size() == 1 && softTrace.constants[0].cameraRight[3] == 0.5f &&
           softTrace.constants[0].cameraUp[3] == 1.0f);
    softBillboard.Destroy();

    auto linkedArtifact = std::make_shared<ShaderProgramArtifact>();
    linkedArtifact->key = {{"Tests/ParticleSprite", "Tests/ParticleSurface"}, 7};
    linkedArtifact->domain = ShaderProgramDomain::ParticleSprite;
    linkedArtifact->usesParticleSceneDepthBinding = true;
    linkedArtifact->materialBufferSize = 32;
    linkedArtifact->alphaClipThresholdOffset = 20;
    linkedArtifact->compatibilitySignature = 99;
    linkedArtifact->properties = {
        {"baseColor", "Color", "[1.0, 1.0, 1.0, 1.0]", "", ShaderProgramStageMask::Fragment, false, std::nullopt, 0,
         std::nullopt, 16, 16},
        {"intensity", "Float", "2.0", "", ShaderProgramStageMask::Fragment, false, std::nullopt, 16, std::nullopt, 4,
         4},
        {"albedo", "Texture2D", "", "white", ShaderProgramStageMask::Fragment, false, std::nullopt, std::nullopt, 0, 0,
         0},
        {"detail", "Texture2D", "", "white", ShaderProgramStageMask::Vertex | ShaderProgramStageMask::Fragment, false,
         std::nullopt, std::nullopt, 1, 0, 0},
    };
    ShaderProgramArtifact::PassVariant linkedForward;
    linkedForward.compatibilitySignature = linkedArtifact->compatibilitySignature;
    linkedForward.vertexSpirv.resize(5 * sizeof(uint32_t));
    linkedForward.fragmentSpirv.resize(5 * sizeof(uint32_t));
    const uint32_t spirvMagic = 0x07230203u;
    std::memcpy(linkedForward.vertexSpirv.data(), &spirvMagic, sizeof(spirvMagic));
    std::memcpy(linkedForward.fragmentSpirv.data(), &spirvMagic, sizeof(spirvMagic));
    linkedArtifact->variants.push_back(std::move(linkedForward));
    auto linkedMotion = linkedArtifact->variants.front();
    linkedMotion.target = ShaderCompileTarget::Motion;
    linkedArtifact->variants.push_back(std::move(linkedMotion));
    assert(linkedArtifact->IsValid());

    FakeDevice linkedDevice;
    GpuRetirementQueue linkedDeletionQueue;
    linkedDeletionQueue.BindSerialSource([] { return rhi::SubmissionSerial{1}; });
    particle::GpuBillboardRendererDesc linkedDesc;
    linkedDesc.vertexShader = billboardDesc.vertexShader;
    linkedDesc.motionVertexShader = billboardDesc.motionVertexShader;
    linkedDesc.shaderProgram = linkedArtifact;
    linkedDesc.instances = instanceBuffer;
    linkedDesc.renderIndices = renderIndexBuffer;
    linkedDesc.material = std::make_shared<InxMaterial>("linked-particle-material");
    linkedDesc.material->SetColor("baseColor", glm::vec4(0.2f, 0.4f, 0.6f, 0.8f));
    linkedDesc.material->SetFloat("intensity", 3.5f);
    linkedDesc.material->SetTextureGuid("albedo", "white");
    linkedDesc.material->SetTextureGuid("detail", "normal");
    uint32_t linkedTextureResolves = 0;
    TestTextureSlots linkedTextureSlots;
    linkedDesc.textureResolver = [&linkedDevice, &linkedTextureResolves, &linkedTextureSlots](const std::string &guid,
                                                                                              const std::string &name) {
        ++linkedTextureResolves;
        const uint32_t identity = name == "albedo" ? 1u : 2u;
        assert((name == "albedo" && (guid == "white" || guid == "black")) || (name == "detail" && guid == "normal"));
        const uint64_t revision = guid == "black" ? 2u : 1u;
        return AcquireTestTexture(linkedDevice, linkedTextureSlots, guid, revision, {600u + identity, 1},
                                  {700u + identity, 1});
    };
    linkedDesc.deletionQueue = &linkedDeletionQueue;

    auto linkedLitDesc = linkedDesc;
    linkedLitDesc.semantics.receiveSceneLighting = true;
    particle::ParticleGpuBillboardRenderer linkedLitBillboard;
    assert(!linkedLitBillboard.Create(linkedDevice, linkedLitDesc));

    particle::ParticleGpuBillboardRenderer linkedBillboard;
    assert(linkedBillboard.Create(linkedDevice, linkedDesc));
    assert(linkedDevice.shaderCreates == 4 && linkedDevice.buffers.size() == 1);
    assert(linkedDevice.buffers[0].byteSize == 32 && linkedDevice.buffers[0].usage == rhi::BufferUsageFlags::Uniform &&
           linkedDevice.buffers[0].memory == rhi::BufferMemory::Upload);
    assert(linkedDevice.layouts.size() == 3 && linkedDevice.layouts[0].entryCount == 2 &&
           linkedDevice.layouts[1].entryCount == 0 && linkedDevice.layouts[2].entryCount == 4);
    assert(linkedDevice.layouts[0].entries[0].binding == 0 && linkedDevice.layouts[0].entries[1].binding == 1 &&
           linkedDevice.layouts[2].entries[0].binding == 2 && linkedDevice.layouts[2].entries[1].binding == 3 &&
           linkedDevice.layouts[2].entries[2].binding == 14 && linkedDevice.layouts[2].entries[3].binding == 15);
    assert(linkedDevice.bindGroups.size() == 3 && linkedDevice.bindGroups[1].bufferCount == 1 &&
           linkedDevice.bindGroups[1].textureCount == 3 && linkedDevice.bindGroups[2].bufferCount == 2);
    assert(linkedDevice.bindGroups[1].buffers[0].binding == 14 && linkedDevice.bindGroups[2].buffers[0].binding == 0 &&
           linkedDevice.bindGroups[2].buffers[1].binding == 1 &&
           linkedDevice.bindGroups[2].buffers[1].buffer == renderIndexBuffer);
    assert(linkedDevice.bindGroups[1].textures[0].binding == 2 && linkedDevice.bindGroups[1].textures[1].binding == 3 &&
           linkedDevice.bindGroups[1].textures[2].binding == 15 && !linkedDevice.bindGroups[1].textures[2].depthRead);
    assert(linkedTextureResolves == 2 && linkedDevice.writes == 1 && linkedDevice.writtenBytes[0].size() == 32);
    glm::vec4 packedColor{};
    float packedIntensity = 0.0f;
    float packedAlphaClipThreshold = -1.0f;
    std::memcpy(&packedColor, linkedDevice.writtenBytes[0].data(), sizeof(packedColor));
    std::memcpy(&packedIntensity, linkedDevice.writtenBytes[0].data() + 16, sizeof(packedIntensity));
    std::memcpy(&packedAlphaClipThreshold, linkedDevice.writtenBytes[0].data() + 20, sizeof(packedAlphaClipThreshold));
    assert(packedColor == inx::color::SrgbToLinear(glm::vec4(0.2f, 0.4f, 0.6f, 0.8f)) && packedIntensity == 3.5f &&
           packedAlphaClipThreshold == 0.0f);

    GraphicsTrace linkedGraphicsTrace;
    const rhi::GraphicsCommandEncoder linkedGraphicsEncoder(&linkedGraphicsTrace, &graphicsDispatch);
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {},
                                      true, perView));
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {},
                                      true, perView));
    assert(linkedDevice.writes == 1 && linkedDevice.groupCreates == 3 && linkedDevice.graphicsPipelineCreates == 1 &&
           linkedTextureResolves == 2);
    assert(linkedGraphicsTrace.constants.back().materialTint == (std::array<float, 4>{1.0f, 1.0f, 1.0f, 1.0f}));

    const rhi::BufferHandle sceneViewIndices{901, 1};
    const rhi::BufferHandle gameViewIndices{902, 1};
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view,
                                      sceneViewIndices, {}, true, perView));
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view,
                                      sceneViewIndices, {}, true, perView));
    assert(linkedDevice.groupCreates == 4 && linkedDevice.bindGroups.back().buffers[1].buffer == sceneViewIndices);
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view,
                                      gameViewIndices, {}, true, perView));
    assert(linkedDevice.groupCreates == 5 && linkedDevice.bindGroups.back().buffers[1].buffer == gameViewIndices);

    linkedDesc.material->SetFloat("intensity", 8.0f);
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {},
                                      true, perView));
    assert(linkedDevice.writes == 2 && linkedDevice.groupCreates == 5 && linkedDevice.graphicsPipelineCreates == 1 &&
           linkedTextureResolves == 2);
    std::memcpy(&packedIntensity, linkedDevice.writtenBytes.back().data() + 16, sizeof(packedIntensity));
    assert(packedIntensity == 8.0f);

    linkedDesc.material->SetTextureGuid("albedo", "black");
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {},
                                      true, perView));
    assert(linkedDevice.writes == 3 && linkedDevice.groupCreates == 6 && linkedDevice.graphicsPipelineCreates == 1 &&
           linkedTextureResolves == 3);
    (void)linkedDeletionQueue.Collect(1);
    assert(linkedDevice.groupReleases == 1 && linkedDevice.textureReleases == 0 && linkedDevice.samplerReleases == 0);

    auto linkedMaterialState = linkedDesc.material->GetRenderState();
    linkedMaterialState.alphaClipEnabled = true;
    linkedMaterialState.alphaClipThreshold = 0.35f;
    linkedDesc.material->SetRenderState(linkedMaterialState);
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {},
                                      true, perView));
    std::memcpy(&packedAlphaClipThreshold, linkedDevice.writtenBytes.back().data() + 20,
                sizeof(packedAlphaClipThreshold));
    assert(packedAlphaClipThreshold == 0.35f);

    linkedMaterialState.alphaClipEnabled = false;
    linkedDesc.material->SetRenderState(linkedMaterialState);
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {},
                                      true, perView));
    std::memcpy(&packedAlphaClipThreshold, linkedDevice.writtenBytes.back().data() + 20,
                sizeof(packedAlphaClipThreshold));
    assert(packedAlphaClipThreshold == 0.0f);

    linkedBillboard.Destroy();
    assert(linkedDevice.bufferReleases == 1 && linkedDevice.groupReleases == 2 && linkedDevice.textureReleases == 0 &&
           linkedDevice.samplerReleases == 0);

    auto linkedForwardPlusArtifact = std::make_shared<ShaderProgramArtifact>(*linkedArtifact);
    linkedForwardPlusArtifact->key.revision = 8;
    auto linkedForwardPlusVariant = linkedForwardPlusArtifact->variants.front();
    linkedForwardPlusVariant.target = ShaderCompileTarget::ForwardPlus;
    linkedForwardPlusArtifact->variants.push_back(std::move(linkedForwardPlusVariant));
    assert(linkedForwardPlusArtifact->IsValid());
    FakeDevice linkedForwardPlusDevice;
    auto linkedForwardPlusDesc = linkedDesc;
    linkedForwardPlusDesc.shaderProgram = linkedForwardPlusArtifact;
    linkedForwardPlusDesc.semantics.receiveSceneLighting = true;
    linkedForwardPlusDesc.deletionQueue = nullptr;
    particle::ParticleGpuBillboardRenderer linkedForwardPlusBillboard;
    assert(linkedForwardPlusBillboard.Create(linkedForwardPlusDevice, linkedForwardPlusDesc));
    assert(linkedForwardPlusDevice.shaderCreates == 5);
    GraphicsTrace linkedForwardPlusTrace;
    const rhi::GraphicsCommandEncoder linkedForwardPlusEncoder(&linkedForwardPlusTrace, &graphicsDispatch);
    auto linkedForwardPlusPass = forwardPass;
    linkedForwardPlusPass.target = ShaderCompileTarget::ForwardPlus;
    const particle::GpuParticlePerViewBindings linkedLighting{{930, 1}, {931, 1}};
    assert(linkedForwardPlusBillboard.RecordDraw(linkedForwardPlusEncoder, firstTarget, linkedForwardPlusPass,
                                                 indirectBuffer, view, {}, {}, true, linkedLighting));
    assert(linkedForwardPlusDevice.graphicsPipelineDescs.size() == 1 &&
           linkedForwardPlusDevice.graphicsPipelineDescs[0].bindingLayoutCount == 3 &&
           linkedForwardPlusTrace.groupSets == std::vector<uint32_t>({0, 1, 2}));
    linkedForwardPlusBillboard.Destroy();
    linkedTextureSlots.clear();
    assert(linkedDevice.textureReleases == 3 && linkedDevice.samplerReleases == 3);

    {
        FakeDevice meshDevice;
        std::array<uint32_t, 4> meshVertexShader = {0x07230203};
        std::array<uint32_t, 4> meshFragmentShader = {0x07230203};
        std::array<uint32_t, 4> meshPickingShader = {0x07230203};
        auto mesh = std::make_shared<InxMesh>("particle-triangle");
        std::vector<Vertex> vertices(3);
        vertices[0].pos = {-0.5f, -0.5f, 0.0f};
        vertices[1].pos = {0.5f, -0.5f, 0.0f};
        vertices[2].pos = {0.0f, 0.5f, 0.0f};
        for (auto &vertex : vertices) {
            vertex.normal = {0.0f, 0.0f, 1.0f};
            vertex.tangent = {1.0f, 0.0f, 0.0f, 1.0f};
        }
        SubMesh triangleSubMesh;
        triangleSubMesh.indexCount = 3;
        triangleSubMesh.vertexCount = 3;
        mesh->SetData(std::move(vertices), {0, 1, 2}, {triangleSubMesh});

        particle::GpuMeshRendererDesc meshDesc;
        meshDesc.vertexShader = {meshVertexShader.data(), meshVertexShader.size()};
        meshDesc.shadowFragmentShader = {meshFragmentShader.data(), meshFragmentShader.size()};
        meshDesc.pickingFragmentShader = {meshPickingShader.data(), meshPickingShader.size()};
        meshDesc.motionVertexShader = {meshVertexShader.data(), meshVertexShader.size()};
        meshDesc.motionFragmentShader = {meshFragmentShader.data(), meshFragmentShader.size()};
        meshDesc.instances = instanceBuffer;
        meshDesc.renderIndices = renderIndexBuffer;
        meshDesc.mesh = mesh;
        meshDesc.meshVertices = meshDevice.CreateBuffer({3 * 5 * sizeof(glm::vec4), rhi::BufferUsageFlags::Storage});
        meshDesc.meshIndices = meshDevice.CreateBuffer({3 * sizeof(uint32_t), rhi::BufferUsageFlags::Storage});
        meshDesc.indexCount = 3;
        meshDesc.meshBufferKeepAlive = std::make_shared<int>(1);
        meshDesc.shaderProgram = billboardArtifact;
        meshDesc.material = std::make_shared<InxMaterial>("particle-mesh-material");
        meshDesc.material->SetRenderQueue(2450);
        meshDesc.material->SetColor("baseColor", glm::vec4(0.2f, 0.4f, 0.8f, 0.75f));
        meshDesc.material->SetTextureGuid("texSampler", "white");
        TestTextureSlots meshTextureSlots;
        meshDesc.textureResolver = [&meshDevice, &meshTextureSlots](const std::string &guid, const std::string &name) {
            assert(name == "texSampler");
            return AcquireTestTexture(meshDevice, meshTextureSlots, guid, 1, {980, 1}, {981, 1});
        };

        particle::ParticleGpuMeshRenderer meshRenderer;
        assert(meshRenderer.Create(meshDevice, meshDesc));
        assert(meshRenderer.IsValid() && meshRenderer.VertexCount() == 3 && meshRenderer.RenderQueue() == 2450);
        assert(meshRenderer.InstanceBuffer() == instanceBuffer &&
               meshRenderer.RenderIndexBuffer() == renderIndexBuffer);
        const auto staticMeshBuffers = meshRenderer.StaticVertexStorageBuffers();
        assert(staticMeshBuffers.size() == 2 && staticMeshBuffers[0].buffer == meshDesc.meshVertices &&
               staticMeshBuffers[0].byteSize == 3 * 5 * sizeof(glm::vec4) &&
               staticMeshBuffers[1].buffer == meshDesc.meshIndices &&
               staticMeshBuffers[1].byteSize == 3 * sizeof(uint32_t));
        assert(meshDevice.buffers.size() == 2 && meshDevice.initialBufferBytes.size() == 2);
        assert(meshDevice.buffers[0].usage == rhi::BufferUsageFlags::Storage &&
               meshDevice.buffers[0].byteSize == 3 * 5 * sizeof(glm::vec4));
        assert(meshDevice.buffers[1].usage == rhi::BufferUsageFlags::Storage &&
               meshDevice.buffers[1].byteSize == 3 * sizeof(uint32_t));
        assert(meshDevice.initialBufferBytes[0].empty() && meshDevice.initialBufferBytes[1].empty());
        assert(meshDevice.layoutEntryCounts == std::vector<uint32_t>({4, 0, 2}) &&
               meshDevice.groupBufferCounts == std::vector<uint32_t>({0, 0, 4}));

        GraphicsTrace meshTrace;
        const rhi::GraphicsCommandEncoder::Dispatch meshGraphicsDispatch = {
            &GraphicsTrace::BindPipeline, &GraphicsTrace::BindGroup, &GraphicsTrace::PushConstants,
            &GraphicsTrace::Draw, &GraphicsTrace::DrawIndirect};
        const rhi::GraphicsCommandEncoder meshEncoder(&meshTrace, &meshGraphicsDispatch);
        assert(meshRenderer.RecordDraw(meshEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, {}, true,
                                       perView));
        assert(meshTrace.indirectBuffers == std::vector<rhi::BufferHandle>({indirectBuffer}));
        assert(meshDevice.graphicsPipelineDescs.size() == 1 &&
               meshDevice.graphicsPipelineDescs[0].raster.cullMode == rhi::CullMode::Back &&
               meshDevice.graphicsPipelineDescs[0].raster.frontFace == rhi::FrontFace::Clockwise);
        assert(meshTrace.constants.size() == 1 &&
               meshTrace.constants[0].materialTint == (std::array<float, 4>{1.0f, 1.0f, 1.0f, 1.0f}));

        const auto sortedRenderIndexBuffer =
            meshDevice.CreateBuffer({runtime.Capacity() * sizeof(uint32_t), rhi::BufferUsageFlags::Storage});
        assert(meshRenderer.RecordDraw(meshEncoder, firstTarget, forwardPass, indirectBuffer, view,
                                       sortedRenderIndexBuffer, {}, true, perView));
        assert(meshDevice.groupBufferCounts == std::vector<uint32_t>({0, 0, 4, 4}) &&
               meshDevice.bindGroups.back().buffers[1].buffer == sortedRenderIndexBuffer);
        assert(meshTrace.groups.size() == 6 && meshTrace.groups[0] != meshTrace.groups[3]);

        auto pickingPass = forwardPass;
        pickingPass.target = ShaderCompileTarget::Picking;
        pickingPass.colorFormats = {rhi::PixelFormat::RG32UInt};
        pickingPass.samples = rhi::SampleCount::One;
        assert(meshRenderer.RecordPickingDraw(meshEncoder, {101, 1}, pickingPass, indirectBuffer, view,
                                              0x123456789abcdef0ull));
        assert(meshDevice.graphicsPipelineCreates == 2 && meshTrace.indirectBuffers.size() == 3);
        std::array<uint32_t, 4> encodedObjectId{};
        std::memcpy(encodedObjectId.data(), meshTrace.constants.back().materialTint.data(), sizeof(encodedObjectId));
        assert(encodedObjectId[0] == 0x9abcdef0u && encodedObjectId[1] == 0x12345678u);

        meshRenderer.Destroy();
        assert(!meshRenderer.IsValid() && meshDevice.bufferReleases == 0 && meshDevice.shaderReleases == 6 &&
               meshDevice.layoutReleases == 3 && meshDevice.groupReleases == 4 &&
               meshDevice.graphicsPipelineReleases == 2);
        meshDevice.Release(sortedRenderIndexBuffer);

        {
            FakeDevice litMeshDevice;
            auto litMeshDesc = meshDesc;
            auto litMeshArtifact = std::make_shared<ShaderProgramArtifact>(*billboardArtifact);
            litMeshArtifact->key.revision = 3;
            auto meshForwardPlus = billboardForward;
            meshForwardPlus.target = ShaderCompileTarget::ForwardPlus;
            litMeshArtifact->variants.push_back(std::move(meshForwardPlus));
            assert(litMeshArtifact->IsValid());
            litMeshDesc.shaderProgram = std::move(litMeshArtifact);
            litMeshDesc.semantics.receiveSceneLighting = true;
            particle::ParticleGpuMeshRenderer litMeshRenderer;
            assert(litMeshRenderer.Create(litMeshDevice, litMeshDesc));

            auto forwardPlusPass = forwardPass;
            forwardPlusPass.target = ShaderCompileTarget::ForwardPlus;
            GraphicsTrace litMeshTrace;
            const rhi::GraphicsCommandEncoder litMeshEncoder(&litMeshTrace, &meshGraphicsDispatch);
            assert(!litMeshRenderer.RecordDraw(litMeshEncoder, firstTarget, forwardPlusPass, indirectBuffer, view));
            const particle::GpuParticlePerViewBindings lighting{{910, 1}, {911, 1}};
            assert(litMeshRenderer.RecordDraw(litMeshEncoder, firstTarget, forwardPlusPass, indirectBuffer, view, {},
                                              {}, true, lighting));
            assert(litMeshDevice.graphicsPipelineDescs.size() == 1);
            const auto &litMeshPipeline = litMeshDevice.graphicsPipelineDescs.front();
            assert(litMeshPipeline.bindingLayoutCount == 3 && litMeshPipeline.bindingLayouts[1] == lighting.layout);
            assert(litMeshTrace.groupSets == std::vector<uint32_t>({0, 1, 2}) &&
                   litMeshTrace.groups[1] == lighting.group);
            assert(litMeshTrace.constants.size() == 1 && litMeshTrace.constants[0].lightingControl[0] == 1.0f);
            litMeshRenderer.Destroy();
        }

        auto invalidMesh = std::make_shared<InxMesh>("invalid-particle-mesh");
        invalidMesh->SetData(std::vector<Vertex>(1), {1}, {});
        meshDesc.mesh = invalidMesh;
        meshDesc.indexCount = 1;
        assert(!meshRenderer.Create(meshDevice, meshDesc));
        meshDevice.Release(meshDesc.meshIndices);
        meshDevice.Release(meshDesc.meshVertices);
    }

    {
        auto registryBillboardDesc = billboardDesc;
        registryBillboardDesc.renderIndices = renderIndexBuffer;
        registryBillboardDesc.material->SetRenderQueue(3150);
        auto registeredBillboard = std::make_shared<particle::ParticleGpuBillboardRenderer>();
        assert(registeredBillboard->Create(device, registryBillboardDesc));
        particle::ParticleGpuDrawRegistry registry;
        const uint64_t initialRevision = registry.Revision();
        particle::GpuParticleDrawEntry registryEntry;
        registryEntry.id = 77;
        registryEntry.emitterId = 701;
        registryEntry.graphInstanceId = 702;
        registryEntry.ownerLayerMask = 1u << 6u;
        registryEntry.capacity = runtime.Capacity();
        registryEntry.instances = instanceBuffer;
        registryEntry.renderIndices = renderIndexBuffer;
        registryEntry.indirectArguments = indirectBuffer;
        registryEntry.bounds = {999, 1};
        registryEntry.simulationControl = runtime.SimulationControlBuffer();
        registryEntry.renderer = registeredBillboard;
        assert(registry.Set(std::move(registryEntry)));
        assert(registry.Revision() == initialRevision + 1 && registry.Size() == 1);
        const auto visibleEntries = registry.Snapshot(3000, 3200);
        assert(visibleEntries.size() == 1 && visibleEntries[0].id == 77 &&
               visibleEntries[0].ownerLayerMask == 1u << 6u);
        assert(registry.Snapshot(0, 2999).empty());
        assert(registry.Remove(77) && !registry.Remove(77) && registry.Size() == 0);
    }
    const uint32_t textureReleasesBeforeSlotsClear = device.textureReleases;
    const uint32_t samplerReleasesBeforeSlotsClear = device.samplerReleases;
    textureSlots.clear();
    assert(device.textureReleases == textureReleasesBeforeSlotsClear + 2u);
    assert(device.samplerReleases == samplerReleasesBeforeSlotsClear + 2u);

    const uint32_t bufferReleasesBeforeRuntimeDestroy = device.bufferReleases;
    const uint32_t pipelineReleasesBeforeRuntimeDestroy = device.pipelineReleases;
    const uint32_t shaderReleasesBeforeRuntimeDestroy = device.shaderReleases;
    const uint32_t groupReleasesBeforeRuntimeDestroy = device.groupReleases;
    const uint32_t layoutReleasesBeforeRuntimeDestroy = device.layoutReleases;
    const uint32_t textureReleasesBeforeRuntimeDestroy = device.textureReleases;
    const uint32_t samplerReleasesBeforeRuntimeDestroy = device.samplerReleases;
    runtime.Destroy();
    assert(!runtime.IsValid() && runtime.StateStride() == 0);
    assert(device.pipelineReleases == pipelineReleasesBeforeRuntimeDestroy + FusedGpuKernelStageCount);
    assert(device.groupReleases == groupReleasesBeforeRuntimeDestroy + 2u);
    assert(device.layoutReleases == layoutReleasesBeforeRuntimeDestroy + 3u);
    assert(device.bufferReleases == bufferReleasesBeforeRuntimeDestroy);
    assert(device.shaderReleases == shaderReleasesBeforeRuntimeDestroy);
    assert(device.textureReleases == textureReleasesBeforeRuntimeDestroy);
    assert(device.samplerReleases == samplerReleasesBeforeRuntimeDestroy);

    fusedRuntime.Destroy();
    assert(!fusedRuntime.IsValid() && fusedRuntime.StateStride() == 0);
    assert(device.pipelineReleases ==
           pipelineReleasesBeforeRuntimeDestroy + FusedGpuKernelStageCount + RequiredGpuKernelStageCount);
    assert(device.groupReleases == groupReleasesBeforeRuntimeDestroy + 4u);
    assert(device.layoutReleases == layoutReleasesBeforeRuntimeDestroy + 6u);
    assert(device.bufferReleases == bufferReleasesBeforeRuntimeDestroy + 8u);
    assert(device.shaderReleases == shaderReleasesBeforeRuntimeDestroy);
    assert(device.textureReleases == textureReleasesBeforeRuntimeDestroy);
    assert(device.samplerReleases == samplerReleasesBeforeRuntimeDestroy);
    return 0;
}
