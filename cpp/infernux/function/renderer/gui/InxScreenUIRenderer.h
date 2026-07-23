/**
 * @file InxScreenUIRenderer.h
 * @brief GPU-based 2D screen-space UI renderer for RenderGraph integration
 *
 * Renders screen-space UI elements (filled rects, text) directly into the
 * scene render target as a RenderGraph pass. Uses ImGui's ImDrawList for
 * command accumulation and font atlas for text rendering, but maintains its
 * own Vulkan pipeline and buffers to render inside scene render passes
 * (MSAA backbuffer) rather than the ImGui overlay pass.
 *
 * Two independent command lists are maintained:
 *   - Camera list: rendered before post-processing (Screen Space - Camera)
 *   - Overlay list: rendered after post-processing  (Screen Space - Overlay)
 *
 * This allows the Python-side RenderGraph to place ScreenUI passes at
 * different points in the pipeline depending on the canvas render mode.
 */

#pragma once

#include "../FrameDeletionQueue.h"

#include <functional>
#include <imgui.h>
#include <string>
#include <vector>
#include <vk_mem_alloc.h>
#include <vulkan/vulkan.h>

namespace infernux
{

/**
 * @brief Screen-space UI command list identifier
 */
enum class ScreenUIList
{
    Camera, ///< Rendered before post-processing
    Overlay ///< Rendered after post-processing
};

/**
 * @brief GPU-based 2D screen-space UI renderer
 *
 * Accumulates draw commands (filled rects, textured rects, text) per frame
 * and renders them into the scene MSAA render target during a RenderGraph pass.
 */
class InxScreenUIRenderer
{
  public:
    InxScreenUIRenderer();
    ~InxScreenUIRenderer();

    // Non-copyable
    InxScreenUIRenderer(const InxScreenUIRenderer &) = delete;
    InxScreenUIRenderer &operator=(const InxScreenUIRenderer &) = delete;

    /**
     * @brief Initialize the renderer
     * @param device Vulkan device
     * @param allocator VMA allocator for buffer management
     * @param colorFormat Scene color attachment format (e.g. B8G8R8A8_SRGB)
     * @param msaaSamples Scene MSAA sample count (e.g. 4x)
     * @return true if successful
     */
    bool Initialize(VkDevice device, VmaAllocator allocator, VkFormat colorFormat, VkSampleCountFlagBits msaaSamples);

    void SetDeletionQueue(FrameDeletionQueue *queue)
    {
        m_deletionQueue = queue;
    }

    void SetTextureUsageValidator(std::function<bool(uint64_t)> validator)
    {
        m_textureUsageValidator = std::move(validator);
    }

    /**
     * @brief Cleanup all resources
     */
    void Destroy();

    // ========================================================================
    // Per-Frame Command Accumulation
    // ========================================================================

    /**
     * @brief Begin a new frame — clears all accumulated commands
     *
     * Must be called once per frame before any Add* calls.
     * @param width  Scene render target width  (pixels)
     * @param height Scene render target height (pixels)
     */
    void BeginFrame(uint32_t width, uint32_t height);

    /**
     * @brief Reuse the previous draw lists when UI content is unchanged.
     * @return true when cached
     * commands were retained; false when callers
     *         must submit the frame's Add* commands again.
     */
    bool BeginFrameCached(uint32_t width, uint32_t height, uint64_t contentRevision);

    /**
     * @brief Add a filled rectangle
     */
    void AddFilledRect(ScreenUIList list, float minX, float minY, float maxX, float maxY, float r, float g, float b,
                       float a, float rounding = 0.0f);

    /**
     * @brief Add a textured image quad
     */
    void AddImage(ScreenUIList list, uint64_t textureId, float minX, float minY, float maxX, float maxY,
                  float uv0X = 0.0f, float uv0Y = 0.0f, float uv1X = 1.0f, float uv1Y = 1.0f, float r = 1.0f,
                  float g = 1.0f, float b = 1.0f, float a = 1.0f, float rotation = 0.0f, bool mirrorH = false,
                  bool mirrorV = false, float rounding = 0.0f);

    /**
     * @brief Add aligned text (uses ImGui font atlas)
     */
    void AddText(ScreenUIList list, float minX, float minY, float maxX, float maxY, const std::string &text, float r,
                 float g, float b, float a, float alignX, float alignY, float fontSize, float wrapWidth = 0.0f,
                 float rotation = 0.0f, bool mirrorH = false, bool mirrorV = false, const std::string &fontPath = "",
                 float lineHeight = 1.0f, float letterSpacing = 0.0f);
    std::pair<float, float> MeasureText(const std::string &text, float fontSize, float wrapWidth = 0.0f,
                                        const std::string &fontPath = "", float lineHeight = 1.0f,
                                        float letterSpacing = 0.0f) const;

    /**
     * @brief Check if a command list has any draw commands
     */
    bool HasCommands(ScreenUIList list) const;

    /**
     * @brief Enable or disable rendering (commands still accumulate)
     *
     * When disabled, Render() becomes a no-op. Useful for suppressing
     * screen-UI in the game texture while the UI editor draws its own
     * elements on top of the same texture.
     */
    void SetEnabled(bool enabled)
    {
        m_enabled = enabled;
    }
    bool IsEnabled() const
    {
        return m_enabled;
    }

    // ========================================================================
    // Rendering (called from RenderGraph pass callback)
    // ========================================================================

    /**
     * @brief Render the specified command list into the active render pass
     *
     * Must be called inside a Vulkan render pass that is compatible with
     * the color format and MSAA settings passed to Initialize().
     *
     * @param cmdBuf Vulkan command buffer (inside active render pass)
     * @param list   Which command list to render
     * @param width  Render target width
     * @param height Render target height
     */
    void Render(VkCommandBuffer cmdBuf, ScreenUIList list, uint32_t width, uint32_t height);

    /**
     * @brief Get the compatible render pass (for pipeline creation verification)
     */
    VkRenderPass GetCompatibleRenderPass() const
    {
        return m_renderPass;
    }

  private:
    /**
     * @brief Create Vulkan pipeline objects (shader modules, layouts, pipeline)
     */
    bool CreatePipeline();

    /**
     * @brief Create a compatible render pass for pipeline creation
     */
    bool CreateCompatibleRenderPass();

    // Per-list vertex / index buffers (Camera and Overlay each need their
    // own GPU buffers because both Render() calls are recorded into the same
    // command buffer — the second upload must not overwrite the first while
    // the GPU has not yet executed it.)
    struct ListBuffers
    {
        VkBuffer vertexBuffer = VK_NULL_HANDLE;
        VmaAllocation vertexAlloc = VK_NULL_HANDLE;
        VkDeviceSize vertexBufferSize = 0;

        VkBuffer indexBuffer = VK_NULL_HANDLE;
        VmaAllocation indexAlloc = VK_NULL_HANDLE;
        VkDeviceSize indexBufferSize = 0;
    };

    /**
     * @brief Ensure vertex/index buffers are large enough
     */
    bool EnsureBuffers(ListBuffers &buf, VkDeviceSize vertexSize, VkDeviceSize indexSize);

    /**
     * @brief Get the ImDrawList for a given list
     */
    ImDrawList *GetDrawList(ScreenUIList list);
    const ImDrawList *GetDrawList(ScreenUIList list) const;

    struct GPUVertex
    {
        ImVec2 pos;
        ImVec2 uv;
        float color[4];
    };

    struct HDRColorRange
    {
        int vertexStart = 0;
        int vertexEnd = 0;
        float rgbScale = 1.0f;
    };

    void TrackHDRColorRange(ScreenUIList list, int vertexStart, int vertexEnd, float rgbScale);
    std::vector<HDRColorRange> &GetHDRRanges(ScreenUIList list);
    const std::vector<HDRColorRange> &GetHDRRanges(ScreenUIList list) const;

    // Device
    VkDevice m_device = VK_NULL_HANDLE;
    VmaAllocator m_allocator = VK_NULL_HANDLE;

    // Formats
    VkFormat m_colorFormat = VK_FORMAT_UNDEFINED;
    VkSampleCountFlagBits m_msaaSamples = VK_SAMPLE_COUNT_1_BIT;

    // Pipeline objects
    VkShaderModule m_vertShader = VK_NULL_HANDLE;
    VkShaderModule m_fragShader = VK_NULL_HANDLE;
    VkDescriptorSetLayout m_descriptorSetLayout = VK_NULL_HANDLE;
    VkPipelineLayout m_pipelineLayout = VK_NULL_HANDLE;
    VkPipeline m_pipeline = VK_NULL_HANDLE;
    VkRenderPass m_renderPass = VK_NULL_HANDLE;

    // Font atlas descriptor (points to ImGui's font atlas)
    VkDescriptorSet m_fontDescriptorSet = VK_NULL_HANDLE;

    ListBuffers m_listBuffers[2]; // [0] = Camera, [1] = Overlay

    // ImDrawList instances (standalone, not attached to any ImGui window)
    ImDrawList *m_cameraDrawList = nullptr;
    ImDrawList *m_overlayDrawList = nullptr;
    std::vector<HDRColorRange> m_cameraHDRRanges;
    std::vector<HDRColorRange> m_overlayHDRRanges;

    bool m_initialized = false;
    bool m_enabled = true;
    bool m_commandCacheValid = false;
    uint32_t m_cachedWidth = 0;
    uint32_t m_cachedHeight = 0;
    uint64_t m_cachedContentRevision = 0;
    ImTextureID m_cachedFontTextureId = 0;
    FrameDeletionQueue *m_deletionQueue = nullptr;
    std::function<bool(uint64_t)> m_textureUsageValidator;
};

} // namespace infernux
