#pragma once

#include <function/editor/EditorPanelFocusPublisher.h>
#include <function/renderer/gui/InxGUIContext.h>
#include <function/renderer/gui/InxGUIRenderable.h>

#include <functional>
#include <imgui.h>
#include <string>
#include <unordered_map>
#include <vector>

namespace infernux
{

/// Base class for C++ editor panels.
/// Provides Begin/End window management so subclasses only implement
/// OnRenderContent().
class EditorPanel : public InxGUIRenderable
{
  public:
    explicit EditorPanel(const std::string &title, const std::string &windowId = "")
        : m_title(title), m_windowId(windowId.empty() ? title : windowId)
    {
    }

    ~EditorPanel() override = default;

    /// True when the panel window is open (visible & docked or floating).
    [[nodiscard]] bool IsOpen() const
    {
        return m_isOpen;
    }

    /// True only when ImGui submitted this panel's contents in the current
    /// frame. An open panel can still be hidden behind another docking tab.
    [[nodiscard]] bool IsContentVisible() const
    {
        return m_isOpen && m_contentVisible;
    }

    /// Content visibility observed during the preceding panel frame. This
    /// distinguishes revealing a dock tab from focusing an already visible
    /// side-by-side panel.
    [[nodiscard]] bool WasContentVisible() const
    {
        return m_contentVisiblePreviousFrame;
    }

    /// True when this panel's presented content owns the current pointer
    /// target. Shared interaction services use this for pointer-addressed
    /// gestures such as files dropped by the operating system.
    [[nodiscard]] bool IsContentHovered() const
    {
        return m_isOpen && m_contentVisible && m_contentHovered;
    }

    void SetOpen(bool open)
    {
        if (m_isOpen && !open)
            CancelAllTransientInteractions();
        m_isOpen = open;
        if (!m_isOpen) {
            m_contentVisiblePreviousFrame = m_contentVisible;
            m_contentVisible = false;
            m_contentHovered = false;
        }
    }

    [[nodiscard]] const std::string &GetWindowId() const
    {
        return m_windowId;
    }

    // The second argument distinguishes a direct pointer activation from
    // passive/native focus synchronization. Only the former is user history.
    std::function<void(bool, bool)> onPanelFocused;
    /// Native title bars publish an intent; WindowManager owns the actual
    /// close transaction and all Focus/Modal/Document lifecycle effects.
    std::function<bool()> onRequestClose;
    std::function<void(const std::string &, const std::string &, int)> onTransientBegin;
    std::function<void(const std::string &)> onTransientEnd;

    // Every native editor panel submits user intent through the same command
    // bridge. Domain panels must not grow private command callbacks: doing so
    // makes new controls silently bypass Editor Interaction Core.
    std::function<bool(const std::string &, const std::string &, const std::string &)> executeCommand;
    std::function<bool(const std::string &, const std::string &)> canExecuteCommand;

    void SetPanelFocusedCallback(std::function<void(bool, bool)> callback)
    {
        onPanelFocused = std::move(callback);
        m_focusPublisher.Republish(onPanelFocused);
    }

    void RepublishPanelFocus()
    {
        m_focusPublisher.RepublishCurrent(onPanelFocused);
    }

    /// Cancel one native temporary interaction through its stable local token.
    /// Escape is routed by Editor Interaction Core; native panels only provide
    /// the operation-specific cancellation callback registered at begin time.
    bool CancelTransientInteraction(const std::string &token)
    {
        auto it = m_transientCancelHandlers.find(token);
        if (it == m_transientCancelHandlers.end())
            return false;
        auto cancel = std::move(it->second);
        m_transientCancelHandlers.erase(it);
        if (onTransientEnd)
            onTransientEnd(token);
        return cancel ? cancel() : true;
    }

    /// InxGUIRenderable override — wraps content in ImGui::Begin/End.
    void OnRender(InxGUIContext *ctx) override
    {
        if (!m_isOpen) {
            m_contentVisiblePreviousFrame = m_contentVisible;
            m_contentVisible = false;
            m_contentHovered = false;
            return;
        }

        PreRender(ctx);

        // Use ###id for stable ImGui window identity
        std::string label = m_title + "###" + m_windowId;

        bool requestedOpen = m_isOpen;
        bool visible = ImGui::Begin(label.c_str(), &requestedOpen, GetWindowFlags());
        const bool closeRequested = m_isOpen && !requestedOpen;
        m_contentVisiblePreviousFrame = m_contentVisible;
        const bool contentPresented = m_isOpen && ctx->IsCurrentWindowContentPresented();
        ctx->RecordSemanticWindow("editor_panel", m_title, m_windowId);
        const bool wasFocused = m_focusPublisher.IsFocused();
        const bool focused = ImGui::IsWindowFocused(ImGuiFocusedFlags_RootAndChildWindows);
        const bool pointerCapturedByPopup = ctx->IsPointerActivationBlockedByPopup();
        m_contentHovered = contentPresented && !pointerCapturedByPopup &&
                           ImGui::IsWindowHovered(ImGuiHoveredFlags_RootAndChildWindows);
        const bool pointerPressed = ImGui::IsMouseClicked(ImGuiMouseButton_Left) ||
                                    ImGui::IsMouseClicked(ImGuiMouseButton_Right) ||
                                    ImGui::IsMouseClicked(ImGuiMouseButton_Middle);
        const bool pointerActivatedNow =
            !pointerCapturedByPopup && ImGui::IsWindowHovered(ImGuiHoveredFlags_RootAndChildWindows) && pointerPressed;
        // The grace window belongs to this panel's pointer press, not to a
        // process-wide mouse edge. Updating every panel here caused hidden or
        // merely adjacent panels to publish fake user focus transitions.
        if (pointerActivatedNow)
            m_lastPointerPressAt = ImGui::GetTime();
        const bool pointerFocusEdge =
            focused && !wasFocused &&
            (pointerActivatedNow || ImGui::GetTime() - m_lastPointerPressAt <= kPointerFocusGraceSeconds);
        const bool revealedByPointer = pointerFocusEdge && !m_contentVisiblePreviousFrame;
        m_focusPublisher.Publish(focused, onPanelFocused, revealedByPointer, pointerFocusEdge);
        if (pointerFocusEdge)
            m_lastPointerPressAt = -1.0;
        // Publish focus while IsContentVisible() still represents the state
        // immediately before this ImGui frame. This gives interaction history
        // an exact distinction between revealing a hidden dock tab and merely
        // clicking a panel that was already presented.
        m_contentVisible = contentPresented;

        if (visible) {
            ctx->SetTransientInteractionBridge(
                [this](const std::string &token, const std::string &kind, int priority, std::function<bool()> cancel) {
                    BeginTransientInteraction(token, kind, priority, std::move(cancel));
                },
                [this](const std::string &token) { EndTransientInteraction(token); });
            VisiblePreRender(ctx);
            OnRenderContent(ctx);
            ctx->ClearTransientInteractionBridge();
        }

        ImGui::End();

        if (closeRequested) {
            if (onRequestClose)
                onRequestClose();
            // A panel without a WindowManager close bridge is not allowed to
            // retire itself. Keeping it open fails closed and prevents a
            // title-bar click from bypassing Document/Modal transactions.
        }

        PostRender(ctx);
        if (!m_isOpen)
            CancelAllTransientInteractions();
    }

  protected:
    bool ExecuteEditorCommand(const std::string &commandId, const std::string &source,
                              const std::string &argument = "") const
    {
        return executeCommand && executeCommand(commandId, source, argument);
    }

    bool CanExecuteEditorCommand(const std::string &commandId, const std::string &argument = "") const
    {
        return canExecuteCommand && canExecuteCommand(commandId, argument);
    }

    /// Override to draw the panel body.  Called between Begin/End.
    virtual void OnRenderContent(InxGUIContext *ctx) = 0;

    /// Override to supply custom ImGui window flags.
    virtual ImGuiWindowFlags GetWindowFlags() const
    {
        return ImGuiWindowFlags_None;
    }

    /// Override for per-frame work before window begins.
    virtual void PreRender(InxGUIContext * /*ctx*/)
    {
    }

    /// Override for preparation needed only while this panel's contents are visible.
    virtual void VisiblePreRender(InxGUIContext * /*ctx*/)
    {
    }

    /// Override for per-frame cleanup after window ends.
    /// Always called (even when the window is collapsed/hidden).
    virtual void PostRender(InxGUIContext * /*ctx*/)
    {
    }

    void BeginTransientInteraction(const std::string &token, const std::string &kind, int priority,
                                   std::function<bool()> cancel)
    {
        if (token.empty() || kind.empty() || !cancel)
            return;
        m_transientCancelHandlers[token] = std::move(cancel);
        if (onTransientBegin)
            onTransientBegin(token, kind, priority);
    }

    void EndTransientInteraction(const std::string &token)
    {
        auto it = m_transientCancelHandlers.find(token);
        if (it == m_transientCancelHandlers.end())
            return;
        m_transientCancelHandlers.erase(it);
        if (onTransientEnd)
            onTransientEnd(token);
    }

    void CancelAllTransientInteractions()
    {
        std::vector<std::string> tokens;
        tokens.reserve(m_transientCancelHandlers.size());
        for (const auto &[token, _] : m_transientCancelHandlers)
            tokens.push_back(token);
        for (const auto &token : tokens)
            CancelTransientInteraction(token);
    }

    std::string m_title;
    std::string m_windowId;
    bool m_isOpen = true;
    bool m_contentVisible = false;
    bool m_contentVisiblePreviousFrame = false;
    bool m_contentHovered = false;

  private:
    static constexpr double kPointerFocusGraceSeconds = 0.35;

    EditorPanelFocusPublisher m_focusPublisher;
    double m_lastPointerPressAt = -1.0;
    std::unordered_map<std::string, std::function<bool()>> m_transientCancelHandlers;
};

} // namespace infernux
