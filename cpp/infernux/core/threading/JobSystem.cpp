/**
 * @file JobSystem.cpp
 * @brief Implementation of the engine-wide worker pool.
 */

#include "JobSystem.h"

#include <algorithm>
#include <cassert>
#include <chrono>
#include <limits>

namespace infernux
{

namespace
{

std::atomic<JobSystem *> g_instance{nullptr};
std::mutex g_singletonMutex;

uint32_t ResolveWorkerCount(uint32_t requested) noexcept
{
    if (requested != 0) {
        return std::clamp<uint32_t>(requested, 1u, 32u);
    }
    const auto detected = std::thread::hardware_concurrency();
    if (detected <= 1) {
        return 1u;
    }
    return std::clamp<uint32_t>(detected - 1, 1u, 32u);
}

uint16_t DomainKey(JobDomain domain) noexcept
{
    return static_cast<uint16_t>(domain);
}

size_t PriorityIndex(JobPriority priority) noexcept
{
    return std::min<size_t>(static_cast<size_t>(priority), 3u);
}

uint64_t AtomicLoad(const std::atomic<uint64_t> &value) noexcept
{
    return value.load(std::memory_order_relaxed);
}

void NotifyState(const std::shared_ptr<JobHandle::State> &state) noexcept
{
    if (!state) {
        return;
    }
    std::lock_guard<std::mutex> lock(state->completionMutex);
    state->completionCv.notify_all();
}

} // namespace

thread_local JobSystem::ExecutionContext *JobSystem::s_currentContext = nullptr;

bool JobHandle::IsComplete() const noexcept
{
    if (!m_state) {
        return true;
    }
    if (m_state->remaining.load(std::memory_order_acquire) != 0) {
        return false;
    }
    return !m_waitsForClose || m_state->closed.load(std::memory_order_acquire);
}

bool JobHandle::Cancel() noexcept
{
    if (!m_state || IsComplete()) {
        return false;
    }
    m_state->cancelRequested.store(true, std::memory_order_release);
    NotifyState(m_state);
    return true;
}

TaskGroup::TaskGroup(JobDomain domain, JobPriority priority)
    : m_state(std::make_shared<JobHandle::State>(0, true)), m_domain(domain), m_priority(priority)
{
}

TaskGroup::~TaskGroup()
{
    Close();
}

TaskGroup::TaskGroup(TaskGroup &&other) noexcept
{
    std::lock_guard<std::mutex> lock(other.m_mutex);
    m_state = std::move(other.m_state);
    m_domain = other.m_domain;
    m_priority = other.m_priority;
}

TaskGroup &TaskGroup::operator=(TaskGroup &&other) noexcept
{
    if (this == &other) {
        return *this;
    }
    std::scoped_lock lock(m_mutex, other.m_mutex);
    if (m_state) {
        m_state->closed.store(true, std::memory_order_release);
        NotifyState(m_state);
    }
    m_state = std::move(other.m_state);
    m_domain = other.m_domain;
    m_priority = other.m_priority;
    return *this;
}

void TaskGroup::Close() noexcept
{
    if (!m_state) {
        return;
    }
    std::lock_guard<std::mutex> lock(m_mutex);
    m_state->closed.store(true, std::memory_order_release);
    NotifyState(m_state);
}

bool TaskGroup::IsClosed() const noexcept
{
    return !m_state || m_state->closed.load(std::memory_order_acquire);
}

bool TaskGroup::IsComplete() const noexcept
{
    return !m_state || (IsClosed() && m_state->remaining.load(std::memory_order_acquire) == 0);
}

bool TaskGroup::IsCancellationRequested() const noexcept
{
    return m_state && m_state->cancelRequested.load(std::memory_order_acquire);
}

bool TaskGroup::Cancel() noexcept
{
    if (!m_state || IsComplete()) {
        return false;
    }
    m_state->cancelRequested.store(true, std::memory_order_release);
    NotifyState(m_state);
    return true;
}

JobFence TaskGroup::Fence() const
{
    return JobFence(m_state, true);
}

void JobSystem::Initialize(uint32_t workerCount)
{
    std::lock_guard<std::mutex> guard(g_singletonMutex);
    if (g_instance.load(std::memory_order_acquire) != nullptr) {
        throw std::logic_error("JobSystem::Initialize called twice");
    }

    auto *instance = new JobSystem();
    instance->m_state.store(State::Running, std::memory_order_release);
    g_instance.store(instance, std::memory_order_release);

    const uint32_t resolved = ResolveWorkerCount(workerCount);
    instance->m_workers.reserve(resolved);
    for (uint32_t i = 0; i < resolved; ++i) {
        instance->m_workers.emplace_back([instance] { instance->WorkerLoop(); });
    }
}

void JobSystem::InitializeInline()
{
    std::lock_guard<std::mutex> guard(g_singletonMutex);
    if (g_instance.load(std::memory_order_acquire) != nullptr) {
        throw std::logic_error("JobSystem::InitializeInline called twice");
    }

    auto *instance = new JobSystem();
    instance->m_inline = true;
    instance->m_state.store(State::Running, std::memory_order_release);
    g_instance.store(instance, std::memory_order_release);
}

void JobSystem::Shutdown()
{
    JobSystem *toDestroy = nullptr;
    {
        std::lock_guard<std::mutex> guard(g_singletonMutex);
        toDestroy = g_instance.exchange(nullptr, std::memory_order_acq_rel);
    }

    if (toDestroy == nullptr) {
        return;
    }

    toDestroy->StopAndJoin();
    delete toDestroy;
}

JobSystem &JobSystem::Get()
{
    auto *instance = g_instance.load(std::memory_order_acquire);
    if (instance == nullptr) {
        throw std::logic_error("JobSystem::Get called outside its lifetime");
    }
    return *instance;
}

bool JobSystem::IsAvailable() noexcept
{
    return g_instance.load(std::memory_order_acquire) != nullptr;
}

JobSystem::~JobSystem()
{
    StopAndJoin();
}

TaskGroup JobSystem::CreateTaskGroup(JobDomain domain, JobPriority priority) const
{
    return TaskGroup(domain, priority);
}

std::shared_ptr<JobSystem::ProfilerState> JobSystem::ProfilerForDomain(JobDomain domain)
{
    const uint16_t key = DomainKey(domain);
    std::lock_guard<std::mutex> lock(m_profilerMutex);
    auto &entry = m_domainProfilers[key];
    if (!entry) {
        entry = std::make_shared<ProfilerState>();
    }
    return entry;
}

JobHandle JobSystem::Schedule(JobFn job, JobDomain domain, JobPriority priority)
{
    return ScheduleInternal(std::move(job), domain, priority, nullptr);
}

JobHandle JobSystem::Schedule(TaskGroup &group, JobFn job)
{
    return Schedule(group, std::move(job), group.GetPriority());
}

JobHandle JobSystem::Schedule(TaskGroup &group, JobFn job, JobPriority priority)
{
    if (!job) {
        throw std::invalid_argument("JobSystem::Schedule requires a callable");
    }
    std::lock_guard<std::mutex> groupLock(group.m_mutex);
    if (!group.m_state || group.m_state->closed.load(std::memory_order_acquire)) {
        throw std::logic_error("JobSystem::Schedule cannot add work to a closed TaskGroup");
    }
    group.m_state->remaining.fetch_add(1, std::memory_order_acq_rel);
    try {
        return ScheduleInternal(std::move(job), group.m_domain, priority, group.m_state);
    } catch (...) {
        CompleteState(group.m_state);
        throw;
    }
}

JobHandle JobSystem::ScheduleInternal(JobFn job, JobDomain domain, JobPriority priority,
                                      std::shared_ptr<JobHandle::State> groupState)
{
    if (!job) {
        throw std::invalid_argument("JobSystem::Schedule requires a callable");
    }

    auto state = std::make_shared<JobHandle::State>(1);
    auto profiler = ProfilerForDomain(domain);
    Task task{std::move(job), std::move(state), std::move(groupState), std::move(profiler), domain, priority, 0};
    auto handleState = task.state;
    auto taskProfiler = task.profiler;

    {
        std::lock_guard<std::mutex> guard(m_queueMutex);
        if (!m_accepting) {
            throw std::runtime_error("JobSystem is shutting down");
        }
        task.sequence = m_nextSequence++;
        m_globalProfiler->submitted.fetch_add(1, std::memory_order_relaxed);
        m_globalProfiler->queued.fetch_add(1, std::memory_order_relaxed);
        taskProfiler->submitted.fetch_add(1, std::memory_order_relaxed);
        taskProfiler->queued.fetch_add(1, std::memory_order_relaxed);
        try {
            m_queues[PriorityIndex(task.priority)].push_back(std::move(task));
        } catch (...) {
            m_globalProfiler->submitted.fetch_sub(1, std::memory_order_relaxed);
            m_globalProfiler->queued.fetch_sub(1, std::memory_order_relaxed);
            taskProfiler->submitted.fetch_sub(1, std::memory_order_relaxed);
            taskProfiler->queued.fetch_sub(1, std::memory_order_relaxed);
            throw;
        }
    }

    m_queueCv.notify_one();
    return JobHandle(std::move(handleState));
}

JobHandle JobSystem::ScheduleBatch(uint32_t count, std::function<JobFn(uint32_t index)> factory, JobDomain domain,
                                   JobPriority priority)
{
    return ScheduleBatchInternal(count, std::move(factory), domain, priority, nullptr, nullptr);
}

JobHandle JobSystem::ScheduleBatch(TaskGroup &group, uint32_t count, std::function<JobFn(uint32_t index)> factory)
{
    return ScheduleBatch(group, count, std::move(factory), group.GetPriority());
}

JobHandle JobSystem::ScheduleBatch(TaskGroup &group, uint32_t count, std::function<JobFn(uint32_t index)> factory,
                                   JobPriority priority)
{
    return ScheduleBatchInternal(count, std::move(factory), group.GetDomain(), priority, group.m_state, &group.m_mutex);
}

JobHandle JobSystem::ScheduleBatchInternal(uint32_t count, std::function<JobFn(uint32_t index)> factory,
                                           JobDomain domain, JobPriority priority,
                                           std::shared_ptr<JobHandle::State> groupState, std::mutex *groupMutex)
{
    if (count == 0) {
        return JobHandle();
    }
    if (!factory) {
        throw std::invalid_argument("JobSystem::ScheduleBatch requires a factory");
    }

    std::vector<JobFn> jobs;
    jobs.reserve(count);
    for (uint32_t i = 0; i < count; ++i) {
        auto job = factory(i);
        if (!job) {
            throw std::invalid_argument("JobSystem::ScheduleBatch factory returned an empty job");
        }
        jobs.push_back(std::move(job));
    }

    std::unique_lock<std::mutex> groupLock;
    if (groupMutex != nullptr) {
        groupLock = std::unique_lock<std::mutex>(*groupMutex);
        if (!groupState || groupState->closed.load(std::memory_order_acquire)) {
            throw std::logic_error("JobSystem::ScheduleBatch cannot add work to a closed TaskGroup");
        }
    }

    auto state = std::make_shared<JobHandle::State>(count);
    auto profiler = ProfilerForDomain(domain);
    if (groupState) {
        groupState->remaining.fetch_add(count, std::memory_order_acq_rel);
    }
    size_t pushed = 0;
    try {
        std::lock_guard<std::mutex> guard(m_queueMutex);
        if (!m_accepting) {
            throw std::runtime_error("JobSystem is shutting down");
        }
        auto &queue = m_queues[PriorityIndex(priority)];
        m_globalProfiler->submitted.fetch_add(count, std::memory_order_relaxed);
        m_globalProfiler->queued.fetch_add(count, std::memory_order_relaxed);
        profiler->submitted.fetch_add(count, std::memory_order_relaxed);
        profiler->queued.fetch_add(count, std::memory_order_relaxed);
        for (auto &job : jobs) {
            try {
                queue.push_back(Task{std::move(job), state, groupState, profiler, domain, priority, m_nextSequence++});
                ++pushed;
            } catch (...) {
                m_globalProfiler->submitted.fetch_sub(count, std::memory_order_relaxed);
                m_globalProfiler->queued.fetch_sub(count, std::memory_order_relaxed);
                profiler->submitted.fetch_sub(count, std::memory_order_relaxed);
                profiler->queued.fetch_sub(count, std::memory_order_relaxed);
                throw;
            }
        }
    } catch (...) {
        if (pushed != 0) {
            std::lock_guard<std::mutex> guard(m_queueMutex);
            auto &queue = m_queues[PriorityIndex(priority)];
            while (pushed-- != 0 && !queue.empty()) {
                queue.pop_back();
            }
        }
        if (groupState) {
            for (uint32_t i = 0; i < count; ++i) {
                CompleteState(groupState);
            }
        }
        throw;
    }

    m_queueCv.notify_all();
    return JobHandle(std::move(state));
}

void JobSystem::ParallelFor(uint32_t count, std::function<void(uint32_t index)> body, JobDomain domain,
                            JobPriority priority)
{
    if (count == 0) {
        return;
    }
    if (!body) {
        throw std::invalid_argument("JobSystem::ParallelFor requires a callable");
    }
    auto handle =
        ScheduleBatch(count, [body](uint32_t i) -> JobFn { return [body, i] { body(i); }; }, domain, priority);
    Wait(handle);
}

void JobSystem::ParallelFor(TaskGroup &group, uint32_t count, std::function<void(uint32_t index)> body)
{
    if (count == 0) {
        return;
    }
    if (!body) {
        throw std::invalid_argument("JobSystem::ParallelFor requires a callable");
    }
    auto handle = ScheduleBatch(group, count, [body](uint32_t i) -> JobFn { return [body, i] { body(i); }; });
    Wait(handle);
}

bool JobSystem::CanRunDomainLocked(JobDomain domain) const
{
    const auto limitIt = m_domainLimits.find(DomainKey(domain));
    if (limitIt == m_domainLimits.end() || limitIt->second == 0) {
        return true;
    }
    const auto activeIt = m_domainActive.find(DomainKey(domain));
    return activeIt == m_domainActive.end() || activeIt->second < limitIt->second;
}

void JobSystem::AcquirePermitLocked(JobDomain domain)
{
    ++m_domainActive[DomainKey(domain)];
}

void JobSystem::ReleasePermit(JobDomain domain) noexcept
{
    {
        std::lock_guard<std::mutex> lock(m_queueMutex);
        auto it = m_domainActive.find(DomainKey(domain));
        if (it != m_domainActive.end()) {
            if (it->second > 1) {
                --it->second;
            } else {
                m_domainActive.erase(it);
            }
        }
    }
    m_queueCv.notify_all();
}

bool JobSystem::DequeueTask(Task &task, const std::function<bool(const Task &)> &allowed)
{
    std::lock_guard<std::mutex> guard(m_queueMutex);

    bool hasQueued = false;
    bool hasDomainBlocked = false;
    std::shared_ptr<ProfilerState> blockedProfiler;
    int bestScore = std::numeric_limits<int>::min();
    uint64_t bestSequence = std::numeric_limits<uint64_t>::max();
    size_t bestQueue = 0;
    size_t bestIndex = 0;
    bool found = false;
    const bool fastPath = !allowed && m_domainLimits.empty();

    for (size_t queueIndex = 0; queueIndex < m_queues.size(); ++queueIndex) {
        const auto &queue = m_queues[queueIndex];
        hasQueued = hasQueued || !queue.empty();
        const size_t candidateCount = fastPath ? std::min<size_t>(queue.size(), 1u) : queue.size();
        for (size_t index = 0; index < candidateCount; ++index) {
            const Task &candidate = queue[index];
            if (allowed && !allowed(candidate)) {
                continue;
            }
            if (!CanRunDomainLocked(candidate.domain)) {
                hasDomainBlocked = true;
                if (!blockedProfiler) {
                    blockedProfiler = candidate.profiler;
                }
                continue;
            }

            const uint64_t age = m_nextSequence > candidate.sequence ? m_nextSequence - candidate.sequence : 0;
            const int basePriority = static_cast<int>(candidate.priority);
            const int effectivePriority = std::min(3, basePriority + static_cast<int>(age / 32u));
            if (!found || effectivePriority > bestScore ||
                (effectivePriority == bestScore && candidate.sequence < bestSequence)) {
                found = true;
                bestScore = effectivePriority;
                bestSequence = candidate.sequence;
                bestQueue = queueIndex;
                bestIndex = index;
            }
        }
    }

    if (!found) {
        if (hasDomainBlocked) {
            m_globalProfiler->blocked.fetch_add(1, std::memory_order_relaxed);
            if (blockedProfiler) {
                blockedProfiler->blocked.fetch_add(1, std::memory_order_relaxed);
            }
        }
        return false;
    }

    auto &queue = m_queues[bestQueue];
    task = std::move(queue[bestIndex]);
    queue.erase(queue.begin() + static_cast<std::ptrdiff_t>(bestIndex));
    AcquirePermitLocked(task.domain);
    task.profiler->queued.fetch_sub(1, std::memory_order_relaxed);
    m_globalProfiler->queued.fetch_sub(1, std::memory_order_relaxed);
    return true;
}

bool JobSystem::TryRunOne()
{
    Task task;
    if (!DequeueTask(task, {})) {
        return false;
    }
    Execute(std::move(task), false);
    return true;
}

bool JobSystem::TryRunOneForWait(const JobHandle &handle)
{
    Task task;
    auto allowed = [this, &handle](const Task &candidate) {
        auto *context = s_currentContext;
        if (context == nullptr || context->system != this) {
            return true;
        }
        if (candidate.state == handle.m_state || candidate.groupState == handle.m_state) {
            return true;
        }
        if (context->groupState &&
            (candidate.state == context->groupState || candidate.groupState == context->groupState)) {
            return true;
        }
        return candidate.domain == context->domain;
    };
    if (!DequeueTask(task, allowed)) {
        return false;
    }
    Execute(std::move(task), true);
    return true;
}

bool JobSystem::SuspendCurrentPermit()
{
    auto *context = s_currentContext;
    if (context == nullptr || context->system != this || !context->ownsPermit) {
        return false;
    }
    {
        std::lock_guard<std::mutex> lock(m_queueMutex);
        auto it = m_domainActive.find(DomainKey(context->domain));
        if (it != m_domainActive.end()) {
            if (it->second > 1) {
                --it->second;
            } else {
                m_domainActive.erase(it);
            }
        }
        context->ownsPermit = false;
    }
    m_queueCv.notify_all();
    return true;
}

void JobSystem::ResumeCurrentPermit()
{
    auto *context = s_currentContext;
    if (context == nullptr || context->system != this || context->ownsPermit) {
        return;
    }
    std::unique_lock<std::mutex> lock(m_queueMutex);
    m_queueCv.wait(lock, [this, context] {
        return CanRunDomainLocked(context->domain) || m_state.load(std::memory_order_acquire) == State::Stopped;
    });
    if (m_state.load(std::memory_order_acquire) != State::Stopped) {
        AcquirePermitLocked(context->domain);
        context->ownsPermit = true;
    }
}

bool JobSystem::WaitSatisfied(const JobHandle &handle) const
{
    if (!handle.IsValid()) {
        return true;
    }
    const auto state = handle.m_state;
    if (state->remaining.load(std::memory_order_acquire) == 0 &&
        (!handle.m_waitsForClose || state->closed.load(std::memory_order_acquire))) {
        return true;
    }

    auto *context = s_currentContext;
    if (context != nullptr && context->system == this && (context->state == state || context->groupState == state)) {
        return (!handle.m_waitsForClose || state->closed.load(std::memory_order_acquire)) &&
               state->remaining.load(std::memory_order_acquire) <= 1;
    }
    return false;
}

void JobSystem::PropagateFailureAndCancellation(const JobHandle &handle)
{
    if (!handle.m_state) {
        return;
    }
    std::exception_ptr failure;
    {
        std::lock_guard<std::mutex> lock(handle.m_state->completionMutex);
        failure = handle.m_state->failure;
    }
    if (failure) {
        std::rethrow_exception(failure);
    }
    if (handle.m_state->cancelRequested.load(std::memory_order_acquire)) {
        throw JobCancelled("job group was cancelled");
    }
}

void JobSystem::Wait(const JobHandle &handle)
{
    if (!handle.IsValid()) {
        return;
    }

    const bool suspended = SuspendCurrentPermit();
    while (!WaitSatisfied(handle)) {
        if (!TryRunOneForWait(handle)) {
            std::unique_lock<std::mutex> lock(handle.m_state->completionMutex);
            handle.m_state->completionCv.wait_for(lock, std::chrono::milliseconds(1),
                                                  [this, &handle] { return WaitSatisfied(handle); });
        }
    }
    if (suspended) {
        ResumeCurrentPermit();
    }
    PropagateFailureAndCancellation(handle);
}

void JobSystem::Wait(TaskGroup &group)
{
    group.Close();
    Wait(group.Fence());
}

void JobSystem::WaitPassive(const JobHandle &handle)
{
    if (!handle.IsValid()) {
        return;
    }

    if (m_inline) {
        Wait(handle);
        return;
    }

    const bool suspended = SuspendCurrentPermit();
    while (!WaitSatisfied(handle)) {
        std::unique_lock<std::mutex> lock(handle.m_state->completionMutex);
        handle.m_state->completionCv.wait_for(lock, std::chrono::milliseconds(1),
                                              [this, &handle] { return WaitSatisfied(handle); });
    }
    if (suspended) {
        ResumeCurrentPermit();
    }
    PropagateFailureAndCancellation(handle);
}

void JobSystem::WaitPassive(TaskGroup &group)
{
    group.Close();
    WaitPassive(group.Fence());
}

void JobSystem::SetDomainConcurrency(JobDomain domain, uint32_t maxConcurrency)
{
    {
        std::lock_guard<std::mutex> lock(m_queueMutex);
        if (maxConcurrency == 0) {
            m_domainLimits.erase(DomainKey(domain));
        } else {
            m_domainLimits[DomainKey(domain)] = maxConcurrency;
        }
    }
    m_queueCv.notify_all();
}

uint32_t JobSystem::GetDomainConcurrency(JobDomain domain) const
{
    std::lock_guard<std::mutex> lock(m_queueMutex);
    const auto it = m_domainLimits.find(DomainKey(domain));
    return it == m_domainLimits.end() ? 0u : it->second;
}

uint32_t JobSystem::GetDomainActiveCount(JobDomain domain) const
{
    std::lock_guard<std::mutex> lock(m_queueMutex);
    const auto it = m_domainActive.find(DomainKey(domain));
    return it == m_domainActive.end() ? 0u : it->second;
}

JobProfilerCounters JobSystem::Snapshot(const std::shared_ptr<ProfilerState> &state)
{
    JobProfilerCounters result;
    if (!state) {
        return result;
    }
    result.submitted = AtomicLoad(state->submitted);
    result.started = AtomicLoad(state->started);
    result.completed = AtomicLoad(state->completed);
    result.failed = AtomicLoad(state->failed);
    result.cancelled = AtomicLoad(state->cancelled);
    result.blocked = AtomicLoad(state->blocked);
    result.helped = AtomicLoad(state->helped);
    result.running = AtomicLoad(state->running);
    result.queued = AtomicLoad(state->queued);
    return result;
}

JobProfilerCounters JobSystem::GetProfilerCounters() const
{
    return Snapshot(m_globalProfiler);
}

JobProfilerCounters JobSystem::GetProfilerCounters(JobDomain domain) const
{
    std::lock_guard<std::mutex> lock(m_profilerMutex);
    const auto it = m_domainProfilers.find(DomainKey(domain));
    return it == m_domainProfilers.end() ? JobProfilerCounters{} : Snapshot(it->second);
}

void JobSystem::ResetProfilerCounters()
{
    auto reset = [](const std::shared_ptr<ProfilerState> &state) {
        if (!state) {
            return;
        }
        state->submitted.store(0, std::memory_order_relaxed);
        state->started.store(0, std::memory_order_relaxed);
        state->completed.store(0, std::memory_order_relaxed);
        state->failed.store(0, std::memory_order_relaxed);
        state->cancelled.store(0, std::memory_order_relaxed);
        state->blocked.store(0, std::memory_order_relaxed);
        state->helped.store(0, std::memory_order_relaxed);
        state->running.store(0, std::memory_order_relaxed);
        state->queued.store(0, std::memory_order_relaxed);
    };
    reset(m_globalProfiler);
    std::lock_guard<std::mutex> lock(m_profilerMutex);
    for (const auto &entry : m_domainProfilers) {
        reset(entry.second);
    }
}

uint32_t JobSystem::RunPendingJobs(uint32_t maxJobs)
{
    uint32_t completed = 0;
    while ((maxJobs == 0 || completed < maxJobs) && TryRunOne())
        ++completed;
    return completed;
}

size_t JobSystem::GetQueuedTaskCount() const
{
    std::lock_guard<std::mutex> guard(m_queueMutex);
    size_t count = 0;
    for (const auto &queue : m_queues) {
        count += queue.size();
    }
    return count;
}

void JobSystem::WorkerLoop()
{
    for (;;) {
        Task task;
        {
            std::unique_lock<std::mutex> guard(m_queueMutex);
            m_queueCv.wait(guard, [this] {
                for (const auto &queue : m_queues) {
                    if (!queue.empty()) {
                        return true;
                    }
                }
                return m_stopRequested;
            });
            bool hasQueued = false;
            for (const auto &queue : m_queues) {
                hasQueued = hasQueued || !queue.empty();
            }
            if (m_stopRequested && !hasQueued) {
                return;
            }
        }
        if (!DequeueTask(task, {})) {
            std::unique_lock<std::mutex> idleLock(m_queueMutex);
            m_queueCv.wait_for(idleLock, std::chrono::milliseconds(1));
            continue;
        }
        Execute(std::move(task), false);
    }
}

void JobSystem::RecordFailure(const std::shared_ptr<JobHandle::State> &state, std::exception_ptr failure) noexcept
{
    if (!state || !failure) {
        return;
    }
    std::lock_guard<std::mutex> lock(state->completionMutex);
    if (!state->failure) {
        state->failure = std::move(failure);
    }
}

void JobSystem::CompleteState(const std::shared_ptr<JobHandle::State> &state) noexcept
{
    if (!state) {
        return;
    }
    if (state->remaining.fetch_sub(1, std::memory_order_acq_rel) == 1) {
        NotifyState(state);
    }
}

void JobSystem::Execute(Task task, bool helped) noexcept
{
    m_activeTasks.fetch_add(1, std::memory_order_acq_rel);
    m_globalProfiler->started.fetch_add(1, std::memory_order_relaxed);
    m_globalProfiler->running.fetch_add(1, std::memory_order_relaxed);
    task.profiler->started.fetch_add(1, std::memory_order_relaxed);
    task.profiler->running.fetch_add(1, std::memory_order_relaxed);
    if (helped) {
        m_globalProfiler->helped.fetch_add(1, std::memory_order_relaxed);
        task.profiler->helped.fetch_add(1, std::memory_order_relaxed);
    }

    ExecutionContext context{this, task.domain, task.state, task.groupState, true};
    ExecutionContext *previous = s_currentContext;
    s_currentContext = &context;

    const bool cancelled = task.state->cancelRequested.load(std::memory_order_acquire) ||
                           (task.groupState && task.groupState->cancelRequested.load(std::memory_order_acquire));
    if (cancelled) {
        task.state->cancelRequested.store(true, std::memory_order_release);
        m_globalProfiler->cancelled.fetch_add(1, std::memory_order_relaxed);
        task.profiler->cancelled.fetch_add(1, std::memory_order_relaxed);
    } else {
        try {
            task.fn();
        } catch (...) {
            const auto failure = std::current_exception();
            RecordFailure(task.state, failure);
            RecordFailure(task.groupState, failure);
            m_globalProfiler->failed.fetch_add(1, std::memory_order_relaxed);
            task.profiler->failed.fetch_add(1, std::memory_order_relaxed);
        }
    }

    if (context.ownsPermit) {
        context.ownsPermit = false;
        ReleasePermit(task.domain);
    }
    m_activeTasks.fetch_sub(1, std::memory_order_acq_rel);
    m_globalProfiler->running.fetch_sub(1, std::memory_order_relaxed);
    task.profiler->running.fetch_sub(1, std::memory_order_relaxed);
    m_globalProfiler->completed.fetch_add(1, std::memory_order_relaxed);
    task.profiler->completed.fetch_add(1, std::memory_order_relaxed);
    CompleteState(task.state);
    CompleteState(task.groupState);
    s_currentContext = previous;
}

void JobSystem::StopAndJoin() noexcept
{
    State expected = State::Running;
    if (!m_state.compare_exchange_strong(expected, State::Draining, std::memory_order_acq_rel)) {
        return;
    }
    {
        std::lock_guard<std::mutex> guard(m_queueMutex);
        m_accepting = false;
        m_stopRequested = true;
    }
    m_queueCv.notify_all();

    if (m_inline) {
        while (TryRunOne()) {
        }
    }

    for (auto &worker : m_workers) {
        if (worker.joinable()) {
            worker.join();
        }
    }
    m_workers.clear();
    m_state.store(State::Stopped, std::memory_order_release);
    m_queueCv.notify_all();
}

} // namespace infernux
