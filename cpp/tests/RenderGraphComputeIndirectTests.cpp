#include <function/renderer/vk/RenderGraph.h>
#include <function/renderer/vk/VkDeviceContext.h>
#include <function/renderer/vk/VkPipelineManager.h>

#include <SDL3/SDL.h>

#include <cstdint>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

namespace
{
using infernux::rhi::BindGroupHandle;
using infernux::rhi::ComputePipelineHandle;
using infernux::rhi::GraphicsPipelineHandle;
using infernux::vk::DeviceConfig;
using infernux::vk::PassBuilder;
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
         const std::filesystem::path &fragmentPath)
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
    if (!Require(!computeCode.empty() && !vertexCode.empty() && !fragmentCode.empty(),
                 "Failed to read generated SPIR-V test shaders"))
        return false;

    ResourceHandle indirectArguments;
    ResourceHandle discardedRewrite;
    ResourceHandle colorTarget;
    resources.graph.AddComputePass("BuildIndirectArguments", [&](PassBuilder &builder) {
        indirectArguments =
            builder.CreateBuffer("IndirectArguments", sizeof(VkDrawIndirectCommand),
                                 VK_BUFFER_USAGE_STORAGE_BUFFER_BIT | VK_BUFFER_USAGE_INDIRECT_BUFFER_BIT);
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

    resources.graph.AddPass("IndirectDraw", [&](PassBuilder &builder) {
        builder.ReadIndirectBuffer(indirectArguments);
        colorTarget = builder.CreateTexture("IndirectColor", 16, 16, VK_FORMAT_R8G8B8A8_UNORM);
        colorTarget = builder.WriteColor(colorTarget);
        builder.SetRenderArea(16, 16);
        builder.SetClearColor(0.0f, 0.0f, 0.0f, 1.0f);
        return [&](RenderContext &context) {
            vkCmdBeginQuery(context.GetCommandBuffer(), resources.queryPool, 0, 0);
            auto &encoder = context.GetGraphicsCommandEncoder();
            encoder.BindPipeline(resources.graphicsHandle);
            encoder.DrawIndirect(context.GetBufferHandle(indirectArguments), 0, 1, sizeof(VkDrawIndirectCommand));
            vkCmdEndQuery(context.GetCommandBuffer(), resources.queryPool, 0);
        };
    });
    resources.graph.SetOutput(colorTarget);

    if (!Require(indirectArguments.version == 1 && discardedRewrite.version == 2,
                 "Buffer writes did not publish monotonic resource versions"))
        return false;

    if (!Require(resources.graph.Compile(), "RenderGraph compilation failed"))
        return false;
    const auto executionNames = resources.graph.GetExecutionPassNames();
    if (!Require(executionNames.size() == 2 && executionNames[0] == "BuildIndirectArguments" &&
                     executionNames[1] == "IndirectDraw",
                 "Versioned culling kept an unrelated rewrite or dropped the compute-to-indirect dependency"))
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
    if (argc != 4) {
        std::cerr << "Expected compute, vertex, and fragment SPIR-V paths\n";
        return 2;
    }
    return Run(argv[1], argv[2], argv[3]) ? 0 : 1;
}
