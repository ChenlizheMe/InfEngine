#include "PlayerHost.h"

#ifdef _WIN32

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <Python.h>
#include <shlobj.h>
#include <windows.h>

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <ctime>
#include <fstream>
#include <sstream>
#include <system_error>
#include <thread>

#include <platform/filesystem/InxPack.h>

namespace infernux::playerhost
{
namespace
{

using PyConfigInitIsolated = void(__cdecl *)(PyConfig *);
using PyConfigSetString = PyStatus(__cdecl *)(PyConfig *, wchar_t **, const wchar_t *);
using PyWideStringListAppend = PyStatus(__cdecl *)(PyWideStringList *, const wchar_t *);
using PyConfigSetArgv = PyStatus(__cdecl *)(PyConfig *, Py_ssize_t, wchar_t *const *);
using PyInitializeFromConfig = PyStatus(__cdecl *)(const PyConfig *);
using PyConfigClear = void(__cdecl *)(PyConfig *);
using PyRunSimpleStringFlags = int(__cdecl *)(const char *, PyCompilerFlags *);
using PyStatusException = int(__cdecl *)(PyStatus);

std::filesystem::path LocalAppDataPath()
{
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
}

std::wstring ErrorText(const std::wstring &prefix, DWORD error = ::GetLastError())
{
    std::wstringstream stream;
    stream << prefix << L" (Windows error " << error << L")";
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

std::filesystem::path FindPythonDll(const std::filesystem::path &cacheRoot)
{
    for (const auto &candidate : {cacheRoot / L"python312.dll", cacheRoot / L"stdlib" / L"python312.dll"}) {
        if (std::filesystem::is_regular_file(candidate))
            return candidate;
    }
    return {};
}

bool HasPlayerModule(const std::filesystem::path &cacheRoot)
{
    for (const auto &directory : {cacheRoot, cacheRoot / L"Infernux" / L"lib"}) {
        std::error_code error;
        for (std::filesystem::directory_iterator iterator(directory, error), end; !error && iterator != end;
             iterator.increment(error)) {
            if (!iterator->is_regular_file(error))
                continue;
            const std::wstring name = iterator->path().filename().wstring();
            const bool playerPrefix = name == L"_InfernuxPlayer.pyd" || name.rfind(L"_InfernuxPlayer.", 0) == 0;
            const bool pydSuffix = name.size() >= 4 && name.compare(name.size() - 4, 4, L".pyd") == 0;
            if (playerPrefix && pydSuffix)
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
            handle_ = ::CreateFileW(path.c_str(), GENERIC_READ | GENERIC_WRITE, 0, nullptr, CREATE_NEW,
                                    FILE_ATTRIBUTE_TEMPORARY, nullptr);
            if (handle_ != INVALID_HANDLE_VALUE) {
                WriteMetadata();
                return;
            }
            const DWORD error = ::GetLastError();
            if (error != ERROR_FILE_EXISTS && error != ERROR_ALREADY_EXISTS)
                return;
            if (ReclaimStale())
                continue;
            if (std::chrono::steady_clock::now() >= deadline)
                return;
            ::Sleep(100);
        }
    }
    ~CacheLock()
    {
        if (handle_ != INVALID_HANDLE_VALUE) {
            ::CloseHandle(handle_);
            std::error_code ignored;
            std::filesystem::remove(path_, ignored);
        }
    }
    CacheLock(const CacheLock &) = delete;
    CacheLock &operator=(const CacheLock &) = delete;
    bool valid() const noexcept
    {
        return handle_ != INVALID_HANDLE_VALUE;
    }

  private:
    void WriteMetadata()
    {
        if (handle_ == INVALID_HANDLE_VALUE)
            return;
        const std::string metadata =
            "pid=" + std::to_string(::GetCurrentProcessId()) + "\ntime=" + std::to_string(std::time(nullptr)) + "\n";
        DWORD written = 0;
        ::WriteFile(handle_, metadata.data(), static_cast<DWORD>(metadata.size()), &written, nullptr);
        ::FlushFileBuffers(handle_);
    }

    static bool ProcessAlive(DWORD pid)
    {
        if (pid == 0)
            return false;
        HANDLE process = ::OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, FALSE, pid);
        if (process == nullptr)
            return false;
        DWORD exitCode = 0;
        const bool alive = ::GetExitCodeProcess(process, &exitCode) && exitCode == STILL_ACTIVE;
        ::CloseHandle(process);
        return alive;
    }

    bool ReclaimStale()
    {
        std::ifstream input(path_);
        DWORD pid = 0;
        long long timestamp = 0;
        std::string line;
        while (std::getline(input, line)) {
            if (line.rfind("pid=", 0) == 0)
                pid = static_cast<DWORD>(std::strtoul(line.c_str() + 4, nullptr, 10));
            else if (line.rfind("time=", 0) == 0)
                timestamp = std::strtoll(line.c_str() + 5, nullptr, 10);
        }
        const auto now = std::time(nullptr);
        if (timestamp == 0 || now - timestamp < 2 || ProcessAlive(pid))
            return false;
        std::filesystem::path stale = path_;
        stale += L".stale." + std::to_wstring(::GetCurrentProcessId()) + L"." +
                 std::to_wstring(std::chrono::steady_clock::now().time_since_epoch().count());
        std::error_code error;
        std::filesystem::rename(path_, stale, error);
        if (error)
            return false;
        std::filesystem::remove(stale, error);
        return true;
    }

    HANDLE handle_ = INVALID_HANDLE_VALUE;
    std::filesystem::path path_;
};

bool CacheMarkerMatches(const std::filesystem::path &cache, const std::string &hash)
{
    std::ifstream marker(cache / L"cache.complete", std::ios::binary);
    std::string value;
    std::getline(marker, value);
    return !marker.fail() && value == hash && std::filesystem::is_regular_file(FindPythonDll(cache)) &&
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
    return std::filesystem::is_regular_file(destination / L"cache.complete") &&
           std::filesystem::is_regular_file(FindPythonDll(destination)) && HasPlayerModule(destination);
}

void AddSearchDirectory(const std::filesystem::path &path)
{
    if (!std::filesystem::is_directory(path))
        return;
    ::AddDllDirectory(path.c_str());
}

} // namespace

Layout ResolveLayout(const std::filesystem::path &hostExecutable)
{
    Layout layout;
    layout.hostExecutable = std::filesystem::absolute(hostExecutable);
    layout.installRoot = layout.hostExecutable.parent_path();
    layout.dataRoot = layout.installRoot / (layout.hostExecutable.stem().wstring() + L"_Data");
    layout.bootstrapArchive = layout.dataRoot / L"Bootstrap.inxrt";
    const auto localAppData = LocalAppDataPath();
    if (localAppData.empty())
        throw std::runtime_error("LOCALAPPDATA is unavailable for the Player cache");
    layout.warmCacheRoot = localAppData / L"Infernux" / L"PlayerCache" / layout.hostExecutable.stem();
    return layout;
}

std::filesystem::path CachePath(const Layout &layout, const std::string &archiveHash)
{
    if (!IsHexHash(archiveHash))
        throw std::invalid_argument("PlayerHost cache identity must be a SHA-256 hash");
    return layout.warmCacheRoot / std::filesystem::path(std::wstring(archiveHash.begin(), archiveHash.end()));
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

    const auto lockPath = layout.warmCacheRoot / (std::wstring(archiveHash.begin(), archiveHash.end()) + L".lock");
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
        layout.warmCacheRoot / (std::wstring(archiveHash.begin(), archiveHash.end()) + L".staging." +
                                std::to_wstring(::GetCurrentProcessId()) + L"." +
                                std::to_wstring(std::chrono::steady_clock::now().time_since_epoch().count()));
    std::filesystem::remove_all(staging, cleanupError);
    try {
        infernux::inxpack::Extract(layout.bootstrapArchive, staging);
        std::ofstream marker(staging / L"cache.complete", std::ios::binary | std::ios::trunc);
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
    const auto pythonDll = FindPythonDll(cacheRoot);
    if (pythonDll.empty())
        return Fail(L"Bootstrap.inxrt does not contain python312.dll in the warm cache.");

    if (!::SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS | LOAD_LIBRARY_SEARCH_USER_DIRS))
        return Fail(ErrorText(L"Unable to configure the secure Player DLL search policy"));
    AddSearchDirectory(cacheRoot);
    AddSearchDirectory(pythonDll.parent_path());
    AddSearchDirectory(cacheRoot / L"Infernux" / L"lib");

    ::SetEnvironmentVariableW(L"_INFERNUX_PLAYER_INSTALL_ROOT", layout.installRoot.c_str());
    ::SetEnvironmentVariableW(L"_INFERNUX_PLAYER_DATA_ROOT", layout.dataRoot.c_str());
    ::SetEnvironmentVariableW(L"_INFERNUX_PLAYER_RUNTIME_ROOT", cacheRoot.c_str());
    ::SetEnvironmentVariableW(L"INFERNUX_NATIVE_MODULE_DIR", (cacheRoot / L"Infernux" / L"lib").c_str());
    // The isolated PyConfig below owns all import paths. Remove inherited
    // Python environment variables before loading any extension module.
    for (const wchar_t *name : {L"PYTHONHOME", L"PYTHONPATH", L"PYTHONUSERBASE", L"PYTHONSTARTUP"})
        ::SetEnvironmentVariableW(name, nullptr);
    ::SetEnvironmentVariableW(L"PYTHONNOUSERSITE", L"1");
    ::SetEnvironmentVariableW(L"PYTHONDONTWRITEBYTECODE", L"1");
    ::SetEnvironmentVariableW(L"PYTHONSAFEPATH", L"1");

    HMODULE python = ::LoadLibraryExW(pythonDll.c_str(), nullptr,
                                      LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (python == nullptr)
        return Fail(ErrorText(L"Unable to load python312.dll from the warm cache"));
    pythonModule_ = python;
    state_ = State::PythonLoaded;
    return true;
}

int PlayerHost::ExecuteModule(const Layout &layout, const std::filesystem::path &cacheRoot,
                              const std::vector<std::wstring> &gameArguments)
{
    auto *python = static_cast<HMODULE>(pythonModule_);
    const auto resolve = [python](const char *name) -> FARPROC { return ::GetProcAddress(python, name); };
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
        Fail(L"python312.dll does not export the required isolated PyConfig API.");
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
    PyStatus status = setString(&config, &config.program_name, layout.hostExecutable.c_str());
    if (statusException(status)) {
        failStatus(status, L"Unable to configure isolated Player program name");
        return 3;
    }
    const std::vector<std::filesystem::path> searchPaths = {
        cacheRoot,
        cacheRoot / L"stdlib",
        cacheRoot / L"Infernux" / L"lib",
    };
    for (const auto &path : searchPaths) {
        status = appendPath(&config.module_search_paths, path.c_str());
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
        ::MessageBoxW(nullptr, error_.c_str(), L"Infernux PlayerHost", MB_OK | MB_ICONERROR);
        return 2;
    }
    const int result = ExecuteModule(layout, cacheRoot, gameArguments);
    if (state_ != State::Failed)
        state_ = State::Exited;
    if (state_ == State::Failed)
        ::MessageBoxW(nullptr, error_.c_str(), L"Infernux PlayerHost", MB_OK | MB_ICONERROR);
    return result;
}

} // namespace infernux::playerhost

#endif // _WIN32
