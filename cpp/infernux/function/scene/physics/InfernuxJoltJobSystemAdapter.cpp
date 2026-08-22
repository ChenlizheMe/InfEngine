/**
 * @file InfernuxJoltJobSystemAdapter.cpp
 * @brief Jolt JobSystem implementation backed by Infernux JobSystem.
 */

#include <Jolt/Jolt.h>
#include <Jolt/Core/Color.h>

#include "InfernuxJoltJobSystemAdapter.h"

#include <algorithm>
#include <chrono>
#include <condition_variable>
#include <exception>
#include <stdexcept>
#include <thread>
#include <unordered_set>

namespace infernux
{

class InfernuxJoltJobSystemAdapter::AdapterJob final : public JPH::JobSystem::Job
{
  public:
    AdapterJob(const char *name, JPH::ColorArg color, InfernuxJoltJobSystemAdapter *owner,
               const JPH::JobSystem::JobFunction &function, uint32_t dependencies)
        : JPH::JobSystem::Job(name, color, owner, function, dependencies), m_owner(owner)
    {
    }

    bool AttachBarrier(JPH::JobSystem::Barrier *barrier)
    {
        return SetBarrier(barrier);
    }

    void AddExternalReference()
    {
        AddRef();
    }

    void ReleaseExternalReference()
    {
        Release();
    }

    void Run()
    {
        Execute();
    }

    void PublishExecution(infernux::JobHandle handle)
    {
        {
            std::lock_guard<std::mutex> lock(m_executionMutex);
            m_execution = std::move(handle);
        }
        m_executionCv.notify_all();
    }

    infernux::JobHandle WaitForExecutionHandle()
    {
        std::lock_guard<std::mutex> lock(m_executionMutex);
        return m_execution;
    }

    void WaitUntilQueued()
    {
        std::unique_lock<std::mutex> lock(m_executionMutex);
        m_executionCv.wait_for(lock, std::chrono::milliseconds(1),
                               [this] { return m_execution.IsValid() || IsDone(); });
    }

    void SetBarrierOwner(BarrierImpl *barrier)
    {
        std::lock_guard<std::mutex> lock(m_executionMutex);
        m_barrierOwner = barrier;
    }

    BarrierImpl *GetBarrierOwner()
    {
        std::lock_guard<std::mutex> lock(m_executionMutex);
        return m_barrierOwner;
    }

    InfernuxJoltJobSystemAdapter *Owner() const noexcept
    {
        return m_owner;
    }

  private:
    InfernuxJoltJobSystemAdapter *m_owner = nullptr;
    mutable std::mutex m_executionMutex;
    std::condition_variable m_executionCv;
    infernux::JobHandle m_execution;
    BarrierImpl *m_barrierOwner = nullptr;
};

class InfernuxJoltJobSystemAdapter::BarrierImpl final : public JPH::JobSystem::Barrier
{
  public:
    explicit BarrierImpl(InfernuxJoltJobSystemAdapter *owner) : m_owner(owner)
    {
    }

    ~BarrierImpl() override
    {
        std::lock_guard<std::mutex> lock(m_mutex);
        for (AdapterJob *job : m_jobs) {
            job->ReleaseExternalReference();
        }
        m_jobs.clear();
    }

    void AddJob(const JPH::JobSystem::JobHandle &handle) override
    {
        auto *job = static_cast<AdapterJob *>(handle.GetPtr());
        if (job == nullptr || !job->AttachBarrier(this)) {
            return;
        }

        job->AddExternalReference();
        job->SetBarrierOwner(this);
        {
            std::lock_guard<std::mutex> lock(m_mutex);
            m_jobs.push_back(job);
            if (m_finished.erase(job) == 0) {
                ++m_pending;
            }
        }
        m_cv.notify_all();
    }

    void AddJobs(const JPH::JobSystem::JobHandle *handles, uint32_t count) override
    {
        for (uint32_t index = 0; index < count; ++index) {
            AddJob(handles[index]);
        }
    }

    void Wait()
    {
        for (;;) {
            std::vector<AdapterJob *> jobs;
            {
                std::lock_guard<std::mutex> lock(m_mutex);
                if (m_pending == 0) {
                    ReleaseCompletedLocked();
                    return;
                }
                jobs = m_jobs;
            }

            bool helped = false;
            for (AdapterJob *job : jobs) {
                if (job == nullptr || job->IsDone()) {
                    continue;
                }
                const infernux::JobHandle execution = job->WaitForExecutionHandle();
                if (execution.IsValid()) {
                    try {
                        m_owner->m_jobSystem->Wait(execution);
                    } catch (const JobCancelled &) {
                        // Jolt has no cancelled-job terminal state: a shared
                        // task cancelled before its wrapper ran must still
                        // execute the guarded Jolt job so its dependency and
                        // barrier state reaches cDoneState.
                        if (!job->IsDone()) {
                            job->Run();
                        }
                    }
                    helped = true;
                } else {
                    job->WaitUntilQueued();
                }
            }

            {
                std::unique_lock<std::mutex> lock(m_mutex);
                ReleaseCompletedLocked();
                if (m_pending != 0 && !helped) {
                    m_cv.wait_for(lock, std::chrono::milliseconds(1));
                }
            }
        }
    }

  protected:
    void OnJobFinished(JPH::JobSystem::Job *job) override
    {
        auto *adapterJob = static_cast<AdapterJob *>(job);
        {
            std::lock_guard<std::mutex> lock(m_mutex);
            auto it = std::find(m_jobs.begin(), m_jobs.end(), adapterJob);
            if (it != m_jobs.end() && m_pending > 0) {
                --m_pending;
                m_finished.insert(adapterJob);
            } else if (it == m_jobs.end()) {
                m_finished.insert(adapterJob);
            }
        }
        m_cv.notify_all();
    }

  private:
    void ReleaseCompletedLocked()
    {
        auto it = m_jobs.begin();
        while (it != m_jobs.end()) {
            AdapterJob *job = *it;
            if (job != nullptr && m_finished.erase(job) != 0) {
                job->ReleaseExternalReference();
                it = m_jobs.erase(it);
            } else {
                ++it;
            }
        }
    }

    InfernuxJoltJobSystemAdapter *m_owner = nullptr;
    std::mutex m_mutex;
    std::condition_variable m_cv;
    std::vector<AdapterJob *> m_jobs;
    std::unordered_set<AdapterJob *> m_finished;
    uint32_t m_pending = 0;
};

InfernuxJoltJobSystemAdapter::InfernuxJoltJobSystemAdapter(uint32_t maxJobs, uint32_t maxBarriers,
                                                           uint32_t maxConcurrency)
    : m_jobSystem(&infernux::JobSystem::Get()), m_maxJobs(maxJobs), m_maxBarriers(maxBarriers),
      m_maxConcurrency(maxConcurrency)
{
    if (!infernux::JobSystem::IsAvailable()) {
        throw std::logic_error("InfernuxJoltJobSystemAdapter requires an initialized Infernux JobSystem");
    }
    if (maxJobs == 0 || maxBarriers == 0) {
        throw std::invalid_argument("InfernuxJoltJobSystemAdapter limits must be non-zero");
    }
}

InfernuxJoltJobSystemAdapter::~InfernuxJoltJobSystemAdapter()
{
    Shutdown();
}

int InfernuxJoltJobSystemAdapter::GetMaxConcurrency() const
{
    const uint32_t domainLimit = m_jobSystem->GetDomainConcurrency(JobDomain::Physics);
    if (domainLimit != 0) {
        return static_cast<int>(domainLimit);
    }
    if (m_maxConcurrency != 0) {
        return static_cast<int>(m_maxConcurrency);
    }
    return static_cast<int>(std::max<uint32_t>(1u, m_jobSystem->GetWorkerCount() + 1u));
}

JPH::JobSystem::JobHandle InfernuxJoltJobSystemAdapter::CreateJob(const char *name, JPH::ColorArg color,
                                                                  const JPH::JobSystem::JobFunction &jobFunction,
                                                                  uint32_t numDependencies)
{
    std::unique_lock<std::mutex> capacityLock(m_stateMutex);
    m_jobCapacityCv.wait(capacityLock, [this] {
        return m_liveJobs.load(std::memory_order_acquire) < m_maxJobs || !m_accepting.load(std::memory_order_acquire);
    });
    if (!m_accepting.load(std::memory_order_acquire)) {
        throw std::runtime_error("Jolt job submission rejected after adapter shutdown");
    }
    m_liveJobs.fetch_add(1, std::memory_order_acq_rel);
    capacityLock.unlock();
    if (!jobFunction) {
        m_liveJobs.fetch_sub(1, std::memory_order_acq_rel);
        m_jobCapacityCv.notify_one();
        throw std::invalid_argument("Jolt job requires a callable");
    }

    const JPH::JobSystem::JobFunction guardedFunction = [this, jobFunction] {
        try {
            jobFunction();
        } catch (...) {
            RecordCaughtException();
        }
    };
    AdapterJob *job = nullptr;
    try {
        job = new AdapterJob(name, color, this, guardedFunction, numDependencies);
    } catch (...) {
        m_liveJobs.fetch_sub(1, std::memory_order_acq_rel);
        m_jobCapacityCv.notify_one();
        throw;
    }
    JPH::JobSystem::JobHandle handle(job);
    if (numDependencies == 0) {
        QueueJob(job);
    }
    return handle;
}

JPH::JobSystem::Barrier *InfernuxJoltJobSystemAdapter::CreateBarrier()
{
    std::lock_guard<std::mutex> lock(m_stateMutex);
    if (!m_accepting.load(std::memory_order_acquire) || m_barriers.size() >= m_maxBarriers) {
        return nullptr;
    }
    auto barrier = std::make_unique<BarrierImpl>(this);
    BarrierImpl *result = barrier.get();
    m_barriers.push_back(std::move(barrier));
    return result;
}

void InfernuxJoltJobSystemAdapter::DestroyBarrier(JPH::JobSystem::Barrier *barrier)
{
    if (barrier == nullptr) {
        return;
    }
    auto *target = static_cast<BarrierImpl *>(barrier);
    target->Wait();
    std::lock_guard<std::mutex> lock(m_stateMutex);
    auto it = std::find_if(m_barriers.begin(), m_barriers.end(),
                           [target](const auto &entry) { return entry.get() == target; });
    if (it != m_barriers.end()) {
        m_barriers.erase(it);
    }
}

void InfernuxJoltJobSystemAdapter::WaitForJobs(JPH::JobSystem::Barrier *barrier)
{
    if (barrier == nullptr) {
        return;
    }
    static_cast<BarrierImpl *>(barrier)->Wait();
}

void InfernuxJoltJobSystemAdapter::BeginFrame(TaskGroup &group)
{
    if (group.GetDomain() != JobDomain::Physics || group.IsClosed() || group.IsCancellationRequested()) {
        throw std::invalid_argument("Jolt adapter requires a Physics job group");
    }
    std::lock_guard<std::mutex> lock(m_stateMutex);
    if (!m_accepting.load(std::memory_order_acquire) || m_activeFrameGroup != nullptr) {
        throw std::logic_error("Jolt adapter frame group is unavailable");
    }
    m_activeFrameGroup = &group;
}

void InfernuxJoltJobSystemAdapter::EndFrame()
{
    std::lock_guard<std::mutex> lock(m_stateMutex);
    m_activeFrameGroup = nullptr;
}

void InfernuxJoltJobSystemAdapter::Shutdown() noexcept
{
    m_accepting.store(false, std::memory_order_release);
    m_jobCapacityCv.notify_all();
    std::lock_guard<std::mutex> lock(m_stateMutex);
    m_activeFrameGroup = nullptr;
}

void InfernuxJoltJobSystemAdapter::QueueJob(JPH::JobSystem::Job *rawJob)
{
    QueueOne(static_cast<AdapterJob *>(rawJob));
}

void InfernuxJoltJobSystemAdapter::QueueJobs(JPH::JobSystem::Job **rawJobs, uint32_t count)
{
    for (uint32_t index = 0; index < count; ++index) {
        QueueOne(static_cast<AdapterJob *>(rawJobs[index]));
    }
}

void InfernuxJoltJobSystemAdapter::QueueOne(AdapterJob *job)
{
    if (job == nullptr) {
        return;
    }

    TaskGroup *group = nullptr;
    {
        std::lock_guard<std::mutex> lock(m_stateMutex);
        group = m_activeFrameGroup;
    }

    job->AddExternalReference();
    try {
        infernux::JobHandle execution;
        if (group != nullptr) {
            execution = m_jobSystem->Schedule(*group, [job] {
                job->Run();
                job->ReleaseExternalReference();
            });
        } else {
            execution = m_jobSystem->Schedule(
                [job] {
                    job->Run();
                    job->ReleaseExternalReference();
                },
                JobDomain::Physics, JobPriority::Critical);
        }
        job->PublishExecution(std::move(execution));
    } catch (...) {
        job->ReleaseExternalReference();
        job->Run();
    }
}

void InfernuxJoltJobSystemAdapter::FreeJob(JPH::JobSystem::Job *rawJob)
{
    delete static_cast<AdapterJob *>(rawJob);
    m_liveJobs.fetch_sub(1, std::memory_order_acq_rel);
    m_jobCapacityCv.notify_one();
}

void InfernuxJoltJobSystemAdapter::RecordCaughtException() noexcept
{
    m_caughtExceptions.fetch_add(1, std::memory_order_relaxed);
}

} // namespace infernux
