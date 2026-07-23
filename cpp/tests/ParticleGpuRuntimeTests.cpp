#include <function/renderer/FrameDeletionQueue.h>
#include <function/renderer/SceneDepthResolver.h>
#include <function/renderer/particle/ParticleGpuBillboardRenderer.h>
#include <function/renderer/particle/ParticleGpuBounds.h>
#include <function/renderer/particle/ParticleGpuCuller.h>
#include <function/renderer/particle/ParticleGpuDrawRegistry.h>
#include <function/renderer/particle/ParticleGpuMeshRenderer.h>
#include <function/renderer/particle/ParticleGpuMigrator.h>
#include <function/renderer/particle/ParticleGpuRuntime.h>
#include <function/renderer/particle/ParticleGpuSorter.h>
#include <function/renderer/rhi/RhiBuffer.h>
#include <function/resources/InxMaterial/InxMaterial.h>

#include <array>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <iostream>
#include <vector>

namespace
{

using namespace infernux;

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
    std::vector<std::vector<uint8_t>> writtenBytes;
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
        assert(desc.computeShader.IsValid() && (desc.bindingLayoutCount == 1 || desc.bindingLayoutCount == 2) &&
               (desc.pushConstantBytes == 16 || desc.pushConstantBytes == 32 || desc.pushConstantBytes == 80 ||
                desc.pushConstantBytes == 112));
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
        assert(handle.IsValid() && offset == 0 && data && byteSize > 0);
        const auto *begin = static_cast<const uint8_t *>(data);
        writtenBytes.emplace_back(begin, begin + byteSize);
        ++writes;
        return true;
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

struct CommandTrace
{
    std::vector<rhi::ComputePipelineHandle> pipelines;
    std::vector<rhi::BindGroupHandle> groups;
    std::vector<uint32_t> groupSets;
    std::vector<particle::GpuParticlePushConstants> constants;
    std::vector<uint32_t> dispatches;

    static void BindPipeline(void *context, rhi::ComputePipelineHandle pipeline)
    {
        static_cast<CommandTrace *>(context)->pipelines.push_back(pipeline);
    }
    static void BindGroup(void *context, rhi::ComputePipelineHandle, uint32_t setIndex, rhi::BindGroupHandle group)
    {
        assert(setIndex <= 1);
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
    static void DispatchIndirect(void *, rhi::BufferHandle, uint64_t)
    {
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
        assert(setIndex <= 1);
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
        FakeDevice sharingDevice;
        std::array<std::array<uint32_t, 4>, static_cast<size_t>(particle::GpuKernelStage::Count)> sharingWords{};
        particle::GpuEmitterDesc sharingDesc;
        sharingDesc.capacity = 128;
        sharingDesc.stateStride = 32;
        for (size_t index = 0; index < sharingWords.size(); ++index) {
            sharingWords[index][0] = 0x07230203;
            sharingDesc.kernels[index] = {sharingWords[index].data(), sharingWords[index].size()};
        }

        particle::ParticleGpuRuntime previous;
        particle::ParticleGpuRuntime compatible;
        assert(previous.Create(sharingDevice, sharingDesc));
        assert(previous.NeedsBootstrap());
        CommandTrace sharingTrace;
        const rhi::ComputeCommandEncoder::DispatchTable sharingDispatch = {
            &CommandTrace::BindPipeline, &CommandTrace::BindGroup, &CommandTrace::PushConstants,
            &CommandTrace::Dispatch, &CommandTrace::DispatchIndirect};
        const rhi::ComputeCommandEncoder sharingEncoder(&sharingTrace, &sharingDispatch);
        previous.RecordBootstrap(sharingEncoder, 13);
        assert(!previous.NeedsBootstrap());
        const auto state = previous.StateBuffer();
        const auto counters = previous.CounterBuffer();
        assert(compatible.CreateCompatible(sharingDevice, sharingDesc, previous));
        assert(compatible.SharesStateWith(previous));
        assert(!compatible.NeedsBootstrap());
        assert(compatible.StateBuffer() == state && compatible.CounterBuffer() == counters);
        assert(sharingDevice.buffers.size() == 7);
        assert(previous.AdoptCompatibleRevision(compatible));
        assert(previous.IsValid() && compatible.IsValid() && previous.SharesStateWith(compatible));
        compatible.RequestBootstrap();
        assert(previous.NeedsBootstrap() && compatible.NeedsBootstrap());
        compatible.RecordBootstrap(sharingEncoder, 13);
        assert(!previous.NeedsBootstrap() && !compatible.NeedsBootstrap());
        previous.Destroy();
        assert(compatible.IsValid() && sharingDevice.bufferReleases == 0);
        compatible.Destroy();
        assert(sharingDevice.bufferReleases == 7 && sharingDevice.pipelineReleases == 10 &&
               sharingDevice.groupReleases == 2 && sharingDevice.layoutReleases == 2);
    }

    {
        FakeDevice pointCacheDevice;
        std::array<std::array<uint32_t, 4>, static_cast<size_t>(particle::GpuKernelStage::Count)> pointCacheWords{};
        particle::GpuEmitterDesc pointCacheDesc;
        pointCacheDesc.capacity = 64;
        pointCacheDesc.stateStride = 48;
        for (size_t index = 0; index < pointCacheWords.size(); ++index) {
            pointCacheWords[index][0] = 0x07230203;
            pointCacheDesc.kernels[index] = {pointCacheWords[index].data(), pointCacheWords[index].size()};
        }

        auto cache = std::make_shared<PointCacheCpuData>();
        cache->stableId = "runtime-points";
        cache->name = "Runtime Points";
        cache->bakeBasis = "right_handed_y_up";
        cache->pointCount = 2;
        cache->channels = {
            {"position", PointCacheChannelType::Float3, PointCacheChannelSemantic::Position, 0, 12},
            {"id", PointCacheChannelType::UInt, PointCacheChannelSemantic::Id, 32, 4},
        };
        cache->bytes.resize(40);
        const std::array<float, 6> positions = {1.0f, 2.0f, 3.0f, 4.0f, 5.0f, 6.0f};
        const std::array<uint32_t, 2> ids = {7, 42};
        std::memcpy(cache->bytes.data(), positions.data(), sizeof(positions));
        std::memcpy(cache->bytes.data() + 32, ids.data(), sizeof(ids));
        cache->RebuildIdLookup();
        assert(cache->IsValid() && cache->idLookupMode == PointCacheIdLookupMode::Hash);

        particle::GpuPointCacheDesc pointCache;
        pointCache.interfaceIndex = 0;
        pointCache.dataBinding = 1;
        pointCache.lookupBinding = 2;
        pointCache.worldSpace = false;
        pointCache.cacheToSpace = {
            1.0f, 0.0f, 0.0f, 2.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f,
        };
        pointCache.data = cache;
        rhi::BufferDesc cacheDataDesc;
        cacheDataDesc.byteSize = cache->bytes.size();
        cacheDataDesc.usage = rhi::BufferUsageFlags::Storage;
        const auto cacheDataResource = std::make_shared<rhi::BufferResource>(
            pointCacheDevice, pointCacheDevice.CreateBuffer(cacheDataDesc), cacheDataDesc.byteSize);
        rhi::BufferDesc cacheLookupDesc;
        cacheLookupDesc.byteSize = cache->idLookup.size() * sizeof(PointCacheIdLookupEntry);
        cacheLookupDesc.usage = rhi::BufferUsageFlags::Storage;
        const auto cacheLookupResource = std::make_shared<rhi::BufferResource>(
            pointCacheDevice, pointCacheDevice.CreateBuffer(cacheLookupDesc), cacheLookupDesc.byteSize);
        const auto cacheResources = std::make_shared<std::array<std::shared_ptr<rhi::BufferResource>, 2>>();
        (*cacheResources)[0] = cacheDataResource;
        (*cacheResources)[1] = cacheLookupResource;
        pointCache.dataBuffer = cacheDataResource->GetBuffer();
        pointCache.lookupBuffer = cacheLookupResource->GetBuffer();
        pointCache.keepAlive = cacheResources;
        pointCache.samples.push_back({0, "position", PointCacheChannelType::Float3, false});
        pointCacheDesc.pointCaches.sampleCount = 1;
        pointCacheDesc.pointCaches.pointCaches.push_back(pointCache);

        particle::ParticleGpuRuntime pointCacheRuntime;
        assert(pointCacheRuntime.Create(pointCacheDevice, pointCacheDesc));
        assert(pointCacheRuntime.IsValid());
        assert(pointCacheDevice.buffers.size() == 10);
        assert(pointCacheDevice.layouts.size() == 2 && pointCacheDevice.layouts[1].entryCount == 3);
        assert(pointCacheDevice.bindGroups.size() == 2 && pointCacheDevice.bindGroups[1].bufferCount == 3);
        assert(pointCacheDevice.computePipelineDescs.size() == 5 &&
               pointCacheDevice.computePipelineDescs[0].bindingLayoutCount == 2);
        assert(pointCacheDevice.buffers[0].memory == rhi::BufferMemory::DeviceLocal &&
               pointCacheDevice.buffers[1].memory == rhi::BufferMemory::DeviceLocal);
        const auto readWord = [](const std::vector<uint8_t> &bytes, size_t index) {
            uint32_t result = 0;
            std::memcpy(&result, bytes.data() + index * sizeof(result), sizeof(result));
            return result;
        };
        assert(readWord(pointCacheDevice.initialBufferBytes[9], 28) == 2 &&
               readWord(pointCacheDevice.initialBufferBytes[9], 29) == 3 &&
               readWord(pointCacheDevice.initialBufferBytes[9], 30) == 1);
        assert(readWord(pointCacheDevice.initialBufferBytes[9], 32) == 0 &&
               readWord(pointCacheDevice.initialBufferBytes[9], 33) == 3);

        particle::GpuParticleTransforms pointCacheTransforms;
        for (uint32_t index = 0; index < 4; ++index) {
            pointCacheTransforms.emitterToWorld[index * 5] = 1.0f;
            pointCacheTransforms.worldToEmitter[index * 5] = 1.0f;
            pointCacheTransforms.simulationToWorld[index * 5] = 1.0f;
            pointCacheTransforms.worldToSimulation[index * 5] = 1.0f;
        }
        pointCacheTransforms.emitterToWorld[12] = 10.0f;
        assert(pointCacheRuntime.UpdateTransforms(pointCacheTransforms));
        assert(pointCacheDevice.writes == 2 && pointCacheDevice.writtenBytes[1].size() == 36 * sizeof(uint32_t));
        const uint32_t translatedBits = readWord(pointCacheDevice.writtenBytes[1], 12);
        float translatedX = 0.0f;
        std::memcpy(&translatedX, &translatedBits, sizeof(translatedX));
        assert(translatedX == 12.0f);

        CommandTrace pointCacheTrace;
        const rhi::ComputeCommandEncoder::DispatchTable pointCacheDispatch = {
            &CommandTrace::BindPipeline, &CommandTrace::BindGroup, &CommandTrace::PushConstants,
            &CommandTrace::Dispatch, &CommandTrace::DispatchIndirect};
        const rhi::ComputeCommandEncoder pointCacheEncoder(&pointCacheTrace, &pointCacheDispatch);
        pointCacheRuntime.RecordUpdate(pointCacheEncoder, 11, 3, 1.0f / 60.0f);
        assert(pointCacheTrace.groups.size() == 2 && pointCacheTrace.groupSets == std::vector<uint32_t>({0, 1}));
        pointCacheRuntime.Destroy();
        assert(pointCacheDevice.bufferReleases == 8 && pointCacheDevice.layoutReleases == 2 &&
               pointCacheDevice.groupReleases == 2 && pointCacheDevice.pipelineReleases == 5);
    }

    FakeDevice device;
    FrameDeletionQueue deletionQueue;
    deletionQueue.Initialize(2);
    std::array<std::array<uint32_t, 4>, static_cast<size_t>(particle::GpuKernelStage::Count)> words{};
    particle::GpuEmitterDesc desc;
    desc.capacity = 1000;
    desc.stateStride = 64;
    for (size_t index = 0; index < words.size(); ++index) {
        words[index][0] = 0x07230203;
        desc.kernels[index] = {words[index].data(), words[index].size()};
    }

    particle::ParticleGpuRuntime runtime;
    assert(runtime.Create(device, desc));
    assert(runtime.IsValid() && runtime.Capacity() == 1000 && runtime.StateStride() == 64);
    assert(device.buffers.size() == 7);
    assert(device.buffers[0].byteSize == 64000);
    assert(device.buffers[3].byteSize == 48000);
    assert(rhi::HasBufferUsage(device.buffers[4].usage, rhi::BufferUsageFlags::Indirect));
    assert(device.buffers[5].byteSize == 4000 && device.buffers[5].usage == rhi::BufferUsageFlags::Storage);
    assert(device.buffers[6].byteSize == sizeof(particle::GpuParticleTransforms) &&
           device.buffers[6].memory == rhi::BufferMemory::Upload);
    assert(device.shaderCreates == 5 && device.shaderReleases == 5);
    assert(device.layoutCreates == 1 && device.groupCreates == 1 && device.pipelineCreates == 5);

    particle::GpuParticleTransforms transforms;
    assert(runtime.UpdateTransforms(transforms));
    assert(device.writes == 1);

    CommandTrace trace;
    const rhi::ComputeCommandEncoder::DispatchTable dispatch = {&CommandTrace::BindPipeline, &CommandTrace::BindGroup,
                                                                &CommandTrace::PushConstants, &CommandTrace::Dispatch,
                                                                &CommandTrace::DispatchIndirect};
    const rhi::ComputeCommandEncoder encoder(&trace, &dispatch);
    runtime.RecordBootstrap(encoder, 7);
    runtime.RecordInit(encoder, 300, 100, 2, 7, 9, 1.0f / 60.0f);
    runtime.RecordUpdate(encoder, 7, 9, 1.0f / 60.0f);
    runtime.RecordRenderReset(encoder);
    runtime.RecordRendering(encoder, 7, 9);
    assert(trace.pipelines.size() == 5 && trace.groups.size() == 5 && trace.constants.size() == 5);
    assert(trace.dispatches == std::vector<uint32_t>({4, 2, 4, 1, 4}));
    assert(trace.constants[1].spawnBaseId == 100 && trace.constants[1].spawnGeneration == 2);
    assert(trace.constants[2].simulationStep == 9);

    FakeDevice boundsDevice;
    std::array<std::array<uint32_t, 5>, 2> boundsWords{};
    for (auto &shader : boundsWords)
        shader[0] = 0x07230203u;
    particle::GpuParticleBoundsDesc boundsDesc;
    boundsDesc.capacity = runtime.Capacity();
    boundsDesc.instances = runtime.InstanceBuffer();
    boundsDesc.sourceIndirectArguments = runtime.IndirectBuffer();
    boundsDesc.program = {
        {boundsWords[0].data(), boundsWords[0].size()},
        {boundsWords[1].data(), boundsWords[1].size()},
    };
    particle::GpuParticleBoundsProgramStorage boundsProgramStorage;
    assert(boundsProgramStorage.Assign(boundsDesc.program) && boundsProgramStorage.IsValid());
    assert(boundsProgramStorage.View().reset.words != boundsDesc.program.reset.words &&
           boundsProgramStorage.View().reset.wordCount == boundsDesc.program.reset.wordCount);
    particle::ParticleGpuBounds bounds;
    assert(bounds.Create(boundsDevice, boundsDesc));
    assert(bounds.IsValid() && bounds.Capacity() == 1000 && bounds.InstanceBuffer() == runtime.InstanceBuffer() &&
           bounds.SourceIndirectBuffer() == runtime.IndirectBuffer());
    assert(boundsDevice.buffers.size() == 2 &&
           boundsDevice.buffers[0].byteSize == particle::ParticleGpuBounds::BoundsBufferBytes &&
           boundsDevice.buffers[0].usage == rhi::BufferUsageFlags::Storage &&
           boundsDevice.buffers[1].byteSize == particle::ParticleGpuBounds::DispatchBufferBytes &&
           rhi::HasBufferUsage(boundsDevice.buffers[1].usage, rhi::BufferUsageFlags::Storage) &&
           rhi::HasBufferUsage(boundsDevice.buffers[1].usage, rhi::BufferUsageFlags::Indirect));
    assert(boundsDevice.layouts.size() == 1 && boundsDevice.layouts[0].entryCount == 4 &&
           boundsDevice.bindGroups.size() == 1 && boundsDevice.bindGroups[0].bufferCount == 4 &&
           boundsDevice.shaderCreates == 2 && boundsDevice.shaderReleases == 2 && boundsDevice.pipelineCreates == 2);

    BoundsTrace boundsTrace;
    const rhi::ComputeCommandEncoder::DispatchTable boundsDispatch = {
        &BoundsTrace::BindPipeline, &BoundsTrace::BindGroup, &BoundsTrace::PushConstants, &BoundsTrace::Dispatch,
        &BoundsTrace::DispatchIndirect};
    const rhi::ComputeCommandEncoder boundsEncoder(&boundsTrace, &boundsDispatch);
    bounds.RecordReset(boundsEncoder);
    bounds.RecordReduce(boundsEncoder);
    assert(boundsTrace.pipelines.size() == 2 && boundsTrace.groups.size() == 2 && boundsTrace.constants.size() == 2);
    assert(boundsTrace.dispatches == std::vector<uint32_t>({1}));
    assert(boundsTrace.indirectDispatches == std::vector<rhi::BufferHandle>({bounds.DispatchBuffer()}));
    assert(boundsTrace.constants[0].capacity == 1000 && boundsTrace.constants[1].capacity == 1000);
    bounds.Destroy();
    assert(!bounds.IsValid() && boundsDevice.bufferReleases == 2 && boundsDevice.groupReleases == 1 &&
           boundsDevice.layoutReleases == 1 && boundsDevice.pipelineReleases == 2);

    FakeDevice cullDevice;
    std::array<std::array<uint32_t, 5>, 3> cullWords{};
    for (auto &shader : cullWords)
        shader[0] = 0x07230203u;
    particle::GpuParticleCullerDesc cullerDesc;
    cullerDesc.capacity = runtime.Capacity();
    cullerDesc.vertexCount = 6;
    cullerDesc.instances = runtime.InstanceBuffer();
    cullerDesc.sourceIndirectArguments = runtime.IndirectBuffer();
    cullerDesc.bounds = {800, 1};
    cullerDesc.program = {
        {cullWords[0].data(), cullWords[0].size()},
        {cullWords[1].data(), cullWords[1].size()},
        {cullWords[2].data(), cullWords[2].size()},
    };
    particle::GpuParticleCullProgramStorage cullProgramStorage;
    assert(cullProgramStorage.Assign(cullerDesc.program) && cullProgramStorage.IsValid());
    assert(cullProgramStorage.View().reset.words != cullerDesc.program.reset.words &&
           cullProgramStorage.View().reset.wordCount == cullerDesc.program.reset.wordCount);
    particle::ParticleGpuCuller sceneCuller;
    particle::ParticleGpuCuller gameCuller;
    assert(sceneCuller.Create(cullDevice, cullerDesc));
    assert(gameCuller.Create(cullDevice, cullerDesc));
    assert(sceneCuller.IsValid() && gameCuller.IsValid() && sceneCuller.Capacity() == 1000 &&
           sceneCuller.InstanceBuffer() == runtime.InstanceBuffer() &&
           sceneCuller.SourceIndirectBuffer() == runtime.IndirectBuffer() &&
           sceneCuller.BoundsBuffer() == cullerDesc.bounds);
    assert(sceneCuller.VisibleIndexBuffer() != gameCuller.VisibleIndexBuffer() &&
           sceneCuller.DrawIndirectBuffer() != gameCuller.DrawIndirectBuffer() &&
           sceneCuller.SortDispatchBuffer() != gameCuller.SortDispatchBuffer());
    assert(cullDevice.buffers.size() == 6 && cullDevice.buffers[0].byteSize == 4000 &&
           cullDevice.buffers[0].usage == rhi::BufferUsageFlags::Storage && cullDevice.buffers[1].byteSize == 16 &&
           rhi::HasBufferUsage(cullDevice.buffers[1].usage, rhi::BufferUsageFlags::Indirect) &&
           cullDevice.buffers[2].byteSize == 12 &&
           rhi::HasBufferUsage(cullDevice.buffers[2].usage, rhi::BufferUsageFlags::Indirect));
    assert(cullDevice.layouts.size() == 2 && cullDevice.layouts[0].entryCount == 6 &&
           cullDevice.bindGroups.size() == 2 && cullDevice.bindGroups[0].bufferCount == 6 &&
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
           cullTrace.constants[2].capacity == 1000);
    sceneCuller.Destroy();
    gameCuller.Destroy();
    assert(!sceneCuller.IsValid() && !gameCuller.IsValid() && cullDevice.bufferReleases == 6 &&
           cullDevice.groupReleases == 2 && cullDevice.layoutReleases == 2 && cullDevice.pipelineReleases == 6);

    FakeDevice sortDevice;
    std::array<std::array<uint32_t, 5>, 4> sortWords{};
    for (auto &shader : sortWords)
        shader[0] = 0x07230203u;
    particle::GpuParticleSorterDesc sorterDesc;
    sorterDesc.capacity = runtime.Capacity();
    sorterDesc.instances = runtime.InstanceBuffer();
    sorterDesc.indirectArguments = runtime.IndirectBuffer();
    sorterDesc.sourceIndices = runtime.RenderIndexBuffer();
    sorterDesc.dispatchArguments = {700, 1};
    sorterDesc.program = {
        {sortWords[0].data(), sortWords[0].size()},
        {sortWords[1].data(), sortWords[1].size()},
        {sortWords[2].data(), sortWords[2].size()},
        {sortWords[3].data(), sortWords[3].size()},
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
    assert(sortDevice.buffers.size() == 14 && sortDevice.buffers[0].byteSize == 4000 &&
           sortDevice.buffers[4].byteSize == 256 && sortDevice.buffers[5].byteSize == 256 &&
           sortDevice.buffers[6].byteSize == 64);
    assert(sortDevice.layouts.size() == 2 && sortDevice.layouts[0].entryCount == 11 &&
           sortDevice.bindGroups.size() == 4 && sortDevice.bindGroups[0].bufferCount == 11 &&
           sortDevice.bindGroups[1].bufferCount == 11);
    assert(sortDevice.shaderCreates == 8 && sortDevice.shaderReleases == 8 && sortDevice.pipelineCreates == 8);

    SortTrace sortTrace;
    const rhi::ComputeCommandEncoder::DispatchTable sortDispatch = {&SortTrace::BindPipeline, &SortTrace::BindGroup,
                                                                    &SortTrace::PushConstants, &SortTrace::Dispatch,
                                                                    &SortTrace::DispatchIndirect};
    const rhi::ComputeCommandEncoder sortEncoder(&sortTrace, &sortDispatch);
    std::array<float, 16> sortView{};
    sortView[0] = sortView[5] = sortView[10] = sortView[15] = 1.0f;
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
           sortDevice.groupReleases == 4 && sortDevice.layoutReleases == 2 && sortDevice.pipelineReleases == 8);

    const auto instanceBuffer = runtime.InstanceBuffer();
    const auto renderIndexBuffer = runtime.RenderIndexBuffer();
    const auto indirectBuffer = runtime.IndirectBuffer();
    std::array<uint32_t, 4> billboardVertex = {0x07230203};
    std::array<uint32_t, 4> billboardFragment = {0x07230203};
    particle::GpuBillboardRendererDesc billboardDesc;
    billboardDesc.vertexShader = {billboardVertex.data(), billboardVertex.size()};
    billboardDesc.fragmentShader = {billboardFragment.data(), billboardFragment.size()};
    billboardDesc.instances = instanceBuffer;
    billboardDesc.renderIndices = renderIndexBuffer;
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
    billboardDesc.textureResolver = [&textureResolveCount, &normalTextureReady](const std::string &textureGuid,
                                                                                const std::string &name) {
        assert(name == "texSampler");
        ++textureResolveCount;
        if (textureGuid == "normal" && !normalTextureReady)
            return particle::GpuBillboardTextureLease{particle::GpuBillboardTextureStatus::Pending};
        const uint32_t identity = textureGuid == "normal" ? 2u : 1u;
        return particle::GpuBillboardTextureLease{particle::GpuBillboardTextureStatus::Ready,
                                                  {400u + identity, 1},
                                                  {500u + identity, 1},
                                                  std::make_shared<uint32_t>(identity),
                                                  true};
    };
    billboardDesc.textureVersionResolver = [](const std::string &textureGuid) {
        return textureGuid == "normal" ? uint64_t{2} : uint64_t{1};
    };
    billboardDesc.deletionQueue = &deletionQueue;

    particle::ParticleGpuBillboardRenderer billboard;
    assert(billboard.Create(device, billboardDesc));
    assert(billboard.IsValid() && billboard.RenderQueue() == 3100 && billboard.InstanceBuffer() == instanceBuffer);
    assert(device.layoutEntryCounts == std::vector<uint32_t>({7, 4}));
    assert(device.groupBufferCounts == std::vector<uint32_t>({7, 2}));
    assert(device.groupTextureCounts == std::vector<uint32_t>({0, 2}));
    assert(device.bindGroups.back().textures[0].binding == 2 && device.bindGroups.back().textures[1].binding == 15 &&
           !device.bindGroups.back().textures[1].depthRead);
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
    assert(billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(device.graphicsPipelineCreates == 1 && device.graphicsPipelineDescs.size() == 1);
    const auto &graphicsDesc = device.graphicsPipelineDescs.front();
    assert(graphicsDesc.pushConstantBytes == sizeof(view));
    assert(graphicsDesc.pushConstantStages == (rhi::ShaderStage::Vertex | rhi::ShaderStage::Fragment));
    assert(graphicsDesc.samples == rhi::SampleCount::Four);
    assert(graphicsDesc.depth.testEnabled && !graphicsDesc.depth.writeEnabled);
    assert(graphicsDesc.colorTargetCount == 1 && graphicsDesc.colorTargets[0].blendEnabled);
    assert(graphicsTrace.pipelines.size() == 2 && graphicsTrace.groups.size() == 2 &&
           graphicsTrace.constants.size() == 2 && graphicsTrace.indirectBuffers.size() == 2);

    billboardDesc.material->SetRenderQueue(3150);
    billboardDesc.material->SetColor("baseColor", glm::vec4(0.25f, 0.5f, 0.75f, 0.8f));
    assert(billboard.RenderQueue() == 3150);
    assert(billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(device.graphicsPipelineCreates == 1 && device.graphicsPipelineReleases == 0);
    assert(textureResolveCount == 1 && device.groupCreates == 2);
    const std::array<float, 4> expectedTint = {0.25f, 0.5f, 0.75f, 0.8f};
    assert(graphicsTrace.constants.back().materialTint == expectedTint);
    assert(textureResolveCount == 1 && device.groupCreates == 2);
    billboardDesc.material->SetTextureGuid("texSampler", "normal");
    assert(billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(textureResolveCount == 3 && device.groupCreates == 3 && device.groupReleases == 0);
    assert(device.textureReleases == 0 && device.samplerReleases == 0);
    assert(billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(textureResolveCount == 4 && device.groupCreates == 3);
    normalTextureReady = true;
    assert(billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(textureResolveCount == 5 && device.groupCreates == 4 && device.groupReleases == 0);
    deletionQueue.Tick();
    deletionQueue.Tick();
    deletionQueue.Tick();
    assert(device.groupReleases == 2 && device.textureReleases == 2 && device.samplerReleases == 2);
    liveMaterialState = billboardDesc.material->GetRenderState();
    liveMaterialState.blendEnable = false;
    liveMaterialState.depthWriteEnable = true;
    billboardDesc.material->SetRenderState(liveMaterialState);
    assert(billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(device.graphicsPipelineCreates == 2 && device.graphicsPipelineReleases == 0);
    const auto &updatedGraphicsDesc = device.graphicsPipelineDescs.back();
    assert(!updatedGraphicsDesc.colorTargets[0].blendEnabled && updatedGraphicsDesc.depth.writeEnabled);
    liveMaterialState.blendEnable = true;
    liveMaterialState.depthWriteEnable = false;
    billboardDesc.material->SetRenderState(liveMaterialState);
    assert(billboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(device.graphicsPipelineCreates == 2 && device.graphicsPipelineReleases == 0);

    MaterialPassPipelineDescriptor unsupportedPass = forwardPass;
    unsupportedPass.target = ShaderCompileTarget::GBuffer;
    assert(!billboard.RecordDraw(graphicsEncoder, firstTarget, unsupportedPass, indirectBuffer, view));
    billboard.Destroy();
    assert(!billboard.IsValid() && device.graphicsPipelineReleases == 2);

    {
        FakeDevice litDevice;
        std::array<uint32_t, 4> forwardPlusFragment = {0x07230203};
        auto litDesc = billboardDesc;
        litDesc.forwardPlusFragmentShader = {forwardPlusFragment.data(), forwardPlusFragment.size()};
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
        const particle::GpuParticleForwardPlusBindings lighting{{900, 1}, {901, 1}};
        assert(litBillboard.RecordDraw(litEncoder, firstTarget, forwardPlusPass, indirectBuffer, view, {},
                                       litSceneDepth, true, lighting));
        assert(litDevice.graphicsPipelineDescs.size() == 1);
        const auto &litPipeline = litDevice.graphicsPipelineDescs.front();
        assert(litPipeline.bindingLayoutCount == 2 && litPipeline.bindingLayouts[1] == lighting.layout);
        assert(litTrace.groupSets == std::vector<uint32_t>({0, 1}) && litTrace.groups.back() == lighting.group);
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
    const rhi::TextureViewHandle sceneDepthView{990, 1};
    assert(softBillboard.RecordDraw(graphicsEncoder, firstTarget, forwardPass, indirectBuffer, view, {}, sceneDepthView,
                                    false));
    assert(!softDevice.bindGroups.back().textures[1].depthRead);
    auto singleSamplePass = forwardPass;
    singleSamplePass.samples = rhi::SampleCount::One;
    assert(!softBillboard.RecordDraw(graphicsEncoder, firstTarget, singleSamplePass, indirectBuffer, view));
    GraphicsTrace softTrace;
    const rhi::GraphicsCommandEncoder softEncoder(&softTrace, &graphicsDispatch);
    assert(
        softBillboard.RecordDraw(softEncoder, firstTarget, singleSamplePass, indirectBuffer, view, {}, sceneDepthView));
    assert(softDevice.bindGroups.size() == 3 && softDevice.bindGroups.back().textureCount == 2);
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
    assert(linkedArtifact->IsValid());

    FakeDevice linkedDevice;
    FrameDeletionQueue linkedDeletionQueue;
    linkedDeletionQueue.Initialize(2);
    particle::GpuBillboardRendererDesc linkedDesc;
    linkedDesc.shaderProgram = linkedArtifact;
    linkedDesc.instances = instanceBuffer;
    linkedDesc.renderIndices = renderIndexBuffer;
    linkedDesc.material = std::make_shared<InxMaterial>("linked-particle-material");
    linkedDesc.material->SetColor("baseColor", glm::vec4(0.2f, 0.4f, 0.6f, 0.8f));
    linkedDesc.material->SetFloat("intensity", 3.5f);
    linkedDesc.material->SetTextureGuid("albedo", "white");
    linkedDesc.material->SetTextureGuid("detail", "normal");
    uint32_t linkedTextureResolves = 0;
    linkedDesc.textureResolver = [&linkedTextureResolves](const std::string &guid, const std::string &name) {
        ++linkedTextureResolves;
        const uint32_t identity = name == "albedo" ? 1u : 2u;
        assert((name == "albedo" && (guid == "white" || guid == "black")) || (name == "detail" && guid == "normal"));
        return particle::GpuBillboardTextureLease{particle::GpuBillboardTextureStatus::Ready,
                                                  {600u + identity, 1},
                                                  {700u + identity, 1},
                                                  std::make_shared<uint32_t>(identity),
                                                  true};
    };
    linkedDesc.textureVersionResolver = [](const std::string &guid) {
        return guid == "black" ? uint64_t{2} : uint64_t{1};
    };
    linkedDesc.deletionQueue = &linkedDeletionQueue;

    auto linkedLitDesc = linkedDesc;
    linkedLitDesc.semantics.receiveSceneLighting = true;
    particle::ParticleGpuBillboardRenderer linkedLitBillboard;
    assert(!linkedLitBillboard.Create(linkedDevice, linkedLitDesc));

    particle::ParticleGpuBillboardRenderer linkedBillboard;
    assert(linkedBillboard.Create(linkedDevice, linkedDesc));
    assert(linkedDevice.shaderCreates == 2 && linkedDevice.buffers.size() == 1);
    assert(linkedDevice.buffers[0].byteSize == 32 && linkedDevice.buffers[0].usage == rhi::BufferUsageFlags::Uniform &&
           linkedDevice.buffers[0].memory == rhi::BufferMemory::Upload);
    assert(linkedDevice.layouts.size() == 1 && linkedDevice.layouts[0].entryCount == 6);
    assert(linkedDevice.layouts[0].entries[0].binding == 0 && linkedDevice.layouts[0].entries[1].binding == 1 &&
           linkedDevice.layouts[0].entries[2].binding == 2 && linkedDevice.layouts[0].entries[3].binding == 3 &&
           linkedDevice.layouts[0].entries[4].binding == 14 && linkedDevice.layouts[0].entries[5].binding == 15);
    assert(linkedDevice.bindGroups.size() == 1 && linkedDevice.bindGroups[0].bufferCount == 3 &&
           linkedDevice.bindGroups[0].textureCount == 3);
    assert(linkedDevice.bindGroups[0].buffers[0].binding == 0 && linkedDevice.bindGroups[0].buffers[1].binding == 1 &&
           linkedDevice.bindGroups[0].buffers[1].buffer == renderIndexBuffer &&
           linkedDevice.bindGroups[0].buffers[2].binding == 14);
    assert(linkedDevice.bindGroups[0].textures[0].binding == 2 && linkedDevice.bindGroups[0].textures[1].binding == 3 &&
           linkedDevice.bindGroups[0].textures[2].binding == 15 && !linkedDevice.bindGroups[0].textures[2].depthRead);
    assert(linkedTextureResolves == 2 && linkedDevice.writes == 1 && linkedDevice.writtenBytes[0].size() == 32);
    glm::vec4 packedColor{};
    float packedIntensity = 0.0f;
    std::memcpy(&packedColor, linkedDevice.writtenBytes[0].data(), sizeof(packedColor));
    std::memcpy(&packedIntensity, linkedDevice.writtenBytes[0].data() + 16, sizeof(packedIntensity));
    assert(packedColor == glm::vec4(0.2f, 0.4f, 0.6f, 0.8f) && packedIntensity == 3.5f);

    GraphicsTrace linkedGraphicsTrace;
    const rhi::GraphicsCommandEncoder linkedGraphicsEncoder(&linkedGraphicsTrace, &graphicsDispatch);
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(linkedDevice.writes == 1 && linkedDevice.groupCreates == 1 && linkedDevice.graphicsPipelineCreates == 1 &&
           linkedTextureResolves == 2);
    assert(linkedGraphicsTrace.constants.back().materialTint == (std::array<float, 4>{1.0f, 1.0f, 1.0f, 1.0f}));

    const rhi::BufferHandle sceneViewIndices{901, 1};
    const rhi::BufferHandle gameViewIndices{902, 1};
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view,
                                      sceneViewIndices));
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view,
                                      sceneViewIndices));
    assert(linkedDevice.groupCreates == 2 && linkedDevice.bindGroups.back().buffers[1].buffer == sceneViewIndices);
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view,
                                      gameViewIndices));
    assert(linkedDevice.groupCreates == 3 && linkedDevice.bindGroups.back().buffers[1].buffer == gameViewIndices);

    linkedDesc.material->SetFloat("intensity", 8.0f);
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(linkedDevice.writes == 2 && linkedDevice.groupCreates == 3 && linkedDevice.graphicsPipelineCreates == 1 &&
           linkedTextureResolves == 2);
    std::memcpy(&packedIntensity, linkedDevice.writtenBytes.back().data() + 16, sizeof(packedIntensity));
    assert(packedIntensity == 8.0f);

    linkedDesc.material->SetTextureGuid("albedo", "black");
    assert(linkedBillboard.RecordDraw(linkedGraphicsEncoder, firstTarget, forwardPass, indirectBuffer, view));
    assert(linkedDevice.writes == 3 && linkedDevice.groupCreates == 4 && linkedDevice.graphicsPipelineCreates == 1 &&
           linkedTextureResolves == 3);
    linkedDeletionQueue.Tick();
    linkedDeletionQueue.Tick();
    linkedDeletionQueue.Tick();
    assert(linkedDevice.groupReleases == 3 && linkedDevice.textureReleases == 1 && linkedDevice.samplerReleases == 1);
    linkedBillboard.Destroy();
    assert(linkedDevice.bufferReleases == 1 && linkedDevice.groupReleases == 4 && linkedDevice.textureReleases == 3 &&
           linkedDevice.samplerReleases == 3);

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
        meshDesc.fragmentShader = {meshFragmentShader.data(), meshFragmentShader.size()};
        meshDesc.pickingFragmentShader = {meshPickingShader.data(), meshPickingShader.size()};
        meshDesc.instances = instanceBuffer;
        meshDesc.renderIndices = renderIndexBuffer;
        meshDesc.mesh = mesh;
        meshDesc.meshVertices = meshDevice.CreateBuffer({3 * 5 * sizeof(glm::vec4), rhi::BufferUsageFlags::Storage});
        meshDesc.meshIndices = meshDevice.CreateBuffer({3 * sizeof(uint32_t), rhi::BufferUsageFlags::Storage});
        meshDesc.indexCount = 3;
        meshDesc.meshBufferKeepAlive = std::make_shared<int>(1);
        meshDesc.material = std::make_shared<InxMaterial>("particle-mesh-material");
        meshDesc.material->SetRenderQueue(2450);
        meshDesc.material->SetColor("baseColor", glm::vec4(0.2f, 0.4f, 0.8f, 0.75f));

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
        assert(meshDevice.layoutEntryCounts == std::vector<uint32_t>({4}) &&
               meshDevice.groupBufferCounts == std::vector<uint32_t>({4}));

        GraphicsTrace meshTrace;
        const rhi::GraphicsCommandEncoder::Dispatch meshGraphicsDispatch = {
            &GraphicsTrace::BindPipeline, &GraphicsTrace::BindGroup, &GraphicsTrace::PushConstants,
            &GraphicsTrace::Draw, &GraphicsTrace::DrawIndirect};
        const rhi::GraphicsCommandEncoder meshEncoder(&meshTrace, &meshGraphicsDispatch);
        assert(meshRenderer.RecordDraw(meshEncoder, firstTarget, forwardPass, indirectBuffer, view));
        assert(meshTrace.indirectBuffers == std::vector<rhi::BufferHandle>({indirectBuffer}));
        assert(meshDevice.graphicsPipelineDescs.size() == 1 &&
               meshDevice.graphicsPipelineDescs[0].raster.cullMode == rhi::CullMode::Back &&
               meshDevice.graphicsPipelineDescs[0].raster.frontFace == rhi::FrontFace::Clockwise);
        const std::array<float, 4> expectedMeshTint = {0.2f, 0.4f, 0.8f, 0.75f};
        assert(meshTrace.constants.size() == 1 && meshTrace.constants[0].materialTint == expectedMeshTint);

        auto pickingPass = forwardPass;
        pickingPass.target = ShaderCompileTarget::Picking;
        pickingPass.colorFormats = {rhi::PixelFormat::RG32UInt};
        pickingPass.samples = rhi::SampleCount::One;
        assert(meshRenderer.RecordPickingDraw(meshEncoder, {101, 1}, pickingPass, indirectBuffer, view,
                                              0x123456789abcdef0ull));
        assert(meshDevice.graphicsPipelineCreates == 2 && meshTrace.indirectBuffers.size() == 2);
        std::array<uint32_t, 4> encodedObjectId{};
        std::memcpy(encodedObjectId.data(), meshTrace.constants.back().materialTint.data(), sizeof(encodedObjectId));
        assert(encodedObjectId[0] == 0x9abcdef0u && encodedObjectId[1] == 0x12345678u);

        meshRenderer.Destroy();
        assert(!meshRenderer.IsValid() && meshDevice.bufferReleases == 0 && meshDevice.shaderReleases == 3 &&
               meshDevice.layoutReleases == 1 && meshDevice.groupReleases == 1 &&
               meshDevice.graphicsPipelineReleases == 2);

        {
            FakeDevice litMeshDevice;
            std::array<uint32_t, 4> meshForwardPlusFragment = {0x07230203};
            auto litMeshDesc = meshDesc;
            litMeshDesc.forwardPlusFragmentShader = {meshForwardPlusFragment.data(), meshForwardPlusFragment.size()};
            litMeshDesc.semantics.receiveSceneLighting = true;
            particle::ParticleGpuMeshRenderer litMeshRenderer;
            assert(litMeshRenderer.Create(litMeshDevice, litMeshDesc));

            auto forwardPlusPass = forwardPass;
            forwardPlusPass.target = ShaderCompileTarget::ForwardPlus;
            GraphicsTrace litMeshTrace;
            const rhi::GraphicsCommandEncoder litMeshEncoder(&litMeshTrace, &meshGraphicsDispatch);
            assert(!litMeshRenderer.RecordDraw(litMeshEncoder, firstTarget, forwardPlusPass, indirectBuffer, view));
            const particle::GpuParticleForwardPlusBindings lighting{{910, 1}, {911, 1}};
            assert(litMeshRenderer.RecordDraw(litMeshEncoder, firstTarget, forwardPlusPass, indirectBuffer, view, {},
                                              {}, true, lighting));
            assert(litMeshDevice.graphicsPipelineDescs.size() == 1);
            const auto &litMeshPipeline = litMeshDevice.graphicsPipelineDescs.front();
            assert(litMeshPipeline.bindingLayoutCount == 2 && litMeshPipeline.bindingLayouts[1] == lighting.layout);
            assert(litMeshTrace.groupSets == std::vector<uint32_t>({0, 1}) &&
                   litMeshTrace.groups.back() == lighting.group);
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
        auto registeredBillboard = std::make_shared<particle::ParticleGpuBillboardRenderer>();
        assert(registeredBillboard->Create(device, registryBillboardDesc));
        particle::ParticleGpuDrawRegistry registry;
        const uint64_t initialRevision = registry.Revision();
        particle::GpuParticleDrawEntry registryEntry;
        registryEntry.id = 77;
        registryEntry.ownerLayerMask = 1u << 6u;
        registryEntry.capacity = runtime.Capacity();
        registryEntry.instances = instanceBuffer;
        registryEntry.renderIndices = renderIndexBuffer;
        registryEntry.indirectArguments = indirectBuffer;
        registryEntry.bounds = {999, 1};
        registryEntry.renderer = registeredBillboard;
        assert(registry.Set(std::move(registryEntry)));
        assert(registry.Revision() == initialRevision + 1 && registry.Size() == 1);
        const auto visibleEntries = registry.Snapshot(3000, 3200);
        assert(visibleEntries.size() == 1 && visibleEntries[0].id == 77 &&
               visibleEntries[0].ownerLayerMask == 1u << 6u);
        assert(registry.Snapshot(0, 2999).empty());
        assert(registry.Remove(77) && !registry.Remove(77) && registry.Size() == 0);
    }

    runtime.Destroy();
    assert(!runtime.IsValid() && runtime.StateStride() == 0);
    if (device.pipelineReleases != 5 || device.groupReleases != 5 || device.layoutReleases != 3) {
        std::cerr << "release counts: pipelines=" << device.pipelineReleases << " groups=" << device.groupReleases
                  << " layouts=" << device.layoutReleases << '\n';
    }
    assert(device.pipelineReleases == 5 && device.groupReleases == 5 && device.layoutReleases == 3);
    assert(device.textureReleases == 4 && device.samplerReleases == 4);
    assert(device.shaderReleases == 9);
    assert(device.bufferReleases == 7);
    return 0;
}
