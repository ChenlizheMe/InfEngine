/**
 * @file InxScreenUIRenderer.cpp
 * @brief Implementation of GPU-based 2D screen-space UI renderer
 *
 * Uses ImGui's ImDrawList for command accumulation (provides text rendering
 * via font atlas for free) and renders with a standalone Vulkan pipeline
 * inside the scene render graph's MSAA render passes.
 *
 * The pipeline is a direct replica of ImGui's internal 2D pipeline:
 *   - Vertex format: ImDrawVert (pos, uv, col)
 *   - Push constants: vec2 scale + vec2 translate (orthographic projection)
 *   - Descriptor: single combined_image_sampler (font atlas)
 *   - Alpha blending, no depth test, no cull
 *
 * SPIR-V bytecode is identical to Dear ImGui's (MIT licensed).
 */

#include "InxScreenUIRenderer.h"
#include "InxTextLayout.h"
#include <algorithm>
#include <array>
#include <cmath>
#include <core/log/InxLog.h>
#include <cstring>
#include <function/renderer/vk/DescriptorBindTrace.h>
#include <function/renderer/vk/VkPipelineHelpers.h>
#include <function/renderer/vk/VkRenderUtils.h>
#include <imgui_internal.h> // for ImGui::GetDrawListSharedData()
#include <type_traits>

namespace infernux
{

namespace
{
constexpr float kTransformRotationEpsilon = 0.001f;
constexpr float kPi = 3.14159265358979f;
constexpr const char *kShaderEntryPoint = "main";
constexpr uint32_t kFontTextureBinding = 0;

struct VertexTransform
{
    ImVec2 pivot{};
    float cosAngle = 1.0f;
    float sinAngle = 0.0f;
    bool mirrorH = false;
    bool mirrorV = false;
    bool enabled = false;
};

float ResolveFontSize(float fontSize)
{
    return textlayout::ResolveFontSize(fontSize);
}

float ExtractHDRScale(float &r, float &g, float &b)
{
    const float maxRGB = std::max(r, std::max(g, b));
    if (maxRGB <= 1.0f) {
        return 1.0f;
    }

    r /= maxRGB;
    g /= maxRGB;
    b /= maxRGB;
    return maxRGB;
}

ImTextureID ToImTextureID(uint64_t textureId)
{
    if constexpr (std::is_pointer_v<ImTextureID>) {
        return (ImTextureID)(static_cast<uintptr_t>(textureId));
    }
    return static_cast<ImTextureID>(textureId);
}

void ResetDrawListForFrame(ImDrawList &drawList, uint32_t width, uint32_t height)
{
    drawList._ResetForNewFrame();
    drawList.PushTextureID(ImGui::GetIO().Fonts->TexRef.GetTexID());
    drawList.PushClipRect(ImVec2(0.0f, 0.0f), ImVec2(static_cast<float>(width), static_cast<float>(height)));
}

float NormalizeRotationDegrees(float rotation)
{
    rotation = std::fmod(rotation, 360.0f);
    if (rotation < 0.0f)
        rotation += 360.0f;
    return rotation;
}

VertexTransform MakeVertexTransform(float minX, float minY, float maxX, float maxY, float rotation, bool mirrorH,
                                    bool mirrorV)
{
    rotation = NormalizeRotationDegrees(rotation);

    VertexTransform transform{};
    transform.enabled = mirrorH || mirrorV || std::fabs(rotation) >= kTransformRotationEpsilon;
    if (!transform.enabled) {
        return transform;
    }

    const float radians = rotation * kPi / 180.0f;
    transform.pivot = ImVec2((minX + maxX) * 0.5f, (minY + maxY) * 0.5f);
    transform.cosAngle = std::cos(radians);
    transform.sinAngle = std::sin(radians);
    transform.mirrorH = mirrorH;
    transform.mirrorV = mirrorV;
    return transform;
}

void ApplyVertexTransform(ImDrawList &drawList, int vertexStart, const VertexTransform &transform)
{
    if (!transform.enabled) {
        return;
    }

    for (int i = vertexStart; i < drawList.VtxBuffer.Size; ++i) {
        ImVec2 local(drawList.VtxBuffer[i].pos.x - transform.pivot.x, drawList.VtxBuffer[i].pos.y - transform.pivot.y);
        if (transform.mirrorH)
            local.x = -local.x;
        if (transform.mirrorV)
            local.y = -local.y;

        const float rx = local.x * transform.cosAngle - local.y * transform.sinAngle;
        const float ry = local.x * transform.sinAngle + local.y * transform.cosAngle;
        drawList.VtxBuffer[i].pos = ImVec2(transform.pivot.x + rx, transform.pivot.y + ry);
    }
}

bool RefreshFontDescriptorSet(VkDescriptorSet &descriptorSet)
{
    const ImTextureID texId = ImGui::GetIO().Fonts->TexRef.GetTexID();
    if (texId == 0) {
        return false;
    }

    descriptorSet = reinterpret_cast<VkDescriptorSet>(static_cast<uintptr_t>(texId));
    return true;
}

VkAttachmentDescription MakeColorAttachmentDescription(VkFormat format, VkSampleCountFlagBits samples,
                                                       VkImageLayout finalLayout)
{
    VkAttachmentDescription attachment{};
    attachment.format = format;
    attachment.samples = samples;
    attachment.loadOp = VK_ATTACHMENT_LOAD_OP_LOAD;
    attachment.storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    attachment.stencilLoadOp = VK_ATTACHMENT_LOAD_OP_DONT_CARE;
    attachment.stencilStoreOp = VK_ATTACHMENT_STORE_OP_DONT_CARE;
    attachment.initialLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    attachment.finalLayout = finalLayout;
    return attachment;
}

VkAttachmentReference MakeColorAttachmentReference(uint32_t attachmentIndex = 0)
{
    VkAttachmentReference colorRef{};
    colorRef.attachment = attachmentIndex;
    colorRef.layout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;
    return colorRef;
}

VkSubpassDescription MakeSingleColorSubpass(const VkAttachmentReference &colorRef)
{
    VkSubpassDescription subpass{};
    subpass.pipelineBindPoint = VK_PIPELINE_BIND_POINT_GRAPHICS;
    subpass.colorAttachmentCount = 1;
    subpass.pColorAttachments = &colorRef;
    return subpass;
}

VkViewport MakeViewport(uint32_t width, uint32_t height)
{
    VkViewport viewport{};
    viewport.width = static_cast<float>(width);
    viewport.height = static_cast<float>(height);
    viewport.minDepth = 0.0f;
    viewport.maxDepth = 1.0f;
    return viewport;
}

std::array<float, 4> MakeOrthoPushConstants(uint32_t width, uint32_t height)
{
    return {
        2.0f / static_cast<float>(width),
        2.0f / static_cast<float>(height),
        -1.0f,
        -1.0f,
    };
}

bool MakeClampedScissor(const ImDrawCmd &cmd, float frameWidth, float frameHeight, VkRect2D &outScissor)
{
    const float clipMinX = std::clamp(cmd.ClipRect.x, 0.0f, frameWidth);
    const float clipMinY = std::clamp(cmd.ClipRect.y, 0.0f, frameHeight);
    const float clipMaxX = std::clamp(cmd.ClipRect.z, 0.0f, frameWidth);
    const float clipMaxY = std::clamp(cmd.ClipRect.w, 0.0f, frameHeight);
    if (clipMaxX <= clipMinX || clipMaxY <= clipMinY) {
        return false;
    }

    outScissor.offset.x = static_cast<int32_t>(clipMinX);
    outScissor.offset.y = static_cast<int32_t>(clipMinY);
    outScissor.extent.width = static_cast<uint32_t>(clipMaxX - clipMinX);
    outScissor.extent.height = static_cast<uint32_t>(clipMaxY - clipMinY);
    return true;
}

bool CreateShaderModule(VkDevice device, const uint32_t *code, size_t codeSize, VkShaderModule &outModule)
{
    VkShaderModuleCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_SHADER_MODULE_CREATE_INFO;
    createInfo.codeSize = codeSize;
    createInfo.pCode = code;

    outModule = VK_NULL_HANDLE;
    return vkCreateShaderModule(device, &createInfo, nullptr, &outModule) == VK_SUCCESS;
}

bool CreatePipelineLayout(VkDevice device, VkDescriptorSetLayout descriptorSetLayout,
                          const VkPushConstantRange &pushConstantRange, VkPipelineLayout &outLayout)
{
    VkPipelineLayoutCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_LAYOUT_CREATE_INFO;
    createInfo.setLayoutCount = 1;
    createInfo.pSetLayouts = &descriptorSetLayout;
    createInfo.pushConstantRangeCount = 1;
    createInfo.pPushConstantRanges = &pushConstantRange;

    outLayout = VK_NULL_HANDLE;
    return vkCreatePipelineLayout(device, &createInfo, nullptr, &outLayout) == VK_SUCCESS;
}

VkPushConstantRange MakeVertexPushConstantRange(uint32_t size)
{
    VkPushConstantRange pushConstantRange{};
    pushConstantRange.stageFlags = VK_SHADER_STAGE_VERTEX_BIT;
    pushConstantRange.size = size;
    return pushConstantRange;
}

using infernux::vkrender::MakeMultisampleState;
using infernux::vkrender::MakeShaderStageInfo;
using infernux::vkrender::MakeTriangleListInputAssembly;

VkPipelineVertexInputStateCreateInfo MakeVertexInputState(const VkVertexInputBindingDescription &bindingDesc,
                                                          const VkVertexInputAttributeDescription *attrDesc,
                                                          uint32_t attrCount)
{
    VkPipelineVertexInputStateCreateInfo vertexInput{};
    vertexInput.sType = VK_STRUCTURE_TYPE_PIPELINE_VERTEX_INPUT_STATE_CREATE_INFO;
    vertexInput.vertexBindingDescriptionCount = 1;
    vertexInput.pVertexBindingDescriptions = &bindingDesc;
    vertexInput.vertexAttributeDescriptionCount = attrCount;
    vertexInput.pVertexAttributeDescriptions = attrDesc;
    return vertexInput;
}

VkPipelineViewportStateCreateInfo MakeDynamicViewportState()
{
    VkPipelineViewportStateCreateInfo viewportState{};
    viewportState.sType = VK_STRUCTURE_TYPE_PIPELINE_VIEWPORT_STATE_CREATE_INFO;
    viewportState.viewportCount = 1;
    viewportState.scissorCount = 1;
    return viewportState;
}

VkPipelineRasterizationStateCreateInfo MakeRasterizationState()
{
    VkPipelineRasterizationStateCreateInfo rasterization{};
    rasterization.sType = VK_STRUCTURE_TYPE_PIPELINE_RASTERIZATION_STATE_CREATE_INFO;
    rasterization.polygonMode = VK_POLYGON_MODE_FILL;
    rasterization.cullMode = VK_CULL_MODE_NONE;
    rasterization.frontFace = VK_FRONT_FACE_CLOCKWISE;
    rasterization.lineWidth = 1.0f;
    return rasterization;
}

VkPipelineColorBlendAttachmentState MakeAlphaBlendAttachment()
{
    VkPipelineColorBlendAttachmentState attachment{};
    attachment.blendEnable = VK_TRUE;
    attachment.srcColorBlendFactor = VK_BLEND_FACTOR_SRC_ALPHA;
    attachment.dstColorBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
    attachment.colorBlendOp = VK_BLEND_OP_ADD;
    attachment.srcAlphaBlendFactor = VK_BLEND_FACTOR_ONE;
    attachment.dstAlphaBlendFactor = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
    attachment.alphaBlendOp = VK_BLEND_OP_ADD;
    attachment.colorWriteMask =
        VK_COLOR_COMPONENT_R_BIT | VK_COLOR_COMPONENT_G_BIT | VK_COLOR_COMPONENT_B_BIT | VK_COLOR_COMPONENT_A_BIT;
    return attachment;
}

VkPipelineColorBlendStateCreateInfo MakeColorBlendState(const VkPipelineColorBlendAttachmentState &attachment)
{
    VkPipelineColorBlendStateCreateInfo colorBlend{};
    colorBlend.sType = VK_STRUCTURE_TYPE_PIPELINE_COLOR_BLEND_STATE_CREATE_INFO;
    colorBlend.attachmentCount = 1;
    colorBlend.pAttachments = &attachment;
    return colorBlend;
}

VkPipelineDepthStencilStateCreateInfo MakeDisabledDepthStencilState()
{
    VkPipelineDepthStencilStateCreateInfo depthStencil{};
    depthStencil.sType = VK_STRUCTURE_TYPE_PIPELINE_DEPTH_STENCIL_STATE_CREATE_INFO;
    return depthStencil;
}

VkPipelineDynamicStateCreateInfo MakeDynamicStateInfo(const VkDynamicState *dynamicStates, uint32_t dynamicStateCount)
{
    VkPipelineDynamicStateCreateInfo dynamicState{};
    dynamicState.sType = VK_STRUCTURE_TYPE_PIPELINE_DYNAMIC_STATE_CREATE_INFO;
    dynamicState.dynamicStateCount = dynamicStateCount;
    dynamicState.pDynamicStates = dynamicStates;
    return dynamicState;
}

bool UploadAllocation(VmaAllocator allocator, VmaAllocation allocation, const void *data, size_t size)
{
    if (size == 0) {
        return true;
    }

    void *mappedData = nullptr;
    if (vmaMapMemory(allocator, allocation, &mappedData) != VK_SUCCESS || mappedData == nullptr) {
        return false;
    }

    std::memcpy(mappedData, data, size);
    vmaUnmapMemory(allocator, allocation);
    return true;
}

VkDeviceSize GrowBufferSize(VkDeviceSize requiredSize)
{
    return requiredSize + (requiredSize >> 1);
}

void DestroyBufferWithQueue(VmaAllocator allocator, GpuRetirementQueue *deletionQueue, VkBuffer &buffer,
                            VmaAllocation &allocation, VkDeviceSize &bufferSize)
{
    if (buffer == VK_NULL_HANDLE) {
        allocation = VK_NULL_HANDLE;
        bufferSize = 0;
        return;
    }

    if (deletionQueue != nullptr) {
        const VkBuffer oldBuffer = buffer;
        const VmaAllocation oldAllocation = allocation;
        deletionQueue->Retire(
            [allocator, oldBuffer, oldAllocation]() { vmaDestroyBuffer(allocator, oldBuffer, oldAllocation); });
    } else {
        vmaDestroyBuffer(allocator, buffer, allocation);
    }

    buffer = VK_NULL_HANDLE;
    allocation = VK_NULL_HANDLE;
    bufferSize = 0;
}

bool EnsureHostVisibleBuffer(VmaAllocator allocator, GpuRetirementQueue *deletionQueue, VkBufferUsageFlags usage,
                             VkBuffer &buffer, VmaAllocation &allocation, VkDeviceSize &bufferSize,
                             VkDeviceSize requiredSize)
{
    if (requiredSize == 0) {
        return true;
    }
    if (buffer != VK_NULL_HANDLE && bufferSize >= requiredSize) {
        return true;
    }

    DestroyBufferWithQueue(allocator, deletionQueue, buffer, allocation, bufferSize);

    VkBufferCreateInfo createInfo{};
    createInfo.sType = VK_STRUCTURE_TYPE_BUFFER_CREATE_INFO;
    createInfo.size = GrowBufferSize(requiredSize);
    createInfo.usage = usage;

    VmaAllocationCreateInfo allocInfo{};
    allocInfo.usage = VMA_MEMORY_USAGE_CPU_TO_GPU;
    allocInfo.flags = VMA_ALLOCATION_CREATE_HOST_ACCESS_SEQUENTIAL_WRITE_BIT;

    if (vmaCreateBuffer(allocator, &createInfo, &allocInfo, &buffer, &allocation, nullptr) != VK_SUCCESS) {
        buffer = VK_NULL_HANDLE;
        allocation = VK_NULL_HANDLE;
        bufferSize = 0;
        return false;
    }

    bufferSize = createInfo.size;
    return true;
}
} // namespace

// ============================================================================
// ImGui SPIR-V shader bytecode (from imgui_impl_vulkan.cpp, MIT license)
// ============================================================================

// clang-format off

// backends/vulkan/glsl_shader.vert — compiled with glslangValidator
static const uint32_t s_vertSpv[] = {
    0x07230203,0x00010000,0x00080001,0x0000002e,0x00000000,0x00020011,0x00000001,0x0006000b,
    0x00000001,0x4c534c47,0x6474732e,0x3035342e,0x00000000,0x0003000e,0x00000000,0x00000001,
    0x000a000f,0x00000000,0x00000004,0x6e69616d,0x00000000,0x0000000b,0x0000000f,0x00000015,
    0x0000001b,0x0000001c,0x00030003,0x00000002,0x000001c2,0x00040005,0x00000004,0x6e69616d,
    0x00000000,0x00030005,0x00000009,0x00000000,0x00050006,0x00000009,0x00000000,0x6f6c6f43,
    0x00000072,0x00040006,0x00000009,0x00000001,0x00005655,0x00030005,0x0000000b,0x0074754f,
    0x00040005,0x0000000f,0x6c6f4361,0x0000726f,0x00030005,0x00000015,0x00565561,0x00060005,
    0x00000019,0x505f6c67,0x65567265,0x78657472,0x00000000,0x00060006,0x00000019,0x00000000,
    0x505f6c67,0x7469736f,0x006e6f69,0x00030005,0x0000001b,0x00000000,0x00040005,0x0000001c,
    0x736f5061,0x00000000,0x00060005,0x0000001e,0x73755075,0x6e6f4368,0x6e617473,0x00000074,
    0x00050006,0x0000001e,0x00000000,0x61635375,0x0000656c,0x00060006,0x0000001e,0x00000001,
    0x61725475,0x616c736e,0x00006574,0x00030005,0x00000020,0x00006370,0x00040047,0x0000000b,
    0x0000001e,0x00000000,0x00040047,0x0000000f,0x0000001e,0x00000002,0x00040047,0x00000015,
    0x0000001e,0x00000001,0x00050048,0x00000019,0x00000000,0x0000000b,0x00000000,0x00030047,
    0x00000019,0x00000002,0x00040047,0x0000001c,0x0000001e,0x00000000,0x00050048,0x0000001e,
    0x00000000,0x00000023,0x00000000,0x00050048,0x0000001e,0x00000001,0x00000023,0x00000008,
    0x00030047,0x0000001e,0x00000002,0x00020013,0x00000002,0x00030021,0x00000003,0x00000002,
    0x00030016,0x00000006,0x00000020,0x00040017,0x00000007,0x00000006,0x00000004,0x00040017,
    0x00000008,0x00000006,0x00000002,0x0004001e,0x00000009,0x00000007,0x00000008,0x00040020,
    0x0000000a,0x00000003,0x00000009,0x0004003b,0x0000000a,0x0000000b,0x00000003,0x00040015,
    0x0000000c,0x00000020,0x00000001,0x0004002b,0x0000000c,0x0000000d,0x00000000,0x00040020,
    0x0000000e,0x00000001,0x00000007,0x0004003b,0x0000000e,0x0000000f,0x00000001,0x00040020,
    0x00000011,0x00000003,0x00000007,0x0004002b,0x0000000c,0x00000013,0x00000001,0x00040020,
    0x00000014,0x00000001,0x00000008,0x0004003b,0x00000014,0x00000015,0x00000001,0x00040020,
    0x00000017,0x00000003,0x00000008,0x0003001e,0x00000019,0x00000007,0x00040020,0x0000001a,
    0x00000003,0x00000019,0x0004003b,0x0000001a,0x0000001b,0x00000003,0x0004003b,0x00000014,
    0x0000001c,0x00000001,0x0004001e,0x0000001e,0x00000008,0x00000008,0x00040020,0x0000001f,
    0x00000009,0x0000001e,0x0004003b,0x0000001f,0x00000020,0x00000009,0x00040020,0x00000021,
    0x00000009,0x00000008,0x0004002b,0x00000006,0x00000028,0x00000000,0x0004002b,0x00000006,
    0x00000029,0x3f800000,0x00050036,0x00000002,0x00000004,0x00000000,0x00000003,0x000200f8,
    0x00000005,0x0004003d,0x00000007,0x00000010,0x0000000f,0x00050041,0x00000011,0x00000012,
    0x0000000b,0x0000000d,0x0003003e,0x00000012,0x00000010,0x0004003d,0x00000008,0x00000016,
    0x00000015,0x00050041,0x00000017,0x00000018,0x0000000b,0x00000013,0x0003003e,0x00000018,
    0x00000016,0x0004003d,0x00000008,0x0000001d,0x0000001c,0x00050041,0x00000021,0x00000022,
    0x00000020,0x0000000d,0x0004003d,0x00000008,0x00000023,0x00000022,0x00050085,0x00000008,
    0x00000024,0x0000001d,0x00000023,0x00050041,0x00000021,0x00000025,0x00000020,0x00000013,
    0x0004003d,0x00000008,0x00000026,0x00000025,0x00050081,0x00000008,0x00000027,0x00000024,
    0x00000026,0x00050051,0x00000006,0x0000002a,0x00000027,0x00000000,0x00050051,0x00000006,
    0x0000002b,0x00000027,0x00000001,0x00070050,0x00000007,0x0000002c,0x0000002a,0x0000002b,
    0x00000028,0x00000029,0x00050041,0x00000011,0x0000002d,0x0000001b,0x0000000d,0x0003003e,
    0x0000002d,0x0000002c,0x000100fd,0x00010038
};

// backends/vulkan/glsl_shader.frag — compiled with glslangValidator
static const uint32_t s_fragSpv[] = {
    0x07230203,0x00010000,0x00080001,0x0000001e,0x00000000,0x00020011,0x00000001,0x0006000b,
    0x00000001,0x4c534c47,0x6474732e,0x3035342e,0x00000000,0x0003000e,0x00000000,0x00000001,
    0x0007000f,0x00000004,0x00000004,0x6e69616d,0x00000000,0x00000009,0x0000000d,0x00030010,
    0x00000004,0x00000007,0x00030003,0x00000002,0x000001c2,0x00040005,0x00000004,0x6e69616d,
    0x00000000,0x00040005,0x00000009,0x6c6f4366,0x0000726f,0x00030005,0x0000000b,0x00000000,
    0x00050006,0x0000000b,0x00000000,0x6f6c6f43,0x00000072,0x00040006,0x0000000b,0x00000001,
    0x00005655,0x00030005,0x0000000d,0x00006e49,0x00050005,0x00000016,0x78655473,0x65727574,
    0x00000000,0x00040047,0x00000009,0x0000001e,0x00000000,0x00040047,0x0000000d,0x0000001e,
    0x00000000,0x00040047,0x00000016,0x00000022,0x00000000,0x00040047,0x00000016,0x00000021,
    0x00000000,0x00020013,0x00000002,0x00030021,0x00000003,0x00000002,0x00030016,0x00000006,
    0x00000020,0x00040017,0x00000007,0x00000006,0x00000004,0x00040020,0x00000008,0x00000003,
    0x00000007,0x0004003b,0x00000008,0x00000009,0x00000003,0x00040017,0x0000000a,0x00000006,
    0x00000002,0x0004001e,0x0000000b,0x00000007,0x0000000a,0x00040020,0x0000000c,0x00000001,
    0x0000000b,0x0004003b,0x0000000c,0x0000000d,0x00000001,0x00040015,0x0000000e,0x00000020,
    0x00000001,0x0004002b,0x0000000e,0x0000000f,0x00000000,0x00040020,0x00000010,0x00000001,
    0x00000007,0x00090019,0x00000013,0x00000006,0x00000001,0x00000000,0x00000000,0x00000000,
    0x00000001,0x00000000,0x0003001b,0x00000014,0x00000013,0x00040020,0x00000015,0x00000000,
    0x00000014,0x0004003b,0x00000015,0x00000016,0x00000000,0x0004002b,0x0000000e,0x00000018,
    0x00000001,0x00040020,0x00000019,0x00000001,0x0000000a,0x00050036,0x00000002,0x00000004,
    0x00000000,0x00000003,0x000200f8,0x00000005,0x00050041,0x00000010,0x00000011,0x0000000d,
    0x0000000f,0x0004003d,0x00000007,0x00000012,0x00000011,0x0004003d,0x00000014,0x00000017,
    0x00000016,0x00050041,0x00000019,0x0000001a,0x0000000d,0x00000018,0x0004003d,0x0000000a,
    0x0000001b,0x0000001a,0x00050057,0x00000007,0x0000001c,0x00000017,0x0000001b,0x00050085,
    0x00000007,0x0000001d,0x00000012,0x0000001c,0x0003003e,0x00000009,0x0000001d,0x000100fd,
    0x00010038
};

// clang-format on

// ============================================================================
// Constructor / Destructor
// ============================================================================

InxScreenUIRenderer::InxScreenUIRenderer() = default;

InxScreenUIRenderer::~InxScreenUIRenderer()
{
    Destroy();
}

// ============================================================================
// Initialization
// ============================================================================

bool InxScreenUIRenderer::Initialize(VkDevice device, VmaAllocator allocator, VkFormat colorFormat,
                                     VkSampleCountFlagBits msaaSamples)
{
    if (m_initialized)
        return true;

    m_device = device;
    m_allocator = allocator;
    m_colorFormat = colorFormat;
    m_msaaSamples = msaaSamples;
    if (!CreatePipeline()) {
        INXLOG_ERROR("InxScreenUIRenderer: Failed to create pipeline");
        return false;
    }

    // Create standalone ImDrawList instances (not attached to any ImGui window)
    ImDrawListSharedData *sharedData = ImGui::GetDrawListSharedData();
    m_cameraDrawList = IM_NEW(ImDrawList)(sharedData);
    m_overlayDrawList = IM_NEW(ImDrawList)(sharedData);

    m_initialized = true;
    // INXLOG_INFO("InxScreenUIRenderer initialized (format=", static_cast<int>(colorFormat),
    //             ", MSAA=", static_cast<int>(msaaSamples), ")");
    return true;
}

void InxScreenUIRenderer::Destroy()
{
    m_commandCacheValid = false;
    if (m_cameraDrawList) {
        IM_DELETE(m_cameraDrawList);
        m_cameraDrawList = nullptr;
    }
    if (m_overlayDrawList) {
        IM_DELETE(m_overlayDrawList);
        m_overlayDrawList = nullptr;
    }

    if (m_device != VK_NULL_HANDLE) {
        for (auto &buf : m_listBuffers) {
            if (buf.vertexBuffer)
                vmaDestroyBuffer(m_allocator, buf.vertexBuffer, buf.vertexAlloc);
            if (buf.indexBuffer)
                vmaDestroyBuffer(m_allocator, buf.indexBuffer, buf.indexAlloc);
            buf = {};
        }
        if (m_pipeline)
            vkDestroyPipeline(m_device, m_pipeline, nullptr);
        if (m_pipelineLayout)
            vkDestroyPipelineLayout(m_device, m_pipelineLayout, nullptr);
        if (m_descriptorSetLayout)
            vkDestroyDescriptorSetLayout(m_device, m_descriptorSetLayout, nullptr);
        if (m_vertShader)
            vkDestroyShaderModule(m_device, m_vertShader, nullptr);
        if (m_fragShader)
            vkDestroyShaderModule(m_device, m_fragShader, nullptr);
    }

    m_listBuffers[0] = {};
    m_listBuffers[1] = {};
    m_pipeline = VK_NULL_HANDLE;
    m_pipelineLayout = VK_NULL_HANDLE;
    m_descriptorSetLayout = VK_NULL_HANDLE;
    m_fontDescriptorSet = VK_NULL_HANDLE;
    m_vertShader = VK_NULL_HANDLE;
    m_fragShader = VK_NULL_HANDLE;
    m_device = VK_NULL_HANDLE;
    m_allocator = VK_NULL_HANDLE;
    m_initialized = false;
}

// ============================================================================
// Per-Frame Command Accumulation
// ============================================================================

void InxScreenUIRenderer::BeginFrame(uint32_t width, uint32_t height)
{
    if (!m_initialized)
        return;

    m_cameraHDRRanges.clear();
    m_overlayHDRRanges.clear();
    m_commandCacheValid = false;

    ResetDrawListForFrame(*m_cameraDrawList, width, height);
    ResetDrawListForFrame(*m_overlayDrawList, width, height);
}

bool InxScreenUIRenderer::BeginFrameCached(uint32_t width, uint32_t height, uint64_t contentRevision)
{
    if (!m_initialized)
        return false;

    const ImTextureID fontTextureId = ImGui::GetIO().Fonts->TexRef.GetTexID();
    if (m_commandCacheValid && m_cachedWidth == width && m_cachedHeight == height &&
        m_cachedContentRevision == contentRevision && m_cachedFontTextureId == fontTextureId) {
        return true;
    }

    m_cameraHDRRanges.clear();
    m_overlayHDRRanges.clear();
    ResetDrawListForFrame(*m_cameraDrawList, width, height);
    ResetDrawListForFrame(*m_overlayDrawList, width, height);
    m_cachedWidth = width;
    m_cachedHeight = height;
    m_cachedContentRevision = contentRevision;
    m_cachedFontTextureId = fontTextureId;
    m_commandCacheValid = true;
    return false;
}

void InxScreenUIRenderer::AddFilledRect(ScreenUIList list, float minX, float minY, float maxX, float maxY, float r,
                                        float g, float b, float a, float rounding)
{
    ImDrawList *dl = GetDrawList(list);
    if (!dl)
        return;
    const int vtxStart = dl->VtxBuffer.Size;
    const float hdrScale = ExtractHDRScale(r, g, b);
    ImU32 col = ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, a));
    dl->AddRectFilled(ImVec2(minX, minY), ImVec2(maxX, maxY), col, rounding);
    TrackHDRColorRange(list, vtxStart, dl->VtxBuffer.Size, hdrScale);
}

void InxScreenUIRenderer::AddImage(ScreenUIList list, uint64_t textureId, float minX, float minY, float maxX,
                                   float maxY, float uv0X, float uv0Y, float uv1X, float uv1Y, float r, float g,
                                   float b, float a, float rotation, bool mirrorH, bool mirrorV, float rounding)
{
    ImDrawList *dl = GetDrawList(list);
    if (!dl || textureId == 0)
        return;

    const int vtxStart = dl->VtxBuffer.Size;
    const float hdrScale = ExtractHDRScale(r, g, b);
    ImU32 tint = ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, a));
    const VertexTransform transform = MakeVertexTransform(minX, minY, maxX, maxY, rotation, mirrorH, mirrorV);
    if (rounding > 0.5f)
        dl->AddImageRounded(ToImTextureID(textureId), ImVec2(minX, minY), ImVec2(maxX, maxY), ImVec2(uv0X, uv0Y),
                            ImVec2(uv1X, uv1Y), tint, rounding);
    else
        dl->AddImage(ToImTextureID(textureId), ImVec2(minX, minY), ImVec2(maxX, maxY), ImVec2(uv0X, uv0Y),
                     ImVec2(uv1X, uv1Y), tint);

    ApplyVertexTransform(*dl, vtxStart, transform);
    TrackHDRColorRange(list, vtxStart, dl->VtxBuffer.Size, hdrScale);
}

void InxScreenUIRenderer::AddText(ScreenUIList list, float minX, float minY, float maxX, float maxY,
                                  const std::string &text, float r, float g, float b, float a, float alignX,
                                  float alignY, float fontSize, float wrapWidth, float rotation, bool mirrorH,
                                  bool mirrorV, const std::string &fontPath, float lineHeight, float letterSpacing)
{
    ImDrawList *dl = GetDrawList(list);
    if (!dl || text.empty())
        return;

    const textlayout::TextLayoutResult layout =
        textlayout::LayoutText({text, fontPath, ResolveFontSize(fontSize), wrapWidth, lineHeight, letterSpacing});

    const float hdrScale = ExtractHDRScale(r, g, b);
    ImU32 col = ImGui::ColorConvertFloat4ToU32(ImVec4(r, g, b, a));
    const int vtxStart = dl->VtxBuffer.Size;
    const VertexTransform transform = MakeVertexTransform(minX, minY, maxX, maxY, rotation, mirrorH, mirrorV);
    dl->PushTextureID(ImGui::GetIO().Fonts->TexRef);
    textlayout::RenderTextBox(dl, minX, minY, maxX, maxY, layout, col, alignX, alignY, letterSpacing);
    dl->PopTextureID();

    ApplyVertexTransform(*dl, vtxStart, transform);
    TrackHDRColorRange(list, vtxStart, dl->VtxBuffer.Size, hdrScale);
}

std::pair<float, float> InxScreenUIRenderer::MeasureText(const std::string &text, float fontSize, float wrapWidth,
                                                         const std::string &fontPath, float lineHeight,
                                                         float letterSpacing) const
{
    const textlayout::TextLayoutResult layout =
        textlayout::LayoutText({text, fontPath, ResolveFontSize(fontSize), wrapWidth, lineHeight, letterSpacing});
    return {layout.totalWidth, layout.totalHeight};
}

bool InxScreenUIRenderer::HasCommands(ScreenUIList list) const
{
    const ImDrawList *dl = GetDrawList(list);
    return dl && dl->CmdBuffer.Size > 0 && dl->VtxBuffer.Size > 0;
}

void InxScreenUIRenderer::TrackHDRColorRange(ScreenUIList list, int vertexStart, int vertexEnd, float rgbScale)
{
    if (rgbScale <= 1.0f || vertexEnd <= vertexStart) {
        return;
    }

    auto &ranges = GetHDRRanges(list);
    ranges.push_back({vertexStart, vertexEnd, rgbScale});
}

std::vector<InxScreenUIRenderer::HDRColorRange> &InxScreenUIRenderer::GetHDRRanges(ScreenUIList list)
{
    return (list == ScreenUIList::Camera) ? m_cameraHDRRanges : m_overlayHDRRanges;
}

const std::vector<InxScreenUIRenderer::HDRColorRange> &InxScreenUIRenderer::GetHDRRanges(ScreenUIList list) const
{
    return (list == ScreenUIList::Camera) ? m_cameraHDRRanges : m_overlayHDRRanges;
}

// ============================================================================
// Rendering
// ============================================================================

void InxScreenUIRenderer::Render(VkCommandBuffer cmdBuf, ScreenUIList list, uint32_t width, uint32_t height)
{
    if (!m_initialized || !m_pipeline || width == 0 || height == 0 || !m_enabled)
        return;

    ImDrawList *dl = GetDrawList(list);
    if (!dl || dl->VtxBuffer.Size == 0 || dl->IdxBuffer.Size == 0)
        return;

    // With ImGui 1.92+ dynamic font atlas, the texture may be recreated
    // at any time. Always pull the current descriptor set before drawing.
    if (!RefreshFontDescriptorSet(m_fontDescriptorSet)) {
        return;
    }

    // ---- Upload vertex/index data ----
    std::vector<GPUVertex> gpuVertices(static_cast<size_t>(dl->VtxBuffer.Size));

    const auto &hdrRanges = GetHDRRanges(list);
    size_t rangeIndex = 0;
    for (int i = 0; i < dl->VtxBuffer.Size; ++i) {
        while (rangeIndex < hdrRanges.size() && i >= hdrRanges[rangeIndex].vertexEnd) {
            ++rangeIndex;
        }

        float rgbScale = 1.0f;
        if (rangeIndex < hdrRanges.size()) {
            const HDRColorRange &range = hdrRanges[rangeIndex];
            if (i >= range.vertexStart && i < range.vertexEnd) {
                rgbScale = range.rgbScale;
            }
        }

        const ImDrawVert &src = dl->VtxBuffer[i];
        GPUVertex &dst = gpuVertices[static_cast<size_t>(i)];
        dst.pos = src.pos;
        dst.uv = src.uv;

        const ImVec4 unpacked = ImGui::ColorConvertU32ToFloat4(src.col);
        dst.color[0] = unpacked.x * rgbScale;
        dst.color[1] = unpacked.y * rgbScale;
        dst.color[2] = unpacked.z * rgbScale;
        dst.color[3] = unpacked.w;
    }

    const VkDeviceSize vtxSize = gpuVertices.size() * sizeof(GPUVertex);
    const VkDeviceSize idxSize = static_cast<VkDeviceSize>(dl->IdxBuffer.Size) * sizeof(ImDrawIdx);
    const int bufIdx = (list == ScreenUIList::Camera) ? 0 : 1;
    ListBuffers &buf = m_listBuffers[bufIdx];
    if (!EnsureBuffers(buf, vtxSize, idxSize)) {
        INXLOG_ERROR("InxScreenUIRenderer: Failed to resize screen UI upload buffers");
        return;
    }
    if (!UploadAllocation(m_allocator, buf.vertexAlloc, gpuVertices.data(), static_cast<size_t>(vtxSize)) ||
        !UploadAllocation(m_allocator, buf.indexAlloc, dl->IdxBuffer.Data, static_cast<size_t>(idxSize))) {
        INXLOG_ERROR("InxScreenUIRenderer: Failed to upload screen UI draw data");
        return;
    }

    // ---- Bind pipeline ----
    vkCmdBindPipeline(cmdBuf, VK_PIPELINE_BIND_POINT_GRAPHICS, m_pipeline);

    // ---- Bind vertex/index buffers ----
    const VkBuffer vertexBuffer = buf.vertexBuffer;
    const VkDeviceSize vertexBufferOffset = 0;
    vkCmdBindVertexBuffers(cmdBuf, 0, 1, &vertexBuffer, &vertexBufferOffset);
    vkCmdBindIndexBuffer(cmdBuf, buf.indexBuffer, 0,
                         sizeof(ImDrawIdx) == 2 ? VK_INDEX_TYPE_UINT16 : VK_INDEX_TYPE_UINT32);

    const VkViewport viewport = MakeViewport(width, height);
    vkCmdSetViewport(cmdBuf, 0, 1, &viewport);

    // ---- Push constants: ortho projection (scale + translate) ----
    // Maps [0, width] x [0, height] → [-1, 1] x [-1, 1]
    const auto pushConstants = MakeOrthoPushConstants(width, height);
    vkCmdPushConstants(cmdBuf, m_pipelineLayout, VK_SHADER_STAGE_VERTEX_BIT, 0, sizeof(pushConstants),
                       pushConstants.data());

    // ---- Bind font atlas descriptor set ----
    {
        const uint64_t fontDescRaw = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(m_fontDescriptorSet));
        const uint32_t lo = static_cast<uint32_t>(fontDescRaw & 0xffffffffull);
        const uint32_t hi = static_cast<uint32_t>((fontDescRaw >> 32) & 0xffffffffull);
        if (hi == lo && lo != 0u && lo <= 0x000fffffu) {
            static int badUiDescWarnCount = 0;
            if (badUiDescWarnCount++ < 16) {
                INXLOG_WARN("[InxScreenUIRenderer] suspicious font set0 desc=0x", fontDescRaw,
                            " -- skip UI batch to avoid invalid bind");
            }
            return;
        }
    }
    vkdebug::CmdBindDescriptorSetsTracked("InxScreenUIRenderer.Render.Set0Font", cmdBuf,
                                          VK_PIPELINE_BIND_POINT_GRAPHICS, m_pipelineLayout, 0, 1, &m_fontDescriptorSet,
                                          0, nullptr);
    VkDescriptorSet lastBoundDescSet = m_fontDescriptorSet;

    // ---- Issue draw commands ----
    const float frameWidth = static_cast<float>(width);
    const float frameHeight = static_cast<float>(height);

    for (int cmdI = 0; cmdI < dl->CmdBuffer.Size; cmdI++) {
        const ImDrawCmd &cmd = dl->CmdBuffer[cmdI];

        if (cmd.UserCallback != nullptr) {
            // User callbacks are not supported in scene render passes
            continue;
        }

        // Per-command texture (usually font atlas)
        VkDescriptorSet texDescSet = reinterpret_cast<VkDescriptorSet>(static_cast<uintptr_t>(cmd.GetTexID()));
        if (texDescSet == VK_NULL_HANDLE) {
            m_commandCacheValid = false;
            continue;
        }
        if (texDescSet != m_fontDescriptorSet && m_textureUsageValidator &&
            !m_textureUsageValidator(static_cast<uint64_t>(reinterpret_cast<uintptr_t>(texDescSet)))) {
            // Cached ImDrawLists retain raw descriptor handles. A texture replacement or
            // residency eviction invalidates that handle, so reject this command and force
            // Python to rebuild the list with the currently published texture next UI tick.
            m_commandCacheValid = false;
            continue;
        }
        {
            const uint64_t texDescRaw = static_cast<uint64_t>(reinterpret_cast<uintptr_t>(texDescSet));
            const uint32_t lo = static_cast<uint32_t>(texDescRaw & 0xffffffffull);
            const uint32_t hi = static_cast<uint32_t>((texDescRaw >> 32) & 0xffffffffull);
            if (hi == lo && lo != 0u && lo <= 0x000fffffu) {
                static int badUiTexDescWarnCount = 0;
                if (badUiTexDescWarnCount++ < 24) {
                    INXLOG_WARN("[InxScreenUIRenderer] suspicious cmd texture set0 desc=0x", texDescRaw,
                                " -- skip draw command");
                }
                continue;
            }
        }
        if (texDescSet != lastBoundDescSet) {
            vkdebug::CmdBindDescriptorSetsTracked("InxScreenUIRenderer.Render.Set0Tex", cmdBuf,
                                                  VK_PIPELINE_BIND_POINT_GRAPHICS, m_pipelineLayout, 0, 1, &texDescSet,
                                                  0, nullptr);
            lastBoundDescSet = texDescSet;
        }

        // Scissor rect from ImDrawCmd clip rect — clamped to render area
        // to prevent Vulkan validation errors and potential DEVICE_LOST.
        VkRect2D scissor{};
        if (!MakeClampedScissor(cmd, frameWidth, frameHeight, scissor))
            continue; // Degenerate scissor — skip draw

        vkCmdSetScissor(cmdBuf, 0, 1, &scissor);

        vkCmdDrawIndexed(cmdBuf, cmd.ElemCount, 1, cmd.IdxOffset, static_cast<int32_t>(cmd.VtxOffset), 0);
    }
}

// ============================================================================
// Pipeline Creation
// ============================================================================

bool InxScreenUIRenderer::CreatePipeline()
{
    // ---- Shader modules ----
    if (!CreateShaderModule(m_device, s_vertSpv, sizeof(s_vertSpv), m_vertShader))
        return false;
    if (!CreateShaderModule(m_device, s_fragSpv, sizeof(s_fragSpv), m_fragShader))
        return false;

    // ---- Descriptor set layout (identical to ImGui's) ----
    const VkDescriptorSetLayoutBinding binding = vkrender::MakeDescriptorSetLayoutBinding(
        kFontTextureBinding, VK_DESCRIPTOR_TYPE_COMBINED_IMAGE_SAMPLER, VK_SHADER_STAGE_FRAGMENT_BIT);
    if (!vkrender::CreateDescriptorSetLayout(m_device, &binding, 1, m_descriptorSetLayout))
        return false;

    // ---- Pipeline layout (identical to ImGui's: 4 floats push constant) ----
    const VkPushConstantRange pushConstRange = MakeVertexPushConstantRange(sizeof(float) * 4);
    if (!CreatePipelineLayout(m_device, m_descriptorSetLayout, pushConstRange, m_pipelineLayout))
        return false;

    // ---- Graphics pipeline (replicates ImGui's pipeline for scene render target) ----
    const std::array<VkPipelineShaderStageCreateInfo, 2> stages = {
        MakeShaderStageInfo(VK_SHADER_STAGE_VERTEX_BIT, m_vertShader),
        MakeShaderStageInfo(VK_SHADER_STAGE_FRAGMENT_BIT, m_fragShader),
    };

    VkVertexInputBindingDescription bindingDesc{};
    bindingDesc.stride = sizeof(GPUVertex);
    bindingDesc.inputRate = VK_VERTEX_INPUT_RATE_VERTEX;

    VkVertexInputAttributeDescription attrDesc[3]{};
    attrDesc[0].location = 0;
    attrDesc[0].format = VK_FORMAT_R32G32_SFLOAT;
    attrDesc[0].offset = offsetof(GPUVertex, pos);
    attrDesc[1].location = 1;
    attrDesc[1].format = VK_FORMAT_R32G32_SFLOAT;
    attrDesc[1].offset = offsetof(GPUVertex, uv);
    attrDesc[2].location = 2;
    attrDesc[2].format = VK_FORMAT_R32G32B32A32_SFLOAT;
    attrDesc[2].offset = offsetof(GPUVertex, color);

    const VkPipelineVertexInputStateCreateInfo vertInput = MakeVertexInputState(bindingDesc, attrDesc, 3);
    const VkPipelineInputAssemblyStateCreateInfo iaInfo = MakeTriangleListInputAssembly();
    const VkPipelineViewportStateCreateInfo vpInfo = MakeDynamicViewportState();
    const VkPipelineRasterizationStateCreateInfo rsInfo = MakeRasterizationState();
    const VkPipelineMultisampleStateCreateInfo msInfo = MakeMultisampleState(m_msaaSamples);
    const VkPipelineColorBlendAttachmentState blendAttach = MakeAlphaBlendAttachment();
    const VkPipelineColorBlendStateCreateInfo cbInfo = MakeColorBlendState(blendAttach);
    const VkPipelineDepthStencilStateCreateInfo dsInfo = MakeDisabledDepthStencilState();
    const std::array<VkDynamicState, 2> dynStates = {VK_DYNAMIC_STATE_VIEWPORT, VK_DYNAMIC_STATE_SCISSOR};
    const VkPipelineDynamicStateCreateInfo dynInfo = MakeDynamicStateInfo(dynStates.data(), dynStates.size());

    VkGraphicsPipelineCreateInfo pipeInfo{};
    pipeInfo.sType = VK_STRUCTURE_TYPE_GRAPHICS_PIPELINE_CREATE_INFO;
    pipeInfo.stageCount = static_cast<uint32_t>(stages.size());
    pipeInfo.pStages = stages.data();
    pipeInfo.pVertexInputState = &vertInput;
    pipeInfo.pInputAssemblyState = &iaInfo;
    pipeInfo.pViewportState = &vpInfo;
    pipeInfo.pRasterizationState = &rsInfo;
    pipeInfo.pMultisampleState = &msInfo;
    pipeInfo.pDepthStencilState = &dsInfo;
    pipeInfo.pColorBlendState = &cbInfo;
    pipeInfo.pDynamicState = &dynInfo;
    pipeInfo.layout = m_pipelineLayout;
    VkPipelineRenderingCreateInfo renderingInfo{};
    renderingInfo.sType = VK_STRUCTURE_TYPE_PIPELINE_RENDERING_CREATE_INFO;
    renderingInfo.colorAttachmentCount = 1;
    renderingInfo.pColorAttachmentFormats = &m_colorFormat;
    pipeInfo.pNext = &renderingInfo;
    pipeInfo.renderPass = VK_NULL_HANDLE;
    pipeInfo.subpass = 0;

    return vkCreateGraphicsPipelines(m_device, VK_NULL_HANDLE, 1, &pipeInfo, nullptr, &m_pipeline) == VK_SUCCESS;
}

// ============================================================================
// Buffer Management
// ============================================================================

bool InxScreenUIRenderer::EnsureBuffers(ListBuffers &buf, VkDeviceSize vertexSize, VkDeviceSize indexSize)
{
    return EnsureHostVisibleBuffer(m_allocator, m_deletionQueue, VK_BUFFER_USAGE_VERTEX_BUFFER_BIT, buf.vertexBuffer,
                                   buf.vertexAlloc, buf.vertexBufferSize, vertexSize) &&
           EnsureHostVisibleBuffer(m_allocator, m_deletionQueue, VK_BUFFER_USAGE_INDEX_BUFFER_BIT, buf.indexBuffer,
                                   buf.indexAlloc, buf.indexBufferSize, indexSize);
}

// ============================================================================
// Helpers
// ============================================================================

ImDrawList *InxScreenUIRenderer::GetDrawList(ScreenUIList list)
{
    return (list == ScreenUIList::Camera) ? m_cameraDrawList : m_overlayDrawList;
}

const ImDrawList *InxScreenUIRenderer::GetDrawList(ScreenUIList list) const
{
    return (list == ScreenUIList::Camera) ? m_cameraDrawList : m_overlayDrawList;
}

} // namespace infernux
