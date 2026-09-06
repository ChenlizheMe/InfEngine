#ifdef _WIN32

#ifndef NOMINMAX
#define NOMINMAX
#endif
// shellapi.h depends on declarations supplied by windows.h.
// clang-format off
#include <windows.h>
#include <shellapi.h>
// clang-format on

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

#else

#include "PlayerHost.h"

#include <codecvt>
#include <locale>

int main(int argc, char **argv)
{
    std::error_code error;
    const auto executable = std::filesystem::read_symlink("/proc/self/exe", error);
    if (error || executable.empty())
        return 2;

    std::wstring_convert<std::codecvt_utf8<wchar_t>> converter;
    std::vector<std::wstring> gameArguments;
    gameArguments.reserve(argc > 1 ? static_cast<size_t>(argc - 1) : 0);
    try {
        for (int index = 1; index < argc; ++index)
            gameArguments.push_back(converter.from_bytes(argv[index]));
    } catch (const std::range_error &) {
        return 2;
    }

    const auto layout = infernux::playerhost::ResolveLayout(executable);
    infernux::playerhost::PlayerHost host;
    return host.Run(layout, gameArguments);
}

#endif
