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
    TransformECSStore::Instance().SetInvalidationObserver([](Transform *transform) {
        auto *gameObject = transform ? transform->GetGameObject() : nullptr;
        if (!gameObject)
            return;
        PhysicsECSStore::Instance().MarkGameObjectDirty(gameObject);
    });

    // Create editor camera
    m_editorCameraObject = std::make_unique<GameObject>("Editor Camera");
    m_editorCameraComponent = m_editorCameraObject->AddComponent<Camera>();
    m_editorCamera.SetCamera(m_editorCameraComponent);
    m_editorCamera.Reset(); // Set default position
}

Scene *SceneManager::CreateScene(const std::string &name)
{
    auto scene = std::make_unique<Scene>(name);
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
    m_activeScene = scene;

    // ── Migrate DontDestroyOnLoad objects to the new scene ──
    if (scene && !m_persistentObjects.empty()) {
        for (auto &obj : m_persistentObjects) {
            if (!obj)
                continue;
            GameObject *raw = obj.get();
            scene->AttachRootObject(std::move(obj)); // sets scene ptr on tree

            // Re-register MeshRenderers and Lights that were cleared
            // when the old scene was unloaded (ClearComponentRegistries).
            for (auto *mr : raw->GetComponentsInChildren<MeshRenderer>()) {
                if (mr && mr->IsEnabled())
                    RegisterMeshRenderer(mr);
            }
            for (auto *lt : raw->GetComponentsInChildren<Light>()) {
                if (lt && lt->IsEnabled())
                    RegisterLight(lt);
            }
        }
        m_persistentObjects.clear();
    }

    // Note: We do NOT auto-assign the editor camera as mainCamera.
    // mainCamera == nullptr means "no game camera assigned" — the Game View
    // will show a placeholder. Scene View always uses the editor camera
    // via SceneRenderBridge / EditorCameraController, independent of mainCamera.
}

void SceneManager::UnloadScene(Scene *scene)
{
    if (!scene)
        return;

    // ── Extract persistent (DontDestroyOnLoad) root objects before unload ──
    ExtractPersistentObjects(scene);

    if (m_onSceneUnloaded) {
        m_onSceneUnloaded(scene);
    }

    // If this was the active scene, clear registry and pointer
    if (m_activeScene == scene) {
        ClearComponentRegistries();
        m_activeScene = nullptr;
    }

    auto it = std::find_if(m_scenes.begin(), m_scenes.end(),
                           [scene](const std::unique_ptr<Scene> &s) { return s.get() == scene; });

    if (it != m_scenes.end()) {
        m_scenes.erase(it);
    }

    // Set a new active scene if available
    if (!m_activeScene && !m_scenes.empty()) {
        m_activeScene = m_scenes[0].get();
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

    // Persistent roots live outside any scene; destroy them first so their
    // colliders release Jolt bodies while PhysicsWorld is still initialized.
    m_persistentObjects.clear();

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
    // ── Extract persistent objects from all scenes before unload ──
    for (auto &scene : m_scenes) {
        ExtractPersistentObjects(scene.get());
    }

    ClearComponentRegistries();

    for (auto &scene : m_scenes) {
        if (m_onSceneUnloaded) {
            m_onSceneUnloaded(scene.get());
        }
    }

    m_scenes.clear();
    m_activeScene = nullptr;
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
}

void SceneManager::Update(float deltaTime)
{
    m_lastFrameProfile = {};

    auto t0 = ProfileClock::now();
    UpdateCapturedEditorCamera(m_editorCamera, deltaTime);
    m_lastFrameProfile.editorCameraMs += ProfileMsSince(t0);

    if (!m_isPlaying && m_activeScene) {
        t0 = ProfileClock::now();
        m_activeScene->EditorUpdate(deltaTime);
        m_lastFrameProfile.editorUpdateMs += ProfileMsSince(t0);
    }

    // Update active scene if playing
    if (m_isPlaying && !m_isPaused && m_activeScene) {
        t0 = ProfileClock::now();
        m_activeScene->ProcessPendingStarts();
        m_lastFrameProfile.pendingStartsMs += ProfileMsSince(t0);

        // Keep gameplay and physics on the same scaled clock. The raw frame
        // delta is clamped once before scaling so long frames cannot create an
        // unbounded fixed-step catch-up burst.
        const float unscaledDeltaTime = std::clamp(deltaTime, 0.0f, m_maxFixedDeltaTime);
        m_lastScaledDeltaTime = unscaledDeltaTime * m_timeScale;

        // ---- Fixed-update accumulator (Unity-style) ----
        m_fixedTimeAccumulator += m_lastScaledDeltaTime;
        while (m_fixedTimeAccumulator >= m_fixedTimeStep) {
            m_lastFrameProfile.fixedSteps += 1.0;

            m_fixedTime += static_cast<double>(m_fixedTimeStep);
            if (m_timeScale > 0.0f)
                m_fixedUnscaledTime += static_cast<double>(m_fixedTimeStep / m_timeScale);

            // Flush deferred broadphase additions (Unity-style batch add)
            FlushPendingBroadphase();

            auto &physicsStore = PhysicsECSStore::Instance();
            bool hasRigidbodies = physicsStore.GetAliveRigidbodyCount() > 0;
            if (hasRigidbodies) {
                // Collider-only scenes have no dynamic/static or kinematic/static
                // pairs to solve. Keep their dirty poses queued until a scene
                // query needs current broadphase state.
                t0 = ProfileClock::now();
                SyncCollidersToPhysics(m_fixedTimeStep);
                m_lastFrameProfile.syncCollidersMs += ProfileMsSince(t0);
            }

            t0 = ProfileClock::now();
            m_activeScene->FixedUpdate(m_fixedTimeStep);
            m_lastFrameProfile.fixedUpdateMs += ProfileMsSince(t0);

            // FixedUpdate may rotate/move kinematic colliders or instantiate
            // new physics objects. Flush those changes before the Jolt step.
            FlushPendingBroadphase();
            hasRigidbodies = physicsStore.GetAliveRigidbodyCount() > 0;
            if (hasRigidbodies) {
                t0 = ProfileClock::now();
                SyncCollidersToPhysics(m_fixedTimeStep);
                m_lastFrameProfile.syncCollidersMs += ProfileMsSince(t0);

                // Step Jolt only when at least one Rigidbody can participate.
                t0 = ProfileClock::now();
                PhysicsWorld::Instance().Step(m_fixedTimeStep);
                m_lastFrameProfile.physicsStepMs += ProfileMsSince(t0);
                m_lastFrameProfile.dynamicCCDSplits +=
                    static_cast<double>(PhysicsWorld::Instance().GetLastDynamicCCDSplitCount());

                t0 = ProfileClock::now();
                m_lastFrameProfile.contactEvents +=
                    static_cast<double>(PhysicsWorld::Instance().DispatchContactEvents());
                m_lastFrameProfile.physicsEventsMs += ProfileMsSince(t0);

                t0 = ProfileClock::now();
                SyncRigidbodiesToTransform();
                m_lastFrameProfile.syncRigidbodiesMs += ProfileMsSince(t0);
            }

            m_fixedTimeAccumulator -= m_fixedTimeStep;
        }

        t0 = ProfileClock::now();
        ApplyInterpolatedRigidbodies(m_fixedTimeAccumulator / m_fixedTimeStep);
        m_lastFrameProfile.interpolationMs += ProfileMsSince(t0);

        t0 = ProfileClock::now();
        m_activeScene->Update(m_lastScaledDeltaTime);
        m_lastFrameProfile.gameplayUpdateMs += ProfileMsSince(t0);
    }
}

void SceneManager::EnsurePhysicsQueriesCurrent()
{
    if (!m_activeScene)
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
    if (m_isPlaying && !m_isPaused && m_activeScene) {
        auto t0 = ProfileClock::now();
        m_activeScene->ProcessPendingStarts();
        m_lastFrameProfile.pendingStartsMs += ProfileMsSince(t0);

        t0 = ProfileClock::now();
        m_activeScene->LateUpdate(m_lastScaledDeltaTime);
        m_lastFrameProfile.lateUpdateMs += ProfileMsSince(t0);
    }

    // Update spatial audio (runs even when paused so listener position stays synced)
    auto t0 = ProfileClock::now();
    AudioEngine::Instance().Update(deltaTime);
    m_lastFrameProfile.audioMs += ProfileMsSince(t0);
}

void SceneManager::EndFrame()
{
    if (m_activeScene) {
        auto t0 = ProfileClock::now();
        m_activeScene->ProcessPendingDestroys();
        m_lastFrameProfile.endFrameMs += ProfileMsSince(t0);
    }
}

void SceneManager::Play()
{
    // Only reset accumulator on initial play, not on resume-from-pause
    if (!m_isPlaying) {
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
        INXLOG_INFO("[Perf] StartActiveSceneForPlay: total=", totalMs, "ms transform=", initialTransformMs,
                    "ms lifecycle=", startMs, "ms postTransform=", postStartTransformMs,
                    "ms colliderSync=", colliderSyncMs, "ms broadphase=", flushMs, "ms activate=", activationMs, "ms");
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

    // Discard any persistent objects — play session is over.
    m_persistentObjects.clear();

    if (m_activeScene) {
        m_activeScene->SetPlaying(false);
    }

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
    if (!m_isPaused || !m_isPlaying || !m_activeScene)
        return;

    m_lastFrameProfile = {};
    m_activeScene->ProcessPendingStarts();

    FlushPendingBroadphase();
    SyncCollidersToPhysics(m_fixedTimeStep);
    m_fixedTime += static_cast<double>(m_fixedTimeStep);
    if (m_timeScale > 0.0f)
        m_fixedUnscaledTime += static_cast<double>(m_fixedTimeStep / m_timeScale);
    m_activeScene->FixedUpdate(m_fixedTimeStep);
    FlushPendingBroadphase();
    SyncCollidersToPhysics(m_fixedTimeStep);
    PhysicsWorld::Instance().Step(m_fixedTimeStep);
    m_lastFrameProfile.dynamicCCDSplits += static_cast<double>(PhysicsWorld::Instance().GetLastDynamicCCDSplitCount());
    m_lastFrameProfile.contactEvents += static_cast<double>(PhysicsWorld::Instance().DispatchContactEvents());
    SyncRigidbodiesToTransform();
    ApplyInterpolatedRigidbodies(1.0f);
    m_activeScene->Update(deltaTime);
    m_activeScene->ProcessPendingStarts();
    m_activeScene->LateUpdate(deltaTime);
    m_activeScene->ProcessPendingDestroys();
}

void SceneManager::DontDestroyOnLoad(GameObject *gameObject)
{
    if (!gameObject)
        return;

    // Walk up to root if called on a child
    if (gameObject->GetParent() != nullptr) {
        GameObject *root = gameObject;
        while (root->GetParent()) {
            root = root->GetParent();
        }
        gameObject = root;
    }

    // Just mark as persistent — the object stays in its scene normally.
    // When a scene is unloaded, persistent roots are migrated to the new scene.
    gameObject->SetPersistent(true);
}

void SceneManager::ExtractPersistentObjects(Scene *scene)
{
    if (!scene)
        return;

    // Collect persistent roots — iterate by index since DetachRootObject
    // modifies the vector.
    std::vector<GameObject *> toExtract;
    for (const auto &root : scene->GetRootObjects()) {
        if (root && root->IsPersistent())
            toExtract.push_back(root.get());
    }

    for (GameObject *go : toExtract) {
        auto owned = scene->DetachRootObject(go);
        if (owned) {
            owned->SetScene(nullptr);
            m_persistentObjects.push_back(std::move(owned));
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
        if (!go || go->GetScene() != m_activeScene)
            continue;
        col->SyncTransformToPhysics(fixedDeltaTime, &staticPoseBatch);
        const auto actorHandle = data.actorHandle;
        if (!store.IsValid(actorHandle))
            throw std::logic_error("dirty Collider references a stale PhysicsActor");
        auto &actor = store.GetActor(actorHandle);
        if (actor.rigidbody && actor.rigidbody->IsEnabled() && !actor.rigidbody->IsKinematic()) {
            auto t0 = ProfileClock::now();
            actor.rigidbody->SyncExternalMovesToPhysics();
            m_lastFrameProfile.syncExternalMovesMs += ProfileMsSince(t0);
        }
    }
    PhysicsWorld::Instance().SetBodyPositionsBatch(staticPoseBatch);
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

    // Rebuild broad-phase tree once after batch-adding all new bodies.
    auto t2 = ProfileClock::now();
    pw.OptimizeBroadPhase();
    double optimizeMs = ProfileMsSince(t2);

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
        if (!go || go->GetScene() != m_activeScene)
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
        if (go && go->GetScene() == m_activeScene)
            rb->ApplyInterpolatedTransform(1.0f);
    }
    m_posePresentationBodyIds.assign(bodyIds.begin(), bodyIds.end());

    for (const uint32_t bodyId : bodyIds) {
        auto *collider = physics.FindColliderByBodyId(bodyId);
        auto *rb = collider ? collider->GetCachedRigidbody() : nullptr;
        if (!rb || !rb->IsEnabled())
            continue;
        auto *go = rb->GetGameObject();
        if (!go || go->GetScene() != m_activeScene)
            continue;
        rb->SyncPhysicsToTransform();
    }
}

void SceneManager::ApplyInterpolatedRigidbodies(float alpha)
{
    if (!m_activeScene)
        return;

    auto &physics = PhysicsWorld::Instance();
    m_lastFrameProfile.interpolationCandidates += static_cast<double>(m_posePresentationBodyIds.size());
    for (const uint32_t bodyId : m_posePresentationBodyIds) {
        auto *collider = physics.FindColliderByBodyId(bodyId);
        auto *rb = collider ? collider->GetCachedRigidbody() : nullptr;
        if (!rb || !rb->IsEnabled())
            continue;
        auto *go = rb->GetGameObject();
        if (!go || go->GetScene() != m_activeScene)
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
}

// ========================================================================
// MeshRenderer component registry
// ========================================================================

void SceneManager::ReserveRendererCapacity(size_t count)
{
    m_activeMeshRenderers.reserve(m_activeMeshRenderers.size() + count);
    m_activeMeshRendererSet.reserve(m_activeMeshRendererSet.size() + count);
}

void SceneManager::RegisterMeshRenderer(MeshRenderer *renderer)
{
    if (!renderer)
        return;
    if (m_activeMeshRendererSet.insert(renderer).second) {
        m_activeMeshRenderers.push_back(renderer);
        ++m_meshRendererVersion;
    }
}

void SceneManager::UnregisterMeshRenderer(MeshRenderer *renderer)
{
    if (!m_activeMeshRendererSet.erase(renderer))
        return;
    ++m_meshRendererVersion;
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
        ++m_meshRendererVersion;
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
