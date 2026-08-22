#include <function/scene/GameObject.h>
#include <function/scene/Scene.h>
#include <function/scene/SceneManager.h>
#include <function/scene/Transform.h>
#include <function/scene/TransformECSStore.h>
#include <function/scene/physics/PhysicsECSStore.h>

#include <cassert>
#include <vector>

using infernux::GameObject;
using infernux::PhysicsECSStore;
using infernux::Scene;
using infernux::SceneManager;
using infernux::Transform;
using infernux::TransformECSStore;

int main()
{
    SceneManager &manager = SceneManager::Instance();
    using Barrier = SceneManager::RuntimeFrameBarrier;

    std::vector<Barrier> observed;
    int beginCount = 0;
    int fixedUpdateCount = 0;
    int updateCount = 0;
    int lateUpdateCount = 0;
    int editorUpdateCount = 0;
    int endCount = 0;
    manager.SetRuntimeFrameBarrierCallback([&observed](Barrier barrier) { observed.push_back(barrier); });

    // A production barrier is meaningful only while a lifecycle frame is
    // open. Direct emission outside one must stay a no-op.
    manager.EmitRuntimeFrameBarrier(Barrier::TransformToPhysics);
    assert(observed.empty());

    manager.CreateScene("RuntimeFrameBarrierTests");
    assert(manager.ConsumeFrameDeltaTime(0.25f) == 0.0f);
    assert(manager.ConsumeFrameDeltaTime(0.25f) == 0.25f);
    manager.PrepareActiveSceneReplacement();
    assert(manager.ConsumeFrameDeltaTime(0.25f) == 0.0f);
    assert(manager.ConsumeFrameDeltaTime(0.25f) == 0.25f);
    manager.SetRuntimeLifecycleCallbacks(
        [&beginCount] { ++beginCount; }, [&fixedUpdateCount](float) { ++fixedUpdateCount; },
        [&updateCount](float) { ++updateCount; }, [&lateUpdateCount](float) { ++lateUpdateCount; },
        [&editorUpdateCount](float) { ++editorUpdateCount; }, [&endCount] { ++endCount; });

    // Installing the bridge alone must not add Python crossings to a scene
    // with no script components. Structural registration enables it later.
    manager.Update(0.0f);
    manager.EmitRuntimeFrameBarrier(Barrier::RenderExtraction);
    manager.EndFrame();
    assert(beginCount == 0);
    assert(editorUpdateCount == 0);
    assert(endCount == 0);
    assert(observed.empty());

    manager.SetRuntimeLifecycleWorkAvailable(true);
    manager.Update(0.0f);
    assert(beginCount == 1);
    assert(editorUpdateCount == 1);
    assert(endCount == 0);

    manager.EmitRuntimeFrameBarrier(Barrier::RenderExtraction);
    manager.EmitRuntimeFrameBarrier(Barrier::RenderGraph);
    manager.EmitRuntimeFrameBarrier(Barrier::SnapshotPublication);
    manager.EndFrame();
    assert((observed == std::vector<Barrier>{Barrier::RenderExtraction, Barrier::RenderGraph,
                                             Barrier::SnapshotPublication, Barrier::PendingDestroy}));
    assert(endCount == 1);

    // Exercise the real empty-scene production flow. Physics work is skipped,
    // but its fixed-step boundaries remain observable and no extra transform
    // synchronization is introduced merely to emit a barrier.
    observed.clear();
    manager.Play();
    manager.Update(manager.GetFixedTimeStep());
    manager.LateUpdate(manager.GetFixedTimeStep());
    manager.EmitRuntimeFrameBarrier(Barrier::FinalTransformResolve);
    manager.EmitRuntimeFrameBarrier(Barrier::AnimationTimeline);
    manager.EmitRuntimeFrameBarrier(Barrier::RenderExtraction);
    manager.EmitRuntimeFrameBarrier(Barrier::RenderGraph);
    manager.EmitRuntimeFrameBarrier(Barrier::SnapshotPublication);
    manager.EndFrame();
    assert(fixedUpdateCount == 1);
    assert(updateCount == 1);
    assert(lateUpdateCount == 1);
    assert((observed == std::vector<Barrier>{
                            Barrier::TransformToPhysics,
                            Barrier::PhysicsSimulation,
                            Barrier::PhysicsToTransform,
                            Barrier::TransformResolve,
                            Barrier::FinalTransformResolve,
                            Barrier::AnimationTimeline,
                            Barrier::RenderExtraction,
                            Barrier::RenderGraph,
                            Barrier::SnapshotPublication,
                            Barrier::PendingDestroy,
                        }));
    assert(endCount == 2);
    manager.Stop();

    // Frame-cache commits retain mutation origin. A physics-authored root pose
    // must not feed the same Rigidbody back into transform-to-physics sync,
    // while descendants still observe their inherited world-space change.
    Scene *scene = manager.GetActiveScene();
    assert(scene);
    GameObject *root = scene->CreateGameObject("PhysicsPoseRoot");
    GameObject *child = scene->CreateGameObject("PhysicsPoseChild");
    child->SetParent(root, true);
    auto &transforms = TransformECSStore::Instance();
    std::vector<Transform *> invalidated;
    transforms.SetInvalidationObserver([&invalidated](Transform *transform) { invalidated.push_back(transform); });
    transforms.SyncSceneWorldMatrices(scene);
    transforms.BeginFrameCache(scene);
    transforms.SetCachedWorldPoseFromPhysics(root->GetTransform()->GetECSHandle().index, glm::vec3(1.0f, 2.0f, 3.0f),
                                             glm::quat(1.0f, 0.0f, 0.0f, 0.0f), true);
    const uint64_t revisionBeforePhysicsPose = manager.GetRenderTransformRevision();
    const bool publishedPhysicsPose = transforms.EndFrameCache();
    assert(publishedPhysicsPose);
    manager.PublishPhysicsTransformsToRenderer();
    assert(manager.GetRenderTransformRevision() != revisionBeforePhysicsPose);
    assert((invalidated == std::vector<Transform *>{child->GetTransform()}));

    invalidated.clear();
    transforms.BeginFrameCache(scene);
    root->GetTransform()->SetPosition(glm::vec3(2.0f, 3.0f, 4.0f));
    assert(!transforms.EndFrameCache());
    assert((invalidated == std::vector<Transform *>{root->GetTransform(), child->GetTransform()}));

    // Runtime authoring and bulk Instantiate may allocate transforms after
    // BeginFrameCache(). Every frame-cache array must grow in lockstep so the
    // new slot can be written and committed in the same frame.
    invalidated.clear();
    transforms.BeginFrameCache(scene);
    GameObject *runtimeCreated = scene->CreateGameObject("CreatedDuringFrameCache");
    runtimeCreated->GetTransform()->SetPosition(glm::vec3(7.0f, 8.0f, 9.0f));
    assert(!transforms.EndFrameCache());
    assert(runtimeCreated->GetTransform()->GetPosition() == glm::vec3(7.0f, 8.0f, 9.0f));
    assert((invalidated == std::vector<Transform *>{runtimeCreated->GetTransform()}));

    transforms.SetInvalidationObserver([](Transform *transform) {
        auto *gameObject = transform ? transform->GetGameObject() : nullptr;
        if (gameObject)
            PhysicsECSStore::Instance().MarkGameObjectDirty(gameObject);
    });

    manager.ClearRuntimeLifecycleCallbacks();
    manager.EmitRuntimeFrameBarrier(Barrier::SnapshotPublication);
    assert(observed.size() == 10);
    manager.UnloadAllScenes();
    return 0;
}
