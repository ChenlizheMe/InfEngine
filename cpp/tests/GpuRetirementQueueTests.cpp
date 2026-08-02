#include <function/renderer/rhi/GpuRetirementQueue.h>

#include <cassert>
#include <stdexcept>
#include <utility>

using infernux::GpuRetirementQueue;

int main()
{
    GpuRetirementQueue unbound;
    bool rejectedUnboundRetirement = false;
    try {
        unbound.Retire([] {});
    } catch (const std::logic_error &) {
        rejectedUnboundRetirement = true;
    }
    assert(rejectedUnboundRetirement);

    infernux::rhi::SubmissionSerial latestSubmission = 7;
    GpuRetirementQueue queue;
    queue.BindSerialSource([&] { return latestSubmission; });

    int retired = 0;
    queue.Retire([&] { ++retired; });
    assert(queue.Collect(6) == 0);
    assert(retired == 0);
    assert(queue.Collect(7) == 1);
    assert(retired == 1);

    latestSubmission = 11;
    queue.Retire([&] {
        ++retired;
        queue.RetireAfter(12, [&] { retired += 10; });
    });
    assert(queue.Collect(10) == 0);
    assert(queue.Collect(11) == 1);
    assert(retired == 2);
    assert(queue.PendingCount() == 1);
    assert(queue.Collect(12) == 1);
    assert(retired == 12);

    int destinationRetired = 0;
    GpuRetirementQueue destination;
    destination.RetireAfter(1, [&] { ++destinationRetired; });
    GpuRetirementQueue source;
    source.RetireAfter(2, [&] { destinationRetired += 10; });
    destination = std::move(source);
    assert(destinationRetired == 1);
    destination.FlushAll();
    assert(destinationRetired == 11);

    const auto stats = queue.GetStats();
    assert(stats.collectCalls == 5);
    assert(stats.pushed == 3);
    assert(stats.retired == 3);
    assert(stats.pending == 0);
    assert(stats.highWatermark >= 1);
    return 0;
}
