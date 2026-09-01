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
#include "vk/RhiVulkanTypes.h"

#include <function/renderer/shader/ShaderProgram.h>
#include <function/resources/AssetRegistry/AssetRegistry.h>
#include <function/resources/InxFileLoader/InxShaderLoader.hpp>
#include <function/resources/InxMaterial/InxMaterial.h>

#include <glm/glm.hpp>
#include <glm/gtc/matrix_transform.hpp>

#include <SDL3/SDL_vulkan.h>

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
    m_deletionQueue.BindSerialSource([this] { return m_backend.Queues().GetLastReservedCompletionEpoch(); });
}

InxVkCoreModular::~InxVkCoreModular()
{
    // Renderer construction precedes SDL/Vulkan startup.  If window creation
    // fails, this object is still destroyed even though no Vulkan instance or
    // device ever existed.  The normal teardown below assumes initialized
    // renderer subsystems, so leave the default-constructed RAII members to
    // perform their null-handle cleanup in that partial-startup state.
    if (m_backend.Device().GetInstance() == VK_NULL_HANDLE && !m_backend.Device().IsValid()) {
        return;
    }

    if (m_backend.Device().IsValid() && !m_shuttingDown) {
        m_backend.Device().WaitIdle();
    }

    // Async preview submissions retain transient buffers and cloned materials.
    // Release those leases while every renderer subsystem and the device are
    // still alive, then destroy both previewers in the controlled order below.
    m_resourceManager.DrainAsyncGraphicsSubmissions();

    // Draw-list metadata owns stable mesh-buffer leases. Release those leases
    // before clearing the residency maps and before the Vulkan allocator dies.
    ReleaseActiveDrawLists();

    // Pass-resolution entries retain immutable ShaderProgram publications.
    // Drop those external owners before MaterialPipelineManager shuts down its
    // program cache; otherwise their Vulkan layouts/modules can outlive the
    // device and be destroyed by member teardown with an invalid VkDevice.
    ReleaseMaterialPassResolutionCache();

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
        m_backend.Presentation().SetSkipWaitIdle(true);
    }

    // Cleanup shadow pipeline resources before general cleanup
    CleanupShadowPipeline();

    // Cleanup per-view descriptor resources (multi-camera shadow)
    DestroyPerViewDescriptorResources();

    // Cleanup engine globals descriptor resources
    DestroyGlobalsDescriptorResources();

    // The shadow/view cleanup above may enqueue resources after the initial
    // shutdown drain. The device is idle here, so release them before their
    // resource manager and allocator disappear.
    m_deletionQueue.FlushAll();

    // Explicit destruction in controlled order (avoids double-free from
    // RAII reverse-declaration order when handles are shared across systems).
    m_perObjectBuffers.clear();
    m_sharedMeshBuffers.clear();
    m_pendingSharedMeshBuffers.clear();
    m_pendingTextureAssetLoads.clear();
    m_pendingTextureStagingLoads.clear();
    m_pendingTextureGpuUploads.clear();

    // Preview renderers are texture/material publication consumers. Destroy
    // them before the residency cache so no TextureGpuView can outlive the RHI
    // device through a preview-owned descriptor or asynchronous readback.
    m_gpuMeshPreview.reset();
    m_gpuMaterialPreview.reset();
    // ShaderProgram keeps the table layout as a device-global ABI object. Drop
    // that reference before destroying the table's VkDescriptorSetLayout.
    ShaderProgram::SetBindlessTextureEnabled(false);
    ShaderProgram::SetBindlessTextureDescSetLayout(VK_NULL_HANDLE);
    InxShaderLoader::SetBindlessTextureABIEnabled(false);
    m_backend.Device().GetRhiDevice().ClearBindlessTextureTable();
    m_bindlessTextureTable.DestroyAfterDeviceIdle();
    m_textureCache.Clear();
    m_shaderCache.Clear();

    m_canonicalLightGpuBuffer.Shutdown();
    m_materialUbo.reset();
    m_materialUboMapped = nullptr;
    m_globalsBuffers.clear();

    m_depthImage.reset();

#if INFERNUX_FRAME_PROFILE
    m_gpuTimestampQueries.Destroy();
#endif
    DestroyGuiRenderGraphs();
    m_submissionExecutor.Destroy();
    m_resourceManager.Destroy();
    m_asyncReadbackContext.Destroy();
    m_asyncTransferContext.Destroy();

    // RenderGraph::Destroy() and MaterialPipelineManager::Shutdown()
    // already destroyed render passes, layouts, and descriptor set
    // layouts that VkPipelineManager may have tracked. Keep pipeline
    // tracking alive so any leftovers still get reclaimed here.
    m_pipelineManager.ClearTrackedNonPipelineResources();
    m_pipelineManager.Destroy();

    m_backend.Presentation().Destroy();
    m_backend.Queues().Destroy();

    // Some shutdown paths defer frees into the submission retirement queue. Flush one
    // final time after all subsystems have torn down but before destroying the
    // allocator/device.
    m_deletionQueue.FlushAll();

    m_backend.Devices().Destroy();

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

#if INFERNUX_VULKAN_VALIDATION_LAYERS
    m_deviceConfig.enableValidationLayers = true;
#endif

    // Initialize instance only (device will be created in PrepareSurface after surface is available)
    if (!m_backend.Devices().InitializePrimaryInstance(m_deviceConfig)) {
        INXLOG_ERROR("Failed to initialize Vulkan instance");
        return false;
    }

    // Store instance for InxRenderer access
    m_instance = m_backend.Device().GetInstance();

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
    if (!m_backend.Devices().InitializePrimaryDevice(m_surface, m_deviceConfig)) {
        INXLOG_ERROR("Failed to initialize Vulkan device");
        return false;
    }
    if (!m_backend.Queues().Initialize(m_backend.Device(), m_maxFramesInFlight)) {
        INXLOG_ERROR("Failed to initialize Vulkan queue manager");
        return false;
    }
    if (!m_submissionExecutor.Initialize(m_backend.Device(), m_backend.Queues(), m_maxFramesInFlight)) {
        INXLOG_ERROR("Failed to initialize Vulkan submission executor");
        return false;
    }
    m_backend.Device().GetRhiDevice().UseSubmissionSerials(
        [this] { return m_backend.Queues().GetLastReservedCompletionEpoch(); });

    // Now that device is ready, initialize resource manager
    if (!m_resourceManager.Initialize(m_backend.Device(), &m_backend.Queues())) {
        INXLOG_ERROR("Failed to initialize resource manager");
        return false;
    }

    // Shader assets are imported as soon as InxRenderer::Init returns, before
    // PreparePipeline runs. Finalize the material texture ABI here, using the
    // actual table allocation result rather than physical-device capability.
    // This makes the first shader compilation bounded when table creation or
    // descriptor allocation fails, even on an otherwise capable device.
    m_textureCache.CreateDefaultWhiteTexture("white", m_resourceManager);
    auto &rhiDevice = m_backend.Device().GetRhiDevice();
    if (auto fallbackSlot = m_textureCache.Find("white")) {
        const auto fallback = fallbackSlot->Acquire();
        if (fallback &&
            m_bindlessTextureTable.Initialize(
                GetDevice(), rhiDevice.GetDescriptorManager(), rhiDevice.GetCapabilityState(),
                rhiDevice.GetCapabilities().limits, rhiDevice.Resolve(fallback->GetView()),
                rhiDevice.Resolve(fallback->GetSampler()), std::static_pointer_cast<const void>(fallback))) {
            const auto stats = m_bindlessTextureTable.GetStats();
            INXLOG_INFO("Bindless texture table initialized: capacity=", stats.capacity);
        } else {
            INXLOG_WARN("Bindless texture table unavailable; using bounded material descriptors");
        }
    } else {
        INXLOG_WARN("Bindless texture table unavailable: default white texture publication is missing");
    }

    const bool bindlessTextureABI = vk::VulkanBindlessTextureTable::CanUseShaderABI(rhiDevice.GetCapabilityState(),
                                                                                    m_bindlessTextureTable.IsReady());
    if (bindlessTextureABI &&
        !rhiDevice.ConfigureBindlessTextureTable(
            m_bindlessTextureTable.GetLayout(), m_bindlessTextureTable.GetSet(),
            [this, &rhiDevice](const std::shared_ptr<const rhi::TextureGpuView> &view) {
                if (!view)
                    return rhi::ResourceIndex{};
                return m_bindlessTextureTable.PublishTextureView(view, rhiDevice.Resolve(view->GetView()),
                                                                 rhiDevice.Resolve(view->GetSampler()));
            },
            [this](const rhi::ResourceIndex *resources, size_t count, rhi::SubmissionSerial serial) {
                m_bindlessTextureTable.MarkSetUsed(serial);
                for (size_t index = 0; index < count; ++index)
                    (void)m_bindlessTextureTable.MarkUsed(resources[index], serial);
            })) {
        INXLOG_ERROR("Bindless texture table could not publish its RHI binding; using bounded material descriptors");
    }
    const bool bindlessRhiReady = rhiDevice.GetBindlessTextureTableBinding().IsValid();
    ShaderProgram::SetBindlessTextureDescSetLayout(
        bindlessRhiReady ? rhiDevice.Resolve(rhiDevice.GetBindlessTextureTableBinding().layout) : VK_NULL_HANDLE);
    ShaderProgram::SetBindlessTextureEnabled(bindlessTextureABI && bindlessRhiReady);
    InxShaderLoader::SetBindlessTextureABIEnabled(bindlessTextureABI && bindlessRhiReady);

    // Initialize the async-transfer context. On GPUs without a dedicated
    // transfer queue this aliases to the graphics queue and behaves like
    // a pooled-fence fast path; on GPUs with one (most discrete cards) it
    // unlocks truly parallel asset uploads. Failures are non-fatal — the
    // engine simply keeps using the synchronous VkResourceManager path.
    const auto &queueIndices = m_backend.Device().GetQueueIndices();
    const uint32_t graphicsFamily = queueIndices.graphicsFamily.value_or(0);
    const uint32_t transferFamily = queueIndices.transferFamily.value_or(graphicsFamily);
    if (m_asyncTransferContext.Initialize(
            m_backend.Device().GetDevice(), transferFamily, m_backend.Device().GetTransferQueue(),
            m_backend.Device().HasDedicatedTransferQueue(), m_backend.Device().IsTimelineSemaphoreEnabled(),
            &m_backend.Queues(),
            m_backend.Device().HasDedicatedTransferQueue() ? rhi::QueueRole::Transfer : rhi::QueueRole::Graphics)) {
        // Plug the async context into the resource manager so non-mipmap
        // texture uploads route through the dedicated DMA queue. Mipmap
        // generation still falls back to the graphics queue because
        // vkCmdBlitImage is not legal on transfer-only queues.
        m_resourceManager.SetAsyncTransferContext(&m_asyncTransferContext, graphicsFamily);
    } else {
        INXLOG_WARN("Async transfer context unavailable; uploads will use the graphics queue.");
    }

    if (m_asyncReadbackContext.Initialize(m_backend.Device().GetDevice(), graphicsFamily,
                                          m_backend.Device().GetGraphicsQueue(), false, false, &m_backend.Queues(),
                                          rhi::QueueRole::Graphics)) {
        m_resourceManager.SetAsyncReadbackContext(&m_asyncReadbackContext);
    } else {
        INXLOG_WARN("Async graphics readback context unavailable.");
    }

    // Initialize pipeline manager
    m_pipelineManager.Initialize(m_backend.Device().GetDevice());

    // Initialize render graph
    m_renderGraph.Initialize(&m_backend.Device(), nullptr, &m_backend.Queues());
#if INFERNUX_FRAME_PROFILE
    if (!m_gpuTimestampQueries.Initialize(m_backend.Device(), m_maxFramesInFlight)) {
        INXLOG_WARN("GPU timestamp queries are unavailable on this device");
    }
#endif

    // Get extent from surface capabilities
    auto swapchainSupport = m_backend.Device().QuerySwapchainSupport();
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
    if (!m_backend.Presentation().Create(m_backend.Device(), width, height)) {
        INXLOG_ERROR("Failed to create swapchain");
        return false;
    }
    if (m_presentationView.id == rhi::InvalidRenderViewId)
        m_presentationView.id = rhi::AllocateRenderViewId();
    m_presentationView.device = m_backend.Device().GetDeviceId();
    m_presentationView.kind = rhi::RenderViewKind::Presentation;
    m_presentationView.output = rhi::RenderOutputKind::PresentationImage;
    m_presentationView.width = width;
    m_presentationView.height = height;
    m_presentationView.colorFormat = rhi::FromVkFormat(m_backend.Presentation().GetImageFormat());
    m_presentationView.samples = rhi::SampleCount::One;
    ++m_presentationView.revision;
    m_renderGraph.SetRenderView(m_presentationView);

    // Create depth resources
    CreateDepthResources();

    // Create uniform buffers
    CreateUniformBuffers();

    INXLOG_INFO("Surface prepared successfully");
    return true;
}

bool InxVkCoreModular::RecreatePresentationSurface(const std::function<bool(VkInstance, VkSurfaceKHR *)> &createSurface)
{
    if (!createSurface || !m_backend.Device().IsValid() || m_instance == VK_NULL_HANDLE)
        return false;

    // Mobile surface replacement is rare and is a hard presentation boundary.
    // A full drain is intentional: no command may retain an image from the
    // Android SurfaceView that was destroyed while the app was backgrounded.
    m_backend.Device().WaitIdle();
    ReleaseMaterialPassResolutionCache();
    DestroyGuiRenderGraphs();
    m_depthImage.reset();
    m_backend.Presentation().Destroy();

    m_backend.Device().SetExternalSurface(VK_NULL_HANDLE);
    if (m_surface != VK_NULL_HANDLE) {
        SDL_Vulkan_DestroySurface(m_instance, m_surface, nullptr);
        m_surface = VK_NULL_HANDLE;
    }

    VkSurfaceKHR replacement = VK_NULL_HANDLE;
    if (!createSurface(m_instance, &replacement) || replacement == VK_NULL_HANDLE) {
        INXLOG_WARN("Platform presentation surface is not ready; recreation will be retried");
        return false;
    }
    m_surface = replacement;
    m_backend.Device().SetExternalSurface(replacement);

    const auto support = m_backend.Device().QuerySwapchainSupport();
    uint32_t width = support.capabilities.currentExtent.width;
    uint32_t height = support.capabilities.currentExtent.height;
    if (width == std::numeric_limits<uint32_t>::max() || height == std::numeric_limits<uint32_t>::max() || width == 0 ||
        height == 0) {
        width = m_windowWidth;
        height = m_windowHeight;
    }
    if (width == 0 || height == 0 || !m_backend.Presentation().Create(m_backend.Device(), width, height)) {
        INXLOG_WARN("Replacement presentation surface has no usable swapchain yet; recreation will be retried");
        return false;
    }

    m_renderGraph.Initialize(&m_backend.Device(), nullptr, &m_backend.Queues());
    const VkExtent2D extent = m_backend.Presentation().GetExtent();
    m_presentationView.width = extent.width;
    m_presentationView.height = extent.height;
    m_presentationView.colorFormat = rhi::FromVkFormat(m_backend.Presentation().GetImageFormat());
    ++m_presentationView.revision;
    m_renderGraph.SetRenderView(m_presentationView);
    CreateDepthResources();
    INXLOG_INFO("Platform presentation surface recreated: ", extent.width, "x", extent.height);
    return true;
}

void InxVkCoreModular::PreparePipeline()
{
    // Create default flat normal texture (0.5, 0.5, 1.0 = tangent-space (0,0,1))
    m_textureCache.CreateSolidColorTexture("_default_normal", 128, 128, 255, 255, m_resourceManager);
    INXLOG_INFO("Created default flat normal texture: _default_normal");

    // Register the canonical per-view ABI before any ShaderProgram creates a
    // pipeline layout. Descriptor sets are allocated later, after the default
    // textures needed to initialize their shadow binding already exist.
    if (!CreatePerViewDescriptorResources())
        throw std::runtime_error("Failed to create per-view descriptor resources");

    // Initialize material system (default material + pipelines).
    InitializeMaterialSystem();

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

bool InxVkCoreModular::EnsureShaderAvailable(const std::string &name, const std::string &type)
{
    if (HasShader(name, type))
        return true;
    if (!m_shaderAssetResolver || !m_shaderAssetResolver(name, type))
        return false;
    return HasShader(name, type);
}

uint64_t InxVkCoreModular::GetShaderCodeFingerprint(const std::string &name, const std::string &type) const
{
    return m_shaderCache.GetCodeFingerprint(name, type);
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
            m_deletionQueue.Retire([device, pipeline] { vkDestroyPipeline(device, pipeline, nullptr); });
        ++m_shaderHotReloadRetirementCount;
        it = m_shadowPipelineCache.erase(it);
    }

    if (publish.replacedProgram) {
        auto previousPrograms = m_shaderCache.GetProgramCache().TakePrograms(*publish.replacedProgram);
        for (auto &previous : previousPrograms) {
            m_deletionQueue.Retire([retired = std::move(previous)]() mutable { retired.reset(); });
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
    m_shaderCache.UnloadShader(name, m_pipelineManager);
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
        m_deletionQueue.Retire([retired = std::move(program)]() mutable { retired.reset(); });
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
            m_deletionQueue.Retire([device, pipeline] { vkDestroyPipeline(device, pipeline, nullptr); });
        ++m_shaderHotReloadRetirementCount;
        it = m_shadowPipelineCache.erase(it);
    }

    // Shader modules are only consumed during pipeline creation, so their
    // standalone cache entry can be destroyed immediately on the owner thread.
    m_shaderCache.UnloadShader(shaderId.c_str(), m_pipelineManager);

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

    m_pendingTextureAssetLoads.erase(textureGuid);
    for (auto pending = m_pendingTextureStagingLoads.begin(); pending != m_pendingTextureStagingLoads.end();) {
        if (!pending->second || pending->second->GetGuid() == textureGuid)
            pending = m_pendingTextureStagingLoads.erase(pending);
        else
            ++pending;
    }
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

    const uint64_t runtimeVersion = AssetRegistry::Instance().GetAssetVersion(textureGuid);
    const uint64_t requestedRevision =
        runtimeVersion == 0 || runtimeVersion == std::numeric_limits<uint64_t>::max() ? 0 : runtimeVersion + 1;
    // Preserve every resident variant as last-known-good. The stable slot tells
    // particle consumers to resolve the requested revision, while material
    // descriptors immediately enter their existing pending-refresh path.
    const size_t requestedSlots =
        requestedRevision == 0 ? 0 : m_textureCache.RequestAssetRevision(textureGuid, requestedRevision);
    const uint32_t refreshedMaterials =
        m_materialPipelineManagerInitialized ? m_materialPipelineManager.RefreshMaterialsUsingTexture(textureGuid) : 0;

    // INXLOG_INFO("Texture revision requested for GUID: ", textureGuid,
    //             " revision=", requestedRevision == 0 ? std::string("pending") : std::to_string(requestedRevision),
    //             " slots=", requestedSlots, " materials=", refreshedMaterials);
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

void InxVkCoreModular::SetFrameComputeExecutor(std::function<void(VkCommandBuffer cmdBuf)> executor)
{
    m_frameComputeExecutor = std::move(executor);
}

void InxVkCoreModular::SetFrameComputeWorkPredicate(std::function<bool()> predicate)
{
    m_frameComputeWorkPredicate = std::move(predicate);
}

void InxVkCoreModular::SetFrameAsyncComputeExecutors(std::function<bool(VkCommandBuffer)> simulation,
                                                     std::function<bool(VkCommandBuffer)> exportPhase,
                                                     std::function<bool()> ready, std::function<uint64_t()> generation,
                                                     std::function<bool()> partitionedReady)
{
    m_frameAsyncSimulationExecutor = std::move(simulation);
    m_frameAsyncExportExecutor = std::move(exportPhase);
    m_frameAsyncComputeReady = std::move(ready);
    m_framePartitionedComputeReady = partitionedReady ? std::move(partitionedReady) : m_frameAsyncComputeReady;
    m_frameAsyncComputeGeneration = std::move(generation);
    m_frameAsyncComputePrimed = false;
    m_frameAsyncComputePrimedGeneration = 0;
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
    // Get new extent from surface capabilities
    auto swapchainSupport = m_backend.Device().QuerySwapchainSupport();
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

    // Presentation first builds a complete unpublished generation. Only at
    // its commit point do we release aliases that reference
    // the old image views; creation failure therefore leaves the active GUI
    // and swapchain generation untouched.
    const bool recreated =
        m_backend.Presentation().Recreate(m_backend.Device(), m_backend.Queues(), width, height, [this]() {
            // The old render-graph generation is about to retire. Release pass
            // publications at the commit boundary while their device and
            // material manager are still valid.
            ReleaseMaterialPassResolutionCache();
            DestroyGuiRenderGraphs();
            m_depthImage.reset();
        });
    if (!recreated) {
        return;
    }

    m_renderGraph.Initialize(&m_backend.Device(), nullptr, &m_backend.Queues());
    const VkExtent2D extent = m_backend.Presentation().GetExtent();
    m_presentationView.width = extent.width;
    m_presentationView.height = extent.height;
    m_presentationView.colorFormat = rhi::FromVkFormat(m_backend.Presentation().GetImageFormat());
    ++m_presentationView.revision;
    m_renderGraph.SetRenderView(m_presentationView);

    // Recreate depth resources
    CreateDepthResources();
}

void InxVkCoreModular::ReleaseMaterialPassResolutionCache() noexcept
{
    m_materialPassResolutionCache.clear();
    m_materialPassResolutionCacheGeneration = 0;
}

void InxVkCoreModular::SetPresentMode(int mode)
{
    static constexpr VkPresentModeKHR kModes[] = {
        VK_PRESENT_MODE_IMMEDIATE_KHR,
        VK_PRESENT_MODE_MAILBOX_KHR,
        VK_PRESENT_MODE_FIFO_KHR,
        VK_PRESENT_MODE_FIFO_RELAXED_KHR,
    };
    if (mode < 0 || mode > 3)
        return;
    m_backend.Presentation().SetPreferredPresentMode(kModes[mode]);
    RecreateSwapchain();
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
        graph->Initialize(&m_backend.Device(), nullptr, &m_backend.Queues());
        graph->SetRenderView(m_presentationView);
    }
    return *graph;
}

bool InxVkCoreModular::EnsureGuiRenderGraph(uint32_t imageIndex)
{
    vk::RenderGraph &guiGraph = GetGuiRenderGraph(imageIndex);
    if (m_guiRenderGraphReady.size() <= imageIndex)
        m_guiRenderGraphReady.resize(static_cast<size_t>(imageIndex) + 1, false);
    if (m_guiRenderGraphReady[imageIndex])
        return true;

    guiGraph.Reset();
    guiGraph.SetRenderView(m_presentationView);

    const VkImage swapchainImage = m_backend.Presentation().GetImage(imageIndex);
    const VkImageView swapchainView = m_backend.Presentation().GetImageView(imageIndex);
    const VkExtent2D extent = m_backend.Presentation().GetExtent();
    const VkFormat format = m_backend.Presentation().GetImageFormat();

    vk::ResourceHandle backbuffer =
        guiGraph.SetBackbuffer(swapchainImage, swapchainView, format, extent.width, extent.height,
                               VK_SAMPLE_COUNT_1_BIT, rhi::TextureLayout::Undefined);

    guiGraph.AddPass("GUI", [this, &backbuffer, extent](vk::PassBuilder &builder) {
        backbuffer = builder.WriteColor(backbuffer, 0);
        builder.SetRenderArea(extent.width, extent.height);
        builder.SetClearColor(0.0f, 0.0f, 0.0f, 1.0f);
        return [this, extent](vk::RenderContext &ctx) {
            VkViewport viewport{};
            viewport.width = static_cast<float>(extent.width);
            viewport.height = static_cast<float>(extent.height);
            viewport.minDepth = 0.0f;
            viewport.maxDepth = 1.0f;
            ctx.SetViewport(viewport);

            VkRect2D scissor{};
            scissor.extent = extent;
            ctx.SetScissor(scissor);

            if (m_guiRenderCallback)
                m_guiRenderCallback(ctx);
        };
    });

    guiGraph.AddPresentPass("Present", [&backbuffer](vk::PassBuilder &builder) {
        builder.PresentRead(backbuffer);
        return [](vk::RenderContext &) {};
    });

    guiGraph.SetOutput(backbuffer);
    if (!guiGraph.Compile()) {
        INXLOG_ERROR("Failed to compile swapchain GUI render graph");
        return false;
    }
    m_guiRenderGraphReady[imageIndex] = true;
    return true;
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
    VkExtent2D extent = m_backend.Presentation().GetExtent();
    VkFormat depthFormat = m_backend.Device().FindDepthFormat();

    m_depthImage = m_resourceManager.CreateDepthBuffer(extent.width, extent.height, depthFormat);
}

void InxVkCoreModular::CreateUniformBuffers()
{
    if (!m_canonicalLightGpuBuffer.Initialize(m_backend.Device().GetRhiDevice(), m_maxFramesInFlight))
        throw std::runtime_error("Failed to initialize canonical light GPU buffers");

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

bool InxVkCoreModular::RecordFrameCommands(VkCommandBuffer cmdBuf, uint32_t imageIndex)
{
#if INFERNUX_FRAME_PROFILE
    using Clock = std::chrono::high_resolution_clock;
    auto _tPrev = Clock::now();
    auto _tNow = _tPrev;
#endif

#if INFERNUX_FRAME_PROFILE
    m_gpuTimestampQueries.BeginFrame(cmdBuf, m_currentFrame);
    const auto gpuFrameRegion = m_gpuTimestampQueries.BeginRegion(cmdBuf, "Frame", VK_PIPELINE_STAGE_TOP_OF_PIPE_BIT);
#endif

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
    // Swapchain GUI Pass via RenderGraph. Each swapchain image owns one
    // persistent compiled graph because its VkImageView is stable
    // until swapchain recreation. Rebuilding this one-pass graph every frame
    // used to repeat RHI registration and graph compilation
    // lookup on the hottest render path.
    // ========================================================================
    vk::RenderGraph &guiGraph = GetGuiRenderGraph(imageIndex);
    if (!EnsureGuiRenderGraph(imageIndex)) {
#if INFERNUX_FRAME_PROFILE
        m_gpuTimestampQueries.EndRegion(cmdBuf, gpuFrameRegion, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT);
        m_gpuTimestampQueries.FinishFrame(m_currentFrame);
#endif
        return false;
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
    m_drawSubMs[6] += std::chrono::duration<double, std::milli>(_tNow - _tPrev).count();
#endif

#if INFERNUX_FRAME_PROFILE
    m_gpuTimestampQueries.EndRegion(cmdBuf, gpuFrameRegion, VK_PIPELINE_STAGE_BOTTOM_OF_PIPE_BIT);
    m_gpuTimestampQueries.FinishFrame(m_currentFrame);
#endif
    return true;
}

// ============================================================================
// Frame Synchronization & Deferred Deletion
// ============================================================================

void InxVkCoreModular::WaitForCurrentFrame()
{
    const uint32_t frameSlot = GetCurrentFrameSlot();
    if (m_backend.Queues().WaitForGraphicsFrameSlot(frameSlot, [this, frameSlot](uint32_t elapsedMilliseconds) {
            m_submissionExecutor.LogFrameWaitDiagnostics(frameSlot, elapsedMilliseconds);
        })) {
        m_submissionExecutor.CompleteFrame(frameSlot);
        (void)m_backend.Queues().CompleteFrameSlot(frameSlot);
    }
}

void InxVkCoreModular::CollectRetiredGpuResources()
{
#if INFERNUX_FRAME_PROFILE
    (void)m_gpuTimestampQueries.CollectCompletedFrame(m_currentFrame);
#endif
    m_resourceManager.PollGpuUploads();
    m_resourceManager.PollAsyncGraphicsSubmissions();
    m_resourceManager.PollImageReadbacks();
    const auto completedEpoch = m_backend.Queues().GetCompletedCompletionEpoch();
    (void)m_bindlessTextureTable.Collect(completedEpoch);
    (void)m_backend.Device().GetRhiDevice().CollectDescriptorRetirements(completedEpoch);
    (void)m_backend.Device().GetRhiDevice().CollectResourceRetirements(completedEpoch);
    (void)m_deletionQueue.Collect(completedEpoch);
    if ((m_ensureFrameCounter & 63u) == 0u)
        CollectUnusedShadowMaterialBindings();
    if (m_materialPipelineManagerInitialized)
        (void)m_materialPipelineManager.CollectUnusedRenderData();
    (void)m_textureCache.TrimToBudget();
    (void)TrimMeshGpuBudget();
}

void InxVkCoreModular::FlushRetiredGpuResources()
{
    m_deletionQueue.FlushAll();
    // Graph/runtime destructors Release() buffers and descriptor sets onto
    // serial-gated RHI queues. After vkDeviceWaitIdle those serials are
    // complete; collect them now so the next Play generation cannot reuse
    // handle slots or descriptor sets that still own the previous objects.
    const auto completed = (std::numeric_limits<rhi::SubmissionSerial>::max)();
    (void)m_bindlessTextureTable.Collect(completed);
    (void)m_backend.Device().GetRhiDevice().CollectDescriptorRetirements(completed);
    (void)m_backend.Device().GetRhiDevice().CollectResourceRetirements(completed);
}

void InxVkCoreModular::RetireGpuResource(std::function<void()> deleter)
{
    m_deletionQueue.Retire(std::move(deleter));
}

} // namespace infernux
