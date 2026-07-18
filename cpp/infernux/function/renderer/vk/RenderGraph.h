/**
 * @file RenderGraph.h
 * @brief Frame graph / Render graph system for modern rendering architecture
 *
 * The RenderGraph provides a declarative way to describe the rendering pipeline.
 * It enables:
 * - Automatic resource barrier management
 * - Render pass optimization and merging
 * - Resource lifetime tracking and transient resource allocation
 * - Easy visualization of the rendering pipeline
 * - Future: Python layer configuration for dynamic pipeline modification
 *
 * Architecture Notes:
 * - Resources are virtual handles that get resolved to actual GPU resources at execution
 * - Passes are recorded first, then compiled to optimize barriers and resource usage
 * - Designed for easy extension with pybind11 for Python layer control
 *
 * Usage:
 *   RenderGraph graph;
 *
 *   // Define passes
 *   auto gbufferPass = graph.AddPass("GBuffer", [&](PassBuilder& builder) {
 *       auto albedo = builder.CreateTexture("Albedo", width, height, format);
 *       auto depth = builder.CreateDepthStencil("Depth", width, height);
 *       builder.WriteColor(albedo);
 *       builder.WriteDepth(depth);
 *       return [=](RenderContext& ctx) {
 *           // Render geometry
 *       };
 *   });
 *
 *   auto lightingPass = graph.AddPass("Lighting", [&](PassBuilder& builder) {
 *       builder.Read(gbufferAlbedo);
 *       builder.Read(gbufferDepth);
 *       builder.WriteColor(backbuffer);
 *       return [=](RenderContext& ctx) {
 *           // Apply lighting
 *       };
 *   });
 *
 *   graph.Compile();
 *   graph.Execute(commandBuffer);
 */

#pragma once

#include "VkTypes.h"
#include "VulkanRhiDevice.h"
#include <function/renderer/ProfileConfig.h>
#include <function/renderer/RenderGraphIdentity.h>
#include <functional>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>
#include <vk_mem_alloc.h>

namespace infernux
{
namespace vk
{

// Forward declarations
class VkDeviceContext;
class VkPipelineManager;
class RenderGraph;
class RenderPass;
class PassBuilder;
class RenderContext;

// ============================================================================
// Resource Handles
// ============================================================================

/**
 * @brief Resource type enumeration
 */
enum class ResourceType
{
    Buffer,
    Texture2D,
    TextureCube,
    DepthStencil
};

/**
 * @brief Resource usage flags for a pass
 */
enum class ResourceUsage
{
    None = 0,
    Read = 1 << 0,
    Write = 1 << 1,
    ReadWrite = Read | Write,
    ColorOutput = 1 << 2,
    DepthOutput = 1 << 3,
    ShaderRead = 1 << 4,
    Transfer = 1 << 5,
    DepthRead = 1 << 6, ///< Read-only depth attachment (depth testing without writing)
    IndirectArgument = 1 << 7,
    VersionDependency = 1 << 8, ///< Graph ordering only; emits no backend access or barrier
    Present = 1 << 9            ///< Final presentation/export read
};

inline ResourceUsage operator|(ResourceUsage a, ResourceUsage b)
{
    return static_cast<ResourceUsage>(static_cast<int>(a) | static_cast<int>(b));
}

inline ResourceUsage operator&(ResourceUsage a, ResourceUsage b)
{
    return static_cast<ResourceUsage>(static_cast<int>(a) & static_cast<int>(b));
}

using ResourceHandle = GraphResourceHandle;
using ResourceHandleHash = GraphResourceHandleHash;

/**
 * @brief Description of a virtual texture resource
 */
struct TextureDesc
{
    std::string name;
    uint32_t width = 0;
    uint32_t height = 0;
    uint32_t depth = 1;
    uint32_t mipLevels = 1;
    uint32_t arrayLayers = 1;
    VkFormat format = VK_FORMAT_UNDEFINED;
    VkSampleCountFlagBits samples = VK_SAMPLE_COUNT_1_BIT;
    bool isTransient = true; // Can be aliased with other resources
};

/**
 * @brief Description of a virtual buffer resource
 */
struct BufferDesc
{
    std::string name;
    VkDeviceSize size = 0;
    VkBufferUsageFlags usage = 0;
    bool isTransient = true;
};

// ============================================================================
// Pass Definitions
// ============================================================================

using PassHandle = GraphPassHandle;
using PassHandleHash = GraphPassHandleHash;

/**
 * @brief Pass type enumeration
 */
enum class PassType
{
    Graphics, ///< Regular graphics pass with render targets
    Compute,  ///< Compute dispatch without a render pass
    Transfer, ///< Resource copy/transfer pass
    Present   ///< Final present pass
};

enum class PassCullReason
{
    Unreachable,
    GraphOutput,
    SideEffect,
    ExternalWrite,
    Dependency
};

struct PassCompileInfo
{
    std::string name;
    PassType type = PassType::Graphics;
    bool culled = true;
    PassCullReason reason = PassCullReason::Unreachable;
};

/**
 * @brief Configuration for a render pass
 */
struct PassConfig
{
    std::string name;
    PassType type = PassType::Graphics;
    VkPipelineStageFlags stageMask = VK_PIPELINE_STAGE_ALL_GRAPHICS_BIT;
};

/**
 * @brief Resource access record for dependency tracking
 */
struct ResourceAccess
{
    ResourceHandle handle;
    ResourceUsage usage = ResourceUsage::None;
    VkPipelineStageFlags stages = 0;
    VkAccessFlags access = 0;
    VkImageLayout layout = VK_IMAGE_LAYOUT_UNDEFINED;
};

// ============================================================================
// Resource state and layout tracking
// ============================================================================

/**
 * @brief Tracks the current Vulkan state of a resource after each pass
 *
 * Used for precise barrier insertion: knowing the old layout, access mask,
 * and pipeline stages allows generating exact VkImageMemoryBarrier instead
 * of pessimistic TOP_OF_PIPE → ALL_GRAPHICS barriers.
 */
struct ResourceState
{
    VkImageLayout layout = VK_IMAGE_LAYOUT_UNDEFINED;
    VkAccessFlags accessMask = 0;
    VkPipelineStageFlags stages = 0;
    uint32_t writerPassId = UINT32_MAX; ///< Pass that last wrote/used this resource
};

// ============================================================================
// Render Context
// ============================================================================

/**
 * @brief Context provided to pass execute callbacks
 *
 * This provides access to resolved resources and command buffer for rendering.
 */
class RenderContext
{
  public:
    RenderContext(VkCommandBuffer cmdBuffer, RenderGraph *graph);

    /// @brief Get the command buffer
    [[nodiscard]] VkCommandBuffer GetCommandBuffer() const
    {
        return m_cmdBuffer;
    }

    /// @brief Get the current viewport
    [[nodiscard]] VkViewport GetViewport() const
    {
        return m_viewport;
    }

    /// @brief Get the current scissor rect
    [[nodiscard]] VkRect2D GetScissor() const
    {
        return m_scissor;
    }

    /// @brief Set viewport for rendering
    void SetViewport(const VkViewport &viewport);

    /// @brief Set scissor rect
    void SetScissor(const VkRect2D &scissor);

    /// @brief Bind a graphics pipeline
    void BindPipeline(VkPipeline pipeline);

    /// @brief Draw command
    void Draw(uint32_t vertexCount, uint32_t instanceCount = 1, uint32_t firstVertex = 0, uint32_t firstInstance = 0);

    /// @brief Indexed draw command
    void DrawIndexed(uint32_t indexCount, uint32_t instanceCount = 1, uint32_t firstIndex = 0, int32_t vertexOffset = 0,
                     uint32_t firstInstance = 0);

    /// @brief Transition to the next subpass (for multi-subpass render passes)
    void NextSubpass();

    /// @brief Get resolved texture for a resource handle
    [[nodiscard]] VkImageView GetTexture(ResourceHandle handle) const;
    [[nodiscard]] VkImage GetImage(ResourceHandle handle) const;

    /// Backend-neutral texture view used by RHI draw paths.
    [[nodiscard]] rhi::TextureViewHandle GetTextureView(ResourceHandle handle) const;

    [[nodiscard]] rhi::GraphicsCommandEncoder &GetGraphicsCommandEncoder()
    {
        return m_graphicsEncoder;
    }

    [[nodiscard]] rhi::ComputeCommandEncoder &GetComputeCommandEncoder()
    {
        return m_computeEncoder;
    }

    /// @brief Get resolved buffer for a resource handle
    [[nodiscard]] VkBuffer GetBuffer(ResourceHandle handle) const;
    [[nodiscard]] rhi::BufferHandle GetBufferHandle(ResourceHandle handle) const;

  private:
    VkCommandBuffer m_cmdBuffer;
    RenderGraph *m_graph;
    VulkanGraphicsCommandContext m_graphicsCommandContext;
    rhi::GraphicsCommandEncoder m_graphicsEncoder;
    VulkanComputeCommandContext m_computeCommandContext;
    rhi::ComputeCommandEncoder m_computeEncoder;
    VkViewport m_viewport{};
    VkRect2D m_scissor{};
};

// ============================================================================
// Pass Builder
// ============================================================================

/**
 * @brief Builder for configuring render pass resources
 *
 * Used in pass setup callbacks to declare resource dependencies.
 */
class PassBuilder
{
  public:
    PassBuilder(RenderGraph *graph, uint32_t passId);

    /// @brief Create a new transient texture resource
    [[nodiscard]] ResourceHandle CreateTexture(const std::string &name, uint32_t width, uint32_t height,
                                               VkFormat format, VkSampleCountFlagBits samples = VK_SAMPLE_COUNT_1_BIT);

    /// @brief Create a new transient depth/stencil resource
    [[nodiscard]] ResourceHandle CreateDepthStencil(const std::string &name, uint32_t width, uint32_t height,
                                                    VkFormat format = VK_FORMAT_D32_SFLOAT,
                                                    VkSampleCountFlagBits samples = VK_SAMPLE_COUNT_1_BIT);

    /// @brief Create a new transient buffer resource
    [[nodiscard]] ResourceHandle CreateBuffer(const std::string &name, VkDeviceSize size, VkBufferUsageFlags usage);

    /// @brief Import an external texture (e.g., swapchain image)
    [[nodiscard]] ResourceHandle ImportTexture(const std::string &name, VkImage image, VkImageView view,
                                               VkFormat format, uint32_t width, uint32_t height);

    /// @brief Import an external buffer
    [[nodiscard]] ResourceHandle ImportBuffer(const std::string &name, VkBuffer buffer, VkDeviceSize size);

    /// @brief Read a texture in shader
    ResourceHandle Read(ResourceHandle handle, VkPipelineStageFlags stages = VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT);

    /// @brief Read a depth texture in shader as sampler2D
    ResourceHandle ReadSampledDepth(ResourceHandle handle,
                                    VkPipelineStageFlags stages = VK_PIPELINE_STAGE_FRAGMENT_SHADER_BIT);

    /// @brief Write to a color attachment
    ResourceHandle WriteColor(ResourceHandle handle, uint32_t attachmentIndex = 0);

    /// @brief Write to depth/stencil attachment
    ResourceHandle WriteDepth(ResourceHandle handle);

    /// @brief Read depth/stencil as a read-only attachment (for depth testing without writing)
    ResourceHandle ReadDepth(ResourceHandle handle);

    /// @brief Declare MSAA resolve target (1x image that receives resolved data)
    ResourceHandle WriteResolve(ResourceHandle handle);

    /// @brief Read/Write a resource (UAV access)
    ResourceHandle ReadWrite(ResourceHandle handle, VkPipelineStageFlags stages);

    /// Read a storage buffer from a compute shader.
    ResourceHandle ReadStorageBuffer(ResourceHandle handle);

    /// Write a storage buffer from a compute shader.
    ResourceHandle WriteStorageBuffer(ResourceHandle handle);

    /// Consume a buffer as graphics indirect draw arguments.
    ResourceHandle ReadIndirectBuffer(ResourceHandle handle);

    /// @brief Read a resource as transfer source (for blit/copy operations)
    ResourceHandle TransferRead(ResourceHandle handle);

    /// @brief Write a resource as transfer destination (for blit/copy operations)
    ResourceHandle TransferWrite(ResourceHandle handle);

    /// Declare the final presentation read and retain this pass as an external side effect.
    ResourceHandle PresentRead(ResourceHandle handle);

    /// Keep this pass even when it has no path to a graph output. Use only for
    /// externally observable work such as readback, events, or debug capture.
    void SetSideEffect(bool enabled = true);

    /// @brief Set the pass render area
    void SetRenderArea(uint32_t width, uint32_t height);

    /// @brief Enable/disable depth test
    void SetDepthTest(bool enable)
    {
        m_depthTestEnabled = enable;
    }

    /// @brief Set clear values
    void SetClearColor(float r, float g, float b, float a = 1.0f);
    void SetClearDepth(float depth, uint32_t stencil = 0);

  private:
    RenderGraph *m_graph;
    uint32_t m_passId;
    bool m_depthTestEnabled = true;

    friend class RenderGraph;
};

// ============================================================================
// Render Pass Data
// ============================================================================

/**
 * @brief Internal data structure for a render pass
 */
struct RenderPassData
{
    std::string name;
    uint32_t id = 0;
    PassType type = PassType::Graphics;

    // Resource accesses
    std::vector<ResourceAccess> reads;
    std::vector<ResourceAccess> writes;

    // Color/depth outputs
    std::vector<ResourceHandle> colorOutputs;
    ResourceHandle depthOutput;
    ResourceHandle depthInput;    ///< Read-only depth attachment shared from an earlier pass
    ResourceHandle resolveOutput; // MSAA resolve target (1x)

    // Render area
    VkExtent2D renderArea{0, 0};

    // Clear values
    VkClearColorValue clearColor = {{0.0f, 0.0f, 0.0f, 1.0f}};
    VkClearDepthStencilValue clearDepth = {1.0f, 0};
    bool clearColorEnabled = false;
    bool clearDepthEnabled = false;
    bool hasResolveAttachment = false; // True when MSAA resolve is used
    bool hasSideEffect = false;

    // Vulkan objects (resolved during compile)
    VkRenderPass vulkanRenderPass = VK_NULL_HANDLE;
    rhi::RenderTargetLayoutHandle renderTargetLayout;
    VkFramebuffer framebuffer = VK_NULL_HANDLE;

    // Execute callback
    std::function<void(RenderContext &)> executeCallback;

    // Dependency tracking
    std::vector<uint32_t> dependsOn;
    uint32_t refCount = 0;
    bool culled = false;
    PassCullReason cullReason = PassCullReason::Unreachable;

    // Pre-computed execution data (populated at end of Compile, used in Execute).
    // Eliminates per-frame struct construction for beginInfo, clear values,
    // viewport, and scissor.
    VkRenderPassBeginInfo cachedBeginInfo{};
    VkClearValue cachedClearValues[10]{};
    uint32_t cachedClearValueCount = 0;
    VkViewport cachedViewport{};
    VkRect2D cachedScissor{};
};

/**
 * @brief Internal data structure for a resource
 */
struct ResourceData
{
    std::string name;
    ResourceType type = ResourceType::Texture2D;

    // Texture info
    TextureDesc textureDesc;

    // Buffer info
    BufferDesc bufferDesc;

    // External resource (imported)
    bool isExternal = false;
    VkImage externalImage = VK_NULL_HANDLE;
    VkImageView externalView = VK_NULL_HANDLE;
    rhi::TextureViewHandle rhiView;
    VkBuffer externalBuffer = VK_NULL_HANDLE;
    rhi::BufferHandle rhiBuffer;

    // Allocated resources (for transient)
    VkImage allocatedImage = VK_NULL_HANDLE;
    VkImageView allocatedView = VK_NULL_HANDLE;
    VmaAllocation allocatedMemory = VK_NULL_HANDLE;
    VkBuffer allocatedBuffer = VK_NULL_HANDLE;

    // Lifetime tracking
    uint32_t firstPass = UINT32_MAX;
    uint32_t lastPass = 0;
    uint32_t refCount = 0;

    // Layout state tracking
    VkImageLayout currentLayout = VK_IMAGE_LAYOUT_UNDEFINED;
};

// ============================================================================
// Render Graph
// ============================================================================

/**
 * @brief Execute callback type for passes
 */
using PassExecuteCallback = std::function<void(RenderContext &)>;

/**
 * @brief Setup callback type for passes
 */
using PassSetupCallback = std::function<PassExecuteCallback(PassBuilder &)>;

/**
 * @brief Main render graph class
 *
 * Manages the entire frame rendering pipeline with automatic
 * resource management and barrier handling.
 */
class RenderGraph
{
  public:
#if INFERNUX_FRAME_PROFILE
    struct ExecuteProfileSnapshot
    {
        double barrierMs = 0.0;
        double beginPassMs = 0.0;
        double callbackMs = 0.0;
        double endPassMs = 0.0;
        uint64_t executeCalls = 0;
        uint64_t passCount = 0;
        uint64_t graphicsPassCount = 0;
        uint64_t computePassCount = 0;
        uint64_t barrierCallCount = 0;
    };

    struct PassCallbackProfileEntry
    {
        std::string name;
        double totalMs = 0.0;
        uint64_t calls = 0;
    };
#endif

    RenderGraph();
    ~RenderGraph();

    // Non-copyable, movable
    RenderGraph(const RenderGraph &) = delete;
    RenderGraph &operator=(const RenderGraph &) = delete;
    RenderGraph(RenderGraph &&other) noexcept;
    RenderGraph &operator=(RenderGraph &&other) noexcept;

    // ========================================================================
    // Initialization
    // ========================================================================

    /**
     * @brief Initialize the render graph
     *
     * @param context Device context for Vulkan access
     * @param pipelineManager Pipeline manager for render pass creation
     */
    void Initialize(VkDeviceContext *context, VkPipelineManager *pipelineManager);

    /**
     * @brief Reset the graph for a new frame
     *
     * Clears all passes and transient resources.
     */
    void Reset();

    /**
     * @brief Cleanup all resources
     */
    void Destroy();

    // ========================================================================
    // Graph Building
    // ========================================================================

    /**
     * @brief Add a render pass to the graph
     *
     * @param name Pass name for debugging
     * @param setup Setup callback that configures resources and returns execute callback
     * @return Pass handle
     */
    PassHandle AddPass(const std::string &name, PassSetupCallback setup);

    /// Add a compute pass. It records commands outside a Vulkan render pass.
    PassHandle AddComputePass(const std::string &name, PassSetupCallback setup);

    /**
     * @brief Add a transfer pass to the graph (copy/blit operations, no render pass)
     */
    PassHandle AddTransferPass(const std::string &name, PassSetupCallback setup);

    /// Add a final presentation/export pass outside a Vulkan render pass.
    PassHandle AddPresentPass(const std::string &name, PassSetupCallback setup);

    /**
     * @brief Set the backbuffer (swapchain image) for this frame
     */
    ResourceHandle SetBackbuffer(VkImage image, VkImageView view, VkFormat format, uint32_t width, uint32_t height,
                                 VkSampleCountFlagBits samples = VK_SAMPLE_COUNT_1_BIT,
                                 VkImageLayout initialLayout = VK_IMAGE_LAYOUT_MAX_ENUM);

    /**
     * @brief Set the desired final image layout for the backbuffer after all passes.
     *
     * For swapchain targets, use VK_IMAGE_LAYOUT_PRESENT_SRC_KHR.
     * Default is VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL (offscreen scene targets).
     */
    void SetBackbufferFinalLayout(VkImageLayout layout)
    {
        m_backbufferFinalLayout = layout;
    }

    /**
     * @brief Import an external texture as an MSAA resolve target
     */
    ResourceHandle ImportResolveTarget(VkImage image, VkImageView view, VkFormat format, uint32_t width,
                                       uint32_t height);

    /**
     * @brief Override the initial tracked state of an imported/external resource.
     *
     * External resources can be transitioned outside RenderGraph::Execute()
     * (for example by post-scene callbacks or explicit resolve barriers).
     * Call this before Execute() to keep the tracked oldLayout aligned with
     * the real Vulkan image layout at frame start.
     */
    void SetResourceInitialState(ResourceHandle handle, VkImageLayout layout, VkAccessFlags accessMask,
                                 VkPipelineStageFlags stages);

    /**
     * @brief Mark a resource as the final output
     */
    void SetOutput(ResourceHandle handle);

#if INFERNUX_FRAME_PROFILE
    static ExecuteProfileSnapshot GetExecuteProfileSnapshot();
    static std::vector<PassCallbackProfileEntry> GetTopCallbackProfiles(size_t maxEntries);
    static void ResetExecuteProfileSnapshot();
#endif

    /**
     * @brief Pre-register a transient texture resource before passes are added.
     *
     * This allocates a real ResourceData slot so the returned handle is
     * valid for ResolveTextureView() after Compile().  The texture will be
     * allocated during Compile()'s AllocateResources() phase.
     *
     * @param name       Debug name for the resource
     * @param width      Texture width
     * @param height     Texture height
     * @param format     Vulkan format
     * @param samples    MSAA sample count
     * @param isTransient If true, the resource can be memory-aliased
     * @return ResourceHandle with a valid id
     */
    ResourceHandle RegisterTransientTexture(const std::string &name, uint32_t width, uint32_t height, VkFormat format,
                                            VkSampleCountFlagBits samples = VK_SAMPLE_COUNT_1_BIT,
                                            bool isTransient = true);

    /// Pre-register a transient buffer before pass declarations reference it.
    ResourceHandle RegisterTransientBuffer(const std::string &name, VkDeviceSize size, VkBufferUsageFlags usage);

    // ========================================================================
    // Compilation and Execution
    // ========================================================================

    /**
     * @brief Compile the render graph
     *
     * This performs:
     * - Dead pass culling
     * - Resource lifetime analysis
     * - Barrier generation
     * - Render pass creation/caching
     *
     * @return true if compilation succeeded
     */
    bool Compile();

    /**
     * @brief Execute the render graph
     *
     * @param commandBuffer Command buffer to record into
     */
    void Execute(VkCommandBuffer commandBuffer);

    // ========================================================================
    // Per-Frame Clear Value Updates (no rebuild/recompile needed)
    // ========================================================================

    /**
     * @brief Update a pass's clear color value without rebuilding the graph.
     *
     * Only modifies the VkClearColorValue used in VkRenderPassBeginInfo;
     * does NOT change the VkRenderPass loadOp.  Safe to call every frame.
     *
     * @param passName  Name of the target pass
     * @param r, g, b, a  New clear color components
     * @return true if the pass was found and updated
     */
    bool UpdatePassClearColor(const std::string &passName, float r, float g, float b, float a);

    /**
     * @brief Update a pass's clear depth value without rebuilding the graph.
     *
     * @param passName  Name of the target pass
     * @param depth     New depth clear value
     * @param stencil   New stencil clear value
     * @return true if the pass was found and updated
     */
    bool UpdatePassClearDepth(const std::string &passName, float depth, uint32_t stencil = 0);

    // ========================================================================
    // Debug / Visualization
    // ========================================================================

    /**
     * @brief Get a text representation of the graph for debugging
     */
    [[nodiscard]] std::string GetDebugString() const;

    /**
     * @brief Get pass count
     */
    [[nodiscard]] size_t GetPassCount() const
    {
        return m_passes.size();
    }

    /// Passes in the compiled execution order, excluding culled passes.
    [[nodiscard]] std::vector<std::string> GetExecutionPassNames() const;

    /// Declaration-order culling report for Graph Viewer and diagnostics.
    [[nodiscard]] std::vector<PassCompileInfo> GetPassCompileInfos() const;

    /**
     * @brief Get resource count
     */
    [[nodiscard]] size_t GetResourceCount() const
    {
        return m_resources.size();
    }

    [[nodiscard]] RenderGraphScopeId GetIdentityScope() const noexcept
    {
        return m_identity.Current();
    }

    [[nodiscard]] bool Owns(ResourceHandle handle) const noexcept
    {
        return handle.IsValid() && handle.scope == m_identity.Current() && handle.id < m_resources.size() &&
               handle.id < m_resourceVersions.size() && handle.version <= m_resourceVersions[handle.id];
    }

    [[nodiscard]] bool Owns(PassHandle handle) const noexcept
    {
        return handle.IsValid() && handle.scope == m_identity.Current();
    }

    /**
     * @brief Get the Vulkan render pass for a specific pass
     * @param passName Name of the pass
     * @return VkRenderPass or VK_NULL_HANDLE if not found
     */
    [[nodiscard]] VkRenderPass GetPassRenderPass(const std::string &passName) const;

    [[nodiscard]] VkRenderPass GetPassRenderPass(PassHandle pass) const;

    [[nodiscard]] rhi::RenderTargetLayoutHandle GetPassRenderTargetLayout(const std::string &passName) const;

    [[nodiscard]] rhi::RenderTargetLayoutHandle GetPassRenderTargetLayout(PassHandle pass) const;

    /**
     * @brief Get the first graphics pass render pass
     * @return VkRenderPass suitable for pipeline creation, or VK_NULL_HANDLE
     */
    [[nodiscard]] VkRenderPass GetCompatibleRenderPass() const;

    // ========================================================================
    // Resource Resolution (for RenderContext)
    // ========================================================================

    [[nodiscard]] VkImageView ResolveTextureView(ResourceHandle handle) const;
    [[nodiscard]] VkImage ResolveImage(ResourceHandle handle) const;
    [[nodiscard]] rhi::TextureViewHandle ResolveRhiTextureView(ResourceHandle handle) const;
    [[nodiscard]] VkBuffer ResolveBuffer(ResourceHandle handle) const;
    [[nodiscard]] rhi::BufferHandle ResolveRhiBuffer(ResourceHandle handle) const;
    [[nodiscard]] uint64_t GetTransientResidentBytes() const;

  private:
    // ========================================================================
    // Internal Methods
    // ========================================================================

    // PassBuilder needs access to internal methods and data
    friend class PassBuilder;
    friend class RenderContext;

    /// @brief Create a new resource entry
    ResourceHandle CreateResource(const std::string &name, ResourceType type);

    /// Produce the next SSA-style version for a resource write. Writes must
    /// consume the latest version; reads may keep referencing older versions.
    ResourceHandle AdvanceResourceVersion(ResourceHandle handle);

    /// @brief Cull unused passes (from output backwards)
    void CullPasses();

    /// @brief Compute resource lifetimes
    void ComputeResourceLifetimes();

    /// @brief Topological sort via Kahn's algorithm. Returns false for cycles.
    bool TopologicalSort();

    /// @brief Allocate transient resources
    bool AllocateResources();

    /// @brief Create Vulkan render passes
    bool CreateVulkanRenderPasses();

    /// @brief Create framebuffers
    bool CreateFramebuffers();

    /// @brief Pre-compute per-pass VkRenderPassBeginInfo, clear values,
    ///        viewport and scissor so Execute() can skip per-frame construction.
    void PrecomputeExecuteData();

    /// @brief Insert barriers between passes using tracked resource layouts
    void InsertBarriers(VkCommandBuffer cmdBuffer, uint32_t passIndex);

    /// @brief Free transient resources
    void FreeResources();

    // ========================================================================
    // Layout / barrier helpers
    // ========================================================================

    /// @brief Convert ResourceUsage to appropriate VkImageLayout
    static VkImageLayout UsageToLayout(ResourceUsage usage, ResourceType type);

    /// @brief Convert ResourceUsage to VkAccessFlags
    static VkAccessFlags UsageToAccessMask(ResourceUsage usage);

    /// @brief Convert ResourceUsage to VkPipelineStageFlags
    static VkPipelineStageFlags UsageToStageFlags(ResourceUsage usage);

    /// @brief Get the effective depth handle for a pass (write takes priority over read)
    static ResourceHandle GetEffectiveDepth(const RenderPassData &pass);

    /// @brief Determine if a resource is used after a given pass index
    bool IsResourceUsedAfter(uint32_t resourceId, uint32_t passIndex) const;

    // ========================================================================
    // RenderPass / framebuffer caching
    // ========================================================================

    /// @brief Compute hash for RenderPassConfig (for cache lookup)
    static size_t HashRenderPassConfig(VkFormat colorFmt, VkFormat depthFmt, VkSampleCountFlagBits samples,
                                       bool clearColor, bool clearDepth, bool storeDepth,
                                       VkImageLayout colorFinalLayout, bool hasResolve = false,
                                       VkFormat resolveFormat = VK_FORMAT_UNDEFINED, bool hasColorAttachments = true,
                                       bool readOnlyDepth = false);

    /// @brief Compute hash for Framebuffer (for cache lookup)
    static size_t HashFramebuffer(VkRenderPass renderPass, const std::vector<VkImageView> &attachments, uint32_t width,
                                  uint32_t height);

    /// @brief Flush unused cache entries (GC)
    void FlushUnusedCaches();

  private:
    RenderGraphIdentitySource m_identity;
    VkDeviceContext *m_context = nullptr;
    VulkanRhiDevice *m_rhiDevice = nullptr;
    VkPipelineManager *m_pipelineManager = nullptr;

    // Graph data
    std::vector<RenderPassData> m_passes;
    std::vector<ResourceData> m_resources;
    std::vector<uint32_t> m_resourceVersions;
    std::vector<uint32_t> m_executionOrder;

    // Output
    ResourceHandle m_backbuffer;
    ResourceHandle m_output;
    VkImageLayout m_backbufferFinalLayout = VK_IMAGE_LAYOUT_COLOR_ATTACHMENT_OPTIMAL;

    // State
    bool m_compiled = false;

    // Per-resource layout state (reset each Execute())
    // Flat vector indexed by resource id — O(1) lookup, memcpy reset.
    std::vector<ResourceState> m_resourceStates;
    // Initial states set during Import/SetBackbuffer — restored at the
    // start of each Execute() so external layout changes (e.g.
    // ResolveSceneMsaa) don't cause stale oldLayout in barriers.
    std::vector<ResourceState> m_initialResourceStates;

    // Pre-allocated scratch buffers reused every Execute() to avoid per-pass heap allocs.
    std::vector<VkImageMemoryBarrier> m_barrierScratch;
    std::vector<VkBufferMemoryBarrier> m_bufferBarrierScratch;
    std::vector<VkClearValue> m_clearValueScratch;

    // RenderPass cache (long-lived across frames)
    std::unordered_map<size_t, VkRenderPass> m_renderPassCache;
    std::unordered_map<size_t, rhi::RenderTargetLayoutHandle> m_renderTargetLayoutCache;

    // Framebuffer cache (long-lived across frames)
    struct FramebufferCacheEntry
    {
        VkFramebuffer framebuffer = VK_NULL_HANDLE;
        uint32_t unusedFrames = 0; ///< Frames since last use (for GC)
    };
    std::unordered_map<size_t, FramebufferCacheEntry> m_framebufferCache;

    // Track which cache entries were used this frame
    std::vector<size_t> m_usedRenderPassKeys;
    std::vector<size_t> m_usedFramebufferKeys;

    // Memory aliasing: shared VmaAllocation for transient resources
    // with non-overlapping lifetimes (not owned by any single resource).
    std::vector<VmaAllocation> m_aliasedMemoryHeaps;

#if INFERNUX_FRAME_PROFILE
    static ExecuteProfileSnapshot s_executeProfile; // The windows msvc will parse later. But linux gcc will not.
    inline static std::unordered_map<std::string, PassCallbackProfileEntry> s_callbackProfiles;
#endif
};

#if INFERNUX_FRAME_PROFILE
inline RenderGraph::ExecuteProfileSnapshot RenderGraph::s_executeProfile = {};
#endif

} // namespace vk
} // namespace infernux
