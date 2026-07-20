/**
 * @file InxVkCoreModular.cpp
 * @brief Implementation of the modular Vulkan core — init, lifecycle, texture/shader, internal
 *
 * Drawing methods → VkCoreDraw.cpp
 * Material/lighting/accessors → VkCoreMaterial.cpp
 */

#include "InxVkCoreModular.h"
#include "InxError.h"
#include "ProfileConfig.h"
#include "SceneRenderTarget.h"
#include "gui/GPUMaterialPreview.h"
#include "gui/GPUMeshPreview.h"

#include <function/renderer/shader/ShaderProgram.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxMaterial/InxMaterial.h>
#include <function/scene/SceneRenderer.h>

#include <glm/glm.hpp>
#include <glm/gtc/matrix_transform.hpp>

#include <algorithm>
#include <chrono>
#include <cstring>
#include <limits>
#include <unordered_set>

namespace infernux
{

namespace
{

void DestroyLingeringMaterialPassPipelines(VkDevice device)
{
    if (device == VK_NULL_HANDLE) {
        return;
    }

    auto &assetRegistry = AssetRegistry::Instance();
    if (!assetRegistry.IsInitialized()) {
        return;
    }

    std::unordered_set<VkPipeline> destroyedPipelines;
    size_t destroyedCount = 0;

    for (const auto &material : assetRegistry.GetAllMaterials()) {
        if (!material) {
            continue;
        }

        for (int passIndex = 0; passIndex < static_cast<int>(ShaderCompileTarget::Count); ++passIndex) {
            const auto pass = static_cast<ShaderCompileTarget>(passIndex);
            // Shadow pipelines are owned by m_shadowPipelineCache and
            // destroyed in CleanupShadowPipeline() — skip here.
            if (pass == ShaderCompileTarget::Shadow) {
                material->ClearPassPipeline(pass);
                continue;
            }
            const VkPipeline pipeline = material->GetPassPipeline(pass);
            if (pipeline != VK_NULL_HANDLE) {
                if (destroyedPipelines.insert(pipeline).second) {
                    vkDestroyPipeline(device, pipeline, nullptr);
                    ++destroyedCount;
                }
            }

            material->ClearPassPipeline(pass);
        }
    }

    if (destroyedCount > 0) {
        INXLOG_WARN("InxVkCoreModular shutdown: destroyed ", destroyedCount,
                    " lingering material pass pipeline(s) outside MaterialPipelineManager ownership");
    }
}

} // namespace

// ============================================================================
// Constructor / Destructor
// ============================================================================

InxVkCoreModular::InxVkCoreModular(int maxFrameInFlight) : m_maxFramesInFlight(static_cast<uint32_t>(maxFrameInFlight))
{
    m_deletionQueue.Initialize(m_maxFramesInFlight);
}

InxVkCoreModular::~InxVkCoreModular()
{
    if (m_deviceContext.IsValid() && !m_shuttingDown) {
        m_deviceContext.WaitIdle();
    }

    // Async preview submissions retain transient buffers and cloned materials.
    // Release those leases while every renderer subsystem and the device are
    // still alive, then destroy both previewers in the controlled order below.
    m_resourceManager.DrainAsyncGraphicsSubmissions();

    // Flush all deferred deletions before tearing down subsystems
    m_deletionQueue.FlushAll();

    if (m_materialPipelineManagerInitialized) {
        m_materialPipelineManager.Shutdown(m_shuttingDown);
        m_materialPipelineManagerInitialized = false;
    }

    // Some material-owned auxiliary pass pipelines (typically shadow variants)
    // can outlive MaterialPipelineManager bookkeeping after invalidation or
    // hot-reload paths. Retire any pass handles still attached to live
    // materials before the device goes away.
    DestroyLingeringMaterialPassPipelines(GetDevice());

    // Cleanup shaders via VkShaderCache → VkPipelineManager.DestroyShaderModule
    // which also removes handles from tracking, preventing double-free.
    m_shaderCache.DestroyModules(m_pipelineManager);

    // During shutdown the device is already idle (drained once by ~InxRenderer).
    // Tell RAII members to skip their own vkDeviceWaitIdle calls.
    if (m_shuttingDown) {
        m_resourceManager.SetSkipWaitIdle(true);
        m_pipelineManager.SetSkipWaitIdle(true);
        m_swapchain.SetSkipWaitIdle(true);
    }

    // Cleanup shadow pipeline resources before general cleanup
    CleanupShadowPipeline();

    // Cleanup per-view descriptor resources (multi-camera shadow)
    DestroyPerViewDescriptorResources();

    // Cleanup engine globals descriptor resources
    DestroyGlobalsDescriptorResources();

    // Explicit destruction in controlled order (avoids double-free from
    // RAII reverse-declaration order when handles are shared across systems).
    m_perObjectBuffers.clear();
    m_sharedMeshBuffers.clear();
    m_pendingSharedMeshBuffers.clear();
    m_pendingTextureCpuLoads.clear();
    m_pendingTextureGpuUploads.clear();
    m_textureCache.Clear();
    m_shaderCache.Clear();

    m_lightingUbo.reset();
    m_lightingUboMapped = nullptr;
    m_materialUbo.reset();
    m_materialUboMapped = nullptr;
    m_sceneUbo.reset();
    m_globalsBuffers.clear();
    m_gpuMeshPreview.reset();
    m_gpuMaterialPreview.reset();

    m_commandBuffers.clear();
    m_depthImage.reset();

#if INFERNUX_FRAME_PROFILE
    m_gpuTimestampQueries.Destroy();
#endif
    DestroyGuiRenderGraphs();
    m_resourceManager.Destroy();
    m_asyncReadbackContext.Destroy();
    m_asyncTransferContext.Destroy();

    // RenderGraph::Destroy() and MaterialPipelineManager::Shutdown()
    // already destroyed render passes, layouts, and descriptor set
    // layouts that VkPipelineManager may have tracked. Keep pipeline
    // tracking alive so any leftovers still get reclaimed here.
    m_pipelineManager.ClearTrackedNonPipelineResources();
    m_pipelineManager.Destroy();

    m_swapchain.Destroy();

    // Some shutdown paths defer frees into the frame deletion queue. Flush one
    // final time after all subsystems have torn down but before destroying the
    // allocator/device.
    m_deletionQueue.FlushAll();

    m_deviceContext.Destroy();

    // All RAII destructors that fire after this point will find VK_NULL_HANDLE
    // in their stored device and skip Vulkan calls.
}

// ============================================================================
// Initialization
// ============================================================================

bool InxVkCoreModular::Init(InxAppMetadata appMetaData, InxAppMetadata rendererMetaData, uint32_t vkWindowExtCount,
                            const char **vkWindowExts)
{
    INXLOG_INFO("Initializing InxVkCoreModular...");

    // Configure device (store for use in PrepareSurface)
    m_deviceConfig.appName = appMetaData.appName ? appMetaData.appName : "Infernux App";
    m_deviceConfig.engineName = rendererMetaData.appName ? rendererMetaData.appName : "Infernux";

#if defined(INFERNUX_VULKAN_VALIDATION_LAYERS)
    m_deviceConfig.enableValidationLayers = true;
#endif

    // Initialize instance only (device will be created in PrepareSurface after surface is available)
    if (!m_deviceContext.InitializeInstance(m_deviceConfig)) {
        INXLOG_ERROR("Failed to initialize Vulkan instance");
        return false;
    }

    // Store instance for InxRenderer access
    m_instance = m_deviceContext.GetInstance();

    INXLOG_INFO("InxVkCoreModular instance initialized successfully");
    return true;
}

bool InxVkCoreModular::PrepareSurface()
{
    // Surface should be set by InxRenderer before calling this
    if (m_surface == VK_NULL_HANDLE) {
        INXLOG_ERROR("Surface not set. Call CreateSurface first.");
        return false;
    }

    // Complete device initialization with the surface
    if (!m_deviceContext.InitializeDevice(m_surface, m_deviceConfig)) {
        INXLOG_ERROR("Failed to initialize Vulkan device");
        return false;
    }

    // Now that device is ready, initialize resource manager
    if (!m_resourceManager.Initialize(m_deviceContext)) {
        INXLOG_ERROR("Failed to initialize resource manager");
        return false;
    }

    // Initialize the async-transfer context. On GPUs without a dedicated
    // transfer queue this aliases to the graphics queue and behaves like
    // a pooled-fence fast path; on GPUs with one (most discrete cards) it
    // unlocks truly parallel asset uploads. Failures are non-fatal — the
    // engine simply keeps using the synchronous VkResourceManager path.
    const auto &queueIndices = m_deviceContext.GetQueueIndices();
    const uint32_t graphicsFamily = queueIndices.graphicsFamily.value_or(0);
    const uint32_t transferFamily = queueIndices.transferFamily.value_or(graphicsFamily);
    if (m_asyncTransferContext.Initialize(
            m_deviceContext.GetDevice(), transferFamily, m_deviceContext.GetTransferQueue(),
            m_deviceContext.HasDedicatedTransferQueue(), m_deviceContext.IsTimelineSemaphoreEnabled())) {
        // Plug the async context into the resource manager so non-mipmap
        // texture uploads route through the dedicated DMA queue. Mipmap
        // generation still falls back to the graphics queue because
        // vkCmdBlitImage is not legal on transfer-only queues.
        m_resourceManager.SetAsyncTransferContext(&m_asyncTransferContext, graphicsFamily);
    } else {
        INXLOG_WARN("Async transfer context unavailable; uploads will use the graphics queue.");
    }

    if (m_asyncReadbackContext.Initialize(m_deviceContext.GetDevice(), graphicsFamily,
                                          m_deviceContext.GetGraphicsQueue(), false)) {
        m_resourceManager.SetAsyncReadbackContext(&m_asyncReadbackContext);
    } else {
        INXLOG_WARN("Async graphics readback context unavailable.");
    }

    // Initialize pipeline manager
    m_pipelineManager.Initialize(m_deviceContext.GetDevice());

    // Initialize render graph
    m_renderGraph.Initialize(&m_deviceContext, &m_pipelineManager);
#if INFERNUX_FRAME_PROFILE
    if (!m_gpuTimestampQueries.Initialize(m_deviceContext, m_maxFramesInFlight)) {
        INXLOG_WARN("GPU timestamp queries are unavailable on this device");
    }
#endif

    // Get extent from surface capabilities
    auto swapchainSupport = m_deviceContext.QuerySwapchainSupport();
    uint32_t width = swapchainSupport.capabilities.currentExtent.width;
    uint32_t height = swapchainSupport.capabilities.currentExtent.height;

    if (width == std::numeric_limits<uint32_t>::max() || height == std::numeric_limits<uint32_t>::max() || width == 0 ||
        height == 0) {
        width = (m_windowWidth > 0) ? m_windowWidth : swapchainSupport.capabilities.minImageExtent.width;
        height = (m_windowHeight > 0) ? m_windowHeight : swapchainSupport.capabilities.minImageExtent.height;

        width = std::clamp(width, swapchainSupport.capabilities.minImageExtent.width,
                           swapchainSupport.capabilities.maxImageExtent.width);
        height = std::clamp(height, swapchainSupport.capabilities.minImageExtent.height,
                            swapchainSupport.capabilities.maxImageExtent.height);
    }

    // Create swapchain
    if (!m_swapchain.Create(m_deviceContext, width, height)) {
        INXLOG_ERROR("Failed to create swapchain");
        return false;
    }

    // Create depth resources
    CreateDepthResources();

    // Create uniform buffers
    CreateUniformBuffers();

    // Allocate command buffers
    m_commandBuffers.resize(m_maxFramesInFlight);
    for (uint32_t i = 0; i < m_maxFramesInFlight; ++i) {
        auto alloc = m_resourceManager.AllocatePrimaryCommandBuffer();
        m_commandBuffers[i] = alloc.cmdBuffer;
    }

    INXLOG_INFO("Surface prepared successfully");
    return true;
}

void InxVkCoreModular::PreparePipeline()
{
    // Create default white texture
    m_textureCache.CreateDefaultWhiteTexture("white", m_resourceManager);

    // Create default flat normal texture (0.5, 0.5, 1.0 = tangent-space (0,0,1))
    m_textureCache.CreateSolidColorTexture("_default_normal", 128, 128, 255, 255, m_resourceManager);
    INXLOG_INFO("Created default flat normal texture: _default_normal");

    // Initialize material system (default material + pipelines)
    InitializeMaterialSystem();

    // Create per-view descriptor set layout and pool (multi-camera shadow isolation).
    // Must be after InitializeMaterialSystem so default textures are available.
    CreatePerViewDescriptorResources();

    // Create shadow depth sampler eagerly so that it is available when
    // RefreshPerViewShadowDescriptor runs (before the first DrawShadowCasters).
    if (m_shadowDepthSampler == VK_NULL_HANDLE) {
        CreateShadowDepthSampler();
    }

    INXLOG_INFO("Pipeline prepared successfully");
}

// ============================================================================
// Texture Management (delegates to VkTextureCache)
// ============================================================================

void InxVkCoreModular::CreateDefaultWhiteTexture(std::string name)
{
    m_textureCache.CreateDefaultWhiteTexture(name, m_resourceManager);
}

// ============================================================================
// Shader and Pipeline Management (delegates to VkShaderCache)
// ============================================================================

void InxVkCoreModular::LoadShader(const char *name, const std::vector<char> &spirvCode, const char *type)
{
    m_shaderCache.LoadShader(name, spirvCode, type, m_pipelineManager);
}

bool InxVkCoreModular::PublishShaderProgramArtifact(const ShaderProgramArtifact &artifact)
{
    const auto publish = m_shaderCache.PublishProgramArtifact(artifact);
    if (!publish.accepted)
        return false;
    if (!publish.changed)
        return true;

    if (m_materialPipelineManagerInitialized)
        m_materialPipelineManager.InvalidateMaterialsUsingProgramPair(artifact.key.stages);

    // Linked shadow pipelines are keyed by the stable stage pair plus artifact
    // revision. Retire every older revision for this pair before its owning
    // ShaderProgram modules leave the cache.
    const std::string shadowProgramPrefix = artifact.key.stages.ToString() + "|";
    const VkDevice device = GetDevice();
    for (auto it = m_shadowPipelineCache.begin(); it != m_shadowPipelineCache.end();) {
        if (it->first.rfind(shadowProgramPrefix, 0) != 0) {
            ++it;
            continue;
        }
        const VkPipeline pipeline = it->second;
        if (pipeline != VK_NULL_HANDLE)
            m_deletionQueue.Push([device, pipeline] { vkDestroyPipeline(device, pipeline, nullptr); });
        ++m_shaderHotReloadRetirementCount;
        it = m_shadowPipelineCache.erase(it);
    }

    if (publish.replacedProgram) {
        auto previousPrograms = m_shaderCache.GetProgramCache().TakePrograms(*publish.replacedProgram);
        for (auto &previous : previousPrograms) {
            auto retired = std::shared_ptr<ShaderProgram>(std::move(previous));
            m_deletionQueue.Push([retired = std::move(retired)]() mutable { retired.reset(); });
            ++m_shaderHotReloadRetirementCount;
        }
    }

    INXLOG_INFO("Published shader program artifact '", artifact.key.ToString(), "'");
    return true;
}

bool InxVkCoreModular::HasShaderProgramArtifact(const ShaderProgramKey &programKey) const
{
    const auto *artifact = m_shaderCache.FindProgramArtifact(programKey.stages);
    return artifact && artifact->key == programKey;
}

std::shared_ptr<const ShaderProgramArtifact>
InxVkCoreModular::CopyShaderProgramArtifact(const ShaderStagePair &stages) const
{
    const auto *artifact = m_shaderCache.FindProgramArtifact(stages);
    return artifact ? std::make_shared<const ShaderProgramArtifact>(*artifact) : nullptr;
}

void InxVkCoreModular::StoreShaderRenderMeta(const std::string &shaderId, const std::string &cullMode,
                                             const std::string &depthWrite, const std::string &depthTest,
                                             const std::string &blend, int queue, const std::string &passTag,
                                             const std::string &stencil, const std::string &alphaClip)
{
    m_shaderCache.StoreRenderMeta(shaderId, cullMode, depthWrite, depthTest, blend, queue, passTag, stencil, alphaClip);
}

void InxVkCoreModular::UnloadShader(const char *name)
{
    m_shaderCache.UnloadShader(name, GetDevice());
}

bool InxVkCoreModular::HasShader(const std::string &name, const std::string &type) const
{
    return m_shaderCache.HasShader(name, type);
}

void InxVkCoreModular::InvalidateShaderCache(const std::string &shaderId)
{
    if (shaderId.empty())
        throw std::invalid_argument("Shader cache invalidation requires a non-empty shader identifier");
    INXLOG_INFO("Invalidating shader cache for: ", shaderId);

    // Clear every CPU-visible raw ShaderProgram/pipeline handle before moving
    // the owning Vulkan objects into the frame-safe retirement queue.
    if (m_materialPipelineManagerInitialized) {
        m_materialPipelineManager.InvalidateMaterialsUsingShader(shaderId);
    }

    auto retiredPrograms = m_shaderCache.GetProgramCache().TakeProgramsContainingShader(shaderId);
    for (auto &program : retiredPrograms) {
        auto retired = std::shared_ptr<ShaderProgram>(std::move(program));
        m_deletionQueue.Push([retired = std::move(retired)]() mutable { retired.reset(); });
        ++m_shaderHotReloadRetirementCount;
    }

    const VkDevice device = GetDevice();
    auto matchesShadowStage = [&](const std::string &stage) {
        return stage == shaderId || stage.rfind(shaderId + "/", 0) == 0;
    };
    for (auto it = m_shadowPipelineCache.begin(); it != m_shadowPipelineCache.end();) {
        const size_t firstPipe = it->first.find('|');
        const size_t secondPipe =
            firstPipe == std::string::npos ? std::string::npos : it->first.find('|', firstPipe + 1);
        const std::string vertStage = firstPipe == std::string::npos ? it->first : it->first.substr(0, firstPipe);
        const std::string fragStage = firstPipe == std::string::npos
                                          ? std::string{}
                                          : it->first.substr(firstPipe + 1, secondPipe - firstPipe - 1);
        if (!matchesShadowStage(vertStage) && !matchesShadowStage(fragStage)) {
            ++it;
            continue;
        }
        const VkPipeline pipeline = it->second;
        if (pipeline != VK_NULL_HANDLE)
            m_deletionQueue.Push([device, pipeline] { vkDestroyPipeline(device, pipeline, nullptr); });
        ++m_shaderHotReloadRetirementCount;
        it = m_shadowPipelineCache.erase(it);
    }

    // Shader modules are only consumed during pipeline creation, so their
    // standalone cache entry can be destroyed immediately on the owner thread.
    m_shaderCache.UnloadShader(shaderId.c_str(), GetDevice());

    INXLOG_INFO("Shader cache invalidated for: ", shaderId);
}

void InxVkCoreModular::InvalidateTextureCache(const std::string &textureGuid)
{
    // GUID-only contract: cache keys and material texture properties are
    // GUID-based ("GUID::srgb::…"). Path→GUID resolution happens at the
    // engine boundary (Infernux::ReloadTexture / material setters), never here.
    if (textureGuid.empty()) {
        return;
    }

#ifndef NDEBUG
    // Catch contract violations early in debug builds: a path slipping in
    // means some caller skipped boundary normalization.
    if (textureGuid.find('/') != std::string::npos || textureGuid.find('\\') != std::string::npos) {
        INXLOG_WARN("InvalidateTextureCache: received a path-like identifier '", textureGuid,
                    "' — callers must resolve to a GUID first.");
    }
#endif

    INXLOG_INFO("Invalidating texture cache for GUID: ", textureGuid);

    m_pendingTextureCpuLoads.erase(textureGuid);
    for (auto pending = m_pendingTextureGpuUploads.begin(); pending != m_pendingTextureGpuUploads.end();) {
        if (pending->second.guid != textureGuid) {
            ++pending;
            continue;
        }
        try {
            (void)m_resourceManager.TryPublishTextureUpload(pending->second.ticket);
        } catch (const std::exception &exception) {
            INXLOG_ERROR("Failed to retire invalidated texture upload for GUID '", textureGuid,
                         "': ", exception.what());
        }
        pending = m_pendingTextureGpuUploads.erase(pending);
    }

    // Runtime materials such as SpriteRenderer instances are not represented
    // in the asset dependency graph. Retiring their descriptor sets first keeps
    // the texture lease alive until the frame deletion queue releases it.
    if (m_materialPipelineManagerInitialized) {
        m_materialPipelineManager.InvalidateMaterialsUsingTexture(textureGuid);
    }

    // Evict all cached variants for this GUID
    size_t evicted = m_textureCache.EvictByPrefix(textureGuid);
    (void)evicted;

    INXLOG_INFO("Texture cache invalidated for GUID: ", textureGuid);
}

void InxVkCoreModular::RemoveMaterialPipeline(const std::string &materialName)
{
    if (m_materialPipelineManagerInitialized) {
        m_materialPipelineManager.RemoveRenderData(materialName);
        INXLOG_INFO("Removed material pipeline render data for: ", materialName);
    }
}

// ============================================================================
// Command Buffer Utilities
// ============================================================================

VkCommandBuffer InxVkCoreModular::BeginSingleTimeCommands()
{
    return m_resourceManager.BeginSingleTimeCommands();
}

void InxVkCoreModular::EndSingleTimeCommands(VkCommandBuffer commandBuffer)
{
    m_resourceManager.EndSingleTimeCommands(commandBuffer);
}

// ============================================================================
// Render Callbacks (RenderGraph-based)
// ============================================================================

void InxVkCoreModular::SetRenderGraphExecutor(std::function<void(VkCommandBuffer cmdBuf)> executor)
{
    m_renderGraphExecutor = std::move(executor);
}

void InxVkCoreModular::SetGuiRenderCallback(std::function<void(vk::RenderContext &ctx)> callback)
{
    m_guiRenderCallback = std::move(callback);
}

// ============================================================================
// Internal Methods
// ============================================================================

void InxVkCoreModular::RecreateSwapchain()
{
    // Wait for device idle
    m_deviceContext.WaitIdle();

    // GUI graphs retain framebuffers and RHI aliases for individual
    // swapchain images. Retire them before those views are destroyed.
    DestroyGuiRenderGraphs();

    // Cleanup old depth resources
    m_depthImage.reset();

    // Get new extent from surface capabilities
    auto swapchainSupport = m_deviceContext.QuerySwapchainSupport();
    uint32_t width = swapchainSupport.capabilities.currentExtent.width;
    uint32_t height = swapchainSupport.capabilities.currentExtent.height;

    if (width == std::numeric_limits<uint32_t>::max() || height == std::numeric_limits<uint32_t>::max() || width == 0 ||
        height == 0) {
        width = (m_windowWidth > 0) ? m_windowWidth : swapchainSupport.capabilities.minImageExtent.width;
        height = (m_windowHeight > 0) ? m_windowHeight : swapchainSupport.capabilities.minImageExtent.height;

        width = std::clamp(width, swapchainSupport.capabilities.minImageExtent.width,
                           swapchainSupport.capabilities.maxImageExtent.width);
        height = std::clamp(height, swapchainSupport.capabilities.minImageExtent.height,
                            swapchainSupport.capabilities.maxImageExtent.height);
    }

    // Handle special case (some window managers report invalid extents)
    if (width == 0 || height == 0) {
        // Cannot recreate, wait for valid extent
        return;
    }

    // Recreate swapchain
    m_swapchain.Recreate(m_deviceContext, width, height);
    m_renderGraph.Initialize(&m_deviceContext, &m_pipelineManager);

    // Recreate depth resources
    CreateDepthResources();
}

vk::RenderGraph &InxVkCoreModular::GetGuiRenderGraph(uint32_t imageIndex)
{
    if (imageIndex == 0)
        return m_renderGraph;

    const size_t additionalIndex = static_cast<size_t>(imageIndex - 1);
    if (m_additionalGuiRenderGraphs.size() <= additionalIndex)
        m_additionalGuiRenderGraphs.resize(additionalIndex + 1);

    auto &graph = m_additionalGuiRenderGraphs[additionalIndex];
    if (!graph) {
        graph = std::make_unique<vk::RenderGraph>();
        graph->Initialize(&m_deviceContext, &m_pipelineManager);
    }
    return *graph;
}

void InxVkCoreModular::DestroyGuiRenderGraphs()
{
    for (auto &graph : m_additionalGuiRenderGraphs) {
        if (graph)
            graph->Destroy();
    }
    m_additionalGuiRenderGraphs.clear();
    m_guiRenderGraphReady.clear();
    m_renderGraph.Destroy();
}

void InxVkCoreModular::CreateDepthResources()
{
    VkExtent2D extent = m_swapchain.GetExtent();
    VkFormat depthFormat = m_deviceContext.FindDepthFormat();

    m_depthImage = m_resourceManager.CreateDepthBuffer(extent.width, extent.height, depthFormat);
}

void InxVkCoreModular::CreateUniformBuffers()
{
    // Scene UBO (binding 0). Single buffer: all material descriptor sets
    // reference it, and updates are recorded inline in the command buffer
    // (CmdUpdateUniformBuffer) so the GPU never races a CPU write.
    m_sceneUbo = m_resourceManager.CreateUniformBuffer(sizeof(UniformBufferObject));

    // Lighting UBO (binding 1) — single buffer, persistently mapped.
    VkDeviceSize lightingUboSize = sizeof(ShaderLightingUBO);
    m_lightingUbo = m_resourceManager.CreateUniformBuffer(lightingUboSize);
    m_lightingUboMapped = nullptr;
    if (m_lightingUbo) {
        m_lightingUboMapped = m_lightingUbo->Map(0, lightingUboSize);
        if (m_lightingUboMapped) {
            // Initialize with default ambient lighting (neutral white)
            ShaderLightingUBO defaultLighting{};
            defaultLighting.lightCounts = glm::ivec4(0, 0, 0, 0);
            defaultLighting.ambientColor = glm::vec4(1.0f, 1.0f, 1.0f, 0.3f); // White ambient, low intensity
            defaultLighting.cameraPos = glm::vec4(0.0f, 0.0f, 5.0f, 1.0f);
            std::memcpy(m_lightingUboMapped, &defaultLighting, lightingUboSize);
        }
    }
    INXLOG_INFO("Created lighting UBO: ", lightingUboSize, " bytes (single buffer)");

    // Default material UBO (binding 2) — single buffer, persistently mapped.
    // 256 bytes is a safe default for the fallback material UBO; per-material
    // UBOs use reflection-derived sizes via MaterialDescriptorManager.
    constexpr size_t materialUboSize = 256;
    m_materialUbo = m_resourceManager.CreateUniformBuffer(materialUboSize);
    m_materialUboMapped = nullptr;
    if (m_materialUbo) {
        m_materialUboMapped = m_materialUbo->Map(0, materialUboSize);
        if (m_materialUboMapped) {
            std::memset(m_materialUboMapped, 0, materialUboSize);
        }
    }

    // Create engine globals UBO buffers (set 2, binding 0)
    CreateGlobalsBuffers();
    CreateGlobalsDescriptorResources();
}

void InxVkCoreModular::RecordCommandBuffer(uint32_t imageIndex)
{
#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    auto _tPrev = Clock::now();
    auto _tNow = _tPrev;
#endif

    VkCommandBuffer cmdBuf = m_commandBuffers[m_currentFrame];

    VkCommandBufferBeginInfo beginInfo{};
    beginInfo.sType = VK_STRUCTURE_TYPE_COMMAND_BUFFER_BEGIN_INFO;

    if (vkBeginCommandBuffer(cmdBuf, &beginInfo) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to begin recording command buffer");
        return;
    }

#if INFERNUX_FRAME_PROFILE
    m_gpuTimestampQueries.BeginFrame(cmdBuf, m_currentFrame);
    const auto gpuFrameRegion = m_gpuTimestampQueries.BeginRegion(cmdBuf, "Frame", VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT);
#endif

    // ========================================================================
    // Inline UBO Updates (Fix 1: replaces CPU-side memcpy)
    // ========================================================================
    if (m_uboDirty) {
        CmdUpdateUniformBuffer(cmdBuf, m_stagedUBO.view, m_stagedUBO.proj);
        m_uboDirty = false;
    }
    CmdUpdateLightingUBO(cmdBuf);
    CmdUpdateShadowUBO(cmdBuf);
    CmdUpdateGlobals(cmdBuf);
#if INFERNUX_FRAME_PROFILE
    _tNow = Clock::now();
    m_drawSubMs[4] += std::chrono::duration<double, std::milli>(_tNow - _tPrev).count();
    _tPrev = _tNow;
#endif

    // ========================================================================
    // Execute scene render graph (offscreen scene rendering)
    // ========================================================================
#if INFERNUX_FRAME_PROFILE
    const auto gpuSceneRegion =
        m_gpuTimestampQueries.BeginRegion(cmdBuf, "SceneRenderGraph", VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT);
#endif
    if (m_renderGraphExecutor) {
        m_renderGraphExecutor(cmdBuf);
    }
#if INFERNUX_FRAME_PROFILE
    m_gpuTimestampQueries.EndRegion(cmdBuf, gpuSceneRegion, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT);
#endif
#if INFERNUX_FRAME_PROFILE
    _tNow = Clock::now();
    m_drawSubMs[5] += std::chrono::duration<double, std::milli>(_tNow - _tPrev).count();
    _tPrev = _tNow;
#endif

    // ========================================================================
    // Post-Scene-Render Callback (OutlineRenderer injection point)
    // ========================================================================
#if INFERNUX_FRAME_PROFILE
    const auto gpuPostSceneRegion =
        m_gpuTimestampQueries.BeginRegion(cmdBuf, "PostScene", VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT);
#endif
    if (m_postSceneRenderCallback) {
        m_postSceneRenderCallback(cmdBuf, drawCalls());
    }
#if INFERNUX_FRAME_PROFILE
    m_gpuTimestampQueries.EndRegion(cmdBuf, gpuPostSceneRegion, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT);
#endif
#if INFERNUX_FRAME_PROFILE
    _tNow = Clock::now();
    m_drawSubMs[6] += std::chrono::duration<double, std::milli>(_tNow - _tPrev).count();
    _tPrev = _tNow;
#endif

    // ========================================================================
    // Swapchain GUI Pass via RenderGraph. Each swapchain image owns one
    // persistent compiled graph because its VkImageView/framebuffer is stable
    // until swapchain recreation. Rebuilding this one-pass graph every frame
    // used to repeat RHI registration, graph compilation and framebuffer
    // lookup on the hottest render path.
    // ========================================================================
    vk::RenderGraph &guiGraph = GetGuiRenderGraph(imageIndex);
    if (m_guiRenderGraphReady.size() <= imageIndex)
        m_guiRenderGraphReady.resize(static_cast<size_t>(imageIndex) + 1, false);

    if (!m_guiRenderGraphReady[imageIndex]) {
        guiGraph.Reset();

        const VkImage swapchainImage = m_swapchain.GetImage(imageIndex);
        const VkImageView swapchainView = m_swapchain.GetImageView(imageIndex);
        const VkExtent2D extent = m_swapchain.GetExtent();
        const VkFormat format = m_swapchain.GetImageFormat();

        vk::ResourceHandle backbuffer =
            guiGraph.SetBackbuffer(swapchainImage, swapchainView, format, extent.width, extent.height,
                                   VK_SAMPLE_COUNT_1_BIT, rhi::TextureLayout::Undefined);
        guiGraph.SetBackbufferFinalLayout(rhi::TextureLayout::Present);

        guiGraph.AddPass("GUI", [this, &backbuffer, extent](vk::PassBuilder &builder) {
            backbuffer = builder.WriteColor(backbuffer, 0);
            builder.SetRenderArea(extent.width, extent.height);
            builder.SetClearColor(0.0f, 0.0f, 0.0f, 1.0f);

            return [this, extent](vk::RenderContext &ctx) {
                VkViewport viewport{};
                viewport.x = 0.0f;
                viewport.y = 0.0f;
                viewport.width = static_cast<float>(extent.width);
                viewport.height = static_cast<float>(extent.height);
                viewport.minDepth = 0.0f;
                viewport.maxDepth = 1.0f;
                ctx.SetViewport(viewport);

                VkRect2D scissor{};
                scissor.offset = {0, 0};
                scissor.extent = extent;
                ctx.SetScissor(scissor);

                if (m_guiRenderCallback)
                    m_guiRenderCallback(ctx);
            };
        });

        guiGraph.SetOutput(backbuffer);
        if (!guiGraph.Compile()) {
            INXLOG_ERROR("Failed to compile swapchain GUI render graph");
#if INFERNUX_FRAME_PROFILE
            m_gpuTimestampQueries.EndRegion(cmdBuf, gpuFrameRegion, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT);
            m_gpuTimestampQueries.FinishFrame(m_currentFrame);
#endif
            vkEndCommandBuffer(cmdBuf);
            return;
        }
        m_guiRenderGraphReady[imageIndex] = true;
    }

#if INFERNUX_FRAME_PROFILE
    const auto gpuGuiRegion = m_gpuTimestampQueries.BeginRegion(cmdBuf, "GUI", VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT);
#endif
    guiGraph.Execute(cmdBuf);
#if INFERNUX_FRAME_PROFILE
    m_gpuTimestampQueries.EndRegion(cmdBuf, gpuGuiRegion, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT);
#endif
#if INFERNUX_FRAME_PROFILE
    _tNow = Clock::now();
    m_drawSubMs[7] += std::chrono::duration<double, std::milli>(_tNow - _tPrev).count();
#endif

#if INFERNUX_FRAME_PROFILE
    m_gpuTimestampQueries.EndRegion(cmdBuf, gpuFrameRegion, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT);
    m_gpuTimestampQueries.FinishFrame(m_currentFrame);
#endif
    if (vkEndCommandBuffer(cmdBuf) != VK_SUCCESS) {
        INXLOG_ERROR("Failed to record command buffer");
    }
}

void InxVkCoreModular::UpdateUniformBuffer(uint32_t currentImage, const float *viewPos, const float *viewLookAt,
                                           const float *viewUp)
{
    UniformBufferObject ubo{};
    ubo.model = glm::mat4(1.0f); // Identity - actual model matrices are per-object

    // Use camera's actual matrices from SceneRenderBridge for consistency with picking
    SceneRenderBridge &bridge = SceneRenderBridge::Instance();
    Camera *activeCamera = bridge.GetEditorCamera();

    if (activeCamera) {
        // Use camera's actual view and projection matrices
        ubo.view = activeCamera->GetViewMatrix();
        ubo.proj = activeCamera->GetProjectionMatrix(); // Already has Y-flip for Vulkan
    } else {
        // Fallback to legacy calculation
        glm::vec3 eye(viewPos[0], viewPos[1], viewPos[2]);
        glm::vec3 center(viewLookAt[0], viewLookAt[1], viewLookAt[2]);
        glm::vec3 up(viewUp[0], viewUp[1], viewUp[2]);
        ubo.view = glm::lookAt(eye, center, up);

        VkExtent2D extent = m_swapchain.GetExtent();
        float aspect = static_cast<float>(extent.width) / static_cast<float>(extent.height);
        if (m_sceneRenderTargetWidth > 0 && m_sceneRenderTargetHeight > 0) {
            aspect = static_cast<float>(m_sceneRenderTargetWidth) / static_cast<float>(m_sceneRenderTargetHeight);
        }
        ubo.proj = glm::perspective(glm::radians(60.0f), aspect, 0.01f, 1000.0f);
        ubo.proj[1][1] *= -1; // Flip Y for Vulkan
    }

    ubo.previousViewProj = ubo.proj * ubo.view;

    // Stage the UBO data — the actual GPU write happens inline in the
    // command buffer via CmdUpdateUniformBuffer() during RecordCommandBuffer().
    // This eliminates the CPU→GPU race on the scene UBO that previously
    // required vkDeviceWaitIdle() for correctness.
    m_stagedUBO = ubo;
    m_uboDirty = true;
}

// ============================================================================
// Frame Synchronization & Deferred Deletion
// ============================================================================

void InxVkCoreModular::WaitForCurrentFrame()
{
    m_swapchain.WaitForFrame();
}

void InxVkCoreModular::TickDeletionQueue()
{
#if INFERNUX_FRAME_PROFILE
    (void)m_gpuTimestampQueries.CollectCompletedFrame(m_currentFrame);
#endif
    m_resourceManager.PollGpuUploads();
    m_resourceManager.PollAsyncGraphicsSubmissions();
    m_resourceManager.PollImageReadbacks();
    m_deletionQueue.Tick();
    if ((m_ensureFrameCounter & 63u) == 0u)
        CollectUnusedShadowMaterialBindings();
    if (m_materialPipelineManagerInitialized)
        (void)m_materialPipelineManager.CollectUnusedRenderData();
    (void)m_textureCache.TrimToBudget();
    (void)TrimMeshGpuBudget();
}

void InxVkCoreModular::FlushDeletionQueue()
{
    m_deletionQueue.FlushAll();
}

void InxVkCoreModular::DeferDeletion(std::function<void()> deleter)
{
    m_deletionQueue.Push(std::move(deleter));
}

} // namespace infernux
