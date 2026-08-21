#ifdef _WIN32

#ifndef NOMINMAX
#define NOMINMAX
#endif
#include <shellapi.h>
#include <windows.h>

#include "PlayerHost.h"

int WINAPI wWinMain(HINSTANCE, HINSTANCE, PWSTR arguments, int)
{
    int argc = 0;
    LPWSTR *argv = CommandLineToArgvW(GetCommandLineW(), &argc);
    if (argv == nullptr)
        return 2;
    std::vector<std::wstring> gameArguments;
    for (int index = 1; index < argc; ++index)
        gameArguments.emplace_back(argv[index]);
    LocalFree(argv);

    std::vector<wchar_t> executableBuffer(32768, L'\0');
    const DWORD length =
        GetModuleFileNameW(nullptr, executableBuffer.data(), static_cast<DWORD>(executableBuffer.size()));
    if (length == 0 || length >= executableBuffer.size())
        return 2;
    const auto layout =
        infernux::playerhost::ResolveLayout(std::filesystem::path(std::wstring(executableBuffer.data(), length)));
    infernux::playerhost::PlayerHost host;
    return host.Run(layout, gameArguments);
}

#endif
