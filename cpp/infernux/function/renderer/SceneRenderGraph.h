/**
 * @file SceneRenderGraph.h
 * @brief RenderGraph-based scene rendering system
 *
 * This class fully integrates with the low-level vk::RenderGraph API for
 * declarative, frame-graph-driven rendering. All rendering is now handled
 * via RenderGraph passes - no more imperative BeginRenderPass/EndRenderPass.
 *
 * Architecture:
 * - Uses vk::RenderGraph for automatic resource management and barrier handling
 * - All passes are defined via RenderGraph's AddPass API
 * - Transient resources managed by RenderGraph
 * - External resources (scene target) imported into RenderGraph
 * - Supports GPU->CPU readback for Python/ML integration
 */

#pragma once

#include "FullscreenRenderer.h"
#include "InxRenderStruct.h"
#include "RenderGraphDescription.h"
#include "RendererList.h"
#include "vk/RenderGraph.h"
#include "vk/VkDeviceContext.h"
#include "vk/VkPipelineManager.h"
#include <functional>
#include <map>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

namespace infernux
{

class InxVkCoreModular;
class InxMaterial;
class InxScreenUIRenderer;
class SceneRenderTarget;

// Forward-declare from Camera.h
enum class CameraClearFlags;

/**
 * @brief Pass type enumeration for scene rendering
 */
enum class ScenePassType
{
    DepthPrePass, ///< Depth-only pass for early-z optimization
    ShadowPass,   ///< Shadow map generation
    MainColor,    ///< Main color pass with materials
    Transparent,  ///< Transparent objects (back-to-front)
    UI,           ///< UI overlay (ImGui)
    Custom        ///< Custom user-defined pass
};

/**
 * @brief Configuration for a scene render pass
 */
struct ScenePassConfig
{
    std::string name;
    ScenePassType type = ScenePassType::MainColor;
    bool enabled = true;

    // Clear settings
    bool clearColor = true;
    bool clearDepth = true;
    float clearColorValue[4] = {0.1f, 0.1f, 0.1f, 1.0f};
    float clearDepthValue = 1.0f;

    // Output settings for readback
    bool hasOwnRenderTarget = false; ///< If true, creates dedicated render target for this pass
    bool enableReadback = false;     ///< If true, allows CPU readback of output

    // Input dependencies (pass names to read from)
    std::vector<std::string> inputPasses;

    // ========================================================================
    // Resource and subpass support
    // ========================================================================

    // Resource declarations (if empty, uses default scene target)
    std::vector<vk::ResourceHandle> inputTextures;  ///< Input textures to read from
    std::vector<vk::ResourceHandle> outputTextures; ///< Output color attachments
    vk::ResourceHandle depthOutput;                 ///< Depth attachment (optional)

    // Subpass support - allows multiple subpasses in one RenderPass
    bool isSubpass = false;     ///< If true, this is a subpass of a parent pass
    std::string parentPassName; ///< Parent pass name (if isSubpass == true)
    uint32_t subpassIndex = 0;  ///< Index within parent pass's subpasses
};

/**
 * @brief Pass render callback signature using RenderGraph context
 * @param ctx RenderGraph context for drawing commands
 * @param width Render target width
 * @param height Render target height
 */
using ScenePassRenderCallback = std::function<void(vk::RenderContext &ctx, uint32_t width, uint32_t height)>;

/**
 * @brief RenderGraph-based scene rendering system
 *
 * Provides a fully declarative rendering pipeline using vk::RenderGraph.
 * All rendering is handled through RenderGraph passes with automatic
 * resource management and barrier handling.
 */
class SceneRenderGraph
{
  public:
    SceneRenderGraph();
    ~SceneRenderGraph();

    // Non-copyable
    SceneRenderGraph(const SceneRenderGraph &) = delete;
    SceneRenderGraph &operator=(const SceneRenderGraph &) = delete;

    /**
     * @brief Initialize the scene render graph
     * @param vkCore Vulkan core for resource access
     * @param sceneTarget Scene render target for external resources
     * @return true if successful
     */
    bool Initialize(InxVkCoreModular *vkCore, SceneRenderTarget *sceneTarget);

    /**
     * @brief Cleanup resources
     */
    void Destroy();

    [[nodiscard]] uint64_t GetTransientResidentBytes() const;

    // ========================================================================
    // RenderGraph topology defined from Python
    // ========================================================================

    /**
     * @brief Apply a render graph topology defined from Python
     *
     * Receives a RenderGraphDescription from Python and translates it into
     * SceneRenderGraph passes with appropriate callbacks. C++ retains
     * compilation authority (DAG compilation, barrier insertion, resource
     * allocation) while Python has definition authority (topology, pass
     * order, resource connections).
     *
     * @param desc The graph topology description from Python
     */
    void ApplyPythonGraph(const RenderGraphDescription &desc);

    /**
     * @brief Set the screen UI renderer for DrawScreenUI passes
     * @param renderer Pointer to the screen UI renderer (may be nullptr)
     */
    void SetScreenUIRenderer(InxScreenUIRenderer *renderer)
    {
        m_screenUIRenderer = renderer;
    }

    /**
     * @brief Check if a custom graph topology has been applied
     */
    [[nodiscard]] bool HasPythonGraph() const
    {
        return m_hasPythonGraph;
    }

    /**
     * @brief Get the MSAA sample count requested by the current graph (0 = no preference).
     */
    [[nodiscard]] int GetRequestedMsaaSamples() const
    {
        return m_hasPythonGraph ? m_pythonGraphDesc.msaaSamples : 0;
    }

    // ========================================================================
    // Resource management
    // ========================================================================

    /**
     * @brief Create a transient texture resource
     * @param name Resource name for debugging
     * @param width Texture width
     * @param height Texture height
     * @param format Vulkan format
     * @param isTransient If true, resource can be aliased
     * @return Resource handle for use in pass configuration
     */
    vk::ResourceHandle CreateTransientTexture(const std::string &name, uint32_t width, uint32_t height, VkFormat format,
                                              bool isTransient = true);

    /**
     * @brief Get the scene color target resource handle
     * @return Handle to the imported scene color target
     */
    [[nodiscard]] vk::ResourceHandle GetSceneColorTarget() const
    {
        return m_importedColorTarget;
    }

    /**
     * @brief Get the scene depth target resource handle
     * @return Handle to the imported scene depth target
     */
    [[nodiscard]] vk::ResourceHandle GetSceneDepthTarget() const
    {
        return m_importedDepthTarget;
    }

    // ========================================================================
    // Execution (Pure RenderGraph)
    // ========================================================================

    /**
     * @brief Build and execute the render graph for the current frame
     * @param commandBuffer Command buffer to record into
     *
     * This method:
     * 1. Applies per-frame camera clear overrides without rebuild
     * 2. Calls RenderGraph::Execute() to record all commands
     *
     * Call EnsureGraphBuilt() before command buffer recording to handle
     * rebuilds and compilation.
     */
    void Execute(VkCommandBuffer commandBuffer);

    /**
     * @brief Rebuild and compile the render graph if needed (pre-record phase).
     *
     * Must be called BEFORE command buffer recording starts.  Moves
     * BuildRenderGraph / Compile out of the recording path so that
     * descriptor set recreation (triggered by InvalidateAllMaterialPipelines)
     * does not destroy sets already bound to an in-recording command buffer.
     */
    void EnsureGraphBuilt();

    /// @brief Diagnostic: whether the graph is currently built and ready to execute
    [[nodiscard]] bool IsGraphBuilt() const
    {
        return m_graphBuilt;
    }

    /// @brief Diagnostic: whether the graph needs a rebuild before next execute
    [[nodiscard]] bool NeedsRebuild() const
    {
        return m_needsRebuild;
    }

    /**
     * @brief Called when scene render target is resized
     */
    void OnResize(uint32_t width, uint32_t height);

    /**
     * @brief Force rebuild of the render graph on next frame
     */
    void MarkDirty()
    {
        m_needsRebuild = true;
    }

    void UpdateMainPassClearSettings(CameraClearFlags clearFlags, const glm::vec4 &bgColor);

    // ========================================================================
    // ========================================================================
    // Debug
    // ========================================================================

    /**
     * @brief Get debug visualization of the render graph
     */
    [[nodiscard]] std::string GetDebugString() const;

    /**
     * @brief Get pass count
     */
    [[nodiscard]] size_t GetPassCount() const
    {
        return m_hasPythonGraph ? m_pythonGraphDesc.passes.size() : 0;
    }

    [[nodiscard]] const std::string &GetGraphName() const
    {
        return m_pythonGraphDesc.name;
    }

    [[nodiscard]] uint64_t GetExecutionCount() const
    {
        return m_executionCount;
    }

    [[nodiscard]] bool HasExecutedCurrentGraph() const
    {
        return m_graphBuilt && m_executionCount > 0 && m_lastExecutedBuildRevision == m_graphBuildRevision;
    }

    [[nodiscard]] std::vector<std::string> GetExecutedPassNames() const
    {
        return HasExecutedCurrentGraph() && m_renderGraph ? m_renderGraph->GetExecutionPassNames()
                                                          : std::vector<std::string>{};
    }

    // ========================================================================
    // Per-Graph Draw Call Cache (for multi-camera rendering)
    // ========================================================================

    /// @brief Cache the submitted renderer list for this graph.
    void SetCachedRendererList(RendererList rendererList)
    {
        m_cachedRenderers = std::move(rendererList);
        m_hasCachedDrawCalls = true;
    }

    /// @brief Cache owned or borrowed shadow-caster candidates for this graph.
    void SetCachedShadowRendererList(RendererList rendererList)
    {
        m_hasCachedShadowDrawCalls = !rendererList.Empty();
        m_cachedShadowRenderers = std::move(rendererList);
    }

    /// @brief Clear cached shadow-caster candidates.
    void ClearCachedShadowDrawCalls()
    {
        m_cachedShadowRenderers.Clear();
        m_hasCachedShadowDrawCalls = false;
    }

    /// @brief Get cached draw calls
    [[nodiscard]] const std::vector<DrawCall> &GetCachedDrawCalls() const
    {
        return m_cachedRenderers.DrawCalls();
    }

    [[nodiscard]] const RendererList &GetCachedRendererList() const
    {
        return m_cachedRenderers;
    }

    /// @brief Check if this graph has cached draw calls
    [[nodiscard]] bool HasCachedDrawCalls() const
    {
        return m_hasCachedDrawCalls;
    }

    /// @brief Get cached shadow draw calls.
    [[nodiscard]] const std::vector<DrawCall> &GetCachedShadowDrawCalls() const
    {
        return m_cachedShadowRenderers.DrawCalls();
    }

    [[nodiscard]] const RendererList &GetCachedShadowRendererList() const
    {
        return m_cachedShadowRenderers;
    }

    /// @brief Check if this graph has cached shadow draw calls.
    [[nodiscard]] bool HasCachedShadowDrawCalls() const
    {
        return m_hasCachedShadowDrawCalls;
    }

    /// @brief True when the current Python graph contains a shadow-caster pass.
    [[nodiscard]] bool HasShadowCasterPass() const
    {
        return m_hasShadowCasterPass;
    }

    // ========================================================================
    // Per-Graph Camera VP Cache (for multi-camera UBO updates)
    // ========================================================================

    /// @brief Cache camera VP matrices (called by SubmitCulling)
    void SetCachedCameraVP(const glm::mat4 &view, const glm::mat4 &proj)
    {
        m_cachedView = view;
        m_cachedProj = proj;
        m_hasCachedCameraVP = true;
    }

    /// @brief Check if this graph has cached camera VP matrices
    [[nodiscard]] bool HasCachedCameraVP() const
    {
        return m_hasCachedCameraVP;
    }

    /// @brief Clear cached draw calls and camera state.
    ///
    /// Scene switching destroys the source meshes/components immediately.
    /// Any cross-frame cache that still references those draw calls becomes
    /// invalid and must be dropped before the next render submission.
    /// Also forces a full render graph rebuild so that stale transient
    /// VkImage handles are not used in barrier insertion.
    void ClearCachedFrameState()
    {
        m_cachedRenderers.Clear();
        m_hasCachedDrawCalls = false;
        m_cachedShadowRenderers.Clear();
        m_hasCachedShadowDrawCalls = false;
        m_cachedView = glm::mat4(1.0f);
        m_cachedProj = glm::mat4(1.0f);
        m_hasCachedCameraVP = false;
        m_needsRebuild = true;
        // Prevent Execute() from running the old compiled graph before
        // EnsureGraphBuilt() has a chance to rebuild it.  Without this,
        // an early-return path in EnsureGraphBuilt (e.g. MSAA mismatch
        // guard) could leave m_graphBuilt = true while the graph still
        // references stale VkImage handles from the previous scene.
        m_graphBuilt = false;
        // Clear the stale Python graph descriptor so that
        // GetRequestedMsaaSamples() returns 0 until the new scene's
        // pipeline calls ApplyPythonGraph().  This avoids the MSAA
        // mismatch guard firing on an outdated descriptor.
        m_hasPythonGraph = false;
        m_hasShadowCasterPass = false;
        m_pythonGraphDesc = {};
    }

    /// @brief Get cached view matrix
    [[nodiscard]] const glm::mat4 &GetCachedView() const
    {
        return m_cachedView;
    }

    /// @brief Get cached projection matrix
    [[nodiscard]] const glm::mat4 &GetCachedProj() const
    {
        return m_cachedProj;
    }

    /// @brief Get per-graph shadow descriptor set (set 1) for the current frame-in-flight
    [[nodiscard]] VkDescriptorSet GetPerViewDescriptorSet() const;

    /**
     * @brief Explicit MSAA resolve from 4x MSAA color to 1x color target
     *
     * Called after all render graph passes finish, so every draw call
     * (scene objects, gizmos, grid) benefits from MSAA.
     */
    void ResolveSceneMsaa(VkCommandBuffer commandBuffer);

  private:
    /**
     * @brief Build the vk::RenderGraph from configured passes
     */
    void BuildRenderGraph();

    /**
     * @brief Pre-register all non-backbuffer transient textures so their
     * ResourceHandles are available before passes reference them.
     */
    void RegisterTransientTextures(uint32_t width, uint32_t height,
                                   std::unordered_map<std::string, vk::ResourceHandle> &customRTHandles);

    /**
     * @brief Append a system auto-pass (gizmos / editor tools) that draws
     * into the backbuffer with read-only depth testing.
     */
    void AppendAutoPass(const std::string &name, vk::ResourceHandle colorTarget, vk::ResourceHandle depthTarget,
                        uint32_t width, uint32_t height);

    /**
     * @brief Set the graph output handle for dead-pass culling.
     */
    void FinalizeGraphOutput(const std::unordered_map<std::string, vk::ResourceHandle> &customRTHandles);

    /**
     * @brief Import scene target resources into RenderGraph
     */
    void ImportSceneTargetResources();

    /// @brief Update this frame's per-view shadow descriptor before recording.
    void RefreshPerViewShadowDescriptor();

    InxVkCoreModular *m_vkCore = nullptr;
    SceneRenderTarget *m_sceneTarget = nullptr;
    InxScreenUIRenderer *m_screenUIRenderer = nullptr;

    // Build state
    bool m_needsRebuild = true;
    bool m_needsCompile = true;
    bool m_graphBuilt = false;
    bool m_hasPythonGraph = false;
    uint64_t m_graphBuildRevision = 0;
    uint64_t m_lastExecutedBuildRevision = 0;
    uint64_t m_executionCount = 0;

    // Python graph description (stored for BuildRenderGraph)
    RenderGraphDescription m_pythonGraphDesc;

    // Render callbacks keyed by pass name.
    // Populated by ApplyPythonGraph(). BuildRenderGraph() reads this map directly,
    // bypassing the intermediate ScenePassConfig conversion.
    std::unordered_map<std::string, ScenePassRenderCallback> m_pythonCallbacks;

    // The underlying render graph (now fully utilized)
    std::unique_ptr<vk::RenderGraph> m_renderGraph;

    // Imported resource handles from scene target
    vk::ResourceHandle m_importedColorTarget;
    vk::ResourceHandle m_importedResolveTarget; // 1x resolve target for MSAA
    vk::ResourceHandle m_importedDepthTarget;

    // Transient resources created by CreateTransientTexture()
    std::unordered_map<std::string, vk::ResourceHandle> m_transientResources;

    // Camera-driven clear overrides (set per-frame by UpdateMainPassClearSettings)
    bool m_hasCameraClearOverride = false;
    CameraClearFlags m_cameraClearFlags = {};
    glm::vec4 m_cameraBgColor{0.1f, 0.1f, 0.1f, 1.0f};

    // Previous frame's camera clear state — used to detect changes that
    // actually require a graph rebuild (= loadOp change) vs. changes that
    // only require updating clear *values* (no rebuild needed).
    bool m_prevClearStateValid = false;
    CameraClearFlags m_prevCameraClearFlags = {};
    glm::vec4 m_prevCameraBgColor{0.1f, 0.1f, 0.1f, 1.0f};

    // Name of the first graph pass that clears color (set during BuildRenderGraph).
    // Used to apply per-frame clear value updates without rebuilding the graph.
    std::string m_mainClearPassName;

    // Dimensions
    uint32_t m_width = 0;
    uint32_t m_height = 0;

    // Per-graph draw call cache for multi-camera rendering
    RendererList m_cachedRenderers;
    bool m_hasCachedDrawCalls = false;
    RendererList m_cachedShadowRenderers;
    bool m_hasCachedShadowDrawCalls = false;
    bool m_hasShadowCasterPass = false;

    // Per-graph camera VP cache — set by SubmitCulling so the executor
    // uses the exact same matrices that were active during SetupCameraProperties.
    glm::mat4 m_cachedView{1.0f};
    glm::mat4 m_cachedProj{1.0f};
    bool m_hasCachedCameraVP = false;

    // Per-graph shadow descriptor sets (set 1) — multi-camera shadow isolation.
    // One set per frame-in-flight to prevent host-side vkUpdateDescriptorSets
    // from stomping a set the GPU is still sampling in the previous frame.
    static constexpr uint32_t kMaxFramesInFlight = 2;
    VkDescriptorSet m_perViewDescSets[kMaxFramesInFlight] = {};

    // Resource handle bound to the graph's "shadowMap" sampler input.
    // Resolved after Compile() so the per-view descriptor can be updated
    // BEFORE command buffer recording starts.
    vk::ResourceHandle m_shadowMapInputHandle;
    bool m_shadowMapInputIsDepth = false;

    // Fullscreen effect renderer — manages pipeline cache, descriptor pool,
    // and linear sampler for FullscreenQuad graph passes.
    FullscreenRenderer m_fullscreenRenderer;
};

} // namespace infernux
