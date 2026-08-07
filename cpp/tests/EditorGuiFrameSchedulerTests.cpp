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

    const auto requestedSnapshot = scheduler.Inspect(start + 19ms);
    assert(requestedSnapshot.started);
    assert(!requestedSnapshot.requested);
    assert(requestedSnapshot.requestCount == 1);
    assert(requestedSnapshot.approvedCount == 3);

    assert(scheduler.Consume(start + 20ms, true));
    // A held immediate-refresh condition is coalesced until the normal
    // cadence. The next false frame arms the following input edge.
    assert(!scheduler.Consume(start + 21ms, true));
    assert(!scheduler.Consume(start + 22ms));
    assert(scheduler.Consume(start + 23ms, true));
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

    const auto quantizedSnapshot = quantized.Inspect(start + 1s);
    assert(quantizedSnapshot.consumeCount == 921);
    assert(quantizedSnapshot.approvedCount == static_cast<uint64_t>(builds));

    // Editor Play remains on the authoring cadence even when its immediate
    // refresh condition is held by repeated input events.
    EditorGuiFrameScheduler editorPlay;
    int editorPlayBuilds = 0;
    for (int frame = 0; frame < 120; ++frame) {
        editorPlayBuilds += editorPlay.Consume(start + frame * 1ms, true) ? 1 : 0;
    }
    assert(editorPlayBuilds >= 7 && editorPlayBuilds <= 8);

    // Player mode is deliberately unthrottled. This is a separate API path,
    // not a permanently-held editor force flag, so Editor Play can retain the
    // 60 Hz authoring cadence without slowing the exported game.
    EditorGuiFrameScheduler player;
    int playerBuilds = 0;
    for (int frame = 0; frame < 120; ++frame) {
        playerBuilds += player.ConsumeUnthrottled(start + frame * 1ms, true) ? 1 : 0;
    }
    assert(playerBuilds == 120);
    const auto playerSnapshot = player.Inspect(start + 120ms);
    assert(playerSnapshot.consumeCount == 120);
    assert(playerSnapshot.approvedCount == 120);

    return 0;
}
