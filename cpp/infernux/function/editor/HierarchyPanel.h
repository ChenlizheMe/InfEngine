#pragma once

#include <function/editor/EditorPanel.h>
#include <function/editor/EditorTheme.h>
#include <function/editor/interaction/EditorCollectionModel.h>
#include <function/editor/interaction/EditorSearchModel.h>

#include <cstdint>
#include <functional>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <utility>
#include <vector>

namespace infernux
{

class GameObject;
class Scene;

/// C++ native Hierarchy panel — Unity-style scene tree with drag-drop,
/// inline rename, virtual scrolling, and search filtering.
///
/// Heavy-lift rendering happens entirely in C++.  Python-only managers
/// (Undo, Prefab, Clipboard, UI structure queries) are reached via
/// std::function callbacks set from the bootstrap layer.
class HierarchyPanel : public EditorPanel
{
  public:
    HierarchyPanel();
    std::unordered_map<std::string, double> ConsumeSubTimings() override;

    // ── Public API (called from Python bootstrap / other panels) ─────

    void ClearSearch();
    void RequestSearchFocus()
    {
        m_focusSearchNextFrame = true;
    }
    void ClearSelectionAndNotify();
    void SetSelectedObjectById(uint64_t id, bool clearSearch = false);
    void SetSelectionSnapshot(const std::vector<uint64_t> &ids, uint64_t primary);
    [[nodiscard]] std::vector<uint64_t> GetExpandedObjectIds() const;
    void SetExpandedObjectIds(const std::vector<uint64_t> &ids);
    void SetRuntimeHiddenIds(const std::unordered_set<uint64_t> &ids);
    void SetSceneHeaderSnapshot(const std::string &sceneDisplayName, bool prefabMode,
                                const std::string &prefabDisplayName);
    void ExpandToObject(uint64_t objId);
    void InvalidateSceneStructureCache();
    void BeginRenameObject(uint64_t objId);

    /// Allow external panels (UIEditorPanel) to queue an auto-expand.
    void SetPendingExpandId(uint64_t id)
    {
        m_pendingExpandId = id;
    }

    // ── Selection callbacks (project typed SelectionService) ─────────

    std::function<bool(uint64_t)> isSelected;
    std::function<void(uint64_t)> selectId;
    std::function<void(uint64_t)> toggleId;
    std::function<void(uint64_t)> rangeSelectId;
    std::function<void()> clearSelection;
    std::function<uint64_t()> getPrimary;
    std::function<std::vector<uint64_t>()> getSelectedIds;
    std::function<int()> selectionCount;
    std::function<bool()> isSelectionEmpty;
    std::function<void(const std::vector<uint64_t> &)> setOrderedIds;

    // ── Scene info callbacks ─────────────────────────────────────────

    std::function<std::string()> getSceneDisplayName;
    std::function<bool()> isPrefabMode;
    std::function<std::string()> getPrefabDisplayName;

    // ── Runtime hidden objects ───────────────────────────────────────

    std::function<std::unordered_set<uint64_t>()> getRuntimeHiddenIds;

    // ── UI structure queries (need Python py_components) ───────────

    std::function<bool(uint64_t)> goHasCanvas;
    std::function<bool(uint64_t)> goHasUiScreenComponent;
    std::function<bool(uint64_t)> parentHasCanvasAncestor;

    // ── Unified editor command entry points ─────────────────────────

    /// Draw the popup body from a frozen
    /// (targetId, targetIsPrefab, createParentId) snapshot.
    std::function<void(InxGUIContext *, uint64_t, bool, uint64_t)> renderContextMenu;

    // ── Translation ──────────────────────────────────────────────────

    std::function<std::string(const std::string &)> translate;

    // ── Warning display ──────────────────────────────────────────────

    std::function<void(const std::string &)> showWarning;

    // ── Drag-drop payload type ───────────────────────────────────────

    static constexpr const char *DRAG_DROP_TYPE = "HIERARCHY_GAMEOBJECT";

  protected:
    void OnRenderContent(InxGUIContext *ctx) override;
    void VisiblePreRender(InxGUIContext *ctx) override;

  private:
    // ── Translation cache ────────────────────────────────────────────
    const std::string &Tr(const std::string &key);
    std::unordered_map<std::string, std::string> m_trCache;

    // ── Cached selection state (synced once per frame) ───────────────
    void SyncSelectionCache();
    std::unordered_set<uint64_t> m_selIds;
    std::vector<uint64_t> m_selOrderedIds;
    uint64_t m_selPrimary = 0;
    int m_selCount = 0;
    bool m_selectionPushMode = false;

    // ── Runtime hidden IDs ───────────────────────────────────────────
    std::unordered_set<uint64_t> m_hiddenIds;
    bool m_runtimeHiddenPushMode = false;

    // ── Scene header state ───────────────────────────────────────────
    bool IsPrefabModeActive() const;
    std::string SceneDisplayName() const;
    std::string PrefabDisplayName() const;
    std::string m_sceneDisplayName;
    std::string m_prefabDisplayName;
    bool m_cachedPrefabMode = false;
    bool m_sceneHeaderPushMode = false;

    // ── Root-object cache ────────────────────────────────────────────
    std::string m_cachedSceneKey;
    uint64_t m_cachedStructureVer = UINT64_MAX;
    size_t m_cachedRawRootCount = 0;
    std::vector<GameObject *> m_cachedRoots;
    float m_lastRootRefreshTime = 0.0f;
    static constexpr float STALE_ROOT_INTERVAL = 0.12f;
    static constexpr int STALE_ROOT_THRESHOLD = 128;

    // ── Ordered IDs cache (for shift-range select) ───────────────────
    std::vector<uint64_t> m_cachedOrderedIds;
    bool m_orderedIdsDirty = true;

    // ── Search ───────────────────────────────────────────────────────
    char m_searchBuf[256] = {};
    EditorSearchModel m_search;
    std::unordered_map<uint64_t, bool> m_searchVisCache;
    bool m_focusSearchNextFrame = false;

    // ── Virtual scrolling ────────────────────────────────────────────
    float m_cachedItemHeight = 18.0f;
    bool m_itemHeightMeasured = false;

    // ── Flat virtual scrolling ───────────────────────────────────────
    struct FlatItem
    {
        GameObject *obj;
        int depth;
        bool hasVisibleChildren;
    };
    std::vector<FlatItem> m_flatItems;
    EditorTreeProjectionModel<uint64_t> m_treeProjection;
    std::unordered_set<uint64_t> m_forceExpandIds; // one-shot SetNextItemOpen
    bool m_flatListDirty = true;                   // rebuild flat list when true

    void BuildFlatVisibleList(const std::vector<GameObject *> &roots);
    void RebuildFlatListIfNeeded(const std::vector<GameObject *> &roots);
    void BuildFlatListRecurse(GameObject *obj, int depth);
    void BuildFlatListRecurse(GameObject *obj, int depth, std::vector<FlatItem> &items);
    void RenderFlatItem(InxGUIContext *ctx, const FlatItem &item, float baseIndentX, float indentStep);

    // ── Pending selection (deferred left-click) ──────────────────────
    uint64_t m_pendingSelectId = 0;
    bool m_pendingCtrl = false;
    bool m_pendingShift = false;
    uint64_t m_lastObservedPrimaryId = 0;
    uint64_t m_scrollToObjectId = 0;
    bool m_forceRootRefresh = false;

    // ── Pending auto-expand ──────────────────────────────────────────
    uint64_t m_pendingExpandId = 0;
    std::unordered_set<uint64_t> m_pendingExpandIds;

    // ── Inline rename ────────────────────────────────────────────────
    uint64_t m_renameId = 0;
    char m_renameBuf[256] = {};
    bool m_renameFocus = false;
    // Context-menu dismissal can deactivate the newly opened input before it
    // receives focus. Keep rename alive until the input has settled.
    int m_renameSkipDeactivateFrames = 0;

    // ── Right-click tracking ─────────────────────────────────────────
    uint64_t m_rightClickedObjId = 0;

    // ── Split sub-timings (accumulated ms, consumed by profile) ────
    double m_subPreHidden = 0.0;
    double m_subPreSelection = 0.0;
    double m_subPreShortcuts = 0.0;
    double m_subPrePendingSelect = 0.0;
    double m_subHeader = 0.0;
    double m_subSearch = 0.0;
    double m_subRefreshRoots = 0.0;
    double m_subFilterRoots = 0.0;
    double m_subFlatBuild = 0.0;
    double m_subRows = 0.0;
    double m_subPopup = 0.0;
    double m_subTailDrop = 0.0;

    // ── Helpers ──────────────────────────────────────────────────────

    // Hidden-object filtering
    [[nodiscard]] bool IsHidden(uint64_t id) const;
    std::vector<GameObject *> FilterHidden(const std::vector<std::unique_ptr<GameObject>> &objects) const;
    void RefreshRootObjects(Scene *scene, bool allowStale, bool forceRefresh = false);

    // Search
    void SetSearchQuery(const char *text);
    [[nodiscard]] bool HasActiveSearch() const
    {
        return m_search.IsActive();
    }
    [[nodiscard]] bool MatchesSearch(GameObject *obj) const;
    bool IsVisibleInSearch(GameObject *obj);
    std::vector<GameObject *> FilterForSearch(const std::vector<GameObject *> &objects);

    // Tree rendering
    void RenderRenameInput(InxGUIContext *ctx, GameObject *obj);
    void RenderItemContextMenu(InxGUIContext *ctx, GameObject *obj);
    void RenderReorderSep(InxGUIContext *ctx, const char *sepId, std::function<void(uint64_t)> onDrop,
                          float indentPx = 0.0f, bool consumeSpace = false);
    void RenderMultiDropTarget(InxGUIContext *ctx, uint64_t parentId);

    // Selection
    [[nodiscard]] bool IsCtrl(InxGUIContext *ctx) const;
    [[nodiscard]] bool IsShift(InxGUIContext *ctx) const;

    // Drag-drop logic
    std::vector<uint64_t> GetDragIds(uint64_t primaryId);
    static std::vector<uint64_t> TopoSortIds(Scene *scene, const std::vector<uint64_t> &ids);
    void ReparentObject(uint64_t draggedId, uint64_t newParentId);
    void MoveObjectAdjacent(uint64_t draggedId, uint64_t targetId, bool after);
    void ReparentToRoot(uint64_t draggedId);
    void HandleExternalDrop(const std::string &dropType, uint64_t payload, uint64_t parentId = 0);
    void HandleExternalDropStr(const std::string &dropType, const std::string &payload, uint64_t parentId = 0);
    bool ValidateReparent(GameObject *obj, uint64_t newParentId, GameObject *newParent);
    bool ValidateMoveAdjacent(GameObject *obj, uint64_t newParentId, GameObject *newParent);
    static bool IsDescendantOf(GameObject *potentialChild, GameObject *potentialParent);
    [[nodiscard]] bool HasUiScreenComponentInSubtree(GameObject *obj) const;

    // Rename
    void BeginRename(uint64_t objId);
    void CommitRename();
    void CancelRename();

    // Ordered IDs
    std::vector<uint64_t> CollectOrderedIds(const std::vector<GameObject *> &roots) const;

    bool ExecuteEditorCommand(const std::string &commandId, const std::string &argument = "",
                              const std::string &source = "context_menu") const;
    // Context menus
};

} // namespace infernux
