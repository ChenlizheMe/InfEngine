#include "platform/filesystem/DocumentStore.h"

#include <condition_variable>
#include <filesystem>
#include <fstream>
#include <iterator>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

using infernux::DocumentStore;
using infernux::DocumentWriteCancelled;
using infernux::DocumentWriteSuperseded;

namespace
{

void Require(bool condition, const char *message)
{
    if (!condition)
        throw std::runtime_error(message);
}

void TestCoalescesQueuedGenerations()
{
    std::mutex mutex;
    std::condition_variable condition;
    bool firstStarted = false;
    bool releaseFirst = false;
    std::vector<std::string> writes;

    DocumentStore store([&](const std::string &, const std::string &content, const infernux::DocumentWriteOptions &) {
        std::unique_lock lock(mutex);
        writes.push_back(content);
        if (content == "first") {
            firstStarted = true;
            condition.notify_all();
            condition.wait(lock, [&] { return releaseFirst; });
        }
    });

    auto first = store.Submit("coalesced.scene", "first");
    {
        std::unique_lock lock(mutex);
        Require(condition.wait_for(lock, std::chrono::seconds(2), [&] { return firstStarted; }),
                "first generation did not start");
    }
    auto second = store.Submit("coalesced.scene", "second");
    auto third = store.Submit("coalesced.scene", "third");

    bool superseded = false;
    try {
        second->Wait();
    } catch (const DocumentWriteSuperseded &) {
        superseded = true;
    }
    Require(superseded, "intermediate generation was not marked superseded");

    {
        std::lock_guard lock(mutex);
        releaseFirst = true;
    }
    condition.notify_all();
    first->Wait();
    third->Wait();
    store.Flush();
    store.Shutdown();

    Require(writes.size() == 2, "coalesced store performed an extra write");
    Require(writes[0] == "first" && writes[1] == "third", "coalesced store wrote the wrong generations");
    Require(first->GetGeneration() + 2 == third->GetGeneration(), "generation sequence is not monotonic");
}

void TestShutdownDrainsAndRestarts()
{
    std::vector<std::string> writes;
    DocumentStore store([&](const std::string &, const std::string &content, const infernux::DocumentWriteOptions &) {
        writes.push_back(content);
    });
    auto accepted = store.Submit("drain.scene", "accepted");
    store.Shutdown();
    accepted->Wait();
    Require(writes == std::vector<std::string>{"accepted"}, "shutdown did not drain accepted work");

    auto restarted = store.Submit("drain.scene", "next lifetime");
    restarted->Wait();
    store.Shutdown();
    Require(writes == std::vector<std::string>({"accepted", "next lifetime"}), "store did not restart cleanly");
}

void TestDifferentPathsRunConcurrently()
{
    std::mutex mutex;
    std::condition_variable condition;
    int started = 0;
    bool release = false;
    DocumentStore store(
        [&](const std::string &, const std::string &, const infernux::DocumentWriteOptions &) {
            std::unique_lock lock(mutex);
            ++started;
            condition.notify_all();
            condition.wait(lock, [&] { return release; });
        },
        2);

    auto first = store.Submit("parallel-a.scene", "a");
    auto second = store.Submit("parallel-b.scene", "b");
    {
        std::unique_lock lock(mutex);
        Require(condition.wait_for(lock, std::chrono::seconds(2), [&] { return started == 2; }),
                "different paths did not execute concurrently");
        release = true;
    }
    condition.notify_all();
    first->Wait();
    second->Wait();
    store.Shutdown();
}

void TestWriterFailurePropagates()
{
    DocumentStore store([](const std::string &, const std::string &, const infernux::DocumentWriteOptions &) {
        throw std::runtime_error("disk full");
    });
    auto failed = store.Submit("failed.scene", "content");
    bool propagated = false;
    try {
        failed->Wait();
    } catch (const std::runtime_error &error) {
        propagated = std::string(error.what()) == "disk full";
    }
    store.Shutdown();
    Require(propagated, "writer failure was not propagated through the ticket");
    Require(failed->GetStatusName() == "failed", "failed ticket did not expose its terminal status");
    Require(failed->GetError() == "disk full", "failed ticket did not preserve its diagnostic");
    Require(store.GetMetrics("failed.scene").latestFailedGeneration == failed->GetGeneration(),
            "failed generation metric was not recorded");
}

void TestFlushPublishesTerminalTicketState()
{
    DocumentStore store([](const std::string &, const std::string &, const infernux::DocumentWriteOptions &) {});
    for (int index = 0; index < 256; ++index) {
        const std::string path = "flush-ticket-" + std::to_string(index) + ".scene";
        auto ticket = store.Submit(path, "content");
        store.Flush(path);
        Require(ticket->IsComplete(), "path flush returned before its ticket reached a terminal state");
        Require(ticket->GetStatusName() == "succeeded", "path flush exposed a non-success terminal state");
    }
    store.Shutdown();
}

void TestWriterPreservesPathCasing()
{
    std::string writtenPath;
    DocumentStore store([&](const std::string &path, const std::string &, const infernux::DocumentWriteOptions &) {
        writtenPath = path;
    });
    auto ticket = store.Submit("ResultsLightLift.animtimeline", "content");
    ticket->Wait();
    store.Shutdown();
    const std::string expectedSuffix = "ResultsLightLift.animtimeline";
    Require(
        writtenPath.size() >= expectedSuffix.size() &&
            writtenPath.compare(writtenPath.size() - expectedSuffix.size(), expectedSuffix.size(), expectedSuffix) == 0,
        "writer path casing was not preserved");
}

void TestQueuedCancellationAndGenerationMetrics()
{
    std::mutex mutex;
    std::condition_variable condition;
    bool blockerStarted = false;
    bool releaseBlocker = false;
    std::vector<std::string> writes;
    DocumentStore store(
        [&](const std::string &, const std::string &content, const infernux::DocumentWriteOptions &) {
            std::unique_lock lock(mutex);
            writes.push_back(content);
            if (content == "blocker") {
                blockerStarted = true;
                condition.notify_all();
                condition.wait(lock, [&] { return releaseBlocker; });
            }
        },
        1);

    auto blocker = store.Submit("cancel-blocker.scene", "blocker");
    {
        std::unique_lock lock(mutex);
        Require(condition.wait_for(lock, std::chrono::seconds(2), [&] { return blockerStarted; }),
                "cancellation blocker did not start");
    }
    Require(!store.Cancel(blocker), "active generation was incorrectly cancelled");

    auto queued = store.Submit("cancel-target.scene", "must-not-write");
    const auto queuedMetrics = store.GetMetrics("cancel-target.scene");
    Require(queuedMetrics.latestSubmittedGeneration == queued->GetGeneration(),
            "submitted generation metric was not recorded");
    Require(queuedMetrics.pendingGeneration == queued->GetGeneration(), "pending generation metric was not recorded");
    Require(store.Cancel(queued), "queued generation could not be cancelled");
    Require(queued->IsComplete() && queued->GetStatusName() == "cancelled",
            "cancelled ticket did not expose its terminal status");
    Require(store.GetMetrics("cancel-target.scene").pendingGeneration == 0,
            "cancelled generation remained visible as pending");

    bool cancelled = false;
    try {
        queued->Wait();
    } catch (const DocumentWriteCancelled &) {
        cancelled = true;
    }
    Require(cancelled, "cancelled ticket did not propagate DocumentWriteCancelled");

    {
        std::lock_guard lock(mutex);
        releaseBlocker = true;
    }
    condition.notify_all();
    blocker->Wait();
    store.Shutdown();
    Require(writes == std::vector<std::string>{"blocker"}, "cancelled generation reached the writer");
    Require(store.GetMetrics("cancel-blocker.scene").latestSucceededGeneration == blocker->GetGeneration(),
            "successful generation metric was not recorded");
}

void TestConditionalWriteRejectsAChangedTarget()
{
    const auto path = std::filesystem::temp_directory_path() /
                      ("infernux-document-cas-" + std::to_string(
                           std::chrono::steady_clock::now().time_since_epoch().count()) +
                       ".txt");
    {
        std::ofstream file(path, std::ios::binary | std::ios::trunc);
        file << "before";
    }
    DocumentStore store;
    const auto expected = store.CaptureFileState(path.string());
    const auto originalTime = std::filesystem::last_write_time(path);
    {
        std::ofstream file(path, std::ios::binary | std::ios::trunc);
        file << "extern";
    }
    std::filesystem::last_write_time(path, originalTime);

    infernux::DocumentWriteOptions options;
    options.expectedFileState = expected;
    auto rejected = store.Submit(path.string(), "editor", options);
    bool failed = false;
    try {
        rejected->Wait();
    } catch (const std::runtime_error &error) {
        failed = std::string(error.what()).find("changed outside the editor") != std::string::npos;
    }
    std::ifstream current(path, std::ios::binary);
    const std::string contents((std::istreambuf_iterator<char>(current)), std::istreambuf_iterator<char>());
    store.Shutdown();
    std::error_code ignored;
    std::filesystem::remove(path, ignored);

    Require(failed, "conditional write did not reject an externally changed target");
    Require(contents == "extern", "conditional write overwrote the external target");
}

} // namespace

int main()
{
    try {
        TestCoalescesQueuedGenerations();
        TestShutdownDrainsAndRestarts();
        TestDifferentPathsRunConcurrently();
        TestWriterFailurePropagates();
        TestFlushPublishesTerminalTicketState();
        TestWriterPreservesPathCasing();
        TestQueuedCancellationAndGenerationMetrics();
        TestConditionalWriteRejectsAChangedTarget();
        std::cout << "DocumentStore tests passed\n";
        return 0;
    } catch (const std::exception &error) {
        std::cerr << "DocumentStore test failure: " << error.what() << '\n';
        return 1;
    }
}
