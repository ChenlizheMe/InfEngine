#pragma once

#include <string>
#include <vector>

namespace infernux
{

enum class NativeFileDialogKind
{
    OpenFile,
    SaveFile,
    OpenFolder,
};

struct NativeFileDialogFilter
{
    std::string name;
    std::string pattern;
};

struct NativeFileDialogResult
{
    bool accepted = false;
    bool cancelled = false;
    std::string path;
    std::string error;
    int selectedFilter = -1;
};

/// Show one modal desktop file dialog through SDL's platform backend.
///
/// SDL exposes dialogs asynchronously. The Editor-facing API remains
/// synchronous like Unity's EditorUtility panels, so this bridge keeps pumping
/// SDL events until the platform callback completes. On Linux SDL selects the
/// XDG desktop portal first and falls back to Zenity.
NativeFileDialogResult ShowNativeFileDialog(NativeFileDialogKind kind, const std::string &title,
                                            const std::string &defaultLocation,
                                            const std::vector<NativeFileDialogFilter> &filters);

} // namespace infernux
