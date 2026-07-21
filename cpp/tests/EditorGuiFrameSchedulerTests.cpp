#include <function/renderer/gui/EditorGuiFrameScheduler.h>

#include <cassert>
#include <chrono>

using infernux::EditorGuiFrameScheduler;

int main()
{
    using namespace std::chrono_literals;

    EditorGuiFrameScheduler scheduler;
    const auto start = EditorGuiFrameScheduler::TimePoint{};

    assert(scheduler.Consume(start));
    assert(!scheduler.Consume(start + 1ms));
    assert(!scheduler.Consume(start + 6ms));
    assert(!scheduler.Consume(start + 16ms));
    assert(scheduler.Consume(start + 17ms));

    scheduler.Request();
    assert(scheduler.Consume(start + 18ms));
    assert(!scheduler.Consume(start + 19ms));

    assert(scheduler.Consume(start + 20ms, true));
    assert(!scheduler.Consume(start + 21ms));
    assert(!scheduler.Consume(start + 33ms));
    assert(scheduler.Consume(start + 34ms));

    int builds = 0;
    EditorGuiFrameScheduler quantized;
    for (int frame = 0; frame <= 920; ++frame) {
        const auto now = start + std::chrono::duration_cast<EditorGuiFrameScheduler::Clock::duration>(
                                     std::chrono::duration<double>(static_cast<double>(frame) / 920.0));
        builds += quantized.Consume(now) ? 1 : 0;
    }
    assert(builds >= 60 && builds <= 61);

    return 0;
}
