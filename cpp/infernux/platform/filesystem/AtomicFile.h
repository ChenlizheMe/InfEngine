#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <string_view>

namespace infernux
{

struct AtomicFileState
{
    bool exists = false;
    uint64_t size = 0;
    int64_t modifiedNs = 0;
    uint64_t contentHash = 0;

    [[nodiscard]] bool operator==(const AtomicFileState &other) const noexcept
    {
        return exists == other.exists && size == other.size && modifiedNs == other.modifiedNs &&
               contentHash == other.contentHash;
    }
};

struct AtomicWriteOptions
{
    bool createBackup = false;
    std::optional<AtomicFileState> expectedState;
};

/// Capture a stable state used by conditional document writes. Missing files
/// are represented explicitly; inspection failures throw with path context.
AtomicFileState CaptureAtomicFileState(const std::string &path);

/// Write UTF-8 text through a unique same-directory temporary file and atomically replace the target.
/// When createBackup is enabled and the target exists, the previous complete target is first
/// durably published as `<target>.bak`.
bool WriteTextFileAtomically(const std::string &path, std::string_view content, std::string &error,
                             AtomicWriteOptions options = {});

/// Remove a file and persist the directory update where the platform exposes directory fsync.
/// Missing files are treated as already removed.
bool RemoveFileDurably(const std::string &path, std::string &error);

} // namespace infernux
