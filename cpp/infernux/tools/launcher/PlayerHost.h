#pragma once

#ifdef _WIN32

#include <filesystem>
#include <string>
#include <vector>

namespace infernux::playerhost
{

enum class State
{
    Created,
    LayoutResolved,
    ArchiveValidated,
    CacheReady,
    PythonLoaded,
    ModuleExecuted,
    Exited,
    Failed,
};

struct Layout
{
    std::filesystem::path hostExecutable;
    std::filesystem::path installRoot;
    std::filesystem::path dataRoot;
    std::filesystem::path bootstrapArchive;
    std::filesystem::path warmCacheRoot;
};

Layout ResolveLayout(const std::filesystem::path &hostExecutable);
std::filesystem::path CachePath(const Layout &layout, const std::string &archiveHash);
std::vector<std::wstring> BuildPythonArguments(const std::filesystem::path &hostExecutable,
                                               const std::vector<std::wstring> &gameArguments);

class PlayerHost
{
  public:
    int Run(const Layout &layout, const std::vector<std::wstring> &gameArguments);

    State state() const noexcept
    {
        return state_;
    }

    const std::wstring &error() const noexcept
    {
        return error_;
    }

  private:
    bool Fail(const std::wstring &message);
    bool PrepareCache(const Layout &layout, std::filesystem::path &cacheRoot);
    bool LoadPython(const Layout &layout, const std::filesystem::path &cacheRoot);
    int ExecuteModule(const Layout &layout, const std::filesystem::path &cacheRoot,
                      const std::vector<std::wstring> &gameArguments);

    State state_ = State::Created;
    std::wstring error_;
    void *pythonModule_ = nullptr;
};

} // namespace infernux::playerhost

#endif // _WIN32
