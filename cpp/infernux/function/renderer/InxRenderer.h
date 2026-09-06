#pragma once

#include <function/renderer/lighting/ShadowFrame.h>

// Minimal includes required for public API value types and POD members
#include "CaptureService.h"
#include "GpuResidency.h"
#include "InxRenderStruct.h"
#include "ProfileConfig.h"
#include "ScenePickingTypes.h"
#include "particle/ParticleGpuViewDiagnostics.h"
#include "rhi/RhiDescriptors.h"
#include <array>
#include <chrono>
#include <core/log/InxLog.h>           // LogLevel enum (used in SetLogLevel)
#include <core/types/InxApplication.h> // InxAppMetadata value type (public methods)
#include <cstdint>
#include <functional>
#include <glm/glm.hpp>
#include <memory>
#include <string>
#include <unordered_map>
#include <vector>

namespace infernux
{
// ============================================================================
// Forward declarations for private subsystem types.
// Full definitions are in InxRenderer.cpp. Keeping them out of this header
// eliminates the transitive include fan-out that made every InxRenderer.h
// consumer recompile when any subsystem changed.
// ============================================================================
class EditorGizmos;
class EditorTools;
class GizmosDrawCallBuffer;
namespace particle
{
class ParticleGpuDrawRegistry;
class ParticleGpuSystemManager;
} // namespace particle
class InxGUI;
class InxGUIRenderable;
class InxMaterial;
class InxMesh;
class InxVkCoreModular;
class InxView;
class OutlineRenderer;
struct ParticleProgramBootstrap;
class RenderPipelineCallback;
class ResourcePreviewManager;
class Scene;
class ScenePickingService;
class SceneRenderGraph;
class SceneRenderTarget;
class TransientResourcePool;
class InxGUIContext;
class InxScreenUIRenderer;
struct ShaderProgramArtifact;
struct ShaderProgramKey;
namespace vk
{
class ImageReadbackTicket;
}

struct RendererFrameTelemetrySnapshot
{
    uint64_t frame = 0;
    bool sceneViewVisible = false;
    bool sceneTargetReady = false;
    bool gameCameraEnabled = false;
    bool gameCameraAvailable = false;
    size_t gameCameraCount = 0;
    std::vector<uint64_t> gameCameraIds;
    std::vector<uint64_t> gameRenderViewIds;
    bool gameTargetReady = false;
    uint32_t sceneTargetWidth = 0;
    uint32_t sceneTargetHeight = 0;
    uint32_t gameTargetWidth = 0;
    uint32_t gameTargetHeight = 0;
    size_t sceneDrawCallCount = 0;
    size_t sceneShadowDrawCallCount = 0;
    size_t gameDrawCallCount = 0;
    size_t gameShadowDrawCallCount = 0;
    uint32_t sceneShadowViewCount = 0;
    uint32_t gameShadowViewCount = 0;
    uint32_t sceneShadowAssignmentCount = 0;
    uint32_t gameShadowAssignmentCount = 0;
    uint64_t sceneShadowResourceIdentity = 0;
    uint64_t gameShadowResourceIdentity = 0;
    size_t lightCount = 0;
    bool canonicalLightGpuBufferReady = false;
    uint64_t canonicalLightGpuBytes = 0;
    uint64_t canonicalLightGeneration = 0;
    uint32_t canonicalDirectionalLightCount = 0;
    uint32_t canonicalLocalLightCount = 0;
    uint64_t sceneTemporalDiscontinuityRevision = 0;
    uint64_t temporalHistoryInvalidationCount = 0;
    uint64_t sceneRenderViewId = 0;
    uint64_t gameRenderViewId = 0;
    size_t sceneTemporalHistoryCount = 0;
    size_t gameTemporalHistoryCount = 0;
    size_t sceneValidTemporalHistoryCount = 0;
    size_t gameValidTemporalHistoryCount = 0;
    uint32_t sceneTemporalSampleIndex = 0;
    uint32_t gameTemporalSampleIndex = 0;
    size_t gpuParticleSystemCount = 0;
    size_t gpuParticleOutputCount = 0;
    uint64_t gpuParticleCapacity = 0;
    uint64_t gpuParticleLastScheduledFrame = 0;
    size_t gpuParticleScheduledSystemCount = 0;
    size_t gpuParticleSimulatingSystemCount = 0;
    size_t gpuParticleRenderingSystemCount = 0;
    uint64_t gpuParticleRequestedSpawnCount = 0;
    size_t gpuParticleContinuationSystemCount = 0;
    uint64_t gpuParticleContinuationCapacity = 0;
    uint32_t gpuParticleContinuationProgramGeneration = 0;
    uint64_t gpuParticleContinuationPrepareRecordCalls = 0;
    uint64_t gpuParticleContinuationClassifyRecordCalls = 0;
    uint64_t gpuParticleContinuationDispatchRecordCalls = 0;
    size_t gpuParticleContinuationResetPendingCount = 0;
    size_t gpuParticleContactRuntimeSystemCount = 0;
    uint64_t gpuParticleContactRecordCapacity = 0;
    uint64_t gpuParticleContactWorkItemCapacity = 0;
    uint64_t gpuParticleContactResidentBytes = 0;
    uint64_t gpuParticleContactPrepareRecordCalls = 0;
    uint64_t gpuParticleContactSolveRecordCalls = 0;
    uint64_t gpuParticleCollisionSceneRevision = 0;
    uint32_t gpuParticleCollisionSceneColliderCount = 0;
    uint64_t gpuParticleCollisionSceneTopologyRevision = 0;
    uint32_t gpuParticleCollisionSceneMeshVertexCount = 0;
    uint32_t gpuParticleCollisionSceneMeshIndexCount = 0;
    uint32_t gpuParticleCollisionSceneMeshBvhNodeCount = 0;
    std::string sceneRenderGraphName;
    std::string gameRenderGraphName;
    uint64_t sceneRenderGraphExecutionCount = 0;
    uint64_t gameRenderGraphExecutionCount = 0;
    bool sceneRenderGraphCurrentExecuted = false;
    bool gameRenderGraphCurrentExecuted = false;
    std::vector<std::string> sceneRenderGraphPassNames;
    std::vector<std::string> gameRenderGraphPassNames;
    std::string sceneRenderGraphDebug;
    std::string gameRenderGraphDebug;
    uint64_t submissionGeneration = 0;
    bool submissionComposed = false;
    bool submissionComputeQueueIndependent = false;
    bool submissionTransferQueueIndependent = false;
    bool submissionAsyncComputeActive = false;
    bool submissionParallelComputeGraphics = false;
    uint32_t submissionBatchCount = 0;
    uint32_t submissionGraphicsBatchCount = 0;
    uint32_t submissionComputeBatchCount = 0;
    uint32_t submissionTransferBatchCount = 0;
    uint32_t submissionCrossQueueDependencyCount = 0;
    uint32_t submissionUnorderedComputeGraphicsPairCount = 0;
    double gameRenderMs = 0.0;
    double gameOnlyFrameMs = 0.0;
    double sceneUpdateMs = 0.0;
    double guiBuildMs = 0.0;
    double prepareFrameMs = 0.0;
    uint64_t guiFrame = 0;
    bool guiFrameRequested = false;
    double guiFrameIntervalMs = 0.0;
    double guiFrameUntilDueMs = 0.0;
    uint64_t guiFrameConsumeCount = 0;
    uint64_t guiFrameApprovedCount = 0;
    uint64_t guiFrameForcedCount = 0;
    uint64_t guiFrameRequestCount = 0;
    std::unordered_map<std::string, double> guiPanelTimesMs;
    std::unordered_map<std::string, std::unordered_map<std::string, double>> guiPanelSubTimesMs;
};

struct MsaaStateSnapshot
{
    int activeSamples = 1;
    int sceneRequestedSamples = 0;
    int gameRequestedSamples = 0;
    uint32_t supportedSampleMask = 0;
    bool requestConflict = false;
    bool sceneTargetAligned = true;
    bool gameTargetAligned = true;
    bool materialPipelinesAligned = true;
    uint64_t sceneMsaaColorBytes = 0;
    uint64_t gameMsaaColorBytes = 0;
    uint64_t reconfigurationCount = 0;
    uint64_t rejectedRequestCount = 0;
};

struct UIPerformanceMetricStats
{
    size_t sampleCount = 0;
    double meanMs = 0.0;
    double medianMs = 0.0;
    double p95Ms = 0.0;
    double p99Ms = 0.0;
    double maxMs = 0.0;
};

struct RendererUIPerformanceSnapshot
{
    uint64_t firstFrame = 0;
    uint64_t lastFrame = 0;
    size_t sampleCount = 0;
    UIPerformanceMetricStats guiBuild;
    std::unordered_map<std::string, UIPerformanceMetricStats> panelTimes;
    std::unordered_map<std::string, std::unordered_map<std::string, UIPerformanceMetricStats>> panelSubTimes;
};

struct RendererFramePerformanceSnapshot
{
    uint64_t firstFrame = 0;
    uint64_t lastFrame = 0;
    size_t sampleCount = 0;
    size_t droppedSampleCount = 0;
    UIPerformanceMetricStats frame;
    UIPerformanceMetricStats gameOnly;
    UIPerformanceMetricStats render;
    UIPerformanceMetricStats scene;
    UIPerformanceMetricStats gui;
    UIPerformanceMetricStats prepare;
};

class InxRenderer
{
  public:
    InxRenderer();
    ~InxRenderer();

    InxRenderer(const InxRenderer &) = delete;
    InxRenderer &operator=(const InxRenderer &) = delete;
    InxRenderer &operator=(InxRenderer &&) = delete;
    InxRenderer(InxRenderer &&) = delete;

    void SetCameraPos(float x, float y, float z);
    void SetCameraLookAt(float x, float y, float z);
    void SetCameraUp(float x, float y, float z);

    float *GetCameraPos();
    float *GetCameraLookAt();
    float *GetCameraUp();

    void TranslateCamera(float x, float y, float z);

    void SetAppMetadata(InxAppMetadata appMetaData);
    InxAppMetadata GetAppMetadata();
    InxAppMetadata GetRendererMetadata();

    void Init(int width, int height, InxAppMetadata appMetaData);
    void PreparePipeline();
    void DrawFrame();

    /// @brief Force the next DrawFrame() to simulate with an exact delta time
    /// instead of the wall clock. Used by Infernux::Tick so graphical frames
    /// can be stepped deterministically with the same contract as headless.
    void OverrideNextFrameDeltaTime(float seconds)
    {
        m_nextFrameDeltaTimeOverride = seconds;
    }

    /// @brief Drain GPU work before destructive scene/resource replacement.
    void WaitForGpuIdle();
    [[nodiscard]] size_t GetPendingMeshUploadCount() const;
    [[nodiscard]] uint64_t GetSubmittedMeshUploadCount() const;
    [[nodiscard]] uint64_t GetCompletedMeshUploadCount() const;
    [[nodiscard]] uint64_t GetAsyncMeshUploadCount() const;
    [[nodiscard]] size_t GetPendingTextureCpuLoadCount() const;
    [[nodiscard]] size_t GetPendingTextureUploadCount() const;
    [[nodiscard]] uint64_t GetSubmittedTextureUploadCount() const;
    [[nodiscard]] uint64_t GetCompletedTextureUploadCount() const;
    [[nodiscard]] uint64_t GetAsyncTextureUploadCount() const;
    [[nodiscard]] uint64_t GetStagingPoolBytes() const;
    [[nodiscard]] size_t GetStagingPoolBufferCount() const;
    [[nodiscard]] uint64_t GetStagingAllocationCount() const;
    [[nodiscard]] uint64_t GetStagingReuseCount() const;
    [[nodiscard]] uint64_t GetStagingDiscardCount() const;
    [[nodiscard]] uint64_t GetTextureGpuResidentBytes() const;
    [[nodiscard]] uint64_t GetTextureGpuBudgetBytes() const;
    [[nodiscard]] size_t GetTextureGpuCacheEntryCount() const;
    [[nodiscard]] size_t GetRetiredTextureGpuLeaseCount() const;
    [[nodiscard]] uint64_t GetTextureGpuEvictionCount() const;
    void SetTextureGpuBudgetBytes(uint64_t bytes);
    [[nodiscard]] size_t TrimTextureGpuBudget();
    [[nodiscard]] uint64_t GetMeshGpuResidentBytes() const;
    [[nodiscard]] uint64_t GetMeshGpuBudgetBytes() const;
    [[nodiscard]] size_t GetMeshGpuCacheEntryCount() const;
    [[nodiscard]] size_t GetRetiredMeshGpuLeaseCount() const;
    [[nodiscard]] uint64_t GetMeshGpuEvictionCount() const;
    void SetMeshGpuBudgetBytes(uint64_t bytes);
    [[nodiscard]] size_t TrimMeshGpuBudget();
    [[nodiscard]] GpuResidencySnapshot GetGpuResidencySnapshot() const;
    /// Non-blocking completion poll for headless/tool loops that are not
    /// currently advancing DrawFrame(). Never waits for a queue or device.
    void PollGpuCompletions();
    [[nodiscard]] RendererFrameTelemetrySnapshot GetFrameTelemetrySnapshot();
    [[nodiscard]] uint64_t BeginFramePerformanceWindow();
    [[nodiscard]] RendererFramePerformanceSnapshot GetFramePerformanceWindow() const;
    [[nodiscard]] uint64_t RequestGpuParticleViewDiagnostics(bool gameView, uint64_t graphInstanceId,
                                                             uint64_t cameraComponentId = 0);
    [[nodiscard]] particle::GpuParticleViewDiagnosticSnapshot
    QueryGpuParticleViewDiagnostics(bool gameView, uint64_t requestId, uint64_t cameraComponentId = 0) const;
    [[nodiscard]] RendererUIPerformanceSnapshot GetUIPerformanceSnapshot(size_t maxSamples = 240) const;
    [[nodiscard]] uint64_t GetGpuResidencyBudgetBytes() const;
    void SetGpuResidencyBudgetBytes(uint64_t bytes);
    [[nodiscard]] size_t TrimGpuResidencyBudget();
    [[nodiscard]] std::vector<GpuAssetResidencyRecord> GetAssetGpuResidency() const;

    void LoadShader(const char *name, const std::vector<char> &code, const char *type);
    void SetShaderAssetResolver(std::function<bool(const std::string &, const std::string &)> resolver);
    bool PublishShaderProgramArtifact(const ShaderProgramArtifact &artifact);
    [[nodiscard]] bool HasShaderProgramArtifact(const ShaderProgramKey &programKey) const;
    [[nodiscard]] std::shared_ptr<const ShaderProgramArtifact>
    ResolveShaderProgramArtifact(const std::shared_ptr<InxMaterial> &material);
    void SetShaderProgramArtifactResolver(std::function<void(const std::shared_ptr<InxMaterial> &)> resolver);
    bool HasShader(const std::string &name, const std::string &type) const;

    /// @brief Store shader render-state annotations (forwarded to InxVkCoreModular)
    void StoreShaderRenderMeta(const std::string &shaderId, const std::string &cullMode, const std::string &depthWrite,
                               const std::string &depthTest, const std::string &blend, int queue,
                               const std::string &passTag = "", const std::string &stencil = "",
                               const std::string &alphaClip = "");
    bool GetUserEvent();
    uint64_t QueueSyntheticKeyInput(int scancode, bool pressed, bool repeat = false);
    uint64_t QueueSyntheticMouseButtonInput(int button, bool pressed, float x, float y);
    uint64_t QueueSyntheticMouseMotionInput(float x, float y, float deltaX, float deltaY);
    uint64_t QueueSyntheticMouseWheelInput(float horizontal, float vertical);
    uint64_t QueueSyntheticTextInput(const std::string &text);
    uint64_t QueueSyntheticCloseRequest();
    [[nodiscard]] uint64_t GetLastProcessedSyntheticInputSequence() const;
    [[nodiscard]] size_t GetPendingSyntheticInputCount() const;
    void ShowWindow();
    void HideWindow();
    /// Player-only: apply chrome from the environment and show the window as
    /// soon as the SDL window exists, before Vulkan device and pipeline work.
    /// Drain startup events without revealing the hidden native window.
    /// Returns false if the user requested close/quit.
    [[nodiscard]] bool PumpStartupEvents();
    [[nodiscard]] bool IsWindowMinimized() const;
    void SetWindowIcon(const std::string &iconPath);
    void SetWindowFullscreen(bool fullscreen);
    void SetWindowTitle(const std::string &title);
    void SetWindowMaximized(bool maximized);
    void SetWindowResizable(bool resizable);

    // Close-request interception (delegates to InxView)
    bool IsCloseRequested() const;
    void ConfirmClose();
    void CancelClose();

    void SetGUIFont(const char *fontPath, float fontSize);
    float GetDisplayScale() const;
    void RegisterGUIRenderable(const char *name, std::shared_ptr<InxGUIRenderable> renderable, int priority = 0);
    void UnregisterGUIRenderable(const char *name);
    void QueueDockTabSelection(const char *windowId, bool allowDuringModal = false);
    void SetGUIPlayerMode(bool enabled);

    // ImGui texture management
    uint64_t SubmitTextureForImGui(const std::string &name, const unsigned char *pixels, size_t byteCount, int width,
                                   int height, rhi::FilterMode filter = rhi::FilterMode::Linear, bool pinned = false);
    uint64_t QueryImportedTextureForImGui(const std::string &name, const std::string &textureGuid);
    void SupersedePendingImGuiTextureUploads(const std::string &name);
    void RemoveImGuiTexture(const std::string &name);
    bool HasImGuiTexture(const std::string &name) const;
    uint64_t GetImGuiTextureId(const std::string &name) const;
    uint64_t GetImGuiTextureVersion(const std::string &name) const;
    uint64_t GetFailedImGuiTextureVersion(const std::string &name) const;
    void SetImGuiTextureBudgetBytes(uint64_t bytes);
    [[nodiscard]] size_t TrimImGuiTextureBudget();
    [[nodiscard]] uint64_t GetImGuiTextureBudgetBytes() const;
    [[nodiscard]] uint64_t GetImGuiTextureResidentBytes() const;
    [[nodiscard]] size_t GetImGuiTextureEntryCount() const;
    [[nodiscard]] size_t GetPendingImGuiTextureUploadCount() const;
    [[nodiscard]] uint64_t GetPendingImGuiTextureUploadBytes() const;
    [[nodiscard]] uint64_t GetSubmittedImGuiTextureUploadCount() const;
    [[nodiscard]] uint64_t GetCompletedImGuiTextureUploadCount() const;
    [[nodiscard]] uint64_t GetAsyncImGuiTextureUploadCount() const;
    [[nodiscard]] uint64_t GetImGuiTextureEvictionCount() const;

    // Resource preview manager
    ResourcePreviewManager *GetResourcePreviewManager();

    void SetLogLevel(LogLevel level);

    // Scene system integration
    void InitializeDefaultScene();
    void InitializeRuntimeScene();
    void UpdateSceneLighting();

    // Get the material from the first MeshRenderer in the scene
    std::shared_ptr<InxMaterial> GetFirstMeshRendererMaterial();

    // Scene render target for offscreen rendering. Replacements publish all
    // resources at one device completion epoch shared with their framebuffers.
    uint64_t GetSceneTextureId() const;
    void ResizeSceneRenderTarget(uint32_t width, uint32_t height);
    [[nodiscard]] std::shared_ptr<vk::ImageReadbackTicket> RequestRenderTargetReadback(bool gameView);
    [[nodiscard]] uint64_t RequestCapture(CaptureSource source, const std::string &outputPath);
    [[nodiscard]] CaptureSnapshot QueryCapture(uint64_t captureId) const;
    [[nodiscard]] bool CancelCapture(uint64_t captureId);
    [[nodiscard]] uint64_t RequestScenePick(float x, float y, float viewportWidth, float viewportHeight);
    [[nodiscard]] ScenePickSnapshot QueryScenePick(uint64_t requestId) const;

    // Editor gizmos
    void SetShowGrid(bool show);
    bool IsShowGrid() const;
    EditorGizmos &GetEditorGizmos();

    /// @brief Access editor tools (translate/rotate/scale gizmo)
    EditorTools *GetEditorTools();

    /// @brief Access the component gizmos draw call buffer used by the scripting layer
    GizmosDrawCallBuffer *GetGizmosDrawCallBuffer();

    particle::ParticleGpuDrawRegistry *GetParticleGpuDrawRegistry();
    particle::ParticleGpuSystemManager *GetParticleGpuSystemManager();
    [[nodiscard]] uint32_t GetMaxFramesInFlight() const noexcept;
    [[nodiscard]] uint64_t GetNextFrameIndex() const noexcept
    {
        return m_frameCount + 1;
    }

    /// @brief Set one selected object as both the gizmo target and outline target.
    void SetSelectedObjectId(uint64_t objectId);
    /// @brief Publish the primary gizmo target separately from the objects outlined for selection.
    void SetSelectionState(uint64_t primaryObjectId, const std::vector<uint64_t> &outlineObjectIds);
    [[nodiscard]] uint64_t GetSelectedObjectId() const
    {
        return m_selectedObjectId;
    }

    // Material pipeline refresh - call this after modifying material shader paths
    bool RefreshMaterialPipeline(std::shared_ptr<InxMaterial> material);

    [[nodiscard]] std::shared_ptr<vk::ImageReadbackTicket>
    BeginMaterialPreviewGPU(const std::shared_ptr<InxMaterial> &material, int size, bool *texturePending = nullptr);
    bool TryCompleteMaterialPreviewGPU(const std::shared_ptr<vk::ImageReadbackTicket> &ticket, int outputSize,
                                       std::vector<unsigned char> &outPixels);

    [[nodiscard]] std::shared_ptr<vk::ImageReadbackTicket>
    BeginMeshPreviewGPU(const InxMesh &mesh, const std::vector<std::shared_ptr<InxMaterial>> &materials, int size);
    bool TryCompleteMeshPreviewGPU(const std::shared_ptr<vk::ImageReadbackTicket> &ticket, int outputSize,
                                   std::vector<unsigned char> &outPixels);

    uint64_t RenderMeshPreviewGPUImGuiCamera(const InxMesh &mesh,
                                             const std::vector<std::shared_ptr<InxMaterial>> &materials, int size,
                                             const glm::mat4 &view, const glm::mat4 &proj, const glm::vec3 &cameraPos,
                                             bool cloneMaterials = false);

    /// Currently-published live mesh preview descriptor id (0 when absent).
    [[nodiscard]] uint64_t GetMeshPreviewDisplayTextureId() const;

    // Refresh all materials using a specific shader
    bool RefreshMaterialsUsingShader(const std::string &shaderId);

    // Invalidate shader cache for hot-reload (must call before loading new shader code)
    void InvalidateShaderCache(const std::string &shaderId, const std::string &shaderType = "");

    // Invalidate cached GPU texture and force materials to re-resolve it
    void InvalidateTextureCache(const std::string &texturePath);

    // Remove pipeline render data for a specific material (releases shared_ptr)
    void RemoveMaterialPipeline(const std::string &materialName);

    // ========================================================================
    // Render Graph Access (for Python/ML integration)
    // ========================================================================

    /// @brief Get the scene render graph for pass configuration and output access
    /// @return Pointer to SceneRenderGraph, or nullptr if not initialized
    SceneRenderGraph *GetSceneRenderGraph();

    /// @brief Set a custom render pipeline provided through ScriptableRenderContext.
    /// Pass nullptr to revert to the default C++ rendering path.
    void SetRenderPipeline(std::shared_ptr<RenderPipelineCallback> pipeline);

    // ========================================================================
    // Game Camera Render Target (for Game View panel)
    // ========================================================================

    /// @brief Get Game View texture ID for ImGui display
    uint64_t GetGameTextureId();

    /// @brief Monotonic generation of the native Game render target.
    ///
    /// The ImGui descriptor changes whenever resize or MSAA reconfiguration
    /// replaces the target, even if the active Scene and Camera are unchanged.
    [[nodiscard]] uint64_t GetGameRenderTargetGeneration() const
    {
        return m_gameRenderTargetGeneration;
    }

    /// @brief Resize the game render target to match Game View panel size
    void ResizeGameRenderTarget(uint32_t width, uint32_t height);

    /// @brief Invalidate accumulated temporal effects for selected render views.
    void InvalidateTemporalHistory(bool sceneView = true, bool gameView = true);

    /// @brief Enable/disable game camera rendering
    void SetGameCameraEnabled(bool enabled);

    /// @brief Enable/disable scene view rendering (called by Python panel visibility)
    void SetSceneViewVisible(bool visible);
    [[nodiscard]] bool IsSceneViewVisible() const
    {
        return m_sceneViewVisible;
    }

    /// @brief Check if game camera rendering is enabled
    [[nodiscard]] bool IsGameCameraEnabled() const
    {
        return m_gameCameraEnabled;
    }

    /// @brief Get last frame's game view render time (CPU-side command recording) in ms.
    /// This measures ONLY the game camera render pipeline, excluding editor panels, scene view, etc.
    [[nodiscard]] double GetLastGameRenderMs() const
    {
        return m_lastGameRenderMs;
    }

    /// @brief Get game-only frame cost in ms (SceneUpdate + PrepareFrame + GameRender).
    /// Excludes editor panel rendering (Inspector, Hierarchy, Console, etc.).
    [[nodiscard]] double GetGameOnlyFrameMs() const
    {
        return m_gameOnlyFrameMs;
    }

    /// @brief Get SceneManager::Update + LateUpdate time in ms.
    [[nodiscard]] double GetSceneUpdateMs() const
    {
        return m_sceneUpdateMs;
    }

    /// @brief Get GUI::BuildFrame time in ms (all ImGui panels).
    [[nodiscard]] double GetGuiBuildMs() const
    {
        return m_guiBuildMs;
    }

    /// @brief Get PrepareFrame (collect/cull renderables) time in ms.
    [[nodiscard]] double GetPrepareFrameMs() const
    {
        return m_prepareFrameMs;
    }

    /// @brief Get the exact time uploaded to EngineGlobalsUBO._Time.x.
    /// CPU simulations that mirror vertex displacement use this to stay in
    /// phase with the rendered surface across editor and play transitions.
    [[nodiscard]] float GetShaderTimeSeconds() const
    {
        return m_totalTime;
    }

    /// @brief Get the screen UI renderer for GPU-based 2D screen-space UI
    /// @return Pointer to InxScreenUIRenderer, or nullptr if not initialized
    InxScreenUIRenderer *GetScreenUIRenderer();

    // ========================================================================
    // MSAA Configuration
    // ========================================================================

    /// @brief Set MSAA sample count for both scene and game render targets.
    /// Valid values: 1 (off), 2, 4, 8.  Triggers Vulkan resource recreation.
    void SetMsaaSamples(int samples);

    /// @brief Get current MSAA sample count (1 = off).
    int GetMsaaSamples() const;

    /// @brief Read validated requests, device support and resource alignment.
    [[nodiscard]] MsaaStateSnapshot GetMsaaStateSnapshot() const;

    // ========================================================================
    // Present Mode
    // ========================================================================

    /// @brief Set present mode: 0=IMMEDIATE, 1=MAILBOX, 2=FIFO, 3=FIFO_RELAXED
    void SetPresentMode(int mode);

    /// @brief Set a callback invoked immediately before scene Update.
    /// Gameplay timing is advanced here so scripts observe the current frame.
    void SetPreSceneUpdateCallback(std::function<void(float)> callback)
    {
        m_preSceneUpdateCallback = std::move(callback);
    }

    /// @brief Set a callback invoked each frame before GUI::BuildFrame().
    /// Scene-mutating deferred tasks run here to prevent stale-reference hangs.
    void SetPreGuiCallback(std::function<void()> callback)
    {
        m_preGuiCallback = std::move(callback);
    }

    /// @brief Set a callback invoked each frame AFTER VkCore::DrawFrame() + EndFrame().
    /// Heavy scene-loading work runs here so that it occurs between frames.
    /// The Python callback calls engine.pump_events() internally when needed.
    void SetPostDrawCallback(std::function<void()> callback)
    {
        m_postDrawCallback = std::move(callback);
    }

    /// @brief Get current present mode (0=IMMEDIATE, 1=MAILBOX, 2=FIFO, 3=FIFO_RELAXED)
    int GetPresentMode() const;

    // ========================================================================
    // Editor Power-Save / Idle Mode
    // ========================================================================

    /// @brief Enable/disable editor idle mode (reduced FPS when no input).
    void SetEditorIdleEnabled(bool enabled);

    /// @brief Check if editor idle mode is enabled.
    bool IsEditorIdleEnabled() const;

    /// @brief Set the idle-mode target FPS (e.g. 10).  0 disables idling.
    void SetEditorIdleFps(float fps);

    /// @brief Get the current idle-mode target FPS.
    float GetEditorIdleFps() const;

    /// @brief Check if the editor is currently in idle (reduced-FPS) state.
    bool IsEditorIdling() const;

    /// @brief Set the editor-mode FPS cap (e.g. 60). 0 = uncapped.
    /// Only applies outside play mode.
    void SetEditorFpsCap(float fps);

    /// @brief Get the editor-mode FPS cap.
    float GetEditorFpsCap() const;

    /// @brief Set the play/player-mode FPS cap. 0 = uncapped.
    void SetPlayFpsCap(float fps);

    /// @brief Get the play/player-mode FPS cap.
    float GetPlayFpsCap() const;

    /// @brief Tell the renderer whether the engine is in play mode.
    /// In play mode, editor idling is disabled; an explicit play cap may remain.
    /// A Play/Stop edge also drains GPU work and drops Game/Scene view caches
    /// so particle graphs cannot keep retired buffer bindings.
    void SetPlayModeRendering(bool play);

    /// @brief Check if the renderer is in play-mode (uncapped FPS).
    bool IsPlayModeRendering() const;

    /// @brief Force full-speed rendering for the next few frames (e.g. after
    /// a programmatic scene change that doesn't generate SDL events).
    void RequestFullSpeedFrame();

    /// @brief Wake the editor event loop from a background service.
    void RequestExternalWake();

  private:
    void UpdateParticleCollisionScene();
    void ConsumeSceneTemporalDiscontinuity();
    void InvalidateGpuViewStateForSceneBoundary();
    void RecreateSceneRenderGraph();

    InxAppMetadata m_appMetadata;
    InxAppMetadata m_rendererMetadata;

    float m_cameraPos[3] = {2.0f, 2.0f, 2.0f};
    float m_cameraLookAt[3] = {0.0f, 0.0f, 0.0f};
    float m_cameraUp[3] = {0.0f, 1.0f, 0.0f};

    // Delta time tracking
    std::chrono::high_resolution_clock::time_point m_lastFrameTime;
    float m_deltaTime = 0.016f;
    /// Negative = no override. Consumed by the next DrawFrame().
    float m_nextFrameDeltaTimeOverride = -1.0f;
    float m_totalTime = 0.0f;
    float m_smoothDeltaTime = 0.016f;
    uint64_t m_frameCount = 0;

    std::unique_ptr<InxVkCoreModular> m_vkCore;
    std::function<void(const std::shared_ptr<InxMaterial> &)> m_shaderProgramArtifactResolver;
    std::unique_ptr<InxGUI> m_gui;
    std::unique_ptr<InxView> m_view;
    bool m_guiPlayerMode = false;
    uint64_t m_lastSemanticSyntheticInputSequence = 0;
    std::unique_ptr<SceneRenderTarget> m_sceneRenderTarget;
    std::unique_ptr<SceneRenderGraph> m_sceneRenderGraph;
    uint64_t m_temporalHistoryWorldId = 0;
    uint64_t m_observedSceneTemporalDiscontinuityRevision = 0;
    uint64_t m_temporalHistoryInvalidationCount = 0;
    std::unique_ptr<CaptureService> m_captureService;
    std::unique_ptr<ScenePickingService> m_scenePickingService;
    struct PendingCapture
    {
        uint64_t id = 0;
        CaptureSource source = CaptureSource::Game;
        uint64_t sourceGeneration = 0;
    };
    [[nodiscard]] bool HasPendingCapture(CaptureSource source) const;
    void SubmitPendingCaptureReadbacks();
    std::vector<PendingCapture> m_pendingCaptures;
    uint64_t m_sceneRenderTargetGeneration = 0;
    uint64_t m_gameRenderTargetGeneration = 0;
    std::unique_ptr<EditorGizmos> m_editorGizmos;
    std::unique_ptr<EditorTools> m_editorTools;
    std::unique_ptr<GizmosDrawCallBuffer> m_componentGizmos;
    std::unique_ptr<particle::ParticleGpuDrawRegistry> m_particleGpuDrawRegistry;
    std::unique_ptr<particle::ParticleGpuSystemManager> m_particleGpuSystemManager;
    std::shared_ptr<ParticleProgramBootstrap> m_particleProgramBootstrap;
    bool m_particleGpuSystemManagerInitializationAttempted = false;
    const Scene *m_particleCollisionSourceScene = nullptr;
    uint64_t m_particleCollisionSourceRevision = 0;
    uint64_t m_particleCollisionPublishedRevision = 1;
    uint64_t m_particleCollisionTopologyFingerprint = 0;
    uint64_t m_particleCollisionTopologyRevision = 1;
    struct ParticleCollisionPublishedState
    {
        std::array<float, 12> worldToCollider{};
        std::array<float, 4> worldAabbMin{};
        std::array<float, 4> worldAabbMax{};
    };
    std::unordered_map<uint64_t, ParticleCollisionPublishedState> m_particleCollisionPublishedState;
    std::unique_ptr<OutlineRenderer> m_outlineRenderer;
    std::unique_ptr<TransientResourcePool> m_transientResourcePool;
    uint64_t m_gpuResidencyBudgetBytes = 0;
    uint32_t m_gpuResidencyCheckFrames = 0;

    // Game cameras share one output target but own independent RenderGraphs.
    // This keeps camera matrices, shadows, Forward+ lists, particles and
    // temporal history isolated while Camera.depth determines execution order.
    std::unique_ptr<SceneRenderTarget> m_gameRenderTarget;
    std::unordered_map<uint64_t, std::unique_ptr<SceneRenderGraph>> m_gameRenderGraphs;
    SceneRenderGraph *m_gameRenderGraph = nullptr; ///< Representative graph for legacy single-view queries.
    std::unique_ptr<InxScreenUIRenderer> m_screenUIRenderer;
    bool m_gameCameraEnabled = false;
    bool m_sceneViewVisible = false; ///< Default false; Python editor sets true via SetSceneViewVisible()
    double m_lastGameRenderMs = 0.0; ///< Per-frame game render time (CPU command recording)
    double m_sceneUpdateMs = 0.0;    ///< SceneManager::Update + LateUpdate (ms)
    double m_guiBuildMs = 0.0;       ///< GUI::BuildFrame (all ImGui panels) (ms)
    double m_prepareFrameMs = 0.0;   ///< PrepareFrame (collect/cull) (ms)
    double m_gameOnlyFrameMs = 0.0;  ///< Sum of game-only phases (ms)

    static constexpr size_t FRAME_PERFORMANCE_HISTORY_SIZE = 240;
    struct FramePerformanceSample
    {
        uint64_t frame = 0;
        double frameMs = 0.0;
        double gameOnlyMs = 0.0;
        double renderMs = 0.0;
        double sceneMs = 0.0;
        double guiMs = 0.0;
        double prepareMs = 0.0;
    };
    std::array<FramePerformanceSample, FRAME_PERFORMANCE_HISTORY_SIZE> m_framePerformanceHistory{};
    bool m_framePerformanceWindowActive = false;
    size_t m_framePerformanceWriteIndex = 0;
    size_t m_framePerformanceSampleCount = 0;
    size_t m_framePerformanceDroppedSampleCount = 0;
    void RecordFramePerformanceSample(double frameMs);

    static constexpr size_t UI_PERFORMANCE_HISTORY_SIZE = 240;
    struct UIMetricHistory
    {
        std::array<double, UI_PERFORMANCE_HISTORY_SIZE> values{};
        std::array<uint64_t, UI_PERFORMANCE_HISTORY_SIZE> frames{};
    };
    UIMetricHistory m_uiBuildHistory;
    std::unordered_map<std::string, UIMetricHistory> m_uiPanelHistory;
    std::unordered_map<std::string, std::unordered_map<std::string, UIMetricHistory>> m_uiPanelSubHistory;
    size_t m_uiPerformanceWriteIndex = 0;
    size_t m_uiPerformanceSampleCount = 0;
    void RecordUIPerformanceFrame();

    /// Per-frame cached game camera pointer, lazily resolved once per frame
    /// by FindGameCameraCached() and cleared at the start of each DrawFrame.
    class Camera *m_cachedGameCamera = nullptr;
    std::vector<class Camera *> m_cachedGameCameras;
    bool m_gameCameraCacheValid = false;

    // Scriptable render pipeline (nullptr = default C++ path)
    std::shared_ptr<RenderPipelineCallback> m_renderPipeline;

    // Selection tracking for auto-update of outline transforms
    uint64_t m_selectedObjectId = 0;
    std::vector<uint64_t> m_selectedOutlineObjectIds;

    // Executor sub-timing (accumulated during the render-graph executor callback)
#if INFERNUX_FRAME_PROFILE
    struct ExecutorSubTiming
    {
        double sceneExecMs = 0;
        double sceneMsaaMs = 0;
        double gameSetupMs = 0;
        double gameExecMs = 0;
        double gameMsaaMs = 0;
        double gameRestoreMs = 0;
    };
    ExecutorSubTiming m_executorTiming{};

    struct FrameDetailTiming
    {
        double frameCacheBeginMs = 0.0;
        double sceneUpdateCallMs = 0.0;
        double lateUpdateCallMs = 0.0;
        double frameCacheEndMs = 0.0;
        double cleanupCollectIdsMs = 0.0;
        double cleanupReleaseMs = 0.0;
        double cleanupActiveIds = 0.0;
        double lightingCollectMs = 0.0;
        double lightingShadowEditorMs = 0.0;
        double lightingShadowGameMs = 0.0;
        double lightingUploadMs = 0.0;
    };
    FrameDetailTiming m_frameDetailTiming{};
#endif

    /// @brief Find effective game camera via Scene::FindGameCamera().
    /// Returns the highest-priority active Camera (by depth), excluding the editor camera.
    class Camera *FindGameCamera();

    /// @brief Find every active game camera in stable Camera.depth order.
    std::vector<class Camera *> FindGameCameras();

    /// @brief Per-frame cached version of FindGameCamera().
    /// First call per frame does the actual discovery; subsequent calls return cached result.
    class Camera *FindGameCameraCached();

    /// @brief Per-frame camera stack and per-camera RenderGraph ownership.
    const std::vector<class Camera *> &FindGameCamerasCached();
    SceneRenderGraph *EnsureGameRenderGraph(class Camera *camera);

    /// Callback invoked once per frame BEFORE GUI::BuildFrame().
    /// Used by Python to tick DeferredTaskRunner so that scene-mutating
    /// operations (deserialize, scene load) complete before any ImGui
    /// panel renders — preventing stale-reference hangs.
    std::function<void()> m_preGuiCallback;

    /// Callback invoked before SceneManager::Update with the raw frame delta.
    std::function<void(float)> m_preSceneUpdateCallback;

    /// Callback invoked once per frame AFTER VkCore::DrawFrame() + EndFrame().
    /// Used by Python to run heavy scene loads between frames, avoiding
    /// Windows "Not Responding" by running between SDL_PumpEvents() calls.
    std::function<void()> m_postDrawCallback;

    // ---- DrawFrame sub-methods (extracted for readability) ----

    /// @brief Check scene & game render graph MSAA requests; apply if changed.
    /// @return true if MSAA change was triggered and DrawFrame should return early.
    bool CheckAndApplyMsaaRequest(bool finalFrameCheck, bool sceneViewActive, bool gameViewActive);

    [[nodiscard]] uint32_t GetSupportedMsaaSampleMask() const;
    [[nodiscard]] bool ApplyMsaaSamples(int samples, const char *source);
    void SetEffectiveGraphMsaaSamples(int samples);

    int m_sceneRequestedMsaaSamples = 0;
    int m_gameRequestedMsaaSamples = 0;
    bool m_msaaRequestConflict = false;
    uint64_t m_msaaReconfigurationCount = 0;
    uint64_t m_msaaRejectedRequestCount = 0;
    uint64_t m_lastMsaaRejectionSignature = 0;

    /// @brief Build EngineGlobalsUBO and stage it for the current frame.
    void StageEngineGlobalsUBO();

    /// @brief Collect and merge draw calls from all active render graphs,
    /// then pass them to VkCore for unused buffer cleanup.
    void CleanupDrawCallBuffers();
};
} // namespace infernux
