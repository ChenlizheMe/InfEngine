#include "HierarchyPanel.h"

#include <function/renderer/gui/InxGUISemantics.h>
#include <function/scene/GameObject.h>
#include <function/scene/Scene.h>
#include <function/scene/SceneManager.h>
#include <function/scene/Transform.h>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstring>

// ImGui key constants (must match imgui.h ImGuiKey enum)
static constexpr int kKeyLeftCtrl = ImGuiKey_LeftCtrl;
static constexpr int kKeyRightCtrl = ImGuiKey_RightCtrl;
static constexpr int kKeyLeftShift = ImGuiKey_LeftShift;
static constexpr int kKeyRightShift = ImGuiKey_RightShift;

namespace infernux
{

// ════════════════════════════════════════════════════════════════════
// Helpers — casefold for search
// ════════════════════════════════════════════════════════════════════

static std::string ExternalDropCommandArgument(const std::string &reference, uint64_t parentId, bool isGuid)
{
    return reference + "\t" + std::to_string(parentId) + "\t" + (isGuid ? "1" : "0");
}

static std::string RenameCommandArgument(uint64_t objectId, const std::string &name)
{
    return std::to_string(objectId) + "\t" + name;
}

static std::string TreeExpandedCommandArgument(uint64_t objectId, bool expanded)
{
    return std::to_string(objectId) + "\t" + (expanded ? "1" : "0");
}

static std::string MoveCommandArgument(const std::vector<uint64_t> &objectIds, const std::string &mode,
                                       uint64_t targetId, bool after)
{
    std::string ids;
    for (const uint64_t objectId : objectIds) {
        if (!ids.empty())
            ids.push_back(',');
        ids += std::to_string(objectId);
    }
    return ids + "\t" + mode + "\t" + std::to_string(targetId) + "\t" + (after ? "1" : "0");
}

// ════════════════════════════════════════════════════════════════════
// Construction
// ════════════════════════════════════════════════════════════════════

HierarchyPanel::HierarchyPanel() : EditorPanel("Hierarchy", "hierarchy")
{
}

std::unordered_map<std::string, double> HierarchyPanel::ConsumeSubTimings()
{
    std::unordered_map<std::string, double> out;
    out["pre_hidden"] = m_subPreHidden;
    out["pre_select"] = m_subPreSelection;
    out["pre_shortcuts"] = m_subPreShortcuts;
    out["pre_pending"] = m_subPrePendingSelect;
    out["header"] = m_subHeader;
    out["search"] = m_subSearch;
    out["refresh"] = m_subRefreshRoots;
    out["filterRoots"] = m_subFilterRoots;
    out["flatBuild"] = m_subFlatBuild;
    out["rows"] = m_subRows;
    out["popup"] = m_subPopup;
    out["tailDrop"] = m_subTailDrop;

    m_subPreHidden = 0.0;
    m_subPreSelection = 0.0;
    m_subPreShortcuts = 0.0;
    m_subPrePendingSelect = 0.0;
    m_subHeader = 0.0;
    m_subSearch = 0.0;
    m_subRefreshRoots = 0.0;
    m_subFilterRoots = 0.0;
    m_subFlatBuild = 0.0;
    m_subRows = 0.0;
    m_subPopup = 0.0;
    m_subTailDrop = 0.0;
    return out;
}

// ════════════════════════════════════════════════════════════════════
// Translation helper
// ════════════════════════════════════════════════════════════════════

const std::string &HierarchyPanel::Tr(const std::string &key)
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

void HierarchyPanel::InvalidateSceneStructureCache()
{
    m_cachedSceneKey.clear();
    m_cachedStructureVer = UINT64_MAX;
    m_lastRootRefreshTime = 0.0f;
    m_orderedIdsDirty = true;
    m_searchVisCache.clear();
    m_itemHeightMeasured = false;
    m_flatItems.clear();
    m_flatListDirty = true;
}

void HierarchyPanel::ClearSearch()
{
    SetSearchQuery("");
}

void HierarchyPanel::ClearSelectionAndNotify()
{
    if (clearSelection)
        clearSelection();
    SyncSelectionCache();
}

void HierarchyPanel::SetSelectedObjectById(uint64_t id, bool clearSearchFirst)
{
    if (id == 0)
        id = 0;
    if (clearSearchFirst)
        ClearSearch();

    uint64_t curPrimary = getPrimary ? getPrimary() : 0;
    int curCount = selectionCount ? selectionCount() : 0;
    bool changed = (curPrimary != id || curCount != 1);
    if (changed && selectId)
        selectId(id);

    // This API represents an explicit single selection. Keep the native
    // snapshot coherent immediately; push-mode listeners may run before or
    // after the Python selection callback depending on where the action
    // originated in the current ImGui frame.
    m_selIds.clear();
    m_selOrderedIds.clear();
    if (id) {
        m_selIds.insert(id);
        m_selOrderedIds.push_back(id);
        m_selCount = 1;
    } else {
        m_selCount = 0;
    }
    m_selPrimary = id;

    // Always expand the parent chain
    if (id) {
        ExpandToObject(id);
        m_scrollToObjectId = id;
        const bool missingFromCache = std::none_of(m_flatItems.begin(), m_flatItems.end(), [id](const FlatItem &item) {
            return item.obj && item.obj->GetID() == id;
        });
        m_forceRootRefresh = missingFromCache;
        if (missingFromCache) {
            Scene *scene = SceneManager::Instance().GetActiveScene();
            GameObject *selected = SceneManager::Instance().FindRuntimeObjectByID(id);
            if (selected) {
                // Runtime-only IDs can be recycled after leaving Play Mode.
                // An explicitly revealed live scene object must not inherit a
                // stale hidden marker from the previous runtime world.
                m_hiddenIds.erase(id);
                RefreshRootObjects(scene, false, true);
                m_forceRootRefresh = false;
            }
        }
    }
}

void HierarchyPanel::ExpandToObject(uint64_t objId)
{
    if (objId == 0)
        return;
    Scene *scene = SceneManager::Instance().GetActiveScene();
    if (!scene)
        return;
    GameObject *go = SceneManager::Instance().FindRuntimeObjectByID(objId);
    if (!go)
        return;
    GameObject *parent = go->GetParent();
    while (parent) {
        uint64_t pid = parent->GetID();
        if (m_treeProjection.SetExpanded(pid, true))
            m_flatListDirty = true;
        m_forceExpandIds.insert(pid);
        parent = parent->GetParent();
    }
}

// ════════════════════════════════════════════════════════════════════
// Selection cache — sync once per frame
// ════════════════════════════════════════════════════════════════════

void HierarchyPanel::SyncSelectionCache()
{
    if (m_selectionPushMode)
        return;
    m_selIds.clear();
    m_selOrderedIds.clear();
    if (getSelectedIds) {
        m_selOrderedIds = getSelectedIds();
        for (auto id : m_selOrderedIds)
            m_selIds.insert(id);
    }
    m_selPrimary = getPrimary ? getPrimary() : 0;
    m_selCount = selectionCount ? selectionCount() : 0;
}

void HierarchyPanel::SetSelectionSnapshot(const std::vector<uint64_t> &ids, uint64_t primary)
{
    m_selectionPushMode = true;
    m_selOrderedIds = ids;
    m_selIds.clear();
    m_selIds.insert(ids.begin(), ids.end());
    m_selPrimary = primary;
    m_selCount = static_cast<int>(ids.size());
}

std::vector<uint64_t> HierarchyPanel::GetExpandedObjectIds() const
{
    std::vector<uint64_t> ids(m_treeProjection.ExpandedIds().begin(), m_treeProjection.ExpandedIds().end());
    std::sort(ids.begin(), ids.end());
    return ids;
}

void HierarchyPanel::SetExpandedObjectIds(const std::vector<uint64_t> &ids)
{
    const std::unordered_set<uint64_t> expanded(ids.begin(), ids.end());
    if (m_treeProjection.ReplaceExpanded(expanded)) {
        m_flatListDirty = true;
        m_orderedIdsDirty = true;
    }
}

void HierarchyPanel::SetRuntimeHiddenIds(const std::unordered_set<uint64_t> &ids)
{
    m_runtimeHiddenPushMode = true;
    m_hiddenIds = ids;
}

void HierarchyPanel::SetSceneHeaderSnapshot(const std::string &sceneDisplayName, bool prefabMode,
                                            const std::string &prefabDisplayName)
{
    m_sceneHeaderPushMode = true;
    m_sceneDisplayName = sceneDisplayName;
    m_cachedPrefabMode = prefabMode;
    m_prefabDisplayName = prefabDisplayName;
}

bool HierarchyPanel::IsPrefabModeActive() const
{
    return m_sceneHeaderPushMode ? m_cachedPrefabMode : (isPrefabMode && isPrefabMode());
}

std::string HierarchyPanel::SceneDisplayName() const
{
    return m_sceneHeaderPushMode ? m_sceneDisplayName : (getSceneDisplayName ? getSceneDisplayName() : "");
}

std::string HierarchyPanel::PrefabDisplayName() const
{
    return m_sceneHeaderPushMode ? m_prefabDisplayName : (getPrefabDisplayName ? getPrefabDisplayName() : "Prefab");
}

// ════════════════════════════════════════════════════════════════════
// Hidden-object filtering
// ════════════════════════════════════════════════════════════════════

bool HierarchyPanel::IsHidden(uint64_t id) const
{
    return m_hiddenIds.count(id) > 0;
}

std::vector<GameObject *> HierarchyPanel::FilterHidden(const std::vector<std::unique_ptr<GameObject>> &objects) const
{
    std::vector<GameObject *> out;
    out.reserve(objects.size());
    for (auto &obj : objects) {
        if (!IsHidden(obj->GetID()))
            out.push_back(obj.get());
    }
    return out;
}

void HierarchyPanel::RefreshRootObjects(Scene *scene, bool allowStale, bool forceRefresh)
{
    if (!scene) {
        m_cachedRoots.clear();
        m_cachedRawRootCount = 0;
        return;
    }
    std::string sceneKey = scene->GetName();
    uint64_t ver = scene->GetStructureVersion();
    const size_t rawRootCount = scene->GetRootObjects().size();

    float now = ImGui::GetTime();
    bool canReuseStale =
        (allowStale && m_cachedSceneKey == sceneKey && !m_cachedRoots.empty() && rawRootCount == m_cachedRawRootCount &&
         static_cast<int>(m_cachedRoots.size()) >= STALE_ROOT_THRESHOLD &&
         (now - m_lastRootRefreshTime) < STALE_ROOT_INTERVAL);

    if (forceRefresh || rawRootCount != m_cachedRawRootCount || sceneKey != m_cachedSceneKey ||
        (ver != m_cachedStructureVer && !canReuseStale)) {
        m_cachedRoots = FilterHidden(scene->GetRootObjects());
        m_orderedIdsDirty = true;
        m_searchVisCache.clear();
        m_itemHeightMeasured = false;
        m_flatListDirty = true;
        m_cachedSceneKey = sceneKey;
        m_cachedStructureVer = ver;
        m_cachedRawRootCount = rawRootCount;
        m_lastRootRefreshTime = now;
    }
}

// ════════════════════════════════════════════════════════════════════
// Search
// ════════════════════════════════════════════════════════════════════

void HierarchyPanel::SetSearchQuery(const char *text)
{
    if (!m_search.SetQuery(text ? text : ""))
        return;
    m_searchVisCache.clear();
    m_flatListDirty = true;
}

bool HierarchyPanel::MatchesSearch(GameObject *obj) const
{
    return m_search.Matches(obj->GetName());
}

bool HierarchyPanel::IsVisibleInSearch(GameObject *obj)
{
    if (!m_search.IsActive())
        return true;
    uint64_t id = obj->GetID();
    auto it = m_searchVisCache.find(id);
    if (it != m_searchVisCache.end())
        return it->second;

    bool visible = MatchesSearch(obj);
    if (!visible) {
        for (auto &child : obj->GetChildren()) {
            if (!IsHidden(child->GetID()) && IsVisibleInSearch(child.get())) {
                visible = true;
                break;
            }
        }
    }
    m_searchVisCache[id] = visible;
    return visible;
}

std::vector<GameObject *> HierarchyPanel::FilterForSearch(const std::vector<GameObject *> &objects)
{
    if (!HasActiveSearch())
        return objects;
    std::vector<GameObject *> out;
    for (auto *obj : objects) {
        if (IsVisibleInSearch(obj))
            out.push_back(obj);
    }
    return out;
}

// ════════════════════════════════════════════════════════════════════
// Flat virtual scrolling — build a flat list of visible items
// ════════════════════════════════════════════════════════════════════

void HierarchyPanel::BuildFlatVisibleList(const std::vector<GameObject *> &roots)
{
    m_flatItems.clear();
    m_flatItems.reserve(roots.size() * 2); // heuristic
    for (auto *root : roots)
        BuildFlatListRecurse(root, 0);
    m_flatListDirty = false;
}

void HierarchyPanel::RebuildFlatListIfNeeded(const std::vector<GameObject *> &roots)
{
    if (m_flatListDirty) {
        BuildFlatVisibleList(roots);
    }
}

void HierarchyPanel::BuildFlatListRecurse(GameObject *obj, int depth)
{
    BuildFlatListRecurse(obj, depth, m_flatItems);
}

void HierarchyPanel::BuildFlatListRecurse(GameObject *obj, int depth, std::vector<FlatItem> &items)
{
    if (!obj)
        return;
    if (HasActiveSearch() && !IsVisibleInSearch(obj))
        return;

    uint64_t objId = obj->GetID();
    const auto &children = obj->GetChildren();

    // Check for visible children without allocating a vector
    bool hasVisibleChildren = false;
    for (const auto &child : children) {
        if (IsHidden(child->GetID()))
            continue;
        if (HasActiveSearch() && !IsVisibleInSearch(child.get()))
            continue;
        hasVisibleChildren = true;
        break;
    }

    items.push_back({obj, depth, hasVisibleChildren});

    // Determine expanded state
    bool isExpanded = m_treeProjection.IsExpanded(objId);

    // Search expansion is transient.  Do not write it into the persistent
    // user-authored expansion set, otherwise clearing a search permanently
    // changes the tree layout.
    if (HasActiveSearch() && hasVisibleChildren) {
        isExpanded = true;
    }

    if (hasVisibleChildren && isExpanded) {
        for (const auto &child : children) {
            if (IsHidden(child->GetID()))
                continue;
            BuildFlatListRecurse(child.get(), depth + 1, items);
        }
    }
}

// ════════════════════════════════════════════════════════════════════
// Keyboard helpers
// ════════════════════════════════════════════════════════════════════

bool HierarchyPanel::IsCtrl(InxGUIContext *ctx) const
{
    return ctx->IsKeyDown(kKeyLeftCtrl) || ctx->IsKeyDown(kKeyRightCtrl);
}

bool HierarchyPanel::IsShift(InxGUIContext *ctx) const
{
    return ctx->IsKeyDown(kKeyLeftShift) || ctx->IsKeyDown(kKeyRightShift);
}

// ════════════════════════════════════════════════════════════════════
// Ordered IDs (for shift-range select)
// ════════════════════════════════════════════════════════════════════

std::vector<uint64_t> HierarchyPanel::CollectOrderedIds(const std::vector<GameObject *> &roots) const
{
    std::vector<uint64_t> result;
    // Iterative DFS
    std::vector<GameObject *> stack;
    for (auto it = roots.rbegin(); it != roots.rend(); ++it)
        stack.push_back(*it);

    while (!stack.empty()) {
        auto *obj = stack.back();
        stack.pop_back();
        if (!obj || IsHidden(obj->GetID()))
            continue;
        result.push_back(obj->GetID());
        auto &children = obj->GetChildren();
        for (auto it = children.rbegin(); it != children.rend(); ++it) {
            if (!IsHidden(it->get()->GetID()))
                stack.push_back(it->get());
        }
    }
    return result;
}

// ════════════════════════════════════════════════════════════════════
// Drag-drop helpers
// ════════════════════════════════════════════════════════════════════

std::vector<uint64_t> HierarchyPanel::GetDragIds(uint64_t primaryId)
{
    if (m_selIds.count(primaryId) && m_selCount > 1)
        return m_selOrderedIds;
    return {primaryId};
}

std::vector<uint64_t> HierarchyPanel::TopoSortIds(Scene *scene, const std::vector<uint64_t> &ids)
{
    std::unordered_set<uint64_t> idSet(ids.begin(), ids.end());
    std::vector<uint64_t> ordered;
    ordered.reserve(ids.size());

    std::function<void(GameObject *)> walk = [&](GameObject *go) {
        uint64_t gid = go->GetID();
        if (idSet.count(gid)) {
            ordered.push_back(gid);
            idSet.erase(gid);
        }
        for (auto &child : go->GetChildren())
            walk(child.get());
    };

    for (auto &root : scene->GetRootObjects()) {
        walk(root.get());
        if (idSet.empty())
            break;
    }
    Scene *persistentScene = SceneManager::Instance().GetRuntimePersistentScene();
    if (persistentScene && persistentScene != scene) {
        for (const auto &root : persistentScene->GetRootObjects()) {
            walk(root.get());
            if (idSet.empty())
                break;
        }
    }
    // Append any remaining IDs not found in tree
    for (auto id : ids) {
        if (std::find(ordered.begin(), ordered.end(), id) == ordered.end())
            ordered.push_back(id);
    }
    return ordered;
}

bool HierarchyPanel::IsDescendantOf(GameObject *potentialChild, GameObject *potentialParent)
{
    GameObject *cur = potentialChild;
    while (cur) {
        if (cur->GetID() == potentialParent->GetID())
            return true;
        cur = cur->GetParent();
    }
    return false;
}

bool HierarchyPanel::HasUiScreenComponentInSubtree(GameObject *obj) const
{
    if (!obj)
        return false;
    if (goHasUiScreenComponent && goHasUiScreenComponent(obj->GetID()))
        return true;
    for (const auto &child : obj->GetChildren()) {
        if (HasUiScreenComponentInSubtree(child.get()))
            return true;
    }
    return false;
}

bool HierarchyPanel::ValidateReparent(GameObject *obj, uint64_t newParentId, GameObject *newParent)
{
    if (goHasCanvas && goHasCanvas(obj->GetID())) {
        if (showWarning)
            showWarning("Canvas can only be a root object.");
        return false;
    }
    if (HasUiScreenComponentInSubtree(obj)) {
        if (newParent == nullptr || (parentHasCanvasAncestor && !parentHasCanvasAncestor(newParentId))) {
            if (showWarning)
                showWarning("UI components must be placed under a Canvas.");
            return false;
        }
    }
    return true;
}

bool HierarchyPanel::ValidateMoveAdjacent(GameObject *obj, uint64_t newParentId, GameObject *newParent)
{
    const bool isCanvas = goHasCanvas && goHasCanvas(obj->GetID());
    if (isCanvas && newParentId != 0) {
        if (showWarning)
            showWarning("Canvas can only be a root object.");
        return false;
    }
    if (!isCanvas && HasUiScreenComponentInSubtree(obj)) {
        if (newParentId == 0 || (newParent && parentHasCanvasAncestor && !parentHasCanvasAncestor(newParentId))) {
            if (showWarning)
                showWarning("UI components must be placed under a Canvas.");
            return false;
        }
    }
    return true;
}

void HierarchyPanel::ReparentObject(uint64_t draggedId, uint64_t newParentId)
{
    Scene *scene = SceneManager::Instance().GetActiveScene();
    if (!scene)
        return;
    GameObject *newParent = SceneManager::Instance().FindRuntimeObjectByID(newParentId);
    if (!newParent)
        return;

    auto dragIds = GetDragIds(draggedId);
    auto sorted = TopoSortIds(scene, dragIds);

    std::vector<uint64_t> validIds;
    for (uint64_t did : sorted) {
        if (did == newParentId)
            continue;
        auto *obj = SceneManager::Instance().FindRuntimeObjectByID(did);
        if (!obj)
            continue;
        if (obj->GetScene() != newParent->GetScene())
            continue;
        if (IsDescendantOf(newParent, obj))
            continue;
        if (!ValidateReparent(obj, newParentId, newParent))
            continue;

        validIds.push_back(did);
    }
    if (!validIds.empty() &&
        ExecuteEditorCommand("scene.move_hierarchy", MoveCommandArgument(validIds, "parent", newParentId, false),
                             "drag_drop"))
        m_pendingExpandId = newParentId;
}

void HierarchyPanel::MoveObjectAdjacent(uint64_t draggedId, uint64_t targetId, bool after)
{
    Scene *scene = SceneManager::Instance().GetActiveScene();
    if (!scene)
        return;
    auto *targetObj = SceneManager::Instance().FindRuntimeObjectByID(targetId);
    if (!targetObj)
        return;

    auto *newParent = targetObj->GetParent();
    uint64_t newParentId = newParent ? newParent->GetID() : 0;

    auto dragIds = GetDragIds(draggedId);
    auto sorted = TopoSortIds(scene, dragIds);

    std::vector<uint64_t> validIds;
    for (uint64_t did : sorted) {
        if (did == targetId)
            continue;
        auto *obj = SceneManager::Instance().FindRuntimeObjectByID(did);
        if (!obj)
            continue;
        if (obj->GetScene() != targetObj->GetScene())
            continue;
        if (IsDescendantOf(targetObj, obj))
            continue;
        if (!ValidateMoveAdjacent(obj, newParentId, newParent))
            continue;
        validIds.push_back(did);
    }
    if (validIds.empty())
        return;

    if (ExecuteEditorCommand("scene.move_hierarchy", MoveCommandArgument(validIds, "adjacent", targetId, after),
                             "drag_drop") &&
        newParentId != 0)
        m_pendingExpandId = newParentId;
}

void HierarchyPanel::ReparentToRoot(uint64_t draggedId)
{
    Scene *scene = SceneManager::Instance().GetActiveScene();
    if (!scene)
        return;

    auto dragIds = GetDragIds(draggedId);
    auto sorted = TopoSortIds(scene, dragIds);

    std::vector<uint64_t> validIds;
    for (uint64_t did : sorted) {
        auto *obj = SceneManager::Instance().FindRuntimeObjectByID(did);
        if (!obj)
            continue;
        const bool isCanvas = goHasCanvas && goHasCanvas(obj->GetID());
        if (!isCanvas && HasUiScreenComponentInSubtree(obj)) {
            if (showWarning)
                showWarning("UI components must be placed under a Canvas.");
            continue;
        }

        validIds.push_back(did);
    }
    if (!validIds.empty())
        ExecuteEditorCommand("scene.move_hierarchy", MoveCommandArgument(validIds, "root", 0, false), "drag_drop");
}

void HierarchyPanel::HandleExternalDrop(const std::string &dropType, uint64_t payload, uint64_t parentId)
{
    // In Prefab Mode, force under prefab root
    if (IsPrefabModeActive() && parentId == 0) {
        Scene *scene = SceneManager::Instance().GetActiveScene();
        if (scene && !scene->GetRootObjects().empty())
            parentId = scene->GetRootObjects()[0]->GetID();
    }

    if (dropType == DRAG_DROP_TYPE) {
        if (parentId == 0)
            ReparentToRoot(payload);
        else
            ReparentObject(payload, parentId);
    }
}

void HierarchyPanel::HandleExternalDropStr(const std::string &dropType, const std::string &payload, uint64_t parentId)
{
    // In Prefab Mode, force under prefab root
    if (IsPrefabModeActive() && parentId == 0) {
        Scene *scene = SceneManager::Instance().GetActiveScene();
        if (scene && !scene->GetRootObjects().empty())
            parentId = scene->GetRootObjects()[0]->GetID();
    }

    if (dropType == "PREFAB_GUID" || dropType == "PREFAB_FILE") {
        const bool isGuid = (dropType == "PREFAB_GUID");
        ExecuteEditorCommand("scene.instantiate_prefab", ExternalDropCommandArgument(payload, parentId, isGuid),
                             "drag_drop");
    } else if (dropType == "MODEL_GUID" || dropType == "MODEL_FILE") {
        const bool isGuid = (dropType == "MODEL_GUID");
        ExecuteEditorCommand("scene.create_model", ExternalDropCommandArgument(payload, parentId, isGuid), "drag_drop");
    }
}

// ════════════════════════════════════════════════════════════════════
// Rename
// ════════════════════════════════════════════════════════════════════

void HierarchyPanel::BeginRename(uint64_t objId)
{
    Scene *scene = SceneManager::Instance().GetActiveScene();
    if (!scene)
        return;
    auto *obj = SceneManager::Instance().FindRuntimeObjectByID(objId);
    if (!obj)
        return;
    m_renameId = objId;
    std::strncpy(m_renameBuf, obj->GetName().c_str(), sizeof(m_renameBuf) - 1);
    m_renameBuf[sizeof(m_renameBuf) - 1] = '\0';
    m_renameFocus = true;
    m_renameSkipDeactivateFrames = 2;
    BeginTransientInteraction("rename", "inline_rename", 100, [this]() {
        CancelRename();
        return true;
    });
}

void HierarchyPanel::BeginRenameObject(uint64_t objId)
{
    BeginRename(objId);
}

void HierarchyPanel::CommitRename()
{
    if (!m_renameId)
        return;
    std::string newName(m_renameBuf);
    // Trim
    while (!newName.empty() && newName.front() == ' ')
        newName.erase(newName.begin());
    while (!newName.empty() && newName.back() == ' ')
        newName.pop_back();
    if (!newName.empty()) {
        Scene *scene = SceneManager::Instance().GetActiveScene();
        if (scene) {
            auto *obj = SceneManager::Instance().FindRuntimeObjectByID(m_renameId);
            if (obj && obj->GetName() != newName) {
                ExecuteEditorCommand("scene.rename_object", RenameCommandArgument(m_renameId, newName), "inline_edit");
            }
        }
    }
    m_renameId = 0;
    m_renameBuf[0] = '\0';
    m_renameFocus = false;
    m_renameSkipDeactivateFrames = 0;
    EndTransientInteraction("rename");
}

void HierarchyPanel::CancelRename()
{
    m_renameId = 0;
    m_renameBuf[0] = '\0';
    m_renameFocus = false;
    m_renameSkipDeactivateFrames = 0;
    EndTransientInteraction("rename");
}

// ════════════════════════════════════════════════════════════════════
// Reorder separator helper
// ════════════════════════════════════════════════════════════════════

void HierarchyPanel::RenderReorderSep(InxGUIContext *ctx, const char *sepId, std::function<void(uint64_t)> onDrop,
                                      float indentPx, bool consumeSpace)
{
    if (ImGui::GetDragDropPayload() == nullptr)
        return;

    float savedY = ctx->GetCursorPosY();
    float savedX = ctx->GetCursorPosX();
    float availW = ctx->GetContentRegionAvailWidth();
    if (indentPx > 0.0f) {
        ctx->SetCursorPosX(savedX + indentPx);
        availW = (std::max)(1.0f, availW - indentPx);
    }
    ctx->SetNextItemAllowOverlap();
    const float dpi = ctx->GetDpiScale();
    ctx->InvisibleButton(sepId, availW, EditorTheme::DND_REORDER_SEPARATOR_H * dpi);
    ctx->PushStyleColor(ImGuiCol_DragDropTarget, 0.0f, 0.0f, 0.0f, 0.0f);
    if (ctx->BeginDragDropTarget()) {
        // Draw separator line at midpoint
        float minY = ctx->GetItemRectMinY();
        float maxY = ctx->GetItemRectMaxY();
        float midY = (minY + maxY) * 0.5f;
        float x1 = ctx->GetItemRectMinX();
        float x2 = x1 + availW;
        ctx->DrawLine(x1, midY, x2, midY, EditorTheme::DND_REORDER_LINE.x, EditorTheme::DND_REORDER_LINE.y,
                      EditorTheme::DND_REORDER_LINE.z, EditorTheme::DND_REORDER_LINE.w,
                      EditorTheme::DND_REORDER_LINE_THICKNESS * dpi);
        uint64_t payload = 0;
        if (ctx->AcceptDragDropPayload(DRAG_DROP_TYPE, &payload)) {
            if (onDrop)
                onDrop(payload);
        }
        ctx->EndDragDropTarget();
    }
    ctx->PopStyleColor(1);
    ctx->SetCursorPosX(savedX);
    if (!consumeSpace)
        ctx->SetCursorPosY(savedY);
}

// ════════════════════════════════════════════════════════════════════
// Multi-drop target helper
// ════════════════════════════════════════════════════════════════════

void HierarchyPanel::RenderMultiDropTarget(InxGUIContext *ctx, uint64_t parentId)
{
    if (ImGui::GetDragDropPayload() == nullptr)
        return;

    ctx->PushStyleColor(ImGuiCol_DragDropTarget, 0.0f, 0.0f, 0.0f, 0.0f);
    if (ctx->BeginDragDropTarget()) {
        ctx->DrawRect(ctx->GetItemRectMinX(), ctx->GetItemRectMinY(), ctx->GetItemRectMaxX(), ctx->GetItemRectMaxY(),
                      EditorTheme::DND_PARENT_OUTLINE.x, EditorTheme::DND_PARENT_OUTLINE.y,
                      EditorTheme::DND_PARENT_OUTLINE.z, EditorTheme::DND_PARENT_OUTLINE.w,
                      EditorTheme::DND_PARENT_OUTLINE_THICKNESS * ctx->GetDpiScale());
        // Accept HIERARCHY_GAMEOBJECT (uint64_t payload)
        uint64_t payload = 0;
        if (ctx->AcceptDragDropPayload(DRAG_DROP_TYPE, &payload)) {
            HandleExternalDrop(DRAG_DROP_TYPE, payload, parentId);
        }
        // Accept string payloads
        for (const char *dt : {"MODEL_GUID", "MODEL_FILE", "PREFAB_GUID", "PREFAB_FILE"}) {
            std::string strPayload;
            if (ctx->AcceptDragDropPayload(dt, &strPayload)) {
                HandleExternalDropStr(dt, strPayload, parentId);
                break;
            }
        }
        ctx->EndDragDropTarget();
    }
    ctx->PopStyleColor(1);
}

// ════════════════════════════════════════════════════════════════════
// Context menus
// ════════════════════════════════════════════════════════════════════

void HierarchyPanel::RenderItemContextMenu(InxGUIContext *ctx, GameObject *obj)
{
    if (!obj || !renderContextMenu)
        return;
    renderContextMenu(ctx, obj->GetID(), obj->IsPrefabInstance(), obj->GetID());
}

bool HierarchyPanel::ExecuteEditorCommand(const std::string &commandId, const std::string &argument,
                                          const std::string &source) const
{
    return executeCommand && executeCommand(commandId, source, argument);
}

// ════════════════════════════════════════════════════════════════════
// Inline rename rendering
// ════════════════════════════════════════════════════════════════════

void HierarchyPanel::RenderRenameInput(InxGUIContext *ctx, GameObject *obj)
{
    if (m_renameFocus) {
        ctx->SetKeyboardFocusHere();
        m_renameFocus = false;
    }

    float availW = ctx->GetContentRegionAvailWidth();
    ctx->SetNextItemWidth(availW);
    const bool submitted =
        ctx->InputTextWithHint("##rename", "", m_renameBuf, sizeof(m_renameBuf), ImGuiInputTextFlags_EnterReturnsTrue);
    ctx->RecordSemanticItem("hierarchy_rename", obj->GetName(), true,
                            "hierarchy.object." + std::to_string(obj->GetID()) + ".rename", std::nullopt, std::nullopt,
                            std::string(m_renameBuf));

    if (m_renameSkipDeactivateFrames > 0)
        --m_renameSkipDeactivateFrames;

    if (submitted) {
        CommitRename();
        return;
    }
    if (m_renameSkipDeactivateFrames == 0 && ctx->IsItemDeactivated())
        CommitRename();
}

// ════════════════════════════════════════════════════════════════════
// Flat item rendering (replaces recursive RenderGameObjectTree for
// the main scrollable body; the old recursive function is kept for
// reference but no longer called from OnRenderContent).
// ════════════════════════════════════════════════════════════════════

void HierarchyPanel::RenderFlatItem(InxGUIContext *ctx, const FlatItem &item, float baseIndentX, float indentStep)
{
    GameObject *obj = item.obj;
    if (!obj)
        return;

    uint64_t objId = obj->GetID();
    ctx->PushID(static_cast<int>(objId & 0x7FFFFFFF));

    // ── Inline rename mode ──────────────────────────────────────
    if (m_renameId == objId) {
        float indentPx = static_cast<float>(item.depth) * indentStep;
        if (indentPx > 0)
            ImGui::Indent(indentPx);
        RenderRenameInput(ctx, obj);
        if (indentPx > 0)
            ImGui::Unindent(indentPx);
        ctx->PopID();
        return;
    }

    // Tree node flags — always use NoTreePushOnOpen so no TreePop needed
    int nodeFlags = ImGuiTreeNodeFlags_OpenOnArrow | ImGuiTreeNodeFlags_SpanAvailWidth |
                    ImGuiTreeNodeFlags_FramePadding | ImGuiTreeNodeFlags_NoTreePushOnOpen;

    if (m_selIds.count(objId))
        nodeFlags |= ImGuiTreeNodeFlags_Selected;

    bool isLeaf = !item.hasVisibleChildren;
    if (isLeaf)
        nodeFlags |= ImGuiTreeNodeFlags_Leaf;

    // Keep our stable object-ID set authoritative. ImGui normally owns tree
    // state by widget ID; rows disappearing during reparenting or changing
    // their visible labels during rename must not reset that state.
    const bool requestedOpen = HasActiveSearch() || m_treeProjection.IsExpanded(objId);
    ctx->SetNextItemOpen(requestedOpen, ImGuiCond_Always);
    m_forceExpandIds.erase(objId);

    // Display name with prefab decoration
    bool isPrefab = obj->IsPrefabInstance();
    const std::string &objectName = obj->GetName();
    const std::string *displayName = &objectName;
    std::string prefabDisplayName;
    if (isPrefab) {
        prefabDisplayName.reserve(objectName.size() + sizeof(EditorTheme::PREFAB_ICON) + 1);
        prefabDisplayName = EditorTheme::PREFAB_ICON;
        prefabDisplayName += " ";
        prefabDisplayName += objectName;
        displayName = &prefabDisplayName;
    }

    const bool inactiveDimmed = !obj->IsActiveInHierarchy();
    int textColorPushed = 0;
    if (inactiveDimmed) {
        ctx->PushStyleColor(ImGuiCol_Text, EditorTheme::TEXT_DISABLED.x, EditorTheme::TEXT_DISABLED.y,
                            EditorTheme::TEXT_DISABLED.z, EditorTheme::TEXT_DISABLED.w);
        textColorPushed = 1;
    } else if (isPrefab) {
        ctx->PushStyleColor(ImGuiCol_Text, EditorTheme::PREFAB_TEXT.x, EditorTheme::PREFAB_TEXT.y,
                            EditorTheme::PREFAB_TEXT.z, EditorTheme::PREFAB_TEXT.w);
        textColorPushed = 1;
    }

    // Manual indentation for flat rendering
    float indentPx = static_cast<float>(item.depth) * indentStep;
    if (indentPx > 0)
        ImGui::Indent(indentPx);

    std::string stableLabel;
    stableLabel.reserve(displayName->size() + 40);
    stableLabel = *displayName;
    stableLabel += "###HierarchyObject_";
    stableLabel += std::to_string(objId);
    bool isOpen = ctx->TreeNodeEx(stableLabel, nodeFlags);
    if (InxGUISemantics::IsCaptureEnabled())
        ctx->RecordSemanticItem("hierarchy_object", objectName, true, "hierarchy.object." + std::to_string(objId),
                                m_selIds.count(objId) > 0);

    if (indentPx > 0)
        ImGui::Unindent(indentPx);

    if (textColorPushed)
        ctx->PopStyleColor(1);

    // Search forces matching branches open only for presentation. Preserve the
    // user's pre-search expansion choices until normal tree interaction resumes.
    const bool toggledOpen = !isLeaf && ImGui::IsItemToggledOpen();
    if (!HasActiveSearch() && toggledOpen)
        ExecuteEditorCommand("hierarchy.set_expanded", TreeExpandedCommandArgument(objId, isOpen), "pointer");

    // ── Selection ───────────────────────────────────────────────
    if (ctx->IsItemClicked(0)) {
        if (m_renameId && m_renameId != objId)
            CancelRename();
        m_pendingSelectId = objId;
        m_pendingCtrl = IsCtrl(ctx);
        m_pendingShift = IsShift(ctx);
    }
    if (ctx->IsItemClicked(1)) {
        if (!m_selIds.count(objId)) {
            if (selectId)
                selectId(objId);
            SyncSelectionCache();
        }
        m_rightClickedObjId = objId;
        // The shared popup is rendered after the flat rows, outside this
        // object's ID scope. Open it in that same scope so ImGui hashes the
        // popup ID consistently.
        ctx->PopID();
        ctx->OpenPopup("##HierarchyItemContext");
        ctx->PushID(std::to_string(objId));
    }

    // Double-click frame selected through the shared editor command path.
    // A rapid expand/collapse gesture on the disclosure arrow is not a row
    // activation. Keep it from leaking into the double-click frame command.
    if (!toggledOpen && ctx->IsMouseDoubleClicked(0) && ctx->IsItemHovered()) {
        ExecuteEditorCommand("scene.frame_selected", std::to_string(objId), "pointer");
    }

    // ── Drag source ─────────────────────────────────────────────
    if (ctx->BeginDragDropSource(0)) {
        ctx->SetDragDropPayload(DRAG_DROP_TYPE, objId);
        int n = m_selIds.count(objId) ? m_selCount : 1;
        if (n > 1)
            ctx->Label(obj->GetName() + " (+" + std::to_string(n - 1) + ")");
        else
            ctx->Label(obj->GetName());
        ctx->EndDragDropSource();
    }

    // ── Drop target on body → reparent as child ─────────────────
    RenderMultiDropTarget(ctx, objId);

    ctx->PopID();
}

// ════════════════════════════════════════════════════════════════════
// VisiblePreRender — keyboard shortcuts + deferred selection
// ════════════════════════════════════════════════════════════════════

void HierarchyPanel::VisiblePreRender(InxGUIContext *ctx)
{
    using Clock = std::chrono::high_resolution_clock;
    auto msSince = [](const Clock::time_point &start) {
        return std::chrono::duration<double, std::milli>(Clock::now() - start).count();
    };

    // Refresh hidden IDs
    auto preHiddenStart = Clock::now();
    if (!m_runtimeHiddenPushMode && getRuntimeHiddenIds)
        m_hiddenIds = getRuntimeHiddenIds();
    else if (!m_runtimeHiddenPushMode)
        m_hiddenIds.clear();
    m_subPreHidden += msSince(preHiddenStart);

    // Sync selection once per frame
    auto preSelectionStart = Clock::now();
    SyncSelectionCache();
    if (m_selPrimary != m_lastObservedPrimaryId) {
        m_lastObservedPrimaryId = m_selPrimary;
        m_scrollToObjectId = m_selPrimary;
        if (m_selPrimary) {
            ExpandToObject(m_selPrimary);
            const bool selectionMissingFromCache =
                std::none_of(m_flatItems.begin(), m_flatItems.end(),
                             [this](const FlatItem &item) { return item.obj && item.obj->GetID() == m_selPrimary; });
            m_forceRootRefresh = selectionMissingFromCache;
        }
    }
    m_subPreSelection += msSince(preSelectionStart);

    // Deferred left-click selection
    auto pendingStart = Clock::now();
    if (m_pendingSelectId != 0) {
        if (!ctx->IsMouseButtonDown(0)) {
            if (!ctx->IsMouseDragging(0)) {
                uint64_t pid = m_pendingSelectId;

                if (m_pendingCtrl) {
                    if (toggleId)
                        toggleId(pid);
                } else if (m_pendingShift) {
                    Scene *scene = SceneManager::Instance().GetActiveScene();
                    if (scene) {
                        if (m_orderedIdsDirty) {
                            m_cachedOrderedIds = CollectOrderedIds(m_cachedRoots);
                            m_orderedIdsDirty = false;
                        }
                        auto searchFiltered =
                            HasActiveSearch() ? CollectOrderedIds(FilterForSearch(m_cachedRoots)) : m_cachedOrderedIds;
                        if (setOrderedIds)
                            setOrderedIds(searchFiltered);
                    }
                    if (rangeSelectId)
                        rangeSelectId(pid);
                } else {
                    if (selectId)
                        selectId(pid);
                }
                SyncSelectionCache();
            }
            m_pendingSelectId = 0;
            m_pendingCtrl = false;
            m_pendingShift = false;
        } else if (ctx->IsMouseDragging(0)) {
            m_pendingSelectId = 0;
            m_pendingCtrl = false;
            m_pendingShift = false;
        }
    }
    m_subPrePendingSelect += msSince(pendingStart);
}

// ════════════════════════════════════════════════════════════════════
// OnRenderContent — the main hierarchy body
// ════════════════════════════════════════════════════════════════════

void HierarchyPanel::OnRenderContent(InxGUIContext *ctx)
{
    using Clock = std::chrono::high_resolution_clock;
    auto msSince = [](const Clock::time_point &start) {
        return std::chrono::duration<double, std::milli>(Clock::now() - start).count();
    };
    const float dpi = ctx->GetDpiScale();
    if (std::abs(dpi - m_lastDpiScale) >= 0.01f) {
        m_lastDpiScale = dpi;
        m_cachedItemHeight = 18.0f * dpi;
        m_itemHeightMeasured = false;
    }

    // ── Header: scene name / prefab mode ────────────────────────
    auto headerStart = Clock::now();
    if (IsPrefabModeActive()) {
        std::string prefabName = PrefabDisplayName();
        ctx->PushStyleColor(ImGuiCol_Text, EditorTheme::PREFAB_TEXT.x, EditorTheme::PREFAB_TEXT.y,
                            EditorTheme::PREFAB_TEXT.z, EditorTheme::PREFAB_TEXT.w);
        ctx->Label(prefabName);
        ctx->PopStyleColor(1);
    } else {
        std::string displayName = SceneDisplayName();
        if (!displayName.empty())
            ctx->Label(displayName);
        else {
            Scene *scene = SceneManager::Instance().GetActiveScene();
            ctx->Label(scene ? scene->GetName() : Tr("hierarchy.no_scene"));
        }
    }
    m_subHeader += msSince(headerStart);

    // ── Search bar ──────────────────────────────────────────────
    auto searchStart = Clock::now();
    ctx->SetNextItemWidth(ctx->GetContentRegionAvailWidth());
    if (m_focusSearchNextFrame) {
        ctx->SetKeyboardFocusHere();
        m_focusSearchNextFrame = false;
    }
    std::strncpy(m_searchBuf, m_search.Query().c_str(), sizeof(m_searchBuf) - 1);
    m_searchBuf[sizeof(m_searchBuf) - 1] = '\0';
    ctx->InputTextWithHint("##HierarchySearch", Tr("hierarchy.search_placeholder").c_str(), m_searchBuf,
                           sizeof(m_searchBuf), 0);
    if (InxGUISemantics::IsCaptureEnabled())
        ctx->RecordSemanticItem("hierarchy_search", Tr("hierarchy.search_placeholder"), true, "hierarchy.search");
    SetSearchQuery(m_searchBuf);

    ctx->Separator();
    m_subSearch += msSince(searchStart);

    // ── Scene tree ──────────────────────────────────────────────
    Scene *scene = SceneManager::Instance().GetActiveScene();
    if (scene) {
        ctx->PushStyleVarVec2(ImGuiStyleVar_ItemSpacing, EditorTheme::TREE_ITEM_SPC.x * dpi,
                              EditorTheme::TREE_ITEM_SPC.y * dpi);
        ctx->PushStyleVarVec2(ImGuiStyleVar_FramePadding, EditorTheme::TREE_FRAME_PAD.x * dpi,
                              EditorTheme::TREE_FRAME_PAD.y * dpi);
        ctx->PushStyleVarFloat(ImGuiStyleVar_IndentSpacing, EditorTheme::TREE_INDENT * dpi);

        bool allowStale =
            !m_forceRootRefresh && !ctx->IsWindowFocused(0) && !ctx->IsWindowHovered() && !m_cachedRoots.empty();
        {
            auto t0 = Clock::now();
            RefreshRootObjects(scene, allowStale, m_forceRootRefresh);
            m_forceRootRefresh = false;
            m_subRefreshRoots += msSince(t0);
        }

        // A root can be transferred to the runtime-persistent scene between
        // hierarchy refreshes. Never render that stale active-scene cache
        // entry alongside the persistent group in the same frame.
        const auto ownedRootsEnd =
            std::remove_if(m_cachedRoots.begin(), m_cachedRoots.end(),
                           [scene](GameObject *root) { return root == nullptr || root->GetScene() != scene; });
        if (ownedRootsEnd != m_cachedRoots.end()) {
            m_cachedRoots.erase(ownedRootsEnd, m_cachedRoots.end());
            m_flatListDirty = true;
        }

        // Apply the pending expansion requested by the latest hierarchy action.
        if (m_pendingExpandId) {
            m_treeProjection.SetExpanded(m_pendingExpandId, true);
            m_forceExpandIds.insert(m_pendingExpandId);
            m_pendingExpandId = 0;
            m_flatListDirty = true;
        }

        // Use cachedRoots directly when no search is active to avoid O(n) copy
        const std::vector<GameObject *> *pVisibleRoots = &m_cachedRoots;
        std::vector<GameObject *> filteredRoots;
        if (HasActiveSearch()) {
            auto t0 = Clock::now();
            filteredRoots = FilterForSearch(m_cachedRoots);
            m_subFilterRoots += msSince(t0);
            pVisibleRoots = &filteredRoots;
        }
        const auto &visibleRoots = *pVisibleRoots;
        int nRoots = static_cast<int>(visibleRoots.size());

        // Build flat list of all visible items (roots + expanded children)
        // Only rebuild when structure, search, or expand state changes
        {
            auto t0 = Clock::now();
            RebuildFlatListIfNeeded(visibleRoots);
            m_subFlatBuild += msSince(t0);
        }

        // A live, explicitly selected root must never disappear from the
        // Hierarchy because a stale structure or runtime-hidden snapshot still
        // carries the same recycled object ID. This path is intentionally
        // exceptional and O(n); the normal cached list remains untouched.
        if (!HasActiveSearch() && m_selPrimary != 0) {
            const bool selectedVisible =
                std::any_of(m_flatItems.begin(), m_flatItems.end(),
                            [this](const FlatItem &item) { return item.obj && item.obj->GetID() == m_selPrimary; });
            if (!selectedVisible) {
                GameObject *selected = SceneManager::Instance().FindRuntimeObjectByID(m_selPrimary);
                if (selected && selected->GetScene() == scene && selected->GetParent() == nullptr) {
                    const bool hasVisibleChildren =
                        std::any_of(selected->GetChildren().begin(), selected->GetChildren().end(),
                                    [this](const auto &child) { return child && !IsHidden(child->GetID()); });
                    m_flatItems.push_back({selected, 0, hasVisibleChildren});
                }
            }
        }
        int nItems = static_cast<int>(m_flatItems.size());

        // Root-level insertion line before first root (only when dragging)
        bool hasDrag = (ImGui::GetDragDropPayload() != nullptr);
        if (hasDrag) {
            if (nRoots > 0) {
                uint64_t firstRootId = visibleRoots[0]->GetID();
                RenderReorderSep(ctx, "##sep_before_first_root", [this, firstRootId](uint64_t payload) {
                    MoveObjectAdjacent(payload, firstRootId, false);
                });
            } else {
                RenderReorderSep(ctx, "##sep_empty_root", [this](uint64_t payload) { ReparentToRoot(payload); });
            }
        }

        // ── Flat virtual scrolling ──────────────────────────────
        if (nItems > 0) {
            auto rowsStart = Clock::now();
            float availW = ctx->GetContentRegionAvailWidth();
            float scrollY = ctx->GetScrollY();
            float viewportH = ctx->GetContentRegionAvailHeight();
            if (viewportH <= 0)
                viewportH = 400.0f * dpi;
            float startY = ctx->GetCursorPosY();
            float itemH = m_cachedItemHeight;
            float indentStep = EditorTheme::TREE_INDENT * dpi;

            // Selection can be changed by creation services, undo/redo, scene
            // picking, or another panel. Ensure a newly selected row is not
            // discarded by virtual scrolling when it lies outside the current
            // viewport. Keep this one-shot so normal hierarchy scrolling is
            // never pulled back to an old selection.
            if (m_scrollToObjectId != 0) {
                auto selectedIt = std::find_if(m_flatItems.begin(), m_flatItems.end(), [this](const FlatItem &item) {
                    return item.obj && item.obj->GetID() == m_scrollToObjectId;
                });
                if (selectedIt != m_flatItems.end()) {
                    const int selectedIndex = static_cast<int>(std::distance(m_flatItems.begin(), selectedIt));
                    const float rowTop = startY + static_cast<float>(selectedIndex) * itemH;
                    const float rowBottom = rowTop + itemH;
                    if (rowTop < scrollY || rowBottom > scrollY + viewportH) {
                        const float targetY = (std::max)(0.0f, rowTop - (viewportH - itemH) * 0.5f);
                        ImGui::SetScrollY(targetY);
                        scrollY = targetY;
                    }
                    m_scrollToObjectId = 0;
                } else if (!HasActiveSearch()) {
                    // The object no longer exists or is hidden by a collapsed
                    // branch that could not be expanded.
                    m_scrollToObjectId = 0;
                }
            }

            // ImGui may preserve the previous scroll offset for one frame after
            // a hierarchy shrinks. Clamp both ends to the current flat list so
            // that the final object is still rendered instead of producing an
            // empty clipped range at the bottom.
            int firstVis = (std::max)(0, static_cast<int>((scrollY - startY) / itemH) - 2);
            firstVis = (std::min)(nItems - 1, firstVis);
            int lastVis = (std::min)(nItems - 1, static_cast<int>((scrollY + viewportH - startY) / itemH) + 5);
            lastVis = (std::max)(firstVis, lastVis);

            if (firstVis > 0)
                ctx->Dummy(availW, static_cast<float>(firstVis) * itemH);

            float baseIndentX = ctx->GetCursorPosX();
            for (int i = firstVis; i <= lastVis; i++) {
                float beforeY = ctx->GetCursorPosY();

                // Reorder separator before first child (only when dragging)
                if (hasDrag && i > 0 && m_flatItems[i].depth > m_flatItems[i - 1].depth) {
                    uint64_t childId = m_flatItems[i].obj->GetID();
                    std::string sepId = "##sep_fc_" + std::to_string(m_flatItems[i - 1].obj->GetID());
                    RenderReorderSep(ctx, sepId.c_str(), [this, childId](uint64_t payload) {
                        MoveObjectAdjacent(payload, childId, false);
                    });
                }

                RenderFlatItem(ctx, m_flatItems[i], baseIndentX, indentStep);

                // Reorder separator after each item (only when dragging)
                if (hasDrag) {
                    uint64_t afterObjId = m_flatItems[i].obj->GetID();
                    std::string sepAfterId = "##sep_a_" + std::to_string(afterObjId);
                    RenderReorderSep(
                        ctx, sepAfterId.c_str(),
                        [this, afterObjId](uint64_t payload) { MoveObjectAdjacent(payload, afterObjId, true); },
                        static_cast<float>(m_flatItems[i].depth) * indentStep);

                    const int nextDepth = (i + 1 < nItems) ? m_flatItems[i + 1].depth : 0;
                    if (m_flatItems[i].depth > nextDepth) {
                        for (int ancestorDepth = m_flatItems[i].depth - 1; ancestorDepth >= nextDepth;
                             --ancestorDepth) {
                            uint64_t ancestorId = 0;
                            for (int j = i - 1; j >= 0; --j) {
                                if (m_flatItems[j].depth == ancestorDepth) {
                                    ancestorId = m_flatItems[j].obj->GetID();
                                    break;
                                }
                            }
                            if (ancestorId == 0)
                                continue;
                            std::string outdentSepId =
                                "##sep_out_" + std::to_string(afterObjId) + "_" + std::to_string(ancestorDepth);
                            RenderReorderSep(
                                ctx, outdentSepId.c_str(),
                                [this, ancestorId](uint64_t payload) { MoveObjectAdjacent(payload, ancestorId, true); },
                                static_cast<float>(ancestorDepth) * indentStep, true);
                        }
                    }
                }

                float afterY = ctx->GetCursorPosY();
                float actualH = afterY - beforeY;
                if (!hasDrag && actualH > 1.0f && !m_itemHeightMeasured) {
                    m_cachedItemHeight = actualH;
                    itemH = actualH;
                    m_itemHeightMeasured = true;
                }
            }

            int remaining = nItems - lastVis - 1;
            if (remaining > 0)
                ctx->Dummy(availW, static_cast<float>(remaining) * itemH);
            m_subRows += msSince(rowsStart);
        }

        // Unity-style runtime residency is a real, separate Scene. Keep the
        // active authored Scene first, then present persistent roots in their
        // own group below it.
        Scene *persistentScene = SceneManager::Instance().GetRuntimePersistentScene();
        if (persistentScene && !persistentScene->GetRootObjects().empty()) {
            std::vector<GameObject *> persistentRoots = FilterHidden(persistentScene->GetRootObjects());
            if (HasActiveSearch())
                persistentRoots = FilterForSearch(persistentRoots);
            if (!persistentRoots.empty()) {
                ctx->Separator();
                ctx->PushStyleColor(ImGuiCol_Text, EditorTheme::TEXT_DISABLED.x, EditorTheme::TEXT_DISABLED.y,
                                    EditorTheme::TEXT_DISABLED.z, EditorTheme::TEXT_DISABLED.w);
                ctx->Label("DontDestroyOnLoad");
                ctx->PopStyleColor(1);

                std::vector<FlatItem> persistentItems;
                persistentItems.reserve(persistentRoots.size() * 2);
                for (GameObject *root : persistentRoots)
                    BuildFlatListRecurse(root, 0, persistentItems);
                const float baseIndentX = ctx->GetCursorPosX();
                for (const FlatItem &item : persistentItems)
                    RenderFlatItem(ctx, item, baseIndentX, EditorTheme::TREE_INDENT * dpi);
            }
        }

        auto popupStart = Clock::now();
        if (ctx->BeginPopup("##HierarchyItemContext")) {
            GameObject *popupObj = SceneManager::Instance().FindRuntimeObjectByID(m_rightClickedObjId);
            if (popupObj)
                RenderItemContextMenu(ctx, popupObj);
            else
                m_rightClickedObjId = 0;
            ctx->EndPopup();
        } else if (!ImGui::IsPopupOpen("##HierarchyItemContext")) {
            m_rightClickedObjId = 0;
        }
        m_subPopup += msSince(popupStart);

        // ── Tail drop zone ──────────────────────────────────────
        auto tailDropStart = Clock::now();
        bool tailContextMenuRequested = false;
        float remainingH = ctx->GetContentRegionAvailHeight();
        if (remainingH > 4.0f * dpi) {
            float tailW = ctx->GetContentRegionAvailWidth();
            ctx->InvisibleButton("##drop_to_root_tail", tailW, remainingH);
            if (InxGUISemantics::IsCaptureEnabled())
                ctx->RecordSemanticItem("hierarchy_background", "Hierarchy Background", true, "hierarchy.background");

            if (ctx->IsItemClicked(0)) {
                CancelRename();
                ClearSelectionAndNotify();
            }
            if (ctx->IsItemClicked(1))
                tailContextMenuRequested = true;

            // Drop target with top-edge line
            ctx->PushStyleColor(ImGuiCol_DragDropTarget, 0.0f, 0.0f, 0.0f, 0.0f);
            if (ctx->BeginDragDropTarget()) {
                float lineY = ctx->GetItemRectMinY();
                float lineX1 = ctx->GetItemRectMinX();
                float lineX2 = lineX1 + tailW;
                ctx->DrawLine(lineX1, lineY, lineX2, lineY, EditorTheme::DND_REORDER_LINE.x,
                              EditorTheme::DND_REORDER_LINE.y, EditorTheme::DND_REORDER_LINE.z,
                              EditorTheme::DND_REORDER_LINE.w, EditorTheme::DND_REORDER_LINE_THICKNESS * dpi);
                // Accept uint64_t payload
                uint64_t payload = 0;
                bool accepted = false;
                if (ctx->AcceptDragDropPayload(DRAG_DROP_TYPE, &payload)) {
                    HandleExternalDrop(DRAG_DROP_TYPE, payload, 0);
                    accepted = true;
                }
                if (!accepted) {
                    for (const char *dt : {"MODEL_GUID", "MODEL_FILE", "PREFAB_GUID", "PREFAB_FILE"}) {
                        std::string strPayload;
                        if (ctx->AcceptDragDropPayload(dt, &strPayload)) {
                            HandleExternalDropStr(dt, strPayload, 0);
                            break;
                        }
                    }
                }
                ctx->EndDragDropTarget();
            }
            ctx->PopStyleColor(1);
        }

        // The tail is an InvisibleButton so it can accept root-level drops.
        // BeginPopupContextWindow(..., noOpenOverItems=true) intentionally
        // ignores it; forward its right click to a dedicated background popup.
        if (tailContextMenuRequested && m_rightClickedObjId == 0)
            ctx->OpenPopup("##HierarchyBackgroundContext");

        // Fallback: deselect when clicking the scrollable background
        // (the tail InvisibleButton only works when remainingH > 4)
        if (ImGui::IsWindowHovered(ImGuiHoveredFlags_AllowWhenBlockedByActiveItem) &&
            ImGui::IsMouseClicked(ImGuiMouseButton_Left) && !ImGui::IsAnyItemHovered()) {
            CancelRename();
            ClearSelectionAndNotify();
        }
        m_subTailDrop += msSince(tailDropStart);

        ctx->PopStyleVar(3); // IndentSpacing + FramePadding + ItemSpacing

        if (HasActiveSearch() && nItems == 0)
            ctx->Label(Tr("hierarchy.no_search_results"));
    }

    // ── Parent for background-menu creations ─────────────────────
    // A blank-area context menu creates a root object. Creating a child is
    // deliberately reserved for an object's own context menu, otherwise a
    // selected object silently turns the next root creation into a collapsed
    // child and makes it appear to disappear from the Hierarchy.
    uint64_t parentIdForNew = 0;
    if (IsPrefabModeActive()) {
        Scene *pscene = SceneManager::Instance().GetActiveScene();
        if (pscene && !pscene->GetRootObjects().empty())
            parentIdForNew = pscene->GetRootObjects()[0]->GetID();
    }

    // ── Background context menu ─────────────────────────────────
    // An object row opens the shared item popup earlier in this frame. Do not
    // also let the window-level popup consume that same right click.
    bool backgroundContextOpen = false;
    if (m_rightClickedObjId == 0) {
        backgroundContextOpen = ctx->BeginPopup("##HierarchyBackgroundContext");
        if (!backgroundContextOpen)
            backgroundContextOpen = ctx->BeginPopupContextWindow("", 1, true);
    }
    if (backgroundContextOpen) {
        ctx->RecordSemanticWindow("context_menu", "Hierarchy Create", "hierarchy.context.root");
        if (renderContextMenu)
            renderContextMenu(ctx, 0, false, parentIdForNew);
        ctx->EndPopup();
    }
}

} // namespace infernux
