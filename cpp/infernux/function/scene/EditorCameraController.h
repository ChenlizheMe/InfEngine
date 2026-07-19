#pragma once

#include "Camera.h"
#include <glm/glm.hpp>

namespace infernux
{

/**
 * @brief Editor camera controller with Unity-style controls.
 *
 * Controls:
 * - Right-click + drag: Rotate camera (look around)
 * - Middle-click + drag: Pan camera
 * - Scroll wheel: Zoom in/out
 * - Right-click + WASD: Fly mode movement
 * - Right-click + QE: Up/Down in fly mode
 * - Shift: Speed boost in fly mode
 *
 * This is NOT a Component - it's a controller that manipulates a Camera.
 */
class EditorCameraController
{
  public:
    EditorCameraController() = default;
    ~EditorCameraController() = default;

    /// @brief Set the camera to control
    void SetCamera(Camera *camera)
    {
        m_camera = camera;
    }
    [[nodiscard]] Camera *GetCamera() const
    {
        return m_camera;
    }

    // ========================================================================
    // Input handling - call these from input system
    // ========================================================================

    /// @brief Call every frame with delta time
    void Update(float deltaTime);

    /// @brief Mouse button pressed
    void OnMouseButtonDown(int button, float x, float y);

    /// @brief Mouse button released
    void OnMouseButtonUp(int button, float x, float y);

    /// @brief Mouse moved
    void OnMouseMove(float x, float y);

    /// @brief Mouse scroll
    void OnMouseScroll(float delta);

    /// @brief Key pressed
    void OnKeyDown(int keyCode);

    /// @brief Key released
    void OnKeyUp(int keyCode);

    // ========================================================================
    // Settings
    // ========================================================================

    float rotationSpeed = 0.05f; // Degrees per pixel
    float panSpeed = 1.0f;       // Unity-style pan multiplier
    float zoomSpeed = 1.0f;      // Units per scroll step
    float moveSpeed = 5.0f;      // Units per second
    float moveSpeedBoost = 3.0f; // Speed multiplier when Shift held

    // Focus/orbit point for orbit mode
    [[nodiscard]] glm::vec3 GetFocusPoint() const
    {
        return m_focusPoint;
    }
    void SetFocusPoint(const glm::vec3 &point)
    {
        m_focusPoint = point;
    }

    /// @brief Move camera to look at a point from current direction
    void FocusOn(const glm::vec3 &point, float distance = 10.0f);

    /// @brief Reset to default position
    void Reset();

    // Direct control methods (for immediate response input)

    /// @brief Apply rotation delta (modifies pitch/yaw)
    /// @param deltaX horizontal delta (changes yaw)
    /// @param deltaY vertical delta (changes pitch)
    void ApplyRotation(float deltaX, float deltaY);

    /// @brief Apply pan delta (modifies position relative to view plane)
    void ApplyPan(float deltaX, float deltaY);

    /// @brief Apply zoom delta (modifies position along forward vector)
    void ApplyZoom(float delta);

  private:
    void UpdateFlyMode(float deltaTime);
    void SyncAnglesFromTransform();

    Camera *m_camera = nullptr;

    // Mouse state
    glm::vec2 m_lastMousePos{0.0f};
    bool m_rightMouseDown = false;
    bool m_middleMouseDown = false;

    // Key state for fly mode
    bool m_keyW = false, m_keyA = false, m_keyS = false, m_keyD = false;
    bool m_keyQ = false, m_keyE = false;
    bool m_keyShift = false;

    // Focus point for orbit/pan operations
    glm::vec3 m_focusPoint{0.0f};
    float m_focusDistance = 10.0f;

    // Accumulated rotation (for clamping pitch)
    float m_yaw = 0.0f;   // Horizontal rotation
    float m_pitch = 0.0f; // Vertical rotation (clamped to avoid gimbal lock)

  public:
    /// @brief Get current yaw (horizontal rotation in degrees)
    [[nodiscard]] float GetYaw() const
    {
        return m_yaw;
    }

    /// @brief Get current pitch (vertical rotation in degrees)
    [[nodiscard]] float GetPitch() const
    {
        return m_pitch;
    }

    /// @brief Get focus distance (for camera state persistence)
    [[nodiscard]] float GetFocusDistance() const
    {
        return m_focusDistance;
    }

    /// @brief Restore full camera state (position, focus point, yaw/pitch)
    void RestoreState(const glm::vec3 &position, const glm::vec3 &focusPoint, float focusDistance, float yaw,
                      float pitch);
};

} // namespace infernux
