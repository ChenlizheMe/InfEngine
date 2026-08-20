#pragma once

#include <function/editor/EditorPanel.h>
#include <function/editor/EditorTheme.h>
#include <function/editor/interaction/EditorCollectionModel.h>
#include <function/editor/interaction/EditorSearchModel.h>
#include <function/renderer/InxRenderer.h>
#include <function/resources/AssetDatabase/AssetDatabase.h>
#include <function/resources/InxFileLoader/InxTextureLoader.hpp>

#include <atomic>
#include <cstdint>
#include <filesystem>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace infernux
{

class Infernux;

/// C++ native Project panel — Unity-style asset browser with folder tree,
/// file grid, thumbnails, drag-drop, inline rename, and virtual scrolling.
///
/// Heavy-lift rendering (grid loop, folder tree, thumbnail management) is
/// entirely in C++.  Python-only managers (scene open, prefab creation,
/// script validation, asset inspector) are reached via std::function
/// callbacks set from the bootstrap layer.
class ProjectPanel : public EditorPanel
{
  public:
    ProjectPanel();
    std::unordered_map<std::string, double> ConsumeSubTimings() override;

    // ── Public API (called from Python bootstrap / other panels) ─────

    void SetRootPath(const std::string &path);
    void SetEngine(Infernux *engine);
    void SetRenderer(InxRenderer *renderer);
    void SetAssetDatabase(AssetDatabase *adb);
    void SetIconsDirectory(const std::string &dir);
    void RequestSearchFocus()
    {
        m_focusSearchNextFrame = true;
    }

    void ClearSelection(bool notify = true);
    void SetSelectedFile(const std::string &path, bool notify = true);
    void SetSelectedFiles(const std::vector<std::string> &paths, const std::string &primary = "", bool notify = true);

    void InvalidateMaterialThumbnail(const std::string &filePath);
    void InvalidateTextureThumbnail(const std::string &filePath);

    /// Invalidate the directory cache so listing refreshes next frame.
    void InvalidateDirCache();

    /// Publish files dropped from the OS as one global asset import command.
    void ReceiveDroppedFiles(const std::vector<std::string> &paths);

    /// State persistence
    std::string GetCurrentPath() const
    {
        return m_currentPath;
    }
    bool CanNavigateToPath(const std::string &path) const;
    bool SetCurrentPath(const std::string &path);
    [[nodiscard]] std::vector<std::string> GetFolderExpandedPaths() const;
    void SetFolderExpandedPaths(const std::vector<std::string> &paths);
    [[nodiscard]] std::vector<std::string> GetModelExpandedPaths() const;
    void SetModelExpandedPaths(const std::vector<std::string> &paths);

    // Presentation adapters. Domain mutations are owned by the global
    // ProjectAssetInteractionService and reach this view only as projections.
    bool BeginRenameSelectedAsset(const std::string &path = "");
    bool HasSelectedAssets() const;
    bool CanRenameSelectedAsset(const std::string &path = "") const;
    std::vector<std::string> GetOSClipboardFiles() const;

    // Unified command presentation callbacks.
    /// Draw the popup body from a frozen
    /// (logicalTargetPath, revealPath, currentPath) snapshot.
    std::function<void(InxGUIContext *, const std::string &, const std::string &, const std::string &)>
        renderContextMenu;

    // ── Notification callbacks ───────────────────────────────────────

    /// User-authored Project selection intent. SelectionService owns the
    /// authoritative snapshot and projects it back through SetSelectedFiles().
    std::function<void(const std::vector<std::string> &, const std::string &)> onSelectionChanged;
    /// Called when current_path changes between frames.
    std::function<void()> onStateChanged;

    /// Get GUID from path (delegates to AssetDatabase if callback not set)
    std::function<std::string(const std::string &)> getGuidFromPath;
    /// Get path from GUID
    std::function<std::string(const std::string &)> getPathFromGuid;

    /// Invalidate asset inspector cache
    std::function<void(const std::string &)> invalidateAssetInspector;

    // ── Translation ──────────────────────────────────────────────────

    std::function<std::string(const std::string &)> translate;

    // ── Drag-drop payload types ──────────────────────────────────────

    static constexpr const char *DRAG_TYPE_PROJECT_ITEM = "PROJECT_PANEL_ITEM_PATH";
    static constexpr const char *DRAG_TYPE_HIERARCHY_GO = "HIERARCHY_GAMEOBJECT";

  protected:
    void OnRenderContent(InxGUIContext *ctx) override;
    void PreRender(InxGUIContext *ctx) override;
    void VisiblePreRender(InxGUIContext *ctx) override;

  private:
    // ── Translation cache ────────────────────────────────────────────
    const std::string &Tr(const std::string &key);
    std::unordered_map<std::string, std::string> m_trCache;

    // ── Item representation ──────────────────────────────────────────
    struct FileItem
    {
        enum Type : uint8_t
        {
            Dir,
            File,
            SubMesh,
            SubMaterial
        };
        Type type = File;
        std::string name;
        std::string path;
        std::string selectionKey;
        std::string ext;
        std::string parentPath; // for sub-assets
        uint64_t mtimeNs = 0;
        int slotIndex = -1; // for SubMaterial
        ResourceType resourceType = ResourceType::DefaultText;
    };

    struct SearchIndexEntry
    {
        FileItem item;
        std::string searchKey;
        std::string sortKey;
    };

    struct SearchAsyncCompletion
    {
        uint64_t requestSerial = 0;
        EditorSearchToken token;
        uint64_t indexGeneration = UINT64_MAX;
        std::string indexRoot;
        std::shared_ptr<const std::vector<SearchIndexEntry>> index;
        std::vector<FileItem> results;
        std::string error;
        bool cancelled = false;
    };

    struct SearchAsyncState
    {
        std::atomic<uint64_t> desiredSerial{0};
        std::mutex mutex;
        std::unique_ptr<SearchAsyncCompletion> completion;
    };

    // ── Directory snapshot cache ─────────────────────────────────────
    struct DirSnapshot
    {
        uint64_t mtimeNs = 0;
        uint64_t assetGeneration = 0;
        double lastValidatedAt = 0.0; // steady-clock seconds
        std::vector<FileItem> dirs;
        std::vector<FileItem> files;
        std::vector<FileItem> items; // dirs + files
        // Stable model candidates.  GetProjectItems uses this instead of
        // rescanning every visible item when the model expansion projection
        // has not changed.
        std::vector<std::string> modelPaths;
    };

    struct DirTreeMeta
    {
        bool hasSubdirs = false;
    };

    DirSnapshot *GetDirSnapshot(const std::string &path);
    DirTreeMeta *GetDirTreeMeta(const std::string &path);
    std::vector<FileItem> *GetProjectItems(const std::string &path, DirSnapshot *snapshot = nullptr);
    static void AppendModelSubAssets(std::vector<FileItem> &out, AssetDatabase *adb, const FileItem &modelItem);

    std::unordered_map<std::string, DirSnapshot> m_dirCache;
    std::unordered_map<std::string, DirTreeMeta> m_dirTreeMetaCache;

    // Augmented items (with model sub-assets)
    struct AugmentedCache
    {
        uint64_t mtimeNs = 0;
        std::vector<std::string> expandedPaths;
        std::vector<FileItem> items;
    };
    std::unordered_map<std::string, AugmentedCache> m_augmentedCache;

    // ── Label layout cache ───────────────────────────────────────────
    struct LabelCacheKey
    {
        std::string path;
        std::string name;
        uint8_t type;
        bool expanded;
        int widthPx;

        bool operator==(const LabelCacheKey &o) const
        {
            return type == o.type && expanded == o.expanded && widthPx == o.widthPx && path == o.path && name == o.name;
        }
    };
    struct LabelCacheKeyHash
    {
        size_t operator()(const LabelCacheKey &k) const
        {
            size_t h = std::hash<std::string>{}(k.path);
            h ^= std::hash<int>{}(k.widthPx) + 0x9e3779b9 + (h << 6) + (h >> 2);
            return h;
        }
    };
    struct LabelEntry
    {
        std::string displayText;
        float offsetX = 0.0f;
    };
    std::unordered_map<LabelCacheKey, LabelEntry, LabelCacheKeyHash> m_labelCache;
    float m_gridTextLineHeight = 0.0f;

    // ── Thumbnail system ─────────────────────────────────────────────
    std::unordered_map<std::string, std::pair<uint64_t, double>> m_materialMtimeCache;
    std::unordered_map<std::string, std::pair<uint64_t, double>> m_textureMtimeCache;
    std::unordered_map<std::string, std::pair<uint64_t, double>> m_modelMtimeCache;

    struct TexturePreviewSizeCacheEntry
    {
        uint64_t textureId = 0;
        int width = 0;
        int height = 0;
    };
    // GetTexturePreviewSize takes the renderer preview mutex and canonicalizes
    // the key. Keep the stable-frame dimensions keyed by the live texture id;
    // a new id naturally invalidates the entry after a preview rebuild.
    std::unordered_map<std::string, TexturePreviewSizeCacheEntry> m_texturePreviewSizes;

    struct TexturePreviewContract
    {
        uint64_t catalogGeneration = 0;
        uint64_t contentStamp = 0;
        bool nearest = false;
        bool srgb = false;
        int maxSize = 2048;
        std::string textureFormat = "auto";
        std::string textureType = "default";
    };
    std::unordered_map<std::string, TexturePreviewContract> m_texturePreviewContracts;

    struct PreviewRequestCacheEntry
    {
        uint64_t fingerprint = 0;
        uint64_t textureId = 0;
        uint64_t lastRequestFrame = 0;
    };
    std::unordered_map<std::string, PreviewRequestCacheEntry> m_texturePreviewRequests;
    // Material preview queries are more expensive than the read-only texture
    // lookup. Keep the last published id beside the source fingerprint so a
    // stable Project frame does not re-enter the preview state machine.
    std::unordered_map<std::string, PreviewRequestCacheEntry> m_materialPreviewResults;
    std::unordered_map<std::string, PreviewRequestCacheEntry> m_modelPreviewRequests;
    uint64_t m_previewFrameSerial = 0;
    // FileManager must not enqueue a whole directory's previews in one frame.
    // These are scheduling budgets, not display limits: ready thumbnails remain visible.
    int m_texturePreviewRequestsThisFrame = 0;
    int m_modelPreviewRequestsThisFrame = 0;
    static constexpr int kTexturePreviewRequestBudget = 2;
    static constexpr int kModelPreviewRequestBudget = 1;
    struct PrefabTypeCacheEntry
    {
        uint64_t mtimeNs = 0;
        bool isUi = false;
    };
    std::unordered_map<std::string, PrefabTypeCacheEntry> m_prefabTypeCache;

    void ProcessPendingThumbnails();
    uint64_t GetThumbnail(const std::string &filePath, uint64_t cachedMtimeNs);
    uint64_t GetMaterialThumbnail(const std::string &filePath, uint64_t cachedMtimeNs);
    uint64_t GetEmbeddedMaterialThumbnail(const FileItem &item);
    uint64_t GetModelThumbnail(const std::string &filePath, uint64_t cachedMtimeNs);
    uint64_t GetMaterialMtimeNs(const std::string &filePath);
    uint64_t GetTextureMtimeNs(const std::string &filePath);

    // ── File-type icon cache ─────────────────────────────────────────
    std::unordered_map<std::string, uint64_t> m_typeIconCache;
    std::unordered_set<std::string> m_pendingTypeIconIds;
    bool m_typeIconsLoaded = false;
    std::string m_iconsDir;

    void EnsureTypeIconsLoaded();
    uint64_t GetTypeIconId(const FileItem &item) const;
    uint64_t GetModel3dIconId() const;
    bool IsUiPrefabFile(const std::string &filePath, uint64_t cachedMtimeNs);

    // ── Drag-drop maps ───────────────────────────────────────────────
    struct DragDropInfo
    {
        const char *payloadType;
        const char *label;
    };
    struct GuidDragDropInfo
    {
        const char *guidPayloadType;
        const char *pathPayloadType;
        const char *label;
    };
    static const std::unordered_map<std::string, DragDropInfo> &GetDragDropMap();
    static const std::unordered_map<std::string, GuidDragDropInfo> &GetGuidDragDropMap();
    static const std::vector<std::string> &GetMoveAcceptTypes();

    // ── Filtering ────────────────────────────────────────────────────
    static bool ShouldShow(const std::string &name);

    // File type tag for text fallback
    static const char *GetFileTypeTag(const std::string &filename);

    // Icon map: ext/key → icon filename
    static const std::unordered_map<std::string, std::string> &GetIconMap();

    // ── Panel state ──────────────────────────────────────────────────
    std::string m_rootPath;
    std::string m_currentPath;
    /// Default browse folder (usually `<project>/Assets`); also the navigation floor.
    std::string m_preferredNavPath;
    /// When true, bare project root is not a valid browse target ([..] stops at subfolders).
    bool m_navHasSubfolders = false;
    bool m_canNavigateUp = false;
    std::string m_currentPathKey;
    double m_subPreIcons = 0.0;
    double m_subPrePreview = 0.0;
    double m_subPreOther = 0.0;
    double m_subBreadcrumb = 0.0;
    double m_subFolderTree = 0.0;
    double m_subFileGrid = 0.0;
    double m_subTail = 0.0;
    double m_subGridContext = 0.0;
    double m_subGridData = 0.0;
    double m_subGridItems = 0.0;
    double m_subGridTail = 0.0;
    std::string m_lastNotifiedPath;

    // Lightweight counters are intentionally exposed through
    // ConsumeSubTimings().  They make cache invalidation testable without
    // requiring an editor or a GPU build.
    uint64_t m_dirSnapshotBuilds = 0;
    uint64_t m_folderTreeRebuilds = 0;
    uint64_t m_projectItemsRebuilds = 0;
    uint64_t m_previewScheduleAttempts = 0;
    uint64_t m_folderRowsSubmitted = 0;
    uint64_t m_folderRowsVisible = 0;
    uint64_t m_gridItemsSubmitted = 0;
    uint64_t m_gridItemsVisible = 0;

    // Breadcrumb
    std::string m_breadcrumbPath;
    std::string m_breadcrumbText;

    // Project-wide search (Unity-style box on the Path bar)
    char m_searchBuf[256] = {};
    EditorSearchModel m_search;
    bool m_focusSearchNextFrame = false;
    EditorSearchToken m_searchResultToken;
    EditorSearchToken m_searchDesiredToken;
    uint64_t m_searchIndexGeneration = UINT64_MAX;
    std::string m_searchIndexRoot;
    std::shared_ptr<const std::vector<SearchIndexEntry>> m_searchIndex;
    std::vector<FileItem> m_searchResults;
    std::shared_ptr<SearchAsyncState> m_searchAsyncState = std::make_shared<SearchAsyncState>();
    uint64_t m_searchRequestSerial = 0;
    bool m_searchJobInFlight = false;
    bool m_searchBusy = false;
    static constexpr size_t kMaxSearchResults = 200;

    // Selection
    std::string m_selectedFile;
    std::vector<std::string> m_selectedFiles;
    std::unordered_set<std::string> m_selectedSet; // normalized asset/subresource identity keys for O(1) lookup

    // Frozen when a right-click opens the popup. Menu rendering and command
    // execution never read a later selection/current-path mutation.
    std::string m_contextTargetPath;
    std::string m_contextRevealPath;
    std::string m_contextCurrentPath;

    void NotifySelectionChanged();
    void PublishSelectionIntent(const std::vector<std::string> &paths, const std::string &primary) const;
    std::vector<std::string> GetSelectedPaths() const;

    // Double-click detection
    std::string m_lastClickedFile;
    double m_lastClickTime = 0.0;

    // Rename
    std::string m_renamingPath;
    char m_renameBuf[256] = {};
    bool m_renameFocusRequested = false;
    // Skip IsItemDeactivated commit for the frame that opens rename (F2/context menu),
    // otherwise the new InputText can deactivate immediately and cancel the rename UI.
    int m_renameSkipDeactivateFrames = 0;

    // Deferred cache invalidation — set by operations that modify the filesystem
    // mid-render (CommitRename, Delete, Paste, Move) so that the file grid's item
    // pointer stays valid for the remainder of the frame.
    bool m_pendingCacheInvalidation = false;
    // Model expand/collapse only needs augmented sub-asset rows rebuilt — not a full dir rescan.
    bool m_pendingAugmentedCacheInvalidation = false;

    // Stable tree expansion projections. User toggles are submitted through
    // TreeViewStateService; these models only drive native rendering.
    EditorTreeProjectionModel<std::string> m_folderTreeProjection;
    EditorTreeProjectionModel<std::string> m_modelTreeProjection;

    // Folder rows are flattened only when the directory snapshot or expansion
    // projection changes. Rendering then submits just the rows intersecting
    // the child window's clipper range instead of recursively visiting every
    // expanded directory on every GUI frame.
    struct FolderTreeRow
    {
        std::string path;
        std::string name;
        std::string imguiId;
        std::string pathKey;
        std::string semanticId;
        int depth = 0;
        bool hasSubdirs = false;
        bool isRoot = false;
        bool expanded = false;
    };
    std::vector<FolderTreeRow> m_folderTreeRows;
    uint64_t m_directoryRevision = 1;
    uint64_t m_folderTreeRowsDirectoryRevision = 0;
    uint64_t m_folderTreeRowsProjectionRevision = UINT64_MAX;
    uint64_t m_folderTreeAppliedProjectionRevision = UINT64_MAX;

    enum class ProjectItemsCacheSource : uint8_t
    {
        None,
        Snapshot,
        Augmented,
    };

    // Current-directory projection cache stores only stable identity and
    // revisions. Container values are reacquired on every access so an
    // unordered_map insertion/rehash cannot invalidate a retained pointer.
    std::string m_projectItemsCachePath;
    uint64_t m_projectItemsCacheDirectoryRevision = 0;
    uint64_t m_projectItemsCacheProjectionRevision = UINT64_MAX;
    uint64_t m_projectItemsCacheSnapshotMtime = 0;
    ProjectItemsCacheSource m_projectItemsCacheSource = ProjectItemsCacheSource::None;

    // Visible items for shift-range select
    std::vector<FileItem> *m_visibleItems = nullptr;

    // External refs
    Infernux *m_engine = nullptr;
    InxRenderer *m_renderer = nullptr;
    AssetDatabase *m_assetDatabase = nullptr;

    // ── Extension sets ───────────────────────────────────────────────
    static bool IsImageExt(const std::string &ext);
    static bool IsMaterialExt(const std::string &ext);
    static bool IsModelExt(const std::string &ext);

    // ── Rendering helpers ────────────────────────────────────────────
    void RenderBreadcrumb(InxGUIContext *ctx);
    void ResetAsyncSearch();
    void PollSearchCompletion();
    void ScheduleSearch(const EditorSearchToken &token, uint64_t generation, const std::string &folderRoot);
    void UpdateSearchResults();
    void RenderSearchResults(InxGUIContext *ctx);
    void RebuildFolderTreeRows();
    void RenderFolderTree(InxGUIContext *ctx);
    void RenderFileGrid(InxGUIContext *ctx);
    void RenderContextMenu(InxGUIContext *ctx);
    void RenderDragDropSource(InxGUIContext *ctx, const FileItem &item);
    void RenderFolderDropTarget(InxGUIContext *ctx, const std::string &folderPath);
    void RenderItemLabel(InxGUIContext *ctx, const FileItem &item, float iconSize, float cellStartX);
    [[nodiscard]] std::string MakeProjectFolderSemanticId(const std::string &path) const;
    [[nodiscard]] std::string MakeProjectItemSemanticId(const FileItem &item) const;
    [[nodiscard]] std::string MakeProjectRelativeSemanticPath(const std::string &path) const;

    // ── Click & keyboard handling ────────────────────────────────────
    void HandleItemClick(const FileItem &item, InxGUIContext *ctx);
    void HandleExternalFileDrops();
    bool ImportExternalAssetBatch(const std::vector<std::string> &paths, const std::string &source);

    [[nodiscard]] bool IsCtrl(InxGUIContext *ctx) const;
    [[nodiscard]] bool IsShift(InxGUIContext *ctx) const;

    // ── Rename helpers ───────────────────────────────────────────────
    void BeginRename(const std::string &path);
    void CommitRename();
    void CancelRename();
    /// Immediately clear directory caches. Prefer InvalidateDirCache() which defers
    /// until the next OnRenderContent so mid-frame item pointers stay valid.
    void ClearDirCachesNow();

    bool ExecuteEditorCommand(const std::string &commandId, const std::string &argument = "",
                              const std::string &source = "context_menu") const;

    // ── Move helpers ─────────────────────────────────────────────────
    std::vector<std::string> GetDragMoveSources(const std::string &draggedPath) const;
    std::string ResolveMovePayloadPath(const std::string &payloadType, const std::string &payload) const;
    void MoveProjectItemsToFolder(const std::string &targetDir, const std::string &payloadType,
                                  const std::string &payload);

    // ── Path utility ─────────────────────────────────────────────────
    /// Directory depth relative to m_rootPath (root=0, Assets=1, Assets/Mats=2, …).
    int GetPathDepthFromRoot(const std::string &path) const;
    /// Lowest folder users may browse (Assets/Logs when present, else project root).
    std::string GetMinimumBrowsePath() const;
    void ClampNavigationPath();
    void UpdateNavigationCache();
    void AssignCurrentPath(const std::string &path);
    bool RequestDirectoryNavigation(const std::string &path, const std::string &source = "pointer") const;
    bool RequestAssetLocation(const std::string &path, const std::string &source = "pointer") const;
    /// True when [..] may move to the parent folder (blocked at project-root subfolders).
    bool CanNavigateUpFromCurrent() const;
    static uint64_t GetMtimeNs(const std::string &path);

    // ── Grid layout ──────────────────────────────────────────────────
    static constexpr int ICON_SIZE = 64;
    static constexpr int GRID_PADDING = 10;
    static constexpr int CELL_WIDTH = ICON_SIZE + GRID_PADDING;
    static constexpr double DIR_CACHE_TTL = 0.5; // seconds before re-checking mtime

    double m_frameTimeNow = 0.0; // steady_clock time cached once per frame

    struct GridRange
    {
        int startIndex = 0;
        int endIndex = 0;
        float topSpacer = 0.0f;
        float bottomSpacer = 0.0f;
    };
    GridRange GetVisibleGridRange(InxGUIContext *ctx, int itemCount, int cols, float rowHeight,
                                  float startY = 0.0f) const;
    float GetGridTextLineHeight(InxGUIContext *ctx);
    const LabelEntry &GetCachedItemLabel(InxGUIContext *ctx, const FileItem &item, float textRegionW);
};

} // namespace infernux
