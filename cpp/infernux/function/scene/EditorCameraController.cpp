#include "EditorCameraController.h"
#include "GameObject.h"
#include <algorithm>
#include <cmath>
#include <glm/gtc/quaternion.hpp>

namespace infernux
{

void EditorCameraController::Update(float deltaTime)
{
    if (!m_camera)
        return;

    // Fly mode is only active when right mouse button is held
    if (m_rightMouseDown) {
        UpdateFlyMode(deltaTime);
    }
}

void EditorCameraController::OnMouseButtonDown(int button, float x, float y)
{
    m_lastMousePos = glm::vec2(x, y);

    // ImGui convention: Button 0 = left, 1 = right, 2 = middle
    if (button == 1) { // Right mouse button
        // Camera transforms can be restored or changed independently of this
        // controller. Start every look drag from the orientation that is
        // actually rendered so the first delta cannot snap to stale angles.
        SyncAnglesFromTransform();
        m_rightMouseDown = true;
    } else if (button == 2) { // Middle mouse button
        m_middleMouseDown = true;
    }
}

void EditorCameraController::OnMouseButtonUp(int button, float x, float y)
{
    if (button == 1) {
        m_rightMouseDown = false;
    } else if (button == 2) {
        m_middleMouseDown = false;
    }
}

void EditorCameraController::OnMouseMove(float x, float y)
{
    glm::vec2 currentPos(x, y);
    glm::vec2 delta = currentPos - m_lastMousePos;
    m_lastMousePos = currentPos;

    if (m_rightMouseDown) {
        // Rotate camera
        ApplyRotation(delta.x, delta.y);
    } else if (m_middleMouseDown) {
        // Pan camera
        ApplyPan(delta.x, delta.y);
    }
}

void EditorCameraController::OnMouseScroll(float delta)
{
    ApplyZoom(delta);
}

void EditorCameraController::OnKeyDown(int keyCode)
{
    // Handle key codes:
    // - ASCII: 'W'=87, 'w'=119, 'A'=65, 'a'=97, etc.
    // - SDL scancodes: W=26, A=4, S=22, D=7, Q=20, E=8
    // - ImGuiKey: LeftShift=340, RightShift=344
    switch (keyCode) {
    case 'W': // 87
    case 'w': // 119
    case 26:  // SDL scancode W
        m_keyW = true;
        break;
    case 'A': // 65
    case 'a': // 97
    case 4:   // SDL scancode A
        m_keyA = true;
        break;
    case 'S': // 83
    case 's': // 115
    case 22:  // SDL scancode S
        m_keyS = true;
        break;
    case 'D': // 68
    case 'd': // 100
    case 7:   // SDL scancode D
        m_keyD = true;
        break;
    case 'Q': // 81
    case 'q': // 113
    case 20:  // SDL scancode Q
        m_keyQ = true;
        break;
    case 'E': // 69
    case 'e': // 101
    case 8:   // SDL scancode E
        m_keyE = true;
        break;
    case 225: // SDL Left Shift
    case 229: // SDL Right Shift
    case 340: // ImGuiKey_LeftShift
    case 344: // ImGuiKey_RightShift
        m_keyShift = true;
        break;
    }
}

void EditorCameraController::OnKeyUp(int keyCode)
{
    switch (keyCode) {
    case 'W':
    case 'w':
    case 26:
        m_keyW = false;
        break;
    case 'A':
    case 'a':
    case 4:
        m_keyA = false;
        break;
    case 'S':
    case 's':
    case 22:
        m_keyS = false;
        break;
    case 'D':
    case 'd':
    case 7:
        m_keyD = false;
        break;
    case 'Q':
    case 'q':
    case 20:
        m_keyQ = false;
        break;
    case 'E':
    case 'e':
    case 8:
        m_keyE = false;
        break;
    case 225:
    case 229:
    case 340:
    case 344:
        m_keyShift = false;
        break;
    }
}

void EditorCameraController::FocusOn(const glm::vec3 &point, float distance)
{
    if (!m_camera || !m_camera->GetGameObject())
        return;

    m_focusPoint = point;
    m_focusDistance = distance;

    Transform *transform = m_camera->GetGameObject()->GetTransform();

    // Position camera at distance from focus point, looking at it
    glm::vec3 direction = glm::normalize(transform->GetPosition() - point);
    if (glm::length(direction) < 0.001f) {
        direction = glm::vec3(0.0f, 0.0f, 1.0f);
    }

    transform->SetPosition(point + direction * distance);
    transform->LookAt(point);

    SyncAnglesFromTransform();
}

void EditorCameraController::Reset()
{
    if (!m_camera || !m_camera->GetGameObject())
        return;

    Transform *transform = m_camera->GetGameObject()->GetTransform();
    transform->SetPosition(glm::vec3(0.0f, 2.0f, 10.0f));
    transform->LookAt(glm::vec3(0.0f, 0.0f, 0.0f));

    m_focusPoint = glm::vec3(0.0f);
    m_focusDistance = glm::length(m_focusPoint - transform->GetPosition());

    SyncAnglesFromTransform();
}

void EditorCameraController::SyncAnglesFromTransform()
{
    if (!m_camera || !m_camera->GetGameObject())
        return;

    const glm::vec3 forward = m_camera->GetGameObject()->GetTransform()->GetForward();
    m_yaw = glm::degrees(std::atan2(forward.x, forward.z));

    // Transform's Unity-style positive X rotation looks downward: forward.y
    // is therefore -sin(pitch), not sin(pitch).
    const float forwardY = std::clamp(forward.y, -1.0f, 1.0f);
    m_pitch = -glm::degrees(std::asin(forwardY));
}

void EditorCameraController::UpdateFlyMode(float deltaTime)
{
    if (!m_camera || !m_camera->GetGameObject())
        return;

    Transform *transform = m_camera->GetGameObject()->GetTransform();

    float speed = moveSpeed;
    if (m_keyShift) {
        speed *= moveSpeedBoost;
    }

    glm::vec3 movement(0.0f);

    if (m_keyW)
        movement += transform->GetForward();
    if (m_keyS)
        movement -= transform->GetForward();
    if (m_keyD)
        movement += transform->GetRight();
    if (m_keyA)
        movement -= transform->GetRight();
    if (m_keyE)
        movement += glm::vec3(0.0f, 1.0f, 0.0f); // World up
    if (m_keyQ)
        movement -= glm::vec3(0.0f, 1.0f, 0.0f); // World down

    if (glm::length(movement) > 0.001f) {
        movement = glm::normalize(movement) * speed * deltaTime;
        transform->Translate(movement);

        // Update focus point to maintain relative position
        m_focusPoint += movement;
    }
}

void EditorCameraController::ApplyRotation(float deltaX, float deltaY)
{
    if (!m_camera || !m_camera->GetGameObject())
        return;

    Transform *transform = m_camera->GetGameObject()->GetTransform();

    // Update yaw and pitch.
    m_yaw += deltaX * rotationSpeed;
    m_pitch += deltaY * rotationSpeed;

    // Clamp pitch to avoid gimbal lock
    m_pitch = std::clamp(m_pitch, -89.0f, 89.0f);

    // Normalize yaw
    while (m_yaw > 180.0f)
        m_yaw -= 360.0f;
    while (m_yaw < -180.0f)
        m_yaw += 360.0f;

    // Apply rotation using Euler angles (not quaternion!)
    transform->SetEulerAngles(m_pitch, m_yaw, 0.0f);

    // Update focus point to stay consistent with the new viewing direction
    // (FPS-style in-place rotation — focus moves to stay ahead of camera).
    m_focusPoint = transform->GetPosition() + transform->GetForward() * m_focusDistance;
}

void EditorCameraController::ApplyPan(float deltaX, float deltaY)
{
    if (!m_camera || !m_camera->GetGameObject())
        return;

    Transform *transform = m_camera->GetGameObject()->GetTransform();

    glm::vec3 right = transform->GetRight();
    glm::vec3 up = transform->GetUp();

    const float viewportWidth = std::max(1.0f, static_cast<float>(m_camera->GetPixelWidth()));
    const float viewportHeight = std::max(1.0f, static_cast<float>(m_camera->GetPixelHeight()));

    float worldUnitsPerPixelX = 0.0f;
    float worldUnitsPerPixelY = 0.0f;

    if (m_camera->GetProjectionMode() == CameraProjection::Orthographic) {
        const float halfHeight = std::max(0.001f, m_camera->GetOrthographicSize());
        const float halfWidth = halfHeight * std::max(0.01f, m_camera->GetAspectRatio());
        worldUnitsPerPixelX = (halfWidth * 2.0f) / viewportWidth;
        worldUnitsPerPixelY = (halfHeight * 2.0f) / viewportHeight;
    } else {
        const float focusDistance = std::max(0.01f, m_focusDistance);
        const float halfHeight = std::tan(glm::radians(m_camera->GetFieldOfView()) * 0.5f) * focusDistance;
        const float halfWidth = halfHeight * std::max(0.01f, m_camera->GetAspectRatio());
        worldUnitsPerPixelX = (halfWidth * 2.0f) / viewportWidth;
        worldUnitsPerPixelY = (halfHeight * 2.0f) / viewportHeight;
    }

    glm::vec3 pan = ((-right * deltaX * worldUnitsPerPixelX) + (up * deltaY * worldUnitsPerPixelY)) * panSpeed;
    transform->Translate(pan);

    // Also move focus point
    m_focusPoint += pan;
}

void EditorCameraController::ApplyZoom(float delta)
{
    if (!m_camera || !m_camera->GetGameObject())
        return;

    Transform *transform = m_camera->GetGameObject()->GetTransform();

    // Move camera forward/backward
    glm::vec3 forward = transform->GetForward();
    glm::vec3 movement = forward * delta * zoomSpeed;

    transform->Translate(movement);

    // Update focus distance
    m_focusDistance = glm::length(m_focusPoint - transform->GetPosition());
}

void EditorCameraController::RestoreState(const glm::vec3 &position, const glm::vec3 &focusPoint, float focusDistance,
                                          float yaw, float pitch)
{
    if (!m_camera || !m_camera->GetGameObject())
        return;

    m_focusPoint = focusPoint;
    m_focusDistance = focusDistance;
    m_yaw = yaw;
    m_pitch = std::clamp(pitch, -89.0f, 89.0f);

    Transform *transform = m_camera->GetGameObject()->GetTransform();
    transform->SetPosition(position);
    transform->SetEulerAngles(m_pitch, m_yaw, 0.0f);
}

} // namespace infernux
