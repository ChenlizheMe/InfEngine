#pragma once

#include <array>
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
/// An optional editor FPS cap can limit edit mode when explicitly requested.
struct FpsIdling
{
    float fpsIdle = 10.0f;     ///< Target FPS when idling (0 = disable idle)
    float editorFpsCap = 0.0f; ///< Max FPS in editor mode (0 = uncapped)
    bool enableIdling = true;  ///< Master switch for idle detection
    bool isIdling = false;     ///< Output — true when the last frame went idle
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
    int cooldownRemainingMs = 0;
    float targetFps = 0.0f;
    double elapsedBeforeSleepMs = 0.0;
    double frameBudgetMs = 0.0;
    double requestedSleepMs = 0.0;
    double actualSleepMs = 0.0;
    uint32_t queuedEventCount = 0;
    uint32_t mouseMotionEventCount = 0;
    uint32_t dispatchedMouseMotionCount = 0;
    double inputBeginFrameMs = 0.0;
    double inputPumpEventsMs = 0.0;
    double inputPeepEventsMs = 0.0;
    double inputImGuiDispatchMs = 0.0;
    double inputManagerDispatchMs = 0.0;
    double inputSyntheticDispatchMs = 0.0;
    double inputWindowQueryMs = 0.0;
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

    /// Pump the OS queue during long startup work while the native window is
    /// still hidden. Returns false if the user requested close/quit.
    [[nodiscard]] bool PumpStartupEvents();
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
        return m_isMinimized || m_applicationInBackground.load(std::memory_order_acquire) ||
               m_surfaceRecreationPending.load(std::memory_order_acquire);
    }
    [[nodiscard]] bool IsApplicationInBackground() const noexcept
    {
        return m_applicationInBackground.load(std::memory_order_acquire);
    }
    [[nodiscard]] bool NeedsSurfaceRecreation() const noexcept
    {
        return m_surfaceRecreationPending.load(std::memory_order_acquire);
    }
    void AcknowledgeSurfaceRecreation() noexcept
    {
        m_surfaceRecreationPending.store(false, std::memory_order_release);
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
    /// Extends the real-time idle cooldown so active editor work stays smooth
    /// regardless of the machine's current frame rate.
    void RequestFullSpeedFrame()
    {
        m_activeUntil = std::chrono::steady_clock::now() + ACTIVE_COOLDOWN_DURATION;
    }

    /// Wake an editor frame requested by a non-render thread (for example the
    /// asset watcher). The render thread consumes the activity flag before
    /// touching the non-atomic idle deadline.
    void RequestExternalWake() noexcept;

    void CreateSurface(VkInstance *vkInstance, VkSurfaceKHR *vkSurface);
    [[nodiscard]] bool TryCreateSurface(VkInstance vkInstance, VkSurfaceKHR *vkSurface) noexcept;
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
    std::atomic_bool m_applicationInBackground{false};
    std::atomic_bool m_surfaceRecreationPending{false};
    std::atomic_bool m_hasCreatedSurface{false};
    bool m_eventWatchInstalled = false;
    bool m_isPlayMode = false;
    bool m_needsImmediateGuiRefresh = false;
    InxAppMetadata m_appMetadata;

    // ---- Power-save idle state ----
    FpsIdling m_idling;
    FramePacingSample m_lastPacingSample;
    // Keep editor activity time-based. A frame-count cooldown expires almost
    // immediately on high-refresh machines and makes continuously visible
    // panels oscillate between full speed and the 10 FPS idle tier.
    static constexpr auto ACTIVE_COOLDOWN_DURATION = std::chrono::milliseconds(100);
    std::chrono::steady_clock::time_point m_activeUntil = std::chrono::steady_clock::now() + ACTIVE_COOLDOWN_DURATION;
    std::atomic_bool m_externalWakeRequested{false};

    /// Timestamp of the last frame start — used to compute the remaining
    /// frame budget so the sleep duration adapts to actual render time.
    std::chrono::steady_clock::time_point m_lastFrameStart = std::chrono::steady_clock::now();
    std::chrono::steady_clock::time_point m_lastStartupPump{};
    bool m_hasStartupPumped = false;

    mutable std::mutex m_syntheticInputMutex;
    std::deque<SyntheticInputEvent> m_syntheticInputEvents;
    uint64_t m_nextSyntheticInputSequence = 1;
    std::atomic<uint64_t> m_lastProcessedSyntheticInputSequence{0};
    double m_currentImGuiEventDispatchMs = 0.0;
    double m_currentInputManagerDispatchMs = 0.0;
    // A synthetic mouse release is held until InxRenderer confirms that a
    // real ImGui frame has consumed the corresponding press transition.
    uint8_t m_syntheticMouseButtonsAwaitingGuiFrame = 0;
    uint8_t m_syntheticMouseButtonsReadyForRelease = 0;
    // SDL does not update its global modifier state for events injected by
    // automation, so retain the synthetic contribution for subsequent keys.
    SDL_Keymod m_syntheticKeyModifiers = SDL_KMOD_NONE;

    void SDLInit();
    static bool SDLCALL WatchApplicationEvents(void *userdata, SDL_Event *event);
    uint64_t QueueSyntheticInput(SyntheticInputEvent event);
    [[nodiscard]] bool HasPendingSyntheticInput() const;
    void DrainSyntheticInputEvents(bool &hadInputEvent);
    void NotifyGuiFrameBuilt() noexcept;
    bool ProcessOneEvent(SDL_Event &event, bool syntheticScreenCoordinates = false);
};
} // namespace infernux
