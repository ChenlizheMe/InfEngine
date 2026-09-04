#include <atomic>
#include <chrono>
#include <condition_variable>
#include <core/threading/JobSystem.h>
#include <iostream>
#include <mutex>
#include <stdexcept>
#include <thread>

namespace
{

void Require(bool condition, const char *message)
{
    if (!condition) {
        throw std::runtime_error(message);
    }
}

void TestSchedulingAndBatchWait()
{
    infernux::JobSystem::Initialize(2);
    auto &jobs = infernux::JobSystem::Get();

    std::atomic<int> total{0};
    auto single = jobs.Schedule([&total] { total.fetch_add(1, std::memory_order_relaxed); });
    jobs.Wait(single);
    Require(single.IsComplete(), "single job did not complete");

    auto batch = jobs.ScheduleBatch(64, [&total](uint32_t index) {
        return [&total, index] { total.fetch_add(static_cast<int>(index), std::memory_order_relaxed); };
    });
    jobs.Wait(batch);
    Require(total.load(std::memory_order_relaxed) == 2017, "batch result was incomplete");

    infernux::JobSystem::Shutdown();
}

void TestExceptionPropagationKeepsPoolAlive()
{
    infernux::JobSystem::Initialize(2);
    auto &jobs = infernux::JobSystem::Get();

    auto failing = jobs.Schedule([] { throw std::runtime_error("expected failure"); });
    bool propagated = false;
    try {
        jobs.Wait(failing);
    } catch (const std::runtime_error &error) {
        propagated = std::string(error.what()) == "expected failure";
    }
    Require(propagated, "worker exception was not propagated by Wait");

    std::atomic<bool> ran{false};
    auto followUp = jobs.Schedule([&ran] { ran.store(true, std::memory_order_release); });
    jobs.Wait(followUp);
    Require(ran.load(std::memory_order_acquire), "worker pool stopped after a job exception");

    infernux::JobSystem::Shutdown();
}

void TestPassiveWaitPreservesCallerThreadAffinity()
{
    infernux::JobSystem::Initialize(1);
    auto &jobs = infernux::JobSystem::Get();
    const std::thread::id caller = std::this_thread::get_id();
    std::thread::id producer;

    auto handle = jobs.Schedule([&producer] { producer = std::this_thread::get_id(); });
    jobs.WaitPassive(handle);
    Require(producer != caller, "WaitPassive executed thread-affine work on the caller");

    infernux::JobSystem::Shutdown();
}

void TestShutdownDrainsQueue()
{
    infernux::JobSystem::Initialize(2);
    std::atomic<int> completed{0};

    infernux::JobSystem::Get().ScheduleBatch(96, [&completed](uint32_t) {
        return [&completed] {
            std::this_thread::sleep_for(std::chrono::milliseconds(1));
            completed.fetch_add(1, std::memory_order_relaxed);
        };
    });

    infernux::JobSystem::Shutdown();
    Require(completed.load(std::memory_order_relaxed) == 96, "Shutdown dropped queued jobs");
}

void TestCancellationAndObservableState()
{
    infernux::JobSystem::Initialize(1);
    auto &jobs = infernux::JobSystem::Get();
    Require(jobs.GetState() == infernux::JobSystem::State::Running, "JobSystem did not enter Running state");

    std::mutex mutex;
    std::condition_variable condition;
    bool blockerStarted = false;
    bool releaseBlocker = false;
    std::atomic<bool> cancelledTaskRan{false};
    auto blocker = jobs.Schedule([&] {
        std::unique_lock lock(mutex);
        blockerStarted = true;
        condition.notify_all();
        condition.wait(lock, [&] { return releaseBlocker; });
    });
    {
        std::unique_lock lock(mutex);
        Require(condition.wait_for(lock, std::chrono::seconds(2), [&] { return blockerStarted; }),
                "cancellation blocker did not start");
    }
    Require(jobs.GetActiveTaskCount() == 1, "active task metric did not observe the blocker");

    auto cancelled = jobs.Schedule([&] { cancelledTaskRan.store(true, std::memory_order_release); });
    Require(jobs.GetQueuedTaskCount() == 1, "queued task metric did not observe cancellable work");
    Require(cancelled.Cancel(), "queued job group rejected cancellation");
    {
        std::lock_guard lock(mutex);
        releaseBlocker = true;
    }
    condition.notify_all();
    jobs.Wait(blocker);

    bool cancellationPropagated = false;
    try {
        jobs.WaitPassive(cancelled);
    } catch (const infernux::JobCancelled &) {
        cancellationPropagated = true;
    }
    Require(cancellationPropagated, "cancelled job group did not propagate JobCancelled");
    Require(cancelled.IsComplete(), "cancelled job group did not decrement its completion counter");
    Require(!cancelledTaskRan.load(std::memory_order_acquire), "cancelled queued task still executed");
    Require(jobs.GetQueuedTaskCount() == 0 && jobs.GetActiveTaskCount() == 0,
            "JobSystem metrics did not return to idle");

    infernux::JobSystem::Shutdown();
}

void TestDrainingStateIsObservable()
{
    infernux::JobSystem::Initialize(1);
    auto &jobs = infernux::JobSystem::Get();
    std::mutex mutex;
    std::condition_variable condition;
    bool started = false;
    bool release = false;
    jobs.Schedule([&] {
        std::unique_lock lock(mutex);
        started = true;
        condition.notify_all();
        condition.wait(lock, [&] { return release; });
    });
    {
        std::unique_lock lock(mutex);
        Require(condition.wait_for(lock, std::chrono::seconds(2), [&] { return started; }),
                "draining-state blocker did not start");
    }

    std::thread shutdown([] { infernux::JobSystem::Shutdown(); });
    const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
    while (jobs.GetState() != infernux::JobSystem::State::Draining && std::chrono::steady_clock::now() < deadline)
        std::this_thread::yield();
    Require(jobs.GetState() == infernux::JobSystem::State::Draining, "JobSystem did not expose Draining state");
    {
        std::lock_guard lock(mutex);
        release = true;
    }
    condition.notify_all();
    shutdown.join();
}

void TestInvalidWorkIsRejected()
{
    infernux::JobSystem::Initialize(1);
    auto &jobs = infernux::JobSystem::Get();

    bool emptyRejected = false;
    try {
        jobs.Schedule({});
    } catch (const std::invalid_argument &) {
        emptyRejected = true;
    }
    Require(emptyRejected, "empty job was accepted");

    std::atomic<int> executed{0};
    bool factoryFailure = false;
    try {
        jobs.ScheduleBatch(8, [&executed](uint32_t index) -> infernux::JobSystem::JobFn {
            if (index == 3) {
                throw std::runtime_error("factory failure");
            }
            return [&executed] { executed.fetch_add(1, std::memory_order_relaxed); };
        });
    } catch (const std::runtime_error &) {
        factoryFailure = true;
    }
    Require(factoryFailure, "factory exception was not propagated");
    Require(executed.load(std::memory_order_relaxed) == 0, "partially-created batch was submitted");

    infernux::JobSystem::Shutdown();
}

void TestTaskGroupFenceAndClosedSubmission()
{
    infernux::JobSystem::Initialize(2);
    auto &jobs = infernux::JobSystem::Get();
    auto group = jobs.CreateTaskGroup(infernux::JobDomain::Runtime, infernux::JobPriority::Normal);
    std::atomic<int> completed{0};

    for (int i = 0; i < 16; ++i) {
        jobs.Schedule(group, [&completed] { completed.fetch_add(1, std::memory_order_relaxed); });
    }
    Require(!group.IsComplete(), "open TaskGroup was reported complete");
    group.Close();
    jobs.Wait(group.Fence());
    Require(group.IsComplete(), "TaskGroup fence completed before its jobs");
    Require(completed.load(std::memory_order_relaxed) == 16, "TaskGroup lost a submitted job");

    bool rejected = false;
    try {
        jobs.Schedule(group, [] {});
    } catch (const std::logic_error &) {
        rejected = true;
    }
    Require(rejected, "closed TaskGroup accepted new work");
    infernux::JobSystem::Shutdown();
}

void TestDomainConcurrencyPermitAndProfilerCounters()
{
    infernux::JobSystem::Initialize(4);
    auto &jobs = infernux::JobSystem::Get();
    jobs.ResetProfilerCounters();
    jobs.SetDomainConcurrency(infernux::JobDomain::Asset, 1);

    std::atomic<uint32_t> active{0};
    std::atomic<uint32_t> maximum{0};
    std::mutex mutex;
    std::condition_variable condition;
    bool blockerStarted = false;
    bool releaseBlocker = false;
    auto blocker = jobs.Schedule(
        [&] {
            active.fetch_add(1, std::memory_order_acq_rel);
            {
                std::lock_guard lock(mutex);
                blockerStarted = true;
            }
            condition.notify_all();
            std::unique_lock lock(mutex);
            condition.wait(lock, [&] { return releaseBlocker; });
            active.fetch_sub(1, std::memory_order_acq_rel);
        },
        infernux::JobDomain::Asset);
    {
        std::unique_lock lock(mutex);
        Require(condition.wait_for(lock, std::chrono::seconds(2), [&] { return blockerStarted; }),
                "domain permit blocker did not start");
    }

    auto handle = jobs.ScheduleBatch(
        32,
        [&active, &maximum](uint32_t) {
            return [&active, &maximum] {
                const uint32_t now = active.fetch_add(1, std::memory_order_acq_rel) + 1;
                uint32_t old = maximum.load(std::memory_order_relaxed);
                while (old < now && !maximum.compare_exchange_weak(old, now, std::memory_order_relaxed)) {
                }
                std::this_thread::sleep_for(std::chrono::milliseconds(1));
                active.fetch_sub(1, std::memory_order_acq_rel);
            };
        },
        infernux::JobDomain::Asset);
    const auto blockedDeadline = std::chrono::steady_clock::now() + std::chrono::seconds(2);
    while (jobs.GetProfilerCounters().blocked == 0 && std::chrono::steady_clock::now() < blockedDeadline)
        std::this_thread::yield();
    {
        std::lock_guard lock(mutex);
        releaseBlocker = true;
    }
    condition.notify_all();
    jobs.Wait(blocker);
    jobs.Wait(handle);

    Require(maximum.load(std::memory_order_relaxed) <= 1, "domain concurrency permit was bypassed");
    Require(jobs.GetDomainActiveCount(infernux::JobDomain::Asset) == 0,
            "domain permit was not released after completion");
    const auto global = jobs.GetProfilerCounters();
    const auto asset = jobs.GetProfilerCounters(infernux::JobDomain::Asset);
    Require(global.submitted == 33 && global.completed == 33, "global profiler counters are incomplete");
    Require(asset.submitted == 33 && asset.completed == 33, "domain profiler counters are incomplete");
    Require(global.blocked > 0, "domain contention was not reflected in profiler counters");

    jobs.SetDomainConcurrency(infernux::JobDomain::Asset, 0);
    infernux::JobSystem::Shutdown();
}

void TestTaskGroupCancellationAndException()
{
    infernux::JobSystem::Initialize(2);
    auto &jobs = infernux::JobSystem::Get();

    // Keep the task pending until cancellation has been published. Without
    // this gate the worker may legitimately begin the callback before
    // Cancel(), which tests scheduling luck rather than queued cancellation.
    jobs.SetDomainConcurrency(infernux::JobDomain::Runtime, 1);
    std::mutex cancellationMutex;
    std::condition_variable cancellationCondition;
    bool blockerStarted = false;
    bool releaseBlocker = false;
    auto cancellationBlocker = jobs.Schedule(
        [&] {
            std::unique_lock lock(cancellationMutex);
            blockerStarted = true;
            cancellationCondition.notify_all();
            cancellationCondition.wait(lock, [&] { return releaseBlocker; });
        },
        infernux::JobDomain::Runtime);
    {
        std::unique_lock lock(cancellationMutex);
        Require(cancellationCondition.wait_for(lock, std::chrono::seconds(2), [&] { return blockerStarted; }),
                "TaskGroup cancellation blocker did not start");
    }

    auto cancelledGroup = jobs.CreateTaskGroup(infernux::JobDomain::Runtime);
    std::atomic<bool> cancelledTaskRan{false};
    jobs.Schedule(cancelledGroup, [&cancelledTaskRan] { cancelledTaskRan.store(true, std::memory_order_release); });
    Require(jobs.GetQueuedTaskCount() == 1, "TaskGroup cancellation target did not remain queued");
    Require(cancelledGroup.Cancel(), "TaskGroup rejected cancellation");
    cancelledGroup.Close();
    {
        std::lock_guard lock(cancellationMutex);
        releaseBlocker = true;
    }
    cancellationCondition.notify_all();
    jobs.Wait(cancellationBlocker);
    bool cancellationPropagated = false;
    try {
        jobs.Wait(cancelledGroup.Fence());
    } catch (const infernux::JobCancelled &) {
        cancellationPropagated = true;
    }
    Require(cancellationPropagated, "TaskGroup cancellation was not propagated");
    Require(!cancelledTaskRan.load(std::memory_order_acquire), "cancelled TaskGroup task executed");
    jobs.SetDomainConcurrency(infernux::JobDomain::Runtime, 0);

    auto failingGroup = jobs.CreateTaskGroup(infernux::JobDomain::Runtime);
    jobs.Schedule(failingGroup, [] { throw std::runtime_error("group failure"); });
    failingGroup.Close();
    bool failurePropagated = false;
    try {
        jobs.Wait(failingGroup.Fence());
    } catch (const std::runtime_error &error) {
        failurePropagated = std::string(error.what()) == "group failure";
    }
    Require(failurePropagated, "TaskGroup exception was not propagated");
    infernux::JobSystem::Shutdown();
}

void TestGroupAwareNestedWaitReleasesPermit()
{
    infernux::JobSystem::Initialize(2);
    auto &jobs = infernux::JobSystem::Get();
    jobs.SetDomainConcurrency(infernux::JobDomain::Runtime, 1);
    auto group = jobs.CreateTaskGroup(infernux::JobDomain::Runtime);
    std::atomic<bool> childRan{false};

    auto parent = jobs.Schedule(group, [&jobs, &group, &childRan] {
        auto child = jobs.Schedule(group, [&childRan] { childRan.store(true, std::memory_order_release); });
        jobs.Wait(child);
    });
    jobs.Wait(parent);
    group.Close();
    jobs.Wait(group.Fence());
    Require(childRan.load(std::memory_order_acquire), "nested group wait deadlocked behind its domain permit");
    Require(jobs.GetDomainActiveCount(infernux::JobDomain::Runtime) == 0, "nested group wait leaked its domain permit");
    jobs.SetDomainConcurrency(infernux::JobDomain::Runtime, 0);
    infernux::JobSystem::Shutdown();
}

void TestPriorityAgingPreventsStarvation()
{
    infernux::JobSystem::Initialize(1);
    auto &jobs = infernux::JobSystem::Get();
    std::mutex mutex;
    std::condition_variable condition;
    bool blockerStarted = false;
    bool releaseBlocker = false;

    auto blocker = jobs.Schedule(
        [&] {
            std::unique_lock lock(mutex);
            blockerStarted = true;
            condition.notify_all();
            condition.wait(lock, [&] { return releaseBlocker; });
        },
        infernux::JobDomain::Runtime, infernux::JobPriority::Normal);
    {
        std::unique_lock lock(mutex);
        Require(condition.wait_for(lock, std::chrono::seconds(2), [&] { return blockerStarted; }),
                "priority test blocker did not start");
    }

    std::atomic<bool> lowRan{false};
    auto high = jobs.ScheduleBatch(
        128, [](uint32_t) { return [] {}; }, infernux::JobDomain::Runtime, infernux::JobPriority::High);
    auto low = jobs.Schedule([&lowRan] { lowRan.store(true, std::memory_order_release); }, infernux::JobDomain::Runtime,
                             infernux::JobPriority::Low);
    {
        std::lock_guard lock(mutex);
        releaseBlocker = true;
    }
    condition.notify_all();
    jobs.Wait(blocker);
    jobs.Wait(low);
    jobs.Wait(high);
    Require(lowRan.load(std::memory_order_acquire), "low priority work was starved by high priority work");
    infernux::JobSystem::Shutdown();
}

void TestWaitHelpIsProfiled()
{
    infernux::JobSystem::Initialize(1);
    auto &jobs = infernux::JobSystem::Get();
    jobs.ResetProfilerCounters();
    std::mutex mutex;
    std::condition_variable condition;
    bool blockerStarted = false;
    bool releaseBlocker = false;

    auto blocker = jobs.Schedule([&] {
        std::unique_lock lock(mutex);
        blockerStarted = true;
        condition.notify_all();
        condition.wait(lock, [&] { return releaseBlocker; });
    });
    {
        std::unique_lock lock(mutex);
        Require(condition.wait_for(lock, std::chrono::seconds(2), [&] { return blockerStarted; }),
                "wait-help blocker did not start");
    }
    std::atomic<bool> helpedTaskRan{false};
    auto helped = jobs.Schedule([&helpedTaskRan] { helpedTaskRan.store(true, std::memory_order_release); });
    jobs.Wait(helped);
    Require(helpedTaskRan.load(std::memory_order_acquire), "Wait did not help queued work");
    Require(jobs.GetProfilerCounters().helped > 0, "Wait help was not counted");
    {
        std::lock_guard lock(mutex);
        releaseBlocker = true;
    }
    condition.notify_all();
    jobs.Wait(blocker);
    infernux::JobSystem::Shutdown();
}

void TestInlineExecutionMode()
{
    infernux::JobSystem::InitializeInline();
    auto &jobs = infernux::JobSystem::Get();
    Require(jobs.IsInline(), "inline JobSystem did not expose its execution mode");
    Require(jobs.GetWorkerCount() == 0, "inline JobSystem created a worker thread");

    std::thread::id executionThread;
    const std::thread::id ownerThread = std::this_thread::get_id();
    auto deferred = jobs.Schedule([&executionThread] { executionThread = std::this_thread::get_id(); });
    Require(!deferred.IsComplete(), "inline work ran during submission");
    Require(jobs.RunPendingJobs(1) == 1, "inline pump did not execute one queued job");
    Require(deferred.IsComplete(), "inline pump did not complete its handle");
    Require(executionThread == ownerThread, "inline work escaped the owner thread");

    std::atomic<uint32_t> batchCount{0};
    auto batch = jobs.ScheduleBatch(
        8, [&batchCount](uint32_t) { return [&batchCount] { batchCount.fetch_add(1, std::memory_order_relaxed); }; });
    jobs.WaitPassive(batch);
    Require(batchCount.load(std::memory_order_relaxed) == 8, "inline passive wait did not drain the target work");

    infernux::JobSystem::Get().Schedule([&batchCount] { batchCount.fetch_add(1, std::memory_order_relaxed); });
    infernux::JobSystem::Shutdown();
    Require(batchCount.load(std::memory_order_relaxed) == 9, "inline shutdown dropped queued work");
}

} // namespace

int main()
{
    try {
        TestSchedulingAndBatchWait();
        TestExceptionPropagationKeepsPoolAlive();
        TestPassiveWaitPreservesCallerThreadAffinity();
        TestShutdownDrainsQueue();
        TestCancellationAndObservableState();
        TestDrainingStateIsObservable();
        TestInvalidWorkIsRejected();
        TestTaskGroupFenceAndClosedSubmission();
        TestDomainConcurrencyPermitAndProfilerCounters();
        TestTaskGroupCancellationAndException();
        TestGroupAwareNestedWaitReleasesPermit();
        TestPriorityAgingPreventsStarvation();
        TestWaitHelpIsProfiled();
        TestInlineExecutionMode();
    } catch (const std::exception &error) {
        std::cerr << "JobSystem test failed: " << error.what() << '\n';
        infernux::JobSystem::Shutdown();
        return 1;
    }

    std::cout << "JobSystem tests passed\n";
    return 0;
}
