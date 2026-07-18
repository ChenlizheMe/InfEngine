#pragma once

#include <cstdint>
#include <vk_mem_alloc.h>
#include <vulkan/vulkan.h>

namespace infernux
{

class InxVkCoreModular;

/**
 * @brief Manages an offscreen render target for scene rendering.
 *
 * This creates a framebuffer with color and depth attachments that
 * can be used as an ImGui texture for display in the Scene View panel.
 */
class SceneRenderTarget
{
  public:
    SceneRenderTarget(InxVkCoreModular *vkCore);
    ~SceneRenderTarget();

    SceneRenderTarget(const SceneRenderTarget &) = delete;
    SceneRenderTarget &operator=(const SceneRenderTarget &) = delete;

    /// @brief Initialize the render target with given dimensions
    /// @param width Width of the render target
    /// @param height Height of the render target
    /// @return true if successful
    bool Initialize(uint32_t width, uint32_t height);

    /// @brief Resize the render target (recreates resources)
    /// @param width New width
    /// @param height New height
    void Resize(uint32_t width, uint32_t height);

    /// @brief Get the ImGui texture ID for displaying this render target
    /// @return Texture ID (VkDescriptorSet) or 0 if not ready
    uint64_t GetImGuiTextureId() const
    {
        return reinterpret_cast<uint64_t>(m_imguiDescriptorSet);
    }

    /// @brief Get current dimensions
    uint32_t GetWidth() const
    {
        return m_width;
    }
    uint32_t GetHeight() const
    {
        return m_height;
    }

    /// @brief Check if the render target is ready for use
    bool IsReady() const
    {
        return m_isInitialized;
    }

    // ========================================================================
    // Resource Access (for RenderGraph integration)
    // ========================================================================

    /// @brief Get color image handle for RenderGraph import
    VkImage GetColorImage() const
    {
        return m_colorImage;
    }

    /// @brief Get color image view for RenderGraph import
    VkImageView GetColorImageView() const
    {
        return m_colorImageView;
    }

    // ========================================================================
    // MSAA Resource Access (multisampled render target)
    // When MSAA is disabled (1x), these return the color attachment directly
    // so that the RenderGraph backbuffer points to the 1x image seamlessly.
    // ========================================================================

    /// @brief Get MSAA color image handle for RenderGraph import
    /// When MSAA is off, returns the 1x color image.
    VkImage GetMsaaColorImage() const
    {
        return (m_msaaSampleCount != VK_SAMPLE_COUNT_1_BIT && m_msaaColorImage != VK_NULL_HANDLE) ? m_msaaColorImage
                                                                                                  : m_colorImage;
    }

    /// @brief Get MSAA color image view for RenderGraph import
    /// When MSAA is off, returns the 1x color image view.
    VkImageView GetMsaaColorImageView() const
    {
        return (m_msaaSampleCount != VK_SAMPLE_COUNT_1_BIT && m_msaaColorImageView != VK_NULL_HANDLE)
                   ? m_msaaColorImageView
                   : m_colorImageView;
    }

    /// @brief Get MSAA sample count
    VkSampleCountFlagBits GetMsaaSampleCount() const
    {
        return m_msaaSampleCount;
    }

    /// @brief Set MSAA sample count (takes effect on next Initialize/Resize)
    void SetMsaaSampleCount(VkSampleCountFlagBits sampleCount)
    {
        m_msaaSampleCount = sampleCount;
    }

    /// @brief Check if MSAA is enabled (sample count > 1)
    bool IsMsaaEnabled() const
    {
        return m_msaaSampleCount != VK_SAMPLE_COUNT_1_BIT;
    }

    /// @brief Get depth image handle for RenderGraph import
    VkImage GetDepthImage() const
    {
        return m_depthImage;
    }

    /// @brief Get depth image view for RenderGraph import
    VkImageView GetDepthImageView() const
    {
        return m_depthImageView;
    }

    /// @brief Get scene color format.
    ///
    /// The scene target is HDR so emissive/light contributions > 1.0 can
    /// survive until bloom / post-process passes.
    VkFormat GetColorFormat() const
    {
        return VK_FORMAT_R16G16B16A16_SFLOAT;
    }

    /// @brief Get depth format
    VkFormat GetDepthFormat() const;

    [[nodiscard]] uint64_t GetResidentBytes() const;
    [[nodiscard]] uint64_t GetMsaaColorResidentBytes() const;

    // ========================================================================
    // Outline Mask Render Target (for post-process selection outline)
    // ========================================================================

    /// @brief Get outline mask image for framebuffer attachment
    VkImage GetOutlineMaskImage() const
    {
        return m_outlineMaskImage;
    }

    /// @brief Get outline mask image view for framebuffer attachment
    VkImageView GetOutlineMaskImageView() const
    {
        return m_outlineMaskImageView;
    }

    /// @brief Get outline mask sampler for sampling in composite pass
    VkSampler GetOutlineMaskSampler() const
    {
        return m_outlineMaskSampler;
    }

    /// @brief Cleanup all resources
    void Cleanup();

  private:
    void CreateColorAttachment();
    void CreateMsaaColorAttachment();
    void CreateDepthAttachment();
    void CreateOutlineMaskAttachment();
    void CreateImGuiDescriptor();
    void CleanupResources();

    InxVkCoreModular *m_vkCore = nullptr;

    uint32_t m_width = 0;
    uint32_t m_height = 0;
    bool m_isInitialized = false;
    VkSampleCountFlagBits m_msaaSampleCount = VK_SAMPLE_COUNT_4_BIT;

    // Color attachment (1x resolve target, sampled by ImGui)
    VkImage m_colorImage = VK_NULL_HANDLE;
    VmaAllocation m_colorAllocation = VK_NULL_HANDLE;
    VkImageView m_colorImageView = VK_NULL_HANDLE;

    // MSAA color attachment (2x/4x/8x; absent at 1x)
    VkImage m_msaaColorImage = VK_NULL_HANDLE;
    VmaAllocation m_msaaColorAllocation = VK_NULL_HANDLE;
    VkImageView m_msaaColorImageView = VK_NULL_HANDLE;

    // Depth attachment (sample count matches the active color target)
    VkImage m_depthImage = VK_NULL_HANDLE;
    VmaAllocation m_depthAllocation = VK_NULL_HANDLE;
    VkImageView m_depthImageView = VK_NULL_HANDLE;

    // Outline mask attachment (for screen-space edge detection)
    VkImage m_outlineMaskImage = VK_NULL_HANDLE;
    VmaAllocation m_outlineMaskAllocation = VK_NULL_HANDLE;
    VkImageView m_outlineMaskImageView = VK_NULL_HANDLE;
    VkSampler m_outlineMaskSampler = VK_NULL_HANDLE;

    // For ImGui display
    VkSampler m_sampler = VK_NULL_HANDLE;
    VkDescriptorSet m_imguiDescriptorSet = VK_NULL_HANDLE;
};

} // namespace infernux
