#include <function/renderer/RenderWorld.h>

#include <cassert>

using infernux::RenderProxy;
using infernux::RenderWorldSnapshot;

int main()
{
    RenderProxy proxy;
    proxy.structural.objectId = 42;
    proxy.frame.visible = false;
    proxy.cache.drawCallStart = 3;
    proxy.cache.drawCallCount = 2;
    assert(proxy.structural.objectId == 42);
    assert(!proxy.frame.visible);
    assert(proxy.cache.drawCallStart == 3);
    assert(proxy.cache.drawCallCount == 2);

    RenderWorldSnapshot world;
    assert(world.FrameRevision() == 0);
    assert(!world.IsPublished());

    world.BeginFrame(7, 11);
    assert(world.WorldId() == 7);
    assert(world.StructuralRevision() == 11);
    assert(world.FrameRevision() == 1);
    assert(!world.IsPublished());
    assert(!world.MatchesSource(7, 11));

    world.Publish();
    assert(world.IsPublished());
    assert(world.MatchesSource(7, 11));
    assert(!world.MatchesSource(8, 11));
    assert(!world.MatchesSource(7, 12));

    world.BeginFrame(7, 11);
    assert(world.FrameRevision() == 2);
    assert(!world.IsPublished());
    assert(world.Proxies().empty());

    world.Clear();
    assert(world.FrameRevision() == 3);
    assert(world.WorldId() == 0);
    assert(world.StructuralRevision() == 0);
    assert(world.IsPublished());
    assert(world.Proxies().empty());

    return 0;
}
