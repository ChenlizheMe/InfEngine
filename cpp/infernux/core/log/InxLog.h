#pragma once

#include <algorithm>
#include <atomic>
#include <deque>
#include <filesystem>
#include <fstream>
#include <functional>
#include <iostream>
#include <mutex>
#include <sstream>
#include <string>
#include <vector>

namespace infernux
{
#ifndef INFERNUX_COMPILE_OUT_DEBUG_LOGS
#define INFERNUX_COMPILE_OUT_DEBUG_LOGS 0
#endif

enum LogLevel
{
    LOG_DEBUG = 0,
    LOG_INFO = 1,
    LOG_WARN = 2,
    LOG_ERROR = 3,
    LOG_FATAL = 4
};

class InxLog
{
  public:
    enum class FileLogMode
    {
        ImmediateFlush,
        DeferredTail,
    };

    static InxLog &GetInstance();

    /// Open a log file under the given path.  When a file is open, all log
    /// output goes to the file instead of the console.
    void SetLogFile(const std::string &path)
    {
        ConfigureLogFile(path, FileLogMode::ImmediateFlush, 0);
    }

    void SetDeferredLogFile(const std::string &path, size_t retainedEntries = 100)
    {
        ConfigureLogFile(path, FileLogMode::DeferredTail, retainedEntries);
    }

    void FlushLogFile()
    {
        std::lock_guard<std::mutex> lock(mutex_);
        FlushLogFileLocked();
    }

    void Shutdown()
    {
        std::lock_guard<std::mutex> lock(mutex_);
        FlushLogFileLocked();
        if (logFile_.is_open())
            logFile_.close();
        logFilePath_.clear();
        deferredEntries_.clear();
        deferredFileLogging_ = false;
        deferredRetention_ = 0;
    }

    /// Callback signature for log sinks.
    using SinkCallback =
        std::function<void(LogLevel level, const char *file, int line, const std::string &message, bool internalOnly)>;

    /// Register a log sink that receives every log message at or above the
    /// current log level.  Returns an opaque ID for later removal.
    size_t AddSink(SinkCallback sink)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        size_t id = nextSinkId_++;
        sinks_.push_back({id, std::move(sink)});
        return id;
    }

    /// Remove a previously registered sink by its ID.
    void RemoveSink(size_t sinkId)
    {
        std::lock_guard<std::mutex> lock(mutex_);
        sinks_.erase(
            std::remove_if(sinks_.begin(), sinks_.end(), [sinkId](const SinkEntry &e) { return e.id == sinkId; }),
            sinks_.end());
    }

    template <typename... Args> void Log(LogLevel level, const char *file, int line, Args &&...args)
    {
        LogImpl(level, file, int(line), false, std::forward<Args>(args)...);
    }

    template <typename... Args> void LogInternal(LogLevel level, const char *file, int line, Args &&...args)
    {
        LogImpl(level, file, int(line), true, std::forward<Args>(args)...);
    }

  private:
    template <typename... Args>
    void LogImpl(LogLevel level, const char *file, int line, bool internalOnly, Args &&...args)
    {
#if INFERNUX_COMPILE_OUT_DEBUG_LOGS
        // Shipping builds must not forward C++ debug diagnostics to any sink,
        // even when a caller bypasses INXLOG_DEBUG and invokes Log directly.
        if (level == LOG_DEBUG)
            return;
#endif
        if (logLevel.load(std::memory_order_relaxed) > level)
            return;

        // Build the ENTIRE formatted message outside the lock so that
        // string formatting never holds the mutex.  Use '\n' instead of
        // std::endl to avoid a blocking flush() while the lock is held —
        // this prevents deadlocks / severe contention when multiple
        // threads (main loop, Vulkan validation callback, file watcher)
        // log concurrently.

        // Plain message for file output (no ANSI color codes)
        std::ostringstream plain;
        plain << '[' << LogLevelToString(level) << "] " << '(' << file << ':' << line << ") ";
        (plain << ... << args);
        plain << '\n';

        std::string plainStr = plain.str();

        // Copy sink list under lock, then invoke outside lock to avoid
        // deadlocks if a sink calls back into the logger.
        std::vector<SinkCallback> sinksCopy;
        {
            std::lock_guard<std::mutex> lock(mutex_);

            if (deferredFileLogging_) {
                deferredEntries_.push_back(plainStr);
                while (deferredEntries_.size() > deferredRetention_)
                    deferredEntries_.pop_front();
            }

            if (logFile_.is_open()) {
                logFile_.write(plainStr.data(), static_cast<std::streamsize>(plainStr.size()));
                logFile_.flush();
            } else if (!internalOnly) {
                // Console output with ANSI colors
                std::string msg = LogLevelToColor(level) + plainStr;
                // Insert reset code before the trailing newline
                msg.insert(msg.size() - 1, "\033[0m");
                std::cout.write(msg.data(), static_cast<std::streamsize>(msg.size()));
            }

            sinksCopy.reserve(sinks_.size());
            for (const auto &s : sinks_)
                sinksCopy.push_back(s.callback);
        }

        // Strip trailing newline for sink consumers
        std::string msgForSinks = plainStr;
        if (!msgForSinks.empty() && msgForSinks.back() == '\n')
            msgForSinks.pop_back();

        for (const auto &sink : sinksCopy)
            sink(level, file, line, msgForSinks, internalOnly);
    }

  public:
    void SetLogLevel(int level)
    {
        logLevel.store(level, std::memory_order_relaxed);
    }

    int GetLogLevel() const
    {
        return logLevel.load(std::memory_order_relaxed);
    }

  private:
    InxLog() : logLevel(LOG_INFO), nextSinkId_(0)
    {
    }
    ~InxLog()
    {
        Shutdown();
    }
    InxLog(const InxLog &) = delete;
    InxLog &operator=(const InxLog &) = delete;

    void ConfigureLogFile(const std::string &path, FileLogMode mode, size_t retainedEntries)
    {
        std::lock_guard<std::mutex> lock(mutex_);

        if (logFile_.is_open())
            logFile_.close();

        logFilePath_ = path;
        deferredEntries_.clear();
        deferredFileLogging_ = (mode == FileLogMode::DeferredTail);
        deferredRetention_ = deferredFileLogging_ ? (retainedEntries > 0 ? retainedEntries : 100) : 0;

        if (deferredFileLogging_) {
            std::ofstream truncateFile(std::filesystem::u8path(path), std::ios::out | std::ios::trunc);
            return;
        }

        logFile_.open(std::filesystem::u8path(path), std::ios::out | std::ios::trunc);
    }

    void FlushLogFileLocked()
    {
        if (deferredFileLogging_) {
            if (logFilePath_.empty())
                return;

            std::ofstream out(std::filesystem::u8path(logFilePath_), std::ios::out | std::ios::trunc);
            if (!out.is_open())
                return;

            for (const auto &entry : deferredEntries_)
                out.write(entry.data(), static_cast<std::streamsize>(entry.size()));
            out.flush();
            return;
        }

        if (logFile_.is_open())
            logFile_.flush();
    }

    std::mutex mutex_;
    std::ofstream logFile_;
    std::string logFilePath_;
    std::deque<std::string> deferredEntries_;
    bool deferredFileLogging_ = false;
    size_t deferredRetention_ = 0;

    std::atomic<int> logLevel;

    struct SinkEntry
    {
        size_t id;
        SinkCallback callback;
    };
    std::vector<SinkEntry> sinks_;
    size_t nextSinkId_ = 0;

    const char *LogLevelToString(LogLevel level)
    {
        switch (level) {
        case LOG_DEBUG:
            return "DEBUG";
        case LOG_INFO:
            return "INFO";
        case LOG_WARN:
            return "WARN";
        case LOG_ERROR:
            return "ERROR";
        case LOG_FATAL:
            return "FATAL";
        default:
            return "UNKNOWN";
        }
    }

    const char *LogLevelToColor(LogLevel level)
    {
        switch (level) {
        case LOG_DEBUG:
            return "\033[36m"; // Cyan
        case LOG_INFO:
            return "\033[37m"; // White
        case LOG_WARN:
            return "\033[33m"; // Yellow
        case LOG_ERROR:
            return "\033[31m"; // Red
        case LOG_FATAL:
            return "\033[35m"; // Magenta
        default:
            return "\033[0m"; // Default color
        }
    }
};
} // namespace infernux

#define INXLOG_INTERNAL(level, ...)                                                                                    \
    do {                                                                                                               \
        if ((level) >= InxLog::GetInstance().GetLogLevel())                                                            \
            InxLog::GetInstance().Log(static_cast<LogLevel>(level), __FILE__, __LINE__, __VA_ARGS__);                  \
    } while (false)

#define INXLOG_FILE_ONLY(level, ...)                                                                                   \
    do {                                                                                                               \
        if ((level) >= InxLog::GetInstance().GetLogLevel())                                                            \
            InxLog::GetInstance().LogInternal(static_cast<LogLevel>(level), __FILE__, __LINE__, __VA_ARGS__);          \
    } while (false)

#if INFERNUX_COMPILE_OUT_DEBUG_LOGS
// Do not evaluate debug-log arguments in shipping builds. Besides keeping the
// editor Console clean, this removes formatting and helper-call overhead.
#define INXLOG_DEBUG(...) ((void)0)
#define INXLOG_DEBUG_INTERNAL(...) ((void)0)
#else
#define INXLOG_DEBUG(...) INXLOG_INTERNAL(LOG_DEBUG, __VA_ARGS__)
#define INXLOG_DEBUG_INTERNAL(...) INXLOG_FILE_ONLY(LOG_DEBUG, __VA_ARGS__)
#endif
#define INXLOG_INFO(...) INXLOG_INTERNAL(LOG_INFO, __VA_ARGS__)
#define INXLOG_WARN(...) INXLOG_INTERNAL(LOG_WARN, __VA_ARGS__)
#define INXLOG_ERROR(...) INXLOG_INTERNAL(LOG_ERROR, __VA_ARGS__)
#define INXLOG_INFO_INTERNAL(...) INXLOG_FILE_ONLY(LOG_INFO, __VA_ARGS__)
#define INXLOG_WARN_INTERNAL(...) INXLOG_FILE_ONLY(LOG_WARN, __VA_ARGS__)
#define INXLOG_ERROR_INTERNAL(...) INXLOG_FILE_ONLY(LOG_ERROR, __VA_ARGS__)
#define INXLOG_FATAL(...)                                                                                              \
    do {                                                                                                               \
        INXLOG_INTERNAL(LOG_FATAL, __VA_ARGS__);                                                                       \
        std::abort();                                                                                                  \
    } while (false)

#define INXLOG_FATAL_INTERNAL(...)                                                                                     \
    do {                                                                                                               \
        INXLOG_FILE_ONLY(LOG_FATAL, __VA_ARGS__);                                                                      \
        std::abort();                                                                                                  \
    } while (false)

#define INXLOG_SET_LEVEL(level) InxLog::GetInstance().SetLogLevel(level)

#define INXLOG_GET_LEVEL() InxLog::GetInstance().GetLogLevel()

#define INXLOG_SET_FILE(path) InxLog::GetInstance().SetLogFile(path)

#define INXLOG_SET_DEFERRED_FILE(path, retained) InxLog::GetInstance().SetDeferredLogFile(path, retained)

#define INXLOG_FLUSH_FILE() InxLog::GetInstance().FlushLogFile()

#define INXLOG_SHUTDOWN() InxLog::GetInstance().Shutdown()
