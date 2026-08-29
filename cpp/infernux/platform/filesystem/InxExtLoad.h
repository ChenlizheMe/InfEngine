#pragma once

#include <platform/filesystem/InxPath.h>

#include <iostream>
#include <stdexcept>
#include <string>
#include <unordered_map>

#include <core/config/InxPlatform.h>

#if defined(__unix__) || defined(__unix) || defined(__APPLE__)
#include <dlfcn.h>
#define INFERNUX_API extern "C"
#elif defined(INX_PLATFORM_WINDOWS)
#define INFERNUX_API extern "C" __declspec(dllexport)
#define dlsym GetProcAddress
#endif

#include <core/log/InxLog.h>

namespace infernux
{

class InxExtLoad
{
  public:
    void LoadDLL(const std::string &dllName, const std::string &dllDir)
    {
#if defined(__unix__) || defined(__unix) || defined(__APPLE__) || defined(__MACH__)
#if defined(__APPLE__)
        std::string fullPath = JoinPath({dllDir, "lib" + dllName + ".dylib"});
        INXLOG_DEBUG("Loading shared lib on macOS: ", fullPath.c_str());
#else
        std::string fullPath = JoinPath({dllDir, "lib" + dllName + ".so"});
        INXLOG_DEBUG("Loading shared lib on Linux: ", fullPath.c_str());
#endif
        void *hLib = dlopen(fullPath.c_str(), RTLD_NOW | RTLD_GLOBAL);
#else
        std::string fullPath = JoinPath({dllDir, dllName + ".dll"});
        INXLOG_DEBUG("Loading DLL in windows: ", fullPath.c_str());
        HMODULE hLib = LoadLibraryA(fullPath.c_str());
#endif

        if (!hLib) {
#if defined(__unix__) || defined(__unix) || defined(__APPLE__) || defined(__MACH__)
            INXLOG_FATAL("Failed to load ", dllName.c_str(), ". Error: ", dlerror());
#else
            INXLOG_ERROR("Failed to load ", dllName.c_str(), ". Reason: ", GetLastErrorAsString().c_str());
#endif
            return;
        }

        m_dlls[dllName] = hLib;
    }

    template <typename T> T LoadFunc(const std::string &dllName, const std::string &funcName)
    {
        auto it = m_dlls.find(dllName);
        if (it == m_dlls.end())
            INXLOG_FATAL("DLL", dllName.c_str(), " not loaded.");

        auto sym = reinterpret_cast<T>(dlsym(it->second, funcName.c_str()));
        if (!sym)
            INXLOG_FATAL("Cannot to load symbol: ", funcName.c_str(), " from ", dllName.c_str());

        INXLOG_DEBUG("Succeed to load symbol: ", funcName.c_str(), " from ", dllName.c_str());
        return sym;
    }

    void UnloadAll()
    {
        for (auto &[name, handle] : m_dlls) {
#ifdef INX_PLATFORM_WINDOWS
            FreeLibrary(handle);
#else
            dlclose(handle);
#endif
        }
        m_dlls.clear();
    }

    ~InxExtLoad()
    {
        UnloadAll();
    }

  private:
#ifdef INX_PLATFORM_WINDOWS
    std::unordered_map<std::string, HMODULE> m_dlls;

    std::string GetLastErrorAsString()
    {
        DWORD errorMessageID = ::GetLastError();
        if (errorMessageID == 0)
            return "No error message"; // No error

        LPSTR messageBuffer = nullptr;

        size_t size = FormatMessageA(
            FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS, NULL,
            errorMessageID, MAKELANGID(LANG_NEUTRAL, SUBLANG_DEFAULT), (LPSTR)&messageBuffer, 0, NULL);

        std::string message(messageBuffer, size);
        LocalFree(messageBuffer);
        return message;
    }
#else
    std::unordered_map<std::string, void *> m_dlls;
#endif
};

} // namespace infernux
