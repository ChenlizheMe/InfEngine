#pragma once

#include "EditorPanel.h"
#include "EditorTheme.h"

#include <functional>
#include <map>
#include <string>

namespace infernux
{

/// Play-mode state enum (mirrors Python PlayModeState).
enum class PlayState
{
    Edit = 0,
    Playing = 1,
    Paused = 2,
};

/// C++ native toolbar panel — Play/Pause/Stop + Gizmos/Camera dropdowns.
/// Dockable (inherits EditorPanel).
///
/// Play-mode operations are delegated to Python via std::function callbacks.
/// Camera settings read/write the C++ EditorCamera directly via callbacks.
class ToolbarPanel : public EditorPanel
{
  public:
    ToolbarPanel();
    ~ToolbarPanel() override = default;

    // ── Callbacks set from Python ────────────────────────────────────

    /// Unified command entry used by toolbar buttons.
    std::function<bool(const std::string &, const std::string &, const std::string &)> executeCommand;
    std::function<bool(const std::string &, const std::string &)> canExecuteCommand;

    /// Query current play state.
    std::function<PlayState()> getPlayState;

    /// Query total play time as string "MM:SS.mmm".
    std::function<std::string()> getPlayTimeStr;

    /// Query show-grid state.
    std::function<bool()> isShowGrid;

    /// Set show-grid state.
    std::function<void(bool)> setShowGrid;

    // ── Camera settings ──────────────────────────────────────────────

    struct CameraSettings
    {
        bool orthographic = false;
        float fov = 60.0f;
        float orthographicSize = 5.0f;
        float rotationSpeed = 0.15f;
        float panSpeed = 1.0f;
        float zoomSpeed = 1.0f;
        float moveSpeed = 5.0f;
        float moveSpeedBoost = 3.0f;
    };

    /// Get current camera settings (synced from engine).
    CameraSettings GetCameraSettings() const;

    /// Set camera settings (pushed to engine).
    void SetCameraSettings(const CameraSettings &settings);

    /// Callback to sync camera from engine (Python sets this).
    std::function<CameraSettings()> syncCameraFromEngine;

    /// Callback to apply camera to engine (Python sets this).
    std::function<void(const CameraSettings &)> applyCameraToEngine;

    // ── i18n callback ────────────────────────────────────────────────

    /// Translation function: key → translated text.
    std::function<std::string(const std::string &)> translate;

  protected:
    void OnRenderContent(InxGUIContext *ctx) override;
    ImGuiWindowFlags GetWindowFlags() const override;
    void PreRender(InxGUIContext *ctx) override;
    void PostRender(InxGUIContext *ctx) override;

  private:
    CameraSettings m_cameraSettings;

    void RenderPlayControls(InxGUIContext *ctx, float winW);
    void RenderRightDropdowns(InxGUIContext *ctx, float winW);
    void PopupGizmos(InxGUIContext *ctx);
    void PopupCamera(InxGUIContext *ctx);

    std::string T(const std::string &key) const;

    static constexpr float CAMERA_DEFAULTS_FOV = 60.0f;
    static constexpr float CAMERA_DEFAULTS_ORTHOGRAPHIC_SIZE = 5.0f;
    static constexpr float CAMERA_DEFAULTS_ROTATION = 0.15f;
    static constexpr float CAMERA_DEFAULTS_PAN = 1.0f;
    static constexpr float CAMERA_DEFAULTS_ZOOM = 1.0f;
    static constexpr float CAMERA_DEFAULTS_MOVE = 5.0f;
    static constexpr float CAMERA_DEFAULTS_BOOST = 3.0f;
};

} // namespace infernux
