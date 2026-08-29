#include <cassert>
#include <cstdlib>
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
    assert(layout.bootstrapArchive == layout.dataRoot / L"Bootstrap.inxrt");
    const auto *localAppDataValue = _wgetenv(L"LOCALAPPDATA");
    assert(localAppDataValue != nullptr);
    const auto localAppData = std::filesystem::path(localAppDataValue);
    assert(layout.warmCacheRoot == localAppData / L"Infernux" / L"PlayerCache" / L"Star");
#else
    const auto layout = ResolveLayout(std::filesystem::path("/opt/games/Star"));
    assert(layout.installRoot == std::filesystem::path("/opt/games"));
    assert(layout.dataRoot == std::filesystem::path("/opt/games/Star_Data"));
    assert(layout.bootstrapArchive == layout.dataRoot / "Bootstrap.inxrt");
    const char *xdgCacheValue = std::getenv("XDG_CACHE_HOME");
    const char *homeValue = std::getenv("HOME");
    assert((xdgCacheValue != nullptr && *xdgCacheValue != '\0') || (homeValue != nullptr && *homeValue != '\0'));
    const auto userCache = xdgCacheValue != nullptr && *xdgCacheValue != '\0'
                               ? std::filesystem::path(xdgCacheValue)
                               : std::filesystem::path(homeValue) / ".cache";
    assert(layout.warmCacheRoot == userCache / "Infernux" / "PlayerCache" / "Star");
#endif

    const std::string hash(64, 'a');
    assert(CachePath(layout, hash).filename() ==
           std::filesystem::path(L"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"));
    const auto args = BuildPythonArguments(layout.hostExecutable, {L"--scene", L"场景"});
    assert(args.size() == 3);
    assert(args[1] == L"--scene");
    assert(args[2] == L"场景");
    return 0;
}
