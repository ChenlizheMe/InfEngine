#pragma once

#include <algorithm>
#include <cctype>
#include <core/log/InxLog.h>
#include <cwctype>
#include <filesystem>
#include <fstream>
#include <string>
#include <vector>

#include <core/config/InxPlatform.h>

namespace infernux
{
inline std::string FromFsPath(const std::filesystem::path &p);
}

#ifdef INX_PLATFORM_WINDOWS
namespace infernux
{
inline const char *GetExecutableDir()
{
    // Thread-safe via C++11 magic statics (initialized exactly once).
    static const std::string path = []() -> std::string {
        wchar_t buffer[MAX_PATH];
        DWORD len = GetModuleFileNameW(NULL, buffer, MAX_PATH);
        if (len == 0) {
            INXLOG_ERROR("Failed to get executable path, using current directory as fallback.");
            return ".";
        }
        return FromFsPath(std::filesystem::path(buffer).parent_path());
    }();
    return path.c_str();
}

} // namespace infernux
#else
#include <limits.h>
#include <string.h>
#include <unistd.h>

#if defined(__APPLE__)
#include <mach-o/dyld.h>
#endif

namespace infernux
{
inline const char *GetExecutableDir()
{
    // Thread-safe via C++11 magic statics (initialized exactly once).
    static const std::string path = []() -> std::string {
        char result[PATH_MAX];
        ssize_t len = 0;

#if defined(__linux__)
        len = readlink("/proc/self/exe", result, PATH_MAX);
#elif defined(__APPLE__)
        uint32_t size = sizeof(result);
        if (_NSGetExecutablePath(result, &size) != 0) {
            INXLOG_ERROR("Buffer too small for executable path, using current directory as fallback.");
            return ".";
        }
        len = strlen(result);
#endif

        if (len <= 0) {
            INXLOG_ERROR("Failed to get executable path, using current directory as fallback.");
            return ".";
        }
        return FromFsPath(std::filesystem::path(result, result + len).parent_path());
    }();
    return path.c_str();
}
} // namespace infernux
#endif
namespace infernux
{

/**
 * @brief Normalize a portable logical path by using forward slashes.
 */
inline std::string NormalizePortablePath(const std::string &path)
{
    std::string result = path;
    std::replace(result.begin(), result.end(), '\\', '/');
    return result;
}

/**
 * @brief Convert a UTF-8 path string to std::filesystem::path.
 *
 * On Windows the native encoding for narrow strings is the active code-page,
 * NOT UTF-8.  This helper ensures paths that were produced by Python
 * (which always outputs UTF-8) are correctly converted to wide-char
 * internally so that std::ifstream / std::filesystem operations work with
 * non-ASCII characters (e.g. Chinese filenames).
 */
inline std::filesystem::path ToFsPath(const std::string &utf8Path)
{
#ifdef INX_PLATFORM_WINDOWS
    // MultiByteToWideChar: convert UTF-8 → wchar_t
    if (utf8Path.empty())
        return {};
    int wlen = MultiByteToWideChar(CP_UTF8, 0, utf8Path.data(), static_cast<int>(utf8Path.size()), nullptr, 0);
    if (wlen <= 0)
        return std::filesystem::path(utf8Path);
    std::wstring wstr(static_cast<size_t>(wlen), L'\0');
    MultiByteToWideChar(CP_UTF8, 0, utf8Path.data(), static_cast<int>(utf8Path.size()), wstr.data(), wlen);
    return std::filesystem::path(std::move(wstr));
#else
    // On Linux / macOS the native encoding is UTF-8 already.
    return std::filesystem::path(utf8Path);
#endif
}

/**
 * @brief Convert a std::filesystem::path to a UTF-8 std::string.
 *
 * On Windows, std::filesystem::path::string() / generic_string() encode
 * using the active code-page (e.g. GBK), NOT UTF-8.  This helper always
 * produces a UTF-8 string so that the result is compatible with ToFsPath()
 * and with Python (which expects UTF-8).
 */
inline std::string FromFsPath(const std::filesystem::path &p)
{
#ifdef INX_PLATFORM_WINDOWS
    std::wstring ws = p.generic_wstring();
    if (ws.empty())
        return {};
    int len = WideCharToMultiByte(CP_UTF8, 0, ws.data(), static_cast<int>(ws.size()), nullptr, 0, nullptr, nullptr);
    if (len <= 0)
        return p.generic_string();
    std::string result(static_cast<size_t>(len), '\0');
    WideCharToMultiByte(CP_UTF8, 0, ws.data(), static_cast<int>(ws.size()), result.data(), len, nullptr, nullptr);
    return result;
#else
    return p.generic_string();
#endif
}

/**
 * @brief Return an absolute, lexically-normal UTF-8 path without consulting disk.
 *
 * Use this for paths that may
 * no longer exist. It deliberately does not
 * resolve symlinks or Windows 8.3 aliases, so its result never changes
 * merely
 * because a file was created or deleted.
 */
inline std::string NormalizeFilesystemPathLexically(const std::string &path)
{
    if (path.empty())
        return {};

    std::error_code error;
    std::filesystem::path normalized = ToFsPath(path);
    if (normalized.is_relative()) {
        auto absolute = std::filesystem::absolute(normalized, error);
        if (!error)
            normalized = std::move(absolute);
    }
    return FromFsPath(normalized.lexically_normal());
}

/**
 * @brief Resolve an existing path to its stable display/storage spelling.
 *
 * Existing aliases and symlinks are
 * resolved when possible. Windows short
 * names are expanded to long names. Missing paths fall back to absolute
 *
 * lexical normalization.
 */
inline std::string ResolveFilesystemPath(const std::string &path)
{
    std::string lexical = NormalizeFilesystemPathLexically(path);
    if (lexical.empty())
        return {};

    std::error_code error;
    std::filesystem::path resolved = ToFsPath(lexical);
    auto canonical = std::filesystem::weakly_canonical(resolved, error);
    if (!error)
        resolved = std::move(canonical);

#ifdef INX_PLATFORM_WINDOWS
    std::filesystem::path existingPrefix = resolved;
    std::vector<std::filesystem::path> missingSuffix;
    while (!existingPrefix.empty() && !std::filesystem::exists(existingPrefix, error)) {
        error.clear();
        const auto parent = existingPrefix.parent_path();
        if (parent == existingPrefix)
            break;
        missingSuffix.push_back(existingPrefix.filename());
        existingPrefix = parent;
    }

    const std::wstring native = existingPrefix.native();
    const DWORD required = GetLongPathNameW(native.c_str(), nullptr, 0);
    if (required > 0) {
        std::wstring expanded(static_cast<size_t>(required), L'\0');
        const DWORD written = GetLongPathNameW(native.c_str(), expanded.data(), required);
        if (written > 0 && written < required) {
            expanded.resize(static_cast<size_t>(written));
            resolved = std::filesystem::path(std::move(expanded));
            for (auto it = missingSuffix.rbegin(); it != missingSuffix.rend(); ++it)
                resolved /= *it;
        }
    }
#endif

    return FromFsPath(resolved.lexically_normal());
}

inline std::string PortablePathFilename(const std::string &path)
{
    return FromFsPath(ToFsPath(NormalizePortablePath(path)).filename());
}

inline std::string PortablePathStem(const std::string &path)
{
    return FromFsPath(ToFsPath(NormalizePortablePath(path)).stem());
}

inline bool TryNormalizePortableRelativePath(const std::string &path, std::string &normalized, bool allowRoot = false)
{
    normalized.clear();
    if (path.empty())
        return false;

    const auto relative = ToFsPath(NormalizePortablePath(path)).lexically_normal();
    if (relative.empty() || relative.is_absolute())
        return false;
    if (relative == ".") {
        if (!allowRoot)
            return false;
        normalized = ".";
        return true;
    }
    if (*relative.begin() == "..")
        return false;
    normalized = NormalizePortablePath(FromFsPath(relative));
    return true;
}

inline std::string FoldFilesystemPathCase(std::string path)
{
#ifdef INX_PLATFORM_WINDOWS
    std::wstring folded = ToFsPath(path).generic_wstring();
    if (!folded.empty())
        CharLowerBuffW(folded.data(), static_cast<DWORD>(folded.size()));
    return FromFsPath(std::filesystem::path(std::move(folded)));
#else
    return path;
#endif
}

inline std::string FilesystemPathKey(const std::string &path)
{
    return FoldFilesystemPathCase(ResolveFilesystemPath(path));
}

/**
 * @brief Return a stable key for a filesystem-backed asset or virtual subresource.
 *
 * Project assets may append an engine-owned ``::kind:id`` suffix to a real
 * filesystem path. Only the backing path participates in filesystem
 * normalization; the virtual identity remains exact and case-sensitive.
 */
inline std::string AssetPathKey(const std::string &path)
{
    const auto virtualSuffix = path.find("::");
    if (virtualSuffix == std::string::npos)
        return FilesystemPathKey(path);
    return FilesystemPathKey(path.substr(0, virtualSuffix)) + path.substr(virtualSuffix);
}

inline std::string LexicalFilesystemPathKey(const std::string &path)
{
    return FoldFilesystemPathCase(NormalizeFilesystemPathLexically(path));
}

inline bool FilesystemPathsEquivalent(const std::string &left, const std::string &right)
{
    if (left.empty() || right.empty())
        return false;
    std::error_code error;
    if (std::filesystem::exists(ToFsPath(left), error) && !error && std::filesystem::exists(ToFsPath(right), error) &&
        !error) {
        const bool equivalent = std::filesystem::equivalent(ToFsPath(left), ToFsPath(right), error);
        if (!error)
            return equivalent;
    }
    return FilesystemPathKey(left) == FilesystemPathKey(right);
}

inline bool IsFilesystemPathWithin(const std::string &path, const std::string &root, bool allowRoot = true)
{
    if (path.empty() || root.empty())
        return false;

    const auto candidate = ToFsPath(FilesystemPathKey(path));
    const auto parent = ToFsPath(FilesystemPathKey(root));
    auto candidatePart = candidate.begin();
    for (auto rootPart = parent.begin(); rootPart != parent.end(); ++rootPart, ++candidatePart) {
        if (candidatePart == candidate.end() || *candidatePart != *rootPart)
            return false;
    }
    return allowRoot || candidatePart != candidate.end();
}

inline bool TryMakeRelativeFilesystemPath(const std::string &path, const std::string &root, std::string &relative,
                                          bool allowRoot = false)
{
    relative.clear();
    if (!IsFilesystemPathWithin(path, root, allowRoot))
        return false;

    const auto candidate = ToFsPath(ResolveFilesystemPath(path));
    const auto parent = ToFsPath(ResolveFilesystemPath(root));
    auto result = candidate.lexically_relative(parent).lexically_normal();
    if (result.empty() || result.is_absolute())
        return false;
    if (result == ".") {
        if (!allowRoot)
            return false;
        relative = ".";
        return true;
    }
    if (*result.begin() == "..")
        return false;
    return TryNormalizePortableRelativePath(FromFsPath(result), relative, allowRoot);
}

/**
 * @brief Read a file into a byte vector, supporting Unicode paths on Windows.
 */
inline bool ReadFileBytes(const std::string &filePath, std::vector<unsigned char> &out)
{
    std::ifstream file(ToFsPath(filePath), std::ios::binary | std::ios::ate);
    if (!file.is_open())
        return false;
    auto size = file.tellg();
    if (size <= 0) {
        out.clear();
        return size == 0;
    }
    out.resize(static_cast<size_t>(size));
    file.seekg(0);
    file.read(reinterpret_cast<char *>(out.data()), size);
    return !file.fail();
}

inline std::ifstream OpenInputFile(const std::string &filePath, std::ios_base::openmode mode = std::ios::in)
{
    return std::ifstream(ToFsPath(filePath), mode);
}

inline std::ofstream OpenOutputFile(const std::string &filePath, std::ios_base::openmode mode = std::ios::out)
{
    return std::ofstream(ToFsPath(filePath), mode);
}

inline std::string JoinPath(std::initializer_list<const char *> parts)
{
    std::filesystem::path path;
    for (const auto &part : parts) {
        path /= ToFsPath(part);
    }
    return FromFsPath(path);
}

inline std::string JoinPath(std::initializer_list<std::string> parts)
{
    std::filesystem::path path;
    for (const auto &part : parts) {
        path /= ToFsPath(part);
    }
    return FromFsPath(path);
}
} // namespace infernux
