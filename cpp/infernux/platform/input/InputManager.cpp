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
 *   SDL_EVENT_FINGER_DOWN / SDL_EVENT_FINGER_UP
 *   SDL_EVENT_WINDOW_FOCUS_LOST
 */

#include "InputManager.h"

#include <algorithm>
#include <cctype>
#include <core/log/InxLog.h>
#include <cstring>

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
    m_prevKeys.fill(0);
    m_mouseButtons.fill(0);
    m_prevMouseButtons.fill(0);
    BuildNameTable();
}

// ============================================================================
// Frame lifecycle
// ============================================================================

void InputManager::BeginFrame()
{
    // Swap current → previous
    std::memcpy(m_prevKeys.data(), m_keys.data(), INPUT_MAX_KEYS);
    std::memcpy(m_prevMouseButtons.data(), m_mouseButtons.data(), INPUT_MAX_MOUSE_BUTTONS);

    // Clear per-frame deltas
    m_mouseDX = 0.f;
    m_mouseDY = 0.f;
    m_scrollX = 0.f;
    m_scrollY = 0.f;
    m_inputString.clear();
    m_touchCount = 0;
    m_droppedFiles.clear();
    m_hasSyntheticMousePositionThisFrame = false;
    m_syntheticInputThisFrame = false;

    // Re-apply relative mouse mode for the current window/focus state without
    // disturbing persistent capture flags. Editor capture is released on
    // explicit end or focus loss, not once per frame.
    ApplyRelativeMouseMode();
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
    case SDL_EVENT_KEY_DOWN: {
        int sc = static_cast<int>(event.key.scancode);
        if (sc >= 0 && sc < INPUT_MAX_KEYS) {
            m_keys[sc] = 1;
        }
        break;
    }
    case SDL_EVENT_KEY_UP: {
        int sc = static_cast<int>(event.key.scancode);
        if (sc >= 0 && sc < INPUT_MAX_KEYS) {
            m_keys[sc] = 0;
        }
        break;
    }

    // ---- Mouse buttons ----
    // SDL3 mouse button indices: 1=left, 2=middle, 3=right, 4/5=side
    // We remap to Unity convention: 0=left, 1=right, 2=middle, 3/4=side
    case SDL_EVENT_MOUSE_BUTTON_DOWN: {
        int btn = remapButton(event.button.button);
        if (btn >= 0 && btn < INPUT_MAX_MOUSE_BUTTONS) {
            m_mouseButtons[btn] = 1;
        }
        break;
    }
    case SDL_EVENT_MOUSE_BUTTON_UP: {
        int btn = remapButton(event.button.button);
        if (btn >= 0 && btn < INPUT_MAX_MOUSE_BUTTONS) {
            m_mouseButtons[btn] = 0;
        }
        break;
    }

    // ---- Mouse motion ----
    case SDL_EVENT_MOUSE_MOTION:
        m_mouseX = event.motion.x;
        m_mouseY = event.motion.y;
        m_mouseDX += event.motion.xrel;
        m_mouseDY += event.motion.yrel;
        if (m_editorMouseCaptured) {
            m_editorMouseDX += event.motion.xrel;
            m_editorMouseDY += event.motion.yrel;
        }
        break;

    // ---- Mouse wheel ----
    case SDL_EVENT_MOUSE_WHEEL:
        m_scrollX += event.wheel.x;
        m_scrollY += event.wheel.y;
        break;

    // ---- Text input ----
    case SDL_EVENT_TEXT_INPUT:
        m_inputString += event.text.text;
        break;

    // ---- Touch (basic tracking) ----
    case SDL_EVENT_FINGER_DOWN:
        ++m_touchCount;
        break;
    case SDL_EVENT_FINGER_UP:
        // Don't go negative
        m_touchCount = std::max(0, m_touchCount - 1);
        break;

    // ---- File drop from OS ----
    case SDL_EVENT_DROP_FILE: {
        if (event.drop.data) {
            m_droppedFiles.emplace_back(event.drop.data);
        }
        break;
    }

    // ---- Window focus lost → release editor drag capture and clear inputs ----
    case SDL_EVENT_WINDOW_FOCUS_LOST:
        m_editorMouseCaptured = false;
        ResetAll();
        ApplyRelativeMouseMode();
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
    return m_keys[scancode] != 0 && m_prevKeys[scancode] == 0;
}

bool InputManager::GetKeyUp(int scancode) const
{
    if (scancode < 0 || scancode >= INPUT_MAX_KEYS)
        return false;
    return m_keys[scancode] == 0 && m_prevKeys[scancode] != 0;
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
        if (m_keys[i] && !m_prevKeys[i])
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
    return m_mouseButtons[button] != 0 && m_prevMouseButtons[button] == 0;
}

bool InputManager::GetMouseButtonUp(int button) const
{
    if (button < 0 || button >= INPUT_MAX_MOUSE_BUTTONS)
        return false;
    return m_mouseButtons[button] == 0 && m_prevMouseButtons[button] != 0;
}

std::tuple<float, float, float, float, bool, bool, bool> InputManager::GetMouseFrameState(int button) const
{
    if (button < 0 || button >= INPUT_MAX_MOUSE_BUTTONS) {
        return {m_mouseX, m_mouseY, m_scrollX, m_scrollY, false, false, false};
    }

    const bool held = (m_mouseButtons[button] != 0);
    const bool down = held && (m_prevMouseButtons[button] == 0);
    const bool up = (!held) && (m_prevMouseButtons[button] != 0);
    return {m_mouseX, m_mouseY, m_scrollX, m_scrollY, held, down, up};
}

// ============================================================================
// Reset
// ============================================================================

void InputManager::ResetAll()
{
    m_keys.fill(0);
    m_prevKeys.fill(0);
    m_mouseButtons.fill(0);
    m_prevMouseButtons.fill(0);
    m_mouseX = m_mouseY = 0.f;
    m_mouseDX = m_mouseDY = 0.f;
    m_editorMouseDX = m_editorMouseDY = 0.f;
    m_scrollX = m_scrollY = 0.f;
    m_syntheticMouseX = m_syntheticMouseY = 0.f;
    m_hasSyntheticMousePositionThisFrame = false;
    m_inputString.clear();
    m_touchCount = 0;
    m_droppedFiles.clear();
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

    // Fallback: try SDL's own name lookup
    SDL_Scancode sc = SDL_GetScancodeFromName(name.c_str());
    if (sc != SDL_SCANCODE_UNKNOWN) {
        return static_cast<int>(sc);
    }

    return -1;
}

const char *InputManager::ScancodeToName(int scancode)
{
    if (scancode < 0 || scancode >= INPUT_MAX_KEYS)
        return "unknown";
    return SDL_GetScancodeName(static_cast<SDL_Scancode>(scancode));
}

// ============================================================================
// Cursor lock (FPS-style capture)
// ============================================================================

void InputManager::SetWindow(SDL_Window *window)
{
    m_window = window;
    ApplyRelativeMouseMode();
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
}

} // namespace infernux
