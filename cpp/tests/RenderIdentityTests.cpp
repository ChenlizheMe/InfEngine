#include <function/renderer/RenderIdentity.h>

#include <cassert>

using infernux::ObjectHandle;
using infernux::RenderDomain;
using infernux::RenderDrawIdentity;
using infernux::RenderProxyHandle;

int main()
{
    const ObjectHandle object{10, 2, 7};
    const ObjectHandle renderer{20, 4, 7};
    const RenderProxyHandle sceneProxy = RenderProxyHandle::FromScene(object, renderer);

    assert(sceneProxy.IsValid());
    assert(sceneProxy.IsSceneBacked());
    assert(sceneProxy == RenderProxyHandle::FromScene(object, renderer));

    const RenderProxyHandle replacedRenderer =
        RenderProxyHandle::FromScene(object, ObjectHandle{renderer.id, renderer.generation + 1, renderer.worldId});
    assert(replacedRenderer.IsValid());
    assert(replacedRenderer != sceneProxy);

    const RenderProxyHandle sameGenerationDifferentObject =
        RenderProxyHandle::FromScene(ObjectHandle{object.id + 1, object.generation, object.worldId},
                                     ObjectHandle{renderer.id + 1, renderer.generation, renderer.worldId});
    assert(sameGenerationDifferentObject.IsValid());
    assert(sameGenerationDifferentObject.MakeDrawIdentity() != sceneProxy.MakeDrawIdentity());

    const RenderProxyHandle sameGenerationDifferentWorld =
        RenderProxyHandle::FromScene(ObjectHandle{object.id, object.generation, object.worldId + 1},
                                     ObjectHandle{renderer.id, renderer.generation, renderer.worldId + 1});
    assert(sameGenerationDifferentWorld.IsValid());
    assert(sameGenerationDifferentWorld.MakeDrawIdentity() != sceneProxy.MakeDrawIdentity());

    const RenderProxyHandle reloadedWorld =
        RenderProxyHandle::FromScene(ObjectHandle{object.id, object.generation, object.worldId + 1},
                                     ObjectHandle{renderer.id, renderer.generation + 2, renderer.worldId + 1});
    assert(reloadedWorld.IsValid());
    assert(reloadedWorld != sceneProxy);

    const RenderProxyHandle mismatchedWorld =
        RenderProxyHandle::FromScene(object, ObjectHandle{renderer.id, renderer.generation, renderer.worldId + 1});
    assert(!mismatchedWorld.IsValid());
    assert(!mismatchedWorld.MakeDrawIdentity().IsValid());

    const RenderProxyHandle particle = RenderProxyHandle::Synthetic(RenderDomain::Particle, 99);
    assert(particle.IsValid());
    assert(!particle.IsSceneBacked());
    assert(!RenderProxyHandle::Synthetic(RenderDomain::Particle, 0).IsValid());
    assert(!RenderProxyHandle::Synthetic(RenderDomain::Unknown, 99).IsValid());

    const RenderDrawIdentity firstPrimitive = sceneProxy.MakeDrawIdentity(0);
    const RenderDrawIdentity secondPrimitive = sceneProxy.MakeDrawIdentity(1);
    assert(firstPrimitive.IsValid());
    assert(firstPrimitive != secondPrimitive);
    assert(firstPrimitive != replacedRenderer.MakeDrawIdentity(0));
    assert(firstPrimitive != reloadedWorld.MakeDrawIdentity(0));
    assert(particle.MakeDrawIdentity().IsValid());

    return 0;
}
