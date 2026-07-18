#include <function/renderer/RendererList.h>
#include <function/renderer/particle/ParticleGpuBillboardRenderer.h>
#include <function/renderer/particle/ParticleGpuDrawRegistry.h>
#include <function/renderer/particle/ParticleGpuSystemManager.h>
#include <function/renderer/particle/ParticleRenderGraph.h>
#include <function/renderer/shader/ShaderReflection.h>
#include <function/renderer/vk/RenderGraph.h>
#include <function/renderer/vk/VkDeviceContext.h>
#include <function/renderer/vk/VkPipelineManager.h>

#include <SDL3/SDL.h>

#include <algorithm>
#include <array>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

namespace
{
using infernux::DrawCall;
using infernux::RenderDomain;
using infernux::RenderDomainBit;
using infernux::RendererList;
using infernux::RendererListPurpose;
using infernux::ShaderReflection;
using infernux::rhi::BindGroupHandle;
using infernux::rhi::BindingLayoutHandle;
using infernux::rhi::ComputePipelineHandle;
using infernux::rhi::ShaderModuleHandle;
using infernux::vk::DeviceConfig;
using infernux::vk::PassBuilder;
using infernux::vk::PassCullReason;
using infernux::vk::RenderContext;
using infernux::vk::RenderGraph;
using infernux::vk::ResourceHandle;
using infernux::vk::VkDeviceContext;
using infernux::vk::VkPipelineManager;

struct TestResources
{
    SDL_Window *window = nullptr;
    VkDeviceContext context;
    VkPipelineManager pipelines;
    RenderGraph graph;

    VkCommandPool commandPool = VK_NULL_HANDLE;
    VkQueryPool queryPool = VK_NULL_HANDLE;
    BindGroupHandle computeGroup;
    BindingLayoutHandle computeBindingLayout;
    ComputePipelineHandle computeHandle;
    ShaderModuleHandle computeShader;

    ~TestResources()
    {
        if (context.IsValid()) {
            context.WaitIdle();
            auto &rhi = context.GetRhiDevice();
            rhi.Release(computeHandle);
            rhi.Release(computeGroup);
            rhi.Release(computeBindingLayout);
            rhi.Release(computeShader);
            graph.Destroy();

            const VkDevice device = context.GetDevice();
            if (queryPool != VK_NULL_HANDLE)
                vkDestroyQueryPool(device, queryPool, nullptr);
            if (commandPool != VK_NULL_HANDLE)
                vkDestroyCommandPool(device, commandPool, nullptr);

            pipelines.Destroy();
            context.Destroy();
        }
        if (window)
            SDL_DestroyWindow(window);
        SDL_Quit();
    }
};

std::vector<uint32_t> ReadSpirv(const std::filesystem::path &path)
{
    std::ifstream stream(path, std::ios::binary | std::ios::ate);
    if (!stream)
        return {};

    const auto byteCount = stream.tellg();
    if (byteCount <= 0 || (byteCount % static_cast<std::streamoff>(sizeof(uint32_t))) != 0)
        return {};

    std::vector<uint32_t> code(static_cast<size_t>(byteCount) / sizeof(uint32_t));
    stream.seekg(0);
    stream.read(reinterpret_cast<char *>(code.data()), byteCount);
    return stream ? code : std::vector<uint32_t>{};
}

bool Require(bool condition, const char *message)
{
    if (!condition)
        std::cerr << "FAILED: " << message << '\n';
    return condition;
}

bool Run(const std::filesystem::path &computePath, const std::filesystem::path &vertexPath,
         const std::filesystem::path &fragmentPath, const std::filesystem::path &reflectionPath,
         const std::filesystem::path &particleComputePath, const std::filesystem::path &particleVertexPath,
         const std::filesystem::path &particleFragmentPath)
{
    TestResources resources;
    if (!Require(SDL_Init(SDL_INIT_VIDEO), SDL_GetError()))
        return false;

    resources.window =
        SDL_CreateWindow("Infernux RenderGraph Compute Indirect Test", 64, 64, SDL_WINDOW_VULKAN | SDL_WINDOW_HIDDEN);
    if (!Require(resources.window != nullptr, SDL_GetError()))
        return false;

    DeviceConfig deviceConfig;
    deviceConfig.appName = "Infernux RenderGraph Compute Indirect Test";
    deviceConfig.enableValidationLayers = true;
    if (!Require(resources.context.Initialize(resources.window, deviceConfig), "Vulkan device initialization failed"))
        return false;

    const VkDevice device = resources.context.GetDevice();
    resources.pipelines.Initialize(device);
    resources.graph.Initialize(&resources.context, &resources.pipelines);

    const auto computeCode = ReadSpirv(computePath);
    const auto vertexCode = ReadSpirv(vertexPath);
    const auto fragmentCode = ReadSpirv(fragmentPath);
    const auto reflectionCode = ReadSpirv(reflectionPath);
    const auto particleComputeCode = ReadSpirv(particleComputePath);
    const auto particleVertexCode = ReadSpirv(particleVertexPath);
    const auto particleFragmentCode = ReadSpirv(particleFragmentPath);
    if (!Require(!computeCode.empty() && !vertexCode.empty() && !fragmentCode.empty() && !reflectionCode.empty() &&
                     !particleComputeCode.empty() && !particleVertexCode.empty() && !particleFragmentCode.empty(),
                 "Failed to read generated SPIR-V test shaders"))
        return false;

    infernux::FrameDeletionQueue particleDeletionQueue;
    particleDeletionQueue.Initialize(2);
    infernux::particle::ParticleGpuDrawRegistry particleDrawRegistry;
    infernux::particle::ParticleGpuSystemManager particleSystems;
    if (!Require(particleSystems.Initialize(resources.context, resources.pipelines, particleDeletionQueue,
                                            particleDrawRegistry),
                 "GPU particle system manager initialization failed"))
        return false;

    infernux::particle::GpuParticleEmitterProgram managedProgram;
    managedProgram.id = 91;
    managedProgram.artifactRevision = 1;
    managedProgram.stableId = "managed-emitter";
    managedProgram.capacity = 32;
    managedProgram.stateStride = 16;
    for (auto &kernel : managedProgram.kernels)
        kernel = particleComputeCode;
    managedProgram.billboardVertexShader = particleVertexCode;
    managedProgram.billboardFragmentShader = particleFragmentCode;
    infernux::particle::GpuParticleOutputProgram primaryOutput;
    primaryOutput.id = 911;
    primaryOutput.stableId = "managed-primary";
    primaryOutput.material.renderQueue = 3050;
    primaryOutput.material.blendEnabled = false;
    managedProgram.outputs.push_back(primaryOutput);
    std::string managedError;
    if (!Require(particleSystems.CreateOrReplace(managedProgram, &managedError), managedError.c_str()) ||
        !Require(particleSystems.Size() == 1 && particleSystems.Contains(managedProgram.id) &&
                     particleSystems.ActiveArtifactRevision(managedProgram.id) == 1 && particleDrawRegistry.Size() == 1,
                 "GPU particle system was not published atomically"))
        return false;

    auto invalidManagedProgram = managedProgram;
    invalidManagedProgram.artifactRevision = 2;
    invalidManagedProgram.billboardFragmentShader.clear();
    if (!Require(!particleSystems.CreateOrReplace(invalidManagedProgram, &managedError) &&
                     particleSystems.ActiveArtifactRevision(managedProgram.id) == 1 && particleDrawRegistry.Size() == 1,
                 "Invalid GPU particle replacement disturbed the active revision"))
        return false;

    managedProgram.artifactRevision = 2;
    auto secondaryOutput = primaryOutput;
    secondaryOutput.id = 912;
    secondaryOutput.stableId = "managed-secondary";
    secondaryOutput.material.renderQueue = 3075;
    managedProgram.outputs.push_back(secondaryOutput);
    const bool managedReplacement = particleSystems.CreateOrReplace(managedProgram, &managedError);
    if (!managedReplacement || particleSystems.ActiveArtifactRevision(managedProgram.id) != 2 ||
        particleDeletionQueue.PendingCount() != 1) {
        std::cerr << "GPU particle replacement detail: success=" << managedReplacement
                  << " revision=" << particleSystems.ActiveArtifactRevision(managedProgram.id)
                  << " pending=" << particleDeletionQueue.PendingCount() << " error=" << managedError << '\n';
    }
    if (!Require(managedReplacement && particleSystems.ActiveArtifactRevision(managedProgram.id) == 2 &&
                     particleSystems.ActiveOutputCount(managedProgram.id) == 2 && particleDrawRegistry.Size() == 2 &&
                     particleDeletionQueue.PendingCount() == 1,
                 "Valid GPU particle hot replacement was not published with deferred retirement"))
        return false;

    auto duplicateOutputProgram = managedProgram;
    duplicateOutputProgram.artifactRevision = 3;
    duplicateOutputProgram.outputs[1].stableId = duplicateOutputProgram.outputs[0].stableId;
    if (!Require(!particleSystems.CreateOrReplace(duplicateOutputProgram, &managedError) &&
                     particleSystems.ActiveArtifactRevision(managedProgram.id) == 2 &&
                     particleSystems.ActiveOutputCount(managedProgram.id) == 2 && particleDrawRegistry.Size() == 2 &&
                     particleDeletionQueue.PendingCount() == 1,
                 "Duplicate GPU particle output identity disturbed the active revision"))
        return false;

    auto companionProgram = managedProgram;
    companionProgram.id = 92;
    companionProgram.stableId = "managed-companion";
    companionProgram.outputs.resize(1);
    companionProgram.outputs[0].id = 921;
    companionProgram.outputs[0].stableId = "companion-primary";
    companionProgram.outputs[0].material.renderQueue = 3200;
    if (!Require(particleSystems.CreateOrReplaceBatch({companionProgram}, &managedError) &&
                     particleSystems.Size() == 2 && particleSystems.Contains(companionProgram.id) &&
                     particleSystems.ActiveOutputCount(companionProgram.id) == 1 && particleDrawRegistry.Size() == 3 &&
                     particleDeletionQueue.PendingCount() == 2,
                 "GPU particle batch did not publish a valid companion emitter"))
        return false;

    auto invalidCompanion = companionProgram;
    invalidCompanion.artifactRevision = 3;
    invalidCompanion.billboardFragmentShader.clear();
    auto candidateManagedProgram = managedProgram;
    candidateManagedProgram.artifactRevision = 3;
    if (!Require(!particleSystems.CreateOrReplaceBatch({candidateManagedProgram, invalidCompanion}, &managedError) &&
                     particleSystems.ActiveArtifactRevision(managedProgram.id) == 2 &&
                     particleSystems.ActiveArtifactRevision(companionProgram.id) == 2 &&
                     particleDrawRegistry.Size() == 3 && particleDeletionQueue.PendingCount() == 2,
                 "Failed GPU particle batch disturbed its last-known-good emitters"))
        return false;

    managedProgram.artifactRevision = 3;
    if (!Require(particleSystems.ApplyBatch({managedProgram}, {companionProgram.id}, &managedError) &&
                     particleSystems.Size() == 1 && !particleSystems.Contains(companionProgram.id) &&
                     particleSystems.ActiveArtifactRevision(managedProgram.id) == 3 &&
                     particleSystems.ActiveOutputCount(managedProgram.id) == 2 && particleDrawRegistry.Size() == 2 &&
                     particleDeletionQueue.PendingCount() == 3,
                 "GPU particle replacement and removal were not published atomically"))
        return false;

    infernux::particle::GpuParticleFrameRequest managedFrame;
    managedFrame.frameIndex = 43;
    managedFrame.spawnCount = 4;
    managedFrame.systemSeed = 23;
    managedFrame.simulationStep = 1;
    managedFrame.deltaTime = 1.0f / 60.0f;
    infernux::particle::GpuParticleTransforms managedTransforms;
    managedTransforms.emitterToWorld[0] = managedTransforms.emitterToWorld[5] = managedTransforms.emitterToWorld[10] =
        managedTransforms.emitterToWorld[15] = 1.0f;
    managedTransforms.worldToEmitter = managedTransforms.emitterToWorld;
    managedTransforms.simulationToWorld = managedTransforms.emitterToWorld;
    managedTransforms.worldToSimulation = managedTransforms.emitterToWorld;
    if (!Require(particleSystems.Reset(managedProgram.id) && !particleSystems.Reset(999999),
                 "GPU particle manager reset lookup is incorrect"))
        return false;
    if (!Require(particleSystems.BeginFrame(managedProgram.id, managedFrame, managedTransforms),
                 "GPU particle manager rejected a valid frame request"))
        return false;
    if (!Require(particleSystems.Reset(managedProgram.id) &&
                     particleSystems.BeginFrame(managedProgram.id, managedFrame, managedTransforms),
                 "GPU particle reset did not cancel and replace a pending frame request"))
        return false;
    const auto managedEntries = particleDrawRegistry.Snapshot(3000, 3100);
    if (!Require(managedEntries.size() == 2 && managedEntries[0].renderer->RenderQueue() == 3050 &&
                     managedEntries[1].renderer->RenderQueue() == 3075 &&
                     managedEntries[0].instances == managedEntries[1].instances &&
                     managedEntries[0].indirectArguments == managedEntries[1].indirectArguments,
                 "GPU particle outputs did not share one simulated stream across ordered draw queues"))
        return false;
    const auto managedEntry = managedEntries.front();

    ShaderReflection computeReflection;
    if (!Require(computeReflection.Reflect(computeCode, VK_SHADER_STAGE_COMPUTE_BIT),
                 "Generated compute SPIR-V reflection failed"))
        return false;
    const auto &storageBuffers = computeReflection.GetStorageBuffers();
    const auto reflectedBindings = computeReflection.GetDescriptorSetLayoutBindings(0);
    if (!Require(storageBuffers.size() == 1 && storageBuffers[0].set == 0 && storageBuffers[0].binding == 0 &&
                     storageBuffers[0].stageFlags == VK_SHADER_STAGE_COMPUTE_BIT,
                 "Compute storage buffer was not reflected with its set, binding, and stage"))
        return false;
    if (!Require(reflectedBindings.size() == 1 && reflectedBindings[0].binding == 0 &&
                     reflectedBindings[0].descriptorType == VK_DESCRIPTOR_TYPE_STORAGE_BUFFER &&
                     reflectedBindings[0].stageFlags == VK_SHADER_STAGE_COMPUTE_BIT,
                 "Storage reflection did not produce a Vulkan descriptor layout binding"))
        return false;
    if (!Require(computeReflection.GetUsedDescriptorSets() == std::vector<uint32_t>{0},
                 "Storage resources were omitted from reflected descriptor sets"))
        return false;

    ShaderReflection storageReflection;
    if (!Require(storageReflection.Reflect(reflectionCode, VK_SHADER_STAGE_COMPUTE_BIT),
                 "Storage resource reflection fixture failed"))
        return false;
    const auto &reflectedBuffers = storageReflection.GetStorageBuffers();
    const auto &reflectedImages = storageReflection.GetStorageImages();
    const auto &reflectedSampled = storageReflection.GetSampledImages();
    if (!Require(reflectedBuffers.size() == 2 && reflectedImages.size() == 2 && reflectedSampled.size() == 2,
                 "Storage reflection omitted a buffer, image, texture, or sampler"))
        return false;
    const auto setOneBindings = storageReflection.GetDescriptorSetLayoutBindings(1);
    const auto setTwoBindings = storageReflection.GetDescriptorSetLayoutBindings(2);
    const auto setThreeBindings = storageReflection.GetDescriptorSetLayoutBindings(3);
    if (!Require(setOneBindings.size() == 2 && setOneBindings[0].binding == 2 &&
                     setOneBindings[0].descriptorType == VK_DESCRIPTOR_TYPE_STORAGE_IMAGE &&
                     setOneBindings[1].binding == 3 &&
                     setOneBindings[1].descriptorType == VK_DESCRIPTOR_TYPE_STORAGE_IMAGE,
                 "Storage image descriptor bindings are incorrect"))
        return false;
    if (!Require(setTwoBindings.size() == 2 && setTwoBindings[0].descriptorType == VK_DESCRIPTOR_TYPE_SAMPLED_IMAGE &&
                     setTwoBindings[1].descriptorType == VK_DESCRIPTOR_TYPE_SAMPLER,
                 "Separate texture and sampler descriptor types are incorrect"))
        return false;
    if (!Require(setThreeBindings.size() == 2 &&
                     setThreeBindings[0].descriptorType == VK_DESCRIPTOR_TYPE_STORAGE_BUFFER &&
                     setThreeBindings[1].descriptorType == VK_DESCRIPTOR_TYPE_STORAGE_BUFFER,
                 "Storage buffer descriptor bindings are incorrect"))
        return false;
    const auto inputBuffer = std::find_if(reflectedBuffers.begin(), reflectedBuffers.end(),
                                          [](const auto &info) { return info.binding == 0; });
    const auto outputBuffer = std::find_if(reflectedBuffers.begin(), reflectedBuffers.end(),
                                           [](const auto &info) { return info.binding == 1; });
    const auto inputImage = std::find_if(reflectedImages.begin(), reflectedImages.end(),
                                         [](const auto &info) { return info.binding == 2; });
    const auto outputImage = std::find_if(reflectedImages.begin(), reflectedImages.end(),
                                          [](const auto &info) { return info.binding == 3; });
    if (!Require(inputBuffer != reflectedBuffers.end() && inputBuffer->readOnly &&
                     outputBuffer != reflectedBuffers.end() && outputBuffer->writeOnly &&
                     inputImage != reflectedImages.end() && inputImage->readOnly &&
                     outputImage != reflectedImages.end() && outputImage->writeOnly,
                 "Storage access qualifiers were not preserved by reflection"))
        return false;
    if (!Require(storageReflection.GetUsedDescriptorSets() == std::vector<uint32_t>({1, 2, 3}),
                 "Storage reflection descriptor set discovery is incomplete"))
        return false;

    auto &rhi = resources.context.GetRhiDevice();
    infernux::particle::GpuEmitterDesc particleDesc;
    particleDesc.capacity = 32;
    particleDesc.stateStride = 16;
    for (auto &kernel : particleDesc.kernels)
        kernel = {particleComputeCode.data(), particleComputeCode.size()};
    infernux::particle::ParticleGpuRuntime particleRuntime;
    if (!Require(particleRuntime.Create(rhi, particleDesc), "Particle GPU runtime creation failed"))
        return false;
    infernux::particle::ParticleRenderGraph particleGraph;
    infernux::particle::GpuParticleFrameRequest particleFrame;
    particleFrame.frameIndex = 42;
    particleFrame.spawnCount = 3;
    particleFrame.systemSeed = 17;
    particleFrame.simulationStep = 9;
    particleFrame.deltaTime = 1.0f / 60.0f;

    ResourceHandle indirectArguments;
    ResourceHandle discardedRewrite;
    ResourceHandle copiedIndirectArguments;
    ResourceHandle managedInstances;
    ResourceHandle managedIndirectArguments;
    ResourceHandle colorTarget;
    infernux::particle::ParticleGpuBillboardRenderer billboardRenderer;
    infernux::particle::GpuBillboardViewConstants billboardView;
    infernux::MaterialPassPipelineDescriptor billboardPass;
    infernux::rhi::RenderTargetLayoutHandle billboardTargetLayout;
    bool billboardRecorded = false;
    RendererList emptyRendererList;
    std::vector<DrawCall> populatedDrawCalls(1);
    RendererList populatedRendererList = RendererList::Borrow(populatedDrawCalls, RendererListPurpose::CameraVisible,
                                                              RenderDomainBit(RenderDomain::SceneGeometry));
    const ResourceHandle emptyRendererListHandle =
        resources.graph.ImportRendererList("EmptyRendererList", &emptyRendererList);
    const ResourceHandle populatedRendererListHandle =
        resources.graph.ImportRendererList("PopulatedRendererList", &populatedRendererList);
    uint32_t emptyRendererCallbackCount = 0;
    uint32_t populatedRendererCallbackCount = 0;
    resources.graph.AddComputePass("BuildIndirectArguments", [&](PassBuilder &builder) {
        indirectArguments =
            builder.CreateBuffer("IndirectArguments", sizeof(VkDrawIndirectCommand),
                                 VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_INDIRECT_BUFFER_BIT |
                                     VK_BUFFER_USAGE_TRANSFER_SRC_BIT);
        indirectArguments = builder.WriteStorageBuffer(indirectArguments);
        return [&](RenderContext &context) {
            auto &encoder = context.GetComputeCommandEncoder();
            encoder.BindPipeline(resources.computeHandle);
            encoder.BindGroup(resources.computeHandle, 0, resources.computeGroup);
            encoder.Dispatch(1, 1, 1);
        };
    });

    resources.graph.AddComputePass("DiscardedRewrite", [&](PassBuilder &builder) {
        discardedRewrite = builder.WriteStorageBuffer(indirectArguments);
        return [](RenderContext &) {};
    });

    resources.graph.AddTransferPass("CopyIndirectArguments", [&](PassBuilder &builder) {
        builder.TransferRead(indirectArguments);
        copiedIndirectArguments =
            builder.CreateBuffer("CopiedIndirectArguments", sizeof(VkDrawIndirectCommand),
                                 VK_BUFFER_USAGE_TRANSFER_DST_BIT | VK_BUFFER_USAGE_INDIRECT_BUFFER_BIT);
        copiedIndirectArguments = builder.TransferWrite(copiedIndirectArguments);
        return [&](RenderContext &context) {
            context.GetTransferCommandEncoder().CopyBuffer(context.GetBufferHandle(indirectArguments),
                                                           context.GetBufferHandle(copiedIndirectArguments),
                                                           {0, 0, sizeof(VkDrawIndirectCommand)});
        };
    });

    if (!Require(particleGraph.Attach(resources.graph, particleRuntime, "Particle/TestEmitter"),
                 "Particle RenderGraph attachment failed"))
        return false;
    if (!Require(particleGraph.BeginFrame(particleFrame), "Particle frame request was rejected"))
        return false;
    const auto particleOutputs = particleGraph.Outputs();

    resources.graph.AddPass("IndirectDraw", [&](PassBuilder &builder) {
        builder.ReadIndirectBuffer(copiedIndirectArguments);
        builder.ReadStorageBuffer(particleOutputs.instances, infernux::rhi::PipelineStage::VertexShader);
        builder.ReadIndirectBuffer(particleOutputs.indirectArguments);
        managedInstances = builder.ImportBuffer("ManagedParticleInstances", managedEntry.instances,
                                                static_cast<uint64_t>(managedEntry.capacity) *
                                                    infernux::particle::ParticleGpuRuntime::RenderInstanceStride);
        managedIndirectArguments = builder.ImportBuffer("ManagedParticleIndirect", managedEntry.indirectArguments, 16);
        resources.graph.SetResourceInitialState(managedInstances, infernux::rhi::TextureLayout::Undefined,
                                                infernux::rhi::Access::ShaderWrite,
                                                infernux::rhi::PipelineStage::ComputeShader);
        resources.graph.SetResourceInitialState(managedIndirectArguments, infernux::rhi::TextureLayout::Undefined,
                                                infernux::rhi::Access::ShaderWrite,
                                                infernux::rhi::PipelineStage::ComputeShader);
        builder.ReadStorageBuffer(managedInstances, infernux::rhi::PipelineStage::VertexShader);
        builder.ReadIndirectBuffer(managedIndirectArguments);
        colorTarget = builder.CreateTexture("IndirectColor", 16, 16, VK_FORMAT_R8G8B8A8_UNORM);
        colorTarget = builder.WriteColor(colorTarget);
        builder.SetRenderArea(16, 16);
        builder.SetClearColor(0.0f, 0.0f, 0.0f, 1.0f);
        auto managedRenderer = managedEntry.renderer;
        return [&, managedRenderer](RenderContext &context) {
            vkCmdBeginQuery(context.GetCommandBuffer(), resources.queryPool, 0, 0);
            auto &encoder = context.GetGraphicsCommandEncoder();
            billboardRecorded =
                billboardRenderer.RecordDraw(encoder, billboardTargetLayout, billboardPass,
                                             context.GetBufferHandle(particleOutputs.indirectArguments), billboardView);
            billboardRecorded =
                managedRenderer->RecordDraw(encoder, billboardTargetLayout, billboardPass,
                                            context.GetBufferHandle(managedIndirectArguments), billboardView) &&
                billboardRecorded;
            encoder.DrawIndirect(context.GetBufferHandle(copiedIndirectArguments));
            vkCmdEndQuery(context.GetCommandBuffer(), resources.queryPool, 0);
        };
    });
    resources.graph.AddComputePass("SkipEmptyRendererList", [&](PassBuilder &builder) {
        builder.ReadRendererList(emptyRendererListHandle);
        builder.SkipCallbackWhenRendererListsEmpty();
        builder.SetSideEffect();
        return [&](RenderContext &context) {
            ++emptyRendererCallbackCount;
            (void)context;
        };
    });
    resources.graph.AddComputePass("RunPopulatedRendererList", [&](PassBuilder &builder) {
        builder.ReadRendererList(populatedRendererListHandle);
        builder.SkipCallbackWhenRendererListsEmpty();
        builder.SetSideEffect();
        return [&](RenderContext &context) {
            if (context.GetRendererList(populatedRendererListHandle) == &populatedRendererList)
                ++populatedRendererCallbackCount;
        };
    });
    resources.graph.SetOutput(colorTarget);

    if (!Require(indirectArguments.version == 1 && discardedRewrite.version == 2 &&
                     copiedIndirectArguments.version == 1,
                 "Buffer writes did not publish monotonic resource versions"))
        return false;

    if (!Require(resources.graph.Compile(), "RenderGraph compilation failed"))
        return false;
    const auto executionNames = resources.graph.GetExecutionPassNames();
    if (!Require(executionNames.size() == 10 && executionNames[0] == "BuildIndirectArguments" &&
                     executionNames[1] == "CopyIndirectArguments" &&
                     executionNames[2] == "Particle/TestEmitter/Bootstrap" &&
                     executionNames[3] == "Particle/TestEmitter/Init" &&
                     executionNames[4] == "Particle/TestEmitter/Update" &&
                     executionNames[5] == "Particle/TestEmitter/RenderReset" &&
                     executionNames[6] == "Particle/TestEmitter/Rendering" && executionNames[7] == "IndirectDraw" &&
                     executionNames[8] == "SkipEmptyRendererList" && executionNames[9] == "RunPopulatedRendererList",
                 "Versioned culling broke the compute-to-transfer-to-indirect dependency"))
        return false;
    if (!Require(resources.graph.ResolveRendererList(emptyRendererListHandle) == &emptyRendererList &&
                     resources.graph.ResolveRendererList(populatedRendererListHandle) == &populatedRendererList,
                 "Imported renderer lists did not preserve their stable host objects"))
        return false;

    infernux::rhi::Device &deviceApi = rhi;
    const std::array<uint32_t, 4> initialUpload = {1, 2, 3, 4};
    infernux::rhi::BufferDesc uploadDesc;
    uploadDesc.byteSize = sizeof(initialUpload);
    uploadDesc.usage = infernux::rhi::BufferUsageFlags::Uniform;
    uploadDesc.memory = infernux::rhi::BufferMemory::Upload;
    uploadDesc.initialData = initialUpload.data();
    uploadDesc.initialDataBytes = sizeof(initialUpload);
    const auto uploadBuffer = deviceApi.CreateBuffer(uploadDesc);
    const uint32_t replacement = 9;
    if (!Require(uploadBuffer.IsValid() &&
                     deviceApi.WriteBuffer(uploadBuffer, sizeof(uint32_t), &replacement, sizeof(replacement)),
                 "RHI upload buffer creation or update failed"))
        return false;

    infernux::rhi::BufferDesc residentDesc;
    residentDesc.byteSize = 4096;
    residentDesc.usage = infernux::rhi::BufferUsageFlags::Storage | infernux::rhi::BufferUsageFlags::Indirect |
                         infernux::rhi::BufferUsageFlags::TransferDestination;
    const auto residentBuffer = deviceApi.CreateBuffer(residentDesc);
    if (!Require(residentBuffer.IsValid(), "RHI device-local buffer creation failed"))
        return false;
    deviceApi.Release(residentBuffer);
    deviceApi.Release(uploadBuffer);

    resources.computeShader = rhi.CreateShaderModule({computeCode.data(), computeCode.size()});
    infernux::rhi::BindingLayoutDesc layoutDesc;
    layoutDesc.entries[0] = {0, infernux::rhi::BindingType::StorageBuffer, infernux::rhi::ShaderStage::Compute, 1};
    layoutDesc.entryCount = 1;
    resources.computeBindingLayout = rhi.CreateBindingLayout(layoutDesc);
    if (!Require(resources.computeShader.IsValid() && resources.computeBindingLayout.IsValid(),
                 "RHI compute shader or binding layout creation failed"))
        return false;

    infernux::rhi::ComputePipelineDesc computeDesc;
    computeDesc.computeShader = resources.computeShader;
    computeDesc.bindingLayouts[0] = resources.computeBindingLayout;
    computeDesc.bindingLayoutCount = 1;
    resources.computeHandle = rhi.CreateComputePipeline(computeDesc);
    if (!Require(resources.computeHandle.IsValid(), "RHI compute pipeline creation failed"))
        return false;

    infernux::rhi::BindGroupDesc groupDesc;
    groupDesc.layout = resources.computeBindingLayout;
    groupDesc.buffers[0].binding = 0;
    groupDesc.buffers[0].type = infernux::rhi::BindingType::StorageBuffer;
    groupDesc.buffers[0].buffer = resources.graph.ResolveRhiBuffer(indirectArguments);
    groupDesc.buffers[0].byteSize = sizeof(VkDrawIndirectCommand);
    groupDesc.bufferCount = 1;
    resources.computeGroup = rhi.CreateBindGroup(groupDesc);
    if (!Require(resources.computeGroup.IsValid(), "RHI compute bind group creation failed"))
        return false;

    infernux::particle::GpuBillboardRendererDesc billboardDesc;
    billboardDesc.vertexShader = {particleVertexCode.data(), particleVertexCode.size()};
    billboardDesc.fragmentShader = {particleFragmentCode.data(), particleFragmentCode.size()};
    billboardDesc.instances = particleRuntime.InstanceBuffer();
    billboardDesc.material.blendEnabled = false;
    billboardTargetLayout = resources.graph.GetPassRenderTargetLayout("IndirectDraw");
    billboardPass.colorFormats = {infernux::rhi::PixelFormat::RGBA8UNorm};
    billboardView.viewProjection[0] = 1.0f;
    billboardView.viewProjection[5] = 1.0f;
    billboardView.viewProjection[10] = 1.0f;
    billboardView.viewProjection[15] = 1.0f;
    billboardView.cameraRight[0] = 1.0f;
    billboardView.cameraUp[1] = 1.0f;
    if (!Require(resources.computeGroup.IsValid() && resources.computeHandle.IsValid() &&
                     billboardRenderer.Create(rhi, billboardDesc),
                 "Typed RHI particle billboard creation failed"))
        return false;

    rhi.Release(resources.computeShader);
    resources.computeShader = {};

    VkQueryPoolCreateInfo queryInfo{};
    queryInfo.sType = VK_STRUCTURE_TYPE_QUERY_POOL_CREATE_INFO;
    queryInfo.queryType = VK_QUERY_TYPE_OCCLUSION;
    queryInfo.queryCount = 1;
    if (!Require(vkCreateQueryPool(device, &queryInfo, nullptr, &resources.queryPool) == VK_SUCCESS,
                 "Occlusion query creation failed"))
        return false;

    VkCommandPoolCreateInfo commandPoolInfo{};
    commandPoolInfo.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    commandPoolInfo.queueFamilyIndex = resources.context.GetQueueIndices().graphicsFamily.value();
    commandPoolInfo.flags = VK_COMMAND_POOL_CREATE_TRANSIENT_BIT;
    if (!Require(vkCreateCommandPool(device, &commandPoolInfo, nullptr, &resources.commandPool) == VK_SUCCESS,
                 "Command pool creation failed"))
        return false;

    VkCommandBuffer commandBuffer = VK_NULL_HANDLE;
    VkCommandBufferAllocateInfo commandInfo{};
    commandInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    commandInfo.commandPool = resources.commandPool;
    commandInfo.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    commandInfo.commandBufferCount = 1;
    if (!Require(vkAllocateCommandBuffers(device, &commandInfo, &commandBuffer) == VK_SUCCESS,
                 "Command buffer allocation failed"))
        return false;

    VkCommandBufferBeginInfo beginInfo{};
    beginInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    beginInfo.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    if (!Require(vkBeginCommandBuffer(commandBuffer, &beginInfo) == VK_SUCCESS, "Command buffer begin failed"))
        return false;
    vkCmdResetQueryPool(commandBuffer, resources.queryPool, 0, 1);
    particleSystems.Execute(commandBuffer);
    resources.graph.Execute(commandBuffer);
    if (!Require(emptyRendererCallbackCount == 0 && populatedRendererCallbackCount == 1,
                 "Renderer-list callback culling did not distinguish empty and populated lists"))
        return false;
    if (!Require(billboardRecorded, "Particle billboard renderer did not record its indirect draw"))
        return false;
    if (!Require(!particleGraph.HasPendingFrame() && particleGraph.LastConsumedFrame() == 42,
                 "Particle graph did not consume exactly one armed frame"))
        return false;
    if (!Require(!particleGraph.BeginFrame(particleFrame), "Particle graph accepted the same engine frame twice"))
        return false;
    if (!Require(vkEndCommandBuffer(commandBuffer) == VK_SUCCESS, "Command buffer end failed"))
        return false;

    VkFence fence = VK_NULL_HANDLE;
    VkFenceCreateInfo fenceInfo{};
    fenceInfo.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    if (!Require(vkCreateFence(device, &fenceInfo, nullptr, &fence) == VK_SUCCESS, "Fence creation failed"))
        return false;
    VkSubmitInfo submitInfo{};
    submitInfo.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    submitInfo.commandBufferCount = 1;
    submitInfo.pCommandBuffers = &commandBuffer;
    const VkResult submitResult = vkQueueSubmit(resources.context.GetGraphicsQueue(), 1, &submitInfo, fence);
    const VkResult waitResult =
        submitResult == VK_SUCCESS ? vkWaitForFences(device, 1, &fence, VK_TRUE, UINT64_MAX) : submitResult;
    vkDestroyFence(device, fence, nullptr);
    if (!Require(submitResult == VK_SUCCESS && waitResult == VK_SUCCESS, "GPU submission failed"))
        return false;

    uint64_t passedSamples = 0;
    const VkResult queryResult =
        vkGetQueryPoolResults(device, resources.queryPool, 0, 1, sizeof(passedSamples), &passedSamples,
                              sizeof(passedSamples), VK_QUERY_RESULT_64_BIT | VK_QUERY_RESULT_WAIT_BIT);
    if (!Require(queryResult == VK_SUCCESS, "Occlusion query readback failed"))
        return false;
    if (!Require(passedSamples > 0, "Compute-generated indirect draw produced no visible samples"))
        return false;

    if (!Require(!particleSystems.BeginFrame(managedProgram.id, managedFrame, managedTransforms),
                 "GPU particle manager accepted the same engine frame twice"))
        return false;
    if (!Require(particleSystems.Remove(managedProgram.id) && particleSystems.Size() == 0 &&
                     particleDrawRegistry.Size() == 0 && particleDeletionQueue.PendingCount() == 4,
                 "GPU particle manager removal did not retire graph resources"))
        return false;
    particleSystems.Shutdown();
    particleDeletionQueue.FlushAll();

    billboardRenderer.Destroy();
    resources.graph.Destroy();
    particleRuntime.Destroy();

    RenderGraph rootGraph;
    rootGraph.Initialize(&resources.context, &resources.pipelines);
    auto recordRootGraph = [&] {
        rootGraph.AddComputePass("Unreachable", [](PassBuilder &) { return [](RenderContext &) {}; });
        rootGraph.AddComputePass("ExplicitSideEffect", [](PassBuilder &builder) {
            builder.SetSideEffect();
            return [](RenderContext &) {};
        });
        rootGraph.AddComputePass("ExternalWrite", [](PassBuilder &builder) {
            auto external = builder.ImportBuffer("ExternalBuffer", VK_NULL_HANDLE, 16);
            builder.WriteStorageBuffer(external);
            return [](RenderContext &) {};
        });
    };
    recordRootGraph();
    if (!Require(rootGraph.Compile(), "Side-effect root graph failed to compile"))
        return false;
    if (!Require(rootGraph.GetStructuralCacheHitCount() == 0 && rootGraph.GetStructuralCacheMissCount() == 1,
                 "First structural compilation did not populate the cache"))
        return false;
    const auto rootExecution = rootGraph.GetExecutionPassNames();
    if (!Require(rootExecution == std::vector<std::string>{"ExplicitSideEffect", "ExternalWrite"},
                 "Side-effect or external-write culling roots are incorrect"))
        return false;
    const auto rootInfos = rootGraph.GetPassCompileInfos();
    if (!Require(rootInfos.size() == 3 && rootInfos[0].culled && rootInfos[0].reason == PassCullReason::Unreachable &&
                     !rootInfos[1].culled && rootInfos[1].reason == PassCullReason::SideEffect &&
                     !rootInfos[2].culled && rootInfos[2].reason == PassCullReason::ExternalWrite,
                 "Pass compile report did not preserve culling reasons"))
        return false;

    rootGraph.Reset();
    recordRootGraph();
    if (!Require(rootGraph.Compile(), "Repeated side-effect root graph failed to compile"))
        return false;
    if (!Require(rootGraph.GetStructuralCacheHitCount() == 1 && rootGraph.GetStructuralCacheMissCount() == 1 &&
                     rootGraph.GetExecutionPassNames() == rootExecution,
                 "Identical graph rebuild did not reuse structural dependency analysis"))
        return false;

    rootGraph.Reset();
    recordRootGraph();
    rootGraph.AddTransferPass("NewSideEffect", [](PassBuilder &builder) {
        builder.SetSideEffect();
        return [](RenderContext &) {};
    });
    if (!Require(rootGraph.Compile(), "Changed root graph failed to compile"))
        return false;
    const auto changedRootExecution = rootGraph.GetExecutionPassNames();
    if (!Require(rootGraph.GetStructuralCacheHitCount() == 1 && rootGraph.GetStructuralCacheMissCount() == 2 &&
                     changedRootExecution.size() == 3 && changedRootExecution.back() == "NewSideEffect",
                 "Changed graph structure incorrectly reused a cached dependency analysis"))
        return false;

    RenderGraph typedResourceGraph;
    typedResourceGraph.Initialize(&resources.context, &resources.pipelines);
    auto registeredBuffer = typedResourceGraph.RegisterTransientBuffer(
        "RegisteredStorage", 64, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_TRANSFER_DST_BIT);
    ResourceHandle writtenBuffer;
    typedResourceGraph.AddComputePass("WriteRegisteredBuffer", [&](PassBuilder &builder) {
        writtenBuffer = builder.WriteStorageBuffer(registeredBuffer);
        return [](RenderContext &) {};
    });
    typedResourceGraph.SetOutput(writtenBuffer);
    if (!Require(registeredBuffer.IsValid() && writtenBuffer.version == 1 && typedResourceGraph.Compile(),
                 "Pre-registered transient buffer did not participate in graph compilation"))
        return false;

    RenderGraph bufferAliasGraph;
    bufferAliasGraph.Initialize(&resources.context, &resources.pipelines);
    constexpr VkDeviceSize aliasBufferBytes = 64 * 1024;
    const auto firstAliasBuffer = bufferAliasGraph.RegisterTransientBuffer("FirstAliasBuffer", aliasBufferBytes,
                                                                           VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
    const auto aliasBridge =
        bufferAliasGraph.RegisterTransientBuffer("AliasBridge", 16, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
    const auto secondAliasBuffer = bufferAliasGraph.RegisterTransientBuffer("SecondAliasBuffer", aliasBufferBytes,
                                                                            VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
    ResourceHandle firstAliasVersion;
    ResourceHandle aliasBridgeVersion;
    ResourceHandle secondAliasVersion;
    bufferAliasGraph.AddComputePass("WriteFirstAlias", [&](PassBuilder &builder) {
        firstAliasVersion = builder.WriteStorageBuffer(firstAliasBuffer);
        return [](RenderContext &) {};
    });
    bufferAliasGraph.AddComputePass("BridgeAliasLifetimes", [&](PassBuilder &builder) {
        builder.ReadStorageBuffer(firstAliasVersion);
        aliasBridgeVersion = builder.WriteStorageBuffer(aliasBridge);
        return [](RenderContext &) {};
    });
    bufferAliasGraph.AddComputePass("WriteSecondAlias", [&](PassBuilder &builder) {
        builder.ReadStorageBuffer(aliasBridgeVersion);
        secondAliasVersion = builder.WriteStorageBuffer(secondAliasBuffer);
        return [](RenderContext &) {};
    });
    bufferAliasGraph.SetOutput(secondAliasVersion);
    if (!Require(bufferAliasGraph.Compile(), "Transient buffer alias graph failed to compile"))
        return false;
    if (!Require(bufferAliasGraph.ResolveBuffer(firstAliasVersion) != VK_NULL_HANDLE &&
                     bufferAliasGraph.ResolveBuffer(secondAliasVersion) != VK_NULL_HANDLE &&
                     bufferAliasGraph.ResolveBuffer(firstAliasVersion) !=
                         bufferAliasGraph.ResolveBuffer(secondAliasVersion),
                 "Aliased transient buffers did not retain distinct Vulkan handles"))
        return false;
    if (!Require(bufferAliasGraph.GetTransientAllocationCount() == 2,
                 "Non-overlapping transient buffers did not reuse one allocation"))
        return false;

    infernux::FrameDeletionQueue graphDeletionQueue;
    graphDeletionQueue.Initialize(2);
    RenderGraph deferredReleaseGraph;
    deferredReleaseGraph.Initialize(&resources.context, &resources.pipelines, &graphDeletionQueue);
    const auto deferredBuffer =
        deferredReleaseGraph.RegisterTransientBuffer("DeferredBuffer", 256, VK_BUFFER_USAGE_STORAGE_BUFFER_BIT);
    ResourceHandle deferredBufferVersion;
    deferredReleaseGraph.AddComputePass("WriteDeferredBuffer", [&](PassBuilder &builder) {
        deferredBufferVersion = builder.WriteStorageBuffer(deferredBuffer);
        return [](RenderContext &) {};
    });
    deferredReleaseGraph.SetOutput(deferredBufferVersion);
    if (!Require(deferredReleaseGraph.Compile(), "Deferred-release graph failed to compile"))
        return false;
    deferredReleaseGraph.Reset();
    if (!Require(graphDeletionQueue.PendingCount() == 1,
                 "RenderGraph reset did not defer transient resource destruction"))
        return false;
    deferredReleaseGraph.Destroy();
    graphDeletionQueue.FlushAll();

    RenderGraph presentGraph;
    presentGraph.Initialize(&resources.context, &resources.pipelines);
    auto presentTexture = presentGraph.RegisterTransientTexture("PresentTexture", 4, 4, VK_FORMAT_R8G8B8A8_UNORM);
    ResourceHandle presentVersion;
    presentGraph.AddPass("ProducePresentTexture", [&](PassBuilder &builder) {
        presentVersion = builder.WriteColor(presentTexture);
        builder.SetRenderArea(4, 4);
        return [](RenderContext &) {};
    });
    presentGraph.AddPresentPass("Present", [&](PassBuilder &builder) {
        builder.PresentRead(presentVersion);
        return [](RenderContext &) {};
    });
    if (!Require(presentGraph.Compile(), "Present graph failed to compile"))
        return false;
    if (!Require(presentGraph.GetExecutionPassNames() == std::vector<std::string>{"ProducePresentTexture", "Present"},
                 "Present access did not retain its producer dependency"))
        return false;
    const auto presentInfos = presentGraph.GetPassCompileInfos();
    if (!Require(presentInfos.size() == 2 && presentInfos[1].type == infernux::vk::PassType::Present &&
                     presentInfos[1].reason == PassCullReason::SideEffect,
                 "Present pass did not report its typed side-effect root"))
        return false;

    // Two logical versions share one physical image. A final pass cannot read
    // both sides of an overwrite without a copy, because it would need to run
    // both before and after that overwrite. Reject the cycle instead of
    // silently falling back to declaration order.
    RenderGraph invalidVersionGraph;
    invalidVersionGraph.Initialize(&resources.context, &resources.pipelines);
    ResourceHandle firstVersion;
    ResourceHandle secondVersion;
    ResourceHandle invalidOutput;
    invalidVersionGraph.AddPass("FirstVersion", [&](PassBuilder &builder) {
        firstVersion = builder.CreateTexture("VersionedColor", 4, 4, VK_FORMAT_R8G8B8A8_UNORM);
        firstVersion = builder.WriteColor(firstVersion);
        builder.SetRenderArea(4, 4);
        return [](RenderContext &) {};
    });
    invalidVersionGraph.AddPass("OverwriteVersion", [&](PassBuilder &builder) {
        secondVersion = builder.WriteColor(firstVersion);
        builder.SetRenderArea(4, 4);
        return [](RenderContext &) {};
    });
    invalidVersionGraph.AddPass("ConsumeBothVersions", [&](PassBuilder &builder) {
        builder.Read(firstVersion);
        builder.Read(secondVersion);
        invalidOutput = builder.CreateTexture("InvalidOutput", 4, 4, VK_FORMAT_R8G8B8A8_UNORM);
        invalidOutput = builder.WriteColor(invalidOutput);
        builder.SetRenderArea(4, 4);
        return [](RenderContext &) {};
    });
    invalidVersionGraph.SetOutput(invalidOutput);
    return Require(!invalidVersionGraph.Compile(), "An unschedulable multi-version alias was not rejected");
}
} // namespace

int main(int argc, char **argv)
{
    if (argc != 8) {
        std::cerr << "Expected compute, vertex, fragment, reflection, and particle SPIR-V paths\n";
        return 2;
    }
    return Run(argv[1], argv[2], argv[3], argv[4], argv[5], argv[6], argv[7]) ? 0 : 1;
}
