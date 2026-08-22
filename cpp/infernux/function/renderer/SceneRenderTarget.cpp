#include "SceneRenderTarget.h"
#include "InxVkCoreModular.h"
#include "rhi/GpuRetirementQueue.h"
#include "vk/RhiVulkanTypes.h"
#include "vk/VkDeviceContext.h"
#include "vk/VkRenderUtils.h"
#include <array>
#include <backends/imgui_impl_vulkan.h>
#include <core/log/InxLog.h>
#include <vk_mem_alloc.h>

namespace infernux
{

namespace
{

inline void SafeDestroyVmaImage(VmaAllocator allocator, VkImage &image, VmaAllocation &alloc)
{
    if (image != VK_NULL_HANDLE) {
        vmaDestroyImage(allocator, image, alloc);
        image = VK_NULL_HANDLE;
        alloc = VK_NULL_HANDLE;
    }
}

struct RetiredSceneRenderTargetResources
{
    VkDevice device = VK_NULL_HANDLE;
    VmaAllocator allocator = VK_NULL_HANDLE;

    VkDescriptorSet imguiDescriptorSet = VK_NULL_HANDLE;
    VkSampler sampler = VK_NULL_HANDLE;
    VkSampler outlineMaskSampler = VK_NULL_HANDLE;

    VkImageView outlineMaskImageView = VK_NULL_HANDLE;
    VkImageView msaaColorImageView = VK_NULL_HANDLE;
    VkImageView depthImageView = VK_NULL_HANDLE;
    VkImageView colorImageView = VK_NULL_HANDLE;

    VkImage outlineMaskImage = VK_NULL_HANDLE;
    VmaAllocation outlineMaskAllocation = VK_NULL_HANDLE;
    VkImage msaaColorImage = VK_NULL_HANDLE;
    VmaAllocation msaaColorAllocation = VK_NULL_HANDLE;
    VkImage depthImage = VK_NULL_HANDLE;
    VmaAllocation depthAllocation = VK_NULL_HANDLE;
    VkImage colorImage = VK_NULL_HANDLE;
    VmaAllocation colorAllocation = VK_NULL_HANDLE;

    void Destroy()
    {
        if (imguiDescriptorSet != VK_NULL_HANDLE) {
            ImGui_ImplVulkan_RemoveTexture(imguiDescriptorSet);
            imguiDescriptorSet = VK_NULL_HANDLE;
        }

        vkrender::SafeDestroy(device, sampler);
        vkrender::SafeDestroy(device, outlineMaskSampler);
        vkrender::SafeDestroy(device, outlineMaskImageView);
        vkrender::SafeDestroy(device, msaaColorImageView);
        vkrender::SafeDestroy(device, depthImageView);
        vkrender::SafeDestroy(device, colorImageView);

        SafeDestroyVmaImage(allocator, outlineMaskImage, outlineMaskAllocation);
        SafeDestroyVmaImage(allocator, msaaColorImage, msaaColorAllocation);
        SafeDestroyVmaImage(allocator, depthImage, depthAllocation);
        SafeDestroyVmaImage(allocator, colorImage, colorAllocation);
    }
};

} // anonymous namespace

SceneRenderTarget::SceneRenderTarget(InxVkCoreModular *vkCore) : m_vkCore(vkCore)
{
}

SceneRenderTarget::~SceneRenderTarget()
{
    Cleanup();
}

bool SceneRenderTarget::Initialize(uint32_t width, uint32_t height)
{
    if (width == 0 || height == 0) {
        INXLOG_ERROR("SceneRenderTarget: Invalid dimensions ", width, "x", height);
        return false;
    }

    m_width = width;
    m_height = height;

    try {
        CreateColorAttachment();
        if (m_msaaSampleCount != VK_SAMPLE_COUNT_1_BIT) {
            CreateMsaaColorAttachment();
        }
        CreateDepthAttachment();
        CreateOutlineMaskAttachment();
        // NOTE: Framebuffer is no longer created here - RenderGraph creates its own
        CreateImGuiDescriptor();
        m_isInitialized = true;
        // INXLOG_INFO("SceneRenderTarget initialized: ", width, "x", height);
        return true;
    } catch (const std::exception &e) {
        INXLOG_ERROR("SceneRenderTarget initialization failed: ", e.what());
        CleanupResources();
        return false;
    }
}

VkFormat SceneRenderTarget::GetDepthFormat() const
{
    if (m_vkCore) {
        return m_vkCore->GetDeviceContext().FindSampledDepthFormat();
    }
    return VK_FORMAT_D32_SFLOAT;
}

uint64_t SceneRenderTarget::GetResidentBytes() const
{
    const VmaAllocator allocator = m_vkCore->GetDeviceContext().GetVmaAllocator();
    uint64_t bytes = 0;
    for (const VmaAllocation allocation :
         {m_colorAllocation, m_msaaColorAllocation, m_depthAllocation, m_outlineMaskAllocation}) {
        if (allocation == VK_NULL_HANDLE)
            continue;
        VmaAllocationInfo info{};
        vmaGetAllocationInfo(allocator, allocation, &info);
        bytes += info.size;
    }
    return bytes;
}

uint64_t SceneRenderTarget::GetMsaaColorResidentBytes() const
{
    if (m_msaaColorAllocation == VK_NULL_HANDLE)
        return 0;
    VmaAllocationInfo info{};
    vmaGetAllocationInfo(m_vkCore->GetDeviceContext().GetVmaAllocator(), m_msaaColorAllocation, &info);
    return info.size;
}

void SceneRenderTarget::CreateColorAttachment()
{
    auto imageInfo =
        vkrender::MakeImageCreateInfo2D(m_width, m_height, VK_FORMAT_R16G16B16A16_SFLOAT,
                                        VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT |
                                            VK_IMAGE_USAGE_TRANSFER_DST_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT);

    VmaAllocator allocator = m_vkCore->GetDeviceContext().GetVmaAllocator();
    VmaAllocationCreateInfo allocCreateInfo{};
    allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE;

    VkResult result =
        vmaCreateImage(allocator, &imageInfo, &allocCreateInfo, &m_colorImage, &m_colorAllocation, nullptr);
    if (result != VK_SUCCESS) {
        throw std::runtime_error("Failed to create color image via VMA");
    }

    auto viewInfo =
        vkrender::MakeImageViewCreateInfo2D(m_colorImage, VK_FORMAT_R16G16B16A16_SFLOAT, VK_IMAGE_ASPECT_COLOR_BIT);
    if (vkCreateImageView(m_vkCore->GetDevice(), &viewInfo, nullptr, &m_colorImageView) != VK_SUCCESS) {
        throw std::runtime_error("Failed to create color image view");
    }

    // Transition to shader read optimal initially
    VkCommandBuffer cmdBuf = m_vkCore->BeginSingleTimeCommands();
    auto barrier =
        vkrender::MakeImageBarrier(m_colorImage, VK_IMAGE_LAYOUT_UNDEFINED, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL,
                                   VK_IMAGE_ASPECT_COLOR_BIT, 0, VK_ACCESS_SHADER_READ_BIT);
    vkCmdPipelineBarrier(cmdBuf, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0, 0,
                         nullptr, 0, nullptr, 1, &barrier);
    m_vkCore->EndSingleTimeCommands(cmdBuf);
}

void SceneRenderTarget::CreateMsaaColorAttachment()
{
    const VkSampleCountFlagBits msaaSamples = m_msaaSampleCount;

    // TRANSFER_SRC_BIT needed for explicit MSAA resolve via vkCmdResolveImage
    // (cannot use TRANSIENT_ATTACHMENT_BIT with TRANSFER_SRC_BIT)
    auto imageInfo = vkrender::MakeImageCreateInfo2D(
        m_width, m_height, VK_FORMAT_R16G16B16A16_SFLOAT,
        VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_TRANSFER_SRC_BIT, msaaSamples);

    VmaAllocator allocator = m_vkCore->GetDeviceContext().GetVmaAllocator();
    VmaAllocationCreateInfo allocCreateInfo{};
    allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE;

    VkResult result =
        vmaCreateImage(allocator, &imageInfo, &allocCreateInfo, &m_msaaColorImage, &m_msaaColorAllocation, nullptr);
    if (result != VK_SUCCESS) {
        throw std::runtime_error("Failed to create MSAA color image via VMA");
    }

    auto viewInfo =
        vkrender::MakeImageViewCreateInfo2D(m_msaaColorImage, VK_FORMAT_R16G16B16A16_SFLOAT, VK_IMAGE_ASPECT_COLOR_BIT);
    if (vkCreateImageView(m_vkCore->GetDevice(), &viewInfo, nullptr, &m_msaaColorImageView) != VK_SUCCESS) {
        throw std::runtime_error("Failed to create MSAA color image view");
    }

    // Keep the real image layout aligned with the render-graph's tracked initial
    // state for the imported MSAA backbuffer.
    VkCommandBuffer cmdBuf = m_vkCore->BeginSingleTimeCommands();
    auto barrier = vkrender::MakeImageBarrier(m_msaaColorImage, VK_IMAGE_LAYOUT_UNDEFINED,
                                              VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL, VK_IMAGE_ASPECT_COLOR_BIT, 0,
                                              VK_ACCESS_COLOR_ATTACHMENT_WRITE_BIT);
    vkCmdPipelineBarrier(cmdBuf, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_COLOR_ATTACHMENT_OUTPUT_BIT, 0, 0,
                         nullptr, 0, nullptr, 1, &barrier);
    m_vkCore->EndSingleTimeCommands(cmdBuf);
}

void SceneRenderTarget::CreateDepthAttachment()
{
    const VkFormat depthFormat = m_vkCore->GetDeviceContext().FindSampledDepthFormat();
    if (depthFormat == VK_FORMAT_UNDEFINED) {
        throw std::runtime_error("No depth format supports both attachment and sampled-image usage");
    }

    auto imageInfo = vkrender::MakeImageCreateInfo2D(
        m_width, m_height, depthFormat, VK_IMAGE_USAGE_DEPTH_STENCIL_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT,
        m_msaaSampleCount);

    VmaAllocator allocator = m_vkCore->GetDeviceContext().GetVmaAllocator();
    VmaAllocationCreateInfo allocCreateInfo{};
    allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE;

    VkResult result =
        vmaCreateImage(allocator, &imageInfo, &allocCreateInfo, &m_depthImage, &m_depthAllocation, nullptr);
    if (result != VK_SUCCESS) {
        throw std::runtime_error("Failed to create depth image via VMA");
    }

    const VkImageAspectFlags depthAspect = rhi::ToVkImageAspectMask(depthFormat);
    auto viewInfo = vkrender::MakeImageViewCreateInfo2D(m_depthImage, depthFormat, depthAspect);
    if (vkCreateImageView(m_vkCore->GetDevice(), &viewInfo, nullptr, &m_depthImageView) != VK_SUCCESS) {
        throw std::runtime_error("Failed to create depth image view");
    }

    // Transition depth image to depth-stencil attachment optimal
    VkCommandBuffer cmdBuf = m_vkCore->BeginSingleTimeCommands();
    auto barrier = vkrender::MakeImageBarrier(m_depthImage, VK_IMAGE_LAYOUT_UNDEFINED,
                                              VK_IMAGE_LAYOUT_DEPTH_STENCIL_ATTACHMENT_OPTIMAL, depthAspect, 0,
                                              VK_ACCESS_DEPTH_STENCIL_ATTACHMENT_WRITE_BIT);
    vkCmdPipelineBarrier(cmdBuf, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_EARLY_FRAGMENT_TESTS_BIT, 0, 0,
                         nullptr, 0, nullptr, 1, &barrier);
    m_vkCore->EndSingleTimeCommands(cmdBuf);
}

void SceneRenderTarget::CreateImGuiDescriptor()
{
    auto samplerInfo = vkrender::MakeLinearClampSamplerInfo();
    if (vkCreateSampler(m_vkCore->GetDevice(), &samplerInfo, nullptr, &m_sampler) != VK_SUCCESS) {
        throw std::runtime_error("Failed to create scene texture sampler");
    }

    // Create ImGui descriptor set for the texture
    m_imguiDescriptorSet =
        ImGui_ImplVulkan_AddTexture(m_sampler, m_colorImageView, VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL);

    if (m_imguiDescriptorSet == VK_NULL_HANDLE) {
        throw std::runtime_error("Failed to create ImGui descriptor set for scene texture");
    }
}

void SceneRenderTarget::CreateOutlineMaskAttachment()
{
    VkDevice device = m_vkCore->GetDevice();

    auto imageInfo = vkrender::MakeImageCreateInfo2D(m_width, m_height, VK_FORMAT_R8G8B8A8_UNORM,
                                                     VK_IMAGE_USAGE_COLOR_ATTACHMENT_BIT | VK_IMAGE_USAGE_SAMPLED_BIT);

    VmaAllocator allocator = m_vkCore->GetDeviceContext().GetVmaAllocator();
    VmaAllocationCreateInfo allocCreateInfo{};
    allocCreateInfo.usage = VMA_MEMORY_USAGE_AUTO_PREFER_DEVICE;

    if (vmaCreateImage(allocator, &imageInfo, &allocCreateInfo, &m_outlineMaskImage, &m_outlineMaskAllocation,
                       nullptr) != VK_SUCCESS)
        throw std::runtime_error("Failed to create outline mask image via VMA");

    auto viewInfo =
        vkrender::MakeImageViewCreateInfo2D(m_outlineMaskImage, VK_FORMAT_R8G8B8A8_UNORM, VK_IMAGE_ASPECT_COLOR_BIT);
    if (vkCreateImageView(device, &viewInfo, nullptr, &m_outlineMaskImageView) != VK_SUCCESS)
        throw std::runtime_error("Failed to create outline mask image view");

    // Sampler for composite pass
    auto samplerInfo = vkrender::MakeLinearClampSamplerInfo(VK_BORDER_COLOR_FLOAT_OPAQUE_BLACK);
    if (vkCreateSampler(device, &samplerInfo, nullptr, &m_outlineMaskSampler) != VK_SUCCESS)
        throw std::runtime_error("Failed to create outline mask sampler");

    // Transition to shader-read initially (will be transitioned at runtime)
    VkCommandBuffer cmdBuf = m_vkCore->BeginSingleTimeCommands();
    auto barrier = vkrender::MakeImageBarrier(m_outlineMaskImage, VK_IMAGE_LAYOUT_UNDEFINED,
                                              VK_IMAGE_LAYOUT_SHADER_READ_ONLY_OPTIMAL, VK_IMAGE_ASPECT_COLOR_BIT, 0,
                                              VK_ACCESS_SHADER_READ_BIT);
    vkCmdPipelineBarrier(cmdBuf, VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT, VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT, 0, 0,
                         nullptr, 0, nullptr, 1, &barrier);
    m_vkCore->EndSingleTimeCommands(cmdBuf);
}

void SceneRenderTarget::CleanupResources()
{
    VkDevice device = m_vkCore->GetDevice();
    VmaAllocator allocator = m_vkCore->GetDeviceContext().GetVmaAllocator();

    if (m_imguiDescriptorSet != VK_NULL_HANDLE) {
        ImGui_ImplVulkan_RemoveTexture(m_imguiDescriptorSet);
        m_imguiDescriptorSet = VK_NULL_HANDLE;
    }

    vkrender::SafeDestroy(device, m_sampler);
    vkrender::SafeDestroy(device, m_outlineMaskSampler);
    vkrender::SafeDestroy(device, m_outlineMaskImageView);
    SafeDestroyVmaImage(allocator, m_outlineMaskImage, m_outlineMaskAllocation);
    vkrender::SafeDestroy(device, m_msaaColorImageView);
    SafeDestroyVmaImage(allocator, m_msaaColorImage, m_msaaColorAllocation);
    vkrender::SafeDestroy(device, m_depthImageView);
    SafeDestroyVmaImage(allocator, m_depthImage, m_depthAllocation);
    vkrender::SafeDestroy(device, m_colorImageView);
    SafeDestroyVmaImage(allocator, m_colorImage, m_colorAllocation);

    m_isInitialized = false;
}

bool SceneRenderTarget::HasOwnedResources() const noexcept
{
    return m_imguiDescriptorSet != VK_NULL_HANDLE || m_sampler != VK_NULL_HANDLE ||
           m_outlineMaskSampler != VK_NULL_HANDLE || m_outlineMaskImageView != VK_NULL_HANDLE ||
           m_outlineMaskImage != VK_NULL_HANDLE || m_outlineMaskAllocation != VK_NULL_HANDLE ||
           m_msaaColorImageView != VK_NULL_HANDLE || m_msaaColorImage != VK_NULL_HANDLE ||
           m_msaaColorAllocation != VK_NULL_HANDLE || m_depthImageView != VK_NULL_HANDLE ||
           m_depthImage != VK_NULL_HANDLE || m_depthAllocation != VK_NULL_HANDLE ||
           m_colorImageView != VK_NULL_HANDLE || m_colorImage != VK_NULL_HANDLE || m_colorAllocation != VK_NULL_HANDLE;
}

void SceneRenderTarget::RetireResourcesAfter(GpuRetirementQueue &retirementQueue,
                                             rhi::SubmissionSerial retirementSerial)
{
    if (!HasOwnedResources()) {
        m_width = 0;
        m_height = 0;
        m_isInitialized = false;
        return;
    }

    RetiredSceneRenderTargetResources resources{};
    resources.device = m_vkCore->GetDevice();
    resources.allocator = m_vkCore->GetDeviceContext().GetVmaAllocator();
    resources.imguiDescriptorSet = m_imguiDescriptorSet;
    resources.sampler = m_sampler;
    resources.outlineMaskSampler = m_outlineMaskSampler;
    resources.outlineMaskImageView = m_outlineMaskImageView;
    resources.msaaColorImageView = m_msaaColorImageView;
    resources.depthImageView = m_depthImageView;
    resources.colorImageView = m_colorImageView;
    resources.outlineMaskImage = m_outlineMaskImage;
    resources.outlineMaskAllocation = m_outlineMaskAllocation;
    resources.msaaColorImage = m_msaaColorImage;
    resources.msaaColorAllocation = m_msaaColorAllocation;
    resources.depthImage = m_depthImage;
    resources.depthAllocation = m_depthAllocation;
    resources.colorImage = m_colorImage;
    resources.colorAllocation = m_colorAllocation;

    retirementQueue.RetireAfter(retirementSerial, [resources]() mutable { resources.Destroy(); });

    m_imguiDescriptorSet = VK_NULL_HANDLE;
    m_sampler = VK_NULL_HANDLE;
    m_outlineMaskSampler = VK_NULL_HANDLE;
    m_outlineMaskImageView = VK_NULL_HANDLE;
    m_outlineMaskImage = VK_NULL_HANDLE;
    m_outlineMaskAllocation = VK_NULL_HANDLE;
    m_msaaColorImageView = VK_NULL_HANDLE;
    m_msaaColorImage = VK_NULL_HANDLE;
    m_msaaColorAllocation = VK_NULL_HANDLE;
    m_depthImageView = VK_NULL_HANDLE;
    m_depthImage = VK_NULL_HANDLE;
    m_depthAllocation = VK_NULL_HANDLE;
    m_colorImageView = VK_NULL_HANDLE;
    m_colorImage = VK_NULL_HANDLE;
    m_colorAllocation = VK_NULL_HANDLE;
    m_width = 0;
    m_height = 0;
    m_isInitialized = false;
}

void SceneRenderTarget::Cleanup()
{
    if (m_vkCore && m_vkCore->GetDevice() != VK_NULL_HANDLE && HasOwnedResources()) {
        if (!m_vkCore->IsShuttingDown()) {
            m_vkCore->GetDeviceContext().WaitIdle();
        }
        CleanupResources();
    }
}

} // namespace infernux
