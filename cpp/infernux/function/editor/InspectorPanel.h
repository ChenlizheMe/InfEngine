#pragma once

#include <function/editor/EditorPanel.h>
#include <function/editor/EditorTheme.h>

#include <array>
#include <chrono>
#include <cstdint>
#include <functional>
#include <string>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace infernux
{

/// Metadata for a single component attached to a GameObject.
/// Provided by Python callback — avoids C++ iterating py_components.
struct ComponentInfo
{
    std::string typeName;
    std::string displayName;
    uint64_t componentId = 0;
    bool enabled = true;
    bool isNative = true;
    bool isScript = false; // Python script (show "(Script)" suffix)
    bool isBroken = false; // Script has load errors
    std::string brokenError;
    uint64_t iconId = 0; // Pre-resolved texture ID for component icon
};

/// Inspector display mode — mutually exclusive.
enum class InspectorMode
{
    Object,  // GameObject selected from Hierarchy
    Asset,   // Asset file selected from Project panel
    Preview, // Non-editable file preview
};

/// C++ native Inspector panel — Unity-style property/component editor.
///
/// Heavy rendering (component headers, Transform, tag/layer, splitter,
/// object header) happens entirely in C++.  Component body rendering
/// and Python-dependent operations (undo, script loading, serialize/
/// deserialize) are reached via std::function callbacks set from the
/// Python bootstrap layer.
class InspectorPanel : public EditorPanel
{
  public:
    InspectorPanel();

    // ── Public API (called from Python bootstrap / other panels) ─────

    void SetSelectedObjectId(uint64_t id);
    void ClearSelectedObject();
    [[nodiscard]] uint64_t GetSelectedObjectId() const
    {
        return m_selectedObjectId;
    }

    void SetSelectedFile(const std::string &filePath, const std::string &category);
    void ClearSelectedFile();
    [[nodiscard]] const std::string &GetSelectedFile() const
    {
        return m_selectedFile;
    }

    void SetDetailFile(const std::string &filePath, const std::string &category);

    /// Project the authoritative component selection into the native view.
    /// Component IDs are scene-unique and are only used for header styling.
    void SetSelectedComponentIds(const std::vector<uint64_t> &componentIds);
    void ClearSelectedComponents();

    // ── Selection callbacks (project typed SelectionService) ─────────

    std::function<bool()> isMultiSelection;
    std::function<std::vector<uint64_t>()> getSelectedIds;
    std::function<uint64_t()> getValueGeneration;

    struct RevisionSnapshot
    {
        uint64_t target = 0;
        uint64_t schema = 0;
        uint64_t value = 0;
        uint64_t preview = 0;
    };
    /// One immutable revision packet is captured at the start of a visible
    /// Inspector frame. The fallback generation remains for older hosts.
    std::function<RevisionSnapshot()> getRevisionSnapshot;

    // ── Object info callbacks ────────────────────────────────────────

    /// Returns (name, active, tag, layer, prefabGuid, hideTransform)
    struct ObjectInfo
    {
        std::string name;
        bool active = true;
        std::string tag;
        int layer = 0;
        std::string prefabGuid;
        bool hideTransform = false;
        uint64_t transformComponentId = 0;
    };
    std::function<ObjectInfo(uint64_t)> getObjectInfo;

    // ── Transform callbacks ──────────────────────────────────────────

    struct TransformData
    {
        float px = 0, py = 0, pz = 0; // local position
        float rx = 0, ry = 0, rz = 0; // local euler
        float sx = 1, sy = 1, sz = 1; // local scale
    };
    std::function<TransformData(uint64_t)> getTransformData;

    // ── Component enumeration callbacks ──────────────────────────────

    /// Get list of all component infos for a single object.
    std::function<std::vector<ComponentInfo>(uint64_t)> getComponentList;
    /// Get icon texture ID for a component type.
    std::function<uint64_t(const std::string &, bool)> getComponentIconId;

    // ── Component body rendering callbacks (Python → ImGui) ─────────

    /// Render a component body (everything under the header).
    /// Parameters: (ctx, objId, typeName, componentId, isNative)
    std::function<void(InxGUIContext *, uint64_t, const std::string &, uint64_t, bool)> renderComponentBody;

    /// Render a common component body for multi-selection.
    /// Parameters: (ctx, objIds, typeName, componentIds, isNative)
    std::function<void(InxGUIContext *, const std::vector<uint64_t> &, const std::string &,
                       const std::vector<uint64_t> &, bool)>
        renderMultiComponentBody;

    /// Return and reset Python-side component body profile metrics.
    std::function<std::unordered_map<std::string, double>()> consumeComponentBodyProfile;

    /// Publish a real component-header selection gesture. The two ID arrays
    /// are parallel and contain one entry per selected object/component.
    std::function<void(const std::vector<uint64_t> &, const std::vector<uint64_t> &, bool)> onComponentSelectionChanged;

    /// Render a component right-click context menu.
    /// Returns true if an action consumed the frame (caller should bail).
    std::function<bool(InxGUIContext *, uint64_t, const std::string &, uint64_t, bool)> renderComponentContextMenu;

    // ── Add Component callbacks ──────────────────────────────────────

    struct AddComponentEntry
    {
        std::string displayName;
        std::string category;
        bool isNative = true;
        std::string scriptPath; // only for scripts
    };
    std::function<std::vector<AddComponentEntry>()> getAddComponentEntries;

    // ── Asset / File preview callbacks ───────────────────────────────

    /// Render the asset inspector for the given file/category.
    std::function<void(InxGUIContext *, const std::string &, const std::string &)> renderAssetInspector;
    /// Render generic file preview.
    std::function<void(InxGUIContext *, const std::string &)> renderFilePreview;

    // ── Material sections callback ───────────────────────────────────

    /// Render material override sections for an object.
    std::function<void(InxGUIContext *, uint64_t)> renderMaterialSections;

    // ── Prefab callbacks ─────────────────────────────────────────────

    struct PrefabInfo
    {
        int overrideCount = 0;
        bool isReadonly = false;
        bool isTransformReadonly = false;
    };
    std::function<PrefabInfo(uint64_t)> getPrefabInfo;

    // ── Tag & Layer info ─────────────────────────────────────────────

    std::function<std::vector<std::string>()> getAllTags;
    std::function<std::vector<std::string>()> getAllLayers;

    // ── Translation ──────────────────────────────────────────────────

    std::function<std::string(const std::string &)> translate;

    // ── Script drop on PropertiesModule ──────────────────────────────

    std::unordered_map<std::string, double> ConsumeSubTimings() override;

  protected:
    void OnRenderContent(InxGUIContext *ctx) override;
    void VisiblePreRender(InxGUIContext *ctx) override;

  private:
    // ── Translation cache ────────────────────────────────────────────
    const std::string &Tr(const std::string &key);
    std::unordered_map<std::string, std::string> m_trCache;

    // ── Mode state ───────────────────────────────────────────────────
    InspectorMode m_mode = InspectorMode::Object;
    uint64_t m_selectedObjectId = 0;
    std::string m_selectedFile;
    std::string m_assetCategory;
    std::unordered_set<uint64_t> m_selectedComponentIds;

    // ── Splitter state ───────────────────────────────────────────────
    float m_propertiesRatio = EditorTheme::INSPECTOR_DEFAULT_RATIO;

    // ── Tag/Layer cache ──────────────────────────────────────────────
    float m_tagLayerCacheTime = 0.0f;
    std::vector<std::string> m_cachedTags;
    std::vector<std::string> m_cachedLayers;
    std::vector<std::string> m_cachedTagItems;   // tags + "Add Tag..."
    std::vector<std::string> m_cachedLayerItems; // "0: Default" + "Add Layer..."
    static constexpr float TAG_LAYER_CACHE_TTL = 0.25f;

    // ── Add Component popup state ────────────────────────────────────
    char m_addCompSearch[256] = {};
    std::vector<AddComponentEntry> m_addCompEntries;
    bool m_addCompNeedsFocus = false;
    bool m_addCompPopupOpen = false;
    bool m_addCompCloseRequested = false;

    // ── Timing ───────────────────────────────────────────────────────
    float m_frameTimeNow = 0.0f;
    // ── Split sub-timings (accumulated ms, consumed by profile) ───
    double m_subGetInfo = 0.0;
    double m_subTransform = 0.0;
    double m_subGetComponents = 0.0;
    double m_subComponentBodies = 0.0;
    double m_subMaterials = 0.0;
    // ── Cached object info ───────────────────────────────────────────
    uint64_t m_cachedObjInfoId = 0;
    ObjectInfo m_cachedObjInfo;
    PrefabInfo m_cachedPrefabInfo;
    uint64_t m_cachedComponentListObjId = 0;
    std::vector<ComponentInfo> m_cachedComponents;
    std::unordered_map<uint64_t, float> m_cachedComponentBodyHeights;
    RevisionSnapshot m_frameRevisions;
    bool m_revisionSnapshotErrorReported = false;
    uint64_t m_cachedObjectTargetRevision = 0;
    uint64_t m_cachedObjectValueRevision = 0;
    uint64_t m_cachedComponentTargetRevision = 0;
    uint64_t m_cachedComponentSchemaRevision = 0;
    uint64_t m_cachedTransformObjId = 0;
    uint64_t m_cachedTransformValueRevision = 0;
    TransformData m_cachedTransformData;
    std::array<std::string, 3> m_transformGestureIds;
    uint64_t m_transformGestureSerial = 0;

    struct CommonComponent
    {
        ComponentInfo display;
        std::vector<uint64_t> componentIds;
    };

    struct MultiTransformSnapshot
    {
        TransformData first;
        std::array<bool, 9> mixed{};
        std::vector<uint64_t> componentIds;
    };

    bool m_cachedMultiComponentsValid = false;
    std::vector<uint64_t> m_cachedMultiComponentIds;
    std::vector<CommonComponent> m_cachedMultiCommonComponents;
    uint64_t m_cachedMultiComponentTargetRevision = 0;
    uint64_t m_cachedMultiComponentSchemaRevision = 0;

    bool m_cachedMultiTransformValid = false;
    std::vector<uint64_t> m_cachedMultiTransformIds;
    MultiTransformSnapshot m_cachedMultiTransformSnapshot;
    uint64_t m_cachedMultiTransformTargetRevision = 0;
    uint64_t m_cachedMultiTransformValueRevision = 0;

    // ── Cached icon IDs ──────────────────────────────────────────────
    uint64_t m_cachedTransformIconId = 0;
    float m_cachedTransformBodyHeight = 0.0f;

    // ── Render helpers ───────────────────────────────────────────────
    void RenderPropertiesModule(InxGUIContext *ctx, float height);
    void RenderRawDataModule(InxGUIContext *ctx, float height);
    float RenderSplitter(InxGUIContext *ctx, float totalHeight);

    void RenderSingleObject(InxGUIContext *ctx, uint64_t objId);
    void RenderMultiEdit(InxGUIContext *ctx, const std::vector<uint64_t> &ids);
    void InvalidateObjectCaches();
    const std::vector<CommonComponent> &GetCommonComponentsForMultiSelection(const std::vector<uint64_t> &ids);
    const MultiTransformSnapshot &GetMultiTransformSnapshot(const std::vector<uint64_t> &ids);

    void RenderObjectHeader(InxGUIContext *ctx, uint64_t objId, const ObjectInfo &info);
    void RenderTagLayerRow(InxGUIContext *ctx, uint64_t objId, const ObjectInfo &info);
    void RenderTransform(InxGUIContext *ctx, uint64_t objId);
    void RenderMultiTransform(InxGUIContext *ctx, const std::vector<uint64_t> &ids);
    std::string UpdateTransformGesture(size_t rowIndex, uint32_t lifecycleFlags);
    void FinishTransformGesture(size_t rowIndex, uint32_t lifecycleFlags);
    void RenderPrefabHeader(InxGUIContext *ctx, uint64_t objId, const PrefabInfo &pinfo);

    struct ComponentHeaderResult
    {
        bool open = false;
        bool enabled = true;
        bool selectionRequested = false;
    };

    [[nodiscard]] bool IsComponentSelected(uint64_t componentId) const;
    [[nodiscard]] bool AreComponentsSelected(const std::vector<uint64_t> &componentIds) const;

    /// Render one component header (icon + enabled + collapsing).
    ComponentHeaderResult
    RenderComponentHeader(InxGUIContext *ctx, const std::string &typeName, const std::string &headerId, uint64_t iconId,
                          bool showEnabled, bool isEnabled, const std::string &suffix = "", bool defaultOpen = true,
                          const std::string &semanticId = "", const std::string &contextPopupId = "",
                          bool selected = false, const std::vector<uint64_t> &dragObjectIds = {},
                          const std::vector<uint64_t> &dragComponentIds = {}, bool allowComponentReorder = true);

    bool RenderInspectorCheckbox(InxGUIContext *ctx, const char *label, bool value);

    void RenderAddComponentButton(InxGUIContext *ctx);
    void RenderAddComponentPopup(InxGUIContext *ctx);

    void RefreshTagLayerCache();
    bool ExecuteEditorCommand(const std::string &commandId, const std::string &argument = "",
                              const std::string &source = "context_menu") const;
    bool CanExecuteEditorCommand(const std::string &commandId, const std::string &argument = "") const;
};

} // namespace infernux
