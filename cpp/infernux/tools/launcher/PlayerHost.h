#pragma once

#include <filesystem>
#include <string>
#include <vector>

namespace infernux::playerhost
{

enum class State
{
    Created,
    LayoutResolved,
    RuntimeReady,
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
    std::filesystem::path runtimeRoot;
};

Layout ResolveLayout(const std::filesystem::path &hostExecutable);
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
    bool PrepareRuntime(const Layout &layout);
    bool LoadPython(const Layout &layout);
    int ExecuteModule(const Layout &layout, const std::vector<std::wstring> &gameArguments);

    State state_ = State::Created;
    std::wstring error_;
    void *pythonModule_ = nullptr;
};

} // namespace infernux::playerhost
