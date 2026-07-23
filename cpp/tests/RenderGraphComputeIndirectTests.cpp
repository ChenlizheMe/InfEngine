#include <function/renderer/RendererList.h>
#include <function/renderer/particle/ParticleGpuBillboardRenderer.h>
#include <function/renderer/particle/ParticleGpuBounds.h>
#include <function/renderer/particle/ParticleGpuCuller.h>
#include <function/renderer/particle/ParticleGpuDrawRegistry.h>
#include <function/renderer/particle/ParticleGpuMigrator.h>
#include <function/renderer/particle/ParticleGpuSystemManager.h>
#include <function/renderer/particle/ParticleRenderGraph.h>
#include <function/renderer/shader/ShaderReflection.h>
#include <function/renderer/vk/RenderGraph.h>
#include <function/renderer/vk/VkDeviceContext.h>
#include <function/renderer/vk/VkPipelineManager.h>
#include <function/renderer/vk/VkResourceManager.h>
#include <function/renderer/vk/VulkanRhiDevice.h>
#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
#include <function/resources/InxMaterial/InxMaterial.h>

#include <SDL3/SDL.h>

#include <algorithm>
#include <array>
#include <cmath>
#include <cstdint>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>
#include <vk_mem_alloc.h>

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
    infernux::vk::VkResourceManager resources;
    VkPipelineManager pipelines;
    RenderGraph graph;

    VkCommandPool commandPool = VK_NULL_HANDLE;
    VkQueryPool queryPool = VK_NULL_HANDLE;
    VkSampler sampledTextureSampler = VK_NULL_HANDLE;
    BindGroupHandle computeGroup;
    BindGroupHandle sampledTextureGroup;
    BindingLayoutHandle computeBindingLayout;
    BindingLayoutHandle sampledTextureBindingLayout;
    ComputePipelineHandle computeHandle;
    ShaderModuleHandle computeShader;
    infernux::rhi::SamplerHandle sampledTextureSamplerHandle;

    ~TestResources()
    {
        if (context.IsValid()) {
            context.WaitIdle();
            auto &rhi = context.GetRhiDevice();
            rhi.Release(computeHandle);
            rhi.Release(computeGroup);
            rhi.Release(sampledTextureGroup);
            rhi.Release(computeBindingLayout);
            rhi.Release(sampledTextureBindingLayout);
            rhi.Release(computeShader);
            rhi.Release(sampledTextureSamplerHandle);
            graph.Destroy();

            const VkDevice device = context.GetDevice();
            if (sampledTextureSampler != VK_NULL_HANDLE)
                vkDestroySampler(device, sampledTextureSampler, nullptr);
            if (queryPool != VK_NULL_HANDLE)
                vkDestroyQueryPool(device, queryPool, nullptr);
            if (commandPool != VK_NULL_HANDLE)
                vkDestroyCommandPool(device, commandPool, nullptr);

            pipelines.Destroy();
            resources.Destroy();
            context.Destroy();
        }
        if (window)
            SDL_DestroyWindow(window);
        SDL_Quit();
    }
};

struct BufferReadback
{
    VmaAllocator allocator = VK_NULL_HANDLE;
    VkBuffer buffer = VK_NULL_HANDLE;
    VmaAllocation allocation = VK_NULL_HANDLE;
    void *mapped = nullptr;
    VkDeviceSize byteSize = 0;

    ~BufferReadback()
    {
        if (allocator != VK_NULL_HANDLE && buffer != VK_NULL_HANDLE)
            vmaDestroyBuffer(allocator, buffer, allocation);
    }

    [[nodiscard]] bool Create(VmaAllocator sourceAllocator, VkDeviceSize size)
    {
        allocator = sourceAllocator;
        byteSize = size;
        VkBufferCreateInfo bufferInfo{};
        bufferInfo.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
        bufferInfo.size = size;
        bufferInfo.usage = VK_BUFFER_USAGE_TRANSFER_DST_BIT;
        bufferInfo.sharingMode = VK_SHARING_MODE_EXCLUSIVE;
        VmaAllocationCreateInfo allocationInfo{};
        allocationInfo.usage = VMA_MEMORY_USAGE_AUTO;
        allocationInfo.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_RANDOM_BIT | VMA_ALLOCATION_CREATE_MAPPED_BIT;
        VmaAllocationInfo resultInfo{};
        if (vmaCreateBuffer(allocator, &bufferInfo, &allocationInfo, &buffer, &allocation, &resultInfo) != VK_SUCCESS)
            return false;
        mapped = resultInfo.pMappedData;
        return mapped != nullptr;
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

std::vector<char> SpirvBytes(const std::vector<uint32_t> &words)
{
    std::vector<char> bytes(words.size() * sizeof(uint32_t));
    if (!bytes.empty())
        std::memcpy(bytes.data(), words.data(), bytes.size());
    return bytes;
}

std::vector<uint32_t> SpirvWords(const std::vector<char> &bytes)
{
    if (bytes.size() < 5 * sizeof(uint32_t) || bytes.size() % sizeof(uint32_t) != 0)
        return {};
    std::vector<uint32_t> words(bytes.size() / sizeof(uint32_t));
    std::memcpy(words.data(), bytes.data(), bytes.size());
    return words;
}

bool Require(bool condition, const char *message)
{
    if (!condition)
        std::cerr << "FAILED: " << message << '\n';
    return condition;
}

bool VerifyRhiBufferUpload(TestResources &resources)
{
    const std::array<uint32_t, 8> source = {3, 5, 8, 13, 21, 34, 55, 89};
    auto ticket =
        resources.resources.BeginBufferUpload({source.data(), sizeof(source), infernux::rhi::BufferUsage::Storage});
    if (!Require(ticket && resources.resources.TryPublishBufferUpload(ticket),
                 "DeviceLocal RHI buffer upload did not publish"))
        return false;

    auto resident = resources.resources.GetPublishedRhiBuffer(ticket);
    if (!Require(resident && resident->IsValid() && resident->GetByteSize() == sizeof(source),
                 "Published RHI buffer resource is invalid"))
        return false;
    const auto handle = resident->GetBuffer();
    if (!Require(resources.context.GetRhiDevice().Resolve(handle) == ticket->GetBuffer()->GetBuffer(),
                 "Published RHI buffer does not resolve to the uploaded Vulkan allocation"))
        return false;

    VkMemoryPropertyFlags memoryFlags = 0;
    vmaGetAllocationMemoryProperties(resources.context.GetVmaAllocator(), ticket->GetBuffer()->GetAllocation(),
                                     &memoryFlags);
    if (!Require((memoryFlags & VK_MEMORY_PROPERTY_DEVICE_LOCAL_BIT) != 0, "Published RHI buffer is not DeviceLocal"))
        return false;

    resident.reset();
    ticket.reset();
    return Require(resources.context.GetRhiDevice().Resolve(handle) == VK_NULL_HANDLE,
                   "Released RHI buffer registration remained visible");
}

bool VerifyGpuParticleMigration(TestResources &resources,
                                const infernux::particle::GpuParticleMigrationProgram &program)
{
    constexpr uint32_t sourceCapacity = 4;
    constexpr uint32_t destinationCapacity = 3;
    constexpr uint32_t sourceStrideWords = 6;
    constexpr uint32_t destinationStrideWords = 8;
    constexpr VkDeviceSize destinationStateBytes = destinationCapacity * destinationStrideWords * sizeof(uint32_t);
    constexpr VkDeviceSize destinationFreeListBytes = destinationCapacity * sizeof(uint32_t);
    constexpr VkDeviceSize counterBytes = 4 * sizeof(uint32_t);
    constexpr VkDeviceSize readbackBytes = destinationStateBytes + destinationFreeListBytes + counterBytes;

    const std::array<uint32_t, sourceCapacity * sourceStrideWords> sourceStates = {
        1, 11, 100, 101, 200, 900, 0, 0, 110, 111, 210, 901, 1, 22, 120, 121, 220, 902, 1, 33, 130, 131, 230, 903,
    };
    const std::array<uint32_t, 4> sourceCounters = {1, 3, 5, 7};
    const std::array<uint32_t, destinationStrideWords> defaults = {
        0, 0, 0xDEADu, 0xBEEFu, 0, 0, 0x1234u, 0x5678u,
    };

    auto &rhi = resources.context.GetRhiDevice();
    infernux::rhi::BufferDesc sourceDesc;
    sourceDesc.usage = infernux::rhi::BufferUsageFlags::Storage;
    sourceDesc.memory = infernux::rhi::BufferMemory::Upload;
    sourceDesc.byteSize = sizeof(sourceStates);
    sourceDesc.initialData = sourceStates.data();
    sourceDesc.initialDataBytes = sizeof(sourceStates);
    const auto sourceStateBuffer = rhi.CreateBuffer(sourceDesc);
    sourceDesc.byteSize = sizeof(sourceCounters);
    sourceDesc.initialData = sourceCounters.data();
    sourceDesc.initialDataBytes = sizeof(sourceCounters);
    const auto sourceCounterBuffer = rhi.CreateBuffer(sourceDesc);

    infernux::rhi::BufferDesc destinationDesc;
    destinationDesc.usage = infernux::rhi::BufferUsageFlags::Storage | infernux::rhi::BufferUsageFlags::TransferSource;
    destinationDesc.byteSize = destinationStateBytes;
    const auto destinationStateBuffer = rhi.CreateBuffer(destinationDesc);
    destinationDesc.byteSize = destinationFreeListBytes;
    const auto destinationFreeListBuffer = rhi.CreateBuffer(destinationDesc);
    destinationDesc.byteSize = counterBytes;
    const auto destinationCounterBuffer = rhi.CreateBuffer(destinationDesc);
    if (!Require(sourceStateBuffer.IsValid() && sourceCounterBuffer.IsValid() && destinationStateBuffer.IsValid() &&
                     destinationFreeListBuffer.IsValid() && destinationCounterBuffer.IsValid(),
                 "GPU particle migration fixture buffers are invalid"))
        return false;

    infernux::particle::GpuParticleMigrationDesc migrationDesc;
    migrationDesc.sourceCapacity = sourceCapacity;
    migrationDesc.destinationCapacity = destinationCapacity;
    migrationDesc.sourceStride = sourceStrideWords * sizeof(uint32_t);
    migrationDesc.destinationStride = destinationStrideWords * sizeof(uint32_t);
    migrationDesc.sourceStates = sourceStateBuffer;
    migrationDesc.sourceCounters = sourceCounterBuffer;
    migrationDesc.destinationStates = destinationStateBuffer;
    migrationDesc.destinationFreeList = destinationFreeListBuffer;
    migrationDesc.destinationCounters = destinationCounterBuffer;
    migrationDesc.copyRanges = {
        {4, 2, 1, 0},
        {2, 4, 2, 0},
    };
    migrationDesc.defaultStateWords.assign(defaults.begin(), defaults.end());
    migrationDesc.program = program;
    infernux::particle::ParticleGpuMigrator migrator;
    if (!Require(migrator.Create(rhi, migrationDesc), "GPU particle migrator creation failed"))
        return false;

    RenderGraph migrationGraph;
    migrationGraph.Initialize(&resources.context, &resources.pipelines);
    ResourceHandle sourceState;
    ResourceHandle sourceCounter;
    ResourceHandle destinationState;
    ResourceHandle destinationFreeList;
    ResourceHandle destinationCounter;
    ResourceHandle copyRanges;
    ResourceHandle defaultState;
    migrationGraph.AddComputePass("ParticleMigration/Reset", [&](PassBuilder &builder) {
        sourceCounter = builder.ImportBuffer("ParticleMigrationSourceCounters", sourceCounterBuffer, counterBytes);
        destinationCounter =
            builder.ImportBuffer("ParticleMigrationDestinationCounters", destinationCounterBuffer, counterBytes);
        builder.ReadStorageBuffer(sourceCounter);
        destinationCounter = builder.WriteStorageBuffer(destinationCounter);
        return [&](RenderContext &context) { migrator.RecordReset(context.GetComputeCommandEncoder()); };
    });
    migrationGraph.AddComputePass("ParticleMigration/Migrate", [&](PassBuilder &builder) {
        sourceState = builder.ImportBuffer("ParticleMigrationSourceStates", sourceStateBuffer, sizeof(sourceStates));
        destinationState =
            builder.ImportBuffer("ParticleMigrationDestinationStates", destinationStateBuffer, destinationStateBytes);
        destinationFreeList = builder.ImportBuffer("ParticleMigrationDestinationFreeList", destinationFreeListBuffer,
                                                   destinationFreeListBytes);
        copyRanges = builder.ImportBuffer("ParticleMigrationCopyRanges", migrator.CopyRangeBuffer(),
                                          migrationDesc.copyRanges.size() *
                                              sizeof(infernux::particle::GpuParticleMigrationRange));
        defaultState = builder.ImportBuffer("ParticleMigrationDefaults", migrator.DefaultStateBuffer(),
                                            defaults.size() * sizeof(uint32_t));
        builder.ReadStorageBuffer(sourceState);
        builder.ReadStorageBuffer(copyRanges);
        builder.ReadStorageBuffer(defaultState);
        destinationState = builder.WriteStorageBuffer(destinationState);
        destinationFreeList = builder.WriteStorageBuffer(destinationFreeList);
        destinationCounter = builder.ReadWrite(destinationCounter, infernux::rhi::PipelineStage::ComputeShader);
        return [&](RenderContext &context) { migrator.RecordMigrate(context.GetComputeCommandEncoder()); };
    });
    if (!Require(migrationGraph.Compile(), "GPU particle migration RenderGraph compilation failed") ||
        !Require(migrationGraph.GetExecutionPassNames() ==
                     std::vector<std::string>{"ParticleMigration/Reset", "ParticleMigration/Migrate"},
                 "GPU particle migration pass ordering is incorrect"))
        return false;

    BufferReadback readback;
    if (!Require(readback.Create(resources.context.GetVmaAllocator(), readbackBytes),
                 "GPU particle migration readback creation failed"))
        return false;

    VkCommandPool commandPool = VK_NULL_HANDLE;
    VkCommandPoolCreateInfo poolInfo{};
    poolInfo.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    poolInfo.queueFamilyIndex = resources.context.GetQueueIndices().graphicsFamily.value();
    poolInfo.flags = VK_COMMAND_POOL_CREATE_TRANSIENT_BIT;
    if (!Require(vkCreateCommandPool(resources.context.GetDevice(), &poolInfo, nullptr, &commandPool) == VK_SUCCESS,
                 "GPU particle migration command pool creation failed"))
        return false;

    VkCommandBuffer commandBuffer = VK_NULL_HANDLE;
    VkCommandBufferAllocateInfo allocateInfo{};
    allocateInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocateInfo.commandPool = commandPool;
    allocateInfo.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocateInfo.commandBufferCount = 1;
    if (!Require(vkAllocateCommandBuffers(resources.context.GetDevice(), &allocateInfo, &commandBuffer) == VK_SUCCESS,
                 "GPU particle migration command buffer allocation failed")) {
        vkDestroyCommandPool(resources.context.GetDevice(), commandPool, nullptr);
        return false;
    }

    VkCommandBufferBeginInfo beginInfo{};
    beginInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    beginInfo.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    if (!Require(vkBeginCommandBuffer(commandBuffer, &beginInfo) == VK_SUCCESS,
                 "GPU particle migration command buffer begin failed")) {
        vkDestroyCommandPool(resources.context.GetDevice(), commandPool, nullptr);
        return false;
    }
    migrationGraph.Execute(commandBuffer);

    const std::array<infernux::rhi::BufferHandle, 3> destinations = {
        destinationStateBuffer,
        destinationFreeListBuffer,
        destinationCounterBuffer,
    };
    const std::array<VkDeviceSize, 3> destinationBytes = {
        destinationStateBytes,
        destinationFreeListBytes,
        counterBytes,
    };
    const std::array<VkDeviceSize, 3> readbackOffsets = {
        0,
        destinationStateBytes,
        destinationStateBytes + destinationFreeListBytes,
    };
    std::array<VkBufferMemoryBarrier, 3> barriers{};
    for (size_t index = 0; index < destinations.size(); ++index) {
        auto &barrier = barriers[index];
        barrier.sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
        barrier.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT;
        barrier.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
        barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        barrier.buffer = rhi.Resolve(destinations[index]);
        barrier.offset = 0;
        barrier.size = destinationBytes[index];
    }
    vkCmdPipelineBarrier(commandBuffer, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0,
                         nullptr, static_cast<uint32_t>(barriers.size()), barriers.data(), 0, nullptr);
    for (size_t index = 0; index < destinations.size(); ++index) {
        const VkBufferCopy copy{0, readbackOffsets[index], destinationBytes[index]};
        vkCmdCopyBuffer(commandBuffer, barriers[index].buffer, readback.buffer, 1, &copy);
    }
    const bool commandRecorded = migrator.WasRecorded() && vkEndCommandBuffer(commandBuffer) == VK_SUCCESS;

    VkFence fence = VK_NULL_HANDLE;
    VkFenceCreateInfo fenceInfo{};
    fenceInfo.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    const bool fenceCreated =
        commandRecorded && vkCreateFence(resources.context.GetDevice(), &fenceInfo, nullptr, &fence) == VK_SUCCESS;
    VkSubmitInfo submitInfo{};
    submitInfo.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    submitInfo.commandBufferCount = 1;
    submitInfo.pCommandBuffers = &commandBuffer;
    const VkResult submitResult = fenceCreated
                                      ? vkQueueSubmit(resources.context.GetGraphicsQueue(), 1, &submitInfo, fence)
                                      : VK_ERROR_INITIALIZATION_FAILED;
    const VkResult waitResult = submitResult == VK_SUCCESS
                                    ? vkWaitForFences(resources.context.GetDevice(), 1, &fence, VK_TRUE, UINT64_MAX)
                                    : submitResult;
    if (fence != VK_NULL_HANDLE)
        vkDestroyFence(resources.context.GetDevice(), fence, nullptr);
    vkDestroyCommandPool(resources.context.GetDevice(), commandPool, nullptr);
    if (!Require(commandRecorded && submitResult == VK_SUCCESS && waitResult == VK_SUCCESS,
                 "GPU particle migration submission failed"))
        return false;
    if (!Require(vmaInvalidateAllocation(readback.allocator, readback.allocation, 0, readback.byteSize) == VK_SUCCESS,
                 "GPU particle migration readback invalidation failed"))
        return false;

    const std::array<uint32_t, destinationCapacity * destinationStrideWords> expectedStates = {
        1, 11, 200,     0xBEEFu, 100, 101, 0x1234u, 0x5678u, 0,   0,   0xDEADu, 0xBEEFu,
        0, 0,  0x1234u, 0x5678u, 1,   22,  220,     0xBEEFu, 120, 121, 0x1234u, 0x5678u,
    };
    std::array<uint32_t, expectedStates.size()> migratedStates{};
    std::array<uint32_t, destinationCapacity> migratedFreeList{};
    std::array<uint32_t, 4> migratedCounters{};
    std::memcpy(migratedStates.data(), readback.mapped, sizeof(migratedStates));
    std::memcpy(migratedFreeList.data(), static_cast<const uint8_t *>(readback.mapped) + destinationStateBytes,
                sizeof(migratedFreeList));
    std::memcpy(migratedCounters.data(),
                static_cast<const uint8_t *>(readback.mapped) + destinationStateBytes + destinationFreeListBytes,
                sizeof(migratedCounters));
    const bool migrationMatches = migratedStates == expectedStates && migratedFreeList[0] == 1 &&
                                  migratedCounters == std::array<uint32_t, 4>{1, 0, 6, 7};

    migrationGraph.Destroy();
    migrator.Destroy();
    rhi.Release(destinationCounterBuffer);
    rhi.Release(destinationFreeListBuffer);
    rhi.Release(destinationStateBuffer);
    rhi.Release(sourceCounterBuffer);
    rhi.Release(sourceStateBuffer);
    return Require(
        migrationMatches,
        "GPU particle migration did not preserve fields, apply defaults, rebuild free slots, or count drops");
}

bool VerifyVectorFieldSampling(TestResources &resources, infernux::InxShaderLoader &compiler)
{
    constexpr uint32_t sampleCount = 5;
    constexpr VkDeviceSize outputBytes = sampleCount * 4 * sizeof(float);
    const std::array<float, 2 * 2 * 2 * 4> texels = {
        1, 2, 3, 0, 4, 5, 6, 0, 7, 8, 9, 0, 10, 11, 12, 0, 13, 14, 15, 0, 16, 17, 18, 0, 19, 20, 21, 0, 22, 23, 24, 0,
    };
    const std::string source = R"(#version 450
layout(local_size_x = 1, local_size_y = 1, local_size_z = 1) in;
layout(std430, set = 0, binding = 0) writeonly buffer OutputBuffer { vec4 values[]; } output_buffer;
layout(set = 0, binding = 1) uniform sampler3D linear_clamp_field;
layout(set = 0, binding = 2) uniform sampler3D nearest_clamp_field;
layout(set = 0, binding = 3) uniform sampler3D nearest_repeat_field;

vec3 simulation_to_field(vec3 position) {
    return vec3(position.x * 0.5 + 0.25, position.y, position.z);
}

vec3 field_to_simulation(vec3 value) {
    return mat3(vec3(0.0, 2.0, 0.0), vec3(-1.0, 0.0, 0.0), vec3(0.0, 0.0, 0.5)) * value * 1.5;
}

vec3 sample_zero_linear(vec3 position) {
    vec3 uvw = simulation_to_field(position);
    if (any(lessThan(uvw, vec3(0.0))) || any(greaterThan(uvw, vec3(1.0)))) return vec3(0.0);
    return field_to_simulation(texture(linear_clamp_field, uvw).xyz);
}

void main() {
    output_buffer.values[0] = vec4(sample_zero_linear(vec3(0.5, 0.5, 0.5)), 1.0);
    output_buffer.values[1] = vec4(sample_zero_linear(vec3(-1.0, 0.5, 0.5)), 1.0);
    output_buffer.values[2] = vec4(field_to_simulation(texture(nearest_clamp_field, vec3(1.25, 0.25, 0.75)).xyz), 1.0);
    output_buffer.values[3] = vec4(field_to_simulation(texture(nearest_repeat_field, vec3(1.25, 0.25, 0.75)).xyz), 1.0);
    output_buffer.values[4] = vec4(sample_zero_linear(vec3(0.0, 0.25, 0.75)), 1.0);
}
)";
    const auto shaderWords = SpirvWords(compiler.CompileComputeGlsl(source, "Tests/VectorFieldSampling.comp"));
    if (!Require(!shaderWords.empty(), "Failed to compile Vector Field sampling fixture"))
        return false;

    auto &rhi = resources.context.GetRhiDevice();
    const infernux::rhi::TextureSubresourceUpload uploadRegion = {
        texels.data(), sizeof(texels), 0, 0, 1, 2, 2, 2, 2 * 4 * sizeof(float), 2 * 2 * 4 * sizeof(float)};
    infernux::rhi::TextureUploadRequest uploadRequest;
    uploadRequest.texture.dimension = infernux::rhi::TextureDimension::Texture3D;
    uploadRequest.texture.width = 2;
    uploadRequest.texture.height = 2;
    uploadRequest.texture.depthOrLayers = 2;
    uploadRequest.texture.format = infernux::rhi::PixelFormat::RGBA32SFloat;
    uploadRequest.texture.usage =
        infernux::rhi::TextureUsageFlags::Sampled | infernux::rhi::TextureUsageFlags::TransferDestination;
    uploadRequest.view.dimension = infernux::rhi::TextureViewDimension::Texture3D;
    uploadRequest.subresources = &uploadRegion;
    uploadRequest.subresourceCount = 1;
    const auto upload = resources.resources.BeginTextureUpload(uploadRequest);
    if (!Require(upload && upload->IsPublished() && upload->GetTexture() && upload->GetTexture()->IsValid(),
                 "Vector Field Texture3D upload failed"))
        return false;

    infernux::rhi::BufferDesc outputDesc;
    outputDesc.byteSize = outputBytes;
    outputDesc.usage = infernux::rhi::BufferUsageFlags::Storage | infernux::rhi::BufferUsageFlags::TransferSource;
    const auto output = rhi.CreateBuffer(outputDesc);
    infernux::rhi::SamplerDesc linearClampDesc;
    linearClampDesc.addressU = infernux::rhi::AddressMode::ClampToEdge;
    linearClampDesc.addressV = infernux::rhi::AddressMode::ClampToEdge;
    linearClampDesc.addressW = infernux::rhi::AddressMode::ClampToEdge;
    const auto linearClamp = rhi.CreateSampler(linearClampDesc);
    auto nearestClampDesc = linearClampDesc;
    nearestClampDesc.minFilter = infernux::rhi::FilterMode::Nearest;
    nearestClampDesc.magFilter = infernux::rhi::FilterMode::Nearest;
    nearestClampDesc.mipFilter = infernux::rhi::FilterMode::Nearest;
    const auto nearestClamp = rhi.CreateSampler(nearestClampDesc);
    auto nearestRepeatDesc = nearestClampDesc;
    nearestRepeatDesc.addressU = infernux::rhi::AddressMode::Repeat;
    nearestRepeatDesc.addressV = infernux::rhi::AddressMode::Repeat;
    nearestRepeatDesc.addressW = infernux::rhi::AddressMode::Repeat;
    const auto nearestRepeat = rhi.CreateSampler(nearestRepeatDesc);

    infernux::rhi::BindingLayoutDesc layoutDesc;
    layoutDesc.entries[0] = {0, infernux::rhi::BindingType::StorageBuffer, infernux::rhi::ShaderStage::Compute, 1};
    layoutDesc.entries[1] = {1, infernux::rhi::BindingType::CombinedTextureSampler, infernux::rhi::ShaderStage::Compute,
                             1};
    layoutDesc.entries[2] = {2, infernux::rhi::BindingType::CombinedTextureSampler, infernux::rhi::ShaderStage::Compute,
                             1};
    layoutDesc.entries[3] = {3, infernux::rhi::BindingType::CombinedTextureSampler, infernux::rhi::ShaderStage::Compute,
                             1};
    layoutDesc.entryCount = 4;
    const auto layout = rhi.CreateBindingLayout(layoutDesc);
    const auto shader = rhi.CreateShaderModule({shaderWords.data(), shaderWords.size()});
    infernux::rhi::ComputePipelineDesc pipelineDesc;
    pipelineDesc.computeShader = shader;
    pipelineDesc.bindingLayouts[0] = layout;
    pipelineDesc.bindingLayoutCount = 1;
    const auto pipeline = rhi.CreateComputePipeline(pipelineDesc);

    infernux::rhi::BindGroupDesc groupDesc;
    groupDesc.layout = layout;
    groupDesc.buffers[0] = {0, infernux::rhi::BindingType::StorageBuffer, output, 0, outputBytes};
    groupDesc.bufferCount = 1;
    const auto view = upload->GetTexture()->GetView();
    groupDesc.textures[0] = {1, infernux::rhi::BindingType::CombinedTextureSampler, view, linearClamp};
    groupDesc.textures[1] = {2, infernux::rhi::BindingType::CombinedTextureSampler, view, nearestClamp};
    groupDesc.textures[2] = {3, infernux::rhi::BindingType::CombinedTextureSampler, view, nearestRepeat};
    groupDesc.textureCount = 3;
    const auto group = rhi.CreateBindGroup(groupDesc);
    if (!Require(output.IsValid() && linearClamp.IsValid() && nearestClamp.IsValid() && nearestRepeat.IsValid() &&
                     layout.IsValid() && shader.IsValid() && pipeline.IsValid() && group.IsValid(),
                 "Vector Field RHI sampling resources are invalid"))
        return false;

    BufferReadback readback;
    if (!Require(readback.Create(resources.context.GetVmaAllocator(), outputBytes),
                 "Vector Field readback creation failed"))
        return false;
    VkCommandPool commandPool = VK_NULL_HANDLE;
    VkCommandPoolCreateInfo poolInfo{};
    poolInfo.sType = VK_STRUCTURE_TYPE_COMMAND_POOL_CREATE_INFO;
    poolInfo.queueFamilyIndex = resources.context.GetQueueIndices().graphicsFamily.value();
    poolInfo.flags = VK_COMMAND_POOL_CREATE_TRANSIENT_BIT;
    if (!Require(vkCreateCommandPool(resources.context.GetDevice(), &poolInfo, nullptr, &commandPool) == VK_SUCCESS,
                 "Vector Field command pool creation failed"))
        return false;
    VkCommandBuffer commandBuffer = VK_NULL_HANDLE;
    VkCommandBufferAllocateInfo allocateInfo{};
    allocateInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_ALLOCATE_INFO;
    allocateInfo.commandPool = commandPool;
    allocateInfo.level = VK_COMMAND_BUFFER_LEVEL_PRIMARY;
    allocateInfo.commandBufferCount = 1;
    if (!Require(vkAllocateCommandBuffers(resources.context.GetDevice(), &allocateInfo, &commandBuffer) == VK_SUCCESS,
                 "Vector Field command allocation failed")) {
        vkDestroyCommandPool(resources.context.GetDevice(), commandPool, nullptr);
        return false;
    }
    VkCommandBufferBeginInfo beginInfo{};
    beginInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
    beginInfo.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
    if (!Require(vkBeginCommandBuffer(commandBuffer, &beginInfo) == VK_SUCCESS, "Vector Field command begin failed")) {
        vkDestroyCommandPool(resources.context.GetDevice(), commandPool, nullptr);
        return false;
    }
    infernux::vk::VulkanComputeCommandContext computeContext;
    auto encoder = rhi.MakeComputeCommandEncoder(computeContext, commandBuffer);
    encoder.BindPipeline(pipeline);
    encoder.BindGroup(pipeline, 0, group);
    encoder.Dispatch(1, 1, 1);
    VkBufferMemoryBarrier barrier{};
    barrier.sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
    barrier.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT;
    barrier.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
    barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
    barrier.buffer = rhi.Resolve(output);
    barrier.size = outputBytes;
    vkCmdPipelineBarrier(commandBuffer, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT, VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0,
                         nullptr, 1, &barrier, 0, nullptr);
    const VkBufferCopy copy{0, 0, outputBytes};
    vkCmdCopyBuffer(commandBuffer, barrier.buffer, readback.buffer, 1, &copy);
    const bool recorded = vkEndCommandBuffer(commandBuffer) == VK_SUCCESS;
    VkFence fence = VK_NULL_HANDLE;
    VkFenceCreateInfo fenceInfo{};
    fenceInfo.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
    const bool fenceCreated =
        recorded && vkCreateFence(resources.context.GetDevice(), &fenceInfo, nullptr, &fence) == VK_SUCCESS;
    VkSubmitInfo submitInfo{};
    submitInfo.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
    submitInfo.commandBufferCount = 1;
    submitInfo.pCommandBuffers = &commandBuffer;
    const VkResult submitted = fenceCreated ? vkQueueSubmit(resources.context.GetGraphicsQueue(), 1, &submitInfo, fence)
                                            : VK_ERROR_INITIALIZATION_FAILED;
    const VkResult waited = submitted == VK_SUCCESS
                                ? vkWaitForFences(resources.context.GetDevice(), 1, &fence, VK_TRUE, UINT64_MAX)
                                : submitted;
    if (fence != VK_NULL_HANDLE)
        vkDestroyFence(resources.context.GetDevice(), fence, nullptr);
    vkDestroyCommandPool(resources.context.GetDevice(), commandPool, nullptr);
    if (!Require(submitted == VK_SUCCESS && waited == VK_SUCCESS &&
                     vmaInvalidateAllocation(readback.allocator, readback.allocation, 0, outputBytes) == VK_SUCCESS,
                 "Vector Field compute submission or readback failed"))
        return false;

    using Vec3 = std::array<float, 3>;
    const auto texel = [&](int x, int y, int z) {
        const size_t base = static_cast<size_t>(((z * 2 + y) * 2 + x) * 4);
        return Vec3{texels[base], texels[base + 1], texels[base + 2]};
    };
    const auto transform = [](Vec3 value) { return Vec3{-1.5f * value[1], 3.0f * value[0], 0.75f * value[2]}; };
    const auto nearest = [&](Vec3 uvw, bool repeat) {
        std::array<int, 3> index{};
        for (size_t axis = 0; axis < 3; ++axis) {
            float coordinate = uvw[axis];
            if (repeat)
                coordinate -= std::floor(coordinate);
            else
                coordinate = std::clamp(coordinate, 0.0f, 1.0f);
            index[axis] = std::min(static_cast<int>(std::floor(coordinate * 2.0f)), 1);
        }
        return texel(index[0], index[1], index[2]);
    };
    const auto linear = [&](Vec3 uvw) {
        std::array<int, 3> base{};
        std::array<int, 3> next{};
        Vec3 fraction{};
        for (size_t axis = 0; axis < 3; ++axis) {
            const float coordinate = std::clamp(uvw[axis], 0.0f, 1.0f) * 2.0f - 0.5f;
            base[axis] = std::clamp(static_cast<int>(std::floor(coordinate)), 0, 1);
            next[axis] = std::clamp(static_cast<int>(std::floor(coordinate)) + 1, 0, 1);
            fraction[axis] = coordinate - std::floor(coordinate);
        }
        Vec3 result{};
        for (int z = 0; z < 2; ++z) {
            for (int y = 0; y < 2; ++y) {
                for (int x = 0; x < 2; ++x) {
                    const float weight = (x ? fraction[0] : 1.0f - fraction[0]) *
                                         (y ? fraction[1] : 1.0f - fraction[1]) *
                                         (z ? fraction[2] : 1.0f - fraction[2]);
                    const auto value = texel(x ? next[0] : base[0], y ? next[1] : base[1], z ? next[2] : base[2]);
                    for (size_t axis = 0; axis < 3; ++axis)
                        result[axis] += value[axis] * weight;
                }
            }
        }
        return result;
    };
    const std::array<Vec3, sampleCount> expected = {
        transform(linear({0.5f, 0.5f, 0.5f})),
        Vec3{0.0f, 0.0f, 0.0f},
        transform(nearest({1.25f, 0.25f, 0.75f}, false)),
        transform(nearest({1.25f, 0.25f, 0.75f}, true)),
        transform(linear({0.25f, 0.25f, 0.75f})),
    };
    std::array<float, sampleCount * 4> actual{};
    std::memcpy(actual.data(), readback.mapped, outputBytes);
    bool matches = true;
    for (size_t sample = 0; sample < sampleCount; ++sample) {
        for (size_t axis = 0; axis < 3; ++axis)
            matches = matches && std::abs(actual[sample * 4 + axis] - expected[sample][axis]) <= 1.0e-4f;
        matches = matches && std::abs(actual[sample * 4 + 3] - 1.0f) <= 1.0e-6f;
    }

    rhi.Release(group);
    rhi.Release(pipeline);
    rhi.Release(shader);
    rhi.Release(layout);
    rhi.Release(nearestRepeat);
    rhi.Release(nearestClamp);
    rhi.Release(linearClamp);
    rhi.Release(output);
    return Require(matches, "Vector Field GPU samples diverged from the CPU sampling contract");
}

bool Run(const std::filesystem::path &computePath, const std::filesystem::path &vertexPath,
         const std::filesystem::path &fragmentPath, const std::filesystem::path &reflectionPath,
         const std::filesystem::path &particleComputePath, const std::filesystem::path &particleVertexPath,
         const std::filesystem::path &particleFragmentPath, const std::filesystem::path &particleTexturedVertexPath,
         const std::filesystem::path &particleTexturedFragmentPath)
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
    if (!Require(resources.resources.Initialize(resources.context), "Vulkan resource manager initialization failed"))
        return false;
    if (!VerifyRhiBufferUpload(resources))
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
    const auto particleTexturedVertexCode = ReadSpirv(particleTexturedVertexPath);
    const auto particleTexturedFragmentCode = ReadSpirv(particleTexturedFragmentPath);
    if (!Require(!computeCode.empty() && !vertexCode.empty() && !fragmentCode.empty() && !reflectionCode.empty() &&
                     !particleComputeCode.empty() && !particleVertexCode.empty() && !particleFragmentCode.empty() &&
                     !particleTexturedVertexCode.empty() && !particleTexturedFragmentCode.empty(),
                 "Failed to read generated SPIR-V test shaders"))
        return false;

    infernux::InxShaderLoader sortCompiler(false, true, false, true, false, true, false, false, false, false);
    if (!VerifyVectorFieldSampling(resources, sortCompiler))
        return false;
    const std::array<std::string_view, 4> sortSources = {
        infernux::particle::GpuParticleSortShaderSources::Generate(),
        infernux::particle::GpuParticleSortShaderSources::Histogram(),
        infernux::particle::GpuParticleSortShaderSources::Scan(),
        infernux::particle::GpuParticleSortShaderSources::Scatter(),
    };
    std::array<std::vector<uint32_t>, 4> sortCode;
    for (size_t index = 0; index < sortSources.size(); ++index) {
        sortCode[index] = SpirvWords(sortCompiler.CompileComputeGlsl(
            std::string(sortSources[index]), "Tests/ParticleSort" + std::to_string(index) + ".comp"));
        if (!Require(!sortCode[index].empty(), "Failed to compile GPU particle sort fixture"))
            return false;
    }
    const infernux::particle::GpuParticleSortProgram sortProgram = {
        {sortCode[0].data(), sortCode[0].size()},
        {sortCode[1].data(), sortCode[1].size()},
        {sortCode[2].data(), sortCode[2].size()},
        {sortCode[3].data(), sortCode[3].size()},
    };
    const std::array<std::string_view, 3> cullSources = {
        infernux::particle::GpuParticleCullShaderSources::Reset(),
        infernux::particle::GpuParticleCullShaderSources::Cull(),
        infernux::particle::GpuParticleCullShaderSources::Finalize(),
    };
    std::array<std::vector<uint32_t>, 3> cullCode;
    for (size_t index = 0; index < cullSources.size(); ++index) {
        cullCode[index] = SpirvWords(sortCompiler.CompileComputeGlsl(
            std::string(cullSources[index]), "Tests/ParticleCull" + std::to_string(index) + ".comp"));
        if (!Require(!cullCode[index].empty(), "Failed to compile GPU particle cull fixture"))
            return false;
    }
    const infernux::particle::GpuParticleCullProgram cullProgram = {
        {cullCode[0].data(), cullCode[0].size()},
        {cullCode[1].data(), cullCode[1].size()},
        {cullCode[2].data(), cullCode[2].size()},
    };
    const std::array<std::string_view, 2> boundsSources = {
        infernux::particle::GpuParticleBoundsShaderSources::Reset(),
        infernux::particle::GpuParticleBoundsShaderSources::Reduce(),
    };
    std::array<std::vector<uint32_t>, 2> boundsCode;
    for (size_t index = 0; index < boundsSources.size(); ++index) {
        boundsCode[index] = SpirvWords(sortCompiler.CompileComputeGlsl(
            std::string(boundsSources[index]), "Tests/ParticleBounds" + std::to_string(index) + ".comp"));
        if (!Require(!boundsCode[index].empty(), "Failed to compile GPU particle bounds fixture"))
            return false;
    }
    const infernux::particle::GpuParticleBoundsProgram boundsProgram = {
        {boundsCode[0].data(), boundsCode[0].size()},
        {boundsCode[1].data(), boundsCode[1].size()},
    };
    const std::array<std::string_view, 2> migrationSources = {
        infernux::particle::GpuParticleMigrationShaderSources::Reset(),
        infernux::particle::GpuParticleMigrationShaderSources::Migrate(),
    };
    std::array<std::vector<uint32_t>, 2> migrationCode;
    for (size_t index = 0; index < migrationSources.size(); ++index) {
        migrationCode[index] = SpirvWords(sortCompiler.CompileComputeGlsl(
            std::string(migrationSources[index]), "Tests/ParticleMigration" + std::to_string(index) + ".comp"));
        if (!Require(!migrationCode[index].empty(), "Failed to compile GPU particle migration fixture"))
            return false;
    }
    const infernux::particle::GpuParticleMigrationProgram migrationProgram = {
        {migrationCode[0].data(), migrationCode[0].size()},
        {migrationCode[1].data(), migrationCode[1].size()},
    };
    if (!VerifyGpuParticleMigration(resources, migrationProgram))
        return false;
    const auto eventCode = SpirvWords(sortCompiler.CompileComputeGlsl(
        std::string(infernux::particle::GpuParticleEventShaderSource::Prepare()), "Tests/ParticleEventPrepare.comp"));
    if (!Require(!eventCode.empty(), "Failed to compile GPU particle event fixture"))
        return false;
    const infernux::particle::GpuParticleEventProgram eventProgram = {eventCode.data(), eventCode.size()};

    auto initialLinkedParticleProgram = std::make_shared<infernux::ShaderProgramArtifact>();
    initialLinkedParticleProgram->key = {{"Tests/ParticleSprite", "Tests/ParticleSurface"}, 10};
    initialLinkedParticleProgram->domain = infernux::ShaderProgramDomain::ParticleSprite;
    initialLinkedParticleProgram->compatibilitySignature = 41;
    infernux::ShaderProgramArtifact::PassVariant initialLinkedParticleForward;
    initialLinkedParticleForward.compatibilitySignature = initialLinkedParticleProgram->compatibilitySignature;
    initialLinkedParticleForward.vertexSpirv = SpirvBytes(particleVertexCode);
    initialLinkedParticleForward.fragmentSpirv = SpirvBytes(particleFragmentCode);
    initialLinkedParticleProgram->variants.push_back(std::move(initialLinkedParticleForward));
    if (!Require(initialLinkedParticleProgram->IsValid(), "Initial linked particle test artifact is invalid"))
        return false;

    infernux::FrameDeletionQueue particleDeletionQueue;
    particleDeletionQueue.Initialize(2);
    infernux::particle::ParticleGpuDrawRegistry particleDrawRegistry;
    infernux::particle::ParticleGpuSystemManager particleSystems;
    if (!Require(particleSystems.Initialize(resources.context, resources.pipelines, resources.resources,
                                            particleDeletionQueue, particleDrawRegistry, {}, {}, {}, sortProgram,
                                            cullProgram, boundsProgram, migrationProgram, eventProgram),
                 "GPU particle system manager initialization failed"))
        return false;

    infernux::particle::GpuParticleEmitterProgram managedProgram;
    managedProgram.id = 91;
    managedProgram.graphInstanceId = 7001;
    managedProgram.ownerLayerMask = 1u << 4u;
    managedProgram.artifactRevision = 1;
    managedProgram.stableId = "managed-emitter";
    managedProgram.capacity = 32;
    managedProgram.stateStride = 16;
    for (auto &kernel : managedProgram.kernels)
        kernel = particleComputeCode;
    managedProgram.billboardVertexShader = particleVertexCode;
    managedProgram.billboardFragmentShader = particleFragmentCode;
    auto pointCacheData = std::make_shared<infernux::PointCacheCpuData>();
    pointCacheData->stableId = "managed-points";
    pointCacheData->name = "Managed Points";
    pointCacheData->bakeBasis = "right_handed_y_up";
    pointCacheData->pointCount = 2;
    pointCacheData->channels = {
        {"position", infernux::PointCacheChannelType::Float3, infernux::PointCacheChannelSemantic::Position, 0, 12},
        {"id", infernux::PointCacheChannelType::UInt, infernux::PointCacheChannelSemantic::Id, 32, 4},
    };
    pointCacheData->bytes.resize(40);
    const std::array<float, 6> managedPointPositions = {0.0f, 1.0f, 2.0f, 3.0f, 4.0f, 5.0f};
    const std::array<uint32_t, 2> managedPointIds = {7, 42};
    std::memcpy(pointCacheData->bytes.data(), managedPointPositions.data(), sizeof(managedPointPositions));
    std::memcpy(pointCacheData->bytes.data() + 32, managedPointIds.data(), sizeof(managedPointIds));
    pointCacheData->RebuildIdLookup();
    auto managedPointCache = std::make_shared<infernux::InxPointCache>();
    managedPointCache->SetGuid("managed-point-cache-guid");
    managedPointCache->SetCpuData(pointCacheData);
    infernux::particle::GpuParticlePointCacheProgram pointCacheProgram;
    pointCacheProgram.stableId = "managed-point-cache";
    pointCacheProgram.interfaceIndex = 0;
    pointCacheProgram.dataBinding = 1;
    pointCacheProgram.lookupBinding = 2;
    pointCacheProgram.cacheToSpace = {
        1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f, 0.0f, 0.0f, 0.0f, 0.0f, 1.0f,
    };
    pointCacheProgram.cache = managedPointCache;
    pointCacheProgram.samples.push_back({0, "position", infernux::PointCacheChannelType::Float3, false});
    managedProgram.pointCaches.sampleCount = 1;
    managedProgram.pointCaches.pointCaches.push_back(std::move(pointCacheProgram));
    infernux::particle::GpuParticleOutputProgram primaryOutput;
    primaryOutput.id = 911;
    primaryOutput.stableId = "managed-primary";
    primaryOutput.semantics.sortMode = infernux::particle::ParticleSortMode::FrontToBack;
    primaryOutput.shaderProgram = initialLinkedParticleProgram;
    primaryOutput.material = std::make_shared<infernux::InxMaterial>("managed-primary-material");
    auto primaryMaterialState = primaryOutput.material->GetRenderState();
    primaryMaterialState.renderQueue = 3050;
    primaryMaterialState.blendEnable = false;
    primaryMaterialState.depthWriteEnable = false;
    primaryOutput.material->SetRenderState(primaryMaterialState);
    managedProgram.outputs.push_back(primaryOutput);
    std::string managedError;
    auto publishManagedGraph = [&](std::vector<infernux::particle::GpuParticleEmitterProgram> emitters,
                                   std::vector<uint64_t> removeIds = {}) {
        infernux::particle::GpuParticleGraphProgram graphProgram;
        graphProgram.graphInstanceId = managedProgram.graphInstanceId;
        graphProgram.emitters = std::move(emitters);
        graphProgram.removeEmitterIds = std::move(removeIds);
        return particleSystems.ApplyGraph(graphProgram, &managedError);
    };
    const uint64_t pointCacheUploadsBeforeCreate = resources.resources.GetBufferUploadSubmissionCount();
    if (!Require(publishManagedGraph({managedProgram}), managedError.c_str()) ||
        !Require(particleSystems.Size() == 1 && particleSystems.Contains(managedProgram.id) &&
                     particleSystems.ActiveArtifactRevision(managedProgram.id) == 1 &&
                     particleDrawRegistry.Size() == 1 &&
                     particleSystems.ActivePointCacheGeneration(managedProgram.id, 0) ==
                         managedPointCache->GetGeneration() &&
                     resources.resources.GetBufferUploadSubmissionCount() == pointCacheUploadsBeforeCreate + 2,
                 "GPU particle system was not published atomically"))
        return false;
    const auto residentParticleTelemetry = particleSystems.TelemetrySnapshot();
    if (!Require(residentParticleTelemetry.systemCount == 1 && residentParticleTelemetry.outputCount == 1 &&
                     residentParticleTelemetry.totalCapacity == 32 &&
                     residentParticleTelemetry.scheduledSystemCount == 0,
                 "GPU particle resident telemetry is incorrect before scheduling"))
        return false;
    const auto initialManagedEntries = particleDrawRegistry.Snapshot(3000, 3100);
    if (!Require(initialManagedEntries.size() == 1 && initialManagedEntries[0].ownerLayerMask == 1u << 4u &&
                     !particleSystems.ActiveStateWasPreserved(managedProgram.id),
                 "Initial GPU particle publication reported a preserved state"))
        return false;
    const auto initialManagedInstances = initialManagedEntries[0].instances;
    const auto initialManagedIndirect = initialManagedEntries[0].indirectArguments;
    const auto primarySemantics = particleSystems.ActiveOutputSemantics(managedProgram.id, primaryOutput.id);
    if (!Require(primarySemantics && !primarySemantics->receiveSceneLighting && !primarySemantics->receiveShadows &&
                     primarySemantics->sortMode == infernux::particle::ParticleSortMode::FrontToBack,
                 "GPU particle output semantics were lost during publication"))
        return false;

    auto invalidSemanticsProgram = managedProgram;
    invalidSemanticsProgram.artifactRevision = 2;
    invalidSemanticsProgram.outputs[0].semantics.receiveShadows = true;
    if (!Require(!publishManagedGraph({invalidSemanticsProgram}) &&
                     particleSystems.ActiveArtifactRevision(managedProgram.id) == 1,
                 "Invalid GPU particle output semantics disturbed the active revision"))
        return false;

    auto unsupportedLinkedLightingProgram = managedProgram;
    unsupportedLinkedLightingProgram.artifactRevision = 2;
    unsupportedLinkedLightingProgram.billboardForwardPlusFragmentShader = particleFragmentCode;
    unsupportedLinkedLightingProgram.outputs[0].semantics.receiveSceneLighting = true;
    if (!Require(!publishManagedGraph({unsupportedLinkedLightingProgram}) &&
                     managedError.find("requires a linked Particle Forward+ shader variant") != std::string::npos &&
                     particleSystems.ActiveArtifactRevision(managedProgram.id) == 1,
                 "Custom lit ParticleSprite publication silently replaced the linked program"))
        return false;

    auto linkedParticleProgram = std::make_shared<infernux::ShaderProgramArtifact>();
    linkedParticleProgram->key = {{"Tests/ParticleSprite", "Tests/ParticleSurface"}, 11};
    linkedParticleProgram->domain = infernux::ShaderProgramDomain::ParticleSprite;
    linkedParticleProgram->compatibilitySignature = 41;
    infernux::ShaderProgramArtifact::PassVariant linkedParticleForward;
    linkedParticleForward.compatibilitySignature = linkedParticleProgram->compatibilitySignature;
    linkedParticleForward.vertexSpirv = SpirvBytes(particleVertexCode);
    linkedParticleForward.fragmentSpirv = SpirvBytes(particleFragmentCode);
    linkedParticleProgram->variants.push_back(std::move(linkedParticleForward));
    if (!Require(linkedParticleProgram->IsValid(), "Linked particle test artifact is invalid"))
        return false;
    const uint64_t drawRevisionBeforeMaterialRefresh = particleDrawRegistry.Revision();
    if (!Require(particleSystems.RefreshMaterialProgram(primaryOutput.material, linkedParticleProgram, &managedError),
                 managedError.c_str()) ||
        !Require(particleSystems.ActiveArtifactRevision(managedProgram.id) == 1 &&
                     particleDrawRegistry.Revision() == drawRevisionBeforeMaterialRefresh + 1 &&
                     particleDeletionQueue.PendingCount() == 1,
                 "GPU particle material hot refresh reset simulation or failed to republish draws")) {
        return false;
    }
    auto invalidParticleProgram = std::make_shared<infernux::ShaderProgramArtifact>(*linkedParticleProgram);
    invalidParticleProgram->key.revision = 12;
    invalidParticleProgram->domain = infernux::ShaderProgramDomain::Mesh;
    const uint64_t drawRevisionBeforeRejectedMaterial = particleDrawRegistry.Revision();
    if (!Require(
            !particleSystems.RefreshMaterialProgram(primaryOutput.material, invalidParticleProgram, &managedError) &&
                particleDrawRegistry.Revision() == drawRevisionBeforeRejectedMaterial &&
                particleSystems.ActiveArtifactRevision(managedProgram.id) == 1,
            "Invalid GPU particle material disturbed the last-known-good renderer")) {
        return false;
    }
    particleDeletionQueue.Tick();
    particleDeletionQueue.Tick();
    particleDeletionQueue.Tick();
    if (!Require(particleDeletionQueue.PendingCount() == 0,
                 "GPU particle material retirement did not complete after deferred frames"))
        return false;

    auto invalidManagedProgram = managedProgram;
    invalidManagedProgram.artifactRevision = 2;
    auto invalidLinkedParticleProgram =
        std::make_shared<infernux::ShaderProgramArtifact>(*initialLinkedParticleProgram);
    invalidLinkedParticleProgram->variants.clear();
    invalidManagedProgram.outputs[0].shaderProgram = std::move(invalidLinkedParticleProgram);
    if (!Require(!publishManagedGraph({invalidManagedProgram}) &&
                     particleSystems.ActiveArtifactRevision(managedProgram.id) == 1 && particleDrawRegistry.Size() == 1,
                 "Invalid GPU particle replacement disturbed the active revision"))
        return false;

    managedProgram.artifactRevision = 2;
    managedProgram.preserveState = true;
    auto secondaryOutput = primaryOutput;
    secondaryOutput.id = 912;
    secondaryOutput.stableId = "managed-secondary";
    secondaryOutput.material = std::make_shared<infernux::InxMaterial>("managed-secondary-material");
    auto secondaryMaterialState = secondaryOutput.material->GetRenderState();
    secondaryMaterialState.renderQueue = 3075;
    secondaryMaterialState.blendEnable = true;
    secondaryMaterialState.depthWriteEnable = false;
    secondaryOutput.material->SetRenderState(secondaryMaterialState);
    managedProgram.outputs.push_back(secondaryOutput);
    const bool managedReplacement = publishManagedGraph({managedProgram});
    if (!managedReplacement || particleSystems.ActiveArtifactRevision(managedProgram.id) != 2 ||
        particleDeletionQueue.PendingCount() != 1) {
        std::cerr << "GPU particle replacement detail: success=" << managedReplacement
                  << " revision=" << particleSystems.ActiveArtifactRevision(managedProgram.id)
                  << " pending=" << particleDeletionQueue.PendingCount() << " error=" << managedError << '\n';
    }
    if (!Require(managedReplacement && particleSystems.ActiveArtifactRevision(managedProgram.id) == 2 &&
                     particleSystems.ActiveOutputCount(managedProgram.id) == 2 && particleDrawRegistry.Size() == 2 &&
                     particleSystems.ActiveStateWasPreserved(managedProgram.id) &&
                     particleDeletionQueue.PendingCount() == 1,
                 "Valid GPU particle hot replacement was not published with deferred retirement"))
        return false;
    const auto preservedManagedEntries = particleDrawRegistry.Snapshot(3000, 3100);
    if (!Require(preservedManagedEntries.size() == 2 &&
                     resources.resources.GetBufferUploadSubmissionCount() == pointCacheUploadsBeforeCreate + 2 &&
                     preservedManagedEntries[0].instances == initialManagedInstances &&
                     preservedManagedEntries[1].instances == initialManagedInstances &&
                     preservedManagedEntries[0].indirectArguments == initialManagedIndirect &&
                     preservedManagedEntries[1].indirectArguments == initialManagedIndirect,
                 "Compatible GPU particle reload replaced resident simulation buffers"))
        return false;

    auto incompatiblePreservation = managedProgram;
    incompatiblePreservation.artifactRevision = 3;
    incompatiblePreservation.capacity *= 2;
    if (!Require(!publishManagedGraph({incompatiblePreservation}) &&
                     particleSystems.ActiveArtifactRevision(managedProgram.id) == 2 &&
                     particleSystems.ActiveStateWasPreserved(managedProgram.id) &&
                     particleDeletionQueue.PendingCount() == 1,
                 "Incompatible GPU state preservation disturbed the last-known-good revision"))
        return false;

    auto duplicateOutputProgram = managedProgram;
    duplicateOutputProgram.artifactRevision = 3;
    duplicateOutputProgram.outputs[1].stableId = duplicateOutputProgram.outputs[0].stableId;
    if (!Require(!publishManagedGraph({duplicateOutputProgram}) &&
                     particleSystems.ActiveArtifactRevision(managedProgram.id) == 2 &&
                     particleSystems.ActiveOutputCount(managedProgram.id) == 2 && particleDrawRegistry.Size() == 2 &&
                     particleDeletionQueue.PendingCount() == 1,
                 "Duplicate GPU particle output identity disturbed the active revision"))
        return false;

    auto companionProgram = managedProgram;
    companionProgram.id = 92;
    companionProgram.stableId = "managed-companion";
    companionProgram.preserveState = false;
    companionProgram.outputs.resize(1);
    companionProgram.outputs[0].id = 921;
    companionProgram.outputs[0].stableId = "companion-primary";
    companionProgram.outputs[0].material = std::make_shared<infernux::InxMaterial>("companion-material");
    companionProgram.outputs[0].material->SetRenderQueue(3200);
    if (!Require(publishManagedGraph({companionProgram}) && particleSystems.Size() == 2 &&
                     particleSystems.Contains(companionProgram.id) &&
                     particleSystems.ActiveOutputCount(companionProgram.id) == 1 && particleDrawRegistry.Size() == 3 &&
                     particleDeletionQueue.PendingCount() == 2,
                 "GPU particle batch did not publish a valid companion emitter"))
        return false;

    auto invalidCompanion = companionProgram;
    invalidCompanion.artifactRevision = 3;
    auto invalidCompanionShader = std::make_shared<infernux::ShaderProgramArtifact>(*initialLinkedParticleProgram);
    invalidCompanionShader->variants.clear();
    invalidCompanion.outputs[0].shaderProgram = std::move(invalidCompanionShader);
    auto candidateManagedProgram = managedProgram;
    candidateManagedProgram.artifactRevision = 3;
    if (!Require(!publishManagedGraph({candidateManagedProgram, invalidCompanion}) &&
                     particleSystems.ActiveArtifactRevision(managedProgram.id) == 2 &&
                     particleSystems.ActiveArtifactRevision(companionProgram.id) == 2 &&
                     particleDrawRegistry.Size() == 3 && particleDeletionQueue.PendingCount() == 2,
                 "Failed GPU particle batch disturbed its last-known-good emitters"))
        return false;

    managedProgram.artifactRevision = 3;
    if (!Require(publishManagedGraph({managedProgram}, {companionProgram.id}) && particleSystems.Size() == 1 &&
                     !particleSystems.Contains(companionProgram.id) &&
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
    const infernux::particle::GpuParticleBatchFrameItem managedBatchItem{managedProgram.id, managedFrame,
                                                                         managedTransforms};
    if (!Require(particleSystems.BeginFrameBatch(managedProgram.graphInstanceId, {managedBatchItem}),
                 "GPU particle manager rejected a valid graph-instance frame batch"))
        return false;
    const auto scheduledParticleTelemetry = particleSystems.TelemetrySnapshot();
    if (!Require(scheduledParticleTelemetry.systemCount == 1 && scheduledParticleTelemetry.outputCount == 2 &&
                     scheduledParticleTelemetry.totalCapacity == 32 &&
                     scheduledParticleTelemetry.lastScheduledFrame == managedFrame.frameIndex &&
                     scheduledParticleTelemetry.scheduledSystemCount == 1 &&
                     scheduledParticleTelemetry.simulatingSystemCount == 1 &&
                     scheduledParticleTelemetry.renderingSystemCount == 1 &&
                     scheduledParticleTelemetry.requestedSpawnCount == managedFrame.spawnCount,
                 "GPU particle scheduling telemetry is incorrect"))
        return false;
    if (!Require(!particleSystems.BeginFrameBatch(managedProgram.graphInstanceId + 1, {managedBatchItem}) &&
                     !particleSystems.BeginFrameBatch(managedProgram.graphInstanceId,
                                                      {managedBatchItem, managedBatchItem}) &&
                     particleSystems.Reset(managedProgram.id) &&
                     particleSystems.BeginFrameBatch(managedProgram.graphInstanceId, {managedBatchItem}),
                 "GPU particle reset did not cancel and replace a pending frame request"))
        return false;
    const auto managedEntries = particleDrawRegistry.Snapshot(3000, 3100);
    if (!Require(managedEntries.size() == 2 && managedEntries[0].renderer->RenderQueue() == 3050 &&
                     managedEntries[1].renderer->RenderQueue() == 3075 &&
                     managedEntries[0].semantics.sortMode == infernux::particle::ParticleSortMode::FrontToBack &&
                     managedEntries[1].semantics.sortMode == infernux::particle::ParticleSortMode::FrontToBack &&
                     managedEntries[0].cullProgram && managedEntries[0].cullProgram == managedEntries[1].cullProgram &&
                     managedEntries[0].sortProgram && managedEntries[0].sortProgram == managedEntries[1].sortProgram &&
                     managedEntries[0].instances == managedEntries[1].instances &&
                     managedEntries[0].renderIndices == managedEntries[1].renderIndices &&
                     managedEntries[0].indirectArguments == managedEntries[1].indirectArguments &&
                     managedEntries[0].bounds.IsValid() && managedEntries[0].bounds == managedEntries[1].bounds,
                 "GPU particle outputs did not share one simulated stream across ordered draw queues"))
        return false;
    primaryOutput.material->SetRenderQueue(3080);
    const auto liveMaterialEntries = particleDrawRegistry.Snapshot(3000, 3100);
    if (!Require(particleSystems.ActiveOutputRenderQueue(managedProgram.id, primaryOutput.id) == 3080 &&
                     liveMaterialEntries.size() == 2 && liveMaterialEntries[0].id == secondaryOutput.id &&
                     liveMaterialEntries[1].id == primaryOutput.id,
                 "GPU particle queue routing did not observe the shared material's live state"))
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
    infernux::particle::ParticleGpuBounds particleBounds;
    infernux::particle::GpuParticleBoundsDesc particleBoundsDesc;
    particleBoundsDesc.capacity = particleRuntime.Capacity();
    particleBoundsDesc.instances = particleRuntime.InstanceBuffer();
    particleBoundsDesc.sourceIndirectArguments = particleRuntime.IndirectBuffer();
    particleBoundsDesc.program = boundsProgram;
    if (!Require(particleBounds.Create(rhi, particleBoundsDesc), "Particle GPU bounds creation failed"))
        return false;
    infernux::particle::ParticleGpuCuller managedViewCuller;
    infernux::particle::GpuParticleCullerDesc managedCullerDesc;
    managedCullerDesc.capacity = managedEntry.capacity;
    managedCullerDesc.vertexCount = 6;
    managedCullerDesc.instances = managedEntry.instances;
    managedCullerDesc.sourceIndirectArguments = managedEntry.indirectArguments;
    managedCullerDesc.bounds = managedEntry.bounds;
    managedCullerDesc.program = cullProgram;
    if (!Require(managedViewCuller.Create(rhi, managedCullerDesc), "Managed particle view culler creation failed"))
        return false;
    infernux::particle::ParticleGpuCuller offscreenViewCuller;
    if (!Require(offscreenViewCuller.Create(rhi, managedCullerDesc), "Offscreen particle view culler creation failed"))
        return false;
    infernux::particle::ParticleGpuSorter managedViewSorter;
    infernux::particle::GpuParticleSorterDesc managedSorterDesc;
    managedSorterDesc.capacity = managedEntry.capacity;
    managedSorterDesc.instances = managedEntry.instances;
    managedSorterDesc.indirectArguments = managedViewCuller.DrawIndirectBuffer();
    managedSorterDesc.sourceIndices = managedViewCuller.VisibleIndexBuffer();
    managedSorterDesc.dispatchArguments = managedViewCuller.SortDispatchBuffer();
    managedSorterDesc.program = sortProgram;
    if (!Require(managedViewSorter.Create(rhi, managedSorterDesc), "Managed particle view sorter creation failed"))
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
    ResourceHandle managedSourceIndirectArguments;
    ResourceHandle managedBounds;
    ResourceHandle managedVisibleIndices;
    ResourceHandle managedIndirectArguments;
    ResourceHandle managedSortDispatchArguments;
    ResourceHandle offscreenInstances;
    ResourceHandle offscreenSourceIndirectArguments;
    ResourceHandle offscreenBounds;
    ResourceHandle offscreenVisibleIndices;
    ResourceHandle offscreenIndirectArguments;
    ResourceHandle offscreenSortDispatchArguments;
    std::array<ResourceHandle, 2> managedSortKeys;
    std::array<ResourceHandle, 2> managedSortIndices;
    ResourceHandle managedSortHistograms;
    ResourceHandle managedSortBlockOffsets;
    ResourceHandle managedSortGlobalOffsets;
    ResourceHandle particleTexture;
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
    const std::array<float, 24> managedFrustum = {
        1, 0, 0, 1, -1, 0, 0, 1, 0, 1, 0, 1, 0, -1, 0, 1, 0, 0, 1, 0, 0, 0, -1, 1,
    };
    auto offscreenFrustum = managedFrustum;
    offscreenFrustum[3] = -100.0f;
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

    if (!Require(particleGraph.Attach(resources.graph, particleRuntime, particleBounds, "Particle/TestEmitter"),
                 "Particle RenderGraph attachment failed"))
        return false;
    if (!Require(particleGraph.BeginFrame(particleFrame), "Particle frame request was rejected"))
        return false;
    const auto particleOutputs = particleGraph.Outputs();

    resources.graph.AddComputePass("ManagedParticleCull/Reset", [&](PassBuilder &builder) {
        const uint64_t elementBytes = static_cast<uint64_t>(managedEntry.capacity) * sizeof(uint32_t);
        managedInstances = builder.ImportBuffer("ManagedParticleInstances", managedEntry.instances,
                                                static_cast<uint64_t>(managedEntry.capacity) *
                                                    infernux::particle::ParticleGpuRuntime::RenderInstanceStride);
        managedSourceIndirectArguments =
            builder.ImportBuffer("ManagedParticleSourceIndirect", managedEntry.indirectArguments, 16);
        managedBounds = builder.ImportBuffer("ManagedParticleBounds", managedEntry.bounds,
                                             infernux::particle::ParticleGpuBounds::BoundsBufferBytes);
        managedVisibleIndices =
            builder.ImportBuffer("ManagedParticleVisibleIndices", managedViewCuller.VisibleIndexBuffer(), elementBytes);
        managedIndirectArguments =
            builder.ImportBuffer("ManagedParticleViewIndirect", managedViewCuller.DrawIndirectBuffer(), 16);
        managedSortDispatchArguments =
            builder.ImportBuffer("ManagedParticleSortDispatch", managedViewCuller.SortDispatchBuffer(), 12);
        resources.graph.SetResourceInitialState(managedInstances, infernux::rhi::TextureLayout::Undefined,
                                                infernux::rhi::Access::ShaderWrite,
                                                infernux::rhi::PipelineStage::ComputeShader);
        resources.graph.SetResourceInitialState(managedSourceIndirectArguments, infernux::rhi::TextureLayout::Undefined,
                                                infernux::rhi::Access::ShaderWrite,
                                                infernux::rhi::PipelineStage::ComputeShader);
        resources.graph.SetResourceInitialState(managedBounds, infernux::rhi::TextureLayout::Undefined,
                                                infernux::rhi::Access::ShaderWrite,
                                                infernux::rhi::PipelineStage::ComputeShader);
        resources.graph.SetResourceInitialState(managedVisibleIndices, infernux::rhi::TextureLayout::Undefined,
                                                infernux::rhi::Access::ShaderWrite,
                                                infernux::rhi::PipelineStage::ComputeShader);
        resources.graph.SetResourceInitialState(managedIndirectArguments, infernux::rhi::TextureLayout::Undefined,
                                                infernux::rhi::Access::ShaderWrite,
                                                infernux::rhi::PipelineStage::ComputeShader);
        resources.graph.SetResourceInitialState(managedSortDispatchArguments, infernux::rhi::TextureLayout::Undefined,
                                                infernux::rhi::Access::ShaderWrite,
                                                infernux::rhi::PipelineStage::ComputeShader);
        builder.ReadStorageBuffer(managedSourceIndirectArguments);
        builder.ReadStorageBuffer(managedBounds);
        managedIndirectArguments = builder.WriteStorageBuffer(managedIndirectArguments);
        managedSortDispatchArguments = builder.WriteStorageBuffer(managedSortDispatchArguments);
        return [&](RenderContext &context) {
            managedViewCuller.RecordReset(context.GetComputeCommandEncoder(), managedFrustum);
        };
    });
    resources.graph.AddComputePass("ManagedParticleCull/Cull", [&](PassBuilder &builder) {
        builder.ReadStorageBuffer(managedInstances);
        builder.ReadStorageBuffer(managedSourceIndirectArguments);
        builder.ReadIndirectBuffer(managedSortDispatchArguments);
        managedVisibleIndices = builder.WriteStorageBuffer(managedVisibleIndices);
        managedIndirectArguments =
            builder.ReadWrite(managedIndirectArguments, infernux::rhi::PipelineStage::ComputeShader);
        return [&](RenderContext &context) {
            managedViewCuller.RecordCull(context.GetComputeCommandEncoder(), managedFrustum);
        };
    });
    resources.graph.AddComputePass("ManagedParticleCull/Finalize", [&](PassBuilder &builder) {
        managedIndirectArguments =
            builder.ReadWrite(managedIndirectArguments, infernux::rhi::PipelineStage::ComputeShader);
        managedSortDispatchArguments = builder.WriteStorageBuffer(managedSortDispatchArguments);
        return [&](RenderContext &context) { managedViewCuller.RecordFinalize(context.GetComputeCommandEncoder()); };
    });
    resources.graph.AddComputePass("OffscreenParticleCull/Reset", [&](PassBuilder &builder) {
        const uint64_t elementBytes = static_cast<uint64_t>(managedEntry.capacity) * sizeof(uint32_t);
        offscreenInstances = builder.ImportBuffer("OffscreenParticleInstances", managedEntry.instances,
                                                  static_cast<uint64_t>(managedEntry.capacity) *
                                                      infernux::particle::ParticleGpuRuntime::RenderInstanceStride);
        offscreenSourceIndirectArguments =
            builder.ImportBuffer("OffscreenParticleSourceIndirect", managedEntry.indirectArguments, 16);
        offscreenBounds = builder.ImportBuffer("OffscreenParticleBounds", managedEntry.bounds,
                                               infernux::particle::ParticleGpuBounds::BoundsBufferBytes);
        offscreenVisibleIndices = builder.ImportBuffer("OffscreenParticleVisibleIndices",
                                                       offscreenViewCuller.VisibleIndexBuffer(), elementBytes);
        offscreenIndirectArguments =
            builder.ImportBuffer("OffscreenParticleViewIndirect", offscreenViewCuller.DrawIndirectBuffer(), 16);
        offscreenSortDispatchArguments =
            builder.ImportBuffer("OffscreenParticleSortDispatch", offscreenViewCuller.SortDispatchBuffer(), 12);
        for (const auto handle : {offscreenInstances, offscreenSourceIndirectArguments, offscreenBounds})
            resources.graph.SetResourceInitialState(handle, infernux::rhi::TextureLayout::Undefined,
                                                    infernux::rhi::Access::ShaderWrite,
                                                    infernux::rhi::PipelineStage::ComputeShader);
        for (const auto handle : {offscreenVisibleIndices, offscreenIndirectArguments, offscreenSortDispatchArguments})
            resources.graph.SetResourceInitialState(handle, infernux::rhi::TextureLayout::Undefined,
                                                    infernux::rhi::Access::ShaderWrite,
                                                    infernux::rhi::PipelineStage::ComputeShader);
        builder.ReadStorageBuffer(offscreenSourceIndirectArguments);
        builder.ReadStorageBuffer(offscreenBounds);
        offscreenIndirectArguments = builder.WriteStorageBuffer(offscreenIndirectArguments);
        offscreenSortDispatchArguments = builder.WriteStorageBuffer(offscreenSortDispatchArguments);
        return [&](RenderContext &context) {
            offscreenViewCuller.RecordReset(context.GetComputeCommandEncoder(), offscreenFrustum);
        };
    });
    resources.graph.AddComputePass("OffscreenParticleCull/Cull", [&](PassBuilder &builder) {
        builder.ReadStorageBuffer(offscreenInstances);
        builder.ReadStorageBuffer(offscreenSourceIndirectArguments);
        builder.ReadIndirectBuffer(offscreenSortDispatchArguments);
        offscreenVisibleIndices = builder.WriteStorageBuffer(offscreenVisibleIndices);
        offscreenIndirectArguments =
            builder.ReadWrite(offscreenIndirectArguments, infernux::rhi::PipelineStage::ComputeShader);
        return [&](RenderContext &context) {
            offscreenViewCuller.RecordCull(context.GetComputeCommandEncoder(), offscreenFrustum);
        };
    });
    resources.graph.AddComputePass("ManagedParticleSort/Generate", [&](PassBuilder &builder) {
        const uint64_t elementBytes = static_cast<uint64_t>(managedEntry.capacity) * sizeof(uint32_t);
        const uint64_t blockBytes = static_cast<uint64_t>(managedViewSorter.BlockCount()) *
                                    infernux::particle::ParticleGpuSorter::Radix * sizeof(uint32_t);
        managedSortKeys = {
            builder.ImportBuffer("ManagedParticleSortKeys0", managedViewSorter.KeyBuffer(0), elementBytes),
            builder.ImportBuffer("ManagedParticleSortKeys1", managedViewSorter.KeyBuffer(1), elementBytes),
        };
        managedSortIndices = {
            builder.ImportBuffer("ManagedParticleSortIndices0", managedViewSorter.IndexBuffer(0), elementBytes),
            builder.ImportBuffer("ManagedParticleSortIndices1", managedViewSorter.IndexBuffer(1), elementBytes),
        };
        managedSortHistograms =
            builder.ImportBuffer("ManagedParticleSortHistograms", managedViewSorter.HistogramBuffer(), blockBytes);
        managedSortBlockOffsets =
            builder.ImportBuffer("ManagedParticleSortBlockOffsets", managedViewSorter.BlockOffsetBuffer(), blockBytes);
        managedSortGlobalOffsets =
            builder.ImportBuffer("ManagedParticleSortGlobalOffsets", managedViewSorter.GlobalOffsetBuffer(),
                                 infernux::particle::ParticleGpuSorter::Radix * sizeof(uint32_t));
        resources.graph.SetResourceInitialState(managedSortKeys[0], infernux::rhi::TextureLayout::Undefined,
                                                infernux::rhi::Access::ShaderWrite,
                                                infernux::rhi::PipelineStage::ComputeShader);
        resources.graph.SetResourceInitialState(managedSortKeys[1], infernux::rhi::TextureLayout::Undefined,
                                                infernux::rhi::Access::ShaderRead,
                                                infernux::rhi::PipelineStage::ComputeShader);
        resources.graph.SetResourceInitialState(managedSortIndices[0], infernux::rhi::TextureLayout::Undefined,
                                                infernux::rhi::Access::ShaderRead,
                                                infernux::rhi::PipelineStage::VertexShader);
        resources.graph.SetResourceInitialState(managedSortIndices[1], infernux::rhi::TextureLayout::Undefined,
                                                infernux::rhi::Access::ShaderRead,
                                                infernux::rhi::PipelineStage::ComputeShader);
        for (const auto handle : {managedSortHistograms, managedSortBlockOffsets, managedSortGlobalOffsets})
            resources.graph.SetResourceInitialState(handle, infernux::rhi::TextureLayout::Undefined,
                                                    infernux::rhi::Access::ShaderRead,
                                                    infernux::rhi::PipelineStage::ComputeShader);
        builder.ReadStorageBuffer(managedInstances);
        builder.ReadStorageBuffer(managedIndirectArguments);
        builder.ReadStorageBuffer(managedVisibleIndices);
        builder.ReadIndirectBuffer(managedSortDispatchArguments);
        managedSortKeys[0] = builder.WriteStorageBuffer(managedSortKeys[0]);
        managedSortIndices[0] = builder.WriteStorageBuffer(managedSortIndices[0]);
        return [&](RenderContext &context) {
            std::array<float, 16> view{};
            view[0] = view[5] = view[10] = view[15] = 1.0f;
            managedViewSorter.RecordGenerate(context.GetComputeCommandEncoder(), view,
                                             infernux::particle::ParticleSortMode::FrontToBack);
        };
    });
    for (uint32_t passIndex = 0; passIndex < infernux::particle::ParticleGpuSorter::PassCount; ++passIndex) {
        const uint32_t input = passIndex % 2u;
        const uint32_t output = 1u - input;
        const std::string prefix = "ManagedParticleSort/Radix" + std::to_string(passIndex);
        resources.graph.AddComputePass(prefix + "/Histogram", [&, passIndex, input](PassBuilder &builder) {
            builder.ReadStorageBuffer(managedIndirectArguments);
            builder.ReadStorageBuffer(managedSortKeys[input]);
            builder.ReadIndirectBuffer(managedSortDispatchArguments);
            managedSortHistograms = builder.WriteStorageBuffer(managedSortHistograms);
            return [&, passIndex](RenderContext &context) {
                managedViewSorter.RecordHistogram(context.GetComputeCommandEncoder(), passIndex);
            };
        });
        resources.graph.AddComputePass(prefix + "/Scan", [&, passIndex](PassBuilder &builder) {
            builder.ReadStorageBuffer(managedSortHistograms);
            builder.ReadStorageBuffer(managedSortDispatchArguments);
            managedSortBlockOffsets = builder.WriteStorageBuffer(managedSortBlockOffsets);
            managedSortGlobalOffsets = builder.WriteStorageBuffer(managedSortGlobalOffsets);
            return [&, passIndex](RenderContext &context) {
                managedViewSorter.RecordScan(context.GetComputeCommandEncoder(), passIndex);
            };
        });
        resources.graph.AddComputePass(prefix + "/Scatter", [&, passIndex, input, output](PassBuilder &builder) {
            builder.ReadStorageBuffer(managedIndirectArguments);
            builder.ReadStorageBuffer(managedSortKeys[input]);
            builder.ReadStorageBuffer(managedSortIndices[input]);
            builder.ReadStorageBuffer(managedSortBlockOffsets);
            builder.ReadStorageBuffer(managedSortGlobalOffsets);
            builder.ReadIndirectBuffer(managedSortDispatchArguments);
            managedSortKeys[output] = builder.WriteStorageBuffer(managedSortKeys[output]);
            managedSortIndices[output] = builder.WriteStorageBuffer(managedSortIndices[output]);
            return [&, passIndex](RenderContext &context) {
                managedViewSorter.RecordScatter(context.GetComputeCommandEncoder(), passIndex);
            };
        });
    }

    resources.graph.AddPass("ParticleTexture", [&](PassBuilder &builder) {
        particleTexture = builder.CreateTexture("ParticleTexture", 1, 1, VK_FORMAT_R8G8B8A8_UNORM);
        particleTexture = builder.WriteColor(particleTexture);
        builder.SetRenderArea(1, 1);
        builder.SetClearColor(1.0f, 1.0f, 1.0f, 1.0f);
        return [](RenderContext &) {};
    });

    resources.graph.AddPass("IndirectDraw", [&](PassBuilder &builder) {
        builder.Read(particleTexture);
        builder.ReadIndirectBuffer(copiedIndirectArguments);
        builder.ReadStorageBuffer(particleOutputs.instances, infernux::rhi::PipelineStage::VertexShader);
        builder.ReadIndirectBuffer(particleOutputs.indirectArguments);
        builder.ReadStorageBuffer(managedInstances, infernux::rhi::PipelineStage::VertexShader);
        builder.ReadStorageBuffer(managedSortIndices[0], infernux::rhi::PipelineStage::VertexShader);
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
            billboardRecorded = managedRenderer->RecordDraw(encoder, billboardTargetLayout, billboardPass,
                                                            context.GetBufferHandle(managedIndirectArguments),
                                                            billboardView, managedViewSorter.SortedIndices()) &&
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

    infernux::rhi::BindingLayoutDesc sampledTextureLayoutDesc;
    sampledTextureLayoutDesc.entries[0] = {0, infernux::rhi::BindingType::CombinedTextureSampler,
                                           infernux::rhi::ShaderStage::Fragment, 1};
    sampledTextureLayoutDesc.entryCount = 1;
    resources.sampledTextureBindingLayout = rhi.CreateBindingLayout(sampledTextureLayoutDesc);
    VkSamplerCreateInfo samplerInfo{};
    samplerInfo.sType = VK_STRUCTURE_TYPE_SAMPLER_CREATE_INFO;
    samplerInfo.magFilter = VK_FILTER_LINEAR;
    samplerInfo.minFilter = VK_FILTER_LINEAR;
    samplerInfo.mipmapMode = VK_SAMPLER_MIPMAP_MODE_LINEAR;
    samplerInfo.addressModeU = VK_SAMPLER_ADDRESS_MODE_REPEAT;
    samplerInfo.addressModeV = VK_SAMPLER_ADDRESS_MODE_REPEAT;
    samplerInfo.addressModeW = VK_SAMPLER_ADDRESS_MODE_REPEAT;
    samplerInfo.maxLod = VK_LOD_CLAMP_NONE;
    if (!Require(resources.sampledTextureBindingLayout.IsValid() &&
                     vkCreateSampler(device, &samplerInfo, nullptr, &resources.sampledTextureSampler) == VK_SUCCESS,
                 "RHI sampled texture layout or Vulkan sampler creation failed"))
        return false;
    resources.sampledTextureSamplerHandle = rhi.RegisterSampler(resources.sampledTextureSampler);
    infernux::rhi::BindGroupDesc sampledTextureGroupDesc;
    sampledTextureGroupDesc.layout = resources.sampledTextureBindingLayout;
    sampledTextureGroupDesc.textures[0] = {0, infernux::rhi::BindingType::CombinedTextureSampler,
                                           resources.graph.ResolveRhiTextureView(particleTexture),
                                           resources.sampledTextureSamplerHandle, false};
    sampledTextureGroupDesc.textureCount = 1;
    resources.sampledTextureGroup = rhi.CreateBindGroup(sampledTextureGroupDesc);
    if (!Require(resources.sampledTextureGroup.IsValid(), "RHI combined texture sampler bind group creation failed"))
        return false;

    const auto executionNames = resources.graph.GetExecutionPassNames();
    if (!Require(
            executionNames.size() == 43 && executionNames[0] == "BuildIndirectArguments" &&
                executionNames[1] == "CopyIndirectArguments" && executionNames[2] == "Particle/TestEmitter/Bootstrap" &&
                executionNames[3] == "Particle/TestEmitter/Init" &&
                executionNames[4] == "Particle/TestEmitter/Update" &&
                executionNames[5] == "Particle/TestEmitter/RenderReset" &&
                executionNames[6] == "Particle/TestEmitter/Rendering" &&
                executionNames[7] == "Particle/TestEmitter/BoundsReset" &&
                executionNames[8] == "Particle/TestEmitter/BoundsReduce" &&
                executionNames[9] == "ManagedParticleCull/Reset" && executionNames[10] == "ManagedParticleCull/Cull" &&
                executionNames[11] == "ManagedParticleCull/Finalize" &&
                executionNames[12] == "OffscreenParticleCull/Reset" &&
                executionNames[13] == "OffscreenParticleCull/Cull" &&
                executionNames[14] == "ManagedParticleSort/Generate" &&
                executionNames[38] == "ManagedParticleSort/Radix7/Scatter" && executionNames[39] == "ParticleTexture" &&
                executionNames[40] == "IndirectDraw" && executionNames[41] == "SkipEmptyRendererList" &&
                executionNames[42] == "RunPopulatedRendererList",
            "Versioned cull/sort broke the compute-to-indirect draw dependency"))
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

    infernux::rhi::TextureDesc texture2DDesc;
    texture2DDesc.width = 16;
    texture2DDesc.height = 8;
    texture2DDesc.mipLevels = 5;
    texture2DDesc.format = infernux::rhi::PixelFormat::RGBA8UNorm;
    texture2DDesc.usage =
        infernux::rhi::TextureUsageFlags::Sampled | infernux::rhi::TextureUsageFlags::TransferDestination;
    const auto texture2D = deviceApi.CreateTexture(texture2DDesc);
    infernux::rhi::TextureViewDesc texture2DViewDesc;
    texture2DViewDesc.texture = texture2D;
    texture2DViewDesc.mipCount = texture2DDesc.mipLevels;
    const auto texture2DView = deviceApi.CreateTextureView(texture2DViewDesc);

    infernux::rhi::TextureDesc texture3DDesc;
    texture3DDesc.dimension = infernux::rhi::TextureDimension::Texture3D;
    texture3DDesc.width = 8;
    texture3DDesc.height = 4;
    texture3DDesc.depthOrLayers = 4;
    texture3DDesc.mipLevels = 3;
    texture3DDesc.format = infernux::rhi::PixelFormat::RGBA16SFloat;
    texture3DDesc.usage =
        infernux::rhi::TextureUsageFlags::Sampled | infernux::rhi::TextureUsageFlags::TransferDestination;
    const auto texture3D = deviceApi.CreateTexture(texture3DDesc);
    infernux::rhi::TextureViewDesc texture3DViewDesc;
    texture3DViewDesc.texture = texture3D;
    texture3DViewDesc.dimension = infernux::rhi::TextureViewDimension::Texture3D;
    texture3DViewDesc.mipCount = texture3DDesc.mipLevels;
    const auto texture3DView = deviceApi.CreateTextureView(texture3DViewDesc);

    infernux::rhi::SamplerDesc textureSamplerDesc;
    textureSamplerDesc.addressU = infernux::rhi::AddressMode::ClampToEdge;
    textureSamplerDesc.addressV = infernux::rhi::AddressMode::ClampToEdge;
    textureSamplerDesc.addressW = infernux::rhi::AddressMode::ClampToEdge;
    textureSamplerDesc.maxLod = 4.0f;
    const auto textureSampler = deviceApi.CreateSampler(textureSamplerDesc);
    if (!Require(texture2D.IsValid() && texture2DView.IsValid() && texture3D.IsValid() && texture3DView.IsValid() &&
                     textureSampler.IsValid(),
                 "RHI Texture2D/Texture3D/view/sampler creation failed"))
        return false;
    deviceApi.Release(textureSampler);
    deviceApi.Release(texture3DView);
    deviceApi.Release(texture3D);
    deviceApi.Release(texture2DView);
    deviceApi.Release(texture2D);

    const std::array<uint8_t, 4 * 4 * 4> upload2DBytes{};
    const infernux::rhi::TextureSubresourceUpload upload2DRegion = {
        upload2DBytes.data(), upload2DBytes.size(), 0, 0, 1, 4, 4, 1, 16, 64};
    infernux::rhi::TextureUploadRequest upload2DRequest;
    upload2DRequest.texture.width = 4;
    upload2DRequest.texture.height = 4;
    upload2DRequest.texture.format = infernux::rhi::PixelFormat::RGBA8UNorm;
    upload2DRequest.texture.usage =
        infernux::rhi::TextureUsageFlags::Sampled | infernux::rhi::TextureUsageFlags::TransferDestination;
    upload2DRequest.subresources = &upload2DRegion;
    upload2DRequest.subresourceCount = 1;
    const auto upload2DTicket = resources.resources.BeginTextureUpload(upload2DRequest);

    const std::array<uint8_t, 2 * 2 * 2 * 8> upload3DBytes{};
    const infernux::rhi::TextureSubresourceUpload upload3DRegion = {
        upload3DBytes.data(), upload3DBytes.size(), 0, 0, 1, 2, 2, 2, 16, 32};
    infernux::rhi::TextureUploadRequest upload3DRequest;
    upload3DRequest.texture.dimension = infernux::rhi::TextureDimension::Texture3D;
    upload3DRequest.texture.width = 2;
    upload3DRequest.texture.height = 2;
    upload3DRequest.texture.depthOrLayers = 2;
    upload3DRequest.texture.format = infernux::rhi::PixelFormat::RGBA16SFloat;
    upload3DRequest.texture.usage =
        infernux::rhi::TextureUsageFlags::Sampled | infernux::rhi::TextureUsageFlags::TransferDestination;
    upload3DRequest.view.dimension = infernux::rhi::TextureViewDimension::Texture3D;
    upload3DRequest.subresources = &upload3DRegion;
    upload3DRequest.subresourceCount = 1;
    const auto upload3DTicket = resources.resources.BeginTextureUpload(upload3DRequest);
    if (!Require(upload2DTicket->IsPublished() && upload2DTicket->GetTexture()->IsValid() &&
                     upload3DTicket->IsPublished() && upload3DTicket->GetTexture()->IsValid() &&
                     rhi.Resolve(upload2DTicket->GetTexture()->GetTexture()) != VK_NULL_HANDLE &&
                     rhi.Resolve(upload3DTicket->GetTexture()->GetTexture()) != VK_NULL_HANDLE,
                 "RHI Texture2D/Texture3D staging uploads failed"))
        return false;

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
    billboardDesc.vertexShader = {particleTexturedVertexCode.data(), particleTexturedVertexCode.size()};
    billboardDesc.fragmentShader = {particleTexturedFragmentCode.data(), particleTexturedFragmentCode.size()};
    billboardDesc.instances = particleRuntime.InstanceBuffer();
    billboardDesc.renderIndices = particleRuntime.RenderIndexBuffer();
    billboardDesc.fallbackMaterial.blendEnabled = false;
    billboardDesc.textureResolver = [&](const std::string &, const std::string &bindingName) {
        if (bindingName != "texSampler")
            return infernux::particle::GpuBillboardTextureLease{};
        const auto texture = rhi.RegisterTextureView(resources.graph.ResolveTextureView(particleTexture));
        const auto sampler = rhi.RegisterSampler(resources.sampledTextureSampler);
        return infernux::particle::GpuBillboardTextureLease{infernux::particle::GpuBillboardTextureStatus::Ready,
                                                            texture, sampler, std::make_shared<uint32_t>(1), true};
    };
    billboardDesc.textureVersionResolver = [](const std::string &) { return uint64_t{1}; };
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
    BufferReadback particleReadback;
    if (!Require(particleReadback.Create(resources.context.GetVmaAllocator(), 56),
                 "Particle cull readback buffer creation failed"))
        return false;
    const std::array<infernux::rhi::BufferHandle, 4> cullReadbackSources = {
        managedViewCuller.DrawIndirectBuffer(),
        managedViewCuller.SortDispatchBuffer(),
        offscreenViewCuller.DrawIndirectBuffer(),
        offscreenViewCuller.SortDispatchBuffer(),
    };
    std::array<VkBufferMemoryBarrier, 4> cullReadbackBarriers{};
    for (size_t index = 0; index < cullReadbackSources.size(); ++index) {
        auto &barrier = cullReadbackBarriers[index];
        barrier.sType = VK_STRUCTURE_TYPE_BUFFER_MEMORY_BARRIER;
        barrier.srcAccessMask = VK_ACCESS_SHADER_WRITE_BIT | VK_ACCESS_INDIRECT_COMMAND_READ_BIT;
        barrier.dstAccessMask = VK_ACCESS_TRANSFER_READ_BIT;
        barrier.srcQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        barrier.dstQueueFamilyIndex = VK_QUEUE_FAMILY_IGNORED;
        barrier.buffer = rhi.Resolve(cullReadbackSources[index]);
        barrier.offset = 0;
        barrier.size = VK_WHOLE_SIZE;
    }
    vkCmdPipelineBarrier(commandBuffer, VK_PIPELINE_STAGE_COMPUTE_SHADER_BIT | VK_PIPELINE_STAGE_DRAW_INDIRECT_BIT,
                         VK_PIPELINE_STAGE_TRANSFER_BIT, 0, 0, nullptr,
                         static_cast<uint32_t>(cullReadbackBarriers.size()), cullReadbackBarriers.data(), 0, nullptr);
    const std::array<VkDeviceSize, 4> cullReadbackOffsets = {0, 16, 28, 44};
    const std::array<VkDeviceSize, 4> cullReadbackBytes = {16, 12, 16, 12};
    for (size_t index = 0; index < cullReadbackSources.size(); ++index) {
        const VkBufferCopy copy{0, cullReadbackOffsets[index], cullReadbackBytes[index]};
        vkCmdCopyBuffer(commandBuffer, cullReadbackBarriers[index].buffer, particleReadback.buffer, 1, &copy);
    }
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
    if (vmaInvalidateAllocation(particleReadback.allocator, particleReadback.allocation, 0,
                                particleReadback.byteSize) != VK_SUCCESS)
        return false;
    std::array<uint32_t, 4> viewCounts{};
    std::memcpy(&viewCounts[0], static_cast<const uint8_t *>(particleReadback.mapped) + 4, sizeof(uint32_t));
    std::memcpy(&viewCounts[1], static_cast<const uint8_t *>(particleReadback.mapped) + 16, sizeof(uint32_t));
    std::memcpy(&viewCounts[2], static_cast<const uint8_t *>(particleReadback.mapped) + 32, sizeof(uint32_t));
    std::memcpy(&viewCounts[3], static_cast<const uint8_t *>(particleReadback.mapped) + 44, sizeof(uint32_t));
    if (!Require(viewCounts == std::array<uint32_t, 4>{1, 1, 0, 0},
                 "Per-view particle cull workspaces did not preserve visible/offscreen indirect counts"))
        return false;

    auto executeManagedFrame = [&](const char *failureMessage) {
        if (vkResetCommandPool(device, resources.commandPool, 0) != VK_SUCCESS)
            return Require(false, failureMessage);
        VkCommandBufferBeginInfo managerBeginInfo{};
        managerBeginInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;
        managerBeginInfo.flags = VK_COMMAND_BUFFER_USAGE_ONE_TIME_SUBMIT_BIT;
        if (vkBeginCommandBuffer(commandBuffer, &managerBeginInfo) != VK_SUCCESS)
            return Require(false, failureMessage);
        particleSystems.Execute(commandBuffer);
        if (vkEndCommandBuffer(commandBuffer) != VK_SUCCESS)
            return Require(false, failureMessage);
        VkFence managerFence = VK_NULL_HANDLE;
        VkFenceCreateInfo managerFenceInfo{};
        managerFenceInfo.sType = VK_STRUCTURE_TYPE_FENCE_CREATE_INFO;
        if (vkCreateFence(device, &managerFenceInfo, nullptr, &managerFence) != VK_SUCCESS)
            return Require(false, failureMessage);
        VkSubmitInfo managerSubmitInfo{};
        managerSubmitInfo.sType = VK_STRUCTURE_TYPE_SUBMIT_INFO;
        managerSubmitInfo.commandBufferCount = 1;
        managerSubmitInfo.pCommandBuffers = &commandBuffer;
        const VkResult managerSubmit =
            vkQueueSubmit(resources.context.GetGraphicsQueue(), 1, &managerSubmitInfo, managerFence);
        const VkResult managerWait = managerSubmit == VK_SUCCESS
                                         ? vkWaitForFences(device, 1, &managerFence, VK_TRUE, UINT64_MAX)
                                         : managerSubmit;
        vkDestroyFence(device, managerFence, nullptr);
        return Require(managerSubmit == VK_SUCCESS && managerWait == VK_SUCCESS, failureMessage);
    };

    const auto preMigrationEntries = particleDrawRegistry.Snapshot(3000, 3100);
    const auto preMigrationInstances = preMigrationEntries.front().instances;
    managedProgram.artifactRevision = 4;
    managedProgram.capacity = 64;
    infernux::particle::GpuParticleEmitterProgram::StateMigration managerMigration;
    managerMigration.sourceStride = 16;
    managerMigration.destinationStride = 16;
    managerMigration.copyRanges = {{2, 2, 2, 0}};
    managerMigration.defaultStateWords = {0, 0, 0, 0};
    managedProgram.migration = std::move(managerMigration);
    if (!Require(
            publishManagedGraph({managedProgram}) && particleSystems.ActiveArtifactRevision(managedProgram.id) == 4 &&
                particleSystems.ActiveStateWasPreserved(managedProgram.id) && particleDeletionQueue.PendingCount() == 4,
            "GPU particle manager rejected a layout-migratable revision"))
        return false;
    const auto migratingEntries = particleDrawRegistry.Snapshot(3000, 3100);
    if (!Require(migratingEntries.size() == 2 && migratingEntries.front().capacity == 64 &&
                     migratingEntries.front().instances != preMigrationInstances,
                 "GPU particle manager reused incompatible resident buffers during layout migration"))
        return false;

    auto migrationFrame = managedFrame;
    migrationFrame.frameIndex = 44;
    migrationFrame.spawnCount = 0;
    migrationFrame.simulate = false;
    migrationFrame.render = false;
    if (!Require(particleSystems.BeginFrame(managedProgram.id, migrationFrame, managedTransforms),
                 "GPU particle manager rejected the migration boundary frame") ||
        !executeManagedFrame("GPU particle manager migration frame failed") ||
        !Require(particleDeletionQueue.PendingCount() == 5,
                 "GPU particle manager did not retire the migration graph and source at the frame boundary"))
        return false;

    particleDeletionQueue.FlushAll();
    auto postMigrationFrame = migrationFrame;
    postMigrationFrame.frameIndex = 45;
    if (!Require(particleSystems.BeginFrame(managedProgram.id, postMigrationFrame, managedTransforms),
                 "GPU particle manager rejected the post-migration frame") ||
        !executeManagedFrame("GPU particle manager post-migration graph failed") ||
        !Require(particleDeletionQueue.PendingCount() == 0,
                 "GPU particle manager retained migration resources after graph replacement"))
        return false;

    if (!Require(!particleSystems.BeginFrame(managedProgram.id, postMigrationFrame, managedTransforms),
                 "GPU particle manager accepted the same engine frame twice"))
        return false;

    infernux::particle::GpuParticleGraphProgram managedGraphProgram;
    managedGraphProgram.graphInstanceId = managedProgram.graphInstanceId;
    managedGraphProgram.emitters = {managedProgram};
    infernux::particle::GpuParticleEventDomainDesc managedEvents;
    managedEvents.graphInstanceId = managedProgram.graphInstanceId;
    managedEvents.eventAbiHash = 0xfeed1234u;
    managedEvents.framesInFlight = 2;
    managedEvents.channels.push_back({0x44u, 0, 0, 0, 3, 64});
    managedGraphProgram.eventDomain = managedEvents;
    if (!Require(particleSystems.ApplyGraph(managedGraphProgram, &managedError) &&
                     particleSystems.ActiveEventAbiHash(managedProgram.graphInstanceId) == managedEvents.eventAbiHash &&
                     particleSystems.ActiveEventPageCount(managedProgram.graphInstanceId) == 2,
                 "GPU particle graph event domain was not published atomically"))
        return false;
    auto eventFrame = postMigrationFrame;
    eventFrame.frameIndex = 46;
    if (!Require(particleSystems.BeginFrame(managedProgram.id, eventFrame, managedTransforms),
                 "GPU particle manager rejected an event-domain frame") ||
        !executeManagedFrame("GPU particle event preparation frame failed"))
        return false;

    auto foreignGraphProgram = managedGraphProgram;
    foreignGraphProgram.graphInstanceId += 1;
    foreignGraphProgram.eventDomain.reset();
    foreignGraphProgram.removeEmitterIds = {managedProgram.id};
    if (!Require(!particleSystems.ApplyGraph(foreignGraphProgram, &managedError) &&
                     particleSystems.Contains(managedProgram.id) &&
                     particleSystems.ActiveEventAbiHash(managedProgram.graphInstanceId) == managedEvents.eventAbiHash,
                 "GPU particle graph crossed another graph's emitter ownership boundary"))
        return false;

    managedEvents.eventAbiHash += 1;
    managedGraphProgram.eventDomain = managedEvents;
    if (!Require(particleSystems.ApplyGraph(managedGraphProgram, &managedError) &&
                     particleSystems.ActiveEventAbiHash(managedProgram.graphInstanceId) == managedEvents.eventAbiHash,
                 "GPU particle event ABI replacement did not publish the new domain"))
        return false;
    particleDeletionQueue.FlushAll();
    if (!Require(publishManagedGraph({}, {managedProgram.id}) && particleSystems.Size() == 0 &&
                     particleDrawRegistry.Size() == 0 && particleDeletionQueue.PendingCount() == 1,
                 "GPU particle manager removal did not retire graph resources"))
        return false;
    particleSystems.Shutdown();
    particleDeletionQueue.FlushAll();

    billboardRenderer.Destroy();
    resources.graph.Destroy();
    managedViewSorter.Destroy();
    offscreenViewCuller.Destroy();
    managedViewCuller.Destroy();
    particleBounds.Destroy();
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

    RenderGraph sampledDepthGraph;
    sampledDepthGraph.Initialize(&resources.context, &resources.pipelines);
    ResourceHandle sceneDepth;
    ResourceHandle softParticleColor;
    sampledDepthGraph.AddPass("WriteSceneDepth", [&](PassBuilder &builder) {
        sceneDepth = builder.CreateDepthStencil("SoftParticleDepth", 8, 8, VK_FORMAT_D32_SFLOAT);
        sceneDepth = builder.WriteDepth(sceneDepth);
        builder.SetRenderArea(8, 8);
        builder.SetClearDepth(1.0f, 0);
        return [](RenderContext &) {};
    });
    sampledDepthGraph.AddPass("ReadDepthAndSampleForParticles", [&](PassBuilder &builder) {
        builder.ReadDepth(sceneDepth);
        builder.ReadSampledDepth(sceneDepth, infernux::rhi::PipelineStage::FragmentShader);
        softParticleColor = builder.CreateTexture("SoftParticleColor", 8, 8, VK_FORMAT_R8G8B8A8_UNORM);
        softParticleColor = builder.WriteColor(softParticleColor);
        builder.SetRenderArea(8, 8);
        return [](RenderContext &) {};
    });
    sampledDepthGraph.SetOutput(softParticleColor);
    if (!Require(sampledDepthGraph.Compile(),
                 "Read-only depth attachment could not also be sampled by a soft-particle pass"))
        return false;
    if (!Require(sampledDepthGraph.GetExecutionPassNames() ==
                     std::vector<std::string>{"WriteSceneDepth", "ReadDepthAndSampleForParticles"},
                 "Soft-particle scene-depth dependency did not retain its producer"))
        return false;

    RenderGraph resolvedDepthGraph;
    resolvedDepthGraph.Initialize(&resources.context, &resources.pipelines);
    ResourceHandle multisampledDepth;
    ResourceHandle resolvedDepth;
    ResourceHandle resolvedDepthOutput;
    resolvedDepthGraph.AddPass("WriteMultisampledDepth", [&](PassBuilder &builder) {
        multisampledDepth =
            builder.CreateDepthStencil("MultisampledDepth", 8, 8, VK_FORMAT_D32_SFLOAT, VK_SAMPLE_COUNT_4_BIT);
        multisampledDepth = builder.WriteDepth(multisampledDepth);
        builder.SetRenderArea(8, 8);
        builder.SetClearDepth(1.0f, 0);
        return [](RenderContext &) {};
    });
    resolvedDepthGraph.AddComputePass("ResolveDepth", [&](PassBuilder &builder) {
        builder.ReadSampledDepth(multisampledDepth, infernux::rhi::PipelineStage::ComputeShader);
        resolvedDepth = builder.CreateTexture("ResolvedDepth", 8, 8, VK_FORMAT_R32_SFLOAT);
        resolvedDepth = builder.WriteStorageTexture(resolvedDepth);
        return [](RenderContext &) {};
    });
    resolvedDepthGraph.AddPass("SampleResolvedDepth", [&](PassBuilder &builder) {
        builder.Read(resolvedDepth, infernux::rhi::PipelineStage::FragmentShader);
        resolvedDepthOutput = builder.CreateTexture("ResolvedDepthOutput", 8, 8, VK_FORMAT_R8G8B8A8_UNORM);
        resolvedDepthOutput = builder.WriteColor(resolvedDepthOutput);
        builder.SetRenderArea(8, 8);
        return [](RenderContext &) {};
    });
    resolvedDepthGraph.SetOutput(resolvedDepthOutput);
    if (!Require(resolvedDepthGraph.Compile(),
                 "MSAA scene depth could not resolve through a backend-neutral storage texture"))
        return false;
    if (!Require(resolvedDepthGraph.GetExecutionPassNames() ==
                     std::vector<std::string>{"WriteMultisampledDepth", "ResolveDepth", "SampleResolvedDepth"},
                 "Resolved scene-depth dependency did not retain its compute producer"))
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
    if (argc != 10) {
        std::cerr << "Expected compute, vertex, fragment, reflection, and particle SPIR-V paths\n";
        return 2;
    }
    return Run(argv[1], argv[2], argv[3], argv[4], argv[5], argv[6], argv[7], argv[8], argv[9]) ? 0 : 1;
}
