#include "PlayerHost.h"

#include <Python.h>

#ifndef NOMINMAX
#define NOMINMAX
#endif
#ifdef _WIN32
#include <windows.h>
#else
#include <dlfcn.h>

#include <cerrno>
#include <cstring>
#endif

#include <codecvt>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <locale>
#include <sstream>
#include <system_error>

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
using PyPreConfigInitIsolated = void(INFERNUX_PYTHON_CALL *)(PyPreConfig *);
using PyPreInitialize = PyStatus(INFERNUX_PYTHON_CALL *)(const PyPreConfig *);
using PyConfigSetString = PyStatus(INFERNUX_PYTHON_CALL *)(PyConfig *, wchar_t **, const wchar_t *);
using PyWideStringListAppend = PyStatus(INFERNUX_PYTHON_CALL *)(PyWideStringList *, const wchar_t *);
using PyConfigSetArgv = PyStatus(INFERNUX_PYTHON_CALL *)(PyConfig *, Py_ssize_t, wchar_t *const *);
using PyInitializeFromConfig = PyStatus(INFERNUX_PYTHON_CALL *)(const PyConfig *);
using PyConfigClear = void(INFERNUX_PYTHON_CALL *)(PyConfig *);
using PyRunSimpleStringFlags = int(INFERNUX_PYTHON_CALL *)(const char *, PyCompilerFlags *);
using PyStatusException = int(INFERNUX_PYTHON_CALL *)(PyStatus);

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
    layout.runtimeRoot = layout.dataRoot / "Runtime";
    return layout;
}

std::vector<std::wstring> BuildPythonArguments(const std::filesystem::path &hostExecutable,
                                               const std::vector<std::wstring> &gameArguments)
{
    std::vector<std::wstring> result;
    result.reserve(gameArguments.size() + 1);
    result.push_back(WidePath(hostExecutable));
    result.insert(result.end(), gameArguments.begin(), gameArguments.end());
    return result;
}

bool PlayerHost::Fail(const std::wstring &message)
{
    state_ = State::Failed;
    error_ = message;
    return false;
}

bool PlayerHost::PrepareRuntime(const Layout &layout)
{
    if (!std::filesystem::is_directory(layout.runtimeRoot))
        return Fail(L"The Player Runtime directory is missing:\n" + layout.runtimeRoot.wstring());
    if (!HasPlayerModule(layout.runtimeRoot))
        return Fail(L"The Player Runtime does not contain the _InfernuxPlayer module.");
    if (FindPythonLibrary(layout.runtimeRoot).empty())
        return Fail(L"The Player Runtime does not contain the configured CPython shared library.");
    state_ = State::RuntimeReady;
    return true;
}

bool PlayerHost::LoadPython(const Layout &layout)
{
    const auto pythonLibrary = FindPythonLibrary(layout.runtimeRoot);

#ifdef _WIN32
    if (!::SetDefaultDllDirectories(LOAD_LIBRARY_SEARCH_DEFAULT_DIRS | LOAD_LIBRARY_SEARCH_USER_DIRS))
        return Fail(ErrorText(L"Unable to configure the secure Player DLL search policy"));
    AddSearchDirectory(layout.runtimeRoot);
    AddSearchDirectory(pythonLibrary.parent_path());
    AddSearchDirectory(layout.runtimeRoot / "Infernux" / "lib");
#endif

    if (!SetEnvironmentPath("_INFERNUX_PLAYER_INSTALL_ROOT", layout.installRoot) ||
        !SetEnvironmentPath("_INFERNUX_PLAYER_DATA_ROOT", layout.dataRoot) ||
        !SetEnvironmentPath("_INFERNUX_PLAYER_RUNTIME_ROOT", layout.runtimeRoot) ||
        !SetEnvironmentPath("INFERNUX_NATIVE_MODULE_DIR", layout.runtimeRoot / "Infernux" / "lib"))
        return Fail(ErrorText(L"Unable to configure the Player runtime environment"));
    // The isolated PyConfig below owns all import paths. Remove inherited
    // Python environment variables before loading any extension module.
    ClearPythonEnvironment();

#ifdef _WIN32
    HMODULE python = ::LoadLibraryExW(pythonLibrary.c_str(), nullptr,
                                      LOAD_LIBRARY_SEARCH_DLL_LOAD_DIR | LOAD_LIBRARY_SEARCH_DEFAULT_DIRS);
    if (python == nullptr)
        return Fail(ErrorText(L"Unable to load the CPython shared library from the Player Runtime"));
#else
    void *python = ::dlopen(pythonLibrary.c_str(), RTLD_NOW | RTLD_GLOBAL);
    if (python == nullptr) {
        const char *detail = ::dlerror();
        return Fail(L"Unable to load the CPython shared library from the Player Runtime: " +
                    std::filesystem::path(detail != nullptr ? detail : "unknown loader error").wstring());
    }
#endif
    pythonModule_ = python;
    state_ = State::PythonLoaded;
    return true;
}

int PlayerHost::ExecuteModule(const Layout &layout, const std::vector<std::wstring> &gameArguments)
{
#ifdef _WIN32
    auto *python = static_cast<HMODULE>(pythonModule_);
    const auto resolve = [python](const char *name) -> FARPROC { return ::GetProcAddress(python, name); };
#else
    void *python = pythonModule_;
    const auto resolve = [python](const char *name) -> void * { return ::dlsym(python, name); };
#endif
    const auto initIsolated = reinterpret_cast<PyConfigInitIsolated>(resolve("PyConfig_InitIsolatedConfig"));
    const auto initPreIsolated = reinterpret_cast<PyPreConfigInitIsolated>(resolve("PyPreConfig_InitIsolatedConfig"));
    const auto preInitialize = reinterpret_cast<PyPreInitialize>(resolve("Py_PreInitialize"));
    const auto setString = reinterpret_cast<PyConfigSetString>(resolve("PyConfig_SetString"));
    const auto appendPath = reinterpret_cast<PyWideStringListAppend>(resolve("PyWideStringList_Append"));
    const auto setConfigArgv = reinterpret_cast<PyConfigSetArgv>(resolve("PyConfig_SetArgv"));
    const auto initialize = reinterpret_cast<PyInitializeFromConfig>(resolve("Py_InitializeFromConfig"));
    const auto clearConfig = reinterpret_cast<PyConfigClear>(resolve("PyConfig_Clear"));
    const auto runSimple = reinterpret_cast<PyRunSimpleStringFlags>(resolve("PyRun_SimpleStringFlags"));
    const auto statusException = reinterpret_cast<PyStatusException>(resolve("PyStatus_Exception"));
    if (initIsolated == nullptr || initPreIsolated == nullptr || preInitialize == nullptr || setString == nullptr ||
        appendPath == nullptr || setConfigArgv == nullptr || initialize == nullptr || clearConfig == nullptr ||
        runSimple == nullptr || statusException == nullptr) {
        Fail(L"The CPython shared library does not export the required isolated PyConfig API.");
        return 3;
    }

    PyPreConfig preconfig;
    initPreIsolated(&preconfig);
    preconfig.utf8_mode = 1;
    PyStatus status = preInitialize(&preconfig);
    if (statusException(status)) {
        Fail(L"Unable to preinitialize isolated CPython Player runtime in UTF-8 mode.");
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
    status = setString(&config, &config.program_name, programName.c_str());
    if (statusException(status)) {
        failStatus(status, L"Unable to configure isolated Player program name");
        return 3;
    }
    const std::vector<std::filesystem::path> searchPaths = {
        layout.runtimeRoot,
        layout.runtimeRoot / "stdlib",
        layout.runtimeRoot / "Infernux" / "lib",
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
    if (!PrepareRuntime(layout) || !LoadPython(layout)) {
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
    const int result = ExecuteModule(layout, gameArguments);
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
