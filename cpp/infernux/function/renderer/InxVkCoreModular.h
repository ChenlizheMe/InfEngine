/**
 * @file InxVkCoreModular.h
 * @brief Modern modular Vulkan core using the new RAII-based architecture
 *
 * This file provides a drop-in replacement for InxVkCore that uses the new
 * modular architecture. It maintains API compatibility with the original
 * InxVkCore while internally using:
 *
 * - VkDeviceContext for device management
 * - VkSwapchainManager for swapchain lifecycle
 * - VkPipelineManager for pipeline/shader management
 * - VkResourceManager for resource creation
 * - RenderGraph for declarative rendering (optional)
 *
 * Migration Guide:
 * 1. Include this header instead of InxVkCore.h
 * 2. Change InxVkCore to InxVkCoreModular
 * 3. Optionally use RenderGraph for declarative rendering
 *
 * Example:
 *   // Old code:
 *   InxVkCore core;
 *   core.Init(...);
 *   core.DrawFrame(...);
 *
 *   // New code:
 *   InxVkCoreModular core;
 *   core.Init(...);
 *   core.DrawFrame(...);  // Same API!
 *
 *   // Or use RenderGraph:
 *   auto& graph = core.GetRenderGraph();
 *   graph.AddPass("MyPass", [](PassBuilder& builder) { ... });
 */

#pragma once

#include "EngineGlobals.h"
#include "GpuResidency.h"
#include "InxRenderStruct.h"
#include "MaterialPipelineManager.h"
#include "ProfileConfig.h"
#include "RenderGraphDescription.h"
#include "RenderInstanceHistory.h"
#include "VkShaderCache.h"
#include "VkTextureCache.h"
#include "rhi/GpuRetirementQueue.h"
#include "vk/VkCore.h"
#include "vk/VulkanFrameSubmission.h"
#include "vk/VulkanSubmissionExecutor.h"
#if INFERNUX_FRAME_PROFILE
#include "vk/GpuTimestampQueries.h"
#endif
#include <core/types/InxApplication.h>
#include <function/renderer/lighting/CanonicalLightGpuBuffer.h>
#include <function/scene/LightingData.h>

#include <functional>
#include <cstdint>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

struct SDL_Window;

namespace infernux
{

struct FrameSubmissionTelemetry
{
    uint64_t generation = 0;
    bool composed = false;
    bool computeQueueIndependent = false;
    bool transferQueueIndependent = false;
    bool asyncComputeActive = false;
    bool parallelComputeGraphics = false;
    uint32_t batchCount = 0;
    uint32_t graphicsBatchCount = 0;
    uint32_t computeBatchCount = 0;
    uint32_t transferBatchCount = 0;
    uint32_t crossQueueDependencyCount = 0;
    uint32_t unorderedComputeGraphicsPairCount = 0;
};

class AssetLoadTicket;
class EditorGizmos;
class GPUMaterialPreview;
class GPUMeshPreview;
class InxMaterial;
class InxMesh;
class SceneRenderTarget;
enum class TextureDimension : uint32_t;
struct RenderState;

/**
 * @brief Modern modular Vulkan core with RAII resource management
 *
 * This class provides the same interface as InxVkCore but uses
 * the new modular Vulkan architecture internally.
 */
class InxVkCoreModular
{
  public:
    friend class InxGUI;
    friend class InxRenderer;

    /**
     * @brief Construct with specified max frames in flight
     * @param maxFrameInFlight Maximum concurrent frames (default: 2)
     */
    explicit InxVkCoreModular(int maxFrameInFlight = 2);
    ~InxVkCoreModular();

    // Non-copyable, non-movable (like original InxVkCore)
    InxVkCoreModular(const InxVkCoreModular &) = delete;
    InxVkCoreModular &operator=(const InxVkCoreModular &) = delete;
    InxVkCoreModular(InxVkCoreModular &&) = delete;
    InxVkCoreModular &operator=(InxVkCoreModular &&) = delete;

    /// @brief When true, subsystem destructors should skip their individual
    ///        vkDeviceWaitIdle calls — the caller already did a single drain.
    void SetShuttingDown(bool v)
    {
        if (v && !m_shuttingDown) {
            ReleaseMaterialPassResolutionCache();
        }
        m_shuttingDown = v;
        m_backend.SetShuttingDown(v);
    }
    bool IsShuttingDown() const
    {
        return m_shuttingDown;
    }

    // ========================================================================
    // Initialization (API compatible with InxVkCore)
    // ========================================================================

    /**
     * @brief Initialize Vulkan core
     *
     * @param appMetaData Application metadata
     * @param rendererMetaData Renderer metadata
     * @param vkWindowExtCount Number of window extensions
     * @param vkWindowExts Window extension names
     */
    [[nodiscard]] bool Init(InxAppMetadata appMetaData, InxAppMetadata rendererMetaData, uint32_t vkWindowExtCount,
                            const char **vkWindowExts);

    /**
     * @brief Prepare surface for rendering
     */
    [[nodiscard]] bool PrepareSurface();

    /**
     * @brief Prepare graphics pipeline
     */
    void PreparePipeline();

    /// @brief Set window size for swapchain extent fallback
    void SetWindowSize(uint32_t width, uint32_t height)
    {
        m_windowWidth = width;
        m_windowHeight = height;
    }

    /// @brief Change the swapchain present mode and recreate the swapchain.
    /// 0 = IMMEDIATE, 1 = MAILBOX, 2 = FIFO, 3 = FIFO_RELAXED
    void SetPresentMode(int mode);

    /// @brief Get current present mode preference (0=IMMEDIATE,1=MAILBOX,2=FIFO,3=FIFO_RELAXED)
    [[nodiscard]] int GetPresentMode() const
    {
        switch (m_backend.Presentation().GetPreferredPresentMode()) {
        case VK_PRESENT_MODE_IMMEDIATE_KHR:
            return 0;
        case VK_PRESENT_MODE_MAILBOX_KHR:
            return 1;
        case VK_PRESENT_MODE_FIFO_KHR:
            return 2;
        case VK_PRESENT_MODE_FIFO_RELAXED_KHR:
            return 3;
        default:
            return 1;
        }
    }

    // ========================================================================
    // Texture Management
    // ========================================================================

    void CreateDefaultWhiteTexture(std::string name);

    // ========================================================================
    // Shader and Pipeline Management
    // ========================================================================

    void LoadShader(const char *name, const std::vector<char> &spirvCode, const char *type);
    bool PublishShaderProgramArtifact(const ShaderProgramArtifact &artifact);
    [[nodiscard]] bool HasShaderProgramArtifact(const ShaderProgramKey &programKey) const;
    [[nodiscard]] std::shared_ptr<const ShaderProgramArtifact>
    CopyShaderProgramArtifact(const ShaderStagePair &stages) const;
    void SetShaderProgramArtifactResolver(std::function<void(const std::shared_ptr<InxMaterial> &)> resolver)
    {
        m_shaderProgramArtifactResolver = std::move(resolver);
    }
    void UnloadShader(const char *name);
    bool HasShader(const std::string &name, const std::string &type) const;

    /// @brief Store shader render-state annotations for a shader_id.
    /// Called after parsing shader @annotations.
    void StoreShaderRenderMeta(const std::string &shaderId, const std::string &cullMode, const std::string &depthWrite,
                               const std::string &depthTest, const std::string &blend, int queue,
                               const std::string &passTag = "", const std::string &stencil = "",
                               const std::string &alphaClip = "");

    /**
     * @brief Invalidate shader program cache for hot-reload
     *
     * Must be called before loading updated shader code to force pipeline recreation.
     * @param shaderId The shader identifier to invalidate
     */
    void InvalidateShaderCache(const std::string &shaderId);

    /**
     * @brief Invalidate cached GPU textures matching a GUID or file path
     *
     * Evicts all cached variants (both SRGB and UNORM) for the given identifier
     * and invalidates any materials that reference it, forcing re-resolve on next draw.
     * @param textureIdentifier A texture GUID (preferred) or file path
     */
    void InvalidateTextureCache(const std::string &textureIdentifier);

    /**
     * @brief Remove pipeline render data for a specific material
     *
     * Frees the MaterialPipelineManager entry and its shared_ptr to the material.
     * @param materialName The material key (GetMaterialKey())
     */
    void RemoveMaterialPipeline(const std::string &materialName);

    // ========================================================================
    // Rendering
    // ========================================================================

    /**
     * @brief Draw a frame
     *
     * @param viewPos Camera position
     * @param viewLookAt Camera look-at point
     * @param viewUp Camera up vector
     */
    void DrawFrame(const float *viewPos, const float *viewLookAt, const float *viewUp);

    /**
     * @brief Draw scene objects filtered by render queue range
     *
     * Renders draw calls whose material render queue falls within
     * [queueMin, queueMax]. Used by RenderGraph passes defined from Python
     * to split rendering into multiple passes.
     *
     * @param cmdBuf Vulkan command buffer
     * @param width  Render target width
     * @param height Render target height
     * @param queueMin Minimum render queue (inclusive)
     * @param queueMax Maximum render queue (inclusive)
     * @param sortMode "front_to_back", "back_to_front", or empty/none
     * @param overrideMaterial If non-empty, all objects use this material name
     */
    void DrawSceneFiltered(VkCommandBuffer cmdBuf, uint32_t width, uint32_t height, rhi::BindGroupHandle perViewGroup,
                           const glm::mat4 &viewMatrix, int queueMin, int queueMax, const std::string &sortMode = "",
                           const std::string &overrideMaterial = "", const std::string &passTag = "",
                           const MaterialPassPipelineDescriptor *pipelineDescriptor = nullptr,
                           GraphMaterialFilter materialFilter = GraphMaterialFilter::All);

    /**
     * @brief Draw shadow casters into a depth-only shadow map.
     *
     * Uses per-material shadow pipelines (auto-generated shadow variants) with
     * the material's culling mode. Receiver bias is applied by the lighting
     * shader in resolution-independent
     * shadow texels. The light VP is obtained from
     * SceneLightCollector. Shadow infrastructure is created lazily
     * on first use.
     *
     * @param cmdBuf Vulkan command buffer (inside a render pass)
     * @param width  Shadow map width
     * @param height Shadow map height
     * @param queueMin Minimum render queue (inclusive)
     * @param queueMax Maximum render queue (inclusive)
     * @param lightIndex Index of the shadow-casting light (only 0 supported currently)
     * Hard/soft selection lives on the Light component (shadowParams.w in the
     * lighting UBO), not on this pass — the shadow map itself is filter-agnostic.
     */
    using ShadowViewDrawCallback = std::function<void(uint32_t viewIndex, const lighting::ShadowView &view)>;
    using ShadowCameraResourceId = uint64_t;

    [[nodiscard]] ShadowCameraResourceId CreateShadowCameraResources();
    void DestroyShadowCameraResources(ShadowCameraResourceId resourceId) noexcept;

    void DrawShadowCasters(VkCommandBuffer cmdBuf, uint32_t width, uint32_t height, int queueMin, int queueMax,
                           ShadowCameraResourceId resourceId, const lighting::ShadowFrame &shadowFrame,
                           int lightIndex = 0, const ShadowViewDrawCallback &additionalDraws = {});

    /// @brief Set draw calls for multi-material rendering (stores pointer, no copy)
    void SetDrawCalls(const std::vector<DrawCall> *drawCalls);

    [[nodiscard]] bool UsesDrawCalls(const std::vector<DrawCall> *drawCalls) const noexcept
    {
        return m_drawCallsPtr == drawCalls;
    }

    /// @brief Set shadow-caster draw calls (stores pointer, no copy)
    void SetShadowDrawCalls(const std::vector<DrawCall> *drawCalls);

    /// @brief Stage per-frame engine globals (called by InxRenderer each frame).
    void StageGlobals(const EngineGlobalsUBO &globals);

    /// @brief Get the engine-globals descriptor set layout (set 2) for pipeline creation.
    [[nodiscard]] VkDescriptorSetLayout GetGlobalsDescSetLayout() const
    {
        return m_globalsDescSetLayout;
    }

    /// @brief Get the engine-globals descriptor set for the current frame.
    [[nodiscard]] VkDescriptorSet GetCurrentGlobalsDescSet() const
    {
        if (m_globalsDescSets.empty())
            return VK_NULL_HANDLE;
        uint32_t idx = GetCurrentFrameSlot() % static_cast<uint32_t>(m_globalsDescSets.size());
        return m_globalsDescSets[idx];
    }

    /// @brief Get the globals UBO VkBuffer for a specific frame index.
    [[nodiscard]] VkBuffer GetGlobalsBuffer(uint32_t frameIndex) const
    {
        if (frameIndex >= m_globalsBuffers.size() || !m_globalsBuffers[frameIndex])
            return VK_NULL_HANDLE;
        return m_globalsBuffers[frameIndex]->GetBuffer();
    }

    /// @brief Get max frames in flight.
    [[nodiscard]] uint32_t GetMaxFramesInFlight() const
    {
        return m_maxFramesInFlight;
    }

    /// Renderer-owned frame slot. Presentation consumes this value but does
    /// not own or advance it, so offscreen views do not depend on swapchain state.
    [[nodiscard]] uint32_t GetCurrentFrameSlot() const noexcept
    {
        return m_maxFramesInFlight == 0 ? 0 : m_currentFrame % m_maxFramesInFlight;
    }

    /// @brief Update material UBO with current material properties (stub)
    void UpdateMaterialUBO(InxMaterial &material);

    /// @brief Ensure a material has its own UBO buffer allocated (stub)
    void EnsureMaterialUBO(std::shared_ptr<InxMaterial> material);

    /// @brief Ensure per-object GPU buffers exist and match the given mesh data.
    /// Creates new buffers or recreates if vertex/index count changed.
    void EnsureObjectBuffers(uint64_t objectId, const std::vector<Vertex> &vertices,
                             const std::vector<uint32_t> &indices, bool forceUpdate, const std::string &assetGuid,
                             uint64_t runtimeVersion);

    /// @brief Advance the frame counter for EnsureObjectBuffers dedup.
    /// Call once per frame before any Render calls.
    void AdvanceEnsureFrame()
    {
        PumpPendingTextureLoads();
        PumpPendingMeshUploads();
        ++m_ensureFrameCounter;
        m_textureCache.AdvanceFrame(m_ensureFrameCounter);
        m_fullObjectBufferEnsureThisFrame = false;
        m_skipObjectBufferCleanupThisFrame = false;
    }

    /// @brief Bulk-stamp all per-object buffer entries with the current frame
    /// counter without hash-map lookups.  Use when no `forceBufferUpdate` is
    /// set and no new objects appeared, skipping the full EnsureObjectBuffers
    /// loop (~0.3 ms saved at 10 k objects).
    void BulkStampEnsuredFrame()
    {
        for (auto &[id, entry] : m_perObjectBuffers) {
            entry.ensuredOnFrame = m_ensureFrameCounter;
        }
    }

    /// @brief Return the count of per-object buffer entries.
    size_t GetPerObjectBufferCount() const
    {
        return m_perObjectBuffers.size();
    }
    [[nodiscard]] size_t GetPendingMeshUploadCount() const noexcept
    {
        return m_pendingSharedMeshBuffers.size();
    }
    [[nodiscard]] bool CanReuseObjectBufferBindings(uint64_t identity, size_t drawCallCount) const noexcept
    {
        if (!m_pendingSharedMeshBuffers.empty())
            return false;
        for (const auto &entry : m_objectBufferBindingCache) {
            if (entry.valid && entry.identity == identity && entry.drawCallCount == drawCallCount)
                return true;
        }
        return false;
    }
    void PrimeObjectBufferBindingCache(uint64_t identity, size_t drawCallCount) noexcept
    {
        m_fullObjectBufferEnsureThisFrame = true;
        m_skipObjectBufferCleanupThisFrame = false;
        if (!m_pendingSharedMeshBuffers.empty())
            return;
        for (auto &entry : m_objectBufferBindingCache) {
            if (entry.valid && entry.identity == identity && entry.drawCallCount == drawCallCount)
                return;
        }
        auto &entry =
            m_objectBufferBindingCache[m_objectBufferBindingCacheCursor++ % m_objectBufferBindingCache.size()];
        entry = {identity, drawCallCount, true};
    }
    void ReuseObjectBufferBindingsThisFrame() noexcept
    {
        if (!m_fullObjectBufferEnsureThisFrame)
            m_skipObjectBufferCleanupThisFrame = true;
    }
    [[nodiscard]] uint64_t GetSubmittedMeshUploadCount() const noexcept
    {
        return m_submittedMeshUploadCount;
    }
    [[nodiscard]] uint64_t GetCompletedMeshUploadCount() const noexcept
    {
        return m_completedMeshUploadCount;
    }
    [[nodiscard]] uint64_t GetAsyncMeshUploadCount() const noexcept
    {
        return m_asyncMeshUploadCount;
    }
    [[nodiscard]] size_t GetPendingTextureCpuLoadCount() const noexcept
    {
        return m_pendingTextureCpuLoads.size();
    }
    [[nodiscard]] size_t GetPendingTextureUploadCount() const noexcept
    {
        return m_pendingTextureGpuUploads.size();
    }
    [[nodiscard]] uint64_t GetSubmittedTextureUploadCount() const noexcept
    {
        return m_submittedTextureUploadCount;
    }
    [[nodiscard]] uint64_t GetCompletedTextureUploadCount() const noexcept
    {
        return m_completedTextureUploadCount;
    }
    [[nodiscard]] uint64_t GetAsyncTextureUploadCount() const noexcept
    {
        return m_asyncTextureUploadCount;
    }
    [[nodiscard]] uint64_t GetStagingPoolBytes() const noexcept
    {
        return m_resourceManager.GetStagingPoolBytes();
    }
    [[nodiscard]] size_t GetStagingPoolBufferCount() const noexcept
    {
        return m_resourceManager.GetStagingPoolBufferCount();
    }
    [[nodiscard]] uint64_t GetStagingAllocationCount() const noexcept
    {
        return m_resourceManager.GetStagingAllocationCount();
    }
    [[nodiscard]] uint64_t GetStagingReuseCount() const noexcept
    {
        return m_resourceManager.GetStagingReuseCount();
    }
    [[nodiscard]] uint64_t GetStagingDiscardCount() const noexcept
    {
        return m_resourceManager.GetStagingDiscardCount();
    }
    [[nodiscard]] uint64_t GetTextureGpuResidentBytes() const
    {
        return m_textureCache.GetResidentBytes();
    }
    [[nodiscard]] uint64_t GetTextureGpuBudgetBytes() const
    {
        return m_textureCache.GetBudgetBytes();
    }
    [[nodiscard]] size_t GetTextureGpuCacheEntryCount() const
    {
        return m_textureCache.GetEntryCount();
    }
    [[nodiscard]] size_t GetRetiredTextureGpuLeaseCount() const
    {
        return m_textureCache.GetRetiredLeaseCount();
    }
    [[nodiscard]] uint64_t GetTextureGpuEvictionCount() const
    {
        return m_textureCache.GetEvictionCount();
    }
    void SetTextureGpuBudgetBytes(uint64_t bytes)
    {
        m_textureCache.SetBudgetBytes(bytes);
    }
    [[nodiscard]] size_t TrimTextureGpuBudget()
    {
        return m_textureCache.TrimToBudget();
    }
    [[nodiscard]] uint64_t GetMeshGpuResidentBytes() const;
    [[nodiscard]] uint64_t GetMeshGpuBudgetBytes() const noexcept
    {
        return m_meshGpuBudgetBytes;
    }
    [[nodiscard]] size_t GetMeshGpuCacheEntryCount() const noexcept
    {
        return m_sharedMeshBuffers.size();
    }
    [[nodiscard]] size_t GetRetiredMeshGpuLeaseCount() const;
    [[nodiscard]] uint64_t GetMeshGpuEvictionCount() const noexcept
    {
        return m_meshGpuEvictionCount;
    }
    void SetMeshGpuBudgetBytes(uint64_t bytes);
    [[nodiscard]] size_t TrimMeshGpuBudget();
    [[nodiscard]] uint64_t GetRetiredMeshGpuLeaseBytes() const;
    [[nodiscard]] uint64_t GetRetiredTextureGpuLeaseBytes() const
    {
        return m_textureCache.GetRetiredLeaseBytes();
    }
    [[nodiscard]] GpuEvictionCandidate PeekOldestMeshGpuEvictable() const;
    [[nodiscard]] GpuEvictionCandidate PeekOldestTextureGpuEvictable() const
    {
        return m_textureCache.PeekOldestEvictable();
    }
    [[nodiscard]] uint64_t EvictOldestMeshGpu();
    [[nodiscard]] uint64_t EvictOldestTextureGpu()
    {
        return m_textureCache.EvictOldest();
    }
    [[nodiscard]] std::vector<GpuAssetResidencyRecord> GetAssetGpuResidency() const;
    [[nodiscard]] MaterialGpuResidencySnapshot GetMaterialGpuResidency() const
    {
        MaterialGpuResidencySnapshot snapshot = m_materialPipelineManagerInitialized
                                                    ? m_materialPipelineManager.GetResidencySnapshot()
                                                    : MaterialGpuResidencySnapshot{};
        snapshot.shadowDescriptorSetCount = m_shadowMaterialBindingCache.size();
        snapshot.shadowDescriptorPoolCount = 0; // Shadow no longer owns private pool pages.
        snapshot.shadowBindingCacheHits = m_shadowMaterialBindingCacheHits;
        snapshot.shadowBindingCacheMisses = m_shadowMaterialBindingCacheMisses;
        snapshot.shadowBindingRetirements = m_shadowMaterialBindingRetirements;
        return snapshot;
    }
    [[nodiscard]] size_t GetRuntimeMeshGpuEntryCount() const;
    [[nodiscard]] uint64_t GetRuntimeMeshGpuResidentBytes() const;

    /// @brief Remove per-object buffers for objects that are no longer active.
    /// Call once per frame after SetDrawCalls with the current active draw calls.
    void CleanupUnusedBuffers(const std::vector<DrawCall> &activeDrawCalls);

    /// @brief Same as CleanupUnusedBuffers but accepts pre-built objectId set
    /// to avoid copying DrawCall vectors (saves shared_ptr atomic refcount ops).
    void CleanupUnusedBuffersByIds(const std::unordered_set<uint64_t> &activeIds);

    /// @brief Remove per-object buffers that were not ensured on the current frame.
    /// Returns the number of surviving object buffer entries after cleanup.
    [[nodiscard]] size_t CleanupUnusedBuffersByFrameStamp();

    // ========================================================================
    // Command Buffer Utilities
    // ========================================================================

    VkCommandBuffer BeginSingleTimeCommands();
    void EndSingleTimeCommands(VkCommandBuffer commandBuffer);
    [[nodiscard]] std::shared_ptr<vk::GraphicsSubmissionTicket>
    EndSingleTimeCommandsAsync(VkCommandBuffer commandBuffer, std::function<void()> releaseResources = {})
    {
        return m_resourceManager.EndSingleTimeCommandsAsync(commandBuffer, std::move(releaseResources));
    }

    // ========================================================================
    // Render Callbacks (RenderGraph-based)
    // ========================================================================

    /// @brief Set the render graph execution callback (offscreen/pre-render)
    void SetRenderGraphExecutor(std::function<void(VkCommandBuffer cmdBuf)> executor);

    /// Publish Scene/Game/Preview graph batches into the same frame submission
    /// contract used by async particle compute and presentation.
    using FrameSubmissionBuildCallback =
        std::function<bool(vk::VulkanFrameSubmission &submission, uint32_t frameSetupWorkItem)>;
    void SetFrameSubmissionBuilder(FrameSubmissionBuildCallback builder)
    {
        m_frameSubmissionBuilder = std::move(builder);
    }

    [[nodiscard]] const FrameSubmissionTelemetry &GetFrameSubmissionTelemetry() const noexcept
    {
        return m_frameSubmissionTelemetry;
    }

    /// Optional GPU work that precedes scene rendering. It is submitted as a
    /// typed Compute batch when Compute aliases the Graphics native lane; the
    /// callback falls back to the Graphics command buffer otherwise until
    /// cross-graph queue-family ownership is fully published.
    void SetFrameComputeExecutor(std::function<void(VkCommandBuffer cmdBuf)> executor);

    /// Publish whether the optional frame compute callback has work for the
    /// current frame.  The executor is installed for the lifetime of the
    /// renderer, so the predicate prevents an empty compute submission from
    /// introducing a needless queue dependency on frames without particles.
    void SetFrameComputeWorkPredicate(std::function<bool()> predicate);

    /// Configure a pipelined async-compute frame: simulation overlaps the
    /// current Graphics frame, export runs after Graphics and is consumed by
    /// the next frame.
    void SetFrameAsyncComputeExecutors(std::function<bool(VkCommandBuffer)> simulation,
                                       std::function<bool(VkCommandBuffer)> exportPhase, std::function<bool()> ready,
                                       std::function<uint64_t()> generation);

    /// @brief Set the GUI render callback using RenderGraph context
    /// @param callback Callback that receives RenderContext for drawing
    void SetGuiRenderCallback(std::function<void(vk::RenderContext &ctx)> callback);

    // ========================================================================
    // New Modular API Access
    // ========================================================================

    /**
     * @brief Get the device context for advanced operations
     */
    [[nodiscard]] vk::VkDeviceContext &GetDeviceContext()
    {
        return m_backend.Device();
    }
    [[nodiscard]] const vk::VkDeviceContext &GetDeviceContext() const
    {
        return m_backend.Device();
    }

    [[nodiscard]] const rhi::DeviceCaps &GetRhiCapabilities() const noexcept
    {
        return m_backend.Device().GetCapabilities();
    }

    [[nodiscard]] vk::VulkanBackendContext &GetBackendContext() noexcept
    {
        return m_backend;
    }
    [[nodiscard]] const vk::VulkanBackendContext &GetBackendContext() const noexcept
    {
        return m_backend;
    }

    /**
     * @brief Get the swapchain manager
     */
    [[nodiscard]] vk::VkSwapchainManager &GetSwapchain()
    {
        return m_backend.Presentation();
    }
    [[nodiscard]] const vk::VkSwapchainManager &GetSwapchain() const
    {
        return m_backend.Presentation();
    }

    /**
     * @brief Get the pipeline manager
     */
    [[nodiscard]] vk::VkPipelineManager &GetPipelineManager()
    {
        return m_pipelineManager;
    }
    [[nodiscard]] const vk::VkPipelineManager &GetPipelineManager() const
    {
        return m_pipelineManager;
    }

    /**
     * @brief Get the resource manager
     */
    [[nodiscard]] vk::VkResourceManager &GetResourceManager()
    {
        return m_resourceManager;
    }
    [[nodiscard]] const vk::VkResourceManager &GetResourceManager() const
    {
        return m_resourceManager;
    }

    /**
     * @brief Get the render graph for declarative rendering
     */
    [[nodiscard]] vk::RenderGraph &GetRenderGraph()
    {
        return m_renderGraph;
    }
    [[nodiscard]] const vk::RenderGraph &GetRenderGraph() const
    {
        return m_renderGraph;
    }

    /// @brief Get the shader cache (modules, SPIR-V code, annotations)
    [[nodiscard]] VkShaderCache &GetShaderCache()
    {
        return m_shaderCache;
    }
    [[nodiscard]] const VkShaderCache &GetShaderCache() const
    {
        return m_shaderCache;
    }

    /// @brief Get the texture cache
    [[nodiscard]] VkTextureCache &GetTextureCache()
    {
        return m_textureCache;
    }

    [[nodiscard]] uint64_t GetShaderHotReloadRetirementCount() const noexcept
    {
        return m_shaderHotReloadRetirementCount;
    }
    [[nodiscard]] const VkTextureCache &GetTextureCache() const
    {
        return m_textureCache;
    }

    // ========================================================================
    // Direct Vulkan Access (for compatibility)
    // ========================================================================

    [[nodiscard]] VkDevice GetDevice() const
    {
        return m_backend.Device().GetDevice();
    }
    [[nodiscard]] VkPhysicalDevice GetPhysicalDevice() const
    {
        return m_backend.Device().GetPhysicalDevice();
    }
    [[nodiscard]] VkInstance GetInstance() const
    {
        return m_backend.Device().GetInstance();
    }
    [[nodiscard]] VkQueue GetGraphicsQueue() const
    {
        return m_backend.Device().GetGraphicsQueue();
    }
    [[nodiscard]] VkQueue GetPresentQueue() const
    {
        return m_backend.Device().GetPresentQueue();
    }
    [[nodiscard]] uint32_t GetSwapchainImageCount() const
    {
        return m_backend.Presentation().GetImageCount();
    }
    [[nodiscard]] VkCommandPool GetCommandPool() const
    {
        return m_resourceManager.GetCommandPool();
    }
    [[nodiscard]] VkFormat GetSwapchainFormat() const
    {
        return m_backend.Presentation().GetImageFormat();
    }
    [[nodiscard]] VkExtent2D GetSwapchainExtent() const
    {
        return m_backend.Presentation().GetExtent();
    }

    // ========================================================================
    // Scene Render Target / Editor Integration
    // ========================================================================

    /// @brief Set scene render target dimensions for aspect ratio calculation
    void SetSceneRenderTargetSize(uint32_t width, uint32_t height)
    {
        m_sceneRenderTargetWidth = width;
        m_sceneRenderTargetHeight = height;
    }

    /// @brief Set editor gizmos for rendering
    void SetEditorGizmos(EditorGizmos *gizmos)
    {
        m_editorGizmos = gizmos;
    }

    /// @brief Refresh a material's pipeline using its vertex and fragment shader names.
    bool RefreshMaterialPipeline(std::shared_ptr<InxMaterial> material, const std::string &vertShaderName,
                                 const std::string &fragShaderName);
    bool RefreshPreviewMaterialPipeline(std::shared_ptr<InxMaterial> material, const std::string &vertShaderName,
                                        const std::string &fragShaderName, bool reportDomainMismatch = true);

    [[nodiscard]] std::shared_ptr<vk::ImageReadbackTicket>
    BeginMaterialPreviewGPU(const std::shared_ptr<InxMaterial> &material, int size);
    bool TryCompleteMaterialPreviewGPU(const std::shared_ptr<vk::ImageReadbackTicket> &ticket, int outputSize,
                                       std::vector<unsigned char> &outPixels);

    [[nodiscard]] std::shared_ptr<vk::ImageReadbackTicket>
    BeginMeshPreviewGPU(const InxMesh &mesh, const std::vector<std::shared_ptr<InxMaterial>> &materials, int size);
    bool TryCompleteMeshPreviewGPU(const std::shared_ptr<vk::ImageReadbackTicket> &ticket, int outputSize,
                                   std::vector<unsigned char> &outPixels);

    /// @brief Live mesh preview — GPU render target displayed directly in ImGui (no CPU readback).
    uint64_t RenderMeshPreviewGPUImGuiCamera(const InxMesh &mesh,
                                             const std::vector<std::shared_ptr<InxMaterial>> &materials, int size,
                                             const glm::mat4 &view, const glm::mat4 &proj, const glm::vec3 &cameraPos,
                                             bool cloneMaterials = false);

    /// @brief Currently-published live mesh preview descriptor id (0 when absent).
    ///
    /// Ids returned by RenderMeshPreviewGPUImGuiCamera become invalid whenever
    /// the preview target is recreated; callers caching an id must validate it
    /// against this before reuse.
    [[nodiscard]] uint64_t GetMeshPreviewDisplayTextureId() const;

    /// @brief Release GPU preview resources while the ImGui Vulkan backend is still alive.
    void ReleaseGpuPreviews();

    /// @brief Resolve the shared shadow pipeline and cached material binding.
    /// @return The material descriptor set used at set 2, or VK_NULL_HANDLE on failure.
    VkDescriptorSet EnsureMaterialShadowPipeline(const std::shared_ptr<InxMaterial> &material,
                                                 const std::string &vertShaderName, const std::string &fragShaderName);

    /// Shadow pipeline layout always includes set 2; bind this when a material
    /// has no per-material shadow descriptors (e.g. alpha clip off, no vtx UBO).
    bool EnsureShadowMaterialDummyDescriptorSet();

    /// @brief Initialize material system (default material, pipelines)
    void InitializeMaterialSystem();

    /// @brief Transactionally publish a new material-pipeline MSAA generation.
    /// Shader programs and descriptors remain resident; replaced GPU objects
    /// retire after the last reserved cross-queue completion epoch.
    [[nodiscard]] bool CommitMaterialPipelineGeneration(VkSampleCountFlagBits newSampleCount);

    // ========================================================================
    // Buffer Accessors (for OutlineRenderer)
    // ========================================================================

    /// @brief Get per-object vertex buffer VkBuffer handle (VK_NULL_HANDLE if not found)
    [[nodiscard]] VkBuffer GetObjectVertexBuffer(uint64_t objectId) const;

    /// @brief Get per-object index buffer VkBuffer handle (VK_NULL_HANDLE if not found)
    [[nodiscard]] VkBuffer GetObjectIndexBuffer(uint64_t objectId) const;

    /// @brief Get the zero-initialized fallback material UBO.
    [[nodiscard]] VkBuffer GetFallbackMaterialUbo() const;

    /// @brief Get instance SSBO VkBuffer at given index.
    [[nodiscard]] VkBuffer GetInstanceSSBO(size_t index) const;

    /// @brief Get shader module by name and type ("vertex" or "fragment")
    [[nodiscard]] VkShaderModule GetShaderModule(const std::string &name, const std::string &type) const;

    /// @brief Get the shadow depth sampler (used by per-view shadow descriptors)
    [[nodiscard]] VkSampler GetShadowDepthSampler() const
    {
        return m_shadowDepthSampler;
    }

    // ========================================================================
    // Per-View Descriptor Set (set 1) — multi-camera shadow isolation
    // ========================================================================

    /// @brief Get the canonical per-view descriptor layout used by geometry and particles.
    /// Used by SceneRenderGraph to allocate per-graph descriptor sets.
    [[nodiscard]] VkDescriptorSetLayout GetPerViewDescSetLayout() const
    {
        return m_perViewDescSetLayout;
    }

    /// Allocate a per-view descriptor lease. The owning RenderView retires the
    /// lease when its generation is replaced or destroyed.
    [[nodiscard]] vk::DescriptorLease AllocatePerViewDescriptorLease();

    /// @brief Update a per-view descriptor set with shadow map resources.
    void UpdatePerViewShadowMap(VkDescriptorSet perViewDescSet, VkImageView shadowView, VkSampler shadowSampler,
                                VkImageLayout imageLayout = VK_IMAGE_LAYOUT_DEPTH_STENCIL_READ_ONLY_OPTIMAL);

    /// @brief Clear a per-view descriptor set (bind default white texture).
    void ClearPerViewShadowMap(VkDescriptorSet perViewDescSet);

    /// Bind the frame-local canonical lights and tiled Forward+ outputs.
    void UpdatePerViewForwardPlusBuffers(VkDescriptorSet perViewDescSet, rhi::BufferHandle canonicalLights,
                                         uint64_t canonicalBytes, rhi::BufferHandle tileHeaders,
                                         uint64_t tileHeaderBytes, rhi::BufferHandle tileLightMasks,
                                         uint64_t tileLightMaskBytes, rhi::BufferHandle lightingUbo = {},
                                         uint64_t lightingUboBytes = 0);

    /// Bind the camera-local LightingUBO even when the pipeline does not use Forward+.
    void UpdatePerViewLightingBuffer(VkDescriptorSet perViewDescSet, VkBuffer lightingUbo, uint64_t lightingUboBytes);

    /// Bind the frame-local camera matrices owned by this RenderView.
    void UpdatePerViewCameraBuffer(VkDescriptorSet perViewDescSet, VkBuffer cameraUbo, uint64_t cameraUboBytes);

    // ========================================================================
    // Lighting System
    // ========================================================================

    /// @brief Get the scene light collector for ambient/fog settings
    [[nodiscard]] SceneLightCollector &GetLightCollector()
    {
        return m_lightCollector;
    }

    /// @brief Get material pipeline manager
    [[nodiscard]] MaterialPipelineManager &GetMaterialPipelineManager()
    {
        return m_materialPipelineManager;
    }

    /// @brief Set ambient color (convenience method)
    void SetAmbientColor(const glm::vec3 &color, float intensity = 1.0f);

    /// @brief Rebuild scene lighting state and upload canonical lights.
    void UpdateLightingState();

    [[nodiscard]] const lighting::CanonicalLightGpuFrame *GetCanonicalLightGpuFrame() const noexcept
    {
        if (m_canonicalLightGpuBuffer.FrameCount() == 0)
            return nullptr;
        return &m_canonicalLightGpuBuffer.Frame(m_currentFrame % m_maxFramesInFlight);
    }

    [[nodiscard]] const lighting::CanonicalLightGpuFrame *GetCanonicalLightGpuFrame(uint32_t frameIndex) const noexcept
    {
        if (frameIndex >= m_canonicalLightGpuBuffer.FrameCount())
            return nullptr;
        return &m_canonicalLightGpuBuffer.Frame(frameIndex);
    }

    // ========================================================================
    // Frame Synchronization & Deferred Deletion
    // ========================================================================

    /// @brief Wait for the current frame-in-flight fence to signal.
    ///
    /// Must be called BEFORE any CPU-side resource mutation that could
    /// conflict with in-flight GPU work for this frame slot.
    void WaitForCurrentFrame();

    /// @brief Tick the deferred deletion queue.
    ///
    /// Flushes entries that are old enough (>= maxFramesInFlight frames)
    /// to guarantee no in-flight command buffer references them.
    /// Call once per frame AFTER WaitForCurrentFrame().
    void CollectRetiredGpuResources();

    /// @brief Immediately flush all deferred deletions.
    ///
    /// Caller must ensure the device is idle before invoking this.
    void FlushRetiredGpuResources();

    /// @brief Enqueue a GPU resource for deferred deletion.
    ///
    /// The deleter lambda will be invoked after maxFramesInFlight frames,
    /// when all in-flight command buffers that might reference the resource
    /// have completed.
    void RetireGpuResource(std::function<void()> deleter);

    [[nodiscard]] GpuRetirementQueue &GetRetirementQueue()
    {
        return m_deletionQueue;
    }

#if INFERNUX_FRAME_PROFILE
    [[nodiscard]] const rhi::GpuTimestampFrame &GetLatestGpuTimestampFrame() const noexcept
    {
        return m_gpuTimestampQueries.LatestFrame();
    }

    [[nodiscard]] const rhi::TimestampQueryCapabilities &GetTimestampQueryCapabilities() const noexcept
    {
        return m_gpuTimestampQueries.Capabilities();
    }
#endif

    /// @brief Get the async upload context. Always valid after Initialize();
    /// will alias to the graphics queue if the GPU has no dedicated transfer
    /// family, so callers don't need to branch.
    [[nodiscard]] vk::AsyncTransferContext &GetAsyncTransferContext()
    {
        return m_asyncTransferContext;
    }

    [[nodiscard]] vk::AsyncTransferContext &GetAsyncReadbackContext()
    {
        return m_asyncReadbackContext;
    }

  private:
    // ========================================================================
    // Internal Methods
    // ========================================================================

    void RecreateSwapchain();
    void ReleaseMaterialPassResolutionCache() noexcept;
    void CreateDepthResources();

    void CreateUniformBuffers();
    vk::RenderGraph &GetGuiRenderGraph(uint32_t imageIndex);
    [[nodiscard]] bool EnsureGuiRenderGraph(uint32_t imageIndex);
    void DestroyGuiRenderGraphs();
    [[nodiscard]] bool RecordFrameCommands(VkCommandBuffer commandBuffer, uint32_t imageIndex);

    /// @brief Create a raw Vulkan buffer via VMA
    void CreateBuffer(VkDeviceSize size, VkBufferUsageFlags usage, VkMemoryPropertyFlags properties, VkBuffer &buffer,
                      VmaAllocation &allocation);

    // ========================================================================
    // New Modular Components
    // ========================================================================

    vk::VulkanBackendContext m_backend;
    vk::VkPipelineManager m_pipelineManager;
    vk::VkResourceManager m_resourceManager;
    vk::AsyncTransferContext m_asyncTransferContext;
    vk::AsyncTransferContext m_asyncReadbackContext;
    vk::RenderGraph m_renderGraph;
    vk::VulkanFrameSubmission m_frameSubmission;
    vk::VulkanSubmissionExecutor m_submissionExecutor;
    FrameSubmissionTelemetry m_frameSubmissionTelemetry;
    std::vector<std::unique_ptr<vk::RenderGraph>> m_additionalGuiRenderGraphs;
    std::vector<bool> m_guiRenderGraphReady;
#if INFERNUX_FRAME_PROFILE
    vk::GpuTimestampQueries m_gpuTimestampQueries;
#endif

    // ========================================================================
    // Configuration
    // ========================================================================

    vk::DeviceConfig m_deviceConfig;
    uint32_t m_maxFramesInFlight;
    uint32_t m_currentFrame = 0;
    bool m_framebufferResized = false;

    // DrawFrame sub-timing accumulators
    // [0] Acquire  [1] Record(total)  [2] Submit  [3] Present
    // Record breakdown: [4] UBO  [5] SceneGraph  [6] GUIGraph  [7] reserved
    // Scene draw breakdown: [8] FilteredTotal  [9] Filter  [10] Sort  [11] Draw
    // Shadow breakdown: [12] ShadowTotal  [13] ShadowFilter  [14] ShadowDraw
    // Shadow sub-breakdown: [15] Sort  [16] Cull  [17] Upload  [18] Batch
    static constexpr int kDrawSubSlots = 19;
#if INFERNUX_FRAME_PROFILE
    double m_drawSubMs[kDrawSubSlots] = {};
    int m_drawSubCount = 0;
    uint64_t m_drawSceneFilteredCalls = 0;
    uint64_t m_drawSceneFilteredEligible = 0;
    uint64_t m_drawSceneFilteredIssued = 0;
    uint64_t m_drawFilteredActualDraws = 0;
    uint64_t m_drawShadowCalls = 0;
    uint64_t m_drawShadowEligible = 0;
    uint64_t m_drawShadowIssued = 0;
    uint64_t m_drawShadowActualDraws = 0;
#endif

  public:
#if INFERNUX_FRAME_PROFILE
    /// Retrieve and reset DrawFrame sub-timing (returns frame count).
    int GetDrawSubTimings(double outMs[kDrawSubSlots]) const
    {
        for (int i = 0; i < kDrawSubSlots; ++i)
            outMs[i] = m_drawSubMs[i];
        return m_drawSubCount;
    }
    void GetDrawSubCounters(uint64_t &filteredCalls, uint64_t &filteredEligible, uint64_t &filteredIssued,
                            uint64_t &filteredActualDraws, uint64_t &shadowCalls, uint64_t &shadowEligible,
                            uint64_t &shadowIssued, uint64_t &shadowActualDraws) const
    {
        filteredCalls = m_drawSceneFilteredCalls;
        filteredEligible = m_drawSceneFilteredEligible;
        filteredIssued = m_drawSceneFilteredIssued;
        filteredActualDraws = m_drawFilteredActualDraws;
        shadowCalls = m_drawShadowCalls;
        shadowEligible = m_drawShadowEligible;
        shadowIssued = m_drawShadowIssued;
        shadowActualDraws = m_drawShadowActualDraws;
    }
    void ResetDrawSubTimings()
    {
        for (int i = 0; i < kDrawSubSlots; ++i)
            m_drawSubMs[i] = 0.0;
        m_drawSubCount = 0;
        m_drawSceneFilteredCalls = 0;
        m_drawSceneFilteredEligible = 0;
        m_drawSceneFilteredIssued = 0;
        m_drawFilteredActualDraws = 0;
        m_drawShadowCalls = 0;
        m_drawShadowEligible = 0;
        m_drawShadowIssued = 0;
        m_drawShadowActualDraws = 0;
    }
#endif

  private:
    // Window size fallback (used when surface extent is undefined)
    uint32_t m_windowWidth = 0;
    uint32_t m_windowHeight = 0;

    // ========================================================================
    // Vulkan handles (accessed by InxRenderer for surface creation)
    // ========================================================================

  public:
    // These are exposed for InxRenderer compatibility (friend class can access)
    VkInstance m_instance = VK_NULL_HANDLE;
    VkSurfaceKHR m_surface = VK_NULL_HANDLE;

  private:
    // Scene render target dimensions for aspect ratio calculation
    uint32_t m_sceneRenderTargetWidth = 0;
    uint32_t m_sceneRenderTargetHeight = 0;

    // Material system state
    bool m_materialSystemInitialized = false;
    bool m_materialPipelineManagerInitialized = false;
    VkSampleCountFlagBits m_msaaSampleCount = VK_SAMPLE_COUNT_4_BIT;

    // Shutdown coordination — set by InxRenderer before destroying subsystems
    bool m_shuttingDown = false;

    // Editor gizmos
    EditorGizmos *m_editorGizmos = nullptr;

    // Depth resources
    std::unique_ptr<vk::VkImageHandle> m_depthImage;

    // Default material UBO (binding 2) — single buffer, persistently mapped.
    std::unique_ptr<vk::VkBufferHandle> m_materialUbo;
    void *m_materialUboMapped = nullptr;

    // Scene light collector
    SceneLightCollector m_lightCollector;
    lighting::CanonicalLightGpuBuffer m_canonicalLightGpuBuffer;

    // Shader cache (modules, SPIR-V code, render-state annotations, program cache)
    VkShaderCache m_shaderCache;
    std::function<void(const std::shared_ptr<InxMaterial> &)> m_shaderProgramArtifactResolver;

    // Reflection-based material pipeline manager
    MaterialPipelineManager m_materialPipelineManager;

    // Geometry-domain rejections are terminal for one material/stage pair.
    // The draw path keeps the previous complete pipeline generation instead
    // of attempting to combine its Forward pass with incompatible new passes.
    std::unordered_set<std::string> m_rejectedGeometryMaterialPrograms;

    // Resolved semantic pass handles are stable across frames. Keep this
    // cache outside DrawSceneFiltered so a stable material does not repeatedly
    // rebuild MaterialPassRenderDataKey, walk descriptor ABI bindings, or
    // materialize shader variants. The manager publication generation and the
    // material pipeline-dirty flag invalidate it on relevant hot updates;
    // descriptor liveness is still checked immediately before a cached result
    // is used.
    struct MaterialPassResolutionCacheKey
    {
        const InxMaterial *material = nullptr;
        size_t pipelineHash = 0;

        friend bool operator==(const MaterialPassResolutionCacheKey &lhs,
                               const MaterialPassResolutionCacheKey &rhs) noexcept
        {
            return lhs.material == rhs.material && lhs.pipelineHash == rhs.pipelineHash;
        }
    };

    struct MaterialPassResolutionCacheKeyHash
    {
        [[nodiscard]] size_t operator()(const MaterialPassResolutionCacheKey &key) const noexcept
        {
            size_t hash = std::hash<const InxMaterial *>{}(key.material);
            hash ^= key.pipelineHash + static_cast<size_t>(0x9e3779b97f4a7c15ull) + (hash << 6u) + (hash >> 2u);
            return hash;
        }
    };

    struct MaterialPassResolutionCacheEntry
    {
        std::weak_ptr<InxMaterial> owner;
        MaterialPassPipelineDescriptor pipeline;
        VkPipeline pipelineHandle = VK_NULL_HANDLE;
        VkPipelineLayout pipelineLayout = VK_NULL_HANDLE;
        VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
        ShaderProgramPublication shaderProgram;

        [[nodiscard]] bool IsValid() const noexcept
        {
            return pipelineHandle != VK_NULL_HANDLE && pipelineLayout != VK_NULL_HANDLE &&
                   descriptorSet != VK_NULL_HANDLE && shaderProgram != nullptr;
        }
    };

    std::unordered_map<MaterialPassResolutionCacheKey, MaterialPassResolutionCacheEntry,
                       MaterialPassResolutionCacheKeyHash>
        m_materialPassResolutionCache;
    uint64_t m_materialPassResolutionCacheGeneration = 0;

    // Texture cache (GPU textures keyed by name/GUID, thread-safe)
    VkTextureCache m_textureCache;
    struct PendingTextureGpuUpload
    {
        std::string guid;
        uint64_t runtimeVersion = 0;
        std::shared_ptr<vk::TextureUploadTicket> ticket;
    };
    std::unordered_map<std::string, std::shared_ptr<AssetLoadTicket>> m_pendingTextureCpuLoads;
    std::unordered_map<std::string, PendingTextureGpuUpload> m_pendingTextureGpuUploads;
    uint64_t m_submittedTextureUploadCount = 0;
    uint64_t m_completedTextureUploadCount = 0;
    uint64_t m_asyncTextureUploadCount = 0;

    // GPU material preview (lazy-initialized)
    std::unique_ptr<GPUMaterialPreview> m_gpuMaterialPreview;
    // GPU mesh preview (lazy-initialized)
    std::unique_ptr<GPUMeshPreview> m_gpuMeshPreview;

    /// @brief Shared texture resolution logic (used by TextureResolver lambda).
    /// Resolves an asset GUID to a GPU image using GUID-based cache keys.
    TextureResolveResult ResolveTextureForMaterial(const std::string &textureRef, const std::string &bindingName);
    TextureResolveResult ResolveTextureForVectorField(const std::string &textureGuid, bool linearFiltering,
                                                      bool repeat);
    TextureResolveResult ResolveTextureAsset(const std::string &textureGuid, const std::string &bindingName,
                                             TextureDimension expectedDimension, const char *filterOverride,
                                             const char *wrapOverride);

    // ========================================================================
    // Per-object GPU buffers
    // ========================================================================

    struct SharedMeshKey
    {
        std::string assetGuid;
        uint64_t runtimeVersion = 0;
        size_t contentHash = 0;
        size_t vertexCount = 0;
        size_t indexCount = 0;

        bool operator==(const SharedMeshKey &other) const noexcept
        {
            return assetGuid == other.assetGuid && runtimeVersion == other.runtimeVersion &&
                   contentHash == other.contentHash && vertexCount == other.vertexCount &&
                   indexCount == other.indexCount;
        }
    };

    struct SharedMeshKeyHash
    {
        size_t operator()(const SharedMeshKey &key) const noexcept
        {
            size_t h = std::hash<std::string>{}(key.assetGuid);
            h ^= std::hash<uint64_t>{}(key.runtimeVersion) + 0x9e3779b9 + (h << 6) + (h >> 2);
            h ^= key.contentHash + 0x9e3779b9 + (h << 6) + (h >> 2);
            h ^= std::hash<size_t>{}(key.vertexCount) + 0x9e3779b9 + (h << 6) + (h >> 2);
            h ^= std::hash<size_t>{}(key.indexCount) + 0x9e3779b9 + (h << 6) + (h >> 2);
            return h;
        }
    };

    /// @brief FNV-1a content hash for mesh data deduplication.
    static size_t HashMeshContent(const void *vertexData, size_t vertexBytes, const void *indexData, size_t indexBytes)
    {
        // FNV-1a 64-bit
        constexpr size_t fnvOffset = 14695981039346656037ULL;
        constexpr size_t fnvPrime = 1099511628211ULL;
        size_t hash = fnvOffset;
        const auto *bytes = static_cast<const uint8_t *>(vertexData);
        for (size_t i = 0; i < vertexBytes; ++i) {
            hash ^= bytes[i];
            hash *= fnvPrime;
        }
        bytes = static_cast<const uint8_t *>(indexData);
        for (size_t i = 0; i < indexBytes; ++i) {
            hash ^= bytes[i];
            hash *= fnvPrime;
        }
        return hash;
    }

    void PumpPendingMeshUploads();
    void PumpPendingTextureLoads();
    [[nodiscard]] std::vector<GpuAssetResidencyRecord> GetAssetTextureGpuResidency() const
    {
        return m_textureCache.GetAssetResidency();
    }

    /// @brief Shared vertex/index buffer pair keyed by mesh storage identity.
    struct SharedMeshBuffers
    {
        std::shared_ptr<vk::VkBufferHandle> vertexBuffer;
        std::shared_ptr<vk::VkBufferHandle> indexBuffer;
        size_t vertexCount = 0;
        size_t indexCount = 0;
        uint64_t residentBytes = 0;
        uint64_t lastUsedFrame = 0;
    };

    struct RetiredMeshLease
    {
        std::weak_ptr<vk::VkBufferHandle> vertexBuffer;
        std::weak_ptr<vk::VkBufferHandle> indexBuffer;
        uint64_t residentBytes = 0;
    };

    struct PendingSharedMeshBuffers
    {
        std::shared_ptr<vk::BufferUploadTicket> vertexUpload;
        std::shared_ptr<vk::BufferUploadTicket> indexUpload;
        size_t vertexCount = 0;
        size_t indexCount = 0;
    };

    /// @brief Per-object reference into the shared mesh-buffer cache.
    struct PerObjectBuffers
    {
        std::shared_ptr<vk::VkBufferHandle> vertexBuffer;
        std::shared_ptr<vk::VkBufferHandle> indexBuffer;
        size_t vertexCount = 0;
        size_t indexCount = 0;
        SharedMeshKey sharedKey;
        const void *lastVertexPtr = nullptr; // fast-path: skip hash if pointer unchanged
        const void *lastIndexPtr = nullptr;
        uint64_t ensuredOnFrame = 0; // frame-stamp: skip duplicate EnsureObjectBuffers per frame
    };

    /// @brief Map from objectId → persistent GPU buffers.
    /// Objects with identical mesh storage share the same GPU buffers.
    std::unordered_map<uint64_t, PerObjectBuffers> m_perObjectBuffers;
    // Invalidates metadata snapshots whenever an object binding is inserted,
    // replaced, or erased. Revision mismatch takes the safe lookup path.
    uint64_t m_objectBufferRevision = 1;
    uint64_t m_ensureFrameCounter = 0; // incremented once per frame
    struct ObjectBufferBindingCacheEntry
    {
        uint64_t identity = 0;
        size_t drawCallCount = 0;
        bool valid = false;
    };
    std::array<ObjectBufferBindingCacheEntry, 4> m_objectBufferBindingCache{};
    size_t m_objectBufferBindingCacheCursor = 0;
    bool m_fullObjectBufferEnsureThisFrame = false;
    bool m_skipObjectBufferCleanupThisFrame = false;

    /// @brief Shared mesh GPU buffer cache keyed by vertex/index storage pointers.
    std::unordered_map<SharedMeshKey, SharedMeshBuffers, SharedMeshKeyHash> m_sharedMeshBuffers;
    std::unordered_map<SharedMeshKey, PendingSharedMeshBuffers, SharedMeshKeyHash> m_pendingSharedMeshBuffers;
    mutable std::vector<RetiredMeshLease> m_retiredMeshLeases;
    uint64_t m_meshGpuBudgetBytes = 512ULL * 1024ULL * 1024ULL;
    mutable uint64_t m_meshGpuResidentBytes = 0;
    uint64_t m_meshGpuEvictionCount = 0;
    uint64_t m_submittedMeshUploadCount = 0;
    uint64_t m_completedMeshUploadCount = 0;
    uint64_t m_asyncMeshUploadCount = 0;

    void PublishSharedMeshBuffers(const SharedMeshKey &key, SharedMeshBuffers buffers);
    void RetireSharedMeshBuffers(SharedMeshBuffers buffers, bool eviction);
    void SweepRetiredMeshLeases() const;
    [[nodiscard]] std::vector<GpuAssetResidencyRecord> GetAssetMeshGpuResidency() const;

    // Render callbacks (RenderGraph-based)
    std::function<void(VkCommandBuffer cmdBuf)> m_renderGraphExecutor;
    FrameSubmissionBuildCallback m_frameSubmissionBuilder;
    std::function<void(VkCommandBuffer cmdBuf)> m_frameComputeExecutor;
    std::function<bool()> m_frameComputeWorkPredicate;
    std::function<bool(VkCommandBuffer cmdBuf)> m_frameAsyncSimulationExecutor;
    std::function<bool(VkCommandBuffer cmdBuf)> m_frameAsyncExportExecutor;
    std::function<bool()> m_frameAsyncComputeReady;
    std::function<uint64_t()> m_frameAsyncComputeGeneration;
    bool m_frameAsyncComputePrimed = false;
    uint64_t m_frameAsyncComputePrimedGeneration = 0;
    VkSemaphore m_frameAsyncPreviousExportTimeline = VK_NULL_HANDLE;
    uint64_t m_frameAsyncPreviousExportTimelineValue = 0;
    std::function<void(vk::RenderContext &ctx)> m_guiRenderCallback;
    rhi::RenderViewContext m_presentationView;

    // Unity-style draw calls for multi-material rendering (pointer to external storage, no copy)
    const std::vector<DrawCall> *m_drawCallsPtr = nullptr;
    const std::vector<DrawCall> *m_shadowDrawCallsPtr = nullptr;

    // Per-list metadata is built once when a graph activates its draw list.
    // DrawSceneFiltered and DrawShadowCasters can run several passes over the
    // same list; keeping the material queue and stable GPU-buffer leases here
    // avoids repeating material accessors and unordered_map lookups in every
    // pass. The leases deliberately do not point into m_perObjectBuffers:
    // cleanup, replacement, and unordered_map rehash must not invalidate a
    // draw list that is already being recorded.
    struct DrawListMetadata
    {
        uint64_t objectId = 0;
        InxMaterial *material = nullptr;
        int renderQueue = 2000;
        std::shared_ptr<vk::VkBufferHandle> vertexBuffer;
        std::shared_ptr<vk::VkBufferHandle> indexBuffer;
        size_t indexCapacity = 0;
    };
    const std::vector<DrawCall> *m_drawListMetadataSource = nullptr;
    const std::vector<DrawCall> *m_shadowListMetadataSource = nullptr;
    uint64_t m_drawListBufferRevision = 0;
    uint64_t m_shadowListBufferRevision = 0;
    std::vector<DrawListMetadata> m_drawListMetadata;
    std::vector<DrawListMetadata> m_shadowListMetadata;
    // SkyboxPass has an explicit RenderDomain contract. Keep its indices so
    // the pass does not rescan the full camera list on every callback.
    const std::vector<DrawCall> *m_skyboxDrawListSource = nullptr;
    std::vector<size_t> m_skyboxDrawCallIndices;
    std::vector<int> m_drawQueueValues;
    bool m_drawQueueValuesOverflow = false;
    static inline const std::vector<DrawCall> s_emptyDrawCalls{};
    const std::vector<DrawCall> &drawCalls() const
    {
        return m_drawCallsPtr ? *m_drawCallsPtr : s_emptyDrawCalls;
    }
    const std::vector<DrawCall> &shadowDrawCalls() const
    {
        return m_shadowDrawCallsPtr ? *m_shadowDrawCallsPtr : drawCalls();
    }

    // Pre-allocated scratch buffers for DrawSceneFiltered / DrawShadowCasters
    struct SortableDrawCall
    {
        const DrawCall *dc;
        float sortKey;
        size_t materialHash;
        // Raw handles are used only during the current recording. Their
        // VkBufferHandle owners live in the active/fallback metadata leases.
        VkBuffer vertexBuf;
        VkBuffer indexBuf;
        const std::shared_ptr<InxMaterial> *materialOwner;
        InxMaterial *material; // Resolved once in the filter loop; materialOwner keeps it alive.
    };
    std::vector<SortableDrawCall> m_eligibleScratch;

    // Cached builtin material lookups (refreshed per SetDrawCalls)
    std::shared_ptr<InxMaterial> m_cachedDefaultLit;
    std::shared_ptr<InxMaterial> m_cachedErrorMat;

    struct ShadowDraw
    {
        const DrawCall *dc;
        // Backed by draw-list metadata leases for this recording.
        VkBuffer vertexBuf = VK_NULL_HANDLE;
        VkBuffer indexBuf = VK_NULL_HANDLE;
        VkPipeline shadowPipeline;
        VkDescriptorSet shadowMaterialDescSet = VK_NULL_HANDLE;
        AABB worldBounds; // Cached for per-cascade frustum culling
    };
    std::vector<ShadowDraw> m_shadowDrawScratch;
    std::vector<uint32_t> m_shadowViewVisible; ///< Per-view visible indices into m_shadowDrawScratch

    struct ResolvedShadowMaterial
    {
        VkPipeline pipeline = VK_NULL_HANDLE;
        VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
    };
    std::unordered_map<const InxMaterial *, ResolvedShadowMaterial> m_resolvedShadowMaterialsScratch;

    // ========================================================================
    // Shadow Pipeline (lazy-initialized by DrawShadowCasters)
    // ========================================================================
    VkPipelineLayout m_shadowPipelineLayout = VK_NULL_HANDLE;
    VkDescriptorSetLayout m_shadowDescSetLayout = VK_NULL_HANDLE;
    VkDescriptorSetLayout m_shadowGlobalsDescSetLayout = VK_NULL_HANDLE;
    VkDescriptorSetLayout m_shadowMaterialDescSetLayout = VK_NULL_HANDLE;
    struct ShadowCameraResources
    {
        struct StreamFrame
        {
            VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
            std::unique_ptr<vk::VkBufferHandle> instanceBuffer;
            std::unique_ptr<vk::VkBufferHandle> skinInstanceBuffer;
            std::unique_ptr<vk::VkBufferHandle> skinPaletteBuffer;
            void *instanceMapped = nullptr;
            void *skinInstanceMapped = nullptr;
            void *skinPaletteMapped = nullptr;
            size_t instanceCapacity = 0;
            size_t skinPaletteCapacity = 0;
            uint32_t instanceWriteOffset = 0;
            uint32_t skinPaletteWriteOffset = 0;
            uint64_t frameSerial = 0;
            std::unordered_map<const void *, GPUSkinInstanceData> skinPaletteCache;
        };

        std::vector<vk::DescriptorLease> descriptorLeases;
        std::vector<VkDescriptorSet> descriptorSets;
        std::vector<VkBuffer> uniformBuffers;
        std::vector<VmaAllocation> allocations;
        std::vector<void *> mappedPointers;
        std::vector<vk::DescriptorLease> streamDescriptorLeases;
        std::vector<StreamFrame> streamFrames;
    };
    std::unordered_map<ShadowCameraResourceId, ShadowCameraResources> m_shadowCameraResources;
    ShadowCameraResourceId m_nextShadowCameraResourceId = 1;
    VkSampler m_shadowDepthSampler = VK_NULL_HANDLE;
    VkRenderPass m_shadowCompatRenderPass = VK_NULL_HANDLE; ///< For pipeline compatibility
    bool m_shadowPipelineReady = false;

    /// Cache of shadow pipelines keyed by shader ID (vert|frag).
    /// Materials sharing the same shader share the same shadow VkPipeline.
    std::unordered_map<std::string, VkPipeline> m_shadowPipelineCache;
    uint64_t m_shaderHotReloadRetirementCount = 0;

    static constexpr uint32_t kMaxShadowMaterialTextures = 8;
    struct ShadowMaterialBindingEntry
    {
        std::weak_ptr<InxMaterial> owner;
        std::string materialKey;
        uint64_t materialVersion = 0;
        uint64_t artifactRevision = 0;
        size_t resourceSignature = 0;
        VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
        vk::DescriptorLease descriptorLease;
        std::vector<std::shared_ptr<const rhi::TextureGpuView>> textureKeepAlive;
    };

    struct ShadowDescriptorAllocation
    {
        VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
        vk::DescriptorLease descriptorLease;
    };

    vk::DescriptorLease m_shadowMaterialDummyLease;
    std::unordered_map<const InxMaterial *, ShadowMaterialBindingEntry> m_shadowMaterialBindingCache;
    uint64_t m_shadowMaterialBindingCacheHits = 0;
    uint64_t m_shadowMaterialBindingCacheMisses = 0;
    uint64_t m_shadowMaterialBindingRetirements = 0;

    /// @brief Lazily create/recreate shadow pipeline resources.
    bool EnsureShadowPipeline(VkRenderPass compatibleRenderPass);
    bool EnsureShadowCameraResources(ShadowCameraResourceId resourceId);
    bool EnsureShadowCameraStreamCapacity(ShadowCameraResources &resources, uint32_t frameIndex, size_t instanceCount,
                                          size_t skinPaletteCount);
    void UpdateShadowCameraStreamDescriptor(ShadowCameraResources::StreamFrame &frame, uint32_t frameIndex);
    void DestroyShadowCameraResources(ShadowCameraResources &resources) noexcept;
    [[nodiscard]] ShadowDescriptorAllocation AllocateShadowMaterialDescriptorSet();
    [[nodiscard]] VkDescriptorSet EnsureShadowMaterialBinding(const std::shared_ptr<InxMaterial> &material,
                                                              const MaterialDescriptorSet *forwardMaterialDesc,
                                                              const ShaderProgram *forwardProgram,
                                                              const ShaderProgram *shadowProgram,
                                                              uint64_t artifactRevision);
    void RetireShadowMaterialBinding(ShadowMaterialBindingEntry entry);
    void CollectUnusedShadowMaterialBindings();
    /// @brief Create shadow depth sampler for shadow map sampling.
    bool CreateShadowDepthSampler();
    /// @brief Cleanup shadow pipeline resources.
    void CleanupShadowPipeline();

    // ========================================================================
    // Per-View Descriptor Set (set 1) — multi-camera shadow isolation
    // ========================================================================
    VkDescriptorSetLayout m_perViewDescSetLayout = VK_NULL_HANDLE;
    /// Valid dummy bindings for layout set 2 (never freed per material).
    VkDescriptorSet m_shadowMaterialDummyDescSet = VK_NULL_HANDLE;

    /// @brief Create per-view descriptor set layout and pool.
    bool CreatePerViewDescriptorResources();
    /// @brief Destroy per-view descriptor set layout and pool.
    void DestroyPerViewDescriptorResources();

    // ========================================================================
    // Frame-safe deferred deletion queue
    // ========================================================================
    GpuRetirementQueue m_deletionQueue;

    // ========================================================================
    // Staged UBO data (CPU-side cache → GPU via vkCmdUpdateBuffer)
    // ========================================================================

    // ========================================================================
    // Engine Globals UBO (set 2, binding 0) — per-frame time/screen/camera
    // ========================================================================
    std::vector<std::unique_ptr<vk::VkBufferHandle>> m_globalsBuffers;
    VkDescriptorSetLayout m_globalsDescSetLayout = VK_NULL_HANDLE;
    std::vector<vk::DescriptorLease> m_globalsDescriptorLeases;
    std::vector<VkDescriptorSet> m_globalsDescSets;

    EngineGlobalsUBO m_stagedGlobals{};
    bool m_globalsDirty = false;

    /// @brief Create globals UBO buffers (one per frame-in-flight).
    void CreateGlobalsBuffers();
    /// @brief Create globals descriptor set layout, pool, and sets.
    bool CreateGlobalsDescriptorResources();
    /// @brief Destroy globals descriptor resources.
    void DestroyGlobalsDescriptorResources();
    /// @brief Push staged globals to GPU via vkCmdUpdateBuffer.
    void CmdUpdateGlobals(VkCommandBuffer cmdBuf);

    // ========================================================================
    // Instance Buffer (set 2, binding 1) — per-frame instanced transforms
    // ========================================================================
    struct InstanceBufferFrame
    {
        std::unique_ptr<vk::VkBufferHandle> buffer;
        VkDeviceSize capacity = 0; ///< Number of mat4 instances, not bytes
        void *mapped = nullptr;    ///< Persistently mapped CPU pointer for host-visible SSBO
    };
    std::vector<InstanceBufferFrame> m_instanceBuffers; ///< One per frame-in-flight
    static constexpr size_t INSTANCE_BUFFER_INITIAL_CAPACITY = 256;

    struct SkinBufferFrame
    {
        std::unique_ptr<vk::VkBufferHandle> buffer;
        VkDeviceSize capacity = 0; ///< Element count, not bytes
        void *mapped = nullptr;
    };
    std::vector<SkinBufferFrame> m_skinInstanceBuffers; ///< GPUSkinInstanceData per draw instance
    std::vector<SkinBufferFrame> m_skinPaletteBuffers;  ///< mat4 bone palettes per draw instance
    std::vector<SkinBufferFrame> m_instanceAuxBuffers;  ///< Optional GPUInstanceAuxData per draw instance
    static constexpr size_t SKIN_INSTANCE_BUFFER_INITIAL_CAPACITY = 256;
    static constexpr size_t SKIN_PALETTE_BUFFER_INITIAL_CAPACITY = 1024;
    static constexpr size_t INSTANCE_AUX_BUFFER_INITIAL_CAPACITY = 1;

    /// @brief Running write offset into the instance SSBO (reset per frame).
    uint32_t m_instanceWriteOffset = 0;
    uint32_t m_skinPaletteWriteOffset = 0;
    std::unordered_map<const void *, GPUSkinInstanceData> m_skinPaletteFrameCache;
    RenderInstanceHistory m_instanceHistory;
    /// @brief Frame counter for detecting new frames and resetting offset.
    uint64_t m_lastInstanceFrame = UINT64_MAX;

    /// @brief Ensure the current frame's instance buffer can hold at least \p instanceCount matrices.
    /// Preserves existing data when growing.
    void EnsureInstanceBufferCapacity(uint32_t frameIndex, size_t instanceCount);
    /// @brief Update the globals descriptor set binding 1 with the current frame's instance buffer.
    void UpdateInstanceBufferDescriptor(uint32_t frameIndex);
    void EnsureSkinBuffersCapacity(uint32_t frameIndex, size_t skinInstanceCount, size_t boneMatrixCount);
    void EnsureInstanceAuxBufferCapacity(uint32_t frameIndex, size_t instanceCount);
    void UpdateSkinBufferDescriptors(uint32_t frameIndex);
    void UpdateInstanceAuxBufferDescriptor(uint32_t frameIndex);
    void ResetPerFrameGpuStreamOffsets();

  public:
    /// @brief Pre-allocate the exact upper bound of instance stream entries for the current frame and
    /// update their descriptor bindings. Must be called before command-buffer
    /// recording begins.
    void PreallocateInstances(size_t requiredInstances);

    /// @brief Write a single instance matrix into the frame's instance SSBO.
    /// Grows the buffer and refreshes the descriptor if needed.
    [[nodiscard]] bool WriteInstanceMatrix(uint32_t frameIndex, uint32_t instanceIndex, const glm::mat4 &matrix);

    /// @brief Lazily prepare the optional picking/motion stream for one logical frame.
    void PrepareInstanceAuxiliary(uint64_t frameSerial, size_t totalInstances);
    [[nodiscard]] bool WriteInstanceAuxiliary(uint32_t frameIndex, uint32_t instanceIndex,
                                              const RenderDrawIdentity &identity, const glm::mat4 &currentModel,
                                              uint64_t objectId, uint32_t layerMask);
};

} // namespace infernux
