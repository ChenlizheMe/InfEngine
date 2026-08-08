#include <function/renderer/gui/EditorGuiFrameScheduler.h>

#include <cassert>
#include <chrono>

using infernux::EditorGuiFrameScheduler;
using infernux::EditorGuiInputRearmBudget;

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
    // An explicit wake request represents a new input batch and must bypass
    // the held immediate-refresh level, even while the force condition stays
    // true. This is what keeps synthetic down/up events aligned with GUI
    // builds without making the editor permanently unthrottled.
    scheduler.Request();
    assert(scheduler.Consume(start + 21ms, true));
    assert(!scheduler.Consume(start + 22ms));
    assert(scheduler.Consume(start + 23ms, true));
    assert(!scheduler.Consume(start + 33ms));
    assert(scheduler.Consume(start + 34ms));

    // ImGui may trickle one submitted input batch over several NewFrame calls.
    // The GUI re-arms a coalesced request after each build that still has
    // queued transitions. Every re-arm must grant exactly one following build
    // without changing the idle cadence once the queue is empty.
    EditorGuiFrameScheduler trickledInput;
    assert(trickledInput.Consume(start));
    for (int frame = 1; frame <= 3; ++frame) {
        trickledInput.Request();
        assert(trickledInput.Consume(start + frame * 1ms));
    }
    assert(!trickledInput.Consume(start + 4ms));
    const auto trickledSnapshot = trickledInput.Inspect(start + 4ms);
    assert(!trickledSnapshot.requested);
    assert(trickledSnapshot.requestCount == 3);
    assert(trickledSnapshot.approvedCount == 4);

    EditorGuiInputRearmBudget rearm;
    rearm.BeginBatch();
    assert(rearm.Remaining() == EditorGuiInputRearmBudget::kMaxFrames);
    assert(rearm.AfterBuild(true));
    assert(rearm.AfterBuild(true));
    assert(rearm.AfterBuild(true));
    assert(!rearm.AfterBuild(true));
    assert(rearm.Remaining() == 0);
    rearm.BeginBatch();
    assert(!rearm.AfterBuild(false));
    assert(rearm.Remaining() == 0);

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
