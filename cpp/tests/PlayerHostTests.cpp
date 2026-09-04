#include <cassert>
#include <filesystem>
#include <string>
#include <vector>

#include <tools/launcher/PlayerHost.h>

int main()
{
    using namespace infernux::playerhost;
#ifdef _WIN32
    const auto layout = ResolveLayout(std::filesystem::path(L"C:/Games/测试/Star.exe"));
    assert(layout.installRoot == std::filesystem::path(L"C:/Games/测试"));
    assert(layout.dataRoot == std::filesystem::path(L"C:/Games/测试/Star_Data"));
    assert(layout.runtimeRoot == layout.dataRoot / L"Runtime");
#else
    const auto layout = ResolveLayout(std::filesystem::path("/opt/games/Star"));
    assert(layout.installRoot == std::filesystem::path("/opt/games"));
    assert(layout.dataRoot == std::filesystem::path("/opt/games/Star_Data"));
    assert(layout.runtimeRoot == layout.dataRoot / "Runtime");
    const auto unicodeArgs = BuildPythonArguments(std::filesystem::u8path(u8"/home/player/桌面/Star"), {});
    assert(unicodeArgs == std::vector<std::wstring>{L"/home/player/桌面/Star"});
#endif
    const auto args = BuildPythonArguments(layout.hostExecutable, {L"--scene", L"场景"});
    assert(args.size() == 3);
    assert(args[1] == L"--scene");
    assert(args[2] == L"场景");
    return 0;
}
