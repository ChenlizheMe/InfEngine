#ifdef _WIN32

#include <cassert>
#include <cstdlib>
#include <filesystem>
#include <string>
#include <vector>

#include <tools/launcher/PlayerHost.h>

int main()
{
    using namespace infernux::playerhost;
    const auto layout = ResolveLayout(std::filesystem::path(L"C:/Games/测试/Star.exe"));
    assert(layout.installRoot == std::filesystem::path(L"C:/Games/测试"));
    assert(layout.dataRoot == std::filesystem::path(L"C:/Games/测试/Star_Data"));
    assert(layout.bootstrapArchive == layout.dataRoot / L"Bootstrap.inxrt");
    const auto *localAppDataValue = _wgetenv(L"LOCALAPPDATA");
    assert(localAppDataValue != nullptr);
    const auto localAppData = std::filesystem::path(localAppDataValue);
    assert(layout.warmCacheRoot == localAppData / L"Infernux" / L"PlayerCache" / L"Star");

    const std::string hash(64, 'a');
    assert(CachePath(layout, hash).filename() ==
           std::filesystem::path(L"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"));
    const auto args = BuildPythonArguments(layout.hostExecutable, {L"--scene", L"场景"});
    assert(args.size() == 3);
    assert(args[1] == L"--scene");
    assert(args[2] == L"场景");
    return 0;
}

#else
int main()
{
    return 0;
}
#endif
