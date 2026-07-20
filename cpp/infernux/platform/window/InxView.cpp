#include "InxView.h"

#include <chrono>
#include <cmath>
#include <iostream>
#include <optional>
#include <stdexcept>

#include <imgui_impl_sdl3.h>
#include <platform/filesystem/InxPath.h>
#include <platform/input/InputManager.h>
#include <stb_image.h>

namespace infernux
{
namespace
{
SDL_Keymod SyntheticModifierForScancode(SDL_Scancode scancode)
{
    switch (scancode) {
    case SDL_SCANCODE_LSHIFT:
        return SDL_KMOD_LSHIFT;
    case SDL_SCANCODE_RSHIFT:
        return SDL_KMOD_RSHIFT;
    case SDL_SCANCODE_LCTRL:
        return SDL_KMOD_LCTRL;
    case SDL_SCANCODE_RCTRL:
        return SDL_KMOD_RCTRL;
    case SDL_SCANCODE_LALT:
        return SDL_KMOD_LALT;
    case SDL_SCANCODE_RALT:
        return SDL_KMOD_RALT;
    case SDL_SCANCODE_LGUI:
        return SDL_KMOD_LGUI;
    case SDL_SCANCODE_RGUI:
        return SDL_KMOD_RGUI;
    default:
        return SDL_KMOD_NONE;
    }
}

SDL_Keymod MergeKeyModifiers(SDL_Keymod first, SDL_Keymod second)
{
    return static_cast<SDL_Keymod>(static_cast<Uint16>(first) | static_cast<Uint16>(second));
}

SDL_Keymod RemoveKeyModifiers(SDL_Keymod value, SDL_Keymod removed)
{
    return static_cast<SDL_Keymod>(static_cast<Uint16>(value) & ~static_cast<Uint16>(removed));
}

void MergeMouseMotion(SDL_Event &accumulated, const SDL_Event &latest)
{
    accumulated.motion.timestamp = latest.motion.timestamp;
    accumulated.motion.windowID = latest.motion.windowID;
    accumulated.motion.which = latest.motion.which;
    accumulated.motion.state = latest.motion.state;
    accumulated.motion.x = latest.motion.x;
    accumulated.motion.y = latest.motion.y;
    accumulated.motion.xrel += latest.motion.xrel;
    accumulated.motion.yrel += latest.motion.yrel;
}
} // namespace

InxView::InxView()
{
}

const char *const *InxView::GetVkExtensions(uint32_t *count)
{
    INXLOG_DEBUG("Get Vulkan Extensions.");
    unsigned int extensionCount = 0;
    const char *const *extensions = SDL_Vulkan_GetInstanceExtensions(&extensionCount);
    if (!extensions) {
        INXLOG_ERROR("SDL_Vulkan_GetInstanceExtensions failed: ", SDL_GetError());
        return nullptr;
    }
    if (count) {
        *count = extensionCount;
    }
    return extensions;
}

void InxView::Init(int width, int height)
{
    m_keepRunning = true;
    m_windowWidth = width;
    m_windowHeight = height;

    INXLOG_DEBUG("Initialize InxView Window with size: ", m_windowWidth, "x", m_windowHeight);
    SDLInit();
}

uint64_t InxView::QueueSyntheticKeyInput(int scancode, bool pressed, bool repeat)
{
    if (scancode <= SDL_SCANCODE_UNKNOWN || scancode >= SDL_SCANCODE_COUNT)
        return 0;

    SyntheticInputEvent event;
    event.type = SyntheticInputType::Key;
    event.keyOrButton = scancode;
    event.pressed = pressed;
    event.repeat = repeat;
    return QueueSyntheticInput(std::move(event));
}

uint64_t InxView::QueueSyntheticMouseButtonInput(int button, bool pressed, float x, float y)
{
    if (button < 0 || button > 4 || !std::isfinite(x) || !std::isfinite(y))
        return 0;

    SyntheticInputEvent event;
    event.type = SyntheticInputType::MouseButton;
    event.keyOrButton = button;
    event.pressed = pressed;
    event.x = x;
    event.y = y;
    return QueueSyntheticInput(std::move(event));
}

uint64_t InxView::QueueSyntheticMouseMotionInput(float x, float y, float deltaX, float deltaY)
{
    if (!std::isfinite(x) || !std::isfinite(y) || !std::isfinite(deltaX) || !std::isfinite(deltaY))
        return 0;

    SyntheticInputEvent event;
    event.type = SyntheticInputType::MouseMotion;
    event.x = x;
    event.y = y;
    event.deltaX = deltaX;
    event.deltaY = deltaY;
    return QueueSyntheticInput(std::move(event));
}

uint64_t InxView::QueueSyntheticMouseWheelInput(float horizontal, float vertical)
{
    if (!std::isfinite(horizontal) || !std::isfinite(vertical))
        return 0;

    SyntheticInputEvent event;
    event.type = SyntheticInputType::MouseWheel;
    event.x = horizontal;
    event.y = vertical;
    return QueueSyntheticInput(std::move(event));
}

uint64_t InxView::QueueSyntheticTextInput(const std::string &text)
{
    if (text.empty() || text.size() > 4096)
        return 0;

    SyntheticInputEvent event;
    event.type = SyntheticInputType::Text;
    event.text = text;
    return QueueSyntheticInput(std::move(event));
}

uint64_t InxView::QueueSyntheticCloseRequest()
{
    SyntheticInputEvent event;
    event.type = SyntheticInputType::CloseRequest;
    return QueueSyntheticInput(std::move(event));
}

size_t InxView::GetPendingSyntheticInputCount() const
{
    std::lock_guard<std::mutex> lock(m_syntheticInputMutex);
    return m_syntheticInputEvents.size();
}

uint64_t InxView::QueueSyntheticInput(SyntheticInputEvent event)
{
    std::lock_guard<std::mutex> lock(m_syntheticInputMutex);
    if (m_syntheticInputEvents.size() >= MAX_SYNTHETIC_INPUT_EVENTS) {
        INXLOG_WARN("Synthetic input queue is full; rejecting automation event.");
        return 0;
    }

    event.sequence = m_nextSyntheticInputSequence++;
    m_syntheticInputEvents.emplace_back(std::move(event));
    return m_syntheticInputEvents.back().sequence;
}

bool InxView::HasPendingSyntheticInput() const
{
    std::lock_guard<std::mutex> lock(m_syntheticInputMutex);
    return !m_syntheticInputEvents.empty();
}

void InxView::ProcessEvent()
{
    // Begin a new input frame: swap current → previous, clear deltas
    InputManager::Instance().SetWindow(m_window);
    InputManager::Instance().BeginFrame();

    // ====================================================================
    // Frame-rate limiter
    //
    // Three tiers:
    //   play mode      → no sleep, full speed (bypass entirely)
    //   editor active  → hard cap to editorFpsCap via SDL_Delay
    //   editor idle    → sleep via SDL_WaitEventTimeout, wake on input
    //
    // We measure elapsed time since the last frame start and sleep only
    // for the *remaining* budget.  Active mode uses SDL_Delay (hard cap);
    // idle mode uses SDL_WaitEventTimeout with a real event struct so the
    // thread wakes immediately on user input and no events are lost.
    // ====================================================================
    m_idling.isIdling = false;

    FramePacingSample pacing{};
    pacing.playModeBypass = m_isPlayMode;
    pacing.cooldownRemaining = m_activeFramesRemaining;

    SDL_Event firstEvent{};
    bool gotFirstEvent = false;

    if (!m_isPlayMode) {
        bool isIdle = m_idling.enableIdling && m_idling.fpsIdle > 0.0f && m_activeFramesRemaining <= 0 &&
                      !HasPendingSyntheticInput();
        float targetFps = isIdle ? m_idling.fpsIdle : m_idling.editorFpsCap;

        pacing.idleMode = isIdle;
        pacing.targetFps = targetFps;

        if (targetFps > 0.0f) {
            auto now = std::chrono::steady_clock::now();
            double elapsed = std::chrono::duration<double>(now - m_lastFrameStart).count();
            double budget = 1.0 / static_cast<double>(targetFps);
            double requestedSleepMs = (budget - elapsed) * 1000.0;
            int sleepMs = static_cast<int>(requestedSleepMs);

            pacing.elapsedBeforeSleepMs = elapsed * 1000.0;
            pacing.frameBudgetMs = budget * 1000.0;
            pacing.requestedSleepMs = requestedSleepMs > 0.0 ? requestedSleepMs : 0.0;

            if (sleepMs > 0) {
                if (isIdle) {
                    // Idle: block until an event arrives OR the timeout expires.
                    // A real event struct is used so the event data is preserved.
                    auto sleepStart = std::chrono::steady_clock::now();
                    gotFirstEvent = SDL_WaitEventTimeout(&firstEvent, sleepMs);

                    auto sleepEnd = std::chrono::steady_clock::now();
                    double actualSleepMs = std::chrono::duration<double, std::milli>(sleepEnd - sleepStart).count();
                    pacing.slept = true;
                    pacing.wokeByEvent = gotFirstEvent;
                    pacing.actualSleepMs = actualSleepMs;

                    m_idling.isIdling = (actualSleepMs > pacing.frameBudgetMs * 0.9);
                } else {
                    // Active editor: hard sleep for the remaining frame budget.
                    auto sleepStart = std::chrono::steady_clock::now();
                    SDL_Delay(sleepMs);
                    auto sleepEnd = std::chrono::steady_clock::now();
                    pacing.slept = true;
                    pacing.actualSleepMs = std::chrono::duration<double, std::milli>(sleepEnd - sleepStart).count();
                }
            }
        }
    }

    // Always keep m_lastFrameStart current (even in play mode) so the
    // first editor frame after exiting play mode doesn't see a huge elapsed.
    m_lastFrameStart = std::chrono::steady_clock::now();

    // ---- Poll & process all pending events ----
    bool hadInputEvent = false;

    // A high polling-rate mouse can enqueue dozens of consecutive motion
    // events between frames. ImGui only consumes the final absolute position,
    // while InputManager needs the accumulated relative delta. Coalesce only
    // adjacent motion events so button/wheel/key ordering remains exact.
    std::optional<SDL_Event> pendingMouseMotion;
    auto flushMouseMotion = [&]() {
        if (!pendingMouseMotion)
            return;
        hadInputEvent = ProcessOneEvent(*pendingMouseMotion) || hadInputEvent;
        pendingMouseMotion.reset();
    };
    auto processQueuedEvent = [&](SDL_Event &queuedEvent) {
        if (queuedEvent.type == SDL_EVENT_MOUSE_MOTION) {
            if (pendingMouseMotion)
                MergeMouseMotion(*pendingMouseMotion, queuedEvent);
            else
                pendingMouseMotion = queuedEvent;
            return;
        }
        flushMouseMotion();
        hadInputEvent = ProcessOneEvent(queuedEvent) || hadInputEvent;
    };

    // Process the event captured by SDL_WaitEventTimeout (if any)
    if (gotFirstEvent) {
        switch (firstEvent.type) {
        case SDL_EVENT_MOUSE_MOTION:
        case SDL_EVENT_MOUSE_BUTTON_DOWN:
        case SDL_EVENT_MOUSE_BUTTON_UP:
        case SDL_EVENT_MOUSE_WHEEL:
        case SDL_EVENT_KEY_DOWN:
        case SDL_EVENT_KEY_UP:
        case SDL_EVENT_TEXT_INPUT:
        case SDL_EVENT_DROP_FILE:
        case SDL_EVENT_DROP_TEXT:
            pacing.wokeByInputEvent = true;
            break;
        case SDL_EVENT_WINDOW_MINIMIZED:
        case SDL_EVENT_WINDOW_RESTORED:
        case SDL_EVENT_WINDOW_EXPOSED:
        case SDL_EVENT_WINDOW_FOCUS_GAINED:
        case SDL_EVENT_WINDOW_OCCLUDED:
            pacing.wokeByWindowEvent = true;
            break;
        default:
            pacing.wokeByOtherEvent = true;
            break;
        }
        processQueuedEvent(firstEvent);
    }

    // Drain remaining queued events
    SDL_Event event{};
    while (SDL_PollEvent(&event)) {
        processQueuedEvent(event);
        if (m_closeRequested)
            break;
    }
    flushMouseMotion();

    // Automation events remain distinct from SDL's OS queue, but they are
    // translated into SDL_Event instances and sent through ProcessOneEvent.
    // This keeps ImGui and gameplay input state in lockstep with user input.
    // A close request is only an intercepted state until Python confirms or
    // cancels it. Keep draining here so an Editor-owned Save/Discard/Cancel
    // modal remains operable by remote validation input.
    DrainSyntheticInputEvents(hadInputEvent);

    // Reset idle cooldown when user interacted
    if (hadInputEvent) {
        m_activeFramesRemaining = ACTIVE_COOLDOWN_FRAMES;
    } else if (m_activeFramesRemaining > 0) {
        --m_activeFramesRemaining;
    }

    pacing.hadInputEvent = hadInputEvent;
    pacing.cooldownRemaining = m_activeFramesRemaining;
    m_lastPacingSample = pacing;

    SDL_GetWindowSize(m_window, &m_windowWidth, &m_windowHeight);
}

bool InxView::ProcessOneEvent(SDL_Event &event)
{
    bool hadInputEvent = false;
    bool forwardToImGui = true;
    if (InputManager::Instance().IsEditorMouseCaptureActive() && event.type == SDL_EVENT_MOUSE_MOTION) {
        forwardToImGui = false;
    }

    if (forwardToImGui) {
        ImGui_ImplSDL3_ProcessEvent(&event);
    }

    InputManager::Instance().ProcessSDLEvent(event);

    switch (event.type) {
    case SDL_EVENT_MOUSE_MOTION:
    case SDL_EVENT_MOUSE_BUTTON_DOWN:
    case SDL_EVENT_MOUSE_BUTTON_UP:
    case SDL_EVENT_MOUSE_WHEEL:
    case SDL_EVENT_KEY_DOWN:
    case SDL_EVENT_KEY_UP:
    case SDL_EVENT_TEXT_INPUT:
    case SDL_EVENT_DROP_FILE:
    case SDL_EVENT_DROP_TEXT:
    case SDL_EVENT_QUIT:
        hadInputEvent = true;
        break;
    default:
        break;
    }

    if (event.type == SDL_EVENT_QUIT) {
        m_closeRequested = true;
    }

    if (event.type == SDL_EVENT_WINDOW_MINIMIZED) {
        m_isMinimized = true;
    }
    if (event.type == SDL_EVENT_WINDOW_RESTORED || event.type == SDL_EVENT_WINDOW_EXPOSED ||
        event.type == SDL_EVENT_WINDOW_FOCUS_GAINED) {
        m_isMinimized = false;
        if (event.type != SDL_EVENT_WINDOW_EXPOSED) {
            hadInputEvent = true;
        }
    }
    if (event.type == SDL_EVENT_WINDOW_OCCLUDED) {
        m_isMinimized = true;
    }
    return hadInputEvent;
}

void InxView::DrainSyntheticInputEvents(bool &hadInputEvent)
{
    std::deque<SyntheticInputEvent> events;
    {
        std::lock_guard<std::mutex> lock(m_syntheticInputMutex);
        events.swap(m_syntheticInputEvents);
    }

    const SDL_WindowID windowId = m_window ? SDL_GetWindowID(m_window) : 0;
    for (auto &synthetic : events) {
        InputManager::Instance().MarkSyntheticInputForFrame();
        SDL_Event event{};
        const Uint64 timestamp = SDL_GetTicksNS();

        switch (synthetic.type) {
        case SyntheticInputType::Key: {
            event.key.type = synthetic.pressed ? SDL_EVENT_KEY_DOWN : SDL_EVENT_KEY_UP;
            event.type = event.key.type;
            event.key.timestamp = timestamp;
            event.key.windowID = windowId;
            event.key.which = 0;
            event.key.scancode = static_cast<SDL_Scancode>(synthetic.keyOrButton);
            const SDL_Keymod modifier = SyntheticModifierForScancode(event.key.scancode);
            if (modifier != SDL_KMOD_NONE) {
                if (synthetic.pressed) {
                    m_syntheticKeyModifiers = MergeKeyModifiers(m_syntheticKeyModifiers, modifier);
                } else {
                    m_syntheticKeyModifiers = RemoveKeyModifiers(m_syntheticKeyModifiers, modifier);
                }
            }
            const SDL_Keymod effectiveModifiers = MergeKeyModifiers(SDL_GetModState(), m_syntheticKeyModifiers);
            event.key.key = SDL_GetKeyFromScancode(event.key.scancode, effectiveModifiers, true);
            event.key.mod = effectiveModifiers;
            event.key.raw = 0;
            event.key.down = synthetic.pressed;
            event.key.repeat = synthetic.repeat;
            break;
        }
        case SyntheticInputType::MouseButton:
            event.button.type = synthetic.pressed ? SDL_EVENT_MOUSE_BUTTON_DOWN : SDL_EVENT_MOUSE_BUTTON_UP;
            event.type = event.button.type;
            event.button.timestamp = timestamp;
            event.button.windowID = windowId;
            event.button.which = 0;
            switch (synthetic.keyOrButton) {
            case 0:
                event.button.button = SDL_BUTTON_LEFT;
                break;
            case 1:
                event.button.button = SDL_BUTTON_RIGHT;
                break;
            case 2:
                event.button.button = SDL_BUTTON_MIDDLE;
                break;
            case 3:
                event.button.button = SDL_BUTTON_X1;
                break;
            default:
                event.button.button = SDL_BUTTON_X2;
                break;
            }
            event.button.down = synthetic.pressed;
            event.button.clicks = 1;
            event.button.x = synthetic.x;
            event.button.y = synthetic.y;
            break;
        case SyntheticInputType::MouseMotion:
            event.motion.type = SDL_EVENT_MOUSE_MOTION;
            event.type = event.motion.type;
            event.motion.timestamp = timestamp;
            event.motion.windowID = windowId;
            event.motion.which = 0;
            event.motion.state = 0;
            event.motion.x = synthetic.x;
            event.motion.y = synthetic.y;
            event.motion.xrel = synthetic.deltaX;
            event.motion.yrel = synthetic.deltaY;
            break;
        case SyntheticInputType::MouseWheel:
            event.wheel.type = SDL_EVENT_MOUSE_WHEEL;
            event.type = event.wheel.type;
            event.wheel.timestamp = timestamp;
            event.wheel.windowID = windowId;
            event.wheel.which = 0;
            event.wheel.x = synthetic.x;
            event.wheel.y = synthetic.y;
            event.wheel.direction = SDL_MOUSEWHEEL_NORMAL;
            event.wheel.mouse_x = InputManager::Instance().GetMousePositionX();
            event.wheel.mouse_y = InputManager::Instance().GetMousePositionY();
            event.wheel.integer_x = static_cast<Sint32>(synthetic.x);
            event.wheel.integer_y = static_cast<Sint32>(synthetic.y);
            break;
        case SyntheticInputType::Text:
            event.text.type = SDL_EVENT_TEXT_INPUT;
            event.type = event.text.type;
            event.text.timestamp = timestamp;
            event.text.windowID = windowId;
            event.text.text = synthetic.text.c_str();
            break;
        case SyntheticInputType::CloseRequest:
            event.quit.type = SDL_EVENT_QUIT;
            event.type = event.quit.type;
            event.quit.timestamp = timestamp;
            break;
        }

        if (synthetic.type == SyntheticInputType::MouseButton || synthetic.type == SyntheticInputType::MouseMotion) {
            // Keep the synthetic pointer authoritative for this GUI frame. The
            // SDL ImGui backend may otherwise replace it with the physical OS
            // cursor during its release-frame fallback query.
            InputManager::Instance().SetSyntheticMousePositionForFrame(synthetic.x, synthetic.y);
        }
        if (synthetic.type == SyntheticInputType::MouseButton) {
            // ImGui consumes SDL events in order. Put the synthetic pointer at
            // the requested position before its button transition so event
            // trickling cannot apply the click at the physical OS cursor.
            SDL_Event positionEvent{};
            positionEvent.motion.type = SDL_EVENT_MOUSE_MOTION;
            positionEvent.type = positionEvent.motion.type;
            positionEvent.motion.timestamp = timestamp;
            positionEvent.motion.windowID = windowId;
            positionEvent.motion.which = 0;
            positionEvent.motion.state = 0;
            positionEvent.motion.x = synthetic.x;
            positionEvent.motion.y = synthetic.y;
            positionEvent.motion.xrel = 0.0f;
            positionEvent.motion.yrel = 0.0f;
            hadInputEvent = ProcessOneEvent(positionEvent) || hadInputEvent;
        }
        hadInputEvent = ProcessOneEvent(event) || hadInputEvent;
        m_lastProcessedSyntheticInputSequence.store(synthetic.sequence, std::memory_order_release);
        if (m_closeRequested)
            break;
    }
}

void InxView::Quit()
{
    if (m_window) {
        SDL_DestroyWindow(m_window);
        m_window = nullptr;
    }
    // Note: We intentionally don't call SDL_Quit() here to avoid
    // affecting other parts of the application (like a launcher).
    // SDL_Quit() would terminate all SDL subsystems which could
    // cause issues if the application continues running.
    INXLOG_DEBUG("Quit the InxView Window.");
}

int InxView::GetUserEvent()
{
    return m_keepRunning ? 1 : 0;
}

void InxView::Show()
{
    if (m_window) {
        SDL_ShowWindow(m_window);
    } else {
        INXLOG_ERROR("InxView Window is not initialized.");
    }
}

void InxView::Hide()
{
    if (m_window) {
        SDL_HideWindow(m_window);
    } else {
        INXLOG_ERROR("InxView Window is not initialized.");
    }
}

void InxView::SetWindowIcon(const std::string &iconPath)
{
    if (!m_window) {
        INXLOG_ERROR("Cannot set window icon: window not initialized.");
        return;
    }

    int w = 0, h = 0, channels = 0;
    // Read via ReadFileBytes to support Unicode paths on Windows
    std::vector<unsigned char> fileBytes;
    if (!ReadFileBytes(iconPath, fileBytes) || fileBytes.empty()) {
        INXLOG_ERROR("Failed to read icon file: ", iconPath);
        return;
    }
    unsigned char *pixels =
        stbi_load_from_memory(fileBytes.data(), static_cast<int>(fileBytes.size()), &w, &h, &channels, 4);
    if (!pixels) {
        INXLOG_ERROR("Failed to load icon: ", iconPath);
        return;
    }

    SDL_Surface *surface = SDL_CreateSurfaceFrom(w, h, SDL_PIXELFORMAT_RGBA32, pixels, w * 4);
    if (surface) {
        SDL_SetWindowIcon(m_window, surface);
        SDL_DestroySurface(surface);
        INXLOG_DEBUG("Window icon set from: ", iconPath);
    } else {
        INXLOG_ERROR("Failed to create SDL surface for icon: ", SDL_GetError());
    }

    stbi_image_free(pixels);
}

void InxView::SetWindowFullscreen(bool fullscreen)
{
    if (!m_window) {
        INXLOG_ERROR("Cannot set fullscreen: window not initialized.");
        return;
    }
    if (!SDL_SetWindowFullscreen(m_window, fullscreen)) {
        INXLOG_ERROR("SDL_SetWindowFullscreen failed: ", SDL_GetError());
    }
}

void InxView::SetWindowTitle(const std::string &title)
{
    if (!m_window) {
        INXLOG_ERROR("Cannot set window title: window not initialized.");
        return;
    }
    if (!SDL_SetWindowTitle(m_window, title.c_str())) {
        INXLOG_ERROR("SDL_SetWindowTitle failed: ", SDL_GetError());
    }
}

void InxView::SetWindowMaximized(bool maximized)
{
    if (!m_window) {
        INXLOG_ERROR("Cannot set maximized: window not initialized.");
        return;
    }
    if (maximized) {
        SDL_MaximizeWindow(m_window);
    } else {
        SDL_RestoreWindow(m_window);
    }
}

void InxView::SetWindowResizable(bool resizable)
{
    if (!m_window) {
        INXLOG_ERROR("Cannot set resizable: window not initialized.");
        return;
    }
    SDL_SetWindowResizable(m_window, resizable);
}

void InxView::SDLInit()
{
    SDL_SetLogPriorities(SDL_LOG_PRIORITY_VERBOSE);
    if (!SDL_Init(SDL_INIT_VIDEO | SDL_INIT_AUDIO)) {
        const std::string error = SDL_GetError();
        INXLOG_ERROR("SDL_Init failed: ", error);
        throw std::runtime_error("SDL initialization failed: " + error);
    }
    INXLOG_DEBUG("SDL_Init succeeded.");

    INXLOG_DEBUG("Window engine: SDL Vulkan");
    m_window =
        SDL_CreateWindow(m_appMetadata.appName, m_windowWidth, m_windowHeight,
                         SDL_WINDOW_RESIZABLE | SDL_WINDOW_VULKAN | SDL_WINDOW_HIDDEN | SDL_WINDOW_HIGH_PIXEL_DENSITY);
    if (!m_window) {
        const std::string error = SDL_GetError();
        INXLOG_ERROR("Could not create a window: ", error);
        throw std::runtime_error("SDL window creation failed: " + error);
    }
    INXLOG_DEBUG("Window created successfully.");

    SDL_MaximizeWindow(m_window);
}

void InxView::CreateSurface(VkInstance *vkInstance, VkSurfaceKHR *vkSurface)
{
    if (!m_window || !vkInstance || *vkInstance == VK_NULL_HANDLE || !vkSurface) {
        throw std::runtime_error("Cannot create Vulkan surface before the window and instance are initialized");
    }
    if (!SDL_Vulkan_CreateSurface(m_window, *vkInstance, nullptr, vkSurface)) {
        const std::string error = SDL_GetError();
        INXLOG_ERROR("Could not create Vulkan surface: ", error);
        throw std::runtime_error("Vulkan surface creation failed: " + error);
    }
    INXLOG_DEBUG("Vulkan surface created successfully.");
}

void InxView::SetAppMetadata(InxAppMetadata appMetaData)
{
    m_appMetadata = appMetaData;
    INXLOG_DEBUG("Set InxView application metadata: ", m_appMetadata.appName);
}
} // namespace infernux
