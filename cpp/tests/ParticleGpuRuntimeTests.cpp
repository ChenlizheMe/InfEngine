#include <function/renderer/FrameDeletionQueue.h>
#include <function/renderer/particle/ParticleGpuBillboardRenderer.h>
#include <function/renderer/particle/ParticleGpuDrawRegistry.h>
#include <function/renderer/particle/ParticleGpuRuntime.h>
#include <function/renderer/particle/ParticleGpuSorter.h>
#include <function/resources/InxMaterial/InxMaterial.h>

#include <array>
#include <cassert>
#include <cstdint>
#include <cstring>
#include <vector>

namespace
{

using namespace infernux;

struct FakeDevice final : rhi::Device
{
    std::vector<rhi::BufferDesc> buffers;
    uint32_t shaderCreates = 0;
    std::vector<rhi::BindingLayoutDesc> layouts;
    std::vector<rhi::BindGroupDesc> bindGroups;
    std::vector<uint32_t> layoutEntryCounts;
    std::vector<uint32_t> groupBufferCounts;
    std::vector<uint32_t> groupTextureCounts;
    std::vector<rhi::GraphicsPipelineDesc> graphicsPipelineDescs;
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
        assert(desc.computeShader.IsValid() && desc.bindingLayoutCount == 1 &&
               (desc.pushConstantBytes == 32 || desc.pushConstantBytes == 80));
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
    std::vector<particle::GpuParticlePushConstants> constants;
    std::vector<uint32_t> dispatches;

    static void BindPipeline(void *context, rhi::ComputePipelineHandle pipeline)
    {
        static_cast<CommandTrace *>(context)->pipelines.push_back(pipeline);
    }
    static void BindGroup(void *context, rhi::ComputePipelineHandle, uint32_t setIndex, rhi::BindGroupHandle group)
    {
        assert(setIndex == 0);
        static_cast<CommandTrace *>(context)->groups.push_back(group);
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

struct GraphicsTrace
{
    std::vector<rhi::GraphicsPipelineHandle> pipelines;
    std::vector<rhi::BindGroupHandle> groups;
    std::vector<particle::GpuBillboardViewConstants> constants;
    std::vector<rhi::BufferHandle> indirectBuffers;

    static void BindPipeline(void *context, rhi::GraphicsPipelineHandle pipeline)
    {
        static_cast<GraphicsTrace *>(context)->pipelines.push_back(pipeline);
    }
    static void BindGroup(void *context, rhi::GraphicsPipelineHandle, uint32_t setIndex, rhi::BindGroupHandle group)
    {
        assert(setIndex == 0);
        static_cast<GraphicsTrace *>(context)->groups.push_back(group);
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

struct SortTrace
{
    std::vector<rhi::ComputePipelineHandle> pipelines;
    std::vector<rhi::BindGroupHandle> groups;
    std::vector<particle::GpuParticleSortConstants> constants;
    std::vector<uint32_t> dispatches;

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
    static void DispatchIndirect(void *, rhi::BufferHandle, uint64_t)
    {
    }
};

} // namespace

int main()
{
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
    assert(device.buffers[5].memory == rhi::BufferMemory::Upload);
    assert(device.buffers[6].byteSize == 4000 && device.buffers[6].usage == rhi::BufferUsageFlags::Storage);
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

    FakeDevice sortDevice;
    std::array<std::array<uint32_t, 5>, 4> sortWords{};
    for (auto &shader : sortWords)
        shader[0] = 0x07230203u;
    particle::GpuParticleSorterDesc sorterDesc;
    sorterDesc.capacity = runtime.Capacity();
    sorterDesc.instances = runtime.InstanceBuffer();
    sorterDesc.indirectArguments = runtime.IndirectBuffer();
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
    assert(sorter.IsValid() && sorter.Capacity() == 1000 && sorter.BlockCount() == 4 &&
           sorter.SortedIndices() == sorter.IndexBuffer(0) && gameViewSorter.IsValid() &&
           gameViewSorter.SortedIndices() != sorter.SortedIndices());
    assert(sortDevice.buffers.size() == 14 && sortDevice.buffers[0].byteSize == 4000 &&
           sortDevice.buffers[4].byteSize == 256 && sortDevice.buffers[5].byteSize == 256 &&
           sortDevice.buffers[6].byteSize == 64);
    assert(sortDevice.layouts.size() == 2 && sortDevice.layouts[0].entryCount == 9 &&
           sortDevice.bindGroups.size() == 4 && sortDevice.bindGroups[0].bufferCount == 9 &&
           sortDevice.bindGroups[1].bufferCount == 9);
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
    assert(sortTrace.dispatches == std::vector<uint32_t>({4, 4, 1, 4, 4}));
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
           gameSortTrace.constants[0].descending == 0 && gameSortTrace.groups[0] != sortTrace.groups[0]);
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
                                                  std::make_shared<uint32_t>(identity)};
    };
    billboardDesc.textureVersionResolver = [](const std::string &textureGuid) {
        return textureGuid == "normal" ? uint64_t{2} : uint64_t{1};
    };
    billboardDesc.deletionQueue = &deletionQueue;

    particle::ParticleGpuBillboardRenderer billboard;
    assert(billboard.Create(device, billboardDesc));
    assert(billboard.IsValid() && billboard.RenderQueue() == 3100 && billboard.InstanceBuffer() == instanceBuffer);
    assert(device.layoutEntryCounts == std::vector<uint32_t>({6, 2}));
    assert(device.groupBufferCounts == std::vector<uint32_t>({6, 1}));
    assert(device.groupTextureCounts == std::vector<uint32_t>({0, 1}));
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

    auto linkedArtifact = std::make_shared<ShaderProgramArtifact>();
    linkedArtifact->key = {{"Tests/ParticleSprite", "Tests/ParticleSurface"}, 7};
    linkedArtifact->domain = ShaderProgramDomain::ParticleSprite;
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
                                                  std::make_shared<uint32_t>(identity)};
    };
    linkedDesc.textureVersionResolver = [](const std::string &guid) {
        return guid == "black" ? uint64_t{2} : uint64_t{1};
    };
    linkedDesc.deletionQueue = &linkedDeletionQueue;

    particle::ParticleGpuBillboardRenderer linkedBillboard;
    assert(linkedBillboard.Create(linkedDevice, linkedDesc));
    assert(linkedDevice.shaderCreates == 2 && linkedDevice.buffers.size() == 1);
    assert(linkedDevice.buffers[0].byteSize == 32 && linkedDevice.buffers[0].usage == rhi::BufferUsageFlags::Uniform &&
           linkedDevice.buffers[0].memory == rhi::BufferMemory::Upload);
    assert(linkedDevice.layouts.size() == 1 && linkedDevice.layouts[0].entryCount == 5);
    assert(linkedDevice.layouts[0].entries[0].binding == 0 && linkedDevice.layouts[0].entries[1].binding == 1 &&
           linkedDevice.layouts[0].entries[2].binding == 2 && linkedDevice.layouts[0].entries[3].binding == 3 &&
           linkedDevice.layouts[0].entries[4].binding == 14);
    assert(linkedDevice.bindGroups.size() == 1 && linkedDevice.bindGroups[0].bufferCount == 3 &&
           linkedDevice.bindGroups[0].textureCount == 2);
    assert(linkedDevice.bindGroups[0].buffers[0].binding == 0 && linkedDevice.bindGroups[0].buffers[1].binding == 1 &&
           linkedDevice.bindGroups[0].buffers[1].buffer == renderIndexBuffer &&
           linkedDevice.bindGroups[0].buffers[2].binding == 14);
    assert(linkedDevice.bindGroups[0].textures[0].binding == 2 && linkedDevice.bindGroups[0].textures[1].binding == 3);
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

    auto registeredBillboard = std::make_shared<particle::ParticleGpuBillboardRenderer>();
    assert(registeredBillboard->Create(device, billboardDesc));
    particle::ParticleGpuDrawRegistry registry;
    const uint64_t initialRevision = registry.Revision();
    assert(registry.Set(
        {77, runtime.Capacity(), instanceBuffer, renderIndexBuffer, indirectBuffer, registeredBillboard, nullptr, {}}));
    assert(registry.Revision() == initialRevision + 1 && registry.Size() == 1);
    const auto visibleEntries = registry.Snapshot(3000, 3200);
    assert(visibleEntries.size() == 1 && visibleEntries[0].id == 77);
    assert(registry.Snapshot(0, 2999).empty());
    assert(registry.Remove(77) && !registry.Remove(77) && registry.Size() == 0);
    registeredBillboard.reset();

    runtime.Destroy();
    assert(!runtime.IsValid() && runtime.StateStride() == 0);
    assert(device.pipelineReleases == 5 && device.groupReleases == 5 && device.layoutReleases == 3);
    assert(device.textureReleases == 4 && device.samplerReleases == 4);
    assert(device.shaderReleases == 9);
    assert(device.bufferReleases == 6);
    return 0;
}
