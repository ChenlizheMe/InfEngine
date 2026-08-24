// Jolt types hidden behind opaque headers — no Jolt include needed
#include "SceneManager.h"
#include "Collider.h"
#include "EditorCameraController.h"
#include "GameObject.h"
#include "Light.h"
#include "MeshCollider.h"
#include "MeshRenderer.h"
#include "Rigidbody.h"
#include "SkinnedMeshRenderer.h"
#include "Transform.h"
#include "TransformECSStore.h"
#include "physics/PhysicsECSStore.h"
#include "physics/PhysicsWorld.h"
#include <InxLog.h>
#include <algorithm>
#include <function/audio/AudioEngine.h>
#include <platform/input/InputManager.h>

namespace
{
using ProfileClock = std::chrono::high_resolution_clock;

double ProfileMsSince(ProfileClock::time_point start)
{
    return std::chrono::duration<double, std::milli>(ProfileClock::now() - start).count();
}
} // namespace

namespace infernux
{

namespace
{
void UpdateCapturedEditorCamera(EditorCameraController &controller, float deltaTime)
{
    InputManager &input = InputManager::Instance();
    const bool captured = input.IsEditorMouseCaptureActive();
    const bool rightDown = captured && input.GetMouseButton(1);
    const bool middleDown = captured && input.GetMouseButton(2);

    static bool previousRightDown = false;
    static bool previousMiddleDown = false;
    if (rightDown != previousRightDown) {
        if (rightDown)
            controller.OnMouseButtonDown(1, 0.0f, 0.0f);
        else
            controller.OnMouseButtonUp(1, 0.0f, 0.0f);
        previousRightDown = rightDown;
    }
    if (middleDown != previousMiddleDown) {
        if (middleDown)
            controller.OnMouseButtonDown(2, 0.0f, 0.0f);
        else
            controller.OnMouseButtonUp(2, 0.0f, 0.0f);
        previousMiddleDown = middleDown;
    }

    if (captured) {
        const auto [deltaX, deltaY] = input.ConsumeEditorMouseDelta();
        if (rightDown && (deltaX != 0.0f || deltaY != 0.0f))
            controller.ApplyRotation(deltaX, deltaY);
        else if (middleDown && (deltaX != 0.0f || deltaY != 0.0f))
            controller.ApplyPan(deltaX, deltaY);
    }

    const auto updateKey = [&](SDL_Scancode scancode, int controllerKey) {
        if (rightDown && input.GetKey(static_cast<int>(scancode)))
            controller.OnKeyDown(controllerKey);
        else
            controller.OnKeyUp(controllerKey);
    };
    updateKey(SDL_SCANCODE_W, 'W');
    updateKey(SDL_SCANCODE_A, 'A');
    updateKey(SDL_SCANCODE_S, 'S');
    updateKey(SDL_SCANCODE_D, 'D');
    updateKey(SDL_SCANCODE_Q, 'Q');
    updateKey(SDL_SCANCODE_E, 'E');
    const bool shiftDown = rightDown && (input.GetKey(SDL_SCANCODE_LSHIFT) || input.GetKey(SDL_SCANCODE_RSHIFT));
    if (shiftDown)
        controller.OnKeyDown(SDL_SCANCODE_LSHIFT);
    else
        controller.OnKeyUp(SDL_SCANCODE_LSHIFT);

    controller.Update(std::max(deltaTime, 0.0f));
}
} // namespace

SceneManager &SceneManager::Instance()
{
    // Intentionally leaked: scene teardown is driven explicitly by
    // Infernux::Cleanup() -> Shutdown(); never by C++ static destruction,
    // whose cross-TU ordering vs the ECS stores is undefined (was the root
    // cause of the shutdown heap corruption / DLL-unload access violations).
    static SceneManager *instance = new SceneManager();
    return *instance;
}

SceneManager::SceneManager()
{
    TransformECSStore::Instance().SetInvalidationObserver([this](Transform *transform) {
        auto *gameObject = transform ? transform->GetGameObject() : nullptr;
        if (!gameObject)
            return;
        PhysicsECSStore::Instance().MarkGameObjectDirty(gameObject);
        Scene *scene = gameObject->GetScene();
        // A moving camera, light, audio source, or UI element must not force
        // the renderer to rescan every geometry proxy. Invalidate the shared
        // RenderWorld only for objects that actually contribute geometry.
        // Transform invalidation walks descendants, so moving a plain parent
        // still reaches and invalidates every child MeshRenderer correctly.
        if (scene && IsRuntimeScene(scene) && gameObject->GetComponent<MeshRenderer>()) {
            ++m_renderTransformRevision;
            if (m_renderTransformRevision == 0)
                m_renderTransformRevision = 1;
        }
    });

    // Create editor camera
    m_editorCameraObject = std::make_unique<GameObject>("Editor Camera");
    m_editorCameraComponent = m_editorCameraObject->AddComponent<Camera>();
    m_editorCamera.SetCamera(m_editorCameraComponent);
    m_editorCamera.Reset(); // Set default position
}

uint64_t SceneManager::GetGlobalTransformSerial() const
{
    return TransformECSStore::Instance().GetGlobalTransformSerial();
}

void SceneManager::PublishPhysicsTransformsToRenderer() noexcept
{
    ++m_renderTransformRevision;
    if (m_renderTransformRevision == 0)
        m_renderTransformRevision = 1;
}

Scene *SceneManager::CreateScene(const std::string &name)
{
    auto scene = std::make_unique<Scene>(name);
    scene->SetRuntimeLifecycleSchedulerEnabled(m_runtimeLifecycleSchedulerEnabled && m_runtimeLifecycleWorkAvailable);
    Scene *ptr = scene.get();
    m_scenes.push_back(std::move(scene));

    // If no active scene, make this one active
    if (!m_activeScene) {
        SetActiveScene(ptr);
    }

    if (m_onSceneLoaded) {
        m_onSceneLoaded(ptr);
    }

    return ptr;
}

void SceneManager::SetActiveScene(Scene *scene)
{
    const bool activeSceneChanged = scene != m_activeScene;
    if (activeSceneChanged && m_isPlaying)
        FlushPersistentPromotions();

    m_activeScene = scene;
    if (activeSceneChanged) {
        m_fixedTimeAccumulator = 0.0f;
        m_lastScaledDeltaTime = 0.0f;
        m_resetDeltaTimeOnNextFrame = true;
    }
    if (m_activeScene) {
        m_activeScene->SetRuntimeLifecycleSchedulerEnabled(m_runtimeLifecycleSchedulerEnabled &&
                                                           m_runtimeLifecycleWorkAvailable);
        m_activeScene->SetPlaying(m_isPlaying);
    }

    // Note: We do NOT auto-assign the editor camera as mainCamera.
    // mainCamera == nullptr means "no game camera assigned" — the Game View
    // will show a placeholder. Scene View always uses the editor camera
    // via SceneRenderBridge / EditorCameraController, independent of mainCamera.
}

float SceneManager::ConsumeFrameDeltaTime(float deltaTime) noexcept
{
    if (!m_resetDeltaTimeOnNextFrame)
        return deltaTime;
    m_resetDeltaTimeOnNextFrame = false;
    return 0.0f;
}

void SceneManager::UnloadScene(Scene *scene)
{
    if (!scene)
        return;

    if (m_isPlaying)
        FlushPersistentPromotions();

    const bool wasActive = m_activeScene == scene;
    Scene *replacement = nullptr;
    if (wasActive) {
        for (const auto &candidate : m_scenes) {
            if (candidate && candidate.get() != scene) {
                replacement = candidate.get();
                break;
            }
        }
        // Every active-scene transition goes through the same publication
        // path. Never leave registries and lifecycle flags behind by assigning
        // m_activeScene directly.
        SetActiveScene(replacement);
    }

    if (m_onSceneUnloaded) {
        m_onSceneUnloaded(scene);
    }

    auto it = std::find_if(m_scenes.begin(), m_scenes.end(),
                           [scene](const std::unique_ptr<Scene> &s) { return s.get() == scene; });

    if (it != m_scenes.end()) {
        m_scenes.erase(it);
    }
}

void SceneManager::Shutdown()
{
    // Mesh cooking uses immutable snapshots, but Jolt's geometry helpers must
    // finish before PhysicsWorld and its global factory are torn down.
    MeshCollider::FlushCompletedCooking(true);

    // No play-mode restore logic here — this is irreversible engine teardown.
    m_isPlaying = false;
    m_isPaused = false;

    // Destroy the runtime-only persistent Scene first so its colliders release
    // Jolt bodies while PhysicsWorld is still initialized.
    ClearRuntimePersistentScene();

    // Destroy all scenes (GameObjects → Components → Colliders → bodies).
    UnloadAllScenes();

    // Destroy the editor camera object (its Camera component must leave the
    // component registry before any later teardown step).
    m_editorCamera.SetCamera(nullptr);
    m_editorCameraComponent = nullptr;
    m_editorCameraObject.reset();

    // Drop callbacks so nothing external fires into a dead engine.
    m_onSceneLoaded = nullptr;
    m_onSceneUnloaded = nullptr;
    m_onPlayStateChanged = nullptr;
}

void SceneManager::UnloadAllScenes()
{
    if (m_isPlaying)
        FlushPersistentPromotions();

    SetActiveScene(nullptr);

    for (auto &scene : m_scenes) {
        if (m_onSceneUnloaded) {
            m_onSceneUnloaded(scene.get());
        }
    }

    m_scenes.clear();
}

Scene *SceneManager::GetScene(const std::string &name) const
{
    for (const auto &scene : m_scenes) {
        if (scene->GetName() == name) {
            return scene.get();
        }
    }
    return nullptr;
}

void SceneManager::Start()
{
    if (m_activeScene) {
        m_activeScene->Start();
    }
    if (m_runtimePersistentScene)
        m_runtimePersistentScene->Start();
}

void SceneManager::SetRuntimeLifecycleCallbacks(RuntimeLifecycleBeginCallback beginFrame,
                                                RuntimeLifecyclePhaseCallback fixedUpdate,
                                                RuntimeLifecyclePhaseCallback update,
                                                RuntimeLifecyclePhaseCallback lateUpdate,
                                                RuntimeLifecyclePhaseCallback editorUpdate,
                                                RuntimeLifecycleEndCallback endFrame)
{
    m_runtimeLifecycleBegin = std::move(beginFrame);
    m_runtimeLifecycleFixedUpdate = std::move(fixedUpdate);
    m_runtimeLifecycleUpdate = std::move(update);
    m_runtimeLifecycleLateUpdate = std::move(lateUpdate);
    m_runtimeLifecycleEditorUpdate = std::move(editorUpdate);
    m_runtimeLifecycleEnd = std::move(endFrame);
    m_runtimeLifecycleSchedulerEnabled =
        static_cast<bool>(m_runtimeLifecycleBegin) && static_cast<bool>(m_runtimeLifecycleFixedUpdate) &&
        static_cast<bool>(m_runtimeLifecycleUpdate) && static_cast<bool>(m_runtimeLifecycleLateUpdate) &&
        static_cast<bool>(m_runtimeLifecycleEditorUpdate) && static_cast<bool>(m_runtimeLifecycleEnd);
    const bool schedulerActive = m_runtimeLifecycleSchedulerEnabled && m_runtimeLifecycleWorkAvailable;
    for (const auto &scene : m_scenes) {
        if (scene)
            scene->SetRuntimeLifecycleSchedulerEnabled(schedulerActive);
    }
    if (m_runtimePersistentScene)
        m_runtimePersistentScene->SetRuntimeLifecycleSchedulerEnabled(schedulerActive);
}

void SceneManager::SetRuntimeLifecycleWorkAvailable(bool available) noexcept
{
    if (m_runtimeLifecycleWorkAvailable == available)
        return;

    m_runtimeLifecycleWorkAvailable = available;
    const bool schedulerActive = m_runtimeLifecycleSchedulerEnabled && m_runtimeLifecycleWorkAvailable;
    for (const auto &scene : m_scenes) {
        if (scene)
            scene->SetRuntimeLifecycleSchedulerEnabled(schedulerActive);
    }
    if (m_runtimePersistentScene)
        m_runtimePersistentScene->SetRuntimeLifecycleSchedulerEnabled(schedulerActive);
}

void SceneManager::SetRuntimeLifecyclePlan(uint64_t revision, size_t fixedUpdateCount, size_t updateCount,
                                           size_t lateUpdateCount) noexcept
{
    if (revision < m_runtimeLifecyclePlanRevision)
        return;
    m_runtimeLifecyclePlanRevision = revision;
    m_runtimeLifecycleFixedUpdateCount = fixedUpdateCount;
    m_runtimeLifecycleUpdateCount = updateCount;
    m_runtimeLifecycleLateUpdateCount = lateUpdateCount;
}

void SceneManager::SetRuntimeFrameBarrierCallback(RuntimeFrameBarrierCallback callback)
{
    m_runtimeFrameBarrier = std::move(callback);
}

void SceneManager::EmitRuntimeFrameBarrier(RuntimeFrameBarrier barrier) const
{
    if (m_runtimeLifecycleFrameOpen && m_runtimeFrameBarrier)
        m_runtimeFrameBarrier(barrier);
}

void SceneManager::ClearRuntimeLifecycleCallbacks()
{
    if (m_runtimeLifecycleFrameOpen && m_runtimeLifecycleEnd)
        m_runtimeLifecycleEnd();
    m_runtimeLifecycleBegin = nullptr;
    m_runtimeLifecycleFixedUpdate = nullptr;
    m_runtimeLifecycleUpdate = nullptr;
    m_runtimeLifecycleLateUpdate = nullptr;
    m_runtimeLifecycleEditorUpdate = nullptr;
    m_runtimeLifecycleEnd = nullptr;
    m_runtimeFrameBarrier = nullptr;
    m_runtimeLifecycleSchedulerEnabled = false;
    m_runtimeLifecycleWorkAvailable = false;
    m_runtimeLifecycleFrameOpen = false;
    m_runtimeLifecyclePlanRevision = 0;
    m_runtimeLifecycleFixedUpdateCount = 0;
    m_runtimeLifecycleUpdateCount = 0;
    m_runtimeLifecycleLateUpdateCount = 0;
    for (const auto &scene : m_scenes) {
        if (scene)
            scene->SetRuntimeLifecycleSchedulerEnabled(false);
    }
    if (m_runtimePersistentScene)
        m_runtimePersistentScene->SetRuntimeLifecycleSchedulerEnabled(false);
}

void SceneManager::Update(float deltaTime)
{
    m_lastFrameProfile = {};

    auto t0 = ProfileClock::now();
    UpdateCapturedEditorCamera(m_editorCamera, deltaTime);
    m_lastFrameProfile.editorCameraMs += ProfileMsSince(t0);

    if (!m_isPlaying && m_activeScene) {
        const bool useRuntimeScheduler = m_runtimeLifecycleSchedulerEnabled && m_runtimeLifecycleWorkAvailable;
        if (useRuntimeScheduler) {
            m_runtimeLifecycleBegin();
            m_runtimeLifecycleFrameOpen = true;
        }

        t0 = ProfileClock::now();
        m_activeScene->EditorUpdate(deltaTime);
        m_lastFrameProfile.editorUpdateMs += ProfileMsSince(t0);

        if (useRuntimeScheduler && m_runtimeLifecycleUpdateCount > 0)
            m_runtimeLifecycleEditorUpdate(deltaTime);
    }

    // Update active scene if playing
    if (m_isPlaying && !m_isPaused && (m_activeScene || m_runtimePersistentScene)) {
        t0 = ProfileClock::now();
        if (m_activeScene)
            m_activeScene->ProcessPendingStarts();
        if (m_runtimePersistentScene)
            m_runtimePersistentScene->ProcessPendingStarts();
        m_lastFrameProfile.pendingStartsMs += ProfileMsSince(t0);

        const bool useRuntimeScheduler = m_runtimeLifecycleSchedulerEnabled && m_runtimeLifecycleWorkAvailable;
        if (useRuntimeScheduler) {
            m_runtimeLifecycleBegin();
            m_runtimeLifecycleFrameOpen = true;
        }

        // Keep gameplay and physics on the same scaled clock. The raw frame
        // delta is clamped once before scaling so long frames cannot create an
        // unbounded fixed-step catch-up burst.
        const float unscaledDeltaTime = std::clamp(deltaTime, 0.0f, m_maxFixedDeltaTime);
        m_lastScaledDeltaTime = unscaledDeltaTime * m_timeScale;

        // ---- Fixed-update accumulator (Unity-style) ----
        m_fixedTimeAccumulator += m_lastScaledDeltaTime;
        while (m_fixedTimeAccumulator >= m_fixedTimeStep) {
            RunFixedSimulationStep(useRuntimeScheduler);
            m_fixedTimeAccumulator -= m_fixedTimeStep;
        }

        t0 = ProfileClock::now();
        ApplyInterpolatedRigidbodies(m_fixedTimeAccumulator / m_fixedTimeStep);
        m_lastFrameProfile.interpolationMs += ProfileMsSince(t0);
        EmitRuntimeFrameBarrier(RuntimeFrameBarrier::TransformResolve);

        t0 = ProfileClock::now();
        if (useRuntimeScheduler && m_runtimeLifecycleUpdateCount > 0)
            m_runtimeLifecycleUpdate(m_lastScaledDeltaTime);
        if (m_activeScene)
            m_activeScene->Update(m_lastScaledDeltaTime);
        if (m_runtimePersistentScene)
            m_runtimePersistentScene->Update(m_lastScaledDeltaTime);
        if (!TransformECSStore::Instance().IsFrameCacheActive())
            FlushPersistentPromotions();
        m_lastFrameProfile.gameplayUpdateMs += ProfileMsSince(t0);
    }
}

void SceneManager::EnsurePhysicsQueriesCurrent()
{
    if (!m_activeScene && !m_runtimePersistentScene)
        return;
    FlushPendingBroadphase();
    // Outside play mode nothing steps the simulation, so moved bodies must be
    // teleported (dt = 0): the kinematic-velocity paths would otherwise leave
    // bodies with a velocity that is never integrated nor settled.
    SyncCollidersToPhysics(m_isPlaying ? m_fixedTimeStep : 0.0f);
}

void SceneManager::FixedUpdate()
{
    // Intentionally empty — fixed update is driven by the accumulator inside
    // Update() for correct time-step handling.  Exposed in the header so
    // external code *could* call it manually if needed, but normally it is
    // not called directly.
}

void SceneManager::LateUpdate(float deltaTime)
{
    if (m_isPlaying && !m_isPaused && (m_activeScene || m_runtimePersistentScene)) {
        auto t0 = ProfileClock::now();
        if (m_activeScene)
            m_activeScene->ProcessPendingStarts();
        if (m_runtimePersistentScene)
            m_runtimePersistentScene->ProcessPendingStarts();
        m_lastFrameProfile.pendingStartsMs += ProfileMsSince(t0);

        t0 = ProfileClock::now();
        if (m_runtimeLifecycleSchedulerEnabled && m_runtimeLifecycleWorkAvailable &&
            m_runtimeLifecycleLateUpdateCount > 0)
            m_runtimeLifecycleLateUpdate(m_lastScaledDeltaTime);
        if (m_activeScene)
            m_activeScene->LateUpdate(m_lastScaledDeltaTime);
        if (m_runtimePersistentScene)
            m_runtimePersistentScene->LateUpdate(m_lastScaledDeltaTime);
        if (!TransformECSStore::Instance().IsFrameCacheActive())
            FlushPersistentPromotions();
        m_lastFrameProfile.lateUpdateMs += ProfileMsSince(t0);
    }

    // Update spatial audio (runs even when paused so listener position stays synced)
    auto t0 = ProfileClock::now();
    AudioEngine::Instance().Update(deltaTime);
    m_lastFrameProfile.audioMs += ProfileMsSince(t0);
}

void SceneManager::EndFrame()
{
    EmitRuntimeFrameBarrier(RuntimeFrameBarrier::PendingDestroy);
    if (m_activeScene || m_runtimePersistentScene) {
        auto t0 = ProfileClock::now();
        if (m_activeScene)
            m_activeScene->ProcessPendingDestroys();
        if (m_runtimePersistentScene)
            m_runtimePersistentScene->ProcessPendingDestroys();
        FlushPersistentPromotions();
        m_lastFrameProfile.endFrameMs += ProfileMsSince(t0);
    }
    if (m_runtimeLifecycleFrameOpen) {
        if (m_runtimeLifecycleEnd)
            m_runtimeLifecycleEnd();
        m_runtimeLifecycleFrameOpen = false;
    }
}

void SceneManager::Play()
{
    // Only reset accumulator on initial play, not on resume-from-pause
    if (!m_isPlaying) {
        ClearRuntimePersistentScene();
        m_fixedTimeAccumulator = 0.0f;
        m_fixedTime = 0.0;
        m_fixedUnscaledTime = 0.0;
        m_lastScaledDeltaTime = 0.0f;
    }

    m_isPlaying = true;
    m_isPaused = false;
    AudioEngine::Instance().ResumeAll();

    // Notify renderer to exit idle mode immediately.
    if (m_onPlayStateChanged)
        m_onPlayStateChanged(true);

    StartActiveSceneForPlay();
}

void SceneManager::StartActiveSceneForPlay()
{
    if (!m_activeScene)
        return;

    const auto transitionStart = ProfileClock::now();
    m_activeScene->SetPlaying(true);

    // A transactional runtime load publishes freshly-deserialized Transform
    // rows immediately before this call. Their world caches can still contain
    // values from recycled ECS slots. Jolt shapes consume world scale during
    // body creation, so synchronize the graph before Collider::RegisterBody.
    auto phaseStart = ProfileClock::now();
    TransformECSStore::Instance().SyncSceneWorldMatrices(m_activeScene);
    const double initialTransformMs = ProfileMsSince(phaseStart);

    phaseStart = ProfileClock::now();
    m_activeScene->Start();
    FlushPersistentPromotions();
    const double startMs = ProfileMsSince(phaseStart);

    // Start callbacks may author transforms. Publish those edits before shape
    // creation as well, matching the transform state visible to gameplay.
    phaseStart = ProfileClock::now();
    TransformECSStore::Instance().SyncSceneWorldMatrices(m_activeScene);
    const double postStartTransformMs = ProfileMsSince(phaseStart);

    // Start callbacks can author transforms. The transform observer already
    // records exactly which physics actors changed, so avoid rewriting every
    // resident body when entering Play Mode.
    phaseStart = ProfileClock::now();
    SyncCollidersToPhysics(m_fixedTimeStep);
    const double colliderSyncMs = ProfileMsSince(phaseStart);

    phaseStart = ProfileClock::now();
    FlushPendingBroadphase();
    const double flushMs = ProfileMsSince(phaseStart);

    // Jolt bodies default to sleeping and need activation after broadphase
    // publication for gravity and forces to apply on the first fixed step.
    phaseStart = ProfileClock::now();
    ActivateAllDynamicBodies();
    const double activationMs = ProfileMsSince(phaseStart);

    const double totalMs = ProfileMsSince(transitionStart);
    if (totalMs > 25.0) {
        // INXLOG_INFO("[Perf] StartActiveSceneForPlay: total=", totalMs, "ms transform=", initialTransformMs,
        //             "ms lifecycle=", startMs, "ms postTransform=", postStartTransformMs,
        //             "ms colliderSync=", colliderSyncMs, "ms broadphase=", flushMs, "ms activate=", activationMs,
        //             "ms");
    }
}

void SceneManager::Stop()
{
    m_isPlaying = false;
    m_isPaused = false;
    AudioEngine::Instance().ResumeAll();
    m_fixedTimeAccumulator = 0.0f;
    m_fixedTime = 0.0;
    m_fixedUnscaledTime = 0.0;
    m_lastScaledDeltaTime = 0.0f;

    // Notify renderer that play stopped.
    if (m_onPlayStateChanged)
        m_onPlayStateChanged(false);

    // Runtime persistence ends exactly at the Play boundary. Python restores
    // the authored scene snapshot after this native graph has been destroyed.
    ClearRuntimePersistentScene();
    UpdateRuntimeScenePlayingState(false);

    // Scene state restore is handled by Python PlayModeManager
    // (serialize on Play, deserialize on Stop)
}

void SceneManager::Pause()
{
    m_isPaused = !m_isPaused;
    if (m_isPaused)
        AudioEngine::Instance().PauseAll();
    else
        AudioEngine::Instance().ResumeAll();
}

void SceneManager::Step(float deltaTime)
{
    if (!m_isPaused || !m_isPlaying || (!m_activeScene && !m_runtimePersistentScene))
        return;

    m_lastFrameProfile = {};
    if (m_activeScene)
        m_activeScene->ProcessPendingStarts();
    if (m_runtimePersistentScene)
        m_runtimePersistentScene->ProcessPendingStarts();
    const bool useRuntimeScheduler = m_runtimeLifecycleSchedulerEnabled && m_runtimeLifecycleWorkAvailable;
    if (useRuntimeScheduler) {
        m_runtimeLifecycleBegin();
        m_runtimeLifecycleFrameOpen = true;
    }

    RunFixedSimulationStep(useRuntimeScheduler);
    ApplyInterpolatedRigidbodies(1.0f);
    EmitRuntimeFrameBarrier(RuntimeFrameBarrier::TransformResolve);
    if (useRuntimeScheduler && m_runtimeLifecycleUpdateCount > 0)
        m_runtimeLifecycleUpdate(deltaTime);
    if (m_activeScene)
        m_activeScene->Update(deltaTime);
    if (m_runtimePersistentScene)
        m_runtimePersistentScene->Update(deltaTime);
    if (!TransformECSStore::Instance().IsFrameCacheActive())
        FlushPersistentPromotions();
    if (m_activeScene)
        m_activeScene->ProcessPendingStarts();
    if (m_runtimePersistentScene)
        m_runtimePersistentScene->ProcessPendingStarts();
    if (useRuntimeScheduler && m_runtimeLifecycleLateUpdateCount > 0)
        m_runtimeLifecycleLateUpdate(deltaTime);
    if (m_activeScene)
        m_activeScene->LateUpdate(deltaTime);
    if (m_runtimePersistentScene)
        m_runtimePersistentScene->LateUpdate(deltaTime);
    if (!TransformECSStore::Instance().IsFrameCacheActive())
        FlushPersistentPromotions();
    if (m_activeScene)
        TransformECSStore::Instance().SyncSceneWorldMatrices(m_activeScene);
    if (m_runtimePersistentScene)
        TransformECSStore::Instance().SyncSceneWorldMatrices(m_runtimePersistentScene.get());
    EmitRuntimeFrameBarrier(RuntimeFrameBarrier::FinalTransformResolve);
    EmitRuntimeFrameBarrier(RuntimeFrameBarrier::AnimationTimeline);
    // Keep the lifecycle frame open. The renderer still has to publish
    // RenderExtraction, RenderGraph and Snapshot before EndFrame owns pending
    // destruction and retirement, exactly like an ordinary Play frame.
}

void SceneManager::DontDestroyOnLoad(GameObject *gameObject)
{
    // DontDestroyOnLoad is runtime residency, not an authored flag. Ignoring
    // Edit Mode calls prevents a script/tool invocation from leaking an object
    // across project or play-session boundaries.
    if (!m_isPlaying || !gameObject)
        return;

    while (gameObject->GetParent())
        gameObject = gameObject->GetParent();
    if (gameObject->GetScene() == m_runtimePersistentScene.get())
        return;
    if (!gameObject->GetScene())
        return;

    gameObject->SetPersistent(true);
    if (m_pendingPersistentRootIdSet.insert(gameObject->GetID()).second)
        m_pendingPersistentRootIds.push_back(gameObject->GetID());
}

Scene *SceneManager::EnsureRuntimePersistentScene()
{
    if (!m_isPlaying)
        return nullptr;
    if (!m_runtimePersistentScene) {
        m_runtimePersistentScene = std::make_unique<Scene>("DontDestroyOnLoad");
        m_runtimePersistentScene->SetRuntimeLifecycleSchedulerEnabled(m_runtimeLifecycleSchedulerEnabled &&
                                                                      m_runtimeLifecycleWorkAvailable);
        m_runtimePersistentScene->SetPlaying(true);
        // Start the empty Scene once. Trees transferred into it already own
        // their lifecycle state and must never replay Awake/Start/OnEnable.
        m_runtimePersistentScene->Start();
    }
    return m_runtimePersistentScene.get();
}

GameObject *SceneManager::FindRuntimeObjectByID(uint64_t id) const
{
    if (id == 0)
        return nullptr;
    if (m_activeScene) {
        if (GameObject *object = m_activeScene->FindByID(id))
            return object;
    }
    if (m_runtimePersistentScene) {
        if (GameObject *object = m_runtimePersistentScene->FindByID(id))
            return object;
    }
    for (const auto &scene : m_scenes) {
        if (!scene || scene.get() == m_activeScene)
            continue;
        if (GameObject *object = scene->FindByID(id))
            return object;
    }
    return nullptr;
}

void SceneManager::FlushPersistentPromotions()
{
    if (!m_isPlaying || m_pendingPersistentRootIds.empty())
        return;

    std::vector<uint64_t> requests;
    requests.swap(m_pendingPersistentRootIds);
    m_pendingPersistentRootIdSet.clear();
    Scene *persistentScene = EnsureRuntimePersistentScene();
    if (!persistentScene)
        return;

    for (uint64_t id : requests) {
        GameObject *root = FindRuntimeObjectByID(id);
        if (!root)
            continue;
        while (root->GetParent())
            root = root->GetParent();
        Scene *source = root->GetScene();
        if (!source || source == persistentScene)
            continue;
        if (!source->TransferRootObjectTo(root, *persistentScene)) {
            root->SetPersistent(false);
            INXLOG_WARN("DontDestroyOnLoad could not promote root '", root->GetName(), "' (id=", root->GetID(), ")");
        }
    }
}

void SceneManager::PrepareActiveSceneReplacement()
{
    // SceneDocumentTransaction replaces the active graph in place, so the
    // Scene pointer does not change and SetActiveScene cannot publish this
    // boundary. Discard both wall-clock loading time and old-scene fixed-step
    // remainder before the new graph is allowed to tick.
    m_fixedTimeAccumulator = 0.0f;
    m_lastScaledDeltaTime = 0.0f;
    m_resetDeltaTimeOnNextFrame = true;
    if (m_isPlaying) {
        FlushPersistentPromotions();
        // Scene commit clears pending physics queues belonging to the dying
        // active graph. Publish persistent bodies first so no queued creation
        // or broadphase add is accidentally discarded with that graph.
        FlushPendingBroadphase();
    }
}

void SceneManager::UpdateRuntimeScenePlayingState(bool playing)
{
    if (m_activeScene)
        m_activeScene->SetPlaying(playing);
    if (m_runtimePersistentScene)
        m_runtimePersistentScene->SetPlaying(playing);
}

void SceneManager::ClearRuntimePersistentScene()
{
    for (uint64_t id : m_pendingPersistentRootIds) {
        if (GameObject *root = FindRuntimeObjectByID(id))
            root->SetPersistent(false);
    }
    m_pendingPersistentRootIds.clear();
    m_pendingPersistentRootIdSet.clear();
    if (m_runtimePersistentScene)
        m_runtimePersistentScene->SetPlaying(false);
    m_runtimePersistentScene.reset();
}

void SceneManager::RestorePersistentComponentRegistries()
{
    if (!m_runtimePersistentScene)
        return;

    for (GameObject *object : m_runtimePersistentScene->GetAllObjects()) {
        if (!object || !object->IsActiveInHierarchy())
            continue;
        for (MeshRenderer *renderer : object->GetComponents<MeshRenderer>()) {
            if (renderer && renderer->IsEnabled())
                RegisterMeshRenderer(renderer);
        }
        for (Light *light : object->GetComponents<Light>()) {
            if (light && light->IsEnabled())
                RegisterLight(light);
        }
        auto colliders = object->GetComponents<Collider>();
        if (!colliders.empty()) {
            Collider *primary = colliders.front();
            if (primary && primary->IsEnabled()) {
                if (primary->GetBodyId() == 0xFFFFFFFF)
                    primary->RegisterBody();
                primary->RestoreSceneResidency();
            }
        }
    }
}

void SceneManager::SyncCollidersToPhysics(float fixedDeltaTime)
{
    auto &store = PhysicsECSStore::Instance();
    const auto &dirtyColliders = store.ConsumeDirtyColliders();
    m_lastFrameProfile.colliderSyncCandidates += static_cast<double>(dirtyColliders.size());
    static thread_local std::vector<PhysicsBodyPoseUpdate> staticPoseBatch;
    staticPoseBatch.clear();
    staticPoseBatch.reserve(dirtyColliders.size());

    for (const auto handle : dirtyColliders) {
        if (!store.IsValid(handle))
            continue;
        auto &data = store.GetCollider(handle);
        auto *col = data.owner;
        if (!col || !col->IsEnabled())
            continue;
        auto *go = col->GetGameObject();
        if (!go || !IsRuntimeScene(go->GetScene()))
            continue;
        const auto actorHandle = data.actorHandle;
        if (!store.IsValid(actorHandle))
            throw std::logic_error("dirty Collider references a stale PhysicsActor");
        const auto &actor = store.GetActor(actorHandle);
        const bool profilesRigidbodySync = actor.rigidbody && actor.rigidbody->IsEnabled();
        const auto syncStart = profilesRigidbodySync ? ProfileClock::now() : ProfileClock::time_point{};
        col->SyncTransformToPhysics(fixedDeltaTime, &staticPoseBatch);
        if (profilesRigidbodySync)
            m_lastFrameProfile.syncExternalMovesMs += ProfileMsSince(syncStart);
    }
    PhysicsWorld::Instance().SetBodyPositionsBatch(staticPoseBatch);
}

void SceneManager::PublishAuthoredTransformsToPhysics()
{
    // Runtime persistence is committed only after the Transform frame cache
    // has flushed. Moving a root while that cache still names the authored
    // Scene would otherwise make EndFrameCache skip its dirty slots.
    FlushPersistentPromotions();
    if (m_runtimePersistentScene)
        TransformECSStore::Instance().SyncSceneWorldMatrices(m_runtimePersistentScene.get());

    auto &store = PhysicsECSStore::Instance();
    if ((!m_activeScene && !m_runtimePersistentScene) || !store.HasDirtyColliders())
        return;

    // Transform writes performed by Update/LateUpdate are collected by the
    // frame cache. Publish them once, after the cache commits, instead of
    // synchronizing on every property setter. This preserves high-FPS editor
    // performance while preventing Rigidbody interpolation from restoring an
    // older pose on the following frame.
    FlushPendingBroadphase();
    SyncCollidersToPhysics(0.0f);
}

void SceneManager::RunFixedSimulationStep(bool useRuntimeScheduler)
{
    m_lastFrameProfile.fixedSteps += 1.0;
    m_fixedTime += static_cast<double>(m_fixedTimeStep);
    if (m_timeScale > 0.0f)
        m_fixedUnscaledTime += static_cast<double>(m_fixedTimeStep / m_timeScale);

    // Publish bodies created before this fixed step so FixedUpdate queries see
    // the authoritative previous state. Transform writes from FixedUpdate are
    // synchronized only once, at the explicit TransformToPhysics boundary.
    FlushPendingBroadphase();

    auto phaseStart = ProfileClock::now();
    if (useRuntimeScheduler && m_runtimeLifecycleFixedUpdateCount > 0)
        m_runtimeLifecycleFixedUpdate(m_fixedTimeStep);
    if (m_activeScene)
        m_activeScene->FixedUpdate(m_fixedTimeStep);
    if (m_runtimePersistentScene)
        m_runtimePersistentScene->FixedUpdate(m_fixedTimeStep);
    if (!TransformECSStore::Instance().IsFrameCacheActive())
        FlushPersistentPromotions();
    m_lastFrameProfile.fixedUpdateMs += ProfileMsSince(phaseStart);

    FlushPendingBroadphase();
    auto &physicsStore = PhysicsECSStore::Instance();
    const bool hasRigidbodies = physicsStore.GetAliveRigidbodyCount() > 0;

    EmitRuntimeFrameBarrier(RuntimeFrameBarrier::TransformToPhysics);
    if (hasRigidbodies) {
        phaseStart = ProfileClock::now();
        SyncCollidersToPhysics(m_fixedTimeStep);
        m_lastFrameProfile.syncCollidersMs += ProfileMsSince(phaseStart);
    }

    EmitRuntimeFrameBarrier(RuntimeFrameBarrier::PhysicsSimulation);
    if (hasRigidbodies) {
        phaseStart = ProfileClock::now();
        PhysicsWorld::Instance().Step(m_fixedTimeStep);
        m_lastFrameProfile.physicsStepMs += ProfileMsSince(phaseStart);
        m_lastFrameProfile.dynamicCCDSplits +=
            static_cast<double>(PhysicsWorld::Instance().GetLastDynamicCCDSplitCount());

        phaseStart = ProfileClock::now();
        m_lastFrameProfile.contactEvents += static_cast<double>(PhysicsWorld::Instance().DispatchContactEvents());
        m_lastFrameProfile.physicsEventsMs += ProfileMsSince(phaseStart);
    }

    EmitRuntimeFrameBarrier(RuntimeFrameBarrier::PhysicsToTransform);
    if (hasRigidbodies) {
        phaseStart = ProfileClock::now();
        SyncRigidbodiesToTransform();
        m_lastFrameProfile.syncRigidbodiesMs += ProfileMsSince(phaseStart);
    }
}

void SceneManager::FlushPendingBroadphase()
{
    MeshCollider::FlushCompletedCooking();
    auto &store = PhysicsECSStore::Instance();
    auto &pw = PhysicsWorld::Instance();
    if (!pw.IsInitialized())
        return;

    // ── Create deferred Jolt bodies ──
    auto pendingBodies = store.ConsumePendingBodyCreations();
    if (pendingBodies.empty() && !store.HasPendingBroadphaseAdds() && !store.HasPendingBroadphaseRemoves())
        return;

    auto t0 = ProfileClock::now();
    const size_t bodyCount = pendingBodies.size();

    for (auto handle : pendingBodies) {
        if (!store.IsValid(handle))
            continue;
        auto &data = store.GetCollider(handle);
        auto *col = data.owner;
        if (!col || !col->IsEnabled() || col->GetBodyId() != 0xFFFFFFFF)
            continue;

        // Actually create the Jolt body (deferred from Awake)
        col->RegisterBody();

        // Queue broadphase add for the batch step below
        if (col->GetBodyId() != 0xFFFFFFFF) {
            col->AddToBroadphase();
        }
    }

    double createBodiesMs = ProfileMsSince(t0);

    // ── Remove before add ──
    // Gameplay Update/FixedUpdate may toggle Collider.enabled. Submit those
    // mutations only at this fixed-step boundary so Jolt never observes a
    // broadphase change from inside a component callback.
    for (const uint32_t bodyId : store.ConsumePendingBroadphaseRemoves()) {
        pw.RemoveBodyFromBroadphase(bodyId);
    }

    // ── Batch add to broadphase ──
    auto t1 = ProfileClock::now();
    auto pending = store.ConsumePendingBroadphaseAdds();
    if (pending.empty())
        return;

    // Use Jolt batch API (AddBodiesPrepare/Finalize) for large batches,
    // which is significantly faster than individual AddBody calls.
    pw.AddBodiesBatch(pending);

    // Start() runs before this first physics flush, so force commands may
    // already be queued on Rigidbodies whose deferred body did not exist yet.
    // Jolt requires force submission after the body enters the system.
    for (const auto handle : pendingBodies) {
        if (!store.IsValid(handle))
            continue;
        auto *collider = store.GetCollider(handle).owner;
        auto *rigidbody = collider ? collider->GetCachedRigidbody() : nullptr;
        if (rigidbody && rigidbody->IsEnabled() && collider->GetBodyId() != 0xFFFFFFFF)
            rigidbody->FlushPendingForceCommands();
    }

    double addBodiesMs = ProfileMsSince(t1);

    // Jolt incrementally maintains its broad phase when bodies are added.
    // Rebuilding the complete tree for one or two runtime spawns creates a
    // periodic main-thread spike (particularly visible in continuous physics
    // piles) without improving the query structure. Reserve the explicit
    // optimization for genuinely large scene-import batches.
    double optimizeMs = 0.0;
    constexpr size_t kBroadphaseRebuildBatchThreshold = 128;
    if (pending.size() >= kBroadphaseRebuildBatchThreshold) {
        auto t2 = ProfileClock::now();
        pw.OptimizeBroadPhase();
        optimizeMs = ProfileMsSince(t2);
    }

    // if (bodyCount >= 100) {
    //     INXLOG_INFO("[Perf] FlushPendingBroadphase: ", bodyCount, " bodies — "
    //                 "CreateBody: ", static_cast<int>(createBodiesMs), "ms, "
    //                 "AddBatch: ", static_cast<int>(addBodiesMs), "ms, "
    //                 "Optimize: ", static_cast<int>(optimizeMs), "ms");
    // }
}

void SceneManager::SyncTransforms()
{
    // First pass starts any deferred mesh cooking. Explicit SyncTransforms is
    // a barrier: wait for those immutable CPU jobs, commit them on this main
    // thread, then create/rebuild bodies before returning.
    FlushPendingBroadphase();
    MeshCollider::FlushCompletedCooking(true);
    FlushPendingBroadphase();
    PhysicsECSStore::Instance().MarkAllCollidersDirty();
    // During play, route moved kinematic / collider-only bodies through the
    // velocity-driven move paths — the editor gizmo calls Physics.sync_transforms
    // after every drag frame, and a dt of 0 would degrade those moves into
    // zero-velocity teleports that push dynamic bodies aside without imparting
    // any momentum. Outside play nothing steps the simulation, so moves must
    // remain teleports.
    SyncCollidersToPhysics(m_isPlaying ? m_fixedTimeStep : 0.0f);
}

void SceneManager::ForceAllBodiesToCurrentTransform()
{
    auto &pw = PhysicsWorld::Instance();
    if (!pw.IsInitialized())
        return;

    PhysicsECSStore::Instance().ForEachAliveCollider([&pw](ColliderECSData &data) {
        auto *col = data.owner;
        if (!col || col->GetBodyId() == 0xFFFFFFFF)
            return;

        auto *go = col->GetGameObject();
        if (!go)
            return;

        Transform *tf = go->GetTransform();
        if (!tf)
            return;

        glm::quat rot = tf->GetWorldRotation();
        glm::vec3 pos = tf->GetPosition();
        pw.SetBodyPosition(col->GetBodyId(), pos, rot);
    });
}

void SceneManager::ActivateAllDynamicBodies()
{
    auto &pw = PhysicsWorld::Instance();
    if (!pw.IsInitialized())
        return;

    PhysicsECSStore::Instance().ForEachAliveRigidbody([this](RigidbodyECSData &data) {
        auto *rb = data.owner;
        if (!rb || !rb->IsEnabled() || rb->IsKinematic())
            return;
        auto *go = rb->GetGameObject();
        if (!go || !IsRuntimeScene(go->GetScene()))
            return;
        rb->WakeUp();
    });
}

void SceneManager::SyncRigidbodiesToTransform()
{
    auto &physics = PhysicsWorld::Instance();
    const auto &bodyIds = physics.GetPoseReadbackBodyIds();
    m_lastFrameProfile.rigidbodySyncCandidates += static_cast<double>(bodyIds.size());

    // A body absent from the new active union has gone to sleep or been
    // deactivated. Finish its previous interpolation at the exact solver pose
    // before removing it from the dense presentation set.
    for (const uint32_t previousBodyId : m_posePresentationBodyIds) {
        if (std::binary_search(bodyIds.begin(), bodyIds.end(), previousBodyId))
            continue;
        auto *collider = physics.FindColliderByBodyId(previousBodyId);
        auto *rb = collider ? collider->GetCachedRigidbody() : nullptr;
        if (!rb || !rb->IsEnabled())
            continue;
        auto *go = rb->GetGameObject();
        if (go && IsRuntimeScene(go->GetScene()))
            rb->ApplyInterpolatedTransform(1.0f);
    }
    m_posePresentationBodyIds.assign(bodyIds.begin(), bodyIds.end());

    for (const uint32_t bodyId : bodyIds) {
        auto *collider = physics.FindColliderByBodyId(bodyId);
        auto *rb = collider ? collider->GetCachedRigidbody() : nullptr;
        if (!rb || !rb->IsEnabled())
            continue;
        auto *go = rb->GetGameObject();
        if (!go || !IsRuntimeScene(go->GetScene()))
            continue;
        rb->SyncPhysicsToTransform();
    }
}

void SceneManager::ApplyInterpolatedRigidbodies(float alpha)
{
    if (!m_activeScene && !m_runtimePersistentScene)
        return;

    auto &physics = PhysicsWorld::Instance();
    m_lastFrameProfile.interpolationCandidates += static_cast<double>(m_posePresentationBodyIds.size());
    for (const uint32_t bodyId : m_posePresentationBodyIds) {
        auto *collider = physics.FindColliderByBodyId(bodyId);
        auto *rb = collider ? collider->GetCachedRigidbody() : nullptr;
        if (!rb || !rb->IsEnabled())
            continue;
        auto *go = rb->GetGameObject();
        if (!go || !IsRuntimeScene(go->GetScene()))
            continue;
        rb->ApplyInterpolatedTransform(alpha);
    }
}

// ============================================================================
// Component registries
// ============================================================================

void SceneManager::ClearComponentRegistries()
{
    // Renderer-facing registries: MeshRenderer/Light keep raw component
    // pointers, so we must drop them before the owning GameObjects die during
    // a Scene::DeserializeDocument() commit (see the Scene Rebuild Contract).
    m_activeMeshRenderers.clear();
    m_activeMeshRendererSet.clear();
    m_activeLights.clear();
    ++m_meshRendererVersion;

    // Physics pending queues: edit-mode Collider::Awake() may have queued
    // body creations whose handle.index entries are about to be reused for
    // freshly-allocated colliders. If we leave the dedup set populated, the
    // new QueueBodyCreation() silently fails its insert and the body is never
    // created, leading to invisible collisions/missing rigidbodies post-load.
    PhysicsECSStore::Instance().ClearPendingQueues();
    m_posePresentationBodyIds.clear();

    // Scene document replacement clears process-wide renderer registries.
    // The runtime-only persistent Scene is outside that transaction, so put
    // its still-live components back immediately. Registration is idempotent.
    RestorePersistentComponentRegistries();
}

// ========================================================================
// MeshRenderer component registry
// ========================================================================

void SceneManager::ReserveRendererCapacity(size_t count)
{
    m_activeMeshRenderers.reserve(m_activeMeshRenderers.size() + count);
    m_activeMeshRendererSet.reserve(m_activeMeshRendererSet.size() + count);
}

void SceneManager::BeginRendererRegistryTransaction()
{
    ++m_rendererRegistryTransactionDepth;
}

void SceneManager::EndRendererRegistryTransaction()
{
    if (m_rendererRegistryTransactionDepth == 0)
        throw std::logic_error("renderer registry transaction underflow");
    --m_rendererRegistryTransactionDepth;
    if (m_rendererRegistryTransactionDepth == 0 && m_rendererRegistryTransactionDirty) {
        m_rendererRegistryTransactionDirty = false;
        ++m_meshRendererVersion;
    }
}

namespace
{
void MarkRendererRegistryChanged(uint32_t transactionDepth, bool &transactionDirty, uint64_t &version)
{
    if (transactionDepth != 0)
        transactionDirty = true;
    else
        ++version;
}
} // namespace

void SceneManager::RegisterMeshRenderer(MeshRenderer *renderer)
{
    if (!renderer)
        return;
    if (m_activeMeshRendererSet.insert(renderer).second) {
        m_activeMeshRenderers.push_back(renderer);
        MarkRendererRegistryChanged(m_rendererRegistryTransactionDepth, m_rendererRegistryTransactionDirty,
                                    m_meshRendererVersion);
    }
}

void SceneManager::UnregisterMeshRenderer(MeshRenderer *renderer)
{
    if (!m_activeMeshRendererSet.erase(renderer))
        return;
    MarkRendererRegistryChanged(m_rendererRegistryTransactionDepth, m_rendererRegistryTransactionDirty,
                                m_meshRendererVersion);
    // Swap-and-pop for O(1) removal from vector
    for (size_t i = 0; i < m_activeMeshRenderers.size(); ++i) {
        if (m_activeMeshRenderers[i] == renderer) {
            m_activeMeshRenderers[i] = m_activeMeshRenderers.back();
            m_activeMeshRenderers.pop_back();
            return;
        }
    }
}

void SceneManager::NotifyMeshRendererChanged(MeshRenderer *renderer)
{
    if (!renderer)
        return;
    if (m_activeMeshRendererSet.find(renderer) != m_activeMeshRendererSet.end())
        MarkRendererRegistryChanged(m_rendererRegistryTransactionDepth, m_rendererRegistryTransactionDirty,
                                    m_meshRendererVersion);
}

void SceneManager::NotifyMeshRendererContentChanged(MeshRenderer *renderer)
{
    if (!renderer || m_activeMeshRendererSet.find(renderer) == m_activeMeshRendererSet.end())
        return;
    ++m_renderContentRevision;
    if (m_renderContentRevision == 0)
        m_renderContentRevision = 1;
}

void SceneManager::MarkMeshRenderersDirtyForAsset(const std::string &meshGuid, const std::string &meshPath)
{
    (void)meshPath;
    if (meshGuid.empty())
        return;
    for (auto *renderer : m_activeMeshRenderers) {
        if (renderer && renderer->HasMeshAsset() && renderer->GetMeshAssetGuid() == meshGuid) {
            renderer->MarkMeshBufferDirty();
            // Update local bounds from the reloaded mesh
            auto mesh = renderer->GetMeshAssetRef().Get();
            if (mesh)
                renderer->SetLocalBounds(mesh->GetBoundsMin(), mesh->GetBoundsMax());
        }
        if (auto *skinned = dynamic_cast<SkinnedMeshRenderer *>(renderer)) {
            if (skinned->GetSourceModelGuid() == meshGuid)
                skinned->ReloadSourceModel();
        }
    }
}

// ========================================================================
// Light component registry
// ========================================================================

void SceneManager::RegisterLight(Light *light)
{
    if (!light)
        return;
    for (auto *l : m_activeLights) {
        if (l == light)
            return;
    }
    m_activeLights.push_back(light);
}

void SceneManager::UnregisterLight(Light *light)
{
    for (size_t i = 0; i < m_activeLights.size(); ++i) {
        if (m_activeLights[i] == light) {
            m_activeLights[i] = m_activeLights.back();
            m_activeLights.pop_back();
            return;
        }
    }
}

} // namespace infernux
