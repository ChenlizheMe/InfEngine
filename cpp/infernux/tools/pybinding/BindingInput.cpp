/**
 * @file BindingInput.cpp
 * @brief Python bindings for InputManager — Unity-style input query API.
 *
 * Exposes the InputManager singleton so that the Python `Input` static class
 * can delegate to C++ for frame-accurate key/mouse/touch queries.
 *
 * Naming follows Unity conventions with Python snake_case:
 *   C++ GetKeyDown(scancode)  →  Python input_manager.get_key_down(scancode)
 */

#include <platform/input/InputManager.h>
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

namespace py = pybind11;

namespace infernux
{

void RegisterInputBindings(py::module_ &m)
{
    py::class_<TouchState>(m, "TouchState", "Immutable snapshot of one touch contact for the current frame")
        .def_readonly("touch_id", &TouchState::touchId)
        .def_readonly("finger_id", &TouchState::fingerId)
        .def_readonly("timestamp_ns", &TouchState::timestampNs)
        .def_readonly("window_id", &TouchState::windowId)
        .def_readonly("x", &TouchState::x, "Normalized horizontal position")
        .def_readonly("y", &TouchState::y, "Normalized vertical position")
        .def_readonly("delta_x", &TouchState::deltaX, "Normalized horizontal movement this frame")
        .def_readonly("delta_y", &TouchState::deltaY, "Normalized vertical movement this frame")
        .def_readonly("pressure", &TouchState::pressure)
        .def_readonly("contact_width", &TouchState::contactWidth, "Normalized contact width")
        .def_readonly("contact_height", &TouchState::contactHeight, "Normalized contact height")
        .def_readonly("is_primary", &TouchState::isPrimary, "True for the platform's primary contact")
        .def_readonly("cancel_reason", &TouchState::cancelReason, "Reason supplied for a canceled contact")
        .def_property_readonly("phase", [](const TouchState &touch) {
            switch (touch.phase) {
            case TouchPhase::Began:
                return "began";
            case TouchPhase::Moved:
                return "moved";
            case TouchPhase::Stationary:
                return "stationary";
            case TouchPhase::Ended:
                return "ended";
            case TouchPhase::Canceled:
                return "canceled";
            }
            return "canceled";
        });

    py::class_<ScreenState>(m, "ScreenState", "Current logical, framebuffer, safe-area, and focus snapshot")
        .def_readonly("revision", &ScreenState::revision)
        .def_readonly("logical_width", &ScreenState::logicalWidth)
        .def_readonly("logical_height", &ScreenState::logicalHeight)
        .def_readonly("framebuffer_width", &ScreenState::framebufferWidth)
        .def_readonly("framebuffer_height", &ScreenState::framebufferHeight)
        .def_readonly("pixel_ratio", &ScreenState::pixelRatio)
        .def_readonly("safe_area_x", &ScreenState::safeAreaX)
        .def_readonly("safe_area_y", &ScreenState::safeAreaY)
        .def_readonly("safe_area_width", &ScreenState::safeAreaWidth)
        .def_readonly("safe_area_height", &ScreenState::safeAreaHeight)
        .def_readonly("keyboard_inset", &ScreenState::keyboardInset)
        .def_readonly("keyboard_inset_known", &ScreenState::keyboardInsetKnown)
        .def_readonly("focused", &ScreenState::focused)
        .def_readonly("occluded", &ScreenState::occluded);

    py::class_<InputManager, std::unique_ptr<InputManager, py::nodelete>>(
        m, "InputManager", "Low-level input state manager. Use the Python `Input` class for the public API.")

        .def_static("instance", &InputManager::Instance, py::return_value_policy::reference,
                    "Get the singleton InputManager instance")
        .def_property_readonly("frame_index", &InputManager::GetFrameIndex,
                               "Monotonic input-frame identity for action sampling")

        // ---- Keyboard ----
        .def("get_key", &InputManager::GetKey, py::arg("scancode"), "True while the key (by SDL scancode) is held down")
        .def("get_key_down", &InputManager::GetKeyDown, py::arg("scancode"),
             "True during the frame the key was pressed")
        .def("get_key_up", &InputManager::GetKeyUp, py::arg("scancode"), "True during the frame the key was released")
        .def("any_key", &InputManager::AnyKey, "True if any key is currently held down")
        .def("any_key_down", &InputManager::AnyKeyDown, "True during the frame any key was first pressed")

        // ---- Mouse buttons ----
        .def("get_mouse_button", &InputManager::GetMouseButton, py::arg("button"),
             "True while mouse button is held (0=left, 1=right, 2=middle)")
        .def("get_mouse_button_down", &InputManager::GetMouseButtonDown, py::arg("button"),
             "True during the frame the mouse button was pressed")
        .def("get_mouse_button_up", &InputManager::GetMouseButtonUp, py::arg("button"),
             "True during the frame the mouse button was released")
        .def("get_mouse_frame_state", &InputManager::GetMouseFrameState, py::arg("button"),
             "Return (mouse_x, mouse_y, scroll_x, scroll_y, held, down, up) for one mouse button")

        // ---- Mouse position & delta ----
        .def_property_readonly("mouse_position_x", &InputManager::GetMousePositionX,
                               "Current mouse X (window-space pixels)")
        .def_property_readonly("mouse_position_y", &InputManager::GetMousePositionY,
                               "Current mouse Y (window-space pixels)")
        .def_property_readonly("mouse_delta_x", &InputManager::GetMouseDeltaX, "Mouse X movement this frame")
        .def_property_readonly("mouse_delta_y", &InputManager::GetMouseDeltaY, "Mouse Y movement this frame")

        // ---- Scroll wheel ----
        .def_property_readonly("mouse_scroll_delta_y", &InputManager::GetMouseScrollDeltaY,
                               "Vertical scroll delta (positive = up)")
        .def_property_readonly("mouse_scroll_delta_x", &InputManager::GetMouseScrollDeltaX, "Horizontal scroll delta")

        // ---- Text input ----
        .def_property_readonly("input_string", &InputManager::GetInputString, "Characters typed this frame (UTF-8)")
        .def("start_text_input", &InputManager::StartTextInput,
             "Begin committed platform text input and show a software keyboard when available")
        .def("stop_text_input", &InputManager::StopTextInput, "End platform text input")
        .def_property_readonly("is_text_input_active", &InputManager::IsTextInputActive,
                               "True while gameplay requested platform text input")

        // ---- Touch ----
        .def_property_readonly("touch_count", &InputManager::GetTouchCount, "Number of active touch contacts")
        .def("get_touch", &InputManager::GetTouch, py::arg("index"), py::return_value_policy::copy,
             "Return one touch from the current frame snapshot")
        .def("get_touches", &InputManager::GetTouches, py::return_value_policy::copy,
             "Return all touches in stable first-contact order")

        // ---- Screen / safe area ----
        .def_property_readonly("screen_state", &InputManager::GetScreenState, py::return_value_policy::copy,
                               "Current logical, framebuffer, safe-area, keyboard, and focus snapshot")

        // ---- File drop (OS drag-drop) ----
        .def("has_dropped_files", &InputManager::HasDroppedFiles,
             "True if files were dropped onto the window this frame")
        .def("get_dropped_files", &InputManager::GetDroppedFiles, "List of file paths dropped this frame")

        // ---- Cursor lock ----
        .def("set_cursor_locked", &InputManager::SetCursorLocked, py::arg("locked"),
             "Lock/unlock cursor (hides cursor and captures relative mouse movement)")
        .def_property_readonly("is_cursor_locked", &InputManager::IsCursorLocked,
                               "True when cursor is locked (relative mouse mode)")
        .def("set_editor_mouse_capture", &InputManager::SetEditorMouseCapture, py::arg("captured"),
             "Enable or disable editor-only Scene view mouse capture without marking gameplay cursor lock")
        .def("consume_editor_mouse_delta", &InputManager::ConsumeEditorMouseDelta,
             "Return and clear relative mouse movement accumulated since the last editor UI update")
        .def_property_readonly("is_editor_mouse_capture_active", &InputManager::IsEditorMouseCaptureActive,
                               "True when the Scene view editor camera is using relative mouse capture")
        .def_property_readonly("is_synthetic_input_frame", &InputManager::IsSyntheticInputFrame,
                               "True when trusted automation supplied input during the current frame")
        .def_property_readonly("has_synthetic_gameplay_input", &InputManager::HasSyntheticGameplayInput,
                               "True while trusted automation input is active without requiring window focus")

        // ---- Utility ----
        .def("reset_all", &InputManager::ResetAll, "Clear all input state (focus loss, scene change, etc.)")
        .def_static("name_to_scancode", &InputManager::NameToScancode, py::arg("name"),
                    "Map a key name (e.g. 'space', 'a', 'left shift') to SDL scancode. Returns -1 if unknown.")
        .def_static("scancode_to_name", &InputManager::ScancodeToName, py::arg("scancode"),
                    "Get the human-readable name for a scancode");
}

} // namespace infernux
