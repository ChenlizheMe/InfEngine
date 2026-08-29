/**
 * @file InputManager.h
 * @brief Unified input state manager — SDL event-driven, Unity-style query API.
 *
 * Design:
 *   - Persistent held state plus explicit per-frame down/up edge arrays.
 *   - O(1) lookup by SDL_Scancode index (no
 * hash maps on hot path).
 *   - BeginFrame() clears edge arrays and per-frame deltas.
 *   - ProcessSDLEvent() fills current-frame state from raw
 * SDL events.
 *
 * Integration:
 *   InxView::ProcessEvent() calls BeginFrame() once per frame, then
 *   feeds every SDL_Event through ProcessSDLEvent().
 *
 * Exposed to Python via BindingInput.cpp; the Python `Input` static class
 * wraps these methods with Unity-style naming (get_key_down, etc.).
 */

#pragma once

#include <array>
#include <cstdint>
#include <string>
#include <tuple>
#include <unordered_map>
#include <utility>
#include <vector>

union SDL_Event;
struct SDL_Window;

namespace infernux
{

/// Maximum number of keyboard scancodes tracked (SDL_SCANCODE_COUNT ≈ 512).
static constexpr int INPUT_MAX_KEYS = 512;

/// Maximum number of mouse buttons tracked (SDL supports up to 5, reserve 8).
static constexpr int INPUT_MAX_MOUSE_BUTTONS = 8;

enum class TouchPhase : uint8_t
{
    Began,
    Moved,
    Stationary,
    Ended,
    Canceled,
};

struct TouchState
{
    uint64_t touchId = 0;
    uint64_t fingerId = 0;
    uint64_t timestampNs = 0;
    uint32_t windowId = 0;
    float x = 0.0f;
    float y = 0.0f;
    float deltaX = 0.0f;
    float deltaY = 0.0f;
    float pressure = 0.0f;
    TouchPhase phase = TouchPhase::Stationary;
};

/**
 * @class InputManager
 * @brief Singleton that accumulates SDL input events and exposes Unity-style queries.
 *
 * Frame lifecycle:
 *   1. BeginFrame()        — swap buffers, clear deltas
 *   2. ProcessSDLEvent()×N — accumulate events
 *   3. User code queries   — GetKey/GetKeyDown/GetKeyUp/GetMouseButton/etc.
 */
class InputManager
{
  public:
    static InputManager &Instance();

    // ---- Per-frame lifecycle (called by InxView) ----

    /// @brief Begin a new input frame. Clears transition edges and deltas.
    void BeginFrame();

    /// @brief Feed an SDL event into the input state.
    void ProcessSDLEvent(const SDL_Event &event);

    // ---- Platform-neutral event ingestion ----
    // Window-system adapters translate their native events once and feed this
    // semantic layer. Web, Android, and desktop therefore share the exact same
    // held/edge/touch lifecycle instead of maintaining parallel input models.
    void ProcessKeyEvent(int scancode, bool pressed);
    void ProcessPointerButtonEvent(int button, bool pressed);
    void ProcessPointerMotionEvent(float x, float y, float deltaX, float deltaY);
    void ProcessScrollEvent(float deltaX, float deltaY);
    void ProcessTextInputEvent(const std::string &text);
    void ProcessTouchEvent(uint64_t touchId, uint64_t fingerId, uint64_t timestampNs, uint32_t windowId, float x,
                           float y, float deltaX, float deltaY, float pressure, TouchPhase phase);
    void ProcessFocusEvent(bool focused);

    /// @brief Mark a trusted synthetic pointer position for the current GUI frame.
    ///
    /// SDL's ImGui backend can otherwise fall back to the physical OS cursor
    /// after a synthetic button release. The override is intentionally scoped
    /// to the current frame and is consumed by the GUI layer before NewFrame.
    void SetSyntheticMousePositionForFrame(float x, float y);

    /// @brief Return the synthetic pointer position queued for the current frame.
    [[nodiscard]] bool GetSyntheticMousePositionForFrame(float &x, float &y) const;

    /// @brief Mark that the current frame is processing trusted synthetic input.
    ///
    /// Editor workflows use this to keep automation-only controls out of
    /// normal desktop interaction, such as native file dialogs.
    void MarkSyntheticInputForFrame();

    /// @brief Track trusted automation-held state without claiming OS or editor focus.
    ///
    /// Synthetic key/button presses can span many rendered frames. Keeping their
    /// ownership separate from the ordinary SDL held-state lets gameplay accept
    /// MCP input while the Editor remains in the background.
    void TrackSyntheticEvent(const SDL_Event &event);

    /// @brief True when at least one trusted synthetic event was handled this frame.
    [[nodiscard]] bool IsSyntheticInputFrame() const
    {
        return m_syntheticInputThisFrame;
    }

    /// @brief True while trusted automation supplied this frame's input or holds a control.
    [[nodiscard]] bool HasSyntheticGameplayInput() const
    {
        return m_syntheticInputThisFrame || m_syntheticHeldCount != 0;
    }

    // ---- Keyboard queries (Unity: Input.GetKey / GetKeyDown / GetKeyUp) ----

    /// @brief Returns true while the key identified by scancode is held down.
    [[nodiscard]] bool GetKey(int scancode) const;

    /// @brief Returns true during the frame the key was pressed down.
    [[nodiscard]] bool GetKeyDown(int scancode) const;

    /// @brief Returns true during the frame the key was released.
    [[nodiscard]] bool GetKeyUp(int scancode) const;

    /// @brief Returns true if any key is currently held down.
    [[nodiscard]] bool AnyKey() const;

    /// @brief Returns true during the frame any key was pressed down.
    [[nodiscard]] bool AnyKeyDown() const;

    // ---- Mouse button queries (Unity: Input.GetMouseButton / Down / Up) ----
    // button: 0 = left, 1 = right, 2 = middle, 3/4 = side buttons

    /// @brief Returns true while the given mouse button is held down.
    [[nodiscard]] bool GetMouseButton(int button) const;

    /// @brief Returns true during the frame the mouse button was pressed.
    [[nodiscard]] bool GetMouseButtonDown(int button) const;

    /// @brief Returns true during the frame the mouse button was released.
    [[nodiscard]] bool GetMouseButtonUp(int button) const;

    /// @brief Returns a batched snapshot for a mouse button this frame.
    /// Tuple layout: (mouseX, mouseY, scrollX, scrollY, held, down, up).
    [[nodiscard]] std::tuple<float, float, float, float, bool, bool, bool> GetMouseFrameState(int button) const;

    // ---- Mouse position & delta (Unity: Input.mousePosition, mouseDelta) ----
    // Coordinates are in window-space pixels, origin top-left.

    /// @brief Current mouse X position (window-space pixels).
    [[nodiscard]] float GetMousePositionX() const
    {
        return m_mouseX;
    }

    /// @brief Current mouse Y position (window-space pixels).
    [[nodiscard]] float GetMousePositionY() const
    {
        return m_mouseY;
    }

    /// @brief Mouse X movement this frame.
    [[nodiscard]] float GetMouseDeltaX() const
    {
        return m_mouseDX;
    }

    /// @brief Mouse Y movement this frame.
    [[nodiscard]] float GetMouseDeltaY() const
    {
        return m_mouseDY;
    }

    // ---- Scroll wheel (Unity: Input.mouseScrollDelta) ----

    /// @brief Vertical scroll delta this frame (positive = scroll up).
    [[nodiscard]] float GetMouseScrollDeltaY() const
    {
        return m_scrollY;
    }

    /// @brief Horizontal scroll delta this frame.
    [[nodiscard]] float GetMouseScrollDeltaX() const
    {
        return m_scrollX;
    }

    // ---- Text input (Unity: Input.inputString) ----

    /// @brief Characters typed this frame (UTF-8).
    [[nodiscard]] const std::string &GetInputString() const
    {
        return m_inputString;
    }

    // ---- Touch ----

    /// @brief Number of touch contacts reported by the current frame snapshot.
    [[nodiscard]] int GetTouchCount() const
    {
        return static_cast<int>(m_touches.size());
    }

    /// @brief Return one stable-indexed touch from the current frame snapshot.
    [[nodiscard]] const TouchState &GetTouch(int index) const;

    /// @brief Return every touch in first-contact order for this frame.
    [[nodiscard]] const std::vector<TouchState> &GetTouches() const
    {
        return m_touches;
    }

    // ---- File drop (OS drag-drop) ----

    /// @brief Returns true if one or more files were dropped onto the window this frame.
    [[nodiscard]] bool HasDroppedFiles() const
    {
        return !m_droppedFiles.empty();
    }

    /// @brief Returns the list of file paths dropped onto the window this frame.
    [[nodiscard]] const std::vector<std::string> &GetDroppedFiles() const
    {
        return m_droppedFiles;
    }

    // ---- Cursor lock (FPS-style mouse capture) ----

    /// @brief Store the SDL window handle so cursor lock can target it.
    void SetWindow(SDL_Window *window);

    /// @brief Enable/disable cursor lock (SDL relative mouse mode).
    ///        When locked the cursor is hidden and mouse deltas are captured.
    void SetCursorLocked(bool locked);

    /// @brief Enable/disable editor-only mouse capture for Scene view camera drag.
    ///        Uses the same SDL relative mouse mode backend but does not report
    ///        as gameplay cursor lock to Python game scripts or ImGui suppression.
    void SetEditorMouseCapture(bool captured);

    /// @brief Consume all relative mouse movement received during editor capture.
    ///
    /// Editor UI construction can run slower than the renderer. Keeping this
    /// accumulator independent from the per-frame gameplay delta prevents
    /// Scene view navigation from dropping motion between UI updates.
    [[nodiscard]] std::pair<float, float> ConsumeEditorMouseDelta();

    /// @brief Returns true when cursor lock is active.
    [[nodiscard]] bool IsCursorLocked() const
    {
        return m_cursorLocked;
    }

    /// @brief Returns true when editor Scene view mouse capture is active.
    [[nodiscard]] bool IsEditorMouseCaptureActive() const
    {
        return m_editorMouseCaptured;
    }

    // ---- Utility ----

    /// @brief Reset all input state (e.g. on window focus loss or scene change).
    void ResetAll();

    /// @brief Map a human-readable key name to SDL_Scancode. Case-insensitive.
    ///        Returns -1 if the name is unknown.
    static int NameToScancode(const std::string &name);

    /// @brief Get the human-readable name for a scancode.
    static const char *ScancodeToName(int scancode);

  private:
    InputManager();
    ~InputManager() = default;
    InputManager(const InputManager &) = delete;
    InputManager &operator=(const InputManager &) = delete;

    // ---- State buffers ----
    std::array<uint8_t, INPUT_MAX_KEYS> m_keys{};
    std::array<uint8_t, INPUT_MAX_KEYS> m_keyDown{};
    std::array<uint8_t, INPUT_MAX_KEYS> m_keyUp{};
    std::array<uint8_t, INPUT_MAX_MOUSE_BUTTONS> m_mouseButtons{};
    std::array<uint8_t, INPUT_MAX_MOUSE_BUTTONS> m_mouseButtonDown{};
    std::array<uint8_t, INPUT_MAX_MOUSE_BUTTONS> m_mouseButtonUp{};

    float m_mouseX = 0.f;
    float m_mouseY = 0.f;
    float m_mouseDX = 0.f;
    float m_mouseDY = 0.f;
    float m_editorMouseDX = 0.f;
    float m_editorMouseDY = 0.f;
    float m_scrollX = 0.f;
    float m_scrollY = 0.f;

    float m_syntheticMouseX = 0.f;
    float m_syntheticMouseY = 0.f;
    bool m_hasSyntheticMousePositionThisFrame = false;
    bool m_syntheticInputThisFrame = false;
    std::array<uint8_t, INPUT_MAX_KEYS> m_syntheticKeys{};
    std::array<uint8_t, INPUT_MAX_MOUSE_BUTTONS> m_syntheticMouseButtons{};
    uint32_t m_syntheticHeldCount = 0;

    std::string m_inputString;
    std::vector<TouchState> m_touches;
    std::vector<std::string> m_droppedFiles;

    SDL_Window *m_window = nullptr;
    bool m_cursorLocked = false;
    bool m_editorMouseCaptured = false;

    // ---- Name → scancode lookup ----
    static std::unordered_map<std::string, int> s_nameToScancode;
    static bool s_nameTableBuilt;
    static void BuildNameTable();

    void ResetPhysicalInputForFocusLoss();
    void ApplyRelativeMouseMode();
};

} // namespace infernux
