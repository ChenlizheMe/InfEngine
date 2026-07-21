#pragma once

#include <function/editor/EditorPanel.h>
#include <function/editor/EditorTheme.h>

#include <cstdint>
#include <functional>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

/// A single data-driven entry for the Hierarchy "Create" context menu.
/// Python registers these via ``add_create_entry()`` — no C++ changes
/// needed when adding new component types.
struct HierarchyCreateEntry
{
    std::string category;                   ///< Menu section: "Rendering", "UI", etc.
    std::string localeKey;                  ///< Translation key, e.g. "hierarchy.camera"
    std::function<void(uint64_t)> callback; ///< (parentId) → void
};

namespace infernux
{

class GameObject;
class Scene;

/// C++ native Hierarchy panel — Unity-style scene tree with drag-drop,
/// inline rename, virtual scrolling, search filtering, and UI mode.
///
/// Heavy-lift rendering happens entirely in C++.  Python-only managers
/// (Undo, Prefab, Clipboard, UICanvas queries) are reached via
/// std::function callbacks set from the bootstrap layer.
class HierarchyPanel : public EditorPanel
{
  public:
    HierarchyPanel();
    std::unordered_map<std::string, double> ConsumeSubTimings() override;

    // ── Public API (called from Python bootstrap / other panels) ─────

    void SetUiMode(bool enabled);
    [[nodiscard]] bool GetUiMode() const
    {
        return m_uiMode;
    }

    void ClearSearch();
    void ClearSelectionAndNotify();
    void SetSelectedObjectById(uint64_t id, bool clearSearch = false);
    void SetSelectionSnapshot(const std::vector<uint64_t> &ids, uint64_t primary);
    void SetRuntimeHiddenIds(const std::unordered_set<uint64_t> &ids);
    void SetSceneHeaderSnapshot(const std::string &sceneDisplayName, bool prefabMode,
                                const std::string &prefabDisplayName);
    void ExpandToObject(uint64_t objId);
    void InvalidateSceneStructureCache();

    /// Allow external panels (UIEditorPanel) to queue an auto-expand.
    void SetPendingExpandId(uint64_t id)
    {
        m_pendingExpandId = id;
    }

    // ── Selection callbacks (wrap Python SelectionManager) ───────────

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

    // ── Notification callbacks ───────────────────────────────────────

    /// Called when selection changes (receives primary object ID, 0 = none).
    std::function<void(uint64_t)> onSelectionChanged;
    /// Called on double-click (receives object ID to focus camera on).
    std::function<void(uint64_t)> onDoubleClickFocus;
    /// Extra callback for UI-editor sync.
    std::function<void(uint64_t)> onSelectionChangedUiEditor;

    // ── Undo callbacks ───────────────────────────────────────────────

    std::function<void(uint64_t, const std::string &)> undoRecordCreate;
    std::function<void(uint64_t, const std::string &)> undoRecordDelete;
    std::function<void(uint64_t, const std::string &, const std::string &)> undoRecordRename;
    /// (objId, oldParentId, newParentId, oldSibIdx, newSibIdx)
    std::function<void(uint64_t, uint64_t, uint64_t, int, int)> undoRecordMove;

    // ── Scene info callbacks ─────────────────────────────────────────

    std::function<std::string()> getSceneDisplayName;
    std::function<bool()> isPrefabMode;
    std::function<std::string()> getPrefabDisplayName;

    // ── Runtime hidden objects ───────────────────────────────────────

    std::function<std::unordered_set<uint64_t>()> getRuntimeHiddenIds;

    // ── Canvas / UI-mode queries (need Python py_components) ────────

    std::function<bool(uint64_t)> goHasCanvas;
    std::function<bool(uint64_t)> goHasUiScreenComponent;
    std::function<bool(uint64_t)> parentHasCanvasAncestor;
    std::function<bool(uint64_t)> hasCanvasDescendant;
    std::function<std::vector<uint64_t>()> getCanvasRootIds;

    // ── Context-menu action callbacks ────────────────────────────────

    std::function<void(int, uint64_t)> createPrimitive; // (typeIdx, parentId)
    std::function<void(int, uint64_t)> createLight;     // (typeIdx, parentId)
    std::function<void(uint64_t)> createEmpty;

    /// Data-driven create-menu entries (populated from Python).
    std::vector<HierarchyCreateEntry> createEntries;
    void AddCreateEntry(const std::string &category, const std::string &localeKey,
                        std::function<void(uint64_t)> callback);
    void ClearCreateEntries();

    std::function<void(uint64_t)> saveAsPrefab;
    std::function<void(uint64_t)> prefabSelectAsset;
    std::function<void(uint64_t)> prefabOpenAsset;
    std::function<void(uint64_t)> prefabApplyOverrides;
    std::function<void(uint64_t)> prefabRevertOverrides;
    std::function<void(uint64_t)> prefabUnpack;

    // ── Focus callback ──────────────────────────────────────────────

    std::function<void(bool)> onHierarchyPanelFocused;

    // ── Clipboard callbacks ──────────────────────────────────────────

    std::function<bool(bool)> copySelected; // (cut) → success
    std::function<bool()> pasteClipboard;
    std::function<bool()> hasClipboardData;

    // ── External drop callbacks (from Project panel) ─────────────────

    std::function<void(const std::string &, uint64_t, bool)> instantiatePrefab;
    std::function<void(const std::string &, uint64_t, bool)> createModelObject;

    // ── Delete-selected callback ─────────────────────────────────────

    std::function<void()> deleteSelectedObjects;

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

    // ── Canvas root IDs ──────────────────────────────────────────────
    std::unordered_set<uint64_t> m_canvasRootIds;
    bool m_canvasRootsDirty = true;

    // ── Search ───────────────────────────────────────────────────────
    char m_searchBuf[256] = {};
    std::string m_searchQuery;
    std::string m_searchQueryNorm;
    std::unordered_map<uint64_t, bool> m_searchVisCache;

    // ── UI mode ──────────────────────────────────────────────────────
    bool m_uiMode = false;

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
    std::unordered_set<uint64_t> m_expandedNodes;
    std::unordered_set<uint64_t> m_forceExpandIds; // one-shot SetNextItemOpen
    bool m_flatListDirty = true;                   // rebuild flat list when true

    void BuildFlatVisibleList(const std::vector<GameObject *> &roots);
    void RebuildFlatListIfNeeded(const std::vector<GameObject *> &roots);
    void BuildFlatListRecurse(GameObject *obj, int depth);
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

    // ── Focus tracking ───────────────────────────────────────────────
    bool m_wasFocused = false;

    // ── Split sub-timings (accumulated ms, consumed by profile) ────
    double m_subPreHidden = 0.0;
    double m_subPreSelection = 0.0;
    double m_subPreShortcuts = 0.0;
    double m_subPrePendingSelect = 0.0;
    double m_subHeader = 0.0;
    double m_subSearch = 0.0;
    double m_subRefreshRoots = 0.0;
    double m_subCanvasRoots = 0.0;
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
        return !m_searchQueryNorm.empty();
    }
    [[nodiscard]] bool MatchesSearch(GameObject *obj) const;
    bool IsVisibleInSearch(GameObject *obj);
    std::vector<GameObject *> FilterForSearch(const std::vector<GameObject *> &objects);

    // Canvas helpers
    [[nodiscard]] bool IsInCanvasTree(GameObject *obj) const;
    void RefreshCanvasRootIds(const std::vector<GameObject *> &roots);

    // Tree rendering
    void RenderGameObjectTree(InxGUIContext *ctx, GameObject *obj);
    void RenderRenameInput(InxGUIContext *ctx, GameObject *obj);
    void RenderItemContextMenu(InxGUIContext *ctx, GameObject *obj);
    void RenderReorderSep(InxGUIContext *ctx, const char *sepId, std::function<void(uint64_t)> onDrop,
                          float indentPx = 0.0f, bool consumeSpace = false);
    void RenderMultiDropTarget(InxGUIContext *ctx, uint64_t parentId);

    // Selection
    void NotifySelectionChanged();
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

    // Rename
    void BeginRename(uint64_t objId);
    void CommitRename();
    void CancelRename();

    // Ordered IDs
    std::vector<uint64_t> CollectOrderedIds(const std::vector<GameObject *> &roots) const;

    // Clipboard shortcuts
    void HandleClipboardShortcuts(InxGUIContext *ctx);

    // Context menus
    void ShowCreatePrimitiveMenu(InxGUIContext *ctx, uint64_t parentId);
    void ShowCreateLightMenu(InxGUIContext *ctx, uint64_t parentId);
    void ShowCreate2DMenu(InxGUIContext *ctx, uint64_t parentId);
    void ShowPostProcessingMenu(InxGUIContext *ctx, uint64_t parentId);
    void ShowUiMenu(InxGUIContext *ctx, uint64_t parentId);
    void ShowUiModeContextMenu(InxGUIContext *ctx, uint64_t parentId);
    void ShowCreateEntriesForCategory(InxGUIContext *ctx, uint64_t parentId, const std::string &category);
};

} // namespace infernux
