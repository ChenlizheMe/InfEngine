#pragma once
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <core/threading/JobSystem.h>
#include <core/types/InxFwdType.h>
#include <core/types/ShaderProgramArtifact.h>
#include <filesystem>
#include <functional>
#include <iostream>
#include <memory>
#include <mutex>
#include <queue>
#include <tuple>
#include <unordered_map>
#include <vector>

#include <function/renderer/InxRenderer.h>
#include <function/scene/EditorCameraController.h>
#include <function/scene/SceneManager.h>

#include <core/error/InxError.h>
#include <core/log/InxLog.h>
#include <platform/filesystem/InxExtLoad.h>
#include <platform/filesystem/InxPath.h>
#include <platform/input/InputManager.h>

#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/AssetRuntimeRecord.h>

namespace infernux
{

enum class RuntimeMode
{
    Graphical,
    Headless,
};

class Infernux
{
  public:
    Infernux(std::string dllPath, RuntimeMode mode = RuntimeMode::Graphical);
    ~Infernux();

    // Prevent copying
    Infernux(const Infernux &) = delete;
    Infernux &operator=(const Infernux &) = delete;
    Infernux(Infernux &&) = delete;
    Infernux &operator=(Infernux &&) = delete;

    void Run();
    void Tick(float deltaTime);
    void SetPreSceneUpdateCallback(std::function<void(float)> callback);
    void Exit();
    [[nodiscard]] bool IsExitRequested() const
    {
        return m_exitRequested.load(std::memory_order_acquire);
    }
    void Cleanup();

    void InitHeadless(const std::string &projectPath, const std::string &builtinResourcePath = "");

    [[nodiscard]] RuntimeMode GetRuntimeMode() const noexcept
    {
        return m_runtimeMode;
    }

    // renderer
    void InitRenderer(int width, int height, const std::string &projectPath,
                      const std::string &builtinResourcePath = "");
    void ResetImGuiLayout();
    void SelectDockedWindow(const std::string &windowId);

    // Automation input is queued and consumed by the graphical event loop.
    // It is kept separate from gameplay's InputManager query API.
    uint64_t QueueSyntheticKeyInput(int scancode, bool pressed, bool repeat = false);
    uint64_t QueueSyntheticMouseButtonInput(int button, bool pressed, float x, float y);
    uint64_t QueueSyntheticMouseMotionInput(float x, float y, float deltaX, float deltaY);
    uint64_t QueueSyntheticMouseWheelInput(float horizontal, float vertical);
    uint64_t QueueSyntheticTextInput(const std::string &text);
    uint64_t QueueSyntheticCloseRequest();
    [[nodiscard]] uint64_t GetLastProcessedSyntheticInputSequence() const;
    [[nodiscard]] size_t GetPendingSyntheticInputCount() const;

    /// @brief Get the asset database instance
    /// @return Pointer to AssetDatabase, or nullptr if not initialized
    AssetDatabase *GetAssetDatabase() const;
    [[nodiscard]] std::vector<AssetRuntimeRecord> GetAssetRuntimeRecords() const;

    // ========================================================================
    // Scene Camera Control API - for Scene View with Unity-style controls
    // ========================================================================

    /// @brief Get the editor camera controller (property-based access).
    /// @return Pointer to EditorCameraController, or nullptr if not valid
    EditorCameraController *GetEditorCamera();

    /// @brief Process scene view input (call from Python when scene view is hovered/focused)
    /// @param deltaTime Time since last frame
    /// @param rightMouseDown Is right mouse button held
    /// @param middleMouseDown Is middle mouse button held
    /// @param mouseDeltaX Mouse movement X
    /// @param mouseDeltaY Mouse movement Y
    /// @param scrollDelta Mouse wheel scroll
    /// @param keyW W key held
    /// @param keyA A key held
    /// @param keyS S key held
    /// @param keyD D key held
    /// @param keyQ Q key held
    /// @param keyE E key held
    /// @param keyShift Shift key held
    void ProcessSceneViewInput(float deltaTime, bool rightMouseDown, bool middleMouseDown, float mouseDeltaX,
                               float mouseDeltaY, float scrollDelta, bool keyW, bool keyA, bool keyS, bool keyD,
                               bool keyQ, bool keyE, bool keyShift);

    // ========================================================================
    // Scene Picking API - for editor selection (screen-space to object)
    // ========================================================================

    /// @brief Pick a scene object by screen-space coordinates in the scene view
    /// @param screenX Mouse X in pixels relative to the scene viewport
    /// @param screenY Mouse Y in pixels relative to the scene viewport
    /// @param viewportWidth Scene viewport width in pixels
    /// @param viewportHeight Scene viewport height in pixels
    /// @return Picked GameObject ID, or 0 if none
    uint64_t PickSceneObjectId(float screenX, float screenY, float viewportWidth, float viewportHeight);

    /// @brief Pick all feasible scene objects under screen-space coordinates, nearest first.
    /// @param screenX Mouse X in pixels relative to the scene viewport
    /// @param screenY Mouse Y in pixels relative to the scene viewport
    /// @param viewportWidth Scene viewport width in pixels
    /// @param viewportHeight Scene viewport height in pixels
    /// @return Ordered candidate GameObject IDs (nearest first), deduplicated.
    std::vector<uint64_t> PickSceneObjectIds(float screenX, float screenY, float viewportWidth, float viewportHeight);

    /// @brief Lightweight gizmo-only handle proximity test (no scene raycast).
    /// Used every frame for hover highlighting — tests axis and plane handles.
    /// @return Gizmo handle ID, or 0 if not hovering any handle.
    uint64_t PickGizmoAxis(float screenX, float screenY, float viewportWidth, float viewportHeight);

    // ========================================================================
    // Editor Tools API — highlight + ray + mode for Python-side gizmo interaction
    // ========================================================================

    /// @brief Set the highlighted gizmo handle. 0=None, 1=X, 2=Y, 3=Z, 4=XY, 5=XZ, 6=YZ.
    void SetEditorToolHighlight(int axis);

    /// @brief Set the active tool mode. 0=None, 1=Translate, 2=Rotate, 3=Scale.
    void SetEditorToolMode(int mode);

    /// @brief Get the active tool mode. 0=None, 1=Translate, 2=Rotate, 3=Scale.
    int GetEditorToolMode() const;

    /// @brief Set local coordinate mode for editor tools (gizmo aligns to object rotation).
    void SetEditorToolLocalMode(bool local);

    /// @brief Build a world-space ray from screen coordinates (same math as picking).
    /// @return (originX, originY, originZ, dirX, dirY, dirZ)
    std::tuple<float, float, float, float, float, float> ScreenToWorldRay(float screenX, float screenY,
                                                                          float viewportWidth, float viewportHeight);

    // ========================================================================
    // Editor Gizmos API
    // ========================================================================

    /// @brief Set selection outline for a game object (Unity-style orange wireframe)
    /// @param objectId The ID of the object to show selection for, or 0 to clear
    void SetSelectionOutline(uint64_t objectId);

    /// @brief Set combined selection outline for multiple objects
    /// @param objectIds List of object IDs to show combined outline for
    void SetSelectionOutlines(const std::vector<uint64_t> &objectIds);

    /// @brief Get the currently selected object ID (0 if none)
    [[nodiscard]] uint64_t GetSelectedObjectId() const
    {
        return m_selectedObjectId;
    }

    /// @brief Clear selection outline
    void ClearSelectionOutline();

    // ========================================================================
    // Material Pipeline API
    // ========================================================================

    /// @brief Refresh a material's pipeline by reloading shaders
    /// @param material The material to refresh
    /// @return true if successful, false otherwise
    bool RefreshMaterialPipeline(std::shared_ptr<InxMaterial> material);

    /// @brief Reload a shader from file (hot-reload support)
    /// @param shaderPath The path to the shader file (.vert or .frag)
    /// @return true if successful, false otherwise
    /// @brief Reload a shader file and refresh materials using it.
    /// @return Empty string on success, or error message on failure.
    std::string ReloadShaderRuntime(const std::string &shaderPath, const std::string &previousShaderId);

    /// @brief Invalidate and reload a texture after import settings change
    /// @param texturePath The texture file path whose .meta was updated
    void ReloadTexture(const std::string &texturePath);

    /// @brief Reload a mesh asset after import settings change and notify dependents
    /// @param meshPath The mesh file path whose .meta was updated
    void ReloadMesh(const std::string &meshPath);

    /// @brief Reload an audio clip asset and notify dependents
    /// @param audioPath The audio file path to reload
    void ReloadAudio(const std::string &audioPath);

    /// @brief Combined query + schedule for material preview.
    ///
    /// Returns the current ImGui texture id (stale-return for anti-flicker).
    /// Internally manages a monotonic generation counter; re-renders only
    /// when the content actually changes (JSON hash or file mtime).
    ///
    /// @param materialJson  If non-empty, render from this JSON (Inspector live edits).
    /// @param fileMtimeHint If != 0 and materialJson is empty, used to detect file changes (ProjectPanel).
    uint64_t QueryOrScheduleMaterialPreview(const std::string &resourceKey, const std::string &matFilePath,
                                            const std::string &materialJson = "", uint64_t fileMtimeHint = 0);

    struct PreviewTaskSnapshot
    {
        std::string kind;
        std::string resourceKey;
        std::string textureName;
        uint64_t generation = 0;
        uint64_t readyGeneration = 0;
        uint64_t pendingUploadVersion = 0;
        uint64_t pendingPreviewGeneration = 0;
        uint64_t publishedUploadVersion = 0;
        uint64_t failedUploadVersion = 0;
        uint64_t textureId = 0;
        bool inFlight = false;
        bool hasRenderTicket = false;
        bool renderTicketDone = false;
        int pendingWidth = 0;
        int pendingHeight = 0;
        int readyWidth = 0;
        int readyHeight = 0;
        uint64_t pixelGeneration = 0;
        uint64_t pixelHash = 0;
        uint32_t nonTransparentPixelCount = 0;
        uint8_t minRgb = 0;
        uint8_t maxRgb = 0;
        uint32_t imguiDrawCommandCount = 0;
    };

    /// @brief Read-only diagnostics for active preview state machines.
    std::vector<PreviewTaskSnapshot> GetPreviewTaskSnapshots() const;

    /// @brief Pump completed preview tasks on main thread and upload textures.
    void PumpPreviewTasks();

    /// @brief Get uploaded texture id for a material preview key.
    /// @return Non-zero ImGui texture id when available (stale-return for anti-flicker).
    uint64_t GetMaterialPreviewTextureId(const std::string &resourceKey) const;

    /// @brief Check whether the uploaded material preview matches the latest requested generation.
    bool IsMaterialPreviewReady(const std::string &resourceKey) const;

    /// @brief Get uploaded texture id for a texture preview key.
    /// @return Non-zero ImGui texture id when available (stale-return for anti-flicker).
    uint64_t GetTexturePreviewTextureId(const std::string &resourceKey) const;

    /// @brief Get uploaded texture preview dimensions.
    /// @return (width,height); (0,0) when not ready.
    std::pair<int, int> GetTexturePreviewSize(const std::string &resourceKey) const;

    /// @brief Invalidate one material preview task/cache entry.
    void InvalidateMaterialPreviewTask(const std::string &resourceKey);

    /// @brief Invalidate one texture preview task/cache entry.
    void InvalidateTexturePreviewTask(const std::string &resourceKey);

    /// @brief Combined query + schedule for texture preview.
    ///
    /// Returns (textureId, width, height).  Internally manages a monotonic
    /// generation counter; re-renders only when content changes.
    ///
    /// @param contentStampHint Caller-provided content hash (mtime combo, etc.).
    ///        C++ uses this to detect changes and bump the generation counter.
    std::tuple<uint64_t, int, int> QueryOrScheduleTexturePreview(const std::string &resourceKey,
                                                                 const std::string &textureFilePath,
                                                                 uint64_t contentStampHint, bool nearest, bool srgb,
                                                                 bool pump);

    /// @brief Schedule texture preview from in-memory data (JPEG/PNG/etc.).
    ///
    /// The image bytes are decoded on a worker thread via stb_image.
    /// @param resourceKey Stable cache key
    /// @param imageData Raw encoded image bytes (JPEG, PNG, etc.)
    /// @param stamp Revision stamp
    /// @param nearest Use point filter for uploaded texture
    /// @return true if task accepted
    bool ScheduleTexturePreviewFromMemory(const std::string &resourceKey, std::vector<unsigned char> imageData,
                                          uint64_t stamp, bool nearest);

    /// @brief Combined query + schedule for mesh/model preview.
    ///
    /// Returns the current ImGui texture id (stale-return for anti-flicker).
    /// Internally manages a monotonic generation counter; re-renders only
    /// when the content changes (file mtime).
    ///
    /// @param resourceKey    Stable cache key (e.g. "mesh|<norm_path>")
    /// @param meshFilePath   Path to the model file (.fbx, .obj, .gltf, ...)
    /// @param fileMtimeHint  File mtime for change detection (0 = unknown)
    /// @return ImGui texture id (0 if not ready yet)
    uint64_t QueryOrScheduleMeshPreview(const std::string &resourceKey, const std::string &meshFilePath,
                                        uint64_t fileMtimeHint = 0);

    /// @brief Queue a Timeline cube preview render (non-blocking). Returns the latest
    ///        ImGui texture id immediately; GPU work runs in PumpPreviewTasks().
    uint64_t RenderTimelineCubePreview(float px, float py, float pz, float rx, float ry, float rz, float sx, float sy,
                                       float sz, float camYaw, float camPitch, float camDistance, int size);

    /// @brief Execute a pending Timeline cube preview render if one was queued this frame.
    void PumpTimelineCubePreviewIfDirty();
    /// Process queued material preview renders (returns uploads consumed).
    int PumpMaterialPreviewUploads(int uploadBudget, bool ignoreCooldown);

    /// @brief Schedule async material save from JSON snapshot.
    /// @param key Coalescing key (usually file path)
    /// @param filePath Target .mat path
    /// @param jsonSnapshot Serialized material JSON snapshot
    /// @return true if task accepted
    bool ScheduleMaterialSaveSnapshotTask(const std::string &key, const std::string &filePath,
                                          const std::string &jsonSnapshot);

    // debug
    void SetLogLevel(LogLevel engineLevel);

    // State check
    [[nodiscard]] bool IsCleanedUp() const
    {
        return m_isCleanedUp;
    }
    [[nodiscard]] bool IsCleaningUp() const
    {
        return m_isCleaningUp;
    }
    [[nodiscard]] bool IsInitialized() const
    {
        return m_isInitialized && !m_isCleanedUp;
    }

    /// @brief Get the renderer subsystem for direct access.
    /// @return Pointer to InxRenderer, or nullptr if not initialized / cleaned up
    [[nodiscard]] InxRenderer *GetRenderer() const
    {
        return (m_isCleanedUp || !m_renderer) ? nullptr : m_renderer.get();
    }

  private:
    /// @brief Check if engine is valid for operations
    [[nodiscard]] bool CheckEngineValid(const char *operation) const;

    /// @brief Ensure a shader is loaded in the renderer
    /// @param shaderId The shader_id to check/load
    /// @param shaderType "vertex" or "fragment"
    /// @return true if shader is loaded, false otherwise
    bool EnsureShaderLoaded(const std::string &shaderId, const std::string &shaderType);

    /// @brief Load shaders from a directory via AssetRegistry and register them with the renderer.
    /// @param dir Directory to scan for .vert/.frag files
    /// @param recursive If true, scan subdirectories recursively
    void LoadAndRegisterShaders(const std::string &dir, bool recursive);

    /// @brief Push a compiled ShaderAsset into the renderer (SPIR-V + variants + render meta).
    void RegisterShaderToRenderer(const struct ShaderAsset &asset);

    struct LinkedShaderProgramPreparation
    {
        bool usesLinkedArtifact = false;
        bool success = true;
        std::string error;
    };

    struct LinkedShaderProgramCacheEntry
    {
        uint64_t sourceStamp = 0;
        ShaderProgramKey programKey;
        uint64_t failedSourceStamp = 0;
        std::string lastError;
    };

    [[nodiscard]] LinkedShaderProgramPreparation EnsureLinkedShaderProgramArtifact(const ShaderStagePair &stages);
    [[nodiscard]] LinkedShaderProgramPreparation EnsureLinkedShaderProgramArtifact(const ShaderStagePair &stages,
                                                                                   const std::string &vertexPath,
                                                                                   const std::string &fragmentPath);
    [[nodiscard]] LinkedShaderProgramPreparation
    EnsureLinkedShaderProgramArtifact(const std::shared_ptr<InxMaterial> &material);

    std::unordered_map<ShaderStagePair, LinkedShaderProgramCacheEntry, ShaderStagePairHash> m_linkedShaderProgramCache;

    struct TexturePreviewCompleted
    {
        std::string resourceKey;
        uint64_t generation = 0;
        int width = 0;
        int height = 0;
        bool nearest = false;
        bool success = false;
        std::vector<unsigned char> pixels;
    };

    struct MaterialPreviewRequest
    {
        std::string resourceKey;
        std::string matFilePath;
        uint64_t generation = 0;
        std::string materialJson; ///< If non-empty, render from this JSON instead of reading matFilePath from disk.
    };

    struct TexturePreviewRequest
    {
        std::string resourceKey;
        std::string textureFilePath;
        uint64_t generation = 0;
        bool nearest = false;
        bool srgb = false;
    };

    struct MeshPreviewRequest
    {
        std::string resourceKey;
        std::string meshFilePath;
        uint64_t generation = 0;
    };

    struct MaterialPreviewState
    {
        uint64_t generation = 0;      ///< Monotonic counter, bumped on detected content change
        uint64_t readyGeneration = 0; ///< Generation of last completed render
        uint64_t lastJsonHash = 0;    ///< std::hash of last JSON string seen
        uint64_t lastFileMtime = 0;   ///< Last file mtime seen from ProjectPanel
        uint64_t pendingUploadVersion = 0;
        uint64_t pendingPreviewGeneration = 0;
        bool inFlight = false;
        uint64_t renderGeneration = 0;
        std::shared_ptr<vk::ImageReadbackTicket> renderTicket;
        std::shared_ptr<InxMaterial> renderMaterial;
        int pendingSize = 0;
        int readySize = 0;
        std::string textureName;
        uint64_t textureId = 0;
        uint64_t pixelGeneration = 0;
        uint64_t pixelHash = 0;
        uint32_t nonTransparentPixelCount = 0;
        uint8_t minRgb = 0;
        uint8_t maxRgb = 0;
    };

    struct TexturePreviewState
    {
        uint64_t generation = 0;       ///< Monotonic counter, bumped on detected content change
        uint64_t readyGeneration = 0;  ///< Generation of last completed render
        uint64_t lastContentStamp = 0; ///< Last content stamp seen from caller
        uint64_t pendingUploadVersion = 0;
        uint64_t pendingPreviewGeneration = 0;
        bool inFlight = false;
        int pendingWidth = 0;
        int pendingHeight = 0;
        int readyWidth = 0;
        int readyHeight = 0;
        std::string textureName;
        uint64_t textureId = 0;
        bool nearest = false;
        bool srgb = false;
        uint64_t pixelGeneration = 0;
        uint64_t pixelHash = 0;
        uint32_t nonTransparentPixelCount = 0;
        uint8_t minRgb = 0;
        uint8_t maxRgb = 0;
    };

    struct MeshPreviewState
    {
        uint64_t generation = 0;
        uint64_t readyGeneration = 0;
        uint64_t lastFileMtime = 0;
        uint64_t pendingUploadVersion = 0;
        uint64_t pendingPreviewGeneration = 0;
        bool inFlight = false;
        uint64_t renderGeneration = 0;
        std::shared_ptr<vk::ImageReadbackTicket> renderTicket;
        int pendingSize = 0;
        int readySize = 0;
        std::string textureName;
        uint64_t textureId = 0;
        std::string meshFilePath; ///< Absolute path to model file
        uint64_t pixelGeneration = 0;
        uint64_t pixelHash = 0;
        uint32_t nonTransparentPixelCount = 0;
        uint8_t minRgb = 0;
        uint8_t maxRgb = 0;
    };

    void EnqueuePreviewTask(std::function<void()> fn);
    static std::string BuildPreviewTextureName(const std::string &resourceKey);
    static std::string BuildTexturePreviewTextureName(const std::string &resourceKey);
    static std::string BuildMeshPreviewTextureName(const std::string &resourceKey);
    void CommitPublishedPreviewTextures();
    void DrainPreviewJobs();

    InxAppMetadata m_metadata{"Infernux", 0, 1, 0, "com.infrenderer.Infernux"};
    RuntimeMode m_runtimeMode = RuntimeMode::Graphical;

    std::unique_ptr<InxExtLoad> m_extLoader;
    std::unique_ptr<AssetDatabase> m_assetDatabase;
    std::unique_ptr<InxRenderer> m_renderer;

    LogLevel m_logLevel = LogLevel::LOG_INFO;
    bool m_isCleanedUp = false;
    bool m_isCleaningUp = false;
    bool m_isInitialized = false;
    std::atomic<bool> m_exitRequested{false};
    std::mutex m_runMutex;
    std::condition_variable m_runCv;
    std::function<void(float)> m_preSceneUpdateCallback;

    // Selection tracking for outline updates
    uint64_t m_selectedObjectId = 0;
    std::vector<uint64_t> m_cachedOutlineIds; ///< Last set of IDs passed to SetSelectionOutlines

    // ImGui ini file path — stored as std::filesystem::path so that
    // wide-char paths (e.g. Chinese usernames) work correctly on Windows.
    std::filesystem::path m_imguiIniPath;

    std::queue<std::function<void()>> m_previewJobs;
    JobHandle m_previewDispatcherJob;
    std::mutex m_previewJobMutex;
    bool m_acceptPreviewJobs = true;
    bool m_previewDispatcherScheduled = false;

    mutable std::mutex m_previewResultMutex;
    std::queue<TexturePreviewCompleted> m_texturePreviewCompletedQueue;
    std::queue<MaterialPreviewRequest> m_previewRequestQueue;
    std::queue<MeshPreviewRequest> m_meshPreviewRequestQueue;
    std::unordered_map<std::string, MaterialPreviewState> m_materialPreviewStates;
    std::unordered_map<std::string, TexturePreviewState> m_texturePreviewStates;
    std::unordered_map<std::string, MeshPreviewState> m_meshPreviewStates;
    std::atomic_bool m_hasPendingPreviewUploads{false};
    std::atomic_bool m_hasPreviewPumpWork{false};
    int m_lastPumpFrame = -1;
    std::chrono::steady_clock::time_point m_lastMaterialRenderTime{};

    // Timeline-editor cube preview: query schedules, PumpTimelineCubePreviewIfDirty renders.
    uint64_t m_cubePreviewTexId = 0;
    uint64_t m_lastCubePreviewHash = 0;
    uint64_t m_pendingCubePreviewHash = 0;
    bool m_timelineCubeDirty = false;
    float m_pendingCubePx = 0, m_pendingCubePy = 0, m_pendingCubePz = 0;
    float m_pendingCubeRx = 0, m_pendingCubeRy = 0, m_pendingCubeRz = 0;
    float m_pendingCubeSx = 1, m_pendingCubeSy = 1, m_pendingCubeSz = 1;
    float m_pendingCubeCamYaw = 0, m_pendingCubeCamPitch = 0, m_pendingCubeCamDist = 6;
    int m_pendingCubeSize = 240;
    std::shared_ptr<InxMaterial> m_cubePreviewCubeMat;
    std::shared_ptr<InxMaterial> m_cubePreviewFloorMat;
    std::vector<Vertex> m_cubePreviewFloorVerts;
    std::vector<uint32_t> m_cubePreviewFloorIndices;
    bool m_cubePreviewFloorBuilt = false;

    bool ExecuteTimelineCubePreviewRender(float px, float py, float pz, float rx, float ry, float rz, float sx,
                                          float sy, float sz, float camYaw, float camPitch, float camDistance, int size,
                                          uint64_t hash);

    void LoadImGuiLayout();
    void SaveImGuiLayout();
};
} // namespace infernux
