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
#include "FrameDeletionQueue.h"
#include "GpuResidency.h"
#include "InxRenderStruct.h"
#include "MaterialPipelineManager.h"
#include "ProfileConfig.h"
#include "RenderInstanceHistory.h"
#include "VkShaderCache.h"
#include "VkTextureCache.h"
#include "vk/VkCore.h"
#if INFERNUX_FRAME_PROFILE
#include "vk/GpuTimestampQueries.h"
#endif
#include <core/types/InxApplication.h>
#include <function/renderer/lighting/CanonicalLightGpuBuffer.h>
#include <function/scene/LightingData.h>

#include <functional>
#include <memory>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

struct SDL_Window;

namespace infernux
{

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
        m_shuttingDown = v;
        m_deviceContext.SetShuttingDown(v);
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
    void SetPresentMode(int mode)
    {
        static constexpr VkPresentModeKHR kModes[] = {
            VK_PRESENT_MODE_IMMEDIATE_KHR,
            VK_PRESENT_MODE_MAILBOX_KHR,
            VK_PRESENT_MODE_FIFO_KHR,
            VK_PRESENT_MODE_FIFO_RELAXED_KHR,
        };
        if (mode < 0 || mode > 3)
            return;
        m_swapchain.SetPreferredPresentMode(kModes[mode]);
        m_deviceContext.WaitIdle();
        DestroyGuiRenderGraphs();
        m_swapchain.Recreate(m_deviceContext, m_windowWidth, m_windowHeight);
        m_renderGraph.Initialize(&m_deviceContext, &m_pipelineManager);
    }

    /// @brief Get current present mode preference (0=IMMEDIATE,1=MAILBOX,2=FIFO,3=FIFO_RELAXED)
    [[nodiscard]] int GetPresentMode() const
    {
        switch (m_swapchain.GetPreferredPresentMode()) {
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
    void DrawSceneFiltered(VkCommandBuffer cmdBuf, uint32_t width, uint32_t height, int queueMin, int queueMax,
                           const std::string &sortMode = "", const std::string &overrideMaterial = "",
                           const std::string &passTag = "",
                           const MaterialPassPipelineDescriptor *pipelineDescriptor = nullptr);

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
        uint32_t idx = GetSwapchain().GetCurrentFrame() % static_cast<uint32_t>(m_globalsDescSets.size());
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
        snapshot.shadowDescriptorPoolCount = m_shadowMaterialDescPools.size();
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
        return m_deviceContext;
    }
    [[nodiscard]] const vk::VkDeviceContext &GetDeviceContext() const
    {
        return m_deviceContext;
    }

    [[nodiscard]] const rhi::DeviceCapabilities &GetRhiCapabilities() const noexcept
    {
        return m_deviceContext.GetCapabilities();
    }

    /**
     * @brief Get the swapchain manager
     */
    [[nodiscard]] vk::VkSwapchainManager &GetSwapchain()
    {
        return m_swapchain;
    }
    [[nodiscard]] const vk::VkSwapchainManager &GetSwapchain() const
    {
        return m_swapchain;
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
        return m_deviceContext.GetDevice();
    }
    [[nodiscard]] VkPhysicalDevice GetPhysicalDevice() const
    {
        return m_deviceContext.GetPhysicalDevice();
    }
    [[nodiscard]] VkInstance GetInstance() const
    {
        return m_deviceContext.GetInstance();
    }
    [[nodiscard]] VkQueue GetGraphicsQueue() const
    {
        return m_deviceContext.GetGraphicsQueue();
    }
    [[nodiscard]] VkQueue GetPresentQueue() const
    {
        return m_deviceContext.GetPresentQueue();
    }
    [[nodiscard]] uint32_t GetSwapchainImageCount() const
    {
        return m_swapchain.GetImageCount();
    }
    [[nodiscard]] VkCommandPool GetCommandPool() const
    {
        return m_resourceManager.GetCommandPool();
    }
    [[nodiscard]] VkFormat GetSwapchainFormat() const
    {
        return m_swapchain.GetImageFormat();
    }
    [[nodiscard]] VkExtent2D GetSwapchainExtent() const
    {
        return m_swapchain.GetExtent();
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
                                        const std::string &fragShaderName, VkBuffer sceneUbo, VkBuffer lightingUbo,
                                        bool reportDomainMismatch = true);

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

    /// @brief Re-initialize the material pipeline manager with a new MSAA sample count.
    /// Must be called after vkDeviceWaitIdle; destroys all cached pipelines.
    void ReinitializeMaterialPipelines(VkSampleCountFlagBits newSampleCount);

    // ========================================================================
    // Pre/Post-Scene-Render Callbacks
    // ========================================================================

    using PostSceneRenderCallback = std::function<void(VkCommandBuffer cmdBuf, const std::vector<DrawCall> &drawCalls)>;

    /// @brief Set callback invoked after scene rendering, before GUI.
    /// Used by InxRenderer to inject OutlineRenderer commands.
    void SetPostSceneRenderCallback(PostSceneRenderCallback callback)
    {
        m_postSceneRenderCallback = std::move(callback);
    }

    // ========================================================================
    // Buffer Accessors (for OutlineRenderer)
    // ========================================================================

    /// @brief Get per-object vertex buffer VkBuffer handle (VK_NULL_HANDLE if not found)
    [[nodiscard]] VkBuffer GetObjectVertexBuffer(uint64_t objectId) const;

    /// @brief Get per-object index buffer VkBuffer handle (VK_NULL_HANDLE if not found)
    [[nodiscard]] VkBuffer GetObjectIndexBuffer(uint64_t objectId) const;

    /// @brief Get the scene (view/projection) UBO VkBuffer (single buffer; all
    /// material descriptor sets bind it, updated in-command-buffer each frame).
    [[nodiscard]] VkBuffer GetSceneUbo() const;

    /// @brief Get the lighting UBO VkBuffer (single buffer, see GetSceneUbo).
    [[nodiscard]] VkBuffer GetLightingUbo() const;

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

    /// @brief Allocate a per-view descriptor set from the shared pool.
    /// Each SceneRenderGraph calls this once during initialization.
    [[nodiscard]] VkDescriptorSet AllocatePerViewDescriptorSet();

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

    /// @brief Set the active per-view descriptor set for subsequent draw calls.
    void SetActiveShadowDescriptorSet(VkDescriptorSet descSet)
    {
        m_activeShadowDescSet = descSet;
    }

    /// @brief Get the active per-view descriptor set (set 1) for draw calls.
    [[nodiscard]] VkDescriptorSet GetActiveShadowDescriptorSet() const
    {
        return m_activeShadowDescSet;
    }

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

    /// @brief Update lighting UBO for current frame
    void UpdateLightingUBO(const glm::vec3 &cameraPosition);

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
    void TickDeletionQueue();

    /// @brief Immediately flush all deferred deletions.
    ///
    /// Caller must ensure the device is idle before invoking this.
    void FlushDeletionQueue();

    /// @brief Enqueue a GPU resource for deferred deletion.
    ///
    /// The deleter lambda will be invoked after maxFramesInFlight frames,
    /// when all in-flight command buffers that might reference the resource
    /// have completed.
    void DeferDeletion(std::function<void()> deleter);

    [[nodiscard]] FrameDeletionQueue &GetDeletionQueue()
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

    /// @brief Inline-update the lighting UBO in a command buffer.
    ///
    /// Uses vkCmdUpdateBuffer with proper pipeline barriers so that all
    /// subsequent rendering commands see the updated lighting data.
    /// Called from RecordCommandBuffer() instead of CPU-side memcpy.
    void CmdUpdateLightingUBO(VkCommandBuffer cmdBuf);

    /// @brief Inline-update ONLY the cameraPos field of the lighting UBO.
    ///
    /// Used to override the camera position for per-view rendering (e.g.
    /// Game View uses the game camera while Scene View uses the editor camera)
    /// without re-uploading the full 6 KB lighting UBO.
    void CmdUpdateLightingCameraPos(VkCommandBuffer cmdBuf, const glm::vec3 &cameraPos);

    /// @brief Cache the lighting UBO data for inline command-buffer update.
    ///
    /// Called on the CPU timeline (before DrawFrame). The cached data is
    /// pushed to the GPU via CmdUpdateLightingUBO during command recording.
    void StageLightingUBO(const glm::vec3 &cameraPosition);

  private:
    // ========================================================================
    // Internal Methods
    // ========================================================================

    void RecreateSwapchain();
    void CreateDepthResources();

    void CreateUniformBuffers();
    vk::RenderGraph &GetGuiRenderGraph(uint32_t imageIndex);
    void DestroyGuiRenderGraphs();
    void RecordCommandBuffer(uint32_t imageIndex);
    void UpdateUniformBuffer(uint32_t currentImage, const float *viewPos, const float *viewLookAt, const float *viewUp);

    /// @brief Update the VP UBO inline in a command buffer (for multi-camera rendering).
    /// Uses vkCmdUpdateBuffer with proper barriers so each render graph sees its own VP matrices.
    void CmdUpdateUniformBuffer(VkCommandBuffer cmdBuf, const glm::mat4 &view, const glm::mat4 &proj,
                                const glm::mat4 *previousViewProj = nullptr);

    /// @brief Create a raw Vulkan buffer via VMA
    void CreateBuffer(VkDeviceSize size, VkBufferUsageFlags usage, VkMemoryPropertyFlags properties, VkBuffer &buffer,
                      VmaAllocation &allocation);

    // ========================================================================
    // New Modular Components
    // ========================================================================

    vk::VkDeviceContext m_deviceContext;
    vk::VkSwapchainManager m_swapchain;
    vk::VkPipelineManager m_pipelineManager;
    vk::VkResourceManager m_resourceManager;
    vk::AsyncTransferContext m_asyncTransferContext;
    vk::AsyncTransferContext m_asyncReadbackContext;
    vk::RenderGraph m_renderGraph;
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
    // Record breakdown: [4] UBO  [5] SceneGraph  [6] PostScene  [7] GUIGraph
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

    // Command buffers
    std::vector<VkCommandBuffer> m_commandBuffers;

    // Scene UBO (binding 0). Single buffer by design: every material
    // descriptor set binds this one buffer, and per-frame updates happen
    // inline in the command buffer via CmdUpdateUniformBuffer(), so
    // per-frame-in-flight copies would never be read and only obscured the
    // actual synchronization model.
    std::unique_ptr<vk::VkBufferHandle> m_sceneUbo;

    // Default material UBO (binding 2) — single buffer, persistently mapped.
    std::unique_ptr<vk::VkBufferHandle> m_materialUbo;
    void *m_materialUboMapped = nullptr;

    // Lighting UBO (binding 1) — single buffer, persistently mapped.
    std::unique_ptr<vk::VkBufferHandle> m_lightingUbo;
    void *m_lightingUboMapped = nullptr;

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
    std::function<void(vk::RenderContext &ctx)> m_guiRenderCallback;

    // Unity-style draw calls for multi-material rendering (pointer to external storage, no copy)
    const std::vector<DrawCall> *m_drawCallsPtr = nullptr;
    const std::vector<DrawCall> *m_shadowDrawCallsPtr = nullptr;
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
        VkBuffer vertexBuf;
        const std::shared_ptr<InxMaterial> *materialOwner;
        InxMaterial *material; // Resolved once in the filter loop; materialOwner keeps it alive.
        std::unordered_map<uint64_t, PerObjectBuffers>::const_iterator bufIt;
    };
    std::vector<SortableDrawCall> m_eligibleScratch;

    // Cached builtin material lookups (refreshed per SetDrawCalls)
    std::shared_ptr<InxMaterial> m_cachedDefaultLit;
    std::shared_ptr<InxMaterial> m_cachedErrorMat;

    struct ShadowDraw
    {
        const DrawCall *dc;
        std::unordered_map<uint64_t, PerObjectBuffers>::const_iterator bufIt;
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

    // Pre/Post scene render callbacks
    PostSceneRenderCallback m_postSceneRenderCallback;

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

        VkDescriptorPool descriptorPool = VK_NULL_HANDLE;
        std::vector<VkDescriptorSet> descriptorSets;
        std::vector<VkBuffer> uniformBuffers;
        std::vector<VmaAllocation> allocations;
        std::vector<void *> mappedPointers;
        VkDescriptorPool streamDescriptorPool = VK_NULL_HANDLE;
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
    static constexpr uint32_t kShadowMaterialPoolPageSize = 256;

    struct ShadowMaterialBindingEntry
    {
        std::weak_ptr<InxMaterial> owner;
        std::string materialKey;
        uint64_t materialVersion = 0;
        uint64_t artifactRevision = 0;
        size_t resourceSignature = 0;
        VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
        VkDescriptorPool ownerPool = VK_NULL_HANDLE;
        std::vector<std::shared_ptr<rhi::TextureResource>> textureKeepAlive;
    };

    struct ShadowDescriptorAllocation
    {
        VkDescriptorSet descriptorSet = VK_NULL_HANDLE;
        VkDescriptorPool ownerPool = VK_NULL_HANDLE;
    };

    std::vector<VkDescriptorPool> m_shadowMaterialDescPools;
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
    [[nodiscard]] VkDescriptorPool CreateShadowMaterialDescriptorPoolPage(uint32_t maxSets);
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
    VkDescriptorPool m_perViewDescPool = VK_NULL_HANDLE;
    /// Valid dummy bindings for layout set 2 (never freed per material).
    VkDescriptorSet m_shadowMaterialDummyDescSet = VK_NULL_HANDLE;
    VkDescriptorSet m_activeShadowDescSet = VK_NULL_HANDLE; ///< Currently active per-view desc for draw calls

    /// @brief Create per-view descriptor set layout and pool.
    bool CreatePerViewDescriptorResources();
    /// @brief Destroy per-view descriptor set layout and pool.
    void DestroyPerViewDescriptorResources();

    // ========================================================================
    // Frame-safe deferred deletion queue
    // ========================================================================
    FrameDeletionQueue m_deletionQueue;

    // ========================================================================
    // Staged UBO data (CPU-side cache → GPU via vkCmdUpdateBuffer)
    // ========================================================================
    ShaderLightingUBO m_stagedLightingUBO{};
    bool m_lightingUBODirty = false;

    UniformBufferObject m_stagedUBO{};
    bool m_uboDirty = false;

    // ========================================================================
    // Engine Globals UBO (set 2, binding 0) — per-frame time/screen/camera
    // ========================================================================
    std::vector<std::unique_ptr<vk::VkBufferHandle>> m_globalsBuffers;
    VkDescriptorSetLayout m_globalsDescSetLayout = VK_NULL_HANDLE;
    VkDescriptorPool m_globalsDescPool = VK_NULL_HANDLE;
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
