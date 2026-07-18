#include <function/renderer/shader/ShaderReflection.h>
#include <function/renderer/vk/RenderGraph.h>
#include <function/renderer/vk/VkDeviceContext.h>
#include <function/renderer/vk/VkPipelineManager.h>

#include <SDL3/SDL.h>

#include <algorithm>
#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

namespace
{
using infernux::ShaderReflection;
using infernux::rhi::BindGroupHandle;
using infernux::rhi::ComputePipelineHandle;
using infernux::rhi::GraphicsPipelineHandle;
using infernux::vk::DeviceConfig;
using infernux::vk::PassBuilder;
using infernux::vk::PassCullReason;
using infernux::vk::PipelineConfig;
using infernux::vk::PipelineResult;
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
    VkDescriptorPool descriptorPool = VK_NULL_HANDLE;
    VkDescriptorSetLayout computeSetLayout = VK_NULL_HANDLE;
    VkPipelineLayout computeLayout = VK_NULL_HANDLE;
    VkPipeline computePipeline = VK_NULL_HANDLE;
    VkQueryPool queryPool = VK_NULL_HANDLE;
    PipelineResult graphicsPipeline;

    BindGroupHandle computeGroup;
    ComputePipelineHandle computeHandle;
    GraphicsPipelineHandle graphicsHandle;

    ~TestResources()
    {
        if (context.IsValid()) {
            context.WaitIdle();
            auto &rhi = context.GetRhiDevice();
            rhi.Release(graphicsHandle);
            rhi.Release(computeHandle);
            rhi.Release(computeGroup);

            graph.Destroy();
            pipelines.DestroyPipelineResult(graphicsPipeline);

            const VkDevice device = context.GetDevice();
            if (queryPool != VK_NULL_HANDLE)
                vkDestroyQueryPool(device, queryPool, nullptr);
            if (computePipeline != VK_NULL_HANDLE)
                vkDestroyPipeline(device, computePipeline, nullptr);
            if (computeLayout != VK_NULL_HANDLE)
                pipelines.DestroyPipelineLayout(computeLayout);
            if (descriptorPool != VK_NULL_HANDLE)
                vkDestroyDescriptorPool(device, descriptorPool, nullptr);
            if (computeSetLayout != VK_NULL_HANDLE)
                pipelines.DestroyDescriptorSetLayout(computeSetLayout);
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
         const std::filesystem::path &fragmentPath, const std::filesystem::path &reflectionPath)
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
    if (!Require(!computeCode.empty() && !vertexCode.empty() && !fragmentCode.empty() && !reflectionCode.empty(),
                 "Failed to read generated SPIR-V test shaders"))
        return false;

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

    ResourceHandle indirectArguments;
    ResourceHandle discardedRewrite;
    ResourceHandle copiedIndirectArguments;
    ResourceHandle colorTarget;
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

    resources.graph.AddPass("IndirectDraw", [&](PassBuilder &builder) {
        builder.ReadIndirectBuffer(copiedIndirectArguments);
        colorTarget = builder.CreateTexture("IndirectColor", 16, 16, VK_FORMAT_R8G8B8A8_UNORM);
        colorTarget = builder.WriteColor(colorTarget);
        builder.SetRenderArea(16, 16);
        builder.SetClearColor(0.0f, 0.0f, 0.0f, 1.0f);
        return [&](RenderContext &context) {
            vkCmdBeginQuery(context.GetCommandBuffer(), resources.queryPool, 0, 0);
            auto &encoder = context.GetGraphicsCommandEncoder();
            encoder.BindPipeline(resources.graphicsHandle);
            encoder.DrawIndirect(context.GetBufferHandle(copiedIndirectArguments), 0, 1, sizeof(VkDrawIndirectCommand));
            vkCmdEndQuery(context.GetCommandBuffer(), resources.queryPool, 0);
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
    if (!Require(executionNames.size() == 3 && executionNames[0] == "BuildIndirectArguments" &&
                     executionNames[1] == "CopyIndirectArguments" && executionNames[2] == "IndirectDraw",
                 "Versioned culling broke the compute-to-transfer-to-indirect dependency"))
        return false;

    VkDescriptorSetLayoutBinding storageBinding{};
    storageBinding.binding = 0;
    storageBinding.descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    storageBinding.descriptorCount = 1;
    storageBinding.stageFlags = VK_SHADER_STAGE_COMPUTE_BIT;
    resources.computeSetLayout = resources.pipelines.CreateDescriptorSetLayout({storageBinding});
    if (!Require(resources.computeSetLayout != VK_NULL_HANDLE, "Compute descriptor layout creation failed"))
        return false;

    resources.computeLayout = resources.pipelines.CreatePipelineLayout({resources.computeSetLayout});
    if (!Require(resources.computeLayout != VK_NULL_HANDLE, "Compute pipeline layout creation failed"))
        return false;

    const VkShaderModule computeModule = resources.pipelines.CreateShaderModule(computeCode);
    if (!Require(computeModule != VK_NULL_HANDLE, "Compute shader module creation failed"))
        return false;
    VkPipelineShaderStageCreateInfo computeStage{};
    computeStage.sType = VK_STRUCTURE_TYPE_PIPELINE_SHADER_STAGE_CREATE_INFO;
    computeStage.stage = VK_SHADER_STAGE_COMPUTE_BIT;
    computeStage.module = computeModule;
    computeStage.pName = "main";
    VkComputePipelineCreateInfo computePipelineInfo{};
    computePipelineInfo.sType = VK_STRUCTURE_TYPE_COMPUTE_PIPELINE_CREATE_INFO;
    computePipelineInfo.stage = computeStage;
    computePipelineInfo.layout = resources.computeLayout;
    const VkResult computeResult =
        vkCreateComputePipelines(device, VK_NULL_HANDLE, 1, &computePipelineInfo, nullptr, &resources.computePipeline);
    resources.pipelines.DestroyShaderModule(computeModule);
    if (!Require(computeResult == VK_SUCCESS, "Compute pipeline creation failed"))
        return false;

    VkDescriptorPoolSize poolSize{};
    poolSize.type = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    poolSize.descriptorCount = 1;
    VkDescriptorPoolCreateInfo poolInfo{};
    poolInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_POOL_CREATE_INFO;
    poolInfo.maxSets = 1;
    poolInfo.poolSizeCount = 1;
    poolInfo.pPoolSizes = &poolSize;
    if (!Require(vkCreateDescriptorPool(device, &poolInfo, nullptr, &resources.descriptorPool) == VK_SUCCESS,
                 "Descriptor pool creation failed"))
        return false;

    VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
    VkDescriptorSetAllocateInfo setInfo{};
    setInfo.sType = VK_STRUCTURE_TYPE_DESCRIPTOR_SET_ALLOCATE_INFO;
    setInfo.descriptorPool = resources.descriptorPool;
    setInfo.descriptorSetCount = 1;
    setInfo.pSetLayouts = &resources.computeSetLayout;
    if (!Require(vkAllocateDescriptorSets(device, &setInfo, &descriptorSet) == VK_SUCCESS,
                 "Descriptor set allocation failed"))
        return false;

    VkDescriptorBufferInfo argumentInfo{};
    argumentInfo.buffer = resources.graph.ResolveBuffer(indirectArguments);
    argumentInfo.offset = 0;
    argumentInfo.range = sizeof(VkDrawIndirectCommand);
    if (!Require(argumentInfo.buffer != VK_NULL_HANDLE, "RenderGraph did not allocate the indirect buffer"))
        return false;
    VkWriteDescriptorSet write{};
    write.sType = VK_STRUCTURE_TYPE_WRITE_DESCRIPTOR_SET;
    write.dstSet = descriptorSet;
    write.dstBinding = 0;
    write.descriptorCount = 1;
    write.descriptorType = VK_DESCRIPTOR_TYPE_STORAGE_BUFFER;
    write.pBufferInfo = &argumentInfo;
    vkUpdateDescriptorSets(device, 1, &write, 0, nullptr);

    PipelineConfig graphicsConfig;
    graphicsConfig.vertexShaderCode = vertexCode;
    graphicsConfig.fragmentShaderCode = fragmentCode;
    graphicsConfig.renderPass = resources.graph.GetPassRenderPass("IndirectDraw");
    graphicsConfig.extent = {16, 16};
    graphicsConfig.depthTestEnable = false;
    graphicsConfig.depthWriteEnable = false;
    graphicsConfig.cullMode = VK_CULL_MODE_NONE;
    resources.graphicsPipeline = resources.pipelines.CreateGraphicsPipeline(graphicsConfig);
    if (!Require(resources.graphicsPipeline.pipeline != VK_NULL_HANDLE, "Graphics pipeline creation failed"))
        return false;

    auto &rhi = resources.context.GetRhiDevice();
    resources.computeGroup = rhi.RegisterBindGroup(descriptorSet);
    resources.computeHandle = rhi.RegisterComputePipeline(resources.computePipeline, resources.computeLayout);
    resources.graphicsHandle =
        rhi.RegisterGraphicsPipeline(resources.graphicsPipeline.pipeline, resources.graphicsPipeline.layout);
    if (!Require(resources.computeGroup.IsValid() && resources.computeHandle.IsValid() &&
                     resources.graphicsHandle.IsValid(),
                 "Typed RHI registration failed"))
        return false;

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
    resources.graph.Execute(commandBuffer);
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
    if (argc != 5) {
        std::cerr << "Expected compute, vertex, fragment, and reflection SPIR-V paths\n";
        return 2;
    }
    return Run(argv[1], argv[2], argv[3], argv[4]) ? 0 : 1;
}
