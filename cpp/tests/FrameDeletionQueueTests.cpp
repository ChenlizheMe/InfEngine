#include <function/renderer/FrameDeletionQueue.h>

#include <cassert>
#include <utility>

using infernux::FrameDeletionQueue;

int main()
{
    FrameDeletionQueue queue;
    queue.Initialize(2);

    int retired = 0;
    queue.Push([&] { ++retired; });
    queue.Tick();
    queue.Tick();
    assert(retired == 0);
    queue.Tick();
    assert(retired == 1);

    queue.Push([&] {
        ++retired;
        queue.Push([&] { retired += 10; });
    });
    queue.Tick();
    queue.Tick();
    queue.Tick();
    assert(retired == 2);
    assert(queue.PendingCount() == 1);
    queue.FlushAll();
    assert(retired == 12);

    int destinationRetired = 0;
    FrameDeletionQueue destination;
    destination.Push([&] { ++destinationRetired; });
    FrameDeletionQueue source;
    source.Push([&] { destinationRetired += 10; });
    destination = std::move(source);
    assert(destinationRetired == 1);
    destination.FlushAll();
    assert(destinationRetired == 11);

    const auto stats = queue.GetStats();
    assert(stats.completedFenceTicks == 6);
    assert(stats.pushed == 3);
    assert(stats.retired == 3);
    assert(stats.pending == 0);
    assert(stats.highWatermark >= 1);

    return 0;
}
