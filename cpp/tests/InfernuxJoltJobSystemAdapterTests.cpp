#include <Jolt/Jolt.h>

#include <Jolt/Core/Factory.h>

#include <core/threading/JobSystem.h>
#include <function/scene/physics/InfernuxJoltJobSystemAdapter.h>

#include <atomic>
#include <cassert>
#include <stdexcept>

using infernux::InfernuxJoltJobSystemAdapter;
using infernux::JobDomain;
using infernux::JobPriority;
using infernux::JobSystem;

namespace
{

void TestDependenciesAndBarrier()
{
    InfernuxJoltJobSystemAdapter adapter(64, 8, 1);
    auto group = JobSystem::Get().CreateTaskGroup(JobDomain::Physics, JobPriority::Critical);
    adapter.BeginFrame(group);

    std::atomic<int> runs{0};
    JPH::JobSystem::JobHandle dependent;
    auto dependency = adapter.CreateJob(
        "dependency", JPH::Color::sGreen,
        [&] {
            runs.fetch_add(1);
            dependent.RemoveDependency();
        },
        1);
    dependent = adapter.CreateJob("dependent", JPH::Color::sBlue, [&] { runs.fetch_add(10); }, 1);
    auto *barrier = adapter.CreateBarrier();
    assert(barrier != nullptr);
    barrier->AddJob(dependency);
    barrier->AddJob(dependent);
    dependency.RemoveDependency();
    adapter.WaitForJobs(barrier);
    adapter.DestroyBarrier(barrier);
    adapter.EndFrame();
    group.Close();
    JobSystem::Get().Wait(group);
    assert(runs.load() == 10);
}

void TestDynamicBarrierAndException()
{
    InfernuxJoltJobSystemAdapter adapter(64, 8, 0);
    auto group = JobSystem::Get().CreateTaskGroup(JobDomain::Physics, JobPriority::Critical);
    adapter.BeginFrame(group);

    std::atomic<int> childRuns{0};
    auto *barrier = adapter.CreateBarrier();
    assert(barrier != nullptr);
    auto parent = adapter.CreateJob(
        "parent", JPH::Color::sYellow,
        [&] {
            auto child = adapter.CreateJob("child", JPH::Color::sOrange, [&] { childRuns.fetch_add(1); }, 0);
            barrier->AddJob(child);
            throw std::runtime_error("adapter must contain user exceptions");
        },
        1);
    barrier->AddJob(parent);
    parent.RemoveDependency();
    adapter.WaitForJobs(barrier);
    adapter.DestroyBarrier(barrier);
    adapter.EndFrame();
    group.Close();
    JobSystem::Get().Wait(group);
    assert(childRuns.load() == 1);
    assert(adapter.GetCaughtExceptionCount() == 1);
}

void TestConcurrencyAndCancellation()
{
    JobSystem::Get().SetDomainConcurrency(JobDomain::Physics, 1);
    InfernuxJoltJobSystemAdapter serial(64, 8, 0);
    assert(serial.GetMaxConcurrency() == 1);
    assert(serial.GetOwnedWorkerThreadCount() == 0);

    auto cancelled = JobSystem::Get().CreateTaskGroup(JobDomain::Physics, JobPriority::Critical);
    assert(cancelled.Cancel());
    bool rejected = false;
    try {
        serial.BeginFrame(cancelled);
    } catch (const std::logic_error &) {
        rejected = true;
    }
    assert(rejected);

    JobSystem::Get().SetDomainConcurrency(JobDomain::Physics, 0);
    InfernuxJoltJobSystemAdapter parallel(64, 8, 0);
    assert(parallel.GetMaxConcurrency() >= 1);
    assert(parallel.GetOwnedWorkerThreadCount() == 0);
}

void TestShutdownRejectsNewJobs()
{
    InfernuxJoltJobSystemAdapter adapter(8, 2, 1);
    adapter.Shutdown();
    bool rejected = false;
    try {
        (void)adapter.CreateJob("after-shutdown", JPH::Color::sRed, [] {}, 0);
    } catch (const std::runtime_error &) {
        rejected = true;
    }
    assert(rejected);
    assert(adapter.GetOwnedWorkerThreadCount() == 0);
}

} // namespace

int main()
{
    JPH::RegisterDefaultAllocator();
    JobSystem::Initialize(4);
    TestDependenciesAndBarrier();
    TestDynamicBarrierAndException();
    TestConcurrencyAndCancellation();
    TestShutdownRejectsNewJobs();
    JobSystem::Shutdown();
    return 0;
}
