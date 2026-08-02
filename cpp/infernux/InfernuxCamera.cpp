/**
 * @file InfernuxCamera.cpp
 * @brief Infernux — Editor camera control methods
 *
 * Split from Infernux.cpp for maintainability.
 * Contains: GetEditorCamera, ProcessSceneViewInput.
 */

#include "Infernux.h"
#include <platform/input/InputManager.h>

namespace infernux
{

// Static input state tracking (simple solution without modifying header extensively)
static bool s_lastRightMouseDown = false;
static bool s_lastMiddleMouseDown = false;
static float s_lastMouseX = 0.0f;
static float s_lastMouseY = 0.0f;

// ----------------------------------
// Editor Camera Access
// ----------------------------------

EditorCameraController *Infernux::GetEditorCamera()
{
    if (m_isCleanedUp) {
        return nullptr;
    }
    return &SceneManager::Instance().GetEditorCameraController();
}

// ----------------------------------
// Scene Camera Control
// ----------------------------------

void Infernux::ProcessSceneViewInput(float deltaTime, bool rightMouseDown, bool middleMouseDown, float mouseDeltaX,
                                     float mouseDeltaY, float scrollDelta, bool keyW, bool keyA, bool keyS, bool keyD,
                                     bool keyQ, bool keyE, bool keyShift)
{
    if (m_isCleanedUp) {
        return;
    }

    EditorCameraController &controller = SceneManager::Instance().GetEditorCameraController();

    // Scene drag is consumed natively every engine frame while capture is
    // active. Python still calls this entry point for wheel input and for the
    // non-captured compatibility path, but must not apply a second 60 Hz drag.
    if (InputManager::Instance().IsEditorMouseCaptureActive()) {
        if (scrollDelta != 0.0f)
            controller.OnMouseScroll(scrollDelta);
        return;
    }

    // Handle mouse button state changes
    if (rightMouseDown) {
        if (!s_lastRightMouseDown) {
            controller.OnMouseButtonDown(1, 0, 0);
        }
    } else {
        if (s_lastRightMouseDown) {
            controller.OnMouseButtonUp(1, 0, 0);
        }
    }
    s_lastRightMouseDown = rightMouseDown;

    if (middleMouseDown) {
        if (!s_lastMiddleMouseDown) {
            controller.OnMouseButtonDown(2, 0, 0);
        }
    } else {
        if (s_lastMiddleMouseDown) {
            controller.OnMouseButtonUp(2, 0, 0);
        }
    }
    s_lastMiddleMouseDown = middleMouseDown;

    // Apply mouse movement - Python side already handles priority
    // Only apply if there's actual delta
    if (mouseDeltaX != 0.0f || mouseDeltaY != 0.0f) {
        if (rightMouseDown && !middleMouseDown) {
            // Rotate mode
            controller.ApplyRotation(mouseDeltaX, mouseDeltaY);
        } else if (middleMouseDown && !rightMouseDown) {
            // Pan mode
            controller.ApplyPan(mouseDeltaX, mouseDeltaY);
        } else if (rightMouseDown && middleMouseDown) {
            // Both pressed: prioritize rotation
            controller.ApplyRotation(mouseDeltaX, mouseDeltaY);
        }
    }

    // Handle scroll
    if (scrollDelta != 0) {
        controller.OnMouseScroll(scrollDelta);
    }

    // Debug: Log key states when right mouse is down
    // if (rightMouseDown && (keyW || keyA || keyS || keyD || keyQ || keyE)) {
    //     INXLOG_INFO("Keys: W=", keyW, " A=", keyA, " S=", keyS, " D=", keyD, " Q=", keyQ, " E=", keyE);
    // }

    // Handle key state
    if (keyW)
        controller.OnKeyDown(87);
    else
        controller.OnKeyUp(87);
    if (keyA)
        controller.OnKeyDown(65);
    else
        controller.OnKeyUp(65);
    if (keyS)
        controller.OnKeyDown(83);
    else
        controller.OnKeyUp(83);
    if (keyD)
        controller.OnKeyDown(68);
    else
        controller.OnKeyUp(68);
    if (keyQ)
        controller.OnKeyDown(81);
    else
        controller.OnKeyUp(81);
    if (keyE)
        controller.OnKeyDown(69);
    else
        controller.OnKeyUp(69);
    if (keyShift)
        controller.OnKeyDown(340);
    else
        controller.OnKeyUp(340);

    // Update camera controller (for fly mode)
    controller.Update(deltaTime);
}

} // namespace infernux
