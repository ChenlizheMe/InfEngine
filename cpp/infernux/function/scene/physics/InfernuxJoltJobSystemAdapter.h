#pragma once

/**
 * @file InfernuxJoltJobSystemAdapter.h
 * @brief Adapts Jolt jobs to the engine-wide Infernux JobSystem.
 *
 * This module is deliberately the only place where the Jolt JobSystem API is
 * coupled to Infernux scheduling.  core/threading remains Jolt-independent.
 */

#include <Jolt/Core/JobSystem.h>

#include <core/threading/JobSystem.h>

#include <atomic>
#include <condition_variable>
#include <cstdint>
#include <memory>
#include <mutex>
#include <vector>

namespace infernux
{

class InfernuxJoltJobSystemAdapter final : public JPH::JobSystem
{
  public:
    InfernuxJoltJobSystemAdapter(uint32_t maxJobs, uint32_t maxBarriers, uint32_t maxConcurrency);
    ~InfernuxJoltJobSystemAdapter() override;

    InfernuxJoltJobSystemAdapter(const InfernuxJoltJobSystemAdapter &) = delete;
    InfernuxJoltJobSystemAdapter &operator=(const InfernuxJoltJobSystemAdapter &) = delete;

    int GetMaxConcurrency() const override;

    JPH::JobSystem::JobHandle CreateJob(const char *name, JPH::ColorArg color,
                                        const JPH::JobSystem::JobFunction &jobFunction,
                                        uint32_t numDependencies) override;

    JPH::JobSystem::Barrier *CreateBarrier() override;
    void DestroyBarrier(JPH::JobSystem::Barrier *barrier) override;
    void WaitForJobs(JPH::JobSystem::Barrier *barrier) override;

    /** Bind Jolt submissions made during one PhysicsWorld step to a group. */
    void BeginFrame(TaskGroup &group);
    void EndFrame();

    /** Stop accepting new work. Existing shared-pool work is still drained. */
    void Shutdown() noexcept;
    [[nodiscard]] bool IsAccepting() const noexcept
    {
        return m_accepting.load(std::memory_order_acquire);
    }

    /** Structural proof that this adapter owns no worker threads. */
    [[nodiscard]] uint32_t GetOwnedWorkerThreadCount() const noexcept
    {
        return 0;
    }

    [[nodiscard]] uint64_t GetCaughtExceptionCount() const noexcept
    {
        return m_caughtExceptions.load(std::memory_order_acquire);
    }

  protected:
    void QueueJob(JPH::JobSystem::Job *job) override;
    void QueueJobs(JPH::JobSystem::Job **jobs, uint32_t count) override;
    void FreeJob(JPH::JobSystem::Job *job) override;

  private:
    class AdapterJob;
    class BarrierImpl;

    void QueueOne(AdapterJob *job);
    void RecordCaughtException() noexcept;

    infernux::JobSystem *m_jobSystem = nullptr;
    uint32_t m_maxJobs = 0;
    uint32_t m_maxBarriers = 0;
    uint32_t m_maxConcurrency = 0;

    mutable std::mutex m_stateMutex;
    TaskGroup *m_activeFrameGroup = nullptr;
    std::vector<std::unique_ptr<BarrierImpl>> m_barriers;
    std::atomic<bool> m_accepting{true};
    std::atomic<uint32_t> m_liveJobs{0};
    std::atomic<uint64_t> m_caughtExceptions{0};
    std::condition_variable m_jobCapacityCv;
};

} // namespace infernux
