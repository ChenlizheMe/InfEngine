#include <function/editor/InspectorPanel.h>
#include <function/renderer/ProfileConfig.h>
#include <function/renderer/gui/InxGUISemantics.h>

#include <imgui.h>

#include <algorithm>
#include <array>
#include <cctype>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <unordered_map>

namespace infernux
{

// ── Drag speeds (match Python inspector_utils constants) ─────────
static constexpr float DRAG_SPEED_DEFAULT = 0.1f;
static constexpr float DRAG_SPEED_FINE = 0.01f;

// ============================================================================
// Construction
// ============================================================================

InspectorPanel::InspectorPanel() : EditorPanel("Inspector", "inspector")
{
}

// ============================================================================
// Sub-timing snapshot (consumed + reset by profile system)
// ============================================================================

std::unordered_map<std::string, double> InspectorPanel::ConsumeSubTimings()
{
    std::unordered_map<std::string, double> out;
    out["info"] = m_subGetInfo;
    out["transform"] = m_subTransform;
    out["compList"] = m_subGetComponents;
    out["compBody"] = m_subComponentBodies;
    out["material"] = m_subMaterials;
    if (consumeComponentBodyProfile) {
        auto bodyMetrics = consumeComponentBodyProfile();
        for (const auto &kv : bodyMetrics)
            out[kv.first] += kv.second;
    }
    m_subGetInfo = 0.0;
    m_subTransform = 0.0;
    m_subGetComponents = 0.0;
    m_subComponentBodies = 0.0;
    m_subMaterials = 0.0;
    return out;
}

// ============================================================================
// Translation cache
// ============================================================================

const std::string &InspectorPanel::Tr(const std::string &key)
{
    auto it = m_trCache.find(key);
    if (it != m_trCache.end())
        return it->second;
    if (translate) {
        auto &ref = m_trCache[key];
        ref = translate(key);
        return ref;
    }
    return m_trCache[key] = key;
}

void InspectorPanel::InvalidateObjectCaches()
{
    m_cachedObjInfoId = 0;
    m_cachedComponentListObjId = 0;
    m_cachedComponents.clear();
    m_cachedValueGeneration = 0;
    m_cachedValueRefreshTime = 0.0f;

    m_cachedMultiComponentsValid = false;
    m_cachedMultiComponentIds.clear();
    m_cachedMultiCommonComponents.clear();
    m_cachedMultiComponentValueGeneration = 0;
    m_cachedMultiComponentRefreshTime = 0.0f;

    m_cachedMultiTransformValid = false;
    m_cachedMultiTransformIds.clear();
    m_cachedMultiTransformSnapshot = {};
    m_cachedMultiTransformValueGeneration = 0;
    m_cachedMultiTransformRefreshTime = 0.0f;
}

// ============================================================================
// Public API
// ============================================================================

void InspectorPanel::SetSelectedObjectId(uint64_t id)
{
    if (m_selectedObjectId != id)
        InvalidateObjectCaches();
    m_selectedObjectId = id;
    if (id != 0) {
        m_selectedFile.clear();
        m_assetCategory.clear();
        m_mode = InspectorMode::Object;
    }
}

void InspectorPanel::ClearSelectedObject()
{
    m_selectedObjectId = 0;
    InvalidateObjectCaches();
    m_mode = InspectorMode::Object;
}

void InspectorPanel::SetSelectedFile(const std::string &filePath, const std::string &category)
{
    if (filePath != m_selectedFile) {
        m_selectedFile = filePath;
        InvalidateObjectCaches();
    }
    if (!filePath.empty()) {
        m_assetCategory = category;
        m_mode = category.empty() ? InspectorMode::Preview : InspectorMode::Asset;
        // Clear object selection when file is selected
        m_selectedObjectId = 0;
    } else {
        m_assetCategory.clear();
        m_mode = InspectorMode::Object;
    }
}

void InspectorPanel::ClearSelectedFile()
{
    m_selectedFile.clear();
    m_assetCategory.clear();
    InvalidateObjectCaches();
    m_mode = InspectorMode::Object;
}

void InspectorPanel::SetDetailFile(const std::string &filePath, const std::string &category)
{
    if (filePath != m_selectedFile) {
        m_selectedFile = filePath;
    }
    if (!filePath.empty()) {
        m_assetCategory = category;
        m_mode = category.empty() ? InspectorMode::Preview : InspectorMode::Asset;
        // NOTE: unlike SetSelectedFile, does NOT clear object selection
    } else {
        m_assetCategory.clear();
        m_mode = InspectorMode::Object;
    }
}

// ============================================================================
// VisiblePreRender
// ============================================================================

void InspectorPanel::VisiblePreRender(InxGUIContext * /*ctx*/)
{
    auto now = std::chrono::steady_clock::now();
    m_frameTimeNow = std::chrono::duration<float>(now.time_since_epoch()).count();
}

// ============================================================================
// OnRenderContent — main entry
// ============================================================================

void InspectorPanel::OnRenderContent(InxGUIContext *ctx)
{
    float totalHeight = ImGui::GetContentRegionAvail().y;

    bool hasDetailContent = !m_selectedFile.empty();
    bool fileOnly = hasDetailContent && m_selectedObjectId == 0;

    if (fileOnly) {
        // Full-height file view (asset inspector or generic preview)
        RenderRawDataModule(ctx, 0.0f);
    } else if (hasDetailContent &&
               totalHeight > (EditorTheme::INSPECTOR_MIN_PROPS_H + EditorTheme::INSPECTOR_MIN_RAWDATA_H +
                              EditorTheme::INSPECTOR_SPLITTER_H)) {
        float usableHeight = totalHeight - EditorTheme::INSPECTOR_SPLITTER_H;
        float propsHeight = usableHeight * m_propertiesRatio;
        float rawDataHeight = usableHeight - propsHeight;

        if (propsHeight < EditorTheme::INSPECTOR_MIN_PROPS_H) {
            propsHeight = EditorTheme::INSPECTOR_MIN_PROPS_H;
            rawDataHeight = usableHeight - propsHeight;
        }
        if (rawDataHeight < EditorTheme::INSPECTOR_MIN_RAWDATA_H) {
            rawDataHeight = EditorTheme::INSPECTOR_MIN_RAWDATA_H;
            propsHeight = usableHeight - rawDataHeight;
        }

        RenderPropertiesModule(ctx, propsHeight);
        RenderSplitter(ctx, totalHeight);
        RenderRawDataModule(ctx, rawDataHeight);
    } else {
        RenderPropertiesModule(ctx, 0.0f);
    }
}

// ============================================================================
// Properties module (top half)
// ============================================================================

void InspectorPanel::RenderPropertiesModule(InxGUIContext *ctx, float height)
{
    if (undoBeginFrame)
        undoBeginFrame();

    bool childVisible = ImGui::BeginChild("PropertiesModule", ImVec2(0, height), ImGuiChildFlags_Borders,
                                          ImGuiWindowFlags_AlwaysVerticalScrollbar);
    if (childVisible) {
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, EditorTheme::INSPECTOR_FRAME_PAD);
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, EditorTheme::INSPECTOR_ITEM_SPC);

        bool multi = isMultiSelection && isMultiSelection();
        if (multi) {
            auto ids = getSelectedIds ? getSelectedIds() : std::vector<uint64_t>{};
            if (ids.size() > 1) {
                RenderMultiEdit(ctx, ids);
            } else {
                ImGui::TextUnformatted(Tr("inspector.no_objects_selected").c_str());
            }
        } else {
            if (m_selectedObjectId != 0) {
                RenderSingleObject(ctx, m_selectedObjectId);
            } else {
                ImGui::TextUnformatted(Tr("inspector.no_object_selected").c_str());
            }
        }

        ImGui::PopStyleVar(2);
    }
    ImGui::EndChild();

    bool anyActive = ImGui::IsAnyItemActive();
    if (undoEndFrame)
        undoEndFrame(anyActive);

    // Drag-drop target for scripts on the whole PropertiesModule
    if (m_selectedObjectId != 0 && ImGui::BeginDragDropTarget()) {
        const ImGuiPayload *payload = ImGui::AcceptDragDropPayload("SCRIPT_FILE");
        if (payload && handleScriptDrop) {
            std::string path(static_cast<const char *>(payload->Data), payload->DataSize);
            // Remove trailing null if present
            if (!path.empty() && path.back() == '\0')
                path.pop_back();
            handleScriptDrop(path);
        }
        ImGui::EndDragDropTarget();
    }
}

// ============================================================================
// Raw data module (bottom half)
// ============================================================================

void InspectorPanel::RenderRawDataModule(InxGUIContext *ctx, float height)
{
    bool childVisible = ImGui::BeginChild("RawDataModule", ImVec2(0, height), ImGuiChildFlags_Borders,
                                          ImGuiWindowFlags_AlwaysVerticalScrollbar);
    if (childVisible) {
        if (!m_selectedFile.empty() && m_mode == InspectorMode::Asset) {
            if (renderAssetInspector)
                renderAssetInspector(ctx, m_selectedFile, m_assetCategory);
            else
                ImGui::TextUnformatted("(Asset inspector not available)");
        } else if (!m_selectedFile.empty()) {
            if (renderFilePreview)
                renderFilePreview(ctx, m_selectedFile);
            else
                ImGui::TextUnformatted(m_selectedFile.c_str());
        } else {
            ImGui::TextUnformatted(Tr("inspector.no_selection").c_str());
        }
    }
    ImGui::EndChild();
}

// ============================================================================
// Splitter
// ============================================================================

float InspectorPanel::RenderSplitter(InxGUIContext * /*ctx*/, float totalHeight)
{
    ImGui::Separator();

    float availWidth = ImGui::GetContentRegionAvail().x;
    ImGui::InvisibleButton("##InspectorSplitter", ImVec2(availWidth, EditorTheme::INSPECTOR_SPLITTER_H));

    bool hovered = ImGui::IsItemHovered();
    bool active = ImGui::IsItemActive();

    if (hovered || active)
        ImGui::SetMouseCursor(ImGuiMouseCursor_ResizeNS);

    if (active) {
        float deltaY = ImGui::GetMouseDragDelta(0).y;
        if (std::abs(deltaY) > 1.0f) {
            float usableHeight = totalHeight - EditorTheme::INSPECTOR_SPLITTER_H;
            if (usableHeight > 0.0f) {
                float newPropsHeight = m_propertiesRatio * usableHeight + deltaY;
                float newRatio = newPropsHeight / usableHeight;
                float minRatio = EditorTheme::INSPECTOR_MIN_PROPS_H / usableHeight;
                float maxRatio = 1.0f - (EditorTheme::INSPECTOR_MIN_RAWDATA_H / usableHeight);
                m_propertiesRatio = std::clamp(newRatio, minRatio, maxRatio);
            }
            ImGui::ResetMouseDragDelta(0);
        }
    }

    ImGui::Separator();
    return m_propertiesRatio;
}

// ============================================================================
// Single object inspector
// ============================================================================

void InspectorPanel::RenderSingleObject(InxGUIContext *ctx, uint64_t objId)
{
#if INFERNUX_FRAME_PROFILE
    using clock = std::chrono::high_resolution_clock;
#endif

    uint64_t valueGeneration = getValueGeneration ? getValueGeneration() : 0;
    const bool objectChanged = (m_cachedObjInfoId != objId) || (m_cachedComponentListObjId != objId);
    bool refreshSnapshots = objectChanged || (valueGeneration != m_cachedValueGeneration) ||
                            ((m_frameTimeNow - m_cachedValueRefreshTime) >= VALUE_CACHE_TTL);

    if (objectChanged) {
        m_cachedComponentBodyHeights.clear();
        m_cachedTransformBodyHeight = 0.0f;
    }

    // ── Sub-timing: getObjectInfo + getPrefabInfo ────────────────────
#if INFERNUX_FRAME_PROFILE
    auto t0 = clock::now();
#endif

    if (refreshSnapshots) {
        if (getObjectInfo)
            m_cachedObjInfo = getObjectInfo(objId);
        else
            m_cachedObjInfo = {};
        m_cachedObjInfoId = objId;
        if (!m_cachedObjInfo.prefabGuid.empty() && getPrefabInfo)
            m_cachedPrefabInfo = getPrefabInfo(objId);
        else
            m_cachedPrefabInfo = {};
    }

#if INFERNUX_FRAME_PROFILE
    auto t1 = clock::now();
    m_subGetInfo += std::chrono::duration<double, std::milli>(t1 - t0).count();
#endif

    const auto &info = m_cachedObjInfo;
    const auto &pinfo = m_cachedPrefabInfo;
    bool isPrefabReadonly = pinfo.isReadonly;
    bool isPrefabTransformReadonly = pinfo.isTransformReadonly;

    ImGui::PushID(static_cast<int>(objId));

    // Prefab header bar (if applicable)
    if (!info.prefabGuid.empty()) {
        RenderPrefabHeader(ctx, objId, pinfo);
    }

    // Active, Name, Tag, Layer are scene-instance properties — always editable,
    // even on prefab instances (they don't come from the prefab asset).

    // Object header: active checkbox + name input
    RenderObjectHeader(ctx, objId, info);

    // Tag & Layer
    RenderTagLayerRow(ctx, objId, info);

    ImGui::Dummy(ImVec2(0, EditorTheme::INSPECTOR_TITLE_GAP));
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, EditorTheme::INSPECTOR_SECTION_GAP));

    // ── Sub-timing: Transform ────────────────────────────────────────
#if INFERNUX_FRAME_PROFILE
    auto t2 = clock::now();
#endif

    // Transform (skip for screen-space UI elements)
    if (!info.hideTransform) {
        if (m_cachedTransformIconId == 0 && getComponentIconId)
            m_cachedTransformIconId = getComponentIconId("Transform", false);
        auto [headerOpen, _unused] = RenderComponentHeader(
            ctx, "Transform", "transform", m_cachedTransformIconId,
            /*showEnabled=*/false, /*isEnabled=*/true, /*suffix=*/"",
            /*defaultOpen=*/true, "inspector.object." + std::to_string(objId) + ".component.transform");

        if (headerOpen) {
            if (isPrefabTransformReadonly)
                ImGui::BeginDisabled();
            if (m_cachedTransformBodyHeight > 0.0f && ctx &&
                !ctx->IsVirtualizedRegionVisible(m_cachedTransformBodyHeight)) {
                ImGui::Dummy(ImVec2(0.0f, m_cachedTransformBodyHeight));
            } else {
                const float bodyStartY = ImGui::GetCursorPosY();
                RenderTransform(ctx, objId);
                const float bodyHeight = ImGui::GetCursorPosY() - bodyStartY;
                if (bodyHeight > 0.0f)
                    m_cachedTransformBodyHeight = bodyHeight;
            }
            if (isPrefabTransformReadonly)
                ImGui::EndDisabled();
        }
    }

#if INFERNUX_FRAME_PROFILE
    auto t3 = clock::now();
    m_subTransform += std::chrono::duration<double, std::milli>(t3 - t2).count();
#endif

    // --- Prefab instance: disable everything below Transform ---
    if (isPrefabReadonly)
        ImGui::BeginDisabled();

    // ── Sub-timing: getComponentList ─────────────────────────────────
#if INFERNUX_FRAME_PROFILE
    auto t4 = clock::now();
#endif

    if (refreshSnapshots) {
        if (getComponentList)
            m_cachedComponents = getComponentList(objId);
        else
            m_cachedComponents.clear();
        m_cachedComponentListObjId = objId;
        m_cachedValueGeneration = valueGeneration;
        m_cachedValueRefreshTime = m_frameTimeNow;
    }

    const auto &components = m_cachedComponents;

#if INFERNUX_FRAME_PROFILE
    auto t5 = clock::now();
    m_subGetComponents += std::chrono::duration<double, std::milli>(t5 - t4).count();
#endif

    // ── Sub-timing: Component bodies ─────────────────────────────────
#if INFERNUX_FRAME_PROFILE
    auto t6 = clock::now();
#endif

    // Render each component
    for (const auto &comp : components) {
        ImGui::PushID(static_cast<int>(comp.componentId));

        const char *scriptSuffix = comp.isBroken ? " (Missing Script)" : " (Script)";
        const std::string &componentLabel = comp.displayName.empty() ? comp.typeName : comp.displayName;
        auto [headerOpen, newEnabled] = RenderComponentHeader(
            ctx, componentLabel, "comp_" + std::to_string(comp.componentId), comp.iconId,
            /*showEnabled=*/true, comp.enabled, comp.isScript ? scriptSuffix : "",
            /*defaultOpen=*/true,
            "inspector.object." + std::to_string(objId) + ".component." + std::to_string(comp.componentId),
            comp.isScript ? "py_comp_ctx" : "comp_ctx");

        // Right-click context menu — only call Python when popup is open
        bool componentRemoved = false;
        {
            const char *ctxPopupId = comp.isScript ? "py_comp_ctx" : "comp_ctx";
            if (ImGui::BeginPopup(ctxPopupId)) {
                if (renderComponentContextMenu) {
                    componentRemoved =
                        renderComponentContextMenu(ctx, objId, comp.typeName, comp.componentId, comp.isNative);
                }
                ImGui::EndPopup();
            }
        }

        if (!componentRemoved) {
            // Enabled toggle
            if (newEnabled != comp.enabled && setComponentEnabled) {
                setComponentEnabled(objId, comp.componentId, newEnabled, comp.isNative);
            }

            // Component body
            if (headerOpen) {
                if (comp.isBroken && !comp.brokenError.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text, EditorTheme::ERROR_TEXT);
                    ImGui::TextWrapped("%s", comp.brokenError.c_str());
                    ImGui::PopStyleColor();
                } else if (renderComponentBody) {
                    const auto heightIt = m_cachedComponentBodyHeights.find(comp.componentId);
                    const float cachedHeight = heightIt != m_cachedComponentBodyHeights.end() ? heightIt->second : 0.0f;
                    if (cachedHeight > 0.0f && ctx && !ctx->IsVirtualizedRegionVisible(cachedHeight)) {
                        ImGui::Dummy(ImVec2(0.0f, cachedHeight));
                    } else {
                        const float bodyStartY = ImGui::GetCursorPosY();
                        renderComponentBody(ctx, objId, comp.typeName, comp.componentId, comp.isNative);
                        const float bodyHeight = ImGui::GetCursorPosY() - bodyStartY;
                        if (bodyHeight > 0.0f)
                            m_cachedComponentBodyHeights[comp.componentId] = bodyHeight;
                    }
                }
            }
        }

        ImGui::PopID();
    }

    // Add Component button + popup
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0, EditorTheme::INSPECTOR_SECTION_GAP));
    RenderAddComponentButton(ctx);
    ImGui::Dummy(ImVec2(0, EditorTheme::INSPECTOR_SECTION_GAP));
    RenderAddComponentPopup(ctx);

#if INFERNUX_FRAME_PROFILE
    auto t7 = clock::now();
    m_subComponentBodies += std::chrono::duration<double, std::milli>(t7 - t6).count();
#endif

    // ── Sub-timing: Material sections ────────────────────────────────
#if INFERNUX_FRAME_PROFILE
    auto t8 = clock::now();
#endif

    // Material override sections
    if (renderMaterialSections)
        renderMaterialSections(ctx, objId);

#if INFERNUX_FRAME_PROFILE
    auto t9 = clock::now();
    m_subMaterials += std::chrono::duration<double, std::milli>(t9 - t8).count();
#endif

    if (isPrefabReadonly)
        ImGui::EndDisabled();

    ImGui::PopID();
}

// ============================================================================
// Multi-edit inspector
// ============================================================================

const std::vector<InspectorPanel::CommonComponent> &
InspectorPanel::GetCommonComponentsForMultiSelection(const std::vector<uint64_t> &ids)
{
    const uint64_t valueGeneration = getValueGeneration ? getValueGeneration() : 0;
    const bool refreshSnapshots = !m_cachedMultiComponentsValid || (m_cachedMultiComponentIds != ids) ||
                                  (valueGeneration != m_cachedMultiComponentValueGeneration) ||
                                  ((m_frameTimeNow - m_cachedMultiComponentRefreshTime) >= VALUE_CACHE_TTL);
    if (!refreshSnapshots)
        return m_cachedMultiCommonComponents;

    m_cachedMultiComponentsValid = true;
    m_cachedMultiComponentIds = ids;
    m_cachedMultiComponentValueGeneration = valueGeneration;
    m_cachedMultiComponentRefreshTime = m_frameTimeNow;
    m_cachedMultiCommonComponents.clear();

    if (!getComponentList || ids.empty())
        return m_cachedMultiCommonComponents;

    std::vector<std::vector<ComponentInfo>> perObjectComponents;
    perObjectComponents.reserve(ids.size());
    for (uint64_t objectId : ids)
        perObjectComponents.push_back(getComponentList(objectId));

    if (perObjectComponents.empty())
        return m_cachedMultiCommonComponents;

    auto makeKindKey = [](const ComponentInfo &component) {
        return component.typeName + (component.isNative ? "#n" : "#p");
    };
    auto makeKey = [&](const ComponentInfo &component, int occurrence) {
        return makeKindKey(component) + "#" + std::to_string(occurrence);
    };

    std::vector<std::pair<std::string, ComponentInfo>> firstKeys;
    std::unordered_map<std::string, int> firstCounts;
    for (const auto &component : perObjectComponents[0]) {
        const int occurrence = firstCounts[makeKindKey(component)]++;
        firstKeys.push_back({makeKey(component, occurrence), component});
    }

    std::vector<std::unordered_map<std::string, ComponentInfo>> componentMaps;
    componentMaps.reserve(perObjectComponents.size());
    for (const auto &componentList : perObjectComponents) {
        std::unordered_map<std::string, ComponentInfo> componentMap;
        std::unordered_map<std::string, int> counts;
        for (const auto &component : componentList) {
            const int occurrence = counts[makeKindKey(component)]++;
            componentMap[makeKey(component, occurrence)] = component;
        }
        componentMaps.push_back(std::move(componentMap));
    }

    for (const auto &[key, firstComponent] : firstKeys) {
        CommonComponent entry;
        entry.display = firstComponent;
        bool presentOnAll = true;
        for (const auto &componentMap : componentMaps) {
            auto iter = componentMap.find(key);
            if (iter == componentMap.end()) {
                presentOnAll = false;
                break;
            }
            entry.componentIds.push_back(iter->second.componentId);
        }
        if (presentOnAll)
            m_cachedMultiCommonComponents.push_back(std::move(entry));
    }

    return m_cachedMultiCommonComponents;
}

const InspectorPanel::MultiTransformSnapshot &
InspectorPanel::GetMultiTransformSnapshot(const std::vector<uint64_t> &ids)
{
    const uint64_t valueGeneration = getValueGeneration ? getValueGeneration() : 0;
    const bool refreshSnapshot = !m_cachedMultiTransformValid || (m_cachedMultiTransformIds != ids) ||
                                 (valueGeneration != m_cachedMultiTransformValueGeneration) ||
                                 ((m_frameTimeNow - m_cachedMultiTransformRefreshTime) >= VALUE_CACHE_TTL);
    if (!refreshSnapshot)
        return m_cachedMultiTransformSnapshot;

    m_cachedMultiTransformValid = true;
    m_cachedMultiTransformIds = ids;
    m_cachedMultiTransformValueGeneration = valueGeneration;
    m_cachedMultiTransformRefreshTime = m_frameTimeNow;
    m_cachedMultiTransformSnapshot = {};

    if (!getTransformData || ids.empty())
        return m_cachedMultiTransformSnapshot;

    TransformData first = getTransformData(ids[0]);
    m_cachedMultiTransformSnapshot.first = first;

    for (size_t objectIndex = 1; objectIndex < ids.size(); ++objectIndex) {
        TransformData transformData = getTransformData(ids[objectIndex]);
        auto &mixed = m_cachedMultiTransformSnapshot.mixed;
        mixed[0] = mixed[0] || std::abs(transformData.px - first.px) > 1e-6f;
        mixed[1] = mixed[1] || std::abs(transformData.py - first.py) > 1e-6f;
        mixed[2] = mixed[2] || std::abs(transformData.pz - first.pz) > 1e-6f;
        mixed[3] = mixed[3] || std::abs(transformData.rx - first.rx) > 1e-6f;
        mixed[4] = mixed[4] || std::abs(transformData.ry - first.ry) > 1e-6f;
        mixed[5] = mixed[5] || std::abs(transformData.rz - first.rz) > 1e-6f;
        mixed[6] = mixed[6] || std::abs(transformData.sx - first.sx) > 1e-6f;
        mixed[7] = mixed[7] || std::abs(transformData.sy - first.sy) > 1e-6f;
        mixed[8] = mixed[8] || std::abs(transformData.sz - first.sz) > 1e-6f;
    }

    return m_cachedMultiTransformSnapshot;
}

void InspectorPanel::RenderMultiEdit(InxGUIContext *ctx, const std::vector<uint64_t> &ids)
{
#if INFERNUX_FRAME_PROFILE
    using clock = std::chrono::high_resolution_clock;
#endif

    int n = static_cast<int>(ids.size());
    ImGui::PushID("multi_edit");

    ImGui::Text("%d objects selected", n);

    if (!ids.empty()) {
        ImGui::Dummy(ImVec2(0, EditorTheme::INSPECTOR_TITLE_GAP));
        ImGui::Separator();
        ImGui::Dummy(ImVec2(0, EditorTheme::INSPECTOR_SECTION_GAP));

        // Transform header
        uint64_t transformIcon = getComponentIconId ? getComponentIconId("Transform", false) : 0;
        auto [headerOpen, _unused] = RenderComponentHeader(ctx, "Transform", "multi_transform", transformIcon,
                                                           /*showEnabled=*/false, /*isEnabled=*/true, /*suffix=*/"",
                                                           /*defaultOpen=*/true);

#if INFERNUX_FRAME_PROFILE
        auto transformStart = clock::now();
#endif
        if (headerOpen)
            RenderMultiTransform(ctx, ids);
#if INFERNUX_FRAME_PROFILE
        auto transformEnd = clock::now();
        m_subTransform += std::chrono::duration<double, std::milli>(transformEnd - transformStart).count();
#endif

#if INFERNUX_FRAME_PROFILE
        auto componentsStart = clock::now();
#endif
        const auto &commonComponents = GetCommonComponentsForMultiSelection(ids);
#if INFERNUX_FRAME_PROFILE
        auto componentsEnd = clock::now();
        m_subGetComponents += std::chrono::duration<double, std::milli>(componentsEnd - componentsStart).count();
#endif

#if INFERNUX_FRAME_PROFILE
        auto componentBodiesStart = clock::now();
#endif
        for (const auto &entry : commonComponents) {
            const auto &comp = entry.display;
            ImGui::PushID(static_cast<int>(comp.componentId));

            uint64_t iconId =
                comp.iconId ? comp.iconId : (getComponentIconId ? getComponentIconId(comp.typeName, comp.isScript) : 0);

            const std::string &componentLabel = comp.displayName.empty() ? comp.typeName : comp.displayName;
            auto [compOpen, newEnabled] =
                RenderComponentHeader(ctx, componentLabel, "multi_comp_" + std::to_string(comp.componentId), iconId,
                                      true, comp.enabled, comp.isScript ? " (Script)" : "", true);

            if (newEnabled != comp.enabled && setComponentEnabled) {
                for (size_t objectIndex = 0; objectIndex < ids.size() && objectIndex < entry.componentIds.size();
                     ++objectIndex)
                    setComponentEnabled(ids[objectIndex], entry.componentIds[objectIndex], newEnabled, comp.isNative);
                m_cachedMultiComponentsValid = false;
            }

            if (compOpen) {
                if (comp.isBroken && !comp.brokenError.empty()) {
                    ImGui::PushStyleColor(ImGuiCol_Text, EditorTheme::ERROR_TEXT);
                    ImGui::TextWrapped("%s", comp.brokenError.c_str());
                    ImGui::PopStyleColor();
                } else if (renderMultiComponentBody)
                    renderMultiComponentBody(ctx, ids, comp.typeName, entry.componentIds, comp.isNative);
                else if (renderComponentBody)
                    renderComponentBody(ctx, ids[0], comp.typeName, comp.componentId, comp.isNative);
            }

            ImGui::PopID();
        }
#if INFERNUX_FRAME_PROFILE
        auto componentBodiesEnd = clock::now();
        m_subComponentBodies +=
            std::chrono::duration<double, std::milli>(componentBodiesEnd - componentBodiesStart).count();
#endif

        // Add Component
        ImGui::Separator();
        ImGui::Dummy(ImVec2(0, EditorTheme::INSPECTOR_SECTION_GAP));
        RenderAddComponentButton(ctx);
        ImGui::Dummy(ImVec2(0, EditorTheme::INSPECTOR_SECTION_GAP));
        RenderAddComponentPopup(ctx);
    }

    ImGui::PopID();
}

// ============================================================================
// Object header (active + name)
// ============================================================================

void InspectorPanel::RenderObjectHeader(InxGUIContext *ctx, uint64_t objId, const ObjectInfo &info)
{
    const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
    // Active checkbox
    bool active = info.active;
    bool newActive = RenderInspectorCheckbox(ctx, "##obj_active", active);
    if (captureSemantics)
        ctx->RecordSemanticItem("inspector_active", "Active", true,
                                "inspector.object." + std::to_string(objId) + ".active");
    if (newActive != active && setObjectProperty) {
        m_cachedObjInfo.active = newActive;
        setObjectProperty(objId, "active", newActive ? "true" : "false");
    }

    ImGui::SameLine(0, 6);

    // Editable name
    ImGui::SetNextItemWidth(-1);
    char nameBuf[256];
    std::strncpy(nameBuf, info.name.c_str(), sizeof(nameBuf) - 1);
    nameBuf[sizeof(nameBuf) - 1] = '\0';
    const bool nameChanged = ImGui::InputText("##obj_name", nameBuf, sizeof(nameBuf));
    if (captureSemantics)
        ctx->RecordSemanticItem("inspector_name", "Name", true, "inspector.object." + std::to_string(objId) + ".name");
    if (nameChanged) {
        std::string newName(nameBuf);
        if (newName != info.name && setObjectProperty) {
            m_cachedObjInfo.name = newName;
            setObjectProperty(objId, "name", newName);
        }
    }
}

// ============================================================================
// Tag & Layer row
// ============================================================================

void InspectorPanel::RefreshTagLayerCache()
{
    if (m_frameTimeNow - m_tagLayerCacheTime < TAG_LAYER_CACHE_TTL && !m_cachedTagItems.empty() &&
        !m_cachedLayerItems.empty())
        return;

    m_tagLayerCacheTime = m_frameTimeNow;

    if (getAllTags) {
        m_cachedTags = getAllTags();
        m_cachedTagItems = m_cachedTags;
        m_cachedTagItems.push_back("Add Tag...");
    }

    if (getAllLayers) {
        m_cachedLayers = getAllLayers();
        m_cachedLayerItems.clear();
        for (size_t i = 0; i < m_cachedLayers.size(); ++i) {
            std::string label = std::to_string(i) + ": ";
            label += m_cachedLayers[i].empty() ? "---" : m_cachedLayers[i];
            m_cachedLayerItems.push_back(std::move(label));
        }
        m_cachedLayerItems.push_back("Add Layer...");
    }
}

void InspectorPanel::RenderTagLayerRow(InxGUIContext *ctx, uint64_t objId, const ObjectInfo &info)
{
    const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
    RefreshTagLayerCache();

    float availW = ImGui::GetContentRegionAvail().x;
    float halfW = availW * 0.5f - 4.0f;

    // --- Tag (left column) ---
    ImGui::TextUnformatted(Tr("inspector.tag").c_str());
    ImGui::SameLine(0, 4.0f);

    int tagIdx = 0;
    for (size_t i = 0; i < m_cachedTags.size(); ++i) {
        if (m_cachedTags[i] == info.tag) {
            tagIdx = static_cast<int>(i);
            break;
        }
    }

    int newTagIdx = ctx->SearchableCombo("Inspector.Tag", tagIdx, m_cachedTagItems, halfW - 30, 8,
                                         Tr("igui.search_hint").c_str(), Tr("igui.no_results").c_str());
    if (captureSemantics)
        ctx->RecordSemanticItem("inspector_tag", Tr("inspector.tag"), true,
                                "inspector.object." + std::to_string(objId) + ".tag");
    if (newTagIdx != tagIdx) {
        if (newTagIdx == static_cast<int>(m_cachedTags.size())) {
            // "Add Tag..." selected
            if (openWindow)
                openWindow("tag_layer_settings");
        } else if (newTagIdx >= 0 && newTagIdx < static_cast<int>(m_cachedTags.size())) {
            m_cachedObjInfo.tag = m_cachedTags[newTagIdx];
            if (setObjectProperty)
                setObjectProperty(objId, "tag", m_cachedTags[newTagIdx]);
        }
    }

    // --- Layer (right column) ---
    ImGui::SameLine(halfW + 8);
    ImGui::TextUnformatted(Tr("inspector.layer").c_str());
    ImGui::SameLine(0, 4.0f);
    float layerComboW = ImGui::GetContentRegionAvail().x;

    int newLayer = ctx->SearchableCombo("Inspector.Layer", info.layer, m_cachedLayerItems, layerComboW, 8,
                                        Tr("igui.search_hint").c_str(), Tr("igui.no_results").c_str());
    if (captureSemantics)
        ctx->RecordSemanticItem("inspector_layer", Tr("inspector.layer"), true,
                                "inspector.object." + std::to_string(objId) + ".layer");
    if (newLayer != info.layer) {
        if (newLayer == static_cast<int>(m_cachedLayerItems.size()) - 1) {
            // "Add Layer..." selected
            if (openWindow)
                openWindow("tag_layer_settings");
        } else if (setObjectProperty) {
            m_cachedObjInfo.layer = newLayer;
            setObjectProperty(objId, "layer", std::to_string(newLayer));
        }
    }
}

// ============================================================================
// Transform rendering
// ============================================================================

void InspectorPanel::RenderTransform(InxGUIContext *ctx, uint64_t objId)
{
    if (!getTransformData)
        return;

    TransformData td = getTransformData(objId);
    float labelW = EditorTheme::INSPECTOR_MIN_LABEL_WIDTH;

    // Vector3Control modifies the array in-place
    float pos[3] = {td.px, td.py, td.pz};
    float rot[3] = {td.rx, td.ry, td.rz};
    float scl[3] = {td.sx, td.sy, td.sz};

    const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
    const std::string transformBase =
        captureSemantics ? "inspector.object." + std::to_string(objId) + ".transform." : std::string{};
    const std::string positionSemanticId = captureSemantics ? transformBase + "position" : std::string{};
    const std::string rotationSemanticId = captureSemantics ? transformBase + "rotation" : std::string{};
    const std::string scaleSemanticId = captureSemantics ? transformBase + "scale" : std::string{};

    ctx->Vector3Control(Tr("Position"), pos, DRAG_SPEED_DEFAULT, labelW, positionSemanticId);
    if (captureSemantics)
        ctx->RecordSemanticItem("inspector_transform", Tr("Position"), true, positionSemanticId);
    ctx->Vector3Control(Tr("Rotation"), rot, DRAG_SPEED_DEFAULT, labelW, rotationSemanticId);
    if (captureSemantics)
        ctx->RecordSemanticItem("inspector_transform", Tr("Rotation"), true, rotationSemanticId);
    ctx->Vector3Control(Tr("Scale"), scl, DRAG_SPEED_FINE, labelW, scaleSemanticId);
    if (captureSemantics)
        ctx->RecordSemanticItem("inspector_transform", Tr("Scale"), true, scaleSemanticId);

    if (setTransformData) {
        bool changed = false;
        changed |= std::abs(pos[0] - td.px) > 1e-6f;
        changed |= std::abs(pos[1] - td.py) > 1e-6f;
        changed |= std::abs(pos[2] - td.pz) > 1e-6f;
        changed |= std::abs(rot[0] - td.rx) > 1e-6f;
        changed |= std::abs(rot[1] - td.ry) > 1e-6f;
        changed |= std::abs(rot[2] - td.rz) > 1e-6f;
        changed |= std::abs(scl[0] - td.sx) > 1e-6f;
        changed |= std::abs(scl[1] - td.sy) > 1e-6f;
        changed |= std::abs(scl[2] - td.sz) > 1e-6f;

        if (changed) {
            TransformData newTd;
            newTd.px = pos[0];
            newTd.py = pos[1];
            newTd.pz = pos[2];
            newTd.rx = rot[0];
            newTd.ry = rot[1];
            newTd.rz = rot[2];
            newTd.sx = scl[0];
            newTd.sy = scl[1];
            newTd.sz = scl[2];
            setTransformData(objId, newTd);
        }
    }
}

void InspectorPanel::RenderMultiTransform(InxGUIContext *ctx, const std::vector<uint64_t> &ids)
{
    if (!getTransformData || ids.empty())
        return;

    const auto &snapshot = GetMultiTransformSnapshot(ids);
    TransformData first = snapshot.first;
    bool mixed[9] = {};
    for (size_t axisIndex = 0; axisIndex < snapshot.mixed.size(); ++axisIndex)
        mixed[axisIndex] = snapshot.mixed[axisIndex];

    float labelW = EditorTheme::INSPECTOR_MIN_LABEL_WIDTH;
    float pos[3] = {first.px, first.py, first.pz};
    float rot[3] = {first.rx, first.ry, first.rz};
    float scl[3] = {first.sx, first.sy, first.sz};
    bool axisChanged[9] = {};

    auto renderRow = [&](const std::string &label, const char *rowId, float *values, const float *originalValues,
                         const bool *rowMixed, int baseIndex, float speed) {
        ImGui::TextUnformatted(label.c_str());
        ImGui::SameLine(labelW);

        const ImGuiStyle &style = ImGui::GetStyle();
        const float avail = std::max(1.0f, ImGui::GetContentRegionAvail().x);
        const float cellWidth = std::max(24.0f, (avail - style.ItemSpacing.x * 2.0f) / 3.0f);

        ImGui::PushID(rowId);
        for (int axisIndex = 0; axisIndex < 3; ++axisIndex) {
            if (axisIndex > 0)
                ImGui::SameLine(0.0f, style.ItemSpacing.x);

            ImGui::PushID(axisIndex);
            ImGui::SetNextItemWidth(cellWidth);
            if (rowMixed[axisIndex]) {
                static std::unordered_map<std::string, std::array<char, 64>> mixedBuffers;
                const std::string bufferKey = std::string(rowId) + ":" + std::to_string(axisIndex);
                auto &buffer = mixedBuffers[bufferKey];
                const bool submitted = ImGui::InputTextWithHint("##mixed", "--", buffer.data(), buffer.size(),
                                                                ImGuiInputTextFlags_EnterReturnsTrue);
                const bool committed = submitted || ImGui::IsItemDeactivatedAfterEdit();
                if (committed) {
                    char *end = nullptr;
                    const float parsed = std::strtof(buffer.data(), &end);
                    while (end && *end != '\0' && std::isspace(static_cast<unsigned char>(*end)))
                        ++end;
                    if (end != buffer.data() && end && *end == '\0') {
                        values[axisIndex] = parsed;
                        axisChanged[baseIndex + axisIndex] = true;
                    }
                    buffer.fill('\0');
                }
            } else {
                ImGui::DragFloat("##value", &values[axisIndex], speed, -1000000.0f, 1000000.0f, "%.3f");
                axisChanged[baseIndex + axisIndex] = std::abs(values[axisIndex] - originalValues[axisIndex]) > 1e-6f;
            }
            ImGui::PopID();
        }
        ImGui::PopID();
    };

    const float originalPos[3] = {first.px, first.py, first.pz};
    const float originalRot[3] = {first.rx, first.ry, first.rz};
    const float originalScale[3] = {first.sx, first.sy, first.sz};

    renderRow(Tr("Position"), "position", pos, originalPos, mixed, 0, DRAG_SPEED_DEFAULT);
    renderRow(Tr("Rotation"), "rotation", rot, originalRot, mixed + 3, 3, DRAG_SPEED_DEFAULT);
    renderRow(Tr("Scale"), "scale", scl, originalScale, mixed + 6, 6, DRAG_SPEED_FINE);

    if (!setTransformData)
        return;

    bool anyAxisChanged = false;
    for (bool changed : axisChanged)
        anyAxisChanged |= changed;
    if (!anyAxisChanged)
        return;

    for (uint64_t id : ids) {
        TransformData td = getTransformData(id);
        if (axisChanged[0])
            td.px = pos[0];
        if (axisChanged[1])
            td.py = pos[1];
        if (axisChanged[2])
            td.pz = pos[2];
        if (axisChanged[3])
            td.rx = rot[0];
        if (axisChanged[4])
            td.ry = rot[1];
        if (axisChanged[5])
            td.rz = rot[2];
        if (axisChanged[6])
            td.sx = scl[0];
        if (axisChanged[7])
            td.sy = scl[1];
        if (axisChanged[8])
            td.sz = scl[2];
        setTransformData(id, td);
    }
    m_cachedMultiTransformValid = false;
}

// ============================================================================
// Prefab header
// ============================================================================

void InspectorPanel::RenderPrefabHeader(InxGUIContext *ctx, uint64_t objId, const PrefabInfo &pinfo)
{
    const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
    const std::string semanticBase =
        captureSemantics ? "inspector.object." + std::to_string(objId) + ".prefab" : std::string{};
    ImGui::Dummy(ImVec2(0, 4));

    ImGui::PushStyleColor(ImGuiCol_ChildBg, EditorTheme::PREFAB_HEADER_BG);
    ImGui::BeginChild("##prefab_header_bar", ImVec2(0, EditorTheme::PREFAB_HEADER_H), ImGuiChildFlags_Borders);

    ImGui::PushStyleColor(ImGuiCol_Text, EditorTheme::PREFAB_TEXT);
    ImGui::TextUnformatted(Tr("inspector.prefab_label").c_str());
    if (captureSemantics)
        ctx->RecordSemanticItem("prefab_header", Tr("inspector.prefab_label"), true, semanticBase, std::nullopt,
                                static_cast<double>(pinfo.overrideCount));
    ImGui::PopStyleColor();

    float gap = EditorTheme::PREFAB_HEADER_BTN_GAP;

    ImGui::SameLine(0, gap * 2);
    EditorTheme::PushFlatButtonStyle(EditorTheme::INSPECTOR_INLINE_BTN_IDLE);
    const std::string selectLabel = Tr("inspector.prefab_select");
    if (ImGui::Button(selectLabel.c_str())) {
        if (prefabAction)
            prefabAction(objId, "select");
    }
    if (captureSemantics)
        ctx->RecordSemanticItem("prefab_action", selectLabel, true, semanticBase + ".select");
    ImGui::PopStyleColor(3);

    ImGui::SameLine(0, gap);
    EditorTheme::PushFlatButtonStyle(EditorTheme::INSPECTOR_INLINE_BTN_IDLE);
    const std::string openLabel = Tr("inspector.prefab_open");
    if (ImGui::Button(openLabel.c_str())) {
        if (prefabAction)
            prefabAction(objId, "open");
    }
    if (captureSemantics)
        ctx->RecordSemanticItem("prefab_action", openLabel, true, semanticBase + ".open");
    ImGui::PopStyleColor(3);

    ImGui::SameLine(0, gap * 3);

    if (pinfo.overrideCount > 0) {
        ImGui::PushStyleColor(ImGuiCol_Text, EditorTheme::WARNING_TEXT);
        ImGui::Text("%d %s", pinfo.overrideCount, Tr("inspector.overrides").c_str());
        if (captureSemantics)
            ctx->RecordSemanticItem("prefab_override_count", Tr("inspector.overrides"), false,
                                    semanticBase + ".override_count", std::nullopt,
                                    static_cast<double>(pinfo.overrideCount));
        ImGui::PopStyleColor();

        ImGui::SameLine(0, gap * 2);
        EditorTheme::PushFlatButtonStyle(EditorTheme::INSPECTOR_INLINE_BTN_IDLE);
        const std::string applyLabel = Tr("inspector.prefab_apply");
        if (ImGui::Button(applyLabel.c_str())) {
            if (prefabAction)
                prefabAction(objId, "apply");
        }
        if (captureSemantics)
            ctx->RecordSemanticItem("prefab_action", applyLabel, true, semanticBase + ".apply");
        ImGui::PopStyleColor(3);

        ImGui::SameLine(0, gap);
        EditorTheme::PushFlatButtonStyle(EditorTheme::INSPECTOR_INLINE_BTN_IDLE);
        const std::string revertLabel = Tr("inspector.prefab_revert");
        if (ImGui::Button(revertLabel.c_str())) {
            if (prefabAction)
                prefabAction(objId, "revert");
        }
        if (captureSemantics)
            ctx->RecordSemanticItem("prefab_action", revertLabel, true, semanticBase + ".revert");
        ImGui::PopStyleColor(3);
    } else {
        ImGui::PushStyleColor(ImGuiCol_Text, EditorTheme::TEXT_DIM2);
        ImGui::TextUnformatted(Tr("inspector.no_overrides").c_str());
        if (captureSemantics)
            ctx->RecordSemanticItem("prefab_override_count", Tr("inspector.no_overrides"), false,
                                    semanticBase + ".override_count", std::nullopt, 0.0);
        ImGui::PopStyleColor();
    }

    ImGui::EndChild();
    ImGui::PopStyleColor();
}

// ============================================================================
// Component header (icon + checkbox + collapsing header)
// ============================================================================

std::pair<bool, bool> InspectorPanel::RenderComponentHeader(InxGUIContext *ctx, const std::string &typeName,
                                                            const std::string &headerId, uint64_t iconId,
                                                            bool showEnabled, bool isEnabled, const std::string &suffix,
                                                            bool defaultOpen, const std::string &semanticId,
                                                            const std::string &contextPopupId)
{
    bool newEnabled = isEnabled;

    // Build display name: insert spaces before uppercase chars
    std::string displayName;
    for (size_t i = 0; i < typeName.size(); ++i) {
        char c = typeName[i];
        if (i > 0 && std::isupper(static_cast<unsigned char>(c))) {
            char prev = typeName[i - 1];
            if (!std::isupper(static_cast<unsigned char>(prev)) && prev != ' ')
                displayName += ' ';
        }
        displayName += c;
    }
    displayName += suffix;

    // Styling
    ImGui::PushStyleColor(ImGuiCol_Header, EditorTheme::INSPECTOR_HEADER_PRIMARY);
    ImGui::PushStyleColor(ImGuiCol_HeaderHovered, EditorTheme::INSPECTOR_HEADER_PRIMARY_HOVERED);
    ImGui::PushStyleColor(ImGuiCol_HeaderActive, EditorTheme::INSPECTOR_HEADER_PRIMARY_ACTIVE);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, EditorTheme::INSPECTOR_HEADER_PRIMARY_FRAME_PAD);
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, EditorTheme::INSPECTOR_HEADER_ITEM_SPC);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, EditorTheme::INSPECTOR_HEADER_BORDER_SIZE);
    ImGui::SetWindowFontScale(EditorTheme::INSPECTOR_HEADER_PRIMARY_FONT_SCALE);

    // Full-width collapsing header
    if (defaultOpen)
        ImGui::SetNextItemOpen(true, ImGuiCond_FirstUseEver);
    ImGui::SetNextItemAllowOverlap();

    std::string headerKey = "##comp_" + headerId;
    {
        float clipMaxX = ImGui::GetWindowPos().x + ImGui::GetCursorPosX() + ImGui::GetContentRegionAvail().x -
                         EditorTheme::INSPECTOR_HEADER_RIGHT_MARGIN;
        ImGui::GetWindowDrawList()->PushClipRect(ImVec2(0.0f, 0.0f), ImVec2(clipMaxX, 1e7f), true);
    }
    bool headerOpen = ImGui::CollapsingHeader(headerKey.c_str());
    ImGui::GetWindowDrawList()->PopClipRect();
    const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
    const std::string semanticBase =
        captureSemantics ? (semanticId.empty() ? "inspector.component." + headerId : semanticId) : std::string{};
    if (ctx && captureSemantics)
        ctx->RecordSemanticItem("component_header", displayName, true, semanticBase);

    const ImVec2 headerMin = ImGui::GetItemRectMin();
    const ImVec2 headerMax = ImGui::GetItemRectMax();
    float headerMinY = headerMin.y;
    float headerMaxY = headerMax.y;
    float headerHeight = (std::max)(0.0f, headerMaxY - headerMinY);

    // Overlay: icon + checkbox + label on the same row
    float indent = EditorTheme::INSPECTOR_HEADER_CONTENT_INDENT;
    ImGui::SameLine(indent, 0);

    if (iconId != 0) {
        float iconSize = EditorTheme::COMPONENT_ICON_SIZE;
        ImGui::Dummy(ImVec2(iconSize, (std::max)(headerHeight, iconSize)));
        ImVec2 slotMin = ImGui::GetItemRectMin();
        ImVec2 slotMax = ImGui::GetItemRectMax();
        float drawSize = (std::min)({iconSize, slotMax.x - slotMin.x, slotMax.y - slotMin.y});
        float drawX = slotMin.x + (std::max)(0.0f, (slotMax.x - slotMin.x - drawSize) * 0.5f);
        float drawY = slotMin.y + (std::max)(0.0f, (slotMax.y - slotMin.y - drawSize) * 0.5f);
        // Snap to integer pixels to avoid bilinear mush on subpixel edges (common vs Unity crisp UI).
        drawSize = std::floor(drawSize);
        drawX = std::floor(drawX);
        drawY = std::floor(drawY);

        ImDrawList *drawList = ImGui::GetWindowDrawList();
        ImTextureRef texRef(static_cast<ImTextureID>(iconId));
        drawList->AddImage(texRef, ImVec2(drawX, drawY), ImVec2(drawX + drawSize, drawY + drawSize));

        ImGui::SameLine(0, EditorTheme::INSPECTOR_HEADER_ITEM_SPC.x);
    }

    if (showEnabled) {
        newEnabled = RenderInspectorCheckbox(ctx, "##hdr_en", isEnabled);
        if (ctx && captureSemantics)
            ctx->RecordSemanticItem("component_enabled", displayName, true, semanticBase + ".enabled");
        ImGui::SameLine(0, EditorTheme::INSPECTOR_HEADER_ITEM_SPC.x);
    }

    ImGui::AlignTextToFramePadding();
    ImGui::TextUnformatted(displayName.c_str());

    if (!contextPopupId.empty()) {
        constexpr float optionsWidth = 24.0f;
        ImGui::SameLine();
        const float rightEdge = ImGui::GetWindowContentRegionMax().x;
        ImGui::SetCursorPosX((std::max)(ImGui::GetCursorPosX(), rightEdge - optionsWidth));
        if (ImGui::SmallButton("...##component_options"))
            ImGui::OpenPopup(contextPopupId.c_str());
        const std::string optionsLabel = Tr("inspector.component_options");
        if (ImGui::IsItemHovered(ImGuiHoveredFlags_DelayNormal))
            ImGui::SetTooltip("%s", optionsLabel.c_str());
        if (ctx && captureSemantics)
            ctx->RecordSemanticItem("component_options", optionsLabel, true, semanticBase + ".options");
        if (ImGui::IsMouseHoveringRect(headerMin, headerMax) && ImGui::IsMouseClicked(ImGuiMouseButton_Right))
            ImGui::OpenPopup(contextPopupId.c_str());
    }

    // Cleanup
    ImGui::SetWindowFontScale(1.0f);
    ImGui::PopStyleColor(3);
    ImGui::PopStyleVar(3);

    return {headerOpen, newEnabled};
}

// ============================================================================
// Inspector checkbox
// ============================================================================

bool InspectorPanel::RenderInspectorCheckbox(InxGUIContext *ctx, const char *label, bool value)
{
    bool newValue = value;
    if (ctx)
        ctx->CheckboxInspector(label ? label : "##cb", &newValue);
    return newValue;
}

// ============================================================================
// Add Component button + popup
// ============================================================================

void InspectorPanel::RenderAddComponentButton(InxGUIContext *ctx)
{
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, EditorTheme::ADD_COMP_FRAME_PAD);
    ImGui::SetCursorPosX(EditorTheme::INSPECTOR_ACTION_ALIGN_X);
    const std::string label = Tr("inspector.add_component");
    const bool clicked = ImGui::Button(label.c_str(), ImVec2(-1, 0));
    if (ctx && InxGUISemantics::IsCaptureEnabled())
        ctx->RecordSemanticItem("add_component", label, true, "inspector.add_component");
    if (clicked) {
        m_addCompSearch[0] = '\0';
        if (getAddComponentEntries)
            m_addCompEntries = getAddComponentEntries();
        m_addCompNeedsFocus = true;
        ImGui::OpenPopup("##add_component_popup");
    }
    ImGui::PopStyleVar();
}

void InspectorPanel::RenderAddComponentPopup(InxGUIContext *ctx)
{
    // Unity-style component browser: generous bleed, full-width search and
    // tall rows with a clear hover band.
    const float em = ImGui::GetFontSize();
    ImGui::SetNextWindowSizeConstraints(ImVec2(em * 18.0f, 0.0f), ImVec2(FLT_MAX, FLT_MAX));
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, ImVec2(em * 0.7f, em * 0.7f));
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, ImVec2(em * 0.4f, em * 0.4f));

    if (ImGui::BeginPopup("##add_component_popup")) {
        const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
        if (ctx && captureSemantics)
            ctx->RecordSemanticWindow("add_component_popup", "Add Component", "inspector.add_component.popup");
        // Search field
        if (m_addCompNeedsFocus)
            ImGui::SetKeyboardFocusHere();
        ImGui::SetNextItemWidth(std::max(EditorTheme::ADD_COMP_SEARCH_W, ImGui::GetContentRegionAvail().x));
        const bool submitFirst =
            ImGui::InputTextWithHint("##comp_search", Tr("inspector.search_components").c_str(), m_addCompSearch,
                                     sizeof(m_addCompSearch), ImGuiInputTextFlags_EnterReturnsTrue);
        if (ctx && captureSemantics)
            ctx->RecordSemanticItem("component_search", Tr("inspector.search_components"), true,
                                    "inspector.add_component.search");
        const bool searchFocused = ImGui::IsItemFocused();
        if (searchFocused)
            m_addCompNeedsFocus = false;
        const bool focusFirst = searchFocused && ImGui::IsKeyPressed(ImGuiKey_DownArrow);
        if (ImGui::IsKeyPressed(ImGuiKey_Escape))
            ImGui::CloseCurrentPopup();

        ImGui::Separator();

        // Scrollable region
        if (ImGui::BeginChild("##comp_list", ImVec2(0, 350), ImGuiChildFlags_None)) {
            std::string searchLower(m_addCompSearch);
            std::transform(searchLower.begin(), searchLower.end(), searchLower.begin(),
                           [](unsigned char c) { return static_cast<char>(std::tolower(c)); });

            bool foundAny = false;
            bool assignedKeyboardFocus = false;
            bool handledKeyboardSelection = false;
            int uid = 0;

            // Group entries by category
            std::unordered_map<std::string, std::vector<const AddComponentEntry *>> categories;
            std::vector<std::string> categoryOrder;

            for (const auto &entry : m_addCompEntries) {
                // Filter by search
                if (!searchLower.empty()) {
                    std::string nameLower = entry.displayName;
                    std::transform(nameLower.begin(), nameLower.end(), nameLower.begin(),
                                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
                    if (nameLower.find(searchLower) == std::string::npos)
                        continue;
                }

                std::string cat = entry.category.empty() ? "Miscellaneous" : entry.category;
                if (categories.find(cat) == categories.end())
                    categoryOrder.push_back(cat);
                categories[cat].push_back(&entry);
            }

            std::sort(categoryOrder.begin(), categoryOrder.end());

            const float rowHeight = ImGui::GetTextLineHeight() + ImGui::GetFontSize() * 0.55f;
            for (const auto &cat : categoryOrder) {
                // Muted category header, Unity-style section grouping.
                ImGui::PushStyleColor(ImGuiCol_Text, ImVec4(0.55f, 0.55f, 0.55f, 1.0f));
                ImGui::TextUnformatted(cat.c_str());
                ImGui::PopStyleColor();
                ImGui::Separator();

                for (const auto *entry : categories[cat]) {
                    foundAny = true;
                    ++uid;
                    std::string selectLabel = "  " + entry->displayName + "##" + std::to_string(uid);
                    if (focusFirst && !assignedKeyboardFocus) {
                        ImGui::SetKeyboardFocusHere();
                        assignedKeyboardFocus = true;
                    }
                    const bool activateFromKeyboard = submitFirst && !handledKeyboardSelection;
                    // Taller rows with vertically centered labels give the
                    // hover highlight a solid, easy-to-track band.
                    ImGui::PushStyleVar(ImGuiStyleVar_SelectableTextAlign, ImVec2(0.0f, 0.5f));
                    const bool selected = ImGui::Selectable(selectLabel.c_str(), false, 0, ImVec2(0.0f, rowHeight));
                    ImGui::PopStyleVar();
                    if (ctx && captureSemantics)
                        ctx->RecordSemanticItem("component_option", entry->displayName, true,
                                                "inspector.add_component.option." + std::to_string(uid));
                    if (selected || activateFromKeyboard) {
                        if (addComponent) {
                            addComponent(entry->displayName, entry->isNative, entry->scriptPath);
                            // The selection stays unchanged, so the normal selection-driven
                            // invalidation path does not run after adding a component.
                            InvalidateObjectCaches();
                        }
                        ImGui::CloseCurrentPopup();
                        handledKeyboardSelection = true;
                    }
                }
                ImGui::Dummy(ImVec2(0, 4));
            }

            if (!foundAny) {
                ImGui::TextUnformatted(Tr("inspector.no_components_found").c_str());
            }
        }
        ImGui::EndChild();
        ImGui::EndPopup();
    }

    ImGui::PopStyleVar(2);
}

} // namespace infernux
