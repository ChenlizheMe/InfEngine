#include <platform/input/InputManager.h>

#include <SDL3/SDL.h>
#include <cassert>
#include <stdexcept>

using infernux::InputManager;
using infernux::TouchPhase;

namespace
{
SDL_Event KeyEvent(SDL_EventType type, SDL_Scancode scancode)
{
    SDL_Event event{};
    event.type = type;
    event.key.scancode = scancode;
    return event;
}

SDL_Event MouseButtonEvent(SDL_EventType type, Uint8 button)
{
    SDL_Event event{};
    event.type = type;
    event.button.button = button;
    return event;
}

SDL_Event TouchEvent(SDL_EventType type, Uint64 touchId, Uint64 fingerId, float x, float y, float dx = 0.0f,
                     float dy = 0.0f, float pressure = 1.0f)
{
    SDL_Event event{};
    event.type = type;
    event.tfinger.touchID = touchId;
    event.tfinger.fingerID = fingerId;
    event.tfinger.timestamp = 123456789;
    event.tfinger.windowID = 7;
    event.tfinger.x = x;
    event.tfinger.y = y;
    event.tfinger.dx = dx;
    event.tfinger.dy = dy;
    event.tfinger.pressure = pressure;
    return event;
}
} // namespace

int main()
{
    auto &input = InputManager::Instance();
    input.ResetAll();

    input.BeginFrame();
    auto keyDown = KeyEvent(SDL_EVENT_KEY_DOWN, SDL_SCANCODE_W);
    auto keyUp = KeyEvent(SDL_EVENT_KEY_UP, SDL_SCANCODE_W);
    input.TrackSyntheticEvent(keyDown);
    input.ProcessSDLEvent(keyDown);
    assert(input.HasSyntheticGameplayInput());
    assert(input.GetKeyDown(SDL_SCANCODE_W));
    assert(input.GetKey(SDL_SCANCODE_W));
    assert(input.AnyKeyDown());
    input.BeginFrame();
    assert(input.HasSyntheticGameplayInput());
    input.TrackSyntheticEvent(keyUp);
    input.ProcessSDLEvent(keyUp);
    assert(!input.GetKeyDown(SDL_SCANCODE_W));
    assert(input.GetKeyUp(SDL_SCANCODE_W));
    assert(!input.GetKey(SDL_SCANCODE_W));
    assert(!input.AnyKeyDown());
    assert(input.IsSyntheticInputFrame());

    input.BeginFrame();
    assert(!input.HasSyntheticGameplayInput());
    assert(!input.GetKeyDown(SDL_SCANCODE_W));
    assert(!input.GetKeyUp(SDL_SCANCODE_W));

    // Background MCP validation owns its synthetic held state independently
    // from the physical window focus lifecycle.
    input.TrackSyntheticEvent(keyDown);
    input.ProcessSDLEvent(keyDown);
    auto focusLost = SDL_Event{};
    focusLost.type = SDL_EVENT_WINDOW_FOCUS_LOST;
    input.ProcessSDLEvent(focusLost);
    assert(input.HasSyntheticGameplayInput());
    assert(input.GetKey(SDL_SCANCODE_W));
    input.TrackSyntheticEvent(keyUp);
    input.ProcessSDLEvent(keyUp);
    assert(input.HasSyntheticGameplayInput());
    assert(input.IsSyntheticInputFrame());
    assert(!input.GetKey(SDL_SCANCODE_W));
    input.BeginFrame();
    assert(!input.HasSyntheticGameplayInput());

    // A physical key still releases normally on focus loss.
    input.ProcessSDLEvent(keyDown);
    assert(input.GetKey(SDL_SCANCODE_W));
    input.ProcessSDLEvent(focusLost);
    assert(!input.GetKey(SDL_SCANCODE_W));

    input.ProcessSDLEvent(keyDown);
    input.ProcessSDLEvent(keyDown);
    assert(input.GetKeyDown(SDL_SCANCODE_W));
    assert(input.GetKey(SDL_SCANCODE_W));
    input.BeginFrame();
    input.ProcessSDLEvent(keyDown);
    assert(!input.GetKeyDown(SDL_SCANCODE_W));
    input.ProcessSDLEvent(keyUp);
    assert(input.GetKeyUp(SDL_SCANCODE_W));

    input.BeginFrame();
    auto mouseDown = MouseButtonEvent(SDL_EVENT_MOUSE_BUTTON_DOWN, SDL_BUTTON_LEFT);
    auto mouseUp = MouseButtonEvent(SDL_EVENT_MOUSE_BUTTON_UP, SDL_BUTTON_LEFT);
    input.ProcessSDLEvent(mouseDown);
    input.ProcessSDLEvent(mouseUp);
    assert(input.GetMouseButtonDown(0));
    assert(input.GetMouseButtonUp(0));
    assert(!input.GetMouseButton(0));

    // Touch contacts persist across frames, retain stable first-contact order,
    // and publish terminal phases for exactly one frame.
    input.ResetAll();
    input.BeginFrame();
    input.ProcessSDLEvent(TouchEvent(SDL_EVENT_FINGER_DOWN, 3, 101, 0.25f, 0.75f, 0.0f, 0.0f, 0.6f));
    input.ProcessSDLEvent(TouchEvent(SDL_EVENT_FINGER_DOWN, 3, 202, 0.80f, 0.20f));
    assert(input.GetTouchCount() == 2);
    assert(input.GetTouch(0).fingerId == 101);
    assert(input.GetTouch(0).phase == TouchPhase::Began);
    assert(input.GetTouch(0).pressure == 0.6f);
    assert(input.GetTouch(1).fingerId == 202);

    input.BeginFrame();
    assert(input.GetTouchCount() == 2);
    assert(input.GetTouch(0).phase == TouchPhase::Stationary);
    input.ProcessSDLEvent(TouchEvent(SDL_EVENT_FINGER_MOTION, 3, 101, 0.30f, 0.70f, 0.05f, -0.05f, 0.7f));
    assert(input.GetTouch(0).phase == TouchPhase::Moved);
    assert(input.GetTouch(0).deltaX == 0.05f);
    input.ProcessSDLEvent(TouchEvent(SDL_EVENT_FINGER_UP, 3, 202, 0.80f, 0.20f));
    assert(input.GetTouch(1).phase == TouchPhase::Ended);

    input.BeginFrame();
    assert(input.GetTouchCount() == 1);
    assert(input.GetTouch(0).fingerId == 101);
    input.ProcessSDLEvent(focusLost);
    assert(input.GetTouchCount() == 1);
    assert(input.GetTouch(0).phase == TouchPhase::Canceled);
    input.BeginFrame();
    assert(input.GetTouchCount() == 0);

    bool invalidTouchRejected = false;
    try {
        static_cast<void>(input.GetTouch(0));
    } catch (const std::out_of_range &) {
        invalidTouchRejected = true;
    }
    assert(invalidTouchRejected);

    // SDL compatibility mouse events generated from touch must not create a
    // second gameplay action beside the first-class touch stream.
    input.BeginFrame();
    auto compatibilityMouse = MouseButtonEvent(SDL_EVENT_MOUSE_BUTTON_DOWN, SDL_BUTTON_LEFT);
    compatibilityMouse.button.which = SDL_TOUCH_MOUSEID;
    input.ProcessSDLEvent(compatibilityMouse);
    assert(!input.GetMouseButtonDown(0));
    assert(!input.GetMouseButton(0));

    // Non-SDL hosts feed the same semantic state machine directly. Browser
    // key, pointer, text, wheel, and touch events must therefore preserve the
    // desktop edge and frame-lifecycle contract.
    input.ResetAll();
    input.BeginFrame();
    input.ProcessKeyEvent(SDL_SCANCODE_A, true);
    input.ProcessPointerButtonEvent(1, true);
    input.ProcessPointerMotionEvent(120.0f, 80.0f, 4.0f, -2.0f);
    input.ProcessScrollEvent(0.5f, -1.5f);
    input.ProcessTextInputEvent("web");
    input.ProcessTouchEvent(8, 42, 999, 0, 0.2f, 0.3f, 0.0f, 0.0f, 0.75f, TouchPhase::Began);
    assert(input.GetKeyDown(SDL_SCANCODE_A));
    assert(input.GetKey(SDL_SCANCODE_A));
    assert(input.GetMouseButtonDown(1));
    assert(input.GetMouseButton(1));
    assert(input.GetMousePositionX() == 120.0f);
    assert(input.GetMousePositionY() == 80.0f);
    assert(input.GetMouseDeltaX() == 4.0f);
    assert(input.GetMouseDeltaY() == -2.0f);
    assert(input.GetMouseScrollDeltaX() == 0.5f);
    assert(input.GetMouseScrollDeltaY() == -1.5f);
    assert(input.GetInputString() == "web");
    assert(input.GetTouchCount() == 1);
    assert(input.GetTouch(0).phase == TouchPhase::Began);

    input.BeginFrame();
    input.ProcessKeyEvent(SDL_SCANCODE_A, false);
    input.ProcessPointerButtonEvent(1, false);
    input.ProcessTouchEvent(8, 42, 1000, 0, 0.2f, 0.3f, 0.0f, 0.0f, 0.0f, TouchPhase::Ended);
    assert(input.GetKeyUp(SDL_SCANCODE_A));
    assert(input.GetMouseButtonUp(1));
    assert(input.GetTouch(0).phase == TouchPhase::Ended);

    input.ResetAll();
    return 0;
}
