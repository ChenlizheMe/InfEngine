/**
 * @file InputManager.cpp
 * @brief Implementation of the unified input state manager.
 *
 * Uses SDL3 event types:
 *   SDL_EVENT_KEY_DOWN / SDL_EVENT_KEY_UP
 *   SDL_EVENT_MOUSE_BUTTON_DOWN / SDL_EVENT_MOUSE_BUTTON_UP
 *   SDL_EVENT_MOUSE_MOTION
 *   SDL_EVENT_MOUSE_WHEEL
 *   SDL_EVENT_TEXT_INPUT
 *   SDL_EVENT_FINGER_DOWN / SDL_EVENT_FINGER_MOTION /
 *   SDL_EVENT_FINGER_UP / SDL_EVENT_FINGER_CANCELED
 *
 * SDL_EVENT_WINDOW_FOCUS_LOST
 */

#include "InputManager.h"

#include <SDL3/SDL.h>

#include <algorithm>
#include <cctype>
#include <cmath>
#include <core/log/InxLog.h>
#include <cstdlib>
#include <cstring>
#include <stdexcept>

namespace infernux
{

// ============================================================================
// Static name table
// ============================================================================

std::unordered_map<std::string, int> InputManager::s_nameToScancode;
bool InputManager::s_nameTableBuilt = false;

void InputManager::BuildNameTable()
{
    if (s_nameTableBuilt)
        return;
    s_nameTableBuilt = true;

    // Letters a-z
    for (int i = 0; i < 26; ++i) {
        std::string lower(1, static_cast<char>('a' + i));
        s_nameToScancode[lower] = SDL_SCANCODE_A + i;
    }

    // Digits 0-9
    s_nameToScancode["0"] = SDL_SCANCODE_0;
    for (int i = 1; i <= 9; ++i) {
        s_nameToScancode[std::to_string(i)] = SDL_SCANCODE_1 + (i - 1);
    }

    // Alpha aliases (Unity KeyCode style, lowercase)
    for (int i = 0; i <= 9; ++i) {
        s_nameToScancode["alpha" + std::to_string(i)] = (i == 0) ? SDL_SCANCODE_0 : SDL_SCANCODE_1 + (i - 1);
    }

    // Function keys F1-F12
    for (int i = 1; i <= 12; ++i) {
        s_nameToScancode["f" + std::to_string(i)] = SDL_SCANCODE_F1 + (i - 1);
    }

    // Arrow keys
    s_nameToScancode["up"] = SDL_SCANCODE_UP;
    s_nameToScancode["down"] = SDL_SCANCODE_DOWN;
    s_nameToScancode["left"] = SDL_SCANCODE_LEFT;
    s_nameToScancode["right"] = SDL_SCANCODE_RIGHT;
    s_nameToScancode["up_arrow"] = SDL_SCANCODE_UP;
    s_nameToScancode["down_arrow"] = SDL_SCANCODE_DOWN;
    s_nameToScancode["left_arrow"] = SDL_SCANCODE_LEFT;
    s_nameToScancode["right_arrow"] = SDL_SCANCODE_RIGHT;

    // Modifiers
    s_nameToScancode["left_shift"] = SDL_SCANCODE_LSHIFT;
    s_nameToScancode["right_shift"] = SDL_SCANCODE_RSHIFT;
    s_nameToScancode["left_control"] = SDL_SCANCODE_LCTRL;
    s_nameToScancode["right_control"] = SDL_SCANCODE_RCTRL;
    s_nameToScancode["left_ctrl"] = SDL_SCANCODE_LCTRL;
    s_nameToScancode["right_ctrl"] = SDL_SCANCODE_RCTRL;
    s_nameToScancode["left_alt"] = SDL_SCANCODE_LALT;
    s_nameToScancode["right_alt"] = SDL_SCANCODE_RALT;
    s_nameToScancode["left_command"] = SDL_SCANCODE_LGUI;
    s_nameToScancode["right_command"] = SDL_SCANCODE_RGUI;
    s_nameToScancode["left_super"] = SDL_SCANCODE_LGUI;
    s_nameToScancode["right_super"] = SDL_SCANCODE_RGUI;

    // Common keys
    s_nameToScancode["space"] = SDL_SCANCODE_SPACE;
    s_nameToScancode["return"] = SDL_SCANCODE_RETURN;
    s_nameToScancode["enter"] = SDL_SCANCODE_RETURN;
    s_nameToScancode["escape"] = SDL_SCANCODE_ESCAPE;
    s_nameToScancode["backspace"] = SDL_SCANCODE_BACKSPACE;
    s_nameToScancode["tab"] = SDL_SCANCODE_TAB;
    s_nameToScancode["delete"] = SDL_SCANCODE_DELETE;
    s_nameToScancode["insert"] = SDL_SCANCODE_INSERT;
    s_nameToScancode["home"] = SDL_SCANCODE_HOME;
    s_nameToScancode["end"] = SDL_SCANCODE_END;
    s_nameToScancode["page_up"] = SDL_SCANCODE_PAGEUP;
    s_nameToScancode["page_down"] = SDL_SCANCODE_PAGEDOWN;
    s_nameToScancode["caps_lock"] = SDL_SCANCODE_CAPSLOCK;
    s_nameToScancode["num_lock"] = SDL_SCANCODE_NUMLOCKCLEAR;
    s_nameToScancode["scroll_lock"] = SDL_SCANCODE_SCROLLLOCK;
    s_nameToScancode["print_screen"] = SDL_SCANCODE_PRINTSCREEN;
    s_nameToScancode["pause"] = SDL_SCANCODE_PAUSE;

    // Punctuation / symbols
    s_nameToScancode["minus"] = SDL_SCANCODE_MINUS;
    s_nameToScancode["equals"] = SDL_SCANCODE_EQUALS;
    s_nameToScancode["left_bracket"] = SDL_SCANCODE_LEFTBRACKET;
    s_nameToScancode["right_bracket"] = SDL_SCANCODE_RIGHTBRACKET;
    s_nameToScancode["backslash"] = SDL_SCANCODE_BACKSLASH;
    s_nameToScancode["semicolon"] = SDL_SCANCODE_SEMICOLON;
    s_nameToScancode["quote"] = SDL_SCANCODE_APOSTROPHE;
    s_nameToScancode["backquote"] = SDL_SCANCODE_GRAVE;
    s_nameToScancode["comma"] = SDL_SCANCODE_COMMA;
    s_nameToScancode["period"] = SDL_SCANCODE_PERIOD;
    s_nameToScancode["slash"] = SDL_SCANCODE_SLASH;

    // Numpad
    for (int i = 0; i <= 9; ++i) {
        s_nameToScancode["keypad_" + std::to_string(i)] = SDL_SCANCODE_KP_0 + i;
    }
    s_nameToScancode["keypad_plus"] = SDL_SCANCODE_KP_PLUS;
    s_nameToScancode["keypad_minus"] = SDL_SCANCODE_KP_MINUS;
    s_nameToScancode["keypad_multiply"] = SDL_SCANCODE_KP_MULTIPLY;
    s_nameToScancode["keypad_divide"] = SDL_SCANCODE_KP_DIVIDE;
    s_nameToScancode["keypad_enter"] = SDL_SCANCODE_KP_ENTER;
    s_nameToScancode["keypad_period"] = SDL_SCANCODE_KP_PERIOD;
}

// ============================================================================
// Singleton
// ============================================================================

InputManager &InputManager::Instance()
{
    static InputManager instance;
    return instance;
}

InputManager::InputManager()
{
    m_keys.fill(0);
    m_keyDown.fill(0);
    m_keyUp.fill(0);
    m_mouseButtons.fill(0);
    m_mouseButtonDown.fill(0);
    m_mouseButtonUp.fill(0);
    BuildNameTable();
}

// ============================================================================
// Frame lifecycle
// ============================================================================

void InputManager::BeginFrame()
{
    ++m_frameIndex;
    m_keyDown.fill(0);
    m_keyUp.fill(0);
    m_mouseButtonDown.fill(0);
    m_mouseButtonUp.fill(0);

    // Clear per-frame deltas
    m_mouseDX = 0.f;
    m_mouseDY = 0.f;
    m_scrollX = 0.f;
    m_scrollY = 0.f;
    m_inputString.clear();
    m_accelerationEvents.clear();
    m_touches.erase(std::remove_if(m_touches.begin(), m_touches.end(),
                                   [](const TouchState &touch) {
                                       return touch.phase == TouchPhase::Ended || touch.phase == TouchPhase::Canceled;
                                   }),
                    m_touches.end());
    for (TouchState &touch : m_touches) {
        touch.phase = TouchPhase::Stationary;
        touch.deltaX = 0.0f;
        touch.deltaY = 0.0f;
    }
    m_droppedFiles.clear();
    m_hasSyntheticMousePositionThisFrame = false;
    m_syntheticInputThisFrame = false;

    // Re-apply relative mouse mode for the current window/focus state without
    // disturbing persistent capture flags. Editor capture is released on
    // explicit end or focus loss, not once per frame.
#if !defined(INFERNUX_INPUT_SEMANTIC_HOST)
    RefreshScreenState();
    ApplyRelativeMouseMode();
#endif
}

void InputManager::ProcessKeyEvent(int scancode, bool pressed)
{
    if (scancode < 0 || scancode >= INPUT_MAX_KEYS)
        return;
    const size_t index = static_cast<size_t>(scancode);
    if (pressed) {
        if (m_keys[index] == 0)
            m_keyDown[index] = 1;
        m_keys[index] = 1;
    } else {
        if (m_keys[index] != 0)
            m_keyUp[index] = 1;
        m_keys[index] = 0;
    }
}

void InputManager::ProcessPointerButtonEvent(int button, bool pressed)
{
    if (button < 0 || button >= INPUT_MAX_MOUSE_BUTTONS)
        return;
    const size_t index = static_cast<size_t>(button);
    if (pressed) {
        if (m_mouseButtons[index] == 0)
            m_mouseButtonDown[index] = 1;
        m_mouseButtons[index] = 1;
    } else {
        if (m_mouseButtons[index] != 0)
            m_mouseButtonUp[index] = 1;
        m_mouseButtons[index] = 0;
    }
}

void InputManager::ProcessPointerMotionEvent(float x, float y, float deltaX, float deltaY)
{
    m_mouseX = x;
    m_mouseY = y;
    m_mouseDX += deltaX;
    m_mouseDY += deltaY;
    if (m_editorMouseCaptured) {
        m_editorMouseDX += deltaX;
        m_editorMouseDY += deltaY;
    }
}

void InputManager::ProcessScrollEvent(float deltaX, float deltaY)
{
    m_scrollX += deltaX;
    m_scrollY += deltaY;
}

void InputManager::ProcessTextInputEvent(const std::string &text)
{
    m_inputString += text;
}

void InputManager::ProcessMotionSensorEvent(MotionSensorType type, uint64_t timestampNs, float x, float y, float z)
{
    if (type == MotionSensorType::Accelerometer) {
        const float inverseGravity = 1.0f / SDL_STANDARD_GRAVITY;
        m_acceleration = {x * inverseGravity, y * inverseGravity, z * inverseGravity};
        const float deltaTime = m_lastAccelerationTimestampNs != 0 && timestampNs >= m_lastAccelerationTimestampNs
                                    ? static_cast<float>(timestampNs - m_lastAccelerationTimestampNs) * 1.0e-9f
                                    : 0.0f;
        m_lastAccelerationTimestampNs = timestampNs;
        m_accelerationEvents.push_back({m_acceleration, deltaTime});
        m_accelerometerAvailable = true;
        return;
    }

    m_gyroscopeRotationRate = {x, y, z};
    m_gyroscopeAvailable = true;
}

void InputManager::InitializeMotionSensors()
{
#if !defined(INFERNUX_INPUT_SEMANTIC_HOST)
    ShutdownMotionSensors();
    int sensorCount = 0;
    SDL_SensorID *sensorIds = SDL_GetSensors(&sensorCount);
    if (sensorIds == nullptr)
        return;

    for (int index = 0; index < sensorCount; ++index) {
        const SDL_SensorID sensorId = sensorIds[index];
        const SDL_SensorType sensorType = SDL_GetSensorTypeForID(sensorId);
        if (sensorType == SDL_SENSOR_ACCEL && m_accelerometer == nullptr) {
            m_accelerometer = SDL_OpenSensor(sensorId);
            if (m_accelerometer != nullptr) {
                m_accelerometerId = sensorId;
                m_accelerometerAvailable = true;
            }
        } else if (sensorType == SDL_SENSOR_GYRO && m_gyroscope == nullptr) {
            m_gyroscope = SDL_OpenSensor(sensorId);
            if (m_gyroscope != nullptr) {
                m_gyroscopeId = sensorId;
                m_gyroscopeAvailable = true;
            }
        }
    }
    SDL_free(sensorIds);
#endif
}

void InputManager::ShutdownMotionSensors()
{
#if !defined(INFERNUX_INPUT_SEMANTIC_HOST)
    if (m_accelerometer != nullptr)
        SDL_CloseSensor(m_accelerometer);
    if (m_gyroscope != nullptr)
        SDL_CloseSensor(m_gyroscope);
#endif
    m_accelerometer = nullptr;
    m_gyroscope = nullptr;
    m_accelerometerId = 0;
    m_gyroscopeId = 0;
    m_lastAccelerationTimestampNs = 0;
    m_accelerometerAvailable = false;
    m_gyroscopeAvailable = false;
    m_acceleration = {};
    m_gyroscopeRotationRate = {};
    m_accelerationEvents.clear();
}

void InputManager::ProcessTouchEvent(uint64_t touchId, uint64_t fingerId, uint64_t timestampNs, uint32_t windowId,
                                     float x, float y, float deltaX, float deltaY, float pressure, TouchPhase phase,
                                     float contactWidth, float contactHeight, bool isPrimary,
                                     const std::string &cancelReason)
{
    const auto existing = std::find_if(m_touches.begin(), m_touches.end(), [&](const TouchState &touch) {
        return touch.touchId == touchId && touch.fingerId == fingerId;
    });
    TouchState *touch = nullptr;
    if (existing == m_touches.end()) {
        const bool hasPrimaryContact = std::any_of(m_touches.begin(), m_touches.end(), [](const TouchState &candidate) {
            return candidate.isPrimary && candidate.phase != TouchPhase::Ended &&
                   candidate.phase != TouchPhase::Canceled;
        });
        m_touches.emplace_back();
        touch = &m_touches.back();
        touch->touchId = touchId;
        touch->fingerId = fingerId;
        touch->isPrimary = isPrimary || !hasPrimaryContact;
    } else {
        touch = &*existing;
    }
    const uint64_t previousTimestampNs = touch->timestampNs;
    touch->timestampNs = timestampNs;
    touch->windowId = windowId;
    touch->x = x;
    touch->y = y;
    touch->deltaX = deltaX;
    touch->deltaY = deltaY;
    touch->deltaTime = previousTimestampNs != 0 && timestampNs >= previousTimestampNs
                           ? static_cast<float>(timestampNs - previousTimestampNs) * 1.0e-9f
                           : 0.0f;
    touch->pressure = pressure;
    touch->contactWidth = contactWidth;
    touch->contactHeight = contactHeight;
    touch->isPrimary = touch->isPrimary || isPrimary;
    touch->cancelReason = phase == TouchPhase::Canceled ? cancelReason : std::string{};
    touch->phase = phase;
}

void InputManager::ProcessFocusEvent(bool focused)
{
    if (focused) {
        if (!m_screenState.focused) {
            m_screenState.focused = true;
            ++m_screenState.revision;
        }
        return;
    }
    StopTextInput();
    m_editorMouseCaptured = false;
    ResetPhysicalInputForFocusLoss();
    if (m_screenState.focused) {
        m_screenState.focused = false;
        ++m_screenState.revision;
    }
#if !defined(INFERNUX_INPUT_SEMANTIC_HOST)
    ApplyRelativeMouseMode();
#endif
}

void InputManager::ProcessScreenMetrics(int logicalWidth, int logicalHeight, int framebufferWidth,
                                        int framebufferHeight, float pixelRatio, int safeAreaX, int safeAreaY,
                                        int safeAreaWidth, int safeAreaHeight, bool keyboardInsetKnown,
                                        int keyboardInset)
{
    if (!std::isfinite(pixelRatio) || pixelRatio <= 0.0f)
        throw std::invalid_argument("screen pixel ratio must be finite and positive");
    ScreenState next = m_screenState;
    next.logicalWidth = std::max(1, logicalWidth);
    next.logicalHeight = std::max(1, logicalHeight);
    next.framebufferWidth = std::max(1, framebufferWidth);
    next.framebufferHeight = std::max(1, framebufferHeight);
    next.pixelRatio = pixelRatio;
    next.safeAreaX = std::clamp(safeAreaX, 0, next.logicalWidth);
    next.safeAreaY = std::clamp(safeAreaY, 0, next.logicalHeight);
    next.safeAreaWidth = std::clamp(safeAreaWidth, 0, next.logicalWidth - next.safeAreaX);
    next.safeAreaHeight = std::clamp(safeAreaHeight, 0, next.logicalHeight - next.safeAreaY);
    next.keyboardInsetKnown = keyboardInsetKnown;
    next.keyboardInset = keyboardInsetKnown ? std::clamp(keyboardInset, 0, next.logicalHeight) : 0;
    const bool changed =
        next.logicalWidth != m_screenState.logicalWidth || next.logicalHeight != m_screenState.logicalHeight ||
        next.framebufferWidth != m_screenState.framebufferWidth ||
        next.framebufferHeight != m_screenState.framebufferHeight || next.pixelRatio != m_screenState.pixelRatio ||
        next.safeAreaX != m_screenState.safeAreaX || next.safeAreaY != m_screenState.safeAreaY ||
        next.safeAreaWidth != m_screenState.safeAreaWidth || next.safeAreaHeight != m_screenState.safeAreaHeight ||
        next.keyboardInsetKnown != m_screenState.keyboardInsetKnown ||
        next.keyboardInset != m_screenState.keyboardInset;
    if (!changed)
        return;
    next.revision = m_screenState.revision + 1;
    const bool viewportChanged =
        next.logicalWidth != m_screenState.logicalWidth || next.logicalHeight != m_screenState.logicalHeight ||
        next.safeAreaX != m_screenState.safeAreaX || next.safeAreaY != m_screenState.safeAreaY ||
        next.safeAreaWidth != m_screenState.safeAreaWidth || next.safeAreaHeight != m_screenState.safeAreaHeight;
    m_screenState = next;
    if (viewportChanged)
        CancelActiveTouches("viewport_changed");
}

bool InputManager::StartTextInput()
{
    if (m_textInputActive)
        return true;
#if defined(INFERNUX_INPUT_SEMANTIC_HOST)
    m_textInputActive = true;
    return true;
#else
    if (m_window == nullptr || !SDL_StartTextInput(m_window))
        return false;
    m_textInputActive = true;
    return true;
#endif
}

void InputManager::StopTextInput()
{
    if (!m_textInputActive)
        return;
#if !defined(INFERNUX_INPUT_SEMANTIC_HOST)
    if (m_window != nullptr)
        SDL_StopTextInput(m_window);
#endif
    m_textInputActive = false;
}

void InputManager::ProcessSDLEvent(const SDL_Event &event)
{
    // Helper: Remap SDL button index to Unity convention
    // SDL3: 1=left, 2=middle, 3=right, 4=X1, 5=X2
    // Unity: 0=left, 1=right, 2=middle, 3=X1, 4=X2
    auto remapButton = [](int sdlButton) -> int {
        switch (sdlButton) {
        case SDL_BUTTON_LEFT:
            return 0;
        case SDL_BUTTON_RIGHT:
            return 1;
        case SDL_BUTTON_MIDDLE:
            return 2;
        case SDL_BUTTON_X1:
            return 3;
        case SDL_BUTTON_X2:
            return 4;
        default:
            return sdlButton - 1;
        }
    };
    switch (event.type) {

    // ---- Keyboard ----
    case SDL_EVENT_KEY_DOWN:
        ProcessKeyEvent(static_cast<int>(event.key.scancode), true);
        break;
    case SDL_EVENT_KEY_UP:
        ProcessKeyEvent(static_cast<int>(event.key.scancode), false);
        break;

    // ---- Mouse buttons ----
    // SDL3 mouse button indices: 1=left, 2=middle, 3=right, 4/5=side
    // We remap to Unity convention: 0=left, 1=right, 2=middle, 3/4=side
    case SDL_EVENT_MOUSE_BUTTON_DOWN: {
        if (event.button.which == SDL_TOUCH_MOUSEID)
            break;
        ProcessPointerButtonEvent(remapButton(event.button.button), true);
        break;
    }
    case SDL_EVENT_MOUSE_BUTTON_UP: {
        if (event.button.which == SDL_TOUCH_MOUSEID)
            break;
        ProcessPointerButtonEvent(remapButton(event.button.button), false);
        break;
    }

    // ---- Mouse motion ----
    case SDL_EVENT_MOUSE_MOTION:
        if (event.motion.which == SDL_TOUCH_MOUSEID)
            break;
        ProcessPointerMotionEvent(event.motion.x, event.motion.y, event.motion.xrel, event.motion.yrel);
        break;

    // ---- Mouse wheel ----
    case SDL_EVENT_MOUSE_WHEEL:
        ProcessScrollEvent(event.wheel.x, event.wheel.y);
        break;

    // ---- Text input ----
    case SDL_EVENT_TEXT_INPUT:
        ProcessTextInputEvent(event.text.text);
        break;

    // ---- Motion sensors ----
    case SDL_EVENT_SENSOR_UPDATE:
        if (event.sensor.which == m_accelerometerId) {
            ProcessMotionSensorEvent(MotionSensorType::Accelerometer, event.sensor.sensor_timestamp,
                                     event.sensor.data[0], event.sensor.data[1], event.sensor.data[2]);
        } else if (event.sensor.which == m_gyroscopeId) {
            ProcessMotionSensorEvent(MotionSensorType::Gyroscope, event.sensor.sensor_timestamp, event.sensor.data[0],
                                     event.sensor.data[1], event.sensor.data[2]);
        }
        break;

    // ---- Touch ----
    case SDL_EVENT_FINGER_DOWN:
    case SDL_EVENT_FINGER_MOTION:
    case SDL_EVENT_FINGER_UP:
    case SDL_EVENT_FINGER_CANCELED: {
        const SDL_TouchFingerEvent &finger = event.tfinger;
        TouchPhase phase = TouchPhase::Canceled;
        switch (event.type) {
        case SDL_EVENT_FINGER_DOWN:
            phase = TouchPhase::Began;
            break;
        case SDL_EVENT_FINGER_MOTION:
            phase = (finger.dx == 0.0f && finger.dy == 0.0f) ? TouchPhase::Stationary : TouchPhase::Moved;
            break;
        case SDL_EVENT_FINGER_UP:
            phase = TouchPhase::Ended;
            break;
        default:
            break;
        }
        ProcessTouchEvent(finger.touchID, finger.fingerID, finger.timestamp, finger.windowID, finger.x, finger.y,
                          finger.dx, finger.dy, finger.pressure, phase, 0.0f, 0.0f, false,
                          phase == TouchPhase::Canceled ? "platform_cancel" : "");
        break;
    }

    // ---- File drop from OS ----
    // SDL drop events carry the last drop position even when the window did
    // not receive ordinary mouse-motion events while another application
    // owned the drag. Keep the shared pointer state aligned with that target.
    case SDL_EVENT_DROP_POSITION:
        m_mouseX = event.drop.x;
        m_mouseY = event.drop.y;
        break;
    case SDL_EVENT_DROP_FILE: {
        m_mouseX = event.drop.x;
        m_mouseY = event.drop.y;
        if (event.drop.data) {
            m_droppedFiles.emplace_back(event.drop.data);
        }
        break;
    }

    // ---- Window focus lost → release editor drag capture and clear inputs ----
    case SDL_EVENT_WINDOW_FOCUS_LOST:
        ProcessFocusEvent(false);
        break;
    case SDL_EVENT_WINDOW_FOCUS_GAINED:
        ProcessFocusEvent(true);
        break;
    case SDL_EVENT_WINDOW_RESIZED:
    case SDL_EVENT_WINDOW_PIXEL_SIZE_CHANGED:
    case SDL_EVENT_WINDOW_DISPLAY_SCALE_CHANGED:
    case SDL_EVENT_WINDOW_SAFE_AREA_CHANGED:
        RefreshScreenState();
        break;
    case SDL_EVENT_WINDOW_OCCLUDED:
        if (!m_screenState.occluded) {
            m_screenState.occluded = true;
            ++m_screenState.revision;
        }
        break;
    case SDL_EVENT_WINDOW_EXPOSED:
    case SDL_EVENT_WINDOW_RESTORED:
        if (m_screenState.occluded) {
            m_screenState.occluded = false;
            ++m_screenState.revision;
        }
        RefreshScreenState();
        break;

    default:
        break;
    }
}

void InputManager::SetSyntheticMousePositionForFrame(float x, float y)
{
    m_mouseX = x;
    m_mouseY = y;
    m_syntheticMouseX = x;
    m_syntheticMouseY = y;
    m_hasSyntheticMousePositionThisFrame = true;
}

bool InputManager::GetSyntheticMousePositionForFrame(float &x, float &y) const
{
    if (!m_hasSyntheticMousePositionThisFrame)
        return false;
    x = m_syntheticMouseX;
    y = m_syntheticMouseY;
    return true;
}

void InputManager::MarkSyntheticInputForFrame()
{
    m_syntheticInputThisFrame = true;
}

void InputManager::TrackSyntheticEvent(const SDL_Event &event)
{
    m_syntheticInputThisFrame = true;
    const auto setHeld = [this](auto &states, int index, bool held) {
        if (index < 0 || index >= static_cast<int>(states.size()))
            return;
        const uint8_t value = held ? 1u : 0u;
        if (states[static_cast<size_t>(index)] == value)
            return;
        states[static_cast<size_t>(index)] = value;
        if (held)
            ++m_syntheticHeldCount;
        else if (m_syntheticHeldCount > 0)
            --m_syntheticHeldCount;
    };

    if (event.type == SDL_EVENT_KEY_DOWN || event.type == SDL_EVENT_KEY_UP) {
        setHeld(m_syntheticKeys, static_cast<int>(event.key.scancode), event.type == SDL_EVENT_KEY_DOWN);
        return;
    }
    if (event.type != SDL_EVENT_MOUSE_BUTTON_DOWN && event.type != SDL_EVENT_MOUSE_BUTTON_UP)
        return;

    int button = -1;
    switch (event.button.button) {
    case SDL_BUTTON_LEFT:
        button = 0;
        break;
    case SDL_BUTTON_RIGHT:
        button = 1;
        break;
    case SDL_BUTTON_MIDDLE:
        button = 2;
        break;
    case SDL_BUTTON_X1:
        button = 3;
        break;
    case SDL_BUTTON_X2:
        button = 4;
        break;
    default:
        break;
    }
    setHeld(m_syntheticMouseButtons, button, event.type == SDL_EVENT_MOUSE_BUTTON_DOWN);
}

// ============================================================================
// Keyboard queries
// ============================================================================

bool InputManager::GetKey(int scancode) const
{
    if (scancode < 0 || scancode >= INPUT_MAX_KEYS)
        return false;
    return m_keys[scancode] != 0;
}

bool InputManager::GetKeyDown(int scancode) const
{
    if (scancode < 0 || scancode >= INPUT_MAX_KEYS)
        return false;
    return m_keyDown[scancode] != 0;
}

bool InputManager::GetKeyUp(int scancode) const
{
    if (scancode < 0 || scancode >= INPUT_MAX_KEYS)
        return false;
    return m_keyUp[scancode] != 0;
}

bool InputManager::AnyKey() const
{
    for (int i = 0; i < INPUT_MAX_KEYS; ++i) {
        if (m_keys[i])
            return true;
    }
    return false;
}

bool InputManager::AnyKeyDown() const
{
    for (int i = 0; i < INPUT_MAX_KEYS; ++i) {
        if (m_keyDown[i])
            return true;
    }
    return false;
}

// ============================================================================
// Mouse button queries
// ============================================================================

bool InputManager::GetMouseButton(int button) const
{
    if (button < 0 || button >= INPUT_MAX_MOUSE_BUTTONS)
        return false;
    return m_mouseButtons[button] != 0;
}

bool InputManager::GetMouseButtonDown(int button) const
{
    if (button < 0 || button >= INPUT_MAX_MOUSE_BUTTONS)
        return false;
    return m_mouseButtonDown[button] != 0;
}

bool InputManager::GetMouseButtonUp(int button) const
{
    if (button < 0 || button >= INPUT_MAX_MOUSE_BUTTONS)
        return false;
    return m_mouseButtonUp[button] != 0;
}

std::tuple<float, float, float, float, bool, bool, bool> InputManager::GetMouseFrameState(int button) const
{
    if (button < 0 || button >= INPUT_MAX_MOUSE_BUTTONS) {
        return {m_mouseX, m_mouseY, m_scrollX, m_scrollY, false, false, false};
    }

    const bool held = (m_mouseButtons[button] != 0);
    const bool down = (m_mouseButtonDown[button] != 0);
    const bool up = (m_mouseButtonUp[button] != 0);
    return {m_mouseX, m_mouseY, m_scrollX, m_scrollY, held, down, up};
}

// ============================================================================
// Reset
// ============================================================================

void InputManager::ResetAll()
{
    m_keys.fill(0);
    m_keyDown.fill(0);
    m_keyUp.fill(0);
    m_mouseButtons.fill(0);
    m_mouseButtonDown.fill(0);
    m_mouseButtonUp.fill(0);
    m_mouseX = m_mouseY = 0.f;
    m_mouseDX = m_mouseDY = 0.f;
    m_editorMouseDX = m_editorMouseDY = 0.f;
    m_scrollX = m_scrollY = 0.f;
    m_syntheticMouseX = m_syntheticMouseY = 0.f;
    m_hasSyntheticMousePositionThisFrame = false;
    m_syntheticInputThisFrame = false;
    m_syntheticKeys.fill(0);
    m_syntheticMouseButtons.fill(0);
    m_syntheticHeldCount = 0;
    m_inputString.clear();
    m_touches.clear();
    m_accelerationEvents.clear();
    m_droppedFiles.clear();
}

void InputManager::ResetPhysicalInputForFocusLoss()
{
    // Losing OS focus must release real device state, but trusted synthetic
    // input intentionally drives gameplay while the Editor remains in the
    // background. Preserve that independently tracked held state and rebuild
    // the shared query arrays from it after clearing physical input.
    const auto syntheticKeys = m_syntheticKeys;
    const auto syntheticMouseButtons = m_syntheticMouseButtons;
    const uint32_t syntheticHeldCount = m_syntheticHeldCount;
    auto interruptedTouches = std::move(m_touches);
    ResetAll();
    for (TouchState &touch : interruptedTouches) {
        if (touch.phase != TouchPhase::Ended && touch.phase != TouchPhase::Canceled) {
            touch.phase = TouchPhase::Canceled;
            touch.cancelReason = "focus_lost";
            touch.deltaX = 0.0f;
            touch.deltaY = 0.0f;
            m_touches.emplace_back(std::move(touch));
        }
    }
    m_syntheticKeys = syntheticKeys;
    m_syntheticMouseButtons = syntheticMouseButtons;
    m_syntheticHeldCount = syntheticHeldCount;
    m_syntheticInputThisFrame = syntheticHeldCount != 0;
    for (size_t index = 0; index < m_syntheticKeys.size(); ++index)
        m_keys[index] = m_syntheticKeys[index];
    for (size_t index = 0; index < m_syntheticMouseButtons.size(); ++index)
        m_mouseButtons[index] = m_syntheticMouseButtons[index];
}

void InputManager::CancelActiveTouches(const std::string &reason)
{
    for (TouchState &touch : m_touches) {
        if (touch.phase == TouchPhase::Ended || touch.phase == TouchPhase::Canceled)
            continue;
        touch.phase = TouchPhase::Canceled;
        touch.cancelReason = reason;
        touch.deltaX = 0.0f;
        touch.deltaY = 0.0f;
    }
}

const TouchState &InputManager::GetTouch(int index) const
{
    if (index < 0 || static_cast<size_t>(index) >= m_touches.size())
        throw std::out_of_range("Touch index is outside the current frame snapshot");
    return m_touches[static_cast<size_t>(index)];
}

// ============================================================================
// Name ↔ scancode mapping
// ============================================================================

int InputManager::NameToScancode(const std::string &name)
{
    BuildNameTable();

    // Lowercase + replace spaces with underscores
    std::string key = name;
    std::transform(key.begin(), key.end(), key.begin(), [](unsigned char c) { return std::tolower(c); });
    std::replace(key.begin(), key.end(), ' ', '_');

    auto it = s_nameToScancode.find(key);
    if (it != s_nameToScancode.end()) {
        return it->second;
    }

#if !defined(INFERNUX_INPUT_SEMANTIC_HOST)
    // Desktop adapters retain SDL's extended name vocabulary. Semantic hosts
    // deliberately expose only the engine's stable cross-platform table.
    SDL_Scancode sc = SDL_GetScancodeFromName(name.c_str());
    if (sc != SDL_SCANCODE_UNKNOWN) {
        return static_cast<int>(sc);
    }
#endif

    return -1;
}

const char *InputManager::ScancodeToName(int scancode)
{
    if (scancode < 0 || scancode >= INPUT_MAX_KEYS)
        return "unknown";
#if defined(INFERNUX_INPUT_SEMANTIC_HOST)
    BuildNameTable();
    const auto it = std::find_if(s_nameToScancode.begin(), s_nameToScancode.end(),
                                 [scancode](const auto &entry) { return entry.second == scancode; });
    return it != s_nameToScancode.end() ? it->first.c_str() : "unknown";
#else
    return SDL_GetScancodeName(static_cast<SDL_Scancode>(scancode));
#endif
}

// ============================================================================
// Cursor lock (FPS-style capture)
// ============================================================================

void InputManager::SetWindow(SDL_Window *window)
{
    m_window = window;
    RefreshScreenState();
    ApplyRelativeMouseMode();
}

void InputManager::RefreshScreenState()
{
#if defined(INFERNUX_INPUT_SEMANTIC_HOST)
    return;
#else
    if (m_window == nullptr)
        return;
    int logicalWidth = 1;
    int logicalHeight = 1;
    int framebufferWidth = 1;
    int framebufferHeight = 1;
    SDL_GetWindowSize(m_window, &logicalWidth, &logicalHeight);
    SDL_GetWindowSizeInPixels(m_window, &framebufferWidth, &framebufferHeight);
    SDL_Rect safeArea{0, 0, logicalWidth, logicalHeight};
    SDL_GetWindowSafeArea(m_window, &safeArea);
    const float displayScale = SDL_GetWindowDisplayScale(m_window);
    bool keyboardInsetKnown = false;
    int keyboardInset = 0;
#if defined(__ANDROID__)
    const char *keyboardInsetKnownValue = std::getenv("INFERNUX_ANDROID_KEYBOARD_INSET_KNOWN");
    const char *keyboardInsetValue = std::getenv("INFERNUX_ANDROID_KEYBOARD_INSET");
    keyboardInsetKnown = keyboardInsetKnownValue != nullptr && std::strcmp(keyboardInsetKnownValue, "1") == 0 &&
                         keyboardInsetValue != nullptr;
    if (keyboardInsetKnown) {
        char *end = nullptr;
        const long parsedInset = std::strtol(keyboardInsetValue, &end, 10);
        keyboardInsetKnown = end != keyboardInsetValue && end != nullptr && *end == '\0';
        if (keyboardInsetKnown) {
            keyboardInset = static_cast<int>(std::clamp(parsedInset, 0L, static_cast<long>(logicalHeight)));
        }
    }
#endif
    if (!std::isfinite(displayScale) || displayScale <= 0.0f)
        throw std::runtime_error("SDL reported an invalid screen display scale");
    ProcessScreenMetrics(logicalWidth, logicalHeight, framebufferWidth, framebufferHeight, displayScale, safeArea.x,
                         safeArea.y, safeArea.w, safeArea.h, keyboardInsetKnown, keyboardInset);
#endif
}

void InputManager::SetCursorLocked(bool locked)
{
    if (locked == m_cursorLocked)
        return;

    m_cursorLocked = locked;
    ApplyRelativeMouseMode();
}

void InputManager::SetEditorMouseCapture(bool captured)
{
    if (captured == m_editorMouseCaptured)
        return;

    m_editorMouseDX = 0.f;
    m_editorMouseDY = 0.f;
    m_editorMouseCaptured = captured;
    ApplyRelativeMouseMode();
}

std::pair<float, float> InputManager::ConsumeEditorMouseDelta()
{
    const std::pair<float, float> delta{m_editorMouseDX, m_editorMouseDY};
    m_editorMouseDX = 0.f;
    m_editorMouseDY = 0.f;
    return delta;
}

void InputManager::ApplyRelativeMouseMode()
{
#if defined(INFERNUX_INPUT_SEMANTIC_HOST)
    // Browser and other semantic hosts own pointer-lock policy in their window
    // adapter. The engine only preserves the requested logical state here.
    return;
#else
    const bool relativeMouseEnabled = m_cursorLocked || m_editorMouseCaptured;

    if (!m_window) {
        if (relativeMouseEnabled) {
            INXLOG_WARN("InputManager::ApplyRelativeMouseMode — no window set, ignoring");
        }
        return;
    }

    if (!relativeMouseEnabled) {
        SDL_SetWindowRelativeMouseMode(m_window, false);
        return;
    }

    // Never keep SDL in relative mouse mode while the editor window is not the
    // active input target, otherwise alt-tab can leave the OS cursor captured.
    const Uint64 windowFlags = SDL_GetWindowFlags(m_window);
    if ((windowFlags & SDL_WINDOW_INPUT_FOCUS) == 0) {
        SDL_SetWindowRelativeMouseMode(m_window, false);
        return;
    }

    SDL_SetWindowRelativeMouseMode(m_window, true);
#endif
}

} // namespace infernux
