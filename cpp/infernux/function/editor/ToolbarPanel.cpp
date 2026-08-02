#include "ToolbarPanel.h"
#include <function/renderer/gui/InxGUISemantics.h>

#include <algorithm>
#include <cstdio>
#include <iterator>

namespace infernux
{

// ════════════════════════════════════════════════════════════════════
// Construction
// ════════════════════════════════════════════════════════════════════

ToolbarPanel::ToolbarPanel() : EditorPanel("Toolbar", "toolbar")
{
}

// ════════════════════════════════════════════════════════════════════
// Camera settings
// ════════════════════════════════════════════════════════════════════

ToolbarPanel::CameraSettings ToolbarPanel::GetCameraSettings() const
{
    return m_cameraSettings;
}

void ToolbarPanel::SetCameraSettings(const CameraSettings &settings)
{
    m_cameraSettings = settings;
    if (applyCameraToEngine)
        applyCameraToEngine(m_cameraSettings);
}

// ════════════════════════════════════════════════════════════════════
// Translation helper
// ════════════════════════════════════════════════════════════════════

std::string ToolbarPanel::T(const std::string &key) const
{
    if (translate)
        return translate(key);
    // Fallback: return the key suffix after the last dot
    auto dot = key.rfind('.');
    return (dot != std::string::npos) ? key.substr(dot + 1) : key;
}

// ════════════════════════════════════════════════════════════════════
// Window flags & pre-render
// ════════════════════════════════════════════════════════════════════

ImGuiWindowFlags ToolbarPanel::GetWindowFlags() const
{
    return ImGuiWindowFlags_NoScrollbar | ImGuiWindowFlags_NoScrollWithMouse;
}

void ToolbarPanel::PreRender(InxGUIContext * /*ctx*/)
{
    // Push toolbar spacing vars (5 vars)
    ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, EditorTheme::TOOLBAR_WIN_PAD);
    ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, EditorTheme::TOOLBAR_FRAME_PAD);
    ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, EditorTheme::TOOLBAR_ITEM_SPC);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameRounding, EditorTheme::TOOLBAR_FRAME_RND);
    ImGui::PushStyleVar(ImGuiStyleVar_FrameBorderSize, EditorTheme::TOOLBAR_FRAME_BRD);
}

// ════════════════════════════════════════════════════════════════════
// Main content
// ════════════════════════════════════════════════════════════════════

void ToolbarPanel::OnRenderContent(InxGUIContext *ctx)
{
    float winW = ctx->GetWindowWidth();
    RenderPlayControls(ctx, winW);
    RenderRightDropdowns(ctx, winW);
}

void ToolbarPanel::PostRender(InxGUIContext * /*ctx*/)
{
    // Pop the 5 style vars pushed in PreRender (always, even when collapsed)
    ImGui::PopStyleVar(5);
}

// ════════════════════════════════════════════════════════════════════
// Play controls (centered)
// ════════════════════════════════════════════════════════════════════

void ToolbarPanel::RenderPlayControls(InxGUIContext *ctx, float winW)
{
    const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
    PlayState state = getPlayState ? getPlayState() : PlayState::Edit;
    bool isPlaying = (state == PlayState::Playing || state == PlayState::Paused);
    bool isPaused = (state == PlayState::Paused);
    const bool canTogglePlay = canExecuteCommand && canExecuteCommand("play.toggle", "");
    const bool canTogglePause = canExecuteCommand && canExecuteCommand("play.pause", "");
    const bool canStep = canExecuteCommand && canExecuteCommand("play.step", "");

    float btnW = 160.0f;
    float cx = (winW - btnW) * 0.5f;
    if (cx < 6.0f)
        cx = 6.0f;
    ImGui::SetCursorPosX(cx);

    // ── Play / Stop ──────────────────────────────────────────────
    if (isPlaying && !isPaused)
        EditorTheme::PushFlatButtonStyle(EditorTheme::PLAY_ACTIVE);
    else
        EditorTheme::PushFlatButtonStyle(EditorTheme::BTN_IDLE);

    std::string playLabel = isPlaying ? T("toolbar.stop") : T("toolbar.play");
    const bool playClicked = ImGui::Button(playLabel.c_str());
    if (captureSemantics)
        ctx->RecordSemanticItem("toolbar_play_stop", playLabel, canTogglePlay, "toolbar.play_stop");
    if (playClicked && canTogglePlay && executeCommand)
        executeCommand("play.toggle", "toolbar", "");
    ImGui::PopStyleColor(3);

    ImGui::SameLine(0.0f, 2.0f);

    // ── Pause / Resume ───────────────────────────────────────────
    if (!isPlaying)
        EditorTheme::PushFlatButtonStyle(EditorTheme::BTN_DISABLED);
    else if (isPaused)
        EditorTheme::PushFlatButtonStyle(EditorTheme::PAUSE_ACTIVE);
    else
        EditorTheme::PushFlatButtonStyle(EditorTheme::BTN_IDLE);

    std::string pauseLabel = isPaused ? T("toolbar.resume") : T("toolbar.pause");
    const bool pauseClicked = ImGui::Button(pauseLabel.c_str());
    if (captureSemantics)
        ctx->RecordSemanticItem("toolbar_pause_resume", pauseLabel, canTogglePause, "toolbar.pause_resume");
    if (pauseClicked && canTogglePause && executeCommand)
        executeCommand("play.pause", "toolbar", "");
    ImGui::PopStyleColor(3);

    ImGui::SameLine(0.0f, 2.0f);

    // ── Step ─────────────────────────────────────────────────────
    if (isPaused)
        EditorTheme::PushFlatButtonStyle(EditorTheme::BTN_IDLE);
    else
        EditorTheme::PushFlatButtonStyle(EditorTheme::BTN_DISABLED);

    std::string stepLabel = T("toolbar.step");
    const bool stepClicked = ImGui::Button(stepLabel.c_str());
    if (captureSemantics)
        ctx->RecordSemanticItem("toolbar_step", stepLabel, canStep, "toolbar.step");
    if (stepClicked && canStep && executeCommand)
        executeCommand("play.step", "toolbar", "");
    ImGui::PopStyleColor(3);

    // ── Time label while playing ─────────────────────────────────
    if (isPlaying) {
        ImGui::SameLine(0.0f, 8.0f);
        std::string tag = isPaused ? T("toolbar.status_paused") : T("toolbar.status_playing");
        std::string timeStr = getPlayTimeStr ? getPlayTimeStr() : "00:00.000";
        ImGui::TextUnformatted((tag + "  " + timeStr).c_str());
    }
}

// ════════════════════════════════════════════════════════════════════
// Right-aligned dropdowns
// ════════════════════════════════════════════════════════════════════

void ToolbarPanel::RenderRightDropdowns(InxGUIContext *ctx, float winW)
{
    const bool captureSemantics = InxGUISemantics::IsCaptureEnabled();
    float rightX = winW - 200.0f;
    if (rightX < 300.0f)
        rightX = 300.0f;

    ImGui::SameLine(rightX);

    // Gizmos dropdown
    EditorTheme::PushGhostButtonStyle();
    std::string gizLabel = T("toolbar.gizmos");
    const bool gizmosClicked = ImGui::Button(gizLabel.c_str());
    if (captureSemantics)
        ctx->RecordSemanticItem("toolbar_gizmos", gizLabel, true, "toolbar.gizmos");
    if (gizmosClicked)
        ImGui::OpenPopup("##giz");
    ImGui::PopStyleColor(3);

    if (ImGui::BeginPopup("##giz")) {
        if (captureSemantics)
            ctx->RecordSemanticWindow("toolbar_popup", gizLabel, "toolbar.gizmos.popup");
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, EditorTheme::POPUP_WIN_PAD);
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, EditorTheme::POPUP_ITEM_SPC);
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, EditorTheme::POPUP_FRAME_PAD);
        PopupGizmos(ctx);
        ImGui::PopStyleVar(3);
        ImGui::EndPopup();
    }

    ImGui::SameLine(0.0f, 4.0f);

    // Camera dropdown
    EditorTheme::PushGhostButtonStyle();
    std::string camLabel = T("toolbar.camera");
    const bool cameraClicked = ImGui::Button(camLabel.c_str());
    if (captureSemantics)
        ctx->RecordSemanticItem("toolbar_camera", camLabel, true, "toolbar.camera");
    if (cameraClicked)
        ImGui::OpenPopup("##cam");
    ImGui::PopStyleColor(3);

    if (ImGui::BeginPopup("##cam")) {
        if (captureSemantics)
            ctx->RecordSemanticWindow("toolbar_popup", camLabel, "toolbar.camera.popup");
        ImGui::PushStyleVar(ImGuiStyleVar_WindowPadding, EditorTheme::POPUP_WIN_PAD);
        ImGui::PushStyleVar(ImGuiStyleVar_ItemSpacing, EditorTheme::POPUP_ITEM_SPC);
        ImGui::PushStyleVar(ImGuiStyleVar_FramePadding, EditorTheme::POPUP_FRAME_PAD);
        PopupCamera(ctx);
        ImGui::PopStyleVar(3);
        ImGui::EndPopup();
    }
}

// ════════════════════════════════════════════════════════════════════
// Popup: Gizmos
// ════════════════════════════════════════════════════════════════════

void ToolbarPanel::PopupGizmos(InxGUIContext *ctx)
{
    if (!isShowGrid) {
        ImGui::TextUnformatted(T("toolbar.engine_not_available").c_str());
        return;
    }

    ImGui::Dummy(ImVec2(200.0f, 0.0f)); // minimum popup width
    ImGui::TextUnformatted(T("toolbar.gizmos_header").c_str());
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0.0f, 4.0f));

    bool grid = isShowGrid();
    const std::string gridLabel = T("toolbar.show_grid");
    const bool gridChanged = ImGui::Checkbox(gridLabel.c_str(), &grid);
    if (InxGUISemantics::IsCaptureEnabled())
        ctx->RecordSemanticItem("toolbar_show_grid", gridLabel, true, "toolbar.gizmos.show_grid");
    if (gridChanged) {
        if (setShowGrid)
            setShowGrid(grid);
    }
    ImGui::Dummy(ImVec2(0.0f, 4.0f));
}

// ════════════════════════════════════════════════════════════════════
// Popup: Camera
// ════════════════════════════════════════════════════════════════════

void ToolbarPanel::PopupCamera(InxGUIContext *ctx)
{
    // Sync from engine
    if (syncCameraFromEngine)
        m_cameraSettings = syncCameraFromEngine();

    ImGui::Dummy(ImVec2(360.0f, 0.0f)); // minimum popup width
    ImGui::TextUnformatted(T("toolbar.scene_camera").c_str());
    ImGui::Separator();
    ImGui::Dummy(ImVec2(0.0f, 4.0f));

    ImGui::TextUnformatted(T("toolbar.projection_mode").c_str());
    ImGui::SameLine(145.0f);
    ImGui::SetNextItemWidth(200.0f);
    int projection = m_cameraSettings.orthographic ? 1 : 0;
    const std::string perspective = T("toolbar.perspective");
    const std::string orthographic = T("toolbar.orthographic");
    const char *projectionItems[] = {perspective.c_str(), orthographic.c_str()};
    bool changed = ImGui::Combo("##camera_projection", &projection, projectionItems, 2);
    m_cameraSettings.orthographic = projection == 1;
    ImGui::Dummy(ImVec2(0.0f, 4.0f));

    struct CamParam
    {
        const char *key;
        float *value;
        float mn, mx, step, stepFast;
        const char *headerKey; // null if no header
    };

    CamParam perspectiveParams[] = {
        {"toolbar.field_of_view", &m_cameraSettings.fov, 10.0f, 120.0f, 1.0f, 10.0f, nullptr},
    };
    CamParam orthographicParams[] = {
        {"toolbar.orthographic_size", &m_cameraSettings.orthographicSize, 0.01f, 1000.0f, 0.1f, 1.0f, nullptr},
    };
    CamParam navigationParams[] = {
        {"toolbar.rotation_sensitivity", &m_cameraSettings.rotationSpeed, 0.005f, 1.0f, 0.005f, 0.05f,
         "toolbar.navigation_header"},
        {"toolbar.pan_speed", &m_cameraSettings.panSpeed, 0.1f, 10.0f, 0.1f, 1.0f, nullptr},
        {"toolbar.zoom_speed", &m_cameraSettings.zoomSpeed, 0.1f, 10.0f, 0.1f, 1.0f, nullptr},
        {"toolbar.move_speed", &m_cameraSettings.moveSpeed, 0.1f, 50.0f, 0.1f, 1.0f, nullptr},
        {"toolbar.speed_boost", &m_cameraSettings.moveSpeedBoost, 1.0f, 20.0f, 0.1f, 1.0f, nullptr},
    };

    auto renderParams = [&](CamParam *params, size_t count) {
        for (size_t index = 0; index < count; ++index) {
            auto &p = params[index];
            if (p.headerKey) {
                ImGui::TextUnformatted(T(p.headerKey).c_str());
                ImGui::Separator();
                ImGui::Dummy(ImVec2(0.0f, 4.0f));
            }

            ImGui::TextUnformatted(T(p.key).c_str());
            ImGui::SameLine(145.0f);

            char sliderId[64];
            snprintf(sliderId, sizeof(sliderId), "##%s_slider", p.key);
            ImGui::SetNextItemWidth(120.0f);
            float prev = *p.value;
            ImGui::SliderFloat(sliderId, p.value, p.mn, p.mx);

            ImGui::SameLine(0.0f, 6.0f);

            char inputId[64];
            snprintf(inputId, sizeof(inputId), "##%s_input", p.key);
            ImGui::SetNextItemWidth(72.0f);
            ImGui::InputFloat(inputId, p.value, p.step, p.stepFast, "%.3f");

            // Clamp
            *p.value = (std::min)((std::max)(*p.value, p.mn), p.mx);

            if (*p.value != prev)
                changed = true;

            ImGui::Dummy(ImVec2(0.0f, 4.0f));
        }
    };

    if (m_cameraSettings.orthographic)
        renderParams(orthographicParams, std::size(orthographicParams));
    else
        renderParams(perspectiveParams, std::size(perspectiveParams));
    renderParams(navigationParams, std::size(navigationParams));

    // Reset button
    ImGui::Dummy(ImVec2(0.0f, 2.0f));
    if (ImGui::Button(T("toolbar.reset_camera_settings").c_str(), ImVec2(-1.0f, 0.0f))) {
        m_cameraSettings.fov = CAMERA_DEFAULTS_FOV;
        m_cameraSettings.orthographic = false;
        m_cameraSettings.orthographicSize = CAMERA_DEFAULTS_ORTHOGRAPHIC_SIZE;
        m_cameraSettings.rotationSpeed = CAMERA_DEFAULTS_ROTATION;
        m_cameraSettings.panSpeed = CAMERA_DEFAULTS_PAN;
        m_cameraSettings.zoomSpeed = CAMERA_DEFAULTS_ZOOM;
        m_cameraSettings.moveSpeed = CAMERA_DEFAULTS_MOVE;
        m_cameraSettings.moveSpeedBoost = CAMERA_DEFAULTS_BOOST;
        changed = true;
    }
    ImGui::Dummy(ImVec2(0.0f, 4.0f));

    if (changed && applyCameraToEngine)
        applyCameraToEngine(m_cameraSettings);
}

} // namespace infernux
