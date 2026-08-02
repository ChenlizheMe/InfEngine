#pragma once

#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <deque>
#include <functional>
#include <memory>
#include <mutex>
#include <optional>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace infernux
{

class DocumentWriteSuperseded final : public std::runtime_error
{
  public:
    using std::runtime_error::runtime_error;
};

class DocumentWriteCancelled final : public std::runtime_error
{
  public:
    using std::runtime_error::runtime_error;
};

struct DocumentFileState
{
    uint64_t size = 0;
    int64_t modifiedNs = 0;
};

struct DocumentWriteOptions
{
    bool createBackup = false;
};

struct DocumentPathMetrics
{
    uint64_t latestSubmittedGeneration = 0;
    uint64_t latestSucceededGeneration = 0;
    uint64_t latestFailedGeneration = 0;
    uint64_t pendingGeneration = 0;
    uint64_t activeGeneration = 0;
};

class DocumentWriteTicket final
{
  public:
    [[nodiscard]] const std::string &GetPath() const noexcept
    {
        return m_path;
    }
    [[nodiscard]] uint64_t GetGeneration() const noexcept
    {
        return m_generation;
    }

    [[nodiscard]] bool IsComplete() const;
    [[nodiscard]] std::string GetStatusName() const;

    void Wait() const;
    bool WaitFor(std::chrono::milliseconds timeout) const;
    [[nodiscard]] std::optional<DocumentFileState> GetCommittedFileState() const;

  private:
    friend class DocumentStore;

    enum class Status : uint8_t
    {
        Pending,
        Succeeded,
        Superseded,
        Cancelled,
        Failed,
    };

    DocumentWriteTicket(std::string path, uint64_t generation) : m_path(std::move(path)), m_generation(generation)
    {
    }

    void Complete(Status status, std::string error = {}, std::optional<DocumentFileState> fileState = std::nullopt);
    void ThrowIfFailed() const;

    std::string m_path;
    uint64_t m_generation = 0;
    mutable std::mutex m_mutex;
    mutable std::condition_variable m_condition;
    Status m_status = Status::Pending;
    std::string m_error;
    std::optional<DocumentFileState> m_fileState;
};

class DocumentStore final
{
  public:
    using Writer = std::function<void(const std::string &, const std::string &, const DocumentWriteOptions &)>;

    explicit DocumentStore(Writer writer = {}, size_t workerCount = 2);
    ~DocumentStore();

    DocumentStore(const DocumentStore &) = delete;
    DocumentStore &operator=(const DocumentStore &) = delete;

    static DocumentStore &Instance();

    std::shared_ptr<DocumentWriteTicket> Submit(const std::string &path, std::string content,
                                                DocumentWriteOptions options = {});
    uint64_t WriteAndWait(const std::string &path, std::string content, DocumentWriteOptions options = {});
    bool Cancel(const std::shared_ptr<DocumentWriteTicket> &ticket);
    [[nodiscard]] DocumentPathMetrics GetMetrics(const std::string &path) const;
    void Flush();
    void Flush(const std::string &path);
    void Shutdown();

  private:
    enum class State : uint8_t
    {
        Stopped,
        Running,
        Closing,
    };

    struct Request
    {
        std::string key;
        std::string path;
        std::string content;
        DocumentWriteOptions options;
        std::shared_ptr<DocumentWriteTicket> ticket;
    };

    static std::string ResolvePath(const std::string &path);
    void StartWorkerLocked();
    void WorkerMain();
    [[nodiscard]] bool IsIdleLocked(const std::string *key = nullptr) const;

    Writer m_writer;
    size_t m_workerCount = 2;
    mutable std::mutex m_mutex;
    std::condition_variable m_condition;
    State m_state = State::Stopped;
    std::vector<std::thread> m_workers;
    std::unordered_map<std::string, Request> m_pending;
    std::unordered_set<std::string> m_activePaths;
    std::deque<std::string> m_readyPaths;
    std::unordered_set<std::string> m_queuedPaths;
    std::unordered_map<std::string, uint64_t> m_generations;
    std::unordered_map<std::string, uint64_t> m_succeededGenerations;
    std::unordered_map<std::string, uint64_t> m_failedGenerations;
    std::unordered_map<std::string, uint64_t> m_activeGenerations;
};

} // namespace infernux
