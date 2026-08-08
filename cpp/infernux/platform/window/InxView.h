#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <deque>
#include <mutex>
#include <string>

#include <core/log/InxLog.h>
#include <core/types/InxApplication.h>

#include <vulkan/vulkan.h>

#include <SDL3/SDL.h>
#include <SDL3/SDL_vulkan.h>

namespace infernux
{

/// Power-save / idle configuration for the editor main loop.
/// When no user input is detected for a short period, the loop sleeps
/// via ``SDL_WaitEventTimeout`` to reduce CPU/GPU usage.
/// An editor FPS cap also limits the maximum frame rate in edit mode.
struct FpsIdling
{
    float fpsIdle = 10.0f;       ///< Target FPS when idling (0 = disable idle)
    float editorFpsCap = 144.0f; ///< Max FPS in editor mode (0 = uncapped)
    bool enableIdling = true;    ///< Master switch for idle detection
    bool isIdling = false;       ///< Output — true when the last frame went idle
};

/// Per-frame pacing diagnostics for editor FPS cap / idle mode.
/// This is intentionally lightweight so the renderer profiler can report
/// whether frame pacing is actually sleeping the main loop.
struct FramePacingSample
{
    bool playModeBypass = false;
    bool idleMode = false;
    bool slept = false;
    bool wokeByEvent = false;
    bool wokeByInputEvent = false;
    bool wokeByWindowEvent = false;
    bool wokeByOtherEvent = false;
    bool hadInputEvent = false;
    int cooldownRemaining = 0;
    float targetFps = 0.0f;
    double elapsedBeforeSleepMs = 0.0;
    double frameBudgetMs = 0.0;
    double requestedSleepMs = 0.0;
    double actualSleepMs = 0.0;
};

class InxView
{
  public:
    friend class InxRenderer;

    InxView();

    InxView(const InxView &) = delete;
    InxView(InxView &&) = delete;
    InxView &operator=(const InxView &) = delete;
    InxView &operator=(InxView &&) = delete;

    const char *const *GetVkExtensions(uint32_t *count);

    void Init(int width, int height);
    void ProcessEvent();
    void Quit();

    // Synthetic input is intentionally consumed through ProcessEvent(), so
    // automation follows the same ImGui and InputManager path as hardware input.
    uint64_t QueueSyntheticKeyInput(int scancode, bool pressed, bool repeat = false);
    uint64_t QueueSyntheticMouseButtonInput(int button, bool pressed, float x, float y);
    uint64_t QueueSyntheticMouseMotionInput(float x, float y, float deltaX, float deltaY);
    uint64_t QueueSyntheticMouseWheelInput(float horizontal, float vertical);
    uint64_t QueueSyntheticTextInput(const std::string &text);
    uint64_t QueueSyntheticCloseRequest();
    [[nodiscard]] uint64_t GetLastProcessedSyntheticInputSequence() const noexcept
    {
        return m_lastProcessedSyntheticInputSequence.load(std::memory_order_acquire);
    }
    [[nodiscard]] size_t GetPendingSyntheticInputCount() const;

    int GetUserEvent();
    void Show();
    void Hide();
    void SetWindowIcon(const std::string &iconPath);
    void SetWindowFullscreen(bool fullscreen);
    void SetWindowTitle(const std::string &title);
    void SetWindowMaximized(bool maximized);
    void SetWindowResizable(bool resizable);

    /// Close-request interception: SDL_EVENT_QUIT sets this flag instead of
    /// immediately terminating.  Python checks the flag each frame and may
    /// show a "save before exit?" dialog before calling ConfirmClose().
    bool IsCloseRequested() const
    {
        return m_closeRequested;
    }
    void ConfirmClose()
    {
        m_keepRunning = false;
    }
    void CancelClose()
    {
        m_closeRequested = false;
    }

    bool IsMinimized() const
    {
        return m_isMinimized;
    }
    // ---- Power-save / idle accessors ----
    FpsIdling &GetIdling()
    {
        return m_idling;
    }
    const FpsIdling &GetIdling() const
    {
        return m_idling;
    }
    const FramePacingSample &GetLastPacingSample() const
    {
        return m_lastPacingSample;
    }
    [[nodiscard]] bool NeedsImmediateGuiRefresh() const noexcept
    {
        return m_needsImmediateGuiRefresh;
    }

    /// Tell InxView whether the engine is in play mode.
    /// When true, the frame-rate cap and idle sleep are both disabled.
    void SetPlayMode(bool play)
    {
        m_isPlayMode = play;
    }
    bool IsPlayMode() const
    {
        return m_isPlayMode;
    }

    /// Signal that the current frame required full-speed rendering
    /// (e.g. animation playing, programmatic scene change).
    /// Resets the idle cooldown so the next few frames run at editor cap.
    void RequestFullSpeedFrame()
    {
        m_activeFramesRemaining = ACTIVE_COOLDOWN_FRAMES;
    }

    void CreateSurface(VkInstance *vkInstance, VkSurfaceKHR *vkSurface);
    void SetAppMetadata(InxAppMetadata appMetaData);

  private:
    enum class SyntheticInputType
    {
        Key,
        MouseButton,
        MouseMotion,
        MouseWheel,
        Text,
        CloseRequest,
    };

    struct SyntheticInputEvent
    {
        uint64_t sequence = 0;
        SyntheticInputType type = SyntheticInputType::Key;
        int keyOrButton = 0;
        bool pressed = false;
        bool repeat = false;
        float x = 0.0f;
        float y = 0.0f;
        float deltaX = 0.0f;
        float deltaY = 0.0f;
        std::string text;
    };

    static constexpr size_t MAX_SYNTHETIC_INPUT_EVENTS = 4096;

    int m_windowWidth = 0;
    int m_windowHeight = 0;

    SDL_Window *m_window = nullptr;

    bool m_keepRunning;
    bool m_closeRequested = false;
    bool m_isMinimized = false;
    bool m_isPlayMode = false;
    bool m_needsImmediateGuiRefresh = false;
    InxAppMetadata m_appMetadata;

    // ---- Power-save idle state ----
    FpsIdling m_idling;
    FramePacingSample m_lastPacingSample;
    /// Number of full-speed frames remaining after the last user interaction.
    /// When this reaches 0 and idling is enabled the loop will sleep more.
    static constexpr int ACTIVE_COOLDOWN_FRAMES = 10;
    int m_activeFramesRemaining = ACTIVE_COOLDOWN_FRAMES;

    /// Timestamp of the last frame start — used to compute the remaining
    /// frame budget so the sleep duration adapts to actual render time.
    std::chrono::steady_clock::time_point m_lastFrameStart = std::chrono::steady_clock::now();

    mutable std::mutex m_syntheticInputMutex;
    std::deque<SyntheticInputEvent> m_syntheticInputEvents;
    uint64_t m_nextSyntheticInputSequence = 1;
    std::atomic<uint64_t> m_lastProcessedSyntheticInputSequence{0};
    // A synthetic mouse release is held until InxRenderer confirms that a
    // real ImGui frame has consumed the corresponding press transition.
    uint8_t m_syntheticMouseButtonsAwaitingGuiFrame = 0;
    uint8_t m_syntheticMouseButtonsReadyForRelease = 0;
    // SDL does not update its global modifier state for events injected by
    // automation, so retain the synthetic contribution for subsequent keys.
    SDL_Keymod m_syntheticKeyModifiers = SDL_KMOD_NONE;

    void SDLInit();
    uint64_t QueueSyntheticInput(SyntheticInputEvent event);
    [[nodiscard]] bool HasPendingSyntheticInput() const;
    void DrainSyntheticInputEvents(bool &hadInputEvent);
    void NotifyGuiFrameBuilt() noexcept;
    bool ProcessOneEvent(SDL_Event &event, bool syntheticScreenCoordinates = false);
};
} // namespace infernux
