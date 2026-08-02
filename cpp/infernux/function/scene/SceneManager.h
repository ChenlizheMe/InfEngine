#pragma once

#include "EditorCameraController.h"
#include "Scene.h"
#include <algorithm>
#include <chrono>
#include <cmath>
#include <functional>
#include <memory>
#include <stdexcept>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace infernux
{

// Forward declaration for MeshRenderer registry
class MeshRenderer;
// Forward declaration for Light registry
class Light;

/**
 * @brief SceneManager - singleton that manages all scenes.
 *
 * Handles scene loading, switching, and provides access to the active scene.
 * In editor mode, it also manages the editor scene camera.
 */
class SceneManager
{
  public:
    struct FrameProfile
    {
        double editorCameraMs = 0.0;
        double editorUpdateMs = 0.0;
        double pendingStartsMs = 0.0;
        double syncExternalMovesMs = 0.0;
        double syncCollidersMs = 0.0;
        double fixedUpdateMs = 0.0;
        double physicsStepMs = 0.0;
        double physicsEventsMs = 0.0;
        double syncRigidbodiesMs = 0.0;
        double interpolationMs = 0.0;
        double gameplayUpdateMs = 0.0;
        double lateUpdateMs = 0.0;
        double audioMs = 0.0;
        double endFrameMs = 0.0;
        double fixedSteps = 0.0;
        double colliderSyncCandidates = 0.0;
        double rigidbodySyncCandidates = 0.0;
        double interpolationCandidates = 0.0;
        double contactEvents = 0.0;
        double dynamicCCDSplits = 0.0;
    };

    // Singleton access
    static SceneManager &Instance();

    // Prevent copying
    SceneManager(const SceneManager &) = delete;
    SceneManager &operator=(const SceneManager &) = delete;

    // ========================================================================
    // Scene management
    // ========================================================================

    /// @brief Create a new empty scene
    Scene *CreateScene(const std::string &name);

    /// @brief Set the active scene
    void SetActiveScene(Scene *scene);

    /// @brief Get the currently active scene
    [[nodiscard]] Scene *GetActiveScene() const
    {
        return m_activeScene;
    }

    /// Publish an explicit time jump in the active world. This is intentionally
    /// separate from Pause/Resume and ordinary simulation updates.
    void MarkActiveSceneTemporalDiscontinuity() noexcept
    {
        if (m_activeScene)
            m_activeScene->MarkTemporalDiscontinuity();
    }

    /// @brief Unload a scene
    void UnloadScene(Scene *scene);

    /// @brief Unload all scenes
    void UnloadAllScenes();

    /// @brief Full engine-shutdown teardown.
    ///
    /// Destroys every scene, persistent (DontDestroyOnLoad) object, and the
    /// editor camera while the physics world and ECS stores are still alive.
    /// Must be called from Infernux::Cleanup() BEFORE PhysicsWorld::Shutdown();
    /// the SceneManager singleton itself is intentionally leaked so no scene
    /// teardown ever happens during C++ static destruction.
    void Shutdown();

    /// @brief Get a scene by name
    [[nodiscard]] Scene *GetScene(const std::string &name) const;

    /// @brief Get all loaded scenes
    [[nodiscard]] const std::vector<std::unique_ptr<Scene>> &GetAllScenes() const
    {
        return m_scenes;
    }

    [[nodiscard]] size_t GetSceneCount() const
    {
        return m_scenes.size();
    }

    // ========================================================================
    // Frame update
    // ========================================================================

    /// @brief Call at the start of the game (after first scene loads)
    void Start();

    /// @brief Call every frame
    void Update(float deltaTime);

    /// Flush deferred body creation and dirty static Collider poses before a
    /// scene query. Static-only scenes otherwise defer this work because they
    /// have no dynamics or contacts to simulate.
    void EnsurePhysicsQueriesCurrent();

    /// @brief Called at a fixed time step (physics / deterministic logic)
    void FixedUpdate();

    /// @brief Call every frame after Update
    void LateUpdate(float deltaTime);

    /// @brief Process pending destroys at end of frame
    void EndFrame();

    /// @brief Manually flush Transform changes to the physics engine.
    /// Unity equivalent: Physics.SyncTransforms().
    /// Useful when script code modifies transforms in Update and needs
    /// immediate physics queries (raycast, overlap) against the new positions.
    /// Normally called automatically before each physics step; calling it
    /// explicitly is only needed for same-frame queries after transform edits.
    void SyncTransforms();

    [[nodiscard]] const FrameProfile &GetLastFrameProfile() const
    {
        return m_lastFrameProfile;
    }

    [[nodiscard]] size_t GetLastColliderSyncCandidateCount() const
    {
        return static_cast<size_t>(m_lastFrameProfile.colliderSyncCandidates);
    }

    [[nodiscard]] size_t GetLastRigidbodySyncCandidateCount() const
    {
        return static_cast<size_t>(m_lastFrameProfile.rigidbodySyncCandidates);
    }

    [[nodiscard]] size_t GetLastInterpolationCandidateCount() const
    {
        return static_cast<size_t>(m_lastFrameProfile.interpolationCandidates);
    }

    // ========================================================================
    // DontDestroyOnLoad
    // ========================================================================

    /// @brief Mark a root GameObject so it survives scene switches.
    ///
    /// The object stays inside its current scene during normal operation;
    /// `UnloadScene` and `SetActiveScene` migrate persistent roots out of the
    /// dying scene into `m_persistentObjects`, then attach them to the new
    /// active scene. `Stop()` drops them — DontDestroyOnLoad survives scene
    /// switches but does NOT survive a play-session boundary.
    /// Unity: Object.DontDestroyOnLoad(gameObject)
    void DontDestroyOnLoad(GameObject *gameObject);

    /// @brief Get all persistent (DontDestroyOnLoad) roots currently parked
    /// outside any scene (between Unload and the next SetActiveScene).
    [[nodiscard]] const std::vector<std::unique_ptr<GameObject>> &GetPersistentObjects() const
    {
        return m_persistentObjects;
    }

    // ========================================================================
    // Editor support
    // ========================================================================

    /// @brief Get the editor camera controller
    [[nodiscard]] EditorCameraController &GetEditorCameraController()
    {
        return m_editorCamera;
    }

    /// @brief Is the scene in play mode?
    [[nodiscard]] bool IsPlaying() const
    {
        return m_isPlaying;
    }

    /// @brief Enter play mode.
    ///
    /// Resets the fixed-step accumulator (only on a fresh Play, not on resume),
    /// flips internal flags, calls `Scene::Start()` on the active scene, and
    /// force-syncs every Jolt body to its current Transform so the first frame
    /// runs against authored positions instead of stale editor values.
    void Play();

    /// @brief Publish the active Scene into an already-running play session.
    ///
    /// Runtime scene replacement keeps SceneManager in play mode, so it must
    /// rebuild the same transform/physics state that a fresh Play() creates.
    /// This is an engine lifecycle hook, not a gameplay scene-loading API.
    void StartActiveSceneForPlay();

    /// @brief Exit play mode.
    ///
    /// Flips `m_isPlaying`/`m_isPaused` to false, fires the play-state-changed
    /// callback, and clears `m_persistentObjects` (DontDestroyOnLoad does NOT
    /// outlive a play session). Scene snapshot restore is the responsibility
    /// of the Python `PlayModeManager.exit_play_mode` flow — Stop() itself
    /// does not deserialize anything.
    void Stop();

    /// @brief Set a callback that fires when Play()/Stop() transitions occur.
    void SetPlayStateChangedCallback(std::function<void(bool)> cb)
    {
        m_onPlayStateChanged = std::move(cb);
    }

    /// @brief Pause play mode
    void Pause();

    /// @brief Step exactly one frame while paused (Update + LateUpdate + EndFrame).
    /// Does nothing if not currently paused and playing.
    void Step(float deltaTime);

    [[nodiscard]] bool IsPaused() const
    {
        return m_isPaused;
    }

    /// @brief Get the fixed physics timestep in seconds.
    [[nodiscard]] float GetFixedTimeStep() const
    {
        return m_fixedTimeStep;
    }

    /// @brief Set the fixed physics timestep in seconds.
    void SetFixedTimeStep(float value)
    {
        if (!std::isfinite(value) || value < 0.001f)
            throw std::invalid_argument("fixed time step must be finite and at least 0.001 seconds");
        m_fixedTimeStep = value;
        m_maxFixedDeltaTime = std::max(m_maxFixedDeltaTime, m_fixedTimeStep);
    }

    /// @brief Get the max clamped frame delta used by the fixed-step accumulator.
    [[nodiscard]] float GetMaxFixedDeltaTime() const
    {
        return m_maxFixedDeltaTime;
    }

    /// @brief Set the max clamped frame delta used by the fixed-step accumulator.
    void SetMaxFixedDeltaTime(float value)
    {
        if (!std::isfinite(value) || value < m_fixedTimeStep)
            throw std::invalid_argument("max fixed delta time must be finite and not less than the fixed time step");
        m_maxFixedDeltaTime = value;
    }

    /// @brief Global gameplay time scale. Zero keeps Update running with dt=0
    ///        while suspending fixed-step simulation.
    [[nodiscard]] float GetTimeScale() const
    {
        return m_timeScale;
    }
    void SetTimeScale(float value)
    {
        if (!std::isfinite(value) || value < 0.0f)
            throw std::invalid_argument("time scale must be finite and non-negative");
        m_timeScale = value;
    }

    /// @brief Scaled simulation time at the current fixed step.
    [[nodiscard]] double GetFixedTime() const
    {
        return m_fixedTime;
    }

    /// @brief Real time represented by completed fixed steps.
    [[nodiscard]] double GetFixedUnscaledTime() const
    {
        return m_fixedUnscaledTime;
    }

    // ========================================================================
    // Callbacks
    // ========================================================================

    using SceneCallback = std::function<void(Scene *)>;

    void OnSceneLoaded(SceneCallback callback)
    {
        m_onSceneLoaded = callback;
    }
    void OnSceneUnloaded(SceneCallback callback)
    {
        m_onSceneUnloaded = callback;
    }

    // ========================================================================
    // Component registries
    // ========================================================================

    /// Clear MeshRenderer registry (called on scene unload / deserialize).
    void ClearComponentRegistries();

    /// Pre-allocate MeshRenderer registry storage for bulk creation.
    void ReserveRendererCapacity(size_t count);

    /// Register a MeshRenderer so rendering can iterate it directly.
    void RegisterMeshRenderer(MeshRenderer *renderer);

    /// Unregister a MeshRenderer (e.g. OnDisable / destruction).
    void UnregisterMeshRenderer(MeshRenderer *renderer);

    /// Bump the renderable cache version after a registered MeshRenderer
    /// changes mesh/material state without leaving the registry.
    void NotifyMeshRendererChanged(MeshRenderer *renderer);

    /// Read-only access to the active mesh renderers registry.
    [[nodiscard]] const std::vector<MeshRenderer *> &GetActiveMeshRenderers() const
    {
        return m_activeMeshRenderers;
    }

    /// Monotonic counter bumped when a MeshRenderer is registered/unregistered.
    [[nodiscard]] uint64_t GetMeshRendererVersion() const
    {
        return m_meshRendererVersion;
    }

    /// Mark all MeshRenderers referencing a given mesh GUID/path as buffer-dirty.
    void MarkMeshRenderersDirtyForAsset(const std::string &meshGuid, const std::string &meshPath = "");

    /// Register a Light so lighting can iterate it directly.
    void RegisterLight(Light *light);

    /// Unregister a Light (e.g. OnDisable / destruction).
    void UnregisterLight(Light *light);

    /// Read-only access to the active lights registry.
    [[nodiscard]] const std::vector<Light *> &GetActiveLights() const
    {
        return m_activeLights;
    }

  private:
    friend class SceneCommitToken;

    SceneManager();
    ~SceneManager() = default;

    /// Walk all colliders in the active scene and sync transforms to Jolt.
    /// Uses a global transform serial to skip entirely when no transforms changed.
    void SyncCollidersToPhysics(float fixedDeltaTime = 0.0f);

    /// Flush pending broadphase additions (batched from Collider::AddToBroadphase).
    /// Also rebuilds the BVH tree when new bodies were added.
    void FlushPendingBroadphase();

    /// Force-sync ALL collider body positions to their current Transform,
    /// including dynamic bodies (which SyncCollidersToPhysics normally skips).
    /// Called once at the start of play to fix stale editor-mode positions.
    void ForceAllBodiesToCurrentTransform();

    /// Activate all dynamic (non-kinematic) rigidbodies so they are awake
    /// when play mode starts.  Jolt bodies default to sleeping and won't
    /// respond to gravity until explicitly activated.
    void ActivateAllDynamicBodies();

    /// Write active Jolt body poses back to their owning Rigidbody transforms.
    void SyncRigidbodiesToTransform();

    /// Apply presentation interpolation for the latest dense active-body set.
    void ApplyInterpolatedRigidbodies(float alpha);

    /// Detach persistent (DontDestroyOnLoad) root objects from a scene
    /// into m_persistentObjects, keeping them alive across scene switches.
    void ExtractPersistentObjects(Scene *scene);

    std::vector<std::unique_ptr<Scene>> m_scenes;
    Scene *m_activeScene = nullptr;

    // Editor camera (exists even when no scene is loaded)
    std::unique_ptr<GameObject> m_editorCameraObject;
    Camera *m_editorCameraComponent = nullptr;
    EditorCameraController m_editorCamera;

    // Persistent objects (DontDestroyOnLoad)
    std::vector<std::unique_ptr<GameObject>> m_persistentObjects;

    // Fixed-update timing
    float m_fixedTimeStep = 1.0f / 50.0f; // 50 Hz default (Unity default)
    float m_fixedTimeAccumulator = 0.0f;
    float m_maxFixedDeltaTime = 0.1f; // cap to avoid spiral-of-death
    float m_timeScale = 1.0f;
    float m_lastScaledDeltaTime = 0.0f;
    double m_fixedTime = 0.0;
    double m_fixedUnscaledTime = 0.0;

    // Play mode state
    bool m_isPlaying = false;
    bool m_isPaused = false;

    // Callbacks
    SceneCallback m_onSceneLoaded;
    SceneCallback m_onSceneUnloaded;
    /// Called from Play()/Stop() so the renderer can bypass idle sleep.
    std::function<void(bool)> m_onPlayStateChanged;

    // MeshRenderer component registry — populated by MeshRenderer OnEnable/OnDisable.
    // Avoids per-frame GetAllObjects() + dynamic_cast in CollectRenderables.
    std::vector<MeshRenderer *> m_activeMeshRenderers;
    std::unordered_set<MeshRenderer *> m_activeMeshRendererSet; // O(1) duplicate check
    uint64_t m_meshRendererVersion = 0;

    // Light component registry — populated by Light OnEnable/OnDisable.
    // Avoids per-frame GetAllObjects() + GetComponent<Light>() in CollectLights/ComputeShadowVP.
    std::vector<Light *> m_activeLights;

    // Full generation-aware body IDs that still need presentation updates.
    std::vector<uint32_t> m_posePresentationBodyIds;

    FrameProfile m_lastFrameProfile;
};

} // namespace infernux
