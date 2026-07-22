#include <function/renderer/lighting/CanonicalLightGpuBuffer.h>
#include <function/renderer/lighting/ForwardPlusLightGrid.h>
#include <function/scene/LightingData.h>

#include <cassert>
#include <cstdint>
#include <cstring>
#include <vector>

using infernux::CanonicalLightAffectsGeometry;
using infernux::CanonicalLightAffectsParticles;
using infernux::CanonicalLightData;
using infernux::CanonicalLightSnapshot;
using infernux::CanonicalLightType;
using infernux::lighting::BuildCanonicalLightUpload;
using infernux::lighting::CanonicalLightGpuHeader;

namespace
{

struct FakeDevice final : infernux::rhi::Device
{
    std::vector<infernux::rhi::BufferDesc> buffers;
    std::vector<infernux::rhi::BindingLayoutDesc> layouts;
    std::vector<infernux::rhi::BindGroupDesc> groups;
    std::vector<infernux::rhi::ComputePipelineDesc> pipelines;
    uint32_t next = 1;
    uint32_t releasedBuffers = 0;
    uint32_t releasedGroups = 0;

    infernux::rhi::BufferHandle CreateBuffer(const infernux::rhi::BufferDesc &desc) override
    {
        buffers.push_back(desc);
        return {next++, 1};
    }
    infernux::rhi::TextureHandle CreateTexture(const infernux::rhi::TextureDesc &) override
    {
        return {};
    }
    infernux::rhi::TextureViewHandle CreateTextureView(const infernux::rhi::TextureViewDesc &) override
    {
        return {};
    }
    infernux::rhi::SamplerHandle CreateSampler(const infernux::rhi::SamplerDesc &) override
    {
        return {};
    }
    infernux::rhi::ShaderModuleHandle CreateShaderModule(const infernux::rhi::ShaderModuleDesc &desc) override
    {
        assert(desc.spirv && desc.wordCount >= 5);
        return {next++, 1};
    }
    infernux::rhi::BindingLayoutHandle CreateBindingLayout(const infernux::rhi::BindingLayoutDesc &desc) override
    {
        layouts.push_back(desc);
        return {next++, 1};
    }
    infernux::rhi::BindGroupHandle CreateBindGroup(const infernux::rhi::BindGroupDesc &desc) override
    {
        groups.push_back(desc);
        return {next++, 1};
    }
    infernux::rhi::GraphicsPipelineHandle CreateGraphicsPipeline(const infernux::rhi::GraphicsPipelineDesc &) override
    {
        return {};
    }
    infernux::rhi::ComputePipelineHandle CreateComputePipeline(const infernux::rhi::ComputePipelineDesc &desc) override
    {
        pipelines.push_back(desc);
        return {next++, 1};
    }
    bool WriteBuffer(infernux::rhi::BufferHandle, uint64_t, const void *, uint64_t) override
    {
        return true;
    }
    void Release(infernux::rhi::BufferHandle handle) noexcept override
    {
        releasedBuffers += handle.IsValid();
    }
    void Release(infernux::rhi::TextureHandle) noexcept override
    {
    }
    void Release(infernux::rhi::TextureViewHandle) noexcept override
    {
    }
    void Release(infernux::rhi::SamplerHandle) noexcept override
    {
    }
    void Release(infernux::rhi::ShaderModuleHandle) noexcept override
    {
    }
    void Release(infernux::rhi::BindingLayoutHandle) noexcept override
    {
    }
    void Release(infernux::rhi::BindGroupHandle handle) noexcept override
    {
        releasedGroups += handle.IsValid();
    }
    void Release(infernux::rhi::GraphicsPipelineHandle) noexcept override
    {
    }
    void Release(infernux::rhi::ComputePipelineHandle) noexcept override
    {
    }
};

struct DispatchTrace
{
    infernux::lighting::ForwardPlusGridConstants constants{};
    uint32_t x = 0;
    uint32_t y = 0;
    uint32_t z = 0;

    static void BindPipeline(void *, infernux::rhi::ComputePipelineHandle)
    {
    }
    static void BindGroup(void *, infernux::rhi::ComputePipelineHandle, uint32_t, infernux::rhi::BindGroupHandle)
    {
    }
    static void PushConstants(void *context, infernux::rhi::ComputePipelineHandle, uint32_t bytes, const void *data)
    {
        auto &self = *static_cast<DispatchTrace *>(context);
        assert(bytes == sizeof(self.constants));
        std::memcpy(&self.constants, data, bytes);
    }
    static void Dispatch(void *context, uint32_t x, uint32_t y, uint32_t z)
    {
        auto &self = *static_cast<DispatchTrace *>(context);
        self.x = x;
        self.y = y;
        self.z = z;
    }
    static void DispatchIndirect(void *, infernux::rhi::BufferHandle, uint64_t)
    {
    }
};

} // namespace

int main()
{
    static_assert(sizeof(CanonicalLightData) == 80);
    static_assert(alignof(CanonicalLightData) == 16);

    CanonicalLightSnapshot snapshot;
    snapshot.Clear(41);

    CanonicalLightData directional{};
    directional.metadata = glm::uvec4(static_cast<uint32_t>(CanonicalLightType::Directional), 0xFFFFFFFFu, 2u,
                                      CanonicalLightAffectsGeometry | CanonicalLightAffectsParticles);
    snapshot.Add(directional);

    for (uint32_t index = 0; index < 1000; ++index) {
        CanonicalLightData local{};
        local.positionRange = glm::vec4(static_cast<float>(index), 0.0f, 0.0f, 12.0f);
        local.metadata = glm::uvec4(index % 2u == 0u ? static_cast<uint32_t>(CanonicalLightType::Point)
                                                     : static_cast<uint32_t>(CanonicalLightType::Spot),
                                    1u << (index % 31u), 0u, CanonicalLightAffectsGeometry);
        snapshot.Add(local);
    }

    assert(snapshot.generation == 41);
    assert(snapshot.directionalLights.size() == 1);
    assert(snapshot.localLights.size() == 1000);
    assert(snapshot.Size() == 1001);
    assert(snapshot.localLights[999].positionRange.x == 999.0f);

    const auto upload = BuildCanonicalLightUpload(snapshot);
    assert(upload.header.countsAndGeneration.x == 1);
    assert(upload.header.countsAndGeneration.y == 1000);
    assert(upload.header.countsAndGeneration.z == 41);
    assert(upload.bytes.size() == sizeof(CanonicalLightGpuHeader) + 1001 * sizeof(CanonicalLightData));
    CanonicalLightData packedDirectional{};
    std::memcpy(&packedDirectional, upload.bytes.data() + sizeof(CanonicalLightGpuHeader), sizeof(CanonicalLightData));
    assert(packedDirectional.metadata.w == (CanonicalLightAffectsGeometry | CanonicalLightAffectsParticles));

    snapshot.Clear(42);
    assert(snapshot.generation == 42);
    assert(snapshot.Size() == 0);
    assert(snapshot.directionalLights.capacity() >= 1);
    assert(snapshot.localLights.capacity() >= 1000);

    const auto gridConfig =
        infernux::lighting::BuildForwardPlusGridConfig(1920, 1080, 1000, CanonicalLightAffectsGeometry);
    assert(gridConfig.IsValid());
    assert(gridConfig.tileCountX == 120 && gridConfig.tileCountY == 68 && gridConfig.tileCount == 8160);
    assert(gridConfig.indexStride == 1000);
    assert(gridConfig.headerBytes == 8161ull * 16ull);
    assert(gridConfig.indexBytes == 8160ull * 1000ull * sizeof(uint32_t));

    FakeDevice device;
    std::array<uint32_t, 5> shaderWords{0x07230203u};
    infernux::lighting::ForwardPlusLightGrid grid;
    assert(grid.Initialize(device, 2, {shaderWords.data(), shaderWords.size()}));
    const infernux::rhi::BufferHandle canonicalBuffer{900, 1};
    assert(grid.PrepareFrame(0, 1920, 1080, 1000, CanonicalLightAffectsGeometry, canonicalBuffer));
    assert(grid.PrepareFrame(1, 1280, 720, 4, CanonicalLightAffectsParticles, canonicalBuffer));
    assert(device.layouts.size() == 2 && device.layouts[0].entryCount == 3 && device.layouts[1].entryCount == 3);
    assert(grid.ConsumerLayout().IsValid());
    assert(device.pipelines.size() == 1 && device.pipelines[0].pushConstantBytes == 112);
    assert(device.groups.size() == 4 && device.groups[0].bufferCount == 3 && device.groups[1].bufferCount == 3);
    assert(grid.Frame(0).headers != grid.Frame(1).headers && grid.Frame(0).indices != grid.Frame(1).indices);

    DispatchTrace trace;
    const infernux::rhi::ComputeCommandEncoder::DispatchTable dispatch = {
        &DispatchTrace::BindPipeline, &DispatchTrace::BindGroup, &DispatchTrace::PushConstants,
        &DispatchTrace::Dispatch, &DispatchTrace::DispatchIndirect};
    const infernux::rhi::ComputeCommandEncoder encoder(&trace, &dispatch);
    infernux::lighting::ForwardPlusGridConstants constants{};
    constants.viewportAndProjectionScale[2] = 1.0f;
    constants.viewportAndProjectionScale[3] = 1.0f;
    grid.Record(1, encoder, constants);
    assert(trace.x == 80 && trace.y == 45 && trace.z == 1);
    assert(trace.constants.gridAndLights[2] == 4 && trace.constants.gridAndLights[3] == 16);
    assert(trace.constants.domainAndStride[0] == CanonicalLightAffectsParticles);
    assert(trace.constants.domainAndStride[1] == 4);

    const std::string_view source = infernux::lighting::ForwardPlusLightGrid::ShaderSource();
    assert(source.find("local_size_x = 64") != std::string_view::npos);
    assert(source.find("metadata.w & pc.domain_stride.x") != std::string_view::npos);
    assert(source.find("MAX_LIGHTS") == std::string_view::npos);
    grid.Shutdown();
    assert(device.releasedBuffers == 4 && device.releasedGroups == 4);
    return 0;
}
