#include "ProjectPanel.h"

#include "Infernux.h"

#include <core/threading/JobSystem.h>
#include <function/editor/EditorThemeRegistry.h>
#include <function/renderer/gui/InxGUISemantics.h>
#include <function/renderer/gui/InxResourcePreviewer.h>
#include <function/renderer/gui/InxTextLayout.h>
#include <function/resources/AssetFormatRegistry.h>
#include <platform/filesystem/InxPath.h>

#include <algorithm>
#include <any>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <functional>
#include <imgui_internal.h>
#include <nlohmann/json.hpp>
#include <string_view>
#include <unordered_set>

#ifdef INX_PLATFORM_WINDOWS
#include <ShlObj.h> // CF_HDROP, DragQueryFileW
#include <shellapi.h>
#include <strsafe.h>
#endif

namespace fs = std::filesystem;

namespace
{
constexpr const char *kSubMatToken = "::submat:";
constexpr const char *kSubBoneToken = "::subbone:";
constexpr const char *kSubAnimToken = "::subanim:";

static std::string TrimCopy(std::string s)
{
    auto notSpace = [](unsigned char ch) { return !std::isspace(ch); };
    while (!s.empty() && !notSpace(static_cast<unsigned char>(s.front())))
        s.erase(s.begin());
    while (!s.empty() && !notSpace(static_cast<unsigned char>(s.back())))
        s.pop_back();
    return s;
}

/// "Armature | Action" or "骨架|骨架Action" from importers → display stem only.
static std::string StripPipeDisplaySuffix(const std::string &s)
{
    auto pos = s.find(" | ");
    if (pos == std::string::npos)
        pos = s.find('|');
    if (pos == std::string::npos)
        return s;
    return TrimCopy(s.substr(0, pos));
}

static std::vector<std::string> SplitCommaList(const std::string &csv)
{
    std::vector<std::string> out;
    std::string cur;
    for (char ch : csv) {
        if (ch == ',') {
            auto t = TrimCopy(cur);
            if (!t.empty())
                out.push_back(std::move(t));
            cur.clear();
        } else {
            cur.push_back(ch);
        }
    }
    auto t = TrimCopy(cur);
    if (!t.empty())
        out.push_back(std::move(t));
    return out;
}

static std::string MakeSubAssetVirtualPath(const std::string &basePath, const char *token, int index)
{
    return basePath + token + std::to_string(index);
}

static bool IsVirtualSubAssetPath(const std::string &path)
{
    return path.find(kSubMatToken) != std::string::npos || path.find(kSubBoneToken) != std::string::npos ||
           path.find(kSubAnimToken) != std::string::npos;
}

static std::string ResolveRealAssetPath(const std::string &path)
{
    if (path.empty())
        return path;
    for (const char *tok : {kSubMatToken, kSubBoneToken, kSubAnimToken}) {
        auto pos = path.find(tok);
        if (pos != std::string::npos)
            return path.substr(0, pos);
    }
    return path;
}

static std::string AssetSelectionPathKey(const std::string &path)
{
    return infernux::AssetPathKey(path);
}

static std::string MakeAssetOpenCommandArgument(const std::string &kind, const std::string &path)
{
    return kind + "\t" + path;
}

static std::string MakePrefabSaveAsCommandArgument(uint64_t objectId, const std::string &directory)
{
    return std::to_string(objectId) + "\t" + directory;
}

static std::string MakeAssetRenameCommandArgument(const std::string &sourcePath, const std::string &newName)
{
    return sourcePath + "\t" + newName;
}

static std::string MakeAssetImportCommandArgument(const std::vector<std::string> &paths, const std::string &destination)
{
    return nlohmann::json{{"paths", paths}, {"destination", destination}}.dump();
}

static std::string MakeAssetTransferCommandArgument(const std::vector<std::string> &paths,
                                                    const std::string &destination)
{
    return nlohmann::json{{"paths", paths}, {"destination", destination}}.dump();
}

static std::string MakeTreeExpandedCommandArgument(const std::string &itemId, bool expanded)
{
    return itemId + "\t" + (expanded ? "1" : "0");
}

#ifdef INX_PLATFORM_WINDOWS
static std::string Utf8FromWidePath(const std::wstring &wpath)
{
    return infernux::FromFsPath(fs::path(wpath));
}
#endif

static std::string SelectionPathForInspector(const std::string &path)
{
    if (path.empty())
        return path;
    // Embedded material slots use the material inspector (Python + virtual path).
    if (path.find(kSubMatToken) != std::string::npos)
        return path;
    // Embedded animation takes use the 3D clip inspector (Python + virtual path).
    if (path.find(kSubAnimToken) != std::string::npos)
        return path;
    return ResolveRealAssetPath(path);
}

/// True if the mouse is over the docked/floating Inspector window (screen space).
/// Prevents Project panel from clearing file selection when clicking empty Inspector space.
bool IsMouseOverInspectorWindow()
{
    ImGuiWindow *win = ImGui::FindWindowByName("Inspector###inspector");
    if (win == nullptr || win->Hidden)
        return false;
    const ImVec2 mp = ImGui::GetIO().MousePos;
    const float x0 = win->Pos.x;
    const float y0 = win->Pos.y;
    const float x1 = x0 + win->SizeFull.x;
    const float y1 = y0 + win->SizeFull.y;
    return mp.x >= x0 && mp.x <= x1 && mp.y >= y0 && mp.y <= y1;
}
} // namespace

// ImGui key constants
static constexpr int kKeyLeftCtrl = ImGuiKey_LeftCtrl;
static constexpr int kKeyRightCtrl = ImGuiKey_RightCtrl;
static constexpr int kKeyLeftShift = ImGuiKey_LeftShift;
static constexpr int kKeyRightShift = ImGuiKey_RightShift;
static constexpr int kKeyEnter = ImGuiKey_Enter;

namespace infernux
{

static std::vector<std::string> GetOSClipboardFiles();

namespace
{
// All editor colors resolve through the runtime theme registry (active theme),
// so a theme switch re-skins the Project panel live. Fallbacks reproduce the
// current look when a token is absent.
inline ImVec4 ThemeColor(const char *name, const ImVec4 &fb)
{
    return EditorThemeRegistry::Color(name, fb);
}
inline ImU32 ThemeU32(const char *name, const ImVec4 &fb)
{
    return ImGui::ColorConvertFloat4ToU32(EditorThemeRegistry::Color(name, fb));
}

inline ImU32 ProjectSelectionOutlineColor()
{
    return ThemeU32("ROLE_ACCENT", ImVec4(EditorTheme::ACCENT_R, EditorTheme::ACCENT_G, EditorTheme::ACCENT_B, 1.0f));
}

inline ImU32 ProjectExpandStripBg(bool hovered)
{
    return hovered ? ThemeU32("PROJECT_EXPAND_STRIP_HOVER", EditorTheme::PROJECT_EXPAND_STRIP_HOVER)
                   : ThemeU32("PROJECT_EXPAND_STRIP_BG", EditorTheme::PROJECT_EXPAND_STRIP_BG);
}

inline ImU32 ProjectSubAssetCellBg()
{
    return ThemeU32("PROJECT_SUBASSET_CELL_BG", EditorTheme::PROJECT_SUBASSET_CELL_BG);
}

// Accent-tinted highlight used for eased hover / selection backgrounds.
inline ImVec4 ProjectAccentColor()
{
    return ThemeColor("ROLE_ACCENT", ImVec4(EditorTheme::ACCENT_R, EditorTheme::ACCENT_G, EditorTheme::ACCENT_B, 1.0f));
}
inline ImVec4 ProjectHoverColor()
{
    return ThemeColor("ROLE_BG_HOVER", ImVec4(0.165f, 0.165f, 0.165f, 1.0f));
}

constexpr float kProjectSelectionOutlineThickness = 2.0f;
/// Full-height click strip on the right of the model icon (image drawn with aspect preserved).
constexpr float kModelExpandStripW = 12.0f;
/// Pixel size of `model_expand_*.png` in repo (square); used only for correct scaling in the strip.
constexpr float kModelExpandIconSrcPx = 32.0f;

} // namespace

// ════════════════════════════════════════════════════════════════════
// Static extension sets
// ════════════════════════════════════════════════════════════════════

static const std::unordered_set<std::string> sImageExtensions = [] {
    std::unordered_set<std::string> result;
    for (const auto extension : asset_formats::kTextureExtensions)
        result.emplace(extension);
    return result;
}();

static const std::unordered_set<std::string> sMaterialExtensions = {".mat"};

static const std::unordered_set<std::string> sModelExtensions = [] {
    std::unordered_set<std::string> result;
    for (const auto extension : asset_formats::kMeshExtensions)
        result.emplace(extension);
    return result;
}();

static const std::unordered_set<std::string> sHiddenExtensions = {".meta",        ".pyc",    ".pyo",       ".tmp",
                                                                  ".inxparticle", ".inxtex", ".inxvfield", ".inxsdf"};

static const std::unordered_set<std::string> sHiddenFiles = {"imgui.ini"};

bool ProjectPanel::IsImageExt(const std::string &ext)
{
    return sImageExtensions.count(ext) > 0;
}
bool ProjectPanel::IsMaterialExt(const std::string &ext)
{
    return sMaterialExtensions.count(ext) > 0;
}
bool ProjectPanel::IsModelExt(const std::string &ext)
{
    return sModelExtensions.count(ext) > 0;
}

// ════════════════════════════════════════════════════════════════════
// Static data: Icon map
// ════════════════════════════════════════════════════════════════════

const std::unordered_map<std::string, std::string> &ProjectPanel::GetIconMap()
{
    static const std::unordered_map<std::string, std::string> map = [] {
        std::unordered_map<std::string, std::string> result = {
            {"__dir__", "folder"},
            {".py", "script_py"},
            {".vert", "shader_vert"},
            {".frag", "shader_frag"},
            {".hlsl", "shader_hlsl"},
            {".glsl", "shader_glsl"},
            {".shadingmodel", "shadingmodel"},
            {".ttf", "font"},
            {".otf", "font"},
            {".txt", "text"},
            {".md", "readme"},
            {".mat", "material"},
            {".physicmaterial", "physic_material"},
            {".scene", "scene"},
            // Scene prefabs still use the mesh-preview pipeline when possible;
            // this named icon is only the explicit fallback for missing previews.
            {".animclip2d", "animclip2d"},
            {".animclip3d", "animclip3d"},
            {".animfsm", "animfsm"},
            {".animtimeline", "timeline"},
            {".timelinefsm", "timeline_fsm"},
            {".particlegraph", "particle_graph"},
            {".effect", "render_effect"},
            {".effectgroup", "render_effect_group"},
            {".prefab", "prefab"},
        };
        for (const auto extension : asset_formats::kTextureExtensions)
            result.emplace(std::string(extension), "texture");
        for (const auto extension : asset_formats::kMeshExtensions)
            result.emplace(std::string(extension), "model_3d");
        for (const auto extension : asset_formats::kAudioExtensions)
            result.emplace(std::string(extension), "audio");
        return result;
    }();
    return map;
}

// ════════════════════════════════════════════════════════════════════
// Static data: Drag-drop maps
// ════════════════════════════════════════════════════════════════════

const std::unordered_map<std::string, ProjectPanel::DragDropInfo> &ProjectPanel::GetDragDropMap()
{
    static const std::unordered_map<std::string, DragDropInfo> map = [] {
        std::unordered_map<std::string, DragDropInfo> result = {
            {".py", {"SCRIPT_FILE", "Script"}},
            {".mat", {"MATERIAL_FILE", "Material"}},
            {".physicmaterial", {"PHYSIC_MATERIAL_FILE", "PhysicMaterial"}},
            {".vert", {"SHADER_FILE", "Shader"}},
            {".frag", {"SHADER_FILE", "Shader"}},
            {".glsl", {"SHADER_FILE", "Shader"}},
            {".hlsl", {"SHADER_FILE", "Shader"}},
            {".ttf", {"FONT_FILE", "Font"}},
            {".otf", {"FONT_FILE", "Font"}},
            {".scene", {"SCENE_FILE", "Scene"}},
            {".animclip2d", {"ANIMCLIP_FILE", "2D AnimClip"}},
            {".animclip3d", {"ANIMCLIP3D_FILE", "3D AnimClip"}},
            {".animfsm", {"ANIMFSM_FILE", "AnimFSM"}},
            {".particlegraph", {"PARTICLE_GRAPH_FILE", "Particle Graph"}},
            {".effect", {"RENDER_EFFECT_FILE", "Render Effect"}},
            // Effect assets and groups occupy the same RenderStack slot type.
            {".effectgroup", {"RENDER_EFFECT_FILE", "Render Effect Group"}},
            {".animtimeline", {"ANIMTIMELINE_FILE", "Timeline"}},
            {".timelinefsm", {"TIMELINEFSM_FILE", "TimelineFSM"}},
        };
        for (const auto extension : asset_formats::kTextureExtensions)
            result.emplace(std::string(extension), DragDropInfo{"TEXTURE_FILE", "Texture"});
        for (const auto extension : asset_formats::kMeshExtensions)
            result.emplace(std::string(extension), DragDropInfo{"MODEL_FILE", "Model"});
        for (const auto extension : asset_formats::kAudioExtensions)
            result.emplace(std::string(extension), DragDropInfo{"AUDIO_FILE", "Audio"});
        return result;
    }();
    return map;
}

const std::unordered_map<std::string, ProjectPanel::GuidDragDropInfo> &ProjectPanel::GetGuidDragDropMap()
{
    static const std::unordered_map<std::string, GuidDragDropInfo> map = [] {
        std::unordered_map<std::string, GuidDragDropInfo> result = {
            {".prefab", {"PREFAB_GUID", "PREFAB_FILE", "Prefab"}},
        };
        for (const auto extension : asset_formats::kMeshExtensions)
            result.emplace(std::string(extension), GuidDragDropInfo{"MODEL_GUID", "MODEL_FILE", "Model"});
        return result;
    }();
    return map;
}

const std::vector<std::string> &ProjectPanel::GetMoveAcceptTypes()
{
    static const std::vector<std::string> types = [] {
        std::unordered_set<std::string> pathTypes;
        std::unordered_set<std::string> guidTypes;
        for (auto &[_, info] : GetDragDropMap())
            pathTypes.insert(info.payloadType);
        for (auto &[_, info] : GetGuidDragDropMap()) {
            pathTypes.insert(info.pathPayloadType);
            guidTypes.insert(info.guidPayloadType);
        }
        pathTypes.insert(DRAG_TYPE_PROJECT_ITEM);
        std::vector<std::string> result;
        result.reserve(pathTypes.size() + guidTypes.size());
        for (auto &t : pathTypes)
            result.push_back(t);
        for (auto &t : guidTypes)
            result.push_back(t);
        return result;
    }();
    return types;
}

// ════════════════════════════════════════════════════════════════════
// File type tag for text fallback
// ════════════════════════════════════════════════════════════════════

const char *ProjectPanel::GetFileTypeTag(const std::string &filename)
{
    auto dot = filename.rfind('.');
    if (dot == std::string::npos)
        return "[FILE]";
    std::string ext = filename.substr(dot);
    for (auto &c : ext)
        c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    if (ext == ".py" || ext == ".lua" || ext == ".cs")
        return "[PY]";
    if (asset_formats::Contains(asset_formats::kAudioExtensions, ext))
        return "[AUD]";
    if (ext == ".mat")
        return "[MAT]";
    if (ext == ".physicmaterial")
        return "[PMAT]";
    if (ext == ".effect")
        return "[FX]";
    if (ext == ".effectgroup")
        return "[FXG]";
    if (ext == ".vert" || ext == ".frag" || ext == ".glsl" || ext == ".hlsl")
        return "[SHDR]";
    if (IsImageExt(ext))
        return "[IMG]";
    if (IsModelExt(ext))
        return "[3D]";
    if (ext == ".scene")
        return "[SCN]";
    if (ext == ".prefab")
        return "[PRE]";
    if (ext == ".animclip3d")
        return "[A3]";
    if (ext == ".animtimeline")
        return "[Timeline]";
    if (ext == ".timelinefsm")
        return "[TLFSM]";
    if (asset_formats::Contains(asset_formats::kAudioExtensions, ext))
        return "[AUD]";
    if (ext == ".ttf" || ext == ".otf")
        return "[FNT]";
    if (ext == ".json" || ext == ".yaml" || ext == ".yml" || ext == ".xml")
        return "[CFG]";
    if (ext == ".txt" || ext == ".md")
        return "[TXT]";
    return "[FILE]";
}

// ════════════════════════════════════════════════════════════════════
// Filtering
// ════════════════════════════════════════════════════════════════════

bool ProjectPanel::ShouldShow(const std::string &name)
{
    if (name.empty())
        return false;
    if (sHiddenFiles.count(name) > 0)
        return false;
    // Check hidden prefixes: '.', '__'
    if (name[0] == '.')
        return false;
    if (name.size() >= 2 && name[0] == '_' && name[1] == '_')
        return false;
    // Check extension
    auto dot = name.rfind('.');
    if (dot != std::string::npos) {
        std::string ext = name.substr(dot);
        for (auto &c : ext)
            c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
        if (sHiddenExtensions.count(ext) > 0)
            return false;
    }
    return true;
}

// ════════════════════════════════════════════════════════════════════
// Path utilities
// ════════════════════════════════════════════════════════════════════

std::string ProjectPanel::GetMinimumBrowsePath() const
{
    if (!m_rootPath.empty() && m_navHasSubfolders && !m_preferredNavPath.empty())
        return FilesystemPathKey(m_preferredNavPath);
    return FilesystemPathKey(m_rootPath);
}

bool ProjectPanel::CanNavigateUpFromCurrent() const
{
    return m_canNavigateUp;
}

int ProjectPanel::GetPathDepthFromRoot(const std::string &path) const
{
    if (m_rootPath.empty() || path.empty())
        return 0;

    std::string relative;
    if (!infernux::TryMakeRelativeFilesystemPath(path, m_rootPath, relative, true))
        return -1;
    if (relative == ".")
        return 0;

    int depth = 0;
    for (const auto &part : infernux::ToFsPath(relative)) {
        const std::string name = infernux::FromFsPath(part);
        if (name.empty() || name == ".")
            continue;
        ++depth;
    }
    return depth;
}

void ProjectPanel::ClampNavigationPath()
{
    if (m_rootPath.empty() || m_currentPath.empty()) {
        UpdateNavigationCache();
        return;
    }

    if (!IsFilesystemPathWithin(m_currentPath, m_rootPath)) {
        m_currentPath = (m_navHasSubfolders && !m_preferredNavPath.empty()) ? m_preferredNavPath : m_rootPath;
        UpdateNavigationCache();
        return;
    }

    UpdateNavigationCache();
}

void ProjectPanel::UpdateNavigationCache()
{
    if (m_rootPath.empty() || m_currentPath.empty()) {
        m_currentPathKey.clear();
        m_canNavigateUp = false;
        return;
    }
    const std::string current = FilesystemPathKey(m_currentPath);
    m_currentPathKey = current;
    std::string floor = GetMinimumBrowsePath();
    // Navigation covers the whole worktree, while Up stops at the current
    // top-level branch. Assets therefore remains its own floor; a generated
    // resource under Library can still climb back to Library without exposing
    // paths above the project or unexpectedly jumping into Assets.
    if (floor.empty() || !IsFilesystemPathWithin(current, floor)) {
        floor = FilesystemPathKey(m_rootPath);
        std::string relative;
        if (infernux::TryMakeRelativeFilesystemPath(m_currentPath, m_rootPath, relative, true) && relative != ".") {
            for (const auto &part : infernux::ToFsPath(relative)) {
                const std::string name = infernux::FromFsPath(part);
                if (name.empty() || name == ".")
                    continue;
                floor = FilesystemPathKey(infernux::FromFsPath(infernux::ToFsPath(m_rootPath) / part));
                break;
            }
        }
    }
    m_canNavigateUp = IsFilesystemPathWithin(current, floor) && current != floor;
}

void ProjectPanel::AssignCurrentPath(const std::string &path)
{
    m_currentPath = path;
    ClampNavigationPath();
}

uint64_t ProjectPanel::GetMtimeNs(const std::string &path)
{
    std::error_code ec;
    auto ftime = fs::last_write_time(fs::u8path(path), ec);
    if (ec)
        return 0;
    // Store raw bit pattern as uint64_t.  On MSVC, file_clock ns since epoch
    // can exceed INT64_MAX; we only need the value for change-detection, not
    // as a signed timestamp, so reinterpret the bits.
    const auto rawNs = std::chrono::duration_cast<std::chrono::nanoseconds>(ftime.time_since_epoch()).count();
    uint64_t bits;
    std::memcpy(&bits, &rawNs, sizeof(bits));
    return bits;
}

// ════════════════════════════════════════════════════════════════════
// Construction
// ════════════════════════════════════════════════════════════════════

ProjectPanel::ProjectPanel() : EditorPanel("Project", "project")
{
}

std::unordered_map<std::string, double> ProjectPanel::ConsumeSubTimings()
{
    std::unordered_map<std::string, double> result{
        {"breadcrumb", m_subBreadcrumb},
        {"fileGrid", m_subFileGrid},
        {"folderTree", m_subFolderTree},
        {"gridContext", m_subGridContext},
        {"gridData", m_subGridData},
        {"gridItems", m_subGridItems},
        {"gridTail", m_subGridTail},
        {"preIcons", m_subPreIcons},
        {"preOther", m_subPreOther},
        {"prePreview", m_subPrePreview},
        {"tail", m_subTail},
        {"dirSnapshotBuilds", static_cast<double>(m_dirSnapshotBuilds)},
        {"folderTreeRebuilds", static_cast<double>(m_folderTreeRebuilds)},
        {"projectItemsRebuilds", static_cast<double>(m_projectItemsRebuilds)},
        {"previewScheduleAttempts", static_cast<double>(m_previewScheduleAttempts)},
        {"folderRowsSubmitted", static_cast<double>(m_folderRowsSubmitted)},
        {"folderRowsVisible", static_cast<double>(m_folderRowsVisible)},
        {"gridItemsSubmitted", static_cast<double>(m_gridItemsSubmitted)},
        {"gridItemsVisible", static_cast<double>(m_gridItemsVisible)},
    };
    m_subPreIcons = m_subPrePreview = m_subPreOther = 0.0;
    m_subBreadcrumb = m_subFolderTree = m_subFileGrid = m_subTail = 0.0;
    m_subGridContext = m_subGridData = m_subGridItems = m_subGridTail = 0.0;
    m_dirSnapshotBuilds = 0;
    m_folderTreeRebuilds = 0;
    m_projectItemsRebuilds = 0;
    m_previewScheduleAttempts = 0;
    m_folderRowsSubmitted = 0;
    m_folderRowsVisible = 0;
    m_gridItemsSubmitted = 0;
    m_gridItemsVisible = 0;
    return result;
}

// ════════════════════════════════════════════════════════════════════
// Translation helper
// ════════════════════════════════════════════════════════════════════

const std::string &ProjectPanel::Tr(const std::string &key)
{
    auto it = m_trCache.find(key);
    if (it != m_trCache.end())
        return it->second;
    if (translate)
        m_trCache[key] = translate(key);
    else
        m_trCache[key] = key;
    return m_trCache[key];
}

// ════════════════════════════════════════════════════════════════════
// Public API
// ════════════════════════════════════════════════════════════════════

void ProjectPanel::SetRootPath(const std::string &path)
{
    m_rootPath = path;
    m_preferredNavPath = path;
    m_navHasSubfolders = false;
    m_searchIndexGeneration = UINT64_MAX;
    m_searchIndexRoot.clear();
    m_searchIndex.reset();
    ResetAsyncSearch();
    m_folderTreeProjection.Clear();
    m_folderTreeProjection.SetExpanded(path, true);
    InvalidateDirCache();

    std::error_code ec;
    fs::path assetsPath = fs::u8path(path) / "Assets";
    if (fs::is_directory(assetsPath, ec)) {
        m_preferredNavPath = infernux::FromFsPath(assetsPath);
        m_folderTreeProjection.SetExpanded(m_preferredNavPath, true);
        m_navHasSubfolders = true;
        m_currentPath = m_preferredNavPath;
        UpdateNavigationCache();
        return;
    }

    if (fs::is_directory(fs::u8path(path), ec)) {
        for (const auto &entry : fs::directory_iterator(fs::u8path(path), ec)) {
            if (ec)
                break;
            if (!entry.is_directory(ec))
                continue;
            m_navHasSubfolders = true;
            m_preferredNavPath = infernux::FromFsPath(entry.path());
            break;
        }
    }

    m_currentPath = m_navHasSubfolders ? m_preferredNavPath : path;
    UpdateNavigationCache();
}

void ProjectPanel::SetEngine(Infernux *engine)
{
    m_engine = engine;
}

void ProjectPanel::SetRenderer(InxRenderer *renderer)
{
    if (m_renderer == renderer)
        return;
    if (m_renderer) {
        for (const auto &[iconKey, textureId] : m_typeIconCache) {
            (void)textureId;
            m_renderer->RemoveImGuiTexture("__typeicon__" + iconKey);
        }
    }
    m_renderer = renderer;
    m_typeIconCache.clear();
    m_pendingTypeIconIds.clear();
    m_typeIconsLoaded = false;
}
void ProjectPanel::SetAssetDatabase(AssetDatabase *adb)
{
    if (m_assetDatabase == adb)
        return;
    m_assetDatabase = adb;
    m_searchIndexGeneration = UINT64_MAX;
    m_searchIndex.reset();
    ResetAsyncSearch();
    InvalidateDirCache();
}
void ProjectPanel::SetIconsDirectory(const std::string &dir)
{
    if (m_iconsDir == dir && m_typeIconsLoaded)
        return;
    if (m_renderer) {
        for (const auto &[iconKey, textureId] : m_typeIconCache) {
            (void)textureId;
            m_renderer->RemoveImGuiTexture("__typeicon__" + iconKey);
        }
    }
    m_iconsDir = dir;
    m_typeIconCache.clear();
    m_pendingTypeIconIds.clear();
    m_typeIconsLoaded = false;
}

bool ProjectPanel::CanNavigateToPath(const std::string &path) const
{
    std::error_code ec;
    if (path.empty() || !fs::is_directory(fs::u8path(path), ec))
        return false;
    if (!m_rootPath.empty() && !IsFilesystemPathWithin(path, m_rootPath))
        return false;
    return true;
}

bool ProjectPanel::SetCurrentPath(const std::string &path)
{
    if (!CanNavigateToPath(path))
        return false;
    AssignCurrentPath(path);
    return FilesystemPathKey(m_currentPath) == FilesystemPathKey(path);
}

std::vector<std::string> ProjectPanel::GetFolderExpandedPaths() const
{
    std::vector<std::string> paths(m_folderTreeProjection.ExpandedIds().begin(),
                                   m_folderTreeProjection.ExpandedIds().end());
    std::sort(paths.begin(), paths.end());
    return paths;
}

void ProjectPanel::SetFolderExpandedPaths(const std::vector<std::string> &paths)
{
    m_folderTreeProjection.ReplaceExpanded(std::unordered_set<std::string>(paths.begin(), paths.end()));
}

std::vector<std::string> ProjectPanel::GetModelExpandedPaths() const
{
    std::vector<std::string> paths(m_modelTreeProjection.ExpandedIds().begin(),
                                   m_modelTreeProjection.ExpandedIds().end());
    std::sort(paths.begin(), paths.end());
    return paths;
}

void ProjectPanel::SetModelExpandedPaths(const std::vector<std::string> &paths)
{
    if (m_modelTreeProjection.ReplaceExpanded(std::unordered_set<std::string>(paths.begin(), paths.end())))
        m_pendingAugmentedCacheInvalidation = true;
}

void ProjectPanel::ClearSelection(bool notify)
{
    if (!m_selectedFile.empty() || !m_selectedFiles.empty()) {
        m_selectedFile.clear();
        m_selectedFiles.clear();
        m_selectedSet.clear();
        if (notify)
            NotifySelectionChanged();
    }
}

void ProjectPanel::SetSelectedFile(const std::string &path, bool notify)
{
    if (path.empty()) {
        ClearSelection(notify);
        return;
    }
    SetSelectedFiles({path}, path, notify);
}

void ProjectPanel::SetSelectedFiles(const std::vector<std::string> &paths, const std::string &primary, bool notify)
{
    std::vector<std::string> selected;
    std::unordered_set<std::string> keys;
    selected.reserve(paths.size());
    for (const auto &path : paths) {
        const std::string key = AssetSelectionPathKey(path);
        const std::string backingPath = ResolveRealAssetPath(path);
        std::error_code ec;
        if (path.empty() || key.empty() || keys.find(key) != keys.end() || !fs::exists(fs::u8path(backingPath), ec))
            continue;
        // FileManager can reveal generated project resources such as imported
        // built-in shaders under Library.  Keep selection inside the project
        // worktree, but do not confuse that boundary with the Assets-only
        // authoring picker policy.
        if (!m_rootPath.empty() && !IsFilesystemPathWithin(backingPath, m_rootPath))
            continue;
        keys.insert(key);
        selected.push_back(path);
    }
    if (selected.empty()) {
        ClearSelection(notify);
        return;
    }

    auto primaryIt = std::find_if(selected.begin(), selected.end(), [&primary](const std::string &path) {
        return !primary.empty() && AssetSelectionPathKey(path) == AssetSelectionPathKey(primary);
    });
    m_selectedFile = primaryIt != selected.end() ? *primaryIt : selected.back();
    m_selectedFiles = std::move(selected);
    m_selectedSet = std::move(keys);

    if (notify)
        NotifySelectionChanged();
}

void ProjectPanel::InvalidateMaterialThumbnail(const std::string &filePath)
{
    if (filePath.empty())
        return;
    auto normTarget = FilesystemPathKey(filePath);

    std::vector<std::string> mtimeToRemove;
    for (auto &[path, _] : m_materialMtimeCache) {
        if (FilesystemPathKey(path) == normTarget)
            mtimeToRemove.push_back(path);
    }
    for (auto &path : mtimeToRemove)
        m_materialMtimeCache.erase(path);

    // A material can be edited in memory before its file timestamp advances.
    // Drop the passive Project result so the next visible frame adopts the
    // current published texture from the shared preview state.
    for (auto it = m_materialPreviewResults.begin(); it != m_materialPreviewResults.end();) {
        const std::string resourcePath = it->first.rfind("mat|", 0) == 0 ? it->first.substr(4) : it->first;
        if (FilesystemPathKey(ResolveRealAssetPath(resourcePath)) == normTarget)
            it = m_materialPreviewResults.erase(it);
        else
            ++it;
    }
}

void ProjectPanel::InvalidateTextureThumbnail(const std::string &filePath)
{
    if (filePath.empty())
        return;
    auto normTarget = FilesystemPathKey(filePath);

    // The live ImGui texture may keep its resource key while its dimensions
    // change after a reimport. Drop the dimension cache before the next draw;
    // preview request scheduling remains owned by the existing generation path.
    m_texturePreviewSizes.clear();

    std::vector<std::string> mtimeToRemove;
    for (auto &[path, _] : m_textureMtimeCache) {
        if (FilesystemPathKey(path) == normTarget)
            mtimeToRemove.push_back(path);
    }
    for (auto &path : mtimeToRemove)
        m_textureMtimeCache.erase(path);
}

// ════════════════════════════════════════════════════════════════════
// Notification helpers
// ════════════════════════════════════════════════════════════════════

void ProjectPanel::NotifySelectionChanged()
{
    PublishSelectionIntent(m_selectedFiles, m_selectedFile);
}

void ProjectPanel::PublishSelectionIntent(const std::vector<std::string> &paths, const std::string &primary) const
{
    std::vector<std::string> selectedPaths;
    selectedPaths.reserve(paths.size());
    for (const auto &path : paths)
        selectedPaths.push_back(SelectionPathForInspector(path));

    const std::string primaryPath = primary.empty() ? "" : SelectionPathForInspector(primary);
    if (onSelectionChanged)
        onSelectionChanged(selectedPaths, primaryPath);
}

std::vector<std::string> ProjectPanel::GetSelectedPaths() const
{
    std::vector<std::string> result;
    std::error_code ec;
    std::unordered_set<std::string> seen;
    for (auto &p : m_selectedFiles) {
        if (p.empty())
            continue;
        std::string real = ResolveRealAssetPath(p);
        if (real.empty())
            continue;
        if (!fs::exists(fs::u8path(real), ec))
            continue;
        if (seen.insert(real).second)
            result.push_back(real);
    }
    if (result.empty() && !m_selectedFile.empty()) {
        std::string real = ResolveRealAssetPath(m_selectedFile);
        if (!real.empty() && fs::exists(fs::u8path(real), ec))
            result.push_back(real);
    }
    return result;
}

// ════════════════════════════════════════════════════════════════════
// Directory snapshot cache
// ════════════════════════════════════════════════════════════════════

void ProjectPanel::InvalidateDirCache()
{
    // Always defer. AssetManager.move/import calls this from Python while the
    // file grid may still be iterating a DirSnapshot / items vector.
    m_pendingCacheInvalidation = true;
}

void ProjectPanel::ClearDirCachesNow()
{
    m_dirCache.clear();
    m_dirTreeMetaCache.clear();
    m_augmentedCache.clear();
    m_labelCache.clear();
    m_prefabTypeCache.clear();
    m_texturePreviewContracts.clear();
    m_texturePreviewRequests.clear();
    m_materialPreviewResults.clear();
    m_modelPreviewRequests.clear();
    m_texturePreviewSizes.clear();
    m_projectItemsCachePath.clear();
    m_projectItemsCacheSource = ProjectItemsCacheSource::None;
    m_projectItemsCacheDirectoryRevision = 0;
    m_projectItemsCacheProjectionRevision = UINT64_MAX;
    m_projectItemsCacheSnapshotMtime = 0;
    ++m_directoryRevision;
}

ProjectPanel::DirSnapshot *ProjectPanel::GetDirSnapshot(const std::string &path)
{
    if (path.empty())
        return nullptr;

    const auto catalog = m_assetDatabase ? m_assetDatabase->GetCatalogSnapshot() : nullptr;
    const uint64_t assetGeneration = catalog ? catalog->GetGeneration() : 0;

    // File changes are generation-driven. TTL validation is only for directory nodes.
    auto it = m_dirCache.find(path);
    if (it != m_dirCache.end()) {
        if (it->second.assetGeneration == assetGeneration) {
            // AssetDatabase and the Editor file watcher both invalidate this
            // cache on mutations. Avoid polling directory mtimes from the UI
            // thread when that event-driven source of truth is available.
            if (catalog || (m_frameTimeNow - it->second.lastValidatedAt) < DIR_CACHE_TTL)
                return &it->second;
        }
        uint64_t mtimeNs = GetMtimeNs(path);
        it->second.lastValidatedAt = m_frameTimeNow;
        if (it->second.assetGeneration == assetGeneration && it->second.mtimeNs == mtimeNs)
            return &it->second;
    }

    std::error_code ec;
    if (!fs::is_directory(fs::u8path(path), ec))
        return nullptr;

    uint64_t mtimeNs = GetMtimeNs(path);

    DirSnapshot snap;
    snap.mtimeNs = mtimeNs;
    snap.assetGeneration = assetGeneration;
    snap.lastValidatedAt = m_frameTimeNow;

    for (auto &entry : fs::directory_iterator(fs::u8path(path), ec)) {
        if (ec)
            break;
        auto name = infernux::FromFsPath(entry.path().filename());
        if (!ShouldShow(name))
            continue;

        bool isDir = entry.is_directory(ec);
        if (ec) {
            ec.clear();
            continue;
        }

        FileItem item;
        item.name = std::move(name);
        item.path = infernux::FromFsPath(entry.path());

        if (isDir) {
            item.type = FileItem::Dir;
            snap.dirs.push_back(std::move(item));
        } else if (!catalog) {
            item.type = FileItem::File;
            auto ext = infernux::FromFsPath(entry.path().extension());
            for (auto &c : ext)
                c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
            item.ext = std::move(ext);

            if (IsImageExt(item.ext) || IsMaterialExt(item.ext)) {
                auto ftime = entry.last_write_time(ec);
                if (!ec) {
                    const auto rawNs =
                        std::chrono::duration_cast<std::chrono::nanoseconds>(ftime.time_since_epoch()).count();
                    std::memcpy(&item.mtimeNs, &rawNs, sizeof(item.mtimeNs));
                }
            }
            snap.files.push_back(std::move(item));
        }
    }

    if (catalog) {
        const auto &assets = catalog->GetDirectory(FilesystemPathKey(path));
        snap.files.reserve(assets.size());
        for (const auto &asset : assets) {
            if (!ShouldShow(asset.name))
                continue;
            FileItem item;
            item.type = FileItem::File;
            item.name = asset.name;
            item.path = asset.path;
            item.resourceType = asset.resourceType;
            item.ext = infernux::FromFsPath(fs::u8path(asset.path).extension());
            for (char &character : item.ext)
                character = static_cast<char>(std::tolower(static_cast<unsigned char>(character)));
            std::memcpy(&item.mtimeNs, &asset.source.modifiedNs, sizeof(item.mtimeNs));
            snap.files.push_back(std::move(item));
        }
    }

    // Sort: ASCII case-insensitive; leave UTF-8 bytes unchanged outside ASCII letters.
    auto cmpName = [](const FileItem &a, const FileItem &b) {
        auto asciiLowerCopy = [](std::string s) {
            for (auto &c : s) {
                unsigned char uc = static_cast<unsigned char>(c);
                if (uc >= 'A' && uc <= 'Z')
                    c = static_cast<char>(uc + 32);
            }
            return s;
        };
        return asciiLowerCopy(a.name) < asciiLowerCopy(b.name);
    };
    std::sort(snap.dirs.begin(), snap.dirs.end(), cmpName);
    std::sort(snap.files.begin(), snap.files.end(), cmpName);

    snap.items.reserve(snap.dirs.size() + snap.files.size());
    snap.items.insert(snap.items.end(), snap.dirs.begin(), snap.dirs.end());
    snap.items.insert(snap.items.end(), snap.files.begin(), snap.files.end());
    for (auto &item : snap.items)
        item.selectionKey = AssetSelectionPathKey(item.path);
    for (const auto &file : snap.files) {
        if (IsModelExt(file.ext))
            snap.modelPaths.push_back(file.path);
    }

    // Update tree meta
    m_dirTreeMetaCache[path] = {!snap.dirs.empty()};

    auto &stored = m_dirCache[path] = std::move(snap);
    ++m_dirSnapshotBuilds;
    return &stored;
}

ProjectPanel::DirTreeMeta *ProjectPanel::GetDirTreeMeta(const std::string &path)
{
    if (path.empty())
        return nullptr;

    auto it = m_dirTreeMetaCache.find(path);
    if (it != m_dirTreeMetaCache.end())
        return &it->second;

    std::error_code ec;
    bool hasSubdirs = false;
    for (auto &entry : fs::directory_iterator(fs::u8path(path), ec)) {
        if (ec)
            break;
        auto name = infernux::FromFsPath(entry.path().filename());
        if (!ShouldShow(name))
            continue;
        if (entry.is_directory(ec) && !ec) {
            hasSubdirs = true;
            break;
        }
        ec.clear();
    }

    auto &meta = m_dirTreeMetaCache[path];
    meta.hasSubdirs = hasSubdirs;
    return &meta;
}

namespace
{
std::string TryGetMetaString(const infernux::InxResourceMeta *meta, const std::string &key)
{
    if (!meta || key.empty())
        return {};
    const auto &map = meta->GetMetadata();
    auto it = map.find(key);
    if (it == map.end())
        return {};
    const auto &typeName = it->second.first;
    const auto &value = it->second.second;
    try {
        if (typeName == "string")
            return std::any_cast<std::string>(value);
    } catch (...) {
    }
    return {};
}

int TryGetMetaInt(const infernux::InxResourceMeta *meta, const std::string &key, int defaultValue)
{
    if (!meta || key.empty())
        return defaultValue;
    const auto &map = meta->GetMetadata();
    auto it = map.find(key);
    if (it == map.end())
        return defaultValue;
    const auto &typeName = it->second.first;
    const auto &value = it->second.second;
    try {
        if (typeName == "int")
            return std::any_cast<int>(value);
        if (typeName == "size_t")
            return static_cast<int>(std::any_cast<size_t>(value));
        if (typeName == "float")
            return static_cast<int>(std::lround(std::any_cast<float>(value)));
    } catch (...) {
    }
    return defaultValue;
}
} // namespace

void ProjectPanel::AppendModelSubAssets(std::vector<FileItem> &out, AssetDatabase *adb, const FileItem &modelItem)
{
    const std::string &modelPath = modelItem.path;
    std::shared_ptr<const infernux::InxResourceMeta> meta;
    if (adb)
        meta = adb->GetMetaByPath(modelPath);

    const uint64_t childMtime = modelItem.mtimeNs;

    // ── Materials (material slots) ────────────────────────────────────
    std::vector<std::string> matNames = SplitCommaList(TryGetMetaString(meta.get(), "material_slots"));
    int matCount = TryGetMetaInt(meta.get(), "material_slot_count", -1);
    if (matNames.empty() && matCount > 0) {
        matNames.reserve(static_cast<size_t>(matCount));
        for (int i = 0; i < matCount; ++i)
            matNames.push_back("Material_" + std::to_string(i));
    }

    if (!matNames.empty()) {
        for (int i = 0; i < static_cast<int>(matNames.size()); ++i) {
            FileItem sub{};
            sub.type = FileItem::SubMaterial;
            sub.name = matNames[static_cast<size_t>(i)];
            sub.path = MakeSubAssetVirtualPath(modelPath, kSubMatToken, i);
            sub.ext = ".mat";
            sub.parentPath = modelPath;
            sub.mtimeNs = childMtime;
            sub.slotIndex = i;
            out.push_back(std::move(sub));
        }
    } else {
        FileItem sub{};
        sub.type = FileItem::SubMaterial;
        sub.name = "(No materials in meta — reimport model)";
        sub.path = MakeSubAssetVirtualPath(modelPath, kSubMatToken, -1);
        sub.ext = ".mat";
        sub.parentPath = modelPath;
        sub.mtimeNs = childMtime;
        sub.slotIndex = -1;
        out.push_back(std::move(sub));
    }

    // Embedded takes use the model path as their editor identity. Asset GUIDs
    // remain persistence identities, but they are not filesystem paths and
    // must never be fed back through Project selection/path projection.
    const std::string &animVirtualBase = modelPath;
    std::vector<std::string> animNames = SplitCommaList(TryGetMetaString(meta.get(), "animation_names_csv"));
    int animCount = TryGetMetaInt(meta.get(), "animation_count", -1);
    if (!animNames.empty()) {
        const int maxShow = 24;
        const int total = static_cast<int>(animNames.size());
        const int show = std::min(total, maxShow);
        for (int i = 0; i < show; ++i) {
            FileItem sub{};
            sub.type = FileItem::SubMesh;
            const std::string &takeName = animNames[static_cast<size_t>(i)];
            sub.name = StripPipeDisplaySuffix(takeName) + ".animclip3d";
            sub.path = MakeSubAssetVirtualPath(animVirtualBase, kSubAnimToken, i);
            sub.ext = ".animclip3d";
            sub.parentPath = modelPath;
            sub.mtimeNs = childMtime;
            sub.slotIndex = i;
            out.push_back(std::move(sub));
        }
        if (total > show) {
            FileItem sub{};
            sub.type = FileItem::SubMesh;
            sub.name = std::string("... ") + std::to_string(total - show) + " more animation takes";
            sub.path = MakeSubAssetVirtualPath(animVirtualBase, kSubAnimToken, 999999);
            sub.ext = ".animclip3d";
            sub.parentPath = modelPath;
            sub.mtimeNs = childMtime;
            sub.slotIndex = -1;
            out.push_back(std::move(sub));
        }
    } else if (animCount > 0) {
        FileItem sub{};
        sub.type = FileItem::SubMesh;
        sub.name = std::string("Animations: ") + std::to_string(animCount) + " take(s) (reimport for names)";
        sub.path = MakeSubAssetVirtualPath(animVirtualBase, kSubAnimToken, 0);
        sub.ext = ".animclip3d";
        sub.parentPath = modelPath;
        sub.mtimeNs = childMtime;
        sub.slotIndex = -1;
        out.push_back(std::move(sub));
    }
}

std::vector<ProjectPanel::FileItem> *ProjectPanel::GetProjectItems(const std::string &path, DirSnapshot *snapshot)
{
    if (!snapshot)
        snapshot = GetDirSnapshot(path);
    if (!snapshot)
        return nullptr;

    const uint64_t projectionRevision = m_modelTreeProjection.Revision();
    if (m_projectItemsCacheSource != ProjectItemsCacheSource::None && m_projectItemsCachePath == path &&
        m_projectItemsCacheDirectoryRevision == m_directoryRevision &&
        m_projectItemsCacheProjectionRevision == projectionRevision &&
        m_projectItemsCacheSnapshotMtime == snapshot->mtimeNs) {
        if (m_projectItemsCacheSource == ProjectItemsCacheSource::Snapshot)
            return &snapshot->items;
        if (auto cached = m_augmentedCache.find(path); cached != m_augmentedCache.end())
            return &cached->second.items;
    }

    ++m_projectItemsRebuilds;
    m_projectItemsCachePath = path;
    m_projectItemsCacheDirectoryRevision = m_directoryRevision;
    m_projectItemsCacheProjectionRevision = projectionRevision;
    m_projectItemsCacheSnapshotMtime = snapshot->mtimeNs;

    if (m_modelTreeProjection.Empty()) {
        m_projectItemsCacheSource = ProjectItemsCacheSource::Snapshot;
        return &snapshot->items;
    }

    // Check if any expanded models in current items
    std::vector<std::string> expandedPaths;
    expandedPaths.reserve(snapshot->modelPaths.size());
    for (const auto &modelPath : snapshot->modelPaths) {
        if (m_modelTreeProjection.IsExpanded(modelPath))
            expandedPaths.push_back(modelPath);
    }
    if (expandedPaths.empty()) {
        m_projectItemsCacheSource = ProjectItemsCacheSource::Snapshot;
        return &snapshot->items;
    }

    auto cacheIt = m_augmentedCache.find(path);
    if (cacheIt != m_augmentedCache.end() && cacheIt->second.mtimeNs == snapshot->mtimeNs &&
        cacheIt->second.expandedPaths == expandedPaths) {
        m_projectItemsCacheSource = ProjectItemsCacheSource::Augmented;
        return &cacheIt->second.items;
    }

    auto &cached = m_augmentedCache[path];
    cached.mtimeNs = snapshot->mtimeNs;
    cached.expandedPaths = expandedPaths;
    cached.items.clear();
    cached.items.reserve(snapshot->items.size() + 8);

    std::unordered_set<std::string> expandedSet(expandedPaths.begin(), expandedPaths.end());
    for (const auto &item : snapshot->items) {
        cached.items.push_back(item);
        if (item.type == FileItem::File && IsModelExt(item.ext) && expandedSet.count(item.path) > 0)
            AppendModelSubAssets(cached.items, m_assetDatabase, item);
    }
    for (auto &item : cached.items) {
        if (item.selectionKey.empty())
            item.selectionKey = AssetSelectionPathKey(item.path);
    }
    m_projectItemsCacheSource = ProjectItemsCacheSource::Augmented;
    return &cached.items;
}

// ════════════════════════════════════════════════════════════════════
// Thumbnail system
// ════════════════════════════════════════════════════════════════════

uint64_t ProjectPanel::GetMaterialMtimeNs(const std::string &filePath)
{
    if (filePath.empty())
        return 0;

    double now = m_frameTimeNow;

    auto it = m_materialMtimeCache.find(filePath);
    if (it != m_materialMtimeCache.end() && (now - it->second.second) < 1.0)
        return it->second.first;

    std::string diskPath = filePath;
    const auto subPos = filePath.find(kSubMatToken);
    if (subPos != std::string::npos)
        diskPath = filePath.substr(0, subPos);

    std::error_code ec;
    if (!fs::exists(fs::u8path(diskPath), ec))
        return 0;

    uint64_t mtimeNs = GetMtimeNs(diskPath);
    m_materialMtimeCache[filePath] = {mtimeNs, now};
    return mtimeNs;
}

uint64_t ProjectPanel::GetTextureMtimeNs(const std::string &filePath)
{
    if (filePath.empty())
        return 0;

    double now = m_frameTimeNow;

    auto it = m_textureMtimeCache.find(filePath);
    if (it != m_textureMtimeCache.end() && (now - it->second.second) < 1.0)
        return it->second.first;

    std::error_code ec;
    if (!fs::exists(fs::u8path(filePath), ec))
        return 0;

    uint64_t imageMtime = GetMtimeNs(filePath);

    // Also watch the .meta file so that import setting changes (filter_mode, max_size, srgb…)
    // invalidate the cached thumbnail.
    uint64_t metaMtime = 0;
    std::string metaPath = InxResourceMeta::GetMetaFilePath(filePath);
    if (!metaPath.empty() && fs::exists(fs::u8path(metaPath), ec))
        metaMtime = GetMtimeNs(metaPath);

    // Combine both mtimes into a single fingerprint that changes when either changes.
    uint64_t combined = imageMtime ^ (metaMtime * UINT64_C(2654435761));
    m_textureMtimeCache[filePath] = {combined, now};
    return combined;
}

uint64_t ProjectPanel::GetThumbnail(const std::string &filePath, uint64_t cachedMtimeNs)
{
    if (filePath.empty() || !m_engine)
        return 0;

    uint64_t texMtime = cachedMtimeNs;
    // The directory catalog carries the source fingerprint, while the
    // throttled mtime cache also includes the sidecar.  Use the latter as the
    // contract key so an external import-setting edit invalidates immediately
    // after the existing one-second filesystem validation window.
    const uint64_t observedStamp = GetTextureMtimeNs(filePath);
    if (observedStamp != 0)
        texMtime = observedStamp;
    if (texMtime == 0)
        return 0;

    const uint64_t catalogGeneration = m_assetDatabase ? m_assetDatabase->GetQueryGeneration() : 0;
    auto &contract = m_texturePreviewContracts[filePath];
    if (contract.catalogGeneration != catalogGeneration || contract.contentStamp != texMtime) {
        contract = TexturePreviewContract{};
        contract.catalogGeneration = catalogGeneration;
        contract.contentStamp = texMtime;

        // Read the complete preview-affecting import contract from .meta only
        // when its directory/asset revision or content fingerprint changed.
        if (m_assetDatabase) {
            const auto meta = m_assetDatabase->GetMetaByPath(filePath);
            if (meta) {
                if (meta->HasKey("filter_mode")) {
                    std::string fm = meta->GetDataAs<std::string>("filter_mode");
                    contract.nearest = (fm == "point" || fm == "nearest");
                }
                if (meta->HasKey("srgb"))
                    contract.srgb = meta->GetDataAs<bool>("srgb");
                if (meta->HasKey("max_size"))
                    contract.maxSize = meta->GetDataAs<int>("max_size");
                if (meta->HasKey("texture_format"))
                    contract.textureFormat = meta->GetDataAs<std::string>("texture_format");
                if (meta->HasKey("texture_type"))
                    contract.textureType = meta->GetDataAs<std::string>("texture_type");
            }
        }
    }

    const bool nearest = contract.nearest;
    const bool srgb = contract.srgb;
    const int maxSize = contract.maxSize;
    const std::string &textureFormat = contract.textureFormat;
    const std::string &textureType = contract.textureType;
    // Import settings affect the generated pixels even when the source file
    // itself is unchanged. Fold only the settings used by this preview path
    // into the generation fingerprint.
    texMtime ^= nearest ? UINT64_C(0x9e3779b97f4a7c15) : 0;
    texMtime ^= srgb ? UINT64_C(0xc2b2ae3d27d4eb4f) : 0;

    const std::string resourceKey = std::string("tex|") + filePath;
    const uint64_t readyTexture = m_engine->GetTexturePreviewTextureId(resourceKey);
    if (readyTexture != 0)
        return readyTexture;
    if (m_texturePreviewRequestsThisFrame >= kTexturePreviewRequestBudget)
        return 0;

    uint64_t fingerprint = texMtime;
    fingerprint ^= nearest ? UINT64_C(0x9e3779b97f4a7c15) : 0;
    fingerprint ^= srgb ? UINT64_C(0xc2b2ae3d27d4eb4f) : 0;
    fingerprint ^= std::hash<std::string>{}(textureFormat) + UINT64_C(0x517cc1b727220a95);
    fingerprint ^= std::hash<std::string>{}(textureType) + UINT64_C(0x6eed0e9da4d94a4f);
    auto &request = m_texturePreviewRequests[resourceKey];
    if (request.fingerprint == fingerprint && m_previewFrameSerial - request.lastRequestFrame < 30)
        return 0;
    request.fingerprint = fingerprint;
    request.lastRequestFrame = m_previewFrameSerial;
    ++m_previewScheduleAttempts;
    ++m_texturePreviewRequestsThisFrame;
    // pump=false: PreRender already pumped once this frame.
    auto [texId, w, h] = m_engine->QueryOrScheduleTexturePreview(resourceKey, filePath, texMtime, nearest, srgb,
                                                                 maxSize, textureFormat, textureType, false, false);
    return texId;
}

uint64_t ProjectPanel::GetMaterialThumbnail(const std::string &filePath, uint64_t cachedMtimeNs)
{
    if (filePath.empty() || !m_engine)
        return 0;

    const std::string resourceKey = std::string("mat|") + filePath;
    uint64_t mtimeNs = cachedMtimeNs != 0 ? cachedMtimeNs : GetMaterialMtimeNs(filePath);
    if (mtimeNs == 0) {
        // Transient stat failure (e.g. during an atomic .mat save: tmp + rename
        // briefly leaves the file missing). Keep showing the last-rendered
        // preview instead of flickering to the placeholder icon, and don't
        // reschedule until the file resolves again.
        return m_engine->GetMaterialPreviewTextureId(resourceKey);
    }

    auto &result = m_materialPreviewResults[resourceKey];
    const uint64_t publishedTexture = m_engine->GetMaterialPreviewTextureId(resourceKey);
    const bool previewReady = publishedTexture != 0 && m_engine->IsMaterialPreviewReady(resourceKey);
    if (result.fingerprint == mtimeNs && publishedTexture == result.textureId) {
        // Stable ready state: stay on the read-only lookup path and avoid
        // re-entering QueryOrScheduleMaterialPreview on every UI build.
        if (previewReady)
            return publishedTexture;
        if (m_previewFrameSerial - result.lastRequestFrame < 30)
            return publishedTexture;
    }
    if (result.fingerprint == mtimeNs && previewReady && publishedTexture != 0 &&
        publishedTexture != result.textureId) {
        // Inspector authoring may publish a new live texture without changing
        // the disk mtime. Adopt it immediately without scheduling a duplicate.
        result.textureId = publishedTexture;
        result.lastRequestFrame = m_previewFrameSerial;
        return publishedTexture;
    }

    // Material authoring can publish an in-memory JSON generation before the
    // asynchronous file mtime changes. Always enter the engine's existing
    // generation/in-flight deduplication path so Project and Prefab previews
    // observe that revision immediately.
    const uint64_t textureId = m_engine->QueryOrScheduleMaterialPreview(resourceKey, filePath, "", mtimeNs);
    result.fingerprint = mtimeNs;
    result.textureId = textureId;
    result.lastRequestFrame = m_previewFrameSerial;
    ++m_previewScheduleAttempts;
    return textureId;
}

uint64_t ProjectPanel::GetEmbeddedMaterialThumbnail(const FileItem &item)
{
    if (item.path.empty() || item.slotIndex < 0 || !m_engine)
        return 0;

    // Sub-asset rows inherit the parent model's mtime, but model files are NOT
    // stamped in the dir snapshot (only image/material files are), so item.mtimeNs
    // is 0 here.  Passing 0 as the stamp leaves the preview state's generation at 0
    // and no render is ever scheduled.  GetMaterialMtimeNs strips the "::submat:"
    // token and stats the underlying model file (cached, 1s TTL), giving a non-zero
    // stamp so the embedded material preview actually renders — same path as .mat.
    uint64_t stamp = GetMaterialMtimeNs(item.path);
    if (stamp == 0)
        return 0; // parent model file missing/unreadable

    const std::string resourceKey = std::string("mat|") + item.path;
    auto &result = m_materialPreviewResults[resourceKey];
    const uint64_t publishedTexture = m_engine->GetMaterialPreviewTextureId(resourceKey);
    const bool previewReady = publishedTexture != 0 && m_engine->IsMaterialPreviewReady(resourceKey);
    if (result.fingerprint == stamp && publishedTexture == result.textureId) {
        if (previewReady)
            return publishedTexture;
        if (m_previewFrameSerial - result.lastRequestFrame < 30)
            return publishedTexture;
    }
    if (result.fingerprint == stamp && previewReady && publishedTexture != 0 && publishedTexture != result.textureId) {
        result.textureId = publishedTexture;
        result.lastRequestFrame = m_previewFrameSerial;
        return publishedTexture;
    }
    // The shared preview state owns generation and pending-request coalescing.
    // Do not add a UI-frame throttle here: embedded/material authoring may
    // advance independently of the backing file timestamp.
    const uint64_t textureId = m_engine->QueryOrScheduleMaterialPreview(resourceKey, item.path, "", stamp);
    result.fingerprint = stamp;
    result.textureId = textureId;
    result.lastRequestFrame = m_previewFrameSerial;
    ++m_previewScheduleAttempts;
    return textureId;
}

uint64_t ProjectPanel::GetModelThumbnail(const std::string &filePath, uint64_t cachedMtimeNs)
{
    if (filePath.empty() || !m_engine)
        return 0;

    double now = m_frameTimeNow;

    uint64_t mtimeNs = cachedMtimeNs;
    if (mtimeNs == 0) {
        auto it = m_modelMtimeCache.find(filePath);
        if (it != m_modelMtimeCache.end() && (now - it->second.second) < 1.0) {
            mtimeNs = it->second.first;
        } else {
            std::error_code ec;
            if (!fs::exists(fs::u8path(filePath), ec))
                return 0;
            mtimeNs = GetMtimeNs(filePath);
            m_modelMtimeCache[filePath] = {mtimeNs, now};
        }
    }
    if (mtimeNs == 0)
        return 0;

    const std::string resourceKey = std::string("mesh|") + filePath;
    const uint64_t readyTexture = m_engine->GetMeshPreviewTextureId(resourceKey);
    if (readyTexture != 0)
        return readyTexture;
    if (m_modelPreviewRequestsThisFrame >= kModelPreviewRequestBudget)
        return 0;

    auto &request = m_modelPreviewRequests[resourceKey];
    if (request.fingerprint == mtimeNs && m_previewFrameSerial - request.lastRequestFrame < 30)
        return 0;
    request.fingerprint = mtimeNs;
    request.lastRequestFrame = m_previewFrameSerial;
    ++m_previewScheduleAttempts;
    ++m_modelPreviewRequestsThisFrame;
    return m_engine->QueryOrScheduleMeshPreview(resourceKey, filePath, mtimeNs);
}

uint64_t ProjectPanel::GetModel3dIconId() const
{
    auto it = m_typeIconCache.find("model_3d");
    return it != m_typeIconCache.end() ? it->second : 0;
}

bool ProjectPanel::IsUiPrefabFile(const std::string &filePath, uint64_t cachedMtimeNs)
{
    if (filePath.empty())
        return false;

    uint64_t mtimeNs = cachedMtimeNs != 0 ? cachedMtimeNs : GetMtimeNs(filePath);
    auto cached = m_prefabTypeCache.find(filePath);
    if (cached != m_prefabTypeCache.end() && cached->second.mtimeNs == mtimeNs)
        return cached->second.isUi;

    std::ifstream input(fs::u8path(filePath), std::ios::binary);
    if (!input.is_open())
        return false;

    nlohmann::json prefabJson = nlohmann::json::parse(input, nullptr, false);
    if (prefabJson.is_discarded())
        return false;

    auto rootIt = prefabJson.find("root_object");
    if (rootIt == prefabJson.end() || !rootIt->is_object())
        return false;

    bool hasUi = false;
    bool hasSceneMesh = false;

    const auto isUiComponent = [](const std::string &type) {
        return type == "UICanvas" || type == "UIText" || type == "UIButton" || type == "UIImage" ||
               type == "InxUIComponent" || type == "InxUIScreenComponent" || type == "InxUISelectable";
    };
    const auto isSceneMeshComponent = [](const std::string &type) {
        return type == "MeshRenderer" || type == "SkinnedMeshRenderer" || type == "SpriteRenderer";
    };

    std::function<void(const nlohmann::json &)> walk = [&](const nlohmann::json &node) {
        auto componentsIt = node.find("components");
        if (componentsIt != node.end() && componentsIt->is_array()) {
            for (const auto &componentJson : *componentsIt) {
                if (!componentJson.is_object())
                    continue;
                std::string type = componentJson.value("type_id", componentJson.value("type", std::string()));
                const size_t typeSeparator = type.find_last_of(".:");
                if (typeSeparator != std::string::npos)
                    type = type.substr(typeSeparator + 1);
                if (isUiComponent(type))
                    hasUi = true;
                if (isSceneMeshComponent(type))
                    hasSceneMesh = true;
            }
        }
        auto childrenIt = node.find("children");
        if (childrenIt != node.end() && childrenIt->is_array()) {
            for (const auto &childJson : *childrenIt)
                walk(childJson);
        }
    };

    walk(*rootIt);
    // UI-only prefabs (Canvas / widgets without a mesh renderer) use the static icon.
    const bool isUi = hasUi && !hasSceneMesh;
    m_prefabTypeCache[filePath] = {mtimeNs, isUi};
    return isUi;
}

void ProjectPanel::ProcessPendingThumbnails()
{
    if (!m_engine)
        return;
    m_engine->PumpPreviewTasks();
}

// ════════════════════════════════════════════════════════════════════
// File-type icon system
// ════════════════════════════════════════════════════════════════════

void ProjectPanel::EnsureTypeIconsLoaded()
{
    if (!m_renderer || m_iconsDir.empty())
        return;

    if (m_typeIconsLoaded) {
        for (auto it = m_pendingTypeIconIds.begin(); it != m_pendingTypeIconIds.end();) {
            const uint64_t textureId = m_renderer->GetImGuiTextureId("__typeicon__" + *it);
            if (textureId == 0) {
                ++it;
                continue;
            }
            m_typeIconCache[*it] = textureId;
            it = m_pendingTypeIconIds.erase(it);
        }
        return;
    }

    std::unordered_set<std::string> needed;
    for (auto &[_, iconName] : GetIconMap())
        needed.insert(iconName);
    needed.insert("model_expand_open");
    needed.insert("model_expand_closed");

    std::error_code ec;
    for (auto &iconKey : needed) {
        std::string texName = "__typeicon__" + iconKey;
        if (m_renderer->HasImGuiTexture(texName)) {
            const uint64_t textureId = m_renderer->GetImGuiTextureId(texName);
            m_typeIconCache[iconKey] = textureId;
            if (textureId == 0)
                m_pendingTypeIconIds.insert(iconKey);
            continue;
        }

        fs::path iconFs = fs::u8path(m_iconsDir) / (iconKey + ".png");
        if (!fs::is_regular_file(iconFs, ec))
            continue;

        auto iconPath = infernux::FromFsPath(iconFs);
        auto texData = InxTextureLoader::LoadFromFile(iconPath);
        if (!texData.IsValid())
            continue;

        (void)m_renderer->SubmitTextureForImGui(texName, texData.pixels.data(), texData.pixels.size(), texData.width,
                                                texData.height, VK_FILTER_LINEAR, true);
        const uint64_t textureId = m_renderer->GetImGuiTextureId(texName);
        m_typeIconCache[iconKey] = textureId;
        if (textureId == 0)
            m_pendingTypeIconIds.insert(iconKey);
    }

    m_typeIconsLoaded = true;
}

uint64_t ProjectPanel::GetTypeIconId(const FileItem &item) const
{
    const std::string *key = nullptr;
    auto &iconMap = GetIconMap();

    if (item.type == FileItem::Dir) {
        auto sit = iconMap.find("__dir__");
        key = sit != iconMap.end() ? &sit->second : nullptr;
    } else if (item.type == FileItem::SubMesh) {
        auto mapIt = iconMap.find(item.ext.empty() ? ".fbx" : item.ext);
        key = mapIt != iconMap.end() ? &mapIt->second : nullptr;
    } else if (item.type == FileItem::SubMaterial) {
        auto sit = iconMap.find(".mat");
        key = sit != iconMap.end() ? &sit->second : nullptr;
    } else {
        auto mapIt = iconMap.find(item.ext);
        key = mapIt != iconMap.end() ? &mapIt->second : nullptr;
    }

    if (key) {
        auto it = m_typeIconCache.find(*key);
        if (it != m_typeIconCache.end())
            return it->second;
        // A mapped type whose icon asset is missing (e.g. model/prefab, which have no
        // type icon and rely on their rendered preview). Return 0 so the caller keeps
        // showing the preview / type tag — NOT the generic file.png placeholder.
        return 0;
    }
    // Truly unmapped extension → generic file.png placeholder instead of "[FILE]".
    if (item.type == FileItem::File) {
        auto f = m_typeIconCache.find("file");
        if (f != m_typeIconCache.end())
            return f->second;
    }
    return 0;
}

// ════════════════════════════════════════════════════════════════════
// Label layout cache
// ════════════════════════════════════════════════════════════════════

float ProjectPanel::GetGridTextLineHeight(InxGUIContext *ctx)
{
    if (m_gridTextLineHeight <= 0.0f)
        m_gridTextLineHeight = std::max(ImGui::GetTextLineHeight(), 14.0f);
    return m_gridTextLineHeight;
}

const ProjectPanel::LabelEntry &ProjectPanel::GetCachedItemLabel(InxGUIContext *ctx, const FileItem &item,
                                                                 float textRegionW)
{
    LabelCacheKey key;
    key.path = item.path;
    key.name = item.name;
    key.type = static_cast<uint8_t>(item.type);
    // Model expand/collapse is shown with a side button, not in the string (avoids missing glyphs).
    key.expanded = false;
    key.widthPx = static_cast<int>(textRegionW);

    auto it = m_labelCache.find(key);
    if (it != m_labelCache.end())
        return it->second;

    // Build display name
    std::string nameDisplay = item.name;
    if (item.type == FileItem::File) {
        auto dot = nameDisplay.rfind('.');
        if (dot != std::string::npos)
            nameDisplay = nameDisplay.substr(0, dot);
    } else if (item.type == FileItem::SubMesh) {
        std::string subLabel = item.name;
        if (item.ext == ".animclip3d" && !subLabel.empty()) {
            auto d = subLabel.rfind('.');
            if (d != std::string::npos) {
                std::string stem = subLabel.substr(0, d);
                std::string ext = subLabel.substr(d);
                stem = StripPipeDisplaySuffix(stem);
                subLabel = std::move(stem) + ext;
            }
        }
        nameDisplay = std::string("  ") + subLabel;
    } else if (item.type == FileItem::SubMaterial) {
        nameDisplay = std::string("  ") + item.name;
    }

    static constexpr const char *kEllipsisAscii = "...";
    float maxTextW = textRegionW - 4.0f;
    float textW = ctx->CalcTextWidth(nameDisplay);
    if (textW > maxTextW) {
        // Truncate with ASCII ellipsis
        std::string truncated = nameDisplay;
        while (!truncated.empty()) {
            infernux::textlayout::PopBackUtf8Codepoint(truncated);
            float tw = ctx->CalcTextWidth(std::string(truncated) + kEllipsisAscii);
            if (tw <= maxTextW) {
                nameDisplay = truncated + kEllipsisAscii;
                textW = tw;
                break;
            }
        }
        if (truncated.empty()) {
            nameDisplay = kEllipsisAscii;
            textW = ctx->CalcTextWidth(nameDisplay);
        }
    }

    float offsetX = std::max((textRegionW - textW) * 0.5f, 0.0f);

    LabelEntry entry{std::move(nameDisplay), offsetX};
    if (m_labelCache.size() > 4096)
        m_labelCache.clear();
    auto [insertIt, _] = m_labelCache.emplace(std::move(key), std::move(entry));
    return insertIt->second;
}

// ════════════════════════════════════════════════════════════════════
// Grid layout — virtual scrolling
// ════════════════════════════════════════════════════════════════════

ProjectPanel::GridRange ProjectPanel::GetVisibleGridRange(InxGUIContext *ctx, int itemCount, int cols, float rowHeight,
                                                          float startY) const
{
    if (itemCount <= 0 || cols <= 0 || rowHeight <= 0.0f)
        return {0, itemCount, 0.0f, 0.0f};

    float scrollY = std::max(ctx->GetScrollY() - startY, 0.0f);
    float viewportH = std::max(ctx->GetContentRegionAvailHeight(), rowHeight);
    int totalRows = (itemCount + cols - 1) / cols;
    int firstRow = std::max(static_cast<int>(scrollY / rowHeight), 0);
    int visibleRows = std::max(static_cast<int>(viewportH / rowHeight) + 2, 1);
    int lastRow = std::min(totalRows, firstRow + visibleRows);

    GridRange r;
    r.topSpacer = firstRow * rowHeight;
    r.bottomSpacer = std::max(totalRows - lastRow, 0) * rowHeight;
    r.startIndex = firstRow * cols;
    r.endIndex = std::min(itemCount, lastRow * cols);
    return r;
}

// ════════════════════════════════════════════════════════════════════
// Click & keyboard input
// ════════════════════════════════════════════════════════════════════

bool ProjectPanel::IsCtrl(InxGUIContext *ctx) const
{
    return ctx->IsKeyDown(kKeyLeftCtrl) || ctx->IsKeyDown(kKeyRightCtrl);
}

bool ProjectPanel::IsShift(InxGUIContext *ctx) const
{
    return ctx->IsKeyDown(kKeyLeftShift) || ctx->IsKeyDown(kKeyRightShift);
}

void ProjectPanel::HandleItemClick(const FileItem &item, InxGUIContext *ctx)
{
    double now = m_frameTimeNow;
    const bool nativeDoubleClick = ctx->IsMouseDoubleClicked(0);
    const bool repeatedClick =
        AssetSelectionPathKey(m_lastClickedFile) == AssetSelectionPathKey(item.path) && (now - m_lastClickTime) < 0.4;
    const bool doubleClicked = nativeDoubleClick || repeatedClick;
    m_lastClickedFile = item.path;
    m_lastClickTime = now;

    bool ctrl = IsCtrl(ctx);
    bool shift = IsShift(ctx);

    if (ctrl && !doubleClicked) {
        std::vector<std::string> proposed = m_selectedFiles;
        const std::string itemKey = AssetSelectionPathKey(item.path);
        auto it = std::find_if(proposed.begin(), proposed.end(),
                               [&itemKey](const std::string &path) { return AssetSelectionPathKey(path) == itemKey; });
        if (it != proposed.end())
            proposed.erase(it);
        else
            proposed.push_back(item.path);
        PublishSelectionIntent(proposed, proposed.empty() ? "" : proposed.back());
        return;
    }

    if (shift && !doubleClicked && !m_selectedFile.empty() && m_visibleItems) {
        int anchorIdx = -1, targetIdx = -1;
        const std::string anchorKey = AssetSelectionPathKey(m_selectedFile);
        const std::string targetKey = AssetSelectionPathKey(item.path);
        for (int i = 0; i < static_cast<int>(m_visibleItems->size()); ++i) {
            auto &vi = (*m_visibleItems)[i];
            const std::string visibleKey = AssetSelectionPathKey(vi.path);
            if (visibleKey == anchorKey)
                anchorIdx = i;
            if (visibleKey == targetKey)
                targetIdx = i;
        }
        if (anchorIdx >= 0 && targetIdx >= 0) {
            int lo = std::min(anchorIdx, targetIdx);
            int hi = std::max(anchorIdx, targetIdx);
            std::vector<std::string> proposed;
            proposed.reserve(static_cast<size_t>(hi - lo + 1));
            for (int i = lo; i <= hi; ++i)
                proposed.push_back((*m_visibleItems)[i].path);
            PublishSelectionIntent(proposed, item.path);
            return;
        }
    }

    // Normal single select
    PublishSelectionIntent({item.path}, item.path);

    if (item.type == FileItem::Dir) {
        if (doubleClicked) {
            if (RequestDirectoryNavigation(item.path))
                m_lastClickedFile.clear();
        }
    } else if (item.type == FileItem::SubMesh || item.type == FileItem::SubMaterial) {
        // Sub-assets: select only
    } else if (doubleClicked) {
        std::string openKind = "system";
        if (item.ext == ".scene") {
            openKind = "scene";
        } else if (item.ext == ".prefab") {
            openKind = "prefab";
        } else if (item.ext == ".animclip2d") {
            openKind = "animation_clip";
        } else if (item.ext == ".animclip3d") {
            // 3D clips are edited via the Inspector (Python asset_details_renderer).
            return;
        } else if (item.ext == ".animfsm") {
            openKind = "animation_fsm";
        } else if (item.ext == ".particlegraph") {
            openKind = "particle_graph";
        } else if (item.ext == ".animtimeline") {
            openKind = "timeline";
        } else if (item.ext == ".timelinefsm") {
            openKind = "timeline_fsm";
        } else if (item.ext == ".inxpkg") {
            openKind = "inxpackage";
        }
        ExecuteEditorCommand("asset.open", MakeAssetOpenCommandArgument(openKind, item.path), "pointer");
    }
}

bool ProjectPanel::HasSelectedAssets() const
{
    return !GetSelectedPaths().empty();
}

bool ProjectPanel::CanRenameSelectedAsset(const std::string &path) const
{
    const std::string target = path.empty() ? m_selectedFile : path;
    if (target.empty() || IsVirtualSubAssetPath(target))
        return false;
    // An explicit command target is authoritative. Creation commands reach
    // this function before the native grid has necessarily consumed the new
    // global selection, so an older local selection must not reject rename.
    if (!path.empty())
        return true;
    const auto selected = GetSelectedPaths();
    return selected.size() == 1 && FilesystemPathKey(selected.front()) == FilesystemPathKey(target);
}

bool ProjectPanel::BeginRenameSelectedAsset(const std::string &path)
{
    const std::string target = path.empty() ? m_selectedFile : path;
    if (!CanRenameSelectedAsset(path))
        return false;
    if (!path.empty()) {
        PublishSelectionIntent({target}, target);
    }
    BeginRename(target);
    return true;
}

bool ProjectPanel::ExecuteEditorCommand(const std::string &commandId, const std::string &argument,
                                        const std::string &source) const
{
    return executeCommand && executeCommand(commandId, source, argument);
}

bool ProjectPanel::RequestDirectoryNavigation(const std::string &path, const std::string &source) const
{
    return ExecuteEditorCommand("project.navigate_directory", path, source);
}

bool ProjectPanel::RequestAssetLocation(const std::string &path, const std::string &source) const
{
    return ExecuteEditorCommand("project.locate_asset", path, source);
}

void ProjectPanel::HandleExternalFileDrops()
{
    // This is handled via callback from Python's InputManager binding
    // since InputManager singleton access is simpler from Python.
    // The Python bootstrap wiring handles this.
}

void ProjectPanel::ReceiveDroppedFiles(const std::vector<std::string> &paths)
{
    ImportExternalAssetBatch(paths, "drag_drop");
}

bool ProjectPanel::ImportExternalAssetBatch(const std::vector<std::string> &paths, const std::string &source)
{
    if (paths.empty() || m_currentPath.empty())
        return false;
    return ExecuteEditorCommand("asset.import_external", MakeAssetImportCommandArgument(paths, m_currentPath), source);
}

// ════════════════════════════════════════════════════════════════════
// Rename
// ════════════════════════════════════════════════════════════════════

void ProjectPanel::BeginRename(const std::string &path)
{
    if (path.empty())
        return;
    // Virtual sub-assets are not real files — renaming them would be meaningless.
    if (IsVirtualSubAssetPath(path))
        return;

    m_renamingPath = path;
    auto name = infernux::FromFsPath(fs::u8path(path).filename());
    std::error_code ec;
    if (fs::is_regular_file(fs::u8path(path), ec)) {
        auto stem = infernux::FromFsPath(fs::u8path(path).stem());
        if (!stem.empty())
            name = stem;
    }
    std::strncpy(m_renameBuf, name.c_str(), sizeof(m_renameBuf) - 1);
    m_renameBuf[sizeof(m_renameBuf) - 1] = '\0';
    m_renameFocusRequested = true;
    m_renameSkipDeactivateFrames = 2;
    BeginTransientInteraction("rename", "inline_rename", 100, [this]() {
        CancelRename();
        return true;
    });
}

void ProjectPanel::CommitRename()
{
    if (m_renamingPath.empty()) {
        CancelRename();
        return;
    }

    std::string newName(m_renameBuf);
    if (newName.empty()) {
        CancelRename();
        return;
    }

    if (ExecuteEditorCommand("asset.rename", MakeAssetRenameCommandArgument(m_renamingPath, newName), "inline_edit")) {
        // The command publishes one AssetRelocationChange. SelectionService
        // remaps selection and the Project relocation projection remaps the
        // current directory. Do not derive either result a second time here.
        m_pendingCacheInvalidation = true;
    }
    m_renamingPath.clear();
    m_renameSkipDeactivateFrames = 0;
    EndTransientInteraction("rename");
}

void ProjectPanel::CancelRename()
{
    m_renamingPath.clear();
    m_renameSkipDeactivateFrames = 0;
    EndTransientInteraction("rename");
}

// ════════════════════════════════════════════════════════════════════
// Clipboard
// ════════════════════════════════════════════════════════════════════

/// Retrieve file paths from the OS clipboard (CF_HDROP on Windows).
std::vector<std::string> ProjectPanel::GetOSClipboardFiles() const
{
    std::vector<std::string> result;
#ifdef INX_PLATFORM_WINDOWS
    if (!OpenClipboard(nullptr))
        return result;

    HANDLE hData = GetClipboardData(CF_HDROP);
    if (hData) {
        HDROP hDrop = static_cast<HDROP>(hData);
        UINT fileCount = DragQueryFileW(hDrop, 0xFFFFFFFF, nullptr, 0);
        for (UINT i = 0; i < fileCount; ++i) {
            UINT len = DragQueryFileW(hDrop, i, nullptr, 0);
            if (len == 0)
                continue;
            std::wstring wpath(len + 1, L'\0');
            DragQueryFileW(hDrop, i, wpath.data(), len + 1);
            wpath.resize(len);
            auto u8str = Utf8FromWidePath(wpath);
            if (!u8str.empty())
                result.push_back(std::move(u8str));
        }
    }
    CloseClipboard();
#endif
    return result;
}

// ════════════════════════════════════════════════════════════════════
// Move helpers
// ════════════════════════════════════════════════════════════════════

std::vector<std::string> ProjectPanel::GetDragMoveSources(const std::string &draggedPath) const
{
    auto selected = GetSelectedPaths();
    std::vector<std::string> candidates;
    if (std::find(selected.begin(), selected.end(), draggedPath) != selected.end())
        candidates = selected;
    else
        candidates = {draggedPath};

    // Remove ancestors
    std::sort(candidates.begin(), candidates.end(), [](const std::string &a, const std::string &b) {
        auto sepA = std::count(a.begin(), a.end(), '\\') + std::count(a.begin(), a.end(), '/');
        auto sepB = std::count(b.begin(), b.end(), '\\') + std::count(b.begin(), b.end(), '/');
        if (sepA != sepB)
            return sepA < sepB;
        return a.size() < b.size();
    });

    std::vector<std::string> kept;
    for (auto &p : candidates) {
        bool subsumed = false;
        for (auto &k : kept) {
            if (IsFilesystemPathWithin(p, k)) {
                subsumed = true;
                break;
            }
        }
        if (!subsumed)
            kept.push_back(p);
    }
    return kept;
}

std::string ProjectPanel::ResolveMovePayloadPath(const std::string &payloadType, const std::string &payload) const
{
    if (payload.empty())
        return "";

    // Check if it's a path-based type
    auto &ddMap = GetDragDropMap();
    auto &gdMap = GetGuidDragDropMap();
    bool isPathType = (payloadType == DRAG_TYPE_PROJECT_ITEM);
    if (!isPathType) {
        for (auto &[_, info] : ddMap)
            if (info.payloadType == payloadType) {
                isPathType = true;
                break;
            }
    }
    if (!isPathType) {
        for (auto &[_, info] : gdMap)
            if (info.pathPayloadType == payloadType) {
                isPathType = true;
                break;
            }
    }

    if (isPathType) {
        std::error_code ec;
        return fs::exists(fs::u8path(payload), ec) ? payload : "";
    }

    // GUID-based type
    bool isGuidType = false;
    for (auto &[_, info] : gdMap)
        if (info.guidPayloadType == payloadType) {
            isGuidType = true;
            break;
        }

    if (isGuidType) {
        std::string path;
        if (getPathFromGuid)
            path = getPathFromGuid(payload);
        else if (m_assetDatabase)
            path = m_assetDatabase->GetPathFromGuid(payload);

        std::error_code ec;
        return (!path.empty() && fs::exists(fs::u8path(path), ec)) ? path : "";
    }

    return "";
}

void ProjectPanel::MoveProjectItemsToFolder(const std::string &targetDir, const std::string &payloadType,
                                            const std::string &payload)
{
    std::error_code ec;
    if (targetDir.empty() || !fs::is_directory(fs::u8path(targetDir), ec))
        return;

    auto draggedPath = ResolveMovePayloadPath(payloadType, payload);
    if (draggedPath.empty())
        return;

    auto sources = GetDragMoveSources(draggedPath);
    // Remove items targeting self
    sources.erase(
        std::remove_if(sources.begin(), sources.end(),
                       [&](const std::string &s) { return FilesystemPathKey(s) == FilesystemPathKey(targetDir); }),
        sources.end());

    if (sources.empty())
        return;

    ExecuteEditorCommand("asset.transfer", MakeAssetTransferCommandArgument(sources, targetDir), "drag_drop");
}

// ════════════════════════════════════════════════════════════════════
// PreRender
// ════════════════════════════════════════════════════════════════════

void ProjectPanel::PreRender(InxGUIContext *ctx)
{
    (void)ctx;
    m_frameTimeNow = std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();

    if (m_currentPath != m_lastNotifiedPath) {
        m_lastNotifiedPath = m_currentPath;
        if (onStateChanged)
            onStateChanged();
    }
}

void ProjectPanel::VisiblePreRender(InxGUIContext *ctx)
{
    const auto preStart = std::chrono::steady_clock::now();
    ++m_previewFrameSerial;
    const auto iconsStart = std::chrono::steady_clock::now();
    EnsureTypeIconsLoaded();
    const auto previewStart = std::chrono::steady_clock::now();
    m_texturePreviewRequestsThisFrame = 0;
    m_modelPreviewRequestsThisFrame = 0;
    ProcessPendingThumbnails();
    const auto otherStart = std::chrono::steady_clock::now();
    GetGridTextLineHeight(ctx);

    const auto preEnd = std::chrono::steady_clock::now();
    m_subPreOther += std::chrono::duration<double, std::milli>(preEnd - otherStart).count();
    m_subPrePreview += std::chrono::duration<double, std::milli>(otherStart - previewStart).count();
    m_subPreIcons += std::chrono::duration<double, std::milli>(previewStart - iconsStart).count();
    m_subPreOther += std::chrono::duration<double, std::milli>(iconsStart - preStart).count();
}

// ════════════════════════════════════════════════════════════════════
// OnRenderContent — main entry
// ════════════════════════════════════════════════════════════════════

void ProjectPanel::OnRenderContent(InxGUIContext *ctx)
{
    const auto contentStart = std::chrono::steady_clock::now();
    // Process any deferred cache invalidation from the previous frame
    // (CommitRename, Delete, Paste, Move / AssetManager invalidate_dir_cache).
    if (m_pendingCacheInvalidation) {
        m_pendingCacheInvalidation = false;
        ClearDirCachesNow();
    }
    if (m_pendingAugmentedCacheInvalidation) {
        m_pendingAugmentedCacheInvalidation = false;
        m_augmentedCache.clear();
        m_projectItemsCacheSource = ProjectItemsCacheSource::None;
        m_projectItemsCachePath.clear();
    }

    const auto breadcrumbStart = std::chrono::steady_clock::now();
    RenderBreadcrumb(ctx);
    ctx->Separator();
    const auto folderStart = std::chrono::steady_clock::now();

    const bool searchActive = m_search.IsActive();

    // Left panel: folder tree (200px)
    if (ctx->BeginChild("FolderTree", 200, 0, false)) {
        RenderFolderTree(ctx);
    }
    ctx->EndChild();
    const auto gridStart = std::chrono::steady_clock::now();

    ctx->SameLine();

    // Right panel: file grid, or project-wide search hits while the Path
    // search box has text.
    ctx->PushStyleVarVec2(ImGuiStyleVar_WindowPadding, 12.0f, 8.0f);
    ctx->PushStyleColor(ImGuiCol_Border, 0.0f, 0.0f, 0.0f, 0.0f); // transparent border
    if (ctx->BeginChild("FileGrid", 0, 0, true)) {
        if (searchActive)
            RenderSearchResults(ctx);
        else
            RenderFileGrid(ctx);
    }
    ctx->EndChild();
    ctx->PopStyleColor(1); // Border
    ctx->PopStyleVar(1);   // WindowPadding
    const auto tailStart = std::chrono::steady_clock::now();

    bool hasSelection = !m_selectedFile.empty() || !m_selectedFiles.empty();
    bool clickedOutsideProject = hasSelection &&
                                 (ImGui::IsMouseClicked(0) || ImGui::IsMouseClicked(1) || ImGui::IsMouseClicked(2)) &&
                                 !ImGui::IsWindowHovered(ImGuiHoveredFlags_RootAndChildWindows) &&
                                 !ImGui::IsAnyItemActive() && !IsMouseOverInspectorWindow();
    if (clickedOutsideProject)
        ClearSelection();
    const auto contentEnd = std::chrono::steady_clock::now();
    m_subBreadcrumb += std::chrono::duration<double, std::milli>(folderStart - breadcrumbStart).count();
    m_subFolderTree += std::chrono::duration<double, std::milli>(gridStart - folderStart).count();
    m_subFileGrid += std::chrono::duration<double, std::milli>(tailStart - gridStart).count();
    m_subTail += std::chrono::duration<double, std::milli>(contentEnd - tailStart).count();
    m_subTail += std::chrono::duration<double, std::milli>(breadcrumbStart - contentStart).count();
}

// ════════════════════════════════════════════════════════════════════
// Breadcrumb
// ════════════════════════════════════════════════════════════════════

void ProjectPanel::RenderBreadcrumb(InxGUIContext *ctx)
{
    if (m_currentPath != m_breadcrumbPath) {
        m_breadcrumbPath = m_currentPath;
        if (!m_rootPath.empty()) {
            std::string relStr;
            if (!infernux::TryMakeRelativeFilesystemPath(m_currentPath, m_rootPath, relStr, true))
                relStr = m_currentPath;
            if (relStr == ".")
                relStr = infernux::FromFsPath(fs::u8path(m_rootPath).filename());
            m_breadcrumbText = "Path: " + relStr;
        } else {
            m_breadcrumbText = "Path: " + m_currentPath;
        }
    }

    constexpr float kSearchWidth = 220.0f;
    constexpr float kSearchGap = 8.0f;
    const float avail = ctx->GetContentRegionAvailWidth();
    const float searchW = (std::min)(kSearchWidth, (std::max)(140.0f, avail * 0.32f));
    const float pathBudget = (std::max)(48.0f, avail - searchW - kSearchGap);

    std::string pathLabel = m_breadcrumbText;
    if (ctx->CalcTextSizeA(pathLabel).first > pathBudget && pathLabel.size() > 4) {
        // Keep the trailing folders readable when the Path row is tight.
        while (pathLabel.size() > 1 && ctx->CalcTextSizeA(std::string("...") + pathLabel).first > pathBudget) {
            infernux::textlayout::EraseFirstUtf8Codepoint(pathLabel);
        }
        pathLabel = "..." + pathLabel;
    }

    ctx->Label(pathLabel);
    ctx->SameLine(0.0f, kSearchGap);
    const float remain = ctx->GetContentRegionAvailWidth();
    if (remain > searchW)
        ctx->SetCursorPosX(ctx->GetCursorPosX() + (remain - searchW));
    ctx->SetNextItemWidth(searchW);
    if (m_focusSearchNextFrame) {
        ctx->SetKeyboardFocusHere();
        m_focusSearchNextFrame = false;
    }
    ctx->InputTextWithHint("##project_search", Tr("project.search_hint"), m_searchBuf, sizeof(m_searchBuf));
    ctx->RecordSemanticItem("text_input", Tr("project.search_hint"), true, "project.search", std::nullopt, std::nullopt,
                            std::string(m_searchBuf));
    (void)m_search.SetQuery(m_searchBuf);
    UpdateSearchResults();
}

void ProjectPanel::ResetAsyncSearch()
{
    ++m_searchRequestSerial;
    m_searchAsyncState->desiredSerial.store(m_searchRequestSerial, std::memory_order_release);
    m_searchDesiredToken = {};
    m_searchResultToken = {};
    m_searchResults.clear();
    m_searchBusy = false;
}

void ProjectPanel::PollSearchCompletion()
{
    std::unique_ptr<SearchAsyncCompletion> completion;
    {
        std::lock_guard lock(m_searchAsyncState->mutex);
        completion = std::move(m_searchAsyncState->completion);
    }
    if (!completion)
        return;

    m_searchJobInFlight = false;
    if (completion->requestSerial != m_searchRequestSerial)
        return;
    if (completion->cancelled)
        return;
    if (!completion->error.empty()) {
        INXLOG_ERROR("Project search worker failed: ", completion->error);
        if (completion->token == m_searchDesiredToken)
            m_searchResultToken = completion->token;
        m_searchBusy = false;
        return;
    }

    const uint64_t generation = m_assetDatabase ? m_assetDatabase->GetQueryGeneration() : 0;
    const std::string folderRoot = !m_preferredNavPath.empty() ? m_preferredNavPath : m_rootPath;
    if (completion->index && completion->indexGeneration == generation && completion->indexRoot == folderRoot) {
        m_searchIndexGeneration = completion->indexGeneration;
        m_searchIndexRoot = completion->indexRoot;
        m_searchIndex = std::move(completion->index);
    }

    if (completion->token != m_searchDesiredToken)
        return;
    m_searchResults = std::move(completion->results);
    m_searchResultToken = completion->token;
    m_searchBusy = false;
}

void ProjectPanel::ScheduleSearch(const EditorSearchToken &token, uint64_t generation, const std::string &folderRoot)
{
    if (m_searchJobInFlight || !m_assetDatabase)
        return;
    if (!JobSystem::IsAvailable()) {
        m_searchResultToken = token;
        m_searchBusy = false;
        return;
    }

    const uint64_t requestSerial = m_searchRequestSerial;
    const std::string normalizedQuery = m_search.NormalizedQuery();
    const std::string projectRoot = m_rootPath;
    const auto cachedIndex = m_searchIndex && m_searchIndexGeneration == generation && m_searchIndexRoot == folderRoot
                                 ? m_searchIndex
                                 : nullptr;
    const auto catalog =
        cachedIndex ? std::shared_ptr<const AssetCatalogSnapshot>{} : m_assetDatabase->GetCatalogSnapshot();
    const auto asyncState = m_searchAsyncState;

    try {
        JobSystem::Get().Schedule(
            [asyncState, cachedIndex, catalog, token, generation, folderRoot, projectRoot, normalizedQuery,
             requestSerial]() {
                auto completion = std::make_unique<SearchAsyncCompletion>();
                completion->requestSerial = requestSerial;
                completion->token = token;
                completion->indexGeneration = generation;
                completion->indexRoot = folderRoot;
                try {
                    const auto cancelled = [&]() {
                        return asyncState->desiredSerial.load(std::memory_order_acquire) != requestSerial;
                    };
                    completion->cancelled = cancelled();
                    std::shared_ptr<const std::vector<SearchIndexEntry>> index = cachedIndex;
                    if (!completion->cancelled && !index) {
                        auto built = std::make_shared<std::vector<SearchIndexEntry>>();
                        std::unordered_set<std::string> folderPaths;
                        const fs::path rootPath = fs::u8path(folderRoot);
                        if (catalog) {
                            size_t assetCount = 0;
                            for (const auto &[directory, entries] : catalog->GetDirectories()) {
                                if (cancelled()) {
                                    completion->cancelled = true;
                                    break;
                                }
                                (void)directory;
                                assetCount += entries.size();
                            }
                            built->reserve(assetCount);

                            size_t visitedAssets = 0;
                            for (const auto &[directory, entries] : catalog->GetDirectories()) {
                                if (completion->cancelled)
                                    break;
                                (void)directory;
                                for (const AssetCatalogEntry &entry : entries) {
                                    if ((++visitedAssets & 0xffu) == 0u && cancelled()) {
                                        completion->cancelled = true;
                                        break;
                                    }
                                    const std::string &path = entry.path;
                                    if (path.empty() || !ProjectPanel::ShouldShow(entry.name))
                                        continue;

                                    std::string rel = path;
                                    std::string relativePath;
                                    if (!projectRoot.empty() &&
                                        infernux::TryMakeRelativeFilesystemPath(path, projectRoot, relativePath, true))
                                        rel = std::move(relativePath);

                                    FileItem item;
                                    item.type = FileItem::File;
                                    item.name = entry.name;
                                    item.path = path;
                                    item.ext = infernux::FromFsPath(fs::u8path(path).extension());
                                    if (!item.ext.empty() && item.ext.front() == '.')
                                        item.ext.erase(item.ext.begin());
                                    item.resourceType = entry.resourceType;

                                    SearchIndexEntry indexed;
                                    indexed.item = std::move(item);
                                    indexed.sortKey = EditorSearchModel::Normalize(entry.name);
                                    indexed.searchKey = indexed.sortKey + "\n" + EditorSearchModel::Normalize(rel);
                                    built->push_back(std::move(indexed));

                                    fs::path parent = fs::u8path(path).parent_path();
                                    while (!folderRoot.empty() && !parent.empty()) {
                                        const std::string parentString = infernux::FromFsPath(parent);
                                        std::string ignored;
                                        if (!infernux::TryMakeRelativeFilesystemPath(parentString, folderRoot, ignored,
                                                                                     true))
                                            break;
                                        if (parent == rootPath)
                                            break;
                                        folderPaths.insert(parentString);
                                        const fs::path next = parent.parent_path();
                                        if (next == parent)
                                            break;
                                        parent = next;
                                    }
                                }
                            }
                        }

                        size_t visitedFolders = 0;
                        for (const std::string &path : folderPaths) {
                            if ((++visitedFolders & 0xffu) == 0u && cancelled()) {
                                completion->cancelled = true;
                                break;
                            }
                            FileItem item;
                            item.type = FileItem::Dir;
                            item.path = path;
                            item.name = infernux::FromFsPath(fs::u8path(path).filename());
                            if (!ProjectPanel::ShouldShow(item.name))
                                continue;

                            std::string rel = path;
                            std::string relativePath;
                            if (!projectRoot.empty() &&
                                infernux::TryMakeRelativeFilesystemPath(path, projectRoot, relativePath, true))
                                rel = std::move(relativePath);

                            SearchIndexEntry indexed;
                            indexed.item = std::move(item);
                            indexed.sortKey = EditorSearchModel::Normalize(indexed.item.name);
                            indexed.searchKey = indexed.sortKey + "\n" + EditorSearchModel::Normalize(rel);
                            built->push_back(std::move(indexed));
                        }

                        if (!completion->cancelled) {
                            std::sort(built->begin(), built->end(),
                                      [](const SearchIndexEntry &left, const SearchIndexEntry &right) {
                                          if (left.item.type != right.item.type)
                                              return left.item.type < right.item.type;
                                          if (left.sortKey != right.sortKey)
                                              return left.sortKey < right.sortKey;
                                          return left.item.path < right.item.path;
                                      });
                            completion->cancelled = cancelled();
                        }
                        if (!completion->cancelled)
                            index = std::move(built);
                    }

                    if (!completion->cancelled)
                        completion->index = index;
                    completion->results.reserve((std::min)(kMaxSearchResults, index ? index->size() : size_t{0}));
                    size_t visitedMatches = 0;
                    if (!completion->cancelled && index) {
                        for (const SearchIndexEntry &indexed : *index) {
                            if ((++visitedMatches & 0xffu) == 0u && cancelled()) {
                                completion->cancelled = true;
                                completion->results.clear();
                                break;
                            }
                            if (indexed.searchKey.find(normalizedQuery) == std::string::npos)
                                continue;
                            completion->results.push_back(indexed.item);
                            if (completion->results.size() == kMaxSearchResults)
                                break;
                        }
                        if (cancelled()) {
                            completion->cancelled = true;
                            completion->results.clear();
                        }
                    }
                } catch (const std::exception &exception) {
                    completion->error = exception.what();
                } catch (...) {
                    completion->error = "unknown exception";
                }

                std::lock_guard lock(asyncState->mutex);
                if (!asyncState->completion || asyncState->completion->requestSerial <= completion->requestSerial)
                    asyncState->completion = std::move(completion);
            },
            JobDomain::Asset, JobPriority::Low);
        m_searchJobInFlight = true;
        m_searchBusy = true;
    } catch (const std::exception &exception) {
        INXLOG_ERROR("Could not schedule Project search: ", exception.what());
        m_searchBusy = false;
    }
}

void ProjectPanel::UpdateSearchResults()
{
    const uint64_t generation = m_assetDatabase ? m_assetDatabase->GetQueryGeneration() : 0;
    const std::string folderRoot = !m_preferredNavPath.empty() ? m_preferredNavPath : m_rootPath;
    const EditorSearchToken token = m_search.MakeToken(generation, folderRoot);
    if (token != m_searchDesiredToken) {
        ++m_searchRequestSerial;
        m_searchAsyncState->desiredSerial.store(m_searchRequestSerial, std::memory_order_release);
        m_searchDesiredToken = token;
        m_searchResults.clear();
        m_searchBusy = m_search.IsActive();
    }
    PollSearchCompletion();
    if (!m_search.IsActive()) {
        m_searchResultToken = token;
        m_searchIndexRoot = folderRoot;
        m_searchBusy = false;
        return;
    }
    if (token == m_searchResultToken)
        return;
    ScheduleSearch(token, generation, folderRoot);
}

void ProjectPanel::RenderSearchResults(InxGUIContext *ctx)
{
    if (m_searchResults.empty()) {
        ctx->Label(Tr(m_searchBusy ? "project.searching" : "project.search_no_results"));
        return;
    }

    FileItem activatedItem;
    bool hasActivatedItem = false;
    ImGuiListClipper clipper;
    clipper.Begin(static_cast<int>(m_searchResults.size()));
    while (!hasActivatedItem && clipper.Step()) {
        for (int itemIndex = clipper.DisplayStart; itemIndex < clipper.DisplayEnd; ++itemIndex) {
            const FileItem &item = m_searchResults[static_cast<size_t>(itemIndex)];
            std::string rel = item.path;
            if (!m_rootPath.empty()) {
                std::string tmp;
                if (infernux::TryMakeRelativeFilesystemPath(item.path, m_rootPath, tmp, true))
                    rel = std::move(tmp);
            }

            const std::string kind =
                (item.type == FileItem::Dir) ? Tr("project.search_folder") : Tr("project.search_asset");
            // Show "Name — relative/path" so users can tell duplicates apart.
            const std::string label = item.name + "  —  " + rel + "  (" + kind + ")##search_" + item.path;
            if (!ctx->Selectable(label, false))
                continue;

            // Keep a stable copy. Clearing m_searchResults while item still refers to
            // one of its elements leaves a dangling reference and made activation
            // intermittently navigate to an invalid path.
            activatedItem = item;
            hasActivatedItem = true;
            break;
        }
    }

    if (!hasActivatedItem)
        return;

    // Search activation is an explicit navigation intent. Only leave search
    // mode after the global interaction core accepts the target.
    const bool accepted = activatedItem.type == FileItem::Dir ? RequestDirectoryNavigation(activatedItem.path)
                                                              : RequestAssetLocation(activatedItem.path);
    if (!accepted)
        return;
    m_searchBuf[0] = '\0';
    (void)m_search.Clear();
    ResetAsyncSearch();
}

// ════════════════════════════════════════════════════════════════════
// Folder tree
// ════════════════════════════════════════════════════════════════════

void ProjectPanel::RebuildFolderTreeRows()
{
    ++m_folderTreeRebuilds;
    m_folderTreeRows.clear();
    m_folderTreeRowsDirectoryRevision = m_directoryRevision;
    m_folderTreeRowsProjectionRevision = m_folderTreeProjection.Revision();
    m_folderTreeAppliedProjectionRevision = UINT64_MAX;

    if (m_rootPath.empty())
        return;

    auto *rootSnapshot = GetDirSnapshot(m_rootPath);
    if (!rootSnapshot)
        return;

    const auto rootName = infernux::FromFsPath(fs::u8path(m_rootPath).filename());
    m_folderTreeRows.push_back({m_rootPath, rootName, rootName + "###" + m_rootPath, FilesystemPathKey(m_rootPath),
                                "project.folder.root", 0, !rootSnapshot->dirs.empty(), true,
                                m_folderTreeProjection.IsExpanded(m_rootPath)});

    std::function<void(const std::string &, DirSnapshot *, int)> appendChildren;
    appendChildren = [&](const std::string &parentPath, DirSnapshot *parentSnapshot, int depth) {
        if (!parentSnapshot || !m_folderTreeProjection.IsExpanded(parentPath))
            return;

        for (const auto &directory : parentSnapshot->dirs) {
            auto *meta = GetDirTreeMeta(directory.path);
            const bool hasSubdirs = meta != nullptr && meta->hasSubdirs;
            m_folderTreeRows.push_back({directory.path, directory.name, directory.name + "###" + directory.path,
                                        FilesystemPathKey(directory.path), MakeProjectFolderSemanticId(directory.path),
                                        depth, hasSubdirs, false, m_folderTreeProjection.IsExpanded(directory.path)});
            if (hasSubdirs && m_folderTreeProjection.IsExpanded(directory.path))
                appendChildren(directory.path, GetDirSnapshot(directory.path), depth + 1);
        }
    };
    appendChildren(m_rootPath, rootSnapshot, 1);
}

void ProjectPanel::RenderFolderTree(InxGUIContext *ctx)
{
    if (m_folderTreeRowsDirectoryRevision != m_directoryRevision ||
        m_folderTreeRowsProjectionRevision != m_folderTreeProjection.Revision())
        RebuildFolderTreeRows();

    if (m_folderTreeRows.empty()) {
        ctx->Label(Tr("project.no_project_path"));
    } else {
        const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
        const float baseX = ctx->GetCursorPosX();
        const float indent = ImGui::GetStyle().IndentSpacing;
        const std::string &currentPathKey = m_currentPathKey;
        const uint64_t projectionRevision = m_folderTreeRowsProjectionRevision;
        const bool syncExpandedState = m_folderTreeAppliedProjectionRevision != projectionRevision;
        auto renderRow = [&](int index) {
            const auto &row = m_folderTreeRows[static_cast<size_t>(index)];
            ++m_folderRowsSubmitted;
            int flags = ImGuiTreeNodeFlags_OpenOnArrow | ImGuiTreeNodeFlags_SpanAvailWidth |
                        ImGuiTreeNodeFlags_FramePadding | ImGuiTreeNodeFlags_NoTreePushOnOpen;
            const bool selected = currentPathKey == row.pathKey;
            if (selected)
                flags |= ImGuiTreeNodeFlags_Selected;
            if (!row.hasSubdirs)
                flags |= ImGuiTreeNodeFlags_Leaf;

            if (row.hasSubdirs && syncExpandedState)
                ctx->SetNextItemOpen(row.expanded, ImGuiCond_Always);
            ctx->SetCursorPosX(baseX + static_cast<float>(row.depth) * indent);

            // TreeNodeEx on the context wrapper performs a second semantic
            // capture check for every row. The Project panel already knows
            // whether capture is active, so use the native call on the hot
            // path and explicitly preserve the wrapper's generic semantic
            // record when capture is requested.
            const bool open = ImGui::TreeNodeEx(row.imguiId.c_str(), static_cast<ImGuiTreeNodeFlags>(flags));
            const bool toggledOpen = row.hasSubdirs && ImGui::IsItemToggledOpen();
            if (captureSemantics) {
                ctx->RecordSemanticItem("tree_node", row.imguiId);
                ctx->RecordSemanticItem("project_folder", row.name, true, row.semanticId, selected);
            }
            if (toggledOpen) {
                ExecuteEditorCommand("project.set_folder_expanded", MakeTreeExpandedCommandArgument(row.path, open),
                                     "pointer");
            } else if (ctx->IsItemClicked()) {
                RequestDirectoryNavigation(row.path);
            }
        };

        // ImGuiListClipper has a fixed setup cost. A small tree is already
        // fully visible in normal panel sizes, so submit it directly; larger
        // projects retain the clipped path and its scrolling behavior.
        constexpr size_t kFolderTreeClipperThreshold = 32;
        if (m_folderTreeRows.size() <= kFolderTreeClipperThreshold) {
            m_folderRowsVisible += static_cast<uint64_t>(m_folderTreeRows.size());
            for (int index = 0; index < static_cast<int>(m_folderTreeRows.size()); ++index)
                renderRow(index);
        } else {
            ImGuiListClipper clipper;
            clipper.Begin(static_cast<int>(m_folderTreeRows.size()));
            while (clipper.Step()) {
                m_folderRowsVisible += static_cast<uint64_t>(std::max(clipper.DisplayEnd - clipper.DisplayStart, 0));
                for (int index = clipper.DisplayStart; index < clipper.DisplayEnd; ++index)
                    renderRow(index);
            }
            clipper.End();
        }
        m_folderTreeAppliedProjectionRevision = projectionRevision;
    }

    float remainH = ctx->GetContentRegionAvailHeight();
    if (remainH > 4.0f) {
        ctx->InvisibleButton("##folder_tree_empty_area", ctx->GetContentRegionAvailWidth(), remainH);
        if (ctx->IsItemClicked(0))
            PublishSelectionIntent({}, "");
    }
}

// ════════════════════════════════════════════════════════════════════
// File grid
// ════════════════════════════════════════════════════════════════════

void ProjectPanel::RenderFileGrid(InxGUIContext *ctx)
{
    const auto dataStart = std::chrono::steady_clock::now();
    std::string requestedContextTarget;
    bool requestedBackgroundContext = false;
    bool contextMenuRendered = false;

    // A popup owns an independent ImGui lifecycle once it has opened.  Asset
    // snapshots may legitimately be unavailable for a frame while the
    // asynchronous directory model changes, and navigation deliberately
    // returns early after replacing that model.  Neither event may skip an
    // already-open context menu or make its command payload/focus flicker.
    if (ImGui::IsPopupOpen("ProjectContextMenu")) {
        const auto contextStart = std::chrono::steady_clock::now();
        RenderContextMenu(ctx);
        m_subGridContext +=
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - contextStart).count();
        contextMenuRendered = true;
    }

    auto *snapshot = m_currentPath.empty() ? nullptr : GetDirSnapshot(m_currentPath);
    if (!snapshot) {
        ctx->Label(Tr("project.invalid_path"));
        m_subGridData +=
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - dataStart).count();
        return;
    }

    auto *items = GetProjectItems(m_currentPath, snapshot);
    if (!items) {
        m_subGridData +=
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - dataStart).count();
        return;
    }

    // Back button — navigate up within the project, but stop at project-root
    // subfolders (Assets, Logs, …). Never enter the bare project root folder or
    // any path above it.
    if (CanNavigateUpFromCurrent()) {
        if (ctx->Selectable("[..]", false)) {
            const std::string parent =
                infernux::FromFsPath(infernux::ToFsPath(infernux::ResolveFilesystemPath(m_currentPath)).parent_path());
            if (!RequestDirectoryNavigation(parent))
                return;
            // The snapshot and item pointers above belong to the previous
            // directory. Do not continue rendering this frame with a new path
            // and stale grid data; the next frame will acquire one coherent
            // snapshot for the parent directory.
            m_subGridData +=
                std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - dataStart).count();
            return;
        }
    }

    // Grid config
    float iconSize = static_cast<float>(ICON_SIZE);
    float avail_w = ctx->GetContentRegionAvailWidth();
    int cols = std::max(static_cast<int>(avail_w / CELL_WIDTH), 1);
    float rowHeight = iconSize + GetGridTextLineHeight(ctx) + GRID_PADDING + 8.0f;

    if (items->empty() && FilesystemPathKey(m_currentPath) == FilesystemPathKey(m_rootPath)) {
        ctx->Label(Tr("project.empty_folder"));
        ctx->Label(Tr("project.right_click_hint"));
    }

    // Virtual scrolling
    float gridStartX = ctx->GetCursorPosX();
    float gridStartY = ctx->GetCursorPosY();
    int itemCount = static_cast<int>(items->size());
    auto range = GetVisibleGridRange(ctx, itemCount, cols, rowHeight, gridStartY);
    m_gridItemsVisible += static_cast<uint64_t>(std::max(range.endIndex - range.startIndex, 0));
    const ImGuiPayload *dragPayload = ImGui::GetDragDropPayload();
    bool hasDragPayload = (dragPayload != nullptr);
    bool hasHierarchyDragPayload = hasDragPayload && dragPayload->IsDataType(DRAG_TYPE_HIERARCHY_GO);
    const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
    ImVec2 semanticBackgroundMin(0.0f, 0.0f);
    ImVec2 semanticBackgroundMax(0.0f, 0.0f);
    const auto itemsStart = std::chrono::steady_clock::now();
    m_subGridData += std::chrono::duration<double, std::milli>(itemsStart - dataStart).count();

    if (ctx->BeginTable("FileGrid", cols, 0, 0.0f)) {
        m_visibleItems = items;

        if (range.topSpacer > 0.0f) {
            ctx->TableNextRow();
            ctx->TableSetColumnIndex(0);
            ctx->Dummy(1.0f, range.topSpacer);
            ctx->TableNextRow();
        }

        ImDrawList *drawList = ImGui::GetWindowDrawList();

        // Resolve theme colors ONCE per frame (the registry lookups allocate a
        // temp std::string per call; doing it per-item was a measurable hit).
        const ImVec4 cAccent = ProjectAccentColor();
        const ImVec4 cHover = ProjectHoverColor();
        const ImU32 cHoverFill = ImGui::ColorConvertFloat4ToU32(ImVec4(cHover.x, cHover.y, cHover.z, 0.90f));
        // Uniform neutral tint drawn ON TOP of the whole cell on hover (covers
        // full-bleed thumbnails AND the model expand strip with ONE colour).
        const ImU32 cHoverTopTint = ImGui::ColorConvertFloat4ToU32(ImVec4(cHover.x, cHover.y, cHover.z, 0.22f));
        const ImU32 cSelFill = ImGui::ColorConvertFloat4ToU32(ImVec4(cAccent.x, cAccent.y, cAccent.z, 0.22f));
        const ImU32 cSelOutline = ProjectSelectionOutlineColor();
        const ImU32 cSubAssetBg = ProjectSubAssetCellBg();
        const ImU32 cExpandBg = ProjectExpandStripBg(false);
        const ImU32 cExpandBgHover = ProjectExpandStripBg(true);
        const ImU32 cTagText = ImGui::ColorConvertFloat4ToU32(ImVec4(0.62f, 0.63f, 0.66f, 1.0f));
        const float cellW = avail_w / static_cast<float>(cols);

        // Unified per-cell hover/selection feedback so EVERY item type (thumbnail,
        // inline sub-asset, model, and text-placeholder) reads identically: the
        // fill goes behind the icon in the table background channel and the
        // selection outline sits a couple px OUTSIDE the icon box.
        auto drawCellFeedback = [&](const ImVec2 &g0, const ImVec2 &g1, bool hovered, bool selected) {
            if (hovered || selected) {
                ImGui::TablePushBackgroundChannel();
                if (hovered)
                    drawList->AddRectFilled(g0, g1, cHoverFill, 3.0f);
                if (selected)
                    drawList->AddRectFilled(g0, g1, cSelFill, 3.0f);
                ImGui::TablePopBackgroundChannel();
            }
            // Single uniform tint on top → consistent hover for thumbnails, full-bleed
            // images and model expand-strips alike (no second accent-coloured patch).
            if (hovered)
                drawList->AddRectFilled(g0, g1, cHoverTopTint, 3.0f);
            if (selected) {
                // The outline sits 2px OUTSIDE the icon box. For the leftmost/topmost
                // column that 2px falls into the window-padding band, outside the
                // cell clip rect, so it would be cropped. Briefly widen the clip rect
                // (still well within the panel's padding) so the full outline shows.
                const ImVec2 cMin = drawList->GetClipRectMin();
                const ImVec2 cMax = drawList->GetClipRectMax();
                drawList->PushClipRect(ImVec2(cMin.x - 4.0f, cMin.y - 4.0f), ImVec2(cMax.x + 4.0f, cMax.y + 4.0f),
                                       false);
                drawList->AddRect(ImVec2(g0.x - 2.0f, g0.y - 2.0f), ImVec2(g1.x + 2.0f, g1.y + 2.0f), cSelOutline, 3.0f,
                                  0, kProjectSelectionOutlineThickness);
                drawList->PopClipRect();
            }
        };

        for (int i = range.startIndex; i < range.endIndex; ++i) {
            auto &item = (*items)[i];
            ++m_gridItemsSubmitted;
            ctx->TableNextColumn();
            ImGui::PushID(i);

            std::string fallbackSelectionKey;
            const std::string *selectionKey = &item.selectionKey;
            if (selectionKey->empty()) {
                fallbackSelectionKey = AssetSelectionPathKey(item.path);
                selectionKey = &fallbackSelectionKey;
            }

            const bool isSubAsset = (item.type == FileItem::SubMaterial || item.type == FileItem::SubMesh);
            const std::string itemSemanticId = captureSemantics ? MakeProjectItemSemanticId(item) : std::string{};
            const ImVec2 cellTopLeft = ImGui::GetCursorScreenPos();

            // A filled vertical grid still has real, pointer-receiving space to the
            // right of each fixed-width icon. Retain one such gutter as the
            // request-only semantic fallback for background context actions.
            if (captureSemantics && semanticBackgroundMax.x <= semanticBackgroundMin.x) {
                constexpr float kSemanticGutterInset = 3.0f;
                const float gutterWidth = cellW - iconSize;
                if (gutterWidth > kSemanticGutterInset * 2.0f) {
                    semanticBackgroundMin =
                        ImVec2(cellTopLeft.x + iconSize + kSemanticGutterInset, cellTopLeft.y + kSemanticGutterInset);
                    semanticBackgroundMax = ImVec2(cellTopLeft.x + cellW - kSemanticGutterInset,
                                                   cellTopLeft.y + iconSize - kSemanticGutterInset);
                }
            }

            const auto isSubAssetItem = [](const FileItem &it) {
                return it.type == FileItem::SubMaterial || it.type == FileItem::SubMesh;
            };

            // Expanded model on this row: draw the left portion of the inline strip so it
            // bridges into the first sub-asset cell on the same row.
            const bool isModelFile = (item.type == FileItem::File && IsModelExt(item.ext));
            if (isModelFile && m_modelTreeProjection.IsExpanded(item.path)) {
                const bool nextIsSubSameRow = (i + 1 < itemCount) && isSubAssetItem((*items)[i + 1]) &&
                                              (*items)[i + 1].parentPath == item.path && ((i + 1) % cols) != 0;
                if (nextIsSubSameRow) {
                    const ImVec2 bgMin(cellTopLeft.x, cellTopLeft.y);
                    const ImVec2 bgMax(cellTopLeft.x + cellW, cellTopLeft.y + iconSize);
                    ImGui::TablePushBackgroundChannel();
                    drawList->AddRectFilled(bgMin, bgMax, cSubAssetBg, 0.0f);
                    ImGui::TablePopBackgroundChannel();
                }
            }

            if (isSubAsset) {
                // Icon-row band only (labels stay transparent). Width is limited to
                // icon columns — extend left to the parent model, right only into the
                // next sibling on the same row (never bleed into unrelated columns).
                const bool prevIsParent =
                    (i > 0) && (*items)[i - 1].type == FileItem::File && (*items)[i - 1].path == item.parentPath;
                const bool prevIsSibling =
                    (i > 0) && isSubAssetItem((*items)[i - 1]) && (*items)[i - 1].parentPath == item.parentPath;
                const bool nextIsSibling = (i + 1 < itemCount) && isSubAssetItem((*items)[i + 1]) &&
                                           (*items)[i + 1].parentPath == item.parentPath && ((i + 1) % cols) != 0;

                float stripLeft = cellTopLeft.x;
                if (prevIsParent)
                    stripLeft = cellTopLeft.x - cellW; // connect back into the parent model cell
                else if (!prevIsSibling)
                    stripLeft = cellTopLeft.x; // first on a wrapped row — icon column only

                float stripRight = cellTopLeft.x + iconSize;
                if (nextIsSibling)
                    stripRight = cellTopLeft.x + cellW; // bridge to the next sibling cell

                const ImVec2 cellBgMin(stripLeft, cellTopLeft.y);
                const ImVec2 cellBgMax(stripRight, cellTopLeft.y + iconSize);
                ImGui::TablePushBackgroundChannel();
                drawList->AddRectFilled(cellBgMin, cellBgMax, cSubAssetBg, 0.0f);
                ImGui::TablePopBackgroundChannel();
            }

            const bool isSelected = m_selectedSet.find(*selectionKey) != m_selectedSet.end();
            // Record cell start position for full-cell drop overlay later
            // ── Resolve display texture (inline for speed) ──
            uint64_t displayTexId = 0;
            bool isUiPrefab = false;
            if (item.type == FileItem::SubMesh) {
                displayTexId = GetTypeIconId(item);
            } else if (item.type == FileItem::SubMaterial) {
                displayTexId = GetEmbeddedMaterialThumbnail(item);
                if (displayTexId == 0)
                    displayTexId = GetTypeIconId(item);
            } else if (item.type == FileItem::File) {
                if (IsImageExt(item.ext))
                    displayTexId = GetThumbnail(item.path, item.mtimeNs);
                else if (IsMaterialExt(item.ext))
                    displayTexId = GetMaterialThumbnail(item.path, item.mtimeNs);
                else if (IsModelExt(item.ext))
                    displayTexId = GetModelThumbnail(item.path, item.mtimeNs);
                else if (item.ext == ".prefab") {
                    isUiPrefab = IsUiPrefabFile(item.path, item.mtimeNs);
                    if (isUiPrefab)
                        displayTexId = GetModel3dIconId();
                    else
                        displayTexId = GetModelThumbnail(item.path, item.mtimeNs);
                }
                if (displayTexId == 0) {
                    // Scene prefabs waiting for GPU preview → model_3d icon, not file.png.
                    if (item.ext == ".prefab" && !isUiPrefab)
                        displayTexId = GetModel3dIconId();
                    if (displayTexId == 0)
                        displayTexId = GetTypeIconId(item);
                }
            } else {
                displayTexId = GetTypeIconId(item);
            }

            // ── Render icon (model: thumbnail on the left + narrow expand strip, same height) ──
            const float stripW = isModelFile ? kModelExpandStripW : 0.0f;
            const float thumbW = (stripW > 0.0f) ? (iconSize - stripW) : iconSize;

            if (displayTexId != 0) {
                int srcW = 0;
                int srcH = 0;
                if (item.type == FileItem::SubMaterial) {
                    srcW = 200;
                    srcH = 200;
                } else if (item.type == FileItem::File) {
                    if (IsImageExt(item.ext) && m_engine) {
                        const std::string resourceKey = std::string("tex|") + item.path;
                        int readyW = 0;
                        int readyH = 0;
                        const auto sizeIt = m_texturePreviewSizes.find(resourceKey);
                        if (sizeIt != m_texturePreviewSizes.end() && sizeIt->second.textureId == displayTexId) {
                            readyW = sizeIt->second.width;
                            readyH = sizeIt->second.height;
                        } else {
                            const auto readySize = m_engine->GetTexturePreviewSize(resourceKey);
                            readyW = readySize.first;
                            readyH = readySize.second;
                            m_texturePreviewSizes.insert_or_assign(
                                resourceKey, TexturePreviewSizeCacheEntry{displayTexId, readyW, readyH});
                        }
                        srcW = readyW;
                        srcH = readyH;
                    } else if (IsMaterialExt(item.ext)) {
                        srcW = 200;
                        srcH = 200;
                    } else if (IsModelExt(item.ext) || item.ext == ".prefab") {
                        srcW = 256;
                        srcH = 256;
                    }
                }

                ImGui::BeginGroup();
                // InvisibleButton for hit-testing; AddImage for drawing
                ImGui::InvisibleButton("##ic", ImVec2(thumbW, iconSize));
                if (captureSemantics)
                    ctx->RecordSemanticItem("project_item", item.name, true, itemSemanticId, isSelected);
                const bool thumbHovered = ImGui::IsItemHovered();
                const bool thumbClicked =
                    thumbHovered && ImGui::IsMouseReleased(ImGuiMouseButton_Left) && !hasDragPayload;
                const bool thumbRmb = ImGui::IsItemClicked(1);
                ImVec2 rMin = ImGui::GetItemRectMin();
                ImVec2 rMax = ImGui::GetItemRectMax();
                ImVec2 drawMin = rMin;
                ImVec2 drawMax = rMax;
                if (srcW > 0 && srcH > 0) {
                    const float scale =
                        std::min(thumbW / static_cast<float>(srcW), iconSize / static_cast<float>(srcH));
                    const float drawW = std::max(1.0f, static_cast<float>(srcW) * scale);
                    const float drawH = std::max(1.0f, static_cast<float>(srcH) * scale);
                    drawMin.x += (thumbW - drawW) * 0.5f;
                    drawMin.y += (iconSize - drawH) * 0.5f;
                    drawMax = ImVec2(drawMin.x + drawW, drawMin.y + drawH);
                }
                drawList->AddImage(ImTextureRef(static_cast<ImTextureID>(displayTexId)), drawMin, drawMax);
                if (isModelFile) {
                    ImGui::SameLine(0.0f, 0.0f);
                    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, ImVec2(0.0f, 0.0f));
                    const bool ex = m_modelTreeProjection.IsExpanded(item.path);
                    uint64_t expandTex = 0;
                    if (ex) {
                        auto eit = m_typeIconCache.find("model_expand_open");
                        expandTex = eit != m_typeIconCache.end() ? eit->second : 0;
                    } else {
                        auto eit = m_typeIconCache.find("model_expand_closed");
                        expandTex = eit != m_typeIconCache.end() ? eit->second : 0;
                    }
                    bool expandClicked = false;
                    if (expandTex != 0) {
                        // ImageButton with ImVec2(stripW, iconSize) distorts a square art asset; keep hit area
                        // full strip x icon height but draw the glyph with aspect preserved and centered.
                        ImGui::InvisibleButton("##mdlexpand", ImVec2(stripW, iconSize));
                        const bool expandHovered = ImGui::IsItemHovered();
                        expandClicked = ImGui::IsItemClicked(0) && !hasDragPayload;
                        const ImVec2 ex0 = ImGui::GetItemRectMin();
                        const ImVec2 ex1 = ImGui::GetItemRectMax();
                        drawList->AddRectFilled(ex0, ex1, expandHovered ? cExpandBgHover : cExpandBg, 2.0f);
                        const float exW = ex1.x - ex0.x;
                        const float exH = ex1.y - ex0.y;
                        const float srcW = kModelExpandIconSrcPx;
                        const float srcH = kModelExpandIconSrcPx;
                        const float gScale = std::min(exW / srcW, exH / srcH);
                        const float dW = std::max(1.0f, srcW * gScale);
                        const float dH = std::max(1.0f, srcH * gScale);
                        const float cx = (ex0.x + ex1.x) * 0.5f;
                        const float cy = (ex0.y + ex1.y) * 0.5f;
                        const ImVec2 dmin(cx - dW * 0.5f, cy - dH * 0.5f);
                        const ImVec2 dmax(cx + dW * 0.5f, cy + dH * 0.5f);
                        drawList->AddImage(ImTextureRef(static_cast<ImTextureID>(expandTex)), dmin, dmax);
                    } else {
                        ImGui::PushStyleColor(ImGuiCol_Button, EditorTheme::PROJECT_EXPAND_STRIP_BG);
                        ImGui::PushStyleColor(ImGuiCol_ButtonHovered, EditorTheme::PROJECT_EXPAND_STRIP_HOVER);
                        ImGui::PushStyleColor(ImGuiCol_ButtonActive, EditorTheme::PROJECT_EXPAND_STRIP_HOVER);
                        expandClicked = ImGui::Button(ex ? "v" : ">", ImVec2(stripW, iconSize));
                        ImGui::PopStyleColor(3);
                    }
                    if (captureSemantics) {
                        ctx->RecordSemanticItem("project_model_expand", ex ? "Collapse Model" : "Expand Model", true,
                                                itemSemanticId + ".expand", ex);
                    }
                    if (expandClicked) {
                        ExecuteEditorCommand("project.set_model_expanded",
                                             MakeTreeExpandedCommandArgument(item.path, !ex), "pointer");
                    }
                    ImGui::PopStyleVar();
                }
                ImGui::EndGroup();

                drawCellFeedback(ImGui::GetItemRectMin(), ImGui::GetItemRectMax(), thumbHovered, isSelected);
                if (thumbClicked && !hasDragPayload)
                    HandleItemClick(item, ctx);
                if (thumbRmb) {
                    const bool alreadySelected = m_selectedSet.find(*selectionKey) != m_selectedSet.end();
                    PublishSelectionIntent(alreadySelected ? m_selectedFiles : std::vector<std::string>{item.path},
                                           item.path);
                    requestedContextTarget = item.path;
                }
            } else {
                // No icon texture for this type → centered "[TAG]" placeholder, but
                // rendered with the SAME iconSize box + feedback as thumbnailed items
                // so every cell looks and sizes identically.
                const char *tag = (item.type != FileItem::Dir) ? GetFileTypeTag(item.name) : "[DIR]";
                ImGui::InvisibleButton("##ic", ImVec2(iconSize, iconSize));
                if (captureSemantics)
                    ctx->RecordSemanticItem("project_item", item.name, true, itemSemanticId, isSelected);
                const bool thumbHovered = ImGui::IsItemHovered();
                const bool thumbClicked =
                    thumbHovered && ImGui::IsMouseReleased(ImGuiMouseButton_Left) && !hasDragPayload;
                const bool thumbRmb = ImGui::IsItemClicked(1);
                const ImVec2 g0 = ImGui::GetItemRectMin();
                const ImVec2 g1 = ImGui::GetItemRectMax();
                const ImVec2 ts = ImGui::CalcTextSize(tag);
                drawList->AddText(ImVec2((g0.x + g1.x - ts.x) * 0.5f, (g0.y + g1.y - ts.y) * 0.5f), cTagText, tag);
                drawCellFeedback(g0, g1, thumbHovered, isSelected);
                if (thumbClicked && !hasDragPayload)
                    HandleItemClick(item, ctx);
                if (thumbRmb) {
                    const bool alreadySelected = m_selectedSet.find(*selectionKey) != m_selectedSet.end();
                    PublishSelectionIntent(alreadySelected ? m_selectedFiles : std::vector<std::string>{item.path},
                                           item.path);
                    requestedContextTarget = item.path;
                }
            }

            // ── Drag-drop source (must always run to detect drag start) ──
            RenderDragDropSource(ctx, item);

            // ── Drop targets (only when a drag is active) ──
            if (hasDragPayload && item.type == FileItem::Dir) {
                RenderFolderDropTarget(ctx, item.path);
            }

            // ── Label ──
            {
                float cellStartX = ImGui::GetItemRectMin().x - ImGui::GetWindowPos().x + ImGui::GetScrollX();
                RenderItemLabel(ctx, item, iconSize, cellStartX);
            }

            ImGui::PopID();
        }

        if (range.bottomSpacer > 0.0f) {
            ctx->TableNextRow();
            ctx->TableSetColumnIndex(0);
            ctx->Dummy(1.0f, range.bottomSpacer);
        }

        ctx->EndTable();
    }
    const auto gridTailStart = std::chrono::steady_clock::now();
    m_subGridItems += std::chrono::duration<double, std::milli>(gridTailStart - itemsStart).count();

    // Full-grid hierarchy drop target (covers entire FileGrid child window)
    if (hasHierarchyDragPayload) {
        ImGuiWindow *win = ImGui::GetCurrentWindow();
        if (ImGui::BeginDragDropTargetCustom(win->InnerRect, win->ID)) {
            uint64_t objId = 0;
            if (ctx->AcceptDragDropPayload(DRAG_TYPE_HIERARCHY_GO, &objId)) {
                ExecuteEditorCommand("prefab.save_as", MakePrefabSaveAsCommandArgument(objId, m_currentPath),
                                     "drag_drop");
            }
            ImGui::EndDragDropTarget();
        }
    }

    // Bottom empty area: click to deselect + accept hierarchy drops
    float remainH = ctx->GetContentRegionAvailHeight();
    if (remainH > 10.0f) {
        ctx->InvisibleButton("##drop_prefab_area", ctx->GetContentRegionAvailWidth(), remainH);
        if (captureSemantics) {
            ctx->RecordSemanticRect("project_background", "File Grid Background", ctx->GetItemRectMinX(),
                                    ctx->GetItemRectMinY(), ctx->GetItemRectMaxX() - ctx->GetItemRectMinX(),
                                    ctx->GetItemRectMaxY() - ctx->GetItemRectMinY(), true,
                                    "project.file_grid.background");
        }
        if (hasHierarchyDragPayload && ctx->BeginDragDropTarget()) {
            uint64_t objId = 0;
            if (ctx->AcceptDragDropPayload(DRAG_TYPE_HIERARCHY_GO, &objId)) {
                ExecuteEditorCommand("prefab.save_as", MakePrefabSaveAsCommandArgument(objId, m_currentPath),
                                     "drag_drop");
            }
            ctx->EndDragDropTarget();
        }
        if (ctx->IsItemClicked(0))
            PublishSelectionIntent({}, "");
        if (ctx->IsItemClicked(1))
            requestedBackgroundContext = true;
    } else if (captureSemantics && semanticBackgroundMax.x > semanticBackgroundMin.x &&
               semanticBackgroundMax.y > semanticBackgroundMin.y) {
        ctx->RecordSemanticRect("project_background", "File Grid Background", semanticBackgroundMin.x,
                                semanticBackgroundMin.y, semanticBackgroundMax.x - semanticBackgroundMin.x,
                                semanticBackgroundMax.y - semanticBackgroundMin.y, true,
                                "project.file_grid.background");
    }
    m_subGridTail +=
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - gridTailStart).count();

    requestedBackgroundContext =
        requestedBackgroundContext ||
        (requestedContextTarget.empty() && ImGui::IsWindowHovered(ImGuiHoveredFlags_AllowWhenBlockedByActiveItem) &&
         ImGui::IsMouseClicked(ImGuiMouseButton_Right) && !ImGui::IsAnyItemHovered());
    if (!requestedContextTarget.empty() || requestedBackgroundContext) {
        m_contextTargetPath = requestedContextTarget;
        m_contextRevealPath =
            requestedContextTarget.empty() ? m_currentPath : ResolveRealAssetPath(requestedContextTarget);
        m_contextCurrentPath = m_currentPath;
        ctx->OpenPopup("ProjectContextMenu");
    }

    if (!contextMenuRendered) {
        const auto contextStart = std::chrono::steady_clock::now();
        RenderContextMenu(ctx);
        m_subGridContext +=
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - contextStart).count();
    }
}

// ════════════════════════════════════════════════════════════════════
// Context menu
// ════════════════════════════════════════════════════════════════════

void ProjectPanel::RenderContextMenu(InxGUIContext *ctx)
{
    if (!ctx->BeginPopup("ProjectContextMenu")) {
        if (!ImGui::IsPopupOpen("ProjectContextMenu")) {
            m_contextTargetPath.clear();
            m_contextRevealPath.clear();
            m_contextCurrentPath.clear();
        }
        return;
    }
    if (renderContextMenu)
        renderContextMenu(ctx, m_contextTargetPath, m_contextRevealPath, m_contextCurrentPath);
    ctx->EndPopup();
}

void ProjectPanel::RenderDragDropSource(InxGUIContext *ctx, const FileItem &item)
{
    // Embedded model materials are browse-only (no drag — use a standalone .mat to assign).
    if (item.type != FileItem::Dir && item.type != FileItem::File && item.type != FileItem::SubMesh)
        return;

    // BeginDragDropSource is cheap (~1µs) — returns false 99.9% of the time.
    // All map lookups and string formatting are deferred to the rare drag-active path.
    if (!ctx->BeginDragDropSource(0))
        return;

    if (item.type == FileItem::Dir) {
        ctx->SetDragDropPayload(DRAG_TYPE_PROJECT_ITEM, item.path);
        ctx->Label("Folder: " + item.name);
        ctx->EndDragDropSource();
        return;
    }

    if (item.type == FileItem::SubMesh) {
        if (item.parentPath.empty()) {
            ctx->EndDragDropSource();
            return;
        }

        // Embedded animation take (model.fbx::subanim:i) — drag as 3D clip, same as a .animclip3d file.
        // slotIndex >= 0 marks a real take row; overflow / placeholder rows use -1.
        if (item.path.find(kSubAnimToken) != std::string::npos && item.slotIndex >= 0) {
            auto &ddMap = GetDragDropMap();
            auto ddIt = ddMap.find(".animclip3d");
            if (ddIt != ddMap.end()) {
                ctx->SetDragDropPayload(ddIt->second.payloadType, item.path);
                ctx->Label(std::string(ddIt->second.label) + ": " + item.name);
            } else {
                ctx->SetDragDropPayload("ANIMCLIP3D_FILE", item.path);
                ctx->Label("3D AnimClip: " + item.name);
            }
            ctx->EndDragDropSource();
            return;
        }

        // Other embedded FBX sub-entries: parent model asset (GUID when possible).
        std::string ext = infernux::FromFsPath(fs::u8path(item.parentPath).extension());
        for (auto &c : ext)
            c = static_cast<char>(std::tolower(static_cast<unsigned char>(c)));

        auto &gdMap = GetGuidDragDropMap();
        auto gdIt = gdMap.find(ext);

        if (gdIt != gdMap.end()) {
            std::string guid;
            if (getGuidFromPath)
                guid = getGuidFromPath(item.parentPath);
            else if (m_assetDatabase)
                guid = m_assetDatabase->GetGuidFromPath(item.parentPath);

            if (!guid.empty())
                ctx->SetDragDropPayload(gdIt->second.guidPayloadType, guid);
            else
                ctx->SetDragDropPayload(gdIt->second.pathPayloadType, item.parentPath);

            ctx->Label(std::string("Model") + ": " + item.name);
        } else {
            ctx->SetDragDropPayload(DRAG_TYPE_PROJECT_ITEM, item.parentPath);
            ctx->Label("Model: " + item.name);
        }

        ctx->EndDragDropSource();
        return;
    }

    // File type — resolve drag payload only when actually dragging
    auto &ddMap = GetDragDropMap();
    auto &gdMap = GetGuidDragDropMap();
    auto ddIt = ddMap.find(item.ext);
    auto gdIt = gdMap.find(item.ext);

    if (ddIt != ddMap.end()) {
        const char *pType = ddIt->second.payloadType;
        const char *labelPfx = ddIt->second.label;

        constexpr std::string_view kParticleScriptSuffix = ".particle.py";
        const bool isParticleScript =
            item.name.size() >= kParticleScriptSuffix.size() &&
            item.name.compare(item.name.size() - kParticleScriptSuffix.size(), kParticleScriptSuffix.size(),
                              kParticleScriptSuffix.data(), kParticleScriptSuffix.size()) == 0;
        if (item.ext == ".py" && isParticleScript) {
            pType = "PARTICLE_GRAPH_FILE";
            labelPfx = "Particle Script";
        }

        ctx->SetDragDropPayload(pType, item.path);
        ctx->Label(std::string(labelPfx) + ": " + item.name);
    } else if (gdIt != gdMap.end()) {
        std::string guid;
        if (getGuidFromPath)
            guid = getGuidFromPath(item.path);
        else if (m_assetDatabase)
            guid = m_assetDatabase->GetGuidFromPath(item.path);

        if (!guid.empty())
            ctx->SetDragDropPayload(gdIt->second.guidPayloadType, guid);
        else
            ctx->SetDragDropPayload(gdIt->second.pathPayloadType, item.path);

        ctx->Label(std::string(gdIt->second.label) + ": " + item.name);
    } else {
        ctx->SetDragDropPayload(DRAG_TYPE_PROJECT_ITEM, item.path);
        ctx->Label("Item: " + item.name);
    }

    ctx->EndDragDropSource();
}

void ProjectPanel::RenderFolderDropTarget(InxGUIContext *ctx, const std::string &folderPath)
{
    ImGui::PushStyleColor(ImGuiCol_DragDropTarget, ImVec4(0, 0, 0, 0));
    if (ctx->BeginDragDropTarget()) {
        bool handled = false;
        uint64_t objId = 0;
        if (ctx->AcceptDragDropPayload(DRAG_TYPE_HIERARCHY_GO, &objId)) {
            ExecuteEditorCommand("prefab.save_as", MakePrefabSaveAsCommandArgument(objId, folderPath), "drag_drop");
            handled = true;
        }
        if (!handled) {
            auto &acceptTypes = GetMoveAcceptTypes();
            for (auto &dt : acceptTypes) {
                std::string payload;
                if (ctx->AcceptDragDropPayload(dt, &payload)) {
                    MoveProjectItemsToFolder(folderPath, dt, payload);
                    break;
                }
            }
        }
        ctx->EndDragDropTarget();
    }
    ImGui::PopStyleColor(1);
}

void ProjectPanel::RenderItemLabel(InxGUIContext *ctx, const FileItem &item, float iconSize, float cellStartX)
{
    if (!m_renamingPath.empty() && FilesystemPathKey(m_renamingPath) == FilesystemPathKey(item.path)) {
        if (m_renameFocusRequested) {
            ctx->SetKeyboardFocusHere();
            m_renameFocusRequested = false;
        }

        ctx->SetCursorPosX(cellStartX);
        ctx->SetNextItemWidth(iconSize);
        ctx->TextInput("##rename_" + item.path, m_renameBuf, sizeof(m_renameBuf));
        ctx->RecordSemanticItem("text_input", "Name", true, "project.rename.input");

        if (m_renameSkipDeactivateFrames > 0)
            --m_renameSkipDeactivateFrames;

        if (ctx->IsKeyPressed(kKeyEnter))
            CommitRename();
        else if (m_renameSkipDeactivateFrames == 0 && ctx->IsItemDeactivated())
            CommitRename();
    } else {
        auto &entry = GetCachedItemLabel(ctx, item, iconSize);
        const float labelHeight = GetGridTextLineHeight(ctx);
        ctx->SetCursorPosX(cellStartX);
        const ImVec2 labelMin = ImGui::GetCursorScreenPos();
        ctx->InvisibleButton("##label_click", iconSize, labelHeight);

        const float textY = labelMin.y + std::max((labelHeight - ImGui::GetTextLineHeight()) * 0.5f, 0.0f);
        ImGui::GetWindowDrawList()->AddText(ImVec2(labelMin.x + entry.offsetX, textY),
                                            ImGui::GetColorU32(ImGuiCol_Text), entry.displayText.c_str());

        // The icon and its filename are one file item. Previously only the icon
        // received clicks, so a normal double-click on the filename did nothing.
        if (ImGui::IsItemHovered() && ImGui::IsMouseReleased(ImGuiMouseButton_Left))
            HandleItemClick(item, ctx);
    }
}

std::string ProjectPanel::MakeProjectFolderSemanticId(const std::string &path) const
{
    return "project.folder." + MakeProjectRelativeSemanticPath(path);
}

std::string ProjectPanel::MakeProjectItemSemanticId(const FileItem &item) const
{
    const char *prefix = item.type == FileItem::Dir ? "project.folder." : "project.asset.";
    return std::string(prefix) + MakeProjectRelativeSemanticPath(item.path);
}

std::string ProjectPanel::MakeProjectRelativeSemanticPath(const std::string &path) const
{
    const std::string realPath = ResolveRealAssetPath(path);
    const std::string virtualSuffix = path.substr(realPath.size());
    std::string relativePath;

    if (!m_rootPath.empty() && !realPath.empty()) {
        std::string candidate;
        if (infernux::TryMakeRelativeFilesystemPath(realPath, m_rootPath, candidate) && !candidate.empty())
            relativePath = std::move(candidate);
    }

    if (relativePath.empty() && !realPath.empty())
        relativePath = infernux::FromFsPath(fs::u8path(realPath).filename());
    if (relativePath.empty())
        relativePath = "unknown";

    return infernux::NormalizePortablePath(relativePath) + virtualSuffix;
}

} // namespace infernux
