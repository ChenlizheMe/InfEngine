/**
 * @file JobSystem.h
 * @brief Engine-wide worker pool and its scheduling primitives.
 *
 * The pool deliberately remains the only native general-purpose worker owner.
 * Domains, priorities and groups describe work submitted to that pool; they
 * never create a second executor. Existing Schedule/ScheduleBatch/ParallelFor
 * calls use the Default domain and Normal priority.
 */

#pragma once

#include <array>
#include <atomic>
#include <condition_variable>
#include <cstddef>
#include <cstdint>
#include <deque>
#include <exception>
#include <functional>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <unordered_map>
#include <vector>

namespace infernux
{

class JobCancelled final : public std::runtime_error
{
  public:
    using std::runtime_error::runtime_error;
};

/** Logical scheduling domain. A value outside the named values is valid for
 * project-specific domains and is still owned by the same JobSystem. */
enum class JobDomain : uint16_t
{
    Default = 0,
    Runtime = 1,
    Render = 2,
    Asset = 3,
    Physics = 4,
    IO = 5,
    User = 1024,
};

/** Lower values are preferred only after the aging/fairness policy is applied. */
enum class JobPriority : uint8_t
{
    Low = 0,
    Normal = 1,
    High = 2,
    Critical = 3,
};

struct JobProfilerCounters
{
    uint64_t submitted = 0;
    uint64_t started = 0;
    uint64_t completed = 0;
    uint64_t failed = 0;
    uint64_t cancelled = 0;
    uint64_t blocked = 0;
    uint64_t helped = 0;
    uint64_t running = 0;
    uint64_t queued = 0;
};

/**
 * @brief Opaque completion handle for one job or a batch.
 *
 * Handles are copyable views of shared completion state. A fence returned by
 * TaskGroup::Fence additionally waits for the group to be closed.
 */
class JobHandle
{
  public:
    JobHandle() = default;
    JobHandle(const JobHandle &) = default;
    JobHandle &operator=(const JobHandle &) = default;
    JobHandle(JobHandle &&) noexcept = default;
    JobHandle &operator=(JobHandle &&) noexcept = default;

    [[nodiscard]] bool IsValid() const noexcept
    {
        return static_cast<bool>(m_state);
    }

    [[nodiscard]] bool IsComplete() const noexcept;

    bool Cancel() noexcept;

    [[nodiscard]] bool IsCancellationRequested() const noexcept
    {
        return m_state && m_state->cancelRequested.load(std::memory_order_acquire);
    }

  public:
    friend class JobSystem;
    friend class TaskGroup;

    struct State
    {
        explicit State(uint32_t count = 0, bool groupState = false)
            : remaining(count), closed(!groupState)
        {
        }

        std::atomic<uint32_t> remaining{0};
        std::atomic<bool> cancelRequested{false};
        std::atomic<bool> closed{true};
        mutable std::mutex completionMutex;
        std::condition_variable completionCv;
        std::exception_ptr failure;
    };

  private:

    explicit JobHandle(std::shared_ptr<State> state, bool waitsForClose = false)
        : m_state(std::move(state)), m_waitsForClose(waitsForClose)
    {
    }

    std::shared_ptr<State> m_state;
    bool m_waitsForClose = false;
};

using JobFence = JobHandle;

/** A dynamic set of jobs sharing cancellation, error and domain semantics. */
class TaskGroup
{
  public:
    explicit TaskGroup(JobDomain domain = JobDomain::Default,
                       JobPriority priority = JobPriority::Normal);
    ~TaskGroup();

    TaskGroup(const TaskGroup &) = delete;
    TaskGroup &operator=(const TaskGroup &) = delete;
    TaskGroup(TaskGroup &&other) noexcept;
    TaskGroup &operator=(TaskGroup &&other) noexcept;

    /** Prevent further submissions. Existing work is still drained. */
    void Close() noexcept;
    [[nodiscard]] bool IsClosed() const noexcept;
    [[nodiscard]] bool IsComplete() const noexcept;
    [[nodiscard]] bool IsCancellationRequested() const noexcept;
    bool Cancel() noexcept;

    /** Returns a fence that completes only after Close() and all jobs. */
    [[nodiscard]] JobFence Fence() const;
    [[nodiscard]] JobDomain GetDomain() const noexcept
    {
        return m_domain;
    }
    [[nodiscard]] JobPriority GetPriority() const noexcept
    {
        return m_priority;
    }

  private:
    friend class JobSystem;

    std::shared_ptr<JobHandle::State> m_state;
    mutable std::mutex m_mutex;
    JobDomain m_domain = JobDomain::Default;
    JobPriority m_priority = JobPriority::Normal;
};

/** Engine-wide worker pool singleton. */
class JobSystem
{
  public:
    using JobFn = std::function<void()>;

    enum class State : uint8_t
    {
        Running,
        Draining,
        Stopped,
    };

    static void Initialize(uint32_t workerCount = 0);
    static void Shutdown();
    [[nodiscard]] static JobSystem &Get();
    [[nodiscard]] static bool IsAvailable() noexcept;

    JobSystem(const JobSystem &) = delete;
    JobSystem &operator=(const JobSystem &) = delete;
    JobSystem(JobSystem &&) = delete;
    JobSystem &operator=(JobSystem &&) = delete;

    /** Existing callers retain Default/Normal behavior. */
    JobHandle Schedule(JobFn job,
                       JobDomain domain = JobDomain::Default,
                       JobPriority priority = JobPriority::Normal);

    JobHandle Schedule(TaskGroup &group, JobFn job);
    JobHandle Schedule(TaskGroup &group, JobFn job, JobPriority priority);

    JobHandle ScheduleBatch(uint32_t count,
                            std::function<JobFn(uint32_t index)> factory,
                            JobDomain domain = JobDomain::Default,
                            JobPriority priority = JobPriority::Normal);
    JobHandle ScheduleBatch(TaskGroup &group,
                            uint32_t count,
                            std::function<JobFn(uint32_t index)> factory);
    JobHandle ScheduleBatch(TaskGroup &group,
                            uint32_t count,
                            std::function<JobFn(uint32_t index)> factory,
                            JobPriority priority);

    void ParallelFor(uint32_t count,
                     std::function<void(uint32_t index)> body,
                     JobDomain domain = JobDomain::Default,
                     JobPriority priority = JobPriority::Normal);
    void ParallelFor(TaskGroup &group,
                     uint32_t count,
                     std::function<void(uint32_t index)> body);

    TaskGroup CreateTaskGroup(JobDomain domain = JobDomain::Default,
                              JobPriority priority = JobPriority::Normal) const;

    /** Wait helps work from the target group/domain while preserving permits. */
    void Wait(const JobHandle &handle);
    void Wait(TaskGroup &group);

    /** Wait without executing work; a worker temporarily releases its permit. */
    void WaitPassive(const JobHandle &handle);
    void WaitPassive(TaskGroup &group);

    /** 0 means unlimited. Changes affect tasks not yet dequeued. */
    void SetDomainConcurrency(JobDomain domain, uint32_t maxConcurrency);
    [[nodiscard]] uint32_t GetDomainConcurrency(JobDomain domain) const;
    [[nodiscard]] uint32_t GetDomainActiveCount(JobDomain domain) const;

    [[nodiscard]] JobProfilerCounters GetProfilerCounters() const;
    [[nodiscard]] JobProfilerCounters GetProfilerCounters(JobDomain domain) const;
    void ResetProfilerCounters();

    [[nodiscard]] uint32_t GetWorkerCount() const noexcept
    {
        return static_cast<uint32_t>(m_workers.size());
    }
    [[nodiscard]] State GetState() const noexcept
    {
        return m_state.load(std::memory_order_acquire);
    }
    [[nodiscard]] size_t GetQueuedTaskCount() const;
    [[nodiscard]] uint32_t GetActiveTaskCount() const noexcept
    {
        return m_activeTasks.load(std::memory_order_acquire);
    }

  private:
    JobSystem() = default;
    ~JobSystem();

    struct ProfilerState
    {
        std::atomic<uint64_t> submitted{0};
        std::atomic<uint64_t> started{0};
        std::atomic<uint64_t> completed{0};
        std::atomic<uint64_t> failed{0};
        std::atomic<uint64_t> cancelled{0};
        std::atomic<uint64_t> blocked{0};
        std::atomic<uint64_t> helped{0};
        std::atomic<uint64_t> running{0};
        std::atomic<uint64_t> queued{0};
    };

    struct Task
    {
        JobFn fn;
        std::shared_ptr<JobHandle::State> state;
        std::shared_ptr<JobHandle::State> groupState;
        std::shared_ptr<ProfilerState> profiler;
        JobDomain domain = JobDomain::Default;
        JobPriority priority = JobPriority::Normal;
        uint64_t sequence = 0;
    };

    struct ExecutionContext
    {
        JobSystem *system = nullptr;
        JobDomain domain = JobDomain::Default;
        std::shared_ptr<JobHandle::State> state;
        std::shared_ptr<JobHandle::State> groupState;
        bool ownsPermit = false;
    };

    static thread_local ExecutionContext *s_currentContext;

    void WorkerLoop();
    bool TryRunOne();
    bool TryRunOneForWait(const JobHandle &handle);
    void Execute(Task task, bool helped) noexcept;
    void StopAndJoin() noexcept;

    JobHandle ScheduleInternal(JobFn job,
                               JobDomain domain,
                               JobPriority priority,
                               std::shared_ptr<JobHandle::State> groupState);
    JobHandle ScheduleBatchInternal(uint32_t count,
                                    std::function<JobFn(uint32_t index)> factory,
                                    JobDomain domain,
                                    JobPriority priority,
                                    std::shared_ptr<JobHandle::State> groupState,
                                    std::mutex *groupMutex);

    bool DequeueTask(Task &task, const std::function<bool(const Task &)> &allowed);
    bool CanRunDomainLocked(JobDomain domain) const;
    void AcquirePermitLocked(JobDomain domain);
    void ReleasePermit(JobDomain domain) noexcept;
    bool SuspendCurrentPermit();
    void ResumeCurrentPermit();
    [[nodiscard]] bool WaitSatisfied(const JobHandle &handle) const;
    void PropagateFailureAndCancellation(const JobHandle &handle);

    std::shared_ptr<ProfilerState> ProfilerForDomain(JobDomain domain);
    static JobProfilerCounters Snapshot(const std::shared_ptr<ProfilerState> &state);
    static void RecordFailure(const std::shared_ptr<JobHandle::State> &state,
                              std::exception_ptr failure) noexcept;
    static void CompleteState(const std::shared_ptr<JobHandle::State> &state) noexcept;

    std::vector<std::thread> m_workers;
    std::array<std::deque<Task>, 4> m_queues;
    mutable std::mutex m_queueMutex;
    std::condition_variable m_queueCv;
    std::atomic<State> m_state{State::Stopped};
    std::atomic<uint32_t> m_activeTasks{0};
    bool m_accepting = true;
    bool m_stopRequested = false;
    uint64_t m_nextSequence = 0;
    std::unordered_map<uint16_t, uint32_t> m_domainLimits;
    std::unordered_map<uint16_t, uint32_t> m_domainActive;

    std::shared_ptr<ProfilerState> m_globalProfiler = std::make_shared<ProfilerState>();
    mutable std::mutex m_profilerMutex;
    std::unordered_map<uint16_t, std::shared_ptr<ProfilerState>> m_domainProfilers;
};

} // namespace infernux
