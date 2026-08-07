#include "InxGUISemantics.h"

#include <imgui.h>
#include <imgui_internal.h>

#include <algorithm>
#include <array>
#include <atomic>
#include <mutex>
#include <unordered_map>
#include <unordered_set>
#include <utility>

namespace infernux
{
namespace
{

std::atomic_bool g_captureEnabled{false};
std::atomic_bool g_continuousCapture{false};
std::atomic_uint64_t g_captureRequestSequence{0};
std::atomic_uint64_t g_completedRequestSequence{0};
std::atomic_uint64_t g_pendingInputSequence{0};
std::mutex g_snapshotMutex;
std::mutex g_focusMutex;
std::mutex g_windowPresentationMutex;
InxGUISemanticSnapshot g_workingSnapshot;
InxGUISemanticSnapshot g_publishedSnapshot;
std::string g_focusedWindowId;
std::unordered_set<std::string> g_presentedWindowIds;
std::unordered_map<std::string, std::string> g_presentedDockPeerByWindowId;
bool g_hasWindowPresentationSnapshot = false;
std::atomic_uint64_t g_workingRequestSequence{0};
std::atomic_uint64_t g_beginFrameCount{0};
std::atomic_uint64_t g_endFrameCount{0};
std::atomic_uint64_t g_abortFrameCount{0};
std::atomic_uint64_t g_publishCount{0};

struct PendingTargetSource
{
    ImGuiWindow *window = nullptr;
};

std::vector<PendingTargetSource> g_workingTargetSources;

std::string VisibleLabel(const std::string &label)
{
    const size_t separator = label.find("##");
    return separator == std::string::npos ? label : label.substr(0, separator);
}

void SplitWindowName(const char *name, std::string &displayName, std::string &windowId)
{
    const std::string fullName = name ? name : "";
    const size_t separator = fullName.find("###");
    if (separator == std::string::npos) {
        displayName = fullName;
        windowId = fullName;
        return;
    }

    displayName = fullName.substr(0, separator);
    windowId = fullName.substr(separator + 3);
    if (displayName.empty())
        displayName = windowId;
}

std::string MakeTargetId(const std::string &kind, const std::string &windowId, const std::string &semanticId,
                         const std::string &label, uint32_t itemId)
{
    const std::string &name = semanticId.empty() ? label : semanticId;
    std::string id;
    id.reserve(kind.size() + windowId.size() + name.size() + 16);
    id.append(kind).push_back(':');
    id.append(windowId).push_back(':');
    id.append(name).push_back(':');
    id.append(std::to_string(itemId));
    return id;
}

bool IsItemVisible(const ImGuiLastItemData &item)
{
    return (item.StatusFlags & ImGuiItemStatusFlags_Visible) != 0;
}

const ImGuiWindow *RootWindow(const ImGuiWindow *window)
{
    return window != nullptr && window->RootWindow != nullptr ? window->RootWindow : window;
}

bool IsWindowInteractable(const ImGuiWindow *window)
{
    if (window == nullptr || window->Hidden || window->Collapsed || window->SkipItems)
        return false;

    // Semantic records for compound editor tools are usually emitted from
    // nested child windows. Dock visibility belongs to their root panel.
    const ImGuiWindow *rootWindow = RootWindow(window);
    if (rootWindow->Hidden || rootWindow->Collapsed || rootWindow->SkipItems)
        return false;

#ifdef IMGUI_HAS_DOCK
    // ImGui already resolves docking presentation on the docked panel window.
    // Root dock hosts may have no VisibleWindow of their own, so inferring tab
    // visibility from the host loses ordinary side panels such as Inspector.
    // DockTabIsVisible is the authoritative selected-tab result for this frame.
    const ImGuiDockNode *dockNode = rootWindow->DockNode;
    if (dockNode != nullptr) {
        if (!rootWindow->DockNodeIsVisible || !rootWindow->DockTabIsVisible)
            return false;
        if (dockNode->VisibleWindow != nullptr && dockNode->VisibleWindow != rootWindow)
            return false;
    }
#endif

    return true;
}

bool IsItemEnabled(const ImGuiLastItemData &item, bool requestedEnabled)
{
    return requestedEnabled && (item.ItemFlags & ImGuiItemFlags_Disabled) == 0;
}

bool ReceivesPointerAt(const PendingTargetSource &source, const ImVec2 &point)
{
    ImGuiWindow *hoveredWindow = nullptr;
    ImGui::FindHoveredWindowEx(point, false, &hoveredWindow, nullptr);
    if (hoveredWindow == nullptr)
        return false;

    // ImGui's final hit test can resolve a root panel rather than the deepest
    // nested child that submitted the item. A different root panel is an
    // actual obstruction; the same root preserves normal child routing.
    if (RootWindow(hoveredWindow) == RootWindow(source.window))
        return true;

#ifdef IMGUI_HAS_DOCK
    // A dock tab is drawn and hit-tested by its dock host, while its semantic
    // controls may be submitted by a child window whose own DockNode is null.
    // Resolve the node through the panel root and treat only that exact host
    // as the native interaction surface, without admitting unrelated windows.
    const ImGuiWindow *sourceRoot = RootWindow(source.window);
    const ImGuiDockNode *dockNode = source.window != nullptr ? source.window->DockNode : nullptr;
    if (dockNode == nullptr && sourceRoot != nullptr)
        dockNode = sourceRoot->DockNode;
    for (const ImGuiDockNode *node = dockNode; node != nullptr; node = node->ParentNode) {
        if (node->HostWindow == hoveredWindow || hoveredWindow->DockNodeAsHost == node)
            return true;
        if (node->HostWindow != nullptr && RootWindow(hoveredWindow) == RootWindow(node->HostWindow))
            return true;
    }
#endif

    return false;
}

bool FindSafeClickPoint(const InxGUISemanticTarget &target, const PendingTargetSource &source, ImVec2 &point)
{
    if (source.window == nullptr || target.width <= 0.0f || target.height <= 0.0f)
        return false;

    // ImGui items can report their complete content width even when most of
    // that item is clipped by a child window (long Console rows are a common
    // example). Choosing the raw rectangle center can therefore target a
    // different control in the same root panel. Restrict semantic pointer
    // candidates to the portion a human can actually see and click.
    ImRect clickable(ImVec2(target.x, target.y), ImVec2(target.x + target.width, target.y + target.height));
    clickable.ClipWith(source.window->ClipRect);
    if (clickable.Min.x >= clickable.Max.x || clickable.Min.y >= clickable.Max.y)
        return false;
    const float clickableWidth = clickable.GetWidth();
    const float clickableHeight = clickable.GetHeight();

    // Try the ordinary center first, then representative points around it.
    // This retains normal button behavior while recovering visible portions
    // of a panel that are partially covered by another floating tool window.
    static constexpr std::array<std::array<float, 2>, 9> kSamples = {{{0.5f, 0.5f},
                                                                      {0.2f, 0.5f},
                                                                      {0.8f, 0.5f},
                                                                      {0.5f, 0.2f},
                                                                      {0.5f, 0.8f},
                                                                      {0.2f, 0.2f},
                                                                      {0.8f, 0.2f},
                                                                      {0.2f, 0.8f},
                                                                      {0.8f, 0.8f}}};
    for (const auto &sample : kSamples) {
        const ImVec2 candidate(clickable.Min.x + clickableWidth * sample[0],
                               clickable.Min.y + clickableHeight * sample[1]);
        if (ReceivesPointerAt(source, candidate)) {
            point = candidate;
            return true;
        }
    }

    // A floating tool can leave only a thin, but still usable, edge of a
    // docked panel exposed. Interior samples alone miss that situation and
    // incorrectly describe the target as unavailable even though a human can
    // still click the revealed canvas. Keep this fallback just inside each
    // edge, rather than on the resize border itself.
    const float insetX = std::min(4.0f, std::max(1.0f, clickableWidth * 0.01f));
    const float insetY = std::min(4.0f, std::max(1.0f, clickableHeight * 0.01f));
    const std::array<float, 3> xSamples = {clickable.Min.x + insetX, clickable.GetCenter().x, clickable.Max.x - insetX};
    const std::array<float, 3> ySamples = {clickable.Min.y + insetY, clickable.GetCenter().y, clickable.Max.y - insetY};
    for (float y : ySamples) {
        for (float x : xSamples) {
            const ImVec2 candidate(x, y);
            if (ReceivesPointerAt(source, candidate)) {
                point = candidate;
                return true;
            }
        }
    }
    return false;
}

void RecordOccludingWindow(InxGUISemanticTarget &target)
{
    if (target.width <= 0.0f || target.height <= 0.0f)
        return;

    ImGuiWindow *occludingWindow = nullptr;
    const ImVec2 center(target.x + target.width * 0.5f, target.y + target.height * 0.5f);
    ImGui::FindHoveredWindowEx(center, false, &occludingWindow, nullptr);
    if (occludingWindow != nullptr)
        SplitWindowName(occludingWindow->Name, target.occludedByWindow, target.occludedByWindowId);
}

void MarkTargetUnavailable(InxGUISemanticTarget &target)
{
    target.visible = false;
    target.enabled = false;
    target.hasClickPoint = false;
}

void FinalizeTargetReachability()
{
    const size_t sourceCount = std::min(g_workingSnapshot.targets.size(), g_workingTargetSources.size());
    for (size_t index = 0; index < sourceCount; ++index) {
        InxGUISemanticTarget &target = g_workingSnapshot.targets[index];
        const PendingTargetSource &source = g_workingTargetSources[index];
        if (!target.visible || !IsWindowInteractable(source.window)) {
            MarkTargetUnavailable(target);
            continue;
        }

        ImVec2 clickPoint;
        if (!FindSafeClickPoint(target, source, clickPoint)) {
            RecordOccludingWindow(target);
            MarkTargetUnavailable(target);
            continue;
        }
        target.clickX = clickPoint.x;
        target.clickY = clickPoint.y;
        target.hasClickPoint = true;
    }

    for (size_t index = sourceCount; index < g_workingSnapshot.targets.size(); ++index)
        MarkTargetUnavailable(g_workingSnapshot.targets[index]);
}

} // namespace

void InxGUISemantics::SetCaptureEnabled(bool enabled)
{
    g_continuousCapture.store(enabled, std::memory_order_release);
    if (enabled)
        return;

    // Disabling continuous capture must not cancel an already queued one-shot
    // request. MCP requests arrive from a worker thread and may race startup or
    // tool-registration policy changes; treating "continuous off" as "complete
    // every request" silently loses the exact frame the caller is waiting for.
    if (!HasPendingCaptureRequest())
        g_captureEnabled.store(false, std::memory_order_release);
}

bool InxGUISemantics::IsCaptureEnabled()
{
    return g_captureEnabled.load(std::memory_order_acquire);
}

bool InxGUISemantics::HasPendingCaptureRequest()
{
    return g_continuousCapture.load(std::memory_order_acquire) ||
           g_captureRequestSequence.load(std::memory_order_acquire) !=
               g_completedRequestSequence.load(std::memory_order_acquire);
}

uint64_t InxGUISemantics::RequestSnapshot(uint64_t inputSequence)
{
    uint64_t pending = g_pendingInputSequence.load(std::memory_order_acquire);
    while (pending < inputSequence &&
           !g_pendingInputSequence.compare_exchange_weak(pending, inputSequence, std::memory_order_acq_rel,
                                                         std::memory_order_acquire)) {
    }
    const uint64_t sequence = g_captureRequestSequence.fetch_add(1, std::memory_order_acq_rel) + 1;
    return sequence;
}

void InxGUISemantics::BeginFrame(uint64_t frame)
{
    g_beginFrameCount.fetch_add(1, std::memory_order_relaxed);
    const uint64_t requestedSequence = g_captureRequestSequence.load(std::memory_order_acquire);
    const bool captureFrame = g_continuousCapture.load(std::memory_order_acquire) ||
                              requestedSequence != g_completedRequestSequence.load(std::memory_order_acquire);
    g_captureEnabled.store(captureFrame, std::memory_order_release);
    if (!captureFrame)
        return;

    g_workingSnapshot.captureEnabled = true;
    g_workingSnapshot.frame = frame;
    g_workingSnapshot.requestSequence = requestedSequence;
    g_workingSnapshot.inputSequence = g_pendingInputSequence.exchange(0, std::memory_order_acq_rel);
    g_workingRequestSequence.store(requestedSequence, std::memory_order_release);
    g_workingSnapshot.mouseX = 0.0f;
    g_workingSnapshot.mouseY = 0.0f;
    g_workingSnapshot.wantsTextInput = false;
    g_workingSnapshot.dragDropActive = false;
    g_workingSnapshot.dragDropPreview = false;
    g_workingSnapshot.dragDropDelivery = false;
    g_workingSnapshot.dragDropPayloadType.clear();
    g_workingSnapshot.dragDropSourceId = 0;
    g_workingSnapshot.dragDropAcceptId = 0;
    g_workingSnapshot.focusedWindow.clear();
    g_workingSnapshot.focusedWindowId.clear();
    g_workingSnapshot.targets.clear();
    g_workingTargetSources.clear();
}

void InxGUISemantics::EndFrame()
{
    g_endFrameCount.fetch_add(1, std::memory_order_relaxed);
    ImGuiContext *context = ImGui::GetCurrentContext();
    std::string focusedWindow;
    std::string focusedWindowId;
    if (context != nullptr && context->NavWindow != nullptr)
        SplitWindowName(context->NavWindow->Name, focusedWindow, focusedWindowId);
    {
        std::lock_guard<std::mutex> lock(g_focusMutex);
        g_focusedWindowId = focusedWindowId;
    }

    std::unordered_set<std::string> presentedWindowIds;
    std::unordered_map<std::string, std::string> presentedDockPeerByWindowId;
    if (context != nullptr) {
        for (ImGuiWindow *window : context->Windows) {
            if (window == nullptr || window->LastFrameActive != context->FrameCount)
                continue;
            std::string displayName;
            std::string windowId;
            SplitWindowName(window->Name, displayName, windowId);
            const ImGuiWindow *rootWindow = RootWindow(window);
            std::string rootWindowId;
            if (rootWindow != nullptr) {
                SplitWindowName(rootWindow->Name, displayName, windowId);
                rootWindowId = windowId;
            }

#ifdef IMGUI_HAS_DOCK
            ImGuiDockNode *dockNode = window->DockNode;
            if (dockNode == nullptr && rootWindow != nullptr)
                dockNode = rootWindow->DockNode;
            if (dockNode != nullptr) {
                const ImGuiWindow *visibleWindow = dockNode->VisibleWindow;
                if (visibleWindow != nullptr) {
                    std::string visibleWindowId;
                    SplitWindowName(visibleWindow->Name, displayName, visibleWindowId);
                    if (!visibleWindowId.empty()) {
                        // A hidden dock tab is not guaranteed to retain the
                        // same root/DockNode projection as the selected tab.
                        // Build the relation from the dock node's own tab
                        // collection so every peer can resolve the page it
                        // would displace before the current frame completes.
                        for (const ImGuiWindow *tabWindow : dockNode->Windows) {
                            if (tabWindow == nullptr)
                                continue;
                            std::string tabWindowId;
                            SplitWindowName(tabWindow->Name, displayName, tabWindowId);
                            if (!tabWindowId.empty())
                                presentedDockPeerByWindowId[std::move(tabWindowId)] = visibleWindowId;
                        }
                        if (!rootWindowId.empty())
                            presentedDockPeerByWindowId[rootWindowId] = visibleWindowId;
                    }
                }
            }
#endif

            if (!IsWindowInteractable(window))
                continue;
            SplitWindowName(window->Name, displayName, windowId);
            if (!windowId.empty())
                presentedWindowIds.insert(std::move(windowId));

            // Compound panels submit controls from child windows. Publish the
            // stable panel id as well so all interaction producers ask the
            // same presentation question regardless of which child focused.
            if (!rootWindowId.empty())
                presentedWindowIds.insert(std::move(rootWindowId));
        }
    }
    {
        std::lock_guard<std::mutex> lock(g_windowPresentationMutex);
        g_presentedWindowIds = std::move(presentedWindowIds);
        g_presentedDockPeerByWindowId = std::move(presentedDockPeerByWindowId);
        g_hasWindowPresentationSnapshot = context != nullptr;
    }

    if (!IsCaptureEnabled())
        return;

    if (context) {
        const ImGuiIO &io = ImGui::GetIO();
        g_workingSnapshot.mouseX = io.MousePos.x;
        g_workingSnapshot.mouseY = io.MousePos.y;
        g_workingSnapshot.wantsTextInput = io.WantTextInput;
        g_workingSnapshot.dragDropActive = context->DragDropActive;
        if (context->DragDropActive) {
            const ImGuiPayload &payload = context->DragDropPayload;
            g_workingSnapshot.dragDropPreview = payload.Preview;
            g_workingSnapshot.dragDropDelivery = payload.Delivery;
            g_workingSnapshot.dragDropPayloadType = payload.DataType;
            g_workingSnapshot.dragDropSourceId = payload.SourceId;
            g_workingSnapshot.dragDropAcceptId = context->DragDropAcceptIdCurr;
        }
        g_workingSnapshot.focusedWindow = std::move(focusedWindow);
        g_workingSnapshot.focusedWindowId = focusedWindowId;
        FinalizeTargetReachability();
    } else {
        for (InxGUISemanticTarget &target : g_workingSnapshot.targets)
            MarkTargetUnavailable(target);
    }
    g_workingTargetSources.clear();

    std::lock_guard<std::mutex> lock(g_snapshotMutex);
    if (IsCaptureEnabled()) {
        g_publishedSnapshot = g_workingSnapshot;
        g_publishCount.fetch_add(1, std::memory_order_relaxed);
    }
    g_completedRequestSequence.store(g_workingRequestSequence.load(std::memory_order_acquire),
                                     std::memory_order_release);
    g_captureEnabled.store(false, std::memory_order_release);
}

std::string InxGUISemantics::GetFocusedWindowId()
{
    std::lock_guard<std::mutex> lock(g_focusMutex);
    return g_focusedWindowId;
}

std::optional<bool> InxGUISemantics::WasWindowContentPresented(const std::string &windowId)
{
    std::lock_guard<std::mutex> lock(g_windowPresentationMutex);
    if (!g_hasWindowPresentationSnapshot)
        return std::nullopt;
    return g_presentedWindowIds.find(windowId) != g_presentedWindowIds.end();
}

std::optional<std::string> InxGUISemantics::PresentedDockPeerForWindow(const std::string &windowId)
{
    std::lock_guard<std::mutex> lock(g_windowPresentationMutex);
    if (!g_hasWindowPresentationSnapshot)
        return std::nullopt;
    const auto it = g_presentedDockPeerByWindowId.find(windowId);
    if (it == g_presentedDockPeerByWindowId.end())
        return std::nullopt;
    return it->second;
}

void InxGUISemantics::AbortFrame() noexcept
{
    g_abortFrameCount.fetch_add(1, std::memory_order_relaxed);
    g_workingSnapshot.targets.clear();
    g_workingTargetSources.clear();
    g_workingRequestSequence.store(0, std::memory_order_release);
    g_captureEnabled.store(false, std::memory_order_release);
}

void InxGUISemantics::RecordLastItem(const std::string &kind, const std::string &label, bool enabled,
                                     const std::string &semanticId, std::optional<bool> boolValue,
                                     std::optional<double> numericValue, std::optional<std::string> stringValue)
{
    if (!IsCaptureEnabled())
        return;

    ImGuiContext *context = ImGui::GetCurrentContext();
    if (!context)
        return;

    const ImGuiLastItemData &item = context->LastItemData;
    ImGuiWindow *window = ImGui::GetCurrentWindowRead();
    const bool openMenu = kind == "menu" && boolValue.value_or(false);
    if (openMenu && context->HoveredWindow != nullptr && item.Rect.Contains(context->IO.MousePos))
        window = context->HoveredWindow;
    InxGUISemanticTarget target;
    target.kind = kind;
    target.label = VisibleLabel(label);
    target.semanticId = semanticId;
    target.itemId = item.ID;
    target.x = item.Rect.Min.x;
    target.y = item.Rect.Min.y;
    target.width = item.Rect.Max.x - item.Rect.Min.x;
    target.height = item.Rect.Max.y - item.Rect.Min.y;
    target.visible = openMenu ? IsWindowInteractable(window) : IsItemVisible(item);
    target.enabled = openMenu ? enabled : IsItemEnabled(item, enabled);
    target.active = openMenu || ImGui::IsItemActive();
    target.focused = ImGui::IsItemFocused();
    if (boolValue.has_value()) {
        target.hasBoolValue = true;
        target.boolValue = *boolValue;
    } else if (numericValue.has_value()) {
        target.hasNumericValue = true;
        target.numericValue = *numericValue;
    } else if (stringValue.has_value()) {
        target.hasStringValue = true;
        target.stringValue = *stringValue;
    }
    SplitWindowName(window ? window->Name : "", target.window, target.windowId);
    target.id = MakeTargetId(target.kind, target.windowId, target.semanticId, target.label, target.itemId);

    g_workingSnapshot.targets.push_back(std::move(target));
    g_workingTargetSources.push_back({window});
}

void InxGUISemantics::RecordRect(const std::string &kind, const std::string &label, float x, float y, float width,
                                 float height, bool enabled, const std::string &semanticId)
{
    if (!IsCaptureEnabled() || width <= 0.0f || height <= 0.0f)
        return;

    ImGuiContext *context = ImGui::GetCurrentContext();
    ImGuiWindow *window = context ? ImGui::GetCurrentWindowRead() : nullptr;
    if (!window)
        return;

    const ImRect sourceRect(x, y, x + width, y + height);
    ImRect visibleRect = sourceRect;
    visibleRect.ClipWith(window->ClipRect);

    InxGUISemanticTarget target;
    target.kind = kind;
    target.label = VisibleLabel(label);
    target.semanticId = semanticId;
    target.itemId = window->GetID(("##semantic_rect:" + semanticId).c_str());
    target.x = visibleRect.Min.x;
    target.y = visibleRect.Min.y;
    target.width = std::max(0.0f, visibleRect.GetWidth());
    target.height = std::max(0.0f, visibleRect.GetHeight());
    target.visible = sourceRect.Overlaps(window->ClipRect) && target.width > 0.0f && target.height > 0.0f;
    target.enabled = enabled;
    target.active = false;
    target.focused = false;
    SplitWindowName(window->Name, target.window, target.windowId);
    target.id = MakeTargetId(target.kind, target.windowId, target.semanticId, target.label, target.itemId);

    g_workingSnapshot.targets.push_back(std::move(target));
    g_workingTargetSources.push_back({window});
}

void InxGUISemantics::RecordCurrentWindow(const std::string &kind, const std::string &label,
                                          const std::string &semanticId)
{
    if (!IsCaptureEnabled())
        return;

    ImGuiContext *context = ImGui::GetCurrentContext();
    ImGuiWindow *window = context ? ImGui::GetCurrentWindowRead() : nullptr;
    if (!window)
        return;

    InxGUISemanticTarget target;
    target.kind = kind;
    target.label = VisibleLabel(label);
    target.semanticId = semanticId;
    target.itemId = window->ID;
    target.x = window->Pos.x;
    target.y = window->Pos.y;
    target.width = window->Size.x;
    target.height = window->Size.y;
    target.visible = true;
    target.active = ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows);
    target.focused = target.active;
    SplitWindowName(window->Name, target.window, target.windowId);
    target.id = MakeTargetId(target.kind, target.windowId, target.semanticId, target.label, target.itemId);

    g_workingSnapshot.targets.push_back(std::move(target));
    g_workingTargetSources.push_back({window});
}

void InxGUISemantics::RecordCurrentWindowCloseButton(const std::string &semanticId)
{
    if (!IsCaptureEnabled())
        return;

    ImGuiContext *context = ImGui::GetCurrentContext();
    ImGuiWindow *window = context ? ImGui::GetCurrentWindowRead() : nullptr;
    if (!window || (window->Flags & ImGuiWindowFlags_NoTitleBar) != 0)
        return;

    const ImGuiStyle &style = ImGui::GetStyle();
    const float buttonSize = context->FontSize;
    ImGuiID closeButtonId = window->GetID("#CLOSE");
    ImVec2 buttonPos;

#ifdef IMGUI_HAS_DOCK
    if (ImGuiDockNode *dockNode = window->DockNode) {
        ImGuiTabBar *tabBar = dockNode->TabBar;
        ImGuiTabItem *tab = nullptr;
        if (tabBar != nullptr) {
            for (ImGuiTabItem &candidate : tabBar->Tabs) {
                if (candidate.Window == window) {
                    tab = &candidate;
                    break;
                }
            }
        }
        if (tab == nullptr || tabBar->VisibleTabId != tab->ID || !window->HasCloseButton)
            return;

        const bool centralSection = (tab->Flags & ImGuiTabItemFlags_SectionMask_) == 0;
        const float offset = centralSection ? IM_TRUNC(tab->Offset - tabBar->ScrollingAnim) : tab->Offset;
        const ImVec2 tabMin(tabBar->BarRect.Min.x + offset, tabBar->BarRect.Min.y);
        const ImRect tabRect(tabMin.x, tabMin.y, tabMin.x + tab->Width, tabMin.y + tabBar->BarRect.GetHeight());
        buttonPos = ImVec2(ImMax(tabRect.Min.x, tabRect.Max.x - tabBar->FramePadding.x - buttonSize),
                           tabRect.Min.y + tabBar->FramePadding.y);
        closeButtonId = ImGui::GetIDWithSeed("#CLOSE", nullptr, window->ID);
    } else
#endif
    {
        const ImRect titleBar = window->TitleBarRect();
        buttonPos = ImVec2(titleBar.Max.x - style.FramePadding.x - buttonSize, titleBar.Min.y + style.FramePadding.y);
    }

    InxGUISemanticTarget target;
    target.kind = "window_close";
    target.label = "Close";
    target.semanticId = semanticId;
    target.itemId = closeButtonId;
    target.x = buttonPos.x;
    target.y = buttonPos.y;
    target.width = buttonSize;
    target.height = buttonSize;
    target.visible = !window->Hidden && !window->SkipItems;
    target.enabled = true;
    target.active = context->ActiveId == target.itemId;
    target.focused = false;
    SplitWindowName(window->Name, target.window, target.windowId);
    target.id = MakeTargetId(target.kind, target.windowId, target.semanticId, target.label, target.itemId);

    g_workingSnapshot.targets.push_back(std::move(target));
    g_workingTargetSources.push_back({window});
}

InxGUISemanticSnapshot InxGUISemantics::GetSnapshot()
{
    std::lock_guard<std::mutex> lock(g_snapshotMutex);
    return g_publishedSnapshot;
}

InxGUISemanticCaptureState InxGUISemantics::GetCaptureState() noexcept
{
    InxGUISemanticCaptureState state;
    state.continuous = g_continuousCapture.load(std::memory_order_acquire);
    state.active = g_captureEnabled.load(std::memory_order_acquire);
    state.requestedSequence = g_captureRequestSequence.load(std::memory_order_acquire);
    state.completedSequence = g_completedRequestSequence.load(std::memory_order_acquire);
    state.pendingInputSequence = g_pendingInputSequence.load(std::memory_order_acquire);
    state.workingRequestSequence = g_workingRequestSequence.load(std::memory_order_acquire);
    state.beginFrameCount = g_beginFrameCount.load(std::memory_order_relaxed);
    state.endFrameCount = g_endFrameCount.load(std::memory_order_relaxed);
    state.abortFrameCount = g_abortFrameCount.load(std::memory_order_relaxed);
    state.publishCount = g_publishCount.load(std::memory_order_relaxed);
    return state;
}

} // namespace infernux
