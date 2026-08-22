#include "DocumentStore.h"

#include "AtomicFile.h"
#include "InxPath.h"

#include <algorithm>
#include <cctype>
#include <filesystem>
#include <limits>
#include <utility>

namespace infernux
{
namespace
{
size_t DefaultDocumentWorkerCount()
{
    const size_t hardwareThreads = std::thread::hardware_concurrency();
    if (hardwareThreads == 0)
        return 4;
    return std::clamp(hardwareThreads / 2, size_t{2}, size_t{8});
}
} // namespace

void DocumentWriteTicket::Wait() const
{
    {
        std::unique_lock lock(m_mutex);
        m_condition.wait(lock, [this] { return m_status != Status::Pending; });
    }
    ThrowIfFailed();
}

bool DocumentWriteTicket::WaitFor(std::chrono::milliseconds timeout) const
{
    {
        std::unique_lock lock(m_mutex);
        if (!m_condition.wait_for(lock, timeout, [this] { return m_status != Status::Pending; }))
            return false;
    }
    ThrowIfFailed();
    return true;
}

std::optional<DocumentFileState> DocumentWriteTicket::GetCommittedFileState() const
{
    std::lock_guard lock(m_mutex);
    if (m_status != Status::Succeeded)
        return std::nullopt;
    return m_fileState;
}

bool DocumentWriteTicket::IsComplete() const
{
    std::lock_guard lock(m_mutex);
    return m_status != Status::Pending;
}

std::string DocumentWriteTicket::GetStatusName() const
{
    std::lock_guard lock(m_mutex);
    switch (m_status) {
    case Status::Pending:
        return "pending";
    case Status::Succeeded:
        return "succeeded";
    case Status::Superseded:
        return "superseded";
    case Status::Cancelled:
        return "cancelled";
    case Status::Failed:
        return "failed";
    }
    return "invalid";
}

std::string DocumentWriteTicket::GetError() const
{
    std::lock_guard lock(m_mutex);
    return m_error;
}

void DocumentWriteTicket::Complete(Status status, std::string error, std::optional<DocumentFileState> fileState)
{
    {
        std::lock_guard lock(m_mutex);
        if (m_status != Status::Pending)
            throw std::logic_error("document write ticket completed more than once");
        m_status = status;
        m_error = std::move(error);
        m_fileState = fileState;
    }
    m_condition.notify_all();
}

void DocumentWriteTicket::ThrowIfFailed() const
{
    std::lock_guard lock(m_mutex);
    if (m_status == Status::Superseded)
        throw DocumentWriteSuperseded(m_error);
    if (m_status == Status::Cancelled)
        throw DocumentWriteCancelled(m_error);
    if (m_status == Status::Failed)
        throw std::runtime_error(m_error);
}

DocumentStore::DocumentStore(Writer writer, size_t workerCount)
    : m_writer(std::move(writer)), m_workerCount(workerCount)
{
    if (m_workerCount == 0)
        throw std::invalid_argument("DocumentStore requires at least one worker");
    if (!m_writer) {
        m_writer = [](const std::string &path, const std::string &content, const DocumentWriteOptions &options) {
            AtomicWriteOptions atomicOptions;
            atomicOptions.createBackup = options.createBackup;
            // Preserve the complete caller-owned fingerprint. In particular,
            // an empty commit chain has no store-owned baseline that may
            // replace the expected content hash.
            atomicOptions.expectedState = options.expectedFileState;
            if (atomicOptions.expectedState.has_value()) {
                const AtomicFileState current = CaptureAtomicFileState(path);
                if (!(current == *atomicOptions.expectedState)) {
                    throw std::runtime_error("atomic write failed for '" + path +
                                             "': target changed outside the editor before atomic replace");
                }
            }
            std::string error;
            // AtomicFile repeats the full comparison immediately before the
            // replace, closing the race between this early rejection and the
            // final publication.
            if (!WriteTextFileAtomically(path, content, error, std::move(atomicOptions)))
                throw std::runtime_error("atomic write failed for '" + path + "': " + error);
        };
    }
}

DocumentStore::~DocumentStore()
{
    Shutdown();
}

DocumentStore &DocumentStore::Instance()
{
    static auto *instance = new DocumentStore({}, DefaultDocumentWorkerCount());
    return *instance;
}

std::shared_ptr<DocumentWriteTicket> DocumentStore::Submit(const std::string &path, std::string content,
                                                           DocumentWriteOptions options)
{
    const std::string normalizedPath = FilesystemPathKey(path);
    const std::string resolvedPath = ResolvePath(path);
    std::shared_ptr<DocumentWriteTicket> superseded;
    std::shared_ptr<DocumentWriteTicket> ticket;

    {
        std::lock_guard lock(m_mutex);
        if (m_state == State::Closing)
            throw std::runtime_error("DocumentStore is shutting down");
        if (m_state == State::Stopped)
            StartWorkerLocked();

        const uint64_t generation = ++m_generations[normalizedPath];
        ticket = std::shared_ptr<DocumentWriteTicket>(new DocumentWriteTicket(normalizedPath, generation));
        if (const auto existing = m_pending.find(normalizedPath); existing != m_pending.end())
            superseded = existing->second.ticket;

        m_pending.insert_or_assign(normalizedPath,
                                   Request{normalizedPath, resolvedPath, std::move(content), options, ticket});
        if (m_activePaths.find(normalizedPath) == m_activePaths.end() &&
            m_queuedPaths.find(normalizedPath) == m_queuedPaths.end()) {
            m_readyPaths.push_back(normalizedPath);
            m_queuedPaths.insert(normalizedPath);
        }
    }

    if (superseded) {
        superseded->Complete(DocumentWriteTicket::Status::Superseded,
                             "document generation " + std::to_string(superseded->GetGeneration()) + " for '" +
                                 normalizedPath + "' was superseded by generation " +
                                 std::to_string(ticket->GetGeneration()));
    }
    m_condition.notify_one();
    return ticket;
}

uint64_t DocumentStore::WriteAndWait(const std::string &path, std::string content, DocumentWriteOptions options)
{
    auto ticket = Submit(path, std::move(content), options);
    ticket->Wait();
    return ticket->GetGeneration();
}

bool DocumentStore::Cancel(const std::shared_ptr<DocumentWriteTicket> &ticket)
{
    if (!ticket)
        return false;

    std::shared_ptr<DocumentWriteTicket> cancelled;
    {
        std::lock_guard lock(m_mutex);
        const auto pending = m_pending.find(ticket->GetPath());
        if (pending == m_pending.end() || pending->second.ticket != ticket)
            return false;

        cancelled = pending->second.ticket;
        m_pending.erase(pending);
        if (m_activePaths.find(ticket->GetPath()) == m_activePaths.end()) {
            m_readyPaths.erase(std::remove(m_readyPaths.begin(), m_readyPaths.end(), ticket->GetPath()),
                               m_readyPaths.end());
            m_queuedPaths.erase(ticket->GetPath());
        }
    }

    cancelled->Complete(DocumentWriteTicket::Status::Cancelled,
                        "document generation " + std::to_string(cancelled->GetGeneration()) + " for '" +
                            cancelled->GetPath() + "' was cancelled before IO began");
    m_condition.notify_all();
    return true;
}

DocumentPathMetrics DocumentStore::GetMetrics(const std::string &path) const
{
    const std::string key = FilesystemPathKey(path);
    std::lock_guard lock(m_mutex);
    DocumentPathMetrics metrics;
    if (const auto generation = m_generations.find(key); generation != m_generations.end())
        metrics.latestSubmittedGeneration = generation->second;
    if (const auto generation = m_succeededGenerations.find(key); generation != m_succeededGenerations.end())
        metrics.latestSucceededGeneration = generation->second;
    if (const auto generation = m_failedGenerations.find(key); generation != m_failedGenerations.end())
        metrics.latestFailedGeneration = generation->second;
    if (const auto pending = m_pending.find(key); pending != m_pending.end())
        metrics.pendingGeneration = pending->second.ticket->GetGeneration();
    if (const auto active = m_activeGenerations.find(key); active != m_activeGenerations.end())
        metrics.activeGeneration = active->second;
    return metrics;
}

DocumentFileState DocumentStore::CaptureFileState(const std::string &path) const
{
    return CaptureAtomicFileState(ResolvePath(path));
}

bool DocumentStore::IsIdle() const
{
    std::lock_guard<std::mutex> lock(m_mutex);
    return IsIdleLocked(nullptr);
}

void DocumentStore::Flush()
{
    std::unique_lock lock(m_mutex);
    m_condition.wait(lock, [this] { return IsIdleLocked(); });
}

void DocumentStore::Flush(const std::string &path)
{
    const std::string key = FilesystemPathKey(path);
    std::unique_lock lock(m_mutex);
    m_condition.wait(lock, [this, &key] { return IsIdleLocked(&key); });
}

void DocumentStore::Shutdown()
{
    std::vector<std::thread> workers;
    {
        std::unique_lock lock(m_mutex);
        if (m_state == State::Stopped)
            return;
        if (m_state == State::Closing) {
            m_condition.wait(lock, [this] { return m_state == State::Stopped; });
            return;
        }
        m_state = State::Closing;
        workers = std::move(m_workers);
    }
    m_condition.notify_all();
    for (auto &worker : workers) {
        if (worker.joinable())
            worker.join();
    }

    {
        std::lock_guard lock(m_mutex);
        m_state = State::Stopped;
    }
    m_condition.notify_all();
}

std::string DocumentStore::ResolvePath(const std::string &path)
{
    if (path.empty())
        throw std::invalid_argument("document path must not be empty");
    return ResolveFilesystemPath(path);
}

void DocumentStore::StartWorkerLocked()
{
    if (!m_workers.empty())
        throw std::logic_error("stopped DocumentStore still owns worker threads");
    m_state = State::Running;
    m_workers.reserve(m_workerCount);
    for (size_t index = 0; index < m_workerCount; ++index)
        m_workers.emplace_back(&DocumentStore::WorkerMain, this);
}

void DocumentStore::WorkerMain()
{
    while (true) {
        Request request;
        {
            std::unique_lock lock(m_mutex);
            m_condition.wait(lock, [this] { return !m_readyPaths.empty() || m_state == State::Closing; });
            if (m_readyPaths.empty())
                return;

            const std::string key = std::move(m_readyPaths.front());
            m_readyPaths.pop_front();
            m_queuedPaths.erase(key);
            auto pending = m_pending.find(key);
            if (pending == m_pending.end())
                throw std::logic_error("DocumentStore ready path has no pending request");
            request = std::move(pending->second);
            m_pending.erase(pending);
            m_activePaths.insert(key);
            m_activeGenerations.insert_or_assign(key, request.ticket->GetGeneration());
        }

        std::string error;
        std::optional<DocumentFileState> fileState;
        try {
            DocumentWriteOptions effectiveOptions = request.options;
            {
                std::lock_guard lock(m_mutex);
                const auto committed = m_committedFileStates.find(request.key);
                if (!request.options.commitChainToken.empty() && committed != m_committedFileStates.end() &&
                    committed->second.commitChainToken == request.options.commitChainToken &&
                    effectiveOptions.expectedFileState.has_value()) {
                    // Preserve CAS semantics only across the explicit same
                    // editor chain.  The caller's original baseline can be
                    // older than the active write, but the last store-owned
                    // commit is authoritative for this chain.
                    effectiveOptions.expectedFileState = committed->second.fileState;
                }
            }
            m_writer(request.path, request.content, effectiveOptions);
            fileState = CaptureAtomicFileState(request.path);
        } catch (const std::exception &exception) {
            error = exception.what();
        }

        const bool succeeded = error.empty();
        {
            std::lock_guard lock(m_mutex);
            if (succeeded && !request.options.commitChainToken.empty() && fileState.has_value()) {
                // Keep the last durable state for the explicit document
                // chain, independent of whether its successor was queued
                // before or after this completion. A future request from the
                // same chain still performs strict CAS against this complete
                // state, so an intervening external edit is rejected.
                m_committedFileStates.insert_or_assign(
                    request.key, CommittedChainState{request.options.commitChainToken, *fileState});
            } else if (succeeded) {
                // An unchained writer is independent and invalidates any
                // previous store-owned chain baseline for this path.
                m_committedFileStates.erase(request.key);
            }
            // Complete the ticket while the path is still active.  This keeps
            // Wait/Flush observers from seeing an idle path before its
            // terminal status and committed fingerprint are published.
            if (succeeded)
                request.ticket->Complete(DocumentWriteTicket::Status::Succeeded, {}, fileState);
            else
                request.ticket->Complete(DocumentWriteTicket::Status::Failed, std::move(error));
            m_activePaths.erase(request.key);
            m_activeGenerations.erase(request.key);
            if (succeeded)
                m_succeededGenerations.insert_or_assign(request.key, request.ticket->GetGeneration());
            else
                m_failedGenerations.insert_or_assign(request.key, request.ticket->GetGeneration());
            if (m_pending.find(request.key) != m_pending.end() &&
                m_queuedPaths.find(request.key) == m_queuedPaths.end()) {
                m_readyPaths.push_back(request.key);
                m_queuedPaths.insert(request.key);
            }
        }
        m_condition.notify_all();
    }
}

bool DocumentStore::IsIdleLocked(const std::string *key) const
{
    if (!key)
        return m_pending.empty() && m_activePaths.empty() && m_readyPaths.empty();
    return m_pending.find(*key) == m_pending.end() && m_activePaths.find(*key) == m_activePaths.end() &&
           m_queuedPaths.find(*key) == m_queuedPaths.end();
}

} // namespace infernux
