#include "InxView.h"

#include <algorithm>
#include <array>
#include <chrono>
#include <cmath>
#include <cstdlib>
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

void InxView::RequestExternalWake() noexcept
{
    m_externalWakeRequested.store(true, std::memory_order_release);
    SDL_Event event{};
    event.type = SDL_EVENT_USER;
    SDL_PushEvent(&event);
}

void InxView::ProcessEvent()
{
    const auto inputFrameStart = std::chrono::steady_clock::now();
    // Begin a new input frame: clear transition edges and per-frame deltas.
    InputManager::Instance().SetWindow(m_window);
    InputManager::Instance().BeginFrame();
    const auto inputFrameReady = std::chrono::steady_clock::now();
    m_needsImmediateGuiRefresh = false;
    m_currentImGuiEventDispatchMs = 0.0;
    m_currentInputManagerDispatchMs = 0.0;

    if (m_externalWakeRequested.exchange(false, std::memory_order_acq_rel))
        m_activeUntil = std::chrono::steady_clock::now() + ACTIVE_COOLDOWN_DURATION;

    // ====================================================================
    // Frame-rate limiter
    //
    // Three tiers:
    //   play mode      → optional explicit cap, otherwise full speed
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
    const auto pacingStart = std::chrono::steady_clock::now();
    pacing.cooldownRemainingMs = static_cast<int>(std::max<int64_t>(
        0, std::chrono::duration_cast<std::chrono::milliseconds>(m_activeUntil - pacingStart).count()));

    SDL_Event firstEvent{};
    bool gotFirstEvent = false;

    const bool isIdle = !m_isPlayMode && m_idling.enableIdling && m_idling.fpsIdle > 0.0f &&
                        pacingStart >= m_activeUntil && !HasPendingSyntheticInput();
    const float targetFps = m_isPlayMode ? m_idling.playFpsCap : (isIdle ? m_idling.fpsIdle : m_idling.editorFpsCap);
    pacing.playModeBypass = m_isPlayMode && targetFps <= 0.0f;

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
                // Active editor or capped play mode: hard sleep for the
                // remaining frame budget without delaying event dispatch more
                // than one frame.
                auto sleepStart = std::chrono::steady_clock::now();
                SDL_Delay(sleepMs);
                auto sleepEnd = std::chrono::steady_clock::now();
                pacing.slept = true;
                pacing.actualSleepMs = std::chrono::duration<double, std::milli>(sleepEnd - sleepStart).count();
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
        ++pacing.dispatchedMouseMotionCount;
        pendingMouseMotion.reset();
    };
    auto processQueuedEvent = [&](SDL_Event &queuedEvent) {
        ++pacing.queuedEventCount;
        if (queuedEvent.type == SDL_EVENT_MOUSE_MOTION) {
            ++pacing.mouseMotionEventCount;
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
        case SDL_EVENT_FINGER_DOWN:
        case SDL_EVENT_FINGER_MOTION:
        case SDL_EVENT_FINGER_UP:
        case SDL_EVENT_FINGER_CANCELED:
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

    // Preserve SDL's complete native event semantics. Only adjacent motion
    // events are coalesced after SDL has translated them.
    const auto pollStart = std::chrono::steady_clock::now();
    SDL_Event event{};
    while (SDL_PollEvent(&event)) {
        processQueuedEvent(event);
        if (m_closeRequested)
            break;
    }
    flushMouseMotion();
    const auto pollEnd = std::chrono::steady_clock::now();

    // Automation events remain distinct from SDL's OS queue, but they are
    // translated into SDL_Event instances and sent through ProcessOneEvent.
    // This keeps ImGui and gameplay input state in lockstep with user input.
    // A close request is only an intercepted state until Python confirms or
    // cancels it. Keep draining here so an Editor-owned Save/Discard/Cancel
    // modal remains operable by remote validation input.
    const auto syntheticStart = std::chrono::steady_clock::now();
    DrainSyntheticInputEvents(hadInputEvent);
    const auto syntheticEnd = std::chrono::steady_clock::now();

    // Reset idle cooldown when user interacted
    if (hadInputEvent) {
        m_activeUntil = std::chrono::steady_clock::now() + ACTIVE_COOLDOWN_DURATION;
    }

    pacing.hadInputEvent = hadInputEvent;
    pacing.cooldownRemainingMs = static_cast<int>(std::max<int64_t>(
        0, std::chrono::duration_cast<std::chrono::milliseconds>(m_activeUntil - std::chrono::steady_clock::now())
               .count()));
    const auto windowQueryStart = std::chrono::steady_clock::now();
    SDL_GetWindowSize(m_window, &m_windowWidth, &m_windowHeight);
    const auto windowQueryEnd = std::chrono::steady_clock::now();
    const auto milliseconds = [](auto begin, auto end) {
        return std::chrono::duration<double, std::milli>(end - begin).count();
    };
    pacing.inputBeginFrameMs = milliseconds(inputFrameStart, inputFrameReady);
    pacing.inputPumpEventsMs = 0.0;
    pacing.inputPeepEventsMs = milliseconds(pollStart, pollEnd);
    pacing.inputImGuiDispatchMs = m_currentImGuiEventDispatchMs;
    pacing.inputManagerDispatchMs = m_currentInputManagerDispatchMs;
    pacing.inputSyntheticDispatchMs = milliseconds(syntheticStart, syntheticEnd);
    pacing.inputWindowQueryMs = milliseconds(windowQueryStart, windowQueryEnd);
    m_lastPacingSample = pacing;
}

bool InxView::ProcessOneEvent(SDL_Event &event, bool syntheticScreenCoordinates)
{
    bool hadInputEvent = false;
    bool forwardToImGui = true;
    if (InputManager::Instance().IsEditorMouseCaptureActive() && event.type == SDL_EVENT_MOUSE_MOTION) {
        forwardToImGui = false;
    }

    if (forwardToImGui) {
        // Dear ImGui's SDL backend does not consume SDL drop-position/file
        // events. Re-publish the drop coordinates as pointer motion first so
        // docked panels resolve the OS gesture against the actual drop target,
        // including the first frame after the Editor regains focus.
        if (event.type == SDL_EVENT_DROP_POSITION || event.type == SDL_EVENT_DROP_FILE ||
            event.type == SDL_EVENT_DROP_TEXT || event.type == SDL_EVENT_DROP_COMPLETE) {
            SDL_Event dropPointerEvent{};
            dropPointerEvent.type = SDL_EVENT_MOUSE_MOTION;
            dropPointerEvent.motion.windowID = event.drop.windowID;
            dropPointerEvent.motion.x = event.drop.x;
            dropPointerEvent.motion.y = event.drop.y;
            ImGui_ImplSDL3_ProcessEvent(&dropPointerEvent);
        }
        SDL_Event imguiEvent = event;
        if (syntheticScreenCoordinates && imguiEvent.type == SDL_EVENT_MOUSE_MOTION &&
            (ImGui::GetIO().ConfigFlags & ImGuiConfigFlags_ViewportsEnable) != 0 && m_window) {
            // Semantic item rectangles use Dear ImGui screen coordinates when
            // multi-viewport support is enabled. SDL mouse events are local to
            // their source window and the backend adds that window's origin.
            // Convert only the backend copy; gameplay input keeps the exact
            // coordinates submitted by automation.
            int windowX = 0;
            int windowY = 0;
            SDL_GetWindowPosition(m_window, &windowX, &windowY);
            imguiEvent.motion.x -= static_cast<float>(windowX);
            imguiEvent.motion.y -= static_cast<float>(windowY);
        }
        const auto imguiStart = std::chrono::steady_clock::now();
        ImGui_ImplSDL3_ProcessEvent(&imguiEvent);
        m_currentImGuiEventDispatchMs +=
            std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - imguiStart).count();
    }

    const auto inputManagerStart = std::chrono::steady_clock::now();
    InputManager::Instance().ProcessSDLEvent(event);
    m_currentInputManagerDispatchMs +=
        std::chrono::duration<double, std::milli>(std::chrono::steady_clock::now() - inputManagerStart).count();

    switch (event.type) {
    case SDL_EVENT_MOUSE_MOTION:
        hadInputEvent = true;
        // Pointer motion is sampled by InputManager every engine frame, while
        // editor hover visuals already have a dedicated 60 Hz GUI cadence.
        // Forcing an ImGui/Python rebuild for every high-polling or remote
        // desktop motion packet made editor rendering scale with mouse event
        // rate. Buttons, wheels, keys and drops still request an immediate
        // GUI frame below because those transitions must never wait.
        break;
    case SDL_EVENT_MOUSE_BUTTON_DOWN:
    case SDL_EVENT_MOUSE_BUTTON_UP:
    case SDL_EVENT_MOUSE_WHEEL:
    case SDL_EVENT_KEY_DOWN:
    case SDL_EVENT_KEY_UP:
    case SDL_EVENT_TEXT_INPUT:
    case SDL_EVENT_FINGER_DOWN:
    case SDL_EVENT_FINGER_MOTION:
    case SDL_EVENT_FINGER_UP:
    case SDL_EVENT_FINGER_CANCELED:
    case SDL_EVENT_DROP_FILE:
    case SDL_EVENT_DROP_TEXT:
    case SDL_EVENT_QUIT:
        hadInputEvent = true;
        m_needsImmediateGuiRefresh = true;
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
        m_needsImmediateGuiRefresh = true;
        if (event.type != SDL_EVENT_WINDOW_EXPOSED) {
            hadInputEvent = true;
        }
    }
    if (event.type == SDL_EVENT_WINDOW_OCCLUDED) {
        m_isMinimized = true;
    }
    if (event.type == SDL_EVENT_WINDOW_RESIZED || event.type == SDL_EVENT_WINDOW_PIXEL_SIZE_CHANGED) {
        m_needsImmediateGuiRefresh = true;
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
    std::deque<SyntheticInputEvent> deferredEvents;
    bool mouseButtonPressedInBatch[5] = {};
    bool pointerMotionProcessedInBatch = false;
    for (size_t eventIndex = 0; eventIndex < events.size(); ++eventIndex) {
        auto &synthetic = events[eventIndex];

        // A synthetic click may be submitted as motion + press + release in
        // one MCP call. Keep the release on the next graphical frame when it
        // follows a press from this same batch. This preserves ImGui's
        // press-frame/active-id and release-frame/click contract while still
        // sending both transitions through the normal SDL path.
        const bool isValidMouseButton = synthetic.type == SyntheticInputType::MouseButton &&
                                        synthetic.keyOrButton >= 0 && synthetic.keyOrButton < 5;
        const uint8_t mouseButtonMask = isValidMouseButton ? static_cast<uint8_t>(1u << synthetic.keyOrButton) : 0;
        bool hasMatchingReleaseLater = false;
        if (isValidMouseButton && synthetic.pressed) {
            for (size_t laterIndex = eventIndex + 1; laterIndex < events.size(); ++laterIndex) {
                const auto &later = events[laterIndex];
                if (later.type == SyntheticInputType::MouseButton && later.keyOrButton == synthetic.keyOrButton &&
                    !later.pressed) {
                    hasMatchingReleaseLater = true;
                    break;
                }
            }
        }
        // Dear ImGui decides window moving/focus ownership in NewFrame(),
        // before this frame's widgets are submitted. A motion and press
        // consumed together therefore cannot establish the hovered widget in
        // time: the press may be claimed by the window itself. Mirror a real
        // pointer by publishing motion, press, and release on successive GUI
        // build boundaries. Apply this only to a complete click transaction;
        // gameplay batches that intentionally combine motion and a held press
        // must still reach InputManager in one simulation frame.
        const bool waitingForPointerHoverFrame =
            isValidMouseButton && synthetic.pressed && hasMatchingReleaseLater && pointerMotionProcessedInBatch;
        const bool waitingForGuiFrame = isValidMouseButton && !synthetic.pressed &&
                                        (mouseButtonPressedInBatch[synthetic.keyOrButton] ||
                                         ((m_syntheticMouseButtonsAwaitingGuiFrame & mouseButtonMask) != 0 &&
                                          (m_syntheticMouseButtonsReadyForRelease & mouseButtonMask) == 0));
        if (waitingForPointerHoverFrame || waitingForGuiFrame) {
            for (size_t deferredIndex = eventIndex; deferredIndex < events.size(); ++deferredIndex)
                deferredEvents.emplace_back(std::move(events[deferredIndex]));
            break;
        }

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
        if (synthetic.type == SyntheticInputType::MouseButton || synthetic.type == SyntheticInputType::MouseMotion) {
            // SDL3's ImGui backend tracks the viewport under the pointer from
            // WINDOW_MOUSE_ENTER, not from a synthetic motion event. Establish
            // that ownership before every synthetic pointer transition;
            // otherwise a remote click/drag can be routed using the user's
            // physical mouse window.
            SDL_Event enterEvent{};
            enterEvent.window.type = SDL_EVENT_WINDOW_MOUSE_ENTER;
            enterEvent.type = enterEvent.window.type;
            enterEvent.window.timestamp = timestamp;
            enterEvent.window.windowID = windowId;
            hadInputEvent = ProcessOneEvent(enterEvent) || hadInputEvent;
        }
        if (synthetic.type == SyntheticInputType::MouseButton) {

            // ImGui consumes SDL events in order. Put the synthetic pointer at
            // the requested position before its button transition so both
            // press and release use the same local viewport coordinates.
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
            hadInputEvent = ProcessOneEvent(positionEvent, true) || hadInputEvent;
        }
        InputManager::Instance().TrackSyntheticEvent(event);
        hadInputEvent = ProcessOneEvent(event, synthetic.type == SyntheticInputType::MouseMotion) || hadInputEvent;
        if (synthetic.type == SyntheticInputType::MouseMotion)
            pointerMotionProcessedInBatch = true;
        if (isValidMouseButton && synthetic.pressed) {
            mouseButtonPressedInBatch[synthetic.keyOrButton] = true;
            m_syntheticMouseButtonsAwaitingGuiFrame |= mouseButtonMask;
            m_syntheticMouseButtonsReadyForRelease &= static_cast<uint8_t>(~mouseButtonMask);
        } else if (isValidMouseButton) {
            m_syntheticMouseButtonsAwaitingGuiFrame &= static_cast<uint8_t>(~mouseButtonMask);
            m_syntheticMouseButtonsReadyForRelease &= static_cast<uint8_t>(~mouseButtonMask);
        }
        m_lastProcessedSyntheticInputSequence.store(synthetic.sequence, std::memory_order_release);
        if (m_closeRequested)
            break;
    }

    if (!deferredEvents.empty()) {
        std::lock_guard<std::mutex> lock(m_syntheticInputMutex);
        std::deque<SyntheticInputEvent> queuedAfterDrain;
        queuedAfterDrain.swap(m_syntheticInputEvents);
        while (!deferredEvents.empty()) {
            m_syntheticInputEvents.emplace_back(std::move(deferredEvents.front()));
            deferredEvents.pop_front();
        }
        while (!queuedAfterDrain.empty()) {
            m_syntheticInputEvents.emplace_back(std::move(queuedAfterDrain.front()));
            queuedAfterDrain.pop_front();
        }
    }
}

void InxView::NotifyGuiFrameBuilt() noexcept
{
    // This is called only after InxGUI::BuildFrameIfDue() returned true. It
    // therefore marks the exact boundary at which ImGui::NewFrame() and the
    // Python renderables have consumed the synthetic press state.
    m_syntheticMouseButtonsReadyForRelease |= m_syntheticMouseButtonsAwaitingGuiFrame;
}

void InxView::Quit()
{
    if (m_eventWatchInstalled) {
        SDL_RemoveEventWatch(&InxView::WatchApplicationEvents, this);
        m_eventWatchInstalled = false;
    }
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

bool InxView::PumpStartupEvents()
{
    if (m_closeRequested)
        return false;

    const auto now = std::chrono::steady_clock::now();
    if (m_hasStartupPumped && now - m_lastStartupPump < std::chrono::milliseconds(50))
        return true;
    m_hasStartupPumped = true;
    m_lastStartupPump = now;

    if (!m_window)
        return true;

    SDL_PumpEvents();

    SDL_Event event{};
    while (SDL_PeepEvents(&event, 1, SDL_GETEVENT, SDL_EVENT_QUIT, SDL_EVENT_QUIT) > 0)
        m_closeRequested = true;
    while (SDL_PeepEvents(&event, 1, SDL_GETEVENT, SDL_EVENT_WINDOW_CLOSE_REQUESTED, SDL_EVENT_WINDOW_CLOSE_REQUESTED) >
           0)
        m_closeRequested = true;
    return !m_closeRequested;
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
    // Touch is a first-class input stream. Compatibility mouse synthesis would
    // otherwise deliver one physical contact through both APIs and cause
    // duplicate gameplay/UI actions on Android and mobile Web.
    SDL_SetHint(SDL_HINT_TOUCH_MOUSE_EVENTS, "0");
    SDL_SetHint(SDL_HINT_MOUSE_TOUCH_EVENTS, "0");
    if (!SDL_Init(SDL_INIT_VIDEO | SDL_INIT_AUDIO)) {
        const std::string error = SDL_GetError();
        INXLOG_ERROR("SDL_Init failed: ", error);
        throw std::runtime_error("SDL initialization failed: " + error);
    }
    INXLOG_DEBUG("SDL_Init succeeded.");
    if (!SDL_AddEventWatch(&InxView::WatchApplicationEvents, this)) {
        INXLOG_WARN("Could not install SDL application lifecycle watch: ", SDL_GetError());
    } else {
        m_eventWatchInstalled = true;
    }

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

    const char *playerModeFlag = std::getenv("_INFERNUX_PLAYER_MODE");
    const bool playerMode = playerModeFlag != nullptr && playerModeFlag[0] == '1' && playerModeFlag[1] == '\0';
    if (!playerMode)
        SDL_MaximizeWindow(m_window);
}

void InxView::CreateSurface(VkInstance *vkInstance, VkSurfaceKHR *vkSurface)
{
    if (!m_window || !vkInstance || *vkInstance == VK_NULL_HANDLE || !vkSurface) {
        throw std::runtime_error("Cannot create Vulkan surface before the window and instance are initialized");
    }
    if (!TryCreateSurface(*vkInstance, vkSurface)) {
        const std::string error = SDL_GetError();
        INXLOG_ERROR("Could not create Vulkan surface: ", error);
        throw std::runtime_error("Vulkan surface creation failed: " + error);
    }
    INXLOG_DEBUG("Vulkan surface created successfully.");
}

bool InxView::TryCreateSurface(VkInstance vkInstance, VkSurfaceKHR *vkSurface) noexcept
{
    if (!m_window || vkInstance == VK_NULL_HANDLE || !vkSurface)
        return false;
    *vkSurface = VK_NULL_HANDLE;
    const bool created = SDL_Vulkan_CreateSurface(m_window, vkInstance, nullptr, vkSurface);
    if (created)
        m_hasCreatedSurface.store(true, std::memory_order_release);
    return created;
}

bool SDLCALL InxView::WatchApplicationEvents(void *userdata, SDL_Event *event)
{
    auto *view = static_cast<InxView *>(userdata);
    if (!view || !event)
        return true;

    switch (event->type) {
    case SDL_EVENT_WILL_ENTER_BACKGROUND:
    case SDL_EVENT_DID_ENTER_BACKGROUND:
        // SDL requires mobile lifecycle events to be handled from an event
        // watch: Android may suspend the normal event loop immediately after
        // delivering them. Stop presentation before SurfaceView tears down.
        view->m_applicationInBackground.store(true, std::memory_order_release);
#if defined(SDL_PLATFORM_ANDROID) || defined(__ANDROID__) || defined(ANDROID)
        // Android replaces the ANativeWindow while an Activity is backgrounded.
        // The old VkSurfaceKHR and its swapchain cannot be reused on resume.
        view->m_surfaceRecreationPending.store(true, std::memory_order_release);
#endif
        break;
    case SDL_EVENT_WILL_ENTER_FOREGROUND:
        // Keep presentation suspended until the new native surface is ready.
        break;
    case SDL_EVENT_DID_ENTER_FOREGROUND:
        view->m_applicationInBackground.store(false, std::memory_order_release);
        view->RequestExternalWake();
        break;
#if defined(SDL_PLATFORM_ANDROID) || defined(__ANDROID__) || defined(ANDROID)
    case SDL_EVENT_WINDOW_RESIZED:
    case SDL_EVENT_WINDOW_PIXEL_SIZE_CHANGED:
        // Android may replace the SurfaceView buffer queue after the Activity
        // has already resumed (notably during fixed-rotation transitions).
        // Recreating only on DID_ENTER_FOREGROUND can therefore bind Vulkan to
        // the retiring ANativeWindow and leave the renderer dequeuing from an
        // abandoned BufferQueue. Once the initial surface exists, any native
        // pixel-size transition is a presentation-surface boundary.
        if (view->m_hasCreatedSurface.load(std::memory_order_acquire)) {
            view->m_surfaceRecreationPending.store(true, std::memory_order_release);
            view->RequestExternalWake();
        }
        break;
#endif
    default:
        break;
    }
    return true;
}

void InxView::SetAppMetadata(InxAppMetadata appMetaData)
{
    m_appMetadata = appMetaData;
    INXLOG_DEBUG("Set InxView application metadata: ", m_appMetadata.appName);
}
} // namespace infernux
