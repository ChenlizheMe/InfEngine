#include "NativeFileDialog.h"

#include <SDL3/SDL.h>

#include <chrono>
#include <condition_variable>
#include <mutex>

namespace infernux
{
namespace
{

struct NativeFileDialogState
{
    std::mutex mutex;
    std::condition_variable completedCondition;
    bool completed = false;
    NativeFileDialogResult result;
};

void SDLCALL CompleteNativeFileDialog(void *userdata, const char *const *files, int selectedFilter)
{
    auto *state = static_cast<NativeFileDialogState *>(userdata);
    if (state == nullptr)
        return;

    NativeFileDialogResult result;
    result.selectedFilter = selectedFilter;
    if (files == nullptr) {
        const char *error = SDL_GetError();
        result.error = error != nullptr && *error != '\0' ? error : "SDL file dialog failed";
    } else if (files[0] == nullptr) {
        result.cancelled = true;
    } else {
        result.accepted = true;
        result.path = files[0];
    }

    {
        std::lock_guard lock(state->mutex);
        state->result = std::move(result);
        state->completed = true;
    }
    state->completedCondition.notify_all();
}

SDL_FileDialogType ToSDLDialogType(NativeFileDialogKind kind)
{
    switch (kind) {
    case NativeFileDialogKind::OpenFile:
        return SDL_FILEDIALOG_OPENFILE;
    case NativeFileDialogKind::SaveFile:
        return SDL_FILEDIALOG_SAVEFILE;
    case NativeFileDialogKind::OpenFolder:
        return SDL_FILEDIALOG_OPENFOLDER;
    }
    return SDL_FILEDIALOG_OPENFILE;
}

} // namespace

NativeFileDialogResult ShowNativeFileDialog(NativeFileDialogKind kind, const std::string &title,
                                            const std::string &defaultLocation,
                                            const std::vector<NativeFileDialogFilter> &filters)
{
    if (!SDL_IsMainThread()) {
        NativeFileDialogResult result;
        result.error = "Native file dialogs must be opened from the Editor main thread";
        return result;
    }
    if ((SDL_WasInit(SDL_INIT_VIDEO) & SDL_INIT_VIDEO) == 0) {
        NativeFileDialogResult result;
        result.error = "Native file dialogs require an initialized graphical Editor";
        return result;
    }

    std::vector<SDL_DialogFileFilter> nativeFilters;
    nativeFilters.reserve(filters.size());
    for (const auto &filter : filters)
        nativeFilters.push_back({filter.name.c_str(), filter.pattern.c_str()});

    const SDL_PropertiesID properties = SDL_CreateProperties();
    if (properties == 0) {
        NativeFileDialogResult result;
        result.error = std::string("Could not create SDL file dialog properties: ") + SDL_GetError();
        return result;
    }

    const auto failProperties = [properties](const char *property) {
        NativeFileDialogResult result;
        result.error = std::string("Could not configure SDL file dialog property ") + property + ": " + SDL_GetError();
        SDL_DestroyProperties(properties);
        return result;
    };

    SDL_Window *parent = SDL_GetKeyboardFocus();
    if (parent == nullptr)
        parent = SDL_GetMouseFocus();
    if (parent != nullptr && !SDL_SetPointerProperty(properties, SDL_PROP_FILE_DIALOG_WINDOW_POINTER, parent))
        return failProperties(SDL_PROP_FILE_DIALOG_WINDOW_POINTER);
    if (!title.empty() && !SDL_SetStringProperty(properties, SDL_PROP_FILE_DIALOG_TITLE_STRING, title.c_str()))
        return failProperties(SDL_PROP_FILE_DIALOG_TITLE_STRING);
    if (!defaultLocation.empty() &&
        !SDL_SetStringProperty(properties, SDL_PROP_FILE_DIALOG_LOCATION_STRING, defaultLocation.c_str()))
        return failProperties(SDL_PROP_FILE_DIALOG_LOCATION_STRING);
    if (kind != NativeFileDialogKind::OpenFolder && !nativeFilters.empty()) {
        if (!SDL_SetPointerProperty(properties, SDL_PROP_FILE_DIALOG_FILTERS_POINTER, nativeFilters.data()))
            return failProperties(SDL_PROP_FILE_DIALOG_FILTERS_POINTER);
        if (!SDL_SetNumberProperty(properties, SDL_PROP_FILE_DIALOG_NFILTERS_NUMBER,
                                   static_cast<Sint64>(nativeFilters.size())))
            return failProperties(SDL_PROP_FILE_DIALOG_NFILTERS_NUMBER);
    }
    if (!SDL_SetBooleanProperty(properties, SDL_PROP_FILE_DIALOG_MANY_BOOLEAN, false))
        return failProperties(SDL_PROP_FILE_DIALOG_MANY_BOOLEAN);

    NativeFileDialogState state;
    SDL_ShowFileDialogWithProperties(ToSDLDialogType(kind), &CompleteNativeFileDialog, &state, properties);

    std::unique_lock lock(state.mutex);
    while (!state.completed) {
        lock.unlock();
        // XDG portals dispatch their DBus response through SDL's event loop.
        // Pumping without polling preserves queued Editor input for the normal
        // InxView event path after the modal dialog closes.
        SDL_PumpEvents();
        lock.lock();
        state.completedCondition.wait_for(lock, std::chrono::milliseconds(10), [&state] { return state.completed; });
    }
    NativeFileDialogResult result = std::move(state.result);
    lock.unlock();
    SDL_DestroyProperties(properties);
    return result;
}

} // namespace infernux
