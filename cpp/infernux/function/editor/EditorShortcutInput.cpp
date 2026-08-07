#include "EditorShortcutInput.h"

#include <function/renderer/gui/InxGUIContext.h>

#include <imgui.h>

namespace infernux
{

void EditorShortcutInput::OnRender(InxGUIContext *ctx)
{
    const int frame = ImGui::GetFrameCount();
    if (m_lastFrame == frame)
        return;
    m_lastFrame = frame;

    const auto isDown = [ctx](ImGuiKey key) { return ctx->IsKeyDown(static_cast<int>(key)); };
    const bool ctrl = isDown(ImGuiKey_LeftCtrl) || isDown(ImGuiKey_RightCtrl);
    const bool shift = isDown(ImGuiKey_LeftShift) || isDown(ImGuiKey_RightShift);
    const bool alt = isDown(ImGuiKey_LeftAlt) || isDown(ImGuiKey_RightAlt);
    const bool super = isDown(ImGuiKey_LeftSuper) || isDown(ImGuiKey_RightSuper);
    const auto pressedOnce = [](ImGuiKey key) { return ImGui::IsKeyPressed(key, false); };
    const bool textInputActive = ImGui::GetIO().WantTextInput;
    const bool popupActiveNow = ImGui::IsPopupOpen("", ImGuiPopupFlags_AnyPopupId);
    // ImGui may consume Escape and retire a popup before this late-frame input
    // adapter runs. Preserve the previous rendered frame as a capture barrier
    // so the same key edge cannot also execute a panel command.
    const bool popupActive = popupActiveNow || m_popupActivePreviousFrame;
    m_popupActivePreviousFrame = popupActiveNow;
    const auto dispatch = [&](const char *chord) {
        if (routeShortcut)
            routeShortcut(chord, textInputActive, popupActive);
    };

    if (ctrl && !alt && !super) {
        if (pressedOnce(ImGuiKey_S))
            dispatch(shift ? "Ctrl+Shift+S" : "Ctrl+S");
        if (pressedOnce(ImGuiKey_N))
            dispatch(shift ? "Ctrl+Shift+N" : "Ctrl+N");
        if (pressedOnce(ImGuiKey_Z))
            dispatch(shift ? "Ctrl+Shift+Z" : "Ctrl+Z");
        if (!shift && pressedOnce(ImGuiKey_Y))
            dispatch("Ctrl+Y");
        if (!shift && pressedOnce(ImGuiKey_C))
            dispatch("Ctrl+C");
        if (!shift && pressedOnce(ImGuiKey_X))
            dispatch("Ctrl+X");
        if (!shift && pressedOnce(ImGuiKey_V))
            dispatch("Ctrl+V");
        if (!shift && pressedOnce(ImGuiKey_D))
            dispatch("Ctrl+D");
        if (!shift && pressedOnce(ImGuiKey_F))
            dispatch("Ctrl+F");
        if (shift && pressedOnce(ImGuiKey_P))
            dispatch("Ctrl+Shift+P");
        return;
    }

    if (!ctrl && !shift && !alt && !super) {
        if (pressedOnce(ImGuiKey_F2))
            dispatch("F2");
        if (pressedOnce(ImGuiKey_Delete))
            dispatch("Delete");
        if (pressedOnce(ImGuiKey_Escape))
            dispatch("Escape");
        if (pressedOnce(ImGuiKey_Space))
            dispatch("Space");
        if (pressedOnce(ImGuiKey_Q))
            dispatch("Q");
        if (pressedOnce(ImGuiKey_W))
            dispatch("W");
        if (pressedOnce(ImGuiKey_E))
            dispatch("E");
        if (pressedOnce(ImGuiKey_R))
            dispatch("R");
        if (pressedOnce(ImGuiKey_LeftArrow))
            dispatch("Left");
        if (pressedOnce(ImGuiKey_RightArrow))
            dispatch("Right");
        if (pressedOnce(ImGuiKey_UpArrow))
            dispatch("Up");
        if (pressedOnce(ImGuiKey_DownArrow))
            dispatch("Down");
        if (pressedOnce(ImGuiKey_Enter) || pressedOnce(ImGuiKey_KeypadEnter))
            dispatch("Enter");
        return;
    }

    if (!ctrl && shift && !alt && !super) {
        if (pressedOnce(ImGuiKey_LeftArrow))
            dispatch("Shift+Left");
        if (pressedOnce(ImGuiKey_RightArrow))
            dispatch("Shift+Right");
        if (pressedOnce(ImGuiKey_UpArrow))
            dispatch("Shift+Up");
        if (pressedOnce(ImGuiKey_DownArrow))
            dispatch("Shift+Down");
    }
}

} // namespace infernux
