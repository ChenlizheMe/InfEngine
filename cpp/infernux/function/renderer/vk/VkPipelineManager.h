/**
 * @file VkPipelineManager.h
 * @brief Vulkan pipeline and render pass management
 *
 * This class manages:
 * - Shader module loading and compilation
 * - Graphics pipeline creation with configurable states
 * - Render pass creation
 * - Pipeline layout management
 * - Descriptor set layouts
 *
 * Architecture Notes:
 * - Designed for easy extension with RenderGraph integration
 * - Pipelines are cached by configuration hash (future)
 * - RAII: All Vulkan objects are automatically cleaned up
 *
 * Usage:
 *   VkPipelineManager pipelines;
 *   pipelines.Initialize(device);
 *
 *   // Create render pass
 *   RenderPassConfig rpConfig;
 *   rpConfig.colorFormat = swapchain.GetImageFormat();
 *   VkRenderPass renderPass = pipelines.CreateRenderPass(rpConfig);
 *
 *   // Create pipeline
 *   PipelineConfig pConfig;
 *   pConfig.vertexShaderPath = "shaders/vert.spv";
 *   pConfig.fragmentShaderPath = "shaders/frag.spv";
 *   pConfig.renderPass = renderPass;
 *   VkPipeline pipeline = pipelines.CreateGraphicsPipeline(pConfig);
 */

#pragma once

#include "VkTypes.h"
#include <string>
#include <unordered_map>
#include <vector>

namespace infernux
{
namespace vk
{

// Forward declarations
class VkDeviceContext;

/**
 * @brief Vertex input configuration for pipeline creation
 */
struct VertexInputConfig
{
    std::vector<VkVertexInputBindingDescription> bindings;
    std::vector<VkVertexInputAttributeDescription> attributes;
};

/**
 * @brief Render pass attachment configuration
 */
struct AttachmentConfig
{
    VkFormat format = VK_FORMAT_UNDEFINED;
    VkSampleCountFlagBits samples = VK_SAMPLE_COUNT_1_BIT;
    VkAttachmentLoadOp loadOp = VK_ATTACHMENT_LOAD_OP_CLEAR;
    VkAttachmentStoreOp storeOp = VK_ATTACHMENT_STORE_OP_STORE;
    VkImageLayout initialLayout = VK_IMAGE_LAYOUT_UNDEFINED;
    VkImageLayout finalLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;
    bool isDepth = false;
};

/**
 * @brief Configuration for render pass creation
 */
struct RenderPassConfig
{
    VkFormat colorFormat = VK_FORMAT_B8G8R8A8_UNORM;
    VkFormat depthFormat = VK_FORMAT_D32_SFLOAT;
    bool hasColor = true; ///< When false, no color attachment (depth-only pass, e.g. shadow map)
    bool hasDepth = true;
    bool clearColor = true;
    bool clearDepth = true;
    bool storeColor = true;     ///< Preserve color for a later pass that loads the same attachment
    bool storeDepth = false;    ///< If true, depth storeOp = STORE (needed when subsequent passes read depth)
    bool readOnlyDepth = false; ///< True when depth is a read-only input (depthInput, not depthOutput). Uses
                                ///< DEPTH_STENCIL_READ_ONLY_OPTIMAL layout for initial/final/subpass ref.
    VkSampleCountFlagBits samples = VK_SAMPLE_COUNT_1_BIT;
    /// Final layout for the color attachment after the render pass ends.
    /// Defaults to PRESENT_SRC_KHR for swapchain passes; offscreen passes
    /// should use COLOR_ATTACHMENT_OPTIMAL or SHADER_READ_ONLY_OPTIMAL.
    VkImageLayout colorFinalLayout = VK_IMAGE_LAYOUT_PRESENT_SRC_KHR;

    /// MSAA resolve attachment support
    bool hasResolve = false;
    VkFormat resolveFormat = VK_FORMAT_UNDEFINED;
    VkImageLayout resolveFinalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

    /// MRT (Multiple Render Targets) support.
    /// When non-empty, each entry specifies the format for that color attachment.
    /// colorFormat is used for attachment 0 (and as fallback when this is empty).
    std::vector<VkFormat> colorFormats;
};

/**
 * @brief Configuration for graphics pipeline creation
 */
struct PipelineConfig
{
    // Shader paths (SPIR-V bytecode)
    std::string vertexShaderPath;
    std::string fragmentShaderPath;
    std::vector<uint32_t> vertexShaderCode; // Alternative: direct bytecode
    std::vector<uint32_t> fragmentShaderCode;

    // Vertex input
    VertexInputConfig vertexInput;

    // Render pass
    VkRenderPass renderPass = VK_NULL_HANDLE;
    uint32_t subpass = 0;

    // Pipeline layout (if provided externally)
    VkPipelineLayout layout = VK_NULL_HANDLE;

    // Descriptor set layouts (if layout not provided)
    std::vector<VkDescriptorSetLayout> descriptorSetLayouts;
    std::vector<VkPushConstantRange> pushConstantRanges;

    // Rasterization state
    VkPrimitiveTopology topology = VK_PRIMITIVE_TOPOLOGY_TRIANGLE_LIST;
    VkPolygonMode polygonMode = VK_POLYGON_MODE_FILL;
    VkCullModeFlags cullMode = VK_CULL_MODE_BACK_BIT;
    VkFrontFace frontFace = VK_FRONT_FACE_CLOCKWISE;
    float lineWidth = 1.0f;
    bool depthClampEnable = false;

    // Depth/stencil state
    bool depthTestEnable = true;
    bool depthWriteEnable = true;
    VkCompareOp depthCompareOp = VK_COMPARE_OP_LESS;

    // Blending
    bool blendEnable = false;
    VkBlendFactor srcColorBlend = VK_BLEND_FACTOR_SRC_ALPHA;
    VkBlendFactor dstColorBlend = VK_BLEND_FACTOR_ONE_MINUS_SRC_ALPHA;
    VkBlendFactor srcAlphaBlend = VK_BLEND_FACTOR_ONE;
    VkBlendFactor dstAlphaBlend = VK_BLEND_FACTOR_ZERO;

    // Dynamic state
    std::vector<VkDynamicState> dynamicStates = {VK_DYNAMIC_STATE_VIEWPORT, VK_DYNAMIC_STATE_SCISSOR};

    // Multisampling
    VkSampleCountFlagBits samples = VK_SAMPLE_COUNT_1_BIT;

    // Viewport (used if not dynamic)
    VkExtent2D extent{800, 600};
};

/**
 * @brief Result of pipeline creation
 */
struct PipelineResult
{
    VkPipeline pipeline = VK_NULL_HANDLE;
    VkPipelineLayout layout = VK_NULL_HANDLE;
    bool layoutOwned = false; // Did we create the layout?
};

/**
 * @brief Manages Vulkan pipelines, render passes, and related objects
 */
class VkPipelineManager
{
  public:
    VkPipelineManager() = default;
    ~VkPipelineManager();

    // Non-copyable, movable
    VkPipelineManager(const VkPipelineManager &) = delete;
    VkPipelineManager &operator=(const VkPipelineManager &) = delete;
    VkPipelineManager(VkPipelineManager &&other) noexcept;
    VkPipelineManager &operator=(VkPipelineManager &&other) noexcept;

    // ========================================================================
    // Initialization
    // ========================================================================

    /**
     * @brief Initialize the pipeline manager
     * @param device Vulkan device handle
     */
    void Initialize(VkDevice device);

    /**
     * @brief Cleanup all managed resources
     */
    void Destroy() noexcept;

    /// @brief Check if initialized
    [[nodiscard]] bool IsValid() const
    {
        return m_device != VK_NULL_HANDLE;
    }

    // ========================================================================
    // Shader Management
    // ========================================================================

    /**
     * @brief Create a shader module from SPIR-V bytecode
     *
     * @param code SPIR-V bytecode
     * @return Shader module handle, or VK_NULL_HANDLE on failure
     */
    [[nodiscard]] VkShaderModule CreateShaderModule(const std::vector<uint32_t> &code);

    /**
     * @brief Load a shader module from file
     *
     * @param filePath Path to SPIR-V file
     * @return Shader module handle, or VK_NULL_HANDLE on failure
     */
    [[nodiscard]] VkShaderModule LoadShaderModule(const std::string &filePath);

    /**
     * @brief Destroy a shader module
     */
    void DestroyShaderModule(VkShaderModule module);

    // ========================================================================
    // Render Pass Management
    // ========================================================================

    /**
     * @brief Create a render pass with the specified configuration
     *
     * @param config Render pass configuration
     * @return Render pass handle, or VK_NULL_HANDLE on failure
     */
    [[nodiscard]] VkRenderPass CreateRenderPass(const RenderPassConfig &config);

    /**
     * @brief Create a simple single-subpass render pass
     *
     * @param colorFormat Color attachment format
     * @param depthFormat Depth attachment format (VK_FORMAT_UNDEFINED to disable)
     * @return Render pass handle
     */
    [[nodiscard]] VkRenderPass CreateSimpleRenderPass(VkFormat colorFormat, VkFormat depthFormat = VK_FORMAT_UNDEFINED);

    /**
     * @brief Destroy a render pass
     */
    void DestroyRenderPass(VkRenderPass renderPass);

    /**
     * @brief Transfer a render pass out of this manager without destroying it
     *
     * The caller owns the detached Vulkan object and must retire it safely.
     * RenderGraph uses this to coordinate cache lifetime with its deletion queue.
     */
    [[nodiscard]] bool DetachRenderPass(VkRenderPass renderPass) noexcept;

    // ========================================================================
    // Pipeline Layout Management
    // ========================================================================

    /**
     * @brief Create a pipeline layout
     *
     * @param descriptorSetLayouts Descriptor set layouts
     * @param pushConstantRanges Push constant ranges
     * @return Pipeline layout handle
     */
    [[nodiscard]] VkPipelineLayout
    CreatePipelineLayout(const std::vector<VkDescriptorSetLayout> &descriptorSetLayouts,
                         const std::vector<VkPushConstantRange> &pushConstantRanges = {});

    /**
     * @brief Destroy a pipeline layout
     */
    void DestroyPipelineLayout(VkPipelineLayout layout);

    // ========================================================================
    // Graphics Pipeline Management
    // ========================================================================

    /**
     * @brief Create a graphics pipeline with the specified configuration
     *
     * @param config Pipeline configuration
     * @return Pipeline result (pipeline + layout handles)
     */
    [[nodiscard]] PipelineResult CreateGraphicsPipeline(const PipelineConfig &config);

    /**
     * @brief Destroy a pipeline
     */
    void DestroyPipeline(VkPipeline pipeline);

    /**
     * @brief Destroy a pipeline result (pipeline and owned layout)
     */
    void DestroyPipelineResult(PipelineResult &result);

    // ========================================================================
    // Descriptor Set Layout Management
    // ========================================================================

    /**
     * @brief Create a descriptor set layout
     *
     * @param bindings Layout bindings
     * @return Descriptor set layout handle
     */
    [[nodiscard]] VkDescriptorSetLayout
    CreateDescriptorSetLayout(const std::vector<VkDescriptorSetLayoutBinding> &bindings);

    /**
     * @brief Destroy a descriptor set layout
     */
    void DestroyDescriptorSetLayout(VkDescriptorSetLayout layout);

    // ========================================================================
    // Standard Vertex Input Configurations
    // ========================================================================

    /**
     * @brief Get standard vertex input for 3D mesh rendering
     *
     * Layout: position (vec3), normal (vec3), texCoord (vec2)
     */
    [[nodiscard]] static VertexInputConfig GetStandardMeshVertexInput();

    /**
     * @brief Get vertex input for 2D UI rendering
     *
     * Layout: position (vec2), texCoord (vec2), color (vec4)
     */
    [[nodiscard]] static VertexInputConfig GetUIVertexInput();

  private:
    // ========================================================================
    // Internal Methods
    // ========================================================================

    /// @brief Read file contents as binary
    std::vector<uint32_t> ReadShaderFile(const std::string &filePath);

  public:
    void SetSkipWaitIdle(bool v)
    {
        m_skipWaitIdle = v;
    }

    /// @brief Forget non-pipeline tracked resources WITHOUT destroying them.
    /// Call this when the actual Vulkan handles have already been destroyed
    /// by other owners (RenderGraph, ShaderProgram, etc.) so that
    /// Destroy() does not double-free them. Tracked VkPipeline handles are
    /// intentionally preserved so Destroy() can still reclaim any leftovers.
    void ClearTrackedNonPipelineResources() noexcept
    {
        m_shaderModules.clear();
        m_renderPasses.clear();
        m_pipelineLayouts.clear();
        m_descriptorSetLayouts.clear();
    }

  private:
    bool m_skipWaitIdle = false;
    VkDevice m_device = VK_NULL_HANDLE;

    // Tracked objects for cleanup
    std::vector<VkShaderModule> m_shaderModules;
    std::vector<VkRenderPass> m_renderPasses;
    std::vector<VkPipelineLayout> m_pipelineLayouts;
    std::vector<VkPipeline> m_pipelines;
    std::vector<VkDescriptorSetLayout> m_descriptorSetLayouts;
};

} // namespace vk
} // namespace infernux
