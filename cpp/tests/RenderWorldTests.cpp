#include <function/renderer/RenderWorld.h>

#include <cassert>
#include <type_traits>

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

    world.Clear();
    assert(world.FrameRevision() == 1);
    assert(world.IsPublished());
    assert(world.WorldId() == 0);
    assert(world.StructuralRevision() == 0);
    assert(world.MatchesSource(0, 0));
    assert(world.Acquire()->Proxies().empty());

    const auto firstPublication = world.Acquire();
    assert(firstPublication);
    assert(firstPublication->FrameRevision() == 1);

    // A new publication must not mutate a frame retained by a render consumer.
    world.Clear();
    assert(world.FrameRevision() == 2);
    const auto secondPublication = world.Acquire();
    assert(secondPublication);
    assert(secondPublication != firstPublication);
    assert(secondPublication->FrameRevision() == 2);
    assert(firstPublication->FrameRevision() == 1);

    static_assert(std::is_same_v<decltype(world.Acquire()), std::shared_ptr<const infernux::RenderWorldFrame>>);

    return 0;
}
