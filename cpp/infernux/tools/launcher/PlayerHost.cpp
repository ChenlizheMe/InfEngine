#include "PlayerHost.h"

#include <Python.h>

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifdef _WIN32
#include <shlobj.h>
#include <windows.h>
#else
#include <dlfcn.h>
#include <fcntl.h>
#include <signal.h>
#include <unistd.h>

#include <cerrno>
#include <cstring>
#endif

#include <algorithm>
#include <chrono>
#include <codecvt>
#include <cstdlib>
#include <ctime>
#include <fstream>
#include <iostream>
#include <locale>
#include <sstream>
#include <system_error>
#include <thread>

#include <platform/filesystem/InxPack.h>

namespace infernux::playerhost
{
namespace
{

#ifdef _WIN32
#define INFERNUX_PYTHON_CALL __cdecl
#else
#define INFERNUX_PYTHON_CALL
#endif

using PyConfigInitIsolated = void(INFERNUX_PYTHON_CALL *)(PyConfig *);
using PyConfigSetString = PyStatus(INFERNUX_PYTHON_CALL *)(PyConfig *, wchar_t **, const wchar_t *);
using PyWideStringListAppend = PyStatus(INFERNUX_PYTHON_CALL *)(PyWideStringList *, const wchar_t *);
using PyConfigSetArgv = PyStatus(INFERNUX_PYTHON_CALL *)(PyConfig *, Py_ssize_t, wchar_t *const *);
using PyInitializeFromConfig = PyStatus(INFERNUX_PYTHON_CALL *)(const PyConfig *);
using PyConfigClear = void(INFERNUX_PYTHON_CALL *)(PyConfig *);
using PyRunSimpleStringFlags = int(INFERNUX_PYTHON_CALL *)(const char *, PyCompilerFlags *);
using PyStatusException = int(INFERNUX_PYTHON_CALL *)(PyStatus);

std::filesystem::path UserCachePath()
{
#ifdef _WIN32
    wchar_t buffer[MAX_PATH]{};
    DWORD length = ::GetEnvironmentVariableW(L"LOCALAPPDATA", buffer, ARRAYSIZE(buffer));
    if (length != 0 && length < ARRAYSIZE(buffer))
        return std::filesystem::path(buffer, buffer + length);

    PWSTR knownFolder = nullptr;
    if (SUCCEEDED(::SHGetKnownFolderPath(FOLDERID_LocalAppData, KF_FLAG_DEFAULT, nullptr, &knownFolder))) {
        std::filesystem::path result(knownFolder);
        ::CoTaskMemFree(knownFolder);
        return result;
    }
    return {};
#else
    if (const char *xdgCache = std::getenv("XDG_CACHE_HOME"); xdgCache != nullptr && *xdgCache != '\0')
        return std::filesystem::path(xdgCache);
    if (const char *home = std::getenv("HOME"); home != nullptr && *home != '\0')
        return std::filesystem::path(home) / ".cache";
    return {};
#endif
}

std::wstring ErrorText(const std::wstring &prefix)
{
    std::wstringstream stream;
#ifdef _WIN32
    const DWORD error = ::GetLastError();
    stream << prefix << L" (Windows error " << error << L")";
#else
    stream << prefix << L" (Linux error " << errno << L": " << std::filesystem::path(std::strerror(errno)).wstring()
           << L")";
#endif
    return stream.str();
}

bool IsHexHash(const std::string &value)
{
    if (value.size() != 64)
        return false;
    return std::all_of(value.begin(), value.end(), [](const char value) {
        return (value >= '0' && value <= '9') || (value >= 'a' && value <= 'f') || (value >= 'A' && value <= 'F');
    });
}

std::filesystem::path FindPythonLibrary(const std::filesystem::path &cacheRoot)
{
#define INFERNUX_STRINGIFY_IMPL(value) #value
#define INFERNUX_STRINGIFY(value) INFERNUX_STRINGIFY_IMPL(value)
#ifdef _WIN32
    constexpr const char *pythonLibraryName =
        "python" INFERNUX_STRINGIFY(PY_MAJOR_VERSION) INFERNUX_STRINGIFY(PY_MINOR_VERSION) ".dll";
    const std::filesystem::path candidates[] = {pythonLibraryName, std::filesystem::path("stdlib") / pythonLibraryName};
#else
    constexpr const char *pythonLibraryName =
        "libpython" INFERNUX_STRINGIFY(PY_MAJOR_VERSION) "." INFERNUX_STRINGIFY(PY_MINOR_VERSION) ".so";
    const auto versionedLibraryName = std::filesystem::path(std::string(pythonLibraryName) + ".1.0");
    const std::filesystem::path candidates[] = {versionedLibraryName, pythonLibraryName,
                                                std::filesystem::path("stdlib") / versionedLibraryName,
                                                std::filesystem::path("stdlib") / pythonLibraryName};
#endif
    for (const auto &relative : candidates) {
        const auto candidate = cacheRoot / relative;
        if (std::filesystem::is_regular_file(candidate))
            return candidate;
    }
#undef INFERNUX_STRINGIFY
#undef INFERNUX_STRINGIFY_IMPL
    return {};
}

bool HasPlayerModule(const std::filesystem::path &cacheRoot)
{
    for (const auto &directory : {cacheRoot, cacheRoot / "Infernux" / "lib"}) {
        std::error_code error;
        for (std::filesystem::directory_iterator iterator(directory, error), end; !error && iterator != end;
             iterator.increment(error)) {
            if (!iterator->is_regular_file(error))
                continue;
            const std::string name = iterator->path().filename().string();
            const bool playerPrefix =
                name == "_InfernuxPlayer.pyd" || name == "_InfernuxPlayer.so" || name.rfind("_InfernuxPlayer.", 0) == 0;
#ifdef _WIN32
            const bool nativeSuffix = name.size() >= 4 && name.compare(name.size() - 4, 4, ".pyd") == 0;
#else
            const bool nativeSuffix = name.size() >= 3 && name.compare(name.size() - 3, 3, ".so") == 0;
#endif
            if (playerPrefix && nativeSuffix)
                return true;
        }
    }
    return false;
}

class CacheLock
{
  public:
    explicit CacheLock(const std::filesystem::path &path) : path_(path)
    {
        const auto deadline = std::chrono::steady_clock::now() + std::chrono::seconds(30);
        for (;;) {
#ifdef _WIN32
            handle_ = ::CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                                    FILE_ATTRIBUTE_TEMPORARY, nullptr);
            if (handle_ != INVALID_HANDLE_VALUE) {
#else
            handle_ = ::open(path.c_str(), O_CREAT | O_EXCL | O_RDWR | O_CLOEXEC, 0600);
            if (handle_ >= 0) {
#endif
                WriteMetadata();
                return;
            }
#ifdef _WIN32
            const DWORD error = ::GetLastError();
            if (error != ERROR_FILE_EXISTS && error != ERROR_ALREADY_EXISTS)
                return;
#else
            if (errno != EEXIST)
                return;
#endif
            if (ReclaimStale())
                continue;
            if (std::chrono::steady_clock::now() >= deadline)
                return;
#ifdef _WIN32
            ::Sleep(100);
#else
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
#endif
        }
    }
    ~CacheLock()
    {
#ifdef _WIN32
        if (handle_ != INVALID_HANDLE_VALUE) {
            ::CloseHandle(handle_);
#else
        if (handle_ >= 0) {
            ::close(handle_);
#endif
            std::error_code ignored;
            std::filesystem::remove(path_, ignored);
        }
    }
    CacheLock(const CacheLock &) = delete;
    CacheLock &operator=(const CacheLock &) = delete;
    bool valid() const noexcept
    {
#ifdef _WIN32
        return handle_ != INVALID_HANDLE_VALUE;
#else
        return handle_ >= 0;
#endif
    }

  private:
    void WriteMetadata()
    {
#ifdef _WIN32
        if (handle_ == INVALID_HANDLE_VALUE)
            return;
        const std::string metadata =
            "pid=" + std::to_string(::GetCurrentProcessId()) + "\ntime=" + std::to_string(std::time(nullptr)) + "\n";
        DWORD written = 0;
        ::WriteFile(handle_, metadata.data(), static_cast<DWORD>(metadata.size()), &written, nullptr);
        ::FlushFileBuffers(handle_);
#else
        if (handle_ < 0)
            return;
        const std::string metadata =
            "pid=" + std::to_string(::getpid()) + "\ntime=" + std::to_string(std::time(nullptr)) + "\n";
        const auto ignored = ::write(handle_, metadata.data(), metadata.size());
        (void)ignored;
        ::fsync(handle_);
#endif
    }

    static bool ProcessAlive(unsigned long pid)
    {
        if (pid == 0)
            return false;
#ifdef _WIN32
        HANDLE process = ::OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
        if (process == nullptr)
            return false;
        DWORD exitCode = 0;
        const bool alive = ::GetExitCodeProcess(process, &exitCode) && exitCode == STILL_ACTIVE;
        ::CloseHandle(process);
        return alive;
#else
        return ::kill(static_cast<pid_t>(pid), 0) == 0 || errno == EPERM;
#endif
    }

    bool ReclaimStale()
    {
        std::ifstream input(path_);
        unsigned long pid = 0;
        long long timestamp = 0;
        std::string line;
        while (std::getline(input, line)) {
            if (line.rfind("pid=", 0) == 0)
                pid = std::strtoul(line.c_str() + 4, nullptr, 10);
            else if (line.rfind("time=", 0) == 0)
                timestamp = std::strtoll(line.c_str() + 5, nullptr, 10);
        }
        const auto now = std::time(nullptr);
        if (timestamp == 0 || now - timestamp < 2 || ProcessAlive(pid))
            return false;
        std::filesystem::path stale = path_;
#ifdef _WIN32
        stale += L".stale." + std::to_wstring(::GetCurrentProcessId()) + L"." +
                 std::to_wstring(std::chrono::steady_clock::now().time_since_epoch().count());
#else
        stale += ".stale." + std::to_string(::getpid()) + "." +
                 std::to_string(std::chrono::steady_clock::now().time_since_epoch().count());
#endif
        std::error_code error;
        std::filesystem::rename(path_, stale, error);
        if (error)
            return false;
        std::filesystem::remove(stale, error);
        return true;
    }

#ifdef _WIN32
    HANDLE handle_ = INVALID_HANDLE_VALUE;
#else
    int handle_ = -1;
#endif
    std::filesystem::path path_;
};

bool CacheMarkerMatches(const std::filesystem::path &cache, const std::string &hash)
{
    std::ifstream marker(cache / "cache.complete", std::ios::binary);
    std::string value;
    std::getline(marker, value);
    return !marker.fail() && value == hash && std::filesystem::is_regular_file(FindPythonLibrary(cache)) &&
           HasPlayerModule(cache);
}

bool PublishCache(const std::filesystem::path &staging, const std::filesystem::path &destination)
{
    std::error_code error;
    std::filesystem::rename(staging, destination, error);
    if (!error)
        return true;
    // Another publisher may have won the rename race. Accept it only after
    // the marker and the two executable startup requirements are present.
    return std::filesystem::is_regular_file(destination / "cache.complete") &&
           std::filesystem::is_regular_file(FindPythonLibrary(destination)) && HasPlayerModule(destination);
}

std::wstring WidePath(const std::filesystem::path &path)
{
#ifdef _WIN32
    return path.wstring();
#else
    std::wstring_convert<std::codecvt_utf8<wchar_t>> converter;
    return converter.from_bytes(path.string());
#endif
}

#ifdef _WIN32
void AddSearchDirectory(const std::filesystem::path &path)
{
    if (!std::filesystem::is_directory(path))
        return;
    ::AddDllDirectory(path.c_str());
}
#endif

bool SetEnvironmentPath(const char *name, const std::filesystem::path &value)
{
#ifdef _WIN32
    const std::wstring wideName(name, name + std::char_traits<char>::length(name));
    return ::SetEnvironmentVariableW(wideName.c_str(), value.c_str()) != 0;
#else
    return ::setenv(name, value.c_str(), 1) == 0;
#endif
}

#ifdef _WIN32
bool HasManagedPlayerControlChannel()
{
    return ::GetEnvironmentVariableW(L"_INFERNUX_PLAYER_CONTROL_FILE", nullptr, 0) > 0;
}

std::string Utf8Text(const std::wstring &value)
{
    if (value.empty())
        return {};
    const int size =
        ::WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), nullptr, 0, nullptr, nullptr);
    if (size <= 0)
        return {};
    std::string result(static_cast<size_t>(size), '\0');
    ::WideCharToMultiByte(CP_UTF8, 0, value.data(), static_cast<int>(value.size()), result.data(), size, nullptr,
                          nullptr);
    return result;
}

void ReportManagedPlayerFailure(const std::wstring &message)
{
    const DWORD required = ::GetEnvironmentVariableW(L"_INFERNUX_READY_FILE", nullptr, 0);
    if (required <= 1)
        return;
    std::wstring path(static_cast<size_t>(required), L'\0');
    const DWORD written = ::GetEnvironmentVariableW(L"_INFERNUX_READY_FILE", path.data(), required);
    if (written == 0 || written >= required)
        return;
    path.resize(written);
    std::ofstream stream(std::filesystem::path(path), std::ios::binary | std::ios::trunc);
    stream << "ERROR:" << Utf8Text(message) << '\n';
}
#endif

void ClearPythonEnvironment()
{
#ifdef _WIN32
    for (const wchar_t *name : {L"PYTHONHOME", L"PYTHONPATH", L"PYTHONUSERBASE", L"PYTHONSTARTUP"})
        ::SetEnvironmentVariableW(name, nullptr);
    ::SetEnvironmentVariableW(L"PYTHONNOUSERSITE", L"1");
    ::SetEnvironmentVariableW(L"PYTHONDONTWRITEBYTECODE", L"1");
    ::SetEnvironmentVariableW(L"PYTHONSAFEPATH", L"1");
#else
    for (const char *name : {"PYTHONHOME", "PYTHONPATH", "PYTHONUSERBASE", "PYTHONSTARTUP"})
        ::unsetenv(name);
    ::setenv("PYTHONNOUSERSITE", "1", 1);
    ::setenv("PYTHONDONTWRITEBYTECODE", "1", 1);
    ::setenv("PYTHONSAFEPATH", "1", 1);
#endif
}

} // namespace

Layout ResolveLayout(const std::filesystem::path &hostExecutable)
{
    Layout layout;
    layout.hostExecutable = std::filesystem::absolute(hostExecutable);
    layout.installRoot = layout.hostExecutable.parent_path();
#ifdef _WIN32
    layout.dataRoot = layout.installRoot / (layout.hostExecutable.stem().wstring() + L"_Data");
#else
    layout.dataRoot = layout.installRoot / (layout.hostExecutable.stem().string() + "_Data");
#endif
    layout.bootstrapArchive = layout.dataRoot / "Bootstrap.inxrt";
    const auto userCache = UserCachePath();
    if (userCache.empty())
        throw std::runtime_error("The per-user cache directory is unavailable for the Player cache");
    layout.warmCacheRoot = userCache / "Infernux" / "PlayerCache" / layout.hostExecutable.stem();
    return layout;
}

std::filesystem::path CachePath(const Layout &layout, const std::string &archiveHash)
{
    if (!IsHexHash(archiveHash))
        throw std::invalid_argument("PlayerHost cache identity must be a SHA-256 hash");
    return layout.warmCacheRoot / archiveHash;
}

std::vector<std::wstring> BuildPythonArguments(const std::filesystem::path &hostExecutable,
                                               const std::vector<std::wstring> &gameArguments)
{
    std::vector<std::wstring> result;
    result.reserve(gameArguments.size() + 1);
    result.push_back(hostExecutable.wstring());
    result.insert(result.end(), gameArguments.begin(), gameArguments.end());
    return result;
}

bool PlayerHost::Fail(const std::wstring &message)
{
    state_ = State::Failed;
    error_ = message;
    return false;
}

bool PlayerHost::PrepareCache(const Layout &layout, std::filesystem::path &cacheRoot)
{
    if (!std::filesystem::is_regular_file(layout.bootstrapArchive))
        return Fail(L"Bootstrap.inxrt is missing:\n" + layout.bootstrapArchive.wstring());

    infernux::inxpack::Manifest manifest;
    try {
        manifest = infernux::inxpack::ReadManifest(layout.bootstrapArchive);
    } catch (const std::exception &exception) {
        return Fail(L"Bootstrap.inxrt is damaged or unsupported:\n" +
                    std::filesystem::path(exception.what()).wstring());
    }
    const std::string archiveHash = infernux::inxpack::HashToHex(manifest.archiveHash);
    if (!IsHexHash(archiveHash) || manifest.entries.empty())
        return Fail(L"Bootstrap.inxrt has no valid content hash or entries.");

    try {
        std::filesystem::create_directories(layout.warmCacheRoot);
        cacheRoot = CachePath(layout, archiveHash);
    } catch (const std::exception &exception) {
        return Fail(L"Unable to create the Player warm-cache path:\n" +
                    std::filesystem::path(exception.what()).wstring());
    }

    if (CacheMarkerMatches(cacheRoot, archiveHash)) {
        state_ = State::CacheReady;
        return true;
    }

    const auto lockPath = layout.warmCacheRoot / (archiveHash + ".lock");
    CacheLock lock(lockPath);
    if (!lock.valid() && CacheMarkerMatches(cacheRoot, archiveHash)) {
        state_ = State::CacheReady;
        return true;
    }
    if (!lock.valid())
        return Fail(ErrorText(L"Unable to acquire the Player warm-cache lock"));
    if (CacheMarkerMatches(cacheRoot, archiveHash)) {
        state_ = State::CacheReady;
        return true;
    }

    std::error_code cleanupError;
    std::filesystem::remove_all(cacheRoot, cleanupError);
    const auto staging =
        layout.warmCacheRoot / (archiveHash + ".staging." +
#ifdef _WIN32
                                std::to_string(::GetCurrentProcessId()) + "." +
#else
                                std::to_string(::getpid()) + "." +
#endif
                                std::to_string(std::chrono::steady_clock::now().time_since_epoch().count()));
    std::filesystem::remove_all(staging, cleanupError);
    try {
        infernux::inxpack::Extract(layout.bootstrapArchive, staging);
        std::ofstream marker(staging / "cache.complete", std::ios::binary | std::ios::trunc);
        marker << archiveHash << '\n';
        marker.flush();
        if (!marker) {
            std::filesystem::remove_all(staging, cleanupError);
            return Fail(L"Unable to write the Player warm-cache completion marker.");
        }
        marker.close();
        if (!PublishCache(staging, cacheRoot)) {
            std::filesystem::remove_all(staging, cleanupError);
            return Fail(ErrorText(L"Unable to atomically publish the Player warm cache"));
        }
    } catch (const std::exception &exception) {
        std::filesystem::remove_all(staging, cleanupError);
        return Fail(L"Unable to extract Bootstrap.inxrt:\n" + std::filesystem::path(exception.what()).wstring());
    }

    if (!CacheMarkerMatches(cacheRoot, archiveHash))
        return Fail(L"The published Player warm cache failed validation.");
    state_ = State::CacheReady;
    return true;
}

bool PlayerHost::LoadPython(const Layout &layout, const std::filesystem::path &cacheRoot)
{
    if (!HasPlayerModule(cacheRoot))
        return Fail(L"Bootstrap.inxrt does not contain the _InfernuxPlayer module.");
    const auto pythonLibrary = FindPythonLibrary(cacheRoot);
    if (pythonLibrary.empty())
        return Fail(L"Bootstrap.inxrt does not contain the configured CPython shared library in the warm cache.");

#ifdef _WIN32
    if (!::SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS | LOAD_LIBRARY_SEARCH_USER_DIRS))
        return Fail(ErrorText(L"Unable to configure the secure Player DLL search policy"));
    AddSearchDirectory(cacheRoot);
    AddSearchDirectory(pythonLibrary.parent_path());
    AddSearchDirectory(cacheRoot / "Infernux" / "lib");
#endif

    if (!SetEnvironmentPath("_INFERNUX_PLAYER_INSTALL_ROOT", layout.installRoot) ||
        !SetEnvironmentPath("_INFERNUX_PLAYER_DATA_ROOT", layout.dataRoot) ||
        !SetEnvironmentPath("_INFERNUX_PLAYER_RUNTIME_ROOT", cacheRoot) ||
        !SetEnvironmentPath("INFERNUX_NATIVE_MODULE_DIR", cacheRoot / "Infernux" / "lib"))
        return Fail(ErrorText(L"Unable to configure the Player runtime environment"));
    // The isolated PyConfig below owns all import paths. Remove inherited
    // Python environment variables before loading any extension module.
    ClearPythonEnvironment();

#ifdef _WIN32
    HMODULE python = ::LoadLibraryExW(pythonLibrary.c_str(), nullptr,
                                      LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (python == nullptr)
        return Fail(ErrorText(L"Unable to load the CPython shared library from the warm cache"));
#else
    void *python = ::dlopen(pythonLibrary.c_str(), RTLD_NOW | RTLD_GLOBAL);
    if (python == nullptr) {
        const char *detail = ::dlerror();
        return Fail(L"Unable to load the CPython shared library from the warm cache: " +
                    std::filesystem::path(detail != nullptr ? detail : "unknown loader error").wstring());
    }
#endif
    pythonModule_ = python;
    state_ = State::PythonLoaded;
    return true;
}

int PlayerHost::ExecuteModule(const Layout &layout, const std::filesystem::path &cacheRoot,
                              const std::vector<std::wstring> &gameArguments)
{
#ifdef _WIN32
    auto *python = static_cast<HMODULE>(pythonModule_);
    const auto resolve = [python](const char *name) -> FARPROC { return ::GetProcAddress(python, name); };
#else
    void *python = pythonModule_;
    const auto resolve = [python](const char *name) -> void * { return ::dlsym(python, name); };
#endif
    const auto initIsolated = reinterpret_cast<PyConfigInitIsolated>(resolve("PyConfig_InitIsolatedConfig"));
    const auto setString = reinterpret_cast<PyConfigSetString>(resolve("PyConfig_SetString"));
    const auto appendPath = reinterpret_cast<PyWideStringListAppend>(resolve("PyWideStringList_Append"));
    const auto setConfigArgv = reinterpret_cast<PyConfigSetArgv>(resolve("PyConfig_SetArgv"));
    const auto initialize = reinterpret_cast<PyInitializeFromConfig>(resolve("Py_InitializeFromConfig"));
    const auto clearConfig = reinterpret_cast<PyConfigClear>(resolve("PyConfig_Clear"));
    const auto runSimple = reinterpret_cast<PyRunSimpleStringFlags>(resolve("PyRun_SimpleStringFlags"));
    const auto statusException = reinterpret_cast<PyStatusException>(resolve("PyStatus_Exception"));
    if (initIsolated == nullptr || setString == nullptr || appendPath == nullptr || setConfigArgv == nullptr ||
        initialize == nullptr || clearConfig == nullptr || runSimple == nullptr || statusException == nullptr) {
        Fail(L"The CPython shared library does not export the required isolated PyConfig API.");
        return 3;
    }

    PyConfig config;
    initIsolated(&config);
    config.isolated = 1;
    config.use_environment = 0;
    config.user_site_directory = 0;
    config.site_import = 0;
    config.write_bytecode = 0;
    config.safe_path = 1;
    config.parse_argv = 0;
    config.module_search_paths_set = 1;

    const auto failStatus = [this, &clearConfig, &config](PyStatus status, const wchar_t *context) {
        clearConfig(&config);
        std::wstring message = context;
        if (status.err_msg != nullptr)
            message += L": " + std::filesystem::path(status.err_msg).wstring();
        return Fail(message);
    };
    const std::wstring programName = WidePath(layout.hostExecutable);
    PyStatus status = setString(&config, &config.program_name, programName.c_str());
    if (statusException(status)) {
        failStatus(status, L"Unable to configure isolated Player program name");
        return 3;
    }
    const std::vector<std::filesystem::path> searchPaths = {
        cacheRoot,
        cacheRoot / "stdlib",
        cacheRoot / "Infernux" / "lib",
    };
    for (const auto &path : searchPaths) {
        const std::wstring widePath = WidePath(path);
        status = appendPath(&config.module_search_paths, widePath.c_str());
        if (statusException(status)) {
            failStatus(status, L"Unable to configure isolated Player module path");
            return 3;
        }
    }
    const auto arguments = BuildPythonArguments(layout.hostExecutable, gameArguments);
    std::vector<std::vector<wchar_t>> storage;
    storage.reserve(arguments.size());
    for (const auto &argument : arguments)
        storage.emplace_back(argument.begin(), argument.end());
    std::vector<wchar_t *> argv;
    argv.reserve(storage.size());
    for (auto &argument : storage) {
        argument.push_back(L'\0');
        argv.push_back(argument.data());
    }
    status = setConfigArgv(&config, static_cast<Py_ssize_t>(argv.size()), argv.data());
    if (statusException(status)) {
        failStatus(status, L"Unable to configure isolated Player arguments");
        return 3;
    }
    status = initialize(&config);
    clearConfig(&config);
    if (statusException(status)) {
        Fail(L"Unable to initialize isolated CPython Player runtime.");
        return 3;
    }

    const int result = runSimple("import _InfernuxPlayer", nullptr);
    state_ = State::ModuleExecuted;
    return result;
}

int PlayerHost::Run(const Layout &layout, const std::vector<std::wstring> &gameArguments)
{
    state_ = State::LayoutResolved;
    std::filesystem::path cacheRoot;
    if (!PrepareCache(layout, cacheRoot) || !LoadPython(layout, cacheRoot)) {
#ifdef _WIN32
        if (HasManagedPlayerControlChannel()) {
            ReportManagedPlayerFailure(error_);
            std::wcerr << L"Infernux PlayerHost: " << error_ << std::endl;
        } else {
            ::MessageBoxW(nullptr, error_.c_str(), L"Infernux PlayerHost", MB_OK | MB_ICONERROR);
        }
#else
        std::wcerr << L"Infernux PlayerHost: " << error_ << std::endl;
#endif
        return 2;
    }
    const int result = ExecuteModule(layout, cacheRoot, gameArguments);
    if (state_ != State::Failed)
        state_ = State::Exited;
    if (state_ == State::Failed) {
#ifdef _WIN32
        if (HasManagedPlayerControlChannel()) {
            ReportManagedPlayerFailure(error_);
            std::wcerr << L"Infernux PlayerHost: " << error_ << std::endl;
        } else {
            ::MessageBoxW(nullptr, error_.c_str(), L"Infernux PlayerHost", MB_OK | MB_ICONERROR);
        }
#else
        std::wcerr << L"Infernux PlayerHost: " << error_ << std::endl;
#endif
    }
    return result;
}

} // namespace infernux::playerhost
