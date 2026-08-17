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
#include "MaterialPassPipeline.h"
#include "RenderGraphDescription.h"
#include "RendererList.h"
#include "SceneDepthResolver.h"
#include "lighting/CanonicalLightGpuBuffer.h"
#include "lighting/ForwardPlusLightGrid.h"
#include "particle/ParticleGpuViewDiagnostics.h"
#include "vk/RenderGraph.h"
#include "vk/VkDescriptorManager.h"
#include "vk/VkDeviceContext.h"
#include "vk/VkPipelineManager.h"
#include <algorithm>
#include <array>
#include <functional>
#include <map>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <vector>

namespace infernux
{

class InxVkCoreModular;
class InxMaterial;
class InxScreenUIRenderer;
class OutlineRenderer;
class SceneRenderTarget;
class Camera;
class Scene;
namespace particle
{
class ParticleGpuDrawRegistry;
class ParticleGpuCuller;
class ParticleGpuSorter;
} // namespace particle

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
    bool Initialize(InxVkCoreModular *vkCore, SceneRenderTarget *sceneTarget,
                    rhi::RenderViewKind viewKind = rhi::RenderViewKind::Scene);

    /**
     * @brief Cleanup resources
     */
    void Destroy();

    [[nodiscard]] uint64_t GetTransientResidentBytes() const;

    /**
     * @brief Retire framebuffer objects that reference the current scene target.
     *
     * Call this with
     * the last submission epoch that can reference the current
     * target before ReplaceSceneTarget(). The graph
     * becomes non-executable
     * immediately while Vulkan framebuffer destruction remains deferred.
     */
    void RetireFramebuffersBeforeTargetReplacement(rhi::SubmissionSerial retirementSerial);

    [[nodiscard]] const rhi::RenderViewContext &GetRenderViewContext() const noexcept
    {
        return m_renderView;
    }

    [[nodiscard]] size_t GetTemporalHistoryCount() const noexcept
    {
        return m_temporalHistories.size();
    }

    [[nodiscard]] size_t GetValidTemporalHistoryCount() const noexcept
    {
        return static_cast<size_t>(std::count_if(m_temporalHistories.begin(), m_temporalHistories.end(),
                                                 [](const auto &entry) { return entry.second.valid; }));
    }

    [[nodiscard]] uint32_t GetTemporalSampleIndex() const noexcept
    {
        return m_temporalSampleIndex;
    }

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

    /// Validate a backend-neutral graph description before applying it.
    [[nodiscard]] static bool ValidateGraphDescription(const RenderGraphDescription &desc, uint32_t activeFrameSamples);

    /// Upload changed runtime parameter blocks without rebuilding graph topology.
    void UpdateParameterBlocks(const std::vector<GraphParameterBlockUpdate> &updates);

    /// True when this graph already owns the requested Python artifact and its
    /// callback sample contract still matches the active render targets.
    [[nodiscard]] bool IsPythonGraphCurrent(uint64_t sourceRevision) const;

    /**
     * @brief Set the screen UI renderer for DrawScreenUI passes
     * @param renderer Pointer to the screen UI renderer (may be nullptr)
     */
    void SetScreenUIRenderer(InxScreenUIRenderer *renderer)
    {
        m_screenUIRenderer = renderer;
    }

    /// Attach the Scene-only editor outline renderer. The graph topology only
    /// changes when outline rendering transitions between inactive and active.
    void SetOutlineRenderer(OutlineRenderer *renderer);

    void SetParticleGpuDrawRegistry(particle::ParticleGpuDrawRegistry *registry)
    {
        if (m_particleDrawRegistry == registry)
            return;
        m_particleDrawRegistry = registry;
        m_particleDrawRegistryRevision = 0;
        m_needsRebuild = true;
    }

    [[nodiscard]] uint64_t RequestParticleViewDiagnostics(uint64_t graphInstanceId);
    [[nodiscard]] particle::GpuParticleViewDiagnosticSnapshot QueryParticleViewDiagnostics(uint64_t requestId) const;

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

    /// Return the square shadow atlas extent declared by this graph, or zero
    /// when the graph has no shadow-caster pass.
    [[nodiscard]] uint32_t GetShadowMapResolution() const;

    /// Set the globally validated sample count used by this view. Scene and
    /// game graphs currently share material pipelines and therefore must use
    /// one compatible sample count.
    void SetEffectiveMsaaSamples(int samples)
    {
        m_effectiveMsaaSamples = samples;
    }

    /// Replace the external target without reinitializing graph-owned pools.
    /// The next EnsureGraphBuilt call imports the replacement resources.
    void ReplaceSceneTarget(SceneRenderTarget *sceneTarget);

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

    /// Prepare mutable per-frame state before the backend records the
    /// compiler-produced submission batches on their native queues.
    [[nodiscard]] bool PrepareSubmissionExecution();

    /// Complete diagnostics and publish the offscreen output after every
    /// compiler-produced batch has been recorded.
    [[nodiscard]] bool CompleteSubmissionExecution(VkCommandBuffer commandBuffer);

    [[nodiscard]] vk::RenderGraph *GetCompiledRenderGraph() noexcept
    {
        return m_graphBuilt ? m_renderGraph.get() : nullptr;
    }

    [[nodiscard]] const vk::RenderGraph *GetCompiledRenderGraph() const noexcept
    {
        return m_graphBuilt ? m_renderGraph.get() : nullptr;
    }

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
    /**
     * @brief Force rebuild of the render graph on next frame
     */
    void MarkDirty()
    {
        m_needsRebuild = true;
    }

    /// Drop cached fullscreen pipelines compiled from a replaced shader
    /// module. Graph commands keep their stable shader identifier and resolve
    /// the newly published pipeline on the next record.
    void InvalidateFullscreenShader(const std::string &shaderName)
    {
        m_fullscreenRenderer.InvalidateShader(shaderName);
    }

    void UpdateMainPassClearSettings(CameraClearFlags clearFlags, const glm::vec4 &bgColor, bool dithering,
                                     bool stopNaNs);

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

    [[nodiscard]] bool CanReuseCachedSubmission(uint64_t signature, uint64_t objectBufferRevision) const noexcept
    {
        return m_hasCachedDrawCalls && signature != 0 && objectBufferRevision != 0 &&
               m_cachedSubmissionSignature == signature && m_cachedObjectBufferRevision == objectBufferRevision;
    }

    void SetCachedSubmissionSignature(uint64_t signature, std::shared_ptr<const void> owner = {},
                                      uint64_t objectBufferRevision = 0)
    {
        m_cachedSubmissionSignature = signature;
        m_cachedRenderWorldOwner = std::move(owner);
        m_cachedObjectBufferRevision = objectBufferRevision;
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
        m_cachedSubmissionSignature = 0;
        m_cachedObjectBufferRevision = 0;
        m_cachedRenderWorldOwner.reset();
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
    void SetCachedCameraVP(const Camera *camera, const glm::mat4 &view, const glm::mat4 &proj);

    /// Return the centered sub-pixel projection offset for one temporal sample.
    /// The result is expressed in NDC and is independent for each RenderView.
    [[nodiscard]] static glm::vec2 ComputeTemporalJitterNdc(uint32_t sampleIndex, uint32_t width, uint32_t height);

    /// Apply an NDC offset to a projection without assuming perspective or
    /// orthographic matrix layout. Culling continues to use the source matrix.
    [[nodiscard]] static glm::mat4 ApplyTemporalJitter(const glm::mat4 &projection, const glm::vec2 &jitterNdc);

    /// Upload this view's camera constants into its current frame-local buffer.
    /// This is host-side preparation and must happen before graph recording.
    bool StageCameraMatrices(const glm::mat4 &view, const glm::mat4 &proj, const glm::mat4 *previousViewProj = nullptr);

    /// @brief Check if this graph has cached camera VP matrices
    [[nodiscard]] bool HasCachedCameraVP() const
    {
        return m_hasCachedCameraVP;
    }

    /// @brief Drop only camera-dependent submissions for an inactive view.
    ///
    /// Hiding an editor panel must not invalidate the compiled pipeline or its
    /// resource description.  The Game view still relies on that shared
    /// pipeline description when it builds the per-camera shadow layout.
    void ClearCachedViewSubmission()
    {
        m_cachedRenderers.Clear();
        m_hasCachedDrawCalls = false;
        m_cachedShadowRenderers.Clear();
        m_hasCachedShadowDrawCalls = false;
        m_cachedSubmissionSignature = 0;
        m_cachedObjectBufferRevision = 0;
        m_cachedRenderWorldOwner.reset();
        m_cachedView = glm::mat4(1.0f);
        m_cachedProj = glm::mat4(1.0f);
        m_cachedUnjitteredProj = glm::mat4(1.0f);
        m_hasCachedCameraVP = false;
        m_cachedCamera = nullptr;
        m_previousViewProj = glm::mat4(1.0f);
        m_cameraHistoryValid = false;
        InvalidateTemporalHistory();
    }

    /// Retire per-view particle cullers/sorters and forbid executing a compiled
    /// graph that still imports those retired buffers. Vulkan may recycle the
    /// same raw handle values, so handle equality cannot keep these views.
    void InvalidateParticleViews();

    /// @brief Clear cached draw calls and camera state.
    ///
    /// Scene switching destroys the source meshes/components immediately.
    /// Any cross-frame cache that still references those draw calls becomes
    /// invalid and must be dropped before the next render submission.
    /// Also forces a full render graph rebuild so that stale transient
    /// VkImage handles are not used in barrier insertion.
    void ClearCachedFrameState()
    {
        ClearCachedViewSubmission();
        InvalidateParticleViews();
        InvalidatePerViewShadowBindings();
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
        m_parameterBlocks.clear();
        m_pythonMaterialPasses.clear();
        m_pythonGraphDesc = {};
    }

    /// @brief Get cached view matrix
    [[nodiscard]] const glm::mat4 &GetCachedView() const
    {
        return m_cachedView;
    }

    void SetDrawViewMatrix(const glm::mat4 &view)
    {
        m_drawView = view;
    }

    /// @brief Get cached projection matrix
    [[nodiscard]] const glm::mat4 &GetCachedProj() const
    {
        return m_cachedProj;
    }

    [[nodiscard]] glm::mat4 GetPreviousViewProj() const
    {
        return m_cameraHistoryValid ? m_previousViewProj : m_cachedProj * m_cachedView;
    }

    void CommitCameraHistory()
    {
        if (!m_hasCachedCameraVP)
            return;
        m_previousViewProj = m_cachedProj * m_cachedView;
        m_cameraHistoryValid = true;
        if (UsesTemporalHistory())
            m_temporalSampleIndex = (m_temporalSampleIndex + 1u) % kTemporalJitterSampleCount;
    }

    /// Drop accumulated temporal color history for this view.
    void InvalidateTemporalHistory();

    /// @brief Get per-graph shadow descriptor set (set 1) for the current frame-in-flight
    [[nodiscard]] VkDescriptorSet GetPerViewDescriptorSet() const;
    [[nodiscard]] rhi::BindGroupHandle GetPerViewBindGroup() const;

    /// Build immutable lighting and shadow state owned by this camera graph.
    void StageCameraLighting(Scene *scene, Camera *camera, const glm::vec3 &cameraPosition,
                             const ShaderLightingUBO &environmentLighting);
    [[nodiscard]] bool HasCameraShadows() const noexcept
    {
        return !m_cameraLightCollector.GetShadowFrame().views.empty();
    }
    [[nodiscard]] uint32_t GetCameraLightCount() const noexcept
    {
        return m_cameraLightCollector.GetTotalLightCount();
    }
    [[nodiscard]] uint32_t GetCameraShadowViewCount() const noexcept
    {
        return static_cast<uint32_t>(m_cameraLightCollector.GetShadowFrame().views.size());
    }
    [[nodiscard]] uint32_t GetCameraShadowAssignmentCount() const noexcept
    {
        return static_cast<uint32_t>(m_cameraLightCollector.GetShadowFrame().assignments.size());
    }
    [[nodiscard]] uint64_t GetShadowResourceIdentity() const noexcept
    {
        return m_shadowCameraResourceId;
    }

  private:
    struct PendingParticleViewDiagnostic
    {
        uint64_t requestId = 0;
        uint64_t graphInstanceId = 0;
    };

    struct ParticleViewDiagnosticState
    {
        mutable std::mutex mutex;
        std::unordered_map<uint64_t, particle::GpuParticleViewDiagnosticSnapshot> snapshots;
    };

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

    [[nodiscard]] MaterialPassPipelineDescriptor GetEditorOverlayMaterialPass() const;

    /**
     * @brief Append a system auto-pass (gizmos / editor tools) that draws
     * into the backbuffer with read-only depth testing.
     */
    vk::ResourceHandle AppendAutoPass(const std::string &name, vk::ResourceHandle colorTarget,
                                      vk::ResourceHandle depthTarget, uint32_t width, uint32_t height);

    /**
     * @brief Set the graph output handle for dead-pass culling.
     */
    void FinalizeGraphOutput(const std::unordered_map<std::string, vk::ResourceHandle> &customRTHandles);
    vk::ResourceHandle AppendEditorOutline(vk::ResourceHandle displayTarget);

    /**
     * @brief Import scene target resources into RenderGraph
     */
    void ImportSceneTargetResources();
    void ImportTemporalHistoryResources(std::unordered_map<std::string, vk::ResourceHandle> &handles);
    void BindTemporalHistoryResources();
    void CommitTemporalHistory();
    void RetireTemporalHistoryResources();
    [[nodiscard]] bool UsesTemporalHistory() const;

    /// @brief Update this frame's per-view shadow descriptor before recording.
    void RefreshPerViewShadowDescriptor();
    void InvalidatePerViewShadowBindings() noexcept;
    [[nodiscard]] bool UsesForwardPlus() const;
    void RefreshForwardPlusParticleRequirement();
    [[nodiscard]] bool PrepareForwardPlusFrame();
    void RetireForwardPlusResources();
    void RecordParticleViewDiagnostics(VkCommandBuffer commandBuffer);

    InxVkCoreModular *m_vkCore = nullptr;
    SceneRenderTarget *m_sceneTarget = nullptr;
    rhi::RenderViewContext m_renderView;
    InxScreenUIRenderer *m_screenUIRenderer = nullptr;
    OutlineRenderer *m_outlineRenderer = nullptr;
    bool m_outlinePassesEnabled = false;
    bool m_outlinePipelineFailureReported = false;

    // Build state
    bool m_needsRebuild = true;
    bool m_needsCompile = true;
    bool m_graphBuilt = false;
    int m_effectiveMsaaSamples = 0;
    bool m_hasPythonGraph = false;
    uint64_t m_pythonGraphSourceRevision = 0;
    VkSampleCountFlagBits m_pythonCallbackSamples = VK_SAMPLE_COUNT_FLAG_BITS_MAX_ENUM;
    uint64_t m_graphBuildRevision = 0;
    uint64_t m_lastExecutedBuildRevision = 0;
    uint64_t m_executionCount = 0;

    // Python graph description (stored for BuildRenderGraph)
    RenderGraphDescription m_pythonGraphDesc;

    struct RuntimeParameterBlock
    {
        uint64_t revision = 0;
        std::vector<std::string> names;
        FullscreenPushConstants values{};
        uint32_t byteSize = 0;
    };

    // Dynamic values keyed by GraphCommandDesc::parameterBlock. Layout is
    // compiled with the graph; revisions update independently at runtime.
    std::unordered_map<std::string, RuntimeParameterBlock> m_parameterBlocks;

    // Render callbacks keyed by pass name.
    // Populated by ApplyPythonGraph(). BuildRenderGraph() reads this map directly,
    // bypassing the intermediate ScenePassConfig conversion.
    std::unordered_map<std::string, ScenePassRenderCallback> m_pythonCallbacks;
    std::unordered_map<std::string, MaterialPassPipelineDescriptor> m_pythonMaterialPasses;

    particle::ParticleGpuDrawRegistry *m_particleDrawRegistry = nullptr;
    uint64_t m_particleDrawRegistryRevision = 0;
    std::unordered_map<uint64_t, std::shared_ptr<particle::ParticleGpuCuller>> m_particleCullers;
    std::unordered_map<uint64_t, std::shared_ptr<particle::ParticleGpuSorter>> m_particleSorters;
    std::shared_ptr<ParticleViewDiagnosticState> m_particleViewDiagnosticState;
    std::vector<PendingParticleViewDiagnostic> m_pendingParticleViewDiagnostics;
    uint64_t m_nextParticleViewDiagnosticRequestId = 1;

    // The underlying render graph (now fully utilized)
    std::unique_ptr<vk::RenderGraph> m_renderGraph;

    // Imported resource handles from scene target
    vk::ResourceHandle m_importedColorTarget;
    vk::ResourceHandle m_importedResolveTarget; // 1x resolve target for MSAA
    vk::ResourceHandle m_importedDepthTarget;
    vk::ResourceHandle m_visibleRendererList;
    vk::ResourceHandle m_shadowRendererList;

    // Transient resources created by CreateTransientTexture()
    std::unordered_map<std::string, vk::ResourceHandle> m_transientResources;

    struct TemporalHistoryResource
    {
        std::array<rhi::TextureHandle, 2> textures{};
        std::array<rhi::TextureViewHandle, 2> views{};
        vk::ResourceHandle readHandle;
        vk::ResourceHandle writeHandle;
        std::string readName;
        std::string writeName;
        rhi::PixelFormat format = rhi::PixelFormat::Undefined;
        uint32_t width = 0;
        uint32_t height = 0;
        uint32_t readIndex = 0;
        bool valid = false;
    };
    std::unordered_map<std::string, TemporalHistoryResource> m_temporalHistories;

    // Camera-driven clear overrides (set per-frame by UpdateMainPassClearSettings)
    bool m_hasCameraClearOverride = false;
    CameraClearFlags m_cameraClearFlags = {};
    glm::vec4 m_cameraBgColor{0.1f, 0.1f, 0.1f, 1.0f};
    bool m_cameraDithering = false;
    bool m_cameraStopNaNs = false;

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
    uint64_t m_cachedSubmissionSignature = 0;
    uint64_t m_cachedObjectBufferRevision = 0;
    std::shared_ptr<const void> m_cachedRenderWorldOwner;
    bool m_hasShadowCasterPass = false;

    // Per-graph camera VP cache — set by SubmitCulling so the executor
    // uses the exact same matrices that were active during SetupCameraProperties.
    glm::mat4 m_cachedView{1.0f};
    glm::mat4 m_cachedProj{1.0f};
    glm::mat4 m_cachedUnjitteredProj{1.0f};
    std::array<float, 24> m_particleFrustumPlanes{};
    bool m_hasCachedCameraVP = false;
    glm::mat4 m_drawView{1.0f};
    glm::mat4 m_previousViewProj{1.0f};
    bool m_cameraHistoryValid = false;
    const Camera *m_cachedCamera = nullptr;
    static constexpr uint32_t kTemporalJitterSampleCount = 8;
    uint32_t m_temporalSampleIndex = 0;
    glm::vec2 m_temporalJitterNdc{0.0f};

    // Per-graph shadow descriptor sets (set 1) — multi-camera shadow isolation.
    // One set per frame-in-flight to prevent host-side vkUpdateDescriptorSets
    // from stomping a set the GPU is still sampling in the previous frame.
    static constexpr uint32_t kMaxFramesInFlight = 2;
    struct PerViewBufferBindingState
    {
        VkBuffer canonicalLights = VK_NULL_HANDLE;
        VkBuffer tileHeaders = VK_NULL_HANDLE;
        VkBuffer tileLightMasks = VK_NULL_HANDLE;
        VkBuffer lighting = VK_NULL_HANDLE;
        uint64_t canonicalBytes = 0;
        uint64_t tileHeaderBytes = 0;
        uint64_t tileLightMaskBytes = 0;
        bool initialized = false;
    };
    struct PerViewShadowBindingState
    {
        VkImageView imageView = VK_NULL_HANDLE;
        VkSampler sampler = VK_NULL_HANDLE;
        VkImageLayout layout = VK_IMAGE_LAYOUT_UNDEFINED;
        bool fallback = true;
    };

    struct PerViewFrameState
    {
        vk::DescriptorLease geometryDescriptor;
        vk::DescriptorLease particleDescriptor;
        rhi::BindGroupHandle geometryGroup;
        rhi::BindGroupHandle particleGroup;
        rhi::BufferHandle cameraMatrix;
        rhi::BufferHandle lighting;
        PerViewBufferBindingState geometryBindings;
        PerViewBufferBindingState particleBindings;
        PerViewShadowBindingState shadowBinding;

        [[nodiscard]] VkDescriptorSet GeometrySet() const noexcept
        {
            return geometryDescriptor.set;
        }

        [[nodiscard]] VkDescriptorSet ParticleSet() const noexcept
        {
            return particleDescriptor.set;
        }
    };

    std::array<PerViewFrameState, kMaxFramesInFlight> m_perViewFrames{};
    rhi::BindingLayoutHandle m_perViewLayout;
    lighting::CanonicalLightGpuBuffer m_cameraCanonicalLights;
    SceneLightCollector m_cameraLightCollector;
    uint64_t m_shadowCameraResourceId = 0;

    // Resource handle bound to the graph's "shadowMap" sampler input.
    // Resolved after Compile() so the per-view descriptor can be updated
    // BEFORE command buffer recording starts.
    vk::ResourceHandle m_shadowMapInputHandle;
    bool m_shadowMapInputIsDepth = false;

    // Fullscreen effect renderer — manages pipeline cache, descriptor pool,
    // and linear sampler for FullscreenQuad graph passes.
    FullscreenRenderer m_fullscreenRenderer;
    SceneDepthResolver m_sceneDepthResolver;
    lighting::ForwardPlusLightGrid m_forwardPlusGeometryGrid;
    lighting::ForwardPlusLightGrid m_forwardPlusParticleGrid;
    bool m_forwardPlusParticlesRequired = false;
};

} // namespace infernux
