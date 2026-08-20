#include "AtomicFile.h"

#include "InxPath.h"

#include <atomic>
#include <cerrno>
#include <chrono>
#include <filesystem>
#include <fstream>
#include <limits>
#include <system_error>
#include <thread>

#ifdef _WIN32
#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <Windows.h>
#else
#include <fcntl.h>
#include <unistd.h>
#endif

namespace infernux
{
namespace
{

uint64_t HashFileContents(const std::filesystem::path &path)
{
    std::ifstream file(path, std::ios::in | std::ios::binary);
    if (!file.is_open())
        throw std::runtime_error("cannot open file for state capture");
    uint64_t hash = 1469598103934665603ull;
    char buffer[64 * 1024];
    while (file) {
        file.read(buffer, sizeof(buffer));
        const auto count = file.gcount();
        for (std::streamsize index = 0; index < count; ++index) {
            hash ^= static_cast<unsigned char>(buffer[index]);
            hash *= 1099511628211ull;
        }
    }
    if (!file.eof())
        throw std::runtime_error("failed while reading file for state capture");
    return hash;
}

std::filesystem::path MakeTemporaryPath(const std::filesystem::path &target)
{
    static std::atomic<uint64_t> sequence{0};
    std::filesystem::path temporary = target;
    const auto timestamp = std::chrono::steady_clock::now().time_since_epoch().count();
    temporary += std::filesystem::path(".tmp." + std::to_string(timestamp) + "." +
                                       std::to_string(sequence.fetch_add(1, std::memory_order_relaxed)));
    return temporary;
}

bool ReplaceFile(const std::filesystem::path &source, const std::filesystem::path &target, std::error_code &error)
{
#ifdef _WIN32
    if (::MoveFileExW(source.c_str(), target.c_str(), MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH)) {
        error.clear();
        return true;
    }
    error = std::error_code(static_cast<int>(::GetLastError()), std::system_category());
    return false;
#else
    std::filesystem::rename(source, target, error);
    return !error;
#endif
}

bool FlushFileToDisk(const std::filesystem::path &path, std::error_code &error)
{
#ifdef _WIN32
    HANDLE handle = ::CreateFileW(path.c_str(), GENERIC_WRITE, FILE_SHARE_READ, nullptr, OPEN_EXISTING,
                                  FILE_ATTRIBUTE_NORMAL, nullptr);
    if (handle == INVALID_HANDLE_VALUE) {
        error = std::error_code(static_cast<int>(::GetLastError()), std::system_category());
        return false;
    }
    const bool flushed = ::FlushFileBuffers(handle) != 0;
    const DWORD flushError = flushed ? ERROR_SUCCESS : ::GetLastError();
    ::CloseHandle(handle);
    if (!flushed) {
        error = std::error_code(static_cast<int>(flushError), std::system_category());
        return false;
    }
#else
    const int descriptor = ::open(path.c_str(), O_RDONLY);
    if (descriptor < 0) {
        error = std::error_code(errno, std::generic_category());
        return false;
    }
    const bool flushed = ::fsync(descriptor) == 0;
    const int flushError = flushed ? 0 : errno;
    ::close(descriptor);
    if (!flushed) {
        error = std::error_code(flushError, std::generic_category());
        return false;
    }
#endif
    error.clear();
    return true;
}

bool FlushParentDirectory(const std::filesystem::path &target, std::error_code &error)
{
#ifdef _WIN32
    error.clear();
    return true;
#else
    const std::filesystem::path parent =
        target.parent_path().empty() ? std::filesystem::path(".") : target.parent_path();
    const int descriptor = ::open(parent.c_str(), O_RDONLY | O_DIRECTORY);
    if (descriptor < 0) {
        error = std::error_code(errno, std::generic_category());
        return false;
    }
    const bool flushed = ::fsync(descriptor) == 0;
    const int flushError = flushed ? 0 : errno;
    ::close(descriptor);
    if (!flushed) {
        error = std::error_code(flushError, std::generic_category());
        return false;
    }
    error.clear();
    return true;
#endif
}

bool PublishBackup(const std::filesystem::path &target, std::string &error)
{
    std::error_code existsError;
    if (!std::filesystem::exists(target, existsError)) {
        if (existsError) {
            error = "failed to inspect backup source: " + existsError.message();
            return false;
        }
        return true;
    }

    std::filesystem::path backup = target;
    backup += ".bak";
    const std::filesystem::path temporary = MakeTemporaryPath(backup);
    std::error_code copyError;
    std::filesystem::copy_file(target, temporary, std::filesystem::copy_options::overwrite_existing, copyError);
    if (copyError) {
        std::error_code ignored;
        std::filesystem::remove(temporary, ignored);
        error = "failed to copy backup: " + copyError.message();
        return false;
    }

    std::error_code flushError;
    if (!FlushFileToDisk(temporary, flushError)) {
        std::error_code ignored;
        std::filesystem::remove(temporary, ignored);
        error = "failed to flush backup: " + flushError.message();
        return false;
    }

    std::error_code replaceError;
    if (!ReplaceFile(temporary, backup, replaceError)) {
        std::error_code ignored;
        std::filesystem::remove(temporary, ignored);
        error = "failed to publish backup: " + replaceError.message();
        return false;
    }

    std::error_code directoryFlushError;
    if (!FlushParentDirectory(backup, directoryFlushError)) {
        error = "backup published but parent directory flush failed: " + directoryFlushError.message();
        return false;
    }
    return true;
}

} // namespace

AtomicFileState CaptureAtomicFileState(const std::string &path)
{
    const std::filesystem::path target = ToFsPath(path);
    std::error_code error;
    const bool exists = std::filesystem::exists(target, error);
    if (error)
        throw std::runtime_error("failed to inspect '" + path + "': " + error.message());
    if (!exists)
        return {};
    if (!std::filesystem::is_regular_file(target, error) || error)
        throw std::runtime_error("document target is not a regular file: '" + path + "'");
    const uintmax_t size = std::filesystem::file_size(target, error);
    if (error || size > std::numeric_limits<uint64_t>::max())
        throw std::runtime_error("failed to inspect file size for '" + path + "'");
    const auto modified = std::filesystem::last_write_time(target, error);
    if (error)
        throw std::runtime_error("failed to inspect modification time for '" + path + "'");
    AtomicFileState state;
    state.exists = true;
    state.size = static_cast<uint64_t>(size);
    state.modifiedNs = static_cast<int64_t>(modified.time_since_epoch().count());
    state.contentHash = HashFileContents(target);
    return state;
}

bool IsTransientReplaceError(const std::error_code &error)
{
#ifdef _WIN32
    return error.value() == ERROR_ACCESS_DENIED || error.value() == ERROR_SHARING_VIOLATION ||
           error.value() == ERROR_LOCK_VIOLATION;
#else
    (void)error;
    return false;
#endif
}

bool WriteTextFileAtomically(const std::string &path, std::string_view content, std::string &error,
                             AtomicWriteOptions options)
{
    const std::filesystem::path target = ToFsPath(path);
    const std::filesystem::path temporary = MakeTemporaryPath(target);
    try {
        if (options.createBackup && !PublishBackup(target, error))
            return false;

        std::ofstream file(temporary, std::ios::out | std::ios::trunc | std::ios::binary);
        if (!file.is_open()) {
            error = "cannot open temporary file";
            return false;
        }
        file.write(content.data(), static_cast<std::streamsize>(content.size()));
        file.flush();
        if (!file.good()) {
            file.close();
            std::error_code ignored;
            std::filesystem::remove(temporary, ignored);
            error = "failed while writing temporary file";
            return false;
        }
        file.close();

        std::error_code flushError;
        if (!FlushFileToDisk(temporary, flushError)) {
            std::error_code ignored;
            std::filesystem::remove(temporary, ignored);
            error = "failed to flush temporary file: " + flushError.message();
            return false;
        }

        if (options.expectedState.has_value()) {
            const AtomicFileState current = CaptureAtomicFileState(path);
            if (!(current == *options.expectedState)) {
                std::error_code ignored;
                std::filesystem::remove(temporary, ignored);
                error = "target changed outside the editor before atomic replace";
                return false;
            }
        }

        std::error_code replaceError;
        constexpr unsigned kReplaceAttempts = 8;
        for (unsigned attempt = 0; attempt < kReplaceAttempts; ++attempt) {
            if (ReplaceFile(temporary, target, replaceError))
                break;
            if (!IsTransientReplaceError(replaceError) || attempt + 1 == kReplaceAttempts) {
                std::error_code ignored;
                std::filesystem::remove(temporary, ignored);
                error = replaceError.message();
                return false;
            }

            // Editors, indexers, and virus scanners can briefly open the
            // destination without FILE_SHARE_DELETE. Keep the retry on the IO
            // worker and preserve CAS authority across the wait.
            std::this_thread::sleep_for(std::chrono::milliseconds(2u << attempt));
            if (options.expectedState.has_value()) {
                const AtomicFileState current = CaptureAtomicFileState(path);
                if (!(current == *options.expectedState)) {
                    std::error_code ignored;
                    std::filesystem::remove(temporary, ignored);
                    error = "target changed outside the editor before atomic replace";
                    return false;
                }
            }
        }
        std::error_code directoryFlushError;
        if (!FlushParentDirectory(target, directoryFlushError)) {
            error = "file replaced but parent directory flush failed: " + directoryFlushError.message();
            return false;
        }
        error.clear();
        return true;
    } catch (const std::exception &exception) {
        std::error_code ignored;
        std::filesystem::remove(temporary, ignored);
        error = exception.what();
        return false;
    }
}

bool RemoveFileDurably(const std::string &path, std::string &error)
{
    const std::filesystem::path target = ToFsPath(path);
    std::error_code removeError;
    const bool removed = std::filesystem::remove(target, removeError);
    if (removeError) {
        error = "failed to remove file: " + removeError.message();
        return false;
    }
    if (!removed) {
        error.clear();
        return true;
    }

    std::error_code directoryFlushError;
    if (!FlushParentDirectory(target, directoryFlushError)) {
        error = "file removed but parent directory flush failed: " + directoryFlushError.message();
        return false;
    }
    error.clear();
    return true;
}

} // namespace infernux
