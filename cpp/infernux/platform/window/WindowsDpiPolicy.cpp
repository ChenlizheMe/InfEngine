#include "WindowsDpiPolicy.h"

#include <SDL3/SDL.h>

#include <cstdint>
#include <stdexcept>

#if defined(_WIN32)
#define WIN32_LEAN_AND_MEAN
#define NOMINMAX
#include <windows.h>
#endif

namespace infernux
{

void ConfigureRequiredWindowsDpiPolicy()
{
#if defined(_WIN32)
    if (!SDL_SetHint("SDL_WINDOWS_DPI_AWARENESS", "permonitorv2"))
        throw std::runtime_error("SDL rejected the required Windows Per-Monitor V2 DPI policy");
#endif
}

void VerifyRequiredWindowsDpiPolicy()
{
#if defined(_WIN32)
    HMODULE user32 = GetModuleHandleW(L"user32.dll");
    if (user32 == nullptr)
        throw std::runtime_error("Windows user32.dll is unavailable for DPI policy verification");

    using GetThreadDpiAwarenessContextFn = HANDLE(WINAPI *)();
    using AreDpiAwarenessContextsEqualFn = BOOL(WINAPI *)(HANDLE, HANDLE);
    const auto getThreadContext =
        reinterpret_cast<GetThreadDpiAwarenessContextFn>(GetProcAddress(user32, "GetThreadDpiAwarenessContext"));
    const auto contextsEqual =
        reinterpret_cast<AreDpiAwarenessContextsEqualFn>(GetProcAddress(user32, "AreDpiAwarenessContextsEqual"));
    if (getThreadContext == nullptr || contextsEqual == nullptr)
        throw std::runtime_error("Windows Per-Monitor V2 DPI APIs are unavailable");

    const HANDLE perMonitorV2 = reinterpret_cast<HANDLE>(static_cast<intptr_t>(-4));
    if (!contextsEqual(getThreadContext(), perMonitorV2))
        throw std::runtime_error("Windows rejected the required Per-Monitor V2 DPI policy");
#endif
}

} // namespace infernux
